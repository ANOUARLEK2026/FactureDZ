"""Stock / purchase pages: the SAME scenarios run on a fake Qt (always) and on real PySide6 (when installed).

The fake run executes every line of the pages' logic (signals, models, dialogs, saving flows); the real run adds
Qt's own behaviour. Neither can judge how it looks - open the program once and look.
"""
import os
import sys
import unittest
from decimal import Decimal as D
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import fake_qt
from tests._helpers import HomeTestCase
from tests.test_service import fake_pdf

try:
    import PySide6  # noqa: F401
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


class UiScenarios:
    """Mixin: needs self.win (MainWindow), self.shown (dialog log) and self.answers (confirm answers)."""

    # ---- infrastructure ---------------------------------------------------------------------
    def build_window(self):
        from facturedz.db import Repository
        from facturedz.purchase_service import PurchaseService
        from facturedz.service import InvoiceService
        from facturedz.ui import dialogs
        from facturedz.ui.app import MainWindow
        self.repo = Repository(self.home)
        self.shown, self.answers = [], []
        self.dialogs = dialogs

        def record(kind):
            return lambda parent, message, title="", **k: self.shown.append((kind, message))

        def confirm(parent, message, title="", yes="نعم", no="لا", danger=False):
            self.shown.append(("confirm", message))
            return self.answers.pop(0) if self.answers else True

        def choose(parent, message, buttons, kind="info", title="", default=0):
            self.shown.append(("choose", message))
            return "close"

        for name, fn in (("info", record("info")), ("warn", record("warn")), ("error", record("error")),
                         ("confirm", confirm), ("choose", choose)):
            patcher = mock.patch.object(dialogs, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("facturedz.service.render_invoice_pdf", side_effect=lambda d, p, t=None: fake_pdf(p))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.win = MainWindow(self.repo, InvoiceService(self.repo), self.home, PurchaseService(self.repo))
        self.addCleanup(self.win.close)
        return self.win

    def messages(self, kind):
        return [m for k, m in self.shown if k == kind]

    def type_row(self, table, row, name, qty, price=""):
        model = table.items_model
        model.setData(model.index(row, 1), name)
        model.setData(model.index(row, 2), qty)
        if price:
            model.setData(model.index(row, 3), price)

    def stock_of(self, name):
        return self.repo.stock.find_product(name)["on_hand"]

    # ---- scenarios ----------------------------------------------------------------------------
    def test_every_page_opens(self):
        win = self.build_window()
        from facturedz.ui.app import PAGES
        for key, *_rest in PAGES:
            win.show_page(key)
            self.assertIs(win.stack.currentWidget(), win.pages[key])
        self.assertEqual(len(PAGES), 9)

    def test_new_product_with_opening_stock_shows_in_the_list_and_tiles(self):
        win = self.build_window()
        page = win.stock_page
        page.fields.edits["name"].setText("طماطم")
        page.fields.edits["sell_price"].setText("120")
        page.fields.edits["min_stock"].setText("50")
        page.opening.edits["opening_qty"].setText("200")
        page.opening.edits["opening_cost"].setText("80")
        page.save()
        self.assertEqual(self.messages("error"), [])
        p = self.repo.stock.find_product("طماطم")
        self.assertEqual((p["on_hand"], p["sell_price"], p["min_stock"]), (D(200), D(120), D(50)))
        self.assertEqual(page.model.rowCount(), 1)
        self.assertEqual(page.selected_id, p["id"])
        self.assertFalse(page.opening_box.isVisible())          # opening stock is only for a new product
        self.assertEqual(page.t_count.value.text(), "1")
        self.assertEqual(page.t_value.value.text(), "16,000.00")
        # duplicate name -> a message, nothing created
        page.clear()
        page.fields.edits["name"].setText("  طماطم ")
        page.save()
        self.assertEqual(len(self.messages("error")), 1)
        self.assertEqual(len(self.repo.stock.list_products()), 1)

    def test_filters_and_search(self):
        win = self.build_window()
        stock = self.repo.stock
        stock.save_product({"name": "أ", "min_stock": "10"}, opening_qty="50")
        stock.save_product({"name": "ب", "min_stock": "10"}, opening_qty="5")
        stock.save_product({"name": "ج"})
        arch = stock.save_product({"name": "د"}, opening_qty="1")
        stock.set_archived(arch, True)
        page = win.stock_page
        page.refresh()
        counts = {}
        for key in ("all", "ok", "low", "out", "archived"):
            page.filters.buttons[key].click()
            counts[key] = page.model.rowCount()
        self.assertEqual(counts, {"all": 3, "ok": 1, "low": 1, "out": 1, "archived": 1})
        page.filters.buttons["all"].click()
        page.search.setText("ب")
        self.assertEqual(page.proxy.rowCount(), 1)
        page.search.setText("zzz")
        self.assertEqual(page.proxy.rowCount(), 0)
        self.assertTrue(page.empty.isVisible())

    def test_adjust_stock_through_the_dialog(self):
        win = self.build_window()
        from facturedz.ui.stock_dialogs import AdjustDialog
        pid = self.repo.stock.save_product({"name": "بصل"}, opening_qty="40")
        page = win.stock_page
        page.refresh()
        page.view.selectRow(0)
        self.assertEqual(page.selected_id, pid)
        values = {"mode": "set", "qty": "35.5", "reason": "spoilage", "note": "فاسد"}
        with mock.patch.object(AdjustDialog, "exec", return_value=1), \
                mock.patch.object(AdjustDialog, "values", return_value=values):
            page.adjust()
        self.assertEqual(self.messages("error"), [])
        self.assertEqual(self.stock_of("بصل"), D("35.5"))
        self.assertEqual(self.repo.stock.list_movements(kind="adjust")[0]["reason"], "spoilage")
        self.assertEqual(page.hmodel.rowCount(), 2)                      # opening + adjustment in the card's history

    def test_adjust_dialog_preview_logic(self):
        self.build_window()
        from facturedz.ui.stock_dialogs import preview
        self.assertEqual(preview(D(10), "set", "4"), (D(-6), D(4)))
        self.assertEqual(preview(D(10), "add", "2.5"), (D("2.5"), D("12.5")))
        self.assertEqual(preview(D(10), "remove", "12"), (D(-12), D(-2)))
        for bad in (("set", "10"), ("add", "0"), ("add", ""), ("remove", "x"), ("add", "-1")):
            self.assertIsNone(preview(D(10), *bad), bad)

    def test_archive_and_delete_from_the_page(self):
        win = self.build_window()
        pid = self.repo.stock.save_product({"name": "كوسة"}, opening_qty="3")
        empty = self.repo.stock.save_product({"name": "فارغ"})
        page = win.stock_page
        page.refresh()
        page.selected_id = pid
        page.toggle_archive()
        self.assertTrue(self.repo.stock.get_product(pid)["archived"])
        page.selected_id = pid
        page.delete()                                                     # has movements -> refused, not crashed
        self.assertIsNotNone(self.repo.stock.get_product(pid))
        self.assertEqual(len(self.messages("info")), 1)
        page.selected_id = empty
        page.delete()
        self.assertIsNone(self.repo.stock.get_product(empty))

    def test_import_products_from_old_invoices(self):
        win = self.build_window()
        from facturedz.config import DEFAULT_SELLER
        from facturedz.service import build_invoice
        data = build_invoice({"date": "10.10.2026", "vat_rate": "19", "payment": "cash", "customer": {"name": "ز"},
                              "items": [{"description": "جزر", "quantity": "5", "price": "70"}]}, DEFAULT_SELLER)
        win.service.issue(data)
        page = win.stock_page
        page.refresh()
        self.assertTrue(page.import_button.isVisible())
        page.import_products()
        self.assertEqual([p["name"] for p in self.repo.stock.list_products()], ["جزر"])
        self.assertEqual(page.model.rowCount(), 1)
        self.assertFalse(page.import_button.isVisible())

    # -- purchases -------------------------------------------------------------------------------
    def fill_purchase(self, supplier, rows):
        page = self.win.purchase_page
        page.fields.edits["name"].setText(supplier)
        for i, (name, qty, price) in enumerate(rows):
            self.type_row(page.table, i, name, qty, price)
        return page

    def test_purchase_flow_adds_stock_creates_products_and_files_everything(self):
        win = self.build_window()
        self.repo.stock.save_product({"name": "طماطم"}, opening_qty="10", opening_cost="100")
        win.purchase_page.refresh_catalog()
        page = self.fill_purchase("مزرعة الأمل", [("طماطم", "50", "80"), ("خيار", "20", "60")])
        model = page.table.items_model
        self.assertEqual(model.stock_info(0), ("الحالي 10", "muted"))
        self.assertEqual(model.stock_info(1), ("صنف جديد في المخزون", "new"))
        self.assertIn("5,200.00", page.lbl_ht.text())                     # 50*80 + 20*60
        page.save()
        self.assertEqual(self.messages("error"), [])
        confirm = self.messages("confirm")
        self.assertEqual(len(confirm), 1)
        self.assertIn("خيار", confirm[0])                                  # asks before creating a new product
        self.assertEqual((self.stock_of("طماطم"), self.stock_of("خيار")), (D(60), D(20)))
        row = self.repo.purchases.list_purchases()[0]
        self.assertTrue(row["purchase_no"].startswith("A"))
        self.assertEqual(win.purchases_page.model.rowCount(), 1)          # history refreshed
        self.assertEqual(win.stock_page.t_count.value.text(), "2")
        self.assertEqual(len(self.repo.purchases.list_suppliers()), 1)    # supplier file auto-saved
        self.assertEqual(page.fields.edits["name"].text(), "")            # page reset for the next one
        self.assertEqual(len(self.messages("choose")), 1)

    def test_purchase_declined_confirmation_changes_nothing(self):
        self.build_window()
        page = self.fill_purchase("م", [("صنف جديد", "5", "10")])
        self.answers = [False]
        page.save()
        self.assertEqual(self.repo.purchases.list_purchases(), [])
        self.assertEqual(self.repo.stock.list_products(), [])

    def test_purchase_validation_focuses_and_explains(self):
        self.build_window()
        page = self.fill_purchase("", [("أ", "1", "1")])
        page.save()
        self.assertEqual(len(self.messages("error")), 1)
        page.fields.edits["name"].setText("م")
        self.type_row(page.table, 1, "ب", "0", "5")
        page.save()
        self.assertEqual(len(self.messages("error")), 2)
        self.assertEqual(self.repo.purchases.list_purchases(), [])

    def test_edit_and_delete_a_purchase_from_the_pages(self):
        win = self.build_window()
        page = self.fill_purchase("مورد", [("برتقال", "100", "90")])
        self.answers = [True]
        page.save()
        record = self.repo.purchases.get_purchase(self.repo.purchases.list_purchases()[0]["id"])
        page.load_for_edit(record)
        self.assertEqual(page.editing_id, record["id"])
        self.assertTrue(page.banner.isVisible())
        self.assertEqual(page.invoice_no.text(), record["invoice_no"])
        self.type_row(page.table, 0, "برتقال", "60", "95")
        page.save()
        self.assertEqual(self.stock_of("برتقال"), D(60))
        self.assertIsNone(page.editing_id)
        # sell most of it, then deleting the purchase must warn about the negative stock
        from facturedz.config import DEFAULT_SELLER
        from facturedz.service import build_invoice
        sale = build_invoice({"date": "10.10.2026", "vat_rate": "19", "payment": "cash", "customer": {"name": "ز"},
                              "items": [{"description": "برتقال", "quantity": "50", "price": "120"}]}, DEFAULT_SELLER)
        win.service.issue(sale)
        history = win.purchases_page
        history.refresh()
        history.view.selectRow(0)
        before = len(self.shown)
        self.dialogs.choose = lambda parent, message, *a, **k: (self.shown.append(("choose", message)), None)[1]
        history.delete_invoice()                                           # the user cancels the dialog
        self.assertIn("برتقال", self.shown[before][1])
        self.assertIn("سالب", self.shown[before][1])
        self.assertEqual(len(self.repo.purchases.list_purchases()), 1)

    def test_suppliers_page_saves_and_feeds_the_purchase_page(self):
        win = self.build_window()
        page = win.suppliers_page
        page.fields.edits["name"].setText("مزرعة النخيل")
        page.fields.edits["phone"].setText("0550")
        page.save()
        self.assertEqual(self.messages("error"), [])
        self.assertEqual(page.model.rowCount(), 1)
        page.use_in_invoice()
        self.assertIs(win.stack.currentWidget(), win.purchase_page)
        self.assertEqual(win.purchase_page.fields.edits["name"].text(), "مزرعة النخيل")
        self.assertEqual(win.purchase_page.combo.currentText(), "مزرعة النخيل")

    # -- sales ---------------------------------------------------------------------------------------
    def fill_sale(self, customer, rows):
        page = self.win.invoice_page
        page.fields.edits["name"].setText(customer)
        for i, (name, qty, price) in enumerate(rows):
            self.type_row(page.table, i, name, qty, price)
        return page

    def test_sale_links_products_shows_availability_and_takes_stock(self):
        win = self.build_window()
        self.repo.stock.save_product({"name": "بطاطا", "sell_price": "60", "min_stock": "20"}, opening_qty="100")
        win.invoice_page.refresh_catalog()
        page = self.fill_sale("زبون", [("بطاطا", "30", "")])
        model = page.table.items_model
        self.assertEqual(model.raw()[0]["price"], "60")                    # the usual price was offered
        self.assertEqual(model.stock_info(0), ("متاح 100", "ok"))
        self.type_row(page.table, 0, "بطاطا", "85")
        self.assertEqual(model.stock_info(0), ("يتبقى 15", "warn"))        # below the alert threshold
        self.type_row(page.table, 0, "بطاطا", "30")
        page.save()
        self.assertEqual(self.messages("error"), [])
        self.assertEqual(self.messages("confirm"), [])                     # nothing to warn about
        self.assertEqual(self.stock_of("بطاطا"), D(70))
        self.assertEqual(win.stock_page.model.rowCount(), 1)

    def test_selling_more_than_available_asks_and_can_be_cancelled(self):
        win = self.build_window()
        self.repo.stock.save_product({"name": "موز"}, opening_qty="10")
        win.invoice_page.refresh_catalog()
        page = self.fill_sale("زبون", [("موز", "15", "200")])
        self.assertEqual(page.table.items_model.stock_info(0), ("ناقص 5", "bad"))
        self.answers = [False]
        page.save()
        self.assertIn("المطلوب 15", self.messages("confirm")[0])
        self.assertEqual(self.repo.list_invoices(), [])
        self.assertEqual(self.stock_of("موز"), D(10))
        self.answers = [True]
        page.save()
        self.assertEqual(self.stock_of("موز"), D(-5))

    def test_two_rows_of_the_same_product_are_added_together(self):
        win = self.build_window()
        self.repo.stock.save_product({"name": "ليمون"}, opening_qty="10")
        win.invoice_page.refresh_catalog()
        page = self.fill_sale("زبون", [("ليمون", "6", "10"), ("ليمون", "6", "10")])
        self.assertEqual(page.table.items_model.stock_info(0)[1], "bad")
        self.assertEqual(page.table.items_model.stock_info(1)[1], "bad")

    def test_unlinked_item_warning_and_the_setting_that_turns_it_off(self):
        win = self.build_window()
        page = self.fill_sale("زبون", [("خدمة نقل", "1", "500")])
        self.assertEqual(page.table.items_model.stock_info(0), ("غير مرتبط بالمخزون", "muted"))
        self.assertEqual(page.table.items_model.unlinked_names(), ["خدمة نقل"])
        self.answers = [False]
        page.save()
        self.assertEqual(self.repo.list_invoices(), [])
        win.settings_page.warn_unlinked.setChecked(False)
        win.settings_page.save()
        self.assertEqual(self.repo.get_setting("warn_unlinked"), "0")
        before = len(self.messages("confirm"))
        page.save()
        self.assertEqual(len(self.messages("confirm")), before)            # no more question
        self.assertEqual(len(self.repo.list_invoices()), 1)

    def test_editing_a_sale_credits_its_own_quantity(self):
        win = self.build_window()
        self.repo.stock.save_product({"name": "جزر"}, opening_qty="50")
        win.invoice_page.refresh_catalog()
        page = self.fill_sale("زبون", [("جزر", "40", "70")])
        page.save()
        self.assertEqual(self.stock_of("جزر"), D(10))
        record = self.repo.get_invoice(self.repo.list_invoices()[0]["id"])
        page.load_for_edit(record)
        model = page.table.items_model
        self.assertEqual(model.stock_info(0), ("متاح 50", "ok"))           # 10 left + the 40 this invoice holds
        self.type_row(page.table, 0, "جزر", "45")
        self.assertEqual(model.stock_info(0)[1], "ok")
        page.save()
        self.assertEqual(self.messages("confirm"), [])
        self.assertEqual(self.stock_of("جزر"), D(5))

    def test_old_invoice_edit_shows_no_stock_figures_and_asks_nothing(self):
        win = self.build_window()
        self.repo.stock.save_product({"name": "قديم", "sell_price": "10"}, opening_qty="5")
        win.invoice_page.refresh_catalog()
        page = self.fill_sale("زبون", [("قديم", "2", "10")])
        page.save()
        import json
        with self.repo.connect(write=True) as con:
            old = json.loads(con.execute("SELECT items_json FROM invoices").fetchone()[0])
            for i in old:
                del i["product_id"]
            con.execute("UPDATE invoices SET items_json=?", (json.dumps(old, ensure_ascii=False),))
            con.execute("DELETE FROM stock_movements WHERE kind='sale'")
        record = self.repo.get_invoice(self.repo.list_invoices()[0]["id"])
        page.load_for_edit(record)
        self.assertEqual(page.table.items_model.stock_info(0)[1], "muted")
        self.assertTrue(page.banner.text().find("قبل تفعيل المخزون") >= 0)
        self.type_row(page.table, 0, "قديم", "99")                       # far more than the 5 in stock
        before = len(self.messages("confirm"))
        page.save()
        self.assertEqual(len(self.messages("confirm")), before)
        self.assertEqual(self.stock_of("قديم"), D(5))

    def test_deleting_a_sale_from_history_returns_the_goods(self):
        win = self.build_window()
        self.repo.stock.save_product({"name": "تفاح"}, opening_qty="20")
        win.invoice_page.refresh_catalog()
        self.fill_sale("زبون", [("تفاح", "8", "150")])
        win.invoice_page.save()
        self.assertEqual(self.stock_of("تفاح"), D(12))
        history = win.history_page
        history.refresh()
        history.view.selectRow(0)
        self.dialogs.choose = lambda *a, **k: "record"
        history.delete_invoice()
        self.assertEqual(self.stock_of("تفاح"), D(20))

    # -- movements ------------------------------------------------------------------------------------
    def test_movements_page_filters(self):
        win = self.build_window()
        a = self.repo.stock.save_product({"name": "أ"}, opening_qty="10")
        self.repo.stock.save_product({"name": "ب"}, opening_qty="5")
        self.repo.stock.adjust(a, "remove", "3", "spoilage")
        page = win.movements_page
        page.refresh()
        self.assertEqual(page.model.rowCount(), 3)
        self.assertEqual((page.t_in.value.text(), page.t_out.value.text()), ("15", "3"))
        page.show_for_product(a, "أ")
        self.assertEqual(page.model.rowCount(), 2)
        self.assertTrue(page.chip.isVisible())
        page.clear_product()
        self.assertEqual(page.model.rowCount(), 3)
        page.kind.setCurrentIndex(page.kind.findText("تعديل / جرد"))
        self.assertEqual(page.model.rowCount(), 1)
        page.kind.setCurrentIndex(0)
        page.period.setCurrentIndex(page.period.findText("اليوم"))
        self.assertEqual(page.model.rowCount(), 3)                         # all created today
        page.search.setText("ب")
        self.assertEqual(page.model.rowCount(), 1)

    def test_reload_all_after_a_restore_keeps_every_page_consistent(self):
        win = self.build_window()
        self.repo.stock.save_product({"name": "ثوم"}, opening_qty="9")
        win.reload_all()
        self.assertEqual(win.stock_page.model.rowCount(), 1)


class FakeQtUiTests(UiScenarios, HomeTestCase):
    def setUp(self):
        super().setUp()
        saved = fake_qt.install()
        self.addCleanup(fake_qt.uninstall, saved)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class RealQtUiTests(UiScenarios, HomeTestCase):
    def setUp(self):
        super().setUp()
        from PySide6.QtWidgets import QApplication
        from facturedz.ui import theme
        self.app = QApplication.instance() or QApplication([])
        theme.apply(self.app)


if __name__ == "__main__":
    unittest.main()
