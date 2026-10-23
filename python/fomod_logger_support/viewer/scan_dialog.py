"""The "Scan existing mods..." dialog of the FOMOD Plus edition.

It asks a scanner (see ``scan.py``) to record the mods Companion has no record of (the FOMOD Plus edition also
rebuilds the installs of the mods FOMOD Plus recorded), and shows how far it has got. The scan runs in the background and MO2 stays usable: the dialog is not modal,
Cancel stops the scan before the next mod, and closing the dialog cancels a scan that is still running.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget


def _label(text: str, name: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    return label


class ScanDialog(QDialog):
    # The status text under the bar. It hides QDialog.result(), which Qt itself never calls from Python; the tests and
    # the live driver read it by this name.
    result: QLabel  # pyright: ignore[reportIncompatibleMethodOverride]

    def __init__(self, parent: QWidget, scanner: Any, on_finished=None, *, plus: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Scan existing mods")
        self.setObjectName("scanDialog")
        self._scanner = scanner
        self._on_finished = on_finished
        self.summary: Any = None
        self.running = False
        layout = QVBoxLayout(self)
        layout.addWidget(_label(
            "Recording each mod Companion has no record of: its version, archive, files and plugins. "
            + ("Mods FOMOD Plus installed get their choices rebuilt from the download archive as well. " if plus else "")
            + "MO2 stays usable meanwhile."))
        self.status = _label("", "scanStatus")
        layout.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setObjectName("scanProgress")
        self.bar.setTextVisible(True)
        layout.addWidget(self.bar)
        self.result = _label("", "hint")
        layout.addWidget(self.result)
        row = QHBoxLayout()
        row.addStretch(1)
        self.button = QPushButton("Cancel")
        self.button.setObjectName("tool")
        self.button.setDefault(True)
        self.button.clicked.connect(self._pressed)
        row.addWidget(self.button)
        layout.addLayout(row)
        self.resize(520, 220)

    def begin(self) -> None:
        """Start the scan (the dialog then follows it)."""

        self.summary = None
        self.running = True
        self.result.setText("")
        self.button.setText("Cancel")
        self.bar.setRange(0, 0)           # busy until the first progress report says how many there are
        self.status.setText("Looking for mods to record…")
        started = self._scanner.start(self._progress, self._done)
        if started is None:
            self.running = False
            self.status.setText("A scan is already running.")
            self.button.setText("Close")

    def _progress(self, finished: int, total: int, name: str) -> None:
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(min(finished, total))
        self.bar.setFormat(f"{finished} of {total}")
        self.status.setText(f"Reading {name}…" if finished < total else "Finishing…")

    def _done(self, summary: Any) -> None:
        self.running = False
        self.summary = summary
        total = max(1, summary.total)
        self.bar.setRange(0, total)
        self.bar.setValue(min(summary.done, total))
        self.bar.setFormat(f"{summary.done} of {summary.total}")
        self.status.setText("Stopped." if summary.cancelled else "Done.")
        text = summary.message()
        if summary.problems:
            text += "\n" + "\n".join(summary.problems[:4]) + (f"\n…and {len(summary.problems) - 4} more." if len(summary.problems) > 4 else "")
        self.result.setText(text)
        self.button.setText("Close")
        self.button.setEnabled(True)
        if self._on_finished is not None:
            self._on_finished(summary)

    def _pressed(self) -> None:
        if self.running:
            self._scanner.cancel()
            self.button.setEnabled(False)
            self.status.setText("Stopping after the mod being read…")
            return
        self.close()

    def reject(self) -> None:  # Escape and the title bar's close: the same as the button
        self._pressed()

    def stop(self) -> None:
        """The window is closing: end a scan that is still running (it stops before its next mod) and hide."""

        if self.running:
            self._scanner.cancel()
        self.hide()
