# -*- coding: utf-8 -*-
"""Stock movements ledger: every entry and exit, filterable by type, period, product and text."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from ...amounts import money, qty_text
from ...config import STOCK_UNIT
from ...dates import PERIODS, iso_to_display, period_range
from .. import theme
from ..widgets import StatTile, button, label, list_item, make_list_view
from .stock import movement_label

log = logging.getLogger("facturedz.ui.movements")
HEADERS = ["التاريخ", "الصنف", "النوع", f"الكمية ({STOCK_UNIT})", "سعر الوحدة (دج)", "المرجع", "ملاحظة"]
WIDTHS = (100, 230, 170, 120, 120, 150, 200)


class MovementsPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.setObjectName("Page")
        self.app = app
        self.product_id: Optional[int] = None
        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(10)

        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.t_in = StatTile("الداخل", f"مجموع الكميات الداخلة ({STOCK_UNIT})", theme.P["ok"])
        self.t_out = StatTile("الخارج", f"مجموع الكميات الخارجة ({STOCK_UNIT})", theme.P["danger"])
        self.t_net = StatTile("الصافي", "الداخل − الخارج", theme.P["accent"])
        self.t_count = StatTile("عدد الحركات", "للعرض الحالي", theme.P["muted"])
        for tile in (self.t_in, self.t_out, self.t_net, self.t_count):
            tiles.addWidget(tile, 1)
        col.addLayout(tiles)

        bar = QFrame()
        bar.setObjectName("Toolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 8, 14, 8)
        row.addWidget(label("بحث"))
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(220)
        self.search.setPlaceholderText("الصنف أو رقم الفاتورة أو ملاحظة")
        self.search.textChanged.connect(lambda _t: self.refresh())
        row.addWidget(self.search)
        self.kind = QComboBox()
        self.kind.addItem("كل الأنواع", "")
        from ...config import MOVEMENT_KINDS
        for key, text in MOVEMENT_KINDS:
            self.kind.addItem(text, key)
        self.kind.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self.kind)
        self.period = QComboBox()
        for key, text in PERIODS:
            self.period.addItem(text, key)
        self.period.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self.period)
        self.chip = button("", "link", self.clear_product)
        self.chip.setObjectName("Chip")
        self.chip.setVisible(False)
        row.addWidget(self.chip)
        row.addStretch(1)
        self.count = label("0 حركة", "Muted")
        row.addWidget(self.count)
        row.addWidget(button("↻  تحديث", "link", self.refresh))
        col.addWidget(bar)

        self.view, self.model, self.proxy = make_list_view(HEADERS, WIDTHS)
        col.addWidget(self.view, 1)
        self.empty = label("لا توجد حركات مطابقة", "Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.empty)
        self.refresh()

    def show_for_product(self, product_id: int, name: str) -> None:
        self.product_id = product_id
        self.chip.setText(f"الصنف: {name}   ✕")
        self.chip.setVisible(True)
        self.refresh()

    def clear_product(self) -> None:
        self.product_id = None
        self.chip.setVisible(False)
        self.refresh()

    def refresh(self) -> None:
        start, end = period_range(str(self.period.currentData() or "all"))
        moves = self.app.repo.stock.list_movements(
            product_id=self.product_id, kind=str(self.kind.currentData() or ""), date_from=start, date_to=end,
            search=self.search.text())
        self.model.setRowCount(0)
        total_in = total_out = Decimal(0)
        for m in moves:
            qty = m["qty"]
            total_in += qty if qty > 0 else 0
            total_out += -qty if qty < 0 else 0
            color = theme.P["ok"] if qty > 0 else theme.P["danger"]
            note = m["note"] or ""
            self.model.appendRow([
                list_item(iso_to_display(m["moved_on"]), m["moved_on"] + f"{m['id']:09d}", False),
                list_item(m["product_name"]), list_item(movement_label(m)),
                list_item(("+" if qty > 0 else "−") + qty_text(abs(qty)), float(qty), False, color),
                list_item(money(m["unit_cost"]) if m["unit_cost"] else "—", float(m["unit_cost"]), False),
                list_item(m["ref_no"] or "—", m["ref_no"], False), list_item(note)])
        self.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.t_in.set(qty_text(total_in))
        self.t_out.set(qty_text(total_out))
        self.t_net.set(("+" if total_in >= total_out else "−") + qty_text(abs(total_in - total_out)))
        self.t_count.set(str(len(moves)))
        self.count.setText(f"{len(moves)} حركة")
        self.empty.setVisible(not moves)
