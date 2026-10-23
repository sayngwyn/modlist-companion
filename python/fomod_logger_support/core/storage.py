"""SQLite index of the immutable FOMOD installation events.

Each event is stored whole as JSON. The few columns beside it are the ones the
current view ranks and filters on, plus the user's own state (hidden, removed,
renamed), which never rewrites the recorded evidence.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import history, history_store, plain_record, reviews
from .cross_patches import analyze_cross_patches
from .file_origin import FileIndex, OriginCache, looks_like_path, plain_matches, plain_path


class EventValidationError(ValueError):
    """Raised when a native or migrated event violates storage invariants."""


class DatabaseVersionError(RuntimeError):
    """Raised when a database was written by a Logger this one cannot read."""


@dataclass(frozen=True)
class IngestionSummary:
    """Outcome of one event folder scan."""

    scanned: int
    imported: int
    duplicates: int
    errors: int


# Database versions 1-4 were used by early test builds; this schema starts at 5. Each
# migration is applied once, in order, and moves user_version up by one.
_FIRST_VERSION = 5
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE events (
             event_id TEXT PRIMARY KEY,
             instance_id TEXT NOT NULL,
             mod_name TEXT NOT NULL,
             name_override TEXT,
             archive TEXT NOT NULL,
             started_at TEXT NOT NULL,
             completed_at TEXT NOT NULL,
             hidden INTEGER NOT NULL DEFAULT 0,
             removed_at TEXT,
             raw_json TEXT NOT NULL)""",
        """CREATE TABLE reviews (
             review_id TEXT PRIMARY KEY,
             event_id TEXT NOT NULL REFERENCES events(event_id),
             option_id TEXT NOT NULL,
             reason TEXT NOT NULL,
             state TEXT NOT NULL CHECK (state IN ('pending', 'resolved', 'dismissed')),
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL,
             UNIQUE (event_id, option_id))""",
        """CREATE TABLE import_errors (
             path TEXT PRIMARY KEY,
             directory TEXT NOT NULL,
             error TEXT NOT NULL,
             observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    ),
    (
        # "Seen" on a changed-installer card, kept per install: a newer install that changes
        # things again has a new event id, so it gets a new card.
        """CREATE TABLE changes_seen (
             event_id TEXT PRIMARY KEY REFERENCES events(event_id),
             seen_at TEXT NOT NULL)""",
    ),
    (
        # New-patch alerts: a skipped patch option that a mod installed later is made for. A row
        # stays after the alert is dealt with ("cleared"), so the same patch isn't announced twice
        # for one install of that mod; removing the mod forgets its rows.
        """CREATE TABLE patch_alerts (
             instance_id TEXT NOT NULL,
             mod_key TEXT NOT NULL,
             mod_name TEXT NOT NULL,
             event_id TEXT NOT NULL,
             option_id TEXT NOT NULL,
             install_event_id TEXT NOT NULL DEFAULT '',
             state TEXT NOT NULL CHECK (state IN ('pending', 'cleared')),
             created_at TEXT NOT NULL,
             cleared_at TEXT,
             PRIMARY KEY (instance_id, mod_key, event_id, option_id))""",
    ),
    (
        # Mods that were not installed through a logged installer ("plain" records): the same events, told
        # apart by ``kind``, with their file lists in a table of their own so reading the events stays cheap.
        "ALTER TABLE events ADD COLUMN kind TEXT NOT NULL DEFAULT 'fomod'",
        "CREATE INDEX events_kind ON events (kind, instance_id)",
        """CREATE TABLE plain_files (
             event_id TEXT NOT NULL,
             path TEXT NOT NULL,
             key TEXT NOT NULL,
             name TEXT NOT NULL)""",
        "CREATE INDEX plain_files_event ON plain_files (event_id)",
        "CREATE INDEX plain_files_key ON plain_files (key)",
        "CREATE INDEX plain_files_name ON plain_files (name)",
    ),
    (
        # The file lists again, about a third of the size: one row per file, (the event's rowid, the path), in the
        # index itself (no row of its own, no second copy of the path, no text event id). The path compares without
        # regard to ASCII case, so a lookup asks one event at a time with a range on its own rows, and no mod can use
        # up the room of another. Version 8 only ever existed in test builds, but a database that has it is moved over.
        "DROP INDEX plain_files_event",
        "DROP INDEX plain_files_key",
        "DROP INDEX plain_files_name",
        "ALTER TABLE plain_files RENAME TO plain_files_v8",
        """CREATE TABLE plain_files (
             event_rowid INTEGER NOT NULL,
             path TEXT NOT NULL COLLATE NOCASE,
             PRIMARY KEY (event_rowid, path)) WITHOUT ROWID""",
        "INSERT OR IGNORE INTO plain_files (event_rowid, path) SELECT e.rowid, f.path "
        "FROM plain_files_v8 AS f JOIN events AS e ON e.event_id = f.event_id",
        "DROP TABLE plain_files_v8",
    ),
    (
        # The list's change history (see core/history.py): one row per change (a burst is one row with its items in
        # ``detail``), and the last snapshot of each instance and profile to compare with at the next start. Purely
        # additive: no existing table changes (a build supporting an earlier schema refuses a version 10 database, so keep a backup before
        # upgrading, as for every version). The two ``events``
        # indexes let an install be compared with the mod's earlier records without reading every event.
        """CREATE TABLE history (
             id INTEGER PRIMARY KEY,
             instance_id TEXT NOT NULL,
             profile TEXT NOT NULL DEFAULT '',
             at TEXT NOT NULL,
             kind TEXT NOT NULL,
             count INTEGER NOT NULL DEFAULT 1,
             subject TEXT NOT NULL DEFAULT '',
             names TEXT NOT NULL DEFAULT '',
             detail TEXT NOT NULL DEFAULT '',
             source TEXT NOT NULL CHECK (source IN ('snapshot', 'record')),
             offline INTEGER NOT NULL DEFAULT 0,
             event_id TEXT,
             ended_at TEXT NOT NULL DEFAULT '',
             exit_code INTEGER)""",
        "CREATE INDEX history_when ON history (instance_id, at, id)",
        "CREATE INDEX history_kind ON history (instance_id, profile, kind, at)",
        "CREATE UNIQUE INDEX history_event ON history (event_id, kind) WHERE event_id IS NOT NULL",
        """CREATE TABLE history_snapshots (
             instance_id TEXT NOT NULL,
             profile TEXT NOT NULL,
             taken_at TEXT NOT NULL,
             data BLOB NOT NULL,
             PRIMARY KEY (instance_id, profile)) WITHOUT ROWID""",
        "CREATE INDEX events_mod_name ON events (instance_id, mod_name COLLATE NOCASE)",
        "CREATE INDEX events_override_name ON events (instance_id, name_override COLLATE NOCASE)",
    ),
)
CURRENT_DATABASE_VERSION = _FIRST_VERSION + len(_MIGRATIONS) - 1

# At an exact tie a plain record loses to an installer's record of the same time (it is the one that was guessed), and a
# record that is still there wins over one that was removed (a scan of a mod that came back, whose folder keeps its date).
_NEWEST_FIRST = ("ORDER BY julianday(completed_at) DESC, completed_at DESC, (kind = 'plain') ASC, "
                 "(removed_at IS NULL) DESC, event_id DESC")
# A plain record and an installer's record of one install (same mod, same archive) are made within seconds of each
# other, whichever side notices first; this is how far apart they may be and still be that one install.
_SAME_INSTALL_SECONDS = 120
# How many of a mod's newest plain records keep their file lists (see ``_prune_files``).
_KEPT_FILE_LISTS = 2
# A lookup for a file name lists at most this many files of one mod (a bare name can be in every folder).
_PLAIN_FILES_PER_MOD = 3

_EVENT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_INSTANCE_ID = re.compile(r"[0-9a-f]{64}")
_UPSTREAM_COMMIT = re.compile(r"[0-9a-f]{40}|unknown")
_DATE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_GROUP_TYPES = {"SelectAtLeastOne", "SelectAtMostOne", "SelectExactlyOne", "SelectAny", "SelectAll"}
_EVIDENCE_KEYS = ("plugins", "masters", "detects")
_MAX_EVIDENCE_NAMES = 4096
_MAX_FILE_ENTRIES = 2000   # per option; the writer also stops at 20,000 per event
_FILE_ENTRY_KEYS = {"source", "destination", "resolved", "kind", "priority", "order", "escapes"}


def _search_key(value: Any) -> str:
    """The Unicode rule every search uses: NFKC, then casefold."""

    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _name_key(name: str) -> str:
    # MO2 compares mod names case-insensitively, one character at a time.
    return name.lower()


def _archive_key(archive: str) -> str:
    """An archive's file name for comparing: no folder, no case."""

    return str(archive or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _seconds(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# --- validation -------------------------------------------------------------
# Required fields must be present with the right types. Unknown extra fields
# from newer native builds are kept as evidence, not rejected.

def _fail(path: str, message: str) -> EventValidationError:
    return EventValidationError(f"{path} {message}")


def _object(value: Any, path: str, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(path, "must be an object")
    missing = [key for key in keys if key not in value]
    if missing:
        raise _fail(path, f"is missing properties: {', '.join(sorted(missing))}")
    return value


def _text(value: Any, path: str, *, required: bool = False,
          pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or (required and not value):
        raise _fail(path, "must be a string")
    if pattern is not None and not pattern.fullmatch(value):
        raise _fail(path, "has an invalid format")
    return value


def _flag(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(path, "must be a boolean")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(path, "must be an array")
    return value


def _timestamp(value: Any, path: str) -> str:
    text = _text(value, path, required=True, pattern=_DATE_TIME)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail(path, "has an invalid date-time") from exc
    return text


def _evidence(item: dict[str, Any], path: str) -> None:
    for key in _EVIDENCE_KEYS:
        if key in item:
            names = item[key]
            if not isinstance(names, list) or len(names) > _MAX_EVIDENCE_NAMES:
                raise _fail(f"{path}.{key}", "must be an array of plugin names")
            for index, name in enumerate(names):
                _text(name, f"{path}.{key}[{index}]", required=True)


def _files(item: dict[str, Any], path: str) -> None:
    """The optional ``files`` list (the FOMOD's <file>/<folder> entries for an option) and its truncated flag."""

    if "files" in item:
        entries = item["files"]
        if not isinstance(entries, list) or len(entries) > _MAX_FILE_ENTRIES:
            raise _fail(f"{path}.files", "must be an array of file entries")
        for index, raw in enumerate(entries):
            entry_path = f"{path}.files[{index}]"
            entry = _object(raw, entry_path, ("source", "destination", "resolved", "kind"))
            extra = sorted(set(entry) - _FILE_ENTRY_KEYS)
            if extra:
                raise _fail(entry_path, f"has unknown properties: {', '.join(extra)}")
            for key in ("source", "destination", "resolved"):
                _text(entry[key], f"{entry_path}.{key}")
            if entry["kind"] not in ("file", "folder"):
                raise _fail(f"{entry_path}.kind", "must be file or folder")
            for key in ("priority", "order"):
                if key in entry and (isinstance(entry[key], bool) or not isinstance(entry[key], int)):
                    raise _fail(f"{entry_path}.{key}", "must be an integer")
            if entry.get("order", 0) < 0:
                raise _fail(f"{entry_path}.order", "must not be negative")
            if "escapes" in entry:
                _flag(entry["escapes"], f"{entry_path}.escapes")
    if "files_truncated" in item:
        _flag(item["files_truncated"], f"{path}.files_truncated")


def validate_event(event: Any) -> dict[str, Any]:
    """Check the invariants ingestion, search and the viewer rely on."""

    root = _object(event, "event", ("schema_version", "event_id", "started_at", "completed_at",
                                    "instance", "source", "mod", "installer", "steps"))
    if root["schema_version"] != 1:
        raise EventValidationError("schema_version must be 1")
    _text(root["event_id"], "event_id", required=True, pattern=_EVENT_ID)
    _timestamp(root["started_at"], "started_at")
    _timestamp(root["completed_at"], "completed_at")

    instance = _object(root["instance"], "instance", ("id", "label"))
    _text(instance["id"], "instance.id", required=True, pattern=_INSTANCE_ID)
    _text(instance["label"], "instance.label")
    source = _object(root["source"], "source", ("archive", "reinstallation"))
    _text(source["archive"], "source.archive")
    _flag(source["reinstallation"], "source.reinstallation")
    mod = _object(root["mod"], "mod", ("name", "version", "nexus_id"))
    _text(mod["name"], "mod.name", required=True)
    _text(mod["version"], "mod.version")
    nexus_id = mod["nexus_id"]
    if isinstance(nexus_id, bool) or not isinstance(nexus_id, int):
        raise EventValidationError("mod.nexus_id must be an integer")
    if not -(2**63) <= nexus_id < 2**63:
        raise EventValidationError("mod.nexus_id must fit in a signed 64-bit SQLite integer")
    installer = _object(root["installer"], "installer", ("name", "version", "upstream_commit"))
    _text(installer["name"], "installer.name")
    _text(installer["version"], "installer.version")
    _text(installer["upstream_commit"], "installer.upstream_commit", required=True,
          pattern=_UPSTREAM_COMMIT)

    for index, raw in enumerate(_array(root.get("automatic_installs", []), "automatic_installs")):
        path = f"automatic_installs[{index}]"
        item = _object(raw, path, ("id", "installed"))
        _text(item["id"], f"{path}.id", required=True)
        _flag(item["installed"], f"{path}.installed")
        _evidence(item, path)
        _files(item, path)

    if "required_files" in root:
        required = _object(root["required_files"], "required_files", ("files",))
        extra = sorted(set(required) - {"files", "files_truncated"})
        if extra:
            raise _fail("required_files", f"has unknown properties: {', '.join(extra)}")
        _files(required, "required_files")

    identities: set[str] = set()

    def identity(value: Any, path: str) -> None:
        text = _text(value, path, required=True)
        if text in identities:
            raise EventValidationError(f"duplicate identity: {text}")
        identities.add(text)

    for s, raw_step in enumerate(_array(root["steps"], "steps")):
        step_path = f"steps[{s}]"
        step = _object(raw_step, step_path, ("id", "name", "visible", "groups"))
        identity(step["id"], f"{step_path}.id")
        _text(step["name"], f"{step_path}.name")
        visible = _flag(step["visible"], f"{step_path}.visible")
        for g, raw_group in enumerate(_array(step["groups"], f"{step_path}.groups")):
            group_path = f"{step_path}.groups[{g}]"
            group = _object(raw_group, group_path, ("id", "name", "type", "options"))
            identity(group["id"], f"{group_path}.id")
            _text(group["name"], f"{group_path}.name")
            if _text(group["type"], f"{group_path}.type") not in _GROUP_TYPES:
                raise _fail(f"{group_path}.type", "is invalid")
            for o, raw_option in enumerate(_array(group["options"], f"{group_path}.options")):
                path = f"{group_path}.options[{o}]"
                option = _object(raw_option, path, ("id", "name", "selected", "enabled"))
                identity(option["id"], f"{path}.id")
                _text(option["name"], f"{path}.name")
                selected = _flag(option["selected"], f"{path}.selected")
                _flag(option["enabled"], f"{path}.enabled")
                if selected and not visible:
                    raise _fail(path, "cannot be selected in an invisible step")
                _evidence(option, path)
                _files(option, path)
    _plain(root)
    return root


def _plain(root: dict[str, Any]) -> None:
    """The optional ``kind`` and, for a plain record, its ``plain`` part (see ``core/plain_record``)."""

    kind = root.get("kind", "fomod")
    if kind not in ("fomod", plain_record.KIND):
        raise _fail("kind", "must be fomod or plain")
    if kind == "fomod":
        if "plain" in root:
            raise _fail("plain", "belongs to a plain record")
        return
    if root["steps"]:
        raise _fail("steps", "must be empty in a plain record")
    plain = _object(root.get("plain"), "plain", ("origin", "file_count", "files_truncated"))
    if plain["origin"] not in plain_record.ORIGINS:
        raise _fail("plain.origin", "must be install or scan")
    count = plain["file_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise _fail("plain.file_count", "must be a non-negative integer")
    _flag(plain["files_truncated"], "plain.files_truncated")
    for key in ("game", "files_digest"):
        if key in plain:
            _text(plain[key], f"plain.{key}")
    plugins = _array(plain.get("plugins", []), "plain.plugins")
    if len(plugins) > plain_record.MAX_PLUGINS:
        raise _fail("plain.plugins", "has too many plugins")
    for index, raw in enumerate(plugins):
        entry = _object(raw, f"plain.plugins[{index}]", ("name",))
        _text(entry["name"], f"plain.plugins[{index}].name", required=True)
        if "masters" in entry:
            masters = _array(entry["masters"], f"plain.plugins[{index}].masters")
            if len(masters) > _MAX_EVIDENCE_NAMES:
                raise _fail(f"plain.plugins[{index}].masters", "has too many masters")
            for number, name in enumerate(masters):
                _text(name, f"plain.plugins[{index}].masters[{number}]", required=True)
    if "files" in plain:
        files = _array(plain["files"], "plain.files")
        if len(files) > plain_record.MAX_FILES:
            raise _fail("plain.files", "has too many files")
        used = 0
        for index, name in enumerate(files):
            _text(name, f"plain.files[{index}]", required=True)
            used += len(name.encode("utf-8", "replace")) + 1
        if used > plain_record.BYTE_BUDGET:
            raise _fail("plain.files", "is too large")
        if plain["file_count"] != len(files):
            raise _fail("plain.file_count", "does not match the files")


def _matches(event: dict[str, Any], term: str) -> bool:
    if term in _search_key(event["mod"]["name"]) or term in _search_key(
            event["mod"].get("recorded_name")):
        return True
    if event.get("kind") == plain_record.KIND:
        # No installer pages or options: the archive and the plugins are what else names the mod.
        return (term in _search_key(event["source"].get("archive"))
                or any(term in _search_key(plugin["name"]) for plugin in event["plain"].get("plugins", [])))
    return any(
        term in _search_key(label)
        for step in event["steps"] for group in step["groups"] for option in group["options"]
        for label in (step["name"], group["name"], option["name"])
    )


class EventStore:
    """One connection to the Logger database. Event files stay the evidence."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path)
        # True when this connection made a new, empty database.
        self.created = False
        # Every event, parsed once per connection: one window refresh reads
        # the current list, cross-patches and missed patches from it.
        self._all_events: list[dict[str, Any]] | None = None
        # The index columns of every event, newest first, and the events parsed so far by id: a refresh needs each
        # mod's newest events (and the one before), not all of the history, so only those are parsed.
        self._index_rows: list[sqlite3.Row] | None = None
        self._parsed: dict[str, dict[str, Any]] = {}
        # The index columns of every event of one instance, newest first, for finding a mod's events by name.
        self._mod_row_cache: dict[str, list[sqlite3.Row]] = {}
        # The files of every event, indexed once and only when a path is looked up, with the event list it was
        # built from and the answers already worked out.
        self._file_index: tuple[list[dict[str, Any]], OriginCache] | None = None
        # A cache the caller keeps between connections (the window's), and the stamp that says it is still good.
        self._origin_cache: tuple[OriginCache, Any] | None = None
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._migrate()
            self._connection.execute("PRAGMA journal_mode = WAL")
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        if self._version() == CURRENT_DATABASE_VERSION:
            return
        # Decide under the write lock: another connection may be creating
        # this same new file right now.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            version = self._version()
            if version == CURRENT_DATABASE_VERSION:
                self._connection.rollback()
                return
            if version > CURRENT_DATABASE_VERSION:
                raise DatabaseVersionError(
                    f"{self.database_path} was written by a newer Modlist Companion "
                    f"(database version {version}); update Modlist Companion to read it")
            has_tables = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone() is not None
            if version < _FIRST_VERSION and (version or has_tables):
                raise DatabaseVersionError(
                    f"{self.database_path} was written by an early Modlist Companion test build and "
                    "cannot be read. Close Mod Organizer and rename or move that file; "
                    "Modlist Companion rebuilds its index from the event files and imports "
                    "FOMOD Logger 1.x data again.")
            for target in range(max(version + 1, _FIRST_VERSION), CURRENT_DATABASE_VERSION + 1):
                for statement in _MIGRATIONS[target - _FIRST_VERSION]:
                    self._connection.execute(statement)
            self._connection.execute(f"PRAGMA user_version = {CURRENT_DATABASE_VERSION}")
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        self.created = version == 0

    def _version(self) -> int:
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    # --- writing -------------------------------------------------------------

    def _forget(self) -> None:
        """Drop everything read so far (after a write)."""

        self._all_events = None
        self._index_rows = None
        self._parsed.clear()
        self._mod_row_cache.clear()

    def ingest(self, event: Any) -> bool:
        """Store one event; False when the identical event is already stored.

        A plain record's file list goes to its own table; the event keeps the count and a fingerprint. An
        installer's record of a mod makes the plain record of that same install (see ``_drop_shadowed``) redundant."""

        self._forget()
        root = validate_event(event)
        stored, files = root, None
        if root.get("kind") == plain_record.KIND:
            stored = dict(root)
            stored["plain"] = {key: value for key, value in root["plain"].items() if key != "files"}
            files = root["plain"].get("files")
        raw_json = json.dumps(stored, ensure_ascii=False, sort_keys=True)
        with self._connection:
            existing = self._connection.execute(
                "SELECT raw_json FROM events WHERE event_id = ?", (root["event_id"],)).fetchone()
            if existing is not None:
                if existing["raw_json"] == raw_json:
                    return False
                raise EventValidationError(
                    f"event_id {root['event_id']} conflicts with different existing content")
            cursor = self._connection.execute(
                "INSERT INTO events (event_id, instance_id, mod_name, archive, started_at,"
                " completed_at, raw_json, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (root["event_id"], root["instance"]["id"], root["mod"]["name"],
                 root["source"]["archive"], root["started_at"], root["completed_at"], raw_json,
                 root.get("kind", "fomod")))
            if files is not None:
                if files:
                    self._connection.executemany(
                        "INSERT OR IGNORE INTO plain_files (event_rowid, path) VALUES (?, ?)",
                        ((cursor.lastrowid, path) for path in files))
                self._prune_files(root)
            else:
                self._drop_shadowed(root)
            self._note_install(root)
        return True

    def _note_install(self, event: dict[str, Any]) -> None:
        """The list's history tells this install (see ``core/history_store``)."""

        entry = history_store.install_entry(self._connection, event)
        if entry is not None:
            history_store.add_record(self._connection, event["instance"]["id"], entry, event["completed_at"],
                                     event_id=event["event_id"])

    def _prune_files(self, event: dict[str, Any]) -> None:
        """Only a mod's newest ``_KEPT_FILE_LISTS`` plain records keep their file lists. The history needs the record
        (version, archive, plugins, how many files), and the last two lists are what an update is compared with;
        older lists are the bulk of the database (every reinstall adds a whole copy) and nothing asks for them."""

        name = event["mod"]["name"]
        rows = self._connection.execute(
            "SELECT rowid, event_id, completed_at, mod_name, name_override FROM events WHERE instance_id = ? AND kind = 'plain' "
            "AND (mod_name = ? COLLATE NOCASE OR name_override = ? COLLATE NOCASE)",
            (event["instance"]["id"], name, name)).fetchall()
        same = [row for row in rows if _name_key(row["name_override"] or row["mod_name"]) == _name_key(name)]
        if len(same) <= _KEPT_FILE_LISTS:
            return
        same.sort(key=lambda row: (_seconds(row["completed_at"]), row["event_id"]), reverse=True)
        for row in same[_KEPT_FILE_LISTS:]:
            self._connection.execute("DELETE FROM plain_files WHERE event_rowid = ?", (row["rowid"],))

    def _drop_shadowed(self, event: dict[str, Any]) -> None:
        """A plain record made for the same install as this installer's record (same mod, same archive, within
        ``_SAME_INSTALL_SECONDS``: whichever side noticed the install first) is dropped. A plain record of an
        earlier install, or of another archive, stays: it is history."""

        archive = _archive_key(event["source"]["archive"])
        if not archive:
            return
        rows = self._connection.execute(
            "SELECT rowid, event_id, mod_name, name_override, archive, completed_at FROM events "
            "WHERE instance_id = ? AND kind = 'plain'", (event["instance"]["id"],)).fetchall()
        if not rows:
            return
        name = _name_key(event["mod"]["name"])
        when = _seconds(event["completed_at"])
        gone = [(row["rowid"], row["event_id"]) for row in rows
                if _name_key(row["name_override"] or row["mod_name"]) == name
                and _archive_key(row["archive"]) == archive
                and abs(_seconds(row["completed_at"]) - when) <= _SAME_INSTALL_SECONDS]
        history_store.drop_for_events(self._connection, [event_id for _rowid, event_id in gone])
        for rowid, event_id in gone:
            self._connection.execute("DELETE FROM plain_files WHERE event_rowid = ?", (rowid,))
            self._connection.execute("DELETE FROM reviews WHERE event_id = ?", (event_id,))
            self._connection.execute("DELETE FROM changes_seen WHERE event_id = ?", (event_id,))
            self._connection.execute("DELETE FROM events WHERE event_id = ?", (event_id,))

    def ingest_directory(self, directory: Path) -> IngestionSummary:
        """Import new event files. Files are named by their immutable event id,
        so a file whose name is already stored is never read again."""

        directory = Path(directory)
        known = {row[0] for row in self._connection.execute("SELECT event_id FROM events")}
        paths = sorted(directory.glob("*.json"))
        imported = duplicates = 0
        errors: dict[str, str] = {}
        for path in paths:
            if path.stem in known:
                duplicates += 1
                continue
            try:
                event = json.loads(path.read_bytes().decode("utf-8"))
                if isinstance(event, dict) and event.get("event_id") != path.stem:
                    raise EventValidationError("file name does not match its event_id")
                if self.ingest(event):
                    imported += 1
                else:
                    duplicates += 1
            except (OSError, UnicodeError, ValueError) as exc:
                errors[str(path)] = str(exc)
        # Errors describe this folder as it is now: fixed or deleted files drop out.
        with self._connection:
            folder = str(directory)
            stale = [row[0] for row in self._connection.execute(
                "SELECT path FROM import_errors WHERE directory = ?", (folder,))
                if row[0] not in errors]
            self._connection.executemany(
                "DELETE FROM import_errors WHERE path = ?", ((path,) for path in stale))
            for path, error in errors.items():
                self._connection.execute(
                    "INSERT INTO import_errors (path, directory, error) VALUES (?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET error = excluded.error "
                    "WHERE error <> excluded.error",
                    (path, folder, error))
        return IngestionSummary(len(paths), imported, duplicates, len(errors))

    def set_hidden(self, event_id: str, hidden: bool) -> bool:
        self._forget()
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE events SET hidden = ? WHERE event_id = ?", (int(hidden), event_id))
        return cursor.rowcount == 1

    def _mod_rows(self, instance_id: str, mod_name: str) -> list[sqlite3.Row]:
        """Every event of one mod (by its current name), newest first."""

        _text(instance_id, "instance_id", required=True, pattern=_INSTANCE_ID)
        _text(mod_name, "mod_name", required=True)
        key = _name_key(mod_name)
        rows = self._mod_row_cache.get(instance_id)
        if rows is None:
            rows = self._mod_row_cache[instance_id] = self._connection.execute(
                "SELECT event_id, mod_name, name_override, removed_at, kind, archive, completed_at FROM events "
                f"WHERE instance_id = ? {_NEWEST_FIRST}", (instance_id,)).fetchall()
        return [row for row in rows
                if _name_key(row["name_override"] or row["mod_name"]) == key]

    def mark_removed(self, instance_id: str, mod_name: str, removed_at: str, *, offline: bool = False) -> bool:
        """Mark the newest event for one current mod as removed. ``offline``: it was found gone when MO2 started
        (the list's history then says it was removed while Companion wasn't watching)."""

        self._forget()
        removed_at = _timestamp(removed_at, "removed_at")
        rows = self._mod_rows(instance_id, mod_name)
        if not rows or rows[0]["removed_at"] is not None:
            return False
        with self._connection:
            self._connection.execute(
                "UPDATE events SET removed_at = ? WHERE event_id = ?",
                (removed_at, rows[0]["event_id"]))
            name = rows[0]["name_override"] or rows[0]["mod_name"]
            history_store.add_record(self._connection, instance_id, {
                "kind": history.MOD_REMOVED, "count": 1, "items": [{"name": name}]}, removed_at, offline=offline)
        self._forget()   # the rows read above are out of date now
        return True

    def rename_mod(self, instance_id: str, old_name: str, new_name: str, *, offline: bool = False) -> bool:
        self._forget()
        """Move a current mod's records to the name MO2 now uses for it.

        The recorded name stays in the event; only the override changes."""

        _text(new_name, "new_name", required=True)
        rows = self._mod_rows(instance_id, old_name)
        if not rows or rows[0]["removed_at"] is not None:
            return False
        with self._connection:
            for row in rows:
                override = None if row["mod_name"] == new_name else new_name
                self._connection.execute(
                    "UPDATE events SET name_override = ? WHERE event_id = ?",
                    (override, row["event_id"]))
            if old_name.casefold() != new_name.casefold():
                history_store.add_record(self._connection, instance_id, {
                    "kind": history.MOD_RENAMED, "count": 1, "items": [{"name": new_name, "before": old_name}]},
                    history.utc_text(), offline=offline)
        self._forget()   # the rows read above are out of date now
        return True

    def save_review(self, body: dict[str, Any]) -> dict[str, Any]:
        return reviews.save_review(self, body)

    def delete_review(self, body: dict[str, Any]) -> bool:
        return reviews.delete_review(self, body)

    def set_changes_seen(self, event_id: str, seen: bool) -> bool:
        """Settle (or reopen) the changed-installer card of one install; False when it doesn't exist."""

        if not isinstance(event_id, str):
            raise ValueError("event_id must be text")
        with self._connection:
            if not seen:
                self._connection.execute("DELETE FROM changes_seen WHERE event_id = ?", (event_id,))
                return True
            if self._connection.execute(
                    "SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone() is None:
                return False
            self._connection.execute(
                "INSERT INTO changes_seen (event_id, seen_at) VALUES (?, ?) "
                "ON CONFLICT(event_id) DO NOTHING",
                (event_id, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return True

    def changes_seen(self) -> set[str]:
        """The installs whose changed-installer card was marked seen."""

        return {row[0] for row in self._connection.execute("SELECT event_id FROM changes_seen")}

    # --- new-patch alerts ------------------------------------------------------

    def add_patch_alerts(self, instance_id: str, mod_name: str, install_event_id: str,
                         keys: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Remember that ``mod_name`` was just installed and these skipped options (event id, option
        id) are patches for it. Returns the keys that were new: one already stored for this mod,
        pending or cleared, is not announced again."""

        _text(instance_id, "instance_id", required=True, pattern=_INSTANCE_ID)
        _text(mod_name, "mod_name", required=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        added = []
        with self._connection:
            for event_id, option_id in keys:
                cursor = self._connection.execute(
                    "INSERT INTO patch_alerts (instance_id, mod_key, mod_name, event_id, option_id,"
                    " install_event_id, state, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?) "
                    "ON CONFLICT DO NOTHING",
                    (instance_id, _name_key(mod_name), mod_name, event_id, option_id,
                     install_event_id or "", stamp))
                if cursor.rowcount:
                    added.append((event_id, option_id))
        return added

    def patch_alerts(self, instance_id: str = "", *, pending: bool = True) -> list[dict[str, Any]]:
        """Alert rows, oldest first: the pending ones (or every one), for one instance or all."""

        where, parameters = [], []
        if instance_id:
            where.append("instance_id = ?")
            parameters.append(instance_id)
        if pending:
            where.append("state = 'pending'")
        rows = self._connection.execute(
            "SELECT * FROM patch_alerts" + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY created_at, mod_key, event_id, option_id", parameters)
        return [dict(row) for row in rows]

    def clear_patch_alerts(self, rows: list[dict[str, Any]]) -> int:
        """Mark these alert rows (as ``patch_alerts`` returned them) dealt with."""

        stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        changed = 0
        with self._connection:
            for row in rows:
                changed += self._connection.execute(
                    "UPDATE patch_alerts SET state = 'cleared', cleared_at = ? WHERE instance_id = ?"
                    " AND mod_key = ? AND event_id = ? AND option_id = ? AND state = 'pending'",
                    (stamp, row["instance_id"], row["mod_key"], row["event_id"], row["option_id"])).rowcount
        return changed

    def forget_patch_alerts(self, instance_id: str, mod_name: str) -> int:
        """Drop every alert row of one mod (it was removed), so a later install can announce again."""

        with self._connection:
            return self._connection.execute(
                "DELETE FROM patch_alerts WHERE instance_id = ? AND mod_key = ?",
                (instance_id, _name_key(mod_name))).rowcount

    # --- the list's change history (core/history.py, core/history_store.py) ----------------------------------

    def history_save(self, instance_id: str, profile: str, snapshot: dict[str, Any],
                     entries: list[dict[str, Any]] | None = None, *, at: str | None = None, offline: int = False,
                     dedupe_since: str | None = None, keep: int = history.KEEP_ENTRIES) -> int:
        """Store the entries of a diff and the snapshot they lead to in one transaction (a snapshot is never kept
        without the entries that explain how it differs from the last one). Returns how many entries were added."""

        at = at or history.utc_text()
        with self._connection:
            added = history_store.add_entries(self._connection, instance_id, profile, entries or [], at=at,
                                              offline=offline, dedupe_since=dedupe_since, keep=keep)
            history_store.save_snapshot(self._connection, instance_id, profile, snapshot, at)
        return added

    def history_add(self, instance_id: str, profile: str, entries: list[dict[str, Any]], *, at: str | None = None,
                    offline: int = False, dedupe_since: str | None = None, keep: int = history.KEEP_ENTRIES) -> int:
        with self._connection:
            return history_store.add_entries(self._connection, instance_id, profile, entries, at=at, offline=offline,
                                             dedupe_since=dedupe_since, keep=keep)

    def history_snapshot(self, instance_id: str, profile: str) -> tuple[dict[str, Any], str] | None:
        return history_store.load_snapshot(self._connection, instance_id, profile)

    def history_last_profile(self, instance_id: str) -> str | None:
        return history_store.last_profile(self._connection, instance_id)

    def history_touch(self, instance_id: str, profile: str) -> None:
        with self._connection:
            history_store.touch_snapshot(self._connection, instance_id, profile)

    def history_start_run(self, instance_id: str, profile: str, name: str, path: str, *,
                          at: str | None = None, keep: int = history.KEEP_ENTRIES) -> int:
        with self._connection:
            return history_store.start_run(self._connection, instance_id, profile, name, path, at, keep)

    def history_finish_run(self, entry_id: int, exit_code: int | None, *, at: str | None = None) -> bool:
        with self._connection:
            return history_store.finish_run(self._connection, entry_id, exit_code, at)

    def history_page(self, instance_id: str, **options: Any) -> list[dict[str, Any]]:
        return history_store.page(self._connection, instance_id, **options)

    def history_count(self, instance_id: str, **options: Any) -> int:
        return history_store.count(self._connection, instance_id, **options)

    def history_latest_run(self, instance_id: str, *, profile: str | None = None) -> dict[str, Any] | None:
        return history_store.latest_run(self._connection, instance_id, profile=profile)

    def history_profiles(self, instance_id: str) -> list[str]:
        return history_store.profiles(self._connection, instance_id)

    def history_instances(self) -> list[str]:
        return history_store.instances(self._connection)

    # --- reading -------------------------------------------------------------

    def _events(self, where: str = "", parameters: tuple = ()) -> list[dict[str, Any]]:
        if not where and self._all_events is not None:
            return self._all_events
        rows = self._connection.execute(
            "SELECT raw_json, name_override, hidden, removed_at FROM events "
            f"{where} {_NEWEST_FIRST}", parameters).fetchall()
        events = [self._unpack(row) for row in rows]
        if not where:
            self._all_events = events
        return events

    @staticmethod
    def _unpack(row: sqlite3.Row) -> dict[str, Any]:
        event = json.loads(row["raw_json"])
        if row["name_override"] is not None:
            event["mod"]["recorded_name"] = event["mod"]["name"]
            event["mod"]["name"] = row["name_override"]
        event["hidden"] = bool(row["hidden"])
        event["removed_at"] = row["removed_at"]
        return event

    def event(self, event_id: str) -> dict[str, Any] | None:
        found = self._events("WHERE event_id = ?", (event_id,))
        return found[0] if found else None

    def installer_record_of(self, instance_id: str, mod_name: str, archive: str, since: float) -> dict[str, Any] | None:
        """The newest record of an installer (not a plain record) for this mod from this archive file, completed
        at or after ``since`` (seconds since the epoch): the record of the install that has just happened."""

        key = _archive_key(archive)
        for row in self._mod_rows(instance_id, mod_name):
            if (row["kind"] != plain_record.KIND and _archive_key(row["archive"]) == key
                    and _seconds(row["completed_at"]) >= since):
                return self.event(row["event_id"])
        return None

    def newest_for_mod(self, instance_id: str, mod_name: str) -> dict[str, Any] | None:
        """The newest event of one mod, removed or hidden or not."""

        rows = self._mod_rows(instance_id, mod_name)
        return self.event(rows[0]["event_id"]) if rows else None

    def _index(self) -> list[sqlite3.Row]:
        """The index columns of every event, newest first (read once per state of the database)."""

        if self._index_rows is None:
            self._index_rows = self._connection.execute(
                "SELECT rowid AS rid, event_id, instance_id, mod_name, name_override, removed_at, kind FROM events "
                f"{_NEWEST_FIRST}").fetchall()
        return self._index_rows

    def _fetch(self, ids: list[str]) -> list[dict[str, Any]]:
        """The events with these ids, parsed (once each), in the order given."""

        if self._all_events is not None:
            by_id = {event["event_id"]: event for event in self._all_events}
            return [by_id[event_id] for event_id in ids if event_id in by_id]
        missing = [event_id for event_id in ids if event_id not in self._parsed]
        for start in range(0, len(missing), 400):
            chunk = missing[start:start + 400]
            for row in self._connection.execute(
                    "SELECT event_id, raw_json, name_override, hidden, removed_at FROM events WHERE event_id IN ("
                    + ",".join("?" * len(chunk)) + ")", chunk):
                self._parsed[row["event_id"]] = self._unpack(row)
        return [self._parsed[event_id] for event_id in ids if event_id in self._parsed]

    def _current(self) -> list[dict[str, Any]]:
        """The newest event of each mod in each instance, removed ones left out. Only those are parsed."""

        seen: set[tuple[str, str]] = set()
        ids = []
        for row in self._index():
            key = (row["instance_id"], _name_key(row["name_override"] or row["mod_name"]))
            if key in seen:
                continue
            seen.add(key)
            if row["removed_at"] is None:
                ids.append(row["event_id"])
        return self._fetch(ids)

    def newest_installs(self) -> list[dict[str, Any]]:
        """For each mod: its newest record (removed or hidden or not) and its two newest installer records, newest
        first: all that ``installer_changes`` needs of the history, without parsing the rest of it."""

        everything: dict[tuple[str, str], int] = {}
        installers: dict[tuple[str, str], int] = {}
        ids = []
        for row in self._index():
            key = (row["instance_id"], _name_key(row["name_override"] or row["mod_name"]))
            take = False
            if everything.get(key, 0) < 1:
                everything[key] = 1
                take = True
            if row["kind"] != plain_record.KIND and installers.get(key, 0) < 2:
                installers[key] = installers.get(key, 0) + 1
                take = True
            if take:
                ids.append(row["event_id"])
        return self._fetch(ids)

    def use_origin_cache(self, cache: OriginCache, stamp: Any) -> None:
        """Keep the file index in ``cache`` between connections, for as long as ``stamp`` (what says the database is
        unchanged) stays the same. With no stamp (None) the index is built for this connection only."""

        self._origin_cache = (cache, stamp)

    def _origin_state(self) -> OriginCache:
        if self._origin_cache is not None and self._origin_cache[1] is not None:
            cache, stamp = self._origin_cache
            if not cache.valid(stamp):
                cache.reset(stamp, FileIndex(self._installer_events()))
            return cache
        events = self._events()
        if self._file_index is None or self._file_index[0] is not events:
            local = OriginCache()
            local.reset(object(), FileIndex(self._installer_events()))
            self._file_index = (events, local)
        return self._file_index[1]

    def _installer_events(self) -> list[dict[str, Any]]:
        """The events that have installer options (plain records have none; their files are looked up in SQL)."""

        return [event for event in self._events() if event.get("kind") != plain_record.KIND]

    def file_origins(self, query: str, *, current: bool = False, instance_id: str = "") -> dict[str, Any]:
        """Which options install a path or plugin (see ``core.file_origin``), over every install, or over each
        mod's current install in one instance (all instances when ``instance_id`` is empty); mods that were not
        installed through an installer answer too, as plain matches. The index of installer files is built the
        first time this is asked, and the answers are kept until the events change."""

        state = self._origin_state()
        key = (query, current, instance_id)
        if key not in state.answers:
            ids = None
            if current:
                if instance_id not in state.current:
                    state.current[instance_id] = {
                        event["event_id"] for event in self._current()
                        if not instance_id or event["instance"]["id"] == instance_id}
                ids = state.current[instance_id]
            answer = state.index.lookup(query, ids)  # pyright: ignore[reportOptionalMemberAccess]  # _origin_state() fills the index
            self._add_plain_matches(answer, ids)
            state.answers[key] = answer
        return state.answers[key]

    def _add_plain_matches(self, answer: dict[str, Any], ids: set[str] | None) -> None:
        """Add the files of plain records to an answer from the installer index (in place)."""

        scope = {row["event_id"]: row["rid"] for row in self._index()
                 if row["kind"] == plain_record.KIND and (ids is None or row["event_id"] in ids)}
        plain_events = {event["event_id"]: event for event in self._fetch(list(scope))}
        if not plain_events:
            return
        wanted = plain_path(answer["path"])
        if wanted:
            rows, many = self._plain_rows(wanted, scope)
            answer["matches"] = sorted(
                [*answer["matches"], *plain_matches(rows, plain_events)],
                key=lambda match: (-_seconds(match["completed_at"]), match["mod_name"].casefold(),
                                   match.get("plain", False), match["path"].casefold()))
            if many:
                names = sorted({plain_events[event_id]["mod"]["name"] for event_id in many}, key=str.casefold)
                answer["notes"].append(
                    f"{', '.join(names[:4])}{f' and {len(names) - 4} more' if len(names) > 4 else ''} "
                    f"{'has' if len(names) == 1 else 'have'} more than {_PLAIN_FILES_PER_MOD} files with that name: "
                    f"only the first {_PLAIN_FILES_PER_MOD} of each are listed.")
        cut = sorted({event["mod"]["name"] for event in plain_events.values()
                      if event["plain"].get("files_truncated")}, key=str.casefold)
        if cut:
            answer["truncated"] = sorted({*answer["truncated"], *cut}, key=str.casefold)
            answer["notes"] = [note for note in answer["notes"] if not note.startswith("The list of files was cut")]
            names = ", ".join(answer["truncated"][:4]) + (
                f" and {len(answer['truncated']) - 4} more" if len(answer["truncated"]) > 4 else "")
            answer["notes"].insert(0, f"The list of files was cut short for {names}, so a file may be missing "
                                      "from the answer.")

    def _plain_rows(self, wanted: str, scope: dict[str, int]) -> tuple[list[tuple[str, str, str]], set[str]]:
        """(event id, path, how) of the plain files that are ``wanted``, and the events with more files of that name than
        are listed. ``scope`` maps each plain record in question to its rowid. Each record is asked on its own, with a
        range on its own rows (the path compares without regard to ASCII case), so no record can use up another's room:
        the file itself, a file of that name anywhere in the mod when no folder is given, and, one file is enough to
        name the mod, a file inside the folder when it is a folder."""

        found: list[tuple[str, str, str]] = []
        many: set[str] = set()
        execute = self._connection.execute
        suffix = "/" + wanted.lower()
        for event_id, rowid in scope.items():
            exact = execute("SELECT path FROM plain_files WHERE event_rowid = ? AND path = ?", (rowid, wanted)).fetchone()
            if exact is not None:
                found.append((event_id, exact[0], "file"))
            if "/" not in wanted:
                named = [row[0] for row in execute(
                    "SELECT path FROM plain_files WHERE event_rowid = ? AND path <> ? AND lower(substr(path, ?)) = ? LIMIT ?",
                    (rowid, wanted, -len(suffix), suffix, _PLAIN_FILES_PER_MOD + 1))]
                if len(named) > _PLAIN_FILES_PER_MOD:
                    many.add(event_id)
                found += [(event_id, path, "file") for path in named[:_PLAIN_FILES_PER_MOD]]
            inside = execute("SELECT path FROM plain_files WHERE event_rowid = ? AND path >= ? AND path < ? LIMIT 1",
                             (rowid, wanted + "/", wanted + "0")).fetchone()
            if inside is not None:
                found.append((event_id, inside[0], "inside"))
        return found, many

    def plain_files(self, event_id: str) -> list[str]:
        """The files recorded for one plain record, by path. Empty for a record whose list was not kept (see
        ``files_kept``)."""

        return [row[0] for row in self._connection.execute(
            "SELECT path FROM plain_files WHERE event_rowid = (SELECT rowid FROM events WHERE event_id = ?)", (event_id,))]

    def files_kept(self, event: dict[str, Any]) -> bool:
        """Whether a plain record still has its file list: only a mod's newest records do (``_prune_files``)."""

        if event.get("kind") != plain_record.KIND or not event["plain"].get("file_count"):
            return True
        return self._connection.execute(
            "SELECT 1 FROM plain_files WHERE event_rowid = (SELECT rowid FROM events WHERE event_id = ?) LIMIT 1",
            (event["event_id"],)).fetchone() is not None

    def with_files(self, event: dict[str, Any]) -> dict[str, Any]:
        """The event, with a plain record's file list put back (a copy; any other event comes back as it is, and so
        does a plain record whose list was not kept)."""

        if event.get("kind") != plain_record.KIND or not self.files_kept(event):
            return event
        copy = dict(event)
        copy["plain"] = {**event["plain"], "files": self.plain_files(event["event_id"])}
        return copy

    def _origin_events(self, search: str) -> set[str]:
        """The installs that have a file or plugin matching the search, when it looks like a path ("" otherwise)."""

        if not looks_like_path(search):
            return set()
        return {match["event_id"] for match in self.file_origins(search)["matches"]}

    def latest(self, search: str = "", *, include_hidden: bool = False) -> list[dict[str, Any]]:
        term = _search_key(search.strip())
        files = self._origin_events(search) if term else set()
        return [event for event in self._current()
                if (include_hidden or not event["hidden"])
                and (not term or _matches(event, term) or event["event_id"] in files)]

    def history(self, search: str = "", *, include_hidden: bool = False) -> list[dict[str, Any]]:
        """Every event, newest first, without the current-view ranking."""

        term = _search_key(search.strip())
        files = self._origin_events(search) if term else set()
        return [event for event in self._events()
                if (include_hidden or not event["hidden"])
                and (not term or _matches(event, term) or event["event_id"] in files)]

    def current_mods(self, instance_id: str) -> list[dict[str, str]]:
        """Each current, non-removed mod of one instance: its name, the archive
        of its newest install, when its first install started and the kind of
        its newest record ("fomod" or "plain"). Reads only the indexed columns,
        so it stays cheap on MO2's UI thread."""

        _text(instance_id, "instance_id", required=True, pattern=_INSTANCE_ID)
        mods: dict[str, dict[str, Any]] = {}
        for row in self._connection.execute(
                "SELECT mod_name, name_override, archive, started_at, removed_at, kind FROM events "
                f"WHERE instance_id = ? {_NEWEST_FIRST}", (instance_id,)):
            name = row["name_override"] or row["mod_name"]
            key = _name_key(name)
            if key not in mods:
                mods[key] = {"name": name, "archive": row["archive"], "kind": row["kind"],
                             "removed": row["removed_at"] is not None}
            elif row["removed_at"] is not None:
                mods[key]["ended"] = True  # an earlier install that was removed
            if not mods[key].get("ended"):
                mods[key]["first_started_at"] = row["started_at"]
        current = [{key: mod[key] for key in ("name", "archive", "first_started_at", "kind")}
                   for mod in mods.values() if not mod["removed"]]
        return sorted(current, key=lambda mod: (mod["name"].lower(), mod["name"]))

    def current_mod_names(self, instance_id: str) -> list[str]:
        """Current, non-removed mod names for one Logger instance."""

        return [mod["name"] for mod in self.current_mods(instance_id)]

    def cross_patches(self, search: str = "", *,
                      include_hidden: bool = False) -> list[dict[str, Any]]:
        patches = analyze_cross_patches(self.latest(include_hidden=include_hidden))
        term = _search_key(search.strip())
        if not term:
            return patches

        def matches(patch: dict[str, Any]) -> bool:
            labels = [patch.get("option")]
            for mod in patch.get("mods", []):
                labels.append(mod.get("name"))
                labels.extend(mod.get("original_texts", []))
                labels.extend(occurrence.get(field) for occurrence in mod.get("occurrences", [])
                              for field in ("step_name", "group_name", "option_name"))
            return any(term in _search_key(label) for label in labels)

        return [patch for patch in patches if matches(patch)]

    def reviews(self) -> list[dict[str, Any]]:
        return reviews.list_reviews(self)

    def full_history_export(self) -> dict[str, Any]:
        """Every event and review plus the current cross-patch analysis."""

        self._connection.execute("BEGIN")  # one read snapshot for all three parts
        try:
            return {
                "format": "fomod-logger-full-history",
                "format_version": 2,
                "events": [self.with_files(event) for event in self.history(include_hidden=True)],
                "reviews": self.reviews(),
                "current_cross_patches": analyze_cross_patches(self.latest(include_hidden=True)),
            }
        finally:
            self._connection.rollback()

    def instances(self) -> list[dict[str, str]]:
        labels: dict[str, str] = {}
        # The label of each instance comes from its newest event: only those are parsed.
        first: dict[str, str] = {}
        for row in self._index():
            first.setdefault(row["instance_id"], row["event_id"])
        for event in self._fetch(list(first.values())):
            instance = event["instance"]
            labels.setdefault(instance["id"], instance["label"])
        return [{"id": id_, "label": label}
                for id_, label in sorted(labels.items(), key=lambda item: (item[1], item[0]))]

    def import_errors(self) -> list[dict[str, str]]:
        rows = self._connection.execute(
            "SELECT path, error, observed_at FROM import_errors ORDER BY observed_at DESC, path")
        return [dict(row) for row in rows]

    def event_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
