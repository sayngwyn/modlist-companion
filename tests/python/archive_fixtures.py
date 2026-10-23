"""Builders for small test archives: zip (stdlib), 7z (stored), RAR 4 (stored).

The 7z and RAR writers are minimal on purpose: they store data uncompressed, which is all
the reader tests need (a real decoder still parses the container), and they can produce the
shapes that are hard to get from real tools: one solid block of many files, a fake AES coder,
encrypted headers, split-volume flags.
"""

from __future__ import annotations

import io
import lzma
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Mapping, Sequence

Entry = tuple[str, "bytes | None"]   # (path, data) ; data None means a directory


def make_zip(path: Path, files: Mapping[str, bytes | None], *, raw_names: Sequence[str] = (),
             flag_bits: Mapping[str, int] | None = None, compress: int = zipfile.ZIP_DEFLATED) -> Path:
    """A zip with ``files`` (``None`` data = directory). ``raw_names`` are written verbatim, so
    hostile names such as ``../x`` can be tested; ``flag_bits`` sets header flags per name."""

    with zipfile.ZipFile(path, "w", compress) as archive:
        for name, data in files.items():
            if data is None and not name.endswith("/"):
                name += "/"
            info = zipfile.ZipInfo(name)
            info.compress_type = compress
            if data is None:
                info.external_attr = 0o40755 << 16
                archive.writestr(info, b"")
            else:
                archive.writestr(info, data)
        for name in raw_names:
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            archive.writestr(info, b"payload")
    if flag_bits:
        blob = bytearray(path.read_bytes())
        for name, bits in flag_bits.items():
            encoded = name.encode("utf-8")
            for header, name_offset, flag_offset in ((b"PK\x03\x04", 30, 6), (b"PK\x01\x02", 46, 8)):
                start = 0
                while True:
                    found = blob.find(header, start)
                    if found < 0:
                        break
                    start = found + 4
                    if bytes(blob[found + name_offset: found + name_offset + len(encoded)]) == encoded:
                        flags = struct.unpack_from("<H", blob, found + flag_offset)[0] | bits
                        struct.pack_into("<H", blob, found + flag_offset, flags)
        path.write_bytes(bytes(blob))
    return path


# ------------------------------------------------------------------------------ 7z


def _num(value: int) -> bytes:
    first = 0
    mask = 0x80
    for i in range(8):
        if value < (1 << (7 * (i + 1))):
            first |= value >> (8 * i)
            return bytes([first]) + (value & ((1 << (8 * i)) - 1)).to_bytes(i, "little")
        first |= mask
        mask >>= 1
    return b"\xff" + value.to_bytes(8, "little")


def _bits(flags: Sequence[bool]) -> bytes:
    out = bytearray((len(flags) + 7) // 8)
    for index, flag in enumerate(flags):
        if flag:
            out[index // 8] |= 0x80 >> (index % 8)
    return bytes(out)


_AES_CODER = b"\x24" + b"\x06\xf1\x07\x01" + b"\x01" + b"\x00"   # id size 4 + props, id, props size 1, props
_COPY_CODER = b"\x01\x00"


_LZMA2_CODER = b"\x21\x21\x01\x10"   # id size 1 + props, id 0x21, props size 1, dictionary 1 MiB


def make_7z(path: Path, entries: Sequence[Entry], *, solid: bool = False, aes: bool = False,
            encrypt_header: bool = False, lzma2: bool = False) -> Path:
    """A 7z with the entries stored uncompressed.

    ``solid`` puts every file in one folder (one block); ``aes`` marks the folders with an AES
    coder (the data is not really encrypted, but the reader must ask for a password);
    ``encrypt_header`` hides the header behind an AES coder; ``lzma2`` really compresses each
    folder, so a solid block has to be decoded from its start.
    """

    files = [(name, data) for name, data in entries if data is not None and len(data) > 0]
    streams = [data for _, data in files]
    coder = _AES_CODER if aes else (_LZMA2_CODER if lzma2 else _COPY_CODER)
    folders = ([streams] if solid else [[s] for s in streams]) if files else []
    pack_sizes = []
    pieces = []
    for folder in folders:
        raw = b"".join(folder)
        if lzma2 and not aes:
            raw = lzma.compress(raw, format=lzma.FORMAT_RAW,
                                filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 20}])
        pieces.append(raw)
        pack_sizes.append(len(raw))
    packed = b"".join(pieces)

    header = bytearray(b"\x01")
    if files:
        header += b"\x04"
        header += b"\x06" + _num(0) + _num(len(folders)) + b"\x09"
        for size in pack_sizes:
            header += _num(size)
        header += b"\x00"
        header += b"\x07\x0b" + _num(len(folders)) + b"\x00"
        for _ in folders:
            header += _num(1) + coder
        header += b"\x0c"
        for folder in folders:
            header += _num(sum(len(s) for s in folder))
        header += b"\x00"
        header += b"\x08\x0d"
        for folder in folders:
            header += _num(len(folder))
        sizes = b"".join(_num(len(s)) for folder in folders if len(folder) > 1 for s in folder[:-1])
        if sizes:
            header += b"\x09" + sizes
        header += b"\x0a\x01"
        for stream in streams:
            header += struct.pack("<I", zlib.crc32(stream))
        header += b"\x00\x00"
    if entries:
        header += b"\x05" + _num(len(entries))
        empty = [not (data is not None and len(data) > 0) for _, data in entries]
        if any(empty):
            vector = _bits(empty)
            header += b"\x0e" + _num(len(vector)) + vector
            empty_file = [data is not None for (_, data), is_empty in zip(entries, empty) if is_empty]
            if any(empty_file):
                vector = _bits(empty_file)
                header += b"\x0f" + _num(len(vector)) + vector
        names = b"".join(name.encode("utf-16-le") + b"\x00\x00" for name, _ in entries)
        header += b"\x11" + _num(len(names) + 1) + b"\x00" + names
        header += b"\x00"
    header += b"\x00"
    header = bytes(header)

    next_header = header
    if encrypt_header:
        # EncodedHeader: a streams-info that points at the (fake-encrypted) header bytes.
        encoded = bytearray(b"\x17\x06")
        encoded += _num(len(packed)) + _num(1) + b"\x09" + _num(len(header)) + b"\x00"
        encoded += b"\x07\x0b" + _num(1) + b"\x00" + _num(1) + _AES_CODER
        encoded += b"\x0c" + _num(len(header)) + b"\x00\x00"
        packed = packed + header
        next_header = bytes(encoded)

    start_fields = struct.pack("<QQI", len(packed), len(next_header), zlib.crc32(next_header))
    signature = b"7z\xbc\xaf\x27\x1c" + b"\x00\x04" + struct.pack("<I", zlib.crc32(start_fields)) + start_fields
    path.write_bytes(signature + packed + next_header)
    return path


# ----------------------------------------------------------------------------- RAR 4


def make_rar4(path: Path, entries: Sequence[tuple[str, bytes]], *, solid_flags: bool = False,
              encrypted: bool = False, split_after: bool = False, split_before: bool = False) -> Path:
    """A RAR 3/4 archive with stored files (each header CRC-correct)."""

    def block(kind: int, flags: int, body: bytes, extra: bytes = b"") -> bytes:
        head = struct.pack("<BHH", kind, flags, 7 + len(body)) + body
        crc = zlib.crc32(head) & 0xFFFF
        return struct.pack("<H", crc) + head + extra

    out = bytearray(b"Rar!\x1a\x07\x00")
    out += block(0x73, 0x0008 if solid_flags else 0, b"\x00\x00\x00\x00\x00\x00")
    for index, (name, data) in enumerate(entries):
        encoded = name.encode("ascii")
        flags = 0x8000
        if solid_flags and index > 0:
            flags |= 0x10
        if encrypted:
            flags |= 0x04 | 0x400
        if split_before:
            flags |= 0x01
        if split_after:
            flags |= 0x02
        body = struct.pack("<IIBIIBBHI", len(data), len(data), 2, zlib.crc32(data), 0x5A000000,
                           20, 0x30, len(encoded), 0x20) + encoded
        if encrypted:
            body += b"\x01" * 8
        out += block(0x74, flags, body, data)
    out += bytes.fromhex("c43d7b00400700")
    path.write_bytes(bytes(out))
    return path


def zip_bytes(files: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()
