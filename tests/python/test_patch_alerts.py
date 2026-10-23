"""New-patch alerts: which skipped options became patches because a mod was just installed."""

from __future__ import annotations

from copy import deepcopy

from fomod_logger_support.core import patch_alerts
from fomod_logger_support.core.missed_patches import find_missed_patches
from fomod_logger_support.core.storage import EventStore

INSTANCE = "a" * 64
GRASS_ID = "22222222-2222-4222-8222-222222222222"
VILLAGE_OPTION = "step:0/group:0/option:1"
BASE = ["Skyrim.esm", "Update.esm"]


def grass_patches(option: dict | None = None, *, picked_main: bool = True, only: dict | None = None) -> dict:
    """Grass Patches: a main plugin and a skipped "Village patch", made for Village.esp. ``only`` replaces the
    patch option's evidence (everything but its id, name and flags)."""

    patch = {"id": VILLAGE_OPTION, "name": "Village patch", "selected": False, "enabled": True}
    patch.update(only if only is not None else {
        "plugins": ["GP - Village.esp"], "masters": ["Skyrim.esm", "GrassPatches.esp", "Village.esp"],
        "detects": ["Village.esp"]})
    patch.update(option or {})
    return {
        "schema_version": 1, "event_id": GRASS_ID,
        "started_at": "2026-10-20T12:00:00+00:00", "completed_at": "2026-10-20T12:01:00+00:00",
        "instance": {"id": INSTANCE, "label": "MO2 Test"},
        "source": {"archive": "Grass Patches.7z", "reinstallation": False},
        "mod": {"name": "Grass Patches", "version": "1.0", "nexus_id": 1},
        "installer": {"name": "FOMOD Logger Native Installer", "version": "1.0.0",
                      "upstream_commit": "fc263f2d923c704b4853c11ed4f8b8cf3920f30d"},
        "steps": [{"id": "step:0", "name": "Compatibility", "visible": True, "groups": [{
            "id": "step:0/group:0", "name": "Patches", "type": "SelectAny", "options": [
                {"id": "step:0/group:0/option:0", "name": "Core", "selected": picked_main, "enabled": True,
                 "plugins": ["GrassPatches.esp"], "masters": ["Skyrim.esm"], "detects": []},
                patch]}]}],
    }


def order(*active: str, mods: tuple[str, ...] = (), disabled: dict | None = None,
          disabled_mods: tuple[str, ...] = ()) -> dict:
    return {"instance_id": INSTANCE, "active_plugins": [*BASE, "GrassPatches.esp", *active], "base_plugins": BASE,
            "mods": ["Grass Patches", *mods], "unchecked_plugins": [], "plugin_masters": {},
            "disabled_mod_plugins": disabled or {}, "disabled_mods": list(disabled_mods)}


def alerts_for(event: dict, load_order: dict, mod: str, plugins: list[str], **kwargs) -> list[dict]:
    return patch_alerts.new_patches([event], load_order, mod_name=mod, mod_plugins=plugins, **kwargs)


def test_installing_the_mod_a_skipped_patch_is_made_for_raises_an_alert():
    event = grass_patches()
    before = order()
    assert find_missed_patches([event], before)["missed"] == []   # no Village yet: nothing to flag
    after = order("Village.esp", mods=("Village",))
    [item] = alerts_for(event, after, "Village", ["Village.esp"], previous=before)
    assert item["label"] == "Village patch" and item["state"] == "missing"
    assert patch_alerts.item_key(item) == (GRASS_ID, VILLAGE_OPTION)
    assert item["mod_name"] == "Grass Patches"


def test_a_mod_nothing_was_made_for_raises_nothing():
    event = grass_patches()
    after = order("Bridges.esp", mods=("Bridges",))
    assert alerts_for(event, after, "Bridges", ["Bridges.esp"], previous=order()) == []
    # Village is in the list too, but this install is not Village: only entries made for the new mod count.
    both = order("Village.esp", "Bridges.esp", mods=("Village", "Bridges"))
    assert alerts_for(event, both, "Bridges", ["Bridges.esp"]) == []


def test_a_patch_that_is_already_installed_raises_nothing():
    event = grass_patches()
    after = order("Village.esp", "GP - Village.esp", mods=("Village",))
    assert alerts_for(event, after, "Village", ["Village.esp"]) == []
    # The option was picked: not skipped, so not a candidate either.
    picked = grass_patches({"selected": True})
    assert alerts_for(picked, order("Village.esp", mods=("Village",)), "Village", ["Village.esp"]) == []


def test_not_needed_and_the_other_ways_of_settling_a_patch_stop_the_alert():
    event = grass_patches()
    after = order("Village.esp", mods=("Village",))
    dismissed = [{"event_id": GRASS_ID, "option_id": VILLAGE_OPTION, "state": "dismissed", "reason": ""}]
    assert alerts_for(event, after, "Village", ["Village.esp"], reviews=dismissed) == []
    resolved = [{**dismissed[0], "state": "resolved"}]
    assert alerts_for(event, after, "Village", ["Village.esp"], reviews=resolved) == []
    # A reminder keeps the card listed, and a mod with a reminder is exactly what an alert is for.
    reminded = [{**dismissed[0], "state": "pending"}]
    [item] = alerts_for(event, after, "Village", ["Village.esp"], reviews=reminded)
    assert item["reminder"] is True


def test_a_mod_that_is_still_switched_off_raises_the_switched_off_state():
    event = grass_patches()
    # MO2 installs a mod disabled: its plugins are in the mod folder but not in the plugin list.
    after = order(mods=(), disabled={"Village.esp": "Village"}, disabled_mods=("Village",))
    [item] = alerts_for(event, after, "Village", ["Village.esp"])
    assert item["state"] == "needs-disabled"
    assert item["disabled"] == [{"plugin": "Village.esp", "where": "its mod “Village” is disabled"}]
    [group] = patch_alerts.group_by_mod([{
        "instance_id": INSTANCE, "mod_key": "village", "mod_name": "Village", "created_at": "2026-10-21T10:00:00",
        "event_id": GRASS_ID, "option_id": VILLAGE_OPTION, "item": item}])
    # describe_group says the mod has to be switched on first.
    _short, lines, advice = patch_alerts.describe_group(group)
    assert "Village is still disabled in MO2: enable it first." in advice


def test_an_update_only_counts_what_the_install_added():
    event = grass_patches()
    old = order("Village.esp", mods=("Village",))
    new = order("Village.esp", "Village Extras.esp", mods=("Village",))
    # Village was there before and its patch was already listed: nothing new.
    assert alerts_for(event, new, "Village", ["Village.esp"], previous=old) == []
    # With no earlier snapshot to compare with (a fresh mod), it is new.
    assert len(alerts_for(event, new, "Village", ["Village.esp"], previous=None)) == 1


def test_a_name_only_event_never_errors():
    # An installer logged before patch details existed: no plugins, masters or detects anywhere.
    legacy = grass_patches()
    for step in legacy["steps"]:
        for group in step["groups"]:
            for option in group["options"]:
                for key in ("plugins", "masters", "detects"):
                    option.pop(key, None)
    after = order("Village.esp", mods=("Village",))
    assert alerts_for(legacy, after, "Village", ["Village.esp"]) == []
    # A mod with no plugins at all, and an event with nothing in it.
    assert alerts_for(legacy, after, "Textures Only", []) == []
    bare = deepcopy(legacy)
    bare["steps"] = []
    assert alerts_for(bare, after, "Village", ["Village.esp"]) == []
    # A patch found only by its label (no plugin of its own) is made for the mod the label names.
    named = grass_patches(only={"name": "Village Overhaul patch", "plugins": []})
    named_order = order(mods=("Village Overhaul",))
    [item] = alerts_for(named, named_order, "Village Overhaul", [])
    assert item["kind"] == "name" and item["needs"] == ["Village Overhaul"]
    assert alerts_for(named, named_order, "Something Else", []) == []


def test_live_alerts_settle_when_the_patch_is_dealt_with():
    event = grass_patches()
    after = order("Village.esp", mods=("Village",))
    row = {"instance_id": INSTANCE, "mod_key": "village", "mod_name": "Village", "event_id": GRASS_ID,
           "option_id": VILLAGE_OPTION, "install_event_id": "", "state": "pending",
           "created_at": "2026-10-21T10:00:00.000000+00:00"}

    def split(load_order: dict, reviews: list | None = None):
        missed = find_missed_patches([event], load_order, reviews or [])["missed"]
        return patch_alerts.live_alerts([row], missed, reviews or [])

    live, settled = split(after)
    assert [r["option_id"] for r in live] == [VILLAGE_OPTION] and settled == []
    assert live[0]["item"]["label"] == "Village patch"
    # The patch got installed.
    assert split(order("Village.esp", "GP - Village.esp", mods=("Village",)))[0] == []
    # Marked Not needed.
    assert split(after, [{"event_id": GRASS_ID, "option_id": VILLAGE_OPTION, "state": "dismissed",
                          "updated_at": "2026-10-21T10:05:00+00:00"}])[0] == []
    # A reminder set after the alert settles it; one that was already there does not.
    pending = {"event_id": GRASS_ID, "option_id": VILLAGE_OPTION, "state": "pending", "reason": ""}
    assert split(after, [{**pending, "updated_at": "2026-10-21T10:05:00+00:00"}])[0] == []
    assert len(split(after, [{**pending, "updated_at": "2026-10-20T09:00:00+00:00"}])[0]) == 1
    # The mod was switched off and removed from the list altogether: nothing to patch any more.
    assert split(order())[0] == []


def test_alert_text_names_installer_page_option_and_why():
    event = grass_patches()
    after = order("Village.esp", mods=("Village",))
    [item] = alerts_for(event, after, "Village", ["Village.esp"])
    rows = [{"instance_id": INSTANCE, "mod_key": "village", "mod_name": "Village", "created_at": "t",
             "event_id": GRASS_ID, "option_id": VILLAGE_OPTION, "item": item}]
    [group] = patch_alerts.group_by_mod(rows)
    short, lines, advice = patch_alerts.describe_group(group)
    assert short == "New patches available for Village"
    assert lines == ["Grass Patches › Compatibility › Village patch: patch for Village; "
                     "you skipped it when you installed Grass Patches"]
    assert advice == ["Reinstall Grass Patches and pick the patch to add it."]
    assert "enable it first" not in " ".join(advice)
    unavailable = {**item, "available": False}
    assert patch_alerts.describe_item(unavailable, "Village").endswith("it wasn't available when you installed Grass Patches")
    automatic = {**item, "automatic": True, "label": "Automatic patch for Village.esp", "step_name": ""}
    assert patch_alerts.describe_item(automatic, "Village") == (
        "Grass Patches › Automatic patch for Village.esp: patch for Village; "
        "it wasn't added when you installed Grass Patches")


def test_alerts_come_out_of_the_store_with_their_cards(tmp_path):
    event = grass_patches()
    with EventStore(tmp_path / "db") as store:
        store.ingest(event)
        after = order("Village.esp", mods=("Village",))
        found = alerts_for(event, after, "Village", ["Village.esp"])
        keys = [patch_alerts.item_key(item) for item in found]
        assert store.add_patch_alerts(INSTANCE, "Village", "", keys) == keys
        missed = find_missed_patches(store.latest(""), after, store.reviews())["missed"]
        live, settled = patch_alerts.live_alerts(store.patch_alerts(INSTANCE), missed, store.reviews())
        assert len(live) == 1 and settled == []
        assert store.clear_patch_alerts(settled) == 0


def _row(**kwargs) -> dict:
    return {"instance_id": INSTANCE, "mod_key": "village", "mod_name": "Village", "event_id": GRASS_ID,
            "option_id": VILLAGE_OPTION, "install_event_id": "", "state": "pending",
            "created_at": "2026-10-21T10:00:00.000000+00:00", **kwargs}


def test_an_alert_is_cleared_for_good_only_for_a_definite_reason():
    event = grass_patches()
    row = _row()

    def split(load_order: dict, *, reviews=(), gone=(), events=(event,)):
        missed = find_missed_patches([event], load_order, list(reviews))["missed"]
        installed = [*load_order["active_plugins"], *load_order.get("unchecked_plugins", []),
                     *load_order.get("disabled_mod_plugins", {})]
        return patch_alerts.live_alerts([row], missed, list(reviews), gone=gone, events=events, installed=installed)

    empty = {**order(), "mods": [], "active_plugins": []}    # MO2 hasn't read its lists yet
    live, settled = split(empty)
    assert live == [] and settled == []    # hidden for now, still pending: nothing says it was dealt with
    unlisted = order("Bridges.esp", mods=("Bridges",))    # the mod is gone from the list (or not listed yet)
    assert split(unlisted) == ([], [])

    on = order("Village.esp", mods=("Village",))
    assert [r["option_id"] for r in split(on)[0]] == [VILLAGE_OPTION] and split(on)[1] == []
    # Definite: the patch is installed (active, or present but switched off), a review settled it,
    # or the installer's record is gone.
    assert split(order("Village.esp", "GP - Village.esp", mods=("Village",))) == ([], [row])
    off = order("Village.esp", mods=("Village",), disabled={"GP - Village.esp": "Grass Patches"})
    assert split(off) == ([], [row])
    assert split(on, reviews=[{"event_id": GRASS_ID, "option_id": VILLAGE_OPTION, "state": "dismissed"}]) == ([], [row])
    assert split(unlisted, gone=[GRASS_ID], events=()) == ([], [row])


def test_a_patch_made_for_two_mods_installed_together_is_one_group():
    def row(mod: str, option: str, created: str) -> dict:
        item = {"event_id": GRASS_ID, "option_id": option, "mod_name": "Grass Patches", "step_name": "Compatibility",
                "label": option, "state": "missing", "available": True}
        return _row(mod_key=mod.casefold(), mod_name=mod, option_id=option, created_at=created, item=item)

    rows = [row("Village", "a", "2"), row("Village", "both", "2"), row("Bridges", "both", "2"), row("Bridges", "b", "2"),
            row("Fences", "c", "1")]
    groups = patch_alerts.group_by_mod(rows)
    assert [g["mod_name"] for g in groups] == ["Fences", "Bridges and Village"]    # oldest first
    assert groups[1]["mod_keys"] == ["bridges", "village"] and len(groups[1]["rows"]) == 4
    _short, lines, _advice = patch_alerts.describe_group(groups[1])
    assert len(lines) == 3 and sum("both" in line for line in lines) == 1   # the shared patch is listed once
    assert patch_alerts.join_names(["B", "a", "C", "a"]) == "a, B and C"
    assert patch_alerts.join_names([]) == ""
