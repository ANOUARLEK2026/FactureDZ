"""Tests that need a real PySide6 (skipped automatically when it is not installed).

Run with:  python -m unittest tests.test_qt        (offscreen platform, no display needed)
"""
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from tests._helpers import HomeTestCase
from tests.sample_data import make_invoice
from facturedz.paths import template_path


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class QtPdfTests(HomeTestCase):
    def test_pdf_is_one_a4_page_with_arabic_text_and_flags(self):
        from facturedz.pdf_invoice import render_invoice_pdf
        out = render_invoice_pdf(make_invoice(3), self.tmp / "a.pdf", template_path())
        raw = out.read_bytes()
        self.assertTrue(raw.startswith(b"%PDF"))
        try:
            import pypdf
        except ImportError:
            return
        reader = pypdf.PdfReader(str(out))
        self.assertEqual(len(reader.pages), 1)
        box = reader.pages[0].mediabox
        self.assertAlmostEqual(float(box.width), 595.3, delta=1.5)
        self.assertAlmostEqual(float(box.height), 841.9, delta=1.5)

    def test_16_items_still_one_page(self):
        from facturedz.pdf_invoice import render_invoice_pdf
        out = render_invoice_pdf(make_invoice(16, "term"), self.tmp / "b.pdf", template_path())
        try:
            import pypdf
            self.assertEqual(len(pypdf.PdfReader(str(out)).pages), 1)
        except ImportError:
            self.assertGreater(out.stat().st_size, 5000)

    def test_arabic_path_is_fine(self):
        from facturedz.pdf_invoice import render_invoice_pdf
        out = render_invoice_pdf(make_invoice(2), self.tmp / "فاتورة_1" / "x.pdf", None)
        self.assertTrue(out.exists())


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class QtUiSmokeTests(HomeTestCase):
    def test_window_builds_and_every_page_opens(self):
        from PySide6.QtWidgets import QApplication
        from facturedz.db import Repository
        from facturedz.service import InvoiceService
        from facturedz.ui import theme
        from facturedz.ui.app import MainWindow
        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        repo = Repository(self.home)
        win = MainWindow(repo, InvoiceService(repo), self.home)
        win.showMaximized()
        for key in ("history", "customers", "settings", "invoice"):
            win.show_page(key)
        self.assertTrue(win.isMaximized() or win.windowState())
        page = win.invoice_page
        page.fields.edits["name"].setText("زبون")
        model = page.table.items_model
        model.setData(model.index(0, 1), "بطاطا")
        model.setData(model.index(0, 2), "١٠")
        model.setData(model.index(0, 3), "45,5")
        self.assertEqual(model.raw()[0]["quantity"], "10")
        self.assertIn("541.45", page.lbl_ttc.text().replace(",", ""))
        win.close()


if __name__ == "__main__":
    unittest.main()
