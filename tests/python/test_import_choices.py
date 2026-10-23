"""Importing a choices file: what is stored, who wins, and how it is undone."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from fomod_logger_support.core import export_choices, import_choices
from fomod_logger_support.core.choices_file import ChoicesFileError, FORMAT_ID
from fomod_logger_support.core.storage import EventStore

from test_export_choices import INSTANCE, OTHER_INSTANCE, good_file, grass_event

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "native" / "fixtures"
NOW = "2026-10-21T11:00:00Z"
GAME = {"name": "Skyrim Special Edition", "nexus_domain": "skyrimspecialedition"}


def entry(name: str, *, nexus_id: int = 0, archive: str = "", picked: bool = True) -> dict:
    return {"name": name, "nexus_id": nexus_id, "version": "1", "archive": archive, "installed_at": "t",
            "steps": [{"name": "Main", "skipped": False, "groups": [
                {"name": "Options", "type": "SelectAny", "options": [
                    {"name": "Fancy", "picked": picked, "enabled": True}]}]}]}


def choices(*mods: dict) -> dict:
    return {"format": FORMAT_ID, "format_version": 1, "exported_at": "2026-10-20T09:00:00Z",
            "source": {"game": "Skyrim Special Edition", "nexus_domain": "skyrimspecialedition", "instance_label": "x"},
            "mods": list(mods)}


@pytest.fixture
def store(tmp_path):
    with EventStore(tmp_path / "data" / "logger.sqlite3") as opened:
        yield opened


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "data"


def run(store, root, data, instance=INSTANCE):
    return import_choices.import_choices(data, data_root=root, instance_id=instance, store=store, game=GAME, imported_at=NOW)


def records(root, instance=INSTANCE):
    return import_choices.list_imported(root, instance)


# --- storing ----------------------------------------------------------------------------------------------------

def test_each_mod_is_stored_as_a_record_the_installer_can_read(store, root):
    summary = run(store, root, choices(entry("Alpha", nexus_id=5, archive="alpha.7z"), entry("Beta")))
    assert (summary.imported, summary.own_wins, summary.not_installed) == (2, 0, 2)
    found = {record["entry"]["name"]: record for record in records(root)}
    assert set(found) == {"Alpha", "Beta"}
    alpha = found["Alpha"]
    assert (alpha["format"], alpha["record_version"], alpha["instance_id"]) == (
        "fomod-companion-imported-choices", 1, INSTANCE)
    assert (alpha["imported_at"], alpha["source_exported_at"]) == (NOW, "2026-10-20T09:00:00Z")
    assert alpha["entry"]["nexus_id"] == 5 and alpha["entry"]["archive"] == "alpha.7z"
    folder = import_choices.imported_folder(root)
    assert sorted(path.suffix for path in folder.iterdir()) == [".json", ".json"]      # no temporary files left
    assert all(path.name.startswith(INSTANCE[:16] + "-") for path in folder.iterdir())


def test_the_record_matches_the_one_the_native_tests_read(store, root, tmp_path):
    """tests/native/fixtures/imported_record.json is read by the native test too, which holds both sides to one
    format. It is what importing imported_choices.json for an instance of 64 'a's at a fixed time writes."""

    data = json.loads((FIXTURES / "imported_choices.json").read_text(encoding="utf-8"))
    import_choices.import_choices(data, data_root=root, instance_id="a" * 64, store=store, game=GAME, imported_at=NOW)
    [written] = import_choices.imported_folder(root).glob("*.json")
    assert written.read_bytes() == (FIXTURES / "imported_record.json").read_bytes()


def test_the_fixture_choices_file_is_what_exporting_the_native_fixture_event_gives():
    event = json.loads((FIXTURES / "duplicate_labels_event.json").read_text(encoding="utf-8"))
    made = export_choices.build_choices_file(
        [event], game="Skyrim Special Edition", nexus_domain="skyrimspecialedition", instance_label="Fixture",
        exported_at="2026-10-21T10:00:00Z")
    assert export_choices.choices_json(made) == (FIXTURES / "imported_choices.json").read_text(encoding="utf-8")


def test_a_second_import_of_a_mod_replaces_the_first(store, root):
    run(store, root, choices(entry("Alpha", picked=False), entry("Beta")))
    summary = import_choices.import_choices(choices(entry("alpha", picked=True)), data_root=root, instance_id=INSTANCE,
                                            store=store, game=GAME, imported_at="2026-10-22T08:00:00Z")
    assert (summary.imported, summary.replaced) == (1, 1)
    [alpha] = [r for r in records(root) if r["entry"]["name"].lower() == "alpha"]
    assert alpha["imported_at"] == "2026-10-22T08:00:00Z"
    assert alpha["entry"]["steps"][0]["groups"][0]["options"][0]["picked"] is True
    assert len(records(root)) == 2                                   # Beta stays; Alpha is not stored twice


def test_duplicate_entries_in_one_file_keep_the_first(store, root):
    summary = run(store, root, choices(entry("Alpha", picked=True), entry("ALPHA", picked=False), entry("Beta")))
    assert (summary.imported, summary.duplicates) == (2, 1)
    [alpha] = [r for r in records(root) if r["entry"]["name"] == "Alpha"]
    assert alpha["entry"]["steps"][0]["groups"][0]["options"][0]["picked"] is True
    assert "1 repeated entry" in summary.message()


def test_two_mods_on_one_nexus_page_are_both_kept(store, root):
    summary = run(store, root, choices(entry("Big Mod", nexus_id=7, archive="main.zip"),
                                       entry("Big Mod Patches", nexus_id=7, archive="patches.zip")))
    assert (summary.imported, summary.duplicates) == (2, 0)


# --- your own choices win -----------------------------------------------------------------------------------

def test_mods_you_have_your_own_choices_for_are_counted_and_still_yours(store, root):
    store.ingest(grass_event(name="Grass Patches", archive="Grass-2.1.zip"))
    store.ingest(grass_event("22222222-2222-4222-8222-222222222222", name="Renamed Later", archive="later.7z"))
    other = grass_event("33333333-3333-4333-8333-333333333333", name="Elsewhere Mod", instance=OTHER_INSTANCE)
    store.ingest(other)
    data = choices(entry("grass patches"),                          # same name, other case
                   entry("Something Else", archive="LATER.7z"),    # same archive file, other name
                   entry("Elsewhere Mod"),                          # only another MO2 instance has it
                   entry("Brand New"))
    summary = run(store, root, data)
    assert (summary.imported, summary.own_wins, summary.not_installed) == (4, 2, 2)
    assert sorted(summary.own) == ["Something Else", "grass patches"]
    assert sorted(summary.new) == ["Brand New", "Elsewhere Mod"]
    text = summary.message()
    assert "Imported choices for 4 mods." in text
    assert "2 already have your own previous choices" in text and "2 are not installed yet" in text
    assert "only offers the imported ones if yours match nothing" in text
    assert "Nothing is ticked until you do." in text
    # Nothing of the user's own was touched.
    assert store.event_count() == 3
    assert [e["event_id"] for e in store.history("", include_hidden=True)].count("11111111-1111-4111-8111-111111111111") == 1


def test_a_removed_mod_still_counts_as_having_your_own_choices(store, root):
    store.ingest(grass_event(name="Gone Mod"))
    assert store.mark_removed(INSTANCE, "Gone Mod", "2026-10-02T00:00:00+00:00")
    # The installer reads the user's recorded installs whether or not the mod is still installed.
    assert run(store, root, choices(entry("Gone Mod"))).own_wins == 1


def test_imports_are_kept_per_instance(store, root):
    run(store, root, choices(entry("Alpha")))
    run(store, root, choices(entry("Alpha")), instance=OTHER_INSTANCE)
    assert len(records(root)) == 1 and len(records(root, OTHER_INSTANCE)) == 1
    assert import_choices.remove_imported(root, INSTANCE) == 1
    assert records(root) == [] and len(records(root, OTHER_INSTANCE)) == 1


# --- removal ------------------------------------------------------------------------------------------------

def test_removing_the_imported_choices_deletes_only_those(store, root):
    run(store, root, choices(entry("Alpha"), entry("Beta")))
    folder = import_choices.imported_folder(root)
    (folder / "notes.txt").write_text("not mine", encoding="utf-8")
    (folder / "other.json").write_text(json.dumps({"hello": 1}), encoding="utf-8")
    assert import_choices.imported_count(root, INSTANCE) == 2
    assert import_choices.remove_imported(root, INSTANCE) == 2
    assert import_choices.imported_count(root, INSTANCE) == 0
    assert sorted(path.name for path in folder.iterdir()) == ["notes.txt", "other.json"]
    assert import_choices.remove_imported(root, INSTANCE) == 0
    assert import_choices.remove_imported(root / "nowhere", INSTANCE) == 0
    store.ingest(grass_event())
    assert store.event_count() == 1


# --- bad input ----------------------------------------------------------------------------------------------

def test_a_malformed_file_changes_nothing(store, root):
    run(store, root, choices(entry("Alpha")))
    before = {path.name: path.read_bytes() for path in import_choices.imported_folder(root).iterdir()}
    bad = good_file()
    bad["mods"].append({"name": "Broken", "steps": "nope"})
    with pytest.raises(ChoicesFileError, match=r"mods\[1\]\.steps must be a list"):
        run(store, root, bad)
    after = {path.name: path.read_bytes() for path in import_choices.imported_folder(root).iterdir()}
    assert after == before


@pytest.mark.parametrize("content, message", [
    (b"", "not valid JSON"),
    (b"{oops", "not valid JSON"),
    (b'["a list"]', "must be an object"),
    (json.dumps({"format": "other", "format_version": 1, "mods": []}).encode(), "not a Modlist Companion choices file"),
    (json.dumps({"format": FORMAT_ID, "format_version": 7, "mods": []}).encode(), "newer Modlist Companion"),
    (b"\xff\xfe\xfa", "not UTF-8"),
])
def test_a_bad_file_is_refused_with_a_clear_message(store, root, tmp_path, content, message):
    path = tmp_path / "bad.json"
    path.write_bytes(content)
    with pytest.raises(ChoicesFileError, match=message):
        import_choices.import_choices_path(path, data_root=root, instance_id=INSTANCE, store=store, game=GAME)
    assert not import_choices.imported_folder(root).exists() or records(root) == []


def test_a_missing_or_oversized_file_is_refused(store, root, tmp_path, monkeypatch):
    with pytest.raises(ChoicesFileError, match="could not be read"):
        import_choices.import_choices_path(tmp_path / "nothing.json", data_root=root, instance_id=INSTANCE, store=store, game=GAME)
    monkeypatch.setattr(import_choices, "MAX_FILE_BYTES", 10)
    big = tmp_path / "big.json"
    big.write_text(json.dumps(choices(entry("Alpha"))), encoding="utf-8")
    with pytest.raises(ChoicesFileError, match="larger than"):
        import_choices.import_choices_path(big, data_root=root, instance_id=INSTANCE, store=store, game=GAME)


def test_an_unknown_instance_is_refused(store, root):
    for instance in ("", "short", "G" * 64):
        with pytest.raises(ChoicesFileError, match="not known"):
            run(store, root, choices(entry("Alpha")), instance=instance)


def test_an_empty_file_imports_nothing_and_says_so(store, root):
    summary = run(store, root, choices())
    assert summary.imported == 0 and "nothing was imported" in summary.message()
    assert records(root) == []


def test_a_file_exported_by_this_build_imports_whole(store, root, tmp_path):
    store.ingest(grass_event(name="Grass Patches"))
    made = export_choices.build_choices_file(export_choices.current_events(store, instance_id=INSTANCE),
                                             game=GAME["name"], nexus_domain=GAME["nexus_domain"])
    path = tmp_path / "export.json"
    export_choices.write_text(path, export_choices.choices_json(made))
    with EventStore(tmp_path / "other.sqlite3") as fresh:                 # another PC: no history at all
        summary = import_choices.import_choices_path(path, data_root=tmp_path / "pc2", instance_id=OTHER_INSTANCE,
                                                      store=fresh, game=GAME, imported_at=NOW)
    assert (summary.imported, summary.own_wins, summary.not_installed) == (1, 0, 1)
    [record] = import_choices.list_imported(tmp_path / "pc2", OTHER_INSTANCE)
    assert record["entry"] == made["mods"][0]
    assert deepcopy(record["entry"]["steps"][1]["skipped"]) is True       # the skipped page survives the trip


# --- one game's file in another game ---------------------------------------------------------------------------

def test_a_file_for_another_game_is_refused_and_names_both_games(store, root):
    data = choices(entry("Alpha", nexus_id=5))
    data["source"] = {"game": "Fallout 4", "nexus_domain": "fallout4", "instance_label": "x"}
    with pytest.raises(ChoicesFileError) as error:
        run(store, root, data)
    text = str(error.value)
    assert "Fallout 4" in text and "Skyrim Special Edition" in text and "fallout4" in text and "skyrimspecialedition" in text
    assert records(root) == []                                                    # nothing was stored
    data["source"]["nexus_domain"] = "SkyrimSpecialEdition "                       # same game, other spelling: fine
    assert run(store, root, data).imported == 1


def test_a_file_that_does_not_say_its_game_is_imported_without_its_nexus_ids(store, root):
    data = choices(entry("Alpha", nexus_id=5, archive="alpha.7z"), entry("Beta", nexus_id=6))
    data["source"] = {"game": "", "nexus_domain": "", "instance_label": ""}
    summary = run(store, root, data)
    assert summary.imported == 2 and summary.ids_dropped is True
    assert [r["entry"]["nexus_id"] for r in records(root)] == [0, 0]
    assert [r["entry"]["archive"] for r in records(root) if r["entry"]["name"] == "Alpha"] == ["alpha.7z"]   # these still match
    assert "Nexus IDs were ignored" in summary.message()
    # With the game known on both sides the IDs are kept and nothing is said.
    kept = run(store, root, choices(entry("Gamma", nexus_id=9)))
    assert kept.ids_dropped is False and "Nexus IDs" not in kept.message()
    assert [r["entry"]["nexus_id"] for r in records(root) if r["entry"]["name"] == "Gamma"] == [9]


def test_when_this_mo2s_game_is_not_known_the_ids_cannot_be_checked_and_are_dropped(store, root):
    summary = import_choices.import_choices(choices(entry("Alpha", nexus_id=5)), data_root=root, instance_id=INSTANCE,
                                            store=store, game=None, imported_at=NOW)
    assert summary.ids_dropped and records(root)[0]["entry"]["nexus_id"] == 0


# --- limits -----------------------------------------------------------------------------------------------------

def test_an_entry_too_big_for_the_installer_to_read_is_left_out_and_counted(store, root, monkeypatch):
    monkeypatch.setattr(import_choices, "MAX_RECORD_BYTES", 900)
    big = entry("Big Mod")
    big["steps"][0]["groups"][0]["options"] = [{"name": f"Option number {n}", "picked": True, "enabled": True}
                                               for n in range(40)]
    summary = run(store, root, choices(entry("Small Mod"), big))
    assert (summary.imported, summary.too_large) == (1, 1)
    assert [r["entry"]["name"] for r in records(root)] == ["Small Mod"]
    assert "1 mod left out: too large to read back." in summary.message()


def test_the_record_limit_is_the_one_the_installer_reads():
    cpp = (Path(__file__).resolve().parents[2] / "src" / "fomodpreviouschoices.h").read_text(encoding="utf-8")
    assert "constexpr qint64 kMaxEventFileBytes = 4LL * 1024 * 1024;" in cpp
    assert import_choices.MAX_RECORD_BYTES == 4 * 1024 * 1024


def test_a_file_with_far_too_many_options_is_refused_before_it_is_read_through(monkeypatch):
    from fomod_logger_support.core import choices_file

    monkeypatch.setattr(choices_file, "MAX_TOTAL_OPTIONS", 50)
    mods = []
    for n in range(3):
        mod = entry(f"Mod {n}")
        mod["steps"][0]["groups"][0]["options"] = [{"name": f"o{i}", "picked": False} for i in range(20)]
        mods.append(mod)
    with pytest.raises(ChoicesFileError, match="more than 50 options in all"):
        choices_file.validate_choices_file(choices(*mods))
    choices_file.validate_choices_file(choices(*mods[:2]))                         # 40 options: fine


def test_the_real_whole_file_cap_is_about_200000_options():
    from fomod_logger_support.core import choices_file

    assert choices_file.MAX_TOTAL_OPTIONS == 200_000


# --- the time a record carries is the time of the import ---------------------------------------------------------

def test_a_file_cannot_set_the_time_its_records_count_as_imported(store, root):
    data = choices(entry("Alpha"))
    data["exported_at"] = "2099-01-01T00:00:00Z"
    data["mods"][0]["installed_at"] = "2099-01-01T00:00:00Z"
    data["mods"][0]["imported_at"] = "2099-01-01T00:00:00Z"                  # an unknown field: dropped
    import_choices.import_choices(data, data_root=root, instance_id=INSTANCE, store=store, game=GAME)
    [record] = records(root)
    assert record["imported_at"] < "2099"                                      # the importer's own clock
    assert "imported_at" not in record["entry"]


# --- counting without reading ---------------------------------------------------------------------------------------

def test_the_menu_count_comes_from_file_names_alone(store, root):
    run(store, root, choices(entry("Alpha"), entry("Beta")))
    run(store, root, choices(entry("Alpha")), instance=OTHER_INSTANCE)
    folder = import_choices.imported_folder(root)
    for path in folder.glob("*.json"):
        path.write_text("not even json", encoding="utf-8")                      # never read by the count
    assert import_choices.imported_file_count(root, INSTANCE) == 2
    assert import_choices.imported_file_count(root, OTHER_INSTANCE) == 1
    assert import_choices.imported_file_count(root, "") == 0
    assert import_choices.imported_file_count(root / "nowhere", INSTANCE) == 0


def test_importing_again_adds_to_what_was_imported_before(store, root):
    first = run(store, root, choices(entry("Alpha"), entry("Beta")))
    second = run(store, root, choices(entry("Alpha"), entry("Gamma")))
    assert sorted(r["entry"]["name"] for r in records(root)) == ["Alpha", "Beta", "Gamma"]
    assert "stays until you choose Remove imported choices" in first.message()
    assert second.replaced == 1
