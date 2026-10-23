"""The change history in both editions of the plugin, driven through the callbacks MO2 would raise: the snapshot the
plugin builds from MO2's lists, the entries each kind of change makes, bursts, changes found when MO2 starts, profile
switches, and the programs MO2 starts. MO2 and Qt are fakes, as in test_plugin_contract.py."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from fomod_logger_support.core import history
from fomod_logger_support.core.identity import stable_instance_id
from fomod_logger_support.core.storage import EventStore
from test_plugin_contract import _FakeOrganizer, _load_plugin_module

EDITIONS = [("fomod_logger_plugin.py", "FomodLoggerPlugin", "FOMOD Logger Native Installer"),
            ("fomod_logger_plus_plugin.py", "FomodLoggerPlusPlugin", "FOMOD Plus")]


class _Mod:
    def __init__(self, name: str, folder: Path, version: str = "1.0", archive: str = ""):
        self._name, self.folder, self.version_text, self.archive = name, folder, version, archive
        folder.mkdir(parents=True, exist_ok=True)

    def name(self):
        return self._name

    def version(self):
        return types.SimpleNamespace(canonicalString=lambda: self.version_text)

    def installationFile(self):
        return self.archive

    def absolutePath(self):
        return str(self.folder)

    def nexusId(self):
        return 0


class _ModList:
    ACTIVE = 2

    def __init__(self, root: Path):
        self.root, self.order, self.mods, self.enabled = root, [], {}, set()
        self.priority_calls = 0
        self.installed_callbacks, self.removed_callbacks = [], []
        self.state_callbacks, self.moved_callbacks = [], []

    def add(self, name: str, *, enabled: bool = True, version: str = "1.0", archive: str = "") -> None:
        self.mods[name] = _Mod(name, self.root / "mods" / name, version, archive)
        self.order.append(name)
        if enabled:
            self.enabled.add(name)

    def allMods(self):
        return sorted(self.mods)             # not in priority order, as MO2's isn't

    def allModsByProfilePriority(self):
        self.priority_calls += 1
        return list(self.order)

    def state(self, name):
        return self.ACTIVE if name in self.enabled else 1

    def getMod(self, name):
        return self.mods.get(name)

    def onModInstalled(self, callback):
        self.installed_callbacks.append(callback)
        return True

    def onModRemoved(self, callback):
        self.removed_callbacks.append(callback)
        return True

    def onModStateChanged(self, callback):
        self.state_callbacks.append(callback)
        return True

    def onModMoved(self, callback):
        self.moved_callbacks.append(callback)
        return True


class _PluginList:
    def __init__(self):
        self.order: list[str] = []
        self.active: set[str] = set()
        self.origins: dict[str, str] = {}
        self.refreshed_callbacks, self.state_callbacks, self.moved_callbacks = [], [], []

    def add(self, name: str, mod: str, *, active: bool = True) -> None:
        self.order.append(name)
        self.origins[name] = mod
        if active:
            self.active.add(name)

    def pluginNames(self):
        return sorted(self.order)

    def state(self, name):
        return 2 if name in self.active else 1

    def origin(self, name):
        return self.origins[name]

    def masters(self, name):
        return []

    def priority(self, name):
        return self.order.index(name)

    def onRefreshed(self, callback):
        self.refreshed_callbacks.append(callback)

    def onPluginStateChanged(self, callback):
        self.state_callbacks.append(callback)

    def onPluginMoved(self, callback):
        self.moved_callbacks.append(callback)


class _Org(_FakeOrganizer):
    def __init__(self, root: Path, native: str):
        super().__init__(root)
        self.root = root
        self.mod_list = _ModList(root)
        self.plugin_list = _PluginList()
        self.profile = "Default"
        self.enabled_plugins = {native}
        self.profile_callbacks, self.about_callbacks, self.finished_callbacks, self.setting_callbacks = [], [], [], []
        self.about_result = True
        (root / "profiles" / "Default").mkdir(parents=True, exist_ok=True)
        (root / "profiles" / "Hardcore").mkdir(parents=True, exist_ok=True)

    def basePath(self):
        return str(self.root / "instance")

    def profilePath(self):
        return str(self.root / "profiles" / self.profile)

    def pluginList(self):
        return self.plugin_list

    def managedGame(self):
        return types.SimpleNamespace(primaryPlugins=lambda: ["Skyrim.esm"], DLCPlugins=lambda: [], CCPlugins=lambda: [])

    def onProfileChanged(self, callback):
        self.profile_callbacks.append(callback)

    def onPluginSettingChanged(self, callback):
        self.setting_callbacks.append(callback)

    def onAboutToRun(self, callback):
        self.about_callbacks.append(callback)
        return True

    def onFinishedRun(self, callback):
        self.finished_callbacks.append(callback)
        return True


def _world(root: Path, native: str) -> _Org:
    org = _Org(root, native)
    for mod in ("Skyrim Base", "Lux", "Cathedral"):
        org.mod_list.add(mod)
    org.plugin_list.add("Skyrim.esm", "Skyrim Base")
    org.plugin_list.add("Lux.esp", "Lux")
    org.plugin_list.add("Cathedral.esp", "Cathedral")
    return org


class _Run:
    """One MO2 session with one edition's plugin: ``start`` (plugin starts and MO2 reads its list) and ``stop``."""

    def __init__(self, monkeypatch, tmp_path: Path, edition, org: _Org, name: str):
        entry, cls, _native = edition
        self.module, self.timer, self.application = _load_plugin_module(monkeypatch, name, entry)
        mobase = sys.modules["mobase"]
        monkeypatch.setattr(mobase, "PluginState", types.SimpleNamespace(ACTIVE=2), raising=False)
        monkeypatch.setattr(mobase, "ModState", types.SimpleNamespace(ACTIVE=2), raising=False)
        self.org = org
        self.plugin = getattr(self.module, cls)()
        self.database = tmp_path / "plugin_data" / self.plugin.DATA_FOLDER / "fomod_logger.sqlite3"
        self.instance = stable_instance_id(org.basePath())
        org.pluginDataPath = lambda: str(tmp_path / "plugin_data")
        self.notified = []
        self.plugin._notify_viewer = lambda: self.notified.append(1)

    def start(self) -> "_Run":
        assert self.plugin.init(self.org) is True and self.plugin._active
        self.org.refresh_callbacks[-1]()          # MO2 has read its list: the first look that counts
        return self

    def stop(self) -> None:
        self.plugin._shutdown()

    def look(self) -> None:
        self.plugin._capture_load_order()

    def entries(self, **options):
        with EventStore(self.database) as store:
            return store.history_page(self.instance, limit=500, **options)[::-1]

    def kinds(self, **options):
        return [(e["kind"], tuple(i["name"] for i in e["items"]), e["offline"]) for e in self.entries(**options)]


@pytest.fixture(params=EDITIONS, ids=["full", "plus"])
def edition(request):
    return request.param


def _run(monkeypatch, tmp_path, edition, org=None, name="history_run") -> _Run:
    return _Run(monkeypatch, tmp_path, edition, org or _world(tmp_path, edition[2]), name)


def test_the_first_look_after_mo2_reads_its_list_is_a_baseline_and_nothing_before_it_is_judged(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition)
    assert run.plugin.init(run.org) is True
    run.look()                                   # start-up's look, before MO2 has read its list
    # MO2 has no profile yet: asking it for the mods in priority order then ends MO2 (found by the live run), so
    # nothing of the history is read before the first refresh.
    assert run.org.mod_list.priority_calls == 0
    with EventStore(run.database) as store:       # nothing was judged or kept yet
        assert store.history_snapshot(run.instance, "Default") is None and store.history_count(run.instance) == 0
    run.org.refresh_callbacks[-1]()
    assert run.entries() == []
    with EventStore(run.database) as store:
        snapshot, _taken = store.history_snapshot(run.instance, "Default")
    assert snapshot["mods"] == ["Skyrim Base", "Lux", "Cathedral"] and snapshot["plugins"] == ["Skyrim.esm", "Lux.esp", "Cathedral.esp"]
    assert sorted(snapshot["enabled"]) == ["Cathedral", "Lux", "Skyrim Base"]
    assert sorted(snapshot["active"]) == ["Cathedral.esp", "Lux.esp", "Skyrim.esm"]
    assert snapshot["versions"]["Lux"] == ["1.0", ""]


def test_the_callbacks_the_history_needs_are_registered_and_each_may_be_missing(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()
    org = run.org
    assert len(org.mod_list.moved_callbacks) == 1 and len(org.plugin_list.moved_callbacks) == 1
    assert len(org.about_callbacks) == 1 and len(org.finished_callbacks) == 1 and len(org.profile_callbacks) == 1
    assert len(org.plugin_list.refreshed_callbacks) == 1 and len(org.mod_list.state_callbacks) == 1
    # An MO2 without any of them still starts the plugin.
    missing = _world(tmp_path / "other", edition[2])
    broken = _run(monkeypatch, tmp_path / "other", edition, missing, "history_missing")
    missing.onAboutToRun = lambda callback: (_ for _ in ()).throw(AttributeError("no onAboutToRun"))
    missing.mod_list.onModMoved = lambda callback: (_ for _ in ()).throw(AttributeError("no onModMoved"))
    assert broken.plugin.init(missing) is True and broken.plugin._active
    assert broken.plugin._runtime_error == ""


def test_switching_a_plugin_or_a_mod_off_and_moving_things_each_make_their_entry(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()
    org = run.org
    org.plugin_list.active.discard("Lux.esp")
    org.plugin_list.state_callbacks[0]({})
    assert run.kinds() == [(history.PLUGIN_DISABLED, ("Lux.esp",), False)]
    org.mod_list.enabled.discard("Cathedral")
    org.mod_list.state_callbacks[0]({})
    assert run.kinds()[-1] == (history.MOD_DISABLED, ("Cathedral",), False)
    # A mod and a plugin are dragged: MO2 calls back per item, the look waits for the drag to end.
    org.mod_list.order = ["Cathedral", "Skyrim Base", "Lux"]
    org.plugin_list.order = ["Skyrim.esm", "Cathedral.esp", "Lux.esp"]
    before = len(run.entries())
    org.mod_list.moved_callbacks[0]("Cathedral", 2, 0)
    org.plugin_list.moved_callbacks[0]("Cathedral.esp", 2, 1)
    assert len(run.entries()) == before                                   # nothing yet: the look is waiting
    timer = run.timer.instances[-1]
    assert timer.single is True and timer.start_count == 2
    timer.timeout.emit()
    kinds = [kind for kind, *_ in run.kinds()]
    assert history.MOD_MOVED in kinds and history.PLUGIN_MOVED in kinds
    [moved] = [e for e in run.entries() if e["kind"] == history.MOD_MOVED]
    assert moved["items"] == [{"name": "Cathedral", "before": 2, "after": 0}] and run.notified


def test_a_refresh_that_changes_nothing_writes_nothing_and_does_not_wake_the_window(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()
    run.notified.clear()
    with EventStore(run.database) as store:
        taken = store.history_snapshot(run.instance, "Default")[1]
    for callback in run.org.plugin_list.refreshed_callbacks:
        callback()
    run.look()
    with EventStore(run.database) as store:
        assert store.history_snapshot(run.instance, "Default")[1] == taken
    assert run.entries() == []


def test_a_mod_enabled_by_the_user_and_a_new_mod_are_different_things(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()
    org = run.org
    org.mod_list.add("Fresh", enabled=False)
    org.plugin_list.add("Fresh.esp", "Fresh", active=False)
    run.look()
    assert run.kinds() == [(history.MOD_INSTALLED, ("Fresh",), False)]     # appeared (no install record said so)
    org.mod_list.enabled.add("Fresh")
    org.mod_list.state_callbacks[0]({})
    assert run.kinds()[-1] == (history.MOD_ENABLED, ("Fresh",), False)


def test_changes_made_while_mo2_was_closed_are_found_when_it_starts(monkeypatch, tmp_path, edition):
    first = _run(monkeypatch, tmp_path, edition, name="history_first").start()
    org = first.org
    first.stop()
    # Closed: a tool edits plugins.txt (a plugin off), modlist.txt (a mod off, a mod moved) and a mod is updated.
    org.plugin_list.active.discard("Lux.esp")
    org.mod_list.enabled.discard("Lux")
    org.mod_list.order = ["Lux", "Skyrim Base", "Cathedral"]
    org.mod_list.mods["Cathedral"].version_text = "2.0"
    second = _Run(monkeypatch, tmp_path, edition, org, "history_second")
    org.refresh_callbacks.clear()
    second.start()
    found = {kind: (names, offline) for kind, names, offline in second.kinds()}
    assert found[history.PLUGIN_DISABLED] == (("Lux.esp",), True)
    assert found[history.MOD_DISABLED] == (("Lux",), True)
    assert found[history.MOD_UPDATED] == (("Cathedral",), True)
    assert history.MOD_MOVED in found
    assert all(offline for _k, _n, offline in second.kinds())
    count = len(second.entries())
    second.look()
    assert len(second.entries()) == count            # found once


def test_a_mod_removed_while_mo2_was_closed_is_removed_once_not_twice(monkeypatch, tmp_path, edition):
    first = _run(monkeypatch, tmp_path, edition, name="history_removed_first").start()
    org = first.org
    with EventStore(first.database) as store:             # the install of Lux was recorded when it happened
        store.ingest({
            "schema_version": 1, "event_id": "00000001-1111-4111-8111-111111111111",
            "started_at": "2026-10-01T12:00:00+00:00", "completed_at": "2026-10-01T12:00:00+00:00",
            "instance": {"id": first.instance, "label": "MO2 Test"}, "source": {"archive": "Lux.zip", "reinstallation": False},
            "mod": {"name": "Lux", "version": "1.0", "nexus_id": 1},
            "installer": {"name": "x", "version": "1", "upstream_commit": "unknown"}, "steps": []})
    first.stop()
    del org.mod_list.mods["Lux"]
    org.mod_list.order.remove("Lux")
    org.mod_list.enabled.discard("Lux")
    org.plugin_list.order.remove("Lux.esp")
    del org.plugin_list.origins["Lux.esp"]
    org.plugin_list.active.discard("Lux.esp")
    second = _Run(monkeypatch, tmp_path, edition, org, "history_removed_second")
    org.refresh_callbacks.clear()
    second.start()
    removed = [(e["kind"], e["source"], e["offline"]) for e in second.entries() if e["kind"] == history.MOD_REMOVED]
    assert removed == [(history.MOD_REMOVED, "record", True)]


def test_a_removal_mo2_announces_is_one_entry(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()
    with EventStore(run.database) as store:
        store.ingest({
            "schema_version": 1, "event_id": "00000002-1111-4111-8111-111111111111",
            "started_at": "2026-10-01T12:00:00+00:00", "completed_at": "2026-10-01T12:00:00+00:00",
            "instance": {"id": run.instance, "label": "MO2 Test"}, "source": {"archive": "Lux.zip", "reinstallation": False},
            "mod": {"name": "Lux", "version": "1.0", "nexus_id": 1},
            "installer": {"name": "x", "version": "1", "upstream_commit": "unknown"}, "steps": []})
    org = run.org
    del org.mod_list.mods["Lux"]
    org.mod_list.order.remove("Lux")
    org.mod_list.enabled.discard("Lux")
    org.plugin_list.order.remove("Lux.esp")
    del org.plugin_list.origins["Lux.esp"]
    org.plugin_list.active.discard("Lux.esp")
    org.mod_list.removed_callbacks[0]("Lux")
    run.look()
    assert [(k, n) for k, n, _o in run.kinds() if k in (history.MOD_REMOVED, history.MOD_INSTALLED)] == [
        (history.MOD_INSTALLED, ("Lux",)), (history.MOD_REMOVED, ("Lux",))]
    assert [e["source"] for e in run.entries() if e["kind"] == history.MOD_REMOVED] == ["record"]      # the record's, not the snapshot's


def test_a_profile_switch_is_recorded_and_the_new_profiles_own_snapshot_is_what_is_compared(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()
    org = run.org
    # Hardcore was kept by an earlier session with Lux switched off; Default is the one in use now.
    with EventStore(run.database) as store:
        store.history_save(run.instance, "Hardcore", history.make_snapshot(
            ["Skyrim Base", "Lux", "Cathedral"], ["Skyrim Base", "Cathedral"], ["Skyrim.esm", "Lux.esp", "Cathedral.esp"],
            ["Skyrim.esm"], {"Skyrim Base": ("1.0", ""), "Lux": ("1.0", ""), "Cathedral": ("1.0", "")}), [], at="2026-10-20T10:00:00.000Z")
    org.profile = "Hardcore"
    old, new = types.SimpleNamespace(name=lambda: "Default"), types.SimpleNamespace(name=lambda: "Hardcore")
    org.profile_callbacks[0](old, new)
    assert run.plugin._history.switch_pending is True
    switched = [(e["kind"], e["profile"], e["offline"]) for e in run.entries()]
    assert switched == [(history.PROFILE_SWITCHED, "Hardcore", False)]
    # The plugin list refreshes once MO2 has loaded Hardcore's: only then is it compared.
    org.plugin_list.refreshed_callbacks[0]()
    assert run.plugin._history.switch_pending is False
    assert run.kinds(profile="Hardcore") == [
        (history.PROFILE_SWITCHED, ("Hardcore",), False), (history.MOD_ENABLED, ("Lux",), True),
        (history.PLUGIN_ENABLED, ("Lux.esp", "Cathedral.esp"), True)]
    assert run.kinds(profile="Default") == []             # the profile that was left shows nothing of the switch


def test_a_slow_plugin_list_refresh_after_a_profile_switch_is_never_looked_at_early(monkeypatch, tmp_path, edition):
    """On a big list MO2's refresh can take longer than any timer: until the plugin list says it has refreshed, nothing
    compares the new profile's snapshot with the plugin states of the old profile."""

    run = _run(monkeypatch, tmp_path, edition).start()
    org = run.org
    with EventStore(run.database) as store:        # Hardcore as it was kept: everything on, in the order it has now
        store.history_save(run.instance, "Hardcore", history.make_snapshot(
            ["Skyrim Base", "Lux", "Cathedral"], ["Skyrim Base", "Lux", "Cathedral"], ["Skyrim.esm", "Lux.esp", "Cathedral.esp"],
            ["Skyrim.esm", "Lux.esp", "Cathedral.esp"], {"Skyrim Base": ("1.0", ""), "Lux": ("1.0", ""), "Cathedral": ("1.0", "")}),
            [], at="2026-10-20T10:00:00.000Z")
    timers_before = len(run.timer.instances)
    org.profile = "Hardcore"
    org.profile_callbacks[0](types.SimpleNamespace(name=lambda: "Default"), types.SimpleNamespace(name=lambda: "Hardcore"))
    assert run.plugin._history.switch_pending is True and len(run.timer.instances) == timers_before    # no timer to give up on
    # The refresh is slow. Meanwhile everything else that looks at the list does so, with Default's plugin states still
    # in MO2's list (a plugin off, another moved), and every timer there is fires.
    org.plugin_list.active.discard("Lux.esp")
    org.plugin_list.order = ["Cathedral.esp", "Skyrim.esm", "Lux.esp"]
    for _ in range(3):
        run.look()
        org.plugin_list.state_callbacks[0]({})
        org.mod_list.state_callbacks[0]({})
        org.plugin_list.moved_callbacks[0]("Cathedral.esp", 2, 0)
        for timer in list(run.timer.instances):
            timer.timeout.emit()
    assert run.plugin._history.switch_pending is True
    assert run.kinds() == [(history.PROFILE_SWITCHED, ("Hardcore",), False)]           # nothing bogus: only the switch
    # MO2's refresh ends with Hardcore's lists in place: now it is compared, and only with what is really there.
    org.plugin_list.active.update({"Lux.esp"})
    org.plugin_list.order = ["Skyrim.esm", "Lux.esp", "Cathedral.esp"]
    org.plugin_list.refreshed_callbacks[0]()
    assert run.plugin._history.switch_pending is False
    assert run.kinds(profile="Hardcore") == [(history.PROFILE_SWITCHED, ("Hardcore",), False)]    # equal to what was kept


def test_a_mod_updated_while_mo2_runs_is_told_live_not_found_at_the_next_start(monkeypatch, tmp_path, edition):
    org = _world(tmp_path, edition[2])
    org.mod_list.mods["Lux"].archive = "Lux-1.0.zip"
    first = _run(monkeypatch, tmp_path, edition, org, "history_update_first").start()
    first.plugin._watch_install = lambda _mod: None
    org.mod_list.mods["Lux"].version_text, org.mod_list.mods["Lux"].archive = "2.0", "Lux-2.0.zip"
    first.plugin._on_mod_installed(org.mod_list.mods["Lux"])         # MO2 announces the reinstall (either edition)
    assert first.plugin._history_versions["Lux"] == ("2.0", "Lux-2.0.zip")
    [updated] = [e for e in first.entries() if e["kind"] == history.MOD_UPDATED]
    assert updated["items"] == [{"name": "Lux", "before": "1.0", "after": "2.0"}]
    assert updated["offline"] is False
    first.stop()
    second = _Run(monkeypatch, tmp_path, edition, org, "history_update_second")
    org.refresh_callbacks.clear()
    second.start()
    assert [e for e in second.entries() if e["offline"]] == []         # nothing left to find


def test_a_mod_renamed_while_mo2_runs_is_one_rename_and_the_next_start_adds_nothing(monkeypatch, tmp_path, edition):
    org = _world(tmp_path, edition[2])
    org.mod_list.mods["Lux"].archive = "Lux-1.0.zip"
    run = _run(monkeypatch, tmp_path, edition, org).start()
    mod = org.mod_list.mods.pop("Lux")
    mod._name = "Lux Renamed"
    org.mod_list.mods["Lux Renamed"] = mod
    org.mod_list.order[org.mod_list.order.index("Lux")] = "Lux Renamed"
    org.mod_list.enabled.discard("Lux")
    org.mod_list.enabled.add("Lux Renamed")
    org.plugin_list.origins["Lux.esp"] = "Lux Renamed"
    run.look()
    changes = [(e["kind"], tuple(i["name"] for i in e["items"])) for e in run.entries()]
    assert changes == [(history.MOD_RENAMED, ("Lux Renamed",))]


def test_a_program_mo2_starts_is_recorded_with_its_end_and_never_with_its_arguments(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()
    org = run.org
    exe = r"C:\Example MO2\Stock Game\SkyrimSE.exe"
    # The newer MO2 passes the program, its folder and its arguments; the older one passes the program only.
    assert org.about_callbacks[0](exe, types.SimpleNamespace(), r"-nolauncher --token=secret C:\Users\someone\save.ess") is True
    assert org.about_callbacks[0](r"C:\Tools\LOOT\LOOT.exe") is True
    [skyrim, loot] = run.entries()
    assert (skyrim["kind"], skyrim["subject"], skyrim["detail"]) == (history.PROGRAM_RUN, "Skyrim Special Edition", {"path": exe})
    assert "secret" not in str(run.entries()) and "someone" not in str(run.entries()) and loot["subject"] == "LOOT"
    org.finished_callbacks[0](exe, 0)
    org.finished_callbacks[0](r"C:\Tools\LOOT\LOOT.exe", 1)
    skyrim, loot = run.entries()
    assert (skyrim["exit_code"], loot["exit_code"]) == (0, 1) and skyrim["ended_at"] and loot["ended_at"]
    assert skyrim["profile"] == "Default" and run.notified


def test_a_failure_while_noting_a_program_never_stops_it_from_running(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()

    def broken(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    run.plugin._history.run_started = broken
    run.plugin._history.run_finished = broken
    assert run.org.about_callbacks[0](r"C:\Game\Game.exe") is True
    assert run.org.finished_callbacks[0](r"C:\Game\Game.exe", 0) is None
    run.plugin._history.look = broken
    run.org.mod_list.enabled.discard("Lux")
    run.look()                                  # a failing history never breaks the look at the list


def test_a_stopped_plugin_notes_nothing(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()
    callback = run.org.about_callbacks[0]
    run.stop()
    assert callback(r"C:\Game\Game.exe") is True
    assert run.entries() == []


def test_the_history_never_changes_what_the_window_and_the_missed_patch_checks_read(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()
    before = dict(run.plugin._load_order)
    assert "mod_order" not in before and "plugins" not in before and "versions" not in before
    run.org.mod_list.order = ["Cathedral", "Lux", "Skyrim Base"]
    run.org.plugin_list.order = ["Cathedral.esp", "Skyrim.esm", "Lux.esp"]
    run.notified.clear()
    run.look()
    assert run.plugin._load_order == before            # moving things is not a change the missed-patch checks care about
    assert history.MOD_MOVED in [kind for kind, *_ in run.kinds()]


def test_a_history_that_cannot_be_read_never_costs_the_missed_patch_checks_their_snapshot(monkeypatch, tmp_path, edition):
    run = _run(monkeypatch, tmp_path, edition).start()

    def broken(*_args, **_kwargs):
        raise RuntimeError("a call this MO2 does not have")

    run.plugin._history_snapshot = broken
    run.org.plugin_list.active.discard("Lux.esp")
    run.look()
    assert "Lux.esp" not in run.plugin._load_order["active_plugins"] and "Lux.esp" in run.plugin._load_order["unchecked_plugins"]
    assert run.entries() == []
