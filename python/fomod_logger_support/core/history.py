"""The list's change history: what changed in a Mod Organizer 2 list, and when.

People who build a list by hand keep asking "what changed since it last worked?". This module works out the changes
without any Qt, MO2 or database code, so it can be tested alone (``history_store.py`` keeps them in SQLite and the
window shows them).

A **snapshot** is what the plugin reads from MO2 for one profile::

    {"mods": [every mod, in the left pane's order],      "enabled": [the mods that are switched on],
     "plugins": [every plugin, in load order],           "active": [the plugins that are switched on],
     "versions": {mod: [version, archive]}}              # optional; mods whose version is known

``diff`` compares two snapshots of one profile and returns **entries**: a ``kind``, a ``count`` and the ``items``
(each with a ``name`` and what it was ``before`` and is ``after``). One entry holds every item of its kind that
changed between the two snapshots, so one MO2 refresh, or a drag of twenty plugins, is one grouped entry ("12 plugins
moved") with the details underneath. ``merge_items`` joins entries made a moment apart (a burst) into one.

Mods installed, removed, renamed or updated also come from the install records (see ``history_store``): the
snapshot only finds them when no record said so, mostly because they happened while the plugin wasn't running.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

# --- kinds ----------------------------------------------------------------------------------------------------------

MOD_INSTALLED, MOD_UPDATED, MOD_REINSTALLED = "mod_installed", "mod_updated", "mod_reinstalled"
MOD_REMOVED, MOD_RENAMED = "mod_removed", "mod_renamed"
MOD_ENABLED, MOD_DISABLED, MOD_MOVED = "mod_enabled", "mod_disabled", "mod_moved"
PLUGIN_ENABLED, PLUGIN_DISABLED, PLUGIN_MOVED = "plugin_enabled", "plugin_disabled", "plugin_moved"
PROFILE_SWITCHED, PROGRAM_RUN = "profile_switched", "program_run"

KINDS = (MOD_INSTALLED, MOD_UPDATED, MOD_REINSTALLED, MOD_REMOVED, MOD_RENAMED, MOD_ENABLED, MOD_DISABLED, MOD_MOVED,
         PLUGIN_ENABLED, PLUGIN_DISABLED, PLUGIN_MOVED, PROFILE_SWITCHED, PROGRAM_RUN)

# What the window's filter buttons mean: (key, label, the kinds it shows).
GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("installs", "Installs and removals", (MOD_INSTALLED, MOD_UPDATED, MOD_REINSTALLED, MOD_REMOVED, MOD_RENAMED)),
    ("mods_on_off", "Mods on or off", (MOD_ENABLED, MOD_DISABLED)),
    ("mod_order", "Mod order", (MOD_MOVED,)),
    ("plugins_on_off", "Plugins on or off", (PLUGIN_ENABLED, PLUGIN_DISABLED)),
    ("plugin_order", "Plugin order", (PLUGIN_MOVED,)),
    ("profiles", "Profiles", (PROFILE_SWITCHED,)),
    ("programs", "Programs", (PROGRAM_RUN,)),
)
GROUP_KEYS = tuple(key for key, _label, _kinds in GROUPS)


def kinds_of(groups: Iterable[str]) -> tuple[str, ...]:
    chosen = set(groups)
    return tuple(kind for key, _label, kinds in GROUPS if key in chosen for kind in kinds)


# A mod that was installed, updated or removed is told by the install records; the snapshot diff tells it too when a
# record is missing. These are the kinds that are the same fact whichever side told it.
ADDING = (MOD_INSTALLED, MOD_UPDATED, MOD_REINSTALLED, MOD_RENAMED)
REMOVING = (MOD_REMOVED, MOD_RENAMED)

# How many items one entry keeps (its ``count`` is always the true number) and how many entries are kept per instance.
MAX_ITEMS = 500
KEEP_ENTRIES = 50_000
# Entries of one kind made within this many seconds are one burst.
BURST_SECONDS = 3.0
# A record and a snapshot that tell the same install or removal are taken for each other within this time.
SAME_FACT_SECONDS = 15 * 60


def name_key(name: str) -> str:
    """MO2 compares mod and plugin names without regard to case."""

    return name.casefold()


def same_archive(a: str, b: str) -> bool:
    """Whether two archive paths name the same file (the folder and the case don't matter)."""

    def file_name(path: str) -> str:
        return str(path or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()

    return file_name(a) == file_name(b)


# --- time -----------------------------------------------------------------------------------------------------------

def utc_text(moment: datetime | None = None) -> str:
    """UTC, millisecond ISO 8601 with a ``Z``: every entry's time, so that text order is time order."""

    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def normalize(value: str) -> str:
    """Any ISO 8601 time as ``utc_text`` (a text that isn't a time is returned as it is)."""

    try:
        return utc_text(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return str(value)


def seconds(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def shifted(value: str, delta_seconds: float) -> str:
    return utc_text(datetime.fromtimestamp(seconds(value) + delta_seconds, timezone.utc))


# --- snapshots ------------------------------------------------------------------------------------------------------

def make_snapshot(mods: Iterable[str], enabled: Iterable[str], plugins: Iterable[str] | None, active: Iterable[str],
                  versions: dict[str, tuple[str, str]] | None = None) -> dict[str, Any]:
    """A snapshot from what MO2 reports. ``plugins`` None means the load order couldn't be read (its changes are then
    not judged)."""

    snapshot: dict[str, Any] = {"mods": [str(name) for name in mods], "enabled": [str(name) for name in enabled],
                                "active": [str(name) for name in active]}
    if plugins is not None:
        snapshot["plugins"] = [str(name) for name in plugins]
    if versions is not None:
        snapshot["versions"] = {name: [str(value[0]), str(value[1])] for name, value in versions.items()}
    return snapshot


def valid_snapshot(value: Any) -> bool:
    return (isinstance(value, dict) and all(isinstance(value.get(key), list) for key in ("mods", "enabled", "active"))
            and (value.get("plugins") is None or isinstance(value["plugins"], list)))


def longest_increasing(values: list[int]) -> set[int]:
    """The positions (indexes into ``values``) of one longest strictly increasing run."""

    tails: list[int] = []          # tails[k]: the smallest value that ends a run of length k + 1
    tail_at: list[int] = []        # ... and where it is
    previous = [-1] * len(values)
    for index, value in enumerate(values):
        place = bisect_left(tails, value)
        if place == len(tails):
            tails.append(value)
            tail_at.append(index)
        else:
            tails[place] = value
            tail_at[place] = index
        previous[index] = tail_at[place - 1] if place else -1
    kept: set[int] = set()
    cursor = tail_at[-1] if tail_at else -1
    while cursor >= 0:
        kept.add(cursor)
        cursor = previous[cursor]
    return kept


def moved(old: list[str], new: list[str]) -> list[dict[str, Any]]:
    """The items whose place changed between two orders, among those in both. Items that only shifted because
    others moved are not counted: the fewest items that explain the new order are returned (a block of twenty dragged
    elsewhere is twenty, not the hundreds it passed). ``before`` and ``after`` are positions, counting from 0 as MO2's
    priority column does."""

    where = {name_key(name): index for index, name in enumerate(old)}
    common = [(name, where[name_key(name)], index) for index, name in enumerate(new) if name_key(name) in where]
    if len(common) < 2:
        return []
    still = longest_increasing([old_index for _name, old_index, _new in common])
    return [{"name": name, "before": old_index, "after": new_index}
            for place, (name, old_index, new_index) in enumerate(common) if place not in still]


def _items(names: Iterable[str], **extra: Any) -> list[dict[str, Any]]:
    return [{"name": name, **extra} for name in names]


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """The entries that turn ``old`` into ``new`` (two snapshots of one profile), each ``{"kind", "count", "items"}``.

    Mods that appear or go are reported as installed or removed (``history_store`` drops them when an install record
    said so already). A plugin that appears or goes is not reported: it came with a mod or left with it. Only plugins
    in both are judged for being switched on or off, and for having moved."""

    entries: list[dict[str, Any]] = []

    def add(kind: str, items: list[dict[str, Any]]) -> None:
        if items:
            entries.append({"kind": kind, "count": len(items), "items": items})

    old_mods = {name_key(name) for name in old["mods"]}
    new_mods = {name_key(name) for name in new["mods"]}
    old_versions, new_versions = old.get("versions"), new.get("versions")
    both = old_mods & new_mods
    appeared = [name for name in new["mods"] if name_key(name) not in both]
    gone = [name for name in old["mods"] if name_key(name) not in both]
    renamed = _renames(gone, appeared, old_versions, new_versions)
    taken = {name_key(old_name) for _new, old_name in renamed} | {name_key(new_name) for new_name, _old in renamed}
    add(MOD_INSTALLED, _items(name for name in appeared if name_key(name) not in taken))
    add(MOD_REMOVED, _items(name for name in gone if name_key(name) not in taken))
    add(MOD_RENAMED, [{"name": new_name, "before": old_name} for new_name, old_name in renamed])
    if old_versions is not None and new_versions is not None:
        before = {name_key(name): value for name, value in old_versions.items()}
        updated = []
        for name, value in new_versions.items():
            earlier = before.get(name_key(name))
            if earlier is not None and name_key(name) in old_mods and list(earlier) != list(value):
                shown = _version_change(earlier, value)
                updated.append({"name": name, "before": shown[0], "after": shown[1]})
        add(MOD_UPDATED, sorted(updated, key=lambda item: name_key(item["name"])))
    was_on, now_on = {name_key(name) for name in old["enabled"]}, {name_key(name) for name in new["enabled"]}
    add(MOD_ENABLED, _items(name for name in new["mods"]
                            if name_key(name) in old_mods and name_key(name) in now_on and name_key(name) not in was_on))
    add(MOD_DISABLED, _items(name for name in new["mods"]
                             if name_key(name) in old_mods and name_key(name) in was_on and name_key(name) not in now_on))
    add(MOD_MOVED, moved(old["mods"], new["mods"]))
    if old.get("plugins") is not None and new.get("plugins") is not None:
        old_plugins = {name_key(name) for name in old["plugins"]}
        was_active, now_active = {name_key(name) for name in old["active"]}, {name_key(name) for name in new["active"]}
        add(PLUGIN_ENABLED, _items(name for name in new["plugins"] if name_key(name) in old_plugins
                                   and name_key(name) in now_active and name_key(name) not in was_active))
        add(PLUGIN_DISABLED, _items(name for name in new["plugins"] if name_key(name) in old_plugins
                                    and name_key(name) in was_active and name_key(name) not in now_active))
        add(PLUGIN_MOVED, moved(old["plugins"], new["plugins"]))
    return entries


def _renames(gone: list[str], appeared: list[str], old_versions: dict | None, new_versions: dict | None) -> list[tuple[str, str]]:
    """(new name, old name) for a mod that vanished while one appeared in the same look and both came from the same
    archive (MO2 reports no renames: a renamed mod looks like a removal and an install). Only when exactly one of each
    has that archive, so two mods from one archive are never guessed at."""

    if not gone or not appeared or old_versions is None or new_versions is None:
        return []

    def by_archive(names: list[str], versions: dict) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for name in names:
            archive = (versions.get(name) or ["", ""])[1]
            if archive:
                found.setdefault(archive.casefold(), []).append(name)
        return found

    before, after = by_archive(gone, old_versions), by_archive(appeared, new_versions)
    return [(after[archive][0], names[0]) for archive, names in before.items()
            if len(names) == 1 and len(after.get(archive, [])) == 1]


def _version_change(old: list[str], new: list[str]) -> tuple[str, str]:
    """What to show for a mod whose version or archive changed: the versions, or the archives when the version is the
    same."""

    if old[0] != new[0]:
        return old[0] or "(no version)", new[0] or "(no version)"
    return old[1] or "(no archive)", new[1] or "(no archive)"


# --- joining entries -------------------------------------------------------------------------------------------------

def merge_items(kind: str, first: list[dict[str, Any]], second: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items of two entries of one kind made a moment apart, as one list. A moved item keeps where it started and
    where it ended up (and is dropped when that is the same place); the others are listed once each."""

    merged: dict[str, dict[str, Any]] = {}
    for item in [*first, *second]:
        key = name_key(item["name"])
        if key not in merged:
            merged[key] = dict(item)
        elif "after" in item:
            merged[key]["after"] = item["after"]
    items = list(merged.values())
    if kind in (MOD_MOVED, PLUGIN_MOVED):
        items = [item for item in items if item.get("before") != item.get("after")]
    return items


# --- words -----------------------------------------------------------------------------------------------------------

def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count:,} {word if count == 1 else many or word + 's'}"


def exit_text(code: int | None) -> str:
    if code is None:
        return ""
    if code == 0:
        return "exited normally"
    shown = f"{code}" if 0 < code < 0x10000 else f"0x{code & 0xFFFFFFFF:08X}"
    return f"exited with code {shown}"


_TIME_FORMAT = "%I:%M %p"


def clock(moment: datetime) -> str:
    return moment.strftime(_TIME_FORMAT).lstrip("0")


def date_text(moment: datetime, *, year: bool = True) -> str:
    return moment.strftime("%b %d, %Y" if year else "%b %d").replace(" 0", " ")


def local(value: str, zone: Callable[[datetime], datetime] | None = None) -> datetime | None:
    """A stored time as a local ``datetime`` (``zone`` converts it, for tests; the default is this PC's zone)."""

    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return zone(moment) if zone is not None else moment.astimezone()


def run_text(entry: dict[str, Any], zone: Callable[[datetime], datetime] | None = None) -> str:
    """"Skyrim Special Edition ran 8:14 PM–9:02 PM, exited normally" (and what it says when the end was not seen)."""

    name = entry["subject"] or "A program"
    start = local(entry["at"], zone)
    stop = local(entry["ended_at"], zone) if entry.get("ended_at") else None
    if start is None:
        return f"{name} ran"
    if stop is None:
        return f"{name} started {clock(start)}, end not seen (MO2 may have closed first)"
    span = f"{clock(start)}–{clock(stop)}" if stop.date() == start.date() else (
        f"{clock(start)}–{date_text(stop, year=stop.year != start.year)}, {clock(stop)}")
    ending = exit_text(entry.get("exit_code"))
    return f"{name} ran {span}" + (f", {ending}" if ending else "")


def title(entry: dict[str, Any], zone: Callable[[datetime], datetime] | None = None) -> str:
    """The one line an entry is listed by."""

    kind, count, items = entry["kind"], entry["count"], entry.get("items") or []
    one = items[0]["name"] if count == 1 and items else ""
    if kind == PROGRAM_RUN:
        return run_text(entry, zone)
    if kind == PROFILE_SWITCHED:
        return f"Switched to profile {entry['subject']}"
    if kind == MOD_RENAMED and count == 1 and items:
        return f"Renamed {items[0].get('before', '')} to {one}"
    words = {
        MOD_INSTALLED: ("Installed {}", "mod installed", "mods installed"),
        MOD_UPDATED: ("Updated {}", "mod updated", "mods updated"),
        MOD_REINSTALLED: ("Reinstalled {}", "mod reinstalled", "mods reinstalled"),
        MOD_REMOVED: ("Removed {}", "mod removed", "mods removed"),
        MOD_RENAMED: ("Renamed {}", "mod renamed", "mods renamed"),
        MOD_ENABLED: ("Switched on {}", "mod switched on", "mods switched on"),
        MOD_DISABLED: ("Switched off {}", "mod switched off", "mods switched off"),
        MOD_MOVED: ("Moved {}", "mod moved", "mods moved"),
        PLUGIN_ENABLED: ("Plugin switched on: {}", "plugin switched on", "plugins switched on"),
        PLUGIN_DISABLED: ("Plugin switched off: {}", "plugin switched off", "plugins switched off"),
        PLUGIN_MOVED: ("Plugin moved: {}", "plugin moved", "plugins moved"),
    }.get(kind)
    if words is None:
        return kind
    if count == 1 and one:
        return words[0].format(one)
    return f"{count:,} {words[1] if count == 1 else words[2]}"


def item_detail(kind: str, item: dict[str, Any]) -> str:
    """What one item changed from and to ("priority 5 → 50", "1.0 → 1.1"), or nothing."""

    if "before" not in item and "after" not in item:
        return ""
    before, after = item.get("before"), item.get("after")
    if kind in (MOD_MOVED, PLUGIN_MOVED):
        return f"{'priority ' if kind == MOD_MOVED else 'position '}{before} → {after}"
    if kind == MOD_RENAMED:
        return f"was {before}"
    if before in (None, "") and after in (None, ""):
        return ""
    return f"{before or '(none)'} → {after or '(none)'}"


# Why a change was found rather than seen: the code stored in an entry's ``offline`` column (0: seen as it happened).
FOUND_START, FOUND_PROFILE, FOUND_RESTART = 1, 2, 3
OFFLINE_NOTE = "found at MO2 start"
FOUND_NOTES = {FOUND_START: OFFLINE_NOTE, FOUND_PROFILE: "found when this profile was opened",
               FOUND_RESTART: "found when Companion was switched on"}
FOUND_EXPLAIN = {
    FOUND_START: "Found when MO2 started: this changed while Companion wasn't watching, so the time is when it was noticed.",
    FOUND_PROFILE: "Found when this profile was opened: it changed while the profile wasn't in use (or Companion wasn't "
                   "watching), so the time is when it was noticed.",
    FOUND_RESTART: "Found when Companion was switched on again: it changed while Companion wasn't watching, so the time "
                   "is when it was noticed.",
}


def detail(entry: dict[str, Any]) -> str:
    """The short text beside an entry's line: what changed for a single item, and that it was found when MO2 started
    (it changed while Companion wasn't watching, so its time is when it was noticed)."""

    items = entry.get("items") or []
    parts = []
    if entry["kind"] != PROGRAM_RUN and entry["count"] == 1 and items:
        parts.append(item_detail(entry["kind"], items[0]))
        version = (entry.get("detail") or {}).get("version")
        if version and entry["kind"] in (MOD_INSTALLED, MOD_REINSTALLED):
            parts.append(f"version {version}")
    if entry.get("offline"):
        parts.append(FOUND_NOTES.get(int(entry.get("found") or FOUND_START), OFFLINE_NOTE))
    return " · ".join(part for part in parts if part)


# --- days and export ---------------------------------------------------------------------------------------------------

def day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return day.strftime("%A, %b %d, %Y").replace(" 0", " ")


def by_day(entries: list[dict[str, Any]], zone: Callable[[datetime], datetime] | None = None) -> list[tuple[date, list[dict[str, Any]]]]:
    """Entries (newest first) grouped by the local day they happened, newest day first."""

    days: list[tuple[date, list[dict[str, Any]]]] = []
    for entry in entries:
        moment = local(entry["at"], zone)
        day = moment.date() if moment is not None else date.min
        if days and days[-1][0] == day:
            days[-1][1].append(entry)
        else:
            days.append((day, [entry]))
    return days


def render_text(entries: list[dict[str, Any]], heading: str = "", *, today: date | None = None,
                zone: Callable[[datetime], datetime] | None = None, markdown: bool = False) -> str:
    """The history as readable text or Markdown: a heading per day, newest first, each entry on a line with its details
    (the items of a group) indented underneath."""

    today = today or date.today()
    lines: list[str] = []
    if heading:
        lines += [f"# {heading}" if markdown else heading, ""]
    if not entries:
        lines.append("Nothing has been recorded for this selection yet.")
    for day, group in by_day(entries, zone):
        label = day_label(day, today) if day != date.min else "Unknown date"
        lines += [f"## {label}" if markdown else label, "" if markdown else "-" * len(label)]
        for entry in group:
            moment = local(entry["at"], zone)
            stamp = clock(moment) if moment is not None else "?"
            text = title(entry, zone)
            note = detail(entry)
            if markdown:
                if entry["kind"] == PROGRAM_RUN:
                    text = f"**{text}**"
                lines.append(f"- {stamp}: {text}" + (f" ({note})" if note else ""))
            else:
                lines.append(f"{stamp:>9}  {text}" + (f"  [{note}]" if note else ""))
            if entry["count"] > 1:
                for item in entry.get("items") or []:
                    more = item_detail(entry["kind"], item)
                    lines.append(("  - " if markdown else "           - ") + item["name"] + (f"  ({more})" if more else ""))
                shown = len(entry.get("items") or [])
                if entry["count"] > shown:
                    lines.append(("  - " if markdown else "           - ") + f"... and {entry['count'] - shown:,} more")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
