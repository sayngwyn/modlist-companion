from __future__ import annotations

from fomod_logger_support.core.cross_patches import (
    analyze_cross_patches,
    canonical_option_label,
)
from fomod_logger_support.core.storage import EventStore
import pytest


@pytest.mark.parametrize("first_selected,second_selected,states", [
    (True, False, ["selected_here", "selected_elsewhere"]),
    (False, False, ["not_selected_anywhere", "not_selected_anywhere"]),
    (True, True, ["selected_here", "selected_here"]),
])
def test_shared_three_way_states_and_authored_occurrences(clone_event, first_selected, second_selected, states):
    a, b = clone_event(), clone_event()
    a["mod"]["name"], b["mod"]["name"] = "A", "B"
    b["event_id"] = "22222222-2222-4222-8222-222222222222"
    for event, selected in ((a, first_selected), (b, second_selected)):
        for group in event["steps"][0]["groups"]:
            group["options"][0]["selected"] = selected
    b["steps"][0]["groups"][0]["options"][0]["enabled"] = False
    patch = analyze_cross_patches([a, b])[0]
    assert [mod["review_state"] for mod in patch["mods"]] == states
    occurrence = patch["mods"][1]["occurrences"][0]
    assert occurrence["option_name"] == "Shared label"
    assert occurrence["enabled"] is False
    assert occurrence["step_name"] == "Main"
    assert occurrence["selected"] is second_selected


def test_normalization_is_conservative():
    assert canonical_option_label("  Shared\tPatch  ") == "shared patch"
    assert canonical_option_label("Ｓｈａｒｅｄ Patch") == "shared patch"
    assert canonical_option_label("Patch v1.2") != canonical_option_label("Patch v2.0")
    assert canonical_option_label("Patch (SE)") != canonical_option_label("Patch")


def test_cross_patches_are_current_instance_scoped_and_deduplicated(
    tmp_path, clone_event
):
    older_a = clone_event()
    older_a["mod"]["name"] = "Mod A"
    newer_a = clone_event()
    newer_a["event_id"] = "22222222-2222-4222-8222-222222222222"
    newer_a["completed_at"] = "2026-08-26T13:00:00+00:00"
    newer_a["mod"]["name"] = "Mod A"
    for group in newer_a["steps"][0]["groups"]:
        group["options"][0]["name"] = "Shared Patch"

    mod_b = clone_event()
    mod_b["event_id"] = "33333333-3333-4333-8333-333333333333"
    mod_b["mod"]["name"] = "Mod B"
    for group in mod_b["steps"][0]["groups"]:
        group["options"][0]["name"] = " shared   patch "

    other_instance = clone_event()
    other_instance["event_id"] = "44444444-4444-4444-8444-444444444444"
    other_instance["instance"] = {"id": "b" * 64, "label": "Other MO2"}
    other_instance["mod"]["name"] = "Mod C"
    for group in other_instance["steps"][0]["groups"]:
        group["options"][0]["name"] = "Shared Patch"

    with EventStore(tmp_path / "events.sqlite3") as store:
        for event in (older_a, newer_a, mod_b, other_instance):
            store.ingest(event)
        current = store.latest(include_hidden=True)
        patches = store.cross_patches()

    assert len(patches) == 1
    patch = patches[0]
    assert patch["instance_id"] == "a" * 64
    assert [mod["name"] for mod in patch["mods"]] == ["Mod A", "Mod B"]
    assert len(patch["mods"][0]["occurrences"]) == 2
    assert patch["mods"][0]["selected"] is True
    assert patch["selected_anywhere"] is True
    assert len(current) == 3


def test_cross_patch_analysis_requires_two_distinct_mods(clone_event):
    event = clone_event()
    assert analyze_cross_patches([event]) == []


def test_distinct_current_events_are_not_merged_by_casefolded_mod_name(clone_event):
    first = clone_event()
    first["mod"]["name"] = "Straße"
    second = clone_event()
    second["event_id"] = "22222222-2222-4222-8222-222222222222"
    second["mod"]["name"] = "STRASSE"

    patches = analyze_cross_patches([first, second])

    assert len(patches) == 1
    assert {mod["name"] for mod in patches[0]["mods"]} == {"Straße", "STRASSE"}
