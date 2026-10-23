"""Mods that were not installed through a logged installer: the record (`core/plain_record`), how the store keeps and
supersedes it, the file lookup, compare and missing masters over it, and how both editions notice and record a plain
install and scan the mods that have no record."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from fomod_logger_support import executor as background
from fomod_logger_support.core import compare_choices as cc
from fomod_logger_support.core import plain_record as pr
from fomod_logger_support.core.export_choices import current_events
from fomod_logger_support.core.installer_changes import installer_changes
from fomod_logger_support.core.missed_patches import find_missed_patches
from fomod_logger_support.core.storage import EventStore, EventValidationError, validate_event

ROOT = Path(__file__).resolve().parents[2]
INSTANCE = "b" * 64
OTHER_INSTANCE = "c" * 64


def _live_fixtures():
    import importlib.util
    spec = importlib.util.spec_from_file_location("live_fixtures", ROOT / "tests" / "live" / "fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plugin_bytes(*masters: str) -> bytes:
    return _live_fixtures().plugin_bytes(tuple(masters))


def make_mod(folder: Path, files: dict[str, bytes | str] | None = None) -> Path:
    """A mod folder with files (str content is text) and the meta.ini MO2 keeps in every one."""

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.ini").write_text("[General]\n", encoding="utf-8")
    for name, content in (files or {}).items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return folder


def job(folder: Path, name: str = "Lantern", *, kind: str = "install", when: str = "2026-10-21T12:00:00.000Z",
        archive: str = "Lantern-1.0.zip", version: str = "1.0", instance: str = INSTANCE, nexus: int = 4242) -> pr.PlainJob:
    return pr.PlainJob(instance_id=instance, instance_label="MO2 Test", mod_name=name, mod_version=version,
                       nexus_id=nexus, game="skyrimspecialedition", archive=archive, folder=str(folder), when=when,
                       kind=kind)


_made = iter(range(1, 100_000))


def plain_event(tmp_path: Path, name: str = "Lantern", files=None, **kwargs) -> dict:
    folder = make_mod(tmp_path / "mods" / f"{name}-{next(_made)}", files if files is not None else {
        "Lantern.esp": plugin_bytes("Skyrim.esm"), "Textures/Lantern/lamp.dds": "dds", "meshes/lamp.nif": "nif"})
    result = pr.run_job(job(folder, name, **kwargs))
    assert result.mode == pr.PLAIN, result.reason
    return result.event


def fomod_event(name: str = "Lantern", *, archive: str = "Lantern-1.0.zip", number: int = 1,
                completed: str = "2026-10-21T12:00:05+00:00", instance: str = INSTANCE, picked: bool = True,
                plugin: str = "") -> dict:
    option = {"id": "step:0/group:0/option:0", "name": "Main", "selected": picked, "enabled": True}
    if plugin:
        option |= {"plugins": [plugin], "masters": ["Skyrim.esm"], "detects": []}
    return {
        "schema_version": 1, "event_id": f"{number:08d}-1111-4111-8111-111111111111",
        "started_at": completed, "completed_at": completed,
        "instance": {"id": instance, "label": "MO2 Test"},
        "source": {"archive": archive, "reinstallation": False},
        "mod": {"name": name, "version": "1.0", "nexus_id": 4242},
        "installer": {"name": "FOMOD Logger Native Installer", "version": "1.0.0",
                      "upstream_commit": "fc263f2d923c704b4853c11ed4f8b8cf3920f30d"},
        "steps": [{"id": "step:0", "name": "Main", "visible": True, "groups": [
            {"id": "step:0/group:0", "name": "Options", "type": "SelectAny", "options": [option]}]}],
    }


# ---------- making the record ----------

def test_a_record_holds_the_mods_files_plugins_and_masters_and_leaves_out_mo2s_meta_ini(tmp_path):
    event = plain_event(tmp_path)
    plain = event["plain"]
    assert event["kind"] == "plain" and event["steps"] == [] and plain["origin"] == "install"
    assert plain["files"] == ["Lantern.esp", "meshes/lamp.nif", "Textures/Lantern/lamp.dds"]    # each folder's files, then its folders
    assert plain["file_count"] == 3 and plain["files_truncated"] is False and plain["game"] == "skyrimspecialedition"
    assert plain["plugins"] == [{"name": "Lantern.esp", "masters": ["Skyrim.esm"]}]
    assert event["mod"] == {"name": "Lantern", "version": "1.0", "nexus_id": 4242}
    assert event["source"] == {"archive": "Lantern-1.0.zip", "reinstallation": False}
    assert event["installer"]["name"] == pr.INSTALLER_NAME
    assert event["started_at"] == event["completed_at"] == "2026-10-21T12:00:00.000Z"
    assert validate_event(deepcopy(event))


def test_the_same_folder_gives_the_same_record_and_each_install_has_its_own_id(tmp_path):
    folder = make_mod(tmp_path / "m", {"a.txt": "a"})
    first = pr.run_job(job(folder, when="2026-10-21T12:00:00.000Z")).event
    assert pr.run_job(job(folder, when="2026-10-21T12:00:00.000Z")).event == first
    assert pr.run_job(job(folder, when="2026-10-21T12:00:01.000Z")).event["event_id"] != first["event_id"]
    scanned = pr.run_job(job(folder, kind="scan", when="")).event
    assert scanned["plain"]["origin"] == "scan" and scanned["completed_at"].endswith("Z")         # dated by the folder
    # A scan makes a new id each time: a mod that was removed and came back can be recorded again.
    assert pr.run_job(job(folder, kind="scan", when="")).event["event_id"] != scanned["event_id"]


def test_a_removed_mod_that_comes_back_is_recorded_again_by_the_next_scan(tmp_path):
    folder = make_mod(tmp_path / "m", {"a.txt": "a"})
    with EventStore(tmp_path / "db") as store:
        first = pr.run_job(job(folder, kind="scan", when="")).event
        assert store.ingest(first) is True
        assert store.mark_removed(INSTANCE, "Lantern", "2026-10-22T10:00:00+00:00") is True
        assert store.current_mods(INSTANCE) == []
        again = pr.run_job(job(folder, kind="scan", when="")).event
        assert store.ingest(again) is True                      # not "already stored", not "conflicts with other content"
        [current] = store.latest("")
        assert current["event_id"] == again["event_id"] and len(store.history("", include_hidden=True)) == 2


def test_a_plugin_whose_header_cannot_be_read_has_no_masters_not_an_empty_list(tmp_path):
    folder = make_mod(tmp_path / "m", {"Broken.esp": b"not a plugin", "Fine.esm": plugin_bytes(), "sub/Deep.esp": plugin_bytes(),
                                       "note.txt": "x"})
    plugins = pr.run_job(job(folder)).event["plain"]["plugins"]
    assert plugins == [{"name": "Broken.esp"}, {"name": "Fine.esm", "masters": []}]      # only the root counts for MO2


def test_the_file_list_stops_at_the_limits_and_says_so(tmp_path):
    folder = make_mod(tmp_path / "m", {f"d/f{n:03}.txt": "x" for n in range(30)})
    files, cut = pr.walk_files(folder, max_files=10)
    assert len(files) == 10 and cut is True and files == sorted(files)
    files, cut = pr.walk_files(folder, byte_budget=40)
    assert 0 < len(files) < 30 and cut is True
    files, cut = pr.walk_files(folder)
    assert len(files) == 30 and cut is False
    event = pr.build_event(job(folder), files[:5], True, [])
    assert event["plain"]["files_truncated"] is True and event["plain"]["file_count"] == 5


def test_links_are_not_followed_and_an_unreadable_folder_is_a_failure_not_a_crash(tmp_path):
    folder = make_mod(tmp_path / "m", {"a.txt": "a"})
    result = pr.run_job(job(tmp_path / "missing"))
    assert result.mode == pr.FAILED and "Lantern" in result.reason and result.event is None
    outside = make_mod(tmp_path / "outside", {"secret.txt": "x"})
    link = folder / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:                       # no right to make a symbolic link: a junction needs none
        import subprocess
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        if made.returncode != 0:
            pytest.skip("cannot make a link here")
    assert pr.walk_files(folder)[0] == ["a.txt"]


def test_a_cancelled_walk_stops_inside_a_big_folder(tmp_path):
    folder = make_mod(tmp_path / "m", {f"d{n}/f.txt": "x" for n in range(600)})
    stop = threading.Event()
    stop.set()
    assert pr.run_job(job(folder), stop).mode == pr.CANCELLED
    assert pr.run_job(job(folder), threading.Event()).mode == pr.PLAIN


def test_the_digest_follows_the_files_not_their_case_or_order():
    assert pr.files_digest(["A.esp", "b/C.dds"]) == pr.files_digest(["B/c.dds", "a.esp"])
    assert pr.files_digest(["a.esp"]) != pr.files_digest(["a.esp", "b.esp"])


# ---------- the schema and the store ----------

def _schema_errors(event: dict) -> list:
    schema = json.loads((ROOT / "schemas" / "installation-event.schema.json").read_text(encoding="utf-8"))
    return list(Draft202012Validator(schema).iter_errors(event))


def test_the_schema_takes_a_plain_record_additively_and_refuses_a_malformed_one(tmp_path, installation_event):
    event = plain_event(tmp_path)
    assert _schema_errors(event) == [] and _schema_errors(installation_event) == [] and event["schema_version"] == 1
    for damage in (lambda e: e["plain"].pop("origin"), lambda e: e["plain"].update(extra=1),
                   lambda e: e.update(kind="other"), lambda e: e["plain"]["plugins"].append({"masters": []}),
                   lambda e: e["plain"].update(origin="never")):
        broken = deepcopy(event)
        damage(broken)
        assert _schema_errors(broken), damage
    stored = deepcopy(event)
    stored["plain"].pop("files")
    assert _schema_errors(stored) == []             # as the database keeps it: the count and the digest only


def test_the_store_keeps_the_files_in_their_own_table_and_hands_them_back(tmp_path):
    event = plain_event(tmp_path)
    with EventStore(tmp_path / "db") as store:
        assert store.ingest(event) is True and store.ingest(event) is False
        stored = store.event(event["event_id"])
        assert "files" not in stored["plain"] and stored["plain"]["file_count"] == 3          # a read stays cheap
        assert store.plain_files(event["event_id"]) == event["plain"]["files"]
        assert store.with_files(stored)["plain"]["files"] == event["plain"]["files"]
        assert store.with_files(fomod_event()) == fomod_event()                                 # any other event is as it was
        exported = store.full_history_export()
    [exported_event] = exported["events"]
    assert exported_event["plain"]["files"] == event["plain"]["files"]
    assert _schema_errors({k: v for k, v in exported_event.items() if k not in ("hidden", "removed_at")}) == []
    assert "plain" in event and event["plain"]["files"]                                       # the caller's event is untouched


def test_validation_refuses_a_plain_record_with_steps_a_wrong_count_or_an_installer_record_with_plain(tmp_path):
    event = plain_event(tmp_path)
    for damage in (lambda e: e["steps"].append({}), lambda e: e["plain"].update(file_count=99),
                   lambda e: e["plain"]["files"].append(""), lambda e: e["plain"].update(files="x")):
        broken = deepcopy(event)
        damage(broken)
        with pytest.raises(EventValidationError):
            validate_event(broken)
    mixed = fomod_event()
    mixed["plain"] = deepcopy(event["plain"])
    with pytest.raises(EventValidationError):
        validate_event(mixed)


def test_a_plain_record_is_the_mods_current_record_and_search_finds_it_by_name_archive_and_plugin(tmp_path):
    event = plain_event(tmp_path)
    with EventStore(tmp_path / "db") as store:
        store.ingest(event)
        [only] = store.latest("")
        assert only["kind"] == "plain" and only["removed_at"] is None
        assert [e["event_id"] for e in store.latest("lantern")] == [event["event_id"]]          # name
        assert len(store.latest("lantern-1.0.zip")) == 1 and len(store.latest("Lantern.esp")) == 1
        assert store.latest("nothing like it") == []
        [mod] = store.current_mods(INSTANCE)
        assert (mod["name"], mod["archive"], mod["kind"]) == ("Lantern", "Lantern-1.0.zip", "plain")


def test_removing_a_mod_marks_its_plain_record_removed_and_renaming_follows(tmp_path):
    event = plain_event(tmp_path)
    with EventStore(tmp_path / "db") as store:
        store.ingest(event)
        assert store.rename_mod(INSTANCE, "Lantern", "Lantern Renamed") is True
        assert store.latest("")[0]["mod"]["name"] == "Lantern Renamed"
        assert store.mark_removed(INSTANCE, "lantern renamed", "2026-10-22T10:00:00+00:00") is True
        assert store.latest("") == [] and store.current_mods(INSTANCE) == []
        [kept] = store.history("", include_hidden=True)
        assert kept["removed_at"] == "2026-10-22T10:00:00+00:00"


# ---------- an installer's record and a plain one of the same mod ----------

def test_a_newer_installer_record_takes_the_place_of_a_plain_one_and_the_plain_one_stays_in_the_history(tmp_path):
    plain = plain_event(tmp_path, archive="Lantern-1.0.zip", when="2026-10-01T12:00:00.000Z")
    with EventStore(tmp_path / "db") as store:
        store.ingest(plain)
        assert store.ingest(fomod_event(archive="Lantern-2.0.zip", completed="2026-10-21T12:00:00+00:00")) is True
        assert [e["kind"] if "kind" in e else "fomod" for e in store.latest("")] == ["fomod"]
        assert len(store.history("", include_hidden=True)) == 2                                  # a real reinstall is history
        assert store.current_mods(INSTANCE)[0]["kind"] == "fomod"


def test_a_later_plain_reinstall_takes_the_place_of_an_installer_record(tmp_path):
    with EventStore(tmp_path / "db") as store:
        store.ingest(fomod_event(archive="Lantern-1.0.zip", completed="2026-10-01T12:00:00+00:00"))
        store.ingest(plain_event(tmp_path, archive="Lantern-2.0.zip", when="2026-10-21T12:00:00.000Z"))
        assert [e.get("kind") for e in store.latest("")] == ["plain"]
        assert store.current_mods(INSTANCE)[0]["kind"] == "plain"


def test_a_plain_record_made_for_the_same_install_as_an_installer_record_is_dropped_when_that_arrives_late(tmp_path):
    plain = plain_event(tmp_path, archive="Lantern-1.0.zip", when="2026-10-21T12:00:03.000Z")
    other_mod = plain_event(tmp_path, name="Other", archive="Lantern-1.0.zip", when="2026-10-21T12:00:03.000Z")
    older = plain_event(tmp_path, name="Lantern", archive="Lantern-1.0.zip", when="2026-09-01T12:00:00.000Z")
    elsewhere = plain_event(tmp_path, name="Lantern", archive="Lantern-1.0.zip", when="2026-10-21T12:00:04.000Z",
                            instance=OTHER_INSTANCE)
    with EventStore(tmp_path / "db") as store:
        for event in (plain, other_mod, older, elsewhere):
            store.ingest(event)
        store.save_review({"event_id": plain["event_id"], "option_id": "", "reason": "x", "state": "pending"})
        store.ingest(fomod_event(archive="LANTERN-1.0.zip", completed="2026-10-21T12:00:01+00:00"))
        left = {(e["mod"]["name"], e["event_id"]) for e in store.history("", include_hidden=True)}
        assert (("Lantern", plain["event_id"]) not in left and ("Other", other_mod["event_id"]) in left
                and ("Lantern", older["event_id"]) in left)              # the other mod, an earlier install...
        assert any(e["event_id"] == elsewhere["event_id"] for e in store.history("", include_hidden=True))   # ...and another instance stay
        assert store.plain_files(plain["event_id"]) == [] and store.reviews() == []
        assert [e.get("kind") for e in store.latest("lantern") if e["instance"]["id"] == INSTANCE
                and e["mod"]["name"] == "Lantern"] == [None]


def test_at_an_exact_tie_the_installer_record_is_the_current_one(tmp_path):
    with EventStore(tmp_path / "db") as store:
        store.ingest(plain_event(tmp_path, archive="Other-1.zip", when="2026-10-21T12:00:00.000Z"))
        store.ingest(fomod_event(archive="Lantern-9.zip", completed="2026-10-21T12:00:00.000Z"))
        assert [e.get("kind") for e in store.latest("")] == [None]


def test_installer_record_of_finds_the_install_that_just_happened(tmp_path):
    with EventStore(tmp_path / "db") as store:
        store.ingest(fomod_event(archive="C:\\dl\\Lantern-1.0.zip", completed="2026-10-21T12:00:00+00:00"))
        stamp = pr_seconds("2026-10-21T11:59:00+00:00")
        assert store.installer_record_of(INSTANCE, "lantern", "Lantern-1.0.zip", stamp)["mod"]["name"] == "Lantern"
        assert store.installer_record_of(INSTANCE, "Lantern", "Lantern-1.0.zip", pr_seconds("2026-10-21T12:00:01+00:00")) is None
        assert store.installer_record_of(INSTANCE, "Lantern", "Other.zip", stamp) is None
        assert store.installer_record_of(INSTANCE, "Other", "Lantern-1.0.zip", stamp) is None
        store.ingest(plain_event(tmp_path, name="Lantern", archive="Lantern-2.0.zip"))
        assert store.installer_record_of(INSTANCE, "Lantern", "Lantern-2.0.zip", 0) is None      # a plain record is no installer's


def pr_seconds(text: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(text).timestamp()


# ---------- which mod installed this file ----------

def _lookup_store(tmp_path) -> EventStore:
    store = EventStore(tmp_path / "db")
    store.ingest(plain_event(tmp_path))
    store.ingest(plain_event(tmp_path, name="Rugs", files={"Textures/Lantern/rug.dds": "x", "Rugs.esp": plugin_bytes()},
                             archive="Rugs.zip"))
    return store


def test_a_file_in_a_mod_without_an_installer_is_answered_by_the_mod(tmp_path):
    with _lookup_store(tmp_path) as store:
        for query in ("Textures/Lantern/lamp.dds", "textures\\lantern\\LAMP.DDS", "Data/Textures/Lantern/lamp.dds",
                      "C:/MO2/mods/Lantern/Textures/Lantern/lamp.dds", "lamp.dds", "Lantern.esp"):
            [match] = store.file_origins(query)["matches"]
            assert match["mod_name"] == "Lantern" and match["text"] == "Lantern (not installed through an installer)", query
            assert match["plain"] is True and match["picked"] is True and match["automatic"] is False
        answer = store.file_origins("Textures/Lantern")                       # a folder: both mods have files in it
        assert sorted(m["mod_name"] for m in answer["matches"]) == ["Lantern", "Rugs"]
        assert store.file_origins("nothing/here.dds")["matches"] == []
        detail = store.file_origins("Lantern.esp")["matches"][0]["detail"]
        assert detail.startswith("has Lantern.esp") and "1.0" in detail and "Lantern-1.0.zip" in detail


def test_the_lookup_covers_each_mods_current_record_only_when_asked_and_skips_removed_mods(tmp_path):
    older = plain_event(tmp_path, when="2026-09-01T12:00:00.000Z", files={"old.txt": "x"})
    with _lookup_store(tmp_path) as store:
        store.ingest(older)
        assert [m["path"] for m in store.file_origins("old.txt")["matches"]] == ["old.txt"]            # in the history
        assert store.file_origins("old.txt", current=True, instance_id=INSTANCE)["matches"] == []       # not in the current record
        assert len(store.file_origins("lamp.dds", current=True, instance_id=INSTANCE)["matches"]) == 1
        assert store.file_origins("lamp.dds", current=True, instance_id=OTHER_INSTANCE)["matches"] == []
        store.mark_removed(INSTANCE, "Lantern", "2026-10-22T10:00:00+00:00")
        assert store.file_origins("lamp.dds", current=True, instance_id=INSTANCE)["matches"] == []


def test_a_search_for_a_path_finds_plain_mods_and_mixes_with_installer_answers(tmp_path):
    with _lookup_store(tmp_path) as store:
        store.ingest(fomod_event(name="Mountains", archive="M.zip", number=7, plugin="Peak.esp"))
        assert [e["mod"]["name"] for e in store.latest("Textures/Lantern/lamp.dds")] == ["Lantern"]
        assert [e["mod"]["name"] for e in store.latest("Peak.esp")] == ["Mountains"]
        answer = store.file_origins("Peak.esp")
        assert [m.get("plain", False) for m in answer["matches"]] == [False]
        notes = " ".join(answer["notes"])                    # Mountains has no file data; the plain mods are not "unrecorded"
        assert "Mountains" in notes and "Lantern" not in notes and "Rugs" not in notes


def test_a_cut_short_file_list_is_said_in_the_answer(tmp_path):
    folder = make_mod(tmp_path / "m", {f"f{n}.txt": "x" for n in range(5)})
    files, _ = pr.walk_files(folder)
    cut = pr.build_event(job(folder), files[:2], True, [])
    with EventStore(tmp_path / "db") as store:
        store.ingest(cut)
        answer = store.file_origins("f0.txt")
        assert answer["truncated"] == ["Lantern"] and any("cut short for Lantern" in note for note in answer["notes"])


def test_the_matching_helper_picks_the_rows_a_lookup_found():
    paths = ["a/b.dds", "c.esp", "A/b/c.txt", "z/b.dds"]
    assert pr_matching(paths, "a/b.dds") == {"a/b.dds"}
    assert pr_matching(paths, "b.dds") == {"a/b.dds", "z/b.dds"}
    assert pr_matching(paths, "a/b") == {"A/b/c.txt"}
    assert pr_matching(paths, "") == set()


def pr_matching(paths, query):
    from fomod_logger_support.core.file_origin import plain_path_matches
    return plain_path_matches(paths, query)


# ---------- compare ----------

def test_two_plain_installs_differ_by_version_archive_plugins_and_files(tmp_path):
    a = plain_event(tmp_path, name="Lantern", version="1.0", archive="L-1.zip", files={"a.txt": "x", "same.txt": "x",
                                                                                    "Old.esp": plugin_bytes()})
    b = plain_event(tmp_path, name="Lantern", version="1.1", archive="L-2.zip", when="2026-10-22T12:00:00.000Z",
                    files={"b.txt": "x", "same.txt": "x", "New.esp": plugin_bytes()})
    result = cc.compare_installs(a, b)
    plain = result["plain"]
    assert result["status"] == cc.DIFFERENT and plain["both_plain"] and plain["count"] == 4
    assert plain["version"] == ["1.0", "1.1"] and plain["version_differs"] and plain["archive_differs"]
    assert plain["plugins"] == {"only_a": ["Old.esp"], "only_b": ["New.esp"]}
    assert plain["files"]["differs"] and (plain["files"]["count_a"], plain["files"]["count_b"]) == (3, 3)
    assert cc.describe(result) == "These differ: the version (1.0 → 1.1), the archive, the plugins and the files."
    diff = cc.file_differences(a["plain"]["files"], b["plain"]["files"])
    assert (diff["only_a"], diff["only_b"], diff["same"]) == (["a.txt", "Old.esp"], ["b.txt", "New.esp"], 1)


def test_two_identical_plain_installs_are_the_same_and_say_so(tmp_path):
    a = plain_event(tmp_path)
    b = plain_event(tmp_path, when="2026-10-22T12:00:00.000Z")
    result = cc.compare_installs(a, b)
    assert result["status"] == cc.SAME and result["plain"]["count"] == 0
    assert cc.describe(result) == "Nothing differs: the same version, archive, plugins and files."
    assert cc.compare_installs(fomod_event(), fomod_event(number=2))["plain"] is None          # installers are as before


def test_a_plain_install_against_an_installer_install_says_which_had_an_installer(tmp_path):
    result = cc.compare_installs(fomod_event(), plain_event(tmp_path))
    assert result["status"] == cc.DIFFERENT and result["plain"]["installer"] == "a" and result["plain"]["files"] is None
    assert cc.describe(result).startswith("One was installed through an installer and the other was not")


def test_instances_are_compared_over_plain_mods_too(tmp_path):
    a = plain_event(tmp_path, instance=INSTANCE, version="1.0")
    b = plain_event(tmp_path, instance=OTHER_INSTANCE, version="1.2", archive="Lantern-1.2.zip")
    c = plain_event(tmp_path, name="Only Here", instance=INSTANCE)
    result = cc.compare_instances([a, b, c], INSTANCE, OTHER_INSTANCE)
    by_name = {entry["name"]: entry for entry in result["mods"]}
    assert by_name["Lantern"]["status"] == cc.DIFFERENT and by_name["Lantern"]["paired_by"] == "nexus"
    assert by_name["Only Here"]["status"] == cc.ONLY_A
    # Two mods from one archive are copies of each other, whether or not an installer was involved.
    twin = plain_event(tmp_path, name="Lantern Copy", instance=INSTANCE)
    assert [sorted(e["mod"]["name"] for e in g["mods"]) for g in cc.find_duplicates([a, twin])] == [["Lantern", "Lantern Copy"]]


# ---------- the rest of the window's checks ----------

def test_installer_changes_and_the_exports_leave_plain_installs_out(tmp_path):
    older, newer = fomod_event(number=1, completed="2026-10-01T12:00:00+00:00"), fomod_event(
        number=2, completed="2026-10-10T12:00:00+00:00")
    newer["steps"][0]["groups"][0]["options"].append(
        {"id": "step:0/group:0/option:1", "name": "Brand new", "selected": False, "enabled": True})
    assert len(installer_changes([older, newer], None)) == 1                       # the installer did change
    plain = plain_event(tmp_path, when="2026-10-21T12:00:00.000Z", archive="Other.zip")
    assert installer_changes([older, newer, plain], None, picks=True) == []              # now without an installer: nothing to compare
    between = plain_event(tmp_path, when="2026-10-05T12:00:00.000Z", archive="Other.zip")
    assert len(installer_changes([older, between, newer], None, picks=True)) == 1      # a plain install between is no part of it
    with EventStore(tmp_path / "db") as store:
        store.ingest(plain)
        store.ingest(fomod_event(name="Other Mod", number=9))
        assert [e["mod"]["name"] for e in current_events(store)] == ["Other Mod"]


def _order(*active: str, masters=None, **extra) -> dict:
    return {"instance_id": INSTANCE, "active_plugins": ["Skyrim.esm", *active], "base_plugins": ["Skyrim.esm"],
            "mods": ["Lantern"], "unchecked_plugins": [], "plugin_masters": masters or {}, "disabled_mod_plugins": {}, **extra}


def test_a_plain_mods_plugin_that_is_on_without_a_master_is_a_missing_master_card(tmp_path):
    event = plain_event(tmp_path, files={"Lantern.esp": plugin_bytes("Skyrim.esm", "Needed.esm")})
    result = find_missed_patches([event], _order("Lantern.esp"), [])
    [item] = result["missed"]
    assert (item["state"], item["kind"], item["label"], item["mod_name"]) == ("missing-masters", "plain", "Lantern.esp", "Lantern")
    assert item["option_id"] == "" and item["event_id"] == event["event_id"]
    assert item["disabled"] == [{"plugin": "Lantern.esp", "where": "switched on", "on": True, "missing": ["Needed.esm"]}]
    assert result["events_without_evidence"] == 0                      # a plain record is not "logged before patch checking"
    # The master is there, or the plugin is off, or it is another instance: nothing to say.
    assert find_missed_patches([event], _order("Lantern.esp", "Needed.esm"), [])["missed"] == []
    assert find_missed_patches([event], _order(), [])["missed"] == []
    assert find_missed_patches([event], {**_order("Lantern.esp"), "instance_id": OTHER_INSTANCE}, [])["missed"] == []
    # What MO2 reports for the plugin wins over what the header said.
    assert find_missed_patches([event], _order("Lantern.esp", masters={"Lantern.esp": ["Skyrim.esm"]}), [])["missed"] == []
    # A reminder that is done settles it.
    done = [{"event_id": event["event_id"], "option_id": "", "state": "dismissed"}]
    assert find_missed_patches([event], _order("Lantern.esp"), done)["missed"] == []


def test_a_plain_mods_masters_that_are_off_but_installed_do_not_count_as_missing(tmp_path):
    event = plain_event(tmp_path, files={"Lantern.esp": plugin_bytes("Needed.esm")})
    order = _order("Lantern.esp", unchecked_plugins=["Needed.esm"])
    assert find_missed_patches([event], order, [])["missed"] == []


# ---------- the plugin: noticing a plain install ----------

from test_plugin_contract import _FakeOrganizer, _FakeWindow, _load_plugin_module  # noqa: E402
import test_plus_plugin as plus_tests  # noqa: E402


class _PlainMod:
    """The part of IModInterface a plain record needs."""

    def __init__(self, name: str, folder: Path | None = None, archive: str = "", version: str = "1.0", nexus: int = 4242,
                 flags=()):
        self._name, self.folder, self.archive, self.version_text, self.nexus = name, folder, archive, version, nexus
        self.flags = set(flags)

    def name(self):
        return self._name

    def version(self):
        return type("V", (), {"canonicalString": lambda _s: self.version_text})()

    def nexusId(self):
        return self.nexus

    def installationFile(self):
        return self.archive

    def absolutePath(self):
        return str(self.folder)

    def isSeparator(self):
        return "separator" in self.flags

    def isForeign(self):
        return "foreign" in self.flags

    def isOverwrite(self):
        return "overwrite" in self.flags

    def isBackup(self):
        return "backup" in self.flags


class _Mods:
    def __init__(self):
        self.mods: dict[str, _PlainMod] = {}

    def add(self, mod):
        self.mods[mod.name()] = mod
        return mod

    def allMods(self):
        return list(self.mods)

    def getMod(self, name):
        return self.mods.get(name)

    def onModInstalled(self, callback):
        return True

    def onModRemoved(self, callback):
        return True


def _full(tmp_path, monkeypatch, name: str):
    """The full edition wired to fakes: a manual executor and a fixed instance. The fake timers run at once."""

    module, _timer, _app = _load_plugin_module(monkeypatch, name)
    monkeypatch.setattr(module._base, "stable_instance_id", lambda _path: INSTANCE)
    monkeypatch.setattr(module, "stable_instance_id", lambda _path: INSTANCE)
    organizer = _FakeOrganizer(tmp_path)
    organizer.mod_list = _Mods()
    organizer.managedGame = lambda: type("G", (), {"gameNexusName": lambda _s: "skyrimspecialedition",
                                                    "gameName": lambda _s: "Skyrim Special Edition"})()
    plugin = module.FomodLoggerPlugin()
    plugin._organizer = organizer
    plugin._data_root = tmp_path / "fomod_logger"
    plugin._data_root.mkdir()
    manual = background.ManualExecutor()
    plugin._new_executor = lambda: manual
    plugin._active = plugin._ready = True
    plugin._migrate_once = lambda: None
    plugin._edition_start()
    plugin.manual = manual
    plugin.window = _FakeWindow()
    plugin._window = plugin.window
    plugin._capture_load_order = lambda: None
    return module, plugin, organizer


def _install_full(plugin, organizer, mod):
    organizer.mod_list.add(mod)
    plugin._on_mod_installed(mod)


def _records(plugin, name: str | None = None) -> list[dict]:
    with EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        found = store.history("", include_hidden=True)
    return [e for e in found if name is None or e["mod"]["name"] == name]


def _write_event_file(plugin, event: dict) -> None:
    folder = plugin._data_root / "events"
    folder.mkdir(exist_ok=True)
    (folder / f"{event['event_id']}.json").write_text(json.dumps(event), encoding="utf-8")


def _now_text(seconds: float = 0) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def test_a_plain_install_is_recorded_after_it_settles_with_the_folder_read_off_the_ui_thread(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_install")
    folder = make_mod(tmp_path / "mods" / "Lantern", {"Lantern.esp": plugin_bytes("Skyrim.esm"), "meshes/lamp.nif": "x"})
    seen = {}
    real = pr.run_job

    def spy(job, cancelled=None):
        seen["thread"] = threading.get_ident()
        return real(job, cancelled)

    monkeypatch.setattr(pr, "run_job", spy)
    _install_full(plugin, organizer, _PlainMod("Lantern", folder, archive="C:/dl/Lantern-1.0.zip"))
    assert _records(plugin) == [] and len(plugin.manual.pending) == 1 and len(plugin._plain_handles) == 1
    plugin.manual.run_pending()
    [event] = _records(plugin)
    assert event["kind"] == "plain" and event["mod"] == {"name": "Lantern", "version": "1.0", "nexus_id": 4242}
    assert event["source"] == {"archive": "Lantern-1.0.zip", "reinstallation": False}
    assert event["plain"]["origin"] == "install" and event["plain"]["game"] == "skyrimspecialedition"
    assert event["plain"]["plugins"] == [{"name": "Lantern.esp", "masters": ["Skyrim.esm"]}]
    with EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        assert store.plain_files(event["event_id"]) == ["Lantern.esp", "meshes/lamp.nif"]
    assert plugin._plain_handles == [] and plugin.window.refreshes >= 1 and plugin.activeProblems() == []
    assert plugin._check_held("lantern") is False


def test_the_walk_runs_on_a_worker_thread(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_thread")
    seen = {}
    real = pr.run_job

    def spy(job, cancelled=None):
        seen["thread"] = threading.get_ident()
        return real(job, cancelled)

    monkeypatch.setattr(pr, "run_job", spy)

    class Threaded:
        def __init__(self):
            self.ends = []

        def run(self, work, on_message, on_finished):
            handle = background.Handle()

            def target():
                value, error = background._run(work, handle, on_message)
                self.ends.append((on_finished, value, error))

            thread = threading.Thread(target=target)
            thread.start()
            thread.join()
            return handle

        def close(self):
            return

    threaded = plugin._executor = Threaded()
    _install_full(plugin, organizer, _PlainMod("Lantern", make_mod(tmp_path / "m", {"a.txt": "x"}), archive="L.zip"))
    assert seen["thread"] != threading.get_ident() and _records(plugin) == []              # nothing stored from the worker
    for finished, value, error in threaded.ends:
        finished(value, error)                                                                # the "UI thread" collects it
    assert len(_records(plugin)) == 1


def test_an_install_the_native_installer_recorded_gets_no_plain_record(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_covered")
    _write_event_file(plugin, fomod_event(archive="Lantern-1.0.zip", completed=_now_text(-3)))
    _install_full(plugin, organizer, _PlainMod("Lantern", make_mod(tmp_path / "m", {"a.txt": "x"}), archive="C:/dl/lantern-1.0.zip"))
    assert plugin.manual.pending == []
    assert [e.get("kind") for e in _records(plugin)] == [None]                         # the event file was imported, nothing else


def test_an_installer_record_of_another_archive_or_an_old_one_does_not_cover_a_plain_install(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_not_covered")
    _write_event_file(plugin, fomod_event(archive="Other.zip", completed=_now_text(-3), number=1))
    _write_event_file(plugin, fomod_event(archive="Lantern-1.0.zip", completed=_now_text(-3600), number=2))
    _install_full(plugin, organizer, _PlainMod("Lantern", make_mod(tmp_path / "m", {"a.txt": "x"}), archive="Lantern-1.0.zip"))
    assert len(plugin.manual.pending) == 1
    plugin.manual.run_pending()
    assert sorted(e.get("kind", "fomod") for e in _records(plugin)) == ["fomod", "fomod", "plain"]
    [current] = _records(plugin, "Lantern")[:1]
    assert current["kind"] == "plain"                                       # the newest record of the mod


def test_updating_a_plain_mod_writes_a_new_record_marked_as_a_reinstall(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_update")
    folder = make_mod(tmp_path / "m", {"a.txt": "x"})
    mod = _PlainMod("Lantern", folder, archive="Lantern-1.0.zip")
    _install_full(plugin, organizer, mod)
    plugin.manual.run_pending()
    (folder / "b.txt").write_text("new", encoding="utf-8")
    mod.archive, mod.version_text = "Lantern-1.1.zip", "1.1"
    import time
    time.sleep(0.01)
    _install_full(plugin, organizer, mod)
    plugin.manual.run_pending()
    newest, first = _records(plugin)
    assert (first["source"]["reinstallation"], newest["source"]["reinstallation"]) == (False, True)
    assert newest["mod"]["version"] == "1.1" and newest["plain"]["file_count"] == 2 and first["plain"]["file_count"] == 1
    with EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        assert [e["plain"]["file_count"] for e in store.latest("")] == [2]


def test_an_installer_record_that_arrives_while_the_folder_is_read_wins(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_race")
    _install_full(plugin, organizer, _PlainMod("Lantern", make_mod(tmp_path / "m", {"a.txt": "x"}), archive="Lantern-1.0.zip"))
    _write_event_file(plugin, fomod_event(archive="Lantern-1.0.zip", completed=_now_text(-1)))
    plugin._ingest()                                                            # the installer's event lands first
    plugin.manual.run_pending()
    assert [e.get("kind") for e in _records(plugin)] == [None]                  # the plain record was not stored
    # ...and one that lands after the plain record replaces it.
    second = tmp_path / "second"
    second.mkdir()
    _module, plugin, organizer = _full(second, monkeypatch, "plain_full_race_two")
    _install_full(plugin, organizer, _PlainMod("Lantern", make_mod(tmp_path / "m2", {"a.txt": "x"}), archive="Lantern-1.0.zip"))
    plugin.manual.run_pending()
    assert [e.get("kind") for e in _records(plugin)] == ["plain"]
    _write_event_file(plugin, fomod_event(archive="Lantern-1.0.zip", completed=_now_text(-1)))
    plugin._ingest()
    assert [e.get("kind") for e in _records(plugin)] == [None]


def test_a_fomod_reinstall_of_a_plain_mod_is_the_mods_current_record(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_fomod_after")
    mod = _PlainMod("Lantern", make_mod(tmp_path / "m", {"a.txt": "x"}), archive="Lantern-1.0.zip")
    _install_full(plugin, organizer, mod)
    plugin.manual.run_pending()
    mod.archive = "Lantern-2.0.zip"
    _write_event_file(plugin, fomod_event(archive="Lantern-2.0.zip", completed=_now_text(5)))
    _install_full(plugin, organizer, mod)
    assert plugin.manual.pending == []
    with EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        assert [e.get("kind") for e in store.latest("")] == [None]
        assert len(store.history("", include_hidden=True)) == 2


def test_removing_a_plain_mod_marks_its_record_removed_and_a_record_finished_after_that_is_dropped(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_remove")
    mod = _PlainMod("Lantern", make_mod(tmp_path / "m", {"a.txt": "x"}), archive="L.zip")
    _install_full(plugin, organizer, mod)
    plugin.manual.run_pending()
    plugin._on_mod_removed("Lantern")
    assert _records(plugin)[0]["removed_at"] is not None
    # A walk that was still running when the mod was removed stores nothing.
    other = _PlainMod("Other", make_mod(tmp_path / "o", {"a.txt": "x"}), archive="O.zip")
    _install_full(plugin, organizer, other)
    del organizer.mod_list.mods["Other"]
    plugin.manual.run_pending()
    assert _records(plugin, "Other") == []


def test_a_folder_that_cannot_be_read_is_a_problem_until_the_mod_is_recorded_or_removed(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_failure")
    mod = _PlainMod("Lantern", tmp_path / "missing", archive="L.zip")
    _install_full(plugin, organizer, mod)
    plugin.manual.run_pending()
    assert _records(plugin) == [] and plugin.activeProblems() == [13]
    assert "Lantern" in plugin.fullDescription(13) and plugin.shortDescription(13)
    plugin._on_mod_removed("Lantern")
    assert plugin.activeProblems() == []


def test_separators_the_overwrite_folder_backups_and_the_games_own_entries_get_no_record(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_special")
    for flag in ("separator", "overwrite", "foreign", "backup"):
        _install_full(plugin, organizer, _PlainMod(f"x {flag}", make_mod(tmp_path / flag, {"a.txt": "x"}), flags=(flag,)))
    assert plugin.manual.pending == [] and _records(plugin) == []


def test_a_job_that_raises_is_a_problem_and_the_plugin_goes_on(tmp_path, monkeypatch, capsys):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_raise")
    monkeypatch.setattr(pr, "run_job", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("the worker fell over")))
    _install_full(plugin, organizer, _PlainMod("Lantern", make_mod(tmp_path / "m", {"a.txt": "x"}), archive="L.zip"))
    plugin.manual.run_pending()
    assert plugin.activeProblems() == [13] and "the worker fell over" in plugin.fullDescription(13)


def test_a_result_that_arrives_after_the_plugin_was_switched_off_is_dropped(tmp_path, monkeypatch):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, "plain_full_off")
    _install_full(plugin, organizer, _PlainMod("Lantern", make_mod(tmp_path / "m", {"a.txt": "x"}), archive="L.zip"))
    old = plugin.manual
    handles = list(plugin._plain_handles)
    plugin._shutdown()
    assert plugin._executor is None and old in plugin._retired and plugin._plain_handles == []
    assert [handle.cancelled.is_set() for handle in handles] == [True]       # the walk that was running is told to stop
    old.run_pending()
    assert _records(plugin) == []


# ---------- scanning the mods that have no record ----------

def _scan_world_full(tmp_path, monkeypatch, name: str, count: int = 3):
    _module, plugin, organizer = _full(tmp_path, monkeypatch, name)
    for number in range(count):
        organizer.mod_list.add(_PlainMod(f"Mod {number}", make_mod(tmp_path / "mods" / f"Mod {number}",
                                                                  {"a.txt": "x", f"b{number}.esp": plugin_bytes()}),
                                         archive=f"Mod{number}.zip", version=f"1.{number}"))
    organizer.mod_list.add(_PlainMod("Divider_separator", make_mod(tmp_path / "mods" / "sep"), flags=("separator",)))
    organizer.mod_list.add(_PlainMod("DLC: Dragonborn", tmp_path / "nowhere", flags=("foreign",)))
    return plugin, organizer


def test_the_scan_lists_the_mods_with_no_record_and_not_the_special_ones(tmp_path, monkeypatch):
    plugin, organizer = _scan_world_full(tmp_path, monkeypatch, "plain_full_scan_list")
    jobs = plugin._scan_jobs()
    assert [j.mod_name for j in jobs] == ["Mod 0", "Mod 1", "Mod 2"] and plugin._scanner.count() == 3
    assert all(isinstance(j, pr.PlainJob) and j.kind == "scan" and j.when == "" for j in jobs)    # dated by the folder, off the UI thread
    assert jobs[1].mod_version == "1.1" and jobs[1].archive == "Mod1.zip" and jobs[1].game == "skyrimspecialedition"
    plugin._active = False
    assert plugin._scan_jobs() == []


def test_the_scan_records_every_mod_in_the_background_with_progress_and_a_summary(tmp_path, monkeypatch):
    plugin, organizer = _scan_world_full(tmp_path, monkeypatch, "plain_full_scan_run")
    progress, done = [], []
    plugin._scanner.start(lambda *args: progress.append(args), done.append)
    assert progress == [] and _records(plugin) == [] and plugin._scanner.running is True
    plugin.manual.run_pending()
    [summary] = done
    assert (summary.total, summary.plain, summary.failed, summary.cancelled) == (3, 3, 0, False)
    assert "3 of 3 mods: 3 recorded as installed without an installer" in summary.message()
    assert [p[:2] for p in progress] == [(0, 3), (1, 3), (1, 3), (2, 3), (2, 3), (3, 3)]
    assert {e["mod"]["name"] for e in _records(plugin)} == {"Mod 0", "Mod 1", "Mod 2"}
    assert all(e["plain"]["origin"] == "scan" and e["plain"]["file_count"] == 2 for e in _records(plugin))
    assert plugin._scanner.count() == 0 and plugin.window.refreshes >= 1
    again = []
    assert plugin._scanner.start(lambda *a: None, again.append).total == 0 and again[0].message().startswith("Every mod")


def test_the_scan_can_be_cancelled_between_mods_and_continued_later(tmp_path, monkeypatch):
    plugin, organizer = _scan_world_full(tmp_path, monkeypatch, "plain_full_scan_cancel")
    done = []

    def on_progress(finished, total, name):
        if finished == 1:
            plugin._scanner.cancel()

    plugin._scanner.start(on_progress, done.append)
    plugin.manual.run_pending()
    [summary] = done
    assert summary.cancelled is True and summary.done == 1 and "Stopped after 1 of 3 mods" in summary.message()
    assert len(_records(plugin)) == 1 and plugin._scanner.count() == 2


def test_the_scan_stops_inside_a_big_folder_when_cancelled(tmp_path):
    folder = make_mod(tmp_path / "big", {f"d{n}/f.txt": "x" for n in range(700)})
    jobs = [job(folder, "Big", kind="scan", when="")]
    from fomod_logger_support.scan import Scanner
    stored = []
    scanner = Scanner(lambda: jobs, lambda r: stored.append(r) or True, lambda: background.InlineExecutor())
    done = []
    original = pr.walk_files

    def walking(path, cancelled=None, **kw):
        cancelled.set()                  # the user presses Cancel while the folder is being read
        return original(path, cancelled, **kw)

    try:
        pr.walk_files = walking
        scanner.start(lambda *a: None, done.append)
    finally:
        pr.walk_files = original
    assert done[0].cancelled is True and stored == []


def test_a_scan_never_overwrites_a_record_made_in_the_meantime(tmp_path, monkeypatch):
    plugin, organizer = _scan_world_full(tmp_path, monkeypatch, "plain_full_scan_race", count=2)
    done = []
    plugin._scanner.start(lambda *a: None, done.append)
    _install_full(plugin, organizer, organizer.mod_list.mods["Mod 0"])             # reinstalled while the scan is queued
    plugin.manual.pending.reverse()                                               # and its record is made first
    plugin.manual.run_pending()
    assert len(_records(plugin, "Mod 0")) == 1 and done[0].skipped == 1 and done[0].plain == 1


def test_a_mod_removed_during_the_scan_is_not_resurrected_and_a_removed_mod_is_scanned_again_when_back(tmp_path, monkeypatch):
    plugin, organizer = _scan_world_full(tmp_path, monkeypatch, "plain_full_scan_removed", count=1)
    plugin._scanner.start(lambda *a: None, lambda s: None)
    plugin.manual.run_pending()
    plugin._on_mod_removed("Mod 0")
    assert plugin._scanner.count() == 1                       # a removed record does not count: the mod is back in the list


def test_the_unscanned_hint_count_and_the_window_options_come_from_the_scanner(tmp_path, monkeypatch):
    plugin, _organizer = _scan_world_full(tmp_path, monkeypatch, "plain_full_options")
    assert plugin._window_options() == {"scanner": plugin._scanner}


# ---------- the FOMOD Plus edition ----------

def _plus_plain(tmp_path, monkeypatch, name: str):
    module, plugin, organizer = plus_tests._plus(tmp_path, monkeypatch, name, plain=True)
    organizer.mod_list = plus_tests._Mods()
    organizer.managedGame = lambda: type("G", (), {"gameNexusName": lambda _s: "skyrimspecialedition"})()
    return module, plugin, organizer


class _PlusMod(plus_tests._Mod):
    def __init__(self, *args, flags=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.flags = set(flags)

    def isSeparator(self):
        return "separator" in self.flags

    def isForeign(self):
        return "foreign" in self.flags


def test_plus_a_plain_install_has_no_fomod_plus_record_and_gets_a_plain_record(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus_plain(tmp_path, monkeypatch, "plain_plus_install")
    folder = make_mod(tmp_path / "mods" / "Lantern", {"Lantern.esp": plugin_bytes("Skyrim.esm"), "meshes/lamp.nif": "x"})
    mod = _PlusMod("Lantern", None, archive="Lantern-1.0.zip", version="1.0", nexus=4242, folder=folder)
    plus_tests._install(plugin, organizer, mod)
    assert plugin.manual.pending and plugin._pending == {}
    plugin.manual.run_pending()
    [event] = plus_tests._events(plugin)
    assert event["kind"] == "plain" and event["plain"]["file_count"] == 2 and event["source"]["archive"] == "Lantern-1.0.zip"
    assert plugin.activeProblems() == []


def test_plus_an_install_fomod_plus_handled_gets_only_the_rebuilt_record(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus_plain(tmp_path, monkeypatch, "plain_plus_covered")
    archive = plus_tests._grass_zip(tmp_path)
    folder = make_mod(tmp_path / "mods" / "LiveTest Grass", {"x.txt": "x"})
    mod = _PlusMod("LiveTest Grass", plus_tests.SKIP_VILLAGE, archive=str(archive), folder=folder)
    plus_tests._install(plugin, organizer, mod)
    assert len(plugin.manual.pending) == 1 and plugin._pending == {"livetest grass": 1}      # the rebuild, no walk
    plugin.manual.run_pending()
    assert [e.get("kind") for e in plus_tests._events(plugin)] == [None]
    # The same archive again with no new choices (FOMOD Plus restored its own): the stored record is the install.
    plus_tests._install(plugin, organizer, mod)
    assert plugin.manual.pending == []


def test_plus_a_mod_reinstalled_without_fomod_plus_choices_is_a_plain_install(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus_plain(tmp_path, monkeypatch, "plain_plus_update")
    archive = plus_tests._grass_zip(tmp_path)
    folder = make_mod(tmp_path / "mods" / "LiveTest Grass", {"x.txt": "x"})
    mod = _PlusMod("LiveTest Grass", plus_tests.SKIP_VILLAGE, archive=str(archive), folder=folder)
    plus_tests._install(plugin, organizer, mod)
    plugin.manual.run_pending()
    mod.archive, mod.record = "A-Plain-Update.zip", None            # a plain zip over it: FOMOD Plus saved nothing new
    plus_tests._install(plugin, organizer, mod)
    plugin.manual.run_pending()
    assert [e.get("kind") for e in plus_tests._events(plugin)] == ["plain", None]
    assert plus_tests._events(plugin)[0]["source"]["reinstallation"] is True


def test_plus_an_unusable_record_is_a_problem_and_the_mod_still_gets_a_plain_record(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus_plain(tmp_path, monkeypatch, "plain_plus_bad")
    folder = make_mod(tmp_path / "mods" / "Bad", {"x.txt": "x"})
    plus_tests._install(plugin, organizer, _PlusMod("Bad", '{"steps": 3}', archive="B.zip", folder=folder))
    plugin.manual.run_pending()
    assert plugin.activeProblems() == [12] and [e["kind"] for e in plus_tests._events(plugin)] == ["plain"]


def test_plus_a_scan_rebuilds_what_fomod_plus_recorded_and_records_the_rest_and_a_rebuild_replaces_a_plain_record(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus_plain(tmp_path, monkeypatch, "plain_plus_scan")
    archive = plus_tests._grass_zip(tmp_path)
    for name, record in (("LiveTest Grass", plus_tests.SKIP_VILLAGE), ("Plain One", None)):
        folder = make_mod(tmp_path / "mods" / name, {"x.txt": "x"})
        organizer.mod_list.add(_PlusMod(name, record, archive=str(archive) if record else "P.zip", folder=folder))
    organizer.mod_list.add(_PlusMod("Divider_separator", None, folder=tmp_path / "mods", flags=("separator",)))
    assert [(j.mod_name, type(j).__name__) for j in plugin._scan_jobs()] == [
        ("LiveTest Grass", "RebuildJob"), ("Plain One", "PlainJob")]
    done = []
    plugin._scanner.start(lambda *a: None, done.append)
    plugin.manual.run_pending()
    assert (done[0].rebuilt, done[0].plain, done[0].failed) == (1, 1, 0)
    assert sorted((e["mod"]["name"], e.get("kind", "fomod")) for e in plus_tests._events(plugin)) == [
        ("LiveTest Grass", "fomod"), ("Plain One", "plain")]
    # A mod that has only a plain record is rebuilt when FOMOD Plus's record turns up: the rebuild takes its place.
    with EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        store.ingest(plain_event(tmp_path, name="Late Grass", archive=archive.name, when=pr.utc_text()))
    folder = make_mod(tmp_path / "mods" / "Late Grass", {"x.txt": "x"})
    organizer.mod_list.add(_PlusMod("Late Grass", plus_tests.WITH_VILLAGE, archive=str(archive), folder=folder))
    assert [j.mod_name for j in plugin._scan_jobs()] == ["Late Grass"]
    plugin._scanner.start(lambda *a: None, lambda s: None)
    plugin.manual.run_pending()
    assert [e.get("kind", "fomod") for e in plus_tests._events(plugin, "Late Grass")] == ["fomod"]
