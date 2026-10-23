"""TEST-ONLY MO2 plugin that drives Modlist Companion from inside the running MO2.

Never shipped. ``run_live.py`` copies this file into the test MO2's ``plugins``
folder as ``fomod_companion_driver.py`` and removes it again afterwards.

It watches ``<plugin data>/fomod_companion_driver/inbox`` for ``<id>.json``
command files and writes ``outbox/<id>.json`` results. Everything happens in
this process through Qt and MO2's plugin API: no screen clicking. The commands:

  ping, state, refresh, install, set_plugin, set_mod, remove_mod, window_state,
  click_card, screenshot, idle_probe, quit, eval (debug), logger_problems, logger_fix.

``profiles`` and ``compare_choices`` read the profile information the window shows and open the More menu's "Compare
choices..." dialog (``screenshot`` takes it as target "compare").

``export_choices`` / ``import_choices`` / ``remove_imported`` / ``imported_state`` use the window's export and import actions
(given a path, so no file dialog opens); ``forget_own_events`` makes the test MO2 look like a PC with no history of one mod.

``logger_problems`` / ``logger_fix`` read and use the Companion's MO2 problem list (IPluginDiagnose) the way MO2's
notification dialog does: the problem keys, their short and full descriptions, and the "Fix" button.

The next group lets another tool (the ModLabs bridge, through ``run_live.py``) make the changes while this
plugin keeps doing what only it can: ticking the FOMOD dialog's choices, reading the Companion window and
taking screenshots. Window changes are stamped with ``time.perf_counter()``, a clock all processes on this PC
share, so the caller can compare them with its own timestamps (the "wall" fields below are such stamps):

  install_arm / install_report   drive the installer dialogs of an ``installMod`` somebody else starts
  watch_start / watch_peek / watch_stop   watch the open window (and MO2's change signals) while the caller acts

The FOMOD Plus edition (``run_live.py --edition plus``) is driven by the same plugin: ``install`` / ``install_arm`` tick the
options of FOMOD Plus's own dialog (see ``InstallSession.plus_fomod``), and ``mod_record``, ``plus_state``, ``wait_plus_idle``,
``forget_plus_events``, ``scan_existing``, ``close_scan_dialog``, ``menu_actions`` and ``set_persistent`` read and exercise what is
specific to that edition.

See the ``cmd_*`` methods for the arguments.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

import mobase
from PyQt6.QtCore import QCoreApplication, QEventLoop, QFileSystemWatcher, Qt, QTimer
from PyQt6.QtGui import QAction, QGuiApplication, QIcon
from PyQt6.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QDialog, QFrame, QGroupBox, QLabel, QMessageBox, QProgressDialog,
    QPushButton, QStackedWidget, QToolButton, QTreeView, QWidget, QDialogButtonBox,
)

_PLUGINS_DIR = Path(__file__).resolve().parent
_ROLE_CHECK = Qt.ItemDataRole.CheckStateRole
_PLUGIN_SUFFIXES = (".esp", ".esm", ".esl")
# The Companion plugin classes (one per edition) and the Tools-menu names they are listed under.
_COMPANION_CLASSES = ("FomodLoggerPlugin", "FomodLoggerPlusPlugin")
_COMPANION_ACTIONS = ("Modlist Companion", "Modlist Companion (for FOMOD Plus)")


def _now() -> float:
    return time.perf_counter()


def _spin(seconds: float) -> None:
    """Run the event loop for a while (nested), so timers and callbacks fire."""

    loop = QEventLoop()
    QTimer.singleShot(max(1, int(seconds * 1000)), loop.quit)
    loop.exec()


def _wait_until(predicate, timeout: float, step: float = 0.05) -> bool:
    end = _now() + timeout
    while _now() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        _spin(step)
    try:
        return bool(predicate())
    except Exception:
        return False


def right_screen():
    """The right-most monitor (where the test MO2 already sits on this PC).

    Qt's screen names are monitor models, not Windows' DISPLAYn device names, and
    on this PC DISPLAY2 is the portrait monitor on the LEFT, so position
    decides; ping reports the names so the choice is visible.
    """

    return max(QGuiApplication.screens(), key=lambda s: s.geometry().x())


class FomodCompanionDriver(mobase.IPluginTool):
    def __init__(self) -> None:
        mobase.IPluginTool.__init__(self)
        self._org = None
        self._root: Path | None = None
        self._queue: list[Path] = []
        self._seen: set[str] = set()
        self._busy = False
        self._ready_written = False
        self._placed: set[int] = set()
        self._window_events: list[dict] = []
        self._instrumented = None
        self._t0 = _now()
        self._watcher = None
        self._scan_timer = None
        self._ready_timer = None
        self._source = {"inbox": 0, "poll": 0}
        self._session = None
        self._watch = None
        self._probe_refs: list = []
        self._probe_log: list[dict] | None = None
        self._probe_registered: dict | None = None

    # ----- IPlugin boilerplate -----

    def name(self) -> str:
        return "Modlist Companion Live Driver"

    def author(self) -> str:
        return "test only"

    def description(self) -> str:
        return "Test-only driver for the live Modlist Companion checks. Never ship."

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(0, 1, 0)

    def settings(self) -> list:
        return []

    def displayName(self) -> str:
        return "Modlist Companion Live Driver"

    def tooltip(self) -> str:
        return "Test only"

    def icon(self) -> QIcon:
        return QIcon()

    def setParentWidget(self, _widget) -> None:
        return

    def display(self) -> None:
        return

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._org = organizer
        self._root = Path(organizer.pluginDataPath()) / "fomod_companion_driver"
        for sub in ("inbox", "outbox"):
            (self._root / sub).mkdir(parents=True, exist_ok=True)
        if str(_PLUGINS_DIR) not in sys.path:
            sys.path.insert(0, str(_PLUGINS_DIR))
        self.log(f"driver init; python {sys.version.split()[0]}")
        # If MO2 aborts (an uncaught Python exception in a Qt slot does that), say where: Python's fault handler
        # writes every thread's stack to this file on SIGABRT and on a crash.
        try:
            import faulthandler
            self._fault_file = open(self._root / "fault.log", "a", encoding="utf-8")
            faulthandler.enable(file=self._fault_file, all_threads=True)
        except Exception as exc:
            self.log(f"faulthandler unavailable: {exc}")
        # The fault locator hooks every exception in the process (a Python callback that can run on any thread), so it is
        # off unless asked for with FOMOD_COMPANION_FAULT_LOCATOR=1 in the environment MO2 starts with.
        if os.environ.get("FOMOD_COMPANION_FAULT_LOCATOR") == "1":
            try:
                self._install_fault_locator()
            except Exception as exc:
                self.log(f"fault locator unavailable: {exc}")
        self._watcher = QFileSystemWatcher()
        self._watcher.addPath(str(self._root / "inbox"))
        self._watcher.directoryChanged.connect(lambda _p: self._scan("inbox"))
        # A slow safety net in case the folder watcher misses a change; the
        # source of every command is reported so a watcher failure is visible.
        self._scan_timer = QTimer()
        self._scan_timer.setInterval(1000)
        self._scan_timer.timeout.connect(lambda: self._scan("poll"))
        self._scan_timer.start()
        self._ready_timer = QTimer()
        self._ready_timer.setInterval(300)
        self._ready_timer.timeout.connect(self._check_ready)
        self._ready_timer.start()
        return True

    def _install_fault_locator(self) -> None:
        """TEST ONLY: name the module and offset of the first access violations and aborts in this process (a vectored
        exception handler that logs and lets the exception go on), so a crash inside somebody else's DLL can be reported
        precisely. Written to fault-locator.log beside the driver's log."""

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        kernel32.GetModuleHandleExW.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        kernel32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
        kernel32.GetModuleFileNameW.restype = wintypes.DWORD

        class Record(ctypes.Structure):
            pass

        Record._fields_ = [("code", wintypes.DWORD), ("flags", wintypes.DWORD), ("chain", ctypes.POINTER(Record)),
                           ("address", ctypes.c_void_p), ("count", wintypes.DWORD), ("info", ctypes.c_size_t * 15)]

        class Pointers(ctypes.Structure):
            _fields_ = [("record", ctypes.POINTER(Record)), ("context", ctypes.c_void_p)]

        path = str(self._root / "fault-locator.log")
        state = {"busy": False, "count": 0}

        def where(address) -> str:
            handle = ctypes.c_void_p()
            if not address or not kernel32.GetModuleHandleExW(0x6, address, ctypes.byref(handle)):  # FROM_ADDRESS | UNCHANGED_REFCOUNT
                return f"0x{address or 0:x} (no module)"
            name = ctypes.create_unicode_buffer(520)
            kernel32.GetModuleFileNameW(handle, name, 520)
            return f"{Path(name.value).name}+0x{address - (handle.value or 0):x}"

        @ctypes.WINFUNCTYPE(wintypes.LONG, ctypes.POINTER(Pointers))
        def handler(pointers):
            try:
                record = pointers.contents.record.contents
                if record.code not in (0xC0000005, 0xC00000FD, 0xC0000409, 0xC0000374) or state["busy"] or state["count"] >= 2:
                    return 0
                state["busy"] = True
                state["count"] += 1
                lines = [f"{time.strftime('%H:%M:%S')} exception 0x{record.code:08X} at {where(record.address)}"
                         + (f" (access {'write' if record.info[0] == 1 else 'read' if record.info[0] == 0 else 'execute'} of 0x{record.info[1]:x})"
                            if record.code == 0xC0000005 and record.count >= 2 else "")]
                # The faulting thread's own stack: x64 CONTEXT has Rsp at 0x98 and Rip at 0xF8. A call through a null
                # pointer leaves its return address at the top of the stack; list what looks like code from there up.
                context = ctypes.string_at(pointers.contents.context, 0x100)
                rip, rsp = (int.from_bytes(context[0xF8:0x100], "little"), int.from_bytes(context[0x98:0xA0], "little"))
                lines.append(f"    rip={where(rip)} rsp=0x{rsp:x}")
                stack = ctypes.string_at(rsp, 8 * 400)
                found = 0
                for slot in range(400):
                    value = int.from_bytes(stack[slot * 8:slot * 8 + 8], "little")
                    if value > 0x10000 and not kernel32.GetModuleHandleExW(0x6, value, ctypes.byref(ctypes.c_void_p())) == 0:
                        lines.append(f"    stack[{slot}] {where(value)}")
                        found += 1
                        if found >= 40:
                            break
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                state["busy"] = False
            except BaseException:
                state["busy"] = False
            return 0     # EXCEPTION_CONTINUE_SEARCH: only look

        self._fault_handler = handler            # keep it alive
        kernel32.AddVectoredExceptionHandler = ntdll.RtlAddVectoredExceptionHandler
        ntdll.RtlAddVectoredExceptionHandler.argtypes = [wintypes.ULONG, ctypes.c_void_p]
        ntdll.RtlAddVectoredExceptionHandler.restype = ctypes.c_void_p
        self._fault_handle = ntdll.RtlAddVectoredExceptionHandler(1, ctypes.cast(handler, ctypes.c_void_p))
        self.log(f"fault locator installed ({'ok' if self._fault_handle else 'failed'})")

        def remove() -> None:
            # Before MO2 tears Python down: a handler left registered would call into a finished interpreter.
            if self._fault_handle:
                ntdll.RtlRemoveVectoredExceptionHandler.argtypes = [ctypes.c_void_p]
                ntdll.RtlRemoveVectoredExceptionHandler(self._fault_handle)
                self._fault_handle = None

        application = QCoreApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(remove)

    # ----- plumbing -----

    def log(self, message: str) -> None:
        try:
            with open(self._root / "driver.log", "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%H:%M:%S')} +{_now() - self._t0:8.2f}s {message}\n")
        except Exception:
            pass

    def main_window(self):
        for w in QApplication.topLevelWidgets():
            if w.objectName() == "MainWindow" or w.metaObject().className() == "MainWindow":
                return w
        return None

    def _check_ready(self) -> None:
        main = self.main_window()
        visible = [w for w in QApplication.topLevelWidgets() if w.isVisible()]
        if main is None or not main.isVisible():
            self.log("waiting for the main window; visible: " +
                     ", ".join(f"{type(w).__name__}:{w.objectName()}:{w.windowTitle()!r}" for w in visible))
            return
        self._ready_timer.stop()
        self._place(main)
        self._write_json(self._root / "outbox" / "_ready.json", {
            "ready": True, "profile": self._org.profileName(), "base": str(self._org.basePath()),
            "screen": right_screen().name(), "elapsed": _now() - self._t0})
        self.log("ready")

    def _place(self, widget, force: bool = False) -> None:
        key = id(widget)
        if key in self._placed and not force:
            return
        self._placed.add(key)
        area = right_screen().availableGeometry()
        widget.move(area.x() + 40, area.y() + 40)

    @staticmethod
    def _write_json(path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _read_new(path: Path) -> str:
        # A file that has only just appeared can be locked for a moment by
        # Windows (antivirus, indexer), even though it was moved in whole.
        for attempt in range(20):
            try:
                return path.read_text(encoding="utf-8")
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
        raise AssertionError("unreachable")

    def _scan(self, source: str) -> None:
        inbox = self._root / "inbox"
        for path in sorted(inbox.glob("*.json")):
            if path.name not in self._seen:
                self._seen.add(path.name)
                self._queue.append(path)
                self._source[source] += 1
                self._last_source = source
        self._pump()

    def _pump(self) -> None:
        if self._busy or not self._queue:
            return
        self._busy = True
        try:
            path = self._queue.pop(0)
            started = _now()
            result = {"id": path.stem, "ok": False}
            try:
                command = json.loads(self._read_new(path))
                result["cmd"] = command.get("cmd")
                result["picked_up_by"] = getattr(self, "_last_source", "?")
                handler = getattr(self, "cmd_" + str(command.get("cmd")), None)
                if handler is None:
                    raise ValueError(f"unknown command {command.get('cmd')!r}")
                self.log(f"> {path.stem} {command.get('cmd')}")
                result["data"] = handler(command)
                result["ok"] = True
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
                result["trace"] = traceback.format_exc()
                self.log(f"! {path.stem} {result['error']}")
            result["elapsed_ms"] = round((_now() - started) * 1000, 1)
            self._write_json(self._root / "outbox" / (path.stem + ".json"), result)
            try:
                path.unlink()
            except OSError:
                pass
            self.log(f"< {path.stem} ok={result['ok']} {result['elapsed_ms']} ms")
        finally:
            self._busy = False
        if self._queue:
            QTimer.singleShot(0, self._pump)

    # ----- reading MO2 -----

    def _mods(self) -> dict:
        mod_list = self._org.modList()
        found = {}
        for name in mod_list.allMods():
            mod = mod_list.getMod(name)
            folder = Path(str(mod.absolutePath())) if mod is not None else None
            plugins = []
            if folder and folder.is_dir():
                plugins = [e.name for e in os.scandir(folder) if e.is_file() and e.name.lower().endswith(_PLUGIN_SUFFIXES)]
            found[str(name)] = {"enabled": bool(mod_list.state(name) & mobase.ModState.ACTIVE), "plugins": plugins}
        return found

    def _plugin_states(self, only: list[str] | None = None) -> dict:
        plugins = self._org.pluginList()
        names = only if only is not None else [str(n) for n in plugins.pluginNames()]
        out = {}
        for name in names:
            state = plugins.state(name)
            out[name] = {"state": {mobase.PluginState.ACTIVE: "active", mobase.PluginState.INACTIVE: "inactive",
                                   mobase.PluginState.MISSING: "missing"}.get(state, str(state)),
                         "masters": [str(m) for m in plugins.masters(name)] if state != mobase.PluginState.MISSING else [],
                         "origin": str(plugins.origin(name))}
        return out

    # ----- the Companion window -----

    def window(self):
        for w in QApplication.topLevelWidgets():
            if w.objectName() == "fomodCompanion":
                return w
        return None

    def ensure_window(self, timeout: float = 10.0):
        win = self.window()
        route = "existing"
        opened = False
        if win is None or not win.isVisible():
            opened = True
            route = None
            for action in QApplication.instance().findChildren(QAction):
                if action.text().replace("&", "") in _COMPANION_ACTIONS and action.menu() is None:
                    action.trigger()
                    route = "tools-menu-action"
                    break
            if route is None:
                import gc
                for obj in gc.get_objects():
                    if type(obj).__name__ in _COMPANION_CLASSES:
                        obj.display()
                        route = "gc-display"
                        break
            if route is None:
                raise RuntimeError("could not find a way to open Modlist Companion")
            _wait_until(lambda: self.window() is not None and self.window().isVisible(), timeout)
            win = self.window()
            if win is None:
                raise RuntimeError("Modlist Companion window did not appear")
        if id(win) not in self._placed:
            self._place(win)
            win.resize(1200, 760)
        self._instrument(win)
        self._last_route = route
        if opened:
            _spin(0.3)
        return win

    def logger_plugin(self):
        """The running Companion plugin object (MO2 gives plugins no way to look another one up)."""

        import gc
        for obj in gc.get_objects():
            if type(obj).__name__ in _COMPANION_CLASSES:
                return obj
        raise RuntimeError("the Modlist Companion plugin object was not found")

    def _instrument(self, win) -> None:
        if self._instrumented is win:
            return
        self._instrumented = win
        model = importlib.import_module("fomod_logger_support.viewer.model")
        if not getattr(model.load, "_live_timed", False):
            original = model.load

            def timed_load(*args, **kwargs):
                start = wall = _now()
                try:
                    return original(*args, **kwargs)
                finally:
                    self._window_events.append({"kind": "load", "at": start, "wall": wall, "ms": (_now() - start) * 1000})
            timed_load._live_timed = True
            model.load = timed_load
        original_render = win._render

        def timed_render(*args, **kwargs):
            start = wall = _now()
            try:
                return original_render(*args, **kwargs)
            finally:
                self._window_events.append({"kind": "render", "at": start, "wall": wall, "ms": (_now() - start) * 1000})
        win._render = timed_render

    @staticmethod
    def _cards(layout) -> list:
        out = []
        for i in range(layout.count()):
            widget = layout.itemAt(i).widget()
            if widget is None:
                continue
            if widget.objectName() == "card":
                labels = [{"role": l.objectName(), "text": l.text()} for l in widget.findChildren(QLabel)]
                out.append({"title": labels[0]["text"] if labels else "", "lines": labels[1:],
                            "buttons": [b.text() for b in widget.findChildren(QPushButton)]})
            elif isinstance(widget, QLabel):
                out.append({"hint": widget.text()})
            elif isinstance(widget, QToolButton):
                out.append({"fold": widget.text()})
            elif widget.layout() is not None:
                out.extend(FomodCompanionDriver._cards(widget.layout()))
        return out

    def _tree_rows(self, item, depth: int = 0) -> dict:
        row = {"text": [item.text(c) for c in range(3)]}
        if item.toolTip(0):
            row["tip"] = item.toolTip(0)
        if item.childCount() and depth < 5:
            kids = [self._tree_rows(item.child(i), depth + 1) for i in range(item.childCount())]
            if not (len(kids) == 1 and kids[0]["text"][0] == "…"):
                row["children"] = kids
        return row

    def content(self, win, expand: bool = False) -> dict:
        if expand and win.toggle_all.text() == "Expand all":
            win.toggle_all.click()
            _spin(0.1)

        def label(l, rel=win):
            return l.text() if l.isVisibleTo(rel) else ""
        sections = {}
        for key, box, items, count in (
                ("missed", win.missed_box, win.missed_list, win.missed_count),
                ("masters", win.masters_box, win.masters_list, win.masters_count),
                ("off", win.off_box, win.off_list, win.off_count),
                ("changes", win.changes_box, win.changes_list, win.changes_count),
                ("review", win.review_box, win.review_list, win.review_count),
                ("cross", win.cross_box, win.cross_list, win.cross_count),
                ("problems", win.problem_box, win.problem_list, None)):
            sections[key] = {"visible": box.isVisibleTo(win), "count": label(count) if count is not None else "",
                             "cards": self._cards(items)}
        tree = [self._tree_rows(win.tree.topLevelItem(i)) for i in range(win.tree.topLevelItemCount())]
        hint = getattr(win, "scan_hint", None)
        return {
            "scan_hint": {"visible": bool(hint is not None and hint.isVisibleTo(win)),
                          "text": label(win.scan_hint_label) if hint is not None else ""},
            "header": {"summary": label(win.summary_label), "missed": label(win.missed_chip),
                       "masters": label(win.masters_chip), "off": label(win.off_chip),
                       "revisit": label(win.revisit_chip)},
            "sections": sections, "tree": tree, "error": label(win.error_label),
            "folded": {key: arrow.isChecked() for key, arrow in getattr(win, "_arrows", {}).items()},
            "visible": win.isVisible(), "size": [win.width(), win.height()],
            "geometry": [win.x(), win.y(), win.width(), win.height()], "screen": win.screen().name(),
            "colours": {k: v.name() for k, v in win._colours.items()},
        }

    def signature(self) -> str | None:
        win = self.window()
        if win is None or not win.isVisible():
            return None
        data = self.content(win)
        data.pop("size", None)
        data.pop("geometry", None)
        data.pop("screen", None)
        return json.dumps(data, sort_keys=True)

    def _drain_events(self) -> list:
        events, self._window_events = self._window_events, []
        base = self._t0
        return [{"kind": e["kind"], "t": round(e["at"] - base, 3), "wall": e["wall"], "ms": round(e["ms"], 2)} for e in events]

    def measure(self, action, timeout: float = 4.0, settle: float = 0.7) -> dict:
        """Run ``action`` and time how long the open window takes to change."""

        win = self.ensure_window()
        sig0 = self.signature()
        self._drain_events()
        self._probe_start()
        t0 = _now()
        value = action()
        t_action = _now()
        first = None
        last_change = None
        sig = sig0
        end = t0 + timeout
        while _now() < end:
            _spin(0.01)
            current = self.signature()
            if current != sig:
                sig = current
                last_change = _now()
                if first is None:
                    first = last_change
            if last_change is not None and _now() - last_change >= settle:
                break
        events = self._drain_events()
        signals = self._probe_take()
        return {
            "changed": first is not None,
            "signals": [{"kind": e["kind"], "ms": round((e["at"] - t0) * 1000, 1), "detail": e["detail"]} for e in signals],
            "first_change_ms": round((first - t0) * 1000, 1) if first else None,
            "first_change_after_action_ms": round((first - t_action) * 1000, 1) if first else None,
            "last_change_ms": round((last_change - t0) * 1000, 1) if last_change else None,
            "action_ms": round((t_action - t0) * 1000, 1),
            "refreshes": sum(1 for e in events if e["kind"] == "render"),
            "loads_ms": [e["ms"] for e in events if e["kind"] == "load"],
            "renders_ms": [e["ms"] for e in events if e["kind"] == "render"],
            "value": value,
        }

    # ----- MO2's own change signals (what the Companion hooks) -----

    def _probe_register(self) -> dict:
        """Hook every list-change signal MO2 offers once, so a run can say which of them an action raised. The
        callbacks cannot be unhooked; they only record while a probe is running."""

        if self._probe_registered is not None:
            return self._probe_registered
        modlist, pluginlist = self._org.modList(), self._org.pluginList()

        def brief(args) -> str:
            parts = []
            for arg in args:
                if isinstance(arg, dict):
                    parts.append("{" + ", ".join(f"{k}: {getattr(v, 'name', str(v))}" for k, v in list(arg.items())[:6]) + "}")
                elif hasattr(arg, "name") and callable(arg.name):
                    parts.append(str(arg.name()))
                else:
                    parts.append(str(arg))
            return ", ".join(parts)[:200]

        def recorder(kind):
            def hook(*args):
                if self._probe_log is not None:
                    self._probe_log.append({"kind": kind, "at": _now(), "wall": _now(), "detail": brief(args)})
            self._probe_refs.append(hook)
            return hook

        outcome = {}
        for kind, owner, method in (("mod_state_changed", modlist, "onModStateChanged"), ("mod_moved", modlist, "onModMoved"),
                                    ("mod_installed", modlist, "onModInstalled"), ("mod_removed", modlist, "onModRemoved"),
                                    ("plugin_state_changed", pluginlist, "onPluginStateChanged"),
                                    ("plugin_moved", pluginlist, "onPluginMoved"), ("plugins_refreshed", pluginlist, "onRefreshed")):
            try:
                outcome[kind] = getattr(owner, method)(recorder(kind))
            except Exception as exc:
                outcome[kind] = f"{type(exc).__name__}: {exc}"
        self._probe_registered = outcome
        return outcome

    def _probe_start(self) -> None:
        self._probe_register()
        self._probe_log = []

    def _probe_take(self) -> list[dict]:
        log, self._probe_log = self._probe_log or [], None
        return log

    # ----- watching the window while somebody else acts -----

    def cmd_watch_start(self, c) -> dict:
        """Start timing: the window's content is compared every few ms and each change is stamped with the wall
        clock; MO2's change signals and the window's own load/render calls are stamped the same way."""

        self.ensure_window()
        if self._watch is not None:
            self._watch["timer"].stop()
        self._drain_events()
        self._probe_start()
        timer = QTimer()
        timer.setInterval(int(c.get("interval_ms", 10)))
        timer.timeout.connect(self._watch_tick)
        self._watch = {"sig": self.signature(), "changes": [], "timer": timer, "started": _now()}
        timer.start()
        return {"wall": self._watch["started"], "hooks": self._probe_registered}

    def _watch_tick(self) -> None:
        watch = self._watch
        if watch is None:
            return
        sig = self.signature()
        if sig != watch["sig"]:
            watch["sig"] = sig
            watch["changes"].append(_now())

    def _watch_report(self, stop: bool) -> dict:
        watch = self._watch
        if watch is None:
            raise RuntimeError("no watch is running (watch_start first)")
        if stop:
            watch["timer"].stop()
        log = self._probe_log or []
        events = [{"kind": e["kind"], "wall": e["wall"], "ms": round(e["ms"], 2)} for e in self._window_events]
        report = {"changes": list(watch["changes"]), "events": events,
                  "signals": [{"kind": e["kind"], "wall": e["wall"], "detail": e["detail"]} for e in log]}
        if stop:
            self._watch = None
            self._probe_take()
            self._window_events.clear()
        return report

    def cmd_watch_peek(self, c) -> dict:
        return self._watch_report(stop=False)

    def cmd_watch_stop(self, c) -> dict:
        return self._watch_report(stop=True)

    # ----- installer dialogs of an installMod that somebody else starts -----

    def cmd_install_arm(self, c) -> dict:
        """Start driving MO2's installer dialogs (FOMOD choices, 'Mod Exists') from a timer and return at once. The
        caller then starts the install (for example the bridge's ``install_archive``); the dialogs are handled
        whenever they appear, whoever started the install. ``install_report`` ends the session."""

        if self._session is not None:
            self._session.stop()
        self._drain_events()
        self._session = InstallSession(self, c)
        self._session.start()
        return {"armed": True, "wall": _now()}

    def cmd_install_report(self, c) -> dict:
        session = self._session
        if session is None:
            raise RuntimeError("no install session is armed")
        if c.get("wait_for_window") and self.window() is not None:
            # The event is imported and the window redrawn a moment after the install returns.
            _wait_until(lambda: any(e["kind"] == "render" for e in self._window_events), 3.0, 0.005)
            _spin(float(c.get("settle", 0.4)))
        session.stop()
        self._session = None
        events = self._drain_events()
        return {"session": session.report(), "window_events": events}

    # ----- commands -----

    def cmd_ping(self, c) -> dict:
        main = self.main_window()
        return {"profile": self._org.profileName(), "base": str(self._org.basePath()),
                "mods_path": str(self._org.modsPath()), "main_window": bool(main),
                "screen": right_screen().name(), "screens": [s.name() for s in QGuiApplication.screens()],
                "command_sources": self._source, "version": str(self._org.appVersion().canonicalString())}

    def cmd_logger_problems(self, c) -> dict:
        """What MO2's notification dialog would list for the Companion: every active problem with its short and
        full description and whether it has a guided fix."""

        plugin = self.logger_plugin()
        problems = [{"key": int(key), "short": plugin.shortDescription(key), "full": plugin.fullDescription(key),
                     "guided_fix": bool(plugin.hasGuidedFix(key))} for key in plugin.activeProblems()]
        setting = self._org.pluginSetting(plugin.name(), "alert_new_patches")
        return {"problems": problems, "alert_new_patches": setting}

    def cmd_logger_fix(self, c) -> dict:
        """Press Fix on one problem (``key``), as MO2's dialog does, then report the window and the problem list."""

        plugin = self.logger_plugin()
        before = self.window()
        was_visible = bool(before is not None and before.isVisible())
        plugin.startGuidedFix(int(c["key"]))
        _spin(float(c.get("wait", 0.8)))
        win = self.window()
        return {"was_visible": was_visible, "window_visible": bool(win is not None and win.isVisible()),
                "problems_after": [int(key) for key in plugin.activeProblems()]}

    def cmd_state(self, c) -> dict:
        only = c.get("plugins")
        data = {"mods": self._mods()}
        if c.get("all_plugins") or only:
            data["plugins"] = self._plugin_states(only)
        else:
            data["plugins"] = {n: v for n, v in self._plugin_states().items() if n.startswith("LT_")}
        return data

    def cmd_refresh(self, c) -> dict:
        self._org.refresh(True)
        _spin(float(c.get("wait", 1.0)))
        return {}

    def cmd_eval(self, c) -> dict:
        env = {"self": self, "org": self._org, "mobase": mobase, "Qt": Qt, "QApplication": QApplication,
               "result": None, "spin": _spin}
        exec(c["code"], env)
        return {"result": env["result"]}

    @staticmethod
    def _section_box(win, key: str):
        return {"missed": win.missed_box, "masters": win.masters_box, "off": win.off_box, "review": win.review_box,
                "cross": win.cross_box, "changes": win.changes_box, "problems": win.problem_box}[key]

    def cmd_click_card(self, c) -> dict:
        """Press a button on a card of a side section: ``section`` ("changes", "missed", ...), ``title`` (the card's
        title), ``button`` (its label). Cards inside a closed fold ("Seen (1)") are not built yet: open it first with
        window_state's ``folds``."""

        win = self.ensure_window()
        box = self._section_box(win, c["section"])
        for card in box.findChildren(QFrame, "card"):
            labels = card.findChildren(QLabel)
            if not card.isVisibleTo(win) or not labels or labels[0].text() != c["title"]:
                continue
            for button in card.findChildren(QPushButton):
                if button.text() == c["button"]:
                    button.click()
                    _spin(float(c.get("wait", 0.5)))  # the card's button acts once its click has returned
                    return {"clicked": c["button"], "card": c["title"]}
        raise RuntimeError(f"no visible {c['section']} card {c['title']!r} with a {c['button']!r} button")

    def cmd_window_state(self, c) -> dict:
        win = self.ensure_window()
        if c.get("wait"):
            _spin(float(c["wait"]))
        for key in c.get("folds") or []:  # open the closed groups ("Seen (1)") of these sections so their cards are built
            for fold in self._section_box(win, key).findChildren(QToolButton, "fold"):
                if fold.isVisibleTo(win) and fold.isCheckable() and not fold.isChecked():
                    fold.setChecked(True)
            _spin(0.2)
        data = self.content(win, expand=bool(c.get("expand")))
        data["opened_via"] = getattr(self, "_last_route", "")
        if c.get("events"):
            data["window_events"] = self._drain_events()
        return data

    def cmd_screenshot(self, c) -> dict:
        target = c.get("target", "window")
        if target == "window":
            widget = self.ensure_window()
        elif target == "main":
            widget = self.main_window()
        elif target == "dialog":
            widget = self.ensure_window()._origin_dialog
            if widget is None:
                raise RuntimeError("the Which option installed dialog is not open")
        elif target == "compare":
            widget = self.ensure_window()._compare_dialog
            if widget is None:
                raise RuntimeError("the Compare choices dialog is not open")
        elif target == "scan":
            widget = self.ensure_window()._scan_dialog
            if widget is None:
                raise RuntimeError("the Scan existing mods dialog is not open")
        else:
            raise ValueError("target must be window, main, dialog, compare or scan")
        if c.get("expand"):
            self.content(widget, expand=True)
        _spin(0.3)
        path = Path(c["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        pixmap = widget.grab()
        if not pixmap.save(str(path)):
            raise RuntimeError(f"could not save {path}")
        return {"path": str(path), "size": [pixmap.width(), pixmap.height()]}

    def cmd_idle_probe(self, c) -> dict:
        """Do nothing for a while and report whether the window redrew (polling check)."""

        self.ensure_window()
        self._drain_events()
        _spin(float(c.get("seconds", 5)))
        events = self._drain_events()
        return {"seconds": float(c.get("seconds", 5)), "loads": sum(e["kind"] == "load" for e in events),
                "renders": sum(e["kind"] == "render" for e in events)}

    def cmd_set_plugin(self, c) -> dict:
        name, on = c["name"], bool(c["on"])
        via = c.get("via", "ui")
        self._wait_plugins([name], float(c.get("wait_for_plugin", 6)))

        def act():
            if via == "api":
                self._org.pluginList().setState(name, mobase.PluginState.ACTIVE if on else mobase.PluginState.INACTIVE)
            else:
                view = self.main_window().findChild(QTreeView, "espList")
                model = view.model()
                for row in range(model.rowCount()):
                    index = model.index(row, 0)
                    if str(index.data()) == name:
                        if not model.setData(index, Qt.CheckState.Checked.value if on else Qt.CheckState.Unchecked.value, _ROLE_CHECK):
                            raise RuntimeError(f"the plugin list refused to change {name}")
                        break
                else:
                    raise RuntimeError(f"{name} is not in the plugin list")
            return self._plugin_states([name]).get(name)

        if c.get("measure", True):
            return self.measure(act, timeout=float(c.get("window_timeout", 4)))
        return {"value": act()}

    def _wait_plugins(self, names: list, timeout: float) -> bool:
        listed = lambda: {str(n) for n in self._org.pluginList().pluginNames()}
        return _wait_until(lambda: all(n in listed() for n in names), timeout)

    def cmd_wait_plugins(self, c) -> dict:
        names = list(c["names"])
        ok = self._wait_plugins(names, float(c.get("timeout", 8)))
        return {"listed": ok, "plugins": self._plugin_states([n for n in names if ok])}

    def cmd_set_mod(self, c) -> dict:
        name, on = c["name"], bool(c["on"])

        def act():
            if not self._org.modList().setActive(name, on):
                raise RuntimeError(f"MO2 did not change {name}")
            if on and c.get("wait_plugins"):
                self._wait_plugins(list(c["wait_plugins"]), float(c.get("plugin_wait", 6)))
            return {**self._mods().get(name), "plugin_states": self._plugin_states(
                [p for p in c.get("wait_plugins", [])]) if on and c.get("wait_plugins") else {}}

        if c.get("measure", True):
            return self.measure(act, timeout=float(c.get("window_timeout", 4)))
        return {"value": act()}

    def cmd_remove_mod(self, c) -> dict:
        name = c["name"]

        def act():
            mod = self._org.modList().getMod(name)
            if mod is None:
                raise RuntimeError(f"{name} is not installed")
            ok = self._org.modList().removeMod(mod)
            self._org.refresh(True)
            _spin(0.5)
            return {"removed": bool(ok), "still_listed": name in self._mods()}

        if c.get("measure", True):
            return self.measure(act, timeout=float(c.get("window_timeout", 4)))
        return {"value": act()}

    def cmd_search_window(self, c) -> dict:
        """Type ``query`` in the window's search box (what a user does, after the typing delay) and return the window's
        content. ``open_files`` opens that many "Files (N)" rows, so their lists are in the returned tree."""

        win = self.ensure_window()
        win.search.setText(str(c.get("query", "")))
        win._search_timer.stop()
        win._apply_search()
        _spin(float(c.get("wait", 0.3)))
        self.content(win, expand=True)  # builds the rows of every install, so the Files rows exist
        holders = []

        def walk(item):
            if item.text(0).startswith("Files ("):
                holders.append(item)
            for index in range(item.childCount()):
                walk(item.child(index))

        for index in range(win.tree.topLevelItemCount()):
            walk(win.tree.topLevelItem(index))
        for holder in holders[:int(c.get("open_files", 0))]:
            holder.setExpanded(True)  # as a click does: the rows are made when it opens
        _spin(0.1)
        return self.content(win)

    def cmd_which_option(self, c) -> dict:
        """Open the More menu's "Which option installed...?" (its action is triggered, as the menu item does), type
        ``query`` in the box and return what the dialog shows. ``close`` closes it afterwards."""

        win = self.ensure_window()
        if win._origin_dialog is None or not win._origin_dialog.isVisible():
            action = next((a for a in win.findChildren(QAction) if a.text() == "Which option installed…?"), None)
            if action is None:
                raise RuntimeError("the More menu has no 'Which option installed...?' action")
            action.trigger()
        dialog = win._origin_dialog
        dialog.edit.setText(str(c.get("query", "")))  # as typing does: the answer follows after a short delay
        _spin(0.7)  # past the typing delay
        found = {"title": dialog.windowTitle(), "visible": dialog.isVisible(), "message": dialog.message.text(),
                 "results": dialog.results_text(), "notes": dialog.notes.text(), "screen": dialog.screen().name()}
        if c.get("close"):
            dialog.close()
        return found

    def cmd_profiles(self, c) -> dict:
        """What the window knows about the profiles of this instance (read from their modlist.txt files): the profile
        names, and for each mod in ``mods`` the profiles that have it enabled. A fresh read is asked for first."""

        win = self.ensure_window()
        win.refresh(force=True)
        data = win._profile_data or {}
        wanted = [str(name) for name in c.get("mods", [])]
        return {"folder": data.get("folder"), "profiles": data.get("profiles"),
                "mods": {name: data.get("mods", {}).get(name.casefold(), []) for name in wanted}}

    def cmd_compare_choices(self, c) -> dict:
        """Open the More menu's "Compare choices..." (its action is triggered, as the menu item does), pick a comparison
        and return what the dialog shows. ``kind`` is instances, copies or installs; for the last two ``a`` and ``b`` are
        a mod name, or [mod name, n] for that mod's n-th newest install (in this instance). ``identical`` also lists what
        is the same; ``close`` closes the dialog afterwards."""

        win = self.ensure_window()
        if win._compare_dialog is None or not win._compare_dialog.isVisible():
            action = next((a for a in win.findChildren(QAction) if a.text() == "Compare choices…"), None)
            if action is None:
                raise RuntimeError("the More menu has no 'Compare choices...' action")
            action.trigger()
        dialog = win._compare_dialog
        dialog.reload()
        chosen = None
        kind = c.get("kind", "")
        if kind:
            events = dialog._data["events"]
            here = dialog._data["this_instance"]

            def find(ref) -> str:
                name, n = (ref, 0) if isinstance(ref, str) else (ref[0], int(ref[1]))
                found = sorted((e for e in events if e["instance"]["id"] == here and e["mod"]["name"].casefold() == name.casefold()),
                               key=lambda e: e["completed_at"], reverse=True)
                return found[n]["event_id"]

            a, b = c.get("a"), c.get("b")
            if kind != "instances":
                a, b = find(a), find(b)
            chosen = dialog.choose(kind, a, b)
        dialog.identical_box.setChecked(bool(c.get("identical")))
        dialog.expand_all()
        _spin(0.3)
        found = {"visible": dialog.isVisible(), "chosen": chosen, "kinds": [dialog.kind_box.itemData(i) for i in range(dialog.kind_box.count())],
                 "kind": dialog.kind, "scope": dialog.scope_box.currentText(), "summary": dialog.summary_text(),
                 "legend": dialog.legend.text(), "rows": dialog.rows(), "screen": dialog.screen().name(),
                 "groups": [[e["mod"]["name"] for e in g["mods"]] for g in dialog._groups]}
        if c.get("close"):
            dialog.close()
        return found

    def cmd_export_choices(self, c) -> dict:
        """Export the choices file or the install guide from the open window (the "More" menu's actions, given a path
        so no file dialog opens): ``kind`` "choices" or "guide", ``path``."""

        win = self.ensure_window()
        method = {"choices": win.export_choices_file, "guide": win.export_install_guide}[c.get("kind", "choices")]
        return {"mods": method(str(c["path"]), quiet=True)}

    def cmd_import_choices(self, c) -> dict:
        """Import a choices file through the window's "Import choices file" action (``path``); the summary is returned
        and no message box opens. A refused file comes back as ``error``."""

        win = self.ensure_window()
        try:
            summary = win.import_choices_file(str(c["path"]), quiet=True)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"summary": {"imported": summary.imported, "own_wins": summary.own_wins,
                            "not_installed": summary.not_installed, "duplicates": summary.duplicates,
                            "replaced": summary.replaced, "own": summary.own, "new": summary.new},
                "message": summary.message()}

    def cmd_remove_imported(self, c) -> dict:
        """The window's "Remove imported choices" action, without its question."""

        win = self.ensure_window()
        win._update_import_actions()
        enabled = win.remove_imported_action.isEnabled()
        return {"enabled_before": enabled, "removed": win.remove_imported_choices(quiet=True)}

    def cmd_imported_state(self, c) -> dict:
        """The imported records on disk for this instance: mod names and imported_at, and the menu action's state."""

        from fomod_logger_support.core import import_choices
        win = self.ensure_window()
        win._update_import_actions()
        root = self.logger_plugin()._data_root
        found = import_choices.list_imported(root, win._this_instance())
        return {"mods": sorted(r["entry"]["name"] for r in found), "files": sorted(Path(r["path"]).name for r in found),
                "remove_enabled": win.remove_imported_action.isEnabled(), "folder": str(import_choices.imported_folder(root))}

    def cmd_forget_own_events(self, c) -> dict:
        """TEST ONLY: make this MO2 look like a PC that never installed ``mod`` through the Companion: delete that mod's
        install event files and their rows. (The run restores the Companion's data folder afterwards.)"""

        import sqlite3
        plugin = self.logger_plugin()
        root = Path(plugin._data_root)
        wanted = str(c["mod"]).casefold()
        ids = []
        for path in sorted((root / "events").glob("*.json")):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(event.get("mod", {}).get("name", "")).casefold() == wanted:
                ids.append(path.stem)
                path.unlink()
        rows = 0
        connection = sqlite3.connect(root / "fomod_logger.sqlite3")
        try:
            with connection:
                for table in ("reviews", "changes_seen"):
                    connection.executemany(f"DELETE FROM {table} WHERE event_id = ?", [(i,) for i in ids])
                connection.executemany("DELETE FROM patch_alerts WHERE install_event_id = ?", [(i,) for i in ids])
                connection.executemany("DELETE FROM patch_alerts WHERE event_id = ?", [(i,) for i in ids])
                for event_id in ids:
                    rows += connection.execute("DELETE FROM events WHERE event_id = ?", (event_id,)).rowcount
        finally:
            connection.close()
        win = self.window()
        if win is not None:
            win.refresh(force=True)
        _spin(0.3)
        return {"files_deleted": len(ids), "rows_deleted": rows}

    # ----- the FOMOD Plus edition -----

    def cmd_mod_record(self, c) -> dict:
        """What FOMOD Plus saved for a mod, read through MO2 (``IModInterface.pluginSetting``), and where the mod's
        archive is according to MO2."""

        mod = self._org.modList().getMod(str(c["name"]))
        if mod is None:
            raise RuntimeError(f"{c['name']} is not installed")
        value = mod.pluginSetting("FOMOD Plus", "fomod", "")
        return {"record": value if isinstance(value, str) else None, "installation_file": str(mod.installationFile()),
                "version": str(mod.version().canonicalString()), "nexus_id": int(mod.nexusId())}

    def cmd_plus_state(self, c) -> dict:
        """The Plus edition's own view: whether FOMOD Plus is enabled, the edition's MO2 problems, its pending rebuilds
        and the digests it remembers."""

        plugin = self.logger_plugin()
        records = getattr(plugin, "_records", None)
        instance = plugin._instance()[0] if hasattr(plugin, "_instance") else ""
        return {"class": type(plugin).__name__, "name": plugin.name(), "data_root": str(plugin._data_root),
                "fomod_plus_enabled": bool(self._org.isPluginEnabled("FOMOD Plus")),
                "pending": dict(getattr(plugin, "_pending", {})), "active": bool(plugin._active),
                "problems": [{"key": int(k), "short": plugin.shortDescription(k), "full": plugin.fullDescription(k)}
                             for k in plugin.activeProblems()],
                "remembered": {k: dict(v) for k, v in (records._data.get(instance, {}) if records else {}).items()}}

    def cmd_wait_plus_idle(self, c) -> dict:
        """Wait until no rebuild is pending (the install's event is stored and the window told), then let the window settle."""

        plugin = self.logger_plugin()
        ok = _wait_until(lambda: not getattr(plugin, "_pending", {}), float(c.get("timeout", 20)))
        _spin(float(c.get("settle", 0.5)))
        return {"idle": ok}

    def cmd_forget_plus_events(self, c) -> dict:
        """TEST ONLY: make this MO2 look like one that had FOMOD Plus install ``mods`` before Companion was there: delete
        those mods' install rows and what the edition remembers about their records. (The run restores the data folder.)"""

        import sqlite3
        plugin = self.logger_plugin()
        instance_id = plugin._instance()[0]
        connection = sqlite3.connect(Path(plugin._data_root) / "fomod_logger.sqlite3")
        rows = 0
        try:
            with connection:
                for name in c["mods"]:
                    ids = [r[0] for r in connection.execute(
                        "SELECT event_id FROM events WHERE instance_id = ? AND lower(mod_name) = lower(?)", (instance_id, name))]
                    for table, column in (("reviews", "event_id"), ("changes_seen", "event_id"), ("patch_alerts", "install_event_id"),
                                          ("patch_alerts", "event_id")):
                        connection.executemany(f"DELETE FROM {table} WHERE {column} = ?", [(i,) for i in ids])
                    rows += connection.execute("DELETE FROM events WHERE instance_id = ? AND lower(mod_name) = lower(?)",
                                               (instance_id, name)).rowcount
                    plugin._records.forget(instance_id, str(name).casefold())
        finally:
            connection.close()
        win = self.window()
        if win is not None:
            win.refresh(force=True)
        _spin(0.3)
        return {"rows_deleted": rows}

    def cmd_scan_existing(self, c) -> dict:
        """The More menu's "Scan existing mods…" (``cancel_at`` N: press Cancel once N mods are done, 0 straight away),
        waited for until the dialog is idle again. Returns what the dialog says."""

        win = self.ensure_window()
        count = win._scanner.count()
        if not count:
            return {"started": False, "candidates": 0}      # (the menu item would say so in a message box, which waits for a click)
        dialog = win.scan_existing()
        if dialog is None:
            return {"started": False, "candidates": count}
        shown = {"status": dialog.status.text(), "button": dialog.button.text()}
        cancel_at = c.get("cancel_at")
        pressed = None
        if cancel_at is not None:
            end = _now() + 20
            while _now() < end and dialog.running and dialog.bar.value() < int(cancel_at):
                _spin(0.005)
            if dialog.running:
                pressed = dialog.bar.value()
                dialog.button.click()
        if c.get("shot"):
            Path(c["shot"]).parent.mkdir(parents=True, exist_ok=True)
            dialog.grab().save(str(c["shot"]))
        _wait_until(lambda: not dialog.running, float(c.get("timeout", 60)))
        _spin(0.4)
        summary = dialog.summary
        return {"started": True, "candidates": count, "first": shown, "cancel_pressed_at": pressed,
                "running": dialog.running, "status": dialog.status.text(), "button": dialog.button.text(),
                "progress": [dialog.bar.value(), dialog.bar.maximum()], "bar_text": dialog.bar.text(),
                "result": dialog.result.text(), "visible": dialog.isVisible(),
                "summary": ({"total": summary.total, "rebuilt": summary.rebuilt, "names_only": summary.names_only,
                             "plain": summary.plain,
                             "failed": summary.failed, "skipped": summary.skipped, "cancelled": summary.cancelled,
                             "problems": summary.problems} if summary is not None else None)}

    def cmd_scan_jobs(self, c) -> dict:
        """What "Scan existing mods…" would do now: (mod name, kind of job) for each mod, as the plugin lists them."""

        win = self.ensure_window()
        jobs = win._scanner._jobs()
        return {"jobs": [[job.mod_name, type(job).__name__] for job in jobs], "count": len(jobs)}

    def cmd_scan_hint(self, c) -> dict:
        """The quiet line about mods that are not recorded yet: ``refresh`` asks the window to look again, ``press``
        ("scan", "later" or "never") clicks one of its buttons. Returns what the line says."""

        win = self.ensure_window()
        if c.get("refresh"):
            win._update_scan_hint()
        if c.get("press") == "later":
            win.scan_hint_later.click()
        elif c.get("press") == "never":
            win.scan_hint_never.click()
        _spin(0.2)
        return {"visible": win.scan_hint.isVisibleTo(win), "text": win.scan_hint_label.text(),
                "buttons": [win.scan_hint_scan.text(), win.scan_hint_later.text(), win.scan_hint_never.text()],
                "dismissed": bool(win._settings.get("scan_hint_dismissed"))}

    def cmd_set_persistent(self, c) -> dict:
        """Set one of MO2's persistent plugin values (IOrganizer.setPersistent): ``plugin``, ``key``, ``value``. The
        "enabled" key is what MO2's plugin settings page switches, so ``isPluginEnabled`` follows it. TEST ONLY."""

        self._org.setPersistent(str(c["plugin"]), str(c["key"]), c["value"], True)
        # MO2's settings page also raises plugin-enabled / plugin-disabled; this call does not, so say it to the Companion
        # the way those signals do (its look at other plugins runs on MO2's thread, which this is).
        companion = next((o for o in __import__("gc").get_objects() if type(o).__name__ in _COMPANION_CLASSES), None)
        if companion is not None and hasattr(companion, "_look_at_other_plugins"):
            companion._look_at_other_plugins()
        return {"now": self._org.persistent(str(c["plugin"]), str(c["key"]), None),
                "enabled": bool(self._org.isPluginEnabled(str(c["plugin"])))}

    def cmd_set_setting(self, c) -> dict:
        """Set a plugin setting through IOrganizer.setPluginSetting (``plugin``, ``key``, ``value``). TEST ONLY."""

        self._org.setPluginSetting(str(c["plugin"]), str(c["key"]), c["value"])
        return {"now": self._org.pluginSetting(str(c["plugin"]), str(c["key"]))}

    def cmd_close_scan_dialog(self, c) -> dict:
        win = self.window()
        dialog = getattr(win, "_scan_dialog", None) if win is not None else None
        if dialog is not None:
            dialog.hide()
        return {"closed": dialog is not None}

    def cmd_menu_actions(self, c) -> dict:
        """The texts of the window's More-menu actions."""

        win = self.ensure_window()
        return {"actions": [a.text() for a in win.findChildren(QAction) if a.text()], "title": win.windowTitle()}

    def cmd_history_state(self, c) -> dict:
        """The List history page: switch to it (``page``), apply what the command says (``search``, ``kinds_off``,
        ``kinds_on``, ``since`` true/false, ``profile`` text of the profile box, ``more`` to press Show more), open the
        grouped entries (``open``) and report what is listed."""

        win = self.ensure_window()
        if c.get("page"):
            win.show_page(c["page"])
        _spin(0.1)
        view = win.history
        if "search" in c:
            view.search.setText(c["search"])
            view._search_timer.stop()
            view._search_changed()
        for key in c.get("kinds_off") or []:
            view.kind_buttons[key].setChecked(False)
        for key in c.get("kinds_on") or []:
            view.kind_buttons[key].setChecked(True)
        if c.get("since") is not None and view.since_button.isChecked() != bool(c["since"]):
            view.since_button.click()
        if c.get("profile") is not None:
            index = view.profile_box.findText(c["profile"])
            if index < 0:
                raise RuntimeError(f"no profile {c['profile']!r} in {[view.profile_box.itemText(i) for i in range(view.profile_box.count())]}")
            view.profile_box.setCurrentIndex(index)
        if c.get("more"):
            view.more_button.click()
        view.reload(bool(c.get("force", True)))
        if c.get("open"):
            view.open_groups(None)
            _spin(0.1)
        _spin(float(c.get("wait", 0.2)))
        rows = []
        for index in range(view.tree.topLevelItemCount()):
            day = view.tree.topLevelItem(index)
            for number in range(day.childCount()):
                item = day.child(number)
                rows.append({"day": day.text(0), "text": item.text(0), "detail": item.text(1), "time": item.text(2),
                             "bold": bool(item.font(0).bold()), "children": [item.child(n).text(0) for n in range(item.childCount())]})
        return {"page": win.page, "summary": view.summary.text(), "chip": win.history_chip.text(), "total": view.total,
                "shown": len(view.entries), "rows": rows, "installer_chips_visible": win.installer_chips.isVisibleTo(win),
                "since": {"enabled": view.since_button.isEnabled(), "checked": view.since_button.isChecked()},
                "kinds": {key: button.isChecked() for key, button in view.kind_buttons.items()},
                "profiles": [view.profile_box.itemText(i) for i in range(view.profile_box.count())],
                "profile": view.profile_box.currentText(), "empty": view.empty.text() if view.empty.isVisibleTo(win) else "",
                "more_visible": view.more_button.isVisibleTo(win), "export_enabled": view.export_button.isEnabled(),
                "error": getattr(view, "_error", "")}

    def cmd_history_export(self, c) -> dict:
        win = self.ensure_window()
        written = win.history.export(str(c["path"]), quiet=True)
        return {"written": written}

    def cmd_history_info(self, c) -> dict:
        """What the plugin has set up for the history: the callbacks MO2 accepted, and what the tracker last saw."""

        plugin = self.logger_plugin()
        tracker = plugin._history
        cost = {}
        if c.get("cost"):
            # What the history adds to each look at the list, in this MO2's real lists: the snapshot (with the mods'
            # versions read for the first time, then kept) and the diff of two looks.
            mod_list, plugins = self._org.modList(), self._org.pluginList()
            names = [str(n) for n in plugins.pluginNames()]
            active = [n for n in names if plugins.state(n) == mobase.PluginState.ACTIVE]
            enabled = [str(n) for n in mod_list.allMods() if mod_list.state(n) & mobase.ModState.ACTIVE]
            kept = dict(plugin._history_versions)
            plugin._history_versions.clear()
            history = importlib.import_module("fomod_logger_support.core.history")
            started = _now()
            first = plugin._history_snapshot(mod_list, plugins, names, active, enabled)
            first_ms = (_now() - started) * 1000
            started = _now()
            second = plugin._history_snapshot(mod_list, plugins, names, active, enabled)
            second_ms = (_now() - started) * 1000
            started = _now()
            history.diff(first, second)
            diff_ms = (_now() - started) * 1000
            plugin._history_versions.clear()
            plugin._history_versions.update(kept)
            cost = {"mods": len(first["mods"]), "plugins": len(names),
                    "snapshot_first_ms": round(first_ms, 2), "snapshot_again_ms": round(second_ms, 2), "diff_ms": round(diff_ms, 3)}
        return {"cost": cost, "callbacks": sorted(plugin._registered_callbacks), "active": bool(plugin._active), "ready": bool(plugin._ready),
                "last_seen": (None if tracker.last is None else {"mods": len(tracker.last["mods"]), "plugins": len(tracker.last.get("plugins") or [])}),
                "key": list(tracker.key) if tracker.key else None, "switch_pending": bool(tracker.switch_pending),
                "profile": plugin._profile_name()}

    def cmd_switch_profile(self, c) -> dict:
        """Pick a profile in MO2's main window (the profile box), as a user does, and wait until MO2 has switched."""

        main = self.main_window()
        box = main.findChild(QComboBox, "profileBox") if main is not None else None
        if box is None:
            raise RuntimeError("MO2's profile box was not found")
        index = box.findText(c["name"])
        if index < 0:
            raise RuntimeError(f"no profile {c['name']!r} in {[box.itemText(i) for i in range(box.count())]}")
        before = str(self._org.profileName())
        box.setCurrentIndex(index)
        _wait_until(lambda: str(self._org.profileName()) == c["name"], float(c.get("timeout", 30)))
        _spin(float(c.get("wait", 2.0)))
        return {"before": before, "after": str(self._org.profileName())}

    def cmd_move_mod(self, c) -> dict:
        """Move a mod in the left pane (IModList.setPriority), as a drag would."""

        mod_list = self._org.modList()
        before = int(mod_list.priority(c["name"]))
        done = mod_list.setPriority(c["name"], int(c["priority"]))
        _spin(float(c.get("wait", 0.8)))
        return {"moved": bool(done), "before": before, "after": int(mod_list.priority(c["name"]))}

    def cmd_quit(self, c) -> dict:
        main = self.main_window()
        QTimer.singleShot(300, lambda: main.close() if main is not None else QCoreApplication.quit())
        return {"closing": True}

    def _installed_name(self, wanted: str, before: set) -> str | None:
        """The name of the mod an install made: the one new mod, or (a replace) the name asked for."""

        after = set(self._mods())
        new = sorted(after - before)
        if len(new) == 1:
            return new[0]
        return wanted if wanted in after else (new[0] if new else None)

    def cmd_install(self, c) -> dict:
        archive, name = str(c["archive"]), str(c["name"])
        session = InstallSession(self, c)
        before = set(self._mods())
        self._drain_events()
        started = _now()
        session.start()
        # ``installer`` "stock": FOMOD Plus is switched off (its "enabled" setting, the one MO2's plugin settings page
        # keeps) while the install runs, so MO2's own FOMOD installer takes the archive; ``plus_record`` is then saved on
        # the new mod through the plugin-setting API the way FOMOD Plus saves its choices at the end of an install.
        stock = c.get("installer") == "stock"
        if stock:
            self._org.setPersistent("FOMOD Plus", "enabled", False, True)
        installed_name = None
        try:
            # The pointer installMod returns is only good for "was there a mod": an installer (FOMOD Plus) may refresh
            # MO2's mod list, which frees the object it points to. Nothing is ever called on it; the mod is looked up
            # again by name.
            installed = self._org.installMod(archive, name) is not None
            if stock:
                self._org.setPersistent("FOMOD Plus", "enabled", True, True)
                stock = False
            if installed:
                installed_name = self._installed_name(name, before)
            if installed and installed_name and c.get("plus_record") is not None:
                saved = self._org.modList().getMod(installed_name)
                saved.setPluginSetting("FOMOD Plus", "fomod", json.dumps(c["plus_record"]))
                self.log(f"  saved FOMOD Plus's record on {installed_name}")
        finally:
            if stock:
                self._org.setPersistent("FOMOD Plus", "enabled", True, True)
            session.stop()
        ended = _now()
        install_ms = round((ended - started) * 1000, 1)
        if installed and self.window() is not None:
            # The event is imported and the window redrawn a moment after installMod returns.
            _wait_until(lambda: any(e["kind"] == "render" for e in self._window_events), 3.0, 0.005)
            _spin(0.4)
        result = {"returned_mod": installed_name, "install_ms": install_ms,
                  "session": session.report()}
        # When did the open window redraw, relative to the end of installMod?
        result["window_timeline"] = [
            {"kind": e["kind"], "ms": round(e["ms"], 2), "after_install_end_ms": round((e["at"] - ended) * 1000, 1)}
            for e in self._window_events]
        self._window_events.clear()

        def finish():
            return None
        if not installed:
            return result
        mod_name = installed_name
        _spin(0.3)
        result["mod"] = self._mods().get(mod_name)
        result["new_mods"] = sorted(set(self._mods()) - before)
        return result


# ---------------------------------------------------------------------------

class InstallSession:
    """Drives MO2's dialogs during one ``installMod`` call, from a timer."""

    def __init__(self, driver: FomodCompanionDriver, command: dict) -> None:
        self.d = driver
        self.c = command
        self.timer = QTimer()
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.tick)
        self.started = _now()
        self.timeout = float(command.get("session_timeout", 90))
        self.events: list[str] = []
        self.pages: list[dict] = []
        self.warnings: list[str] = []
        self.dialogs: list[dict] = []
        self.done_pages: set[int] = set()
        self.dialog_seen = False
        self.first_info: dict = {}
        self.overwrite_done = 0
        self.finished_dialog = False
        self.capture_dir = Path(command["capture_dir"]) if command.get("capture_dir") else None
        self.shot_count = 0

    def start(self) -> None:
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()

    def note(self, text: str) -> None:
        self.events.append(f"+{_now() - self.started:6.2f}s {text}")
        self.d.log("  install: " + text)

    def report(self) -> dict:
        return {"events": self.events, "pages": self.pages, "warnings": self.warnings,
                "unexpected_dialogs": self.dialogs, "dialog_seen": self.dialog_seen, **self.first_info}

    def shoot(self, widget, label: str) -> None:
        if self.capture_dir is None:
            return
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.shot_count += 1
        target = self.capture_dir / f"{self.c.get('shot_prefix', 'install')}-{self.shot_count:02d}-{label}.png"
        try:
            widget.grab().save(str(target))
        except Exception as exc:
            self.warnings.append(f"screenshot failed: {exc}")

    # ----- dialogs -----

    def tick(self) -> None:
        try:
            self._tick()
        except Exception as exc:
            self.warnings.append(f"driver error: {type(exc).__name__}: {exc}")
            self.d.log(traceback.format_exc())
        if _now() - self.started > self.timeout:
            self.warnings.append("timed out; closing dialogs")
            for w in QApplication.topLevelWidgets():
                if isinstance(w, QDialog) and w.isVisible():
                    w.reject()

    def _tick(self) -> None:
        for w in list(QApplication.topLevelWidgets()):
            if not w.isVisible() or w is self.d.window() or w is self.d.main_window():
                continue
            if w.metaObject().className() == "FomodInstallerWindow":
                self.plus_fomod(w)           # FOMOD Plus's own dialog
            elif w.objectName() == "FomodInstallerDialog" or w.findChild(QStackedWidget, "stepsStack") is not None:
                self.fomod(w)
            elif w.findChild(QPushButton, "replaceBtn") is not None:
                self.overwrite(w)
            elif self.is_quick_install(w):
                self.quick_install(w)
            elif isinstance(w, QProgressDialog):
                continue
            elif isinstance(w, QMessageBox) or (isinstance(w, QDialog) and (w.findChildren(QPushButton) or w.findChildren(QDialogButtonBox))):
                self.unexpected(w)

    @staticmethod
    def is_quick_install(dlg) -> bool:
        """MO2's simple installer's box for a plain zip (no FOMOD): a name to edit and Manual, OK and Cancel."""

        names = {button.text().replace("&", "") for button in dlg.findChildren(QPushButton)}
        return {"OK", "Manual", "Cancel"} <= names and dlg.findChild(QComboBox) is not None

    def quick_install(self, dlg) -> None:
        if id(dlg) in getattr(self, "_handled", set()):
            return
        self._handled = getattr(self, "_handled", set()) | {id(dlg)}
        self.d._place(dlg)
        self.note(f"quick install box ({dlg.windowTitle()!r}); OK")
        self.shoot(dlg, "quick-install")
        next(b for b in dlg.findChildren(QPushButton) if b.text().replace("&", "") == "OK").click()

    def overwrite(self, dlg) -> None:
        if id(dlg) in getattr(self, "_handled", set()):
            return
        self._handled = getattr(self, "_handled", set()) | {id(dlg)}
        self.d._place(dlg)
        choice = self.c.get("overwrite", "replace")
        text = dlg.findChild(QWidget, "textBrowser")
        self.note(f"'Mod Exists' prompt (text: {text.toPlainText()[:80]!r}); choosing {choice}")
        self.shoot(dlg, "mod-exists")
        self.overwrite_done += 1
        button = dlg.findChild(QPushButton, {"replace": "replaceBtn", "merge": "mergeBtn", "rename": "renameBtn"}.get(choice, "cancelBtn"))
        button.click()

    def unexpected(self, dlg) -> None:
        if id(dlg) in getattr(self, "_handled", set()):
            return
        self._handled = getattr(self, "_handled", set()) | {id(dlg)}
        info = {"class": type(dlg).__name__, "title": dlg.windowTitle(),
                "text": " | ".join(l.text() for l in dlg.findChildren(QLabel) if l.text()),
                "buttons": [b.text() for b in dlg.findChildren(QPushButton)]}
        self.dialogs.append(info)
        self.warnings.append(f"unexpected dialog {info}")
        self.shoot(dlg, "unexpected-dialog")
        action = self.c.get("unexpected", "reject")
        self.note(f"unexpected dialog {info['title']!r}; {action}")
        dlg.reject()

    # ----- the FOMOD dialog -----

    @staticmethod
    def _group_boxes(page) -> list:
        boxes = page.findChildren(QGroupBox, "fomodLoggerGroup")
        if not boxes:
            # MO2's own FOMOD installer (no names of ours): the groups are the page's group boxes that hold options.
            return [g for g in page.findChildren(QGroupBox) if g is not page and g.findChildren(QAbstractButton, "choice")]
        return sorted(boxes, key=lambda g: g.property("fomodLoggerGroupIndex") or 0)

    @staticmethod
    def _buttons(group) -> list:
        layout = group.layout()
        found = [b for b in group.findChildren(QAbstractButton) if b.objectName() in ("choice", "none")]
        return sorted(found, key=lambda b: layout.indexOf(b) if layout is not None else 0)

    @staticmethod
    def _author_name(button) -> str:
        name = button.property("fomodLoggerName")
        return str(name) if name else button.text()

    def describe_page(self, dlg) -> dict:
        stack = dlg.findChild(QStackedWidget, "stepsStack")
        page = stack.currentWidget()
        groups = []
        for box in self._group_boxes(page):
            groups.append({"name": box.title(), "type": box.property("fomodLoggerGroupType"), "options": [
                {"name": self._author_name(b), "text": b.text(), "checked": b.isChecked(), "enabled": b.isEnabled(),
                 "none": b.objectName() == "none"} for b in self._buttons(box)]})
        nxt = dlg.findChild(QPushButton, "nextBtn")
        return {"index": stack.currentIndex(), "count": stack.count(), "title": page.title(), "groups": groups,
                "next_text": nxt.text(), "next_enabled": nxt.isEnabled()}

    # ----- FOMOD Plus's dialog -----
    #
    # FOMOD Plus (a C++ QDialog, class FomodInstallerWindow) builds one QGroupBox per step in a QStackedWidget, one
    # QGroupBox per group inside it, and a QCheckBox or QRadioButton per option whose text is the option's name and
    # whose objectName is "[<step>:<group>] <group>-<option>". The bottom row has QPushButtons "Back", "Next" (which
    # reads "Install" on the last visible step) and "Cancel", and the name of the mod is the one editable combo box.

    @staticmethod
    def _plus_pages(dlg) -> QStackedWidget | None:
        stacks = dlg.findChildren(QStackedWidget)
        return stacks[0] if stacks else None

    @staticmethod
    def _plus_buttons(group) -> list:
        layout = group.layout()
        found = [b for b in group.findChildren(QAbstractButton) if b.parentWidget() is group
                 and b.objectName().startswith("[")]
        return sorted(found, key=lambda b: layout.indexOf(b))

    def _plus_groups(self, page) -> list:
        return [g for g in page.findChildren(QGroupBox) if g is not page and self._plus_buttons(g)]

    def describe_plus_page(self, dlg) -> dict:
        stack = self._plus_pages(dlg)
        page = stack.currentWidget() if stack is not None else None
        title = page.title() if isinstance(page, QGroupBox) else ""
        groups = []
        for box in self._plus_groups(page) if isinstance(page, QGroupBox) else []:
            groups.append({"name": box.title(), "type": "radio" if self._plus_buttons(box)[0].autoExclusive() else "check",
                           "options": [{"name": b.text(), "text": b.text(), "checked": b.isChecked(), "enabled": b.isEnabled(),
                                        "object": b.objectName()} for b in self._plus_buttons(box)]})
        nxt = next((b for b in dlg.findChildren(QPushButton) if b.text() in ("Next", "Install")), None)
        return {"index": stack.currentIndex() if stack is not None else 0, "count": stack.count() if stack is not None else 0,
                "title": title, "groups": groups, "next_text": nxt.text() if nxt else "", "next_enabled": bool(nxt and nxt.isEnabled())}

    def plus_fomod(self, dlg) -> None:
        stack = self._plus_pages(dlg)
        if not self.dialog_seen:
            self.dialog_seen = True
            self.d._place(dlg)
            self.note("FOMOD Plus dialog opened")
            combo = next((c for c in dlg.findChildren(QComboBox) if c.isEditable()), None)
            if combo is not None:
                combo.setEditText(str(self.c["name"]))
            restore = next((b for b in dlg.findChildren(QPushButton) if b.text() == "Restore Previous Choices"), None)
            first = self.describe_plus_page(dlg)
            self.first_info = {"first_page_as_opened": first["groups"], "dialog_screen": dlg.screen().name(),
                               "dialog_geometry": [dlg.x(), dlg.y(), dlg.width(), dlg.height()],
                               "previous_button_visible": bool(restore and restore.isVisibleTo(dlg)),
                               "previous_tooltip": restore.toolTip() if restore else "", "previous_tags": [],
                               "previous_button_text": restore.text() if restore else "", "imported_tags": [],
                               "dialog_title": dlg.windowTitle(), "plus_dialog": True,
                               "plus_steps": [stack.widget(i).title() for i in range(stack.count())
                                              if isinstance(stack.widget(i), QGroupBox)] if stack is not None else [],
                               "info": {"author": "", "version": ""}}
            self.note(f"FOMOD Plus pages: {self.first_info['plus_steps']}")
            return  # let the first page settle one tick
        index = stack.currentIndex() if stack is not None else 0
        if index in self.done_pages:
            return
        self.done_pages.add(index)
        page = self.describe_plus_page(dlg)
        self.apply_plus_choices(stack.currentWidget() if stack is not None else None, page["title"])
        page_after = self.describe_plus_page(dlg)
        page_after["before"] = page["groups"]
        self.pages.append(page_after)
        self.shoot(dlg, f"page{index + 1}-{page['title'].replace(' ', '_')}")
        nxt = next((b for b in dlg.findChildren(QPushButton) if b.text() in ("Next", "Install")), None)
        if nxt is None or not nxt.isEnabled():
            self.warnings.append(f"Next is disabled on page {page['title']!r}; cancelling")
            dlg.reject()
            return
        self.note(f"page {page['title']!r}: pressing {nxt.text()!r}")
        nxt.click()

    def apply_plus_choices(self, page_widget, title: str) -> None:
        wanted = (self.c.get("choices") or {}).get(title)
        if wanted is None or not isinstance(page_widget, QGroupBox):
            return
        boxes = {g.title(): g for g in self._plus_groups(page_widget)}
        for group_name, spec in wanted.items():
            box = boxes.get(group_name)
            if box is None:
                self.warnings.append(f"no group {group_name!r} on page {title!r}")
                continue
            buttons = {b.text(): b for b in self._plus_buttons(box)}
            if isinstance(spec, list):
                targets = {n: (n in spec) for n in buttons}
                for n in spec:
                    if n not in buttons:
                        self.warnings.append(f"no option {n!r} in {title!r}/{group_name!r}")
            else:
                targets = {}
                for n, want in spec.items():
                    if n not in buttons:
                        self.warnings.append(f"no option {n!r} in {title!r}/{group_name!r}")
                    else:
                        targets[n] = bool(want)
            for n, want in targets.items():
                b = buttons[n]
                if b.isChecked() == want:
                    continue
                if not b.isEnabled():
                    self.warnings.append(f"option {n!r} is disabled; cannot set it to {want}")
                    continue
                if not want and b.autoExclusive():
                    continue  # a radio button can only be switched off by choosing another
                b.click()     # a real click: FOMOD Plus notes a box the user unticked as "deselected"
                self.note(f"{'ticked' if want else 'unticked'} {title} / {group_name} / {n}")

    def fomod(self, dlg) -> None:
        stack = dlg.findChild(QStackedWidget, "stepsStack")
        if not self.dialog_seen:
            self.dialog_seen = True
            self.d._place(dlg)
            self.note("FOMOD dialog opened")
            name_box = dlg.findChild(QWidget, "nameCombo")
            name_box.setEditText(str(self.c["name"]))
            previous = dlg.findChild(QPushButton, "previousBtn") or QPushButton("")   # MO2's own installer has none
            tags, imported_tags = [], []
            for i in range(stack.count()):
                page = stack.widget(i)
                for box in self._group_boxes(page):
                    for b in self._buttons(box):
                        if "(previous)" in b.text():
                            tags.append({"page": page.title(), "group": box.title(), "option": self._author_name(b), "text": b.text()})
                        if "(imported)" in b.text():
                            imported_tags.append({"page": page.title(), "group": box.title(), "option": self._author_name(b), "text": b.text()})
            self.first_info = {"first_page_as_opened": self.describe_page(dlg)["groups"],
                               "dialog_screen": dlg.screen().name(), "dialog_geometry": [dlg.x(), dlg.y(), dlg.width(), dlg.height()],
                               "previous_button_visible": previous.isVisibleTo(dlg),
                               "previous_tooltip": previous.toolTip(), "previous_tags": tags,
                               "previous_button_text": previous.text(), "imported_tags": imported_tags,
                               "dialog_title": dlg.windowTitle(), "info": {
                                   "author": dlg.findChild(QLabel, "authorLabel").text(),
                                   "version": dlg.findChild(QLabel, "versionLabel").text()}}
            self.note(f"previous button visible={previous.isVisibleTo(dlg)}, tagged={len(tags)}")
            if self.c.get("mode") in ("previous", "imported") and previous.isVisibleTo(dlg):
                previous.click()
                self.note(f"clicked {previous.text()}")
            return  # let the first page settle one tick
        index = stack.currentIndex()
        if index in self.done_pages:
            return
        self.done_pages.add(index)
        page = self.describe_page(dlg)
        self.apply_choices(dlg, stack.currentWidget(), page["title"])
        page_after = self.describe_page(dlg)
        page_after["before"] = page["groups"]
        self.pages.append(page_after)
        self.shoot(dlg, f"page{index + 1}-{page['title'].replace(' ', '_')}")
        nxt = dlg.findChild(QPushButton, "nextBtn")
        if not nxt.isEnabled():
            self.warnings.append(f"Next is disabled on page {page['title']!r}; cancelling")
            dlg.reject()
            return
        self.note(f"page {page['title']!r}: pressing {nxt.text()!r}")
        nxt.click()

    def apply_choices(self, dlg, page_widget, title: str) -> None:
        wanted = (self.c.get("choices") or {}).get(title)
        if wanted is None:
            return
        boxes = {g.title(): g for g in self._group_boxes(page_widget)}
        for group_name, spec in wanted.items():
            box = boxes.get(group_name)
            if box is None:
                self.warnings.append(f"no group {group_name!r} on page {title!r}")
                continue
            buttons = {self._author_name(b): b for b in self._buttons(box) if b.objectName() == "choice"}
            if isinstance(spec, list):
                targets = {n: (n in spec) for n in buttons}
                for n in spec:
                    if n not in buttons:
                        self.warnings.append(f"no option {n!r} in {title!r}/{group_name!r}")
            else:
                targets = {}
                for n, want in spec.items():
                    if n not in buttons:
                        self.warnings.append(f"no option {n!r} in {title!r}/{group_name!r}")
                    else:
                        targets[n] = bool(want)
            for n, want in targets.items():
                b = buttons[n]
                if b.isChecked() == want:
                    continue
                if not b.isEnabled():
                    self.warnings.append(f"option {n!r} is disabled; cannot set it to {want}")
                    continue
                if not want and b.autoExclusive():
                    continue  # a radio button can only be switched off by choosing another
                b.click()
                self.note(f"{'ticked' if want else 'unticked'} {title} / {group_name} / {n}")


def createPlugin() -> mobase.IPlugin:
    return FomodCompanionDriver()
