"""PDF 生成：封面 + 自动目录页（可点击）+ 多级书签(outline) + 页码。

使用 reportlab BaseDocTemplate：
- afterFlowable 为每个标题书签生成 PDF outline 条目，并按层级嵌套；
- 同时 notify TableOfContents 以生成可点击目录页；
- 通过 multiBuild 二次排版解析目录页码与内部链接。
"""
import os
import logging
import datetime
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph,
                                Spacer, PageBreak, Table, TableStyle, NextPageTemplate)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch, cm
from reportlab.lib import colors
from reportlab.pdfbase.pdfdoc import PDFName
from reportlab.pdfgen import canvas as _canvas

from . import styles as _styles
from . import html2pdf

logger = logging.getLogger('archwiki')

LEFT = 0.9 * inch
RIGHT = 0.9 * inch
TOP = 1.0 * inch
BOTTOM = 0.8 * inch
FRAME_W = A4[0] - LEFT - RIGHT


class WikiDocTemplate(BaseDocTemplate):
    def __init__(self, filename, outline_open_levels=0, **kw):
        super().__init__(filename, **kw)
        frame = Frame(LEFT, BOTTOM, FRAME_W, A4[1] - TOP - BOTTOM, id='main')
        self.addPageTemplates([
            PageTemplate(id='cover', frames=[frame], onPage=self._decorate_cover),
            PageTemplate(id='body', frames=[frame], onPage=self._decorate),
        ])
        self._toc = TableOfContents()
        self._toc.levelStyles = _styles.toc_level_styles()
        self._cur_outline = -1  # 上次写入的 outline 层级（用于逐级下降钳制）
        self.outline_open_levels = outline_open_levels

    def afterFlowable(self, flowable):
        bm = getattr(flowable, '_bm', None)
        if not bm:
            return
        text = flowable.getPlainText()
        base = getattr(flowable, '_outline_base', 0)
        off = getattr(flowable, '_outline_off', 0)
        level = base + off
        # reportlab 5.0 要求 outline 层级只能逐级增减，跨级下降会抛异常；做钳制
        if level > self._cur_outline + 1:
            level = self._cur_outline + 1
        self._cur_outline = level
        self.canv.bookmarkPage(bm)
        # 仅第一层(level==0)展开，其下所有层级(>=1)默认折叠
        closed = level >= self.outline_open_levels
        self.canv.addOutlineEntry(text, bm, level, closed=closed)
        # 仅顶层（文章标题，level==0）进入可点目录；若把所有标题都列入，
        # 目录会膨胀到上万条、自身占据数百页，失去索引意义。
        if level == 0:
            self.notify('TOCEntry', (0, text, self.page, bm))

    def _decorate(self, canv, doc):
        # 打开 PDF 时默认展开书签面板
        canv.setCatalogEntry('PageMode', PDFName('UseOutlines'))
        canv.saveState()
        canv.setFont(_styles.FONT, 9)
        canv.setFillColor(colors.HexColor('#888888'))
        canv.drawCentredString(LEFT + FRAME_W / 2, 0.45 * inch,
                               '第 %d 页' % doc.page)
        canv.setStrokeColor(colors.HexColor('#dddddd'))
        canv.line(LEFT, 0.62 * inch, LEFT + FRAME_W, 0.62 * inch)
        canv.restoreState()

    def _decorate_cover(self, canv, doc):
        canv.setCatalogEntry('PageMode', PDFName('UseOutlines'))
        canv.saveState()
        canv.setFillColor(_styles.NAVY if hasattr(_styles, 'NAVY') else colors.HexColor('#1f3a5f'))
        canv.rect(0, A4[1] - 0.35 * inch, A4[0], 0.35 * inch, fill=1, stroke=0)
        canv.rect(0, 0, A4[0], 0.35 * inch, fill=1, stroke=0)
        canv.restoreState()


def _cover_flowables(stats, title='Arch Linux 简体中文维基'):
    st = _styles.get_styles()
    today = datetime.date.today().strftime('%Y 年 %m 月 %d 日')
    flow = []
    flow.append(Spacer(1, 1.6 * inch))
    flow.append(Paragraph(title, st['cover_title']))
    flow.append(Paragraph('离线电子书（PDF）', st['cover_sub']))
    flow.append(Spacer(1, 0.3 * inch))
    flow.append(Paragraph('由爬虫自动抓取并生成', st['cover_meta']))
    flow.append(Spacer(1, 0.8 * inch))
    rows = [
        ['抓取条目总数', '%d' % stats.get('total', 0)],
        ['分类页数', '%d' % stats.get('categories', 0)],
        ['文章页数', '%d' % stats.get('articles', 0)],
        ['最大层级深度', '%d' % stats.get('depth_max', 0)],
        ['缓存正文大小', '%s' % stats.get('bytes_human', '-')],
        ['生成时间', today],
    ]
    data = [[Paragraph(k, st['cover_meta']), Paragraph(v, st['cover_meta'])] for k, v in rows]
    t = Table(data, colWidths=[2.2 * inch, 2.6 * inch])
    t.setStyle(TableStyle([
        ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
        ('ALIGN', (1, 0), (1, -1), 'LEFT'),
        ('LINEBELOW', (0, 0), (-1, -1), 0.4, colors.HexColor('#dddddd')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    flow.append(t)
    flow.append(PageBreak())
    return flow


def build_pdf(ordered_pages, out_path, stats, title='Arch Linux 简体中文维基',
              crawled_keys=None, url_map=None):
    """生成最终 PDF。

    ordered_pages: [{page_key, title, depth, html_path}, ...]（已按层级排序）
    crawled_keys / url_map: 见 html2pdf.convert，用于正确解析站内/站外链接。
    """
    doc = WikiDocTemplate(out_path, title=title, author='ArchWiki Crawler',
                         leftMargin=LEFT, rightMargin=RIGHT, topMargin=TOP, bottomMargin=BOTTOM,
                         pagesize=A4)
    st = _styles.get_styles()
    story = []
    # 封面（cover 模板）
    story.append(NextPageTemplate('cover'))
    story.extend(_cover_flowables(stats, title))
    # 切换到正文模板，开始目录页
    story.append(NextPageTemplate('body'))
    story.append(PageBreak())
    story.append(Paragraph('目录', st['toc_title']))
    story.append(doc._toc)
    story.append(PageBreak())
    # 正文：每个章节（页）从新页开始
    first = True
    for pg in ordered_pages:
        try:
            with open(pg['html_path'], 'r', encoding='utf-8') as f:
                html = f.read()
        except Exception as e:
            logger.warning('读取缓存失败 %s: %s', pg.get('html_path'), e)
            continue
        flowables = html2pdf.convert(html, page_depth=pg.get('depth', 0), frame_width=FRAME_W,
                                      crawled_keys=crawled_keys, url_map=url_map)
        if not first:
            story.append(PageBreak())
        first = False
        story.extend(flowables)
    doc.multiBuild(story)
    return out_path
