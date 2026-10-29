# -*- coding: utf-8 -*-
"""New-invoice page."""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QCompleter, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget)

from ...inventory import Catalog
from ...amounts import amount_in_words, money, plain_number
from ...config import CUSTOMER_FIELDS, MAX_ITEMS, PAYMENT_METHODS
from ...service import (ValidationError, build_invoice, compute_items, compute_totals, date_to_date, normalize_date,
                        parse_rate)
from ...template import TemplateError
from .. import dialogs
from ..theme import P
from ..widgets import COL_DESC, COL_PRICE, COL_QTY, Card, FieldGrid, ItemsTable, button, label, set_invalid

log = logging.getLogger("facturedz.ui.invoice")
_ORDER = ("name", "commercial_title", "phone", "address", "rc", "nif", "nis", "bank_account", "article_no", "activity")


def _numeric(edit: QLineEdit, width: int = 0) -> QLineEdit:
    edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
    edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    if width:
        edit.setMaximumWidth(width)
    return edit


class InvoicePage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.setObjectName("Page")
        self.app = app
        self._customers: Dict[str, Dict[str, Any]] = {}
        self.editing_id: Optional[int] = None      # set while an already-issued invoice is being edited
        self._stock_off = False                    # True while an invoice from before stock tracking is edited
        self._serial_hint = ""                     # random serial shown on screen; made final when the invoice is saved
        self._build()
        self.date.textChanged.connect(lambda _t: self.refresh_numbers())
        self.fields.edits["name"].textChanged.connect(lambda _t: self.refresh_numbers())
        self.new_invoice()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.scroll = scroll
        body = QWidget()
        body.setObjectName("ScrollBody")
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        col = QVBoxLayout(body)
        col.setContentsMargins(14, 12, 14, 14)
        col.setSpacing(10)

        self.banner = label("", "")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(f"background: {P['warn_soft']}; color: {P['warn']}; font-weight: bold; "
                                  f"border: 1px solid {P['warn']}; border-radius: 6px; padding: 7px 12px;")
        self.banner.setVisible(False)
        col.addWidget(self.banner)

        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(self._meta_card(), 0)                   # first = right edge
        top.addWidget(self._customer_card(), 1)
        col.addLayout(top)

        items = Card("تفاصيل السلع", f"الكمية بالكيلوغرام والسعر بالدينار — حتى {MAX_ITEMS} سطرًا")
        self.table = ItemsTable()
        self.table.items_model.changed.connect(self._recalc)
        items.body.addWidget(self.table)
        row = QHBoxLayout()
        self.add_button = button("＋  إضافة سطر", "ghost", self._add_row)
        row.addWidget(self.add_button)
        self.rows_hint = label("", "Muted")
        row.addWidget(self.rows_hint)
        row.addStretch(1)
        row.addWidget(label("Enter ينتقل إلى الخانة التالية — اختر الصنف من القائمة ليُخصم من المخزون", "Field"))
        items.body.addLayout(row)
        col.addWidget(items)
        col.addStretch(1)
        outer.addWidget(self._bottom_bar())

    def _meta_card(self) -> QWidget:
        card = Card("بيانات الفاتورة")
        card.setFixedWidth(330)
        grid = QGridLayout()
        grid.setVerticalSpacing(8)
        grid.setHorizontalSpacing(10)
        self.invoice_no, self.serial_no = _numeric(QLineEdit()), _numeric(QLineEdit())
        for edit, tip in ((self.invoice_no, "يتكوّن تلقائيًا: الشهر + السنة + رقم المشتري + رقم الفاتورة داخل الشهر"),
                          (self.serial_no, "رقم عشوائي يُولَّد تلقائيًا ولا يتكرر أبدًا")):
            edit.setReadOnly(True)
            edit.setToolTip(tip)
            font = edit.font()
            font.setBold(True)
            edit.setFont(font)
        self.date, self.vat = _numeric(QLineEdit()), _numeric(QLineEdit())
        self.vat.textChanged.connect(self._recalc)
        today = button("اليوم", "small", self._today)
        date_row = QHBoxLayout()
        date_row.addWidget(self.date, 1)
        date_row.addWidget(today)
        self.payment_group = QButtonGroup(self)
        pay_row = QHBoxLayout()
        pay_row.setSpacing(0)
        self.pay_buttons: Dict[str, Any] = {}
        for key, text in PAYMENT_METHODS:
            b = button(text)
            b.setCheckable(True)
            b.setProperty("seg", True)
            self.payment_group.addButton(b)
            self.pay_buttons[key] = b
            pay_row.addWidget(b, 1)
        rows = [("رقم الفاتورة", self.invoice_no), ("الرقم التسلسلي", self.serial_no), ("التاريخ", date_row),
                ("نسبة TVA %", self.vat), ("طريقة الدفع", pay_row)]
        for r, (text, w) in enumerate(rows):
            grid.addWidget(label(text), r, 0)
            if isinstance(w, QWidget):
                grid.addWidget(w, r, 1)
            else:
                grid.addLayout(w, r, 1)
        grid.setColumnStretch(1, 1)
        card.body.addLayout(grid)
        self.renew_button = button("↻  رقم تسلسلي جديد", "link", self._renew_serial)
        card.body.addWidget(self.renew_button, 0, Qt.AlignmentFlag.AlignRight)
        hint = label("التاريخ بالصيغة 10.10.2026 — الأرقام العربية ٠١٢ مقبولة.\n"
                     "رقم الفاتورة = الشهر + السنة + رقم المشتري + رقم الفاتورة في الشهر، ويُنشأ تلقائيًا.", "Muted")
        hint.setWordWrap(True)
        card.body.addWidget(hint)
        return card

    def _customer_card(self) -> QWidget:
        card = Card("بيانات المشتري")
        self.combo = QComboBox()
        self.combo.setMinimumWidth(220)
        self.combo.activated.connect(self._on_customer_selected)
        card.head.addWidget(label("زبون محفوظ", "Field"))
        card.head.addWidget(self.combo)
        card.head.addWidget(button("حفظ الملف", "small", self._save_customer_now))
        labels = dict(CUSTOMER_FIELDS)
        self.fields = FieldGrid([(k, labels[k]) for k in _ORDER if k in labels], 3, wide=("address",))
        card.body.addWidget(self.fields)
        self.completer = QCompleter([], self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.activated.connect(self._on_customer_name)
        self.fields.edits["name"].setCompleter(self.completer)
        self.auto_save = QCheckBox("حفظ بيانات المشتري تلقائيًا بعد إنشاء الفاتورة")
        self.auto_save.setChecked(True)
        card.body.addWidget(self.auto_save)
        return card

    def _bottom_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("BottomBar")
        col = QVBoxLayout(bar)
        col.setContentsMargins(18, 8, 18, 6)
        col.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(22)
        self.lbl_ht, self.lbl_vat, self.lbl_ttc = label("0.00 دج", "TotalVal"), label("0.00 دج", "TotalVal"), label("0.00 دج", "GrandVal")
        self.key_vat = label("TVA", "TotalKey")
        for key, val in ((label("المجموع HT", "TotalKey"), self.lbl_ht), (self.key_vat, self.lbl_vat)):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            cell.addWidget(key)
            cell.addWidget(val)
            row.addLayout(cell)
        grand = QFrame()
        grand.setObjectName("GrandBox")
        gl = QVBoxLayout(grand)
        gl.setContentsMargins(12, 3, 12, 3)
        gl.setSpacing(0)
        gl.addWidget(label("المجموع TTC", "TotalKey"))
        gl.addWidget(self.lbl_ttc)
        row.addWidget(grand)
        row.addStretch(1)
        self.save_button = button("حفظ الفاتورة وإنشاء PDF   ·   Ctrl+S", "primary", self.save)
        row.addWidget(self.save_button)
        row.addWidget(button("فاتورة جديدة", "", self.new_invoice))
        self.cancel_edit_button = button("إلغاء التعديل", "", self.new_invoice)
        self.cancel_edit_button.setVisible(False)
        row.addWidget(self.cancel_edit_button)
        col.addLayout(row)
        self.words = label("", "Words")
        self.words.setWordWrap(True)
        col.addWidget(self.words)
        return bar

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #
    def payment(self) -> str:
        return next((k for k, b in self.pay_buttons.items() if b.isChecked()), "cash")

    def set_payment(self, key: str) -> None:
        self.pay_buttons.get(key, self.pay_buttons["cash"]).setChecked(True)

    def refresh_customers(self) -> None:
        self._customers = {r["name"]: r for r in self.app.repo.list_customers()}
        current = self.combo.currentText()
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(list(self._customers))
        self.combo.setCurrentIndex(self.combo.findText(current))
        self.combo.blockSignals(False)
        self.completer.setModel(self.combo.model())

    def refresh_catalog(self) -> None:
        """Re-read the stock products (names, usual prices, quantities) for the item table. While an issued invoice
        is edited, what it already took out of stock counts as available again."""
        if self._stock_off:                               # old invoice: it never touched stock, so no stock figures
            self.table.set_catalog(Catalog(), {})
            return
        credit = self.app.repo.stock.sale_credit(self.editing_id) if self.editing_id is not None else {}
        self.table.set_catalog(self.app.repo.stock.catalog(), credit)

    def _invoice_day(self):
        try:
            return date_to_date(normalize_date(self.date.text()))
        except ValidationError:
            return None

    def refresh_numbers(self) -> None:
        """Show the numbers the next invoice would get. Nothing is written; ``service.issue`` makes them final."""
        if self.editing_id is not None:               # an issued invoice keeps its numbers for ever
            return
        day = self._invoice_day()
        self.invoice_no.setText(
            self.app.repo.preview_invoice_no(day, self.fields.edits["name"].text()) if day else "—")
        if not self._serial_hint:
            self._serial_hint = self.app.repo.new_serial_candidate()
        self.serial_no.setText(self._serial_hint)

    def _renew_serial(self) -> None:
        self._serial_hint = ""
        self.refresh_numbers()

    def has_content(self) -> bool:
        return bool(self.fields.edits["name"].text().strip()
                    or any(any(str(v).strip() for v in row.values()) for row in self.table.items_model.raw()))

    def _apply_mode(self, record: Optional[Dict[str, Any]] = None) -> None:
        editing = self.editing_id is not None
        self.banner.setVisible(editing)
        self.cancel_edit_button.setVisible(editing)
        self.renew_button.setVisible(not editing)
        self.save_button.setText("حفظ التعديلات   ·   Ctrl+S" if editing else "حفظ الفاتورة وإنشاء PDF   ·   Ctrl+S")
        if editing and record:
            self.banner.setText(f"تعديل الفاتورة رقم {record['invoice_no']} — رقم الفاتورة والرقم التسلسلي ثابتان "
                                "ولا يتغيران. تُنشأ نسخة PDF وExcel جديدة وتحل محل القديمة."
                                + ("\nهذه الفاتورة صدرت قبل تفعيل المخزون، فلا تؤثر عليه ولا يتغير مخزونك بتعديلها." if self._stock_off else ""))

    def load_for_edit(self, record: Dict[str, Any]) -> None:
        """Open an issued invoice in this form to change it (numbers stay locked)."""
        self.new_invoice()
        self.editing_id = int(record["id"])
        self._stock_off = not record.get("stock_tracked", True)
        self.fields.set_values(record["customer"])
        self.combo.setCurrentIndex(self.combo.findText(record["customer"].get("name", "")))
        self.date.setText(record["invoice_date"])
        self.vat.setText(plain_number(record["vat_rate"]))
        self.set_payment(record["payment_method"])
        self.table.items_model.reset([{"description": i["description"], "quantity": plain_number(i["quantity"]),
                                       "price": plain_number(i["unit_price"]), "product_id": i.get("product_id")}
                                      for i in record["items"]])
        self.refresh_catalog()                            # after editing_id is set: its own quantities are credited
        self.invoice_no.setText(record["invoice_no"])
        self.serial_no.setText(record["serial_no"])
        self._apply_mode(record)
        self._recalc()

    def _today(self) -> None:
        self.date.setText(dt.date.today().strftime("%d.%m.%Y"))

    def new_invoice(self) -> None:
        self.editing_id = None
        self._stock_off = False
        self._serial_hint = ""
        self._apply_mode()
        self.fields.clear()
        self.combo.setCurrentIndex(-1)
        self._today()
        self.vat.setText(self.app.repo.get_setting("vat_rate", "19"))
        self.set_payment("cash")
        self.table.items_model.reset()
        self.refresh_customers()
        self.refresh_catalog()
        self.refresh_numbers()
        self._recalc()
        self.fields.edits["name"].setFocus()

    def use_customer(self, record: Dict[str, Any]) -> None:
        self.fields.set_values(record)
        self.combo.setCurrentIndex(self.combo.findText(record.get("name", "")))

    def load_from_invoice(self, data: Dict[str, Any]) -> None:
        """New invoice pre-filled from a stored one (new numbers, today's date)."""
        self.new_invoice()
        self.fields.set_values(data["customer"])
        self.combo.setCurrentIndex(self.combo.findText(data["customer"].get("name", "")))
        self.vat.setText(plain_number(data["vat_rate"]))
        self.set_payment(data["payment_method"])
        self.table.items_model.reset([{"description": i["description"], "quantity": plain_number(i["quantity"]),
                                       "price": plain_number(i["unit_price"]), "product_id": i.get("product_id")}
                                      for i in data["items"]])
        self.refresh_catalog()

    def _on_customer_selected(self, _index: int) -> None:
        record = self._customers.get(self.combo.currentText())
        if record:
            self.use_customer(record)

    def _on_customer_name(self, name: str) -> None:
        record = self._customers.get(name)
        if record:
            self.use_customer(record)

    def _save_customer_now(self) -> None:
        data = self.fields.values()
        try:
            self.app.repo.save_customer(data)
        except ValueError as exc:
            dialogs.error(self.app, str(exc))
            self.fields.edits["name"].setFocus()
            return
        self.app.refresh_customers()
        self.combo.setCurrentIndex(self.combo.findText(data["name"]))
        self.app.status(f"تم حفظ ملف المشتري «{data['name']}».")

    def _add_row(self) -> None:
        if not self.table.add_row():
            dialogs.info(self.app, f"الحد الأقصى {MAX_ITEMS} سطرًا في الفاتورة الواحدة.")

    # ------------------------------------------------------------------ #
    # live calculation
    # ------------------------------------------------------------------ #
    def _recalc(self) -> None:
        if not hasattr(self, "table"):
            return
        try:
            rate = parse_rate(self.vat.text())
            set_invalid(self.vat, False)
        except ValidationError:
            set_invalid(self.vat, True)
            rate = Decimal(self.app.repo.get_setting("vat_rate", "19") or "19")
        totals = compute_totals(compute_items(self.table.items_model.raw(), strict=False), rate)
        self.lbl_ht.setText(f"{money(totals['subtotal'])} دج")
        self.lbl_vat.setText(f"{money(totals['vat_amount'])} دج")
        self.key_vat.setText(f"TVA {plain_number(rate)}%")
        self.lbl_ttc.setText(f"{money(totals['total'])} دج")
        try:
            words = amount_in_words(totals["total"]) if totals["total"] else ""
        except OverflowError:
            words = ""
        self.words.setText(f"المبلغ بالحروف:  {words}" if words else "")
        used = self.table.items_model.rowCount()
        self.rows_hint.setText(f"{used} / {MAX_ITEMS} سطرًا" + ("  — سيُوسَّع القالب تلقائيًا" if used > 8 else ""))
        self.add_button.setEnabled(used < MAX_ITEMS)

    # ------------------------------------------------------------------ #
    # saving
    # ------------------------------------------------------------------ #
    def _form(self) -> Dict[str, Any]:
        # A new invoice sends no invoice number (service.issue builds it); the serial on screen is only a proposal.
        editing = self.editing_id is not None
        return {"invoice_no": self.invoice_no.text() if editing else "",
                "serial_no": self.serial_no.text() if editing else self._serial_hint, "date": self.date.text(),
                "vat_rate": self.vat.text(), "payment": self.payment(), "customer": self.fields.values(),
                "items": self.table.items_model.raw()}

    def _focus(self, field: str) -> None:
        simple = {"invoice_no": self.invoice_no, "serial_no": self.serial_no, "date": self.date, "vat_rate": self.vat}
        if field in simple:
            simple[field].setFocus()
        elif field.startswith("customer."):
            edit = self.fields.edits.get(field.split(".", 1)[1])
            if edit:
                edit.setFocus()
        elif field.startswith("items"):
            parts = field.split(".")
            row = int(parts[1]) - 1 if len(parts) > 1 and parts[1].isdigit() else 0
            col = {"description": COL_DESC, "quantity": COL_QTY, "price": COL_PRICE}.get(parts[2] if len(parts) > 2 else "", COL_DESC)
            self.table.focus_cell(row, col)
            self.scroll.ensureWidgetVisible(self.table)

    def save(self) -> None:
        if not self.save_button.isEnabled():
            return
        self.setFocus()                                   # leaving the cell editor commits what was typed
        editing_id = self.editing_id
        if editing_id is not None:
            stored = self.app.repo.get_invoice(editing_id)
            if not stored:
                dialogs.error(self.app, "هذه الفاتورة لم تعد موجودة (ربما حُذفت).")
                self.new_invoice()
                return
            seller = stored["seller"]                     # an issued invoice keeps the seller block it was issued with
        else:
            seller = self.app.repo.get_seller()
        try:
            data = build_invoice(self._form(), seller)
        except ValidationError as exc:
            dialogs.error(self.app, str(exc), "تحقق من البيانات")
            self._focus(exc.field)
            return
        if editing_id is None and not seller.get("business_name") and not dialogs.confirm(
                self.app, "لم تُدخل اسم المؤسسة في الإعدادات. هل تريد المتابعة؟"):
            self.app.show_page("settings")
            return
        if not self._confirm_stock(data, editing_id):
            return
        target: Optional[Path] = None
        if editing_id is None and self.app.repo.get_setting("ask_save_location", "0") == "1":
            suggested = self.app.service.default_pdf_path(data)
            chosen, _ = QFileDialog.getSaveFileName(self.app, "اختر مكان حفظ الفاتورة", str(suggested), "ملف PDF (*.pdf)")
            if not chosen:
                return
            target = Path(chosen)
        self.save_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if editing_id is not None:
                result = self.app.service.edit(editing_id, data)
            else:
                result = self.app.service.issue(data, target)
        except ValidationError as exc:
            QApplication.restoreOverrideCursor()
            dialogs.error(self.app, str(exc), "تحقق من البيانات")
            if exc.field.endswith("_no") and editing_id is None:
                self._renew_serial()
            self._focus(exc.field)
            return
        except TemplateError as exc:
            QApplication.restoreOverrideCursor()
            log.error("template problem: %s", exc)
            dialogs.error(self.app, f"مشكلة في قالب Excel:\n{exc}", "القالب")
            return
        except Exception as exc:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            log.error("issue failed", exc_info=True)
            dialogs.error(self.app, f"تعذر إنشاء الفاتورة:\n{exc}\n\nالتفاصيل في الملف: {self.app.log_file}")
            return
        finally:
            if QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            self.save_button.setEnabled(True)
        self._after_issue(data, result, edited=editing_id is not None)

    def _confirm_stock(self, data: Dict[str, Any], editing_id: Optional[int]) -> bool:
        """Before saving a sale: say so when items are not tracked in stock or would take a product below zero."""
        repo, items = self.app.repo, data["items"]
        if self._stock_off:
            return True
        if repo.get_setting("warn_unlinked", "1") == "1":
            loose = [i["description"] for i in items if i.get("product_id") is None]
            if loose:
                shown = "\n".join(f"• {n}" for n in loose[:10]) + (f"\n… و{len(loose) - 10} أخرى" if len(loose) > 10 else "")
                if not dialogs.confirm(self.app, "هذه السلع غير موجودة في المخزون، فلن يتغير مخزونها:\n" + shown +
                                       "\n\nإن كانت خطأ في كتابة صنف موجود فارجع واختره من القائمة.",
                                       yes="متابعة", no="رجوع للتعديل"):
                    return False
        short = repo.stock.shortages(items, exclude_invoice_id=editing_id)
        if short:
            shown = "\n".join(f"• {x['name']}: المطلوب {plain_number(x['requested'])} والمتاح {plain_number(x['available'])} كلغ"
                              for x in short[:10])
            if not dialogs.confirm(self.app, "الكمية المباعة أكبر من المتوفر في المخزون:\n" + shown +
                                   "\n\nإن تابعت يصبح رصيد هذه الأصناف سالبًا (سجّل فاتورة شراء لتصحيحه).",
                                   yes="متابعة رغم ذلك", no="رجوع للتعديل", danger=True):
                return False
        return True

    def _after_issue(self, data: Dict[str, Any], result, edited: bool = False) -> None:
        if self.auto_save.isChecked():
            try:
                self.app.repo.save_customer(data["customer"])
            except ValueError:
                pass
        self.app.refresh_customers()
        self.app.refresh_history()
        self.app.refresh_stock()
        lines = [f"رقم الفاتورة: {data['invoice_no']}", f"الرقم التسلسلي: {data['serial_no']}",
                 f"المجموع TTC: {money(data['total'])} دج"]
        buttons = []
        if result.pdf_ok:
            lines.append(f"\nملف PDF:\n{result.pdf_path}")
            buttons.append(("فتح PDF", "pdf"))
        else:
            lines.append("\n" + result.warnings[0])
        if result.xlsm_path:
            buttons.append(("فتح ملف Excel", "xlsm"))
        buttons += [("فاتورة جديدة", "new"), ("إغلاق", "close")]
        choice = dialogs.choose(self.app, "\n".join(lines), buttons, "info" if result.pdf_ok else "warn",
                                "تم حفظ التعديلات" if edited else "تم حفظ الفاتورة")
        if choice == "pdf":
            self.app.open_path(result.pdf_path)
        elif choice == "xlsm":
            self.app.open_path(result.xlsm_path)
        self.new_invoice()
        self.app.status(f"تم تعديل الفاتورة {data['invoice_no']}" if edited else f"تم حفظ الفاتورة {data['invoice_no']}")
        if edited:
            self.app.show_page("history")
