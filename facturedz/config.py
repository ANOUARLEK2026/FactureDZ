# -*- coding: utf-8 -*-
"""Static configuration: field lists, defaults and limits."""
from __future__ import annotations

APP_TITLE = "دفتر الفواتير | Facture DZ"

# Limits ---------------------------------------------------------------------
TEMPLATE_ITEM_ROWS = 8      # item rows physically present in the Excel template
MAX_ITEMS = 16              # rows are inserted into the template beyond 8, up to this
DESCRIPTION_SOFT_LIMIT = 90

# Numbering ------------------------------------------------------------------
# invoice_no = MM + YY + customer number + running number inside that month, e.g. 1026 00007 0012
#   -> "1026000070012" (13 digits). The month-counter part is the tail, so it may grow past 4 digits
#   without ever becoming ambiguous; the customer part is fixed-width and is checked, never truncated.
CUSTOMER_DIGITS = 5         # customer number, zero-padded (1 .. 99 999)
SEQ_DIGITS = 4              # invoice counter inside the month, zero-padded (grows if ever exceeded)
SERIAL_DIGITS = 12          # random serial number, never reused (even after an invoice is deleted)
NUMBER_MAX_DIGITS = 18      # hard ceiling: fits SQLite INTEGER / Qt qlonglong and every printed box
BACKUP_KEEP_DAILY = 14
BACKUP_KEEP_MONTHLY = 12

PAYMENT_METHODS = [("cash", "نقدًا"), ("cheque", "شيك"), ("term", "لأجل"), ("bank", "بنك")]
PAYMENT_ALIASES = {"check": "cheque", "chèque": "cheque", "": "cash"}

SELLER_FIELDS = [
    ("business_name", "اسم المؤسسة"),
    ("address", "مقر المؤسسة (يظهر تحت الاسم)"),
    ("commercial_title", "العنوان التجاري"),
    ("rc", "رقم السجل التجاري"),
    ("nif", "الرقم الجبائي"),
    ("nis", "الرقم الإحصائي"),
    ("bank_account", "رقم الحساب البنكي"),
    ("article_no", "رقم المادة"),
    ("activity", "النشاط التجاري"),
    ("phone", "رقم الهاتف"),
    ("city", "المدينة (تظهر قبل التاريخ)"),
]

CUSTOMER_FIELDS = [
    ("name", "اسم المشتري"),
    ("commercial_title", "العنوان التجاري"),
    ("address", "العنوان / المقر"),
    ("rc", "رقم السجل التجاري"),
    ("nif", "الرقم الجبائي"),
    ("nis", "الرقم الإحصائي"),
    ("bank_account", "رقم الحساب البنكي"),
    ("article_no", "رقم المادة"),
    ("activity", "النشاط التجاري"),
    ("phone", "رقم الهاتف"),
]

SUPPLIER_FIELDS = [("name", "اسم المورد")] + [f for f in CUSTOMER_FIELDS if f[0] != "name"]

# First-run defaults only; whatever the user saves in Settings always wins.
DEFAULT_SELLER = {
    "business_name": "أولاد الحاجة صليحة للتجارة",
    "address": "حي الصحن - ولاية الوادي",
    "commercial_title": "حي الصحن الأول، قسم 092، مجموعة ملكية 175، بلدية الوادي",
    "rc": "26 أ 2771413-00/39",
    "nif": "19739010246618203900",
    "nis": "199739010246619",
    "bank_account": "",
    "article_no": "39010116333",
    "activity": "توزيع بالجملة للفواكه والخضر",
    "phone": "0550 38 76 95",
    "city": "الوادي",
}

# Stock ----------------------------------------------------------------------
QTY_SCALE = 1000            # stock quantities are stored as whole grams (kg x 1000): exact sums, no float noise
STOCK_UNIT = "كلغ"
PURCHASE_FOLDER = "Achats"  # purchase invoices are saved in <output>/Achats/<year>/<month>
PRODUCT_NAME_MAX = 90

MOVEMENT_KINDS = [("initial", "رصيد افتتاحي"), ("purchase", "شراء"), ("sale", "بيع"), ("adjust", "تعديل / جرد")]
ADJUST_REASONS = [("count", "جرد وتصحيح"), ("spoilage", "تالف / فاسد"), ("return_in", "مرتجع من زبون"),
                  ("return_out", "مرتجع إلى مورد"), ("other", "سبب آخر")]

DEFAULT_SETTINGS = {
    "vat_rate": "19",
    "last_invoice_number": "0",
    "last_serial_number": "0",
    "ask_save_location": "0",   # 0 = save automatically into <output>/<year>/<month>
    "keep_xlsm": "1",           # keep the editable filled Excel copy next to the PDF
    "warn_unlinked": "1",       # warn when a sold item is not in the stock catalogue (its stock cannot change)
}
