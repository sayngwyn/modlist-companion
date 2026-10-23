"""The "List history" page of the Modlist Companion window: a timeline of the changes to the list.

Newest day first, each change on a line (a burst such as "12 plugins moved" opens to its items), and the programs MO2
started as bold markers, so that "what changed since it last worked?" is a matter of looking above the last marker.
The page reads the database itself (``core/history_store`` through ``EventStore``), a page of entries at a time, so a
history of 50,000 entries costs no more than a short one: only the newest ``_PAGE`` that match are drawn, and "Show
more" asks for the next ones.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
    QPushButton, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core import history
from ..core.storage import EventStore
from .rows import TintDelegate
from .text_menu import use_themed_menu

if TYPE_CHECKING:
    from .window import LoggerWindow

_PAGE = 250                      # entries drawn at first, and added by each "Show more"
_SEARCH_DELAY_MS = 175
_ENTRY, _DAY, _ITEM, _PLACEHOLDER = "entry", "day", "item", "placeholder"
_ROLE = Qt.ItemDataRole.UserRole
_ALL_PROFILES = "all"
_EXPORT_LIMIT = history.KEEP_ENTRIES


class HistoryView(QWidget):
    def __init__(self, window: "LoggerWindow"):
        super().__init__(window)
        self._window = window
        self.kinds: set[str] = set(history.GROUP_KEYS)
        self.profile: str | None = None              # None: every profile
        self._profile_default = True                 # the profile is still the window's own
        self.since_mode = False                      # "since the last program ran"
        self.since_entry: int | None = None          # or since this entry (a program run picked from the list)
        self.limit = _PAGE
        self.entries: list[dict[str, Any]] = []
        self.total = 0
        self.baseline: dict[str, Any] | None = None   # the program run the "since" filter counts from
        self.has_runs = False
        self._loaded_key: tuple | None = None
        self._drawn_key: tuple | None = None
        self._build()

    # ---------- layout ----------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.search = QLineEdit()
        use_themed_menu(self.search)
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search the history by mod, plugin or program…")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._search_changed)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        top.addWidget(self.search, 1)
        self.profile_box = QComboBox()
        self.profile_box.setObjectName("tool")
        self.profile_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.profile_box.setMinimumContentsLength(16)
        self.profile_box.setToolTip("Show the changes of one profile (the whole instance's changes are always included)")
        self.profile_box.currentIndexChanged.connect(self._profile_chosen)
        top.addWidget(self.profile_box)
        self.since_button = QPushButton("Since the last program ran")
        self.since_button.setObjectName("tool")
        self.since_button.setCheckable(True)
        self.since_button.setToolTip("Only the changes made after the last program MO2 started")
        self.since_button.clicked.connect(self._since_clicked)
        top.addWidget(self.since_button)
        layout.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(6)
        # The kind buttons wrap onto a second row when the window is narrow.
        from .window import FlowLayout
        holder = QWidget()
        flow = FlowLayout(holder, spacing=6, row_spacing=6)
        self.kind_buttons: dict[str, QToolButton] = {}
        for key, label, _kinds in history.GROUPS:
            button = QToolButton()
            button.setObjectName("kindChip")
            button.setText(label)
            button.setCheckable(True)
            button.setChecked(True)
            button.toggled.connect(lambda checked, key=key: self._kind_toggled(key, checked))
            self.kind_buttons[key] = button
            flow.addWidget(button)
        row.addWidget(holder, 1)
        self.export_button = QPushButton("Export history…")
        self.export_button.setObjectName("tool")
        self.export_button.setToolTip("Save what is listed as readable text or Markdown")
        self.export_button.clicked.connect(lambda _checked=False: self.export())
        row.addWidget(self.export_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(row)

        self.summary = QLabel("")
        self.summary.setObjectName("historySummary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.empty = QLabel("")
        self.empty.setObjectName("hint")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.empty.hide()
        layout.addWidget(self.empty)

        self.tree = QTreeWidget()
        self.tree.setItemDelegate(TintDelegate(self.tree))   # search-match tints under a styled row
        self.tree.setObjectName("historyTree")
        self.tree.setColumnCount(3)
        self.tree.setHeaderHidden(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setIndentation(18)
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        self.tree.setUniformRowHeights(False)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setResizeContentsPrecision(1000)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemExpanded.connect(self._expanded)
        layout.addWidget(self.tree, 1)

        self.more_button = QPushButton("Show more")
        self.more_button.setObjectName("action")
        self.more_button.clicked.connect(lambda _checked=False: self._show_more())
        self.more_button.hide()
        layout.addWidget(self.more_button, 0, Qt.AlignmentFlag.AlignLeft)

    # ---------- what is asked for ----------

    def _instance(self) -> str:
        window = self._window
        chosen = window.state.instance or window._this_instance()
        if chosen:
            return chosen
        try:
            with EventStore(window._database) as store:
                known = store.history_instances()
        except Exception:
            return ""
        return known[0] if len(known) == 1 else ""

    def _current_profile(self) -> str:
        window = self._window
        try:
            return window._current_profile() if window._current_profile is not None else ""
        except Exception:
            return ""

    def _effective_profile(self) -> str | None:
        """The profile entries are shown for (None: all): the one picked in the box, or, until one is picked, the profile
        MO2 is using now."""

        if self._profile_default and self.profile is None:
            return self._current_profile() or None
        return self.profile

    def _since(self) -> tuple[str, int] | None:
        if self.since_mode and self.baseline is not None:
            return self.baseline["at"], self.baseline["id"]
        return None

    def _fresh_filters(self) -> None:
        self.limit = _PAGE
        self._loaded_key = None

    def _search_changed(self) -> None:
        self._fresh_filters()
        self.reload()

    def _profile_chosen(self, _index: int) -> None:
        value = self.profile_box.currentData()
        self.profile = None if value in (None, _ALL_PROFILES) else value
        self._profile_default = False
        self._fresh_filters()
        self.reload()

    def _kind_toggled(self, key: str, checked: bool) -> None:
        (self.kinds.add if checked else self.kinds.discard)(key)
        self._fresh_filters()
        self.reload()

    def _since_clicked(self, checked: bool) -> None:
        self.since_mode = checked
        if not checked:
            self.since_entry = None
        self._fresh_filters()
        self.reload()

    def since_run(self, entry_id: int | None) -> None:
        """Show only what changed after one program run (``None``: the last one)."""

        self.since_entry = entry_id
        self.since_mode = True
        self.since_button.setChecked(True)
        self._fresh_filters()
        self.reload()

    def _show_more(self) -> None:
        self.limit += _PAGE
        self.reload()

    # ---------- reading ----------

    def reload(self, force: bool = False) -> bool:
        """Read the entries again unless nothing they depend on has changed. Returns whether it read."""

        window = self._window
        stamp, _newest = window._database_stamp()
        instance = self._instance()
        profile = self._effective_profile()
        key = (stamp, instance, profile, tuple(sorted(self.kinds)), self.search.text().strip(), self.since_mode,
               self.since_entry, self.limit, window._profile_serial)
        if not force and key == self._loaded_key:
            return False
        error = ""
        entries: list[dict[str, Any]] = []
        total, runs, names, baseline = 0, False, [], None
        try:
            with EventStore(window._database) as store:
                if instance:
                    names = store.history_profiles(instance)
                    scope = {} if profile is None else {"profile": profile}
                    latest = store.history_latest_run(instance, **scope)
                    runs = latest is not None
                    if self.since_mode:
                        baseline = (self._entry_of(store, self.since_entry) or latest) if runs else None
                    since = (baseline["at"], baseline["id"]) if baseline is not None else None
                    kinds = history.kinds_of(self.kinds)
                    search = self.search.text()
                    entries = store.history_page(instance, profile=profile, kinds=kinds, search=search, since=since,
                                                 limit=self.limit)
                    total = (len(entries) if len(entries) < self.limit else
                             store.history_count(instance, profile=profile, kinds=kinds, search=search, since=since))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        self._loaded_key = key
        self.entries, self.total, self.baseline, self.has_runs = entries, total, baseline, runs
        self._instance_shown, self._profiles_known, self._profile_shown = instance, names, profile
        self._error = error
        self._render()
        return True

    @staticmethod
    def _entry_of(store: EventStore, entry_id: int | None) -> dict[str, Any] | None:
        if entry_id is None:
            return None
        from ..core import history_store
        return history_store.entry(store._connection, entry_id)

    # ---------- drawing ----------

    def _render_profiles(self) -> None:
        profiles = list(self._profiles_known)
        current = self._current_profile()
        if current and current not in profiles:
            profiles.append(current)
        wanted = [(_ALL_PROFILES, "All profiles"), *[(name, name) for name in profiles]]
        shown = [(self.profile_box.itemData(i), self.profile_box.itemText(i)) for i in range(self.profile_box.count())]
        self.profile_box.blockSignals(True)
        if shown != wanted:
            self.profile_box.clear()
            for data, text in wanted:
                self.profile_box.addItem(f"Profile: {text}" if data != _ALL_PROFILES else text, data)
        index = self.profile_box.findData(self._profile_shown if self._profile_shown is not None else _ALL_PROFILES)
        self.profile_box.setCurrentIndex(max(0, index))
        self.profile_box.blockSignals(False)
        self.profile_box.setVisible(len(profiles) > 0)

    def _render(self) -> None:
        window = self._window
        self._render_profiles()
        self.since_button.setEnabled(self.has_runs or self.since_mode)
        self.since_button.setToolTip(
            "Only the changes made after the last program MO2 started" if self.has_runs else
            "No program has been started through MO2 since Companion began keeping this history")
        self.since_button.blockSignals(True)
        self.since_button.setChecked(self.since_mode)
        self.since_button.blockSignals(False)
        for key, button in self.kind_buttons.items():
            button.blockSignals(True)
            button.setChecked(key in self.kinds)
            button.blockSignals(False)
        self.summary.setText(self._summary_text())
        window.history_chip.setText(history.plural(self.total, "change"))
        window.history_chip.setVisible(bool(self._instance_shown))
        if self._error:
            self.empty.setText(f"Couldn't read the history ({self._error}).")
        else:
            self.empty.setText(self._empty_text())
        shown_empty = not self.entries
        self.empty.setVisible(shown_empty)
        self.tree.setVisible(not shown_empty)
        remaining = self.total - len(self.entries)
        self.more_button.setVisible(remaining > 0)
        self.more_button.setText(f"Show {min(_PAGE, remaining):,} older changes"
                                 + (f" ({remaining:,} in all)" if remaining > _PAGE else ""))
        self.export_button.setEnabled(bool(self.entries))
        self._render_tree()

    def _summary_text(self) -> str:
        if not self._instance_shown:
            return "Which MO2 instance? Pick one from More ▸ MO2 instance."
        where = f" in profile {self._profile_shown}" if self._profile_shown else ""
        text = history.plural(self.total, "change") + where
        if self.since_mode and self.baseline is not None:
            moment = history.local(self.baseline["at"])
            when = f"{history.date_text(moment, year=False)}, {history.clock(moment)}" if moment else ""
            text += f", since {self.baseline['subject'] or 'a program'} started {when}"
        elif self.since_mode:
            text += ", since the last program ran"
        shown = len(self.entries)
        if shown < self.total:
            text += f" (newest {shown:,} shown)"
        return text

    def _empty_text(self) -> str:
        if not self._instance_shown:
            return "Choose an MO2 instance to see its history."
        if self.since_mode and self.baseline is None:
            return ("No program has been started through MO2 since Companion began keeping this history, so there is "
                    "nothing to count from. Switch off “Since the last program ran” to see everything.")
        if self.search.text().strip() or len(self.kinds) < len(history.GROUP_KEYS) or self.since_mode:
            return "Nothing matches these filters."
        return ("No changes recorded yet. Companion notes what happens to your list while MO2 runs (mods installed, "
                "switched on or off or moved, plugins switched or moved, profiles, programs started through MO2), "
                "and finds changes made while MO2 was closed the next time it starts.")

    def retheme(self) -> None:
        self._drawn_key = None
        self._render_tree()

    def _render_tree(self) -> None:
        window = self._window
        signature = (tuple((e["id"], e["count"], e["ended_at"], e["exit_code"], e["offline"],
                           tuple((i["name"], i.get("before"), i.get("after")) for i in e["items"])) for e in self.entries),
                     id(window._tokens), window._theme_mode)
        if signature == self._drawn_key:
            return
        self._drawn_key = signature
        colours = window._colours
        self.tree.setUpdatesEnabled(False)
        self.tree.blockSignals(True)
        self.tree.clear()
        today = date.today()
        bold = QFont(self.tree.font())
        bold.setBold(True)
        faint = QBrush(colours["faint"])
        for day, group in history.by_day(self.entries):
            label = history.day_label(day, today) if day != date.min else "Unknown date"
            heading = QTreeWidgetItem(self.tree)
            heading.setText(0, label)
            heading.setText(1, history.plural(len(group), "change"))
            heading.setFont(0, bold)
            heading.setForeground(1, faint)
            heading.setData(0, _ROLE, (_DAY, 0))
            for entry in group:
                self._add_entry(heading, entry, bold, faint)
            heading.setExpanded(True)
        self.tree.blockSignals(False)
        self.tree.setUpdatesEnabled(True)

    def _add_entry(self, parent: QTreeWidgetItem, entry: dict[str, Any], bold: QFont, faint: QBrush) -> None:
        colours = self._window._colours
        item = QTreeWidgetItem(parent)
        item.setData(0, _ROLE, (_ENTRY, entry["id"]))
        item.setText(0, history.title(entry))
        item.setText(1, history.detail(entry))
        moment = history.local(entry["at"])
        item.setText(2, history.clock(moment) if moment is not None else "")
        item.setForeground(1, faint)
        item.setForeground(2, faint)
        item.setToolTip(0, self._tooltip(entry))
        if entry["kind"] == history.PROGRAM_RUN:
            # A marker: bold, in the reminder colour on its tint (every state also has its words: "ran ... exited ...").
            item.setFont(0, bold)
            for column in range(3):
                item.setBackground(column, QBrush(colours["hit"]))
            item.setForeground(0, QBrush(colours["mark"]))
            item.setForeground(1, QBrush(colours["mark"]))
            item.setForeground(2, QBrush(colours["mark"]))
        if entry["count"] > 1 and entry["items"]:
            placeholder = QTreeWidgetItem(item)
            placeholder.setText(0, "…")
            placeholder.setData(0, _ROLE, (_PLACEHOLDER, entry["id"]))

    @staticmethod
    def _tooltip(entry: dict[str, Any]) -> str:
        moment = history.local(entry["at"])
        lines = [history.title(entry)]
        if moment is not None:
            lines.append(f"{history.date_text(moment)}, {history.clock(moment)}")
        if entry.get("offline"):
            lines.append(history.FOUND_EXPLAIN.get(int(entry.get("found") or 1), history.FOUND_EXPLAIN[1]))
        if entry["kind"] == history.PROGRAM_RUN:
            lines.append(entry["detail"].get("path", ""))
        if entry["count"] > len(entry["items"]) > 0:
            lines.append(f"Showing the first {len(entry['items']):,} of {entry['count']:,}.")
        return "\n".join(line for line in lines if line)

    def _expanded(self, item: QTreeWidgetItem) -> None:
        """A grouped entry is opened: its items are made now (a burst can hold hundreds)."""

        if item.childCount() != 1:
            return
        child = item.child(0)
        data = child.data(0, _ROLE)
        if not data or data[0] != _PLACEHOLDER:
            return
        entry = next((e for e in self.entries if e["id"] == data[1]), None)
        self.tree.setUpdatesEnabled(False)
        item.takeChild(0)
        faint = QBrush(self._window._colours["faint"])
        if entry is not None:
            for number, thing in enumerate(entry["items"]):
                row = QTreeWidgetItem(item)
                row.setData(0, _ROLE, (_ITEM, entry["id"]))
                row.setText(0, thing["name"])
                row.setText(1, history.item_detail(entry["kind"], thing))
                row.setForeground(1, faint)
            if entry["count"] > len(entry["items"]):
                more = QTreeWidgetItem(item)
                more.setText(0, f"… and {entry['count'] - len(entry['items']):,} more")
                more.setForeground(0, faint)
        self.tree.setUpdatesEnabled(True)

    def open_groups(self, limit: int | None = None) -> None:
        """Open the grouped entries (the first ``limit`` of them; for tests and pictures)."""

        opened = 0
        for index in range(self.tree.topLevelItemCount()):
            day = self.tree.topLevelItem(index)
            for number in range(day.childCount()):
                item = day.child(number)
                if item.childCount() and (limit is None or opened < limit):
                    item.setExpanded(True)
                    opened += 1

    def items(self) -> list[QTreeWidgetItem]:
        found: list[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            found.append(item)
            for number in range(item.childCount()):
                walk(item.child(number))  # pyright: ignore[reportArgumentType]  # Qt stubs: never None here

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index))  # pyright: ignore[reportArgumentType]  # Qt stubs: never None here
        return found

    # ---------- actions ----------

    def _context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        if item is None:
            return
        data = item.data(0, _ROLE)
        window = self._window
        menu = QMenu(self)
        if data and data[0] == _ENTRY:
            entry = next((e for e in self.entries if e["id"] == data[1]), None)
            if entry is not None and entry["kind"] == history.PROGRAM_RUN:
                menu.addAction("Show only the changes after this run", lambda: self.since_run(entry["id"]))
        if self.since_mode:
            menu.addAction("Show everything again", lambda: self._since_clicked_off())
        menu.addAction("Copy this line", lambda: QGuiApplication.clipboard().setText(
            "  ".join(text for text in (item.text(2), item.text(0), item.text(1)) if text)))
        with window._modal_scope():
            menu.exec(self.tree.viewport().mapToGlobal(position))

    def _since_clicked_off(self) -> None:
        self.since_button.setChecked(False)
        self._since_clicked(False)

    # ---------- export ----------

    def export(self, path: str | None = None, *, quiet: bool = False) -> int | None:
        """Save what is listed (every entry matching the filters, not only those drawn) as Markdown or plain text.
        ``path`` None asks where; ``quiet`` (tests, the live driver) shows no message and lets an error through.
        Returns how many entries were written, or None."""

        window = self._window
        try:
            instance = self._instance()
            if not instance:
                if not quiet:
                    window._tell("information", "Choose an MO2 instance first.")
                return None
            profile = self._effective_profile()            # as the page does, also when it has not been drawn yet
            with EventStore(window._database) as store:
                since = self._since()
                entries = store.history_page(instance, profile=profile, kinds=history.kinds_of(self.kinds),
                                             search=self.search.text(), since=since, limit=_EXPORT_LIMIT)
            if not entries:
                if not quiet:
                    window._tell("information", "There is nothing in the history to export with these filters.")
                return None
            if path is None:
                with window._modal_scope():
                    path, selected = QFileDialog.getSaveFileName(
                        window, "Export list history", "modlist-companion-list-history.md",
                        "Markdown (*.md);;Plain text (*.txt)")
                if not path:
                    return None
                if not Path(path).suffix:
                    path += ".txt" if selected.startswith("Plain") else ".md"
            target = Path(path)
            markdown = target.suffix.lower() in (".md", ".markdown")
            target.write_text(history.render_text(entries, self._heading(instance, profile), markdown=markdown),
                              encoding="utf-8")
        except Exception as error:
            if quiet:
                raise
            window._tell("warning", f"Couldn't export: {error}")
            return None
        if not quiet:
            window._tell("information", f"Saved {history.plural(len(entries), 'change')} to {target}.")
        return len(entries)

    def _heading(self, instance: str, profile: str | None) -> str:
        label = next((entry["label"] for entry in self._window.snapshot.instances if entry["id"] == instance), "")
        parts = ["Changes to the list" + (f" in {label}" if label else ""),
                 f"profile {profile}" if profile else "all profiles"]
        if len(self.kinds) < len(history.GROUP_KEYS):
            parts.append("only " + ", ".join(label.lower() for key, label, _k in history.GROUPS if key in self.kinds))
        if self.search.text().strip():
            parts.append(f"matching “{self.search.text().strip()}”")
        if self.since_mode and self.baseline is not None:
            moment = history.local(self.baseline["at"])
            if moment is not None:
                parts.append(f"since {self.baseline['subject']} started {history.date_text(moment)}, "
                             f"{history.clock(moment)}")
        return " · ".join(parts)
