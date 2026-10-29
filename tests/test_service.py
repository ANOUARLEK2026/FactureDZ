from unittest import mock
import unittest
from decimal import Decimal as D
from pathlib import Path

from tests._helpers import HomeTestCase
from facturedz.config import DEFAULT_SELLER
from facturedz.db import Repository
from facturedz.service import (InvoiceService, ValidationError, build_invoice, compute_items, compute_totals,
                               normalize_date)


def form(**over):
    base = {"invoice_no": "", "serial_no": "", "date": "10.10.2026", "vat_rate": "19", "payment": "cash",
            "customer": {"name": "زبون", "phone": "055"},
            "items": [{"description": "بطاطا", "quantity": "10", "price": "45,5"},
                      {"description": "", "quantity": "", "price": ""}]}
    base.update(over)
    return base


class ValidationTests(unittest.TestCase):
    def test_builds_canonical_invoice(self):
        d = build_invoice(form(), DEFAULT_SELLER)
        self.assertEqual((d["subtotal"], d["vat_amount"], d["total"]), (D("455.00"), D("86.45"), D("541.45")))
        self.assertEqual(len(d["items"]), 1)
        self.assertEqual(d["payment_method"], "cash")

    def test_arabic_digits_and_dates(self):
        d = build_invoice(form(invoice_no="٥٧", serial_no="٥٧", date="١٠/١٠/٢٠٢٦",
                               items=[{"description": "x", "quantity": "٢٫٥", "price": "١٠٠"}]), DEFAULT_SELLER)
        self.assertEqual((d["invoice_no"], d["invoice_date"], d["subtotal"]), ("57", "10.10.2026", D("250.00")))
        self.assertEqual(normalize_date("2026-1-5"), "05.01.2026")

    def test_errors_name_the_field(self):
        cases = [
            (form(customer={"name": " "}), "customer.name"),
            (form(items=[]), "items.1.description"),
            (form(items=[{"description": "", "quantity": "1", "price": "1"}]), "items.1.description"),
            (form(items=[{"description": "a", "quantity": "0", "price": "1"}]), "items.1.quantity"),
            (form(items=[{"description": "a", "quantity": "1", "price": "-1"}]), "items.1.price"),
            (form(items=[{"description": "a", "quantity": "x", "price": "1"}]), "items.1.quantity"),
            (form(date="31.02.2026"), "date"),
            (form(date="01.01.1990"), "date"),
            (form(vat_rate="101"), "vat_rate"),
            (form(vat_rate="abc"), "vat_rate"),
            (form(invoice_no="0"), "invoice_no"),
            (form(serial_no="1.5"), "serial_no"),
            (form(invoice_no="9" * 19), "invoice_no"),            # longer than the supported 18 digits
            (form(items=[{"description": "a", "quantity": "1e9", "price": "1"}]), "items.1.quantity"),
            (form(items=[{"description": "a", "quantity": "9999999999", "price": "9999999999"}]), "items"),
            (form(items=[{"description": str(i), "quantity": "1", "price": "1"} for i in range(17)]), "items"),
        ]
        for f, fld in cases:
            with self.subTest(field=fld, form=str(f)[:60]):
                with self.assertRaises(ValidationError) as cm:
                    build_invoice(f, DEFAULT_SELLER)
                self.assertEqual(cm.exception.field, fld)

    def test_numbers_are_optional_and_long_ones_keep_their_leading_zero(self):
        self.assertNotIn("invoice_no", build_invoice(form(), DEFAULT_SELLER))
        d = build_invoice(form(invoice_no="0127000010001", serial_no="123456789012"), DEFAULT_SELLER)
        self.assertEqual((d["invoice_no"], d["serial_no"]), ("0127000010001", "123456789012"))   # not turned into an int

    def test_live_typing_is_lenient(self):
        rows = [{"description": "a", "quantity": "2", "price": "3"}, {"description": "b", "quantity": "x", "price": "1"},
                {"description": "c", "quantity": "-1", "price": "1"}]
        items = compute_items(rows, strict=False)
        self.assertEqual(len(items), 1)
        self.assertEqual(compute_totals(items, D(19))["total"], D("7.14"))

    def test_legacy_payment_alias(self):
        self.assertEqual(build_invoice(form(payment="check"), DEFAULT_SELLER)["payment_method"], "cheque")
        self.assertEqual(build_invoice(form(payment="???"), DEFAULT_SELLER)["payment_method"], "cash")


def fake_pdf(path, template=None, ok=True):
    path.write_bytes(b"%PDF-1.4\n" + b"0" * 3000)
    return path


class PipelineTests(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.repo = Repository(self.home)
        self.svc = InvoiceService(self.repo)
        patcher = mock.patch("facturedz.service.render_invoice_pdf", side_effect=lambda d, p, t=None: fake_pdf(p))
        self.render = patcher.start()
        self.addCleanup(patcher.stop)

    def data(self, **over):
        return build_invoice(form(**over), DEFAULT_SELLER)

    def test_issue_writes_pdf_and_xlsm_and_records_invoice(self):
        res = self.svc.issue(self.data())
        self.assertTrue(res.pdf_ok and res.pdf_path.exists() and res.xlsm_path.exists())
        self.assertEqual(res.pdf_path.suffix, ".pdf")
        row = self.repo.list_invoices()[0]
        self.assertRegex(row["invoice_no"], r"^1026\d{5}0001$")            # MMYY + customer + number in the month
        self.assertRegex(row["serial_no"], r"^[1-9]\d{11}$")
        self.assertIn(row["invoice_no"], res.pdf_path.name)

    def test_numbers_are_assigned_per_customer_and_month(self):
        a = self.data(customer={"name": "أ"})
        b = self.data(customer={"name": "ب"})
        c = self.data(customer={"name": "أ"}, date="02.11.2026")
        for d in (a, b, c):
            self.svc.issue(d)
        self.assertEqual((a["invoice_no"], b["invoice_no"], c["invoice_no"]),
                         ("1026000010001", "1026000020002", "1126000010001"))
        self.assertEqual(len({d["serial_no"] for d in (a, b, c)}), 3)

    def test_pdf_and_excel_are_built_from_the_same_data(self):
        data = self.data()
        self.svc.issue(data)
        self.assertIs(self.render.call_args[0][0], data)

    def test_duplicate_number_is_refused_before_any_file_is_written(self):
        self.svc.issue(self.data(invoice_no="5", serial_no="5"))
        before = sorted(p.name for p in self.repo.default_output.rglob("*") if p.is_file())
        with self.assertRaises(ValidationError) as cm:
            self.svc.issue(self.data(invoice_no="5", serial_no="6"))
        self.assertEqual(cm.exception.field, "invoice_no")
        self.assertEqual(before, sorted(p.name for p in self.repo.default_output.rglob("*") if p.is_file()))

    def test_pdf_failure_still_saves_editable_excel_and_the_invoice(self):
        self.render.side_effect = RuntimeError("boom")
        res = self.svc.issue(self.data())
        self.assertFalse(res.pdf_ok)
        self.assertTrue(res.xlsm_path.exists())
        self.assertIn("boom", res.warnings[0])
        self.assertEqual(len(self.repo.list_invoices()), 1)
        # later the PDF can be rebuilt from the stored invoice
        self.render.side_effect = lambda d, p, t=None: fake_pdf(p)
        again = self.svc.reexport(self.repo.list_invoices()[0]["id"])
        self.assertTrue(again.pdf_ok and again.pdf_path.exists())

    def test_keep_xlsm_setting(self):
        self.repo.set_setting("keep_xlsm", "0")
        res = self.svc.issue(self.data())
        self.assertIsNone(res.xlsm_path)
        self.assertEqual([p.suffix for p in res.pdf_path.parent.iterdir()], [".pdf"])

    def test_same_name_never_overwrites_existing_file(self):
        a = self.svc.issue(self.data())
        b = self.svc.issue(self.data())
        self.assertNotEqual(a.pdf_path, b.pdf_path)

    def test_hostile_customer_name_is_safe_in_file_names(self):
        res = self.svc.issue(self.data(customer={"name": 'a<b>:"c/d\\\\e|f?*'}))
        self.assertTrue(res.pdf_path.exists())

    def test_failed_issue_gives_the_invoice_number_back(self):
        with mock.patch("facturedz.service.fill_template", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.svc.issue(self.data())
        self.assertEqual(self.repo.list_invoices(), [])
        d = self.data()
        self.svc.issue(d)
        self.assertTrue(d["invoice_no"].endswith("0001"))                  # no hole in the month's numbering

    def test_edit_keeps_numbers_replaces_files_and_logs(self):
        d = self.data()
        res = self.svc.issue(d)
        iid = res.invoice_id
        old_files = [res.pdf_path, res.xlsm_path]
        new = self.data(items=[{"description": "طماطم", "quantity": "20", "price": "50"}], payment="cheque")
        out = self.svc.edit(iid, new)
        got = self.repo.get_invoice(iid)
        self.assertEqual((got["invoice_no"], got["serial_no"]), (d["invoice_no"], d["serial_no"]))
        self.assertEqual((float(got["subtotal"]), got["payment_method"], got["items"][0]["description"]),
                         (1000.0, "cheque", "طماطم"))
        self.assertEqual(out.pdf_path, res.pdf_path)                        # same name -> replaced in place
        self.assertEqual([p.exists() for p in old_files], [True, True])
        self.assertEqual(len(list(self.repo.default_output.rglob("*.pdf"))), 1)
        with self.repo.connect() as con:
            self.assertEqual([r["action"] for r in con.execute("SELECT action FROM invoice_log")], ["edit"])

    def test_edit_that_changes_date_moves_the_files(self):
        res = self.svc.issue(self.data())
        out = self.svc.edit(res.invoice_id, self.data(date="05.12.2026"))
        self.assertIn("12", out.pdf_path.parent.name)
        self.assertEqual(out.pdf_path.parent.parent.name, "2026")
        self.assertFalse(res.pdf_path.exists() or res.xlsm_path.exists())   # nothing left behind in the old month
        self.assertEqual(len(list(self.repo.default_output.rglob("*.pdf"))), 1)
        self.assertTrue(self.repo.get_invoice(res.invoice_id)["invoice_no"].startswith("1026"))   # number unchanged

    def test_edit_failure_leaves_invoice_untouched(self):
        res = self.svc.issue(self.data())
        before = self.repo.get_invoice(res.invoice_id)
        with mock.patch("facturedz.service.fill_template", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.svc.edit(res.invoice_id, self.data(payment="term"))
        self.assertEqual(self.repo.get_invoice(res.invoice_id)["payment_method"], before["payment_method"])
        with self.assertRaises(ValidationError):
            self.svc.edit(9999, self.data())

    def test_delete_removes_files_and_never_reuses_numbers(self):
        a = self.data()
        res = self.svc.issue(a)
        removed = self.svc.delete(res.invoice_id)
        self.assertEqual(sorted(p.suffix for p in removed), [".pdf", ".xlsm"])
        self.assertFalse(res.pdf_path.exists() or res.xlsm_path.exists())
        self.assertEqual(self.repo.list_invoices(), [])
        b = self.data()
        self.svc.issue(b)
        self.assertTrue(b["invoice_no"].endswith("0002"))                   # 0001 is gone for good
        with self.assertRaises(ValidationError):                            # and cannot be typed back in either
            self.svc.issue(self.data(invoice_no=a["invoice_no"], serial_no="123456789012"))
        with self.assertRaises(ValidationError):
            self.svc.delete(res.invoice_id)

    def test_delete_record_only_keeps_files_and_refuses_foreign_paths(self):
        res = self.svc.issue(self.data())
        self.assertEqual(self.svc.delete(res.invoice_id, remove_files=False), [])
        self.assertTrue(res.pdf_path.exists() and res.xlsm_path.exists())
        victim = self.tmp / "important.txt"
        victim.write_text("keep me")
        self.assertIsNone(self.svc._remove_file(str(victim)))               # only .pdf/.xlsm are ever deleted
        self.assertTrue(victim.exists())

    def test_diagnostics_report(self):
        labels = [r[0] for r in self.svc.diagnostics()]
        self.assertIn("قالب Excel", labels)
        self.assertIn("سلامة قاعدة البيانات", labels)


if __name__ == "__main__":
    unittest.main()
