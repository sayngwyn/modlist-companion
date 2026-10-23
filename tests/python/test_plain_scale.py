"""Mods without an installer at scale and at the edges: lookups that cannot starve, the size of the file lists, an
unreadable folder, the count behind the hint, and what the FOMOD Plus edition does when a rebuild cannot be made or
another install comes while one is under way."""

from __future__ import annotations

import os

from fomod_logger_support.core import plain_record as pr
from fomod_logger_support.core.storage import EventStore

from test_plain_records import (INSTANCE, _PlainMod, _PlusMod, _install_full, _plus_plain, _scan_world_full, fomod_event,
                                job, make_mod, plain_event, plugin_bytes)
import test_plus_plugin as plus_tests


def _many(store: EventStore, count: int, files_of, *, name_of=lambda n: f"Mod {n:02}", when_of=None) -> list[dict]:
    """Ingest ``count`` plain records straight from file lists (no folders on disk); returns the events."""

    events = []
    for number in range(count):
        made = pr.PlainJob(INSTANCE, "MO2 Test", name_of(number), "1.0", 1, "skyrimspecialedition", f"M{number}.zip", "x",
                           when_of(number) if when_of else f"2026-10-21T12:{number % 60:02}:00.000Z", "install")
        event = pr.build_event(made, files_of(number), False, [])
        store.ingest(event)
        events.append(event)
    return events


# ---------- lookups that cannot starve ----------

def test_a_folder_lookup_finds_every_mod_not_only_the_first_ones_alphabetically(tmp_path):
    with EventStore(tmp_path / "db") as store:
        _many(store, 30, lambda n: [f"textures/mod{n:02}/f{i:04}.dds" for i in range(1_000)] + [f"textures/shared/s{n:02}.dds"])
        assert len(store.file_origins("textures")["matches"]) == 30           # a global cap of 20,000 rows gave 20
        assert len(store.latest("textures/")) == 30 and len(store.history("textures/")) == 30
        assert len(store.file_origins("textures/shared")["matches"]) == 30
        assert len(store.file_origins("textures", current=True, instance_id=INSTANCE)["matches"]) == 30


def test_a_folder_lookup_over_mods_with_twenty_thousand_files_each_still_finds_all_of_them(tmp_path):
    with EventStore(tmp_path / "db") as store:
        _many(store, 40, lambda n: [f"textures/landscape/m{n:02}/d{d:03}/f{i:03}.dds" for d in range(200) for i in range(100)])
        answer = store.file_origins("textures/landscape")
        assert len(answer["matches"]) == 40
        assert sorted(m["mod_name"] for m in answer["matches"]) == [f"Mod {n:02}" for n in range(40)]
        assert len(store.latest("textures/landscape/")) == 40


def test_a_file_name_lookup_finds_it_in_every_mod_and_says_when_it_lists_only_some_of_a_mods_files(tmp_path):
    with EventStore(tmp_path / "db") as store:
        _many(store, 40, lambda n: ["meta.txt", f"docs/{n}.txt"])
        answer = store.file_origins("meta.txt")
        assert len(answer["matches"]) == 40 and len(store.latest("meta.txt")) == 40
        assert not [note for note in answer["notes"] if "more than" in note]
    with EventStore(tmp_path / "db2") as store:
        _many(store, 3, lambda n: [f"a{k}/readme.txt" for k in range(6)] + (["b/README.TXT"] if n == 0 else []))
        answer = store.file_origins("readme.txt")
        assert len(answer["matches"]) == 9                                             # three files of each mod
        assert any("have more than 3 files with that name" in note for note in answer["notes"])
        assert len(store.latest("readme.txt")) == 3


def test_the_lookup_compares_paths_without_regard_to_case_and_a_name_is_the_whole_name(tmp_path):
    with EventStore(tmp_path / "db") as store:
        _many(store, 2, lambda n: ["Meshes/Armor/Iron.NIF", "textures/x/iron.dds"] if n == 0 else ["scripts/iron.pex"])
        assert [m["mod_name"] for m in store.file_origins("meshes\\armor\\iron.nif")["matches"]] == ["Mod 00"]
        assert [m["mod_name"] for m in store.file_origins("IRON.nif")["matches"]] == ["Mod 00"]
        assert [m["mod_name"] for m in store.file_origins("MESHES")["matches"]] == ["Mod 00"]
        assert store.file_origins("ron.nif")["matches"] == []


# ---------- the size of the file lists ----------

def test_only_a_mods_newest_two_plain_records_keep_their_file_lists(tmp_path):
    with EventStore(tmp_path / "db") as store:
        events = _many(store, 4, lambda n: [f"v{n}/file.txt", "common.txt"], name_of=lambda n: "Lantern",
                       when_of=lambda n: f"2026-10-2{n + 1}T12:00:00.000Z")
        other = _many(store, 1, lambda n: ["other.txt"], name_of=lambda n: "Other")[0]
        kept = [store.files_kept(store.event(e["event_id"])) for e in events]
        assert kept == [False, False, True, True]                                        # the oldest two lost theirs
        assert store.files_kept(store.event(other["event_id"])) is True
        assert store.plain_files(events[0]["event_id"]) == []
        assert store.plain_files(events[3]["event_id"]) == ["common.txt", "v3/file.txt"]
        old = store.with_files(store.event(events[0]["event_id"]))
        assert "files" not in old["plain"] and old["plain"]["file_count"] == 2          # the record itself stays
        assert len(store.history("", include_hidden=True)) == 5
        assert store.file_origins("v0/file.txt")["matches"] == []                       # nothing asks for the lost list
        assert len(store.file_origins("v3/file.txt")["matches"]) == 1 and len(store.file_origins("common.txt")["matches"]) == 2
        exported = {e["event_id"]: e for e in store.full_history_export()["events"]}
        assert "files" not in exported[events[1]["event_id"]]["plain"] and exported[events[2]["event_id"]]["plain"]["files"]


def test_a_plain_record_costs_a_few_dozen_bytes_a_path_not_hundreds_and_takes_milliseconds(tmp_path):
    import time

    files = [f"textures/landscape/mod/folder{f:03}/file{i:03}.dds" for f in range(200) for i in range(100)]
    with EventStore(tmp_path / "db") as store:
        made = pr.PlainJob(INSTANCE, "MO2 Test", "Big", "1.0", 1, "g", "B.zip", "x", "2026-10-21T12:00:00.000Z", "install")
        event = pr.build_event(made, files, False, [])
        began = time.perf_counter()
        assert store.ingest(event) is True
        seconds = time.perf_counter() - began
        _many(store, 2, lambda n: files, name_of=lambda n: f"Other {n}")
        store._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert os.path.getsize(tmp_path / "db") / (3 * len(files)) < 90                      # version 8 took about 285 a file
    assert seconds < 1.0                                                                  # (about 0.03 s here; 0.1 to 0.2 before)


def test_a_folder_that_cannot_be_read_inside_a_mod_makes_the_list_incomplete(tmp_path, monkeypatch):
    folder = make_mod(tmp_path / "m", {"a.txt": "a", "locked/b.txt": "b", "fine/c.txt": "c"})
    real = pr.os.scandir

    def scandir(path):
        if str(path).endswith("locked"):
            raise PermissionError("no access")
        return real(path)

    monkeypatch.setattr(pr.os, "scandir", scandir)
    files, incomplete = pr.walk_files(folder)
    assert files == ["a.txt", "fine/c.txt"] and incomplete is True
    event = pr.run_job(job(folder)).event
    assert event["plain"]["files_truncated"] is True and event["plain"]["file_count"] == 2


# ---------- edges ----------

def test_a_mod_without_an_installer_is_not_one_of_your_own_previous_choices(tmp_path):
    from fomod_logger_support.core.import_choices import _own_keys

    with EventStore(tmp_path / "db") as store:
        store.ingest(plain_event(tmp_path, name="Plain Mod", archive="Plain-1.zip"))
        store.ingest(fomod_event(name="Real Mod", archive="Real-1.zip"))
        names, archives = _own_keys(store, INSTANCE)
    assert names == {"real mod"} and archives == {"real-1.zip"}


def test_the_scanner_can_count_without_building_the_jobs():
    from fomod_logger_support.scan import Scanner

    built = []
    scanner = Scanner(lambda: built.append(1) or [], lambda r: True, lambda: None, count=lambda: 7)
    assert scanner.count() == 7 and built == []
    assert Scanner(lambda: [1, 2], lambda r: True, lambda: None).count() == 2


def test_the_count_of_unrecorded_mods_looks_only_at_the_mods_without_a_record(tmp_path, monkeypatch):
    plugin, organizer = _scan_world_full(tmp_path, monkeypatch, "plain_full_count")
    plugin._scanner.start(lambda *a: None, lambda s: None)
    plugin.manual.run_pending()
    organizer.mod_list.add(_PlainMod("Unrecorded", make_mod(tmp_path / "u", {"a.txt": "x"}), archive="U.zip"))
    asked = []
    real = organizer.mod_list.getMod
    organizer.mod_list.getMod = lambda name: asked.append(name) or real(name)
    assert plugin._unrecorded_count() == 1 == len(plugin._scan_jobs()) and plugin._scanner.count() == 1
    asked.clear()
    plugin._unrecorded_count()
    assert "Unrecorded" in asked and not [name for name in asked if name.startswith("Mod ")]    # recorded ones are not asked about
    plugin._active = False
    assert plugin._unrecorded_count() == 0


def test_plus_a_choices_record_that_cannot_be_used_does_not_leave_the_mod_without_a_record(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus_plain(tmp_path, monkeypatch, "plain_plus_broken_scan")
    folder = make_mod(tmp_path / "mods" / "Broken", {"a.txt": "x", "Broken.esp": plugin_bytes()})
    organizer.mod_list.add(_PlusMod("Broken", "not json", archive="B.zip", folder=folder))
    [first] = plugin._scan_jobs()
    assert type(first).__name__ == "RebuildJob" and first.fallback is not None
    assert plugin._scanner.count() == 1
    done = []
    plugin._scanner.start(lambda *a: None, done.append)
    plugin.manual.run_pending()
    assert (done[0].failed, done[0].plain, done[0].rebuilt) == (0, 1, 0)
    [event] = plus_tests._events(plugin, "Broken")
    assert event["kind"] == "plain" and event["plain"]["file_count"] == 2 and event["plain"]["origin"] == "scan"
    assert plugin._scanner.count() == 0 and plugin._scan_jobs() == []                    # the hint would not count it again


def test_plus_a_rebuild_under_way_covers_only_an_install_of_its_own_archive(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus_plain(tmp_path, monkeypatch, "plain_plus_pending_archive")
    archive = plus_tests._grass_zip(tmp_path)
    folder = make_mod(tmp_path / "mods" / "LiveTest Grass", {"x.txt": "x"})
    mod = _PlusMod("LiveTest Grass", plus_tests.SKIP_VILLAGE, archive=str(archive), folder=folder)
    plugin._watch_install = lambda _mod: None
    plus_tests._install(plugin, organizer, mod)
    assert plugin._pending == {"livetest grass": 1}
    assert plugin._pending_archives == {"livetest grass": [archive.name.casefold()]}
    ours = {"name": "LiveTest Grass", "archive": str(archive)}
    other = {"name": "LiveTest Grass", "archive": "A-Different-Zip.zip"}
    assert plugin._covered_by_installer(ours) is True
    mod.record = None                                                  # the other install saved no FOMOD Plus choices
    assert plugin._covered_by_installer(other) is False                # the rebuild under way is not for that archive
    plugin.manual.run_pending()
    assert plugin._pending == {} and plugin._pending_archives == {}
