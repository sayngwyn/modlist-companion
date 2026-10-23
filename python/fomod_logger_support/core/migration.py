"""Migration from the released FOMOD Logger JSON structure."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .identity import stable_instance_id


LEGACY_NAMESPACE = uuid.UUID("17678f72-d826-4f83-b3ba-50ea0bad0ace")


def _timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "1970-01-01T00:00:00+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return "1970-01-01T00:00:00+00:00"
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat(timespec="seconds")


def legacy_entry_to_event(
    entry: dict[str, Any], *, instance_id: str, instance_label: str
) -> dict[str, Any]:
    canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    event_id = str(uuid.uuid5(LEGACY_NAMESPACE, f"{instance_id}:{canonical}"))
    completed_at = _timestamp(entry.get("timestamp"))

    steps_by_name: dict[str, dict[str, Any]] = {}
    for option_index, option in enumerate(entry.get("options", [])):
        # Reject malformed legacy choices instead of interpreting their truthiness.
        if not isinstance(option.get("selected", False), bool):
            raise ValueError(
                f"legacy options[{option_index}].selected must be a boolean"
            )
        step_name = str(option.get("step", ""))
        group_name = str(option.get("group", ""))
        step = steps_by_name.setdefault(
            step_name, {"name": step_name, "groups": {}}
        )
        group = step["groups"].setdefault(group_name, [])
        group.append(option)

    steps = []
    for step_index, step_data in enumerate(steps_by_name.values()):
        groups = []
        for group_index, (group_name, raw_options) in enumerate(
            step_data["groups"].items()
        ):
            options = []
            for option_index, raw_option in enumerate(raw_options):
                options.append(
                    {
                        "id": f"step:{step_index}/group:{group_index}/option:{option_index}",
                        "name": str(raw_option.get("option", "")),
                        "selected": raw_option.get("selected", False),
                        "enabled": True,
                    }
                )
            groups.append(
                {
                    "id": f"step:{step_index}/group:{group_index}",
                    "name": group_name,
                    "type": "SelectAny",
                    "options": options,
                }
            )
        steps.append(
            {
                "id": f"step:{step_index}",
                "name": step_data["name"],
                "visible": True,
                "groups": groups,
            }
        )

    return {
        "schema_version": 1,
        "event_id": event_id,
        "started_at": completed_at,
        "completed_at": completed_at,
        "instance": {"id": instance_id, "label": instance_label},
        "source": {"archive": "", "reinstallation": False},
        "mod": {
            "name": str(entry.get("mod_name", "Unknown FOMOD")),
            "version": str(entry.get("version", "")),
            "nexus_id": -1,
        },
        "installer": {
            "name": "FOMOD Logger legacy import",
            "version": "1.x",
            "upstream_commit": "unknown",
        },
        "steps": steps,
    }


def load_legacy_file(
    path: Path, *, instance_path: Path, instance_label: str
) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        if "mods" not in raw:
            raise ValueError("legacy JSON object must contain a mods array")
        entries = raw["mods"]
    else:
        entries = raw
    if not isinstance(entries, list):
        raise ValueError("legacy JSON must contain an array of mods")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"legacy mods[{index}] must be an object")
        options = entry.get("options", [])
        if not isinstance(options, list):
            raise ValueError(f"legacy mods[{index}].options must be an array")
        for option_index, option in enumerate(options):
            if not isinstance(option, dict):
                raise ValueError(
                    f"legacy mods[{index}].options[{option_index}] must be an object"
                )
    instance_id = stable_instance_id(instance_path)
    return [
        legacy_entry_to_event(
            entry, instance_id=instance_id, instance_label=instance_label
        )
        for entry in entries
    ]
