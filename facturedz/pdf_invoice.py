# -*- coding: utf-8 -*-
"""The invoice PDF, drawn directly with Qt (QPdfWriter + QPainter).

No Excel, LibreOffice, browser, printer or PowerShell is involved, so the PDF is produced the same
way on every machine and never depends on what is installed.

It reproduces the Excel template's form - rounded header with the two flags, seller box on the right and
buyer box on the left, the date | invoice number | serial number row, the bordered items table, totals, amount in words, payment boxes, signature -
and takes every string from ``invoice_format`` (the same module that fills the Excel file).

Structure
  build_layout()      pure Python: turns an invoice into a list of drawing ops (points, A4).
                      Needs only a ``Measurer`` for text sizes, so it is unit-testable without Qt.
  render_invoice_pdf  runs build_layout() with Qt font metrics and paints the ops into a PDF.
"""
from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from . import invoice_format as fmt
from .amounts import amount_in_words
from .config import TEMPLATE_ITEM_ROWS

log = logging.getLogger("facturedz.pdf")

# --------------------------------------------------------------------------- #
# geometry (PostScript points, A4). Proportions measured from the Excel template.
# --------------------------------------------------------------------------- #
PAGE_W, PAGE_H = 595.28, 841.89
CX = PAGE_W / 2
TOP = 21.0
BOTTOM_LIMIT = PAGE_H - 22.0

BOX_W = 532.5                        # header / seller / buyer / date / serial / invoice boxes
BOX_L, BOX_R = CX - BOX_W / 2, CX + BOX_W / 2
GAP = 7.0
HALF_W = (BOX_W - GAP) / 2           # seller & buyer & date & serial boxes

# Row under the party boxes, right -> left: date | gap | invoice number | gap | serial number.
# Template widths 206.8 / 6.5 / 115.5 / 7.1 / 127.8 pt, stretched to the width of the header box.
_ROW_PARTS = (206.8, 6.5, 115.5, 7.1, 127.8)
_ROW_K = BOX_W / sum(_ROW_PARTS)
DATE_W, ROW_GAP_1, INVOICE_W, ROW_GAP_2, SERIAL_W = (p * _ROW_K for p in _ROW_PARTS)

TABLE_W = 492.5
TABLE_L, TABLE_R = CX - TABLE_W / 2, CX + TABLE_W / 2
# column widths right -> left: N°, description, quantity, price, total
COL_W = (36.2, 199.3, 73.6, 84.0, 99.4)
ROW_H = 19.3

INK = "#000000"
GRID = "#000000"
HEADER_FILL = "#F2F2F2"

# Qt text-flag values (stable constants of Qt::AlignmentFlag / Qt::TextFlag).
A_LEFT, A_RIGHT, A_HCENTER, A_ABSOLUTE = 0x1, 0x2, 0x4, 0x10
A_TOP, A_VCENTER = 0x20, 0x80
T_SINGLE, T_DONTCLIP, T_WRAP, T_LTR, T_RTL = 0x100, 0x200, 0x1000, 0x20000, 0x40000


# --------------------------------------------------------------------------- #
# drawing ops
# --------------------------------------------------------------------------- #
@dataclass
class Card:
    """Rounded box with the template's soft grey edge, thin outline and drop shadow."""
    x: float
    y: float
    w: float
    h: float


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float
    fill: Optional[str] = None
    stroke: Optional[str] = None
    width: float = 0.74


@dataclass
class Line:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float = 0.74
    color: str = INK


@dataclass
class Text:
    x: float
    y: float
    w: float
    h: float
    text: str
    size: float
    bold: bool = False
    align: str = "right"          # right | left | center
    valign: str = "center"        # top | center
    underline: bool = False
    title_font: bool = False
    wrap: bool = True
    ltr: bool = False             # Arabic content is laid out right-to-left unless told otherwise


@dataclass
class Flag:
    x: float
    y: float
    w: float
    h: float


Op = Any


class Measurer(Protocol):
    def wrapped_height(self, text: str, size: float, bold: bool, width: float, title_font: bool = False) -> float: ...
    def text_width(self, text: str, size: float, bold: bool, title_font: bool = False) -> float: ...


@dataclass
class Layout:
    ops: List[Op] = field(default_factory=list)
    top: float = TOP
    bottom: float = TOP
    scale: float = 1.0
    title: str = ""

    def point(self, x: float, y: float) -> Tuple[float, float]:
        """Layout point -> page point after the global fit-to-page scale."""
        return CX + (x - CX) * self.scale, self.top + (y - self.top) * self.scale


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #
def _fit_one_line(m: Measurer, text: str, width: float, size: float, floor: float, bold: bool,
                  title_font: bool = False) -> float:
    while size > floor and m.text_width(text, size, bold, title_font) > width:
        size -= 0.5
    return size


def _fit_block(m: Measurer, lines: Sequence[str], width: float, height: float, size: float, floor: float,
               bold: bool, gap: float) -> float:
    while size > floor:
        used = sum(m.wrapped_height(t, size, bold, width) for t in lines) + gap * (len(lines) - 1)
        if used <= height:
            break
        size -= 0.5
    return size


def _party_box(ops: List[Op], m: Measurer, x: float, y: float, w: float, h: float,
               lines: List[str], size: float, gap: float = 1.5) -> None:
    pad_x, pad_y = 9.0, 8.0
    cy = y + pad_y
    for text in lines:
        th = m.wrapped_height(text, size, True, w - 2 * pad_x)
        ops.append(Text(x + pad_x, cy, w - 2 * pad_x, th, text, size, bold=True, valign="top"))
        cy += th + gap


def _info_box(ops: List[Op], m: Measurer, x: float, y: float, w: float, h: float, text: str) -> None:
    """Rounded box holding 'label : value'. One line if it fits at a readable size, otherwise the label
    above the value (what Excel does when it wraps the template's box) - a long number is never split."""
    ops.append(Card(x, y, w, h))
    pad = 12.0
    inner = w - 2 * pad
    size = _fit_one_line(m, text, inner, 14.0, 11.0, True)
    if m.text_width(text, size, True) <= inner:
        ops.append(Text(x + pad, y, inner, h, text, size, bold=True, wrap=False))
        return
    label, sep, value = text.partition(":")
    if not sep:                                              # nothing to split on: shrink as far as needed
        size = _fit_one_line(m, text, inner, 14.0, 7.0, True)
        ops.append(Text(x + pad, y, inner, h, text, size, bold=True, wrap=False))
        return
    label, value = label.strip() + " :", value.strip()
    l_size = _fit_one_line(m, label, inner, 13.0, 8.0, True)
    v_size = _fit_one_line(m, value, inner, 13.0, 7.0, True)
    l_h, v_h, gap = l_size * 1.25, v_size * 1.25, 1.0
    top = y + (h - (l_h + v_h + gap)) / 2
    ops.append(Text(x + pad, top, inner, l_h, label, l_size, bold=True, wrap=False))
    ops.append(Text(x + pad, top + l_h + gap, inner, v_h, value, v_size, bold=True, wrap=False, ltr=True))


def build_layout(data: Dict[str, Any], m: Measurer) -> Layout:
    ops: List[Op] = []
    seller, buyer = data.get("seller") or {}, data.get("customer") or {}

    # ---- header ----------------------------------------------------------------------------
    y = TOP
    head_h = 91.2
    ops.append(Card(BOX_L, y, BOX_W, head_h))
    ops.append(Flag(BOX_L + 14.6, y + 15.2, 52.0, 52.0))
    ops.append(Flag(BOX_R - 14.6 - 52.0, y + 15.2, 52.0, 52.0))
    name, sub = fmt.title_lines(seller)
    tx, tw = BOX_L + 92, BOX_W - 184
    t_size = _fit_one_line(m, name, tw, 28.0, 12.0, True, title_font=True)
    ops.append(Text(tx, y + 4, tw, 38, name, t_size, bold=True, align="center", underline=True,
                    title_font=True, wrap=False))
    if sub:
        s_size = _fit_one_line(m, sub, tw, 18.0, 10.0, True, title_font=True)
        ops.append(Text(tx, y + 44, tw, 34, sub, s_size, bold=True, align="center", title_font=True, wrap=False))
    y += head_h + 7.0

    # ---- seller (RIGHT panel: our details) / buyer (LEFT panel), as in the template ------------
    box_h = 190.0
    seller_txt = [fmt.labelled(label, value) for _k, label, value in fmt.seller_lines(seller)]
    buyer_txt = [fmt.labelled(label, value) for _k, label, value in fmt.buyer_lines(buyer)]
    inner_w, inner_h = HALF_W - 18.0, box_h - 16.0
    size = min(_fit_block(m, seller_txt, inner_w, inner_h, 13.0, 8.0, True, 1.5),
               _fit_block(m, buyer_txt, inner_w, inner_h, 13.0, 8.0, True, 1.5))
    left_x, right_x = BOX_L, BOX_R - HALF_W
    ops.append(Card(right_x, y, HALF_W, box_h))
    ops.append(Card(left_x, y, HALF_W, box_h))
    _party_box(ops, m, right_x, y, HALF_W, box_h, seller_txt, size)
    _party_box(ops, m, left_x, y, HALF_W, box_h, buyer_txt, size)
    y += box_h + 6.0

    # ---- one row of three boxes, right -> left: date | invoice number | serial number ----------
    small_h = 44.2
    date_x = BOX_R - DATE_W
    invoice_x = date_x - ROW_GAP_1 - INVOICE_W
    serial_x = invoice_x - ROW_GAP_2 - SERIAL_W
    _info_box(ops, m, date_x, y, DATE_W, small_h, fmt.date_line(data))
    _info_box(ops, m, invoice_x, y, INVOICE_W, small_h, fmt.invoice_line(data))
    _info_box(ops, m, serial_x, y, SERIAL_W, small_h, fmt.serial_line(data))
    y += small_h + 21.0                                       # the template leaves about one row here

    # ---- items table -----------------------------------------------------------------------
    items = list(data.get("items") or [])
    n_rows = max(TEMPLATE_ITEM_ROWS, len(items))
    xs = [TABLE_R]                                   # column edges, right -> left
    for w in COL_W:
        xs.append(xs[-1] - w)
    heads = fmt.COLUMN_HEADERS
    ops.append(Rect(xs[-1], y, TABLE_W, ROW_H, fill=HEADER_FILL))
    for i, text in enumerate(heads):
        ops.append(Text(xs[i + 1], y, COL_W[i], ROW_H, text, 11.0, bold=i in (0, 4), align="center", wrap=False,
                        ltr=(i == 0)))
    table_top = y
    row_tops: List[float] = [y]
    y += ROW_H

    for r in range(n_rows):
        item = items[r] if r < len(items) else None
        desc = (item or {}).get("description", "")
        d_size = 11.5
        desc_w = COL_W[1] - 10
        row_h = ROW_H
        if desc:
            while d_size > 8.5 and m.wrapped_height(desc, d_size, False, desc_w) > 3 * d_size * 1.25:
                d_size -= 0.5
            row_h = max(ROW_H, m.wrapped_height(desc, d_size, False, desc_w) + 6.0)
        cells = [f"{r + 1:02d}", desc,
                 fmt.fmt_qty(item["quantity"]) if item else "",
                 fmt.fmt_number(item["unit_price"]) if item else "",
                 fmt.fmt_number(item["line_total"]) if item else "-"]
        for i, text in enumerate(cells):
            if not text:
                continue
            if i == 1:
                ops.append(Text(xs[i + 1] + 4, y, COL_W[i] - 8, row_h, text, d_size, align="right"))
            else:
                ops.append(Text(xs[i + 1], y, COL_W[i], row_h, text, 11.0, bold=True, align="center",
                                wrap=False, ltr=(i != 4 or text == "-")))
        y += row_h
        row_tops.append(y)
    items_bottom = y

    # totals: label spans the quantity+price columns, value sits under the total column
    label_l, label_r = xs[4], xs[2]
    totals = [(fmt.LABEL_HT, fmt.fmt_money(data["subtotal"])),
              (fmt.vat_label(data), fmt.fmt_money(data["vat_amount"])),
              (fmt.LABEL_TTC, fmt.fmt_money(data["total"]))]
    for text, value in totals:
        ops.append(Text(label_l, y, label_r - label_l, ROW_H, text, 11.0, bold=True, align="center", wrap=False,
                        ltr=True))
        ops.append(Text(xs[5], y, COL_W[4], ROW_H, value, 11.0, bold=True, align="center", wrap=False))
        y += ROW_H
    totals_bottom = y

    # ---- table grid ------------------------------------------------------------------------
    for t in row_tops:                                         # horizontal rules over the full width
        ops.append(Line(xs[-1], t, xs[0], t, 0.74, GRID))
    ops.append(Line(xs[-1], items_bottom, xs[0], items_bottom, 0.74, GRID))
    for i, x in enumerate(xs):                                 # verticals: full height inside the items
        ops.append(Line(x, table_top, x, items_bottom, 0.74, GRID))
    for k in range(1, 4):                                      # totals rows: only label + value cells
        ty = items_bottom + k * ROW_H
        ops.append(Line(xs[5], ty, xs[2], ty, 0.74, GRID))
    for x in (xs[5], xs[4], xs[2]):
        ops.append(Line(x, items_bottom, x, totals_bottom, 0.74, GRID))

    # ---- amount in words (the template leaves one empty row after the totals) -------------
    y = totals_bottom + ROW_H
    title_w = m.text_width(fmt.WORDS_TITLE, 11.0, True) + 4
    ops.append(Text(TABLE_R - title_w, y, title_w, ROW_H, fmt.WORDS_TITLE, 11.0, bold=True, wrap=False,
                    underline=True))
    y += ROW_H
    try:
        words = amount_in_words(data["total"])
    except OverflowError:
        words = ""
    w_size = 12.0
    words_h = 44.2
    while w_size > 9.0 and m.wrapped_height(words, w_size, False, TABLE_W - 12) > words_h - 4:
        w_size -= 0.5
    ops.append(Text(TABLE_L + 6, y, TABLE_W - 12, words_h, words, w_size, align="center"))
    y += words_h

    # ---- payment ---------------------------------------------------------------------------
    cursor = TABLE_R
    label_w = m.text_width(fmt.PAYMENT_TITLE, 11.0, True) + 2
    ops.append(Text(cursor - label_w, y, label_w, ROW_H, fmt.PAYMENT_TITLE, 11.0, bold=True, wrap=False))
    cursor -= label_w + 16
    chosen = data.get("payment_method", "cash")
    box = 15.0
    for key, label in fmt.payment_options():                    # right -> left: box, then its word (as in the template)
        word_w = m.text_width(label, 11.0, False) + 2
        bx = cursor - box
        by = y + (ROW_H - box) / 2
        ops.append(Rect(bx, by, box, box, fill="#FFFFFF", stroke=INK, width=0.9))
        if key == chosen:                                       # a drawn tick: no symbol font needed
            ops.append(Line(bx + 3.2, by + 7.8, bx + 6.2, by + 11.2, 1.6))
            ops.append(Line(bx + 6.2, by + 11.2, bx + box - 3.2, by + 3.6, 1.6))
        cursor = bx - 5
        ops.append(Text(cursor - word_w, y, word_w, ROW_H, label, 11.0, wrap=False))
        cursor -= word_w + 26
    y += ROW_H
    ops.append(Line(TABLE_L, y, TABLE_R, y, 0.74, GRID))
    ops.append(Text(TABLE_L, y + 1, 90, 16, fmt.SIGNATURE, 10.5, align="left", wrap=False))
    bottom = y + 18.0

    layout = Layout(ops=ops, top=TOP, bottom=bottom, title=fmt.document_title(data))
    height = bottom - TOP
    layout.scale = min(1.0, (BOTTOM_LIMIT - TOP) / height) if height > 0 else 1.0
    return layout


# --------------------------------------------------------------------------- #
# Qt side
# --------------------------------------------------------------------------- #
_BODY_FAMILIES = ("Arial", "Tahoma", "Segoe UI", "Noto Sans Arabic", "Noto Naskh Arabic", "DejaVu Sans",
                  "Liberation Sans")
_TITLE_FAMILIES = ("Andalus", "Traditional Arabic", "Simplified Arabic") + _BODY_FAMILIES
_qt_app = None


class PdfError(RuntimeError):
    pass


def ensure_qt_app():
    """Fonts need a Qt application object. Reuse the GUI's; create a headless one for scripts/tests."""
    global _qt_app
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance()
    if app is None:
        _qt_app = app = QGuiApplication(["facturedz"])
    return app


def _installed(candidates: Sequence[str]) -> List[str]:
    from PySide6.QtGui import QFontDatabase
    have = {f.casefold() for f in QFontDatabase.families()}
    found = [c for c in candidates if c.casefold() in have]
    return found or list(candidates[-2:])


class QtMeasurer:
    """Text sizes from Qt font metrics at the PDF's own resolution (so wrapping matches what is drawn)."""

    def __init__(self, device):
        self.device = device
        self.k = device.resolution() / 72.0
        self.body = _installed(_BODY_FAMILIES)
        self.title = _installed(_TITLE_FAMILIES)

    def font(self, size: float, bold: bool, underline: bool = False, title_font: bool = False):
        from PySide6.QtGui import QFont
        font = QFont()
        font.setFamilies(self.title if title_font else self.body)
        font.setPointSizeF(size)
        font.setBold(bold)
        font.setUnderline(underline)
        return font

    def wrapped_height(self, text, size, bold, width, title_font=False) -> float:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QFontMetricsF
        fm = QFontMetricsF(self.font(size, bold, title_font=title_font), self.device)
        rect = fm.boundingRect(QRectF(0, 0, width * self.k, 1.0e7), A_ABSOLUTE | A_RIGHT | T_WRAP | T_RTL, text or " ")
        return rect.height() / self.k

    def text_width(self, text, size, bold, title_font=False) -> float:
        from PySide6.QtGui import QFontMetricsF
        fm = QFontMetricsF(self.font(size, bold, title_font=title_font), self.device)
        return fm.horizontalAdvance(text) / self.k


def _text_flags(op: Text) -> int:
    flags = A_ABSOLUTE | T_DONTCLIP                 # never cut a glyph if font metrics differ slightly
    flags |= {"right": A_RIGHT, "left": A_LEFT, "center": A_HCENTER}[op.align]
    flags |= A_TOP if op.valign == "top" else A_VCENTER
    flags |= T_LTR if op.ltr else T_RTL
    flags |= T_WRAP if op.wrap else T_SINGLE
    return flags


def _flag_image(template: Optional[Path]):
    """The round flag artwork stored inside the Excel template (None if unavailable)."""
    if not template:
        return None
    try:
        with zipfile.ZipFile(template) as zf:
            media = sorted(n for n in zf.namelist() if n.startswith("xl/media/") and n.lower().endswith(
                (".jpg", ".jpeg", ".png")))
            if not media:
                return None
            from PySide6.QtGui import QImage
            image = QImage()
            if image.loadFromData(zf.read(media[0])):
                return image
    except (OSError, zipfile.BadZipFile, KeyError):
        log.warning("flag artwork unavailable", exc_info=True)
    return None


def _paint(layout: Layout, writer, measurer: QtMeasurer, flag) -> None:
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen

    painter = QPainter(writer)
    if not painter.isActive():
        raise PdfError("تعذر بدء الرسم على ملف PDF.")
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        k, s = measurer.k, layout.scale

        def px(x: float, y: float) -> Tuple[float, float]:
            ax, ay = layout.point(x, y)
            return ax * k, ay * k

        def rect(x, y, w, h) -> QRectF:
            ax, ay = px(x, y)
            return QRectF(ax, ay, w * s * k, h * s * k)

        def pen(color: str, width: float) -> QPen:
            return QPen(QColor(color), max(width * s * k, 1.0))

        no_pen = QPen(Qt.PenStyle.NoPen)

        def fill_rounded(r: QRectF, radius: float, color: QColor) -> None:
            painter.setPen(no_pen)
            painter.setBrush(QBrush(color))
            painter.drawRoundedRect(r, radius, radius)

        for op in layout.ops:
            if isinstance(op, Card):
                r = rect(op.x, op.y, op.w, op.h)
                radius = 12.0 * s * k
                for i, alpha in enumerate((26, 18, 10)):          # soft drop shadow
                    grow = (3 - i) * 0.7 * s * k
                    fill_rounded(QRectF(r.x() + 1.6 * s * k - grow, r.y() + 2.2 * s * k - grow,
                                        r.width() + 2 * grow, r.height() + 2 * grow), radius + grow,
                                 QColor(0, 0, 0, alpha))
                steps = 14                                       # grey edge fading to white, like the template
                depth = min(r.height() / 2.0, 15.0 * s * k)
                for i in range(steps):
                    t = i / (steps - 1)
                    shade = int(191 + (255 - 191) * t)
                    inset = depth * (i / steps)
                    fill_rounded(QRectF(r.x() + inset, r.y() + inset, r.width() - 2 * inset,
                                        r.height() - 2 * inset), max(radius - inset, 1.0),
                                 QColor(shade, shade, shade))
                painter.setPen(pen(INK, 0.6))
                painter.setBrush(QBrush())
                painter.drawRoundedRect(r, radius, radius)
            elif isinstance(op, Rect):
                painter.setPen(pen(op.stroke, op.width) if op.stroke else no_pen)
                painter.setBrush(QBrush(QColor(op.fill)) if op.fill else QBrush())
                painter.drawRect(rect(op.x, op.y, op.w, op.h))
            elif isinstance(op, Line):
                painter.setPen(pen(op.color, op.width))
                painter.drawLine(QPointF(*px(op.x1, op.y1)), QPointF(*px(op.x2, op.y2)))
            elif isinstance(op, Flag):
                if flag is not None:
                    r = rect(op.x, op.y, op.w, op.h)
                    path = QPainterPath()
                    path.addEllipse(r)
                    painter.save()
                    painter.setClipPath(path)
                    painter.drawImage(r, flag)
                    painter.restore()
            elif isinstance(op, Text):
                painter.setFont(measurer.font(op.size * s, op.bold, op.underline, op.title_font))
                painter.setPen(QPen(QColor(INK)))
                painter.setBrush(QBrush())
                painter.drawText(rect(op.x, op.y, op.w, op.h), _text_flags(op), op.text)
    finally:
        painter.end()


def render_invoice_pdf(data: Dict[str, Any], path: Path, template: Optional[Path] = None) -> Path:
    """Write the invoice ``data`` (canonical dict from service.build_invoice) to ``path`` as an A4 PDF."""
    ensure_qt_app()
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
    writer.setResolution(600)
    writer.setCreator("Facture DZ")
    measurer = QtMeasurer(writer)
    layout = build_layout(data, measurer)
    writer.setTitle(layout.title)
    _paint(layout, writer, measurer, _flag_image(template))
    del measurer, writer                                  # release the file handle before it is copied
    if not path.exists() or path.stat().st_size < 1500:
        raise PdfError("لم يُنتج ملف PDF صالح.")
    return path
