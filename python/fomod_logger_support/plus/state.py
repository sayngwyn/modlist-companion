"""What the FOMOD Plus edition remembers about the records it has already turned into events.

One small JSON file in the edition's own data folder: for each mod (by instance and folded name), the digest of
the record, the archive and the event made from them. A record that comes back unchanged for the same archive is not
a new install. Losing the file costs nothing worse than one repeated event, so every failure to read it is
treated as an empty file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_VERSION = 1


class RecordState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, dict[str, dict[str, str]]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            records = raw["records"] if raw.get("version") == _VERSION else {}
            self._data = {str(instance): {str(key): dict(entry) for key, entry in mods.items()}
                          for instance, mods in records.items()}
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps({"version": _VERSION, "records": self._data}, indent=1, sort_keys=True),
                             encoding="utf-8")
        os.replace(temporary, self.path)

    def get(self, instance_id: str, mod_key: str) -> dict[str, str] | None:
        return self._data.get(instance_id, {}).get(mod_key)

    def set(self, instance_id: str, mod_key: str, **entry: Any) -> None:
        self._data.setdefault(instance_id, {})[mod_key] = {key: str(value) for key, value in entry.items()}
        self._save()

    def forget(self, instance_id: str, mod_key: str) -> None:
        if self._data.get(instance_id, {}).pop(mod_key, None) is not None:
            self._save()
