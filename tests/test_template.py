import re
import unittest
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal as D

from tests._helpers import HomeTestCase
from tests.sample_data import make_invoice
from facturedz import template as T
from facturedz.paths import template_path

M, X, A = T.M, T.XDR, T.A


def read(path, name):
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode("utf-8")


def cell_map(path):
    root = ET.fromstring(read(path, "xl/worksheets/sheet1.xml"))
    out = {}
    for c in root.iter(f"{M}c"):
        f, v = c.find(f"{M}f"), c.find(f"{M}v")
        t = "".join(x.text or "" for x in c.iter(f"{M}t"))
        out[c.get("r")] = {"f": f.text if f is not None else None, "v": v.text if v is not None else None, "t": t}
    return out


def print_area(path):
    m = re.search(r'<definedName name="_xlnm\.Print_Area"[^>]*>([^<]+)</definedName>',
                  read(path, "xl/workbook.xml"))
    if not m:
        raise AssertionError("Print_Area not found")
    return m.group(1)


def shape_text(path, shape_id):
    root = ET.fromstring(read(path, "xl/drawings/drawing1.xml"))
    for sp in root.iter(f"{X}sp"):
        if T._shape_id(sp) == shape_id:
            return [T._paragraph_text(p) for p in sp.iter(f"{A}p")]
    raise AssertionError(shape_id)


class TemplateTests(HomeTestCase):
    def fill(self, name="out.xlsm", **kw):
        out = self.tmp / name
        data = make_invoice(kw.pop("n", 3), kw.pop("payment", "cheque"))
        T.fill_template(template_path(), out, data, **kw)
        return out, data

    def test_shipped_template_is_valid(self):
        self.assertEqual(T.validate_template(template_path()), [])

    def test_rows_are_detected_from_the_template(self):
        with zipfile.ZipFile(template_path()) as z:
            parts = {n: z.read(n) for n in z.namelist()}
        sheet = T.SheetEditor(T._parse(parts["xl/worksheets/sheet1.xml"])[0], T._shared_strings(parts))
        L = T.detect_layout(sheet)
        self.assertEqual((L.header_row, L.first_item_row, L.item_rows, L.totals_row), (20, 21, 8, 29))
        self.assertEqual((L.title_row, L.words_row, L.payment_row), (33, 34, 35))

    def test_export_survives_rows_added_above_the_table(self):
        """Nothing is hard-coded: two extra rows above the table move every target cell by two."""
        with zipfile.ZipFile(template_path()) as z:
            infos, parts = z.infolist(), {n: z.read(n) for n in z.namelist()}
        root, text = T._parse(parts["xl/worksheets/sheet1.xml"])
        T.SheetEditor(root, T._shared_strings(parts)).insert_rows_after(1, 2)
        parts["xl/worksheets/sheet1.xml"] = T._serialize(root, text)
        moved = self.tmp / "moved.xlsm"
        T._write_zip(moved, infos, parts)
        self.assertEqual(T.validate_template(moved), [])
        out = self.tmp / "moved_out.xlsm"
        T.fill_template(moved, out, make_invoice(3, "cash"))
        c = cell_map(out)
        self.assertEqual(c["D23"]["t"], "موز")                                   # 21 + 2
        self.assertEqual(c["G31"]["f"], "ROUND(SUM(G23:G30),2)")
        self.assertIn("دينار", c["C36"]["t"])

    def test_sample_line_left_in_template_is_replaced(self):
        out, _ = self.fill(n=1)
        c = cell_map(out)
        self.assertEqual(c["D21"]["t"], "موز")
        self.assertEqual(c["E21"]["v"], "10")
        self.assertEqual(c["F21"]["v"], "353.87")
        self.assertEqual(c["D22"]["t"], "")

    def test_package_is_well_formed_and_keeps_macro_and_artwork(self):
        out, _ = self.fill()
        with zipfile.ZipFile(out) as z:
            self.assertIsNone(z.testzip())
            names = set(z.namelist())
        self.assertIn("xl/vbaProject.bin", names)
        self.assertIn("xl/media/image1.jpeg", names)
        self.assertNotIn("xl/calcChain.xml", names)
        self.assertNotIn("calcChain", read(out, "[Content_Types].xml"))
        self.assertNotIn("calcChain", read(out, "xl/_rels/workbook.xml.rels"))
        for part in ("xl/worksheets/sheet1.xml", "xl/drawings/drawing1.xml", "xl/workbook.xml"):
            ET.fromstring(read(out, part))                                  # parses
        sheet = read(out, "xl/worksheets/sheet1.xml")
        for prefix in re.search(r'mc:Ignorable="([^"]+)"', sheet).group(1).split():
            self.assertIn(f"xmlns:{prefix}=", sheet, f"Excel rejects undeclared Ignorable prefix {prefix}")
        self.assertIn('fullCalcOnLoad="1"', read(out, "xl/workbook.xml"))

    def test_untouched_parts_are_byte_identical(self):
        out, _ = self.fill()
        with zipfile.ZipFile(template_path()) as a, zipfile.ZipFile(out) as b:
            for name in ("xl/vbaProject.bin", "xl/media/image1.jpeg", "xl/theme/theme1.xml"):
                self.assertEqual(a.read(name), b.read(name), name)
            # styles.xml differs in exactly one place: the currency format ('.دج' -> 'دج', same as the PDF)
            old, new = a.read("xl/styles.xml").decode(), b.read("xl/styles.xml").decode()
            self.assertEqual(old.replace("&quot;.دج&quot;", "&quot;دج&quot;"), new)

    def test_cells_formulas_and_cached_values(self):
        out, data = self.fill(n=3)
        c = cell_map(out)
        self.assertEqual(c["D21"]["t"], "موز")
        self.assertEqual((c["E22"]["v"], c["F22"]["v"]), ("11", "354.87"))
        self.assertEqual(c["G21"]["f"], "ROUND(E21*F21,2)")
        self.assertEqual(c["G23"]["v"], str(data["items"][2]["line_total"]))
        self.assertEqual(c["D24"]["t"], "")                                  # unused row cleared
        self.assertIsNone(c["E24"]["v"])
        self.assertEqual(c["G24"]["v"], "0")
        self.assertEqual(c["G29"]["f"], "ROUND(SUM(G21:G28),2)")
        self.assertEqual(c["G30"]["f"], "ROUND(G29*19/100,2)")               # not the template's hard-coded 0.19
        self.assertEqual(c["G31"]["v"], str(data["total"]))
        self.assertEqual(c["E30"]["t"], "TVA 19%")
        self.assertIsNone(c["C34"]["f"])                                     # macro MontantDA() replaced by text
        self.assertIn("دينار جزائري", c["C34"]["t"])

    def test_vat_rate_is_not_hardcoded(self):
        out = self.tmp / "v.xlsm"
        T.fill_template(template_path(), out, make_invoice(2, "cash", D("9.5")))
        c = cell_map(out)
        self.assertEqual(c["E30"]["t"], "TVA 9.5%")
        self.assertEqual(c["G30"]["f"], "ROUND(G29*9.5/100,2)")

    def test_text_boxes_keep_labels_and_get_values(self):
        out, _ = self.fill()
        seller = "\n".join(shape_text(out, "14"))
        self.assertIn("الرقم الجبائي : 19739010246618203900", seller)
        self.assertIn("رقم الهاتف : 0550 38 76 95", seller)
        buyer = shape_text(out, "18")
        self.assertTrue(any(p.startswith("المطلوب من : مؤسسة النور") for p in buyer))
        self.assertFalse(any("\uf07d" in p for p in buyer), "Symbol-font placeholders must be gone")
        self.assertIn("رقم الحساب البنكي :", buyer)                          # empty value keeps its label
        self.assertEqual(shape_text(out, "5"), ["الرقم التسلسلي : 57"])
        self.assertEqual(shape_text(out, "17"), ["رقم الفاتورة : 57"])
        self.assertEqual(shape_text(out, "15"), ["الوادي في: 10.10.2026"])
        self.assertEqual(shape_text(out, "16")[0], "أولاد الحاجة صليحة للتجارة")

    def test_rich_formatting_survives(self):
        """Each filled paragraph keeps the formatting of the template's own first run in it."""
        out, _ = self.fill()
        new_root = ET.fromstring(read(out, "xl/drawings/drawing1.xml"))
        old_root = ET.fromstring(read(template_path(), "xl/drawings/drawing1.xml"))

        def paragraphs(root, sp_id):
            sp = next(s for s in root.iter(f"{X}sp") if T._shape_id(s) == sp_id)
            return sp.findall(f"{X}txBody/{A}p")

        for sp_id in ("14", "18"):
            for old_p, new_p in zip(paragraphs(old_root, sp_id), paragraphs(new_root, sp_id)):
                old_runs, new_runs = old_p.findall(f"{A}r"), new_p.findall(f"{A}r")
                if not old_runs:
                    continue
                self.assertEqual(len(new_runs), 1)
                o, n = old_runs[0].find(f"{A}rPr"), new_runs[0].find(f"{A}rPr")
                self.assertEqual(o.get("b"), n.get("b"))                                  # bold kept
                self.assertEqual([c.tag for c in o], [c.tag for c in n])                  # fill/fonts kept
                self.assertIsNone(n.find(f"{A}sym"))                                      # Symbol placeholder gone
                self.assertTrue(900 <= int(n.get("sz")) <= int(o.get("sz")))              # only ever shrinks

    def test_payment_tick_matches_method(self):
        for method, tick_id in T.PAYMENT_BOX_IDS.items():
            out, _ = self.fill(f"p_{method}.xlsm", payment=method)
            for m, sp_id in T.PAYMENT_BOX_IDS.items():
                text = "".join(shape_text(out, sp_id))
                self.assertEqual(text, T.TICK if m == method else "", f"{method} -> box {m}")

    def test_extra_rows_shift_everything_below(self):
        out, data = self.fill(n=12)
        c = cell_map(out)
        self.assertEqual(c["D32"]["t"], data["items"][11]["description"])    # row 21+11
        self.assertEqual(c["G33"]["f"], "ROUND(SUM(G21:G32),2)")             # HT moved 29 -> 33
        self.assertEqual(c["G35"]["v"], str(data["total"]))
        self.assertIn("دينار", c["C38"]["t"])                                # words 34 -> 38
        self.assertIn('ref="C38:G38"', read(out, "xl/worksheets/sheet1.xml"))
        # The print area is user-editable, so derive the expected range from the
        # template instead of hard-coding the stock one.
        old_area, new_area = print_area(template_path()), print_area(out)
        self.assertEqual(new_area.split("!", 1)[0], old_area.split("!", 1)[0])
        old_rows = re.search(r"\$[A-Z]+\$(\d+):\$[A-Z]+\$(\d+)$", old_area)
        new_rows = re.search(r"\$[A-Z]+\$(\d+):\$[A-Z]+\$(\d+)$", new_area)
        self.assertIsNotNone(old_rows)
        self.assertIsNotNone(new_rows)
        delta = 12 - T.TEMPLATE_ITEM_ROWS
        last_item_row = 21 + T.TEMPLATE_ITEM_ROWS - 1
        for old_row, new_row in zip(map(int, old_rows.groups()), map(int, new_rows.groups())):
            self.assertEqual(new_row, old_row + delta if old_row > last_item_row else old_row)
        rows = [int(r) for r in re.findall(r'<row r="(\d+)"', read(out, "xl/worksheets/sheet1.xml"))]
        self.assertEqual(rows, sorted(rows))
        self.assertEqual(len(rows), len(set(rows)))
        # payment boxes follow their row (template row 35 -> 39 => 0-based 38)
        drawing = ET.fromstring(read(out, "xl/drawings/drawing1.xml"))
        box_rows = {T._shape_id(sp): int(a.find(f"{X}from/{X}row").text)
                    for a in drawing for sp in a.iter(f"{X}sp") if T._shape_id(sp) in T.PAYMENT_BOX_IDS.values()}
        self.assertEqual(set(box_rows.values()), {38})

    def test_long_description_gets_taller_row(self):
        out, _ = self.fill(n=8)
        self.assertRegex(read(out, "xl/worksheets/sheet1.xml"), r'<row r="28"[^>]*ht="39\.9"')

    def test_limits_and_errors(self):
        with self.assertRaises(T.TemplateError):
            T.fill_template(template_path(), self.tmp / "x.xlsm", make_invoice(3) | {"items": []})
        data = make_invoice(3)
        data["items"] = data["items"] * 6
        with self.assertRaises(T.TemplateError):
            T.fill_template(template_path(), self.tmp / "x.xlsm", data)
        self.assertTrue(T.validate_template(self.tmp / "missing.xlsm"))

    def test_hostile_text_cannot_break_xml(self):
        data = make_invoice(2)
        data["customer"]["name"] = 'a<b>&"c"\x00\x0b' + "\n" + "]]>"
        data["items"][0]["description"] = "<script>&amp;</script>\x1f"
        out = self.tmp / "h.xlsm"
        T.fill_template(template_path(), out, data)
        self.assertIn("<script>&amp;</script>", cell_map(out)["D21"]["t"])   # kept as text, escaped in XML

    def test_template_changes_are_detected(self):
        broken = self.tmp / "broken.xlsm"
        with zipfile.ZipFile(template_path()) as src, zipfile.ZipFile(broken, "w") as dst:
            for i in src.infolist():
                data = src.read(i.filename)
                if i.filename.endswith("drawing1.xml"):
                    data = data.decode("utf-8").replace("المطلوب من", "XXXX").encode("utf-8")
                dst.writestr(i, data)
        self.assertTrue(any("المشتري" in p for p in T.validate_template(broken)))
        with self.assertRaises(T.TemplateError):
            T.fill_template(broken, self.tmp / "o.xlsm", make_invoice(2))


if __name__ == "__main__":
    unittest.main()
