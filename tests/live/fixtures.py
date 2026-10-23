"""Small real FOMOD archives (zip) for the live driver. Test-only; never shipped.

Three mods (and a newer version of one of them), all named "LiveTest ..." so they cannot be mistaken for anything in
the test MO2's own list:

* LiveTest Village   - the mod others patch. Several steps, a SelectExactlyOne
  group, a SelectAny group and a page that only shows for one edition (flag).
* LiveTest Grass     - offers a patch for Village that is auto-detected
  (a fileDependency on LT_Village.esp turns it into "Recommended"), and a
  patch for a Lanterns mod that is never installed (control: must not be flagged).
* LiveTest Grass 2.2.0 - the update of Grass (``GRASS_NEXT``, installed over 2.1.0): its installer adds a patch
  for Village (``GLOW_PATCH``, auto-detected like the first one) and renames the "Windy" option.
* LiveTest Orphan    - its patch plugin has a master (LT_Missing.esp) that is
  never installed, for the "missing masters" case.
* LiveTest Layout    - a file and folder layout for "which option put this here?": two folders that land on the same
  destination, two files that write the same path (one with a priority), a file with no destination name, a plugin,
  and an automatic (conditional) install.
* LiveTest Plain     - a plain zip: no fomod folder, so MO2's simple installer installs it and no installer records it.
  Version 1.0.0, and 1.1.0 (``PLAIN_NEXT``) which changes its files and gives its plugin a master that is never installed.

The plugin files are tiny but valid: a TES4 header with HEDR and MAST/DATA
subrecords, so MO2 reads their masters.

Run on its own to write the archives: ``python fixtures.py <folder>``.
"""

from __future__ import annotations

import struct
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

VILLAGE = "LiveTest Village"
GRASS = "LiveTest Grass"
GRASS_NEXT = "LiveTest Grass 2.2.0"   # not a mod name: the key of the update's archive; it installs as GRASS
ORPHAN = "LiveTest Orphan"
LAYOUT = "LiveTest Layout"
LAYOUT_COPY = "LiveTest Layout Copy"   # a second install of the Layout archive under another name (scenario j)
PLAIN = "LiveTest Plain"
PLAIN_NEXT = "LiveTest Plain 1.1.0"    # not a mod name: the key of the update's archive; it installs as PLAIN
PLAIN_PLUGIN = "LT_Plain.esp"
PLAIN_DIR = "textures/livetest/plain"
# Mods dropped into the test MO2's mods folder before it starts (no record of them exists): a small one, and one with
# enough files that a scan of it takes a moment (and can be cancelled inside it). Neither is called "LiveTest...".
DROPPED = "Dropped QA Small"
DROPPED_BIG = "AAA Dropped QA Big"
DROPPED_PLUGIN = "DroppedQA.esp"
DROPPED_BIG_FILES = 12_000

VILLAGE_PLUGIN = "LT_Village.esp"
VILLAGE_LITE_PLUGIN = "LT_VillageLite.esp"
GRASS_PATCH_PLUGIN = "LT_Grass_Village.esp"
LANTERNS_PATCH_PLUGIN = "LT_Grass_Lanterns.esp"
GLOW_PATCH = "Village glow patch"           # the option 2.2.0 adds
GLOW_PLUGIN = "LT_Grass_VillageGlow.esp"
WINDY_OLD, WINDY_NEW = "Windy", "Windy grass"  # the option 2.2.0 renames
LANTERNS_PLUGIN = "LT_Lanterns.esp"  # never installed
ORPHAN_PLUGIN = "LT_Orphan.esp"
MISSING_MASTER = "LT_Missing.esp"  # never installed
LAYOUT_PLUGIN = "LT_Layout.esp"
LAYOUT_DIR = "textures/livetest/layout"


def plugin_bytes(masters: tuple[str, ...] = (), author: str = "LiveTest") -> bytes:
    """A minimal Skyrim SE plugin: TES4 header record with HEDR, CNAM, MAST/DATA."""

    def sub(kind: bytes, data: bytes) -> bytes:
        return kind + struct.pack("<H", len(data)) + data

    data = sub(b"HEDR", struct.pack("<fiI", 1.7, 1, 0x800))
    data += sub(b"CNAM", author.encode("ascii") + b"\0")
    for master in masters:
        data += sub(b"MAST", master.encode("ascii") + b"\0")
        data += sub(b"DATA", struct.pack("<Q", 0))
    header = b"TES4" + struct.pack("<IIIIHH", len(data), 0, 0, 0, 44, 0)
    return header + data


# ----- ModuleConfig.xml helpers -----

def _files(*pairs: tuple[str, str]) -> str:
    if not pairs:
        return ""
    body = "".join(f"<file source={quoteattr(s)} destination={quoteattr(d)} />" for s, d in pairs)
    return f"<files>{body}</files>"


def _entries(*items: tuple[str, str, str | None, int | None]) -> str:
    """<files> from (tag, source, destination or None for no attribute, priority or None) tuples."""

    body = ""
    for tag, source, destination, priority in items:
        attributes = f"source={quoteattr(source)}"
        if destination is not None:
            attributes += f" destination={quoteattr(destination)}"
        if priority is not None:
            attributes += f' priority="{priority}"'
        body += f"<{tag} {attributes} />"
    return f"<files>{body}</files>"


def _flags(**flags: str) -> str:
    if not flags:
        return ""
    return "<conditionFlags>" + "".join(
        f"<flag name={quoteattr(k)}>{escape(v)}</flag>" for k, v in flags.items()) + "</conditionFlags>"


def _plugin(name: str, description: str, files: str = "", flags: str = "", detects: str = "") -> str:
    if detects:
        # A fileDependency pattern: "Recommended" while that plugin is active.
        kind = ('<dependencyType><defaultType name="Optional"/><patterns><pattern>'
                f'<dependencies operator="And"><fileDependency file={quoteattr(detects)} state="Active"/></dependencies>'
                '<type name="Recommended"/></pattern></patterns></dependencyType>')
    else:
        kind = '<type name="Optional"/>'
    return (f"<plugin name={quoteattr(name)}><description>{escape(description)}</description>"
            f"{flags}{files}<typeDescriptor>{kind}</typeDescriptor></plugin>")


def _group(name: str, kind: str, plugins: list[str]) -> str:
    return (f"<group name={quoteattr(name)} type={quoteattr(kind)}>"
            f'<plugins order="Explicit">{"".join(plugins)}</plugins></group>')


def _step(name: str, groups: list[str], visible: tuple[str, str] | None = None) -> str:
    cond = (f"<visible><flagDependency flag={quoteattr(visible[0])} value={quoteattr(visible[1])}/></visible>"
            if visible else "")
    return (f"<installStep name={quoteattr(name)}>{cond}"
            f'<optionalFileGroups order="Explicit">{"".join(groups)}</optionalFileGroups></installStep>')


def _config(module: str, steps: list[str], conditional: str = "", required: str = "") -> bytes:
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
           'xsi:noNamespaceSchemaLocation="http://qconsulting.ca/fo3/ModConfig5.0.xsd">'
           f"<moduleName>{escape(module)}</moduleName>{required}"
           f'<installSteps order="Explicit">{"".join(steps)}</installSteps>{conditional}</config>')
    return xml.encode("utf-8")


def _info(name: str, version: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><fomod><Name>{escape(name)}</Name>'
            f"<Author>LiveTest</Author><Version>{version}</Version></fomod>").encode("utf-8")


def _write_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def _asset(label: str) -> bytes:
    return f"live test asset: {label}\n".encode("ascii")


# ----- the three archives -----

def village(folder: Path) -> Path:
    steps = [
        _step("Edition", [_group("Edition", "SelectExactlyOne", [
            _plugin("Standard", "The full village.", _files(("core/LT_Village.esp", VILLAGE_PLUGIN),
                                                            ("core/standard.txt", "textures/livetest/village_standard.txt")),
                    _flags(edition="standard")),
            _plugin("Lite", "A smaller village.", _files(("core/LT_VillageLite.esp", VILLAGE_LITE_PLUGIN)),
                    _flags(edition="lite")),
        ])]),
        _step("Standard extras", [_group("Extras", "SelectAny", [
            _plugin("Lanterns", "Street lanterns.", _files(("extras/lanterns.txt", "meshes/livetest/lanterns.txt"))),
            _plugin("Bridges", "Wooden bridges.", _files(("extras/bridges.txt", "meshes/livetest/bridges.txt"))),
        ])], visible=("edition", "standard")),
        _step("Lite notes", [_group("Notes", "SelectAny", [
            _plugin("Verbose", "Extra notes.", _files(("extras/notes.txt", "meshes/livetest/notes.txt"))),
        ])], visible=("edition", "lite")),
    ]
    files = {
        "fomod/info.xml": _info(VILLAGE, "1.0.0"),
        "fomod/ModuleConfig.xml": _config(VILLAGE, steps),
        "core/LT_Village.esp": plugin_bytes(),
        "core/LT_VillageLite.esp": plugin_bytes(),
        "core/standard.txt": _asset("standard"),
        "extras/lanterns.txt": _asset("lanterns"),
        "extras/bridges.txt": _asset("bridges"),
        "extras/notes.txt": _asset("notes"),
    }
    return _write_zip(folder / "LiveTest-Village-1.0.0.zip", files)


def _grass_steps(*, next_version: bool) -> list[str]:
    windy = WINDY_NEW if next_version else WINDY_OLD
    patches = [
        _plugin("Village patch", "Grass for LiveTest Village.",
                _files(("patches/LT_Grass_Village.esp", GRASS_PATCH_PLUGIN)), detects=VILLAGE_PLUGIN),
    ]
    if next_version:
        patches.append(_plugin(GLOW_PATCH, "Glowing grass for LiveTest Village.",
                               _files(("patches/LT_Grass_VillageGlow.esp", GLOW_PLUGIN)), detects=VILLAGE_PLUGIN))
    patches.append(_plugin("Lanterns patch", "Grass for a Lanterns mod.",
                           _files(("patches/LT_Grass_Lanterns.esp", LANTERNS_PATCH_PLUGIN)), detects=LANTERNS_PLUGIN))
    return [
        _step("Grass", [_group("Length", "SelectExactlyOne", [
            _plugin("Short", "Short grass.", _files(("grass/short.txt", "textures/livetest/grass_short.txt")),
                    _flags(length="short")),
            _plugin("Tall", "Tall grass.", _files(("grass/tall.txt", "textures/livetest/grass_tall.txt")),
                    _flags(length="tall")),
        ])]),
        _step("Tall options", [_group("Wind", "SelectAny", [
            _plugin(windy, "Grass sways.", _files(("grass/wind.txt", "textures/livetest/grass_wind.txt"))),
        ])], visible=("length", "tall")),
        _step("Compatibility", [_group("Compatibility patches", "SelectAny", patches)]),
    ]


def _grass_files(version: str, steps: list[str], *, next_version: bool) -> dict[str, bytes]:
    files = {
        "fomod/info.xml": _info(GRASS, version),
        "fomod/ModuleConfig.xml": _config(GRASS, steps),
        "grass/short.txt": _asset("short"),
        "grass/tall.txt": _asset("tall"),
        "grass/wind.txt": _asset("wind"),
        "patches/LT_Grass_Village.esp": plugin_bytes((VILLAGE_PLUGIN,)),
        "patches/LT_Grass_Lanterns.esp": plugin_bytes((LANTERNS_PLUGIN,)),
    }
    if next_version:
        files["patches/LT_Grass_VillageGlow.esp"] = plugin_bytes((VILLAGE_PLUGIN,))
    return files


def grass(folder: Path) -> Path:
    return _write_zip(folder / "LiveTest-Grass-2.1.0.zip",
                      _grass_files("2.1.0", _grass_steps(next_version=False), next_version=False))


def grass_next(folder: Path) -> Path:
    """Grass 2.2.0: one more patch for Village (auto-detected, so ticked unless skipped) and "Windy" renamed."""

    return _write_zip(folder / "LiveTest-Grass-2.2.0.zip",
                      _grass_files("2.2.0", _grass_steps(next_version=True), next_version=True))


def orphan(folder: Path) -> Path:
    steps = [
        _step("Main", [_group("Plugins", "SelectAny", [
            _plugin("Orphan patch", "A patch for a mod that is not installed.",
                    _files(("patch/LT_Orphan.esp", ORPHAN_PLUGIN))),
            _plugin("Readme", "Notes only.", _files(("patch/readme.txt", "docs/livetest/readme.txt"))),
        ])]),
    ]
    files = {
        "fomod/info.xml": _info(ORPHAN, "0.9.0"),
        "fomod/ModuleConfig.xml": _config(ORPHAN, steps),
        "patch/LT_Orphan.esp": plugin_bytes((MISSING_MASTER,)),
        "patch/readme.txt": _asset("readme"),
    }
    return _write_zip(folder / "LiveTest-Orphan-0.9.0.zip", files)


def layout(folder: Path) -> Path:
    """Files and folders as FOMODs lay them out: 2K and 4K both install a folder onto the same destination; "Peak fix"
    (priority 5) and "Dark peaks" write the same file; "Notes" has an empty destination, so the file keeps its name;
    picking 2K sets a flag that makes a conditional (automatic) install add one more file; one file is
    required (installed whatever is picked)."""

    steps = [
        _step("Textures", [
            _group("Resolution", "SelectExactlyOne", [
                _plugin("2K", "2K textures and the plugin.",
                        _entries(("folder", "2k", "textures\\livetest\\layout", None),
                                 ("file", "core\\LT_Layout.esp", "LT_Layout.esp", None)), _flags(res="2k")),
                _plugin("4K", "4K textures.", _entries(("folder", "4k", "textures\\livetest\\layout", None)),
                        _flags(res="4k")),
            ]),
            _group("Extras", "SelectAny", [
                _plugin("Peak fix", "A better peak.",
                        _entries(("file", "fix\\peak.txt", "textures\\livetest\\layout\\peak.txt", 5))),
                _plugin("Dark peaks", "A darker peak.",
                        _entries(("file", "dark\\peak.txt", "textures\\livetest\\layout\\peak.txt", None))),
                _plugin("Notes", "Notes only.", _entries(("file", "docs\\layout_notes.txt", "", None))),
            ]),
        ]),
    ]
    conditional = ('<conditionalFileInstalls><patterns><pattern><dependencies operator="And">'
                   '<flagDependency flag="res" value="2k"/></dependencies>'
                   + _entries(("file", "auto\\hd.txt", "textures\\livetest\\layout\\auto_2k.txt", None))
                   + "</pattern></patterns></conditionalFileInstalls>")
    required = _entries(("file", "core/required.txt", "textures/livetest/layout/required.txt", None)).replace(
        "files>", "requiredInstallFiles>")
    files = {
        "fomod/info.xml": _info(LAYOUT, "1.0.0"),
        "fomod/ModuleConfig.xml": _config(LAYOUT, steps, conditional, required),
        "core/required.txt": _asset("required"),
        "core/LT_Layout.esp": plugin_bytes(),
        "2k/base.txt": _asset("2k base"),
        "4k/base.txt": _asset("4k base"),
        "fix/peak.txt": _asset("peak fix"),
        "dark/peak.txt": _asset("dark peaks"),
        "docs/layout_notes.txt": _asset("notes"),
        "auto/hd.txt": _asset("auto hd"),
    }
    return _write_zip(folder / "LiveTest-Layout-1.0.0.zip", files)


def plain(folder: Path) -> Path:
    files = {
        "LT_Plain.esp": plugin_bytes(),
        "textures/livetest/plain/plain.txt": _asset("plain 1.0"),
        "meshes/livetest/plain/lamp.txt": _asset("lamp"),
    }
    return _write_zip(folder / "LiveTest-Plain-1.0.0.zip", files)


def plain_next(folder: Path) -> Path:
    files = {
        "LT_Plain.esp": plugin_bytes((MISSING_MASTER,)),
        "textures/livetest/plain/plain.txt": _asset("plain 1.1"),
        "meshes/livetest/plain/lamp2.txt": _asset("lamp 2"),
        "sounds/livetest/plain/chime.txt": _asset("chime"),
    }
    return _write_zip(folder / "LiveTest-Plain-1.1.0.zip", files)


def dropped(mods: Path) -> list[Path]:
    """Make the two dropped mods in a test MO2's mods folder (MO2 must be closed). Returns their folders."""

    small = mods / DROPPED
    for relative, data in ((DROPPED_PLUGIN, plugin_bytes(("Skyrim.esm",))), ("textures/dropped/small.txt", _asset("small")),
                           ("meshes/dropped/a.txt", _asset("a")), ("meta.ini", b"[General]\n")):
        target = small / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    big = mods / DROPPED_BIG
    (big).mkdir(parents=True, exist_ok=True)
    (big / "meta.ini").write_bytes(b"[General]\n")
    for folder in range(DROPPED_BIG_FILES // 100):
        target = big / "textures" / "dropped" / "big" / f"d{folder:03}"
        target.mkdir(parents=True, exist_ok=True)
        for number in range(100):
            (target / f"f{number:03}.txt").write_bytes(b"x")
    return [small, big]


def build_all(folder: Path) -> dict[str, Path]:
    """Write every archive into ``folder``; returns {mod name: archive path} (the Grass update under ``GRASS_NEXT``)."""

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    return {VILLAGE: village(folder), GRASS: grass(folder), GRASS_NEXT: grass_next(folder), ORPHAN: orphan(folder),
            LAYOUT: layout(folder), PLAIN: plain(folder), PLAIN_NEXT: plain_next(folder)}


if __name__ == "__main__":
    for mod, archive in build_all(Path(sys.argv[1])).items():
        print(f"{mod}: {archive}")
