"""
deck_builder.py — движок презентаций: из описания слайдов (JSON) собирает красивый
редактируемый PPTX без шаблонов. Дизайн — в духе современных AI-дизайнеров: чистая
типографика, много воздуха, один акцентный цвет, карточки вместо плотных таблиц.

  • темы (палитра + шрифты): minimal, warm, dark, academic; свой акцент — accent;
  • макеты: cover, section, bullets, cards, stats, compare, quote, timeline, table,
    chart, numbered, image, closing — каждый со своей раскладкой;
  • автоподбор размера шрифта под объём текста;
  • заметки докладчика;
  • lint(): проверка качества структуры (плотность, однообразие, длина, объём);
  • render_png(): предпросмотр слайдов картинками, если на сервере есть LibreOffice.

Модуль чистый: ничего не знает про Telegram и приложение.
"""

import io
import math
import os
import re
import shutil
import subprocess
import tempfile

import file_tools

W, H = 13.333, 7.5          # дюймы, 16:9
M = 0.9                     # поля
CW = W - 2 * M              # ширина контента
CT, CB = 2.4, 6.8           # верх и низ области контента

LAYOUTS = ["cover", "section", "bullets", "cards", "stats", "compare", "quote", "timeline",
           "table", "chart", "numbered", "image", "closing"]
MAX_SLIDES = 40

THEMES = {
    # светлая, нейтральная, синий акцент — деловая презентация
    "minimal": dict(bg="FFFFFF", fg="16181D", muted="6B7280", accent="2563EB", card="F4F5F7", line="E5E7EB",
                    font_h="Calibri", font_b="Calibri"),
    # тёплая бумага и терракота — «авторский» стиль, рассказ, идеи
    "warm": dict(bg="F6F1E9", fg="2B2420", muted="7A6F66", accent="C2410C", card="FFFDF8", line="E4DACB",
                 font_h="Georgia", font_b="Calibri"),
    # тёмная — выступления, питч, вечерний зал
    "dark": dict(bg="0F1115", fg="F3F4F6", muted="9CA3AF", accent="34D399", card="1A1D24", line="2A2F3A",
                 font_h="Calibri", font_b="Calibri"),
    # строгая — учёба, защита, отчёт: тёмно-синий акцент, засечки в заголовках
    "academic": dict(bg="FFFFFF", fg="1F2937", muted="6B7280", accent="1E3A8A", card="F3F4F6", line="D1D5DB",
                     font_h="Georgia", font_b="Calibri"),
}
THEME_HINTS = {
    "minimal": "светлая деловая, синий акцент", "warm": "тёплая бумага, терракота, засечки — авторский стиль",
    "dark": "тёмная, зелёный акцент — питч и выступления", "academic": "строгая, тёмно-синий — учёба и защита",
}


class DeckError(Exception):
    pass


# ── цвет ──────────────────────────────────────────────────────────────────────

def _hex(h, default="2563EB") -> str:
    h = str(h or "").strip().lstrip("#")
    return h.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", h) else default


def _mix(a: str, b: str, t: float) -> str:
    """t доли цвета a в цвете b."""
    pa, pb = [int(a[i:i + 2], 16) for i in (0, 2, 4)], [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "".join(f"{round(x * t + y * (1 - t)):02X}" for x, y in zip(pa, pb))


def _lum(h: str) -> float:
    r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _on(h: str) -> str:
    return "FFFFFF" if _lum(h) < 0.6 else "111111"


class Theme:
    def __init__(self, name: str, accent: str = "", font: str = ""):
        base = THEMES.get(name) or THEMES["minimal"]
        self.name = name if name in THEMES else "minimal"
        self.bg, self.fg, self.muted = base["bg"], base["fg"], base["muted"]
        self.card, self.line = base["card"], base["line"]
        self.accent = _hex(accent, base["accent"]) if accent else base["accent"]
        self.accent2 = _mix(self.accent, self.bg, 0.55)
        self.soft = _mix(self.accent, self.bg, 0.13)
        self.font_h = font or base["font_h"]
        self.font_b = font or base["font_b"]
        self.dark = _lum(self.bg) < 0.4
        # средняя ширина буквы жирного заголовка (доля кегля): с засечками заметно шире
        self.hcw = 0.72 if self.font_h in ("Georgia", "Times New Roman", "Cambria", "Garamond") else 0.56

    @property
    def palette(self):
        return [self.accent, self.accent2, _mix(self.accent, self.fg, 0.55), _mix(self.accent, self.bg, 0.3),
                self.muted, _mix(self.fg, self.bg, 0.5)]


# ── оценка размера текста (python-pptx не умеет мерить) ───────────────────────

def _lines(text: str, width_in: float, pt: float, cw: float) -> int:
    per = max(1, int(width_in * 72 / (pt * cw)))
    return sum(max(1, math.ceil(len(p) / per)) for p in str(text).split("\n"))


def fit_pt(paras, w_in, h_in, max_pt, min_pt, cw=0.54, spacing=1.22, gap_pt=6) -> int:
    texts = [plain(p) for p in paras if p is not None] or [""]
    longest = max((len(wd) for t in texts for wd in re.split(r"\s+", t)), default=1)
    for pt in range(int(max_pt), int(min_pt) - 1, -1):
        if longest * pt * cw / 72 > w_in * 0.97:      # самое длинное слово не должно рваться по буквам
            continue
        n = sum(_lines(t, w_in, pt, cw) for t in texts)
        if (n * pt * spacing + gap_pt * len(texts)) / 72 <= h_in:
            return pt
    return int(min_pt)


def plain(t) -> str:
    return file_tools.plain(str(t or ""))


# ── примитивы python-pptx ─────────────────────────────────────────────────────

def _imports():
    try:
        from pptx import Presentation
        from pptx.chart.data import CategoryChartData
        from pptx.dml.color import RGBColor
        from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
        from pptx.oxml.ns import qn
        from pptx.util import Emu, Inches, Pt
        from lxml import etree
    except ImportError:
        raise DeckError("на сервере не установлен python-pptx (добавьте в requirements.txt и задеплойте)")
    return dict(Presentation=Presentation, CategoryChartData=CategoryChartData, RGBColor=RGBColor,
                XL_CHART_TYPE=XL_CHART_TYPE, XL_LEGEND_POSITION=XL_LEGEND_POSITION, MSO_SHAPE=MSO_SHAPE,
                MSO_ANCHOR=MSO_ANCHOR, MSO_AUTO_SIZE=MSO_AUTO_SIZE, PP_ALIGN=PP_ALIGN, qn=qn, Emu=Emu,
                Inches=Inches, Pt=Pt, etree=etree)


class _Ctx:
    """Рабочий контекст сборки: библиотеки, тема, название колоды, картинки."""
    def __init__(self, lib, T, deck_title, total, images):
        self.l, self.T, self.deck_title, self.total, self.images = lib, T, deck_title, total, images
        self.n = 0


def _rgb(c, h):
    return c.l["RGBColor"].from_string(h)


def _rect(c, slide, x, y, w, h, fill=None, line=None, radius=None, oval=False, name=None):
    S = c.l["MSO_SHAPE"]
    kind = S.OVAL if oval else (S.ROUNDED_RECTANGLE if radius else S.RECTANGLE)
    shp = slide.shapes.add_shape(kind, c.l["Inches"](x), c.l["Inches"](y), c.l["Inches"](w), c.l["Inches"](h))
    if radius and not oval:
        shp.adjustments[0] = min(0.5, radius / max(0.01, min(w, h)))
    if fill:
        shp.fill.solid()
        shp.fill.fore_color.rgb = _rgb(c, fill)
    else:
        shp.fill.background()
    if line:
        shp.line.color.rgb = _rgb(c, line)
        shp.line.width = c.l["Pt"](1)
    else:
        shp.line.fill.background()
    shp.shadow.inherit = False
    st = shp._element.find(c.l["qn"]("p:style"))   # стиль темы тянет тень и обводку — убираем целиком
    if st is not None:
        shp._element.remove(st)
    if name:
        shp.name = name
    return shp


def _bullet(c, p, char, color, indent):
    pPr = p._p.get_or_add_pPr()
    qn, etree = c.l["qn"], c.l["etree"]
    pPr.set("marL", str(int(c.l["Inches"](indent))))
    pPr.set("indent", str(-int(c.l["Inches"](indent))))
    for tag in ("a:buNone", "a:buChar", "a:buClr", "a:buFont", "a:buSzPct"):
        for el in pPr.findall(qn(tag)):
            pPr.remove(el)
    clr = etree.SubElement(pPr, qn("a:buClr"))
    etree.SubElement(clr, qn("a:srgbClr")).set("val", color)
    etree.SubElement(pPr, qn("a:buSzPct")).set("val", "70000")
    etree.SubElement(pPr, qn("a:buChar")).set("char", char)


def _runs(c, p, text, size, color, bold=False, italic=False, font=None, accent=None):
    for t, fl in file_tools.parse_inline(str(text or "")):
        r = p.add_run()
        r.text = t
        f = r.font
        f.size = c.l["Pt"](size)
        f.name = font or c.T.font_b
        f.bold = bool(bold or "b" in fl)
        f.italic = bool(italic or "i" in fl)
        f.color.rgb = _rgb(c, (accent or c.T.accent) if "code" in fl else color)


def _text(c, slide, x, y, w, h, paras, size=18, color=None, bold=False, italic=False, font=None, align="l",
          anchor="t", spacing=1.12, after=0, bullet=None, name=None, accent=None):
    """paras: строка или список строк / словарей {text,size,bold,color,italic,bullet,level,after,font}."""
    L = c.l
    tb = slide.shapes.add_textbox(L["Inches"](x), L["Inches"](y), L["Inches"](w), L["Inches"](h))
    if name:
        tb.name = name
    tf = tb.text_frame
    tf.word_wrap = True
    tf.auto_size = L["MSO_AUTO_SIZE"].NONE
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = {"t": L["MSO_ANCHOR"].TOP, "m": L["MSO_ANCHOR"].MIDDLE, "b": L["MSO_ANCHOR"].BOTTOM}[anchor]
    items = paras if isinstance(paras, list) else [paras]
    for i, it in enumerate(items):
        d = it if isinstance(it, dict) else {"text": it}
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = {"l": L["PP_ALIGN"].LEFT, "c": L["PP_ALIGN"].CENTER, "r": L["PP_ALIGN"].RIGHT}[d.get("align", align)]
        p.line_spacing = spacing
        p.space_after = L["Pt"](d.get("after", after))
        _runs(c, p, d.get("text"), d.get("size", size), d.get("color") or color or c.T.fg, d.get("bold", bold),
              d.get("italic", italic), d.get("font") or font, accent)
        b = d.get("bullet", bullet)
        if b:
            lvl = d.get("level", 0)
            _bullet(c, p, b, d.get("bcolor") or c.T.accent, 0.3 + 0.1 * lvl)
            if lvl:
                p._p.get_or_add_pPr().set("marL", str(int(L["Inches"](0.75))))
    return tb


def _new_slide(c, bg=None, notes=""):
    slide = c.prs.slides.add_slide(c.prs.slide_layouts[5])   # «Только заголовок»: у слайда есть настоящий title
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = _rgb(c, bg or c.T.bg)
    if notes:
        slide.notes_slide.notes_text_frame.text = str(notes)
    c.n += 1
    return slide


def _title(c, slide, text, y=0.78, h=1.2, max_pt=38, min_pt=24, color=None, w=None, x=M, bar=True, bar_color=None):
    L, T = c.l, c.T
    w = w or CW
    ph = slide.shapes.title
    ph.left, ph.top, ph.width, ph.height = L["Inches"](x), L["Inches"](y), L["Inches"](w), L["Inches"](h)
    tf = ph.text_frame
    tf.word_wrap = True
    tf.auto_size = L["MSO_AUTO_SIZE"].NONE
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = L["MSO_ANCHOR"].TOP
    size = fit_pt([plain(text)], w, h, max_pt, min_pt, cw=T.hcw, spacing=1.1, gap_pt=0)
    p = tf.paragraphs[0]
    p.alignment = L["PP_ALIGN"].LEFT
    p.line_spacing = 1.04
    r = p.add_run()
    r.text = plain(text)
    r.font.size, r.font.bold, r.font.name = L["Pt"](size), True, T.font_h
    r.font.color.rgb = _rgb(c, color or T.fg)
    if bar:
        _rect(c, slide, x, y - 0.26, 0.55, 0.07, bar_color or T.accent, name="Accent bar")


def _footer(c, slide, light=False):
    T = c.T
    col = _mix(T.muted, T.bg, 0.9)
    _text(c, slide, M, 7.0, 8, 0.25, c.deck_title, size=10, color=col, name="Footer")
    _text(c, slide, W - M - 1.5, 7.0, 1.5, 0.25, f"{c.n} / {c.total}", size=10, color=col, align="r", name="Slide number")


def _items(v, key=None):
    """Нормализация списков: строки или словари → список словарей {text,...}."""
    out = []
    for it in v or []:
        if isinstance(it, dict):
            out.append(it)
        elif it not in (None, ""):
            out.append({(key or "text"): str(it)})
    return out


# ── макеты ────────────────────────────────────────────────────────────────────

def _cover(c, s, closing=False):
    T = c.T
    slide = _new_slide(c, notes=s.get("notes"))
    _rect(c, slide, 9.6, -1.6, 6.2, 6.2, T.soft, oval=True, name="Decor 1")
    _rect(c, slide, 11.3, 3.6, 3.6, 3.6, _mix(T.accent, T.bg, 0.07), oval=True, name="Decor 2")
    _rect(c, slide, M, 2.55, 0.9, 0.09, T.accent, name="Accent bar")
    title = s.get("title") or ("Спасибо" if closing else "Презентация")
    _title(c, slide, title, y=2.85, h=1.75, max_pt=58 if not closing else 66, min_pt=34, w=9.2, bar=False)
    sub = s.get("subtitle")
    if sub:
        _text(c, slide, M, 4.75, 8.6, 1.0, sub, size=fit_pt([plain(sub)], 8.6, 1.0, 24, 15), color=T.muted, spacing=1.2)
    meta = s.get("meta") or s.get("author")
    if meta:
        _text(c, slide, M, 6.55, 9, 0.4, meta, size=15, color=T.muted, name="Meta")


def _section(c, s):
    T = c.T
    bg = T.accent
    on = _on(bg)
    slide = _new_slide(c, bg=bg, notes=s.get("notes"))
    num = s.get("number")
    if num not in (None, ""):
        _text(c, slide, M, 1.5, 4, 1.5, str(num), size=96, bold=True, color=_mix(on, bg, 0.5), font=T.font_h)
    _title(c, slide, s.get("title") or "", y=3.35, h=1.6, max_pt=48, min_pt=30, color=on, w=10.5, bar=False)
    if s.get("subtitle"):
        _text(c, slide, M, 5.1, 9.5, 1.0, s["subtitle"], size=22, color=_mix(on, bg, 0.85), spacing=1.2)


def _bullets(c, s):
    T = c.T
    slide = _new_slide(c, notes=s.get("notes"))
    _title(c, slide, s.get("title") or "", w=CW)
    items = _items(s.get("bullets"))
    h = CB - CT
    paras_txt = []
    for it in items:
        paras_txt.append(it.get("text", ""))
        paras_txt += [x if isinstance(x, str) else x.get("text", "") for x in (it.get("sub") or [])]
    size = fit_pt(paras_txt, CW - 0.5, h, 26, 15, cw=0.5, gap_pt=11)
    paras = []
    for it in items:
        paras.append({"text": it.get("text", ""), "bullet": "●", "after": 11, "size": size})
        for sub in it.get("sub") or []:
            paras.append({"text": sub if isinstance(sub, str) else sub.get("text", ""), "bullet": "–", "level": 1,
                          "size": max(12, size - 3), "color": T.muted, "after": 6})
    _text(c, slide, M, CT, CW, h, paras, size=size, spacing=1.12, name="Bullets")
    _footer(c, slide)


def _cards(c, s):
    T = c.T
    slide = _new_slide(c, notes=s.get("notes"))
    _title(c, slide, s.get("title") or "")
    cards = _items(s.get("cards"), "heading")[:4]
    n = max(1, len(cards))
    gap = 0.3
    longest = max([len(wd) for cd in cards for wd in re.split(r"\s+", plain(cd.get("heading") or cd.get("title") or ""))] + [1])
    # четыре узкие колонки не вмещают длинные слова заголовков — тогда сетка 2×2
    grid = n == 4 and longest * 16 * T.hcw / 72 > (CW - gap * 3) / 4 - 0.75
    if grid:
        cw, ch = (CW - gap) / 2, (CB - CT - gap) / 2
    else:
        cw, ch = (CW - gap * (n - 1)) / n, CB - CT - 0.1
    for i, cd in enumerate(cards):
        if grid:
            x, y = M + (i % 2) * (cw + gap), CT + (i // 2) * (ch + gap)
        else:
            x, y = M + i * (cw + gap), CT
        _rect(c, slide, x, y, cw, ch, T.card, T.line, radius=0.2, name=f"Card {i + 1}")
        badge = cd.get("icon")
        _rect(c, slide, x + 0.35, y + 0.35, 0.62, 0.62, T.soft, oval=True)
        _text(c, slide, x + 0.35, y + 0.35, 0.62, 0.62, str(badge) if badge else f"{i + 1}", size=22 if badge else 18,
              bold=True, color=T.accent, align="c", anchor="m", font=T.font_h)
        hd, body = cd.get("heading") or cd.get("title") or "", cd.get("text") or ""
        if grid:
            hx, hw = x + 1.25, cw - 1.6
            hsz = fit_pt([plain(hd)], hw, 0.7, 22, 14, cw=T.hcw)
            _text(c, slide, hx, y + 0.3, hw, 0.7, hd, size=hsz, bold=True, font=T.font_h, spacing=1.08, anchor="m")
            bsz = fit_pt([plain(body)], hw, ch - 1.3, 19, 12, cw=0.5, gap_pt=4)
            _text(c, slide, hx, y + 1.1, hw, ch - 1.3, body, size=bsz, color=T.muted, spacing=1.2)
            continue
        inner = cw - 0.7
        hsz = fit_pt([plain(hd)], inner, 1.0, 22, 12, cw=T.hcw)
        _text(c, slide, x + 0.35, y + 1.25, inner, 1.0, hd, size=hsz, bold=True, font=T.font_h, spacing=1.08)
        bsz = fit_pt([plain(body)], inner, ch - 2.5, 19, 12, cw=0.5, gap_pt=4)
        _text(c, slide, x + 0.35, y + 2.3, inner, ch - 2.5, body, size=bsz, color=T.muted, spacing=1.2)
    _footer(c, slide)


def _stats(c, s):
    T = c.T
    slide = _new_slide(c, notes=s.get("notes"))
    stats = _items(s.get("stats"), "value")[:4]
    if not stats and s.get("value") is not None:
        stats = [{"value": s.get("value"), "label": s.get("label", "")}]
    if s.get("title"):
        _title(c, slide, s["title"])
    n = max(1, len(stats))
    cap = s.get("caption")
    top = 2.9 if s.get("title") else 1.7
    if n == 1:
        st = stats[0]
        v = str(st.get("value", ""))
        _text(c, slide, M, top, CW, 2.2, v, size=fit_pt([v], CW, 2.2, 120, 60, cw=0.62), bold=True, color=T.accent,
              font=T.font_h, spacing=1.0, anchor="m")
        _text(c, slide, M, top + 2.35, 10, 1.0, st.get("label", ""), size=28, bold=True, spacing=1.12)
    else:
        gap = 0.45
        cw = (CW - gap * (n - 1)) / n
        for i, st in enumerate(stats):
            x = M + i * (cw + gap)
            _rect(c, slide, x, top, cw, 0.05, T.accent)
            v = str(st.get("value", ""))
            _text(c, slide, x, top + 0.2, cw, 1.5, v, size=fit_pt([v], cw, 1.5, 72, 32, cw=0.62), bold=True, color=T.accent,
                  font=T.font_h, spacing=1.0, anchor="m")
            lab = st.get("label", "")
            _text(c, slide, x, top + 1.85, cw, 1.4, lab, size=fit_pt([plain(lab)], cw, 1.4, 20, 13), spacing=1.18)
    if cap:
        _text(c, slide, M, 6.35, CW, 0.5, cap, size=14, color=T.muted)
    _footer(c, slide)


def _compare(c, s):
    T = c.T
    slide = _new_slide(c, notes=s.get("notes"))
    _title(c, slide, s.get("title") or "")
    gap = 0.4
    pw = (CW - gap) / 2
    ph = CB - CT - 0.1
    for i, key in enumerate(("left", "right")):
        side = s.get(key) or {}
        x = M + i * (pw + gap)
        col = T.accent if i == 0 else _mix(T.accent, T.fg, 0.55)
        _rect(c, slide, x, CT, pw, ph, T.card, T.line, radius=0.2, name=f"Panel {i + 1}")
        _rect(c, slide, x + 0.4, CT + 0.4, 0.5, 0.07, col)
        _text(c, slide, x + 0.4, CT + 0.6, pw - 0.8, 0.8, side.get("heading") or ("До" if i == 0 else "После"),
              size=24, bold=True, font=T.font_h, color=col if T.dark else T.fg)
        bl = [b if isinstance(b, str) else b.get("text", "") for b in side.get("bullets") or []]
        size = fit_pt(bl, pw - 1.1, ph - 1.7, 20, 13, cw=0.5, gap_pt=9)
        _text(c, slide, x + 0.4, CT + 1.5, pw - 0.8, ph - 1.7,
              [{"text": b, "bullet": "●", "bcolor": col, "after": 9, "size": size} for b in bl], size=size, spacing=1.14)
    _footer(c, slide)


def _quote(c, s):
    T = c.T
    slide = _new_slide(c, notes=s.get("notes"))
    _rect(c, slide, M, 1.55, 0.09, 4.4, T.accent, name="Accent bar")
    _text(c, slide, M + 0.45, 1.0, 2, 1.6, "“", size=130, bold=True, color=T.accent, font=T.font_h, spacing=0.8)
    q = s.get("quote") or s.get("title") or ""
    _text(c, slide, M + 0.45, 2.55, 10.4, 3.0, q, size=fit_pt([plain(q)], 10.4, 3.0, 38, 22, cw=T.hcw + 0.02, spacing=1.2), italic=True,
          font=T.font_h, spacing=1.18, anchor="t")
    if s.get("author"):
        _text(c, slide, M + 0.45, 5.75, 10, 0.5, "— " + s["author"], size=18, color=T.muted)
    _footer(c, slide)


def _timeline(c, s):
    T = c.T
    slide = _new_slide(c, notes=s.get("notes"))
    _title(c, slide, s.get("title") or "")
    steps = _items(s.get("steps"), "label")[:6]
    n = max(1, len(steps))
    ly = 3.55
    _rect(c, slide, M, ly - 0.015, CW, 0.03, T.line, name="Line")
    cw = CW / n
    for i, st in enumerate(steps):
        cx = M + (i + 0.5) * cw
        _rect(c, slide, cx - 0.32, ly - 0.32, 0.64, 0.64, T.accent, oval=True, name=f"Step {i + 1}")
        _text(c, slide, cx - 0.32, ly - 0.32, 0.64, 0.64, str(i + 1), size=18, bold=True, color=_on(T.accent), align="c", anchor="m")
        inner = cw - 0.3
        lab, txt = st.get("label") or st.get("heading") or "", st.get("text") or ""
        _text(c, slide, cx - inner / 2, ly + 0.55, inner, 0.9, lab, size=fit_pt([plain(lab)], inner, 0.9, 20, 11, cw=T.hcw), bold=True,
              align="c", font=T.font_h, spacing=1.08)
        _text(c, slide, cx - inner / 2, ly + 1.55, inner, 1.8, txt, size=fit_pt([plain(txt)], inner, 1.8, 16, 11, cw=0.5), color=T.muted,
              align="c", spacing=1.2)
    _footer(c, slide)


def _table(c, s):
    T, L = c.T, c.l
    slide = _new_slide(c, notes=s.get("notes"))
    _title(c, slide, s.get("title") or "")
    header = [str(x) for x in (s.get("header") or [])]
    rows = [[("" if v is None else str(v)) for v in r] for r in (s.get("rows") or [])]
    cols = max([len(header)] + [len(r) for r in rows] + [1])
    header += [""] * (cols - len(header))
    rows = [r + [""] * (cols - len(r)) for r in rows]
    nrows = len(rows) + 1
    size = max(11, 20 - (1 if nrows > 5 else 0) * 2 - (1 if cols > 4 else 0) * 2 - (1 if nrows > 8 else 0) * 2)
    rh = min(0.8, (CB - CT) / nrows)
    shape = slide.shapes.add_table(nrows, cols, L["Inches"](M), L["Inches"](CT), L["Inches"](CW), L["Inches"](rh * nrows))
    tbl = shape.table
    tbl.horz_banding = False
    tbl.first_row = True
    weights = [max(6, max([len(header[j])] + [len(r[j]) for r in rows]) or 6) for j in range(cols)]
    tot = float(sum(min(w, 40) for w in weights))
    for j in range(cols):
        tbl.columns[j].width = L["Inches"](CW * min(weights[j], 40) / tot)
    for i in range(nrows):
        tbl.rows[i].height = L["Inches"](rh)
        for j in range(cols):
            cell = tbl.cell(i, j)
            val = header[j] if i == 0 else rows[i - 1][j]
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(c, T.accent if i == 0 else (T.card if i % 2 else T.bg))
            cell.margin_left = cell.margin_right = L["Inches"](0.14)
            cell.margin_top = cell.margin_bottom = L["Inches"](0.05)
            cell.vertical_anchor = L["MSO_ANCHOR"].MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            numeric = bool(re.fullmatch(r"[\s\-+−–]?[\d\s.,]+\s*[%₽$€]?", val)) and i > 0
            p.alignment = L["PP_ALIGN"].RIGHT if numeric else L["PP_ALIGN"].LEFT
            _runs(c, p, val, size, _on(T.accent) if i == 0 else T.fg, bold=(i == 0 or j == 0))
    _footer(c, slide)


def _chart(c, s):
    T, L = c.T, c.l
    slide = _new_slide(c, notes=s.get("notes"))
    _title(c, slide, s.get("title") or "")
    ch = s.get("chart") or {}
    cats = [str(x) for x in ch.get("categories") or []]
    series = ch.get("series") or []
    kind = str(ch.get("type") or "column").lower()
    XT = L["XL_CHART_TYPE"]
    ctype = {"column": XT.COLUMN_CLUSTERED, "bar": XT.BAR_CLUSTERED, "line": XT.LINE_MARKERS, "pie": XT.PIE,
             "doughnut": XT.DOUGHNUT}.get(kind, XT.COLUMN_CLUSTERED)
    if not cats or not series:
        raise DeckError("chart: нужны categories и series [{name, values}]")
    data = L["CategoryChartData"]()
    data.categories = cats
    for sr in series:
        vals = [float(v) if isinstance(v, (int, float)) or re.fullmatch(r"-?\d+(\.\d+)?", str(v)) else 0.0 for v in (sr.get("values") or [])]
        vals = (vals + [0.0] * len(cats))[:len(cats)]
        data.add_series(str(sr.get("name") or ""), vals)
    has_panel = bool(s.get("takeaway") or s.get("bullets"))
    cw_ = 7.7 if has_panel else CW
    gf = slide.shapes.add_chart(ctype, L["Inches"](M), L["Inches"](CT - 0.05), L["Inches"](cw_), L["Inches"](CB - CT + 0.1), data)
    chart = gf.chart
    chart.font.size = L["Pt"](13)
    chart.font.name = T.font_b
    chart.font.color.rgb = _rgb(c, T.muted)
    pal = T.palette
    pie = kind in ("pie", "doughnut")
    chart.has_legend = pie or len(series) > 1
    if chart.has_legend:
        chart.legend.position = L["XL_LEGEND_POSITION"].BOTTOM
        chart.legend.include_in_layout = False
        chart.legend.font.size = L["Pt"](13)
    chart.has_title = False
    plot = chart.plots[0]
    if pie:
        plot.has_data_labels = True
        plot.data_labels.font.size = L["Pt"](13)
        plot.data_labels.font.color.rgb = _rgb(c, "FFFFFF")
        plot.data_labels.number_format = '0"%"' if ch.get("percent") else "General"
        plot.data_labels.number_format_is_linked = False
        for i, pt in enumerate(plot.series[0].points):
            pt.format.fill.solid()
            pt.format.fill.fore_color.rgb = _rgb(c, pal[i % len(pal)])
    else:
        if kind in ("column", "bar"):
            plot.gap_width = 60
        for i, sr in enumerate(plot.series):
            col = pal[i % len(pal)]
            if kind == "line":
                sr.format.line.color.rgb = _rgb(c, col)
                sr.format.line.width = L["Pt"](3)
                sr.smooth = False
                sr.marker.format.fill.solid()
                sr.marker.format.fill.fore_color.rgb = _rgb(c, col)
                sr.marker.format.line.color.rgb = _rgb(c, col)
            else:
                sr.format.fill.solid()
                sr.format.fill.fore_color.rgb = _rgb(c, col)
        if len(series) == 1 and kind in ("column", "bar"):
            plot.has_data_labels = True
            plot.data_labels.font.size = L["Pt"](13)
            plot.data_labels.font.color.rgb = _rgb(c, T.fg)
        va, ca = chart.value_axis, chart.category_axis
        va.has_major_gridlines = True
        va.major_gridlines.format.line.color.rgb = _rgb(c, T.line)
        va.format.line.fill.background()
        ca.format.line.color.rgb = _rgb(c, T.line)
        va.tick_labels.font.size = ca.tick_labels.font.size = L["Pt"](13)
    if has_panel:
        px, pw = M + cw_ + 0.45, CW - cw_ - 0.45
        tk = s.get("takeaway") or ""
        tsz = fit_pt([plain(tk)], pw - 0.3, 2.3, 28, 16, cw=T.hcw, spacing=1.2)
        tk_h = min(2.6, _lines(plain(tk), pw - 0.3, tsz, T.hcw) * tsz * 1.2 / 72 + 0.1)
        _rect(c, slide, px, CT + 0.1, 0.07, max(1.0, tk_h), T.accent)
        _text(c, slide, px + 0.3, CT + 0.05, pw - 0.3, tk_h, tk, size=tsz, bold=True, font=T.font_h, spacing=1.12)
        bl = [b if isinstance(b, str) else b.get("text", "") for b in s.get("bullets") or []]
        if bl:
            by = CT + max(1.0, tk_h) + 0.45
            _text(c, slide, px + 0.3, by, pw - 0.3, CB - by, [{"text": b, "bullet": "●", "after": 8} for b in bl], size=16,
                  color=T.muted, spacing=1.15)
    _footer(c, slide)


def _numbered(c, s):
    T = c.T
    slide = _new_slide(c, notes=s.get("notes"))
    _title(c, slide, s.get("title") or "")
    items = [it.get("text") or it.get("heading") or "" for it in _items(s.get("items") or s.get("bullets"))][:8]
    two = len(items) > 5
    colw = (CW - 0.5) / 2 if two else CW
    per = math.ceil(len(items) / 2) if two else len(items)
    rowh = min(0.95, (CB - CT) / max(1, per))
    for i, t in enumerate(items):
        col, row = (i // per, i % per) if two else (0, i)
        x, y = M + col * (colw + 0.5), CT + row * rowh
        _text(c, slide, x, y, 1.0, rowh, f"{i + 1:02d}", size=30, bold=True, color=T.accent, font=T.font_h, anchor="m")
        _text(c, slide, x + 1.05, y, colw - 1.1, rowh, t, size=fit_pt([plain(t)], colw - 1.1, rowh, 24, 14, cw=0.52, gap_pt=0), anchor="m",
              spacing=1.1)
        if row < per - 1 and i < len(items) - 1:
            _rect(c, slide, x + 1.05, y + rowh - 0.02, colw - 1.1, 0.012, T.line)
    _footer(c, slide)


def _image(c, s):
    T, L = c.T, c.l
    slide = _new_slide(c, notes=s.get("notes"))
    blob = c.images.get(id(s)) or c.images.get(s.get("image_prompt"))
    tw = 6.0 if blob else CW
    _title(c, slide, s.get("title") or "", w=tw)
    items = _items(s.get("bullets"))
    size = fit_pt([it.get("text", "") for it in items], tw - 0.4, CB - CT, 22, 14, cw=0.5, gap_pt=10)
    _text(c, slide, M, CT, tw, CB - CT, [{"text": it.get("text", ""), "bullet": "●", "after": 10, "size": size} for it in items],
          size=size, spacing=1.12)
    if blob:
        from PIL import Image
        x, y, w, h = 7.45, 0, W - 7.45, H
        im = Image.open(io.BytesIO(blob))
        iw, ih = im.size
        pic = slide.shapes.add_picture(io.BytesIO(blob), L["Inches"](x), L["Inches"](y), L["Inches"](w), L["Inches"](h))
        box, ratio = w / h, iw / ih
        if ratio > box:
            pic.crop_left = pic.crop_right = (1 - box / ratio) / 2
        else:
            pic.crop_top = pic.crop_bottom = (1 - ratio / box) / 2
        pic.name = "Image"
    else:
        _footer(c, slide)


_LAYOUT_FN = {"cover": _cover, "section": _section, "bullets": _bullets, "cards": _cards, "stats": _stats,
              "compare": _compare, "quote": _quote, "timeline": _timeline, "table": _table, "chart": _chart,
              "numbered": _numbered, "image": _image, "closing": lambda c, s: _cover(c, s, closing=True)}


# ── нормализация и проверка качества ──────────────────────────────────────────

def _s(v, n=400) -> str:
    return re.sub(r"[ \t]+", " ", str(v if v is not None else "")).strip()[:n]


def normalize(spec: dict) -> dict:
    """Приводит описание к безопасному виду; DeckError с понятным текстом, если слайдов нет
    или макет неизвестен."""
    if not isinstance(spec, dict):
        raise DeckError("описание презентации должно быть объектом")
    slides = spec.get("slides")
    if not isinstance(slides, list) or not slides:
        raise DeckError("slides пуст: нужен список слайдов [{layout, title, …}]")
    if len(slides) > MAX_SLIDES:
        raise DeckError(f"слишком много слайдов ({len(slides)}); максимум {MAX_SLIDES}")
    out = []
    for i, sl in enumerate(slides, 1):
        if not isinstance(sl, dict):
            raise DeckError(f"слайд {i}: ожидался объект")
        lay = str(sl.get("layout") or "bullets").strip().lower()
        if lay not in LAYOUTS:
            raise DeckError(f"слайд {i}: неизвестный макет «{lay}». Доступны: {', '.join(LAYOUTS)}")
        d = dict(sl)
        d["layout"] = lay
        for k in ("title", "subtitle", "quote", "author", "caption", "takeaway", "meta", "label", "number"):
            if k in d:
                d[k] = _s(d[k], 220)
        d["notes"] = _s(d.get("notes"), 3000)
        out.append(d)
    theme = str(spec.get("theme") or "minimal").lower()
    return {"title": _s(spec.get("title") or out[0].get("title") or "Презентация", 120),
            "theme": theme if theme in THEMES else "minimal", "accent": _hex(spec.get("accent")) if spec.get("accent") else "",
            "font": _s(spec.get("font"), 40), "author": _s(spec.get("author"), 120), "slides": out}


def _words(t) -> int:
    return len(re.findall(r"\S+", plain(t)))


def lint(spec: dict, minutes: float = 0) -> list:
    """Замечания по качеству структуры — модель исправляет их до отправки пользователю."""
    w, sl = [], spec["slides"]
    if sl[0]["layout"] != "cover":
        w.append("первый слайд должен быть cover (титульный)")
    if len(sl) > 3 and sl[-1]["layout"] not in ("closing", "bullets", "cards", "numbered"):
        w.append("в конце нужен итог: слайд closing или bullets с выводами/следующими шагами")
    if minutes:
        if len(sl) > max(4, minutes * 2):
            w.append(f"{len(sl)} слайдов — много для {minutes:g} мин (ориентир ≈ {round(minutes * 1.2)}); сократи")
        if len(sl) < max(3, minutes * 0.5):
            w.append(f"{len(sl)} слайдов — мало для {minutes:g} мин (ориентир ≈ {round(minutes * 1.2)})")
    run = 0
    prev = None
    for i, s in enumerate(sl, 1):
        lay = s["layout"]
        if lay not in ("cover", "closing", "quote") and not s.get("title") and not (lay == "stats" and s.get("stats")):
            w.append(f"слайд {i}: нет title (заголовок = вывод слайда)")
        if s.get("title") and _words(s["title"]) > 14:
            w.append(f"слайд {i}: заголовок из {_words(s['title'])} слов — сократи до ≤ 12")
        bl = _items(s.get("bullets"))
        if lay in ("bullets", "image") and len(bl) > 6:
            w.append(f"слайд {i}: {len(bl)} пунктов — оставь ≤ 6 или раздели на два слайда")
        for b in bl:
            if _words(b.get("text")) > 18:
                w.append(f"слайд {i}: пункт «{plain(b.get('text'))[:40]}…» длиннее 18 слов — сократи, подробности в notes")
                break
        if lay == "cards" and len(_items(s.get("cards"))) > 4:
            w.append(f"слайд {i}: карточек {len(_items(s.get('cards')))} — максимум 4")
        if lay == "timeline" and len(_items(s.get("steps"))) > 6:
            w.append(f"слайд {i}: шагов в timeline {len(_items(s.get('steps')))} — максимум 6")
        if lay == "stats" and len(_items(s.get("stats"))) > 4:
            w.append(f"слайд {i}: показателей {len(_items(s.get('stats')))} — максимум 4")
        if lay == "table" and (len(s.get("rows") or []) > 8 or len(s.get("header") or []) > 6):
            w.append(f"слайд {i}: таблица велика (≤ 8 строк и ≤ 6 столбцов) — раздели или используй chart")
        if lay == "chart" and len((s.get("chart") or {}).get("categories") or []) > 12:
            w.append(f"слайд {i}: на диаграмме больше 12 категорий — сгруппируй")
        if lay == "image" and not s.get("image_prompt"):
            w.append(f"слайд {i}: у image нет image_prompt")
        run = run + 1 if lay == prev and lay in ("bullets", "cards") else 1
        prev = lay
        if run >= 3:
            w.append(f"слайды {i - 2}–{i}: три «{lay}» подряд — чередуй макеты (stats, chart, compare, timeline, quote…)")
            run = 0
    body = [s for s in sl if s["layout"] not in ("cover", "closing", "section")]
    if len(body) >= 4 and all(s["layout"] == "bullets" for s in body):
        w.append("все слайды — bullets: добавь разнообразие (cards, stats, chart, timeline, compare)")
    if len(sl) >= 5 and sum(1 for s in sl if s.get("notes")) < len(sl) * 0.6:
        w.append("мало заметок докладчика (notes): напиши, что сказать на каждом слайде (30–60 секунд)")
    return w


# ── сборка ────────────────────────────────────────────────────────────────────

def build(spec: dict, images: dict = None) -> bytes:
    """spec — результат normalize(). images — {id(слайда) или image_prompt: bytes}."""
    lib = _imports()
    T = Theme(spec["theme"], spec.get("accent"), spec.get("font"))
    prs = lib["Presentation"]()
    prs.slide_width, prs.slide_height = lib["Inches"](W), lib["Inches"](H)
    c = _Ctx(lib, T, spec["title"], len(spec["slides"]), images or {})
    c.prs = prs
    for i, s in enumerate(spec["slides"], 1):
        try:
            _LAYOUT_FN[s["layout"]](c, s)
        except DeckError:
            raise
        except Exception as e:     # ошибка в одном слайде не должна ронять всю колоду
            raise DeckError(f"слайд {i} ({s['layout']}): {type(e).__name__}: {e}")
    core = prs.core_properties
    core.title, core.author = spec["title"], spec.get("author") or "Jarvis"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ── предпросмотр (нужен LibreOffice на сервере) ───────────────────────────────

def preview_available() -> bool:
    return bool(shutil.which("soffice") or shutil.which("libreoffice")) and bool(shutil.which("pdftoppm"))


def render_png(pptx_bytes: bytes, max_slides: int = 10, width: int = 1280):
    """→ (список PNG-байтов, pdf-байты) либо (None, None), если рендерить нечем или не вышло."""
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe or not shutil.which("pdftoppm"):
        return None, None
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "deck.pptx")
        with open(src, "wb") as f:
            f.write(pptx_bytes)
        try:
            subprocess.run([exe, "--headless", f"-env:UserInstallation=file://{d}/lo", "--convert-to", "pdf", "--outdir", d, src],
                           check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pdf = os.path.join(d, "deck.pdf")
            subprocess.run(["pdftoppm", "-png", "-scale-to-x", str(width), "-scale-to-y", "-1", "-l", str(max_slides), pdf,
                            os.path.join(d, "s")], check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pngs = sorted(p for p in os.listdir(d) if p.startswith("s") and p.endswith(".png"))
            return [open(os.path.join(d, p), "rb").read() for p in pngs], open(pdf, "rb").read()
        except Exception:
            return None, None
