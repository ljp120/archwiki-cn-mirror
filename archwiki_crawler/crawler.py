"""爬虫核心：robots.txt 合规、请求节流、失败重试、BFS 递归抓取、
去重、层级记录与断点续爬。"""
import os
import time
import logging
import hashlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from . import utils
from . import extractor
from . import cache as cache_mod

logger = logging.getLogger('archwiki')


class RobotPool:
    """robots.txt 合规检查；若声明 Crawl-delay 则自动取较大值。"""

    def __init__(self, host, scheme, user_agent, default_delay, timeout=20):
        self.host = host
        self.delay = default_delay
        self.ua = user_agent
        self.rp = RobotFileParser()
        self.rp.set_url('%s://%s/robots.txt' % (scheme, host))
        last_err = None
        for attempt in range(3):
            try:
                r = requests.get(self.rp.url, timeout=timeout,
                                headers={'User-Agent': user_agent})
                self.rp.parse(r.text.splitlines())
                self._parse_crawl_delay(r.text)
                logger.info('已加载 robots.txt: %s', self.rp.url)
                return
            except Exception as e:
                last_err = e
                time.sleep(min(2 ** attempt, 8))
        logger.warning('robots.txt 获取失败，默认允许全部: %s', last_err)
        self.rp = None

    def _parse_crawl_delay(self, text):
        cur = None
        best = None
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith('user-agent:'):
                cur = line.split(':', 1)[1].strip()
                continue
            if line.lower().startswith('crawl-delay:'):
                try:
                    cd = float(line.split(':', 1)[1].strip())
                except ValueError:
                    continue
                if cur == '*' or cur.lower() == self.ua.lower():
                    best = cd
        if best and best > self.delay:
            logger.info('robots.txt 要求 Crawl-delay=%.1fs，已采用', best)
            self.delay = min(best, 30.0)

    def can_fetch(self, url):
        if self.rp is None:
            return True
        return self.rp.can_fetch(self.ua, url)


class Crawler:
    def __init__(self, start_url, out_dir, cache_dir, max_depth=2, concurrency=1,
                 delay=1.0, retries=3, timeout=20, user_agent=None,
                 resume=True, lang_filter=True, max_pages=0, fetch_images=True,
                 allowed_host=None, include_prefixes=None, require_suffix=None):
        self.start_url = start_url
        self.out_dir = out_dir
        self.max_depth = max_depth
        self.concurrency = max(concurrency, 1)
        self.retries = retries
        self.timeout = timeout
        self.resume = resume
        self.lang_filter = lang_filter
        self.max_pages = max_pages
        self.fetch_images = fetch_images
        # 范围约束（可选）：仅抓取标题以某前缀开头、且/或以某后缀结尾的页面
        self.include_prefixes = tuple(include_prefixes or ())
        self.require_suffix = require_suffix

        p = urlparse(start_url)
        self.scheme = p.scheme or 'https'
        self.host = allowed_host or p.netloc
        self.ua = user_agent or ('ArchWikiCrawler/1.0 (+https://wiki.archlinux.org; respectful bot)')

        self.cache = cache_mod.CacheManager(cache_dir)
        self.robots = RobotPool(self.host, self.scheme, self.ua, delay, self.timeout)
        self.delay = self.robots.delay
        self._ensure_clearance()  # 预解反爬挑战（如有），获取会话级放行 cookie

        self.session = requests.Session()
        self.session.headers.update({'User-Agent': self.ua,
                                     'Accept-Language': 'zh-CN,zh;q=0.9'})
        self._lock = __import__('threading').Lock()
        self._last_req = 0.0

        self.stats = {'total': 0, 'categories': 0, 'articles': 0, 'depth_max': 0,
                      'bytes': 0, 'failed': 0, 'robots_skipped': 0, 'cached_reused': 0,
                      'images': 0}

    # ---------- 网络 ----------
    def _request(self, url, is_image=False):
        last_err = None
        for attempt in range(self.retries + 1):
            with self._lock:
                now = time.time()
                wait = self.delay - (now - self._last_req)
                if wait > 0:
                    time.sleep(wait)
                self._last_req = time.time()
            try:
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code == 429:
                    ra = r.headers.get('Retry-After')
                    try:
                        sleep_t = float(ra) if ra and ra.isdigit() else 5.0
                    except ValueError:
                        sleep_t = 5.0
                    logger.warning('429 限流，等待 %.0fs: %s', sleep_t, url)
                    time.sleep(sleep_t)
                    continue
                if r.status_code >= 500:
                    last_err = 'HTTP %d' % r.status_code
                    time.sleep(min(2 ** attempt, 8))
                    continue
                if r.status_code == 200:
                    # 反爬挑战检测（如 archlinuxcn.org 的 /__challenge 安全检查页）
                    if self._is_challenge(r):
                        if self._solve_challenge(r.url):
                            r2 = self.session.get(url, timeout=self.timeout)
                            if r2.status_code == 200 and not self._is_challenge(r2):
                                return r2
                        last_err = 'WAF challenge unresolved'
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    return r
                return r
            except Exception as e:
                last_err = repr(e)
                time.sleep(min(2 ** attempt, 8))
        logger.error('请求失败（放弃）: %s | %s', url, last_err)
        return None

    def _is_challenge(self, r):
        """判断响应是否为反爬挑战页。"""
        if r is None:
            return False
        if '/__challenge' in (r.url or ''):
            return True
        return '安全检查' in (r.text or '')[:4000]

    def _solve_challenge(self, challenge_url):
        """POST /__verify（带 Referer=挑战页 URL）获取 __v 放行 cookie。
        成功后该 cookie 在会话内通吃全站；WAF 偶发不下发时重试。"""
        base = '%s://%s/__verify' % (self.scheme, self.host)
        for _ in range(self.retries + 1):
            try:
                vr = self.session.post(base, data=b'',
                                       headers={'Referer': challenge_url},
                                       timeout=self.timeout)
                if vr.status_code == 200 and list(self.session.cookies):
                    return True
            except Exception:
                pass
            time.sleep(1)
        return bool(list(self.session.cookies))

    def _ensure_clearance(self):
        """预解挑战：访问首页，若被挡则 POST /__verify 获取 __v cookie。
        仅当检测到挑战时触发，对无 WAF 的站点（gentoo/archlinux.org）无副作用。"""
        probe = '%s://%s/wiki/Main_page' % (self.scheme, self.host)
        try:
            r = self.session.get(probe, timeout=self.timeout, allow_redirects=True)
        except Exception:
            return
        if r is not None and self._is_challenge(r):
            self._solve_challenge(r.url)

    def _download_image(self, img):
        url = img['url']
        ext = os.path.splitext(urlparse(url).path)[1].lstrip('.') or 'png'
        key = 'img' + hashlib.sha1(url.encode()).hexdigest()[:12]
        if self.cache.has_image(key, ext):
            self.stats['images'] += 1
            return self.cache.image_path(key, ext)
        r = self._request(url, is_image=True)
        if r is None or r.status_code != 200:
            return None
        path = self.cache.image_path(key, ext)
        try:
            with open(path, 'wb') as f:
                f.write(r.content)
            self.stats['images'] += 1
            return path
        except Exception:
            return None

    # ---------- 过滤 ----------
    def _allowed(self, url):
        if not url:
            return False
        pu = urlparse(url)
        if pu.netloc.lower() != self.host.lower():
            return False
        title = utils.title_from_url(url)
        ns = utils.namespace_of(title)
        if ns in utils.SKIP_NAMESPACES:
            return False
        if self.include_prefixes and not any(title.startswith(p) for p in self.include_prefixes):
            return False
        if self.require_suffix and not title.endswith(self.require_suffix):
            return False
        if self.lang_filter and not utils.has_cjk(title):
            return False
        return True

    # ---------- 单页抓取任务 ----------
    def _fetch_task(self, url, depth, parent):
        key = utils.page_key(url)
        if not self.robots.can_fetch(url):
            logger.info('robots 禁止，跳过: %s', url)
            self.stats['robots_skipped'] += 1
            return {'key': key, 'url': url, 'new_links': [], 'skipped': True,
                    'title': utils.title_from_url(url), 'parent': parent, 'depth': depth}
        r = self._request(url)
        if r is None or r.status_code != 200:
            self.cache.mark_failed(key, {'url': url, 'title': utils.title_from_url(url),
                                          'depth': depth, 'parent': parent})
            self.stats['failed'] += 1
            return {'key': key, 'url': url, 'new_links': [], 'failed': True,
                    'title': utils.title_from_url(url), 'parent': parent, 'depth': depth}
        res = extractor.extract(r.text, url, self.host,
                                fetch_images=self.fetch_images,
                                image_dir=self.cache.img_dir)
        # 下载图片并改写本地路径
        if self.fetch_images and res['images']:
            soup = BeautifulSoup(res['content_html'], 'lxml')
            for img in soup.find_all('img'):
                local = self._download_image({'url': img['src'], 'alt': img.get('alt', '')})
                if local:
                    img['src'] = local
            res['content_html'] = str(soup)
        node = {
            'url': url, 'title': res['title'], 'depth': depth, 'parent': parent,
            'type': 'category' if utils.is_category(res['title']) else 'article',
            'categories': res['categories'], 'internal_links': res['internal_links'],
            'children': [],
        }
        self.cache.save_page(key, res['content_html'], node)
        self.stats['bytes'] += len(res['content_html'].encode('utf-8'))
        if node['type'] == 'category':
            self.stats['categories'] += 1
        else:
            self.stats['articles'] += 1
        self.stats['depth_max'] = max(self.stats['depth_max'], depth)
        self.stats['total'] += 1
        return {'key': key, 'url': url, 'new_links': res['internal_links'],
                'title': res['title'], 'parent': parent, 'depth': depth}

    # ---------- 编排 ----------
    def run(self):
        start_key = utils.page_key(self.start_url)
        discovered = {start_key: (0, None)}
        queue = deque([(self.start_url, 0, None)])
        futures = {}
        ex = ThreadPoolExecutor(max_workers=self.concurrency)
        t0 = time.time()
        pages = {}  # key -> node (from index)

        def enqueue_children(links, depth, parent_key):
            for u in links:
                if depth + 1 > self.max_depth:
                    continue
                if not self._allowed(u):
                    continue
                ckey = utils.page_key(u)
                if ckey in discovered:
                    # 已发现：仍需保证父子关系记录
                    child = self.cache.get_node(ckey)
                    if child:
                        child.setdefault('children', [])
                        if parent_key not in child['children']:
                            child['children'].append(parent_key)
                    continue
                discovered[ckey] = (depth + 1, parent_key)
                queue.append((u, depth + 1, parent_key))

        def record_parent(parent_key, child_key):
            if parent_key:
                pn = self.cache.get_node(parent_key)
                if pn is not None:
                    pn.setdefault('children', [])
                    if child_key not in pn['children']:
                        pn['children'].append(child_key)

        try:
            while queue or futures:
                # 提交新任务（受并发上限约束）
                while queue and len(futures) < self.concurrency:
                    if self.max_pages and self.stats['total'] >= self.max_pages:
                        queue.clear()
                        break
                    url, depth, parent = queue.popleft()
                    key = utils.page_key(url)
                    if key in pages or self.cache.get_node(key) and self.cache.get_node(key).get('status') == 'fetched' and self.resume:
                        # 断点续爬：复用缓存，同步处理
                        node = self.cache.get_node(key)
                        if node and node.get('status') == 'fetched':
                            self.stats['cached_reused'] += 1
                            try:
                                self.stats['bytes'] += os.path.getsize(self.cache.html_path(key))
                            except Exception:
                                pass
                            if node.get('type') == 'category':
                                self.stats['categories'] += 1
                            else:
                                self.stats['articles'] += 1
                            self.stats['total'] += 1
                            self.stats['depth_max'] = max(self.stats['depth_max'], node.get('depth', 0))
                            record_parent(parent, key)
                            enqueue_children(node.get('internal_links', []), depth, key)
                            pages[key] = node
                            continue
                    if key in pages:
                        continue
                    fut = ex.submit(self._fetch_task, url, depth, parent)
                    futures[fut] = key
                if futures:
                    done, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED, timeout=120)
                    for fut in done:
                        key = futures.pop(fut)
                        try:
                            res = fut.result()
                        except Exception as e:
                            logger.error('任务异常: %s', e)
                            res = {'key': key, 'new_links': [], 'parent': None, 'depth': 0}
                        record_parent(res.get('parent'), key)
                        if not res.get('skipped') and not res.get('failed'):
                            node = self.cache.get_node(key)
                            pages[key] = node
                            enqueue_children(res.get('new_links', []), res.get('depth', 0), key)
                        elif res.get('failed'):
                            pass
                else:
                    # 仅缓存复用处理完成
                    pass
        finally:
            ex.shutdown(wait=True)
            self.cache.save_index()

        self.stats['duration'] = time.time() - t0
        ordered = self._order_pages(pages)
        return ordered, self.stats

    def _order_pages(self, pages):
        """按层级 DFS 排序：根页面 -> 子分类/子条目。"""
        children_of = {}
        roots = []
        for key, node in pages.items():
            parent = node.get('parent')
            if not parent or parent not in pages:
                roots.append(key)
            children_of.setdefault(parent, []).append(key)
        ordered = []

        def dfs(key, depth):
            node = pages.get(key)
            if not node:
                return
            ordered.append({'page_key': key, 'title': node.get('title', ''),
                            'depth': node.get('depth', depth),
                            'html_path': self.cache.html_path(key),
                            'type': node.get('type', 'article')})
            for ch in children_of.get(key, []):
                if ch in pages:
                    dfs(ch, depth + 1)

        # 根按深度排序，深度相同的按标题
        for key in sorted(roots, key=lambda k: (pages[k].get('depth', 0), pages[k].get('title', ''))):
            dfs(key, pages[key].get('depth', 0))
        return ordered
