"""The "Compare choices" dialog: two MO2 instances, two copies of a mod, or two installs of one mod, side by side.

It asks a function for the data (the window reads the database), works the comparison out with
``core/compare_choices`` and shows it as a tree of mod > page > group > option with the words "only in A",
"only in B", "different" and "same". It opens a window of its own beside the list, like "Which option
installed...?", and follows the database while it is open.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core import compare_choices as cc

_ROLE = Qt.ItemDataRole.UserRole
_MOD = "mod"
_PLACEHOLDER = "placeholder"
_AUTO_OPEN = 2     # the differing mods opened at once in a two-instance comparison, when there are this few

KINDS = {"instances": "Two MO2 instances", "copies": "Two copies of a mod", "installs": "Two installs of a mod"}
_SCOPE_WORDS = {"copies": "Copies of", "installs": "Mod"}
_FILE_ROWS = 300      # files listed under "only in A" / "only in B" for mods without installer options

_WORDS = {
    "mod": {"same": "same", "different": "different", "only-a": "only in A", "only-b": "only in B"},
    "page": {"same": "same", "different": "different", "only-a": "page only in A", "only-b": "page only in B",
             "skipped-a": "page skipped in A", "skipped-b": "page skipped in B"},
    "group": {"same": "same", "different": "different"},
    "fact": {"same": "same", "different": "different", "only-a": "only in A", "only-b": "only in B"},
    "option": {"same": "same", "picked-a": "picked only in A", "picked-b": "picked only in B",
               "offered-a": "option only in A", "offered-b": "option only in B", "renamed": "named differently",
               "skipped": "page skipped"},
}
# Which colour says it: A is the accent (blue), B is orange, so the two sides read apart without red or green,
# and every one of them also has its words. Differences that are neither side's are in the plain text colour.
_TONE = {"only-a": "accent", "only-b": "off", "picked-a": "accent", "picked-b": "off", "offered-a": "accent",
         "offered-b": "off", "skipped-a": "accent", "skipped-b": "off", "same": "faint", "skipped": "faint"}


def _label(text: str, name: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    return label


def _day(stamp: str | None) -> str:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone().strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return str(stamp or "unknown date")


def _install_text(event: dict[str, Any] | None) -> str:
    if event is None:
        return "not installed"
    return " · ".join(part for part in ((event.get("mod") or {}).get("version"), _day(event.get("completed_at"))) if part)


class CompareDialog(QDialog):
    """Compare choices: pick what to compare, see how the picks differ."""

    def __init__(self, parent: QWidget, source: Callable[[], dict[str, Any]],
                 colours: Callable[[], dict[str, Any]]):
        super().__init__(parent)
        self.setWindowTitle("Compare choices")
        self.setObjectName("compareDialog")
        self._source = source
        self._colours = colours
        self._data: dict[str, Any] = {"events": [], "instances": [], "this_instance": "", "include_hidden": False}
        self._groups: list[dict[str, Any]] = []
        self._histories: list[dict[str, Any]] = []
        self._entries: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = None     # the last comparison (core/compare_choices)
        self.kind = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(_label("Compare the choices made in two MO2 instances, in two copies of a mod, or in two "
                                "installs of one mod.", "hint"))
        form = QHBoxLayout()
        form.setSpacing(8)
        self.kind_box = QComboBox()
        self.kind_box.setObjectName("compareKind")
        self.kind_box.setAccessibleName("What to compare")
        self.scope_label = QLabel("")
        self.scope_box = QComboBox()
        self.scope_box.setObjectName("compareScope")
        self.scope_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.scope_box.setMinimumContentsLength(18)
        form.addWidget(QLabel("Compare"))
        form.addWidget(self.kind_box)
        form.addWidget(self.scope_label)
        form.addWidget(self.scope_box)
        form.addStretch(0)
        self._form = form
        layout.addLayout(form)
        pick = QHBoxLayout()
        pick.setSpacing(8)
        self.a_box, self.b_box = QComboBox(), QComboBox()
        for box, name, letter in ((self.a_box, "compareA", "A"), (self.b_box, "compareB", "B")):
            box.setObjectName(name)
            box.setAccessibleName(f"Side {letter}")
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(18)
        pick.addWidget(QLabel("A"))
        pick.addWidget(self.a_box, 1)
        pick.addWidget(QLabel("B"))
        pick.addWidget(self.b_box, 1)
        layout.addLayout(pick)

        self.legend = _label("", "hint")
        layout.addWidget(self.legend)
        summary_row = QHBoxLayout()
        self.summary = _label("", "compareSummary")
        summary_row.addWidget(self.summary, 1)
        self.identical_box = QCheckBox("Show identical")
        self.identical_box.setToolTip("Also list the mods, pages, groups and options that are the same on both sides")
        summary_row.addWidget(self.identical_box, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(summary_row)

        self.tree = QTreeWidget()
        self.tree.setObjectName("compareTree")
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Mod · page · group · option", "A", "B", "Difference"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setIndentation(18)
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemExpanded.connect(self._expanded)
        layout.addWidget(self.tree, 1)
        self.notes = _label("", "hint")
        layout.addWidget(self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setObjectName("tool")
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.resize(900, 600)
        self.setPalette(parent.palette())

        self.kind_box.currentIndexChanged.connect(self._kind_changed)
        self.scope_box.currentIndexChanged.connect(self._scope_changed)
        self.a_box.currentIndexChanged.connect(self._sides_changed)
        self.b_box.currentIndexChanged.connect(self._sides_changed)
        self.identical_box.toggled.connect(lambda _on: self._render())
        self.reload()

    # ---------- data ----------

    def retheme(self) -> None:
        """Follow the window's palette (a dialog is a window of its own, so it doesn't inherit it) and draw again."""

        self.setPalette(self.parentWidget().palette())
        self.reload()

    def reload(self) -> None:
        """Read the data again and keep what was chosen where it still exists."""

        try:
            self._data = self._source()
        except Exception as error:  # shown here; the window must not break because of the dialog
            self._data = {"events": [], "instances": [], "this_instance": "", "include_hidden": False}
            self.notes.setText(f"Couldn't read the choices ({type(error).__name__}: {error}).")
        events, hidden = self._data["events"], self._data.get("include_hidden", False)
        self._groups = cc.find_duplicates(events, include_hidden=hidden)
        histories = []
        for entry in self._data["instances"]:
            histories.extend(({**mod, "instance_id": entry["id"], "instance_label": entry["label"]}
                              for mod in cc.mods_with_history(events, entry["id"], include_hidden=hidden)))
        self._histories = histories
        keep = (self.kind, self._ids())
        self.kind_box.blockSignals(True)
        self.kind_box.clear()
        available = [kind for kind in KINDS if self.available(kind)]
        for kind in available:
            self.kind_box.addItem(KINDS[kind], kind)
        self.kind_box.blockSignals(False)
        if not available:
            self.kind = ""
            self._fill_scope()
            self._fill_sides()
            self._render()
            return
        wanted = keep[0] if keep[0] in available else available[0]
        self.kind_box.blockSignals(True)
        self.kind_box.setCurrentIndex(available.index(wanted))
        self.kind_box.blockSignals(False)
        self.kind = wanted
        self._fill_scope(keep[1])
        self._fill_sides(keep[1])
        self._render()

    def available(self, kind: str) -> bool:
        """Whether the database has anything to compare in this way."""

        if kind == "instances":
            return len(self._data["instances"]) > 1
        return bool(self._groups if kind == "copies" else self._histories)

    def _ids(self) -> tuple:
        return (self.scope_box.currentData(), self.a_box.currentData(), self.b_box.currentData())

    def _instance_name(self, instance_id: str) -> str:
        label = next((e["label"] for e in self._data["instances"] if e["id"] == instance_id), "") or instance_id
        return f"{label} (this instance)" if instance_id and instance_id == self._data.get("this_instance") else label

    def _several_instances(self) -> bool:
        return len(self._data["instances"]) > 1

    # ---------- the pickers ----------

    def _fill_scope(self, keep: tuple = (None, None, None)) -> None:
        self.scope_box.blockSignals(True)
        self.scope_box.clear()
        shown = self.kind in _SCOPE_WORDS
        self.scope_label.setText(_SCOPE_WORDS.get(self.kind, ""))
        self.scope_label.setVisible(shown)
        self.scope_box.setVisible(shown)
        # The scope picker takes the room when there is one; otherwise the kind picker keeps its own size.
        self._form.setStretch(3, 1 if shown else 0)
        self._form.setStretch(4, 0 if shown else 1)
        if self.kind == "copies":
            for index, group in enumerate(self._groups):
                names = " ↔ ".join(event["mod"]["name"] for event in group["mods"][:2])
                more = f" ↔ {len(group['mods']) - 2} more" if len(group["mods"]) > 2 else ""
                where = f"{group['instance_label']}: " if self._several_instances() else ""
                self.scope_box.addItem(f"{where}{names}{more} ({group['why']})", group["mods"][0]["event_id"])
        elif self.kind == "installs":
            for mod in self._histories:
                where = f"{mod['instance_label']}: " if self._several_instances() else ""
                self.scope_box.addItem(f"{where}{mod['name']} ({len(mod['installs'])} installs)", mod["installs"][0]["event_id"])
        found = self.scope_box.findData(keep[0]) if isinstance(keep[0], str) else -1
        self.scope_box.setCurrentIndex(max(0, found) if self.scope_box.count() else -1)
        self.scope_box.blockSignals(False)

    def _members(self) -> list[dict[str, Any]]:
        """The installs the A and B pickers offer for the chosen scope."""

        data = self.scope_box.currentData()
        if data is None:
            return []
        if self.kind == "copies":
            group = next((g for g in self._groups if g["mods"][0]["event_id"] == data), None)
            return list(group["mods"]) if group else []
        mod = next((m for m in self._histories if m["installs"][0]["event_id"] == data), None)
        return list(mod["installs"]) if mod else []

    def _fill_sides(self, keep: tuple = (None, None, None)) -> None:
        for box in (self.a_box, self.b_box):
            box.blockSignals(True)
            box.clear()
        if self.kind == "instances":
            for entry in self._data["instances"]:
                for box in (self.a_box, self.b_box):
                    box.addItem(self._instance_name(entry["id"]), entry["id"])
            ids = [entry["id"] for entry in self._data["instances"]]
            this = self._data.get("this_instance")
            first = ids.index(this) if this in ids else 0
            second = next((i for i in range(len(ids)) if i != first), 0)
            a_index, b_index = first, second
            if keep[1] in ids and keep[2] in ids and keep[1] != keep[2]:
                a_index, b_index = ids.index(keep[1]), ids.index(keep[2])
        elif self.kind in ("copies", "installs"):
            members = self._members()
            for event in members:
                name = event["mod"]["name"]
                if self.kind == "copies":
                    text = f"{name} ({_install_text(event)})"
                else:
                    text = _install_text(event) + (" · newest" if event is members[0] else "") + (" · removed" if event.get("removed_at") else "")
                for box in (self.a_box, self.b_box):
                    box.addItem(text, event["event_id"])
            if self.kind == "installs":      # an older install against the newest
                a_index, b_index = (1 if len(members) > 1 else 0), 0
            else:
                a_index, b_index = 0, (1 if len(members) > 1 else 0)
            ids = [event["event_id"] for event in members]
            if keep[1] in ids and keep[2] in ids and keep[1] != keep[2]:
                a_index, b_index = ids.index(keep[1]), ids.index(keep[2])
        else:
            a_index = b_index = -1
        self.a_box.setCurrentIndex(a_index)
        self.b_box.setCurrentIndex(b_index)
        for box in (self.a_box, self.b_box):
            box.blockSignals(False)
        for widget in (self.a_box, self.b_box):
            widget.setEnabled(self.kind != "")

    def _kind_changed(self, _index: int) -> None:
        self.kind = self.kind_box.currentData() or ""
        self._fill_scope()
        self._fill_sides()
        self._render()

    def _scope_changed(self, _index: int) -> None:
        self._fill_sides()
        self._render()

    def _sides_changed(self, _index: int) -> None:
        self._render()

    # ---------- driving it from code (tests, the live driver) ----------

    def choose(self, kind: str, a: str | None = None, b: str | None = None) -> bool:
        """Pick what to compare: a kind, and the two instance ids or install ids (``a`` and ``b``; for copies and
        installs the scope follows ``a``). Returns False when that can't be picked."""

        index = self.kind_box.findData(kind)
        if index < 0:
            return False
        if self.kind_box.currentIndex() != index:
            self.kind_box.setCurrentIndex(index)       # fills the pickers
        if kind != "instances" and a is not None:
            for position in range(self.scope_box.count()):
                self.scope_box.setCurrentIndex(position)
                if self.a_box.findData(a) >= 0:
                    break
            else:
                return False
        for box, value in ((self.a_box, a), (self.b_box, b)):
            if value is not None:
                found = box.findData(value)
                if found < 0:
                    return False
                box.blockSignals(True)
                box.setCurrentIndex(found)
                box.blockSignals(False)
        self._render()
        return True

    # ---------- the comparison ----------

    def _compare(self) -> tuple[str, list[dict[str, Any]], str]:
        """(summary sentence, mod entries, legend) for what is picked, or (message, [], "")."""

        a, b = self.a_box.currentData(), self.b_box.currentData()
        self.result = None
        if not self.kind:
            return ("Nothing to compare yet. You need a second MO2 instance in the database, two copies of a mod, or "
                    "a mod installed more than once."), [], ""
        if a is None or b is None or a == b:
            return "Pick two different ones to compare.", [], ""
        events = self._data["events"]
        hidden = self._data.get("include_hidden", False)
        if self.kind == "instances":
            self.result = cc.compare_instances(events, a, b, include_hidden=hidden)
            return (cc.describe_instances(self.result), self.result["mods"],
                    f"A = {self._instance_name(a)}   ·   B = {self._instance_name(b)}")
        by_id = {event["event_id"]: event for event in self._members()}
        left, right = by_id.get(a), by_id.get(b)
        if left is None or right is None:
            return "Pick two different ones to compare.", [], ""
        detail = cc.compare_installs(left, right)
        self.result = detail
        names = (left["mod"]["name"], right["mod"]["name"])
        entry = {"a": left, "b": right, "paired_by": "", "detail": detail, "status": detail["status"],
                 "name": names[0] if names[0].casefold() == names[1].casefold() else f"{names[0]}  ↔  {names[1]}"}
        return (cc.describe(detail), [entry],
                f"A = {left['mod']['name']} ({_install_text(left)})   ·   B = {right['mod']['name']} ({_install_text(right)})")

    def _render(self) -> None:
        sentence, entries, legend = self._compare()
        self._entries = entries
        self.legend.setText(legend)
        self.summary.setText(sentence)
        for box in (self.scope_box, self.a_box, self.b_box):
            box.setToolTip(box.currentText())      # the whole name, when the box is too narrow for it
        self.tree.clear()
        self.notes.setText("")
        show_same = self.identical_box.isChecked()
        shown = [index for index, entry in enumerate(entries) if show_same or entry["status"] != cc.SAME]
        self.tree.setUpdatesEnabled(False)
        for index in shown:
            self._add_mod(index, entries[index])
        if entries and not shown:
            self._message("Everything is identical. Turn on “Show identical” to list it.")
        elif not entries and self.kind:
            self._message(sentence if self.result is None else "No mods to list.")
        if self.kind == "instances" and entries:
            top = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
            opening = [item for item in top if item.data(0, _ROLE) and entries[item.data(0, _ROLE)[1]]["status"] == cc.DIFFERENT]
            if len(opening) <= _AUTO_OPEN:
                for item in opening:
                    item.setExpanded(True)
        elif self.kind in ("copies", "installs") and self.tree.topLevelItemCount() and shown:
            self.tree.topLevelItem(0).setExpanded(True)
        self.tree.setUpdatesEnabled(True)

    def _message(self, text: str) -> None:
        row = QTreeWidgetItem(self.tree, [text])
        row.setForeground(0, QBrush(self._colours()["faint"]))
        row.setFlags(Qt.ItemFlag.ItemIsEnabled)

    # ---------- the tree ----------

    def _brush(self, tone: str) -> QBrush | None:
        return QBrush(self._colours()[tone]) if tone in self._colours() else None

    def _status_item(self, item: QTreeWidgetItem, level: str, status: str) -> None:
        item.setText(3, _WORDS[level][status])
        brush = self._brush(_TONE.get(status, ""))
        if brush is not None:
            item.setForeground(3, brush)
        if status not in ("same", "skipped") and _TONE.get(status) is None:
            font = item.font(3)
            font.setBold(True)
            item.setFont(3, font)

    def _faint(self, item: QTreeWidgetItem, *columns: int) -> None:
        for column in columns:
            item.setForeground(column, QBrush(self._colours()["faint"]))

    def _add_mod(self, index: int, entry: dict[str, Any]) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self.tree)
        item.setData(0, _ROLE, (_MOD, index))
        item.setText(0, entry["name"] or "Unnamed mod")
        bold = item.font(0)
        bold.setBold(True)
        item.setFont(0, bold)
        item.setText(1, _install_text(entry["a"]))
        item.setText(2, _install_text(entry["b"]))
        self._faint(item, 1, 2)
        self._status_item(item, "mod", entry["status"])
        details = []
        for side, event in (("A", entry["a"]), ("B", entry["b"])):
            if event is not None:
                details.append(f"{side}: {event['mod']['name']}, {_install_text(event)}"
                               + (f", {event['source']['archive']}" if event.get("source", {}).get("archive") else ""))
        if entry.get("paired_by"):
            details.append({"nexus": "Paired by Nexus ID", "archive": "Paired by archive file",
                            "name": "Paired by mod name"}[entry["paired_by"]])
        item.setToolTip(0, "\n".join(details))
        if entry["detail"] is not None:
            QTreeWidgetItem(item, ["…"]).setData(0, _ROLE, (_PLACEHOLDER, 0))
        return item

    def _expanded(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, _ROLE)
        first = item.child(0)
        if data and data[0] == _MOD and first is not None and first.data(0, _ROLE) == (_PLACEHOLDER, 0):
            self._fill_mod(item, self._entries[data[1]]["detail"], self._entries[data[1]])

    def _fact_row(self, parent: QTreeWidgetItem, name: str, a: str, b: str, differs: bool) -> QTreeWidgetItem:
        row = QTreeWidgetItem(parent, [name, a, b])
        self._status_item(row, "fact", "different" if differs else "same")
        self._faint(row, 1, 2)
        return row

    def _fill_plain(self, item: QTreeWidgetItem, detail: dict[str, Any], entry: dict[str, Any]) -> None:
        """The rows for a pair where a side has no installer choices: version, archive, installer, plugins, files."""

        plain, show_same = detail["plain"], self.identical_box.isChecked()
        a, b = entry["a"], entry["b"]

        def show(differs: bool) -> bool:
            return differs or show_same

        if show(bool(plain["installer"])):
            self._fact_row(item, "Installer", "none" if cc.is_plain(a) else "an installer",
                           "none" if cc.is_plain(b) else "an installer", bool(plain["installer"]))
        if show(plain["version_differs"]):
            self._fact_row(item, "Version", plain["version"][0] or "unknown", plain["version"][1] or "unknown",
                           plain["version_differs"])
        if show(plain["archive_differs"]):
            self._fact_row(item, "Archive", plain["archive"][0] or "none", plain["archive"][1] or "none",
                           plain["archive_differs"])
        plugins = plain["plugins"]
        if plugins is not None and show(bool(plugins["only_a"] or plugins["only_b"])):
            row = self._fact_row(item, "Plugins", f"{len(plugins['only_a'])} only here" if plugins["only_a"] else "",
                                 f"{len(plugins['only_b'])} only here" if plugins["only_b"] else "",
                                 bool(plugins["only_a"] or plugins["only_b"]))
            for status, names in (("only-a", plugins["only_a"]), ("only-b", plugins["only_b"])):
                for name in names:
                    child = QTreeWidgetItem(row, [name])
                    self._status_item(child, "fact", status)
            row.setExpanded(True)
        files = plain["files"]
        if files is not None and show(files["differs"]):
            cut = lambda side: "+" if files[f"cut_{side}"] else ""
            row = self._fact_row(item, "Files", f"{files['count_a']:,}{cut('a')}", f"{files['count_b']:,}{cut('b')}",
                                 files["differs"])
            reader = self._data.get("files")
            if files["differs"] and reader is not None:
                try:
                    lists = reader(a["event_id"]), reader(b["event_id"])
                    diff = cc.file_differences(*lists)
                except Exception as error:
                    QTreeWidgetItem(row, [f"Couldn't read the files ({error})"])
                    return
                if any(not found and event["plain"].get("file_count") for found, event in zip(lists, (a, b))):
                    self._message_under(row, "The list of files of the earlier install was not kept (a mod keeps the lists "
                                             "of its two newest installs), so only the counts can be compared.")
                    return
                for status, paths in (("only-a", diff["only_a"]), ("only-b", diff["only_b"])):
                    for path in paths[:_FILE_ROWS]:
                        child = QTreeWidgetItem(row, [path])
                        self._status_item(child, "fact", status)
                    if len(paths) > _FILE_ROWS:
                        self._message_under(row, f"…and {len(paths) - _FILE_ROWS:,} more only in "
                                                 f"{'A' if status == 'only-a' else 'B'}")
                row.setExpanded(True)

    def _fill_mod(self, item: QTreeWidgetItem, detail: dict[str, Any], entry: dict[str, Any] | None = None) -> None:
        item.takeChildren()
        show_same = self.identical_box.isChecked()
        if detail.get("plain") and entry is not None:
            self._fill_plain(item, detail, entry)
        for page in detail["pages"]:
            if page["status"] == cc.SAME and not show_same:
                continue
            page_item = QTreeWidgetItem(item, [page["name"] or "Page"])
            self._faint(page_item, 0)
            self._status_item(page_item, "page", page["status"])
            if page["skipped_a"]:
                page_item.setText(1, "page skipped")
            if page["skipped_b"]:
                page_item.setText(2, "page skipped")
            self._faint(page_item, 1, 2)
            for group in page["groups"]:
                if group["status"] == cc.SAME and not show_same:
                    continue
                group_item = QTreeWidgetItem(page_item, [group["name"] or "Options"])
                bold = group_item.font(0)
                bold.setBold(True)
                group_item.setFont(0, bold)
                self._status_item(group_item, "group", group["status"])
                for option in group["options"]:
                    if option["status"] in (cc.SAME, "skipped") and not show_same:
                        continue
                    self._option_item(group_item, option)
                group_item.setExpanded(True)
            page_item.setExpanded(True)
        if item.childCount() == 0:
            self._message_under(item, "Everything is identical. Turn on “Show identical” to list it.")

    def _message_under(self, parent: QTreeWidgetItem, text: str) -> None:
        row = QTreeWidgetItem(parent, [text])
        row.setForeground(0, QBrush(self._colours()["faint"]))
        row.setFlags(Qt.ItemFlag.ItemIsEnabled)

    @staticmethod
    def _cell(picked: bool | None, skipped: bool) -> str:
        if picked is None:
            return "— not offered"
        if skipped:
            return "page skipped"
        return "✓ picked" if picked else "○ not picked"

    def _option_item(self, parent: QTreeWidgetItem, option: dict[str, Any]) -> None:
        item = QTreeWidgetItem(parent, [option["name"] or "Unnamed option"])
        item.setText(1, self._cell(option["picked_a"], option["skipped_a"]))
        item.setText(2, self._cell(option["picked_b"], option["skipped_b"]))
        for column, key in ((1, "picked_a"), (2, "picked_b")):
            picked = option[key] and not option["skipped_a" if column == 1 else "skipped_b"]
            item.setForeground(column, QBrush(self._colours()["picked" if picked else "faint"]))
        self._status_item(item, "option", option["status"])
        if option["status"] == "renamed":
            item.setToolTip(0, f"A calls it “{option['name_a']}”, B calls it “{option['name_b']}”.")
        elif option["status"] == "offered-a":
            item.setToolTip(0, "A's installer offers this option; B's does not.")
        elif option["status"] == "offered-b":
            item.setToolTip(0, "B's installer offers this option; A's does not.")

    # ---------- reading it back (tests, the live driver) ----------

    def expand_all(self) -> None:
        """Open every mod so its pages, groups and options are made."""

        for index in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(index).setExpanded(True)

    def _items(self) -> list[QTreeWidgetItem]:
        found: list[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            found.append(item)
            for position in range(item.childCount()):
                walk(item.child(position))  # pyright: ignore[reportArgumentType]  # Qt stubs: never None here

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index))  # pyright: ignore[reportArgumentType]  # Qt stubs: never None here
        return found

    def rows(self) -> list[tuple[int, str, str, str, str]]:
        """(depth, name, A cell, B cell, difference) for every row made so far."""

        out = []
        for item in self._items():
            depth, parent = 0, item.parent()
            while parent is not None:
                depth, parent = depth + 1, parent.parent()
            out.append((depth, item.text(0), item.text(1), item.text(2), item.text(3)))
        return out

    def summary_text(self) -> str:
        return self.summary.text()
