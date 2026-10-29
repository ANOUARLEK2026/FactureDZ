# -*- coding: utf-8 -*-
"""Invoice / serial number formats (pure functions, no database).

invoice number  =  MM  YY  CCCCC  NNNN
                   |   |   |      '-- running number of the invoice inside that month (per month, all customers)
                   |   |   '--------- customer number (customers.id, zero-padded)
                   |   '------------- year  (2 digits)
                   '----------------- month (2 digits)

serial number   =  12 random digits, first digit never 0 (so no leading zero is ever lost in Excel/CSV).
"""
from __future__ import annotations

import datetime as dt
import secrets

from .config import CUSTOMER_DIGITS, NUMBER_MAX_DIGITS, SEQ_DIGITS, SERIAL_DIGITS

DATE_DIGITS = 4                               # "MMYY"
PREFIX_DIGITS = DATE_DIGITS + CUSTOMER_DIGITS  # everything before the monthly counter
MIN_INVOICE_DIGITS = PREFIX_DIGITS + SEQ_DIGITS
MAX_CUSTOMER_ID = 10 ** CUSTOMER_DIGITS - 1


class NumberingError(ValueError):
    """Arabic, user-presentable message."""


def month_key(day: dt.date) -> str:
    """Counter key for the month of ``day`` (the year is kept in full: 2026-10 and 2126-10 never collide)."""
    return f"{day.year:04d}{day.month:02d}"


def date_part(day: dt.date) -> str:
    return f"{day.month:02d}{day.year % 100:02d}"


def customer_part(customer_id: int) -> str:
    customer_id = int(customer_id)
    if not 1 <= customer_id <= MAX_CUSTOMER_ID:
        raise NumberingError(f"رقم المشتري {customer_id} خارج المجال المسموح (1 إلى {MAX_CUSTOMER_ID}).")
    return f"{customer_id:0{CUSTOMER_DIGITS}d}"


def format_invoice_no(day: dt.date, customer_id: int, seq: int) -> str:
    seq = int(seq)
    if seq < 1:
        raise NumberingError("رقم الفاتورة داخل الشهر يجب أن يبدأ من 1.")
    text = f"{date_part(day)}{customer_part(customer_id)}{seq:0{SEQ_DIGITS}d}"
    if len(text) > NUMBER_MAX_DIGITS:
        raise NumberingError("رقم الفاتورة طويل أكثر من اللازم.")
    return text


def parse_invoice_no(text: str):
    """(MMYY, customer_id, seq) for a number made by format_invoice_no, else None (legacy short numbers)."""
    text = str(text or "")
    if not text.isdigit() or not MIN_INVOICE_DIGITS <= len(text) <= NUMBER_MAX_DIGITS:
        return None
    return (text[:DATE_DIGITS], int(text[DATE_DIGITS:PREFIX_DIGITS]), int(text[PREFIX_DIGITS:]))


PURCHASE_PREFIX = "A"                          # "Achat": a purchase number can never be mistaken for a sale number


def supplier_part(supplier_id: int) -> str:
    supplier_id = int(supplier_id)
    if not 1 <= supplier_id <= MAX_CUSTOMER_ID:
        raise NumberingError(f"رقم المورد {supplier_id} خارج المجال المسموح (1 إلى {MAX_CUSTOMER_ID}).")
    return f"{supplier_id:0{CUSTOMER_DIGITS}d}"


def format_purchase_no(day: dt.date, supplier_id: int, seq: int) -> str:
    """A + MMYY + supplier number + running purchase number of the month, e.g. A1026 00003 0007."""
    seq = int(seq)
    if seq < 1:
        raise NumberingError("رقم فاتورة الشراء داخل الشهر يجب أن يبدأ من 1.")
    text = f"{PURCHASE_PREFIX}{date_part(day)}{supplier_part(supplier_id)}{seq:0{SEQ_DIGITS}d}"
    if len(text) > NUMBER_MAX_DIGITS:
        raise NumberingError("رقم فاتورة الشراء طويل أكثر من اللازم.")
    return text


def random_serial() -> str:
    """12 random digits from the OS CSPRNG; uniqueness is enforced by the database, not by luck."""
    low = 10 ** (SERIAL_DIGITS - 1)
    return str(low + secrets.randbelow(9 * low))
