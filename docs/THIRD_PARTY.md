# Third-party runtime material

## SQLite runtime

FOMOD Logger uses Python's standard-library `sqlite3` module for durable event
storage. MO2 2.5.2 includes `_sqlite3.pyd` but omits its required
`sqlite3.dll` dependency.

Release packages therefore include `sqlite3.dll` 3.45.1 from the official
CPython 3.12.3 Windows embeddable package. It is installed below the Logger's
private `fomod_logger_support/native` directory and loaded with
`os.add_dll_directory`; the package does not alter MO2's shared Python runtime.

Exact source-package and file hashes are recorded at
`packaging/sqlite-runtime.json` in source and at
`fomod_logger_docs/packaging/sqlite-runtime.json` in the binary package. SQLite
is in the public domain. CPython's license is preserved at
`third_party/python-3.12.3/LICENSE.txt` in source and at the corresponding path
below `fomod_logger_docs/` in the binary package. These notices and licenses
must remain in every redistributed binary/source release pair.

## Host dependencies and external source material

Logger consumes the pinned uibase interface and the host's Qt/PyQt/SIP runtime.
It does not install replacement copies of those shared host DLLs.
`../THIRD_PARTY_NOTICES.md` records their roles and licensing boundaries;
`BUILD_CONTRACT.md` lists every required packaging input.

The source ZIP embeds the pinned uibase snapshot. Five separately supplied,
hash-locked Qt Base, Qt Declarative, PyQt6, PyQt6-sip and SIP source archives
are recorded in `packaging/runtime-sources.json`. The CPython embeddable ZIP
is the binary source of the supplied SQLite DLL, not a Python source archive.
The unresolved uibase license coverage remains a public-release gate.
