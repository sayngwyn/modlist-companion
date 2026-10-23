"""Read the master list from the first bytes of a Bethesda plugin.

Mirrors ``readPluginMasters`` in ``src/fomodpatchevidence.cpp`` for the TES4 record
(Oblivion, Fallout 3/NV, Skyrim, Skyrim SE, Fallout 4, Starfield), and also reads
Morrowind's TES3 header, which the native reader does not.

The answer is a list of master names, or ``None`` ("not read") for anything that is not a
readable header, so a failed read is never mistaken for "no masters".
"""

from __future__ import annotations

import struct

__all__ = ["HEADER_WINDOW", "MAX_HEADER_DATA", "needed_bytes", "read_plugin_masters"]

_OBLIVION_HEADER = 20
_RECORD_HEADER = 24
_COMPRESSED = 0x00040000
# Header data larger than this is not a plausible TES4 record (same limit as the native reader).
MAX_HEADER_DATA = 4 * 1024 * 1024
# How much of a plugin is enough for nearly every header; use ``needed_bytes`` for the rest.
HEADER_WINDOW = 64 * 1024

PLUGIN_EXTENSIONS = (".esp", ".esm", ".esl")


def needed_bytes(head: bytes) -> int | None:
    """How many leading bytes of the plugin hold its whole header record, if that is knowable.

    ``None`` when ``head`` is not the start of a TES4/TES3 record.
    """

    if len(head) >= _OBLIVION_HEADER + 4 and head[:4] == b"TES4":
        data_size = struct.unpack_from("<I", head, 4)[0]
        if head[_OBLIVION_HEADER:_OBLIVION_HEADER + 4] == b"HEDR":
            return _OBLIVION_HEADER + data_size
        return _RECORD_HEADER + data_size
    if len(head) >= 16 and head[:4] == b"TES3":
        return 16 + struct.unpack_from("<I", head, 4)[0]
    return None


def _trim(text: str) -> str:
    return text.strip()


def read_plugin_masters(head: bytes) -> list[str] | None:
    """Masters named by the header record at the start of ``head`` (a plugin's first bytes)."""

    if head[:4] == b"TES3":
        return _read_tes3(head)
    if len(head) < _OBLIVION_HEADER + 4 or head[:4] != b"TES4":
        return None
    if head[_OBLIVION_HEADER:_OBLIVION_HEADER + 4] == b"HEDR":
        header_size = _OBLIVION_HEADER
    elif len(head) >= _RECORD_HEADER + 4 and head[_RECORD_HEADER:_RECORD_HEADER + 4] == b"HEDR":
        header_size = _RECORD_HEADER
    else:
        return None
    data_size, flags = struct.unpack_from("<II", head, 4)
    if data_size > MAX_HEADER_DATA or flags & _COMPRESSED:
        return None
    data = head[header_size:header_size + data_size]
    if len(data) != data_size:
        return None

    masters: list[str] = []
    seen: set[str] = set()
    offset = 0
    extended = 0
    while offset + 6 <= len(data):
        kind = data[offset:offset + 4]
        size = struct.unpack_from("<H", data, offset + 4)[0]
        offset += 6
        if extended:
            size, extended = extended, 0
        if offset + size > len(data):
            return None
        if kind == b"XXXX" and size == 4:
            extended = struct.unpack_from("<I", data, offset)[0]
        elif kind == b"MAST" and size > 0:
            name = data[offset:offset + size]
            end = name.find(b"\0")
            if end >= 0:
                name = name[:end]
            # Plugin names are Windows-1252 in practice; Latin-1 keeps ASCII names exact.
            master = _trim(name.decode("latin-1"))
            if master and master.casefold() not in seen:
                seen.add(master.casefold())
                masters.append(master)
        offset += size
    return masters


def _read_tes3(head: bytes) -> list[str] | None:
    """Morrowind: a 16-byte record header, then subrecords of type(4) size(4) data."""

    if len(head) < 16:
        return None
    data_size = struct.unpack_from("<I", head, 4)[0]
    if data_size > MAX_HEADER_DATA:
        return None
    data = head[16:16 + data_size]
    if len(data) != data_size:
        return None
    masters: list[str] = []
    seen: set[str] = set()
    offset = 0
    while offset + 8 <= len(data):
        kind = data[offset:offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        offset += 8
        if offset + size > len(data):
            return None
        if kind == b"MAST" and size > 0:
            name = data[offset:offset + size].split(b"\0", 1)[0]
            master = _trim(name.decode("latin-1"))
            if master and master.casefold() not in seen:
                seen.add(master.casefold())
                masters.append(master)
        offset += size
    return masters
