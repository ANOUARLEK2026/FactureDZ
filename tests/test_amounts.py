import unittest
from decimal import Decimal

from tests import _helpers  # noqa: F401  (sets sys.path)
from facturedz.amounts import (amount_in_words, line_total, money, parse_decimal, plain_number, q2,
                               qty_text, vat_amount)
from facturedz.textnorm import normalize_ar, safe_filename, xml_safe


class ParseTests(unittest.TestCase):
    def test_accepts_common_formats(self):
        cases = [("1234.5", "1234.5"), ("1,234.50", "1234.50"), ("1.234,50", "1234.50"), ("1 234,5", "1234.5"),
                 ("١٢٣٫٥", "123.5"), ("1,5", "1.5"), ("1,000,000", "1000000"), ("", "0"),
                 ("٣٥٣٫٨٧", "353.87"), ("  12  ", "12"), ("1'234", "1234")]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_decimal(text), Decimal(expected))

    def test_rejects_garbage(self):
        for bad in ["abc", "1..2x", "--5", "1e5", "NaN", "12kg"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_decimal(bad)


class MoneyTests(unittest.TestCase):
    def test_rounding_half_up_without_float_noise(self):
        self.assertEqual(q2(Decimal("2.675")), Decimal("2.68"))           # float math gives 2.67
        self.assertEqual(line_total(Decimal("3"), Decimal("0.335")), Decimal("1.01"))
        self.assertEqual(vat_amount(Decimal("1000"), Decimal("19")), Decimal("190.00"))

    def test_formatting(self):
        self.assertEqual(money(1234567.5), "1,234,567.50")
        self.assertEqual(plain_number(Decimal("19.00")), "19")
        self.assertEqual(plain_number(Decimal("0.500")), "0.5")
        self.assertEqual(qty_text(Decimal("1250")), "1,250")
        self.assertEqual(qty_text(Decimal("1250.250")), "1,250.25")


class WordsTests(unittest.TestCase):
    def test_amount_in_words(self):
        cases = [
            (0, "صفر دينار جزائري"),
            (1, "واحد دينار جزائري"),
            (12, "اثنا عشر دينار جزائري"),
            (101, "مائة وواحد دينار جزائري"),
            (200000, "مائتا ألف دينار جزائري"),            # old code printed 'مائتان ألف'
            (2000, "ألفان دينار جزائري"),
            (3000, "ثلاثة آلاف دينار جزائري"),
            (2500000, "مليونان وخمسمائة ألف دينار جزائري"),
            (1250.5, "ألف ومائتان وخمسون دينار جزائري وخمسون سنتيم"),
        ]
        for value, words in cases:
            with self.subTest(value=value):
                self.assertEqual(amount_in_words(value), words)

    def test_exact_on_float_rounding_edge(self):
        # 2.675 * 100 == 267.49999... in binary floats; Decimal path must give 68 centimes
        self.assertTrue(amount_in_words(Decimal("2.675")).endswith("وثمانية وستون سنتيم"))

    def test_huge_amount_is_refused_not_garbled(self):
        with self.assertRaises(OverflowError):
            amount_in_words(Decimal("10000000000000"))


class TextTests(unittest.TestCase):
    def test_normalisation_and_filenames(self):
        self.assertEqual(normalize_ar("الرقــم الإحصــائي"), normalize_ar("الرقم الاحصائي"))
        self.assertEqual(normalize_ar("أحمد"), normalize_ar("احمد"))
        self.assertEqual(normalize_ar("\uf07d\uf07dX"), "x")
        self.assertEqual(xml_safe("a\x00b\nc"), "ab c")
        self.assertEqual(safe_filename('زبون: "عمر"/؟<>'), "زبون عمر ؟")
        self.assertEqual(safe_filename("CON"), "_CON")
        self.assertEqual(safe_filename("x" * 100, 10), "x" * 10)


if __name__ == "__main__":
    unittest.main()
