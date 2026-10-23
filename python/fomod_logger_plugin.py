"""Mod Organizer 2 entry point for Modlist Companion, the full edition: its own native installer records every
FOMOD choice as it is made. (The edition for FOMOD Plus is ``fomod_logger_plus_plugin.py``; the shared part is
``fomod_logger_support/plugin_base.py``.)"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import mobase
from PyQt6.QtCore import QTimer

# MO2 loads this file directly; its plugins directory need not be on sys.path.
_PLUGIN_ROOT = Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))
_SUPPORT_ROOT = _PLUGIN_ROOT / "fomod_logger_support"
_NATIVE_ROOT = _SUPPORT_ROOT / "native"
_DLL_DIRECTORY = (
    os.add_dll_directory(str(_NATIVE_ROOT))
    if os.name == "nt" and _NATIVE_ROOT.is_dir()
    else None
)

from fomod_logger_support import plugin_base as _base  # noqa: E402
from fomod_logger_support.core.identity import stable_instance_id  # noqa: E402
from fomod_logger_support.core.migration import load_legacy_file  # noqa: E402
from fomod_logger_support.core.storage import EventStore  # noqa: E402
from fomod_logger_support.plugin_base import CompanionPlugin, _seconds  # noqa: E402, F401

_NATIVE_PLUGIN_NAME = "FOMOD Logger Native Installer"
_FOMOD_PLUS_NAME = "FOMOD Plus"
# The other edition's internal plugin name (it takes the install events the same way, so only one may run).
_PLUS_EDITION_NAME = "FOMOD Logger Plus"
_INGEST_RETRY_DELAY_MS = 2_000
_INGEST_RETRY_LIMIT = 3
_WINDOW_ERROR_SECONDS = _base._WINDOW_ERROR_SECONDS

# MO2 problem keys (the alerts for new patches count up from 100).
_KEY_RUNTIME, _KEY_PROVIDER, _KEY_IMPORT, _KEY_MIGRATION = 1, 2, 3, 4
_KEY_INGEST, _KEY_STATE, _KEY_WINDOW, _KEY_LEGACY = 5, 6, 7, 8
_KEY_FOMOD_PLUS, _KEY_OTHER_EDITION, _KEY_PLAIN = 9, 10, 13


class FomodLoggerPlugin(CompanionPlugin):
    PLUGIN_NAME = "FOMOD Logger"
    DISPLAY_NAME = "Modlist Companion"
    DESCRIPTION = ("Remembers the FOMOD choices you made, records every mod, and keeps a history of what changed in "
                   "your list.")
    DATA_FOLDER = "fomod_logger"

    def __init__(self) -> None:
        super().__init__()
        self._ingest_error = ""
        self._ingest_retries = 0
        self._provider_error = ""
        self._import_error = ""
        self._migration_error = ""
        self._legacy_error = ""
        self._fomod_plus_error = ""
        self._other_edition_error = ""

    # ---------- what this edition provides ----------

    def _edition_start(self) -> None:
        self._start_background()
        self._cached_state = self._refresh_cached_state()
        self._check_legacy_files()
        self._migrate_once()
        self._ingest()

    def _data_watch_paths(self) -> list[Path]:
        return [self._data_root / "events"]

    def _covered_by_installer(self, facts: dict) -> bool:
        # The native installer writes its event file when the install ends, just before MO2 announces the mod: if
        # that event is in the database now, the install has its record. Otherwise it was a plain install.
        return self._installer_recorded(facts)

    def _ingest(self) -> None:
        if not self._active:
            return
        try:
            self._refresh_provider_health()
            with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
                imported = store.ingest_directory(self._data_root / "events").imported
                errors = store.import_errors()
            previous_error = self._import_error
            if errors:
                first = errors[0]
                self._import_error = (
                    f"{len(errors)} installation event file(s) could not be imported; "
                    f"{first['path']}: {first['error']}"
                )
            else:
                self._import_error = ""
            self._ingest_error = ""
            if imported:
                print(
                    f"[Modlist Companion] imported {imported} installation event(s)",
                    file=sys.stderr,
                )
            if imported or self._import_error != previous_error:
                self._notify_viewer()
            self._ingest_retries = 0
        except Exception as exc:
            self._ingest_error = str(exc)
            print(f"[Modlist Companion] event ingestion failed: {exc}", file=sys.stderr)
            # Nothing re-scans on a timer, so retry a transient failure (such as
            # a briefly locked database) a few times before waiting for the
            # next folder change.
            if self._ingest_retries < _INGEST_RETRY_LIMIT:
                self._ingest_retries += 1
                QTimer.singleShot(_INGEST_RETRY_DELAY_MS * self._ingest_retries, self._ingest)

    def _check_legacy_files(self) -> None:
        """Find what FOMOD Logger 1.x left in MO2's plugins folder.

        1.x copied its own build over MO2's installer_fomod.dll, keeping the
        stock file as installer_fomod_original.dll and its build as
        installer_fomod_logger.dll. MO2 loads every DLL there, so after an
        upgrade those copies run beside this installer. They are in use while
        MO2 runs, so the user has to put them right with MO2 closed.
        """

        try:
            plugins = Path(self._organizer.pluginsPath())
        except Exception:
            self._legacy_error = ""
            return
        original = plugins / "installer_fomod_original.dll"
        custom = plugins / "installer_fomod_logger.dll"
        if not original.exists() and not custom.exists():
            self._legacy_error = ""
            return
        steps = ["Close Mod Organizer, then in its plugins folder:"]
        if original.exists():
            steps.append("rename installer_fomod_original.dll to installer_fomod.dll, "
                         "replacing the file there (this restores MO2's own FOMOD installer);")
        else:
            steps.append("restore installer_fomod.dll from your Mod Organizer download "
                         "(1.x replaced it and its backup is missing);")
        if custom.exists():
            steps.append("delete installer_fomod_logger.dll.")
        self._legacy_error = (
            "Files from FOMOD Logger 1.x are still in Mod Organizer's plugins folder "
            f"({plugins}). " + " ".join(steps)
        )

    def _refresh_provider_health(self) -> None:
        try:
            enabled = self._organizer.isPluginEnabled(_NATIVE_PLUGIN_NAME)
        except Exception as exc:
            self._provider_error = f"Unable to verify the native capture provider: {exc}"
            return
        self._provider_error = (
            "The Modlist Companion native capture provider is disabled or unavailable. "
            "Installations cannot be logged until it is enabled."
            if not enabled
            else ""
        )

    def _migrate_once(self) -> None:
        # 1.x kept fomod_data.json in each instance's base folder, or in the
        # plugin data folder that every instance of one MO2 installation
        # shares. Migrate each instance's own file once per instance, and the
        # shared file only once. Migrated events have stable IDs, so a repeat
        # of the same source is harmless.
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            if store.created:
                # Migrated 1.x records live only in the database. A new
                # database (say, after a test build's file was moved aside)
                # must migrate them again.
                for marker in self._data_root.glob(".legacy-migration-v1*"):
                    marker.unlink(missing_ok=True)
        instance_id = stable_instance_id(Path(self._organizer.basePath()))
        sources = [
            (Path(self._organizer.basePath()) / "fomod_data.json",
             self._data_root / f".legacy-migration-v1-{instance_id}"),
            # Earlier test builds wrote this marker after migrating both files.
            (Path(self._organizer.pluginDataPath()) / "fomod_data.json",
             self._data_root / ".legacy-migration-v1"),
        ]
        sources = [(path, marker) for path, marker in sources if not marker.exists()]
        if not sources:
            return
        candidates = [path for path, _marker in sources]
        failures = []
        pending = []
        # Validate every legacy source before importing any of their records.
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                events = load_legacy_file(
                    candidate,
                    instance_path=Path(self._organizer.basePath()),
                    instance_label=Path(self._organizer.basePath()).name,
                )
                pending.append((candidate, events))
            except (OSError, UnicodeError, ValueError) as exc:
                failures.append(f"{candidate}: {exc}")

        if not failures:
            with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
                for candidate, events in pending:
                    try:
                        for event in events:
                            store.ingest(event)
                    except (OSError, UnicodeError, ValueError) as exc:
                        failures.append(f"{candidate}: {exc}")

        if failures:
            self._migration_error = (
                "Legacy data was not migrated; the original file was left unchanged. "
                + failures[0]
            )
            print(f"[Modlist Companion] {self._migration_error}", file=sys.stderr)
            return

        for _path, marker in sources:
            marker.write_text("migration completed\n", encoding="utf-8")
        self._migration_error = ""


    def _refresh_cached_state(self) -> tuple:
        self._refresh_provider_health()
        self._refresh_conflicts()
        return (self._provider_error, self._fomod_plus_error, self._other_edition_error)

    def _refresh_conflicts(self) -> None:
        """FOMOD Plus (installer priority 999) is offered every FOMOD before this edition's native installer (120 and
        80), and takes it; the FOMOD Plus edition of Modlist Companion is the one that reads what it saves. Never
        raise the native installer's priority to get around that. Both editions together record the same installs
        twice over."""

        try:
            plus = bool(self._organizer.isPluginEnabled(_FOMOD_PLUS_NAME))
        except Exception:
            plus = False
        self._fomod_plus_error = (
            "FOMOD Plus is enabled in Mod Organizer. It takes every FOMOD install before this edition's installer "
            "is offered one, so this edition of Modlist Companion records nothing new. Use the Modlist Companion (for "
            "FOMOD Plus) edition instead of this one, or disable FOMOD Plus." if plus else "")
        try:
            other = bool(self._organizer.isPluginEnabled(_PLUS_EDITION_NAME))
        except Exception:
            other = False
        self._other_edition_error = (
            "Both editions of Modlist Companion are enabled: this one (with its own installer) and Modlist Companion "
            "(for FOMOD Plus). Keep one and disable the other in Mod Organizer's plugin settings; the FOMOD Plus "
            "edition is the one to keep when you use FOMOD Plus." if other else "")

    # ---------- MO2's problem list ----------

    def _failure_keys(self) -> list[int]:
        # MO2 asks from another thread: the enabled state of the native installer, FOMOD Plus and the other edition
        # was read on MO2's thread (``_refresh_cached_state``) and is only reported here.
        problems = []
        for key, text in ((_KEY_RUNTIME, self._runtime_error), (_KEY_INGEST, self._ingest_error),
                          (_KEY_STATE, self._state_error), (_KEY_PROVIDER, self._provider_error),
                          (_KEY_IMPORT, self._import_error), (_KEY_MIGRATION, self._migration_error),
                          (_KEY_WINDOW, self._window_error), (_KEY_LEGACY, self._legacy_error),
                          (_KEY_FOMOD_PLUS, self._fomod_plus_error), (_KEY_OTHER_EDITION, self._other_edition_error),
                          (_KEY_PLAIN, self._plain_problem())):
            if text:
                problems.append(key)
        return problems

    def _short_text(self, key: int) -> str:
        return {
            _KEY_RUNTIME: "Modlist Companion encountered a runtime error",
            _KEY_PROVIDER: "Modlist Companion cannot capture installations",
            _KEY_IMPORT: "Modlist Companion could not import installation events",
            _KEY_MIGRATION: "Modlist Companion could not migrate legacy data",
            _KEY_INGEST: "Modlist Companion could not index installation events",
            _KEY_STATE: "Modlist Companion could not update installed-mod state",
            _KEY_WINDOW: "Modlist Companion window could not read its data",
            _KEY_LEGACY: "FOMOD Logger 1.x files need cleaning up",
            _KEY_FOMOD_PLUS: "FOMOD Plus is installed, so Modlist Companion records nothing new",
            _KEY_OTHER_EDITION: "Two editions of Modlist Companion are enabled; keep one",
            _KEY_PLAIN: "Modlist Companion could not record some mods",
        }.get(key, "")

    def _full_text(self, key: int) -> str:
        return {
            _KEY_RUNTIME: self._runtime_error,
            _KEY_PROVIDER: self._provider_error,
            _KEY_IMPORT: self._import_error,
            _KEY_MIGRATION: self._migration_error,
            _KEY_INGEST: self._ingest_error,
            _KEY_STATE: self._state_error,
            _KEY_WINDOW: self._window_error,
            _KEY_LEGACY: self._legacy_error,
            _KEY_FOMOD_PLUS: self._fomod_plus_error,
            _KEY_OTHER_EDITION: self._other_edition_error,
            _KEY_PLAIN: self._plain_problem(),
        }.get(key, "")


def createPlugin() -> mobase.IPlugin:
    return FomodLoggerPlugin()
