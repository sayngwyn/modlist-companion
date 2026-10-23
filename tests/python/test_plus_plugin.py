"""The FOMOD Plus edition of Modlist Companion: reading what FOMOD Plus saved, rebuilding the install off the UI
thread, the scan of mods installed before, the edition's problems in MO2, and the full edition's side of the
conflicts. The plugin is driven with fake mobase and Qt, as in test_plugin_contract.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from fomod_logger_support.core.storage import EventStore
from fomod_logger_support import executor as plus_executor
from fomod_logger_support.plus import record as plus_record
from fomod_logger_support.plus import jobs as plus_jobs
from fomod_logger_support.executor import make_qt_executor
from fomod_logger_support.scan import ScanSummary, Scanner
from test_viewer import qt  # noqa: F401 - the module fixture that borrows MO2's PyQt6
from fomod_logger_support.plus.state import RecordState
from test_plugin_contract import _FakeOrganizer, _FakeWindow, _load_plugin_module

ROOT = Path(__file__).resolve().parents[2]
INSTANCE = "b" * 64


def _live_fixtures():
    spec = importlib.util.spec_from_file_location("live_fixtures", ROOT / "tests" / "live" / "fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------- what FOMOD Plus saves ----------

SKIP_VILLAGE = {"steps": [
    {"name": "Grass", "groups": [{"name": "Length", "plugins": ["Tall"], "deselected": []}]},
    {"name": "Tall options", "groups": [{"name": "Wind", "plugins": ["Windy"], "deselected": []}]},
    {"name": "Compatibility", "groups": [{"name": "Compatibility patches", "plugins": [],
                                          "deselected": ["Village patch"]}]}]}
WITH_VILLAGE = {"steps": [
    {"name": "Grass", "groups": [{"name": "Length", "plugins": ["Short"], "deselected": []}]},
    {"name": "Compatibility", "groups": [{"name": "Compatibility patches", "plugins": ["Village patch"],
                                          "deselected": []}]}]}


def _text(record: dict) -> str:
    return json.dumps(record)


class _Mod:
    """The part of IModInterface the edition uses."""

    def __init__(self, name: str, record: dict | str | None = None, archive: str = "", version: str = "2.1.0",
                 nexus: int = 12345, folder: Path | None = None):
        self._name, self.record, self.archive, self.version_text, self.nexus = name, record, archive, version, nexus
        self.folder = folder

    def name(self):
        return self._name

    def version(self):
        return types.SimpleNamespace(canonicalString=lambda: self.version_text)

    def nexusId(self):
        return self.nexus

    def installationFile(self):
        return self.archive

    def absolutePath(self):
        return str(self.folder)

    def pluginSetting(self, plugin, key, default=None):
        if plugin == "FOMOD Plus" and key == "fomod" and self.record is not None:
            return self.record if isinstance(self.record, str) else json.dumps(self.record)
        return default


class _Mods:
    def __init__(self):
        self.mods: dict[str, _Mod] = {}
        self.installed_callbacks, self.removed_callbacks = [], []

    def add(self, mod: _Mod) -> _Mod:
        self.mods[mod.name()] = mod
        return mod

    def allMods(self):
        return list(self.mods)

    def getMod(self, name):
        return self.mods.get(name)

    def onModInstalled(self, callback):
        self.installed_callbacks.append(callback)
        return True

    def onModRemoved(self, callback):
        self.removed_callbacks.append(callback)
        return True


class _Org(_FakeOrganizer):
    def __init__(self, tmp_path: Path, enabled=("FOMOD Plus",)):
        super().__init__(tmp_path)
        self.mod_list = _Mods()
        self.enabled_plugins = set(enabled)
        self.downloads = tmp_path / "downloads"
        self.downloads.mkdir(exist_ok=True)
        self.plugins_listed: dict[str, str] = {}

    def downloadsPath(self):
        return str(self.downloads)

    def pluginList(self):
        listed = self.plugins_listed
        return types.SimpleNamespace(pluginNames=lambda: list(listed), state=lambda name: listed[name])


def _plus(tmp_path, monkeypatch, name: str, *, enabled=("FOMOD Plus",), plain: bool = False):
    """The Plus edition wired to fakes: a manual executor (nothing is rebuilt until the test says so), a data folder
    and a fixed instance. ``plugin.looks`` counts how often the load order was read. Plain records of installs
    FOMOD Plus did not handle are left off unless ``plain`` (the fake timers run at once, before FOMOD Plus's
    record could be seen)."""

    module, _timer, _application = _load_plugin_module(monkeypatch, name, "fomod_logger_plus_plugin.py")
    monkeypatch.setattr(module._base, "stable_instance_id", lambda _path: INSTANCE)
    monkeypatch.setattr(module, "stable_instance_id", lambda _path: INSTANCE)
    organizer = _Org(tmp_path, enabled)
    organizer.pluginSetting = lambda plugin, key: key == "alert_new_patches"
    plugin = module.FomodLoggerPlusPlugin()
    plugin._organizer = organizer
    plugin._data_root = tmp_path / "fomod_logger_plus"
    plugin._data_root.mkdir()
    manual = plus_executor.ManualExecutor()
    plugin._new_executor = lambda: manual
    plugin._active = plugin._ready = True
    plugin._edition_start()
    if not plain:
        plugin._watch_install = lambda _mod: None
    plugin.manual = manual
    plugin.looks = []
    plugin._capture_load_order = lambda: plugin.looks.append(1)
    plugin.window = _FakeWindow()
    plugin._window = plugin.window
    return module, plugin, organizer


def _events(plugin, name: str | None = None) -> list[dict]:
    with EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        found = store.history("", include_hidden=True)
    return [e for e in found if name is None or e["mod"]["name"] == name]


def _grass_zip(tmp_path: Path) -> Path:
    folder = tmp_path / "downloads"
    folder.mkdir(exist_ok=True)
    return _live_fixtures().grass(folder)


def _options(event: dict) -> dict[str, bool]:
    return {o["name"]: o["selected"] for s in event["steps"] for g in s["groups"] for o in g["options"]}


# ---------- identity ----------

def test_the_edition_has_its_own_names_and_data_folder_and_no_installer(monkeypatch):
    module, _t, _a = _load_plugin_module(monkeypatch, "plus_identity", "fomod_logger_plus_plugin.py")
    plugin = module.createPlugin()
    assert plugin.name() == "FOMOD Logger Plus"
    assert plugin.displayName() == "Modlist Companion (for FOMOD Plus)"
    assert plugin.DATA_FOLDER == "fomod_logger_plus"
    full, _t, _a = _load_plugin_module(monkeypatch, "full_identity")
    assert full.createPlugin().name() == "FOMOD Logger" and full.FomodLoggerPlugin.DATA_FOLDER == "fomod_logger"
    source = (ROOT / "python" / "fomod_logger_plus_plugin.py").read_text(encoding="utf-8")
    assert "Native Installer" not in source and "IPluginInstaller" not in source


def test_the_edition_keeps_its_data_beside_not_inside_the_full_editions(tmp_path, monkeypatch):
    module, _t, _a = _load_plugin_module(monkeypatch, "plus_data_folder", "fomod_logger_plus_plugin.py")
    plugin = module.FomodLoggerPlusPlugin()
    organizer = _Org(tmp_path)
    plugin._new_executor = lambda: plus_executor.ManualExecutor()
    assert plugin.init(organizer) is True
    assert plugin._data_root == tmp_path / "fomod_logger_plus" and plugin._data_root.is_dir()
    assert not (tmp_path / "fomod_logger").exists()
    # The window is told what is different here.
    assert plugin._window_options() == {"can_import": False, "scanner": plugin._scanner, "edition_label": "for FOMOD Plus"}


# ---------- reading the record ----------

def test_the_record_is_read_through_mo2s_plugin_setting():
    mod = _Mod("Grass", SKIP_VILLAGE)
    assert json.loads(plus_record.read_record(mod)) == SKIP_VILLAGE
    assert plus_record.read_record(_Mod("Plain")) is None                       # MO2 gives the default back
    assert plus_record.read_record(_Mod("Blank", "  ")) is None

    class TwoArguments:                                                          # a binding without the default
        def pluginSetting(self, plugin, key, *default):
            if default:
                raise TypeError("takes 2 arguments")
            return _text(SKIP_VILLAGE)

    assert plus_record.read_record(TwoArguments()) == _text(SKIP_VILLAGE)


def test_a_record_must_have_steps_of_groups_of_names():
    for bad in ("not json", "[]", '{"steps": 3}', '{"steps": [3]}', '{"steps": [{"groups": [3]}]}',
                '{"steps": [{"groups": [{"plugins": "x"}]}]}', '{"steps": [{"groups": [{"deselected": [1]}]}]}'):
        with pytest.raises(plus_record.RecordError):
            plus_record.parse_record(bad)
    assert plus_record.parse_record('{"steps": []}') == {"steps": []}
    assert plus_record.option_counts(SKIP_VILLAGE) == (2, 1)


def test_the_digest_follows_the_choices_not_the_spacing():
    spaced = json.dumps(SKIP_VILLAGE, indent=4)
    assert plus_record.digest(spaced) == plus_record.digest(json.dumps(SKIP_VILLAGE, separators=(",", ":")))
    assert plus_record.digest(spaced) != plus_record.digest(_text(WITH_VILLAGE))


def test_the_state_file_is_atomic_forgiving_and_forgets(tmp_path):
    path = tmp_path / "state" / "plus_records.json"
    state = RecordState(path)
    assert state.get(INSTANCE, "grass") is None
    state.set(INSTANCE, "grass", digest="d", archive="a.zip", event_id="e")
    assert RecordState(path).get(INSTANCE, "grass") == {"digest": "d", "archive": "a.zip", "event_id": "e"}
    state.forget(INSTANCE, "grass")
    assert RecordState(path).get(INSTANCE, "grass") is None
    path.write_text("{ not json", encoding="utf-8")
    assert RecordState(path).get(INSTANCE, "grass") is None                      # a damaged file is an empty one
    path.write_text(json.dumps({"version": 99, "records": {"x": {}}}), encoding="utf-8")
    assert RecordState(path)._data == {}


# ---------- the executors ----------

def test_the_manual_executor_holds_work_until_told_and_reports_errors():
    manual = plus_executor.ManualExecutor()
    got = []
    handle = manual.run(lambda post, cancelled: post("hello") or 7, got.append, lambda value, error: got.append((value, error)))
    assert got == [] and handle.finished is False
    assert manual.run_pending() == 1
    assert got == ["hello", (7, None)] and handle.finished is True

    def broken(_post, _cancelled):
        raise RuntimeError("boom")

    manual.run(broken, got.append, lambda value, error: got.append((value, str(error))))
    manual.run_pending()
    assert got[-1] == (None, "boom")


def test_the_inline_executor_runs_work_at_once():
    seen = []
    plus_executor.InlineExecutor().run(lambda post, cancelled: 3, seen.append, lambda value, error: seen.append((value, error)))
    assert seen == [(3, None)]


# ---------- rebuilding one record ----------

def _job(tmp_path, record=SKIP_VILLAGE, archive="", kind="install", **kwargs) -> plus_jobs.RebuildJob:
    text = _text(record) if isinstance(record, dict) else record
    values = dict(instance_id=INSTANCE, instance_label="MO2 Test", mod_name="LiveTest Grass", mod_version="2.1.0",
                  nexus_id=77, archive_path=str(archive) if archive else "", record=text,
                  digest=plus_record.digest(text), when="2026-10-21T12:00:00.000Z", kind=kind)
    values.update(kwargs)
    return plus_jobs.RebuildJob(**values)


def test_a_record_and_its_archive_make_the_full_event(tmp_path):
    archive = _grass_zip(tmp_path)
    result = plus_jobs.run_job(_job(tmp_path, archive=archive, plugin_states={"LT_Village.esp": "Active"}))
    assert result.mode == plus_jobs.REBUILT and result.reason == ""
    event = result.event
    assert event["source"]["reconstructed"] is True and event["source"]["archive"] == archive.name
    assert event["mod"] == {"name": "LiveTest Grass", "version": "2.1.0", "nexus_id": 77}
    assert event["completed_at"] == "2026-10-21T12:00:00.000Z"
    assert _options(event)["Tall"] is True and _options(event)["Village patch"] is False
    patch = next(o for s in event["steps"] for g in s["groups"] for o in g["options"] if o["name"] == "Village patch")
    assert patch["plugins"] == ["LT_Grass_Village.esp"] and patch["masters"] == ["LT_Village.esp"]   # what makes a missed patch
    assert any("plugins MO2 lists now" in note for note in event["source"]["reconstruction_notes"])


def test_a_gone_or_unreadable_archive_leaves_the_names(tmp_path):
    gone = plus_jobs.run_job(_job(tmp_path, archive=tmp_path / "downloads" / "missing.zip"))
    assert gone.mode == plus_jobs.NAMES_ONLY and "no longer there" in gone.reason
    assert _options(gone.event) == {"Tall": True, "Windy": True, "Village patch": False}
    assert gone.event["installer"]["name"] == plus_jobs.NAMES_ONLY_INSTALLER
    assert gone.event["source"]["reconstructed"] is True
    assert "only the option names" in " ".join(gone.event["source"]["reconstruction_notes"])
    unrecorded = plus_jobs.run_job(_job(tmp_path))
    assert unrecorded.mode == plus_jobs.NAMES_ONLY and "not recorded" in unrecorded.reason
    broken = tmp_path / "downloads" / "broken.zip"
    broken.parent.mkdir(exist_ok=True)
    broken.write_bytes(b"this is not an archive at all" * 20)
    bad = plus_jobs.run_job(_job(tmp_path, archive=broken))
    assert bad.mode == plus_jobs.NAMES_ONLY and "broken.zip" in bad.reason
    nofomod = tmp_path / "downloads" / "plain.zip"
    import zipfile
    with zipfile.ZipFile(nofomod, "w") as handle:
        handle.writestr("readme.txt", "no installer here")
    plain = plus_jobs.run_job(_job(tmp_path, archive=nofomod))
    assert plain.mode == plus_jobs.NAMES_ONLY and "plain.zip" in plain.reason


def test_a_record_that_cannot_be_read_is_a_failure_not_an_event(tmp_path):
    result = plus_jobs.run_job(_job(tmp_path, record='{"steps": 3}'))
    assert result.mode == plus_jobs.FAILED and result.event is None and "cannot be read" in result.reason


def test_event_ids_follow_the_archive_the_choices_and_the_install(tmp_path):
    job = _job(tmp_path)
    again = _job(tmp_path)
    assert plus_jobs.event_id_for(job, "a.zip") == plus_jobs.event_id_for(again, "A.ZIP")
    assert plus_jobs.event_id_for(job, "a.zip") != plus_jobs.event_id_for(job, "b.zip")
    later = _job(tmp_path, when="2026-10-22T12:00:00.000Z")
    assert plus_jobs.event_id_for(job, "a.zip") != plus_jobs.event_id_for(later, "a.zip")           # a new install
    scan_a, scan_b = (_job(tmp_path, kind="scan", when=w) for w in ("2026-01-01T00:00:00.000Z", "2026-02-01T00:00:00.000Z"))
    assert plus_jobs.event_id_for(scan_a, "a.zip") == plus_jobs.event_id_for(scan_b, "a.zip")       # a scan twice is one


# ---------- installs ----------

def _install(plugin, organizer, mod: _Mod):
    organizer.mod_list.add(mod)
    plugin._on_mod_installed(mod)


def test_an_install_is_rebuilt_off_the_ui_thread_then_stored_and_the_window_told(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_install")
    archive = _grass_zip(tmp_path)
    mod = _Mod("LiveTest Grass", SKIP_VILLAGE, archive=str(archive))
    _install(plugin, organizer, mod)
    assert _events(plugin) == []                                    # queued, not done on the UI thread
    assert len(plugin.manual.pending) == 1 and plugin._pending == {"livetest grass": 1}
    assert plugin._check_held("livetest grass") is True
    looks = len(plugin.looks)
    plugin.manual.run_pending()
    [event] = _events(plugin)
    assert event["mod"]["name"] == "LiveTest Grass" and event["source"]["reconstructed"] is True
    assert _options(event)["Village patch"] is False
    assert plugin._pending == {} and plugin._check_held("livetest grass") is False
    assert plugin.window.refreshes >= 1 and len(plugin.looks) > looks      # the window and the load order follow
    assert plugin._records.get(INSTANCE, "livetest grass")["digest"] == plus_record.digest(_text(SKIP_VILLAGE))
    assert plugin.activeProblems() == []


def test_the_work_runs_on_a_worker_thread_and_only_reads_the_job(tmp_path, monkeypatch):
    seen = {}
    real = plus_jobs.run_job

    def spy(job, cancelled=None):
        seen["thread"] = threading.get_ident()
        return real(job, cancelled)

    module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_thread")
    monkeypatch.setattr(module, "run_job", spy)

    class Threaded:
        """Runs each piece of work on a thread of its own and hands the end back when asked."""

        def __init__(self):
            self.ends = []

        def run(self, work, on_message, on_finished):
            handle = plus_executor.Handle()

            def target():
                value, error = plus_executor._run(work, handle, on_message)
                self.ends.append((on_finished, value, error))

            thread = threading.Thread(target=target)
            thread.start()
            thread.join()
            return handle

        def close(self):
            return

    threaded = Threaded()
    plugin._executor = threaded
    _install(plugin, organizer, _Mod("LiveTest Grass", SKIP_VILLAGE, archive=str(_grass_zip(tmp_path))))
    assert seen["thread"] != threading.get_ident()
    assert _events(plugin) == []                                    # nothing stored from the worker
    for finished, value, error in threaded.ends:
        finished(value, error)                                       # the "UI thread" collects it
    assert len(_events(plugin)) == 1


def test_without_the_archive_the_install_is_stored_with_the_names(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_names")
    _install(plugin, organizer, _Mod("LiveTest Grass", SKIP_VILLAGE, archive=str(tmp_path / "downloads" / "gone.zip")))
    plugin.manual.run_pending()
    [event] = _events(plugin)
    assert event["installer"]["name"] == plus_jobs.NAMES_ONLY_INSTALLER
    assert _options(event) == {"Tall": True, "Windy": True, "Village patch": False}
    assert plugin.activeProblems() == []                            # no archive is not a failure


def test_a_relative_archive_name_is_looked_for_in_the_downloads_folder(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_downloads")
    archive = _grass_zip(tmp_path)
    assert plugin._archive_path(archive.name) == str(archive)
    assert plugin._archive_path(r"C:\old\place\\" + archive.name) == str(archive)    # moved into the downloads folder
    assert plugin._archive_path("") == ""
    assert plugin._archive_path(r"C:\old\place\gone.zip") == r"C:\old\place\gone.zip"


def test_a_mod_without_a_record_makes_no_job_and_a_late_record_is_found(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_late")
    plain = _Mod("Plain Mod")
    _install(plugin, organizer, plain)
    assert plugin.manual.pending == [] and plugin._pending == {}    # nothing to rebuild (a plain record is another matter)
    late = _Mod("LiveTest Grass")                                   # FOMOD Plus saves its choices a moment after MO2 announces
    organizer.mod_list.add(late)
    started = plugin._lifecycle_generation
    late.record = SKIP_VILLAGE
    plugin._look_again("LiveTest Grass", started)
    assert len(plugin.manual.pending) == 1
    plugin._look_again("LiveTest Grass", started)                   # already being rebuilt: not twice
    assert len(plugin.manual.pending) == 1
    plugin._look_again("LiveTest Grass", started + 1)               # a plugin that was switched off and on since
    assert len(plugin.manual.pending) == 1


def test_a_reinstall_with_changed_picks_is_a_new_install_and_the_same_picks_are_not(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_reinstall")
    archive = str(_grass_zip(tmp_path))
    mod = _Mod("LiveTest Grass", SKIP_VILLAGE, archive=archive)
    _install(plugin, organizer, mod)
    plugin.manual.run_pending()
    first = _events(plugin)[0]
    assert first["source"]["reinstallation"] is False
    time.sleep(0.01)
    _install(plugin, organizer, mod)                                # the same choices of the same archive
    assert plugin.manual.pending == [] and len(_events(plugin)) == 1
    mod.record = WITH_VILLAGE                                       # reinstalled with the Village patch picked
    time.sleep(0.01)
    _install(plugin, organizer, mod)
    plugin.manual.run_pending()
    events = _events(plugin)
    assert len(events) == 2
    newest = events[0]                                              # newest first
    assert newest["event_id"] != first["event_id"] and newest["source"]["reinstallation"] is True
    assert _options(newest)["Village patch"] is True and _options(first)["Village patch"] is False
    with EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        assert store.newest_for_mod(INSTANCE, "LiveTest Grass")["event_id"] == newest["event_id"]
    # Back to the first picks: a new install again, the latest, whatever an earlier one looked like.
    mod.record = SKIP_VILLAGE
    time.sleep(0.01)
    _install(plugin, organizer, mod)
    plugin.manual.run_pending()
    with EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        latest = store.newest_for_mod(INSTANCE, "LiveTest Grass")
    assert len(_events(plugin)) == 3 and _options(latest)["Village patch"] is False
    assert latest["event_id"] not in {first["event_id"], newest["event_id"]}


def test_a_new_archive_with_the_same_picks_is_a_new_install(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_new_archive")
    old = _grass_zip(tmp_path)
    mod = _Mod("LiveTest Grass", SKIP_VILLAGE, archive=str(old))
    _install(plugin, organizer, mod)
    plugin.manual.run_pending()
    newer = old.with_name("LiveTest-Grass-2.2.0.zip")
    newer.write_bytes(old.read_bytes())
    mod.archive, mod.version_text = str(newer), "2.2.0"
    time.sleep(0.01)
    _install(plugin, organizer, mod)
    plugin.manual.run_pending()
    assert [e["source"]["archive"] for e in _events(plugin)] == [newer.name, old.name]


def test_an_unreadable_record_is_a_problem_until_a_good_one_comes(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_bad_record")
    mod = _Mod("LiveTest Grass", '{"steps": 3}')
    _install(plugin, organizer, mod)
    assert plugin.manual.pending == [] and _events(plugin) == []     # unusable, so it is a problem (a plain record is another matter)
    assert plugin.activeProblems() == [12]
    assert "LiveTest Grass" in plugin.fullDescription(12) and plugin.shortDescription(12)
    mod.record = SKIP_VILLAGE
    _install(plugin, organizer, mod)
    plugin.manual.run_pending()
    assert plugin.activeProblems() == [] and len(_events(plugin)) == 1


def test_a_job_that_raises_is_a_state_problem_and_the_plugin_goes_on(tmp_path, monkeypatch, capsys):
    module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_job_error")

    def explode(_job, _cancelled=None):
        raise RuntimeError("the worker fell over")

    monkeypatch.setattr(module, "run_job", explode)
    _install(plugin, organizer, _Mod("LiveTest Grass", SKIP_VILLAGE))
    plugin.manual.run_pending()
    assert plugin.activeProblems() == [6] and "the worker fell over" in plugin.fullDescription(6)
    assert plugin._pending == {}
    assert "the worker fell over" in capsys.readouterr().err


def test_a_result_that_arrives_after_the_plugin_was_switched_off_is_dropped(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_late_result")
    _install(plugin, organizer, _Mod("LiveTest Grass", SKIP_VILLAGE, archive=str(_grass_zip(tmp_path))))
    plugin._active = False
    plugin._lifecycle_generation += 1
    plugin.manual.run_pending()
    assert _events(plugin) == [] and plugin._pending == {}


def test_removing_a_mod_forgets_what_was_remembered_about_its_record(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_removed")
    plugin._alerts_changed = lambda: None
    _install(plugin, organizer, _Mod("LiveTest Grass", SKIP_VILLAGE, archive=str(_grass_zip(tmp_path))))
    plugin.manual.run_pending()
    assert plugin._records.get(INSTANCE, "livetest grass") is not None
    plugin._on_mod_removed("LiveTest Grass")
    assert plugin._records.get(INSTANCE, "livetest grass") is None
    assert _events(plugin)[0]["removed_at"] is not None


def test_the_look_for_patches_waits_for_the_install_to_be_stored(tmp_path, monkeypatch):
    """The new mod's own install is what the alert is stored against, so the look waits for it."""
    from test_patch_alerts import grass_patches, order

    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_hold")
    with EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        event = grass_patches()
        event["instance"]["id"] = INSTANCE
        store.ingest(event)
    plugin._known_mods = {"grass patches": "Grass Patches"}
    folder = tmp_path / "mods" / "Village"
    folder.mkdir(parents=True)
    (folder / "Village.esp").write_bytes(b"TES4")
    organizer.mod_list.add(_Mod("Village", SKIP_VILLAGE, folder=folder, archive=str(tmp_path / "downloads" / "v.zip")))
    snapshot = dict(order("Village.esp", mods=("Village",)), instance_id=INSTANCE)
    plugin._queue_check("Village", None)
    plugin._pending["village"] = 1                                  # its install is being rebuilt
    plugin._alert_work(None, snapshot, {"village": {"village.esp"}})
    assert plugin.activeProblems() == [] and "village" in plugin._install_checks
    plugin._pending.clear()
    plugin._alert_work(None, snapshot, {"village": {"village.esp"}})
    assert plugin.activeProblems() == [100] and plugin._install_checks == {}


# ---------- the scan ----------

def _scan_world(tmp_path, monkeypatch, name: str):
    module, plugin, organizer = _plus(tmp_path, monkeypatch, name)
    archive = _grass_zip(tmp_path)
    for mod_name, record in (("LiveTest Grass", SKIP_VILLAGE), ("Other Grass", WITH_VILLAGE)):
        folder = tmp_path / "mods" / mod_name
        folder.mkdir(parents=True, exist_ok=True)
        organizer.mod_list.add(_Mod(mod_name, record, archive=str(archive), folder=folder))
    folder = tmp_path / "mods" / "Plain"
    folder.mkdir(parents=True)
    organizer.mod_list.add(_Mod("Plain", None, folder=folder))
    return module, plugin, organizer, archive


def test_the_scan_lists_mods_with_a_record_and_no_install(tmp_path, monkeypatch):
    _module, plugin, organizer, _archive = _scan_world(tmp_path, monkeypatch, "plus_scan_list")
    # The mods FOMOD Plus saved choices for are rebuilt; the one with no record at all is recorded as a plain install.
    assert [job.mod_name for job in plugin._scan_jobs()] == ["LiveTest Grass", "Other Grass", "Plain"]
    assert plugin._scanner.count() == 3
    job = next(job for job in plugin._scan_jobs() if job.mod_name == "LiveTest Grass")
    assert job.kind == "scan" and job.reinstallation is False and job.nexus_id == 12345
    assert next(job for job in plugin._scan_jobs() if job.mod_name == "Plain").kind == "scan"
    organizer.mod_list.add(_Mod("Broken", "not json", folder=tmp_path))
    assert plugin._scanner.count() == 4                             # listed, so that the scan can say it failed
    plugin._active = False
    assert plugin._scan_jobs() == []


def test_the_scan_rebuilds_in_the_background_with_progress_and_ends_with_a_summary(tmp_path, monkeypatch):
    _module, plugin, organizer, _archive = _scan_world(tmp_path, monkeypatch, "plus_scan_run")
    progress, done = [], []
    plugin._scanner.start(lambda *args: progress.append(args), done.append)
    assert progress == [] and _events(plugin) == [] and plugin._scanner.running is True       # held, not yet run
    plugin.manual.run_pending()
    assert plugin._scanner.running is False
    [summary] = done
    assert (summary.total, summary.rebuilt, summary.names_only, summary.failed, summary.plain) == (3, 2, 0, 0, 1)
    assert summary.cancelled is False and "3 of 3 mods: 1 recorded as installed without an installer, 2 rebuilt" in summary.message()
    assert [p[:2] for p in progress] == [(0, 3), (1, 3), (1, 3), (2, 3), (2, 3), (3, 3)]
    assert sorted({p[2] for p in progress}) == ["LiveTest Grass", "Other Grass", "Plain"]
    assert {e["mod"]["name"] for e in _events(plugin)} == {"LiveTest Grass", "Other Grass", "Plain"}
    assert all(e["source"].get("reconstructed") for e in _events(plugin) if e["mod"]["name"] != "Plain")
    assert [e["kind"] for e in _events(plugin) if e["mod"]["name"] == "Plain"] == ["plain"]
    assert "installed before Modlist Companion saw it" in _events(plugin, "LiveTest Grass")[0]["source"]["reconstruction_notes"][0]
    assert plugin.window.refreshes >= 2
    assert plugin._scanner.count() == 0                             # now every one has an install
    again = []
    assert plugin._scanner.start(lambda *a: None, again.append).total == 0 and again[0].message().startswith("Every mod")


def test_the_scan_can_be_cancelled_between_mods(tmp_path, monkeypatch):
    _module, plugin, _organizer, _archive = _scan_world(tmp_path, monkeypatch, "plus_scan_cancel")
    done, progress = [], []

    def on_progress(finished, total, name):
        progress.append((finished, total, name))
        if finished == 1:
            plugin._scanner.cancel()

    plugin._scanner.start(on_progress, done.append)
    plugin.manual.run_pending()
    [summary] = done
    assert summary.cancelled is True and summary.done == 1 and summary.total == 3
    assert "Stopped after 1 of 3 mods" in summary.message()
    assert len(_events(plugin)) == 1 and plugin._scanner.count() == 2                         # the rest can be scanned later


def test_a_scan_never_overwrites_an_install_made_in_the_meantime(tmp_path, monkeypatch):
    _module, plugin, organizer, archive = _scan_world(tmp_path, monkeypatch, "plus_scan_race")
    done = []
    plugin._scanner.start(lambda *a: None, done.append)
    mod = organizer.mod_list.mods["LiveTest Grass"]
    _install(plugin, organizer, mod)                                # FOMOD Plus reinstalls it while the scan is queued
    plugin.manual.pending.reverse()                                 # and its rebuild finishes first
    plugin.manual.run_pending()
    assert len(_events(plugin, "LiveTest Grass")) == 1              # the install; the scan did not add its own
    assert done[0].skipped == 1 and done[0].rebuilt == 1 and done[0].plain == 1


def test_scan_counts_names_only_failures_and_a_store_that_breaks(tmp_path):
    jobs = [_job(tmp_path, mod_name="A", archive=tmp_path / "missing.zip", kind="scan"),
            _job(tmp_path, mod_name="B", record='{"steps": 3}', kind="scan"),
            _job(tmp_path, mod_name="C", archive=tmp_path / "missing.zip", kind="scan")]
    stored = []

    def store(result):
        if result.job.mod_name == "C":
            raise RuntimeError("database is locked")
        stored.append(result.job.mod_name)
        return True

    done = []
    scanner = Scanner(lambda: jobs, store, lambda: plus_executor.InlineExecutor())
    scanner.start(lambda *a: None, done.append)
    summary = done[0]
    assert (summary.names_only, summary.failed, summary.total) == (1, 2, 3)
    assert stored == ["A"] and any("database is locked" in p for p in summary.problems)
    assert "1 with option names only" in summary.message() and "2 could not be read" in summary.message()
    assert scanner.start(lambda *a: None, lambda _s: None) is not None
    assert ScanSummary(total=0).message().startswith("Every mod")


def test_a_second_scan_cannot_start_while_one_runs(tmp_path, monkeypatch):
    _module, plugin, _organizer, _archive = _scan_world(tmp_path, monkeypatch, "plus_scan_twice")
    assert plugin._scanner.start(lambda *a: None, lambda _s: None) is not None
    assert plugin._scanner.start(lambda *a: None, lambda _s: None) is None
    plugin.manual.run_pending()
    assert plugin._scanner.running is False


def test_switching_the_plugin_off_stops_a_scan_and_a_new_one_can_start_after_switching_on(tmp_path, monkeypatch):
    _module, plugin, _organizer, _archive = _scan_world(tmp_path, monkeypatch, "plus_scan_off")
    done = []
    plugin._scanner.start(lambda *a: None, done.append)
    assert plugin._scanner.running is True
    old = plugin.manual
    plugin._shutdown()
    assert plugin._executor is None and plugin._scanner.running is False and done == []
    assert old in plugin._retired                                       # kept alive for a worker still posting to it
    plugin._active = True                                               # switched on again
    plugin.manual = plus_executor.ManualExecutor()
    plugin._new_executor = lambda: plugin.manual
    plugin._edition_start()
    finished = []
    assert plugin._scanner.start(lambda *a: None, finished.append) is not None
    plugin.manual.run_pending()
    assert finished and finished[0].rebuilt == 2 and plugin._scanner.running is False


def test_a_cancelled_scan_stops_a_read_that_is_under_way(tmp_path):
    import threading
    cancelled = threading.Event()
    cancelled.set()
    archive = _grass_zip(tmp_path)
    assert plus_jobs.run_job(_job(tmp_path, archive=archive), cancelled).mode == plus_jobs.CANCELLED
    cancelled.clear()
    assert plus_jobs.run_job(_job(tmp_path, archive=archive), cancelled).mode == plus_jobs.REBUILT
    # The reader itself looks at the flag between entries.
    from fomod_logger_support.archive.sevenzip import ArchiveTimeoutError, Limits, open_archive
    cancelled.set()
    with open_archive(archive, limits=Limits(cancel=cancelled)) as handle:
        result = handle.read([handle.entries[0].path])
        assert isinstance(result.errors.get(handle.entries[0].path), ArchiveTimeoutError)


def test_a_stale_record_does_not_hide_the_new_choices_that_follow(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_stale")
    archive = str(_grass_zip(tmp_path))
    mod = _Mod("LiveTest Grass", SKIP_VILLAGE, archive=archive)
    _install(plugin, organizer, mod)
    plugin.manual.run_pending()
    assert len(_events(plugin)) == 1
    # A reinstall: MO2 announces it while the meta still holds the old record; FOMOD Plus's new one lands after.
    time.sleep(0.01)
    organizer.mod_list.add(mod)
    generation = plugin._lifecycle_generation
    plugin._on_mod_installed(mod)
    assert plugin.manual.pending == []                       # the same record: nothing to do yet
    mod.record = WITH_VILLAGE
    plugin._look_again("LiveTest Grass", generation)         # the timer the install callback set
    plugin.manual.run_pending()
    assert len(_events(plugin)) == 2


def test_a_callback_that_raises_never_escapes_the_qt_slot(qt, capsys):
    from PyQt6.QtCore import QCoreApplication

    executor = make_qt_executor()
    done = []

    def broken(_value, _error):
        raise RuntimeError("callback bug")

    executor.run(lambda post, cancelled: post("m") or 1, lambda m: 1 / 0, broken)
    executor.run(lambda post, cancelled: 2, lambda m: None, lambda value, error: done.append(value))
    end = time.time() + 5
    while not done and time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert done == [2]
    err = capsys.readouterr().err
    assert "callback bug" in err and "ZeroDivisionError" in err


def test_a_problem_check_from_another_thread_calls_no_mo2(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_thread_problems")
    calls = []
    organizer.isPluginEnabled = lambda name: calls.append(name) or True
    plugin.activeProblems()
    plugin.shortDescription(11)
    plugin.fullDescription(11)
    assert calls == []


# ---------- MO2's problem list ----------

def test_without_fomod_plus_there_is_a_problem_that_says_what_to_do(tmp_path, monkeypatch):
    _module, plugin, _organizer = _plus(tmp_path, monkeypatch, "plus_missing", enabled=())
    assert plugin.activeProblems() == [11]
    assert plugin.shortDescription(11) == ("Modlist Companion (for FOMOD Plus) needs FOMOD Plus; install it or use "
                                           "the standard edition")
    assert "needs FOMOD Plus" in plugin.fullDescription(11) and plugin.hasGuidedFix(11) is False
    assert plugin.activeProblems() == [11]                          # MO2's problem thread only reports what was read before
    _organizer.enabled_plugins.add("FOMOD Plus")
    assert plugin.activeProblems() == [11]
    plugin._on_any_plugin_switched(None)                            # MO2 raised plugin-enabled: the main thread looks again
    assert plugin.activeProblems() == []


def test_both_editions_enabled_is_a_problem_in_each(tmp_path, monkeypatch):
    _module, plugin, organizer = _plus(tmp_path, monkeypatch, "plus_both", enabled=("FOMOD Plus", "FOMOD Logger"))
    assert plugin.activeProblems() == [10]
    text = plugin.fullDescription(10)
    assert "Keep one" in text and "Modlist Companion (for FOMOD Plus)" not in text and "this one (for FOMOD Plus)" in text

    full_module, _t, _a = _load_plugin_module(monkeypatch, "full_both")
    full = full_module.FomodLoggerPlugin()
    full._organizer = _Org(tmp_path, enabled=("FOMOD Logger Native Installer", "FOMOD Logger Plus"))
    full._active = True
    full._look_at_other_plugins()
    assert full.activeProblems() == [10]
    assert "Keep one" in full.fullDescription(10) and "Modlist Companion (for FOMOD Plus)" in full.fullDescription(10)
    assert full.shortDescription(10) == "Two editions of Modlist Companion are enabled; keep one"


def test_the_full_edition_says_fomod_plus_takes_every_install(tmp_path, monkeypatch):
    full_module, _t, _a = _load_plugin_module(monkeypatch, "full_with_plus")
    full = full_module.FomodLoggerPlugin()
    organizer = _Org(tmp_path, enabled=("FOMOD Logger Native Installer",))
    full._organizer, full._active = organizer, True
    full._look_at_other_plugins()
    assert full.activeProblems() == []
    organizer.enabled_plugins.add("FOMOD Plus")
    full._look_at_other_plugins()
    assert full.activeProblems() == [9]
    assert full.shortDescription(9) == "FOMOD Plus is installed, so Modlist Companion records nothing new"
    text = full.fullDescription(9)
    assert "takes every FOMOD install" in text and "records nothing new" in text
    assert "Modlist Companion (for FOMOD Plus)" in text and full.hasGuidedFix(9) is False
    organizer.enabled_plugins.discard("FOMOD Plus")
    full._look_at_other_plugins()
    assert full.activeProblems() == []
    # The native installer's priority is never raised to win the install back.
    source = (ROOT / "python" / "fomod_logger_plugin.py").read_text(encoding="utf-8")
    assert "setPriority" not in source and "def priority" not in source


def test_the_editions_do_not_share_problem_state_or_data(tmp_path, monkeypatch):
    plus_module, plus, _o = _plus(tmp_path, monkeypatch, "plus_isolated")
    full_module, _t, _a = _load_plugin_module(monkeypatch, "full_isolated")
    full = full_module.FomodLoggerPlugin()
    assert plus.DATA_FOLDER != full.DATA_FOLDER and plus.name() != full.name()
    assert plus.settings() == [("open_window_on_start", "Open the Modlist Companion window when MO2 starts", False),
                               ("alert_new_patches",
                                "Tell me in MO2 when a mod I install is what a patch I skipped was made for", True)]
