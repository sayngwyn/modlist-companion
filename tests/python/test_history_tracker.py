"""What the plugin remembers between two looks at the list: live changes, changes found at start (made while MO2 was
closed or by another tool), profile switches, bursts, and the programs MO2 starts."""

from __future__ import annotations

import time

import pytest

from fomod_logger_support.core import history
from fomod_logger_support.core.storage import EventStore
from fomod_logger_support.history_tracker import HistoryTracker, program_name

INSTANCE = "c" * 64


class Clock:
    def __init__(self, start="2026-10-21T20:00:00.000Z"):
        self.now = start

    def __call__(self):
        return self.now

    def tick(self, seconds: float) -> str:
        self.now = history.shifted(self.now, seconds)
        return self.now


class Session:
    """One run of MO2 with the plugin: a tracker on the database, an instance and a profile that can change."""

    def __init__(self, database, clock, *, profile="Default", instance=INSTANCE, keep=history.KEEP_ENTRIES):
        self.profile, self.instance = profile, instance
        self.tracker = HistoryTracker(lambda: database, lambda: self.instance, lambda: self.profile, clock=clock, keep=keep)
        self.database = database

    def entries(self, **options):
        with EventStore(self.database) as store:
            return store.history_page(INSTANCE, limit=500, **options)

    def kinds(self, **options):
        return [(e["kind"], tuple(i["name"] for i in e["items"]), e["offline"]) for e in self.entries(**options)][::-1]


def snap(mods, enabled=None, plugins=(), active=(), versions=None):
    return history.make_snapshot(mods, mods if enabled is None else enabled, plugins, active, versions)


@pytest.fixture
def world(tmp_path):
    clock = Clock()
    return clock, tmp_path / "db.sqlite3"


def test_the_first_look_is_a_baseline_and_a_repeat_of_it_writes_nothing(world):
    clock, database = world
    session = Session(database, clock)
    base = snap(["A", "B"], plugins=["a.esp"], active=["a.esp"])
    assert session.tracker.look(base) == 0 and session.entries() == []
    with EventStore(database) as store:
        assert store.history_snapshot(INSTANCE, "Default")[0] == base
    clock.tick(60)
    assert session.tracker.look(dict(base)) == 0
    with EventStore(database) as store:
        assert store.history_snapshot(INSTANCE, "Default")[1] == "2026-10-21T20:00:00.000Z"     # not rewritten


def test_live_changes_are_entries_that_are_not_marked_offline(world):
    clock, database = world
    session = Session(database, clock)
    session.tracker.look(snap(["A", "B", "C"], plugins=["a.esp", "b.esp", "c.esp"], active=["a.esp", "b.esp"]))
    clock.tick(30)
    # A plugin is switched off, a mod is switched off, and one mod and one plugin are moved.
    assert session.tracker.look(snap(["A", "C", "B"], enabled=["A", "B"], plugins=["a.esp", "c.esp", "b.esp"], active=["a.esp"])) == 4
    assert sorted(session.kinds()) == sorted([
        (history.MOD_DISABLED, ("C",), False), (history.MOD_MOVED, ("C",), False),
        (history.PLUGIN_DISABLED, ("b.esp",), False), (history.PLUGIN_MOVED, ("c.esp",), False)])
    assert all(e["source"] == "snapshot" and e["profile"] == "Default" and e["at"] == clock.now for e in session.entries())


def test_one_refresh_or_a_drag_is_one_grouped_entry_and_later_changes_are_new_ones(world):
    clock, database = world
    session = Session(database, clock)
    plugins = [f"p{n:02d}.esp" for n in range(40)]
    session.tracker.look(snap(["A"], plugins=plugins, active=plugins))
    shuffled = plugins[:5] + plugins[20:32] + plugins[5:20] + plugins[32:]
    clock.tick(30)
    session.tracker.look(snap(["A"], plugins=shuffled, active=plugins))
    [moved] = session.entries()
    assert moved["kind"] == history.PLUGIN_MOVED and moved["count"] == 12 and history.title(moved) == "12 plugins moved"
    # A second look a moment later (MO2 calls back once per item) joins the same entry; a minute later it does not.
    again = shuffled[:2] + [shuffled[30]] + shuffled[2:30] + shuffled[31:]
    clock.tick(1)
    session.tracker.look(snap(["A"], plugins=again, active=plugins))
    [joined] = session.entries()
    assert joined["count"] == 13
    clock.tick(60)
    session.tracker.look(snap(["A"], plugins=again[1:] + again[:1], active=plugins))
    assert len(session.entries()) == 2


def test_changes_made_while_mo2_was_closed_are_found_at_the_next_start_and_marked(world):
    clock, database = world
    before = Session(database, clock)
    before.tracker.look(snap(["A", "B", "C"], enabled=["A", "B"], plugins=["a.esp", "b.esp"], active=["a.esp", "b.esp"],
                             versions={"A": ("1.0", "A.zip")}))
    # MO2 closes. Another tool edits plugins.txt and modlist.txt, and a mod folder is replaced.
    clock.tick(3600)
    after = Session(database, clock)
    changed = snap(["B", "A", "C", "D"], enabled=["A", "C", "D"], plugins=["a.esp", "b.esp"], active=["a.esp"],
                   versions={"A": ("2.0", "A2.zip")})
    assert after.tracker.look(changed) > 0
    found = {kind: (names, offline) for kind, names, offline in after.kinds()}
    assert found[history.MOD_INSTALLED] == (("D",), True)
    assert found[history.MOD_ENABLED] == (("C",), True)
    assert found[history.MOD_DISABLED] == (("B",), True)
    assert found[history.PLUGIN_DISABLED] == (("b.esp",), True)
    assert found[history.MOD_UPDATED] == (("A",), True)
    assert history.MOD_MOVED in found
    assert all(e["offline"] and e["at"] == clock.now for e in after.entries())
    clock.tick(5)
    assert after.tracker.look(changed) == 0 and len(after.entries()) == len(found)         # found once, then it is the baseline


def test_an_unchanged_list_at_the_next_start_finds_nothing(world):
    clock, database = world
    base = snap(["A", "B"], plugins=["a.esp"], active=["a.esp"], versions={"A": ("1", "A.zip")})
    Session(database, clock).tracker.look(base)
    clock.tick(600)
    later = Session(database, clock)
    assert later.tracker.look(base) == 0 and later.entries() == []


def test_an_offline_install_that_a_record_already_told_is_not_told_twice(world):
    clock, database = world
    Session(database, clock).tracker.look(snap(["A"]))
    clock.tick(600)
    with EventStore(database) as store:                    # the plugin saw the install of B (a record) before it noticed it
        with store._connection:
            from fomod_logger_support.core import history_store
            history_store.add_record(store._connection, INSTANCE, {"kind": history.MOD_INSTALLED, "count": 1, "items": [{"name": "B"}]},
                                     history.shifted(clock.now, -30))
    later = Session(database, clock)
    later.tracker.look(snap(["A", "B", "C"]))
    assert sorted((e["kind"], e["items"][0]["name"], e["source"]) for e in later.entries()) == [
        (history.MOD_INSTALLED, "B", "record"), (history.MOD_INSTALLED, "C", "snapshot")]


def test_a_new_profile_without_a_snapshot_is_only_a_baseline_and_a_known_one_is_compared_with_its_own(world):
    clock, database = world
    session = Session(database, clock, profile="Default")
    session.tracker.look(snap(["A", "B"], plugins=["a.esp"], active=["a.esp"]))
    clock.tick(60)
    session.profile = "Hardcore"
    session.tracker.profile_switched("Default", "Hardcore")
    assert session.tracker.switch_pending
    # While the switch is pending nothing is compared: the new profile's lists may be half loaded.
    assert session.tracker.look(snap(["A"], plugins=[], active=[])) == 0
    assert session.tracker.settle_switch() is True and session.tracker.settle_switch() is False
    clock.tick(2)
    assert session.tracker.look(snap(["A", "B"], enabled=["A"], plugins=["a.esp"], active=[])) == 0     # a baseline for Hardcore
    assert session.kinds() == [(history.PROFILE_SWITCHED, ("Hardcore",), False)]
    [switched] = session.entries()
    assert switched["profile"] == "Hardcore" and switched["items"] == [{"name": "Hardcore", "before": "Default", "after": "Hardcore"}]
    assert history.title(switched) == "Switched to profile Hardcore"

    clock.tick(600)
    session.tracker.look(snap(["A", "B"], enabled=["A", "B"], plugins=["a.esp"], active=["a.esp"]))     # live change in Hardcore
    # Back to Default: its list changed in the meantime (another tool), and that is found against Default's own snapshot.
    clock.tick(60)
    session.profile = "Default"
    session.tracker.profile_switched("Hardcore", "Default")
    session.tracker.settle_switch()
    session.tracker.look(snap(["A", "B", "C"], plugins=["a.esp"], active=["a.esp"]))
    names = {(kind, names, offline) for kind, names, offline in session.kinds(profile="Default")}
    assert (history.MOD_INSTALLED, ("C",), True) in names and (history.PROFILE_SWITCHED, ("Default",), False) in names
    assert not any(kind == history.MOD_ENABLED and offline for kind, _n, offline in session.kinds(profile="Default"))


def test_starting_in_another_profile_than_the_one_in_use_when_mo2_closed_is_a_switch_found_at_start(world):
    clock, database = world
    Session(database, clock, profile="Default").tracker.look(snap(["A"]))
    clock.tick(30)
    Session(database, clock, profile="Hardcore").tracker.look(snap(["A"]))       # a baseline for Hardcore, written last
    clock.tick(600)
    later = Session(database, clock, profile="Default")
    later.tracker.look(snap(["A"]))
    # (The second session, which started in Hardcore after Default, found its own switch too.)
    assert [(e["kind"], e["offline"], e["profile"]) for e in later.entries()] == [
        (history.PROFILE_SWITCHED, True, "Default"), (history.PROFILE_SWITCHED, True, "Hardcore")]
    assert later.entries()[0]["items"] == [{"name": "Default", "before": "Hardcore", "after": "Default"}]


def test_a_look_says_nothing_when_mo2_has_not_read_its_list_or_the_instance_is_unknown(world):
    clock, database = world
    session = Session(database, clock)
    session.tracker.look(snap(["A", "B"]))
    clock.tick(60)
    assert session.tracker.look(snap([])) == 0            # MO2 hasn't read its list: it isn't "every mod removed"
    assert session.entries() == []
    session.profile = ""
    assert session.tracker.look(snap(["A"])) == 0
    session.profile, session.instance = "Default", ""
    assert session.tracker.look(snap(["A", "Z"])) == 0
    assert session.tracker.run_started(r"C:\Game\Game.exe") is None
    assert HistoryTracker(lambda: None, lambda: INSTANCE, lambda: "Default").look(snap(["A"])) == 0


def test_switching_the_plugin_off_and_on_forgets_what_was_last_seen(world):
    clock, database = world
    session = Session(database, clock)
    session.tracker.look(snap(["A"]))
    session.tracker.profile_switched("Default", "Other")
    session.tracker.reset()
    assert (session.tracker.last, session.tracker.key, session.tracker.switch_pending) == (None, None, False)


def test_the_history_stays_within_its_cap_as_looks_pile_up(world):
    clock, database = world
    session = Session(database, clock, keep=30)
    plugins = [f"p{n}.esp" for n in range(5)]
    session.tracker.look(snap(["A"], plugins=plugins, active=[]))
    for n in range(60):
        clock.tick(30)
        session.tracker.look(snap(["A"], plugins=plugins, active=plugins[: n % 5 + 1]))
    assert len(session.entries()) <= 30


# ---------- programs ----------

def test_a_program_start_is_an_entry_and_its_end_fills_in_when_and_how_it_ended(world):
    clock, database = world
    session = Session(database, clock)
    exe = r"C:\Example MO2\Stock Game\skse64_loader.exe"
    session.tracker.run_started(exe)
    clock.tick(3000)
    assert session.tracker.run_finished(exe.replace("\\", "/").upper(), 0) is True        # matched without regard to case or slashes
    [run] = session.entries()
    assert run["kind"] == history.PROGRAM_RUN and run["subject"] == "Skyrim Special Edition (SKSE)"
    assert run["detail"] == {"path": exe} and run["exit_code"] == 0
    assert history.seconds(run["ended_at"]) - history.seconds(run["at"]) == 3000
    assert session.tracker.run_finished(exe, 0) is False       # it has ended already
    assert session.tracker.run_finished(r"C:\Other\Thing.exe", 1) is False


def test_an_end_goes_to_the_newest_launch_of_the_program_and_an_older_one_that_never_reported_stays_unfinished(world):
    """MO2 does not call back when a launch ends after Unlock or after it vetoed it: that launch must not take the end
    of the next one."""

    clock, database = world
    session = Session(database, clock)
    exe = r"C:\Tools\SSEEdit\SSEEdit64.exe"
    session.tracker.run_started(exe)            # never reports an end
    clock.tick(100)
    session.tracker.run_started(exe)
    clock.tick(100)
    assert session.tracker.run_finished(exe, 3221225477) is True
    first, second = session.entries()[::-1]
    assert first["ended_at"] == "" and first["exit_code"] is None
    assert "end not seen" in history.title(first)
    assert second["exit_code"] == 3221225477 and history.title(second).endswith("exited with code 0xC0000005")
    assert first["subject"] == second["subject"] == "SSEEdit"
    clock.tick(10)
    assert session.tracker.run_finished(exe, 0) is False       # the older launch is not waiting for an end any more
    assert session.entries()[::-1][0]["ended_at"] == ""


def test_changes_found_by_the_first_look_say_why_they_were_not_seen(world):
    clock, database = world
    Session(database, clock).tracker.look(snap(["A"], plugins=["a.esp"], active=["a.esp"]))
    changed = snap(["A"], plugins=["a.esp"], active=[])
    clock.tick(60)
    started = Session(database, clock)
    started.tracker.look(changed)                                       # MO2 started: changed while it was closed
    assert [(e["found"], history.detail(e)) for e in started.entries()] == [(history.FOUND_START, "found at MO2 start")]
    clock.tick(60)
    started.tracker.look(snap(["A"], plugins=["a.esp"], active=["a.esp"]))      # seen live: no mark
    assert [(e["found"]) for e in started.entries()][0] == 0
    # The profile in use changes and the new one's own snapshot is older than its lists: found when it was opened.
    with EventStore(database) as store:
        store.history_save(INSTANCE, "Other", snap(["A"], plugins=["a.esp"], active=["a.esp"]), [], at="2026-10-21T19:00:00.000Z")
    started.profile = "Other"
    started.tracker.profile_switched("Default", "Other")
    started.tracker.settle_switch()
    clock.tick(60)
    started.tracker.look(snap(["A"], plugins=["a.esp"], active=[]))
    other = [e for e in started.entries(profile="Other") if e["kind"] == history.PLUGIN_DISABLED]
    assert [(e["found"], history.detail(e)) for e in other] == [(history.FOUND_PROFILE, "found when this profile was opened")]
    # Companion switched off and on again while MO2 runs: found when it was switched on.
    clock.tick(60)
    started.tracker.reset(again=True)
    started.tracker.look(snap(["A"], plugins=["a.esp"], active=["a.esp"]))       # on again, as Other's kept snapshot has it off
    again = [e for e in started.entries(profile="Other") if e["found"] == history.FOUND_RESTART]
    assert [(e["kind"], history.detail(e)) for e in again] == [(history.PLUGIN_ENABLED, "found when Companion was switched on")]


def test_a_program_still_running_when_mo2_closes_has_no_end(world):
    clock, database = world
    Session(database, clock).tracker.run_started(r"C:\Game\Game.exe")
    [run] = Session(database, clock).entries()
    assert run["ended_at"] == "" and run["exit_code"] is None
    assert history.title(run).startswith("Game started ") and "end not seen" in history.title(run)


def test_a_program_is_named_by_its_usual_name_or_its_file_name():
    assert program_name(r"C:\Games\Stock Game\SkyrimSE.exe") == "Skyrim Special Edition"
    assert program_name("C:/Tools/FO4Edit64.exe") == "FO4Edit"
    assert program_name(r"D:\Mods\Tools\BodySlide x64.exe") == "BodySlide"
    assert program_name(r"D:\Tools\MyTool.exe") == "MyTool" and program_name("noext") == "noext" and program_name("") == "A program"


# ---------- cost ----------

def test_a_look_at_a_big_list_costs_milliseconds(world, capsys):
    """2,000 mods and 4,000 plugins: the diff and the write of a change, measured (printed with -s)."""

    clock, database = world
    mods = [f"Mod number {n:04d}" for n in range(2000)]
    plugins = [f"plugin{n:04d}.esp" for n in range(4000)]
    session = Session(database, clock)
    versions = {name: ("1.0", f"{name}.zip") for name in mods}
    base = snap(mods, plugins=plugins, active=plugins[:3000], versions=versions)
    session.tracker.look(base)
    moved_mods = mods[:100] + mods[1000:1020] + mods[100:1000] + mods[1020:]
    changed = snap(moved_mods, enabled=mods[1:], plugins=plugins[1:] + plugins[:1], active=plugins[:2990], versions=versions)
    started = time.perf_counter()
    entries = history.diff(base, changed)
    diff_seconds = time.perf_counter() - started
    clock.tick(60)
    started = time.perf_counter()
    added = session.tracker.look(changed)
    look_seconds = time.perf_counter() - started
    with capsys.disabled():
        print(f"\n[history] 2000 mods + 4000 plugins: diff {diff_seconds * 1000:.1f} ms, look (diff + write) {look_seconds * 1000:.1f} ms")
    assert added == len(entries) >= 4 and diff_seconds < 0.5 and look_seconds < 1.0


def test_a_profile_set_for_the_first_time_at_start_up_is_not_a_switch(world):
    clock, database = world
    session = Session(database, clock)
    assert session.tracker.profile_switched("", "Default") == 0 and session.entries() == []
    assert session.tracker.switch_pending is False                   # nothing to wait for: the first look waits for MO2 anyway
    assert session.tracker.profile_switched("Default", "Default") == 0 and session.entries() == []
