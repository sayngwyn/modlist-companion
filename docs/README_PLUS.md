<p align="center">
<img alt="Modlist Companion, a Mod Organizer 2 plugin" src="https://sayngwyn.com/img/page/modlist-companion/header-v1.png">
</p>

# Modlist Companion (for FOMOD Plus)

The ultimate companion for a happy modlist. See your recorded choices, check possible missed patches, and follow what changed in your mod list.

This is **Modlist Companion (for FOMOD Plus)**. FOMOD Plus installs the mod; Companion reads its saved picks and the download archive to rebuild the record in the background. It does not change FOMOD Plus, your mods or your profiles.

## How it works

- **Keep your choices.** Open Companion from MO2's tools menu to search recorded installer pages and options. Show all, picked or skipped options, or the full install history, and leave a reminder on an option.
- **Record every mod.** Plain archives and folders added to your list get a record too, including their files and plugins. **More > Scan existing mods...** fills in mods that are not recorded yet.
- **Follow your list history.** See installs, removals, updates, renames, changes to mod and plugin order, enabled states, profile switches, and programs started through MO2. Filter by profile or kind, search, or show changes since the last program ran.

<p align="center"><img alt="Companion's list history with mod changes grouped by day and program runs marked in the timeline; example data" src="https://sayngwyn.com/img/page/modlist-companion/list-history-v1.png"></p>

## What you get

- **Patch checks.** See possible missed patches, missing masters and installed patches that are switched off. New-patch alerts appear in MO2 when a newly installed mod makes an earlier skipped patch relevant.
- **An update helper.** See new, renamed and removed installer options, plus what you picked differently this time.
- **File lookup and comparison.** **More > Which option installed...?** connects a file or plugin to its installer option. **More > Compare choices...** compares installs, copies of a mod or two MO2 instances. Profile notes show where each mod is enabled.
- **Exports you can keep.** Save an install guide as Markdown or HTML, a choices file, or recorded installs and reminders as JSON. The List history page separately exports list changes as Markdown or plain text.

<p align="center"><img alt="Comparison of two copies of a mod, showing different texture resolution, normal-map and snow-cap choices; example data" src="https://sayngwyn.com/img/page/modlist-companion/compare-choices-v1.png"></p>


## Install

Requires **FOMOD Plus 1.20.0** and **MO2 2.5.2** on Windows, the versions tested together. Other FOMOD Plus saved-choice formats are not verified.

Install **one edition**, never both.

| Your FOMOD installer | Companion edition |
|---|---|
| MO2's standard installer | **Modlist Companion** — `modlist-companion-1.0.0.zip` |
| FOMOD Plus | **Modlist Companion (for FOMOD Plus)** — `modlist-companion-plus-1.0.0.zip` |

1. Close MO2 and back up any existing Companion data.
2. If the standard edition is installed, remove `plugins/fomod_logger.dll`, `plugins/fomod_logger.py`, `fomod_logger_docs/` and `fomod_logger-manifest.json` first. Both editions share `plugins/fomod_logger_support/`; the package you extract supplies that folder.
3. Upgrading from FOMOD Logger 1.x? Restore MO2's stock installer as described in the [upgrade guide](https://github.com/sayngwyn/modlist-companion/blob/main/docs/INSTALL_UPGRADE_ROLLBACK.md) first.
4. Extract `modlist-companion-plus-1.0.0.zip` into the MO2 folder containing `ModOrganizer.exe`, merging `plugins/`. **Do not install it as a mod.** Keep FOMOD Plus installed and enabled.
5. Start MO2. **FOMOD Logger Plus** should load without a problem notice. Open **Modlist Companion (for FOMOD Plus)** from the tools menu. Use **More > Scan existing mods...** to pick up earlier installs; new installs appear automatically.

Data lives in `<MO2 plugin data folder>/fomod_logger_plus/`, separate from the standard edition's data. Internal plugin names remain FOMOD Logger Plus for compatibility.

**Upgrade:** close MO2, back up the plugin files and data, then extract the new package. Move files listed only in the old manifest to a backup outside MO2. **Rollback:** restore the old plugin files and matching data backup together. **Uninstall:** close MO2 and move `plugins/fomod_logger_plus.py`, `plugins/fomod_logger_support/`, `fomod_logger_plus_docs/`, `fomod_logger_plus-manifest.json` and its data folder to a backup outside MO2. Keep the shared support folder if switching immediately to the standard edition. FOMOD Plus itself stays installed.


## Choose what it does

The window's **Theme** menu offers **sayngwyn**, **Light**, **Dark** and **Match MO2**, in that order. The first three use their own colours; Match MO2 follows the host theme.

In **Settings > Plugins**, you can turn on opening Companion when MO2 starts (off by default), or turn off new-patch notices. Reminders and the picked/skipped filters are in Companion's window.


## Good to know

- **Keep the download archives.** FOMOD Plus saves option names; the archive supplies the files and plugins used by patch checks, installer-change checks and file lookup. Without a readable archive, Companion keeps the saved names and explains the reduced record.
- **No import or replay in this edition.** There is no **Import choices file** command here. Companion cannot drive FOMOD Plus's dialog or tick its options. You can export choices, but importing them is a standard-edition feature.
- **Earlier installs can be scanned.** Readable picks saved by FOMOD Plus can be recovered. Mods installed another way still get a plain mod record, but their FOMOD choices cannot be reconstructed.
- **Checks are guidance.** A possible missed patch is a reason to look at the installer, not a promise that the patch is needed. Older records may need a reinstall to include patch information.
- **History has limits.** Changes made while MO2 was closed are marked "found at MO2 start"; that is the discovery time. Program history covers programs launched through MO2, keeps their name and path but never their arguments, and retains the newest 50,000 entries per instance.
- **Your data stays separate.** Packages contain plugin files and documents, never your recorded history. Back up the plugin data before upgrading; a rollback may also need the matching data backup.
- **Compatibility.** Tested with Mod Organizer 2.5.2 on Windows. The plugin reads the current game from MO2, but other MO2 versions are not verified.


<details><summary>Build it yourself</summary>

The matching `-source.zip` is published beside each binary package. The full edition's source includes the pinned uibase source and licence evidence; the Plus source contains the tracked project tree. Package manifests identify the build commit and matching source archive by SHA-256.

See the [build contract](https://github.com/sayngwyn/modlist-companion/blob/main/docs/BUILD_CONTRACT.md) for Python 3.12, Qt 6.7.1, MSVC, the pinned dependencies and exact-commit packaging. Run `python -m pytest -q` and `pyright` in a development checkout. Viewer tests additionally need MO2's bundled PyQt6; native tests need a built `previous_choices_test.exe`. Keep test windows off the desktop with the supplied gallery helper.

The [changelog](https://github.com/sayngwyn/modlist-companion/blob/main/CHANGELOG.md) lists the release changes. Source archives are for development; install a binary package into MO2.

</details>


## Report a problem

Find me on [Discord](https://discord.com/invite/sayngwyn). Include your MO2 version, Companion edition, the steps you took and what you expected to happen. Remove personal paths from anything you share.

## More by sayngwyn

- **[Creations Browser](https://www.nexusmods.com/site/mods/2483)**: Browse Creations, download the free ones. ([source](https://github.com/sayngwyn/creations-browser))
- **[Hide Buttons](https://www.nexusmods.com/site/mods/1619)**: Hide the MO2 buttons you never use. ([source](https://github.com/sayngwyn/hide-buttons))
- **[Sayngwyn Theme](https://www.nexusmods.com/site/mods/2487)**: My personal dark theme for MO2. ([source](https://github.com/sayngwyn/sayngwyn-theme))

All my mods: [Nexus](https://www.nexusmods.com/profile/sayngwyn/mods) · [Website](https://sayngwyn.com/projects)

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://sayngwyn.com/img/page/divider-v4.png">
  <img alt="Antler divider" src="https://sayngwyn.com/img/page/divider-light-v4.png">
</picture></p>

## Credits

- **Mod Organizer 2 team** (Tannin, thosrtanner and contributors) for MO2 and the FOMOD installer this plugin builds on.
- **aglowinthefield** for FOMOD Plus, whose saved choices the Plus edition reads.
- **Qt and PyQt** (The Qt Company, Riverbank Computing) for the window.
- **Python and SQLite** for storing your records.

Source code: [GitHub](https://github.com/sayngwyn/modlist-companion).

Made with AI. Claude contributed heavily under my direction: I decided what it does and checked it, and the AI wrote most of the code, its tests and these docs.

## Licence

The combined release uses GNU GPL version 3 only. Upstream MO2 files retain their original GPL version 3-or-later grant. See [LICENSE](https://github.com/sayngwyn/modlist-companion/blob/main/LICENSE), [THIRD_PARTY_NOTICES.md](https://github.com/sayngwyn/modlist-companion/blob/main/THIRD_PARTY_NOTICES.md) and [MODIFICATIONS.md](https://github.com/sayngwyn/modlist-companion/blob/main/MODIFICATIONS.md).

The sayngwyn skull mark is © sayngwyn, all rights reserved. It is not covered by this project's GPL-3.0 licence and is included only for this program's window. Forks must remove or replace it. The Sayngwyn Serif font is under the SIL Open Font License 1.1 (`OFL.txt` beside it).

FOMOD Plus is a separate project by its own author. This edition reads its saved data and contains none of its code.
