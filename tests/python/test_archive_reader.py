"""The hardened archive reader: zip (stdlib), 7z and Rar (MO2's 7z.dll through ctypes)."""

from __future__ import annotations

import dataclasses
import os
import time
import zipfile
from pathlib import Path

import pytest

import archive_fixtures as fx
from fomod_logger_support.archive import sevenzip as sz
from fomod_logger_support.archive.fomod_xml import parse_module_config
from fomod_logger_support.archive.sevenzip import (
    ArchiveCorruptError, ArchiveEncryptedError, ArchiveEntryNotFound, ArchiveError,
    ArchiveMultiPartError, ArchiveTimeoutError, ArchiveTooBigError, ArchiveUnsupportedError,
    Limits, normalize_entry_path, open_archive)

MO2_FOLDER = Path(os.environ.get("FOMOD_LOGGER_TEST_MO2", Path(__file__).resolve().parents[4] / "MO2-Skyrim-Logger"))
DOWNLOADS = MO2_FOLDER / "downloads"


def _find_dll() -> Path | None:
    found = sz.find_sevenzip_dll(MO2_FOLDER)
    if found:
        return found
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        for candidate in (Path(folder) / "7z.dll", Path(folder) / "dlls" / "7z.dll"):
            if candidate.is_file():
                return candidate
    return None


DLL = _find_dll()
needs_dll = pytest.mark.skipif(DLL is None, reason="MO2's 7z.dll is not available")

ENTRIES = [
    ("fomod", None),
    ("fomod/ModuleConfig.xml", b"<config/>"),
    ("a.esp", b"A" * 100),
    ("empty.txt", b""),
    ("sub/c.esp", b"C" * 50),
]


# ------------------------------------------------------------------------------ names


@pytest.mark.parametrize("raw, expected", [
    ("fomod/ModuleConfig.xml", "fomod/ModuleConfig.xml"),
    ("fomod\\ModuleConfig.xml", "fomod/ModuleConfig.xml"),
    ("./a//b/./c.txt", "a/b/c.txt"),
    ("dir/", "dir"),
    ("../evil.txt", None),
    ("a/../../evil.txt", None),
    ("a/../b.txt", None),
    ("/abs/path.txt", None),
    ("\\abs\\path.txt", None),
    ("C:/Windows/x.dll", None),
    ("c:evil.txt", None),
    ("name:stream", None),
    ("a\x00b", None),
    ("", None),
    ("///", None),
])
def test_entry_names_are_normalised_or_ignored(raw, expected):
    assert normalize_entry_path(raw) == expected


# ------------------------------------------------------------------------------- zip


def test_zip_lists_and_reads_with_case_and_slash_insensitive_lookup(tmp_path):
    path = fx.make_zip(tmp_path / "a.zip", {name: data for name, data in ENTRIES})
    with open_archive(path) as archive:
        assert archive.backend == "zipfile" and archive.format == "zip"
        assert {e.path for e in archive.entries} == {"fomod", "fomod/ModuleConfig.xml", "a.esp",
                                                    "empty.txt", "sub/c.esp"}
        assert archive.find("FOMOD\\modulecONFIG.xml").size == 9
        assert archive.find("fomod").is_dir
        result = archive.read(["FOMOD/moduleconfig.xml", "SUB\\C.ESP", "missing.txt", "fomod"])
        assert result.data == {"FOMOD/moduleconfig.xml": b"<config/>", "SUB\\C.ESP": b"C" * 50}
        assert isinstance(result.errors["missing.txt"], ArchiveEntryNotFound)
        assert isinstance(result.errors["fomod"], ArchiveEntryNotFound)   # a folder has no content


def test_zip_hostile_entry_names_are_dropped(tmp_path):
    path = fx.make_zip(tmp_path / "evil.zip", {"ok.txt": b"fine"},
                       raw_names=["../evil.txt", "/abs.txt", "C:/x.txt", "a/../../b.txt", "n:stream"])
    with open_archive(path) as archive:
        assert [e.path for e in archive.entries] == ["ok.txt"]
        assert archive.skipped_unsafe == 5
        assert archive.find("../evil.txt") is None


def test_zip_a_repeated_name_resolves_to_the_later_entry(tmp_path):
    path = tmp_path / "dup.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("same.txt", b"first")
        with pytest.warns(UserWarning):
            z.writestr("same.txt", b"second")
    with open_archive(path) as archive:
        assert archive.read(["same.txt"]).data["same.txt"] == b"second"


def test_zip_per_entry_and_total_budgets(tmp_path):
    path = fx.make_zip(tmp_path / "b.zip", {"big.bin": b"x" * 5000, "s1": b"1" * 10, "s2": b"2" * 10})
    with open_archive(path) as archive:
        too_big = archive.read(["big.bin", "s1"], max_entry_bytes=100)
        assert isinstance(too_big.errors["big.bin"], ArchiveTooBigError)
        assert too_big.data == {"s1": b"1" * 10}
        cut = archive.read(["big.bin"], max_entry_bytes=100, truncate=True)
        assert cut.data["big.bin"] == b"x" * 100 and cut.truncated == {"big.bin"}
        total = archive.read(["s1", "s2"], max_total_bytes=15)
        assert total.data == {"s1": b"1" * 10}
        assert isinstance(total.errors["s2"], ArchiveTooBigError)


def test_zip_header_lying_about_size_cannot_exhaust_memory(tmp_path):
    path = fx.make_zip(tmp_path / "lie.zip", {"bomb.bin": b"\0" * (4 * 1024 * 1024)})
    with open_archive(path) as archive:
        entry = archive.find("bomb.bin")
        archive._by_key["bomb.bin"] = dataclasses.replace(entry, size=1)   # claims 1 byte
        result = archive.read(["bomb.bin"], max_entry_bytes=1000)
        assert isinstance(result.errors["bomb.bin"], ArchiveTooBigError)


def test_zip_password_protected_entries_are_refused_without_reading(tmp_path):
    path = fx.make_zip(tmp_path / "pw.zip", {"secret.txt": b"data", "open.txt": b"data"},
                       flag_bits={"secret.txt": 0x1})
    with open_archive(path) as archive:
        assert archive.find("secret.txt").encrypted and not archive.find("open.txt").encrypted
        result = archive.read(["secret.txt", "open.txt"])
        assert isinstance(result.errors["secret.txt"], ArchiveEncryptedError)
        assert result.data["open.txt"] == b"data"


def test_zip_corrupt_and_truncated_fail_cleanly(tmp_path):
    good = fx.make_zip(tmp_path / "g.zip", {"a.txt": b"hello" * 1000, "b.txt": b"x" * 4000})
    blob = good.read_bytes()
    for name, data in {
        "garbage.zip": b"PK\x03\x04" + os.urandom(64),
        "half.zip": blob[: len(blob) // 2],
        "nodir.zip": blob[:-30],
        "empty.zip": b"",
    }.items():
        target = tmp_path / name
        target.write_bytes(data)
        with pytest.raises(ArchiveError):
            open_archive(target)
    damaged = bytearray(blob)
    damaged[40:60] = b"\xff" * 20                      # inside the first entry's data
    path = tmp_path / "damaged.zip"
    path.write_bytes(bytes(damaged))
    with open_archive(path) as archive:
        result = archive.read(["a.txt"])
        assert "a.txt" in result.errors or result.data["a.txt"] != b"hello" * 1000


def test_zip_timeout(tmp_path):
    path = fx.make_zip(tmp_path / "t.zip", {"a": b"1", "b": b"2"})
    with open_archive(path, limits=Limits(timeout_seconds=-1)) as archive:
        assert all(isinstance(e, ArchiveTimeoutError) for e in archive.read(["a", "b"]).errors.values())


def test_entry_count_is_capped(tmp_path):
    path = fx.make_zip(tmp_path / "many.zip", {f"f{i}.txt": b"x" for i in range(20)})
    with pytest.raises(ArchiveTooBigError):
        open_archive(path, limits=Limits(max_entries=10))


@pytest.mark.parametrize("name", ["mod.7z.001", "mod.zip.002", "mod.z01", "mod.r00", "mod.001"])
def test_numbered_part_names_are_refused(tmp_path, name):
    (tmp_path / name).write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 40)
    with pytest.raises(ArchiveMultiPartError):
        open_archive(tmp_path / name, sevenzip_dll=DLL)


def test_sibling_parts_are_detected(tmp_path):
    fx.make_zip(tmp_path / "a.zip", {"x": b"1"})
    (tmp_path / "a.z01").write_bytes(b"part")
    with pytest.raises(ArchiveMultiPartError):
        open_archive(tmp_path / "a.zip")
    for part in ("big.part1.rar", "big.part2.rar"):
        fx.make_rar4(tmp_path / part, [("a", b"1")])
    with pytest.raises(ArchiveMultiPartError):
        open_archive(tmp_path / "big.part1.rar", sevenzip_dll=DLL)


def test_unsupported_and_missing_files(tmp_path):
    (tmp_path / "notes.txt").write_text("not an archive")
    with pytest.raises(ArchiveUnsupportedError):
        open_archive(tmp_path / "notes.txt")
    with pytest.raises(ArchiveCorruptError):
        open_archive(tmp_path / "missing.zip")
    fx.make_rar4(tmp_path / "a.rar", [("a", b"1")])
    with pytest.raises(ArchiveUnsupportedError):          # no 7z.dll given
        open_archive(tmp_path / "a.rar")


def test_a_file_that_is_not_a_7z_dll_is_refused(tmp_path):
    fake = tmp_path / "7z.dll"
    fake.write_bytes(b"MZ" + b"\0" * 100)
    fx.make_rar4(tmp_path / "a.rar", [("a", b"1")])
    with pytest.raises(ArchiveUnsupportedError):
        open_archive(tmp_path / "a.rar", sevenzip_dll=fake)
    with pytest.raises(ArchiveUnsupportedError):
        open_archive(tmp_path / "a.rar", sevenzip_dll=tmp_path / "nowhere" / "7z.dll")


# -------------------------------------------------------------------------------- 7z


@needs_dll
@pytest.mark.parametrize("solid", [False, True])
def test_7z_lists_and_reads(tmp_path, solid):
    path = fx.make_7z(tmp_path / "a.7z", ENTRIES, solid=solid)
    with open_archive(path, sevenzip_dll=DLL) as archive:
        assert archive.backend == "7z.dll" and archive.format == "7z"
        assert {e.path: (e.size, e.is_dir) for e in archive.entries} == {
            "fomod": (0, True), "fomod/ModuleConfig.xml": (9, False), "a.esp": (100, False),
            "empty.txt": (0, False), "sub/c.esp": (50, False)}
        result = archive.read(["FOMOD\\modulecONFIG.xml", "A.ESP", "Sub/C.esp", "empty.txt", "nope"])
        assert result.data == {"FOMOD\\modulecONFIG.xml": b"<config/>", "A.ESP": b"A" * 100,
                               "Sub/C.esp": b"C" * 50, "empty.txt": b""}
        assert isinstance(result.errors["nope"], ArchiveEntryNotFound)


@needs_dll
def test_7z_hostile_names_are_dropped(tmp_path):
    entries = [("ok.txt", b"fine"), ("../evil.txt", b"bad"), ("/abs.txt", b"bad"),
               ("C:/x.txt", b"bad"), ("a/../../b.txt", b"bad")]
    path = fx.make_7z(tmp_path / "evil.7z", entries)
    with open_archive(path, sevenzip_dll=DLL) as archive:
        assert [e.path for e in archive.entries] == ["ok.txt"]
        assert archive.skipped_unsafe == 4
        assert archive.read(["ok.txt"]).data == {"ok.txt": b"fine"}


@needs_dll
def test_7z_entry_and_total_budgets(tmp_path):
    path = fx.make_7z(tmp_path / "b.7z", [("big", b"x" * 5000), ("s1", b"1" * 10), ("s2", b"2" * 10)],
                      solid=True)
    with open_archive(path, sevenzip_dll=DLL) as archive:
        result = archive.read(["big", "s1"], max_entry_bytes=100)
        assert isinstance(result.errors["big"], ArchiveTooBigError)
        assert result.data == {"s1": b"1" * 10}
        cut = archive.read(["big"], max_entry_bytes=100, truncate=True)
        assert cut.data["big"] == b"x" * 100 and cut.truncated == {"big"}
        total = archive.read(["s1", "s2"], max_total_bytes=15)
        assert total.data == {"s1": b"1" * 10}
        assert isinstance(total.errors["s2"], ArchiveTooBigError)


@needs_dll
def test_7z_solid_block_over_the_decompression_budget_is_too_big_to_scan(tmp_path):
    entries = [("first.bin", b"1" * 1000), ("middle.bin", b"2" * 100_000), ("last.esp", b"3" * 10)]
    solid = fx.make_7z(tmp_path / "solid.7z", entries, solid=True)
    limits = Limits(max_decoded_bytes=5000)
    with open_archive(solid, sevenzip_dll=DLL, limits=limits) as archive:
        result = archive.read(["first.bin", "last.esp"])
        assert result.data == {"first.bin": b"1" * 1000}       # cheap: first in its block
        assert isinstance(result.errors["last.esp"], ArchiveTooBigError)
        assert "too big to scan" in str(result.errors["last.esp"])
    loose = fx.make_7z(tmp_path / "loose.7z", entries, solid=False)
    with open_archive(loose, sevenzip_dll=DLL, limits=limits) as archive:
        assert archive.read(["last.esp"]).data == {"last.esp": b"3" * 10}   # own block: cheap


@needs_dll
def test_7z_decompression_budget_is_also_enforced_while_it_runs(tmp_path):
    entries = [("a.bin", b"1" * 200_000), ("b.esp", b"2" * 10)]
    path = fx.make_7z(tmp_path / "solid.7z", entries, solid=True, lzma2=True)
    with open_archive(path, sevenzip_dll=DLL) as archive:
        # Pretend the up-front estimate said it was cheap; the live counter must still stop it.
        archive.limits = dataclasses.replace(archive.limits, max_decoded_bytes=1000)
        archive._decode_costs = lambda: {}
        result = archive.read(["b.esp"])
        assert isinstance(result.errors["b.esp"], ArchiveTooBigError)
        assert "b.esp" not in result.data


@needs_dll
def test_7z_timeout_stops_reading(tmp_path):
    path = fx.make_7z(tmp_path / "t.7z", ENTRIES)
    with open_archive(path, sevenzip_dll=DLL) as archive:
        archive.limits = dataclasses.replace(archive.limits, timeout_seconds=-1)
        result = archive.read(["a.esp"])
        assert isinstance(result.errors["a.esp"], ArchiveTimeoutError)
    with pytest.raises(ArchiveTimeoutError):
        open_archive(path, sevenzip_dll=DLL, limits=Limits(timeout_seconds=-1))


@needs_dll
def test_7z_password_protected_data_is_refused_not_prompted(tmp_path):
    path = fx.make_7z(tmp_path / "pw.7z", ENTRIES, aes=True)
    with open_archive(path, sevenzip_dll=DLL) as archive:
        assert archive.find("a.esp").encrypted
        result = archive.read(["a.esp", "empty.txt"])
        assert isinstance(result.errors["a.esp"], ArchiveEncryptedError)
        assert result.data == {"empty.txt": b""}


@needs_dll
def test_7z_password_request_during_decoding_is_answered_with_an_abort(tmp_path):
    """Even when the listing does not say 'encrypted', the decoder's password request is refused."""

    path = fx.make_7z(tmp_path / "pw.7z", ENTRIES, aes=True)
    started = time.monotonic()
    with open_archive(path, sevenzip_dll=DLL) as archive:
        archive.entries = [dataclasses.replace(e, encrypted=False) for e in archive.entries]
        archive._by_key = {k: dataclasses.replace(v, encrypted=False) for k, v in archive._by_key.items()}
        result = archive.read(["a.esp"])
        assert isinstance(result.errors["a.esp"], ArchiveEncryptedError)
    assert time.monotonic() - started < 10


@needs_dll
def test_7z_encrypted_headers_are_refused_at_open(tmp_path):
    path = fx.make_7z(tmp_path / "hdr.7z", ENTRIES, encrypt_header=True)
    started = time.monotonic()
    with pytest.raises(ArchiveEncryptedError):
        open_archive(path, sevenzip_dll=DLL)
    assert time.monotonic() - started < 10


@needs_dll
def test_7z_entry_cap(tmp_path):
    path = fx.make_7z(tmp_path / "many.7z", [(f"f{i}.txt", b"x") for i in range(30)])
    with pytest.raises(ArchiveTooBigError):
        open_archive(path, sevenzip_dll=DLL, limits=Limits(max_entries=10))


@needs_dll
def test_7z_truncated_and_damaged_archives_fail_cleanly(tmp_path):
    blob = fx.make_7z(tmp_path / "src.7z", ENTRIES, solid=True).read_bytes()
    outcomes = set()
    cases = {f"cut{n}": blob[:n] for n in (0, 5, 12, 31, 32, 40, len(blob) // 2, len(blob) - 20, len(blob) - 1)}
    for position in (8, 20, 33, len(blob) - 40, len(blob) - 10):
        damaged = bytearray(blob)
        damaged[position] ^= 0xFF
        cases[f"flip{position}"] = bytes(damaged)
    for name, data in cases.items():
        target = tmp_path / f"{name}.7z"
        target.write_bytes(data)
        try:
            with open_archive(target, sevenzip_dll=DLL) as archive:
                archive.read([e.path for e in archive.files()])
            outcomes.add("opened")
        except ArchiveError as error:
            outcomes.add(error.code)
    assert outcomes <= {"opened", "corrupt", "unsupported", "encrypted", "too_big"}
    assert "corrupt" in outcomes


@needs_dll
def test_7z_com_objects_are_released_in_balance(tmp_path, monkeypatch):
    made = []
    for cls in (sz._InStream, sz._OpenCallback, sz._ExtractCallback, sz._EntrySink):
        original = cls.__init__

        def init(self, *args, _original=original, **kwargs):
            _original(self, *args, **kwargs)
            made.append(self)

        monkeypatch.setattr(cls, "__init__", init)
    path = fx.make_7z(tmp_path / "a.7z", ENTRIES, solid=True)
    with open_archive(path, sevenzip_dll=DLL) as archive:
        archive.read(["a.esp", "sub/c.esp"])
        archive.read(["fomod/ModuleConfig.xml"])
    assert made
    assert [obj._refs for obj in made] == [1] * len(made)


# ------------------------------------------------------------------------------- rar


@needs_dll
@pytest.mark.parametrize("solid", [False, True])
def test_rar_lists_and_reads(tmp_path, solid):
    path = fx.make_rar4(tmp_path / "a.rar", [("fomod/ModuleConfig.xml", b"<config/>"), ("a.esp", b"A" * 100)],
                        solid_flags=solid)
    with open_archive(path, sevenzip_dll=DLL) as archive:
        assert archive.format == "Rar"
        result = archive.read(["FOMOD/modulecONFIG.xml", "a.esp"])
        assert result.data == {"FOMOD/modulecONFIG.xml": b"<config/>", "a.esp": b"A" * 100}


@needs_dll
def test_rar_password_protected_is_refused(tmp_path):
    path = fx.make_rar4(tmp_path / "pw.rar", [("a.esp", b"A" * 100)], encrypted=True)
    with open_archive(path, sevenzip_dll=DLL) as archive:
        assert archive.find("a.esp").encrypted
        assert isinstance(archive.read(["a.esp"]).errors["a.esp"], ArchiveEncryptedError)


@needs_dll
@pytest.mark.parametrize("flags", [{"split_after": True}, {"split_before": True}])
def test_rar_volumes_are_refused(tmp_path, flags):
    path = fx.make_rar4(tmp_path / "v.rar", [("a.esp", b"A" * 100)], **flags)
    with pytest.raises(ArchiveMultiPartError):
        open_archive(path, sevenzip_dll=DLL)


@needs_dll
def test_rar_solid_budget_counts_the_whole_chain(tmp_path):
    path = fx.make_rar4(tmp_path / "s.rar", [("a.bin", b"1" * 100_000), ("b.esp", b"2" * 10)],
                        solid_flags=True)
    with open_archive(path, sevenzip_dll=DLL, limits=Limits(max_decoded_bytes=5000)) as archive:
        result = archive.read(["b.esp"])
        assert isinstance(result.errors["b.esp"], ArchiveTooBigError)


@needs_dll
def test_rar_truncated_and_damaged_fail_cleanly(tmp_path):
    blob = fx.make_rar4(tmp_path / "src.rar", [("a.esp", os.urandom(300)), ("b.esp", os.urandom(300))]).read_bytes()
    for name, data in {"cut": blob[: len(blob) // 2], "cut2": blob[:12], "flip": blob[:40] + b"\xff" + blob[41:]}.items():
        target = tmp_path / f"{name}.rar"
        target.write_bytes(data)
        try:
            with open_archive(target, sevenzip_dll=DLL) as archive:
                archive.read([e.path for e in archive.files()])
        except ArchiveError:
            pass


# ------------------------------------------------------------------ real downloads


def _real(name: str) -> Path:
    path = DOWNLOADS / name
    if not path.is_file():
        pytest.skip(f"{name} is not in the test MO2's downloads")
    return path


@needs_dll
@pytest.mark.parametrize("name, config", [
    ("Lux Via (patch hub)-116722-2-2-1713093664.rar", "Lux Via (patch hub)/FOMOD/ModuleConfig.xml"),
    ("Bijin NPCs SE 1.2.1-11287-1-2-1.7z", "Bijin NPCs v1.2.1/Fomod/ModuleConfig.xml"),
    ("Dear Diary-23010-3-0-6-1635701683.zip", "fomod/ModuleConfig.xml"),
])
def test_real_downloads_open_and_give_up_their_fomod_quickly(name, config):
    path = _real(name)
    started = time.perf_counter()
    with open_archive(path, sevenzip_dll=DLL) as archive:
        entry = archive.find(config)
        assert entry is not None
        data = archive.read([config]).data[config]
        assert parse_module_config(data).steps
    assert time.perf_counter() - started < 5


@needs_dll
def test_zip_entries_that_zipfile_cannot_decode_fall_back_to_the_7z_dll(tmp_path, monkeypatch):
    path = fx.make_zip(tmp_path / "odd.zip", {"a.esp": b"A" * 100, "b.txt": b"B" * 10})
    original = zipfile.ZipFile.open

    def refuse(self, name, *args, **kwargs):
        if getattr(name, "filename", name) == "a.esp":
            raise NotImplementedError("compression type 9 (deflate64)")
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", refuse)
    with open_archive(path, sevenzip_dll=DLL) as archive:
        result = archive.read(["a.esp", "b.txt"])
        assert result.data == {"a.esp": b"A" * 100, "b.txt": b"B" * 10} and not result.errors
    with open_archive(path) as archive:                     # no DLL: a clean per-entry error instead
        result = archive.read(["a.esp", "b.txt"])
        assert isinstance(result.errors["a.esp"], ArchiveUnsupportedError) and result.data == {"b.txt": b"B" * 10}


@needs_dll
def test_an_archive_stays_usable_after_the_open_time_limit_has_passed(tmp_path):
    path = fx.make_7z(tmp_path / "late.7z", ENTRIES, solid=True, lzma2=True)
    with open_archive(path, sevenzip_dll=DLL, limits=Limits(timeout_seconds=0.4)) as archive:
        time.sleep(0.6)                                    # longer than the limit that governed the open
        assert archive.read(["a.esp"]).data == {"a.esp": b"A" * 100}
        time.sleep(0.6)
        assert archive.read(["sub/c.esp", "fomod/ModuleConfig.xml"]).data["sub/c.esp"] == b"C" * 50


def test_error_flag_labels_follow_7zips_bit_order():
    labels = dict(sz._ERROR_FLAGS)
    assert labels == {
        1: "not an archive", 2: "damaged headers", 4: "damaged encrypted headers",
        8: "start of the archive is missing", 16: "start of the archive is unconfirmed",
        32: "archive ends early (truncated)", 64: "data after the end of the archive",
        128: "unsupported method", 256: "unsupported feature", 512: "data error", 1024: "checksum error"}


@needs_dll
def test_a_truncated_zip_read_by_the_dll_reports_that_it_ends_early(tmp_path):
    blob = fx.make_zip(tmp_path / "whole.zip", {"a.txt": b"hello" * 2000, "b.txt": b"x" * 500}).read_bytes()
    cut = tmp_path / "cut.zip"
    cut.write_bytes(blob[: len(blob) - 120])            # the central directory is gone
    library = sz.SevenZipLibrary.load(DLL)
    archive = sz._SevenZipArchive(cut, Limits(), library, ("zip",), True)
    try:
        assert "archive ends early (truncated)" in archive.warnings
        assert archive.find("a.txt") is not None
    finally:
        archive.close()
