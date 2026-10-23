"""Exporting choices: the choices file, its schema, and the install guide made from the same data."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from fomod_logger_support.core import export_choices
from fomod_logger_support.core.choices_file import (
    ChoicesFileError, FORMAT_ID, FORMAT_VERSION, parse_choices_file, validate_choices_file)
from fomod_logger_support.core.storage import EventStore

ROOT = Path(__file__).resolve().parents[2]
INSTANCE = "a" * 64
OTHER_INSTANCE = "b" * 64


def schema_errors(data) -> list:
    schema = json.loads((ROOT / "schemas" / "choices-file.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return list(Draft202012Validator(schema).iter_errors(data))


def grass_event(event_id="11111111-1111-4111-8111-111111111111", completed="2026-10-01T12:00:00+00:00",
                name="Grass Patches", instance=INSTANCE, archive="C:\\Downloads\\Grass-2.1.zip", nexus_id=4242) -> dict:
    """A mod with a skipped page, a forced option, a blocked option and a name that needs escaping."""

    def option(oid, label, selected, enabled=True):
        return {"id": oid, "name": label, "selected": selected, "enabled": enabled}

    return {
        "schema_version": 1, "event_id": event_id, "started_at": completed, "completed_at": completed,
        "instance": {"id": instance, "label": "Test MO2"},
        "source": {"archive": archive, "reinstallation": False},
        "mod": {"name": name, "version": "2.1", "nexus_id": nexus_id},
        "installer": {"name": "FOMOD Logger Native Installer", "version": "1.0.0",
                      "upstream_commit": "fc263f2d923c704b4853c11ed4f8b8cf3920f30d"},
        "steps": [
            {"id": "step:0", "name": "Grass", "visible": True, "groups": [
                {"id": "step:0/group:0", "name": "Length", "type": "SelectExactlyOne", "options": [
                    option("step:0/group:0/option:0", "Short", False),
                    option("step:0/group:0/option:1", "Tall", True)]},
                {"id": "step:0/group:1", "name": "Extras", "type": "SelectAny", "options": [
                    option("step:0/group:1/option:0", "Core <files> & *more*", True, enabled=False),
                    option("step:0/group:1/option:1", "Blocked thing", False, enabled=False),
                    option("step:0/group:1/option:2", "Shiny", False)]}]},
            {"id": "step:1", "name": "Lite notes", "visible": False, "groups": [
                {"id": "step:1/group:0", "name": "Notes", "type": "SelectAny", "options": [
                    option("step:1/group:0/option:0", "Verbose", False)]}]},
            {"id": "step:2", "name": "Patches", "visible": True, "groups": [
                {"id": "step:2/group:0", "name": "Compatibility", "type": "SelectAtLeastOne", "options": [
                    option("step:2/group:0/option:0", "Village patch", True)]}]},
        ],
    }


def store_with(tmp_path, *events) -> EventStore:
    store = EventStore(tmp_path / "logger.sqlite3")
    for event in events:
        store.ingest(event)
    return store


# --- the choices file -----------------------------------------------------------------------------------------

def test_the_choices_file_has_the_agreed_shape_and_matches_the_public_schema(tmp_path):
    with store_with(tmp_path, grass_event()) as store:
        events = export_choices.current_events(store, instance_id=INSTANCE)
    choices = export_choices.build_choices_file(
        events, game="Skyrim Special Edition", nexus_domain="skyrimspecialedition", instance_label="Test MO2",
        exported_at="2026-10-21T10:00:00Z")

    assert schema_errors(choices) == []
    assert choices["format"] == FORMAT_ID == "fomod-companion-choices"
    assert choices["format_version"] == FORMAT_VERSION == 1
    assert choices["exported_at"] == "2026-10-21T10:00:00Z"
    assert choices["source"] == {"game": "Skyrim Special Edition", "nexus_domain": "skyrimspecialedition",
                                 "instance_label": "Test MO2"}
    [mod] = choices["mods"]
    assert (mod["name"], mod["nexus_id"], mod["version"]) == ("Grass Patches", 4242, "2.1")
    assert mod["archive"] == "Grass-2.1.zip"          # the file name, not the folder it was in
    assert mod["installed_at"] == "2026-10-01T12:00:00+00:00"
    assert [step["name"] for step in mod["steps"]] == ["Grass", "Lite notes", "Patches"]
    assert [step["skipped"] for step in mod["steps"]] == [False, True, False]
    extras = mod["steps"][0]["groups"][1]
    assert extras["type"] == "SelectAny"
    assert extras["options"] == [{"name": "Core <files> & *more*", "picked": True, "enabled": False},
                                 {"name": "Blocked thing", "picked": False, "enabled": False},
                                 {"name": "Shiny", "picked": False, "enabled": True}]
    # Names only: no event or option ids leak into the file.
    assert '"id"' not in json.dumps(choices)
    # What was written reads back identically through the importer's own check.
    assert parse_choices_file(export_choices.choices_json(choices)) == validate_choices_file(choices)


def test_an_unknown_nexus_id_is_exported_as_zero(tmp_path):
    with store_with(tmp_path, grass_event(nexus_id=-1)) as store:
        events = export_choices.current_events(store, instance_id=INSTANCE)
    [mod] = export_choices.build_choices_file(events)["mods"]
    assert mod["nexus_id"] == 0


def test_only_the_newest_install_of_each_mod_that_is_not_removed(tmp_path):
    older = grass_event("11111111-1111-4111-8111-111111111111", "2026-09-01T12:00:00+00:00")
    newer = deepcopy(older)
    newer.update(event_id="22222222-2222-4222-8222-222222222222", completed_at="2026-10-01T12:00:00+00:00",
                 started_at="2026-10-01T12:00:00+00:00")
    newer["steps"][0]["groups"][0]["options"][0]["selected"] = True    # Short now, Tall before
    newer["steps"][0]["groups"][0]["options"][1]["selected"] = False
    removed = grass_event("33333333-3333-4333-8333-333333333333", name="Removed Mod")
    elsewhere = grass_event("44444444-4444-4444-8444-444444444444", name="Other Instance Mod", instance=OTHER_INSTANCE)
    with store_with(tmp_path, older, newer, removed, elsewhere) as store:
        assert store.mark_removed(INSTANCE, "Removed Mod", "2026-10-02T00:00:00+00:00")
        events = export_choices.current_events(store, instance_id=INSTANCE)
        everything = export_choices.current_events(store)
    mods = export_choices.build_choices_file(events)["mods"]
    assert [mod["name"] for mod in mods] == ["Grass Patches"]
    picked = [o["name"] for o in mods[0]["steps"][0]["groups"][0]["options"] if o["picked"]]
    assert picked == ["Short"]                                          # the newest install, not the older one
    assert sorted(event["mod"]["name"] for event in everything) == ["Grass Patches", "Other Instance Mod"]


def test_hidden_mods_are_left_out_unless_asked_for(tmp_path):
    visible = grass_event("11111111-1111-4111-8111-111111111111", name="Visible Mod")
    hidden = grass_event("22222222-2222-4222-8222-222222222222", name="Hidden Mod")
    with store_with(tmp_path, visible, hidden) as store:
        assert store.set_hidden(hidden["event_id"], True)
        plain = export_choices.current_events(store, instance_id=INSTANCE)
        asked = export_choices.current_events(store, instance_id=INSTANCE, include_hidden=True)
    assert [e["mod"]["name"] for e in plain] == ["Visible Mod"]
    assert sorted(e["mod"]["name"] for e in asked) == ["Hidden Mod", "Visible Mod"]


def test_mods_are_listed_in_name_order(tmp_path):
    events = [grass_event(f"{n}1111111-1111-4111-8111-111111111111", name=name)
              for n, name in enumerate(["zebra", "Apple", "banana"], 1)]
    with store_with(tmp_path, *events) as store:
        mods = export_choices.build_choices_file(export_choices.current_events(store, instance_id=INSTANCE))["mods"]
    assert [mod["name"] for mod in mods] == ["Apple", "banana", "zebra"]


# --- the schema and the importer's check agree -------------------------------------------------------------------

def good_file() -> dict:
    return {"format": FORMAT_ID, "format_version": 1, "exported_at": "2026-10-21T10:00:00Z",
            "source": {"game": "Skyrim", "nexus_domain": "skyrim", "instance_label": "x"},
            "mods": [{"name": "A", "nexus_id": 1, "version": "1", "archive": "a.zip", "installed_at": "t",
                      "steps": [{"name": "S", "skipped": False, "groups": [
                          {"name": "G", "type": "SelectAny", "options": [
                              {"name": "O", "picked": True, "enabled": True}]}]}]}]}


def broken(change) -> dict:
    data = good_file()
    change(data)
    return data


BAD_FILES = {
    "wrong format id": broken(lambda d: d.update(format="something-else")),
    "no format": broken(lambda d: d.pop("format")),
    "newer version": broken(lambda d: d.update(format_version=2)),
    "version zero": broken(lambda d: d.update(format_version=0)),
    "version as text": broken(lambda d: d.update(format_version="1")),
    "version missing": broken(lambda d: d.pop("format_version")),
    "mods missing": broken(lambda d: d.pop("mods")),
    "mods not a list": broken(lambda d: d.update(mods={})),
    "mod not an object": broken(lambda d: d.update(mods=["A"])),
    "mod without a name": broken(lambda d: d["mods"][0].pop("name")),
    "mod with a blank name": broken(lambda d: d["mods"][0].update(name="   ")),
    "mod name not text": broken(lambda d: d["mods"][0].update(name=5)),
    "negative nexus id": broken(lambda d: d["mods"][0].update(nexus_id=-3)),
    "nexus id as text": broken(lambda d: d["mods"][0].update(nexus_id="12")),
    "nexus id as true": broken(lambda d: d["mods"][0].update(nexus_id=True)),
    "steps missing": broken(lambda d: d["mods"][0].pop("steps")),
    "steps not a list": broken(lambda d: d["mods"][0].update(steps="x")),
    "step without groups": broken(lambda d: d["mods"][0]["steps"][0].pop("groups")),
    "skipped as text": broken(lambda d: d["mods"][0]["steps"][0].update(skipped="no")),
    "group without options": broken(lambda d: d["mods"][0]["steps"][0]["groups"][0].pop("options")),
    "unknown group type": broken(lambda d: d["mods"][0]["steps"][0]["groups"][0].update(type="PickWhatever")),
    "option without picked": broken(lambda d: d["mods"][0]["steps"][0]["groups"][0]["options"][0].pop("picked")),
    "picked as text": broken(lambda d: d["mods"][0]["steps"][0]["groups"][0]["options"][0].update(picked="yes")),
    "enabled as number": broken(lambda d: d["mods"][0]["steps"][0]["groups"][0]["options"][0].update(enabled=1)),
    "option name missing": broken(lambda d: d["mods"][0]["steps"][0]["groups"][0]["options"][0].pop("name")),
    "name too long": broken(lambda d: d["mods"][0].update(name="x" * 1001)),
    "not an object": [good_file()],
}


@pytest.mark.parametrize("label", sorted(BAD_FILES))
def test_malformed_files_are_rejected_by_the_check_and_the_schema(label):
    data = BAD_FILES[label]
    with pytest.raises(ChoicesFileError):
        validate_choices_file(data)
    assert schema_errors(data), "the public schema should refuse it too"


def test_a_good_file_passes_both_and_unknown_fields_and_missing_optional_ones_are_fine():
    assert schema_errors(good_file()) == []
    clean = validate_choices_file(good_file())
    assert clean["mods"][0]["steps"][0]["groups"][0]["options"] == [{"name": "O", "picked": True, "enabled": True}]

    sparse = {"format": FORMAT_ID, "format_version": 1,
              "mods": [{"name": "B", "steps": [{"name": "S", "groups": [{"name": "G", "options": [
                  {"name": "O", "picked": False, "extra": "ignored"}]}]}], "something_new": 1}], "future": {}}
    assert schema_errors(sparse) == []
    clean = validate_choices_file(sparse)
    assert clean["mods"][0]["nexus_id"] == 0 and clean["mods"][0]["archive"] == ""
    assert clean["mods"][0]["steps"][0]["skipped"] is False
    assert clean["mods"][0]["steps"][0]["groups"][0]["options"][0] == {"name": "O", "picked": False, "enabled": True}
    assert "something_new" not in clean["mods"][0] and "future" not in clean


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b"null", b'{"format": "x"}', b"\xff\xfe\x00",
                                 '{"format": "fomod-companion-choices", "format_version": 1, "mods": [}'.encode()])
def test_unreadable_files_are_rejected_with_a_message(raw):
    with pytest.raises(ChoicesFileError) as error:
        parse_choices_file(raw)
    assert str(error.value)


def test_a_file_made_by_a_newer_companion_says_so():
    with pytest.raises(ChoicesFileError, match="newer Modlist Companion"):
        validate_choices_file(broken(lambda d: d.update(format_version=9)))


# --- the guide ---------------------------------------------------------------------------------------------

def exported(tmp_path, *events, **kwargs) -> dict:
    with store_with(tmp_path, *events) as store:
        return export_choices.build_choices_file(
            export_choices.current_events(store, instance_id=INSTANCE),
            game="Skyrim Special Edition", nexus_domain="skyrimspecialedition", exported_at="2026-10-21T10:00:00Z",
            **kwargs)


def test_the_markdown_guide_lists_pages_groups_and_picked_options(tmp_path):
    text = export_choices.render_markdown(exported(tmp_path, grass_event()))

    assert text.startswith("# FOMOD install guide\n")
    assert "Skyrim Special Edition · made 2026-10-21 · 1 mod" in text
    assert "## Grass Patches" in text
    assert "version 2.1 · [Nexus page](https://www.nexusmods.com/skyrimspecialedition/mods/4242) · file Grass-2.1.zip" in text
    # Pages in order, numbered over the pages that were shown: the skipped "Lite notes" page is not there.
    assert text.index("### Page 1: Grass") < text.index("### Page 2: Patches")
    assert "Lite notes" not in text and "Verbose" not in text
    assert "**Length** (choose one)" in text
    assert "- [x] **Tall**" in text
    assert "- [ ] Short (optional)" in text                   # the unpicked ones are optional
    assert "**Compatibility** (choose at least one)" in text
    assert "- [x] **Village patch**" in text
    assert "(required)" in text and "(not available)" in text  # forced and blocked options say so
    assert "- [x] **Core \\<files\\> & \\*more\\*** (required)" in text    # Markdown characters in names are escaped
    assert text.endswith("\n") and "\r" not in text


def test_the_guide_leaves_out_the_nexus_link_when_the_game_or_id_is_unknown(tmp_path):
    with store_with(tmp_path, grass_event(), grass_event("22222222-2222-4222-8222-222222222222", name="No Id", nexus_id=0)) as store:
        choices = export_choices.build_choices_file(export_choices.current_events(store, instance_id=INSTANCE))
    text = export_choices.render_markdown(choices)
    assert "nexusmods.com" not in text                          # no game, so no address
    assert "Nexus ID 4242" in text and "Nexus ID 0" not in text
    page = export_choices.render_html(choices)
    assert "nexusmods.com" not in page and "Nexus ID 4242" in page


def test_the_html_guide_is_a_standalone_escaped_page(tmp_path):
    page = export_choices.render_html(exported(tmp_path, grass_event(), grass_event(
        "22222222-2222-4222-8222-222222222222", name="<script>alert(1)</script> Mod")))

    assert page.startswith("<!doctype html>")
    assert "<title>FOMOD install guide</title>" in page and "<style>" in page
    assert "<script" not in page.replace("&lt;script&gt;", "")        # nothing active, mod text is escaped
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Mod" in page
    assert '<a href="https://www.nexusmods.com/skyrimspecialedition/mods/4242" rel="noopener">Nexus page</a>' in page
    assert "<h3>Page 1: Grass</h3>" in page and "Lite notes" not in page
    assert '<li class="picked"><span class="name">Tall</span></li>' in page
    assert '<li class="optional"><span class="name">Short</span><span class="note"> (optional)</span></li>' in page
    assert 'class="required"' in page and 'class="unavailable"' in page
    assert "Core &lt;files&gt; &amp; *more*" in page
    assert "http://" not in page and "src=" not in page                # no external files
    assert page.count('<section class="mod"') == 2 and '<nav>' in page


def test_a_mod_with_nothing_to_choose_says_so(tmp_path):
    event = grass_event()
    event["steps"] = []
    choices = exported(tmp_path, event)
    assert "The installer had nothing to choose." in export_choices.render_markdown(choices)
    assert "The installer had nothing to choose." in export_choices.render_html(choices)


def test_an_unsafe_game_address_is_never_made_into_a_link(tmp_path):
    choices = exported(tmp_path, grass_event())
    choices["source"]["nexus_domain"] = 'x"><script>'
    assert "nexusmods" not in export_choices.render_html(choices)
    assert "nexusmods" not in export_choices.render_markdown(choices)


def test_write_text_replaces_the_file_whole_with_lf_ends(tmp_path):
    target = tmp_path / "guide.md"
    target.write_text("old", encoding="utf-8")
    export_choices.write_text(target, "one\ntwo\n")
    assert target.read_bytes() == b"one\ntwo\n"
    assert [p.name for p in tmp_path.iterdir()] == ["guide.md"]        # no temporary file left
