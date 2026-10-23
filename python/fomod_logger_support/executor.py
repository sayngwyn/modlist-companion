"""Run work off MO2's UI thread and hand what it produces back on it.

``work(post, cancelled)`` runs on a worker thread. ``post(message)`` sends a message to the UI thread; messages arrive
in order, before the end. ``cancelled`` is a ``threading.Event`` that ``Handle.cancel()`` sets: the work looks at it
between its steps. When the work ends, ``on_finished(value, error)`` runs on the UI thread with what the work
returned, or the exception it raised. Nothing from the worker touches Qt widgets or MO2.

``make_qt_executor`` makes the one MO2 uses (a thread and a queued Qt signal). ``InlineExecutor`` runs the work right
away in the caller's thread and ``ManualExecutor`` holds it until told to run; both are for tests.
"""

from __future__ import annotations

import queue
import sys
import threading
from typing import Any, Callable

Work = Callable[[Callable[[Any], None], threading.Event], Any]


class Handle:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.finished = False

    def cancel(self) -> None:
        self.cancelled.set()


def _run(work: Work, handle: Handle, post: Callable[[Any], None]) -> tuple[Any, BaseException | None]:
    try:
        return work(post, handle.cancelled), None
    except BaseException as error:        # reported to the UI thread, never lost in a worker
        return None, error


class InlineExecutor:
    def run(self, work: Work, on_message: Callable[[Any], None],
            on_finished: Callable[[Any, BaseException | None], None]) -> Handle:
        handle = Handle()
        value, error = _run(work, handle, on_message)
        handle.finished = True
        on_finished(value, error)
        return handle

    def close(self) -> None:
        return


class ManualExecutor:
    """Holds each piece of work until ``run_pending``: the caller can see it was queued and not yet done."""

    def __init__(self) -> None:
        self.pending: list[tuple[Work, Callable, Callable, Handle]] = []

    def run(self, work: Work, on_message: Callable[[Any], None],
            on_finished: Callable[[Any, BaseException | None], None]) -> Handle:
        handle = Handle()
        self.pending.append((work, on_message, on_finished, handle))
        return handle

    def run_pending(self) -> int:
        count = 0
        while self.pending:
            work, on_message, on_finished, handle = self.pending.pop(0)
            value, error = _run(work, handle, on_message)
            handle.finished = True
            on_finished(value, error)
            count += 1
        return count

    def close(self) -> None:
        self.pending.clear()


def make_qt_executor():
    """A Qt executor (made when asked for, so that merely importing this module needs no Qt). Make it on the UI
    thread: it delivers to the thread it was made on."""

    from PyQt6.QtCore import QObject, Qt, pyqtSignal

    class QtExecutor(QObject):
        _wake = pyqtSignal()

        def __init__(self) -> None:
            super().__init__()
            self._inbox: queue.Queue = queue.Queue()
            self._closed = False
            self._wake.connect(self._deliver, Qt.ConnectionType.QueuedConnection)  # pyright: ignore[reportCallIssue]  # the PyQt6 stub omits connect()'s type argument

        def run(self, work: Work, on_message: Callable[[Any], None],
                on_finished: Callable[[Any, BaseException | None], None]) -> Handle:
            handle = Handle()

            def post(message: Any) -> None:
                self._inbox.put(("message", on_message, message, handle))
                self._signal()

            def target() -> None:
                value, error = _run(work, handle, post)
                self._inbox.put(("end", on_finished, (value, error), handle))
                self._signal()

            threading.Thread(target=target, name="fomod-companion-background", daemon=True).start()
            return handle

        def _signal(self) -> None:
            try:
                self._wake.emit()
            except RuntimeError:
                pass                      # the object is gone (MO2 is closing): nobody is waiting

        def _deliver(self) -> None:
            while True:
                try:
                    kind, callback, payload, handle = self._inbox.get_nowait()
                except queue.Empty:
                    return
                if self._closed:
                    continue
                # MO2 only logs an exception that escapes a Qt slot, but it would end this loop and strand the
                # rest of the inbox: whatever a callback does wrong stops here.
                try:
                    if kind == "message":
                        callback(payload)
                    else:
                        handle.finished = True
                        callback(*payload)
                except BaseException as error:
                    print(f"[Modlist Companion] a result from the background work could not be used: "
                          f"{type(error).__name__}: {error}", file=sys.stderr)

        def close(self) -> None:
            """Ignore everything the running work sends from now on (it cannot be stopped midway)."""

            self._closed = True

    return QtExecutor()
