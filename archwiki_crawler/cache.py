"""本地缓存与断点续爬索引。

- cache/<page_key>.html  清洗后的正文（供 PDF 生成与断点复用）
- cache/images/<key>.<ext> 下载的图片
- cache/index.json       页面元数据 + 层级关系（parent/children）
"""
import os
import json
import time
import logging

logger = logging.getLogger('archwiki')


class CacheManager:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.html_dir = os.path.join(cache_dir, 'html')
        self.img_dir = os.path.join(cache_dir, 'images')
        self.index_path = os.path.join(cache_dir, 'index.json')
        os.makedirs(self.html_dir, exist_ok=True)
        os.makedirs(self.img_dir, exist_ok=True)
        self.index = self._load_index()

    def _load_index(self):
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning('索引读取失败，重建: %s', e)
        return {'pages': {}}

    def save_index(self):
        tmp = self.index_path + '.tmp'
        last_err = None
        for attempt in range(1, 11):
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self.index, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.index_path)
                return
            except (PermissionError, OSError) as e:
                # Windows 上实时杀毒/索引服务可能短暂锁定 index.json，
                # os.replace 会偶发报 WinError 32，重试退避即可，避免崩溃。
                last_err = e
                logger.debug('save_index 第 %d 次重试: %s', attempt, e)
                time.sleep(0.15 * attempt)
        logger.warning('save_index 失败，索引未落盘（内存仍为最新）: %s | %s',
                       self.index_path, last_err)

    # ---- 页面 ----
    def html_path(self, key):
        return os.path.join(self.html_dir, key + '.html')

    def has_page(self, key):
        return key in self.index.get('pages', {}) and \
            self.index['pages'][key].get('status') == 'fetched' and \
            os.path.exists(self.html_path(key))

    def save_page(self, key, content_html, meta):
        with open(self.html_path(key), 'w', encoding='utf-8') as f:
            f.write(content_html)
        pages = self.index.setdefault('pages', {})
        node = pages.get(key, {})
        node.update(meta)
        node['status'] = 'fetched'
        node['cache'] = self.html_path(key)
        pages[key] = node
        self.save_index()

    def mark_failed(self, key, meta):
        pages = self.index.setdefault('pages', {})
        node = pages.get(key, {})
        node.update(meta)
        node['status'] = 'failed'
        pages[key] = node
        self.save_index()

    def get_node(self, key):
        return self.index.get('pages', {}).get(key)

    def all_pages(self):
        return self.index.get('pages', {})

    # ---- 图片 ----
    def image_path(self, key, ext):
        ext = (ext or 'png').lower().lstrip('.')
        if ext not in ('png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'):
            ext = 'png'
        return os.path.join(self.img_dir, key + '.' + ext)

    def has_image(self, key, ext):
        return os.path.exists(self.image_path(key, ext))
