"""Build the FOMOD Plus edition of Modlist Companion: a Python-only package (no native DLL).

The package holds ``plugins/fomod_logger_plus.py`` (``python/fomod_logger_plus_plugin.py``) and the support folder it
shares with the full edition (``plugins/fomod_logger_support/``), plus the SQLite runtime MO2's Python needs. It is
unzipped into the MO2 folder like the full edition's package; if both are installed, the shared files are the same
files. The corresponding source is every tracked file of the commit (nothing LGPL is shipped: no uibase, no native
code), written beside it.

This reuses the exact-commit checks and helpers of ``build_release.py``. It is scaffolding for the release: the
documentation set, the release identity (names, version) and the publication checklist are step C of the plan.

    python packaging/build_plus_release.py --expected-commit <40 hex> --sqlite-dll <sqlite3.dll> --output <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release as common  # noqa: E402

ROOT = common.ROOT
PRODUCT_NAME = "Modlist Companion (for FOMOD Plus)"
PACKAGE_VERSION = common.PACKAGE_VERSION
ARCHIVE_STEM = f"modlist-companion-plus-{PACKAGE_VERSION}"
BINARY_ARCHIVE_NAME = f"{ARCHIVE_STEM}.zip"
SOURCE_ARCHIVE_NAME = f"{ARCHIVE_STEM}-source.zip"
SOURCE_ARCHIVE_ROOT = f"{ARCHIVE_STEM}-source"

PLUGIN_SOURCE = "python/fomod_logger_plus_plugin.py"
PLUGIN_DESTINATION = "plugins/fomod_logger_plus.py"

# Documentation the Python-only package carries (the native edition's uibase evidence is not part of it).
DOC_FILES = {
    "fomod_logger_plus_docs/README.md": "docs/README_PLUS.md",
    "fomod_logger_plus_docs/CHANGELOG.md": "CHANGELOG.md",
    "fomod_logger_plus_docs/LICENSE": "LICENSE",
    "fomod_logger_plus_docs/MODIFICATIONS.md": "MODIFICATIONS.md",
    "fomod_logger_plus_docs/THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
    "fomod_logger_plus_docs/python-LICENSE.txt": "third_party/python-3.12.3/LICENSE.txt",
    "fomod_logger_plus_docs/schemas/installation-event.schema.json": "schemas/installation-event.schema.json",
    "fomod_logger_plus_docs/schemas/choices-file.schema.json": "schemas/choices-file.schema.json",
    "fomod_logger_plus_docs/packaging/sqlite-runtime.json": "packaging/sqlite-runtime.json",
}
SQLITE_DESTINATION = "plugins/fomod_logger_support/native/sqlite3.dll"


def collect_plus_binary_files(*, source_files: Mapping[str, bytes], sqlite_bytes: bytes,
                              expected_sqlite_sha256: str) -> dict[str, bytes]:
    """Select the package from exact commit blobs plus the verified SQLite runtime."""

    if common.sha256(sqlite_bytes) != expected_sqlite_sha256:
        raise ValueError("sqlite3.dll does not match packaging/sqlite-runtime.json")
    files = {
        PLUGIN_DESTINATION: common._required_commit_blob(source_files, PLUGIN_SOURCE),
        SQLITE_DESTINATION: sqlite_bytes,
    }
    for destination, source in DOC_FILES.items():
        files[destination] = common._required_commit_blob(source_files, source)
    prefix = "python/fomod_logger_support/"
    for source, data in sorted(source_files.items()):
        if not source.startswith(prefix):
            continue
        relative = PurePosixPath(source[len(prefix):])
        if "__pycache__" in relative.parts or relative.suffix == ".pyc":
            raise RuntimeError(f"tracked Python cache file cannot ship: {source}")
        files[f"plugins/fomod_logger_support/{relative.as_posix()}"] = data
    return files


def build_plus_source_archive_bytes(source_files: Mapping[str, bytes], *, commit: str) -> bytes:
    prefixed = {f"{SOURCE_ARCHIVE_ROOT}/{PurePosixPath(name).as_posix()}": data for name, data in source_files.items()}
    manifest = {
        "format": 1, "product": PRODUCT_NAME, "version": PACKAGE_VERSION, "repository_commit": commit,
        "files": {name: common.sha256(data) for name, data in sorted(source_files.items())},
    }
    prefixed[f"{SOURCE_ARCHIVE_ROOT}/fomod_logger_plus-source-manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return common.zip_bytes(prefixed)


def build_plus_binary_archive_bytes(files: Mapping[str, bytes], *, commit: str, source_filename: str,
                                    source_sha256: str) -> bytes:
    manifest = {
        "format": 1, "product": PRODUCT_NAME, "version": PACKAGE_VERSION, "author": common.PACKAGE_AUTHOR,
        "repository_commit": commit,
        "corresponding_source": {"archive": source_filename, "sha256": source_sha256},
        "files": {name: common.sha256(data) for name, data in sorted(files.items())},
    }
    packaged = dict(files)
    packaged["fomod_logger_plus-manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return common.zip_bytes(packaged)


def build(sqlite_dll: Path, output: Path, *, expected_commit: str) -> dict[str, object]:
    common._validate_commit_identity(expected_commit, label="expected commit")
    output = Path(output)
    if output.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("release archive outputs must be outside the exact source checkout")
    binary_output, source_output = output / BINARY_ARCHIVE_NAME, output / SOURCE_ARCHIVE_NAME
    for candidate in (binary_output, source_output):
        if candidate.exists():
            raise FileExistsError(f"refusing to overwrite existing output: {candidate}")
    commit = common.repository_commit(ROOT, expected_commit=expected_commit, verifier_commit=expected_commit)
    source_files = common.collect_tracked_source_files(ROOT, commit=commit)
    lock = json.loads(common._required_commit_blob(source_files, "packaging/sqlite-runtime.json").decode("utf-8"))
    binary_files = collect_plus_binary_files(source_files=source_files, sqlite_bytes=Path(sqlite_dll).read_bytes(),
                                             expected_sqlite_sha256=lock["file_sha256"])
    source_archive = build_plus_source_archive_bytes(source_files, commit=commit)
    binary_archive = build_plus_binary_archive_bytes(
        binary_files, commit=commit, source_filename=SOURCE_ARCHIVE_NAME, source_sha256=common.sha256(source_archive))
    if common.repository_commit(ROOT, expected_commit=expected_commit, verifier_commit=expected_commit) != commit:
        raise RuntimeError("repository changed while release archives were assembled")
    output.mkdir(parents=True, exist_ok=True)
    common._write_pair_without_overwrite(((source_output, source_archive), (binary_output, binary_archive)))
    return {"output": str(binary_output), "sha256": common.sha256(binary_archive), "file_count": len(binary_files) + 1,
            "source_output": str(source_output), "source_sha256": common.sha256(source_archive),
            "repository_commit": commit}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--sqlite-dll", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="a folder for the two archives")
    args = parser.parse_args()
    print(json.dumps(build(args.sqlite_dll, args.output, expected_commit=args.expected_commit), indent=2))


if __name__ == "__main__":
    main()
