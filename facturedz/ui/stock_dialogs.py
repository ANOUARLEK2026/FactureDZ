# -*- coding: utf-8 -*-
"""Dialog used to correct the quantity of a product (stock count, spoilage, returns...)."""
from __future__ import annotations

from decimal import Decimal
from typing import Dict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLineEdit, QVBoxLayout

from ..amounts import parse_decimal, qty_text
from ..config import ADJUST_REASONS, STOCK_UNIT
from ..inventory import to_milli
from . import theme
from .widgets import Segmented, button, label

MODES = [("set", "جرد: الكمية الفعلية"), ("add", "إضافة"), ("remove", "إنقاص")]


def preview(current: Decimal, mode: str, text: str):
    """(delta, after) for the typed quantity, or None when it is not a valid, meaningful entry."""
    try:
        qty = parse_decimal(text)
    except ValueError:
        return None
    if qty < 0 or (not str(text).strip()):
        return None
    if mode != "set" and to_milli(qty) == 0:
        return None
    delta = {"set": qty - current, "add": qty, "remove": -qty}[mode]
    if to_milli(delta) == 0:
        return None
    return delta, current + delta


class AdjustDialog(QDialog):
    def __init__(self, parent, product: Dict[str, object]):
        super().__init__(parent)
        self.product = product
        self.current: Decimal = product["on_hand"]
        self.setWindowTitle("تعديل كمية الصنف")
        self.setModal(True)
        self.setMinimumWidth(480)
        col = QVBoxLayout(self)
        col.setContentsMargins(20, 18, 20, 16)
        col.setSpacing(10)
        name = label(str(product["name"]))
        name.setStyleSheet(f"font-size: 13pt; font-weight: bold; color: {theme.P['brand']};")
        col.addWidget(name)
        col.addWidget(label(f"الرصيد الحالي: {qty_text(self.current)} {STOCK_UNIT}", "Muted"))
        self.mode = Segmented(MODES, "set")
        self.mode.changed.connect(lambda _k: self._update())
        col.addWidget(self.mode)
        col.addWidget(label(f"الكمية ({STOCK_UNIT})", "Field"))
        self.qty = QLineEdit()
        self.qty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qty.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.qty.textChanged.connect(lambda _t: self._update())
        col.addWidget(self.qty)
        col.addWidget(label("السبب", "Field"))
        self.reason = QComboBox()
        for key, text in ADJUST_REASONS:
            self.reason.addItem(text, key)
        col.addWidget(self.reason)
        col.addWidget(label("ملاحظة (اختياري)", "Field"))
        self.note = QLineEdit()
        col.addWidget(self.note)
        self.summary = label("", "Words")
        self.summary.setWordWrap(True)
        col.addWidget(self.summary)
        row = QHBoxLayout()
        self.ok = button("حفظ التعديل", "primary", self.accept)
        row.addWidget(self.ok)
        row.addWidget(button("إلغاء", "", self.reject))
        row.addStretch(1)
        col.addLayout(row)
        self.qty.setFocus()
        self._update()

    def _update(self) -> None:
        result = preview(self.current, self.mode.value(), self.qty.text())
        self.ok.setEnabled(result is not None)
        if result is None:
            self.summary.setText("أدخل كمية صحيحة تُحدث تغييرًا في الرصيد.")
            self.summary.setStyleSheet(f"color: {theme.P['muted']};")
            return
        delta, after = result
        sign = "+" if delta > 0 else "−"
        text = f"الرصيد بعد التعديل: {qty_text(after)} {STOCK_UNIT}   (التغيير {sign}{qty_text(abs(delta))})"
        bad = after < 0
        self.summary.setText(text + ("\nتنبيه: سيصبح الرصيد سالبًا." if bad else ""))
        self.summary.setStyleSheet(f"color: {theme.P['danger'] if bad else theme.P['accent']}; font-weight: bold;")

    def values(self) -> Dict[str, str]:
        return {"mode": self.mode.value(), "qty": self.qty.text(), "reason": str(self.reason.currentData()),
                "note": self.note.text().strip()}
