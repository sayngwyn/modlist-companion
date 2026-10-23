from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from fomod_logger_support.core import compare_choices as cc
from fomod_logger_support.core.installer_changes import compare_events

A_ID, B_ID = "a" * 64, "b" * 64


def _opt(name, picked=False):
    return {"name": name, "selected": picked, "enabled": True}


def _tree(*steps):
    """steps: (name, [(group name, [option dicts])] [, visible]) with positional ids filled in."""

    built = []
    for s, step in enumerate(steps):
        step_name, groups = step[0], step[1]
        visible = step[2] if len(step) > 2 else True
        built_groups = []
        for g, (group_name, options) in enumerate(groups):
            built_groups.append({
                "id": f"step:{s}/group:{g}", "name": group_name, "type": "SelectAny",
                "options": [{"id": f"step:{s}/group:{g}/option:{o}", **option,
                             "selected": bool(option["selected"] and visible)} for o, option in enumerate(options)]})
        built.append({"id": f"step:{s}", "name": step_name, "visible": visible, "groups": built_groups})
    return built


_COUNTER = [0]


def _event(steps, *, name="Majestic Mountains", instance=A_ID, label=None, archive="mm-1234-1-0.7z", nexus=1234,
           version="1.0", day=1, removed=False, hidden=False):
    _COUNTER[0] += 1
    return {
        "schema_version": 1, "event_id": str(UUID(int=_COUNTER[0], version=4)),
        "started_at": f"2026-09-{day:02d}T11:59:00+00:00", "completed_at": f"2026-09-{day:02d}T12:00:00+00:00",
        "instance": {"id": instance, "label": label or ("Main" if instance == A_ID else "Second")},
        "source": {"archive": archive, "reinstallation": False},
        "mod": {"name": name, "version": version, "nexus_id": nexus},
        "installer": {"name": "x", "version": "1", "upstream_commit": "unknown"},
        "steps": steps, "removed_at": "2026-10-01T00:00:00+00:00" if removed else None, "hidden": hidden,
    }


BASE = [("Main", [("Textures", [_opt("1K", True), _opt("2K")]), ("Extras", [_opt("Snow"), _opt("Fog", True)])])]


def _flat(result):
    """{(page, group, option name): status} for every option of a comparison."""

    return {(page["name"], group["name"], option["name"]): option["status"]
            for page in result["pages"] for group in page["groups"] for option in group["options"]}


# ---------- two installs ----------

def test_identical_choices_are_the_same_everywhere():
    result = cc.compare_installs(_event(_tree(*BASE)), _event(_tree(*BASE), name="Copy"))
    assert result["status"] == "same" and not result["picks_differ"] and not result["installer_differs"]
    assert set(_flat(result).values()) == {"same"}
    assert [page["status"] for page in result["pages"]] == ["same"]
    assert cc.describe(result) == "The choices are identical."


def test_differing_picks_say_which_side_picked_them():
    other = [("Main", [("Textures", [_opt("1K"), _opt("2K", True)]), ("Extras", [_opt("Snow", True), _opt("Fog", True)])])]
    result = cc.compare_installs(_event(_tree(*BASE)), _event(_tree(*other)))
    assert _flat(result) == {("Main", "Textures", "1K"): "picked-a", ("Main", "Textures", "2K"): "picked-b",
                             ("Main", "Extras", "Snow"): "picked-b", ("Main", "Extras", "Fog"): "same"}
    assert result["status"] == "different" and result["picks_differ"] and not result["installer_differs"]
    assert result["counts"]["picks"] == 3
    textures = result["pages"][0]["groups"][0]
    assert textures["status"] == "different" and result["pages"][0]["status"] == "different"
    assert [o["picked_a"] for o in textures["options"]] == [True, False]
    assert [o["picked_b"] for o in textures["options"]] == [False, True]
    assert cc.describe(result).startswith("3 picks differ (1 only in A, 2 only in B)")


def test_options_only_one_installer_offers_are_not_pick_differences():
    newer = [("Main", [("Textures", [_opt("1K", True), _opt("2K"), _opt("4K", True)]),
                       ("Extras", [_opt("Snow")])])]
    result = cc.compare_installs(_event(_tree(*BASE)), _event(_tree(*newer)))
    flat = _flat(result)
    assert flat[("Main", "Textures", "4K")] == "offered-b"
    assert flat[("Main", "Extras", "Fog")] == "offered-a"          # only A's installer has Fog
    assert flat[("Main", "Textures", "1K")] == "same"
    assert result["counts"]["picks"] == 0 and result["counts"]["offered"] == 2
    assert result["installer_differs"] and not result["picks_differ"]
    fog = next(o for g in result["pages"][0]["groups"] for o in g["options"] if o["name"] == "Fog")
    assert (fog["picked_a"], fog["picked_b"]) == (True, None)       # picked in A, not offered in B
    assert "2 options only in one installer" in cc.describe(result)


def test_a_reworded_option_is_renamed_and_a_moved_one_is_still_the_same_option():
    reworded = [("Main", [("Textures", [_opt("1K", True), _opt("2K")]), ("Extras", [_opt("Snow"), _opt("Fog (light)", True)])])]
    result = cc.compare_installs(_event(_tree(*BASE)), _event(_tree(*reworded)))
    flat = _flat(result)
    assert flat[("Main", "Extras", "Fog (light)")] == "renamed" and ("Main", "Extras", "Fog") not in flat
    renamed = next(o for g in result["pages"][0]["groups"] for o in g["options"] if o["status"] == "renamed")
    assert (renamed["name_a"], renamed["name_b"]) == ("Fog", "Fog (light)")
    assert result["counts"]["renamed"] == 1 and result["installer_differs"] and not result["picks_differ"]

    moved = [("Main", [("Textures", [_opt("1K", True), _opt("2K"), _opt("Fog", False)]), ("Extras", [_opt("Snow")])])]
    result = cc.compare_installs(_event(_tree(*BASE)), _event(_tree(*moved)))
    assert _flat(result)[("Main", "Textures", "Fog")] == "picked-a"   # compared with A's Fog, wherever it sits
    assert result["counts"]["offered"] == 0


def test_a_page_only_one_installer_has_and_a_page_skipped_on_one_side():
    extra = [BASE[0], ("Patches", [("Patch list", [_opt("Falskaar patch", True)])])]
    result = cc.compare_installs(_event(_tree(*BASE)), _event(_tree(*extra)))
    pages = {page["name"]: page for page in result["pages"]}
    assert pages["Patches"]["status"] == "only-b" and pages["Main"]["status"] == "same"
    assert _flat(result)[("Patches", "Patch list", "Falskaar patch")] == "offered-b"
    assert result["counts"]["pages"] == 1 and result["installer_differs"]

    skipped = [("Main", BASE[0][1], False)]
    result = cc.compare_installs(_event(_tree(*BASE)), _event(_tree(*skipped)))
    assert result["pages"][0]["status"] == "skipped-b" and result["pages"][0]["skipped_b"]
    # A skipped page picks nothing, so its options are not counted as pick differences.
    assert set(_flat(result).values()) == {"skipped"} and result["counts"]["picks"] == 0
    assert result["status"] == "different" and "1 page differ" in cc.describe(result)
    both = cc.compare_installs(_event(_tree(*skipped)), _event(_tree(*skipped)))
    assert both["status"] == "same" and both["pages"][0]["status"] == "same"


def test_name_only_trees_without_ids_compare_too():
    left = {"mod": {"name": "X"}, "steps": [{"name": "P", "groups": [{"name": "G", "options": [
        {"name": "One", "selected": True}, {"name": "Two"}]}]}]}
    right = deepcopy(left)
    right["steps"][0]["groups"][0]["options"][1]["selected"] = True
    result = cc.compare_installs(left, right)
    assert _flat(result) == {("P", "G", "One"): "same", ("P", "G", "Two"): "picked-b"}


def test_compare_events_pairs_are_opt_in():
    old, new = _event(_tree(*BASE)), _event(_tree(*BASE))
    assert "matched" not in compare_events(old, new)
    assert len(compare_events(old, new, pairs=True)["matched"]) == 4


# ---------- a mod's installs ----------

def test_installs_of_a_mod_are_newest_first_and_mods_with_history_need_two():
    one = _event(_tree(*BASE), day=1, version="1.0")
    two = _event(_tree(*BASE), day=5, version="1.1")
    gone = _event(_tree(*BASE), name="Gone Mod", day=2)
    gone2 = _event(_tree(*BASE), name="Gone Mod", day=3, removed=True)
    single = _event(_tree(*BASE), name="Single", day=4)
    other = _event(_tree(*BASE), instance=B_ID, day=6)
    events = [two, one, gone, gone2, single, other]
    assert [e["mod"]["version"] for e in cc.installs_of(events, A_ID, "majestic MOUNTAINS")] == ["1.1", "1.0"]
    found = cc.mods_with_history(events, A_ID)
    assert [mod["name"] for mod in found] == ["Majestic Mountains"]           # Gone Mod's newest is removed
    assert [e["event_id"] for e in found[0]["installs"]] == [two["event_id"], one["event_id"]]


# ---------- duplicates ----------

def test_copies_with_the_same_archive_or_nexus_id_but_different_names_are_found():
    dark = _event(_tree(*BASE), name="Majestic Mountains - Dark")
    light = _event(_tree(*BASE), name="Majestic Mountains - Light")
    unrelated = _event(_tree(*BASE), name="Skyrim Landscapes", archive="sl.7z", nexus=99,
                       )
    found = cc.find_duplicates([dark, light, unrelated])
    assert len(found) == 1
    assert [e["mod"]["name"] for e in found[0]["mods"]] == ["Majestic Mountains - Dark", "Majestic Mountains - Light"]
    assert found[0]["why"] == "same archive and Nexus ID" and found[0]["instance_id"] == A_ID

    by_archive = cc.find_duplicates([_event(_tree(*BASE), name="One", archive="same.7z", nexus=1),
                                     _event(_tree(*BASE), name="Two", archive="SAME.7z", nexus=2)])
    assert by_archive[0]["why"] == "same archive"
    by_nexus = cc.find_duplicates([_event(_tree(*BASE), name="One", archive="a.7z", nexus=7),
                                   _event(_tree(*BASE), name="Two", archive="b.7z", nexus=7)])
    assert by_nexus[0]["why"] == "same Nexus ID"


def test_duplicates_must_be_live_in_one_instance_and_nexus_copies_need_a_shared_option():
    live = _event(_tree(*BASE), name="Dark")
    removed = _event(_tree(*BASE), name="Light", removed=True)
    hidden = _event(_tree(*BASE), name="Hidden", hidden=True)
    elsewhere = _event(_tree(*BASE), name="Elsewhere", instance=B_ID)
    assert cc.find_duplicates([live, removed, hidden, elsewhere]) == []
    assert len(cc.find_duplicates([live, hidden], include_hidden=True)) == 1
    # The same Nexus ID but installers with nothing in common (another game, or another file of the page).
    foreign = _event(_tree(("Other", [("Thing", [_opt("Unrelated")])])), name="Foreign", archive="f.7z", nexus=1234)
    assert cc.find_duplicates([_event(_tree(*BASE), name="Dark", archive="d.7z"), foreign]) == []
    # An older install of a copy doesn't make it a second copy: only each mod's newest counts.
    old_dark = _event(_tree(*BASE), name="Dark", day=1)
    new_dark = _event(_tree(*BASE), name="Dark", day=9)
    assert cc.find_duplicates([old_dark, new_dark]) == []


# ---------- two instances ----------

def _pair_of_instances():
    a = [_event(_tree(*BASE), name="Majestic Mountains", instance=A_ID),
         _event(_tree(("Main", [("Preset", [_opt("Default", True), _opt("Dark")])])), name="Weather", instance=A_ID,
                archive="w-5-1.7z", nexus=5),
         _event(_tree(*BASE), name="Only Here", instance=A_ID, archive="oh.7z", nexus=11)]
    different = [("Main", [("Textures", [_opt("1K"), _opt("2K", True)]), ("Extras", [_opt("Snow"), _opt("Fog", True)])])]
    b = [_event(_tree(*different), name="Majestic Mountains", instance=B_ID),
         _event(_tree(("Main", [("Preset", [_opt("Default", True), _opt("Dark")])])), name="Weather (renamed)",
                instance=B_ID, archive="w-5-1.7z", nexus=5),
         _event(_tree(*BASE), name="Only There", instance=B_ID, archive="ot.7z", nexus=12)]
    return a, b


def test_two_instances_pair_by_nexus_id_then_archive_then_name_and_sort_the_differences_first():
    a, b = _pair_of_instances()
    result = cc.compare_instances(a + b, A_ID, B_ID)
    assert result["counts"] == {"different": 1, "only_a": 1, "only_b": 1, "same": 1, "mods": 4}
    assert [(m["status"], m["name"]) for m in result["mods"]] == [
        ("different", "Majestic Mountains"), ("only-a", "Only Here"), ("only-b", "Only There"),
        ("same", "Weather  ↔  Weather (renamed)")]
    paired = {m["name"]: m["paired_by"] for m in result["mods"]}
    assert paired["Majestic Mountains"] == "nexus" and paired["Weather  ↔  Weather (renamed)"] == "nexus"
    assert result["mods"][0]["detail"]["counts"]["picks"] == 2
    assert result["a"] == {"id": A_ID, "label": "Main"} and result["b"] == {"id": B_ID, "label": "Second"}
    assert cc.describe_instances(result) == "4 mods: 1 with different picks, 1 only in A, 1 only in B, 1 identical."


def test_pairing_falls_back_to_the_archive_and_then_the_name():
    left = [_event(_tree(*BASE), name="By Archive", archive="x.7z", nexus=0),
            _event(_tree(*BASE), name="By Name", archive="n1.7z", nexus=0)]
    right = [_event(_tree(*BASE), name="Renamed", archive="x.7z", nexus=0, instance=B_ID),
             _event(_tree(*BASE), name="by name", archive="n2.7z", nexus=0, instance=B_ID)]
    how = {(a["mod"]["name"], b["mod"]["name"]): way for a, b, way in cc.pair_mods(left, right)}
    assert how == {("By Archive", "Renamed"): "archive", ("By Name", "by name"): "name"}


def test_a_nexus_id_shared_by_unrelated_mods_does_not_pair_them():
    left = [_event(_tree(*BASE), name="Skyrim Thing", nexus=500, archive="s.7z")]
    right = [_event(_tree(("Other", [("Group", [_opt("Different")])])), name="Fallout Thing", nexus=500,
                    archive="f.7z", instance=B_ID)]
    pairs = cc.pair_mods(left, right)
    assert [(a is not None, b is not None) for a, b, _ in pairs] == [(True, False), (False, True)]


def test_several_copies_with_one_nexus_id_pair_by_name_then_archive():
    left = [_event(_tree(*BASE), name="Dark", archive="d.7z"), _event(_tree(*BASE), name="Light", archive="l.7z")]
    right = [_event(_tree(*BASE), name="Light", archive="l.7z", instance=B_ID),
             _event(_tree(*BASE), name="Dark", archive="d.7z", instance=B_ID)]
    pairs = cc.pair_mods(left, right)
    assert sorted((a["mod"]["name"], b["mod"]["name"]) for a, b, _ in pairs) == [("Dark", "Dark"), ("Light", "Light")]
    # Renamed on one side: the archive still says which is which.
    right = [_event(_tree(*BASE), name="Light 2", archive="l.7z", instance=B_ID),
             _event(_tree(*BASE), name="Dark 2", archive="d.7z", instance=B_ID)]
    pairs = cc.pair_mods(left, right)
    assert sorted((a["mod"]["name"], b["mod"]["name"]) for a, b, _ in pairs) == [("Dark", "Dark 2"), ("Light", "Light 2")]


def test_a_mod_removed_or_replaced_in_an_instance_uses_its_newest_live_install():
    old = _event(_tree(*BASE), name="Majestic Mountains", day=1)
    new = _event(_tree(("Main", [("Textures", [_opt("1K"), _opt("2K", True)])])), name="Majestic Mountains", day=5)
    removed = _event(_tree(*BASE), name="Gone", day=2, removed=True)
    b_side = _event(_tree(*BASE), name="Majestic Mountains", instance=B_ID)
    result = cc.compare_instances([old, new, removed, b_side], A_ID, B_ID)
    assert [m["name"] for m in result["mods"]] == ["Majestic Mountains"]
    assert result["mods"][0]["a"]["event_id"] == new["event_id"]


def test_an_empty_instance_has_nothing_to_compare():
    result = cc.compare_instances([], A_ID, B_ID)
    assert result["counts"]["mods"] == 0
    assert cc.describe_instances(result) == "Neither instance has a logged mod to compare."


# ---------- profiles ----------

def _profile(root, name, text, *, raw=None):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "modlist.txt").write_bytes(raw if raw is not None else text.encode("utf-8"))


def test_modlist_lines_enabled_disabled_and_unmanaged():
    text = "# This file was automatically generated by Mod Organizer.\n+Alpha\n-Beta\n*DLC\n+Gamma Mod\r\n\n+\nnonsense\n"
    assert cc.parse_modlist(text) == {"alpha", "gamma mod"}


def test_each_profile_that_has_a_mod_enabled_is_listed(tmp_path):
    _profile(tmp_path, "Main", "# x\n+Majestic Mountains\n+Lux\n-Weather\n")
    _profile(tmp_path, "Test", "+majestic mountains\n-Lux\n+Weather\n")
    _profile(tmp_path, "Empty", "")
    (tmp_path / "not a profile").mkdir()              # no modlist.txt
    (tmp_path / "stray.txt").write_text("+Majestic Mountains", encoding="utf-8")
    result = cc.read_profiles(tmp_path)
    assert result["profiles"] == ["Empty", "Main", "Test"]
    assert cc.profiles_of(result, "Majestic Mountains") == ["Main", "Test"]
    assert cc.profiles_of(result, "LUX") == ["Main"]
    assert cc.profiles_of(result, "Weather") == ["Test"]
    assert cc.profiles_of(result, "Nowhere") == []                         # in no profile
    assert cc.profiles_of(None, "Lux") is None and cc.profiles_of({"profiles": [], "mods": {}}, "Lux") is None


def test_missing_and_odd_profile_files_are_tolerated_and_nothing_is_written(tmp_path):
    assert cc.read_profiles(tmp_path / "nowhere")["profiles"] == []
    assert cc.read_profiles(None)["profiles"] == [] and cc.read_profiles("")["profiles"] == []
    _profile(tmp_path, "Bom", "", raw=b"\xef\xbb\xbf+First\n+Second\n")
    _profile(tmp_path, "Latin", "", raw="+Caf\xe9\n+Plain\n".encode("latin-1"))   # not UTF-8
    _profile(tmp_path, "Binary", "", raw=bytes(range(256)))
    (tmp_path / "Folder").mkdir()
    (tmp_path / "Folder" / "modlist.txt").mkdir()                       # a directory where the file should be
    before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}
    sizes = {path: path.read_bytes() for path in before}
    result = cc.read_profiles(tmp_path)
    assert "Bom" in result["profiles"] and cc.profiles_of(result, "First") == ["Bom"]
    assert cc.profiles_of(result, "Plain") == ["Latin"]
    assert "Folder" not in result["profiles"]
    assert {path: path.stat().st_mtime_ns for path in before} == before
    assert {path: path.read_bytes() for path in before} == sizes
    assert sorted(path.name for path in tmp_path.iterdir()) == ["Binary", "Bom", "Folder", "Latin"]


def test_a_changed_modlist_is_read_again_and_an_unchanged_one_is_not(tmp_path, monkeypatch):
    _profile(tmp_path, "Main", "+One\n")
    assert cc.profiles_of(cc.read_profiles(tmp_path), "One") == ["Main"]
    reads = []
    original = cc.Path.read_bytes
    monkeypatch.setattr(cc.Path, "read_bytes", lambda self: reads.append(self) or original(self))
    cc.read_profiles(tmp_path)
    assert reads == []                                          # same size and time: the cached parse
    _profile(tmp_path, "Main", "+One\n+Twotwo\n")
    assert cc.profiles_of(cc.read_profiles(tmp_path), "Twotwo") == ["Main"] and len(reads) == 1


@pytest.mark.parametrize("name", ["+Plus\n", "-Minus\n"])
def test_a_profile_with_only_disabled_mods_still_counts_as_a_profile(tmp_path, name):
    _profile(tmp_path, "Solo", name)
    result = cc.read_profiles(tmp_path)
    assert result["profiles"] == ["Solo"]
    assert cc.profiles_of(result, "Minus") == []
