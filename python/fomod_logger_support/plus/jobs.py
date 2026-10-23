"""Turn one FOMOD Plus record into an installation event: the work done off MO2's UI thread.

``run_job`` is the whole of it and touches nothing of MO2's: everything it needs is in the ``RebuildJob``, which
the plugin fills in on the UI thread. With the mod's download archive in reach the event is rebuilt from the
archive's FOMOD and the picks (see ``archive/rebuild.py``); without it (gone, unreadable, no FOMOD inside) the
event lists just the option names FOMOD Plus saved, and says so.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..archive.rebuild import ConditionEnvironment, Selection, reconstruct_from_archive
from ..archive.sevenzip import ArchiveError, Limits
from ..core.storage import validate_event
from . import record as plus_record

EDITION_VERSION = "1.0.0"
NAMES_ONLY_INSTALLER = "Modlist Companion (names saved by FOMOD Plus)"
_EVENT_NAMESPACE = uuid.UUID("9d1c6a0e-7a5b-4d5e-9c2f-3b7a21d0c6f4")

REBUILT, NAMES_ONLY, FAILED, CANCELLED = "rebuilt", "names-only", "failed", "cancelled"


@dataclass(frozen=True)
class RebuildJob:
    """Everything one rebuild needs. ``kind`` is "install" (FOMOD Plus has just installed the mod) or "scan" (the
    mod was installed earlier); ``when`` is the event's time (UTC, ISO 8601)."""

    instance_id: str
    instance_label: str
    mod_name: str
    mod_version: str
    nexus_id: int
    archive_path: str
    record: str
    digest: str
    when: str
    kind: str
    reinstallation: bool = False
    plugin_states: Mapping[str, str] = field(default_factory=dict)
    sevenzip_dll: str = ""
    # What to record instead when the choices cannot be used at all (a record that cannot be read): a plain record of the
    # mod, so that it is not left without one (set for the scan's jobs).
    fallback: Any = None

    def run(self, cancelled: Any = None) -> "JobResult":
        """What the scan calls (see ``scan.py``): the same job for every kind of record."""

        result = run_job(self, cancelled)
        if result.mode == FAILED and self.fallback is not None:
            return self.fallback.run(cancelled)
        return result


@dataclass
class JobResult:
    job: RebuildJob
    mode: str                          # REBUILT, NAMES_ONLY or FAILED
    event: dict[str, Any] | None = None
    reason: str = ""                   # why the archive was not used, or what failed
    warnings: list[str] = field(default_factory=list)


def utc_text(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def event_id_for(job: RebuildJob, archive_name: str) -> str:
    """The same record of the same archive makes the same id when scanned twice, and a new one for each install."""

    stamp = job.when if job.kind == "install" else "scan"
    return str(uuid.uuid5(_EVENT_NAMESPACE, "|".join(
        (job.instance_id, job.mod_name.casefold(), archive_name.casefold(), job.digest, stamp))))


def names_only_event(job: RebuildJob, reason: str) -> dict[str, Any]:
    """The event for a record when the archive cannot be used: the options FOMOD Plus saved, picked or turned off by
    hand, in the order it saved them. Nothing else about them is known."""

    parsed = plus_record.parse_record(job.record)
    steps = []
    for s, raw_step in enumerate(parsed["steps"]):
        groups = []
        for g, raw_group in enumerate(raw_step.get("groups", [])):
            options = []
            for name, picked in ([(n, True) for n in raw_group.get("plugins", [])]
                                 + [(n, False) for n in raw_group.get("deselected", [])]):
                options.append({"id": f"step:{s}/group:{g}/option:{len(options)}", "name": name,
                                "selected": picked, "enabled": True})
            groups.append({"id": f"step:{s}/group:{g}", "name": str(raw_group.get("name", "")),
                           "type": "SelectAny", "options": options})
        steps.append({"id": f"step:{s}", "name": str(raw_step.get("name", "")), "visible": True, "groups": groups})
    archive_name = Path(job.archive_path.replace("\\", "/")).name if job.archive_path else ""
    notes = [reason, "only the option names FOMOD Plus saved are listed: not their plugins, files or the "
                     "groups' rules, so missed patches and file lookups cannot use this install"]
    event = {
        "schema_version": 1,
        "event_id": event_id_for(job, archive_name),
        "started_at": job.when,
        "completed_at": job.when,
        "instance": {"id": job.instance_id, "label": job.instance_label},
        "source": {"archive": archive_name, "reinstallation": job.reinstallation, "reconstructed": True,
                   "reconstruction_notes": notes},
        "mod": {"name": job.mod_name, "version": job.mod_version, "nexus_id": job.nexus_id},
        "installer": {"name": NAMES_ONLY_INSTALLER, "version": EDITION_VERSION, "upstream_commit": "unknown"},
        "steps": steps,
    }
    if job.kind == "scan":
        event["source"]["scanned"] = True        # found already installed: not a change the list's history saw
    validate_event(event)
    return event


def run_job(job: RebuildJob, cancelled: Any = None) -> JobResult:
    """Rebuild the event for one record. Never raises for an archive problem: the event then has the names only.
    ``cancelled`` (a ``threading.Event``) stops a read that is under way; the result is then ``CANCELLED``."""

    result = _run_job(job, cancelled)
    if cancelled is not None and cancelled.is_set():
        return JobResult(job, CANCELLED, reason="stopped")
    return result


def _run_job(job: RebuildJob, cancelled: Any) -> JobResult:
    try:
        selection = Selection.from_plus_metadata(job.record)
    except ValueError as error:
        return JobResult(job, FAILED, reason=f"FOMOD Plus's saved choices for {job.mod_name} cannot be read: {error}")
    archive = Path(job.archive_path) if job.archive_path else None
    if archive is None:
        return _names_only(job, "the mod's download archive is not recorded")
    try:
        present = archive.is_file()
    except OSError:
        present = False
    if not present:
        return _names_only(job, f"the download archive {archive.name} is no longer there")
    environment = ConditionEnvironment(file_state=dict(job.plugin_states) or None)
    try:
        built = reconstruct_from_archive(
            archive, selection, sevenzip_dll=job.sevenzip_dll or None, environment=environment,
            limits=Limits(cancel=cancelled),
            instance_id=job.instance_id, instance_label=job.instance_label, mod_name=job.mod_name,
            mod_version=job.mod_version, nexus_id=job.nexus_id, started_at=job.when, completed_at=job.when,
            installer_version=EDITION_VERSION, event_id=event_id_for(job, archive.name),
            reinstallation=job.reinstallation)
    except ArchiveError as error:
        return _names_only(job, f"the download archive {archive.name} cannot be read ({error})")
    except Exception as error:   # the FOMOD inside, or the reader: any failure still leaves the names
        return _names_only(job, f"the FOMOD in {archive.name} cannot be rebuilt ({type(error).__name__}: {error})")
    event = built.event
    notes = event["source"].setdefault("reconstruction_notes", [])
    if job.kind == "scan":
        event["source"]["scanned"] = True        # found already installed: not a change the list's history saw
        notes.insert(0, "the mod was installed before Modlist Companion saw it; this install was rebuilt from its "
                        "download archive and what FOMOD Plus saved")
    if job.plugin_states:
        notes.append("the FOMOD's file conditions were checked against the plugins MO2 lists now, not those "
                     "at install time")
    return JobResult(job, REBUILT, event, warnings=list(built.warnings))


def _names_only(job: RebuildJob, reason: str) -> JobResult:
    try:
        return JobResult(job, NAMES_ONLY, names_only_event(job, reason), reason=reason)
    except Exception as error:
        return JobResult(job, FAILED, reason=f"{job.mod_name}: {type(error).__name__}: {error}")
