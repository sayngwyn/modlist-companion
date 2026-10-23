# Modification notice - 2026-08-31

## Additional changes - 2026-10-21

Modlist Companion is now released in two editions. The standard edition adds,
beside the native installer's capture, an update helper, pick markers, new-patch
alerts, export and replay of choices, a file-origin lookup and a choices
comparison. A second, Python-only edition for FOMOD Plus users reads the choices
FOMOD Plus saved and rebuilds the installation record from the mod's download
archive; it contains no native code and none of FOMOD Plus's code. Database
schema 7 stores the extra data. The upstream-derived native installer also
selects previous or imported choices again (only when asked, or by default for
a mod's own previous install) and records the files each option installs.

## Additional changes - 2026-10-20

The standalone Python entry now bootstraps its sibling support package for MO2
direct-file loading. The local viewer adds focused literal choice search,
selection and instance filters, sorting, expand/collapse, conservative shared
choice states and persistent review reminders. Database schema 4 stores notes
separately from immutable events; history export format 2 includes those notes.
The viewer is a window inside MO2; opening it at startup is optional and off by default. Native installer
capture behavior and the separate Plus product are unchanged.

This is a prominent notice that, on 2026-08-31, the Mod Organizer 2 FOMOD
installer at upstream commit
`fc263f2d923c704b4853c11ed4f8b8cf3920f30d` was modified for FOMOD Logger.

The changes include a distinct plugin identity, structured and immutable
installation-choice capture, atomic event writes and diagnostics, a local history
service and interface, migration and export support, deterministic packaging,
and release/source provenance controls. The original upstream files remain
available under their GPL-3.0-or-later grant; FOMOD Logger's combined release is
conveyed under GPL-3.0-only as described in `THIRD_PARTY_NOTICES.md`.

Modified upstream-derived source and build material includes the native FOMOD
dialog and installer implementation, its English translation, and the CMake,
qmake, SCons, and continuous-integration build definitions. New Logger material
includes the native event writer and version resource, Python plugin and support
modules, local interface, schemas, tests, release tooling, and documentation.

This notice accompanies both the modified source snapshot and the binary
package. The source manifest proves the exact repository commit and hashes every
file in that snapshot. It is not a change log and does not, by itself, identify
which files differ from upstream; that comparison is anchored by the upstream
commit named above and the project repository's Git history. The source ZIP
does not contain a `.git` directory or repository history.
