"""FOMOD Plus's saved choices for a mod, read through MO2.

FOMOD Plus keeps them in each mod's ``meta.ini`` as the plugin setting ``fomod`` of the plugin named
``FOMOD Plus``: a JSON text with, for every step and group, the options picked (``plugins``) and the ones the
user turned off by hand (``deselected``). MO2 hands that setting out through ``IModInterface.pluginSetting``;
nothing here reads ``meta.ini``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

FOMOD_PLUS_NAME = "FOMOD Plus"
RECORD_KEY = "fomod"


class RecordError(ValueError):
    """The text FOMOD Plus saved is not a record this edition can read."""


def read_record(mod: Any) -> str | None:
    """The JSON text FOMOD Plus saved for ``mod`` (an ``IModInterface``), or None when it saved nothing."""

    try:
        value = mod.pluginSetting(FOMOD_PLUS_NAME, RECORD_KEY, "")
    except TypeError:
        value = mod.pluginSetting(FOMOD_PLUS_NAME, RECORD_KEY)
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if not isinstance(value, str):
        return None          # MO2 gave the default back (or a number): nothing was saved
    value = value.strip()
    return value or None


def parse_record(text: str) -> dict[str, Any]:
    """The record as a dict with a ``steps`` list. Raises ``RecordError`` for anything else."""

    try:
        record = json.loads(text)
    except ValueError as error:
        raise RecordError(f"not valid JSON ({error})") from None
    if not isinstance(record, dict) or not isinstance(record.get("steps"), list):
        raise RecordError("it has no list of steps")
    for step in record["steps"]:
        if not isinstance(step, dict) or not isinstance(step.get("groups", []), list):
            raise RecordError("a step has no list of groups")
        for group in step.get("groups", []):
            if not isinstance(group, dict):
                raise RecordError("a group is not an object")
            for key in ("plugins", "deselected"):
                names = group.get(key, [])
                if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                    raise RecordError(f"a group's {key} is not a list of names")
    return record


def digest(text: str) -> str:
    """Identifies the choices in a record whatever its spacing: the same picks give the same digest."""

    try:
        canonical = json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except ValueError:
        canonical = text
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def option_counts(record: dict[str, Any]) -> tuple[int, int]:
    """(options picked, options turned off by hand) in a parsed record."""

    picked = deselected = 0
    for step in record["steps"]:
        for group in step.get("groups", []):
            picked += len(group.get("plugins", []))
            deselected += len(group.get("deselected", []))
    return picked, deselected
