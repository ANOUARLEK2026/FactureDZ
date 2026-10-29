from decimal import Decimal as D

from facturedz.amounts import line_total, q2, vat_amount
from facturedz.config import DEFAULT_SELLER

NAMES = ["موز", "تفاح", "بطاطا", "جزر", "برتقال", "طماطم", "بصل", "ثوم أبيض مستورد من الدرجة الأولى للتصدير",
         "فلفل", "خيار", "ليمون", "كوسة", "باذنجان", "قرنبيط", "سلق", "نعناع"]


def make_invoice(n_items=3, payment="cheque", rate=D(19)):
    items = []
    for i in range(n_items):
        q, p = D(10 + i), D("353.87") + i
        items.append({"description": NAMES[i], "quantity": q, "unit_price": p, "line_total": line_total(q, p)})
    sub = q2(sum(i["line_total"] for i in items))
    vat = vat_amount(sub, rate)
    return {
        "invoice_no": "57", "serial_no": "57", "invoice_date": "10.10.2026", "seller": dict(DEFAULT_SELLER),
        "customer": {"name": "مؤسسة النور للتجارة العامة", "commercial_title": "حي 5 جويلية، ورقلة",
                     "rc": "30 ب 1234567", "nif": "000130123456789", "nis": "000130123456789012",
                     "bank_account": "", "article_no": "30010112", "activity": "تجارة بالجملة"},
        "items": items, "subtotal": sub, "vat_rate": rate, "vat_amount": vat, "total": q2(sub + vat),
        "payment_method": payment,
    }
