"""The record of a mod that was not installed through a logged FOMOD installer.

Most mods in a list come from plain zips (MO2's simple or manual installer, or a folder dropped into ``mods``).
They have no installer choices, but they have a version, an archive, files and plugins, and the window, the file
lookup, compare, profiles and the missing-master check should cover them too. This module makes that record.

A plain record is an installation event like any other (same table, same schema) with ``steps`` empty and two
extra fields:

* ``kind``: ``"plain"`` (absent on FOMOD events);
* ``plain``: ``origin`` (``"install"`` for a mod the plugin saw being installed, ``"scan"`` for one it found already
  in the list), ``game`` (the Nexus address name of the managed game, when known), ``file_count``,
  ``files_truncated``, ``files_digest`` (a fingerprint of the file list, so two records can be compared without
  reading the lists), ``plugins`` (each plugin at the root of the mod folder with the masters its header names)
  and, while the event is being handed to the store, ``files`` (the relative paths; the store keeps them in
  their own table so reading the events stays cheap).

Making the record reads the mod folder, so it belongs on a worker thread (``run_job``); nothing here touches MO2
or Qt.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..archive.plugin_header import HEADER_WINDOW, MAX_HEADER_DATA, PLUGIN_EXTENSIONS, needed_bytes, read_plugin_masters
from .file_origin import _key

KIND = "plain"
INSTALLER_NAME = "No installer (plain install)"
EDITION_VERSION = "1.0.0"
# The same limits as the FOMOD file entries: 20,000 files to a record and about 2 MiB of path text, so a record
# stays small whatever the mod holds. A mod with more is recorded as cut short.
MAX_FILES = 20_000
BYTE_BUDGET = 2 * 1024 * 1024
MAX_PLUGINS = 512
PLAIN, FAILED, CANCELLED = "plain", "failed", "cancelled"
ORIGINS = ("install", "scan")
# MO2 keeps its own bookkeeping in the mod's folder; it is not part of the mod.
_MO2_FILES = frozenset({"meta.ini"})
_CHECK_EVERY = 256
_NAMESPACE = uuid.UUID("0b7d4e52-31c9-4c3f-8a76-5d2f9e4b61a8")


class JobCancelled(Exception):
    """The scan was stopped while a folder was being read."""


@dataclass(frozen=True)
class PlainJob:
    """Everything one record needs, read from MO2 on the UI thread. ``kind`` is "install" (the plugin saw the mod
    being installed) or "scan" (the mod was in the list before); ``when`` is the event's time (UTC, ISO 8601)."""

    instance_id: str
    instance_label: str
    mod_name: str
    mod_version: str
    nexus_id: int
    game: str
    archive: str
    folder: str
    when: str
    kind: str

    def run(self, cancelled: Any = None) -> "PlainResult":
        return run_job(self, cancelled)


@dataclass
class PlainResult:
    job: PlainJob
    mode: str                              # PLAIN, FAILED or CANCELLED
    event: dict[str, Any] | None = None
    reason: str = ""


def utc_text(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def event_id_for(job: PlainJob) -> str:
    """The id of a record: the same install makes the same id, so an install that is read twice is one record. A scan
    makes a new one each time: a mod that was removed and came back must be recorded again, and the store keeps
    only a mod's newest record as current."""

    if job.kind != "install":
        return str(uuid.uuid4())
    return str(uuid.uuid5(_NAMESPACE, "|".join(
        (job.instance_id, job.mod_name.casefold(), job.archive.casefold(), job.mod_version, job.when))))


def is_plain(event: dict[str, Any]) -> bool:
    return event.get("kind") == KIND


# ---------- reading the folder ----------

def _stopped(cancelled: Any) -> bool:
    return cancelled is not None and cancelled.is_set()


def _is_link(entry: os.DirEntry) -> bool:
    try:
        return entry.is_symlink() or bool(getattr(entry, "is_junction", lambda: False)())
    except OSError:
        return True


def walk_files(folder: str | Path, cancelled: Any = None, *, max_files: int = MAX_FILES,
               byte_budget: int = BYTE_BUDGET) -> tuple[list[str], bool]:
    """The files under a mod folder as relative paths with forward slashes, in a fixed order (each folder's files
    first, then its folders, by name), and whether the list is incomplete: cut short at the limits, or a folder inside
    the mod could not be read (a path too long for Windows, no permission). Links are not followed. Raises ``JobCancelled`` when ``cancelled`` is set, and ``OSError`` when the folder itself can't be read."""

    root = os.fspath(folder)
    files: list[str] = []
    used = 0
    seen = 0
    unreadable = False
    stack: list[tuple[str, str]] = [("", root)]
    while stack:
        prefix, path = stack.pop()
        try:
            with os.scandir(path) as listing:
                entries = [(entry.name, _is_link(entry), entry.is_dir(follow_symlinks=False))
                           for entry in listing]
        except OSError:
            if not prefix:
                raise            # the mod's own folder
            unreadable = True    # a folder inside it that can't be read is not listed, and the list says it is incomplete
            continue
        entries.sort(key=lambda item: (item[0].casefold(), item[0]))
        folders = []
        for name, link, is_dir in entries:
            seen += 1
            if seen % _CHECK_EVERY == 0 and _stopped(cancelled):
                raise JobCancelled()
            if link and is_dir:
                continue
            if is_dir:
                folders.append((f"{prefix}{name}/", os.path.join(path, name)))
                continue
            if not prefix and name.casefold() in _MO2_FILES:
                continue
            relative = prefix + name
            cost = len(relative.encode("utf-8", "replace")) + 1
            if len(files) >= max_files or used + cost > byte_budget:
                return files, True
            files.append(relative)
            used += cost
        stack.extend(reversed(folders))
    if _stopped(cancelled):
        raise JobCancelled()
    return files, unreadable


def files_digest(paths: list[str]) -> str:
    """A fingerprint of a file list (case and order don't matter): equal digests mean equal lists."""

    keys = sorted({_key(path) for path in paths})
    return hashlib.sha1("\n".join(keys).encode("utf-8", "replace")).hexdigest()


def read_plugins(folder: str | Path, files: list[str], cancelled: Any = None) -> list[dict[str, Any]]:
    """Each plugin at the root of the mod folder (the only place MO2 looks) with the masters its header names. A
    plugin whose header can't be read has no ``masters`` key: that is "not read", never "no masters"."""

    found = []
    for relative in files:
        if "/" in relative or not relative.casefold().endswith(PLUGIN_EXTENSIONS):
            continue
        if _stopped(cancelled):
            raise JobCancelled()
        if len(found) >= MAX_PLUGINS:
            break
        entry: dict[str, Any] = {"name": relative}
        try:
            with open(os.path.join(os.fspath(folder), relative), "rb") as handle:
                head = handle.read(HEADER_WINDOW)
                needed = needed_bytes(head)
                if needed is not None and len(head) < needed <= MAX_HEADER_DATA + 64:
                    head += handle.read(needed - len(head))
            masters = read_plugin_masters(head)
        except OSError:
            masters = None
        if masters is not None:
            entry["masters"] = masters
        found.append(entry)
    found.sort(key=lambda item: (item["name"].casefold(), item["name"]))
    return found


# ---------- the record ----------

def build_event(job: PlainJob, files: list[str], truncated: bool, plugins: list[dict[str, Any]]) -> dict[str, Any]:
    plain: dict[str, Any] = {
        "origin": job.kind, "file_count": len(files), "files_truncated": truncated,
        "files_digest": files_digest(files), "plugins": plugins, "files": files,
    }
    if job.game:
        plain["game"] = job.game
    return {
        "schema_version": 1,
        "event_id": event_id_for(job),
        "started_at": job.when,
        "completed_at": job.when,
        "instance": {"id": job.instance_id, "label": job.instance_label},
        "source": {"archive": job.archive, "reinstallation": False, **({"scanned": True} if job.kind == "scan" else {})},
        "mod": {"name": job.mod_name, "version": job.mod_version, "nexus_id": job.nexus_id},
        "installer": {"name": INSTALLER_NAME, "version": EDITION_VERSION, "upstream_commit": "unknown"},
        "steps": [],
        "kind": KIND,
        "plain": plain,
    }


def _folder_time(folder: str) -> str:
    """When a mod folder was made (what a scanned mod is dated by); now, when the disk can't say."""

    try:
        info = os.stat(folder)
    except OSError:
        return utc_text()
    # On Windows st_ctime is the creation time.
    return utc_text(datetime.fromtimestamp(getattr(info, "st_birthtime", info.st_ctime), timezone.utc))


def run_job(job: PlainJob, cancelled: Any = None) -> PlainResult:
    """Read the mod's folder and make its record. Never raises for a folder problem: the result then says why."""

    try:
        if not job.when:
            job = replace(job, when=_folder_time(job.folder))
        files, truncated = walk_files(job.folder, cancelled)
        plugins = read_plugins(job.folder, files, cancelled)
    except JobCancelled:
        return PlainResult(job, CANCELLED, reason="stopped")
    except OSError as error:
        return PlainResult(job, FAILED, reason=f"{job.mod_name}: its folder cannot be read ({error})")
    try:
        from .storage import validate_event      # late: storage imports this module's helpers
        event = build_event(job, files, truncated, plugins)
        validate_event(event)
    except Exception as error:
        return PlainResult(job, FAILED, reason=f"{job.mod_name}: {type(error).__name__}: {error}")
    return PlainResult(job, PLAIN, event)
