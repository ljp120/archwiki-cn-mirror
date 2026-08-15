"""将清洗后的正文 HTML 转换为 reportlab flowables。

- 标题层级 -> 带书签(anchor)的 Paragraph；书签键读取提取阶段写入的 data-bm。
- 站内链接(href="#bm") -> PDF 内部锚点；站外链接 -> 可点击 URI。
- 代码块、表格、图片、列表、引用均保留格式。
"""
import os
import logging
import xml.sax.saxutils as su
from bs4 import BeautifulSoup, NavigableString

from reportlab.platypus import (Paragraph, Spacer, XPreformatted, Table, TableStyle,
                                ListFlowable, ListItem, Image, HRFlowable, Flowable)
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics

from . import styles as _styles

logger = logging.getLogger('archwiki')

# 链接解析上下文：构建 PDF 时由调用方注入。
# keys = 已抓取的页面键集合（存在对应书签 -> 内部跳转）；
# urls = 所有发现的内部链接键 -> 绝对 URL（目标未抓取 -> 作为外部可点击链接）。
_LINK_CTX = {'keys': set(), 'urls': {}}

_HEADING = {'h1': 1, 'h2': 2, 'h3': 3, 'h4': 4, 'h5': 5, 'h6': 6}

# 代码块字体：Courier 仅覆盖 ASCII；中文注释会缺字成黑方块，
# 因此非 ASCII 字符回退到 STSong-Light。
_CODEFONT = 'Courier'
_CJKFONT = _styles.FONT


def _is_code_ascii(ch):
    return ord(ch) < 128


def _code_xml(text):
    """转义代码文本，并将非 ASCII 字符用中文字体包裹，避免 tofu 黑方块。"""
    parts = []
    cur_ascii = None
    buf = []

    def flush():
        if not buf:
            return
        s = ''.join(buf)
        if cur_ascii:
            parts.append(_esc(s))
        else:
            parts.append('<font name="%s">%s</font>' % (_CJKFONT, _esc(s)))
        buf.clear()

    for ch in text:
        is_ascii = _is_code_ascii(ch)
        if is_ascii != cur_ascii and buf:
            flush()
        cur_ascii = is_ascii
        buf.append(ch)
    flush()
    return ''.join(parts)


def _code_char_width(ch, size):
    """估算代码字符在 PDF 中的宽度（ASCII 用 Courier，中文用 STSong）。"""
    font = _CODEFONT if _is_code_ascii(ch) else _CJKFONT
    try:
        return pdfmetrics.stringWidth(ch, font, size)
    except Exception:
        return size * (0.6 if font == _CODEFONT else 1.0)


def _wrap_pre_text(txt, frame_width, size=8.5):
    """对 <pre> 代码块手动折行，防止长行超出正文框。"""
    if not txt:
        return txt
    # 正文框减去代码样式左右缩进/内边距后的可用宽度
    max_w = max(frame_width - 20, size * 10)
    indent = '  '
    indent_w = sum(_code_char_width(c, size) for c in indent)
    out_lines = []
    for raw_line in txt.splitlines():
        if not raw_line:
            out_lines.append('')
            continue
        line_w = 0.0
        buf = []
        last_space_idx = -1
        for ch in raw_line:
            cw = _code_char_width(ch, size)
            if line_w + cw > max_w and buf:
                # 优先在最近的空格处折行，避免把单词/字符串拦腰截断
                if 0 < last_space_idx < len(buf):
                    next_chars = buf[last_space_idx + 1:]
                    out_lines.append(''.join(buf[:last_space_idx]))
                    buf = [indent] + next_chars
                    line_w = indent_w + sum(_code_char_width(c, size) for c in next_chars)
                    last_space_idx = -1
                else:
                    out_lines.append(''.join(buf))
                    buf = [indent]
                    line_w = indent_w
            if ch == ' ':
                last_space_idx = len(buf)
            buf.append(ch)
            line_w += cw
        if buf:
            out_lines.append(''.join(buf))
    return '\n'.join(out_lines)
_HEADER_BG = colors.HexColor('#234e70')
_GRID = colors.HexColor('#bbbbbb')

# 页面正文框高度（A4 - 上下边距），用于判断表格是否需要缩放兜底。
_FRAME_HEIGHT = 841.89 - 72.0 - 57.6  # A4 高 - TOP(1in) - BOTTOM(0.8in)


class ScaledTable(Flowable):
    """兜底：当表格存在超过整页框高度的行（reportlab 无法拆分单行）时，
    将整张表作为矢量绘制并按比例缩放到适配正文框，绝不触发 LayoutError。
    矢量绘制，PDF 阅读器放大后仍清晰，内容不丢失。
    """

    def __init__(self, table, frame_width, frame_height=_FRAME_HEIGHT):
        super().__init__()
        self.table = table
        self.fw = frame_width
        self.fh = frame_height
        try:
            nw, nh = table.wrap(frame_width, 10 ** 9)
        except Exception:
            nw, nh = frame_width, frame_height
        if not nw or not nh:
            nw, nh = frame_width, frame_height
        self.nat_w, self.nat_h = nw, nh
        scale = min(frame_width / nw, frame_height / nh, 1.0)
        if scale <= 0:
            scale = 1.0
        self.scale = scale

    def wrap(self, aw, ah):
        w = self.fw if self.fw and self.fw > 0 else aw
        h = self.nat_h * self.scale
        if h <= 0:
            h = self.fh
        # 留 1pt 余量，避免浮点边界被判 too large
        w = min(w, aw) if aw and aw > 0 else w
        h = min(h, ah) if ah and ah > 0 else h
        self.width = w
        self.height = h
        return w, h

    def draw(self):
        c = self.canv
        c.saveState()
        c.scale(self.scale, self.scale)
        self.table.drawOn(c, 0, 0)
        c.restoreState()


def _esc(s):
    return su.escape(s)


def _emit_heading(h, st, page_depth):
    lvl = _HEADING.get(h.name, 4)
    bm = h.get('data-bm')
    inner = _inline_xml(h)
    if not inner.strip():
        return []
    p = Paragraph(inner, st)
    p._bm = bm
    # 书签层级仅由标题级别决定（h1->0, h2->1, ...），不再叠加文章树深度，
    # 避免维基链接图深度把整本书签压成两级、绝大部分文章被埋进深层嵌套。
    p._outline_base = 0
    p._outline_off = lvl - 1
    return [p]


def _inline_xml(el):
    """将行内内容转为 reportlab 可解析的 XML 字符串（转义文本、保留少量标签）。"""
    if isinstance(el, NavigableString):
        return _esc(str(el))
    tag = el.name
    if tag in ('b', 'strong'):
        return '<b>' + ''.join(_inline_xml(c) for c in el.children) + '</b>'
    if tag in ('i', 'em'):
        return '<i>' + ''.join(_inline_xml(c) for c in el.children) + '</i>'
    if tag == 'code':
        # 行内代码同样对中文做字体回退，避免黑方块
        return '<font name="Courier">' + _code_xml(el.get_text()) + '</font>'
    if tag == 'br':
        return '<br/>'
    if tag in ('sub', 'sup'):
        return '<%s>%s</%s>' % (tag, ''.join(_inline_xml(c) for c in el.children), tag)
    if tag == 'a':
        href = el.get('href') or '#'
        txt = ''.join(_inline_xml(c) for c in el.children)
        if not txt.strip():
            return ''
        color = '#0645ad'
        # 站内锚点：目标已抓取 -> 内部跳转；否则回退为外部可点击链接
        if href.startswith('#'):
            key = href[1:]
            if key in _LINK_CTX['keys']:
                pass  # 保持 #key 内部跳转
            elif key in _LINK_CTX['urls']:
                href = _LINK_CTX['urls'][key]  # 转外部 URL
            else:
                return txt  # 无目标，退化为纯文本
        return '<a href="%s" color="%s">%s</a>' % (_esc(href), color, txt)
    # 透明容器：递归
    return ''.join(_inline_xml(c) for c in el.children)


def _emit_paragraph(p_el, st):
    inner = _inline_xml(p_el)
    if not inner.strip():
        return []
    return [Paragraph(inner, st)]


def _emit_pre(el, st, frame_width):
    txt = el.get_text('\n')
    if not txt.strip():
        return []
    # 1) 长行按正文框宽度折行，避免代码超出页面边界
    wrapped = _wrap_pre_text(txt, frame_width, st.fontSize)
    # 2) wiki 代码块常含字面 < > &（如 <para>、2>&1、#include <stdio.h>），
    #    必须 XML 转义后再交给 XPreformatted；同时中文注释用中文字体回退，
    #    避免 Courier 缺字显示为黑方块。
    safe = _code_xml(wrapped)
    try:
        return [XPreformatted(safe, st)]
    except Exception:
        # 兜底：极端情况下仍有解析异常，退化为纯文本（尖括号已安全显示）
        return [XPreformatted(_esc(wrapped), st)]


def _emit_list(el, st, frame_width):
    items = []
    for li in el.find_all('li', recursive=False):
        inner = _inline_xml(li)
        if inner.strip():
            items.append(ListItem(Paragraph(inner, st), value=None))
    if not items:
        # 处理无 <li> 直接嵌套的情况
        inner = _inline_xml(el)
        if inner.strip():
            items.append(ListItem(Paragraph(inner, st)))
    if not items:
        return []
    bullet = '1' if el.name == 'ol' else 'bullet'
    return [ListFlowable(items, bulletType=bullet, leftIndent=18, bulletFontSize=9)]


def _emit_table(el, st, st_h, frame_width):
    rows = el.find_all('tr')
    if not rows:
        return []
    data = []
    ncol = 0
    for tr in rows:
        cells = tr.find_all(['td', 'th'])
        ncol = max(ncol, len(cells))
        row = []
        for c in cells:
            inner = _inline_xml(c)
            cst = st_h if c.name == 'th' else st
            row.append(Paragraph(inner if inner.strip() else ' ', cst))
        data.append(row)
    if not data:
        return []
    for r in data:
        while len(r) < ncol:
            r.append(Paragraph(' ', st))
    col_w = frame_width / ncol
    t = Table(data, colWidths=[col_w] * ncol, repeatRows=1)
    ts = [
        ('GRID', (0, 0), (-1, -1), 0.5, _GRID),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('BACKGROUND', (0, 0), (-1, 0), _HEADER_BG),
    ]
    t.setStyle(TableStyle(ts))
    # 兜底：reportlab 无法拆分「单行超过正文框高度」的表格（如 CUPS 页
    # 2844pt 的巨行），build 时会抛 LayoutError。先 wrap 拿到各行高度，
    # 若存在超框行，整表转矢量缩放（ScaledTable）兜底，绝不报错。
    try:
        t.wrap(frame_width, 10 ** 9)
        rh = getattr(t, '_rowHeights', None)
        if rh and max(rh) > _FRAME_HEIGHT:
            return [ScaledTable(t, frame_width), Spacer(1, 4)]
    except Exception as e:
        logger.debug('表格预检失败，回退缩放兜底: %s', e)
        return [ScaledTable(t, frame_width), Spacer(1, 4)]
    return [t, Spacer(1, 4)]


def _emit_image(el, frame_width):
    src = el.get('src')
    alt = el.get('alt', '') or ''
    if not src or not os.path.exists(src):
        return [Paragraph('[图片: %s]' % _esc(alt), _styles.get_styles()['body'])]
    try:
        from PIL import Image as PILImage
        im = PILImage.open(src)
        w, h = im.size
        maxw = min(frame_width, 6.0 * inch)
        ratio = min(maxw / w, 1.0)
        return [Image(src, width=w * ratio, height=h * ratio)]
    except Exception as e:
        logger.debug('图片嵌入失败 %s: %s', src, e)
        return [Paragraph('[图片: %s]' % _esc(alt), _styles.get_styles()['body'])]


def convert(content_html, page_depth=0, frame_width=6.4 * inch,
            crawled_keys=None, url_map=None):
    """将单页清洗 HTML 转为 flowables 列表。

    crawled_keys: 已抓取页面键集合（用于区分内部/外部链接）
    url_map:      内部链接键 -> 绝对 URL（目标未抓取时回退为外部链接）
    """
    _LINK_CTX['keys'] = crawled_keys or set()
    _LINK_CTX['urls'] = url_map or {}
    st = _styles.get_styles()
    sub = BeautifulSoup(content_html, 'lxml')
    root = sub.body if sub.body else sub
    out = []

    def walk(node):
        for child in node.children:
            if isinstance(child, NavigableString):
                t = str(child).strip()
                if t:
                    out.append(Paragraph(_esc(t), st['body']))
                continue
            if child.name is None:
                continue
            tag = child.name
            if tag in _HEADING:
                out.extend(_emit_heading(child, st.get(tag, st['h4']), page_depth))
            elif tag == 'p':
                out.extend(_emit_paragraph(child, st['body']))
            elif tag in ('pre',) or (tag == 'code' and child.parent.name != 'p'):
                out.extend(_emit_pre(child, st['code'], frame_width))
            elif tag in ('ul', 'ol'):
                out.extend(_emit_list(child, st['li'], frame_width))
            elif tag == 'table':
                out.extend(_emit_table(child, st['cell'], st['cellh'], frame_width))
            elif tag == 'blockquote':
                inner = _inline_xml(child)
                if inner.strip():
                    out.append(Paragraph(inner, st['quote']))
            elif tag == 'img':
                out.extend(_emit_image(child, frame_width))
            elif tag in ('hr',):
                out.append(HRFlowable(width='100%', thickness=0.5,
                                      color=colors.HexColor('#cccccc'), spaceBefore=4, spaceAfter=4))
            elif tag in ('div', 'section', 'span', 'center', 'figure', 'article', 'main'):
                walk(child)
            else:
                # 未知块级：递归其子节点
                walk(child)

    walk(root)
    return out
