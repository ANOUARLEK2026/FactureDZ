"""Number formats, and proof that the longest numbers still fit every place they are printed."""
import datetime as dt
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from tests._helpers import HomeTestCase
from tests.sample_data import make_invoice
from tests.test_uniform_output import FakeMeasurer
from facturedz import numbering as N
from facturedz import pdf_invoice as pi
from facturedz import template as T
from facturedz.config import NUMBER_MAX_DIGITS
from facturedz.paths import template_path


class FormatTests(unittest.TestCase):
    def test_format_and_parse_roundtrip(self):
        no = N.format_invoice_no(dt.date(2026, 10, 22), 7, 12)
        self.assertEqual(no, "1026000070012")
        self.assertEqual(N.parse_invoice_no(no), ("1026", 7, 12))
        self.assertEqual(N.format_invoice_no(dt.date(2027, 1, 5), 1, 1), "0127000010001")   # leading zero kept
        self.assertIsNone(N.parse_invoice_no("57"))                                            # legacy short number

    def test_customer_part_is_checked_never_truncated(self):
        self.assertEqual(N.format_invoice_no(dt.date(2026, 10, 1), N.MAX_CUSTOMER_ID, 1)[4:9], "99999")
        for bad in (0, N.MAX_CUSTOMER_ID + 1):
            with self.assertRaises(N.NumberingError):
                N.format_invoice_no(dt.date(2026, 10, 1), bad, 1)

    def test_monthly_counter_can_grow_and_stays_parseable(self):
        no = N.format_invoice_no(dt.date(2026, 10, 1), 3, 123456)
        self.assertEqual(N.parse_invoice_no(no), ("1026", 3, 123456))

    def test_random_serial_shape(self):
        values = {N.random_serial() for _ in range(2000)}
        self.assertGreater(len(values), 1990)
        self.assertTrue(all(len(v) == 12 and v[0] != "0" and v.isdigit() for v in values))


class FitTests(HomeTestCase):
    """13 digits is the normal length; 18 is the hard limit. Both must stay whole and readable."""
    CASES = [("1026000070012", "546034382916"), ("0127000010001", "123456789012"), ("9" * 14, "9" * 12),
             ("9" * NUMBER_MAX_DIGITS, "9" * NUMBER_MAX_DIGITS)]

    def test_pdf_lines_fit_their_boxes_at_a_readable_size(self):
        for invoice_no, serial_no in self.CASES:
            with self.subTest(invoice_no=invoice_no):
                data = make_invoice(3)
                data.update(invoice_no=invoice_no, serial_no=serial_no)
                layout = pi.build_layout(data, FakeMeasurer())
                seen = 0
                for op in layout.ops:
                    if isinstance(op, pi.Text) and (invoice_no in op.text or serial_no in op.text):
                        seen += 1
                        self.assertGreaterEqual(op.size, 9.0)
                        self.assertLessEqual(len(op.text) * op.size * 0.5, op.w + 0.01)
                self.assertGreaterEqual(seen, 2)

    def test_excel_shapes_never_split_a_number(self):
        for invoice_no, serial_no in self.CASES:
            with self.subTest(invoice_no=invoice_no):
                data = make_invoice(3)
                data.update(invoice_no=invoice_no, serial_no=serial_no)
                out = Path(tempfile.mkdtemp()) / "n.xlsm"
                T.fill_template(template_path(), out, data)
                with zipfile.ZipFile(out) as z:
                    root = ET.fromstring(z.read("xl/drawings/drawing1.xml"))
                found = 0
                for sp in root.iter(f"{T.XDR}sp"):
                    for p in sp.findall(f"{T.XDR}txBody/{T.A}p"):
                        text = T._paragraph_text(p)
                        for number in (invoice_no, serial_no):
                            if text.endswith(number):
                                found += 1
                                size = max(int(r.get("sz")) for r in p.iter(f"{T.A}rPr"))
                                self.assertGreaterEqual(size, 1000)
                                self.assertLessEqual(T._width_pt(number, size / 100), T._shape_box_pt(sp)[0])
                self.assertGreaterEqual(found, 2)


if __name__ == "__main__":
    unittest.main()
