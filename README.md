<p align="center">
<img alt="Modlist Companion, a Mod Organizer 2 plugin" src="https://sayngwyn.com/img/page/modlist-companion/header-v1.png">
</p>

# Modlist Companion

Remembers every FOMOD choice you made in MO2. See your recorded choices, check possible missed patches, and follow what changed in your mod list.

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

This is the **standard edition**. It includes a FOMOD installer that records choices during installation. FOMOD Plus takes priority over it, so disable FOMOD Plus or choose the Plus edition instead.

Install **one edition**, never both.

| Your FOMOD installer | Companion edition |
|---|---|
| MO2's standard installer | **Modlist Companion** — `modlist-companion-1.0.0.zip` |
| FOMOD Plus | **Modlist Companion (for FOMOD Plus)** — `modlist-companion-plus-1.0.0.zip` |

1. Close MO2 and back up any existing Companion data.
2. Upgrading from **FOMOD Logger 1.x**? Follow the [upgrade guide](https://github.com/sayngwyn/modlist-companion/blob/main/docs/INSTALL_UPGRADE_ROLLBACK.md) first, including restoring MO2's stock `installer_fomod.dll`.
3. Switching from Companion's Plus edition? Remove `plugins/fomod_logger_plus.py`, `fomod_logger_plus_docs/` and `fomod_logger_plus-manifest.json` first. Both editions share `plugins/fomod_logger_support/`; the package you extract supplies that folder.
4. Extract `modlist-companion-1.0.0.zip` into the MO2 folder containing `ModOrganizer.exe`, merging `plugins/`. **Do not install it as a mod.** MO2's stock `installer_fomod.dll` is left alone.
5. Start MO2. **FOMOD Logger** and **FOMOD Logger Native Installer** should load without a problem notice. Open **Modlist Companion** from the tools menu and install a FOMOD mod as usual.

The plugin keeps its internal FOMOD Logger names for compatibility. Its data is in `<MO2 plugin data folder>/fomod_logger/`; the Plus edition uses a separate folder.

Full upgrade, rollback and uninstall instructions are in the [install guide](https://github.com/sayngwyn/modlist-companion/blob/main/docs/INSTALL_UPGRADE_ROLLBACK.md), also included at `fomod_logger_docs/docs/INSTALL_UPGRADE_ROLLBACK.md` in the package.


## Choose what it does

The window's **Theme** menu offers **sayngwyn**, **Light**, **Dark** and **Match MO2**, in that order. The first three use their own colours; Match MO2 follows the host theme.

In **Settings > Plugins**, you can turn on opening Companion when MO2 starts (off by default), or turn off new-patch notices. Reminders and the picked/skipped filters are in Companion's window.

The standard edition also restores previous picks on reinstall by default (`restore_previous_choices`); **Select previous** remains available when that setting is off. **More > Import choices file** offers imported picks through **Select imported** in the installer, so you can review them before accepting. **Remove imported choices** undoes the import.


## Good to know

- **Recording starts when Companion is installed.** It cannot recover earlier FOMOD choices, except for its one-time import of a 1.x `fomod_data.json`. Scanning older mods records their files as plain installs, not their original installer choices.
- **Use the matching edition.** The standard edition does not capture choices made by FOMOD Plus. MO2 reports the installer conflict if both are enabled.
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

Find me on [Discord](https://discord.gg/QZHymBYMxP). Include your MO2 version, Companion edition, the steps you took and what you expected to happen. Remove personal paths from anything you share.

## More by sayngwyn

[![All my mods on Nexus, every game](https://sayngwyn.com/img/mods/button-all-mods-v1.png)](https://www.nexusmods.com/profile/sayngwyn/mods)

Also on [my website](https://sayngwyn.com/projects).

Like the look of this window? Get [Sayngwyn Theme for Mod Organizer 2](https://www.nexusmods.com/site/mods/2487).

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://sayngwyn.com/img/page/divider-v4.png">
  <img alt="Antler divider" src="https://sayngwyn.com/img/page/divider-light-v4.png">
</picture></p>

Source code: [GitHub](https://github.com/sayngwyn/modlist-companion).

Made with AI. Claude contributed heavily under my direction: I decided what it does and checked it, and the AI wrote most of the code, its tests and these docs.

## Licence

The combined release uses GNU GPL version 3 only. Upstream MO2 files retain their original GPL version 3-or-later grant. See [LICENSE](https://github.com/sayngwyn/modlist-companion/blob/main/LICENSE), [THIRD_PARTY_NOTICES.md](https://github.com/sayngwyn/modlist-companion/blob/main/THIRD_PARTY_NOTICES.md) and [MODIFICATIONS.md](https://github.com/sayngwyn/modlist-companion/blob/main/MODIFICATIONS.md). Thanks to the Mod Organizer 2 contributors, including Sebastian Herbord (Tannin) and thosrtanner.

The sayngwyn skull mark is © sayngwyn, all rights reserved. It is not covered by this project's GPL-3.0 licence and is included only for this program's window. Forks must remove or replace it. The Sayngwyn Serif font is under the SIL Open Font License 1.1 (`OFL.txt` beside it).
