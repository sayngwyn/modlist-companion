# Issue and regression matrix

| ID | Report or finding | Root cause status | Required evidence |
|---|---|---|---|
| FL-001 | FOMOD Plus build does not log in global instances | Deferred post-standalone; not a standalone release gate | Future global and portable compatibility matrix after standalone acceptance |
| FL-002 | Skipped options appear selected | Repaired in standalone native capture; final-candidate qualification pending | Duplicate-label, invisible-step, and conditional-step regressions pass against the exact packaged DLL |
| FL-003 | Header search missing | Repaired in source; final UI qualification pending | Step and group queries return the correct install in the packaged UI |
| FL-004 | Cross-patch duplicates on reinstall | Repaired in source: derived analysis uses current events, stable instance partitions, conservative label normalization, and one mention per mod; packaged/runtime requalification pending | Two reinstalls produce one deterministic current record and valid history; cross-patch export has no duplicate mod mention |
| FL-005 | Logs appear to disappear between instances | Storage and stable identity repaired in source; disposable profile/instance matrix remains open | MO2 installation-shared spool/database plus visible instance identity tested across profile, restart, and separate portable instance |
| FL-006 | Fixed port 8080 collision | Dynamic loopback binding implemented; final runtime proof pending | Dynamic loopback port test and occupied-port test |
| FL-007 | Silent failures | Repair in progress: health, import, native-write, and UI diagnostics are being made durable and observable | Unit tests plus isolated runtime diagnostics show actionable failures and recovery |
| FL-008 | Standalone DLL source unavailable | Reconstructed source is tracked; publication remains open | Reproducible GPL build plus deterministic corresponding-source archive published beside the binary |
| FL-009 | Stale/inconsistent version and author metadata | Central identity added; exact native rebuild and package validation pending | Package manifest, Python, MO2, PE metadata, filenames, and documentation agree exactly |
| FL-010 | Unsafe/dead DLL replacement code | Replacement behavior removed from the standalone design | Package installs beside and leaves `installer_fomod.dll` byte-identical through install, upgrade, rollback, and uninstall rehearsal |
| FL-011 | No install, upgrade, or rollback contract | Repaired in documentation; release-package inclusion pending qualification | Package contains the guide and an isolated upgrade/rollback rehearsal preserves Logger data and the stock installer |
| FL-012 | Package could claim an arbitrary DLL came from the current commit | Repaired in packaging design; final clean-build proof pending | Packager rejects wrong architecture, version, commit, toolchain record, source identity, and noncanonical outputs |
| FL-013 | GPL modification date and changed-source notice absent | Prominent 2026-08-31 notice added; final source/binary inclusion pending | `MODIFICATIONS.md` and retained upstream notices appear in both exact release archives |
