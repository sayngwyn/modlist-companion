# Third-party and upstream notices

## Mod Organizer 2 native FOMOD installer

The native portion of FOMOD Logger is based on the Mod Organizer 2 FOMOD
installer developed by the Mod Organizer 2 contributors, including Sebastian
Herbord (Tannin) and thosrtanner.

- Upstream repository:
  https://github.com/ModOrganizer2/modorganizer-installer_fomod
- Reconstruction base commit:
  `fc263f2d923c704b4853c11ed4f8b8cf3920f30d`
- Upstream grant: GNU General Public License version 3 or later

FOMOD Logger changes the plugin identity and adds structured installation-event
recording. `MODIFICATIONS.md` is the prominent notice that these upstream files
were changed on 2026-08-31 and describes the nature of those changes. The
corresponding-source archive contains the exact tracked snapshot, source
manifest, and upstream base identity. Git history is retained by the project
repository, not embedded in that ZIP.

Those upstream files remain available under their GPL-3.0-or-later grant.
FOMOD Logger's combined release selects and conveys GNU GPL version 3 terms and
labels its original contributions GPL-3.0-only. That release choice does not
narrow the upstream grant.

## FOMOD Plus interoperability

FOMOD Plus is developed separately at:
https://github.com/aglowinthefield/mo2-fomod-plus

FOMOD Plus is not included in either package. The standard package does not
work with FOMOD Plus. The separate "Modlist Companion (for FOMOD Plus)" package
reads the choices FOMOD Plus saves (through MO2's plugin settings) and was
tested with FOMOD Plus 1.20.0 on MO2 2.5.2. That support is independently
implemented against FOMOD Plus's documented or observed output; it does not
copy FOMOD Plus implementation code.

## CPython 3.12.3 SQLite runtime

The binary package includes `sqlite3.dll` from the Python Software Foundation's
official CPython 3.12.3 Windows embeddable distribution. The exact package and
file hashes are recorded at `packaging/sqlite-runtime.json` in source and below
`fomod_logger_docs/packaging/` in the binary archive. The CPython license is at
`third_party/python-3.12.3/LICENSE.txt` in source and below
`fomod_logger_docs/third_party/` in the binary archive; it must accompany the
redistributed DLL.

- Release: https://www.python.org/downloads/release/python-3123/
- License: Python Software Foundation License Version 2

SQLite itself is dedicated to the public domain:
https://www.sqlite.org/copyright.html

## MO2 uibase interfaces

Logger consumes the unmodified source headers from upstream uibase tag
v2.6.0-dev.0, commit 219dd967dea2f0b72a894ed67155b62dd694ebc2.
The official ModOrganizer2 package registry at commit
2a9bda65de449abf8410d18477cb97417a216db8 declares this version GPL-3.0.
Its declaration and immutable provenance are included under
third_party/uibase-license-evidence. This evidence does not replace or narrow
the original LGPL-3.0-or-later, GPL-3.0-or-later and permissive file notices.

The corresponding-source archive includes the complete exact selected snapshot.
Logger links against the separately hash-recorded MO2 2.5.2 uibase import library;
the build-prefix companion DLL is a comparison input and is not distributed.
See docs/UIBASE_DEPENDENCY.md for the source/host distinction and qualification.

## Qt 6.7.1 and PyQt6 6.7.1

Logger links to the host's Qt Core, Gui and Widgets and uses its PyQt6 bindings.
It does not distribute replacement Qt or PyQt DLLs. Qt is developed by The Qt
Company and contributors; PyQt is developed by Riverbank Computing. Qt offers
commercial and open-source licenses with component-specific terms. Preserve
the notices in the exact source archives. The upstream overview is at
https://doc.qt.io/qt-6/licensing.html.

The pinned public PyQt6 source archive contains GPL version 3 terms. PyQt is
available under GPL v3 or a Riverbank commercial license, not LGPL:
https://www.riverbankcomputing.com/software/pyqt/intro.
This reconstruction uses the GPL distribution. The GPL text is in `LICENSE`;
the LGPL version 3 text is in `third_party/licenses/LGPL-3.0.txt`.

The source-hosting policy includes Qt Base and Qt Declarative 6.7.1 and
PyQt6 6.7.1. Qt Declarative covers the recorded MO2/PyQt dependency lineage;
it is not an added Logger native link target.

## SIP and PyQt6-sip

The MO2 2.5.2 recipe pins SIP 6.8.6 and generates PyQt6-sip 13.8.0 locally.
The public source archives and generator source are locked in
`packaging/runtime-sources.json`, with upstream license notices retained in
those unmodified archives. Neither the public source distribution nor a public
wheel is asserted to be byte-identical to MO2's locally generated module.

## Sayngwyn Serif font

`plugins/fomod_logger_support/viewer/assets/SayngwynSerif-Regular.ttf` is
Sayngwyn Serif, a Modified Version of Cinzel (Copyright 2020 The Cinzel
Project Authors) and Cormorant Garamond (Copyright 2015 The Cormorant Project
Authors), made by sayngwyn in 2026. It is licensed under the SIL Open Font
License, Version 1.1, whose full text ships beside it as `OFL.txt`. The font
may not be sold on its own.

## sayngwyn skull mark

The sayngwyn skull mark (`plugins/fomod_logger_support/viewer/assets/sayngwyn-mark.png`)
is © sayngwyn, all rights reserved. It is not covered by this project's
GPL-3.0 licence and is included only for this program's window. Forks must
remove or replace it.

## Corresponding source

The binary manifest identifies and hashes the deterministic
corresponding-source archive for the same clean repository commit. This is a
one-way binary-to-source binding; the source manifest records the commit and
hashes of its source files but does not identify or hash a binary archive.
Binary publication must place that exact source archive at a real, accessible
location beside the binary. No publication URL is claimed until that release
gate is satisfied.

The five external source archives in `packaging/runtime-sources.json` must
also be hosted under owner control beside the pair under the current release
policy. The manifests record their exact names, sizes and hashes. This source
inventory does not resolve the separately recorded uibase publication gate.
