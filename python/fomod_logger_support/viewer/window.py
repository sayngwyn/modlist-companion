"""The Modlist Companion window: a normal window beside MO2, never modal to it.

It reads the database directly on MO2's main thread and redraws when the
plugin says something changed. Colours come from MO2's palette, with green
for picked, red for possible missed patches and yellow for reminders.
"""

from __future__ import annotations

import copy
import dataclasses
import html
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QByteArray, QEvent, QMargins, QPoint, QRect, QSize, Qt, QTimer
from PyQt6.QtGui import QAction, QBrush, QColor, QFont, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QHeaderView, QLabel, QLayout, QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QStyle, QStyleOptionToolButton, QStylePainter,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core import compare_choices, export_choices, import_choices
from ..core.choices_file import ChoicesFileError
from ..core.file_origin import PLAIN_OPTION, OriginCache, plain_path_matches
from ..core.patch_alerts import join_names
from ..core.storage import EventStore
from . import model, theme
from .compare_dialog import CompareDialog
from .history_view import HistoryView
from .origin_dialog import FileOriginDialog
from .rows import TintDelegate
from .scan_dialog import ScanDialog
from .text_menu import use_themed_menu

_EVENT = "event"
_OPTION = "option"
_PLACEHOLDER = "placeholder"
_SHOW_ALL = "show-all"
_ROLE = Qt.ItemDataRole.UserRole
# The "Files (N)" row under an option: (install, option) of the files it lists, filled in when it is opened.
_FILES_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_PATH_TAIL = 44  # characters of a path shown in a status column; the tooltip has all of it
_ARCHIVE_TAIL = 30  # characters of an archive name shown on a row of a mod without installer options
_FIRST_RUN_GUIDE = ("Install a mod and it shows up here with its FOMOD choices, or choose More > Scan existing mods… "
                    "to record the mods you already have.")
_FIT_ROWS = 5000  # rows Qt measures to fit the status and date columns (its default, 1,000, misses a long list's tail)
_PLAIN_ROWS = 1000  # file rows made when a mod's "Files (N)" is opened; a mod can hold many more
_SEARCH_DELAY_MS = 175
_REFRESH_DELAY_MS = 50
_AUTO_OPEN_LIMIT = 12
# The side panel builds this many cards in a section, then offers the rest a step at a time.
_CARD_LIMIT = 40
_CARD_STEP = 50
_CHANGE_PATCH_LINES = 3  # new patch options named on a changed-installer card
_SIDE_MIN_WIDTH = 240
_SIDE_MAX_SHARE = 0.5  # of the window
# A list that was written to in the last moments might be written to again within the file
# system's clock tick, so a repeat refresh doesn't trust the file's timestamp until it is older.
_SETTLED_SECONDS = 2.0

_BREAK = "​"  # zero-width space: a place where a line may wrap
_LONG_WORD = 16
_RUN = 10  # break points are only chances to wrap: a line breaks at one only when it has to


def _breakable(text: str) -> str:
    """Offer wrap points inside very long words (plugin and option names with no spaces).

    A label can only wrap at spaces, so one long name would force the whole side panel wider.
    Words up to ``_LONG_WORD`` characters are left alone; longer ones get a break point after
    ``_ - . \\ /`` and at least every ``_RUN`` characters. Only the displayed text changes.
    """

    def split(match: re.Match) -> str:
        word = match.group(0)
        if len(word) <= _LONG_WORD:
            return word
        out, run = [], 0
        for index, char in enumerate(word):
            out.append(char)
            run += 1
            if index < len(word) - 1 and (char in "_-.\\/" or run >= _RUN):
                out.append(_BREAK)
                run = 0
        return "".join(out)

    return re.sub(r"\S+", split, text)


def _short(text: str, limit: int) -> str:
    """A long name cut in the middle, keeping both ends (an archive's version number is at the end)."""

    if len(text) <= limit:
        return text
    head = (limit - 1) // 2
    return text[:head] + "…" + text[-(limit - 1 - head):]


def _plain(text: str, *, wrap: bool = False) -> QLabel:
    """A label that never interprets mod-authored text as HTML. ``wrap`` lets it wrap at spaces and,
    inside very long words, at the break points ``_breakable`` adds."""

    label = QLabel(_breakable(text) if wrap else text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if wrap:
        label.setWordWrap(True)
    return label


class FlowLayout(QLayout):
    """Lays widgets out left to right and starts a new row when the next one doesn't fit."""

    def __init__(self, parent: QWidget | None = None, *, spacing: int = 8, row_spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self._gap = spacing
        self._row_gap = row_spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 - Qt hook
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802 - Qt hook
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802 - Qt hook
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt hook
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt hook
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt hook
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt hook
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt hook
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, row_height = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            width = min(hint.width(), max(1, area.width()))
            if x > area.x() and x + width > area.right() + 1:
                x, y, row_height = area.x(), y + row_height + self._row_gap, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), QSize(width, hint.height())))
            x += width + self._gap
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + margins.bottom()


class _ClickLabel(QLabel):
    """A label that runs ``clicked`` when pressed, so a heading's text works like its button."""

    def __init__(self, text: str, clicked: Callable[[], None]):
        super().__init__(text)
        self._clicked = clicked

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt hook
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self._clicked()
        super().mouseReleaseEvent(event)


class _WrapButton(QToolButton):
    """A flat button whose text wraps. (A tool button draws its text on one line, so a label
    inside it draws the text; ``text()`` still returns what was set.)"""

    _PAD = QMargins(4, 3, 4, 3)

    def __init__(self, text: str, name: str, *, tone: str = ""):
        super().__init__()
        self.setObjectName(name)
        self._text = ""
        self._label = _named(_plain("", wrap=True), "wrapText", tone=tone)
        self._label.setParent(self)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt hook
        self._text = text
        self._label.setText(_breakable(text))
        super().setText(text)
        self.updateGeometry()

    def text(self) -> str:
        return self._text

    def _room(self, width: int) -> int:
        return max(1, width - self._PAD.left() - self._PAD.right())

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt hook
        return self._label.heightForWidth(self._room(width)) + self._PAD.top() + self._PAD.bottom()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt hook
        one_line = self.fontMetrics().horizontalAdvance(self._label.text()) + 2
        width = one_line + self._PAD.left() + self._PAD.right()
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt hook
        narrow = self._label.minimumSizeHint().width() + self._PAD.left() + self._PAD.right()
        return QSize(narrow, self.fontMetrics().lineSpacing() + self._PAD.top() + self._PAD.bottom())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt hook
        super().resizeEvent(event)
        self._label.setGeometry(self._PAD.left(), self._PAD.top(), self._room(self.width()),
                                max(1, self.height() - self._PAD.top() - self._PAD.bottom()))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt hook
        # The button's own background and hover; the text is the label's.
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.text = ""
        QStylePainter(self).drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)


def _colours(t: theme.Tokens) -> dict[str, QColor]:
    hit = QColor(t.mark)
    hit.setAlpha(t.hit_alpha)
    return {"picked": t.picked, "missed": t.missed, "mark": t.mark, "off": t.off, "masters": t.masters, "faint": t.faint,
            "accent": t.accent_text, "hit": hit}


def _named(widget, name: str, **properties):
    """Give a widget the object name (and properties) the stylesheet targets."""

    widget.setObjectName(name)
    for key, value in properties.items():
        widget.setProperty(key, value)
    return widget


class _EmptyState(QWidget):
    """The first-run message in the sayngwyn theme, over the empty list: the mark, faded, above one line in Sayngwyn
    Serif with the red offset shadow, and the guidance below in the window's own font. Clicks go through to the list."""

    def __init__(self, viewport: QWidget):
        super().__init__(viewport)
        self.setObjectName("emptyState")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.mark = QLabel()
        self.mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = _named(_plain(""), "emptyTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.guide = _named(_plain(""), "hint")
        self.guide.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.guide.setWordWrap(True)
        column = QVBoxLayout(self)
        column.setContentsMargins(24, 24, 24, 24)
        column.addStretch(1)
        column.addWidget(self.mark)
        column.addSpacing(12)
        column.addWidget(self.title)
        column.addSpacing(6)
        column.addWidget(self.guide)
        column.addStretch(2)                  # a little above the middle, where the eye lands
        viewport.installEventFilter(self)
        self.setGeometry(viewport.rect())
        self.hide()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt hook
        if event.type() == QEvent.Type.Resize and watched is self.parentWidget():
            self.setGeometry(watched.rect())
        return False

    def present(self, title: str, guide: str) -> bool:
        """Show the message; False (and hidden) when the brand font or mark can't be loaded."""

        family = theme.brand_family()
        pixmap = theme.mark(self.devicePixelRatioF())
        if family is None or pixmap is None:
            self.hide()
            return False
        self.mark.setPixmap(pixmap)
        fade = QGraphicsOpacityEffect(self.mark)
        fade.setOpacity(theme.MARK_OPACITY)
        self.mark.setGraphicsEffect(fade)
        font = QFont(family)
        font.setPixelSize(theme.BRAND_TEXT_PX)
        self.title.setFont(font)
        self.title.setText(title)
        shadow = QGraphicsDropShadowEffect(self.title)
        shadow.setBlurRadius(0)
        step = theme.BRAND_TEXT_PX * theme.SHADOW_SHARE
        shadow.setOffset(step, step)
        shadow.setColor(QColor(theme.BRAND_SHADOW))
        self.title.setGraphicsEffect(shadow)
        self.guide.setText(guide)
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        return True


class ReminderDialog(QDialog):
    """Add, edit or remove one reminder. Returns "save", "remove" or nothing."""

    def __init__(self, parent: QWidget, target: str, existing: dict[str, Any] | None):
        super().__init__(parent)
        self.setWindowTitle("Edit reminder" if existing else "Remind me")
        self.action = ""
        layout = QVBoxLayout(self)
        layout.addWidget(_plain(target))
        layout.addWidget(QLabel("Note to yourself (optional)"))
        self.note = QPlainTextEdit((existing or {}).get("reason", ""))
        use_themed_menu(self.note)
        self.note.setPlaceholderText("Install the Lanterns patch once Lanterns of Skyrim is in")
        layout.addWidget(self.note)
        self.state = QComboBox()
        for value, label in (("pending", "To do"), ("resolved", "Done"), ("dismissed", "Dismissed")):
            self.state.addItem(label, value)
        self.state.setCurrentIndex(max(0, self.state.findData((existing or {}).get("state", "pending"))))
        self.state.setVisible(existing is not None)
        layout.addWidget(self.state)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        if existing is not None:
            remove = buttons.addButton("Remove reminder", QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.setObjectName("tool")
            remove.clicked.connect(lambda: self._finish("remove"))
        for role, name in ((QDialogButtonBox.StandardButton.Save, "tool"), (QDialogButtonBox.StandardButton.Cancel, "tool")):
            buttons.button(role).setObjectName(name)
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)  # the one primary action
        buttons.accepted.connect(lambda: self._finish("save"))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(420, 220)

    def _finish(self, action: str) -> None:
        if len(self.note.toPlainText()) > 2000:
            QMessageBox.warning(self, "Note too long", "Keep the note under 2000 characters.")
            return
        self.action = action
        self.accept()

    def values(self) -> tuple[str, str]:
        return self.note.toPlainText().strip(), self.state.currentData()


class LoggerWindow(QWidget):
    def __init__(self, database: Path, *,
                 load_order: Callable[[], dict[str, Any] | None] | None = None,
                 on_error: Callable[[str], None] | None = None,
                 on_changed: Callable[[], None] | None = None,
                 geometry_file: Path | None = None,
                 instance_id: Callable[[], str] | None = None,
                 game: Callable[[], dict[str, str]] | None = None,
                 profiles: Callable[[], dict[str, Any] | None] | None = None,
                 can_import: bool = True,
                 scanner: Any = None,
                 edition_label: str = "",
                 current_profile: Callable[[], str] | None = None):
        """``can_import`` False hides the choices-file import (an edition whose installer cannot replay it);
        ``scanner`` (see ``plus/scan.py``) adds "Scan existing mods…"; ``edition_label`` is added to the window title;
        ``current_profile`` says which MO2 profile is in use (the "List history" page starts on it)."""

        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Modlist Companion" + (f" ({edition_label})" if edition_label else ""))
        self.setObjectName("fomodCompanion")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)  # lets the stylesheet's fade show
        # A size up from MO2's small default, so the history reads comfortably.
        font = self.font()
        font.setPointSizeF(max(10.0, font.pointSizeF() + 1))
        self.setFont(font)
        self._database = Path(database)
        self._load_order = load_order
        self._on_error = on_error
        self._on_changed = on_changed
        self._geometry_file = geometry_file
        # The MO2 instance this window runs in and the managed game ({"name", "nexus_domain"}), when the
        # plugin can say; export and import use them.
        self._instance_id = instance_id
        self._game = game
        # Which of this instance's profiles have each mod enabled (core/compare_choices.read_profiles), when the
        # plugin can say. ``_profile_serial`` changes when what it reports does, so the list redraws.
        self._profiles = profiles
        self._current_profile = current_profile
        self._page = "installers"
        self._can_import = can_import
        self._scanner = scanner
        self._scan_dialog: ScanDialog | None = None
        self._scan_hint_snoozed = False      # "Not now": no hint again in this MO2 session
        self._edition_label = edition_label
        self.remove_imported_action: QAction | None = None
        self.scan_action: QAction | None = None
        self._profile_data: dict[str, Any] | None = None
        self._profile_serial = 0
        self._settings = self._read_settings()
        # MO2's own palette, kept so "Match MO2" can return to it.
        self._mo2_palette = QPalette(self.palette())
        # A saved choice wins; with none (or a damaged one) the window wears the default, sayngwyn.
        saved_mode = self._settings.get("theme")
        self._theme_mode: str = saved_mode if saved_mode in theme.MODES else theme.DEFAULT_MODE
        self._set_theme(self._theme_mode)
        self.state = model.ViewState()
        self.snapshot = model.Snapshot()
        self._expanded: set[str] = set()
        self._show_all: set[str] = set()
        self._modal = 0
        self._refresh_pending = False
        self._drawn_signature: tuple | None = None
        self._drawn_instances: list | None = None
        # What the last load read, to skip a reload when none of it has changed.
        self._serial = 0
        self._loaded_key: tuple | None = None
        self._drawn_side: tuple | None = None
        self._limits: dict[str, int] = {}      # cards shown so far in each side-panel list
        self._open_folds: set[str] = set()
        self._change_cache: tuple[Any, dict[str, dict[str, Any]]] = (None, {})
        # New-patch alerts: the cards flagged by "Fix" in MO2 (alert settled, still shown until the window closes),
        # the alerts whose sections were already opened for the user, and the cards drawn for them.
        self._flagged: dict[tuple[str, str], str] = {}
        self._flag_serial = 0
        self._alerts_opened: set[tuple] = set()
        self._alert_cards: list[QWidget] = []
        self._arrows: dict[str, QToolButton] = {}
        self._persist_folds = True
        self._origin_dialog: FileOriginDialog | None = None
        self._compare_dialog: CompareDialog | None = None
        self._origin_cache = OriginCache()   # the files of every install, indexed once per state of the database

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(_REFRESH_DELAY_MS)
        self._refresh_timer.timeout.connect(self.refresh)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._apply_search)

        self._build()
        self._restore_geometry()

    # ---------- layout ----------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 16)
        root.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(_named(QLabel("Modlist Companion"), "title"))
        header.addWidget(self._page_switch())
        self.summary_label = _named(_plain(""), "chip")
        self.missed_chip = _named(_plain(""), "chipAlert")
        self.masters_chip = _named(_plain(""), "chipMasters")
        self.off_chip = _named(_plain(""), "chipOff")
        self.revisit_chip = _named(_plain(""), "chipMark")
        # The chips wrap onto more rows when the window is narrow.
        chips = QWidget()
        flow = FlowLayout(chips)
        for chip in (self.summary_label, self.missed_chip, self.masters_chip, self.off_chip, self.revisit_chip):
            flow.addWidget(chip)
        header.addWidget(chips, 1)
        # The "List history" page has its own chip; only one of the two sets is shown.
        self.installer_chips = chips
        self.history_chip = _named(_plain(""), "chip")
        self.history_chips = QWidget()
        history_flow = FlowLayout(self.history_chips)
        history_flow.addWidget(self.history_chip)
        self.history_chips.hide()
        header.addWidget(self.history_chips, 1)
        header.addWidget(self._theme_button())
        header.addWidget(self._more_button())
        root.addLayout(header)

        # Two pages under the header: the installers (the tree and its side cards) and the list's change history.
        self.pages = QStackedWidget()
        self.installers_page = QWidget()
        page_layout = QVBoxLayout(self.installers_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = _named(QLineEdit(), "search")
        use_themed_menu(self.search)
        self._tint_placeholder()
        self.search.setPlaceholderText("Search mods, pages, options and files…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        bar.addWidget(self.search, 1)
        self.choice_buttons = QButtonGroup(self)
        segment = QHBoxLayout()
        segment.setSpacing(0)
        choices = (("all", "All options"), ("picked", "Picked"), ("skipped", "Skipped"))
        for index, (value, label) in enumerate(choices):
            edge = "first" if index == 0 else "last" if index == len(choices) - 1 else "middle"
            button = _named(QToolButton(), "segment", edge=edge)
            button.setText(label)
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setProperty("choices", value)
            self.choice_buttons.addButton(button)
            segment.addWidget(button)
        bar.addLayout(segment)
        self.choice_buttons.buttonClicked.connect(self._choices_changed)
        self.sort = _named(QComboBox(), "tool")
        for value, label in (("newest", "Newest first"), ("oldest", "Oldest first"), ("name", "By name")):
            self.sort.addItem(label, value)
        self.sort.currentIndexChanged.connect(self._sort_changed)
        bar.addWidget(self.sort)
        self.toggle_all = _named(QPushButton("Expand all"), "tool")
        self.toggle_all.clicked.connect(self._toggle_all)
        bar.addWidget(self.toggle_all)
        page_layout.addLayout(bar)

        # A quiet line (not a dialog) for mods the window has no record of yet; it comes and goes with the scan.
        self.scan_hint = QWidget()
        hint_row = QHBoxLayout(self.scan_hint)
        hint_row.setContentsMargins(2, 0, 2, 0)
        hint_row.setSpacing(8)
        self.scan_hint_label = _named(_plain("", wrap=True), "hint")
        hint_row.addWidget(self.scan_hint_label, 1)
        self.scan_hint_scan = _named(QPushButton("Scan now"), "action", primary=True)  # the one primary action of the hint
        self.scan_hint_scan.clicked.connect(lambda _checked=False: self.scan_existing())
        self.scan_hint_later = _named(QPushButton("Not now"), "action")
        self.scan_hint_later.setToolTip("Hide this until MO2 is started again")
        self.scan_hint_later.clicked.connect(lambda _checked=False: self._dismiss_scan_hint(forever=False))
        self.scan_hint_never = _named(QPushButton("Don't show again"), "action")
        self.scan_hint_never.clicked.connect(lambda _checked=False: self._dismiss_scan_hint(forever=True))
        hint_row.addWidget(self.scan_hint_scan)
        hint_row.addWidget(self.scan_hint_later)
        hint_row.addWidget(self.scan_hint_never)
        self.scan_hint.hide()
        page_layout.addWidget(self.scan_hint)

        self.error_label = _named(_plain(""), "error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        page_layout.addWidget(self.error_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(10)
        self.tree = _named(QTreeWidget(), "installs")
        self.tree.setItemDelegate(TintDelegate(self.tree))   # search-match tints under a styled row
        self.tree.setColumnCount(3)
        self.tree.setHeaderHidden(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setIndentation(18)
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        # Names take the room; status and dates fit their text.
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        # Qt sizes a fit-to-contents column from the first 1,000 rows only; a long list has wider text further down.
        header.setResizeContentsPrecision(_FIT_ROWS)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemExpanded.connect(lambda item: self._track_expanded(item, True))
        self.tree.itemCollapsed.connect(lambda item: self._track_expanded(item, False))
        viewport = self.tree.viewport()
        assert viewport is not None  # a QTreeWidget always has one
        self.empty_state = _EmptyState(viewport)
        splitter.addWidget(self.tree)

        side = _named(QScrollArea(), "side")
        side.setWidgetResizable(True)
        side.setFrameShape(QFrame.Shape.NoFrame)
        # Everything in the panel wraps to its width, so it only ever scrolls downwards.
        side.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side.setMinimumWidth(_SIDE_MIN_WIDTH)
        holder = _named(QWidget(), "sideHolder")
        self.side = QVBoxLayout(holder)
        self.side.setContentsMargins(0, 0, 4, 0)
        self.side.setSpacing(10)
        self.missed_box, self.missed_list, self.missed_count = self._section(
            "Possible missed patches", "Options you skipped that patch mods you now use.", "missed")
        self.masters_box, self.masters_list, self.masters_count = self._section(
            "Missing masters", "Installed, but a plugin can't load: a master it needs isn't installed.", "masters")
        self.off_box, self.off_list, self.off_count = self._section(
            "Present but plugin disabled", "Installed, but a plugin it needs or adds is switched off.", "off")
        self.changes_box, self.changes_list, self.changes_count = self._section(
            "Changed installers", "Mods you updated or reinstalled whose installer now offers something different.",
            "changes")
        self.review_box, self.review_list, self.review_count = self._section("To revisit", "", "revisit")
        self.cross_box, self.cross_list, self.cross_count = self._section(
            "Cross-patches", "Options that several of your installers offer.", "cross")
        self.problem_box, self.problem_list, _count = self._section(
            "Problems", "These event files couldn't be read.", "missed", key="problems")
        self.side.addStretch(1)
        side.setWidget(holder)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)
        self.splitter = splitter
        self.side_area = side
        page_layout.addWidget(splitter, 1)
        self.pages.addWidget(self.installers_page)
        self.history = HistoryView(self)
        self.pages.addWidget(self.history)
        root.addWidget(self.pages, 1)

    def _page_switch(self) -> QWidget:
        """The segment that switches between the installers and the list's history."""

        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.page_buttons = QButtonGroup(self)
        self.page_button: dict[str, QToolButton] = {}
        pages = (("installers", "Installers"), ("history", "List history"))
        for index, (value, label) in enumerate(pages):
            button = _named(QToolButton(), "segment", edge="first" if index == 0 else "last")
            button.setText(label)
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setToolTip("Every FOMOD and plain install, with what you picked" if value == "installers" else
                              "What changed in your list, and when: installs, mods and plugins switched or moved, "
                              "profiles, programs started through MO2")
            button.clicked.connect(lambda _checked=False, value=value: self.show_page(value))
            self.page_buttons.addButton(button)
            self.page_button[value] = button
            row.addWidget(button)
        return holder

    def show_page(self, page: str) -> None:
        """Switch between the installers ("installers") and the list's change history ("history")."""

        if page not in ("installers", "history"):
            return
        self._page = page
        for value, button in self.page_button.items():
            button.setChecked(value == page)
        self.installer_chips.setVisible(page == "installers")
        self.history_chips.setVisible(page == "history")
        self.pages.setCurrentWidget(self.history if page == "history" else self.installers_page)
        self.refresh()

    @property
    def page(self) -> str:
        return self._page

    def _theme_button(self) -> QToolButton:
        button = _named(QToolButton(), "tool")
        button.setToolTip("The sayngwyn colours, a light or dark window, or match MO2's theme")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(button)
        self._theme_actions = {}
        for mode in theme.MODES:
            action = menu.addAction(theme.MODE_LABELS[mode])
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, value=mode: self.set_theme_mode(value))
            self._theme_actions[mode] = action
        button.setMenu(menu)
        self.theme_button = button
        self._update_theme_button()
        return button

    def _update_theme_button(self) -> None:
        self.theme_button.setText(f"Theme: {theme.MODE_LABELS[self._theme_mode]}  ▾")
        for mode, action in self._theme_actions.items():
            action.setChecked(mode == self._theme_mode)

    def _set_theme(self, mode: str) -> None:
        """Apply a theme mode's palette, colours and stylesheet (no redraw). sayngwyn, Light and Dark style every
        widget themselves; Match MO2 keeps MO2's palette and leaves the standard controls to MO2's own theme."""

        if mode == "auto":      # MO2's palette as it is now: MO2 may have changed theme since the window was made
            app = QApplication.instance()
            if isinstance(app, QApplication):
                self._mo2_palette = QPalette(QApplication.palette(self))
        palette = theme.palette_for(mode, self._mo2_palette)
        self.setPalette(palette)
        self._tokens = theme.tokens(self.palette(), mode)
        self._colours = _colours(self._tokens)
        self.setStyleSheet(theme.stylesheet(self._tokens, match_mo2=mode == "auto"))
        # Qt keeps an old sheet's rules, and the palette they set, on widgets that haven't been shown yet, and polishes
        # them for real only at the first show or grab, which would bring the old palette back then. So: polish now
        # (ensurePolished marks it done), polish the window and everything in it again, then set the palette again
        # (polishing rebuilds it from the sheet).
        self.ensurePolished()
        style = self.style()
        for widget in [self, *self.findChildren(QWidget)]:
            style.unpolish(widget)
            style.polish(widget)
        self.setPalette(palette)

    def _tint_placeholder(self) -> None:
        # Some themes leave the hint text the same colour as the background.
        # Start from the window's palette so a theme switch reaches the box.
        palette = QPalette(self.palette())
        palette.setColor(QPalette.ColorRole.PlaceholderText, self._tokens.faint)
        self.search.setPalette(palette)

    def set_theme_mode(self, mode: str) -> None:
        """Switch between sayngwyn, following MO2, light and dark; remembered for next time."""

        if mode not in theme.MODES:
            return
        self._theme_mode = mode
        self._set_theme(mode)
        self._tint_placeholder()
        self._update_theme_button()
        self._settings["theme"] = mode
        self._write_settings()
        # Item colours are set when items are drawn, so draw everything again.
        self._drawn_signature = None
        self._drawn_side = None
        self._render()
        if self._compare_dialog is not None:
            self._compare_dialog.retheme()
        self.history.retheme()

    def _more_button(self) -> QToolButton:
        more = _named(QToolButton(), "tool")
        more.setText("More  ▾")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        self.history_action = QAction("Show every install (history)", menu)
        self.history_action.setCheckable(True)
        self.history_action.toggled.connect(self._view_changed)
        menu.addAction(self.history_action)
        self.hidden_action = QAction("Show hidden mods", menu)
        self.hidden_action.setCheckable(True)
        self.hidden_action.toggled.connect(self._hidden_changed)
        menu.addAction(self.hidden_action)
        self.instance_menu = menu.addMenu("MO2 instance")
        menu.addSeparator()
        menu.addAction("Which option installed…?", self.which_option)
        menu.addAction("Compare choices…", self.compare_choices)
        if self._scanner is not None:
            self.scan_action = menu.addAction("Scan existing mods…", self.scan_existing)
        menu.addSeparator()
        menu.addAction("Export install guide…", self.export_install_guide)
        menu.addAction("Export choices file…", self.export_choices_file)
        menu.addAction("Export history…", self.export_history)
        menu.addAction("Export list history…", self._export_list_history)
        menu.addSeparator()
        if self._can_import:
            menu.addAction("Import choices file…", self.import_choices_file)
            self.remove_imported_action = menu.addAction("Remove imported choices", self.remove_imported_choices)
            menu.aboutToShow.connect(self._update_import_actions)
            menu.addSeparator()
        menu.addAction("Refresh", lambda: self.refresh(force=True))
        more.setMenu(menu)
        return more

    def _section(self, title: str, hint: str, kind: str, key: str = "") -> tuple[QFrame, QVBoxLayout, QLabel]:
        """A side card: a heading that folds the card away (remembered), its count and its items."""

        key = key or kind
        frame = _named(QFrame(), "section", kind=kind)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        heading.setSpacing(6)
        # Qt draws the arrow itself: symbol glyphs are missing from some MO2 theme fonts.
        arrow = _named(QToolButton(), "sectionFold")
        arrow.setAutoRaise(True)
        arrow.setCheckable(True)
        arrow.setToolTip(f"Fold or unfold “{title}”")
        arrow.setAccessibleName(f"Fold {title}")
        heading.addWidget(arrow, 0, Qt.AlignmentFlag.AlignTop)
        name = _named(_ClickLabel(title, arrow.toggle), "sectionTitle")
        name.setWordWrap(True)
        name.setCursor(Qt.CursorShape.PointingHandCursor)
        heading.addWidget(name, 1)
        count = _named(_plain(""), "chipAlert" if kind == "missed" else "chipMark" if kind == "revisit" else "chipOff" if kind == "off" else "chipMasters" if kind == "masters" else "chip")
        count.hide()
        heading.addWidget(count, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)
        body = QWidget()
        inner = QVBoxLayout(body)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(6)
        if hint:
            note = _named(QLabel(hint), "hint")
            note.setWordWrap(True)
            inner.addWidget(note)
        items = QVBoxLayout()
        items.setSpacing(0)
        inner.addLayout(items)
        layout.addWidget(body)

        def folded(closed: bool) -> None:
            arrow.setArrowType(Qt.ArrowType.RightArrow if closed else Qt.ArrowType.DownArrow)
            body.setVisible(not closed)
            saved = set(self._settings.get("folded_sections") or [])
            if self._persist_folds and (key in saved) != closed:
                (saved.add if closed else saved.discard)(key)
                self._settings["folded_sections"] = sorted(saved)
                self._write_settings()

        arrow.toggled.connect(folded)
        self._arrows[key] = arrow
        closed = key in (self._settings.get("folded_sections") or [])
        arrow.setChecked(closed)
        folded(closed)
        self.side.addWidget(frame)
        return frame, items, count

    @staticmethod
    def _set_count(label: QLabel, count: int) -> None:
        label.setText(str(count))
        label.setVisible(count > 0)

    # ---------- data ----------

    def schedule_refresh(self) -> None:
        """Called by the plugin when anything changed; coalesces bursts."""

        if self.isVisible():
            self._refresh_timer.start()

    def refresh(self, force: bool = False) -> None:
        if self._modal:
            # A dialog or menu is open: redrawing now could delete the widget
            # whose click opened it. Catch up once it closes.
            self._refresh_pending = True
            return
        if self._page == "history":
            self.history.reload(force)
            return
        if self._load(force):
            self._report_error()
            if self._origin_dialog is not None and self._origin_dialog.isVisible():
                self._origin_dialog.update_results()
            if self._compare_dialog is not None and self._compare_dialog.isVisible():
                self._compare_dialog.reload()
        self._render()

    def _state_key(self) -> tuple:
        # Everything that decides what is read; which choices the list shows is decided afterwards.
        return tuple(value for field, value in zip(dataclasses.fields(self.state), dataclasses.astuple(self.state))
                     if field.name != "choices")

    def _database_stamp(self) -> tuple[tuple, float]:
        """(size and modified time of the database and its journal, newest modified time)."""

        stamp, newest = [], 0.0
        for suffix in ("", "-wal"):
            try:
                found = os.stat(f"{self._database}{suffix}")
            except OSError:
                stamp.append(None)
                continue
            stamp.append((found.st_size, found.st_mtime_ns))
            newest = max(newest, found.st_mtime)
        return tuple(stamp), newest

    def _origin_stamp(self) -> tuple | None:
        """What says the database is unchanged since the file index was built; None while it was written to a
        moment ago, when a repeat write might not show in the stamp."""

        stamp, newest = self._database_stamp()
        return stamp if time.time() - newest > _SETTLED_SECONDS else None

    def _load(self, force: bool = False) -> bool:
        """Read the database and MO2's list again, unless neither has changed since the last read.
        Returns whether a new snapshot was read."""

        try:
            order = self._load_order() if self._load_order is not None else None
            readable = True
        except Exception:
            order, readable = None, False  # model.load reports it
        stamp, newest = self._database_stamp()
        self._read_profiles()
        key = (stamp, self._state_key(), order, self._profile_serial) if readable else None
        if not force and key is not None and key == self._loaded_key and not self.snapshot.error:
            return False
        self.snapshot = model.load(self._database, self.state, (lambda: order) if readable else self._load_order,
                                   (self._origin_cache, self._origin_stamp()))
        self._serial += 1
        # A file written to a moment ago might be written again within the clock's tick, so it
        # isn't trusted to show a change until it has been still for a while.
        settled = time.time() - newest > _SETTLED_SECONDS
        self._loaded_key = ((stamp, self._state_key(), copy.deepcopy(order), self._profile_serial)
                            if readable and settled and not self.snapshot.error else None)
        return True

    def _read_profiles(self) -> None:
        """Ask the plugin which profiles have which mods enabled; a change makes the list redraw."""

        if self._profiles is None:
            return
        try:
            found = self._profiles()
        except Exception:
            found = None    # no profile information; nothing in the list claims anything about profiles
        if found != self._profile_data:
            self._profile_data = copy.deepcopy(found)
            self._profile_serial += 1

    def _report_error(self) -> None:
        if self.snapshot.error and self._on_error is not None:
            self._on_error(f"Modlist Companion window: {self.snapshot.error}")

    @contextmanager
    def _modal_scope(self):
        self._modal += 1
        try:
            yield
        finally:
            self._modal -= 1
            if not self._modal and self._refresh_pending:
                self._refresh_pending = False
                QTimer.singleShot(0, self.refresh)

    def _write(self, action: Callable[[EventStore], Any], failure: str) -> Any:
        try:
            with EventStore(self._database) as store:
                result = action(store)
        except Exception as error:
            with self._modal_scope():
                QMessageBox.warning(self, "Modlist Companion", f"{failure}: {error}")
            return None
        if self._on_changed is not None:
            self._on_changed()
        self.refresh(force=True)
        return result

    # ---------- filters ----------

    def _apply_search(self) -> None:
        self._show_all.clear()
        self.state.query = self.search.text()
        if self._load():
            self._report_error()
        # A short list of results opens so the matches are visible at once.
        if self.state.query.strip() and len(self.snapshot.events) <= _AUTO_OPEN_LIMIT:
            self._expanded.update(event["event_id"] for event in self.snapshot.events)
        self._render()

    def _choices_changed(self, button: QToolButton) -> None:
        self.state.choices = button.property("choices")
        self._render()

    def _sort_changed(self) -> None:
        self.state.sort = self.sort.currentData()
        self.snapshot.events = model.sort_events(self.snapshot.events, self.state.sort)
        self._render()

    def _view_changed(self, history: bool) -> None:
        self.state.view = "history" if history else "current"
        self.refresh()

    def _hidden_changed(self, include: bool) -> None:
        self.state.include_hidden = include
        self.refresh()

    def _instance_chosen(self, instance_id: str) -> None:
        self.state.instance = instance_id
        # The menu entry that fired is still emitting; redraw after it returns.
        QTimer.singleShot(0, self.refresh)

    # ---------- drawing ----------

    def _render(self) -> None:
        self.error_label.setVisible(bool(self.snapshot.error))
        self.error_label.setText(f"Couldn't read the Modlist Companion database ({self.snapshot.error})."
                                 if self.snapshot.error else "")
        count, extra, alert = model.summary(self.snapshot, self.state)
        self.summary_label.setText(" · ".join([count, *(part for part in extra if "revisit" not in part)]))
        self.missed_chip.setText(alert)
        self.missed_chip.setVisible(bool(alert))
        revisit = next((part for part in extra if "revisit" in part), "")
        self.revisit_chip.setText(revisit)
        self.revisit_chip.setVisible(bool(revisit))
        self._render_instances()
        # Plugin and mod toggles change only the side panel; skip the list
        # when nothing it shows has changed.
        signature = self._tree_signature()
        if signature != self._drawn_signature:
            self._render_tree()
        self._render_side()

    def _render_side(self, force: bool = False) -> None:
        # The panel shows only what the snapshot holds, so a repeat of the same snapshot draws nothing.
        key = (self._serial, self.state.view, self.state.instance, self.state.include_hidden)
        if not force and key == self._drawn_side:
            return
        self._drawn_side = key
        self._render_missed()
        self._render_changes()
        self._render_reviews()
        self._render_cross_patches()
        self._render_problems()

    def _tree_signature(self) -> tuple:
        return (
            tuple((e["event_id"], e["mod"]["name"], bool(e.get("hidden")), e.get("removed_at"))
                  for e in self.snapshot.events),
            tuple(sorted((r["event_id"], r["option_id"], r["state"], (r.get("latest_event") or {}).get("event_id"),
                          r.get("latest_option_id")) for r in self.snapshot.reviews)),
            self.state.query, self.state.choices, len(self.snapshot.instances), tuple(sorted(self._show_all)),
            tuple(sorted(self.snapshot.file_hits)),
            tuple(sorted(self._missed_keys().items())),
            tuple(sorted(self._alerted())),
            self._profile_serial,
            tuple(sorted((c["event_id"], len(c["removed"]),
                          tuple((i["option_id"], bool(i["patch"])) for i in c["added"]),
                          tuple((i["option_id"], bool(i["patch"])) for i in c["renamed"]),
                          tuple(i["option_id"] for i in c["picked"]),
                          tuple(i["option_id"] for i in c["unpicked"]))
                         for c in self.snapshot.compared)),
        )

    def _missed_keys(self) -> dict[tuple[str, str], str]:
        """(install, option) -> "missing", "patch-disabled" or "needs-disabled"."""

        return {(item["event_id"], item["option_id"]): item.get("state", "missing") for item in self.snapshot.missed}

    def _change_lookup(self, event_id: str) -> dict[str, Any] | None:
        """What the tree marks for a mod's newest install: new and renamed options by id, the
        removed ones by the (page, group) they were in, and the options picked or unpicked since
        the install before. None for any other install."""

        if self._change_cache[0] is not self.snapshot.compared:
            built = {}
            for change in self.snapshot.compared:
                removed: dict[tuple[str, str], list[dict[str, Any]]] = {}
                for item in change["removed"]:
                    removed.setdefault((item["step_name"], item["group_name"]), []).append(item)
                built[change["event_id"]] = {
                    "change": change,
                    "added": {item["option_id"]: item for item in change["added"]},
                    "renamed": {item["option_id"]: item for item in change["renamed"]},
                    "removed": removed,
                    "picked": {item["option_id"]: item for item in change["picked"]},
                    "unpicked": {item["option_id"]: item for item in change["unpicked"]},
                }
            self._change_cache = (self.snapshot.compared, built)
        return self._change_cache[1].get(event_id)

    def _render_instances(self) -> None:
        instances = [(i["id"], i["label"] or i["id"]) for i in self.snapshot.instances]
        if instances != self._drawn_instances:
            self._drawn_instances = instances
            self.instance_menu.clear()
            for instance_id, label in [("", "All instances")] + instances:
                action = self.instance_menu.addAction(label.replace("&", "&&"))
                action.setCheckable(True)
                action.setData(instance_id)
                action.triggered.connect(lambda _checked=False, value=instance_id: self._instance_chosen(value))
        self.instance_menu.menuAction().setVisible(len(instances) > 1)
        for action in self.instance_menu.actions():
            action.setChecked(action.data() == self.state.instance)

    def _render_tree(self) -> None:
        self._drawn_signature = self._tree_signature()
        scroll = self.tree.verticalScrollBar().value()
        self.tree.setUpdatesEnabled(False)
        self.tree.blockSignals(True)
        self.tree.clear()
        query = self.state.query
        missed = self._missed_keys()
        here = self._this_instance() if self._profile_data and self.state.view == "current" else ""
        for event in self.snapshot.events:
            item = QTreeWidgetItem(self.tree)
            item.setData(0, _ROLE, (_EVENT, event["event_id"], ""))
            item.setText(0, event["mod"]["name"] or "Unnamed mod")
            # Status, most important first: what needs attention, then facts.
            status, colour = [], "faint"
            states = [state for key, state in missed.items() if key[0] == event["event_id"]]
            misses = states.count("missing")
            broken = states.count("missing-masters")
            switched_off = len(states) - misses - broken
            if misses:
                status.append("⚠ " + model.plural(misses, "missed patch", "missed patches"))
                colour = "missed"
            if broken:
                status.append("● " + model.plural(broken, "plugin can't load", "plugins can't load"))
                colour = colour if misses else "masters"
            if switched_off:
                status.append("● " + model.plural(switched_off, "plugin", "plugins") + " switched off")
                colour = colour if misses or broken else "off"
            if model.pending_review(self.snapshot.reviews, event["event_id"], ""):
                status.append("Reminder set")
                colour = "missed" if misses else "mark"
            plain = model.is_plain(event)
            if plain:
                # Quiet: nothing to pick, so nothing asks for attention; the archive says where it came from.
                status.append("no installer options")
                if event["source"].get("archive"):
                    status.append(_short(event["source"]["archive"], _ARCHIVE_TAIL))
            note = self._change_lookup(event["event_id"])
            if note:
                if note["change"]["changed"]:
                    added = len(note["change"]["added"])
                    status.append(f"+{added} new" if added else "installer changed")
                picks = model.picks_text(note["change"])
                if picks:
                    status.append(picks)
                colour = "accent" if colour == "faint" else colour
            hits = model.match_count(event, query, self.snapshot.file_hits)
            if hits:
                status.append(model.plural(hits, "match", "matches"))
            for flag, word in ((event["source"].get("reinstallation"), "Reinstall"),
                               (event.get("hidden"), "Hidden"), (event.get("removed_at"), "Removed")):
                if flag:
                    status.append(word)
            # Which of this instance's profiles have the mod enabled: the tooltip says, and a mod that
            # none has gets a faint word on its row.
            in_profiles = (compare_choices.profiles_of(self._profile_data, event["mod"]["name"])
                           if here and event["instance"]["id"] == here and not event.get("removed_at") else None)
            if in_profiles is not None and not in_profiles:
                status.append("in no profile")
            item.setText(1, "  ·  ".join(status))
            item.setForeground(1, QBrush(self._colours[colour]))
            item.setText(2, " · ".join(part for part in (event["mod"].get("version"),
                                                          model.format_date(event["completed_at"])) if part))
            item.setForeground(2, QBrush(self._colours["faint"]))
            details = [f"Installed {model.format_date(event['completed_at'])}"]
            if event["source"].get("archive"):
                details.append(event["source"]["archive"])
            if plain:
                count = event["plain"].get("file_count", 0)
                details.append("Not installed through a logged installer: there are no installer options to show.")
                if event["mod"].get("nexus_id", 0) > 0:
                    details.append(f"Nexus ID {event['mod']['nexus_id']}")
                details.append(f"{count:,}{'+' if event['plain'].get('files_truncated') else ''} "
                               f"file{'' if count == 1 else 's'} recorded")
            if note and note["change"]["picks_changed"]:
                details.append("Picks changed since the previous install: "
                               + model.picks_text(note["change"]) + ".")
            if len(self.snapshot.instances) > 1:
                details.append(event["instance"].get("label", ""))
            if in_profiles is not None:
                details.append("Enabled in profiles: " + ", ".join(in_profiles) if in_profiles
                               else "Not enabled in any profile.")
            item.setToolTip(0, "\n".join(details))
            bold = item.font(0)
            bold.setBold(True)
            item.setFont(0, bold)
            if event.get("hidden") or event.get("removed_at"):
                item.setForeground(0, QBrush(self._colours["faint"]))
            # Choices are built when an install is first opened, so a long
            # history stays quick to draw.
            if event["event_id"] in self._expanded:
                self._populate(item, event)
                item.setExpanded(True)
            else:
                QTreeWidgetItem(item, ["…"]).setData(0, _ROLE, (_PLACEHOLDER, "", ""))
        branded = False
        if not self.snapshot.events:
            first_run = False
            if query.strip():
                message = f"Nothing matches “{query.strip()}”. Try a shorter word, or turn on Show hidden mods under More."
            elif self.state.view == "current" and not self.state.include_hidden:
                first_run = True
                message = "No mods logged yet. " + _FIRST_RUN_GUIDE
            else:
                message = "Nothing to show."
            # The sayngwyn theme gives the first run its branded message; otherwise (or without the font) a plain row.
            if first_run and self._theme_mode == "sayngwyn":
                branded = self.empty_state.present("No mods logged yet.", _FIRST_RUN_GUIDE)
            if not branded:
                QTreeWidgetItem(self.tree, [message])
        if not branded:
            self.empty_state.hide()
        self.tree.blockSignals(False)
        self.tree.setUpdatesEnabled(True)
        self.tree.verticalScrollBar().setValue(scroll)
        self._update_toggle_label()

    def _populate(self, item: QTreeWidgetItem, event: dict[str, Any]) -> None:
        """Fill an install's choices (replacing the placeholder)."""

        item.takeChildren()
        if model.is_plain(event):
            self._populate_plain(item, event)
            return
        searching = bool(self.state.query.strip()) and event["event_id"] not in self._show_all
        shown = self._add_choices(item, event, only_matches=searching)
        automatic = self._add_automatic(item, event)
        if searching and not shown and not automatic:
            shown = self._add_choices(item, event, only_matches=False)
            searching = False
        if not shown and not automatic:
            message = ("Nothing was picked in this installer." if self.state.choices == "picked"
                       else "No options to show with this filter.")
            empty = QTreeWidgetItem(item, [message])
            empty.setForeground(0, QBrush(self._colours["faint"]))
        total = sum(len(group["options"]) for step in event["steps"] for group in step["groups"])
        if searching and shown < total:
            more = QTreeWidgetItem(item, ["Show all options in this installer"])
            more.setData(0, _ROLE, (_SHOW_ALL, event["event_id"], ""))
            more.setForeground(0, QBrush(self._colours["mark"]))
            more.setToolTip(0, "Click to show every option, not only the matches")

    def _populate_plain(self, item: QTreeWidgetItem, event: dict[str, Any]) -> None:
        """What is known about a mod that was not installed through a logged installer: where it came from, its
        plugins and, under "Files (N)", the files in its folder."""

        faint = QBrush(self._colours["faint"])
        plain = event["plain"]
        note = QTreeWidgetItem(item, ["Not installed through a logged installer, so there are no options to show."])
        note.setForeground(0, faint)
        note.setToolTip(0, "Companion saw this mod's folder, not an installer's choices: the plugins and files below "
                           "are what it found. The version and archive are on the row.")
        for plugin in plain.get("plugins", [])[:20]:
            masters = plugin.get("masters")
            row = QTreeWidgetItem(item, [f"Plugin: {plugin['name']}"])
            row.setText(1, ("needs " + ", ".join(masters)) if masters else "")
            row.setForeground(0, faint)
            row.setForeground(1, faint)
        count = plain.get("file_count", 0)
        if count:
            more = "+" if plain.get("files_truncated") else ""
            holder = QTreeWidgetItem(item, [f"Files ({count:,}{more})"])
            holder.setData(0, _FILES_ROLE, (event["event_id"], ""))
            holder.setForeground(0, faint)
            holder.setToolTip(0, "The files in this mod's folder." + (" The list was cut short." if more else ""))
            QTreeWidgetItem(holder, ["…"]).setData(0, _ROLE, (_PLACEHOLDER, "", ""))
            if (event["event_id"], PLAIN_OPTION) in self.snapshot.file_hits:
                self._fill_files(holder)         # a file search found this mod: show the files
                holder.setExpanded(True)

    def _add_choices(self, parent: QTreeWidgetItem, event: dict[str, Any], *, only_matches: bool) -> int:
        shown = 0
        query = self.state.query
        note = self._change_lookup(event["event_id"])
        listed: set[tuple[str, str]] = set()
        for step in event["steps"]:
            step_item = None
            for group in step["groups"]:
                options = [
                    option for option in group["options"]
                    if model.accepts(option, self.state.choices)
                    and (not only_matches
                         or model.option_matches(event, step, group, option, query, self.snapshot.file_hits))
                ]
                if not options:
                    continue
                if step_item is None:
                    step_item = QTreeWidgetItem(parent)
                    hidden_page = step.get("visible") is False
                    step_item.setText(0, (step.get("name") or "Page") + (" · page skipped by the installer" if hidden_page else ""))
                    step_item.setForeground(0, QBrush(self._colours["faint"]))
                    step_item.setExpanded(not hidden_page or only_matches)
                group_item = QTreeWidgetItem(step_item, [group.get("name") or "Options"])
                bold = group_item.font(0)
                bold.setBold(True)
                group_item.setFont(0, bold)
                group_item.setExpanded(True)
                for option in options:
                    self._option_item(group_item, event, step, group, option)
                shown += len(options)
                key = (step.get("name", ""), group.get("name", ""))
                if note and key not in listed:
                    listed.add(key)
                    for gone in note["removed"].get(key, ()):
                        if model.accepts(gone, self.state.choices) and not only_matches:
                            self._removed_item(group_item, note["change"], gone)
        if note and not only_matches and self.state.choices == "all":
            # Removed from a page or group that no longer exists: listed once at the end.
            orphans = [gone for key, items in note["removed"].items() if key not in listed for gone in items]
            if orphans:
                holder = QTreeWidgetItem(parent, ["No longer in the installer"])
                holder.setForeground(0, QBrush(self._colours["faint"]))
                holder.setExpanded(True)
                for gone in orphans:
                    self._removed_item(holder, note["change"], gone, where=True)
        return shown

    def _add_automatic(self, parent: QTreeWidgetItem, event: dict[str, Any]) -> int:
        """The automatic installs of this install that install the file searched for (none unless a path is searched)."""

        found = [match for (event_id, _option), match in self.snapshot.file_hits.items()
                 if event_id == event["event_id"] and match["automatic"]]
        if not found:
            return 0
        holder = QTreeWidgetItem(parent, ["Installed automatically by the installer"])
        holder.setForeground(0, QBrush(self._colours["faint"]))
        holder.setExpanded(True)
        for match in found:
            row = QTreeWidgetItem(holder, [f"{'✓' if match['picked'] else '○'}  {match['option']}",
                                           self._origin_status(match)])
            row.setForeground(0, QBrush(self._colours["picked" if match["picked"] else "faint"]))
            row.setForeground(1, QBrush(self._colours["picked" if match["picked"] else "faint"]))
            row.setBackground(0, QBrush(self._colours["hit"]))
            row.setToolTip(0, f"{match['text']}\n{match['detail']}")
        return len(found)

    def _origin_status(self, match: dict[str, Any]) -> str:
        """"installs Textures/Landscape/Peak.dds", shortened from the left, and whether it wins when picked options
        write the same path."""

        path = match["path"] or "the mod folder"
        if match.get("plain"):
            return "has " + (path if len(path) <= _PATH_TAIL else "…" + path[-(_PATH_TAIL - 1):])
        if match["folder"] or match["how"] == "folder":
            path = (path + "/…") if match["path"] else path
        if len(path) > _PATH_TAIL:
            path = "…" + path[-(_PATH_TAIL - 1):]
        text = f"installs {path}"
        if match["contested"]:
            text += " · wins" if match["winner"] else " · overwritten"
        return text

    def _removed_item(self, parent: QTreeWidgetItem, change: dict[str, Any], gone: dict[str, Any], *,
                      where: bool = False) -> None:
        """A faint row for an option the previous install had and this one doesn't: nothing to click."""

        label = gone.get("name") or "Unnamed option"
        if where:
            label = " › ".join(part for part in (gone.get("step_name"), gone.get("group_name"), label) if part)
        item = QTreeWidgetItem(parent, [f"–  {label}", "removed in this version"])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        for column in (0, 1):
            item.setForeground(column, QBrush(self._colours["faint"]))
        was = f" (version {change['old_version']})" if change.get("old_version") else ""
        item.setToolTip(0, f"This option was in the previous install{was} and is not offered any more."
                           + (" You had picked it." if gone.get("selected") else ""))

    def _option_item(self, parent: QTreeWidgetItem, event: dict[str, Any], step: dict[str, Any],
                     group: dict[str, Any], option: dict[str, Any]) -> None:
        picked = option.get("selected") is True
        unavailable = option.get("enabled") is False or step.get("visible") is False
        item = QTreeWidgetItem(parent, [f"{'✓' if picked else '○'}  {option.get('name') or 'Unnamed option'}"])
        item.setData(0, _ROLE, (_OPTION, event["event_id"], option["id"]))
        status, colour = [], "faint"
        missed_state = self._missed_keys().get((event["event_id"], option["id"]))
        if missed_state == "missing":
            status.append("⚠ possible missed patch")
            colour = "missed"
            item.setForeground(0, QBrush(self._colours["missed"]))
        elif missed_state == "missing-masters":
            status.append("● can't load: missing master")
            colour = "masters"
            item.setForeground(0, QBrush(self._colours["masters"]))
        elif missed_state:
            status.append("● plugin switched off" if missed_state == "picked-disabled" else "● present but disabled")
            colour = "off"
            item.setForeground(0, QBrush(self._colours["off"]))
        elif picked:
            item.setForeground(0, QBrush(self._colours["picked"]))
        elif unavailable:
            item.setForeground(0, QBrush(self._colours["faint"]))
        note = self._change_lookup(event["event_id"])
        if note:
            added, renamed = note["added"].get(option["id"]), note["renamed"].get(option["id"])
            if added or renamed:
                if added:
                    status.insert(0, "+ new patch" if added.get("patch") else "+ new")
                else:
                    was = renamed["old_name"] if len(renamed["old_name"]) <= 28 else renamed["old_name"][:27] + "…"
                    status.insert(0, f"~ was “{was}”" + ("  ·  + new patch" if renamed.get("patch") else ""))
                    item.setToolTip(0, f"Called “{renamed['old_name']}” in the previous install.")
                colour = "accent" if colour == "faint" else colour
            # What you did differently from the install before: green for a new pick, the accent for a dropped one.
            newly, dropped = note["picked"].get(option["id"]), note["unpicked"].get(option["id"])
            if newly or dropped:
                status.insert(1 if added or renamed else 0, "newly picked" if newly else "was picked")
                tip = ("You didn't pick this in the previous install." if newly
                       else "You picked this in the previous install; you didn't this time.")
                item.setToolTip(0, f"{item.toolTip(0)}\n{tip}" if item.toolTip(0) else tip)
                colour = ("picked" if newly else "accent") if colour == "faint" else colour
        if (event["event_id"], option["id"]) in self._alerted():
            status.insert(0, "just installed")   # the mod this patch is made for
        if option.get("enabled") is False and step.get("visible") is not False:
            status.append("picked (marked unavailable)" if picked else "not available")
        if model.pending_review(self.snapshot.reviews, event["event_id"], option["id"]):
            status.append("Reminder set")
            colour = colour if colour in ("missed", "off", "masters") else "mark"
        origin = self.snapshot.file_hits.get((event["event_id"], option["id"]))
        if origin:
            status.insert(0, self._origin_status(origin))
            tip = f"{origin['text']}\n{origin['detail']}"
            item.setToolTip(0, f"{item.toolTip(0)}\n{tip}" if item.toolTip(0) else tip)
            colour = "picked" if origin["picked"] and colour == "faint" else colour
        item.setText(1, "  ·  ".join(status))
        item.setForeground(1, QBrush(self._colours[colour]))
        if model.option_matches(event, step, group, option, self.state.query, self.snapshot.file_hits):
            item.setBackground(0, QBrush(self._colours["hit"]))
        files = option.get("files")
        if isinstance(files, list) and files:
            # What the installer's XML says this option installs; the rows are made when it is opened.
            more = "+" if option.get("files_truncated") else ""
            holder = QTreeWidgetItem(item, [f"Files ({len(files):,}{more})"])
            holder.setData(0, _FILES_ROLE, (event["event_id"], option["id"]))
            holder.setForeground(0, QBrush(self._colours["faint"]))
            holder.setToolTip(0, "What this option installs, as the installer defines it." + (
                " The list was cut short." if more else ""))
            QTreeWidgetItem(holder, ["…"]).setData(0, _ROLE, (_PLACEHOLDER, "", ""))

    def _fill_files(self, holder: QTreeWidgetItem) -> None:
        """Make the rows of an opened "Files (N)" item: each recorded destination, folders as "Folder/…"."""

        first = holder.child(0)
        if first is None or first.data(0, _ROLE) != (_PLACEHOLDER, "", ""):
            return
        event_id, option_id = holder.data(0, _FILES_ROLE)
        if model.is_plain(self._event(event_id)):
            self._fill_plain_files(holder, event_id)
            return
        files: list[dict[str, Any]] = []
        for step in self._event(event_id).get("steps", []):
            for group in step.get("groups", []):
                for option in group.get("options", []):
                    if option.get("id") == option_id:
                        files = option.get("files") or []
        holder.takeChildren()
        faint = QBrush(self._colours["faint"])
        for entry in files:
            folder = entry.get("kind") == "folder"
            resolved = entry.get("resolved", "")
            if entry.get("escapes"):
                text = f"(outside the mod folder) {entry.get('destination', '')}"
            else:
                text = (resolved + "/…") if folder and resolved else "(the mod folder)/…" if folder else resolved
            row = QTreeWidgetItem(holder, [text])
            notes = [part for part in ("folder" if folder else "",
                                       f"priority {entry['priority']}" if entry.get("priority") else "") if part]
            row.setText(1, " · ".join(notes))
            row.setForeground(1, faint)
            row.setToolTip(0, f"From {entry.get('source', '')} in the archive")

    def _fill_plain_files(self, holder: QTreeWidgetItem, event_id: str) -> None:
        """The rows of an opened "Files (N)" under a mod without installer options: the files a search found first,
        then the rest, up to ``_PLAIN_ROWS``."""

        holder.takeChildren()
        try:
            with EventStore(self._database) as store:
                paths = store.plain_files(event_id)
        except Exception as error:
            QTreeWidgetItem(holder, [f"Couldn't read the files ({error})"])
            return
        if not paths:
            gone = QTreeWidgetItem(holder, ["The list of files of this earlier install was not kept: a mod keeps the lists "
                                            "of its two newest installs."])
            gone.setForeground(0, QBrush(self._colours["faint"]))
            return
        searched = (event_id, PLAIN_OPTION) in self.snapshot.file_hits
        found = plain_path_matches(paths, self.state.query) if searched else set()
        ordered = [path for path in paths if path in found] + [path for path in paths if path not in found]
        faint = QBrush(self._colours["faint"])
        for path in ordered[:_PLAIN_ROWS]:
            row = QTreeWidgetItem(holder, [path])
            row.setForeground(0, QBrush(self._colours["picked"]) if path in found else faint)
            if path in found:
                row.setBackground(0, QBrush(self._colours["hit"]))
        if len(ordered) > _PLAIN_ROWS:
            rest = QTreeWidgetItem(holder, [f"…and {len(ordered) - _PLAIN_ROWS:,} more files. Search for one to find it."])
            rest.setForeground(0, faint)

    def _clear(self, layout: QVBoxLayout) -> None:
        while layout.count():
            child = layout.takeAt(0)
            if child.widget() is not None:
                child.widget().hide()  # gone at once; deleted when Qt is ready
                child.widget().deleteLater()
            elif child.layout() is not None:
                self._clear(child.layout())  # pyright: ignore[reportArgumentType]  # Qt stubs: never None here

    def _card(self, layout: QVBoxLayout, title: str, lines: list[tuple[str, str]],
              actions: list[tuple[str, Callable[[], None]]]) -> QWidget:
        card = _named(QFrame(), "card", first=layout.count() == 0)
        box = QVBoxLayout(card)
        box.setContentsMargins(0, 8, 0, 8)
        box.setSpacing(3)
        box.addWidget(_named(_plain(title, wrap=True), "cardTitle"))
        names = {"": "cardLine", "mark": "cardMark", "missed": "cardAlert", "faint": "hint", "off": "cardOff", "masters": "cardMasters", "picked": "cardPicked", "new": "cardNew"}
        for text, colour in lines:
            box.addWidget(_named(_plain(text, wrap=True), names.get(colour, "cardLine")))
        # The buttons start a new row when the panel is too narrow for them side by side.
        row = FlowLayout(spacing=6, row_spacing=4)
        row.setContentsMargins(0, 4, 0, 0)
        for label, handler in actions:
            button = _named(QPushButton(label), "action")
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, run=handler: QTimer.singleShot(0, run))
            row.addWidget(button)
        box.addLayout(row)
        layout.addWidget(card)
        return card

    def _fill(self, layout: QVBoxLayout, key: str, items: list, build: Callable[[QVBoxLayout, Any], None]) -> None:
        """Build the first few cards of a list, then a button for the rest, so a long list
        costs a bounded number of widgets however many entries it has."""

        shown = self._limits.get(key, _CARD_LIMIT)
        for item in items[:shown]:
            build(layout, item)
        rest = len(items) - shown
        if rest > 0:
            step = min(rest, _CARD_STEP)
            self._hint(layout, f"Showing {shown} of {len(items)}.")
            more = _named(QPushButton(f"Show {step} more"), "action")
            more.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            more.setCursor(Qt.CursorShape.PointingHandCursor)
            more.clicked.connect(lambda _checked=False: QTimer.singleShot(0, lambda: self._show_more(key, step)))
            holder = QHBoxLayout()
            holder.setContentsMargins(0, 6, 0, 0)
            holder.addWidget(more)
            holder.addStretch(1)
            layout.addLayout(holder)

    def _show_more(self, key: str, step: int) -> None:
        self._limits[key] = self._limits.get(key, _CARD_LIMIT) + step
        self._render_side(force=True)

    def _fold(self, layout: QVBoxLayout, key: str, text: str, items: list,
              build: Callable[[QVBoxLayout, Any], None]) -> None:
        """A closed group under a button. Its cards are built when it is first opened."""

        fold = _WrapButton(text, "fold")
        fold.setCheckable(True)
        fold.setAutoRaise(True)
        holder = QWidget()
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(12, 0, 0, 0)
        built = []

        def toggled(opened: bool) -> None:
            (self._open_folds.add if opened else self._open_folds.discard)(key)
            if opened and not built:
                built.append(True)
                self._fill(inner, key, items, build)
            holder.setVisible(opened)
            holder.updateGeometry()  # the cards were added after the layout last measured it

        holder.setVisible(False)
        fold.toggled.connect(toggled)
        layout.addWidget(fold)
        layout.addWidget(holder)
        if key in self._open_folds:
            fold.setChecked(True)  # kept open across a redraw

    def _hint(self, layout: QVBoxLayout, text: str) -> None:
        layout.addWidget(_named(_plain(text, wrap=True), "hint"))

    def _render_missed(self) -> None:
        self._alert_cards = []
        self._clear(self.missed_list)
        self._clear(self.off_list)
        self._clear(self.masters_list)
        missing = [item for item in self.snapshot.missed if item.get("state", "missing") == "missing"]
        broken = [item for item in self.snapshot.missed if item.get("state") == "missing-masters"]
        switched_off = [item for item in self.snapshot.missed
                        if item.get("state", "missing") not in ("missing", "missing-masters")]
        # A card for a mod just installed goes first in its list, so it is never behind "Show more".
        alerted = self._alerted()

        def first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return sorted(items, key=lambda item: (item["event_id"], item["option_id"]) not in alerted)

        missing, broken, switched_off = first(missing), first(broken), first(switched_off)
        self.masters_box.setVisible(bool(broken))
        self._set_count(self.masters_count, len(broken))
        self.masters_chip.setText(model.plural(len(broken), "plugin can't load", "plugins can't load"))
        self.masters_chip.setVisible(bool(broken))
        self._fill(self.masters_list, "masters", broken, self._missed_card)
        self.missed_box.setVisible(self.snapshot.missed_available and bool(missing or self.snapshot.without_evidence))
        self.off_box.setVisible(bool(switched_off))
        self._set_count(self.missed_count, len(missing))
        self._set_count(self.off_count, len(switched_off))
        self.off_chip.setText(model.plural(len(switched_off), "plugin switched off", "plugins switched off"))
        self.off_chip.setVisible(bool(switched_off))
        self._fill(self.missed_list, "missed", missing, self._missed_card)
        self._fill(self.off_list, "off", switched_off, self._missed_card)
        if self.snapshot.missed_available and not missing:
            self._hint(self.missed_list, "Nothing found among mods logged with patch details.")
        if self.snapshot.without_evidence:
            self._hint(self.missed_list, f"{model.plural(self.snapshot.without_evidence, 'mod was', 'mods were')} "
                       "logged before patch checking existed. Reinstall to include them.")
        self._open_for_alerts()

    # ---------- new-patch alerts ----------

    def _alerted(self) -> dict[tuple[str, str], str]:
        """(install, option) -> the mod just installed, for the patches that mod made worth a look: the pending
        alerts, and the ones flagged by "Fix" in MO2 until the window is closed."""

        mods: dict[tuple[str, str], list[str]] = {}
        for alert in self.snapshot.alerts:   # a patch made for two mods that were installed together names both
            mods.setdefault((alert["event_id"], alert["option_id"]), []).append(alert["mod_name"])
        found = {key: join_names(names) for key, names in mods.items()}
        found.update(self._flagged)
        return found

    def highlight_alerts(self, patches: list[tuple[str, str, str]]) -> None:
        """Mark these cards as new, open their sections and scroll to the first. ``patches`` are
        (install id, option id, mod just installed). The plugin calls this from "Fix" in MO2, after it has
        settled the alert, so the marks stay until the window is closed."""

        self._flag_serial += 1
        self._flagged = {(event_id, option_id): mod for event_id, option_id, mod in patches}
        self.refresh(force=True)

    def _open_for_alerts(self) -> None:
        """Unfold the sections that hold a card for an alert, once per alert: not on later redraws, so a section
        the user folds again stays folded. The fold is not remembered, so the section is folded again the next time
        the window opens with no alert."""

        wanted = self._alerted()
        if not wanted:
            return
        identities = {(a["mod_key"], a["event_id"], a["option_id"], a["created_at"]) for a in self.snapshot.alerts
                      if (a["event_id"], a["option_id"]) in wanted}
        identities |= {("flag", event_id, option_id, self._flag_serial) for event_id, option_id in self._flagged}
        fresh = identities - self._alerts_opened
        if not fresh:
            return
        self._alerts_opened |= fresh
        sections = set()
        for item in self.snapshot.missed:
            if (item["event_id"], item["option_id"]) in wanted:
                state = item.get("state", "missing")
                sections.add("missed" if state == "missing" else "masters" if state == "missing-masters" else "off")
        self._persist_folds = False
        try:
            for key in sections:
                if self._arrows[key].isChecked():
                    self._arrows[key].setChecked(False)
        finally:
            self._persist_folds = True
        QTimer.singleShot(0, self._reveal_alerts)

    def _reveal_alerts(self) -> None:
        cards = [card for card in self._alert_cards if card.parent() is not None]
        if cards:
            self.side_area.ensureWidgetVisible(cards[0], 0, 40)

    @staticmethod
    def _why(item: dict[str, Any]) -> tuple[str, str]:
        """One plain sentence on why this is listed, and one on what to do."""

        needs = ", ".join(item["needs"])
        off = item.get("disabled", [])

        def where(entry: dict[str, str]) -> str:
            return ("is unchecked in your plugin list" if entry["where"].startswith("unchecked")
                    else f"is in {entry['where'].replace('its mod', 'the mod').replace(' is disabled', ', which is disabled')}")

        if item.get("state") == "missing-masters":
            entry = next(entry for entry in off if entry.get("missing"))
            missing = entry["missing"]
            needs = (f"it needs {missing[0]}, which isn't in your list" if len(missing) == 1
                     else f"it needs {len(missing)} masters that aren't in your list: {', '.join(missing)}")
            if item.get("kind") == "plain":
                return (f"{entry['plugin']} is switched on, but {needs}, so the game won't start with it like this.",
                        "Install what it needs if you use those mods; otherwise switch the plugin off.")
            if entry.get("on"):
                return (f"You picked this and {entry['plugin']} is switched on, but {needs}, "
                        "so the game won't start with it like this.",
                        "Install what it needs if you use those mods; otherwise switch it off "
                        "or reinstall this mod without this option.")
            return (f"You picked this and {entry['plugin']} is installed, but {needs}, so it can't be switched on.",
                    "Install what it needs if you use those mods; otherwise reinstall this mod without this option.")
        if item.get("state") == "picked-disabled":
            names = " and ".join(f"{entry['plugin']} {where(entry)}" for entry in off)
            return f"You picked this and it's installed, but {names}.", "Switch it on in the plugin list to use it."
        if item.get("state") == "patch-disabled":
            entry = off[0]
            return (f"You already have this patch ({entry['plugin']}), but it {where(entry)}.",
                    "Switch it on to use it.")
        if item.get("state") == "needs-disabled":
            parts = "; ".join(f"{entry['plugin']} {where(entry)}" for entry in off)
            return (f"It's made for a mod you have but have switched off: {parts}.",
                    "Switch that on, then reinstall this mod and pick the patch.")
        if item.get("automatic"):
            return (f"The installer adds this automatically when {needs} is in use, and it is now.",
                    "Reinstall the mod to get it.")
        if item["kind"] == "detects":
            return f"The installer looks for {needs}, which you use, but this was skipped.", ""
        if item["kind"] == "masters":
            return f"It's made for {needs}, which you use, but this was skipped.", ""
        return f"Its name mentions {needs}, which you use. (A guess from the name.)", ""

    def _missed_card(self, layout: QVBoxLayout, item: dict[str, Any]) -> None:
        reason, advice = self._why(item)
        where = " › ".join(part for part in (item["mod_name"], item.get("group_name") or item.get("step_name")) if part)
        lines = [(where, "faint"), (reason, "")]
        new_mod = self._alerted().get((item["event_id"], item["option_id"]))
        if new_mod:
            lines.insert(0, (f"New: you just installed {new_mod}", "new"))
        if advice:
            lines.append((advice, {"missing": "", "missing-masters": "masters"}.get(item.get("state", "missing"), "off")))
        elif not item.get("available"):
            lines.append(("Reinstall the mod to get it; it couldn't be picked when you installed.", "mark"))
        reminder = model.pending_review(self.snapshot.reviews, item["event_id"], item["option_id"])
        if reminder:
            lines.append(("Reminder set", "mark"))
        actions: list[tuple[str, Callable[[], Any]]] = [("Show", lambda i=item: self.navigate(i["event_id"], i["option_id"]))] \
            if self._listed(item["event_id"]) else []
        if reminder:
            actions.append(("Edit reminder", lambda r=reminder: self.edit_reminder(r["original_event"], r["option_id"])))
        else:
            actions.append(("Remind me", lambda i=item: self.edit_reminder(self._event(i["event_id"]), i["option_id"])))
        actions.append(("Not needed", lambda i=item, r=reminder: self._dismiss_missed(i, r)))
        card = self._card(layout, item["label"], lines, actions)
        if new_mod:
            self._alert_cards.append(card)

    def _changes_card(self, layout: QVBoxLayout, change: dict[str, Any], *, seen: bool = False) -> None:
        lines = [(model.changes_line(change), "")]
        patches = change["patches"]
        lines.extend(model.patch_line(item) for item in patches[:_CHANGE_PATCH_LINES])
        if len(patches) > _CHANGE_PATCH_LINES:
            lines.append((f"and {model.plural(len(patches) - _CHANGE_PATCH_LINES, 'more new patch option')}", "faint"))
        if change["steps_added"]:
            lines.append((f"New {'page' if len(change['steps_added']) == 1 else 'pages'}: "
                          + ", ".join(change["steps_added"]), "faint"))
        picks = len(change["picked"]) + len(change["unpicked"])
        if picks:
            lines.append((f"You also changed {model.plural(picks, 'pick')} since the last install.", "faint"))
        actions: list[tuple[str, Callable[[], Any]]] = [("Show", lambda c=change: self.navigate(c["event_id"]))] if self._listed(change["event_id"]) else []
        actions.append(("Reopen" if seen else "Seen", lambda c=change, s=seen: self.set_changes_seen(c["event_id"], not s)))
        self._card(layout, change["mod_name"], lines, actions)

    def _render_changes(self) -> None:
        self._clear(self.changes_list)
        changes = self.snapshot.changes
        seen = [c for c in changes if c["event_id"] in self.snapshot.changes_seen]
        unseen = [c for c in changes if c["event_id"] not in self.snapshot.changes_seen]

        def rank(change: dict[str, Any]) -> int:
            # New patches you skipped first, then other new patches, then the rest (newest first within each).
            patches = [item["patch"] for item in change["patches"]]
            missed = any(not p["picked"] and not p["have"] and p["state"] == "missing" for p in patches)
            return 0 if missed else 1 if patches else 2

        unseen.sort(key=rank)
        self.changes_box.setVisible(bool(changes))
        self._set_count(self.changes_count, len(unseen))
        if not unseen and changes:
            self._hint(self.changes_list, "Nothing new to check.")
        self._fill(self.changes_list, "changes", unseen, self._changes_card)
        if seen:
            self._fold(self.changes_list, "changes-seen", f"Seen ({len(seen)})", seen,
                       lambda layout, change: self._changes_card(layout, change, seen=True))

    def _review_card(self, layout: QVBoxLayout, review: dict[str, Any]) -> None:
        event = review["original_event"]
        label = model.option_label(event, review["option_id"]) if review["option_id"] else "Whole installer"
        lines = [(label, "")] if label else []
        if review.get("reason"):
            lines.append((review["reason"], ""))
        latest = review.get("latest_event") or {}
        if latest.get("removed_at"):
            lines.append(("Mod removed since this reminder.", "mark"))
        elif review.get("newer_record"):
            changed = "choices changed" if review.get("choices_changed") else "same choices"
            lines.append((f"Reinstalled {model.format_date(latest.get('completed_at'))}: {changed}.", "mark"))
        target = latest.get("event_id") if review.get("newer_record") else review["event_id"]
        target_option = model.latest_option_id(review) if review.get("newer_record") else review["option_id"]
        actions: list[tuple[str, Callable[[], Any]]] = [("Show", lambda: self.navigate(target, target_option))] if self._listed(target) else []  # pyright: ignore[reportArgumentType]  # _listed() is false for no target
        if review["state"] == "pending":
            actions.append(("Done", lambda r=review: self._set_review_state(r, "resolved")))
        else:
            actions.append(("Reopen", lambda r=review: self._set_review_state(r, "pending")))
        actions.append(("Edit", lambda r=review: self.edit_reminder(r["original_event"], r["option_id"])))
        self._card(layout, event["mod"]["name"], lines, actions)

    def _render_reviews(self) -> None:
        self._clear(self.review_list)
        reviews = [r for r in self.snapshot.reviews
                   if not self.state.instance or r["original_event"]["instance"]["id"] == self.state.instance]
        pending = [r for r in reviews if r["state"] == "pending"]
        closed = [r for r in reviews if r["state"] != "pending"]
        self._set_count(self.review_count, len(pending))
        if not pending:
            self._hint(self.review_list, "Nothing yet. Right-click any option and choose “Remind me” to come back to it.")
        self._fill(self.review_list, "reviews", pending, self._review_card)
        if closed:
            self._fold(self.review_list, "reviews-closed", f"Done or dismissed ({len(closed)})", closed, self._review_card)

    def _render_cross_patches(self) -> None:
        self._clear(self.cross_list)
        if self.state.view != "current":
            self._hint(self.cross_list, "Turn off “Show every install” under More to compare.")
            return
        patches = self.snapshot.cross_patches
        if not patches:
            self._hint(self.cross_list, "None yet. They appear when two or more installers offer the same option.")
            return
        mixed = [p for p in patches if model.classify(p) == "mixed"]
        quiet = [p for p in patches if model.classify(p) != "mixed"]
        self._set_count(self.cross_count, len(mixed))
        if not mixed:
            self._hint(self.cross_list, "Your shared options are consistent: nothing picked in one installer but skipped in another.")
        self._fill(self.cross_list, "cross", mixed, self._cross_block)
        if quiet:
            self._fold(self.cross_list, "cross-quiet", f"Picked everywhere or nowhere ({len(quiet)})", quiet, self._cross_block)

    def _cross_block(self, layout: QVBoxLayout, patch: dict[str, Any]) -> None:
        heading = _named(_plain(patch["option"], wrap=True), "cardTitle")
        heading.setContentsMargins(0, 8, 0, 2)
        layout.addWidget(heading)
        for mod in sorted(patch["mods"], key=lambda m: bool(m.get("selected"))):
            missed = not mod.get("selected") and mod.get("review_state") == "selected_elsewhere"
            mark = "✓" if mod.get("selected") else "✗" if missed else "○"
            tone = "picked" if mod.get("selected") else "missed" if missed else "faint"
            button = _WrapButton(f"{mark}  {mod['name']}" + ("   — skipped here" if missed else ""), "crossMod", tone=tone)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            option_id = (mod.get("occurrences") or [{}])[0].get("option_id", "")
            button.clicked.connect(lambda _checked=False, e=mod["event_id"], o=option_id:
                                   QTimer.singleShot(0, lambda: self.navigate(e, o)))
            layout.addWidget(button)

    def _render_problems(self) -> None:
        self._clear(self.problem_list)
        self.problem_box.setVisible(bool(self.snapshot.problems))
        for problem in self.snapshot.problems:
            self._hint(self.problem_list, f"{problem.get('path') or 'Unknown file'}: {problem.get('error') or 'unknown error'}")

    # ---------- actions ----------

    def _event(self, event_id: str) -> dict[str, Any]:
        for event in self.snapshot.events:
            if event["event_id"] == event_id:
                return event
        try:
            with EventStore(self._database) as store:
                found = store.event(event_id)
        except Exception:
            found = None
        return found or {"event_id": event_id, "mod": {"name": "Mod"}, "steps": [], "source": {}, "instance": {}}

    def _items(self) -> list[QTreeWidgetItem]:
        found = []

        def walk(item: QTreeWidgetItem) -> None:
            found.append(item)
            for index in range(item.childCount()):
                walk(item.child(index))  # pyright: ignore[reportArgumentType]  # Qt stubs: never None here

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index))  # pyright: ignore[reportArgumentType]  # Qt stubs: never None here
        return found

    def _listed(self, event_id: str | None) -> bool:
        return any(event["event_id"] == event_id for event in self.snapshot.events)

    def _find(self, kind: str, event_id: str, option_id: str = "") -> QTreeWidgetItem | None:
        wanted = (kind, event_id, option_id)
        return next((item for item in self._items() if item.data(0, _ROLE) == wanted), None)

    def navigate(self, event_id: str, option_id: str = "") -> bool:
        """Open the install and select the option; the install if the option is filtered out."""

        if not self._listed(event_id):
            return False
        self._expanded.add(event_id)
        if option_id and self.state.query.strip():
            self._show_all.add(event_id)
        if self._tree_signature() != self._drawn_signature or self._find(_EVENT, event_id) is None:
            self._render_tree()
        event_item = self._find(_EVENT, event_id)
        if event_item is None:
            return False
        if event_item.child(0) is not None and event_item.child(0).data(0, _ROLE) == (_PLACEHOLDER, "", ""):
            self._populate(event_item, self._event(event_id))
        event_item.setExpanded(True)
        target = (self._find(_OPTION, event_id, option_id) if option_id else None) or event_item
        parent = target.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        self.tree.setCurrentItem(target)
        self.tree.scrollToItem(target, QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    def edit_reminder(self, event: dict[str, Any], option_id: str) -> None:
        existing = next((r for r in self.snapshot.reviews
                         if r["event_id"] == event["event_id"] and r["option_id"] == option_id), None)
        target = f"{event['mod']['name']} · {model.option_label(event, option_id) if option_id else 'whole installer'}"
        dialog = ReminderDialog(self, target, existing)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)  # blocks this window, not MO2
        with self._modal_scope():
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            action, values = dialog.action, dialog.values()
        dialog.deleteLater()
        if accepted:
            self.apply_reminder(event["event_id"], option_id, action, *values)

    def apply_reminder(self, event_id: str, option_id: str, action: str, note: str = "", state: str = "pending") -> None:
        if action == "remove":
            self._write(lambda store: store.delete_review({"event_id": event_id, "option_id": option_id}),
                        "Couldn't remove the reminder")
        elif action == "save":
            self._write(lambda store: store.save_review(
                {"event_id": event_id, "option_id": option_id, "reason": note, "state": state}),
                "Couldn't save the reminder")

    def _set_review_state(self, review: dict[str, Any], state: str) -> None:
        self.apply_reminder(review["event_id"], review["option_id"], "save", review.get("reason", ""), state)

    def _dismiss_missed(self, item: dict[str, Any], reminder: dict[str, Any] | None) -> None:
        # Settle the existing reminder, keeping its note, rather than adding another.
        if reminder is not None:
            self._set_review_state(reminder, "dismissed")
        else:
            self.apply_reminder(item["event_id"], item["option_id"], "save",
                                "Not needed (from possible missed patches)", "dismissed")

    def set_changes_seen(self, event_id: str, seen: bool = True) -> None:
        """Settle a changed-installer card (or bring it back)."""

        self._write(lambda store: store.set_changes_seen(event_id, seen), "Couldn't save that")

    def set_hidden(self, event_id: str, hidden: bool) -> None:
        self._write(lambda store: store.set_hidden(event_id, hidden), "Couldn't change visibility")

    def export_history(self) -> None:
        with self._modal_scope():
            path, _filter = QFileDialog.getSaveFileName(self, "Export Modlist Companion history",
                                                        "modlist-companion-history-v2.json", "JSON (*.json)")
        if not path:
            return
        try:
            with EventStore(self._database) as store:
                data = store.full_history_export()
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        except Exception as error:
            with self._modal_scope():
                QMessageBox.warning(self, "Modlist Companion", f"Couldn't export: {error}")

    def _export_list_history(self) -> None:
        self.history.export()

    # ---------- which option installed a file ----------

    def _origin_lookup(self, query: str) -> dict[str, Any]:
        """Which options install a path or plugin, among each mod's current install in the instance on show."""

        stamp = self._origin_stamp()     # before the database is opened: opening it makes its journal file appear
        with EventStore(self._database) as store:
            store.use_origin_cache(self._origin_cache, stamp)
            return store.file_origins(query, current=True, instance_id=self.state.instance or self._this_instance())

    def _show_origin(self, event_id: str, option_id: str) -> bool:
        shown = self.navigate(event_id, option_id)
        if shown:
            self.raise_()
            self.activateWindow()
        return shown

    def which_option(self, query: str = "") -> FileOriginDialog:
        """Open "Which option installed…?", optionally with something already typed."""

        dialog = self._origin_dialog
        if dialog is None:
            dialog = self._origin_dialog = FileOriginDialog(self, self._origin_lookup, self._show_origin)
        dialog.set_query(query or dialog.edit.text())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.edit.setFocus()
        return dialog

    # ---------- scan existing mods ----------

    def scan_existing(self, *, show: bool = True) -> ScanDialog | None:
        """The More menu's "Scan existing mods…": record the mods that have no record here yet, in the background
        (the FOMOD Plus edition also rebuilds the installs FOMOD Plus recorded). Returns the dialog (None when there
        is nothing to scan)."""

        if self._scanner is None:
            return None
        dialog = self._scan_dialog
        if dialog is not None and dialog.running:
            dialog.show()
            dialog.raise_()
            return dialog
        try:
            count = self._scanner.count()
        except Exception as error:
            self._tell("warning", f"Couldn't look for mods to scan: {error}")
            return None
        if not count:
            self._tell("information", "Every mod in your list already has a record here.")
            self._update_scan_hint()
            return None
        if dialog is None:
            dialog = self._scan_dialog = ScanDialog(self, self._scanner, self._scan_finished,
                                                    plus=bool(self._edition_label))
        if show:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
        dialog.begin()
        return dialog

    def _scan_finished(self, _summary: Any) -> None:
        self.refresh(force=True)
        self._update_scan_hint()

    def _update_scan_hint(self) -> None:
        """Show the line about unrecorded mods when there are some (and it wasn't dismissed); hide it otherwise. The
        count is the plugin's cheap one: a name check per mod, no files read."""

        count = 0
        if self._scanner is not None and not self._settings.get("scan_hint_dismissed") and not self._scan_hint_snoozed:
            try:
                count = self._scanner.count()
            except Exception:
                count = 0       # the plugin can't say right now; the More menu still has the scan
        self.scan_hint.setVisible(bool(count))
        if count:
            self.scan_hint_label.setText(
                f"{count:,} mod{' is' if count == 1 else 's are'} in your list but not recorded here yet. Scanning adds "
                "their versions, archives, files and plugins to the search, the file lookup, compare and the "
                "missing-master check (More > Scan existing mods…).")

    def _dismiss_scan_hint(self, *, forever: bool) -> None:
        """"Not now" hides the line until MO2 is started again; "Don't show again" for good (remembered)."""

        if forever:
            self._settings["scan_hint_dismissed"] = True
            self._write_settings()
        else:
            self._scan_hint_snoozed = True
        self.scan_hint.hide()

    # ---------- compare choices ----------

    def _plain_files_of(self, event_id: str) -> list[str]:
        with EventStore(self._database) as store:
            return store.plain_files(event_id)

    def _compare_source(self) -> dict[str, Any]:
        """What the compare dialog works on: every install, the instances, and this instance. (The file lists of
        mods without installer options are read when the dialog needs them.)"""

        with EventStore(self._database) as store:
            return {"events": store.history("", include_hidden=True), "instances": store.instances(),
                    "this_instance": self._this_instance(), "include_hidden": self.state.include_hidden,
                    "files": self._plain_files_of}

    def compare_choices(self, kind: str = "", a: str | None = None, b: str | None = None, *,
                        show: bool = True) -> CompareDialog:
        """Open "Compare choices", optionally with a comparison already picked (see ``CompareDialog.choose``).
        ``show`` False only builds the dialog (the gallery lays it out without showing it)."""

        dialog = self._compare_dialog
        if dialog is None:
            dialog = self._compare_dialog = CompareDialog(self, self._compare_source, lambda: self._colours)
        else:
            dialog.reload()
        if kind:
            dialog.choose(kind, a, b)
        if show:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
        return dialog

    # ---------- export and import of choices ----------

    def _this_instance(self) -> str:
        """The MO2 instance this window runs in ("" while that isn't known yet)."""

        try:
            found = self._instance_id() if self._instance_id is not None else ""
            if not found and self._load_order is not None:
                found = (self._load_order() or {}).get("instance_id", "")
        except Exception:
            found = ""
        if found:
            return found
        known = [entry["id"] for entry in self.snapshot.instances]
        return known[0] if len(known) == 1 else ""

    def _tell(self, kind: str, text: str) -> None:
        with self._modal_scope():
            getattr(QMessageBox, kind)(self, "Modlist Companion", text)

    def _choices_to_export(self) -> dict[str, Any]:
        """The choices file for the newest install of every mod still installed in the instance on show (hidden
        mods only while "Show hidden mods" is on)."""

        with EventStore(self._database) as store:
            events = export_choices.current_events(
                store, instance_id=self.state.instance or self._this_instance(), include_hidden=self.state.include_hidden)
        game = (self._game() if self._game is not None else None) or {}
        return export_choices.build_choices_file(
            events, game=game.get("name", ""), nexus_domain=game.get("nexus_domain", ""),
            instance_label=events[0]["instance"]["label"] if events else "")

    def _export(self, what: str, path: str | None, quiet: bool, render: Callable[[dict[str, Any], Path], str], *,
                default_name: str, filters: str, add_suffix: Callable[[str, str], str]) -> int | None:
        """Shared by the exports. ``path`` None asks where to save; ``quiet`` (tests, the live driver) shows no
        message and lets an error through. Returns how many mods were written, or None."""

        try:
            choices = self._choices_to_export()
            if not choices["mods"]:
                if not quiet:
                    self._tell("information", "There are no installed mods with logged FOMOD choices to export yet.")
                return None
            if path is None:
                with self._modal_scope():
                    path, selected = QFileDialog.getSaveFileName(self, f"Export {what}", default_name, filters)
                if not path:
                    return None
                path = add_suffix(path, selected)
            target = Path(path)
            export_choices.write_text(target, render(choices, target))
        except Exception as error:
            if quiet:
                raise
            self._tell("warning", f"Couldn't export: {error}")
            return None
        count = len(choices["mods"])
        if not quiet:
            self._tell("information", f"Saved the {what} for {count} mod{'' if count == 1 else 's'} to {target}.")
        return count

    def export_install_guide(self, path: str | None = None, *, quiet: bool = False) -> int | None:
        """Save a readable install guide: HTML for a .html file, Markdown otherwise."""

        def render(choices: dict[str, Any], target: Path) -> str:
            if target.suffix.lower() in (".html", ".htm"):
                return export_choices.render_html(choices)
            return export_choices.render_markdown(choices)

        def add_suffix(chosen: str, selected: str) -> str:
            if Path(chosen).suffix.lower() in (".md", ".markdown", ".html", ".htm"):
                return chosen
            return chosen + (".html" if selected.startswith("HTML") else ".md")

        return self._export("install guide", path, quiet, render, default_name="modlist-companion-install-guide.md",
                            filters="Markdown (*.md);;HTML page (*.html)", add_suffix=add_suffix)

    def export_choices_file(self, path: str | None = None, *, quiet: bool = False) -> int | None:
        """Save every choice as a choices file that another PC or MO2 instance can import."""

        return self._export("choices file", path, quiet, lambda choices, _target: export_choices.choices_json(choices),
                            default_name="modlist-companion-choices.json", filters="Modlist Companion choices (*.json)",
                            add_suffix=lambda chosen, _selected: chosen if Path(chosen).suffix else chosen + ".json")

    def import_choices_file(self, path: str | None = None, *, quiet: bool = False) -> import_choices.ImportSummary | None:
        """Read a choices file and keep its picks for the installer to offer (see ``core/import_choices``).
        ``path`` None asks which file; ``quiet`` shows no message and lets an error through."""

        if path is None:
            with self._modal_scope():
                path, _filter = QFileDialog.getOpenFileName(
                    self, "Import choices file", "", "Modlist Companion choices (*.json);;All files (*)")
            if not path:
                return None
        try:
            instance = self._this_instance()
            if not instance:
                raise ChoicesFileError("this MO2 instance isn't known yet; try again in a moment")
            with EventStore(self._database) as store:
                summary = import_choices.import_choices_path(
                    path, data_root=self._database.parent, instance_id=instance, store=store,
                    game=(self._game() if self._game is not None else None) or {})
        except Exception as error:
            if quiet:
                raise
            self._tell("warning", f"Couldn't import that file: {error}" + ("." if isinstance(error, ChoicesFileError) else ""))
            return None
        if not quiet:
            self._tell("information", summary.message())
        return summary

    def remove_imported_choices(self, *, quiet: bool = False) -> int | None:
        """Forget every imported choice of this instance (the user's own choices are not touched). Returns how
        many mods' choices were removed, or None when the user said no."""

        instance = self._this_instance()
        count = import_choices.imported_count(self._database.parent, instance) if instance else 0
        if not count:
            if not quiet:
                self._tell("information", "There are no imported choices to remove.")
            return 0
        if not quiet:
            with self._modal_scope():
                answer = QMessageBox.question(
                    self, "Modlist Companion",
                    f"Remove the imported choices for {count} mod{'' if count == 1 else 's'}? Your own previous "
                    "choices are not touched.")
            if answer != QMessageBox.StandardButton.Yes:
                return None
        try:
            removed = import_choices.remove_imported(self._database.parent, instance)
        except Exception as error:
            if quiet:
                raise
            self._tell("warning", f"Couldn't remove the imported choices: {error}")
            return None
        if not quiet:
            self._tell("information", f"Removed the imported choices for {removed} mod{'' if removed == 1 else 's'}.")
        return removed

    def _update_import_actions(self) -> None:
        if self.remove_imported_action is None:
            return
        # Runs every time the menu opens, so it counts files by name instead of reading them.
        instance = self._this_instance()
        count = import_choices.imported_file_count(self._database.parent, instance) if instance else 0
        self.remove_imported_action.setEnabled(bool(count))
        self.remove_imported_action.setToolTip(f"{count} imported" if count else "Nothing imported")

    def _context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        data = item.data(0, _ROLE) if item is not None else None
        if not data:
            return
        kind, event_id, option_id = data
        event = self._event(event_id)
        menu = QMenu(self)
        reminder = model.pending_review(self.snapshot.reviews, event_id, option_id)
        label = "Edit reminder" if reminder else ("Remind me" if kind == _OPTION else "Remind me about this mod")
        if reminder:
            menu.addAction(label, lambda: self.edit_reminder(reminder["original_event"], reminder["option_id"]))
        else:
            menu.addAction(label, lambda: self.edit_reminder(event, option_id))
        if kind == _EVENT:
            menu.addAction("Unhide" if event.get("hidden") else "Hide",
                           lambda: self.set_hidden(event_id, not event.get("hidden")))
            if self._find(_SHOW_ALL, event_id) is not None:
                menu.addAction("Show all options in this installer", lambda: self._show_all_options(event_id))
        with self._modal_scope():
            chosen = menu.exec(self.tree.viewport().mapToGlobal(position))
        menu.deleteLater()
        del chosen  # the chosen action already ran its handler

    def _show_all_options(self, event_id: str) -> None:
        self._show_all.add(event_id)
        self._expanded.add(event_id)
        self._render_tree()
        item = self._find(_EVENT, event_id)
        if item is not None:
            self.tree.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)

    def _double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, _ROLE)
        if data and data[0] == _OPTION:
            # After the click returns: saving redraws the tree, item included.
            QTimer.singleShot(0, lambda: self._remind_option(data[1], data[2]))

    def _remind_option(self, event_id: str, option_id: str) -> None:
        reminder = model.pending_review(self.snapshot.reviews, event_id, option_id)
        if reminder:
            self.edit_reminder(reminder["original_event"], reminder["option_id"])
        else:
            self.edit_reminder(self._event(event_id), option_id)

    def _track_expanded(self, item: QTreeWidgetItem, expanded: bool) -> None:
        if expanded and item.data(0, _FILES_ROLE):
            self._fill_files(item)
            return
        data = item.data(0, _ROLE)
        if data and data[0] == _EVENT:
            (self._expanded.add if expanded else self._expanded.discard)(data[1])
            first = item.child(0)
            if expanded and first is not None and first.data(0, _ROLE) == (_PLACEHOLDER, "", ""):
                self._populate(item, self._event(data[1]))
            self._update_toggle_label()

    def _clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, _ROLE)
        if data and data[0] == _SHOW_ALL:
            QTimer.singleShot(0, lambda: self._show_all_options(data[1]))

    def _toggle_all(self) -> None:
        tops = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        expand = not all(item.isExpanded() for item in tops)
        self.tree.setUpdatesEnabled(False)
        self.tree.blockSignals(True)
        for item in tops:
            data = item.data(0, _ROLE)
            if not data or data[0] != _EVENT:
                continue
            first = item.child(0)
            if expand and first is not None and first.data(0, _ROLE) == (_PLACEHOLDER, "", ""):
                self._populate(item, self._event(data[1]))  # pyright: ignore[reportArgumentType]  # Qt stubs: never None here
            (self._expanded.add if expand else self._expanded.discard)(data[1])
            item.setExpanded(expand)
        self.tree.blockSignals(False)
        self.tree.setUpdatesEnabled(True)
        self._update_toggle_label()

    def _update_toggle_label(self) -> None:
        tops = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        self.toggle_all.setText("Collapse all" if tops and all(item.isExpanded() for item in tops) else "Expand all")

    # ---------- window ----------

    def showEvent(self, event) -> None:  # noqa: N802 - Qt hook
        super().showEvent(event)
        self.refresh()
        # After the window is up: asking the plugin which mods are unrecorded reads every mod.
        QTimer.singleShot(0, self._update_scan_hint)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt hook
        super().resizeEvent(event)
        # The side panel keeps a sensible share of the window: never crushed, never most of it.
        self.side_area.setMaximumWidth(max(_SIDE_MIN_WIDTH, int(self.width() * _SIDE_MAX_SHARE)))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt hook
        self._save_geometry()
        if self._origin_dialog is not None:
            self._origin_dialog.close()
        if self._compare_dialog is not None:
            self._compare_dialog.close()
        if self._scan_dialog is not None:
            self._scan_dialog.stop()
        if self._flagged:
            # The marks from "Fix" in MO2 last until the window is closed; the side panel is drawn again without them.
            self._flagged = {}
            self._drawn_side = None
        super().closeEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt hook
        return QSize(1200, 760)

    def _read_settings(self) -> dict[str, Any]:
        try:
            saved = json.loads(self._geometry_file.read_text(encoding="utf-8")) if self._geometry_file else {}
        except Exception:
            saved = {}  # a missing or damaged file just means the defaults
        return saved if isinstance(saved, dict) else {}

    def _write_settings(self) -> None:
        if self._geometry_file is None:
            return
        try:
            self._geometry_file.write_text(json.dumps(self._settings), encoding="utf-8")
        except OSError:
            pass

    def _restore_geometry(self) -> None:
        try:
            saved = self._settings
            if saved.get("geometry"):
                self.restoreGeometry(QByteArray.fromBase64(saved["geometry"].encode("ascii")))
            if saved.get("splitter"):
                self.splitter.restoreState(QByteArray.fromBase64(saved["splitter"].encode("ascii")))
        except Exception:
            pass  # a missing or damaged file just means the default size

    def _save_geometry(self) -> None:
        self._settings["geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")  # pyright: ignore[reportArgumentType]  # the stub lacks QByteArray's buffer support
        self._settings["splitter"] = bytes(self.splitter.saveState().toBase64()).decode("ascii")  # pyright: ignore[reportArgumentType]  # same
        self._write_settings()
