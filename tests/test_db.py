import datetime as dt
import json
import sqlite3
import unittest
from decimal import Decimal

from tests._helpers import HomeTestCase
from facturedz.config import DEFAULT_SELLER
from facturedz.db import SCHEMA_VERSION, DatabaseError, Repository

LEGACY_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    commercial_title TEXT DEFAULT '', address TEXT DEFAULT '', rc TEXT DEFAULT '', nif TEXT DEFAULT '',
    nis TEXT DEFAULT '', bank_account TEXT DEFAULT '', article_no TEXT DEFAULT '', activity TEXT DEFAULT '',
    phone TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_no TEXT NOT NULL UNIQUE, serial_no TEXT NOT NULL UNIQUE,
    invoice_date TEXT NOT NULL, customer_name TEXT NOT NULL, customer_json TEXT NOT NULL,
    seller_json TEXT NOT NULL, items_json TEXT NOT NULL, subtotal REAL NOT NULL, vat_rate REAL NOT NULL,
    vat_amount REAL NOT NULL, total REAL NOT NULL, payment_method TEXT NOT NULL, file_path TEXT NOT NULL,
    created_at TEXT NOT NULL);
"""


def make_legacy_db(home):
    """A database exactly as the ORIGINAL app wrote it (WAL mode, dd.mm.yyyy dates, 'check')."""
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "factures.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(LEGACY_SCHEMA)
    con.execute("INSERT INTO settings VALUES('seller', ?)",
                (json.dumps({"business_name": "مؤسستي", "city": "ورقلة"}, ensure_ascii=False),))
    for k, v in {"vat_rate": "9", "output_dir": str(home / "Out"),
                 "last_invoice_number": "41", "last_serial_number": "40"}.items():
        con.execute("INSERT INTO settings VALUES(?,?)", (k, v))
    con.execute("INSERT INTO customers(name,phone,created_at) VALUES('زبون قديم','0550',?)", ("2025-01-01T10:00:00",))
    con.execute("""INSERT INTO invoices(invoice_no,serial_no,invoice_date,customer_name,customer_json,seller_json,
        items_json,subtotal,vat_rate,vat_amount,total,payment_method,file_path,created_at)
        VALUES('41','40','05.03.2025','زبون قديم','{"name":"زبون قديم"}','{}',
        '[{"description":"بنان","quantity":10.5,"unit_price":353.87,"line_total":3715.64}]',
        3715.64,19,705.97,4421.61,'check','C:/x/فاتورة_41.pdf','2025-03-05T09:00:00')""")
    con.commit()
    con.close()


def sample(no="1", serial="1"):
    return {"invoice_no": no, "serial_no": serial, "invoice_date": "10.10.2026",
            "customer": {"name": "زبون"}, "seller": dict(DEFAULT_SELLER),
            "items": [{"description": "بطاطا", "quantity": Decimal("10"), "unit_price": Decimal("45.5"),
                       "line_total": Decimal("455.00")}],
            "subtotal": Decimal("455.00"), "vat_rate": Decimal("19"), "vat_amount": Decimal("86.45"),
            "total": Decimal("541.45"), "payment_method": "cash"}


class DatabaseTests(HomeTestCase):
    def test_fresh_database_gets_defaults(self):
        repo = Repository(self.home)
        self.assertEqual(repo.get_seller(), DEFAULT_SELLER)
        self.assertEqual(repo.get_setting("vat_rate"), "19")
        self.assertEqual(repo.preview_invoice_no(dt.date(2026, 10, 10), ""), "1026000010001")   # MMYY + customer 1 + no. 1
        self.assertEqual(repo.integrity_problem(), "")
        with repo.connect() as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_legacy_database_is_upgraded_without_data_loss(self):
        make_legacy_db(self.home)
        repo = Repository(self.home)

        self.assertEqual(repo.get_setting("vat_rate"), "9")                 # user settings preserved
        seller = repo.get_seller()
        self.assertEqual((seller["business_name"], seller["city"]), ("مؤسستي", "ورقلة"))
        self.assertEqual(seller["nif"], DEFAULT_SELLER["nif"])              # missing keys -> defaults
        self.assertEqual(repo.list_customers()[0]["name"], "زبون قديم")

        inv = repo.list_invoices()[0]
        self.assertEqual((inv["invoice_no"], inv["invoice_date_iso"]), ("41", "2025-03-05"))
        full = repo.get_invoice(inv["id"])
        self.assertEqual(full["payment_method"], "cheque")                  # legacy 'check' normalised
        self.assertEqual(full["items"][0]["unit_price"], Decimal("353.87"))
        self.assertEqual(full["total"], Decimal("4421.61"))

        self.assertEqual(repo.duplicate_of("41", "999"), "invoice")         # legacy numbers stay burned
        self.assertEqual(repo.duplicate_of("7", "40"), "serial")
        self.assertEqual(repo.preview_invoice_no(dt.date(2026, 10, 10), "زبون قديم"), "1026000010001")  # old customer = no. 1; new scheme starts
        self.assertTrue(any("pre-migration" in p.name for p in repo.list_backups()))
        with repo.connect() as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        Repository(self.home)                                               # reopening is a no-op

    def test_unknown_higher_user_version_is_upgraded_safely_not_refused(self):
        """Regression: a real user's DB had user_version > ours and the app refused to start."""
        for version in (SCHEMA_VERSION + 5, 20260101):
            with self.subTest(version=version):
                home = self.tmp / f"h{version}"
                make_legacy_db(home)
                con = sqlite3.connect(home / "factures.db")
                con.execute(f"PRAGMA user_version={version}")
                con.commit()
                con.close()
                repo = Repository(home)
                inv = repo.list_invoices()[0]
                self.assertEqual(inv["invoice_date_iso"], "2025-03-05")       # additive columns were added
                self.assertEqual(repo.get_invoice(inv["id"])["payment_method"], "cheque")
                self.assertEqual(repo.duplicate_of("41", "999"), "invoice")
                self.assertTrue(any("pre-migration" in p.name for p in repo.list_backups()))
                with repo.connect() as c:
                    self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], version)   # never downgraded
                repo.add_invoice(sample("42", "41"))                           # and it is fully usable

    def test_invoice_roundtrip_and_duplicates(self):
        repo = Repository(self.home)
        iid = repo.add_invoice(sample("1", "1"), "a.pdf", "a.xlsm")
        self.assertEqual(repo.duplicate_of("1", "9"), "invoice")
        self.assertEqual(repo.duplicate_of("9", "1"), "serial")
        self.assertEqual(repo.duplicate_of("9", "9"), "")
        self.assertEqual(repo.duplicate_of("1", "1", exclude_id=iid), "")
        with self.assertRaises(ValueError):
            repo.add_invoice(sample("1", "5"))
        got = repo.get_invoice(iid)
        self.assertEqual((got["total"], got["xlsm_path"]), (Decimal("541.45"), "a.xlsm"))

    def test_deleted_invoice_numbers_are_never_reused(self):
        repo = Repository(self.home)
        iid = repo.add_invoice(sample("1", "1"))
        self.assertIsNotNone(repo.delete_invoice(iid))
        self.assertEqual(repo.list_invoices(), [])
        self.assertEqual(repo.duplicate_of("1", "9"), "invoice")           # burned although the row is gone
        self.assertEqual(repo.duplicate_of("9", "1"), "serial")
        self.assertIsNone(repo.delete_invoice(iid))                        # second delete is a harmless no-op
        with repo.connect() as con:
            actions = [r["action"] for r in con.execute("SELECT action FROM invoice_log")]
        self.assertEqual(actions, ["delete"])                              # and a copy of it was kept
        self.assertTrue(any("pre-delete" in p.name for p in repo.list_backups()))

    def test_customers_rename_search_and_duplicates(self):
        repo = Repository(self.home)
        cid = repo.save_customer({"name": "أحمد بن علي", "phone": "1"})
        self.assertEqual(repo.save_customer({"name": "أحمد بن علي", "phone": "2"}), cid)   # same name -> update
        ascii_id = repo.save_customer({"name": "Ahmed Co"})
        self.assertEqual(repo.save_customer({"name": "AHMED CO"}), ascii_id)               # NOCASE is ASCII-only
        other = repo.save_customer({"name": "محمد"})
        repo.save_customer({"name": "أحمد الجديد", "phone": "3"}, customer_id=cid)           # rename by id
        self.assertEqual([c["name"] for c in repo.list_customers("احمد")], ["أحمد الجديد"])  # أ/ا-insensitive
        with self.assertRaises(ValueError):
            repo.save_customer({"name": "محمد"}, customer_id=cid)
        with self.assertRaises(ValueError):
            repo.save_customer({"name": "  "})
        repo.delete_customer(other)
        self.assertEqual(len(repo.list_customers()), 2)

    def test_backup_restore_and_pruning(self):
        repo = Repository(self.home)
        repo.add_invoice(sample("1", "1"))
        first = repo.backup("manual", force=True)
        self.assertTrue(first and first.exists())
        repo.add_invoice(sample("2", "2"))
        self.assertEqual(len(repo.list_invoices()), 2)

        repo.restore_backup(first)
        self.assertEqual(len(repo.list_invoices()), 1)
        self.assertTrue(list(self.home.glob("factures_before_restore_*.db")))   # safety copy kept

        bad = self.home / "bad.db"
        bad.write_bytes(b"not a database" * 100)
        with self.assertRaises(DatabaseError):
            repo.restore_backup(bad)
        self.assertEqual(len(repo.list_invoices()), 1)                          # live DB untouched

        for d in range(1, 31):
            (repo.backup_dir / f"factures_2026-01-{d:02d}.db").write_bytes(b"x")
        repo._prune_backups()
        names = sorted(p.name for p in repo.backup_dir.glob("factures_2026-01-*.db"))
        self.assertTrue(14 <= len(names) <= 15)
        self.assertIn("factures_2026-01-01.db", names)                          # monthly snapshot kept

    def test_corruption_is_detected(self):
        repo = Repository(self.home)
        repo.add_invoice(sample())
        data = bytearray(repo.db_path.read_bytes())
        for i in range(2000, min(len(data), 6000)):
            data[i] = 0xFF
        repo.db_path.write_bytes(bytes(data))
        self.assertNotEqual(repo.integrity_problem(), "")


DAY = dt.date(2026, 10, 10)


class NumberingTests(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.repo = Repository(self.home)

    def reserve(self, name="زبون أ", day=DAY, **kw):
        return self.repo.reserve_numbers(day, {"name": name}, **kw)

    def test_invoice_number_is_month_year_customer_and_monthly_counter(self):
        a, b, c = self.reserve("أ"), self.reserve("ب"), self.reserve("أ")
        self.assertEqual((a.invoice_no, b.invoice_no, c.invoice_no),
                         ("1026000010001", "1026000020002", "1026000010003"))   # counter is per month, not per customer
        self.assertEqual(self.reserve("أ", dt.date(2026, 11, 3)).invoice_no, "1126000010001")   # new month restarts
        self.assertEqual(self.reserve("أ", dt.date(2027, 1, 3)).invoice_no, "0127000010001")    # leading zero kept
        self.assertEqual(len(a.serial_no), 12)

    def test_customer_file_is_created_once_and_never_overwritten(self):
        self.repo.save_customer({"name": "أ", "phone": "0550"})
        self.reserve("أ")
        self.assertEqual(self.repo.list_customers()[0]["phone"], "0550")
        self.assertEqual(len(self.repo.list_customers()), 1)
        self.reserve("ب")
        self.assertEqual(len(self.repo.list_customers()), 2)

    def test_preview_matches_what_is_reserved(self):
        self.assertEqual(self.repo.preview_invoice_no(DAY, "جديد"), self.reserve("جديد").invoice_no)
        self.assertEqual(self.repo.preview_invoice_no(DAY, "جديد"), "1026000010002")

    def test_serials_are_random_unique_and_the_shown_one_is_kept(self):
        serials = {self.reserve("زبون").serial_no for _ in range(300)}
        self.assertEqual(len(serials), 300)
        self.assertTrue(all(len(x) == 12 and x[0] != "0" for x in serials))
        hint = self.repo.new_serial_candidate()
        self.assertEqual(self.reserve(serial_hint=hint).serial_no, hint)
        self.assertNotEqual(self.reserve(serial_hint=hint).serial_no, hint)    # already used -> a different one

    def test_counter_survives_deletion_and_is_rebuilt_if_lost(self):
        a, b = self.reserve(), self.reserve()
        for n, r in ((1, a), (2, b)):
            self.repo.add_invoice(sample(r.invoice_no, r.serial_no))
        last = self.repo.list_invoices()[0]
        self.repo.delete_invoice(last["id"])                               # delete number 2 ...
        self.assertTrue(self.reserve().invoice_no.endswith("0003"))        # ... number 2 is not given again
        with self.repo.connect(write=True) as con:
            con.execute("DELETE FROM number_counters")                     # counter table lost
        self.assertTrue(self.reserve().invoice_no.endswith("0004"))        # rebuilt from the numbers ever issued

    def test_release_gives_back_only_the_latest_number(self):
        first = self.reserve()
        self.repo.release_numbers(first)
        self.assertEqual(self.reserve().invoice_no, first.invoice_no)      # nobody took a later one -> reused
        second, third = self.reserve(), self.reserve()
        self.repo.release_numbers(second)                                  # a later one exists -> stays burned
        self.assertTrue(self.reserve().invoice_no.endswith("0004"))

    def test_monthly_counter_may_outgrow_four_digits(self):
        with self.repo.connect(write=True) as con:
            con.execute("INSERT INTO number_counters(key,value) VALUES('inv:202610', 9999)")
        r = self.reserve()
        self.assertEqual(r.invoice_no, "10260000110000")                   # 14 digits, still unambiguous

    def test_update_keeps_numbers_and_logs_the_previous_version(self):
        r = self.reserve()
        iid = self.repo.add_invoice(sample(r.invoice_no, r.serial_no), "a.pdf", "a.xlsm")
        new = sample(r.invoice_no, r.serial_no)
        new.update(total=Decimal("100.00"), subtotal=Decimal("84.03"), vat_amount=Decimal("15.97"),
                   invoice_date="12.11.2026", payment_method="cheque")
        self.repo.update_invoice(iid, new, "b.pdf", "")
        got = self.repo.get_invoice(iid)
        self.assertEqual((got["invoice_no"], got["serial_no"]), (r.invoice_no, r.serial_no))
        self.assertEqual((got["total"], got["invoice_date"], got["payment_method"], got["file_path"]),
                         (Decimal("100.0"), "12.11.2026", "cheque", "b.pdf"))
        self.assertEqual(self.repo.list_invoices()[0]["invoice_date_iso"], "2026-11-12")
        with self.repo.connect() as con:
            row = con.execute("SELECT action, snapshot FROM invoice_log").fetchone()
        self.assertEqual(row["action"], "edit")
        self.assertEqual(json.loads(row["snapshot"])["total"], 541.45)     # what it was before the edit
        with self.assertRaises(ValueError):
            self.repo.update_invoice(9999, new)


if __name__ == "__main__":
    unittest.main()
