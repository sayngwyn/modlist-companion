from __future__ import annotations

import json
from pathlib import Path
from urllib.request import urlopen

import pytest

from fomod_logger_support.core.missed_patches import find_missed_patches, game_plugin_names
from fomod_logger_support.viewer import model
from fomod_logger_support.core.storage import EventStore, EventValidationError, validate_event

ROOT = Path(__file__).resolve().parents[2]
INSTANCE = "a" * 64


def grass_patches_event(*, village_option: dict, automatic: list | None = None) -> dict:
    """Grass Patches: a selected main plugin plus one skipped option under test."""

    event = {
        "schema_version": 1,
        "event_id": "22222222-2222-4222-8222-222222222222",
        "started_at": "2026-10-20T12:00:00+00:00",
        "completed_at": "2026-10-20T12:01:00+00:00",
        "instance": {"id": INSTANCE, "label": "MO2 Test"},
        "source": {"archive": "Grass Patches.7z", "reinstallation": False},
        "mod": {"name": "Grass Patches", "version": "1.0", "nexus_id": 1},
        "installer": {
            "name": "FOMOD Logger Native Installer",
            "version": "1.0.0",
            "upstream_commit": "fc263f2d923c704b4853c11ed4f8b8cf3920f30d",
        },
        "steps": [{
            "id": "step:0", "name": "Patches", "visible": True,
            "groups": [{
                "id": "step:0/group:0", "name": "Compatibility", "type": "SelectAny",
                "options": [
                    {"id": "step:0/group:0/option:0", "name": "Core", "selected": True, "enabled": True,
                     "plugins": ["GrassPatches.esp"], "detects": []},
                    {"id": "step:0/group:0/option:1", "selected": False, "enabled": True, **village_option},
                ],
            }],
        }],
    }
    if automatic is not None:
        event["automatic_installs"] = automatic
    return event


def load_order(*active: str, mods: tuple[str, ...] = ()) -> dict:
    base = ["Skyrim.esm", "Update.esm"]
    return {"instance_id": INSTANCE, "active_plugins": [*base, "GrassPatches.esp", *active],
            "base_plugins": base, "mods": ["Grass Patches", *mods]}


VILLAGE_BY_MASTERS = {
    "name": "Village patch", "plugins": ["GP - Village.esp"],
    "masters": ["Skyrim.esm", "GrassPatches.esp", "Village.esp"], "detects": [],
}


def test_skipped_patch_for_an_active_master_is_flagged():
    result = find_missed_patches([grass_patches_event(village_option=VILLAGE_BY_MASTERS)],
                                 load_order("Village.esp"))
    [item] = result["missed"]
    assert item["kind"] == "masters"
    assert item["needs"] == ["Village.esp"]  # base game and the installer's own plugin ignored
    assert item["label"] == "Village patch"
    assert item["option_id"] == "step:0/group:0/option:1"


def test_nothing_flagged_when_the_master_is_not_in_the_list():
    result = find_missed_patches([grass_patches_event(village_option=VILLAGE_BY_MASTERS)], load_order())
    assert result["missed"] == []


def test_patch_already_active_from_elsewhere_is_not_flagged():
    result = find_missed_patches([grass_patches_event(village_option=VILLAGE_BY_MASTERS)],
                                 load_order("Village.esp", "GP - Village.esp"))
    assert result["missed"] == []


def test_fomod_detection_rule_counts_even_without_masters():
    option = {"name": "Village support", "plugins": ["GP - Village.esp"], "detects": ["Village.esp"]}
    [item] = find_missed_patches([grass_patches_event(village_option=option)],
                                 load_order("Village.esp"))["missed"]
    assert item["kind"] == "detects"


def test_automatic_install_that_did_not_apply_is_flagged():
    automatic = [{"id": "conditional:0", "installed": False, "plugins": ["GP - Village.esp"],
                  "masters": ["Village.esp"], "detects": ["Village.esp"]}]
    option = {"name": "Nothing", "plugins": [], "detects": []}
    [item] = find_missed_patches([grass_patches_event(village_option=option, automatic=automatic)],
                                 load_order("Village.esp"))["missed"]
    assert item["automatic"] is True
    assert item["option_id"] == ""
    assert "Village.esp" in item["label"]


def test_name_match_only_for_asset_only_options():
    asset_only = {"name": "Village grass textures", "plugins": [], "detects": []}
    [item] = find_missed_patches([grass_patches_event(village_option=asset_only)],
                                 load_order(mods=("Village",)))["missed"]
    assert (item["kind"], item["needs"]) == ("name", ["Village"])

    # A plugin option whose masters are all base game is not a patch for Village,
    # whatever its label says.
    plugin_option = {"name": "Village look", "plugins": ["GP - Look.esp"],
                     "masters": ["Skyrim.esm"], "detects": []}
    assert find_missed_patches([grass_patches_event(village_option=plugin_option)],
                               load_order(mods=("Village",)))["missed"] == []


def test_generic_words_do_not_name_match():
    asset_only = {"name": "Patch textures", "plugins": [], "detects": []}
    result = find_missed_patches([grass_patches_event(village_option=asset_only)],
                                 load_order(mods=("Patch", "Textures")))
    assert result["missed"] == []


def test_reviewed_options_are_not_repeated():
    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
    reviews = [{"event_id": event["event_id"], "option_id": "step:0/group:0/option:1", "state": "dismissed"}]
    assert find_missed_patches([event], load_order("Village.esp"), reviews)["missed"] == []


def test_events_logged_before_evidence_are_counted(installation_event):
    result = find_missed_patches([installation_event], load_order("Village.esp"))
    assert result == {"missed": [], "events_without_evidence": 1}


def test_other_instances_are_ignored():
    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
    event["instance"]["id"] = "b" * 64
    assert find_missed_patches([event], load_order("Village.esp"))["missed"] == []


def test_storage_accepts_patch_evidence_and_rejects_bad_values():
    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS, automatic=[
        {"id": "conditional:0", "installed": True, "plugins": [], "detects": ["Village.esp"]}])
    validate_event(event)

    bad = grass_patches_event(village_option={**VILLAGE_BY_MASTERS, "masters": "Village.esp"})
    with pytest.raises(EventValidationError, match="masters"):
        validate_event(bad)
    # Fields a newer native build adds are accepted, not rejected.
    validate_event(grass_patches_event(village_option={**VILLAGE_BY_MASTERS, "unknown": []}))


def test_schema_documents_patch_evidence():
    schema = json.loads((ROOT / "schemas" / "installation-event.schema.json").read_text(encoding="utf-8"))
    option = schema["$defs"]["option"]["properties"]
    assert {"plugins", "masters", "detects"} <= set(option)
    assert "automatic_installs" in schema["properties"]


def test_window_model_reports_missed_patches_from_the_load_order_snapshot(tmp_path):
    database = tmp_path / "events.sqlite3"
    with EventStore(database) as store:
        store.ingest(grass_patches_event(village_option=VILLAGE_BY_MASTERS))

    assert model.load(database, model.ViewState()).missed_available is False
    snapshot = model.load(database, model.ViewState(), lambda: load_order("Village.esp"))
    assert snapshot.missed_available is True
    assert [item["needs"] for item in snapshot.missed] == [["Village.esp"]]


def test_dlc_masters_from_the_game_plugin_lists_are_not_evidence():
    # MO2 reports Skyrim SE's DLC with an origin other than "data"; the game
    # plugin's own lists must still mark them as part of the game.
    names = ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "GrassPatches.esp"]
    origins = {"Skyrim.esm": "data", "Update.esm": "data", "Dawnguard.esm": "<unmanaged>",
               "GrassPatches.esp": "Grass Patches"}
    base = game_plugin_names(names, origins, [["Skyrim.esm", "Update.esm", "Dawnguard.esm"], []])
    assert base == ["Skyrim.esm", "Update.esm", "Dawnguard.esm"]

    beleval = {"name": "Beleval", "plugins": ["BW Beleval.esp"],
               "masters": ["Skyrim.esm", "Update.esm", "Dawnguard.esm"], "detects": []}
    order = {"instance_id": INSTANCE, "active_plugins": names, "base_plugins": base,
             "mods": ["Grass Patches"]}
    assert find_missed_patches([grass_patches_event(village_option=beleval)], order)["missed"] == []


def test_a_settled_reminder_carried_to_a_renumbered_reinstall_still_hides_its_patch():
    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
    review = {"event_id": "older-event", "option_id": "step:0/group:0/option:7", "state": "resolved",
              "latest_event": {"event_id": event["event_id"]},
              "latest_option_id": "step:0/group:0/option:1"}
    assert find_missed_patches([event], load_order("Village.esp"), [review])["missed"] == []


def test_an_open_reminder_keeps_its_patch_listed_and_marked():
    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
    review = {"event_id": event["event_id"], "option_id": "step:0/group:0/option:1", "state": "pending"}
    [item] = find_missed_patches([event], load_order("Village.esp"), [review])["missed"]
    assert item["reminder"] is True
    [unreminded] = find_missed_patches([event], load_order("Village.esp"))["missed"]
    assert unreminded["reminder"] is False


def test_plugin_option_whose_masters_were_not_read_falls_back_to_its_name():
    # The installer skips reading masters for very large archives.
    unread = {"name": "Village patch", "plugins": ["GP - Village.esp"], "detects": []}
    [item] = find_missed_patches([grass_patches_event(village_option=unread)],
                                 load_order("Village.esp"))["missed"]
    assert item["kind"] == "name" and item["needs"] == ["Village.esp"]


def test_a_patch_for_a_switched_off_plugin_is_present_but_disabled():
    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
    order = load_order()
    order["unchecked_plugins"] = ["Village.esp"]
    [item] = find_missed_patches([event], order)["missed"]
    assert item["state"] == "needs-disabled"
    assert item["disabled"] == [{"plugin": "Village.esp", "where": "unchecked in the plugin list"}]

    order = load_order()
    order["disabled_mod_plugins"] = {"Village.esp": "Village"}
    [item] = find_missed_patches([event], order)["missed"]
    assert item["disabled"] == [{"plugin": "Village.esp", "where": "its mod “Village” is disabled"}]


def test_an_installed_but_switched_off_patch_is_reported_as_such():
    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
    order = load_order("Village.esp")
    order["unchecked_plugins"] = ["GP - Village.esp"]
    [item] = find_missed_patches([event], order)["missed"]
    assert item["state"] == "patch-disabled"
    assert item["disabled"] == [{"plugin": "GP - Village.esp", "where": "unchecked in the plugin list"}]


def test_missing_patches_come_before_switched_off_ones_and_absent_plugins_stay_quiet():
    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
    assert find_missed_patches([event], load_order())["missed"] == []  # Village not installed at all
    [item] = find_missed_patches([event], load_order("Village.esp"))["missed"]
    assert item["state"] == "missing" and item["disabled"] == []


def test_a_picked_option_whose_plugin_is_unchecked_is_flagged():
    picked = dict(VILLAGE_BY_MASTERS, selected=True)
    event = grass_patches_event(village_option=picked)
    event["steps"][0]["groups"][0]["options"][1]["selected"] = True
    order = load_order("Village.esp")
    order["unchecked_plugins"] = ["GP - Village.esp"]
    [item] = find_missed_patches([event], order)["missed"]
    assert item["state"] == "picked-disabled" and item["label"] == "Village patch"
    assert item["disabled"] == [{"plugin": "GP - Village.esp", "where": "unchecked in the plugin list", "missing": []}]
    order["unchecked_plugins"] = []
    order["active_plugins"].append("GP - Village.esp")
    assert find_missed_patches([event], order)["missed"] == []  # switched on: nothing to say


def test_a_picked_plugin_that_is_off_because_a_master_is_missing_says_so():
    picked = dict(VILLAGE_BY_MASTERS, name="Keld Nar", plugins=["Lux Via - Keld Nar patch.esp"], selected=True)
    event = grass_patches_event(village_option=picked)
    order = load_order("Village.esp")
    order["unchecked_plugins"] = ["Lux Via - Keld Nar patch.esp"]
    order["plugin_masters"] = {"Lux Via - Keld Nar patch.esp": ["Skyrim.esm", "Lux Via.esp", "Keld-Nar.esp"]}
    order["disabled_mod_plugins"] = {"Lux Via.esp": "Lux Via"}  # installed, only switched off: not missing
    [item] = find_missed_patches([event], order)["missed"]
    assert item["state"] == "missing-masters"
    assert item["disabled"][0]["missing"] == ["Keld-Nar.esp"]


def test_a_picked_plugin_switched_on_with_a_missing_master_is_flagged():
    # MO2 ticks a newly installed plugin itself; the game won't start like this.
    picked = dict(VILLAGE_BY_MASTERS, name="Keld Nar", plugins=["Lux Via - Keld Nar patch.esp"], selected=True)
    event = grass_patches_event(village_option=picked)
    order = load_order("Village.esp", "Lux Via - Keld Nar patch.esp")
    order["plugin_masters"] = {"Lux Via - Keld Nar patch.esp": ["Skyrim.esm", "Keld-Nar.esp"]}
    [item] = find_missed_patches([event], order)["missed"]
    assert item["state"] == "missing-masters"
    assert item["disabled"] == [{"plugin": "Lux Via - Keld Nar patch.esp", "where": "switched on", "on": True,
                                 "missing": ["Keld-Nar.esp"]}]
    order["plugin_masters"]["Lux Via - Keld Nar patch.esp"] = ["Skyrim.esm"]
    assert find_missed_patches([event], order)["missed"] == []  # masters all there: nothing to say
