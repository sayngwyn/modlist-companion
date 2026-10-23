"""What changed in a mod's FOMOD installer between two installs of it.

A mod that is updated or reinstalled can offer new options, drop old ones or rename them. People
most often miss the new compatibility patches, so those are called out: an added option that is
made for a plugin in the current list.

Works on name trees: steps > groups > options with a ``name`` (and ``selected``). Ids, ``enabled``,
``visible`` and the patch evidence (``plugins``, ``masters``, ``detects``) are used when present and
never required. An option with none of the three evidence keys is never called a patch.

Matching is by names, as reviews.py does (``option_walk``): the same step, group and option name
(with an occurrence count for repeats) is the same option, whatever its position or id. Options
that don't match are then paired up as renames when they sit in the same group, between the same
two unchanged neighbours, in equal numbers (or, if the numbers differ, when the labels are clearly
the same one reworded); a name that moved to another group (or whose group was renamed) counts as
unchanged, not as new. Pure reordering is ignored.

Beside the installer changes, the same comparison lists the picks that differ: an option present in
both installs that is picked now and wasn't (``picked``), or was and isn't (``unpicked``). Options on
a page the installer skipped in either install are left out, since a skipped page picks nothing.
Those are not installer changes: ``changed`` stays about the installer, ``picks_changed`` is about
the picks.
"""

from __future__ import annotations

import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Iterable

from .missed_patches import PatchLookup, own_plugins
from .reviews import option_walk

_EVIDENCE_KEYS = ("plugins", "masters", "detects")
_SIMILAR = 0.8  # how alike two labels must be to be one label reworded


def _fold(value: str) -> str:
    return value.casefold()


def _when(event: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(event.get("completed_at", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _record(step: dict[str, Any], group: dict[str, Any], option: dict[str, Any]) -> dict[str, Any]:
    return {
        "option_id": option.get("id", ""),
        "name": option.get("name", ""),
        "step_name": step.get("name", ""),
        "group_name": group.get("name", ""),
        "selected": option.get("selected") is True,
        "available": option.get("enabled") is not False and step.get("visible") is not False,
    }


def _evidence(option: dict[str, Any]) -> tuple[frozenset[str], ...]:
    """The plugin names an option's patch evidence holds, folded: (plugins, masters, detects)."""

    return tuple(frozenset(_fold(name) for name in option.get(key, ())) for key in _EVIDENCE_KEYS)


def _has_evidence(option: dict[str, Any]) -> bool:
    return any(key in option for key in _EVIDENCE_KEYS)


def _patch(option: dict[str, Any], name: str, new: dict[str, Any], lookup: PatchLookup | None,
           own: set[str]) -> dict[str, Any] | None:
    """Evidence that an added option is a patch for plugins in the current list."""

    if lookup is None or not any(key in option for key in _EVIDENCE_KEYS):
        return None
    found = lookup.target(option, name, own=own, mod_name=new.get("mod", {}).get("name", ""))
    if found is None:
        return None
    kind, needs, state = found
    plugins = list(option.get("plugins", []))
    return {
        "kind": kind,
        "needs": needs,
        "state": state,                       # "missing" (target is on) or "needs-disabled"
        "plugins": plugins,
        "picked": option.get("selected") is True,
        "have": any(_fold(plugin) in lookup.active for plugin in plugins),  # already in the list
    }


def compare_events(old: dict[str, Any], new: dict[str, Any],
                   load_order: dict[str, Any] | None = None, *,
                   lookup: PatchLookup | None = None, pairs: bool = False) -> dict[str, Any]:
    """The installer changes from install ``old`` to install ``new`` of the same mod.

    ``load_order`` (the plugin list MO2 reports) lets added patch options be recognised; without
    it, or for options with no patch evidence, nothing is called a patch.

    With ``pairs`` the result also has ``matched``: every option of ``old`` that has a counterpart in ``new``,
    as (path in old, path in new) in ``option_walk``'s terms (the same option, a renamed one, or one that
    moved to another group). ``core/compare_choices`` builds its side-by-side comparison from it.
    """

    if lookup is None and load_order is not None:
        lookup = PatchLookup(load_order)
    old_items = [(step, group, option, path) for step, group, option, path in option_walk(old)]
    new_items = [(step, group, option, path) for step, group, option, path in option_walk(new)]
    old_paths = {path for *_rest, path in old_items}
    new_paths = {path for *_rest, path in new_items}

    # The options of each group, in order, with what they are anchored to: the nearest unchanged
    # option before them in that group.
    def gaps(items, other_paths):
        groups: dict[tuple[str, str], dict[Any, list]] = {}
        anchor: dict[tuple[str, str], Any] = {}
        for step, group, option, path in items:
            key = (step["name"], group["name"])
            if path in other_paths:
                anchor[key] = path
            else:
                groups.setdefault(key, {}).setdefault(anchor.get(key), []).append((step, group, option, path))
        return groups

    old_gaps, new_gaps = gaps(old_items, new_paths), gaps(new_items, old_paths)
    renamed, renamed_old, renamed_new = [], set(), set()
    rename_pairs: list[tuple[Any, Any]] = []
    # Each option of the new install that has a counterpart in the old one, by its path: (old step, old option).
    counterpart = {path: (step, option) for step, _g, option, path in old_items if path in new_paths}
    for key, by_anchor in new_gaps.items():
        for anchor, added in by_anchor.items():
            removed = old_gaps.get(key, {}).get(anchor, [])
            for was, now in _pairs(removed, added):
                _s, _g, was_option, was_path = was
                step, group, option, path = now
                record = {**_record(step, group, option), "old_name": was_option.get("name", ""), "patch": None}
                if _evidence(was_option) != _evidence(option):
                    # Reworded, but it now points at other plugins: it may be a new patch after all.
                    record["patch"] = _patch(option, option.get("name", ""), new, lookup,
                                             own_plugins(new) if lookup is not None else set())
                renamed.append(record)
                renamed_old.add(was_path)
                renamed_new.add(path)
                rename_pairs.append((was_path, path))
                counterpart[path] = (_s, was_option)

    left_old = [item for item in old_items if item[3] not in new_paths and item[3] not in renamed_old]
    left_new = [item for item in new_items if item[3] not in old_paths and item[3] not in renamed_new]
    # A name that appears once on each side moved to another group (or its group was renamed).
    def names(items):
        counts: dict[str, int] = {}
        for item in items:
            counts[_fold(item[2].get("name", ""))] = counts.get(_fold(item[2].get("name", "")), 0) + 1
        return counts

    old_names, new_names = names(left_old), names(left_new)
    moved = {name for name, count in old_names.items() if count == 1 and new_names.get(name) == 1}
    moved_pairs: list[tuple[Any, Any]] = []
    if pairs and moved:
        was_at = {_fold(item[2].get("name", "")): item[3] for item in left_old}
        now_at = {_fold(item[2].get("name", "")): item[3] for item in left_new}
        moved_pairs = [(was_at[name], now_at[name]) for name in moved]
    left_old = [item for item in left_old if _fold(item[2].get("name", "")) not in moved]
    left_new = [item for item in left_new if _fold(item[2].get("name", "")) not in moved]

    picked, unpicked = [], []
    for step, group, option, path in new_items:
        if path not in counterpart:
            continue
        was_step, was_option = counterpart[path]
        if was_step.get("visible") is False or step.get("visible") is False:
            continue
        before, now = was_option.get("selected") is True, option.get("selected") is True
        if now and not before:
            picked.append(_record(step, group, option))
        elif before and not now:
            unpicked.append(_record(step, group, option))

    own = own_plugins(new) if lookup is not None else set()
    added = []
    for step, group, option, _path in left_new:
        record = _record(step, group, option)
        record["patch"] = _patch(option, option.get("name", ""), new, lookup, own)
        added.append(record)
    removed = [_record(step, group, option) for step, group, option, _path in left_old]

    old_steps = {step["name"] for step in old["steps"]}
    new_steps = {step["name"] for step in new["steps"]}
    old_groups = {(step["name"], group["name"]) for step in old["steps"] for group in step["groups"]}
    new_groups = {(step["name"], group["name"]) for step in new["steps"] for group in step["groups"]}
    added_steps = _unique(item["step_name"] for item in added if item["step_name"] not in old_steps)
    removed_steps = _unique(item["step_name"] for item in removed if item["step_name"] not in new_steps)
    added_groups = _unique((item["step_name"], item["group_name"]) for item in added
                           if item["step_name"] in old_steps
                           and (item["step_name"], item["group_name"]) not in old_groups)
    removed_groups = _unique((item["step_name"], item["group_name"]) for item in removed
                             if item["step_name"] in new_steps
                             and (item["step_name"], item["group_name"]) not in new_groups)
    result = {
        "mod_name": new.get("mod", {}).get("name", ""),
        "instance_id": new.get("instance", {}).get("id", ""),
        "event_id": new.get("event_id", ""),
        "old_event_id": old.get("event_id", ""),
        "completed_at": new.get("completed_at", ""),
        "old_version": old.get("mod", {}).get("version", ""),
        "new_version": new.get("mod", {}).get("version", ""),
        "hidden": bool(new.get("hidden")),
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "patches": [item for item in added + renamed if item["patch"]],
        "picked": picked,                     # picked now, not in the previous install (options in both)
        "unpicked": unpicked,                 # picked in the previous install, not now
        "picks_changed": bool(picked or unpicked),
        "steps_added": added_steps,
        "steps_removed": removed_steps,
        "groups_added": added_groups,
        "groups_removed": removed_groups,
        "changed": bool(added or removed or renamed),
    }
    if pairs:
        result["matched"] = [(path, path) for path in old_paths & new_paths] + rename_pairs + moved_pairs
    return result


def _slot(was: dict[str, Any], now: dict[str, Any]) -> bool:
    """Whether two options in the same slot are one option relabelled (see ``_pairs``)."""

    if not _has_evidence(was) and not _has_evidence(now):
        return True
    if _has_evidence(was) and _has_evidence(now) and _evidence(was)[0] == _evidence(now)[0]:
        return True
    return _similar(was.get("name", ""), now.get("name", ""))


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, _words(a), _words(b), autojunk=False).ratio() >= _SIMILAR


def _words(name: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", name.casefold().replace("&", " and ")).split())


def _pairs(removed: list, added: list) -> list[tuple]:
    """Which removed options were renamed to which added ones, among those between the same two
    unchanged neighbours. Equal numbers pair up in order when nothing contradicts it: a pair is kept
    if the labels are alike, or if neither option carries patch evidence (a tree of names only), or
    both do and name the same plugins. Otherwise only labels that are clearly the same one reworded
    pair, best match first."""

    if not removed or not added:
        return []
    if len(removed) == len(added):
        return [(was, now) for was, now in zip(removed, added) if _slot(was[2], now[2])]
    was_words = [_words(item[2].get("name", "")) for item in removed]
    scored = []
    for j, item in enumerate(added):
        matcher = SequenceMatcher(None, autojunk=False)
        matcher.set_seq2(_words(item[2].get("name", "")))   # its statistics are reused for every comparison
        for i, words in enumerate(was_words):
            matcher.set_seq1(words)
            # The cheap upper bounds first: most pairs of labels are nothing alike.
            if (matcher.real_quick_ratio() >= _SIMILAR and matcher.quick_ratio() >= _SIMILAR
                    and matcher.ratio() >= _SIMILAR):
                scored.append((matcher.ratio(), i, j))
    pairs, used_old, used_new = [], set(), set()
    for _score, i, j in sorted(scored, reverse=True):
        if i not in used_old and j not in used_new:
            used_old.add(i)
            used_new.add(j)
            pairs.append((removed[i], added[j]))
    return pairs


def _unique(values: Iterable) -> list:
    return list(dict.fromkeys(values))


def installer_changes(events: Iterable[dict[str, Any]], load_order: dict[str, Any] | None = None, *,
                      instance_id: str = "", picks: bool = False) -> list[dict[str, Any]]:
    """For each mod with two or more installs: what its newest install changed from the one directly
    before it (even if that one was removed since), if anything. Newest first.

    Only installer changes count, unless ``picks`` is set: then an install whose picks differ is listed
    too (check ``changed`` for the installer and ``picks_changed`` for the picks).

    A mod whose newest install was removed is gone and has nothing to show. ``events`` is every
    install (the history), in any order.
    """

    by_mod: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        if instance_id and event.get("instance", {}).get("id") != instance_id:
            continue
        key = (event.get("instance", {}).get("id", ""), event.get("mod", {}).get("name", "").lower())
        by_mod.setdefault(key, []).append(event)
    lookups: dict[str, PatchLookup | None] = {}
    found: list[tuple[tuple[float, str], dict[str, Any]]] = []
    for (instance, _name), installs in by_mod.items():
        installs.sort(key=lambda event: (_when(event), event.get("event_id", "")), reverse=True)
        if installs[0].get("removed_at") or installs[0].get("kind") == "plain":
            continue                # gone, or now a mod without an installer: nothing of an installer to compare
        # Installs without an installer have no options, so they can't be compared with those that do.
        installs = [event for event in installs if event.get("kind") != "plain"]
        if len(installs) < 2:
            continue
        if instance not in lookups:
            lookups[instance] = (PatchLookup(load_order)
                                 if load_order is not None and load_order.get("instance_id") == instance else None)
        result = compare_events(installs[1], installs[0], lookup=lookups[instance])
        if result["changed"] or (picks and result["picks_changed"]):
            found.append(((_when(installs[0]), installs[0].get("event_id", "")), result))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [result for _key, result in found]
