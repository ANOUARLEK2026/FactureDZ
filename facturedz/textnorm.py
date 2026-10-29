# -*- coding: utf-8 -*-
"""Arabic text helpers: digit conversion, search normalisation, safe XML/file text."""
from __future__ import annotations

import re
import unicodedata

_TATWEEL = "\u0640"
_DIACRITICS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_PRIVATE_USE = re.compile("[\ue000-\uf8ff]")  # Symbol/Wingdings placeholders in the template
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_BIDI_CONTROLS = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069\u061c]")
_SPACES = re.compile(r"\s+")

_LETTER_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي", "ی": "ي",
    "ؤ": "و",
    "ة": "ه",
    "ک": "ك",
})

# Arabic-Indic (٠-٩) and Eastern Arabic-Indic / Persian (۰-۹) digits + separators.
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})
_DIGIT_MAP[ord("٫")] = "."   # Arabic decimal separator
_DIGIT_MAP[ord("٬")] = ","   # Arabic thousands separator


def to_ascii_digits(text: object) -> str:
    """Convert Arabic-Indic digits and separators to their ASCII equivalents."""
    return str(text if text is not None else "").translate(_DIGIT_MAP)


def normalize_ar(text: object) -> str:
    """Loose form for searching/matching: no tatweel, diacritics, alef/yeh variants."""
    s = unicodedata.normalize("NFKC", str(text if text is not None else ""))
    s = _PRIVATE_USE.sub("", s)
    s = s.replace(_TATWEEL, "")
    s = _DIACRITICS.sub("", s)
    s = _BIDI_CONTROLS.sub("", s)
    s = s.translate(_LETTER_MAP)
    s = to_ascii_digits(s).casefold()
    return _SPACES.sub(" ", s).strip()


def xml_safe(text: object) -> str:
    """Make arbitrary user text safe for an XML text node (single line)."""
    s = str(text if text is not None else "")
    s = _XML_ILLEGAL.sub("", s)
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return s.strip()


_FILENAME_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(text: object, max_len: int = 40) -> str:
    """Windows-safe file name part (keeps Arabic letters)."""
    s = _BIDI_CONTROLS.sub("", str(text if text is not None else ""))
    s = _FILENAME_ILLEGAL.sub(" ", s)
    s = _SPACES.sub(" ", s).strip(" .")
    s = s[:max_len].strip(" .")
    if s.upper() in _RESERVED:
        s = f"_{s}"
    return s
