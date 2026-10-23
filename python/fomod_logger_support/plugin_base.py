"""The part of the MO2 plugin that both editions of Modlist Companion share.

The full edition (``fomod_logger_plugin.py``) records installs with its own native installer; the FOMOD Plus
edition (``fomod_logger_plus_plugin.py``) rebuilds them from what FOMOD Plus saved. Everything else is the same and
lives here: the plugin lifecycle, the load-order snapshot, the new-patch alerts, removal and rename tracking, MO2's
problem list and the window. An edition fills in the few things that differ (see "what an edition provides").

MO2 loads each edition's file directly; the edition adds this folder's parent to ``sys.path`` and registers the
SQLite runtime's folder before it imports this module.
"""

from __future__ import annotations

import html
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import mobase
from PyQt6.QtCore import QCoreApplication, QFileSystemWatcher, QTimer
from PyQt6.QtGui import QIcon

from . import executor as background
from .core import history, patch_alerts, plain_record
from .core.compare_choices import read_profiles
from .core.identity import stable_instance_id
from .core.missed_patches import find_missed_patches, game_plugin_names
from .core.storage import EventStore
from .history_tracker import HistoryTracker
from .scan import Scanner

# The native installer writes a staging file and then renames it, so one event
# produces several folder notifications; import once they settle.
_INGEST_DEBOUNCE_MS = 150
# Another plugin can switch plugins through IPluginList.setState, which raises
# no MO2 signal; MO2 then rewrites the profile's plugins.txt, so watch that.
_PROFILE_FILES = ("plugins.txt", "loadorder.txt", "modlist.txt")
_PROFILE_DEBOUNCE_MS = 100
_WINDOW_ERROR_SECONDS = 600
_PLUGIN_SUFFIXES = (".esp", ".esm", ".esl")
_PUBLIC_SEMVER = "1.0.0"
_MO2_VERSION = "1.0.0"
# MO2 creates a mod's folder after the installer starts; allow for clock rounding.
_CREATED_SLACK_SECONDS = 120
# A new-patch check waits for MO2 to list the new mod and its plugins; it gives up after this.
_INSTALL_CHECK_SECONDS = 300
# MO2 problem keys: 1-99 are failures (each edition says which it uses); each mod with new patches has its own,
# counting up from here.
_ALERT_KEY_BASE = 100
# After an install, wait this long for the installer's own record to arrive before deciding the mod was a plain
# install (the full edition's installer writes its event file just before MO2 announces the install, FOMOD Plus
# saves its choices a moment after).
_PLAIN_SETTLE_MS = 2_500
# A FOMOD record counts as that install's when it was completed this long before MO2 announced the install, or later.
_INSTALLER_RECORD_SECONDS = 120
# The list's change history: a drag of plugins or mods raises one MO2 callback per item, so the look at the list waits
# until they stop. (After a profile switch it waits for MO2's plugin list to refresh, however long that takes.)
_HISTORY_MOVE_MS = 400


def _seconds(timestamp: str) -> float:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def _archive_key(archive: str) -> str:
    """Archive file name for matching, ignoring its folder and case."""

    return Path(archive.replace("\\", "/")).name.casefold() if archive else ""


class CompanionPlugin(mobase.IPluginTool, mobase.IPluginDiagnose):
    """What an edition provides:

    * the class attributes ``PLUGIN_NAME`` (MO2 keys the plugin's settings and enabled state on it),
      ``DISPLAY_NAME``, ``DESCRIPTION``, ``TOOLTIP`` and ``DATA_FOLDER`` (under MO2's plugin data folder: the
      database and the window's settings live there, and no edition touches another's);
    * ``_edition_start`` (run while the plugin starts, before the load order is read), ``_ingest`` (bring new
      installs into the database; the window and installs call it), ``_data_watch_paths`` (folders whose changes
      should run ``_ingest``);
    * ``_failure_keys``, ``_short_text`` and ``_full_text``: the edition's MO2 problems;
    * ``_window_options`` (what the window shows and does differently), and ``_on_mod_installed`` when installs
      are noticed some other way than by reading the event folder;
    * ``_covered_by_installer`` (did an installer record this install? if not it is a plain install, which the
      base class records) and ``_edition_scan_jobs`` (what else "Scan existing mods..." does).
    """

    PLUGIN_NAME = ""
    DISPLAY_NAME = ""
    DESCRIPTION = ""
    TOOLTIP = "Open Modlist Companion: your FOMOD choices and your list's history"
    DATA_FOLDER = ""

    def __init__(self) -> None:
        mobase.IPluginTool.__init__(self)
        mobase.IPluginDiagnose.__init__(self)
        # Set by init() before anything uses them (MO2 calls init() first), hence the typed None.
        self._organizer: mobase.IOrganizer = None  # pyright: ignore[reportAssignmentType]
        self._window = None
        self._timer = None
        self._watcher = None
        self._profile_pending = False
        self._data_root: Path = None  # pyright: ignore[reportAttributeAccessIssue]
        self._active = False
        self._lifecycle_generation = 0
        self._registered_callbacks: set[str] = set()
        self._runtime_error = ""
        self._state_error = ""
        self._window_error = ""
        self._window_error_at = 0.0
        # Load order snapshot for missed-patch checks. Captured on MO2's main
        # thread only; the HTTP thread reads this reference and never calls MO2.
        self._load_order: dict | None = None
        self._mod_plugin_cache: dict[str, tuple[int, list[str]]] = {}
        # New-patch alerts. MO2 2.5.2 starts plugins before it reads its mod list, so nothing is judged until
        # the first refresh has run (``_ready``). The mods and plugins seen so far (only ever growing, so a
        # reinstall that deletes and recreates a folder isn't new); mods to look for patches for (folded name ->
        # (name, only these plugins or None, queued at)); new mods that were off when looked at; whether any
        # alert might be pending and whether something changed since they were last judged; the problem keys
        # handed out; what MO2's problem list shows right now (one group per mod just installed); and the
        # setting, read on the main thread because MO2 asks for problems from another one.
        self._ready = False
        self._known_mods: dict[str, str] | None = None
        self._known_plugins: dict[str, set[str]] = {}
        self._install_checks: dict[str, tuple[str, set[str] | None, float]] = {}
        self._enable_watch: dict[str, str] = {}
        self._has_alerts = True
        self._alerts_dirty = True
        self._alert_keys: dict[tuple, int] = {}
        self._next_alert_key = _ALERT_KEY_BASE
        self._alert_groups: list[dict] = []
        self._alert_signature: tuple = ()
        self._alerts_setting = True
        # What the edition last found out about other plugins (see ``_refresh_cached_state``).
        self._cached_state: tuple = ()
        # Work that reads files (the folders and plugin headers of mods, FOMOD Plus's archives) runs on a worker
        # thread. Executors that were closed while their work was still running: a worker thread still holds the
        # object it posts to, so it must outlive the work (freeing it on the worker thread would be a crash).
        self._executor: Any = None  # a QtExecutor while active; the code checks before use
        self._retired: list = []
        # The walks of mod folders that are running for installs (so that switching the plugin off can stop them), and
        # the mods one failed for.
        self._plain_handles: list = []
        self._plain_errors: dict[str, str] = {}
        self._plain_settle_ms = _PLAIN_SETTLE_MS
        # "Scan existing mods...": records every mod that has no record yet, in the background.
        self._scanner = Scanner(self._scan_jobs, self._store_scan_result, lambda: self._executor,
                                count=self._unrecorded_count)
        # The list's change history (see ``history_tracker``): the last look at the list, the programs MO2 started that
        # have not ended, each mod's version and archive as last read (the history says when one changes), and the two
        # timers (made when first needed): one waits for a drag of mods or plugins to stop, one for a profile switch to
        # finish loading.
        self._history = HistoryTracker(self._history_database, self._history_instance, self._profile_name)
        self._history_versions: dict[str, tuple[str, str] | None] = {}
        self._history_move_timer = None
        self._history_started_before = False

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        try:
            self._data_root = Path(organizer.pluginDataPath()) / self.DATA_FOLDER
            self._data_root.mkdir(parents=True, exist_ok=True)
            self._register_callbacks()
            self._start_runtime(initializing=True)
            return True
        except Exception as exc:
            self._runtime_error = str(exc)
            self._shutdown()
            print(f"[Modlist Companion] initialization failed: {exc}", file=sys.stderr)
            # MO2 discards a plugin whose init() returns False, and with it this
            # diagnostic. Stay loaded but inactive so MO2 shows the problem.
            return True

    def _register_callbacks(self) -> None:
        mod_list = self._organizer.modList()
        self._register_callback(
            "mod-installed",
            lambda: mod_list.onModInstalled(self._on_mod_installed),
            require_true=True,
        )
        self._register_callback(
            "mod-removed",
            lambda: mod_list.onModRemoved(self._on_mod_removed),
            require_true=True,
        )
        # Missed-patch checks are optional: without these callbacks Logger still
        # records and serves choices; the snapshot then updates on each refresh.
        try:
            self._register_callback(
                "mod-state-changed",
                lambda: mod_list.onModStateChanged(
                    lambda _states: self._capture_load_order()
                ),
            )
        except Exception as exc:
            print(f"[Modlist Companion] mod state tracking unavailable: {exc}", file=sys.stderr)
        try:
            plugin_list = self._organizer.pluginList()
            self._register_callback(
                "plugins-refreshed",
                lambda: plugin_list.onRefreshed(self._on_plugins_refreshed),
            )
            self._register_callback(
                "plugin-state-changed",
                lambda: plugin_list.onPluginStateChanged(
                    lambda _states: self._capture_load_order()
                ),
            )
        except Exception as exc:
            print(f"[Modlist Companion] load order tracking unavailable: {exc}", file=sys.stderr)
        try:
            self._register_callback(
                "profile-changed",
                lambda: self._organizer.onProfileChanged(self._on_profile_changed),
            )
        except Exception as exc:
            print(f"[Modlist Companion] profile tracking unavailable: {exc}", file=sys.stderr)
        # The change history: moves in the left pane and the load order (one callback per item moved), and the programs
        # MO2 starts. None of these is needed for anything else, so each may be missing.
        for name, method in (("mod-moved", "onModMoved"), ("plugin-moved", "onPluginMoved")):
            try:
                owner = mod_list if name == "mod-moved" else self._organizer.pluginList()
                self._register_callback(name, lambda owner=owner, method=method: getattr(owner, method)(self._on_moved))
            except Exception as exc:
                print(f"[Modlist Companion] {name} tracking unavailable: {exc}", file=sys.stderr)
        for name, method, callback in (("about-to-run", "onAboutToRun", self._on_about_to_run),
                                       ("finished-run", "onFinishedRun", self._on_finished_run)):
            try:
                self._register_callback(name, lambda method=method, callback=callback: getattr(self._organizer, method)(callback))
            except Exception as exc:
                print(f"[Modlist Companion] program tracking unavailable ({method}): {exc}", file=sys.stderr)

        # MO2 2.5.2's Python binding exposes the named plugin lifecycle
        # overloads as returning None even though the C++ API returns bool.
        # Binding failures therefore surface as exceptions, not a status value.
        self._register_callback(
            "plugin-enabled",
            lambda: self._organizer.onPluginEnabled(
                self.name(), self._on_plugin_enabled
            ),
        )
        self._register_callback(
            "plugin-disabled",
            lambda: self._organizer.onPluginDisabled(self.name(), self._shutdown),
        )

        try:
            self._register_callback(
                "setting-changed",
                lambda: self._organizer.onPluginSettingChanged(self._on_setting_changed),
            )
        except Exception as exc:
            print(f"[Modlist Companion] setting tracking unavailable: {exc}", file=sys.stderr)

        # Whether other plugins are switched on (FOMOD Plus, the other edition) is read here, on MO2's thread, and kept:
        # MO2 asks for problems from another thread, which must not call MO2.
        for name, method in (("any-plugin-enabled", "onPluginEnabled"), ("any-plugin-disabled", "onPluginDisabled")):
            try:
                self._register_callback(
                    name, lambda method=method: getattr(self._organizer, method)(self._on_any_plugin_switched))
            except Exception as exc:
                print(f"[Modlist Companion] plugin switch tracking unavailable: {exc}", file=sys.stderr)

        application = QCoreApplication.instance()
        if application is not None:
            self._register_callback(
                "about-to-quit",
                lambda: application.aboutToQuit.connect(self._shutdown),
            )

    def _register_callback(
        self,
        name: str,
        register: Callable[[], object],
        *,
        require_true: bool = False,
    ) -> None:
        if name in self._registered_callbacks:
            return
        result = register()
        if require_true and result is not True:
            raise RuntimeError(f"MO2 refused the {name} callback registration")
        self._registered_callbacks.add(name)

    def _start_runtime(self, *, initializing: bool = False) -> None:
        if self._active:
            return
        if self._data_root is None:
            raise RuntimeError("Modlist Companion data root is unavailable")

        self._lifecycle_generation += 1
        generation = self._lifecycle_generation
        self._active = True
        self._ready, self._known_mods, self._known_plugins = False, None, {}
        self._history.reset(again=self._history_started_before)
        self._history_started_before = True
        self._history_versions = {}
        self._install_checks, self._enable_watch = {}, {}
        self._has_alerts = self._alerts_dirty = True
        try:
            self._alerts_setting = self._read_alert_setting()
            self._edition_start()
            self._capture_load_order()
            # Import new event files as soon as they appear instead of polling.
            self._timer = QTimer()
            self._timer.setSingleShot(True)
            self._timer.setInterval(_INGEST_DEBOUNCE_MS)
            self._timer.timeout.connect(self._ingest)
            self._watcher = QFileSystemWatcher()
            for folder in self._data_watch_paths():
                folder.mkdir(exist_ok=True)
                self._watcher.addPath(str(folder))
            self._watcher.directoryChanged.connect(self._folder_changed)
            self._watcher.fileChanged.connect(self._profile_files_changed)
            self._watch_profile(capture=False)  # the startup snapshot is above
            refresh_ran = False

            def refreshed() -> None:
                nonlocal refresh_ran
                refresh_ran = True
                self._on_next_refresh(generation)

            # MO2 2.5.2 returns connection.connected(), which is False when
            # the callback runs immediately. During init the proxy can also
            # suppress that immediate callback because the plugin is not yet
            # active. Queue initial reconciliation until the first refresh;
            # on re-enable, accept a proven immediate callback as success.
            registered = self._organizer.onNextRefresh(refreshed, not initializing)
            if registered is not True and not refresh_ran:
                raise RuntimeError("MO2 refused the next-refresh callback registration")
            self._runtime_error = ""
            if self._organizer.pluginSetting(self.name(), "open_window_on_start"):
                QTimer.singleShot(0, lambda: self._open_if_active(generation))
        except Exception:
            self._shutdown()
            raise

    def _on_any_plugin_switched(self, _plugin=None) -> None:
        self._look_at_other_plugins()

    def _look_at_other_plugins(self) -> None:
        """Read what the edition keeps about other plugins (on MO2's thread) and tell MO2 if it changed."""

        if not self._active or self._organizer is None:
            return
        try:
            state = self._refresh_cached_state()
        except Exception as exc:
            print(f"[Modlist Companion] unable to read the plugin states: {exc}", file=sys.stderr)
            return
        if state != self._cached_state:
            self._cached_state = state
            self._invalidate_problems()

    def _on_plugin_enabled(self) -> None:
        try:
            self._start_runtime()
        except Exception as exc:
            self._runtime_error = f"Unable to restart Modlist Companion: {exc}"
            print(f"[Modlist Companion] {self._runtime_error}", file=sys.stderr)

    def _on_next_refresh(self, generation: int) -> None:
        if not self._active or generation != self._lifecycle_generation:
            return
        self._reconcile_current_mods()
        # MO2 has its mod list now, so the load order can be judged (and, until now, only start-up's empty one
        # existed: it must never settle an alert).
        self._ready = True
        # MO2 runs next-refresh callbacks before it refreshes the plugin list,
        # and at startup the plugin-list callbacks are connected only after
        # that refresh. Read the list once the refresh has finished.
        QTimer.singleShot(0, self._capture_load_order)

    def _capture_load_order(self) -> None:
        """Snapshot active plugins and enabled mods for missed-patch checks."""

        if not self._active or self._organizer is None:
            return
        self._look_at_other_plugins()
        mod_plugins: dict[str, set[str]] = {}
        try:
            plugins = self._organizer.pluginList()
            names = [str(name) for name in plugins.pluginNames()]
            active = [n for n in names if plugins.state(n) == mobase.PluginState.ACTIVE]
            origins = {n: str(plugins.origin(n)) for n in names}
            # Base game, DLC and Creation Club plugins: every patch mentions
            # them, so they are never evidence.
            base = game_plugin_names(names, origins, self._game_plugin_lists())
            mod_list = self._organizer.modList()
            mods, disabled_mods = [], []
            for name in mod_list.allMods():
                (mods if mod_list.state(name) & mobase.ModState.ACTIVE else disabled_mods).append(str(name))
            disabled_plugins = self._plugins_of(mod_list, disabled_mods)
            # Which mod each plugin comes from: a plugin's origin is its mod when it has one.
            mod_keys = {name.casefold() for name in [*mods, *disabled_mods]}
            base_names = set(base)
            for n in names:
                if n not in base_names and origins[n].casefold() in mod_keys:
                    mod_plugins.setdefault(origins[n].casefold(), set()).add(n.casefold())
            for plugin, mod in disabled_plugins.items():
                mod_plugins.setdefault(mod.casefold(), set()).add(plugin.casefold())
            # Never before MO2 has read its list: while plugins start, MO2 has no profile yet, and asking it for the mods in
            # priority order then ends MO2 (an access violation inside MO2, which no try can catch).
            history_state = None
            if self._ready:
                try:
                    history_state = self._history_snapshot(mod_list, plugins, names, active, mods)
                except Exception as exc:        # the history is a bonus: it must never cost the missed-patch checks
                    print(f"[Modlist Companion] unable to read the list for the history: {exc}", file=sys.stderr)
            snapshot = {
                "instance_id": stable_instance_id(Path(self._organizer.basePath())),
                "active_plugins": active,
                "base_plugins": base,
                "mods": mods,
                # Installed but switched off: unchecked in the plugin list, or
                # inside a disabled mod (MO2 doesn't list those plugins at all).
                "unchecked_plugins": [n for n in names if n not in active],
                # Masters, to spot a plugin that can't load: one isn't installed.
                "plugin_masters": {n: [str(m) for m in plugins.masters(n)] for n in names},
                "disabled_mod_plugins": disabled_plugins,
                "disabled_mods": disabled_mods,
            }
        except Exception as exc:
            snapshot = history_state = None
            print(f"[Modlist Companion] unable to read the load order: {exc}", file=sys.stderr)
        previous = self._load_order
        if snapshot != self._load_order:
            # Replace the whole dict so the HTTP thread never sees a partial update.
            self._load_order = snapshot
            self._notify_viewer()
        self._alert_work(previous, snapshot, mod_plugins)
        self._history_look(history_state)

    # ---------- the list's change history ----------
    #
    # Each look at the list also builds a snapshot for the history (the mods and plugins in their order, and which are
    # on), kept apart from ``_load_order`` so that moving a mod doesn't make the missed-patch checks and the window read
    # everything again. ``HistoryTracker`` compares it with the last one. It is cheap: a few calls into MO2 per plugin
    # beyond what the look already does, one for the mods' order, and a list comparison (the mods' versions are read
    # once and kept). Nothing here runs before MO2 has read its mod list (``_ready``).

    def _history_database(self) -> Path | None:
        return self._data_root / "fomod_logger.sqlite3" if self._data_root is not None else None

    def _history_instance(self) -> str:
        try:
            return stable_instance_id(Path(self._organizer.basePath()))
        except Exception:
            return ""

    def _profile_name(self) -> str:
        """The profile in use, by name (the name of its folder)."""

        folder = self._profile_folder()
        return folder.name if folder is not None else ""

    def _history_snapshot(self, mod_list, plugins, names: list[str], active: list[str], enabled: list[str]) -> dict:
        everything = [str(name) for name in mod_list.allMods()]
        try:
            ordered = [str(name) for name in mod_list.allModsByProfilePriority()]
        except Exception:
            ordered = []        # without the order, moves just aren't noticed
        seen = {name.casefold() for name in ordered}
        ordered += sorted((name for name in everything if name.casefold() not in seen), key=str.casefold)
        try:
            priority = {name: int(plugins.priority(name)) for name in names}
            plugin_order = sorted(names, key=lambda name: (priority[name], name.casefold()))
        except Exception:
            plugin_order = list(names)
        return history.make_snapshot(ordered, enabled, plugin_order, active, self._mod_versions(mod_list, ordered))

    def _mod_versions(self, mod_list, mods: list[str]) -> dict[str, tuple[str, str]]:
        """Each real mod's version and archive file name, read once (a reinstall refreshes its own, see
        ``_on_mod_installed``)."""

        cache = self._history_versions
        present = set(mods)
        for gone in [name for name in cache if name not in present]:
            del cache[gone]
        for name in mods:
            if name in cache:
                continue
            mod = None if name.casefold().endswith("_separator") else mod_list.getMod(name)
            cache[name] = self._version_facts(mod) if mod is not None and self._is_real_mod(mod) else None
        return {name: value for name, value in cache.items() if value is not None}

    @staticmethod
    def _version_facts(mod) -> tuple[str, str]:
        try:
            version = str(mod.version().canonicalString())
        except Exception:
            version = ""
        try:
            archive = Path(str(mod.installationFile() or "").replace("\\", "/")).name
        except Exception:
            archive = ""
        return version, archive

    def _history_look(self, state: dict | None) -> None:
        """Compare this look with the last (never lets a failure reach the caller)."""

        if state is None or not self._ready or not self._active:
            return
        try:
            if self._history.look(state):
                self._notify_viewer()
        except Exception as exc:
            print(f"[Modlist Companion] unable to keep the change history: {exc}", file=sys.stderr)

    def _on_plugins_refreshed(self) -> None:
        """MO2 refreshed its plugin list; after a profile switch that is when the new profile's lists are there. Only
        this ends a pending switch, never a timer: on a big list the refresh can take longer than any wait, and a look
        before it would compare the new profile's snapshot with the old profile's plugin states."""

        self._history.settle_switch()
        self._capture_load_order()

    def _on_profile_changed(self, old, new) -> None:
        if self._active:
            try:
                if self._history.profile_switched(self._label_of(old), self._label_of(new)):
                    self._notify_viewer()
            except Exception as exc:
                print(f"[Modlist Companion] unable to note the profile switch: {exc}", file=sys.stderr)
        self._watch_profile(capture=True)

    @staticmethod
    def _label_of(profile) -> str:
        try:
            return str(profile.name())
        except Exception:
            return str(profile or "")

    def _on_moved(self, *_args) -> None:
        """A mod or plugin was moved. MO2 raises this once per item, so the look waits until they stop."""

        if self._active:
            self._lazy_timer("_history_move_timer", _HISTORY_MOVE_MS, self._capture_load_order).start()

    def _lazy_timer(self, attribute: str, interval: int, callback: Callable[[], None]):
        timer = getattr(self, attribute)
        if timer is None:
            timer = QTimer()
            timer.setSingleShot(True)
            timer.setInterval(interval)
            timer.timeout.connect(callback)
            setattr(self, attribute, timer)
        return timer

    def _on_about_to_run(self, *args) -> bool:
        """MO2 is about to start a program (what MO2 passes differs by version: the program's path comes first).
        Only the program is kept, never its arguments. Always lets it run."""

        try:
            if self._active and args:
                if self._history.run_started(str(args[0])) is not None:
                    self._notify_viewer()
        except Exception as exc:
            print(f"[Modlist Companion] unable to note the program start: {exc}", file=sys.stderr)
        return True

    def _on_finished_run(self, *args) -> None:
        try:
            if self._active and len(args) >= 2:
                if self._history.run_finished(str(args[0]), args[1]):
                    self._notify_viewer()
        except Exception as exc:
            print(f"[Modlist Companion] unable to note the program end: {exc}", file=sys.stderr)

    def _plugins_of(self, mod_list, mod_names: list[str]) -> dict[str, str]:
        """Plugin file -> mod name for the plugins at the root of these mods.

        Folder listings are cached by modification time, so a plugin toggle
        doesn't re-read every disabled mod.
        """

        found: dict[str, str] = {}
        for name in mod_names:
            try:
                folder = Path(str(mod_list.getMod(name).absolutePath()))
                stamp = folder.stat().st_mtime_ns
                cached = self._mod_plugin_cache.get(str(folder))
                if cached is None or cached[0] != stamp:
                    plugins = [entry.name for entry in os.scandir(folder)
                               if entry.is_file() and entry.name.casefold().endswith(_PLUGIN_SUFFIXES)]
                    cached = (stamp, plugins)
                    self._mod_plugin_cache[str(folder)] = cached
            except Exception:
                continue  # a mod MO2 can't resolve just isn't counted
            for plugin in cached[1]:
                found.setdefault(plugin, name)
        return found

    # ---------- new-patch alerts ----------

    def _read_alert_setting(self) -> bool:
        """The setting, read on MO2's main thread. MO2 asks for problems from another thread, which only ever sees
        the copy in ``_alerts_setting``."""

        try:
            return bool(self._organizer.pluginSetting(self.name(), "alert_new_patches"))
        except Exception:
            return True  # an unreadable setting keeps the default

    def _on_setting_changed(self, plugin_name, key, _old, new) -> None:
        if str(plugin_name) == self.name() and str(key) == "alert_new_patches":
            self._alerts_setting = bool(new)
            self._invalidate_problems()

    def _alert_work(self, previous: dict | None, snapshot: dict | None,
                    mod_plugins: dict[str, set[str]] | None = None) -> None:
        """After each look at the load order: find the mods installed (or updated with new plugins) since the last one,
        look for patches made for them, and settle the alerts that have been dealt with. Waits until MO2 has read its
        mod list (the load order taken during start-up is empty). Never lets a failure reach the caller."""

        if snapshot is None or not self._ready:
            return
        try:
            self._alerts_setting = self._read_alert_setting()
            self._track_mods(snapshot, mod_plugins or {})
            added = self._check_installs(snapshot) if self._install_checks else False
            # A full pass over the pending alerts only when something could have changed them.
            if self._has_alerts and (added or self._alerts_dirty or snapshot != previous):
                self._refresh_alerts(snapshot)
        except Exception as exc:
            print(f"[Modlist Companion] new-patch check failed: {exc}", file=sys.stderr)

    def _queue_check(self, name: str, only: set[str] | None) -> None:
        """Look for patches made for this mod once MO2 lists it: for all of its plugins (``only`` None), or just
        the ones it gained."""

        key = name.casefold()
        earlier = self._install_checks.get(key)
        if earlier is not None:
            only = None if earlier[1] is None or only is None else earlier[1] | only
        self._install_checks[key] = (name, only, time.monotonic())

    def _check_held(self, key: str) -> bool:
        """Whether the look for this mod's patches must wait (an edition that is still working out how the mod was
        installed says so)."""

        return False

    def _track_mods(self, snapshot: dict, mod_plugins: dict[str, set[str]]) -> None:
        """Notice mods that are new since the last look, and mods that gained plugins (an update).

        What was seen before only ever grows: a replace-reinstall deletes the mod's folder and makes it again, so a
        mod, or a plugin, that vanishes for a moment must not look new when it comes back. A mod is forgotten only
        when it is removed. The first look after start-up is the baseline: nothing in it is new."""

        current = {name.casefold(): name for name in [*snapshot.get("mods", []), *snapshot.get("disabled_mods", [])]}
        if not current:
            return  # MO2 hasn't read its mod list: this says nothing
        if self._known_mods is None:
            self._known_mods = dict(current)
            self._known_plugins = {key: set(plugins) for key, plugins in mod_plugins.items()}
            return
        for key, name in current.items():
            now = mod_plugins.get(key, set())
            if key not in self._known_mods:
                self._known_mods[key] = name
                self._queue_check(name, None)
            else:
                gained = now - self._known_plugins.get(key, set())
                if gained:
                    self._queue_check(name, gained)
            if now:
                self._known_plugins.setdefault(key, set()).update(now)
        # A mod that was switched off when it was checked (a new mod is) can only be matched by name once it is on.
        enabled = {name.casefold() for name in snapshot.get("mods", [])}
        for key in [key for key in self._enable_watch if key in enabled]:
            self._queue_check(self._enable_watch.pop(key), None)

    def _check_installs(self, snapshot: dict) -> bool:
        """Look for skipped patch options made for each queued mod, once MO2 lists the mod (and, if it is switched on,
        its plugins). Only entries made for that mod are looked at. Returns whether an alert was added."""

        now = time.monotonic()
        mod_list = self._organizer.modList()
        known = {name.casefold(): name for name in [*snapshot.get("mods", []), *snapshot.get("disabled_mods", [])]}
        enabled = {name.casefold() for name in snapshot.get("mods", [])}
        listed = {name.casefold() for name in [*snapshot.get("active_plugins", []), *snapshot.get("unchecked_plugins", [])]}
        waiting: dict[str, tuple[str, set[str] | None, float]] = {}
        due: list[tuple[str, str, list[str], bool, bool]] = []
        for key, (name, only, queued) in self._install_checks.items():
            if self._check_held(key):
                waiting[key] = (name, only, now)    # held up for as long as it takes, however long that is
                continue
            if now - queued > _INSTALL_CHECK_SECONDS:
                continue
            spelled = known.get(key)
            if spelled is None:
                waiting[key] = (name, only, queued)
                continue
            plugins = list(self._plugins_of(mod_list, [spelled])) if only is None else sorted(only)
            if key in enabled and any(plugin.casefold() not in listed for plugin in plugins):
                waiting[key] = (name, only, queued)  # switched on, but MO2 hasn't listed its plugins yet
                continue
            due.append((key, spelled, plugins, only is None, key in enabled))
        added = False
        if due and self._alerts_setting and snapshot.get("instance_id"):
            instance_id = snapshot["instance_id"]
            watch: dict[str, str] = {}
            with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
                events = store.latest("", include_hidden=False)
                reviews = store.reviews()
                for key, name, plugins, whole, is_enabled in due:
                    # An update only counts the plugins it added, and a patch found by the mod's name was
                    # announced when the mod first came.
                    found = patch_alerts.new_patches(
                        events, snapshot, mod_name=name if whole else "", mod_plugins=plugins, reviews=reviews)
                    if whole and not is_enabled:
                        watch[key] = name
                    if not found:
                        continue
                    installed = store.newest_for_mod(instance_id, name)
                    if store.add_patch_alerts(instance_id, name, installed["event_id"] if installed else "",
                                              [patch_alerts.item_key(item) for item in found]):
                        added = True
            self._enable_watch.update(watch)
        # Only now: a failure above leaves the queue as it was, to be tried again at the next look.
        self._install_checks = waiting
        if added:
            self._has_alerts = True
        return added

    def _refresh_alerts(self, snapshot: dict | None = None) -> None:
        """Settle the alerts that have been dealt with and tell MO2 and the window when what is pending has changed.

        An alert is cleared for good only for a definite reason (see ``live_alerts``). One that merely isn't a missed
        patch in this snapshot is hidden and stays pending: a load order read before MO2 has its lists, or while the
        mod is unlisted, says nothing about whether the patch was dealt with."""

        snapshot = snapshot if snapshot is not None else self._load_order
        if snapshot is None or not snapshot.get("instance_id"):
            return
        live: list[dict] = []
        remaining = 0
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            rows = store.patch_alerts(snapshot["instance_id"])
            remaining = len(rows)
            if rows:
                reviews = store.reviews()
                events, gone = [], set()
                # Only the installers the pending alerts name are looked at, not the whole history.
                for event_id in sorted({row["event_id"] for row in rows}):
                    event = store.event(event_id)
                    newest = store.newest_for_mod(event["instance"]["id"], event["mod"]["name"]) if event else None
                    if event is None or event["removed_at"] or (newest is not None and newest["event_id"] != event_id):
                        gone.add(event_id)    # removed, or replaced by a later install of the installer
                    elif not event["hidden"]:
                        events.append(event)
                missed = find_missed_patches(events, snapshot, reviews)["missed"]
                installed = [*snapshot.get("active_plugins", []), *snapshot.get("unchecked_plugins", []),
                             *(snapshot.get("disabled_mod_plugins") or {})]
                live, settled = patch_alerts.live_alerts(rows, missed, reviews, gone=gone, events=events,
                                                         installed=installed)
                if settled:
                    store.clear_patch_alerts(settled)
                    remaining -= len(settled)
        self._has_alerts = remaining > 0
        self._alerts_dirty = False
        groups = patch_alerts.group_by_mod(live)
        for group in groups:
            # A problem keeps its key while MO2 shows it, whatever else comes and goes.
            identity = (group["instance_id"], tuple(group["mod_keys"]))
            if identity not in self._alert_keys:
                self._alert_keys[identity] = self._next_alert_key
                self._next_alert_key += 1
            group["key"] = self._alert_keys[identity]
        signature = tuple(
            (group["key"], tuple((row["event_id"], row["option_id"], row["item"]["state"]) for row in group["rows"]))
            for group in groups)
        if signature != self._alert_signature:
            self._alert_groups, self._alert_signature = groups, signature
            self._invalidate_problems()
            self._notify_viewer()

    def _invalidate_problems(self) -> None:
        """Ask MO2 to read the problem list again (the notification icon)."""

        try:
            self._invalidate()
        except Exception:
            pass  # MO2 hasn't connected its callback yet; it reads the problems when it first checks

    def _alert_group(self, key: int) -> dict | None:
        # Called from MO2's problem checks, which run off the main thread: nothing here may call MO2.
        if self._active and self._alerts_setting:
            for group in self._alert_groups:
                if group["key"] == key:
                    return group
        return None

    def _alerts_changed(self) -> None:
        """The window changed a reminder or dismissed a card: an alert may have been dealt with."""

        if not self._active:
            return
        try:
            self._alerts_setting = self._read_alert_setting()
            self._refresh_alerts()
        except Exception as exc:
            print(f"[Modlist Companion] new-patch check failed: {exc}", file=sys.stderr)

    @staticmethod
    def _close_problems_dialog() -> None:
        """"Fix" is pressed in MO2's problem list, a modal dialog that would keep the window behind it: close that
        dialog (only that one) first."""

        try:
            from PyQt6.QtWidgets import QApplication

            modal = QApplication.activeModalWidget()
            if modal is not None and modal.metaObject().className() == "ProblemsDialog":  # pyright: ignore[reportOptionalMemberAccess]
                modal.reject()  # pyright: ignore[reportAttributeAccessIssue]  # a QDialog; the stub only knows QWidget
        except Exception:
            pass  # without it the window still opens; it may sit behind the dialog

    def _notify_viewer(self) -> None:
        """Tell open viewer pages that something they show has changed."""

        if self._window is not None:
            self._window.schedule_refresh()

    def _profile_folder(self) -> Path | None:
        try:
            return Path(str(self._organizer.profilePath()))
        except Exception:
            return None

    def _watch_profile(self, capture: bool) -> None:
        """Watch the current profile's list files, and its folder, since MO2
        replaces those files rather than writing them in place."""

        watcher, folder = self._watcher, self._profile_folder()
        if watcher is None or folder is None:
            return
        data_folders = [str(path) for path in self._data_watch_paths()]
        wanted = [str(folder)] + [str(folder / name) for name in _PROFILE_FILES if (folder / name).is_file()]
        watched = watcher.files() + watcher.directories()
        stale = [path for path in watched if path not in data_folders and path not in wanted]
        if stale:
            watcher.removePaths(stale)
        missing = [path for path in wanted if path not in watched]
        if missing:
            watcher.addPaths(missing)
        if capture:
            self._profile_files_changed()

    def _folder_changed(self, path: str) -> None:
        if Path(path) in self._data_watch_paths():
            self._schedule_ingest(path)
        else:
            self._profile_files_changed(path)

    def _profile_files_changed(self, _path: str = "") -> None:
        if not self._active or self._profile_pending:
            return
        # A replaced file drops out of the watch list; watch the new one.
        self._watch_profile(capture=False)
        self._profile_pending = True
        QTimer.singleShot(_PROFILE_DEBOUNCE_MS, self._profile_settled)

    def _profile_settled(self) -> None:
        self._profile_pending = False
        self._capture_load_order()

    def _schedule_ingest(self, _path: str = "") -> None:
        if self._active and self._timer is not None:
            self._timer.start()

    def _game_plugin_lists(self) -> list[list[str]]:
        """The managed game's primary, DLC and Creation Club plugin names."""

        game = self._organizer.managedGame()
        lists = []
        for method in ("primaryPlugins", "DLCPlugins", "CCPlugins"):
            try:
                lists.append([str(name) for name in getattr(game, method)()])
            except Exception:
                continue  # not every game plugin implements every list
        return lists

    def _on_window_error(self, message: str) -> None:
        """Keep a window failure for MO2 diagnostics for a while."""

        if not self._active:
            return
        self._window_error = str(message)
        self._window_error_at = time.monotonic()

    def _on_mod_installed(self, mod: mobase.IModInterface) -> None:
        if not self._active:
            return
        self._note_mod_versions(mod)
        self._watch_install(mod)
        QTimer.singleShot(0, self._after_install)

    def _note_mod_versions(self, mod) -> None:
        """A mod was installed (or reinstalled): its version and archive are read again for the history, so that an update
        made while Companion runs is told by the install and not found at the next start. Both editions call this."""

        try:
            self._history_versions[str(mod.name())] = self._version_facts(mod)
        except Exception:
            pass

    def _after_install(self) -> None:
        """Import the install's event, then take the load order again. Which mods and plugins are new is worked out
        from the load order itself (see ``_track_mods``): MO2 refreshes the plugin list before it announces an
        install, so the announcement can't tell a new mod from a reinstalled one."""

        self._ingest()
        self._capture_load_order()

    # ---------- mods that were not installed through an installer ----------
    #
    # Every mod gets a record. One installed through a logged FOMOD installer has the installer's (the full edition's
    # native installer writes it, the FOMOD Plus edition rebuilds it); any other install gets a "plain" record
    # (core/plain_record): version, archive, files and plugins. A plain install is told by what is missing: a moment
    # after MO2 announces an install, if the edition finds no installer record of it, the mod folder is read on a
    # worker thread and recorded. "Scan existing mods..." does the same for the mods that were there before.

    @staticmethod
    def _is_real_mod(mod) -> bool:
        """Whether a mod has a folder of its own to record (not a separator, the overwrite folder, a backup or one of
        the game's own DLC entries, which MO2 lists but keeps nowhere)."""

        for method in ("isSeparator", "isOverwrite", "isForeign", "isBackup"):
            try:
                if getattr(mod, method)():
                    return False
            except Exception:
                continue          # an MO2 without that question: it is a mod
        return True

    def _instance(self) -> tuple[str, str]:
        base = Path(self._organizer.basePath())
        return stable_instance_id(base), base.name

    def _archive_path(self, recorded: str) -> str:
        """The download archive of a mod, from the path MO2 recorded (absolute, or a file name in the downloads
        folder). An archive that has moved to the downloads folder is found there by name."""

        if not recorded:
            return ""
        path = Path(recorded)
        try:
            downloads = Path(str(self._organizer.downloadsPath()))
        except Exception:
            return str(path)
        if path.is_absolute() and path.is_file():
            return str(path)
        by_name = downloads / path.name
        return str(by_name if by_name.is_file() or not path.is_absolute() else path)

    def _mod_facts(self, mod, *, resolve_archive: bool = True) -> dict:
        """What a record needs to know about a mod, read now, on MO2's thread. ``resolve_archive`` False keeps the
        archive as MO2 recorded it (no look at the disk): a plain record needs only its file name."""

        name = str(mod.name())
        try:
            version = str(mod.version().canonicalString())
        except Exception:
            version = ""
        try:
            nexus_id = max(0, int(mod.nexusId()))
        except Exception:
            nexus_id = 0
        try:
            recorded = str(mod.installationFile() or "")
            archive = self._archive_path(recorded) if resolve_archive else recorded
        except Exception:
            archive = ""
        try:
            folder = str(mod.absolutePath())
        except Exception:
            folder = ""
        return {"name": name, "version": version, "nexus_id": nexus_id, "archive": archive, "folder": folder,
                "at": plain_record.utc_text()}

    def _plain_job(self, facts: dict, kind: str, game: str = "") -> plain_record.PlainJob:
        instance_id, label = self._instance()
        archive = Path(facts["archive"].replace("\\", "/")).name if facts["archive"] else ""
        return plain_record.PlainJob(
            instance_id=instance_id, instance_label=label, mod_name=facts["name"], mod_version=facts["version"],
            nexus_id=facts["nexus_id"], game=game or self._game_info().get("nexus_domain", ""), archive=archive,
            folder=facts["folder"], when=facts["at"] if kind == "install" else "", kind=kind)

    def _watch_install(self, mod) -> None:
        """MO2 has just announced an install: after a moment, record it as a plain install unless an installer did."""

        try:
            if not self._is_real_mod(mod):
                return
            facts = self._mod_facts(mod, resolve_archive=False)
        except Exception as exc:
            print(f"[Modlist Companion] unable to read the installed mod: {exc}", file=sys.stderr)
            return
        generation = self._lifecycle_generation
        QTimer.singleShot(self._plain_settle_ms, lambda: self._settle_install(facts, generation))

    def _settle_install(self, facts: dict, generation: int) -> None:
        if not self._active or generation != self._lifecycle_generation:
            return
        try:
            self._ingest()                      # whatever the installer wrote is in the database now
            if not self._covered_by_installer(facts):
                self._record_plain(facts)
        except Exception as exc:
            self._plain_errors[facts["name"].casefold()] = f"{facts['name']}: {exc}"
            print(f"[Modlist Companion] unable to record the install of {facts['name']}: {exc}", file=sys.stderr)
            self._invalidate_problems()

    def _covered_by_installer(self, facts: dict) -> bool:
        """Whether an installer has recorded (or is recording) this install, so that no plain record is wanted. The
        edition knows how its installer reports."""

        return False

    def _installer_recorded(self, facts: dict) -> bool:
        """Whether the database holds an installer's record of this install: the same mod from the same archive,
        completed shortly before MO2 announced the install or after."""

        # An installer's record counts for this install when it is from the same archive file and was completed no more
        # than ``_INSTALLER_RECORD_SECONDS`` before MO2 announced it (or later). The trade-off: the same archive
        # installed plain again within two minutes of an installer install of it (say Manual, after a FOMOD) is taken
        # for that install and gets no plain record of its own; the installer's record is the newest either way.
        archive = Path(facts["archive"].replace("\\", "/")).name if facts["archive"] else ""
        if not archive:
            return False
        instance_id, _label = self._instance()
        since = datetime.fromisoformat(facts["at"].replace("Z", "+00:00")).timestamp() - _INSTALLER_RECORD_SECONDS
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            return store.installer_record_of(instance_id, facts["name"], archive, since) is not None

    def _record_plain(self, facts: dict) -> None:
        if self._executor is None:
            return
        job = self._plain_job(facts, "install")
        generation = self._lifecycle_generation
        handle = self._executor.run(lambda _post, cancelled: job.run(cancelled), lambda _message: None,
                                    lambda value, error: self._plain_done(job, facts, generation, value, error))
        self._plain_handles = [other for other in self._plain_handles if not other.finished] + [handle]

    def _plain_done(self, job: plain_record.PlainJob, facts: dict, generation: int, result, error) -> None:
        key = job.mod_name.casefold()
        self._plain_handles = [handle for handle in self._plain_handles if not handle.finished]
        if not self._active or generation != self._lifecycle_generation:
            return
        try:
            if error is not None:
                raise error
            if result.mode == plain_record.CANCELLED:
                return
            if result.mode == plain_record.FAILED:
                self._plain_errors[key] = result.reason
            else:
                self._plain_errors.pop(key, None)
                self._store_plain(result, facts)
        except Exception as exc:
            self._plain_errors[key] = f"{job.mod_name}: {exc}"
            print(f"[Modlist Companion] unable to record the install of {job.mod_name}: {exc}", file=sys.stderr)
        try:
            self._invalidate_problems()
            self._notify_viewer()
        except Exception as exc:       # this runs in a Qt slot: an exception here would end MO2
            print(f"[Modlist Companion] unable to refresh after an install: {exc}", file=sys.stderr)

    def _store_plain(self, result, facts: dict) -> bool:
        """Keep the plain record of an install, unless the mod is gone by now or an installer recorded this
        install while the folder was being read (its record is the one to keep)."""

        event, job = result.event, result.job
        if self._organizer.modList().getMod(job.mod_name) is None or self._covered_by_installer(facts):
            return False
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            if any(mod["name"].casefold() == job.mod_name.casefold() for mod in store.current_mods(job.instance_id)):
                event = {**event, "source": {**event["source"], "reinstallation": True}}
            return store.ingest(event)

    def _plain_problem(self) -> str:
        if not self._plain_errors:
            return ""
        shown = list(self._plain_errors.values())
        more = f" and {len(shown) - 3} more" if len(shown) > 3 else ""
        return "Modlist Companion could not record: " + "; ".join(shown[:3]) + more + "."

    # ---------- scanning the mods that have no record ----------

    def _edition_scan_jobs(self, instance_id: str, label: str) -> list:
        """The scan's jobs that only this edition has (the FOMOD Plus edition rebuilds installs from what FOMOD Plus
        saved)."""

        return []

    def _scan_jobs(self) -> list:
        """A job for every installed mod that has no record: first the edition's own, then a plain record for the
        rest. Read on MO2's thread: a few calls per mod, and (the FOMOD Plus edition) the choices FOMOD Plus saved
        and when each such folder was made; the folders and the archives are read on the worker."""

        if not self._active:
            return []
        instance_id, label = self._instance()
        jobs = list(self._edition_scan_jobs(instance_id, label))
        claimed = {job.mod_name.casefold() for job in jobs}
        mod_list = self._organizer.modList()
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            have = {mod["name"].casefold() for mod in store.current_mods(instance_id)}
        game = self._game_info().get("nexus_domain", "")
        for name in mod_list.allMods():
            name = str(name)
            if name.casefold() in have or name.casefold() in claimed:
                continue
            mod = mod_list.getMod(name)
            if mod is None or not self._is_real_mod(mod):
                continue
            try:
                facts = self._mod_facts(mod, resolve_archive=False)
            except Exception:
                continue
            jobs.append(self._plain_job(facts, "scan", game))
        return jobs

    def _unrecorded_count(self) -> int:
        """How many mods have no record yet (what a scan would record), without building the jobs: a name check against
        the database for each mod and, for the few that have none, whether it is a real mod. The window asks this each
        time it opens."""

        if not self._active:
            return 0
        instance_id, _label = self._instance()
        mod_list = self._organizer.modList()
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            have = {mod["name"].casefold() for mod in store.current_mods(instance_id)}
        count = 0
        for name in mod_list.allMods():
            name = str(name)
            if name.casefold() in have:
                continue
            mod = mod_list.getMod(name)
            if mod is not None and self._is_real_mod(mod):
                count += 1
        return count

    def _store_scan_result(self, result) -> bool:
        """Store a scanned plain record, unless the mod got a record in the meantime (a newer one is never overwritten)."""

        if not self._active:
            return False
        job = result.job
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            if any(mod["name"].casefold() == job.mod_name.casefold() for mod in store.current_mods(job.instance_id)):
                return False
            stored = store.ingest(result.event)
        self._plain_errors.pop(job.mod_name.casefold(), None)
        self._notify_viewer()
        return stored

    def _on_mod_removed(self, mod_name: str) -> None:
        if not self._active:
            return
        try:
            instance_id = stable_instance_id(Path(self._organizer.basePath()))
            removed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
                store.mark_removed(instance_id, str(mod_name), removed_at)
                # Alerts about a removed mod are moot, and a later install of it announces again.
                store.forget_patch_alerts(instance_id, str(mod_name))
            key = str(mod_name).casefold()
            # Forgotten, so that installing it again is new.
            for table in (self._install_checks, self._enable_watch, self._known_plugins, self._plain_errors):
                table.pop(key, None)
            if self._known_mods is not None:
                self._known_mods.pop(key, None)
            self._mod_forgotten(instance_id, key)
            self._history_versions.pop(str(mod_name), None)
            self._state_error = ""
            self._notify_viewer()
            self._alerts_changed()
        except Exception as exc:
            self._state_error = f"Unable to record removal of {mod_name}: {exc}"
            print(f"[Modlist Companion] {self._state_error}", file=sys.stderr)

    def _mod_forgotten(self, instance_id: str, mod_key: str) -> None:
        """A mod was removed: an edition drops what else it keeps about it."""

    def _reconcile_current_mods(self) -> None:
        """Catch removals and renames that happened while Logger was not running."""

        if not self._active:
            return
        try:
            mod_list = self._organizer.modList()
            installed = [str(name) for name in mod_list.allMods()]
            # MO2 compares mod names case-insensitively, and so does storage.
            installed_folded = {name.casefold() for name in installed}
            instance_id = stable_instance_id(Path(self._organizer.basePath()))
            removed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            changed = False
            with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
                current = store.current_mods(instance_id)
                spelling = {name.casefold(): name for name in installed}
                current_folded = {mod["name"].casefold() for mod in current}
                unlogged: dict[str, list[str]] = {}
                for name in installed:
                    if name.casefold() not in current_folded:
                        archive = self._archive_name(mod_list, name)
                        if archive:
                            unlogged.setdefault(archive, []).append(name)
                for mod in current:
                    name = mod["name"]
                    folded = name.casefold()
                    if folded in installed_folded:
                        if name not in installed:
                            # A case-only rename: follow MO2's new spelling.
                            store.rename_mod(instance_id, name, spelling[folded], offline=True)
                            changed = True
                        continue
                    # MO2 reports no renames. A renamed mod looks like a removal
                    # plus an unlogged mod from the same archive whose folder
                    # was made by this install, not before it. One such mod:
                    # move the record to it. Several: unclear, leave it. None:
                    # the mod was removed.
                    since = _seconds(mod["first_started_at"]) - _CREATED_SLACK_SECONDS
                    candidates = [
                        candidate for candidate in unlogged.get(_archive_key(mod["archive"]), [])
                        if (self._folder_created(mod_list, candidate) or 0.0) >= since
                    ]
                    if len(candidates) == 1:
                        store.rename_mod(instance_id, name, candidates[0], offline=True)
                        unlogged[_archive_key(mod["archive"])].remove(candidates[0])
                        changed = True
                    elif not candidates:
                        store.mark_removed(instance_id, name, removed_at, offline=True)
                        store.forget_patch_alerts(instance_id, name)
                        changed = True
            self._state_error = ""
            if changed:
                self._notify_viewer()
        except Exception as exc:
            self._state_error = f"Unable to reconcile installed mods: {exc}"
            print(f"[Modlist Companion] {self._state_error}", file=sys.stderr)

    @staticmethod
    def _folder_created(mod_list, name: str) -> float | None:
        """When a mod's folder was created; a rename keeps this time."""

        try:
            info = os.stat(str(mod_list.getMod(name).absolutePath()))
        except Exception:
            return None
        # On Windows st_ctime is the creation time.
        return getattr(info, "st_birthtime", info.st_ctime)

    @staticmethod
    def _archive_name(mod_list, name: str) -> str:
        try:
            return _archive_key(str(mod_list.getMod(name).installationFile()))
        except Exception:
            return ""

    def _new_executor(self):
        return background.make_qt_executor()

    def _start_background(self) -> None:
        """Make the executor that runs the work off the UI thread (an edition's ``_edition_start`` calls this)."""

        self._executor = self._new_executor()
        self._plain_handles = []

    def _shutdown(self) -> None:
        self._scanner.reset()               # a scan whose end can no longer arrive must not block the next one
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.close()
            self._retired.append(executor)
        for handle in self._plain_handles:      # the walks of folders that are still running stop at their next check
            handle.cancel()
        self._plain_handles = []
        if not self._active and self._timer is None and self._window is None:
            return
        self._active = False
        self._ready = False
        self._lifecycle_generation += 1

        watcher = self._watcher
        self._watcher = None
        if watcher is not None:
            watcher.deleteLater()

        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        history_timer, self._history_move_timer = self._history_move_timer, None
        if history_timer is not None:
            history_timer.stop()
            history_timer.deleteLater()
        self._history.reset()

        window = self._window
        self._window = None
        if window is not None:
            window.close()
            window.deleteLater()

    def name(self) -> str:
        # The product is Modlist Companion (formerly FOMOD Companion), but this is the plugin's internal name and must not
        # change with it: MO2 stores a plugin's settings and enabled state under it ("FOMOD Logger", "FOMOD Logger Plus",
        # "FOMOD Logger Native Installer"), and the data folders and the database are named for it too. Only
        # ``displayName`` is shown. Each edition keeps its own.
        return self.PLUGIN_NAME

    def author(self) -> str:
        return "sayngwyn"

    def description(self) -> str:
        return self.DESCRIPTION

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(_MO2_VERSION, mobase.VersionScheme.REGULAR)

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting(
                "open_window_on_start", "Open the Modlist Companion window when MO2 starts", False),
            mobase.PluginSetting(
                "alert_new_patches",
                "Tell me in MO2 when a mod I install is what a patch I skipped was made for", True),
        ]

    def _open_if_active(self, generation: int) -> None:
        if self._active and generation == self._lifecycle_generation:
            self.display()

    def displayName(self) -> str:
        return self.DISPLAY_NAME

    def tooltip(self) -> str:
        return self.TOOLTIP

    def icon(self) -> QIcon:
        return QIcon()

    def display(self) -> None:
        self._open_window()

    def _open_window(self, highlight: list[tuple[str, str, str]] | None = None) -> None:
        if not self._active:
            return
        self._ingest()
        if self._window is None:
            self._window = self._create_window()
        if highlight:
            self._window.highlight_alerts(highlight)
        # A normal window beside MO2, not a dialog: MO2 stays usable while
        # it is open, and it updates as installs and plugin changes happen.
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _window_options(self) -> dict:
        """What this edition's window does differently (see ``LoggerWindow``)."""

        return {"scanner": self._scanner}

    # ---------- what an edition provides ----------

    def _edition_start(self) -> None:
        """Run while the plugin starts, before the load order is read. May raise: the plugin then stays inactive."""

    def _refresh_cached_state(self) -> tuple:
        """Read, on MO2's thread, what the edition's problems depend on in other plugins (is FOMOD Plus enabled...), keep
        it for ``_failure_keys`` (which MO2 asks from another thread) and return it: a change is told to MO2."""

        return ()

    def _ingest(self) -> None:
        """Bring new installs into the database."""

    def _data_watch_paths(self) -> list[Path]:
        """Folders in the data folder whose changes mean ``_ingest`` has something to do."""

        return []

    def _failure_keys(self) -> list[int]:
        """The keys of this edition's failures that are active now, in the order MO2 should list them."""

        return [1] if self._runtime_error else []

    def _short_text(self, key: int) -> str:
        return {1: "Modlist Companion encountered a runtime error"}.get(key, "")

    def _full_text(self, key: int) -> str:
        return {1: self._runtime_error}.get(key, "")

    def _create_window(self):
        # Imported here so MO2 loads Qt Widgets only when the window opens.
        from fomod_logger_support.viewer.window import LoggerWindow

        return LoggerWindow(
            self._data_root / "fomod_logger.sqlite3",
            load_order=lambda: self._load_order,
            on_error=self._on_window_error,
            on_changed=self._alerts_changed,
            geometry_file=self._data_root / "window.json",
            instance_id=lambda: stable_instance_id(Path(self._organizer.basePath())),
            game=self._game_info,
            profiles=self._profiles_snapshot,
            current_profile=self._profile_name,
            **self._window_options(),
        )

    def _profiles_snapshot(self) -> dict | None:
        """Which of this instance's profiles have each mod enabled. MO2 keeps every profile in one folder, the
        parent of the current profile's folder; only the profiles' modlist.txt files are read, never written."""

        folder = self._profile_folder()
        return read_profiles(folder.parent) if folder is not None else None

    def _game_info(self) -> dict[str, str]:
        """The managed game's name and its Nexus Mods address name, for the exported guide and choices file."""

        try:
            game = self._organizer.managedGame()
            return {"name": str(game.gameName()), "nexus_domain": str(game.gameNexusName())}
        except Exception:
            return {}

    def setParentWidget(self, _widget) -> None:
        return

    def activeProblems(self) -> list[int]:
        # A failed window read is usually transient; don't report it forever.
        if self._window_error and time.monotonic() - self._window_error_at > _WINDOW_ERROR_SECONDS:
            self._window_error = ""
        problems = self._failure_keys()
        if self._active and self._alerts_setting:
            problems.extend(group["key"] for group in self._alert_groups)
        return problems

    def shortDescription(self, key: int) -> str:
        group = self._alert_group(key)
        if group is not None:
            return patch_alerts.describe_group(group)[0]
        return self._short_text(key)

    def fullDescription(self, key: int) -> str:
        group = self._alert_group(key)
        if group is not None:
            _short, lines, advice = patch_alerts.describe_group(group)
            # MO2 shows this in a rich-text box, which wraps only when the text has no line breaks of its own.
            return ("<p>" + "</p><p>".join(html.escape(line, quote=False) for line in lines)
                    + "</p><p>" + " ".join(html.escape(line, quote=False) for line in advice) + "</p>")
        return self._full_text(key)

    def hasGuidedFix(self, key: int) -> bool:
        return self._alert_group(key) is not None

    def startGuidedFix(self, key: int) -> None:
        """Open the window with the new patches' cards marked, and settle the alert."""

        group = self._alert_group(key)
        if group is None:
            return
        try:
            with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
                store.clear_patch_alerts(group["rows"])
        except Exception as exc:
            print(f"[Modlist Companion] unable to settle the new-patch alert: {exc}", file=sys.stderr)
        self._close_problems_dialog()
        self._open_window(highlight=[(row["event_id"], row["option_id"], group["mod_name"]) for row in group["rows"]])
        self._alerts_changed()
