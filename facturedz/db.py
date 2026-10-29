# -*- coding: utf-8 -*-
"""SQLite persistence: versioned migrations, atomic writes, backups and integrity checks.

Compatibility promise: databases created by the original single-file app are upgraded
in place (additive migrations only; nothing is dropped or renamed).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .amounts import to_decimal
from . import inventory, purchasing
from .config import (BACKUP_KEEP_DAILY, BACKUP_KEEP_MONTHLY, DEFAULT_SELLER, DEFAULT_SETTINGS,
                     CUSTOMER_FIELDS, PAYMENT_ALIASES, SERIAL_DIGITS)
from .dates import date_to_iso, iso_or_today  # noqa: F401  (date_to_iso is re-exported: tests import it from here)
from .numbering import PREFIX_DIGITS, date_part, format_invoice_no, month_key, random_serial
from .textnorm import normalize_ar

log = logging.getLogger("facturedz.db")

SCHEMA_VERSION = 4
_CUSTOMER_COLS = [k for k, _ in CUSTOMER_FIELDS]


class DatabaseError(Exception):
    """Raised with an Arabic, user-presentable message."""


@dataclass(frozen=True)
class NumberReservation:
    """Numbers handed out by ``reserve_numbers`` (already burned: they can never be given out again)."""
    invoice_no: str
    serial_no: str
    customer_id: int
    month: str
    seq: int


def _json_default(obj: Any):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not serialisable: {type(obj)!r}")


class Repository:
    def __init__(self, home: Path, default_output: Optional[Path] = None):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.home / "factures.db"
        self.backup_dir = self.home / "backups"
        self.default_output = Path(default_output) if default_output else self.home / "Factures"
        self.default_output.mkdir(parents=True, exist_ok=True)
        self._migrate()
        self.stock = inventory.StockStore(self)          # products, movements, availability
        self.purchases = purchasing.PurchaseStore(self)  # suppliers + purchase invoices

    # ------------------------------------------------------------------ #
    # connections                                                         #
    # ------------------------------------------------------------------ #
    @contextmanager
    def connect(self, write: bool = False):
        con = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=15000")
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if write:
                con.execute("COMMIT")
        except BaseException:
            if write:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            con.close()

    # ------------------------------------------------------------------ #
    # schema / migrations                                                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _columns(con, table: str) -> set:
        return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        existed = self.db_path.exists() and self.db_path.stat().st_size > 0
        with self.connect() as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            # Rollback journal is safer than WAL on roaming/network profiles.
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("PRAGMA synchronous=FULL")
        # A higher (or unknown) user_version is NOT a reason to refuse: every step below is additive and
        # idempotent (checks before adding), so it is safe on any database. Back up first, never downgrade.
        needs_work = version != SCHEMA_VERSION
        if version > SCHEMA_VERSION:
            log.warning("database user_version %s is newer than %s; running idempotent checks", version, SCHEMA_VERSION)
        if existed and needs_work:
            self.backup("pre-migration", force=True)
        with self.connect(write=True) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                commercial_title TEXT DEFAULT '', address TEXT DEFAULT '',
                rc TEXT DEFAULT '', nif TEXT DEFAULT '', nis TEXT DEFAULT '',
                bank_account TEXT DEFAULT '', article_no TEXT DEFAULT '',
                activity TEXT DEFAULT '', phone TEXT DEFAULT '',
                created_at TEXT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_no TEXT NOT NULL UNIQUE,
                serial_no TEXT NOT NULL UNIQUE,
                invoice_date TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                customer_json TEXT NOT NULL,
                seller_json TEXT NOT NULL,
                items_json TEXT NOT NULL,
                subtotal REAL NOT NULL, vat_rate REAL NOT NULL, vat_amount REAL NOT NULL, total REAL NOT NULL,
                payment_method TEXT NOT NULL,
                file_path TEXT NOT NULL,
                created_at TEXT NOT NULL)""")
            # Defensive: make sure every column we read/write exists, whatever variant created the file.
            for table, cols in (
                    ("customers", [(c, "TEXT DEFAULT ''") for c in _CUSTOMER_COLS if c != "name"] +
                                  [("created_at", "TEXT DEFAULT ''")]),
                    ("invoices", [("customer_json", "TEXT DEFAULT '{}'"), ("seller_json", "TEXT DEFAULT '{}'"),
                                  ("items_json", "TEXT DEFAULT '[]'"), ("subtotal", "REAL DEFAULT 0"),
                                  ("vat_rate", "REAL DEFAULT 0"), ("vat_amount", "REAL DEFAULT 0"),
                                  ("total", "REAL DEFAULT 0"), ("payment_method", "TEXT DEFAULT 'cash'"),
                                  ("file_path", "TEXT DEFAULT ''"), ("created_at", "TEXT DEFAULT ''"),
                                  ("updated_at", "TEXT DEFAULT ''")])):
                have = self._columns(con, table)
                for col, ddl in cols:
                    if col not in have:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            # Numbers that were ever issued can never be issued again, even after the invoice is deleted.
            con.execute("""CREATE TABLE IF NOT EXISTS used_numbers (
                kind TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (kind, value))""")
            con.execute("""CREATE TABLE IF NOT EXISTS number_counters (key TEXT PRIMARY KEY, value INTEGER NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS invoice_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER, invoice_no TEXT DEFAULT '',
                serial_no TEXT DEFAULT '', action TEXT NOT NULL, at TEXT NOT NULL, snapshot TEXT NOT NULL)""")
            inventory.ensure_schema(con)         # v4: products + stock movements
            purchasing.ensure_schema(con)        # v4: suppliers + purchase invoices
            if needs_work:
                con.execute("INSERT OR IGNORE INTO used_numbers(kind,value) SELECT 'invoice', invoice_no FROM invoices")
                con.execute("INSERT OR IGNORE INTO used_numbers(kind,value) SELECT 'serial', serial_no FROM invoices")
                inv_cols = self._columns(con, "invoices")
                for col, ddl in (("invoice_date_iso", "TEXT DEFAULT ''"),
                                 ("xlsm_path", "TEXT DEFAULT ''"),
                                 ("app_version", "TEXT DEFAULT ''")):
                    if col not in inv_cols:
                        con.execute(f"ALTER TABLE invoices ADD COLUMN {col} {ddl}")
                if "updated_at" not in self._columns(con, "customers"):
                    con.execute("ALTER TABLE customers ADD COLUMN updated_at TEXT DEFAULT ''")
                for row in con.execute("SELECT id, invoice_date FROM invoices WHERE COALESCE(invoice_date_iso,'')=''").fetchall():
                    iso = date_to_iso(row["invoice_date"])
                    if iso:
                        con.execute("UPDATE invoices SET invoice_date_iso=? WHERE id=?", (iso, row["id"]))
                for old, new in PAYMENT_ALIASES.items():
                    if old:
                        con.execute("UPDATE invoices SET payment_method=? WHERE payment_method=?", (new, old))
            con.execute("CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_name)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date_iso)")
            if not con.execute("SELECT 1 FROM settings WHERE key='seller'").fetchone():
                con.execute("INSERT INTO settings(key,value) VALUES('seller',?)",
                            (json.dumps(DEFAULT_SELLER, ensure_ascii=False),))
            defaults = dict(DEFAULT_SETTINGS)
            defaults["output_dir"] = str(self.default_output)
            for key, value in defaults.items():
                con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))
            con.execute(f"PRAGMA user_version={max(version, SCHEMA_VERSION)}")

    # ------------------------------------------------------------------ #
    # settings                                                            #
    # ------------------------------------------------------------------ #
    def get_setting(self, key: str, fallback: str = "") -> str:
        with self.connect() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else fallback

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect(write=True) as con:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def get_seller(self) -> Dict[str, str]:
        try:
            stored = json.loads(self.get_setting("seller", "{}"))
        except ValueError:
            stored = {}
        seller = dict(DEFAULT_SELLER)
        seller.update({k: str(v if v is not None else "") for k, v in stored.items() if k in DEFAULT_SELLER})
        return seller

    def save_seller(self, fields: Dict[str, Any]) -> None:
        self.backup("daily")
        current = self.get_seller()
        for key in DEFAULT_SELLER:
            if key in fields:
                current[key] = str(fields[key] if fields[key] is not None else "").strip()
        self.set_setting("seller", json.dumps(current, ensure_ascii=False))

    # ------------------------------------------------------------------ #
    # numbering                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _int_or_zero(value) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _number_taken(con, kind: str, value: str) -> bool:
        column = "invoice_no" if kind == "invoice" else "serial_no"
        return bool(con.execute("SELECT 1 FROM used_numbers WHERE kind=? AND value=?", (kind, value)).fetchone()
                    or con.execute(f"SELECT 1 FROM invoices WHERE {column}=?", (value,)).fetchone())

    @staticmethod
    def _customer_id_for(con, fields: Dict[str, Any], create: bool = True) -> Optional[int]:
        """Id of the customer file with this name (created if missing and ``create``). Never overwrites a file."""
        name = str(fields.get("name", "") or "").strip()
        if not name:
            raise ValueError("اسم المشتري مطلوب لتكوين رقم الفاتورة.")
        row = con.execute("SELECT id FROM customers WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if row:
            return int(row["id"])
        if not create:
            return None
        values = {k: str(fields.get(k, "") or "").strip() for k in _CUSTOMER_COLS}
        values["name"] = name
        now = dt.datetime.now().isoformat(timespec="seconds")
        cols = _CUSTOMER_COLS + ["created_at", "updated_at"]
        cur = con.execute(f"INSERT INTO customers({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                          [values[k] for k in _CUSTOMER_COLS] + [now, now])
        return int(cur.lastrowid)

    @staticmethod
    def _next_customer_id(con) -> int:
        try:
            row = con.execute("SELECT seq FROM sqlite_sequence WHERE name='customers'").fetchone()
            top = int(row[0]) if row else 0
        except sqlite3.Error:
            top = 0
        return max(top, int(con.execute("SELECT COALESCE(MAX(id),0) FROM customers").fetchone()[0])) + 1

    def _next_free_invoice_no(self, con, day: dt.date, customer_id: int):
        """(invoice_no, seq): the next running number of ``day``'s month for this customer.

        The monthly counter only goes up. It is also checked against every number ever issued (so a lost or
        restored counter can never produce a duplicate) and against every existing number of that month.
        """
        key = f"inv:{month_key(day)}"
        row = con.execute("SELECT value FROM number_counters WHERE key=?", (key,)).fetchone()
        counter = int(row["value"]) if row else 0
        prefix = date_part(day)
        top = con.execute(
            "SELECT MAX(CAST(substr(value, ?) AS INTEGER)) FROM used_numbers "
            "WHERE kind='invoice' AND length(value) >= ? AND substr(value,1,4)=?",
            (PREFIX_DIGITS + 1, PREFIX_DIGITS + 1, prefix)).fetchone()[0] or 0
        seq = max(counter, int(top)) + 1
        for _ in range(10000):
            candidate = format_invoice_no(day, customer_id, seq)
            if not self._number_taken(con, "invoice", candidate):
                return candidate, seq
            seq += 1
        raise DatabaseError("تعذر إيجاد رقم فاتورة غير مستخدم.")

    def _fresh_serial(self, con, hint: str = "") -> str:
        hint = str(hint or "").strip()
        if hint.isdigit() and len(hint) == SERIAL_DIGITS and hint[0] != "0" and not self._number_taken(con, "serial", hint):
            return hint
        for _ in range(200):
            candidate = random_serial()
            if not self._number_taken(con, "serial", candidate):
                return candidate
        raise DatabaseError("تعذر توليد رقم تسلسلي غير مستخدم.")

    def new_serial_candidate(self) -> str:
        """A random, currently unused serial (shown in the form; only ``reserve_numbers`` makes it final)."""
        with self.connect() as con:
            return self._fresh_serial(con)

    def preview_invoice_no(self, day: dt.date, customer_name: str = "") -> str:
        """The number the next invoice of ``customer_name`` dated ``day`` would get. Writes nothing."""
        with self.connect() as con:
            cid = None
            if str(customer_name or "").strip():
                cid = self._customer_id_for(con, {"name": customer_name}, create=False)
            return self._next_free_invoice_no(con, day, cid or self._next_customer_id(con))[0]

    def reserve_numbers(self, day: dt.date, customer: Dict[str, Any], serial_hint: str = "") -> NumberReservation:
        """Atomically pick the customer file, the invoice number and the serial, and burn them."""
        self.backup("daily")
        with self.connect(write=True) as con:
            cid = self._customer_id_for(con, customer)
            invoice_no, seq = self._next_free_invoice_no(con, day, cid)
            serial = self._fresh_serial(con, serial_hint)
            month = month_key(day)
            con.execute("INSERT INTO number_counters(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=MAX(value, excluded.value)", (f"inv:{month}", seq))
            con.execute("INSERT INTO used_numbers(kind,value) VALUES('invoice',?)", (invoice_no,))
            con.execute("INSERT INTO used_numbers(kind,value) VALUES('serial',?)", (serial,))
        return NumberReservation(invoice_no, serial, cid, month, seq)

    def release_numbers(self, reservation: NumberReservation) -> None:
        """Give the invoice number back after a failed issue, but only if nobody took a later one.
        The serial stays burned (it is random; keeping it used costs nothing)."""
        try:
            with self.connect(write=True) as con:
                key = f"inv:{reservation.month}"
                row = con.execute("SELECT value FROM number_counters WHERE key=?", (key,)).fetchone()
                if row and int(row["value"]) == reservation.seq and not con.execute(
                        "SELECT 1 FROM invoices WHERE invoice_no=?", (reservation.invoice_no,)).fetchone():
                    con.execute("UPDATE number_counters SET value=? WHERE key=?", (reservation.seq - 1, key))
                    con.execute("DELETE FROM used_numbers WHERE kind='invoice' AND value=?", (reservation.invoice_no,))
        except sqlite3.Error:
            log.exception("could not release numbers (harmless: they stay burned)")

    def duplicate_of(self, invoice_no: str, serial_no: str, exclude_id: Optional[int] = None) -> str:
        """'' if both are free, else 'invoice' / 'serial' naming what was already used (even by a deleted invoice)."""
        with self.connect() as con:
            own = (None, None)
            if exclude_id is not None:
                row = con.execute("SELECT invoice_no, serial_no FROM invoices WHERE id=?", (exclude_id,)).fetchone()
                own = (row["invoice_no"], row["serial_no"]) if row else own
            if invoice_no != own[0] and self._number_taken(con, "invoice", invoice_no):
                return "invoice"
            if serial_no != own[1] and self._number_taken(con, "serial", serial_no):
                return "serial"
        return ""

    # ------------------------------------------------------------------ #
    # customers                                                           #
    # ------------------------------------------------------------------ #
    def save_customer(self, fields: Dict[str, Any], customer_id: Optional[int] = None) -> int:
        name = str(fields.get("name", "") or "").strip()
        if not name:
            raise ValueError("اسم المشتري مطلوب لحفظ ملفه.")
        values = {k: str(fields.get(k, "") or "").strip() for k in _CUSTOMER_COLS}
        values["name"] = name
        now = dt.datetime.now().isoformat(timespec="seconds")
        self.backup("daily")
        try:
            with self.connect(write=True) as con:
                if customer_id is None:
                    row = con.execute("SELECT id FROM customers WHERE name=? COLLATE NOCASE", (name,)).fetchone()
                    customer_id = row["id"] if row else None
                if customer_id is None:
                    cols = _CUSTOMER_COLS + ["created_at", "updated_at"]
                    cur = con.execute(
                        f"INSERT INTO customers({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [values[k] for k in _CUSTOMER_COLS] + [now, now])
                    return int(cur.lastrowid)
                sets = ",".join(f"{k}=?" for k in _CUSTOMER_COLS)
                con.execute(f"UPDATE customers SET {sets}, updated_at=? WHERE id=?",
                            [values[k] for k in _CUSTOMER_COLS] + [now, customer_id])
                return int(customer_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError("يوجد زبون آخر بنفس الاسم.") from exc

    def list_customers(self, search: str = "") -> List[Dict[str, Any]]:
        with self.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM customers ORDER BY name COLLATE NOCASE")]
        needle = normalize_ar(search)
        if needle:
            rows = [r for r in rows if needle in normalize_ar(
                " ".join(str(r.get(k, "")) for k in ("name", "commercial_title", "rc", "phone")))]
        return rows

    def get_customer(self, customer_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        return dict(row) if row else None

    def delete_customer(self, customer_id: int) -> None:
        self.backup("daily")
        with self.connect(write=True) as con:
            con.execute("DELETE FROM customers WHERE id=?", (customer_id,))

    # ------------------------------------------------------------------ #
    # invoices                                                            #
    # ------------------------------------------------------------------ #
    def add_invoice(self, data: Dict[str, Any], file_path: str = "", xlsm_path: str = "") -> int:
        """Insert an issued sale invoice. Its stock movements are written in the same transaction."""
        self.backup("daily")
        now = dt.datetime.now().isoformat(timespec="seconds")
        items = [dict(i) for i in data["items"]]
        try:
            with self.connect(write=True) as con:
                inventory.link_items(con, items)              # sold items that match a product leave its stock
                cur = con.execute(
                    """INSERT INTO invoices(invoice_no,serial_no,invoice_date,invoice_date_iso,customer_name,
                       customer_json,seller_json,items_json,subtotal,vat_rate,vat_amount,total,payment_method,
                       file_path,xlsm_path,app_version,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (data["invoice_no"], data["serial_no"], data["invoice_date"], date_to_iso(data["invoice_date"]),
                     data["customer"]["name"],
                     json.dumps(data["customer"], ensure_ascii=False),
                     json.dumps(data["seller"], ensure_ascii=False),
                     json.dumps(self._items_for_json({"items": items}), ensure_ascii=False, default=_json_default),
                     float(data["subtotal"]), float(data["vat_rate"]), float(data["vat_amount"]), float(data["total"]),
                     data["payment_method"], str(file_path or ""), str(xlsm_path or ""), __version__, now))
                invoice_id = int(cur.lastrowid)
                con.execute("INSERT OR IGNORE INTO used_numbers(kind,value) VALUES('invoice',?)", (data["invoice_no"],))
                con.execute("INSERT OR IGNORE INTO used_numbers(kind,value) VALUES('serial',?)", (data["serial_no"],))
                inventory.replace_movements(con, "invoice", invoice_id, "sale",
                                            iso_or_today(data["invoice_date"]), items, -1)
                return invoice_id
        except sqlite3.IntegrityError as exc:
            raise ValueError("رقم الفاتورة أو الرقم التسلسلي مستخدم مسبقًا.") from exc

    def update_invoice_files(self, invoice_id: int, file_path: str, xlsm_path: str) -> None:
        with self.connect(write=True) as con:
            con.execute("UPDATE invoices SET file_path=?, xlsm_path=? WHERE id=?",
                        (str(file_path or ""), str(xlsm_path or ""), invoice_id))

    @staticmethod
    def _items_for_json(data: Dict[str, Any], tracked: bool = True) -> List[Dict[str, Any]]:
        rows = [{"description": i["description"], "quantity": i["quantity"], "unit_price": i["unit_price"],
                 "line_total": i["line_total"], "product_id": i.get("product_id")} for i in data["items"]]
        if not tracked:                      # an invoice from before stock tracking keeps its old shape (no link key)
            for r in rows:
                del r["product_id"]
        return rows

    @staticmethod
    def stock_tracked(items_json: str) -> bool:
        """False for invoices issued before stock tracking existed (their items carry no ``product_id`` key):
        they never touched stock, so editing them must not start doing so."""
        try:
            items = json.loads(items_json or "[]")
        except ValueError:
            return False
        return bool(items) and all("product_id" in i for i in items)

    def update_invoice(self, invoice_id: int, data: Dict[str, Any], file_path: str = "", xlsm_path: str = "") -> None:
        """Rewrite an issued invoice. Its invoice number and serial NEVER change; the previous state is kept in
        ``invoice_log`` and a full backup is taken first."""
        self.backup("pre-edit", force=True)
        now = dt.datetime.now().isoformat(timespec="seconds")
        items = [dict(i) for i in data["items"]]
        with self.connect(write=True) as con:
            row = con.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if not row:
                raise ValueError("الفاتورة غير موجودة.")
            tracked = self.stock_tracked(row["items_json"])
            if tracked:
                inventory.link_items(con, items)
            else:
                for item in items:
                    item["product_id"] = None
            con.execute("INSERT INTO invoice_log(invoice_id,invoice_no,serial_no,action,at,snapshot) VALUES(?,?,?,?,?,?)",
                        (invoice_id, row["invoice_no"], row["serial_no"], "edit", now,
                         json.dumps(dict(row), ensure_ascii=False, default=_json_default)))
            con.execute(
                """UPDATE invoices SET invoice_date=?, invoice_date_iso=?, customer_name=?, customer_json=?,
                   seller_json=?, items_json=?, subtotal=?, vat_rate=?, vat_amount=?, total=?, payment_method=?,
                   file_path=?, xlsm_path=?, app_version=?, updated_at=? WHERE id=?""",
                (data["invoice_date"], date_to_iso(data["invoice_date"]), data["customer"]["name"],
                 json.dumps(data["customer"], ensure_ascii=False), json.dumps(data["seller"], ensure_ascii=False),
                 json.dumps(self._items_for_json({"items": items}, tracked), ensure_ascii=False, default=_json_default),
                 float(data["subtotal"]), float(data["vat_rate"]), float(data["vat_amount"]), float(data["total"]),
                 data["payment_method"], str(file_path or ""), str(xlsm_path or ""), __version__, now, invoice_id))
            if tracked:
                inventory.replace_movements(con, "invoice", int(invoice_id), "sale",
                                            iso_or_today(data["invoice_date"]), items, -1)

    def delete_invoice(self, invoice_id: int) -> Optional[Dict[str, Any]]:
        """Remove an invoice record (full copy kept in ``invoice_log``). Its numbers stay burned forever.
        Returns the deleted row (so the caller can remove its files) or None if it did not exist."""
        self.backup("pre-delete", force=True)
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self.connect(write=True) as con:
            row = con.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if not row:
                return None
            rec = dict(row)
            con.execute("INSERT INTO invoice_log(invoice_id,invoice_no,serial_no,action,at,snapshot) VALUES(?,?,?,?,?,?)",
                        (invoice_id, rec["invoice_no"], rec["serial_no"], "delete", now,
                         json.dumps(rec, ensure_ascii=False, default=_json_default)))
            con.execute("INSERT OR IGNORE INTO used_numbers(kind,value) VALUES('invoice',?)", (rec["invoice_no"],))
            con.execute("INSERT OR IGNORE INTO used_numbers(kind,value) VALUES('serial',?)", (rec["serial_no"],))
            inventory.remove_movements(con, "invoice", int(invoice_id))      # the goods go back to stock
            con.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
        return rec

    def list_invoices(self, search: str = "", limit: int = 3000) -> List[Dict[str, Any]]:
        with self.connect() as con:
            rows = [dict(r) for r in con.execute(
                """SELECT id, invoice_no, serial_no, invoice_date, invoice_date_iso, customer_name, total,
                          payment_method, file_path, xlsm_path
                   FROM invoices ORDER BY id DESC LIMIT ?""", (limit,))]
        needle = normalize_ar(search)
        if needle:
            rows = [r for r in rows if needle in normalize_ar(
                f"{r['invoice_no']} {r['serial_no']} {r['customer_name']} {r['invoice_date']}")]
        return rows

    def get_invoice(self, invoice_id: int) -> Optional[Dict[str, Any]]:
        """Full invoice with parsed JSON and Decimal amounts (ready to re-export)."""
        with self.connect() as con:
            row = con.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        items = []
        for it in json.loads(rec["items_json"] or "[]"):
            items.append({"description": str(it.get("description", "")),
                          "quantity": to_decimal(it.get("quantity", 0)),
                          "unit_price": to_decimal(it.get("unit_price", 0)),
                          "line_total": to_decimal(it.get("line_total", 0)), "product_id": it.get("product_id")})
        method = PAYMENT_ALIASES.get(rec["payment_method"], rec["payment_method"]) or "cash"
        return {
            "id": rec["id"], "invoice_no": rec["invoice_no"], "serial_no": rec["serial_no"],
            "invoice_date": rec["invoice_date"],
            "customer": json.loads(rec["customer_json"] or "{}"),
            "seller": {**DEFAULT_SELLER, **json.loads(rec["seller_json"] or "{}")},
            "items": items, "stock_tracked": self.stock_tracked(rec["items_json"]),
            "subtotal": to_decimal(rec["subtotal"]), "vat_rate": to_decimal(rec["vat_rate"]),
            "vat_amount": to_decimal(rec["vat_amount"]), "total": to_decimal(rec["total"]),
            "payment_method": method,
            "file_path": rec["file_path"], "xlsm_path": rec.get("xlsm_path", "") or "",
        }

    # ------------------------------------------------------------------ #
    # backups & integrity                                                 #
    # ------------------------------------------------------------------ #
    def integrity_problem(self) -> str:
        """'' when healthy, otherwise a short technical description."""
        try:
            with self.connect() as con:
                result = con.execute("PRAGMA quick_check").fetchone()[0]
            return "" if result == "ok" else str(result)
        except sqlite3.Error as exc:
            return str(exc)

    def backup(self, reason: str = "daily", force: bool = False) -> Optional[Path]:
        """Consistent SQLite backup. 'daily' makes at most one per day. Never raises."""
        try:
            if not self.db_path.exists() or self.db_path.stat().st_size < 512:
                return None
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            today = dt.date.today().isoformat()
            if reason == "daily":
                target = self.backup_dir / f"factures_{today}.db"
                if target.exists() and not force:
                    return target
            else:
                stamp = dt.datetime.now().strftime("%H%M%S")
                target = self.backup_dir / f"factures_{today}_{stamp}_{re.sub(r'[^a-z-]', '', reason.lower())}.db"
            self._copy_db(self.db_path, target)
            self._prune_backups()
            return target
        except Exception:
            log.exception("backup failed (continuing)")
            return None

    @staticmethod
    def _copy_db(src_path: Path, dst_path: Path) -> None:
        tmp = dst_path.with_name(dst_path.name + ".tmp")
        tmp.unlink(missing_ok=True)
        src = sqlite3.connect(str(src_path), timeout=15)
        dst = sqlite3.connect(str(tmp), timeout=15)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.replace(tmp, dst_path)

    def list_backups(self) -> List[Path]:
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("factures_*.db"), key=lambda p: p.name, reverse=True)

    def _prune_backups(self) -> None:
        daily = sorted(self.backup_dir.glob("factures_????-??-??.db"), key=lambda p: p.name, reverse=True)
        keep = set(daily[:BACKUP_KEEP_DAILY])
        months: dict = {}
        for p in sorted(daily, key=lambda p: p.name):          # oldest first -> first-of-month wins
            months.setdefault(p.name[9:16], p)
        for month in sorted(months, reverse=True)[:BACKUP_KEEP_MONTHLY]:
            keep.add(months[month])
        for p in daily:
            if p not in keep:
                try:
                    p.unlink()
                except OSError:
                    pass
        extras = sorted((p for p in self.backup_dir.glob("factures_*.db") if p not in daily),
                        key=lambda p: p.name, reverse=True)
        for p in extras[10:]:
            try:
                p.unlink()
            except OSError:
                pass

    def export_backup(self, destination: Path) -> Path:
        destination = Path(destination)
        self._copy_db(self.db_path, destination)
        return destination

    def restore_backup(self, backup_file: Path) -> Path:
        """Replace the live DB with a backup, keeping the replaced file as a safety copy."""
        backup_file = Path(backup_file)
        probe = sqlite3.connect(str(backup_file))
        try:
            ok = probe.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as exc:
            raise DatabaseError("ملف النسخة الاحتياطية تالف.") from exc
        finally:
            probe.close()
        if ok != "ok":
            raise DatabaseError("ملف النسخة الاحتياطية تالف.")
        safety = self.home / f"factures_before_restore_{dt.datetime.now():%Y%m%d_%H%M%S}.db"
        if self.db_path.exists():
            os.replace(self.db_path, safety)
        self._copy_db(backup_file, self.db_path)
        self._migrate()
        return safety
