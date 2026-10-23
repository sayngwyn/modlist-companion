from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from fomod_logger_support.core.installer_changes import compare_events, installer_changes

INSTANCE = "a" * 64


def _opt(name, picked=False, **evidence):
    return {"name": name, "selected": picked, "enabled": True, **evidence}


def _tree(*steps):
    """steps: (name, [(group name, [option dicts])]) with positional ids filled in."""

    built = []
    for s, (step_name, groups) in enumerate(steps):
        built_groups = []
        for g, (group_name, options) in enumerate(groups):
            built_groups.append({
                "id": f"step:{s}/group:{g}", "name": group_name, "type": "SelectAny",
                "options": [{"id": f"step:{s}/group:{g}/option:{o}", **option} for o, option in enumerate(options)]})
        built.append({"id": f"step:{s}", "name": step_name, "visible": True, "groups": built_groups})
    return built


def _event(installation_event, number, steps, *, version="1.0", name="Fixture Mod", day=None, removed=False):
    event = deepcopy(installation_event)
    event["event_id"] = str(UUID(int=number, version=4))
    event["completed_at"] = f"2026-09-{day or number:02d}T12:00:00+00:00"
    event["mod"].update(name=name, version=version)
    event["steps"] = steps
    event["removed_at"] = "2026-10-01T00:00:00+00:00" if removed else None
    return event


def _names(items):
    return [item["name"] for item in items]


BASE = [("Main", [("Textures", [_opt("1K", True), _opt("2K")]), ("Extras", [_opt("Snow"), _opt("Fog")])])]


def test_an_identical_reinstall_changes_nothing(installation_event):
    old = _event(installation_event, 1, _tree(*BASE))
    new = _event(installation_event, 2, _tree(*BASE))
    result = compare_events(old, new)
    assert not result["changed"]
    assert result["added"] == result["removed"] == result["renamed"] == []
    assert installer_changes([old, new]) == []


def test_a_new_option_is_added_and_a_picked_state_change_is_not_a_change(installation_event):
    old = _event(installation_event, 1, _tree(*BASE), version="1.0")
    changed = [("Main", [("Textures", [_opt("1K"), _opt("2K", True), _opt("4K")]),
                         ("Extras", [_opt("Snow"), _opt("Fog")])])]
    new = _event(installation_event, 2, _tree(*changed), version="1.1")
    result = compare_events(old, new)
    assert _names(result["added"]) == ["4K"]
    assert result["added"][0]["option_id"] == "step:0/group:0/option:2"  # its id in the new install
    assert result["added"][0]["group_name"] == "Textures"
    assert result["removed"] == result["renamed"] == [] and result["steps_added"] == []
    assert (result["old_version"], result["new_version"]) == ("1.0", "1.1")


def test_a_removed_option(installation_event):
    old = _event(installation_event, 1, _tree(*BASE))
    new = _event(installation_event, 2, _tree(("Main", [("Textures", [_opt("1K", True), _opt("2K")]),
                                                         ("Extras", [_opt("Snow")])])))
    result = compare_events(old, new)
    assert _names(result["removed"]) == ["Fog"] and result["added"] == []
    assert result["removed"][0]["option_id"] == "step:0/group:1/option:1"  # its id in the old install


def test_a_rename_is_one_rename_not_an_add_and_a_remove(installation_event):
    old = _event(installation_event, 1, _tree(*BASE))
    new = _event(installation_event, 2, _tree(("Main", [("Textures", [_opt("1K", True), _opt("2K")]),
                                                         ("Extras", [_opt("Snow"), _opt("Fog (light)")])])))
    result = compare_events(old, new)
    assert [(r["old_name"], r["name"]) for r in result["renamed"]] == [("Fog", "Fog (light)")]
    assert result["added"] == result["removed"] == []


def test_two_new_options_for_one_removed_one_are_not_guessed_to_be_a_rename(installation_event):
    old = _event(installation_event, 1, _tree(*BASE))
    new = _event(installation_event, 2, _tree(("Main", [("Textures", [_opt("1K", True), _opt("2K")]),
                                                         ("Extras", [_opt("Snow"), _opt("Mist"), _opt("Smoke")])])))
    result = compare_events(old, new)
    assert result["renamed"] == [] and _names(result["removed"]) == ["Fog"]
    assert _names(result["added"]) == ["Mist", "Smoke"]


def test_a_reworded_label_is_a_rename_even_when_options_were_added_beside_it(installation_event):
    old = _event(installation_event, 1, _tree(("Main", [("Patches", [_opt("Wet & Cold patch")])])))
    new = _event(installation_event, 2, _tree(("Main", [("Patches", [
        _opt("Lux patch"), _opt("Wet and Cold patch"), _opt("Falskaar patch")])])))
    result = compare_events(old, new)
    assert [(r["old_name"], r["name"]) for r in result["renamed"]] == [("Wet & Cold patch", "Wet and Cold patch")]
    assert _names(result["added"]) == ["Lux patch", "Falskaar patch"] and result["removed"] == []


def test_a_new_page_and_a_new_group_are_reported(installation_event):
    old = _event(installation_event, 1, _tree(*BASE))
    new = _event(installation_event, 2, _tree(
        ("Main", [("Textures", [_opt("1K", True), _opt("2K")]), ("Extras", [_opt("Snow"), _opt("Fog")]),
                  ("Sounds", [_opt("Wind")])]),
        ("Weather", [("Preset", [_opt("Darker"), _opt("Brighter")])])))
    result = compare_events(old, new)
    assert _names(result["added"]) == ["Wind", "Darker", "Brighter"]
    assert result["steps_added"] == ["Weather"]
    assert result["groups_added"] == [("Main", "Sounds")]  # the group of the new page isn't listed twice
    back = compare_events(new, old)
    assert back["steps_removed"] == ["Weather"] and back["groups_removed"] == [("Main", "Sounds")]
    assert _names(back["removed"]) == ["Wind", "Darker", "Brighter"]


def test_options_with_the_same_name_are_matched_by_occurrence(installation_event):
    twice = [("Main", [("A", [_opt("Shared"), _opt("Shared")])])]
    thrice = [("Main", [("A", [_opt("Shared"), _opt("Shared"), _opt("Shared")])])]
    old = _event(installation_event, 1, _tree(*twice))
    new = _event(installation_event, 2, _tree(*thrice))
    assert compare_events(old, old)["changed"] is False
    result = compare_events(old, new)
    assert _names(result["added"]) == ["Shared"] and result["added"][0]["option_id"] == "step:0/group:0/option:2"
    assert compare_events(new, old)["removed"][0]["option_id"] == "step:0/group:0/option:2"
    # The same label in two groups is two options.
    split = [("Main", [("A", [_opt("Shared")]), ("B", [_opt("Shared")])])]
    assert compare_events(_event(installation_event, 3, _tree(*split)),
                          _event(installation_event, 4, _tree(*split)))["changed"] is False


def test_an_inserted_option_shifts_ids_but_changes_only_what_is_new(installation_event):
    old = _event(installation_event, 1, _tree(*BASE))
    new = _event(installation_event, 2, _tree(("Main", [("Textures", [_opt("0.5K"), _opt("1K", True), _opt("2K")]),
                                                         ("Extras", [_opt("Snow"), _opt("Fog")])])))
    assert new["steps"][0]["groups"][0]["options"][1]["id"].endswith("option:1")  # "1K" was option:0
    result = compare_events(old, new)
    assert _names(result["added"]) == ["0.5K"] and result["removed"] == result["renamed"] == []


def test_reordering_and_moving_between_groups_are_ignored(installation_event):
    old = _event(installation_event, 1, _tree(*BASE))
    reordered = [("Main", [("Extras", [_opt("Fog"), _opt("Snow")]), ("Textures", [_opt("2K"), _opt("1K", True)])])]
    assert compare_events(old, _event(installation_event, 2, _tree(*reordered)))["changed"] is False
    moved = [("Main", [("Textures", [_opt("1K", True), _opt("2K"), _opt("Fog")]), ("Extras", [_opt("Snow")])])]
    assert compare_events(old, _event(installation_event, 3, _tree(*moved)))["changed"] is False
    renamed_group = [("Main", [("Resolution", [_opt("1K", True), _opt("2K")]),
                               ("Extras", [_opt("Snow"), _opt("Fog")])])]
    assert compare_events(old, _event(installation_event, 4, _tree(*renamed_group)))["changed"] is False


def test_a_reinstall_with_one_more_pick_is_a_pick_change_and_no_installer_change(installation_event):
    old = _event(installation_event, 1, _tree(*BASE))
    more = [("Main", [("Textures", [_opt("1K", True), _opt("2K")]), ("Extras", [_opt("Snow", True), _opt("Fog")])])]
    new = _event(installation_event, 2, _tree(*more))
    result = compare_events(old, new)
    assert result["changed"] is False and result["added"] == result["removed"] == result["renamed"] == []
    assert result["picks_changed"] is True and result["unpicked"] == []
    [snow] = result["picked"]
    assert snow["name"] == "Snow" and snow["group_name"] == "Extras" and snow["step_name"] == "Main"
    assert snow["option_id"] == "step:0/group:1/option:0"  # its id in the new install
    # Not an installer change, so the card list stays empty; asked for, it is listed.
    assert installer_changes([old, new]) == []
    [listed] = installer_changes([old, new], picks=True)
    assert listed["event_id"] == new["event_id"] and listed["changed"] is False and _names(listed["picked"]) == ["Snow"]
    assert compare_events(old, old)["picks_changed"] is False


def test_a_swapped_pick_in_a_pick_one_group_is_one_picked_and_one_unpicked(installation_event):
    old = _event(installation_event, 1, _tree(*BASE))
    swapped = [("Main", [("Textures", [_opt("1K"), _opt("2K", True)]), ("Extras", [_opt("Snow"), _opt("Fog")])])]
    new = _event(installation_event, 2, _tree(*swapped))
    result = compare_events(old, new)
    assert (_names(result["picked"]), _names(result["unpicked"])) == (["2K"], ["1K"])
    assert result["unpicked"][0]["option_id"] == "step:0/group:0/option:0" and result["changed"] is False
    # Going back again swaps them the other way round.
    back = compare_events(new, old)
    assert (_names(back["picked"]), _names(back["unpicked"])) == (["1K"], ["2K"])


def test_a_picked_new_option_is_new_not_newly_picked_and_a_renamed_option_is_compared_too(installation_event):
    old = _event(installation_event, 1, _tree(*BASE))
    new = _event(installation_event, 2, _tree(("Main", [
        ("Textures", [_opt("1K", True), _opt("2K"), _opt("4K", True)]), ("Extras", [_opt("Snow"), _opt("Fog (light)")])])))
    result = compare_events(old, new)
    assert _names(result["added"]) == ["4K"] and result["picks_changed"] is False  # new, and picked: no pick change
    assert [(r["old_name"], r["name"]) for r in result["renamed"]] == [("Fog", "Fog (light)")]
    # A renamed option is the same option: picking it in the new install is a pick change.
    new["steps"][0]["groups"][1]["options"][1]["selected"] = True
    result = compare_events(old, new)
    assert _names(result["picked"]) == ["Fog (light)"] and result["unpicked"] == []


def test_picks_on_a_page_the_installer_skipped_are_not_pick_changes(installation_event):
    def tree(visible_old, visible_new, picked_new):
        old = _event(installation_event, 1, _tree(*BASE))
        new = _event(installation_event, 2, _tree(("Main", [("Textures", [_opt("1K", picked_new), _opt("2K")]),
                                                              ("Extras", [_opt("Snow"), _opt("Fog")])])))
        old["steps"][0]["visible"], new["steps"][0]["visible"] = visible_old, visible_new
        return compare_events(old, new)

    assert tree(True, True, False)["unpicked"] != []         # visible both times: the pick was dropped
    assert tree(False, True, False)["picks_changed"] is False  # skipped in the old install
    assert tree(True, False, False)["picks_changed"] is False  # skipped in the new one
    assert tree(True, True, True)["picks_changed"] is False


def _patch_pair(installation_event, *, picked):
    old = _event(installation_event, 1, _tree(("Main", [("Patches", [
        _opt("Core", True, plugins=["Core.esp"], masters=[], detects=[])])])))
    patch = _opt("Lux Via patch", picked, plugins=["Core - Lux Via.esp"],
                 masters=["Core.esp", "Lux Via.esp"], detects=[])
    new = _event(installation_event, 2, _tree(("Main", [("Patches", [
        _opt("Core", True, plugins=["Core.esp"], masters=[], detects=[]), patch,
        _opt("Other patch", False, plugins=["Core - Nothing.esp"],
             masters=["Core.esp", "Not Installed.esp"], detects=[])])])))
    load_order = {"instance_id": INSTANCE, "active_plugins": ["Skyrim.esm", "Core.esp", "Lux Via.esp"],
                  "base_plugins": ["Skyrim.esm"], "mods": ["Core", "Lux Via"]}
    return old, new, load_order


def test_a_new_patch_for_an_active_plugin_is_called_out_skipped(installation_event):
    old, new, load_order = _patch_pair(installation_event, picked=False)
    result = compare_events(old, new, load_order)
    assert _names(result["added"]) == ["Lux Via patch", "Other patch"]
    [patch] = result["patches"]  # the one for a plugin that isn't in the list isn't
    assert patch["name"] == "Lux Via patch"
    assert patch["patch"] == {"kind": "masters", "needs": ["Lux Via.esp"], "state": "missing",
                              "plugins": ["Core - Lux Via.esp"], "picked": False, "have": False}


def test_a_new_patch_for_an_active_plugin_is_called_out_picked(installation_event):
    old, new, load_order = _patch_pair(installation_event, picked=True)
    [patch] = compare_events(old, new, load_order)["patches"]
    assert patch["patch"]["picked"] is True
    load_order["active_plugins"].append("Core - Lux Via.esp")
    assert compare_events(old, new, load_order)["patches"][0]["patch"]["have"] is True


def test_a_patch_for_a_switched_off_mod_and_no_load_order(installation_event):
    old, new, load_order = _patch_pair(installation_event, picked=False)
    load_order["active_plugins"].remove("Lux Via.esp")
    load_order["unchecked_plugins"] = ["Lux Via.esp"]
    [patch] = compare_events(old, new, load_order)["patches"]
    assert patch["patch"]["state"] == "needs-disabled"
    assert compare_events(old, new)["patches"] == []  # nothing to compare with: no guesses


def test_a_name_only_tree_compares_and_never_guesses_a_patch():
    """A tree of just names and picks, like the picks FOMOD Plus saves: no ids, plugins, masters or detects."""

    def tree(*groups):
        return [{"name": "Install", "groups": [
            {"name": name, "options": [{"name": n, "selected": s} for n, s in options]} for name, options in groups]}]

    old = {"mod": {"name": "Plain", "version": "1"},
           "steps": tree(("Patches", [("Core", True), ("Lux Via patch", False)]))}
    new = {"mod": {"name": "Plain", "version": "2"},
           "steps": tree(("Patches", [("Core", True), ("Lux Via patch", False), ("Lux Via 2 patch", True),
                                      ("Old name", False)]))}
    load_order = {"instance_id": INSTANCE, "active_plugins": ["Lux Via.esp", "Lux Via 2.esp"], "base_plugins": [],
                  "mods": ["Lux Via", "Lux Via 2", "Plain"]}
    result = compare_events(old, new, load_order)  # no ids, event ids, instance or enabled/visible flags anywhere
    assert _names(result["added"]) == ["Lux Via 2 patch", "Old name"]
    assert [item["option_id"] for item in result["added"]] == ["", ""]
    assert result["patches"] == []  # no plugins/masters/detects keys: skipped rather than guessed from the name
    assert result["added"][0]["selected"] is True and result["added"][0]["available"] is True
    assert result["changed"] and (result["old_version"], result["new_version"]) == ("1", "2")
    # Picks compare on names too: no ids and no visibility flags are needed.
    new["steps"][0]["groups"][0]["options"][1]["selected"] = True
    new["steps"][0]["groups"][0]["options"][0]["selected"] = False
    result = compare_events(old, new, load_order)
    assert _names(result["picked"]) == ["Lux Via patch"] and _names(result["unpicked"]) == ["Core"]
    assert result["picked"][0]["option_id"] == "" and result["picks_changed"]


def test_each_mod_compares_its_newest_install_with_the_one_before(installation_event):
    same = _tree(*BASE)
    bigger = _tree(("Main", [("Textures", [_opt("1K", True), _opt("2K"), _opt("4K")]),
                             ("Extras", [_opt("Snow"), _opt("Fog")])]))
    biggest = _tree(("Main", [("Textures", [_opt("1K", True), _opt("2K"), _opt("4K"), _opt("8K")]),
                              ("Extras", [_opt("Snow"), _opt("Fog")])]))
    events = [
        _event(installation_event, 1, same, name="Grows", day=1),
        _event(installation_event, 2, bigger, name="Grows", day=2),
        _event(installation_event, 3, biggest, name="Grows", day=3),
        _event(installation_event, 4, same, name="Single", day=4),
        _event(installation_event, 5, same, name="Steady", day=5),
        _event(installation_event, 6, same, name="steady", day=6),
        _event(installation_event, 7, same, name="Gone", day=7),
        _event(installation_event, 8, bigger, name="Gone", day=8, removed=True),
        _event(installation_event, 9, same, name="Back", day=9, removed=True),
        _event(installation_event, 10, bigger, name="Back", day=10),
    ]
    back, grows = installer_changes(events)  # the others: one install, no change, or their newest was removed
    assert grows["mod_name"] == "Grows" and _names(grows["added"]) == ["8K"]  # newest vs the one before, not the first
    assert grows["old_event_id"] == events[1]["event_id"] and grows["event_id"] == events[2]["event_id"]
    # A mod removed and installed again is compared with the install it replaced.
    assert back["mod_name"] == "Back" and _names(back["added"]) == ["4K"]
    assert installer_changes(events, instance_id="b" * 64) == []


def test_the_newest_install_is_compared_with_the_one_directly_before_it_even_if_that_was_removed(installation_event):
    first = _tree(*BASE)
    bigger = _tree(("Main", [("Textures", [_opt("1K", True), _opt("2K"), _opt("4K")]),
                             ("Extras", [_opt("Snow"), _opt("Fog")])]))
    events = [
        _event(installation_event, 1, first, name="Mod", day=1),
        _event(installation_event, 2, bigger, name="Mod", day=2, removed=True),
        _event(installation_event, 3, bigger, name="Mod", day=3),
    ]
    assert installer_changes(events) == []  # the same as the install before: nothing is new
    events[2] = _event(installation_event, 3, _tree(("Main", [("Textures", [_opt("1K", True), _opt("2K"), _opt("4K"),
                                                                            _opt("8K")]),
                                                                ("Extras", [_opt("Snow"), _opt("Fog")])])),
                       name="Mod", day=3)
    [result] = installer_changes(events)
    assert _names(result["added"]) == ["8K"] and result["old_event_id"] == events[1]["event_id"]


def _patch_option(name, plugin, *masters, picked=False):
    return _opt(name, picked, **({"plugins": [plugin]} if plugin else {}), masters=list(masters), detects=[])


def test_a_new_patch_in_the_slot_of_a_dropped_one_is_not_taken_for_a_rename(installation_event):
    load_order = {"instance_id": INSTANCE, "active_plugins": ["M.esp", "X.esp", "Y.esp"], "base_plugins": [], "mods": []}
    old = _event(installation_event, 1, _tree(("Main", [("Patches", [
        _patch_option("Patch for X", "M - X.esp", "M.esp", "X.esp")])])))
    new = _event(installation_event, 2, _tree(("Main", [("Patches", [
        _patch_option("Patch for Y", None, "M.esp", "Y.esp")])])))
    result = compare_events(old, new, load_order)
    # The labels are alike, so it is a rename; but it now points at other plugins, so it is judged as a patch.
    assert [(r["old_name"], r["name"]) for r in result["renamed"]] == [("Patch for X", "Patch for Y")]
    [patch] = result["patches"]
    assert patch["name"] == "Patch for Y" and patch["patch"]["needs"] == ["M.esp", "Y.esp"]
    # With unlike labels it is a removal and an addition.
    new = _event(installation_event, 3, _tree(("Main", [("Patches", [
        _patch_option("Compatibility with the Yorkshire mod", "M - Y.esp", "M.esp", "Y.esp")])])))
    result = compare_events(old, new, load_order)
    assert result["renamed"] == [] and _names(result["removed"]) == ["Patch for X"]
    assert [p["name"] for p in result["patches"]] == ["Compatibility with the Yorkshire mod"]


def test_two_patches_swapped_for_two_others_are_both_called_out(installation_event):
    load_order = {"instance_id": INSTANCE, "active_plugins": ["M.esp", "Y.esp", "W.esp"], "base_plugins": [], "mods": []}
    old = _event(installation_event, 1, _tree(("Main", [("Patches", [
        _patch_option("Patch for X", "M - X.esp", "M.esp", "X.esp"),
        _patch_option("Patch for Z", "M - Z.esp", "M.esp", "Z.esp")])])))
    new = _event(installation_event, 2, _tree(("Main", [("Patches", [
        _patch_option("Patch for Y", "M - Y.esp", "M.esp", "Y.esp"),
        _patch_option("Patch for W", "M - W.esp", "M.esp", "W.esp")])])))
    result = compare_events(old, new, load_order)
    assert sorted(p["name"] for p in result["patches"]) == ["Patch for W", "Patch for Y"]
    # The same slots with different plugins and unlike labels: removed and added, patches called out as before.
    new = _event(installation_event, 3, _tree(("Main", [("Patches", [
        _patch_option("Yorkshire compatibility", "M - Y.esp", "M.esp", "Y.esp"),
        _patch_option("Wessex compatibility", "M - W.esp", "M.esp", "W.esp")])])))
    result = compare_events(old, new, load_order)
    assert result["renamed"] == [] and len(result["removed"]) == 2
    assert sorted(p["name"] for p in result["patches"]) == ["Wessex compatibility", "Yorkshire compatibility"]
    # A real relabel keeps its plugins and is only a rename.
    new = _event(installation_event, 4, _tree(("Main", [("Patches", [
        _patch_option("Compatibility with X", "M - X.esp", "M.esp", "X.esp"),
        _patch_option("Patch for Z", "M - Z.esp", "M.esp", "Z.esp")])])))
    result = compare_events(old, new, load_order)
    assert [r["name"] for r in result["renamed"]] == ["Compatibility with X"] and result["patches"] == []
