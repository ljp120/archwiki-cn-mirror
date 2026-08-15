"""内容提取：从 MediaWiki 渲染页中抽取正文，清除导航/编辑链接等无关元素，
保留标题层级、代码块、表格、图片、列表；把站内链接改写为 PDF 内部锚点，
站外链接保留为可点击超链接。"""
import re
import os
import logging
from bs4 import BeautifulSoup

from . import utils

logger = logging.getLogger('archwiki')

# 需移除的页面装饰/导航元素（CSS 选择器）
_REMOVE_SELECTORS = [
    '.mw-editsection',          # 编辑小节链接 [编辑]
    '#toc', '.toc', '.toccolours',  # 页内自动目录（我们生成全局目录）
    '.navbox', '.navbar',       # 底部导航框
    '.catlinks',                # 底部分类链接（分类单独抽取）
    '.mw-indicators', '.mw-empty-elt',
    '.printfooter', '.mw-jump-link', '#jump-to-nav',
    '#siteSub', '#contentSub', '.visualClear',
    '.noprint', '.mw-navigation', '.navigation',
    '.mw-hidden-catlinks', '.mw-redirectedfrom',
    'script', 'style', 'noscript', 'head',
    '.reflist',                 # 参考文献列表（保留文本价值低，移除减少噪声）
    '.mw-parser-output .mw-references-wrap',
]

# 行内代码/链接等需要保留的标签之外的"透明"容器
_TRANSPARENT = {'div', 'section', 'span', 'center', 'figure', 'article', 'main'}


def _abs_url(base_host, src):
    if not src:
        return None
    if src.startswith('//'):
        return 'https:' + src
    if src.startswith('http'):
        return src
    if src.startswith('/'):
        return 'https://' + base_host + src
    return None


def extract(raw_html, url, host, fetch_images=True, image_dir=None):
    """解析单页 HTML，返回结构化结果。

    返回 dict:
      title          条目标题
      content_html   清洗后的正文 HTML（链接已改写为锚点，图片为本地/绝对路径）
      images         [{url, alt}] 待下载图片列表
      internal_links [规范化 URL] 站内链接（用于层级发现，已去重）
      categories     [分类标题]
      page_key       本页唯一键
    """
    soup = BeautifulSoup(raw_html, 'lxml')
    key = utils.page_key(url)
    title = utils.title_from_url(url)

    # 优先取页面标题（#firstHeading），否则用 URL 反推
    fh = soup.find(id='firstHeading')
    if fh and fh.get_text(strip=True):
        title = fh.get_text(strip=True)

    # 正文容器：MediaWiki 标准容器
    content = (soup.find(id='mw-content-text')
               or soup.find('article')
               or soup.find(id='content')
               or soup.body)
    if content is None:
        logger.warning('未找到正文容器: %s', url)
        content = soup

    # 抽取分类（独立保留，正文中移除）
    categories = []
    for a in soup.select('#catlinks a, .catlinks a'):
        t = a.get_text(strip=True)
        if t and t not in ('Catégorie', '分类', 'Kategorie', 'Category'):
            categories.append(t)

    # 移除无关元素
    for sel in _REMOVE_SELECTORS:
        for el in content.select(sel):
            el.decompose()

    # 收集本节锚点（用于页内链接改写）
    sec_bm = {}
    for h in content.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        span = h.find(class_='mw-headline') or h.find(id=True)
        secid = (span.get('id') if span and span.get('id') else None)
        if not secid:
            secid = re.sub(r'[^\w一-鿿]', '_', h.get_text(strip=True))[:40] or 's'
        bm = '%s_%s' % (key, secid)
        h['data-bm'] = bm
        sec_bm['#' + secid] = bm

    # 链接改写：站内 -> #锚点，站外 -> 绝对 URL
    internal_links = set()
    for a in content.find_all('a'):
        href = a.get('href')
        if not href:
            a.decompose() if not a.get_text(strip=True) else a.unwrap()
            continue
        if href.startswith('#'):
            target = sec_bm.get(href)
            if target:
                a['href'] = '#' + target
            else:
                # 找不到目标锚点：保留为普通文本
                a.unwrap()
            continue
        norm = utils.canonical_url(href, host)
        if norm:
            internal_links.add(norm)
            a['href'] = '#' + utils.page_key(norm)
        else:
            # 站外链接：确保绝对路径，保留可点击
            absu = _abs_url(host, href)
            if absu:
                a['href'] = absu
            else:
                a.unwrap()
        # 清理无关属性
        for attr in ('class', 'rel', 'title', 'id', 'accesskey', 'data-*'):
            if attr == 'data-*':
                for k in list(a.attrs):
                    if k.startswith('data-'):
                        del a[k]
            elif attr in a.attrs:
                del a[attr]

    # 图片：收集待下载列表，src 改写为绝对 URL（下载后由调用方重写为本地路径）
    images = []
    for img in content.find_all('img'):
        src = img.get('src') or img.get('data-src')
        absu = _abs_url(host, src)
        if absu:
            img['src'] = absu
            alt = img.get('alt', '') or ''
            images.append({'url': absu, 'alt': alt})
        else:
            img.decompose()

    # 在正文顶部插入标题 H1（带本页锚点）
    h1 = soup.new_tag('h1')
    h1['data-bm'] = key
    h1.string = title
    content.insert(0, h1)

    content_html = str(content)
    return {
        'title': title,
        'content_html': content_html,
        'images': images,
        'internal_links': sorted(internal_links),
        'categories': categories,
        'page_key': key,
    }
