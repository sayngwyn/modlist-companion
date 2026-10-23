"""Which installer option put this file here?

MO2 says which mod a file comes from; the events say which option of that mod's installer installs
it. Each option (and each automatic install, and the files the installer always installs) records the
``<file>``/``<folder>`` entries the FOMOD defines for it, with where each lands; this module turns that
into answers. No Qt in here.

Matching, for a path or a plugin name:

* a file entry whose destination is exactly the path (a bare file name matches any file of that name);
* a folder entry whose destination is the path or a folder above it (what is inside the folder is not
  recorded, so this says "installs that folder", not "contains that file"); folders that install into
  the mod folder itself are left out, because they say nothing about one file;
* a plugin listed in an option's ``plugins``, which works for installs logged before files were recorded;
* when the query is a folder, the entries that install somewhere inside it.

Paths are compared case-insensitively with slashes of either kind and without a leading ``Data/``.

Who wins. FOMOD installs the picked options' entries in order of ascending priority, ties in the order they
appear in ModuleConfig.xml, and a later entry overwrites an earlier one. Only entries for the same target path
compete, and only within one install:

* among exact file entries the picked one with the highest (priority, position in the XML) wins, definitely;
* a folder entry might or might not hold the file, so it never outranks an exact file entry: it "may also
  write it". Only when no exact entry exists do folder entries (and plugins listed through a folder that
  installs into the mod folder) compete among themselves, and then the winner is only the likely one.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

# Extensions that make a bare word look like a file name ("Foo.esp"), for the window's search box.
KNOWN_EXTENSIONS = frozenset({
    ".esp", ".esm", ".esl", ".bsa", ".ba2", ".dds", ".nif", ".kf", ".hkx", ".tri", ".bgsm", ".bgem",
    ".png", ".jpg", ".jpeg", ".tga", ".bmp", ".swf", ".wav", ".xwm", ".fuz", ".lip", ".ogg", ".mp3",
    ".pex", ".psc", ".seq", ".dll", ".exe", ".bat", ".ini", ".json", ".toml", ".xml", ".yaml", ".yml",
    ".cfg", ".txt", ".lua", ".jar", ".esx", ".strings", ".dlstrings", ".ilstrings", ".csv", ".pdf",
})

REQUIRED_NAME = "Required by the installer"
# The "option" a mod that was not installed through an installer answers with: it has none of its own.
PLAIN_OPTION = "plain"
# A folder query ("textures") can match a great many entries; look at this many, then say the answer is cut.
_INSIDE_SCAN_LIMIT = 20_000

_LEADING_DATA = re.compile(r"^data(?:/|$)")
_MODS_FOLDER = re.compile(r"^[a-z]:/(?:.*?/)?mods/[^/]+/(.*)$", re.IGNORECASE)
_GAME_DATA = re.compile(r"^[a-z]:/(?:.*?/)?data/(.*)$", re.IGNORECASE)


def _key(value: Any) -> str:
    """The comparison form of a name or path: NFKC, case-folded, forward slashes, no leading ``Data/``."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("\\", "/")
    text = re.sub(r"/{2,}", "/", text).strip().strip("/")
    return _LEADING_DATA.sub("", text)


def plain_path(path: str) -> str:
    """A path as a mod without an installer's file list is asked for it: normalised and with slashes like ``_key``, but
    with its case kept (the stored paths compare without regard to ASCII case themselves)."""

    text = unicodedata.normalize("NFKC", str(path or "")).replace("\\", "/")
    text = re.sub(r"/{2,}", "/", text).strip().strip("/")
    return re.sub(r"^data(?:/|$)", "", text, flags=re.IGNORECASE)


def normalise_query(query: str) -> str:
    """What the user typed or pasted, as a path to look for: quotes and spaces off, forward slashes, and, from a
    full path, everything up to the mod's folder (``C:/.../mods/<Mod>/``) or the game's Data folder
    (``C:/.../Data/``)."""

    text = (query or "").strip().strip("\"'").strip().replace("\\", "/")
    text = re.sub(r"/{2,}", "/", text)
    found = _MODS_FOLDER.match(text) or _GAME_DATA.match(text)
    if found:
        text = found.group(1)
    return text.lstrip("/")


def looks_like_path(query: str) -> bool:
    """Whether a search box entry is a path or a file name rather than a word: it has a slash, or it ends in a
    known file extension."""

    text = (query or "").strip().strip("\"'").strip()
    if not text:
        return False
    if "/" in text or "\\" in text:
        return True
    dot = text.rfind(".")
    return dot > 0 and text[dot:].casefold() in KNOWN_EXTENSIONS


@dataclass(frozen=True)
class _Entry:
    source: str
    destination: str
    resolved: str
    folder: bool
    priority: int
    order: int
    key: str          # comparison form of the resolved destination
    position: int     # index in its option's list

    @property
    def rank(self) -> tuple[int, int, int]:
        return (self.priority, self.order, self.position)


@dataclass
class _Source:
    """One option, one automatic install, or the files the installer always installs, with the file entries and
    plugins recorded for it."""

    event: dict[str, Any]
    step: str
    group: str
    name: str
    option_id: str
    automatic: bool
    visible: bool
    picked: bool
    recorded: bool
    truncated: bool
    plugins: list[str]
    required: bool = False
    entries: list[_Entry] = field(default_factory=list)

    @property
    def label(self) -> str:
        parts = (self.event["mod"]["name"], self.step, self.group, self.name)
        return " › ".join(part for part in parts if part)


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _entries(raw: Any) -> list[_Entry]:
    """The usable entries of a ``files`` list. One that points outside the mod folder is not a path in it, so it
    is left out."""

    entries = []
    for position, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict) or item.get("escapes") is True:
            continue
        resolved = item.get("resolved")
        if not isinstance(resolved, str):
            continue
        entries.append(_Entry(
            source=str(item.get("source", "")), destination=str(item.get("destination", "")),
            resolved=resolved, folder=item.get("kind") == "folder", priority=_int(item.get("priority")),
            order=_int(item.get("order")), key=_key(resolved), position=position))
    return entries


def sources_of(event: dict[str, Any]) -> list[_Source]:
    """Every option, automatic install and required-files list of an event that has a plugin or a recorded file."""

    found = []
    for step in event.get("steps", []):
        visible = step.get("visible") is not False
        for group in step.get("groups", []):
            for option in group.get("options", []):
                if option.get("plugins") or "files" in option:
                    found.append(_Source(
                        event=event, step=step.get("name") or "", group=group.get("name") or "",
                        name=option.get("name") or "Unnamed option", option_id=option.get("id", ""),
                        automatic=False, visible=visible, picked=option.get("selected") is True,
                        recorded="files" in option, truncated=option.get("files_truncated") is True,
                        plugins=[name for name in option.get("plugins", []) if isinstance(name, str)],
                        entries=_entries(option.get("files"))))
    for number, item in enumerate(event.get("automatic_installs", []), 1):
        if item.get("plugins") or "files" in item:
            tail = str(item.get("id", "")).rpartition(":")[2]
            index = int(tail) + 1 if tail.isdigit() else number
            found.append(_Source(
                event=event, step="", group="", name=f"Automatic install {index}",
                option_id=str(item.get("id", "")), automatic=True, visible=True,
                picked=item.get("installed") is True, recorded="files" in item,
                truncated=item.get("files_truncated") is True,
                plugins=[name for name in item.get("plugins", []) if isinstance(name, str)],
                entries=_entries(item.get("files"))))
    required = event.get("required_files")
    if isinstance(required, dict) and "files" in required:
        found.append(_Source(
            event=event, step="", group="", name=REQUIRED_NAME, option_id="required", automatic=True, visible=True,
            picked=True, recorded=True, truncated=required.get("files_truncated") is True, plugins=[],
            required=True, entries=_entries(required.get("files"))))
    return found


def has_file_data(event: dict[str, Any]) -> bool:
    """Whether the install recorded the files its options install (installs from before that did not). An
    installer whose only files are the required ones counts."""

    return any(source.recorded for source in sources_of(event))


class OriginCache:
    """A file index kept between lookups. It is good for as long as ``stamp`` (whatever says the database has not
    changed) stays the same; asking with another stamp starts it afresh."""

    def __init__(self) -> None:
        self.stamp: Any = None
        self.index: FileIndex | None = None
        self.current: dict[str, set[str]] = {}       # instance id ("" for all) -> ids of each mod's newest install
        self.answers: dict[tuple, dict[str, Any]] = {}

    def valid(self, stamp: Any) -> bool:
        return stamp is not None and self.index is not None and self.stamp == stamp

    def reset(self, stamp: Any, index: "FileIndex") -> None:
        self.stamp, self.index = stamp, index
        self.current, self.answers = {}, {}


class FileIndex:
    """The files and plugins of a list of events, ready to be asked about. Build it once and keep it as long as the
    events are the same; build it only when a path is being looked up."""

    def __init__(self, events: Iterable[dict[str, Any]]):
        self._sources: list[_Source] = []
        self._exact: dict[str, list[tuple[_Source, _Entry]]] = {}
        self._names: dict[str, list[tuple[_Source, _Entry]]] = {}
        self._folders: dict[str, list[tuple[_Source, _Entry]]] = {}
        self._entries: list[tuple[_Source, _Entry]] = []
        self._plugins: dict[str, list[_Source]] = {}
        self._events: dict[str, dict[str, Any]] = {}
        self._recorded: set[str] = set()
        self._sorted: tuple[list[str], list[tuple[_Source, _Entry]]] | None = None
        for event in events:
            self._events[event["event_id"]] = event
            for source in sources_of(event):
                self._add(source)

    def _add(self, source: _Source) -> None:
        self._sources.append(source)
        if source.recorded:
            self._recorded.add(source.event["event_id"])
        for plugin in source.plugins:
            self._plugins.setdefault(_key(plugin), []).append(source)
        for entry in source.entries:
            pair = (source, entry)
            self._entries.append(pair)
            if entry.folder:
                if entry.key:  # a folder into the mod folder itself says nothing about one file
                    self._folders.setdefault(entry.key, []).append(pair)
            else:
                self._exact.setdefault(entry.key, []).append(pair)
                self._names.setdefault(entry.key.rpartition("/")[2], []).append(pair)

    def _by_path(self) -> tuple[list[str], list[tuple[_Source, _Entry]]]:
        """Every entry in order of its destination, so the ones inside a folder are one run (built when first needed)."""

        if self._sorted is None:
            ordered = sorted(self._entries, key=lambda pair: pair[1].key)
            self._sorted = ([pair[1].key for pair in ordered], ordered)
        return self._sorted

    # --- asking ---

    def lookup(self, query: str, event_ids: set[str] | None = None) -> dict[str, Any]:
        """The options that install ``query``; ``event_ids`` limits the answer to those installs (the newest of
        each mod, usually). Returns ``{"query", "path", "matches", "unrecorded", "truncated", "notes", "more"}``."""

        path = normalise_query(query)
        wanted = _key(path)
        scoped = {event_id for event_id in self._events if event_ids is None or event_id in event_ids}
        matches: dict[tuple[int, int], dict[str, Any]] = {}
        more = self._collect(wanted, scoped, matches) if wanted else False
        found = self._rank(list(matches.values()), wanted)
        unrecorded = sorted({self._events[i]["mod"]["name"] for i in scoped if i not in self._recorded},
                            key=str.casefold)
        cut = sorted({source.event["mod"]["name"] for source in self._sources
                      if source.truncated and source.event["event_id"] in scoped}, key=str.casefold)
        result = {"query": query, "path": path, "matches": found, "unrecorded": unrecorded, "truncated": cut,
                  "notes": [], "more": more}
        result["notes"] = self._notes(result, scoped)
        return result

    def _collect(self, wanted: str, scoped: set[str], out: dict[tuple[int, int], dict[str, Any]]) -> bool:
        """Gather the matches into ``out``. Returns whether a folder query was cut short."""

        def add(source: _Source, entry: _Entry | None, how: str, path: str) -> None:
            if source.event["event_id"] not in scoped:
                return
            slot = (id(source), entry.position if entry is not None else -1)
            if slot not in out:
                out[slot] = _match(source, entry, how, path)

        for source, entry in self._exact.get(wanted, ()):
            add(source, entry, "file", entry.resolved)
        if "/" not in wanted:
            for source, entry in self._names.get(wanted, ()):
                add(source, entry, "file", entry.resolved)
            for source in self._plugins.get(wanted, ()):
                add(source, None, "plugin", next((p for p in source.plugins if _key(p) == wanted), wanted))
        # Folder entries at or above the path: each folder along it is one lookup.
        parts = wanted.split("/")
        for depth in range(1, len(parts) + 1):
            for source, entry in self._folders.get("/".join(parts[:depth]), ()):
                add(source, entry, "folder", entry.resolved)
        # Entries inside the path, when it is a folder: one run in the sorted list.
        keys, ordered = self._by_path()
        prefix = wanted + "/"
        first = bisect.bisect_left(keys, prefix)
        seen: set[int] = set()
        scanned = 0
        for index in range(first, len(keys)):
            if not keys[index].startswith(prefix):
                break
            scanned += 1
            if scanned > _INSIDE_SCAN_LIMIT:
                return True
            source, entry = ordered[index]
            if id(source) not in seen:   # one entry of each option is enough to name it
                seen.add(id(source))
                add(source, entry, "inside", entry.resolved)
        return False

    def _rank(self, matches: list[dict[str, Any]], wanted: str) -> list[dict[str, Any]]:
        """Drop a plugin match that a file entry of the same option already explains, keep each option's best
        match, then say who wins and what overwrites what, install by install and path by path."""

        explained = {(m["event_id"], m["option_id"]) for m in matches if m["how"] != "plugin"}
        matches = [m for m in matches if m["how"] != "plugin" or (m["event_id"], m["option_id"]) not in explained]
        closeness = {"file": 0, "plugin": 1, "folder": 1, "inside": 2}
        none = (-(10 ** 9),) * 3

        def better(match: dict[str, Any]) -> tuple:
            return (-closeness[match["how"]], match["_rank"] if match["_rank"] is not None else none)

        best: dict[tuple[str, str], dict[str, Any]] = {}
        for match in matches:
            slot = (match["event_id"], match["option_id"] or match["label"])
            if slot not in best or better(match) > better(best[slot]):
                best[slot] = match
        matches = list(best.values())

        # The target of a match is the path it writes: an exact file entry its own destination, a folder or a
        # plugin the path that was asked for. Only matches with one target in one install compete.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for match in matches:
            if match["how"] == "inside":
                continue
            target = _key(match["path"]) if match["how"] == "file" else wanted
            groups.setdefault((match["event_id"], target), []).append(match)
        for group in groups.values():
            exact = [m for m in group if m["how"] == "file"]
            maybe = [m for m in group if m["how"] != "file"]
            contenders = exact if exact else [m for m in maybe if m["_rank"] is not None]
            picked = [m for m in contenders if m["picked"]]
            for match in contenders:
                later = [other for other in contenders if other["_rank"] > match["_rank"]]
                match["later"] = [other["label"] for other in later]
                if match["picked"]:
                    match["overwritten_by"] = [other["label"] for other in later if other["picked"]]
                    match["contested"] = len(picked) > 1
            if picked:
                max(picked, key=lambda m: m["_rank"])["winner"] = True
            if exact:
                writers = [m["label"] for m in maybe if m["picked"]]
                for match in exact:
                    match["may_also"] = writers if match["picked"] else []
                for match in maybe:
                    match["may_also_write"] = True   # a file entry already writes this path for sure
        for match in matches:
            match.pop("_rank", None)
        order = {"file": 0, "plugin": 0, "folder": 1, "inside": 2}
        matches.sort(key=lambda m: (-_stamp(m["completed_at"]), m["mod_name"].casefold(), order[m["how"]],
                                    not m["winner"], not m["picked"], m["label"].casefold()))
        for match in matches:
            match["text"] = describe(match)
            match["detail"] = detail(match)
        return matches

    def _notes(self, result: dict[str, Any], scoped: set[str]) -> list[str]:
        notes = []
        if result["unrecorded"]:
            names = result["unrecorded"]
            notes.append(f"{plural(len(names), 'install')} ({_names(names)}) "
                         + ("was" if len(names) == 1 else "were")
                         + " logged before installer files were recorded, so only plugin names were checked.")
        if result["truncated"]:
            notes.append("The list of files was cut short for " + _names(result["truncated"])
                         + ", so a file may be missing from the answer.")
        if result["more"]:
            notes.append("That folder holds a great many files; only the first of them are listed.")
        matched = {m["event_id"] for m in result["matches"]}
        reinstalled = sorted({event["mod"]["name"] for event_id, event in self._events.items()
                              if event_id in scoped and event.get("source", {}).get("reinstallation") is True
                              and (not matched or event_id in matched)}, key=str.casefold)
        if reinstalled:
            notes.append(f"{_names(reinstalled)} {'was' if len(reinstalled) == 1 else 'were'} reinstalled. If you "
                         "chose Merge, files from the earlier install may not be shown.")
        if not result["matches"] and result["path"]:
            roots = sorted({source.event["mod"]["name"] for source in self._sources
                            if source.event["event_id"] in scoped and source.picked
                            and any(entry.folder and not entry.key for entry in source.entries)},
                           key=str.casefold)
            if roots:
                notes.append("These picked options install a whole folder into the mod folder, so the files inside "
                             "them can't be told apart: " + _names(roots) + ".")
        return notes


PLAIN_WORDS = "not installed through an installer"
# A bare file name can be in every folder of a mod; a record names at most this many of them.
_PLAIN_PATHS_PER_MOD = 3


def plain_matches(rows: list[tuple[str, str, str]], events: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Answers from mods that were not installed through an installer. ``rows`` are (event id, path, how) of the
    files that were asked for (how: "file" for the file itself or a file of that name, "inside" for a file in a
    folder that was asked for); ``events`` maps event ids to the plain records in scope. The answer for each is
    "<mod> (not installed through an installer)", with the path and the mod's version and archive below."""

    per_mod: dict[str, list[tuple[str, str]]] = {}
    for event_id, path, how in rows:
        if event_id not in events:
            continue
        taken = per_mod.setdefault(event_id, [])
        if how == "inside" and any(h == "inside" for _p, h in taken):
            continue            # one file of a folder is enough to name the mod
        if how == "file" and sum(1 for _p, h in taken if h == "file") >= _PLAIN_PATHS_PER_MOD:
            continue
        taken.append((path, how))
    found = []
    for event_id, taken in per_mod.items():
        event = events[event_id]
        name = event["mod"]["name"]
        for path, how in taken:
            found.append(_plain_match(event, name, path, how))
    return found


def plain_path_matches(paths: list[str], query: str) -> set[str]:
    """Which of a plain record's paths a lookup for ``query`` found (the file itself, a file of that name when no
    folder was given, or a file inside the folder asked for): what the window highlights in a mod's file list."""

    wanted = _key(normalise_query(query))
    if not wanted:
        return set()
    found = set()
    for path in paths:
        key = _key(path)
        if key == wanted or key.startswith(wanted + "/") or ("/" not in wanted and key.rpartition("/")[2] == wanted):
            found.add(path)
    return found


def _plain_match(event: dict[str, Any], name: str, path: str, how: str) -> dict[str, Any]:
    facts = " · ".join(part for part in (event["mod"].get("version"), event["source"].get("archive")) if part)
    detail = f"has {path}" if how == "file" else f"has {path} (and other files in that folder)"
    return {
        "event_id": event["event_id"], "instance_id": event["instance"]["id"], "mod_name": name,
        "completed_at": event.get("completed_at", ""), "hidden": bool(event.get("hidden")),
        "step": "", "group": "", "option": "", "option_id": PLAIN_OPTION, "automatic": False, "required": False,
        "label": name, "picked": True, "page_skipped": False, "how": how, "path": path, "source": "",
        "folder": False, "priority": 0, "recorded": True, "truncated": event["plain"].get("files_truncated") is True,
        "winner": False, "contested": False, "later": [], "overwritten_by": [], "may_also": [],
        "may_also_write": False, "plain": True,
        "text": f"{name} ({PLAIN_WORDS})", "detail": detail + (f" · {facts}" if facts else ""),
    }


def _names(names: list[str], limit: int = 4) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _stamp(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _match(source: _Source, entry: _Entry | None, how: str, path: str) -> dict[str, Any]:
    event = source.event
    rank = entry.rank if entry is not None else _plugin_rank(source, path)
    return {
        "event_id": event["event_id"], "instance_id": event["instance"]["id"], "mod_name": event["mod"]["name"],
        "completed_at": event.get("completed_at", ""), "hidden": bool(event.get("hidden")),
        "step": source.step, "group": source.group, "option": source.name, "option_id": source.option_id,
        "automatic": source.automatic, "required": source.required, "label": source.label, "picked": source.picked,
        "page_skipped": not source.visible, "how": how, "path": path,
        "source": entry.source if entry is not None else "", "folder": bool(entry is not None and entry.folder),
        "priority": entry.priority if entry is not None else 0, "recorded": source.recorded,
        "truncated": source.truncated, "winner": False, "contested": False, "later": [], "overwritten_by": [],
        "may_also": [], "may_also_write": False, "_rank": rank,
    }


def _plugin_rank(source: _Source, plugin: str) -> tuple[int, int, int] | None:
    """The rank of the entry that installs a plugin listed for an option: its own file entry, or a folder that
    installs into the mod folder. None when there is nothing to rank by (an install without file data)."""

    key = _key(plugin)
    own = [entry.rank for entry in source.entries if not entry.folder and entry.key == key]
    roots = [entry.rank for entry in source.entries if entry.folder and not entry.key]
    ranks = own or roots
    return max(ranks) if ranks else None


def _inside(match: dict[str, Any], labels: list[str]) -> list[str]:
    """Labels of other options of the same mod, without the mod's name (it is the same one)."""

    lead = match["mod_name"] + " › "
    return [label[len(lead):] if label.startswith(lead) else label for label in labels]


def describe(match: dict[str, Any]) -> str:
    """"Majestic Mountains › Textures › Texture resolution › 2K (picked)"."""

    if match["required"]:
        state = "always installed"
    elif match["automatic"]:
        state = "applied automatically" if match["picked"] else "not applied"
    elif match["picked"]:
        state = "picked"
    else:
        state = "not picked" + (", its page was skipped" if match["page_skipped"] else "")
    return f"{match['label']} ({state})"


def detail(match: dict[str, Any]) -> str:
    """The second line of an answer: what it installs, and who wins when several picked options write it."""

    parts = []
    if match["how"] == "plugin":
        parts.append(f"lists the plugin {match['path']}")
    elif match["how"] == "inside":
        parts.append(f"installs {match['path']}")
    elif match["folder"]:
        parts.append(f"installs the folder {match['path'] or 'into the mod folder'} (from {match['source']})")
    else:
        parts.append(f"installs {match['path']}")
    if match["priority"]:
        parts.append(f"priority {match['priority']}")
    if match["winner"] and match["contested"]:
        parts.append("likely winner: it is written last")
    elif match["overwritten_by"]:
        parts.append("overwritten by " + _names(_inside(match, match["overwritten_by"]), 2))
    elif match["later"] and not match["picked"]:
        parts.append("a later entry also writes it: " + _names(_inside(match, match["later"]), 2))
    if match["may_also"]:
        parts.append("a folder entry may also write it: " + _names(_inside(match, match["may_also"]), 2))
    if match["may_also_write"]:
        parts.append("may also write it (a file entry writes it for sure; this folder's files aren't recorded)")
    if not match["recorded"]:
        parts.append("file data isn't recorded for this install")
    elif match["truncated"]:
        parts.append("this option's file list was cut short")
    return " · ".join(parts)
