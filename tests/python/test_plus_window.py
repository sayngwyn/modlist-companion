"""The window of the FOMOD Plus edition: what it hides and adds, the scan dialog, and the Qt hand-off from the
worker thread. Like the other window tests these borrow MO2's PyQt6 (see test_viewer.py) and show nothing."""

from __future__ import annotations

import threading
import time

from fomod_logger_support.core.storage import EventStore
from fomod_logger_support.executor import make_qt_executor
from fomod_logger_support.scan import ScanSummary

from test_viewer import _store, qt  # noqa: F401 - qt is the module's fixture


class _Scanner:
    """What the window asks of a scanner, with the answers set by the test."""

    def __init__(self, count: int = 2):
        self.found = count
        self.running = False
        self.cancelled = 0
        self.started = None

    def count(self) -> int:
        return self.found

    def start(self, on_progress, on_done):
        if self.running:
            return None
        self.running = True
        self.started = (on_progress, on_done)
        return ScanSummary(total=self.found)

    def cancel(self) -> None:
        self.cancelled += 1


def _actions(window) -> list[str]:
    return [a.text() for a in window.findChildren(__import__("PyQt6.QtGui", fromlist=["QAction"]).QAction) if a.text()]


def test_the_full_edition_window_has_import_and_no_scan(qt, tmp_path, installation_event):
    window = qt.LoggerWindow(_store(tmp_path, installation_event))
    names = _actions(window)
    assert "Import choices file…" in names and "Remove imported choices" in names
    assert "Scan existing mods…" not in names and window.scan_action is None
    assert window.windowTitle() == "Modlist Companion"
    window.deleteLater()


def test_the_plus_window_hides_import_and_offers_the_scan(qt, tmp_path, installation_event):
    window = qt.LoggerWindow(_store(tmp_path, installation_event), can_import=False, scanner=_Scanner(),
                             edition_label="for FOMOD Plus")
    names = _actions(window)
    assert "Import choices file…" not in names and "Remove imported choices" not in names
    assert "Scan existing mods…" in names and window.scan_action is not None
    assert "Export choices file…" in names and "Export install guide…" in names       # export stays
    assert "Which option installed…?" in names and "Compare choices…" in names
    assert window.windowTitle() == "Modlist Companion (for FOMOD Plus)" and window.objectName() == "fomodCompanion"
    assert window.remove_imported_action is None
    window._update_import_actions()                                                    # nothing to update, nothing breaks
    window.refresh()
    window.deleteLater()


def test_scan_with_nothing_to_scan_says_so(qt, tmp_path, installation_event, monkeypatch):
    told = []
    scanner = _Scanner(count=0)
    window = qt.LoggerWindow(_store(tmp_path, installation_event), scanner=scanner)
    monkeypatch.setattr(window, "_tell", lambda kind, text: told.append((kind, text)))
    assert window.scan_existing() is None
    assert told == [("information", "Every mod in your list already has a record here.")]
    assert scanner.started is None
    window.deleteLater()


def test_the_scan_dialog_follows_progress_and_ends_with_the_summary(qt, tmp_path, installation_event):
    scanner = _Scanner(count=3)
    window = qt.LoggerWindow(_store(tmp_path, installation_event), scanner=scanner)
    dialog = window.scan_existing(show=False)
    assert dialog is not None and dialog.running is True and dialog.button.text() == "Cancel"
    on_progress, on_done = scanner.started
    on_progress(0, 3, "Grass Patches")
    assert dialog.status.text() == "Reading Grass Patches…" and (dialog.bar.maximum(), dialog.bar.value()) == (3, 0)
    on_progress(2, 3, "Village")
    assert dialog.bar.value() == 2 and dialog.bar.text() == "2 of 3"
    summary = ScanSummary(total=3, rebuilt=2, names_only=1, problems=["Village: the archive is gone"])
    scanner.running = False
    on_done(summary)
    assert dialog.running is False and dialog.button.text() == "Close" and dialog.status.text() == "Done."
    assert "3 of 3 mods: 2 rebuilt" in dialog.result.text() and "the archive is gone" in dialog.result.text()
    assert dialog.bar.value() == 3
    dialog.button.click()
    assert not dialog.isVisible()
    window.deleteLater()


def test_the_scan_dialog_cancels_and_closing_it_cancels(qt, tmp_path, installation_event):
    scanner = _Scanner(count=2)
    window = qt.LoggerWindow(_store(tmp_path, installation_event), scanner=scanner)
    dialog = window.scan_existing(show=False)
    dialog.button.click()
    assert scanner.cancelled == 1 and not dialog.button.isEnabled() and "Stopping" in dialog.status.text()
    scanner.running = False
    scanner.started[1](ScanSummary(total=2, rebuilt=1, cancelled=True))
    assert dialog.button.isEnabled() and dialog.status.text() == "Stopped."
    assert "Stopped after 1 of 2 mods" in dialog.result.text()
    # Closing the window while a scan runs cancels it; opening the menu item again while it runs shows the same dialog.
    scanner.running = False
    again = window.scan_existing(show=False)
    assert again is dialog and dialog.running
    scanner.running = True
    assert window.scan_existing(show=False) is dialog
    window.close()
    assert scanner.cancelled == 2 and not dialog.isVisible()
    window.deleteLater()


def test_a_scan_already_running_is_not_started_twice(qt, tmp_path, installation_event):
    scanner = _Scanner(count=1)
    scanner.running = True
    window = qt.LoggerWindow(_store(tmp_path, installation_event), scanner=scanner)
    dialog = window.scan_existing(show=False)
    assert dialog.running is False and dialog.status.text() == "A scan is already running."
    window.deleteLater()


def test_the_qt_executor_works_off_the_ui_thread_and_answers_on_it(qt):
    from PyQt6.QtCore import QCoreApplication

    executor = make_qt_executor()
    main = threading.get_ident()
    seen = {"work": None, "messages": [], "end": None}
    release = threading.Event()

    def work(post, cancelled):
        seen["work"] = threading.get_ident()
        post(("one", threading.get_ident()))
        post(("two", threading.get_ident()))
        release.wait(2)
        return "done" if not cancelled.is_set() else "cancelled"

    def message(item):
        seen["messages"].append((item[0], threading.get_ident()))

    def finished(value, error):
        seen["end"] = (value, error, threading.get_ident())

    handle = executor.run(work, message, finished)
    deadline = time.time() + 5
    while len(seen["messages"]) < 2 and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert seen["work"] not in (None, main)
    assert [m[0] for m in seen["messages"]] == ["one", "two"] and {m[1] for m in seen["messages"]} == {main}
    assert seen["end"] is None and handle.finished is False
    handle.cancel()
    release.set()
    while seen["end"] is None and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert seen["end"] == ("cancelled", None, main) and handle.finished is True

    failed = {}
    executor.run(lambda post, cancelled: 1 / 0, lambda m: None, lambda value, error: failed.update(error=error))
    while "error" not in failed and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert isinstance(failed["error"], ZeroDivisionError)

    executor.close()
    late = []
    executor.run(lambda post, cancelled: 5, lambda m: None, lambda value, error: late.append(value))
    end = time.time() + 0.3
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert late == []                  # once closed, nothing it was doing is delivered
