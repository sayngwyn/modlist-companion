"""The 7z wrapper must never crash its host process, whatever it is given.

A mistake in the ctypes layer takes the whole process down (in production that is MO2), so the
inputs below run in a child process: if it dies or hangs, the test fails instead of the test run.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import archive_fixtures as fx
from test_archive_reader import DLL, DOWNLOADS, needs_dll

PYTHON_DIR = Path(__file__).resolve().parents[2] / "python"

CHILD = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from fomod_logger_support.archive.sevenzip import ArchiveError, Limits, open_archive

    dll = sys.argv[2] or None
    limits = Limits(max_entry_bytes=1 << 20, max_total_bytes=4 << 20, max_decoded_bytes=64 << 20,
                    timeout_seconds=10, max_entries=10000)
    for path in json.load(open(sys.argv[3])):
        outcome = "ok"
        try:
            with open_archive(path, sevenzip_dll=dll, limits=limits) as archive:
                names = [e.path for e in archive.files()][:50]
                result = archive.read(names)
                codes = sorted({e.code for e in result.errors.values()})
                outcome = "ok" if not codes else "ok:" + ",".join(codes)
        except ArchiveError as error:
            outcome = "error:" + error.code
        except BaseException as error:           # anything else is a bug in the wrapper
            outcome = "UNEXPECTED:" + type(error).__name__ + ":" + str(error)
        print(json.dumps([path, outcome]), flush=True)
""")


def _run(paths: list[Path], tmp_path: Path, timeout: int = 240) -> dict[str, str]:
    listing = tmp_path / "paths.json"
    listing.write_text(json.dumps([str(p) for p in paths]))
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    process = subprocess.run(
        [sys.executable, str(script), str(PYTHON_DIR), str(DLL or ""), str(listing)],
        capture_output=True, text=True, timeout=timeout)
    outcomes = {}
    for line in process.stdout.splitlines():
        path, outcome = json.loads(line)
        outcomes[path] = outcome
    last = list(outcomes)[-1] if outcomes else "(none)"
    assert process.returncode == 0, (
        f"the child died (exit {process.returncode}) after {last}; stderr: {process.stderr[-500:]}")
    assert len(outcomes) == len(paths), f"the child stopped early after {last}"
    return outcomes


def _corpus(tmp_path: Path) -> list[Path]:
    entries = [("fomod", None), ("fomod/ModuleConfig.xml", b"<config/>" * 40), ("a.esp", os.urandom(700)),
               ("empty.txt", b""), ("sub/c.esp", b"C" * 900)]
    bases = {
        "zip": fx.make_zip(tmp_path / "base.zip", {n: d for n, d in entries}),
        "7z": fx.make_7z(tmp_path / "base.7z", entries),
        "7z-solid-lzma2": fx.make_7z(tmp_path / "solid.7z", entries, solid=True, lzma2=True),
        "7z-aes": fx.make_7z(tmp_path / "aes.7z", entries, aes=True),
        "7z-hdr": fx.make_7z(tmp_path / "hdr.7z", entries, encrypt_header=True),
        "rar": fx.make_rar4(tmp_path / "base.rar", [(n, d) for n, d in entries if d]),
        "rar-solid": fx.make_rar4(tmp_path / "solid.rar", [(n, d) for n, d in entries if d], solid_flags=True),
        "rar-pw": fx.make_rar4(tmp_path / "pw.rar", [(n, d) for n, d in entries if d], encrypted=True),
        "rar-split": fx.make_rar4(tmp_path / "split.rar", [(n, d) for n, d in entries if d], split_after=True),
    }
    rng = random.Random(20261021)
    corpus: list[Path] = list(bases.values())
    counter = 0

    def add(source: Path, data: bytes) -> None:
        nonlocal counter
        counter += 1
        target = tmp_path / "mutants" / f"{counter:04d}{source.suffix}"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        corpus.append(target)

    for source in bases.values():
        blob = source.read_bytes()
        for cut in sorted({0, 1, 4, 8, 16, 31, 32, 33, 64, len(blob) - 1, len(blob) - 8, len(blob) // 2,
                           *(rng.randrange(len(blob)) for _ in range(14))}):
            add(source, blob[:cut])
        for _ in range(40):
            mutated = bytearray(blob)
            for _ in range(rng.choice((1, 1, 2, 4, 16))):
                mutated[rng.randrange(len(mutated))] = rng.randrange(256)
            add(source, bytes(mutated))
        for _ in range(4):
            start = rng.randrange(len(blob))
            mutated = bytearray(blob)
            mutated[start:start + rng.randrange(1, 40)] = b"\x00" * rng.randrange(1, 40)
            add(source, bytes(mutated[:len(blob)]))
        add(source, blob + os.urandom(200))
        add(source, blob[:32] + os.urandom(len(blob) - 32))
    for suffix, signature in ((".7z", b"7z\xbc\xaf\x27\x1c\x00\x04"), (".rar", b"Rar!\x1a\x07\x00"),
                              (".rar", b"Rar!\x1a\x07\x01\x00"), (".zip", b"PK\x03\x04"), (".zip", b"PK\x05\x06")):
        for _ in range(6):
            add(Path("x" + suffix), signature + os.urandom(rng.randrange(0, 400)))
    return corpus


@needs_dll
def test_wrapper_survives_corrupt_truncated_encrypted_and_split_archives(tmp_path):
    corpus = _corpus(tmp_path)
    assert len(corpus) > 500
    outcomes = _run(corpus, tmp_path)
    unexpected = {p: o for p, o in outcomes.items() if o.startswith("UNEXPECTED")}
    assert not unexpected, list(unexpected.items())[:5]
    kinds = {o.split(":")[1] if o.startswith("error:") else "ok" for o in outcomes.values()}
    # The corpus really did exercise the failure modes it is meant to.
    assert {"ok", "corrupt", "encrypted", "multipart"} <= kinds


@needs_dll
def test_clean_failures_for_each_named_hazard(tmp_path):
    entries = [("a.esp", b"A" * 300), ("b.esp", b"B" * 300)]
    cases = {
        "corrupt": fx.make_7z(tmp_path / "c.7z", entries),
        "truncated": fx.make_7z(tmp_path / "t.7z", entries),
        "encrypted": fx.make_7z(tmp_path / "e.7z", entries, encrypt_header=True),
        "multipart": fx.make_rar4(tmp_path / "m.rar", entries, split_after=True),
    }
    cases["corrupt"].write_bytes(cases["corrupt"].read_bytes()[:40] + b"\xff" * 20
                                 + cases["corrupt"].read_bytes()[60:])
    cases["truncated"].write_bytes(cases["truncated"].read_bytes()[:-20])
    outcomes = _run(list(cases.values()), tmp_path)
    assert outcomes[str(cases["encrypted"])] == "error:encrypted"
    assert outcomes[str(cases["multipart"])] == "error:multipart"
    assert outcomes[str(cases["corrupt"])].startswith(("error:corrupt", "ok"))
    assert outcomes[str(cases["truncated"])].startswith("error:corrupt")


@needs_dll
def test_wrapper_survives_damaged_copies_of_real_downloads(tmp_path):
    sources = [p for p in (DOWNLOADS / "Lux Via (patch hub)-116722-2-2-1713093664.rar",
                           DOWNLOADS / "Bijin Wives SE 1.1.2-11247-1-1-2.7z",
                           DOWNLOADS / "Dear Diary-23010-3-0-6-1635701683.zip") if p.is_file()]
    if not sources:
        pytest.skip("the test MO2's real downloads are not available")
    rng = random.Random(7)
    paths = []
    for source in sources:
        blob = source.read_bytes()[: 3 * 1024 * 1024 + 7]     # a truncated copy
        for index in range(12):
            mutated = bytearray(blob)
            for _ in range(rng.choice((1, 3, 30))):
                mutated[rng.randrange(min(len(mutated), 200_000))] = rng.randrange(256)
            target = tmp_path / f"{source.stem[:8]}-{index}{source.suffix}"
            target.write_bytes(bytes(mutated[: rng.choice((len(mutated), len(mutated) // 2, 100_000))]))
            paths.append(target)
    outcomes = _run(paths, tmp_path, timeout=420)
    assert not [o for o in outcomes.values() if o.startswith("UNEXPECTED")]
