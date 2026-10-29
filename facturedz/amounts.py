# -*- coding: utf-8 -*-
"""Money, number parsing and Arabic amount-in-words. All money maths uses Decimal."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .textnorm import to_ascii_digits

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("9999999999.99")


def parse_decimal(text: object) -> Decimal:
    """Parse user input leniently. Accepts Arabic-Indic digits and common separators.

    Rules (documented in README):
      * both ',' and '.' present  -> the LAST one is the decimal separator
      * only one ','              -> decimal separator   ("1,5" = 1.5)
      * several ',' (or '.')      -> thousands separators
      * spaces / apostrophes are ignored
    Empty text is 0. Raises ValueError for anything else.
    """
    s = to_ascii_digits(text).strip()
    s = re.sub(r"[\s\u00a0\u202f\u2009']", "", s)
    if not s:
        return Decimal(0)
    if not re.fullmatch(r"[+-]?[\d.,]+", s):
        raise ValueError("قيمة رقمية غير صحيحة")
    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        decimal_sep = "," if s.rfind(",") > s.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        s = s.replace(thousands, "").replace(decimal_sep, ".")
    elif has_comma:
        s = s.replace(",", "") if s.count(",") > 1 else s.replace(",", ".")
    elif has_dot and s.count(".") > 1:
        s = s.replace(".", "")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError("قيمة رقمية غير صحيحة") from exc
    if not value.is_finite():
        raise ValueError("قيمة رقمية غير صحيحة")
    return value


def q2(value: Decimal) -> Decimal:
    """Round to centimes, half away from zero (matches Excel's ROUND)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def line_total(quantity: Decimal, unit_price: Decimal) -> Decimal:
    return q2(Decimal(quantity) * Decimal(unit_price))


def vat_amount(subtotal: Decimal, rate: Decimal) -> Decimal:
    return q2(Decimal(subtotal) * Decimal(rate) / Decimal(100))


def to_decimal(value: object) -> Decimal:
    """Decimal from stored/float values without binary-float noise."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value if value is not None else 0))


def money(value: object) -> str:
    return f"{to_decimal(value):,.2f}"


def plain_number(value: object) -> str:
    """Decimal as text without exponent or trailing zeros ('19', '0.5', '353.87')."""
    d = to_decimal(value)
    if d == d.to_integral_value():
        return format(d.quantize(Decimal(1)), "f")
    return format(d.normalize(), "f")


def qty_text(value: object) -> str:
    d = to_decimal(value)
    text = plain_number(d)
    if "." in text:
        int_part, frac = text.split(".")
        return f"{int(int_part):,}.{frac}"
    return f"{int(text):,}"


# --------------------------------------------------------------------------- #
# Amount in words (Arabic)                                                    #
# --------------------------------------------------------------------------- #
_ONES = ["", "واحد", "اثنان", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة", "عشرة",
         "أحد عشر", "اثنا عشر", "ثلاثة عشر", "أربعة عشر", "خمسة عشر", "ستة عشر", "سبعة عشر",
         "ثمانية عشر", "تسعة عشر"]
_TENS = ["", "", "عشرون", "ثلاثون", "أربعون", "خمسون", "ستون", "سبعون", "ثمانون", "تسعون"]
_HUNDREDS = ["", "مائة", "مائتان", "ثلاثمائة", "أربعمائة", "خمسمائة", "ستمائة", "سبعمائة", "ثمانمائة", "تسعمائة"]
_SCALES = [  # (divisor, singular, dual, plural)
    (10 ** 9, "مليار", "ملياران", "مليارات"),
    (10 ** 6, "مليون", "مليونان", "ملايين"),
    (10 ** 3, "ألف", "ألفان", "آلاف"),
]


def _join(parts: list) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return parts[0] + "".join(" و" + p for p in parts[1:])


def _below_1000(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(_HUNDREDS[hundreds])
    if rest:
        if rest < 20:
            parts.append(_ONES[rest])
        else:
            tens, unit = divmod(rest, 10)
            if unit:
                parts.append(_ONES[unit])
            parts.append(_TENS[tens])
    return _join(parts)


def integer_words(n: int) -> str:
    if n < 0:
        return "سالب " + integer_words(-n)
    if n == 0:
        return "صفر"
    if n >= 10 ** 12:
        raise OverflowError("المبلغ كبير جدًا")
    parts = []
    for divisor, singular, dual, plural in _SCALES:
        value, n = divmod(n, divisor)
        if not value:
            continue
        if value == 1:
            parts.append(singular)
        elif value == 2:
            parts.append(dual)
        elif 3 <= value <= 10:
            parts.append(f"{_below_1000(value)} {plural}")
        elif value == 200:
            parts.append(f"مائتا {singular}")
        else:
            parts.append(f"{_below_1000(value)} {singular}")
    if n:
        parts.append(_below_1000(n))
    return _join(parts)


def amount_in_words(amount: object) -> str:
    """e.g. 1250.50 -> 'ألف ومائتان وخمسون دينار جزائري وخمسون سنتيم'."""
    value = q2(to_decimal(amount))
    sign = "سالب " if value < 0 else ""
    cents_total = int(abs(value) * 100)
    dinars, centimes = divmod(cents_total, 100)
    result = f"{sign}{integer_words(dinars)} دينار جزائري"
    if centimes:
        result += f" و{integer_words(centimes)} سنتيم"
    return result
