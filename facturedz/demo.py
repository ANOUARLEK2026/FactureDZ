# -*- coding: utf-8 -*-
"""Disposable demo data (``FactureDZ.pyw --demo``): customers, suppliers, stock, purchase and sale invoices."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from typing import Dict, List

from .amounts import line_total, q2, vat_amount
from .config import CUSTOMER_FIELDS, SUPPLIER_FIELDS
from .db import Repository
from .purchase_service import PurchaseService, build_purchase
from .service import InvoiceService

SELLER = {
    "business_name": "مؤسسة الواحة للتجارة بالجملة", "address": "سوق الجملة، حي 5 جويلية - الوادي",
    "commercial_title": "بيانات تجريبية — ليست مؤسسة حقيقية", "rc": "39/00-1234567 ب 25",
    "nif": "19739010246618203900", "nis": "199739010246619", "bank_account": "",
    "article_no": "39010116333", "activity": "توزيع بالجملة للفواكه والخضر", "phone": "0550 12 34 56",
    "city": "الوادي",
}
CUSTOMERS = [  # name, commercial title, address, rc, nif, nis, bank, article, activity, phone
    ("مؤسسة النور للتجارة العامة", "النور", "حي 5 جويلية، ورقلة", "30/00-1122334 ب 12", "000130123456789",
     "000130123456789012", "", "30010112", "تجارة بالجملة", "0550 10 20 30"),
    ("شركة الأمل للتوزيع", "الأمل", "شارع الاستقلال، تقرت", "30/00-2233445 ب 08", "000230234567890",
     "000230234567890123", "", "30010223", "توزيع المواد الغذائية", "0551 11 22 33"),
    ("تعاونية الخيرات الفلاحية", "الخيرات", "الطريق الوطني 49، الوادي", "39/00-3344556 ب 15",
     "000330345678901", "000330345678901234", "", "39010334", "إنتاج وتوزيع الخضر", "0552 12 23 34"),
    ("مؤسسة الرياض للفواكه والخضر", "الرياض", "حي 1000 مسكن، بسكرة", "07/00-4455667 ب 21",
     "000430456789012", "000430456789012345", "", "07010445", "بيع الفواكه بالجملة", "0553 13 24 35"),
]
PRODUCTS = [("موز مستورد درجة أولى", 220, 50), ("تفاح أحمر", 180, 40), ("بطاطا موسمية", 60, 300),
            ("جزر", 75, 120), ("برتقال", 110, 90), ("طماطم", 120, 80),
            ("ثوم أبيض مستورد من الدرجة الأولى للتصدير", 400, 20), ("فلفل أخضر", 130, 60)]
ITEM_COUNTS = (3, 5, 2, 8, 4, 12, 1, 6)
SUPPLIERS = [  # name, commercial title, address, rc, nif, nis, bank, article, activity, phone
    ("مزارع وادي ريغ", "وادي ريغ", "المغير، الوادي", "39/00-5566778 ب 10", "000530567890123", "000530567890123456", "",
     "39010556", "إنتاج الخضر", "0554 14 25 36"),
    ("تعاونية الجنوب للفواكه", "الجنوب", "سوق الجملة، بسكرة", "07/00-6677889 ب 14", "000630678901234",
     "000630678901234567", "", "07010667", "استيراد وتوزيع الفواكه", "0555 15 26 37"),
]
LOW_STOCK_PRODUCT = "ثوم أبيض مستورد من الدرجة الأولى للتصدير"      # bought in a small quantity: shows the "low" status


def _demo_purchases(repo: Repository) -> None:
    """Two purchase invoices (months before the sales) so that the demo stock is real: products are created by them."""
    buying = PurchaseService(repo)
    today = dt.date.today()
    for n, supplier_row in enumerate(SUPPLIERS):
        supplier = dict(zip((k for k, _ in SUPPLIER_FIELDS), supplier_row))
        repo.purchases.save_supplier(supplier)
        rows = []
        for name, price, _qty in PRODUCTS[n::2]:
            qty = 260 if name == LOW_STOCK_PRODUCT else 2500
            rows.append({"description": name, "quantity": str(qty), "price": str(int(price * 0.7))})
        form = {"date": (today - dt.timedelta(days=100 - 20 * n)).strftime("%d.%m.%Y"), "vat_rate": "19",
                "payment": "cash", "supplier": supplier, "items": rows}
        buying.issue(build_purchase(form, SELLER))
    low = repo.stock.find_product(LOW_STOCK_PRODUCT)
    if low:
        repo.stock.save_product({"name": low["name"], "sell_price": "400", "min_stock": "100"}, product_id=low["id"])


def build_demo(repo: Repository, service: InvoiceService) -> int:
    repo.save_seller(SELLER)
    repo.set_setting("keep_xlsm", "1")
    _demo_purchases(repo)
    customers: List[Dict[str, str]] = []
    for row in CUSTOMERS:
        payload = dict(zip((k for k, _ in CUSTOMER_FIELDS), row))
        repo.save_customer(payload)
        customers.append(payload)
    today = dt.date.today()
    made = 0
    for i, count in enumerate(ITEM_COUNTS):
        date = today - dt.timedelta(days=(len(ITEM_COUNTS) - i) * 9)
        items = []
        for n in range(count):
            name, price, qty = PRODUCTS[(i + n) % len(PRODUCTS)]
            q, p = D(qty + 7 * n), D(price + n)
            items.append({"description": name, "quantity": q, "unit_price": p, "line_total": line_total(q, p)})
        sub = q2(sum(x["line_total"] for x in items))
        rate = D(19)
        vat = vat_amount(sub, rate)
        data = {"invoice_date": date.strftime("%d.%m.%Y"),          # numbers are assigned by service.issue()
                "seller": dict(SELLER), "customer": dict(customers[i % len(customers)]), "items": items,
                "subtotal": sub, "vat_rate": rate, "vat_amount": vat, "total": q2(sub + vat),
                "payment_method": ("cash", "cheque", "term", "bank")[i % 4]}
        service.issue(data)
        made += 1
    return made
