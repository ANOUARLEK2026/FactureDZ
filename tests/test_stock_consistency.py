"""Randomised consistency test: after every random sale / purchase / edit / delete / adjustment, the stock of each
product must equal what the documents on file say, and no stock row may point at a deleted document."""
import logging
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_service import fake_pdf
from facturedz.db import Repository
from facturedz.config import DEFAULT_SELLER
from facturedz.service import InvoiceService, build_invoice, ValidationError
from facturedz.purchase_service import PurchaseService, build_purchase
from facturedz.inventory import to_milli

NAMES = ["طماطم", "بطاطا", "جزر", "موز", "تفاح", "بصل"]

def expected(repo):
    """stock per product recomputed ONLY from documents + manual rows (independent of movements of documents)."""
    exp = {}
    with repo.connect() as con:
        for r in con.execute("SELECT product_id, SUM(qty_milli) q FROM stock_movements WHERE ref_kind='' GROUP BY product_id"):
            exp[r["product_id"]] = int(r["q"])
    for row in repo.purchases.list_purchases(limit=100000):
        for it in repo.purchases.get_purchase(row["id"])["items"]:
            exp[it["product_id"]] = exp.get(it["product_id"], 0) + to_milli(it["quantity"])
    for row in repo.list_invoices(limit=100000):
        for it in repo.get_invoice(row["id"])["items"]:
            if it.get("product_id") is not None:
                exp[it["product_id"]] = exp.get(it["product_id"], 0) - to_milli(it["quantity"])
    return exp

def check(repo, step):
    exp = expected(repo)
    got = {p["id"]: int(to_milli(p["on_hand"])) for p in repo.stock.list_products(include_archived=True)}
    for pid in set(exp) | set(got):
        if exp.get(pid, 0) != got.get(pid, 0):
            raise AssertionError(f"MISMATCH after {step}: product {pid} expected {exp.get(pid,0)} got {got.get(pid,0)}")
    with repo.connect() as con:   # no orphan movements
        orphans = con.execute("""SELECT COUNT(*) FROM stock_movements m WHERE
            (m.ref_kind='invoice' AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.id=m.ref_id)) OR
            (m.ref_kind='purchase' AND NOT EXISTS (SELECT 1 FROM purchases p WHERE p.id=m.ref_id))""").fetchone()[0]
    if orphans: raise AssertionError(f"ORPHAN movements after {step}: {orphans}")

def run(seed):
    rnd = random.Random(seed)
    home = Path(tempfile.mkdtemp())
    repo = Repository(home); sales = InvoiceService(repo); buying = PurchaseService(repo)
    for n in NAMES[:4]:
        repo.stock.save_product({"name": n}, opening_qty=str(rnd.randint(0, 50)), opening_cost="10")
    inv_ids, pur_ids = [], []
    def items():
        rows = []
        for _ in range(rnd.randint(1, 4)):
            name = rnd.choice(NAMES + ["خدمة"])
            rows.append({"description": name, "quantity": str(round(rnd.uniform(0.001, 40), 3)), "price": str(rnd.randint(1, 300))})
        return rows
    for step in range(60):
        op = rnd.choice(["sale", "buy", "edit_sale", "edit_buy", "del_sale", "del_buy", "adjust", "archive", "reexport"])
        try:
            if op == "sale":
                d = build_invoice({"date": "10.10.2026", "vat_rate": "19", "payment": "cash", "customer": {"name": rnd.choice("ab")}, "items": items()}, DEFAULT_SELLER)
                inv_ids.append(sales.issue(d).invoice_id)
            elif op == "buy":
                d = build_purchase({"date": "10.10.2026", "vat_rate": "19", "payment": "cash", "supplier": {"name": rnd.choice("xy")}, "items": items()}, DEFAULT_SELLER)
                pur_ids.append(buying.issue(d).invoice_id)
            elif op == "edit_sale" and inv_ids:
                d = build_invoice({"date": "11.10.2026", "vat_rate": "19", "payment": "cash", "customer": {"name": "a"}, "items": items()}, DEFAULT_SELLER)
                sales.edit(rnd.choice(inv_ids), d)
            elif op == "edit_buy" and pur_ids:
                d = build_purchase({"date": "11.10.2026", "vat_rate": "19", "payment": "cash", "supplier": {"name": "x"}, "items": items()}, DEFAULT_SELLER)
                buying.edit(rnd.choice(pur_ids), d)
            elif op == "del_sale" and inv_ids:
                i = rnd.choice(inv_ids); inv_ids.remove(i); sales.delete(i)
            elif op == "del_buy" and pur_ids:
                i = rnd.choice(pur_ids); pur_ids.remove(i); buying.delete(i)
            elif op == "adjust":
                p = rnd.choice(repo.stock.list_products(include_archived=True) or [None])
                if p: repo.stock.adjust(p["id"], rnd.choice(["add", "remove", "set"]), str(rnd.randint(0, 30)), "count")
            elif op == "archive":
                p = rnd.choice(repo.stock.list_products(include_archived=True) or [None])
                if p: repo.stock.set_archived(p["id"], not p["archived"])
            elif op == "reexport" and pur_ids:
                buying.reexport(rnd.choice(pur_ids))
        except (ValidationError, Exception) as exc:
            if "لا يوجد تغيير" in str(exc) or "الكمية" in str(exc): continue
            raise
        check(repo, f"seed {seed} step {step} {op}")
    return len(inv_ids), len(pur_ids)



class StockConsistencyTests(unittest.TestCase):
    def test_random_sessions_keep_stock_equal_to_the_documents(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        with mock.patch("facturedz.service.render_invoice_pdf", side_effect=lambda d, p, t=None: fake_pdf(p)):
            for seed in range(12):
                run(seed)


if __name__ == "__main__":
    unittest.main()
