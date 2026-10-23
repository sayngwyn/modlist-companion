"""Client for the ModLabs MO2 bridge (``ModLabsBridge.py``, protocol 2). Test-only; never shipped.

The bridge is a test helper that lives in the test MO2's ``plugins`` folder for the length of a run. Its module
docstring is the protocol; this client follows it and nothing else:

* one connection per request: write one JSON line ``{"token", "action", "params"}``, read one reply line
  ``{"ok": true, "result": ...}`` or ``{"ok": false, "error_type", "error"}``;
* the pipe is ``\\\\.\\pipe\\<pipe_name>``; the token comes from ``plugins\\ModLabsBridge.json`` (the runner writes
  it) and is never printed, logged or put in an error message;
* the mutating actions (``refresh``, ``set_mods``, ``set_plugins``, ``save``, ``install_archive``) carry
  ``expected_base_path`` and ``expected_profile``, which this client adds itself;
* ``refresh``, ``set_mods``, ``save`` and ``install_archive`` return a job; ``run_job`` polls ``job_status``.

Times are ``time.perf_counter()`` values: on Windows that clock is shared by all processes, and unlike
``time.time()`` (15.6 ms steps) it is fine enough to compare with timestamps the driver plugin takes in MO2.

Everything has a timeout: pipe I/O is overlapped and cancelled when it runs out, so a wedged MO2 cannot hang the
runner.
"""

from __future__ import annotations

import ctypes
import json
import time
from ctypes import wintypes
from typing import Any

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_HANDLE = wintypes.HANDLE
_INVALID = ctypes.c_void_p(-1).value

_GENERIC_READ, _GENERIC_WRITE, _OPEN_EXISTING, _OVERLAPPED_FLAG = 0x80000000, 0x40000000, 3, 0x40000000
_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY, _ERROR_IO_PENDING, _ERROR_BROKEN_PIPE = 2, 231, 997, 109
_WAIT_OBJECT_0, _WAIT_TIMEOUT = 0, 258


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t), ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD), ("hEvent", _HANDLE)]


_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, _HANDLE]
_k32.CreateFileW.restype = _HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateEventW.restype = _HANDLE
_k32.ReadFile.argtypes = [_HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                          ctypes.POINTER(_Overlapped)]
_k32.WriteFile.argtypes = list(_k32.ReadFile.argtypes)
_k32.WaitForSingleObject.argtypes = [_HANDLE, wintypes.DWORD]
_k32.GetOverlappedResult.argtypes = [_HANDLE, ctypes.POINTER(_Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
_k32.CancelIoEx.argtypes = [_HANDLE, ctypes.POINTER(_Overlapped)]
_k32.CloseHandle.argtypes = [_HANDLE]
_k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]


class BridgeError(RuntimeError):
    """The bridge refused or failed a request (``error_type`` is its code), or could not be reached."""

    def __init__(self, error_type: str, message: str, action: str = "") -> None:
        super().__init__(f"{action + ': ' if action else ''}{error_type}: {message}")
        self.error_type, self.detail, self.action = error_type, message, action


class BridgeUnreachable(BridgeError):
    """No answer from the pipe (not there, timed out, or closed on us)."""


def _pipe_io(function, handle, buffer, size: int, timeout: float) -> int:
    event = _k32.CreateEventW(None, True, False, None)
    overlapped = _Overlapped()
    overlapped.hEvent = event
    done = wintypes.DWORD(0)
    try:
        if not function(handle, buffer, size, ctypes.byref(done), ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error == _ERROR_BROKEN_PIPE:
                return 0
            if error != _ERROR_IO_PENDING:
                raise OSError(f"pipe I/O failed (Windows error {error})")
            waited = _k32.WaitForSingleObject(event, max(1, int(timeout * 1000)))
            if waited == _WAIT_TIMEOUT:
                _k32.CancelIoEx(handle, ctypes.byref(overlapped))
                _k32.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(done), True)
                raise TimeoutError("pipe I/O timed out")
            if not _k32.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(done), False):
                error = ctypes.get_last_error()
                if error == _ERROR_BROKEN_PIPE:
                    return 0
                raise OSError(f"pipe I/O failed (Windows error {error})")
        return done.value
    finally:
        _k32.CloseHandle(event)


class BridgeClient:
    """Talks to one bridge. ``calls`` keeps a log of every request (action, outcome, duration; never the token)."""

    def __init__(self, pipe_name: str, token: str, base_path: str, profile: str, io_timeout: float = 20.0) -> None:
        self.pipe_name = pipe_name
        self._path = "\\\\.\\pipe\\" + pipe_name
        self._token = token
        self.base_path, self.profile = base_path, profile
        self.io_timeout = io_timeout
        self.calls: list[dict] = []
        self.refusals: list[dict] = []     # every ok=false reply, with when and what was asked
        self.last_busy_retries = 0

    # ------------------------------------------------------------ transport
    def _connect(self, timeout: float):
        end = time.perf_counter() + timeout
        while True:
            handle = _k32.CreateFileW(self._path, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING,
                                      _OVERLAPPED_FLAG, None)
            if handle not in (None, _INVALID):
                return handle
            error = ctypes.get_last_error()
            if time.perf_counter() >= end:
                raise BridgeUnreachable("unreachable", f"cannot open the bridge pipe (Windows error {error})")
            if error == _ERROR_PIPE_BUSY:
                _k32.WaitNamedPipeW(self._path, 200)
            else:
                time.sleep(0.1)

    def raw(self, action: str, params: dict | None = None, timeout: float | None = None) -> dict:
        """One request, the reply as sent (``ok`` true or false). Raises ``BridgeUnreachable`` on I/O trouble."""

        timeout = self.io_timeout if timeout is None else timeout
        line = (json.dumps({"token": self._token, "action": action, "params": params or {}}) + "\n").encode("utf-8")
        started = time.perf_counter()
        handle = self._connect(timeout)
        try:
            try:
                buffer = ctypes.create_string_buffer(line, len(line))
                sent = 0
                while sent < len(line):
                    wrote = _pipe_io(_k32.WriteFile, handle, ctypes.byref(buffer, sent), len(line) - sent, timeout)
                    if wrote == 0:
                        raise OSError("the bridge closed the pipe while a request was being written")
                    sent += wrote
                data = bytearray()
                chunk = ctypes.create_string_buffer(65536)
                while b"\n" not in data:
                    got = _pipe_io(_k32.ReadFile, handle, chunk, len(chunk), max(0.1, timeout - (time.perf_counter() - started)))
                    if got == 0:
                        raise OSError("the bridge closed the pipe before answering")
                    data.extend(chunk.raw[:got])
            except (OSError, TimeoutError) as exc:
                raise BridgeUnreachable("unreachable", f"{type(exc).__name__}: {exc}", action) from None
        finally:
            _k32.CloseHandle(handle)
        try:
            reply = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
        except ValueError:
            raise BridgeUnreachable("unreachable", "the bridge sent a reply that is not JSON", action) from None
        entry = {"action": action, "ok": bool(reply.get("ok")), "ms": round((time.perf_counter() - started) * 1000, 1),
                 "at": started}
        if not reply.get("ok"):
            entry["error_type"], entry["error"] = reply.get("error_type"), reply.get("error")
            self.refusals.append({**entry, "params": _plain(params)})
        self.calls.append(entry)
        return reply

    def call(self, action: str, timeout: float | None = None, **params: Any) -> Any:
        """The ``result`` of a successful request; ``BridgeError`` otherwise. Mutating actions get the target added."""

        if action in ("refresh", "set_mods", "set_plugins", "save", "install_archive"):
            params.setdefault("expected_base_path", self.base_path)
            params.setdefault("expected_profile", self.profile)
        elif action in ("status", "snapshot"):
            params.setdefault("expected_base_path", self.base_path)
            params.setdefault("expected_profile", self.profile)
        reply = self.raw(action, params, timeout)
        if not reply.get("ok"):
            raise BridgeError(str(reply.get("error_type")), str(reply.get("error")), action)
        return reply.get("result")

    def token_in(self, text: str) -> bool:
        """Whether ``text`` contains this client's token (used to prove the bridge never echoes it)."""

        return self._token in text

    # ------------------------------------------------------------ reads
    def ping(self) -> dict:
        return self.call("ping")

    def status(self) -> dict:
        return self.call("status")

    def snapshot(self) -> dict:
        return self.call("snapshot", timeout=60)

    # ------------------------------------------------------------ jobs
    def job_status(self, job_id: str) -> dict:
        return self.call("job_status", job_id=job_id)

    def wait_job(self, job: dict, timeout: float, poll: float = 0.03, on_poll=None) -> dict:
        """Poll until the job leaves queued/running. Returns the final ``job_status`` plus ``states_seen``
        ([[status, ms since the request], ...], one entry per change) and ``elapsed_ms``. ``on_poll(state,
        seconds)`` is called after every poll (the runner uses it to probe the bridge while an installer is open)."""

        started = job.get("_started", time.perf_counter())
        seen = [[job.get("status"), round((time.perf_counter() - started) * 1000, 1)]]
        end = time.perf_counter() + timeout
        while True:
            state = self.job_status(job["job_id"])
            if state["status"] != seen[-1][0]:
                seen.append([state["status"], round((time.perf_counter() - started) * 1000, 1)])
            if state["status"] not in ("queued", "running"):
                state["states_seen"], state["elapsed_ms"] = seen, round((time.perf_counter() - started) * 1000, 1)
                state["ended_at"] = time.perf_counter()
                return state
            if on_poll is not None:
                on_poll(state, time.perf_counter() - started)
            if time.perf_counter() > end:
                raise BridgeError("timeout", f"job {job['job_id']} ({state.get('action')}) still {state['status']} "
                                  f"after {timeout:g} s", "job_status")
            time.sleep(poll)

    def call_busy(self, action: str, busy_wait: float = 10.0, **params: Any) -> Any:
        """``call``, retried while MO2 answers ``MO2_BUSY`` (up to ``busy_wait`` s). Every refusal stays in
        ``refusals``; the number of retries is in ``last_busy_retries``."""

        started = time.perf_counter()
        self.last_busy_retries = 0
        while True:
            try:
                return self.call(action, **params)
            except BridgeError as exc:
                if exc.error_type != "MO2_BUSY" or time.perf_counter() - started > busy_wait:
                    raise
                self.last_busy_retries += 1
                time.sleep(0.25)

    def start_job(self, action: str, busy_wait: float = 10.0, **params: Any) -> dict:
        """Start a job action, retrying while MO2 says ``MO2_BUSY``. Returns ``{job_id, status, _started,
        busy_retries}``."""

        started = time.perf_counter()
        job = dict(self.call_busy(action, busy_wait=busy_wait, **params))
        job["_started"], job["busy_retries"] = started, self.last_busy_retries
        return job

    def run_job(self, action: str, timeout: float = 60.0, busy_wait: float = 10.0, on_poll=None, **params: Any) -> dict:
        """Start a job and wait for it. A job that ends in ``error`` raises ``BridgeError``."""

        job = self.start_job(action, busy_wait=busy_wait, **params)
        final = self.wait_job(job, timeout, on_poll=on_poll)
        final["busy_retries"] = job["busy_retries"]
        if final["status"] == "error":
            raise BridgeError(str(final.get("error_type")), str(final.get("error")), action)
        return final


def _plain(params: dict | None) -> dict:
    """Request parameters for the log, without anything that could be a secret."""

    return {k: v for k, v in (params or {}).items() if k not in ("token",)}
