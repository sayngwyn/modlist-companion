"""User review notes, kept separate from immutable installer evidence."""

from datetime import datetime, timezone
from uuid import uuid4


def save_review(store, body):
    if set(body) != {"event_id", "option_id", "reason", "state"}:
        raise ValueError("review must contain event_id, option_id, reason and state")
    if any(not isinstance(body[k], str) for k in body):
        raise ValueError("review values must be text")
    if body["state"] not in {"pending", "resolved", "dismissed"}:
        raise ValueError("invalid review state")
    if len(body["reason"]) > 2000:  # a note is optional
        raise ValueError("reason must be at most 2000 characters")
    original = store.event(body["event_id"])
    if original is None:
        raise ValueError("original installation not found")
    if body["option_id"] and body["option_id"] not in {
        option["id"] for step in original["steps"]
        for group in step["groups"] for option in group["options"]
    }:
        raise ValueError("original choice not found")
    connection = store._connection
    timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connection:
        connection.execute("""
            INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, option_id) DO UPDATE SET
              reason = excluded.reason, state = excluded.state,
              updated_at = excluded.updated_at
        """, (str(uuid4()), body["event_id"], body["option_id"], body["reason"],
              body["state"], timestamp, timestamp))
    return next(r for r in list_reviews(store)
                if r["event_id"] == body["event_id"] and r["option_id"] == body["option_id"])


def delete_review(store, body):
    """Remove one reminder entirely; True when it existed."""

    if set(body) != {"event_id", "option_id"} or any(not isinstance(v, str) for v in body.values()):
        raise ValueError("review removal must contain event_id and option_id")
    with store._connection:
        cursor = store._connection.execute(
            "DELETE FROM reviews WHERE event_id = ? AND option_id = ?",
            (body["event_id"], body["option_id"]))
    return cursor.rowcount > 0


def option_walk(event):
    """Every option of an event as (step, group, option, path).

    ``path`` is (step name, group name, option name, occurrence): the key that identifies the same
    choice in another install of the mod. Only names and order are used, so a tree without ids
    (just steps > groups > options with names) walks the same way.
    """

    seen = {}
    for step in event["steps"]:
        for group in step["groups"]:
            for option in group["options"]:
                name = (step["name"], group["name"], option["name"])
                seen[name] = seen.get(name, -1) + 1
                yield step, group, option, (*name, seen[name])


def _option_paths(event):
    """Option id -> (step, group, option name, occurrence), and the reverse.

    Positional ids are only stable inside one event: older builds numbered
    options in display order and newer ones in authoring order, and a mod
    update can reorder them. Names identify the same choice across events;
    the occurrence count keeps repeated labels apart.
    """

    by_id, by_path = {}, {}
    for _step, _group, option, path in option_walk(event):
        by_id[option["id"]] = path
        by_path[path] = option["id"]
    return by_id, by_path


def _choices(event):
    by_id, _ = _option_paths(event)
    return sorted(
        (by_id[option["id"]], bool(option["selected"]))
        for step in event["steps"] for group in step["groups"] for option in group["options"]
    )


def list_reviews(store):
    result = []
    newest: dict[tuple[str, str], dict] = {}    # each mod's newest record, looked up once however many reminders it has
    for row in store._connection.execute(
        "SELECT * FROM reviews ORDER BY updated_at DESC, review_id"
    ).fetchall():
        item = dict(row)
        original = store.event(item["event_id"])
        # The same mod identity as the current view, but hidden or removed
        # newer records still count as review evidence.
        mod = (original["instance"]["id"], original["mod"]["name"].lower())
        if mod not in newest:
            newest[mod] = store.newest_for_mod(original["instance"]["id"], original["mod"]["name"])
        newer = newest[mod]
        # The same choice in the newest record ("" for a whole-installer
        # reminder, or when that choice no longer exists there).
        latest_option_id = ""
        if item["option_id"]:
            path = _option_paths(original)[0].get(item["option_id"])
            latest_option_id = _option_paths(newer)[1].get(path, "")
        item.update(original_event=original, latest_event=newer,
                    latest_option_id=latest_option_id,
                    newer_record=newer["event_id"] != original["event_id"],
                    choices_changed=_choices(newer) != _choices(original))
        result.append(item)
    return result
