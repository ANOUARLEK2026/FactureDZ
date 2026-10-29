# -*- coding: utf-8 -*-
"""Main window: header, right-hand navigation, stacked pages, status bar, shortcuts, start-up checks."""
from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from .. import __version__
from ..config import APP_TITLE
from ..db import Repository
from ..paths import log_path
from ..purchase_service import PurchaseService
from ..service import InvoiceService
from . import dialogs
from .pages.customers import CustomersPage
from .pages.history import HistoryPage
from .pages.invoice import InvoicePage
from .pages.movements import MovementsPage
from .pages.purchase import PurchasePage
from .pages.purchases import PurchasesPage
from .pages.settings import SettingsPage
from .pages.stock import StockPage
from .pages.suppliers import SuppliersPage
from .widgets import button, label

log = logging.getLogger("facturedz.ui")

PAGES = [("invoice", "فاتورة جديدة", "＋", "إنشاء فاتورة"), ("history", "الفواتير المحفوظة", "▤", "سجل الفواتير"),
         ("customers", "ملفات الزبائن", "◍", "ملفات الزبائن"),
         ("stock", "المخزون", "▦", "المخزون"), ("movements", "حركة المخزون", "↕", "حركة المخزون"),
         ("purchase", "فاتورة شراء جديدة", "＋", "إنشاء فاتورة شراء"), ("purchases", "فواتير الشراء", "▤", "سجل فواتير الشراء"),
         ("suppliers", "ملفات الموردين", "◍", "ملفات الموردين"),
         ("settings", "الإعدادات", "⚙", "الإعدادات")]
# caption shown in the sidebar just before the page with this key
GROUPS = {"invoice": "المبيعات", "stock": "المخزون والمشتريات", "settings": "النظام"}


class MainWindow(QMainWindow):
    def __init__(self, repo: Repository, service: InvoiceService, home: Path,
                 purchase_service: Optional[PurchaseService] = None):
        super().__init__()
        self.repo, self.service, self.home = repo, service, home
        self.purchase_service = purchase_service or PurchaseService(repo)
        self.log_file = log_path(home)
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1060, 700)
        self.resize(1320, 860)
        sys.excepthook = self._on_exception

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        body = QHBoxLayout()
        body.setSpacing(0)
        outer.addLayout(body, 1)
        body.addWidget(self._build_sidebar())               # first widget = right edge in RTL
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)

        self.invoice_page = InvoicePage(self)
        self.history_page = HistoryPage(self)
        self.customers_page = CustomersPage(self)
        self.stock_page = StockPage(self)
        self.movements_page = MovementsPage(self)
        self.purchase_page = PurchasePage(self)
        self.purchases_page = PurchasesPage(self)
        self.suppliers_page = SuppliersPage(self)
        self.settings_page = SettingsPage(self)
        self.pages: Dict[str, QWidget] = {
            "invoice": self.invoice_page, "history": self.history_page, "customers": self.customers_page,
            "stock": self.stock_page, "movements": self.movements_page, "purchase": self.purchase_page,
            "purchases": self.purchases_page, "suppliers": self.suppliers_page, "settings": self.settings_page}
        for page in self.pages.values():
            self.stack.addWidget(page)

        status = self.statusBar()
        status.setSizeGripEnabled(False)
        self.status_label = QLabel("جاهز")
        status.addWidget(self.status_label, 1)
        status.addPermanentWidget(QLabel(f"v{__version__}  •  {home.name}"))

        shortcuts = [("Ctrl+S", self._save_current), ("Ctrl+N", self._new_current)]
        shortcuts += [(f"Ctrl+{i}", lambda k=key: self.show_page(k)) for i, (key, *_r) in enumerate(PAGES, 1)]
        self._shortcuts = []
        for seq, action in shortcuts:
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(action)
            self._shortcuts.append(sc)

        self.show_page("invoice")
        QTimer.singleShot(300, self._startup_checks)

    # ------------------------------------------------------------------ #
    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(68)
        row = QHBoxLayout(header)
        row.setContentsMargins(18, 9, 18, 9)
        row.setSpacing(12)
        badge = label("DZ", "Badge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(52, 48)
        row.addWidget(badge)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("دفتر الفواتير"))
        titles.itemAt(0).widget().setStyleSheet("font-size: 16pt; font-weight: bold;")
        self.subtitle = label("", "HeaderMuted")
        titles.addWidget(self.subtitle)
        row.addLayout(titles)
        row.addStretch(1)
        ctx = QVBoxLayout()
        ctx.setSpacing(0)
        ctx.addWidget(label("الصفحة الحالية", "HeaderMuted"))
        self.page_title = label("")
        self.page_title.setStyleSheet("font-size: 12pt; font-weight: bold;")
        ctx.addWidget(self.page_title)
        row.addLayout(ctx)
        row.addWidget(button("＋  فاتورة جديدة", "header", lambda: self.show_page("invoice")))
        row.addWidget(button("＋  فاتورة شراء", "header", lambda: self.show_page("purchase")))
        row.addWidget(label(dt.date.today().strftime("%d.%m.%Y"), "DatePill"))
        self._refresh_subtitle()
        return header

    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(218)
        col = QVBoxLayout(side)
        col.setContentsMargins(8, 18, 8, 16)
        col.setSpacing(1)
        self.nav_group = QButtonGroup(self)
        self.nav: Dict[str, QPushButton] = {}
        for key, text, icon, _title in PAGES:
            if key in GROUPS:
                cap = label(GROUPS[key], "SideGroup")
                cap.setContentsMargins(12, 10 if key != "invoice" else 0, 12, 4)
                col.addWidget(cap)
            b = QPushButton(f"{icon}   {text}")
            b.setObjectName("Nav")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=key: self.show_page(k))
            self.nav_group.addButton(b)
            self.nav[key] = b
            col.addWidget(b)
        col.addStretch(1)
        for keys, text in (("Ctrl + N", "فاتورة جديدة"), ("Ctrl + S", "حفظ الفاتورة"), ("Ctrl + 1…9", "تنقّل بين الصفحات")):
            r = QHBoxLayout()
            r.setContentsMargins(12, 2, 12, 2)
            r.addWidget(label(text, "SideHint"), 1)
            r.addWidget(label(keys, "SideKey"))
            col.addLayout(r)
        return side

    def _refresh_subtitle(self) -> None:
        name = self.repo.get_seller().get("business_name", "")
        name = name if len(name) <= 52 else name[:52].rstrip() + "…"
        self.subtitle.setText(f"{name} — إدارة الفواتير والزبائن" if name else "إدارة الفواتير والزبائن")

    # ------------------------------------------------------------------ #
    # services used by the pages
    # ------------------------------------------------------------------ #
    def show_page(self, key: str) -> None:
        self.stack.setCurrentWidget(self.pages[key])
        self.nav[key].setChecked(True)
        self.page_title.setText(next(t for k, _a, _b, t in PAGES if k == key))
        if key == "history":
            self.history_page.refresh()
        elif key == "customers":
            self.customers_page.refresh()
        elif key == "settings":
            self.settings_page.run_diagnostics()
        elif key == "invoice":
            self.invoice_page.refresh_customers()
            self.invoice_page.refresh_catalog()
        elif key == "stock":
            self.stock_page.refresh()
        elif key == "movements":
            self.movements_page.refresh()
        elif key == "purchase":
            self.purchase_page.refresh_suppliers()
            self.purchase_page.refresh_catalog()
        elif key == "purchases":
            self.purchases_page.refresh()
        elif key == "suppliers":
            self.suppliers_page.refresh()
        self._refresh_subtitle()

    def status(self, message: str) -> None:
        self.status_label.setText(message)

    def refresh_customers(self) -> None:
        self.invoice_page.refresh_customers()
        self.customers_page.refresh()

    def refresh_history(self) -> None:
        self.history_page.refresh()

    def refresh_suppliers(self) -> None:
        self.purchase_page.refresh_suppliers()
        self.suppliers_page.refresh()

    def refresh_purchases(self) -> None:
        self.purchases_page.refresh()

    def refresh_stock(self) -> None:
        """Something changed the stock (a sale, a purchase, an adjustment, a new product): update every view of it."""
        self.stock_page.refresh()
        self.movements_page.refresh()
        self.invoice_page.refresh_catalog()
        self.purchase_page.refresh_catalog()

    def reload_all(self) -> None:
        self.settings_page.load()
        self.refresh_customers()
        self.refresh_suppliers()
        self.refresh_history()
        self.refresh_purchases()
        self.stock_page.clear()
        self.refresh_stock()
        self.invoice_page.new_invoice()
        self.purchase_page.new_purchase()
        self._refresh_subtitle()

    def open_path(self, path) -> None:
        path = Path(path)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            log.error("could not open %s", path)
            dialogs.error(self, f"تعذر فتح:\n{path}")

    def _save_current(self) -> None:
        current = self.stack.currentWidget()
        if current is self.invoice_page:
            self.invoice_page.save()
        elif current is self.purchase_page:
            self.purchase_page.save()

    def _new_current(self) -> None:
        current = self.stack.currentWidget()
        if current is self.invoice_page:
            self.invoice_page.new_invoice()
        elif current is self.purchase_page:
            self.purchase_page.new_purchase()

    # ------------------------------------------------------------------ #
    def _startup_checks(self) -> None:
        problem = self.repo.integrity_problem()
        if problem:
            log.error("integrity check failed: %s", problem)
            backups = self.repo.list_backups()
            if backups and dialogs.confirm(
                    self, "قاعدة البيانات تالفة (ربما بسبب انقطاع الكهرباء أو إغلاق الجهاز فجأة).\n"
                          f"هل تريد استعادة آخر نسخة احتياطية؟\n{backups[0].name}", yes="استعادة", no="لاحقًا"):
                try:
                    self.repo.restore_backup(backups[0])
                    self.reload_all()
                    dialogs.info(self, "تمت الاستعادة من آخر نسخة احتياطية.")
                except Exception as exc:  # noqa: BLE001
                    log.exception("auto restore failed")
                    dialogs.error(self, f"تعذرت الاستعادة:\n{exc}")
            elif not backups:
                dialogs.error(self, "قاعدة البيانات تالفة ولا توجد نسخ احتياطية.\nراجع مجلد البيانات.")
            return
        self.repo.backup("daily")
        issues = [d for d in self.service.diagnostics() if not d[1]]
        if issues:
            dialogs.warn(self, "\n".join(f"• {name}: {detail}" for name, _ok, detail in issues), "تنبيه")

    def _on_exception(self, exc_type, value, tb) -> None:
        log.error("Unhandled UI error", exc_info=(exc_type, value, tb))
        QTimer.singleShot(0, lambda: dialogs.error(
            self, f"حدث خطأ غير متوقع وتم تسجيله في:\n{self.log_file}\n\n{value}"))
