#!/usr/bin/env python
"""ArchWiki 爬虫启动入口。

本环境会为每条命令稳定双拉起爬虫进程，导致两个实例并发写同一缓存（WinError 32）。
用 os.open(O_CREAT | O_EXCL) 原子创建锁文件保证全局仅一个实例能持有锁（其余直接退出）；
锁文件内写入 PID，用于陈旧锁清理：若持锁进程已死（被沙箱回收等），重跑时移除陈旧锁并接管。
"""
import os
import sys
import time
import atexit
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))


def _arg_value(flag):
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + '='):
            return a.split('=', 1)[1]
    return None


def _pid_alive(pid):
    # 注意：os.kill(pid, 0) 对本环境 DETACHED（跨会话）进程会误报 OSError(22 参数错误)，
    # 故改用 ctypes OpenProcess(PROCESS_QUERY_INFORMATION) 直接查进程表，跨会话可靠。
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        h = k.OpenProcess(0x400, 0, pid)
        if h:
            k.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def _acquire_lock():
    # 注意：内置 hash() 每进程随机加盐，不能用于跨进程锁文件名；用确定性 md5
    key = hashlib.md5(os.path.abspath(_arg_value('--cache-dir') or 'full_cache').encode('utf-8')).hexdigest()
    lock = os.path.join(HERE, 'archwiki_crawl_%s.lock' % key)
    for _ in range(5):
        if os.path.exists(lock):
            try:
                pid = int((open(lock).read() or '').strip() or 0)
                if pid and _pid_alive(pid):
                    return None  # 持锁进程仍存活 -> 放弃
            except Exception:
                pass
            try:
                os.remove(lock)  # 陈旧锁，清理后重试
            except OSError:
                pass
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            time.sleep(0.3)
            continue
        try:
            os.write(fd, str(os.getpid()).encode('utf-8'))
            os.fsync(fd)
        except Exception:
            pass

        def _release():
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                if os.path.exists(lock):
                    os.remove(lock)
            except OSError:
                pass

        atexit.register(_release)
        return fd
    return None


_lock = _acquire_lock()
if _lock is None:
    sys.stderr.write(
        '另一个抓取进程正在运行（cache-dir=%s 的锁已被占用），本实例自动退出。\n'
        % (_arg_value('--cache-dir') or 'full_cache'))
    sys.exit(0)

from archwiki_crawler import cli

if __name__ == '__main__':
    sys.exit(cli.main())
