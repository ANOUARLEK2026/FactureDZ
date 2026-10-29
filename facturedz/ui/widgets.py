# -*- coding: utf-8 -*-
"""Reusable widgets: Card, stat tile, field grid and the editable invoice-items table (model/view)."""
from __future__ import annotations

from decimal import Decimal
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemDelegate, QAbstractItemView, QButtonGroup, QCompleter, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QStyledItemDelegate,
                               QTableView, QVBoxLayout, QWidget)

from ..amounts import money, parse_decimal, plain_number, qty_text
from ..inventory import Catalog
from ..config import DESCRIPTION_SOFT_LIMIT, MAX_ITEMS, TEMPLATE_ITEM_ROWS
from ..service import compute_items
from ..textnorm import normalize_ar, to_ascii_digits
from . import theme


def button(text: str, variant: str = "", on_click: Optional[Callable[[], None]] = None) -> QPushButton:
    b = QPushButton(text)
    if variant:
        b.setProperty("variant", variant)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click:
        b.clicked.connect(lambda _checked=False: on_click())
    return b


def label(text: str = "", name: str = "") -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    return lab


def set_invalid(widget: QWidget, bad: bool) -> None:
    if widget.property("invalid") != bad:
        widget.setProperty("invalid", bad)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


class Card(QFrame):
    """White panel: title row (+ optional header widgets) and a ``body`` layout."""

    def __init__(self, title: str = "", subtitle: str = ""):
        super().__init__()
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 14)
        outer.setSpacing(8)
        self.head = QHBoxLayout()
        if title:
            self.title_label = label(title, "CardTitle")
            self.head.addWidget(self.title_label)
            if subtitle:
                self.head.addWidget(label(subtitle, "CardSub"))
            self.head.addStretch(1)
            outer.addLayout(self.head)
            rule = QFrame()
            rule.setObjectName("Rule")
            outer.addWidget(rule)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        outer.addLayout(self.body)


class StatTile(QFrame):
    def __init__(self, caption: str, hint: str = "", color: str = ""):
        super().__init__()
        self.setObjectName("Stat")
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 10, 14, 10)
        box.setSpacing(2)
        box.addWidget(label(caption, "Muted"))
        self.value = label("—", "StatVal")
        if color:
            self.value.setStyleSheet(f"color: {color};")
        box.addWidget(self.value)
        self.hint = label(hint, "Field")
        box.addWidget(self.hint)

    def set(self, value: str, hint: Optional[str] = None) -> None:
        self.value.setText(value)
        if hint is not None:
            self.hint.setText(hint)


class FieldGrid(QWidget):
    """Compact form: a small caption above each field, ``columns`` per row. Grid column 0 is the right edge."""

    def __init__(self, fields: Sequence[Tuple[str, str]], columns: int = 3, wide: Sequence[str] = ()):
        super().__init__()
        self.edits: Dict[str, QLineEdit] = {}
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        for col in range(columns):
            grid.setColumnStretch(col, 1)
        row = col = 0
        for key, caption in fields:
            span = columns if key in wide else 1
            if col and col + span > columns:
                row, col = row + 1, 0
            cell = QVBoxLayout()
            cell.setSpacing(2)
            edit = QLineEdit()
            self.edits[key] = edit
            cell.addWidget(label(caption, "Field"))
            cell.addWidget(edit)
            grid.addLayout(cell, row, col, 1, span)
            col += span
            if col >= columns:
                row, col = row + 1, 0

    def values(self) -> Dict[str, str]:
        return {k: e.text().strip() for k, e in self.edits.items()}

    def set_values(self, record: Dict[str, object]) -> None:
        for key, edit in self.edits.items():
            edit.setText(str(record.get(key, "") or ""))

    def clear(self) -> None:
        for edit in self.edits.values():
            edit.clear()


# --------------------------------------------------------------------------- #
# items table                                                                 #
# --------------------------------------------------------------------------- #
COL_NO, COL_DESC, COL_QTY, COL_PRICE, COL_TOTAL, COL_STOCK, COL_DEL = range(7)
HEADERS = ["N°", "التعيين / السلعة", "الكمية (كلغ)", "السعر (دج/كلغ)", "المجموع (دج)", "المخزون", ""]
PURCHASE_HEADERS = ["N°", "الصنف", "الكمية (كلغ)", "سعر الشراء (دج/كلغ)", "المجموع (دج)", "المخزون", ""]
_EDITABLE = (COL_DESC, COL_QTY, COL_PRICE)
_LEVEL_COLOR = {"ok": "ok", "warn": "warn", "bad": "danger", "muted": "muted", "new": "accent"}


class ItemsModel(QAbstractTableModel):
    """Editable invoice lines. ``mode`` is 'sale' or 'purchase'. Each line is linked to a stock product (by the
    catalogue) as the name is typed; the last column shows what that means for the stock, live."""
    changed = Signal()

    def __init__(self, mode: str = "sale"):
        super().__init__()
        self.mode = mode
        self.headers = PURCHASE_HEADERS if mode == "purchase" else HEADERS
        self.catalog = Catalog()
        self.credit: Dict[int, Decimal] = {}          # kg an invoice being edited already took out of stock
        self.rows: List[Dict[str, object]] = []
        self.reset()

    # -- structure ---------------------------------------------------------------------------
    @staticmethod
    def _blank() -> Dict[str, object]:
        return {"description": "", "quantity": "", "price": "", "product_id": None}

    def _relink(self, row: Dict[str, object]) -> None:
        """Keep a product link that still exists; otherwise match the typed name with the catalogue."""
        if row.get("product_id") is not None and self.catalog.get(row["product_id"]) is not None:
            return
        product = self.catalog.find(str(row.get("description", "")))
        row["product_id"] = product["id"] if product else None

    def reset(self, items: Optional[List[Dict[str, object]]] = None) -> None:
        self.beginResetModel()
        known = ("description", "quantity", "price", "product_id")
        self.rows = [dict(self._blank(), **{k: v for k, v in i.items() if k in known}) for i in (items or [])][:MAX_ITEMS]
        while len(self.rows) < TEMPLATE_ITEM_ROWS:
            self.rows.append(self._blank())
        for row in self.rows:
            self._relink(row)
        self.endResetModel()
        self.changed.emit()

    def set_catalog(self, catalog: Catalog, credit: Optional[Dict[int, Decimal]] = None) -> None:
        self.catalog = catalog
        self.credit = dict(credit or {})
        for row in self.rows:
            self._relink(row)
        self._stock_changed()
        self.changed.emit()

    def add_row(self) -> bool:
        if len(self.rows) >= MAX_ITEMS:
            return False
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows))
        self.rows.append(self._blank())
        self.endInsertRows()
        self.changed.emit()
        return True

    def remove_row(self, row: int) -> None:
        if not 0 <= row < len(self.rows):
            return
        if len(self.rows) <= 1:
            self.rows[0] = self._blank()
            self.dataChanged.emit(self.index(0, 0), self.index(0, COL_DEL))
        else:
            self.beginRemoveRows(QModelIndex(), row, row)
            del self.rows[row]
            self.endRemoveRows()
            self.dataChanged.emit(self.index(0, COL_NO), self.index(len(self.rows) - 1, COL_NO))
        self._stock_changed()
        self.changed.emit()

    def raw(self) -> List[Dict[str, object]]:
        return [dict(r) for r in self.rows]

    def _stock_changed(self) -> None:
        if self.rows:
            self.dataChanged.emit(self.index(0, COL_STOCK), self.index(len(self.rows) - 1, COL_STOCK))

    # -- stock figures -------------------------------------------------------------------------
    def _requested(self, product_id: int) -> Decimal:
        total = Decimal(0)
        for row in self.rows:
            if row.get("product_id") == product_id:
                try:
                    total += max(parse_decimal(row["quantity"]), Decimal(0))
                except ValueError:
                    pass
        return total

    def stock_info(self, r: int) -> Tuple[str, str]:
        """(text, level) for line ``r``; level is ok | warn | bad | muted | new | '' (nothing to say)."""
        row = self.rows[r]
        if not str(row["description"]).strip():
            return "", ""
        product = self.catalog.get(row.get("product_id"))
        if product is None:
            return ("صنف جديد في المخزون", "new") if self.mode == "purchase" else ("غير مرتبط بالمخزون", "muted")
        if self.mode == "purchase":
            return f"الحالي {qty_text(product['on_hand'])}", "muted"
        available = product["on_hand"] + self.credit.get(product["id"], Decimal(0))
        left = available - self._requested(product["id"])
        if left < 0:
            return f"ناقص {qty_text(-left)}", "bad"
        if product["min_stock"] > 0 and left <= product["min_stock"]:
            return f"يتبقى {qty_text(left)}", "warn"
        return f"متاح {qty_text(available)}", "ok"

    def unlinked_names(self) -> List[str]:
        """Typed lines (with a quantity or a price) that match no stock product."""
        return [str(r["description"]).strip() for r in self.rows
                if str(r["description"]).strip() and (str(r["quantity"]).strip() or str(r["price"]).strip())
                and r.get("product_id") is None]

    # -- Qt model API --------------------------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return self.headers[section]
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignCenter)
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return base | Qt.ItemFlag.ItemIsEditable if index.column() in _EDITABLE else base

    def _row_total(self, row: Dict[str, object]) -> str:
        done = compute_items([row], strict=False)
        return money(done[0]["line_total"]) if done and (row["quantity"] or row["price"]) else ""

    def _bad(self, row: int, col: int) -> bool:
        r = self.rows[row]
        if col == COL_DESC:
            return len(str(r["description"])) > DESCRIPTION_SOFT_LIMIT
        try:
            return parse_decimal(r["quantity" if col == COL_QTY else "price"]) < 0
        except ValueError:
            return True

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        row = self.rows[r]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if c == COL_STOCK:
                return self.stock_info(r)[0]
            return {COL_NO: f"{r + 1:02d}", COL_DESC: row["description"], COL_QTY: row["quantity"],
                    COL_PRICE: row["price"], COL_TOTAL: self._row_total(row), COL_DEL: "✕"}[c]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if c == COL_DESC:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.BackgroundRole:
            if c in _EDITABLE and self._bad(r, c):
                return QBrush(QColor(theme.P["danger_soft"]))
            if c == COL_STOCK and self.stock_info(r)[1] == "bad":
                return QBrush(QColor(theme.P["danger_soft"]))
        if role == Qt.ItemDataRole.ForegroundRole:
            if c == COL_NO or c == COL_DEL:
                return QBrush(QColor(theme.P["muted"]))
            if c == COL_TOTAL:
                return QBrush(QColor(theme.P["accent"]))
            if c == COL_STOCK:
                return QBrush(QColor(theme.P[_LEVEL_COLOR.get(self.stock_info(r)[1], "muted")]))
        if role == Qt.ItemDataRole.FontRole and (c == COL_TOTAL or (c == COL_STOCK and self.stock_info(r)[1] == "bad")):
            from PySide6.QtGui import QFont
            f = QFont()
            f.setBold(True)
            return f
        if role == Qt.ItemDataRole.ToolTipRole and c == COL_STOCK:
            return self.stock_info(r)[0] or None
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or index.column() not in _EDITABLE:
            return False
        key = {COL_DESC: "description", COL_QTY: "quantity", COL_PRICE: "price"}[index.column()]
        text = str(value or "")
        if key != "description":
            text = to_ascii_digits(text).strip()          # Arabic-Indic digits -> the form we compute with
        row = self.rows[index.row()]
        row[key] = text
        if key == "description":
            product = self.catalog.find(text)
            row["product_id"] = product["id"] if product else None
            if product and not str(row["price"]).strip():           # offer the usual price, never override a typed one
                usual = product["sell_price"] if self.mode == "sale" else product["last_cost"]
                if usual and usual > 0:
                    row["price"] = plain_number(usual)
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), COL_DEL))
        self._stock_changed()
        self.changed.emit()
        return True


class _CellDelegate(QStyledItemDelegate):
    enter_pressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.names: List[str] = []

    def createEditor(self, parent, option, index):
        edit = QLineEdit(parent)
        edit.setAlignment(Qt.AlignmentFlag.AlignRight if index.column() == COL_DESC else Qt.AlignmentFlag.AlignCenter)
        if index.column() != COL_DESC:
            edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        elif self.names:                                       # product names pop up while typing
            completer = QCompleter(self.names, edit)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            edit.setCompleter(completer)
        return edit

    def eventFilter(self, editor, event):
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commitData.emit(editor)
            self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)
            self.enter_pressed.emit()
            return True
        return super().eventFilter(editor, event)


class ItemsTable(QTableView):
    """Editable line items. Enter moves description -> quantity -> price -> next row."""

    def __init__(self, mode: str = "sale"):
        super().__init__()
        self.setObjectName("ItemsTable")
        self.items_model = ItemsModel(mode)
        self.setModel(self.items_model)
        self._delegate = _CellDelegate(self)
        self.setItemDelegate(self._delegate)
        self._delegate.enter_pressed.connect(lambda: QTimer.singleShot(0, self._next_cell))
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(38)
        self.setShowGrid(False)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_DESC, QHeaderView.ResizeMode.Stretch)
        for col, width in ((COL_NO, 44), (COL_QTY, 100), (COL_PRICE, 140), (COL_TOTAL, 115), (COL_STOCK, 150),
                           (COL_DEL, 36)):
            self.setColumnWidth(col, width)
        self.clicked.connect(self._on_click)
        self.items_model.changed.connect(self._fit_height)
        self._fit_height()

    def set_catalog(self, catalog: Catalog, credit: Optional[Dict[int, Decimal]] = None) -> None:
        self._delegate.names = catalog.names()
        self.items_model.set_catalog(catalog, credit)

    def _fit_height(self) -> None:
        rows = self.items_model.rowCount()
        self.setFixedHeight(self.horizontalHeader().height() + rows * self.verticalHeader().defaultSectionSize() + 4)

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setHeight(self.height())
        return hint

    def _on_click(self, index: QModelIndex) -> None:
        if index.column() == COL_DEL:
            self.items_model.remove_row(index.row())

    def focus_cell(self, row: int, column: int) -> None:
        if 0 <= row < self.items_model.rowCount():
            index = self.items_model.index(row, column)
            self.setCurrentIndex(index)
            self.edit(index)

    def add_row(self) -> bool:
        ok = self.items_model.add_row()
        if ok:
            self.focus_cell(self.items_model.rowCount() - 1, COL_DESC)
        return ok

    def _next_cell(self) -> None:
        cur = self.currentIndex()
        if not cur.isValid():
            return
        row, col = cur.row(), cur.column()
        if col in (COL_DESC, COL_QTY):
            self.focus_cell(row, col + 1)
        elif row + 1 < self.items_model.rowCount() or self.items_model.add_row():
            self.focus_cell(row + 1, COL_DESC)


class Segmented(QWidget):
    """A row of mutually exclusive buttons (filters, modes). ``changed`` carries the key of the picked option."""
    changed = Signal(str)

    def __init__(self, options: Sequence[Tuple[str, str]], value: str = ""):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: Dict[str, QPushButton] = {}
        self._value = ""
        for key, text in options:
            b = QPushButton(text)
            b.setCheckable(True)
            b.setProperty("seg", True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=key: self.set_value(k))
            self.group.addButton(b)
            self.buttons[key] = b
            row.addWidget(b)
        self.set_value(value or options[0][0], emit=False)

    def value(self) -> str:
        return self._value

    def set_value(self, key: str, emit: bool = True) -> None:
        if key not in self.buttons:
            return
        self.buttons[key].setChecked(True)
        if key != self._value:
            self._value = key
            if emit:
                self.changed.emit(key)


# --------------------------------------------------------------------------- #
# read-only list views (history / customers)                                  #
# --------------------------------------------------------------------------- #
SORT_ROLE = Qt.ItemDataRole.UserRole
HAYSTACK_ROLE = Qt.ItemDataRole.UserRole + 1
ID_ROLE = Qt.ItemDataRole.UserRole + 2


class SearchProxy(QSortFilterProxyModel):
    """Sorts by SORT_ROLE and filters rows with the app's Arabic-aware normalisation."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self._needle = ""

    def set_needle(self, text: str) -> None:
        self._needle = normalize_ar(text)
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent) -> bool:
        if not self._needle:
            return True
        item = self.sourceModel().item(row, 0)
        return bool(item) and self._needle in (item.data(HAYSTACK_ROLE) or "")


def make_list_view(headers: Sequence[str], widths: Sequence[int]):
    """(view, source model, proxy) - RTL header order comes from the application layout direction."""
    model = QStandardItemModel(0, len(headers))
    model.setHorizontalHeaderLabels(list(headers))
    proxy = SearchProxy()
    proxy.setSourceModel(model)
    view = QTableView()
    view.setModel(proxy)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setSortingEnabled(True)
    view.setShowGrid(False)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(38)
    view.horizontalHeader().setStretchLastSection(True)
    view.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    for i, w in enumerate(widths):
        view.setColumnWidth(i, w)
    return view, model, proxy


def list_item(text: str, sort=None, align_right: bool = True, color: str = "") -> QStandardItem:
    item = QStandardItem(text)
    item.setEditable(False)
    item.setData(text if sort is None else sort, SORT_ROLE)
    flags = Qt.AlignmentFlag.AlignRight if align_right else Qt.AlignmentFlag.AlignCenter
    item.setTextAlignment(flags | Qt.AlignmentFlag.AlignVCenter)
    if color:
        item.setForeground(QBrush(QColor(color)))
    return item
