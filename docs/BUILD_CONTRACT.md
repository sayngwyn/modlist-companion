# Build and release contract

This contract describes the standalone Logger build and paired package.
Passing these checks produces a candidate, not publication approval. See
`RECONSTRUCTION_GATES.md` for runtime, independent-review and publication gates.
The GitHub workflow is a compatibility build only.

## Pinned native target

- Native upstream: `modorganizer-installer_fomod` commit
  `fc263f2d923c704b4853c11ed4f8b8cf3920f30d`.
- MO2 ABI: 2.5.2; Windows x86-64.
- Generator: Visual Studio 17 2022, x64; platform toolset `v143`.
- Windows SDK: 10.0.22621.0 exactly.
- Qt: 6.7.1 exactly, official `win64_msvc2019_64` Core, Gui and Widgets.
- uibase: `219dd967dea2f0b72a894ed67155b62dd694ebc2`; exact source headers,
  from upstream tag v2.6.0-dev.0. The x64 import library and companion DLL
  remain the MO2 2.5.2 build-prefix inputs from 44201d70f7c1a6cda55da86458dc5b8b4665c47b,
  supplied and hash-recorded separately. No host uibase DLL is redistributed.
- Configuration: `RelWithDebInfo`; output `fomod_logger.dll`.
- Tools: CMake 3.25 or newer, Git, PowerShell and Python 3.12 or newer.

Logger's CMake is project-owned. It neither requires Boost nor executes
`cmake_common` or uibase CMake files. Building the uibase binary inputs is a
separate dependency preparation step. The `v143` pin identifies a toolset
family; the record captures the actual compiler and CMake versions.

Logger installs beside `installer_fomod.dll`. It must never overwrite,
rename, restore or delete MO2's stock installer.

## Exact source and external build directories

`release_identity.json` supplies public SemVer `1.0.0`, Python/MO2
version `1.0.0`, numeric Windows version `1,0,0,0`, and archive filenames.
`FOMOD_LOGGER_GIT_COMMIT` must be the explicit lowercase 40-hex release commit.
The PE resource embeds it; a dirty developer build does not prove that its
complete source matches that commit.

Release Git source must contain exactly the committed files and required
directories, plus Git control metadata. Untracked and ignored paths, caches,
in-tree builds, reparse points, submodules, exceptional index flags and
unsupported clean filters are rejected. The verifier checks actual content,
not just Git status. Keep the pinned uibase source clean too; use binary inputs
from a separate build directory.

The preset requires an absolute external `FOMOD_LOGGER_BUILD_DIR`. Configure,
pre-build and post-link checks verify source. Git mode materializes the Python
verifier from the expected Logger commit. The reviewed commit and independent
review establish trust; editable repository scripts cannot authenticate
themselves against coordinated tampering.

Extracted source is checked against `fomod_logger-source-manifest.json`,
including its exact file set and hashes. It must not contain `.git` or an
in-tree build directory. Its embedded uibase tree supplies the headers.

## Configure and build

Prepare fresh release-only checkouts with Git line-ending conversion disabled:

```powershell
git -c core.autocrlf=false clone --no-checkout 'REPLACE_WITH_LOGGER_REPOSITORY' D:\fomod-logger-source
git -C D:\fomod-logger-source config core.autocrlf false
Set-Content -LiteralPath 'D:\fomod-logger-source\.git\info\attributes' `
  -Value '* -text' -Encoding ascii
git -C D:\fomod-logger-source checkout --detach 'REPLACE_WITH_RELEASE_COMMIT'
```

Apply the same recipe to the pinned uibase source. The local info/attributes
override is necessary because upstream tracked attributes explicitly request
CRLF for C++ files; core.autocrlf=false alone does not disable that conversion.
This release-only setting leaves tracked attributes and historical blobs intact.
Do not normalize
existing working directories as a release-preparation shortcut. The release
source ZIP contains exact Git blob bytes. MSVC embeds source-file checksums in
its PDB identity, so a CRLF-converted checkout can produce identical machine
code but a different DLL hash from an LF source archive. For byte-identical
source-ZIP reproduction, use the committed bytes, including any CRLF bytes
already present in an upstream blob. A fresh source-ZIP rebuild and hash
comparison are required in addition to two builds from the Git checkout.

Run from clean Logger source. Relocate the example paths as needed. The install
prefix is a staging destination; these commands only build. Set every variable
used by the preset:

```powershell
$fomodLoggerCommit = (git rev-parse --verify HEAD).Trim()
if ($fomodLoggerCommit -notmatch '^[0-9a-f]{40}$') {
  throw 'Git did not return a lowercase 40-hex commit.'
}
$env:FOMOD_LOGGER_GIT_COMMIT = $fomodLoggerCommit
$env:FOMOD_LOGGER_BUILD_DIR = 'D:\fomod-logger-builds\build-a'
$env:FOMOD_LOGGER_INSTALL_PREFIX = 'D:\fomod-logger-stage'
$env:FOMOD_LOGGER_QT6_DIR = 'D:\Qt\6.7.1\msvc2019_64\lib\cmake\Qt6'
$env:FOMOD_LOGGER_UIBASE_SOURCE_DIR = 'D:\fomod-logger-deps\uibase-source'
$env:FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY = 'D:\fomod-logger-deps\bin\uibase.lib'
$env:FOMOD_LOGGER_UIBASE_COMPANION_DLL = 'D:\fomod-logger-deps\bin\uibase.dll'

cmake --preset release-vs2022-x64
if ($LASTEXITCODE) { throw 'Release configure failed.' }
cmake --build --preset release-vs2022-x64
if ($LASTEXITCODE) { throw 'Release build failed.' }
```

For extracted source, replace the Git lookup and uibase source path with:

```powershell
$fomodManifest = Get-Content '.\fomod_logger-source-manifest.json' -Raw |
  ConvertFrom-Json
$env:FOMOD_LOGGER_GIT_COMMIT = [string]$fomodManifest.repository_commit
$env:FOMOD_LOGGER_UIBASE_SOURCE_DIR = Join-Path (Get-Location) `
  'external\modorganizer-uibase-219dd967dea2f0b72a894ed67155b62dd694ebc2'
```

The DLL and `fomod_logger-native-build-record.json` both appear under
`$env:FOMOD_LOGGER_BUILD_DIR\src\RelWithDebInfo`.

The format-2 record is published atomically after linking and binds the DLL
SHA-256, configuration, source identity, toolchain, SDK, Qt and exact uibase
import-library and companion-DLL hashes. The companion is a build-prefix
comparison input, not evidence of which DLL an MO2 host loads. The packager
separately checks PE architecture, versions, commit and debug state, and rejects
a developer record with `release_build=false`.

Repeat in a second fresh external build directory using identical source and
dependency inputs, and compare DLL and build-record hashes. Inspect the linked
PDB reference: it should name `fomod_logger.pdb` without an absolute machine
path. A single successful build is not a reproducibility result.

The native target uses relative moc includes, specific-first compiler path
maps, and `packaging/write-resources.cmake`. That resource generator fixes
resource timestamps to the ZIP epoch (1980-01-01 UTC) and removes only the
absolute source-directory prefix from generated comments. Resource names and
payload bytes stay intact. This keeps generated-source debug checksums stable
across Git and extracted-source layouts.

## Package the paired candidate

Use the same clean Git source and explicit expected commit. The source ZIP
includes all tracked Logger files and the exact uibase snapshot; the binary
ZIP excludes deferred FOMOD Plus code. Both include required notices.

Verify SQLite against `packaging/sqlite-runtime.json`: `sqlite3.dll` 3.45.1
comes from the CPython 3.12.3 Windows embeddable distribution. That embeddable
ZIP is a binary distribution, not Python source. The five external source
inputs below must match `packaging/runtime-sources.json`.

```powershell
$fomodNativeDir = Join-Path $env:FOMOD_LOGGER_BUILD_DIR 'src\RelWithDebInfo'
$fomodSources = 'D:\fomod-logger-deps\runtime-sources'
python -B .\packaging\build_release.py `
  --expected-commit $env:FOMOD_LOGGER_GIT_COMMIT `
  --native-plugin "$fomodNativeDir\fomod_logger.dll" `
  --native-build-record "$fomodNativeDir\fomod_logger-native-build-record.json" `
  --uibase-source $env:FOMOD_LOGGER_UIBASE_SOURCE_DIR `
  --uibase-import-library $env:FOMOD_LOGGER_UIBASE_IMPORT_LIBRARY `
  --uibase-companion-dll $env:FOMOD_LOGGER_UIBASE_COMPANION_DLL `
  --sqlite-dll 'D:\python-3.12.3-embed-amd64\sqlite3.dll' `
  --qtbase-source "$fomodSources\qtbase-everywhere-src-6.7.1.tar.xz" `
  --qtdeclarative-source "$fomodSources\qtdeclarative-everywhere-src-6.7.1.tar.xz" `
  --pyqt6-source "$fomodSources\PyQt6-6.7.1.tar.gz" `
  --pyqt6-sip-source "$fomodSources\PyQt6_sip-13.8.0.tar.gz" `
  --sip-source "$fomodSources\sip-6.8.6.tar.gz" `
  --output 'D:\fomod-logger-release\a\modlist-companion-1.0.0.zip' `
  --source-output 'D:\fomod-logger-release\a\modlist-companion-1.0.0-source.zip'
if ($LASTEXITCODE) { throw 'Packaging failed.' }
```

Outputs use canonical names, live outside source and must not already exist.
Repeat with the second native build and a separate output directory, then
compare both ZIP hashes. Packaging rejects source drift, incompatible native
inputs and unpinned runtime archives.

Publication does not overwrite existing output files. If writing the second
archive fails, the already published first archive is retained and the command
reports failure. This is safe partial output, not a successful pair; inspect it
and use a fresh output directory for a retry.

The binary manifest names and hashes its source ZIP; the source manifest does
not hash the binary. The five external source archives stay separate and are
recorded in the manifests. The current publication policy requires hosting
them under owner control beside both Logger archives.

## Runtime and publication boundaries

The target MO2 recipe pins Python 3.12.3, PyQt6 6.7.1 and a locally generated
PyQt6-sip 13.8.0 runtime built with SIP 6.8.6. Its `sip.pyd` is not claimed
to originate from the byte-identical public wheel. Derivation and archive roles
are recorded in `packaging/runtime-sources.json`.

Logger supplies SQLite privately below `fomod_logger_support/native`, without
replacing shared Python or Qt files. The final package must pass isolated MO2
installation, reinstall, storage, restart, recovery and rollback qualification.
Source tests and build-prefix hashes cannot substitute for runtime evidence.

The selected uibase version has an official upstream GPL-3.0 package declaration.
The immutable declaration, source-tag identity and consumed-header comparison are
retained under third_party/uibase-license-evidence. Preserve the existing LGPL,
GPL and permissive file notices. See UIBASE_DEPENDENCY.md for the evidence basis
and the separation of source headers from host binary inputs. Packaging does not
authorize publication; exact source hosting and runtime qualification still apply.
