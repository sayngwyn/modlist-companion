# uibase dependency and licensing evidence

Logger remains a GPLv3 standalone MO2 plugin. It uses uibase's published API.

## Selected source

The complete, unmodified source tree is upstream tag v2.6.0-dev.0 at commit
219dd967dea2f0b72a894ed67155b62dd694ebc2. The ModOrganizer2 official package registry
declares this exact package version GPL-3.0 at immutable registry commit
2a9bda65de449abf8410d18477cb97417a216db8:

https://github.com/ModOrganizer2/vcpkg-registry/blob/2a9bda65de449abf8410d18477cb97417a216db8/ports/mo2-uibase/vcpkg.json

The raw declaration, package recipe, URLs and hashes are retained in
third_party/uibase-license-evidence. Original notices remain unmodified, including
LGPL-3.0-or-later interfaces, GPL-3.0-or-later files and the permissive Trolltech
LineEditClear notice. Both GPLv3 and LGPLv3 license texts accompany the package.
The official metadata is the project licensing basis; this documentation is
not a substitute grant or a legal warranty. No new upstream contact was made.

The older 2020 licensing discussion is supporting context, not the sole basis:
https://github.com/ModOrganizer2/modorganizer/issues/1106

The historic binary release asset currently returns 404 from GitHub's release
API. This build uses the available, exact Git source tag, not that asset.
The metadata declaration names the package version rather than spelling out
every file; this is retained transparently rather than described as individual
written permission from every contributor.

## MO2 compatibility and binary input identity

The supported host is still portable MO2 2.5.2 with Qt 6.7.1. This is not a
replacement or upgrade of MO2's uibase.dll. Logger does not ship that DLL.
The import library and build-prefix companion DLL remain the separately
hash-recorded MO2 2.5.2 inputs from source commit
44201d70f7c1a6cda55da86458dc5b8b4665c47b.

The native build record's uibase_commit identifies the selected HEADER SOURCE,
not the source revision of the host or build-prefix DLL. The latter's provenance
is recorded separately by binary_inputs_source_commit in native-dependencies.json.

Of the 32 headers consumed by the previously qualified candidate, 31 are exactly
byte-identical in the selected source. imoinfo.h changes only one include path.
All declarations and inline definitions in that consumed set are unchanged.
The machine-readable comparison is in consumed-header-comparison.json.
This is bounded source-level ABI evidence, not proof that all exports or the
entire newer uibase binary are interchangeable. The host binary is not replaced.

Fresh native builds, source-ZIP reproduction and exact-package isolated runtime
checks are required for each new candidate. Prior runtime reports remain bound
to their own candidate hashes.

## Source distribution

Ship the full selected uibase source tree embedded in the matching Logger source
ZIP, the upstream declaration evidence, preserved notices and license texts.
The five separately pinned Qt/PyQt/SIP source archives remain required by the
project's source-hosting policy. Publication and an actual hosting destination
are separate from successful local packaging.
