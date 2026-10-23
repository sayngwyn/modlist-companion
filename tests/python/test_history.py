"""The list's change history: what a diff of two snapshots reports, how a burst becomes one entry, how entries and the
install records agree, the database (migration from every earlier version, the cap, paging and filters), and the text the
window and the export show. No Qt and no MO2: see test_history_plugin.py for the plugin and test_history_window.py for the
window."""

from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fomod_logger_support.core import history, history_store, storage
from fomod_logger_support.core.storage import CURRENT_DATABASE_VERSION, EventStore

INSTANCE = "a" * 64
OTHER = "b" * 64


# ---------- snapshots ----------

def snap(mods=(), enabled=None, plugins=None, active=(), versions=None):
    return history.make_snapshot(mods, mods if enabled is None else enabled, plugins, active, versions)


def kinds(entries):
    return {entry["kind"]: [item["name"] for item in entry["items"]] for entry in entries}


def test_a_diff_reports_each_kind_of_change_once_with_its_items():
    old = snap(["A", "B", "C", "D", "E"], enabled=["A", "B", "C"], plugins=["a.esp", "b.esp", "c.esp", "d.esp"],
               active=["a.esp", "b.esp"])
    new = snap(["B", "A", "C", "E", "F"], enabled=["B", "C", "F"], plugins=["b.esp", "a.esp", "c.esp", "d.esp"],
               active=["a.esp", "c.esp"])
    found = kinds(history.diff(old, new))
    assert found[history.MOD_INSTALLED] == ["F"]
    assert found[history.MOD_REMOVED] == ["D"]
    # F is new and D is gone: neither counts as switched on or off. A is switched off; nothing was switched on.
    assert history.MOD_ENABLED not in found
    assert found[history.MOD_DISABLED] == ["A"]
    assert found[history.MOD_MOVED] in (["A"], ["B"])
    assert found[history.PLUGIN_ENABLED] == ["c.esp"]
    assert found[history.PLUGIN_DISABLED] == ["b.esp"]
    assert found[history.PLUGIN_MOVED] in (["a.esp"], ["b.esp"])


def test_every_kind_has_a_count_and_before_and_after_where_it_makes_sense():
    old = snap(["A", "B", "C"], plugins=["a.esp", "b.esp", "c.esp"], active=["a.esp"], versions={"A": ("1.0", "A-1.zip")})
    new = snap(["C", "B", "A"], enabled=["A"], plugins=["c.esp", "b.esp", "a.esp"], active=["a.esp", "b.esp"],
               versions={"A": ("1.1", "A-2.zip")})
    entries = {entry["kind"]: entry for entry in history.diff(old, new)}
    assert entries[history.MOD_UPDATED]["items"] == [{"name": "A", "before": "1.0", "after": "1.1"}]
    assert entries[history.MOD_DISABLED]["count"] == 2
    assert entries[history.MOD_MOVED]["count"] == 2 and entries[history.PLUGIN_MOVED]["count"] == 2   # a reversal moves all but one
    assert entries[history.PLUGIN_ENABLED]["items"] == [{"name": "b.esp"}]
    assert all(entry["count"] == len(entry["items"]) for entry in entries.values())


def test_a_changed_archive_with_the_same_version_is_an_update_named_by_its_archives():
    old = snap(["A"], versions={"A": ("1.0", "A-1.zip")})
    new = snap(["A"], versions={"A": ("1.0", "A-1-fixed.zip")})
    assert history.diff(old, new)[0]["items"] == [{"name": "A", "before": "A-1.zip", "after": "A-1-fixed.zip"}]
    assert history.diff(old, snap(["A"])) == []        # no versions known on one side: not judged


def test_a_dragged_block_of_twenty_plugins_is_twenty_moves_not_the_hundreds_it_passed():
    plugins = [f"p{n:03d}.esp" for n in range(300)]
    block = plugins[10:30]
    rest = [name for name in plugins if name not in block]
    new = rest[:200] + block + rest[200:]
    moved = history.moved(plugins, new)
    assert sorted(item["name"] for item in moved) == sorted(block)
    assert all(item["after"] > item["before"] for item in moved)


def test_one_item_moved_is_one_move_and_a_new_item_in_the_middle_moves_nobody():
    names = [f"m{n}" for n in range(50)]
    moved = history.moved(names, [*names[:5], names[30], *names[5:30], *names[31:]])
    assert [item["name"] for item in moved] == ["m30"] and moved[0]["before"] == 30 and moved[0]["after"] == 5
    assert history.moved(names, [*names[:20], "brand new", *names[20:]]) == []
    assert history.moved(names, names[:10]) == []
    assert history.moved([], names) == [] and history.moved(["only"], ["only"]) == []


def test_names_compare_without_regard_to_case():
    assert history.diff(snap(["Alpha"]), snap(["ALPHA"])) == []
    assert history.moved(["a", "B", "c"], ["A", "b", "C"]) == []


def test_plugins_that_appear_or_go_with_a_mod_are_not_reported_and_an_unreadable_load_order_is_not_judged():
    old = snap(["A"], plugins=["a.esp"], active=["a.esp"])
    new = snap(["A", "B"], plugins=["a.esp", "b.esp"], active=["a.esp", "b.esp"])
    assert [entry["kind"] for entry in history.diff(old, new)] == [history.MOD_INSTALLED]
    assert [entry["kind"] for entry in history.diff(new, old)] == [history.MOD_REMOVED]
    unreadable = snap(["A"], plugins=None)
    assert history.diff(old, unreadable) == [] and history.diff(unreadable, new)[0]["kind"] == history.MOD_INSTALLED


def test_a_burst_joins_into_one_list_of_items_that_keeps_where_a_move_started_and_ended():
    first = [{"name": "a", "before": 1, "after": 5}, {"name": "b", "before": 2, "after": 6}]
    second = [{"name": "a", "before": 5, "after": 9}, {"name": "b", "before": 6, "after": 2}, {"name": "c", "before": 0, "after": 4}]
    merged = history.merge_items(history.PLUGIN_MOVED, first, second)
    assert merged == [{"name": "a", "before": 1, "after": 9}, {"name": "c", "before": 0, "after": 4}]   # b came back: not a move
    assert history.merge_items(history.MOD_ENABLED, [{"name": "a"}], [{"name": "A"}, {"name": "b"}]) == [{"name": "a"}, {"name": "b"}]


# ---------- words ----------

UTC = lambda moment: moment.astimezone(timezone.utc)   # noqa: E731 - tests read the same time zone everywhere


def entry_of(kind, names=(), **extra):
    items = [{"name": name} for name in names]
    return {"kind": kind, "count": extra.pop("count", len(items) or 1), "items": items, "subject": extra.pop("subject", ""),
            "at": extra.pop("at", "2026-10-20T20:14:00.000Z"), "ended_at": extra.pop("ended_at", ""),
            "exit_code": extra.pop("exit_code", None), "detail": extra.pop("detail", {}), "offline": extra.pop("offline", False)}


def test_a_program_run_reads_as_a_sentence_with_how_it_ended():
    ran = entry_of(history.PROGRAM_RUN, subject="Skyrim SE", ended_at="2026-10-20T21:02:00.000Z", exit_code=0)
    assert history.title(ran, UTC) == "Skyrim SE ran 8:14 PM–9:02 PM, exited normally"
    crashed = dict(ran, exit_code=3221225477)
    assert history.title(crashed, UTC).endswith("exited with code 0xC0000005")
    assert history.title(dict(ran, exit_code=3), UTC).endswith("exited with code 3")
    lost = dict(ran, ended_at="", exit_code=None)
    assert history.title(lost, UTC) == "Skyrim SE started 8:14 PM, end not seen (MO2 may have closed first)"
    across = dict(ran, ended_at="2026-10-21T01:30:00.000Z")
    assert history.title(across, UTC) == "Skyrim SE ran 8:14 PM–Oct 21, 1:30 AM, exited normally"


def test_titles_say_what_happened_to_one_or_many():
    assert history.title(entry_of(history.MOD_INSTALLED, ["Lux"])) == "Installed Lux"
    assert history.title(entry_of(history.MOD_UPDATED, ["Lux", "Via"])) == "2 mods updated"
    assert history.title(entry_of(history.PLUGIN_MOVED, [f"p{n}" for n in range(12)])) == "12 plugins moved"
    assert history.title(entry_of(history.PLUGIN_ENABLED, ["a.esp"])) == "Plugin switched on: a.esp"
    assert history.title(entry_of(history.MOD_DISABLED, ["Lux"])) == "Switched off Lux"
    assert history.title(entry_of(history.PROFILE_SWITCHED, ["Hard"], subject="Hard")) == "Switched to profile Hard"
    big = entry_of(history.MOD_INSTALLED, ["a", "b"], count=1500)
    assert history.title(big) == "1,500 mods installed"


def test_details_name_what_changed_and_that_it_was_found_at_start():
    moved = entry_of(history.MOD_MOVED, ["Lux"])
    moved["items"][0].update(before=5, after=50)
    assert history.detail(moved) == "priority 5 → 50"
    updated = entry_of(history.MOD_UPDATED, ["Lux"])
    updated["items"][0].update(before="1.0", after="1.1")
    assert history.detail(dict(updated, offline=True)) == "1.0 → 1.1 · found at MO2 start"
    assert history.detail(entry_of(history.MOD_ENABLED, ["a", "b"])) == ""
    assert history.item_detail(history.PLUGIN_MOVED, {"name": "a.esp", "before": 4, "after": 9}) == "position 4 → 9"


def test_days_are_named_for_today_and_yesterday_and_the_export_is_readable_text_and_markdown():
    today = date(2026, 10, 21)
    assert history.day_label(today, today) == "Today"
    assert history.day_label(date(2026, 10, 20), today) == "Yesterday"
    assert history.day_label(date(2026, 10, 3), today) == "Saturday, Oct 3, 2026"
    entries = [entry_of(history.PROGRAM_RUN, subject="Skyrim SE", at="2026-10-21T20:14:00.000Z", ended_at="2026-10-21T21:02:00.000Z", exit_code=0),
               entry_of(history.PLUGIN_MOVED, ["a.esp", "b.esp"], at="2026-10-21T19:00:00.000Z"),
               entry_of(history.MOD_INSTALLED, ["Lux"], at="2026-10-20T10:00:00.000Z")]
    entries[1]["items"][0].update(before=1, after=7)
    text = history.render_text(entries, "Changes to the list", today=today, zone=UTC)
    assert text.splitlines()[:3] == ["Changes to the list", "", "Today"]
    assert "8:14 PM  Skyrim SE ran 8:14 PM–9:02 PM, exited normally" in text
    assert "7:00 PM  2 plugins moved" in text and "- a.esp  (position 1 → 7)" in text
    assert "Yesterday" in text and "Installed Lux" in text
    markdown = history.render_text(entries, "Changes to the list", today=today, zone=UTC, markdown=True)
    assert markdown.startswith("# Changes to the list\n") and "## Today" in markdown
    assert "- 8:14 PM: **Skyrim SE ran 8:14 PM–9:02 PM, exited normally**" in markdown
    assert history.render_text([], today=today).strip() == "Nothing has been recorded for this selection yet."


# ---------- the database ----------

@pytest.fixture
def store(tmp_path):
    with EventStore(tmp_path / "db.sqlite3") as opened:
        yield opened


def at(minutes: float = 0, base: str = "2026-10-21T12:00:00.000Z") -> str:
    return history.shifted(base, minutes * 60)


def rows(store, **options):
    return store.history_page(INSTANCE, **{"limit": 1000, **options})


def test_a_new_database_has_the_history_tables_and_the_version_it_says():
    assert CURRENT_DATABASE_VERSION == 10


def test_entries_are_stored_with_their_items_and_read_back_newest_first(store):
    store.history_add(INSTANCE, "Default", [{"kind": history.PLUGIN_MOVED, "count": 2, "items": [
        {"name": "a.esp", "before": 1, "after": 5}, {"name": "b.esp", "before": 2, "after": 6}]}], at=at(0))
    store.history_add(INSTANCE, "Default", [{"kind": history.MOD_ENABLED, "count": 1, "items": [{"name": "Lux"}]}], at=at(10))
    found = rows(store)
    assert [entry["kind"] for entry in found] == [history.MOD_ENABLED, history.PLUGIN_MOVED]
    assert found[1]["count"] == 2 and found[1]["items"][0] == {"name": "a.esp", "before": 1, "after": 5}
    assert found[1]["profile"] == "Default" and found[1]["source"] == "snapshot" and found[1]["offline"] is False
    assert store.history_count(INSTANCE) == 2 and store.history_page(OTHER) == []


def test_entries_of_one_kind_a_moment_apart_are_one_burst_and_later_ones_are_not(store):
    def moved(name, before, after):
        return [{"kind": history.PLUGIN_MOVED, "count": 1, "items": [{"name": name, "before": before, "after": after}]}]

    store.history_add(INSTANCE, "Default", moved("a.esp", 1, 5), at=at(0))
    store.history_add(INSTANCE, "Default", moved("b.esp", 2, 6), at=history.shifted(at(0), 1.5))
    store.history_add(INSTANCE, "Default", moved("a.esp", 5, 9), at=history.shifted(at(0), 2.5))
    [one] = rows(store)
    assert one["count"] == 2 and [(i["name"], i["before"], i["after"]) for i in one["items"]] == [("a.esp", 1, 9), ("b.esp", 2, 6)]
    store.history_add(INSTANCE, "Default", moved("c.esp", 3, 7), at=at(5))           # minutes later: its own entry
    store.history_add(INSTANCE, "Other", moved("c.esp", 3, 7), at=at(5))             # another profile never joins
    assert len(rows(store)) == 3


def test_a_burst_that_undoes_itself_leaves_nothing(store):
    store.history_add(INSTANCE, "Default", [{"kind": history.PLUGIN_MOVED, "count": 1, "items": [{"name": "a", "before": 1, "after": 5}]}], at=at(0))
    store.history_add(INSTANCE, "Default", [{"kind": history.PLUGIN_MOVED, "count": 1, "items": [{"name": "a", "before": 5, "after": 1}]}],
                      at=history.shifted(at(0), 1))
    assert rows(store) == []


def test_changes_found_at_start_are_marked_and_never_join_a_live_burst(store):
    enabled = [{"kind": history.MOD_ENABLED, "count": 1, "items": [{"name": "Lux"}]}]
    store.history_add(INSTANCE, "Default", enabled, at=at(0))
    store.history_add(INSTANCE, "Default", [{"kind": history.MOD_ENABLED, "count": 1, "items": [{"name": "Via"}]}], at=at(0), offline=True)
    found = rows(store)
    assert [(e["offline"], e["items"][0]["name"]) for e in found] in ([(True, "Via"), (False, "Lux")], [(False, "Lux"), (True, "Via")])
    assert len(found) == 2


def test_a_snapshot_never_repeats_an_install_a_record_already_told_and_a_record_replaces_a_snapshots(store):
    installed = [{"kind": history.MOD_INSTALLED, "count": 2, "items": [{"name": "Lux"}, {"name": "Via"}]}]
    # The record comes first: the snapshot's "installed" for the same mod is dropped, the other mod is kept.
    store._connection.execute("BEGIN")
    history_store.add_record(store._connection, INSTANCE, {"kind": history.MOD_INSTALLED, "count": 1, "items": [{"name": "lux"}]}, at(0))
    store._connection.commit()
    store.history_add(INSTANCE, "Default", installed, at=at(1))
    found = rows(store)
    assert sorted((e["source"], tuple(i["name"] for i in e["items"])) for e in found) == [("record", ("lux",)), ("snapshot", ("Via",))]
    # The snapshot comes first: the record then takes its place.
    store.history_add(INSTANCE, "Default", [{"kind": history.MOD_REMOVED, "count": 1, "items": [{"name": "Old"}]}], at=at(20))
    with store._connection:
        history_store.add_record(store._connection, INSTANCE, {"kind": history.MOD_REMOVED, "count": 1, "items": [{"name": "Old"}]}, at(21))
    removed = [e for e in rows(store) if e["kind"] == history.MOD_REMOVED]
    assert [e["source"] for e in removed] == ["record"]


def test_offline_installs_dedupe_against_anything_told_since_the_snapshot_they_come_from(store):
    with store._connection:
        history_store.add_record(store._connection, INSTANCE, {"kind": history.MOD_INSTALLED, "count": 1, "items": [{"name": "Lux"}]}, at(-300))
    entries = [{"kind": history.MOD_INSTALLED, "count": 2, "items": [{"name": "Lux"}, {"name": "Via"}]}]
    store.history_add(INSTANCE, "Hardcore", entries, at=at(0), offline=True, dedupe_since=at(-400))
    assert [i["name"] for e in rows(store, kinds=[history.MOD_INSTALLED]) for i in e["items"] if e["source"] == "snapshot"] == ["Via"]


def test_the_oldest_entries_beyond_the_cap_go_and_each_instance_keeps_its_own(store):
    for n in range(40):
        store.history_add(INSTANCE, "", [{"kind": history.MOD_ENABLED, "count": 1, "items": [{"name": f"m{n}"}]}], at=at(n * 10), keep=25)
    store.history_add(OTHER, "", [{"kind": history.MOD_ENABLED, "count": 1, "items": [{"name": "x"}]}], at=at(0), keep=25)
    found = rows(store)
    assert len(found) == 25 and found[0]["items"][0]["name"] == "m39" and found[-1]["items"][0]["name"] == "m15"
    assert store.history_count(OTHER) == 1
    assert history.KEEP_ENTRIES == 50_000


def test_the_cap_holds_at_fifty_thousand_without_slowing_the_next_write(store):
    connection = store._connection
    with connection:
        connection.executemany(
            "INSERT INTO history (instance_id, profile, at, kind, count, subject, names, detail, source) "
            "VALUES (?, '', ?, 'mod_enabled', 1, 'm', 'm', '{\"items\":[{\"name\":\"m\"}]}', 'snapshot')",
            ((INSTANCE, history.shifted("2026-01-01T00:00:00.000Z", n)) for n in range(history.KEEP_ENTRIES + 400)))
    started = time.perf_counter()
    store.history_add(INSTANCE, "", [{"kind": history.MOD_DISABLED, "count": 1, "items": [{"name": "new"}]}], at=at(0))
    took = time.perf_counter() - started
    assert store.history_count(INSTANCE) == history.KEEP_ENTRIES
    assert rows(store, limit=1)[0]["items"][0]["name"] == "new" and took < 1.0


def test_pages_filter_by_kind_search_profile_and_since(store):
    entries = [
        ("Default", history.MOD_ENABLED, ["Lux Via"], 0), ("Default", history.PLUGIN_MOVED, ["lux.esp", "via.esp"], 10),
        ("Hardcore", history.MOD_ENABLED, ["Frost"], 20), ("", history.MOD_INSTALLED, ["Cathedral"], 30),
    ]
    for profile, kind, names, minute in entries:
        store.history_add(INSTANCE, profile, [{"kind": kind, "count": len(names), "items": [{"name": n} for n in names]}], at=at(minute))
    run = store.history_start_run(INSTANCE, "Default", "Skyrim SE", r"C:\Game\SkyrimSE.exe", at=at(40))
    store.history_finish_run(run, 0, at=at(100))
    store.history_add(INSTANCE, "Default", [{"kind": history.MOD_DISABLED, "count": 1, "items": [{"name": "Late"}]}], at=at(120))

    assert len(rows(store)) == 6
    # A profile shows its own entries and the instance's (installs, removals), never another profile's.
    assert [e["kind"] for e in rows(store, profile="Default")] == [
        history.MOD_DISABLED, history.PROGRAM_RUN, history.MOD_INSTALLED, history.PLUGIN_MOVED, history.MOD_ENABLED]
    assert [e["kind"] for e in rows(store, profile="Hardcore")] == [history.MOD_INSTALLED, history.MOD_ENABLED]
    assert [e["items"][0]["name"] for e in rows(store, profile="Hardcore", kinds=[history.MOD_ENABLED])] == ["Frost"]
    assert [e["kind"] for e in rows(store, search="LUX")] == [history.PLUGIN_MOVED, history.MOD_ENABLED]   # case, and inside a group
    assert [e["kind"] for e in rows(store, search="skyrimse.exe")] == [history.PROGRAM_RUN]                 # a program's path
    assert rows(store, kinds=[]) == []
    latest = store.history_latest_run(INSTANCE, profile="Default")
    assert latest["subject"] == "Skyrim SE" and latest["ended_at"] == at(100) and latest["exit_code"] == 0
    after = rows(store, since=(latest["at"], latest["id"]))
    assert [e["kind"] for e in after] == [history.MOD_DISABLED]
    assert store.history_count(INSTANCE, since=(latest["at"], latest["id"])) == 1
    assert store.history_latest_run(INSTANCE, profile="Hardcore") is None        # that run was in another profile
    assert store.history_profiles(INSTANCE) == ["Default", "Hardcore"]
    assert store.history_page(INSTANCE, limit=2) == rows(store, limit=2) and len(rows(store, limit=2)) == 2


def test_snapshots_are_kept_per_instance_and_profile_and_read_back_whole(store):
    snapshot = snap(["A", "B"], plugins=["a.esp"], active=["a.esp"], versions={"A": ("1", "A.zip")})
    store.history_save(INSTANCE, "Default", snapshot, [], at=at(0))
    store.history_save(INSTANCE, "Hardcore", snap(["Z"]), [], at=at(10))
    loaded, taken = store.history_snapshot(INSTANCE, "Default")
    assert loaded == snapshot and taken == at(0)
    assert store.history_snapshot(INSTANCE, "Nope") is None and store.history_snapshot(OTHER, "Default") is None
    assert store.history_last_profile(INSTANCE) == "Hardcore"
    store.history_save(INSTANCE, "Default", snapshot, [], at=at(20))        # the profile in use is the one saved last
    assert store.history_last_profile(INSTANCE) == "Default"
    with store._connection:
        store._connection.execute("UPDATE history_snapshots SET data = ? WHERE profile = 'Default'", (b"not zlib",))
    assert store.history_snapshot(INSTANCE, "Default") is None


def test_a_snapshot_and_its_entries_are_one_transaction(store, monkeypatch):
    first = snap(["A"])
    store.history_save(INSTANCE, "Default", first, [], at=at(0))

    def broken(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(history_store, "save_snapshot", broken)
    with pytest.raises(RuntimeError):
        store.history_save(INSTANCE, "Default", snap(["A", "B"]), [{"kind": history.MOD_INSTALLED, "count": 1, "items": [{"name": "B"}]}], at=at(5))
    assert rows(store) == [] and store.history_snapshot(INSTANCE, "Default")[0] == first


def test_a_program_that_ends_is_matched_by_its_entry_and_an_unknown_one_changes_nothing(store):
    entry = store.history_start_run(INSTANCE, "Default", "LOOT", r"C:\LOOT\LOOT.exe", at=at(0))
    assert store.history_finish_run(entry + 99, 0) is False
    assert store.history_finish_run(entry, 7, at=at(3)) is True
    [found] = rows(store)
    assert (found["ended_at"], found["exit_code"], found["detail"]["path"]) == (at(3), 7, r"C:\LOOT\LOOT.exe")


# ---------- install records ----------

_numbers = iter(range(1, 10**6))


def event(name="Lux", *, version="1.0", archive="Lux-1.0.zip", when="2026-10-21T12:00:00+00:00", instance=INSTANCE, kind=None,
          origin="install", scanned=False):
    number = next(_numbers)
    body = {
        "schema_version": 1, "event_id": str(uuid.UUID(int=number, version=4)), "started_at": when, "completed_at": when,
        "instance": {"id": instance, "label": "MO2 Test"}, "source": {"archive": archive, "reinstallation": False},
        "mod": {"name": name, "version": version, "nexus_id": 7},
        "installer": {"name": "FOMOD Logger Native Installer", "version": "1.0.0",
                      "upstream_commit": "fc263f2d923c704b4853c11ed4f8b8cf3920f30d"},
        "steps": [],
    }
    if scanned:
        body["source"]["scanned"] = True
    if kind == "plain":
        body.update(kind="plain", plain={"origin": origin, "file_count": 0, "files_truncated": False, "files": []})
        body["installer"]["name"] = "No installer (plain install)"
    return body


def records(store, kind_names=None):
    return [(e["kind"], e["items"][0]["name"], e["items"][0].get("before"), e["items"][0].get("after"))
            for e in store.history_page(INSTANCE, limit=100, kinds=kind_names)][::-1]


def test_installs_updates_and_reinstalls_come_from_the_records_and_agree_with_them(store):
    store.ingest(event(when="2026-10-21T12:00:00+00:00"))
    store.ingest(event(when="2026-10-21T13:00:00+00:00"))                                              # the same again
    store.ingest(event(version="1.1", archive="Lux-1.1.zip", when="2026-10-21T14:00:00+00:00"))        # a new version
    store.ingest(event(version="1.1", archive="Lux-1.1-hotfix.zip", when="2026-10-21T15:00:00+00:00"))  # same version, other archive
    assert records(store) == [
        (history.MOD_INSTALLED, "Lux", None, None), (history.MOD_REINSTALLED, "Lux", None, None),
        (history.MOD_UPDATED, "Lux", "1.0", "1.1"), (history.MOD_UPDATED, "Lux", "Lux-1.1.zip", "Lux-1.1-hotfix.zip")]
    [first] = [e for e in store.history_page(INSTANCE, limit=100) if e["kind"] == history.MOD_INSTALLED]
    assert first["source"] == "record" and first["profile"] == "" and first["at"] == "2026-10-21T12:00:00.000Z"
    assert first["event_id"] and history.detail(first) == "version 1.0"


def test_an_install_after_a_removal_is_an_install_not_an_update(store):
    store.ingest(event(when="2026-10-21T12:00:00+00:00"))
    assert store.mark_removed(INSTANCE, "Lux", "2026-10-21T13:00:00+00:00") is True
    store.ingest(event(version="2.0", when="2026-10-21T14:00:00+00:00"))
    assert [(k, n) for k, n, _b, _a in records(store)] == [
        (history.MOD_INSTALLED, "Lux"), (history.MOD_REMOVED, "Lux"), (history.MOD_INSTALLED, "Lux")]
    assert store.mark_removed(INSTANCE, "Lux", "2026-10-21T15:00:00+00:00") is True
    assert store.mark_removed(INSTANCE, "Lux", "2026-10-21T16:00:00+00:00") is False        # already removed: nothing more
    assert [k for k, *_ in records(store)].count(history.MOD_REMOVED) == 2


def test_removal_found_at_start_is_marked_and_a_rename_is_one_entry_but_a_case_change_is_none(store):
    store.ingest(event("Old Name"))
    assert store.rename_mod(INSTANCE, "Old Name", "OLD NAME", offline=True) is True            # only the case: no entry
    assert store.rename_mod(INSTANCE, "OLD NAME", "New Name", offline=True) is True
    # Renames are stamped with the real clock, so the removal must be too (as the plugin does), not a fixed date.
    removed_at = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(timespec="milliseconds")
    assert store.mark_removed(INSTANCE, "New Name", removed_at, offline=True) is True
    found = store.history_page(INSTANCE, limit=10)
    assert [(e["kind"], e["offline"]) for e in found] == [(history.MOD_REMOVED, True), (history.MOD_RENAMED, True), (history.MOD_INSTALLED, False)]
    assert found[1]["items"] == [{"name": "New Name", "before": "OLD NAME"}]
    assert history.title(found[1]) == "Renamed OLD NAME to New Name"
    # Searching finds the mod under either name.
    assert [e["kind"] for e in store.history_page(INSTANCE, search="old name")] == [history.MOD_RENAMED, history.MOD_INSTALLED]
    assert [e["kind"] for e in store.history_page(INSTANCE, search="new name")] == [history.MOD_REMOVED, history.MOD_RENAMED]


def test_a_mod_found_by_a_scan_is_not_a_change_but_one_seen_being_installed_is(store, tmp_path):
    store.ingest(event("Scanned", kind="plain", origin="scan"))
    store.ingest(event("Rebuilt", scanned=True))
    store.ingest(event("Seen", kind="plain", origin="install"))
    assert [name for _k, name, *_ in records(store)] == ["Seen"]


def test_the_plain_record_an_installers_record_replaces_leaves_one_install_in_the_history(store):
    store.ingest(event("Lux", kind="plain", when="2026-10-21T12:00:00+00:00", archive="Lux.zip"))
    store.ingest(event("Lux", when="2026-10-21T12:00:30+00:00", archive="Lux.zip"))
    [only] = store.history_page(INSTANCE, limit=10)
    assert only["kind"] == history.MOD_INSTALLED and only["event_id"]
    assert store.history_page(INSTANCE, limit=10) == store.history_page(INSTANCE, limit=10)


def test_ingesting_the_same_event_twice_adds_one_entry(store):
    body = event()
    assert store.ingest(body) is True and store.ingest(body) is False
    assert store.history_count(INSTANCE) == 1


def test_a_record_replaces_the_installed_entry_a_snapshot_made_a_moment_before(store):
    store.history_add(INSTANCE, "Default", [{"kind": history.MOD_INSTALLED, "count": 1, "items": [{"name": "Lux"}]}], at="2026-10-21T12:00:03.000Z")
    store.ingest(event("Lux", when="2026-10-21T12:00:01+00:00"))
    [only] = store.history_page(INSTANCE, limit=10)
    assert only["source"] == "record" and only["event_id"]


def test_ingesting_a_thousand_installs_stays_quick_because_the_lookup_uses_an_index(tmp_path):
    with EventStore(tmp_path / "many.sqlite3") as many:
        many._connection.execute("PRAGMA synchronous = OFF")
        started = time.perf_counter()
        for n in range(1000):
            many.ingest(event(f"Mod {n}", when=(datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n)).isoformat()))
        took = time.perf_counter() - started
        assert many.history_count(INSTANCE) == 1000 and took < 8


# ---------- migration ----------

def _database_at(path: Path, version: int) -> None:
    """A database exactly as the release that wrote ``version`` left it: its migrations, in order, and nothing else."""

    connection = sqlite3.connect(path)
    for steps in storage._MIGRATIONS[:version - 4]:
        for statement in steps:
            connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {version}")
    connection.commit()
    connection.close()


def _put_event(path: Path, body: dict, version: int) -> None:
    import json
    connection = sqlite3.connect(path)
    columns = "event_id, instance_id, mod_name, archive, started_at, completed_at, raw_json"
    values = [body["event_id"], body["instance"]["id"], body["mod"]["name"], body["source"]["archive"],
              body["started_at"], body["completed_at"], json.dumps(body, sort_keys=True)]
    if version >= 8:
        columns, values = columns + ", kind", values + ["fomod"]
    connection.execute(f"INSERT INTO events ({columns}) VALUES ({','.join('?' * len(values))})", values)
    connection.commit()
    connection.close()


@pytest.mark.parametrize("version", [5, 6, 7, 8, 9])
def test_a_database_of_every_earlier_version_gains_the_history_additively_and_keeps_its_events(tmp_path, version):
    path = tmp_path / f"v{version}.sqlite3"
    _database_at(path, version)
    body = event("Kept", when="2026-10-01T12:00:00+00:00")
    _put_event(path, body, version)
    with EventStore(path) as migrated:
        assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_DATABASE_VERSION == 10
        assert migrated.event(body["event_id"])["mod"]["name"] == "Kept" and migrated.event_count() == 1
        names = {row[0] for row in migrated._connection.execute("SELECT name FROM sqlite_master")}
        assert {"history", "history_snapshots", "history_when", "history_kind", "history_event", "events_mod_name",
                "events_override_name"} <= names
        assert migrated.history_page(INSTANCE) == [] and migrated.history_snapshot(INSTANCE, "Default") is None
        # It works as a database of this version does: the next install is told, an update is told against the old record.
        assert migrated.ingest(event("Kept", version="2.0", when="2026-10-02T12:00:00+00:00")) is True
        [entry] = migrated.history_page(INSTANCE)
        assert entry["kind"] == history.MOD_UPDATED and entry["items"][0]["before"] == "1.0"
    with EventStore(path) as again:           # and a second open changes nothing
        assert again.history_count(INSTANCE) == 1


def test_the_migration_to_the_history_is_atomic_and_leaves_the_old_database_untouched_when_it_fails(tmp_path, monkeypatch):
    path = tmp_path / "v9.sqlite3"
    _database_at(path, 9)
    _put_event(path, event("Kept"), 9)
    broken = (*storage._MIGRATIONS[:-1], (*storage._MIGRATIONS[-1][:-1], "CREATE INDEX events_nothing ON no_such_table (x)"))
    monkeypatch.setattr(storage, "_MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        EventStore(path)
    monkeypatch.undo()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
    assert not [row for row in connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'history%'")]
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    connection.close()
    with EventStore(path) as fixed:          # and the next open (with the working migration) completes it
        assert fixed.event_count() == 1 and fixed.history_count(INSTANCE) == 0


def test_a_database_from_a_newer_build_is_refused_not_read(tmp_path):
    path = tmp_path / "future.sqlite3"
    with EventStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {CURRENT_DATABASE_VERSION + 1}")
    connection.commit()
    connection.close()
    with pytest.raises(storage.DatabaseVersionError):
        EventStore(path)


# ---------- renames ----------

def test_a_mod_that_vanishes_while_one_appears_from_the_same_archive_is_a_rename():
    old = snap(["Lux", "Other"], versions={"Lux": ("1.0", "Lux-1.0.zip"), "Other": ("2", "Other.zip")})
    new = snap(["Lux Renamed", "Other"], versions={"Lux Renamed": ("1.0", "lux-1.0.ZIP"), "Other": ("2", "Other.zip")})
    [entry] = history.diff(old, new)
    assert entry["kind"] == history.MOD_RENAMED and entry["items"] == [{"name": "Lux Renamed", "before": "Lux"}]
    assert history.title(entry) == "Renamed Lux to Lux Renamed"


def test_two_mods_from_one_archive_or_no_known_archive_are_never_guessed_at():
    versions = {"A": ("1", "same.zip"), "B": ("1", "same.zip"), "C": ("1", "same.zip")}
    old, new = snap(["A", "B"], versions={k: versions[k] for k in "AB"}), snap(["C"], versions={"C": versions["C"]})
    assert sorted(entry["kind"] for entry in history.diff(old, new)) == [history.MOD_INSTALLED, history.MOD_REMOVED]
    assert sorted(entry["kind"] for entry in history.diff(snap(["A"]), snap(["B"]))) == [history.MOD_INSTALLED, history.MOD_REMOVED]
    no_archive = {"A": ("1", ""), "B": ("1", "")}
    assert sorted(e["kind"] for e in history.diff(snap(["A"], versions={"A": no_archive["A"]}), snap(["B"], versions={"B": no_archive["B"]}))) == [
        history.MOD_INSTALLED, history.MOD_REMOVED]


def test_a_rename_the_look_saw_is_not_told_again_when_the_record_catches_up_and_the_other_way_round(store):
    store.ingest(event("Old Name"))
    seen = [{"kind": history.MOD_RENAMED, "count": 1, "items": [{"name": "New Name", "before": "Old Name"}]}]
    store.history_add(INSTANCE, "Default", seen, at=at(60))                  # live: the snapshot diff
    assert store.rename_mod(INSTANCE, "Old Name", "New Name", offline=True) is True      # next start: the record follows
    assert [e["kind"] for e in store.history_page(INSTANCE, kinds=[history.MOD_RENAMED])] == [history.MOD_RENAMED]
    # And a record first, then the snapshot's (found at start): one entry again.
    store.ingest(event("Second Old"))
    store.rename_mod(INSTANCE, "Second Old", "Second New", offline=True)
    store.history_add(INSTANCE, "Default", [{"kind": history.MOD_RENAMED, "count": 1, "items": [{"name": "Second New", "before": "Second Old"}]}],
                      at=history.utc_text(), offline=True, dedupe_since="2000-01-01T00:00:00.000Z")
    assert len(store.history_page(INSTANCE, kinds=[history.MOD_RENAMED])) == 2


# ---------- scans are not changes (the real job paths) ----------

def test_a_scan_of_a_plain_mod_and_a_scan_of_a_fomod_plus_mod_add_nothing_to_the_history_but_an_install_does(tmp_path, store):
    from test_plain_records import plain_event
    from test_plus_plugin import _grass_zip, _job as plus_job
    from fomod_logger_support.plus import jobs as plus_jobs

    scanned = plain_event(tmp_path, name="Scanned Plain", kind="scan", when="")
    assert scanned["plain"]["origin"] == "scan" and scanned["source"]["scanned"] is True
    archive = _grass_zip(tmp_path)
    rebuilt = plus_jobs.run_job(plus_job(tmp_path, archive=archive, kind="scan", mod_name="Scanned Rebuilt"))
    names_only = plus_jobs.run_job(plus_job(tmp_path, archive=tmp_path / "gone.zip", kind="scan", mod_name="Scanned Names"))
    assert (rebuilt.mode, names_only.mode) == (plus_jobs.REBUILT, plus_jobs.NAMES_ONLY)
    for event_ in (scanned, rebuilt.event, names_only.event):
        assert event_["source"]["scanned"] is True
        assert store.ingest(event_) is True
    assert store.history_instances() == []                      # not one entry, for any instance
    # The same jobs as installs are changes that were seen.
    installed = plus_jobs.run_job(plus_job(tmp_path, archive=archive, kind="install", mod_name="Installed Rebuilt"))
    plain_install = plain_event(tmp_path, name="Installed Plain", kind="install", when="2026-10-21T12:00:00.000Z")
    assert "scanned" not in installed.event["source"] and "scanned" not in plain_install["source"]
    store.ingest(installed.event)
    store.ingest(plain_install)
    [instance] = store.history_instances()
    assert sorted(i["name"] for e in store.history_page(instance) for i in e["items"]) == ["Installed Plain", "Installed Rebuilt"]
