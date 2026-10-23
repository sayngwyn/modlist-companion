"""Derived, instance-scoped analysis of option labels shared by current mods."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


def canonical_option_label(label: str) -> str:
    """Normalize presentation-only differences without guessing semantics."""

    normalized = unicodedata.normalize("NFKC", label)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _display_label(label: str) -> str:
    normalized = unicodedata.normalize("NFKC", label)
    return re.sub(r"\s+", " ", normalized).strip()


def analyze_cross_patches(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic shared-option groups without persisting derived data."""

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        instance = event["instance"]
        mod_name = event["mod"]["name"]
        mod_key = event["event_id"]

        for step in event["steps"]:
            for group in step["groups"]:
                for option in group["options"]:
                    canonical = canonical_option_label(option["name"])
                    if not canonical:
                        continue
                    key = (instance["id"], canonical)
                    analysis = groups.setdefault(
                        key,
                        {
                            "option": _display_label(option["name"]),
                            "instance_id": instance["id"],
                            "instance_label": instance["label"],
                            "mods": {},
                        },
                    )
                    mod = analysis["mods"].setdefault(
                        mod_key,
                        {
                            "event_id": event["event_id"],
                            "name": mod_name,
                            "selected": False,
                            "original_texts": [],
                            "occurrences": [],
                        },
                    )
                    if option["name"] not in mod["original_texts"]:
                        mod["original_texts"].append(option["name"])
                    mod["selected"] = mod["selected"] or option["selected"]
                    mod["occurrences"].append(
                        {
                            "step_id": step["id"],
                            "group_id": group["id"],
                            "option_id": option["id"],
                            "option_name": option["name"],
                            "step_name": step["name"],
                            "group_name": group["name"],
                            "selected": option["selected"],
                            "enabled": option["enabled"],
                            "step_visible": step["visible"],
                        }
                    )

    result = []
    for analysis in groups.values():
        if len(analysis["mods"]) < 2:
            continue
        mods = []
        selected_anywhere = any(mod["selected"] for mod in analysis["mods"].values())
        for mod in sorted(
            analysis["mods"].values(),
            key=lambda item: (
                item["name"].casefold(),
                item["name"],
                item["event_id"],
            ),
        ):
            mods.append(
                {
                    "name": mod["name"],
                    "event_id": mod["event_id"],
                    "review_state": ("selected_here" if mod["selected"] else
                                     "selected_elsewhere" if selected_anywhere else
                                     "not_selected_anywhere"),
                    "selected": mod["selected"],
                    "original_text": mod["original_texts"][0],
                    "original_texts": mod["original_texts"],
                    "occurrences": mod["occurrences"],
                }
            )
        result.append(
            {
                "option": analysis["option"],
                "instance_id": analysis["instance_id"],
                "instance_label": analysis["instance_label"],
                "mods": mods,
                "selected_anywhere": any(mod["selected"] for mod in mods),
            }
        )

    return sorted(
        result,
        key=lambda item: (
            -len(item["mods"]),
            item["instance_id"],
            item["option"].casefold(),
        ),
    )
