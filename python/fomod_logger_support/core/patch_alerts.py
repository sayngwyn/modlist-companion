"""New-patch alerts: a mod you just installed is what a skipped patch option was made for.

Example: Grass Patches offers a "Village patch" and you skipped it, because you had no Village.
You install Village now; Grass Patches' option is a patch for it, so it is worth telling you
at once instead of the next time the window is opened.

This module only decides. It works from the same results as the window's missed-patch cards
(``find_missed_patches``), so an alert and its card never disagree about what a patch is.
"""

from __future__ import annotations

from typing import Any, Iterable

from .missed_patches import find_missed_patches

# Only a patch that would apply now (or once the mod it is made for is switched on) is worth an alert.
# A patch that is installed but switched off, or a plugin that can't load, is not a new skipped patch.
ALERT_STATES = ("missing", "needs-disabled")
_ALERT_KINDS = ("detects", "masters", "name")


def _fold(value: str) -> str:
    return value.casefold()


def join_names(names: Iterable[str]) -> str:
    """"A", "A and B", "A, B and C": mod names in one phrase."""

    names = sorted(set(names), key=_fold)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def item_key(item: dict[str, Any]) -> tuple[str, str]:
    return item["event_id"], item["option_id"]


def points_at(item: dict[str, Any], mod_name: str, mod_plugins: Iterable[str]) -> bool:
    """Whether a missed-patch entry is made for this mod: its plugins, or (for a patch found
    only by its name) the mod itself. An empty ``mod_name`` matches no name."""

    plugins = {_fold(name) for name in mod_plugins}
    if any(_fold(name) in plugins for name in item.get("needs", [])):
        return True
    return bool(mod_name) and item.get("kind") == "name" and any(
        _fold(name) == _fold(mod_name) for name in item.get("needs", []))


def new_patches(
    events: Iterable[dict[str, Any]],
    load_order: dict[str, Any],
    *,
    mod_name: str,
    mod_plugins: Iterable[str],
    previous: dict[str, Any] | None = None,
    reviews: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """The missed-patch entries that exist because ``mod_name`` was installed.

    ``load_order`` is the snapshot that includes the mod (switched on or still off). An entry
    counts when it is made for ``mod_plugins`` (the plugins the install added; an update passes
    only the ones that are new, and no ``mod_name``, so a patch found by the mod's name isn't
    announced again), is still worth acting on, and was not already in the results for
    ``previous``, a snapshot from before the install (None when there is none). Only entries
    made for this mod are examined further, so an install nothing was made for costs one pass.
    """

    events = list(events)
    reviews = list(reviews)
    plugins = list(mod_plugins)
    found = [
        item for item in find_missed_patches(events, load_order, reviews)["missed"]
        if item["state"] in ALERT_STATES and item["kind"] in _ALERT_KINDS and points_at(item, mod_name, plugins)
    ]
    if found and previous is not None:
        already = {item_key(item) for item in find_missed_patches(events, previous, reviews)["missed"]}
        found = [item for item in found if item_key(item) not in already]
    return found


def _reviews_for(reviews: Iterable[dict[str, Any]], event_id: str, option_id: str) -> list[dict[str, Any]]:
    """The reminders (open or settled) on this option, including ones set on an earlier install of the same mod."""

    found = []
    for review in reviews:
        if (review["event_id"], review["option_id"]) == (event_id, option_id):
            found.append(review)
            continue
        # The same mapping find_missed_patches uses for a reminder set on an earlier install.
        latest = review.get("latest_event") or {}
        if latest.get("event_id") == event_id:
            option = review.get("latest_option_id", review.get("option_id", ""))
            if (option or not review.get("option_id")) and option == option_id:
                found.append(review)
    return found


def _option_plugins(event: dict[str, Any] | None, option_id: str) -> list[str]:
    if event is None:
        return []
    for step in event.get("steps", []):
        for group in step.get("groups", []):
            for option in group.get("options", []):
                if option.get("id") == option_id:
                    return list(option.get("plugins") or [])
    for item in event.get("automatic_installs", []) or []:
        if item.get("id") == option_id:
            return list(item.get("plugins") or [])
    return []


def live_alerts(
    rows: Iterable[dict[str, Any]],
    missed: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]] = (),
    *,
    gone: Iterable[str] = (),
    events: Iterable[dict[str, Any]] = (),
    installed: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split pending alert rows into (still worth showing, settled for good), each live row with its card added.

    A row that is neither is only hidden for now: the patch isn't a missed patch in this snapshot, but
    nothing says it was dealt with (the load order may be incomplete, or the mod switched off and
    unlisted), so it stays pending. A row is settled for good only for a definite reason, which the
    caller must supply the facts for:

    * the patch is installed (``installed``: plugin names the load order holds) or its card is no longer
      a waiting patch (for example its plugin is there but switched off);
    * a reminder settled or dismissed it, or one was set after the alert was raised;
    * the installer's record is gone (``gone``: ids of records removed or replaced by a later install).
    """

    by_key = {item_key(item): item for item in missed}
    reviews = list(reviews)
    gone = set(gone)
    by_id = {event["event_id"]: event for event in events}
    have = {_fold(name) for name in installed}
    live, settled = [], []
    for row in rows:
        key = (row["event_id"], row["option_id"])
        item = by_key.get(key)
        related = _reviews_for(reviews, *key)
        reminded = any(r.get("state") == "pending" and (r.get("updated_at") or "") >= row["created_at"]
                       for r in related)
        closed = any(r.get("state") in ("resolved", "dismissed") for r in related)
        if item is not None and item["state"] in ALERT_STATES and not reminded and not closed:
            live.append({**row, "item": item})
        elif item is not None or reminded or closed or row["event_id"] in gone or any(
                _fold(plugin) in have for plugin in _option_plugins(by_id.get(row["event_id"]), row["option_id"])):
            settled.append(row)
    return live, settled


def group_by_mod(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live alert rows grouped by the mod that was installed, oldest group first. A patch made for two
    mods installed together is one problem, not two: mods that share a patch are one group.

    Each group: ``{"mod_name": "A" or "A and B", "mods": [names], "mod_keys", "instance_id", "created_at", "rows"}``."""

    rows = list(rows)
    owner: dict[tuple[str, str, str], tuple[str, str]] = {}   # (instance, event, option) -> a mod key that has it
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(node):
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for row in rows:
        node = (row["instance_id"], row["mod_key"])
        find(node)
        key = (row["instance_id"], row["event_id"], row["option_id"])
        if key in owner:
            parent[find(node)] = find(owner[key])
        else:
            owner[key] = node
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        root = find((row["instance_id"], row["mod_key"]))
        group = groups.setdefault(root, {"mod_name": "", "mods": [], "mod_keys": [], "instance_id": row["instance_id"],
                                         "created_at": row["created_at"], "rows": []})
        if row["mod_key"] not in group["mod_keys"]:
            group["mod_keys"].append(row["mod_key"])
            group["mods"].append(row["mod_name"])
        group["created_at"] = min(group["created_at"], row["created_at"])
        group["rows"].append(row)
    for group in groups.values():
        names = sorted(group["mods"], key=_fold)
        group["mods"], group["mod_keys"] = names, sorted(group["mod_keys"])
        group["mod_name"] = join_names(names)
    return sorted(groups.values(), key=lambda group: (group["created_at"], group["mod_keys"]))


def describe_item(item: dict[str, Any], mod_name: str) -> str:
    """"Grass Patches › Compatibility › Village patch: patch for Village; you skipped it when you
    installed Grass Patches"."""

    where = " › ".join(part for part in (item["mod_name"], item.get("step_name"), item["label"]) if part)
    if item.get("automatic"):
        why = f"it wasn't added when you installed {item['mod_name']}"
    elif not item.get("available", True):
        why = f"it wasn't available when you installed {item['mod_name']}"
    else:
        why = f"you skipped it when you installed {item['mod_name']}"
    return f"{where}: patch for {mod_name}; {why}"


def describe_group(group: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """(short description, one line per patch, closing advice) for the MO2 problem list."""

    mod_name = group["mod_name"]
    seen: set[tuple[str, str]] = set()
    items = []
    for row in group["rows"]:
        if item_key(row["item"]) not in seen:   # a patch for two of the mods is listed once
            seen.add(item_key(row["item"]))
            items.append(row["item"])
    lines = [describe_item(item, mod_name) for item in items]
    advice = []
    if any(item["state"] == "needs-disabled" for item in items):
        advice.append(f"{mod_name} {'is' if len(group['mods']) == 1 else 'are'} still disabled in MO2: enable "
                      f"{'it' if len(group['mods']) == 1 else 'them'} first.")
    installers = sorted({item["mod_name"] for item in items}, key=_fold)
    advice.append(f"Reinstall {' and '.join(installers) if len(installers) < 3 else 'those mods'} "
                  "and pick the patch to add it.")
    return f"New patches available for {mod_name}", lines, advice
