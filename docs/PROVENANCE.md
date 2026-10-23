# Provenance record

## Recovered releases

| Artifact | SHA-256 |
|---|---|
| FOMOD Logger 1.0.5 archive | `369FD554B4FCDC3154224792BD4DDDE7ABB8DB948715B51687EEE8E1F1FF78FC` |
| FOMOD Logger and FOMOD Plus 1.0.0 archive | `04798A25A3826DFE9BAB3D50ECE2FC4562D15EE1372453B178B63FFD53879E13` |
| Released `fomod_logger.py` | `847EDC50C399CBA4A9FE9FD10933A93863EA5354227E5C417F61595FBA75A269` |
| Released `fomod_logger.dll` | `F091D1BF31D173E776B10258C0C55802B2590FA9FF5CBBA382525BB508796F2E` |
| Released `fomod_logger_plus.py` | `F160F733C78473366B2A58C67FF3EDC969700E5A3B13C439552E20B34EEB338D` |

The recovered Python files are preserved byte-for-byte under `legacy/python`.
The DLL is not stored in or used by this repository.

## Native upstream base

The MO2 2.5.2 combined source archive was compared against the official
`modorganizer-installer_fomod` Git history. All 21 files common to the bundled
installer and upstream commit
`fc263f2d923c704b4853c11ed4f8b8cf3920f30d` match byte-for-byte. There were no
hash mismatches. The only upstream-only files were repository infrastructure and
the English translation source.

The reconstruction branch was created directly from that commit with upstream
history intact. The `upstream` remote has its push URL disabled so reconstruction
work cannot accidentally be pushed to the MO2 repository.

`MODIFICATIONS.md` provides the prominent 2026-08-31 notice and description of
the Logger changes. The project repository retains the Git history. A generated
corresponding-source ZIP contains the exact tracked release snapshot and source
manifest, but intentionally contains no `.git` directory or repository history.

## Binary evidence

Static inspection of the released DLL established that it contains the stock
`FomodInstallerDialog` and `FomodScreenshotDialog`, imports MO2 `uibase.dll`,
adds QJson handling, changes the class to `FomodLoggerInstaller`, and adds the
plugin IID `org.fomod_logger.FomodLogger`. No usable source paths or PDB were
embedded.

This evidence is used only to establish provenance and expected behavior. No
decompiled output or binary-derived source is included.

## Reconstructed release binding

`release_identity.json` centralizes the public, Python/MO2-safe, and Windows
version representations and the canonical archive filenames. Release builds
embed the exact 40-hex repository commit in the native PE version resource and
record the pinned toolchain and dependency identities in
`fomod_logger-native-build-record.json`.

The format-2 record is written after linking beside the DLL in the build's
`src/RelWithDebInfo` directory. It binds that exact DLL and the consumed uibase
import library and companion DLL by SHA-256. The companion is evidence of the
build prefix only, not the library loaded by a qualified MO2 host.

The binary package is assembled from that validated native binary, locked
SQLite runtime, native build record, and exact Git blobs at the same clean
commit. Its manifest identifies and hashes the corresponding-source archive.
The source archive's own manifest records the commit and hashes every tracked
source file. It does not hash the binary archive, so the release contract is a
one-way binary-to-source binding rather than a mutual binding.

The source ZIP also carries the exact uibase snapshot named in
`packaging/native-dependencies.json`. The separate Qt/PyQt/SIP source archives
and the MO2 SIP derivation are recorded in `packaging/runtime-sources.json`.
The CPython embeddable ZIP used to obtain SQLite is a binary distribution,
not a Python source archive. Public release still requires the recorded
uibase license-coverage confirmation and exact-package runtime qualification.
