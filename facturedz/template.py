# -*- coding: utf-8 -*-
"""Fill the user's Excel invoice template (.xlsm) by editing the package XML directly.

Why not Excel automation or openpyxl?
  * openpyxl drops drawings (flag artwork, grouped text boxes) -> the design would be lost.
  * Excel COM text replacement flattens each text box's formatting and its Symbol-font placeholders.
  * Editing the XML needs no Excel at all, so the editable .xlsm is always produced.

Everything that is not a value we set (styles, borders, images, macros, print setup) is carried
over byte-for-byte from the template. The wording of every label comes from ``invoice_format`` -
the same module the PDF renderer reads - so the Excel file and the PDF always say the same thing.

Template contract (checked by ``validate_template``): one sheet whose table header sits in column C
("N°"), followed by the item rows (C=N°, D=description, E=quantity, F=unit price, G=line total), the
"TOTAL HT" row, the "أوقف هذا الوصل ..." title, the amount-in-words row and the payment row, plus a
drawing whose text boxes are recognised by their *labels*. Row numbers are **detected** from those
anchors (``detect_layout``), never hard-coded, so inserting or deleting rows in the template cannot
shift the export into the wrong cells.
"""
from __future__ import annotations

import copy
import math
import os
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from .amounts import amount_in_words, plain_number
from .config import MAX_ITEMS, TEMPLATE_ITEM_ROWS
from .invoice_format import (COLUMN_HEADERS, CURRENCY, LABEL_HT, LABEL_TTC, buyer_lines, date_line,
                             invoice_line, labelled, payment_options, seller_lines, serial_line, title_lines,
                             vat_label)
from .textnorm import normalize_ar, xml_safe

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# Payment check-boxes are four small empty rectangles in the template (drawing shape ids).
PAYMENT_BOX_IDS = {"cash": "4", "cheque": "3", "term": "9", "bank": "2"}
TICK = "\u2713"


class TemplateError(Exception):
    """The template no longer matches what the exporter expects (message is Arabic)."""


# --------------------------------------------------------------------------- #
# XML plumbing (keeps namespace declarations Excel insists on)                #
# --------------------------------------------------------------------------- #
_XMLNS_RE = re.compile(r'xmlns(?::([\w.\-]+))?="([^"]*)"')
_ROOT_TAG_RE = re.compile(r"<(?!\?)(?!!)[^>]*>", re.S)


def _parse(xml_bytes: bytes):
    text = xml_bytes.decode("utf-8-sig")
    for prefix, uri in _XMLNS_RE.findall(text):
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            pass
    return ET.fromstring(text.encode("utf-8")), text


def _serialize(root, original_text: str) -> bytes:
    """Serialise and re-declare every namespace the original root declared.

    ElementTree drops declarations that are only referenced from attribute *values*
    (mc:Ignorable="x14ac xr ...", mc:Choice Requires="x15"); Excel rejects such files.
    """
    body = ET.tostring(root, encoding="unicode")
    orig_root = _ROOT_TAG_RE.search(original_text).group(0)
    new_root_match = _ROOT_TAG_RE.search(body)
    new_root = new_root_match.group(0)
    missing = [f'xmlns{":" + p if p else ""}="{u}"' for p, u in _XMLNS_RE.findall(orig_root)
               if f'xmlns{":" + p if p else ""}="' not in new_root]
    if missing:
        insert_at = len(new_root) - (2 if new_root.endswith("/>") else 1)
        patched = new_root[:insert_at] + " " + " ".join(missing) + new_root[insert_at:]
        body = body[:new_root_match.start()] + patched + body[new_root_match.end():]
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + body).encode("utf-8")


def _read_zip(path: Path) -> "tuple[List[zipfile.ZipInfo], Dict[str, bytes]]":
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            return infos, {i.filename: zf.read(i.filename) for i in infos}
    except (zipfile.BadZipFile, OSError) as exc:
        raise TemplateError(f"ملف قالب Excel تالف أو غير موجود: {path}") from exc


def _write_zip(path: Path, infos, parts: Dict[str, bytes]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
            for info in infos:
                if info.filename not in parts:
                    continue
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                zf.writestr(zi, parts[info.filename])
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _locate_parts(parts: Dict[str, bytes]) -> "tuple[str, str]":
    """Return (sheet part name, drawing part name) following the package relationships."""
    try:
        wb_root, _ = _parse(parts["xl/workbook.xml"])
        rels_root, _ = _parse(parts["xl/_rels/workbook.xml.rels"])
        first_sheet = wb_root.find(f"{M}sheets").findall(f"{M}sheet")[0]
        rid = first_sheet.get(f"{{{R_NS}}}id")
        target = next(r.get("Target") for r in rels_root.findall(f"{REL}Relationship") if r.get("Id") == rid)
        sheet_part = posixpath.normpath(posixpath.join("xl", target))
        rels_name = posixpath.join(posixpath.dirname(sheet_part), "_rels", posixpath.basename(sheet_part) + ".rels")
        srels, _ = _parse(parts[rels_name])
        drawing_target = next(r.get("Target") for r in srels.findall(f"{REL}Relationship")
                              if r.get("Type", "").endswith("/drawing"))
        drawing_part = posixpath.normpath(posixpath.join(posixpath.dirname(sheet_part), drawing_target))
        if sheet_part not in parts or drawing_part not in parts:
            raise KeyError(sheet_part)
        return sheet_part, drawing_part
    except (KeyError, StopIteration, IndexError, AttributeError, ET.ParseError) as exc:
        raise TemplateError("بنية قالب Excel غير متوقعة (الورقة أو الرسومات غير موجودة).") from exc


# --------------------------------------------------------------------------- #
# worksheet editing                                                           #
# --------------------------------------------------------------------------- #
def _split_ref(ref: str):
    m = re.fullmatch(r"([A-Z]+)(\d+)", ref)
    return m.group(1), int(m.group(2))


def _col_num(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _shared_strings(parts: Dict[str, bytes]) -> List[str]:
    if "xl/sharedStrings.xml" not in parts:
        return []
    root, _ = _parse(parts["xl/sharedStrings.xml"])
    return ["".join(t.text or "" for t in si.iter(f"{M}t")) for si in root.findall(f"{M}si")]


class SheetEditor:
    def __init__(self, root, shared: Optional[List[str]] = None):
        self.root = root
        self.shared = shared or []
        self.sheet_data = root.find(f"{M}sheetData")
        if self.sheet_data is None:
            raise TemplateError("ورقة القالب لا تحتوي على بيانات.")
        self.rows = {int(r.get("r")): r for r in self.sheet_data.findall(f"{M}row")}

    # -- cells ---------------------------------------------------------- #
    def has_cell(self, ref: str) -> bool:
        col, row = _split_ref(ref)
        return row in self.rows and any(c.get("r") == ref for c in self.rows[row].findall(f"{M}c"))

    def text(self, ref: str) -> str:
        """Plain text of a cell (shared string, inline string or raw value); '' if empty/missing."""
        col, rownum = _split_ref(ref)
        row = self.rows.get(rownum)
        if row is None:
            return ""
        for c in row.findall(f"{M}c"):
            if c.get("r") != ref:
                continue
            if c.get("t") == "inlineStr":
                return "".join(t.text or "" for t in c.iter(f"{M}t"))
            v = c.find(f"{M}v")
            if v is None or v.text is None:
                return ""
            if c.get("t") == "s":
                try:
                    return self.shared[int(v.text)]
                except (ValueError, IndexError):
                    return ""
            return v.text
        return ""

    def cell(self, ref: str):
        col, rownum = _split_ref(ref)
        row = self.rows.get(rownum)
        if row is None:
            raise TemplateError(f"القالب لا يحتوي على الصف {rownum}.")
        existing = row.findall(f"{M}c")
        for c in existing:
            if c.get("r") == ref:
                return c
        new = ET.Element(f"{M}c", {"r": ref})
        position = len(existing)
        for idx, c in enumerate(existing):
            if _col_num(_split_ref(c.get("r"))[0]) > _col_num(col):
                position = idx
                break
        row.insert(list(row).index(existing[position]) if position < len(existing) else len(row), new)
        return new

    @staticmethod
    def _reset(c) -> None:
        for child in list(c):
            c.remove(child)
        c.attrib.pop("t", None)

    def clear(self, ref: str) -> None:
        self._reset(self.cell(ref))

    def set_str(self, ref: str, text: str) -> None:
        c = self.cell(ref)
        self._reset(c)
        c.set("t", "inlineStr")
        is_el = ET.SubElement(c, f"{M}is")
        t = ET.SubElement(is_el, f"{M}t")
        t.set(XML_SPACE, "preserve")
        t.text = xml_safe(text)

    def set_num(self, ref: str, value: Decimal) -> None:
        c = self.cell(ref)
        self._reset(c)
        ET.SubElement(c, f"{M}v").text = plain_number(value)

    def set_formula(self, ref: str, formula: str, cached: Decimal) -> None:
        c = self.cell(ref)
        self._reset(c)
        ET.SubElement(c, f"{M}f").text = formula
        ET.SubElement(c, f"{M}v").text = plain_number(cached)

    # -- rows ----------------------------------------------------------- #
    @staticmethod
    def _renumber(row, new_number: int) -> None:
        row.set("r", str(new_number))
        for c in row.findall(f"{M}c"):
            col, _ = _split_ref(c.get("r"))
            c.set("r", f"{col}{new_number}")

    def insert_rows_after(self, after_row: int, count: int) -> None:
        """Insert ``count`` copies of ``after_row`` right below it, shifting later rows down."""
        if count <= 0:
            return
        for number in sorted((n for n in self.rows if n > after_row), reverse=True):
            self._renumber(self.rows[number], number + count)
            self.rows[number + count] = self.rows.pop(number)
        source = self.rows[after_row]
        anchor_index = list(self.sheet_data).index(source)
        for k in range(1, count + 1):
            clone = copy.deepcopy(source)
            self._renumber(clone, after_row + k)
            for c in clone.findall(f"{M}c"):
                self._reset(c) if c.find(f"{M}f") is not None or c.get("t") == "s" else None
            self.sheet_data.insert(anchor_index + k, clone)
            self.rows[after_row + k] = clone
        # merged ranges, dimension
        merges = self.root.find(f"{M}mergeCells")
        if merges is not None:
            for mc in merges.findall(f"{M}mergeCell"):
                mc.set("ref", _shift_range(mc.get("ref"), after_row, count))
        dim = self.root.find(f"{M}dimension")
        if dim is not None:
            dim.set("ref", _shift_range(dim.get("ref"), after_row, count))

    def set_row_height(self, rownum: int, height: float) -> None:
        row = self.rows[rownum]
        row.set("ht", f"{height:.2f}".rstrip("0").rstrip("."))
        row.set("customHeight", "1")


def _shift_range(ref: str, after_row: int, delta: int) -> str:
    def bump(m):
        row = int(m.group(2))
        return f"{m.group(1)}{row + delta if row > after_row else row}"
    return re.sub(r"(\$?[A-Z]+\$?)(\d+)", bump, ref)


# --------------------------------------------------------------------------- #
# row layout detection                                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SheetLayout:
    """Where the table and everything under it sit in *this* template (1-based row numbers)."""
    header_row: int      # "N° | التعيين | ..." table header
    first_item_row: int
    item_rows: int       # item rows physically present in the template
    totals_row: int      # "TOTAL HT"  (TVA = +1, TTC = +2)
    title_row: int       # "أوقف هذا الوصل عن مبلغ و قدره :"
    words_row: int       # amount in words (the template's MontantDA() macro cell)
    payment_row: int     # "طريقة الدفع : نقدا / شيك / لاجل / بنك" (the four tick boxes float over it)

    @property
    def last_item_row(self) -> int:
        return self.first_item_row + self.item_rows - 1


_ITEM_NO = re.compile(r"\d{1,3}")


def detect_layout(sheet: SheetEditor) -> SheetLayout:
    """Find the table and the rows below it from what the cells say. Raises TemplateError."""
    header = next((n for n in sorted(sheet.rows)
                   if sheet.text(f"C{n}").strip().upper() == "N°" and sheet.has_cell(f"G{n}")), None)
    if header is None:
        raise TemplateError("لم أجد رأس جدول السلع (العمود C «N°») في القالب.")
    first = header + 1
    count = 0
    while _ITEM_NO.fullmatch(sheet.text(f"C{first + count}").strip()):
        count += 1
    if count == 0:
        raise TemplateError("لا توجد أسطر سلع (01، 02، ...) تحت رأس الجدول في القالب.")
    totals = first + count
    if "TOTAL HT" not in sheet.text(f"E{totals}").upper():
        raise TemplateError(f"لم أجد «TOTAL HT» مباشرة تحت أسطر السلع (الخلية E{totals}).")
    needle = normalize_ar("أوقف")
    title = next((n for n in range(totals + 3, totals + 9) if needle in normalize_ar(sheet.text(f"C{n}"))), None)
    if title is None:
        raise TemplateError("لم أجد عنوان «أوقف هذا الوصل عن مبلغ و قدره» في القالب.")
    for ref in (f"G{totals}", f"G{totals + 1}", f"G{totals + 2}", f"C{title + 1}", f"C{title + 2}"):
        if not sheet.has_cell(ref):
            raise TemplateError(f"الخلية {ref} غير موجودة في القالب.")
    return SheetLayout(header, first, count, totals, title, title + 1, title + 2)


# --------------------------------------------------------------------------- #
# drawing editing                                                             #
# --------------------------------------------------------------------------- #
def _shape_id(sp) -> str:
    c = sp.find(f".//{XDR}cNvPr")
    return c.get("id") if c is not None else ""


def _shape_text(sp) -> str:
    return "".join(t.text or "" for t in sp.iter(f"{A}t"))


def _paragraph_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(f"{A}t"))


def _label_of(p) -> str:
    """Raw label part of a 'label : value' paragraph (keeps the template's own tatweel styling)."""
    raw = re.sub("[\ue000-\uf8ff]", "", _paragraph_text(p))
    return raw.split(":")[0].strip()


_FIELD_RULES = [  # (substring in normalised label, data key) — first match wins
    ("المطلوب", "name"),
    ("عنوان", "commercial_title"),
    ("السجل", "rc"),
    ("الجباي", "nif"),
    ("الاحصا", "nis"),
    ("الحساب", "bank_account"),
    ("الماده", "article_no"),
    ("الهاتف", "phone"),
    ("النشاط", "activity"),
]
_FIELD_RULES = [(normalize_ar(k), v) for k, v in _FIELD_RULES]


def _field_for_label(label: str) -> Optional[str]:
    n = normalize_ar(label)
    for needle, key in _FIELD_RULES:
        if needle in n:
            return key
    return None


def classify_shapes(root) -> Dict[str, object]:
    """Find the template's text boxes by what they say. Returns role -> sp element."""
    parents = {c: p for p in root.iter() for c in p}
    roles: Dict[str, object] = {}
    for sp in root.iter(f"{XDR}sp"):
        if sp.find(f"{XDR}txBody") is None:
            continue
        text = normalize_ar(_shape_text(sp))
        if not text:
            continue
        if "المطلوب" in text:
            roles.setdefault("buyer", sp)
        elif "الهاتف" in text and "السجل" in text:
            roles.setdefault("seller", sp)
        elif "التسلسلي" in text:
            roles.setdefault("serial", sp)
        elif "رقم الفاتوره" in text:
            roles.setdefault("invoice", sp)
        elif re.search(r"(^|\s)في\s*:", text) and len(text) < 40:
            roles.setdefault("date", sp)
        else:
            parent = parents.get(sp)
            if parent is not None and parent.tag == f"{XDR}grpSp" and parent.find(f"{XDR}pic") is not None:
                roles.setdefault("header", sp)
    return roles


def _emu(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _shape_box_pt(sp):
    ext = sp.find(f"{XDR}spPr/{A}xfrm/{A}ext")
    body = sp.find(f"{XDR}txBody/{A}bodyPr")
    attrs = body.attrib if body is not None else {}
    w = _emu(ext.get("cx") if ext is not None else None, 3_000_000)
    h = _emu(ext.get("cy") if ext is not None else None, 1_000_000)
    avail_w = (w - _emu(attrs.get("lIns"), 91440) - _emu(attrs.get("rIns"), 91440)) / 12700
    avail_h = (h - _emu(attrs.get("tIns"), 45720) - _emu(attrs.get("bIns"), 45720)) / 12700
    return max(avail_w, 20.0), max(avail_h, 10.0)


def _em(ch: str) -> float:
    # Deliberately generous (bold Arabic in Arial/Calibri/DejaVu): better to shrink 1pt than clip a line.
    if ch == " ":
        return 0.30
    if ch.isdigit() or (ch.isascii() and ch.isalpha()):
        return 0.62
    if "\u0600" <= ch <= "\u06ff":
        return 0.55
    return 0.45


def _width_pt(text: str, size_pt: float) -> float:
    return sum(_em(c) for c in text) * size_pt * 1.05


def _spacing_pt(sp) -> float:
    """Total fixed space (pt) the paragraphs add around their lines (spcBef + spcAft)."""
    total = 0.0
    for p in sp.findall(f"{XDR}txBody/{A}p"):
        for tag in ("spcBef", "spcAft"):
            node = p.find(f"{A}pPr/{A}{tag}/{A}spcPts")
            if node is not None:
                total += _emu(node.get("val"), 0) / 100
    return total


def _fit_size(sp, texts: List[str], base: int, min_size: int = 900, extra_lines: int = 0) -> int:
    """Largest font size (1/100 pt) <= base at which the estimated wrapped text fits the box."""
    avail_w, avail_h = _shape_box_pt(sp)
    avail_h -= _spacing_pt(sp)
    size = base
    while size > min_size:
        pt = size / 100
        lines = extra_lines + sum(max(1, math.ceil(_width_pt(t, pt) / avail_w)) for t in texts)
        if lines * pt * 1.2 <= avail_h:
            break
        size -= 50
    return size


def _set_paragraph(p, text: str, size: Optional[int] = None) -> None:
    """Replace a paragraph's content with one run that inherits the paragraph's first run style."""
    runs = p.findall(f"{A}r")
    if runs:
        donor = runs[0]
        for extra in runs[1:]:
            p.remove(extra)
    else:
        donor = ET.Element(f"{A}r")
        rpr = ET.SubElement(donor, f"{A}rPr")
        end = p.find(f"{A}endParaRPr")
        if end is not None:
            rpr.attrib.update({k: v for k, v in end.attrib.items() if k in ("lang", "sz", "b")})
            for child in end:
                rpr.append(copy.deepcopy(child))
        ET.SubElement(donor, f"{A}t")
        p.insert(list(p).index(end) if end is not None else len(p), donor)
    rpr = donor.find(f"{A}rPr")
    if rpr is not None:
        for sym in rpr.findall(f"{A}sym"):
            rpr.remove(sym)
        if size:
            rpr.set("sz", str(size))
    t = donor.find(f"{A}t")
    if t is None:
        t = ET.SubElement(donor, f"{A}t")
    t.text = xml_safe(text)
    if size:
        end = p.find(f"{A}endParaRPr")
        if end is not None:
            end.set("sz", str(size))


def _fill_labelled_box(sp, lines, role: str) -> None:
    """Fill a 'label : value' box. Paragraphs are matched to fields by their *old* label (so the user
    may reword/move them), then rewritten with the canonical label and order from invoice_format."""
    paragraphs = sp.findall(f"{XDR}txBody/{A}p")
    known = {key for key, _label, _value in lines}
    slots, seen = [], set()
    for p in paragraphs:
        label = _label_of(p)
        key = _field_for_label(label) if label else None
        if key is None or key in seen or key not in known:
            continue
        seen.add(key)
        slots.append(p)
    if not slots:
        raise TemplateError(f"لم أجد حقول «{role}» داخل القالب.")
    texts = [labelled(label, xml_safe(value)) for key, label, value in lines if key in seen]
    base = max((_emu(r.get("sz"), 1400) for p in slots for r in p.iter(f"{A}rPr")), default=1400)
    untouched = len(paragraphs) - len(slots)          # e.g. the template's trailing empty paragraph
    size = _fit_size(sp, texts, base, extra_lines=untouched)
    for p, text in zip(slots, texts):
        _set_paragraph(p, text, size if size < base else None)


def _fill_single(sp, text: str, role: str, min_size: int = 1000) -> None:
    paragraphs = sp.findall(f"{XDR}txBody/{A}p")
    if not paragraphs:
        raise TemplateError(f"الحقل «{role}» فارغ في القالب.")
    first = paragraphs[0]
    base = max((_emu(r.get("sz"), 1600) for r in first.iter(f"{A}rPr")), default=1600)
    size = _fit_size(sp, [text], base, min_size)
    # Wrapping may spread the text over two lines, but a long number must never be split in the middle:
    # shrink until the longest unbreakable word fits the box width on its own.
    avail_w, _h = _shape_box_pt(sp)
    longest = max(text.split(), key=len, default="")
    while size > min_size and _width_pt(longest, size / 100) > avail_w:
        size -= 50
    _set_paragraph(first, text, size if size < base else None)


def _fill_header(sp, name: str, address: str) -> None:
    paragraphs = sp.findall(f"{XDR}txBody/{A}p")
    if len(paragraphs) < 1:
        raise TemplateError("لم أجد ترويسة المؤسسة في القالب.")
    avail_w, _ = _shape_box_pt(sp)
    pairs = [(paragraphs[0], name, 1400)]
    if len(paragraphs) > 1:
        pairs.append((paragraphs[1], address, 1000))
    for p, text, floor in pairs:
        base = max((_emu(r.get("sz"), 2800) for r in p.iter(f"{A}rPr")), default=2800)
        size = base
        while size > floor and _width_pt(text, size / 100) > avail_w:
            size -= 100
        _set_paragraph(p, text, size if size < base else None)


def _tick_box(sp, ticked: bool) -> None:
    body = sp.find(f"{XDR}txBody")
    bp = body.find(f"{A}bodyPr")
    bp.attrib.update({"lIns": "0", "tIns": "0", "rIns": "0", "bIns": "0", "anchor": "ctr", "wrap": "none"})
    for p in body.findall(f"{A}p"):
        body.remove(p)
    p = ET.SubElement(body, f"{A}p")
    ET.SubElement(p, f"{A}pPr", {"algn": "ctr"})
    if ticked:
        r = ET.SubElement(p, f"{A}r")
        rpr = ET.SubElement(r, f"{A}rPr", {"lang": "en-US", "sz": "1200", "b": "1"})
        fill = ET.SubElement(rpr, f"{A}solidFill")
        ET.SubElement(fill, f"{A}srgbClr", {"val": "000000"})
        for tag in ("latin", "ea", "cs"):
            ET.SubElement(rpr, f"{A}{tag}", {"typeface": "Segoe UI Symbol"})
        ET.SubElement(r, f"{A}t").text = TICK
    else:
        ET.SubElement(p, f"{A}endParaRPr", {"lang": "en-US", "sz": "1200"})


def _shift_anchors(root, from_row0: int, delta: int) -> None:
    for anchor in root:
        for tag in ("from", "to"):
            node = anchor.find(f"{XDR}{tag}")
            if node is None:
                continue
            row = node.find(f"{XDR}row")
            if row is not None and int(row.text) >= from_row0:
                row.text = str(int(row.text) + delta)


# --------------------------------------------------------------------------- #
# public API                                                                  #
# --------------------------------------------------------------------------- #
def validate_template(template: Path) -> List[str]:
    """Return a list of human-readable problems ('' list = template is usable)."""
    problems: List[str] = []
    try:
        infos, parts = _read_zip(Path(template))
        sheet_part, drawing_part = _locate_parts(parts)
        sheet = SheetEditor(_parse(parts[sheet_part])[0], _shared_strings(parts))
        drawing, _ = _parse(parts[drawing_part])
    except TemplateError as exc:
        return [str(exc)]
    except ET.ParseError:
        return ["ملف XML داخل القالب تالف."]
    try:
        layout = detect_layout(sheet)
    except TemplateError as exc:
        problems.append(str(exc))
    else:
        if layout.item_rows != TEMPLATE_ITEM_ROWS:
            problems.append(f"عدد أسطر السلع في القالب {layout.item_rows} والمتوقع {TEMPLATE_ITEM_ROWS}.")
        for col in "CDEFG":
            if not sheet.has_cell(f"{col}{layout.first_item_row}"):
                problems.append(f"الخلية {col}{layout.first_item_row} غير موجودة في القالب.")
    roles = classify_shapes(drawing)
    names = {"seller": "بيانات البائع", "buyer": "بيانات المشتري", "serial": "الرقم التسلسلي",
             "invoice": "رقم الفاتورة", "date": "التاريخ", "header": "ترويسة المؤسسة"}
    for role, label in names.items():
        if role not in roles:
            problems.append(f"لم أجد مربع «{label}» في القالب.")
    ids = {_shape_id(sp) for sp in drawing.iter(f"{XDR}sp")}
    if not set(PAYMENT_BOX_IDS.values()) <= ids:
        problems.append("مربعات اختيار طريقة الدفع غير موجودة (سيُكتب الاختيار نصًّا).")
    return problems


def fill_template(template: Path, out_path: Path, data: dict) -> Path:
    """Write ``out_path`` (.xlsm) = template filled with ``data``. Raises TemplateError."""
    items = list(data["items"])
    if not items:
        raise TemplateError("لا توجد سلع في الفاتورة.")
    if len(items) > MAX_ITEMS:
        raise TemplateError(f"الحد الأقصى {MAX_ITEMS} سطرًا في الفاتورة.")

    infos, parts = _read_zip(Path(template))
    sheet_part, drawing_part = _locate_parts(parts)
    sheet_root, sheet_text = _parse(parts[sheet_part])
    drawing_root, drawing_text = _parse(parts[drawing_part])
    sheet = SheetEditor(sheet_root, _shared_strings(parts))
    layout = detect_layout(sheet)

    seller, buyer = data["seller"], data["customer"]
    rate = Decimal(str(data["vat_rate"]))
    first_row = layout.first_item_row
    row_count = max(layout.item_rows, len(items))
    delta = row_count - layout.item_rows
    last_item_row = layout.last_item_row
    if delta:
        sheet.insert_rows_after(last_item_row, delta)
        _shift_anchors(drawing_root, last_item_row, delta)             # 0-based row index of row 31

    # ---- table header (same wording as the PDF) --------------------------
    for col, text in zip("CDEFG", COLUMN_HEADERS):
        if sheet.has_cell(f"{col}{layout.header_row}"):
            sheet.set_str(f"{col}{layout.header_row}", text)

    # ---- item rows -------------------------------------------------------
    for i in range(row_count):
        r = first_row + i
        sheet.set_str(f"C{r}", f"{i + 1:02d}")
        if i < len(items):
            it = items[i]
            sheet.set_str(f"D{r}", it["description"])
            sheet.set_num(f"E{r}", Decimal(it["quantity"]))
            sheet.set_num(f"F{r}", Decimal(it["unit_price"]))
            total_cached = Decimal(it["line_total"])
            lines = max(1, math.ceil(len(it["description"]) / 34))
            if lines > 1:
                sheet.set_row_height(r, 19.95 * min(lines, 3))
        else:
            for col in ("D", "E", "F"):
                sheet.clear(f"{col}{r}")
            total_cached = Decimal(0)
        sheet.set_formula(f"G{r}", f"ROUND(E{r}*F{r},2)", total_cached)

    # ---- totals, words, payment -----------------------------------------
    ht = layout.totals_row + delta                                      # TOTAL HT row
    rate_text = plain_number(rate)
    sheet.set_str(f"E{ht}", LABEL_HT)
    sheet.set_formula(f"G{ht}", f"ROUND(SUM(G{first_row}:G{ht - 1}),2)", Decimal(data["subtotal"]))
    sheet.set_str(f"E{ht + 1}", vat_label(data))
    sheet.set_formula(f"G{ht + 1}", f"ROUND(G{ht}*{rate_text}/100,2)", Decimal(data["vat_amount"]))
    sheet.set_str(f"E{ht + 2}", LABEL_TTC)
    sheet.set_formula(f"G{ht + 2}", f"ROUND(G{ht}+G{ht + 1},2)", Decimal(data["total"]))
    # The template's own MontantDA() macro is replaced by plain text so export never depends on macros.
    sheet.set_str(f"C{layout.words_row + delta}", amount_in_words(data["total"]))

    # ---- text boxes ------------------------------------------------------
    roles = classify_shapes(drawing_root)
    missing = [r for r in ("seller", "buyer", "serial", "invoice", "date", "header") if r not in roles]
    if missing:
        raise TemplateError("تعذّر إيجاد حقول داخل قالب Excel: " + "، ".join(missing) +
                            ". أعد القالب الأصلي إلى مجلد assets أو أصلح تسمياته.")
    _fill_labelled_box(roles["seller"], seller_lines(seller), "البائع")
    _fill_labelled_box(roles["buyer"], buyer_lines(buyer), "المشتري")
    _fill_single(roles["date"], date_line(data), "التاريخ")
    _fill_single(roles["serial"], serial_line(data), "الرقم التسلسلي")
    _fill_single(roles["invoice"], invoice_line(data), "رقم الفاتورة")
    name, sub_title = title_lines(seller)
    _fill_header(roles["header"], name, sub_title)

    # ---- payment method --------------------------------------------------
    payment = data.get("payment_method", "cash")
    boxes = {sp_id: sp for sp in drawing_root.iter(f"{XDR}sp") for sp_id in [_shape_id(sp)]
             if sp_id in PAYMENT_BOX_IDS.values()}
    if len(boxes) == len(PAYMENT_BOX_IDS):
        for method, sp_id in PAYMENT_BOX_IDS.items():
            _tick_box(boxes[sp_id], method == payment)
    else:                                                               # template changed: degrade to text
        line = "طريقة الدفع :   " + "      ".join(
            ("☒ " if k == payment else "☐ ") + v for k, v in payment_options())
        sheet.set_str(f"C{layout.payment_row + delta}", line)

    # ---- sheet view: open at the top of the invoice ----------------------
    view = sheet_root.find(f"{M}sheetViews/{M}sheetView")
    if view is not None:
        view.set("topLeftCell", "A1")
        sel = view.find(f"{M}selection")
        if sel is not None:
            sel.set("activeCell", "A1")
            sel.set("sqref", "A1")

    # ---- workbook-level fixes -------------------------------------------
    wb_text = parts["xl/workbook.xml"].decode("utf-8-sig")
    if "<calcPr" in wb_text:
        wb_text = re.sub(r"<calcPr\b([^>]*?)/>", lambda m: "<calcPr" + re.sub(r'\s+fullCalcOnLoad="[^"]*"', "", m.group(1)) + ' fullCalcOnLoad="1"/>', wb_text, count=1)
    if delta:
        def bump_print_area(m):
            return m.group(1) + _shift_range(m.group(2), last_item_row, delta) + m.group(3)
        wb_text = re.sub(r"(<definedName name=\"_xlnm\.Print_Area\"[^>]*>[^<]*?!)([^<]+)(</definedName>)",
                         bump_print_area, wb_text)
    parts["xl/workbook.xml"] = wb_text.encode("utf-8")

    # The template shows '1,234.00 .دج' (the dot lands on the wrong side in right-to-left text).
    # Use the exact money format the PDF prints: '1,234.00 دج'.
    if "xl/styles.xml" in parts:
        styles = parts["xl/styles.xml"].decode("utf-8-sig")
        styles = styles.replace('#,##0.00\\ &quot;.دج&quot;', f'#,##0.00\\ &quot;{CURRENCY}&quot;')
        parts["xl/styles.xml"] = styles.encode("utf-8")

    # calcChain is only an optimisation and would be stale -> drop it (Excel rebuilds it)
    parts.pop("xl/calcChain.xml", None)
    if "xl/_rels/workbook.xml.rels" in parts:
        rels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8-sig")
        rels = re.sub(r"<Relationship\b[^>]*calcChain[^>]*/>", "", rels)
        parts["xl/_rels/workbook.xml.rels"] = rels.encode("utf-8")
    ct = parts["[Content_Types].xml"].decode("utf-8-sig")
    parts["[Content_Types].xml"] = re.sub(r"<Override\b[^>]*calcChain[^>]*/>", "", ct).encode("utf-8")

    parts[sheet_part] = _serialize(sheet_root, sheet_text)
    parts[drawing_part] = _serialize(drawing_root, drawing_text)
    _write_zip(Path(out_path), infos, parts)
    return Path(out_path)
