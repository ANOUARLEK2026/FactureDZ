# -*- coding: utf-8 -*-
"""Small message helpers on top of QMessageBox (the app's RTL direction applies automatically)."""
from __future__ import annotations

from typing import Any, Sequence, Tuple

from PySide6.QtWidgets import QMessageBox, QWidget

_ICON = {"info": QMessageBox.Icon.Information, "warn": QMessageBox.Icon.Warning,
         "error": QMessageBox.Icon.Critical, "ask": QMessageBox.Icon.Question}
_TITLE = {"info": "معلومة", "warn": "تنبيه", "error": "خطأ", "ask": "تأكيد"}


def choose(parent: QWidget, message: str, buttons: Sequence[Tuple[str, Any]], kind: str = "info",
           title: str = "", default: int = 0) -> Any:
    """Show ``message`` with custom buttons; returns the value of the clicked one (None if closed)."""
    box = QMessageBox(parent)
    box.setIcon(_ICON.get(kind, QMessageBox.Icon.Information))
    box.setWindowTitle(title or _TITLE.get(kind, ""))
    box.setText(message if len(message) < 1500 else message[:1500] + "…")
    made = [(box.addButton(text, QMessageBox.ButtonRole.ActionRole), value) for text, value in buttons]
    box.setDefaultButton(made[default][0])
    box.exec()
    clicked = box.clickedButton()
    return next((value for btn, value in made if btn is clicked), None)


def info(parent, message: str, title: str = "") -> None:
    choose(parent, message, (("موافق", True),), "info", title)


def warn(parent, message: str, title: str = "") -> None:
    choose(parent, message, (("موافق", True),), "warn", title)


def error(parent, message: str, title: str = "") -> None:
    choose(parent, message, (("موافق", True),), "error", title)


def confirm(parent, message: str, title: str = "", yes: str = "نعم", no: str = "لا", danger: bool = False) -> bool:
    return bool(choose(parent, message, ((yes, True), (no, False)), "warn" if danger else "ask", title,
                       default=1 if danger else 0))
