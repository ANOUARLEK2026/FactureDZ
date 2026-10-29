# -*- coding: utf-8 -*-
"""Stock: products, stock movements, availability checks and reports.

* Every quantity is stored as an integer number of grams (kg x 1000): sums are exact.
* The balance of a product is simply the sum of its movements (opening stock, purchases, sales, adjustments),
  so it can never drift away from the documents that caused it.
* Sale and purchase documents write their movements INSIDE the same database transaction as the document
  itself (see ``db.Repository`` and ``purchasing.PurchaseStore``): either both are saved or neither.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, List, Optional

from .amounts import MAX_AMOUNT, parse_decimal, q2, to_decimal
from .config import ADJUST_REASONS, PRODUCT_NAME_MAX, QTY_SCALE
from .textnorm import normalize_ar

log = logging.getLogger("facturedz.inventory")

ZERO = Decimal(0)


class StockError(ValueError):
    """User-presentable (Arabic) stock problem."""


# --------------------------------------------------------------------------- #
# pure helpers                                                                #
# --------------------------------------------------------------------------- #
def to_milli(value: Any) -> int:
    """Decimal kilograms -> integer grams (half up)."""
    return int((to_decimal(value) * QTY_SCALE).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_milli(milli: Any) -> Decimal:
    return Decimal(int(milli or 0)) / QTY_SCALE


def clean_name(text: Any) -> str:
    return " ".join(str(text or "").split())


def stock_status(on_hand: Decimal, min_stock: Decimal) -> str:
    """negative | out | low | ok"""
    if on_hand < 0:
        return "negative"
    if on_hand == 0:
        return "out"
    if min_stock > 0 and on_hand <= min_stock:
        return "low"
    return "ok"


STATUS_LABELS = {"ok": "متوفر", "low": "منخفض", "out": "نفد", "negative": "ناقص (سالب)"}


class Catalog:
    """In-memory view of the products (for the item tables: instant name matching and stock figures)."""

    def __init__(self, products: Iterable[Dict[str, Any]] = ()):
        self.products: List[Dict[str, Any]] = list(products)
        self._by_id = {p["id"]: p for p in self.products}
        self._by_norm = {normalize_ar(p["name"]): p for p in self.products}

    def get(self, product_id: Optional[int]) -> Optional[Dict[str, Any]]:
        return self._by_id.get(product_id) if product_id is not None else None

    def find(self, name: str) -> Optional[Dict[str, Any]]:
        key = normalize_ar(clean_name(name))
        return self._by_norm.get(key) if key else None

    def names(self) -> List[str]:
        return [p["name"] for p in self.products if not p.get("archived")]


# --------------------------------------------------------------------------- #
# schema                                                                      #
# --------------------------------------------------------------------------- #
def ensure_schema(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        name_norm TEXT NOT NULL,
        sell_price REAL NOT NULL DEFAULT 0,
        min_stock_milli INTEGER NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT '',
        archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '')""")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_norm ON products(name_norm)")
    con.execute("""CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL REFERENCES products(id),
        kind TEXT NOT NULL,
        qty_milli INTEGER NOT NULL,
        unit_cost REAL NOT NULL DEFAULT 0,
        moved_on TEXT NOT NULL,
        ref_kind TEXT NOT NULL DEFAULT '',
        ref_id INTEGER,
        reason TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '')""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_moves_product ON stock_movements(product_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_moves_ref ON stock_movements(ref_kind, ref_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_moves_date ON stock_movements(moved_on)")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# helpers used INSIDE other transactions (take an open connection)            #
# --------------------------------------------------------------------------- #
def link_items(con, items: List[Dict[str, Any]]) -> None:
    """Set ``product_id`` on every item: keep a valid one, otherwise match the description with the catalogue
    (None when there is no such product)."""
    for item in items:
        pid = item.get("product_id")
        if pid is not None:
            try:
                if con.execute("SELECT 1 FROM products WHERE id=?", (int(pid),)).fetchone():
                    item["product_id"] = int(pid)
                    continue
            except (TypeError, ValueError):
                pass
        norm = normalize_ar(clean_name(item.get("description", "")))
        row = con.execute("SELECT id FROM products WHERE name_norm=?", (norm,)).fetchone() if norm else None
        item["product_id"] = int(row["id"]) if row else None


def replace_movements(con, ref_kind: str, ref_id: int, kind: str, iso_date: str, items: List[Dict[str, Any]],
                      sign: int, with_cost: bool = False) -> None:
    """Delete the movements a document wrote before and write the current ones (used for new, edited documents)."""
    con.execute("DELETE FROM stock_movements WHERE ref_kind=? AND ref_id=?", (ref_kind, ref_id))
    now = _now()
    for item in items:
        pid = item.get("product_id")
        if pid is None:
            continue
        qty = to_milli(item.get("quantity", 0))
        if qty == 0:
            continue
        cost = float(to_decimal(item.get("unit_price", 0))) if with_cost else 0.0
        con.execute("INSERT INTO stock_movements(product_id,kind,qty_milli,unit_cost,moved_on,ref_kind,ref_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)", (int(pid), kind, sign * qty, cost, iso_date, ref_kind, int(ref_id), now))


def remove_movements(con, ref_kind: str, ref_id: int) -> None:
    con.execute("DELETE FROM stock_movements WHERE ref_kind=? AND ref_id=?", (ref_kind, ref_id))


def on_hand_milli(con, product_id: int) -> int:
    return int(con.execute("SELECT COALESCE(SUM(qty_milli),0) FROM stock_movements WHERE product_id=?",
                           (int(product_id),)).fetchone()[0])


# --------------------------------------------------------------------------- #
# the store                                                                   #
# --------------------------------------------------------------------------- #
_COST_FILTER = "product_id=p.id AND kind IN ('purchase','initial') AND qty_milli>0 AND unit_cost>0"
_PRODUCT_SQL = f"""
SELECT p.id, p.name, p.sell_price, p.min_stock_milli, p.notes, p.archived, p.created_at,
  COALESCE((SELECT SUM(qty_milli) FROM stock_movements WHERE product_id=p.id), 0) AS on_hand_milli,
  (SELECT unit_cost FROM stock_movements WHERE {_COST_FILTER} ORDER BY moved_on DESC, id DESC LIMIT 1) AS last_cost,
  (SELECT SUM(qty_milli * unit_cost) / SUM(qty_milli) FROM stock_movements WHERE {_COST_FILTER}) AS avg_cost,
  (SELECT MAX(moved_on) FROM stock_movements WHERE product_id=p.id) AS last_move
FROM products p {{where}} ORDER BY p.name COLLATE NOCASE"""


def _money(value: Any) -> Decimal:
    return q2(Decimal(str(value))) if value else ZERO


def _product_dict(row) -> Dict[str, Any]:
    on_hand = from_milli(row["on_hand_milli"])
    minimum = from_milli(row["min_stock_milli"])
    avg = _money(row["avg_cost"])
    return {
        "id": int(row["id"]), "name": row["name"], "sell_price": _money(row["sell_price"]),
        "min_stock": minimum, "notes": row["notes"] or "", "archived": bool(row["archived"]),
        "created_at": row["created_at"] or "", "on_hand": on_hand, "last_cost": _money(row["last_cost"]),
        "avg_cost": avg, "value": q2(max(on_hand, ZERO) * avg), "status": stock_status(on_hand, minimum),
        "last_move": row["last_move"] or "",
    }


def _parse_amount(text: Any, what: str, maximum: Decimal = MAX_AMOUNT) -> Decimal:
    try:
        value = parse_decimal(text)
    except ValueError:
        raise StockError(f"{what} غير صحيح.") from None
    if value < 0:
        raise StockError(f"{what} لا يمكن أن يكون سالبًا.")
    if value > maximum:
        raise StockError(f"{what} كبير جدًا.")
    return value


class StockStore:
    def __init__(self, repo):
        self.repo = repo

    # ---- products ------------------------------------------------------------------ #
    def list_products(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        where = "" if include_archived else "WHERE p.archived=0"
        with self.repo.connect() as con:
            return [_product_dict(r) for r in con.execute(_PRODUCT_SQL.format(where=where))]

    def catalog(self) -> Catalog:
        """Every product, archived ones included (a sale may still reference them); ``names()`` hides the archived."""
        return Catalog(self.list_products(include_archived=True))

    def get_product(self, product_id: int) -> Optional[Dict[str, Any]]:
        with self.repo.connect() as con:
            row = con.execute(_PRODUCT_SQL.format(where="WHERE p.id=?"), (int(product_id),)).fetchone()
        return _product_dict(row) if row else None

    def find_product(self, name: str) -> Optional[Dict[str, Any]]:
        norm = normalize_ar(clean_name(name))
        if not norm:
            return None
        with self.repo.connect() as con:
            row = con.execute("SELECT id FROM products WHERE name_norm=?", (norm,)).fetchone()
        return self.get_product(row["id"]) if row else None

    def save_product(self, fields: Dict[str, Any], product_id: Optional[int] = None,
                     opening_qty: Any = None, opening_cost: Any = None) -> int:
        """Create (``product_id`` None) or update a product. Opening stock is only honoured for a new product."""
        name = clean_name(fields.get("name"))
        if not name:
            raise StockError("اسم الصنف مطلوب.")
        if len(name) > PRODUCT_NAME_MAX:
            raise StockError(f"اسم الصنف طويل جدًا (الحد الأقصى {PRODUCT_NAME_MAX} حرفًا).")
        sell = _parse_amount(fields.get("sell_price", ""), "سعر البيع")
        minimum = _parse_amount(fields.get("min_stock", ""), "الحد الأدنى للتنبيه", Decimal(10 ** 7))
        notes = str(fields.get("notes", "") or "").strip()
        qty = _parse_amount(opening_qty if opening_qty is not None else "", "الكمية الافتتاحية", Decimal(10 ** 7)) \
            if product_id is None else ZERO
        cost = _parse_amount(opening_cost if opening_cost is not None else "", "سعر شراء الرصيد الافتتاحي") \
            if product_id is None else ZERO
        norm, now = normalize_ar(name), _now()
        self.repo.backup("daily")
        try:
            with self.repo.connect(write=True) as con:
                clash = con.execute("SELECT id FROM products WHERE name_norm=? AND id IS NOT ?",
                                    (norm, product_id)).fetchone()
                if clash:
                    raise StockError("يوجد صنف آخر بنفس الاسم.")
                if product_id is None:
                    cur = con.execute("INSERT INTO products(name,name_norm,sell_price,min_stock_milli,notes,created_at,"
                                      "updated_at) VALUES(?,?,?,?,?,?,?)",
                                      (name, norm, float(sell), to_milli(minimum), notes, now, now))
                    product_id = int(cur.lastrowid)
                    if to_milli(qty) > 0:
                        con.execute("INSERT INTO stock_movements(product_id,kind,qty_milli,unit_cost,moved_on,reason,"
                                    "created_at) VALUES(?,?,?,?,?,?,?)",
                                    (product_id, "initial", to_milli(qty), float(cost), dt.date.today().isoformat(),
                                     "opening", now))
                else:
                    cur = con.execute("UPDATE products SET name=?, name_norm=?, sell_price=?, min_stock_milli=?, notes=?, "
                                      "updated_at=? WHERE id=?", (name, norm, float(sell), to_milli(minimum), notes, now,
                                                                  int(product_id)))
                    if cur.rowcount == 0:
                        raise StockError("الصنف غير موجود.")
        except sqlite3.IntegrityError as exc:
            raise StockError("يوجد صنف آخر بنفس الاسم.") from exc
        return int(product_id)

    def set_archived(self, product_id: int, archived: bool) -> None:
        with self.repo.connect(write=True) as con:
            con.execute("UPDATE products SET archived=?, updated_at=? WHERE id=?",
                        (1 if archived else 0, _now(), int(product_id)))

    def delete_product(self, product_id: int) -> None:
        """Only a product that never had a movement can be deleted (otherwise archive it)."""
        self.repo.backup("daily")
        with self.repo.connect(write=True) as con:
            if con.execute("SELECT 1 FROM stock_movements WHERE product_id=? LIMIT 1", (int(product_id),)).fetchone():
                raise StockError("لهذا الصنف حركات مسجّلة فلا يمكن حذفه. يمكنك أرشفته ليختفي من القوائم.")
            con.execute("DELETE FROM products WHERE id=?", (int(product_id),))

    def import_from_invoices(self) -> int:
        """Create a product (with its last selling price, no quantity) for every item description found in the
        saved sale invoices. Quantities are NOT touched: invoices issued before stock tracking never change stock."""
        import json
        with self.repo.connect() as con:
            rows = con.execute("SELECT items_json FROM invoices ORDER BY invoice_date_iso, id").fetchall()
        found: Dict[str, Any] = {}
        for row in rows:
            try:
                items = json.loads(row["items_json"] or "[]")
            except ValueError:
                continue
            for it in items:
                name = clean_name(it.get("description"))
                norm = normalize_ar(name)
                if norm and len(name) <= PRODUCT_NAME_MAX:
                    found[norm] = (name, float(it.get("unit_price") or 0))
        if not found:
            return 0
        self.repo.backup("daily")
        created, now = 0, _now()
        with self.repo.connect(write=True) as con:
            for norm, (name, price) in found.items():
                if con.execute("SELECT 1 FROM products WHERE name_norm=?", (norm,)).fetchone():
                    continue
                con.execute("INSERT INTO products(name,name_norm,sell_price,created_at,updated_at) VALUES(?,?,?,?,?)",
                            (name, norm, price, now, now))
                created += 1
        return created

    # ---- adjustments --------------------------------------------------------------- #
    def adjust(self, product_id: int, mode: str, qty: Any, reason: str = "other", note: str = "") -> Decimal:
        """mode: 'set' (the counted quantity), 'add' or 'remove'. Returns the signed change in kg."""
        if mode not in ("set", "add", "remove"):
            raise StockError("نوع التعديل غير معروف.")
        amount = _parse_amount(qty, "الكمية", Decimal(10 ** 7))
        if mode != "set" and to_milli(amount) == 0:
            raise StockError("الكمية يجب أن تكون أكبر من صفر.")
        if reason not in dict(ADJUST_REASONS):
            reason = "other"
        self.repo.backup("daily")
        with self.repo.connect(write=True) as con:
            if not con.execute("SELECT 1 FROM products WHERE id=?", (int(product_id),)).fetchone():
                raise StockError("الصنف غير موجود.")
            current = on_hand_milli(con, product_id)
            delta = {"set": to_milli(amount) - current, "add": to_milli(amount), "remove": -to_milli(amount)}[mode]
            if delta == 0:
                raise StockError("لا يوجد تغيير: الكمية المدخلة تساوي الرصيد الحالي.")
            con.execute("INSERT INTO stock_movements(product_id,kind,qty_milli,moved_on,reason,note,created_at)"
                        " VALUES(?,?,?,?,?,?,?)", (int(product_id), "adjust", delta, dt.date.today().isoformat(),
                                                   reason, str(note or "").strip(), _now()))
        return from_milli(delta)

    # ---- movements ----------------------------------------------------------------- #
    def list_movements(self, product_id: Optional[int] = None, kind: str = "", date_from: str = "", date_to: str = "",
                       search: str = "", limit: int = 5000) -> List[Dict[str, Any]]:
        where, args = [], []
        if product_id is not None:
            where.append("m.product_id=?")
            args.append(int(product_id))
        if kind:
            where.append("m.kind=?")
            args.append(kind)
        if date_from:
            where.append("m.moved_on>=?")
            args.append(date_from)
        if date_to:
            where.append("m.moved_on<=?")
            args.append(date_to)
        sql = ("SELECT m.id, m.product_id, m.kind, m.qty_milli, m.unit_cost, m.moved_on, m.ref_kind, m.ref_id, m.reason,"
               " m.note, p.name AS product_name, COALESCE(i.invoice_no, pu.purchase_no, '') AS ref_no "
               "FROM stock_movements m JOIN products p ON p.id=m.product_id "
               "LEFT JOIN invoices i ON m.ref_kind='invoice' AND i.id=m.ref_id "
               "LEFT JOIN purchases pu ON m.ref_kind='purchase' AND pu.id=m.ref_id "
               + ("WHERE " + " AND ".join(where) + " " if where else "")
               + "ORDER BY m.moved_on DESC, m.id DESC")
        with self.repo.connect() as con:
            rows = [dict(r) for r in con.execute(sql, args)]
        needle = normalize_ar(search)
        out = []
        for r in rows:
            if needle and needle not in normalize_ar(f"{r['product_name']} {r['ref_no']} {r['note']}"):
                continue
            r["qty"] = from_milli(r["qty_milli"])
            r["unit_cost"] = _money(r["unit_cost"])
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def product_history(self, product_id: int, limit: int = 40) -> List[Dict[str, Any]]:
        return self.list_movements(product_id=product_id, limit=limit)

    # ---- availability -------------------------------------------------------------- #
    def sale_credit(self, invoice_id: int) -> Dict[int, Decimal]:
        """Kilograms an already-issued sale invoice took out of each product (added back while it is edited)."""
        with self.repo.connect() as con:
            rows = con.execute("SELECT product_id, -SUM(qty_milli) AS q FROM stock_movements "
                               "WHERE ref_kind='invoice' AND ref_id=? GROUP BY product_id", (int(invoice_id),)).fetchall()
        return {int(r["product_id"]): from_milli(r["q"]) for r in rows}

    def shortages(self, items: List[Dict[str, Any]], exclude_invoice_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Products the items would take below zero: [{name, requested, available}] (kg)."""
        wanted: Dict[int, int] = {}
        for it in items:
            if it.get("product_id") is not None:
                wanted[int(it["product_id"])] = wanted.get(int(it["product_id"]), 0) + to_milli(it.get("quantity", 0))
        out = []
        with self.repo.connect() as con:
            for pid, qty in wanted.items():
                have = on_hand_milli(con, pid)
                if exclude_invoice_id is not None:
                    have += int(con.execute("SELECT COALESCE(-SUM(qty_milli),0) FROM stock_movements WHERE ref_kind='invoice' "
                                            "AND ref_id=? AND product_id=?", (int(exclude_invoice_id), pid)).fetchone()[0])
                if qty > have:
                    name = con.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()["name"]
                    out.append({"product_id": pid, "name": name, "requested": from_milli(qty), "available": from_milli(have)})
        return out

    def negative_after(self, ref_kind: str, ref_id: int, new_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Products that would go (further) below zero if the document (ref_kind, ref_id) is removed / replaced by
        ``new_items``: [{name, after}]. Used before editing or deleting a purchase invoice."""
        new: Dict[int, int] = {}
        for it in new_items:
            if it.get("product_id") is not None:
                new[int(it["product_id"])] = new.get(int(it["product_id"]), 0) + to_milli(it.get("quantity", 0))
        out = []
        with self.repo.connect() as con:
            old = {int(r["product_id"]): int(r["q"]) for r in con.execute(
                "SELECT product_id, SUM(qty_milli) AS q FROM stock_movements WHERE ref_kind=? AND ref_id=? GROUP BY product_id",
                (ref_kind, int(ref_id)))}
            for pid in set(old) | set(new):
                now_qty = on_hand_milli(con, pid)
                after = now_qty - old.get(pid, 0) + new.get(pid, 0)
                if after < 0 and after < now_qty:
                    name = con.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()["name"]
                    out.append({"product_id": pid, "name": name, "after": from_milli(after)})
        return out

    # ---- reports ------------------------------------------------------------------- #
    def summary(self) -> Dict[str, Any]:
        products = self.list_products()
        with self.repo.connect() as con:
            moves = int(con.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0])
            last = con.execute("SELECT MAX(moved_on) FROM stock_movements").fetchone()[0] or ""
        return {
            "count": len(products),
            "value": q2(sum((p["value"] for p in products), ZERO)),
            "attention": sum(1 for p in products if p["status"] != "ok"),
            "negative": sum(1 for p in products if p["status"] == "negative"),
            "movements": moves, "last_move": last,
        }
