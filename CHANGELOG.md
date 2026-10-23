# Changelog

## 1.0.0

First release of **Modlist Companion**, in two editions: the standard edition
for MO2's FOMOD installer and the companion for FOMOD Plus users. Install one.

Moving from FOMOD Logger 1.0.5 or the old FOMOD Logger and FOMOD Plus 1.0.0
release? The internal plugin names, data folders and database are unchanged,
so your data and settings carry over; follow the upgrade guide for legacy
files. The old FOMOD Logger page is retired in favour of this new mod page.

### New in both editions

- A window inside MO2 replaces the old FOMOD Logger browser page. Open it from the tools menu;
  MO2 stays usable and the window updates as installs happen. It opens in the
  "sayngwyn" colours (near-black with red accents); a Theme menu also offers
  Light, Dark and Match MO2, and your choice is remembered.
- Search across every recorded choice; show all, picked or skipped options;
  latest install of each mod or the full history; reminders on options.
- Possible missed patches: skipped options that patch mods you now use.
- Missing masters: plugins that cannot load because a master is not installed.
- Present but disabled: patches and picked options whose plugin (or the mod it is
  for) is switched off.
- Update helper: a "Changed installers" card when an updated mod's installer
  offers new, renamed or removed options, with new patches called out.
- Pick markers: "newly picked" and "was picked" against the previous install.
- New-patch alerts in MO2's problem list when a mod you install is what a skipped
  patch was made for (can be switched off in the plugin settings).
- Every mod gets a record, not only the ones installed through a FOMOD installer:
  version, Nexus ID, archive, files and plugins (with their masters) of a plain
  zip or a dropped folder, shown with a quiet "no installer options" marker. The
  search, the file lookup, compare, profiles and the missing-master check cover
  them. New installs are recorded in the background a moment after MO2 announces
  them; **More > Scan existing mods...** records the ones already there, with
  progress and Cancel.
- List history: a timeline of what changed in your list (mods installed, updated,
  removed, switched on or off or moved; plugins switched or moved; profile
  switches; programs started through MO2, with when they ended), for each MO2
  instance and profile, in a new **List history** page of the window. Filter by
  kind, search, pick a profile, "Since the last program ran" in one click, and
  export it as Markdown or text. Changes made while Companion was not watching
  (MO2 closed, or edits by other tools) are found the next time MO2 starts and
  marked. Programs are recorded only when MO2 starts them (never with their
  arguments); the newest 50,000 entries per MO2 instance are kept and the oldest
  dropped. Database version 10 (additive; 5 to 9 are upgraded in place).
- Export an install guide (Markdown or HTML), a choices file, or the full history.
- "Which option installed...?": which installer option puts a file or plugin in a
  mod, and which option wins when several write the same path.
- Compare choices between two installs, two copies of a mod, or two MO2
  instances; see which profiles have a mod enabled.
- A new history database (SQLite; schema 10) with immutable event files. Legacy
  `fomod_data.json` is imported once. Builds supporting an earlier schema cannot read this database: keep a backup before upgrading.
- Packages have their own documents folder and a manifest listing every file
  with its SHA-256 and the matching source archive.

### Standard edition only

- Our native installer (replaces MO2's FOMOD installer for FOMOD archives; MO2's
  own file is never changed) records every choice as you make it.
- The installer tags last time's picks "(previous)" and selects them again by
  default (setting "restore_previous_choices"; "Select previous" is always there).
- Import a choices file from another PC or MO2 instance: the installer offers
  those picks ("Select imported"); nothing is ticked without you.
- If FOMOD Plus is enabled it takes every FOMOD install first, so this edition
  records no FOMOD choices and MO2's problem list says so.

### FOMOD Plus edition only

- No installer of its own. After each FOMOD Plus install it reads the options
  FOMOD Plus saved in the mod's `meta.ini` and rebuilds the full record from the
  mod's download archive in the background.
- "Scan existing mods..." rebuilds the record of mods FOMOD Plus installed before
  Companion, and records the other mods as plain installs.
- No import or replay of choices files. The patch features need the download
  archive; without it only the saved option names are kept.
- Tested with FOMOD Plus 1.20.0.

### Notes

- Tested with Mod Organizer 2 2.5.2 only.
- Licence: GNU GPL version 3 only for the combined release; the upstream MO2 files
  keep their GPL version 3 or later grant. MO2 credits are in
  `THIRD_PARTY_NOTICES.md`.
