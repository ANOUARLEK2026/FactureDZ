# -*- coding: utf-8 -*-
"""Single source of truth for everything the invoice *says*.

Both outputs read from here, so the Excel file and the PDF can never drift apart:
  * template.py  writes these strings into the Excel template
  * pdf_invoice.py draws the very same strings on the PDF page

Change a label here and both files change together.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .amounts import money, plain_number, qty_text
from .config import PAYMENT_METHODS

CURRENCY = "دج"

# Table header cells, right -> left.
COLUMN_HEADERS = ("N°", "التعيين", "الكمية (كلغ)", "السعر (دج/كلغ)", "المجموع")

LABEL_HT = "TOTAL HT"
LABEL_TTC = "TOTAL TTC"
WORDS_TITLE = "أوقف هذا الوصل عن مبلغ و قدره :"
PAYMENT_TITLE = "طريقة الدفع :"
SIGNATURE = "الامضاء"
FALLBACK_TITLE = "فاتورة بيع"

# (data key, printed label) in printed order. Same labels in the Excel boxes and on the PDF.
SELLER_LINES: List[Tuple[str, str]] = [
    ("commercial_title", "العنوان التجاري"),
    ("rc", "السجل التجاري"),
    ("nif", "الرقم الجبائي"),
    ("nis", "الرقم الإحصائي"),
    ("bank_account", "رقم الحساب البنكي"),
    ("article_no", "رقم المادة"),
    ("phone", "رقم الهاتف"),
    ("activity", "النشاط"),
]
BUYER_LINES: List[Tuple[str, str]] = [
    ("name", "المطلوب من"),
    ("commercial_title", "العنوان التجاري"),
    ("rc", "السجل التجاري"),
    ("nif", "الرقم الجبائي"),
    ("nis", "الرقم الإحصائي"),
    ("bank_account", "رقم الحساب البنكي"),
    ("article_no", "رقم المادة"),
    ("activity", "النشاط"),
]


def fmt_money(value: Any) -> str:
    """'11,712.71 دج' — also installed as the Excel number format (see template.py)."""
    return f"{money(value)} {CURRENCY}"


def fmt_number(value: Any) -> str:
    """Plain amount without currency, e.g. a line total inside the table."""
    return money(value)


def fmt_qty(value: Any) -> str:
    return qty_text(value)


def labelled(label: str, value: Any) -> str:
    text = str(value or "").strip()
    return f"{label} : {text}" if text else f"{label} :"


def seller_lines(seller: Dict[str, str]) -> List[Tuple[str, str, str]]:
    """[(key, label, value)] for the seller box."""
    return [(key, label, str(seller.get(key, "") or "").strip()) for key, label in SELLER_LINES]


def buyer_lines(buyer: Dict[str, str]) -> List[Tuple[str, str, str]]:
    """[(key, label, value)] for the buyer box (address falls back into the commercial title)."""
    values = dict(buyer)
    values["commercial_title"] = (buyer.get("commercial_title") or buyer.get("address") or "")
    return [(key, label, str(values.get(key, "") or "").strip()) for key, label in BUYER_LINES]


def title_lines(seller: Dict[str, str]) -> Tuple[str, str]:
    name = (seller.get("business_name") or "").strip() or FALLBACK_TITLE
    sub = (seller.get("address") or seller.get("city") or "").strip()
    return name, sub


def date_line(data: Dict[str, Any]) -> str:
    city = str((data.get("seller") or {}).get("city") or "").strip()
    return f"{city} في: {data['invoice_date']}".strip()


def serial_line(data: Dict[str, Any]) -> str:
    return f"الرقم التسلسلي : {data['serial_no']}"


def is_purchase(data: Dict[str, Any]) -> bool:
    return data.get("kind") == "purchase"


def invoice_line(data: Dict[str, Any]) -> str:
    label = "رقم فاتورة الشراء" if is_purchase(data) else "رقم الفاتورة"
    return f"{label} : {data['invoice_no']}"


def document_title(data: Dict[str, Any]) -> str:
    """Title stored in the PDF metadata."""
    return f"{'فاتورة شراء' if is_purchase(data) else 'فاتورة'} {data.get('invoice_no', '')}".strip()


def vat_label(data: Dict[str, Any]) -> str:
    return f"TVA {plain_number(data['vat_rate'])}%"


def payment_options() -> List[Tuple[str, str]]:
    """[(key, printed label)] in reading order (right -> left). Wording is the template's own (C-row text)."""
    printed = {"cash": "نقدا", "cheque": "شيك", "term": "لاجل", "bank": "بنك"}
    return [(key, printed.get(key, label)) for key, label in PAYMENT_METHODS]
