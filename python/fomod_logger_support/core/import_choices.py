"""Import a choices file so the installer can offer its picks again.

The native installer finds a mod's previous choices by reading files in the Companion's data folder (it has no
database access), so imported choices are files too: one JSON record per mod in ``<data>/imported``. The
installer reads them only when it has no choices of the user's own for that mod, and only ever offers them
(tagged "(imported)", behind a "Select imported" button); nothing is ticked without the user's say.

A record is ``{"format": "fomod-companion-imported-choices", "record_version": 1, "instance_id", "imported_at",
"source_exported_at", "entry": <the mod's entry from the choices file>}``. ``instance_id`` is the MO2 instance
that imported it; the installer ignores records of other instances. ``imported_at`` is always the time of the
import, taken here: nothing in a choices file can set it, so a file can't make its mods look "newer" than
another import. tests/native/fixtures/imported_record.json is one record, read by both this code's tests and the
native tests, so the two sides agree on it.

A choices file says which game it is for (``source.nexus_domain``). Nexus IDs are only meaningful within one
game, so a file for another game is refused, and one that doesn't say has its Nexus IDs dropped (those mods are
then matched by archive file name and mod name only).

A record is limited to the size the installer will read (``MAX_RECORD_BYTES``, the same 4 MB as the installer's
kMaxEventFileBytes); a mod whose entry is bigger is left out and counted, not stored.

Removing the imported choices deletes these files; nothing else is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .choices_file import ChoicesFileError, MAX_FILE_BYTES, parse_choices_file, validate_choices_file
from .export_choices import now_stamp

RECORD_FORMAT = "fomod-companion-imported-choices"
RECORD_VERSION = 1
FOLDER_NAME = "imported"
MAX_RECORD_BYTES = 4 * 1024 * 1024    # what the installer reads of one file: kMaxEventFileBytes in fomodpreviouschoices.h

_INSTANCE_ID = re.compile(r"[0-9a-f]{64}")


@dataclass
class ImportSummary:
    """What an import did. ``imported`` is every mod stored; of those, ``own_wins`` already have choices of the
    user's own (the installer uses those, and falls back to the imported ones only if they match nothing in the
    installer it is showing) and ``not_installed`` don't (offered when the mod is installed)."""

    imported: int = 0
    own_wins: int = 0
    not_installed: int = 0
    duplicates: int = 0          # entries in the file that repeated an earlier one and were left out
    replaced: int = 0            # mods whose earlier imported choices this import replaced
    too_large: int = 0           # entries left out because their record would be bigger than the installer reads
    ids_dropped: bool = False    # the file didn't say which game it is for, so its Nexus IDs were not kept
    own: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)

    def message(self) -> str:
        if not self.imported:
            return "The file has no mods in it, so nothing was imported."
        lines = [f"Imported choices for {_mods(self.imported)}."]
        if self.own_wins:
            lines.append(f"{self.own_wins} already {'has' if self.own_wins == 1 else 'have'} your own previous "
                         "choices. The installer uses yours, and only offers the imported ones if yours match "
                         "nothing in the installer it is showing (a different version of the mod, say).")
        if self.not_installed:
            lines.append(f"{self.not_installed} {'is' if self.not_installed == 1 else 'are'} not installed yet; "
                         "the installer offers their imported choices when you install them.")
        if self.replaced:
            lines.append(f"{_mods(self.replaced)} had imported choices before; those were replaced.")
        if self.duplicates:
            lines.append(f"{self.duplicates} repeated {'entry' if self.duplicates == 1 else 'entries'} in the "
                         "file left out.")
        if self.too_large:
            lines.append(f"{_mods(self.too_large)} left out: too large to read back.")
        if self.ids_dropped:
            lines.append("The file doesn't say which game it is for, so its Nexus IDs were ignored; its mods are "
                         "matched by archive file name and mod name only.")
        lines.append("Importing again adds to what was imported before: a mod that is only in an earlier file "
                     "stays until you choose Remove imported choices.")
        lines.append("")
        lines.append("In the installer, press Select imported to use them. Nothing is ticked until you do.")
        return "\n".join(lines)


def _mods(count: int) -> str:
    return f"{count} mod{'' if count == 1 else 's'}"


def imported_folder(data_root: Path | str) -> Path:
    return Path(data_root) / FOLDER_NAME


def _key(name: str) -> str:
    return name.casefold()


def _record_name(instance_id: str, name: str) -> str:
    return f"{instance_id[:16]}-{hashlib.sha1(_key(name).encode('utf-8')).hexdigest()[:24]}.json"


def _file_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _write_atomic(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        record = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if (isinstance(record, dict) and record.get("format") == RECORD_FORMAT
            and isinstance(record.get("instance_id"), str) and isinstance(record.get("entry"), dict)):
        return record
    return None


def list_imported(data_root: Path | str, instance_id: str) -> list[dict[str, Any]]:
    """The imported records of one MO2 instance (each record's fields, plus its file under ``"path"``)."""

    folder = imported_folder(data_root)
    found = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        record = _read_record(path)
        if record is not None and record["instance_id"] == instance_id:
            found.append({**record, "path": str(path)})
    return found


def imported_count(data_root: Path | str, instance_id: str) -> int:
    return len(list_imported(data_root, instance_id))


def imported_file_count(data_root: Path | str, instance_id: str) -> int:
    """How many imported records this instance has, counted from the file names alone (each starts with the
    first 16 characters of the instance id) without reading any: cheap enough for every time a menu opens."""

    folder = imported_folder(data_root)
    if not folder.is_dir() or not _INSTANCE_ID.fullmatch(instance_id or ""):
        return 0
    return sum(1 for _ in folder.glob(f"{instance_id[:16]}-*.json"))


def remove_imported(data_root: Path | str, instance_id: str) -> int:
    """Delete this instance's imported choices; returns how many mods they covered. Other instances' records,
    and anything in the folder that isn't an imported record, stay."""

    removed = 0
    for record in list_imported(data_root, instance_id):
        try:
            Path(record["path"]).unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def _own_keys(store: Any, instance_id: str) -> tuple[set[str], set[str]]:
    """(mod names, archive file names), folded, of every install of the user's own in this instance: the same
    two things the installer looks a previous install up by."""

    names, archives = set(), set()
    for event in store.history("", include_hidden=True):
        if event["instance"]["id"] != instance_id or event.get("kind") == "plain":
            continue          # a mod without an installer made no choices: it is not a previous install
        mod = event["mod"]
        names.add(_key(mod["name"]))
        if mod.get("recorded_name"):
            names.add(_key(mod["recorded_name"]))
        archive = _file_name(event["source"].get("archive", "") or "")
        if archive:
            archives.add(archive.casefold())
    return names, archives


def _check_game(choices: dict[str, Any], game: dict[str, str] | None) -> bool:
    """Whether the file's Nexus IDs can be trusted for the game this MO2 manages. Raises for a file made for
    another game; returns False when either side doesn't say (the IDs can't be checked, so they are dropped)."""

    mine = ((game or {}).get("nexus_domain") or "").strip().casefold()
    theirs = (choices["source"].get("nexus_domain") or "").strip().casefold()
    if mine and theirs and mine != theirs:
        file_game = choices["source"].get("game") or theirs
        my_game = (game or {}).get("name") or mine
        raise ChoicesFileError(f"this file is for {file_game} ({theirs}), but this MO2 manages {my_game} ({mine}); "
                               "Nexus IDs and options differ between games, so it was not imported")
    return bool(mine and theirs)


def import_choices(data: Any, *, data_root: Path | str, instance_id: str, store: Any,
                   game: dict[str, str] | None, imported_at: str | None = None) -> ImportSummary:
    """Store the mods of a choices file (already parsed; it is checked again here) as this instance's imported
    choices. ``game`` is what this MO2 manages ({"name", "nexus_domain"}; None or empty when unknown). An earlier
    import of the same mod is replaced; mods only in an earlier import stay; the user's own choices are never
    touched. ``imported_at`` is for tests: the record is stamped with the time of the import."""

    if not _INSTANCE_ID.fullmatch(instance_id or ""):
        raise ChoicesFileError("this MO2 instance is not known yet")
    choices = validate_choices_file(data)
    keep_ids = _check_game(choices, game)
    folder = imported_folder(data_root)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = imported_at or now_stamp()
    own_names, own_archives = _own_keys(store, instance_id)
    existing = {_key(record["entry"].get("name", "")): record for record in list_imported(data_root, instance_id)}

    summary, seen = ImportSummary(), set()
    summary.ids_dropped = bool(choices["mods"]) and not keep_ids
    for entry in choices["mods"]:
        key = _key(entry["name"])
        if key in seen:
            summary.duplicates += 1
            continue
        seen.add(key)
        if not keep_ids:
            entry = {**entry, "nexus_id": 0}
        record = {"format": RECORD_FORMAT, "record_version": RECORD_VERSION, "instance_id": instance_id,
                  "imported_at": stamp, "source_exported_at": choices["exported_at"], "entry": entry}
        text = json.dumps(record, ensure_ascii=False, indent=1) + "\n"
        if len(text.encode("utf-8")) > MAX_RECORD_BYTES:
            summary.too_large += 1
            continue
        target = folder / _record_name(instance_id, entry["name"])
        _write_atomic(target, text)
        if key in existing:
            summary.replaced += 1
        summary.imported += 1
        archive = _file_name(entry["archive"])
        if key in own_names or (archive and archive.casefold() in own_archives):
            summary.own_wins += 1
            summary.own.append(entry["name"])
        else:
            summary.not_installed += 1
            summary.new.append(entry["name"])
    return summary


def import_choices_path(path: Path | str, *, data_root: Path | str, instance_id: str, store: Any,
                        game: dict[str, str] | None, imported_at: str | None = None) -> ImportSummary:
    """Read, check and import a choices file from disk. Any problem with the file raises ``ChoicesFileError``
    with a message fit to show."""

    path = Path(path)
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ChoicesFileError(f"the file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        raw = path.read_bytes()
    except OSError as error:
        raise ChoicesFileError(f"the file could not be read ({error.strerror or error})") from error
    return import_choices(parse_choices_file(raw), data_root=data_root, instance_id=instance_id, store=store,
                          game=game, imported_at=imported_at)
