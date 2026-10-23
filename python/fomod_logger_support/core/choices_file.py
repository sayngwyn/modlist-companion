"""The choices file: every FOMOD choice in a list, in a form a program can read.

``export_choices`` writes it and ``import_choices`` reads it. This module holds what both agree on: the
format id and version, and the check that decides whether a file is one of ours. The public description is
``schemas/choices-file.schema.json``; this check is the one the plugin runs (it cannot rely on a JSON Schema
library inside MO2), and a test keeps the two in step.

Shape (names only, never ids, because ids are positions and a mod update can move them):

    {"format": "fomod-companion-choices", "format_version": 1, "exported_at": "...",
     "source": {"game": "...", "nexus_domain": "...", "instance_label": "..."},
     "mods": [{"name": "...", "nexus_id": 0, "version": "...", "archive": "file.7z", "installed_at": "...",
               "steps": [{"name": "...", "skipped": false,
                          "groups": [{"name": "...", "type": "SelectAny",
                                      "options": [{"name": "...", "picked": true, "enabled": true}]}]}]}]}

Unknown extra fields are ignored (a newer build may add some); a newer ``format_version`` is refused.
"""

from __future__ import annotations

import json
from typing import Any

FORMAT_ID = "fomod-companion-choices"
FORMAT_VERSION = 1

GROUP_TYPES = ("SelectAtLeastOne", "SelectAtMostOne", "SelectExactlyOne", "SelectAny", "SelectAll")

# A file is read whole and written to disk piece by piece, so a hostile or broken one is bounded.
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_MODS = 5000
MAX_STEPS = 500
MAX_GROUPS = 1000        # per mod
MAX_OPTIONS = 20000      # per mod
MAX_TOTAL_OPTIONS = 200_000   # in the whole file; a real list has a few thousand. Past this, reading it would
                              # take seconds and hundreds of MB on MO2's UI thread
MAX_TEXT = 1000          # characters in any name


class ChoicesFileError(ValueError):
    """The file is not a usable choices file. The message says why, in words for the person importing it."""


def _fail(where: str, problem: str) -> ChoicesFileError:
    return ChoicesFileError(f"{where} {problem}")


def _text(value: Any, where: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(where, "must be text")
    if required and not value.strip():
        raise _fail(where, "must not be empty")
    if len(value) > MAX_TEXT:
        raise _fail(where, f"is longer than {MAX_TEXT} characters")
    return value


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(where, "must be true or false")
    return value


def _list(value: Any, where: str, limit: int) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(where, "must be a list")
    if len(value) > limit:
        raise _fail(where, f"has more than {limit} entries")
    return value


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(where, "must be an object")
    return value


def parse_choices_file(raw: bytes | str) -> dict[str, Any]:
    """The checked file as a dict. ``raw`` is the file's contents; anything wrong raises ``ChoicesFileError``."""

    if isinstance(raw, bytes):
        if len(raw) > MAX_FILE_BYTES:
            raise ChoicesFileError(f"the file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ChoicesFileError("the file is not UTF-8 text") from error
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise ChoicesFileError(f"the file is not valid JSON ({error})") from error
    return validate_choices_file(data)


def validate_choices_file(data: Any) -> dict[str, Any]:
    """Check a loaded choices file and return it as a clean copy (only the fields this build uses, with the
    optional ones filled in). Raises ``ChoicesFileError`` for anything that is not a usable file."""

    root = _object(data, "the file")
    if root.get("format") != FORMAT_ID:
        raise ChoicesFileError("this is not a Modlist Companion choices file (its format is not "
                               f"\"{FORMAT_ID}\")")
    version = root.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ChoicesFileError("the file has no format_version")
    if version > FORMAT_VERSION:
        raise ChoicesFileError(f"the file is format version {version}, made by a newer Modlist Companion; "
                               f"this one reads version {FORMAT_VERSION}. Update Modlist Companion first.")
    if version < 1:
        raise ChoicesFileError(f"format_version {version} is not valid")
    exported_at = _text(root.get("exported_at", ""), "exported_at")
    source = _object(root.get("source", {}), "source")
    clean_source = {key: _text(source.get(key, ""), f"source.{key}")
                    for key in ("game", "nexus_domain", "instance_label")}

    mods = []
    total = [0]
    for index, raw_mod in enumerate(_list(root.get("mods"), "mods", MAX_MODS)):
        mods.append(_mod(raw_mod, f"mods[{index}]", total))
    return {"format": FORMAT_ID, "format_version": version, "exported_at": exported_at,
            "source": clean_source, "mods": mods}


def _mod(raw: Any, where: str, total: list[int]) -> dict[str, Any]:
    mod = _object(raw, where)
    name = _text(mod.get("name"), f"{where}.name", required=True)
    nexus_id = mod.get("nexus_id", 0)
    if isinstance(nexus_id, bool) or not isinstance(nexus_id, int) or not 0 <= nexus_id < 2**63:
        raise _fail(f"{where}.nexus_id", "must be a whole number, 0 when the Nexus ID is not known")
    steps, groups, options = [], 0, 0
    for s, raw_step in enumerate(_list(mod.get("steps"), f"{where}.steps", MAX_STEPS)):
        step_where = f"{where}.steps[{s}]"
        step = _object(raw_step, step_where)
        step_groups = []
        for g, raw_group in enumerate(_list(step.get("groups"), f"{step_where}.groups", MAX_GROUPS)):
            group_where = f"{step_where}.groups[{g}]"
            group = _object(raw_group, group_where)
            kind = group.get("type", "")
            if kind != "" and kind not in GROUP_TYPES:
                raise _fail(f"{group_where}.type", f"must be one of {', '.join(GROUP_TYPES)}")
            group_options = []
            raw_options = _list(group.get("options"), f"{group_where}.options", MAX_OPTIONS)
            total[0] += len(raw_options)
            if total[0] > MAX_TOTAL_OPTIONS:    # counted before any option is read, so a huge file stops early
                raise ChoicesFileError(f"the file has more than {MAX_TOTAL_OPTIONS:,} options in all, far more "
                                       "than any real list; it is not a choices file this build will read")
            for o, raw_option in enumerate(raw_options):
                option_where = f"{group_where}.options[{o}]"
                option = _object(raw_option, option_where)
                group_options.append({
                    "name": _text(option.get("name"), f"{option_where}.name"),
                    "picked": _flag(option.get("picked"), f"{option_where}.picked"),
                    "enabled": _flag(option.get("enabled", True), f"{option_where}.enabled"),
                })
            options += len(group_options)
            step_groups.append({"name": _text(group.get("name"), f"{group_where}.name"), "type": kind,
                                "options": group_options})
        groups += len(step_groups)
        steps.append({"name": _text(step.get("name"), f"{step_where}.name"),
                      "skipped": _flag(step.get("skipped", False), f"{step_where}.skipped"),
                      "groups": step_groups})
    if groups > MAX_GROUPS or options > MAX_OPTIONS:
        raise _fail(where, f"has more than {MAX_GROUPS} groups or {MAX_OPTIONS} options")
    return {"name": name, "nexus_id": nexus_id,
            "version": _text(mod.get("version", ""), f"{where}.version"),
            "archive": _text(mod.get("archive", ""), f"{where}.archive"),
            "installed_at": _text(mod.get("installed_at", ""), f"{where}.installed_at"),
            "steps": steps}
