"""Live end-to-end runner for Modlist Companion in its portable test MO2. Test-only.

    python tests/live/run_live.py run --out <results folder> [--close-test-mo2] [--keep-open]
                                      [--backend bridge|driver]
                                      [--edition full|plus] [--use-installed] [--only k,l]
    python tests/live/run_live.py send <command> [json arguments]   (needs a --keep-open run)
    python tests/live/run_live.py finish --out <results folder>     (closes MO2 and cleans up)
    python tests/live/run_live.py recover --out <results folder>    (undo a run that was cut short)

What a run does, in order:
  1. looks only for the TEST MO2 (matched by its executable's path; other MO2s may be running, one per instance
     folder, and are never touched) and closes it gracefully when --close-test-mo2 is given;
  2. backs up the profile, ModOrganizer.ini and the Companion's data folder;
  3. writes the fixture archives and copies driver_plugin.py into the test MO2's
     plugins folder (with --backend bridge it uses the ModLabs bridge already deployed there);
  4. starts the test MO2 (never a game) and drives it;
  5. closes MO2, removes the driver, the test mods and everything it
     added, and restores the backed-up files (checked by comparing hashes).

Two backends, the same scenarios:
  bridge (default)  the ModLabs bridge (tests/live/bridge_client.py) starts installs, switches mods and
                    plugins on and off and reads the mod and plugin lists, the way ModLabs itself would.
                    The driver still ticks the FOMOD dialog's choices, reads the Companion window,
                    takes screenshots and removes the test mods (the bridge never removes mods).
  driver            everything through the driver plugin, as before.

The bridge is a test helper and ModLabs' own: the ModLabs Manager deploys it into the test MO2 and keeps it
upgraded, and this runner only uses it (protocol 3 or later; never copied, pinned or removed here). It is never
part of the product or its packaging.

Two editions, two test MO2s (--edition):
  full (default)  the full edition, in test-env\\MO2-Skyrim-Logger, with its native installer (already installed there).
  plus            the FOMOD Plus edition, in test-env\\MO2-Skyrim-Plus, which has FOMOD Plus and none of our files. The
                  run copies the Plus plugin and the shared support folder from this source tree into it, installs
                  the fixtures through the driver plugin (driver backend only; no bridge), and takes everything
                  out again. --plus-installer real has FOMOD Plus's own installer do the installs and drives its
                  dialog; FOMOD Plus 1.20.0 aborts MO2 at the end of such an install (also without our plugin: see
                  --without-companion), so the default, emulated, lets MO2's own FOMOD installer install with FOMOD
                  Plus switched off for the moment, and then saves on the mod the record FOMOD Plus would have
                  saved, through the same plugin-setting API FOMOD Plus uses. Scenarios: plus-setup, p1 (a FOMOD Plus install -> red missed-patch card from
                  the rebuilt event), p2 (a reinstall with a changed pick -> pick markers), p3 (the scan of mods
                  installed before), p4 (FOMOD Plus switched off -> the MO2 problem), plus-cleanup.

--use-installed: test the files ALREADY INSTALLED in the test MO2 (from a release package extracted there), not
files deployed from this source tree. The run first checks every file in the package's manifest in the MO2 root
(fomod_logger-manifest.json or fomod_logger_plus-manifest.json) against its sha256, refuses if the other edition's
entry file is there too, deploys nothing of the product, and leaves the installed files in place afterwards (it still
removes the driver and everything else it added).

Mods without an installer: scenario k (full edition) installs plain zips (the driver answers the simple installer's
"Name / OK" box) and checks the plain record, the file lookup, search, compare, the missing-master card, an installer's
record replacing it and removal; scenario l checks the hint, a scan that is cancelled, the scan of every mod with no
record (two mods are dropped into the mods folder before MO2 starts: a small one and one with 12,000 files) and that
nothing is left to scan; p5 is the same for the Plus edition. --only k,l runs a few scenarios while developing.

List history: scenario m (full edition) and p6 (Plus) switch a plugin and a mod off and move a mod, check that each is an
entry, close MO2, edit the profile's plugins.txt while it is closed (a plugin off, another on; the run restores the
profile afterwards), start MO2 again and check that the history found both edits and marked them as found at start, and
that the window lists, filters, searches and exports them. No program is started through the test MO2 (the shared rules
forbid it while a game runs); that the callbacks for programs are registered is checked, the rest is in the unit tests.

Profile switch: scenario n (full) and p7 (Plus) switch MO2 to the temporary second profile and back, twice, through MO2's
own profile box, and check that the history holds the four switches and no plugin or mod entries (the second profile
differs from the first, so a look before the new profile's lists were in place would show).

Only the two test MO2s (test-env\\MO2-Skyrim-Logger and ...\\MO2-Skyrim-Plus) are ever touched.
All automation is in-process Qt through the driver plugin and the bridge; nothing clicks the screen.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import traceback
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
# The two test MO2s: where each edition is tried, and the Companion data folder it keeps under plugins/data.
EDITIONS = {"full": {"mo2": "MO2-Skyrim-Logger", "data": "fomod_logger"},
            "plus": {"mo2": "MO2-Skyrim-Plus", "data": "fomod_logger_plus"}}
EDITION = "full"
USE_INSTALLED = False                    # --use-installed: see the docstring
PLUS_INSTALLER = "emulated"              # or "real": see the docstring
MO2 = SOURCE.parents[1] / "MO2-Skyrim-Logger"
MO2_EXE = MO2 / "ModOrganizer.exe"
PLUGINS = MO2 / "plugins"
DRIVER_DST = PLUGINS / "fomod_companion_driver.py"
DRIVER_DATA = PLUGINS / "data" / "fomod_companion_driver"
LOGGER_DATA = PLUGINS / "data" / "fomod_logger"
PROFILE_NAME = "Logger QA"
PROFILE = MO2 / "profiles" / PROFILE_NAME
# Scenario j needs a second profile. It is made from Logger QA before MO2 starts and removed with the run.
COMPARE_PROFILE_NAME = "Logger QA Compare"
COMPARE_PROFILE = MO2 / "profiles" / COMPARE_PROFILE_NAME
COMPARE_DISABLED = "Lanterns"            # a mod of the test MO2, switched off in the second profile
COMPARE_STATE: dict = {}                 # what make_compare_profile wrote: the modlist's hash, for "never written"
INI = MO2 / "ModOrganizer.ini"
BRIDGE_DST = PLUGINS / "ModLabsBridge.py"
BRIDGE_CFG = PLUGINS / "ModLabsBridge.json"
# The FOMOD Plus edition is copied into its test MO2 for a run (and removed again): its entry file and the shared
# support folder, with the SQLite runtime MO2's Python needs (taken from the full edition's test MO2).
PLUS_PLUGIN_SOURCE = SOURCE / "python" / "fomod_logger_plus_plugin.py"
SUPPORT_SOURCE = SOURCE / "python" / "fomod_logger_support"
PLUS_PLUGIN_DST = PLUGINS / "fomod_logger_plus.py"
SUPPORT_DST = PLUGINS / "fomod_logger_support"
SQLITE_SOURCE = SOURCE.parents[1] / "MO2-Skyrim-Logger" / "plugins" / "fomod_logger_support" / "native" / "sqlite3.dll"
# Files in the test MO2's root that FOMOD Plus rewrites when it installs (its window's size, its choices database).
PLUS_ROOT_FILES = ("fomod-plus-settings.ini", "fomod.db")
# The ModLabs bridge is ModLabs' own: ModLabs deploys it into the test MO2 and keeps it upgraded (its register;
# `python -m modlabs_core.bridge sync` while that MO2 is closed). This runner only uses it: it never copies, pins,
# patches or removes it. Its build changes on every upgrade, so the runner asks for a protocol, not a hash.
BRIDGE_PROTOCOL_MIN = 3

sys.path.insert(0, str(HERE))
import fixtures  # noqa: E402
from bridge_client import BridgeClient, BridgeError  # noqa: E402


def use_edition(name: str) -> None:
    """Point the runner at the test MO2 of one edition (before anything else reads these paths)."""

    global EDITION, MO2, MO2_EXE, PLUGINS, DRIVER_DST, DRIVER_DATA, LOGGER_DATA, PROFILE, COMPARE_PROFILE, INI
    global BRIDGE_DST, BRIDGE_CFG, PLUS_PLUGIN_DST, SUPPORT_DST
    EDITION = name
    MO2 = SOURCE.parents[1] / EDITIONS[name]["mo2"]
    MO2_EXE = MO2 / "ModOrganizer.exe"
    PLUGINS = MO2 / "plugins"
    DRIVER_DST = PLUGINS / "fomod_companion_driver.py"
    DRIVER_DATA = PLUGINS / "data" / "fomod_companion_driver"
    LOGGER_DATA = PLUGINS / "data" / EDITIONS[name]["data"]
    PROFILE = MO2 / "profiles" / PROFILE_NAME
    COMPARE_PROFILE = MO2 / "profiles" / COMPARE_PROFILE_NAME
    INI = MO2 / "ModOrganizer.ini"
    BRIDGE_DST = PLUGINS / "ModLabsBridge.py"
    BRIDGE_CFG = PLUGINS / "ModLabsBridge.json"
    PLUS_PLUGIN_DST = PLUGINS / "fomod_logger_plus.py"
    SUPPORT_DST = PLUGINS / "fomod_logger_support"


# ---------------------------------------------------------------- processes

def mo2_processes() -> list[tuple[int, str]]:
    """(pid, exe path) of the running test MO2, matched by its executable's path. Other MO2s (one per instance folder
    may run at once) are never looked at, waited for, closed or touched."""

    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ModOrganizer.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    found = []
    kernel = ctypes.windll.kernel32
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2 or parts[0].strip('"').lower() != "modorganizer.exe":
            continue
        pid = int(parts[1].strip('"'))
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        path = ""
        if handle:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                path = buf.value
            kernel.CloseHandle(handle)
        if _is_test_mo2(path):
            found.append((pid, path))
    return found


def _is_test_mo2(path: str) -> bool:
    return bool(path) and os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(MO2_EXE))


def post_close(pid: int) -> int:
    """WM_CLOSE to every visible top-level window of ``pid``; returns how many."""

    user32 = ctypes.windll.user32
    sent = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            sent.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return len(sent)


def wait_exit(pid: int, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if pid not in [p for p, _ in mo2_processes()]:
            return True
        time.sleep(0.5)
    return False


FORCED_ENDS: list[int] = []     # test MO2s that had to be killed this run: reported in the results, and the run fails


def close_pid(pid: int, log) -> str:
    """Graceful close (the window's own close, asked twice); a kill only as the last resort after 70 seconds, and then
    reported loudly: a killed MO2 can leave RootBuilder's copy-mode files in the Stock Game (the restore step lists any
    new game-folder entry as a problem, and the run fails)."""

    log(f"closing MO2 pid {pid} gracefully")
    post_close(pid)
    if wait_exit(pid, 60):
        return "closed gracefully"
    post_close(pid)
    if wait_exit(pid, 10):
        return "closed gracefully (second request)"
    log(f"WARNING: MO2 pid {pid} did not close after 70 s; ending it (last resort). Check the Stock Game root for "
        "leftover RootBuilder files.")
    FORCED_ENDS.append(pid)
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    wait_exit(pid, 10)
    return "had to be ended after 70 s"


def ensure_no_other_mo2(close_test: bool, log) -> dict:
    running = mo2_processes()
    info = {"was_running": [{"pid": p, "path": path} for p, path in running], "closed": None}
    if running and not close_test:
        raise SystemExit("The test MO2 is running; rerun with --close-test-mo2 (standing permission) "
                         "or close it yourself.")
    for pid, _path in running:
        info["closed"] = close_pid(pid, log)
    if mo2_processes():
        raise SystemExit("The test MO2 is still running; not starting.")
    return info


# ---------------------------------------------------------------- backup and restore

def file_hashes(root: Path) -> dict[str, str]:
    out = {}
    if not root.exists():
        return out
    if root.is_file():
        return {root.name: hashlib.sha256(root.read_bytes()).hexdigest()}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _inside_mo2(path: Path) -> Path:
    resolved = path.resolve()
    if MO2.resolve() not in resolved.parents:
        raise SystemExit(f"refusing to touch {resolved}: outside the test MO2")
    return resolved


def make_backup(out: Path, log) -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = out / f"backup-{stamp}"
    backup.mkdir(parents=True)
    shutil.copytree(PROFILE, backup / "profile")
    shutil.copy2(INI, backup / "ModOrganizer.ini")
    # The Companion's data folder (the Plus edition's does not exist before its first run), and the files in the
    # root of the Plus test MO2 that FOMOD Plus rewrites when it installs.
    data_existed = LOGGER_DATA.exists()
    if data_existed:
        shutil.copytree(LOGGER_DATA, backup / "fomod_logger_data")
    else:
        (backup / "fomod_logger_data").mkdir()
    root_files = {}
    if EDITION == "plus":
        (backup / "root_files").mkdir()
        for name in PLUS_ROOT_FILES:
            source = MO2 / name
            root_files[name] = file_hashes(source).get(name) if source.is_file() else None
            if source.is_file():
                shutil.copy2(source, backup / "root_files" / name)
    manifest = {
        "edition": EDITION,
        "use_installed": USE_INSTALLED,
        "logger_data_existed": data_existed,
        "root_file_hashes": root_files,
        "logs_entries": sorted(p.name for p in (MO2 / "logs").iterdir()) if (MO2 / "logs").is_dir() else [],
        "backup": str(backup),
        "profile_hashes": file_hashes(PROFILE),
        "ini_hash": file_hashes(INI),
        "logger_data_hashes": file_hashes(LOGGER_DATA),
        "mods": sorted(p.name for p in (MO2 / "mods").iterdir()),
        "plugins_entries": sorted(p.name for p in PLUGINS.iterdir()),
        "plugins_data_entries": sorted(p.name for p in (PLUGINS / "data").iterdir()),
        "overwrite_entries": sorted(p.name for p in (MO2 / "overwrite").iterdir()),
        "downloads_entries": sorted(p.name for p in (MO2 / "downloads").iterdir()),
        "root_entries": sorted(p.name for p in MO2.iterdir()),
        "profiles_entries": sorted(p.name for p in (MO2 / "profiles").iterdir()),
    }
    (out / "run-state.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    log(f"backed up profile, ini and Companion data to {backup}")
    return manifest


def make_compare_profile(log) -> None:
    """Scenario j's second profile: a copy of Logger QA (MO2 is closed) in which one test mod is switched off, and which
    already lists the LiveTest Layout mod scenario j installs, enabled. Removed again by ``restore``."""

    target = _inside_mo2(COMPARE_PROFILE)
    if target.exists():
        raise SystemExit(f"{target} already exists; run 'recover' first")
    shutil.copytree(PROFILE, target)
    path = target / "modlist.txt"
    text = path.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    if f"+{COMPARE_DISABLED}" not in lines:
        raise SystemExit(f"the Logger QA profile does not enable {COMPARE_DISABLED}")
    lines = [f"-{COMPARE_DISABLED}" if line == f"+{COMPARE_DISABLED}" else line for line in lines]
    lines.insert(1, f"+{fixtures.LAYOUT}")
    path.write_bytes(eol.join(lines).encode("utf-8"))
    COMPARE_STATE["modlist_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
    log(f"made the temporary profile {COMPARE_PROFILE_NAME} ({COMPARE_DISABLED} off, {fixtures.LAYOUT} on)")


def _replace_dir(src: Path, dst: Path) -> None:
    dst = _inside_mo2(dst)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def restore(manifest: dict, log) -> dict:
    """Put everything back and report whether it matches the backup."""

    backup = Path(manifest["backup"])
    problems = []
    # Driver and its data.
    if DRIVER_DST.exists():
        DRIVER_DST.unlink()
    if DRIVER_DATA.exists():
        shutil.rmtree(_inside_mo2(DRIVER_DATA))
    # The ModLabs bridge stays: it is ModLabs' (deployed and upgraded by ModLabs). Only what it made during the run goes.
    for folder, key in ((MO2, "root_entries"), (PLUGINS, "plugins_entries"), (PLUGINS / "data", "plugins_data_entries")):
        for entry in folder.iterdir():
            if (entry.name.casefold().startswith("modlabs") and entry.name not in manifest.get(key, [])
                    and entry not in (BRIDGE_DST, BRIDGE_CFG)):
                log(f"removing {entry} (made by the bridge)")
                if entry.is_dir():
                    shutil.rmtree(_inside_mo2(entry))
                else:
                    entry.unlink()
    # The Plus edition (a test copy of this source tree) and the cache MO2 made for it.
    if manifest.get("edition") == "plus" and not manifest.get("use_installed"):
        if PLUS_PLUGIN_DST.exists():
            PLUS_PLUGIN_DST.unlink()
        if SUPPORT_DST.exists() and "fomod_logger_support" not in manifest["plugins_entries"]:
            shutil.rmtree(_inside_mo2(SUPPORT_DST))
    cache = PLUGINS / "__pycache__"
    if cache.exists() and "__pycache__" not in manifest["plugins_entries"]:
        shutil.rmtree(_inside_mo2(cache))
    elif cache.exists():
        for stale in list(cache.glob("fomod_companion_driver*")) + list(cache.glob("fomod_logger_plus*")):
            stale.unlink()
    # The temporary profile of scenario j.
    if COMPARE_PROFILE.exists() and COMPARE_PROFILE_NAME not in manifest.get("profiles_entries", []):
        log(f"removing the temporary profile {COMPARE_PROFILE_NAME}")
        shutil.rmtree(_inside_mo2(COMPARE_PROFILE))
    # Test mods: anything in mods/ that was not there before.
    for entry in (MO2 / "mods").iterdir():
        if entry.name not in manifest["mods"]:
            log(f"removing leftover mod folder {entry.name}")
            shutil.rmtree(_inside_mo2(entry))
    _replace_dir(backup / "profile", PROFILE)
    shutil.copy2(backup / "ModOrganizer.ini", INI)
    if manifest.get("logger_data_existed", True):
        _replace_dir(backup / "fomod_logger_data", LOGGER_DATA)
    elif LOGGER_DATA.exists():
        shutil.rmtree(_inside_mo2(LOGGER_DATA))
    # FOMOD Plus's own files in the root, and the logs this run made.
    for name, wanted in (manifest.get("root_file_hashes") or {}).items():
        target = MO2 / name
        if wanted is None:
            if target.exists():
                target.unlink()
        else:
            shutil.copy2(backup / "root_files" / name, target)
            if file_hashes(target).get(name) != wanted:
                problems.append(f"{name} differs from the backup")
    logs = MO2 / "logs"
    if manifest.get("edition") == "plus" and logs.is_dir():
        for entry in logs.iterdir():
            if entry.name not in manifest.get("logs_entries", []):
                log(f"removing {entry} (a log this run made)")
                if entry.is_dir():
                    shutil.rmtree(_inside_mo2(entry))
                else:
                    entry.unlink()
    # Verify.
    for label, current, wanted in (("profile", file_hashes(PROFILE), manifest["profile_hashes"]),
                                   ("ini", file_hashes(INI), manifest["ini_hash"]),
                                   ("companion data", file_hashes(LOGGER_DATA), manifest["logger_data_hashes"])):
        if current != wanted:
            problems.append(f"{label} differs from the backup")
    if manifest.get("edition") == "plus" and logs.is_dir():
        extra = sorted(set(p.name for p in logs.iterdir()) - set(manifest.get("logs_entries", [])))
        if extra:
            problems.append(f"logs folder has extra entries {extra}")
    for label, folder, key in (("mods", MO2 / "mods", "mods"), ("plugins", PLUGINS, "plugins_entries"),
                               ("plugins/data", PLUGINS / "data", "plugins_data_entries"),
                               ("overwrite", MO2 / "overwrite", "overwrite_entries"),
                               ("downloads", MO2 / "downloads", "downloads_entries"),
                               ("profiles", MO2 / "profiles", "profiles_entries")):
        if key not in manifest:
            continue          # a run state written before this folder was listed
        now = sorted(p.name for p in folder.iterdir())
        if now != manifest[key]:
            problems.append(f"{label} folder listing differs: extra {sorted(set(now) - set(manifest[key]))}, "
                            f"missing {sorted(set(manifest[key]) - set(now))}")
    root_now = sorted(p.name for p in MO2.iterdir())
    root_before = manifest.get("root_entries", root_now)
    return {"restored": not problems, "problems": problems,
            "root_folder_extra_entries": sorted(set(root_now) - set(root_before)),
            "root_folder_missing_entries": sorted(set(root_before) - set(root_now))}


# ---------------------------------------------------------------- the FOMOD Plus edition (test copy of this source tree)

def deploy_plus(log) -> dict:
    """Copy the Plus plugin and the shared support folder from this source tree into the Plus test MO2 (MO2 must be
    closed), with the SQLite runtime. Nothing of ours may be there already; ``restore`` takes it all out again."""

    for path in (PLUS_PLUGIN_DST, SUPPORT_DST):
        if path.exists():
            raise SystemExit(f"{path} already exists; run 'recover' first (or it is a real install: not overwriting it).")
    lock = json.loads((SOURCE / "packaging" / "sqlite-runtime.json").read_text(encoding="utf-8"))
    sqlite_bytes = SQLITE_SOURCE.read_bytes()
    if hashlib.sha256(sqlite_bytes).hexdigest() != lock["file_sha256"]:
        raise SystemExit(f"{SQLITE_SOURCE} is not the pinned sqlite3.dll; not deploying.")
    _inside_mo2(PLUS_PLUGIN_DST)
    shutil.copy2(PLUS_PLUGIN_SOURCE, PLUS_PLUGIN_DST)
    shutil.copytree(SUPPORT_SOURCE, SUPPORT_DST, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (SUPPORT_DST / "native").mkdir(exist_ok=True)
    (SUPPORT_DST / "native" / "sqlite3.dll").write_bytes(sqlite_bytes)
    hashes = {**file_hashes(PLUS_PLUGIN_DST), **{f"fomod_logger_support/{k}": v for k, v in file_hashes(SUPPORT_DST).items()}}
    log(f"deployed the Plus edition ({len(hashes)} files) into {PLUGINS}")
    return hashes


def check_installed(log) -> dict:
    """--use-installed: every file the package's manifest names is in the test MO2 with the manifest's sha256, and the
    other edition's entry file is not there. Nothing is changed. Raises SystemExit otherwise."""

    name = "fomod_logger_plus-manifest.json" if EDITION == "plus" else "fomod_logger-manifest.json"
    manifest_path = MO2 / name
    if not manifest_path.is_file():
        raise SystemExit(f"--use-installed: {manifest_path} is missing; the package is not installed.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    for relative, wanted in sorted(manifest["files"].items()):
        target = MO2 / relative
        if not target.is_file():
            problems.append(f"missing {relative}")
        elif hashlib.sha256(target.read_bytes()).hexdigest() != wanted:
            problems.append(f"differs {relative}")
    other = (PLUGINS / "fomod_logger.py", PLUGINS / "fomod_logger.dll") if EDITION == "plus" else (PLUGINS / "fomod_logger_plus.py",)
    problems += [f"the other edition is installed too: {path.name}" for path in other if path.exists()]
    if problems:
        raise SystemExit(f"--use-installed: the installed files do not match {name}: {problems[:8]}")
    log(f"--use-installed: {len(manifest['files'])} installed files match {name} "
        f"(version {manifest.get('version')}, commit {str(manifest.get('repository_commit'))[:12]})")
    return {"manifest": name, "files": len(manifest["files"]), "version": manifest.get("version"),
            "repository_commit": manifest.get("repository_commit")}


# ---------------------------------------------------------------- the bridge (test helper)

def find_bridge(log) -> dict:
    """The bridge ModLabs deployed in the test MO2: its pipe and token from the json beside it. Nothing is written.
    The token is only ever held in memory; it is never logged or put in the results."""

    if not BRIDGE_DST.exists() or not BRIDGE_CFG.exists():
        raise SystemExit(f"no ModLabs bridge in {PLUGINS} (ModLabsBridge.py and .json): ask the ModLabs Manager to deploy "
                         "it to this test MO2, or run with --backend driver.")
    config = json.loads(BRIDGE_CFG.read_text(encoding="utf-8"))
    if not config.get("pipe_name") or not config.get("token"):
        raise SystemExit(f"{BRIDGE_CFG} has no pipe name or token.")
    if not config.get("allow_installer"):
        raise SystemExit(f"{BRIDGE_CFG} doesn't allow installs (allow_installer); the bridge scenarios need them.")
    log(f"using the ModLabs bridge in the test MO2 (build {str(config.get('build', '?'))[:16]}..., "
        f"pipe {config['pipe_name']})")
    return {"pipe_name": config["pipe_name"], "token": config["token"], "build": config.get("build")}


def connect_bridge(info: dict, process, log, seconds: float = 180) -> tuple[BridgeClient, dict]:
    """Wait for the bridge to answer, then check it speaks a protocol this runner knows, in the right MO2 and profile."""

    client = BridgeClient(info["pipe_name"], info["token"], str(MO2), PROFILE_NAME)
    began = time.perf_counter()
    end = began + seconds
    last = None
    ping = None
    while time.perf_counter() < end and ping is None:
        if process.poll() is not None:
            raise RuntimeError(f"MO2 exited early with code {process.returncode}")
        try:
            ping = client.ping()
        except BridgeError as exc:
            last = exc
            time.sleep(0.5)
    if ping is None:
        raise RuntimeError(f"the bridge never answered: {last}")
    answered = round(time.perf_counter() - began, 1)
    problems = []
    if not isinstance(ping.get("protocol"), int) or ping["protocol"] < BRIDGE_PROTOCOL_MIN:
        problems.append(f"protocol {ping.get('protocol')} (this runner needs {BRIDGE_PROTOCOL_MIN} or later)")
    if os.path.normcase(os.path.normpath(str(ping.get("base_path")))) != os.path.normcase(os.path.normpath(str(MO2))):
        problems.append(f"base path {ping.get('base_path')}")
    if str(ping.get("profile", "")).casefold() != PROFILE_NAME.casefold():
        problems.append(f"profile {ping.get('profile')}")
    if ping.get("pid") != process.pid:
        problems.append(f"pid {ping.get('pid')} is not the MO2 we started ({process.pid})")
    if problems:
        raise RuntimeError("not driving this bridge: " + "; ".join(problems))
    client.base_path, client.profile = str(ping["base_path"]), str(ping["profile"])
    ready_after = None
    while time.perf_counter() < end:
        status = client.status()
        if status["ui_ready"] and not status["modal"] and not status["jobs"]:
            ready_after = round(time.perf_counter() - began, 1)
            break
        time.sleep(0.3)
    else:
        raise RuntimeError("the bridge says MO2's main window never became ready")
    log(f"bridge answered after {answered} s (protocol {ping['protocol']}, MO2 {ping['mo2_version']}); "
        f"main window ready {ready_after} s after the start")
    return client, {"ping": ping, "first_answer_s": answered, "ui_ready_s": ready_after}


# ---------------------------------------------------------------- driver client

class Client:
    def __init__(self) -> None:
        self.inbox = DRIVER_DATA / "inbox"
        self.outbox = DRIVER_DATA / "outbox"
        self.counter = 0
        self.process: subprocess.Popen | None = None      # the MO2 being driven: a command is not waited for after it exits

    def prepare(self) -> None:
        for sub in (self.inbox, self.outbox):
            sub.mkdir(parents=True, exist_ok=True)
            for old in sub.glob("*"):
                old.unlink()

    def wait_ready(self, process: subprocess.Popen | None, seconds: float = 240) -> dict:
        ready = self.outbox / "_ready.json"
        end = time.time() + seconds
        while time.time() < end:
            if ready.exists():
                return json.loads(ready.read_text(encoding="utf-8"))
            if process is not None and process.poll() is not None:
                raise RuntimeError(f"MO2 exited early with code {process.returncode}")
            time.sleep(0.5)
        raise RuntimeError("MO2 never reported ready (see the driver log)")

    def send(self, cmd: str, timeout: float = 120, **args) -> dict:
        self.counter += 1
        ident = f"{int(time.time() * 1000) % 10_000_000_000:010d}-{self.counter:03d}-{cmd}"
        tmp = self.inbox / (ident + ".tmp")
        tmp.write_text(json.dumps({"cmd": cmd, **args}), encoding="utf-8")
        os.replace(tmp, self.inbox / (ident + ".json"))
        result_path = self.outbox / (ident + ".json")
        end = time.time() + timeout
        while time.time() < end:
            if self.process is not None and self.process.poll() is not None and not result_path.exists():
                raise RuntimeError(f"MO2 exited (code {self.process.returncode}) while {cmd} was waiting for an answer")
            if result_path.exists():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except (ValueError, OSError):  # still being written, or briefly locked
                    time.sleep(0.05)
                    continue
                for _try in range(20):
                    try:
                        result_path.unlink()
                        break
                    except OSError:
                        time.sleep(0.05)
                if not result.get("ok"):
                    raise RuntimeError(f"{cmd} failed: {result.get('error')}\n{result.get('trace', '')}")
                return result
            time.sleep(0.03)
        raise TimeoutError(f"no answer to {cmd} within {timeout} s")


# ---------------------------------------------------------------- scenario helpers

class Scenario:
    def __init__(self, ident: str, title: str) -> None:
        self.id, self.title = ident, title
        self.checks: list[dict] = []
        self.findings: list[dict] = []  # behaviour worth reporting that is not a pass/fail expectation
        self.timings: dict = {}
        self.notes: dict = {}
        self.error = ""

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def finding(self, name: str, detail: str = "") -> None:
        self.findings.append({"name": name, "detail": detail})

    @property
    def passed(self) -> bool:
        return not self.error and bool(self.checks) and all(c["ok"] for c in self.checks)

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "passed": self.passed, "error": self.error,
                "checks": self.checks, "findings": self.findings, "timings": self.timings, "notes": self.notes}


def cards(state: dict, section: str) -> list[dict]:
    return [c for c in state["sections"][section]["cards"] if "title" in c]


def find_card(state: dict, section: str, title: str, mod: str, text: str = "") -> dict | None:
    """A card in a side section: its title, the mod it belongs to and some line text."""

    for card in cards(state, section):
        lines = " ".join(line["text"] for line in card["lines"])
        if title.lower() in card["title"].lower() and mod.lower() in lines.lower() and text.lower() in lines.lower():
            return card
    return None


def tree_row(state: dict, mod: str) -> dict | None:
    for row in state["tree"]:
        if row["text"][0] == mod:
            return row
    return None


def option_rows(row: dict) -> list[tuple[str, str]]:
    """(label, status) for every option row below an install row."""

    found = []

    def walk(node):
        for child in node.get("children", []):
            text = child["text"]
            if text[0][:1] in ("✓", "○"):
                found.append((text[0], text[1]))
            walk(child)
    walk(row)
    return found


def reconnect_bridge(client: BridgeClient, process, log, seconds: float = 180) -> dict:
    """After MO2 was started again: wait until the bridge (same pipe and token, the deployed file stays) answers from the
    new process and MO2's main window is ready."""

    began = time.perf_counter()
    end = began + seconds
    last = None
    ping = None
    while time.perf_counter() < end and ping is None:
        if process.poll() is not None:
            raise RuntimeError(f"MO2 exited early with code {process.returncode}")
        try:
            ping = client.ping()
        except BridgeError as exc:
            last = exc
            time.sleep(0.5)
    if ping is None:
        raise RuntimeError(f"the bridge never answered after the restart: {last}")
    if ping.get("pid") != process.pid:
        raise RuntimeError(f"the bridge answered from pid {ping.get('pid')}, not the restarted MO2 ({process.pid})")
    while time.perf_counter() < end:
        status = client.status()
        if status["ui_ready"] and not status["modal"] and not status["jobs"]:
            log(f"bridge answered again after {time.perf_counter() - began:.1f} s")
            return ping
        time.sleep(0.3)
    raise RuntimeError("the bridge says MO2's main window never became ready after the restart")


class Run:
    def __init__(self, client: Client, out: Path, archives: dict, log, bridge: BridgeClient | None = None,
                 host: dict | None = None) -> None:
        self.c, self.out, self.archives, self.log = client, out, archives, log
        self.b = bridge
        self.host = host if host is not None else {}   # {"process": the running MO2}, replaced by restart_mo2
        self.shots = out / "screenshots"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.scenarios: list[Scenario] = []
        self.updates: list[dict] = []   # every timed window update

    @property
    def via_bridge(self) -> bool:
        return self.b is not None

    def restart_mo2(self, between=None) -> float:
        """Close the test MO2 the way a user does and start it again (the driver and the bridge stay deployed).
        ``between`` is called while MO2 is closed (a test of what happens to changes made then). Returns the seconds
        it took until both answer."""

        began = time.perf_counter()
        shutdown(self.c, self.host["process"], self.log)
        if between is not None:
            between()
        self.c.prepare()
        process = subprocess.Popen([str(MO2_EXE), "--multiple"], cwd=str(MO2))
        self.host["process"] = process
        self.c.process = process
        self.c.wait_ready(process)
        if self.b is not None:
            reconnect_bridge(self.b, process, self.log)
        return round(time.perf_counter() - began, 1)

    def window(self, expand: bool = True, wait: float = 0.0) -> dict:
        return self.c.send("window_state", expand=expand, wait=wait)["data"]

    def await_window(self, predicate, timeout: float = 6.0) -> tuple[dict, float]:
        start = time.perf_counter()
        while True:
            state = self.window(expand=True)
            if predicate(state) or time.perf_counter() - start > timeout:
                return state, round((time.perf_counter() - start) * 1000)
            time.sleep(0.15)

    def shot(self, name: str, target: str = "window", expand: bool = True) -> str:
        path = self.shots / f"{name}.png"
        self.c.send("screenshot", target=target, path=str(path), expand=expand)
        return str(path)

    def record_update(self, scenario: Scenario, label: str, measured: dict) -> None:
        entry = {"scenario": scenario.id, "update": label,
                 "changed": measured["changed"], "first_change_ms": measured.get("first_change_ms"),
                 "action_ms": measured.get("action_ms"),
                 "first_change_after_action_ms": measured.get("first_change_after_action_ms"),
                 "last_change_ms": measured.get("last_change_ms"), "refreshes": measured.get("refreshes"),
                 "load_ms": measured.get("loads_ms"), "render_ms": measured.get("renders_ms"),
                 "signals": [f"{e['kind']}@{e['ms']}" for e in measured.get("signals") or []]}
        if (measured.get("value") or {}).get("job"):
            entry["bridge_job"] = measured["value"]["job"]
        self.updates.append(entry)
        scenario.timings[label] = entry

    def settle(self, seconds: float = 1.0) -> None:
        """Let any window update that is still on its way (a debounce, a queued redraw) land before measuring."""

        self.window(expand=False, wait=seconds)

    @staticmethod
    def signals_text(measured: dict) -> str:
        signals = measured.get("signals") or []
        return ", ".join(f"{e['kind']} at {e['ms']} ms" for e in signals) or "none"

    # ----- reading MO2: the bridge's snapshot when there is one, the driver otherwise -----

    def mods(self) -> dict:
        """{mod name: details} for every mod MO2 lists."""

        if self.via_bridge:
            return {m["name"]: m for m in self.b.snapshot()["mods"]}
        return self.c.send("state")["data"]["mods"]

    def plugin_info(self, plugin: str) -> dict | None:
        """{state, masters, origin, ...} for one plugin, or None when MO2 does not list it. The bridge's snapshot
        has no masters, so those always come from the driver."""

        from_driver = self.c.send("state", plugins=[plugin])["data"]["plugins"].get(plugin)
        if not self.via_bridge:
            return from_driver
        row = next((r for r in self.b.snapshot()["plugins"] if r["name"].casefold() == plugin.casefold()), None)
        if row is None:
            return None
        return {"state": "missing" if row["missing"] else "active" if row["active"] else "inactive",
                "masters": (from_driver or {}).get("masters", []), "origin": row["origin"],
                "load_order": row["load_order"], "priority": row["priority"]}

    def wait_plugins(self, names, timeout: float = 8.0) -> bool:
        names = list(names)
        if not self.via_bridge:
            return bool(self.c.send("wait_plugins", names=names, timeout=timeout)["data"]["listed"])
        end = time.perf_counter() + timeout
        while True:
            have = {r["name"].casefold() for r in self.b.snapshot()["plugins"]}
            if all(n.casefold() in have for n in names):
                return True
            if time.perf_counter() > end:
                return False
            time.sleep(0.1)

    # ----- changing MO2, and timing the Companion window while it happens -----

    def watch(self, action, timeout: float = 4.0, settle: float = 0.7) -> dict:
        """Run ``action`` (a bridge call) while the driver watches the open Companion window and MO2's change
        signals, all stamped with the wall clock. The result has the same shape the driver's own ``measure`` gives,
        so both backends feed the same checks: times are in ms from the start of the call."""

        self.c.send("watch_start")
        t0 = time.perf_counter()
        value = action()
        t_action = time.perf_counter()
        end = t_action + timeout
        while True:
            changes = self.c.send("watch_peek")["data"]["changes"]
            if changes and time.perf_counter() - changes[-1] >= settle:
                break
            if time.perf_counter() >= end:
                break
            time.sleep(0.1)
        report = self.c.send("watch_stop")["data"]
        changes = report["changes"]
        first, last = (changes[0], changes[-1]) if changes else (None, None)

        def ms(t: float) -> float:
            return round((t - t0) * 1000, 1)
        return {
            "changed": bool(changes),
            "first_change_ms": ms(first) if first else None,
            "first_change_after_action_ms": round((first - t_action) * 1000, 1) if first else None,
            "last_change_ms": ms(last) if last else None,
            "action_ms": ms(t_action),
            "refreshes": sum(1 for e in report["events"] if e["kind"] == "render"),
            "loads_ms": [e["ms"] for e in report["events"] if e["kind"] == "load"],
            "renders_ms": [e["ms"] for e in report["events"] if e["kind"] == "render"],
            "signals": [{"kind": e["kind"], "ms": ms(e["wall"]), "detail": e["detail"]} for e in report["signals"]],
            "value": value,
        }

    def set_mod(self, mod: str, on: bool, wait_plugins=(), window_timeout: float = 4.0) -> dict:
        """Switch a mod on or off in MO2's mod list and time the window."""

        if not self.via_bridge:
            return self.c.send("set_mod", name=mod, on=on, wait_plugins=list(wait_plugins))["data"]

        def act():
            job = self.b.run_job("set_mods", timeout=60, enabled={mod: on})
            row = next(r for r in job["result"]["mods"] if r["name"].casefold() == mod.casefold())
            listed = self.wait_plugins(wait_plugins, 6.0) if on and wait_plugins else None
            return {"enabled": row["enabled"], "plugins_listed": listed,
                    "job": {"states_seen": job["states_seen"], "elapsed_ms": job["elapsed_ms"],
                            "busy_retries": job["busy_retries"], "result": job.get("result")}}
        return self.watch(act, window_timeout)

    def set_plugin(self, plugin: str, on: bool, via: str = "default", measure: bool = True,
                   window_timeout: float = 4.0) -> dict:
        """Tick or untick a plugin. ``via``: ``default`` is the backend's route (the bridge's ``set_plugins``, or the
        driver clicking MO2's plugin list); ``ui`` and ``api`` are the driver's click and its ``IPluginList.setState``."""

        route = via if via != "default" else ("bridge" if self.via_bridge else "ui")
        if route in ("ui", "api"):
            return self.c.send("set_plugin", name=plugin, on=on, via=route, measure=measure,
                               window_timeout=window_timeout)["data"]
        self.wait_plugins([plugin], 6.0)

        def act():
            reply = self.b.call_busy("set_plugins", active={plugin: on})
            row = next(r for r in reply["plugins"] if r["name"].casefold() == plugin.casefold())
            return {"state": "active" if row["active"] else "inactive", "row": row,
                    "busy_retries": self.b.last_busy_retries}
        return self.watch(act, window_timeout) if measure else {"value": act()}

    def refresh_via_bridge(self, window_timeout: float = 3.0) -> dict:
        """The bridge's ``refresh`` (MO2 reloads its lists), with the window timed."""

        def act():
            job = self.b.run_job("refresh", timeout=60)
            return {"job": {"states_seen": job["states_seen"], "elapsed_ms": job["elapsed_ms"]}}
        return self.watch(act, window_timeout)

    def after_plugin_toggle(self, s: Scenario, measured: dict, label: str) -> None:
        """A set_plugins toggle can leave the Companion window stale (MO2 does not announce ``setState``). Where the
        scenario is only setting the world up, let MO2 refresh and say so in the findings; it is never hidden."""

        if not self.via_bridge or measured.get("changed"):
            return
        trigger = self.refresh_via_bridge()
        self.record_update(s, f"{label}: bridge refresh afterwards", trigger)
        outcome = (f"changed after {trigger.get('first_change_ms')} ms" if trigger["changed"] else "still did not change")
        s.finding(f"{label}: the window did not follow set_plugins by itself; a bridge refresh was needed",
                  f"no window change in {measured.get('action_ms')} ms + 4 s; after a bridge refresh the window {outcome} "
                  f"(signals: {self.signals_text(trigger)})")

    # ----- installs -----

    def install(self, scenario: Scenario, mod: str, choices: dict, archive: str | None = None, **extra) -> dict:
        """Install ``mod`` from its archive (or from ``archive``'s: a key of ``archives``, for an update)."""

        key = mod.replace(" ", "_")
        if self.via_bridge:
            result = self._install_bridge(scenario, mod, choices, archive=archive, **extra)
        else:
            result = self.c.send("install", timeout=240, archive=str(self.archives[archive or mod]), name=mod,
                                 choices=choices, capture_dir=str(self.shots / "installer"),
                                 shot_prefix=f"{scenario.id}-{key}", **extra)["data"]
        session = result["session"]
        scenario.notes.setdefault("installs", []).append({
            "mod": mod, "returned": result.get("returned_mod"), "install_ms": result["install_ms"],
            "window_timeline": result.get("window_timeline"),
            "mod_state": result.get("mod"), "plugins": result.get("plugins"),
            "bridge_job": result.get("bridge_job"),
            "previous_button_visible": session.get("previous_button_visible"),
            "previous_tags": session.get("previous_tags"), "warnings": session["warnings"],
            "events": session["events"], "pages": [
                {"title": p["title"], "next": p["next_text"],
                 "options": {g["name"]: [(o["text"], o["checked"], o["enabled"]) for o in g["options"]] for g in p["groups"]}}
                for p in session["pages"]]})
        timeline = result.get("window_timeline") or []
        loads = [e["ms"] for e in timeline if e["kind"] == "load"]
        renders = [e["ms"] for e in timeline if e["kind"] == "render"]
        firsts = [e["after_install_end_ms"] for e in timeline if e["kind"] == "load"]
        self.updates.append({"scenario": scenario.id, "update": f"install {mod}", "changed": bool(timeline),
                             "action_ms": result["install_ms"], "first_change_ms": None,
                             "first_change_after_action_ms": min(firsts) if firsts else None,
                             "refreshes": len(renders), "load_ms": loads, "render_ms": renders})
        scenario.timings[f"install {mod}: window redraws (ms relative to the end of installMod)"] = [
            e["after_install_end_ms"] for e in timeline if e["kind"] == "render"]
        scenario.check(f"{mod}: installer finished without warnings", not session["warnings"], "; ".join(session["warnings"]))
        scenario.check(f"{mod}: MO2 returned the mod", result.get("returned_mod") == mod, str(result.get("returned_mod")))
        job = result.get("bridge_job")
        if job:
            states = [entry[0] for entry in job["states_seen"]]
            scenario.check(f"{mod}: bridge job went queued -> running -> installed (as documented)",
                           states == ["queued", "running", "installed"], f"{states}; {job['job_status']} in {job['elapsed_ms']} ms")
            probe = job["probe"]
            if probe.get("refusal"):
                scenario.check(f"{mod}: bridge refuses a change while the installer dialog is open",
                               probe["refusal"].get("error_type") == "MO2_BUSY", json.dumps(probe["refusal"]))
            else:
                scenario.finding(f"{mod}: no modal dialog was seen through the bridge while the install ran", json.dumps(probe))
        return result

    def _install_bridge(self, scenario: Scenario, mod: str, choices: dict, archive: str | None = None, **extra) -> dict:
        """Start the install with the bridge's ``install_archive`` while the driver, armed beforehand, ticks the
        dialogs as they appear."""

        key = mod.replace(" ", "_")
        archive = str(self.archives[archive or mod])
        before = set(self.mods())
        self.c.send("install_arm", archive=archive, name=mod, choices=choices, capture_dir=str(self.shots / "installer"),
                    shot_prefix=f"{scenario.id}-{key}", **extra)
        probe: dict = {}

        def on_poll(state: dict, seconds: float) -> None:
            """Once MO2's installer dialog is up, ask the bridge for its status and try a change it must refuse."""

            if probe.get("refusal") is not None or state["status"] != "running":
                return
            status = self.b.status()
            probe["status_polls"] = probe.get("status_polls", 0) + 1
            if status["modal"]:
                probe["status"] = {k: status[k] for k in ("modal", "ui_ready", "jobs")}
                reply = self.b.raw("set_plugins", {"expected_base_path": self.b.base_path, "expected_profile": self.b.profile,
                                                   "active": {"zz-not-a-plugin.esp": True}})
                probe["refusal"] = {"ok": reply.get("ok"), "error_type": reply.get("error_type"), "error": reply.get("error"),
                                    "seconds_into_job": round(seconds, 2)}

        started = time.perf_counter()
        try:
            job = self.b.start_job("install_archive", archive=archive, name=mod)
            final = self.b.wait_job(job, 240, poll=0.05, on_poll=on_poll)
        except BaseException:
            try:
                self.c.send("install_report", timeout=30)
            except Exception:
                pass
            raise
        ended = final["ended_at"]
        installed = final["status"] == "installed"
        report = self.c.send("install_report", wait_for_window=installed, timeout=60)["data"]
        info = final.get("result") or {}
        returned = info.get("name") if installed else None
        mod_state, new_mods = None, []
        if installed:
            after = self.mods()
            new_mods = sorted(set(after) - before)
            row = after.get(returned)
            folder = Path(info["path"])
            plugins = ([e.name for e in os.scandir(folder) if e.is_file() and e.name.lower().endswith((".esp", ".esm", ".esl"))]
                       if folder.is_dir() else [])
            mod_state = {"enabled": bool(row and row["enabled"]), "plugins": plugins}
        timeline = [{"kind": e["kind"], "ms": e["ms"], "after_install_end_ms": round((e["wall"] - ended) * 1000, 1)}
                    for e in report["window_events"]]
        return {"returned_mod": returned, "install_ms": round((ended - started) * 1000, 1), "session": report["session"],
                "window_timeline": timeline, "mod": mod_state, "new_mods": new_mods,
                "bridge_job": {"job_status": final["status"], "states_seen": final["states_seen"],
                               "elapsed_ms": final["elapsed_ms"], "result": info or None, "busy_retries": job["busy_retries"],
                               "error": ({"error_type": final.get("error_type"), "error": final.get("error")}
                                         if final["status"] == "error" else None), "probe": probe}}


# ---------------------------------------------------------------- scenarios

def enable_mod(run: Run, s: Scenario, mod: str, plugins: tuple = ()) -> dict:
    """Tick a mod in MO2's list (installMod leaves a new mod switched off)."""

    measured = run.set_mod(mod, True, plugins)
    run.record_update(s, f"enable {mod}", measured)
    s.check(f"{mod}: enabled", bool(measured["value"] and measured["value"]["enabled"]), str(measured["value"]))
    return measured


def activate_plugin(run: Run, s: Scenario, plugin: str) -> dict | None:
    """Tick a plugin in MO2's plugin list unless it already is (the driver clicks the list; the bridge's
    ``set_plugins`` is the other route, see ``Run.set_plugin``)."""

    current = run.plugin_info(plugin) or {}
    s.notes.setdefault("plugin_state_before_tick", {})[plugin] = current
    if current.get("state") == "active":
        return None
    measured = run.set_plugin(plugin, True)
    run.record_update(s, f"switch on {plugin}", measured)
    s.check(f"{plugin}: switched on", measured["value"]["state"] == "active", str(measured["value"]))
    run.after_plugin_toggle(s, measured, f"switch on {plugin}")
    return measured


def plugins_txt_state(plugin: str) -> str:
    """What the profile's plugins.txt says about a plugin: active (starred), inactive (listed, no star) or absent.
    A read of the file on disk; nothing is written."""

    for line in (PROFILE / "plugins.txt").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip("*").strip().casefold() == plugin.casefold():
            return "active" if line.startswith("*") else "inactive"
    return "absent"


def modlist_txt_state(mod: str) -> str:
    """What the profile's modlist.txt says about a mod: enabled (+), disabled (-) or absent. A read only."""

    for line in (PROFILE / "modlist.txt").read_text(encoding="utf-8", errors="replace").splitlines():
        if line[1:].strip().casefold() == mod.casefold() and line[:1] in "+-":
            return "enabled" if line[0] == "+" else "disabled"
    return "absent"


def warning_cards(state: dict, mod: str) -> dict:
    """{section: [card titles]} for the warning cards that belong to ``mod``."""

    found = {}
    for section in ("missed", "off", "masters"):
        found[section] = [c["title"] for c in cards(state, section)
                          if mod.lower() in " ".join(l["text"] for l in c["lines"]).lower()]
    return found


def scenario_setup(run: Run) -> Scenario:
    s = Scenario("setup", "Driver, window and baseline")
    ping = run.c.send("ping")["data"]
    s.notes["ping"] = ping
    s.check("driver answers; profile is Logger QA", ping["profile"] == PROFILE_NAME, ping["profile"])
    s.check("commands arrive through the folder watcher (not the 1 s safety poll)", ping["command_sources"]["poll"] == 0,
            str(ping["command_sources"]))
    state = run.window()
    s.notes["baseline"] = {"header": state["header"], "missed_cards": [c.get("title") for c in cards(state, "missed")],
                           "tree_rows": [r["text"][0] for r in state["tree"]], "opened_via": state.get("opened_via"),
                           "windows_on_screen": ping["screen"], "screens": ping["screens"]}
    s.check("Companion window opens", state["visible"], state.get("opened_via", ""))
    s.notes["companion_window_screen"] = {"screen": state.get("screen"), "geometry": state.get("geometry")}
    s.check("Companion window is on the right-most monitor", state.get("screen") == ping["screen"], f"{state.get('screen')} vs {ping['screen']}")
    s.check("window reads its database without error", not state["error"], state["error"])
    existing = run.mods()
    left = [m for m in existing if m.startswith("LiveTest")]
    s.check("no LiveTest mod is installed to start with", not left, str(left))
    if run.via_bridge:
        snap = run.b.snapshot()
        status = run.b.status()
        s.notes["bridge_at_start"] = {"mods": len(snap["mods"]), "plugins": len(snap["plugins"]), "status": status,
                                      "first_plugins": [p["name"] for p in snap["plugins"][:6]]}
        s.check("bridge: status counts agree with the snapshot",
                status["mod_count"] == len(snap["mods"]) and status["plugin_count"] == len(snap["plugins"]),
                f"{status['mod_count']}/{len(snap['mods'])} mods, {status['plugin_count']}/{len(snap['plugins'])} plugins")
        s.check("bridge: MO2 is idle (no dialog, no job)", not status["modal"] and not status["jobs"], str(status))
    run.shot("00-baseline")
    run.shot("00-main-window", target="main", expand=False)
    return s


def scenario_a(run: Run) -> Scenario:
    s = Scenario("a", "Install Village, then Grass skipping the Village patch -> red missed patch")
    village = run.install(s, fixtures.VILLAGE, {
        "Edition": {"Edition": ["Standard"]}, "Standard extras": {"Extras": {"Lanterns": True, "Bridges": False}}})
    titles = [p["title"] for p in village["session"]["pages"]]
    s.check("Village: the 'Lite notes' page was skipped by the conditional step", "Lite notes" not in titles, str(titles))
    s.check("Village: the Standard extras page was shown (flag condition)", "Standard extras" in titles, str(titles))
    s.notes["village_mod_after_install"] = village.get("mod")
    state, ms = run.await_window(lambda st: tree_row(st, fixtures.VILLAGE) is not None)
    s.timings["village_row_seen_after_poll_ms"] = ms
    s.check("Companion lists Village after the install", tree_row(state, fixtures.VILLAGE) is not None)
    s.notes["village_row_before_enable"] = (tree_row(state, fixtures.VILLAGE) or {}).get("text")
    enable_mod(run, s, fixtures.VILLAGE, (fixtures.VILLAGE_PLUGIN,))
    activate_plugin(run, s, fixtures.VILLAGE_PLUGIN)
    state = run.window()
    s.check("Village shows no warning once its plugin is ticked", not any(warning_cards(state, fixtures.VILLAGE).values()),
            json.dumps(warning_cards(state, fixtures.VILLAGE)))
    # Grass: the Village patch is auto-detected (Recommended) and we skip it.
    grass = run.install(s, fixtures.GRASS, {
        "Grass": {"Length": ["Tall"]}, "Tall options": {"Wind": ["Windy"]},
        "Compatibility": {"Compatibility patches": {"Village patch": False, "Lanterns patch": False}}})
    pages = {p["title"]: p for p in grass["session"]["pages"]}
    comp = pages.get("Compatibility")
    if comp:
        before = {o["name"]: o["checked"] for g in comp["before"] for o in g["options"]}
        s.check("Grass: Village patch was pre-ticked by the plugin auto-detect (Recommended)", before.get("Village patch") is True, str(before))
        s.check("Grass: Lanterns patch was not pre-ticked (Lanterns plugin absent)", before.get("Lanterns patch") is False, str(before))
    else:
        s.check("Grass: Compatibility page seen", False, str(list(pages)))
    s.check("Grass: the 'Tall options' page appeared because Tall set its flag", "Tall options" in pages, str(list(pages)))
    enable_mod(run, s, fixtures.GRASS)
    state, ms = run.await_window(lambda st: find_card(st, "missed", "Village patch", fixtures.GRASS) is not None)
    s.timings["red_card_seen_after_poll_ms"] = ms
    card = find_card(state, "missed", "Village patch", fixtures.GRASS)
    s.check("red 'Possible missed patches' card for the Village patch", card is not None,
            json.dumps([c.get("title") for c in cards(state, "missed")]))
    if card:
        text = " ".join(l["text"] for l in card["lines"])
        s.check("card names the plugin it detected", fixtures.VILLAGE_PLUGIN in text, text)
    s.check("no card for the Lanterns patch (its plugin isn't installed)", find_card(state, "missed", "Lanterns patch", fixtures.GRASS) is None)
    s.check("the section is visible and the header chip shows the alert",
            state["sections"]["missed"]["visible"] and "missed" in state["header"]["missed"],
            f"{state['sections']['missed']['visible']} {state['header']['missed']!r}")
    row = tree_row(state, fixtures.GRASS)
    s.check("tree row for Grass shows a missed-patch warning", bool(row and "missed patch" in row["text"][1]), str(row and row["text"]))
    s.notes["state_after"] = {"header": state["header"], "grass_row": row and row["text"]}
    run.shot("a-red-missed-patch")
    return s


def scenario_b(run: Run) -> Scenario:
    s = Scenario("b", "Reinstall Grass picking the patch -> previous choices tagged, red card goes")
    result = run.install(s, fixtures.GRASS, {
        "Compatibility": {"Compatibility patches": {"Village patch": True, "Lanterns patch": False}}},
        mode="previous", overwrite="replace")
    session = result["session"]
    s.check("'Select previous' button is offered", bool(session.get("previous_button_visible")), str(session.get("previous_tooltip")))
    tags = session.get("previous_tags") or []
    tagged = sorted(t["option"] for t in tags)
    s.check("the options picked last time are tagged '(previous)' (Tall, and Windy on the page the flag reveals)",
            tagged == ["Tall", "Windy"], json.dumps(tags))
    s.check("skipped options are not tagged", not any(t["option"] in ("Village patch", "Lanterns patch", "Short") for t in tags), json.dumps(tags))
    opened = {o["name"]: o["checked"] for g in session.get("first_page_as_opened", []) for o in g["options"]}
    s.notes["first_page_as_opened"] = opened
    s.check("previous choice restored automatically when the dialog opened (Tall ticked, not the default Short)",
            opened.get("Tall") is True and opened.get("Short") is False, str(opened))
    pages = {p["title"]: p for p in session["pages"]}
    s.check("the flag-dependent 'Tall options' page appeared again", "Tall options" in pages, str(list(pages)))
    wind = pages.get("Tall options")
    if wind:
        before = {o["name"]: o["checked"] for g in wind["before"] for o in g["options"]}
        s.check("previous choice restored on the flag-dependent page (Windy ticked)", before.get("Windy") is True, str(before))
    comp = pages.get("Compatibility")
    if comp:
        before = {o["name"]: o["checked"] for g in comp["before"] for o in g["options"]}
        s.notes["compat_before_script"] = before
        s.check("previous choices restored on the Compatibility page (Village patch left unticked, as last time)",
                before.get("Village patch") is False, str(before))
    s.check("MO2's 'Mod Exists' prompt appeared and was answered", any("Mod Exists" in e for e in session["events"]), "; ".join(session["events"][:6]))
    plugin = fixtures.GRASS_PATCH_PLUGIN
    mod_state = result.get("mod") or {}
    s.notes["grass_after_reinstall"] = mod_state
    if not mod_state.get("enabled"):
        enable_mod(run, s, fixtures.GRASS, (plugin,))
    else:
        run.wait_plugins([plugin])
    state, ms = run.await_window(lambda st: find_card(st, "missed", "Village patch", fixtures.GRASS) is None, timeout=5)
    s.timings["red_card_gone_seen_after_poll_ms"] = ms
    s.check("the red missed-patch card is gone", find_card(state, "missed", "Village patch", fixtures.GRASS) is None)
    s.notes["cards_before_tick"] = warning_cards(state, fixtures.GRASS)
    s.notes["tree_row_before_tick"] = (tree_row(state, fixtures.GRASS) or {}).get("text")
    activate_plugin(run, s, plugin)
    state = run.window()
    s.check("no warning card remains for Grass", not any(warning_cards(state, fixtures.GRASS).values()), json.dumps(warning_cards(state, fixtures.GRASS)))
    row = tree_row(state, fixtures.GRASS)
    opts = option_rows(row) if row else []
    s.notes["grass_options"] = opts
    s.check("Village patch shows as picked in the tree", any(o[0].startswith("✓") and "Village patch" in o[0] for o in opts), str(opts))
    s.check("Tall and Windy still show as picked", any(o[0].startswith("✓") and "Tall" in o[0] for o in opts)
            and any(o[0].startswith("✓") and "Windy" in o[0] for o in opts), str(opts))
    s.notes["grass_row_text"] = row and row["text"]
    run.shot("b-reinstalled-with-patch")
    return s


def scenario_c(run: Run) -> Scenario:
    s = Scenario("c", "Untick the patch plugin -> orange 'Present but plugin disabled'")
    plugin = fixtures.GRASS_PATCH_PLUGIN
    measured = run.set_plugin(plugin, False)
    run.record_update(s, f"switch off {plugin}", measured)
    s.check("plugin is now unchecked", measured["value"]["state"] == "inactive", str(measured["value"]))
    if run.via_bridge:
        # set_plugins goes through IPluginList.setState, which MO2 2.5.2 does not announce; the Companion follows
        # because it watches the profile's plugins.txt, which MO2 rewrites.
        s.notes["set_plugins_route"] = {"window_changed": measured["changed"], "first_change_ms": measured.get("first_change_ms"),
                                        "signals": measured.get("signals")}
        s.check("bridge set_plugins persisted: plugins.txt on disk lists the plugin unticked",
                plugins_txt_state(plugin) == "inactive", plugins_txt_state(plugin))
        s.check("bridge set_plugins (off): the window updated by itself within 2 s",
                bool(measured["changed"] and measured["first_change_ms"] <= 2000),
                f"first change {measured.get('first_change_ms')} ms; MO2 signals: {run.signals_text(measured)}")
        if not measured["changed"]:
            trigger = run.refresh_via_bridge()
            run.record_update(s, f"bridge refresh after set_plugins off {plugin}", trigger)
            s.finding("what makes the window catch up after set_plugins: a bridge refresh "
                      + (f"worked ({trigger['first_change_ms']} ms from the start of the refresh call, "
                         f"{trigger['first_change_after_action_ms']} ms after the job completed)" if trigger["changed"] else "did NOT"),
                      f"MO2 signals: {run.signals_text(trigger)}")
    else:
        s.check("window changed without being asked to refresh", measured["changed"], f"first change {measured.get('first_change_ms')} ms")
        s.check("window updated within 2 s", bool(measured["changed"] and measured["first_change_ms"] <= 2000), str(measured.get("first_change_ms")))
    state = run.window()
    card = find_card(state, "off", "Village patch", fixtures.GRASS, "unchecked")
    s.check("orange 'Present but plugin disabled' card appears", card is not None, json.dumps([c.get("title") for c in cards(state, "off")]))
    s.check("the section is visible with a count", state["sections"]["off"]["visible"] and state["sections"]["off"]["count"] != "", str(state["sections"]["off"]["count"]))
    s.check("header chip says a plugin is switched off", "switched off" in state["header"]["off"], state["header"]["off"])
    if card:
        s.check("card uses the 'off' (orange) style", any(l["role"] == "cardOff" for l in card["lines"]), json.dumps(card["lines"]))
    s.notes["off_card"] = card
    run.shot("c-orange-plugin-disabled")
    measured = run.set_plugin(plugin, True)
    run.record_update(s, f"switch on {plugin}", measured)
    if run.via_bridge:
        s.check("bridge set_plugins persisted: plugins.txt on disk lists the plugin ticked",
                plugins_txt_state(plugin) == "active", plugins_txt_state(plugin))
        s.check("bridge set_plugins (on): the window updated by itself within 2 s",
                bool(measured["changed"] and measured["first_change_ms"] <= 2000),
                f"first change {measured.get('first_change_ms')} ms; MO2 signals: {run.signals_text(measured)}")
        run.after_plugin_toggle(s, measured, f"switch on {plugin}")
    state = run.window()
    s.check("card goes away when the plugin is ticked again", find_card(state, "off", "Village patch", fixtures.GRASS) is None)
    if run.via_bridge:
        # The route a user takes: ticking the box in MO2's plugin list (the driver clicks it). This is what the
        # window's promise ("updates by itself within 2 s") is about.
        measured = run.set_plugin(plugin, False, via="ui")
        run.record_update(s, f"switch off {plugin} (plugin-list click, driver)", measured)
        s.check("plugin-list click: plugin is unchecked", measured["value"]["state"] == "inactive", str(measured["value"]))
        s.check("plugin-list click: window changed without being asked to refresh", measured["changed"], f"first change {measured.get('first_change_ms')} ms")
        s.check("plugin-list click: window updated within 2 s", bool(measured["changed"] and measured["first_change_ms"] <= 2000), str(measured.get("first_change_ms")))
        s.finding("plugin-list click (off): MO2 signals", run.signals_text(measured))
        state = run.window()
        s.check("plugin-list click: orange card appears", find_card(state, "off", "Village patch", fixtures.GRASS, "unchecked") is not None)
        run.set_plugin(plugin, True, via="ui", measure=False)
    # MO2's own scripting route (IPluginList.setState) does not announce itself. Let the click's own update land first.
    run.settle(1.0)
    api = run.set_plugin(plugin, False, via="api", window_timeout=3)
    run.record_update(s, f"switch off {plugin} through the plugin API (setState)", api)
    s.notes["setState_api_route"] = {"window_changed": api["changed"], "value": api["value"], "signals": api.get("signals")}
    s.finding("IPluginList.setState called directly (driver): the window "
              + ("updated" if api["changed"] else "did NOT update") + " by itself", f"MO2 signals: {run.signals_text(api)}")
    run.set_plugin(plugin, True, via="ui", measure=False)
    run.settle(1.0)
    return s


def scenario_d(run: Run) -> Scenario:
    s = Scenario("d", "Install a patch whose master is missing -> purple 'Missing masters'")
    run.install(s, fixtures.ORPHAN, {"Main": {"Plugins": ["Orphan patch"]}})
    plugin = fixtures.ORPHAN_PLUGIN
    enable_mod(run, s, fixtures.ORPHAN, (plugin,))
    info = run.plugin_info(plugin)
    s.notes["orphan_plugin_state"] = info
    s.check("MO2 reads the plugin's master", bool(info) and fixtures.MISSING_MASTER in info["masters"], str(info))
    if info and info["state"] == "active":
        # MO2 ticks a new plugin itself and only warns about the missing master.
        state, _ = run.await_window(lambda st: find_card(st, "masters", "Orphan patch", fixtures.ORPHAN) is not None)
        card = find_card(state, "masters", "Orphan patch", fixtures.ORPHAN)
        run.shot("d-ticked-missing-master")
        s.check("ticked plugin with a missing master gets a purple card", card is not None,
                f"{plugin} is active and needs {fixtures.MISSING_MASTER}")
        measured = run.set_plugin(plugin, False)
        run.record_update(s, f"untick {plugin}", measured)
        s.check(f"{plugin} unticked", measured["value"]["state"] == "inactive", str(measured["value"]))
        run.after_plugin_toggle(s, measured, f"untick {plugin}")
    state, ms = run.await_window(lambda st: find_card(st, "masters", "Orphan patch", fixtures.ORPHAN) is not None)
    s.timings["masters_card_seen_after_poll_ms"] = ms
    card = find_card(state, "masters", "Orphan patch", fixtures.ORPHAN)
    s.check("purple 'Missing masters' card", card is not None,
            json.dumps({k: [c.get("title") for c in cards(state, k)] for k in ("missed", "masters", "off")}))
    if card:
        text = " ".join(l["text"] for l in card["lines"])
        s.check("card names the missing master", fixtures.MISSING_MASTER in text, text)
        s.check("card uses the masters (purple) style", any(l["role"] == "cardMasters" for l in card["lines"]), json.dumps(card["lines"]))
    s.check("header chip says a plugin can't load", "can't load" in state["header"]["masters"], state["header"]["masters"])
    s.check("it is not also listed as merely switched off", find_card(state, "off", "Orphan patch", fixtures.ORPHAN) is None)
    run.shot("d-purple-missing-masters")
    return s


def scenario_e(run: Run) -> Scenario:
    s = Scenario("e", "Disable a mod -> its state updates within ~2 s with no polling")
    probe = run.c.send("idle_probe", seconds=5)["data"]
    s.notes["idle_probe"] = probe
    s.check("an idle window does not redraw by itself (5 s, no polling)", probe["loads"] == 0 and probe["renders"] == 0, str(probe))
    state = run.window()
    s.check("(precondition) Grass has no warning before it is disabled", not any(warning_cards(state, fixtures.GRASS).values()), json.dumps(warning_cards(state, fixtures.GRASS)))
    measured = run.set_mod(fixtures.GRASS, False)
    run.record_update(s, f"disable {fixtures.GRASS}", measured)
    s.finding(f"disable a mod ({'bridge set_mods' if run.via_bridge else 'driver setActive'}): MO2 signals", run.signals_text(measured))
    s.check("mod is disabled", measured["value"] and measured["value"]["enabled"] is False, str(measured["value"]))
    if run.via_bridge:
        s.check("bridge set_mods job done -> modlist.txt on disk already says disabled",
                modlist_txt_state(fixtures.GRASS) == "disabled", modlist_txt_state(fixtures.GRASS))
    s.check("window reacted without a refresh", measured["changed"], f"first change {measured.get('first_change_ms')} ms")
    s.check("window reacted within 2 s", bool(measured["changed"] and measured["first_change_ms"] <= 2000), str(measured.get("first_change_ms")))
    state = run.window()
    card = find_card(state, "off", "Village patch", fixtures.GRASS, "disabled")
    s.check("'Present but plugin disabled' card says the mod is disabled", card is not None,
            json.dumps([(c.get("title"), [l["text"] for l in c.get("lines", [])]) for c in cards(state, "off")]))
    run.shot("e-mod-disabled")
    measured = run.set_mod(fixtures.GRASS, True, [fixtures.GRASS_PATCH_PLUGIN])
    run.record_update(s, f"enable {fixtures.GRASS}", measured)
    s.finding(f"enable a mod ({'bridge set_mods' if run.via_bridge else 'driver setActive'}): MO2 signals", run.signals_text(measured))
    s.check("window reacted to re-enabling within 2 s", bool(measured["changed"] and measured["first_change_ms"] <= 2000), str(measured.get("first_change_ms")))
    s.notes["patch_plugin_after_reenable"] = {fixtures.GRASS_PATCH_PLUGIN: run.plugin_info(fixtures.GRASS_PATCH_PLUGIN)}
    activate_plugin(run, s, fixtures.GRASS_PATCH_PLUGIN)
    state = run.window()
    s.check("no warning card for Grass after re-enabling", not any(warning_cards(state, fixtures.GRASS).values()), json.dumps(warning_cards(state, fixtures.GRASS)))
    return s


def scenario_bridge(run: Run) -> Scenario:
    """Bridge only: what it refuses, install_archive's cancel path, and whether it and the driver agree."""

    s = Scenario("bridge", "ModLabs bridge: refusals, install_archive cancelled, bridge and driver agree")
    b = run.b
    # 1. Refusals the docstring promises, one request each (nothing is changed by any of them).
    base, profile = b.base_path, b.profile
    cases = [
        ("a change without expected_base_path/expected_profile", "refresh", {}, "INVALID_PARAMS"),
        ("a change aimed at another profile", "save", {"expected_base_path": base, "expected_profile": "Not A Profile"}, "CONTEXT_MISMATCH"),
        ("a change aimed at another MO2", "save", {"expected_base_path": base + "-other", "expected_profile": profile}, "CONTEXT_MISMATCH"),
        ("an action that does not exist", "launch_game", {}, "unsupported"),
        ("job_status of an unknown job", "job_status", {"job_id": "0" * 32}, "UNKNOWN_JOB"),
        ("set_plugins for a plugin MO2 does not list", "set_plugins",
         {"expected_base_path": base, "expected_profile": profile, "active": {"zz-not-a-plugin.esp": True}}, "INVALID_PARAMS"),
        ("set_mods for a mod MO2 does not list", "set_mods",
         {"expected_base_path": base, "expected_profile": profile, "enabled": {"zz-not-a-mod": True}}, "INVALID_PARAMS"),
        ("set_plugins with nothing to do", "set_plugins", {"expected_base_path": base, "expected_profile": profile}, "INVALID_PARAMS"),
    ]
    seen = {}
    for label, action, params, wanted in cases:
        reply = b.raw(action, params)
        seen[label] = {"ok": reply.get("ok"), "error_type": reply.get("error_type"), "error": reply.get("error")}
        s.check(f"refuses {label} ({wanted})", not reply.get("ok") and reply.get("error_type") == wanted, json.dumps(seen[label]))
    wrong = BridgeClient(b.pipe_name, "not-the-token", base, profile)
    reply = wrong.raw("ping")
    s.check("refuses a wrong token (BAD_TOKEN) without echoing either token",
            not reply.get("ok") and reply.get("error_type") == "BAD_TOKEN"
            and "not-the-token" not in json.dumps(reply) and b.token_in(json.dumps(reply)) is False, json.dumps(reply))
    s.notes["refusals"] = seen
    # 2. save and refresh as jobs.
    for action in ("save", "refresh"):
        job = b.run_job(action, timeout=60)
        s.notes[f"{action}_job"] = {"states_seen": job["states_seen"], "elapsed_ms": job["elapsed_ms"], "result": job.get("result")}
        s.check(f"{action} job completes", job["status"] == "complete", json.dumps(s.notes[f"{action}_job"]))
    # 3. bridge and driver read the same MO2.
    driver_state = run.c.send("state", all_plugins=True)["data"]
    snap = b.snapshot()
    mine = [m for m in snap["mods"] if m["name"].startswith("LiveTest")]
    s.check("bridge and driver agree on every LiveTest mod's enabled state",
            all(driver_state["mods"][m["name"]]["enabled"] == m["enabled"] for m in mine) and len(mine) == 3,
            json.dumps([(m["name"], m["enabled"], driver_state["mods"].get(m["name"], {}).get("enabled")) for m in mine]))
    mismatched = [p["name"] for p in snap["plugins"]
                  if driver_state["plugins"].get(p["name"], {}).get("state") != ("active" if p["active"] else "missing" if p["missing"] else "inactive")]
    s.check("bridge and driver agree on every plugin's state", not mismatched, str(mismatched[:10]))
    managed = [m for m in snap["mods"] if m["managed"]]
    s.notes["snapshot_shape"] = {"mods": len(snap["mods"]), "managed": len(managed), "separators": sum(1 for m in snap["mods"] if m["separator"]),
                                 "plugins": len(snap["plugins"]), "sample_mod": mine[0] if mine else None,
                                 "sample_plugin": next((p for p in snap["plugins"] if p["name"].startswith("LT_")), None)}
    # 4. install_archive cancelled: install Grass again and cancel at MO2's 'Mod Exists' prompt.
    before = run.mods().get(fixtures.GRASS)
    result = run._install_bridge(s, fixtures.GRASS, {
        "Compatibility": {"Compatibility patches": {"Village patch": True, "Lanterns patch": False}}},
        mode="previous", overwrite="cancel")
    job = result["bridge_job"]
    s.notes["install_cancelled"] = {k: job[k] for k in ("job_status", "states_seen", "elapsed_ms", "result", "error")}
    s.notes["install_cancelled"]["session_events"] = result["session"]["events"]
    s.check("install_archive: cancelling at 'Mod Exists' ends the job as 'cancelled' (queued -> running -> cancelled)",
            job["job_status"] == "cancelled" and [e[0] for e in job["states_seen"]] == ["queued", "running", "cancelled"],
            json.dumps(s.notes["install_cancelled"])[:400])
    s.check("install_archive: a cancelled install returned no mod and left the installed one alone",
            result["returned_mod"] is None and run.mods().get(fixtures.GRASS) == before and (MO2 / "mods" / fixtures.GRASS).is_dir(),
            json.dumps({"before": before, "after": run.mods().get(fixtures.GRASS)}))
    s.check("install_archive: the 'Mod Exists' prompt was the dialog that cancelled it", any("Mod Exists" in e for e in result["session"]["events"]),
            "; ".join(result["session"]["events"][-4:]))
    run.settle(0.5)
    return s


def scenario_bridge_missing_archive(run: Run) -> Scenario:
    """Bridge only, last: install_archive pointed at a file that is not there. Runs after the cleanup so a surprise
    here cannot leave test mods behind."""

    s = Scenario("bridge-missing", "ModLabs bridge: install_archive with an archive that does not exist")
    missing = str(run.out / "fixtures" / "does-not-exist.zip")
    # Since 41fac0b the bridge refuses a missing archive up front: no job, no MO2 error box.
    try:
        job = run.b.start_job("install_archive", archive=missing, name="LiveTest Missing")
        refusal = None
    except BridgeError as exc:
        job, refusal = None, exc
    s.notes["missing_archive"] = {"refused": refusal.error_type if refusal else None,
                                  "detail": refusal.detail if refusal else None, "job": job}
    s.check("install_archive on a missing archive is refused with ARCHIVE_NOT_FOUND and makes no job",
            refusal is not None and refusal.error_type == "ARCHIVE_NOT_FOUND", json.dumps(s.notes["missing_archive"], default=str)[:500])
    if job is not None:  # an old bridge: let the job end so MO2 is left idle
        run.b.wait_job(job, 60, poll=0.05)
    s.check("no LiveTest Missing mod appeared", "LiveTest Missing" not in run.mods() and not (MO2 / "mods" / "LiveTest Missing").exists())
    status = run.b.status()
    s.check("MO2 is idle (no dialog, no job)", status["modal"] is False and not status["jobs"], json.dumps(status))
    s.check("status lists the test profile", PROFILE_NAME in (status.get("profiles") or []), json.dumps(status.get("profiles")))
    return s


def change_card(state: dict, mod: str) -> dict | None:
    """The "Changed installers" card of ``mod`` (title, lines, buttons), if one is listed. A card in a closed "Seen"
    fold is not built until window_state is asked to open the folds."""

    return next((c for c in cards(state, "changes") if c["title"] == mod), None)


def card_text(card: dict | None) -> str:
    return " | ".join(line["text"] for line in (card or {}).get("lines", []))


def option_row(row: dict | None, name: str) -> tuple[str, str] | None:
    """(label, status) of the option row called ``name`` below an install row."""

    return next((o for o in option_rows(row or {}) if o[0][3:] == name), None)


def scenario_f(run: Run) -> Scenario:
    s = Scenario("f", "Update Grass with a changed installer -> card and tree markers; Seen; the same version again "
                      "with one more pick -> markers and a note, no card")
    mod = fixtures.GRASS
    keep = {"Tall options": {"Wind": [fixtures.WINDY_NEW]}}   # the renamed option stays picked: no pick change yet

    def patches(glow: bool) -> dict:
        return {"Compatibility": {"Compatibility patches": {
            "Village patch": True, fixtures.GLOW_PATCH: glow, "Lanterns patch": False}}}

    def ensure_enabled(result: dict) -> None:
        if not (result.get("mod") or {}).get("enabled"):
            enable_mod(run, s, mod, (fixtures.GRASS_PATCH_PLUGIN,))
        else:
            run.wait_plugins([fixtures.GRASS_PATCH_PLUGIN])

    # 1. The update: its installer adds a patch for Village and renames Windy. The picks of last time are kept.
    result = run.install(s, mod, {**keep, **patches(False)}, archive=fixtures.GRASS_NEXT, mode="previous", overwrite="replace")
    session = result["session"]
    tagged = sorted(t["option"] for t in session.get("previous_tags") or [])
    s.check("2.2.0: the picks of last time are tagged '(previous)' (Tall and the Village patch; Windy is gone)",
            tagged == ["Tall", "Village patch"], json.dumps(session.get("previous_tags")))
    pages = {p["title"]: p for p in session["pages"]}
    before = {o["name"]: o["checked"] for g in (pages.get("Compatibility") or {}).get("before", []) for o in g["options"]}
    s.check("2.2.0: the new patch was pre-ticked by the plugin auto-detect (Recommended); we untick it",
            before.get(fixtures.GLOW_PATCH) is True, str(before))
    ensure_enabled(result)
    state, ms = run.await_window(lambda st: change_card(st, mod) is not None, timeout=8)
    s.timings["card_seen_after_poll_ms"] = ms
    card = change_card(state, mod)
    s.check("a 'Changed installers' card appears for Grass", card is not None,
            json.dumps([c.get("title") for c in cards(state, "changes")]))
    s.check("the section is shown with a count of 1", state["sections"]["changes"]["visible"]
            and state["sections"]["changes"]["count"] == "1", json.dumps(state["sections"]["changes"]["count"]))
    text = card_text(card)
    s.notes["card_lines"] = (card or {}).get("lines")
    # MO2 hands the plugin its own version string for the mod, which pads the fomod's "2.1.0" to "2.1.0.0".
    s.check("the card's version line says 2.1.0 → 2.2.0 with the new option and the rename",
            re.search(r"2\.1\.0(\.0)? → 2\.2\.0(\.0)?: 1 new option, 1 renamed", text) is not None, text)
    s.check("the card calls out the new patch: you have LT_Village.esp, you skipped it",
            f"New: {fixtures.GLOW_PATCH} — you have {fixtures.VILLAGE_PLUGIN}; you skipped it" in text, text)
    s.check("the card does not talk about changed picks", "You also changed" not in text, text)
    s.check("the card offers Show and Seen", {"Show", "Seen"} <= set((card or {}).get("buttons", [])), str((card or {}).get("buttons")))
    row = tree_row(state, mod)
    s.notes["grass_row_text"] = row and row["text"]
    s.check("the Grass row notes the new option", bool(row and "+1 new" in row["text"][1]), str(row and row["text"]))
    glow = option_row(row, fixtures.GLOW_PATCH)
    windy = option_row(row, fixtures.WINDY_NEW)
    s.notes["markers"] = {"glow": glow, "windy": windy}
    s.check("the new patch is marked '+ new patch'", bool(glow and glow[1].startswith("+ new patch")), str(glow))
    s.check("the renamed option is marked '~ was “Windy”' (and is still picked)",
            bool(windy and windy[0].startswith("✓") and windy[1].startswith(f"~ was “{fixtures.WINDY_OLD}”")), str(windy))
    marks = [o for o in option_rows(row or {}) if o[1].startswith(("newly picked", "was picked"))]
    s.check("no 'newly picked' / 'was picked' marker yet: the picks are the same as last time", not marks, str(marks))
    run.shot("f-changed-installer-card")

    # 2. Seen settles the card: it moves to the Seen group.
    run.c.send("click_card", section="changes", title=mod, button="Seen")
    state, ms = run.await_window(lambda st: change_card(st, mod) is None, timeout=5)
    s.timings["card_settled_after_poll_ms"] = ms
    s.check("after Seen the card is no longer in the list to check", change_card(state, mod) is None)
    folds = [c["fold"] for c in state["sections"]["changes"]["cards"] if "fold" in c]
    s.check("a 'Seen (1)' group appears and the count is gone", any(f.startswith("Seen (1)") for f in folds)
            and state["sections"]["changes"]["count"] == "", f"{folds} count={state['sections']['changes']['count']!r}")
    opened = run.c.send("window_state", expand=True, folds=["changes"])["data"]
    settled = change_card(opened, mod)
    s.check("the card is in the Seen group, with Reopen instead of Seen", bool(settled) and "Reopen" in settled["buttons"]
            and "Seen" not in settled["buttons"], json.dumps(settled))
    run.shot("f-card-in-seen-group")

    # 3. The same version again, picking the new patch this time: picks changed, installer the same.
    result = run.install(s, mod, {**keep, **patches(True)}, archive=fixtures.GRASS_NEXT, mode="previous", overwrite="replace")
    ensure_enabled(result)
    state, ms = run.await_window(lambda st: "+1 picked" in (tree_row(st, mod) or {"text": ["", ""]})["text"][1], timeout=8)
    s.timings["picks_note_seen_after_poll_ms"] = ms
    row = tree_row(state, mod)
    s.notes["picks_row_text"] = row and row["text"]
    s.check("the Grass row says '+1 picked'", bool(row and "+1 picked" in row["text"][1]), str(row and row["text"]))
    glow = option_row(row, fixtures.GLOW_PATCH)
    s.notes["markers_after_reinstall"] = {"glow": glow}
    s.check("the patch is picked and marked 'newly picked'",
            bool(glow and glow[0].startswith("✓") and glow[1].startswith("newly picked")), str(glow))
    others = [o for o in option_rows(row or {}) if o[1].startswith(("newly picked", "was picked")) and fixtures.GLOW_PATCH not in o[0]]
    s.check("no other option is marked as changed", not others, str(others))
    s.check("the patch is not marked '+ new' any more (the installer did not change)", bool(glow) and "+ new" not in glow[1], str(glow))
    opened = run.c.send("window_state", expand=True, folds=["changes"])["data"]
    s.check("no card at all, not even the settled one (the installer is the same as before)", change_card(opened, mod) is None
            and not opened["sections"]["changes"]["visible"],
            json.dumps({"visible": opened["sections"]["changes"]["visible"], "cards": cards(opened, "changes")}))
    run.shot("f-picks-only-reinstall")
    return s


def logger_problems(run: Run) -> dict:
    return run.c.send("logger_problems")["data"]


def await_problem(run: Run, wanted, timeout: float = 10.0) -> tuple[dict, float]:
    """Poll the Companion's MO2 problem list until ``wanted(problems)`` holds (or the time is up)."""

    start = time.perf_counter()
    while True:
        data = logger_problems(run)
        if wanted(data["problems"]) or time.perf_counter() - start > timeout:
            return data, round((time.perf_counter() - start) * 1000)
        time.sleep(0.2)


def close_window(run: Run) -> None:
    """Close the Companion window (it stays loaded in MO2), as a user does."""

    code = ["w = self.window()", "if w is not None:", "    w.close()", "spin(0.3)", "result = True"]
    run.c.send("eval", code=chr(10).join(code))


def alert_problems(data: dict) -> list[dict]:
    return [p for p in data["problems"] if p["short"].startswith("New patches available")]


def new_lines(card: dict | None) -> list[str]:
    return [line["text"] for line in (card or {}).get("lines", []) if line["role"] == "cardNew"]


def scenario_g(run: Run) -> Scenario:
    s = Scenario("g", "Skip a patch for a mod you don't have, install that mod later -> MO2 problem, 'New' card, "
                      "Fix opens the window and clears the alert")
    village, grass = fixtures.VILLAGE, fixtures.GRASS
    # Self-contained: start with neither mod (scenarios a-f leave both installed; cleanup removes whatever is left).
    for mod in (grass, village):
        if mod in run.mods():
            value = run.c.send("remove_mod", name=mod)["data"]["value"] or {}
            s.check(f"(setup) {mod} removed", bool(value.get("removed")) and not value.get("still_listed"), str(value))
    data = logger_problems(run)
    s.notes["problems_at_start"] = data
    s.check("(precondition) the setting alert_new_patches is on (its default)", data["alert_new_patches"] is True, str(data["alert_new_patches"]))
    s.check("(precondition) no new-patch problem is listed", not alert_problems(data), json.dumps(data["problems"]))

    # 1. No Village: install Grass and skip the Village patch. Nothing to flag yet.
    result = run.install(s, grass, {
        "Grass": {"Length": ["Tall"]}, "Tall options": {"Wind": ["Windy"]},
        "Compatibility": {"Compatibility patches": {"Village patch": False, "Lanterns patch": False}}})
    run.settle(1.0)
    state = run.window()
    data = logger_problems(run)
    s.check("Grass without Village: no 'Village patch' card anywhere",
            all(find_card(state, sec, "Village patch", grass) is None for sec in ("missed", "off", "masters")),
            json.dumps(warning_cards(state, grass)))
    s.check("Grass without Village: no new-patch problem in MO2", not alert_problems(data), json.dumps(data["problems"]))

    # 2. Install Village (MO2 leaves it switched off).
    village_result = run.install(s, village, {
        "Edition": {"Edition": ["Standard"]}, "Standard extras": {"Extras": {"Lanterns": True, "Bridges": False}}})
    still_off = not (village_result.get("mod") or {}).get("enabled")
    s.notes["village_enabled_after_install"] = not still_off
    short = f"New patches available for {village}"
    data, ms = await_problem(run, lambda problems: any(p["short"] == short for p in problems))
    s.timings["problem_seen_after_install_ms"] = ms
    [problem] = [p for p in alert_problems(data) if p["short"] == short] or [None]
    s.check("MO2 lists the problem 'New patches available for LiveTest Village'", problem is not None, json.dumps(data["problems"])[:600])
    if problem is None:
        return s
    s.notes["mo2_problem"] = problem
    sentence = (f"{grass} › Compatibility › Village patch: patch for {village}; "
                f"you skipped it when you installed {grass}")
    s.check("the full description names installer, page and option, the mod, and that it was skipped",
            sentence in problem["full"], problem["full"])
    s.check("the full description says the mod must be enabled first while it is still disabled",
            (f"{village} is still disabled in MO2: enable it first." in problem["full"]) == still_off, problem["full"])
    s.check("the problem offers a guided fix (the Fix button)", problem["guided_fix"] is True, str(problem))
    s.check("the Lanterns patch is not in the description (its plugin isn't installed)", "Lanterns patch" not in problem["full"], problem["full"])

    # 3. The window: the card carries the 'New' line, in the section that holds it, unfolded.
    section = "off" if still_off else "missed"
    state, ms = run.await_window(lambda st: new_lines(find_card(st, section, "Village patch", grass)) != [])
    s.timings["new_card_seen_after_poll_ms"] = ms
    card = find_card(state, section, "Village patch", grass)
    s.check(f"the Village patch card is in the {'orange switched-off' if still_off else 'red missed-patch'} section",
            card is not None, json.dumps({k: [c.get("title") for c in cards(state, k)] for k in ("missed", "off")}))
    s.check("the card says 'New: you just installed LiveTest Village'", new_lines(card) == [f"New: you just installed {village}"],
            json.dumps((card or {}).get("lines")))
    s.check("the section holding it is open", state["sections"][section]["visible"] and state["folded"][section] is False,
            json.dumps(state["folded"]))
    row = tree_row(state, grass)
    patch_row = option_row(row, "Village patch")
    s.check("the tree marks the option 'just installed'", bool(patch_row and patch_row[1].startswith("just installed")),
            str(patch_row))
    run.shot("g-new-patch-alert" + ("-mod-disabled" if still_off else ""))

    # 3b. Restart MO2 with the alert still pending: MO2 starts plugins before it has read its mod list, and an alert
    # must survive that (it used to be cleared by the empty start-up load order).
    seconds = run.restart_mo2()
    s.timings["restart_mo2_s"] = seconds
    data, ms = await_problem(run, lambda problems: any(p["short"] == short for p in problems), timeout=45)
    s.timings["problem_back_after_restart_ms"] = ms
    again = next((p for p in data["problems"] if p["short"] == short), None)
    s.check("after restarting MO2 the problem is still listed", again is not None, json.dumps(data["problems"])[:600])
    if again is None:
        return s
    s.check("after the restart it says the same", again["full"] == problem["full"], again["full"])
    s.check("after the restart the mod is still listed by MO2 and still switched off",
            run.mods().get(village, {}).get("enabled") is False if still_off else True, str(run.mods().get(village)))
    state, _ms = run.await_window(lambda st: new_lines(find_card(st, section, "Village patch", grass)) != [], timeout=10)
    card = find_card(state, section, "Village patch", grass)
    s.check("after the restart the card still says New, in its open section",
            new_lines(card) == [f"New: you just installed {village}"] and state["folded"][section] is False,
            json.dumps({"lines": (card or {}).get("lines"), "folded": state["folded"]}))
    run.shot("g-after-restart")
    problem = again

    # 4. Switch Village on: the card moves to the red section and the alert stays (the patch is still skipped).
    if still_off:
        enable_mod(run, s, village, (fixtures.VILLAGE_PLUGIN,))
        activate_plugin(run, s, fixtures.VILLAGE_PLUGIN)
        state, _ = run.await_window(lambda st: new_lines(find_card(st, "missed", "Village patch", grass)) != [])
        card = find_card(state, "missed", "Village patch", grass)
        s.check("with Village on, the card is in the red section and still says New", new_lines(card) != [], json.dumps((card or {}).get("lines")))
        data, _ms = await_problem(run, lambda problems: any(p["short"] == short and "enable it first" not in p["full"] for p in problems))
        s.check("the MO2 problem is still there and no longer says to enable the mod first",
                any(p["short"] == short and "enable it first" not in p["full"] for p in data["problems"]), json.dumps(data["problems"])[:600])
        run.shot("g-new-patch-alert-mod-enabled")
        problem = next(p for p in data["problems"] if p["short"] == short)

    # 5. Fix: the window opens with the card marked, and the alert is cleared in MO2.
    close_window(run)
    fixed = run.c.send("logger_fix", key=problem["key"])["data"]
    s.notes["fix"] = fixed
    s.check("Fix opened the Companion window", fixed["window_visible"], str(fixed))
    s.check("the problem is gone from MO2 after Fix", not any(k >= 100 for k in fixed["problems_after"]), str(fixed))
    state = run.window()
    card = find_card(state, "missed", "Village patch", grass)
    s.check("after Fix the window shows the card marked New", new_lines(card) == [f"New: you just installed {village}"],
            json.dumps((card or {}).get("lines")))
    s.check("after Fix the section is open", state["folded"]["missed"] is False, json.dumps(state["folded"]))
    run.shot("g-after-fix")
    data = logger_problems(run)
    s.check("the problem stays gone when MO2 asks again", not alert_problems(data), json.dumps(data["problems"]))
    # The marks last while the window is open, not for ever: closed and opened again, the card is plain.
    close_window(run)
    state = run.window()
    card = find_card(state, "missed", "Village patch", grass)
    s.check("a window opened afterwards shows the card without the New line (the alert was settled)",
            card is not None and new_lines(card) == [], json.dumps((card or {}).get("lines")))
    return s


def newest_event(mod: str) -> dict | None:
    """The newest install event of ``mod`` in the test MO2's Companion data (the files the installer wrote)."""

    best = None
    for path in (LOGGER_DATA / "events").glob("*.json"):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if event.get("mod", {}).get("name", "").casefold() == mod.casefold():
            if best is None or event["completed_at"] > best["completed_at"]:
                best = event
    return best


def event_picks(event: dict) -> list[tuple[str, str, str]]:
    """(page, group, option) of every option the install picked, in order."""

    return [(step["name"], group["name"], option["name"]) for step in event["steps"] for group in step["groups"]
            for option in group["options"] if option["selected"]]


def entry_picks(entry: dict) -> list[tuple[str, str, str]]:
    return [(step["name"], group["name"], option["name"]) for step in entry["steps"] if not step.get("skipped")
            for group in step["groups"] for option in group["options"] if option["picked"]]


def page_options(session: dict, title: str, key: str = "groups") -> dict[str, bool]:
    page = next((p for p in session["pages"] if p["title"] == title), None)
    return {o["name"]: o["checked"] for g in (page or {}).get(key, []) for o in g["options"]}


def scenario_h(run: Run) -> Scenario:
    s = Scenario("h", "Export the choices file, remove Grass, import the file, reinstall -> '(imported)' tags and "
                      "'Select imported'; own choices win; a hostile file is replayed safely")
    grass, village = fixtures.GRASS, fixtures.VILLAGE
    # Self-contained: both mods installed the plain way, whatever the scenarios before left.
    if village not in run.mods():
        run.install(s, village, {"Edition": {"Edition": ["Standard"]}, "Standard extras": {"Extras": {"Lanterns": True, "Bridges": False}}})
    if grass in run.mods():
        run.c.send("remove_mod", name=grass)
        run.c.send("forget_own_events", mod=grass)
    first = run.install(s, grass, {"Grass": {"Length": ["Tall"]}, "Tall options": {"Wind": ["Windy"]},
                                   "Compatibility": {"Compatibility patches": {"Village patch": False, "Lanterns patch": False}}})
    s.check("(setup) Grass installed with Tall, Windy and no patches", (first.get("mod") or {}).get("enabled") is not None, str(first.get("mod")))
    run.settle(1.0)
    start_picks = event_picks(newest_event(grass))
    s.check("(setup) the recorded picks are Tall and Windy", [p[2] for p in start_picks] == ["Tall", "Windy"], str(start_picks))

    # 1. Export from the window: the choices file and both guides.
    folder = run.out / "h-export"
    folder.mkdir(parents=True, exist_ok=True)
    choices_path, md_path, html_path = folder / "choices.json", folder / "guide.md", folder / "guide.html"
    made = run.c.send("export_choices", kind="choices", path=str(choices_path))["data"]
    run.c.send("export_choices", kind="guide", path=str(md_path))
    run.c.send("export_choices", kind="guide", path=str(html_path))
    choices = json.loads(choices_path.read_text(encoding="utf-8"))
    s.check("the choices file has the agreed format and the game", choices["format"] == "fomod-companion-choices"
            and choices["format_version"] == 1 and choices["source"]["game"], json.dumps(choices["source"]))
    names = [m["name"] for m in choices["mods"]]
    s.check("it lists every logged mod once (Grass and Village among them)", made["mods"] == len(names) and
            grass in names and village in names and len(names) == len(set(n.casefold() for n in names)), str(names))
    entry = next(m for m in choices["mods"] if m["name"] == grass)
    s.check("the Grass entry carries its picks, archive and version",
            entry_picks(entry) == start_picks and entry["archive"].endswith(".zip") and entry["version"].startswith("2.1.0"),
            json.dumps({k: entry[k] for k in ("archive", "version", "nexus_id")}) + str(entry_picks(entry)))
    guide = md_path.read_text(encoding="utf-8")
    s.check("the Markdown guide names the mod, the pages and the picked options",
            f"## {grass}" in guide and "- [x] **Tall**" in guide and "- [ ] Short (optional)" in guide, guide[:300])
    page = html_path.read_text(encoding="utf-8")
    s.check("the HTML guide is a standalone page with the same content", page.startswith("<!doctype html>")
            and 'class="picked"><span class="name">Tall' in page and "<script" not in page)
    s.notes["export"] = {"folder": str(folder), "mods": names}

    # 2. A PC with no history of Grass: remove it, forget its own installs, import the file.
    removed = run.c.send("remove_mod", name=grass)["data"]["value"] or {}
    s.check("(setup) Grass removed", removed.get("removed") and not removed.get("still_listed"), str(removed))
    forgot = run.c.send("forget_own_events", mod=grass)["data"]
    s.check("(setup) Grass's own install records forgotten, as on another PC", forgot["files_deleted"] >= 1, str(forgot))
    imported = run.c.send("import_choices", path=str(choices_path))["data"]
    summary = imported.get("summary") or {}
    s.check("import: every mod imported; Village has its own choices, Grass has none",
            summary.get("imported") == len(names) and village in summary.get("own", []) and grass in summary.get("new", []),
            json.dumps(imported)[:500])
    s.check("import: the summary says what happened", "have your own previous choices" in imported.get("message", "")
            or "has your own previous choices" in imported.get("message", ""), imported.get("message", ""))
    state = run.c.send("imported_state")["data"]
    s.check("the imported choices are stored for this instance and the menu offers to remove them",
            grass in state["mods"] and state["remove_enabled"], json.dumps(state))
    village_events = sorted(p.name for p in (LOGGER_DATA / "events").glob("*.json")
                            if json.loads(p.read_text(encoding="utf-8"))["mod"]["name"] == village)

    # 3. Reinstall Grass: offered as imported, ticked only when the user presses Select imported.
    result = run.install(s, grass, {}, mode="imported")
    session = result["session"]
    s.check("'Select imported' is offered (not 'Select previous')", session.get("previous_button_visible") and
            session.get("previous_button_text") == "Select imported", str(session.get("previous_button_text")))
    tagged = sorted(t["option"] for t in session.get("imported_tags") or [])
    s.check("the picks are tagged '(imported)' (Tall, and Windy on the page the flag reveals)", tagged == ["Tall", "Windy"],
            json.dumps(session.get("imported_tags")))
    s.check("nothing is tagged '(previous)'", not session.get("previous_tags"), json.dumps(session.get("previous_tags")))
    opened = {o["name"]: o["checked"] for g in session.get("first_page_as_opened", []) for o in g["options"]}
    s.check("nothing was ticked for the user: the dialog opened on the FOMOD's defaults (Short, not Tall)",
            opened.get("Short") is True and opened.get("Tall") is False, str(opened))
    s.check("the tooltip says nothing is ticked until pressed", "Nothing is ticked" in session.get("previous_tooltip", ""),
            session.get("previous_tooltip", ""))
    pages = [p["title"] for p in session["pages"]]
    s.check("after Select imported the flag-dependent page appeared (Tall was picked)", "Tall options" in pages, str(pages))
    after = page_options(session, "Grass", "before")
    s.check("Select imported ticked Tall", after.get("Tall") is True and after.get("Short") is False, str(after))
    s.check("it ticked Windy on the later page", page_options(session, "Tall options", "before").get("Windy") is True)
    s.check("it left the Village patch unticked, as exported (its auto-detected default was ticked)",
            page_options(session, "Compatibility", "before").get("Village patch") is False,
            str(page_options(session, "Compatibility", "before")))
    run.settle(1.0)
    event = newest_event(grass)
    s.check("the recorded install has the same picks as the exported ones", event is not None and event_picks(event) == entry_picks(entry),
            str(event and event_picks(event)) + " vs " + str(entry_picks(entry)))
    run.shot("h-imported-install")
    s.notes["imported_session_events"] = session["events"]

    # 4. Now Grass has choices of its own again: they win over the imported ones.
    result = run.install(s, grass, {}, overwrite="replace")
    session = result["session"]
    s.check("with its own history again the installer offers 'Select previous', not the imported choices",
            session.get("previous_button_text") == "Select previous" and not session.get("imported_tags")
            and sorted(t["option"] for t in session.get("previous_tags") or []) == ["Tall", "Windy"],
            json.dumps({"button": session.get("previous_button_text"), "imported": session.get("imported_tags"),
                        "previous": session.get("previous_tags")}))

    # 5. A doctored file: a SelectExactlyOne group with two picks, an option that doesn't exist, no later pages.
    hostile = json.loads(choices_path.read_text(encoding="utf-8"))
    mod = next(m for m in hostile["mods"] if m["name"] == grass)
    group = mod["steps"][0]["groups"][0]
    for option in group["options"]:
        option["picked"] = True                                        # Short and Tall: more than one pick allowed
    group["options"].append({"name": "Ghost option", "picked": True, "enabled": True})
    mod["steps"] = mod["steps"][:1]                                    # a short preset: nothing for the other pages
    hostile_path = folder / "hostile.json"
    hostile_path.write_text(json.dumps(hostile, indent=1), encoding="utf-8")
    run.c.send("remove_mod", name=grass)
    run.c.send("forget_own_events", mod=grass)
    again = run.c.send("import_choices", path=str(hostile_path))["data"]
    s.check("the doctored file imports, replacing the earlier import of Grass", (again.get("summary") or {}).get("replaced") == len(names),
            json.dumps(again)[:400])
    plugin_on = plugins_txt_state(fixtures.VILLAGE_PLUGIN) == "active"
    result = run.install(s, grass, {}, mode="imported")
    session = result["session"]
    s.check("(doctored) the install went through without warnings", not session["warnings"], "; ".join(session["warnings"]))
    first_page = page_options(session, "Grass", "before")
    s.check("(doctored) exactly one option of the 'pick one' group is ticked (Short and Tall were both asked for)",
            sorted(first_page.values()).count(True) == 1, str(first_page))
    s.check("(doctored) the option that doesn't exist was ignored without complaint", "Ghost option" not in first_page, str(first_page))
    compat = page_options(session, "Compatibility", "before")
    s.check("(doctored) a page the file says nothing about keeps the installer's own defaults (the Village patch is "
            "ticked exactly when its plugin is active)", compat.get("Village patch") is plugin_on, f"{compat} (plugin active: {plugin_on})")
    s.check("(doctored) every group still obeys its rule: the Compatibility page is left as the FOMOD set it up",
            compat.get("Lanterns patch") is False, str(compat))

    # 6. Undo the import: only the imported choices go.
    removed = run.c.send("remove_imported")["data"]
    s.check("'Remove imported choices' was on offer and removed the imported mods", removed["enabled_before"] and removed["removed"] == len(names),
            json.dumps(removed))
    state = run.c.send("imported_state")["data"]
    s.check("nothing imported is left and the menu action is switched off", state["mods"] == [] and state["files"] == []
            and not state["remove_enabled"], json.dumps(state))
    s.check("the user's own install records of other mods were not touched by the import or its removal",
            sorted(p.name for p in (LOGGER_DATA / "events").glob("*.json") if json.loads(p.read_text(encoding="utf-8"))["mod"]["name"] == village)
            == village_events, "")
    state_after = run.window()
    s.check("the window still lists Grass and reads its database without error", tree_row(state_after, grass) is not None
            and not state_after["error"], str(state_after.get("error")))
    run.shot("h-after-remove")
    return s


def descend(node: dict | None, *labels: str) -> dict | None:
    """The row reached by following the first row of each label below ``node`` (None when one is missing)."""

    for label in labels:
        node = next((child for child in (node or {}).get("children", []) if child["text"][0] == label), None)
    return node


def tree_texts(node: dict | None) -> list[tuple[str, str]]:
    """(label, status) of every row below ``node``, in order."""

    found = []

    def walk(item):
        for child in item.get("children", []):
            found.append((child["text"][0], child["text"][1]))
            walk(child)
    walk(node or {})
    return found


def scenario_i(run: Run) -> Scenario:
    s = Scenario("i", "Which option put this file here: the installer's files are recorded per option, a path search "
                      "in the window finds the right options, and the More-menu dialog answers a plugin name")
    layout = fixtures.LAYOUT
    base = fixtures.LAYOUT_DIR
    if layout in run.mods():
        run.c.send("remove_mod", name=layout)
    run.install(s, layout, {"Textures": {"Resolution": ["2K"], "Extras": {"Peak fix": True, "Dark peaks": True, "Notes": False}}})
    run.settle(1.0)

    # 1. What the installer wrote down.
    event = newest_event(layout)
    s.check("(setup) the Layout install was logged", event is not None)
    options = {o["name"]: o for step in event["steps"] for g in step["groups"] for o in g["options"]}
    s.check("every option of the installer has a recorded `files` list", set(options) == {"2K", "4K", "Peak fix", "Dark peaks", "Notes"}
            and all(isinstance(o.get("files"), list) for o in options.values()), str({n: o.get("files") for n, o in options.items()})[:500])
    two_k = options["2K"]["files"]
    s.check("2K: the folder and the plugin, as the XML writes them and resolved to the mod folder",
            [(f["kind"], f["source"], f["destination"], f["resolved"]) for f in two_k]
            == [("folder", "2k", "textures\\livetest\\layout", base), ("file", "core\\LT_Layout.esp", "LT_Layout.esp", "LT_Layout.esp")],
            json.dumps(two_k))
    s.check("4K: the other folder lands on the same destination", [(f["kind"], f["resolved"]) for f in options["4K"]["files"]]
            == [("folder", base)], json.dumps(options["4K"]["files"]))
    peak, dark = options["Peak fix"]["files"][0], options["Dark peaks"]["files"][0]
    s.check("Peak fix carries its priority (5); Dark peaks has none recorded (0)", peak.get("priority") == 5 and "priority" not in dark
            and peak["resolved"] == dark["resolved"] == f"{base}/peak.txt", json.dumps([peak, dark]))
    notes = options["Notes"]["files"][0]
    s.check("a file with an empty destination keeps its own name when resolved", notes["destination"] == ""
            and notes["resolved"] == "layout_notes.txt", json.dumps(notes))
    orders = [f["order"] for name in ("2K", "4K", "Peak fix", "Dark peaks", "Notes") for f in options[name]["files"]]
    s.check("the entries carry their position in ModuleConfig.xml, rising in document order", orders == sorted(orders)
            and len(set(orders)) == len(orders), str(orders))
    auto = event.get("automatic_installs") or []
    auto_files = auto[0].get("files", []) if auto else []
    s.check("the automatic (conditional) install is recorded too: it applied (2K was picked) and lists its file",
            len(auto) == 1 and auto[0]["installed"] is True and [f["resolved"] for f in auto_files] == [f"{base}/auto_2k.txt"]
            and auto_files[0]["order"] > max(orders), json.dumps(auto)[:400])
    required = (event.get("required_files") or {}).get("files", [])
    s.check("the files the installer always installs are recorded on their own (not as an automatic install)",
            [f["resolved"] for f in required] == [f"{base}/required.txt"] and len(auto) == 1
            and all(a["id"].startswith("conditional:") for a in auto) and required[0]["order"] < min(orders),
            json.dumps({"required_files": event.get("required_files"), "automatic_installs": auto})[:400])
    try:
        import jsonschema
        schema = json.loads((SOURCE / "schemas" / "installation-event.schema.json").read_text(encoding="utf-8"))
        errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(event)]
        s.check("the event the installer wrote validates against the published schema", not errors, "; ".join(errors[:3]))
    except ImportError:
        s.finding("jsonschema isn't installed here, so the event wasn't checked against the schema")

    # 2. Search the window for a file path (typed with backslashes and other capitals, as a user pastes it).
    state = run.c.send("search_window", query="Textures\\LiveTest\\Layout\\Peak.txt")["data"]
    row = tree_row(state, layout)
    s.check("the search finds the Layout install and nothing else", row is not None and len(state["tree"]) == 1
            and state["header"]["summary"].startswith("1 mod match"), json.dumps([r["text"] for r in state["tree"]])[:300])
    rows = dict(option_rows(row)) if row else {}
    s.check("Peak fix says it installs the file and wins (priority 5), Dark peaks that it is overwritten",
            rows.get("✓  Peak fix", "").startswith(f"installs {base}/peak.txt") and " · wins" in rows.get("✓  Peak fix", "")
            and "overwritten" in rows.get("✓  Dark peaks", ""), str(rows))
    s.check("the two resolution folders that land on that destination are listed as installing it",
            rows.get("✓  2K", "").startswith(f"installs {base}/…") and rows.get("○  4K", "").startswith(f"installs {base}/…"), str(rows))
    s.check("the option that installs nothing there (Notes) is not listed", "○  Notes" not in rows, str(rows))
    s.check("the install row counts the matches", row is not None and "4 matches" in row["text"][1], str(row and row["text"]))
    run.shot("i-path-search")
    auto_state = run.c.send("search_window", query=f"{base}/auto_2k.txt")["data"]
    auto_row = tree_row(auto_state, layout)
    holder = descend(auto_row, "Installed automatically by the installer")
    s.check("a file only the automatic install writes is found under 'Installed automatically by the installer'",
            holder is not None and [t for t in tree_texts(holder) if t[0] == "✓  Automatic install 1"
                                    and t[1].startswith(f"installs {base}/auto_2k.txt")] != [], json.dumps(auto_row)[:500] if auto_row else "no row")
    required_state = run.c.send("search_window", query=f"{base}/required.txt")["data"]
    required_holder = descend(tree_row(required_state, layout), "Installed automatically by the installer")
    s.check("a required file is found under 'Installed automatically by the installer', as 'Required by the installer'",
            required_holder is not None and [t for t in tree_texts(required_holder) if t[0] == "✓  Required by the installer"
                                             and t[1].startswith(f"installs {base}/required.txt")] != [],
            json.dumps(tree_texts(required_holder))[:400])
    plugin_state = run.c.send("search_window", query="lt_layout.ESP")["data"]
    plugin_rows = dict(option_rows(tree_row(plugin_state, layout) or {}))
    s.check("a plugin name finds the option that installs it (2K), and only that one",
            plugin_rows.get("✓  2K", "").startswith("installs LT_Layout.esp") and len(plugin_rows) == 1, str(plugin_rows))
    word_state = run.c.send("search_window", query="layout")["data"]
    word_rows = option_rows(tree_row(word_state, layout) or {})
    s.check("a plain word still searches names: the install is listed and no option claims to install a file",
            tree_row(word_state, layout) is not None and not [r for r in word_rows if "installs" in r[1]], str(word_rows))
    none_state = run.c.send("search_window", query="Meshes/nothing/here.nif")["data"]
    s.check("a path no installer installs finds nothing", not none_state["tree"] or none_state["tree"][0]["text"][0].startswith("Nothing matches"),
            json.dumps([r["text"] for r in none_state["tree"]])[:200])

    # 3. The collapsed "Files (N)" rows.
    clear = run.c.send("search_window", query="", open_files=2)["data"]
    layout_row = tree_row(clear, layout)
    two_row = descend(layout_row, "Textures", "Resolution", "✓  2K")
    files_row = next((c for c in (two_row or {}).get("children", []) if c["text"][0].startswith("Files (")), None)
    s.check("2K has a 'Files (2)' row (4K one for its folder), collapsed until opened",
            files_row is not None and files_row["text"][0] == "Files (2)", json.dumps(two_row)[:400] if two_row else "no 2K row")
    s.check("opened, it lists the recorded destinations, folders as 'Folder/…'", [c["text"][:2] for c in (files_row or {}).get("children", [])]
            == [[f"{base}/…", "folder"], ["LT_Layout.esp", ""]], json.dumps((files_row or {}).get("children")))
    run.shot("i-files-rows")

    # 4. The More menu's "Which option installed...?".
    answer = run.c.send("which_option", query="LT_Layout.esp")["data"]
    s.check("the More menu action opens 'Which option installed…?' and a plugin name is answered",
            answer["visible"] and answer["title"] == "Which option installed…?" and len(answer["results"]) == 1
            and "LiveTest Layout › Textures › Resolution › 2K (picked)" in answer["results"][0], json.dumps(answer)[:500])
    run.shot("i-dialog-plugin", target="dialog", expand=False)
    answer = run.c.send("which_option", query="Textures\\LiveTest\\Layout\\Peak.txt")["data"]
    texts = answer["results"]
    s.check("a path is answered with the exact files first, the likely winner marked, and the folders after",
            len(texts) == 4 and "Peak fix (picked)" in texts[0] and "likely winner" in texts[0] and "Dark peaks (picked)" in texts[1]
            and "overwritten by" in texts[1], json.dumps(texts)[:700])
    s.check("the two folders are in the answer too (the 4K one not picked)", any("4K (not picked)" in t for t in texts)
            and any("2K (picked)" in t for t in texts), json.dumps(texts)[:700])
    s.check("the two folders only 'may also write it': they are not marked as winning or overwritten, and the exact "
            "files decide", all("may also write it" in t and "overwritten" not in t and "likely winner" not in t
                                for t in texts if t.startswith("LiveTest Layout › Textures › Resolution")) and
            sum(1 for t in texts if t.startswith("LiveTest Layout › Textures › Resolution")) == 2, json.dumps(texts)[:700])
    run.shot("i-dialog-path", target="dialog", expand=False)
    answer = run.c.send("which_option", query="Textures\\LiveTest\\Layout\\Required.txt")["data"]
    s.check("a required file is answered as 'always installed'", len(answer["results"]) >= 1
            and "Required by the installer (always installed)" in answer["results"][0], json.dumps(answer["results"])[:500])
    answer = run.c.send("which_option", query="Meshes/Nothing/here.nif", close=True)["data"]
    s.check("something no logged installer installs is 'Not from a logged installer'",
            answer["results"] == [] and "Not from a logged installer" in answer["message"], json.dumps(answer)[:300])
    state = run.window()
    s.check("the window is back to its full list and reads its database without error",
            tree_row(state, layout) is not None and not state["error"], str(state.get("error")))
    return s


def scenario_j(run: Run) -> Scenario:
    s = Scenario("j", "Compare choices: two copies of one fixture under different names with different picks are found "
                      "as duplicates and compared; the window reads which profiles enable each mod (never writing them)")
    layout, copy = fixtures.LAYOUT, fixtures.LAYOUT_COPY
    other = COMPARE_PROFILE / "modlist.txt"
    for mod in (copy, layout):
        if mod in run.mods():
            run.c.send("remove_mod", name=mod)
    run.install(s, layout, {"Textures": {"Resolution": ["2K"], "Extras": {"Peak fix": True, "Dark peaks": False, "Notes": False}}})
    run.install(s, copy, {"Textures": {"Resolution": ["4K"], "Extras": {"Peak fix": False, "Dark peaks": True, "Notes": False}}},
                archive=layout)
    enable_mod(run, s, layout, (fixtures.LAYOUT_PLUGIN,))
    enable_mod(run, s, copy)
    run.settle(1.5)

    # 1. Two copies of the fixture, logged with different picks.
    first, second = newest_event(layout), newest_event(copy)
    s.check("(setup) both copies were logged", first is not None and second is not None)
    picks_first, picks_second = {p[2] for p in event_picks(first)}, {p[2] for p in event_picks(second)}
    s.check("they came from the same archive and picked differently",
            first["source"]["archive"] == second["source"]["archive"] and {"2K", "Peak fix"} <= picks_first
            and not picks_first & {"4K", "Dark peaks"} and {"4K", "Dark peaks"} <= picks_second
            and not picks_second & {"2K", "Peak fix"}, json.dumps([sorted(picks_first), sorted(picks_second)]))

    # 2. The profiles of this instance, read from their modlist.txt files.
    info = run.c.send("profiles", mods=[layout, copy, COMPARE_DISABLED, "Village"])["data"]
    s.check("the window sees both profiles of the instance", info["profiles"] == [PROFILE_NAME, COMPARE_PROFILE_NAME]
            and Path(info["folder"]).resolve() == (MO2 / "profiles").resolve(), json.dumps(info))
    s.check("a mod enabled in both profiles lists both; one enabled only in Logger QA lists that one",
            info["mods"]["Village"] == [PROFILE_NAME, COMPARE_PROFILE_NAME]
            and info["mods"][layout] == [PROFILE_NAME, COMPARE_PROFILE_NAME] and info["mods"][copy] == [PROFILE_NAME],
            json.dumps(info["mods"]))
    s.check(f"{COMPARE_DISABLED}, switched off in the second profile, lists only Logger QA",
            info["mods"][COMPARE_DISABLED] == [PROFILE_NAME], json.dumps(info["mods"]))

    # 3. The mod row's tooltip says it.
    state = run.window()
    row, copy_row = tree_row(state, layout), tree_row(state, copy)
    s.check("the tooltip of the first copy says it is enabled in both profiles",
            row is not None and f"Enabled in profiles: {PROFILE_NAME}, {COMPARE_PROFILE_NAME}" in row.get("tip", ""),
            json.dumps(row)[:400] if row else "no row")
    s.check("the tooltip of the second copy says Logger QA only, and neither row says 'in no profile'",
            copy_row is not None and row is not None and copy_row.get("tip", "").endswith(f"Enabled in profiles: {PROFILE_NAME}")
            and "in no profile" not in row["text"][1] and "in no profile" not in copy_row["text"][1],
            json.dumps(copy_row)[:400] if copy_row else "no row")

    # 4. A mod enabled in no profile gets a faint note, and the window follows MO2 switching it back on.
    def note(st: dict) -> str:
        return (tree_row(st, copy) or {"text": ["", "", ""]})["text"][1]

    measured = run.set_mod(copy, False)
    run.record_update(s, f"switch off {copy}", measured)
    state, ms = run.await_window(lambda st: "in no profile" in note(st), timeout=8.0)
    off_row = tree_row(state, copy)
    s.check("switched off in Logger QA, the copy is in no profile: the row says so", "in no profile" in note(state),
            f"after {ms} ms: {json.dumps(off_row)[:300] if off_row else 'no row'}")
    s.check("its tooltip says it is not enabled in any profile", off_row is not None and "Not enabled in any profile." in off_row.get("tip", ""),
            json.dumps(off_row)[:300] if off_row else "no row")
    run.shot("j-no-profile")
    measured = run.set_mod(copy, True)
    run.record_update(s, f"switch on {copy}", measured)
    state, ms = run.await_window(lambda st: "in no profile" not in note(st), timeout=8.0)
    back = tree_row(state, copy) or {"text": ["", "", ""]}
    s.check("switched on again, the note goes and the tooltip lists Logger QA",
            "in no profile" not in back["text"][1] and back.get("tip", "").endswith(f"Enabled in profiles: {PROFILE_NAME}"),
            f"after {ms} ms: {json.dumps(back)[:300]}")

    # 5. The duplicates are found and the dialog shows the differences.
    answer = run.c.send("compare_choices", kind="copies", a=layout, b=copy, identical=True)["data"]
    s.check("the More menu action opens 'Compare choices' and offers two copies of a mod",
            answer["visible"] and "copies" in answer["kinds"] and answer["kind"] == "copies",
            json.dumps({k: answer[k] for k in ("visible", "kinds", "kind")}))
    s.check("the two copies are found as duplicates", any({layout, copy} <= set(group) for group in answer["groups"]),
            json.dumps(answer["groups"]))
    rows = [tuple(r) for r in answer["rows"]]
    diffs = {(r[1], r[4]) for r in rows if r[0] == 3}
    s.check("the dialog says which options each copy picked: 2K and Peak fix only in A, 4K and Dark peaks only in B",
            {("2K", "picked only in A"), ("Peak fix", "picked only in A"), ("4K", "picked only in B"),
             ("Dark peaks", "picked only in B"), ("Notes", "same")} <= diffs, json.dumps(sorted(diffs)))
    cells = {r[1]: (r[2], r[3]) for r in rows if r[0] == 3}
    s.check("with ticks for each side", cells.get("2K") == ("✓ picked", "○ not picked")
            and cells.get("4K") == ("○ not picked", "✓ picked"), str(cells))
    s.check("the summary counts four differing picks, two on each side",
            answer["summary"] == "4 picks differ (2 only in A, 2 only in B).", answer["summary"])
    s.check("the legend names both copies", layout in answer["legend"] and copy in answer["legend"], answer["legend"])
    run.shot("j-compare-copies", target="compare", expand=False)

    # 6. Remove the second copy: it leaves the duplicates. Nothing wrote the other profile.
    removed = run.c.send("remove_mod", name=copy)["data"]
    run.record_update(s, f"remove {copy}", removed)
    run.settle(1.0)
    after = run.c.send("compare_choices", close=True)["data"]
    s.check("with the second copy removed no group of duplicates holds it", not any(copy in group for group in after["groups"]),
            json.dumps(after["groups"]))
    s.check("the Companion never wrote the other profile's modlist.txt",
            other.exists() and hashlib.sha256(other.read_bytes()).hexdigest() == COMPARE_STATE.get("modlist_hash"),
            COMPARE_STATE.get("modlist_hash", "no hash"))
    state = run.window()
    s.check("the window reads its database without error", not state["error"], str(state.get("error")))
    return s


def store_events(mod: str | None = None, *, kind: str | None = None, with_files: bool = False) -> list[dict]:
    """Every record the Companion keeps (newest first; removed ones too, with ``removed_at``; ``kind`` "fomod" or "plain"),
    read from its database without writing to it. ``mod`` and ``kind`` limit them; ``with_files`` puts a plain record's
    file list back (the database keeps it in a table of its own)."""

    import sqlite3

    path = LOGGER_DATA / "fomod_logger.sqlite3"
    if not path.exists():
        return []
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        rows = connection.execute(
            "SELECT event_id, raw_json, removed_at, kind FROM events "
            "ORDER BY julianday(completed_at) DESC, completed_at DESC, (kind = 'plain') ASC, event_id DESC").fetchall()
        found = []
        for event_id, raw, removed, row_kind in rows:
            event = json.loads(raw)
            if (mod is not None and event["mod"]["name"].casefold() != mod.casefold()) or (kind and row_kind != kind):
                continue
            event["removed_at"], event["kind"] = removed, row_kind
            if with_files and row_kind == "plain":
                event["plain"]["files"] = [r[0] for r in connection.execute(
                    "SELECT path FROM plain_files WHERE event_rowid = (SELECT rowid FROM events WHERE event_id = ?)", (event_id,))]
            found.append(event)
    finally:
        connection.close()
    return found


def wait_records(mod: str, count: int, kind: str | None = None, timeout: float = 25.0) -> list[dict]:
    """The records of ``mod`` once there are at least ``count`` of them (or what there is after ``timeout`` seconds)."""

    end = time.perf_counter() + timeout
    while True:
        found = store_events(mod, kind=kind, with_files=True)
        if len(found) >= count or time.perf_counter() > end:
            return found
        time.sleep(0.2)


def schema_errors(event: dict) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        return []
    schema = json.loads((SOURCE / "schemas" / "installation-event.schema.json").read_text(encoding="utf-8"))
    return [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(
        {k: v for k, v in event.items() if k not in ("removed_at", "hidden")})]


PLAIN_FILES_V1 = ["LT_Plain.esp", "meshes/livetest/plain/lamp.txt", "textures/livetest/plain/plain.txt"]
PLAIN_FILES_V2 = ["LT_Plain.esp", "meshes/livetest/plain/lamp2.txt", "sounds/livetest/plain/chime.txt",
                  "textures/livetest/plain/plain.txt"]


def check_plain_record(s: Scenario, rec: dict, *, archive: str, files: list[str], origin: str, plugin_masters: list[str],
                       reinstall: bool, label: str) -> None:
    plain = rec["plain"]
    s.check(f"{label}: it is a plain record with no steps and the plain installer's name",
            rec["kind"] == "plain" and rec["steps"] == [] and rec["installer"]["name"] == "No installer (plain install)",
            json.dumps({k: rec[k] for k in ("kind", "steps", "installer")}))
    s.check(f"{label}: archive {archive}, origin {origin}, "
            f"{'a reinstall' if reinstall else 'not a reinstall'}", rec["source"]["archive"] == archive
            and plain["origin"] == origin and rec["source"]["reinstallation"] is reinstall, json.dumps([rec["source"], plain["origin"]]))
    s.check(f"{label}: the files in the mod folder, MO2's meta.ini left out", plain["files"] == files and plain["file_count"] == len(files)
            and plain["files_truncated"] is False, json.dumps(plain)[:500])
    s.check(f"{label}: the plugin at the root with the masters its header names",
            plain["plugins"] == [{"name": "LT_Plain.esp", "masters": plugin_masters}], json.dumps(plain["plugins"]))
    s.check(f"{label}: the game is recorded", plain.get("game") == "skyrimspecialedition", str(plain.get("game")))
    errors = schema_errors(rec)
    s.check(f"{label}: it validates against the published schema", not errors, "; ".join(errors[:3]))


def scenario_k(run: Run) -> Scenario:
    s = Scenario("k", "Every mod has a record: a plain zip gets one (version, archive, files, plugins), the file lookup, "
                      "search, compare and the missing-master card cover it, an installer's record replaces it, removal marks it")
    plain = fixtures.PLAIN
    earlier = store_events(kind="plain")
    s.check("(setup) the FOMOD installs of the scenarios before this one got no plain record: the installer's own record "
            "was found each time", not earlier, str([(e["mod"]["name"], e["plain"]["origin"]) for e in earlier]))
    if plain in run.mods():
        run.c.send("remove_mod", name=plain)

    # 1. Install a plain zip: MO2's simple installer, no installer dialog, nothing for the Companion's installer to record.
    result = run.install(s, plain, {})
    s.check("it was installed without an installer dialog (a plain zip)", not result["session"].get("dialog_seen"),
            json.dumps(result["session"]["events"])[:300])
    began = time.perf_counter()
    records = wait_records(plain, 1, "plain")
    s.timings["the plain record was stored after (ms, from the end of the install)"] = round((time.perf_counter() - began) * 1000)
    s.check("a plain record is written a moment after the install", len(records) == 1, str(len(records)))
    first = records[0] if records else None
    if first:
        check_plain_record(s, first, archive="LiveTest-Plain-1.0.0.zip", files=PLAIN_FILES_V1, origin="install",
                           plugin_masters=[], reinstall=False, label="install 1.0.0")
        s.check("the install is the mod's version and Nexus id as MO2 had them", "version" in first["mod"] and "nexus_id" in first["mod"],
                json.dumps(first["mod"]))
    s.check("no event file was written for it (the installer wrote none; plain records live in the database)",
            newest_event(plain) is None)
    enable_mod(run, s, plain, (fixtures.PLAIN_PLUGIN,))
    activate_plugin(run, s, fixtures.PLAIN_PLUGIN)
    state, ms = run.await_window(lambda st: (tree_row(st, plain) or {"text": ["", "", ""]})["text"][1] == "no installer options  ·  LiveTest-Plain-1.0.0.zip",
                                 timeout=20.0)
    s.timings["the plain row seen after (ms, polling the window)"] = ms
    row = tree_row(state, plain)
    s.check("the window lists it with a quiet row: 'no installer options' and the archive", bool(row) and row["text"][1]
            == "no installer options  ·  LiveTest-Plain-1.0.0.zip", json.dumps(row and row["text"]))
    s.check("the summary counts mods without installer options", "without installer options" in state["header"]["summary"],
            state["header"]["summary"])
    s.check("its tooltip says it was not installed through a logged installer and gives the archive and file count",
            bool(row) and "Not installed through a logged installer" in row.get("tip", "") and "LiveTest-Plain-1.0.0.zip" in row["tip"]
            and "3 files recorded" in row["tip"], json.dumps(row and row.get("tip")))
    s.check("no warning card belongs to it (the plugin has no masters)", not any(warning_cards(state, plain).values()),
            json.dumps(warning_cards(state, plain)))
    opened = run.window(expand=True)
    texts = [c[0] for c in tree_texts(tree_row(opened, plain))]
    s.check("opened, it says it has no options, lists its plugin and has a 'Files (3)' row",
            "Not installed through a logged installer, so there are no options to show." in texts
            and "Plugin: LT_Plain.esp" in texts and "Files (3)" in texts, str(texts))
    run.shot("k-plain-row")

    # 2. The file lookup and the search box find the file.
    answer = run.c.send("which_option", query="Textures\\LiveTest\\Plain\\PLAIN.txt")["data"]
    s.check("'Which option installed…?' answers a file of a plain mod: the mod, 'not installed through an installer'",
            len(answer["results"]) == 1 and answer["results"][0].startswith("LiveTest Plain (not installed through an installer)")
            and "has textures/livetest/plain/plain.txt" in answer["results"][0], json.dumps(answer["results"])[:400])
    s.check("and says so in its first line", "1 mod not installed through an installer has it" in answer["message"], answer["message"])
    run.shot("k-which-option", target="dialog", expand=False)
    answer = run.c.send("which_option", query="LT_Plain.esp")["data"]
    s.check("a plugin name is answered the same way", len(answer["results"]) == 1
            and answer["results"][0].startswith("LiveTest Plain (not installed through an installer)"), json.dumps(answer["results"])[:300])
    answer = run.c.send("which_option", query="meshes/livetest", close=True)["data"]
    s.check("a folder is answered by the mods that have files in it (installer options of other mods answer too)",
            sum(1 for r in answer["results"] if r.startswith("LiveTest Plain (not installed through an installer)")) == 1,
            json.dumps(answer["results"])[:400])
    found = run.c.send("search_window", query="meshes/livetest/plain/lamp.txt")["data"]
    row = tree_row(found, plain)
    holder = descend(row, "Files (3)")
    s.check("a file search lists the plain mod, marks the match and opens its Files row with the file first",
            bool(row) and len(found["tree"]) == 1 and "1 match" in row["text"][1] and holder is not None
            and [c["text"][0] for c in holder.get("children", [])][:1] == ["meshes/livetest/plain/lamp.txt"],
            json.dumps(row)[:500] if row else "no row")
    run.shot("k-file-search")
    for query in ("LT_Plain.esp", "livetest-plain-1.0.0.zip", "plain"):
        found = run.c.send("search_window", query=query)["data"]
        s.check(f"searching '{query}' finds it (its plugin, its archive, its name)", tree_row(found, plain) is not None,
                json.dumps([r["text"][0] for r in found["tree"]])[:200])
    run.c.send("search_window", query="")

    # 3. An update: a new plain record, the files differ, compare says how.
    update = run.install(s, plain, {}, archive=fixtures.PLAIN_NEXT)
    records = wait_records(plain, 2, "plain")
    s.check("the update wrote a second plain record; the first is untouched", len(records) == 2
            and records[1]["event_id"] == first["event_id"], str([r["event_id"] for r in records]))
    if len(records) == 2:
        check_plain_record(s, records[0], archive="LiveTest-Plain-1.1.0.zip", files=PLAIN_FILES_V2, origin="install",
                           plugin_masters=[fixtures.MISSING_MASTER], reinstall=True, label="update 1.1.0")
        s.check("the two records have different file fingerprints",
                records[0]["plain"]["files_digest"] != records[1]["plain"]["files_digest"])
    if not (update.get("mod") or {}).get("enabled"):
        enable_mod(run, s, plain, (fixtures.PLAIN_PLUGIN,))
    activate_plugin(run, s, fixtures.PLAIN_PLUGIN)
    def updated(st: dict) -> bool:
        row = tree_row(st, plain) or {"text": ["", "", ""]}
        return find_card(st, "masters", fixtures.PLAIN_PLUGIN, plain) is not None and "LiveTest-Plain-1.1.0.zip" in row["text"][1]

    state, ms = run.await_window(updated, timeout=12.0)
    s.timings["the plain mod's missing-master card and the update's row seen after (ms)"] = ms
    card = find_card(state, "masters", fixtures.PLAIN_PLUGIN, plain)
    text = " ".join(l["text"] for l in card["lines"]) if card else ""
    s.check("its plugin switched on without a master it names is a purple 'Missing masters' card",
            card is not None and f"{fixtures.PLAIN_PLUGIN} is switched on, but it needs {fixtures.MISSING_MASTER}, which isn't in your list" in text,
            text or json.dumps({k: [c.get("title") for c in cards(state, k)] for k in ("missed", "masters", "off")}))
    row = tree_row(state, plain)
    s.check("the row marks it: '1 plugin can't load', then 'no installer options', the archive and 'Reinstall'",
            bool(row) and row["text"][1].startswith("● 1 plugin can't load  ·  no installer options  ·  LiveTest-Plain-1.1.0.zip")
            and "Reinstall" in row["text"][1],
            json.dumps([row and row["text"], [(e["kind"], e["source"]["archive"], e["removed_at"]) for e in store_events(plain)]]))
    run.shot("k-plain-missing-master")
    answer = run.c.send("compare_choices", kind="installs", a=[plain, 1], b=[plain, 0], identical=False)["data"]
    cmp_rows = {r[1]: (r[2], r[3], r[4]) for r in answer["rows"]}
    s.check("Compare choices, two installs of the mod: the summary says the archive and the files differ",
            answer["summary"] == "These differ: the archive and the files.", answer["summary"])
    s.check("it lists the archive and the file counts (3 against 4)", cmp_rows.get("Archive") == ("LiveTest-Plain-1.0.0.zip", "LiveTest-Plain-1.1.0.zip", "different")
            and cmp_rows.get("Files") == ("3", "4", "different"), json.dumps(cmp_rows)[:400])
    s.check("and the files only one side has", cmp_rows.get("meshes/livetest/plain/lamp.txt", ("", "", ""))[2] == "only in A"
            and cmp_rows.get("meshes/livetest/plain/lamp2.txt", ("", "", ""))[2] == "only in B"
            and cmp_rows.get("sounds/livetest/plain/chime.txt", ("", "", ""))[2] == "only in B"
            and "textures/livetest/plain/plain.txt" not in cmp_rows, json.dumps(sorted(cmp_rows))[:500])
    run.shot("k-compare-plain", target="compare", expand=False)
    run.c.send("compare_choices", close=True)

    # 4. An installer's record replaces the plain one (and no plain record is written for an installer's install).
    plain_before = len(store_events(kind="plain"))
    run.install(s, plain, {"Main": {"Plugins": ["Orphan patch"]}}, archive=fixtures.ORPHAN)
    time.sleep(5.0)       # longer than the plugin waits before it decides an install was a plain one
    after = store_events(plain)
    s.check("the FOMOD install over it is the mod's newest record, from the installer", bool(after) and after[0]["kind"] == "fomod"
            and any(o["name"] == "Orphan patch" for st in after[0]["steps"] for g in st["groups"] for o in g["options"]),
            json.dumps([(e["kind"], e["source"]["archive"]) for e in after]))
    s.check("no plain record was written for the installer's install; the two earlier ones are history",
            len(store_events(kind="plain")) == plain_before == 2, f"{plain_before} before, {len(store_events(kind='plain'))} after")
    state, _ms = run.await_window(lambda st: "no installer options" not in (tree_row(st, plain) or {"text": ["", ""]})["text"][1]
                                  and descend(tree_row(st, plain), "Main") is not None, timeout=12.0)
    row = tree_row(state, plain)
    s.check("the window shows it as an installer install again: options, no 'no installer options'",
            bool(row) and "no installer options" not in row["text"][1] and bool(option_rows(row)), json.dumps(row and row["text"]))
    answer = run.c.send("which_option", query="textures/livetest/plain/plain.txt", close=True)["data"]
    s.check("the old plain files are history: the lookup no longer finds them", answer["results"] == []
            and "Not from a logged installer" in answer["message"], json.dumps(answer)[:300])
    answer = run.c.send("compare_choices", kind="installs", a=[plain, 1], b=[plain, 0], identical=True)["data"]
    cmp_rows = {r[1]: (r[2], r[3]) for r in answer["rows"]}
    s.check("compare: a plain install against the installer's says only one had an installer",
            answer["summary"].startswith("One was installed through an installer and the other was not")
            and cmp_rows.get("Installer") == ("none", "an installer"), json.dumps([answer["summary"], cmp_rows.get("Installer")]))
    run.c.send("compare_choices", close=True)

    # 5. Removal marks the newest record removed; a mod installed again after that is a new first record.
    removed = run.c.send("remove_mod", name=plain)["data"]
    run.record_update(s, f"remove {plain}", removed)
    run.settle(1.0)
    after = store_events(plain)
    s.check("removing the mod marks its newest record removed and leaves the older plain records alone",
            bool(after) and after[0]["removed_at"] and all(not e["removed_at"] for e in after[1:]), json.dumps([e["removed_at"] for e in after]))
    state = run.window()
    s.check("the window no longer lists it", tree_row(state, plain) is None)
    run.install(s, plain, {})
    again = wait_records(plain, 3, "plain")
    s.check("installed again after removal: a new plain record, not marked a reinstall", len(again) == 3 and again[0]["source"]["reinstallation"] is False
            and not again[0]["removed_at"], json.dumps([(e["source"]["reinstallation"], e["removed_at"]) for e in again]))
    removed = run.c.send("remove_mod", name=plain)["data"]
    run.settle(1.0)
    s.check("removed again: that record is marked removed", store_events(plain, kind="plain")[0]["removed_at"] is not None)
    state = run.window()
    s.check("the window is clean again and reads its database without error", tree_row(state, plain) is None and not state["error"],
            str(state.get("error")))
    return s


def scenario_l(run: Run) -> Scenario:
    s = Scenario("l", "Scan existing mods: the quiet hint, a scan that is cancelled, the scan of every mod without a record "
                      "(one with 12,000 files), and nothing left to scan afterwards")
    small, big = fixtures.DROPPED, fixtures.DROPPED_BIG
    jobs = run.c.send("scan_jobs")["data"]
    names = [name for name, _kind in jobs["jobs"]]
    s.notes["scan_candidates"] = jobs
    s.check("(setup) the two mods dropped into the mods folder are among the mods that have no record",
            {small, big} <= set(names) and all(kind == "PlainJob" for _n, kind in jobs["jobs"]), json.dumps(jobs)[:400])
    s.check("(setup) none of them has a record yet", not [e for e in store_events() if e["mod"]["name"] in (small, big)])
    s.check("the special entries MO2 lists (the Creation Club ones, separators) are not among them",
            not [n for n in names if n.startswith("Creation Club") or n.endswith("_separator")], str(names)[:300])

    # 1. The hint: a line in the window, not a dialog.
    hint = run.c.send("scan_hint", refresh=True)["data"]
    s.check("the window says, in a quiet line, how many mods are not recorded yet", hint["visible"]
            and hint["text"].startswith(f"{jobs['count']} mods are in your list but not recorded here yet."), json.dumps(hint)[:300])
    s.check("with a Scan now, a Not now (this session) and a Don't show again button",
            hint["buttons"] == ["Scan now", "Not now", "Don't show again"], str(hint["buttons"]))
    run.shot("l-scan-hint")

    # 2. Cancel at once. The big mod is first, so the scan is inside its folder when Cancel is pressed.
    first = run.c.send("scan_existing", cancel_at=0, shot=str(run.shots / "l-scan-dialog-running.png"))["data"]
    s.notes["scan_with_cancel"] = first
    summary = first["summary"] or {}
    s.check("More > Scan existing mods… opened a dialog that was busy, with Cancel", first["started"] and first["first"]["button"] == "Cancel",
            json.dumps(first)[:300])
    s.check("the scan found every mod without a record", first["candidates"] == jobs["count"] == summary.get("total"), json.dumps(first)[:300])
    s.check("the dialog finished (not stuck running) and offers Close", first["running"] is False and first["button"] == "Close"
            and first["cancel_pressed_at"] is not None, json.dumps(first)[:300])
    s.check("Cancel stopped the scan before it was done: the summary says stopped and counts what was done",
            summary.get("cancelled") is True and summary.get("failed", 0) == 0
            and summary.get("plain", 0) < summary.get("total", 0), json.dumps(summary)[:400])
    recorded_big = store_events(big, kind="plain", with_files=True)
    s.check("the big mod, first in the list, is either not recorded (Cancel landed inside its folder) or recorded whole "
            "(the walk of its 12,000 files was already over): never half a record",
            not recorded_big or (len(recorded_big) == 1 and recorded_big[0]["plain"]["file_count"] == fixtures.DROPPED_BIG_FILES),
            json.dumps([e["plain"]["file_count"] for e in recorded_big]))
    s.finding("Cancel landed " + ("inside the big mod's folder" if not recorded_big else "after the big mod's folder was read"),
              f"{summary.get('plain', 0)} of {summary.get('total')} mods were done")
    s.check("the result line says it stopped", "Stopped after" in first["result"], first["result"])
    run.c.send("close_scan_dialog")
    done = {e["mod"]["name"] for e in store_events(kind="plain")} - {fixtures.PLAIN}     # (scenario k's records stay as history)
    s.check("what is recorded after Cancel is only whole records of mods (nothing half done)", done <= set(names), str(sorted(done)))

    # 3. The rest.
    remaining = run.c.send("scan_jobs")["data"]
    second = run.c.send("scan_existing", shot=str(run.shots / "l-scan-dialog-done.png"))["data"]
    s.notes["scan_rest"] = second
    summary = second.get("summary") or {}     # absent when nothing was left (Cancel landed after the last mod): fail the checks, don't crash
    left = remaining["count"]
    s.check("the second scan did what was left, every mod recorded as installed without an installer",
            second["started"] and second["candidates"] == left and summary.get("total") == left and summary.get("plain") == left
            and summary.get("failed") == 0 and summary.get("cancelled") is False, json.dumps(second)[:400])
    s.check("its dialog shows progress 'n of n', says Done, and offers Close", second["progress"] == [left, left]
            and second["status"] == "Done." and second["button"] == "Close" and f"{left} of {left}" in second["bar_text"],
            json.dumps(second)[:300])
    s.check("and its result line", f"{left} of {left} mods: {left} recorded as installed without an installer" in second["result"], second["result"])
    run.c.send("close_scan_dialog")
    run.settle(1.0)
    recorded = store_events(kind="plain")
    s.check("every mod that had no record now has a plain record, dated by when its folder was made, origin 'scan'",
            {e["mod"]["name"] for e in recorded} >= set(names) and all(e["plain"]["origin"] == "scan" for e in recorded
                                                                         if e["mod"]["name"] in names),
            str(sorted(set(names) - {e["mod"]["name"] for e in recorded})))
    [small_record] = wait_records(small, 1, "plain")[:1] or [None]
    s.check("the small dropped mod: its three files and its plugin with the master its header names",
            bool(small_record) and small_record["plain"]["files"] == [fixtures.DROPPED_PLUGIN, "meshes/dropped/a.txt", "textures/dropped/small.txt"]
            and small_record["plain"]["plugins"] == [{"name": fixtures.DROPPED_PLUGIN, "masters": ["Skyrim.esm"]}]
            and small_record["source"]["reinstallation"] is False, json.dumps(small_record and small_record["plain"])[:400])
    [big_record] = wait_records(big, 1, "plain")[:1] or [None]
    s.check(f"the big mod: all {fixtures.DROPPED_BIG_FILES:,} files recorded, nothing cut short, no plugin",
            bool(big_record) and big_record["plain"]["file_count"] == fixtures.DROPPED_BIG_FILES
            and len(big_record["plain"]["files"]) == fixtures.DROPPED_BIG_FILES and big_record["plain"]["files_truncated"] is False
            and big_record["plain"]["plugins"] == [], json.dumps(big_record and {k: v for k, v in big_record["plain"].items() if k != "files"}))
    s.check("a scanned record is dated by when its folder was made (a moment ago, here), with a time of day",
            bool(small_record) and small_record["completed_at"].endswith("Z") and small_record["completed_at"] == small_record["started_at"],
            small_record and small_record["completed_at"])

    # 4. The window and the lookup now cover them.
    state, ms = run.await_window(lambda st: tree_row(st, small) is not None and tree_row(st, big) is not None, timeout=15.0)
    s.timings["the scanned mods seen in the window after (ms)"] = ms
    row = tree_row(state, small)
    s.check("the window lists the dropped mods with the quiet row", bool(row) and row["text"][1].startswith("no installer options")
            and bool(tree_row(state, big)), json.dumps(row and row["text"]))
    s.check("the summary counts the mods without installer options", "without installer options" in state["header"]["summary"],
            state["header"]["summary"])
    answer = run.c.send("which_option", query="textures\\dropped\\small.txt", close=True)["data"]
    s.check("a file of a scanned mod is found: 'Dropped QA Small (not installed through an installer)'",
            len(answer["results"]) == 1 and answer["results"][0].startswith(f"{small} (not installed through an installer)"), json.dumps(answer)[:300])
    found = run.c.send("search_window", query="textures/dropped/big/d050/f007.txt")["data"]
    s.check("a file deep inside the big mod is found by the search box", tree_row(found, big) is not None
            and len(found["tree"]) == 1, json.dumps([r["text"] for r in found["tree"]])[:300])
    run.c.send("search_window", query="")
    after = run.c.send("scan_hint", refresh=True)["data"]
    s.check("with everything recorded the hint is gone", not after["visible"], json.dumps(after))
    again = run.c.send("scan_existing")["data"]
    s.check("with everything scanned, the menu item has nothing to do", again["started"] is False and again["candidates"] == 0, json.dumps(again))
    run.shot("l-after-scan")

    # 5. Removing a scanned mod.
    for mod in (small, big):
        run.c.send("remove_mod", name=mod)
    run.settle(1.0)
    removed = store_events(small)
    s.check("removing a scanned mod marks its record removed", bool(removed) and removed[0]["removed_at"] is not None)
    state = run.window()
    s.check("the window is clean and reads its database without error", not state["error"], str(state.get("error")))
    return s


def history_rows(kind: str | None = None) -> list[dict]:
    """The list history the Companion keeps (oldest first), read from its database without writing to it: each row with
    its kind, profile, source, whether it was found at start, its time and its items' names."""

    import sqlite3

    path = LOGGER_DATA / "fomod_logger.sqlite3"
    if not path.exists():
        return []
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        rows = connection.execute(
            "SELECT id, profile, at, kind, count, subject, detail, source, offline, ended_at, exit_code FROM history "
            "ORDER BY at, id").fetchall()
    except sqlite3.OperationalError:
        return []                    # a database from before the history
    finally:
        connection.close()
    found = []
    for identity, profile, at, row_kind, count, subject, detail, source, offline, ended, code in rows:
        if kind and row_kind != kind:
            continue
        items = json.loads(detail or "{}").get("items", [])
        found.append({"id": identity, "profile": profile, "at": at, "kind": row_kind, "count": count, "subject": subject,
                      "names": [item["name"] for item in items], "items": items, "source": source, "offline": bool(offline),
                      "ended_at": ended, "exit_code": code})
    return found


def profile_lists() -> tuple[list[str], list[str], list[str]]:
    """(enabled mods, active plugins, inactive plugins) as the test profile's modlist.txt and plugins.txt say (a read)."""

    mods = [line[1:].strip() for line in (PROFILE / "modlist.txt").read_text(encoding="utf-8", errors="replace").splitlines()
            if line[:1] == "+" and not line.endswith("_separator")]
    active, inactive = [], []
    for line in (PROFILE / "plugins.txt").read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        (active if line.startswith("*") else inactive).append(line.lstrip("*").strip())
    return mods, active, inactive


def edit_plugins_txt(switch_off: str, switch_on: str) -> dict:
    """While MO2 is closed, another tool edits plugins.txt: one plugin loses its star and another gets one. The file is
    in the test profile, which the run restores from its backup afterwards (and checks by hash)."""

    path = PROFILE / "plugins.txt"
    data = path.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in data else "\n"
    lines, changed = data.split(eol), []
    for index, line in enumerate(lines):
        if line == "*" + switch_off:
            lines[index] = switch_off
            changed.append(f"off {switch_off}")
        elif line == switch_on:
            lines[index] = "*" + switch_on
            changed.append(f"on {switch_on}")
    path.write_bytes(eol.join(lines).encode("utf-8"))
    return {"changed": changed, "eol": "crlf" if eol == "\r\n" else "lf"}


def scenario_history(run: Run, ident: str) -> Scenario:
    """The list's change history (both editions): a plugin switched off, a mod switched off and a mod moved are
    entries; with MO2 closed plugins.txt is edited (a plugin off, another on) and the next start finds those as changes
    made while Companion wasn't watching. No program is started (the shared rules forbid it); the callbacks for programs
    are only checked to be registered."""

    s = Scenario(ident, "List history: a plugin and a mod switched off and a mod moved, then MO2 closed and plugins.txt "
                        "edited: the history shows each change, the offline ones marked")
    mods, active, inactive = profile_lists()
    plugin_off = next((p for p in ("Lux Via - Halla patch.esp", "Lux Via - Riverwood covered bridge patch.esp") if p in active), None)
    mod_off = next((m for m in ("Lanterns", "Bijin Wives SE") if m in mods), None)
    mod_moved = next((m for m in ("Village", "Grass Patches") if m in mods and m != mod_off), None)
    offline_off = next((p for p in ("Village.esp", "GrassPatches.esp") if p in active and p != plugin_off), None)
    offline_on = next((p for p in ("Lux Via - Routa patch.esp", "Lux Via - Keld Nar patch.esp") if p in inactive), None)
    s.check("(precondition) the test profile has the mods and plugins this scenario uses",
            all((plugin_off, mod_off, mod_moved, offline_off, offline_on)),
            json.dumps([plugin_off, mod_off, mod_moved, offline_off, offline_on]))
    if not all((plugin_off, mod_off, mod_moved, offline_off, offline_on)):
        return s
    profile = PROFILE_NAME
    info = run.c.send("history_info", cost=True)["data"]
    s.notes["history_info_at_start"] = info
    s.notes["history_cost_in_this_mo2"] = info["cost"]
    wanted = {"mod-moved", "plugin-moved", "about-to-run", "finished-run", "profile-changed", "plugins-refreshed",
              "plugin-state-changed", "mod-state-changed"}
    s.check("MO2 accepted the callbacks the history uses (moves, programs started and ended, profile switches)",
            wanted <= set(info["callbacks"]), f"missing {sorted(wanted - set(info['callbacks']))}; has {info['callbacks']}")
    s.check("the history has looked at the list since MO2 read it", bool(info["ready"] and info["last_seen"]), json.dumps(info))
    s.check("the profile in use is the test profile", info["profile"] == profile and info["key"][1] == profile, json.dumps(info))
    run.settle(0.5)
    before = len(history_rows())

    # 1. The page opens on the installers; the segment switches to the history.
    state = run.c.send("history_state", page="history")["data"]
    s.check("the List history page shows, and the installers' chips are hidden", state["page"] == "history" and not state["installer_chips_visible"],
            json.dumps({k: state[k] for k in ("page", "installer_chips_visible", "summary")}))
    s.notes["history_at_start"] = {"summary": state["summary"], "total": state["total"], "profiles": state["profiles"]}
    run.shot("m-history-start", expand=False)

    # 2. A plugin off (MO2's plugin list), a mod off, a mod moved: each is an entry, found live (not marked offline).
    run.set_plugin(plugin_off, False, via="ui", measure=False)
    run.settle(0.8)
    run.c.send("set_mod", name=mod_off, on=False, measure=False)
    run.settle(0.8)
    priority = run.c.send("eval", code=f"result = {{n: org.modList().priority(n) for n in ({mod_moved!r},)}}")["data"]["result"][mod_moved]
    target = 4 if priority < 4 else 1
    moved = run.c.send("move_mod", name=mod_moved, priority=target)["data"]
    s.check(f"{mod_moved} was moved in the left pane", moved["moved"] and moved["before"] != moved["after"], json.dumps(moved))
    run.settle(1.2)
    rows = history_rows()[before:]
    s.notes["history_rows_live"] = rows
    kinds = {(r["kind"], tuple(r["names"])): r for r in rows}
    # (Disabling a mod leaves its plugins listed but off, so they may join the plugin's entry: a burst is one entry.)
    s.check(f"switching {plugin_off} off is an entry", any(r["kind"] == "plugin_disabled" and plugin_off in r["names"] for r in rows),
            json.dumps([(r["kind"], r["names"]) for r in rows]))
    s.check(f"switching {mod_off} off is an entry", ("mod_disabled", (mod_off,)) in kinds, json.dumps([(r["kind"], r["names"]) for r in rows]))
    s.check(f"moving {mod_moved} is an entry with where it was and where it went",
            any(r["kind"] == "mod_moved" and mod_moved in r["names"] and r["items"][0].get("before") != r["items"][0].get("after") for r in rows),
            json.dumps([r for r in rows if r["kind"] == "mod_moved"]))
    s.check("those entries are the profile's own and found live (not marked as found at start)",
            all(r["profile"] == profile and not r["offline"] and r["source"] == "snapshot" for r in rows
                if r["kind"] in ("plugin_disabled", "mod_disabled", "mod_moved")), json.dumps(rows)[:600])
    state = run.c.send("history_state", page="history", open=True)["data"]
    texts = [row["text"] for row in state["rows"]]
    listed_off = any(row["text"] == f"Plugin switched off: {plugin_off}" or plugin_off in row["children"] for row in state["rows"])
    s.check("the window lists them", listed_off and f"Switched off {mod_off}" in texts and f"Moved {mod_moved}" in texts,
            json.dumps([(row["text"], row["children"][:3]) for row in state["rows"][:8]]))
    s.check("the window's count matches the database for this profile",
            state["total"] == len([r for r in history_rows() if r["profile"] in ("", profile)]), f"{state['total']} vs {len(history_rows())}")
    run.shot("m-history-live", expand=False)

    # 3. MO2 closes; another tool edits plugins.txt; MO2 starts again: the changes are found and marked.
    edited: dict = {}

    def while_closed() -> None:
        edited.update(edit_plugins_txt(offline_off, offline_on))
        run.log(f"plugins.txt edited while MO2 was closed: {edited}")

    ids_before = max((r["id"] for r in history_rows()), default=0)
    seconds = run.restart_mo2(between=while_closed)
    s.timings["restart_mo2_s"] = seconds
    s.check("plugins.txt was edited while MO2 was closed", len(edited.get("changed", [])) == 2, json.dumps(edited))
    end = time.perf_counter() + 30
    found: list[dict] = []
    while time.perf_counter() < end:
        found = [r for r in history_rows() if r["offline"] and r["id"] > ids_before]
        if len(found) >= 2:
            break
        time.sleep(0.5)
    s.notes["history_rows_offline"] = found
    s.check(f"the next start found {offline_off} switched off, marked as found at start",
            any(r["kind"] == "plugin_disabled" and offline_off in r["names"] and r["profile"] == profile for r in found),
            json.dumps([(r["kind"], r["names"]) for r in found]))
    s.check(f"the next start found {offline_on} switched on, marked as found at start",
            any(r["kind"] == "plugin_enabled" and offline_on in r["names"] and r["profile"] == profile for r in found),
            json.dumps([(r["kind"], r["names"]) for r in found]))
    unexpected = [(r["kind"], r["names"]) for r in found if (r["kind"], tuple(r["names"])) not in
                  {("plugin_disabled", (offline_off,)), ("plugin_enabled", (offline_on,))}]
    s.check("nothing else was found (what was recorded before the restart is not found again)", not unexpected, json.dumps(unexpected))
    earlier = [r for r in history_rows() if not r["offline"] and r["kind"] in ("plugin_disabled", "mod_disabled", "mod_moved")]
    s.check("the entries made before the restart are still there", len(earlier) >= 3, json.dumps([(r["kind"], r["names"]) for r in earlier]))
    end = time.perf_counter() + 15
    while True:                  # the first look waits for MO2 to have read its list
        info = run.c.send("history_info")["data"]
        if info["last_seen"] or time.perf_counter() > end:
            break
        time.sleep(0.5)
    s.notes["history_info_after_restart"] = info
    s.check("after the restart the callbacks are registered again and the history has looked", bool(wanted <= set(info["callbacks"]) and info["last_seen"]),
            json.dumps(info))

    # 4. The window after the restart: the offline lines say so; filters, search, the since button, the export.
    state = run.c.send("history_state", page="history", force=True, open=True)["data"]
    rows_ui = {row["text"]: row for row in state["rows"]}
    off = rows_ui.get(f"Plugin switched off: {offline_off}")
    on = rows_ui.get(f"Plugin switched on: {offline_on}")
    s.check("the offline entries are in the window with 'found at MO2 start'", bool(off and on and "found at MO2 start" in off["detail"]
                                                                                  and "found at MO2 start" in on["detail"]), json.dumps(list(rows_ui)[:10]))
    s.check("the entries from before the restart are listed too", f"Switched off {mod_off}" in rows_ui and f"Moved {mod_moved}" in rows_ui,
            json.dumps(list(rows_ui)[:12]))
    s.check("no program was started through MO2, so none is listed and the 'since' button is off",
            not any(row["bold"] for row in state["rows"]) and not state["since"]["enabled"], json.dumps(state["since"]))
    s.check("the profile box starts on the profile in use", state["profile"] == f"Profile: {profile}", json.dumps(state["profiles"]))
    run.shot("m-history-after-restart", expand=False)
    narrowed = run.c.send("history_state", kinds_off=["mods_on_off", "mod_order", "installs", "profiles"])["data"]
    s.check("switching the kinds off narrows the list to plugin changes", narrowed["total"] >= 2 and all(
        row["text"].startswith("Plugin ") or " plugins " in row["text"] for row in narrowed["rows"]), json.dumps([r["text"] for r in narrowed["rows"]]))
    searched = run.c.send("history_state", kinds_on=["mods_on_off", "mod_order", "installs", "profiles"], search=mod_moved)["data"]
    s.check(f"searching for {mod_moved} finds its entries", searched["total"] >= 1 and any(mod_moved in row["text"] for row in searched["rows"]),
            json.dumps([r["text"] for r in searched["rows"]]))
    cleared = run.c.send("history_state", search="")["data"]
    s.check("clearing the search brings everything back", cleared["total"] == state["total"], f"{cleared['total']} vs {state['total']}")
    exported = run.out / f"{ident}-list-history.md"
    written = run.c.send("history_export", path=str(exported))["data"]["written"]
    body = exported.read_text(encoding="utf-8") if exported.exists() else ""
    s.check("Export history… wrote the list as Markdown, with the offline lines", written == cleared["total"] and f"Plugin switched off: {offline_off}" in body
            and "found at MO2 start" in body and body.startswith("# Changes to the list"), f"{written} entries; {body[:300]!r}")
    run.shot("m-history-final", expand=False)
    run.c.send("history_state", page="installers")           # the next scenarios read the installers' page
    return s


def scenario_profile_switch(run: Run, ident: str) -> Scenario:
    """Switching profile (both editions): MO2 is switched to the temporary second profile and back through its own profile
    box. The history must hold the switches and nothing else: the new profile's lists load slowly, and a look before
    they are in place would compare them with the old profile's plugin states and record bogus enable, disable and move
    entries. The second profile differs from the first (a mod is off in it), so a bogus comparison would show."""

    s = Scenario(ident, "List history: switch to a second profile and back: only the switches are entries, no plugin or "
                        "mod changes")
    other = COMPARE_PROFILE_NAME
    s.check("(precondition) the temporary second profile exists", COMPARE_PROFILE.is_dir(), str(COMPARE_PROFILE))
    run.settle(0.5)
    ids_before = max((r["id"] for r in history_rows()), default=0)

    def wait_settled(profile: str) -> dict:
        end = time.perf_counter() + 30
        while True:
            info = run.c.send("history_info")["data"]
            if (info["key"] and info["key"][1] == profile and not info["switch_pending"]) or time.perf_counter() > end:
                return info
            time.sleep(0.5)

    visits = [other, PROFILE_NAME, other, PROFILE_NAME]
    for number, name in enumerate(visits, 1):
        moved = run.c.send("switch_profile", name=name, timeout=60)["data"]
        s.check(f"MO2 switched to {name}", moved["after"] == name, json.dumps(moved))
        info = wait_settled(name)
        s.check(f"the history followed to {name} and looked at its lists", bool(info["key"] and info["key"][1] == name
                                                                          and not info["switch_pending"] and info["last_seen"]), json.dumps(info))
        run.settle(1.0)
    rows = [r for r in history_rows() if r["id"] > ids_before]
    s.notes["history_rows_after_switches"] = rows
    s.check("the history holds the four switches, and nothing else",
            [r["kind"] for r in rows] == ["profile_switched"] * 4, json.dumps([(r["kind"], r["names"], r["offline"]) for r in rows]))
    s.check("each switch names where it went and where it came from, found live",
            [(r["items"][0]["before"], r["items"][0]["after"], r["offline"]) for r in rows] ==
            [(PROFILE_NAME, other, False), (other, PROFILE_NAME, False), (PROFILE_NAME, other, False), (other, PROFILE_NAME, False)],
            json.dumps([r["items"] for r in rows]))
    state = run.c.send("history_state", page="history", force=True, profile="All profiles")["data"]
    shown = [row["text"] for row in state["rows"][:4]]
    s.check("the window lists the switches", shown == [f"Switched to profile {PROFILE_NAME}", f"Switched to profile {other}"] * 2,
            json.dumps(shown))
    run.shot(f"{ident}-history-switches", expand=False)
    run.c.send("history_state", page="installers")
    ping = run.c.send("ping")["data"]
    s.check("MO2 is back on the test profile", ping["profile"] == PROFILE_NAME, ping["profile"])
    return s


def scenario_n(run: Run) -> Scenario:
    return scenario_profile_switch(run, "n")


def scenario_p7(run: Run) -> Scenario:
    return scenario_profile_switch(run, "p7")


def scenario_m(run: Run) -> Scenario:
    return scenario_history(run, "m")


def scenario_p6(run: Run) -> Scenario:
    return scenario_history(run, "p6")


def scenario_cleanup(run: Run) -> Scenario:
    s = Scenario("cleanup", "Remove the test mods; the window drops them")
    for mod in (fixtures.LAYOUT, fixtures.ORPHAN, fixtures.GRASS, fixtures.VILLAGE):
        measured = run.c.send("remove_mod", name=mod)["data"]
        run.record_update(s, f"remove {mod}", measured)
        value = measured["value"] or {}
        s.check(f"{mod} removed from MO2", value.get("removed") and not value.get("still_listed"), str(value))
        s.check(f"window reacted to removing {mod} within 2 s", bool(measured["changed"] and measured["first_change_ms"] <= 2000),
                f"{measured.get('first_change_ms')} ms after the call started "
                f"({measured.get('first_change_after_action_ms')} ms after it returned)")
    state = run.window()
    s.check("no LiveTest row remains in the window", not [r for r in state["tree"] if r["text"][0].startswith("LiveTest")], str([r["text"][0] for r in state["tree"]]))
    s.check("no LiveTest card remains", all(find_card(state, sec, "", "LiveTest") is None for sec in ("missed", "off", "masters", "review")))
    run.shot("z-after-cleanup")
    left = [m for m in run.mods() if m.startswith("LiveTest")]
    s.check("no LiveTest mod left in MO2", not left, str(left))
    return s


# ---------------------------------------------------------------- the FOMOD Plus edition (--edition plus)

def plus_events(mod: str | None = None) -> list[dict]:
    """Every install the Plus edition has stored (newest first, removed ones included, with ``removed_at``), read
    from its database; ``mod`` limits them to one mod."""

    import sqlite3

    path = LOGGER_DATA / "fomod_logger.sqlite3"
    if not path.exists():
        return []
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        rows = connection.execute(
            "SELECT raw_json, removed_at FROM events ORDER BY julianday(completed_at) DESC, completed_at DESC, event_id DESC").fetchall()
    finally:
        connection.close()
    found = []
    for raw, removed in rows:
        event = json.loads(raw)
        event["removed_at"] = removed
        if mod is None or event["mod"]["name"].casefold() == mod.casefold():
            found.append(event)
    return found


def plus_state(run: Run) -> dict:
    return run.c.send("plus_state")["data"]


def option_of(event: dict, name: str) -> dict | None:
    return next((o for s in event["steps"] for g in s["groups"] for o in g["options"] if o["name"] == name), None)


def plus_record_of(run: Run, mod: str) -> dict:
    """What FOMOD Plus saved for ``mod``, as MO2 hands it out (parsed), plus the facts MO2 keeps beside it."""

    data = run.c.send("mod_record", name=mod)["data"]
    data["parsed"] = json.loads(data["record"]) if data["record"] else None
    return data


def record_picks(parsed: dict) -> list[tuple[str, str, str]]:
    return [(step["name"], group["name"], name) for step in parsed["steps"] for group in step["groups"]
            for name in group["plugins"]]


def plus_json(*steps) -> dict:
    """What FOMOD Plus saves for an install: every step of the FOMOD in order, each group with the options picked
    (``plugins``) and the ones the user unticked by hand (``deselected``). ``steps`` are (step, [(group, picked,
    deselected), ...])."""

    return {"steps": [{"name": name, "groups": [{"name": group, "plugins": list(picked), "deselected": list(unticked)}
                                                 for group, picked, unticked in groups]} for name, groups in steps]}


VILLAGE_RECORD = plus_json(("Edition", [("Edition", ["Standard"], [])]), ("Standard extras", [("Extras", ["Lanterns"], [])]),
                           ("Lite notes", [("Notes", [], [])]))


def grass_record(*, village_patch: bool) -> dict:
    return plus_json(("Grass", [("Length", ["Tall"], [])]), ("Tall options", [("Wind", ["Windy"], [])]),
                     ("Compatibility", [("Compatibility patches", ["Village patch"] if village_patch else [],
                                         [] if village_patch else ["Village patch"])]))


def plus_install(run: Run, s: Scenario, mod: str, choices: dict, record: dict, **extra) -> dict:
    """Install ``mod`` the way FOMOD Plus would and wait for the Plus edition to store the new install. With the real
    FOMOD Plus installer its own dialog is driven; emulated, MO2's own FOMOD installer installs and the driver then
    saves ``record`` on the mod through the plugin-setting API (see the docstring)."""

    before = len(plus_events(mod))
    if PLUS_INSTALLER == "real":
        result = run.install(s, mod, choices, **extra)
    else:
        result = run.install(s, mod, choices, installer="stock", plus_record=record, **extra)
    began = time.perf_counter()
    while len(plus_events(mod)) <= before and time.perf_counter() - began < 25:
        time.sleep(0.1)
    s.timings[f"{mod}: install stored after (ms, from the end of the install)"] = round((time.perf_counter() - began) * 1000)
    s.check(f"{mod}: the Plus edition stored the install", len(plus_events(mod)) == before + 1,
            f"{before} installs before, {len(plus_events(mod))} after")
    run.c.send("wait_plus_idle", settle=0.4)
    return result


def scenario_plus_setup(run: Run) -> Scenario:
    s = scenario_setup(run)
    s.id, s.title = "plus-setup", "FOMOD Plus edition: the plugin, its own data folder, the window and MO2's problem list"
    state = plus_state(run)
    s.notes["plus_state"] = state
    s.check("the plugin running is the FOMOD Plus edition (FomodLoggerPlusPlugin, 'FOMOD Logger Plus')",
            state["class"] == "FomodLoggerPlusPlugin" and state["name"] == "FOMOD Logger Plus" and state["active"], json.dumps(state)[:300])
    s.check("MO2 has FOMOD Plus enabled (the installer that will take the installs)", state["fomod_plus_enabled"] is True)
    s.check("the edition keeps its data in its own folder (plugins/data/fomod_logger_plus)",
            os.path.normcase(state["data_root"]) == os.path.normcase(str(LOGGER_DATA)), state["data_root"])
    s.check("the full edition's data folder was not made", not (PLUGINS / "data" / "fomod_logger").exists())
    s.check("no problem in MO2's problem list to start with", not state["problems"], json.dumps(state["problems"]))
    menu = run.c.send("menu_actions")["data"]
    s.notes["menu"] = menu
    s.check("the window is titled for the edition", menu["title"] == "Modlist Companion (for FOMOD Plus)", menu["title"])
    s.check("More has 'Scan existing mods…'", "Scan existing mods…" in menu["actions"], str(menu["actions"]))
    s.check("More has no 'Import choices file…' or 'Remove imported choices'",
            "Import choices file…" not in menu["actions"] and "Remove imported choices" not in menu["actions"], str(menu["actions"]))
    s.check("export is still there", "Export choices file…" in menu["actions"] and "Export install guide…" in menu["actions"])
    s.check("no LiveTest install is stored yet", not [e for e in plus_events() if e["mod"]["name"].startswith("LiveTest")])
    return s


def scenario_p1(run: Run) -> Scenario:
    s = Scenario("p1", "FOMOD Plus's choices for Village, then Grass with the Village patch unticked -> a rebuilt event "
                       "and the red missed-patch card")
    village, grass = fixtures.VILLAGE, fixtures.GRASS
    result = plus_install(run, s, village, {"Edition": {"Edition": ["Standard"]},
                                             "Standard extras": {"Extras": {"Lanterns": True, "Bridges": False}}}, VILLAGE_RECORD)
    session = result["session"]
    titles = [p["title"] for p in session["pages"]]
    s.notes["installer"] = PLUS_INSTALLER
    s.check("the install ran in the installer's dialog (FOMOD Plus's own with --plus-installer real, MO2's otherwise)",
            bool(session.get("plus_dialog")) is (PLUS_INSTALLER == "real") and session["dialog_seen"], str(session.get("dialog_title")))
    s.check("Village: the step conditions skipped the 'Lite notes' page and showed 'Standard extras'",
            "Lite notes" not in titles and "Standard extras" in titles, str(titles))
    idle = run.c.send("wait_plus_idle")["data"]
    s.check("Village: the rebuild finished (nothing pending)", idle["idle"], str(idle))
    record = plus_record_of(run, village)
    s.notes["village_record"] = record["parsed"]
    s.check("MO2 hands out the choices saved for Village through the plugin setting (FOMOD Plus's key)",
            record["parsed"] is not None and ("Standard extras", "Extras", "Lanterns") in record_picks(record["parsed"]),
            str(record["record"])[:200])
    s.check("MO2 records the archive FOMOD Plus installed from",
            record["installation_file"].endswith("LiveTest-Village-1.0.0.zip"), record["installation_file"])
    [event] = plus_events(village)[:1] or [None]
    s.check("Companion stored an install of Village, rebuilt from the archive", bool(event) and event["source"].get("reconstructed") is True,
            json.dumps(event and event["source"])[:300])
    if event:
        s.notes["village_event_source"] = event["source"]
        s.check("the event says where it came from", event["installer"]["name"].startswith("Modlist Companion (reconstructed"),
                event["installer"]["name"])
        s.check("Village's picks are the ones made in FOMOD Plus's dialog",
                sorted(event_picks(event)) == [("Edition", "Edition", "Standard"), ("Standard extras", "Extras", "Lanterns")],
                str(event_picks(event)))
        s.check("the hidden 'Lite notes' step is recorded as hidden", any(st["name"] == "Lite notes" and not st["visible"] for st in event["steps"]))
        s.check("the standard edition's plugin is listed on the option (rebuilt from the archive, not just names)",
                fixtures.VILLAGE_PLUGIN in (option_of(event, "Standard") or {}).get("plugins", []), json.dumps(option_of(event, "Standard"))[:300])
        s.check("the install's archive, version and Nexus id are recorded",
                event["source"]["archive"] == "LiveTest-Village-1.0.0.zip"
                and (PLUS_INSTALLER == "real" or event["mod"]["version"].startswith("1.0.0")),   # (real: the version MO2 gave the mod)
                json.dumps({"source": event["source"]["archive"], "mod": event["mod"]}))
    s.check("no native event folder was made (this edition has no installer)", not (LOGGER_DATA / "events").exists())
    state, ms = run.await_window(lambda st: tree_row(st, village) is not None)
    s.timings["village_row_seen_after_poll_ms"] = ms
    s.check("the window lists Village after the install", tree_row(state, village) is not None)
    enable_mod(run, s, village, (fixtures.VILLAGE_PLUGIN,))
    activate_plugin(run, s, fixtures.VILLAGE_PLUGIN)

    # Grass: FOMOD Plus pre-ticks the patch for Village (a Recommended option: LT_Village.esp is active); we untick it.
    result = plus_install(run, s, grass, {"Grass": {"Length": ["Tall"]}, "Tall options": {"Wind": ["Windy"]},
                                          "Compatibility": {"Compatibility patches": {"Village patch": False, "Lanterns patch": False}}},
                          grass_record(village_patch=False))
    session = result["session"]
    pages = {p["title"]: p for p in session["pages"]}
    comp = pages.get("Compatibility")
    before = {o["name"]: o["checked"] for g in (comp or {}).get("before", []) for o in g["options"]}
    s.notes["compatibility_page_as_opened"] = before
    if before.get("Village patch") is True:
        s.finding("FOMOD Plus pre-ticked the Village patch (a Recommended option); the driver unticked it by clicking", str(before))
    else:
        s.finding("FOMOD Plus did not pre-tick the Village patch", str(before))
    s.check("Grass: the 'Tall options' page appeared because Tall set its flag", "Tall options" in pages, str(list(pages)))
    idle = run.c.send("wait_plus_idle")["data"]
    s.check("Grass: the rebuild finished", idle["idle"], str(idle))
    record = plus_record_of(run, grass)
    s.notes["grass_record"] = record["parsed"]
    parsed = record["parsed"] or {"steps": []}
    s.check("FOMOD Plus saved Tall and Windy as picked", ("Grass", "Length", "Tall") in record_picks(parsed)
            and ("Tall options", "Wind", "Windy") in record_picks(parsed), str(record_picks(parsed)))
    deselected = [name for step in parsed["steps"] for group in step["groups"] for name in group["deselected"]]
    s.notes["grass_deselected"] = deselected
    s.check("the Village patch is not among the picks", "Village patch" not in [p[2] for p in record_picks(parsed)], str(record_picks(parsed)))
    event = (plus_events(grass) or [None])[0]
    s.check("Companion stored an install of Grass, rebuilt from the archive", bool(event) and event["source"].get("reconstructed") is True,
            json.dumps(event and event["source"])[:300])
    if event:
        s.check("Grass's picks are Tall and Windy only", sorted(event_picks(event)) == [("Grass", "Length", "Tall"), ("Tall options", "Wind", "Windy")],
                str(event_picks(event)))
        patch = option_of(event, "Village patch") or {}
        s.check("the Village patch is a skipped option that carries its plugin and master (what a missed patch needs)",
                patch.get("selected") is False and patch.get("plugins") == [fixtures.GRASS_PATCH_PLUGIN]
                and fixtures.VILLAGE_PLUGIN in patch.get("masters", []), json.dumps(patch)[:300])
        s.check("the event says the load order is what MO2 lists now",
                any("plugins MO2 lists now" in n for n in event["source"].get("reconstruction_notes", [])),
                str(event["source"].get("reconstruction_notes")))
    enable_mod(run, s, grass)
    state, ms = run.await_window(lambda st: find_card(st, "missed", "Village patch", grass) is not None)
    s.timings["red_card_seen_after_poll_ms"] = ms
    card = find_card(state, "missed", "Village patch", grass)
    s.check("the red 'Possible missed patches' card for the Village patch shows, from the rebuilt event", card is not None,
            json.dumps([c.get("title") for c in cards(state, "missed")]))
    if card:
        text = " ".join(l["text"] for l in card["lines"])
        s.check("the card names the plugin it detected (Village's) and says the patch was skipped",
                fixtures.VILLAGE_PLUGIN in text and "skipped" in text, text)
    s.check("the header counts a missed patch", "1" in state["header"]["missed"], json.dumps(state["header"]))
    now = plus_state(run)
    s.check("MO2's problem list still has nothing for the edition", not now["problems"], json.dumps(now["problems"]))
    run.shot("p1-missed-patch-card")
    return s


def scenario_p2(run: Run) -> Scenario:
    s = Scenario("p2", "Reinstall Grass with the Village patch picked -> a new install, 'newly picked' "
                       "markers and a note on the row; the red card goes")
    grass = fixtures.GRASS
    first = plus_events(grass)
    s.check("there is one install of Grass to start with", len(first) == 1, str(len(first)))
    result = plus_install(run, s, grass, {"Grass": {"Length": ["Tall"]}, "Tall options": {"Wind": ["Windy"]},
                                          "Compatibility": {"Compatibility patches": {"Village patch": True, "Lanterns patch": False}}},
                          grass_record(village_patch=True), overwrite="replace")
    session = result["session"]
    pages = {p["title"]: p for p in session["pages"]}
    opened = {o["name"]: o["checked"] for g in (pages.get("Grass") or {}).get("before", []) for o in g["options"]}
    if PLUS_INSTALLER == "real":
        s.check("FOMOD Plus restored last time's picks by itself (Tall was ticked when the page opened)", opened.get("Tall") is True, str(opened))
    comp_before = {o["name"]: o["checked"] for g in (pages.get("Compatibility") or {}).get("before", []) for o in g["options"]}
    s.notes["compatibility_page_as_opened"] = comp_before
    s.finding("the Village patch as the dialog opened on the reinstall", str(comp_before.get("Village patch")))
    idle = run.c.send("wait_plus_idle")["data"]
    s.check("the rebuild finished", idle["idle"], str(idle))
    if not (result.get("mod") or {}).get("enabled"):
        enable_mod(run, s, grass, (fixtures.GRASS_PATCH_PLUGIN,))
    record = plus_record_of(run, grass)
    s.check("the saved choices now have the Village patch picked", ("Compatibility", "Compatibility patches", "Village patch")
            in record_picks(record["parsed"] or {"steps": []}), str(record_picks(record["parsed"] or {"steps": []})))
    events = plus_events(grass)
    s.check("a second install of Grass is stored; the first is untouched", len(events) == 2 and events[1]["event_id"] == first[0]["event_id"],
            str([e["event_id"] for e in events]))
    if len(events) == 2:
        s.check("the new install is the newest and is marked a reinstall", events[0]["source"]["reinstallation"] is True
                and events[0]["completed_at"] > events[1]["completed_at"], json.dumps(events[0]["source"]))
        s.check("the new install has the Village patch picked, the old one skipped",
                (option_of(events[0], "Village patch") or {}).get("selected") is True
                and (option_of(events[1], "Village patch") or {}).get("selected") is False)
    state, ms = run.await_window(lambda st: "+1 picked" in (tree_row(st, grass) or {"text": ["", ""]})["text"][1], timeout=8)
    s.timings["picks_note_seen_after_poll_ms"] = ms
    row = tree_row(state, grass)
    s.notes["picks_row_text"] = row and row["text"]
    s.check("the Grass row says '+1 picked'", bool(row and "+1 picked" in row["text"][1]), str(row and row["text"]))
    patch = option_row(row, "Village patch")
    s.check("the Village patch is ticked and marked 'newly picked'", bool(patch and patch[0].startswith("✓") and patch[1].startswith("newly picked")), str(patch))
    others = [o for o in option_rows(row or {}) if o[1].startswith(("newly picked", "was picked")) and "Village patch" not in o[0]]
    s.check("no other option is marked as changed", not others, str(others))
    s.check("no missed-patch card for Grass any more (the patch is picked)", find_card(state, "missed", "Village patch", grass) is None,
            json.dumps([c.get("title") for c in cards(state, "missed")]))
    opened = run.c.send("window_state", expand=True, folds=["changes"])["data"]
    s.check("no 'Changed installers' card (same archive, same installer)", change_card(opened, grass) is None)
    run.shot("p2-newly-picked")
    return s


def scenario_p3(run: Run) -> Scenario:
    s = Scenario("p3", "Mods FOMOD Plus installed before Companion saw them -> More > Scan existing mods rebuilds them in "
                       "the background, with progress, and Cancel works")
    village, grass = fixtures.VILLAGE, fixtures.GRASS
    # The test MO2 also holds a mod FOMOD Plus (an earlier version) really installed, before this edition existed.
    old_mod = "Logger Skyrim QA"
    wanted = {village, grass, old_mod}
    # The edition decides a moment after each install (2.5 s) whether it was a plain one; let that pass before the
    # records it relies on are taken away below, or it would see a mod it has no record of.
    run.settle(3.5)
    state = plus_state(run)
    s.check("FOMOD Plus's records are remembered for both mods before they are forgotten",
            len(state["remembered"]) >= 2, json.dumps(state["remembered"])[:300])
    before = {mod: plus_events(mod)[0] for mod in (village, grass)}
    forgotten = run.c.send("forget_plus_events", mods=[village, grass])["data"]
    s.check("their installs were forgotten, as on a PC where FOMOD Plus installed them earlier", forgotten["rows_deleted"] >= 3, str(forgotten))
    state_now, _ms = run.await_window(lambda st: tree_row(st, village) is None and tree_row(st, grass) is None)
    s.check("the window no longer lists them", tree_row(state_now, village) is None and tree_row(state_now, grass) is None)
    s.check("no LiveTest install is stored", not [e for e in plus_events() if e["mod"]["name"].startswith("LiveTest")])
    # What the scan will do: rebuild the three FOMOD Plus installs, and record every other mod that has no record as a
    # plain install (the mods the test MO2 holds, and the two dropped into its mods folder).
    jobs = run.c.send("scan_jobs")["data"]
    rebuild = sorted(name for name, kind in jobs["jobs"] if kind == "RebuildJob")
    plain_jobs = sorted(name for name, kind in jobs["jobs"] if kind == "PlainJob")
    s.notes["scan_candidates"] = jobs
    s.check("the scan will rebuild the three FOMOD Plus installs and record the rest as plain installs",
            rebuild == sorted(wanted) and {fixtures.DROPPED, fixtures.DROPPED_BIG} <= set(plain_jobs)
            and not set(plain_jobs) & wanted, json.dumps(jobs)[:400])
    total = jobs["count"]
    # First scan: press Cancel at once. The scan stops before its next mod (or has already done them all).
    first = run.c.send("scan_existing", cancel_at=0, shot=str(run.shots / "p3-scan-dialog-running.png"))["data"]
    s.notes["scan_with_cancel"] = first
    s.check("More > Scan existing mods… found every mod to record: the three FOMOD Plus installs and the plain ones",
            first["candidates"] == total and first["started"], json.dumps(first)[:300])
    s.check("the dialog opened busy and with a Cancel button", first["first"]["button"] == "Cancel", json.dumps(first["first"]))
    s.check("the dialog finished (not stuck running) and offers Close", first["running"] is False and first["button"] == "Close", json.dumps(first)[:300])
    summary = first["summary"] or {}
    s.check("the summary adds up", summary.get("total") == total and summary.get("rebuilt", 0) + summary.get("names_only", 0)
            + summary.get("plain", 0) + summary.get("failed", 0) + summary.get("skipped", 0) <= total, json.dumps(summary))
    s.finding("the Cancel press landed " + ("before any mod was done" if first["cancel_pressed_at"] == 0 else
                                            f"after {first['cancel_pressed_at']} mod(s)" if first["cancel_pressed_at"] is not None
                                            else "too late: the scan had already finished"),
              f"cancelled={summary.get('cancelled')} done={summary.get('rebuilt', 0) + summary.get('names_only', 0)}")
    run.c.send("close_scan_dialog")
    stored = {e["mod"]["name"] for e in plus_events() if e["mod"]["name"] in wanted}
    s.check("only what was done before Cancel is stored (never more than the mods)", stored <= wanted, str(stored))
    # Second scan: whatever is left.
    jobs_left = run.c.send("scan_jobs")["data"]
    second = run.c.send("scan_existing", shot=str(run.shots / "p3-scan-dialog-done.png"))["data"]
    s.notes["scan_rest"] = second
    left = 3 - len(stored)
    every = jobs_left["count"]
    done = second.get("summary") or {}
    if every:
        s.check(f"the second scan did what was left: the {left} FOMOD Plus install(s) rebuilt, the other mods recorded",
                second["started"] and second["candidates"] == every and done.get("rebuilt", 0) + done.get("names_only", 0) == left
                and done.get("failed") == 0 and done.get("rebuilt", 0) + done.get("names_only", 0) + done.get("plain", 0) == every,
                json.dumps(second)[:400])
        s.check("its dialog shows progress 'n of n', says Done, and offers Close", second["progress"] == [every, every]
                and second["status"] == "Done." and second["button"] == "Close" and f"{every} of {every}" in second["bar_text"], json.dumps(second)[:300])
    else:
        s.check("the second scan had nothing left and said so", not second["started"] and second["candidates"] == 0, json.dumps(second)[:300])
    run.c.send("close_scan_dialog")
    old = plus_events(old_mod)
    s.check(f"{old_mod} (installed by FOMOD Plus long before): one install is stored", len(old) == 1, str(len(old)))
    if old:
        picks = [pick[2] for pick in event_picks(old[0])]
        s.notes["old_mod_event"] = {"source": old[0]["source"], "picks": event_picks(old[0])}
        s.check(f"{old_mod}: the options FOMOD Plus recorded are the picks ('Balanced', 'Enabled')", "Balanced" in picks and "Enabled" in picks, str(picks))
        s.finding(f"{old_mod} was " + ("rebuilt from its download archive" if old[0]["installer"]["name"].startswith("Modlist Companion (reconstructed")
                                       else "stored with the option names only (its archive could not be used)"),
                  " | ".join(old[0]["source"].get("reconstruction_notes", []))[:300])
    for mod in (village, grass):
        events = plus_events(mod)
        s.check(f"{mod}: exactly one install is stored again", len(events) == 1, str(len(events)))
        if events:
            event = events[0]
            s.check(f"{mod}: it was rebuilt from the archive, and says it was found by the scan",
                    event["source"].get("reconstructed") is True and "installed before Modlist Companion saw it"
                    in " ".join(event["source"].get("reconstruction_notes", [])), json.dumps(event["source"])[:300])
            s.check(f"{mod}: it has the same picks as the install the scan replaced",
                    sorted(event_picks(event)) == sorted(event_picks(before[mod])), f"{event_picks(event)} vs {event_picks(before[mod])}")
            s.check(f"{mod}: it is not marked a reinstall", event["source"]["reinstallation"] is False)
            s.check(f"{mod}: it is dated when the mod's folder was made, not now", event["completed_at"] < before[mod]["completed_at"],
                    f"{event['completed_at']} vs {before[mod]['completed_at']}")
    state, _ms = run.await_window(lambda st: tree_row(st, village) is not None and tree_row(st, grass) is not None)
    s.check("the window lists both mods again", tree_row(state, village) is not None and tree_row(state, grass) is not None)
    patch = option_row(tree_row(state, grass), "Village patch")
    s.check("Grass's rebuilt row shows the Village patch picked", bool(patch and patch[0].startswith("✓")), str(patch))
    again = run.c.send("scan_existing")["data"]
    s.check("with everything scanned, the menu item has nothing to do", again["started"] is False and again["candidates"] == 0, json.dumps(again))
    scanned = {e["mod"]["name"]: e for e in store_events(kind="plain", with_files=True)}
    s.check("the mods the scan could not rebuild are recorded as plain installs, dated by their folders",
            {fixtures.DROPPED, fixtures.DROPPED_BIG} <= set(scanned) and not set(scanned) & wanted
            and all(e["plain"]["origin"] == "scan" for e in scanned.values()), str(sorted(scanned)))
    s.check("the big mod has all its files", scanned.get(fixtures.DROPPED_BIG, {"plain": {}})["plain"].get("file_count") == fixtures.DROPPED_BIG_FILES
            and scanned[fixtures.DROPPED_BIG]["plain"]["files_truncated"] is False)
    s.check("the scan never wrote anywhere but the edition's own data folder", not (PLUGINS / "data" / "fomod_logger").exists())
    run.shot("p3-after-scan")
    return s


def scenario_p4(run: Run) -> Scenario:
    s = Scenario("p4", "FOMOD Plus switched off -> the MO2 problem 'needs FOMOD Plus'; switched back on -> gone")
    state = plus_state(run)
    s.check("FOMOD Plus is enabled to start with and there is no problem", state["fomod_plus_enabled"] and not state["problems"], json.dumps(state)[:300])
    switched = False
    try:
        run.c.send("set_persistent", plugin="FOMOD Plus", key="enabled", value=False)
        switched = True
        off = plus_state(run)
        s.notes["problems_while_off"] = off["problems"]
        s.check("MO2 now says FOMOD Plus is not enabled", off["fomod_plus_enabled"] is False)
        keys = [p["key"] for p in off["problems"]]
        s.check("the edition shows one problem (11)", keys == [11], json.dumps(off["problems"]))
        if off["problems"]:
            s.check("its text is the one asked for", off["problems"][0]["short"] == (
                "Modlist Companion (for FOMOD Plus) needs FOMOD Plus; install it or use the standard edition"), off["problems"][0]["short"])
        pending = run.c.send("window_state")["data"]
        s.check("the window still opens and reads what was stored", not pending["error"], str(pending["error"]))
    finally:
        if switched:
            run.c.send("set_persistent", plugin="FOMOD Plus", key="enabled", value=True)
    on = plus_state(run)
    s.check("switched back on: FOMOD Plus is enabled again and the problem is gone", on["fomod_plus_enabled"] is True and not on["problems"],
            json.dumps(on["problems"]))
    return s


def scenario_p5(run: Run) -> Scenario:
    s = Scenario("p5", "Every mod has a record in this edition too: a plain zip FOMOD Plus did not handle gets a plain record, "
                       "FOMOD Plus's own installs get none, and FOMOD Plus's record replaces a plain one")
    plain = fixtures.PLAIN
    earlier = store_events(kind="plain")
    names = {e["mod"]["name"] for e in earlier}
    s.check("(setup) the mods FOMOD Plus installed got no plain record of their own: only the scan's records of the other "
            "mods exist", not names & {fixtures.VILLAGE, fixtures.GRASS}, str(sorted(names)))
    if plain in run.mods():
        run.c.send("remove_mod", name=plain)
    result = run.install(s, plain, {})
    s.check("it was installed without an installer dialog (a plain zip: FOMOD Plus's installer does not take it)",
            not result["session"].get("dialog_seen"), json.dumps(result["session"]["events"])[:300])
    began = time.perf_counter()
    records = wait_records(plain, 1, "plain")
    s.timings["the plain record was stored after (ms, from the end of the install)"] = round((time.perf_counter() - began) * 1000)
    s.check("a plain record is written a moment after the install", len(records) == 1, str(len(records)))
    if records:
        check_plain_record(s, records[0], archive="LiveTest-Plain-1.0.0.zip", files=PLAIN_FILES_V1, origin="install",
                           plugin_masters=[], reinstall=False, label="install 1.0.0")
    s.check("this edition wrote no event folder (it has no installer)", not (LOGGER_DATA / "events").exists())
    state, ms = run.await_window(lambda st: tree_row(st, plain) is not None, timeout=20.0)
    s.timings["the plain row seen after (ms, polling the window)"] = ms
    row = tree_row(state, plain)
    s.check("the window lists it with a quiet row: 'no installer options' and the archive", bool(row)
            and row["text"][1].startswith("no installer options  ·  LiveTest-Plain-1.0.0.zip"), json.dumps(row and row["text"]))
    answer = run.c.send("which_option", query="textures/livetest/plain/plain.txt", close=True)["data"]
    s.check("'Which option installed…?' finds its file", len(answer["results"]) == 1 and answer["results"][0].startswith(
        "LiveTest Plain (not installed through an installer)"), json.dumps(answer["results"])[:300])
    state = plus_state(run)
    s.check("MO2's problem list has nothing for the edition", not state["problems"], json.dumps(state["problems"]))
    run.shot("p5-plain-row")

    # FOMOD Plus's record for the same mod (a reinstall through FOMOD Plus): it replaces the plain record.
    plain_before = len(store_events(kind="plain"))
    plus_install(run, s, plain, {"Main": {"Plugins": ["Orphan patch"]}}, plus_json(("Main", [("Plugins", ["Orphan patch"], [])])),
                 archive=fixtures.ORPHAN)
    time.sleep(5.0)       # longer than the plugin waits before it decides an install was a plain one
    after = store_events(plain)
    s.check("FOMOD Plus's install over it is the mod's newest record, rebuilt from FOMOD Plus's record", bool(after)
            and after[0]["kind"] == "fomod" and after[0]["source"].get("reconstructed") is True, json.dumps([(e["kind"], e["source"]["archive"]) for e in after]))
    s.check("no plain record was written for FOMOD Plus's install; the earlier one is history",
            len(store_events(kind="plain")) == plain_before, f"{plain_before} before, {len(store_events(kind='plain'))} after")
    state, _ms = run.await_window(lambda st: "no installer options" not in (tree_row(st, plain) or {"text": ["", ""]})["text"][1], timeout=12.0)
    row = tree_row(state, plain)
    s.check("the window shows it as an installer install again", bool(row) and "no installer options" not in row["text"][1], json.dumps(row and row["text"]))

    # Removal.
    run.c.send("remove_mod", name=plain)
    run.settle(1.0)
    after = store_events(plain)
    s.check("removing the mod marks its newest record removed", bool(after) and after[0]["removed_at"] is not None)
    s.check("the window no longer lists it", tree_row(run.window(), plain) is None)
    return s


def scenario_plus_cleanup(run: Run) -> Scenario:
    s = Scenario("plus-cleanup", "Remove the test mods; the window drops them and the installs are marked removed")
    for mod in (fixtures.GRASS, fixtures.VILLAGE):
        measured = run.c.send("remove_mod", name=mod)["data"]
        run.record_update(s, f"remove {mod}", measured)
        value = measured["value"] or {}
        s.check(f"{mod} removed from MO2", value.get("removed") and not value.get("still_listed"), str(value))
        s.check(f"window reacted to removing {mod} within 2 s", bool(measured["changed"] and measured["first_change_ms"] <= 2000),
                f"{measured.get('first_change_ms')} ms")
    state = run.window()
    s.check("no LiveTest row remains in the window", not [r for r in state["tree"] if r["text"][0].startswith("LiveTest")],
            str([r["text"][0] for r in state["tree"]]))
    removed = [e for e in plus_events() if e["mod"]["name"].startswith("LiveTest")]
    newest = {}
    for event in removed:                    # newest first: a mod's older installs (history) stay as they were
        newest.setdefault(event["mod"]["name"].casefold(), event)
    s.check("every LiveTest mod's newest install is marked removed in the data", newest and all(e["removed_at"] for e in newest.values()),
            f"{len(removed)} installs of {len(newest)} mods")
    now = plus_state(run)
    s.check("what the edition remembered about the records is gone", not [k for k in now["remembered"] if k.startswith("livetest")],
            json.dumps(now["remembered"]))
    s.check("no problem in MO2's list at the end", not now["problems"], json.dumps(now["problems"]))
    left = [m for m in run.mods() if m.startswith("LiveTest")]
    s.check("no LiveTest mod left in MO2", not left, str(left))
    run.shot("z-plus-after-cleanup")
    return s


def scenario_plus_bare(run: Run) -> Scenario:
    """Diagnostic (--without-companion): FOMOD Plus installs a mod through the driver with none of our plugin in MO2, to
    tell a problem of FOMOD Plus itself from one of the edition."""

    s = Scenario("plus-bare", "FOMOD Plus alone: install Village from the driver, no Companion plugin in MO2")
    mod, choices = fixtures.VILLAGE, {"Edition": {"Edition": ["Standard"]},
                                      "Standard extras": {"Extras": {"Lanterns": True, "Bridges": False}}}
    result = run.install(s, mod, choices)
    record = run.c.send("mod_record", name=mod)["data"]
    s.check("MO2 has FOMOD Plus's saved choices for the mod", bool(record["record"]), str(record["record"])[:200])
    run.c.send("remove_mod", name=mod, measure=False)
    return s


SCENARIOS_PLUS = [scenario_plus_setup, scenario_p1, scenario_p2, scenario_p3, scenario_p4, scenario_p5, scenario_p6,
                  scenario_p7, scenario_plus_cleanup]
SCENARIOS = [scenario_setup, scenario_a, scenario_b, scenario_c, scenario_d, scenario_e, scenario_bridge, scenario_f,
             scenario_g, scenario_h, scenario_i, scenario_j, scenario_k, scenario_l, scenario_m, scenario_n, scenario_cleanup,
             scenario_bridge_missing_archive]
BRIDGE_ONLY = (scenario_bridge, scenario_bridge_missing_archive)


# ---------------------------------------------------------------- main flow

def summarize(results: dict) -> str:
    lines = [f"Modlist Companion live run  {results['started']}  ({results['duration_s']} s)  backend: {results.get('backend')}",
             f"MO2: {results.get('mo2_version')}  profile: {results.get('profile')}",
             f"The test MO2 was running at the start: {bool(results['mo2_before'].get('was_running'))}; "
             f"close result: {results['mo2_before'].get('closed')}", ""]
    for s in results["scenarios"]:
        lines.append(f"[{'PASS' if s['passed'] else 'FAIL'}] {s['id']}: {s['title']}")
        for finding in s.get("findings", []):
            lines.append(f"      * FINDING: {finding['name']}")
            if finding.get("detail"):
                lines.append(f"          {finding['detail'][:400]}")
        if s["error"]:
            lines.append(f"      ERROR: {s['error'].splitlines()[0]}")
        for c in s["checks"]:
            if not c["ok"]:
                lines.append(f"      - FAILED: {c['name']}  [{c['detail'][:200]}]")
    lines += ["", "Window updates in ms. First change counts from the start of the call; for installs, from the end of installMod:"]
    for u in results["updates"]:
        lines.append(f"  {u['scenario']:8} {u['update'][:62]:62} first change={u.get('first_change_ms')} "
                     f"(after the call returned: {u.get('first_change_after_action_ms')})  redraws={u.get('refreshes')}  "
                     f"load={u.get('load_ms')}  render={u.get('render_ms')}"
                     + (f"  signals={u['signals']}" if u.get("signals") else ""))
    if results.get("bridge_calls"):
        lines += ["", "Bridge calls (count, min / median / max ms):"]
        for action, row in results["bridge_calls"]["by_action"].items():
            lines.append(f"  {action:16} {row['count']:4}  {row['min_ms']} / {row['median_ms']} / {row['max_ms']}")
        lines.append(f"  refusals: {json.dumps(results['bridge_calls']['refusals'])[:600]}")
    if results.get("aborted"):
        lines += ["", f"Run aborted: {results['aborted']}"]
    if results.get("driver_fault_locator"):
        lines += ["", "Where the first faults were (module+offset):", results["driver_fault_locator"]]
    if results.get("driver_fault_log"):
        lines += ["", "Python fault log (MO2 aborted or crashed):", results["driver_fault_log"]]
    lines += ["", f"Restore: {results['restore']}"]
    return "\n".join(lines)


def run_everything(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logfile = out / "run.log"

    def log(message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        with logfile.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    state_file = out / "run-state.json"
    plus = EDITION == "plus"
    if plus:
        args.backend = "driver"          # the bridge is the full edition's tool; the Plus run uses the driver alone
    if (state_file.exists() or DRIVER_DST.exists() or COMPARE_PROFILE.exists()
            or (plus and not USE_INSTALLED and (PLUS_PLUGIN_DST.exists() or SUPPORT_DST.exists()))):
        raise SystemExit("A previous run left state behind. Run 'recover' first.")
    started = time.time()
    results: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "edition": EDITION, "backend": args.backend,
                     "scenarios": [], "updates": []}
    results["mo2_before"] = ensure_no_other_mo2(args.close_test_mo2, log)
    if PROFILE_NAME not in INI.read_text(encoding="utf-8", errors="replace"):
        raise SystemExit("ModOrganizer.ini does not select the Logger QA profile; stopping.")
    if USE_INSTALLED:
        results["installed"] = check_installed(log)
    manifest = make_backup(out, log)
    process = None
    host: dict = {"process": None}
    client = Client()
    bridge = None
    try:
        fixtures_dir = out / "fixtures"
        archives = fixtures.build_all(fixtures_dir)
        # Two mods nobody installed (and so no record of): "Scan existing mods…" finds them. restore() takes them out.
        fixtures.dropped(MO2 / "mods")
        log(f"dropped {fixtures.DROPPED} and {fixtures.DROPPED_BIG} ({fixtures.DROPPED_BIG_FILES:,} files) into the mods folder")
        if plus and args.without_companion:
            log("--without-companion: the Plus edition is NOT deployed (a diagnostic of FOMOD Plus alone)")
        elif plus and USE_INSTALLED:
            log("--use-installed: the Plus edition is NOT deployed from source; the installed package is tested")
        elif plus:
            results["deployed"] = deploy_plus(log)
        else:
            make_compare_profile(log)
        if plus:
            make_compare_profile(log)        # the profile-switch scenario (p7) needs a second profile here too
        shutil.copy2(HERE / "driver_plugin.py", DRIVER_DST)
        client.prepare()
        deployed = find_bridge(log) if args.backend == "bridge" else None
        log("starting the test MO2")
        process = subprocess.Popen([str(MO2_EXE), "--multiple"], cwd=str(MO2))
        host["process"] = process
        client.process = process
        ready = client.wait_ready(process)
        results["ready"] = ready
        log(f"MO2 ready after {ready.get('elapsed'):.1f} s on {ready.get('screen')}")
        if deployed:
            bridge, bridge_info = connect_bridge(deployed, process, log)
            results["bridge"] = {"build": deployed["build"], "pipe_name": deployed["pipe_name"], **bridge_info}
            deployed["token"] = None
        run = Run(client, out, archives, log, bridge, host)
        scenarios = ([scenario_plus_bare] if plus and args.without_companion else list(SCENARIOS_PLUS) if plus
                     else [f for f in SCENARIOS if bridge is not None or f not in BRIDGE_ONLY])
        if args.only:
            # A few scenarios while developing (the setup one always runs): their ids, as in the summary.
            wanted = {part.strip() for part in args.only.split(",") if part.strip()}
            scenarios = [f for f in scenarios if f.__name__.replace("scenario_", "").replace("_", "-") in wanted
                         or f.__name__ in ("scenario_setup", "scenario_plus_setup")]
        for function in scenarios:
            scenario = None
            try:
                scenario = function(run)
            except Exception as exc:
                scenario = Scenario(function.__name__, function.__doc__ or function.__name__)
                scenario.id = function.__name__.replace("scenario_", "")
                scenario.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                log(f"scenario {scenario.id} failed: {exc}")
            run.scenarios.append(scenario)
            log(f"{scenario.id}: {'PASS' if scenario.passed else 'FAIL'}")
            if args.keep_open and function is scenarios[-1]:
                break
        results["scenarios"] = [s.as_dict() for s in run.scenarios]
        results["updates"] = run.updates
        if bridge is not None:
            results["bridge_calls"] = bridge_call_summary(bridge)
        ping = client.send("ping")["data"]
        results["mo2_version"] = ping.get("version")
        results["profile"] = ping.get("profile")
        results["driver_command_sources"] = ping.get("command_sources")
        if args.keep_open:
            log("--keep-open: leaving MO2 and the driver in place; use 'finish' to clean up")
            results["restore"] = "not yet (keep-open)"
            results["duration_s"] = round(time.time() - started, 1)
            (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
            return 0
        shutdown(client, host["process"], log)
    except BaseException as exc:
        log(f"run aborted: {type(exc).__name__}: {exc}")
        results["aborted"] = f"{type(exc).__name__}: {exc}"
        try:
            shutdown(client, host["process"], log)
        except Exception as inner:
            log(f"shutdown trouble: {inner}")
    results["driver_log_tail"] = _driver_log_tail()
    for key, name in (("driver_fault_log", "fault.log"), ("driver_fault_locator", "fault-locator.log")):
        fault = DRIVER_DATA / name
        if fault.exists() and fault.stat().st_size:
            results[key] = fault.read_text(encoding="utf-8", errors="replace")[:6000 if name == "fault-locator.log" else 3000]
    results["restore"] = restore(manifest, log)
    if FORCED_ENDS:
        results["mo2_force_ended"] = list(FORCED_ENDS)       # the run fails below: a killed MO2 is a defect to look at
    results["duration_s"] = round(time.time() - started, 1)
    (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    (out / "summary.txt").write_text(summarize(results) if results["scenarios"] else json.dumps(results, indent=1), encoding="utf-8")
    if results["restore"].get("restored"):
        shutil.rmtree(Path(manifest["backup"]))
        state_file.unlink(missing_ok=True)
        log("restore verified; backup removed")
    else:
        log(f"RESTORE PROBLEMS, backup kept: {results['restore']}")
    log("done")
    print((out / "summary.txt").read_text(encoding="utf-8"))
    return 0 if all(s["passed"] for s in results["scenarios"]) and not results.get("aborted") and not FORCED_ENDS else 1


def bridge_call_summary(bridge: BridgeClient) -> dict:
    """What the run asked of the bridge: counts and timings per action, and every refusal (no secrets in either)."""

    by_action: dict = {}
    for call in bridge.calls:
        by_action.setdefault(call["action"], []).append(call["ms"])
    rows = {}
    for action, times in sorted(by_action.items()):
        times = sorted(times)
        rows[action] = {"count": len(times), "min_ms": times[0], "median_ms": times[len(times) // 2], "max_ms": times[-1]}
    return {"total": len(bridge.calls), "by_action": rows,
            "refusals": [{k: v for k, v in r.items() if k != "at"} for r in bridge.refusals]}


def _driver_log_tail() -> list[str]:
    path = DRIVER_DATA / "driver.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]


def shutdown(client: Client, process, log) -> None:
    if process is None:
        return
    if process.poll() is None:
        try:
            client.send("quit", timeout=15)
        except Exception as exc:
            log(f"quit command failed ({exc}); sending WM_CLOSE")
            post_close(process.pid)
        end = time.time() + 60
        while time.time() < end and process.poll() is None:
            time.sleep(0.5)
        if process.poll() is None:
            log(close_pid(process.pid, log))
    log(f"MO2 exited with code {process.returncode}")
    time.sleep(1.5)


def finish(args) -> int:
    out = Path(args.out)
    manifest = json.loads((out / "run-state.json").read_text(encoding="utf-8"))
    log = print
    for pid, _path in mo2_processes():
        try:
            Client().send("quit", timeout=15)
        except Exception:
            post_close(pid)
        if not wait_exit(pid, 60):
            close_pid(pid, log)
    time.sleep(1.5)
    result = restore(manifest, log)
    print(result)
    if result["restored"]:
        shutil.rmtree(Path(manifest["backup"]))
        (out / "run-state.json").unlink()
    return 0 if result["restored"] else 1


def send(args) -> int:
    payload = json.loads(args.arguments) if args.arguments else {}
    print(json.dumps(Client().send(args.command, **payload), indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action")
    for name in ("run", "finish", "recover"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--edition", choices=tuple(EDITIONS), default="full",
                       help="full: the full edition in test-env/MO2-Skyrim-Logger (default); plus: the FOMOD Plus edition "
                            "in test-env/MO2-Skyrim-Plus")
        if name == "run":
            p.add_argument("--close-test-mo2", action="store_true")
            p.add_argument("--plus-installer", choices=("emulated", "real"), default="emulated",
                           help="plus only: emulated (default): MO2's own FOMOD installer installs and the FOMOD Plus choices are "
                                "saved through the plugin-setting API; real: FOMOD Plus installs (1.20.0 aborts MO2 at the end)")
            p.add_argument("--without-companion", action="store_true",
                           help="plus only, a diagnostic: leave our plugin out and only let FOMOD Plus install a mod")
            p.add_argument("--use-installed", action="store_true",
                           help="test the files already installed in the test MO2 from a release package (checked against "
                                "the package manifest) instead of deploying the Plus edition from this source tree")
            p.add_argument("--keep-open", action="store_true")
            p.add_argument("--only", default="",
                           help="run only these scenarios (comma-separated ids such as k,l or p5), after the setup one; "
                                "for development: a full run is every scenario")
            p.add_argument("--backend", choices=("bridge", "driver"), default="bridge",
                           help="bridge: installs, mod and plugin changes and reads go through the ModLabs bridge "
                                "(default); driver: everything through the driver plugin")
    p = sub.add_parser("send")
    p.add_argument("command")
    p.add_argument("arguments", nargs="?")
    p.add_argument("--edition", choices=tuple(EDITIONS), default="full")
    args = parser.parse_args()
    use_edition(getattr(args, "edition", "full"))
    global PLUS_INSTALLER, USE_INSTALLED
    PLUS_INSTALLER = getattr(args, "plus_installer", "emulated")
    USE_INSTALLED = bool(getattr(args, "use_installed", False))
    if args.action in (None, "run"):
        if args.action is None:
            parser.print_help()
            return 2
        return run_everything(args)
    if args.action in ("finish", "recover"):
        return finish(args)
    return send(args)


if __name__ == "__main__":
    sys.exit(main())
