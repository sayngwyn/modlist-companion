"""Which installer option put this file here: the lookup, its rules, and the recorded `files` data it reads."""

from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from fomod_logger_support.core import file_origin
from fomod_logger_support.core.file_origin import FileIndex, looks_like_path, normalise_query
from fomod_logger_support.core.storage import EventStore, EventValidationError, validate_event

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "native" / "fixtures"

_ids = iter(range(1, 10_000))


def entry(source: str, destination: str | None = None, *, folder: bool = False, priority: int = 0,
          order: int = 0, resolved: str | None = None) -> dict:
    """A recorded <file>/<folder> entry, resolved the way the native side does (enough for these tests)."""

    destination = source if destination is None else destination
    path = resolved if resolved is not None else destination.replace("\\", "/").strip("/")
    out = {"source": source, "destination": destination, "resolved": path, "kind": "folder" if folder else "file",
           "order": order}
    if priority:
        out["priority"] = priority
    return out


def opt(name: str, picked: bool = False, files=None, *, plugins=None, truncated: bool = False) -> dict:
    option = {"name": name, "selected": picked, "enabled": True}
    if files is not None:
        option["files"] = files
    if plugins is not None:
        option["plugins"] = plugins
    if truncated:
        option["files_truncated"] = True
    return option


def make(base: dict, mod: str, groups: list[tuple[str, list[dict]]], *, event_id: str | None = None,
         when: str = "2026-09-01T12:00:00+00:00", auto=None, page: str = "Textures", visible: bool = True,
         instance: str | None = None) -> dict:
    event = deepcopy(base)
    number = next(_ids)
    event["event_id"] = event_id or f"{number:08x}-0000-4000-8000-000000000000"
    event["completed_at"] = when
    event["mod"]["name"] = mod
    if instance:
        event["instance"]["id"] = instance
    built = []
    for g, (group_name, options) in enumerate(groups):
        built.append({"id": f"step:0/group:{g}", "name": group_name, "type": "SelectAny",
                      "options": [{"id": f"step:0/group:{g}/option:{o}", **option}
                                  for o, option in enumerate(options)]})
    event["steps"] = [{"id": "step:0", "name": page, "visible": visible, "groups": built}]
    if auto is not None:
        event["automatic_installs"] = auto
    return event


@pytest.fixture
def mountains(installation_event):
    return mountains_event(installation_event)


def mountains_event(installation_event):
    """Majestic Mountains: a texture resolution page (2K picked) and a patch that overwrites one texture."""

    return make(installation_event, "Majestic Mountains", [
        ("Texture resolution", [
            opt("1K", False, [entry("1K\\Textures", "Textures", folder=True, order=1)]),
            opt("2K", True, [entry("2K\\Textures", "Textures", folder=True, order=2),
                              entry("2K\\Majestic Mountains.esp", "", resolved="Majestic Mountains.esp", order=3)],
                plugins=["Majestic Mountains.esp"]),
            opt("4K", False, [entry("4K\\Textures", "Textures", folder=True, order=4)]),
        ]),
        ("Extras", [
            opt("Snow Peak Fix", True, [entry("Fix\\peak.dds", "Textures\\Landscape\\Mountains\\Peak.dds", order=5,
                                                priority=3)]),
            opt("Darker Peaks", True, [entry("Dark\\peak.dds", "Textures\\Landscape\\Mountains\\Peak.dds", order=6)]),
        ]),
    ])


def origin(event_or_events, query: str, **kwargs) -> dict:
    events = event_or_events if isinstance(event_or_events, list) else [event_or_events]
    return FileIndex(events).lookup(query, **kwargs)


def labels(result: dict) -> list[str]:
    return [match["text"] for match in result["matches"]]


# ---------- matching ----------

def test_an_exact_file_destination_names_its_option(mountains):
    result = origin(mountains, "Textures\\Landscape\\Mountains\\Peak.dds")
    exact = [m for m in result["matches"] if m["how"] == "file"]
    assert {m["option"] for m in exact} == {"Snow Peak Fix", "Darker Peaks"}
    assert all(m["path"] == "Textures/Landscape/Mountains/Peak.dds" for m in exact)
    assert result["matches"][0]["mod_name"] == "Majestic Mountains"
    # The resolution folders land on Textures/ too, so they may hold it; they come after the exact answers.
    assert [m["how"] for m in result["matches"]] == ["file", "file", "folder", "folder", "folder"]


def test_a_file_under_a_folder_entry_names_every_option_that_installs_the_folder(mountains):
    result = origin(mountains, "textures/landscape/mountains/rock.dds")
    by_option = {m["option"]: m for m in result["matches"]}
    assert set(by_option) == {"1K", "2K", "4K"}              # each of the resolution folders lands on Textures/
    assert all(m["how"] == "folder" and m["folder"] for m in by_option.values())
    assert by_option["2K"]["picked"] is True and by_option["1K"]["picked"] is False
    assert "Majestic Mountains › Textures › Texture resolution › 2K (picked)" in labels(result)
    assert "Majestic Mountains › Textures › Texture resolution › 1K (not picked)" in labels(result)


def test_a_folder_does_not_match_a_path_next_to_it(mountains):
    assert origin(mountains, "TexturesExtra/rock.dds")["matches"] == []
    assert origin(mountains, "Meshes/rock.nif")["matches"] == []


def test_matching_ignores_case_slash_kind_quotes_and_a_leading_data_folder(mountains):
    for query in ("TEXTURES/LANDSCAPE/MOUNTAINS/PEAK.DDS", '  "textures\\landscape\\mountains\\peak.dds" ',
                  "Data\\Textures\\Landscape\\Mountains\\Peak.dds", "/textures/landscape/mountains/peak.dds"):
        found = {m["option"] for m in origin(mountains, query)["matches"] if m["how"] == "file"}
        assert found == {"Snow Peak Fix", "Darker Peaks"}, query


def test_a_path_pasted_from_the_mods_folder_loses_the_folder_of_the_mod(mountains):
    pasted = r"C:\Modding\Skyrim\mods\Majestic Mountains\Textures\Landscape\Mountains\Peak.dds"
    assert normalise_query(pasted) == "Textures/Landscape/Mountains/Peak.dds"
    assert {m["option"] for m in origin(mountains, pasted)["matches"] if m["how"] == "file"} == {
        "Snow Peak Fix", "Darker Peaks"}


def test_a_bare_file_name_matches_the_file_wherever_it_lands(mountains):
    result = origin(mountains, "peak.dds")
    assert {m["option"] for m in result["matches"]} == {"Snow Peak Fix", "Darker Peaks"}


def test_a_folder_query_finds_the_entries_that_install_inside_it(mountains):
    result = origin(mountains, "Textures/Landscape")
    inside = {m["option"] for m in result["matches"] if m["how"] == "inside"}
    assert inside == {"Snow Peak Fix", "Darker Peaks"}
    assert all(not m["winner"] for m in result["matches"] if m["how"] == "inside")   # different files: no contest
    folder = {m["option"] for m in result["matches"] if m["how"] == "folder"}
    assert folder == {"1K", "2K", "4K"}


def test_a_plugin_is_found_in_the_plugins_an_option_lists(mountains):
    result = origin(mountains, "majestic mountains.ESP")
    assert [m["option"] for m in result["matches"]] == ["2K"]
    assert result["matches"][0]["picked"] is True


def test_a_plugin_and_the_file_entry_that_installs_it_are_one_answer(mountains):
    result = origin(mountains, "Majestic Mountains.esp")
    assert len(result["matches"]) == 1
    assert result["matches"][0]["how"] == "file"          # the file entry explains the plugin
    assert result["matches"][0]["source"] == "2K\\Majestic Mountains.esp"


def test_something_no_installer_installs_is_not_found_and_says_so_plainly(mountains):
    result = origin(mountains, "Meshes/Nothing/here.nif")
    assert result["matches"] == []
    assert result["unrecorded"] == []
    assert origin(mountains, "")["matches"] == []
    assert origin(mountains, "   ")["matches"] == []


def test_a_folder_that_installs_into_the_mod_folder_does_not_claim_every_file(installation_event):
    event = make(installation_event, "Whole Folder", [
        ("Main", [opt("Everything", True, [entry("Data", "", folder=True, order=1)])])])
    result = origin(event, "Meshes/anything.nif")
    assert result["matches"] == []
    assert any("whole folder" in note for note in result["notes"])


# ---------- priority and overwrites ----------

def test_a_higher_priority_entry_wins_even_when_it_comes_first(mountains):
    result = origin(mountains, "Textures/Landscape/Mountains/Peak.dds")
    by_option = {m["option"]: m for m in result["matches"]}
    assert by_option["Snow Peak Fix"]["winner"] is True        # priority 3 beats the later Darker Peaks (0)
    assert by_option["Darker Peaks"]["winner"] is False
    assert by_option["Darker Peaks"]["overwritten_by"] == [by_option["Snow Peak Fix"]["label"]]
    assert by_option["Snow Peak Fix"]["overwritten_by"] == []
    assert by_option["Snow Peak Fix"]["contested"] and by_option["Darker Peaks"]["contested"]
    assert "likely winner" in by_option["Snow Peak Fix"]["detail"]
    assert "overwritten by" in by_option["Darker Peaks"]["detail"]


def test_equal_priority_is_won_by_the_later_entry_in_the_installer_file(installation_event):
    event = make(installation_event, "Ties", [("Main", [
        opt("First", True, [entry("a.dds", "tex/a.dds", order=10)]),
        opt("Second", True, [entry("b.dds", "tex/a.dds", order=11)]),
        opt("Third", True, [entry("c.dds", "tex/a.dds", order=12, priority=-1)]),
    ])])
    by_option = {m["option"]: m for m in origin(event, "tex/a.dds")["matches"]}
    assert [name for name, m in by_option.items() if m["winner"]] == ["Second"]
    assert set(by_option["First"]["overwritten_by"]) == {by_option["Second"]["label"]}


def test_the_document_position_decides_not_the_order_the_options_are_shown(installation_event):
    # A page shown in a different order than the XML (Order="Descending"): `order` still says who is later.
    event = make(installation_event, "Shuffled", [("Main", [
        opt("Shown first", True, [entry("a.dds", "tex/a.dds", order=30)]),
        opt("Shown second", True, [entry("b.dds", "tex/a.dds", order=20)]),
    ])])
    winner = [m["option"] for m in origin(event, "tex/a.dds")["matches"] if m["winner"]]
    assert winner == ["Shown first"]


def test_an_option_that_is_not_picked_never_wins_or_overwrites(mountains):
    mountains["steps"][0]["groups"][1]["options"][0]["selected"] = False       # Snow Peak Fix not picked now
    result = origin(mountains, "Textures/Landscape/Mountains/Peak.dds")
    by_option = {m["option"]: m for m in result["matches"]}
    assert by_option["Darker Peaks"]["winner"] is True and by_option["Darker Peaks"]["overwritten_by"] == []
    assert by_option["Snow Peak Fix"]["winner"] is False
    assert by_option["Snow Peak Fix"]["later"] == []
    # The unpicked fix would overwrite it if it were picked, but it is not what is installed.
    assert by_option["Darker Peaks"]["later"] == [by_option["Snow Peak Fix"]["label"]]
    assert "(not picked)" in by_option["Snow Peak Fix"]["text"]


def test_an_unpicked_option_says_a_later_entry_would_overwrite_it(installation_event):
    event = make(installation_event, "Unpicked", [("Main", [
        opt("Early", False, [entry("a.dds", "tex/a.dds", order=1)]),
        opt("Late", True, [entry("b.dds", "tex/a.dds", order=2)]),
    ])])
    by_option = {m["option"]: m for m in origin(event, "tex/a.dds")["matches"]}
    assert by_option["Late"]["winner"] is True
    assert by_option["Early"]["later"] == [by_option["Late"]["label"]] and by_option["Early"]["overwritten_by"] == []
    assert "later entry also writes it" in by_option["Early"]["detail"]


def test_a_folder_and_a_file_in_the_same_option_are_one_answer_by_the_better_ranked_entry(installation_event):
    event = make(installation_event, "Both", [("Main", [
        opt("Combo", True, [entry("Textures", "Textures", folder=True, order=1),
                             entry("x.dds", "Textures/x.dds", order=2)])])])
    result = origin(event, "Textures/x.dds")
    assert len(result["matches"]) == 1 and result["matches"][0]["how"] == "file"


def test_automatic_installs_are_named_and_win_by_the_same_rule(installation_event):
    event = make(installation_event, "Auto", [("Main", [opt("Main", True, [entry("m.dds", "tex/a.dds", order=1)])])],
                 auto=[{"id": "conditional:0", "installed": True, "plugins": ["Auto Patch.esp"], "detects": [],
                        "files": [entry("p.dds", "tex/a.dds", order=9),
                                  entry("Auto Patch.esp", "", resolved="Auto Patch.esp", order=10)]},
                       {"id": "conditional:1", "installed": False, "plugins": [], "detects": [],
                        "files": [entry("q.dds", "tex/q.dds", order=11)]}])
    result = origin(event, "tex/a.dds")
    by_option = {m["option"]: m for m in result["matches"]}
    assert by_option["Automatic install 1"]["automatic"] and by_option["Automatic install 1"]["winner"] is True
    assert by_option["Automatic install 1"]["text"].endswith("(applied automatically)")
    assert by_option["Main"]["overwritten_by"] == [by_option["Automatic install 1"]["label"]]
    skipped = origin(event, "tex/q.dds")["matches"]
    assert skipped[0]["text"].endswith("(not applied)") and skipped[0]["winner"] is False
    assert [m["option"] for m in origin(event, "Auto Patch.esp")["matches"]] == ["Automatic install 1"]


def test_a_skipped_page_is_said_in_the_answer(installation_event):
    event = make(installation_event, "Hidden page", [("Main", [opt("Dev", False, [entry("a", "tex/a.dds")])])],
                 visible=False)
    assert "its page was skipped" in origin(event, "tex/a.dds")["matches"][0]["text"]


def test_two_picked_plugins_are_ranked_by_the_entry_that_installs_them(installation_event):
    event = make(installation_event, "Plugins", [("Main", [
        opt("A", True, [entry("A\\x.esp", "", resolved="x.esp", order=1)], plugins=["x.esp"]),
        opt("B", True, [entry("B\\x.esp", "", resolved="x.esp", order=2, priority=1)], plugins=["x.esp"]),
    ])])
    assert len(origin(event, "x.esp")["matches"]) == 2     # same file entry, two options
    by_option = {m["option"]: m for m in origin(event, "x.esp")["matches"]}
    assert by_option["B"]["winner"] is True and by_option["A"]["overwritten_by"] == [by_option["B"]["label"]]


# ---------- installs without file data, truncation, and which installs count ----------

def test_a_name_only_install_answers_plugin_names_and_says_files_are_not_recorded(installation_event):
    old = make(installation_event, "Old Mod", [("Main", [opt("Core", True, plugins=["Old Core.esp"])])])
    result = origin(old, "Old Core.esp")
    assert [m["option"] for m in result["matches"]] == ["Core"]
    assert result["matches"][0]["recorded"] is False
    assert "file data isn't recorded for this install" in result["matches"][0]["detail"]
    assert result["unrecorded"] == ["Old Mod"]
    assert any("only plugin names were checked" in note for note in result["notes"])
    missing = origin(old, "Textures/a.dds")
    assert missing["matches"] == [] and missing["unrecorded"] == ["Old Mod"]


def test_a_plugin_match_in_a_name_only_install_has_no_winner(installation_event):
    old = make(installation_event, "Old Mod", [("Main", [opt("A", True, plugins=["x.esp"]),
                                                        opt("B", True, plugins=["x.esp"])])])
    matches = origin(old, "x.esp")["matches"]
    assert len(matches) == 2 and not any(m["winner"] for m in matches)


def test_a_recorded_install_with_empty_lists_is_still_recorded(installation_event):
    event = make(installation_event, "Empty", [("Main", [opt("None", True, [])])])
    assert file_origin.has_file_data(event)
    assert origin(event, "a.dds")["unrecorded"] == []
    assert not file_origin.has_file_data(make(installation_event, "Plain", [("Main", [opt("x", True)])]))


def test_a_truncated_list_is_said_to_be_incomplete(installation_event):
    event = make(installation_event, "Huge Pack", [("Main", [
        opt("Everything", True, [entry("a.dds", "tex/a.dds")], truncated=True)])])
    result = origin(event, "tex/zzz.dds")
    assert result["matches"] == [] and result["truncated"] == ["Huge Pack"]
    assert any("cut short" in note for note in result["notes"])
    assert "cut short" in origin(event, "tex/a.dds")["matches"][0]["detail"]


def test_only_the_wanted_installs_are_searched(mountains, installation_event):
    newer = deepcopy(mountains)
    newer["event_id"] = "99999999-0000-4000-8000-000000000000"
    newer["completed_at"] = "2026-10-01T12:00:00+00:00"
    newer["steps"][0]["groups"][1]["options"].pop(0)           # the newer install no longer has Snow Peak Fix
    index = FileIndex([mountains, newer])
    everything = index.lookup("Textures/Landscape/Mountains/Peak.dds")
    assert {m["event_id"] for m in everything["matches"]} == {mountains["event_id"], newer["event_id"]}
    only_newer = index.lookup("Textures/Landscape/Mountains/Peak.dds", {newer["event_id"]})
    assert {m["option"] for m in only_newer["matches"] if m["how"] == "file"} == {"Darker Peaks"}
    assert only_newer["matches"][0]["event_id"] == newer["event_id"]
    # Newest install first when several are asked.
    assert everything["matches"][0]["event_id"] == newer["event_id"]


def test_a_match_carries_what_the_window_needs_to_find_the_option(mountains):
    match = next(m for m in origin(mountains, "Majestic Mountains.esp")["matches"])
    assert match["event_id"] == mountains["event_id"]
    assert match["option_id"] == "step:0/group:0/option:1"
    assert match["step"] == "Textures" and match["group"] == "Texture resolution" and match["option"] == "2K"
    assert match["instance_id"] == mountains["instance"]["id"]


# ---------- what looks like a path ----------

@pytest.mark.parametrize("query,expected", [
    ("Textures\\Foo\\bar.dds", True), ("textures/foo", True), ("Foo.esp", True), ("FOO.ESM", True),
    ("bar.dds", True), ("CBBE", False), ("Lux Via", False), ("patch", False), ("v1.2", False),
    ("", False), ("   ", False), (".esp", False), ("1.5", False), ("notes.", False), ("Dr. Who", False),
])
def test_what_looks_like_a_path(query, expected):
    assert looks_like_path(query) is expected


# ---------- the store ----------

def _store(tmp_path, *events):
    database = tmp_path / "logger.sqlite3"
    store = EventStore(database)
    for event in events:
        store.ingest(event)
    return store


def test_the_store_finds_installs_by_a_file_path_in_search_and_history(tmp_path, mountains, installation_event):
    other = make(installation_event, "Unrelated", [("Main", [opt("x", True, [entry("a", "Meshes/a.nif")])])])
    store = _store(tmp_path, mountains, other)
    with store:
        assert [e["mod"]["name"] for e in store.latest("Textures\\Landscape\\Mountains\\Peak.dds")] == [
            "Majestic Mountains"]
        assert [e["mod"]["name"] for e in store.history("Majestic Mountains.esp")] == ["Majestic Mountains"]
        assert [e["mod"]["name"] for e in store.latest("meshes/a.nif")] == ["Unrelated"]
        assert store.latest("Meshes/none.nif") == []
        # A word still searches names, as before.
        assert [e["mod"]["name"] for e in store.latest("unrelated")] == ["Unrelated"]


def test_the_store_builds_the_file_index_only_when_a_path_is_asked_and_only_once(tmp_path, mountains, monkeypatch):
    built = []
    real = FileIndex.__init__

    def counting(self, events):
        built.append(1)
        real(self, events)

    monkeypatch.setattr(FileIndex, "__init__", counting)
    with _store(tmp_path, mountains) as store:
        store.latest("majestic")
        store.latest("anything")
        assert built == []
        store.latest("a/b.dds")
        store.latest("Majestic Mountains.esp")
        store.file_origins("Textures/x.dds", current=True)
        assert built == [1]


def test_current_installs_only_are_answered_for_the_dialog(tmp_path, mountains):
    newer = deepcopy(mountains)
    newer["event_id"] = "99999999-0000-4000-8000-000000000000"
    newer["completed_at"] = "2026-10-01T12:00:00+00:00"
    newer["steps"][0]["groups"][0]["options"][1]["selected"] = False
    newer["steps"][0]["groups"][0]["options"][2]["selected"] = True       # 4K now
    with _store(tmp_path, mountains, newer) as store:
        answer = store.file_origins("Majestic Mountains.esp", current=True)
        assert {m["event_id"] for m in answer["matches"]} == {newer["event_id"]}
        assert answer["matches"][0]["picked"] is False
        instance = mountains["instance"]["id"]
        assert store.file_origins("Majestic Mountains.esp", current=True, instance_id=instance)["matches"]
        assert store.file_origins("Majestic Mountains.esp", current=True, instance_id="b" * 64)["matches"] == []


def test_the_index_follows_changes_to_the_events(tmp_path, mountains, installation_event):
    with _store(tmp_path, mountains) as store:
        assert store.file_origins("Meshes/a.nif")["matches"] == []
        store.ingest(make(installation_event, "Late", [("Main", [opt("x", True, [entry("a", "Meshes/a.nif")])])]))
        assert [m["mod_name"] for m in store.file_origins("Meshes/a.nif")["matches"]] == ["Late"]


# ---------- the recorded data ----------

def test_events_with_and_without_files_validate(mountains, installation_event):
    validate_event(mountains)
    validate_event(installation_event)         # an event from before files were recorded


@pytest.mark.parametrize("change", [
    lambda o: o.update(files="textures"),
    lambda o: o.update(files=[{"source": "a"}]),
    lambda o: o.update(files=[{"source": "a", "destination": "a", "resolved": "a", "kind": "link"}]),
    lambda o: o.update(files=[{"source": "a", "destination": "a", "resolved": 4, "kind": "file"}]),
    lambda o: o.update(files=[{"source": "a", "destination": "a", "resolved": "a", "kind": "file", "order": "1"}]),
    lambda o: o.update(files=[{"source": "a", "destination": "a", "resolved": "a", "kind": "file", "priority": True}]),
    lambda o: o.update(files=["a"] * 2001),
    lambda o: o.update(files=[], files_truncated="yes"),
])
def test_bad_files_data_is_refused(mountains, change):
    change(mountains["steps"][0]["groups"][0]["options"][0])
    with pytest.raises(EventValidationError):
        validate_event(mountains)


def test_files_on_an_automatic_install_are_checked_too(mountains):
    mountains["automatic_installs"] = [{"id": "conditional:0", "installed": True, "plugins": [], "detects": [],
                                         "files": [{"source": "a"}]}]
    with pytest.raises(EventValidationError):
        validate_event(mountains)


def _schema_errors(event: dict) -> list:
    schema = json.loads((ROOT / "schemas" / "installation-event.schema.json").read_text(encoding="utf-8"))
    return list(Draft202012Validator(schema).iter_errors(event))


def test_the_schema_accepts_files_and_old_events_and_refuses_unknown_entry_fields(mountains, installation_event):
    assert _schema_errors(mountains) == []
    assert _schema_errors(installation_event) == []          # additive: the version stays 1
    assert installation_event["schema_version"] == 1
    mountains["steps"][0]["groups"][0]["options"][0]["files"][0]["extra"] = 1
    assert _schema_errors(mountains)
    mountains["steps"][0]["groups"][0]["options"][0]["files"][0].pop("extra")
    mountains["steps"][0]["groups"][0]["options"][0]["files"] = [
        entry("a", "a") for _ in range(2001)]
    assert _schema_errors(mountains)                        # the per-option cap is in the schema


def test_the_schema_describes_files_on_options_and_automatic_installs():
    schema = json.loads((ROOT / "schemas" / "installation-event.schema.json").read_text(encoding="utf-8"))
    assert {"files", "files_truncated"} <= set(schema["$defs"]["option"]["properties"])
    assert {"files", "files_truncated"} <= set(schema["$defs"]["automaticInstall"]["properties"])
    assert schema["$defs"]["fileEntries"]["maxItems"] == 2000
    assert schema["$defs"]["fileEntry"]["additionalProperties"] is False
    assert schema["properties"]["schema_version"] == {"const": 1}


@pytest.mark.skipif(not os.environ.get("FOMOD_LOGGER_NATIVE_TEST_EXE"),
                    reason="set FOMOD_LOGGER_NATIVE_TEST_EXE to a built previous_choices_test.exe")
def test_the_event_the_native_writer_wrote_validates_and_answers_lookups(tmp_path):
    exe = Path(os.environ["FOMOD_LOGGER_NATIVE_TEST_EXE"])
    sample = tmp_path / "sample"
    result = subprocess.run([str(exe), str(FIXTURES), str(sample)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    (path,) = (sample / "fomod_logger" / "events").glob("*.json")
    event = json.loads(path.read_text(encoding="utf-8"))
    assert _schema_errors(event) == []
    validate_event(event)
    # What the native side wrote is what the lookup reads.
    answer = FileIndex([event]).lookup("Textures/Landscape/anything.dds")
    assert {m["option"] for m in answer["matches"]} == {"1K", "2K"}
    two = next(m for m in answer["matches"] if m["option"] == "2K")
    assert two["picked"] and two["winner"] and two["priority"] == 5
    assert [m["option"] for m in FileIndex([event]).lookup("Majestic Mountains.esp")["matches"]] == ["2K"]
    auto = FileIndex([event]).lookup("SMIM Patch.esp")["matches"]
    assert [m["option"] for m in auto] == ["Automatic install 1"] and auto[0]["truncated"]
    # The required files are their own object (not an automatic install), and a hostile destination is flagged.
    assert event["required_files"]["files"][0]["resolved"] == "Textures/core.dds"
    assert not any(item["id"] == "required" for item in event["automatic_installs"])
    required = FileIndex([event]).lookup("textures/core.dds")["matches"]
    assert [m["option"] for m in required if m["required"]] == ["Required by the installer"]
    assert required[0]["required"] and required[0]["how"] == "file"       # the exact file comes first
    hostile = event["automatic_installs"][2]["files"][0]
    assert hostile["escapes"] is True and hostile["resolved"] == "" and hostile["destination"] == "..\\..\\x.dds"
    assert FileIndex([event]).lookup("x.dds")["matches"] == []
    with EventStore(tmp_path / "db.sqlite3") as store:
        assert store.ingest(event)
        assert [e["mod"]["name"] for e in store.latest("Textures/x/y.dds")] == ["Majestic Mountains"]


# ---------- who wins: only the same path competes, and a folder never outranks an exact file ----------

def test_a_bare_name_search_does_not_make_different_files_compete(installation_event):
    event = make(installation_event, "Resolutions", [("Main", [
        opt("2K", True, [entry("2k\\peak.dds", "textures/2k/peak.dds", order=1)]),
        opt("4K", True, [entry("4k\\peak.dds", "textures/4k/peak.dds", order=2)]),
    ])])
    matches = origin(event, "peak.dds")["matches"]
    assert {m["path"] for m in matches} == {"textures/2k/peak.dds", "textures/4k/peak.dds"}
    for match in matches:             # two different files: neither overwrites the other
        assert match["overwritten_by"] == [] and match["contested"] is False and match["later"] == []
        assert "overwritten" not in match["detail"] and "likely winner" not in match["detail"]


def test_a_bare_name_that_two_options_write_to_the_same_path_still_competes(installation_event):
    event = make(installation_event, "Same", [("Main", [
        opt("A", True, [entry("a\\peak.dds", "textures/peak.dds", order=1)]),
        opt("B", True, [entry("b\\peak.dds", "textures/peak.dds", order=2)]),
        opt("C", True, [entry("c\\peak.dds", "textures/other/peak.dds", order=3)]),
    ])])
    by_option = {m["option"]: m for m in origin(event, "peak.dds")["matches"]}
    assert by_option["B"]["winner"] and by_option["A"]["overwritten_by"] == [by_option["B"]["label"]]
    assert by_option["C"]["contested"] is False and by_option["C"]["overwritten_by"] == []


def test_a_folder_entry_does_not_overwrite_an_exact_file_it_may_not_even_hold(installation_event):
    event = make(installation_event, "Core and Extras", [("Main", [
        opt("Core", True, [entry("core\\x.dds", "textures/x.dds", order=1)]),
        opt("Extras", True, [entry("extras", "textures", folder=True, order=2, priority=9)]),
    ])])
    by_option = {m["option"]: m for m in origin(event, "textures/x.dds")["matches"]}
    core, extras = by_option["Core"], by_option["Extras"]
    assert core["winner"] is True and core["overwritten_by"] == [] and core["later"] == []
    assert core["contested"] is False                                  # only one exact entry writes it
    assert core["may_also"] == [extras["label"]]
    assert "a folder entry may also write it" in core["detail"] and "overwritten" not in core["detail"]
    assert extras["winner"] is False and extras["overwritten_by"] == [] and extras["contested"] is False
    assert extras["may_also_write"] is True and "may also write it" in extras["detail"]


def test_with_no_exact_entry_folders_compete_and_the_later_one_is_the_likely_winner(installation_event):
    event = make(installation_event, "Folders", [("Main", [
        opt("Wide", True, [entry("wide", "textures", folder=True, order=1)]),
        opt("Narrow", True, [entry("narrow", "textures/a", folder=True, order=2)]),
        opt("Unpicked", False, [entry("unpicked", "textures", folder=True, order=3)]),
    ])])
    by_option = {m["option"]: m for m in origin(event, "textures/a/b.dds")["matches"]}
    assert by_option["Narrow"]["winner"] and by_option["Narrow"]["contested"]
    assert by_option["Wide"]["overwritten_by"] == [by_option["Narrow"]["label"]] and not by_option["Wide"]["winner"]
    assert by_option["Unpicked"]["winner"] is False and by_option["Unpicked"]["overwritten_by"] == []
    # Priority decides before position, the same as for files.
    event["steps"][0]["groups"][0]["options"][0]["files"][0]["priority"] = 4
    by_option = {m["option"]: m for m in origin(event, "textures/a/b.dds")["matches"]}
    assert by_option["Wide"]["winner"] and by_option["Narrow"]["overwritten_by"] == [by_option["Wide"]["label"]]


def test_nested_folders_count_each_folder_above_the_path(installation_event):
    event = make(installation_event, "Deep", [("Main", [
        opt("One", True, [entry("1", "textures", folder=True, order=1)]),
        opt("Two", True, [entry("2", "textures/a/b", folder=True, order=2)]),
        opt("Sibling", True, [entry("3", "textures/a/c", folder=True, order=3)]),
    ])])
    assert {m["option"] for m in origin(event, "textures/a/b/c/d.dds")["matches"]} == {"One", "Two"}
    assert {m["option"] for m in origin(event, "textures/a/b")["matches"] if m["how"] == "folder"} == {"One", "Two"}


def test_an_exact_file_beats_folders_whatever_their_priority_and_the_best_entry_of_an_option_is_the_file(
        installation_event):
    event = make(installation_event, "Mixed", [("Main", [
        opt("Both", True, [entry("f", "textures", folder=True, order=9, priority=9),
                            entry("x.dds", "textures/x.dds", order=1)]),
    ])])
    (match,) = origin(event, "textures/x.dds")["matches"]
    assert match["how"] == "file" and match["winner"] is True


# ---------- the files the installer always installs ----------

def required_event(base, files, **extra):
    event = make(base, "Always", [("Main", [opt("Pick", True, [entry("p\\a.dds", "tex/a.dds", order=5)])])], **extra)
    event["required_files"] = {"files": files}
    return event


def test_required_files_are_an_answer_of_their_own(installation_event):
    event = required_event(installation_event, [entry("core\\core.dds", "tex/core.dds", order=1),
                                                 entry("core\\Always.esp", "", resolved="Always.esp", order=2)])
    validate_event(event)
    (match,) = origin(event, "tex/core.dds")["matches"]
    assert match["required"] and match["automatic"] and match["picked"] and match["option"] == "Required by the installer"
    assert match["text"] == "Always › Required by the installer (always installed)"
    assert match["option_id"] == "required" and match["winner"] is True
    assert [m["option"] for m in origin(event, "Always.esp")["matches"]] == ["Required by the installer"]


def test_a_later_option_overwrites_a_required_file_and_a_later_required_entry_overwrites_an_option(installation_event):
    event = required_event(installation_event, [entry("core\\a.dds", "tex/a.dds", order=1)])
    by_option = {m["option"]: m for m in origin(event, "tex/a.dds")["matches"]}
    assert by_option["Pick"]["winner"] and by_option["Required by the installer"]["overwritten_by"] == [
        by_option["Pick"]["label"]]
    event["required_files"]["files"][0]["order"] = 9
    by_option = {m["option"]: m for m in origin(event, "tex/a.dds")["matches"]}
    assert by_option["Required by the installer"]["winner"]


def test_an_installer_with_only_required_files_has_file_data(installation_event):
    event = make(installation_event, "Plain", [("Main", [opt("x", True)])])
    assert not file_origin.has_file_data(event)
    event["required_files"] = {"files": [entry("a", "tex/a.dds")]}
    assert file_origin.has_file_data(event)
    result = origin(event, "tex/a.dds")
    assert result["unrecorded"] == [] and len(result["matches"]) == 1
    assert origin(make(installation_event, "Plain", [("Main", [opt("x", True)])]), "tex/a.dds")["unrecorded"] == [
        "Plain"]


def test_required_files_are_validated_and_in_the_schema(installation_event):
    event = required_event(installation_event, [entry("a", "tex/a.dds")])
    assert _schema_errors(event) == []
    event["required_files"]["files_truncated"] = True
    validate_event(event)
    for bad in ({"files": "x"}, {"files": [{"source": "a"}]}, {"files": [], "extra": 1}, {"files_truncated": True},
                {"files": [], "files_truncated": 1}):
        broken = deepcopy(event)
        broken["required_files"] = bad
        with pytest.raises(EventValidationError):
            validate_event(broken)
        assert _schema_errors(broken)
    schema = json.loads((ROOT / "schemas" / "installation-event.schema.json").read_text(encoding="utf-8"))
    assert "required_files" in schema["properties"] and "required_files" not in schema["required"]


# ---------- entries that leave the mod folder, the Data folder, and the strictness of validation ----------

def test_an_entry_that_points_outside_the_mod_folder_is_never_a_match_and_never_the_mod_folder(installation_event):
    escaping = {"source": "x", "destination": "..\\\\..\\\\x.dds", "resolved": "", "kind": "folder", "order": 1,
                "escapes": True}
    event = make(installation_event, "Hostile", [("Main", [
        opt("Out", True, [escaping, dict(escaping, kind="file", order=2)])])])
    validate_event(event)
    assert _schema_errors(event) == []
    for query in ("x.dds", "textures/x.dds", "Hostile.esp"):
        assert origin(event, query)["matches"] == []
    assert "whole folder" not in " ".join(origin(event, "anything/x.dds")["notes"])   # not "the mod folder itself"


def test_a_data_prefix_in_a_recorded_destination_or_in_the_query_is_ignored(installation_event):
    event = make(installation_event, "Data", [("Main", [
        opt("Old", True, [entry("a", "Data/Textures/a.dds", resolved="Data/Textures/a.dds", order=1)])])])
    for query in ("textures/a.dds", "Data\\Textures\\A.dds", "C:\\Games\\Skyrim\\Data\\Textures\\a.dds"):
        assert [m["option"] for m in origin(event, query)["matches"]] == ["Old"], query


@pytest.mark.parametrize("pasted,expected", [
    (r"C:\Games\Skyrim Special Edition\Data\Textures\Foo\bar.dds", "Textures/Foo/bar.dds"),
    (r"D:\mods\Majestic Mountains\Textures\bar.dds", "Textures/bar.dds"),
    ('"c:/Games/Skyrim/data/Meshes/x.nif"', "Meshes/x.nif"),
    ("textures/data/bar.dds", "textures/data/bar.dds"),          # a relative path is left alone
    ("Data/Textures/bar.dds", "Data/Textures/bar.dds"),
])
def test_a_full_game_path_loses_everything_up_to_the_data_folder(pasted, expected):
    assert normalise_query(pasted) == expected


def test_a_reinstalled_mod_says_merged_files_may_be_missing(installation_event):
    event = make(installation_event, "Again", [("Main", [opt("x", True, [entry("a", "tex/a.dds")])])])
    assert not [n for n in origin(event, "tex/a.dds")["notes"] if "Merge" in n]
    event["source"]["reinstallation"] = True
    notes = origin(event, "tex/a.dds")["notes"]
    assert any("Again was reinstalled" in n and "Merge" in n for n in notes)
    assert any("Merge" in n for n in origin(event, "tex/none.dds")["notes"])      # nothing found: still worth saying


@pytest.mark.parametrize("change", [
    lambda e: e.update(extra=1),
    lambda e: e.update(order=-1),
    lambda e: e.update(escapes="yes"),
])
def test_file_entries_are_as_strict_as_the_schema(mountains, change):
    change(mountains["steps"][0]["groups"][0]["options"][0]["files"][0])
    with pytest.raises(EventValidationError):
        validate_event(mountains)
    assert _schema_errors(mountains)


# ---------- speed ----------

def _big_events(base, events=60, per_event=1500):
    out = []
    for e in range(events):
        files = [entry(f"s{i}", f"textures/pack{e}/sub{i % 40}/file{i}.dds", order=i + 1) for i in range(per_event // 2)]
        files += [entry(f"f{i}", f"textures/pack{e}/folder{i}", folder=True, order=per_event + i)
                  for i in range(per_event // 2)]
        out.append(make(base, f"Pack {e}", [("Main", [opt("Everything", True, files)])]))
    return out


def test_a_folder_query_over_a_big_history_is_one_run_in_a_sorted_list_not_a_scan(installation_event):
    import time

    events = _big_events(installation_event)
    index = FileIndex(events)
    index.lookup("warm up")                    # the sorted list is built by the first question
    started = time.perf_counter()
    for query in ("textures", "textures/pack3", "textures/pack3/sub7", "textures/pack3/sub7/file7.dds",
                  "textures/pack3/folder5/anything.dds"):
        answer = index.lookup(query)
        assert answer["matches"], query
    assert time.perf_counter() - started < 0.5


def test_a_huge_folder_is_cut_and_says_so(installation_event):
    events = _big_events(installation_event, events=40, per_event=2000)
    answer = FileIndex(events).lookup("textures")
    assert answer["more"] is True and any("great many files" in note for note in answer["notes"])
    assert 0 < len(answer["matches"]) <= 40


def test_the_index_is_kept_while_the_stamp_is_the_same_and_rebuilt_when_it_changes(tmp_path, mountains, monkeypatch):
    built = []
    real = FileIndex.__init__

    def counting(self, events):
        built.append(1)
        real(self, events)

    monkeypatch.setattr(FileIndex, "__init__", counting)
    database = tmp_path / "db.sqlite3"
    with EventStore(database) as store:
        store.ingest(mountains)
    cache = file_origin.OriginCache()
    for stamp, wanted in (("a", 1), ("a", 1), ("a", 1), ("b", 2)):
        with EventStore(database) as store:           # a new connection each time, like each refresh
            store.use_origin_cache(cache, stamp)
            assert store.file_origins("Majestic Mountains.esp", current=True)["matches"]
            store.latest("textures/landscape/mountains/peak.dds")
        assert len(built) == wanted
    with EventStore(database) as store:               # no stamp: not trusted, built for this connection only
        store.use_origin_cache(cache, None)
        store.file_origins("Majestic Mountains.esp")
    assert len(built) == 3
