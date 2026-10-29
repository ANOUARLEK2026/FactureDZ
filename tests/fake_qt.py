"""A tiny stand-in for PySide6, enough to RUN the application's page code without Qt (CI machines, sandboxes).

It implements real behaviour for what the pages' logic depends on - signals, line edits, buttons, combo boxes,
standard item models, sorting/filtering proxies, selection - and accepts (ignores) every purely visual call.
It cannot judge looks or real Qt API misuse; tests/test_stock_ui_real.py runs the same scenarios on real PySide6.
"""
from __future__ import annotations

import inspect
import re
import sys
import types

_ROLES = {"DisplayRole": 0, "EditRole": 2, "ToolTipRole": 3, "FontRole": 6, "TextAlignmentRole": 7,
          "BackgroundRole": 8, "ForegroundRole": 9, "UserRole": 256}
_SIGNALS = {"activated", "clicked", "toggled", "textChanged", "currentIndexChanged", "selectionChanged",
            "doubleClicked", "customContextMenuRequested", "editingFinished", "returnPressed", "closeEditor",
            "commitData", "dataChanged", "buttonClicked", "triggered"}
_PLAIN_METHODS = {"show", "hide", "close", "update", "clear", "resize", "move", "exec", "accept", "reject", "text",
                  "font", "style", "parent", "layout", "size", "width", "height", "palette", "viewport", "click",
                  "edit", "repaint", "raise_", "lower", "focusWidget"}
_CAMEL = re.compile(r"^[a-z]+[A-Z]")


class _Flag(int):
    def __new__(cls, name):
        value = _ROLES.get(name)
        if value is None:
            value = 1000 + (sum(ord(c) * (i + 1) for i, c in enumerate(name)) % 5000)
        obj = super().__new__(cls, value)
        obj.name = name
        return obj

    def __or__(self, other):
        return int(self) | int(other)


class _Namespace:
    def __init__(self, level=0):
        self._level = level

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Namespace(self._level + 1) if self._level == 0 else _Flag(name)


class _Stub:
    """Absorbs any call/attribute: the result of purely visual Qt calls."""
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _noop

    def __call__(self, *a, **k):
        return _Stub()

    def __iter__(self):
        return iter(())

    def __int__(self):
        return 0

    __add__ = __radd__ = __mul__ = __rmul__ = __sub__ = __rsub__ = lambda self, other: _Stub()


def _noop(*a, **k):
    return _Stub()


def _fit_args(slot, args):
    """PySide calls a slot with as many of the signal's arguments as it accepts: do the same."""
    try:
        params = list(inspect.signature(slot).parameters.values())
    except (TypeError, ValueError):
        return args
    if any(p.kind is p.VAR_POSITIONAL for p in params):
        return args
    return args[:sum(1 for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))]


class _Bound:
    def __init__(self, owner):
        self.owner, self.slots = owner, []

    def connect(self, fn):
        self.slots.append(fn)

    def disconnect(self, fn=None):
        self.slots = [s for s in self.slots if fn is not None and s is not fn]

    def emit(self, *args):
        if getattr(self.owner, "_blocked", False):
            return
        for slot in list(self.slots):
            slot(*_fit_args(slot, args))


class Signal:
    def __init__(self, *types):
        self.name = ""

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return obj.__dict__.setdefault("_signals", {}).setdefault(self.name, _Bound(obj))


class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Namespace(1)


class _Obj(metaclass=_Meta):
    def __init__(self, *args, **kwargs):
        self._parent = next((a for a in args if isinstance(a, _Obj)), None)
        self._props = {}
        self._blocked = False
        self._enabled = True
        self._visible = True

    def __getattr__(self, name):
        if name.startswith("__") or name.startswith("_"):
            raise AttributeError(name)
        if name == "count":                       # layouts: nothing to take out
            return lambda: 0
        if name in _SIGNALS:
            return self.__dict__.setdefault("_signals", {}).setdefault(name, _Bound(self))
        if name in _PLAIN_METHODS or _CAMEL.match(name):
            return _noop
        raise AttributeError(name)

    # shared behaviour -----------------------------------------------------------------------
    def blockSignals(self, flag):
        previous, self._blocked = self._blocked, bool(flag)
        return previous

    def setProperty(self, key, value):
        self._props[key] = value

    def property(self, key):
        return self._props.get(key)

    def setEnabled(self, flag):
        self._enabled = bool(flag)

    def isEnabled(self):
        return self._enabled

    def setVisible(self, flag):
        self._visible = bool(flag)

    def isVisible(self):
        return self._visible

    def setObjectName(self, name):
        self._object_name = name

    def objectName(self):
        return getattr(self, "_object_name", "")

    def parent(self):
        return self._parent


class _ModelIndex:
    def __init__(self, model=None, row=-1, col=-1):
        self._model, self._row, self._col = model, row, col

    def isValid(self):
        return self._model is not None and self._row >= 0

    def row(self):
        return self._row

    def column(self):
        return self._col

    def data(self, role=0):
        return self._model._data_at(self._row, self._col, role) if self.isValid() else None

    def siblingAtColumn(self, col):
        return _ModelIndex(self._model, self._row, col)

    def model(self):
        return self._model


class QModelIndex(_ModelIndex):
    pass


class QObject(_Obj):
    pass


class QWidget(QObject):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._style = ""

    def setStyleSheet(self, s):
        self._style = s

    def styleSheet(self):
        return self._style

    def setFocus(self, *a):
        _Focus.widget = self

    def hasFocus(self):
        return _Focus.widget is self


class _Focus:
    widget = None


class QFrame(QWidget):
    pass


class QLabel(QFrame):
    def __init__(self, text="", *a):
        super().__init__(*a)
        self._text = str(text)

    def setText(self, text):
        self._text = str(text)

    def text(self):
        return self._text

    def setWordWrap(self, flag):
        pass


class QAbstractButton(QWidget):
    clicked = Signal(bool)
    toggled = Signal(bool)

    def __init__(self, text="", *a):
        super().__init__(*a)
        self._text, self._checked, self._checkable, self._group = str(text), False, False, None

    def setText(self, text):
        self._text = str(text)

    def text(self):
        return self._text

    def setCheckable(self, flag):
        self._checkable = bool(flag)

    def isChecked(self):
        return self._checked

    def setChecked(self, flag):
        flag = bool(flag)
        if flag and self._group is not None:
            for other in self._group.buttons():
                if other is not self:
                    other._checked = False
        self._checked = flag

    def click(self):
        if not self._enabled:
            return
        if self._checkable:
            self.setChecked(True if self._group is not None else not self._checked)
        self.clicked.emit(self._checked)


class QPushButton(QAbstractButton):
    pass


class QCheckBox(QAbstractButton):
    def __init__(self, text="", *a):
        super().__init__(text, *a)
        self._checkable = True


class QButtonGroup(QObject):
    def __init__(self, *a):
        super().__init__(*a)
        self._buttons = []

    def addButton(self, button, *a):
        self._buttons.append(button)
        button._group = self

    def buttons(self):
        return list(self._buttons)

    def setExclusive(self, flag):
        pass


class QLineEdit(QWidget):
    textChanged = Signal(str)

    def __init__(self, text="", *a):
        super().__init__(*a)
        self._text = text if isinstance(text, str) else ""
        self._read_only = False
        self._completer = None

    def text(self):
        return self._text

    def setText(self, text):
        text = str(text)
        if text != self._text:
            self._text = text
            self.textChanged.emit(text)

    def clear(self):
        self.setText("")

    def setReadOnly(self, flag):
        self._read_only = bool(flag)

    def isReadOnly(self):
        return self._read_only

    def setCompleter(self, completer):
        self._completer = completer


class QComboBox(QWidget):
    currentIndexChanged = Signal(int)
    activated = Signal(int)

    def __init__(self, *a):
        super().__init__(*a)
        self._items, self._index = [], -1

    def addItem(self, text, data=None):
        self._items.append((str(text), data))
        if self._index < 0:
            self._index = 0

    def addItems(self, texts):
        for t in texts:
            self.addItem(t)

    def clear(self):
        self._items, self._index = [], -1

    def count(self):
        return len(self._items)

    def currentIndex(self):
        return self._index

    def setCurrentIndex(self, i):
        i = i if 0 <= i < len(self._items) else -1
        if i != self._index:
            self._index = i
            self.currentIndexChanged.emit(i)

    def currentText(self):
        return self._items[self._index][0] if self._index >= 0 else ""

    def currentData(self):
        return self._items[self._index][1] if self._index >= 0 else None

    def findText(self, text):
        return next((i for i, (t, _d) in enumerate(self._items) if t == text), -1)

    def model(self):
        return self


class QCompleter(QObject):
    activated = Signal(str)

    def __init__(self, items=None, *a):
        super().__init__(*a)
        self.items = list(items) if isinstance(items, (list, tuple)) else []

    def setModel(self, model):
        pass


class QShortcut(QObject):
    activated = Signal()


class QKeySequence(_Obj):
    pass


# ---------------------------------------------------------------------------------------------- models
class QStandardItem:
    def __init__(self, text=""):
        self._data = {0: str(text)}

    def text(self):
        return self._data.get(0, "")

    def data(self, role=0):
        return self._data.get(int(role))

    def setData(self, value, role=2):
        self._data[int(role)] = value

    def setEditable(self, flag):
        pass

    def setTextAlignment(self, a):
        self._data[7] = a

    def setForeground(self, brush):
        self._data[9] = brush

    def foreground(self):
        return self._data.get(9)


class QStandardItemModel(QObject):
    def __init__(self, rows=0, cols=0, *a):
        super().__init__(*a)
        self._rows, self.version = [], 0

    def setHorizontalHeaderLabels(self, labels):
        pass

    def setRowCount(self, n):
        self._rows = self._rows[:n]
        self.version += 1

    def appendRow(self, items):
        self._rows.append(list(items))
        self.version += 1

    def rowCount(self, parent=None):
        return len(self._rows)

    def item(self, row, col=0):
        return self._rows[row][col] if 0 <= row < len(self._rows) else None


class QAbstractTableModel(QObject):
    dataChanged = Signal(object, object)

    def index(self, row, col, parent=None):
        return _ModelIndex(self, row, col)

    def _data_at(self, row, col, role):
        return self.data(_ModelIndex(self, row, col), role)


class QSortFilterProxyModel(QObject):
    def __init__(self, *a):
        super().__init__(*a)
        self._source, self._sort, self._role = None, None, 0

    def setSourceModel(self, model):
        self._source = model

    def sourceModel(self):
        return self._source

    def setSortRole(self, role):
        self._role = int(role)

    def filterAcceptsRow(self, row, parent):
        return True

    def invalidateFilter(self):
        pass

    def sort(self, col, order=None):
        self._sort = (col, order)

    def _map(self):
        rows = [r for r in range(self._source.rowCount()) if self.filterAcceptsRow(r, None)]
        if self._sort is not None:
            col, order = self._sort
            rows.sort(key=lambda r: self._source.item(r, col).data(self._role), reverse=getattr(order, "name", "") == "DescendingOrder")
        return rows

    @property
    def version(self):
        return self._source.version

    def rowCount(self, parent=None):
        return len(self._map())

    def index(self, row, col, parent=None):
        return _ModelIndex(self, row, col)

    def _data_at(self, row, col, role):
        mapped = self._map()
        return self._source.item(mapped[row], col).data(role) if 0 <= row < len(mapped) else None


class _SelectionModel(QObject):
    selectionChanged = Signal(object, object)

    def __init__(self, view):
        super().__init__()
        self.view, self.row, self.version = view, None, -1

    def selectedRows(self, col=0):
        model = self.view._model
        if self.row is None or model is None or self.version != getattr(model, "version", 0) or self.row >= model.rowCount():
            return []
        return [_ModelIndex(model, self.row, col)]


class QTableView(QWidget):
    doubleClicked = Signal(object)
    clicked = Signal(object)
    customContextMenuRequested = Signal(object)

    def __init__(self, *a):
        super().__init__(*a)
        self._model, self._sel = None, _SelectionModel(self)

    def setModel(self, model):
        self._model = model

    def model(self):
        return self._model

    def selectionModel(self):
        return self._sel

    def selectRow(self, row):
        if self._model is not None and 0 <= row < self._model.rowCount():
            self._sel.row, self._sel.version = row, getattr(self._model, "version", 0)
            self._sel.selectionChanged.emit(None, None)

    def clearSelection(self):
        self._sel.row = None
        self._sel.selectionChanged.emit(None, None)

    def sortByColumn(self, col, order=None):
        if hasattr(self._model, "sort"):
            self._model.sort(col, order)

    def indexAt(self, pos):
        return QModelIndex()

    def setCurrentIndex(self, index):
        self._current = index

    def currentIndex(self):
        return getattr(self, "_current", QModelIndex())

    def edit(self, index):
        self._editing = index


class QStyledItemDelegate(QObject):
    pass


class QStackedWidget(QWidget):
    def __init__(self, *a):
        super().__init__(*a)
        self._widgets, self._current = [], None

    def addWidget(self, w):
        self._widgets.append(w)
        self._current = self._current or w

    def setCurrentWidget(self, w):
        self._current = w

    def currentWidget(self):
        return self._current


class QMainWindow(QWidget):
    def statusBar(self):
        return _Stub()


class QDialog(QWidget):
    result_value = 1

    def exec(self):
        return type(self).result_value

    def accept(self):
        pass

    def reject(self):
        pass


class QApplication(_Obj):
    _instance = None

    def __init__(self, *a):
        super().__init__()
        QApplication._instance = self

    @staticmethod
    def instance():
        return QApplication._instance

    @staticmethod
    def setOverrideCursor(c):
        pass

    @staticmethod
    def restoreOverrideCursor():
        pass

    @staticmethod
    def overrideCursor():
        return None


class QTimer(_Obj):
    @staticmethod
    def singleShot(ms, fn=None):
        pass


class QDesktopServices:
    opened = []

    @staticmethod
    def openUrl(url):
        QDesktopServices.opened.append(url)
        return True


class QUrl:
    @staticmethod
    def fromLocalFile(p):
        return p


class QColor(_Obj):
    def __init__(self, *a):
        super().__init__()
        self.args = a


class QBrush(_Obj):
    def __init__(self, *a):
        super().__init__()
        self.args = a


class QEvent(_Obj):
    pass


# ---------------------------------------------------------------------------------------------- module wiring
_EXPLICIT = {
    "QtCore": {"Qt": _Namespace(0), "Signal": Signal, "QModelIndex": QModelIndex, "QAbstractTableModel": QAbstractTableModel,
               "QSortFilterProxyModel": QSortFilterProxyModel, "QTimer": QTimer, "QUrl": QUrl, "QEvent": QEvent,
               "QObject": QObject},
    "QtGui": {"QColor": QColor, "QBrush": QBrush, "QStandardItem": QStandardItem, "QStandardItemModel": QStandardItemModel,
              "QDesktopServices": QDesktopServices, "QKeySequence": QKeySequence, "QShortcut": QShortcut},
    "QtWidgets": {"QWidget": QWidget, "QFrame": QFrame, "QLabel": QLabel, "QPushButton": QPushButton,
                  "QCheckBox": QCheckBox, "QButtonGroup": QButtonGroup, "QLineEdit": QLineEdit, "QComboBox": QComboBox,
                  "QCompleter": QCompleter, "QTableView": QTableView, "QStyledItemDelegate": QStyledItemDelegate,
                  "QStackedWidget": QStackedWidget, "QMainWindow": QMainWindow, "QDialog": QDialog,
                  "QApplication": QApplication},
}


def _module(name, explicit):
    mod = types.ModuleType(name)
    cache = dict(explicit)

    def __getattr__(attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        if attr not in cache:
            cache[attr] = _Meta(attr, (QWidget,), {})
        return cache[attr]

    mod.__getattr__ = __getattr__
    mod.__dict__.update(explicit)
    return mod


def install():
    """Put the fake PySide6 into sys.modules. Returns what ``uninstall`` needs."""
    saved = {k: sys.modules.get(k) for k in list(sys.modules) if k == "PySide6" or k.startswith("PySide6.")}
    saved.update({k: sys.modules[k] for k in list(sys.modules) if k.startswith("facturedz.ui")})
    for k in list(sys.modules):
        if k.startswith("facturedz.ui"):
            del sys.modules[k]
    root = types.ModuleType("PySide6")
    root.__path__ = []
    root.__version__ = "fake"
    sys.modules["PySide6"] = root
    for name, explicit in _EXPLICIT.items():
        mod = _module(f"PySide6.{name}", explicit)
        sys.modules[f"PySide6.{name}"] = mod
        setattr(root, name, mod)
    _Focus.widget = None
    QDesktopServices.opened.clear()
    return saved


def uninstall(saved):
    for k in list(sys.modules):
        if k == "PySide6" or k.startswith("PySide6.") or k.startswith("facturedz.ui"):
            del sys.modules[k]
    for k, v in saved.items():
        if v is not None:
            sys.modules[k] = v
