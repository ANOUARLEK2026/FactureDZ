"""Stock + purchase invoices: products, movements, sale/purchase integration, numbering, export wording."""
import datetime as dt
import sqlite3
import unittest
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal as D
from unittest import mock

from tests._helpers import HomeTestCase
from tests.test_service import fake_pdf
from tests.test_uniform_output import FakeMeasurer, drawing_text
from facturedz import pdf_invoice as pi
from facturedz import template as T
from facturedz.config import DEFAULT_SELLER
from facturedz.db import Repository
from facturedz.inventory import Catalog, StockError, from_milli, stock_status, to_milli
from facturedz.purchase_service import PurchaseService, build_purchase
from facturedz.purchasing import to_export_data
from facturedz.service import InvoiceService, ValidationError, build_invoice


def sale_form(items, customer="زبون", date="10.10.2026"):
    return {"date": date, "vat_rate": "19", "payment": "cash", "customer": {"name": customer}, "items": items}


def purchase_form(items, supplier="مورد", date="10.10.2026"):
    return {"date": date, "vat_rate": "19", "payment": "cash", "supplier": {"name": supplier, "phone": "055"},
            "items": items}


def row(name, qty, price, pid=None):
    r = {"description": name, "quantity": str(qty), "price": str(price)}
    if pid is not None:
        r["product_id"] = pid
    return r


class StockTestCase(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.repo = Repository(self.home)
        self.sales = InvoiceService(self.repo)
        self.buying = PurchaseService(self.repo)
        for target in ("facturedz.service.render_invoice_pdf",):
            patcher = mock.patch(target, side_effect=lambda d, p, t=None: fake_pdf(p))
            patcher.start()
            self.addCleanup(patcher.stop)

    # helpers -------------------------------------------------------------------
    def product(self, name, opening=None, cost=None, **fields):
        return self.repo.stock.save_product(dict(name=name, **fields), opening_qty=opening, opening_cost=cost)

    def on_hand(self, name):
        return self.repo.stock.find_product(name)["on_hand"]

    def sell(self, items, **kw):
        data = build_invoice(sale_form(items, **kw), DEFAULT_SELLER)
        return self.sales.issue(data), data

    def buy(self, items, **kw):
        data = build_purchase(purchase_form(items, **kw), DEFAULT_SELLER)
        return self.buying.issue(data), data


class QuantityHelperTests(unittest.TestCase):
    def test_grams_roundtrip_is_exact(self):
        self.assertEqual(to_milli(D("12.345")), 12345)
        self.assertEqual(to_milli("0.0005"), 1)                      # half up
        self.assertEqual(to_milli(D("0.0004")), 0)
        self.assertEqual(from_milli(1500), D("1.5"))
        self.assertEqual(sum(from_milli(to_milli(D("0.1"))) for _ in range(10)), D(1))   # no float drift

    def test_status(self):
        self.assertEqual(stock_status(D(-1), D(0)), "negative")
        self.assertEqual(stock_status(D(0), D(5)), "out")
        self.assertEqual(stock_status(D(5), D(5)), "low")
        self.assertEqual(stock_status(D(6), D(5)), "ok")
        self.assertEqual(stock_status(D(1), D(0)), "ok")             # no threshold -> never "low"


class ProductTests(StockTestCase):
    def test_create_with_opening_stock_and_cost(self):
        pid = self.product("طماطم", opening="120.5", cost="80", sell_price="120", min_stock="20")
        p = self.repo.stock.get_product(pid)
        self.assertEqual((p["on_hand"], p["sell_price"], p["min_stock"]), (D("120.5"), D("120.00"), D(20)))
        self.assertEqual((p["last_cost"], p["avg_cost"], p["value"]), (D("80.00"), D("80.00"), D("9640.00")))
        moves = self.repo.stock.list_movements(product_id=pid)
        self.assertEqual([(m["kind"], m["qty"]) for m in moves], [("initial", D("120.5"))])

    def test_names_are_unique_after_arabic_normalisation(self):
        self.product("تفاح أحمر")
        for clash in ("تفاح أحمر", "  تفاح   أحمر ", "تفّاح أحمر", "تفاح احمر"):
            with self.assertRaises(StockError, msg=clash):
                self.product(clash)

    def test_validation_messages(self):
        with self.assertRaises(StockError):
            self.product("  ")
        with self.assertRaises(StockError):
            self.product("x" * 91)
        with self.assertRaises(StockError):
            self.product("ب", sell_price="abc")
        with self.assertRaises(StockError):
            self.product("ب", sell_price="-1")
        with self.assertRaises(StockError):
            self.product("ب", opening="-3")

    def test_update_keeps_stock_and_ignores_opening_values(self):
        pid = self.product("جزر", opening="10")
        self.repo.stock.save_product({"name": "جزر طازج", "sell_price": "75"}, product_id=pid, opening_qty="999")
        p = self.repo.stock.get_product(pid)
        self.assertEqual((p["name"], p["on_hand"], p["sell_price"]), ("جزر طازج", D(10), D("75.00")))
        with self.assertRaises(StockError):
            self.repo.stock.save_product({"name": "x"}, product_id=9999)

    def test_delete_only_without_movements_else_archive(self):
        empty = self.product("فارغ")
        used = self.product("مستعمل", opening="1")
        self.repo.stock.delete_product(empty)
        self.assertIsNone(self.repo.stock.get_product(empty))
        with self.assertRaises(StockError):
            self.repo.stock.delete_product(used)
        self.repo.stock.set_archived(used, True)
        self.assertEqual([p["name"] for p in self.repo.stock.list_products()], [])
        self.assertEqual([p["name"] for p in self.repo.stock.list_products(include_archived=True)], ["مستعمل"])
        self.assertNotIn("مستعمل", self.repo.stock.catalog().names())
        self.assertIsNotNone(self.repo.stock.catalog().find("مستعمل"))          # but sales can still link to it

    def test_import_from_invoices_creates_products_without_touching_stock(self):
        self.repo.stock  # tables exist before any product
        self.sell([row("بطاطا", 10, 60), row("موز", 5, 220)])
        # invoices issued BEFORE products existed: remove what the sale linked, as if the data were old
        self.assertEqual(self.repo.stock.list_products(), [])
        self.assertEqual(self.repo.stock.import_from_invoices(), 2)
        self.assertEqual(self.repo.stock.import_from_invoices(), 0)               # idempotent
        prices = {p["name"]: (p["sell_price"], p["on_hand"]) for p in self.repo.stock.list_products()}
        self.assertEqual(prices, {"بطاطا": (D("60.00"), D(0)), "موز": (D("220.00"), D(0))})

    def test_summary(self):
        self.product("أ", opening="10", cost="100", min_stock="20")      # low
        self.product("ب", opening="5", cost="10")                        # ok
        self.product("ج")                                                # out
        s = self.repo.stock.summary()
        self.assertEqual((s["count"], s["attention"], s["negative"], s["value"]), (3, 2, 0, D("1050.00")))


class AdjustmentTests(StockTestCase):
    def test_set_add_remove(self):
        pid = self.product("بصل", opening="50")
        self.assertEqual(self.repo.stock.adjust(pid, "set", "42.5", "count"), D("-7.5"))
        self.assertEqual(self.repo.stock.adjust(pid, "add", "10", "return_in"), D(10))
        self.assertEqual(self.repo.stock.adjust(pid, "remove", "2.5", "spoilage", "فاسد"), D("-2.5"))
        self.assertEqual(self.repo.stock.get_product(pid)["on_hand"], D(50))
        kinds = [m["kind"] for m in self.repo.stock.list_movements(product_id=pid)]
        self.assertEqual(kinds, ["adjust", "adjust", "adjust", "initial"])

    def test_errors(self):
        pid = self.product("ثوم", opening="5")
        for mode, qty in (("set", "5"), ("add", "0"), ("remove", "0"), ("add", "-1"), ("add", "x"), ("zzz", "1")):
            with self.subTest(mode=mode, qty=qty), self.assertRaises(StockError):
                self.repo.stock.adjust(pid, mode, qty)
        with self.assertRaises(StockError):
            self.repo.stock.adjust(9999, "add", "1")
        self.assertEqual(self.on_hand("ثوم"), D(5))

    def test_counting_below_zero_is_allowed_to_record_reality(self):
        pid = self.product("خيار", opening="5")
        self.repo.stock.adjust(pid, "remove", "8")
        self.assertEqual(self.repo.stock.get_product(pid)["status"], "negative")


class SaleIntegrationTests(StockTestCase):
    def test_sale_takes_stock_out_and_links_by_name(self):
        pid = self.product("بطاطا", opening="100", cost="40")
        res, data = self.sell([row("بطاطا", "30.5", 60), row("خدمة نقل", 1, 500)])
        self.assertEqual(self.on_hand("بطاطا"), D("69.5"))
        stored = self.repo.get_invoice(res.invoice_id)
        self.assertEqual([i["product_id"] for i in stored["items"]], [pid, None])
        move = self.repo.stock.list_movements(product_id=pid, kind="sale")[0]
        self.assertEqual((move["kind"], move["qty"], move["ref_no"]), ("sale", D("-30.5"), data["invoice_no"]))

    def test_explicit_product_link_wins_over_the_text(self):
        pid = self.product("طماطم", opening="10")
        res, _ = self.sell([row("طماطم عنقودية", 4, 100, pid=pid)])
        self.assertEqual(self.on_hand("طماطم"), D(6))
        self.assertEqual(self.repo.get_invoice(res.invoice_id)["items"][0]["product_id"], pid)

    def test_edit_replaces_movements_and_delete_restores_stock(self):
        self.product("جزر", opening="100")
        res, _ = self.sell([row("جزر", 40, 75)])
        self.assertEqual(self.on_hand("جزر"), D(60))
        edited = build_invoice(sale_form([row("جزر", 10, 75)]), DEFAULT_SELLER)
        self.sales.edit(res.invoice_id, edited)
        self.assertEqual(self.on_hand("جزر"), D(90))
        self.assertEqual(len(self.repo.stock.list_movements(kind="sale")), 1)
        self.sales.delete(res.invoice_id)
        self.assertEqual(self.on_hand("جزر"), D(100))
        self.assertEqual([m["kind"] for m in self.repo.stock.list_movements()], ["initial"])

    def test_editing_can_change_the_product(self):
        self.product("أ", opening="10")
        self.product("ب", opening="10")
        res, _ = self.sell([row("أ", 4, 1)])
        self.sales.edit(res.invoice_id, build_invoice(sale_form([row("ب", 3, 1)]), DEFAULT_SELLER))
        self.assertEqual((self.on_hand("أ"), self.on_hand("ب")), (D(10), D(7)))

    def test_date_of_the_invoice_dates_the_movement(self):
        self.product("ليمون", opening="10")
        self.sell([row("ليمون", 1, 1)], date="03.09.2026")
        self.assertEqual(self.repo.stock.list_movements(kind="sale")[0]["moved_on"], "2026-09-03")

    def test_shortages_are_reported_not_blocked(self):
        pid = self.product("فلفل", opening="10")
        items = [{"description": "فلفل", "quantity": D(8), "product_id": pid},
                 {"description": "فلفل", "quantity": D(5), "product_id": pid}]      # two rows, same product
        short = self.repo.stock.shortages(items)
        self.assertEqual([(s["name"], s["requested"], s["available"]) for s in short], [("فلفل", D(13), D(10))])
        self.assertEqual(self.repo.stock.shortages(items[:1]), [])
        res, _ = self.sell([row("فلفل", 12, 1)])                 # the service itself never refuses a sale
        self.assertEqual(self.on_hand("فلفل"), D(-2))
        # while editing that invoice its own 12 kg are available again
        self.assertEqual(self.repo.stock.shortages([{"quantity": D(10), "product_id": pid}],
                                                   exclude_invoice_id=res.invoice_id), [])
        self.assertEqual(self.repo.stock.sale_credit(res.invoice_id), {pid: D(12)})

    def test_editing_an_old_invoice_never_creates_a_stock_movement(self):
        """Invoices issued before stock tracking (no product_id key stored) must stay out of stock for ever."""
        import json
        res, _ = self.sell([row("قديمة", 10, 100)])
        self.product("قديمة", opening="50")
        with self.repo.connect(write=True) as con:                       # make it look like a pre-3.2 row
            old = json.loads(con.execute("SELECT items_json FROM invoices").fetchone()[0])
            for i in old:
                del i["product_id"]
            con.execute("UPDATE invoices SET items_json=?", (json.dumps(old, ensure_ascii=False),))
            con.execute("DELETE FROM stock_movements WHERE kind='sale'")
        self.assertFalse(self.repo.get_invoice(res.invoice_id)["stock_tracked"])
        self.sales.edit(res.invoice_id, build_invoice(sale_form([row("قديمة", 30, 100)]), DEFAULT_SELLER))
        self.assertEqual(self.on_hand("قديمة"), D(50))
        self.assertFalse(self.repo.get_invoice(res.invoice_id)["stock_tracked"])    # still old-style afterwards
        self.sales.delete(res.invoice_id)
        self.assertEqual(self.on_hand("قديمة"), D(50))
        fresh, _ = self.sell([row("قديمة", 5, 1)])                        # a NEW invoice is tracked as usual
        self.assertTrue(self.repo.get_invoice(fresh.invoice_id)["stock_tracked"])
        self.assertEqual(self.on_hand("قديمة"), D(45))

    def test_unlinked_items_never_touch_stock(self):
        self.product("موز", opening="10")
        self.sell([row("برقوق", 100, 1)])
        self.assertEqual(self.on_hand("موز"), D(10))
        self.assertEqual(len(self.repo.stock.list_movements()), 1)


class PurchaseTests(StockTestCase):
    def test_purchase_adds_stock_creates_products_and_costs(self):
        self.product("طماطم", opening="10", cost="100")
        today = dt.date.today()
        res, data = self.buy([row("طماطم", 50, 80), row("خيار جديد", 20, 60)], date=today.strftime("%d.%m.%Y"))
        self.assertEqual(res.created_products, ["خيار جديد"])
        self.assertEqual(self.on_hand("طماطم"), D(60))
        self.assertEqual(self.on_hand("خيار جديد"), D(20))
        tomato = self.repo.stock.find_product("طماطم")
        self.assertEqual(tomato["last_cost"], D("80.00"))
        self.assertEqual(tomato["avg_cost"], D("83.33"))              # (10*100 + 50*80) / 60
        self.assertRegex(data["invoice_no"], r"^A" + today.strftime("%m%y") + r"\d{5}0001$")
        self.assertRegex(data["serial_no"], r"^[1-9]\d{11}$")
        self.assertTrue(res.xlsm_path.exists() and res.pdf_path.exists())
        self.assertIn("Achats", res.pdf_path.parts)
        self.assertIn("فاتورة_شراء", res.pdf_path.name)

    def test_numbers_per_supplier_and_month_and_never_reused(self):
        a, da = self.buy([row("س", 1, 1)], supplier="أ")
        b, db = self.buy([row("س", 1, 1)], supplier="ب")
        c, dc = self.buy([row("س", 1, 1)], supplier="أ", date="02.11.2026")
        self.assertEqual((da["invoice_no"], db["invoice_no"], dc["invoice_no"]),
                         ("A1026000010001", "A1026000020002", "A1126000010001"))
        self.buying.delete(b.invoice_id)
        _, dd = self.buy([row("س", 1, 1)], supplier="ب")
        self.assertEqual(dd["invoice_no"], "A1026000020003")             # the deleted number is not handed out again
        # sale and purchase counters are independent
        _, sale = self.sell([row("ش", 1, 1)])
        self.assertTrue(sale["invoice_no"].startswith("1026") and not sale["invoice_no"].startswith("A"))

    def test_serials_are_unique_across_sales_and_purchases(self):
        serials = set()
        for _ in range(12):
            serials.add(self.sell([row("ش", 1, 1)])[1]["serial_no"])
            serials.add(self.buy([row("ش", 1, 1)])[1]["serial_no"])
        self.assertEqual(len(serials), 24)

    def test_failed_issue_gives_the_number_back(self):
        with mock.patch.object(self.repo.purchases, "add_purchase", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.buy([row("س", 1, 1)])
        _, data = self.buy([row("س", 1, 1)])
        self.assertTrue(data["invoice_no"].endswith("0001"))

    def test_edit_replaces_movements_keeps_numbers(self):
        res, data = self.buy([row("برتقال", 100, 90)])
        edited = build_purchase(purchase_form([row("برتقال", 60, 95), row("ليمون", 5, 50)]), DEFAULT_SELLER)
        out = self.buying.edit(res.invoice_id, edited)
        self.assertEqual(out.created_products, ["ليمون"])
        self.assertEqual((self.on_hand("برتقال"), self.on_hand("ليمون")), (D(60), D(5)))
        stored = self.repo.purchases.get_purchase(res.invoice_id)
        self.assertEqual((stored["invoice_no"], stored["serial_no"]), (data["invoice_no"], data["serial_no"]))
        self.assertEqual(self.repo.stock.find_product("برتقال")["last_cost"], D("95.00"))
        with self.repo.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM purchase_log WHERE action='edit'").fetchone()[0], 1)

    def test_delete_removes_stock_and_logs(self):
        res, _ = self.buy([row("تفاح", 40, 150)])
        self.buying.delete(res.invoice_id)
        self.assertEqual(self.on_hand("تفاح"), D(0))
        self.assertIsNone(self.repo.purchases.get_purchase(res.invoice_id))
        with self.repo.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM purchase_log WHERE action='delete'").fetchone()[0], 1)

    def test_negative_after_warns_before_removing_stock_already_sold(self):
        res, _ = self.buy([row("موز", 50, 200)])
        self.sell([row("موز", 45, 300)])
        self.assertEqual(self.repo.stock.negative_after("purchase", res.invoice_id, []),
                         [{"product_id": self.repo.stock.find_product("موز")["id"], "name": "موز", "after": D(-45)}])
        pid = self.repo.stock.find_product("موز")["id"]
        smaller = [{"product_id": pid, "quantity": D(46)}]
        self.assertEqual(self.repo.stock.negative_after("purchase", res.invoice_id, smaller), [])
        smaller = [{"product_id": pid, "quantity": D(20)}]
        self.assertEqual(self.repo.stock.negative_after("purchase", res.invoice_id, smaller)[0]["after"], D(-25))

    def test_same_new_product_on_two_lines_is_created_once(self):
        res, _ = self.buy([row("خيار", 10, 50), row(" خيار ", 5, 60), row("جزر", 1, 1)])
        self.assertEqual(res.created_products, ["خيار", "جزر"])
        self.assertEqual(self.on_hand("خيار"), D(15))
        edited = build_purchase(purchase_form([row("كوسة", 2, 5), row("كوسة", 3, 5)]), DEFAULT_SELLER)
        self.buying.edit(res.invoice_id, edited)                              # same rule when editing
        self.assertEqual((self.on_hand("كوسة"), self.on_hand("خيار")), (D(5), D(0)))

    def test_buying_an_archived_product_brings_it_back(self):
        pid = self.product("كوسة", opening="1")
        self.repo.stock.set_archived(pid, True)
        self.buy([row("كوسة", 5, 10)])
        self.assertFalse(self.repo.stock.get_product(pid)["archived"])

    def test_reexport_and_files(self):
        res, _ = self.buy([row("نعناع", 3, 20)])
        again = self.buying.reexport(res.invoice_id)
        self.assertTrue(again.pdf_ok and again.pdf_path == res.pdf_path)

    def test_validation(self):
        base = purchase_form([row("أ", 1, 1)])
        cases = [(dict(base, supplier={"name": " "}), "supplier.name"),
                 (dict(base, items=[]), "items.1.description"),
                 (dict(base, items=[row("أ" * 91, 1, 1)]), "items.1.description"),
                 (dict(base, items=[row("أ", 0, 1)]), "items.1.quantity"),
                 (dict(base, items=[row("أ", 1, -1)]), "items.1.price"),
                 (dict(base, date="31.02.2026"), "date"),
                 (dict(base, vat_rate="500"), "vat_rate")]
        for f, field in cases:
            with self.subTest(field=field), self.assertRaises(ValidationError) as cm:
                build_purchase(f, DEFAULT_SELLER)
            self.assertEqual(cm.exception.field, field)
        built = build_purchase(base, DEFAULT_SELLER)
        self.assertEqual((built["subtotal"], built["total"]), (D("1.00"), D("1.19")))

    def test_supplier_files(self):
        sid = self.repo.purchases.save_supplier({"name": "مزرعة الخير", "phone": "055"})
        with self.assertRaises(ValueError):
            self.repo.purchases.save_supplier({"name": " "})
        self.assertEqual(self.repo.purchases.save_supplier({"name": "مزرعة الخير", "rc": "1"}), sid)   # same name = update
        self.assertEqual(self.repo.purchases.get_supplier(sid)["rc"], "1")
        self.buy([row("أ", 1, 1)], supplier="مزرعة الخير")                                           # re-uses the file
        self.assertEqual(len(self.repo.purchases.list_suppliers()), 1)
        self.assertEqual(len(self.repo.purchases.list_suppliers("خير")), 1)
        self.assertEqual(len(self.repo.purchases.list_suppliers("zzz")), 0)
        self.repo.purchases.delete_supplier(sid)
        self.assertEqual(self.repo.purchases.list_suppliers(), [])

    def test_list_and_search(self):
        self.buy([row("أ", 1, 1)], supplier="مورد الأول")
        self.buy([row("أ", 1, 1)], supplier="مورد الثاني")
        self.assertEqual(len(self.repo.purchases.list_purchases()), 2)
        self.assertEqual([r["supplier_name"] for r in self.repo.purchases.list_purchases("الثاني")], ["مورد الثاني"])


class MovementReportTests(StockTestCase):
    def test_filters(self):
        self.product("أ", opening="10")
        self.buy([row("أ", 5, 10)], date="05.09.2026")
        self.sell([row("أ", 3, 20)], date="07.10.2026")
        everything = self.repo.stock.list_movements()
        self.assertEqual(len(everything), 3)
        self.assertEqual([m["kind"] for m in self.repo.stock.list_movements(kind="sale")], ["sale"])
        sept = self.repo.stock.list_movements(date_from="2026-09-01", date_to="2026-09-30")
        self.assertEqual([m["kind"] for m in sept], ["purchase"])
        self.assertEqual(len(self.repo.stock.list_movements(search="ا")), 3)
        self.assertEqual(self.repo.stock.list_movements(search="لا يوجد"), [])
        self.assertEqual(len(self.repo.stock.list_movements(limit=2)), 2)


class SchemaTests(StockTestCase):
    def test_backup_restore_keeps_stock(self):
        self.product("ملفوف", opening="9")
        backup = self.repo.backup("manual", force=True)
        self.repo.stock.adjust(self.repo.stock.find_product("ملفوف")["id"], "add", "1")
        self.repo.restore_backup(backup)
        self.assertEqual(self.on_hand("ملفوف"), D(9))

    def test_old_v3_database_gains_the_new_tables_without_losing_invoices(self):
        res, _ = self.sell([row("قديم", 1, 1)])
        with sqlite3.connect(str(self.repo.db_path)) as con:
            for table in ("stock_movements", "products", "purchase_log", "purchases", "suppliers"):
                con.execute(f"DROP TABLE {table}")
            con.execute("PRAGMA user_version=3")
        again = Repository(self.home)
        self.assertEqual(len(again.list_invoices()), 1)
        self.assertEqual(again.stock.list_products(), [])
        self.assertEqual(again.purchases.list_purchases(), [])
        self.assertEqual(again.stock.import_from_invoices(), 1)


class PurchaseExportTests(StockTestCase):
    def test_purchase_layout_is_the_supplier_invoice_to_us_with_purchase_wording(self):
        _, data = self.buy([row("طماطم", 5, 80)], supplier="مزرعة الأمل")
        export = to_export_data(self.repo.purchases.get_purchase(1))
        self.assertEqual(export["seller"]["business_name"], "مزرعة الأمل")
        self.assertEqual(export["customer"]["name"], DEFAULT_SELLER["business_name"])
        self.assertEqual(export["seller"]["city"], DEFAULT_SELLER["city"])
        layout = pi.build_layout(export, FakeMeasurer())
        texts = [op.text for op in layout.ops if isinstance(op, pi.Text)]
        self.assertTrue(any(t.startswith("رقم فاتورة الشراء") for t in texts), texts)
        self.assertTrue(any(data["invoice_no"] in t for t in texts), texts)
        self.assertIn("مزرعة الأمل", texts)
        self.assertEqual(layout.title, f"فاتورة شراء {data['invoice_no']}")
        self.assertEqual(layout.scale, 1.0)

    def test_sale_layout_wording_is_unchanged(self):
        from tests.sample_data import make_invoice
        layout = pi.build_layout(make_invoice(2), FakeMeasurer())
        texts = [op.text for op in layout.ops if isinstance(op, pi.Text)]
        self.assertIn("رقم الفاتورة : 57", texts)
        self.assertEqual(layout.title, "فاتورة 57")

    def test_excel_file_carries_the_purchase_wording(self):
        res, data = self.buy([row("طماطم", 5, 80)], supplier="مزرعة الأمل")
        texts = " | ".join(drawing_text(res.xlsm_path))
        self.assertIn("رقم فاتورة الشراء", texts)
        self.assertIn("مزرعة الأمل", texts)
        self.assertIn(data["invoice_no"], texts)
        self.assertIn(DEFAULT_SELLER["business_name"], texts)


class CatalogTests(unittest.TestCase):
    def test_find_is_arabic_aware(self):
        cat = Catalog([{"id": 1, "name": "تفاح أحمر", "archived": False}, {"id": 2, "name": "قديم", "archived": True}])
        self.assertEqual(cat.find("  تفّاح   احمر ")["id"], 1)
        self.assertIsNone(cat.find("غير موجود"))
        self.assertIsNone(cat.find(""))
        self.assertEqual(cat.names(), ["تفاح أحمر"])
        self.assertEqual(cat.get(2)["name"], "قديم")
        self.assertIsNone(cat.get(None))


if __name__ == "__main__":
    unittest.main()
