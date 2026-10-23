from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_entry_direct_file_import_with_no_plugin_search_path(tmp_path):
    import subprocess
    code = '''
import runpy, sys
from pathlib import Path
import pytest
test_file = Path(sys.argv[1])
namespace = runpy.run_path(str(test_file))
plugin_dir = test_file.parents[2] / "python"
assert str(plugin_dir) not in sys.path
assert not any(name.startswith("fomod_logger_support") for name in sys.modules)
with pytest.MonkeyPatch.context() as patch:
    module, _, _ = namespace["_load_plugin_module"](patch, "direct_file_plugin")
    assert module.createPlugin().name() == "FOMOD Logger"
    assert str(plugin_dir) in sys.path
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(Path(__file__).resolve())],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


class _FakeWindow:
    def __init__(self) -> None:
        self.shown = 0
        self.refreshes = 0
        self.closed = False
        self.highlighted = None

    def show(self) -> None:
        self.shown += 1

    def raise_(self) -> None:
        return

    def activateWindow(self) -> None:
        return

    def schedule_refresh(self) -> None:
        self.refreshes += 1

    def highlight_alerts(self, patches) -> None:
        self.highlighted = list(patches)

    def close(self) -> None:
        self.closed = True

    def deleteLater(self) -> None:
        return


def test_window_opening_at_start_is_opt_in_and_generation_gated(monkeypatch):
    module, _, _ = _load_plugin_module(monkeypatch, "window_setting_test")
    plugin = module.FomodLoggerPlugin()
    assert plugin.settings() == [
        ("open_window_on_start", "Open the Modlist Companion window when MO2 starts", False),
        ("alert_new_patches", "Tell me in MO2 when a mod I install is what a patch I skipped was made for", True),
    ]
    opened = []
    plugin.display = lambda: opened.append(1)
    plugin._active = True
    plugin._lifecycle_generation = 2
    plugin._open_if_active(1)
    assert opened == []
    plugin._open_if_active(2)
    assert opened == [1]
    plugin._active = False
    plugin._open_if_active(2)
    assert opened == [1]


@pytest.mark.parametrize("enabled", [False, True])
def test_tools_menu_opens_one_window_that_follows_changes_and_closes_on_shutdown(
    tmp_path, monkeypatch, enabled
):
    module, _, _ = _load_plugin_module(monkeypatch, "window_runtime_test")
    windows = []
    plugin = module.FomodLoggerPlugin()
    plugin._create_window = lambda: windows.append(_FakeWindow()) or windows[-1]
    plugin._organizer = _FakeOrganizer(tmp_path)
    plugin._organizer.pluginSetting = lambda name, key: enabled
    plugin._data_root = tmp_path
    plugin._refresh_provider_health = lambda: None
    plugin._migrate_once = lambda: None
    plugin._ingest = lambda: None
    plugin._start_runtime(initializing=True)
    assert len(windows) == (1 if enabled else 0)  # opened at start only when asked
    plugin.display()
    plugin.display()
    assert len(windows) == 1 and windows[0].shown == (3 if enabled else 2)
    plugin._notify_viewer()
    assert windows[0].refreshes == 1
    plugin._shutdown()
    assert windows[0].closed is True
    plugin.display()  # inactive: nothing opens
    assert len(windows) == 1


def _forget_shared_plugin_code(monkeypatch) -> None:
    """The part both editions share is imported once, with whatever Qt and mobase were around then: each test
    wants its own copy, bound to its own fakes (the package keeps an attribute for it too, so drop that as well)."""

    monkeypatch.delitem(sys.modules, "fomod_logger_support.plugin_base", raising=False)
    package = sys.modules.get("fomod_logger_support")
    if package is not None and hasattr(package, "plugin_base"):
        monkeypatch.delattr(package, "plugin_base")


def _load_plugin_module(monkeypatch, module_name: str, entry: str = "fomod_logger_plugin.py"):
    class Signal:
        def __init__(self) -> None:
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self) -> None:
            for callback in list(self.callbacks):
                callback()

    application = types.SimpleNamespace(aboutToQuit=Signal())

    class FakeQCoreApplication:
        @staticmethod
        def instance():
            return application

    class FakeWatcher:
        instances = []

        def __init__(self) -> None:
            self.paths = []
            self.directoryChanged = Signal()
            self.fileChanged = Signal()
            self.delete_count = 0
            self.__class__.instances.append(self)

        def addPath(self, path: str) -> bool:
            self.paths.append(path)
            return True

        def addPaths(self, paths: list[str]) -> list[str]:
            self.paths.extend(paths)
            return []

        def removePaths(self, paths: list[str]) -> list[str]:
            self.paths = [path for path in self.paths if path not in paths]
            return []

        def files(self) -> list[str]:
            return [path for path in self.paths if Path(path).is_file()]

        def directories(self) -> list[str]:
            return [path for path in self.paths if not Path(path).is_file()]

        def deleteLater(self) -> None:
            self.delete_count += 1

    class FakeTimer:
        instances = []
        single_shots = []
        watchers = FakeWatcher.instances

        def __init__(self) -> None:
            self.interval = None
            self.timeout = Signal()
            self.start_count = 0
            self.stop_count = 0
            self.delete_count = 0
            self.__class__.instances.append(self)

        def setInterval(self, interval: int) -> None:
            self.interval = interval

        def setSingleShot(self, single: bool) -> None:
            self.single = single

        def start(self) -> None:
            self.start_count += 1

        def stop(self) -> None:
            self.stop_count += 1

        def deleteLater(self) -> None:
            self.delete_count += 1

        @classmethod
        def singleShot(cls, delay: int, callback) -> None:
            cls.single_shots.append((delay, callback))
            callback()

    mobase = types.ModuleType("mobase")
    mobase.IPluginTool = type("IPluginTool", (), {})
    mobase.IPluginDiagnose = type("IPluginDiagnose", (), {})
    mobase.VersionInfo = lambda *args: args
    mobase.VersionScheme = types.SimpleNamespace(REGULAR="regular")
    mobase.PluginSetting = lambda *args: args
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.QCoreApplication = FakeQCoreApplication
    qtcore.QTimer = FakeTimer
    qtcore.QFileSystemWatcher = FakeWatcher
    qtgui = types.ModuleType("PyQt6.QtGui")
    qtgui.QIcon = type("QIcon", (), {})
    pyqt = types.ModuleType("PyQt6")
    monkeypatch.setitem(sys.modules, "mobase", mobase)
    monkeypatch.setitem(sys.modules, "PyQt6", pyqt)
    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", qtcore)
    monkeypatch.setitem(sys.modules, "PyQt6.QtGui", qtgui)

    # The shared part is imported once per module name, so each test needs its own copy bound to this test's fakes.
    _forget_shared_plugin_code(monkeypatch)
    module_path = ROOT / "python" / entry
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # There is no Qt object to run background work on here: work is held until a test runs it.
    from fomod_logger_support import executor
    monkeypatch.setattr(executor, "make_qt_executor", lambda: executor.ManualExecutor())
    return module, FakeTimer, application


class _FakeModList:
    def __init__(
        self, *, installed_result: bool = True, removed_result: bool = True
    ) -> None:
        self.installed_result = installed_result
        self.removed_result = removed_result
        self.installed_callbacks = []
        self.removed_callbacks = []

    def onModInstalled(self, callback) -> bool:
        self.installed_callbacks.append(callback)
        return self.installed_result

    def onModRemoved(self, callback) -> bool:
        self.removed_callbacks.append(callback)
        return self.removed_result

    def allMods(self):
        return []


class _FakeOrganizer:
    def __init__(
        self,
        data_root: Path,
        *,
        installed_result: bool = True,
        removed_result: bool = True,
        refresh_result: bool = True,
    ) -> None:
        self.data_root = data_root
        self.mod_list = _FakeModList(
            installed_result=installed_result,
            removed_result=removed_result,
        )
        self.refresh_result = refresh_result
        self.enabled_callbacks = []
        self.disabled_callbacks = []
        self.refresh_callbacks = []

    def pluginDataPath(self):
        return str(self.data_root)

    def pluginSetting(self, name, setting):
        return False

    def basePath(self):
        return str(self.data_root / "instance")

    def modList(self):
        return self.mod_list

    # Plugins that are enabled in this fake MO2: the full edition's native installer, not FOMOD Plus or the other edition.
    enabled_plugins: set = {"FOMOD Logger Native Installer"}

    def isPluginEnabled(self, name: str) -> bool:
        return name in self.enabled_plugins

    def onPluginEnabled(self, name: str, callback) -> None:
        self.enabled_callbacks.append((name, callback))

    def onPluginDisabled(self, name: str, callback) -> None:
        self.disabled_callbacks.append((name, callback))

    def onNextRefresh(self, callback, immediate_if_possible: bool = True) -> bool:
        self.refresh_callbacks.append(callback)
        return self.refresh_result


def _plugin_source() -> str:
    """The full edition's entry file and the part both editions share."""

    return "\n".join((ROOT / "python" / name).read_text(encoding="utf-8") for name in (
        "fomod_logger_plugin.py", "fomod_logger_support/plugin_base.py"))


def test_standalone_runtime_has_no_active_fomod_plus_path() -> None:
    source = _plugin_source()

    assert "event_from_fomod_plus" not in source
    assert "_ingest_fomod_plus" not in source
    assert 'pluginSetting("FOMOD Plus"' not in source


def test_python_plugin_tracks_provider_removal_and_shutdown() -> None:
    source = _plugin_source()

    assert 'isPluginEnabled(_NATIVE_PLUGIN_NAME)' in source
    assert "onModRemoved(self._on_mod_removed)" in source
    assert "self._on_next_refresh(generation)" in source
    assert "if not self._active or generation != self._lifecycle_generation" in source
    assert "store.mark_removed" in source
    assert "store.current_mods(instance_id)" in source
    assert "onPluginEnabled(" in source
    assert "onPluginDisabled(self.name(), self._shutdown)" in source
    assert "application.aboutToQuit.connect(self._shutdown)" in source
    assert "window.close()" in source


def test_plugin_disable_enable_lifecycle_is_gated_idempotent_and_restartable(
    tmp_path, monkeypatch
) -> None:
    module, fake_timer, application = _load_plugin_module(
        monkeypatch, "fomod_logger_plugin_lifecycle_test"
    )


    organizer = _FakeOrganizer(tmp_path)
    plugin = module.FomodLoggerPlugin()
    calls = {"ingest": 0, "reconcile": 0}
    plugin._refresh_provider_health = lambda: None
    plugin._migrate_once = lambda: None

    def ingest() -> None:
        calls["ingest"] += 1

    def reconcile() -> None:
        calls["reconcile"] += 1

    plugin._ingest = ingest
    plugin._reconcile_current_mods = reconcile

    assert plugin.init(organizer) is True
    assert plugin._active is True
    assert len(fake_timer.instances) == 1
    assert len(organizer.mod_list.installed_callbacks) == 1
    assert len(organizer.mod_list.removed_callbacks) == 1
    assert len(organizer.enabled_callbacks) == 1
    assert len(organizer.disabled_callbacks) == 1
    assert len(application.aboutToQuit.callbacks) == 1
    assert len(organizer.refresh_callbacks) == 1
    assert calls["ingest"] == 1

    old_refresh = organizer.refresh_callbacks[0]
    organizer.disabled_callbacks[0][1]()
    organizer.disabled_callbacks[0][1]()
    assert plugin._active is False
    assert fake_timer.instances[0].stop_count == 1
    assert fake_timer.instances[0].delete_count == 1

    organizer.mod_list.installed_callbacks[0](object())
    organizer.mod_list.removed_callbacks[0]("Disabled Mod")
    old_refresh()
    assert fake_timer.single_shots == []
    assert calls == {"ingest": 1, "reconcile": 0}

    organizer.enabled_callbacks[0][1]()
    organizer.enabled_callbacks[0][1]()
    assert plugin._active is True
    assert len(fake_timer.instances) == 2
    assert len(organizer.refresh_callbacks) == 2
    assert len(organizer.mod_list.installed_callbacks) == 1
    assert len(organizer.mod_list.removed_callbacks) == 1
    assert len(organizer.enabled_callbacks) == 1
    assert len(organizer.disabled_callbacks) == 1
    assert calls["ingest"] == 2

    old_refresh()
    assert calls["reconcile"] == 0
    organizer.refresh_callbacks[1]()
    assert calls["reconcile"] == 1
    # The load-order snapshot waits until MO2 has refreshed the plugin list.
    assert [callback.__name__ for _, callback in fake_timer.single_shots] == ["_capture_load_order"]
    organizer.mod_list.installed_callbacks[0](object())
    assert len(fake_timer.single_shots) == 2
    assert calls["ingest"] == 3

    organizer.disabled_callbacks[0][1]()
    organizer.disabled_callbacks[0][1]()
    assert fake_timer.instances[1].stop_count == 1


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("mod-installed", "mod-installed callback"),
        ("mod-removed", "mod-removed callback"),
        ("next-refresh", "next-refresh callback"),
    ],
)
def test_plugin_fails_closed_when_bool_callback_registration_is_refused(
    tmp_path, monkeypatch, failure, message
) -> None:
    module, fake_timer, _application = _load_plugin_module(
        monkeypatch, f"fomod_logger_plugin_registration_{failure}"
    )


    organizer = _FakeOrganizer(
        tmp_path,
        installed_result=failure != "mod-installed",
        removed_result=failure != "mod-removed",
        refresh_result=failure != "next-refresh",
    )
    plugin = module.FomodLoggerPlugin()
    plugin._refresh_provider_health = lambda: None
    plugin._migrate_once = lambda: None
    plugin._ingest = lambda: None

    assert plugin.init(organizer) is True  # stays loaded so MO2 shows the problem
    assert plugin._active is False
    assert plugin.activeProblems() == [1]
    assert message in plugin.fullDescription(1)
    if failure == "next-refresh":
        assert len(fake_timer.instances) == 1
        assert fake_timer.instances[0].stop_count == 1
    else:
        assert fake_timer.instances == []  # stopped before any runtime started


def test_next_refresh_handles_mo2_initialization_and_immediate_completion(
    tmp_path, monkeypatch
) -> None:
    module, _, _ = _load_plugin_module(monkeypatch, "fomod_logger_refresh_contract")


    class MO2Organizer(_FakeOrganizer):
        plugin_live = False

        def onNextRefresh(self, callback, immediate_if_possible=True):
            # OrganizerCore returns an empty connection after the immediate
            # path; OrganizerProxy suppresses callbacks for inactive plugins.
            if immediate_if_possible:
                if self.plugin_live:
                    callback()
                return False
            return super().onNextRefresh(callback, immediate_if_possible)

    organizer = MO2Organizer(tmp_path)
    plugin = module.FomodLoggerPlugin()
    plugin._refresh_provider_health = lambda: None
    plugin._migrate_once = lambda: None
    plugin._ingest = lambda: None
    reconciled = []
    plugin._reconcile_current_mods = lambda: reconciled.append(True)

    assert plugin.init(organizer) is True
    assert len(organizer.refresh_callbacks) == 1
    assert reconciled == []
    organizer.plugin_live = True
    organizer.refresh_callbacks[0]()
    assert reconciled == [True]
    plugin._shutdown()
    assert plugin._active is False
    plugin._on_plugin_enabled()
    assert plugin._active is True
    assert plugin._runtime_error == ""
    assert reconciled == [True, True]
    plugin._shutdown()


def test_plugin_init_reports_data_root_creation_failure(
    tmp_path, monkeypatch
) -> None:
    mobase = types.ModuleType("mobase")
    mobase.IPluginTool = type("IPluginTool", (), {})
    mobase.IPluginDiagnose = type("IPluginDiagnose", (), {})
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.QCoreApplication = type("QCoreApplication", (), {})
    qtcore.QTimer = type("QTimer", (), {})
    qtcore.QFileSystemWatcher = type("QFileSystemWatcher", (), {})
    qtgui = types.ModuleType("PyQt6.QtGui")
    qtgui.QIcon = type("QIcon", (), {})
    pyqt = types.ModuleType("PyQt6")
    monkeypatch.setitem(sys.modules, "mobase", mobase)
    monkeypatch.setitem(sys.modules, "PyQt6", pyqt)
    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", qtcore)
    monkeypatch.setitem(sys.modules, "PyQt6.QtGui", qtgui)

    _forget_shared_plugin_code(monkeypatch)
    module_path = ROOT / "python" / "fomod_logger_plugin.py"
    spec = importlib.util.spec_from_file_location(
        "fomod_logger_plugin_data_root_test", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    blocked_root = tmp_path / "fomod_logger"
    blocked_root.write_text("not a directory", encoding="utf-8")

    class Organizer:
        def pluginDataPath(self):
            return str(tmp_path)

    plugin = module.FomodLoggerPlugin()
    assert plugin.init(Organizer()) is True  # stays loaded so MO2 shows the problem
    assert plugin.activeProblems() == [1]
    assert plugin.fullDescription(1)


def test_final_public_and_mo2_version_identities() -> None:
    plugin = _plugin_source()
    header = (ROOT / "src" / "fomodloggerversion.h").read_text(encoding="utf-8")
    installer = (ROOT / "src" / "installerfomod.cpp").read_text(encoding="utf-8")

    assert '_PUBLIC_SEMVER = "1.0.0"' in plugin
    assert '_MO2_VERSION = "1.0.0"' in plugin
    assert (
        "mobase.VersionInfo(_MO2_VERSION, mobase.VersionScheme.REGULAR)" in plugin
    )
    assert '#define FOMOD_LOGGER_VERSION "1.0.0"' in header
    assert '#define FOMOD_LOGGER_MO2_VERSION "1.0.0"' in header
    assert "QStringLiteral(FOMOD_LOGGER_MO2_VERSION)" in installer
    assert "VersionInfo::SCHEME_REGULAR" in installer
    assert (
        'installer.insert("version", QStringLiteral(FOMOD_LOGGER_VERSION))'
        in installer
    )


def test_new_event_files_are_imported_on_folder_change_not_on_a_timer(tmp_path, monkeypatch) -> None:
    module, fake_timer, _application = _load_plugin_module(monkeypatch, "fomod_logger_plugin_watcher_test")
    notices = []


    plugin = module.FomodLoggerPlugin()
    plugin._window = types.SimpleNamespace(schedule_refresh=lambda: notices.append("changed"),
                                           close=lambda: None, deleteLater=lambda: None)
    plugin._organizer = _FakeOrganizer(tmp_path)
    plugin._data_root = tmp_path
    plugin._refresh_provider_health = lambda: None
    plugin._migrate_once = lambda: None
    ingests = []
    plugin._ingest = lambda: ingests.append(1)
    plugin._start_runtime(initializing=True)

    [watcher] = fake_timer.watchers[-1:]
    assert [Path(path).name for path in watcher.paths] == ["events"]
    [timer] = fake_timer.instances[-1:]
    assert timer.single is True and timer.start_count == 0  # nothing runs until a file appears

    watcher.directoryChanged.callbacks[0](str(tmp_path / "events"))
    assert timer.start_count == 1
    timer.timeout.emit()
    assert len(ingests) == 2  # one at startup, one for the new file

    # A changed load order is pushed to open pages; an unchanged one is not.
    plugin._load_order = {"changed": True}
    plugin._capture_load_order()
    assert notices == ["changed"]
    plugin._capture_load_order()
    assert notices == ["changed"]

    plugin._shutdown()
    assert watcher.delete_count == 1
    watcher.directoryChanged.callbacks[0](str(tmp_path / "events"))
    assert timer.start_count == 1  # ignored after shutdown


def test_profile_list_files_changing_on_disk_retake_the_load_order(tmp_path, monkeypatch) -> None:
    # Another plugin can switch plugins through IPluginList.setState, which
    # raises no MO2 signal; MO2 only rewrites plugins.txt.
    module, fake_timer, _application = _load_plugin_module(monkeypatch, "fomod_logger_plugin_profile_test")
    profile = tmp_path / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "plugins.txt").write_text("*Village.esp\n", encoding="utf-8")
    plugin = module.FomodLoggerPlugin()
    plugin._organizer = _FakeOrganizer(tmp_path)
    plugin._organizer.profilePath = lambda: str(profile)
    plugin._data_root = tmp_path
    plugin._refresh_provider_health = lambda: None
    plugin._migrate_once = lambda: None
    plugin._ingest = lambda: None
    captures = []
    plugin._capture_load_order = lambda: captures.append(1)
    plugin._start_runtime(initializing=True)

    [watcher] = fake_timer.watchers[-1:]
    assert sorted(Path(path).name for path in watcher.paths) == ["Default", "events", "plugins.txt"]
    assert len(captures) == 1  # the startup snapshot only
    watcher.fileChanged.callbacks[0](str(profile / "plugins.txt"))
    assert len(captures) == 2
    watcher.directoryChanged.callbacks[0](str(profile))
    assert len(captures) == 3
    watcher.directoryChanged.callbacks[0](str(tmp_path / "events"))
    assert len(captures) == 3  # new event files are an import, not a load-order change

    plugin._shutdown()
    watcher.fileChanged.callbacks[0](str(profile / "plugins.txt"))
    assert len(captures) == 3  # ignored after shutdown


def test_window_errors_show_in_mo2_then_expire(tmp_path, monkeypatch) -> None:
    module, _, _ = _load_plugin_module(monkeypatch, "logger_window_error_test")
    plugin = module.FomodLoggerPlugin()
    plugin._organizer = _FakeOrganizer(tmp_path)
    plugin._active = True
    clock = [1000.0]
    monkeypatch.setattr(module._base.time, "monotonic", lambda: clock[0])

    plugin._on_window_error("FOMOD Logger window: OSError: disk")
    assert plugin.activeProblems() == [7]
    assert "disk" in plugin.fullDescription(7)
    clock[0] += module._base._WINDOW_ERROR_SECONDS + 1
    assert plugin.activeProblems() == []


def test_native_event_write_is_single_atomic_file_without_recovery_spool() -> None:
    # The native side cannot run under pytest; check the few facts the Python
    # side depends on: one events/<id>.json per install, no recovery files.
    native = "\n".join(
        (ROOT / "src" / name).read_text(encoding="utf-8")
        for name in (
            "installerfomod.cpp",
            "fomodloggereventwriter.cpp",
            "fomodloggereventwriter.h",
        )
    )
    assert 'QStringLiteral("fomod_logger/events")' in native
    assert "QSaveFile" in native
    assert "recovery" not in native.lower()
    assert ".indexed" not in native
    assert "PROBLEM_EVENT_WRITE_FAILED" in native


def test_native_prefer_setting_orders_logger_around_stock_installers() -> None:
    installer = (ROOT / "src" / "installerfomod.cpp").read_text(encoding="utf-8")
    assert 'PluginSetting(\n      "prefer"' in installer
    # Stock XML/C# FOMOD installers use 110 (prefer) / 90.
    assert '"prefer").toBool() ? 120 : 80' in installer


def test_developer_build_entrypoints_include_the_logger_writer() -> None:
    qmake = (ROOT / "src" / "installerFomod.pro").read_text(encoding="utf-8")
    scons = (ROOT / "src" / "SConscript").read_text(encoding="utf-8")

    assert "fomodloggereventwriter.cpp" in qmake
    assert "fomodloggereventwriter.h" in qmake
    assert "fomodloggerversion.h" in qmake
    assert "env.Glob('*.cpp')" in scons


@pytest.mark.parametrize("both_files", [False, True])
@pytest.mark.parametrize("legacy_location", ["instance", "plugin-data"])
@pytest.mark.parametrize("selected", ["false", "true", "", 0, 1, None, [], {}])
def test_migration_invalid_selection_preserves_source_and_retries(
    tmp_path, monkeypatch, legacy_location, selected, both_files
):
    import json

    module, _, _ = _load_plugin_module(monkeypatch, "logger_migration_selection_test")
    organizer = _FakeOrganizer(tmp_path)
    plugin = module.FomodLoggerPlugin()
    plugin._organizer = organizer
    plugin._data_root = tmp_path / "fomod_logger"
    plugin._data_root.mkdir()
    legacy_root = Path(
        organizer.basePath() if legacy_location == "instance" else organizer.pluginDataPath()
    )
    legacy_root.mkdir(parents=True, exist_ok=True)
    legacy_path = legacy_root / "fomod_data.json"
    entries = [
        {"mod_name": "Earlier valid record", "options": []},
        {
            "mod_name": "Selection record",
            "options": [{"option": "Choice", "selected": selected}],
        },
    ]
    original = json.dumps({"mods": entries}).encode("utf-8")
    legacy_path.write_bytes(original)
    other_path = None
    if both_files:
        other_root = Path(
            organizer.pluginDataPath()
            if legacy_location == "instance"
            else organizer.basePath()
        )
        other_root.mkdir(parents=True, exist_ok=True)
        other_path = other_root / "fomod_data.json"
        other_source = b'{"mods": [{"mod_name": "Other file record", "options": []}]}'
        other_path.write_bytes(other_source)
    marker = plugin._data_root / (
        f".legacy-migration-v1-{module.stable_instance_id(Path(organizer.basePath()))}"
        if legacy_location == "instance" else ".legacy-migration-v1"
    )
    database = plugin._data_root / "fomod_logger.sqlite3"

    # The entire file must validate before any event from it is ingested.
    for _ in range(2):
        plugin._migrate_once()
        assert not marker.exists()
        assert "selected must be a boolean" in plugin._migration_error
        assert legacy_path.read_bytes() == original
        if other_path is not None:
            assert other_path.read_bytes() == other_source
        with module.EventStore(database) as store:
            assert store.history() == []

    entries[1]["options"][0]["selected"] = False
    corrected = json.dumps({"mods": entries}).encode("utf-8")
    legacy_path.write_bytes(corrected)
    plugin._migrate_once()
    assert marker.is_file()
    assert plugin._migration_error == ""
    assert legacy_path.read_bytes() == corrected
    with module.EventStore(database) as store:
        history = store.history()
        assert len(history) == (3 if both_files else 2)
        migrated = next(e for e in history if e["mod"]["name"] == "Selection record")
        assert migrated["steps"][0]["groups"][0]["options"][0]["selected"] is False
    plugin._migrate_once()
    with module.EventStore(database) as store:
        assert len(store.history()) == (3 if both_files else 2)


def test_a_failed_import_is_retried_a_few_times_then_waits(tmp_path, monkeypatch) -> None:
    module, fake_timer, _application = _load_plugin_module(monkeypatch, "fomod_logger_plugin_retry_test")
    plugin = module.FomodLoggerPlugin()
    plugin._organizer = _FakeOrganizer(tmp_path)
    plugin._data_root = tmp_path
    plugin._active = True
    plugin._refresh_provider_health = lambda: None

    class LockedStore:
        def __init__(self, *_args): pass
        def __enter__(self): raise RuntimeError("database is locked")
        def __exit__(self, *_args): return False

    monkeypatch.setattr(module, "EventStore", LockedStore)
    fake_timer.single_shots.clear()
    plugin._ingest()  # the fake singleShot runs each retry immediately
    delays = [delay for delay, callback in fake_timer.single_shots]
    assert delays == [2_000, 4_000, 6_000]
    assert plugin._ingest_error == "database is locked"


def test_each_instance_sharing_one_data_root_migrates_its_own_legacy_file(tmp_path, monkeypatch) -> None:
    import json

    module, _, _ = _load_plugin_module(monkeypatch, "logger_migration_per_instance_test")
    data_root = tmp_path / "shared" / "fomod_logger"
    data_root.mkdir(parents=True)
    names = []
    for instance in ("Skyrim", "Fallout"):
        organizer = _FakeOrganizer(tmp_path / instance)
        Path(organizer.basePath()).mkdir(parents=True)
        (Path(organizer.basePath()) / "fomod_data.json").write_text(
            json.dumps({"mods": [{"mod_name": f"{instance} mod", "options": []}]}), encoding="utf-8")
        plugin = module.FomodLoggerPlugin()
        plugin._organizer = organizer
        plugin._data_root = data_root
        plugin._migrate_once()
        assert plugin._migration_error == ""
    with module.EventStore(data_root / "fomod_logger.sqlite3") as store:
        names = sorted(event["mod"]["name"] for event in store.history())
    assert names == ["Fallout mod", "Skyrim mod"]


def test_the_shared_legacy_file_migrates_once_not_into_every_instance(tmp_path, monkeypatch) -> None:
    import json

    module, _, _ = _load_plugin_module(monkeypatch, "logger_migration_shared_file_test")
    shared = tmp_path / "plugins" / "data"
    data_root = shared / "fomod_logger"
    data_root.mkdir(parents=True)
    (shared / "fomod_data.json").write_text(
        json.dumps({"mods": [{"mod_name": "Shared mod", "options": []}]}), encoding="utf-8")
    for instance in ("Skyrim", "Fallout"):
        organizer = _FakeOrganizer(tmp_path / instance)
        organizer.pluginDataPath = lambda: str(shared)
        plugin = module.FomodLoggerPlugin()
        plugin._organizer = organizer
        plugin._data_root = data_root
        plugin._migrate_once()
        assert plugin._migration_error == ""
    with module.EventStore(data_root / "fomod_logger.sqlite3") as store:
        assert [event["mod"]["name"] for event in store.history()] == ["Shared mod"]


def test_reconcile_follows_renamed_and_case_changed_mods_and_removes_gone_ones(tmp_path, monkeypatch) -> None:
    module, _, _ = _load_plugin_module(monkeypatch, "logger_reconcile_rename_test")
    organizer = _FakeOrganizer(tmp_path)
    instance_id = module.stable_instance_id(Path(organizer.basePath()))

    def event(number: int, name: str, archive: str) -> dict:
        return {
            "schema_version": 1,
            "event_id": f"{number:08d}-2222-4222-8222-222222222222",
            "started_at": "2026-10-20T12:00:00+00:00",
            "completed_at": "2026-10-20T12:01:00+00:00",
            "instance": {"id": instance_id, "label": "MO2 Test"},
            "source": {"archive": archive, "reinstallation": False},
            "mod": {"name": name, "version": "1.0", "nexus_id": 1},
            "installer": {"name": "FOMOD Logger Native Installer", "version": "1.0.0",
                          "upstream_commit": "fc263f2d923c704b4853c11ed4f8b8cf3920f30d"},
            "steps": [],
        }

    plugin = module.FomodLoggerPlugin()
    plugin._organizer = organizer
    plugin._data_root = tmp_path / "fomod_logger"
    plugin._data_root.mkdir()
    plugin._active = True
    database = plugin._data_root / "fomod_logger.sqlite3"
    with module.EventStore(database) as store:
        store.ingest(event(1, "Old Name", r"C:\Downloads\Renamed-123.7z"))
        store.ingest(event(2, "Case Mod", "Case-1.zip"))
        store.ingest(event(3, "Gone Mod", "Gone-1.zip"))
        store.ingest(event(4, "Twin Mod", "Twin.zip"))

        store.ingest(event(5, "X 2", "X.zip"))

    # Two unlogged mods from one archive: unclear which is the rename, so
    # the record stays as it is. "X" is an older manual install from the
    # same archive as the deleted "X 2": its folder predates that install,
    # so it must not inherit X 2's choices.
    archives = {"New Name": "C:/MO2/downloads/Renamed-123.7z", "case mod": "Case-1.zip",
                "Twin A": "Twin.zip", "Twin B": "Twin.zip", "X": "X.zip"}
    install = module._seconds("2026-10-20T12:00:00+00:00")
    created = {"New Name": install + 30, "Twin A": install + 5, "Twin B": install + 9,
               "X": install - 86_400}
    organizer.mod_list.allMods = lambda: list(archives)
    organizer.mod_list.getMod = lambda name: types.SimpleNamespace(installationFile=lambda: archives[name])
    plugin._folder_created = lambda _mod_list, name: created.get(name)
    notices = []
    plugin._notify_viewer = lambda: notices.append(1)

    plugin._reconcile_current_mods()
    with module.EventStore(database) as store:
        assert store.current_mod_names(instance_id) == ["case mod", "New Name", "Twin Mod"]
        renamed = store.latest("new name")[0]
        assert renamed["mod"]["recorded_name"] == "Old Name"
        removed = {e["mod"]["name"] for e in store.history() if e["removed_at"]}
        assert removed == {"Gone Mod", "X 2"}
    assert notices == [1]

    # Nothing left to change: the ambiguous twins alone do not notify.
    plugin._reconcile_current_mods()
    assert notices == [1]


def test_reconcile_marks_removed_when_folder_times_are_unknown(tmp_path, monkeypatch) -> None:
    module, _, _ = _load_plugin_module(monkeypatch, "logger_reconcile_unknown_time_test")
    organizer = _FakeOrganizer(tmp_path)
    instance_id = module.stable_instance_id(Path(organizer.basePath()))
    plugin = module.FomodLoggerPlugin()
    plugin._organizer = organizer
    plugin._data_root = tmp_path / "fomod_logger"
    plugin._data_root.mkdir()
    plugin._active = True
    database = plugin._data_root / "fomod_logger.sqlite3"
    with module.EventStore(database) as store:
        store.ingest({
            "schema_version": 1, "event_id": "00000001-2222-4222-8222-222222222222",
            "started_at": "2026-10-20T12:00:00+00:00", "completed_at": "2026-10-20T12:01:00+00:00",
            "instance": {"id": instance_id, "label": "MO2 Test"},
            "source": {"archive": "Same.7z", "reinstallation": False},
            "mod": {"name": "Old Name", "version": "1.0", "nexus_id": 1},
            "installer": {"name": "N", "version": "2", "upstream_commit": "unknown"},
            "steps": []})
    organizer.mod_list.allMods = lambda: ["New Name"]
    organizer.mod_list.getMod = lambda name: types.SimpleNamespace(
        installationFile=lambda: "Same.7z", absolutePath=lambda: str(tmp_path / "missing"))

    plugin._reconcile_current_mods()

    with module.EventStore(database) as store:
        assert store.current_mod_names(instance_id) == []


def test_a_new_database_migrates_legacy_data_again(tmp_path, monkeypatch) -> None:
    import json

    module, _, _ = _load_plugin_module(monkeypatch, "logger_migration_new_database_test")
    organizer = _FakeOrganizer(tmp_path / "Skyrim")
    Path(organizer.basePath()).mkdir(parents=True)
    (Path(organizer.basePath()) / "fomod_data.json").write_text(
        json.dumps({"mods": [{"mod_name": "Legacy mod", "options": []}]}), encoding="utf-8")
    data_root = tmp_path / "fomod_logger"
    data_root.mkdir()
    plugin = module.FomodLoggerPlugin()
    plugin._organizer = organizer
    plugin._data_root = data_root
    plugin._migrate_once()
    database = data_root / "fomod_logger.sqlite3"
    assert list(data_root.glob(".legacy-migration-v1*"))
    (data_root / ".legacy-migration-v1").touch()

    # The database was moved aside, so the 1.x records must come back.
    for path in data_root.glob("fomod_logger.sqlite3*"):
        path.rename(tmp_path / ("moved-" + path.name))
    plugin._migrate_once()
    with module.EventStore(database) as store:
        assert [event["mod"]["name"] for event in store.history()] == ["Legacy mod"]
    plugin._migrate_once()  # an existing database keeps its markers
    with module.EventStore(database) as store:
        assert store.event_count() == 1


@pytest.mark.parametrize("leftovers", [("original", "custom"), ("custom",), ()])
def test_files_left_by_logger_1x_are_reported_with_the_fix(tmp_path, monkeypatch, leftovers) -> None:
    module, _, _ = _load_plugin_module(monkeypatch, f"logger_legacy_files_test_{len(leftovers)}")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    names = {"original": "installer_fomod_original.dll", "custom": "installer_fomod_logger.dll"}
    for leftover in leftovers:
        (plugins / names[leftover]).write_bytes(b"MZ")
    plugin = module.FomodLoggerPlugin()
    plugin._organizer = _FakeOrganizer(tmp_path)
    plugin._organizer.pluginsPath = lambda: str(plugins)
    plugin._check_legacy_files()
    if not leftovers:
        assert 8 not in plugin.activeProblems()
        return
    assert 8 in plugin.activeProblems()
    text = plugin.fullDescription(8)
    assert "Close Mod Organizer" in text and "delete installer_fomod_logger.dll" in text
    if "original" in leftovers:
        assert "rename installer_fomod_original.dll to installer_fomod.dll" in text
    else:
        assert "restore installer_fomod.dll from your Mod Organizer download" in text


def test_plugins_of_disabled_mods_are_found_and_cached(tmp_path, monkeypatch) -> None:
    module, _, _ = _load_plugin_module(monkeypatch, "logger_disabled_mod_plugins_test")
    village = tmp_path / "mods" / "Village"
    village.mkdir(parents=True)
    (village / "Village.esp").write_bytes(b"TES4")
    (village / "readme.txt").write_text("not a plugin", encoding="utf-8")
    mod_list = types.SimpleNamespace(getMod=lambda name: types.SimpleNamespace(absolutePath=lambda: str(village)))
    plugin = module.FomodLoggerPlugin()
    assert plugin._plugins_of(mod_list, ["Village"]) == {"Village.esp": "Village"}
    (village / "Village.esp").unlink()  # same folder time stamp: the cached listing is reused
    stamp = village.stat().st_mtime_ns
    plugin._mod_plugin_cache[str(village)] = (stamp, ["Village.esp"])
    assert plugin._plugins_of(mod_list, ["Village"]) == {"Village.esp": "Village"}
    missing = types.SimpleNamespace(getMod=lambda name: types.SimpleNamespace(absolutePath=lambda: str(tmp_path / "gone")))
    assert plugin._plugins_of(missing, ["Gone"]) == {}


# ---------- new-patch alerts ----------

def _alert_plugin(tmp_path, monkeypatch, name: str, *, alerts_on: bool = True, event=None, mods=()):
    """A plugin wired to a database that holds the Grass Patches install (a skipped Village patch), with the
    load order snapshots supplied by the test instead of MO2. MO2 has read its mod list (``_ready``)."""

    from test_patch_alerts import grass_patches

    module, _, _ = _load_plugin_module(monkeypatch, name)
    organizer = _FakeOrganizer(tmp_path)
    organizer.pluginSetting = lambda plugin, key: alerts_on if key == "alert_new_patches" else False
    folders = {}
    for mod in ("Village", "Bridges", *mods):
        folder = tmp_path / "mods" / mod
        folder.mkdir(parents=True)
        if mod != "Village Overhaul":
            (folder / f"{mod}.esp").write_bytes(b"TES4")
        folders[mod] = folder
    organizer.mod_list.getMod = lambda mod: types.SimpleNamespace(absolutePath=lambda: str(folders[mod]))
    plugin = module.FomodLoggerPlugin()
    plugin._organizer = organizer
    plugin._data_root = tmp_path / "fomod_logger"
    plugin._data_root.mkdir()
    plugin._active = plugin._ready = True
    plugin._ingest = lambda: None
    plugin.invalidated = []
    plugin._invalidate = lambda: plugin.invalidated.append(1)
    with module.EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        store.ingest(event or grass_patches())
    return module, plugin, folders


def _snapshot(*active: str, **kwargs) -> dict:
    from test_patch_alerts import order

    return order(*active, **kwargs)


def _plugins(**mods: str) -> dict:
    """What `_capture_load_order` also finds: which plugins each mod brought (folded names)."""

    return {mod.casefold(): {plugin.casefold() for plugin in plugins.split(",") if plugin} for mod, plugins in mods.items()}


# MO2 with Bridges installed, then Village installed on top (switched on, or still off).
def _base():
    return _snapshot("Bridges.esp", mods=("Bridges",)), _plugins(Bridges="Bridges.esp")


def _with_village():
    return (_snapshot("Bridges.esp", "Village.esp", mods=("Bridges", "Village")),
            _plugins(Bridges="Bridges.esp", Village="Village.esp"))


def _village_off():
    return (_snapshot("Bridges.esp", mods=("Bridges",), disabled={"Village.esp": "Village"}, disabled_mods=("Village",)),
            _plugins(Bridges="Bridges.esp", Village="Village.esp"))


def _look(plugin, look, previous=None) -> None:
    """One look at the load order, as `_capture_load_order` ends: (snapshot, plugins of each mod)."""

    snapshot, mod_plugins = look
    plugin._load_order = snapshot
    plugin._alert_work(previous, snapshot, mod_plugins)


def test_installing_the_mod_a_skipped_patch_is_made_for_shows_a_problem_with_a_guided_fix(tmp_path, monkeypatch):
    from test_patch_alerts import GRASS_ID, VILLAGE_OPTION

    module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_problem_test")
    windows = []
    plugin._create_window = lambda: windows.append(_FakeWindow()) or windows[-1]
    _look(plugin, _base())    # the first look after start-up is the baseline: nothing in it is new
    assert plugin.activeProblems() == []
    plugin._capture_load_order = lambda: _look(plugin, _with_village(), _base()[0])
    plugin._on_mod_installed(object())    # MO2's announcement imports the event and looks at the load order again
    assert plugin.activeProblems() == [100]
    assert plugin.shortDescription(100) == "New patches available for Village"
    text = plugin.fullDescription(100)
    assert ("Grass Patches › Compatibility › Village patch: patch for Village; "
            "you skipped it when you installed Grass Patches") in text
    assert "Reinstall Grass Patches and pick the patch to add it." in text
    assert "enable it first" not in text    # Village is switched on in this snapshot
    assert plugin.hasGuidedFix(100) is True and plugin.hasGuidedFix(1) is False
    assert plugin.invalidated == [1]    # MO2 is asked to read the problems again, once

    plugin.startGuidedFix(100)
    [window] = windows
    assert window.shown == 1
    assert window.highlighted == [(GRASS_ID, VILLAGE_OPTION, "Village")]
    assert plugin.activeProblems() == []    # the alert is settled
    assert plugin.invalidated == [1, 1]
    with module.EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        assert store.patch_alerts(pending=True) == [] and len(store.patch_alerts(pending=False)) == 1
    plugin.startGuidedFix(100)    # nothing left to fix: a stale click does nothing
    assert window.shown == 1


def test_fix_closes_mo2s_problem_dialog_first_and_only_that_one(tmp_path, monkeypatch):
    module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_dialog_test")
    steps = []
    plugin._create_window = lambda: steps.append("window") or _FakeWindow()
    _look(plugin, _base())
    _look(plugin, _with_village(), _base()[0])

    class Dialog:
        def __init__(self, name: str) -> None:
            self.name = name

        def metaObject(self):
            return types.SimpleNamespace(className=lambda: self.name)

        def reject(self) -> None:
            steps.append(f"reject {self.name}")

    widgets = types.ModuleType("PyQt6.QtWidgets")
    for name, expected in (("ProblemsDialog", ["reject ProblemsDialog", "window"]), ("QMessageBox", ["window"])):
        steps.clear()
        plugin._window = None
        widgets.QApplication = types.SimpleNamespace(activeModalWidget=lambda: Dialog(name))
        monkeypatch.setitem(sys.modules, "PyQt6.QtWidgets", widgets)
        if name == "QMessageBox":    # the first fix settled the alert: raise it again
            with module.EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
                store.forget_patch_alerts(_with_village()[0]["instance_id"], "Village")
            plugin._has_alerts = True
            plugin._install_checks.clear()
            plugin._known_mods.pop("village")
            _look(plugin, _with_village(), _base()[0])
        plugin.startGuidedFix(plugin.activeProblems()[0])
        assert steps == expected


def test_a_mod_that_is_still_disabled_is_reported_as_needing_to_be_enabled(tmp_path, monkeypatch):
    _module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_disabled_test")
    _look(plugin, _base())
    _look(plugin, _village_off(), _base()[0])
    assert plugin.activeProblems() == [100]
    assert "Village is still disabled in MO2: enable it first." in plugin.fullDescription(100)
    # Switching the mod on keeps the alert (the patch is still skipped), and the problem keeps its key.
    _look(plugin, _with_village(), _village_off()[0])
    assert plugin.activeProblems() == [100]
    assert "enable it first" not in plugin.fullDescription(100)
    assert plugin.invalidated == [1, 1]    # the state changed, so MO2 reads it again


def test_the_alert_goes_when_its_patch_is_dealt_with(tmp_path, monkeypatch):
    from test_patch_alerts import GRASS_ID, VILLAGE_OPTION

    module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_settle_test")
    _look(plugin, _base())
    _look(plugin, _with_village(), _base()[0])
    plugin._load_order = _with_village()[0]
    assert plugin.activeProblems() == [100]

    # Not needed, set in the window: it calls back, and the plugin re-reads the alerts.
    with module.EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        store.save_review({"event_id": GRASS_ID, "option_id": VILLAGE_OPTION, "reason": "", "state": "dismissed"})
    plugin._alerts_changed()
    assert plugin.activeProblems() == []
    assert plugin.invalidated == [1, 1]
    with module.EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        assert store.patch_alerts(pending=True) == []    # a review is a definite reason: cleared for good

    # The same for a patch that gets installed: a snapshot with the patch plugin active.
    with module.EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        store.delete_review({"event_id": GRASS_ID, "option_id": VILLAGE_OPTION})
        store.forget_patch_alerts(_with_village()[0]["instance_id"], "Village")
    plugin._known_mods.pop("village")
    _look(plugin, _with_village(), _base()[0])
    assert plugin.activeProblems() == [100]
    patched = (_snapshot("Bridges.esp", "Village.esp", "GP - Village.esp", mods=("Bridges", "Village")), _with_village()[1])
    _look(plugin, patched, _with_village()[0])
    assert plugin.activeProblems() == []
    with module.EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        assert store.patch_alerts(pending=True) == []


def test_a_reminder_set_after_the_alert_settles_it(tmp_path, monkeypatch):
    from test_patch_alerts import GRASS_ID, VILLAGE_OPTION

    module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_reminder_test")
    _look(plugin, _base())
    _look(plugin, _with_village(), _base()[0])
    plugin._load_order = _with_village()[0]
    assert plugin.activeProblems() == [100]
    with module.EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        store.save_review({"event_id": GRASS_ID, "option_id": VILLAGE_OPTION, "reason": "later", "state": "pending"})
    plugin._alerts_changed()
    assert plugin.activeProblems() == []


def test_the_empty_start_up_load_order_never_settles_an_alert(tmp_path, monkeypatch):
    # MO2 2.5.2 starts plugins before it reads its mod list: the first snapshot has no mods and no plugins.
    module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_startup_test")
    _look(plugin, _base())
    _look(plugin, _with_village(), _base()[0])
    assert plugin.activeProblems() == [100]
    database = plugin._data_root / "fomod_logger.sqlite3"

    # A new MO2 session: the plugin knows nothing but the database.
    restarted = module.FomodLoggerPlugin()
    restarted._organizer, restarted._data_root, restarted._active = plugin._organizer, plugin._data_root, True
    restarted._invalidate = lambda: None
    empty = {**_snapshot(), "mods": [], "active_plugins": []}
    restarted._alert_work(None, empty, {})    # start-up: not ready, so nothing at all happens
    assert restarted.activeProblems() == []
    restarted._ready = True    # MO2's first refresh has run ...
    restarted._alert_work(None, empty, {})    # ... but a look that still shows nothing must not judge either
    restarted._refresh_alerts(empty)
    assert restarted.activeProblems() == []    # hidden for now
    with module.EventStore(database) as store:
        [row] = store.patch_alerts(pending=True)    # not settled for good
        assert row["mod_name"] == "Village"
    restarted._alert_work(empty, _with_village()[0], _with_village()[1])    # the real list arrives
    assert restarted.activeProblems() == [100]
    assert "Village patch" in restarted.fullDescription(100)
    assert restarted._install_checks == {} and set(restarted._known_mods) == {"grass patches", "bridges", "village"}
    with module.EventStore(database) as store:
        assert len(store.patch_alerts(pending=True)) == 1


def test_alerts_wait_for_mo2s_first_refresh(tmp_path, monkeypatch):
    module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_ready_test")
    plugin._ready = False
    plugin._lifecycle_generation = 1
    captures = []
    plugin._reconcile_current_mods = lambda: None
    plugin._capture_load_order = lambda: captures.append(plugin._ready)
    _look(plugin, _base())
    assert plugin._known_mods is None    # nothing was learned before MO2 had its lists
    plugin._on_next_refresh(2)    # a stale generation does nothing
    assert plugin._ready is False and captures == []
    plugin._on_next_refresh(1)
    assert plugin._ready is True and captures == [True]    # the capture that follows is a judged one
    plugin._shutdown()
    assert plugin._ready is False


def test_alerts_can_be_turned_off_and_back_on_from_mo2(tmp_path, monkeypatch):
    _module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_off_test", alerts_on=False)
    _look(plugin, _base())
    _look(plugin, _with_village(), _base()[0])
    assert plugin.activeProblems() == [] and plugin.hasGuidedFix(100) is False
    assert plugin.invalidated == []
    # Turned on in MO2's settings while running: MO2 tells the plugin, which asks MO2 to read the problems again.
    plugin._on_setting_changed("FOMOD Logger", "alert_new_patches", False, True)
    assert plugin._alerts_setting is True and plugin.invalidated == [1]
    plugin._on_setting_changed("Another plugin", "alert_new_patches", True, False)
    assert plugin._alerts_setting is True


def test_mo2_may_ask_for_problems_from_another_thread_so_nothing_there_calls_mo2(tmp_path, monkeypatch):
    _module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_thread_test")
    _look(plugin, _base())
    _look(plugin, _with_village(), _base()[0])
    calls = []
    plugin._organizer.pluginSetting = lambda *args: calls.append(args) or True
    plugin._organizer.modList = lambda: calls.append("modList")
    plugin._refresh_provider_health = lambda: None
    assert plugin.activeProblems() == [100]
    assert plugin.shortDescription(100) and plugin.fullDescription(100) and plugin.hasGuidedFix(100)
    assert calls == []


def test_the_check_waits_for_mo2_to_list_the_mod_then_looks_once(tmp_path, monkeypatch):
    _module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_wait_test")
    _look(plugin, _base())
    # The mod is on the list, switched on, but MO2 hasn't listed its plugin yet: wait for that refresh too.
    _look(plugin, (_snapshot("Bridges.esp", mods=("Bridges", "Village")), _plugins(Bridges="Bridges.esp")), _base()[0])
    assert plugin.activeProblems() == [] and list(plugin._install_checks) == ["village"]
    _look(plugin, _with_village())
    assert plugin.activeProblems() == [100] and plugin._install_checks == {}
    _look(plugin, _with_village())    # nothing is queued any more: no second alert
    assert plugin.invalidated == [1]


def test_a_failed_check_is_tried_again_at_the_next_look(tmp_path, monkeypatch, capsys):
    module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_retry_test")
    _look(plugin, _base())
    real = module._base.EventStore

    class Broken:
        def __init__(self, *_args):
            raise RuntimeError("database is locked")

    monkeypatch.setattr(module._base, "EventStore", Broken)
    _look(plugin, _with_village(), _base()[0])
    assert "new-patch check failed: database is locked" in capsys.readouterr().err
    assert list(plugin._install_checks) == ["village"]    # still queued
    monkeypatch.setattr(module._base, "EventStore", real)
    _look(plugin, _with_village())
    assert plugin.activeProblems() == [100] and plugin._install_checks == {}


def test_reinstalling_a_mod_is_not_new_whether_replaced_or_merged(tmp_path, monkeypatch):
    # Village was installed before, and the Grass patch for it was already a card then: no alert for it.
    _module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_reinstall_test")
    _look(plugin, _with_village())    # the baseline
    assert plugin.activeProblems() == []

    # Replace: MO2 deletes the mod's folder and makes it again, so the mod and its plugin vanish and come back.
    gone = (_snapshot("Bridges.esp", mods=("Bridges",)), _plugins(Bridges="Bridges.esp"))
    _look(plugin, gone, _with_village()[0])
    _look(plugin, (_snapshot("Bridges.esp", mods=("Bridges", "Village")), _plugins(Bridges="Bridges.esp")), gone[0])
    _look(plugin, _with_village(), gone[0])
    assert plugin.activeProblems() == [] and plugin._install_checks == {}

    # Merge: the same plugin again, now listed before MO2 announces the install.
    _look(plugin, _with_village(), _with_village()[0])
    assert plugin.activeProblems() == []


def test_an_update_that_adds_a_plugin_only_counts_patches_for_that_plugin(tmp_path, monkeypatch):
    from copy import deepcopy

    from test_patch_alerts import grass_patches

    event = grass_patches()
    extras = deepcopy(event["steps"][0]["groups"][0]["options"][1])
    extras.update(id="step:0/group:0/option:2", name="Extras patch", plugins=["GP - Extras.esp"],
                  masters=["Skyrim.esm", "GrassPatches.esp", "Village Extras.esp"], detects=["Village Extras.esp"])
    event["steps"][0]["groups"][0]["options"].append(extras)
    _module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_update_test", event=event)
    _look(plugin, _with_village())    # Village is there: its patch was a card already, not an alert
    updated = (_snapshot("Bridges.esp", "Village.esp", "Village Extras.esp", mods=("Bridges", "Village")),
               _plugins(Bridges="Bridges.esp", Village="Village.esp,Village Extras.esp"))
    _look(plugin, updated, _with_village()[0])
    assert plugin.activeProblems() == [100]
    text = plugin.fullDescription(100)
    assert "Extras patch: patch for Village;" in text and "Village patch" not in text


def test_a_patch_found_only_by_name_is_announced_once_the_new_mod_is_switched_on(tmp_path, monkeypatch):
    from test_patch_alerts import grass_patches

    named = grass_patches(only={"name": "Village Overhaul patch", "plugins": []})
    _module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_name_test", event=named, mods=("Village Overhaul",))
    _look(plugin, _base())
    # The new mod has no plugin and is still switched off: a name can only be matched against mods that are on.
    off = (_snapshot("Bridges.esp", mods=("Bridges",), disabled_mods=("Village Overhaul",)), _base()[1])
    _look(plugin, off, _base()[0])
    assert plugin.activeProblems() == [] and plugin._enable_watch == {"village overhaul": "Village Overhaul"}
    _look(plugin, off, off[0])
    assert plugin.activeProblems() == []
    on = (_snapshot("Bridges.esp", mods=("Bridges", "Village Overhaul")), _base()[1])
    _look(plugin, on, off[0])
    assert plugin.activeProblems() == [100]
    assert plugin.shortDescription(100) == "New patches available for Village Overhaul"
    assert "Village Overhaul patch: patch for Village Overhaul;" in plugin.fullDescription(100)
    assert plugin._enable_watch == {}


def _two_patch_event():
    from copy import deepcopy

    from test_patch_alerts import grass_patches

    event = grass_patches()
    options = event["steps"][0]["groups"][0]["options"]
    bridges = deepcopy(options[1])
    bridges.update(id="step:0/group:0/option:2", name="Bridges patch", plugins=["GP - Bridges.esp"],
                   masters=["Skyrim.esm", "GrassPatches.esp", "Bridges.esp"], detects=["Bridges.esp"])
    both = deepcopy(options[1])
    both.update(id="step:0/group:0/option:3", name="Village and Bridges patch", plugins=["GP - Both.esp"],
                masters=["Skyrim.esm", "GrassPatches.esp", "Village.esp", "Bridges.esp"], detects=[])
    options.extend([bridges, both])
    return event


def test_a_problem_keeps_its_key_while_other_problems_come_and_go(tmp_path, monkeypatch):
    _module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_keys_test", event=_two_patch_event(),
                                              mods=("Core",))
    core = (_snapshot(mods=("Core",)), _plugins(Core=""))
    village = (_snapshot("Village.esp", mods=("Core", "Village")), _plugins(Core="", Village="Village.esp"))
    both = (_snapshot("Village.esp", "Bridges.esp", mods=("Core", "Village", "Bridges")),
            _plugins(Core="", Village="Village.esp", Bridges="Bridges.esp"))
    _look(plugin, core)
    _look(plugin, village, core[0])
    assert plugin.activeProblems() == [100] and plugin.shortDescription(100) == "New patches available for Village"
    _look(plugin, both, village[0])
    # Bridges arrives: its own problem (its patch, and the one made for Village and Bridges together).
    assert plugin.activeProblems() == [100, 101]
    assert plugin.shortDescription(101) == "New patches available for Bridges"
    # Village's patch is installed: its problem goes, and Bridges' keeps key 101 instead of sliding down to 100.
    patched = (_snapshot("Village.esp", "Bridges.esp", "GP - Village.esp", mods=("Core", "Village", "Bridges")), both[1])
    _look(plugin, patched, both[0])
    assert plugin.activeProblems() == [101]
    assert plugin.shortDescription(101) == "New patches available for Bridges"
    assert plugin.hasGuidedFix(101) is True and plugin.hasGuidedFix(100) is False


def test_one_patch_for_two_mods_installed_together_is_one_problem(tmp_path, monkeypatch):
    _module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_merge_test", event=_two_patch_event(),
                                              mods=("Core",))
    core = (_snapshot(mods=("Core",)), _plugins(Core=""))
    both = (_snapshot("Village.esp", "Bridges.esp", mods=("Core", "Village", "Bridges")),
            _plugins(Core="", Village="Village.esp", Bridges="Bridges.esp"))
    _look(plugin, core)
    _look(plugin, both, core[0])
    assert plugin.activeProblems() == [100]
    assert plugin.shortDescription(100) == "New patches available for Bridges and Village"
    text = plugin.fullDescription(100)
    assert text.count("Village and Bridges patch: patch for Bridges and Village;") == 1   # one line for the shared patch
    assert "Village patch: patch for Bridges and Village" in text and "Bridges patch: patch for Bridges and Village" in text


def test_removing_a_mod_forgets_its_alerts_also_when_found_after_a_restart(tmp_path, monkeypatch):
    module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_forget_test")
    instance = _with_village()[0]["instance_id"]
    event = {
        "schema_version": 1, "event_id": "33333333-3333-4333-8333-333333333333",
        "started_at": "2026-10-20T12:00:00+00:00", "completed_at": "2026-10-20T12:01:00+00:00",
        "instance": {"id": instance, "label": "MO2 Test"}, "source": {"archive": "Village.7z", "reinstallation": False},
        "mod": {"name": "Village", "version": "1", "nexus_id": 2},
        "installer": {"name": "N", "version": "2", "upstream_commit": "unknown"}, "steps": []}
    with module.EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        store.ingest(event)
        store.add_patch_alerts(instance, "Village", event["event_id"], [("a", "b")])
    plugin._organizer.mod_list.allMods = lambda: []    # Village was removed while MO2 was closed
    monkeypatch.setattr(module._base, "stable_instance_id", lambda _path: instance)
    plugin._reconcile_current_mods()
    with module.EventStore(plugin._data_root / "fomod_logger.sqlite3") as store:
        assert store.patch_alerts(pending=False) == []
        assert [e["removed_at"] is not None for e in store.history() if e["mod"]["name"] == "Village"] == [True]


def test_an_install_that_fails_checking_never_breaks_the_plugin(tmp_path, monkeypatch, capsys):
    _module, plugin, _folders = _alert_plugin(tmp_path, monkeypatch, "alert_robust_test")
    _look(plugin, _base())

    def gone():
        raise RuntimeError("MO2 went away")

    plugin._organizer.modList = gone
    _look(plugin, _with_village(), _base()[0])
    assert "new-patch check failed: MO2 went away" in capsys.readouterr().err
    assert plugin.activeProblems() == []
    plugin._active = False
    plugin._on_mod_installed(types.SimpleNamespace(name=lambda: "Other"))    # inactive: ignored
