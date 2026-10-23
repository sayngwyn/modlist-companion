"""What the plugin remembers between two looks at the list, to keep its change history (see ``core/history.py``).

The tracker holds no MO2 objects: the plugin hands it a snapshot (what MO2 reports for the current profile) and tells
it about profile switches and program launches. It opens the database for each piece of work and closes it, like the
rest of the plugin, and it runs on MO2's main thread.

* ``look`` compares a snapshot with the last one of the same instance and profile. The first look of a profile in this
  run compares with the snapshot stored when MO2 was last closed (or last had that profile), so changes made while
  Companion wasn't watching are found at start; they are marked ``offline``. Later looks compare with the previous
  look.
* ``profile_switched`` records the switch and makes the next look compare with the new profile's stored snapshot. While
  the switch is pending (MO2 hasn't finished loading the new profile's lists) looks are skipped, so a half-loaded
  list is never taken for changes.
* ``run_started`` and ``run_finished`` record a program MO2 launches and when it ends.
"""

from __future__ import annotations

import ntpath
from pathlib import Path
from typing import Callable

from .core import history
from .core.storage import EventStore

# Friendly names for the programs people start from MO2; anything else is shown by its file name.
_PROGRAMS = {
    "skyrimse": "Skyrim Special Edition", "skse64_loader": "Skyrim Special Edition (SKSE)",
    "skyrimselauncher": "Skyrim Special Edition launcher", "skyrimvr": "Skyrim VR", "skyrim": "Skyrim",
    "skyrimse_vr": "Skyrim VR", "fallout4": "Fallout 4", "f4se_loader": "Fallout 4 (F4SE)",
    "fallout4launcher": "Fallout 4 launcher", "fallout4vr": "Fallout 4 VR", "falloutnv": "Fallout New Vegas",
    "fallout3": "Fallout 3", "oblivion": "Oblivion", "starfield": "Starfield", "sfse_loader": "Starfield (SFSE)",
    "sseedit": "SSEEdit", "sseedit64": "SSEEdit", "fo4edit": "FO4Edit", "fo4edit64": "FO4Edit",
    "tes5edit": "TES5Edit", "tes5edit64": "TES5Edit", "xedit": "xEdit", "xedit64": "xEdit",
    "loot": "LOOT", "creationkit": "Creation Kit", "bodyslide x64": "BodySlide", "bodyslide": "BodySlide",
    "outfitstudio x64": "Outfit Studio", "nifskope": "NifSkope", "synthesis": "Synthesis", "pandora": "Pandora",
    "pandora behaviour engine+": "Pandora", "nemesis unlimited behavior engine": "Nemesis",
    "dyndolod": "DynDOLOD", "dyndolod64": "DynDOLOD", "texgen": "TexGen", "texgen64": "TexGen",
    "grass cache": "Grass cache generator", "wrye bash": "Wrye Bash", "modorganizer": "Mod Organizer 2",
}


def program_name(path: str) -> str:
    """A program's name for the list: a known one's usual name, otherwise its file name without ``.exe``."""

    stem = ntpath.basename(str(path).replace("/", "\\"))
    stem = stem[:-4] if stem.lower().endswith(".exe") else stem
    return _PROGRAMS.get(stem.lower(), stem or "A program")


def _path_key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(path).replace("/", "\\")))


class HistoryTracker:
    def __init__(self, database: Callable[[], Path | None], instance: Callable[[], str], profile: Callable[[], str], *,
                 clock: Callable[[], str] = history.utc_text, keep: int = history.KEEP_ENTRIES):
        self._database, self._instance, self._profile = database, instance, profile
        self._clock, self._keep = clock, keep
        self.last: dict | None = None
        self.key: tuple[str, str] | None = None
        self._started = False           # whether this run has looked at the list yet
        self._first_found = history.FOUND_START      # what a change found by the first look is marked as
        self.switch_pending = False
        self._runs: list[tuple[str, int]] = []      # (program path key, entry id) of programs that have not ended
        self.errors = 0

    def reset(self, *, again: bool = False) -> None:
        """The plugin was switched off and on (``again``) or is starting: forget what was last seen (the stored snapshot
        is the baseline)."""

        self.last, self.key, self._started, self.switch_pending, self._runs = None, None, False, False, []
        self._first_found = history.FOUND_RESTART if again else history.FOUND_START

    # --- snapshots ---------------------------------------------------------------------------------------------

    def look(self, snapshot: dict) -> int:
        """Compare this look at the list with the last one. Returns how many entries it added (0 when nothing changed)."""

        instance, profile, database = self._instance(), self._profile(), self._database()
        if not instance or not profile or database is None or self.switch_pending:
            return 0
        if not snapshot.get("mods"):
            return 0                # MO2 hasn't read its list: this says nothing
        key = (instance, profile)
        if key == self.key and snapshot == self.last:
            return 0
        added = 0
        with EventStore(database) as store:
            now = self._clock()
            found = self._first_found if not self._started else history.FOUND_PROFILE
            if key != self.key or self.last is None:
                stored = store.history_snapshot(instance, profile)
                entries, taken = [], None
                if stored is not None:
                    entries, taken = history.diff(stored[0], snapshot), stored[1]
                if not self._started:
                    earlier = store.history_last_profile(instance)
                    if earlier not in (None, profile):
                        added += store.history_add(instance, profile, [self._switch_entry(earlier, profile)],
                                                   at=now, offline=found)
                added += store.history_save(instance, profile, snapshot, entries, at=now, offline=found,
                                            dedupe_since=taken, keep=self._keep)
            else:
                entries = history.diff(self.last, snapshot)
                added += store.history_save(instance, profile, snapshot, entries, at=now, keep=self._keep)
        self.last, self.key, self._started = snapshot, key, True
        return added

    @staticmethod
    def _switch_entry(before: str, after: str) -> dict:
        return {"kind": history.PROFILE_SWITCHED, "count": 1, "subject": after,
                "items": [{"name": after, "before": before, "after": after}]}

    def profile_switched(self, old: str, new: str) -> int:
        """MO2 switched profile. Looks wait for ``settle_switch`` (the new profile's lists are still loading)."""

        instance, database = self._instance(), self._database()
        # MO2 also says a profile "changed" when it sets its first one at start-up (nothing before it): that is no switch,
        # and the first look already waits for MO2 to have read its list, so there is nothing to wait for or record.
        if not old or old == new:
            return 0
        self.last, self.key, self.switch_pending = None, None, True
        if not instance or not new or database is None:
            return 0
        with EventStore(database) as store:
            return store.history_add(instance, new, [self._switch_entry(old, new)], at=self._clock())

    def settle_switch(self) -> bool:
        """The new profile's lists are loaded: looks may go on. True when a switch was pending."""

        pending, self.switch_pending = self.switch_pending, False
        return pending

    # --- programs ------------------------------------------------------------------------------------------------

    def run_started(self, path: str) -> int | None:
        instance, database, profile = self._instance(), self._database(), self._profile()
        if not instance or database is None or not path:
            return None
        with EventStore(database) as store:
            entry_id = store.history_start_run(instance, profile, program_name(path), str(path), at=self._clock(),
                                               keep=self._keep)
        self._runs.append((_path_key(path), entry_id))
        return entry_id

    def run_finished(self, path: str, exit_code: int | None) -> bool:
        """A program ended. It is matched to the newest launch of the same program that hasn't ended: MO2 does not
        always say when one ends (after Unlock, or a launch it vetoed), and that launch's entry must not take the end
        of the next one. Older launches of the same program that never reported an end stay "end not seen"."""

        database = self._database()
        key = _path_key(path)
        matches = [run for run in self._runs if run[0] == key]
        if not matches or database is None:
            return False
        match = matches[-1]
        self._runs = [run for run in self._runs if run[0] != key]      # the older ones will never end now
        with EventStore(database) as store:
            return store.history_finish_run(match[1], None if exit_code is None else int(exit_code), at=self._clock())
