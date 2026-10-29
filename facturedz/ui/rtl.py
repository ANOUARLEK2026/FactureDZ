# -*- coding: utf-8 -*-
"""Right-to-left layout helpers for Tkinter.

Tk has no mirrored layout mode, so we mirror by construction:
  * form grids put labels on the right and fill from the top-right corner
  * list columns are displayed in reverse order (first column at the right edge)
  * scrollbars sit on the left
  * numeric fields are LTR (centered), text fields are right-aligned
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

from . import theme


def text_entry(parent, var: tk.StringVar, width: int = 24) -> ttk.Entry:
    return ttk.Entry(parent, textvariable=var, width=width, justify="right")


def number_entry(parent, var: tk.StringVar, width: int = 12) -> ttk.Entry:
    e = ttk.Entry(parent, textvariable=var, width=width, justify="center")
    return e


def rtl_label(parent, text: str, style: str = "Card.TLabel", wraplength: int = 0, **kw) -> ttk.Label:
    return ttk.Label(parent, text=text, style=style, anchor="e", justify="right",
                     wraplength=wraplength, **kw)


def build_field_grid(parent, fields: Sequence[Tuple[str, str]], variables: Dict[str, tk.StringVar],
                     wide: Iterable[str] = (), label_width: int = 20) -> Dict[str, ttk.Entry]:
    """Two-column RTL form: field 0 top-right, field 1 to its left, field 2 below field 0 ...

    Grid columns (left -> right): 0 entry-B | 1 label-B | 2 entry-A | 3 label-A.
    Fields in ``wide`` span both groups. Returns key -> Entry for focus/validation.
    """
    entries: Dict[str, ttk.Entry] = {}
    parent.grid_columnconfigure(0, weight=1, uniform="e")
    parent.grid_columnconfigure(2, weight=1, uniform="e")
    row, slot = 0, 0
    wide = set(wide)
    for key, label in fields:
        if key in wide:
            if slot == 1:
                row += 1
                slot = 0
            ttk.Label(parent, text=label, style="Card.TLabel", anchor="e", width=label_width).grid(
                row=row, column=3, sticky="e", padx=(8, 0), pady=5)
            entry = text_entry(parent, variables[key])
            entry.grid(row=row, column=0, columnspan=3, sticky="ew", padx=6, pady=5)
            row += 1
            slot = 0
        else:
            label_col, entry_col = (3, 2) if slot == 0 else (1, 0)
            ttk.Label(parent, text=label, style="Card.TLabel", anchor="e", width=label_width).grid(
                row=row, column=label_col, sticky="e", padx=(8, 0), pady=5)
            entry = text_entry(parent, variables[key])
            entry.grid(row=row, column=entry_col, sticky="ew", padx=6, pady=5)
            slot += 1
            if slot == 2:
                slot, row = 0, row + 1
        entries[key] = entry
    return entries


def reverse_columns(tree: ttk.Treeview, columns: Sequence[str]) -> None:
    tree.configure(displaycolumns=tuple(reversed(list(columns))))


def make_tree(parent, columns: Sequence[Tuple[str, str, int]], height: int = 12):
    """RTL Treeview with the scrollbar on the left. columns = [(key, heading, width)], first = rightmost."""
    holder = ttk.Frame(parent, style="Card.TFrame")
    keys = [c[0] for c in columns]
    tree = ttk.Treeview(holder, columns=keys, show="headings", selectmode="browse", height=height)
    for key, heading, width in columns:
        tree.heading(key, text=heading, anchor="e")
        tree.column(key, width=width, anchor="e", stretch=True)
    reverse_columns(tree, keys)
    scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    scroll.pack(side="left", fill="y")
    tree.pack(side="right", fill="both", expand=True)
    tree.tag_configure("alt", background=theme.P["row_alt"])
    tree.tag_configure("missing", foreground=theme.P["danger"])
    return holder, tree


class ScrollFrame(ttk.Frame):
    """Vertically scrollable area (scrollbar on the left). Put children in ``.inner``."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, bg=theme.P["bg"], highlightthickness=0, borderwidth=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="left", fill="y")
        self.canvas.pack(side="right", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        for widget in (self.canvas, self.inner):
            widget.bind("<Enter>", self._bind_wheel)
            widget.bind("<Leave>", self._maybe_unbind_wheel)

    def _bind_wheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _maybe_unbind_wheel(self, event=None):
        try:
            x, y = self.winfo_pointerxy()
            inside = self.winfo_containing(x, y)
            while inside is not None:
                if inside == self:
                    return
                inside = inside.master
        except (tk.TclError, KeyError):
            pass
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)

    def _on_wheel(self, event):
        if self.inner.winfo_reqheight() <= self.canvas.winfo_height():
            return
        if getattr(event, "num", None) == 4:
            units = -3
        elif getattr(event, "num", None) == 5:
            units = 3
        else:
            delta = getattr(event, "delta", 0)
            if sys.platform == "darwin":
                units = -int(delta)
            else:
                units = -int(delta / 120) * 3 if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        self.canvas.yview_scroll(units, "units")

    def scroll_to_widget(self, widget) -> None:
        try:
            self.update_idletasks()
            total = max(self.inner.winfo_height(), 1)
            y = widget.winfo_rooty() - self.inner.winfo_rooty()
            self.canvas.yview_moveto(max(0.0, (y - 80) / total))
        except tk.TclError:
            pass


class Segmented(tk.Frame):
    """Segmented choice control (replaces radio buttons: reads right-to-left, indicator-free)."""

    def __init__(self, parent, variable: tk.StringVar, options: Sequence[Tuple[str, str]],
                 on_change: Callable[[], None] = lambda: None):
        super().__init__(parent, bg=theme.P["border"], padx=1, pady=1)
        self.variable, self.on_change = variable, on_change
        self._labels: Dict[str, tk.Label] = {}
        for value, text in options:                      # first option ends up at the right edge
            lab = tk.Label(self, text=text, font=theme.f(11), padx=14, pady=7, cursor="hand2", bd=0)
            lab.pack(side="right", fill="x", expand=True, padx=(0, 1))
            lab.bind("<Button-1>", lambda _e, v=value: self.select(v))
            self._labels[value] = lab
        variable.trace_add("write", lambda *_: self._paint())
        self._paint()

    def select(self, value: str) -> None:
        self.variable.set(value)
        self.on_change()

    def _paint(self) -> None:
        for value, lab in self._labels.items():
            on = self.variable.get() == value
            lab.configure(bg=theme.P["accent"] if on else "#ffffff",
                          fg="#ffffff" if on else theme.P["ink"],
                          font=theme.f(11, on))


def bind_enter_chain(entries: List[tk.Widget]) -> None:
    for current, nxt in zip(entries, entries[1:]):
        current.bind("<Return>", lambda _e, n=nxt: (n.focus_set(), "break")[1])
