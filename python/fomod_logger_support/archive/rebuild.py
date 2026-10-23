"""Rebuild a full installation event from a mod's download archive and the options picked.

A native install records every choice together with each option's plugins, masters,
detection rules and file entries. FOMOD Plus (and any mod installed before Companion)
saves only which option names were picked. The archive still holds the FOMOD, so this
module replays the installer's own logic over it and produces the event the native
installer would have written for those picks:

* option, group and step ids, names, group types and the order options are listed in
  are the ones the native installer records;
* step visibility is worked out from the flags the picked options set, and picks in a
  step that turns out to be hidden are dropped;
* ``enabled`` follows the installer's rules for Required / Recommended / NotUsable
  options and for ``SelectAll`` groups;
* ``plugins``, ``masters`` (read from the plugin headers inside the archive), ``detects``,
  ``files``, ``required_files`` and ``automatic_installs`` are filled in the same way,
  including the file-entry caps;
* the event says it was reconstructed (``source.reconstructed``), and lists the
  assumptions that went into it (``source.reconstruction_notes``).

What an archive cannot tell: the load order at install time (file conditions use the
``file_state`` you give, or count every file as Missing), the game and script extender
versions, and anything the user changed after the FOMOD's defaults that the picks do not say.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..core.storage import validate_event
from .fomod_xml import (Composite, FileCondition, FileEntry, FlagCondition, FomodInfo,
                        FomodParseError, Group, ModuleConfig, Option, ParseLimits, Step,
                        VersionCondition, collect_detected_plugins, display_options,
                        parse_info_xml, parse_module_config)
from .plugin_header import HEADER_WINDOW, MAX_HEADER_DATA, needed_bytes, read_plugin_masters
from .sevenzip import Archive, ArchiveError, Limits, open_archive

__all__ = [
    "ArchiveTree",
    "ConditionEnvironment",
    "LoadedFomod",
    "NoFomodError",
    "RebuildResult",
    "Selection",
    "load_archive_fomod",
    "rebuild_event",
    "reconstruct_from_archive",
]

RECONSTRUCTED_INSTALLER = "Modlist Companion (reconstructed from archive)"
UPSTREAM_UNKNOWN = "unknown"

# Caps that mirror the native writer (src/fomodpatchevidence.h).
MAX_FILES_PER_OPTION = 2000
MAX_FILES_PER_EVENT = 20000
MAX_FILE_BYTES_PER_EVENT = 2 * 1024 * 1024
FILE_ENTRY_OVERHEAD_BYTES = 360
MAX_NOTES = 32
MAX_NAMES = 4096                       # plugins / masters / detects per item (the schema's limit)
MAX_EVENT_BYTES = 4 * 1024 * 1024      # the largest event file the installer reads (kMaxEventFileBytes)
RECORD_MARGIN_BYTES = 128 * 1024       # kept clear of that limit
MASTERS_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024   # the native installer's limits for reading masters
MASTERS_MAX_PLUGINS = 400

_EVENT_NAMESPACE = uuid.UUID("5f0b8c43-3c0e-4c43-a3f4-9d9a52b3c4e1")
_PLUGIN_EXTENSIONS = (".esp", ".esm", ".esl")


class NoFomodError(ValueError):
    """The archive has no FOMOD the installer would use (``fomod/ModuleConfig.xml``)."""


# ----------------------------------------------------------------------------- picks


def _casefold(text: str) -> str:
    return text.casefold()


@dataclass
class _PlusGroup:
    name: str
    picked: list[str]
    deselected: list[str]


@dataclass
class _PlusStep:
    name: str
    groups: list[_PlusGroup]


@dataclass
class _Resolved:
    picks: set[tuple[int, int, int]]          # (step, group, option), authored indexes
    explicit_groups: set[tuple[int, int]]     # groups whose picks are known (empty counts)
    warnings: list[str]


class Selection:
    """Which options were picked. Build one with a ``from_*`` constructor."""

    def __init__(self, kind: str, *, plus: list[_PlusStep] | None = None,
                 ids: frozenset[str] = frozenset(), names: frozenset[str] = frozenset()):
        self.kind = kind
        self._plus = plus or []
        self._ids = ids
        self._names = names

    # ---- constructors
    @classmethod
    def from_plus_metadata(cls, payload: str | Mapping[str, Any]) -> "Selection":
        """FOMOD Plus's saved record: ``{"steps": [{"name", "groups": [{"name", "plugins", "deselected"}]}]}``.

        ``plugins`` are the option names picked, ``deselected`` the ones the user turned off.
        Steps and groups are matched by name (the n-th repeat of a name matches the n-th
        step or group of that name), options by name inside their group.
        """

        raw = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
        if not isinstance(raw, Mapping) or not isinstance(raw.get("steps"), list):
            raise ValueError("FOMOD Plus metadata must contain a steps array")
        steps: list[_PlusStep] = []
        for raw_step in raw["steps"]:
            if not isinstance(raw_step, Mapping) or not isinstance(raw_step.get("groups", []), list):
                raise ValueError("each step must contain a groups array")
            groups = []
            for raw_group in raw_step.get("groups", []):
                if not isinstance(raw_group, Mapping):
                    raise ValueError("each group must be an object")
                lists = []
                for key in ("plugins", "deselected"):
                    values = raw_group.get(key, [])
                    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                        raise ValueError(f"{key} must be an array of names")
                    lists.append(list(values))
                groups.append(_PlusGroup(str(raw_group.get("name", "")), lists[0], lists[1]))
            steps.append(_PlusStep(str(raw_step.get("name", "")), groups))
        return cls("plus", plus=steps)

    @classmethod
    def from_option_ids(cls, ids: Iterable[str]) -> "Selection":
        """A full selection by id (``step:0/group:1/option:2``): everything not listed is not picked."""

        return cls("ids", ids=frozenset(ids))

    @classmethod
    def from_names(cls, names: Iterable[str]) -> "Selection":
        """A full selection by option name: every option with a listed name is picked."""

        return cls("names", names=frozenset(_casefold(n) for n in names))

    @classmethod
    def from_event(cls, event: Mapping[str, Any]) -> "Selection":
        """The options an existing event recorded as selected (in visible steps)."""

        ids = []
        for step in event.get("steps", []):
            for group in step.get("groups", []):
                for option in group.get("options", []):
                    if option.get("selected"):
                        ids.append(option["id"])
        return cls.from_option_ids(ids)

    def to_plus_metadata(self, config: ModuleConfig) -> dict[str, Any]:
        """The record FOMOD Plus would save for this selection (used to test the round trip)."""

        resolved = self.resolve(config)
        steps = []
        for step in config.display_steps():
            groups = []
            for group in step.groups:
                picked = [o.name for o in display_options(group)
                          if (step.index, group.index, o.index) in resolved.picks]
                groups.append({"name": group.name, "plugins": picked, "deselected": []})
            steps.append({"name": step.name, "groups": groups})
        return {"steps": steps}

    # ---- matching against a parsed FOMOD
    def resolve(self, config: ModuleConfig) -> _Resolved:
        warnings: list[str] = []
        picks: set[tuple[int, int, int]] = set()
        explicit: set[tuple[int, int]] = set()
        if self.kind == "ids":
            wanted = set(self._ids)
            for step in config.steps:
                for group in step.groups:
                    explicit.add((step.index, group.index))
                    for option in group.options:
                        identity = f"step:{step.index}/group:{group.index}/option:{option.index}"
                        if identity in wanted:
                            picks.add((step.index, group.index, option.index))
                            wanted.discard(identity)
            if wanted:
                warnings.append(f"{len(wanted)} picked option id(s) do not exist in this FOMOD")
        elif self.kind == "names":
            matched: set[str] = set()
            for step in config.steps:
                for group in step.groups:
                    explicit.add((step.index, group.index))
                    for option in group.options:
                        if _casefold(option.name) in self._names:
                            picks.add((step.index, group.index, option.index))
                            matched.add(_casefold(option.name))
            missing = self._names - matched
            if missing:
                warnings.append(f"{len(missing)} picked option name(s) do not exist in this FOMOD")
        else:
            self._resolve_plus(config, picks, explicit, warnings)
        return _Resolved(picks, explicit, warnings)

    def _resolve_plus(self, config: ModuleConfig, picks, explicit, warnings) -> None:
        # n-th occurrence of a name -> n-th item of that name, in authored order.
        steps_by_name: dict[str, list[Step]] = {}
        for step in config.steps:
            steps_by_name.setdefault(step.name, []).append(step)
        seen_steps: dict[str, int] = {}
        for plus_step in self._plus:
            occurrence = seen_steps.get(plus_step.name, 0)
            seen_steps[plus_step.name] = occurrence + 1
            candidates = steps_by_name.get(plus_step.name, [])
            if occurrence >= len(candidates):
                if plus_step.groups:
                    warnings.append(f"saved step {plus_step.name!r} is not in this FOMOD")
                continue
            step = candidates[occurrence]
            groups_by_name: dict[str, list[Group]] = {}
            for group in step.groups:
                groups_by_name.setdefault(group.name, []).append(group)
            seen_groups: dict[str, int] = {}
            for plus_group in plus_step.groups:
                g_occurrence = seen_groups.get(plus_group.name, 0)
                seen_groups[plus_group.name] = g_occurrence + 1
                g_candidates = groups_by_name.get(plus_group.name, [])
                if g_occurrence >= len(g_candidates):
                    warnings.append(f"saved group {plus_group.name!r} (step {plus_step.name!r}) "
                                    "is not in this FOMOD")
                    continue
                group = g_candidates[g_occurrence]
                explicit.add((step.index, group.index))
                taken: set[int] = set()
                for name in plus_group.picked:
                    found = next((o for o in group.options
                                  if o.name == name and o.index not in taken), None)
                    if found is None:
                        if name == "None" and group.type == "SelectAtMostOne" and len(group.options) > 1:
                            continue   # the installer's own "None" button, not an authored option
                        warnings.append(f"saved option {name!r} (group {plus_group.name!r}) "
                                        "is not in this FOMOD")
                        continue
                    taken.add(found.index)
                    picks.add((step.index, group.index, found.index))


# -------------------------------------------------------------------- archive tree


def _clean_relative(path: str) -> str:
    text = path.replace("\\", "/")
    return "/".join(part for part in text.split("/") if part not in ("", "."))


def _key(path: str) -> str:
    return _clean_relative(path).casefold()


class ArchiveTree:
    """The files of an archive as the installer sees them: case-insensitive lookups of files
    and folders, and a way to read plugin headers."""

    def __init__(self, paths: Iterable[str],
                 header_reader: Callable[[list[str], int], Mapping[str, bytes | None]] | None = None,
                 *, max_plugins: int = 600):
        self._files: dict[str, str] = {}           # key -> canonical path
        self._dirs: dict[str, str] = {}            # key -> canonical path
        self._children: dict[str, dict[str, bool]] = {}   # dir key -> {child name: is_dir}
        self._header_reader = header_reader
        self.max_plugins = max_plugins             # plugins whose header is read in one rebuild
        self.read_errors: dict[str, ArchiveError] = {}
        self.truncated_reads: set[str] = set()
        for raw in paths:
            path = _clean_relative(raw)
            if not path:
                continue
            self._files[path.casefold()] = path
            parts = path.split("/")
            for depth in range(len(parts)):
                parent = "/".join(parts[:depth]).casefold()
                child_path = "/".join(parts[:depth + 1])
                is_dir = depth < len(parts) - 1
                self._children.setdefault(parent, {})[parts[depth]] = is_dir
                if is_dir:
                    self._dirs[child_path.casefold()] = child_path

    @classmethod
    def from_archive(cls, archive: Archive, *, max_plugins: int = 600) -> "ArchiveTree":
        """The tree of an open archive; plugin headers are read from the archive on demand."""

        def read_headers(paths: list[str], window: int) -> Mapping[str, bytes | None]:
            result = archive.read(paths, max_entry_bytes=window,
                                  max_total_bytes=max(window * len(paths), window), truncate=True)
            tree.read_errors.update(result.errors)
            tree.truncated_reads.update(result.truncated)
            return {path: result.data.get(path) for path in paths}

        tree = cls((entry.path for entry in archive.entries if not entry.is_dir), read_headers,
                   max_plugins=max_plugins)
        return tree

    def find_file(self, path: str) -> str | None:
        return self._files.get(_key(path))

    def find_dir(self, path: str) -> str | None:
        key = _key(path)
        if key == "":
            return ""
        return self._dirs.get(key)

    def child_files(self, directory: str) -> list[str]:
        """Canonical names of the files directly inside ``directory``."""

        children = self._children.get(_key(directory), {})
        return [name for name, is_dir in children.items() if not is_dir]

    def children(self, directory: str) -> dict[str, bool]:
        return dict(self._children.get(_key(directory), {}))

    def locate_fomod(self) -> tuple[str, str] | None:
        """(folder that holds ``fomod``, canonical path of that fomod folder), as the installer finds it."""

        prefix = ""
        while True:
            children = self.children(prefix)
            for name, is_dir in children.items():
                if is_dir and name.casefold() == "fomod":
                    return prefix, (prefix + "/" + name) if prefix else name
            if len(children) == 1 and next(iter(children.values())):
                name = next(iter(children))
                prefix = (prefix + "/" + name) if prefix else name
                continue
            return None

    def read_masters(self, paths: Sequence[str]) -> dict[str, list[str] | None]:
        """Masters of each plugin (None when unreadable), reading only each plugin's header."""

        if self._header_reader is None:
            return {path: None for path in paths}
        unique = list(dict.fromkeys(paths))
        result: dict[str, list[str] | None] = {path: None for path in unique}
        readable = unique[:self.max_plugins]
        try:
            heads = self._header_reader(readable, HEADER_WINDOW) if readable else {}
        except ArchiveError as error:
            self.read_errors[readable[0]] = error
            return result
        retry: dict[str, int] = {}
        for path in readable:
            head = heads.get(path)
            if head is None:
                continue
            masters = read_plugin_masters(head)
            if masters is not None:
                result[path] = masters
                continue
            need = needed_bytes(head)
            if need is not None and len(head) < need <= MAX_HEADER_DATA + 28:
                retry[path] = need
        if retry:
            try:
                bigger = self._header_reader(list(retry), max(retry.values()))
            except ArchiveError as error:
                self.read_errors[next(iter(retry))] = error
                return result
            for path in retry:
                head = bigger.get(path)
                if head is not None:
                    result[path] = read_plugin_masters(head)
        return result


# -------------------------------------------------------------- the load-time context


@dataclass
class ConditionEnvironment:
    """What the FOMOD's file and version conditions are checked against.

    ``file_state`` gives a plugin's state at install time: a function ``name -> state`` or a
    mapping (names compared case-insensitively). States are ``"Active"``, ``"Inactive"`` or
    ``"Missing"``. With none given, every file counts as Missing. Versions left as ``None``
    are taken to satisfy any version condition.
    """

    file_state: Callable[[str], str] | Mapping[str, str] | None = None
    game_version: str | None = None
    fose_version: str | None = None

    def state_of(self, name: str) -> str:
        source = self.file_state
        if source is None:
            return "Missing"
        if callable(source):
            return str(source(name))
        leaf = name.replace("\\", "/").rsplit("/", 1)[-1]
        folded = {k.casefold(): v for k, v in source.items()}
        return str(folded.get(name.casefold(), folded.get(leaf.casefold(), "Missing")))


def _version_tuple(text: str) -> tuple[int, int, int, int]:
    # std::istringstream >> int, a separator character, repeated four times; failures leave 0.
    parts = [0, 0, 0, 0]
    position = 0
    for index in range(4):
        if index:
            position += 1   # the separator
        match = re.compile(r"\s*([+-]?[0-9]+)").match(text, position)
        if not match:
            break
        parts[index] = int(match.group(1))
        position = match.end()
    return tuple(parts)  # type: ignore[return-value]


def _version_at_least(required: str, actual: str | None, fixed: str | None = None) -> bool:
    available = fixed if fixed is not None else actual
    if available is None:
        return True
    return _version_tuple(required) <= _version_tuple(available)


# ----------------------------------------------------------------- the simulation


def _is_plugin_name(name: str) -> bool:
    return name.lower().endswith(_PLUGIN_EXTENSIONS)


def _data_root_path(path: str) -> str:
    text = path.replace("\\", "/").lstrip("/")
    if text.lower() == "data":
        return ""
    if text.lower().startswith("data/"):
        text = text[5:]
    return text


@dataclass
class _Evidence:
    id: str
    plugins: list[str] = field(default_factory=list)
    plugin_paths: list[str] = field(default_factory=list)
    detects: list[str] = field(default_factory=list)
    masters: list[str] = field(default_factory=list)
    masters_read: bool = False
    installed: bool = False
    files: list[dict[str, Any]] = field(default_factory=list)
    files_truncated: bool = False


def _file_record(entry: FileEntry) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source": entry.source, "destination": entry.destination, "resolved": entry.resolved}
    if entry.escapes:
        record["escapes"] = True
    record["kind"] = entry.kind
    if entry.priority != 0:
        record["priority"] = entry.priority
    record["order"] = entry.order
    return record


def _entry_bytes(record: Mapping[str, Any]) -> int:
    total = FILE_ENTRY_OVERHEAD_BYTES
    for key in ("source", "destination", "resolved"):
        text = record[key]
        total += len(text.encode("utf-8", "surrogatepass"))
        total += text.count("\\") + text.count('"')
    return total


def _trim_to_budget(item: _Evidence, max_count: int, max_bytes: int) -> int:
    """Keep at most ``max_count`` entries of at most ``max_bytes``, the ones that win. Returns bytes kept."""

    sizes = [_entry_bytes(record) for record in item.files]
    total = sum(sizes)
    if len(item.files) <= max_count and total <= max_bytes:
        return total
    ranked = sorted(range(len(item.files)),
                    key=lambda i: (item.files[i].get("priority", 0), item.files[i]["order"]),
                    reverse=True)
    keep = [False] * len(item.files)
    kept = 0
    kept_bytes = 0
    for index in ranked:
        if kept >= max_count:
            break
        if kept_bytes + sizes[index] <= max_bytes:
            keep[index] = True
            kept += 1
            kept_bytes += sizes[index]
    item.files = [record for record, flag in zip(item.files, keep) if flag]
    item.files_truncated = True
    return kept_bytes


def _cap_file_records(evidence: list[_Evidence], max_bytes: int = MAX_FILE_BYTES_PER_EVENT) -> None:
    remaining_count = MAX_FILES_PER_EVENT
    remaining_bytes = max_bytes
    for stage in range(3):
        for item in evidence:
            item_stage = 0 if item.id == "required" else (1 if item.installed else 2)
            if item_stage != stage:
                continue
            allowed = min(MAX_FILES_PER_OPTION, remaining_count)
            used = _trim_to_budget(item, allowed, remaining_bytes)
            remaining_count -= len(item.files)
            remaining_bytes -= used


@dataclass
class _OptionState:
    option: Option
    enabled: bool = True
    checked: bool = False
    effective_type: str = ""


class _Simulation:
    """Replays the installer's page logic over a parsed FOMOD and a set of picks."""

    def __init__(self, config: ModuleConfig, resolved: _Resolved, env: ConditionEnvironment,
                 *, fill_defaults: bool, include_forced: bool):
        self.config = config
        self.env = env
        self.display = config.display_steps()
        self.visible: dict[int, bool] = {}
        self.dropped_picks = 0
        # step index -> group index -> option states in display order
        self.states: dict[int, dict[int, list[_OptionState]]] = {}
        self.effective_group_type: dict[tuple[int, int], str] = {}
        self._position = {step.index: pos for pos, step in enumerate(self.display)}
        self._picks = resolved.picks
        self._explicit = resolved.explicit_groups
        self._fill_defaults = fill_defaults
        self._include_forced = include_forced
        self.warnings: list[str] = []
        self._run()

    # ---- conditions
    def _flag_matches(self, max_position: int, flag: str, value: str) -> bool:
        for position in range(max_position - 1, -1, -1):
            step = self.display[position]
            if not self.visible.get(step.index):
                continue
            for group in step.groups:
                for state in self.states[step.index][group.index]:
                    if not state.checked:
                        continue
                    for name, flag_value in state.option.flags:
                        if name and name == flag:
                            return flag_value == value
        return value == ""

    def evaluate(self, condition: Composite | None, max_position: int) -> bool:
        if condition is None:
            return True
        results = (self._evaluate_one(child, max_position) for child in condition.children)
        if condition.operator == "Or":
            return any(results)
        return all(results)

    def _evaluate_one(self, condition, max_position: int) -> bool:
        if isinstance(condition, Composite):
            return self.evaluate(condition, max_position)
        if isinstance(condition, FlagCondition):
            return self._flag_matches(max_position, condition.flag, condition.value)
        if isinstance(condition, FileCondition):
            return self.env.state_of(condition.file) == condition.state
        if isinstance(condition, VersionCondition):
            if condition.kind == "fomm":
                return _version_at_least(condition.version, None, fixed="0.13.21")
            if condition.kind == "game":
                return _version_at_least(condition.version, self.env.game_version)
            return _version_at_least(condition.version, self.env.fose_version)
        return False

    def type_of(self, option: Option, position: int) -> str:
        for condition, kind in option.type_info.patterns:
            if self.evaluate(condition, position):
                return kind
        return option.type_info.default

    # ---- steps
    def _effective_type(self, group: Group) -> str:
        if len(group.options) == 1:
            if group.type in ("SelectAtLeastOne", "SelectExactlyOne"):
                return "SelectAll"
            if group.type == "SelectAtMostOne":
                return "SelectAny"
        return group.type

    def _run(self) -> None:
        for position, step in enumerate(self.display):
            visible = self.evaluate(step.visible, position)
            self.visible[step.index] = visible
            self.states[step.index] = {}
            for group in step.groups:
                effective = self._effective_type(group)
                self.effective_group_type[(step.index, group.index)] = effective
                self.states[step.index][group.index] = self._group(step, group, effective, position, visible)

    def _group(self, step: Step, group: Group, effective: str, position: int,
               visible: bool) -> list[_OptionState]:
        controls = [_OptionState(option, effective_type=effective) for option in display_options(group)]
        if effective == "SelectAll":
            for state in controls:
                state.enabled = False
                state.checked = True
            return controls
        if not visible:
            # A page that is never shown keeps its widgets as built: enabled, nothing checked.
            return controls

        must_select_one = effective in ("SelectExactlyOne", "SelectAtLeastOne")
        may_select_more = True
        first_optional: _OptionState | None = None
        first_could_be: _OptionState | None = None
        defaults: dict[int, bool] = {}
        forced: set[int] = set()
        for state in controls:
            kind = self.type_of(state.option, position)
            state.enabled = True
            if kind == "Required":
                if effective in ("SelectExactlyOne", "SelectAtMostOne"):
                    for other in controls:
                        other.enabled = False
                else:
                    state.enabled = False
                defaults[state.option.index] = True
                forced.add(state.option.index)
            elif kind == "Recommended":
                if may_select_more or not must_select_one:
                    defaults[state.option.index] = True
            elif kind == "Optional":
                first_optional = first_optional or state
            elif kind == "NotUsable":
                defaults[state.option.index] = False
                state.enabled = False
            elif kind == "CouldBeUsable":
                first_could_be = first_could_be or state
            if defaults.get(state.option.index):
                may_select_more = False
        if may_select_more and must_select_one and controls:
            chosen = first_optional or first_could_be or controls[0]
            defaults[chosen.option.index] = True

        key = (step.index, group.index)
        for state in controls:
            index = state.option.index
            picked = (step.index, group.index, index) in self._picks
            if key in self._explicit or not self._fill_defaults:
                state.checked = picked
            else:
                state.checked = defaults.get(index, False)
            if self._include_forced and index in forced:
                exclusive = effective in ("SelectExactlyOne", "SelectAtMostOne")
                saved_other = any((step.index, group.index, other.option.index) in self._picks
                                  for other in controls if other.option.index != index)
                if exclusive and key in self._explicit and saved_other and not picked:
                    # "Required" can hinge on file states we had to guess; the saved pick wins.
                    self.warnings.append(
                        f"group {group.name!r} (step {step.name!r}): a Required option conflicts with "
                        "the saved pick, so the saved pick was kept")
                else:
                    state.checked = True
        # Picks inside a step that turned out hidden are dropped: nothing in `controls` is checked.
        return controls

    # ---- results
    def finish(self) -> None:
        """Count the picks that fell in hidden steps; they are not part of the event."""

        for step in self.display:
            if self.visible[step.index]:
                continue
            for group in step.groups:
                for state in self.states[step.index][group.index]:
                    if (step.index, group.index, state.option.index) in self._picks:
                        self.dropped_picks += 1

    def is_checked(self, step: Step, group: Group, option: Option) -> bool:
        for state in self.states[step.index][group.index]:
            if state.option is option:
                return state.checked
        return False

    def any_file_condition(self) -> bool:
        def walk(condition) -> bool:
            if condition is None:
                return False
            for child in condition.children:
                if isinstance(child, FileCondition):
                    return True
                if isinstance(child, Composite) and walk(child):
                    return True
            return False

        for step in self.config.steps:
            if walk(step.visible):
                return True
            for group in step.groups:
                for option in group.options:
                    if any(walk(pattern) for pattern, _ in option.type_info.patterns):
                        return True
        return any(walk(install.condition) for install in self.config.conditional_installs)


def _plugin_sources(files: Iterable[FileEntry], tree: ArchiveTree, base: str,
                    item: _Evidence) -> None:
    """``addPluginSources``: plugin names (and their archive paths) an option's entries would install."""

    def add(name: str, path: str) -> None:
        if len(item.plugins) >= MAX_NAMES:
            return
        if name.casefold() not in {existing.casefold() for existing in item.plugins}:
            item.plugins.append(name)
            item.plugin_paths.append(path)

    for entry in files:
        source = (base + "/" + entry.source) if base else entry.source
        if entry.is_folder:
            folder = tree.find_dir(source)
            if folder is None or _data_root_path(entry.destination) != "":
                continue
            for name in tree.child_files(folder):
                if _is_plugin_name(name):
                    add(name, (folder + "/" + name) if folder else name)
            continue
        found = tree.find_file(source)
        if found is None:
            continue
        destination = _data_root_path(entry.destination)
        if destination == "" or destination.endswith("/"):
            destination += found.rsplit("/", 1)[-1]
        if "/" not in destination and _is_plugin_name(destination):
            add(destination, found)


# --------------------------------------------------------------------- the rebuild


@dataclass
class RebuildResult:
    event: dict[str, Any]
    warnings: list[str]
    notes: list[str]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _step_events(sim: _Simulation, evidence: dict[str, _Evidence]) -> list[dict[str, Any]]:
    steps = []
    for step in sim.display:
        visible = sim.visible[step.index]
        groups = []
        for group in step.groups:
            options = []
            for state in sim.states[step.index][group.index]:
                option = state.option
                identity = f"step:{step.index}/group:{group.index}/option:{option.index}"
                item = evidence[identity]
                entry: dict[str, Any] = {
                    "id": identity, "name": option.name,
                    "selected": bool(visible and state.checked),
                    "enabled": state.enabled,
                    "plugins": list(item.plugins),
                }
                if item.masters_read:
                    entry["masters"] = list(item.masters)
                entry["detects"] = list(item.detects)[:MAX_NAMES]
                entry["files"] = list(item.files)
                if item.files_truncated:
                    entry["files_truncated"] = True
                options.append(entry)
            groups.append({"id": f"step:{step.index}/group:{group.index}", "name": group.name,
                           "type": group.type, "options": options})
        steps.append({"id": f"step:{step.index}", "name": step.name, "visible": visible,
                      "groups": groups})
    return steps


def _record_size(event: Mapping[str, Any]) -> int:
    """Bytes of the event as the native writer saves it: Qt's indented JSON (4 spaces, keys sorted)."""

    text = json.dumps(event, ensure_ascii=False, indent=4, sort_keys=True) + "\n"
    return len(text.encode("utf-8", "surrogatepass"))


def _fit_to_record_size(event: dict[str, Any]) -> int:
    """Drop the file lists of skipped options, one at a time, until the event is safely small.

    The installer refuses event files over 4 MiB (``kMaxEventFileBytes``); the aim is that
    size less ``RECORD_MARGIN_BYTES``. Lists go largest first, and an option that is picked
    or an automatic install that applied keeps its list. Each dropped list is marked
    ``files_truncated``. Returns how many were dropped.
    """

    target = MAX_EVENT_BYTES - RECORD_MARGIN_BYTES
    size = _record_size(event)
    if size <= target:
        return 0
    holders = [option for step in event["steps"] for group in step["groups"] for option in group["options"]
               if not option["selected"]]
    holders += [auto for auto in event.get("automatic_installs", []) if not auto["installed"]]
    holders = [item for item in holders if item.get("files")]
    # What a list takes, judged once, so each drop is cheap; the real size is re-measured when it looks done.
    weights = {id(item): len(json.dumps(item["files"], ensure_ascii=False, indent=4, sort_keys=True))
               for item in holders}
    holders.sort(key=lambda item: weights[id(item)], reverse=True)
    dropped = 0
    for item in holders:
        if size <= target:
            break
        item["files"] = []
        item["files_truncated"] = True
        dropped += 1
        size -= max(0, weights[id(item)] - 2)
        if size <= target:
            size = _record_size(event)
    return dropped


def rebuild_event(config: ModuleConfig, selection: Selection, tree: ArchiveTree, *,
                  base: str = "",
                  environment: ConditionEnvironment | None = None,
                  instance_id: str, instance_label: str, mod_name: str, mod_version: str = "",
                  nexus_id: int = 0, archive_name: str = "",
                  started_at: str | None = None, completed_at: str | None = None,
                  installer_version: str = "unknown", event_id: str | None = None,
                  read_masters: str = "skipped", fill_defaults: bool = False,
                  include_forced: bool = True, reinstallation: bool = False,
                  extra_notes: Sequence[str] = (), archive_bytes: int | None = None,
                  masters_max_archive_bytes: int | None = MASTERS_MAX_ARCHIVE_BYTES,
                  masters_max_plugins: int | None = MASTERS_MAX_PLUGINS) -> RebuildResult:
    """The installation event for ``selection`` made over ``config``.

    ``tree`` is the archive's files (and plugin headers); ``base`` is the archive folder that
    holds ``fomod`` (the sources in the FOMOD are relative to it). ``read_masters`` is
    ``"skipped"`` (like the native installer: only options that were not installed),
    ``"all"`` or ``"none"``.

    Like the native installer, masters are not read when the archive is over
    ``masters_max_archive_bytes`` (``archive_bytes``, when the caller knows it) or more than
    ``masters_max_plugins`` plugins would have to be read; pass ``None`` to lift a limit.

    This reads the archive and can take seconds on a big one: run it off MO2's UI thread.
    """

    if read_masters not in ("skipped", "all", "none"):
        raise ValueError("read_masters must be 'skipped', 'all' or 'none'")
    environment = environment or ConditionEnvironment()
    resolved = selection.resolve(config)
    sim = _Simulation(config, resolved, environment, fill_defaults=fill_defaults,
                      include_forced=include_forced)
    sim.finish()

    warnings = list(config.warnings) + resolved.warnings + sim.warnings
    notes: list[str] = list(extra_notes)
    if sim.dropped_picks:
        warnings.append(f"{sim.dropped_picks} pick(s) in hidden steps were dropped")

    # ---- evidence per option, automatic install and the required files
    evidence_list: list[_Evidence] = []
    by_id: dict[str, _Evidence] = {}
    for step in sim.display:
        page_detects = collect_detected_plugins(step.visible)
        for group in step.groups:
            for state in sim.states[step.index][group.index]:
                option = state.option
                item = _Evidence(f"step:{step.index}/group:{group.index}/option:{option.index}")
                item.installed = bool(sim.visible[step.index] and state.checked)
                _plugin_sources(option.files, tree, base, item)
                item.detects = list(page_detects)
                for condition, _ in option.type_info.patterns:
                    collect_detected_plugins(condition, item.detects)
                item.files = [_file_record(entry) for entry in option.files]
                evidence_list.append(item)
                by_id[item.id] = item
    end_position = len(sim.display)
    for install in config.conditional_installs:
        item = _Evidence(f"conditional:{install.index}")
        _plugin_sources(install.files, tree, base, item)
        collect_detected_plugins(install.condition, item.detects)
        item.installed = sim.evaluate(install.condition, end_position)
        item.files = [_file_record(entry) for entry in install.files]
        if item.plugins or item.detects or item.files:
            evidence_list.append(item)
            by_id[item.id] = item
    if config.required_files:
        required = _Evidence("required", installed=True)
        required.files = [_file_record(entry) for entry in config.required_files]
        evidence_list.append(required)
        by_id[required.id] = required

    # ---- masters
    if read_masters != "none":
        wanted_paths = []
        for item in evidence_list:
            if item.id == "required" or not item.plugin_paths:
                continue
            if item.installed and read_masters == "skipped":
                continue
            wanted_paths.extend(item.plugin_paths)
        too_big = (masters_max_archive_bytes is not None and archive_bytes is not None
                   and archive_bytes > masters_max_archive_bytes)
        too_many = (masters_max_plugins is not None
                    and len(set(wanted_paths)) > masters_max_plugins)
        gated = bool(wanted_paths) and (too_big or too_many)
        if gated:
            notes.append("masters were not read: " + (
                f"the archive is over {masters_max_archive_bytes // (1024 * 1024)} MiB" if too_big  # pyright: ignore[reportOptionalOperand]  # too_big needs the limit
                else f"more than {masters_max_plugins} plugins would have to be read"))
            wanted_paths = []
        headers = tree.read_masters(wanted_paths) if wanted_paths else {}
        for item in evidence_list:
            if item.id == "required" or not item.plugin_paths:
                continue
            if item.installed and read_masters == "skipped":
                continue
            if gated:
                continue
            read = [headers.get(path) for path in item.plugin_paths]
            item.masters_read = all(masters is not None for masters in read)
            seen = {name.casefold() for name in item.plugins}
            for masters in read:
                for master in masters or []:
                    if master.casefold() not in seen and len(item.masters) < MAX_NAMES:
                        seen.add(master.casefold())
                        item.masters.append(master)
        unread = sum(1 for path in dict.fromkeys(wanted_paths) if headers.get(path) is None)
        if unread:
            notes.append(f"masters of {unread} plugin(s) could not be read from the archive")
            for path, error in list(getattr(tree, "read_errors", {}).items())[:3]:
                warnings.append(f"{path}: {error}")
    _cap_file_records(evidence_list)

    # ---- assemble
    now = _utc_now()
    started_at = started_at or completed_at or now
    completed_at = completed_at or started_at
    event: dict[str, Any] = {
        "schema_version": 1,
        "event_id": event_id or str(uuid.uuid5(
            _EVENT_NAMESPACE,
            json.dumps([instance_id, archive_name, mod_name, sorted(resolved.picks), sorted(
                resolved.explicit_groups)], sort_keys=True))),
        "started_at": started_at,
        "completed_at": completed_at,
        "instance": {"id": instance_id, "label": instance_label},
        "source": {"archive": archive_name, "reinstallation": reinstallation, "reconstructed": True},
        "mod": {"name": mod_name, "version": mod_version, "nexus_id": nexus_id},
        "installer": {"name": RECONSTRUCTED_INSTALLER, "version": installer_version,
                      "upstream_commit": UPSTREAM_UNKNOWN},
        "steps": _step_events(sim, by_id),
    }
    automatic = []
    for item in evidence_list:
        if not item.id.startswith("conditional:"):
            continue
        entry: dict[str, Any] = {"id": item.id, "installed": item.installed, "plugins": list(item.plugins)}
        if item.masters_read:
            entry["masters"] = list(item.masters)
        entry["detects"] = list(item.detects)[:MAX_NAMES]
        entry["files"] = list(item.files)
        if item.files_truncated:
            entry["files_truncated"] = True
        automatic.append(entry)
    if automatic:
        event["automatic_installs"] = automatic
    required_item = by_id.get("required")
    if required_item is not None:
        required_json: dict[str, Any] = {"files": list(required_item.files)}
        if required_item.files_truncated:
            required_json["files_truncated"] = True
        event["required_files"] = required_json

    if sim.any_file_condition() and environment.file_state is None:
        notes.append("the FOMOD's file conditions were checked as if no plugin was installed "
                     "(the load order at install time is not known)")
    if config.truncated:
        notes.append("the FOMOD is unusually large; some parts were not read")
    if fill_defaults:
        notes.append("groups with no saved picks use the FOMOD's default choices")
    if selection.kind == "plus":
        notes.append("picks come from FOMOD Plus's saved choices")
    shrunk = _fit_to_record_size(event)
    if shrunk:
        notes.append(f"the file lists of {shrunk} skipped option(s) were left out to keep the event under 4 MB")
    if _record_size(event) > MAX_EVENT_BYTES:
        warnings.append("the event is larger than the 4 MB the installer reads, even without file lists")
    event["source"]["reconstruction_notes"] = notes[:MAX_NOTES]

    validate_event(event)
    return RebuildResult(event, warnings, notes)


# ------------------------------------------------------------- straight from an archive


@dataclass
class LoadedFomod:
    config: ModuleConfig
    info: FomodInfo
    tree: ArchiveTree
    base: str                      # archive folder that holds `fomod`
    archive_name: str
    warnings: list[str] = field(default_factory=list)


def load_archive_fomod(archive: Archive, *, parse_limits: ParseLimits | None = None) -> LoadedFomod:
    """Find, read and parse the FOMOD inside an open archive."""

    tree = ArchiveTree.from_archive(archive)
    located = tree.locate_fomod()
    if located is None:
        raise NoFomodError("the archive has no fomod folder the installer would use")
    base, fomod_dir = located
    config_path = next((tree.find_file(f"{fomod_dir}/{name}") for name in ("ModuleConfig.xml",)), None)
    if config_path is None:
        raise NoFomodError("the fomod folder has no ModuleConfig.xml")
    info_path = tree.find_file(f"{fomod_dir}/info.xml")
    result = archive.read([config_path] + ([info_path] if info_path else []))
    if config_path not in result.data:
        error = result.errors.get(config_path)
        raise error if error is not None else NoFomodError("ModuleConfig.xml could not be read")
    try:
        config = parse_module_config(result.data[config_path], parse_limits)
    except FomodParseError as exc:
        raise NoFomodError(f"ModuleConfig.xml cannot be read: {exc}") from exc
    info = parse_info_xml(result.data[info_path], parse_limits) if info_path and info_path in result.data else FomodInfo()
    return LoadedFomod(config, info, tree, base, Path(archive.path).name, list(config.warnings))


def reconstruct_from_archive(archive_path: str | Path, selection: Selection, *,
                             sevenzip_dll: str | Path | None = None,
                             limits: Limits | None = None,
                             parse_limits: ParseLimits | None = None,
                             environment: ConditionEnvironment | None = None,
                             instance_id: str, instance_label: str,
                             mod_name: str | None = None, mod_version: str | None = None,
                             nexus_id: int | None = None,
                             started_at: str | None = None, completed_at: str | None = None,
                             installer_version: str = "unknown", event_id: str | None = None,
                             read_masters: str = "skipped", fill_defaults: bool = False,
                             include_forced: bool = True, reinstallation: bool = False,
                             masters_max_archive_bytes: int | None = MASTERS_MAX_ARCHIVE_BYTES,
                             masters_max_plugins: int | None = MASTERS_MAX_PLUGINS
                             ) -> RebuildResult:
    """Open the archive, read its FOMOD and rebuild the event for ``selection``.

    Raises an ``ArchiveError`` subclass when the archive cannot be read (encrypted, multi-part,
    corrupt, too big to scan) and :class:`NoFomodError` when it has no usable FOMOD.
    Name, version and Nexus id default to the FOMOD's ``info.xml``. The archive size and plugin
    count limits for reading masters are the native installer's (see ``rebuild_event``).

    This reads the archive and can take seconds on a big one: run it off MO2's UI thread.
    """

    with open_archive(archive_path, sevenzip_dll=sevenzip_dll, limits=limits) as archive:
        loaded = load_archive_fomod(archive, parse_limits=parse_limits)
        info = loaded.info
        name = mod_name or info.name or loaded.config.module_name or Path(archive_path).stem
        notes = []
        if archive.skipped_unsafe:
            notes.append(f"{archive.skipped_unsafe} archive entries with unsafe names were ignored")
        if archive.warnings:
            notes.append("the archive reports: " + ", ".join(archive.warnings))
        return rebuild_event(
            loaded.config, selection, loaded.tree, base=loaded.base, environment=environment,
            instance_id=instance_id, instance_label=instance_label, mod_name=name,
            mod_version=mod_version if mod_version is not None else info.version,
            nexus_id=nexus_id if nexus_id is not None else (info.nexus_id if info.nexus_id != -1 else 0),
            archive_name=loaded.archive_name, started_at=started_at, completed_at=completed_at,
            installer_version=installer_version, event_id=event_id, read_masters=read_masters,
            fill_defaults=fill_defaults, include_forced=include_forced,
            reinstallation=reinstallation, extra_notes=notes,
            archive_bytes=Path(archive_path).stat().st_size,
            masters_max_archive_bytes=masters_max_archive_bytes, masters_max_plugins=masters_max_plugins)
