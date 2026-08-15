"""全局排版样式与中文字体注册。

reportlab 内置 CID 字体 STSong-Light（Adobe 宋体）可直接渲染简体中文，
无需额外 TTF 文件；所有段落均设置 wordWrap='CJK' 以保证中文正确换行。
"""
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.units import inch
from reportlab.lib import colors

FONT = 'STSong-Light'      # 中文正文/标题字体
CODEFONT = 'Courier'       # 代码块字体（含 ASCII；中文注释会回退为方框，属已知限制）

# 仅注册一次
try:
    pdfmetrics.registerFont(UnicodeCIDFont(FONT))
except Exception:
    pass

_NAVY = colors.HexColor('#1f3a5f')
_BLUE = colors.HexColor('#234e70')
_GREY = colors.HexColor('#555555')
_LIGHT = colors.HexColor('#f4f4f4')
_LINE = colors.HexColor('#dddddd')
_CODETXT = colors.HexColor('#222222')


def get_styles():
    s = {}
    LINK = colors.HexColor('#0645ad')
    s['body'] = ParagraphStyle('body', fontName=FONT, fontSize=10, leading=15,
                               wordWrap='CJK', spaceAfter=6, alignment=TA_LEFT, linkColor=LINK)
    s['h1'] = ParagraphStyle('h1', fontName=FONT, fontSize=18, leading=22,
                             spaceBefore=12, spaceAfter=8, textColor=_NAVY)
    s['h2'] = ParagraphStyle('h2', fontName=FONT, fontSize=14, leading=18,
                             spaceBefore=10, spaceAfter=5, textColor=_BLUE)
    s['h3'] = ParagraphStyle('h3', fontName=FONT, fontSize=12, leading=16,
                             spaceBefore=8, spaceAfter=4, textColor=_BLUE)
    s['h4'] = ParagraphStyle('h4', fontName=FONT, fontSize=11, leading=15,
                             spaceBefore=6, spaceAfter=3, textColor=_BLUE)
    s['h5'] = ParagraphStyle('h5', fontName=FONT, fontSize=10.5, leading=14,
                             spaceBefore=4, spaceAfter=2)
    s['h6'] = ParagraphStyle('h6', fontName=FONT, fontSize=10, leading=13,
                             spaceBefore=4, spaceAfter=2)
    s['code'] = ParagraphStyle('code', fontName=CODEFONT, fontSize=8.5, leading=11,
                               wordWrap='CJK', backColor=_LIGHT, borderColor=_LINE,
                               borderWidth=0.5, borderPadding=4, leftIndent=4,
                               spaceBefore=4, spaceAfter=4, textColor=_CODETXT)
    s['li'] = ParagraphStyle('li', parent=s['body'], spaceAfter=2)
    s['quote'] = ParagraphStyle('quote', parent=s['body'], leftIndent=16,
                                textColor=colors.HexColor('#444444'))
    s['cell'] = ParagraphStyle('cell', fontName=FONT, fontSize=8.5, leading=11, wordWrap='CJK', linkColor=LINK)
    s['cellh'] = ParagraphStyle('cellh', fontName=FONT, fontSize=8.5, leading=11,
                                wordWrap='CJK', textColor=colors.white)
    # 封面
    s['cover_title'] = ParagraphStyle('cover_title', fontName=FONT, fontSize=30, leading=38,
                                      alignment=TA_CENTER, textColor=_NAVY, spaceAfter=10)
    s['cover_sub'] = ParagraphStyle('cover_sub', fontName=FONT, fontSize=15, leading=22,
                                    alignment=TA_CENTER, textColor=_GREY, spaceAfter=6)
    s['cover_meta'] = ParagraphStyle('cover_meta', fontName=FONT, fontSize=10.5, leading=18,
                                     alignment=TA_CENTER, textColor=colors.HexColor('#777777'))
    s['toc_title'] = ParagraphStyle('toc_title', fontName=FONT, fontSize=22, leading=28,
                                    alignment=TA_CENTER, spaceAfter=14, textColor=_NAVY)
    return s


# 目录（TOC）各级样式
def toc_level_styles():
    from reportlab.lib.styles import ParagraphStyle
    base = get_styles()['body']
    styles = []
    sizes = [(13, 18), (11.5, 16), (10.5, 14), (10, 13), (9.5, 12), (9, 11)]
    for i, (fs, ld) in enumerate(sizes):
        st = ParagraphStyle('toc%d' % i, parent=base, fontSize=fs, leading=ld,
                            leftIndent=12 * i, spaceBefore=2 if i else 6, spaceAfter=2)
        styles.append(st)
    return styles
