# -*- coding: utf-8 -*-
"""Date helpers shared by the database, stock and purchase layers (no imports from them: avoids cycles)."""
from __future__ import annotations

import datetime as dt


def date_to_iso(text: str) -> str:
    """dd.mm.yyyy (or legacy variants) -> yyyy-mm-dd; '' if unparseable."""
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(str(text).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def iso_or_today(text: str) -> str:
    return date_to_iso(text) or dt.date.today().isoformat()


def iso_to_display(iso: str) -> str:
    """yyyy-mm-dd -> dd.mm.yyyy ('' stays '')."""
    try:
        return dt.date.fromisoformat(str(iso)).strftime("%d.%m.%Y")
    except ValueError:
        return str(iso or "")


PERIODS = [("all", "كل الفترات"), ("today", "اليوم"), ("week", "آخر 7 أيام"), ("month", "هذا الشهر"),
           ("last_month", "الشهر الماضي"), ("year", "هذه السنة")]


def period_range(key: str, today: "dt.date | None" = None):
    """(from_iso, to_iso) for a named period; ('', '') means no limit."""
    today = today or dt.date.today()
    if key == "today":
        return today.isoformat(), today.isoformat()
    if key == "week":
        return (today - dt.timedelta(days=6)).isoformat(), today.isoformat()
    if key == "month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if key == "last_month":
        end = today.replace(day=1) - dt.timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat()
    if key == "year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    return "", ""
