"""Rebuilding an installation event from an archive and a set of picks."""

from __future__ import annotations

import json
import shutil
import sqlite3
import struct
from pathlib import Path

import pytest

import archive_fixtures as fx
from fomod_logger_support.archive import rebuild as rb
from fomod_logger_support.archive.fomod_xml import parse_module_config
from fomod_logger_support.archive.rebuild import (
    ArchiveTree, ConditionEnvironment, NoFomodError, Selection, rebuild_event, reconstruct_from_archive)
from fomod_logger_support.archive.sevenzip import ArchiveEncryptedError, ArchiveMultiPartError
from fomod_logger_support.core.storage import validate_event
from test_archive_reader import DLL, DOWNLOADS, MO2_FOLDER, needs_dll

INSTANCE = {"instance_id": "a" * 64, "instance_label": "Test MO2"}


# --------------------------------------------------------------------------- helpers


def plugin(masters=(), *, oblivion=False, extra: bytes = b"", flags: int = 0) -> bytes:
    """A plugin header (TES4 record) with the given masters, then some fake records."""

    def sub(kind: bytes, data: bytes) -> bytes:
        return kind + struct.pack("<H", len(data)) + data

    body = sub(b"HEDR", struct.pack("<fII", 1.7, 0, 0x800)) + sub(b"CNAM", b"author\0")
    for master in masters:
        body += sub(b"MAST", master.encode("latin-1") + b"\0") + sub(b"DATA", b"\0" * 8)
    body += extra
    record = b"TES4" + struct.pack("<III", len(body), flags, 0)
    record += struct.pack("<I", 0) if oblivion else struct.pack("<II", 0, 0)
    return record + body + b"GRUP" + b"\0" * 20


class Files:
    """An in-memory archive: paths -> bytes, with a header reader that counts its calls."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.reads: list[tuple[list[str], int]] = []

    def tree(self) -> ArchiveTree:
        def reader(paths, window):
            self.reads.append((list(paths), window))
            return {path: self.files[path][:window] for path in paths}

        return ArchiveTree(self.files, reader)


def build(xml: str, selection: Selection, files: dict[str, bytes] | None = None, *, base: str = "",
          environment=None, **kwargs):
    config = parse_module_config(xml.encode("utf-8"))
    archive = Files(files or {})
    result = rebuild_event(config, selection, archive.tree(), base=base, environment=environment,
                           mod_name="Kitchen", archive_name="kitchen.zip",
                           started_at="2026-10-21T10:00:00.000Z", completed_at="2026-10-21T10:00:05.000Z",
                           **INSTANCE, **kwargs)
    _schema_validator().validate(result.event)           # the JSON schema, as well as validate_event
    return result, archive


def _schema_validator():
    import jsonschema
    schema = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "installation-event.schema.json")
                        .read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def options(event):
    return {o["name"]: o for s in event["steps"] for g in s["groups"] for o in g["options"]}


def selected(event):
    return sorted(name for name, o in options(event).items() if o["selected"])


def ids(*triples):
    return Selection.from_option_ids([f"step:{s}/group:{g}/option:{o}" for s, g, o in triples])


KITCHEN = """<config>
  <moduleName>Kitchen</moduleName>
  <requiredInstallFiles>
    <file source="core.esp" destination="core.esp"/>
    <folder source="docs" destination="docs" priority="1"/>
  </requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Base">
      <optionalFileGroups order="Explicit">
        <group name="Pick" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="Full">
              <conditionFlags><flag name="tier">full</flag></conditionFlags>
              <files><file source="full\\Full.esp" destination="Full.esp"/><folder source="full\\tex" destination="textures"/></files>
              <typeDescriptor><type name="Recommended"/></typeDescriptor>
            </plugin>
            <plugin name="Lite">
              <conditionFlags><flag name="tier">lite</flag></conditionFlags>
              <files><file source="lite\\Lite.esp" destination="Data\\Lite.esp"/></files>
            </plugin>
            <plugin name="Locked">
              <typeDescriptor><dependencyType><defaultType name="Optional"/><patterns><pattern>
                <dependencies><fileDependency file="Absent.esm" state="Missing"/></dependencies>
                <type name="NotUsable"/></pattern></patterns></dependencyType></typeDescriptor>
            </plugin>
          </plugins>
        </group>
        <group name="Always" type="SelectAll">
          <plugins order="Explicit"><plugin name="Core bits"><files><file source="bits\\a.txt" destination="a.txt"/></files></plugin><plugin name="More bits"/></plugins>
        </group>
        <group name="Solo" type="SelectExactlyOne"><plugins><plugin name="Only"/></plugins></group>
        <group name="Extras" type="SelectAny">
          <plugins order="Explicit">
            <plugin name="Patch A"><files><file source="patches\\PatchA.esp" destination="PatchA.esp"/></files></plugin>
            <plugin name="Patch B">
              <files><file source="patches\\PatchB.esp" destination="PatchB.esp"/><file source="x\\escape.txt" destination="..\\out\\escape.txt"/></files>
            </plugin>
            <plugin name="Folder patches"><files><folder source="loose" destination=""/><folder source="deep" destination="textures"/></files></plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Full only">
      <visible><flagDependency flag="tier" value="full"/></visible>
      <optionalFileGroups>
        <group name="Options" type="SelectAny"><plugins order="Explicit">
          <plugin name="Hi-res"><conditionFlags><flag name="hires">on</flag></conditionFlags></plugin>
          <plugin name="Sounds"/>
        </plugins></group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Hi-res only">
      <visible><flagDependency flag="hires" value="on"/></visible>
      <optionalFileGroups>
        <group name="Res" type="SelectAtMostOne"><plugins order="Explicit"><plugin name="4K"/><plugin name="8K"/></plugins></group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls><patterns>
    <pattern>
      <dependencies operator="And"><flagDependency flag="tier" value="full"/><fileDependency file="Other.esp" state="Active"/></dependencies>
      <files><file source="cond\\Combo.esp" destination="Combo.esp"/></files>
    </pattern>
    <pattern>
      <dependencies operator="Or"><flagDependency flag="tier" value="lite"/><fileDependency file="Skyrim - Extra.esm" state="Active"/></dependencies>
      <files><file source="cond\\LiteOnly.esp" destination="LiteOnly.esp"/></files>
    </pattern>
    <pattern>
      <dependencies operator="And"><fileDependency file="Never.esp" state="Active"/></dependencies>
      <files><file source="cond\\Never.txt" destination="Never.txt"/></files>
    </pattern>
  </patterns></conditionalFileInstalls>
</config>"""

KITCHEN_FILES = {
    "core.esp": plugin(["Skyrim.esm"]),
    "docs/readme.txt": b"hello",
    "full/Full.esp": plugin(["Skyrim.esm", "Update.esm"]),
    "full/tex/a.dds": b"x",
    "lite/Lite.esp": plugin(["Skyrim.esm"]),
    "bits/a.txt": b"x",
    "patches/PatchA.esp": plugin(["Skyrim.esm", "Mod A.esp"]),
    "patches/PatchB.esp": plugin(["Skyrim.esm", "Mod B.esp", "patchb.esp", "Mod A.esp"]),
    "x/escape.txt": b"x",
    "loose/One.esp": plugin(["Skyrim.esm", "Loose.esp"]),
    "loose/Two.esl": plugin(["Skyrim.esm"]),
    "loose/readme.txt": b"x",
    "deep/Hidden.esp": plugin(["Skyrim.esm"]),
    "cond/Combo.esp": plugin(["Skyrim.esm", "Other.esp"]),
    "cond/LiteOnly.esp": plugin(["Skyrim.esm"]),
    "cond/Never.txt": b"x",
}


# -------------------------------------------------------------------- steps and flags


def test_the_event_has_the_native_shape_and_validates():
    result, _ = build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES)
    event = result.event
    validate_event(event)
    assert [s["id"] for s in event["steps"]] == ["step:0", "step:1", "step:2"]
    assert event["source"]["reconstructed"] is True
    assert event["installer"] == {"name": rb.RECONSTRUCTED_INSTALLER, "version": "unknown", "upstream_commit": "unknown"}
    assert event["source"]["archive"] == "kitchen.zip" and event["source"]["reinstallation"] is False
    group_types = {g["name"]: g["type"] for g in event["steps"][0]["groups"]}
    assert group_types == {"Pick": "SelectExactlyOne", "Always": "SelectAll", "Solo": "SelectExactlyOne",
                           "Extras": "SelectAny"}                # the authored type is what is recorded


def test_step_visibility_follows_the_flags_of_the_picked_options():
    full, _ = build(KITCHEN, ids((0, 0, 0), (1, 0, 0), (2, 0, 1)), KITCHEN_FILES)
    assert [s["visible"] for s in full.event["steps"]] == [True, True, True]
    assert selected(full.event) == sorted(["Full", "Core bits", "More bits", "Only", "Hi-res", "8K"])

    lite, _ = build(KITCHEN, ids((0, 0, 1), (1, 0, 0), (2, 0, 0)), KITCHEN_FILES)
    assert [s["visible"] for s in lite.event["steps"]] == [True, False, False]
    # picks inside hidden steps are dropped, with a note
    assert selected(lite.event) == sorted(["Lite", "Core bits", "More bits", "Only"])
    assert any("hidden steps" in w for w in lite.warnings)
    for step in lite.event["steps"][1:]:
        assert all(not o["selected"] for g in step["groups"] for o in g["options"])
    validate_event(lite.event)

    no_pick, _ = build(KITCHEN, ids(), KITCHEN_FILES)
    assert [s["visible"] for s in no_pick.event["steps"]] == [True, False, False]   # empty flag: not "full"


def test_the_nearest_earlier_flag_wins_and_hidden_steps_set_nothing():
    xml = """<config><installSteps order="Explicit">
      <installStep name="One"><optionalFileGroups><group name="G" type="SelectAny"><plugins order="Explicit">
        <plugin name="a"><conditionFlags><flag name="mode">first</flag></conditionFlags></plugin></plugins></group></optionalFileGroups></installStep>
      <installStep name="Two"><optionalFileGroups><group name="G" type="SelectAny"><plugins order="Explicit">
        <plugin name="b"><conditionFlags><flag name="mode">second</flag></conditionFlags></plugin></plugins></group></optionalFileGroups></installStep>
      <installStep name="Hidden"><visible><flagDependency flag="never" value="x"/></visible><optionalFileGroups>
        <group name="G" type="SelectAny"><plugins order="Explicit">
        <plugin name="c"><conditionFlags><flag name="mode">hidden</flag></conditionFlags></plugin></plugins></group></optionalFileGroups></installStep>
      <installStep name="IsSecond"><visible><flagDependency flag="mode" value="second"/></visible><optionalFileGroups>
        <group name="G" type="SelectAny"><plugins order="Explicit"><plugin name="d"/></plugins></group></optionalFileGroups></installStep>
      <installStep name="IsFirst"><visible><flagDependency flag="mode" value="first"/></visible><optionalFileGroups>
        <group name="G" type="SelectAny"><plugins order="Explicit"><plugin name="e"/></plugins></group></optionalFileGroups></installStep>
    </installSteps></config>"""
    result, _ = build(xml, ids((0, 0, 0), (1, 0, 0), (2, 0, 0)))
    assert [s["visible"] for s in result.event["steps"]] == [True, True, False, True, False]
    only_first, _ = build(xml, ids((0, 0, 0)))
    assert [s["visible"] for s in only_first.event["steps"]] == [True, True, False, False, True]


def test_an_or_visibility_and_empty_conditions():
    xml = """<config><installSteps order="Explicit">
      <installStep name="S1"><visible operator="Or"><fileDependency file="A.esp" state="Active"/><flagDependency flag="f" value=""/></visible>
        <optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="a"/></plugins></group></optionalFileGroups></installStep>
      <installStep name="S2"><visible operator="Or"/><optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="b"/></plugins></group></optionalFileGroups></installStep>
    </installSteps></config>"""
    result, _ = build(xml, ids())
    assert [s["visible"] for s in result.event["steps"]] == [True, True]       # unset flag equals ""; empty = always


def test_unset_flag_with_nonempty_value_is_false_and_equal_value_true():
    xml = """<config><installSteps order="Explicit">
      <installStep name="Set"><optionalFileGroups><group name="G" type="SelectAny"><plugins order="Explicit">
        <plugin name="p"><conditionFlags><flag name="x">1</flag></conditionFlags></plugin></plugins></group></optionalFileGroups></installStep>
      <installStep name="Is1"><visible><flagDependency flag="x" value="1"/></visible><optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="a"/></plugins></group></optionalFileGroups></installStep>
      <installStep name="Is2"><visible><flagDependency flag="x" value="2"/></visible><optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="b"/></plugins></group></optionalFileGroups></installStep>
      <installStep name="IsY"><visible><flagDependency flag="y" value="1"/></visible><optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="c"/></plugins></group></optionalFileGroups></installStep>
    </installSteps></config>"""
    assert [s["visible"] for s in build(xml, ids((0, 0, 0)))[0].event["steps"]] == [True, True, False, False]
    assert [s["visible"] for s in build(xml, ids())[0].event["steps"]] == [True, False, False, False]


# ----------------------------------------------------------- enabled / forced / defaults


def test_enabled_follows_group_types_and_plugin_types():
    result, _ = build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES)
    by_name = options(result.event)
    assert by_name["Full"]["enabled"] and by_name["Lite"]["enabled"]
    assert by_name["Locked"]["enabled"] is False            # NotUsable: its file condition (Missing) holds
    assert by_name["Core bits"]["enabled"] is False and by_name["More bits"]["enabled"] is False   # SelectAll
    assert by_name["Only"]["enabled"] is False              # a lone ExactlyOne option becomes SelectAll
    assert by_name["Only"]["selected"] is True              # ... and is always selected
    assert by_name["Patch A"]["enabled"] is True

    active = ConditionEnvironment(file_state={"Absent.esm": "Active"})
    assert options(build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES, environment=active)[0].event)["Locked"]["enabled"]


def test_required_options_are_forced_on_and_exclusive_groups_lock_up():
    xml = """<config><installSteps><installStep name="S"><optionalFileGroups>
      <group name="Exclusive" type="SelectExactlyOne"><plugins order="Explicit">
        <plugin name="first"/><plugin name="forced"><typeDescriptor><type name="Required"/></typeDescriptor></plugin><plugin name="last"/></plugins></group>
      <group name="Many" type="SelectAny"><plugins order="Explicit">
        <plugin name="m1"/><plugin name="m2"><typeDescriptor><type name="Required"/></typeDescriptor></plugin></plugins></group>
    </optionalFileGroups></installStep></installSteps></config>"""
    result, _ = build(xml, ids())
    by_name = options(result.event)
    assert by_name["forced"]["selected"] and by_name["m2"]["selected"]            # forced even with no pick
    assert not by_name["first"]["selected"] and not by_name["m1"]["selected"]
    # Required in an exclusive group disables the controls before it; the ones after re-enable
    # themselves when the installer reaches them (the installer's own order of work).
    assert by_name["first"]["enabled"] is False and by_name["forced"]["enabled"] is False
    assert by_name["last"]["enabled"] is True
    assert by_name["m1"]["enabled"] is True and by_name["m2"]["enabled"] is False
    off, _ = build(xml, ids(), include_forced=False)
    assert not options(off.event)["forced"]["selected"]


def test_defaults_apply_only_when_asked_and_only_to_groups_without_saved_picks():
    metadata = {"steps": [{"name": "Base", "groups": [{"name": "Extras", "plugins": ["Patch A"], "deselected": []}]}]}
    saved = Selection.from_plus_metadata(metadata)
    without, _ = build(KITCHEN, saved, KITCHEN_FILES)
    assert "Full" not in selected(without.event)
    with_defaults, _ = build(KITCHEN, saved, KITCHEN_FILES, fill_defaults=True)
    # Pick is a group Plus did not mention: the Recommended "Full" is the default, so step 2 appears.
    assert "Full" in selected(with_defaults.event) and "Patch A" in selected(with_defaults.event)
    assert with_defaults.event["steps"][1]["visible"] is True
    assert any("default" in n for n in with_defaults.event["source"]["reconstruction_notes"])


def test_a_group_with_a_single_exclusive_option_that_is_required_still_validates():
    xml = """<config><installSteps><installStep name="S"><optionalFileGroups>
      <group name="G" type="SelectAtMostOne"><plugins><plugin name="Solo"/></plugins></group>
    </optionalFileGroups></installStep></installSteps></config>"""
    result, _ = build(xml, ids((0, 0, 0)))
    assert options(result.event)["Solo"]["selected"] and options(result.event)["Solo"]["enabled"]
    assert result.event["steps"][0]["groups"][0]["type"] == "SelectAtMostOne"


# ----------------------------------------------------------------- plugins and masters


def test_plugins_masters_detects_and_files_per_option():
    result, archive = build(KITCHEN, ids((0, 0, 0), (0, 3, 0)), KITCHEN_FILES)
    by_name = options(result.event)

    full = by_name["Full"]
    assert full["plugins"] == ["Full.esp"] and "masters" not in full           # installed: not read
    assert [(f["source"], f["resolved"], f["kind"], f["order"]) for f in full["files"]] == [
        ("full\\Full.esp", "Full.esp", "file", 3), ("full\\tex", "textures", "folder", 4)]

    lite = by_name["Lite"]
    assert lite["plugins"] == ["Lite.esp"] and lite["masters"] == ["Skyrim.esm"]   # skipped: read
    assert lite["files"][0]["resolved"] == "Lite.esp"                              # "Data\" dropped

    patch_b = by_name["Patch B"]
    assert patch_b["plugins"] == ["PatchB.esp"]
    assert patch_b["masters"] == ["Skyrim.esm", "Mod B.esp", "Mod A.esp"]          # no repeats, not itself
    escape = [f for f in patch_b["files"] if f["source"].endswith("escape.txt")][0]
    assert escape["escapes"] is True and escape["resolved"] == ""

    folder = by_name["Folder patches"]
    assert folder["plugins"] == ["One.esp", "Two.esl"]       # plugins directly in a folder that lands in the root
    assert folder["masters"] == ["Skyrim.esm", "Loose.esp"]
    assert "Hidden.esp" not in folder["plugins"]             # destination "textures": not a load-order plugin

    for name in ("Locked", "Only", "Core bits"):
        assert by_name[name]["plugins"] == [] and "masters" not in by_name[name]
    assert by_name["Patch A"]["plugins"] == ["PatchA.esp"] and "masters" not in by_name["Patch A"]   # picked
    assert full["detects"] == [] and by_name["Locked"]["detects"] == []


def test_masters_are_omitted_when_any_header_cannot_be_read():
    files = dict(KITCHEN_FILES)
    files["loose/Two.esl"] = b"not a plugin at all"
    result, _ = build(KITCHEN, ids((0, 0, 0)), files)
    folder = options(result.event)["Folder patches"]
    assert folder["plugins"] == ["One.esp", "Two.esl"] and "masters" not in folder
    assert any("could not be read" in n for n in result.event["source"]["reconstruction_notes"])
    assert "masters" in options(result.event)["Patch A"]


def test_read_masters_modes():
    result, archive = build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES, read_masters="all")
    assert options(result.event)["Full"]["masters"] == ["Skyrim.esm", "Update.esm"]
    result, archive = build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES, read_masters="none")
    assert archive.reads == [] and all("masters" not in o for o in options(result.event).values())
    with pytest.raises(ValueError):
        build(KITCHEN, ids(), KITCHEN_FILES, read_masters="sometimes")


def test_headers_are_read_in_one_batch_of_small_windows():
    _, archive = build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES)
    assert len(archive.reads) == 1 and archive.reads[0][1] == 64 * 1024


def test_a_header_bigger_than_the_first_window_is_read_again_with_the_size_it_needs():
    big = plugin(["Skyrim.esm"], extra=b"".join(b"ZZZZ" + struct.pack("<H", 60000) + b"\0" * 60000 for _ in range(2))
                 + b"MAST" + struct.pack("<H", 8) + b"Late.esp\0"[:8])
    assert len(big) > 64 * 1024
    files = dict(KITCHEN_FILES)
    files["patches/PatchA.esp"] = big
    result, archive = build(KITCHEN, ids((0, 0, 0)), files)
    assert len(archive.reads) == 2 and archive.reads[1][0] == ["patches/PatchA.esp"]
    assert options(result.event)["Patch A"]["masters"] == ["Skyrim.esm", "Late.esp"]


def test_plugin_names_in_the_event_use_the_destination_and_ignore_nonroot_destinations():
    xml = """<config><installSteps><installStep name="S"><optionalFileGroups><group name="G" type="SelectAny"><plugins order="Explicit">
      <plugin name="renamed"><files><file source="a\\orig.esp" destination="Renamed.esp"/></files></plugin>
      <plugin name="nested"><files><file source="a\\orig.esp" destination="sub\\orig.esp"/></files></plugin>
      <plugin name="default"><files><file source="a\\orig.esp"/></files></plugin>
      <plugin name="dir"><files><file source="a\\orig.esp" destination="Data\\"/></files></plugin>
      <plugin name="missing"><files><file source="a\\gone.esp" destination="gone.esp"/></files></plugin>
      <plugin name="case"><files><file source="A\\ORIG.ESP" destination="X.ESP"/><file source="a\\orig.esp" destination="x.esp"/></files></plugin>
      </plugins></group></optionalFileGroups></installStep></installSteps></config>"""
    result, _ = build(xml, ids(), {"a/orig.esp": plugin()}, read_masters="none")
    by_name = options(result.event)
    assert by_name["renamed"]["plugins"] == ["Renamed.esp"]
    assert by_name["nested"]["plugins"] == []
    assert by_name["default"]["plugins"] == []          # destination defaults to the source path: not in the root
    assert by_name["dir"]["plugins"] == ["orig.esp"]    # the file keeps its own name
    assert by_name["missing"]["plugins"] == []
    assert by_name["case"]["plugins"] == ["X.ESP"]      # one plugin; case-insensitive repeat dropped


def test_base_folder_is_where_the_fomods_relative_sources_start():
    files = {f"Mod Name/{path}": data for path, data in KITCHEN_FILES.items()}
    result, _ = build(KITCHEN, ids((0, 0, 0)), files, base="Mod Name")
    assert options(result.event)["Lite"]["plugins"] == ["Lite.esp"]
    wrong, _ = build(KITCHEN, ids((0, 0, 0)), files, base="")
    assert options(wrong.event)["Lite"]["plugins"] == []


# ----------------------------------------------------------- automatic and required


def test_automatic_installs_follow_flags_and_file_states():
    full_other = ConditionEnvironment(file_state={"other.esp": "Active"})          # compared without case
    result, _ = build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES, environment=full_other)
    auto = {a["id"]: a for a in result.event["automatic_installs"]}
    assert set(auto) == {"conditional:0", "conditional:1", "conditional:2"}
    assert auto["conditional:0"]["installed"] is True
    assert auto["conditional:0"]["plugins"] == ["Combo.esp"] and "masters" not in auto["conditional:0"]
    assert auto["conditional:0"]["detects"] == ["Other.esp"]
    assert auto["conditional:1"]["installed"] is False                              # tier is full, not lite
    assert auto["conditional:1"]["masters"] == ["Skyrim.esm"] and auto["conditional:1"]["detects"] == ["Skyrim - Extra.esm"]
    assert auto["conditional:2"]["installed"] is False
    assert auto["conditional:2"]["plugins"] == [] and auto["conditional:2"]["detects"] == ["Never.esp"]
    assert [f["resolved"] for f in auto["conditional:2"]["files"]] == ["Never.txt"]

    lite, _ = build(KITCHEN, ids((0, 0, 1)), KITCHEN_FILES)
    auto = {a["id"]: a for a in lite.event["automatic_installs"]}
    assert auto["conditional:0"]["installed"] is False and auto["conditional:1"]["installed"] is True
    assert "masters" not in auto["conditional:1"] and auto["conditional:0"]["masters"] == ["Skyrim.esm", "Other.esp"]


def test_automatic_install_ids_count_every_pattern_but_empty_ones_are_not_listed():
    xml = """<config><conditionalFileInstalls><patterns>
      <pattern><dependencies><fileDependency file="a.esp" state="Missing"/></dependencies></pattern>
      <pattern><dependencies><fileDependency file="b.esp" state="Active"/></dependencies><files><file source="x" destination="x"/></files></pattern>
    </patterns></conditionalFileInstalls></config>"""
    result, _ = build(xml, ids(), {"x": b"1"})
    assert [a["id"] for a in result.event["automatic_installs"]] == ["conditional:1"]
    none, _ = build("<config/>", ids())
    assert "automatic_installs" not in none.event and "required_files" not in none.event


def test_required_files_are_recorded_in_document_order():
    result, _ = build(KITCHEN, ids(), KITCHEN_FILES)
    required = result.event["required_files"]["files"]
    assert [(f["source"], f["resolved"], f.get("priority"), f["order"]) for f in required] == [
        ("core.esp", "core.esp", None, 1), ("docs", "docs", 1, 2)]


def test_file_conditions_default_to_missing_and_say_so():
    result, _ = build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES)
    assert any("no plugin was installed" in n for n in result.event["source"]["reconstruction_notes"])
    known, _ = build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES, environment=ConditionEnvironment(file_state=lambda name: "Missing"))
    assert not any("no plugin was installed" in n for n in known.event["source"]["reconstruction_notes"])


def test_version_conditions():
    xml = """<config><installSteps order="Explicit">
      <installStep name="G"><visible><gameDependency version="1.6.0"/></visible><optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="a"/></plugins></group></optionalFileGroups></installStep>
      <installStep name="F"><visible><foseDependency version="2.0.0.1"/></visible><optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="b"/></plugins></group></optionalFileGroups></installStep>
      <installStep name="M"><visible><fommDependency version="0.13.21"/></visible><optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="c"/></plugins></group></optionalFileGroups></installStep>
      <installStep name="M2"><visible><fommDependency version="9.0"/></visible><optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="d"/></plugins></group></optionalFileGroups></installStep>
    </installSteps></config>"""
    unknown, _ = build(xml, ids())
    assert [s["visible"] for s in unknown.event["steps"]] == [True, True, True, False]
    old, _ = build(xml, ids(), environment=ConditionEnvironment(game_version="1.5.97.0", fose_version="2.0.0.1"))
    assert [s["visible"] for s in old.event["steps"]] == [False, True, True, False]
    new, _ = build(xml, ids(), environment=ConditionEnvironment(game_version="1.6.1130.0"))
    assert new.event["steps"][0]["visible"] is True


# ------------------------------------------------------------- display order and ids


def test_options_are_listed_in_display_order_with_authored_ids():
    xml = """<config><installSteps><installStep name="S"><optionalFileGroups>
      <group name="G" type="SelectAny"><plugins>
        <plugin name="Charlie"/><plugin name="alpha"/><plugin name="Bravo"/></plugins></group>
      <group name="H" type="SelectAny"><plugins order="Descending"><plugin name="x"/><plugin name="y"/></plugins></group>
    </optionalFileGroups></installStep></installSteps></config>"""
    result, _ = build(xml, ids((0, 0, 0), (0, 1, 1)))
    groups = result.event["steps"][0]["groups"]
    assert [(o["id"], o["name"]) for o in groups[0]["options"]] == [
        ("step:0/group:0/option:2", "Bravo"), ("step:0/group:0/option:0", "Charlie"), ("step:0/group:0/option:1", "alpha")]
    assert [o["name"] for o in groups[1]["options"]] == ["y", "x"]
    assert selected(result.event) == ["Charlie", "y"]                    # picks by authored id


def test_steps_are_listed_in_the_installers_order_with_authored_ids():
    xml = """<config><installSteps><installStep name="b"><optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="p"/></plugins></group></optionalFileGroups></installStep>
      <installStep name="a"><optionalFileGroups><group name="G" type="SelectAny"><plugins><plugin name="p"/></plugins></group></optionalFileGroups></installStep></installSteps></config>"""
    result, _ = build(xml, ids())
    assert [(s["id"], s["name"]) for s in result.event["steps"]] == [("step:1", "a"), ("step:0", "b")]


def test_event_ids_are_deterministic_and_depend_on_the_picks():
    first, _ = build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES)
    again, _ = build(KITCHEN, ids((0, 0, 0)), KITCHEN_FILES)
    other, _ = build(KITCHEN, ids((0, 0, 1)), KITCHEN_FILES)
    assert first.event["event_id"] == again.event["event_id"] != other.event["event_id"]
    assert build(KITCHEN, ids(), KITCHEN_FILES, event_id="11111111-1111-4111-8111-111111111111")[0].event["event_id"] \
        == "11111111-1111-4111-8111-111111111111"


# ------------------------------------------------------------------- the picks formats


METADATA = {"steps": [
    {"name": "Base", "groups": [
        {"name": "Pick", "plugins": ["Lite"], "deselected": ["Full"]},
        {"name": "Always", "plugins": ["Core bits", "More bits"], "deselected": []},
        {"name": "Extras", "plugins": ["Patch B", "Patch A"], "deselected": ["Folder patches"]}]},
    {"name": "Full only", "groups": [{"name": "Options", "plugins": ["Sounds"], "deselected": []}]},
    {"name": "Hi-res only", "groups": [{"name": "Res", "plugins": ["4K"], "deselected": []}]},
]}


def test_fomod_plus_metadata_is_matched_by_name_and_hidden_picks_are_dropped():
    result, _ = build(KITCHEN, Selection.from_plus_metadata(METADATA), KITCHEN_FILES)
    assert selected(result.event) == sorted(["Lite", "Core bits", "More bits", "Only", "Patch A", "Patch B"])
    assert result.event["steps"][1]["visible"] is False
    assert any("hidden steps" in w for w in result.warnings)
    assert any("FOMOD Plus" in n for n in result.event["source"]["reconstruction_notes"])
    # a JSON string works too
    again, _ = build(KITCHEN, Selection.from_plus_metadata(json.dumps(METADATA)), KITCHEN_FILES)
    assert again.event == result.event


def test_plus_metadata_that_does_not_fit_gives_warnings_not_errors():
    odd = {"steps": [{"name": "Base", "groups": [{"name": "Pick", "plugins": ["Gone", "Full"]},
                                                 {"name": "Nope", "plugins": ["x"]}]},
                     {"name": "Not a step", "groups": [{"name": "G", "plugins": ["y"]}]}]}
    result, _ = build(KITCHEN, Selection.from_plus_metadata(odd), KITCHEN_FILES)
    text = " | ".join(result.warnings)
    assert "'Gone'" in text and "'Nope'" in text and "'Not a step'" in text
    assert "Full" in selected(result.event)


def test_plus_metadata_repeated_names_match_in_order():
    xml = """<config><installSteps order="Explicit">
      <installStep name="S"><optionalFileGroups>
        <group name="G" type="SelectAny"><plugins order="Explicit"><plugin name="same"/><plugin name="same"/><plugin name="other"/></plugins></group>
        <group name="G" type="SelectAny"><plugins order="Explicit"><plugin name="same"/></plugins></group>
      </optionalFileGroups></installStep></installSteps></config>"""
    metadata = {"steps": [{"name": "S", "groups": [{"name": "G", "plugins": ["same", "same"]},
                                                   {"name": "G", "plugins": ["same"]}]}]}
    result, _ = build(xml, Selection.from_plus_metadata(metadata))
    flags = [(o["id"], o["selected"]) for o in (o for g in result.event["steps"][0]["groups"] for o in g["options"])]
    assert flags == [("step:0/group:0/option:0", True), ("step:0/group:0/option:1", True),
                     ("step:0/group:0/option:2", False), ("step:0/group:1/option:0", True)]


@pytest.mark.parametrize("payload", [{}, {"steps": "x"}, {"steps": [1]}, {"steps": [{"groups": 3}]},
                                     {"steps": [{"groups": [{"plugins": "x"}]}]}, {"steps": [{"groups": [{"plugins": [1]}]}]},
                                     "not json", [], None])
def test_malformed_plus_metadata_is_rejected(payload):
    with pytest.raises((ValueError, TypeError)):
        Selection.from_plus_metadata(payload)


def test_names_and_ids_selections():
    by_names, _ = build(KITCHEN, Selection.from_names(["lite", "PATCH A", "nonexistent"]), KITCHEN_FILES)
    assert {"Lite", "Patch A"} <= set(selected(by_names.event))
    assert any("name(s) do not exist" in w for w in by_names.warnings)
    by_ids, _ = build(KITCHEN, Selection.from_option_ids(["step:0/group:0/option:0", "step:9/group:9/option:9"]), KITCHEN_FILES)
    assert "Full" in selected(by_ids.event) and any("do not exist" in w for w in by_ids.warnings)


def test_plus_round_trip_reproduces_the_selection():
    base, _ = build(KITCHEN, ids((0, 0, 0), (0, 3, 0), (0, 3, 2), (1, 0, 0), (1, 0, 1), (2, 0, 1)), KITCHEN_FILES)
    config = parse_module_config(KITCHEN.encode())
    metadata = Selection.from_event(base.event).to_plus_metadata(config)
    again, _ = build(KITCHEN, Selection.from_plus_metadata(metadata), KITCHEN_FILES)
    assert again.event["steps"] == base.event["steps"]
    assert again.event["automatic_installs"] == base.event["automatic_installs"]


# ------------------------------------------------------------------------- file caps


def test_file_entry_caps_match_the_native_writer():
    def entry(order, priority=0):
        return {"source": "s", "destination": f"d{order}.dds", "resolved": f"d{order}.dds", "kind": "file",
                "order": order, **({"priority": priority} if priority else {})}

    def many(item_id, count, installed, priority=0):
        return rb._Evidence(item_id, installed=installed, files=[entry(i, priority) for i in range(count)])

    plenty = 1 << 40
    items = [many("small", 3, True), many("none", 0, True), many("big", rb.MAX_FILES_PER_OPTION + 5, True)]
    rb._cap_file_records(items, plenty)
    assert len(items[0].files) == 3 and not items[0].files_truncated
    assert items[1].files == [] and not items[1].files_truncated
    assert len(items[2].files) == rb.MAX_FILES_PER_OPTION and items[2].files_truncated
    assert items[2].files[0]["order"] == 5 and items[2].files[-1]["order"] == rb.MAX_FILES_PER_OPTION + 4

    items = [many(f"many{i}", rb.MAX_FILES_PER_OPTION, True) for i in range(12)]
    rb._cap_file_records(items, plenty)
    assert sum(len(i.files) for i in items) == rb.MAX_FILES_PER_EVENT
    assert len(items[9].files) == rb.MAX_FILES_PER_OPTION and not items[9].files_truncated
    assert items[10].files == [] and items[10].files_truncated and items[11].files_truncated

    # the required files and the installed options are served before the skipped ones
    items = [many("skipped", rb.MAX_FILES_PER_OPTION, False)] + [many(f"on{i}", rb.MAX_FILES_PER_OPTION, True) for i in range(10)]
    required = many("required", 10, True)
    items.append(required)
    rb._cap_file_records(items, plenty)
    assert len(required.files) == 10 and items[0].files == [] and items[0].files_truncated
    assert all(len(i.files) == rb.MAX_FILES_PER_OPTION for i in items[1:10])
    assert len(items[10].files) == rb.MAX_FILES_PER_OPTION - 10 and items[10].files_truncated   # the required files came first

    # winners (highest priority, then latest) survive a cut, in their original order
    item = rb._Evidence("option", installed=True)
    for i in range(rb.MAX_FILES_PER_OPTION + 10):
        item.files.append(entry(1000 + i, i // 100))
    item.files.append(entry(5, 99))
    rb._cap_file_records([item], plenty)
    assert item.files_truncated and len(item.files) == rb.MAX_FILES_PER_OPTION
    assert any(f.get("priority") == 99 for f in item.files)
    assert min(f["order"] for f in item.files if f.get("priority", 0) == 0) == 1011   # the losers went

    # a byte budget cuts as well
    item = many("bytes", 100, True)
    rb._cap_file_records([item], 10 * (rb.FILE_ENTRY_OVERHEAD_BYTES + 30))
    assert item.files_truncated and 0 < len(item.files) < 100
    event_too_big, _ = build(
        "<config><requiredInstallFiles>" + "".join(f'<file source="s{i}" destination="d{i}"/>' for i in range(30)) + "</requiredInstallFiles></config>",
        ids(), {})
    assert len(event_too_big.event["required_files"]["files"]) == 30


def test_option_file_lists_that_were_cut_say_so_and_still_validate():
    big = "".join(f'<file source="s{i}" destination="d{i}.dds"/>' for i in range(2100))
    xml = f"""<config><installSteps><installStep name="S"><optionalFileGroups><group name="G" type="SelectAny"><plugins>
      <plugin name="Big"><files>{big}</files></plugin></plugins></group></optionalFileGroups></installStep></installSteps></config>"""
    config = parse_module_config(xml.encode())
    result = rebuild_event(config, ids((0, 0, 0)), ArchiveTree([]), mod_name="Big", **INSTANCE)
    option = options(result.event)["Big"]
    assert option["files_truncated"] is True and len(option["files"]) == 2000
    validate_event(result.event)


# ------------------------------------------------------------- reading the archive


XML_FOR_ARCHIVE = """<?xml version="1.0" encoding="UTF-8"?>
<config><moduleName>From Archive</moduleName>
  <requiredInstallFiles><file source="Main.esp" destination="Main.esp"/></requiredInstallFiles>
  <installSteps order="Explicit"><installStep name="Patches"><optionalFileGroups><group name="Compat" type="SelectAny"><plugins order="Explicit">
    <plugin name="Village patch"><files><file source="patches\\GP - Village.esp" destination="GP - Village.esp"/></files></plugin>
    <plugin name="Textures"><files><folder source="textures" destination="textures"/></files></plugin>
  </plugins></group></optionalFileGroups></installStep></installSteps>
  <conditionalFileInstalls><patterns><pattern>
    <dependencies><fileDependency file="Lanterns.esp" state="Active"/></dependencies>
    <files><file source="patches\\GP - Lanterns.esp" destination="GP - Lanterns.esp"/></files>
  </pattern></patterns></conditionalFileInstalls>
</config>"""
INFO_XML = '<fomod><Name>Info Name</Name><Version>3.4</Version><Id>4321</Id></fomod>'


def _archive_files(prefix: str = "", *, info: bool = True, fomod_dir: str = "fomod") -> dict[str, bytes]:
    files = {
        f"{fomod_dir}/ModuleConfig.xml": XML_FOR_ARCHIVE.encode("utf-16"),
        "Main.esp": plugin(["Skyrim.esm"]),
        "patches/GP - Village.esp": plugin(["Skyrim.esm", "GrassPatches.esp", "Village.esp"]),
        "patches/GP - Lanterns.esp": plugin(["Skyrim.esm"]),
        "textures/gp/village.dds": b"x" * 20,
    }
    if info:
        files[f"{fomod_dir}/info.xml"] = INFO_XML.encode("utf-8")
    return {f"{prefix}{path}": data for path, data in files.items()}


def _reconstruct(path, **kwargs):
    return reconstruct_from_archive(path, ids((0, 0, 1)), sevenzip_dll=DLL, **INSTANCE, **kwargs)


@pytest.mark.parametrize("kind", ["zip", "7z", "7z-solid", "rar"])
def test_reconstruct_from_each_archive_type(tmp_path, kind):
    if kind != "zip" and DLL is None:
        pytest.skip("MO2's 7z.dll is not available")
    files = _archive_files("Mod Folder/" if kind == "7z" else "", fomod_dir="Fomod" if kind == "rar" else "fomod")
    if kind == "zip":
        path = fx.make_zip(tmp_path / "mod.zip", files)
    elif kind == "rar":
        path = fx.make_rar4(tmp_path / "mod.rar", list(files.items()))
    else:
        path = fx.make_7z(tmp_path / "mod.7z", list(files.items()), solid=kind == "7z-solid",
                          lzma2=kind == "7z-solid")
    result = _reconstruct(path)
    event = result.event
    validate_event(event)
    assert event["mod"] == {"name": "Info Name", "version": "3.4", "nexus_id": 4321}
    assert event["source"]["archive"] == path.name
    by_name = options(event)
    assert by_name["Village patch"]["plugins"] == ["GP - Village.esp"]
    assert by_name["Village patch"]["masters"] == ["Skyrim.esm", "GrassPatches.esp", "Village.esp"]
    assert by_name["Textures"]["selected"] and by_name["Textures"]["plugins"] == []
    auto = event["automatic_installs"][0]
    assert auto["installed"] is False and auto["detects"] == ["Lanterns.esp"] and auto["masters"] == ["Skyrim.esm"]
    assert event["required_files"]["files"][0]["resolved"] == "Main.esp"


def test_reconstruct_lets_the_caller_override_what_info_xml_says(tmp_path):
    path = fx.make_zip(tmp_path / "mod.zip", _archive_files())
    event = _reconstruct(path, mod_name="Mine", mod_version="9", nexus_id=7,
                         completed_at="2026-01-02T03:04:05.000Z").event
    assert event["mod"] == {"name": "Mine", "version": "9", "nexus_id": 7}
    assert event["completed_at"] == event["started_at"] == "2026-01-02T03:04:05.000Z"
    bare = fx.make_zip(tmp_path / "bare.zip", _archive_files(info=False))
    assert _reconstruct(bare).event["mod"] == {"name": "From Archive", "version": "", "nexus_id": 0}


def test_the_fomod_folder_is_found_like_the_installer_finds_it(tmp_path):
    nested = fx.make_zip(tmp_path / "nested.zip", _archive_files("Outer/"))
    assert options(_reconstruct(nested).event)["Village patch"]["plugins"] == ["GP - Village.esp"]
    deeper = fx.make_zip(tmp_path / "deeper.zip", _archive_files("Outer/Inner/"))
    assert options(_reconstruct(deeper).event)["Village patch"]["plugins"] == ["GP - Village.esp"]
    two_roots = _archive_files("Outer/")
    two_roots["Readme.txt"] = b"x"
    with pytest.raises(NoFomodError):               # two top-level entries: the installer would not look inside
        _reconstruct(fx.make_zip(tmp_path / "two.zip", two_roots))
    with pytest.raises(NoFomodError):
        _reconstruct(fx.make_zip(tmp_path / "plain.zip", {"a.esp": plugin()}))
    with pytest.raises(NoFomodError):
        _reconstruct(fx.make_zip(tmp_path / "noconfig.zip", {"fomod/info.xml": b"<fomod/>"}))
    with pytest.raises(NoFomodError):
        _reconstruct(fx.make_zip(tmp_path / "badxml.zip", {"fomod/ModuleConfig.xml": b"<config>"}))
    refused = fx.make_zip(tmp_path / "refused.zip", {"fomod/ModuleConfig.xml": BILLION}, )
    with pytest.raises(NoFomodError, match="DOCTYPE"):
        _reconstruct(refused)


BILLION = b'<?xml version="1.0"?><!DOCTYPE c [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><config>&b;</config>'


@needs_dll
def test_reconstruct_reports_unreadable_archives_with_the_reader_errors(tmp_path):
    encrypted = fx.make_7z(tmp_path / "e.7z", list(_archive_files().items()), encrypt_header=True)
    with pytest.raises(ArchiveEncryptedError):
        _reconstruct(encrypted)
    split = fx.make_rar4(tmp_path / "s.rar", [("a", b"1")], split_after=True)
    with pytest.raises(ArchiveMultiPartError):
        _reconstruct(split)


@needs_dll
def test_a_solid_archive_too_big_to_scan_still_gives_an_event_without_the_masters(tmp_path):
    files = _archive_files()
    config = {k: files[k] for k in ("fomod/ModuleConfig.xml", "fomod/info.xml")}
    rest = {k: v for k, v in files.items() if k not in config}
    entries = list(config.items()) + [("zzz-filler.bin", b"0" * 300_000)] + list(rest.items())
    path = fx.make_7z(tmp_path / "solid.7z", entries, solid=True, lzma2=True)
    from fomod_logger_support.archive.sevenzip import Limits
    result = _reconstruct(path, limits=Limits(max_decoded_bytes=20_000))
    by_name = options(result.event)
    assert by_name["Village patch"]["plugins"] == ["GP - Village.esp"]       # names come from the listing
    assert "masters" not in by_name["Village patch"]                          # the plugin sits behind the filler
    assert any("could not be read" in n for n in result.event["source"]["reconstruction_notes"])
    assert any("too big to scan" in w for w in result.warnings)
    validate_event(result.event)
    with pytest.raises(Exception, match="too big to scan"):                    # the FOMOD itself out of reach
        _reconstruct(path, limits=Limits(max_decoded_bytes=100))


def test_the_unsafe_names_in_an_archive_are_reported(tmp_path):
    files = _archive_files()
    path = fx.make_zip(tmp_path / "mod.zip", files, raw_names=["../evil.esp"])
    result = _reconstruct(path)
    assert any("unsafe names" in n for n in result.event["source"]["reconstruction_notes"])
    assert "evil" not in json.dumps(result.event)


# ------------------------------------------------------- against the native installer

DATABASE = MO2_FOLDER / "plugins" / "data" / "fomod_logger" / "fomod_logger.sqlite3"
SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "installation-event.schema.json"


@pytest.fixture(scope="module")
def native_events(tmp_path_factory):
    if not DATABASE.is_file():
        pytest.skip("the test MO2's Logger database is not available")
    folder = tmp_path_factory.mktemp("nativedb")
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(DATABASE) + suffix)
        if source.is_file():
            shutil.copy2(source, folder / (DATABASE.name + suffix))
    connection = sqlite3.connect(folder / DATABASE.name)
    try:
        rows = connection.execute("SELECT event_id, raw_json FROM events ORDER BY completed_at").fetchall()
    finally:
        connection.close()
    return [json.loads(raw) for _, raw in rows]


def _option_order(event):
    return [(s["id"], [(g["id"], [o["id"] for o in g["options"]]) for g in s["groups"]]) for s in event["steps"]]


def _flat(event):
    out = {}
    for step in event["steps"]:
        out[step["id"]] = {"name": step["name"], "visible": step["visible"]}
        for group in step["groups"]:
            out[group["id"]] = {"name": group["name"], "type": group["type"]}
            for option in group["options"]:
                out[option["id"]] = option
    for auto in event.get("automatic_installs", []):
        out[auto["id"]] = auto
    return out


def differences(native, rebuilt):
    """Fields the native event recorded that the rebuild gets differently."""

    found = []
    if _option_order(native) != _option_order(rebuilt):
        found.append("order")
    flat_native, flat_rebuilt = _flat(native), _flat(rebuilt)
    if set(flat_native) != set(flat_rebuilt):
        found.append(f"ids {sorted(set(flat_native) ^ set(flat_rebuilt))[:3]}")
    for key, item in flat_native.items():
        other = flat_rebuilt.get(key, {})
        for field, value in item.items():
            if value != other.get(field):
                found.append((key, field))
    return found


def _compare(native, **kwargs):
    archive = DOWNLOADS / native["source"]["archive"]
    if not archive.is_file():
        return None
    active = {}
    for auto in native.get("automatic_installs", []):
        if auto["installed"]:
            active.update({d: "Active" for d in auto["detects"]})
    result = reconstruct_from_archive(
        archive, Selection.from_event(native), sevenzip_dll=DLL, instance_id=native["instance"]["id"],
        instance_label=native["instance"]["label"], mod_name=native["mod"]["name"],
        mod_version=native["mod"]["version"], nexus_id=native["mod"]["nexus_id"],
        environment=ConditionEnvironment(file_state=active), **kwargs)
    return result


def test_rebuilt_events_match_the_native_events_for_the_same_picks(native_events):
    import jsonschema
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    compared = {}
    for native in native_events:
        if native["mod"]["name"] not in ("Grass Patches", "Lux - Via (patch hub)", "Bijin Wives SE", "Bijin Warmaidens SE",
                                         "Bijin NPCs SE", "SkyHUD", "Dear Diary"):
            continue
        result = _compare(native)
        if result is None:
            continue
        validate_event(result.event)
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(result.event)
        # Builds before the authored-order id fix numbered options in display order; their ids
        # cannot line up with any rebuild.
        if _option_order(native) != _option_order(result.event):
            compared.setdefault("old id scheme", []).append(native["event_id"])
            continue
        diff = differences(native, result.event)
        # Known, intended difference: builds before the masters rule changed wrote `masters: []`
        # for options with no plugins; the current installer leaves the key out.
        flat_native = _flat(native)
        diff = [d for d in diff if not (isinstance(d, tuple) and d[1] == "masters" and flat_native[d[0]]["masters"] == [])]
        assert diff == [], (native["mod"]["name"], native["event_id"], diff[:5])
        compared.setdefault(native["mod"]["name"], []).append(native["event_id"])
    compared.pop("old id scheme", None)
    if not compared:
        pytest.skip("none of the test MO2's archives for current-format events are in its downloads")
    # Every mod with a current-format event was compared with no differences (the loop asserts it).
    assert sum(len(v) for v in compared.values()) >= 1


def test_the_masters_limits_match_the_native_installer_and_can_be_lifted(native_events):
    current = []
    for event in native_events:
        if event["mod"]["name"] != "Bijin Warmaidens SE":
            continue
        rebuilt = _compare(event)
        if rebuilt is not None and _option_order(event) == _option_order(rebuilt.event):
            current.append((event, rebuilt.event))
    if not current:
        pytest.skip("no current-format Warmaidens event with its archive")
    native, rebuilt = current[-1]
    skipped_natively = [o for o in options(native).values() if not o["selected"] and o["plugins"] and "masters" not in o]
    assert skipped_natively            # the 196 MB archive is over the native installer's 128 MiB limit
    # by default the rebuild gives up on masters there too ...
    assert all("masters" not in options(rebuilt)[o["name"]] for o in skipped_natively)
    # ... and a caller can lift the limit to read them
    lifted = _compare(native, masters_max_archive_bytes=None).event
    assert all("masters" in options(lifted)[o["name"]] for o in skipped_natively)


# ------------------------------------------------------ review fixes: forced picks, "None", limits


def test_a_saved_pick_beats_a_required_option_in_an_exclusive_group():
    xml = """<config><installSteps><installStep name="S"><optionalFileGroups>
      <group name="Exclusive" type="SelectExactlyOne"><plugins order="Explicit">
        <plugin name="first"/><plugin name="forced"><typeDescriptor><type name="Required"/></typeDescriptor></plugin><plugin name="last"/></plugins></group>
      <group name="AtMost" type="SelectAtMostOne"><plugins order="Explicit">
        <plugin name="x"/><plugin name="forcedToo"><typeDescriptor><type name="Required"/></typeDescriptor></plugin></plugins></group>
      <group name="Many" type="SelectAny"><plugins order="Explicit">
        <plugin name="m1"/><plugin name="m2"><typeDescriptor><type name="Required"/></typeDescriptor></plugin></plugins></group>
    </optionalFileGroups></installStep></installSteps></config>"""
    result, _ = build(xml, ids((0, 0, 2), (0, 1, 0), (0, 2, 0)))
    by_name = options(result.event)
    assert by_name["last"]["selected"] and not by_name["forced"]["selected"]
    assert by_name["x"]["selected"] and not by_name["forcedToo"]["selected"]
    assert by_name["m1"]["selected"] and by_name["m2"]["selected"]          # not exclusive: both stay
    assert sum("conflicts with the saved pick" in w for w in result.warnings) == 2
    for group in result.event["steps"][0]["groups"][:2]:
        assert sum(o["selected"] for o in group["options"]) == 1
    # no saved pick in the group: the Required option still applies, without a warning
    quiet, _ = build(xml, ids())
    assert options(quiet.event)["forced"]["selected"] and not quiet.warnings
    # and the picked Required option itself is no conflict
    same, _ = build(xml, ids((0, 0, 1)))
    assert options(same.event)["forced"]["selected"] and not same.warnings


def test_fomod_plus_none_choice_is_ignored_in_at_most_one_groups():
    xml = """<config><installSteps><installStep name="S"><optionalFileGroups>
      <group name="AtMost" type="SelectAtMostOne"><plugins order="Explicit"><plugin name="a"/><plugin name="b"/></plugins></group>
      <group name="Authored" type="SelectAtMostOne"><plugins order="Explicit"><plugin name="None"/><plugin name="c"/></plugins></group>
      <group name="Lone" type="SelectAtMostOne"><plugins><plugin name="d"/></plugins></group>
      <group name="Any" type="SelectAny"><plugins><plugin name="e"/><plugin name="f"/></plugins></group>
    </optionalFileGroups></installStep></installSteps></config>"""
    metadata = {"steps": [{"name": "S", "groups": [
        {"name": "AtMost", "plugins": ["None"]}, {"name": "Authored", "plugins": ["None"]},
        {"name": "Lone", "plugins": ["None"]}, {"name": "Any", "plugins": ["None"]}]}]}
    result, _ = build(xml, Selection.from_plus_metadata(metadata))
    text = " | ".join(result.warnings)
    assert "(group 'AtMost')" not in text                         # synthetic button: no warning
    assert "(group 'Authored')" not in text                       # an authored "None" is just an option
    assert options(result.event)["None"]["selected"]
    assert "(group 'Lone')" in text and "(group 'Any')" in text   # elsewhere it really is unknown
    assert not any(o["selected"] for g in result.event["steps"][0]["groups"][:1] for o in g["options"])


def test_a_reader_error_while_reading_headers_leaves_masters_unread():
    from fomod_logger_support.archive.sevenzip import ArchiveTimeoutError

    def failing(paths, window):
        raise ArchiveTimeoutError("took too long to read")

    config = parse_module_config(KITCHEN.encode())
    tree = ArchiveTree(KITCHEN_FILES, failing)
    result = rebuild_event(config, ids((0, 0, 0)), tree, mod_name="Kitchen", **INSTANCE)
    by_name = options(result.event)
    assert by_name["Lite"]["plugins"] == ["Lite.esp"] and "masters" not in by_name["Lite"]
    assert any("could not be read" in n for n in result.event["source"]["reconstruction_notes"])
    assert any("took too long" in w for w in result.warnings)
    validate_event(result.event)


def test_masters_are_skipped_for_big_archives_and_many_plugins_like_the_native_installer():
    config = parse_module_config(KITCHEN.encode())
    archive = Files(KITCHEN_FILES)

    def run(**kwargs):
        return rebuild_event(config, ids((0, 0, 0)), archive.tree(), mod_name="Kitchen", **INSTANCE, **kwargs)

    plain = run()
    assert "masters" in options(plain.event)["Lite"] and archive.reads
    archive.reads.clear()
    big = run(archive_bytes=rb.MASTERS_MAX_ARCHIVE_BYTES + 1)
    assert archive.reads == [] and all("masters" not in o for o in options(big.event).values())
    assert any("masters were not read" in n for n in big.event["source"]["reconstruction_notes"])
    assert "masters" in options(run(archive_bytes=rb.MASTERS_MAX_ARCHIVE_BYTES).event)["Lite"]   # at the limit: read
    assert "masters" in options(run(archive_bytes=10**10, masters_max_archive_bytes=None).event)["Lite"]
    many = run(masters_max_plugins=2)
    assert all("masters" not in o for o in options(many.event).values())
    assert any("more than 2 plugins" in n for n in many.event["source"]["reconstruction_notes"])
    assert "masters" in options(run(masters_max_plugins=None).event)["Lite"]
    assert rb.MASTERS_MAX_PLUGINS == 400 and rb.MASTERS_MAX_ARCHIVE_BYTES == 128 * 1024 * 1024


def test_reconstruct_from_archive_passes_the_archive_size_to_the_masters_gate(tmp_path):
    path = fx.make_zip(tmp_path / "mod.zip", _archive_files())
    result = _reconstruct(path, masters_max_archive_bytes=10)
    assert "masters" not in options(result.event)["Village patch"]
    assert "masters" in options(_reconstruct(path).event)["Village patch"]
