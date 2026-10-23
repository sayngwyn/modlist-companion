"""Compare the FOMOD choices of two installs: of one mod, or of every mod in two MO2 instances.

MO2 shares a mod's folder between all profiles, so one MO2 instance can't hold two different sets of
choices for the same mod unless the mod is installed twice under different names. The choices worth
comparing therefore live in three places, and this module covers each:

* **two MO2 instances** that use this MO2 installation (``compare_instances``): mods are paired by Nexus
  ID, then by archive file name, then by mod name, using each side's newest live install;
* **two copies of a mod** in one instance (``find_duplicates``, then ``compare_installs``): two live mods
  with different names that came from the same archive, or from the same Nexus ID with installers that
  share options;
* **two installs of the same mod** (``installs_of``, then ``compare_installs``): this install against an
  older one.

Beside that, ``read_profiles`` reads which of an instance's profiles have each mod enabled. It only ever
reads ``modlist.txt``.

Everything works on name trees (steps > groups > options with a ``name`` and ``selected``); ids, ``enabled``
and ``visible`` are used when present and never required. Options are matched by the same name rules as
``installer_changes`` (it does the matching), so a reworded option or one that moved to another group is
still the same option. Plain data in, plain data out; no Qt and no MO2.

Side "a" is the left of a comparison and side "b" the right. An option's ``status`` is one of
``same``, ``picked-a`` / ``picked-b`` (in both installers, picked on one side only), ``offered-a`` /
``offered-b`` (the installer offers it on one side only), ``renamed`` (picked alike, labelled differently)
and ``skipped`` (on a page the installer skipped in either install, where nothing is picked).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .file_origin import _key as _path_key
from .installer_changes import compare_events
from .reviews import option_walk

SAME, DIFFERENT, ONLY_A, ONLY_B = "same", "different", "only-a", "only-b"
PICK_STATUSES = ("picked-a", "picked-b")
OFFER_STATUSES = ("offered-a", "offered-b")


# --- helpers -----------------------------------------------------------------------------------------------

def _fold(value: Any) -> str:
    return str(value or "").casefold()


def _archive(event: dict[str, Any]) -> str:
    """The archive's file name, folded ("" when unknown): folders and case don't make two archives differ."""

    return _fold(str((event.get("source") or {}).get("archive") or "").replace("\\", "/").rsplit("/", 1)[-1])


def _nexus(event: dict[str, Any]) -> int:
    value = (event.get("mod") or {}).get("nexus_id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _name(event: dict[str, Any]) -> str:
    return _fold((event.get("mod") or {}).get("name"))


def _when(event: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(event.get("completed_at", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _newest_first(event: dict[str, Any]) -> tuple[float, str]:
    return _when(event), str(event.get("event_id", ""))


def _option_names(event: dict[str, Any]) -> frozenset[str]:
    return frozenset(_fold(option.get("name")) for step in event.get("steps", []) for group in step.get("groups", [])
                     for option in group.get("options", []))


def _brief(event: dict[str, Any] | None) -> dict[str, Any]:
    """What a result says about one side, enough to label it."""

    if event is None:
        return {}
    return {
        "event_id": event.get("event_id", ""),
        "mod_name": (event.get("mod") or {}).get("name", ""),
        "version": (event.get("mod") or {}).get("version", ""),
        "completed_at": event.get("completed_at", ""),
        "archive": (event.get("source") or {}).get("archive", ""),
        "nexus_id": _nexus(event),
        "instance_id": (event.get("instance") or {}).get("id", ""),
        "instance_label": (event.get("instance") or {}).get("label", ""),
        "removed": bool(event.get("removed_at")),
    }


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"


# --- mods that were not installed through an installer -----------------------------------------------------

def is_plain(event: dict[str, Any] | None) -> bool:
    return bool(event) and event.get("kind") == "plain"


def _plugin_names(event: dict[str, Any]) -> list[str]:
    return [plugin["name"] for plugin in (event.get("plain") or {}).get("plugins", [])]


def plain_differences(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any] | None:
    """How two installs differ when one of them has no installer choices (a plain install): the version, the
    archive, the plugins and the files. ``None`` when neither is plain. Files and plugins are only compared when
    both are plain (an installer's record doesn't list every file it installs); the file lists themselves are not
    needed, because each plain record carries a fingerprint of its list."""

    if not (is_plain(a) or is_plain(b)):
        return None
    both = is_plain(a) and is_plain(b)
    version = [(a.get("mod") or {}).get("version", "") or "", (b.get("mod") or {}).get("version", "") or ""]
    archive = [(a.get("source") or {}).get("archive", "") or "", (b.get("source") or {}).get("archive", "") or ""]
    result: dict[str, Any] = {
        "both_plain": both,
        "installer": "" if both else ("a" if not is_plain(a) else "b"),      # the side that had an installer
        "version": version, "version_differs": _fold(version[0]) != _fold(version[1]),
        "archive": archive, "archive_differs": _archive(a) != _archive(b),
        "files": None, "plugins": None,
    }
    if both:
        pa, pb = a["plain"], b["plain"]
        result["files"] = {
            "count_a": pa.get("file_count", 0), "count_b": pb.get("file_count", 0),
            "cut_a": bool(pa.get("files_truncated")), "cut_b": bool(pb.get("files_truncated")),
            "differs": pa.get("files_digest") != pb.get("files_digest"),
        }
        names_a, names_b = _plugin_names(a), _plugin_names(b)
        folded_a, folded_b = {_fold(n) for n in names_a}, {_fold(n) for n in names_b}
        result["plugins"] = {
            "only_a": [n for n in names_a if _fold(n) not in folded_b],
            "only_b": [n for n in names_b if _fold(n) not in folded_a],
        }
    result["count"] = sum([
        bool(result["installer"]), result["version_differs"], result["archive_differs"],
        bool(result["files"] and result["files"]["differs"]),
        bool(result["plugins"] and (result["plugins"]["only_a"] or result["plugins"]["only_b"]))])
    return result


def file_differences(files_a: Iterable[str], files_b: Iterable[str]) -> dict[str, Any]:
    """Which files only one side has (paths as recorded, in order) and how many both have; paths are compared
    without regard to case or the kind of slash."""

    a, b = list(files_a), list(files_b)
    keys_a, keys_b = {_path_key(path) for path in a}, {_path_key(path) for path in b}
    return {"only_a": [path for path in a if _path_key(path) not in keys_b],
            "only_b": [path for path in b if _path_key(path) not in keys_a],
            "same": len(keys_a & keys_b)}


# --- two installs ------------------------------------------------------------------------------------------

def compare_installs(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Side-by-side comparison of two installs (any two events): pages > groups > options.

    The pages follow ``b``'s installer, with what only ``a`` offers added to the group it was in (or to a
    page of its own). ``status`` of the result, and of each page and group, is ``same`` or ``different``;
    a page that only one installer has is ``only-a`` / ``only-b``, and one that is skipped in one install
    only is ``skipped-a`` / ``skipped-b``.
    """

    matched = compare_events(a, b, pairs=True)["matched"]
    a_items = {path: (step, group, option) for step, group, option, path in option_walk(a)}
    b_items = {path: (step, group, option) for step, group, option, path in option_walk(b)}
    partner = {new: old for old, new in matched if old in a_items and new in b_items}
    paired_a = set(partner.values())
    a_steps = {_fold(step.get("name")): step for step in a.get("steps", [])}
    b_steps = {_fold(step.get("name")): step for step in b.get("steps", [])}

    pages: dict[tuple[str, int], dict[str, Any]] = {}      # (folded step name, occurrence) -> page
    page_order: list[dict[str, Any]] = []
    group_of: dict[tuple[int, str, int], dict[str, Any]] = {}

    def page_for(step: dict[str, Any], side: str) -> dict[str, Any]:
        name = step.get("name", "")
        key = (_fold(name), 0)
        page = pages.get(key)
        if page is None:
            in_a, in_b = _fold(name) in a_steps, _fold(name) in b_steps
            skipped_a = in_a and a_steps[_fold(name)].get("visible") is False
            skipped_b = in_b and b_steps[_fold(name)].get("visible") is False
            page = {"name": name, "in_a": in_a, "in_b": in_b, "skipped_a": skipped_a, "skipped_b": skipped_b,
                    "groups": [], "status": SAME}
            pages[key] = page
            page_order.append(page)
        return page

    def group_for(page: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
        key = (id(page), _fold(group.get("name")), 0)
        found = group_of.get(key)
        if found is None:
            found = group_of[key] = {"name": group.get("name", ""), "options": [], "status": SAME}
            page["groups"].append(found)
        return found

    def entry(step_a, option_a, step_b, option_b) -> dict[str, Any]:
        skipped_a = step_a is not None and step_a.get("visible") is False
        skipped_b = step_b is not None and step_b.get("visible") is False
        picked_a = None if option_a is None else option_a.get("selected") is True
        picked_b = None if option_b is None else option_b.get("selected") is True
        name_a = None if option_a is None else option_a.get("name", "")
        name_b = None if option_b is None else option_b.get("name", "")
        if option_a is None:
            status = "offered-b"
        elif option_b is None:
            status = "offered-a"
        elif skipped_a or skipped_b:
            status = "skipped"
        elif picked_a != picked_b:
            status = "picked-a" if picked_a else "picked-b"
        elif name_a != name_b:
            status = "renamed"
        else:
            status = SAME
        return {"name": name_b if name_b is not None else name_a, "name_a": name_a, "name_b": name_b,
                "picked_a": picked_a, "picked_b": picked_b, "skipped_a": skipped_a, "skipped_b": skipped_b,
                "status": status}

    for path, (step_b, group_b, option_b) in b_items.items():
        step_a = group_a = option_a = None
        if path in partner:
            step_a, group_a, option_a = a_items[partner[path]]
        page = page_for(step_b, "b")
        group = group_for(page, group_b)
        group["options"].append(entry(step_a, option_a, step_b, option_b))
    for path, (step_a, group_a, option_a) in a_items.items():
        if path in paired_a:
            continue
        page = page_for(step_a, "a")
        group = group_for(page, group_a)
        group["options"].append(entry(step_a, option_a, b_steps.get(_fold(step_a.get("name"))), None))

    # Pages with no options (an empty page) still count: they exist, or not, on each side.
    for steps, side in ((b.get("steps", []), "b"), (a.get("steps", []), "a")):
        for step in steps:
            if (_fold(step.get("name")), 0) not in pages:
                page_for(step, side)

    counts = {"picks": 0, "offered": 0, "renamed": 0, "pages": 0, "options": 0}
    for page in page_order:
        differs = False
        if page["in_a"] and not page["in_b"]:
            page["status"] = ONLY_A
        elif page["in_b"] and not page["in_a"]:
            page["status"] = ONLY_B
        elif page["skipped_a"] != page["skipped_b"]:
            page["status"] = "skipped-a" if page["skipped_a"] else "skipped-b"
        if page["status"] != SAME:
            differs = True
            counts["pages"] += 1
        for group in page["groups"]:
            for option in group["options"]:
                counts["options"] += 1
                status = option["status"]
                if status in PICK_STATUSES:
                    counts["picks"] += 1
                elif status in OFFER_STATUSES:
                    counts["offered"] += 1
                elif status == "renamed":
                    counts["renamed"] += 1
            group["status"] = DIFFERENT if any(o["status"] not in (SAME, "skipped") for o in group["options"]) else SAME
            differs = differs or group["status"] != SAME
        if page["status"] == SAME and differs:
            page["status"] = DIFFERENT
    plain = plain_differences(a, b)
    counts["plain"] = plain["count"] if plain else 0
    differs = counts["picks"] or counts["offered"] or counts["renamed"] or counts["pages"] or counts["plain"]
    return {
        "a": _brief(a), "b": _brief(b), "pages": page_order, "counts": counts,
        "picks_differ": bool(counts["picks"]),
        "installer_differs": bool(counts["offered"] or counts["renamed"] or counts["pages"]),
        "plain": plain,
        "status": DIFFERENT if differs else SAME,
    }


def describe_plain(plain: dict[str, Any]) -> str:
    """One plain sentence on how two installs without installer choices differ."""

    parts = []
    if plain["version_differs"]:
        parts.append(f"the version ({plain['version'][0] or 'unknown'} → {plain['version'][1] or 'unknown'})")
    if plain["archive_differs"]:
        parts.append("the archive")
    if plain["plugins"] and (plain["plugins"]["only_a"] or plain["plugins"]["only_b"]):
        parts.append("the plugins")
    if plain["files"] and plain["files"]["differs"]:
        parts.append("the files")
    if not parts:
        return "Nothing differs: the same version, archive, plugins and files."
    return "These differ: " + ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] + "."


def describe(result: dict[str, Any]) -> str:
    """One plain sentence on how two installs differ ("Identical choices." when they don't)."""

    counts = result["counts"]
    plain = result.get("plain")
    if plain and plain["both_plain"]:
        return describe_plain(plain)
    if plain:
        # One side has installer choices and the other has none: there are no picks to compare.
        differing = [word for word, differs in (("version", plain["version_differs"]),
                                                ("archive", plain["archive_differs"])) if differs]
        return ("One was installed through an installer and the other was not"
                + (f"; the {' and the '.join(differing)} differ" if differing else "") + ".")
    if result["status"] == SAME:
        return "The choices are identical."
    parts = []
    if counts["picks"]:
        only_a = sum(1 for page in result["pages"] for group in page["groups"] for o in group["options"]
                     if o["status"] == "picked-a")
        parts.append(f"{plural(counts['picks'], 'pick')} differ ({only_a} only in A, {counts['picks'] - only_a} only in B)")
    else:
        parts.append("The picks are the same")
    if counts["offered"]:
        parts.append(f"{plural(counts['offered'], 'option')} only in one installer")
    if counts["renamed"]:
        parts.append(f"{plural(counts['renamed'], 'option')} labelled differently")
    if counts["pages"]:
        parts.append(f"{plural(counts['pages'], 'page')} differ between the installers")
    return "; ".join(parts) + "."


# --- the installs of one mod -------------------------------------------------------------------------------

def installs_of(events: Iterable[dict[str, Any]], instance_id: str, mod_name: str) -> list[dict[str, Any]]:
    """Every install of one mod in one instance (removed ones too), newest first."""

    key = _fold(mod_name)
    found = [event for event in events
             if event.get("instance", {}).get("id") == instance_id and _name(event) == key]
    return sorted(found, key=_newest_first, reverse=True)


def mods_with_history(events: Iterable[dict[str, Any]], instance_id: str, *,
                      include_hidden: bool = False) -> list[dict[str, Any]]:
    """The mods of one instance with two or more installs, whose newest install is not removed:
    {"name", "installs"} with the installs newest first, by name."""

    by_mod: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event.get("instance", {}).get("id") == instance_id:
            by_mod.setdefault(_name(event), []).append(event)
    found = []
    for installs in by_mod.values():
        installs.sort(key=_newest_first, reverse=True)
        newest = installs[0]
        if len(installs) < 2 or newest.get("removed_at") or (newest.get("hidden") and not include_hidden):
            continue
        found.append({"name": newest["mod"]["name"], "installs": installs})
    return sorted(found, key=lambda mod: (mod["name"].casefold(), mod["name"]))


# --- live mods, duplicates, instances ----------------------------------------------------------------------

def live_events(events: Iterable[dict[str, Any]], instance_id: str = "", *,
                include_hidden: bool = False) -> list[dict[str, Any]]:
    """The newest install of each mod, for mods still installed (one instance, or all of them): the
    installs ``EventStore.latest`` would give, from any list of events."""

    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        ident = event.get("instance", {}).get("id", "")
        if instance_id and ident != instance_id:
            continue
        key = (ident, _name(event))
        if key not in newest or _newest_first(event) > _newest_first(newest[key]):
            newest[key] = event
    return [event for event in newest.values()
            if not event.get("removed_at") and (include_hidden or not event.get("hidden"))]


def _plausible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two installs that share a Nexus ID can be one mod: they share a name, an archive, or at
    least one installer option. (Nexus IDs are numbers per game, so two games' mods can collide, and one
    Nexus page can have unrelated files; installers with nothing in common have nothing to compare.)"""

    return (_name(a) == _name(b) or (_archive(a) and _archive(a) == _archive(b))
            or bool(_option_names(a) & _option_names(b)))


def find_duplicates(events: Iterable[dict[str, Any]], *, instance_id: str = "",
                    include_hidden: bool = False) -> list[dict[str, Any]]:
    """Groups of two or more live mods of one instance that are copies of each other: the same archive file,
    or the same Nexus ID with installers that share an option. Each group is
    {"instance_id", "instance_label", "reasons", "why", "mods"}, its mods (events) by name; groups by
    instance label and first name."""

    by_instance: dict[str, list[dict[str, Any]]] = {}
    for event in live_events(events, instance_id, include_hidden=include_hidden):
        by_instance.setdefault(event["instance"]["id"], []).append(event)
    groups: list[dict[str, Any]] = []
    for ident, mods in by_instance.items():
        mods.sort(key=lambda event: (_name(event), str(event.get("event_id", ""))))
        parent = list(range(len(mods)))

        def root(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        links: dict[tuple[int, int], set[str]] = {}
        for reason, key in (("archive", _archive), ("nexus", _nexus)):
            seen: dict[Any, list[int]] = {}
            for index, event in enumerate(mods):
                if key(event):
                    seen.setdefault(key(event), []).append(index)
            for members in seen.values():
                for later in members[1:]:
                    if reason == "nexus" and not _plausible(mods[members[0]], mods[later]):
                        continue
                    first = members[0]
                    links.setdefault((first, later), set()).add(reason)
                    parent[root(later)] = root(first)
        clusters: dict[int, list[int]] = {}
        for index in range(len(mods)):
            clusters.setdefault(root(index), []).append(index)
        for members in clusters.values():
            if len(members) < 2:
                continue
            reasons = set()
            for (first, later), why in links.items():
                if first in members and later in members:
                    reasons |= why
            wording = ("same archive and Nexus ID" if reasons == {"archive", "nexus"}
                       else "same archive" if reasons == {"archive"} else "same Nexus ID")
            groups.append({"instance_id": ident, "instance_label": mods[members[0]]["instance"].get("label", ""),
                           "reasons": sorted(reasons), "why": wording, "mods": [mods[i] for i in members]})
    groups.sort(key=lambda group: (_fold(group["instance_label"]), group["instance_id"], _name(group["mods"][0])))
    return groups


def _pair_group(left: list[dict[str, Any]], right: list[dict[str, Any]], tier: str) -> list[tuple[dict, dict]]:
    """Pair the mods of two sides that share one key. One against one pairs (when plausible); more are
    paired by equal name, then by an archive only they share, and the rest stay unpaired."""

    pairs: list[tuple[dict, dict]] = []
    if len(left) == 1 and len(right) == 1:
        if tier != "nexus" or _plausible(left[0], right[0]):
            pairs.append((left[0], right[0]))
        return pairs
    left, right = list(left), list(right)
    for key in (_name, _archive):
        for a in list(left):
            if not key(a):
                continue
            same_a = [x for x in left if key(x) == key(a)]
            same_b = [x for x in right if key(x) == key(a)]
            if len(same_a) == 1 and len(same_b) == 1:
                pairs.append((a, same_b[0]))
                left.remove(a)
                right.remove(same_b[0])
    return pairs


def pair_mods(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[tuple[dict | None, dict | None, str]]:
    """Pair two lists of live installs: by Nexus ID, then archive file name, then mod name. Returns
    (left, right, how) for each pair, then (left, None, "") and (None, right, "") for the rest."""

    open_left, open_right = list(left), list(right)
    pairs: list[tuple[dict | None, dict | None, str]] = []
    for tier, key in (("nexus", _nexus), ("archive", _archive), ("name", _name)):
        by_key: dict[Any, tuple[list, list]] = {}
        for event in open_left:
            if key(event):
                by_key.setdefault(key(event), ([], []))[0].append(event)
        for event in open_right:
            if key(event) in by_key:
                by_key[key(event)][1].append(event)
        for lefts, rights in by_key.values():
            if not lefts or not rights:
                continue
            for a, b in _pair_group(lefts, rights, tier):
                pairs.append((a, b, tier))
                open_left.remove(a)
                open_right.remove(b)
    pairs.extend((event, None, "") for event in open_left)
    pairs.extend((None, event, "") for event in open_right)
    return pairs


def compare_instances(events: Iterable[dict[str, Any]], a_id: str, b_id: str, *,
                      include_hidden: bool = False) -> dict[str, Any]:
    """Compare the picks of two MO2 instances, mod by mod, from each side's newest live installs.

    ``events`` is the history (any events of both instances). Each mod entry has ``status``: ``same``,
    ``different``, ``only-a`` or ``only-b``, the two installs (``a``, ``b``), how they were paired
    (``paired_by``: nexus, archive or name) and, for a pair, the ``detail`` of ``compare_installs``.
    Different mods first, then those only on one side, then identical ones, by name within each.
    """

    events = list(events)
    left = live_events(events, a_id, include_hidden=include_hidden)
    right = live_events(events, b_id, include_hidden=include_hidden)
    mods: list[dict[str, Any]] = []
    for a, b, how in pair_mods(left, right):
        entry: dict[str, Any] = {"a": a, "b": b, "paired_by": how, "detail": None}
        if a is None:
            entry["status"], entry["name"] = ONLY_B, b["mod"]["name"]  # pyright: ignore[reportOptionalSubscript]  # pair_mods gives a or b
        elif b is None:
            entry["status"], entry["name"] = ONLY_A, a["mod"]["name"]
        else:
            entry["detail"] = compare_installs(a, b)
            entry["status"] = entry["detail"]["status"]
            names = (a["mod"]["name"], b["mod"]["name"])
            entry["name"] = names[0] if _fold(names[0]) == _fold(names[1]) else f"{names[0]}  ↔  {names[1]}"
        mods.append(entry)
    order = {DIFFERENT: 0, ONLY_A: 1, ONLY_B: 2, SAME: 3}
    mods.sort(key=lambda entry: (order[entry["status"]], entry["name"].casefold(), entry["name"]))
    counts = {status: sum(1 for entry in mods if entry["status"] == status) for status in order}
    return {
        "a": _instance_brief(events, a_id), "b": _instance_brief(events, b_id), "mods": mods,
        "counts": {"different": counts[DIFFERENT], "only_a": counts[ONLY_A], "only_b": counts[ONLY_B],
                   "same": counts[SAME], "mods": len(mods)},
    }


def _instance_brief(events: list[dict[str, Any]], instance_id: str) -> dict[str, str]:
    for event in events:
        if event.get("instance", {}).get("id") == instance_id:
            return {"id": instance_id, "label": event["instance"].get("label") or instance_id}
    return {"id": instance_id, "label": instance_id}


def describe_instances(result: dict[str, Any]) -> str:
    counts = result["counts"]
    if not counts["mods"]:
        return "Neither instance has a logged mod to compare."
    parts = [f"{counts['different']} with different picks", f"{counts['only_a']} only in A", f"{counts['only_b']} only in B",
             f"{counts['same']} identical"]
    return f"{plural(counts['mods'], 'mod')}: " + ", ".join(parts) + "."


# --- which profiles have a mod enabled ---------------------------------------------------------------------

_MODLIST = "modlist.txt"
_PARSED: dict[str, tuple[int, int, frozenset[str]]] = {}   # modlist path -> (modified ns, size, enabled mod names)


def parse_modlist(text: str) -> set[str]:
    """The names of the enabled mods in a ``modlist.txt``: ``+Name`` is enabled, ``-Name`` disabled and
    ``*Name`` unmanaged (only the first is returned). Comments, blank lines and anything else are ignored."""

    enabled = set()
    for line in text.splitlines():
        if line.startswith("+") and len(line) > 1:
            enabled.add(line[1:].rstrip("\r\n").casefold())
    return enabled


def _enabled_in(path: Path) -> frozenset[str] | None:
    """The enabled mods of one modlist.txt, cached by size and modified time; None when it can't be read."""

    try:
        found = os.stat(path)
    except OSError:
        return None
    stamp = (found.st_mtime_ns, found.st_size)
    cached = _PARSED.get(str(path))
    if cached is not None and cached[:2] == stamp:
        return cached[2]
    try:
        text = path.read_bytes().decode("utf-8-sig", errors="replace")   # read-only
    except OSError:
        return None
    parsed = frozenset(parse_modlist(text))
    _PARSED[str(path)] = (*stamp, parsed)
    return parsed


def read_profiles(folder: Path | str | None) -> dict[str, Any]:
    """Which profiles have each mod enabled, from the ``modlist.txt`` of every profile in ``folder`` (MO2's
    profiles folder, the parent of the current profile's folder). Only reads, never writes.

    Returns {"folder", "profiles": [names], "mods": {folded mod name: [profile names]}}. A missing folder,
    a folder in it with no readable modlist.txt, or odd lines are skipped; the result then just has fewer
    profiles (none at all when nothing could be read)."""

    result: dict[str, Any] = {"folder": str(folder or ""), "profiles": [], "mods": {}}
    if not folder:
        return result
    try:
        entries = sorted(os.scandir(folder), key=lambda entry: (entry.name.casefold(), entry.name))
    except OSError:
        return result
    mods: dict[str, list[str]] = {}
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        enabled = _enabled_in(Path(entry.path) / _MODLIST)
        if enabled is None:
            continue
        result["profiles"].append(entry.name)
        for name in enabled:
            mods.setdefault(name, []).append(entry.name)
    result["mods"] = mods
    return result


def profiles_of(profiles: dict[str, Any] | None, mod_name: str) -> list[str] | None:
    """The profiles that have this mod enabled ([] for none); None when no profile could be read."""

    if not profiles or not profiles.get("profiles"):
        return None
    return list(profiles["mods"].get(_fold(mod_name), []))
