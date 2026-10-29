# -*- coding: utf-8 -*-
"""Saved invoices page."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLineEdit, QMenu, QVBoxLayout, QWidget

from ...amounts import money
from ...service import ValidationError
from ...template import TemplateError
from ...textnorm import normalize_ar
from .. import dialogs, theme
from ..widgets import (HAYSTACK_ROLE, ID_ROLE, StatTile, button, label, list_item, make_list_view)

log = logging.getLogger("facturedz.ui.history")
HEADERS = ["رقم الفاتورة", "التسلسلي", "التاريخ", "المشتري", "TTC (دج)", "الملف"]


class HistoryPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.setObjectName("Page")
        self.app = app
        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(10)

        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.s_count = StatTile("الفواتير", "إجمالي السجلات", theme.P["accent"])
        self.s_total = StatTile("مجموع TTC", "للفواتير المعروضة", theme.P["gold"])
        self.s_month = StatTile("هذا الشهر", dt.date.today().strftime("%m/%Y"), theme.P["ok"])
        self.s_last = StatTile("آخر فاتورة", "أحدث رقم مسجّل", theme.P["muted"])
        self.s_last.value.setStyleSheet(f"color: {theme.P['muted']}; font-size: 12pt; font-weight: bold;")  # long number
        for tile in (self.s_count, self.s_total, self.s_month, self.s_last):
            stats.addWidget(tile, 1)
        col.addLayout(stats)

        bar = QFrame()
        bar.setObjectName("Toolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 8, 14, 8)
        row.addWidget(label("بحث"))
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(320)
        self.search.setPlaceholderText("رقم الفاتورة، الزبون، التاريخ")
        self.search.textChanged.connect(self._filter)
        row.addWidget(self.search)
        row.addStretch(1)
        self.count = label("0 فاتورة", "Muted")
        row.addWidget(self.count)
        row.addWidget(button("↻  تحديث", "link", self.refresh))
        col.addWidget(bar)

        self.view, self.model, self.proxy = make_list_view(HEADERS, (170, 150, 100, 220, 120, 100))
        self.view.doubleClicked.connect(lambda _i: self.open_file())
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        col.addWidget(self.view, 1)
        self.empty = label("لا توجد فواتير لعرضها بعد", "Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.empty)

        actions = QFrame()
        actions.setObjectName("Toolbar")
        ar = QHBoxLayout(actions)
        ar.setContentsMargins(14, 8, 14, 8)
        ar.addWidget(button("فتح الفاتورة", "primary", self.open_file))
        ar.addWidget(button("فتح المجلد", "ghost", self.open_folder))
        ar.addWidget(button("إعادة إنشاء PDF", "ghost", self.reexport))
        ar.addWidget(button("فاتورة مماثلة", "ghost", self.duplicate))
        ar.addWidget(button("تعديل الفاتورة", "ghost", self.edit_invoice))
        ar.addWidget(button("حذف الفاتورة", "danger", self.delete_invoice))
        ar.addStretch(1)
        self.total_label = label("", "Muted")
        ar.addWidget(self.total_label)
        col.addWidget(actions)
        self.refresh()

    # ------------------------------------------------------------------ #
    def _filter(self, text: str) -> None:
        self.proxy.set_needle(text)
        self._update_footer()

    def _update_footer(self) -> None:
        shown = self.proxy.rowCount()
        total = sum(self.proxy.index(r, 4).data(Qt.ItemDataRole.UserRole) or 0 for r in range(shown))
        self.count.setText(f"{shown} فاتورة")
        self.s_total.set(f"{money(total)} دج")
        self.total_label.setText(f"مجموع TTC المعروض: {money(total)} دج" if shown else "")
        self.empty.setVisible(shown == 0)

    def refresh(self) -> None:
        rows = self.app.repo.list_invoices()
        self.model.setRowCount(0)
        today, month = dt.date.today(), 0
        for r in rows:
            path = Path(r["file_path"]) if r["file_path"] else None
            if path and path.exists():
                status, color = ("PDF ✓", theme.P["ok"]) if path.suffix.lower() == ".pdf" else ("Excel فقط", theme.P["warn"])
            else:
                status, color = "الملف مفقود", theme.P["danger"]
            try:
                d = dt.datetime.strptime(r["invoice_date"], "%d.%m.%Y").date()
                month += (d.year, d.month) == (today.year, today.month)
            except (ValueError, TypeError):
                d = dt.date.min
            number = lambda v: int(v) if str(v).isdigit() else 0  # noqa: E731
            items = [list_item(r["invoice_no"], number(r["invoice_no"])), list_item(r["serial_no"], number(r["serial_no"])),
                     list_item(r["invoice_date"], d.toordinal()), list_item(r["customer_name"]),
                     list_item(money(r["total"]), float(r["total"] or 0)), list_item(status, color=color)]
            items[0].setData(normalize_ar(f"{r['invoice_no']} {r['serial_no']} {r['customer_name']} {r['invoice_date']}"),
                             HAYSTACK_ROLE)
            items[0].setData(r["id"], ID_ROLE)
            self.model.appendRow(items)
        self.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.s_count.set(str(len(rows)))
        self.s_month.set(str(month))
        self.s_last.set(f"#{rows[0]['invoice_no']}" if rows else "—")
        self._update_footer()

    # ------------------------------------------------------------------ #
    def _selected_id(self, warn: bool = True) -> Optional[int]:
        sel = self.view.selectionModel().selectedRows()
        if not sel:
            if warn:
                dialogs.info(self.app, "حدد فاتورة من القائمة أولًا.")
            return None
        return int(sel[0].siblingAtColumn(0).data(ID_ROLE))

    def _record(self):
        invoice_id = self._selected_id()
        return self.app.repo.get_invoice(invoice_id) if invoice_id is not None else None

    def _menu(self, pos) -> None:
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        self.view.selectRow(index.row())
        menu = QMenu(self)
        for text, fn in (("فتح الفاتورة", self.open_file), ("فتح المجلد", self.open_folder),
                         ("إعادة إنشاء PDF", self.reexport), ("فاتورة مماثلة", self.duplicate),
                         ("تعديل الفاتورة", self.edit_invoice), ("حذف الفاتورة", self.delete_invoice)):
            menu.addAction(text, fn)
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def open_file(self) -> None:
        record = self._record()
        if not record:
            return
        path = Path(record["file_path"]) if record["file_path"] else None
        if path and path.exists():
            self.app.open_path(path)
        elif dialogs.confirm(self.app, f"لم أعثر على الملف:\n{record['file_path']}\n\n"
                                       "هل تريد إعادة إنشائه من بيانات الفاتورة المحفوظة؟"):
            self.reexport()

    def open_folder(self) -> None:
        record = self._record()
        if not record or not record["file_path"]:
            return
        folder = Path(record["file_path"]).parent
        if folder.exists():
            self.app.open_path(folder)
        else:
            dialogs.error(self.app, f"المجلد غير موجود:\n{folder}")

    def duplicate(self) -> None:
        record = self._record()
        if record:
            self.app.invoice_page.load_from_invoice(record)
            self.app.show_page("invoice")
            self.app.status("تم نسخ بيانات الفاتورة إلى فاتورة جديدة.")

    def edit_invoice(self) -> None:
        record = self._record()
        if not record:
            return
        page = self.app.invoice_page
        if page.has_content() and not dialogs.confirm(
                self.app, "في صفحة «فاتورة جديدة» بيانات غير محفوظة وسيتم استبدالها بالفاتورة المراد تعديلها.\nهل تريد المتابعة؟",
                yes="متابعة", no="إلغاء"):
            return
        page.load_for_edit(record)
        self.app.show_page("invoice")
        self.app.status(f"تعديل الفاتورة {record['invoice_no']}")

    def delete_invoice(self) -> None:
        record = self._record()
        if not record:
            return
        text = (f"هل تريد حذف هذه الفاتورة؟\n\nرقم الفاتورة: {record['invoice_no']}\n"
                f"المشتري: {record['customer']['name']}\nالتاريخ: {record['invoice_date']}\n"
                f"المجموع TTC: {money(record['total'])} دج\n\n"
                "• يبقى رقمها وتسلسليها محجوزين ولن يُعطيا لفاتورة أخرى أبدًا.\n"
                "• تُحفظ نسخة احتياطية من قاعدة البيانات قبل الحذف.")
        choice = dialogs.choose(self.app, text, (("حذف الفاتورة مع ملفاتها", "all"), ("حذف السجل فقط وإبقاء الملفات", "record"),
                                                 ("إلغاء", None)), "warn", "تأكيد الحذف", default=2)
        if choice is None:
            return
        try:
            removed = self.app.service.delete(record["id"], remove_files=(choice == "all"))
        except ValidationError as exc:
            dialogs.error(self.app, str(exc))
            self.refresh()
            return
        except Exception as exc:  # noqa: BLE001
            log.error("delete failed", exc_info=True)
            dialogs.error(self.app, f"تعذر الحذف:\n{exc}")
            return
        if self.app.invoice_page.editing_id == record["id"]:
            self.app.invoice_page.new_invoice()
        self.refresh()
        self.app.refresh_stock()
        self.app.status(f"حُذفت الفاتورة {record['invoice_no']}" + (f" و{len(removed)} ملف" if removed else ""))

    def reexport(self) -> None:
        invoice_id = self._selected_id()
        if invoice_id is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = self.app.service.reexport(invoice_id)
        except (ValidationError, TemplateError) as exc:
            QApplication.restoreOverrideCursor()
            dialogs.error(self.app, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            log.error("re-export failed", exc_info=True)
            dialogs.error(self.app, f"تعذر إعادة الإنشاء:\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        self.refresh()
        if result.pdf_ok:
            if dialogs.confirm(self.app, f"تم إنشاء الملف:\n{result.pdf_path}", yes="فتح", no="إغلاق"):
                self.app.open_path(result.pdf_path)
        else:
            dialogs.warn(self.app, result.warnings[0])
