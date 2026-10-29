# -*- coding: utf-8 -*-
"""Settings: seller data, defaults, backups and a system check."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QGridLayout, QHBoxLayout, QLineEdit, QScrollArea, QVBoxLayout,
                               QWidget)

from ... import __version__
from ...amounts import plain_number
from ...config import SELLER_FIELDS
from ...db import DatabaseError
from ...service import ValidationError, parse_rate
from .. import dialogs, theme
from ..widgets import Card, FieldGrid, button, label

log = logging.getLogger("facturedz.ui.settings")
_ORDER = ("business_name", "commercial_title", "phone", "address", "rc", "nif", "nis", "bank_account",
          "article_no", "activity", "city")


class SettingsPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.setObjectName("Page")
        self.app = app
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("ScrollBody")
        scroll.setWidget(body)
        col = QVBoxLayout(body)
        col.setContentsMargins(14, 12, 14, 14)
        col.setSpacing(10)

        seller = Card("بيانات البائع", "تظهر على كل فاتورة جديدة")
        labels = dict(SELLER_FIELDS)
        self.seller = FieldGrid([(k, labels[k]) for k in _ORDER if k in labels], 3)
        seller.body.addWidget(self.seller)
        col.addWidget(seller)

        prefs = Card("إعدادات الفاتورة والحفظ")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.vat = QLineEdit()
        self.vat.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vat.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.vat.setMaximumWidth(120)
        self.output = QLineEdit()
        self.output.setReadOnly(True)
        self.output.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        grid.addWidget(label("نسبة TVA الافتراضية (%)", "Field"), 0, 0)
        grid.addWidget(label("مجلد حفظ الفواتير", "Field"), 0, 1)
        grid.addWidget(self.vat, 1, 0)
        out_row = QHBoxLayout()
        out_row.addWidget(self.output, 1)
        out_row.addWidget(button("اختيار…", "small", self.choose_folder))
        grid.addLayout(out_row, 1, 1)
        grid.setColumnStretch(1, 1)
        prefs.body.addLayout(grid)
        self.ask_location = QCheckBox("اسألني عن مكان الحفظ في كل مرة (الافتراضي: حفظ تلقائي في مجلد السنة/الشهر)")
        self.keep_xlsm = QCheckBox("احتفظ بنسخة Excel القابلة للتعديل بجانب ملف PDF")
        self.warn_unlinked = QCheckBox("نبّهني عند بيع سلعة غير موجودة في المخزون (لن يتغير مخزونها)")
        prefs.body.addWidget(self.ask_location)
        prefs.body.addWidget(self.keep_xlsm)
        prefs.body.addWidget(self.warn_unlinked)
        row = QHBoxLayout()
        row.addWidget(button("حفظ الإعدادات", "primary", self.save))
        row.addWidget(label("تُطبَّق على الفواتير القادمة فقط", "Muted"))
        row.addStretch(1)
        prefs.body.addLayout(row)
        col.addWidget(prefs)

        data = Card("البيانات والنسخ الاحتياطي", "تُؤخذ نسخة يومية تلقائيًا (آخر 14 يومًا + نسخة لكل شهر)")
        g = QGridLayout()
        actions = (("⤓  نسخة احتياطية الآن", self.backup_now), ("⇩  حفظ نسخة في مجلد آخر…", self.export_backup),
                   ("↺  استعادة نسخة…", self.restore_backup),
                   ("▣  فتح مجلد البيانات", lambda: self.app.open_path(self.app.home)),
                   ("▤  فتح سجل الأخطاء", lambda: self.app.open_path(self.app.log_file)))
        for i, (text, fn) in enumerate(actions):
            g.addWidget(button(text, "ghost", fn), i // 2, i % 2)
        data.body.addLayout(g)
        data.body.addWidget(label(f"مجلد البيانات: {self.app.home}", "Muted"))
        col.addWidget(data)

        diag = Card("فحص النظام", "يساعد في تشخيص أي مشكلة")
        self.diag = QVBoxLayout()
        diag.body.addLayout(self.diag)
        foot = QHBoxLayout()
        foot.addWidget(button("↻  إعادة الفحص", "small", self.run_diagnostics))
        foot.addStretch(1)
        foot.addWidget(label(f"دفتر الفواتير — الإصدار {__version__}", "Muted"))
        diag.body.addLayout(foot)
        col.addWidget(diag)
        col.addStretch(1)
        self.load()

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        repo = self.app.repo
        self.seller.set_values(repo.get_seller())
        self.vat.setText(repo.get_setting("vat_rate", "19"))
        self.output.setText(repo.get_setting("output_dir", str(repo.default_output)))
        self.ask_location.setChecked(repo.get_setting("ask_save_location", "0") == "1")
        self.keep_xlsm.setChecked(repo.get_setting("keep_xlsm", "1") == "1")
        self.warn_unlinked.setChecked(repo.get_setting("warn_unlinked", "1") == "1")
        self.run_diagnostics()

    def choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self.app, "اختر مجلد حفظ الفواتير",
                                                  self.output.text() or str(self.app.repo.default_output))
        if chosen:
            self.output.setText(chosen)

    def save(self) -> None:
        try:
            rate = parse_rate(self.vat.text())
            folder = Path(self.output.text()).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
        except (ValidationError, OSError) as exc:
            dialogs.error(self.app, f"إعداد غير صحيح:\n{exc}")
            return
        repo = self.app.repo
        repo.save_seller(self.seller.values())
        repo.set_setting("vat_rate", plain_number(rate))
        repo.set_setting("output_dir", str(folder))
        repo.set_setting("ask_save_location", "1" if self.ask_location.isChecked() else "0")
        repo.set_setting("keep_xlsm", "1" if self.keep_xlsm.isChecked() else "0")
        repo.set_setting("warn_unlinked", "1" if self.warn_unlinked.isChecked() else "0")
        self.app.status("تم حفظ الإعدادات.")
        self.app._refresh_subtitle()
        dialogs.info(self.app, "تم حفظ الإعدادات. ستُستخدم في الفواتير القادمة.")

    def run_diagnostics(self) -> None:
        while self.diag.count():
            item = self.diag.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        try:
            rows = self.app.service.diagnostics()
        except Exception as exc:  # noqa: BLE001
            log.exception("diagnostics failed")
            rows = [("الفحص", False, str(exc))]
        for name, ok, detail in rows:
            line = QWidget()
            r = QHBoxLayout(line)
            r.setContentsMargins(0, 4, 0, 4)
            title = label(name)
            title.setStyleSheet("font-weight: bold;")
            title.setMinimumWidth(190)
            r.addWidget(title)
            msg = label(detail, "Muted")
            msg.setWordWrap(True)
            if not ok:
                msg.setStyleSheet(f"color: {theme.P['danger']};")
            r.addWidget(msg, 1)
            pill = label("سليم" if ok else "تنبيه")
            c, bg = (theme.P["ok"], theme.P["ok_soft"]) if ok else (theme.P["danger"], theme.P["danger_soft"])
            pill.setStyleSheet(f"color: {c}; background: {bg}; border-radius: 4px; padding: 3px 9px; font-weight: bold;")
            r.addWidget(pill)
            self.diag.addWidget(line)

    def backup_now(self) -> None:
        path = self.app.repo.backup("manual", force=True)
        if path:
            dialogs.info(self.app, f"تم إنشاء نسخة احتياطية:\n{path}")
        else:
            dialogs.error(self.app, "تعذر إنشاء النسخة الاحتياطية. راجع سجل الأخطاء.")

    def export_backup(self) -> None:
        target, _ = QFileDialog.getSaveFileName(self.app, "حفظ نسخة احتياطية",
                                                f"factures_{dt.date.today().isoformat()}.db", "قاعدة بيانات (*.db)")
        if target:
            try:
                self.app.repo.export_backup(Path(target))
                dialogs.info(self.app, f"تم حفظ النسخة في:\n{target}")
            except Exception as exc:  # noqa: BLE001
                dialogs.error(self.app, f"تعذر الحفظ:\n{exc}")

    def restore_backup(self) -> None:
        start = self.app.repo.backup_dir if self.app.repo.backup_dir.exists() else self.app.home
        chosen, _ = QFileDialog.getOpenFileName(self.app, "اختر نسخة احتياطية للاستعادة", str(start), "قاعدة بيانات (*.db)")
        if not chosen:
            return
        if not dialogs.confirm(self.app, "ستُستبدل جميع الفواتير والزبائن الحاليين بمحتوى هذه النسخة.\n"
                                         "تُحفظ نسخة من البيانات الحالية قبل الاستبدال. هل تريد المتابعة؟",
                               yes="استعادة", no="إلغاء", danger=True):
            return
        try:
            safety = self.app.repo.restore_backup(Path(chosen))
        except (DatabaseError, OSError) as exc:
            dialogs.error(self.app, f"تعذرت الاستعادة:\n{exc}")
            return
        self.app.reload_all()
        dialogs.info(self.app, f"تمت الاستعادة.\nنسخة البيانات السابقة محفوظة في:\n{safety}")
