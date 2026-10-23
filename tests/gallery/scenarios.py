"""Generated, realistic Skyrim-style data for the Modlist Companion window.

Every scenario is a ``Scenario``: installer events, reminders, the load order
MO2 would report, and a name and description. Nothing is random between runs
(each scenario has its own seed), so a picture or a measurement can be
compared with the next one.

``materialize(scenario, folder)`` writes the scenario into a fresh database
through ``EventStore``, the same way the plugin does.
"""

from __future__ import annotations

import hashlib
import random
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))   # the support package, for the digest below

MAIN = {"id": hashlib.sha256(b"Example MO2").hexdigest(), "label": "Example MO2 (Skyrim SE)"}
OTHER = {"id": hashlib.sha256(b"MO2 Test").hexdigest(), "label": "MO2 Test"}
BASE_PLUGINS = ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm"]
INSTALLER = {"name": "FOMOD Logger Native Installer", "version": "1.0.0",
             "upstream_commit": "fc263f2d923c704b4853c11ed4f8b8cf3920f30d"}


@dataclass
class Scenario:
    name: str
    description: str
    events: list[dict[str, Any]]
    reviews: list[dict[str, str]]
    load_order: dict[str, Any]
    problems: list[tuple[str, str]] = field(default_factory=list)
    removed: list[tuple[str, str, str]] = field(default_factory=list)  # instance, mod, when
    hidden: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)     # event ids whose changed-installer card was marked Seen
    # New-patch alerts pending: {"mod": the mod just installed, "event_id", "option_id": the skipped patch option}.
    alerts: list[dict[str, str]] = field(default_factory=list)
    query: str = ""        # typed into the search box before drawing
    expand: int = 4        # installs opened in the picture
    open_files: int = 0    # "Files (N)" rows opened in the picture
    # What the window must show (checked by checks.content); counts of cards by state.
    expect: dict[str, Any] = field(default_factory=dict)
    # Which profiles have each mod enabled (the shape of core.compare_choices.read_profiles), when the scenario has them.
    profiles: dict[str, Any] | None = None
    # Comparisons to open in the Compare choices dialog: {"kind", "a", "b"} with a and b as an instance id, or
    # (instance id, mod name) or (instance id, mod name, n) for the mod's n-th newest install; "summary" is what the
    # dialog must say and "identical" whether it also lists what is the same.
    compare: list[dict[str, Any]] = field(default_factory=list)
    # The list's change history: entries written to the history table as they are (``at``, ``kind``, ``profile``,
    # ``items``, ``subject``, ``offline``, and for a program run ``ended_at``, ``exit_code`` and ``path``), oldest first.
    # ``history_profile`` is the MO2 profile in use; ``page`` the page the window shows when it is drawn.
    history: list[dict[str, Any]] = field(default_factory=list)
    history_profile: str = ""
    page: str = "installers"


# ---------- building blocks ----------

def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def opt(name: str, picked: bool = False, *, enabled: bool = True, plugins=None, masters=None, detects=None) -> dict:
    option = {"name": name, "selected": picked, "enabled": enabled}
    for key, value in (("plugins", plugins), ("masters", masters), ("detects", detects)):
        if value is not None:
            option[key] = list(value)
    return option


def record_files(event: dict) -> dict:
    """Give an event the files its installer would have recorded: each plugin as a file, each "Texture resolution"
    option as a folder of textures plus the base normal map, each "Normal maps" option as the same one file (the sharpened one with a higher
    priority), and every other option named for meshes, textures or sounds as a folder. Deterministic."""

    slug = "".join(c for c in event["mod"]["name"] if c.isalnum())
    order = 0

    def entry(source: str, destination: str, *, folder: bool = False, priority: int = 0) -> dict:
        nonlocal order
        order += 1
        resolved = destination.replace("\\", "/").strip("/")
        out = {"source": source, "destination": destination, "resolved": resolved,
               "kind": "folder" if folder else "file", "order": order}
        if priority:
            out["priority"] = priority
        return out

    for page in event["steps"]:
        for grp in page["groups"]:
            for option in grp["options"]:
                files = [entry(plugin, "") | {"resolved": plugin} for plugin in option.get("plugins", [])]
                low = option["name"].lower()
                if grp["name"] == "Texture resolution":
                    files.append(entry(f"{option['name']}\\Textures", f"Textures\\{slug}", folder=True))
                    # ... and the base normal map every resolution brings (a normal-map option may overwrite it)
                    files.append(entry(f"{option['name']}\\peak_n.dds", f"Textures\\{slug}\\peak_n.dds"))
                elif grp["name"] == "Normal maps":
                    files.append(entry(f"Normals\\{option['name']}.dds", f"Textures\\{slug}\\peak_n.dds",
                                       priority=1 if option["name"] == "Sharpened" else 0))
                else:
                    for kind in ("meshes", "textures", "sounds"):
                        if kind in low:
                            files.append(entry(f"{option['name']}\\{kind}", f"{kind.capitalize()}\\{slug}", folder=True))
                            break
                option["files"] = files
    return event


def group(name: str, kind: str, *options: dict) -> dict:
    return {"name": name, "type": kind, "options": list(options)}


def step(name: str, *groups: dict, visible: bool = True) -> dict:
    return {"name": name, "visible": visible, "groups": list(groups)}


def make_event(rng: random.Random, mod: str, version: str, when: datetime, steps: list[dict], *,
               nexus: int | None = None, instance: dict = MAIN, archive: str | None = None,
               reinstall: bool = False, auto: list[dict] | None = None) -> dict:
    """A complete event record; ids are assigned from the position of each step, group and option."""

    built = []
    for s, raw in enumerate(steps):
        groups = []
        for g, raw_group in enumerate(raw["groups"]):
            options = []
            for o, raw_option in enumerate(raw_group["options"]):
                options.append({"id": f"step:{s}/group:{g}/option:{o}", **raw_option,
                                "selected": bool(raw_option["selected"] and raw["visible"])})
            groups.append({"id": f"step:{s}/group:{g}", "name": raw_group["name"],
                           "type": raw_group["type"], "options": options})
        built.append({"id": f"step:{s}", "name": raw["name"], "visible": raw["visible"], "groups": groups})
    slug = "".join(c if c.isalnum() else "-" for c in mod).strip("-")[:48] or "mod"
    event = {
        "schema_version": 1,
        "event_id": _uuid(rng),
        "started_at": _stamp(when - timedelta(seconds=rng.randint(40, 400))),
        "completed_at": _stamp(when),
        "instance": dict(instance),
        "source": {"archive": archive or f"{slug}-{rng.randint(1000, 160000)}-{version.replace('.', '-')}.7z",
                   "reinstallation": reinstall},
        "mod": {"name": mod, "version": version, "nexus_id": nexus or rng.randint(1000, 160000)},
        "installer": dict(INSTALLER),
        "steps": built,
    }
    if auto is not None:
        event["automatic_installs"] = auto
    return event


# ---------- page templates (what real FOMODs ask) ----------

def _pick(rng: random.Random, names: list[str], weights: list[int] | None = None) -> str:
    return rng.choices(names, weights=weights)[0]


def page_textures(rng: random.Random) -> dict:
    res = _pick(rng, ["1K", "2K", "4K"], [1, 4, 2])
    nrm = _pick(rng, ["Vanilla style", "Sharpened", "Smoothed"])
    return step("Textures",
                group("Texture resolution", "SelectExactlyOne",
                      *(opt(r, r == res) for r in ("1K", "2K", "4K"))),
                group("Normal maps", "SelectExactlyOne",
                      *(opt(n, n == nrm) for n in ("Vanilla style", "Sharpened", "Smoothed"))))


def page_weather(rng: random.Random) -> dict:
    preset = _pick(rng, ["Default", "Darker nights", "Brighter nights"])
    return step("Weather preset",
                group("Preset", "SelectExactlyOne",
                      *(opt(p, p == preset) for p in ("Default", "Darker nights", "Brighter nights", "Vanilla-ish"))),
                group("Add-ons", "SelectAny",
                      opt("Aurora overhaul", rng.random() < 0.6), opt("Custom fog colours", rng.random() < 0.4),
                      opt("Snowfall tweaks", rng.random() < 0.5)))


def page_gameplay(rng: random.Random) -> dict:
    return step("Gameplay options",
                group("Difficulty tuning", "SelectExactlyOne",
                      opt("Vanilla values", False), opt("Recommended", True), opt("Hardcore", False)),
                group("Optional features", "SelectAny",
                      opt("Show perk requirements in tooltips", rng.random() < 0.7),
                      opt("Add new recipes to the forge", rng.random() < 0.5),
                      opt("Disable the intro message", True)))


def page_audio(rng: random.Random) -> dict:
    return step("Sound options",
                group("Footsteps", "SelectAtMostOne", opt("Soft", rng.random() < 0.5), opt("Heavy", False)),
                group("Ambience", "SelectAny", opt("Cave drips", True), opt("Wind in the pines", rng.random() < 0.5)))


PAGES: dict[str, Callable[[random.Random], dict]] = {
    "textures": page_textures, "weather": page_weather, "gameplay": page_gameplay, "audio": page_audio}


@dataclass
class P:
    """A compatibility patch an installer offers for one or more other plugins."""

    label: str
    plugin: str
    target: str | list[str]
    picked: bool | None = None     # None: picked when the target is installed
    detects: bool = False          # the FOMOD's own conditions look for the target
    enabled: bool = True

    @property
    def targets(self) -> list[str]:
        return [self.target] if isinstance(self.target, str) else list(self.target)


@dataclass
class A:
    """An automatic install the FOMOD does when it detects a plugin."""

    plugin: str
    target: str
    installed: bool | None = None  # None: installed when the target is


@dataclass
class Mod:
    name: str
    version: str
    plugin: str | None
    pages: tuple[str, ...] = ()
    patches: list[P] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)   # asset-only options named after other mods
    auto: list[A] = field(default_factory=list)
    legacy: bool = False        # logged before patch details existed
    hidden_page: bool = False   # a page the installer skipped
    older: list[tuple[str, int]] = field(default_factory=list)   # (version, days earlier) earlier installs
    # How the earlier installs differed from this one (only used where `older` is set):
    older_pages: tuple[str, ...] | None = None        # the pages they had, when not the same
    new_patches: list[str] = field(default_factory=list)          # patch labels they didn't offer yet
    new_extras: list[str] = field(default_factory=list)           # extras they didn't offer yet
    older_extras: list[str] = field(default_factory=list)         # extras they offered that were dropped since
    older_patch_labels: dict[str, str] = field(default_factory=dict)   # patch label now -> its label then
    # Picks (not installer changes): extras the newest / an earlier install had picked, and whether the
    # earlier install chose the light plugin where the newest chose the standard one.
    extras_picked: list[str] = field(default_factory=list)
    older_extras_picked: list[str] = field(default_factory=list)
    older_light: bool = False
    instance: dict = field(default_factory=lambda: MAIN)
    nexus: int | None = None
    # A mod that was not installed through a logged installer: a plain record with this many files (the first is
    # the plugin, when it has one), the masters its plugin names besides Skyrim.esm, and whether the list was cut.
    plain: bool = False
    plain_files: int = 24
    plain_masters: tuple[str, ...] = ()
    plain_cut: bool = False
    plain_origin: str = "install"


def plain_event(rng: random.Random, mod: Mod, version: str, when: datetime, *, reinstall: bool = False) -> dict:
    """The record of a mod that has no installer choices: its folder's files, plugin and the masters its header names."""

    from fomod_logger_support.core.plain_record import files_digest

    slug = "".join(c if c.isalnum() else "-" for c in mod.name).strip("-")[:48] or "mod"
    files = []
    if mod.plugin:
        files.append(mod.plugin)
    kinds = ("textures", "meshes", "sounds", "scripts", "interface")
    for number in range(max(0, mod.plain_files - len(files))):
        kind = kinds[number % len(kinds)]
        files.append(f"{kind}/{slug.lower()}/{kind[:-1] if kind.endswith('s') else kind}_{number:03}."
                     f"{('dds', 'nif', 'xwm', 'pex', 'swf')[number % 5]}")
    plugins = [{"name": mod.plugin, "masters": ["Skyrim.esm", *mod.plain_masters]}] if mod.plugin else []
    return {
        "schema_version": 1,
        "event_id": _uuid(rng),
        "started_at": _stamp(when),
        "completed_at": _stamp(when),
        "instance": dict(mod.instance),
        "source": {"archive": f"{slug}-{rng.randint(1000, 160000)}-{version.replace('.', '-')}.zip",
                   "reinstallation": reinstall},
        "mod": {"name": mod.name, "version": version, "nexus_id": mod.nexus or rng.randint(1000, 160000)},
        "installer": {"name": "No installer (plain install)", "version": "1.0.0", "upstream_commit": "unknown"},
        "steps": [],
        "kind": "plain",
        "plain": {"origin": mod.plain_origin, "game": "skyrimspecialedition", "file_count": len(files),
                  "files_truncated": mod.plain_cut, "files_digest": files_digest(files), "plugins": plugins,
                  "files": files},
    }


class World:
    """A little mod list: installs events and the load order MO2 would report."""

    def __init__(self, seed: int, start: datetime | None = None):
        self.rng = random.Random(seed)
        self.mods: list[Mod] = []
        self.start = start or datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)
        self._uncheck: list[str] = []
        self._disable: list[str] = []
        self._removed: list[tuple[str, str]] = []
        self._reviews: list[tuple[str, str, str, str, str]] = []
        self.extra_mods: list[str] = ["SkyUI", "SKSE64", "Address Library for SKSE Plugins", "Fuz Ro D-oh"]
        self.problems: list[tuple[str, str]] = []
        self.hidden_mods: list[str] = []
        self.seen_mods: list[str] = []
        self._alerts: list[tuple[str, str, str]] = []

    # --- description ---
    def add(self, mod: Mod) -> Mod:
        self.mods.append(mod)
        return mod

    def uncheck(self, plugin: str) -> None:
        self._uncheck.append(plugin)

    def disable_mod(self, name: str) -> None:
        self._disable.append(name)

    def remove(self, name: str, when: str) -> None:
        self._removed.append((name, when))

    def alert(self, new_mod: str, installer: str, label: str) -> None:
        """``new_mod`` was just installed and ``installer``'s skipped option ``label`` is a patch for it."""

        self._alerts.append((new_mod, installer, label))

    def review(self, mod: str, label: str | None, reason: str, state: str = "pending", on: str = "newest") -> None:
        self._reviews.append((mod, label or "", reason, state, on))

    # --- build ---
    def finish(self, name: str, description: str, **kwargs: Any) -> Scenario:
        rng = self.rng
        installed = {m.plugin.casefold() for m in self.mods if m.plugin and m.instance is MAIN}
        events: list[dict] = []
        ids: dict[tuple[str, str], dict[str, str]] = {}
        active: list[str] = []
        masters: dict[str, list[str]] = {}
        names = []
        clock = self.start
        for mod in self.mods:
            for index, (version, days) in enumerate([*mod.older, (mod.version, 0)]):
                newest = index == len(mod.older)
                when = clock + timedelta(hours=rng.randint(1, 5), minutes=rng.randint(0, 59))
                if not newest:
                    when -= timedelta(days=days)
                else:
                    clock = when
                event, labels = self._event(rng, mod, version, when, installed, newest, reinstall=index > 0)
                events.append(event)
                ids[(mod.name, "newest" if newest else "older")] = labels
                if newest and mod.instance is MAIN:
                    names.append(mod.name)
                    for plugin, plugin_masters in labels["_active"]:
                        active.append(plugin)
                        masters[plugin] = plugin_masters
        unchecked = []
        for plugin in self._uncheck:
            if plugin in active:
                active.remove(plugin)
            unchecked.append(plugin)
        disabled_mod_plugins = {}
        for modname in self._disable:
            mod = next(m for m in self.mods if m.name == modname)
            if mod.plugin in active:
                active.remove(mod.plugin)
            disabled_mod_plugins[mod.plugin] = modname
        reviews = []
        for mod, label, reason, state, which in self._reviews:
            table = ids[(mod, which)]
            event_id = table["_event"]
            reviews.append({"event_id": event_id, "option_id": table[label] if label else "",
                            "reason": reason, "state": state})
        removed = [(MAIN["id"], mod, when) for mod, when in self._removed]
        hidden = [ids[(mod, "newest")]["_event"] for mod in self.hidden_mods]
        seen = [ids[(mod, "newest")]["_event"] for mod in self.seen_mods]
        alerts = [{"mod": new_mod, "event_id": ids[(installer, "newest")]["_event"],
                   "option_id": ids[(installer, "newest")][label]} for new_mod, installer, label in self._alerts]
        load_order = {
            "instance_id": MAIN["id"], "active_plugins": [*BASE_PLUGINS, *active], "base_plugins": list(BASE_PLUGINS),
            "mods": [*self.extra_mods, *names], "unchecked_plugins": unchecked, "plugin_masters": masters,
            "disabled_mod_plugins": disabled_mod_plugins,
        }
        return Scenario(name, description, events, reviews, load_order, problems=list(self.problems),
                        removed=removed, hidden=hidden, seen=seen, alerts=alerts, **kwargs)

    def _event(self, rng, mod: Mod, version, when, installed, newest, *, reinstall):
        labels: dict[str, Any] = {}
        active: list[tuple[str, list[str]]] = []
        steps = []
        if mod.plain:
            event = plain_event(rng, mod, version, when, reinstall=reinstall)
            labels["_event"] = event["event_id"]
            labels["_active"] = [(mod.plugin, ["Skyrim.esm", *mod.plain_masters])] if mod.plugin else []
            return event, labels
        if mod.legacy:
            steps.append(step("Main", group("Files", "SelectExactlyOne", opt("Default", True), opt("Lite", False))))
            for page in mod.pages:
                steps.append(PAGES[page](rng))
            event = make_event(rng, mod.name, version, when, steps, nexus=mod.nexus, instance=mod.instance,
                               reinstall=reinstall)
            for s in event["steps"]:
                for g in s["groups"]:
                    for o in g["options"]:
                        labels[o["name"]] = o["id"]
            labels["_event"], labels["_active"] = event["event_id"], []
            return event, labels
        if mod.plugin:
            light = mod.older_light and not newest and bool(mod.older)
            steps.append(step("Installation", group(
                "Main plugin", "SelectExactlyOne",
                opt("Standard", not light, plugins=[mod.plugin], masters=["Skyrim.esm"], detects=[]),
                opt("Light (ESL-flagged)", light, plugins=[mod.plugin], masters=["Skyrim.esm"], detects=[]))))
            active.append((mod.plugin, ["Skyrim.esm"]))
        else:
            steps.append(step("Installation", group("Files", "SelectExactlyOne", opt("Default", True), opt("Lite", False))))
        then = not newest and bool(mod.older)   # an earlier install, which may have differed
        for page in (mod.older_pages if then and mod.older_pages is not None else mod.pages):
            steps.append(PAGES[page](rng))
        extras = [e for e in mod.extras if not (then and e in mod.new_extras)] + (mod.older_extras if then else [])
        if extras:
            kept = mod.older_extras_picked if then else mod.extras_picked
            steps.append(step("Extras", group("Optional meshes", "SelectAny", *(opt(extra, extra in kept) for extra in extras))))
        patches = [p for p in mod.patches if not (then and p.label in mod.new_patches)]
        if patches:
            options = []
            for patch in patches:
                want = all(t.casefold() in installed for t in patch.targets) if patch.picked is None else patch.picked
                if not newest and patch.picked is None:
                    want = False  # the first install, before those mods were in the list
                label = mod.older_patch_labels.get(patch.label, patch.label) if then else patch.label
                options.append(opt(label, want, enabled=patch.enabled, plugins=[patch.plugin],
                                   masters=[mod.plugin or "Skyrim.esm", *patch.targets],
                                   detects=patch.targets if patch.detects else []))
                if want and newest:
                    active.append((patch.plugin, [mod.plugin or "Skyrim.esm", *patch.targets]))
            steps.append(step("Compatibility patches", group("Patches for other mods", "SelectAny", *options)))
        if mod.hidden_page:
            steps.append(step("Developer extras", group("Debug options", "SelectAny", opt("Log verbose output", False),
                                                        opt("Keep source files", False)), visible=False))
        auto = None
        if mod.auto:
            auto = []
            for index, item in enumerate(mod.auto):
                done = item.target.casefold() in installed if item.installed is None else item.installed
                auto.append({"id": f"auto:{index}", "installed": done, "plugins": [item.plugin],
                             "masters": [mod.plugin or "Skyrim.esm", item.target], "detects": [item.target]})
                if done and newest:
                    active.append((item.plugin, [mod.plugin or "Skyrim.esm", item.target]))
        event = make_event(rng, mod.name, version, when, steps, nexus=mod.nexus, instance=mod.instance,
                           reinstall=reinstall, auto=auto)
        for s in event["steps"]:
            for g in s["groups"]:
                for o in g["options"]:
                    labels.setdefault(o["name"], o["id"])
        labels["_event"], labels["_active"] = event["event_id"], active
        return event, labels


# ---------- the mod list used by typical, every_state and many_cards ----------

LUX, LUXVIA = "Lux.esp", "Lux Via.esp"
CWE, OPEN, JKS, CAPITAL = "Cathedral Weathers.esp", "Open Cities Skyrim.esp", "JKs Skyrim.esp", "Capital Whiterun Expansion.esp"
FALSKAAR, WYRM, WETCOLD = "Falskaar.esm", "Wyrmstooth.esp", "WetandCold.esp"


def _typical_world(seed: int, *, full: bool) -> World:
    w = World(seed)
    add = w.add
    add(Mod("Unofficial Skyrim Special Edition Patch", "4.3.2a", "Unofficial Skyrim Special Edition Patch.esp", nexus=266))
    add(Mod("Cathedral Weathers and Seasons", "3.5.0", CWE, ("weather",), nexus=24187, patches=[
        P("Lux patch", "Cathedral Weathers - Lux patch.esp", LUX),
        P("Wet and Cold patch", "Cathedral Weathers - Wet and Cold.esp", WETCOLD, picked=False),
        P("Falskaar patch", "Cathedral Weathers - Falskaar patch.esp", FALSKAAR, picked=False),
        P("Wyrmstooth patch", "Cathedral Weathers - Wyrmstooth.esp", WYRM)]))
    add(Mod("Obsidian Weathers and Seasons", "1.6.1", "Obsidian Weathers.esp", ("weather",), patches=[
        P("Wet and Cold patch", "Obsidian Weathers - Wet and Cold patch.esp", WETCOLD, picked=False)]))
    add(Mod("Lux", "6.1.3", LUX, ("textures",), older=[("6.0.4", 38)]))
    add(Mod("Lux Via", "2.1.0", LUXVIA, patches=[
        P("Capital Whiterun Expansion", "Lux Via - Capital Whiterun Expansion.esp", CAPITAL, detects=True),
        P("JK's Skyrim", "Lux Via - JKs Skyrim.esp", JKS, detects=True),
        P("Open Cities Skyrim", "Lux Via - Open Cities.esp", OPEN, picked=False, detects=True),
        P("Falskaar", "Lux Via - Falskaar.esp", FALSKAAR),
        P("Keld-Nar", "Lux Via - Keld Nar patch.esp", "Keld-Nar.esp", picked=True)]))
    add(Mod("Skyrim Landscape and Water Fixes", "1.0.8", "Landscape and Water Fixes.esp"))
    add(Mod("Majestic Mountains", "1.3.2", "Majestic Mountains.esp", ("textures",), extras=["Capital Whiterun Expansion"]))
    add(Mod("JK's Skyrim", "5.0.4", JKS, ("textures",), hidden_page=True))
    add(Mod("Capital Whiterun Expansion", "3.0", CAPITAL))
    add(Mod("Open Cities Skyrim", "4.2.1", OPEN, patches=[P("JK's Skyrim patch", "Open Cities - JKs Skyrim.esp", JKS)]))
    add(Mod("Immersive Citizens - AI Overhaul", "1.7.0", "ImmersiveCitizens-AIOverhaul.esp", ("gameplay",), patches=[
        P("Open Cities Skyrim patch", "ImmersiveCitizens - Open Cities.esp", OPEN),
        P("JK's Skyrim patch", "ImmersiveCitizens - JKs Skyrim.esp", JKS),
        P("Capital Whiterun Expansion patch", "ImmersiveCitizens - Capital Whiterun.esp", CAPITAL, picked=False)]))
    add(Mod("Immersive Armors", "8.1", "Immersive Armors.esp", ("textures",)))
    add(Mod("Bandolier - Bags and Pouches", "2.6", "Bandolier - Bags and Pouches.esp"))
    add(Mod("Wyrmstooth", "2.9", WYRM, ("audio",)))
    add(Mod("Alternate Start - Live Another Life", "4.1.1", "Alternate Start - Live Another Life.esp", patches=[
        P("Falskaar patch", "Alternate Start - Falskaar.esp", FALSKAAR, picked=True),
        P("Wyrmstooth patch", "Alternate Start - Wyrmstooth.esp", WYRM)]))
    add(Mod("Complete Alchemy & Cooking Overhaul", "1.5.2", "Complete Alchemy & Cooking Overhaul.esp", ("gameplay",), legacy=True))
    add(Mod("Ordinator - Perks of Skyrim", "14.1", "Ordinator - Perks of Skyrim.esp", ("gameplay",), patches=[
        P("Vokrii patch", "Ordinator - Vokrii.esp", "Vokrii - Minimalistic Perks of Skyrim.esp")]))
    add(Mod("Skyrim Immersive Creatures", "8.0", "Skyrim Immersive Creatures Special Edition.esp", ("audio",), patches=[
        P("Wyrmstooth patch", "SIC - Wyrmstooth patch.esp", WYRM, picked=False),
        P("Falskaar patch", "SIC - Falskaar patch.esp", FALSKAAR),
        P("Complete creature hub", "SIC - Everything Hub.esp",
          ["Vigilant.esm", "Heroes of Sovngarde.esp", "Legacy of the Dragonborn.esm", "Beyond Reach.esp"], picked=True)]))
    add(Mod("Dynamic Things Alternative", "2.3.0", "Dynamic Things Alternative.esp"))
    add(Mod("RaceMenu", "0.4.19.14", "RaceMenu.esp", legacy=True))
    add(Mod("Static Mesh Improvement Mod", "2.1", "SMIM-Merged-All.esp", ("textures",), older=[("2.0", 61)]))
    add(Mod("Falskaar", "2.0", FALSKAAR))
    add(Mod("Simply Knock", "1.4", "SimplyKnock.esp", patches=[
        P("Immersive Citizens patch", "SimplyKnock - Immersive Citizens.esp", "ImmersiveCitizens-AIOverhaul.esp")]))
    add(Mod("Wet and Cold", "3.6.4", WETCOLD))
    add(Mod("Sounds of Skyrim - The Dungeons", "2.2", "Sounds of Skyrim - Dungeons.esp", ("audio",)))
    w.uncheck("Lux Via - Keld Nar patch.esp")
    w.uncheck("SIC - Everything Hub.esp")
    w.uncheck("Alternate Start - Falskaar.esp")
    w.uncheck("SIC - Wyrmstooth patch.esp")  # installed from another archive, switched off
    w.disable_mod("Wet and Cold")
    w.review("Lux Via", "Open Cities Skyrim", "Check again after the next Open Cities update.")
    w.review("Majestic Mountains", None, "Pick the 4K textures after the GPU upgrade.")
    return w


# Expected card counts for the typical list (worked out by hand from the data above).
_TYPICAL_STATES = {"missing": 4, "missing-masters": 2, "picked-disabled": 2, "patch-disabled": 1, "needs-disabled": 2}


def typical() -> Scenario:
    w = _typical_world(11, full=False)
    return w.finish(
        "typical", "A normal list: 25 logged mods with a few cards of every state.",
        expect={"states": dict(_TYPICAL_STATES), "reviews": 2, "mods": 25, "problems": 0})


def typical_search() -> Scenario:
    scenario = typical()
    scenario.name = "typical_search"
    scenario.description = "The typical list with the search box set to 'patch': matches highlighted, installs opened."
    scenario.query = "patch"
    scenario.expand = 6
    scenario.expect = {"states": dict(_TYPICAL_STATES), "query": "patch"}
    return scenario


def every_state() -> Scenario:
    """Every card state and every section at once, plus the oddities."""

    w = _typical_world(23, full=True)
    # An automatic install the installer would do now (the target is in the list, the patch isn't).
    w.mods[w.mods.index(next(m for m in w.mods if m.name == "Skyrim Landscape and Water Fixes"))].auto = [
        A("Landscape Fixes - Open Cities.esp", OPEN, installed=False)]
    # A patch the installer greyed out when it ran.
    imm = next(m for m in w.mods if m.name == "Immersive Armors")
    imm.patches = [P("Falskaar armour patch", "Immersive Armors - Falskaar.esp", FALSKAAR, picked=False, enabled=False)]
    # A mod reinstalled with different choices, with a reminder on the old record.
    lux = next(m for m in w.mods if m.name == "Static Mesh Improvement Mod")
    lux.pages = ("textures", "weather")
    w.add(Mod("Bethesda Hearthfire Cottage Redux", "1.2", "Hearthfire Cottage Redux.esp", ("textures",),
              older=[("1.1", 12)], hidden_page=True))
    w.add(Mod("Old Quest Tweaks", "0.9", "Old Quest Tweaks.esp"))
    w.add(Mod("Frostfall Compatibility Hub", "1.0", None, patches=[
        P("Cathedral Weathers patch", "Frostfall - Cathedral Weathers.esp", CWE, picked=True)]))
    # A second MO2 instance, which shows up in tooltips and the instance menu.
    w.add(Mod("Skyrim Script Extender Helper", "1.0", "SKSE Helper.esp", instance=OTHER))
    w.add(Mod("RaceMenu", "0.4.18", "RaceMenu.esp", instance=OTHER))
    # Mods whose installer changed in the latest update (see changed_installers).
    _updated(w, ("Lux Via", "Immersive Citizens - AI Overhaul", "Majestic Mountains"))
    w.review("Lux Via", "Capital Whiterun Expansion", "Done: installed the patch by hand.", "resolved")
    w.review("Wyrmstooth", "Cave drips", "Not needed any more.", "dismissed")
    w.review("Bethesda Hearthfire Cottage Redux", "2K", "Try 4K once the textures are patched.", on="older")
    w.review("Old Quest Tweaks", None, "Look for a better quest mod.")
    w.remove("Old Quest Tweaks", "2026-10-10T09:00:00+00:00")
    w.problems = [("events/6f1c2a90-bad.json", "event.steps[2].groups[0].options[1].selected must be true or false"),
                  ("events/truncated-4411.json", "Expecting ',' delimiter: line 211 column 9 (char 6342)")]
    scenario = w.finish(
        "every_state", "Every card state and section together: reminders, cross-patches, a removed mod, a reinstall, "
        "a greyed-out patch, an automatic patch, a four-master card, two bad files and a second MO2 instance.",
        expect={"states": {"missing": 6, "missing-masters": 2, "picked-disabled": 2, "patch-disabled": 1, "needs-disabled": 2},
                "reviews": 3, "problems": 2, "four_masters": True, "changes": 3, "changes_seen": 0})
    return scenario


def _updated(w: World, only: tuple[str, ...] | None = None) -> None:
    """Give mods of the typical list an earlier install whose installer differed: new patches (picked and
    skipped), a renamed option, a dropped one, a new page."""

    changes = {
        "Cathedral Weathers and Seasons": dict(
            older=[("3.4.2", 40)], new_patches=["Lux patch", "Falskaar patch", "Wyrmstooth patch"],
            older_patch_labels={"Wet and Cold patch": "Wet & Cold patch"}),
        "Lux Via": dict(
            older=[("2.0.3", 30)], new_patches=["Open Cities Skyrim", "JK's Skyrim", "Keld-Nar"],
            older_extras=["Lite meshes"]),
        "Immersive Citizens - AI Overhaul": dict(
            older=[("1.6.2", 20)], older_pages=(), new_patches=["Capital Whiterun Expansion patch"]),
        "Majestic Mountains": dict(
            older=[("1.3.0", 52)], new_extras=["Capital Whiterun Expansion"],
            older_extras=["Old shore meshes", "Snow caps v1"]),
        "Obsidian Weathers and Seasons": dict(older=[("1.6.0", 9)], new_patches=["Wet and Cold patch"]),
        "Alternate Start - Live Another Life": dict(older=[("4.0.9", 15)], new_patches=["Wyrmstooth patch"]),
        "Skyrim Immersive Creatures": dict(
            older=[("7.2", 70)], older_patch_labels={"Wyrmstooth patch": "Wyrmstooth compat",
                                                      "Falskaar patch": "Falskaar compat"}),
    }
    for mod in w.mods:
        if mod.name in changes and (only is None or mod.name in only):
            for key, value in changes[mod.name].items():
                setattr(mod, key, value)


def changed_installers() -> Scenario:
    """Mods updated with a changed installer: new patches for mods you have (skipped, picked, for a
    switched-off mod), a renamed option, dropped options, a new page, a settled card and unchanged updates."""

    w = World(61)
    add = w.add
    add(Mod("Falskaar", "2.0", FALSKAAR))
    add(Mod("Lux", "6.1.3", LUX, ("textures",), older=[("6.0.4", 38)]))   # updated, installer the same
    add(Mod("Open Cities Skyrim", "4.2.1", OPEN))
    add(Mod("Wyrmstooth", "2.9", WYRM, ("audio",)))
    add(Mod("JK's Skyrim", "5.0.4", JKS))
    add(Mod("Capital Whiterun Expansion", "3.0", CAPITAL))
    add(Mod("Wet and Cold", "3.6.4", WETCOLD))
    add(Mod("Cathedral Weathers and Seasons", "3.5.0", CWE, ("weather",), nexus=24187, patches=[
        P("Lux patch", "Cathedral Weathers - Lux patch.esp", LUX),
        P("Wet and Cold patch", "Cathedral Weathers - Wet and Cold.esp", WETCOLD, picked=False),
        P("Falskaar patch", "Cathedral Weathers - Falskaar patch.esp", FALSKAAR, picked=False),
        P("Wyrmstooth patch", "Cathedral Weathers - Wyrmstooth.esp", WYRM, picked=False)]))
    add(Mod("Lux Via", "2.1.0", LUXVIA, patches=[
        P("Capital Whiterun Expansion", "Lux Via - Capital Whiterun Expansion.esp", CAPITAL, detects=True),
        P("JK's Skyrim", "Lux Via - JKs Skyrim.esp", JKS, detects=True),
        P("Open Cities Skyrim", "Lux Via - Open Cities.esp", OPEN, picked=False, detects=True),
        P("Keld-Nar", "Lux Via - Keld Nar patch.esp", "Keld-Nar.esp", picked=False)]))
    add(Mod("Immersive Citizens - AI Overhaul", "1.7.0", "ImmersiveCitizens-AIOverhaul.esp", ("gameplay",), patches=[
        P("Open Cities Skyrim patch", "ImmersiveCitizens - Open Cities.esp", OPEN),
        P("JK's Skyrim patch", "ImmersiveCitizens - JKs Skyrim.esp", JKS),
        P("Capital Whiterun Expansion patch", "ImmersiveCitizens - Capital Whiterun.esp", CAPITAL, picked=False)]))
    add(Mod("Majestic Mountains", "1.3.2", "Majestic Mountains.esp", ("textures",), extras=["Capital Whiterun Expansion"]))
    add(Mod("Obsidian Weathers and Seasons", "1.6.1", "Obsidian Weathers.esp", ("weather",), patches=[
        P("Wet and Cold patch", "Obsidian Weathers - Wet and Cold patch.esp", WETCOLD, picked=False)]))
    add(Mod("Alternate Start - Live Another Life", "4.1.1", "Alternate Start - Live Another Life.esp", patches=[
        P("Falskaar patch", "Alternate Start - Falskaar.esp", FALSKAAR, picked=True),
        P("Wyrmstooth patch", "Alternate Start - Wyrmstooth.esp", WYRM)]))
    add(Mod("Skyrim Immersive Creatures", "8.0", "Skyrim Immersive Creatures Special Edition.esp", ("audio",), patches=[
        P("Wyrmstooth patch", "SIC - Wyrmstooth patch.esp", WYRM, picked=False),
        P("Falskaar patch", "SIC - Falskaar patch.esp", FALSKAAR)]))
    # Reinstalled from the same archive with different picks and nothing else changed: marks, no card.
    add(Mod("Whiterun Lanterns", "1.4", None, extras=["Warm lantern glow", "Market stall banners", "Night watch torches"],
            older=[("1.4", 3)], extras_picked=["Warm lantern glow", "Market stall banners"],
            older_extras_picked=["Warm lantern glow"]))
    add(Mod("Whiterun Banners", "2.2", "Whiterun Banners.esp", older=[("2.2", 1)], older_light=True))
    _updated(w)
    w.disable_mod("Wet and Cold")
    w.seen_mods.append("Alternate Start - Live Another Life")
    return w.finish(
        "changed_installers", "Mods updated with a changed installer: new patches (picked, skipped, for a "
        "switched-off mod), a renamed option, dropped options, a new page, one settled card; and two mods "
        "reinstalled with different picks only (marks in the list, no card).",
        expect={"states": {"missing": 6, "needs-disabled": 2, "picked-disabled": 1},
                "changes": 6, "changes_seen": 1, "reviews": 0,
                "picks_only": {"Whiterun Lanterns": "+1 picked", "Whiterun Banners": "+1 picked, −1 unpicked"}})


def new_patch_alerts() -> Scenario:
    """Mods installed just now that skipped patch options were made for: two switched on (red cards), one still
    switched off (orange card), beside a patch skipped long ago that has no alert."""

    w = World(71)
    add = w.add
    add(Mod("Cathedral Weathers and Seasons", "3.5.0", CWE, ("weather",), nexus=24187, patches=[
        P("Lux patch", "Cathedral Weathers - Lux patch.esp", LUX, picked=False),
        P("Falskaar patch", "Cathedral Weathers - Falskaar patch.esp", FALSKAAR, picked=False)]))
    add(Mod("Obsidian Weathers and Seasons", "1.6.1", "Obsidian Weathers.esp", ("weather",), patches=[
        P("Wet and Cold patch", "Obsidian Weathers - Wet and Cold patch.esp", WETCOLD, picked=False)]))
    add(Mod("Lux Via", "2.1.0", LUXVIA, patches=[
        P("Lux", "Lux Via - Lux.esp", LUX, picked=False, detects=True),
        P("Open Cities Skyrim", "Lux Via - Open Cities.esp", OPEN, picked=False, detects=True)]))
    add(Mod("Falskaar", "2.0", FALSKAAR))        # installed long ago: its patch is skipped, but nothing is new
    add(Mod("Lux", "6.1.3", LUX, ("textures",)))      # installed just now
    add(Mod("Wet and Cold", "3.6.4", WETCOLD))        # installed just now, still switched off
    w.disable_mod("Wet and Cold")
    w.alert("Lux", "Cathedral Weathers and Seasons", "Lux patch")
    w.alert("Lux", "Lux Via", "Lux")
    w.alert("Wet and Cold", "Obsidian Weathers and Seasons", "Wet and Cold patch")
    return w.finish(
        "new_patch_alerts", "Two mods just installed that skipped patches are made for (switched on, and switched "
        "off), marked New in their cards with their sections opened, beside a patch skipped long ago.",
        expect={"states": {"missing": 3, "needs-disabled": 1, "picked-disabled": 1}, "alerts": 3, "reviews": 0, "problems": 0})


def compare_choices() -> Scenario:
    """Two MO2 instances that share mods, two copies of one mod in the main list, a mod installed twice, and the
    profiles of the main instance: the Compare choices dialog in each of its three modes, and the profile notes in the list."""

    rng = random.Random(81)
    start = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
    days = iter(range(0, 400))

    def mm(res: str, normals: str, snow: bool) -> list[dict]:
        return [step("Textures",
                     group("Texture resolution", "SelectExactlyOne", *(opt(r, r == res) for r in ("1K", "2K", "4K"))),
                     group("Normal maps", "SelectExactlyOne",
                           *(opt(n, n == normals) for n in ("Vanilla style", "Sharpened", "Smoothed")))),
                step("Extras", group("Optional meshes", "SelectAny", opt("Snow caps", snow), opt("Old shore meshes", False)))]

    def weather(aurora: bool, fog: bool, snow: bool) -> list[dict]:
        return [step("Weather preset", group("Preset", "SelectExactlyOne", opt("Default", True), opt("Darker nights")),
                     group("Add-ons", "SelectAny", opt("Aurora overhaul", aurora), opt("Custom fog colours", fog),
                           opt("Snowfall tweaks", snow)))]

    def armors(dwarven: bool, elven: bool | None, dev_visible: bool) -> list[dict]:
        sets = [opt("Daedric set", True), opt("Dwarven set", dwarven)]
        if elven is not None:
            sets.append(opt("Elven set", elven))       # only the newer installer offers it
        return [step("Armor sets", group("Sets", "SelectAny", *sets)),
                step("Developer extras", group("Debug options", "SelectAny", opt("Log verbose output", dev_visible)),
                     visible=dev_visible)]

    lanterns = lambda warm, banners: [step("Lanterns", group("Styles", "SelectAny", opt("Warm lantern glow", warm),
                                                             opt("Market stall banners", banners)))]
    plain = lambda: [step("Installation", group("Files", "SelectExactlyOne", opt("Default", True), opt("Lite", False)))]
    events: list[dict] = []

    def add(instance: dict, name: str, version: str, steps: list[dict], nexus: int, archive: str, back: int = 0) -> None:
        when = start + timedelta(hours=3 * next(days)) - timedelta(days=back)
        events.append(make_event(rng, name, version, when, steps, nexus=nexus, instance=instance, archive=archive,
                                 reinstall=bool(back)))

    mm_archive = "Majestic Mountains-1234-1-3-2.7z"
    add(MAIN, "Majestic Mountains - Dark", "1.3.2", mm("4K", "Sharpened", True), 1234, mm_archive)
    add(MAIN, "Majestic Mountains - Light", "1.3.2", mm("2K", "Vanilla style", False), 1234, mm_archive)
    add(MAIN, "Lux", "6.1.3", plain(), 144, "Lux-144-6-1-3.7z")
    add(MAIN, "Cathedral Weathers and Seasons", "3.5.0", weather(True, True, False), 24187, "Cathedral Weathers-24187-3-5-0.7z")
    add(MAIN, "Immersive Armors", "8.1", armors(False, None, False), 19733, "Immersive Armors-19733-8-1.7z")
    add(MAIN, "Main Only Mod", "1.0", plain(), 777, "Main Only Mod-777-1-0.7z")
    add(MAIN, "Whiterun Lanterns", "1.4", lanterns(True, False), 4410, "Whiterun Lanterns-4410-1-4.7z", back=9)
    add(MAIN, "Whiterun Lanterns", "1.4", lanterns(True, True), 4410, "Whiterun Lanterns-4410-1-4.7z")
    add(OTHER, "Majestic Mountains - Dark", "1.3.2", mm("2K", "Sharpened", True), 1234, mm_archive)
    add(OTHER, "Lux", "6.1.3", plain(), 144, "Lux-144-6-1-3.7z")
    add(OTHER, "Cathedral Weathers and Seasons", "3.5.0", weather(False, True, True), 24187, "Cathedral Weathers-24187-3-5-0.7z")
    add(OTHER, "Immersive Armors", "8.2", armors(True, True, True), 19733, "Immersive Armors-19733-8-2.7z")
    add(OTHER, "Second Only Mod", "2.0", plain(), 888, "Second Only Mod-888-2-0.7z")
    names = ["Majestic Mountains - Dark", "Majestic Mountains - Light", "Lux", "Cathedral Weathers and Seasons",
             "Immersive Armors", "Main Only Mod", "Whiterun Lanterns"]
    # Enabled in: the daily list has most of them, the light list a few, the testing list one; Main Only Mod in none.
    enabled = {"Majestic Mountains - Dark": ["Default", "Testing"], "Majestic Mountains - Light": ["Light"],
               "Lux": ["Default", "Light", "Testing"], "Cathedral Weathers and Seasons": ["Default", "Light"],
               "Immersive Armors": ["Default"], "Whiterun Lanterns": ["Default", "Light"]}
    profiles = {"folder": "profiles", "profiles": ["Default", "Light", "Testing"],
                "mods": {name.casefold(): where for name, where in enabled.items()}}
    load_order = {"instance_id": MAIN["id"], "active_plugins": list(BASE_PLUGINS), "base_plugins": list(BASE_PLUGINS),
                  "mods": names, "unchecked_plugins": [], "plugin_masters": {}, "disabled_mod_plugins": {}}
    return Scenario(
        "compare_choices", "Two MO2 instances that share mods, two copies of Majestic Mountains, a mod installed twice and "
        "three profiles: Compare choices in each mode, and the profile notes on the mod rows.", events, [], load_order,
        expand=0, profiles=profiles, expect={"states": {}, "reviews": 0, "problems": 0, "mods": 12, "no_profile": ["Main Only Mod"],
                "profile_tips": {"Lux": "Enabled in profiles: Default, Light, Testing",
                                 "Main Only Mod": "Not enabled in any profile."}},
        compare=[
            {"kind": "instances", "a": MAIN["id"], "b": OTHER["id"], "identical": True,
             "summary": "8 mods: 3 with different picks, 3 only in A, 1 only in B, 1 identical."},
            {"kind": "copies", "a": (MAIN["id"], "Majestic Mountains - Dark"), "b": (MAIN["id"], "Majestic Mountains - Light"),
             "identical": True, "summary": "5 picks differ (3 only in A, 2 only in B)."},
            {"kind": "installs", "a": (MAIN["id"], "Whiterun Lanterns", 1), "b": (MAIN["id"], "Whiterun Lanterns", 0),
             "identical": False, "summary": "1 pick differ (0 only in A, 1 only in B)."},
        ])


def file_search() -> Scenario:
    """The typical list with the files each installer defines recorded, searched for one texture: the options
    that install it are marked "installs ..." (the resolution folders, and the normal maps that write the same file,
    one of which wins), and their "Files (N)" rows are there to open."""

    scenario = _typical_world(11, full=False).finish("file_search", "")
    scenario.description = ("The typical list with installer files recorded, searched for Textures/MajesticMountains/"
                            "peak_n.dds: six options install it, one normal map wins; two Files rows are open.")
    for event in scenario.events:
        record_files(event)
    scenario.query = "Textures/MajesticMountains/peak_n.dds"
    scenario.expand = 1
    scenario.open_files = 2
    scenario.expect = {"states": dict(_TYPICAL_STATES), "reviews": 2, "problems": 0, "file_hits": 6, "query": scenario.query}
    return scenario


def empty() -> Scenario:
    load_order = {"instance_id": MAIN["id"], "active_plugins": list(BASE_PLUGINS), "base_plugins": list(BASE_PLUGINS),
                  "mods": [], "unchecked_plugins": [], "plugin_masters": {}, "disabled_mod_plugins": {}}
    return Scenario("empty", "First run: nothing logged yet.", [], [], load_order,
                    expect={"states": {}, "reviews": 0, "mods": 0, "problems": 0})


def long_names() -> Scenario:
    w = World(31)
    long_plugin = ("Aetherium_Dwemer_Overhaul_Extended_Remastered_Edition_Compatibility_Patch_For_Everything_"
                   "Version_Three_Final_Final2.esp")
    long_mod = ("Ætherium – Dwemer ✦ Overhaul – Extended Remastered Edition for Special Edition and Anniversary "
                "Edition (Creation Club Compatible) Version 3")
    w.add(Mod(long_mod, "3.0.0-beta.12+build.4711", "Ætherium - Dwemer Overhaul.esp", ("textures", "gameplay"), patches=[
        P("Patch for Legacy of the Dragonborn – Special Edition – Anniversary Edition – All DLC – Creation Club "
          "Content – Unofficial Patch Compatible Build (recommended for most players)",
          long_plugin, "Legacy of the Dragonborn.esm", picked=False),
        P("Compatibilité avec « Légendes de Bordeciel » – Édition Spéciale", "Ætherium - Légendes.esp",
          "Légendes de Bordeciel – Édition Spéciale.esp", picked=False),
        P("Ünïcödé Tëxtures ★ patch", "Ætherium - Unicode.esp", "Ünïcödé Tëxtures.esp")]))
    w.add(Mod("Legacy of the Dragonborn", "5.0.9", "Legacy of the Dragonborn.esm"))
    w.add(Mod("Légendes de Bordeciel – Édition Spéciale", "1.4", "Légendes de Bordeciel – Édition Spéciale.esp"))
    w.add(Mod("Ünïcödé Tëxtures ★", "2.0", "Ünïcödé Tëxtures.esp", ("textures",)))
    w.add(Mod("日本語の防具 Overhaul", "1.1", "日本語の防具.esp", ("textures",)))
    w.add(Mod("The Absolutely Complete and Utterly Exhaustive Immersive Realistic Beautiful Enhanced Skyrim "
              "Weather Lighting and Atmosphere Overhaul", "12.0", "Complete_Weather_Lighting_Atmosphere_Overhaul.esp",
              ("weather",), patches=[
                  P("A very long option label for a patch that names the other mod in full: Ætherium – Dwemer ✦ "
                    "Overhaul – Extended Remastered Edition", "CWLA - Aetherium Patch.esp",
                    "Ætherium - Dwemer Overhaul.esp", picked=False, detects=True)]))
    w.add(Mod("A", "1", "A.esp"))
    w.add(Mod("Keld-Nar Overhaul", "1.0", "Keld-Nar.esp", patches=[P(
        "ThisIsAnOptionNameWithoutAnySpacesWhichIsFarWiderThanAnySidePanelCouldEverHopeToFitOnOneLine",
        "KeldNar_Compat_ThisIsAVeryLongPluginNameWithoutAnySpacesAtAllJustToSeeWhatTheCardDoes.esp",
        "Alternate_Start_Live_Another_Life_Complete_Edition_Remastered.esp", picked=False)]))
    w.add(Mod("Alternate Start Live Another Life Complete Edition Remastered", "9", "Alternate_Start_Live_Another_Life_Complete_Edition_Remastered.esp"))
    w.uncheck("Ætherium - Unicode.esp")
    w.review("Legacy of the Dragonborn", None,
             "Remember to read https://www.nexusmods.com/skyrimspecialedition/mods/11802?tab=posts&BH=0&sortby=most_recent "
             "and the long forum thread about museum display patches before the next playthrough. " * 3, "pending")
    scenario = w.finish(
        "long_names", "Very long mod, option and plugin names, unicode, a name with no spaces, a one-letter mod.",
        expect={"reviews": 1})
    return scenario


def many_cards() -> Scenario:
    w = World(47)
    targets = [("Cathedral Weathers and Seasons", "Cathedral Weathers.esp"), ("Lux", "Lux.esp"), ("Lux Via", "Lux Via.esp"),
               ("JK's Skyrim", "JKs Skyrim.esp"), ("Open Cities Skyrim", "Open Cities Skyrim.esp"),
               ("Falskaar", "Falskaar.esm"), ("Wyrmstooth", "Wyrmstooth.esp"), ("Capital Whiterun Expansion", "Capital Whiterun Expansion.esp"),
               ("Immersive Armors", "Immersive Armors.esp"), ("Ordinator - Perks of Skyrim", "Ordinator - Perks of Skyrim.esp"),
               ("Wet and Cold", "WetandCold.esp"), ("Static Mesh Improvement Mod", "SMIM-Merged-All.esp")]
    for name, plugin in targets:
        w.add(Mod(name, "1.0", plugin))
    owners = ["Skyrim Immersive Creatures", "Immersive Citizens - AI Overhaul", "Alternate Start - Live Another Life",
              "Simply Knock", "Bandolier - Bags and Pouches", "Dynamic Things Alternative", "Sounds of Skyrim - The Dungeons",
              "RaceMenu", "Unofficial Skyrim Special Edition Patch", "Skyrim Landscape and Water Fixes",
              "Majestic Mountains", "Complete Alchemy & Cooking Overhaul", "Obsidian Weathers and Seasons",
              "Frostfall", "Campfire", "Realistic Needs and Diseases"]
    for index, owner in enumerate(owners):
        slug = owner.split(" -")[0]
        picks = targets[(index * 3) % len(targets):] + targets[:(index * 3) % len(targets)]
        w.add(Mod(owner, f"{2 + index % 4}.{index % 9}", f"{slug}.esp", ("textures",) if index % 3 == 0 else (), patches=[
            P(f"{t[0].split(' -')[0]} patch", f"{slug} - {t[0].split(' -')[0]}.esp", t[1], picked=False,
              detects=(index + n) % 3 == 0) for n, t in enumerate(picks[:3 + index % 2])]))
    return w.finish("many_cards", "Over 40 possible missed patches at once (a long side panel).",
                    expect={"min_missing": 40})


# ---------- the big one ----------

_PREFIX = ["Cathedral", "Majestic", "Noble", "Immersive", "Realistic", "Enhanced", "Lux", "Vivid", "Rustic", "Dwemer",
           "Nordic", "Frozen", "Ancient", "Radiant", "Elder", "Whiterun", "Solitude", "Riften", "Windhelm", "Markarth",
           "Falkreath", "Dawnstar", "Winterhold", "Morthal", "Skyrim"]
_NOUN = ["Landscapes", "Weathers", "Armors", "Bandits", "Taverns", "Dungeons", "Roads", "Grass", "Trees", "Rocks", "Fires",
         "Banners", "Doors", "Mines", "Bridges", "Farms", "Cities", "Castles", "Textures", "Lighting", "Wildlife",
         "Combat", "Magic", "Swords", "Bows"]
_SUFFIX = ["Overhaul", "Redux", "SE", "HD", "Remastered", "Extended", "Expanded", "Complete", "Lite", "Reborn", "Revisited", "AIO"]


def _slug_plugin(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " -_'&")[:60].strip() + ".esp"


def huge(mods: int = 300, seed: int = 5) -> Scenario:
    rng = random.Random(seed)
    names: list[str] = []
    seen: set[str] = set()
    while len(names) < mods:
        name = f"{rng.choice(_PREFIX)} {rng.choice(_NOUN)} {rng.choice(_SUFFIX)}"
        if name not in seen:
            seen.add(name)
            names.append(name)
    plugins = {name: _slug_plugin(name) for name in names}
    events: list[dict] = []
    active: list[str] = []
    masters: dict[str, list[str]] = {}
    reviews: list[dict] = []
    clock = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    horizon = (datetime(2026, 10, 15, tzinfo=timezone.utc) - clock).total_seconds()
    total_events = 0
    for index, name in enumerate(names):
        plugin = plugins[name]
        patch_targets = rng.sample(names[:index] + names[index + 1:], k=min(rng.randint(2, 7), mods - 1))
        installs = rng.randint(4, 10)
        newest = None
        for n in range(installs):
            latest = n == installs - 1
            when = clock + timedelta(seconds=horizon * (n + rng.random() * 0.8) / installs)
            steps = [step("Installation", group("Main plugin", "SelectExactlyOne",
                                                opt("Standard", True, plugins=[plugin], masters=["Skyrim.esm"], detects=[]),
                                                opt("Light (ESL-flagged)", False, plugins=[plugin], masters=["Skyrim.esm"], detects=[])))]
            for s in range(rng.randint(2, 5)):
                groups = []
                for g in range(rng.randint(2, 4)):
                    # Roughly half the labels are generic ("Option B - darker meshes"), the rest name the mod.
                    options = [opt(f"{rng.choice(['Option', 'Variant', 'Style', 'Preset'])} {chr(65 + o)} – "
                                   f"{rng.choice(['vanilla', 'darker', 'brighter', 'sharper', 'softer', 'compact', 'rich'])}"
                                   f" {rng.choice(['meshes', 'textures', 'plugin', 'sounds', 'lighting'])}"
                                   if rng.random() < 0.45 else
                                   f"{' '.join(name.split()[:2])} {rng.choice(['vanilla', 'darker', 'brighter', 'sharper', 'softer'])} "
                                   f"{rng.choice(['meshes', 'textures', 'plugin', 'sounds'])} {s + 1}.{g + 1}.{o + 1}", False)
                               for o in range(rng.randint(3, 8))]
                    options[rng.randrange(len(options))]["selected"] = True
                    groups.append(group(f"{rng.choice(['Appearance', 'Gameplay', 'Performance', 'Extras', 'Quality'])} {g + 1}",
                                        "SelectExactlyOne", *options))
                steps.append(step(f"Page {s + 1}", *groups, visible=rng.random() > 0.08))
            patches = []
            for target in patch_targets:
                # Picked when the mod is in the list, except for a handful left out.
                want = (index + names.index(target)) % 37 != 0
                patch_plugin = f"{plugin[:-4]} - {plugins[target][:-4]}.esp"[:100]
                patches.append(opt(f"{target} patch", want and latest, plugins=[patch_plugin],
                                   masters=[plugin, plugins[target]], detects=[]))
                if want and latest:
                    active.append(patch_plugin)
                    masters[patch_plugin] = [plugin, plugins[target]]
            steps.append(step("Compatibility patches", group("Patches for other mods", "SelectAny", *patches)))
            event = record_files(make_event(rng, name, f"{rng.randint(1, 4)}.{rng.randint(0, 9)}.{rng.randint(0, 20)}",
                                            when, steps, reinstall=n > 0))
            events.append(event)
            total_events += 1
            if latest:
                newest = event
        active.append(plugin)
        masters[plugin] = ["Skyrim.esm"]
        if index % 14 == 0 and newest is not None:
            option = newest["steps"][1]["groups"][0]["options"][0]
            reviews.append({"event_id": newest["event_id"], "option_id": option["id"],
                            "reason": f"Compare {option['name']} with another preset.", "state": "pending"})
    load_order = {"instance_id": MAIN["id"], "active_plugins": [*BASE_PLUGINS, *active], "base_plugins": list(BASE_PLUGINS),
                  "mods": names, "unchecked_plugins": [], "plugin_masters": masters, "disabled_mod_plugins": {}}
    return Scenario("huge", f"{mods} logged mods and {total_events} installs (reinstalls included).", events, reviews,
                    load_order, expect={"mods": mods, "min_events": 2000, "events": total_events})


# ---------- mods that were not installed through an installer ----------

def _plain_world(seed: int) -> World:
    """The typical list with plain-zip mods among it: plugins and no plugins, a plugin whose master is missing, an
    update (two records), a very long name, a list cut short, and a scanned mod."""

    w = _typical_world(seed, full=False)
    add = w.add
    add(Mod("SkyUI", "5.2SE", "SkyUI_SE.esp", plain=True, plain_files=64, nexus=12604))
    add(Mod("Better Dialogue Controls", "1.4.1", "BetterDialogueControls.esp", plain=True, plain_files=6, nexus=2713))
    add(Mod("Address Library for SKSE Plugins", "11", None, plain=True, plain_files=3, nexus=32444))
    add(Mod("SSE Engine Fixes", "6.0.1", None, plain=True, plain_files=9, nexus=17230))
    add(Mod("Bug Fixes SSE", "2.0.3", "BugFixesSSE.esp", plain=True, plain_files=14, nexus=13604))
    add(Mod("Faster HDT-SMP", "2.1", None, plain=True, plain_files=5, nexus=57339, plain_origin="scan"))
    add(Mod("Dragonborn Voice Fix", "1.0", "DragonbornVoiceFix.esp", plain=True, plain_files=4,
            older=[("0.9", 40)]))
    add(Mod("Static Grass Collection", "3.1", "StaticGrassCollection.esp", plain=True, plain_files=30,
            plain_masters=("Static Grass Base Pack.esm",), nexus=44553))
    add(Mod("Skyrim Textures Mega Pack - Everything From Rocks To Rugs And Every Last Barrel In Between Remastered",
            "2.0", "Mega Textures Pack.esp", plain=True, plain_files=120, plain_cut=True))
    add(Mod("Ætherium Plain – Unicode ✦ Mod", "1", None, plain=True, plain_files=2))
    w.review("SkyUI", None, "Check the new version after the next game update.")
    return w


_PLAIN_STATES = dict(_TYPICAL_STATES, **{"missing-masters": _TYPICAL_STATES["missing-masters"] + 1})


def plain_mods() -> Scenario:
    w = _plain_world(61)
    return w.finish(
        "plain_mods", "The typical list plus ten mods that were not installed through an installer: a quiet row each, one "
        "plugin that can't load, an update and a long list cut short.",
        expect={"states": dict(_PLAIN_STATES), "mods": 35, "plain": 10, "reviews": 3, "problems": 0})


def plain_search() -> Scenario:
    scenario = plain_mods()
    scenario.name = "plain_search"
    scenario.description = "The list with a file searched for: the plain mod that has it is opened, its Files row too."
    scenario.query = "interface/skyui/interface_004.swf"
    scenario.expand = 3
    scenario.expect = {"states": dict(_PLAIN_STATES), "plain_hits": 1}
    return scenario


def plain_many(plain: int = 800, fomod: int = 300, seed: int = 9) -> Scenario:
    """A list built mostly by hand: 800 mods from plain zips and 300 installed through FOMODs."""

    rng = random.Random(seed)
    base = huge(fomod, seed=5)
    taken = {event["mod"]["name"] for event in base.events}
    clock = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    names: list[str] = []
    while len(names) < plain:
        name = f"{rng.choice(_PREFIX)} {rng.choice(_NOUN)} {rng.choice(_SUFFIX)} {rng.randint(1, 99)}"
        if name not in taken:
            taken.add(name)
            names.append(name)
    events = list(base.events)
    active = list(base.load_order["active_plugins"])
    masters = dict(base.load_order["plugin_masters"])
    for index, name in enumerate(names):
        has_plugin = index % 5 != 0
        mod = Mod(name, f"{rng.randint(1, 5)}.{rng.randint(0, 9)}", _slug_plugin(name) if has_plugin else None, plain=True,
                  plain_files=rng.choice((3, 12, 30, 60, 150)), nexus=rng.randint(1000, 160000))
        when = clock + timedelta(hours=index * 7)
        events.append(plain_event(rng, mod, mod.version, when))
        if has_plugin:
            active.append(mod.plugin)
            masters[mod.plugin] = ["Skyrim.esm"]
    load_order = dict(base.load_order, active_plugins=active, plugin_masters=masters,
                      mods=[*base.load_order["mods"], *names])
    return Scenario("plain_many", f"{plain} mods from plain zips and {fomod} logged FOMOD installs in one list.", events,
                    base.reviews, load_order,
                    expect={"mods": plain + fomod, "plain": plain, "min_events": 2800, "events": len(events)})


# ---------- the list's change history ----------

_PROGRAMS = (("Skyrim Special Edition", r"C:\Example MO2\Stock Game\skse64_loader.exe"),
             ("SSEEdit", r"C:\Example MO2\Tools\SSEEdit\SSEEdit64.exe"), ("LOOT", r"C:\Program Files\LOOT\LOOT.exe"))


def _hist(at: datetime, kind: str, items: list[dict] | None = None, *, profile: str = "Default", subject: str = "",
          offline: bool = False, count: int | None = None, **extra: Any) -> dict[str, Any]:
    items = items or []
    return {"at": at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "kind": kind,
            "profile": profile, "items": items, "count": count if count is not None else max(1, len(items)),
            "subject": subject, "offline": offline, **extra}


def _names(rng: random.Random, count: int, taken: set[str]) -> list[str]:
    found: list[str] = []
    while len(found) < count:
        name = f"{rng.choice(_PREFIX)} {rng.choice(_NOUN)} {rng.choice(_SUFFIX)}"
        if name not in taken:
            taken.add(name)
            found.append(name)
    return found


def _history_entries(rng: random.Random, days: int, per_day: int, *, start: datetime) -> list[dict[str, Any]]:
    """A busy list's history: installs, updates and removals, mods and plugins on, off and moved, profile switches,
    programs started (some ending normally, one crashing, one never seen to end), and changes found when MO2 started."""

    taken: set[str] = set()
    mods = _names(rng, 160, taken)
    plugins = [_slug_plugin(name) for name in mods[:120]]
    entries: list[dict[str, Any]] = []
    profile = "Default"
    for day in range(days):
        clock = start + timedelta(days=day)
        session = clock.replace(hour=17, minute=rng.randint(0, 40))
        if day and day % 4 == 0:
            profile = "Hardcore" if profile == "Default" else "Default"
            entries.append(_hist(session, "profile_switched", [{"name": profile, "before": "Default" if profile == "Hardcore" else "Hardcore",
                                                                "after": profile}], profile=profile, subject=profile))
        if day % 3 == 1:         # found at the start of this session
            entries.append(_hist(session, "mod_enabled", [{"name": rng.choice(mods)}], profile=profile, offline=True))
            entries.append(_hist(session, "plugin_disabled", [{"name": rng.choice(plugins)}], profile=profile, offline=True))
        moment = session + timedelta(minutes=2)
        for _ in range(per_day):
            moment += timedelta(minutes=rng.randint(1, 9), seconds=rng.randint(0, 59))
            roll = rng.random()
            if roll < 0.14:
                name = rng.choice(mods)
                entries.append(_hist(moment, "mod_installed", [{"name": name}], profile="", record=True))
            elif roll < 0.22:
                name = rng.choice(mods)
                entries.append(_hist(moment, "mod_updated", [{"name": name, "before": f"{rng.randint(1, 3)}.{rng.randint(0, 9)}",
                                                              "after": f"{rng.randint(4, 6)}.{rng.randint(0, 9)}"}], profile=""))
            elif roll < 0.26:
                entries.append(_hist(moment, "mod_removed", [{"name": rng.choice(mods)}], profile=""))
            elif roll < 0.36:
                picked = sorted(rng.sample(mods, rng.choice((1, 1, 1, 2, 6, 14))))
                entries.append(_hist(moment, rng.choice(("mod_enabled", "mod_disabled")), [{"name": n} for n in picked], profile=profile))
            elif roll < 0.5:
                picked = sorted(rng.sample(mods, rng.choice((1, 1, 3, 9))))
                entries.append(_hist(moment, "mod_moved", [{"name": n, "before": rng.randint(0, 80), "after": rng.randint(81, 150)} for n in picked], profile=profile))
            elif roll < 0.66:
                picked = sorted(rng.sample(plugins, rng.choice((1, 1, 2, 5))))
                entries.append(_hist(moment, rng.choice(("plugin_enabled", "plugin_disabled")), [{"name": n} for n in picked], profile=profile))
            elif roll < 0.8:
                picked = sorted(rng.sample(plugins, rng.choice((1, 4, 12))))
                entries.append(_hist(moment, "plugin_moved", [{"name": n, "before": rng.randint(5, 90), "after": rng.randint(91, 200)} for n in picked], profile=profile))
            elif roll < 0.84:
                name, path = rng.choice(_PROGRAMS)
                ended = moment + timedelta(minutes=rng.randint(4, 120))
                entries.append(_hist(moment, "program_run", profile=profile, subject=name, ended_at=ended.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                                     exit_code=0, detail={"path": path}))
                moment = ended
            elif roll < 0.87:
                name, path = _PROGRAMS[0]
                ended = moment + timedelta(minutes=3)
                entries.append(_hist(moment, "program_run", profile=profile, subject=name, ended_at=ended.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                                     exit_code=3221225477, detail={"path": path}))
                moment = ended
    return entries


def history() -> Scenario:
    """Twelve days of a busy list: every kind of change, programs that ran, and more entries than one page."""

    rng = random.Random(41)
    entries = _history_entries(rng, 12, 40, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
    # Hand-placed: a grouped entry of exactly the size the brief names, a long name, unicode, a program whose end was
    # never seen, and a program that ran across midnight.
    at = datetime(2026, 10, 16, 21, 30, tzinfo=timezone.utc)
    entries.append(_hist(at, "plugin_moved", [{"name": f"Moved Plugin {n:02d}.esp", "before": n, "after": n + 40} for n in range(12)]))
    entries.append(_hist(at + timedelta(minutes=2), "mod_installed", [{"name": "Ætherium – Dwemer ✦ Overhaul – Extended Remastered Edition for Special Edition and Anniversary Edition (Creation Club Compatible) Version 3"}], profile=""))
    entries.append(_hist(at + timedelta(minutes=4), "mod_enabled", [{"name": "日本語の防具 Overhaul"}]))
    entries.append(_hist(at + timedelta(minutes=6), "program_run", subject="Skyrim Special Edition", detail={"path": _PROGRAMS[0][1]}))
    entries.append(_hist(at + timedelta(minutes=8), "mod_reinstalled", [{"name": "Lux Via"}], profile=""))
    entries.append(_hist(at + timedelta(minutes=9), "mod_renamed", [{"name": "Cathedral Weathers", "before": "CWAS"}], profile=""))
    late = datetime(2026, 10, 17, 22, 40, tzinfo=timezone.utc)
    entries.append(_hist(late, "program_run", subject="Skyrim Special Edition",
                         ended_at=(late + timedelta(hours=3)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                         exit_code=0, detail={"path": _PROGRAMS[0][1]}))
    entries.append(_hist(late + timedelta(hours=4), "mod_disabled", [{"name": "Rustic Doors Complete"}]))
    entries.sort(key=lambda entry: entry["at"])
    kinds = sorted({entry["kind"] for entry in entries})
    runs = sum(1 for entry in entries if entry["kind"] == "program_run")
    load_order = {"instance_id": MAIN["id"], "active_plugins": list(BASE_PLUGINS), "base_plugins": list(BASE_PLUGINS),
                  "mods": [], "unchecked_plugins": [], "plugin_masters": {}, "disabled_mod_plugins": {}}
    return Scenario("history", "Twelve days of changes to a busy list, with the programs that ran.", [], [], load_order,
                    history=entries, history_profile="Default", page="history", expand=4,
                    expect={"mods": 0, "history": len(entries), "history_kinds": kinds, "history_runs": runs})


def history_huge(count: int = 50_000, seed: int = 17) -> Scenario:
    """The history at its limit: 50,000 entries (only the newest page is drawn)."""

    rng = random.Random(seed)
    entries: list[dict[str, Any]] = []
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    while len(entries) < count:
        batch = _history_entries(rng, 30, 60, start=start + timedelta(days=len(entries) // 50))
        entries.extend(batch)
    entries = sorted(entries[:count], key=lambda entry: entry["at"])
    runs = sum(1 for entry in entries if entry["kind"] == "program_run")
    load_order = {"instance_id": MAIN["id"], "active_plugins": list(BASE_PLUGINS), "base_plugins": list(BASE_PLUGINS),
                  "mods": [], "unchecked_plugins": [], "plugin_masters": {}, "disabled_mod_plugins": {}}
    return Scenario("history_huge", f"{count:,} history entries.", [], [], load_order, history=entries,
                    history_profile="Default", page="history", expand=4,
                    expect={"mods": 0, "history": len(entries), "history_runs": runs})


SCENARIOS: dict[str, Callable[[], Scenario]] = {
    "empty": empty, "typical": typical, "typical_search": typical_search, "every_state": every_state,
    "changed_installers": changed_installers, "new_patch_alerts": new_patch_alerts, "huge": huge,
    "long_names": long_names, "many_cards": many_cards, "file_search": file_search,
    "compare_choices": compare_choices, "plain_mods": plain_mods, "plain_search": plain_search, "plain_many": plain_many,
    "history": history, "history_huge": history_huge,
}


def build(name: str) -> Scenario:
    return SCENARIOS[name]()


# ---------- writing a scenario to a database ----------

def materialize(scenario: Scenario, folder: Path) -> Path:
    """Write the scenario into ``folder/logger.sqlite3`` and return that path."""

    from fomod_logger_support.core.storage import EventStore

    folder.mkdir(parents=True, exist_ok=True)
    database = folder / "logger.sqlite3"
    with EventStore(database) as store:
        # A throw-away database: skip the fsync per event so 2,000+ events build quickly.
        store._connection.execute("PRAGMA synchronous = OFF")
        store._connection.execute("PRAGMA journal_mode = MEMORY")
        for event in scenario.events:
            store.ingest(event)
        for instance_id, mod, when in scenario.removed:
            store.mark_removed(instance_id, mod, when)
        for event_id in scenario.hidden:
            store.set_hidden(event_id, True)
        for review in scenario.reviews:
            store.save_review(review)
        for event_id in scenario.seen:
            store.set_changes_seen(event_id, True)
        if scenario.history:
            _write_history(store, scenario)
        for alert in scenario.alerts:
            store.add_patch_alerts(MAIN["id"], alert["mod"], "", [(alert["event_id"], alert["option_id"])])
        with store._connection:
            for path, error in scenario.problems:
                store._connection.execute(
                    "INSERT INTO import_errors (path, directory, error) VALUES (?, ?, ?)", (path, "events", error))
    return database


def _write_history(store, scenario: Scenario) -> None:
    from fomod_logger_support.core import history_store

    connection = store._connection
    with connection:
        for entry in scenario.history:
            history_store._insert(connection, MAIN["id"], entry["profile"], entry, entry["at"],
                                 source="record" if entry["kind"] == "program_run" or not entry["profile"] else "snapshot",
                                 offline=entry["offline"])
