"""Verify an exact clean Git worktree without trusting or mutating its index."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath


class SourceIdentityError(RuntimeError):
    """The supplied worktree is not exact clean source at the claimed commit."""


def _validate_commit(commit: str) -> str:
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise SourceIdentityError(
            f"expected commit must be lowercase 40-hex, found {commit!r}"
        )
    return commit


def _absolute_non_reparse_root(root: Path) -> Path:
    supplied_root = root.absolute()
    if not supplied_root.is_dir():
        raise SourceIdentityError(f"source root is not a directory: {supplied_root}")
    if _is_reparse(supplied_root):
        raise SourceIdentityError("source root must not be a reparse point")
    return supplied_root.resolve()


def _json_object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SourceIdentityError(f"source manifest repeats JSON key {key!r}")
        result[key] = value
    return result


def _canonical_manifest_path(name: str) -> PurePosixPath:
    if not name or "\\" in name or ":" in name:
        raise SourceIdentityError(f"unsafe source manifest path: {name!r}")
    normalized = PurePosixPath(name)
    if (
        normalized.is_absolute()
        or normalized.as_posix() != name
        or any(part in {"", ".", ".."} for part in normalized.parts)
    ):
        raise SourceIdentityError(f"unsafe source manifest path: {name!r}")
    return normalized


def _is_reparse(path: Path) -> bool:
    if path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _verify_filesystem_tree(
    root: Path,
    expected_files: set[str],
    *,
    allow_git_control: bool,
    extra_files: set[str] | None = None,
) -> None:
    if _is_reparse(root):
        raise SourceIdentityError("source root must not be a reparse point")
    expected_files = set(expected_files)
    expected_files.update(extra_files or set())
    expected_directories: set[str] = set()
    for name in expected_files:
        parent = PurePosixPath(name).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    git_control = root / ".git"
    if allow_git_control:
        if not git_control.exists() or _is_reparse(git_control):
            raise SourceIdentityError(
                "source root has an unavailable or redirected .git entry"
            )
    elif git_control.exists() or git_control.is_symlink():
        raise SourceIdentityError("manifest-bound source must not contain .git")

    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for current_text, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current = Path(current_text)
        if current == root and allow_git_control:
            directory_names[:] = [name for name in directory_names if name != ".git"]
            file_names = [name for name in file_names if name != ".git"]
        for directory_name in list(directory_names):
            directory = current / directory_name
            relative = directory.relative_to(root).as_posix()
            if _is_reparse(directory):
                raise SourceIdentityError(
                    f"source tree contains a reparse directory: {relative!r}"
                )
            actual_directories.add(relative)
        for file_name in file_names:
            file_path = current / file_name
            relative = file_path.relative_to(root).as_posix()
            if _is_reparse(file_path) or not file_path.is_file():
                raise SourceIdentityError(
                    f"source tree contains a non-regular file: {relative!r}"
                )
            actual_files.add(relative)
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        raise SourceIdentityError(
            f"source file set does not match the claimed source; "
            f"unexpected={unexpected[:5]!r}, missing={missing[:5]!r}"
        )
    if actual_directories != expected_directories:
        unexpected = sorted(actual_directories - expected_directories)
        missing = sorted(expected_directories - actual_directories)
        raise SourceIdentityError(
            f"source directory set does not match the claimed source; "
            f"unexpected={unexpected[:5]!r}, missing={missing[:5]!r}"
        )


def verify_manifest_source(root: Path, expected_commit: str) -> dict[str, object]:
    root = _absolute_non_reparse_root(root)
    expected_commit = _validate_commit(expected_commit)
    manifest_path = root / "fomod_logger-source-manifest.json"
    if not manifest_path.is_file() or _is_reparse(manifest_path):
        raise SourceIdentityError("source manifest is unavailable or redirected")
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceIdentityError("source manifest is invalid") from exc
    if manifest.get("repository_commit") != expected_commit:
        raise SourceIdentityError("source manifest commit does not match expected commit")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise SourceIdentityError("source manifest files record is invalid")
    expected_files: set[str] = set()
    for name, expected_sha in files.items():
        if not isinstance(name, str) or not isinstance(expected_sha, str):
            raise SourceIdentityError("source manifest contains an invalid file record")
        normalized = _canonical_manifest_path(name)
        normalized_name = normalized.as_posix()
        path = root / Path(*normalized.parts)
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise SourceIdentityError(
                f"source manifest path escapes the source root: {name!r}"
            ) from exc
        if not path.is_file() or _is_reparse(path):
            raise SourceIdentityError(f"source manifest file is unavailable: {name!r}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_sha:
            raise SourceIdentityError(f"source manifest hash mismatch: {name!r}")
        expected_files.add(normalized_name)
    _verify_filesystem_tree(
        root,
        expected_files,
        allow_git_control=False,
        extra_files={"fomod_logger-source-manifest.json"},
    )
    return {
        "commit": expected_commit,
        "root": str(root),
        "tracked_file_count": len(expected_files),
    }


def _git(
    root: Path,
    *arguments: str,
    git_executable: str,
    environment: dict[str, str] | None = None,
    check: bool = True,
    input_data: str | bytes | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    command = [git_executable, "-C", str(root), *arguments]
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        env=environment,
        input=input_data,
        text=text,
    )


def verify_git_source(
    root: Path,
    expected_commit: str | None = None,
    *,
    git_executable: str = "git",
) -> dict[str, object]:
    """Return verified identity after comparing tracked worktree bytes to HEAD.

    Ordinary status can trust cached stat data, assume-unchanged, skip-worktree,
    or a filesystem monitor. A fresh temporary index loaded directly from the
    claimed commit has none of that state. A forced refresh must compare the
    actual worktree to those commit blobs using Git's normal clean-filter
    semantics before diff-files can report equality. The repository's real
    index and object database are never written.
    """

    root = _absolute_non_reparse_root(root)
    base_environment = os.environ.copy()
    for variable in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_QUARANTINE_PATH",
    ):
        base_environment.pop(variable, None)
    for variable in tuple(base_environment):
        if variable.startswith("GIT_CONFIG_KEY_") or variable.startswith(
            "GIT_CONFIG_VALUE_"
        ):
            base_environment.pop(variable, None)
    base_environment.pop("GIT_CONFIG_COUNT", None)
    base_environment["GIT_OPTIONAL_LOCKS"] = "0"
    base_environment["GIT_NO_REPLACE_OBJECTS"] = "1"

    try:
        top_level_text = _git(
            root,
            "rev-parse",
            "--show-toplevel",
            git_executable=git_executable,
            environment=base_environment,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceIdentityError(f"unable to resolve Git source root at {root}") from exc
    top_level = Path(top_level_text).resolve()
    if top_level != root:
        raise SourceIdentityError(
            f"Git top-level {top_level} does not match source root {root}"
        )

    commit = _git(
        root,
        "rev-parse",
        "--verify",
        "HEAD",
        git_executable=git_executable,
        environment=base_environment,
    ).stdout.strip()
    _validate_commit(commit)
    if expected_commit is not None:
        _validate_commit(expected_commit)
    if expected_commit is not None and commit != expected_commit:
        raise SourceIdentityError(
            f"Git HEAD {commit} does not match expected commit {expected_commit}"
        )

    index_listing = _git(
        root,
        "ls-files",
        "-v",
        "-z",
        git_executable=git_executable,
        environment=base_environment,
        text=False,
    ).stdout
    tracked_entries = [entry for entry in index_listing.split(b"\0") if entry]
    exceptional_entries = [
        os.fsdecode(entry[2:])
        for entry in tracked_entries
        if not entry.startswith(b"H ")
    ]
    if exceptional_entries:
        preview = ", ".join(repr(name) for name in exceptional_entries[:5])
        raise SourceIdentityError(
            "Git index has non-normal tracked flags (assume-unchanged, "
            f"skip-worktree, or another exceptional state): {preview}"
        )

    tree_listing = _git(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit,
        git_executable=git_executable,
        environment=base_environment,
        text=False,
    ).stdout
    tree_paths: list[bytes] = []
    for entry in (item for item in tree_listing.split(b"\0") if item):
        metadata, path = entry.split(b"\t", 1)
        mode, object_type, _object_id = metadata.decode("ascii").split(" ")
        if mode not in {"100644", "100755"} or object_type != "blob":
            raise SourceIdentityError(
                "source commit contains an unsupported Git entry "
                f"{os.fsdecode(path)!r} ({mode} {object_type})"
            )
        tree_paths.append(path)
    index_paths = [entry[2:] for entry in tracked_entries]
    if sorted(index_paths) != sorted(tree_paths):
        raise SourceIdentityError("Git index file set does not match the claimed commit")

    if tree_paths:
        attribute_input = b"\0".join(tree_paths) + b"\0"
        attribute_output = _git(
            root,
            "check-attr",
            "-z",
            "--stdin",
            "filter",
            "working-tree-encoding",
            "ident",
            git_executable=git_executable,
            environment=base_environment,
            input_data=attribute_input,
            text=False,
        ).stdout
        attribute_fields = attribute_output.split(b"\0")
        if attribute_fields and attribute_fields[-1] == b"":
            attribute_fields.pop()
        if len(attribute_fields) % 3:
            raise SourceIdentityError("Git returned malformed attribute output")
        for field_index in range(0, len(attribute_fields), 3):
            path, attribute, value = attribute_fields[field_index : field_index + 3]
            if value != b"unspecified":
                raise SourceIdentityError(
                    "source verification forbids effective "
                    f"{os.fsdecode(attribute)}={os.fsdecode(value)} on "
                    f"{os.fsdecode(path)!r}"
                )

    expected_files = {os.fsdecode(path).replace("\\", "/") for path in tree_paths}
    expected_directories: set[str] = set()
    for name in expected_files:
        parent = PurePosixPath(name).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    def is_reparse(path: Path) -> bool:
        if path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

    git_control = root / ".git"
    if not git_control.exists() or is_reparse(git_control):
        raise SourceIdentityError("source root has an unavailable or redirected .git entry")

    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for current_text, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current = Path(current_text)
        if current == root:
            directory_names[:] = [name for name in directory_names if name != ".git"]
            file_names = [name for name in file_names if name != ".git"]
        for directory_name in list(directory_names):
            directory = current / directory_name
            relative = directory.relative_to(root).as_posix()
            if is_reparse(directory):
                raise SourceIdentityError(
                    f"source tree contains a reparse directory: {relative!r}"
                )
            actual_directories.add(relative)
        for file_name in file_names:
            file_path = current / file_name
            relative = file_path.relative_to(root).as_posix()
            if is_reparse(file_path) or not file_path.is_file():
                raise SourceIdentityError(
                    f"source tree contains a non-regular file: {relative!r}"
                )
            actual_files.add(relative)
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        raise SourceIdentityError(
            f"source file set does not match the claimed commit; "
            f"unexpected={unexpected[:5]!r}, missing={missing[:5]!r}"
        )
    if actual_directories != expected_directories:
        unexpected = sorted(actual_directories - expected_directories)
        missing = sorted(expected_directories - actual_directories)
        raise SourceIdentityError(
            f"source directory set does not match the claimed commit; "
            f"unexpected={unexpected[:5]!r}, missing={missing[:5]!r}"
        )

    status = _git(
        root,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "status",
        "--porcelain",
        "--untracked-files=all",
        git_executable=git_executable,
        environment=base_environment,
    ).stdout
    if status:
        raise SourceIdentityError(
            f"refusing to use dirty Git source at {root}:\n{status.rstrip()}"
        )

    with tempfile.TemporaryDirectory(prefix="fomod-logger-source-verify-") as temporary:
        temporary_index = Path(temporary) / "index"
        isolated_environment = base_environment.copy()
        isolated_environment["GIT_INDEX_FILE"] = str(temporary_index)
        _git(
            root,
            "read-tree",
            commit,
            git_executable=git_executable,
            environment=isolated_environment,
        )
        refresh = _git(
            root,
            "update-index",
            "--really-refresh",
            git_executable=git_executable,
            environment=isolated_environment,
            check=False,
        )
        if refresh.returncode not in {0, 1}:
            raise SourceIdentityError(
                "unable to refresh the isolated source index: "
                + refresh.stderr.strip()
            )
        comparison = _git(
            root,
            "diff-files",
            "--quiet",
            "--ignore-submodules=none",
            "--",
            git_executable=git_executable,
            environment=isolated_environment,
            check=False,
        )
        if comparison.returncode == 1:
            changed = _git(
                root,
                "diff-files",
                "--name-status",
                "--ignore-submodules=none",
                "--",
                git_executable=git_executable,
                environment=isolated_environment,
            ).stdout.rstrip()
            raise SourceIdentityError(
                "tracked worktree bytes or modes do not match the claimed commit"
                + (f":\n{changed}" if changed else "")
            )
        if comparison.returncode != 0:
            raise SourceIdentityError(
                "unable to compare tracked worktree bytes to the claimed commit: "
                + comparison.stderr.strip()
            )

    final_commit = _git(
        root,
        "rev-parse",
        "--verify",
        "HEAD",
        git_executable=git_executable,
        environment=base_environment,
    ).stdout.strip()
    if final_commit != commit:
        raise SourceIdentityError("Git HEAD changed during source verification")

    return {
        "commit": commit,
        "root": str(root),
        "tracked_file_count": len(tracked_entries),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--git-executable", default="git")
    parser.add_argument(
        "--manifest",
        action="store_true",
        help="verify an extracted source tree against its source manifest",
    )
    arguments = parser.parse_args()
    try:
        if arguments.manifest:
            if arguments.expected_commit is None:
                raise SourceIdentityError(
                    "manifest verification requires --expected-commit"
                )
            result = verify_manifest_source(
                arguments.root,
                arguments.expected_commit,
            )
        else:
            result = verify_git_source(
                arguments.root,
                arguments.expected_commit,
                git_executable=arguments.git_executable,
            )
    except (OSError, subprocess.CalledProcessError, SourceIdentityError) as exc:
        parser.exit(1, f"source verification failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
