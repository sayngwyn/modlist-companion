"""The scan of mods that Companion has no record of: "Scan existing mods...", in both editions.

The plugin lists the mods to record (on MO2's UI thread, where MO2 can be asked) and hands this module the jobs.
A job is anything with a ``mod_name`` and a ``run(cancelled)`` that reads files and returns a result with a ``mode``
and the job it came from: a plain record (``core/plain_record.PlainJob``: walk the mod's folder, read its plugins'
headers) or, in the FOMOD Plus edition, the rebuild of an install from FOMOD Plus's choices
(``plus/jobs.RebuildJob``). The jobs run on a worker thread, one mod after another, and each finished result comes
back to the UI thread to be stored. The window shows the progress and can cancel: the scan stops before the next
mod (and, inside a mod, between folders and files).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

FAILED, CANCELLED = "failed", "cancelled"
# What each kind of result is counted as.
_COUNTED = {"rebuilt": "rebuilt", "names-only": "names_only", "plain": "plain"}


@dataclass
class ScanSummary:
    total: int = 0
    rebuilt: int = 0
    names_only: int = 0
    plain: int = 0                     # recorded as installed without an installer
    failed: int = 0
    skipped: int = 0                   # already had a record by the time its turn came
    cancelled: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def done(self) -> int:
        return self.rebuilt + self.names_only + self.plain + self.failed + self.skipped

    def message(self) -> str:
        if self.total == 0:
            return "Every mod in your list already has a record here."
        parts = []
        if self.plain:
            parts.append(f"{self.plain} recorded as installed without an installer")
        if self.rebuilt:
            parts.append(f"{self.rebuilt} rebuilt from their download archives")
        if self.names_only:
            parts.append(f"{self.names_only} with option names only (the archive could not be used)")
        if self.failed:
            parts.append(f"{self.failed} could not be read")
        if self.skipped:
            parts.append(f"{self.skipped} already had a record by then")
        text = ", ".join(parts) if parts else "nothing done"
        if self.cancelled:
            return f"Stopped after {self.done} of {self.total} mods: {text}."
        return f"{self.done} of {self.total} mods: {text}."


class Scanner:
    """What the window calls. ``jobs`` lists the jobs to do now; ``store`` stores a finished result and returns
    True if it kept the record; ``executor_for`` gives the executor that runs the work off the UI thread. Progress
    and the end are reported on the UI thread."""

    def __init__(self, jobs: Callable[[], list[Any]], store: Callable[[Any], bool], executor_for: Callable[[], Any],
                 count: Callable[[], int] | None = None):
        self._jobs = jobs
        self._counter = count
        self._store = store
        self._executor_for = executor_for
        self._handle = None
        self._summary: ScanSummary | None = None

    @property
    def running(self) -> bool:
        return self._handle is not None and not self._handle.finished

    def count(self) -> int:
        """How many mods a scan would record now (the window asks each time it opens: ``count`` may answer without
        building the jobs)."""

        return self._counter() if self._counter is not None else len(self._jobs())

    def start(self, on_progress: Callable[[int, int, str], None], on_done: Callable[[ScanSummary], None]) -> ScanSummary | None:
        """Begin a scan. ``on_progress(finished, total, name of the mod being read)``; ``on_done(summary)`` once.
        Returns None (and does nothing) while a scan is already running."""

        if self.running:
            return None
        jobs = self._jobs()
        summary = self._summary = ScanSummary(total=len(jobs))
        if not jobs:
            on_done(summary)
            return summary

        def work(post, cancelled):
            for index, job in enumerate(jobs):
                if cancelled.is_set():
                    return "cancelled"
                post(("start", index, job.mod_name))
                result = job.run(cancelled)
                if result.mode == CANCELLED:
                    return "cancelled"
                post(("result", index, result))
            return "finished"

        def message(item) -> None:
            kind, _index, payload = item
            if kind == "start":
                on_progress(summary.done, summary.total, payload)
                return
            result = payload
            if result.mode == FAILED:
                summary.failed += 1
                summary.problems.append(result.reason)
            else:
                kept = False
                try:
                    kept = self._store(result)
                except Exception as error:      # one mod's trouble must not end the scan
                    summary.failed += 1
                    summary.problems.append(f"{result.job.mod_name}: {error}")
                    on_progress(summary.done, summary.total, result.job.mod_name)
                    return
                if not kept:
                    summary.skipped += 1
                else:
                    counter = _COUNTED.get(result.mode)
                    if counter:
                        setattr(summary, counter, getattr(summary, counter) + 1)
            on_progress(summary.done, summary.total, result.job.mod_name)

        def finished(value, error) -> None:
            if error is not None:
                summary.problems.append(f"the scan stopped: {error}")
            summary.cancelled = value == "cancelled"
            on_done(summary)

        self._handle = self._executor_for().run(work, message, finished)
        return summary

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()

    def reset(self) -> None:
        """Stop and forget a scan whose end will never be delivered (the plugin was switched off): a new one may start."""

        self.cancel()
        self._handle = None
