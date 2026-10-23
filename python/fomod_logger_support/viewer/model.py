"""What the Modlist Companion window shows, worked out without any Qt code.

The window asks for a ``Snapshot`` whenever something changes and draws it.
Everything here is plain data so it can be tested without MO2 or a display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..core.file_origin import OriginCache, looks_like_path
from ..core.installer_changes import installer_changes
from ..core.missed_patches import find_missed_patches
from ..core.patch_alerts import live_alerts
from ..core.storage import EventStore, _search_key

CHOICES = ("all", "picked", "skipped")
SORTS = ("newest", "oldest", "name")
VIEWS = ("current", "history")


@dataclass
class ViewState:
    """The window's filters; the model never changes them."""

    query: str = ""
    choices: str = "all"
    sort: str = "newest"
    view: str = "current"
    include_hidden: bool = False
    instance: str = ""


@dataclass
class Snapshot:
    events: list[dict[str, Any]] = field(default_factory=list)
    instances: list[dict[str, str]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    missed: list[dict[str, Any]] = field(default_factory=list)
    missed_available: bool = False
    without_evidence: int = 0
    cross_patches: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)   # installer changes, newest first
    # Every newest install that differs from the one before it: those with installer changes (the same
    # dicts as ``changes``) and those with only different picks. The tree marks these; only
    # ``changes`` get a card.
    compared: list[dict[str, Any]] = field(default_factory=list)
    changes_seen: set[str] = field(default_factory=set)           # installs whose card was settled
    problems: list[dict[str, str]] = field(default_factory=list)
    # Pending new-patch alerts that still apply: the skipped patch option (event_id, option_id), the mod just
    # installed (mod_name), when it was raised, and the missed-patch entry (item) that its card is built from.
    alerts: list[dict[str, Any]] = field(default_factory=list)
    # When the search looks like a path or a file name: the option (or automatic install) of each install that
    # installs it, (event_id, option_id) -> the match from ``core.file_origin`` (the best one for that option).
    file_hits: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    error: str = ""


def load(database: Path, state: ViewState,
         load_order: Callable[[], dict[str, Any] | None] | None = None,
         origin: tuple[OriginCache, Any] | None = None) -> Snapshot:
    """Read everything the window needs in one pass. ``origin`` is the window's file index and the stamp that says
    the database is as it was when the index was built, so a search for a path doesn't rebuild it."""

    snapshot = Snapshot()
    try:
        with EventStore(database) as store:
            if origin is not None:
                store.use_origin_cache(*origin)
            events = (store.latest(state.query, include_hidden=state.include_hidden)
                      if state.view == "current"
                      else store.history(state.query, include_hidden=state.include_hidden))
            # The same read serves the missed-patch check when nothing narrows it.
            everything = events if state.view == "current" and not state.query and not state.include_hidden else None
            if state.instance:
                events = [event for event in events if event["instance"]["id"] == state.instance]
            snapshot.events = sort_events(events, state.sort)
            if looks_like_path(state.query):
                snapshot.file_hits = file_hits(store.file_origins(state.query)["matches"], {e["event_id"] for e in events})
            snapshot.instances = store.instances()
            snapshot.reviews = store.reviews()
            snapshot.problems = store.import_errors()
            if state.view == "current":
                # Cross-patches compare the whole list, so they ignore the search box.
                snapshot.cross_patches = [
                    patch for patch in store.cross_patches("", include_hidden=state.include_hidden)
                    if not state.instance or patch["instance_id"] == state.instance
                ]
            order = load_order() if load_order is not None else None
            # What each mod's newest install changed against the one before it (a mod's two newest installer
            # records are all that takes; the rest of the history isn't read): hidden ones included, then the
            # hidden mods are left out unless they're asked for.
            snapshot.compared = [
                change for change in installer_changes(store.newest_installs(), order,
                                                       instance_id=state.instance, picks=True)
                if state.include_hidden or not change["hidden"]
            ]
            snapshot.changes = [change for change in snapshot.compared if change["changed"]]
            snapshot.changes_seen = store.changes_seen()
            if order is not None:
                result = find_missed_patches(everything if everything is not None else store.latest("", include_hidden=False),
                                             order, snapshot.reviews)
                snapshot.missed_available = True
                snapshot.missed = result["missed"]
                snapshot.without_evidence = result["events_without_evidence"]
                rows = store.patch_alerts(order["instance_id"]) if order.get("instance_id") else []
                snapshot.alerts = live_alerts(rows, snapshot.missed, snapshot.reviews)[0] if rows else []
    except Exception as error:  # shown in the window; the plugin reports it to MO2
        snapshot.error = f"{type(error).__name__}: {error}"
    return snapshot


def sort_events(events: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    if order == "name":
        return sorted(events, key=lambda event: (_search_key(event["mod"]["name"]), event["completed_at"]))
    return sorted(events, key=lambda event: _timestamp(event["completed_at"]), reverse=order != "oldest")


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def is_plain(event: dict[str, Any]) -> bool:
    """Whether a mod was not installed through a logged installer (it has no options, only files and plugins)."""

    return event.get("kind") == "plain"


def accepts(option: dict[str, Any], choices: str) -> bool:
    if choices == "picked":
        return option.get("selected") is True
    if choices == "skipped":
        return option.get("selected") is not True
    return True


def file_hits(matches: list[dict[str, Any]], event_ids: set[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """The matches of a file lookup in the installs on show, one per option (the first, which is the best)."""

    hits: dict[tuple[str, str], dict[str, Any]] = {}
    for match in matches:
        if match["event_id"] in event_ids:
            hits.setdefault((match["event_id"], match["option_id"]), match)
    return hits


def option_matches(event: dict[str, Any], step: dict[str, Any], group: dict[str, Any],
                   option: dict[str, Any], query: str,
                   files: dict[tuple[str, str], dict[str, Any]] | None = None) -> bool:
    term = _search_key(query.strip())
    if not term:
        return False
    if files and (event["event_id"], option["id"]) in files:
        return True
    return any(term in _search_key(label) for label in (
        event["mod"]["name"], step.get("name"), group.get("name"), option.get("name")))


def match_count(event: dict[str, Any], query: str,
                files: dict[tuple[str, str], dict[str, Any]] | None = None) -> int:
    if not query.strip():
        return 0
    options = [(step, group, option) for step in event["steps"] for group in step["groups"]
               for option in group["options"]]
    count = sum(option_matches(event, step, group, option, query, files) for step, group, option in options)
    if files:
        # Automatic installs are not options, but a file they install is a match too.
        ids = {option["id"] for _step, _group, option in options}
        count += sum(1 for event_id, option_id in files if event_id == event["event_id"] and option_id not in ids)
    return count


def latest_option_id(review: dict[str, Any]) -> str:
    """The reminder's choice in the newest record of its mod."""

    if not review.get("option_id"):
        return ""
    return review.get("latest_option_id", review["option_id"]) or ""


def pending_review(reviews: list[dict[str, Any]], event_id: str, option_id: str) -> dict[str, Any] | None:
    """The open reminder on this choice, including one set on an earlier install."""

    for review in reviews:
        if review.get("state") != "pending":
            continue
        if review["event_id"] == event_id and review["option_id"] == option_id:
            return review
        latest = review.get("latest_event") or {}
        if latest.get("event_id") == event_id and latest_option_id(review) == option_id:
            return review
    return None


def option_label(event: dict[str, Any], option_id: str) -> str:
    for step in event.get("steps", []):
        for group in step.get("groups", []):
            for option in group.get("options", []):
                if option["id"] == option_id:
                    return f"{option.get('name') or 'Unnamed option'} ({group.get('name') or step.get('name')})"
    return ""


def classify(patch: dict[str, Any]) -> str:
    picked = sum(1 for mod in patch["mods"] if mod.get("selected"))
    if picked == 0:
        return "none"
    return "all" if picked == len(patch["mods"]) else "mixed"


def changes_for(snapshot: Snapshot, event_id: str) -> dict[str, Any] | None:
    """The installer changes of this install against the one before it, if it is a mod's newest."""

    return next((change for change in snapshot.changes if change["event_id"] == event_id), None)


def changes_line(change: dict[str, Any]) -> str:
    """"1.2 → 1.3: 3 new options, 1 removed, 1 renamed" (the versions only when both are known)."""

    parts = []
    if change["added"]:
        parts.append(plural(len(change["added"]), "new option"))
    if change["removed"]:
        parts.append(f"{len(change['removed'])} removed")
    if change["renamed"]:
        parts.append(f"{len(change['renamed'])} renamed")
    if change["steps_added"]:
        parts.append(plural(len(change["steps_added"]), "new page"))
    old, new = change.get("old_version"), change.get("new_version")
    lead = f"{old} → {new}: " if old and new and old != new else "Reinstalled: " if old and old == new else ""
    return lead + ", ".join(parts)


def picks_text(change: dict[str, Any]) -> str:
    """"+1 picked, −1 unpicked": how this install's picks differ from the previous one's ("" for none)."""

    parts = []
    if change["picked"]:
        parts.append(f"+{len(change['picked'])} picked")
    if change["unpicked"]:
        parts.append(f"−{len(change['unpicked'])} unpicked")
    return ", ".join(parts)


def patch_line(item: dict[str, Any]) -> tuple[str, str]:
    """(sentence, colour) for one new option that is a patch for something in the list."""

    patch = item["patch"]
    needs = ", ".join(patch["needs"])
    if patch["state"] == "needs-disabled":
        return f"New: {item['name']} — made for {needs}, which you have switched off", "off"
    if patch["picked"]:
        return f"New: {item['name']} — you have {needs}; you picked it", "picked"
    if patch["have"]:
        return f"New: {item['name']} — you have {needs}; its plugin is already in your list", ""
    how = "you skipped it" if item.get("available", True) else "it wasn't available when you installed"
    return f"New: {item['name']} — you have {needs}; {how}", "missed"


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"


def summary(snapshot: Snapshot, state: ViewState) -> tuple[str, list[str], str]:
    """(count, extra parts, alert) for the line under the title."""

    count = len(snapshot.events)
    if state.view == "history":
        text = plural(count, "install") + " in history"
    else:
        text = plural(count, "mod") + (" match" if state.query.strip() else " logged")
    pending = sum(1 for review in snapshot.reviews if review.get("state") == "pending")
    extra = []
    plain = sum(1 for event in snapshot.events if is_plain(event))
    if plain:
        extra.append(f"{plain:,} without installer options")
    if pending:
        extra.append(f"{pending} to revisit")
    if state.include_hidden:
        extra.append("including hidden")
    # Switched-off patches are listed separately and don't raise the alert.
    missing = sum(1 for item in snapshot.missed if item.get("state", "missing") == "missing")
    alert = plural(missing, "possible missed patch", "possible missed patches") if missing else ""
    return text, extra, alert


def format_date(value: str | None) -> str:
    if not value:
        return "unknown date"
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return value
    return moment.strftime("%b %d, %Y, %I:%M %p").replace(" 0", " ")
