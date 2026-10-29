"""Headless smoke test: runs the real UI code against a fake tkinter.

It cannot judge looks, but it executes every widget-construction path and the save/validation
flows, which catches NameErrors, wrong attribute names and bad call signatures.
"""
import sys
import types
import unittest
from pathlib import Path

from tests._helpers import HomeTestCase


class _Var:
    def __init__(self, master=None, value=None, name=None):
        self._v = "" if value is None else value
        self._cb = []

    def get(self):
        return self._v

    def set(self, v):
        self._v = v
        for cb in list(self._cb):
            cb()

    def trace_add(self, mode, cb):
        self._cb.append(lambda: cb())


class _Widget:
    def __init__(self, *a, **k):
        self.master = a[0] if a and isinstance(a[0], _Widget) else k.get("master")
        self._opts = dict(k)
        self._children = []
        if isinstance(self.master, _Widget):
            self.master._children.append(self)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None

    def winfo_children(self):
        return list(self._children)

    def get_children(self, *a):
        return ()

    def winfo_toplevel(self):
        return self

    def winfo_manager(self):
        return "pack"

    def winfo_width(self):
        return 800

    winfo_height = winfo_reqheight = winfo_reqwidth = winfo_rootx = winfo_rooty = winfo_width

    def cget(self, k):
        return self._opts.get(k, "")

    def configure(self, *a, **k):
        self._opts.update(k)

    config = configure

    def __setitem__(self, k, v):
        self._opts[k] = v

    def __getitem__(self, k):
        return self._opts.get(k)

    def get(self):
        return self._opts.get("_text", "")

    def set(self, v):
        self._opts["_text"] = v

    def state(self, spec=None):
        return ()

    def after(self, ms, fn=None, *a):
        return "id"

    def destroy(self):
        pass

    def invoke(self):
        pass

    def bbox(self, *a):
        return (0, 0, 1, 1)


def install_fake_tk():
    tk = types.ModuleType("tkinter")
    ttk = types.ModuleType("tkinter.ttk")
    font = types.ModuleType("tkinter.font")
    fd = types.ModuleType("tkinter.filedialog")
    for name in ("Tk", "Frame", "Label", "Canvas", "Toplevel", "Entry", "Button"):
        setattr(tk, name, type(name, (_Widget,), {}))
    for name in ("Frame", "Label", "Entry", "Button", "Combobox", "Checkbutton", "Treeview", "Scrollbar",
                 "Progressbar", "Style"):
        setattr(ttk, name, type(name, (_Widget,), {}))
    tk.StringVar = tk.BooleanVar = tk.IntVar = _Var
    tk.Variable = _Var
    tk.Misc = _Widget
    tk.Widget = _Widget
    tk.TclError = type("TclError", (Exception,), {})
    font.families = lambda root=None: ["Segoe UI"]
    font.nametofont = lambda n: _Widget()
    fd.asksaveasfilename = fd.askdirectory = fd.askopenfilename = lambda **k: ""
    tk.ttk, tk.font, tk.filedialog = ttk, font, fd
    saved = {k: sys.modules.get(k) for k in ("tkinter", "tkinter.ttk", "tkinter.font", "tkinter.filedialog")}
    sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.font": font, "tkinter.filedialog": fd})
    for m in [m for m in sys.modules if m.startswith("facturedz.ui")]:
        del sys.modules[m]
    return saved


def restore(saved):
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    for m in [m for m in sys.modules if m.startswith("facturedz.ui")]:
        del sys.modules[m]


class UiSmokeTests(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.saved = install_fake_tk()
        self.addCleanup(restore, self.saved)

    def make_app(self):
        from facturedz.db import Repository
        from facturedz.service import InvoiceService
        from facturedz.ui import dialogs
        from facturedz.ui.app import App
        from tests.test_service import FakePdfEngine
        self.dialogs = dialogs
        self.shown = []
        # dialogs would block on a real display: record them instead
        for name in ("info", "warn", "error"):
            setattr(dialogs, name, lambda parent, msg, title="", _n=name: self.shown.append((_n, msg)))
        dialogs.confirm = lambda *a, **k: True
        dialogs.choose = lambda parent, msg, buttons, **k: (self.shown.append(("choose", msg)), "close")[1]
        dialogs.BusyDialog = lambda *a, **k: types.SimpleNamespace(message="", close=lambda: None)

        def sync(root, work, done, failed):
            try:
                result = work()
            except BaseException as exc:
                failed(exc)
            else:
                done(result)
        dialogs.run_async = sync
        repo = Repository(self.home)
        repo.set_setting("output_dir", str(self.tmp / "out"))
        service = InvoiceService(repo, engines=[FakePdfEngine()])
        root = sys.modules["tkinter"].Tk()
        return App(root, repo, service, self.home), repo

    def fill_invoice(self, page, name="زبون"):
        page.vars["name"].set(name)
        rows = page.items.rows
        rows[0].desc.set("بطاطا"); rows[0].qty.set("١٠"); rows[0].price.set("45,5")
        rows[1].desc.set("موز"); rows[1].qty.set("2"); rows[1].price.set("100")

    def test_app_builds_and_all_pages_show(self):
        app, _ = self.make_app()
        for key in ("history", "customers", "settings", "invoice"):
            app.show_page(key)
        self.assertEqual(len(app.invoice_page.items.rows), 8)
        app.settings_page.run_diagnostics()
        app._startup_checks()

    def test_live_totals(self):
        app, _ = self.make_app()
        page = app.invoice_page
        self.fill_invoice(page)
        self.assertIn("655.00", page.subtotal_text.get())             # 10*45.5 + 2*100 (Arabic digits parsed)
        self.assertIn("دينار جزائري", page.words_text.get())
        page.vat.set("9")
        self.assertIn("TVA 9%", page.vat_label.get())

    def test_save_flow_creates_invoice_and_resets(self):
        app, repo = self.make_app()
        page = app.invoice_page
        self.fill_invoice(page)
        page.save()
        self.assertEqual([k for k, _ in self.shown if k == "error"], [], self.shown)
        inv = repo.list_invoices()
        self.assertEqual(len(inv), 1)
        self.assertTrue(Path(inv[0]["file_path"]).exists())
        self.assertEqual(repo.list_customers()[0]["name"], "زبون")      # auto-saved customer
        self.assertEqual(page.invoice_no.get(), "2")                     # form reset with next number

    def test_validation_errors_are_shown_not_raised(self):
        app, repo = self.make_app()
        page = app.invoice_page
        page.save()                                                      # empty form
        self.assertEqual(self.shown[-1][0], "error")
        self.assertEqual(repo.list_invoices(), [])
        page.vars["name"].set("x")
        page.items.rows[0].desc.set("a"); page.items.rows[0].qty.set("0"); page.items.rows[0].price.set("1")
        page.save()
        self.assertIn("الكمية", self.shown[-1][1])

    def test_history_customers_settings_actions(self):
        app, repo = self.make_app()
        page = app.invoice_page
        self.fill_invoice(page)
        page.save()
        app.history_page.refresh()
        app.customers_page.refresh()
        app.customers_page.vars["name"].set("زبون جديد")
        app.customers_page.save()
        self.assertEqual(len(repo.list_customers()), 2)
        invoice = repo.get_invoice(repo.list_invoices()[0]["id"])
        page.load_from_invoice(invoice)
        self.assertEqual(page.items.rows[0].desc.get(), "بطاطا")
        s = app.settings_page
        s.vat.set("9")
        s.save()
        self.assertEqual(repo.get_setting("vat_rate"), "9")
        s.vat.set("500")
        s.save()
        self.assertEqual(repo.get_setting("vat_rate"), "9")             # invalid value rejected

    def test_item_rows_add_remove_limits(self):
        app, _ = self.make_app()
        table = app.invoice_page.items
        for _ in range(20):
            table.add_row()
        self.assertEqual(len(table.rows), 16)
        table.rows[0].desc.set("a"); table.rows[1].desc.set("b")
        table.remove_row(table.rows[0])
        self.assertEqual(table.rows[0].desc.get(), "b")
        self.assertEqual(len(table.rows), 15)
        table.reset()
        self.assertEqual(len(table.rows), 8)


if __name__ == "__main__":
    unittest.main()
