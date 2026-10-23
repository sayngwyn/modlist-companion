"""Previous choices: the native matching rule against the Python one, and the wiring.

The native code cannot run without MO2's headers and Qt, so its behaviour is covered
by tests/native/previous_choices_test.cpp (built with
-DFOMOD_LOGGER_BUILD_NATIVE_TESTS=ON). Both read the same fixture and must agree
with the same expected option paths, which is how the native rule is kept identical
to python/fomod_logger_support/core/reviews.py::_option_paths.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from fomod_logger_support.core.reviews import _option_paths

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "native" / "fixtures"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_python_matching_rule_agrees_with_the_shared_native_expectation() -> None:
    event = json.loads(_read(FIXTURES / "duplicate_labels_event.json"))
    expected = json.loads(_read(FIXTURES / "duplicate_labels_paths.json"))["options"]

    _, by_path = _option_paths(event)
    assert list(by_path) == [tuple(row[:4]) for row in expected]

    options = {
        option["id"]: option
        for step in event["steps"] for group in step["groups"] for option in group["options"]
    }
    for row in expected:
        option = options[by_path[tuple(row[:4])]]
        assert option["selected"] is row[4]
        assert option["enabled"] is row[5]
    steps_visible = {
        option["id"]: step["visible"]
        for step in event["steps"] for group in step["groups"] for option in group["options"]
    }
    for row in expected:
        assert steps_visible[by_path[tuple(row[:4])]] is row[6]


def test_previous_choices_setting_is_declared_and_on_by_default() -> None:
    source = _read(ROOT / "src" / "installerfomod.cpp")
    declaration = source[source.index('"restore_previous_choices"'):]
    declaration = declaration[: declaration.index("return result;")]
    assert "QVariant(true)" in declaration  # the installer remembers by default
    assert 'pluginSetting(name(), "restore_previous_choices")' in source


def test_previous_choices_never_match_by_position_and_stay_out_of_the_log() -> None:
    lookup = _read(ROOT / "src" / "fomodpreviouschoices.cpp")
    dialog = _read(ROOT / "src" / "fomodinstallerdialog.cpp")

    # Options are matched by names and occurrence; ids are never compared.
    assert "option:" not in lookup  # no positional ids such as step:0/group:1/option:2
    assert "OccurrenceCounter" in dialog

    # The tag lives in the button text only; the log and the matching use the name.
    snapshot = dialog[dialog.index("QJsonArray FomodInstallerDialog::selectionSnapshot"):]
    snapshot = snapshot[: snapshot.index("void FomodInstallerDialog::highlightControl")]
    assert 'option.insert("name", optionName(choice))' in snapshot
    assert "choice->text()" not in snapshot


def test_previous_choices_lookup_depends_only_on_qt_core() -> None:
    for name in ("fomodpreviouschoices.h", "fomodpreviouschoices.cpp"):
        source = _read(ROOT / "src" / name)
        assert "uibase" not in source.lower()
        assert "imoinfo" not in source.lower()
        assert "#include <q" in source.lower()


def test_select_previous_button_is_hidden_until_something_matches() -> None:
    ui = _read(ROOT / "src" / "fomodinstallerdialog.ui")
    button = ui[ui.index('name="previousBtn"'):]
    button = button[: button.index("</widget>")]
    assert "<bool>false</bool>" in button.split('name="text"')[0]
    assert "Select previous" in button

    dialog = _read(ROOT / "src" / "fomodinstallerdialog.cpp")
    assert "ui->previousBtn->setVisible(true)" in dialog
    # No match returns before the button is shown.
    assert dialog.index("if (m_PreviousRefs.empty())") < dialog.index(
        "ui->previousBtn->setVisible(true)"
    )


@pytest.mark.skipif(
    not os.environ.get("FOMOD_LOGGER_NATIVE_TEST_EXE"),
    reason="set FOMOD_LOGGER_NATIVE_TEST_EXE to a built previous_choices_test.exe",
)
def test_native_previous_choices_executable_passes() -> None:
    exe = Path(os.environ["FOMOD_LOGGER_NATIVE_TEST_EXE"])
    result = subprocess.run([str(exe), str(FIXTURES)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_name_guesses_are_searched_in_tiers_and_back_then_next_reapplies() -> None:
    installer = _read(ROOT / "src" / "installerfomod.cpp")
    assert "query.otherModNames.append(variant)" in installer
    assert "query.modNames.append(dialog.infoName())" in installer
    assert "query.modNames.append(variant)" not in installer

    dialog = _read(ROOT / "src" / "fomodinstallerdialog.cpp")
    assert "m_PreviousAppliedPages" not in dialog
    assert "applyPreviousToCurrentPage();" in dialog


def test_user_visible_names_say_companion_but_the_settings_key_does_not_change() -> None:
    installer = _read(ROOT / "src" / "installerfomod.cpp")
    assert 'return "FOMOD Logger Native Installer";' in installer  # name(): settings key
    assert 'tr("Modlist Companion FOMOD Installer")' in installer
    for line in installer.splitlines():
        if "log::" in line or "tr(" in line:
            assert "FOMOD Logger" not in line, line


def test_imported_choices_are_only_looked_for_when_the_user_has_none_of_their_own() -> None:
    installer = _read(ROOT / "src" / "installerfomod.cpp")
    offer = installer[installer.index("void InstallerFomod::offerPreviousChoices"):]
    offer = offer[: offer.index("catch (const std::exception")]
    own = offer.index("findPreviousInstall(")
    imported = offer.index("findImportedInstall(")
    assert own < imported                               # the user's own install is tried first, and wins
    assert "dialog.setPreviousChoices(*imported, false)" in offer   # offered, never restored by itself
    assert "query.nexusId         = nexusId;" in offer or "query.nexusId = nexusId;" in offer
    assert "offerPreviousChoices(dialog, modName, modID);" in installer


def test_imported_choices_are_tagged_and_never_applied_without_the_button() -> None:
    dialog = _read(ROOT / "src" / "fomodinstallerdialog.cpp")
    body = dialog[dialog.index("bool FomodInstallerDialog::setPreviousChoices"):]
    body = body[: body.index("void FomodInstallerDialog::applyPreviousToCurrentPage")]
    assert "restoreNow = restoreNow && !previous.imported;" in body
    assert 'tr("  (imported)")' in body and 'tr("Select imported")' in body
    # Only options that exist in this installer, and are the user's to change, are ever tagged or applied.
    assert "!option->stepVisible || !option->enabled" in body
    # The group rules of the apply step: forced/blocked options are never touched and a
    # "pick at least one" group is never emptied.
    apply = dialog[dialog.index("void FomodInstallerDialog::applyPreviousToPage"):]
    assert "isEnabled()" in apply and "TYPE_SELECTATLEASTONE" in apply and "TYPE_SELECTALL" in apply


def test_the_imported_lookup_reads_only_the_imported_folder_and_stays_qt_core() -> None:
    lookup = _read(ROOT / "src" / "fomodpreviouschoices.cpp")
    body = lookup[lookup.index("std::optional<PreviousInstall> findImportedInstall"):]
    assert "kImportedFolder" in body and "kEventsFolder" not in body
    header = _read(ROOT / "src" / "fomodpreviouschoices.h")
    assert 'kImportedFolder{"fomod_logger/imported"}' in header and 'kEventsFolder{"fomod_logger/events"}' in header
    assert 'kImportedRecordFormat{"fomod-companion-imported-choices"}' in header
    assert "uibase" not in lookup.lower() and "imoinfo" not in lookup.lower()
