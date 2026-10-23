"""Keeping the list's change history in the Logger database (see ``history.py`` for what an entry is).

Everything here takes the open SQLite connection of an ``EventStore`` (which wraps these in its ``history_*``
methods) and never opens its own or commits: a write is part of whatever transaction the caller is in.

Rows are ``history`` entries (one per change, a group of items for a burst) and ``history_snapshots`` (the last
snapshot of each instance and profile, to compare with at the next start). Entries come from three places:

* the plugin's snapshot diff while MO2 runs (``source`` "snapshot");
* the same diff at start, against what was stored when MO2 was last closed (``offline`` is then 1: the change was made
  while Companion wasn't watching);
* the install records: installs, updates, removals and renames (``source`` "record"), made by the store itself when an
  event is ingested or a mod is marked removed, so the history always agrees with the rest of the window.

A fact told by a record replaces the same fact told by a snapshot (and the other way round, a snapshot doesn't repeat
what a record said).
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from typing import Any, Iterable

from . import history

SNAPSHOT, RECORD = "snapshot", "record"


def _names_text(entry: dict[str, Any]) -> str:
    names = [history.name_key(item["name"]) for item in entry.get("items") or []]
    for item in entry.get("items") or []:
        if entry["kind"] == history.MOD_RENAMED and item.get("before"):
            names.append(history.name_key(item["before"]))
    extra = [entry.get("subject", ""), entry.get("detail", {}).get("path", "")]
    return "\n".join([*names, *(history.name_key(text) for text in extra if text)])


def _row(row: sqlite3.Row) -> dict[str, Any]:
    detail = json.loads(row["detail"]) if row["detail"] else {}
    items = detail.pop("items", [])
    return {"id": row["id"], "instance_id": row["instance_id"], "profile": row["profile"], "at": row["at"],
            "kind": row["kind"], "count": row["count"], "subject": row["subject"], "items": items,
            "detail": detail, "source": row["source"], "offline": bool(row["offline"]), "found": int(row["offline"]),
            "event_id": row["event_id"],
            "ended_at": row["ended_at"], "exit_code": row["exit_code"]}


def _detail_json(entry: dict[str, Any], items: list[dict[str, Any]]) -> str:
    return json.dumps({**(entry.get("detail") or {}), "items": items}, ensure_ascii=False, separators=(",", ":"))


# --- writing -----------------------------------------------------------------------------------------------------------

def _equivalent_names(connection: sqlite3.Connection, instance_id: str, kinds: tuple[str, ...], since: str) -> set[str]:
    """The names an entry of one of these kinds has told about since ``since`` (any source, any profile)."""

    marks = ",".join("?" * len(kinds))
    found: set[str] = set()
    for row in connection.execute(
            f"SELECT names FROM history WHERE instance_id = ? AND at >= ? AND kind IN ({marks})",
            (instance_id, since, *kinds)):
        found.update(row[0].split("\n"))
    return found


def _drop_items(connection: sqlite3.Connection, instance_id: str, kinds: tuple[str, ...], keys: set[str],
                low: str, high: str) -> None:
    """Take these mods (by name key) out of the snapshot-made entries of these kinds in this time span; an entry
    left with nothing is deleted."""

    marks = ",".join("?" * len(kinds))
    for row in connection.execute(
            f"SELECT * FROM history WHERE instance_id = ? AND source = ? AND at >= ? AND at <= ? AND kind IN ({marks})",
            (instance_id, SNAPSHOT, low, high, *kinds)).fetchall():
        entry = _row(row)
        if not any(history.name_key(item["name"]) in keys for item in entry["items"]):
            continue
        left = [item for item in entry["items"] if history.name_key(item["name"]) not in keys]
        dropped = len(entry["items"]) - len(left)
        if not left and entry["count"] <= len(entry["items"]):
            connection.execute("DELETE FROM history WHERE id = ?", (entry["id"],))
            continue
        entry["items"] = left
        entry["count"] -= dropped
        connection.execute("UPDATE history SET count = ?, names = ?, detail = ? WHERE id = ?",
                           (entry["count"], _names_text(entry), _detail_json(entry, left), entry["id"]))


def _insert(connection: sqlite3.Connection, instance_id: str, profile: str, entry: dict[str, Any], at: str, *,
            source: str, offline: int, event_id: str | None = None) -> int:
    items = entry.get("items") or []
    stored = items[:history.MAX_ITEMS]
    cursor = connection.execute(
        "INSERT INTO history (instance_id, profile, at, kind, count, subject, names, detail, source, offline, event_id,"
        " ended_at, exit_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (instance_id, profile, at, entry["kind"], int(entry.get("count", len(items))),
         entry.get("subject") or (items[0]["name"] if items else ""), _names_text({**entry, "items": stored}),
         _detail_json(entry, stored), source, int(offline), event_id, entry.get("ended_at") or "",
         entry.get("exit_code")))
    return int(cursor.lastrowid)  # pyright: ignore[reportArgumentType]  # an INSERT always sets it


def add_entries(connection: sqlite3.Connection, instance_id: str, profile: str, entries: list[dict[str, Any]], *,
                at: str | None = None, offline: int = False, dedupe_since: str | None = None,
                keep: int = history.KEEP_ENTRIES) -> int:
    """Store the entries of one snapshot diff (see ``history.diff``). Returns how many rows were added or extended.

    * An installed, updated or removed mod that an install record already told about since ``dedupe_since`` (a few
      minutes ago when it isn't given) is left out.
    * An entry of a kind that the same profile saw a moment ago (``history.BURST_SECONDS``) joins that entry.
    * The oldest entries beyond ``keep`` are deleted."""

    at = at or history.utc_text()
    changed = 0
    for entry in entries:
        items = list(entry.get("items") or [])
        if entry["kind"] in (history.MOD_INSTALLED, history.MOD_UPDATED, history.MOD_REINSTALLED, history.MOD_REMOVED,
                             history.MOD_RENAMED):
            since = dedupe_since or history.shifted(at, -history.SAME_FACT_SECONDS)
            told = _equivalent_names(connection, instance_id,
                                     history.REMOVING if entry["kind"] == history.MOD_REMOVED else history.ADDING,
                                     since)
            items = [item for item in items if history.name_key(item["name"]) not in told]
        if not items:
            continue
        entry = {**entry, "items": items, "count": len(items)}
        earlier = None if offline else connection.execute(
            "SELECT * FROM history WHERE instance_id = ? AND profile = ? AND kind = ? AND source = ? AND offline = 0"
            " ORDER BY at DESC, id DESC LIMIT 1", (instance_id, profile, entry["kind"], SNAPSHOT)).fetchone()
        if (earlier is not None and entry["kind"] != history.PROGRAM_RUN
                and history.seconds(at) - history.seconds(earlier["at"]) <= history.BURST_SECONDS):
            before = _row(earlier)
            merged = history.merge_items(entry["kind"], before["items"], items)
            if merged:
                before["items"], before["count"] = merged, max(len(merged), before["count"])
                connection.execute("UPDATE history SET count = ?, names = ?, detail = ? WHERE id = ?",
                                   (before["count"], _names_text(before), _detail_json(before, merged[:history.MAX_ITEMS]),
                                    before["id"]))
            else:
                connection.execute("DELETE FROM history WHERE id = ?", (before["id"],))
            changed += 1
            continue
        _insert(connection, instance_id, profile, entry, at, source=SNAPSHOT, offline=offline)
        changed += 1
    prune(connection, instance_id, keep)
    return changed


def add_record(connection: sqlite3.Connection, instance_id: str, entry: dict[str, Any], at: str, *,
               event_id: str | None = None, offline: bool = False, keep: int = history.KEEP_ENTRIES) -> bool:
    """Store one entry that an install record told (an install, update, removal or rename of one mod) and take the
    same fact out of any entry a snapshot made for it. False when this record already has its entry."""

    at = history.normalize(at)
    if event_id is not None and connection.execute(
            "SELECT 1 FROM history WHERE event_id = ? AND kind = ?", (event_id, entry["kind"])).fetchone() is not None:
        return False
    if entry["kind"] == history.MOD_RENAMED and _renamed_before(connection, instance_id, entry["items"]):
        return False            # the look at the list saw this rename as it happened: the record's catching up says nothing new
    keys = {history.name_key(item["name"]) for item in entry["items"]}
    if entry["kind"] == history.MOD_RENAMED:
        keys |= {history.name_key(item["before"]) for item in entry["items"] if item.get("before")}
    low, high = history.shifted(at, -history.SAME_FACT_SECONDS), history.shifted(at, history.SAME_FACT_SECONDS)
    if entry["kind"] == history.MOD_REMOVED:
        same_fact = history.REMOVING
    elif entry["kind"] == history.MOD_RENAMED:
        same_fact = (*history.ADDING, *history.REMOVING)
    else:
        same_fact = history.ADDING
    _drop_items(connection, instance_id, same_fact, keys, low, high)
    _insert(connection, instance_id, "", {**entry, "count": len(entry["items"])}, at, source=RECORD,
            offline=offline, event_id=event_id)
    prune(connection, instance_id, keep)
    return True


def _renamed_before(connection: sqlite3.Connection, instance_id: str, items: list[dict[str, Any]]) -> bool:
    """Whether a rename entry already names this rename (the same old and new name), whenever it was told."""

    wanted = {(history.name_key(item["name"]), history.name_key(item.get("before") or "")) for item in items}
    for row in connection.execute("SELECT detail FROM history WHERE instance_id = ? AND kind = ?",
                                  (instance_id, history.MOD_RENAMED)):
        for item in json.loads(row[0] or "{}").get("items", []):
            wanted.discard((history.name_key(item["name"]), history.name_key(item.get("before") or "")))
    return not wanted


def drop_for_events(connection: sqlite3.Connection, event_ids: Iterable[str]) -> None:
    """The entries of install records that were deleted (a plain record replaced by an installer's)."""

    for event_id in event_ids:
        connection.execute("DELETE FROM history WHERE event_id = ?", (event_id,))


def prune(connection: sqlite3.Connection, instance_id: str, keep: int = history.KEEP_ENTRIES) -> int:
    """Keep the newest ``keep`` entries of an instance (counting is an index scan: a couple of milliseconds at the
    cap, and the delete is a few rows)."""

    total = connection.execute("SELECT COUNT(*) FROM history WHERE instance_id = ?", (instance_id,)).fetchone()[0]
    if total <= keep:
        return 0
    cursor = connection.execute(
        "DELETE FROM history WHERE id IN (SELECT id FROM history WHERE instance_id = ? ORDER BY at ASC, id ASC LIMIT ?)",
        (instance_id, total - keep))
    return cursor.rowcount


# --- programs ------------------------------------------------------------------------------------------------------------

def start_run(connection: sqlite3.Connection, instance_id: str, profile: str, name: str, path: str,
              at: str | None = None, keep: int = history.KEEP_ENTRIES) -> int:
    """A program MO2 is starting. Returns the entry's id (for ``finish_run``)."""

    row = _insert(connection, instance_id, profile,
                  {"kind": history.PROGRAM_RUN, "count": 1, "subject": name, "items": [], "detail": {"path": path}},
                  at or history.utc_text(), source=RECORD, offline=False)
    prune(connection, instance_id, keep)
    return row


def finish_run(connection: sqlite3.Connection, entry_id: int, exit_code: int | None, at: str | None = None) -> bool:
    return connection.execute(
        "UPDATE history SET ended_at = ?, exit_code = ? WHERE id = ? AND kind = ?",
        (at or history.utc_text(), exit_code, entry_id, history.PROGRAM_RUN)).rowcount == 1


# --- reading ----------------------------------------------------------------------------------------------------------

def _where(instance_id: str, profile: str | None, kinds: Iterable[str] | None, search: str,
           since: tuple[str, int] | None) -> tuple[str, list[Any]]:
    clauses: list[str] = ["instance_id = ?"]
    parameters: list[Any] = [instance_id]
    if profile is not None:
        clauses.append("(profile = '' OR profile = ?)")
        parameters.append(profile)
    if kinds is not None:
        kinds = tuple(kinds)
        clauses.append(f"kind IN ({','.join('?' * len(kinds))})" if kinds else "0")
        parameters.extend(kinds)
    term = history.name_key(search.strip())
    if term:
        clauses.append("instr(names, ?) > 0")
        parameters.append(term)
    if since is not None:
        clauses.append("(at > ? OR (at = ? AND id > ?))")
        parameters.extend([since[0], since[0], since[1]])
    return " AND ".join(clauses), parameters


def page(connection: sqlite3.Connection, instance_id: str, *, profile: str | None = None,
         kinds: Iterable[str] | None = None, search: str = "", since: tuple[str, int] | None = None,
         limit: int = 250) -> list[dict[str, Any]]:
    """The newest entries first. ``profile`` None is every profile, otherwise that profile and the entries that belong
    to the whole instance; ``since`` is a (time, id) the entries must come after."""

    where, parameters = _where(instance_id, profile, kinds, search, since)
    rows = connection.execute(f"SELECT * FROM history WHERE {where} ORDER BY at DESC, id DESC LIMIT ?",
                              (*parameters, int(limit))).fetchall()
    return [_row(row) for row in rows]


def count(connection: sqlite3.Connection, instance_id: str, *, profile: str | None = None,
          kinds: Iterable[str] | None = None, search: str = "", since: tuple[str, int] | None = None) -> int:
    where, parameters = _where(instance_id, profile, kinds, search, since)
    return int(connection.execute(f"SELECT COUNT(*) FROM history WHERE {where}", parameters).fetchone()[0])


def latest_run(connection: sqlite3.Connection, instance_id: str, *, profile: str | None = None) -> dict[str, Any] | None:
    found = page(connection, instance_id, profile=profile, kinds=(history.PROGRAM_RUN,), limit=1)
    return found[0] if found else None


def entry(connection: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM history WHERE id = ?", (entry_id,)).fetchone()
    return _row(row) if row is not None else None


def profiles(connection: sqlite3.Connection, instance_id: str) -> list[str]:
    """The profiles that have entries or a snapshot in this instance, by name."""

    names = {row[0] for row in connection.execute(
        "SELECT DISTINCT profile FROM history WHERE instance_id = ? AND profile <> ''", (instance_id,))}
    names |= {row[0] for row in connection.execute(
        "SELECT profile FROM history_snapshots WHERE instance_id = ?", (instance_id,))}
    return sorted(names, key=lambda name: (name.casefold(), name))


def instances(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT DISTINCT instance_id FROM history")]


# --- snapshots ------------------------------------------------------------------------------------------------------------

def save_snapshot(connection: sqlite3.Connection, instance_id: str, profile: str, snapshot: dict[str, Any],
                  taken_at: str | None = None) -> None:
    blob = zlib.compress(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 3)
    connection.execute(
        "INSERT INTO history_snapshots (instance_id, profile, taken_at, data) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(instance_id, profile) DO UPDATE SET taken_at = excluded.taken_at, data = excluded.data",
        (instance_id, profile, taken_at or history.utc_text(), blob))


def touch_snapshot(connection: sqlite3.Connection, instance_id: str, profile: str, taken_at: str | None = None) -> None:
    connection.execute("UPDATE history_snapshots SET taken_at = ? WHERE instance_id = ? AND profile = ?",
                       (taken_at or history.utc_text(), instance_id, profile))


def load_snapshot(connection: sqlite3.Connection, instance_id: str, profile: str) -> tuple[dict[str, Any], str] | None:
    """(the snapshot, when it was taken), or None when there is none or it can't be read."""

    row = connection.execute("SELECT taken_at, data FROM history_snapshots WHERE instance_id = ? AND profile = ?",
                             (instance_id, profile)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(zlib.decompress(row["data"]).decode("utf-8"))
    except (zlib.error, ValueError, UnicodeError):
        return None
    return (data, row["taken_at"]) if history.valid_snapshot(data) else None


def last_profile(connection: sqlite3.Connection, instance_id: str) -> str | None:
    """The profile whose snapshot was written last: the one that was in use when MO2 last ran."""

    row = connection.execute(
        "SELECT profile FROM history_snapshots WHERE instance_id = ? ORDER BY taken_at DESC LIMIT 1",
        (instance_id,)).fetchone()
    return row[0] if row else None


# --- what the install records tell ---------------------------------------------------------------------------------------------

def _scan_record(event: dict[str, Any]) -> bool:
    """A record made for a mod that was already in the list when it was first seen: not a change anyone watched."""

    return (event.get("plain") or {}).get("origin") == "scan" or bool(event["source"].get("scanned"))


def install_entry(connection: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any] | None:
    """The entry for an install record: installed, updated (another version or archive) or reinstalled, judged against
    the mod's earlier records. None for a record made by a scan."""

    if _scan_record(event):
        return None
    instance_id, name = event["instance"]["id"], event["mod"]["name"]
    key = history.name_key(name)
    when = history.seconds(event["completed_at"])
    prior = None
    columns = "event_id, mod_name, name_override, archive, completed_at, removed_at, raw_json"
    # Two indexed lookups, not one with OR: SQLite would otherwise read every event of the instance.
    for row in connection.execute(
            f"SELECT {columns} FROM events WHERE instance_id = ? AND mod_name = ? COLLATE NOCASE AND event_id <> ? "
            f"UNION SELECT {columns} FROM events WHERE instance_id = ? AND name_override = ? COLLATE NOCASE AND event_id <> ?",
            (instance_id, name, event["event_id"], instance_id, name, event["event_id"])):
        if (history.name_key(row["name_override"] or row["mod_name"]) == key
                and history.seconds(row["completed_at"]) <= when
                and (prior is None or history.seconds(row["completed_at"]) >= history.seconds(prior["completed_at"]))):
            prior = row
    version, archive = event["mod"]["version"], event["source"]["archive"]
    if prior is None or prior["removed_at"]:
        kind, before, after = history.MOD_INSTALLED, "", version
    else:
        earlier_version = json.loads(prior["raw_json"])["mod"]["version"]
        same_archive = history.same_archive(prior["archive"], archive)
        if earlier_version != version:
            kind, before, after = history.MOD_UPDATED, earlier_version or "(no version)", version or "(no version)"
        elif not same_archive:
            kind, before, after = history.MOD_UPDATED, prior["archive"] or "(no archive)", archive or "(no archive)"
        else:
            kind, before, after = history.MOD_REINSTALLED, version, version
    item = {"name": name}
    if kind == history.MOD_UPDATED:
        item.update(before=before, after=after)
    return {"kind": kind, "count": 1, "items": [item],
            "detail": {"archive": archive, "plain": event.get("kind") == "plain", "version": version}}
