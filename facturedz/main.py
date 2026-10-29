# -*- coding: utf-8 -*-
"""Application entry point. Always tells the user why it could not start (there is no console)."""
from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

from .paths import SingleInstance, data_home, setup_logging

EXIT_HANDLED = 2        # a message was already shown to the user (the launcher must not add another)


def native_message(title: str, text: str) -> None:
    """Message box that works even when Qt itself is unavailable."""
    try:
        if os.name == "nt":
            import ctypes
            MB_ICONERROR, MB_RIGHT, MB_RTLREADING = 0x10, 0x80000, 0x100000
            ctypes.windll.user32.MessageBoxW(None, text, title, MB_ICONERROR | MB_RIGHT | MB_RTLREADING)
            return
    except Exception:  # noqa: BLE001
        pass
    if sys.stderr:
        print(f"{title}: {text}", file=sys.stderr)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--demo" in argv:                       # safe sandbox data, real data untouched
        os.environ.setdefault("FACTUREDZ_HOME", str(Path(__file__).resolve().parent.parent / "demo"))
    try:
        home = data_home()
    except OSError as exc:
        native_message("Facture DZ", f"تعذر الوصول إلى مجلد البيانات:\n{exc}")
        return EXIT_HANDLED
    setup_logging(home)
    log = logging.getLogger("facturedz")
    guard = SingleInstance(home)
    if not guard.acquire():
        native_message("دفتر الفواتير", "البرنامج يعمل بالفعل. أغلق النافذة الأخرى أولًا.")
        return EXIT_HANDLED
    try:
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            native_message("دفتر الفواتير", "مكتبة PySide6 غير مثبتة.\nافتح موجه الأوامر ونفّذ:\n"
                                            "py -m pip install -r requirements.txt")
            return EXIT_HANDLED
        from .db import DatabaseError, Repository
        from .purchase_service import PurchaseService
        from .service import InvoiceService
        from .ui import theme
        from .ui.app import MainWindow

        theme.prepare_application()
        app = QApplication(sys.argv[:1])
        theme.apply(app)
        try:
            repo = Repository(home)
        except DatabaseError as exc:
            native_message("دفتر الفواتير", str(exc))
            return EXIT_HANDLED
        if "--demo" in argv and not repo.list_invoices(limit=1):
            from .demo import build_demo
            build_demo(repo, InvoiceService(repo))
        window = MainWindow(repo, InvoiceService(repo), home, PurchaseService(repo))
        window.showMaximized()                 # always start maximized
        log.info("started")
        return app.exec()
    except Exception as exc:  # noqa: BLE001 - last line of defence
        log.critical("startup failure", exc_info=True)
        (home / "startup_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        native_message("دفتر الفواتير", f"تعذر تشغيل البرنامج:\n{exc}\n\nالتفاصيل في:\n{home / 'startup_error.log'}")
        return EXIT_HANDLED
    finally:
        guard.release()
