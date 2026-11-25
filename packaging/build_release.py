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
from typing import Any, NamedTuple


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
BINARY_MANIFEST_NAME = "fomod_logger-manifest.json"
# Members "reuse the shipped native build" mode takes from an earlier release
# package: the native DLL, its private SQLite runtime and its build record.
REUSED_NATIVE_MEMBERS = (
    "plugins/fomod_logger.dll",
    "plugins/fomod_logger_support/native/sqlite3.dll",
    "fomod_logger_docs/native-build-record.json",
)


# Paths whose content decides the bytes of the native DLL. Reuse mode refuses
# unless they are identical at the native commit and the release commit.
NATIVE_SOURCE_PATHS = (
    "src",
    "CMakeLists.txt",
    "CMakePresets.json",
    "third_party",
    "packaging/write-native-build-record.cmake",
    "packaging/native-dependencies.json",
    "packaging/write-resources.cmake",
)


class NativeIdentity(NamedTuple):
    """The earlier build a package's native DLL comes from."""

    version: str
    commit: str
    dll_sha256: str
    sqlite_sha256: str | None


def _validate_sha256_identity(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def native_identity(identity: Mapping[str, Any]) -> NativeIdentity | None:
    """Return the native build the identity names, or None when it names none.

    ``native_version``, ``native_commit`` and ``native_dll_sha256`` appear
    together or not at all; ``native_sqlite_sha256`` is optional beside them.
    They say which build the packaged native DLL comes from when it is not
    rebuilt for this package version. Without them every package is strict: the
    DLL must be built for this version at this commit.
    """

    required = ("native_version", "native_commit", "native_dll_sha256")
    present = [name in identity for name in required]
    if not any(present) and "native_sqlite_sha256" not in identity:
        return None
    if not all(present):
        raise ValueError(
            "release_identity.json needs native_version, native_commit and "
            "native_dll_sha256 together"
        )
    version = identity["native_version"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("release_identity.json native_version must be a version string")
    commit = identity["native_commit"]
    if not isinstance(commit, str):
        raise ValueError("native_commit must be a lowercase 40-hex Git commit")
    _validate_commit_identity(commit, label="native_commit")
    dll_sha256 = _validate_sha256_identity(
        identity["native_dll_sha256"], label="native_dll_sha256"
    )
    sqlite_sha256 = identity.get("native_sqlite_sha256")
    if sqlite_sha256 is not None:
        _validate_sha256_identity(sqlite_sha256, label="native_sqlite_sha256")
    return NativeIdentity(version, commit, dll_sha256, sqlite_sha256)


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


NATIVE_IDENTITY = native_identity(IDENTITY)


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
    native_binary_from: Mapping[str, str] | None = None,
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
    if native_binary_from is not None:
        # The native sources of this commit are identical to the sources the
        # packaged DLL was built from; say which build that was.
        manifest["native_binary_from"] = dict(native_binary_from)
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
    native_binary_from: Mapping[str, str] | None = None,
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
    if native_binary_from is not None:
        # plugins/fomod_logger.dll (and its build record) come from an earlier
        # release build, not from a build of this package's commit.
        manifest["native_binary_from"] = dict(native_binary_from)
    files[BINARY_MANIFEST_NAME] = (
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


def _validate_native_binary_and_record(
    *,
    native_bytes: bytes,
    native_metadata: Mapping[str, Any],
    build_record: Mapping[str, Any],
    commit: str,
    version: str,
    expected_record: Mapping[str, Any],
    commit_label: str,
) -> None:
    if pe_machine(native_bytes) != 0x8664:
        raise ValueError("native plugin is not an x86-64 PE image")

    expected_metadata = {
        "FileVersion": version,
        "ProductVersion": version,
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

    for key, expected in expected_record.items():
        if build_record.get(key) != expected:
            raise ValueError(
                f"native build record {key}={build_record.get(key)!r}, "
                f"expected {expected!r}"
            )
    if build_record.get("repository_commit") != commit:
        raise ValueError(
            f"native build record commit does not match {commit_label}"
        )
    for required_identity in ("cxx_compiler_version", "cmake_version"):
        if not str(build_record.get(required_identity, "")):
            raise ValueError(f"native build record omits {required_identity}")


def _check_record_hashes(
    build_record: Mapping[str, Any], expected_hashes: Mapping[str, str]
) -> None:
    for key, expected in expected_hashes.items():
        if build_record.get(key) != expected:
            raise ValueError(
                f"native build record {key} does not match the supplied build input"
            )


def validate_native_inputs(
    *,
    native_bytes: bytes,
    native_metadata: Mapping[str, Any],
    build_record: Mapping[str, Any],
    uibase_import_library_bytes: bytes,
    uibase_companion_dll_bytes: bytes,
    commit: str,
) -> None:
    """Strict check: a native DLL built for this package version at this commit."""

    _validate_native_binary_and_record(
        native_bytes=native_bytes,
        native_metadata=native_metadata,
        build_record=build_record,
        commit=commit,
        version=PACKAGE_VERSION,
        expected_record=EXPECTED_BUILD_RECORD,
        commit_label="release commit",
    )
    _check_record_hashes(
        build_record,
        {
            "native_dll_sha256": sha256(native_bytes),
            "uibase_import_library_sha256": sha256(uibase_import_library_bytes),
            "uibase_companion_dll_sha256": sha256(uibase_companion_dll_bytes),
        },
    )


def validate_reused_native_inputs(
    *,
    native_bytes: bytes,
    native_metadata: Mapping[str, Any],
    build_record: Mapping[str, Any],
    native_version: str,
    native_commit: str,
) -> None:
    """Check a native DLL reused from an earlier release build.

    The version resource and the record are held to the native version and
    commit, not to this package's. The uibase import library and companion DLL
    are not supplied: the record keeps their hashes from the original build
    (the record itself is unchanged), and only the DLL's own hash is re-proved.
    """

    _validate_native_binary_and_record(
        native_bytes=native_bytes,
        native_metadata=native_metadata,
        build_record=build_record,
        commit=native_commit,
        version=native_version,
        expected_record={
            **EXPECTED_BUILD_RECORD,
            "public_version": native_version,
            "mo2_version": native_version,
        },
        commit_label="native commit",
    )
    _check_record_hashes(build_record, {"native_dll_sha256": sha256(native_bytes)})
    for key in ("uibase_import_library_sha256", "uibase_companion_dll_sha256"):
        value = build_record.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError(f"native build record {key} is not a SHA-256")


class ReusedNative(NamedTuple):
    """The native parts taken, verified, from an earlier release package."""

    native_bytes: bytes
    sqlite_bytes: bytes
    build_record_bytes: bytes
    build_record: dict[str, Any]
    native_binary_from: dict[str, str]
    package_sha256: str


def read_native_from_release_zip(
    package: Path, *, identity: NativeIdentity
) -> ReusedNative:
    """Read the native DLL, SQLite runtime and build record out of a release zip.

    Nothing is extracted. The zip's own manifest must name the native build
    (its version and commit, or the ``native_binary_from`` it carries when it
    reused one itself) and hash all three members exactly. That only proves the
    zip agrees with itself, so the DLL (and the SQLite runtime, when the
    identity pins it) must also hash to the value committed in
    ``release_identity.json``.
    """

    native_version, native_commit = identity.version, identity.commit

    if package.is_symlink() or not package.is_file():
        raise ValueError(f"native release package is not a regular file: {package}")
    package_sha256 = file_sha256(package)
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("native release package repeats a member name")
        wanted = (*REUSED_NATIVE_MEMBERS, BINARY_MANIFEST_NAME)
        missing = [name for name in wanted if name not in names]
        if missing:
            raise ValueError(f"native release package lacks {missing!r}")
        data = {name: archive.read(name) for name in wanted}

    manifest = json.loads(data[BINARY_MANIFEST_NAME].decode("utf-8"))
    if manifest.get("product") != PRODUCT_NAME:
        raise ValueError("native release package is for a different product")
    origin = manifest.get("native_binary_from")
    if origin is None:
        origin = {
            "version": manifest.get("version"),
            "repository_commit": manifest.get("repository_commit"),
        }
    expected_origin = {"version": native_version, "repository_commit": native_commit}
    if origin != expected_origin:
        raise ValueError(
            f"native release package native build {origin!r} does not match "
            f"the release identity's {expected_origin!r}"
        )
    listed = manifest.get("files", {})
    for name in REUSED_NATIVE_MEMBERS:
        if listed.get(name) != sha256(data[name]):
            raise ValueError(
                f"{name} does not match the native release package's manifest"
            )

    if sha256(data[REUSED_NATIVE_MEMBERS[0]]) != identity.dll_sha256:
        raise ValueError(
            "native DLL SHA-256 does not match native_dll_sha256 in "
            "release_identity.json"
        )
    if (
        identity.sqlite_sha256 is not None
        and sha256(data[REUSED_NATIVE_MEMBERS[1]]) != identity.sqlite_sha256
    ):
        raise ValueError(
            "SQLite runtime SHA-256 does not match native_sqlite_sha256 in "
            "release_identity.json"
        )

    return ReusedNative(
        native_bytes=data[REUSED_NATIVE_MEMBERS[0]],
        sqlite_bytes=data[REUSED_NATIVE_MEMBERS[1]],
        build_record_bytes=data[REUSED_NATIVE_MEMBERS[2]],
        build_record=json.loads(data[REUSED_NATIVE_MEMBERS[2]].decode("utf-8")),
        native_binary_from=expected_origin,
        package_sha256=package_sha256,
    )


def check_native_sources_unchanged(
    root: Path, *, native_commit: str, release_commit: str
) -> None:
    """Refuse unless everything the native DLL is built from is unchanged.

    Reusing the DLL of ``native_commit`` is only honest when the files that
    decide its bytes are identical at ``release_commit``.
    """

    if GIT_EXECUTABLE is None:
        raise RuntimeError("Git is required to compare native sources")
    _validate_commit_identity(native_commit, label="native_commit")
    environment = _clean_git_environment()
    known = subprocess.run(
        [
            GIT_EXECUTABLE,
            "-C",
            str(root),
            "cat-file",
            "-e",
            f"{native_commit}^{{commit}}",
        ],
        check=False,
        capture_output=True,
        env=environment,
    )
    if known.returncode != 0:
        raise RuntimeError(
            f"native commit {native_commit} is not a commit in this repository"
        )
    result = subprocess.run(
        [
            GIT_EXECUTABLE,
            "-C",
            str(root),
            "diff",
            "--quiet",
            native_commit,
            release_commit,
            "--",
            *NATIVE_SOURCE_PATHS,
        ],
        check=False,
        capture_output=True,
        env=environment,
    )
    if result.returncode == 1:
        raise RuntimeError(
            f"native sources changed between {native_commit} and {release_commit}; "
            "the shipped native DLL cannot be reused (paths: "
            + ", ".join(NATIVE_SOURCE_PATHS)
            + ")"
        )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git diff of native sources failed: {message}")


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
    native_plugin: Path | None,
    native_build_record: Path | None,
    uibase_source: Path,
    uibase_import_library: Path | None,
    uibase_companion_dll: Path | None,
    sqlite_dll: Path | None,
    runtime_source_paths: Mapping[str, Path],
    output: Path,
    source_output: Path,
    *,
    expected_commit: str,
    native_from_release_zip: Path | None = None,
) -> dict[str, object]:
    """Build the paired archives.

    Strict (default): the five native inputs are a DLL built for this package
    version at ``expected_commit``, its build record, the uibase import library
    and companion DLL, and the SQLite runtime.

    Reuse (``native_from_release_zip``, and none of those five): the native
    DLL, SQLite runtime and build record come from an earlier release package,
    held to the release identity's ``native_version`` and ``native_commit``.
    """

    native_inputs = (
        native_plugin,
        native_build_record,
        uibase_import_library,
        uibase_companion_dll,
        sqlite_dll,
    )
    if native_from_release_zip is not None:
        if any(item is not None for item in native_inputs):
            raise ValueError(
                "--native-from-release-zip replaces the native plugin, build "
                "record, uibase import library, uibase companion DLL and "
                "SQLite inputs; do not pass them too"
            )
        if NATIVE_IDENTITY is None:
            raise ValueError(
                "reusing a native build needs native_version, native_commit and "
                "native_dll_sha256 in release_identity.json"
            )
    elif any(item is None for item in native_inputs):
        raise ValueError(
            "strict mode needs the native plugin, build record, uibase import "
            "library, uibase companion DLL and SQLite DLL "
            "(or --native-from-release-zip)"
        )
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
    native_binary_from: dict[str, str] | None = None
    if native_from_release_zip is not None:
        assert NATIVE_IDENTITY is not None  # checked above
        native_version, native_commit = (
            NATIVE_IDENTITY.version,
            NATIVE_IDENTITY.commit,
        )
        check_native_sources_unchanged(
            ROOT, native_commit=native_commit, release_commit=commit
        )
        reused = read_native_from_release_zip(
            native_from_release_zip, identity=NATIVE_IDENTITY
        )
        native_bytes = reused.native_bytes
        sqlite_bytes = reused.sqlite_bytes
        build_record_bytes = reused.build_record_bytes
        with tempfile.TemporaryDirectory(prefix="fomod-logger-native-") as scratch:
            scratch_dll = Path(scratch) / "fomod_logger.dll"
            scratch_dll.write_bytes(native_bytes)
            native_metadata = read_native_version_metadata(scratch_dll)
        validate_reused_native_inputs(
            native_bytes=native_bytes,
            native_metadata=native_metadata,
            build_record=reused.build_record,
            native_version=native_version,
            native_commit=native_commit,
        )
        native_binary_from = reused.native_binary_from
    else:
        assert native_plugin and native_build_record and sqlite_dll
        assert uibase_import_library and uibase_companion_dll
        native_bytes = native_plugin.read_bytes()
        uibase_import_library_bytes = uibase_import_library.read_bytes()
        uibase_companion_dll_bytes = uibase_companion_dll.read_bytes()
        sqlite_bytes = sqlite_dll.read_bytes()
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
        native_binary_from=native_binary_from,
    )
    source_archive_sha256 = sha256(source_archive)
    binary_files = collect_binary_files(
        source_files=source_files,
        native_bytes=native_bytes,
        sqlite_bytes=sqlite_bytes,
        build_record_bytes=build_record_bytes,
        expected_sqlite_sha256=sqlite_lock["file_sha256"],
    )
    binary_archive = build_binary_archive_bytes(
        binary_files,
        commit=commit,
        source_filename=source_output.name,
        source_sha256=source_archive_sha256,
        runtime_source_components=runtime_source_components,
        native_binary_from=native_binary_from,
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
    if native_from_release_zip is not None:
        if file_sha256(native_from_release_zip) != reused.package_sha256:
            raise RuntimeError(
                "native release package changed while release archives were assembled"
            )
    else:
        if native_plugin.read_bytes() != native_bytes:
            raise RuntimeError(
                "native plugin changed while release archives were assembled"
            )
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
    result: dict[str, object] = {
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
    if native_binary_from is not None:
        result["native_binary_from"] = native_binary_from
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-plugin", type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--native-build-record", type=Path)
    parser.add_argument("--uibase-source", required=True, type=Path)
    parser.add_argument("--uibase-import-library", type=Path)
    parser.add_argument("--uibase-companion-dll", type=Path)
    parser.add_argument("--sqlite-dll", type=Path)
    parser.add_argument(
        "--native-from-release-zip",
        type=Path,
        help=(
            "reuse the native DLL, SQLite runtime and build record of an earlier "
            "release package (its native build must match native_version and "
            "native_commit in release_identity.json); replaces --native-plugin, "
            "--native-build-record, --uibase-import-library, "
            "--uibase-companion-dll and --sqlite-dll"
        ),
    )
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
                native_from_release_zip=args.native_from_release_zip,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
