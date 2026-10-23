"""Read FOMOD archives in memory, safely.

One interface, two backends:

* ``zip``  -> the standard library's ``zipfile``;
* ``7z``, ``Rar`` and ``Rar5`` (and a zip the stdlib cannot open) -> MO2's own
  ``7z.dll`` driven through ``ctypes``.

Nothing is ever written to disk, and no entry name is ever used as a path.

A mistake in the ``ctypes`` layer takes MO2 down with it, so this module is
deliberately defensive:

* every COM interface this module implements has its own vtable, and every
  interface it obtains from the DLL is released exactly once;
* every call into the DLL has its HRESULT checked, and every Python callback
  catches every exception and answers with an error code (an exception that
  escapes a ``ctypes`` callback is swallowed and read as success);
* a password is never asked for and never supplied: the callback refuses, the
  archive is reported as encrypted, and nothing waits for input;
* multi-part archives are refused;
* corrupt and truncated archives fail with an :class:`ArchiveError`;
* decompression is bounded by a byte budget (estimated up front from the solid
  layout, then enforced while it runs) and by a time limit, so a huge solid
  archive is reported as "too big to scan" instead of freezing MO2;
* entry names that would escape a folder (``..``, drive letters, absolute
  paths, NUL bytes, alternate data streams) are dropped from the listing;
* lookup is case-insensitive and ignores the path separator style.

It runs on the dev Python and on MO2's embedded Python 3.12 (``_ctypes`` ships
with both).
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import threading
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "Archive",
    "ArchiveCorruptError",
    "ArchiveEncryptedError",
    "ArchiveEntry",
    "ArchiveEntryNotFound",
    "ArchiveError",
    "ArchiveMultiPartError",
    "ArchiveTimeoutError",
    "ArchiveTooBigError",
    "ArchiveUnsupportedError",
    "Limits",
    "ReadResult",
    "find_sevenzip_dll",
    "normalize_entry_path",
    "open_archive",
]


# --------------------------------------------------------------------------- errors


class ArchiveError(Exception):
    """Base class: the archive cannot be read, for the reason in ``code``."""

    code = "archive_error"


class ArchiveUnsupportedError(ArchiveError):
    code = "unsupported"


class ArchiveCorruptError(ArchiveError):
    """Damaged, truncated or not an archive at all."""

    code = "corrupt"


class ArchiveEncryptedError(ArchiveError):
    code = "encrypted"


class ArchiveMultiPartError(ArchiveError):
    code = "multipart"


class ArchiveTooBigError(ArchiveError):
    """Over a size, count or decompression budget: too big to scan."""

    code = "too_big"


class ArchiveTimeoutError(ArchiveError):
    code = "timeout"


class ArchiveEntryNotFound(ArchiveError):
    code = "not_found"


# -------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Limits:
    """Budgets for one archive. Every one is enforced, none is advisory."""

    max_entry_bytes: int = 8 * 1024 * 1024        # one entry kept in memory
    max_total_bytes: int = 64 * 1024 * 1024       # all entries of one read call
    max_decoded_bytes: int = 1024 * 1024 * 1024   # decompressed, solid prefixes included
    timeout_seconds: float = 20.0                 # one open or one read call
    max_entries: int = 500_000                    # listing size
    cancel: Any = None                            # an object with is_set() (a threading.Event): once set, reads stop


@dataclass(frozen=True)
class ArchiveEntry:
    path: str          # normalised: forward slashes, no leading slash, case kept
    size: int          # unpacked size (0 when the format does not say)
    is_dir: bool
    encrypted: bool
    index: int         # position in the archive's own listing


@dataclass
class ReadResult:
    """What a read produced. ``data`` and ``errors`` are keyed by the requested name."""

    data: dict[str, bytes] = field(default_factory=dict)
    errors: dict[str, ArchiveError] = field(default_factory=dict)
    truncated: set[str] = field(default_factory=set)   # kept only the first bytes

    def get(self, name: str) -> bytes | None:
        return self.data.get(name)


_DRIVE = re.compile(r"^[A-Za-z]:")


def normalize_entry_path(raw: str) -> str | None:
    """The safe, normalised form of an archive entry name, or None to ignore it.

    Ignored: empty names, NUL bytes, absolute paths (a leading slash or a drive
    letter), any ``..`` segment, and ``name:stream`` segments.
    """

    if not raw or "\x00" in raw:
        return None
    text = raw.replace("\\", "/")
    if text.startswith("/") or _DRIVE.match(text):
        return None
    parts: list[str] = []
    for segment in text.split("/"):
        if segment in ("", "."):
            continue
        if segment == ".." or ":" in segment:
            return None
        parts.append(segment)
    return "/".join(parts) or None


def _lookup_key(path: str) -> str:
    return path.replace("\\", "/").strip("/").casefold()


# --------------------------------------------------------------- shared archive base

_MULTIPART_NAME = re.compile(
    r"(\.(7z|zip|rar)\.\d{2,4}$)|(\.z\d{2}$)|(\.r\d{2}$)|(\.\d{3}$)", re.IGNORECASE)
_PART_RAR = re.compile(r"^(?P<stem>.*)\.part(?P<n>\d+)\.rar$", re.IGNORECASE)


def _refuse_multipart_by_name(path: Path) -> None:
    name = path.name
    if _MULTIPART_NAME.search(name):
        raise ArchiveMultiPartError(f"{name} is one part of a multi-part archive")
    lowered = name.lower()
    try:
        siblings = {entry.lower() for entry in os.listdir(path.parent)}
    except OSError:
        siblings = set()
    match = _PART_RAR.match(name)
    if match:
        stem = match.group("stem").lower()
        if any(s != lowered and s.startswith(stem + ".part") and s.endswith(".rar") for s in siblings):
            raise ArchiveMultiPartError(f"{name} is one part of a multi-part archive")
    stem, ext = os.path.splitext(lowered)
    candidates = {
        ".zip": (stem + ".z01",),
        ".rar": (stem + ".r00", stem + ".r01"),
        ".7z": (lowered + ".001",),
    }.get(ext, ())
    for candidate in candidates:
        if candidate in siblings:
            raise ArchiveMultiPartError(f"{name} has further parts ({candidate})")


class Archive:
    """A listed archive. Use :func:`open_archive`; close it, or use ``with``."""

    format = ""      # "zip", "7z", "Rar", "Rar5"
    backend = ""     # "zipfile" or "7z.dll"

    def __init__(self, path: Path, limits: Limits):
        self.path = path
        self.limits = limits
        self.entries: list[ArchiveEntry] = []
        self.skipped_unsafe = 0     # entries ignored for an unsafe name
        self.warnings: list[str] = []
        self._by_key: dict[str, ArchiveEntry] = {}
        self._closed = False
        self._lock = threading.RLock()

    def _cancelled(self) -> bool:
        cancel = self.limits.cancel
        return cancel is not None and cancel.is_set()

    # ---- lookup
    def _index_entries(self, entries: Iterable[ArchiveEntry]) -> None:
        self.entries = list(entries)
        self._by_key = {}
        for entry in self.entries:
            # A later entry with the same name replaces the earlier one, as it would on extraction.
            self._by_key[_lookup_key(entry.path)] = entry

    def find(self, name: str) -> ArchiveEntry | None:
        """The entry called ``name``: case-insensitive, either slash style."""

        return self._by_key.get(_lookup_key(name))

    def files(self) -> list[ArchiveEntry]:
        return [entry for entry in self.entries if not entry.is_dir]

    # ---- reading
    def read(self, names: Sequence[str], *, max_entry_bytes: int | None = None,
             max_total_bytes: int | None = None, truncate: bool = False) -> ReadResult:
        """Read the named entries into memory.

        ``max_entry_bytes`` caps one entry. An entry over the cap is an error, or, with
        ``truncate=True``, is cut to its first ``max_entry_bytes`` (the way a plugin header
        is read from a huge plugin) and listed in ``result.truncated``. ``max_total_bytes``
        caps the bytes kept by this call. A problem with one entry (missing, encrypted,
        corrupt, too big) is recorded in ``result.errors`` and the other entries still come
        back; a problem with the whole archive raises.
        """

        entry_cap = self.limits.max_entry_bytes if max_entry_bytes is None else max_entry_bytes
        total_cap = self.limits.max_total_bytes if max_total_bytes is None else max_total_bytes
        with self._lock:
            if self._closed:
                raise ArchiveError("archive is closed")
            result = ReadResult()
            wanted: dict[str, ArchiveEntry] = {}
            for name in dict.fromkeys(names):
                entry = self.find(name)
                if entry is None or entry.is_dir:
                    result.errors[name] = ArchiveEntryNotFound(f"{name} is not in the archive")
                elif entry.encrypted:
                    result.errors[name] = ArchiveEncryptedError(f"{name} is password protected")
                elif entry.size > entry_cap and not truncate:
                    result.errors[name] = ArchiveTooBigError(
                        f"{name} is {entry.size} bytes, over the {entry_cap} byte limit")
                else:
                    wanted[name] = entry
            if wanted:
                self._read_entries(wanted, entry_cap, total_cap, truncate, result)
            return result

    def _read_entries(self, wanted: dict[str, ArchiveEntry], entry_cap: int, total_cap: int,
                      truncate: bool, result: ReadResult) -> None:
        raise NotImplementedError

    # ---- lifetime
    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._release()

    def _release(self) -> None:
        pass

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - safety net only
        try:
            self.close()
        except Exception:
            pass


# ------------------------------------------------------------------------ zip backend


class _ZipArchive(Archive):
    format = "zip"
    backend = "zipfile"

    def __init__(self, path: Path, limits: Limits, fallback_dll: str | os.PathLike[str] | None = None):
        super().__init__(path, limits)
        self._fallback_dll = fallback_dll
        self._fallback: "_SevenZipArchive | None" = None
        self._zip: zipfile.ZipFile | None = None
        self._infos: dict[int, zipfile.ZipInfo] = {}
        try:
            self._zip = zipfile.ZipFile(path)
            infos = self._zip.infolist()
            if len(infos) > limits.max_entries:
                raise ArchiveTooBigError(f"{len(infos)} entries is over the {limits.max_entries} limit")
            entries = []
            for index, info in enumerate(infos):
                normalised = normalize_entry_path(info.filename)
                if normalised is None:
                    self.skipped_unsafe += 1
                    continue
                self._infos[index] = info
                entries.append(ArchiveEntry(normalised, info.file_size, info.is_dir(),
                                            bool(info.flag_bits & 0x1), index))
            self._index_entries(entries)
        except ArchiveError:
            self._release()
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, zlib.error, struct.error) as exc:
            self._release()
            raise ArchiveCorruptError(f"not a readable zip file: {exc}") from exc
        except OSError as exc:
            self._release()
            raise ArchiveCorruptError(f"cannot read the archive: {exc}") from exc
        except Exception as exc:  # defensive: never let a library surprise escape unchecked
            self._release()
            raise ArchiveCorruptError(f"not a readable zip file: {exc}") from exc

    def _read_entries(self, wanted, entry_cap, total_cap, truncate, result) -> None:
        assert self._zip is not None
        deadline = time.monotonic() + self.limits.timeout_seconds
        kept_total = 0
        for name, entry in wanted.items():
            if time.monotonic() > deadline or self._cancelled():
                result.errors[name] = ArchiveTimeoutError("took too long to read")
                continue
            if kept_total >= total_cap and entry.size > 0:
                result.errors[name] = ArchiveTooBigError("over the total read budget")
                continue
            info = self._infos[entry.index]
            try:
                with self._zip.open(info) as handle:
                    chunks: list[bytes] = []
                    kept = 0
                    over = False
                    while True:
                        chunk = handle.read(min(1 << 20, entry_cap + 1 - kept))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        kept += len(chunk)
                        if kept > entry_cap:
                            over = True
                            break
                        if time.monotonic() > deadline or self._cancelled():
                            raise ArchiveTimeoutError("took too long to read")
                data = b"".join(chunks)
            except ArchiveError as exc:
                result.errors[name] = exc
                continue
            except NotImplementedError as exc:
                result.errors[name] = ArchiveUnsupportedError(f"{name}: {exc}")
                continue
            except RuntimeError as exc:   # zipfile: "password required"
                result.errors[name] = ArchiveEncryptedError(f"{name}: {exc}")
                continue
            except (zipfile.BadZipFile, EOFError, zlib.error, OSError, ValueError) as exc:
                result.errors[name] = ArchiveCorruptError(f"{name}: damaged or truncated ({exc})")
                continue
            if over:
                if not truncate:
                    result.errors[name] = ArchiveTooBigError(
                        f"{name} is over the {entry_cap} byte limit")
                    continue
                data = data[:entry_cap]
                result.truncated.add(name)
            if kept_total + len(data) > total_cap:
                result.errors[name] = ArchiveTooBigError("over the total read budget")
                continue
            kept_total += len(data)
            result.data[name] = data
        self._retry_with_sevenzip(wanted, entry_cap, total_cap, truncate, result)

    def _retry_with_sevenzip(self, wanted, entry_cap, total_cap, truncate, result) -> None:
        """Entries zipfile cannot decode (Deflate64, PPMd, ...) are tried with MO2's 7z.dll."""

        names = [name for name, error in result.errors.items()
                 if name in wanted and isinstance(error, ArchiveUnsupportedError)]
        if not names or self._fallback_dll is None:
            return
        try:
            if self._fallback is None:
                self._fallback = _SevenZipArchive(self.path, self.limits, SevenZipLibrary.load(self._fallback_dll),
                                                  ("zip",), True)
            again = self._fallback.read(names, max_entry_bytes=entry_cap, max_total_bytes=total_cap,
                                        truncate=truncate)
        except ArchiveError:
            return
        for name in names:
            if name in again.data:
                result.data[name] = again.data[name]
                del result.errors[name]
                if name in again.truncated:
                    result.truncated.add(name)
            elif name in again.errors:
                result.errors[name] = again.errors[name]

    def _release(self) -> None:
        fallback, self._fallback = self._fallback, None
        try:
            if fallback is not None:
                fallback.close()
        finally:
            if self._zip is not None:
                try:
                    self._zip.close()
                finally:
                    self._zip = None


# ------------------------------------------------------------------- 7z.dll plumbing

HRESULT = ctypes.c_int32
S_OK = 0
S_FALSE = 1
E_ABORT = -2147467260        # 0x80004004
E_FAIL = -2147467259         # 0x80004005
E_NOINTERFACE = -2147467262  # 0x80004002
E_POINTER = -2147467261      # 0x80004003
E_INVALIDARG = -2147024809   # 0x80070057

_PROTO = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

# 7-Zip's PROPID values (CPP/7zip/PropID.h) and handler property ids (IArchive.h).
kpidPath, kpidIsDir, kpidSize, kpidSolid = 3, 6, 7, 13
kpidEncrypted, kpidSplitBefore, kpidSplitAfter = 15, 16, 17
kpidBlock = 27
kpidIsAnti = 21
kpidErrorFlags = 71
kpidIsNotArcType = 84
kHandlerName, kHandlerClassId = 0, 1

# kpidErrorFlags bits (kpv_ErrorFlags_*)
_ERROR_FLAGS = (
    (1 << 0, "not an archive"),
    (1 << 1, "damaged headers"),
    (1 << 2, "damaged encrypted headers"),
    (1 << 3, "start of the archive is missing"),
    (1 << 4, "start of the archive is unconfirmed"),
    (1 << 5, "archive ends early (truncated)"),
    (1 << 6, "data after the end of the archive"),
    (1 << 7, "unsupported method"),
    (1 << 8, "unsupported feature"),
    (1 << 9, "data error"),
    (1 << 10, "checksum error"),
)

# askExtractMode / operation results
kExtract = 0
_OP_RESULT = {
    0: None,
    1: "unsupported compression method",
    2: "data error",
    3: "checksum error",
    4: "data unavailable (volume missing?)",
    5: "unexpected end of data (truncated)",
    6: None,                       # data after the end: the entry itself decoded fine
    7: "not an archive",
    8: "damaged headers",
    9: "wrong or missing password",
}


def _iid(group: int, ident: int) -> bytes:
    return uuid.UUID(f"23170F69-40C1-278A-0000-00{group:02X}00{ident:02X}0000").bytes_le


IID_IUnknown = uuid.UUID("00000000-0000-0000-C000-000000000046").bytes_le
IID_ISequentialInStream = _iid(3, 1)
IID_ISequentialOutStream = _iid(3, 2)
IID_IInStream = _iid(3, 3)
IID_IProgress = _iid(0, 5)
IID_ICryptoGetTextPassword = _iid(5, 0x10)
IID_IArchiveOpenCallback = _iid(6, 0x10)
IID_IArchiveExtractCallback = _iid(6, 0x20)
IID_IInArchive = _iid(6, 0x60)


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_uint16), ("r1", ctypes.c_uint16), ("r2", ctypes.c_uint16),
                ("r3", ctypes.c_uint16), ("val", ctypes.c_uint64), ("val2", ctypes.c_uint64)]


_VT_I2, _VT_I4, _VT_BSTR, _VT_BOOL = 2, 3, 8, 11
_VT_UI1, _VT_UI2, _VT_UI4, _VT_I8, _VT_UI8 = 17, 18, 19, 20, 21


def _pv_value(pv: PROPVARIANT):
    vt = pv.vt
    if vt == _VT_BSTR:
        return ctypes.wstring_at(pv.val) if pv.val else ""
    if vt == _VT_BOOL:
        return (pv.val & 0xFFFF) != 0
    if vt in (_VT_UI8, _VT_I8):
        return pv.val if vt == _VT_UI8 else ctypes.c_int64(pv.val).value
    if vt == _VT_UI4:
        return pv.val & 0xFFFFFFFF
    if vt == _VT_I4:
        return ctypes.c_int32(pv.val & 0xFFFFFFFF).value
    if vt in (_VT_UI2, _VT_UI1):
        return pv.val & (0xFFFF if vt == _VT_UI2 else 0xFF)
    if vt == _VT_I2:
        return ctypes.c_int16(pv.val & 0xFFFF).value
    return None


def _pv_binary_bstr(pv: PROPVARIANT) -> bytes:
    """A BSTR holding raw bytes (the handler class id): its length sits just before it."""

    if pv.vt != _VT_BSTR or not pv.val:
        return b""
    length = ctypes.c_uint32.from_address(pv.val - 4).value
    if length > 4096:
        return b""
    return ctypes.string_at(pv.val, length)


_ole32 = None


def _prop_clear(pv: PROPVARIANT) -> None:
    global _ole32
    if _ole32 is None:
        _ole32 = ctypes.WinDLL("ole32")
        _ole32.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
        _ole32.PropVariantClear.restype = HRESULT
    _ole32.PropVariantClear(ctypes.byref(pv))


class _Context:
    """Shared by the callbacks of one open or read: deadline, budgets, why we stopped."""

    def __init__(self, timeout: float, max_decoded: int, cancel: Any = None):
        self.deadline = time.monotonic() + timeout
        self.cancel = cancel
        self.max_decoded = max_decoded
        self.decoded = 0               # bytes the decoder handed to our output streams
        self.abort_reason: str | None = None   # "timeout" | "budget"
        self.password_requested = False
        self.callback_error: BaseException | None = None

    def expired(self) -> bool:
        if self.abort_reason is None and (time.monotonic() > self.deadline
                                          or (self.cancel is not None and self.cancel.is_set())):
            self.abort_reason = "timeout"      # (a cancelled read is stopped the way a late one is)
        return self.abort_reason is not None


class _ComServer:
    """A COM object made of one or more interfaces, each with its own vtable.

    Reference counts are tracked but nothing is freed on release: Python owns the memory
    and keeps this object alive for as long as the DLL could still call it. Methods that
    raise are caught and answer ``E_FAIL``.
    """

    def __init__(self, ctx: _Context):
        self.ctx = ctx
        self._refs = 1
        self._lock = threading.Lock()
        self._interfaces: list[_Interface] = []

    # IUnknown, shared by every interface of the server
    def add_ref(self) -> int:
        with self._lock:
            self._refs += 1
            return self._refs

    def release(self) -> int:
        with self._lock:
            self._refs = max(0, self._refs - 1)
            return self._refs

    def query(self, riid: bytes) -> int | None:
        for interface in self._interfaces:
            if riid in interface.iids:
                self.add_ref()
                return interface.pointer
        if riid == IID_IUnknown and self._interfaces:
            self.add_ref()
            return self._interfaces[0].pointer
        return None

    def interface(self, iids: Sequence[bytes], methods: Sequence[tuple[type, Callable]]) -> "_Interface":
        interface = _Interface(self, iids, methods)
        self._interfaces.append(interface)
        return interface

    @property
    def pointer(self) -> int:
        return self._interfaces[0].pointer

    def guard(self, function: Callable, default: int = E_FAIL) -> Callable:
        def call(*args):
            try:
                return function(*args)
            except BaseException as exc:   # noqa: BLE001 - nothing may escape into the DLL
                if self.ctx.callback_error is None:
                    self.ctx.callback_error = exc
                return default
        return call


class _Interface:
    def __init__(self, server: _ComServer, iids: Sequence[bytes],
                 methods: Sequence[tuple[type, Callable]]):
        self.server = server
        self.iids = frozenset(iids) | {IID_IUnknown}
        qi_type = _PROTO(HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        count_type = _PROTO(ctypes.c_uint32, ctypes.c_void_p)
        self._functions = [
            qi_type(server.guard(self._query)),
            count_type(server.guard(self._add_ref, 1)),
            count_type(server.guard(self._release, 1)),
        ]
        for prototype, function in methods:
            self._functions.append(prototype(server.guard(function)))
        self._vtable = (ctypes.c_void_p * len(self._functions))(
            *[ctypes.cast(fn, ctypes.c_void_p) for fn in self._functions])
        self._object = (ctypes.c_void_p * 1)(ctypes.addressof(self._vtable))
        self.pointer = ctypes.addressof(self._object)

    def _query(self, this, riid, out):
        if not out:
            return E_POINTER
        if not riid:
            out[0] = None
            return E_INVALIDARG
        pointer = self.server.query(ctypes.string_at(riid, 16))
        if pointer is None:
            out[0] = None
            return E_NOINTERFACE
        out[0] = pointer
        return S_OK

    def _add_ref(self, this):
        return self.server.add_ref()

    def _release(self, this):
        return self.server.release()


_READ_PROTO = _PROTO(HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                     ctypes.POINTER(ctypes.c_uint32))
_SEEK_PROTO = _PROTO(HRESULT, ctypes.c_void_p, ctypes.c_int64, ctypes.c_uint32,
                     ctypes.POINTER(ctypes.c_uint64))
_WRITE_PROTO = _READ_PROTO
_TWO_PTR_PROTO = _PROTO(HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_SET_TOTAL_PROTO = _PROTO(HRESULT, ctypes.c_void_p, ctypes.c_uint64)
_SET_COMPLETED_PROTO = _PROTO(HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64))
_GET_STREAM_PROTO = _PROTO(HRESULT, ctypes.c_void_p, ctypes.c_uint32,
                           ctypes.POINTER(ctypes.c_void_p), ctypes.c_int32)
_INT_PROTO = _PROTO(HRESULT, ctypes.c_void_p, ctypes.c_int32)
_PASSWORD_PROTO = _PROTO(HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_MAX_READ = 8 * 1024 * 1024


class _InStream(_ComServer):
    """IInStream over a Python file: the DLL reads the archive through this."""

    def __init__(self, ctx: _Context, path: Path):
        super().__init__(ctx)
        self.file = open(path, "rb")
        self._io = threading.Lock()   # the DLL may read from more than one thread
        self.interface([IID_ISequentialInStream, IID_IInStream],
                       [(_READ_PROTO, self._read), (_SEEK_PROTO, self._seek)])

    def _read(self, this, data, size, processed):
        if processed:
            processed[0] = 0
        if self.ctx.expired():
            return E_ABORT
        if size == 0:
            return S_OK
        with self._io:
            buffer = self.file.read(min(size, _MAX_READ))
        if buffer:
            ctypes.memmove(data, buffer, len(buffer))
        if processed:
            processed[0] = len(buffer)
        return S_OK

    def _seek(self, this, offset, origin, new_position):
        if origin not in (0, 1, 2):
            return E_INVALIDARG
        try:
            with self._io:
                position = self.file.seek(offset, origin)
        except (OSError, ValueError):
            return E_FAIL
        if new_position:
            new_position[0] = position
        return S_OK

    def close(self) -> None:
        try:
            self.file.close()
        except OSError:
            pass


class _OpenCallback(_ComServer):
    """IArchiveOpenCallback. It never supplies a password; it never offers further volumes."""

    def __init__(self, ctx: _Context):
        super().__init__(ctx)
        self.interface([IID_IArchiveOpenCallback],
                       [(_TWO_PTR_PROTO, self._progress), (_TWO_PTR_PROTO, self._progress)])
        self.interface([IID_ICryptoGetTextPassword], [(_PASSWORD_PROTO, self._password)])

    def _progress(self, this, files, size):
        return E_ABORT if self.ctx.expired() else S_OK

    def _password(self, this, password):
        self.ctx.password_requested = True
        return E_ABORT


class _EntrySink(_ComServer):
    """ISequentialOutStream collecting one entry into memory, within its caps."""

    def __init__(self, ctx: _Context, owner: "_ExtractCallback", index: int, limit: int, truncate: bool):
        super().__init__(ctx)
        self.owner = owner
        self.index = index
        self.limit = limit
        self.truncate = truncate
        self.chunks: list[bytes] = []
        self.kept = 0
        self.written = 0
        self.over_entry = False     # more than the cap, and not allowed to truncate
        self.over_total = False
        self.cut = False            # truncated to the cap
        self.interface([IID_ISequentialOutStream], [(_WRITE_PROTO, self._write)])

    def _write(self, this, data, size, processed):
        if processed:
            processed[0] = 0
        ctx = self.ctx
        if ctx.expired():
            return E_ABORT
        ctx.decoded += size
        if ctx.decoded > ctx.max_decoded:
            ctx.abort_reason = "budget"
            return E_ABORT
        self.written += size
        if not (self.over_entry or self.over_total) and size:
            room = self.limit - self.kept
            take = min(size, max(room, 0))
            if take < size:
                if self.truncate:
                    self.cut = True
                else:
                    self.over_entry = True
            if not self.over_entry and take:
                if self.owner.total_kept + take > self.owner.total_cap:
                    self.over_total = True
                else:
                    self.chunks.append(ctypes.string_at(data, take))
                    self.kept += take
                    self.owner.total_kept += take
            if self.over_entry or self.over_total:
                self.owner.total_kept -= self.kept
                self.chunks = []
                self.kept = 0
        if processed:
            processed[0] = size
        return S_OK

    def data(self) -> bytes:
        return b"".join(self.chunks)


class _ExtractCallback(_ComServer):
    """IArchiveExtractCallback for a handful of entries."""

    def __init__(self, ctx: _Context, wanted: Mapping[int, tuple[int, bool]], total_cap: int):
        super().__init__(ctx)
        self.wanted = dict(wanted)           # index -> (cap, truncate)
        self.total_cap = total_cap
        self.total_kept = 0
        self.sinks: dict[int, _EntrySink] = {}
        self.results: dict[int, int] = {}
        self._current: int | None = None
        self.max_completed = 0
        self.interface(
            [IID_IProgress, IID_IArchiveExtractCallback],
            [(_SET_TOTAL_PROTO, self._set_total), (_SET_COMPLETED_PROTO, self._set_completed),
             (_GET_STREAM_PROTO, self._get_stream), (_INT_PROTO, self._prepare),
             (_INT_PROTO, self._set_result)])
        self.interface([IID_ICryptoGetTextPassword], [(_PASSWORD_PROTO, self._password)])

    def _set_total(self, this, total):
        return E_ABORT if self.ctx.expired() else S_OK

    def _set_completed(self, this, completed):
        if completed:
            # Unpacked bytes the decoder has been through, skipped solid-block data included.
            self.max_completed = max(self.max_completed, completed[0])
            if self.max_completed > self.ctx.max_decoded and self.ctx.abort_reason is None:
                self.ctx.abort_reason = "budget"
        return E_ABORT if self.ctx.expired() else S_OK

    def _get_stream(self, this, index, out, mode):
        if not out:
            return E_POINTER
        out[0] = None
        if self.ctx.expired():
            return E_ABORT
        self._current = index
        if mode == kExtract and index in self.wanted:
            cap, truncate = self.wanted[index]
            sink = _EntrySink(self.ctx, self, index, cap, truncate)
            self.sinks[index] = sink
            sink.add_ref()           # the reference we hand to the caller
            out[0] = sink.pointer
        return S_OK

    def _prepare(self, this, mode):
        return E_ABORT if self.ctx.expired() else S_OK

    def _set_result(self, this, result):
        if self._current is not None:
            self.results[self._current] = result
        return S_OK

    def _password(self, this, password):
        self.ctx.password_requested = True
        return E_ABORT


# vtable slots of IInArchive: IUnknown (0-2), Open, Close, GetNumberOfItems, GetProperty,
# Extract, GetArchiveProperty
_SLOT_RELEASE = 2
_SLOT_OPEN, _SLOT_CLOSE, _SLOT_COUNT, _SLOT_PROPERTY, _SLOT_EXTRACT, _SLOT_ARCHIVE_PROPERTY = 3, 4, 5, 6, 7, 8


class _InArchive:
    """Typed calls on a raw IInArchive pointer. ``release`` must be called exactly once."""

    def __init__(self, pointer: int):
        self.pointer = pointer
        table = ctypes.cast(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0],
                            ctypes.POINTER(ctypes.c_void_p))

        def method(slot: int, *arg_types):
            return _PROTO(HRESULT, ctypes.c_void_p, *arg_types)(table[slot])

        self._release_fn = _PROTO(ctypes.c_uint32, ctypes.c_void_p)(table[_SLOT_RELEASE])
        self._open = method(_SLOT_OPEN, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64), ctypes.c_void_p)
        self._close = method(_SLOT_CLOSE)
        self._count = method(_SLOT_COUNT, ctypes.POINTER(ctypes.c_uint32))
        self._property = method(_SLOT_PROPERTY, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.POINTER(PROPVARIANT))
        self._extract = method(_SLOT_EXTRACT, ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
                               ctypes.c_int32, ctypes.c_void_p)
        self._archive_property = method(_SLOT_ARCHIVE_PROPERTY, ctypes.c_uint32,
                                        ctypes.POINTER(PROPVARIANT))
        self.opened = False
        self.released = False

    def open(self, stream: _InStream, callback: _OpenCallback, max_start: int) -> int:
        limit = ctypes.c_uint64(max_start)
        hr = self._open(self.pointer, stream.pointer, ctypes.byref(limit), callback.pointer)
        self.opened = hr == S_OK
        return hr

    def count(self) -> int:
        number = ctypes.c_uint32()
        hr = self._count(self.pointer, ctypes.byref(number))
        if hr != S_OK:
            raise ArchiveCorruptError(f"7z.dll could not count the entries (0x{hr & 0xFFFFFFFF:08X})")
        return number.value

    def item_property(self, index: int, prop_id: int):
        pv = PROPVARIANT()
        try:
            hr = self._property(self.pointer, index, prop_id, ctypes.byref(pv))
            if hr != S_OK:
                return None
            return _pv_value(pv)
        finally:
            _prop_clear(pv)

    def archive_property(self, prop_id: int):
        pv = PROPVARIANT()
        try:
            hr = self._archive_property(self.pointer, prop_id, ctypes.byref(pv))
            if hr != S_OK:
                return None
            return _pv_value(pv)
        finally:
            _prop_clear(pv)

    def extract(self, indices: Sequence[int], callback: _ExtractCallback) -> int:
        array = (ctypes.c_uint32 * len(indices))(*indices)
        return self._extract(self.pointer, array, len(indices), 0, callback.pointer)

    def close(self) -> None:
        if self.opened:
            self._close(self.pointer)
            self.opened = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self._release_fn(self.pointer)


_LIBRARIES: dict[str, "SevenZipLibrary"] = {}
_LIBRARY_LOCK = threading.Lock()


def find_sevenzip_dll(mo2_folder: str | os.PathLike[str]) -> Path | None:
    """MO2's own ``dlls\\7z.dll`` (or one beside ModOrganizer.exe), if there is one."""

    root = Path(mo2_folder)
    for candidate in (root / "dlls" / "7z.dll", root / "7z.dll"):
        if candidate.is_file():
            return candidate
    return None


class SevenZipLibrary:
    """One loaded 7z.dll and its archive handlers."""

    def __init__(self, dll_path: Path):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise ArchiveUnsupportedError("the 7z.dll reader needs 64-bit Python")
        if not hasattr(ctypes, "WinDLL"):
            raise ArchiveUnsupportedError("the 7z.dll reader is Windows only")
        try:
            self.dll = ctypes.WinDLL(str(dll_path))
            self.dll.GetNumberOfFormats.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
            self.dll.GetNumberOfFormats.restype = HRESULT
            self.dll.GetHandlerProperty2.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                                     ctypes.POINTER(PROPVARIANT)]
            self.dll.GetHandlerProperty2.restype = HRESULT
            self.dll.CreateObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
            self.dll.CreateObject.restype = HRESULT
        except (OSError, AttributeError) as exc:
            raise ArchiveUnsupportedError(f"{dll_path} is not a usable 7-Zip library: {exc}") from exc
        self.path = dll_path
        self.handlers: dict[str, bytes] = {}
        count = ctypes.c_uint32()
        if self.dll.GetNumberOfFormats(ctypes.byref(count)) != S_OK or count.value > 1000:
            raise ArchiveUnsupportedError(f"{dll_path} did not list its archive formats")
        for position in range(count.value):
            name, clsid = PROPVARIANT(), PROPVARIANT()
            try:
                if (self.dll.GetHandlerProperty2(position, kHandlerName, ctypes.byref(name)) == S_OK and
                        self.dll.GetHandlerProperty2(position, kHandlerClassId, ctypes.byref(clsid)) == S_OK):
                    label = _pv_value(name)
                    guid = _pv_binary_bstr(clsid)
                    if isinstance(label, str) and len(guid) == 16:
                        self.handlers[label] = guid
            finally:
                _prop_clear(name)
                _prop_clear(clsid)

    @classmethod
    def load(cls, dll_path: str | os.PathLike[str]) -> "SevenZipLibrary":
        path = Path(dll_path)
        if not path.is_file():
            raise ArchiveUnsupportedError(f"7z.dll not found at {path}")
        key = str(path.resolve()).casefold()
        with _LIBRARY_LOCK:
            library = _LIBRARIES.get(key)
            if library is None:
                library = _LIBRARIES[key] = cls(path)
            return library

    def create_in_archive(self, handler: str) -> _InArchive:
        guid = self.handlers.get(handler)
        if guid is None:
            raise ArchiveUnsupportedError(f"this 7z.dll has no {handler} reader")
        clsid = ctypes.create_string_buffer(guid, 16)
        iid = ctypes.create_string_buffer(IID_IInArchive, 16)
        pointer = ctypes.c_void_p()
        hr = self.dll.CreateObject(clsid, iid, ctypes.byref(pointer))
        if hr != S_OK or not pointer.value:
            raise ArchiveUnsupportedError(f"7z.dll could not create a {handler} reader (0x{hr & 0xFFFFFFFF:08X})")
        return _InArchive(pointer.value)


_SIGNATURES = (
    (b"7z\xbc\xaf\x27\x1c", ("7z",)),
    (b"Rar!\x1a\x07\x01\x00", ("Rar5",)),
    (b"Rar!\x1a\x07\x00", ("Rar",)),
    (b"PK\x03\x04", ("zip",)),
    (b"PK\x05\x06", ("zip",)),
    (b"PK\x07\x08", ("zip",)),
)
_BY_EXTENSION = {".7z": ("7z",), ".rar": ("Rar5", "Rar"), ".zip": ("zip",)}


def _sniff(path: Path) -> tuple[tuple[str, ...], bool]:
    """(handler names to try, True when the signature matched at the very start)."""

    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError as exc:
        raise ArchiveCorruptError(f"cannot read the archive: {exc}") from exc
    for signature, handlers in _SIGNATURES:
        if head.startswith(signature):
            return handlers, True
    return _BY_EXTENSION.get(path.suffix.lower(), ()), False


class _SevenZipArchive(Archive):
    backend = "7z.dll"

    def __init__(self, path: Path, limits: Limits, library: SevenZipLibrary,
                 handlers: Sequence[str], exact: bool):
        super().__init__(path, limits)
        self._library = library
        self._arc: _InArchive | None = None
        self._stream: _InStream | None = None
        self._open_callback: _OpenCallback | None = None
        self._solid: list[bool | None] = []
        self._block: list[int | None] = []
        problems: list[str] = []
        try:
            for handler in handlers:
                try:
                    self._open_with(handler, exact)
                    self.format = handler
                    break
                except (ArchiveEncryptedError, ArchiveTimeoutError, ArchiveTooBigError):
                    raise
                except ArchiveError as exc:
                    problems.append(f"{handler}: {exc}")
                    self._teardown()
            else:
                detail = "; ".join(problems) or "unrecognised archive format"
                raise ArchiveCorruptError(detail)
            self._list_entries()
        except BaseException:
            self._release()
            raise

    # ---- open
    def _open_with(self, handler: str, exact: bool) -> None:
        ctx = _Context(self.limits.timeout_seconds, self.limits.max_decoded_bytes, self.limits.cancel)
        arc = self._library.create_in_archive(handler)
        self._arc = arc
        try:
            self._stream = _InStream(ctx, self.path)
        except OSError as exc:
            raise ArchiveCorruptError(f"cannot read the archive: {exc}") from exc
        self._open_callback = _OpenCallback(ctx)
        hr = arc.open(self._stream, self._open_callback, 0 if exact else 1 << 20)
        if ctx.password_requested:
            raise ArchiveEncryptedError("the archive's headers are password protected")
        if ctx.abort_reason == "timeout":
            raise ArchiveTimeoutError("took too long to open")
        if hr != S_OK:
            raise ArchiveCorruptError(f"7z.dll could not open it as {handler} (0x{hr & 0xFFFFFFFF:08X})")

    def _teardown(self) -> None:
        # Everything the DLL may still call while it closes stays referenced until it is released.
        arc, stream, callback = self._arc, self._stream, self._open_callback
        self._arc = self._stream = self._open_callback = None
        if arc is not None:
            try:
                arc.close()
            finally:
                arc.release()
        if stream is not None:
            stream.close()
        del callback

    # ---- listing
    def _list_entries(self) -> None:
        arc = self._arc
        assert arc is not None
        flags = arc.archive_property(kpidErrorFlags)
        if isinstance(flags, int):
            for bit, label in _ERROR_FLAGS:
                if flags & bit:
                    self.warnings.append(label)
            if flags & 0b100:
                raise ArchiveEncryptedError("the archive's headers are password protected")
            if flags & 0b1:
                raise ArchiveCorruptError("not an archive")
        count = arc.count()
        if count > self.limits.max_entries:
            raise ArchiveTooBigError(f"{count} entries is over the {self.limits.max_entries} limit")
        deadline = time.monotonic() + self.limits.timeout_seconds
        entries: list[ArchiveEntry] = []
        self._solid = [None] * count
        self._block = [None] * count
        multipart = False
        for index in range(count):
            if index % 2048 == 0 and (time.monotonic() > deadline or self._cancelled()):
                raise ArchiveTimeoutError("took too long to list")
            raw = arc.item_property(index, kpidPath)
            if arc.item_property(index, kpidSplitBefore) or arc.item_property(index, kpidSplitAfter):
                multipart = True
            solid = arc.item_property(index, kpidSolid)
            block = arc.item_property(index, kpidBlock)
            self._solid[index] = solid if isinstance(solid, bool) else None
            self._block[index] = block if isinstance(block, int) and not isinstance(block, bool) else None
            if not isinstance(raw, str):
                self.skipped_unsafe += 1
                continue
            normalised = normalize_entry_path(raw)
            if normalised is None:
                self.skipped_unsafe += 1
                continue
            size = arc.item_property(index, kpidSize)
            entries.append(ArchiveEntry(
                normalised,
                size if isinstance(size, int) and not isinstance(size, bool) else 0,
                bool(arc.item_property(index, kpidIsDir)),
                bool(arc.item_property(index, kpidEncrypted)),
                index))
        if multipart:
            raise ArchiveMultiPartError("the archive continues in another part")
        self._index_entries(entries)

    # ---- cost of decoding
    def _decode_costs(self) -> dict[int, tuple[object, int]]:
        """For each file index: (decode group, bytes to decompress to reach and finish it)."""

        sizes = {entry.index: entry.size for entry in self.entries if not entry.is_dir}
        costs: dict[int, tuple[object, int]] = {}
        running: dict[object, int] = {}
        chain = -1
        for index in sorted(sizes):
            size = sizes[index]
            if self.format == "7z":
                block = self._block[index]
                group: object = ("block", block) if block is not None else ("single", index)
            elif self.format in ("Rar", "Rar5"):
                # An unknown solid flag counts as solid: the safe guess for a budget.
                if self._solid[index] is False:
                    chain = index
                group = ("chain", chain)
            else:
                group = ("single", index)
            running[group] = running.get(group, 0) + size
            costs[index] = (group, running[group])
        return costs

    # ---- reading
    def _read_entries(self, wanted, entry_cap, total_cap, truncate, result) -> None:
        arc = self._arc
        assert arc is not None
        costs = self._decode_costs()
        budget = self.limits.max_decoded_bytes
        chosen: dict[str, ArchiveEntry] = {}
        group_cost: dict[object, int] = {}
        total_cost = 0
        for name, entry in sorted(wanted.items(), key=lambda item: costs.get(item[1].index, (0, 0))[1]):
            group, cost = costs.get(entry.index, (("single", entry.index), entry.size))
            added = max(0, cost - group_cost.get(group, 0))
            if total_cost + added > budget:
                result.errors[name] = ArchiveTooBigError(
                    f"too big to scan: reaching {name} means decompressing about {cost} bytes")
                continue
            group_cost[group] = max(group_cost.get(group, 0), cost)
            total_cost += added
            chosen[name] = entry
        if not chosen:
            return

        indices = sorted({entry.index for entry in chosen.values()})
        per_index = {entry.index: (entry_cap, truncate) for entry in chosen.values()}
        ctx = _Context(self.limits.timeout_seconds, budget, self.limits.cancel)
        callback = _ExtractCallback(ctx, per_index, total_cap)
        assert self._stream is not None
        self._stream.ctx = ctx      # the archive stream answers to this read's deadline, not the open's
        try:
            hr = arc.extract(indices, callback)
        finally:
            self._stream.ctx = _Context(self.limits.timeout_seconds, budget, self.limits.cancel)

        if ctx.password_requested:
            for name in chosen:
                result.errors[name] = ArchiveEncryptedError(f"{name} is password protected")
            return
        aborted: ArchiveError | None = None
        if ctx.abort_reason == "timeout":
            aborted = ArchiveTimeoutError("took too long to read")
        elif ctx.abort_reason == "budget":
            aborted = ArchiveTooBigError("too big to scan: over the decompression budget")
        elif hr not in (S_OK, S_FALSE):
            if ctx.callback_error is not None:
                raise ArchiveError(f"internal error while reading: {ctx.callback_error!r}")
            aborted = ArchiveCorruptError(f"7z.dll stopped reading (0x{hr & 0xFFFFFFFF:08X})")

        for name, entry in chosen.items():
            sink = callback.sinks.get(entry.index)
            outcome = callback.results.get(entry.index)
            if sink is None or outcome is None:
                result.errors[name] = aborted or ArchiveCorruptError(f"{name} could not be read")
                continue
            problem = _OP_RESULT.get(outcome, f"unknown error {outcome}")
            if outcome == 9:
                result.errors[name] = ArchiveEncryptedError(f"{name} is password protected")
            elif outcome == 1:
                result.errors[name] = ArchiveUnsupportedError(f"{name}: {problem}")
            elif problem is not None:
                result.errors[name] = ArchiveCorruptError(f"{name}: {problem}")
            elif sink.over_entry:
                result.errors[name] = ArchiveTooBigError(f"{name} is over the {entry_cap} byte limit")
            elif sink.over_total:
                result.errors[name] = ArchiveTooBigError("over the total read budget")
            else:
                result.data[name] = sink.data()
                if sink.cut:
                    result.truncated.add(name)

    def _release(self) -> None:
        self._teardown()


def open_archive(path: str | os.PathLike[str], *, sevenzip_dll: str | os.PathLike[str] | None = None,
                 limits: Limits | None = None) -> Archive:
    """Open and list an archive (zip, 7z, Rar, Rar5).

    ``sevenzip_dll`` is MO2's ``dlls\\7z.dll`` (see :func:`find_sevenzip_dll`); zip files
    open without it. Raises an :class:`ArchiveError` subclass for every failure.
    """

    limits = limits or Limits()
    target = Path(path)
    if not target.is_file():
        raise ArchiveCorruptError(f"{target} is not a file")
    _refuse_multipart_by_name(target)
    handlers, exact = _sniff(target)
    if not handlers:
        raise ArchiveUnsupportedError(f"{target.name} is not a 7z, Rar or zip archive")

    if handlers == ("zip",):
        try:
            return _ZipArchive(target, limits, sevenzip_dll)
        except ArchiveCorruptError as zip_error:
            if sevenzip_dll is None:
                raise
            try:
                return _SevenZipArchive(target, limits, SevenZipLibrary.load(sevenzip_dll), handlers, exact)
            except ArchiveError:
                raise zip_error from None
    if sevenzip_dll is None:
        raise ArchiveUnsupportedError(f"{target.name} needs MO2's 7z.dll, which was not given")
    return _SevenZipArchive(target, limits, SevenZipLibrary.load(sevenzip_dll), handlers, exact)
