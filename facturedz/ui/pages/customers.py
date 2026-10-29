# -*- coding: utf-8 -*-
"""Customer files page."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from ...config import CUSTOMER_DIGITS, CUSTOMER_FIELDS
from ...textnorm import normalize_ar
from .. import dialogs, theme
from ..widgets import Card, FieldGrid, HAYSTACK_ROLE, ID_ROLE, button, label, list_item, make_list_view

_ORDER = ("name", "commercial_title", "phone", "rc", "nif", "address", "nis", "bank_account", "article_no", "activity")


class CustomersPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.setObjectName("Page")
        self.app = app
        self.selected_id: Optional[int] = None
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(12)

        # right: search + list
        right = QVBoxLayout()
        bar = QFrame()
        bar.setObjectName("Toolbar")
        br = QHBoxLayout(bar)
        br.setContentsMargins(14, 8, 14, 8)
        br.addWidget(label("بحث"))
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(280)
        self.search.textChanged.connect(self._filter)
        br.addWidget(self.search)
        br.addStretch(1)
        self.count = label("0 زبون", "Muted")
        br.addWidget(self.count)
        br.addWidget(button("＋  زبون جديد", "ghost", self.clear))
        right.addWidget(bar)
        self.view, self.model, self.proxy = make_list_view(
            ["الرقم", "اسم المشتري", "العنوان التجاري", "السجل التجاري", "الهاتف"], (80, 230, 240, 160, 140))
        self.view.selectionModel().selectionChanged.connect(self._on_select)
        self.view.doubleClicked.connect(lambda _i: self.use_in_invoice())
        right.addWidget(self.view, 1)
        self.empty = label("لا يوجد زبون مطابق لبحثك", "Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.empty)
        row.addLayout(right, 1)

        # left: editor
        card = Card("ملف مشتري", "اختر زبونًا أو أنشئ ملفًا جديدًا")
        card.setFixedWidth(470)
        self.identity = label("زبون جديد")
        self.identity.setStyleSheet(f"font-size: 11pt; font-weight: bold; color: {theme.P['brand']};")
        self.hint = label("لم يُحفظ بعد", "Muted")
        card.body.addWidget(self.identity)
        card.body.addWidget(self.hint)
        labels = dict(CUSTOMER_FIELDS)
        self.fields = FieldGrid([(k, labels[k]) for k in _ORDER if k in labels], 2)
        self.fields.edits["name"].textChanged.connect(self._paint_identity)
        card.body.addWidget(self.fields)
        card.body.addWidget(button("حفظ / تحديث الملف", "primary", self.save))
        pair = QHBoxLayout()
        pair.addWidget(button("استخدام في فاتورة", "", self.use_in_invoice), 1)
        pair.addWidget(button("جديد", "", self.clear), 1)
        card.body.addLayout(pair)
        card.body.addWidget(button("حذف الملف المحدد", "danger", self.delete))
        card.body.addStretch(1)
        row.addWidget(card)
        self.refresh()

    def _filter(self, text: str) -> None:
        self.proxy.set_needle(text)
        self._footer()

    def _footer(self) -> None:
        n = self.proxy.rowCount()
        self.count.setText(f"{n} زبون")
        self.empty.setVisible(n == 0)

    def refresh(self) -> None:
        records = self.app.repo.list_customers()
        self.model.setRowCount(0)
        for r in records:
            number = f"{r['id']:0{CUSTOMER_DIGITS}d}"          # the customer number inside every invoice number
            items = [list_item(number, r["id"]), list_item(r["name"]), list_item(r.get("commercial_title", "")),
                     list_item(r.get("rc", "")), list_item(r.get("phone", ""))]
            items[0].setData(normalize_ar(number + " " + " ".join(
                str(r.get(k, "")) for k in ("name", "commercial_title", "rc", "phone"))), HAYSTACK_ROLE)
            items[0].setData(r["id"], ID_ROLE)
            self.model.appendRow(items)
        self.view.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self._footer()

    def _on_select(self, *_args) -> None:
        sel = self.view.selectionModel().selectedRows()
        if not sel:
            return
        record = self.app.repo.get_customer(int(sel[0].siblingAtColumn(0).data(ID_ROLE)))
        if record:
            self.selected_id = record["id"]
            self.fields.set_values(record)
            self._paint_identity()

    def _paint_identity(self, *_args) -> None:
        name = self.fields.edits["name"].text().strip()
        self.identity.setText(name or "زبون جديد")
        if self.selected_id is not None:
            self.hint.setText(f"رقم المشتري {self.selected_id:0{CUSTOMER_DIGITS}d} — ملف محفوظ، يمكنك تعديله ثم الحفظ")
        else:
            self.hint.setText("لم يُحفظ بعد" if name else "املأ الحقول لإنشاء ملف جديد")

    def clear(self) -> None:
        self.selected_id = None
        self.view.clearSelection()
        self.fields.clear()
        self.search.clear()
        self._paint_identity()

    def save(self) -> None:
        try:
            self.selected_id = self.app.repo.save_customer(self.fields.values(), self.selected_id)
        except ValueError as exc:
            dialogs.error(self.app, str(exc))
            return
        self.app.refresh_customers()
        self._paint_identity()
        self.app.status(f"تم حفظ ملف «{self.fields.edits['name'].text().strip()}».")

    def delete(self) -> None:
        if self.selected_id is None:
            dialogs.info(self.app, "حدد ملف المشتري من القائمة أولًا.")
            return
        name = self.fields.edits["name"].text()
        if dialogs.confirm(self.app, f"هل تريد حذف ملف المشتري «{name}»؟\nلن تُحذف الفواتير السابقة.",
                           yes="حذف", no="إلغاء", danger=True):
            self.app.repo.delete_customer(self.selected_id)
            self.clear()
            self.app.refresh_customers()

    def use_in_invoice(self) -> None:
        if self.selected_id is None:
            dialogs.info(self.app, "حدد ملف المشتري من القائمة أولًا.")
            return
        record = self.app.repo.get_customer(self.selected_id)
        if record:
            self.app.invoice_page.use_customer(record)
            self.app.show_page("invoice")
