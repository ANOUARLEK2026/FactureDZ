# -*- coding: utf-8 -*-
"""Locations, logging and the single-instance guard."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from . import APP_ID


def app_root() -> Path:
    """Project folder (or the PyInstaller bundle folder when frozen)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def template_path() -> Path:
    return app_root() / "assets" / "invoice_template.xlsm"


def data_home() -> Path:
    """Per-user data folder. FACTUREDZ_HOME overrides it (portable use / tests)."""
    override = os.environ.get("FACTUREDZ_HOME")
    if override:
        base = Path(override).expanduser()
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / APP_ID
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / APP_ID
    base.mkdir(parents=True, exist_ok=True)
    return base


def log_path(home: Path) -> Path:
    return home / "app.log"


def setup_logging(home: Path) -> None:
    """Rotating log (3 x 512 KB) so the file can never grow without bound."""
    root = logging.getLogger()
    if any(getattr(h, "_facturedz", False) for h in root.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(
        log_path(home), maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
    handler._facturedz = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    def _hook(exc_type, exc, tb):
        logging.getLogger("facturedz").critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook


class SingleInstance:
    """Cross-platform advisory lock so two copies can't hand out the same invoice number."""

    def __init__(self, home: Path):
        self.path = home / "app.lock"
        self._fh = None

    def acquire(self) -> bool:
        try:
            self._fh = open(self.path, "a+")
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release(self) -> None:
        try:
            if self._fh:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                self._fh.close()
        except OSError:
            pass
        self._fh = None
