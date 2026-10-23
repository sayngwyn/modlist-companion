"""The "Which option installed...?" dialog: type or paste a path or a plugin name, see which installer
options install it.

It asks a function for the answer (the window reads the database) and shows it; it opens a window of its
own beside the list, so the list can stay in view while you look.
"""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from .text_menu import use_themed_menu

_DELAY_MS = 150
_ROLE = Qt.ItemDataRole.UserRole


def _label(text: str, name: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    return label


class FileOriginDialog(QDialog):
    """Answers "which installer option put this file here?" for what is typed in the box."""

    def __init__(self, parent: QWidget, lookup: Callable[[str], dict[str, Any]],
                 show: Callable[[str, str], bool] | None = None, query: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Which option installed…?")
        self.setObjectName("fileOriginDialog")
        self._lookup = lookup
        self._show = show
        self.matches: list[dict[str, Any]] = []
        layout = QVBoxLayout(self)
        layout.addWidget(_label("A file path or a plugin name, as MO2 shows it or as you find it in the mod's "
                                "folder. Folder names work too."))
        self.edit = QLineEdit()
        use_themed_menu(self.edit)
        self.edit.setObjectName("originQuery")
        self.edit.setPlaceholderText("e.g. Textures\\Landscape\\Mountains\\Peak.dds, or Majestic Mountains.esp")
        self.edit.setClearButtonEnabled(True)
        layout.addWidget(self.edit)
        self.message = _label("", "originMessage")
        layout.addWidget(self.message)
        self.results = QListWidget()
        self.results.setObjectName("originResults")
        self.results.setWordWrap(True)
        self.results.setAlternatingRowColors(True)
        self.results.setMinimumHeight(180)
        self.results.itemDoubleClicked.connect(self._double_clicked)
        layout.addWidget(self.results, 1)
        self.notes = _label("", "hint")
        layout.addWidget(self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setObjectName("tool")
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.resize(640, 460)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DELAY_MS)
        self._timer.timeout.connect(self.update_results)
        self.edit.textChanged.connect(lambda _text: self._timer.start())
        self.set_query(query)

    def set_query(self, query: str) -> None:
        """Put a query in the box and show its answer now."""

        self._timer.stop()
        self.edit.blockSignals(True)
        self.edit.setText(query)
        self.edit.blockSignals(False)
        self.update_results()

    def update_results(self) -> None:
        self._timer.stop()
        query = self.edit.text().strip()
        self.results.clear()
        self.matches = []
        if not query:
            self.message.setText("Nothing typed yet.")
            self.notes.setText("")
            return
        try:
            answer = self._lookup(query)
        except Exception as error:  # shown here; the window must not break because of the dialog
            self.message.setText(f"Couldn't look that up ({type(error).__name__}: {error}).")
            self.notes.setText("")
            return
        self.matches = answer["matches"]
        shown = answer["path"] or query
        if not self.matches:
            self.message.setText(f"Not from a logged installer: no installer you ran through Modlist Companion "
                                 f"installs {shown}, and no mod it has recorded has it.")
        else:
            count = len(self.matches)
            plain = sum(1 for match in self.matches if match.get("plain"))
            if plain == 0:
                self.message.setText(f"{count} installer option{'s' if count != 1 else ''} install"
                                     f"{'s' if count == 1 else ''} {shown}:")
            else:
                options = count - plain
                parts = []
                if options:
                    parts.append(f"{options} installer option{'s' if options != 1 else ''} "
                                 f"install{'s' if options == 1 else ''} it")
                parts.append(f"{plain} mod{'s' if plain != 1 else ''} not installed through an installer "
                             f"{'has' if plain == 1 else 'have'} it")
                self.message.setText(f"{shown}: " + ", ".join(parts) + ":")
        for match in self.matches:
            item = QListWidgetItem(f"{match['text']}\n{match['detail']}")
            item.setData(_ROLE, (match["event_id"], match["option_id"]))
            item.setToolTip("Double-click to show it in the list")
            self.results.addItem(item)
        self.notes.setText("\n".join(answer["notes"]))

    def results_text(self) -> list[str]:
        """The answers as shown: one string per option."""

        return [self.results.item(row).text() for row in range(self.results.count())]

    def _double_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(_ROLE)
        if data and self._show is not None and not self._show(*data):
            self.notes.setText("That install isn't in the list on screen right now (check the filters).")
