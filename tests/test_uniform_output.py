"""The Excel file and the PDF must say exactly the same thing (both read invoice_format)."""
import re
import unittest
import xml.etree.ElementTree as ET
import zipfile

from tests._helpers import HomeTestCase
from tests.sample_data import make_invoice
from facturedz import invoice_format as fmt
from facturedz import pdf_invoice as pi
from facturedz import template as T
from facturedz.paths import template_path


class FakeMeasurer:
    """Deterministic metrics so the pure layout can be tested without Qt."""
    def text_width(self, text, size, bold, title_font=False):
        return len(text) * size * 0.5

    def wrapped_height(self, text, size, bold, width, title_font=False):
        per_line = max(1, int(width / (size * 0.5)))
        return -(-max(len(text), 1) // per_line) * size * 1.2


def pdf_texts(data):
    layout = pi.build_layout(data, FakeMeasurer())
    return layout, [op.text for op in layout.ops if isinstance(op, pi.Text)]


def drawing_text(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("xl/drawings/drawing1.xml"))
    return [T._paragraph_text(p) for p in root.iter(f"{T.A}p")]


def sheet_strings(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    return {c.get("r"): "".join(t.text or "" for t in c.iter(f"{T.M}t")) for c in root.iter(f"{T.M}c")}


class UniformOutputTests(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.data = make_invoice(3, "cheque")
        self.data["invoice_no"], self.data["serial_no"] = "1026000070012", "483920174651"   # real-length numbers
        self.xlsm = self.tmp / "u.xlsm"
        T.fill_template(template_path(), self.xlsm, self.data)
        self.layout, self.pdf = pdf_texts(self.data)
        self.boxes = drawing_text(self.xlsm)
        self.cells = sheet_strings(self.xlsm)

    def test_every_labelled_line_is_identical_in_both(self):
        for lines in (fmt.seller_lines(self.data["seller"]), fmt.buyer_lines(self.data["customer"])):
            for _key, label, value in lines:
                text = fmt.labelled(label, value)
                self.assertIn(text, self.pdf)
                self.assertIn(text, [b.strip() for b in self.boxes], text)

    def test_date_serial_invoice_and_title_match(self):
        for text in (fmt.date_line(self.data), fmt.serial_line(self.data), fmt.invoice_line(self.data)):
            self.assertIn(text, [b.strip() for b in self.boxes])
            # The PDF may put the label above a long number; either way every word of it is printed.
            label, _sep, value = text.partition(":")
            self.assertTrue(text in self.pdf or (label.strip() + " :" in self.pdf and value.strip() in self.pdf), text)
        name, sub = fmt.title_lines(self.data["seller"])
        self.assertIn(name, self.pdf)
        self.assertIn(name, [b.strip() for b in self.boxes])

    def test_table_headers_and_totals_labels_match(self):
        for col, text in zip("CDEFG", fmt.COLUMN_HEADERS):
            self.assertEqual(self.cells[f"{col}20"].strip(), text)
            self.assertIn(text, self.pdf)
        for text in (fmt.LABEL_HT, fmt.vat_label(self.data), fmt.LABEL_TTC):
            self.assertIn(text, self.pdf)
            self.assertIn(text, self.cells.values())

    def test_money_format_is_the_same(self):
        with zipfile.ZipFile(self.xlsm) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        self.assertIn(f'#,##0.00\\ &quot;{fmt.CURRENCY}&quot;', styles)
        self.assertNotIn(".دج", styles)
        self.assertIn(fmt.fmt_money(self.data["total"]), self.pdf)
        self.assertEqual(fmt.fmt_money(11712.71), "11,712.71 دج")

    def test_line_items_appear_in_both(self):
        for item in self.data["items"]:
            self.assertIn(item["description"], self.pdf)
            self.assertIn(fmt.fmt_number(item["line_total"]), self.pdf)
        self.assertEqual(self.cells["D21"], self.data["items"][0]["description"])


class LayoutTests(unittest.TestCase):
    def test_one_page_for_every_item_count(self):
        for n in (1, 3, 8, 9, 12, 16):
            layout = pi.build_layout(make_invoice(n), FakeMeasurer())
            self.assertLessEqual(layout.scale, 1.0)
            _x, bottom = layout.point(pi.CX, layout.bottom)
            self.assertLessEqual(bottom, pi.PAGE_H, n)
            self.assertGreater(layout.scale, 0.8, n)

    def test_everything_stays_inside_the_page(self):
        layout = pi.build_layout(make_invoice(16), FakeMeasurer())
        for op in layout.ops:
            if isinstance(op, (pi.Card, pi.Rect, pi.Text, pi.Flag)):
                x0, y0 = layout.point(op.x, op.y)
                x1, y1 = layout.point(op.x + op.w, op.y + op.h)
                self.assertTrue(0 <= x0 and x1 <= pi.PAGE_W and 0 <= y0 and y1 <= pi.PAGE_H, op)

    def test_blank_rows_pad_to_eight(self):
        _layout, texts = pdf_texts(make_invoice(2))
        self.assertIn("08", texts)
        self.assertNotIn("09", texts)

    def test_seller_is_the_right_panel_and_buyer_the_left(self):
        layout = pi.build_layout(make_invoice(3), FakeMeasurer())
        seller_x = next(op.x for op in layout.ops if isinstance(op, pi.Text) and "الهاتف" in op.text)
        buyer_x = next(op.x for op in layout.ops if isinstance(op, pi.Text) and op.text.startswith("المطلوب من"))
        self.assertGreater(seller_x, pi.CX)           # right half of the page: our details
        self.assertLess(buyer_x, pi.CX)               # left half: the buyer

    def test_date_invoice_serial_share_one_row_right_to_left(self):
        data = make_invoice(3)
        data["invoice_no"], data["serial_no"] = "1026000070012", "483920174651"
        layout = pi.build_layout(data, FakeMeasurer())
        cards = [op for op in layout.ops if isinstance(op, pi.Card) and op.h < 60]
        self.assertEqual(len(cards), 3)
        self.assertEqual(len({round(c.y, 3) for c in cards}), 1)           # one row
        by_x = sorted(cards, key=lambda c: -c.x)                           # right -> left

        def words_in(card):
            return " ".join(op.text for op in layout.ops if isinstance(op, pi.Text)
                            and card.x <= op.x and op.x + op.w <= card.x + card.w + 0.01
                            and card.y <= op.y <= card.y + card.h)
        self.assertIn("في:", words_in(by_x[0]))
        self.assertIn("رقم الفاتورة", words_in(by_x[1]))
        self.assertIn("الرقم التسلسلي", words_in(by_x[2]))
        self.assertIn("483920174651", words_in(by_x[2]))                   # long number stays whole

    def test_payment_box_sits_right_of_its_word(self):
        layout = pi.build_layout(make_invoice(2, "cash"), FakeMeasurer())
        boxes = sorted((op for op in layout.ops if isinstance(op, pi.Rect) and op.stroke and op.w == 15.0),
                       key=lambda r: -r.x)
        words = {op.text: op for op in layout.ops if isinstance(op, pi.Text)}
        self.assertEqual(len(boxes), 4)
        for box, (_key, label) in zip(boxes, fmt.payment_options()):
            self.assertLess(words[label].x + words[label].w, box.x + 0.01)   # the word is to the box's left

    def test_payment_tick_follows_method(self):
        def ticks(method):
            layout = pi.build_layout(make_invoice(2, method), FakeMeasurer())
            return sum(1 for op in layout.ops if isinstance(op, pi.Line) and op.width == 1.6)
        for method in ("cash", "cheque", "term", "bank"):
            self.assertEqual(ticks(method), 2)        # one tick = two strokes, never more than one box


if __name__ == "__main__":
    unittest.main()
