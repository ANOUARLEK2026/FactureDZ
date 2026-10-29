# -*- coding: utf-8 -*-
"""Suppliers and purchase invoices (فواتير الشراء).

Same guarantees as the sale invoices: numbers are burned for ever, edits/deletions are logged, and the stock
movements of a purchase are written in the SAME transaction as the purchase itself.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .amounts import to_decimal
from .config import DEFAULT_SELLER, PAYMENT_ALIASES, PRODUCT_NAME_MAX, SUPPLIER_FIELDS
from .dates import iso_or_today
from .inventory import clean_name, link_items, remove_movements, replace_movements
from .numbering import PREFIX_DIGITS, PURCHASE_PREFIX, date_part, format_purchase_no, month_key
from .textnorm import normalize_ar

log = logging.getLogger("facturedz.purchasing")
_SUPPLIER_COLS = [k for k, _ in SUPPLIER_FIELDS]
_NO_DIGITS_FROM = len(PURCHASE_PREFIX) + PREFIX_DIGITS        # the monthly counter starts after this many characters


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _json_default(obj: Any):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not serialisable: {type(obj)!r}")


@dataclass(frozen=True)
class PurchaseReservation:
    invoice_no: str
    serial_no: str
    supplier_id: int
    month: str
    seq: int


def ensure_schema(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS suppliers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        commercial_title TEXT DEFAULT '', address TEXT DEFAULT '',
        rc TEXT DEFAULT '', nif TEXT DEFAULT '', nis TEXT DEFAULT '',
        bank_account TEXT DEFAULT '', article_no TEXT DEFAULT '',
        activity TEXT DEFAULT '', phone TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '', updated_at TEXT DEFAULT '')""")
    con.execute("""CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_no TEXT NOT NULL UNIQUE,
        serial_no TEXT NOT NULL UNIQUE,
        invoice_date TEXT NOT NULL,
        invoice_date_iso TEXT NOT NULL DEFAULT '',
        supplier_name TEXT NOT NULL,
        supplier_json TEXT NOT NULL,
        buyer_json TEXT NOT NULL,
        items_json TEXT NOT NULL,
        subtotal REAL NOT NULL, vat_rate REAL NOT NULL, vat_amount REAL NOT NULL, total REAL NOT NULL,
        payment_method TEXT NOT NULL,
        file_path TEXT NOT NULL DEFAULT '', xlsm_path TEXT NOT NULL DEFAULT '',
        app_version TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT '')""")
    con.execute("""CREATE TABLE IF NOT EXISTS purchase_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, purchase_id INTEGER, purchase_no TEXT DEFAULT '',
        action TEXT NOT NULL, at TEXT NOT NULL, snapshot TEXT NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_purchases_supplier ON purchases(supplier_name)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(invoice_date_iso)")


def supplier_as_seller(supplier: Dict[str, Any], buyer: Dict[str, Any]) -> Dict[str, str]:
    """The supplier in the 'seller' slot of the invoice layout (header + right box)."""
    seller = {k: "" for k in DEFAULT_SELLER}
    for key in ("commercial_title", "address", "rc", "nif", "nis", "bank_account", "article_no", "activity", "phone"):
        seller[key] = str(supplier.get(key, "") or "")
    seller["business_name"] = str(supplier.get("name", "") or "")
    seller["city"] = str(buyer.get("city", "") or "")            # "city, on: date" is where WE write the document
    return seller


def buyer_as_customer(buyer: Dict[str, Any]) -> Dict[str, str]:
    """Our own business in the 'buyer' slot (left box)."""
    customer = {key: str(buyer.get(key, "") or "") for key in
                ("commercial_title", "address", "rc", "nif", "nis", "bank_account", "article_no", "activity", "phone")}
    customer["name"] = str(buyer.get("business_name", "") or "")
    return customer


def to_export_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical purchase dict -> the dict the invoice PDF/Excel writers understand (a purchase invoice is laid out as
    the supplier's invoice to us: supplier on the right, our business on the left)."""
    buyer = data.get("buyer") or {}
    return {
        "kind": "purchase",
        "invoice_no": data["invoice_no"], "serial_no": data["serial_no"], "invoice_date": data["invoice_date"],
        "seller": supplier_as_seller(data["supplier"], buyer), "customer": buyer_as_customer(buyer),
        "items": data["items"], "subtotal": data["subtotal"], "vat_rate": data["vat_rate"],
        "vat_amount": data["vat_amount"], "total": data["total"], "payment_method": data["payment_method"],
    }


class PurchaseStore:
    def __init__(self, repo):
        self.repo = repo

    # ------------------------------------------------------------------ #
    # suppliers                                                           #
    # ------------------------------------------------------------------ #
    def save_supplier(self, fields: Dict[str, Any], supplier_id: Optional[int] = None) -> int:
        name = clean_name(fields.get("name"))
        if not name:
            raise ValueError("اسم المورد مطلوب لحفظ ملفه.")
        values = {k: str(fields.get(k, "") or "").strip() for k in _SUPPLIER_COLS}
        values["name"] = name
        now = _now()
        self.repo.backup("daily")
        try:
            with self.repo.connect(write=True) as con:
                if supplier_id is None:
                    row = con.execute("SELECT id FROM suppliers WHERE name=? COLLATE NOCASE", (name,)).fetchone()
                    supplier_id = row["id"] if row else None
                if supplier_id is None:
                    cols = _SUPPLIER_COLS + ["created_at", "updated_at"]
                    cur = con.execute(f"INSERT INTO suppliers({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                      [values[k] for k in _SUPPLIER_COLS] + [now, now])
                    return int(cur.lastrowid)
                sets = ",".join(f"{k}=?" for k in _SUPPLIER_COLS)
                con.execute(f"UPDATE suppliers SET {sets}, updated_at=? WHERE id=?",
                            [values[k] for k in _SUPPLIER_COLS] + [now, supplier_id])
                return int(supplier_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError("يوجد مورد آخر بنفس الاسم.") from exc

    def list_suppliers(self, search: str = "") -> List[Dict[str, Any]]:
        with self.repo.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM suppliers ORDER BY name COLLATE NOCASE")]
        needle = normalize_ar(search)
        if needle:
            rows = [r for r in rows if needle in normalize_ar(
                " ".join(str(r.get(k, "")) for k in ("name", "commercial_title", "rc", "phone")))]
        return rows

    def get_supplier(self, supplier_id: int) -> Optional[Dict[str, Any]]:
        with self.repo.connect() as con:
            row = con.execute("SELECT * FROM suppliers WHERE id=?", (int(supplier_id),)).fetchone()
        return dict(row) if row else None

    def delete_supplier(self, supplier_id: int) -> None:
        self.repo.backup("daily")
        with self.repo.connect(write=True) as con:
            con.execute("DELETE FROM suppliers WHERE id=?", (int(supplier_id),))

    @staticmethod
    def _supplier_id_for(con, fields: Dict[str, Any], create: bool = True) -> Optional[int]:
        name = clean_name(fields.get("name"))
        if not name:
            raise ValueError("اسم المورد مطلوب لتكوين رقم فاتورة الشراء.")
        row = con.execute("SELECT id FROM suppliers WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if row:
            return int(row["id"])
        if not create:
            return None
        values = {k: str(fields.get(k, "") or "").strip() for k in _SUPPLIER_COLS}
        values["name"] = name
        now = _now()
        cols = _SUPPLIER_COLS + ["created_at", "updated_at"]
        cur = con.execute(f"INSERT INTO suppliers({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                          [values[k] for k in _SUPPLIER_COLS] + [now, now])
        return int(cur.lastrowid)

    @staticmethod
    def _next_supplier_id(con) -> int:
        try:
            row = con.execute("SELECT seq FROM sqlite_sequence WHERE name='suppliers'").fetchone()
            top = int(row[0]) if row else 0
        except sqlite3.Error:
            top = 0
        return max(top, int(con.execute("SELECT COALESCE(MAX(id),0) FROM suppliers").fetchone()[0])) + 1

    # ------------------------------------------------------------------ #
    # numbering                                                           #
    # ------------------------------------------------------------------ #
    def _next_free_no(self, con, day: dt.date, supplier_id: int):
        key = f"pur:{month_key(day)}"
        row = con.execute("SELECT value FROM number_counters WHERE key=?", (key,)).fetchone()
        counter = int(row["value"]) if row else 0
        prefix = PURCHASE_PREFIX + date_part(day)
        top = con.execute(
            "SELECT MAX(CAST(substr(value, ?) AS INTEGER)) FROM used_numbers "
            "WHERE kind='purchase' AND length(value) >= ? AND substr(value,1,?)=?",
            (_NO_DIGITS_FROM + 1, _NO_DIGITS_FROM + 1, len(prefix), prefix)).fetchone()[0] or 0
        seq = max(counter, int(top)) + 1
        for _ in range(10000):
            candidate = format_purchase_no(day, supplier_id, seq)
            if not con.execute("SELECT 1 FROM used_numbers WHERE kind='purchase' AND value=?", (candidate,)).fetchone() \
                    and not con.execute("SELECT 1 FROM purchases WHERE purchase_no=?", (candidate,)).fetchone():
                return candidate, seq
            seq += 1
        raise ValueError("تعذر إيجاد رقم فاتورة شراء غير مستخدم.")

    def preview_no(self, day: dt.date, supplier_name: str = "") -> str:
        with self.repo.connect() as con:
            sid = None
            if clean_name(supplier_name):
                sid = self._supplier_id_for(con, {"name": supplier_name}, create=False)
            return self._next_free_no(con, day, sid or self._next_supplier_id(con))[0]

    def reserve_numbers(self, day: dt.date, supplier: Dict[str, Any], serial_hint: str = "") -> PurchaseReservation:
        self.repo.backup("daily")
        with self.repo.connect(write=True) as con:
            sid = self._supplier_id_for(con, supplier)
            number, seq = self._next_free_no(con, day, sid)
            serial = self.repo._fresh_serial(con, serial_hint)
            month = month_key(day)
            con.execute("INSERT INTO number_counters(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=MAX(value, excluded.value)", (f"pur:{month}", seq))
            con.execute("INSERT INTO used_numbers(kind,value) VALUES('purchase',?)", (number,))
            con.execute("INSERT INTO used_numbers(kind,value) VALUES('serial',?)", (serial,))
        return PurchaseReservation(number, serial, sid, month, seq)

    def release_numbers(self, reservation: PurchaseReservation) -> None:
        """Give the number back after a failed issue (only if nobody took a later one). The serial stays burned."""
        try:
            with self.repo.connect(write=True) as con:
                key = f"pur:{reservation.month}"
                row = con.execute("SELECT value FROM number_counters WHERE key=?", (key,)).fetchone()
                if row and int(row["value"]) == reservation.seq and not con.execute(
                        "SELECT 1 FROM purchases WHERE purchase_no=?", (reservation.invoice_no,)).fetchone():
                    con.execute("UPDATE number_counters SET value=? WHERE key=?", (reservation.seq - 1, key))
                    con.execute("DELETE FROM used_numbers WHERE kind='purchase' AND value=?", (reservation.invoice_no,))
        except sqlite3.Error:
            log.exception("could not release purchase numbers (harmless: they stay burned)")

    # ------------------------------------------------------------------ #
    # purchases                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_products(con, items: List[Dict[str, Any]]) -> List[str]:
        """Every purchased line must point to a product: link by name, creating the products that do not exist yet
        (and bringing archived ones back). Returns the names of the products created."""
        link_items(con, items)
        created, now, made = [], _now(), {}
        for item in items:
            if item.get("product_id") is None:
                name = clean_name(item.get("description"))
                norm = normalize_ar(name)
                if not name or len(name) > PRODUCT_NAME_MAX:
                    raise ValueError(f"اسم الصنف «{name[:30]}» غير صالح للمخزون (1 إلى {PRODUCT_NAME_MAX} حرفًا).")
                if norm not in made:                       # the same new name on two lines is ONE new product
                    cur = con.execute("INSERT INTO products(name,name_norm,created_at,updated_at) VALUES(?,?,?,?)",
                                      (name, norm, now, now))
                    made[norm] = int(cur.lastrowid)
                    created.append(name)
                item["product_id"] = made[norm]
            else:
                con.execute("UPDATE products SET archived=0 WHERE id=? AND archived=1", (item["product_id"],))
        return created

    @staticmethod
    def _items_json(items: List[Dict[str, Any]]) -> str:
        return json.dumps([{"description": i["description"], "quantity": i["quantity"], "unit_price": i["unit_price"],
                            "line_total": i["line_total"], "product_id": i.get("product_id")} for i in items],
                          ensure_ascii=False, default=_json_default)

    def add_purchase(self, data: Dict[str, Any], file_path: str = "", xlsm_path: str = ""):
        """Insert the purchase and its stock movements atomically. Returns (purchase_id, created_product_names)."""
        self.repo.backup("daily")
        items = [dict(i) for i in data["items"]]
        iso = iso_or_today(data["invoice_date"])
        try:
            with self.repo.connect(write=True) as con:
                created = self._resolve_products(con, items)
                cur = con.execute(
                    """INSERT INTO purchases(purchase_no,serial_no,invoice_date,invoice_date_iso,supplier_name,supplier_json,
                       buyer_json,items_json,subtotal,vat_rate,vat_amount,total,payment_method,file_path,xlsm_path,
                       app_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (data["invoice_no"], data["serial_no"], data["invoice_date"], iso, data["supplier"]["name"],
                     json.dumps(data["supplier"], ensure_ascii=False), json.dumps(data["buyer"], ensure_ascii=False),
                     self._items_json(items), float(data["subtotal"]), float(data["vat_rate"]), float(data["vat_amount"]),
                     float(data["total"]), data["payment_method"], str(file_path or ""), str(xlsm_path or ""),
                     __version__, _now()))
                purchase_id = int(cur.lastrowid)
                con.execute("INSERT OR IGNORE INTO used_numbers(kind,value) VALUES('purchase',?)", (data["invoice_no"],))
                con.execute("INSERT OR IGNORE INTO used_numbers(kind,value) VALUES('serial',?)", (data["serial_no"],))
                replace_movements(con, "purchase", purchase_id, "purchase", iso, items, +1, with_cost=True)
        except sqlite3.IntegrityError as exc:
            raise ValueError("رقم فاتورة الشراء أو الرقم التسلسلي مستخدم مسبقًا.") from exc
        return purchase_id, created

    def update_purchase(self, purchase_id: int, data: Dict[str, Any], file_path: str = "", xlsm_path: str = ""):
        """Rewrite an issued purchase (its number and serial never change) and its stock movements. Returns the
        names of the products created."""
        self.repo.backup("pre-edit", force=True)
        items = [dict(i) for i in data["items"]]
        iso = iso_or_today(data["invoice_date"])
        with self.repo.connect(write=True) as con:
            row = con.execute("SELECT * FROM purchases WHERE id=?", (int(purchase_id),)).fetchone()
            if not row:
                raise ValueError("فاتورة الشراء غير موجودة.")
            con.execute("INSERT INTO purchase_log(purchase_id,purchase_no,action,at,snapshot) VALUES(?,?,?,?,?)",
                        (purchase_id, row["purchase_no"], "edit", _now(),
                         json.dumps(dict(row), ensure_ascii=False, default=_json_default)))
            created = self._resolve_products(con, items)
            con.execute(
                """UPDATE purchases SET invoice_date=?, invoice_date_iso=?, supplier_name=?, supplier_json=?, buyer_json=?,
                   items_json=?, subtotal=?, vat_rate=?, vat_amount=?, total=?, payment_method=?, file_path=?, xlsm_path=?,
                   app_version=?, updated_at=? WHERE id=?""",
                (data["invoice_date"], iso, data["supplier"]["name"], json.dumps(data["supplier"], ensure_ascii=False),
                 json.dumps(data["buyer"], ensure_ascii=False), self._items_json(items), float(data["subtotal"]),
                 float(data["vat_rate"]), float(data["vat_amount"]), float(data["total"]), data["payment_method"],
                 str(file_path or ""), str(xlsm_path or ""), __version__, _now(), int(purchase_id)))
            replace_movements(con, "purchase", int(purchase_id), "purchase", iso, items, +1, with_cost=True)
        return created

    def update_files(self, purchase_id: int, file_path: str, xlsm_path: str) -> None:
        with self.repo.connect(write=True) as con:
            con.execute("UPDATE purchases SET file_path=?, xlsm_path=? WHERE id=?",
                        (str(file_path or ""), str(xlsm_path or ""), int(purchase_id)))

    def delete_purchase(self, purchase_id: int) -> Optional[Dict[str, Any]]:
        """Remove a purchase and the stock it added (a full copy stays in ``purchase_log``; its number stays burned)."""
        self.repo.backup("pre-delete", force=True)
        with self.repo.connect(write=True) as con:
            row = con.execute("SELECT * FROM purchases WHERE id=?", (int(purchase_id),)).fetchone()
            if not row:
                return None
            rec = dict(row)
            con.execute("INSERT INTO purchase_log(purchase_id,purchase_no,action,at,snapshot) VALUES(?,?,?,?,?)",
                        (purchase_id, rec["purchase_no"], "delete", _now(),
                         json.dumps(rec, ensure_ascii=False, default=_json_default)))
            con.execute("INSERT OR IGNORE INTO used_numbers(kind,value) VALUES('purchase',?)", (rec["purchase_no"],))
            con.execute("INSERT OR IGNORE INTO used_numbers(kind,value) VALUES('serial',?)", (rec["serial_no"],))
            remove_movements(con, "purchase", int(purchase_id))
            con.execute("DELETE FROM purchases WHERE id=?", (int(purchase_id),))
        return rec

    def list_purchases(self, search: str = "", limit: int = 3000) -> List[Dict[str, Any]]:
        with self.repo.connect() as con:
            rows = [dict(r) for r in con.execute(
                """SELECT id, purchase_no, serial_no, invoice_date, invoice_date_iso, supplier_name, total, payment_method,
                          file_path, xlsm_path FROM purchases ORDER BY id DESC LIMIT ?""", (limit,))]
        needle = normalize_ar(search)
        if needle:
            rows = [r for r in rows if needle in normalize_ar(
                f"{r['purchase_no']} {r['serial_no']} {r['supplier_name']} {r['invoice_date']}")]
        return rows

    def get_purchase(self, purchase_id: int) -> Optional[Dict[str, Any]]:
        with self.repo.connect() as con:
            row = con.execute("SELECT * FROM purchases WHERE id=?", (int(purchase_id),)).fetchone()
        if not row:
            return None
        rec = dict(row)
        items = []
        for it in json.loads(rec["items_json"] or "[]"):
            items.append({"description": str(it.get("description", "")), "quantity": to_decimal(it.get("quantity", 0)),
                          "unit_price": to_decimal(it.get("unit_price", 0)), "line_total": to_decimal(it.get("line_total", 0)),
                          "product_id": it.get("product_id")})
        method = PAYMENT_ALIASES.get(rec["payment_method"], rec["payment_method"]) or "cash"
        return {
            "id": rec["id"], "invoice_no": rec["purchase_no"], "serial_no": rec["serial_no"],
            "invoice_date": rec["invoice_date"], "supplier": json.loads(rec["supplier_json"] or "{}"),
            "buyer": {**DEFAULT_SELLER, **json.loads(rec["buyer_json"] or "{}")}, "items": items,
            "subtotal": to_decimal(rec["subtotal"]), "vat_rate": to_decimal(rec["vat_rate"]),
            "vat_amount": to_decimal(rec["vat_amount"]), "total": to_decimal(rec["total"]), "payment_method": method,
            "file_path": rec["file_path"], "xlsm_path": rec["xlsm_path"] or "",
        }
