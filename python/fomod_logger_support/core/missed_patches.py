"""Find skipped FOMOD options that look like patches for plugins in the current list.

Evidence, strongest first:

* ``detects`` - the FOMOD's own conditions look for this plugin (auto-detection).
* ``masters`` - the option's plugin requires this plugin as a master.
* ``name``    - asset-only options (no plugins) whose label names an installed mod.

Nothing here decides that a patch is required; it lists what is worth a look.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_WORD = re.compile(r"[^0-9a-z]+")
_PLUGIN_SUFFIXES = (".esp", ".esm", ".esl")
# Words too common in option labels and mod names to identify a mod on their own.
_GENERIC = frozenset({
    "patch", "patches", "compatibility", "compat", "main", "core", "file", "files",
    "optional", "options", "option", "texture", "textures", "mesh", "meshes",
    "update", "fix", "fixes", "addon", "plugin", "plugins", "version", "skyrim",
    "special", "edition", "anniversary", "fallout",
})
_MIN_NAME_LENGTH = 4


def _fold(value: str) -> str:
    return value.casefold()


def _words(value: str) -> str:
    return " ".join(_WORD.split(value.casefold())).strip()


def _stem(plugin: str) -> str:
    lower = plugin.casefold()
    for suffix in _PLUGIN_SUFFIXES:
        if lower.endswith(suffix):
            return plugin[: -len(suffix)]
    return plugin


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        if _fold(value) not in seen:
            seen.add(_fold(value))
            result.append(value)
    return result


class _NameIndex:
    """Installed plugins and mods that can be matched by name, built once per call.

    What depends on the installer being checked (its own plugins, its own mod) is
    applied when matching, so the list itself is shared by every install. Phrases
    are indexed by their first word: a phrase can only appear in a label that
    contains that word, so a label is compared with a handful of phrases rather
    than with every installed name.
    """

    def __init__(self, load_order: dict[str, Any]):
        base = {_fold(name) for name in load_order.get("base_plugins", [])}
        candidates = []  # (display name, folded phrase, kind, folded name), in the order they are tried
        for plugin in load_order.get("active_plugins", []):
            if _fold(plugin) in base:
                continue
            candidates.append((plugin, _words(_stem(plugin)), "plugin", _fold(plugin)))
        for name in load_order.get("mods", []):
            candidates.append((name, _words(name), "mod", _fold(name)))
        self._by_first_word: dict[str, list[tuple[int, str, str, str, str]]] = {}
        for order, (display, phrase, kind, key) in enumerate(candidates):
            if len(phrase) < _MIN_NAME_LENGTH or phrase in _GENERIC:
                continue
            words = phrase.split()
            if all(word in _GENERIC for word in words):
                continue
            self._by_first_word.setdefault(words[0], []).append((order, display, phrase, kind, key))

    def match(self, label: str, own: set[str], mod_name: str) -> str | None:
        """The longest installed name found in ``label`` (the earliest of equals), skipping the
        installer's own plugins and its own mod."""

        text = f" {_words(label)} "
        mod_key = _fold(mod_name)
        best: tuple[int, int, str] | None = None
        for word in set(text.split()):
            for order, display, phrase, kind, key in self._by_first_word.get(word, ()):
                if (key in own) if kind == "plugin" else (key == mod_key):
                    continue
                if f" {phrase} " not in text:
                    continue
                if best is None or len(phrase) > best[0] or (len(phrase) == best[0] and order < best[1]):
                    best = (len(phrase), order, display)
        return best[2] if best else None


class PatchLookup:
    """Decides which plugins in the current list an option's evidence points at.

    The one place the "is this a patch for something I have" rules live, so the missed-patch
    check and the installer-changes check mean the same thing. Built once per load order; the
    name index is built the first time a name guess is needed.
    """

    def __init__(self, load_order: dict[str, Any]):
        self.load_order = load_order
        self.active = {_fold(name) for name in load_order.get("active_plugins", [])}
        self.base = {_fold(name) for name in load_order.get("base_plugins", [])}
        # Installed but switched off, and where: these make a patch "present but
        # disabled" rather than missing.
        self.switched_off: dict[str, str] = {}
        for name, mod in (load_order.get("disabled_mod_plugins") or {}).items():
            self.switched_off.setdefault(_fold(name), f"its mod “{mod}” is disabled")
        for name in load_order.get("unchecked_plugins") or []:
            if _fold(name) not in self.active:
                self.switched_off[_fold(name)] = "unchecked in the plugin list"
        self._names: _NameIndex | None = None

    def off(self, names_: Iterable[str]) -> list[dict[str, Any]]:
        return [{"plugin": name, "where": self.switched_off[_fold(name)]}
                for name in names_ if _fold(name) in self.switched_off]

    def target(self, evidence: dict[str, Any], label: str, *, own: set[str], mod_name: str,
               automatic: bool = False) -> tuple[str, list[str], str] | None:
        """(kind, plugins it is made for, state) when the option looks like a patch for plugins in
        the list, else None. ``state`` is "missing" or "needs-disabled" (it would apply once those are on)."""

        plugins = evidence.get("plugins", [])

        def relevant(names_: Iterable[str]) -> list[str]:
            return _unique(n for n in names_ if _fold(n) not in self.base and _fold(n) not in own)

        detects = relevant(evidence.get("detects", []))
        masters = relevant(evidence.get("masters", []))
        needs = _unique(detects + masters)
        if needs:
            if not all(_fold(name) in self.active or _fold(name) in self.switched_off for name in needs):
                return None
            state = "missing" if all(_fold(name) in self.active for name in needs) else "needs-disabled"
            return ("detects" if detects else "masters"), needs, state
        if (
            # No plugins to go by, or their masters weren't read (the
            # installer skips that for very large archives).
            (not plugins or "masters" not in evidence)
            and not evidence.get("detects")
            and not automatic
        ):
            if self._names is None:
                self._names = _NameIndex(self.load_order)  # once per call, for every install
            matched = self._names.match(label, own, mod_name)
            if matched:
                return "name", [matched], "missing"
        return None


def own_plugins(event: dict[str, Any]) -> set[str]:
    """Plugins this installer itself provides: never evidence of another mod."""

    return {
        _fold(plugin)
        for item in [
            option
            for step in event.get("steps", [])
            for group in step.get("groups", [])
            for option in group.get("options", [])
        ] + list(event.get("automatic_installs", []) or [])
        for plugin in item.get("plugins", [])
    }


def game_plugin_names(
    names: Iterable[str],
    origins: dict[str, str],
    game_lists: Iterable[Iterable[str]],
) -> list[str]:
    """Plugins that belong to the game itself: base game, DLC and Creation Club.

    MO2 reports some of these (Skyrim SE's DLC, for example) with an origin other
    than the game's Data folder, so the game plugin's own lists count too.
    """

    from_game = {_fold(name) for names_ in game_lists for name in names_}
    return [
        name for name in names
        if _fold(origins.get(name, "")) == "data" or _fold(name) in from_game
    ]


def _candidates(event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for step in event.get("steps", []):
        for group in step.get("groups", []):
            for option in group.get("options", []):
                if option.get("selected"):
                    continue
                yield {
                    "option_id": option["id"],
                    "label": option.get("name") or "Unnamed option",
                    "step_name": step.get("name", ""),
                    "group_name": group.get("name", ""),
                    "available": bool(option.get("enabled")) and bool(step.get("visible")),
                    "automatic": False,
                    "evidence": option,
                }
    for item in event.get("automatic_installs", []) or []:
        if item.get("installed"):
            continue
        yield {
            "option_id": "",
            "automatic_id": item["id"],
            "label": "Automatic install",
            "step_name": "",
            "group_name": "",
            "available": False,
            "automatic": True,
            "evidence": item,
        }


def _picked(event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Options (and automatic installs) that did install files."""

    for step in event.get("steps", []):
        for group in step.get("groups", []):
            for option in group.get("options", []):
                if option.get("selected"):
                    yield {"option_id": option["id"], "label": option.get("name") or "Unnamed option",
                           "step_name": step.get("name", ""), "group_name": group.get("name", ""),
                           "automatic": False, "evidence": option}
    for item in event.get("automatic_installs", []) or []:
        if item.get("installed"):
            yield {"option_id": "", "label": "Automatic install", "step_name": "", "group_name": "",
                   "automatic": True, "evidence": item}


def has_patch_evidence(event: dict[str, Any]) -> bool:
    """True when the installer recorded plugins/detection rules for this event."""

    if "automatic_installs" in event:
        return True
    return any(
        "plugins" in option
        for step in event.get("steps", [])
        for group in step.get("groups", [])
        for option in group.get("options", [])
    )


def _plain_missing_masters(event: dict[str, Any], active: set[str], installed: set[str],
                           masters_of: dict[str, list[str]], settled: set[tuple[str, str]],
                           reminded: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """The plugins of a mod that was not installed through an installer which are switched on while a master they
    need isn't in the list: the game won't start with them. The masters MO2 reports are used; the ones the record
    read from the plugin's header stand in when MO2 does not list the plugin."""

    key = (event["event_id"], "")
    if key in settled:
        return []
    found = []
    for plugin in event.get("plain", {}).get("plugins", []):
        name = plugin["name"]
        if _fold(name) not in active:
            continue
        masters = masters_of.get(_fold(name))
        if masters is None:
            masters = plugin.get("masters", [])
        missing = [master for master in masters if _fold(master) not in installed]
        if not missing:
            continue
        found.append({
            "event_id": event["event_id"], "mod_name": event.get("mod", {}).get("name", ""), "option_id": "",
            "label": name, "step_name": "", "group_name": "", "plugins": [name], "needs": [], "kind": "plain",
            "available": True, "automatic": False, "reminder": key in reminded, "state": "missing-masters",
            "disabled": [{"plugin": name, "where": "switched on", "on": True, "missing": missing}],
        })
    return found


def find_missed_patches(
    events: Iterable[dict[str, Any]],
    load_order: dict[str, Any],
    reviews: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Return possible missed patches for the instance described by ``load_order``."""

    instance_id = load_order.get("instance_id")
    lookup = PatchLookup(load_order)
    active, base, switched_off = lookup.active, lookup.base, lookup.switched_off

    # Plugin masters, to tell "switched off" from "can't load": a master that
    # is nowhere in the list (not even switched off).
    installed = active | base | set(switched_off)
    masters_of = {_fold(name): list(masters)
                  for name, masters in (load_order.get("plugin_masters") or {}).items()}

    def missing_masters(plugin: str) -> list[str]:
        return [master for master in masters_of.get(_fold(plugin), []) if _fold(master) not in installed]

    off = lookup.off

    # A resolved or dismissed reminder settles a patch, so it is not listed. An
    # open reminder keeps it listed, marked as having one, until it is settled.
    # Automatic installs have no option of their own, so an installer-level
    # reminder (option_id "") covers them.
    settled: set[tuple[str, str]] = set()
    reminded: set[tuple[str, str]] = set()
    for review in reviews:
        keys = {(review.get("event_id", ""), review.get("option_id", ""))}
        latest = review.get("latest_event") or {}
        if latest.get("event_id"):
            # Ids differ between records; reviews.py maps the choice by name.
            option = review.get("latest_option_id", review.get("option_id", ""))
            if option or not review.get("option_id"):
                keys.add((latest["event_id"], option))
        (reminded if review.get("state", "pending") == "pending" else settled).update(keys)

    missed = []
    without_evidence = 0
    for event in events:
        if event.get("instance", {}).get("id") != instance_id or event.get("removed_at"):
            continue
        if event.get("kind") == "plain":
            # No installer, so no options to skip; but its plugins can still fail to load.
            missed.extend(_plain_missing_masters(event, active, installed, masters_of, settled, reminded))
            continue
        if not has_patch_evidence(event):
            without_evidence += 1
            continue
        own = own_plugins(event)
        mod_name = event.get("mod", {}).get("name", "")

        for candidate in _candidates(event):
            key = (event["event_id"], candidate["option_id"])
            if key in settled:
                continue
            evidence = candidate["evidence"]
            plugins = evidence.get("plugins", [])
            if any(_fold(plugin) in active for plugin in plugins):
                continue  # already in the list, e.g. installed from another archive

            found = lookup.target(evidence, candidate["label"], own=own, mod_name=mod_name,
                                  automatic=candidate["automatic"])
            if found is None:
                continue
            kind, needs, state = found
            # The patch plugin itself is installed, only switched off.
            patch_off = off(plugins)
            if patch_off and state == "missing":
                state = "patch-disabled"
            label = candidate["label"]
            if candidate["automatic"]:
                label = f"Automatic patch for {', '.join(needs)}"
            missed.append({
                "event_id": event["event_id"],
                "mod_name": mod_name,
                "option_id": candidate["option_id"],
                "label": label,
                "step_name": candidate["step_name"],
                "group_name": candidate["group_name"],
                "plugins": plugins,
                "needs": needs,
                "kind": kind,
                "available": candidate["available"],
                "automatic": candidate["automatic"],
                "reminder": key in reminded,
                "state": state,
                "disabled": patch_off if state == "patch-disabled" else off(needs),
            })

        # Options that were picked and installed, but whose plugin is switched
        # off now (MO2 leaves a newly installed plugin unchecked, for one).
        for picked in _picked(event):
            key = (event["event_id"], picked["option_id"])
            plugins = picked["evidence"].get("plugins", [])
            disabled = [entry for entry in off(plugins) if _fold(entry["plugin"]) not in active]
            for entry in disabled:
                entry["missing"] = missing_masters(entry["plugin"])
            # Switched on with a master missing: MO2 ticks a new plugin itself,
            # and the game won't start with it like this.
            disabled += [{"plugin": plugin, "where": "switched on", "on": True, "missing": missing_masters(plugin)}
                         for plugin in plugins if _fold(plugin) in active and missing_masters(plugin)]
            if key in settled or not disabled:
                continue
            # Off because a master isn't installed: switching it on won't help.
            state = "missing-masters" if any(entry["missing"] for entry in disabled) else "picked-disabled"
            missed.append({
                "event_id": event["event_id"],
                "mod_name": mod_name,
                "option_id": picked["option_id"],
                "label": picked["label"],
                "step_name": picked["step_name"],
                "group_name": picked["group_name"],
                "plugins": plugins,
                "needs": [],
                "kind": "picked",
                "available": True,
                "automatic": picked["automatic"],
                "reminder": key in reminded,
                "state": state,
                "disabled": disabled,
            })

    order = {"detects": 0, "masters": 1, "name": 2, "picked": 3, "plain": 4}
    states = {"missing": 0, "missing-masters": 1, "picked-disabled": 2, "patch-disabled": 3, "needs-disabled": 4}
    missed.sort(key=lambda item: (states[item["state"]], order[item["kind"]],
                                  _fold(item["mod_name"]), _fold(item["label"])))
    return {"missed": missed, "events_without_evidence": without_evidence}
