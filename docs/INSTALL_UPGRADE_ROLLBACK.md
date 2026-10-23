# Install, upgrade, and rollback

This guide applies to the standard edition of Modlist Companion (internally
"FOMOD Logger") for Mod Organizer 2 2.5.2. People who install FOMODs with the
FOMOD Plus plugin use the other edition, "Modlist Companion (for FOMOD Plus)",
whose install, upgrade and rollback steps are in its own README. Install one
edition, never both: they share the `plugins/fomod_logger_support/` folder. To
switch, close MO2, move the other edition's plugin file, documents folder and
manifest out of MO2, then extract the new package. Each edition keeps its data
in its own folder (`fomod_logger/` and `fomod_logger_plus/`).

## Package boundary

The binary archive is laid out relative to the Mod Organizer 2 installation
directory. Its runtime payload owns only these paths:

- `plugins/fomod_logger.dll`;
- `plugins/fomod_logger.py`;
- `plugins/fomod_logger_support/`.

Its non-runtime audit material owns only:

- `fomod_logger_docs/`;
- `fomod_logger-manifest.json` at archive root.

There are no generic root `README.md`, `LICENSE`, `docs/`, or `schemas/`
entries that could collide with another MO2 package. Persistent Logger data is
created at runtime below MO2's `pluginDataPath()/fomod_logger/`; it is not
included in an installation archive.

FOMOD Logger must not rename, replace, restore, or delete MO2's stock
`installer_fomod.dll`.

The `fomod_logger_docs/` namespace contains the README, build and provenance
records, public event schema, licenses, dated modification notice, and this
guide. Those documents are not executable, but they and the root manifest
should be retained with the installed release for audit and recovery.

## Before changing an installation

1. Close Mod Organizer 2 and confirm no process is using that MO2 installation.
2. Record the exact FOMOD Logger package version and archive SHA-256.
3. Copy the Logger-owned runtime paths, `fomod_logger_docs/`, and
   `fomod_logger-manifest.json` to a dated backup directory.
4. Copy `pluginDataPath()/fomod_logger/` to the same backup. In a portable MO2
   installation, `pluginDataPath()` is commonly below `plugins/data`, but use
   the path for the actual MO2 instance rather than assuming that layout.
5. When upgrading from FOMOD Logger 1.x, also preserve any `fomod_data.json` at
   the MO2 base directory or directly below `pluginDataPath()`.

Do not use a broad MO2-directory restore as a Logger rollback. It can overwrite
unrelated mods, profiles, settings, or other plugins.

## Fresh installation

1. Verify the binary archive hash against the release record.
2. Verify that `fomod_logger-manifest.json` names and hashes the exact
   corresponding-source archive published for that release, and that both
   manifests identify the same repository commit.
3. Inspect the archive and confirm that it contains only the Logger-owned paths
   above, then extract the complete archive relative to the intended MO2
   installation.
4. Start MO2. Confirm that both **FOMOD Logger** and **FOMOD Logger Native
   Installer** load without a plugin diagnostic.
5. Open Modlist Companion from MO2's tools menu and confirm the window opens.

No game or script extender launch is required for these checks.

## Using the window and the data it keeps

Open **Modlist Companion** from MO2's tools menu whenever needed. It opens as a
window beside MO2 and can stay open while you keep using MO2. The MO2 setting
"Open the Modlist Companion window when MO2 starts" (internal name
`open_window_on_start`) optionally opens it at startup; it defaults to off.
`README.md` lists what the window shows and does.

Recorded selections do not prove installed files, enabled mods, active-profile
compatibility or a working patch; patch warnings are prompts to look, not proof
that something is missing. Reminders ("Remind me") keep their original
installation and option IDs across reinstalls and are never resolved or dismissed
automatically.

## Database versions and exports

The database is upgraded transactionally from a new empty database or recognised versions 5 to 9 to
version 10 the first time a build that knows it opens it. Version 10 only adds
tables (the list's change history and the last snapshot of each profile), so
nothing recorded before is touched. Original event JSON is
unchanged. Legacy FOMOD Logger imports contain installation evidence only and create no
invented reminders. Preserve a complete closed-MO2 data backup before upgrading.

A build supporting an earlier schema cannot read a newer database: keep the newer database and its
export separately and restore the older version's pre-upgrade backup when
rolling back. **More > Export history...** writes a portable JSON evidence file
of everything recorded, including hidden or removed overlays and reminders. It
is not an automatic restore format. **Export choices file...** and **Export
install guide...** are separate exports; a choices file can be imported by the
standard edition on another PC or MO2 instance. Re-indexing the immutable event
files alone cannot recover reminders.

Mods that were not installed through an installer ("plain" records: version,
archive, plugins, and the list of files) live only in the SQLite database: the
standard edition's native installer writes event files for its FOMOD installs,
but nothing writes a file for a plain record, so a database that is lost and
rebuilt from the event files loses them. **More > Scan existing mods...** records
every mod that has no record again (a mod's earlier installs and the times it was
installed are not recovered). The file lists of a mod's older plain records are
dropped to save room: only its two newest installs keep theirs, and the record
itself (version, archive, plugins, file count) stays in the history. The
FOMOD Plus edition's records are all rebuilt from FOMOD Plus's saved choices and
the download archives, but its plain records are in the same position.

The list's change history (the **List history** page) also lives only in the
SQLite database. Its entries are not event files and are not part of **More >
Export history...** (the JSON evidence file); the page's own **Export
history...** writes them as Markdown or text. Companion keeps the newest 50,000
entries for each MO2 instance and drops the oldest. Programs are recorded only
when MO2 starts them, and only the program's path, never its arguments.

## Upgrade from FOMOD Logger 1.x

Modlist Companion looks once for the legacy `fomod_data.json` in the MO2 base
directory and directly below `pluginDataPath()`. Successful entries are copied
into the Companion database; the legacy file is not deleted or rewritten.

1. Complete the backups above.
2. Restore MO2's own FOMOD installer. 1.x copied its build over
   `plugins/installer_fomod.dll` and kept the stock file as
   `plugins/installer_fomod_original.dll`. With MO2 closed, rename
   `installer_fomod_original.dll` to `installer_fomod.dll` (replacing the 1.x
   copy) and delete `plugins/installer_fomod_logger.dll`. If the backup is
   missing, take `installer_fomod.dll` from your Mod Organizer download. Companion
   shows an MO2 problem until these files are gone.
3. Put the legacy `fomod_data.json` in one of the searched locations before
   the first Companion start.
4. Install the complete Companion-owned file set. Do not mix the 1.x Python
   plugin with the Companion native plugin or support directory.
5. Start MO2 once and inspect Logger's diagnostics and history before performing
   another FOMOD installation.
6. Confirm that expected legacy records are visible, then preserve a new backup
   of `pluginDataPath()/fomod_logger/`.

After a migration attempt, Logger writes
a marker in `pluginDataPath()/fomod_logger/`: `.legacy-migration-v1-<instance id>`
for the file in that instance's base directory, and `.legacy-migration-v1` for
the file directly below `pluginDataPath()`. That folder is shared by every
instance of one MO2 installation, so the shared file is migrated once, into
whichever instance starts first; keep each instance's 1.x file in its own base
directory instead. If a legacy file was not in place when its marker was
created, move it there and delete that marker: migrated records keep stable
IDs, so migrating the same file again into the same instance adds nothing.

## Upgrade between Modlist Companion builds

1. Complete the backups above.
2. Replace the complete Logger-owned runtime and audit file set with the new
   package. The package does not contain or replace the Logger data directory.
3. Compare the installed Logger-owned files with the new package manifest.
   Files absent from the new manifest are not part of the new release; quarantine
   a stale file outside MO2 instead of deleting it until rollback is no longer
   needed.
4. Start MO2 and verify diagnostics, current history, immutable history, search,
   and one harmless isolated FOMOD fixture before relying on the upgrade.

Do not copy an older SQLite database over a newer live database while MO2 is
running.

## Rollback

1. Close MO2 and preserve a separate copy of the current
   `pluginDataPath()/fomod_logger/` directory. It may contain events created
   after the backup you are restoring.
2. Quarantine the current Logger-owned runtime files, audit directory, and root
   manifest outside MO2.
3. Restore the exact backed-up Logger-owned runtime and audit files as one set.
4. Restore the matching Logger data backup only when the target version requires
   it. Keep the newer data copy until its records have been exported or otherwise
   reconciled.
5. Start MO2 and verify Logger diagnostics and history.

FOMOD Logger 1.x does not read the Companion SQLite database or native event spool.
Rolling back to 1.x therefore does not make Companion-only events visible in the 1.x
interface. The preserved legacy `fomod_data.json` remains the 1.x recovery
source.

## Safe uninstall

With MO2 closed, move the Logger-owned runtime paths, `fomod_logger_docs/`, root
manifest, and data directory to a dated backup outside MO2. Do not remove the
stock `installer_fomod.dll`, the shared MO2 Python runtime, profiles, mods, or
another plugin's data.

## Release-source requirement

Each published binary must be accompanied by the deterministic corresponding
source archive named and hashed in `fomod_logger-manifest.json`. This is a
one-way binary-to-source binding. The source manifest records its repository
commit and source-file hashes; it does not hash the binary archive. Until the
exact source archive has a real publication location beside the binary, the
release gate remains open.
