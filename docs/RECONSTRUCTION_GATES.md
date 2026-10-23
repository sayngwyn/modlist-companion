# Reconstruction and acceptance gates

No gate is implied by completion of a later-looking activity. A candidate is
not accepted until every gate below has explicit evidence.

## Gate 0 - Preserved baseline

- Released archives and files hash-verified.
- Native upstream base resolved exactly.
- Isolated MO2 environment and harmless regression fixture preserved.

Status: `completed` for the preserved static baseline. Live behavior is assessed
separately under Gate 4 and remains incomplete for the final candidate.

## Gate 1 - Source-complete foundation

- GPL-3.0-only license and upstream notices included.
- Exact supported MO2 version and build dependencies documented.
- Native, Python, UI, schema, migration, and packaging sources tracked.
- No released DLL or decompiled output used as a build input.
- Standalone binary package excludes deferred compatibility adapters while the
  complete corresponding-source archive preserves all tracked research and
  legacy evidence.

Status: `in_progress`. The source foundation exists, but the repaired release
contract must be committed, built, and inspected as one exact candidate.

## Gate 2 - Behavioral reconstruction

- Exact selections recorded with stable step/group/option identity.
- Skipped or invisible steps cannot be recorded as selected.
- Duplicate labels in separate groups remain distinct.
- Reinstallation has deterministic replacement/history behavior.
- Data location and cross-instance behavior are explicit.
- Search covers mod, step, group, and option names.
- Cross-patch analysis cannot duplicate one mod or collapse unrelated choices
  solely because of display-name normalization.

Status: `in_progress`. Initial-install and reinstall scenarios passed on an
earlier candidate. The final repaired candidate still requires the full test
suite and exact-package runtime requalification.

## Gate 3 - Reliability

- Native event writes are atomic and recoverable.
- Python ingestion is idempotent and safe after interruption.
- Schema and legacy migration tests pass.
- Local server binds only to loopback and handles port collisions.
- Failures are observable; no bare exception swallowing remains.

Status: `in_progress`. Reliability repairs and focused tests exist in the
working candidate; final integration, full-suite, and interruption/recovery
proof remain open.

## Gate 4 - Runtime qualification

- Clean MO2 2.5.2 portable instance loads the package without plugin errors.
- Regression fixture scenarios pass for initial install and reinstall.
- Disposable profile/instance tests establish intended storage scope.
- Fallout and F4SE are not launched.

Status: `in_progress`. Isolated MO2 qualification exists for earlier candidates.
The exact final package, storage-scope matrix, and restart persistence checks
remain open. Production MO2 and game launch are outside this gate.

## Gate 5 - Independent review

- Exact commit receives bounded, read-only source review by an independent
  reviewer.
- Material findings are reproduced and reconciled.
- Final candidate is re-tested after repairs.

Status: `pending` for the final exact commit. Reviews of earlier work and the
current worktree are findings, not acceptance of the eventual release commit.

## Gate 6 - Release readiness

- Corresponding-source archive and binary package are generated from the same
  clean commit and are reproducible and hash-recorded.
- The binary manifest names and hashes the corresponding-source archive. The
  source manifest records its commit and source-file hashes; it does not hash
  the binary archive.
- Two native builds from fresh external directories produce the same DLL hash.
  The post-link native build record binds the DLL, exact uibase import-library
  and companion-DLL hashes, configuration, SDK, Qt, compiler, toolset and source
  commit. Logger does not require Boost or execute `cmake_common`.
- Binary package contains the README, public schema, build contract, provenance,
  dated modification notice, licenses, and install/upgrade/rollback guide below
  `fomod_logger_docs/`, without generic root-document collisions.
- Every binary has corresponding source and build instructions.
- The five external Qt/PyQt/SIP source archives in
  `packaging/runtime-sources.json` are verified and hosted beside the release.
- The selected uibase version's upstream package license declaration, exact
  source identity and original notices are retained as recorded by
  `packaging/native-dependencies.json`; runtime ABI qualification remains required.
- Upgrade, rollback, and legacy-data migration are documented.
- The exact corresponding-source archive is published at a real accessible
  location beside the binary; no unverified source URL is claimed.
- Nexus permission/file changes are separately approved by the owner.

Status: `pending`. No final clean commit, two-build/two-package reproducibility
record, exact-package independent sign-off, or publication has yet satisfied
this gate.

## Post-standalone follow-up - FOMOD Plus interoperability

FOMOD Plus detection, overlap policy, and compatibility qualification begin only
after the standalone Logger has passed Gates 0 through 6. This follow-up is not
a standalone release gate and must not weaken or replace Logger's native
capture, history, search, or evidence model.
