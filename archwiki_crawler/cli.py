"""命令行入口：参数解析、爬取编排、日志与统计输出。"""
import os
import sys
import argparse
import logging

from . import utils
from . import crawler as crawler_mod
from . import pdf_builder


def human_bytes(n):
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return '%.1f %s' % (n, u)
        n /= 1024
    return '%.1f TB' % n


def build_arg_parser():
    p = argparse.ArgumentParser(
        prog='archwiki-crawler',
        description='抓取 Arch Linux 简体中文维基并生成带目录的 PDF 电子书。')
    p.add_argument('--start', default='https://wiki.archlinuxcn.org/wiki/Main_page',
                   help='起始页面 URL（默认：中文维基首页）。')
    p.add_argument('--allowed-host', default=None,
                   help='允许抓取的站点主机（默认取 --start 的主机）。')
    p.add_argument('-o', '--output', default='archwiki.pdf',
                   help='输出 PDF 路径（默认 ./archwiki.pdf）。')
    p.add_argument('-d', '--max-depth', type=int, default=2,
                   help='最大递归深度（默认 2；-1 表示不限）。')
    p.add_argument('-c', '--concurrency', type=int, default=1,
                   help='并发请求数（默认 1，保持礼貌）。')
    p.add_argument('--delay', type=float, default=1.0,
                   help='请求间隔秒数（默认 1.0，robots.txt 的 Crawl-delay 会取较大值）。')
    p.add_argument('--retries', type=int, default=3,
                   help='单页失败重试次数（默认 3）。')
    p.add_argument('--timeout', type=int, default=20,
                   help='单请求超时秒数（默认 20）。')
    p.add_argument('--user-agent', default=None, help='自定义 User-Agent。')
    p.add_argument('--cache-dir', default='cache', help='本地缓存目录（默认 ./cache）。')
    p.add_argument('--no-resume', action='store_true',
                   help='禁用断点续爬（从头重新抓取）。')
    p.add_argument('--no-lang-filter', action='store_true',
                   help='不过滤语言（默认仅抓取含中文的条目）。')
    p.add_argument('--include-prefix', action='append', default=[],
                   help='仅抓取标题以此前缀开头的页面（可多次指定）；用于限定命名空间。')
    p.add_argument('--require-suffix', default=None,
                   help='仅抓取标题以此结尾的页面（如 /zh-cn 限定中文版本）。')
    p.add_argument('--pdf-title', default=None,
                   help='PDF 书脊/封面标题（默认按站点生成）。')
    p.add_argument('--max-pages', type=int, default=0,
                   help='最大抓取页数（0 表示不限）。')
    p.add_argument('--no-images', action='store_true', help='不下载/嵌入图片。')
    p.add_argument('--no-pdf', action='store_true', help='只爬取不生成 PDF。')
    p.add_argument('--log', default='crawl.log', help='日志文件路径（默认 ./crawl.log）。')
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    logger = utils.setup_logger(args.log)
    logger.info('=== ArchWiki 爬虫启动 ===')
    logger.info('起始页: %s | 最大深度: %s | 并发: %d | 间隔: %.1fs',
                args.start, args.max_depth, args.concurrency, args.delay)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or '.', exist_ok=True)

    c = crawler_mod.Crawler(
        start_url=args.start,
        out_dir=os.path.dirname(os.path.abspath(args.output)),
        cache_dir=args.cache_dir,
        max_depth=args.max_depth if args.max_depth >= 0 else 10**9,
        concurrency=args.concurrency,
        delay=args.delay,
        retries=args.retries,
        timeout=args.timeout,
        user_agent=args.user_agent,
        resume=not args.no_resume,
        lang_filter=not args.no_lang_filter,
        max_pages=args.max_pages,
        fetch_images=not args.no_images,
        allowed_host=args.allowed_host,
        include_prefixes=args.include_prefix or None,
        require_suffix=args.require_suffix,
    )

    try:
        ordered, stats = c.run()
    except KeyboardInterrupt:
        logger.warning('用户中断，已抓取内容保留在缓存中，可加 --no-resume 关闭后用断点续爬。')
        return 1

    stats['bytes_human'] = human_bytes(stats['bytes'])
    s = stats
    logger.info('=== 爬取完成 ===')
    logger.info('总页数=%d (分类=%d, 文章=%d) | 复用缓存=%d | 失败=%d | robots跳过=%d',
                s['total'], s['categories'], s['articles'], s['cached_reused'],
                s['failed'], s['robots_skipped'])
    logger.info('最大深度=%d | 图片=%d | 缓存大小=%s | 耗时=%.1fs',
                s['depth_max'], s['images'], s['bytes_human'], s['duration'])

    if args.no_pdf or not ordered:
        if not ordered:
            logger.warning('未抓取到任何页面，跳过 PDF 生成。')
        print('\n爬取统计：')
        for k, v in s.items():
            print('  %-16s %s' % (k, v))
        return 0

    logger.info('开始生成 PDF（%d 页）...', len(ordered))
    title = args.pdf_title or 'Arch Linux 简体中文维基'
    # 链接解析：已抓取页面键 -> 内部锚点；其余 -> 外部 URL
    crawled_keys = {pg['page_key'] for pg in ordered}
    url_map = {}
    for _k, node in c.cache.all_pages().items():
        for u in node.get('internal_links', []):
            url_map[utils.page_key(u)] = u
    pdf_builder.build_pdf(ordered, args.output, stats, title=title,
                          crawled_keys=crawled_keys, url_map=url_map)
    size = os.path.getsize(args.output) if os.path.exists(args.output) else 0
    logger.info('PDF 已生成: %s (%.2f MB)', args.output, size / 1024 / 1024)
    print('\n爬取统计：')
    for k, v in s.items():
        print('  %-16s %s' % (k, v))
    print('\n输出文件: %s (%.2f MB)' % (args.output, size / 1024 / 1024))
    return 0


if __name__ == '__main__':
    sys.exit(main())
