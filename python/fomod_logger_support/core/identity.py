"""Stable MO2 instance identity shared by all Python ingestion paths."""

from __future__ import annotations

import hashlib
import os
import re
from os import PathLike


_WINDOWS_DRIVE = re.compile(r"^([A-Za-z]:)(.*)$")


def _qdir_clean_path(raw_path: str) -> str:
    """Reproduce the lexical path cleaning used by Qt's QDir::cleanPath."""

    text = raw_path.replace("\\", "/")
    drive_match = _WINDOWS_DRIVE.match(text)
    prefix = ""
    rest = text
    unc = False

    if drive_match:
        prefix = drive_match.group(1)
        rest = drive_match.group(2)
        if rest.startswith("/"):
            prefix += "/"
            rest = rest.lstrip("/")
    elif text.startswith("/"):
        leading_slashes = len(text) - len(text.lstrip("/"))
        prefix = "/" * leading_slashes
        rest = text[leading_slashes:]
        unc = leading_slashes == 2

    parts: list[str] = []
    for part in rest.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            pinned = 1 if unc and parts else 0
            if len(parts) > pinned and parts[-1] != "..":
                parts.pop()
            else:
                parts.append(part)
            continue
        parts.append(part)

    cleaned = prefix + "/".join(parts)
    return cleaned or "."


def normalized_instance_path(instance_path: str | PathLike[str]) -> str:
    """Match the native plugin's QDir::cleanPath(...).toCaseFolded() input."""

    return _qdir_clean_path(os.fspath(instance_path)).casefold()


def stable_instance_id(instance_path: str | PathLike[str]) -> str:
    normalized = normalized_instance_path(instance_path).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()
