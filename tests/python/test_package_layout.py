import importlib.util
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_resource_generation_is_path_and_epoch_stable_and_preserves_good_output(tmp_path):
    cmake = shutil.which("cmake")
    if cmake is None:
        pytest.skip("CMake is required for resource generation")
    fake = tmp_path / "fake_rcc.py"
    fake.write_text(
        "import os, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "if pathlib.Path('fail').exists():\n"
        "    out.write_text('partial')\n"
        "    sys.exit(7)\n"
        "assert os.environ['SOURCE_DATE_EPOCH'] == '315532800'\n"
        "assert os.environ['QT_RCC_SOURCE_DATE_OVERRIDE'] == '315532800'\n"
        "root = pathlib.Path.cwd().as_posix()\n"
        "out.write_bytes(('// generated\\n  // ' + root + '/resources/icon.png\\n'\n"
        "    'static unsigned char payload[] = {0, 255, 17};\\n').encode('utf-8'))\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        executable = tmp_path / "fake_rcc.cmd"
        executable.write_text(
            f'@"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8"
        )
    else:
        executable = tmp_path / "fake_rcc"
        executable.write_text(
            f"#!{sys.executable}\n" + fake.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        executable.chmod(0o755)
    environment = os.environ.copy()
    environment.update(SOURCE_DATE_EPOCH="17", QT_RCC_SOURCE_DATE_OVERRIDE="23")
    expected = (
        "// generated\n  // resources/icon.png\n"
        "static unsigned char payload[] = {0, 255, 17};\n"
    ).replace("\n", os.linesep).encode()

    def generate(source, output):
        command = str(Path(".") / executable.name)
        if os.name != "nt":
            command = "./" + command
        return subprocess.run(
            [cmake, f"-DFOMOD_LOGGER_RCC={command}",
             f"-DFOMOD_LOGGER_RESOURCE_SOURCE_DIR={source}",
             f"-DFOMOD_LOGGER_RESOURCE_OUTPUT={output}",
             "-P", str(ROOT / "packaging" / "write-resources.cmake")],
            env=environment, capture_output=True, text=True,
        )

    for name in ("git source", "extracted source"):
        source = tmp_path / name
        source.mkdir()
        # CMake's -E env launcher does not quote Windows batch-file paths with
        # spaces like real .exe paths. Keep the fixture command relative to
        # its working directory; the production Qt rcc is an executable.
        shutil.copy2(executable, source / executable.name)
        output = tmp_path / (name + ".cpp")
        result = generate(source, output)
        assert result.returncode == 0, result.stderr
        assert output.read_bytes() == expected
        assert not output.with_suffix(".cpp.tmp").exists()
        (source / "fail").touch()
        failure = generate(source, output)
        assert failure.returncode != 0
        assert "Qt resource generation failed" in failure.stderr
        assert output.read_bytes() == expected


def _release_module():
    path = ROOT / "packaging" / "build_release.py"
    spec = importlib.util.spec_from_file_location("fomod_logger_build_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _worktree_source_files(release):
    names = set(release.BINARY_COMMIT_FILES.values())
    support_root = ROOT / "python" / "fomod_logger_support"
    for path in support_root.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            names.add(path.relative_to(ROOT).as_posix())
    return {name: (ROOT / name).read_bytes() for name in names}


def _minimal_x64_pe() -> bytes:
    payload = bytearray(0x100)
    payload[0:2] = b"MZ"
    payload[0x3C:0x40] = (0x80).to_bytes(4, "little")
    payload[0x80:0x84] = b"PE\0\0"
    payload[0x84:0x86] = (0x8664).to_bytes(2, "little")
    return bytes(payload)


def _native_fixture(release):
    commit = "d" * 40
    native_bytes = _minimal_x64_pe()
    import_library_bytes = b"pinned uibase import library"
    companion_dll_bytes = b"pinned companion uibase runtime"
    metadata = {
        "FileVersion": release.PACKAGE_VERSION,
        "ProductVersion": release.PACKAGE_VERSION,
        "PrivateBuild": commit,
        "OriginalFilename": "fomod_logger.dll",
        "IsDebug": False,
        "IsPreRelease": False,
        "IsPrivateBuild": True,
    }
    build_record = {
        **release.EXPECTED_BUILD_RECORD,
        "repository_commit": commit,
        "cxx_compiler_version": "19.44.35219.0",
        "cmake_version": "4.0.0",
        "native_dll_sha256": release.sha256(native_bytes),
        "uibase_import_library_sha256": release.sha256(import_library_bytes),
        "uibase_companion_dll_sha256": release.sha256(companion_dll_bytes),
    }
    return (
        commit,
        native_bytes,
        import_library_bytes,
        companion_dll_bytes,
        metadata,
        build_record,
    )


def _initialize_repository(git, directory: Path, files: dict[str, bytes]) -> str:
    directory.mkdir(parents=True)
    subprocess.run([git, "init", str(directory)], check=True, capture_output=True)
    for name, data in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    subprocess.run(
        [git, "-C", str(directory), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        [
            git,
            "-C",
            str(directory),
            "-c",
            "user.name=FOMOD Logger Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        [git, "-C", str(directory), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_storage_fingerprint(git: str, repository: Path) -> tuple[bytes, tuple]:
    environment = os.environ.copy()
    for variable in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(variable, None)
    git_dir_text = subprocess.run(
        [git, "-C", str(repository), "rev-parse", "--git-dir"],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    ).stdout.strip()
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = (repository / git_dir).resolve()
    index = git_dir / "index"
    index_bytes = index.read_bytes() if index.exists() else b""
    objects = tuple(
        (
            path.relative_to(git_dir / "objects").as_posix(),
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted((git_dir / "objects").rglob("*"))
        if path.is_file()
    )
    return index_bytes, objects


def _make_directory_reparse(link: Path, target: Path) -> None:
    if os.name == "nt":
        command = [
            os.environ.get("ComSpec", "cmd.exe"),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(link),
            str(target),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip(f"Windows junction creation is unavailable: {result.stderr}")
    else:
        link.symlink_to(target, target_is_directory=True)


def _run_cmake_source_gate(
    *,
    cmake: str,
    git: str,
    source: Path,
    commit: str,
    uibase: Path,
    uibase_commit: str,
    temporary_tools: Path,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            cmake,
            f"-DFOMOD_LOGGER_SOURCE_DIR={source}",
            f"-DFOMOD_LOGGER_EXPECTED_COMMIT={commit}",
            f"-DFOMOD_LOGGER_GIT_EXECUTABLE={git}",
            f"-DFOMOD_LOGGER_PYTHON_EXECUTABLE={sys.executable}",
            f"-DFOMOD_LOGGER_GIT_VERIFY_SCRIPT={source / 'packaging' / 'verify-git-source.py'}",
            f"-DFOMOD_LOGGER_VERIFY_TEMP_DIR={temporary_tools}",
            f"-DFOMOD_LOGGER_UIBASE_DIR={uibase}",
            f"-DFOMOD_LOGGER_UIBASE_EXPECTED_COMMIT={uibase_commit}",
            "-DFOMOD_LOGGER_UIBASE_EXPECTED_PREFIX=external/uibase",
            "-P",
            str(ROOT / "packaging" / "verify-release-source.cmake"),
        ],
        capture_output=True,
        text=True,
    )


def test_support_namespace_is_not_a_second_mo2_plugin() -> None:
    support = ROOT / "python" / "fomod_logger_support"
    assert support.is_dir()
    assert not (support / "__init__.py").exists()
    assert (support / "core" / "__init__.py").is_file()
    assert not list((ROOT / "python" / "fomod_logger_core").glob("*.py"))
    assert not list((ROOT / "python" / "fomod_logger_ui").glob("*.*"))


def test_sqlite_dll_search_path_is_registered_before_core_imports() -> None:
    source = (ROOT / "python" / "fomod_logger_plugin.py").read_text(
        encoding="utf-8"
    )
    add_directory = source.index("os.add_dll_directory")
    first_core_import = source.index("from fomod_logger_support.core")
    assert add_directory < first_core_import
    assert '_PLUGIN_ROOT / "fomod_logger_support"' in source


def test_native_snapshot_excludes_synthetic_none_control() -> None:
    source = (ROOT / "src" / "fomodinstallerdialog.cpp").read_text(
        encoding="utf-8"
    )
    snapshot = source[
        source.index("QJsonArray FomodInstallerDialog::selectionSnapshot") :
    ]
    snapshot = snapshot[: snapshot.index("void FomodInstallerDialog::highlightControl")]
    assert 'choice->objectName() != QStringLiteral("choice")' in snapshot
    assert 'QStringLiteral("none")' not in snapshot


def test_native_cmake_is_project_owned_and_does_not_execute_uibase() -> None:
    root_cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    source_cmake = (ROOT / "src" / "CMakeLists.txt").read_text(encoding="utf-8")
    presets = (ROOT / "CMakePresets.json").read_text(encoding="utf-8")

    assert "cmake_common" not in root_cmake
    assert "cmake_common" not in source_cmake
    assert "Boost" not in root_cmake
    assert "BOOST" not in presets
    assert '"binaryDir": "$env{FOMOD_LOGGER_BUILD_DIR}"' in presets
    parsed_presets = json.loads(presets)
    condition = parsed_presets["configurePresets"][0]["condition"]
    assert condition["type"] == "allOf"
    assert {entry["type"] for entry in condition["conditions"]} == {
        "notEquals",
        "matches",
    }
    assert '"${sourceDir}/release-build"' not in presets
    assert "add_subdirectory(${FOMOD_LOGGER_UIBASE" not in root_cmake
    assert "FomodLogger::uibase" in root_cmake
    assert "IMPORTED_IMPLIB" in root_cmake
    assert "IMPORTED_LOCATION" in root_cmake
    assert 'find_package(Qt6 6.7.1 EXACT REQUIRED COMPONENTS Core Gui Widgets)' in root_cmake
    for property_name in ("AUTOMOC ON", "AUTOUIC ON", "AUTOMOC_PATH_PREFIX ON"):
        assert property_name in source_cmake
    assert "packaging/write-resources.cmake" in source_cmake
    assert "CXX_STANDARD 20" in source_cmake
    assert "MSVC_DEBUG_INFORMATION_FORMAT" in source_cmake
    assert "/Zf" in source_cmake
    assert "$<TARGET_PDB_FILE_NAME:installer_fomod>" in source_cmake
    assert "%_PDB%" not in source_cmake
    assert "helper materialized from the expected Logger commit" in root_cmake
    assert "write-native-build-record.cmake" in source_cmake


def test_release_preset_is_disabled_without_an_absolute_external_build_dir(
    tmp_path,
) -> None:
    cmake = shutil.which("cmake")
    if cmake is None:
        pytest.skip("CMake is required for the preset condition gate")
    environment = os.environ.copy()
    environment.pop("FOMOD_LOGGER_BUILD_DIR", None)
    unset = subprocess.run(
        [cmake, "--list-presets"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "release-vs2022-x64" not in unset.stdout
    environment["FOMOD_LOGGER_BUILD_DIR"] = "relative-build"
    relative = subprocess.run(
        [cmake, "--list-presets"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "release-vs2022-x64" not in relative.stdout
    environment["FOMOD_LOGGER_BUILD_DIR"] = str(tmp_path / "external build")
    absolute = subprocess.run(
        [cmake, "--list-presets"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "release-vs2022-x64" in absolute.stdout
    assert not (ROOT / "$env{FOMOD_LOGGER_BUILD_DIR}").exists()
    assert not (ROOT / "relative-build").exists()


def test_standalone_binary_archive_has_release_contract_and_no_plus_adapter() -> None:
    release = _release_module()
    sqlite_bytes = b"test sqlite runtime"
    source_files = _worktree_source_files(release)
    build_record = b'{"test": "build record"}\n'
    runtime_sources = release._locked_runtime_components()
    files = release.collect_binary_files(
        source_files=source_files,
        native_bytes=b"test native plugin",
        sqlite_bytes=sqlite_bytes,
        build_record_bytes=build_record,
        expected_sqlite_sha256=release.sha256(sqlite_bytes),
    )
    archive_bytes = release.build_binary_archive_bytes(
        files,
        commit="a" * 40,
        source_filename=release.SOURCE_ARCHIVE_NAME,
        source_sha256="b" * 64,
        runtime_source_components=runtime_sources,
    )

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        required = {
            "fomod_logger_docs/README.md",
            "fomod_logger_docs/CHANGELOG.md",
            "fomod_logger_docs/LICENSE",
            "fomod_logger_docs/MODIFICATIONS.md",
            "fomod_logger_docs/THIRD_PARTY_NOTICES.md",
            "fomod_logger_docs/docs/BUILD_CONTRACT.md",
            "fomod_logger_docs/docs/INSTALL_UPGRADE_ROLLBACK.md",
            "fomod_logger_docs/docs/PROVENANCE.md",
            "fomod_logger_docs/docs/THIRD_PARTY.md",
            "fomod_logger_docs/schemas/installation-event.schema.json",
            "fomod_logger_docs/packaging/native-dependencies.json",
            "fomod_logger_docs/packaging/runtime-sources.json",
            "fomod_logger_docs/packaging/sqlite-runtime.json",
            "fomod_logger_docs/third_party/licenses/LGPL-3.0.txt",
            "fomod_logger_docs/third_party/python-3.12.3/LICENSE.txt",
            "fomod_logger_docs/native-build-record.json",
            "plugins/fomod_logger.dll",
            "plugins/fomod_logger.py",
            "plugins/fomod_logger_support/viewer/window.py",
            "fomod_logger-manifest.json",
        }
        assert required <= names
        assert not {"README.md", "LICENSE", "docs/", "schemas/"} & names
        assert "plugins/fomod_logger_support/core/fomod_plus.py" not in names
        manifest = json.loads(
            archive.read("fomod_logger-manifest.json").decode("utf-8")
        )

    assert manifest["format"] == 3
    assert manifest["repository_commit"] == "a" * 40
    assert manifest["corresponding_source"] == {
        "archive": release.SOURCE_ARCHIVE_NAME,
        "sha256": "b" * 64,
    }
    assert manifest["native_build_record"] == (
        "fomod_logger_docs/native-build-record.json"
    )
    assert manifest["required_external_sources"] == runtime_sources


def test_source_archive_embeds_exact_uibase_snapshot_and_is_deterministic() -> None:
    release = _release_module()
    source_files = release.collect_tracked_source_files(ROOT, commit="HEAD")
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-tree", "-r", "-z", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout
    expected = {
        item.decode("utf-8").replace("\\", "/")
        for item in listed.split(b"\0")
        if item
    }
    assert set(source_files) == expected
    assert "legacy/python/fomod_logger_1_0_5.py" in source_files
    verifier_name = "packaging/verify-git-source.py"
    verifier_bytes = (ROOT / verifier_name).read_bytes()
    source_files[verifier_name] = verifier_bytes
    expected.add(verifier_name)

    uibase_files = {
        "README.md": b"uibase fixture\n",
        "src/iplugin.h": b"interface fixture\n",
    }
    runtime_sources = release._locked_runtime_components()
    commit = "c" * 40
    kwargs = {
        "uibase_source_files": uibase_files,
        "commit": commit,
        "runtime_source_components": runtime_sources,
    }
    first = release.build_source_archive_bytes(source_files, **kwargs)
    second = release.build_source_archive_bytes(source_files, **kwargs)
    assert first == second

    prefix = f"{release.SOURCE_ARCHIVE_ROOT}/"
    manifest_path = f"{prefix}fomod_logger-source-manifest.json"
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        names = set(archive.namelist())
        assert {f"{prefix}{name}" for name in expected} <= names
        for name in uibase_files:
            assert f"{prefix}{release.UIBASE_SOURCE_PREFIX}/{name}" in names
        assert manifest_path in names
        assert all(
            info.date_time == release.FIXED_ZIP_TIME for info in archive.infolist()
        )
        manifest = json.loads(archive.read(manifest_path).decode("utf-8"))

    expected_manifest_files = expected | {
        f"{release.UIBASE_SOURCE_PREFIX}/{name}" for name in uibase_files
    }
    assert manifest["format"] == 2
    assert manifest["repository_commit"] == commit
    assert set(manifest["files"]) == expected_manifest_files
    assert manifest["embedded_git_sources"]["uibase"] == {
        "repository": release.NATIVE_DEPENDENCIES["uibase"]["repository"],
        "commit": release.UIBASE_COMMIT,
        "path": release.UIBASE_SOURCE_PREFIX,
    }
    assert manifest["required_external_sources"] == runtime_sources
    assert f"{prefix}{verifier_name}" in names
    assert manifest["files"][verifier_name] == release.sha256(verifier_bytes)


def test_uibase_snapshot_requires_exact_clean_commit(monkeypatch, tmp_path) -> None:
    release = _release_module()
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for the uibase snapshot gate")

    repository = tmp_path / "uibase"
    commit = _initialize_repository(git, repository, {"src/iplugin.h": b"pinned\n"})
    verifier_repository = tmp_path / "verifier"
    verifier_commit = _initialize_repository(
        git,
        verifier_repository,
        {
            "packaging/verify-git-source.py": (
                ROOT / "packaging" / "verify-git-source.py"
            ).read_bytes()
        },
    )
    monkeypatch.setattr(release, "UIBASE_COMMIT", commit)
    committed_bytes = subprocess.run(
        [git, "-C", str(repository), "show", "HEAD:src/iplugin.h"],
        check=True,
        capture_output=True,
    ).stdout
    assert release.collect_uibase_source_files(
        repository,
        verifier_root=verifier_repository,
        verifier_commit=verifier_commit,
    ) == {
        "src/iplugin.h": committed_bytes
    }

    (repository / "src" / "iplugin.h").write_bytes(b"dirty\n")
    with pytest.raises(RuntimeError, match="dirty Git source"):
        release.collect_uibase_source_files(
            repository,
            verifier_root=verifier_repository,
            verifier_commit=verifier_commit,
        )


def test_python_git_identity_rejects_a_nested_source_root(tmp_path) -> None:
    release = _release_module()
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for the exact-root identity gate")
    parent = tmp_path / "parent repository"
    _initialize_repository(
        git, parent, {"nested/source/tracked.txt": b"nested source\n"}
    )
    nested = parent / "nested" / "source"
    with pytest.raises(RuntimeError, match="does not match source root"):
        release.repository_commit(nested)


def test_runtime_source_locks_and_mo2_pyqt_sip_derivation() -> None:
    release = _release_module()
    components = {item["id"]: item for item in release._locked_runtime_components()}
    assert components["qtbase"]["size"] == 49324536
    assert components["qtbase"]["sha256"] == (
        "b7338da1bdccb4d861e714efffaa83f174dfe37e194916bfd7ec82279a6ace19"
    )
    assert components["qtdeclarative"]["size"] == 35624448
    assert components["qtdeclarative"]["sha256"] == (
        "81135c96ed2f599385b8a68c57f4f438dad193c62f946f5b200a321558fd9f1c"
    )
    assert components["pyqt6"]["size"] == 1051212
    assert components["pyqt6-sip"]["size"] == 92264
    assert components["sip"]["size"] == 420778
    derivation = release.RUNTIME_SOURCES["mo2_pyqt_sip_derivation"]
    assert derivation["pins"] == {
        "python": "3.12.3",
        "pyqt": "6.7.1",
        "pyqt_sip_abi": "13.8.0",
        "sip_generator": "6.8.6",
    }
    assert "not expected to hash-match" in derivation["method"]
    assert release.RUNTIME_SOURCES["unresolved_publication_gates"] == []


def test_runtime_source_validator_rejects_wrong_set_name_size_and_hash(tmp_path) -> None:
    release = _release_module()
    first_bytes = b"first source"
    second_bytes = b"second source"
    lock = {
        "components": [
            {
                "id": "first",
                "filename": "first.tar.gz",
                "official_url": "https://example.invalid/first.tar.gz",
                "size": len(first_bytes),
                "sha256": release.sha256(first_bytes),
            },
            {
                "id": "second",
                "filename": "second.tar.xz",
                "official_url": "https://example.invalid/second.tar.xz",
                "size": len(second_bytes),
                "sha256": release.sha256(second_bytes),
            },
        ]
    }
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.xz"
    first.write_bytes(first_bytes)
    second.write_bytes(second_bytes)
    paths = {"first": first, "second": second}
    assert release.validate_runtime_source_archives(paths, lock) == lock["components"]

    with pytest.raises(ValueError, match="set mismatch"):
        release.validate_runtime_source_archives({"first": first}, lock)
    wrong_name = tmp_path / "renamed.tar.gz"
    wrong_name.write_bytes(first_bytes)
    with pytest.raises(ValueError, match="must be named"):
        release.validate_runtime_source_archives(
            {"first": wrong_name, "second": second}, lock
        )
    first.write_bytes(first_bytes + b"x")
    with pytest.raises(ValueError, match="size"):
        release.validate_runtime_source_archives(paths, lock)
    first.write_bytes(b"X" * len(first_bytes))
    with pytest.raises(ValueError, match="SHA-256"):
        release.validate_runtime_source_archives(paths, lock)


def test_native_release_binding_accepts_exact_post_link_record() -> None:
    release = _release_module()
    (
        commit,
        native_bytes,
        import_library_bytes,
        companion_dll_bytes,
        metadata,
        build_record,
    ) = _native_fixture(release)
    release.validate_native_inputs(
        native_bytes=native_bytes,
        native_metadata=metadata,
        build_record=build_record,
        uibase_import_library_bytes=import_library_bytes,
        uibase_companion_dll_bytes=companion_dll_bytes,
        commit=commit,
    )


@pytest.mark.parametrize(
    ("record_change", "metadata_change", "native_change", "import_change", "companion_change", "message"),
    [
        ({"native_dll_sha256": "0" * 64}, {}, b"", b"", b"", "native_dll_sha256"),
        ({"configuration": "Release"}, {}, b"", b"", b"", "configuration"),
        ({"release_build": False}, {}, b"", b"", b"", "release_build"),
        ({"windows_sdk": "10.0.26100.0"}, {}, b"", b"", b"", "windows_sdk"),
        ({}, {"IsDebug": True}, b"", b"", b"", "IsDebug"),
        ({}, {"IsPreRelease": True}, b"", b"", b"", "IsPreRelease"),
        ({}, {"PrivateBuild": "e" * 40}, b"", b"", b"", "PrivateBuild"),
        ({}, {}, b"changed", b"", b"", "native_dll_sha256"),
        ({}, {}, b"", b"changed", b"", "uibase_import_library_sha256"),
        ({}, {}, b"", b"", b"changed", "uibase_companion_dll_sha256"),
    ],
)
def test_native_release_binding_rejects_mismatches(
    record_change,
    metadata_change,
    native_change,
    import_change,
    companion_change,
    message,
) -> None:
    release = _release_module()
    (
        commit,
        native_bytes,
        import_library_bytes,
        companion_dll_bytes,
        metadata,
        build_record,
    ) = _native_fixture(release)
    with pytest.raises(ValueError, match=message):
        release.validate_native_inputs(
            native_bytes=native_bytes + native_change,
            native_metadata={**metadata, **metadata_change},
            build_record={**build_record, **record_change},
            uibase_import_library_bytes=import_library_bytes + import_change,
            uibase_companion_dll_bytes=companion_dll_bytes + companion_change,
            commit=commit,
        )


def test_native_version_metadata_path_is_passed_through_environment(
    monkeypatch, tmp_path
) -> None:
    release = _release_module()
    native_plugin = tmp_path / "plugin with spaces.dll"
    native_plugin.write_bytes(_minimal_x64_pe())
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return SimpleNamespace(
            stdout=(
                '{"FileVersion":"1.0.0",'
                '"ProductVersion":"1.0.0",'
                '"PrivateBuild":"' + "a" * 40 + '",'
                '"OriginalFilename":"fomod_logger.dll",'
                '"IsDebug":false,"IsPreRelease":false,"IsPrivateBuild":true}'
            )
        )

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    metadata = release.read_native_version_metadata(native_plugin)

    assert metadata["OriginalFilename"] == "fomod_logger.dll"
    assert metadata["IsDebug"] is False
    assert str(native_plugin.resolve()) not in observed["command"]
    assert observed["environment"]["FOMOD_LOGGER_NATIVE_PLUGIN_PATH"] == str(
        native_plugin.resolve()
    )


def test_post_link_record_writer_hashes_exact_inputs(tmp_path) -> None:
    release = _release_module()
    cmake = shutil.which("cmake")
    if cmake is None:
        pytest.skip("CMake is required for the post-link record test")
    native = tmp_path / "fomod_logger.dll"
    import_library = tmp_path / "uibase.lib"
    companion = tmp_path / "uibase.dll"
    output = tmp_path / "fomod_logger-native-build-record.json"
    native.write_bytes(_minimal_x64_pe())
    import_library.write_bytes(b"import library")
    companion.write_bytes(b"companion runtime")
    script = ROOT / "packaging" / "write-native-build-record.cmake"
    arguments = {
        "FOMOD_LOGGER_CONFIGURATION": "RelWithDebInfo",
        "FOMOD_LOGGER_RECORD_OUTPUT": output,
        "FOMOD_LOGGER_NATIVE_DLL": native,
        "FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY": import_library,
        "FOMOD_LOGGER_UIBASE_COMPANION_DLL": companion,
        "FOMOD_LOGGER_PUBLIC_VERSION": release.PACKAGE_VERSION,
        "FOMOD_LOGGER_MO2_VERSION": release.MO2_VERSION,
        "FOMOD_LOGGER_RELEASE_BUILD_JSON": "true",
        "FOMOD_LOGGER_REPOSITORY_COMMIT": "a" * 40,
        "FOMOD_LOGGER_MO2_ABI_TARGET": "2.5.2",
        "FOMOD_LOGGER_GENERATOR": "Visual Studio 17 2022",
        "FOMOD_LOGGER_GENERATOR_PLATFORM": "x64",
        "FOMOD_LOGGER_GENERATOR_TOOLSET": "v143",
        "FOMOD_LOGGER_CXX_COMPILER_ID": "MSVC",
        "FOMOD_LOGGER_CXX_COMPILER_VERSION": "19.44.35219.0",
        "FOMOD_LOGGER_CMAKE_VERSION": "4.0.0",
        "FOMOD_LOGGER_WINDOWS_SDK": "10.0.22621.0",
        "FOMOD_LOGGER_QT_VERSION": "6.7.1",
        "FOMOD_LOGGER_UIBASE_COMMIT": release.UIBASE_COMMIT,
    }
    command = [cmake, *[f"-D{key}={value}" for key, value in arguments.items()], "-P", str(script)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["format"] == 2
    assert record["configuration"] == "RelWithDebInfo"
    assert record["native_dll_sha256"] == release.file_sha256(native)
    assert record["uibase_import_library_sha256"] == release.file_sha256(
        import_library
    )
    assert record["uibase_companion_dll_sha256"] == release.file_sha256(companion)

    output.unlink()
    arguments["FOMOD_LOGGER_CONFIGURATION"] = "Debug"
    wrong_configuration = [
        cmake,
        *[f"-D{key}={value}" for key, value in arguments.items()],
        "-P",
        str(script),
    ]
    result = subprocess.run(wrong_configuration, capture_output=True, text=True)
    assert result.returncode != 0
    assert "RelWithDebInfo" in result.stderr
    assert not output.exists()


def test_paired_output_failure_never_deletes_a_published_path(tmp_path) -> None:
    release = _release_module()
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    second.write_bytes(b"pre-existing")

    with pytest.raises(FileExistsError):
        release._write_pair_without_overwrite(
            ((first, b"first"), (second, b"second"))
        )

    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"pre-existing"
    assert not list(tmp_path.glob(".*.tmp"))


def test_release_identity_representations_and_canonical_filenames(tmp_path) -> None:
    release = _release_module()
    identity = json.loads((ROOT / "release_identity.json").read_text(encoding="utf-8"))
    presets = json.loads((ROOT / "CMakePresets.json").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert identity["public_semver"] == release.PACKAGE_VERSION
    assert identity["pep440"] == pyproject["project"]["version"]
    assert identity["mo2_version"] == identity["public_semver"] == "1.0.2"
    assert release.BINARY_ARCHIVE_NAME == "modlist-companion-1.0.2.zip"
    assert release.SOURCE_ARCHIVE_NAME == (
        "modlist-companion-1.0.2-source.zip"
    )
    cache = presets["configurePresets"][0]["cacheVariables"]
    assert cache["FOMOD_LOGGER_RELEASE_BUILD"] is True
    assert "FOMOD_LOGGER_UIBASE_SOURCE_DIR" in cache
    assert "FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY" in cache
    assert "FOMOD_LOGGER_UIBASE_COMPANION_DLL" in cache
    assert not any("BOOST" in name or "DEPENDENCIES" in name for name in cache)

    with pytest.raises(ValueError, match="binary output must be named"):
        release.build(
            ROOT / "missing.dll",
            ROOT / "missing-build-record.json",
            ROOT / "missing-uibase-source",
            ROOT / "missing-uibase.lib",
            ROOT / "missing-uibase.dll",
            ROOT / "missing-sqlite.dll",
            {},
            ROOT / "wrong-name.zip",
            ROOT / release.SOURCE_ARCHIVE_NAME,
            expected_commit="a" * 40,
        )

    with pytest.raises(ValueError, match="outside the exact source checkout"):
        release.build(
            ROOT / "missing.dll",
            ROOT / "missing-build-record.json",
            ROOT / "missing-uibase-source",
            ROOT / "missing-uibase.lib",
            ROOT / "missing-uibase.dll",
            ROOT / "missing-sqlite.dll",
            {},
            ROOT / release.BINARY_ARCHIVE_NAME,
            tmp_path / release.SOURCE_ARCHIVE_NAME,
            expected_commit="a" * 40,
        )


def test_release_source_verifier_rejects_dirty_wrong_and_misbound_source(
    tmp_path,
) -> None:
    release = _release_module()
    cmake = shutil.which("cmake")
    git = shutil.which("git")
    if cmake is None or git is None:
        pytest.skip("CMake and Git are required for the release-source gate")

    repository = tmp_path / "source with spaces"
    uibase = tmp_path / "uibase with spaces"
    commit = _initialize_repository(
        git,
        repository,
        {
            "tracked.txt": b"clean\n",
            "packaging/verify-git-source.py": (
                ROOT / "packaging" / "verify-git-source.py"
            ).read_bytes(),
        },
    )
    uibase_commit = _initialize_repository(
        git, uibase, {"src/iplugin.h": b"clean interface\n"}
    )
    script = ROOT / "packaging" / "verify-release-source.cmake"

    def verify(expected_commit, source=repository, uibase_source=uibase):
        source_driver = source / "packaging" / "verify-git-source.py"
        return subprocess.run(
            [
                cmake,
                f"-DFOMOD_LOGGER_SOURCE_DIR={source}",
                f"-DFOMOD_LOGGER_EXPECTED_COMMIT={expected_commit}",
                f"-DFOMOD_LOGGER_GIT_EXECUTABLE={git}",
                f"-DFOMOD_LOGGER_PYTHON_EXECUTABLE={sys.executable}",
                f"-DFOMOD_LOGGER_GIT_VERIFY_SCRIPT={source_driver}",
                f"-DFOMOD_LOGGER_VERIFY_TEMP_DIR={tmp_path / 'verified tools'}",
                f"-DFOMOD_LOGGER_UIBASE_DIR={uibase_source}",
                f"-DFOMOD_LOGGER_UIBASE_EXPECTED_COMMIT={uibase_commit}",
                "-DFOMOD_LOGGER_UIBASE_EXPECTED_PREFIX=external/uibase",
                "-P",
                str(script),
            ],
            capture_output=True,
            text=True,
        )

    assert verify(commit).returncode == 0
    with pytest.raises(RuntimeError, match="does not match expected commit"):
        release.repository_commit(repository, expected_commit="0" * 40)
    with pytest.raises(ValueError, match="lowercase 40-hex"):
        release.repository_commit(repository, expected_commit="not-a-commit")
    wrong = verify("0" * 40)
    assert wrong.returncode != 0
    assert "does not match" in wrong.stderr

    (uibase / "src" / "iplugin.h").write_bytes(b"dirty interface\n")
    dirty_uibase = verify(commit)
    assert dirty_uibase.returncode != 0
    assert "refusing to use dirty Git source" in dirty_uibase.stderr
    subprocess.run(
        [git, "-C", str(uibase), "checkout", "--", "src/iplugin.h"],
        check=True,
        capture_output=True,
    )

    (repository / "tracked.txt").write_bytes(b"dirty\n")
    dirty = verify(commit)
    assert dirty.returncode != 0
    assert "refusing to use dirty Git source" in dirty.stderr

    parent = tmp_path / "unrelated parent repository"
    _initialize_repository(git, parent, {"parent.txt": b"parent\n"})
    extracted = parent / "nested" / "extracted source"
    embedded = extracted / "external" / "uibase"
    extracted.mkdir(parents=True)
    embedded.mkdir(parents=True)
    tracked_bytes = b"manifest-bound\n"
    interface_bytes = b"embedded interface\n"
    (extracted / "tracked.txt").write_bytes(tracked_bytes)
    (embedded / "iplugin.h").write_bytes(interface_bytes)
    extracted_driver = extracted / "packaging" / "verify-git-source.py"
    extracted_driver.parent.mkdir()
    extracted_driver.write_bytes(
        (ROOT / "packaging" / "verify-git-source.py").read_bytes()
    )
    source_manifest = {
        "format": 2,
        "repository_commit": commit,
        "embedded_git_sources": {
            "uibase": {
                "commit": uibase_commit,
                "path": "external/uibase",
            }
        },
        "files": {
            "tracked.txt": release.sha256(tracked_bytes),
            "external/uibase/iplugin.h": release.sha256(interface_bytes),
            "packaging/verify-git-source.py": release.file_sha256(extracted_driver),
        },
    }
    (extracted / "fomod_logger-source-manifest.json").write_text(
        json.dumps(source_manifest), encoding="utf-8"
    )
    nested_result = verify(commit, extracted, embedded)
    assert nested_result.returncode == 0, nested_result.stderr

    outside_directory = tmp_path / "outside directory"
    outside_directory.mkdir()
    nested_reparse = extracted / "nested-reparse"
    _make_directory_reparse(nested_reparse, outside_directory)
    try:
        direct_nested = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(extracted_driver),
                "--root",
                str(extracted),
                "--expected-commit",
                commit,
                "--manifest",
            ],
            capture_output=True,
            text=True,
        )
        assert direct_nested.returncode != 0
        assert "reparse" in direct_nested.stderr.lower()
        nested_reparse_result = verify(commit, extracted, embedded)
        assert nested_reparse_result.returncode != 0
    finally:
        nested_reparse.rmdir()

    source_alias = tmp_path / "extracted-source-alias"
    _make_directory_reparse(source_alias, extracted)
    try:
        alias_result = verify(
            commit,
            source_alias,
            source_alias / "external" / "uibase",
        )
        assert alias_result.returncode != 0
        assert "source root must not be a reparse point" in alias_result.stderr
    finally:
        source_alias.rmdir()

    unsafe_manifest = json.loads(json.dumps(source_manifest))
    unsafe_manifest["files"]["..\\outside.txt"] = release.sha256(b"outside")
    (extracted / "fomod_logger-source-manifest.json").write_text(
        json.dumps(unsafe_manifest), encoding="utf-8"
    )
    unsafe_result = verify(commit, extracted, embedded)
    assert unsafe_result.returncode != 0
    assert "unsafe path in source manifest" in unsafe_result.stderr
    (extracted / "fomod_logger-source-manifest.json").write_text(
        json.dumps(source_manifest), encoding="utf-8"
    )

    generated = extracted / "release-build" / "generated.txt"
    generated.parent.mkdir()
    generated.write_text("in-tree build output is forbidden\n", encoding="utf-8")
    extra_output = verify(commit, extracted, embedded)
    assert extra_output.returncode != 0
    assert "does not exactly match" in extra_output.stderr
    generated.unlink()
    generated.parent.rmdir()
    (embedded / "iplugin.h").write_bytes(b"modified\n")
    mismatch = verify(commit, extracted, embedded)
    assert mismatch.returncode != 0
    assert "source manifest hash mismatch" in mismatch.stderr


def test_shared_git_byte_verifier_rejects_hidden_changes_without_repo_mutation(
    tmp_path, monkeypatch,
) -> None:
    release = _release_module()
    cmake = shutil.which("cmake")
    git = shutil.which("git")
    if cmake is None or git is None:
        pytest.skip("CMake and Git are required for the tracked-byte gate")

    repository = tmp_path / "source"
    uibase = tmp_path / "uibase"
    commit = _initialize_repository(
        git,
        repository,
        {
            "tracked.txt": b"clean\n",
            "packaging/verify-git-source.py": (
                ROOT / "packaging" / "verify-git-source.py"
            ).read_bytes(),
        },
    )
    uibase_commit = _initialize_repository(
        git, uibase, {"src/iplugin.h": b"interface\n"}
    )
    verifier = ROOT / "packaging" / "verify-release-source.cmake"
    byte_verifier = repository / "packaging" / "verify-git-source.py"

    def cmake_verify(source=repository):
        source_driver = source / "packaging" / "verify-git-source.py"
        return subprocess.run(
            [
                cmake,
                f"-DFOMOD_LOGGER_SOURCE_DIR={source}",
                f"-DFOMOD_LOGGER_EXPECTED_COMMIT={commit}",
                f"-DFOMOD_LOGGER_GIT_EXECUTABLE={git}",
                f"-DFOMOD_LOGGER_PYTHON_EXECUTABLE={sys.executable}",
                f"-DFOMOD_LOGGER_GIT_VERIFY_SCRIPT={source_driver}",
                f"-DFOMOD_LOGGER_VERIFY_TEMP_DIR={tmp_path / 'verified tools'}",
                f"-DFOMOD_LOGGER_UIBASE_DIR={uibase}",
                f"-DFOMOD_LOGGER_UIBASE_EXPECTED_COMMIT={uibase_commit}",
                "-DFOMOD_LOGGER_UIBASE_EXPECTED_PREFIX=external/uibase",
                "-P",
                str(verifier),
            ],
            capture_output=True,
            text=True,
        )

    poison = tmp_path / "poisoned path"
    poison.mkdir()
    if os.name == "nt":
        (poison / "git.cmd").write_text("@exit /b 86\r\n", encoding="ascii")
    else:
        fake_git = poison / "git"
        fake_git.write_text("#!/bin/sh\nexit 86\n", encoding="ascii")
        fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(poison) + os.pathsep + os.environ["PATH"])
    python_poison = tmp_path / "python poison"
    python_poison.mkdir()
    (python_poison / "sitecustomize.py").write_text(
        "import os\nos._exit(0)\n", encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", str(python_poison))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong git dir"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "wrong index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "wrong objects"))
    clean_before = _git_storage_fingerprint(git, repository)
    assert release.repository_commit(repository, expected_commit=commit) == commit
    clean_cmake = cmake_verify()
    assert clean_cmake.returncode == 0, clean_cmake.stderr
    assert _git_storage_fingerprint(git, repository) == clean_before
    for variable in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        monkeypatch.delenv(variable)

    source_alias = tmp_path / "git-source-alias"
    _make_directory_reparse(source_alias, repository)
    try:
        with pytest.raises(RuntimeError, match="source root must not be a reparse point"):
            release.repository_commit(source_alias, expected_commit=commit)
        alias_cmake = cmake_verify(source_alias)
        assert alias_cmake.returncode != 0
        assert "source root must not be a reparse point" in alias_cmake.stderr
    finally:
        source_alias.rmdir()

    byte_verifier.write_text(
        "print('{\"commit\": \"" + commit + "\", \"root\": \"forged\", "
        "\"tracked_file_count\": 1}')\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="refusing to use dirty Git source"):
        release.repository_commit(repository, expected_commit=commit)
    modified_driver_cmake = cmake_verify()
    assert modified_driver_cmake.returncode != 0
    assert "refusing to use dirty Git source" in modified_driver_cmake.stderr
    subprocess.run(
        [
            git,
            "-C",
            str(repository),
            "checkout",
            "--force",
            "HEAD",
            "--",
            "packaging/verify-git-source.py",
        ],
        check=True,
        capture_output=True,
    )

    def assert_both_reject(message: str) -> None:
        before = _git_storage_fingerprint(git, repository)
        with pytest.raises(RuntimeError, match=message):
            release.repository_commit(repository, expected_commit=commit)
        assert _git_storage_fingerprint(git, repository) == before

        result = cmake_verify()
        assert result.returncode != 0
        assert message in result.stderr
        assert _git_storage_fingerprint(git, repository) == before

    tracked = repository / "tracked.txt"
    subprocess.run(
        [git, "-C", str(repository), "update-index", "--assume-unchanged", "tracked.txt"],
        check=True,
    )
    tracked.write_bytes(b"other\n")
    assert_both_reject("non-normal tracked flags")
    subprocess.run(
        [
            git,
            "-C",
            str(repository),
            "update-index",
            "--no-assume-unchanged",
            "tracked.txt",
        ],
        check=True,
    )
    subprocess.run(
        [git, "-C", str(repository), "checkout", "--force", "HEAD", "--", "tracked.txt"],
        check=True,
    )

    subprocess.run(
        [git, "-C", str(repository), "update-index", "--skip-worktree", "tracked.txt"],
        check=True,
    )
    assert_both_reject("non-normal tracked flags")
    subprocess.run(
        [
            git,
            "-C",
            str(repository),
            "update-index",
            "--no-skip-worktree",
            "tracked.txt",
        ],
        check=True,
    )
    current_stat = tracked.stat()
    aged_mtime = current_stat.st_mtime_ns - 10_000_000_000
    os.utime(tracked, ns=(current_stat.st_atime_ns, aged_mtime))
    subprocess.run(
        [git, "-C", str(repository), "update-index", "--refresh"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [git, "-C", str(repository), "config", "core.trustctime", "false"],
        check=True,
    )
    subprocess.run(
        [git, "-C", str(repository), "config", "core.checkStat", "minimal"],
        check=True,
    )
    original_bytes = tracked.read_bytes()
    original_stat = tracked.stat()
    tracked.write_bytes(b"X" * len(original_bytes))
    os.utime(tracked, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    flags = subprocess.run(
        [git, "-C", str(repository), "ls-files", "-v", "tracked.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert flags.startswith("H ")
    clean_status_environment = os.environ.copy()
    clean_status_environment["GIT_OPTIONAL_LOCKS"] = "0"
    ordinary_status = subprocess.run(
        [
            git,
            "-C",
            str(repository),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        check=True,
        capture_output=True,
        env=clean_status_environment,
        text=True,
    ).stdout
    assert ordinary_status == ""
    assert_both_reject("tracked worktree bytes")


def test_shared_git_byte_verifier_accepts_raw_exact_crlf_blob_read_only(
    tmp_path,
) -> None:
    release = _release_module()
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for the tracked-byte gate")

    repository = tmp_path / "raw exact crlf"
    repository.mkdir()
    subprocess.run([git, "init", str(repository)], check=True, capture_output=True)
    (repository / ".gitattributes").write_bytes(b"* text=auto\n")
    (repository / "raw.txt").write_bytes(b"raw\r\nbytes\r\n")
    committed_verifier = repository / "packaging" / "verify-git-source.py"
    committed_verifier.parent.mkdir()
    committed_verifier.write_bytes(
        (ROOT / "packaging" / "verify-git-source.py").read_bytes()
    )
    subprocess.run(
        [
            git,
            "-C",
            str(repository),
            "add",
            ".gitattributes",
            "packaging/verify-git-source.py",
        ],
        check=True,
        capture_output=True,
    )
    raw_blob = subprocess.run(
        [
            git,
            "-C",
            str(repository),
            "hash-object",
            "-w",
            "--no-filters",
            "raw.txt",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            git,
            "-C",
            str(repository),
            "update-index",
            "--add",
            "--cacheinfo",
            "100644",
            raw_blob,
            "raw.txt",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            git,
            "-C",
            str(repository),
            "-c",
            "user.name=FOMOD Logger Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-m",
            "raw exact fixture",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [git, "-C", str(repository), "reset", "--hard", "HEAD"],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        [git, "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (repository / "raw.txt").read_bytes() == b"raw\r\nbytes\r\n"

    before = _git_storage_fingerprint(git, repository)
    assert release.repository_commit(repository) == commit
    assert _git_storage_fingerprint(git, repository) == before


def test_git_source_gates_reject_filters_ignored_extras_and_special_tree_modes(
    tmp_path,
) -> None:
    release = _release_module()
    git = shutil.which("git")
    cmake = shutil.which("cmake")
    if git is None or cmake is None:
        pytest.skip("Git and CMake are required for hostile source fixtures")

    helper_bytes = (ROOT / "packaging" / "verify-git-source.py").read_bytes()
    uibase = tmp_path / "uibase"
    uibase_commit = _initialize_repository(
        git,
        uibase,
        {"src/iplugin.h": b"clean interface\n"},
    )

    def assert_rejected(repository: Path, commit: str, message: str) -> None:
        before = _git_storage_fingerprint(git, repository)
        with pytest.raises(RuntimeError, match=message):
            release.repository_commit(repository, expected_commit=commit)
        assert _git_storage_fingerprint(git, repository) == before
        cmake_result = _run_cmake_source_gate(
            cmake=cmake,
            git=git,
            source=repository,
            commit=commit,
            uibase=uibase,
            uibase_commit=uibase_commit,
            temporary_tools=tmp_path / "materialized tools",
        )
        assert cmake_result.returncode != 0
        assert message in " ".join(cmake_result.stderr.split())
        assert _git_storage_fingerprint(git, repository) == before

    filtered = tmp_path / "filtered"
    filtered_commit = _initialize_repository(
        git,
        filtered,
        {
            ".gitattributes": b"*.flt filter=constant\n",
            "source.flt": b"arbitrary source bytes\n",
            "packaging/verify-git-source.py": helper_bytes,
        },
    )
    assert_rejected(filtered, filtered_commit, "forbids effective filter")

    ignored = tmp_path / "ignored-extra"
    ignored_commit = _initialize_repository(
        git,
        ignored,
        {
            ".gitignore": b"ignored.tmp\n",
            "tracked.txt": b"tracked\n",
            "packaging/verify-git-source.py": helper_bytes,
        },
    )
    (ignored / "ignored.tmp").write_bytes(b"must still be rejected\n")
    assert_rejected(ignored, ignored_commit, "source file set does not match")

    for mode, name in (("120000", "link-entry"), ("160000", "submodule-entry")):
        special = tmp_path / name
        base_commit = _initialize_repository(
            git,
            special,
            {
                "tracked.txt": b"tracked\n",
                "packaging/verify-git-source.py": helper_bytes,
            },
        )
        if mode == "120000":
            object_id = subprocess.run(
                [git, "-C", str(special), "hash-object", "-w", "--stdin"],
                input=b"tracked.txt",
                check=True,
                capture_output=True,
            ).stdout.decode("ascii").strip()
        else:
            object_id = base_commit
        subprocess.run(
            [
                git,
                "-C",
                str(special),
                "update-index",
                "--add",
                "--cacheinfo",
                mode,
                object_id,
                name,
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                git,
                "-C",
                str(special),
                "-c",
                "user.name=FOMOD Logger Tests",
                "-c",
                "user.email=tests@example.invalid",
                "commit",
                "-m",
                f"add {mode} entry",
            ],
            check=True,
            capture_output=True,
        )
        special_commit = subprocess.run(
            [git, "-C", str(special), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert_rejected(special, special_commit, "unsupported Git entry")


# ---------- the FOMOD Plus edition's package (Python only) ----------

def _plus_release_module():
    path = ROOT / "packaging" / "build_plus_release.py"
    spec = importlib.util.spec_from_file_location("fomod_logger_build_plus_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plus_source_files(plus):
    names = set(plus.DOC_FILES.values()) | {plus.PLUGIN_SOURCE}
    support_root = ROOT / "python" / "fomod_logger_support"
    for path in support_root.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            names.add(path.relative_to(ROOT).as_posix())
    return {name: (ROOT / name).read_bytes() for name in names}


def test_the_plus_package_has_the_plus_plugin_and_the_shared_support_and_no_native_code():
    plus = _plus_release_module()
    sqlite = b"pinned sqlite runtime"
    files = plus.collect_plus_binary_files(source_files=_plus_source_files(plus), sqlite_bytes=sqlite,
                                           expected_sqlite_sha256=plus.common.sha256(sqlite))
    assert files["plugins/fomod_logger_plus.py"] == (ROOT / "python" / "fomod_logger_plus_plugin.py").read_bytes()
    for needed in ("plugin_base.py", "plus/jobs.py", "executor.py", "plus/record.py", "scan.py",
                   "plus/state.py", "archive/rebuild.py", "archive/sevenzip.py", "core/storage.py",
                   "viewer/window.py", "viewer/scan_dialog.py"):
        assert f"plugins/fomod_logger_support/{needed}" in files, needed
    assert files["plugins/fomod_logger_support/native/sqlite3.dll"] == sqlite
    # Nothing of the full edition's native installer or its entry file.
    assert "plugins/fomod_logger.dll" not in files and "plugins/fomod_logger.py" not in files
    assert [name for name in files if name.endswith((".dll", ".pyd", ".exe"))] == [
        "plugins/fomod_logger_support/native/sqlite3.dll"]
    assert not [name for name in files if "__pycache__" in name or name.endswith(".pyc")]
    assert not [name for name in files if "uibase" in name]
    with pytest.raises(ValueError, match="sqlite3.dll"):
        plus.collect_plus_binary_files(source_files=_plus_source_files(plus), sqlite_bytes=b"other",
                                       expected_sqlite_sha256=plus.common.sha256(sqlite))


def test_the_plus_archives_name_their_contents_and_their_source():
    import io
    import json
    import zipfile

    plus = _plus_release_module()
    sqlite = b"pinned sqlite runtime"
    sources = _plus_source_files(plus)
    files = plus.collect_plus_binary_files(source_files=sources, sqlite_bytes=sqlite,
                                           expected_sqlite_sha256=plus.common.sha256(sqlite))
    source_zip = plus.build_plus_source_archive_bytes(sources, commit="e" * 40)
    binary_zip = plus.build_plus_binary_archive_bytes(files, commit="e" * 40, source_filename=plus.SOURCE_ARCHIVE_NAME,
                                                      source_sha256=plus.common.sha256(source_zip))
    assert plus.BINARY_ARCHIVE_NAME == "modlist-companion-plus-1.0.2.zip"
    with zipfile.ZipFile(io.BytesIO(binary_zip)) as archive:
        manifest = json.loads(archive.read("fomod_logger_plus-manifest.json"))
        assert manifest["product"] == "Modlist Companion (for FOMOD Plus)" and manifest["repository_commit"] == "e" * 40
        assert manifest["corresponding_source"]["sha256"] == plus.common.sha256(source_zip)
        assert set(manifest["files"]) == set(archive.namelist()) - {"fomod_logger_plus-manifest.json"}
    with zipfile.ZipFile(io.BytesIO(source_zip)) as archive:
        assert f"{plus.SOURCE_ARCHIVE_ROOT}/python/fomod_logger_plus_plugin.py" in archive.namelist()


def test_the_plus_plugin_file_registers_the_sqlite_folder_before_it_imports_the_support_code():
    source = (ROOT / "python" / "fomod_logger_plus_plugin.py").read_text(encoding="utf-8")
    assert source.index("os.add_dll_directory") < source.index("from fomod_logger_support")
    assert '_PLUGIN_ROOT / "fomod_logger_support"' in source


def test_the_two_entry_files_are_thin_and_share_one_base():
    full = (ROOT / "python" / "fomod_logger_plugin.py").read_text(encoding="utf-8")
    plus = (ROOT / "python" / "fomod_logger_plus_plugin.py").read_text(encoding="utf-8")
    for source in (full, plus):
        assert "from fomod_logger_support.plugin_base import CompanionPlugin" in source
        assert "(CompanionPlugin)" in source
    assert "Native Installer" in full and "Native Installer" not in plus


def test_the_packaged_readmes_name_the_product_and_say_which_edition_to_pick():
    plus = _plus_release_module()
    full_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    plus_readme = (ROOT / plus.DOC_FILES["fomod_logger_plus_docs/README.md"]).read_text(encoding="utf-8")
    # The branded header image precedes the accessible product heading.
    assert "# Modlist Companion" in full_readme.splitlines()
    assert "# Modlist Companion (for FOMOD Plus)" in plus_readme.splitlines()
    for text in (full_readme, plus_readme):
        assert "never both" in text and "GNU GPL version 3" in text and "THIRD_PARTY_NOTICES.md" in text
    assert "Import choices file" in plus_readme and "download archive" in plus_readme
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    identity = json.loads((ROOT / "release_identity.json").read_text(encoding="utf-8"))
    assert f"## {identity['public_semver']}" in changelog


# --- Reusing the shipped native build (docs/BUILD_CONTRACT.md, "Reusing a shipped native build") ---

NATIVE_REUSE_VERSION = "1.0.0"
NATIVE_REUSE_COMMIT = "3fb5486ec60ba89cf3fca5f2be0c00d941eb9c71"


def _native_reuse_record(release, native_bytes, **changes):
    record = {
        **release.EXPECTED_BUILD_RECORD,
        "public_version": NATIVE_REUSE_VERSION,
        "mo2_version": NATIVE_REUSE_VERSION,
        "repository_commit": NATIVE_REUSE_COMMIT,
        "cxx_compiler_version": "19.36.32548.0",
        "cmake_version": "4.4.2",
        "native_dll_sha256": release.sha256(native_bytes),
        "uibase_import_library_sha256": release.sha256(b"original import library"),
        "uibase_companion_dll_sha256": release.sha256(b"original companion dll"),
    }
    record.update(changes)
    return record


def _native_reuse_metadata(**changes):
    metadata = {
        "FileVersion": NATIVE_REUSE_VERSION,
        "ProductVersion": NATIVE_REUSE_VERSION,
        "PrivateBuild": NATIVE_REUSE_COMMIT,
        "OriginalFilename": "fomod_logger.dll",
        "IsDebug": False,
        "IsPreRelease": False,
        "IsPrivateBuild": True,
    }
    metadata.update(changes)
    return metadata


def _earlier_release_zip(
    release,
    monkeypatch,
    tmp_path,
    *,
    native_bytes=None,
    record_changes=None,
    version=NATIVE_REUSE_VERSION,
    commit=NATIVE_REUSE_COMMIT,
    native_binary_from=None,
    sqlite_bytes=b"test sqlite runtime",
    name="earlier.zip",
):
    """A package shaped like the shipped 1.0.0 one: manifest, DLL, SQLite, record."""

    monkeypatch.setattr(release, "PACKAGE_VERSION", version)
    native_bytes = _minimal_x64_pe() if native_bytes is None else native_bytes
    record = _native_reuse_record(release, native_bytes, **(record_changes or {}))
    record_bytes = (json.dumps(record) + "\n").encode("utf-8")
    files = release.collect_binary_files(
        source_files=_worktree_source_files(release),
        native_bytes=native_bytes,
        sqlite_bytes=sqlite_bytes,
        build_record_bytes=record_bytes,
        expected_sqlite_sha256=release.sha256(sqlite_bytes),
    )
    package = tmp_path / name
    package.write_bytes(
        release.build_binary_archive_bytes(
            files,
            commit=commit,
            source_filename="earlier-source.zip",
            source_sha256="b" * 64,
            runtime_source_components=release._locked_runtime_components(),
            native_binary_from=native_binary_from,
        )
    )
    return package, native_bytes, sqlite_bytes, record_bytes


def _replace_zip_members(package: Path, replacements: dict) -> Path:
    tampered = package.with_name("tampered-" + package.name)
    with zipfile.ZipFile(package) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = replacements.get(info.filename, source.read(info.filename))
            target.writestr(info.filename, data)
    return tampered


def _reuse_identity(release, **changes):
    """The identity pinning the genuine fixture package's DLL and SQLite runtime."""

    values = {
        "version": NATIVE_REUSE_VERSION,
        "commit": NATIVE_REUSE_COMMIT,
        "dll_sha256": release.sha256(_minimal_x64_pe()),
        "sqlite_sha256": release.sha256(b"test sqlite runtime"),
    }
    values.update(changes)
    return release.NativeIdentity(**values)


def _read_reused(release, package, **identity_changes):
    return release.read_native_from_release_zip(
        package, identity=_reuse_identity(release, **identity_changes)
    )


def _validate_reused(release, reused, **metadata_changes):
    release.validate_reused_native_inputs(
        native_bytes=reused.native_bytes,
        native_metadata=_native_reuse_metadata(**metadata_changes),
        build_record=reused.build_record,
        native_version=NATIVE_REUSE_VERSION,
        native_commit=NATIVE_REUSE_COMMIT,
    )


def test_release_identity_names_the_shipped_native_build() -> None:
    release = _release_module()
    identity = release.NATIVE_IDENTITY
    assert (identity.version, identity.commit) == (NATIVE_REUSE_VERSION, NATIVE_REUSE_COMMIT)
    assert len(identity.dll_sha256) == 64 and len(identity.sqlite_sha256) == 64
    assert release.native_identity({"public_semver": "1.0.1"}) is None
    complete = {
        "native_version": "1.0.0",
        "native_commit": NATIVE_REUSE_COMMIT,
        "native_dll_sha256": "a" * 64,
    }
    assert release.native_identity(complete).sqlite_sha256 is None
    for name in complete:
        incomplete = {key: value for key, value in complete.items() if key != name}
        with pytest.raises(ValueError, match="together"):
            release.native_identity(incomplete)
    with pytest.raises(ValueError, match="together"):
        release.native_identity({"native_sqlite_sha256": "a" * 64})
    with pytest.raises(ValueError, match="40-hex"):
        release.native_identity({**complete, "native_commit": "3FB5486"})
    with pytest.raises(ValueError, match="SHA-256"):
        release.native_identity({**complete, "native_dll_sha256": "ABC"})
    with pytest.raises(ValueError, match="SHA-256"):
        release.native_identity({**complete, "native_sqlite_sha256": "ABC"})


def test_reuse_mode_accepts_a_genuine_earlier_package(monkeypatch, tmp_path) -> None:
    release = _release_module()
    package, native_bytes, sqlite_bytes, record_bytes = _earlier_release_zip(
        release, monkeypatch, tmp_path
    )
    # The package being built is a later version; the native parts are still accepted.
    monkeypatch.setattr(release, "PACKAGE_VERSION", "1.0.1")
    reused = _read_reused(release, package)
    assert reused.native_bytes == native_bytes
    assert reused.sqlite_bytes == sqlite_bytes
    assert reused.build_record_bytes == record_bytes
    assert reused.native_binary_from == {
        "version": NATIVE_REUSE_VERSION,
        "repository_commit": NATIVE_REUSE_COMMIT,
    }
    # The DLL's version resource is held to the native version, not the package version.
    _validate_reused(release, reused)
    with pytest.raises(ValueError, match="FileVersion"):
        _validate_reused(release, reused, FileVersion="1.0.1")


def test_reuse_mode_accepts_a_package_that_itself_reused_the_native_build(
    monkeypatch, tmp_path
) -> None:
    release = _release_module()
    package, native_bytes, *_ = _earlier_release_zip(
        release,
        monkeypatch,
        tmp_path,
        version="1.0.1",
        commit="a" * 40,
        native_binary_from={
            "version": NATIVE_REUSE_VERSION,
            "repository_commit": NATIVE_REUSE_COMMIT,
        },
    )
    assert _read_reused(release, package).native_bytes == native_bytes


def test_reuse_mode_rejects_a_tampered_dll(monkeypatch, tmp_path) -> None:
    release = _release_module()
    package, native_bytes, *_ = _earlier_release_zip(release, monkeypatch, tmp_path)
    tampered = _replace_zip_members(
        package, {"plugins/fomod_logger.dll": native_bytes + b"tampered"}
    )
    with pytest.raises(ValueError, match="does not match the native release package"):
        _read_reused(release, tampered)

    # A DLL whose manifest entry was rewritten to match still fails the build record.
    forged_dll = native_bytes + b"forged"
    forged, *_ = _earlier_release_zip(
        release,
        monkeypatch,
        tmp_path,
        native_bytes=forged_dll,
        record_changes={"native_dll_sha256": release.sha256(native_bytes)},
        name="forged.zip",
    )
    with pytest.raises(ValueError, match="native_dll_sha256"):
        _validate_reused(release, _read_reused(release, forged, dll_sha256=release.sha256(forged_dll)))

    # A DLL swapped with the record AND the manifest rewritten to agree with it
    # is a consistent zip; only the hash committed in the identity refuses it.
    swapped, *_ = _earlier_release_zip(
        release, monkeypatch, tmp_path, native_bytes=forged_dll, name="swapped.zip"
    )
    consistent = _read_reused(release, swapped, dll_sha256=release.sha256(forged_dll))
    _validate_reused(release, consistent)  # the zip alone agrees with itself
    with pytest.raises(ValueError, match="native_dll_sha256 in release_identity"):
        _read_reused(release, swapped)

    swapped_sqlite, *_ = _earlier_release_zip(
        release, monkeypatch, tmp_path, sqlite_bytes=b"another sqlite", name="sqlite.zip"
    )
    with pytest.raises(ValueError, match="native_sqlite_sha256 in release_identity"):
        _read_reused(release, swapped_sqlite)
    assert _read_reused(
        release, swapped_sqlite, sqlite_sha256=None
    ).sqlite_bytes == b"another sqlite"


def test_reuse_mode_rejects_a_tampered_sqlite_runtime_or_record(
    monkeypatch, tmp_path
) -> None:
    release = _release_module()
    package, native_bytes, sqlite_bytes, record_bytes = _earlier_release_zip(
        release, monkeypatch, tmp_path
    )
    for member, data in (
        ("plugins/fomod_logger_support/native/sqlite3.dll", sqlite_bytes + b"x"),
        ("fomod_logger_docs/native-build-record.json", record_bytes + b" "),
    ):
        tampered = _replace_zip_members(package, {member: data})
        with pytest.raises(ValueError, match="does not match the native release package"):
            _read_reused(release, tampered)


def test_reuse_mode_rejects_a_record_or_package_from_another_commit(
    monkeypatch, tmp_path
) -> None:
    release = _release_module()
    wrong_record, *_ = _earlier_release_zip(
        release,
        monkeypatch,
        tmp_path,
        record_changes={"repository_commit": "e" * 40},
        name="wrong-record.zip",
    )
    with pytest.raises(ValueError, match="commit does not match native commit"):
        _validate_reused(release, _read_reused(release, wrong_record))

    genuine, *_ = _earlier_release_zip(release, monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="PrivateBuild"):
        _validate_reused(release, _read_reused(release, genuine), PrivateBuild="e" * 40)

    other, *_ = _earlier_release_zip(
        release, monkeypatch, tmp_path, commit="e" * 40, name="other.zip"
    )
    with pytest.raises(ValueError, match="does not match the release identity"):
        _read_reused(release, other)

    incomplete = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(incomplete, "w") as archive:
        archive.writestr("readme.txt", b"no native parts")
    with pytest.raises(ValueError, match="lacks"):
        _read_reused(release, incomplete)


def test_strict_mode_still_binds_the_dll_to_the_package_version() -> None:
    release = _release_module()
    commit, native_bytes, import_lib, companion, metadata, record = _native_fixture(release)
    strict = dict(
        native_bytes=native_bytes,
        uibase_import_library_bytes=import_lib,
        uibase_companion_dll_bytes=companion,
        commit=commit,
    )
    release.validate_native_inputs(native_metadata=metadata, build_record=record, **strict)
    older = {**metadata, "FileVersion": "0.9.0", "ProductVersion": "0.9.0"}
    with pytest.raises(ValueError, match="FileVersion"):
        release.validate_native_inputs(native_metadata=older, build_record=record, **strict)
    with pytest.raises(ValueError, match="commit does not match release commit"):
        release.validate_native_inputs(
            native_metadata=metadata,
            build_record={**record, "repository_commit": "e" * 40},
            **strict,
        )


def test_build_chooses_exactly_one_native_source(monkeypatch, tmp_path) -> None:
    release = _release_module()
    output = tmp_path / "out" / release.BINARY_ARCHIVE_NAME
    source_output = tmp_path / "out" / release.SOURCE_ARCHIVE_NAME
    arguments = (None, None, tmp_path / "uibase", None, None, None, {}, output, source_output)
    earlier = tmp_path / "earlier.zip"
    with pytest.raises(ValueError, match="strict mode needs"):
        release.build(*arguments, expected_commit="a" * 40)
    with pytest.raises(ValueError, match="do not pass them too"):
        release.build(
            tmp_path / "fomod_logger.dll",
            *arguments[1:],
            expected_commit="a" * 40,
            native_from_release_zip=earlier,
        )
    monkeypatch.setattr(release, "NATIVE_IDENTITY", None)
    with pytest.raises(ValueError, match="native_version, native_commit and"):
        release.build(
            *arguments, expected_commit="a" * 40, native_from_release_zip=earlier
        )


def test_manifests_name_the_reused_native_build_only_in_reuse_mode() -> None:
    release = _release_module()
    origin = {"version": NATIVE_REUSE_VERSION, "repository_commit": NATIVE_REUSE_COMMIT}

    def binary_manifest(**extra):
        archive = release.build_binary_archive_bytes(
            {"plugins/fomod_logger.dll": b"dll"},
            commit="a" * 40,
            source_filename="s.zip",
            source_sha256="b" * 64,
            runtime_source_components=[],
            **extra,
        )
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            return json.loads(zipped.read("fomod_logger-manifest.json"))

    def source_manifest(**extra):
        archive = release.build_source_archive_bytes(
            {"a.txt": b"a"},
            uibase_source_files={},
            commit="a" * 40,
            runtime_source_components=[],
            **extra,
        )
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            name = next(
                item
                for item in zipped.namelist()
                if item.endswith("fomod_logger-source-manifest.json")
            )
            return json.loads(zipped.read(name))

    for manifest in (binary_manifest, source_manifest):
        assert "native_binary_from" not in manifest()
        assert manifest(native_binary_from=origin)["native_binary_from"] == origin


# --- Reuse mode against a real Git history ---

NATIVE_SOURCE_FIXTURE = {
    "src/native.cpp": b"int native() { return 1; }\n",
    "CMakeLists.txt": b"project(fixture)\n",
    "CMakePresets.json": b"{}\n",
    "third_party/lib.txt": b"vendored\n",
    "packaging/write-native-build-record.cmake": b"# record\n",
    "packaging/write-resources.cmake": b"# resources\n",
}


def _exact_git(git, directory: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            git,
            "-C",
            str(directory),
            "-c",
            "core.autocrlf=false",
            "-c",
            "user.name=FOMOD Logger Tests",
            "-c",
            "user.email=tests@example.invalid",
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_exact(git, directory: Path, files: dict, *, first: bool = False) -> str:
    """Commit exact bytes (no line-ending conversion) and return the commit."""

    if first:
        subprocess.run([git, "init", str(directory)], check=True, capture_output=True)
        (directory / ".git" / "info" / "attributes").write_text("* -text\n", encoding="ascii")
    for name, data in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _exact_git(git, directory, "add", "-A")
    _exact_git(git, directory, "commit", "-m", "fixture")
    return _exact_git(git, directory, "rev-parse", "HEAD")


def _native_history(git, tmp_path, release, **extra_files):
    """A repository whose first commit is the native build and second a Python-only fix."""

    repository = tmp_path / "repo"
    files = {**_worktree_source_files(release), **NATIVE_SOURCE_FIXTURE, **extra_files}
    files["packaging/verify-git-source.py"] = (
        ROOT / "packaging" / "verify-git-source.py"
    ).read_bytes()
    native = _commit_exact(git, repository, files, first=True)
    plugin = files["python/fomod_logger_plugin.py"] + b"\n# python-only fix\n"
    fixed = _commit_exact(git, repository, {"python/fomod_logger_plugin.py": plugin})
    return repository, native, fixed


def test_reuse_mode_refuses_changed_native_sources_or_an_unknown_native_commit(
    tmp_path,
) -> None:
    release = _release_module()
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required")
    repository, native, fixed = _native_history(git, tmp_path, release)

    # Python-only change: native sources identical.
    release.check_native_sources_unchanged(
        repository, native_commit=native, release_commit=fixed
    )
    # Every path the DLL is built from is guarded.
    targets = {
        "src": "src/native.cpp",
        "third_party": "third_party/lib.txt",
    }
    for name in release.NATIVE_SOURCE_PATHS:
        target = targets.get(name, name)
        data = (repository / target).read_bytes()
        changed = _commit_exact(git, repository, {target: data + b"changed\n"})
        with pytest.raises(RuntimeError, match="native sources changed"):
            release.check_native_sources_unchanged(
                repository, native_commit=native, release_commit=changed
            )
    with pytest.raises(RuntimeError, match="not a commit in this repository"):
        release.check_native_sources_unchanged(
            repository, native_commit="9" * 40, release_commit=fixed
        )
    with pytest.raises(ValueError, match="40-hex"):
        release.check_native_sources_unchanged(
            repository, native_commit="HEAD", release_commit=fixed
        )


def test_reuse_mode_builds_end_to_end_from_a_clean_commit(monkeypatch, tmp_path) -> None:
    release = _release_module()
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required")
    sqlite_bytes = b"test sqlite runtime"
    qt_bytes = b"qt source archive"
    runtime_lock = {
        **release.RUNTIME_SOURCES,
        "components": [
            {
                "id": "qtbase",
                "filename": "qtbase.tar.xz",
                "official_url": "https://example.invalid/qtbase.tar.xz",
                "size": len(qt_bytes),
                "sha256": release.sha256(qt_bytes),
            }
        ],
    }
    sqlite_lock = json.loads(
        (ROOT / "packaging" / "sqlite-runtime.json").read_text(encoding="utf-8")
    )
    sqlite_lock["file_sha256"] = release.sha256(sqlite_bytes)
    repository, native, fixed = _native_history(
        git,
        tmp_path,
        release,
        **{
            "packaging/runtime-sources.json": json.dumps(runtime_lock).encode("utf-8"),
            "packaging/sqlite-runtime.json": json.dumps(sqlite_lock).encode("utf-8"),
        },
    )
    uibase = tmp_path / "uibase"
    uibase_commit = _commit_exact(git, uibase, {"src/iplugin.h": b"interface\n"}, first=True)
    qt_source = tmp_path / "sources" / "qtbase.tar.xz"
    qt_source.parent.mkdir()
    qt_source.write_bytes(qt_bytes)

    package, native_bytes, *_ = _earlier_release_zip(
        release,
        monkeypatch,
        tmp_path,
        commit=native,
        record_changes={"repository_commit": native},
    )
    package_bytes = package.read_bytes()
    seen = []

    def fake_metadata(path):
        seen.append(Path(path).read_bytes())
        return _native_reuse_metadata(PrivateBuild=native)

    monkeypatch.setattr(release, "ROOT", repository)
    monkeypatch.setattr(release, "RUNTIME_SOURCES", runtime_lock)
    monkeypatch.setattr(release, "UIBASE_COMMIT", uibase_commit)
    monkeypatch.setattr(release, "read_native_version_metadata", fake_metadata)
    monkeypatch.setattr(release, "PACKAGE_VERSION", NATIVE_REUSE_VERSION)
    monkeypatch.setattr(
        release, "NATIVE_IDENTITY", _reuse_identity(release, commit=native)
    )
    outputs = tmp_path / "outputs"
    result = release.build(
        None,
        None,
        uibase,
        None,
        None,
        None,
        {"qtbase": qt_source},
        outputs / release.BINARY_ARCHIVE_NAME,
        outputs / release.SOURCE_ARCHIVE_NAME,
        expected_commit=fixed,
        native_from_release_zip=package,
    )

    origin = {"version": NATIVE_REUSE_VERSION, "repository_commit": native}
    assert result["native_binary_from"] == origin
    assert result["repository_commit"] == fixed
    assert result["native_plugin_sha256"] == release.sha256(native_bytes)
    assert seen == [native_bytes]  # the scratch copy the version resource was read from
    assert package.read_bytes() == package_bytes  # the earlier package is never touched
    with zipfile.ZipFile(outputs / release.BINARY_ARCHIVE_NAME) as archive:
        manifest = json.loads(archive.read("fomod_logger-manifest.json"))
        assert archive.read("plugins/fomod_logger.dll") == native_bytes
    with zipfile.ZipFile(outputs / release.SOURCE_ARCHIVE_NAME) as archive:
        root = release.SOURCE_ARCHIVE_ROOT
        source_manifest = json.loads(
            archive.read(f"{root}/fomod_logger-source-manifest.json")
        )
        assert f"{root}/src/native.cpp" in archive.namelist()
    assert manifest["native_binary_from"] == source_manifest["native_binary_from"] == origin
    assert manifest["repository_commit"] == fixed

    # The same history with a changed native source is refused before anything is written.
    changed = _commit_exact(git, repository, {"src/native.cpp": b"int native() { return 2; }\n"})
    refused = tmp_path / "refused"
    with pytest.raises(RuntimeError, match="native sources changed"):
        release.build(
            None,
            None,
            uibase,
            None,
            None,
            None,
            {"qtbase": qt_source},
            refused / release.BINARY_ARCHIVE_NAME,
            refused / release.SOURCE_ARCHIVE_NAME,
            expected_commit=changed,
            native_from_release_zip=package,
        )
    assert not refused.exists()
