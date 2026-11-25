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


def choose_one_event(group_type: str, picked: str | None) -> dict:
    """WACCF-style either/or: two patches for the same mod in one group, ``picked`` of them selected."""

    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
    plain = {"id": "step:0/group:1/option:0", "name": "Village patch", "enabled": True,
             "plugins": ["GP - Village.esp"], "masters": ["Skyrim.esm", "GrassPatches.esp", "Village.esp"], "detects": []}
    plus = {"id": "step:0/group:1/option:1", "name": "Village + Extra patch", "enabled": True,
            "plugins": ["GP - Village Extra.esp"], "masters": ["Skyrim.esm", "Village.esp", "Extra.esp"], "detects": []}
    for option in (plain, plus):
        option["selected"] = option["name"] == picked
    event["steps"][0]["groups"].append({"id": "step:0/group:1", "name": "Either or", "type": group_type,
                                        "options": [plain, plus]})
    return event


@pytest.mark.parametrize("group_type", ["SelectExactlyOne", "SelectAtMostOne"])
def test_the_other_choice_in_a_choose_one_group_is_not_a_missed_patch(group_type):
    event = choose_one_event(group_type, "Village patch")
    labels = [item["label"] for item in find_missed_patches(
        [event], load_order("Village.esp", "Extra.esp"))["missed"]
        if item["option_id"].startswith("step:0/group:1")]
    assert labels == []


@pytest.mark.parametrize("none_label", ["None", "No patch", "Skip patches"])
def test_picking_a_none_choice_in_a_choose_one_group_still_reports_its_patches(none_label):
    event = choose_one_event("SelectExactlyOne", None)
    event["steps"][0]["groups"][-1]["options"].insert(0, {
        "id": "step:0/group:1/option:9", "name": none_label, "enabled": True, "selected": True,
        "plugins": [], "masters": [], "detects": []})
    labels = [i["label"] for i in find_missed_patches([event], load_order("Village.esp", "Extra.esp"))["missed"]
              if i["option_id"].startswith("step:0/group:1")]
    assert sorted(labels) == ["Village + Extra patch", "Village patch"]


def test_a_choose_one_group_with_no_pick_still_reports_and_pick_any_groups_keep_reporting():
    none_picked = choose_one_event("SelectAtMostOne", None)
    assert [i["label"] for i in find_missed_patches([none_picked], load_order("Village.esp"))["missed"]
            if i["option_id"].startswith("step:0/group:1")] == ["Village patch"]
    pick_any = choose_one_event("SelectAny", "Village patch")
    assert [i["label"] for i in find_missed_patches([pick_any], load_order("Village.esp", "Extra.esp"))["missed"]
            if i["option_id"].startswith("step:0/group:1")] == ["Village + Extra patch"]


def test_an_exclude_option_is_not_guessed_to_be_a_patch_for_the_mod_it_names():
    for label in ("Exclude Village", "Skip Village grass", "No Village", "Village: none"):
        option = {"name": label, "plugins": [], "detects": []}
        assert find_missed_patches([grass_patches_event(village_option=option)],
                                   load_order(mods=("Village",)))["missed"] == [], label


def test_a_name_guess_is_not_made_in_a_group_that_leaves_mods_out():
    """Real Risa's All In One Menu 7.1: the options are plain mod names; the group says they are excluded."""

    event = grass_patches_event(village_option={"name": "Village", "plugins": [], "detects": []})
    event["steps"][0]["groups"][0]["name"] = "Exclude: Menus & Frameworks (check to leave unmanaged)"
    assert find_missed_patches([event], load_order(mods=("Village",)))["missed"] == []
    event["steps"][0]["groups"][0]["name"] = "Select the Mods You Use"
    assert [i["label"] for i in find_missed_patches([event], load_order(mods=("Village",)))["missed"]] == ["Village"]


def test_a_name_guess_is_not_made_for_an_option_the_installer_did_not_offer():
    option = {"name": "Village grass textures", "plugins": [], "detects": []}
    event = grass_patches_event(village_option=option)
    event["steps"][0]["groups"][0]["options"][1]["enabled"] = False
    assert find_missed_patches([event], load_order(mods=("Village",)))["missed"] == []
    # The installer's own file condition on a plugin you have still counts when the option wasn't offered:
    # a mod added later would unhide it.
    by_detects = {"name": "Village patch", "plugins": ["GP - Village.esp"], "masters": ["Skyrim.esm"],
                  "detects": ["Village.esp"]}
    event = grass_patches_event(village_option=by_detects)
    event["steps"][0]["groups"][0]["options"][1]["enabled"] = False
    [item] = find_missed_patches([event], load_order("Village.esp"))["missed"]
    assert item["available"] is False


def test_an_option_the_installer_did_not_offer_is_not_flagged_by_its_plugins_masters_alone():
    """WACCF's Bashed Patch option: made for a mod you use, but hidden by the installer for its own reasons."""

    for hidden in ("option", "step"):
        event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
        if hidden == "option":
            event["steps"][0]["groups"][0]["options"][1]["enabled"] = False
        else:
            event["steps"][0]["visible"] = False
        assert find_missed_patches([event], load_order("Village.esp"))["missed"] == [], hidden
    # The same option, offered and skipped, is still reported.
    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
    assert [i["label"] for i in find_missed_patches([event], load_order("Village.esp"))["missed"]] == ["Village patch"]


def bashed_patch_event(main_builds_masters: list[list[str]]) -> dict:
    """WACCF's shape: its own main plugin (one or two builds of it) needs USSEP, and the Bashed Patch extra needs the
    main plugin and USSEP."""

    options = [{"id": f"step:0/group:0/option:{n}", "name": f"Main {n}", "selected": n == 0, "enabled": True,
                "plugins": ["Fixes.esp"], "masters": ["Skyrim.esm", *masters], "detects": []}
               for n, masters in enumerate(main_builds_masters)]
    event = grass_patches_event(village_option={
        "name": "Fixes_BashedPatchFix.esp", "plugins": ["Fixes_BashedPatchFix.esp"],
        "masters": ["Skyrim.esm", "Fixes.esp", USSEP], "detects": []})
    skipped = event["steps"][0]["groups"][0]["options"][1]
    skipped["id"] = "step:0/group:1/option:0"
    event["steps"][0]["groups"] = [
        {"id": "step:0/group:0", "name": "Main", "type": "SelectExactlyOne", "options": options},
        {"id": "step:0/group:1", "name": "Bashed Patch", "type": "SelectAny", "options": [skipped]}]
    return event


USSEP = "unofficial skyrim special edition patch.esp"


@pytest.mark.parametrize("builds", [
    [[USSEP]],
    [[], [USSEP]],   # the picked build does not need it, the other one does
])
def test_a_requirement_the_mods_own_plugin_has_does_not_make_an_extra_a_missed_patch(builds):
    assert find_missed_patches([bashed_patch_event(builds)], load_order(USSEP))["missed"] == []


def test_the_mods_own_installed_plugin_lends_its_masters_from_mo2_when_the_installer_recorded_none():
    """Real WACCF 2.11.4: the installer records no masters for the main plugin it installed; MO2 knows them."""

    event = bashed_patch_event([[]])
    del event["steps"][0]["groups"][0]["options"][0]["masters"]
    order = load_order(USSEP, "Fixes.esp")
    order["plugin_masters"] = {"Fixes.esp": ["Skyrim.esm", USSEP]}
    assert find_missed_patches([event], order)["missed"] == []
    # Without MO2's masters the extra would look like a USSEP patch: that is the bug this guards.
    order["plugin_masters"] = {}
    assert [i["label"] for i in find_missed_patches([event], order)["missed"]] == ["Fixes_BashedPatchFix.esp"]


def test_an_option_whose_condition_looks_only_for_the_mods_own_requirement_is_not_flagged():
    """Real WACCF 2.11.4: the hidden SkyPatched Modular step's "Constructible Objects" looks for USSEP, which WACCF
    itself needs."""

    event = bashed_patch_event([[USSEP]])
    event["steps"].append({"id": "step:1", "name": "Modular", "visible": False, "groups": [{
        "id": "step:1/group:0", "name": "Other", "type": "SelectAny", "options": [{
            "id": "step:1/group:0/option:0", "name": "Constructible Objects", "selected": False, "enabled": True,
            "plugins": [], "detects": [USSEP]}]}]})
    labels = [i["label"] for i in find_missed_patches([event], load_order(USSEP))["missed"]]
    assert "Constructible Objects" not in labels


def test_an_option_on_a_page_the_installer_never_showed_counts_only_by_its_file_condition():
    """A hidden page is not offered: its option's own file condition still says a mod added later brings it up (as in
    1.0.2), but neither its name nor its plugin's masters do."""

    event = bashed_patch_event([[]])
    event["steps"].append({"id": "step:1", "name": "Patches", "visible": False, "groups": [{
        "id": "step:1/group:0", "name": "Other", "type": "SelectAny", "options": [
            {"id": "step:1/group:0/option:0", "name": "Village patch", "selected": False, "enabled": True,
             "plugins": ["Fixes - Village.esp"], "masters": ["Skyrim.esm"], "detects": ["Village.esp"]},
            {"id": "step:1/group:0/option:1", "name": "Town patch", "selected": False, "enabled": True,
             "plugins": ["Fixes - Town.esp"], "masters": ["Skyrim.esm", "Town.esp"], "detects": []},
            {"id": "step:1/group:0/option:2", "name": "Town", "selected": False, "enabled": True,
             "plugins": [], "detects": []}]}]})
    missed = find_missed_patches([event], load_order("Village.esp", "Town.esp"))["missed"]
    assert [(i["label"], i["needs"]) for i in missed] == [("Village patch", ["Village.esp"])]


def test_an_extra_built_on_a_plugin_two_picked_options_put_down_is_not_flagged_by_its_masters():
    """Real WACCF 2.11.4 with SkyPatched Full: the main file's plugin (needs USSEP) and SkyPatched Full's lighter
    copy of it (doesn't) are both picked; MO2 sees only the lighter one and the installer recorded neither."""

    event = bashed_patch_event([[]])
    main = event["steps"][0]["groups"][0]["options"][0]
    del main["masters"]
    event["steps"][0]["groups"].insert(1, {"id": "step:0/group:2", "name": "SkyPatched", "type": "SelectExactlyOne",
                                           "options": [{"id": "step:0/group:2/option:0", "name": "Full",
                                                        "selected": True, "enabled": True,
                                                        "plugins": ["Fixes.esp"], "detects": []}]})
    order = load_order(USSEP, "Fixes.esp")
    order["plugin_masters"] = {"Fixes.esp": ["Skyrim.esm"]}
    assert find_missed_patches([event], order)["missed"] == []
    # A file condition on another mod still counts.
    event["steps"][0]["groups"][2]["options"][0]["detects"] = ["Village.esp"]
    order["active_plugins"].append("Village.esp")
    assert [i["needs"] for i in find_missed_patches([event], order)["missed"]] == [["Village.esp"]]


def test_a_patch_on_a_plugin_two_picked_options_lay_counts_only_by_its_file_condition():
    """Known limitation, on purpose: a base file plus a hotfix both lay Fixes.esp, so the hidden copy's masters are
    unknown and a patch on it can't be told apart from the mod's own needs by its masters (WACCF's SkyPatched Full is
    the same shape). Only its file condition counts. Reading the hidden copy from the archive could lift this."""

    event = bashed_patch_event([[]])
    del event["steps"][0]["groups"][0]["options"][0]["masters"]
    event["steps"][0]["groups"][1]["options"][0]["masters"] = ["Skyrim.esm", "Fixes.esp", "Village.esp"]
    order = load_order("Village.esp", "Fixes.esp")
    order["plugin_masters"] = {"Fixes.esp": ["Skyrim.esm"]}
    assert [i["label"] for i in find_missed_patches([event], order)["missed"]] == ["Fixes_BashedPatchFix.esp"]
    event["steps"][0]["groups"].insert(1, {"id": "step:0/group:2", "name": "Hotfix", "type": "SelectAny",
                                           "options": [{"id": "step:0/group:2/option:0", "name": "Hotfix",
                                                        "selected": True, "enabled": True,
                                                        "plugins": ["Fixes.esp"], "detects": []}]})
    assert find_missed_patches([event], order)["missed"] == []
    event["steps"][0]["groups"][2]["options"][0]["detects"] = ["Village.esp"]
    assert [i["needs"] for i in find_missed_patches([event], order)["missed"]] == [["Village.esp"]]


@pytest.mark.parametrize("picked_files, flagged", [
    ([{"kind": "folder", "source": "skypatcher\\ConstructibleObject", "destination": ""}], False),   # same folder
    ([{"kind": "folder", "source": "skypatcher", "destination": ""}], True),   # parent: lands a folder deeper
    ([{"kind": "file", "source": "skypatcher\\ConstructibleObject", "destination": ""}], False),  # same entry
    ([{"kind": "folder", "source": "skypatcher", "destination": "SKSE"}], True),                      # elsewhere
    ([{"kind": "folder", "source": "skypatcher\\Armor", "destination": ""}], True),                   # another one
])
def test_an_option_whose_files_a_picked_option_installed_is_not_a_skipped_patch(picked_files, flagged):
    """Real WACCF 2.11.4: SkyPatched Full installs skypatcher\\ConstructibleObject, which the hidden Modular page's
    "Constructible Objects" (file condition: USSEP) would install."""

    event = bashed_patch_event([[]])
    event["steps"][0]["groups"][0]["options"][0]["files"] = picked_files
    event["steps"].append({"id": "step:1", "name": "Modular", "visible": False, "groups": [{
        "id": "step:1/group:0", "name": "Other", "type": "SelectAny", "options": [{
            "id": "step:1/group:0/option:0", "name": "Constructible Objects", "selected": False, "enabled": True,
            "plugins": [], "detects": ["Village.esp"],
            "files": [{"kind": "folder", "source": "skypatcher\\ConstructibleObject", "destination": ""}]}]}]})
    labels = [i["label"] for i in find_missed_patches([event], load_order("Village.esp"))["missed"]]
    assert ("Constructible Objects" in labels) is flagged


def test_a_skipped_patch_does_not_count_its_own_masters_as_the_mods_requirements():
    """Only the plugins this install put in (and the plugin a patch builds on) lend their requirements."""

    event = grass_patches_event(village_option=VILLAGE_BY_MASTERS)
    assert [i["label"] for i in find_missed_patches([event], load_order("Village.esp"))["missed"]] == ["Village patch"]


def test_a_patch_for_another_mod_is_still_reported_when_the_main_plugin_does_not_need_that_mod():
    event = bashed_patch_event([[USSEP]])
    event["steps"][0]["groups"][1]["options"][0]["masters"].append("Village.esp")
    [item] = find_missed_patches([event], load_order(USSEP, "Village.esp"))["missed"]
    assert item["needs"] == ["Village.esp"]


def test_a_build_with_several_plugins_does_not_lend_its_masters_to_each_of_them():
    event = bashed_patch_event([[]])
    event["steps"][0]["groups"][0]["options"].append({
        "id": "step:0/group:0/option:9", "name": "All in one", "selected": False, "enabled": True,
        "plugins": ["Fixes.esp", "Fixes - Village.esp"], "masters": ["Village.esp"], "detects": []})
    event["steps"][0]["groups"][1]["options"][0]["masters"] = ["Skyrim.esm", "Fixes.esp", "Village.esp"]
    assert "Fixes_BashedPatchFix.esp" in [
        i["label"] for i in find_missed_patches([event], load_order("Village.esp"))["missed"]]


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
