# Architecture

## Product boundary

FOMOD Logger is an independent MO2 plugin. ModLabs can provide external test
infrastructure, but Logger does not load or call a ModLabs bridge, MCP service,
named pipe, state directory, Control Tower, or worker at runtime.

## Components

### Native installer plugin

The native plugin remains a fork of MO2's FOMOD installer so it can observe the
actual control state that produced the installed file tree. After a successful
install it serializes one immutable, versioned event and writes it atomically
(Qt `QSaveFile`: temporary file, then rename) to `events/<event-id>.json`. It
refuses to replace an existing event file. If the write fails, the error is
logged and kept as an MO2 diagnostic ("installation choices could not be
saved") naming the mod, for the rest of the MO2 session. There is no second
copy or recovery folder.

The event is written in `onInstallationEnd`, after MO2 has finished copying
the mod, because only then are the final mod name and success known. A crash
between the dialog closing and that call loses the record; reinstalling the
mod records it again.

#### Patch evidence (masters)

For options the user skipped (and automatic installs that did not apply), the
plugin reads the master list of each plugin they would have installed. That
needs a second, silent extraction of just those plugins after **Install**: no
progress dialog, nothing to cancel, and a failure leaves only the masters
unread. It is skipped when the archive is larger than **128 MB** or more than
400 plugins are involved: silent extraction blocks MO2's UI thread, and
extracting from a big solid archive is slow, so the cap keeps the worst-case
freeze short. Options that install no plugins get no `masters` field either.
When skipped or unreadable, `masters` is omitted from the option (meaning "not
read"), never written as an empty list ("no masters").

The header reader accepts a TES4 record with either Oblivion's 20-byte record
header or the 24-byte header of later games, and reports "not read" for any
other file (including Morrowind's TES3 and compressed headers).

#### Files per option ("which option put this here?")

Each option and each automatic install also records the `<file>`/`<folder>`
entries ModuleConfig.xml defines for it (`files`: `source` and `destination` as
written, `resolved` = where it lands relative to the mod folder with forward
slashes, a leading `Data/` dropped and case kept, `kind`, `priority` when not 0,
and `order`, its position in the XML). A destination outside the mod folder
(`..`, a drive letter, a UNC path) is recorded with `escapes: true` and an empty
`resolved`; empty `resolved` otherwise means the mod folder itself, and only for
folders. The FOMOD's `requiredInstallFiles` (installed whatever you pick) go in
a separate top-level `required_files` object, not in `automatic_installs`, which
the patch checks treat as conditional installs.

This is the XML definition, not the extracted file list, so it is small even for
huge texture packs. An option's list is in the order the installer applies it
(ascending priority, then XML position); across options the winner is the entry
with the highest (priority, `order`). Caps: 2,000 entries per option, 20,000 per
event, and about 2 MiB of entries per event, so a whole event stays well under
the 4 MiB the previous-choices reader accepts. The budget goes first to the
required files, then to picked options and automatic installs that applied, then
to skipped options. When an item has to be cut, the entries that win (highest
priority, then latest in the XML) are kept, in their original order, and
`files_truncated` is set. An empty list means "recorded, installs nothing"; no
`files` key means the event predates the field (the schema stays at version 1:
every new field is optional).

`core/file_origin.py` reads this. A path matches an exact destination, a folder
entry above it, or a listed plugin. Files are installed in ascending priority,
ties in XML order, and a later entry overwrites an earlier one, so among picked
options the exact-file entry with the highest (priority, `order`) is the winner.
A folder entry may or may not hold the file (what is inside it is not recorded),
so it only outranks files when no exact entry exists; otherwise it "may also
write it".

#### Settings

Beside the stock settings `use_any_file` and `see_disabled_mods`, `prefer`
(default on) sets priority 120, ahead of MO2's XML and C# FOMOD installers
(110 preferred / 90 otherwise), which Logger needs to record choices. Off, it
drops to 80, so those installers handle FOMODs first. The hidden
`ignored_missing_files` list is read from both Logger's settings and the stock
`Fomod Installer` entry.

`restore_previous_choices` (default on) applies a mod's previous choices
automatically when its dialog opens (see below).

#### Previous choices

When the dialog opens, the installer looks in `<pluginDataPath>/fomod_logger/events`
for this mod's newest recorded install: same instance id, mod name equal
case-insensitively to the proposed name, MO2's other name guesses or the mod being
reinstalled, otherwise the archive file name (`source.archive`, file name only,
case-insensitive); the newest `completed_at` wins and a name match beats an archive
match. Files over 4 MB, unreadable files and malformed events are skipped silently,
at most 5000 files are looked at (newest files first), and files that do not contain
the instance id are rejected before any JSON is parsed. The lookup lives in
`fomodpreviouschoices.cpp` and needs only Qt Core.

Options are matched to the recorded event by (step name, group name, option name,
occurrence of that triple), counted through the event in recorded order: the same
rule as `_option_paths` in `reviews.py`, and never by id, which is positional and
changes between builds and mod updates. Options of skipped steps, and options the
FOMOD had forced or blocked, were not choices and are ignored. With no match there is
no button, no tag and no change in behaviour.

Matched options that were selected get a `(previous)` suffix on the button text only;
the author's name stays in the `fomodLoggerName` property, which the log, the matching
and the screenshot viewer use. **Select previous** applies the recorded selection to
the current page and to each later page as it is shown (also switched on for the whole
dialog by `restore_previous_choices`); pages already passed are not touched, because
changing them could change which pages are visible. Per group: SelectAll is left
alone, disabled options are never changed, radio groups check the wanted enabled
option (or None for SelectAtMostOne when nothing was picked), checkbox groups follow
the record, and a SelectAtLeastOne group is never left empty. Unmatched options keep
the dialog's own defaults.

The native component does not own UI history, hiding, searching, or cross-patch
analysis.

### Python service plugin

The Python plugin:

- discovers and ingests event files idempotently;
- migrates the legacy `fomod_data.json` format;
- stores durable normalized records;
- shows everything in a Qt window inside MO2 (no web server or browser);
- handles removal/hiding/export without rewriting native events;
- reports actionable errors through MO2 logging and diagnostics.

Support modules live below the non-plugin namespace directory
`fomod_logger_support`. It intentionally has no top-level `__init__.py`, so
MO2's Python loader does not mistake support code for a second plugin. Native
runtime dependencies are loaded only from `fomod_logger_support/native`.

### Window

`fomod_logger_support/viewer` holds the window. `model.py` works out what to
show from the database and the plugin's load-order snapshot, as plain data
that tests can check without Qt; `window.py` draws it with PyQt6 widgets from
MO2's own Python, so it follows MO2's theme. The window is a top-level window,
not a dialog, so MO2 stays usable while it is open. It reads the database on
MO2's main thread, and the plugin asks it to redraw whenever an import,
removal, load-order or mod-state change happens; nothing polls. Search operates
across mod, step, group and option names. Mod-authored text is always shown as
plain text, never as HTML.

### Deferred FOMOD Plus compatibility research

FOMOD Plus interoperability is intentionally outside the first standalone
release. The standalone binary does not contain the research adapter and does
not auto-detect or ingest FOMOD Plus data. Tracked research remains in the
corresponding-source archive so the complete source context is preserved
without turning unfinished compatibility into a runtime feature or release
gate.

After standalone acceptance, any compatibility provider must remain optional,
must not key selections by display label alone or assume
`organizer.basePath()/logs`, and must not supersede Logger's native capture,
history, search, or evidence model.

## Storage boundaries

- Native event spool: shared by instances using the same MO2 installation via
  MO2's `pluginDataPath()`. Every event is tagged with its originating instance.
- Durable service database: shared by instances using the same MO2 installation;
  current-install views remain partitioned by stable instance identity.
- Stable instance identity: native and legacy-migration paths hash the same
  lexically cleaned, forward-slash, Unicode-case-folded absolute MO2 base path.
  Display labels and profile names do not affect the identity. Any future
  compatibility provider must reuse this contract.
- Window views: the latest install of each mod by default, or every
  immutable event newest-first under More, without changing stored evidence.
- Optional shared archive: explicit opt-in and tagged with instance identity.
- Compatibility JSON: generated export, never the sole authoritative store.

## Release artifact boundary

The standalone binary archive contains runtime files below `plugins/`, audit
material below `fomod_logger_docs/`, and one root
`fomod_logger-manifest.json`. Its manifest records the clean repository commit
and the filename and SHA-256 of the paired corresponding-source archive.

The source archive contains every Git-tracked file without filtering research or
legacy evidence. It is an exact tracked snapshot plus a source manifest, not a
Git repository and not a copy of repository history. This preserves the release
source while allowing deferred code to remain absent from the standalone
runtime. Binary publication is not complete until that exact source archive is
accessible beside it.

The binding is one-way: the binary manifest identifies and hashes the source
archive. The source manifest records its repository commit and source-file
hashes but does not identify or hash the binary archive.

Cross-patch analysis is derived from the current event for each mod and is
partitioned by stable instance identity. It correlates option labels only after
Unicode compatibility normalization, case folding, and whitespace collapse;
it does not remove versions, platform markers, or punctuation. Repeated labels
inside one mod remain inspectable as positional occurrences but count as one
mod mention, so reinstalls and duplicate authored labels cannot inflate the
cross-mod result.

## Native event invariants

Each event contains:

- schema version and unique event ID;
- installation timestamp and result;
- mod name, version, and Nexus ID when available;
- installer/plugin version;
- ordered steps with stable positional IDs and visibility;
- ordered groups with stable positional IDs and group type;
- ordered options with stable positional IDs, display name, selected state, and
  effective step visibility.

Selection identity is the tuple `(step_id, group_id, option_id)`, never the
option label.

IDs are positions in `ModuleConfig.xml` **as the author wrote it**, counted
from 0: `step:N` is the Nth `installStep`, `group:N` the Nth `group` within
that step, `option:N` the Nth `plugin` within that group. They are assigned
before MO2 applies the FOMOD's `order` attribute (Ascending, the default, sorts
steps and options alphabetically for display), so a changed label or sort
order does not move an ID. The arrays in the event list steps and options in
display order, which may therefore differ from ID order. Adding, removing or
reordering entries in the XML does shift IDs.
Only authored FOMOD options are recorded: MO2's synthetic `None`
radio button is excluded because the false selected state of every authored
option already represents that choice without inventing a searchable option.

The ingestion validator enforces the public schema's closed-object, identifier,
timestamp, commit, integer, and group-type constraints before SQLite indexing.

## Developing

Developer tooling only; none of it is packaged (the binary packages list their files one by one, and the source
archive is just the tracked files).

- **Tests.** `python -m pytest` from the source root. The window tests borrow PyQt6 from the test MO2
  (`FOMOD_LOGGER_MO2_ROOT`, default `test-env\MO2-Skyrim-Logger`) and are skipped without it; the native test needs
  `FOMOD_LOGGER_NATIVE_TEST_EXE`. Put the test MO2 folder and its `dlls` on PATH so Qt's DLLs load.
- **Type check.** `python -m pip install -e .[dev]` (installs `mobase-stubs` 2.5.2, the stubs for MO2 2.5.2), install
  pyright (1.1.414 was used) and run `pyright` from the source root. `pyrightconfig.json` checks `python/` in basic
  mode and takes PyQt6 from the test MO2's bundled libs. The baseline is clean; a `# pyright: ignore[rule]` comment
  always says why (mostly PyQt6 stubs marking Qt accessors as possibly None, or missing `connect()` arguments).
- **pytest-qt does not work here.** MO2's bundled PyQt6 has no `QtTest` (`Qt6Test.dll` is not shipped), and pytest-qt
  refuses to start without it, so the tests keep their own `qt` fixture.
