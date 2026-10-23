"""Mod Organizer 2 entry point for Modlist Companion, the FOMOD Plus edition.

This edition has no installer of its own: FOMOD Plus installs, and saves which options were picked in each mod's
``meta.ini``. Companion reads that through MO2, finds the mod's download archive, and rebuilds from the two the
installation event its own installer would have recorded, so the whole window works as it does in the full edition.
The shared part is ``fomod_logger_support/plugin_base.py``; the rebuilding is in ``fomod_logger_support/plus``.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import mobase
from PyQt6.QtCore import QCoreApplication, QTimer

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

from fomod_logger_support import plugin_base as _base  # noqa: E402,F401 - the tests reach the shared code through it
from fomod_logger_support.core import plain_record  # noqa: E402
from fomod_logger_support.core.identity import stable_instance_id  # noqa: E402
from fomod_logger_support.core.storage import EventStore  # noqa: E402
from fomod_logger_support.plugin_base import CompanionPlugin  # noqa: E402
from fomod_logger_support.plus import record as plus_record  # noqa: E402
from fomod_logger_support.plus.jobs import CANCELLED, FAILED, JobResult, RebuildJob, run_job, utc_text  # noqa: E402
from fomod_logger_support.plus.state import RecordState  # noqa: E402

_FOMOD_PLUS_NAME = plus_record.FOMOD_PLUS_NAME
# The other edition's internal plugin name (it also records installs, so only one may run).
_FULL_EDITION_NAME = "FOMOD Logger"
# MO2 may announce an install before FOMOD Plus has saved its choices; look again a little later, a few times.
_RECORD_RETRY_MS = (500, 1_500)

# MO2 problem keys (the alerts for new patches count up from 100).
_KEY_RUNTIME, _KEY_STATE, _KEY_WINDOW = 1, 6, 7
_KEY_OTHER_EDITION, _KEY_FOMOD_PLUS_MISSING, _KEY_RECORDS, _KEY_PLAIN = 10, 11, 12, 13

_MISSING_TEXT = ("Modlist Companion (for FOMOD Plus) needs FOMOD Plus; install it or use the standard edition")


def job_archive(job: RebuildJob) -> str:
    return Path(job.archive_path.replace("\\", "/")).name.casefold() if job.archive_path else ""


class FomodLoggerPlusPlugin(CompanionPlugin):
    PLUGIN_NAME = "FOMOD Logger Plus"
    DISPLAY_NAME = "Modlist Companion (for FOMOD Plus)"
    DESCRIPTION = ("Remembers the FOMOD choices FOMOD Plus saved, records every mod, and keeps a history of what "
                   "changed in your list. Needs FOMOD Plus; it has no installer of its own.")
    TOOLTIP = "Open Modlist Companion: your FOMOD choices and your list's history (for FOMOD Plus)"
    DATA_FOLDER = "fomod_logger_plus"

    def __init__(self) -> None:
        super().__init__()
        self._records: RecordState = None  # pyright: ignore[reportAttributeAccessIssue]  # made in _edition_start
        self._sevenzip = ""
        # Mods whose install is being rebuilt right now (folded name -> how many rebuilds), so that the look for
        # patches made for them waits until their install is stored.
        self._pending: dict[str, int] = {}
        # The archives (file names, folded) of the rebuilds under way for each mod: only an install of one of them is
        # covered by a rebuild.
        self._pending_archives: dict[str, list[str]] = {}
        self._record_errors: dict[str, str] = {}
        self._plus_missing_error = ""
        self._other_edition_error = ""

    # ---------- what this edition provides ----------

    def _edition_start(self) -> None:
        self._records = RecordState(self._data_root / "plus_records.json")
        self._start_background()
        self._sevenzip = self._find_sevenzip()
        self._pending.clear()
        self._pending_archives.clear()
        self._cached_state = self._refresh_cached_state()

    def _window_options(self) -> dict:
        # FOMOD Plus restores its own picks, so there is nothing for an imported choices file to do here.
        return {"can_import": False, "scanner": self._scanner, "edition_label": "for FOMOD Plus"}

    def _find_sevenzip(self) -> str:
        """MO2's own 7z.dll, which reads the download archives (zip files need none)."""

        from fomod_logger_support.archive.sevenzip import find_sevenzip_dll

        try:
            folder = Path(QCoreApplication.applicationDirPath())
        except Exception:
            folder = Path(sys.executable).resolve().parent
        found = find_sevenzip_dll(folder)
        if found is None:
            found = find_sevenzip_dll(Path(sys.executable).resolve().parent)
        return str(found) if found else ""

    def _shutdown(self) -> None:
        self._pending.clear()
        self._pending_archives.clear()
        super()._shutdown()

    # ---------- installs ----------

    def _check_held(self, key: str) -> bool:
        return self._pending.get(key, 0) > 0

    def _on_mod_installed(self, mod: mobase.IModInterface) -> None:
        if not self._active:
            return
        self._note_mod_versions(mod)
        try:
            facts = self._mod_facts(mod)
            text = plus_record.read_record(mod)
            if text:
                self._begin_install(facts, text)
            # Look again a little later whether or not a record is there: it may be the one left by an earlier
            # install, with FOMOD Plus's new choices still to come (a record that is already stored is skipped).
            generation = self._lifecycle_generation
            for delay in _RECORD_RETRY_MS:
                QTimer.singleShot(delay, lambda: self._look_again(facts["name"], generation))
        except Exception as exc:
            self._state_error = f"Unable to read the choices FOMOD Plus saved: {exc}"
            print(f"[Modlist Companion] {self._state_error}", file=sys.stderr)
        # After the rebuild is under way: a moment from now this is a plain install only if FOMOD Plus handled none.
        self._watch_install(mod)
        QTimer.singleShot(0, self._after_install)

    def _look_again(self, name: str, generation: int) -> None:
        if not self._active or generation != self._lifecycle_generation:
            return
        try:
            if self._pending.get(name.casefold()):
                return
            mod = self._organizer.modList().getMod(name)
            text = plus_record.read_record(mod) if mod is not None else None
            if text:
                self._begin_install(self._mod_facts(mod), text)
        except Exception as exc:
            print(f"[Modlist Companion] unable to read the choices FOMOD Plus saved for {name}: {exc}", file=sys.stderr)

    def _plugin_states(self) -> dict[str, str]:
        """The plugins MO2 lists and whether each is switched on: what the FOMOD's file conditions are checked
        against (the list at install time is not known; this is the closest there is)."""

        states: dict[str, str] = {}
        try:
            plugins = self._organizer.pluginList()
            for name in plugins.pluginNames():
                states[str(name)] = "Active" if plugins.state(name) == mobase.PluginState.ACTIVE else "Inactive"
        except Exception:
            return {}
        return states

    def _has_install(self, store: EventStore, instance_id: str, name: str, *, installer_only: bool = False) -> bool:
        """Whether the mod has a record. ``installer_only`` leaves out a plain record: a record of the install
        FOMOD Plus made takes the place of one made without an installer."""

        key = name.casefold()
        return any(mod["name"].casefold() == key and not (installer_only and mod["kind"] == plain_record.KIND)
                   for mod in store.current_mods(instance_id))

    def _covered_by_installer(self, facts: dict) -> bool:
        """Whether FOMOD Plus handled this install: its record is being rebuilt, or the record it saved is the one
        already stored for this very archive. Anything else was a plain install."""

        key = facts["name"].casefold()
        archive = Path(facts["archive"].replace("\\", "/")).name.casefold() if facts["archive"] else ""
        if archive in self._pending_archives.get(key, ()):
            return True
        mod = self._organizer.modList().getMod(facts["name"])
        try:
            saved = plus_record.read_record(mod) if mod is not None else None
        except Exception:
            saved = None
        if not saved:
            return False
        instance_id, _label = self._instance()
        earlier = self._records.get(instance_id, key)
        return earlier is not None and earlier.get("archive") == archive

    def _begin_install(self, facts: dict, text: str) -> None:
        """FOMOD Plus has just installed this mod (or saved new choices for it): rebuild its install off the UI
        thread, unless the choices and the archive are the ones already stored."""

        name, key = facts["name"], facts["name"].casefold()
        try:
            plus_record.parse_record(text)
        except plus_record.RecordError as error:
            self._record_errors[key] = f"{name}: {error}"
            return
        self._record_errors.pop(key, None)
        instance_id, label = self._instance()
        digest = plus_record.digest(text)
        archive_key = Path(facts["archive"].replace("\\", "/")).name.casefold() if facts["archive"] else ""
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            has_install = self._has_install(store, instance_id, name)
            has_choices = self._has_install(store, instance_id, name, installer_only=True)
        earlier = self._records.get(instance_id, key)
        if (has_choices and earlier is not None and earlier.get("digest") == digest
                and earlier.get("archive") == archive_key):
            return          # the same choices of the same archive: not a new install
        job = RebuildJob(
            instance_id=instance_id, instance_label=label, mod_name=name, mod_version=facts["version"],
            nexus_id=facts["nexus_id"], archive_path=facts["archive"], record=text, digest=digest,
            when=utc_text(), kind="install", reinstallation=has_install, plugin_states=self._plugin_states(),
            sevenzip_dll=self._sevenzip)
        generation = self._lifecycle_generation
        self._pending[key] = self._pending.get(key, 0) + 1
        self._pending_archives.setdefault(key, []).append(archive_key)
        self._executor.run(lambda _post, cancelled: run_job(job, cancelled), lambda _message: None,
                           lambda value, error: self._install_done(job, generation, value, error))

    def _install_done(self, job: RebuildJob, generation: int, result: JobResult, error) -> None:  # result is None only when error is set, and that raises first
        key = job.mod_name.casefold()
        left = self._pending.get(key, 0) - 1
        archives = self._pending_archives.get(key, [])
        if job_archive(job) in archives:
            archives.remove(job_archive(job))
        if left > 0:
            self._pending[key] = left
        else:
            self._pending.pop(key, None)
            self._pending_archives.pop(key, None)
        if not self._active or generation != self._lifecycle_generation:
            return
        try:
            if error is not None:
                raise error
            if result.mode == CANCELLED:
                return
            if result.mode == FAILED:
                self._record_errors[key] = result.reason
            else:
                self._record_errors.pop(key, None)
                self._store_result(result)
            self._state_error = ""
        except Exception as exc:
            self._state_error = f"Unable to store the choices FOMOD Plus saved for {job.mod_name}: {exc}"
            print(f"[Modlist Companion] {self._state_error}", file=sys.stderr)
        try:
            self._notify_viewer()
            self._capture_load_order()
        except Exception as exc:       # this runs in a Qt slot: an exception here would end MO2
            print(f"[Modlist Companion] unable to refresh after an install: {exc}", file=sys.stderr)

    def _store_result(self, result: JobResult) -> bool:
        """Keep the rebuilt install and remember the record it came from. False if the install was already there."""

        job, event = result.job, result.event
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            stored = store.ingest(event)
        self._records.set(job.instance_id, job.mod_name.casefold(), digest=job.digest,
                          archive=Path(event["source"]["archive"]).name.casefold(), event_id=event["event_id"])  # pyright: ignore[reportOptionalSubscript]  # only stored jobs have an event
        if stored:
            print(f"[Modlist Companion] stored the install of {job.mod_name} ({result.mode}"
                  f"{': ' + result.reason if result.reason else ''})", file=sys.stderr)
        return stored

    def _mod_forgotten(self, instance_id: str, mod_key: str) -> None:
        self._records.forget(instance_id, mod_key)
        self._record_errors.pop(mod_key, None)

    # ---------- the scan of mods installed before ----------

    def _edition_scan_jobs(self, instance_id: str, label: str) -> list[RebuildJob]:
        """A job for every installed mod that FOMOD Plus saved choices for and Companion has no install of (a plain
        record doesn't count: the choices take its place). Read on MO2's thread. A mod whose choices cannot be read is
        listed too, with a plain record to make instead (so it is not left without one); once it has that, it is left
        alone."""

        mod_list = self._organizer.modList()
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            current = store.current_mods(instance_id)
            have = {mod["name"].casefold() for mod in current if mod["kind"] != plain_record.KIND}
            plain = {mod["name"].casefold() for mod in current if mod["kind"] == plain_record.KIND}
        states = None
        jobs = []
        for name in mod_list.allMods():
            name = str(name)
            if name.casefold() in have:
                continue
            mod = mod_list.getMod(name)
            if mod is None:
                continue
            try:
                text = plus_record.read_record(mod)
            except Exception:
                continue
            if not text:
                continue
            if name.casefold() in plain:
                try:
                    plus_record.parse_record(text)
                except plus_record.RecordError:
                    continue            # unusable, and the mod has its plain record already
            if states is None:
                states = self._plugin_states()
            facts = self._mod_facts(mod)
            created = self._folder_created(mod_list, name)
            when = utc_text(datetime.fromtimestamp(created, timezone.utc)) if created else utc_text()
            jobs.append(RebuildJob(
                instance_id=instance_id, instance_label=label, mod_name=name, mod_version=facts["version"],
                nexus_id=facts["nexus_id"], archive_path=facts["archive"], record=text,
                digest=plus_record.digest(text), when=when, kind="scan", reinstallation=False,
                plugin_states=states, sevenzip_dll=self._sevenzip,
                fallback=self._plain_job(facts, "scan")))
        return jobs

    def _store_scan_result(self, result) -> bool:
        """Store a scanned install, unless the mod got one in the meantime (a newer install is never overwritten)."""

        if result.mode == plain_record.PLAIN:
            return super()._store_scan_result(result)
        if not self._active:
            return False
        job = result.job
        with EventStore(self._data_root / "fomod_logger.sqlite3") as store:
            if self._has_install(store, job.instance_id, job.mod_name, installer_only=True):
                return False
        self._record_errors.pop(job.mod_name.casefold(), None)
        stored = self._store_result(result)
        self._notify_viewer()
        return stored

    # ---------- conflicts and MO2's problem list ----------

    def _refresh_cached_state(self) -> tuple:
        self._refresh_conflicts()
        return (self._plus_missing_error, self._other_edition_error)

    def _refresh_conflicts(self) -> None:
        try:
            plus_there = bool(self._organizer.isPluginEnabled(_FOMOD_PLUS_NAME))
        except Exception:
            plus_there = False
        self._plus_missing_error = "" if plus_there else (
            _MISSING_TEXT + ". Without FOMOD Plus nothing new is recorded; what was recorded before stays.")
        try:
            other = bool(self._organizer.isPluginEnabled(_FULL_EDITION_NAME))
        except Exception:
            other = False
        self._other_edition_error = (
            "Both editions of Modlist Companion are enabled: this one (for FOMOD Plus) and the full edition with its "
            "own installer. Keep one and disable the other in Mod Organizer's plugin settings; when you use FOMOD "
            "Plus, keep this one." if other else "")

    def _failure_keys(self) -> list[int]:
        # MO2 asks from another thread: whether FOMOD Plus (and the other edition) are enabled was read on MO2's thread.
        problems = []
        for key, text in ((_KEY_RUNTIME, self._runtime_error), (_KEY_STATE, self._state_error),
                          (_KEY_FOMOD_PLUS_MISSING, self._plus_missing_error),
                          (_KEY_OTHER_EDITION, self._other_edition_error),
                          (_KEY_RECORDS, self._records_text()), (_KEY_WINDOW, self._window_error),
                          (_KEY_PLAIN, self._plain_problem())):
            if text:
                problems.append(key)
        return problems

    def _records_text(self) -> str:
        if not self._record_errors:
            return ""
        shown = list(self._record_errors.values())
        more = f" and {len(shown) - 3} more" if len(shown) > 3 else ""
        return "The choices FOMOD Plus saved could not be used for: " + "; ".join(shown[:3]) + more + "."

    def _short_text(self, key: int) -> str:
        return {
            _KEY_RUNTIME: "Modlist Companion encountered a runtime error",
            _KEY_STATE: "Modlist Companion could not update installed-mod state",
            _KEY_WINDOW: "Modlist Companion window could not read its data",
            _KEY_FOMOD_PLUS_MISSING: _MISSING_TEXT,
            _KEY_OTHER_EDITION: "Two editions of Modlist Companion are enabled; keep one",
            _KEY_RECORDS: "Modlist Companion could not use the choices FOMOD Plus saved for some mods",
            _KEY_PLAIN: "Modlist Companion could not record some mods",
        }.get(key, "")

    def _full_text(self, key: int) -> str:
        return {
            _KEY_RUNTIME: self._runtime_error,
            _KEY_STATE: self._state_error,
            _KEY_WINDOW: self._window_error,
            _KEY_FOMOD_PLUS_MISSING: self._plus_missing_error,
            _KEY_OTHER_EDITION: self._other_edition_error,
            _KEY_RECORDS: self._records_text(),
            _KEY_PLAIN: self._plain_problem(),
        }.get(key, "")


def createPlugin() -> mobase.IPlugin:
    return FomodLoggerPlusPlugin()
