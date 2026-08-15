"""工具函数：URL 规范化、中文判定、页面唯一键、日志。"""
import re
import hashlib
import logging
from urllib.parse import urlparse, urlunparse, unquote

# 中日韩统一表意文字（含扩展A）及兼容区，用于判定"中文页面"
_CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')

# 不需要抓取内容的命名空间（中文维基多用英文前缀）
SKIP_NAMESPACES = {
    'Special', 'MediaWiki', 'File', 'Image', 'Media', 'Template',
    'Help', 'Talk', 'User', 'User talk', 'Category talk', 'File talk',
    'Portal', 'Draft', 'Module', 'MediaWiki talk',
}


def has_cjk(text: str) -> bool:
    """标题中是否包含 CJK 字符（用于识别简体中文条目）。"""
    return bool(_CJK_RE.search(text or ''))


def page_key(url: str) -> str:
    """由规范 URL 生成稳定的 ASCII 锚点/缓存键（用于 PDF 书签与本地缓存）。"""
    return 'pg' + hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]


def canonical_url(href: str, host: str, scheme: str = 'https'):
    """将任意 href 规范化为站内 wiki 绝对 URL；非站内返回 None。

    返回形如 https://host/wiki/<Title> 的规范链接，作为去重与锚点映射依据。
    """
    if not href:
        return None
    href = href.strip()
    if href.startswith('#') or href.startswith('mailto:') or href.startswith('javascript:'):
        return None
    if href.startswith('//'):
        href = scheme + ':' + href
    if href.startswith('http://') or href.startswith('https://'):
        p = urlparse(href)
        if p.netloc.lower() != host.lower():
            return None  # 站外链接
        path = p.path
        if p.query.startswith('title='):
            path = '/wiki/' + p.query.split('=', 1)[1].split('&')[0]
        return urlunparse((scheme, host, path, '', '', ''))
    if href.startswith('/'):
        # 处理 /index.php?title=X
        if 'index.php' in href and 'title=' in href:
            m = re.search(r'title=([^&]+)', href)
            if m:
                return urlunparse((scheme, host, '/wiki/' + unquote(m.group(1)), '', '', ''))
            return None
        path = href.split('#')[0].split('?')[0]
        if not path:
            return None
        return urlunparse((scheme, host, path, '', '', ''))
    return None  # 相对路径（无前导 /）忽略


def title_from_url(url: str) -> str:
    """从 URL 反推 wiki 条目标题（用于显示与中文判定）。"""
    p = urlparse(url)
    seg = p.path.rstrip('/')
    m = re.search(r'/(?:wiki|title)/(.+)$', seg)
    if m:
        return unquote(m.group(1)).replace('_', ' ')
    return seg or '首页'


def namespace_of(title: str) -> str:
    """返回条目所属命名空间（Category / File / 空字符串表示主命名空间）。"""
    m = re.match(r'^([A-Za-z][A-Za-z ]*):', title)
    return m.group(1) if m else ''


def is_category(title: str) -> bool:
    return namespace_of(title).lower() == 'category'


def setup_logger(log_path):
    """返回同时输出到控制台与文件的 logger。"""
    logger = logging.getLogger('archwiki')
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S')
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    try:
        fh = logging.FileHandler(log_path, encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    return logger
