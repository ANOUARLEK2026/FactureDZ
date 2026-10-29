# -*- coding: utf-8 -*-
"""Stock page: every product with its quantity, cost and status, plus the product card and its latest movements."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLineEdit, QMenu, QScrollArea, QVBoxLayout, QWidget

from ...amounts import money, qty_text
from ...config import ADJUST_REASONS, MOVEMENT_KINDS, STOCK_UNIT
from ...dates import iso_to_display
from ...inventory import STATUS_LABELS, StockError
from ...textnorm import normalize_ar
from .. import dialogs, theme
from ..stock_dialogs import AdjustDialog
from ..widgets import (Card, FieldGrid, HAYSTACK_ROLE, ID_ROLE, Segmented, StatTile, button, label, list_item,
                       make_list_view)

log = logging.getLogger("facturedz.ui.stock")
HEADERS = ["الصنف", f"المتوفر ({STOCK_UNIT})", "الحالة", "آخر سعر شراء", "سعر البيع", "قيمة المخزون (دج)"]
WIDTHS = (190, 100, 90, 105, 90, 125)
CARD_WIDTH = 410
FILTERS = [("all", "الكل"), ("ok", "متوفر"), ("low", "منخفض"), ("out", "نفد / ناقص"), ("archived", "مؤرشف")]
_STATUS_COLOR = {"ok": "ok", "low": "warn", "out": "danger", "negative": "danger"}
_KIND = dict(MOVEMENT_KINDS)
_REASON = dict(ADJUST_REASONS)


def movement_label(move: Dict[str, Any]) -> str:
    text = _KIND.get(move["kind"], move["kind"])
    if move["kind"] == "adjust":
        text += f" — {_REASON.get(move['reason'], '')}".rstrip(" —")
    return text


def matches_filter(product: Dict[str, Any], key: str) -> bool:
    if key == "archived":
        return product["archived"]
    if product["archived"]:
        return False
    if key == "all":
        return True
    if key == "out":
        return product["status"] in ("out", "negative")
    return product["status"] == key


class StockPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.setObjectName("Page")
        self.app = app
        self.selected_id: Optional[int] = None
        self._records: List[Dict[str, Any]] = []
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(12)

        # ---- right: figures, toolbar, list ---------------------------------------------------
        right = QVBoxLayout()
        right.setSpacing(10)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.t_count = StatTile("الأصناف", "في الكتالوج", theme.P["accent"])
        self.t_value = StatTile("قيمة المخزون", "بمتوسط سعر الشراء (دج)", theme.P["gold"])
        self.t_attention = StatTile("تحتاج انتباهًا", "منخفض أو نفد أو ناقص", theme.P["danger"])
        self.t_last = StatTile("آخر حركة", "تاريخ آخر حركة مسجّلة", theme.P["muted"])
        for tile in (self.t_count, self.t_value, self.t_attention, self.t_last):
            tiles.addWidget(tile, 1)
        right.addLayout(tiles)

        bar = QFrame()
        bar.setObjectName("Toolbar")
        br = QHBoxLayout(bar)
        br.setContentsMargins(14, 8, 14, 8)
        br.addWidget(label("بحث"))
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(140)
        self.search.setPlaceholderText("اسم الصنف")
        self.search.textChanged.connect(self._filter)
        br.addWidget(self.search)
        self.filters = Segmented(FILTERS, "all")
        self.filters.changed.connect(lambda _k: self._populate())
        br.addWidget(self.filters)
        br.addStretch(1)
        br.addWidget(button("＋  صنف جديد", "ghost", self.clear))
        right.addWidget(bar)
        self.count = label("0 صنف", "Muted")

        self.view, self.model, self.proxy = make_list_view(HEADERS, WIDTHS)
        self.view.selectionModel().selectionChanged.connect(self._on_select)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        right.addWidget(self.view, 1)
        right.addWidget(self.count)
        self.empty = label("", "Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        right.addWidget(self.empty)
        self.import_button = button("استيراد الأصناف من فواتير البيع السابقة", "ghost", self.import_products)
        right.addWidget(self.import_button, 0, Qt.AlignmentFlag.AlignCenter)
        row.addLayout(right, 1)

        # ---- left: product card + history ----------------------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(CARD_WIDTH + 12)
        body = QWidget()
        body.setObjectName("ScrollBody")
        scroll.setWidget(body)
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(10)

        card = Card("بطاقة الصنف", "اختر صنفًا أو أنشئ صنفًا جديدًا")
        self.identity = label("صنف جديد")
        self.identity.setStyleSheet(f"font-size: 11pt; font-weight: bold; color: {theme.P['brand']};")
        self.hint = label("", "Muted")
        self.hint.setWordWrap(True)
        card.body.addWidget(self.identity)
        card.body.addWidget(self.hint)
        self.fields = FieldGrid([("name", "اسم الصنف"), ("sell_price", "سعر البيع (دج/كلغ)"),
                                 ("min_stock", f"حد التنبيه ({STOCK_UNIT})"), ("notes", "ملاحظات")], 2, wide=("name", "notes"))
        for key in ("sell_price", "min_stock"):
            self._numeric(self.fields.edits[key])
        self.fields.edits["name"].textChanged.connect(self._paint_identity)
        card.body.addWidget(self.fields)
        self.opening_box = QWidget()
        ob = QVBoxLayout(self.opening_box)
        ob.setContentsMargins(0, 4, 0, 0)
        ob.setSpacing(4)
        ob.addWidget(label("الرصيد الافتتاحي (اختياري — ما هو موجود فعلًا في المخزن الآن)", "Muted"))
        self.opening = FieldGrid([("opening_qty", f"الكمية ({STOCK_UNIT})"), ("opening_cost", "سعر الشراء (دج/كلغ)")], 2)
        for edit in self.opening.edits.values():
            self._numeric(edit)
        ob.addWidget(self.opening)
        card.body.addWidget(self.opening_box)
        card.body.addWidget(button("حفظ الصنف", "primary", self.save))
        pair = QHBoxLayout()
        self.adjust_button = button("تعديل الكمية / جرد", "", self.adjust)
        pair.addWidget(self.adjust_button, 1)
        self.moves_button = button("كل حركات الصنف", "", self.show_moves)
        pair.addWidget(self.moves_button, 1)
        card.body.addLayout(pair)
        pair2 = QHBoxLayout()
        self.archive_button = button("أرشفة", "", self.toggle_archive)
        pair2.addWidget(self.archive_button, 1)
        self.delete_button = button("حذف الصنف", "danger", self.delete)
        pair2.addWidget(self.delete_button, 1)
        card.body.addLayout(pair2)
        col.addWidget(card)

        hist = Card("آخر الحركات", "للصنف المحدد")
        self.hview, self.hmodel, _hproxy = make_list_view(["التاريخ", "النوع", f"الكمية ({STOCK_UNIT})", "المرجع"],
                                                          (90, 150, 90, 120))
        self.hview.setMinimumHeight(210)
        hist.body.addWidget(self.hview)
        self.hempty = label("لا توجد حركات.", "Muted")
        self.hempty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hist.body.addWidget(self.hempty)
        col.addWidget(hist)
        col.addStretch(1)
        row.addWidget(scroll)
        self.refresh()
        self.clear()

    @staticmethod
    def _numeric(edit: QLineEdit) -> None:
        edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

    # ------------------------------------------------------------------ #
    # list                                                                #
    # ------------------------------------------------------------------ #
    def _filter(self, text: str) -> None:
        self.proxy.set_needle(text)
        self._footer()

    def _footer(self) -> None:
        n = self.proxy.rowCount()
        self.count.setText(f"{n} صنف")
        total = len(self._records)
        if n:
            self.empty.setText("")
        elif total == 0:
            self.empty.setText("لا توجد أصناف بعد.\nأنشئ صنفًا من البطاقة على اليسار، أو سجّل فاتورة شراء فتُنشأ الأصناف تلقائيًا.")
        else:
            self.empty.setText("لا يوجد صنف مطابق.")
        self.empty.setVisible(n == 0)
        self.import_button.setVisible(n == 0 and total == 0)

    def refresh(self) -> None:
        self._records = self.app.repo.stock.list_products(include_archived=True)
        summary = self.app.repo.stock.summary()
        self.t_count.set(str(summary["count"]))
        self.t_value.set(money(summary["value"]))
        self.t_attention.set(str(summary["attention"]),
                             f"منها {summary['negative']} برصيد سالب" if summary["negative"] else "منخفض أو نفد أو ناقص")
        self.t_last.set(iso_to_display(summary["last_move"]) or "—")
        self._populate()
        if self.selected_id is not None:
            self._load_history()

    def _populate(self) -> None:
        key = self.filters.value()
        self.model.setRowCount(0)
        for p in (r for r in self._records if matches_filter(r, key)):
            color = theme.P[_STATUS_COLOR.get(p["status"], "ink")] if p["status"] != "ok" else ""
            items = [
                list_item(p["name"] + ("  (مؤرشف)" if p["archived"] else "")),
                list_item(qty_text(p["on_hand"]), float(p["on_hand"]), False, color),
                list_item(STATUS_LABELS[p["status"]], p["status"], False, theme.P[_STATUS_COLOR.get(p["status"], "ok")]),
                list_item(money(p["last_cost"]) if p["last_cost"] else "—", float(p["last_cost"]), False),
                list_item(money(p["sell_price"]) if p["sell_price"] else "—", float(p["sell_price"]), False),
                list_item(money(p["value"]), float(p["value"]), False),
            ]
            items[0].setData(normalize_ar(p["name"]), HAYSTACK_ROLE)
            items[0].setData(p["id"], ID_ROLE)
            self.model.appendRow(items)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self._footer()
        self._select_row(self.selected_id)

    def _select_row(self, product_id: Optional[int]) -> None:
        if product_id is None:
            return
        for r in range(self.proxy.rowCount()):
            if self.proxy.index(r, 0).data(ID_ROLE) == product_id:
                self.view.selectRow(r)
                return

    # ------------------------------------------------------------------ #
    # card                                                                #
    # ------------------------------------------------------------------ #
    def _on_select(self, *_args) -> None:
        sel = self.view.selectionModel().selectedRows()
        if not sel:
            return
        product = self.app.repo.stock.get_product(int(sel[0].siblingAtColumn(0).data(ID_ROLE)))
        if not product:
            return
        self.selected_id = product["id"]
        self.fields.set_values({"name": product["name"], "notes": product["notes"],
                                "sell_price": "" if not product["sell_price"] else qty_text(product["sell_price"]).replace(",", ""),
                                "min_stock": "" if not product["min_stock"] else qty_text(product["min_stock"]).replace(",", "")})
        self._paint_identity()
        self._load_history()
        self._apply_mode()

    def _paint_identity(self, *_args) -> None:
        name = self.fields.edits["name"].text().strip()
        self.identity.setText(name or "صنف جديد")
        if self.selected_id is None:
            self.hint.setText("لم يُحفظ بعد" if name else "املأ الحقول لإنشاء صنف جديد")
            return
        product = next((p for p in self._records if p["id"] == self.selected_id), None)
        if product:
            self.hint.setText(
                f"الرصيد الحالي: {qty_text(product['on_hand'])} {STOCK_UNIT} — {STATUS_LABELS[product['status']]}\n"
                f"متوسط سعر الشراء: {money(product['avg_cost']) if product['avg_cost'] else '—'} دج"
                f"   •   حد التنبيه: {qty_text(product['min_stock']) if product['min_stock'] else '—'}")

    def _apply_mode(self) -> None:
        new = self.selected_id is None
        product = next((p for p in self._records if p["id"] == self.selected_id), None)
        self.opening_box.setVisible(new)
        for b in (self.adjust_button, self.moves_button, self.archive_button, self.delete_button):
            b.setEnabled(not new)
        self.archive_button.setText("إلغاء الأرشفة" if product and product["archived"] else "أرشفة")

    def _load_history(self) -> None:
        self.hmodel.setRowCount(0)
        moves = self.app.repo.stock.product_history(self.selected_id, 25) if self.selected_id is not None else []
        for m in moves:
            color = theme.P["ok"] if m["qty"] > 0 else theme.P["danger"]
            sign = "+" if m["qty"] > 0 else "−"
            self.hmodel.appendRow([list_item(iso_to_display(m["moved_on"]), m["moved_on"], False),
                                   list_item(movement_label(m)),
                                   list_item(f"{sign}{qty_text(abs(m['qty']))}", float(m["qty"]), False, color),
                                   list_item(m["ref_no"] or "—", m["ref_no"], False)])
        self.hview.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.hempty.setVisible(not moves)

    def clear(self) -> None:
        self.selected_id = None
        self.view.clearSelection()
        self.fields.clear()
        self.opening.clear()
        self.search.clear()
        self._paint_identity()
        self._load_history()
        self._apply_mode()

    def save(self) -> None:
        values = self.fields.values()
        opening = self.opening.values()
        try:
            self.selected_id = self.app.repo.stock.save_product(
                values, self.selected_id, opening["opening_qty"], opening["opening_cost"])
        except StockError as exc:
            dialogs.error(self.app, str(exc))
            self.fields.edits["name"].setFocus()
            return
        self.opening.clear()
        self.app.refresh_stock()
        self._apply_mode()
        self.app.status(f"تم حفظ الصنف «{values['name']}».")

    def _selected(self) -> Optional[Dict[str, Any]]:
        if self.selected_id is None:
            dialogs.info(self.app, "حدد صنفًا من القائمة أولًا.")
            return None
        product = self.app.repo.stock.get_product(self.selected_id)
        if product is None:
            dialogs.error(self.app, "هذا الصنف لم يعد موجودًا.")
            self.clear()
            self.refresh()
        return product

    def adjust(self) -> None:
        product = self._selected()
        if not product:
            return
        dialog = AdjustDialog(self.app, product)
        if not dialog.exec():
            return
        v = dialog.values()
        try:
            delta = self.app.repo.stock.adjust(product["id"], v["mode"], v["qty"], v["reason"], v["note"])
        except StockError as exc:
            dialogs.error(self.app, str(exc))
            return
        self.app.refresh_stock()
        self.app.status(f"تم تعديل رصيد «{product['name']}» ({'+' if delta > 0 else '−'}{qty_text(abs(delta))} {STOCK_UNIT}).")

    def show_moves(self) -> None:
        product = self._selected()
        if product:
            self.app.movements_page.show_for_product(product["id"], product["name"])
            self.app.show_page("movements")

    def toggle_archive(self) -> None:
        product = self._selected()
        if not product:
            return
        self.app.repo.stock.set_archived(product["id"], not product["archived"])
        self.app.refresh_stock()
        self.app.status(("أُرجع الصنف إلى القوائم: " if product["archived"] else "أُرشف الصنف: ") + product["name"])

    def delete(self) -> None:
        product = self._selected()
        if not product:
            return
        if not dialogs.confirm(self.app, f"هل تريد حذف الصنف «{product['name']}»؟\nلا يمكن حذف صنف له حركات (يمكنك أرشفته).",
                               yes="حذف", no="إلغاء", danger=True):
            return
        try:
            self.app.repo.stock.delete_product(product["id"])
        except StockError as exc:
            dialogs.info(self.app, str(exc))
            return
        self.clear()
        self.app.refresh_stock()

    def import_products(self) -> None:
        if not dialogs.confirm(self.app, "سيُنشأ صنف لكل اسم سلعة ورد في فواتير البيع المحفوظة، بآخر سعر بيع.\n"
                                         "لن تتغير أي كمية: الفواتير القديمة لا تؤثر على المخزون.\nهل تريد المتابعة؟",
                               yes="استيراد", no="إلغاء"):
            return
        created = self.app.repo.stock.import_from_invoices()
        self.app.refresh_stock()
        dialogs.info(self.app, f"تم إنشاء {created} صنف." if created else "لا توجد أسماء سلع جديدة للاستيراد.")

    def _menu(self, pos) -> None:
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        self.view.selectRow(index.row())
        menu = QMenu(self)
        for text, fn in (("تعديل الكمية / جرد", self.adjust), ("كل حركات الصنف", self.show_moves),
                         ("أرشفة / إلغاء الأرشفة", self.toggle_archive), ("حذف الصنف", self.delete)):
            menu.addAction(text, fn)
        menu.exec(self.view.viewport().mapToGlobal(pos))
