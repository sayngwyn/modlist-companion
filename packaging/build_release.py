"""Build deterministic FOMOD Logger binary and corresponding-source archives."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
IDENTITY = json.loads((ROOT / "release_identity.json").read_text(encoding="utf-8"))
NATIVE_DEPENDENCIES = json.loads(
    (ROOT / "packaging" / "native-dependencies.json").read_text(encoding="utf-8")
)
RUNTIME_SOURCES = json.loads(
    (ROOT / "packaging" / "runtime-sources.json").read_text(encoding="utf-8")
)
PRODUCT_NAME = IDENTITY["product"]
PACKAGE_VERSION = IDENTITY["public_semver"]
PACKAGE_AUTHOR = IDENTITY["author"]
MO2_VERSION = IDENTITY["mo2_version"]
BINARY_ARCHIVE_NAME = IDENTITY["binary_archive"]
SOURCE_ARCHIVE_NAME = IDENTITY["source_archive"]
SOURCE_ARCHIVE_ROOT = f"modlist-companion-{PACKAGE_VERSION}-source"
UIBASE_COMMIT = NATIVE_DEPENDENCIES["uibase"]["commit"]
UIBASE_SOURCE_PREFIX = NATIVE_DEPENDENCIES["uibase"]["source_archive_prefix"]
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
GIT_EXECUTABLE = shutil.which("git")

BINARY_COMMIT_FILES = {
    "plugins/fomod_logger.py": "python/fomod_logger_plugin.py",
    "fomod_logger_docs/README.md": "README.md",
    "fomod_logger_docs/CHANGELOG.md": "CHANGELOG.md",
    "fomod_logger_docs/LICENSE": "LICENSE",
    "fomod_logger_docs/MODIFICATIONS.md": "MODIFICATIONS.md",
    "fomod_logger_docs/THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
    "fomod_logger_docs/release_identity.json": "release_identity.json",
    "fomod_logger_docs/docs/BUILD_CONTRACT.md": "docs/BUILD_CONTRACT.md",
    "fomod_logger_docs/docs/INSTALL_UPGRADE_ROLLBACK.md": (
        "docs/INSTALL_UPGRADE_ROLLBACK.md"
    ),
    "fomod_logger_docs/docs/PROVENANCE.md": "docs/PROVENANCE.md",
    "fomod_logger_docs/docs/THIRD_PARTY.md": "docs/THIRD_PARTY.md",
    "fomod_logger_docs/schemas/installation-event.schema.json": (
        "schemas/installation-event.schema.json"
    ),
    "fomod_logger_docs/schemas/choices-file.schema.json": (
        "schemas/choices-file.schema.json"
    ),
    "fomod_logger_docs/packaging/native-dependencies.json": (
        "packaging/native-dependencies.json"
    ),
    "fomod_logger_docs/packaging/runtime-sources.json": (
        "packaging/runtime-sources.json"
    ),
    "fomod_logger_docs/packaging/sqlite-runtime.json": (
        "packaging/sqlite-runtime.json"
    ),
    "fomod_logger_docs/third_party/licenses/LGPL-3.0.txt": (
        "third_party/licenses/LGPL-3.0.txt"
    ),
    "fomod_logger_docs/third_party/python-3.12.3/LICENSE.txt": (
        "third_party/python-3.12.3/LICENSE.txt"
    ),
    "fomod_logger_docs/docs/UIBASE_DEPENDENCY.md": "docs/UIBASE_DEPENDENCY.md",
    "fomod_logger_docs/third_party/uibase-license-evidence/vcpkg.json": "third_party/uibase-license-evidence/vcpkg.json",
    "fomod_logger_docs/third_party/uibase-license-evidence/portfile.cmake": "third_party/uibase-license-evidence/portfile.cmake",
    "fomod_logger_docs/third_party/uibase-license-evidence/provenance.json": "third_party/uibase-license-evidence/provenance.json",
    "fomod_logger_docs/third_party/uibase-license-evidence/consumed-header-comparison.json": "third_party/uibase-license-evidence/consumed-header-comparison.json",
}

# Compatibility is deliberately a post-standalone workstream. Its research
# source remains in corresponding source, but it is not a standalone runtime.
DEFERRED_BINARY_SUPPORT_FILES = {PurePosixPath("core/fomod_plus.py")}
EXPECTED_BUILD_RECORD = {
    "format": 2,
    "product": PRODUCT_NAME,
    "public_version": PACKAGE_VERSION,
    "mo2_version": MO2_VERSION,
    "release_build": True,
    "configuration": "RelWithDebInfo",
    "architecture": "x86-64",
    "mo2_abi_target": "2.5.2",
    "generator": "Visual Studio 17 2022",
    "generator_platform": "x64",
    "generator_toolset": "v143",
    "cxx_compiler_id": "MSVC",
    "windows_sdk": "10.0.22621.0",
    "qt_version": "6.7.1",
    "qt_modules": ["Core", "Gui", "Widgets"],
    "uibase_commit": UIBASE_COMMIT,
    "native_dll_name": "fomod_logger.dll",
    "uibase_import_library_name": "uibase.lib",
    "uibase_companion_dll_name": "uibase.dll",
    "uibase_companion_role": (
        "build-prefix comparison only; not isolated-MO2 host qualification"
    ),
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for variable in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_QUARANTINE_PATH",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_EXEC_PATH",
    ):
        environment.pop(variable, None)
    for variable in tuple(environment):
        if variable.startswith("GIT_CONFIG_KEY_") or variable.startswith(
            "GIT_CONFIG_VALUE_"
        ):
            environment.pop(variable, None)
    environment.pop("GIT_CONFIG_COUNT", None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def _validate_commit_identity(commit: str, *, label: str) -> str:
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise ValueError(f"{label} must be a lowercase 40-hex Git commit")
    return commit


def _git_identity(root: Path) -> tuple[Path, str]:
    if GIT_EXECUTABLE is None:
        raise RuntimeError("Git is required for release source verification")
    environment = _clean_git_environment()
    top_level = Path(
        subprocess.run(
            [GIT_EXECUTABLE, "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            env=environment,
            text=True,
        ).stdout.strip()
    ).resolve()
    resolved_root = root.resolve()
    if top_level != resolved_root:
        raise RuntimeError(
            f"Git top-level {top_level} does not match source root {resolved_root}"
        )
    commit = subprocess.run(
        [GIT_EXECUTABLE, "-C", str(root), "rev-parse", "--verify", "HEAD"],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    ).stdout.strip()
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise RuntimeError(f"Git returned an invalid commit identity: {commit!r}")
    return top_level, commit


def repository_commit(
    root: Path = ROOT,
    *,
    expected_commit: str | None = None,
    verifier_root: Path | None = None,
    verifier_commit: str | None = None,
) -> str:
    if GIT_EXECUTABLE is None:
        raise RuntimeError("Git is required for release source verification")
    _target_root, target_commit = _git_identity(root)
    if expected_commit is not None:
        _validate_commit_identity(expected_commit, label="expected commit")
        if target_commit != expected_commit:
            raise RuntimeError(
                f"Git HEAD {target_commit} does not match expected commit "
                f"{expected_commit}"
            )
    if verifier_root is None:
        verifier_root = root
    verifier_root, discovered_verifier_commit = _git_identity(verifier_root)
    if verifier_commit is None:
        verifier_commit = discovered_verifier_commit
    _validate_commit_identity(verifier_commit, label="verifier commit")
    if discovered_verifier_commit != verifier_commit:
        raise RuntimeError("Git verifier source is not at the expected commit")

    environment = _clean_git_environment()
    with tempfile.TemporaryDirectory(prefix="fomod-logger-driver-") as temporary:
        verifier = Path(temporary) / "verify-git-source.py"
        verifier_bytes = subprocess.run(
            [
                GIT_EXECUTABLE,
                "-C",
                str(verifier_root),
                "show",
                f"{verifier_commit}:packaging/verify-git-source.py",
            ],
            check=True,
            capture_output=True,
            env=environment,
        ).stdout
        verifier.write_bytes(verifier_bytes)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(verifier),
                "--root",
                str(root),
                "--expected-commit",
                target_commit,
                "--git-executable",
                GIT_EXECUTABLE,
            ],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(message or f"Git source verification failed at {root}")
    try:
        verification = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Git source verifier returned invalid output") from exc
    commit = verification.get("commit", "")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise RuntimeError(f"Git returned an invalid commit identity: {commit!r}")
    verified_root = verification.get("root")
    tracked_file_count = verification.get("tracked_file_count")
    if Path(str(verified_root)).resolve() != root.resolve():
        raise RuntimeError("Git source verifier returned a mismatched source root")
    if not isinstance(tracked_file_count, int) or tracked_file_count < 1:
        raise RuntimeError("Git source verifier returned an invalid tracked-file count")
    return commit


def collect_tracked_source_files(
    root: Path = ROOT, *, commit: str = "HEAD"
) -> dict[str, bytes]:
    """Return every tracked file exactly as stored by one Git commit."""

    if GIT_EXECUTABLE is None:
        raise RuntimeError("Git is required to collect release source blobs")
    listing = subprocess.run(
        [GIT_EXECUTABLE, "-C", str(root), "ls-tree", "-r", "-z", commit],
        check=True,
        capture_output=True,
        env=_clean_git_environment(),
    )
    files: dict[str, bytes] = {}
    for item in listing.stdout.split(b"\0"):
        if not item:
            continue
        metadata, encoded_name = item.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ")
        name = encoded_name.decode("utf-8")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise RuntimeError(
                f"source snapshot contains unsupported Git entry {name!r} "
                f"({mode} {object_type})"
            )
        if name in files:
            raise RuntimeError(f"source snapshot repeats path {name!r}")
        files[name] = subprocess.run(
            [GIT_EXECUTABLE, "-C", str(root), "cat-file", "blob", object_id],
            check=True,
            capture_output=True,
            env=_clean_git_environment(),
        ).stdout
    return files


def collect_uibase_source_files(
    uibase_source: Path,
    *,
    verifier_root: Path | None = None,
    verifier_commit: str | None = None,
) -> dict[str, bytes]:
    actual_commit = repository_commit(
        uibase_source,
        expected_commit=UIBASE_COMMIT,
        verifier_root=verifier_root,
        verifier_commit=verifier_commit,
    )
    if actual_commit != UIBASE_COMMIT:
        raise RuntimeError(
            f"uibase commit {actual_commit} does not match pinned {UIBASE_COMMIT}"
        )
    return collect_tracked_source_files(uibase_source, commit=actual_commit)


def _required_commit_blob(source_files: Mapping[str, bytes], name: str) -> bytes:
    try:
        return source_files[name]
    except KeyError as exc:
        raise RuntimeError(f"release commit is missing required file {name}") from exc


def _locked_runtime_components(
    runtime_source_lock: Mapping[str, Any] = RUNTIME_SOURCES,
) -> list[dict[str, Any]]:
    components = runtime_source_lock.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("runtime source lock has no components")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("runtime source lock contains a non-object component")
        identifier = component.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("runtime source lock has an invalid or duplicate id")
        seen.add(identifier)
        for key in ("filename", "official_url", "sha256"):
            if not isinstance(component.get(key), str) or not component[key]:
                raise ValueError(f"runtime source {identifier} has invalid {key}")
        if PurePosixPath(component["filename"]).name != component["filename"]:
            raise ValueError(f"runtime source {identifier} filename is not basename-only")
        if not isinstance(component.get("size"), int) or component["size"] < 1:
            raise ValueError(f"runtime source {identifier} has invalid size")
        if len(component["sha256"]) != 64 or any(
            character not in "0123456789abcdef" for character in component["sha256"]
        ):
            raise ValueError(f"runtime source {identifier} has invalid SHA-256")
        normalized.append(json.loads(json.dumps(component, sort_keys=True)))
    return normalized


def validate_runtime_source_archives(
    paths: Mapping[str, Path],
    runtime_source_lock: Mapping[str, Any] = RUNTIME_SOURCES,
) -> list[dict[str, Any]]:
    components = _locked_runtime_components(runtime_source_lock)
    expected_ids = {component["id"] for component in components}
    if set(paths) != expected_ids:
        missing = sorted(expected_ids - set(paths))
        extra = sorted(set(paths) - expected_ids)
        raise ValueError(
            f"runtime source archive set mismatch; missing={missing!r}, extra={extra!r}"
        )
    for component in components:
        path = paths[component["id"]]
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"runtime source {component['id']} is not a regular file")
        if path.name != component["filename"]:
            raise ValueError(
                f"runtime source {component['id']} must be named {component['filename']}"
            )
        if path.stat().st_size != component["size"]:
            raise ValueError(f"runtime source {component['id']} size does not match lock")
        if file_sha256(path) != component["sha256"]:
            raise ValueError(f"runtime source {component['id']} SHA-256 does not match lock")
    return components


def collect_binary_files(
    *,
    source_files: Mapping[str, bytes],
    native_bytes: bytes,
    sqlite_bytes: bytes,
    build_record_bytes: bytes,
    expected_sqlite_sha256: str,
) -> dict[str, bytes]:
    """Select the standalone runtime from exact commit blobs plus verified binaries."""

    if sha256(sqlite_bytes) != expected_sqlite_sha256:
        raise ValueError("sqlite3.dll does not match packaging/sqlite-runtime.json")

    files = {
        "plugins/fomod_logger.dll": native_bytes,
        "plugins/fomod_logger_support/native/sqlite3.dll": sqlite_bytes,
        "fomod_logger_docs/native-build-record.json": build_record_bytes,
    }
    for destination, source in BINARY_COMMIT_FILES.items():
        files[destination] = _required_commit_blob(source_files, source)

    prefix = "python/fomod_logger_support/"
    for source, data in sorted(source_files.items()):
        if not source.startswith(prefix):
            continue
        relative = PurePosixPath(source[len(prefix) :])
        if relative in DEFERRED_BINARY_SUPPORT_FILES:
            continue
        if "__pycache__" in relative.parts or relative.suffix == ".pyc":
            raise RuntimeError(f"tracked Python cache file cannot ship: {source}")
        files[f"plugins/fomod_logger_support/{relative.as_posix()}"] = data
    return files


def zip_bytes(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    # Stored entries avoid zlib-version drift; inputs are already release-sized.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def build_source_archive_bytes(
    source_files: Mapping[str, bytes],
    *,
    uibase_source_files: Mapping[str, bytes],
    commit: str,
    runtime_source_components: list[dict[str, Any]],
) -> bytes:
    combined_files = dict(source_files)
    for name, data in uibase_source_files.items():
        destination = f"{UIBASE_SOURCE_PREFIX}/{PurePosixPath(name).as_posix()}"
        if destination in combined_files:
            raise RuntimeError(f"embedded uibase source collision: {destination}")
        combined_files[destination] = data

    prefixed = {
        f"{SOURCE_ARCHIVE_ROOT}/{PurePosixPath(name).as_posix()}": data
        for name, data in combined_files.items()
    }
    manifest = {
        "format": 2,
        "product": PRODUCT_NAME,
        "version": PACKAGE_VERSION,
        "repository_commit": commit,
        "embedded_git_sources": {
            "uibase": {
                "repository": NATIVE_DEPENDENCIES["uibase"]["repository"],
                "commit": UIBASE_COMMIT,
                "path": UIBASE_SOURCE_PREFIX,
            }
        },
        "required_external_sources": runtime_source_components,
        "external_source_publication_requirement": RUNTIME_SOURCES[
            "publication_policy"
        ],
        "files": {
            name: sha256(data) for name, data in sorted(combined_files.items())
        },
    }
    prefixed[f"{SOURCE_ARCHIVE_ROOT}/fomod_logger-source-manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return zip_bytes(prefixed)


def build_binary_archive_bytes(
    binary_files: Mapping[str, bytes],
    *,
    commit: str,
    source_filename: str,
    source_sha256: str,
    runtime_source_components: list[dict[str, Any]],
) -> bytes:
    files = dict(binary_files)
    manifest = {
        "format": 3,
        "product": PRODUCT_NAME,
        "version": PACKAGE_VERSION,
        "author": PACKAGE_AUTHOR,
        "repository_commit": commit,
        "corresponding_source": {
            "archive": source_filename,
            "sha256": source_sha256,
        },
        "required_external_sources": runtime_source_components,
        "external_source_publication_requirement": RUNTIME_SOURCES[
            "publication_policy"
        ],
        "native_build_record": "fomod_logger_docs/native-build-record.json",
        "files": {name: sha256(data) for name, data in sorted(files.items())},
    }
    files["fomod_logger-manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return zip_bytes(files)


def pe_machine(native_bytes: bytes) -> int:
    if len(native_bytes) < 0x40 or native_bytes[:2] != b"MZ":
        raise ValueError("native plugin is not a PE file")
    header_offset = struct.unpack_from("<I", native_bytes, 0x3C)[0]
    if header_offset + 6 > len(native_bytes):
        raise ValueError("native plugin has a truncated PE header")
    if native_bytes[header_offset : header_offset + 4] != b"PE\0\0":
        raise ValueError("native plugin has an invalid PE signature")
    return struct.unpack_from("<H", native_bytes, header_offset + 4)[0]


def read_native_version_metadata(native_plugin: Path) -> dict[str, Any]:
    """Read Windows version-resource fields without adding a package dependency."""

    script = r"""
$ErrorActionPreference = 'Stop'
$version = (Get-Item -LiteralPath $env:FOMOD_LOGGER_NATIVE_PLUGIN_PATH -ErrorAction Stop).VersionInfo
[ordered]@{
  FileVersion = $version.FileVersion
  ProductVersion = $version.ProductVersion
  PrivateBuild = $version.PrivateBuild
  OriginalFilename = $version.OriginalFilename
  IsDebug = $version.IsDebug
  IsPreRelease = $version.IsPreRelease
  IsPrivateBuild = $version.IsPrivateBuild
} | ConvertTo-Json -Compress
"""
    child_environment = os.environ.copy()
    child_environment["FOMOD_LOGGER_NATIVE_PLUGIN_PATH"] = str(
        native_plugin.resolve()
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=True,
        capture_output=True,
        env=child_environment,
        text=True,
    )
    return json.loads(result.stdout)


def validate_native_inputs(
    *,
    native_bytes: bytes,
    native_metadata: Mapping[str, Any],
    build_record: Mapping[str, Any],
    uibase_import_library_bytes: bytes,
    uibase_companion_dll_bytes: bytes,
    commit: str,
) -> None:
    if pe_machine(native_bytes) != 0x8664:
        raise ValueError("native plugin is not an x86-64 PE image")

    expected_metadata = {
        "FileVersion": PACKAGE_VERSION,
        "ProductVersion": PACKAGE_VERSION,
        "PrivateBuild": commit,
        "OriginalFilename": "fomod_logger.dll",
        "IsDebug": False,
        "IsPreRelease": False,
        "IsPrivateBuild": True,
    }
    for key, expected in expected_metadata.items():
        actual = native_metadata.get(key)
        if isinstance(actual, str):
            actual = actual.strip()
        if actual != expected:
            raise ValueError(
                f"native version resource {key}={actual!r}, expected {expected!r}"
            )

    for key, expected in EXPECTED_BUILD_RECORD.items():
        if build_record.get(key) != expected:
            raise ValueError(
                f"native build record {key}={build_record.get(key)!r}, "
                f"expected {expected!r}"
            )
    if build_record.get("repository_commit") != commit:
        raise ValueError("native build record commit does not match release commit")
    for required_identity in ("cxx_compiler_version", "cmake_version"):
        if not str(build_record.get(required_identity, "")):
            raise ValueError(f"native build record omits {required_identity}")

    expected_hashes = {
        "native_dll_sha256": sha256(native_bytes),
        "uibase_import_library_sha256": sha256(uibase_import_library_bytes),
        "uibase_companion_dll_sha256": sha256(uibase_companion_dll_bytes),
    }
    for key, expected in expected_hashes.items():
        if build_record.get(key) != expected:
            raise ValueError(
                f"native build record {key} does not match the supplied build input"
            )


def _write_pair_without_overwrite(
    outputs: tuple[tuple[Path, bytes], tuple[Path, bytes]]
) -> None:
    """Stage both outputs, then publish without overwriting or deleting paths."""

    staged: list[tuple[Path, Path]] = []
    try:
        for final, data in outputs:
            final.parent.mkdir(parents=True, exist_ok=True)
            temporary = final.with_name(f".{final.name}.{uuid.uuid4().hex}.tmp")
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            staged.append((temporary, final))

        for temporary, final in staged:
            # A hard-link publish is atomic and fails if another process created
            # the canonical output after the earlier existence check.
            os.link(temporary, final)
    finally:
        # Never roll back a published path: a concurrent process could replace
        # it between observation and deletion. A rare partial pair is reported
        # to the caller and left intact for explicit, identity-aware handling.
        for temporary, _final in staged:
            temporary.unlink(missing_ok=True)


def build(
    native_plugin: Path,
    native_build_record: Path,
    uibase_source: Path,
    uibase_import_library: Path,
    uibase_companion_dll: Path,
    sqlite_dll: Path,
    runtime_source_paths: Mapping[str, Path],
    output: Path,
    source_output: Path,
    *,
    expected_commit: str,
) -> dict[str, object]:
    _validate_commit_identity(expected_commit, label="expected commit")
    if output.resolve() == source_output.resolve():
        raise ValueError("binary and source outputs must be different paths")
    if output.name != BINARY_ARCHIVE_NAME:
        raise ValueError(f"binary output must be named {BINARY_ARCHIVE_NAME}")
    if source_output.name != SOURCE_ARCHIVE_NAME:
        raise ValueError(f"source output must be named {SOURCE_ARCHIVE_NAME}")
    resolved_root = ROOT.resolve()
    for candidate in (output, source_output):
        if candidate.resolve().is_relative_to(resolved_root):
            raise ValueError(
                "release archive outputs must be outside the exact source checkout"
            )
    for candidate in (output, source_output):
        if candidate.exists():
            raise FileExistsError(f"refusing to overwrite existing output: {candidate}")

    commit = repository_commit(
        ROOT,
        expected_commit=expected_commit,
        verifier_commit=expected_commit,
    )
    source_files = collect_tracked_source_files(ROOT, commit=commit)
    _required_commit_blob(source_files, "packaging/verify-git-source.py")
    identity_from_commit = json.loads(
        _required_commit_blob(source_files, "release_identity.json").decode("utf-8")
    )
    if identity_from_commit != IDENTITY:
        raise RuntimeError("release_identity.json does not match the release commit")
    native_dependencies_from_commit = json.loads(
        _required_commit_blob(source_files, "packaging/native-dependencies.json").decode(
            "utf-8"
        )
    )
    if native_dependencies_from_commit != NATIVE_DEPENDENCIES:
        raise RuntimeError("native-dependencies.json does not match the release commit")
    runtime_sources_from_commit = json.loads(
        _required_commit_blob(source_files, "packaging/runtime-sources.json").decode(
            "utf-8"
        )
    )
    if runtime_sources_from_commit != RUNTIME_SOURCES:
        raise RuntimeError("runtime-sources.json does not match the release commit")

    runtime_source_components = validate_runtime_source_archives(
        runtime_source_paths, runtime_sources_from_commit
    )
    uibase_source_files = collect_uibase_source_files(
        uibase_source,
        verifier_root=ROOT,
        verifier_commit=commit,
    )
    sqlite_lock = json.loads(
        _required_commit_blob(source_files, "packaging/sqlite-runtime.json").decode(
            "utf-8"
        )
    )
    native_bytes = native_plugin.read_bytes()
    uibase_import_library_bytes = uibase_import_library.read_bytes()
    uibase_companion_dll_bytes = uibase_companion_dll.read_bytes()
    build_record_bytes = native_build_record.read_bytes()
    build_record = json.loads(build_record_bytes.decode("utf-8"))
    validate_native_inputs(
        native_bytes=native_bytes,
        native_metadata=read_native_version_metadata(native_plugin),
        build_record=build_record,
        uibase_import_library_bytes=uibase_import_library_bytes,
        uibase_companion_dll_bytes=uibase_companion_dll_bytes,
        commit=commit,
    )

    source_archive = build_source_archive_bytes(
        source_files,
        uibase_source_files=uibase_source_files,
        commit=commit,
        runtime_source_components=runtime_source_components,
    )
    source_archive_sha256 = sha256(source_archive)
    binary_files = collect_binary_files(
        source_files=source_files,
        native_bytes=native_bytes,
        sqlite_bytes=sqlite_dll.read_bytes(),
        build_record_bytes=build_record_bytes,
        expected_sqlite_sha256=sqlite_lock["file_sha256"],
    )
    binary_archive = build_binary_archive_bytes(
        binary_files,
        commit=commit,
        source_filename=source_output.name,
        source_sha256=source_archive_sha256,
        runtime_source_components=runtime_source_components,
    )

    if repository_commit(
        ROOT,
        expected_commit=expected_commit,
        verifier_commit=expected_commit,
    ) != commit:
        raise RuntimeError("repository changed while release archives were assembled")
    if repository_commit(
        uibase_source,
        verifier_root=ROOT,
        verifier_commit=commit,
    ) != UIBASE_COMMIT:
        raise RuntimeError("uibase source changed while release archives were assembled")
    if native_plugin.read_bytes() != native_bytes:
        raise RuntimeError("native plugin changed while release archives were assembled")
    if uibase_import_library.read_bytes() != uibase_import_library_bytes:
        raise RuntimeError("uibase import library changed during packaging")
    if uibase_companion_dll.read_bytes() != uibase_companion_dll_bytes:
        raise RuntimeError("uibase companion DLL changed during packaging")
    validate_runtime_source_archives(runtime_source_paths, runtime_sources_from_commit)

    _write_pair_without_overwrite(
        ((source_output, source_archive), (output, binary_archive))
    )

    with zipfile.ZipFile(io.BytesIO(binary_archive)) as archive:
        binary_manifest = json.loads(
            archive.read("fomod_logger-manifest.json").decode("utf-8")
        )
    return {
        "output": str(output),
        "sha256": sha256(binary_archive),
        "file_count": len(binary_files) + 1,
        "source_output": str(source_output),
        "source_sha256": source_archive_sha256,
        "source_file_count": len(source_files) + len(uibase_source_files) + 1,
        "repository_commit": commit,
        "uibase_commit": UIBASE_COMMIT,
        "native_plugin_sha256": binary_manifest["files"][
            "plugins/fomod_logger.dll"
        ],
        "sqlite_sha256": binary_manifest["files"][
            "plugins/fomod_logger_support/native/sqlite3.dll"
        ],
        "required_external_source_count": len(runtime_source_components),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-plugin", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--native-build-record", required=True, type=Path)
    parser.add_argument("--uibase-source", required=True, type=Path)
    parser.add_argument("--uibase-import-library", required=True, type=Path)
    parser.add_argument("--uibase-companion-dll", required=True, type=Path)
    parser.add_argument("--sqlite-dll", required=True, type=Path)
    parser.add_argument("--qtbase-source", required=True, type=Path)
    parser.add_argument("--qtdeclarative-source", required=True, type=Path)
    parser.add_argument("--pyqt6-source", required=True, type=Path)
    parser.add_argument("--pyqt6-sip-source", required=True, type=Path)
    parser.add_argument("--sip-source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-output", required=True, type=Path)
    args = parser.parse_args()
    runtime_source_paths = {
        "qtbase": args.qtbase_source,
        "qtdeclarative": args.qtdeclarative_source,
        "pyqt6": args.pyqt6_source,
        "pyqt6-sip": args.pyqt6_sip_source,
        "sip": args.sip_source,
    }
    print(
        json.dumps(
            build(
                args.native_plugin,
                args.native_build_record,
                args.uibase_source,
                args.uibase_import_library,
                args.uibase_companion_dll,
                args.sqlite_dll,
                runtime_source_paths,
                args.output,
                args.source_output,
                expected_commit=args.expected_commit,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
