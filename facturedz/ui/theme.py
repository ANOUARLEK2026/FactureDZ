# -*- coding: utf-8 -*-
"""Palette + one Qt style sheet. Everything visual is restyled from here."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

P = {
    "bg": "#f2f7f5", "bg_deep": "#e8f0ed", "card": "#ffffff", "card_alt": "#f8fbfa",
    "border": "#dae5e1", "border_strong": "#c7d7d1",
    "ink": "#12302a", "ink_soft": "#3d5751", "muted": "#5f7770",
    "brand": "#0b2b26", "brand_soft": "#17453b",
    "accent": "#0b7a57", "accent_dark": "#086247", "accent_soft": "#e4f5ee", "accent_line": "#b3e3d1",
    "gold": "#b9851f", "gold_soft": "#fbf2dc",
    "danger": "#b3261e", "danger_dark": "#8f1d17", "danger_soft": "#fdeceb",
    "warn": "#8a5a00", "warn_soft": "#fff4d6", "ok": "#1b7f4d", "ok_soft": "#e8f6ee",
    "side_hover": "#154037", "side_active": "#1c4f43", "side_text": "#b9d3ca", "side_muted": "#7fa79b",
    "row_alt": "#f7faf9", "select": "#d9f0e7",
}
FAMILY = "Segoe UI"


def prepare_application() -> None:
    """Must run before QApplication is created."""
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def make_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(P["accent"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)
        p.setPen(QColor("white"))
        font = QFont(FAMILY)
        font.setBold(True)
        font.setPixelSize(int(size * 0.46))
        p.setFont(font)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "DZ")
        p.end()
        icon.addPixmap(pm)
    return icon


def apply(app: QApplication) -> None:
    global FAMILY
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)       # the whole UI mirrors natively
    app.setStyle("Fusion")
    have = {f.casefold(): f for f in QFontDatabase.families()}
    for name in ("Segoe UI", "Tahoma", "Arial", "Noto Sans Arabic", "DejaVu Sans"):
        if name.casefold() in have:
            FAMILY = have[name.casefold()]
            break
    app.setFont(QFont(FAMILY, 10))
    app.setApplicationName("Facture DZ")
    app.setWindowIcon(make_icon())
    app.setStyleSheet(stylesheet())


def stylesheet() -> str:
    c = P
    return f"""
* {{ font-family: "{FAMILY}"; }}
QWidget {{ color: {c['ink']}; font-size: 10pt; }}
QMainWindow, QStackedWidget, QScrollArea, QDialog, #Page, #ScrollBody {{ background: {c['bg']}; }}
QScrollArea {{ border: none; }}
QToolTip {{ background: {c['brand']}; color: white; border: none; padding: 4px 8px; }}

#Header {{ background: {c['brand']}; }}
#Header QLabel {{ color: white; background: transparent; }}
#HeaderMuted {{ color: {c['side_muted']}; font-size: 9pt; }}
#Badge {{ background: {c['accent']}; color: white; font-weight: bold; font-size: 14pt; border-radius: 8px; }}
#DatePill {{ background: {c['brand_soft']}; color: {c['side_text']}; border-radius: 6px; padding: 5px 12px; font-weight: bold; }}
#Sidebar {{ background: {c['brand']}; }}
#SideCaption {{ color: {c['side_muted']}; font-size: 8pt; font-weight: bold; background: transparent; }}
#SideHint {{ color: {c['side_text']}; font-size: 8pt; background: transparent; }}
#SideKey {{ background: {c['brand_soft']}; color: {c['side_text']}; font-size: 7pt; font-weight: bold; border-radius: 3px; padding: 1px 5px; }}
QPushButton#Nav {{ background: transparent; color: {c['side_text']}; border: none; border-right: 3px solid transparent;
    text-align: right; padding: 9px 14px; font-size: 10.5pt; border-radius: 0; }}
QPushButton#Nav:hover {{ background: {c['side_hover']}; }}
QPushButton#Nav:checked {{ background: {c['side_active']}; color: white; font-weight: bold; border-right: 3px solid {c['accent']}; }}

#Card {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 8px; }}
#Card QLabel {{ background: transparent; }}
#CardTitle {{ color: {c['brand']}; font-size: 12pt; font-weight: bold; }}
#CardSub {{ color: {c['muted']}; font-size: 9pt; }}
#Rule {{ background: {c['border']}; max-height: 1px; min-height: 1px; }}
#Field {{ color: {c['muted']}; font-size: 8pt; }}
#Muted {{ color: {c['muted']}; font-size: 9pt; }}
#PageTitle {{ color: {c['brand']}; font-size: 17pt; font-weight: bold; }}
#TotalKey {{ color: {c['muted']}; font-size: 8pt; }}
#TotalVal {{ color: {c['ink']}; font-size: 13pt; font-weight: bold; }}
#GrandBox {{ background: {c['accent_soft']}; border-radius: 6px; }}
#GrandBox QLabel {{ background: transparent; }}
#GrandVal {{ color: {c['accent']}; font-size: 16pt; font-weight: bold; }}
#Words {{ color: {c['ink_soft']}; font-size: 8.5pt; font-weight: bold; }}
#BottomBar, #Toolbar {{ background: {c['card']}; border: 1px solid {c['border']}; }}
#Toolbar {{ border-radius: 8px; }}
#Stat {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 8px; }}
#Stat QLabel {{ background: transparent; }}
#StatVal {{ font-size: 15pt; font-weight: bold; }}

QLineEdit, QComboBox {{ background: {c['card']}; border: 1px solid {c['border_strong']}; border-radius: 6px;
    padding: 6px 8px; selection-background-color: {c['accent']}; selection-color: white; min-height: 18px; }}
QLineEdit:focus, QComboBox:focus {{ border: 1px solid {c['accent']}; }}
QLineEdit[invalid="true"] {{ background: {c['danger_soft']}; border: 1px solid {c['danger']}; }}
QLineEdit:read-only {{ background: {c['row_alt']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: white; selection-background-color: {c['accent']}; selection-color: white; border: 1px solid {c['border_strong']}; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QListView {{ background: white; border: 1px solid {c['border_strong']}; selection-background-color: {c['accent']}; selection-color: white; outline: 0; }}
#Chip {{ background: {c['accent_soft']}; color: {c['accent_dark']}; border: 1px solid {c['accent_line']}; border-radius: 10px; padding: 3px 10px; font-weight: bold; }}
#SideGroup {{ color: {c['side_muted']}; font-size: 8pt; font-weight: bold; background: transparent; }}

QPushButton {{ background: #e7efec; color: {c['ink']}; border: none; border-radius: 6px; padding: 8px 16px; }}
QPushButton:hover {{ background: #d9e7e2; }}
QPushButton:disabled {{ background: #cddbd6; color: #f4f7f6; }}
QPushButton[variant="primary"] {{ background: {c['accent']}; color: white; padding: 9px 18px; font-weight: bold; }}
QPushButton[variant="primary"]:hover {{ background: {c['accent_dark']}; }}
QPushButton[variant="ghost"] {{ background: white; color: {c['accent']}; border: 1px solid {c['border_strong']}; }}
QPushButton[variant="ghost"]:hover {{ background: {c['accent_soft']}; border-color: {c['accent_line']}; }}
QPushButton[variant="danger"] {{ background: {c['danger_soft']}; color: {c['danger']}; border: 1px solid #f0c9c5; }}
QPushButton[variant="danger"]:hover {{ background: #f9dcd9; }}
QPushButton[variant="link"] {{ background: transparent; color: {c['accent']}; font-weight: bold; padding: 4px 8px; }}
QPushButton[variant="link"]:hover {{ background: {c['accent_soft']}; }}
QPushButton[variant="header"] {{ background: {c['brand_soft']}; color: white; font-weight: bold; border: 1px solid {c['brand_soft']}; }}
QPushButton[variant="header"]:hover {{ background: {c['side_hover']}; border-color: {c['accent']}; }}
QPushButton[variant="small"] {{ padding: 5px 10px; font-size: 9pt; }}
QPushButton[seg="true"] {{ background: white; border: 1px solid {c['border_strong']}; border-radius: 0; padding: 6px 14px; }}
QPushButton[seg="true"]:checked {{ background: {c['accent']}; color: white; font-weight: bold; border-color: {c['accent']}; }}
QPushButton[seg="true"]:hover:!checked {{ background: {c['accent_soft']}; }}

QTableView {{ background: {c['card']}; alternate-background-color: {c['row_alt']}; border: 1px solid {c['border']};
    gridline-color: {c['border']}; selection-background-color: {c['select']}; selection-color: {c['brand']}; outline: 0; }}
QTableView::item {{ padding: 4px 8px; }}
QTableView QLineEdit {{ border: 2px solid {c['accent']}; border-radius: 0; padding: 2px 6px; }}
QHeaderView::section {{ background: {c['bg_deep']}; color: {c['brand']}; font-weight: bold; padding: 9px 10px;
    border: none; border-left: 1px solid {c['border']}; }}
#ItemsTable QHeaderView::section {{ background: {c['brand']}; color: white; padding: 6px 8px; }}
QScrollBar:vertical {{ background: {c['bg']}; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #c9d8d3; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #b6cbc4; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: {c['bg']}; height: 11px; }}
QScrollBar::handle:horizontal {{ background: #c9d8d3; border-radius: 5px; min-width: 30px; }}
QStatusBar {{ background: {c['bg_deep']}; color: {c['ink_soft']}; border-top: 1px solid {c['border']}; }}
QStatusBar QLabel {{ color: {c['muted']}; font-size: 8pt; padding: 0 8px; }}
QMenu {{ background: white; border: 1px solid {c['border_strong']}; padding: 4px; }}
QMenu::item {{ padding: 7px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c['accent_soft']}; color: {c['brand']}; }}
QMessageBox {{ background: white; }}
QProgressBar {{ background: {c['bg_deep']}; border: none; border-radius: 4px; height: 8px; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 4px; }}
"""
