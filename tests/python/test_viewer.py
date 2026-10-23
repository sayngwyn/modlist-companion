"""The FOMOD Logger window: its data model, and the Qt window itself.

The window tests need PyQt6. MO2 bundles it (built for Python 3.12), so they
borrow MO2's copy: set FOMOD_LOGGER_MO2_ROOT to a portable MO2 folder, or
keep the project's test MO2 beside the source. Without one they are skipped.
No window is ever shown on screen.
"""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from fomod_logger_support.core.storage import EventStore
from fomod_logger_support.viewer import model

ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MO2 = ROOT.parents[1] / "MO2-Skyrim-Logger"


def _store(tmp_path: Path, *events: dict) -> Path:
    database = tmp_path / "logger.sqlite3"
    with EventStore(database) as store:
        for event in events:
            store.ingest(event)
    return database


def _reinstall(event: dict, event_id: str, completed_at: str) -> dict:
    newer = deepcopy(event)
    newer["event_id"] = event_id
    newer["completed_at"] = completed_at
    newer["source"]["reinstallation"] = True
    return newer


# ---------- model ----------

def test_model_filters_sorts_and_summarises(tmp_path, installation_event):
    other = deepcopy(installation_event)
    other.update(event_id="22222222-2222-4222-8222-222222222222", completed_at="2026-08-27T12:00:00+00:00")
    other["mod"]["name"] = "Another Mod"
    database = _store(tmp_path, installation_event, other)

    snapshot = model.load(database, model.ViewState())
    assert [e["mod"]["name"] for e in snapshot.events] == ["Another Mod", "Fixture Mod"]  # newest first
    assert [e["mod"]["name"] for e in model.load(database, model.ViewState(sort="name")).events] == [
        "Another Mod", "Fixture Mod"]
    assert [e["mod"]["name"] for e in model.load(database, model.ViewState(sort="oldest")).events] == [
        "Fixture Mod", "Another Mod"]
    assert model.summary(snapshot, model.ViewState()) == ("2 mods logged", [], "")

    searched = model.load(database, model.ViewState(query="fixture"))
    assert [e["mod"]["name"] for e in searched.events] == ["Fixture Mod"]
    assert model.match_count(searched.events[0], "fixture") == 2  # the mod name matches every option
    assert model.summary(searched, model.ViewState(query="fixture"))[0] == "1 mod match"


def test_model_maps_a_reminder_to_the_newest_install(tmp_path, installation_event):
    newer = _reinstall(installation_event, "33333333-3333-4333-8333-333333333333", "2026-09-01T12:00:00+00:00")
    database = _store(tmp_path, installation_event, newer)
    option = "step:0/group:0/option:0"
    with EventStore(database) as store:
        store.save_review({"event_id": installation_event["event_id"], "option_id": option,
                           "reason": "", "state": "pending"})
    snapshot = model.load(database, model.ViewState())
    assert model.pending_review(snapshot.reviews, newer["event_id"], option) is not None
    assert model.pending_review(snapshot.reviews, newer["event_id"], "step:0/group:1/option:0") is None
    assert model.summary(snapshot, model.ViewState())[1] == ["1 to revisit"]


def test_model_reports_a_broken_database_instead_of_raising(tmp_path):
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not a database")
    assert model.load(broken, model.ViewState()).error


# ---------- the window ----------

@pytest.fixture(scope="module")
def qt():
    mo2 = Path(os.environ.get("FOMOD_LOGGER_MO2_ROOT", _DEFAULT_MO2))
    libs = mo2 / "plugins" / "plugin_python" / "libs"
    if sys.version_info[:2] != (3, 12) or not (libs / "PyQt6" / "QtWidgets.pyd").is_file():
        pytest.skip("needs MO2's bundled PyQt6 (set FOMOD_LOGGER_MO2_ROOT)")
    os.add_dll_directory(str(mo2))
    os.add_dll_directory(str(mo2 / "dlls"))
    os.environ.setdefault("QT_PLUGIN_PATH", str(mo2))
    sys.path.append(str(libs))  # last, so only PyQt6 comes from MO2
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["fomod-logger-tests"])
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # tests/, for the gallery's Qt helper
    from gallery import qtenv

    qtenv.keep_off_screen(app)          # nothing the tests open (or the code shows) reaches the desktop
    from fomod_logger_support.viewer import window

    yield window
    sys.path.remove(str(libs))


def _window(qt, database, **kwargs):
    window = qt.LoggerWindow(database, **kwargs)
    window.refresh()
    return window


def _open_all(window) -> None:
    if window.toggle_all.text() == "Expand all":
        window._toggle_all()


def _texts(window) -> list[str]:
    _open_all(window)
    return [item.text(0) for item in window._items()]


def test_window_lists_installs_and_options(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    texts = _texts(window)
    assert "Fixture Mod" in texts
    assert "✓  Shared label" in texts and "○  Shared label" in texts
    assert window.summary_label.text().startswith("1 mod logged")
    window.state.choices = "picked"
    window._render()
    assert "○  Shared label" not in _texts(window)
    window.deleteLater()


def test_window_shows_missed_patches_and_reminders_and_saves_them(qt, tmp_path, installation_event):
    event = deepcopy(installation_event)
    skipped = event["steps"][0]["groups"][1]["options"][0]
    skipped.update(name="Village patch", plugins=["GP - Village.esp"], masters=["Village.esp"], detects=[])
    load_order = {"instance_id": event["instance"]["id"], "active_plugins": ["Village.esp"],
                  "base_plugins": [], "mods": []}
    changes = []
    database = _store(tmp_path, event)
    window = _window(qt, database, load_order=lambda: load_order, on_changed=lambda: changes.append(1))
    assert window.missed_box.isVisibleTo(window)
    assert len(window.snapshot.missed) == 1
    assert window.missed_chip.text() == "1 possible missed patch"

    window.apply_reminder(event["event_id"], skipped["id"], "save", "check later", "pending")
    assert changes == [1]
    assert window.snapshot.missed[0]["reminder"] is True  # stays listed until settled
    _open_all(window)
    statuses = [item.text(1) for item in window._items()]
    assert any("Reminder set" in s and "possible missed patch" in s for s in statuses)

    window.apply_reminder(event["event_id"], skipped["id"], "save", "", "dismissed")
    assert window.snapshot.missed == []
    window.apply_reminder(event["event_id"], skipped["id"], "remove")
    assert window.snapshot.reviews == [] and len(window.snapshot.missed) == 1
    window.deleteLater()


def test_window_navigates_to_an_option_hidden_by_the_search(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    window.state.query = "First group"
    window.refresh()
    # The search shows only the matching group; navigating reveals the other one.
    assert window.navigate(installation_event["event_id"], "step:0/group:1/option:0") is True
    assert window.tree.currentItem().text(0) == "○  Shared label"
    window.deleteLater()


def test_window_shows_mod_authored_text_as_plain_text(qt, tmp_path, installation_event):
    event = deepcopy(installation_event)
    event["mod"]["name"] = "<b>Bold</b> & Co"
    window = _window(qt, _store(tmp_path, event))
    window.apply_reminder(event["event_id"], "", "save", "<i>note</i>", "pending")
    labels = [label for label in window.findChildren(qt.QLabel) if label.text() == "<b>Bold</b> & Co"]
    assert labels and all(label.textFormat() == qt.Qt.TextFormat.PlainText for label in labels)
    window.deleteLater()


def test_window_remembers_its_size(qt, tmp_path, installation_event):
    geometry = tmp_path / "window.json"
    database = _store(tmp_path, installation_event)
    first = _window(qt, database, geometry_file=geometry)
    first.resize(900, 600)
    first.close()
    assert geometry.is_file()
    second = _window(qt, database, geometry_file=geometry)
    assert (second.width(), second.height()) == (900, 600)
    for window in (first, second):
        window.deleteLater()


def test_choices_are_built_only_when_an_install_is_opened(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    item = window.tree.topLevelItem(0)
    assert [item.child(i).text(0) for i in range(item.childCount())] == ["…"]
    item.setExpanded(True)  # as a user click does
    assert "Main" in [item.child(i).text(0) for i in range(item.childCount())]
    window.deleteLater()


def test_load_order_changes_redraw_only_the_side_panel(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    first = window.tree.topLevelItem(0)
    window.refresh()  # nothing the list shows changed
    assert window.tree.topLevelItem(0) is first
    window.state.choices = "picked"
    window._render()
    assert window.tree.topLevelItem(0) is not first
    window.deleteLater()


def test_refresh_waits_while_a_dialog_is_open(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    first = window.tree.topLevelItem(0)
    with window._modal_scope():
        window.state.choices = "skipped"
        window.refresh()
        assert window.tree.topLevelItem(0) is first  # nothing deleted under an open dialog
        assert window._refresh_pending is True
    assert window._refresh_pending is False  # caught up once it closed (queued)
    window.deleteLater()


def test_show_falls_back_to_the_install_when_the_option_is_filtered_out(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    window.state.choices = "picked"
    window._render()
    skipped = "step:0/group:1/option:0"
    assert window.navigate(installation_event["event_id"], skipped) is True
    assert window.tree.currentItem().text(0) == "Fixture Mod"
    assert window.navigate("00000000-0000-4000-8000-000000000000") is False  # not listed
    window.deleteLater()


def test_search_offers_show_all_options(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    window.state.query = "First group"
    window._expanded.add(installation_event["event_id"])
    window.refresh()
    assert "Show all options in this installer" in [item.text(0) for item in window._items()]
    window._show_all_options(installation_event["event_id"])
    assert "Show all options in this installer" not in [item.text(0) for item in window._items()]
    assert "○  Shared label" in [item.text(0) for item in window._items()]
    window.deleteLater()


def test_the_window_switches_between_light_dark_and_mo2s_theme_and_remembers(qt, tmp_path, installation_event):
    settings = tmp_path / "window.json"
    database = _store(tmp_path, installation_event)
    window = _window(qt, database, geometry_file=settings)
    assert window._theme_mode == "sayngwyn"           # the default when nothing is saved
    window.set_theme_mode("dark")
    assert window._tokens.dark is True
    assert window.palette().color(qt.QPalette.ColorRole.Window).lightness() < 128
    assert window.theme_button.text().startswith("Theme: Dark")
    window.set_theme_mode("light")
    assert window._tokens.dark is False
    window.close()
    reopened = _window(qt, database, geometry_file=settings)
    assert reopened._theme_mode == "light" and reopened._tokens.dark is False
    reopened.set_theme_mode("auto")
    assert reopened.palette().color(qt.QPalette.ColorRole.Window) == reopened._mo2_palette.color(qt.QPalette.ColorRole.Window)
    for window_ in (window, reopened):
        window_.deleteLater()


def test_the_default_theme_is_sayngwyn_and_a_saved_choice_is_respected(qt, tmp_path, installation_event):
    database = _store(tmp_path, installation_event)
    # No file at all, a file that only holds the size, and damaged or unknown values: all get sayngwyn.
    for content in (None, '{"geometry": ""}', "not json", '{"theme": "neon"}', '{"theme": 3}', "[]"):
        settings = tmp_path / "window.json"
        settings.unlink(missing_ok=True)
        if content is not None:
            settings.write_text(content, encoding="utf-8")
        window = _window(qt, database, geometry_file=settings)
        assert window._theme_mode == "sayngwyn", content
        assert window.theme_button.text().startswith("Theme: sayngwyn")
        window.deleteLater()
    # Looking at the default doesn't save it: only choosing a theme does, so a later change of default still reaches
    # everyone who never chose.
    window = _window(qt, database, geometry_file=settings)
    window.close()
    assert "theme" not in json.loads(settings.read_text(encoding="utf-8"))
    window.deleteLater()
    # Every mode a user can have saved comes back as it was, including the one that used to be the default.
    for mode in ("auto", "light", "dark", "sayngwyn"):
        settings.write_text(json.dumps({"theme": mode, "geometry": ""}), encoding="utf-8")
        window = _window(qt, database, geometry_file=settings)
        assert window._theme_mode == mode
        window.close()
        assert json.loads(settings.read_text(encoding="utf-8"))["theme"] == mode   # and closing keeps it
        window.deleteLater()


def test_the_theme_menu_offers_sayngwyn_light_dark_then_match_mo2(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    actions = window.theme_button.menu().actions()
    assert [action.text() for action in actions] == ["sayngwyn", "Light", "Dark", "Match MO2"]
    assert all("&" not in action.text() for action in actions)       # an "&" would turn the next letter into a shortcut
    assert [action.isChecked() for action in actions] == [True, False, False, False]
    actions[2].trigger()
    assert window._theme_mode == "dark" and [action.isChecked() for action in actions] == [False, False, True, False]
    actions[3].trigger()
    assert window._theme_mode == "auto" and [action.isChecked() for action in actions] == [False, False, False, True]
    actions[0].trigger()
    assert window._theme_mode == "sayngwyn" and actions[0].isChecked()
    window.deleteLater()


def test_the_sayngwyn_theme_uses_the_sheet_colours_and_every_text_colour_reads(qt, tmp_path, installation_event):
    from PyQt6.QtGui import QColor

    from fomod_logger_support.viewer import theme

    window = _window(qt, _store(tmp_path, installation_event))
    t = window._tokens
    assert t.dark and t.window.name() == "#0f0d0e" and t.surface.name() == "#211a1a"
    assert t.text.name() == "#dededb" and t.deep.name() == "#601d2c" and t.deep_hover.name() == "#3f1b24"
    assert t.raised.name() == "#2b2324" and t.alternate.name() == "#262020" and t.border.name() == "#403839"
    assert t.stripe.name() == "#767375" and t.faint.name() == "#aca3a2"
    # The red text starts from the brand's #d36e6e; it falls short on search-match and hovered rows, so it is
    # lifted towards white there, keeping its hue.
    assert abs(t.accent_text.hsvHue() - QColor("#d36e6e").hsvHue()) <= 2
    assert QColor("#d36e6e").lightness() <= t.accent_text.lightness() < t.text.lightness()
    assert window.palette().color(qt.QPalette.ColorRole.Window).name() == "#0f0d0e"
    # The muted text is the brand's #aca3a2 (still warm and clearly quieter than the main text).
    assert theme.contrast_ratio(t.faint, t.surface) >= 4.5
    assert t.faint.red() > t.faint.blue() and t.faint.lightness() < t.text.lightness() - 40
    # The interactive red is the brand red; white text on it clears 4.5:1 without being darkened.
    assert t.accent.name() == "#812727" and t.on_accent.name() == "#ffffff"
    assert theme.contrast_ratio(t.on_accent, t.accent) >= 4.5
    # The window fades from the window colour to the card colour; hovered and selected rows use the deep red.
    sheet = window.styleSheet()
    assert "qlineargradient" in sheet and "#0f0d0e" in sheet and "#211a1a" in sheet and t.deep.name() in sheet
    assert t.deep_hover.name() in sheet and t.alternate.name() in sheet and "#963030" in sheet
    # Every colour used as text reads on every background it can sit on.
    backgrounds = [t.window, t.surface, t.raised, t.alternate, t.deep_hover]
    for name in ("text", "faint", "picked", "missed", "mark", "off", "masters", "accent_text"):
        for background in backgrounds:
            assert theme.contrast_ratio(getattr(t, name), background) >= 4.5, (name, background.name())
    # The missed-patch red is not the brand red: lighter, pinker, a different hue.
    brand, missed = QColor("#812727"), t.missed
    gap = abs(missed.hsvHue() - brand.hsvHue())
    assert min(gap, 360 - gap) >= 8
    assert missed.lightness() - brand.lightness() > 40
    # Red is for what is selected and for the one primary action; everyday buttons are outlined in silver.
    assert t.silver.name() == "#c2c2c4" and "rgba(194, 194, 196" in sheet
    assert window.scan_hint_scan.property("primary") is True and window.scan_hint_later.property("primary") is None
    assert window.scan_hint_never.property("primary") is None
    window.resize(1200, 700)
    window.grab()                                  # lays everything out, as a real show would
    for button in (window.toggle_all, window.scan_hint_later, window.theme_button):
        image = button.grab().toImage()
        edge = image.pixelColor(image.width() // 2, 0)       # the middle of the top border
        assert abs(edge.red() - edge.blue()) < 40 and edge.red() > 50, (button.text(), edge.name())   # grey, not red
    # The missed-patch pill and badge are outlined and filled more strongly than the other chips.
    assert t.alarm_alpha > 36
    assert "border: 1px solid " + t.missed.name() in sheet
    # No other theme carries the brand fills.
    for mode in ("auto", "light", "dark"):
        window.set_theme_mode(mode)
        assert window._tokens.deep is None and window._tokens.alternate is None and window._tokens.gradient is None and window._tokens.silver is None
        assert "qlineargradient" not in window.styleSheet()
    window.deleteLater()


def test_the_sayngwyn_theme_follows_the_skills_control_rules(qt, tmp_path, installation_event):
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QColor, QIcon, QPixmap

    from fomod_logger_support.viewer import text_menu

    window = _window(qt, _store(tmp_path, installation_event))
    sheet = window.styleSheet()
    # Selected rows: the deep red with a bone edge, on the selected row only (transparent borders on every row hid
    # the rows' own colours); its padding is one step smaller, so the text doesn't move.
    assert "background: #601d2c; color: #dededb; border-top: 1px solid #dededb; border-bottom: 1px solid #dededb;" in sheet
    assert "::item:selected:first { border-left: 3px solid #dededb; }" in sheet
    assert "solid transparent" not in sheet and "padding-top: 3px; padding-bottom: 3px;" in sheet
    # Toggled-on buttons red with white text; the default dialog button a bone outline; no red on inputs' focus.
    assert "QToolButton:checked" in sheet and "background: #812727; color: #ffffff" in sheet
    assert "QPushButton:default" in sheet and "background: transparent; border: 1px solid #dededb" in sheet
    assert "QLineEdit#search:focus" in sheet and "QLineEdit#search:hover" in sheet
    focus_rules = [line for line in sheet.splitlines() if "#search:focus" in line]
    assert focus_rules[-1].endswith("{ border: 1px solid #dededb; }"), focus_rules
    # Headers raised with dividers and the deep hover; progress in the deep red; inputs disabled on the window colour.
    assert "QHeaderView::section:hover" in sheet and "#3f1b24" in sheet and "background: #2b2324; color: #dededb" in sheet
    assert "QProgressBar::chunk { background: #601d2c; }" in sheet
    assert "background: #0f0d0e; color: #aca3a2" in sheet
    # A header's own widget carries no padding: that squeezed the column titles until their descenders were cut off.
    from PyQt6.QtWidgets import QTreeWidget

    listing = QTreeWidget(window)          # a list with column titles, styled like the Compare dialog's
    listing.setObjectName("compareTree")
    listing.setHeaderLabels(["Mod · page · group · option", "A", "B"])
    listing.resize(600, 300)
    for mode in ("sayngwyn", "light", "dark"):
        window.set_theme_mode(mode)
        window.grab()
        header = listing.header()
        assert header.height() > 0
        assert header.contentsRect().height() >= header.fontMetrics().height() + 8, (mode, header.contentsRect().height())
    window.set_theme_mode("sayngwyn")
    # Stock menu icons are redrawn in one colour.
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor("#000000"))
    icon = text_menu.tinted(QIcon(pixmap), QColor("#dededb"), QColor("#aca3a2"), 16, 1.0)
    assert icon.pixmap(QSize(16, 16)).toImage().pixelColor(8, 8).name() == "#dededb"
    assert icon.pixmap(QSize(16, 16), QIcon.Mode.Disabled).toImage().pixelColor(8, 8).name() == "#aca3a2"
    window.deleteLater()


def test_rows_show_their_own_colours_and_the_lists_branch_arrows_in_every_own_theme(qt, tmp_path, installation_event):
    from fomod_logger_support.viewer import theme

    window = _window(qt, _store(tmp_path, installation_event))
    window.resize(1200, 700)
    for mode in ("sayngwyn", "light", "dark", "auto"):      # (runs on Qt's Windows style, as MO2 does)
        window.set_theme_mode(mode)
        _open_all(window)
        window.grab()
        picked = window._tokens.picked
        image = window.tree.viewport().grab().toImage()
        near = sum(1 for x in range(image.width()) for y in range(image.height())
                   if theme.contrast_ratio(image.pixelColor(x, y), picked) < 1.08
                   and abs(image.pixelColor(x, y).hsvHue() - picked.hsvHue()) < 12)
        assert near > 20, (mode, near)       # the picked option is drawn in the picked green, not the plain text colour
        sheet = window.styleSheet()
        if mode != "auto":                    # (Match MO2 leaves the arrows to MO2's theme)
            assert "QTreeView::branch:has-children:closed { image: url(" in sheet and "branch-open-" in sheet, mode
    window.deleteLater()


def test_search_matches_show_their_tint_in_every_own_theme(qt, tmp_path, installation_event):
    """A sheet rule with any border on plain rows (even ``none``) made Qt drop the rows' own background: the
    search-match tint never showed. Sample the drawn pixel, not the item's brush."""

    from fomod_logger_support.viewer import theme

    window = _window(qt, _store(tmp_path, installation_event))
    window.resize(1200, 700)
    for mode in ("sayngwyn", "light", "dark", "auto"):
        window.set_theme_mode(mode)
        window.search.setText("Shared label")
        window._search_timer.stop()
        window._apply_search()
        window.grab()
        image = window.tree.viewport().grab().toImage()
        t = window._tokens
        expected = [theme.over(t.mark, t.hit_alpha, row) for row in (t.surface, t.alternate or t.raised, t.raised)]
        x = window.tree.header().sectionSize(0) - 12           # the blank right end of the option's own column
        tinted = 0
        for item in window._items():
            if item.background(0).style() == qt.Qt.BrushStyle.NoBrush:
                continue
            rect = window.tree.visualItemRect(item)
            if 0 <= rect.center().y() < image.height():
                drawn = image.pixelColor(x, rect.center().y())
                assert any(theme.contrast_ratio(drawn, colour) < 1.05 for colour in expected), (mode, drawn.name())
                tinted += 1
        assert tinted > 0, mode
    window.deleteLater()


def test_the_missed_patch_alarm_is_pinkish_in_every_theme_never_the_brand_reds_hue(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    for mode in ("sayngwyn", "light", "dark", "auto"):
        window.set_theme_mode(mode)
        hue = window._tokens.missed.hsvHue()
        assert 340 <= hue <= 356, (mode, window._tokens.missed.name(), hue)
    window.deleteLater()


def test_windows_the_tests_open_never_reach_the_desktop(qt, tmp_path, installation_event):
    from gallery import qtenv

    window = _window(qt, _store(tmp_path, installation_event))
    window.show()                                   # what MO2 does; the tests must not put it on the desktop
    dialog = window.compare_choices()               # the code show()s this one itself
    qt.QApplication.instance().processEvents()
    assert window.isVisible() and (dialog is None or dialog.isVisible())
    assert qtenv.desktop_windows() == []
    if dialog is not None:
        dialog.close()
    window.close()
    window.deleteLater()


# A deliberately loud MO2 theme: if any of it shows in the window's own themes, a widget isn't covered. Plain rows
# are the one thing left uncovered on purpose: a row's colour and background are its own (picked, switched off,
# missing masters, search matches, alternating rows), and a sheet rule for them would hide those, as it hides MO2's
# own row colours. Selected and hovered rows are covered.
_LOUD_MO2_SHEET = """
* { background-color: #ff00ff; color: #00ff00; border: 2px solid #00ffff;
    selection-background-color: #ff00ff; selection-color: #00ff00; }
QAbstractItemView { background-color: #ff00ff; alternate-background-color: #ff00ff; color: #00ff00; }
QAbstractItemView::item:selected, QAbstractItemView::item:hover { background-color: #ff00ff; color: #00ff00; }
QHeaderView::section, QTabBar::tab { background-color: #ff00ff; color: #00ff00; border: 2px solid #00ffff; }
QScrollBar, QScrollBar::handle, QSplitter::handle, QTreeView::branch { background-color: #ff00ff; border: 2px solid #00ffff; }
QCheckBox::indicator, QAbstractItemView::indicator { background-color: #ff00ff; border: 2px solid #00ffff; }
QToolButton, QPushButton, QComboBox, QComboBox::drop-down, QLineEdit, QPlainTextEdit, QProgressBar, QMenu, QToolTip {
    background-color: #ff00ff; color: #00ff00; border: 2px solid #00ffff; }
QProgressBar::chunk { background-color: #00ffff; }
"""


def _loud_pixels(widget) -> int:
    image = widget.grab().toImage()
    loud = ((255, 0, 255), (0, 255, 0), (0, 255, 255))

    def is_loud(pixel) -> bool:
        rgb = (pixel.red(), pixel.green(), pixel.blue())
        return any(all(abs(c - v) < 40 for c, v in zip(rgb, colour)) for colour in loud)

    return sum(1 for x in range(0, image.width(), 2) for y in range(0, image.height(), 2) if is_loud(image.pixelColor(x, y)))


def test_no_mo2_sheet_shows_through_the_windows_own_themes(qt, tmp_path, installation_event):
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    before = app.styleSheet()
    database = _store(tmp_path, installation_event)
    try:
        app.setStyleSheet(_LOUD_MO2_SHEET)
        for mode in ("sayngwyn", "light", "dark"):
            window = _window(qt, database)
            if mode != "sayngwyn":
                window.set_theme_mode(mode)       # a switch before the first show, which once left the old colours
            window.resize(1300, 800)
            _open_all(window)
            window.grab()                          # lays everything out, as a real show would
            assert _loud_pixels(window) == 0, mode
            window.deleteLater()
        # And back and forth on one window.
        window = _window(qt, database)
        window.resize(1300, 800)
        for mode in ("dark", "light", "sayngwyn", "auto", "dark"):
            window.set_theme_mode(mode)
        window.grab()
        assert _loud_pixels(window) == 0
        # "Match MO2" is the one choice that does show MO2's theme, as it should.
        window.set_theme_mode("auto")
        window.grab()
        assert _loud_pixels(window) > 0
        window.deleteLater()
    finally:
        app.setStyleSheet(before)


# The brand skill that owns the sayngwyn colours. Dev-only: skipped where it isn't installed (it isn't part of the
# published source, and nothing reads it at runtime).
_BRAND_SKILL = Path(os.environ.get("SAYNGWYN_BRAND_SKILL",
                                   str(Path.home() / ".claude" / "skills" / "sayngwyn-mod-style" / "SKILL.md")))


def test_the_built_in_sayngwyn_colours_are_the_brand_skills(qt):
    import re

    from fomod_logger_support.viewer import theme

    if not _BRAND_SKILL.is_file():
        pytest.skip(f"brand skill not found at {_BRAND_SKILL}")
    text = _BRAND_SKILL.read_text(encoding="utf-8")
    table = text[text.index("## App and window theme"):]
    rows = {}
    for line in table.splitlines()[1:]:
        if line.startswith("## "):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0] not in ("Role", "---") and "whole-app theme only" not in cells[0]:
            rows[cells[0]] = cells[1].lower()      # (rows for a whole-app MO2 theme only, like its Run button, don't apply)

    def row(start: str) -> str:
        hits = [colour for role, colour in rows.items() if role.startswith(start)]
        assert len(hits) == 1, f"the skill's table has no single row starting {start!r}"
        return hits[0]

    fixed = theme._FIXED["sayngwyn"]
    ours = {
        "Window background": [fixed["window"]], "Panels and cards": [fixed["base"]], "Alternate rows": [fixed["alternate"]],
        "Raised surfaces": [fixed["button"], fixed["tooltip"], theme._BRAND_RAISED], "Borders": [theme._BRAND_BORDER],
        "Neutral side stripes": [theme._BRAND_STRIPE], "Main text": [fixed["text"]], "Muted text": [fixed["faint"]],
        "Accent:": [fixed["highlight"], theme._BRAND_ACCENT_HOVER], "Selected row": [theme._BRAND_DEEP, theme._BRAND_DEEP_HOVER],
        "Accent text": [theme._BRAND_CHIP_TEXT], "Errors and alarms": [theme._BRAND_ALARM], "Everyday buttons": [theme._BRAND_SILVER],
    }
    for start, colours in ours.items():
        cell = row(start)
        for colour in colours:
            assert colour.lower() in cell, f"{colour} is not what the skill's {start!r} row says: {cell}"
    # And no colour of the table is missing here (the lifted text variants are derived, not set).
    used = {colour.lower() for colours in ours.values() for colour in colours}
    derived = {"#dd8f8f", "#ff7993", "#8a7f80"}      # lifted accent text and alarm text; the too-dark page grey
    missing = set(re.findall(r"#[0-9a-f]{6}", " ".join(rows.values()))) - used - derived
    assert not missing, f"the skill's table has colours the built-in sayngwyn copy lacks: {sorted(missing)}"


def test_the_first_run_is_branded_only_in_the_sayngwyn_theme(qt, tmp_path, installation_event):
    from fomod_logger_support.viewer import theme

    window = _window(qt, _store(tmp_path))
    window.resize(1200, 700)
    window.grab()
    empty = window.empty_state
    # The mark, faded, above one line in Sayngwyn Serif with the red offset shadow; the guidance stays in the plain font.
    assert empty.isVisibleTo(window) and empty.title.text() == "No mods logged yet."
    assert "Scan existing mods" in empty.guide.text() and not window._items()
    assert theme.brand_family() is not None and empty.title.font().family() == theme.brand_family()
    assert empty.title.font().pixelSize() == theme.BRAND_TEXT_PX >= 18
    assert empty.title.graphicsEffect().color().name() == "#601d2c" and empty.title.graphicsEffect().blurRadius() == 0
    assert empty.mark.graphicsEffect().opacity() == theme.MARK_OPACITY
    assert empty.mark.pixmap().deviceIndependentSize().height() == theme.MARK_PX
    assert empty.geometry() == window.tree.viewport().rect()
    assert theme.contrast_ratio(window._tokens.faint, window._tokens.surface) >= 4.5
    # Every other theme keeps the plain row.
    for mode in ("auto", "light", "dark"):
        window.set_theme_mode(mode)
        assert not empty.isVisibleTo(window)
        assert any("No mods logged yet" in item.text(0) for item in window._items())
    window.set_theme_mode("sayngwyn")
    assert empty.isVisibleTo(window) and not window._items()
    window.deleteLater()
    # Once something is logged the list shows it and the message goes.
    (tmp_path / "full").mkdir()
    full = _window(qt, _store(tmp_path / "full", installation_event))
    assert not full.empty_state.isVisibleTo(full) and full._items()
    full.deleteLater()

def test_switched_off_patches_are_listed_apart_and_marked_in_amber(qt, tmp_path, installation_event):
    event = deepcopy(installation_event)
    skipped = event["steps"][0]["groups"][1]["options"][0]
    skipped.update(name="Village patch", plugins=["GP - Village.esp"], masters=["Village.esp"], detects=[])
    load_order = {"instance_id": event["instance"]["id"], "active_plugins": [], "base_plugins": [], "mods": [],
                  "unchecked_plugins": [], "disabled_mod_plugins": {"Village.esp": "Village"}}
    window = _window(qt, _store(tmp_path, event), load_order=lambda: load_order)
    [item] = window.snapshot.missed
    assert item["state"] == "needs-disabled"
    assert window.missed_chip.isHidden()  # not counted as a missed patch
    assert window.off_box.isVisibleTo(window) and not window.missed_box.isVisibleTo(window)
    assert window.off_count.text() == "1"
    texts = [label.text() for label in window.findChildren(qt.QLabel) if label.isVisibleTo(window)]
    assert "Village patch" in texts  # the card leads with the patch, not the mod
    assert any("Village.esp is in the mod “Village”, which is disabled" in text for text in texts)
    _open_all(window)
    assert "● present but disabled" in [i.text(1) for i in window._items()]
    assert window.off_chip.text() == "1 plugin switched off"
    window.deleteLater()


def test_a_plugin_missing_a_master_gets_its_own_section(qt, tmp_path, installation_event):
    event = deepcopy(installation_event)
    picked = event["steps"][0]["groups"][0]["options"][0]
    picked.update(name="Keld Nar", plugins=["Lux Via - Keld Nar patch.esp"], detects=[])
    load_order = {"instance_id": event["instance"]["id"], "active_plugins": [], "base_plugins": [], "mods": [],
                  "unchecked_plugins": ["Lux Via - Keld Nar patch.esp"],
                  "plugin_masters": {"Lux Via - Keld Nar patch.esp": ["Keld-Nar.esp"]}, "disabled_mod_plugins": {}}
    window = _window(qt, _store(tmp_path, event), load_order=lambda: load_order)
    assert window.masters_box.isVisibleTo(window) and not window.off_box.isVisibleTo(window)
    assert window.masters_chip.text() == "1 plugin can't load"
    texts = [label.text() for label in window.findChildren(qt.QLabel) if label.isVisibleTo(window)]
    assert any("it needs Keld-Nar.esp, which isn't in your list" in text for text in texts)
    window.deleteLater()


def test_side_sections_fold_from_their_heading_and_stay_folded(qt, tmp_path, installation_event):
    event = deepcopy(installation_event)
    picked = event["steps"][0]["groups"][0]["options"][0]
    picked.update(name="Keld Nar", plugins=["Lux Via - Keld Nar patch.esp"], detects=[])
    load_order = {"instance_id": event["instance"]["id"], "active_plugins": [], "base_plugins": [], "mods": [],
                  "unchecked_plugins": ["Lux Via - Keld Nar patch.esp"],
                  "plugin_masters": {"Lux Via - Keld Nar patch.esp": ["Keld-Nar.esp"]}, "disabled_mod_plugins": {}}
    database, settings = _store(tmp_path, event), tmp_path / "window.json"

    def card_shown(window) -> bool:
        return any("Keld-Nar.esp" in label.text() for label in window.findChildren(qt.QLabel) if label.isVisibleTo(window))

    window = _window(qt, database, load_order=lambda: load_order, geometry_file=settings)
    title = next(label for label in window.masters_box.findChildren(qt.QLabel) if label.objectName() == "sectionTitle")
    assert card_shown(window)
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication

    # (MO2 ships no QtTest.) The heading text folds it, like its arrow.
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(title, QMouseEvent(kind, QPointF(4, 4), QPointF(4, 4), qt.Qt.MouseButton.LeftButton,
                                                  qt.Qt.MouseButton.LeftButton, qt.Qt.KeyboardModifier.NoModifier))
    assert not card_shown(window)
    assert window.masters_box.isVisibleTo(window) and window.masters_count.isVisibleTo(window)  # the count stays
    window.deleteLater()

    window = _window(qt, database, load_order=lambda: load_order, geometry_file=settings)
    assert not card_shown(window)  # remembered
    [arrow] = [button for button in window.masters_box.findChildren(qt.QToolButton) if button.objectName() == "sectionFold"]
    arrow.click()
    assert card_shown(window)
    window.deleteLater()


# ---------- changed installers ----------

OLD_ID = "44444444-4444-4444-8444-444444444444"
NEW_ID = "55555555-5555-4555-8555-555555555555"
NEWER_ID = "66666666-6666-4666-8666-666666666666"
VILLAGE = "GP - Village.esp"


def _settle() -> None:
    from PyQt6.QtWidgets import QApplication

    QApplication.processEvents()


def _updated_pair(installation_event, *, picked_patch=False):
    """The fixture mod installed twice: the second install renames one option, drops one and adds a
    patch for Village.esp (picked or skipped) and one for a mod that isn't in the list."""

    old = deepcopy(installation_event)
    old["event_id"] = OLD_ID
    old["mod"]["version"] = "1.0"
    old["steps"][0]["groups"][1]["options"].append(
        {"id": "step:0/group:1/option:1", "name": "Old extra", "selected": False, "enabled": True})
    new = deepcopy(installation_event)
    new.update(event_id=NEW_ID, completed_at="2026-09-01T12:00:00+00:00")
    new["source"]["reinstallation"] = True
    new["mod"]["version"] = "1.1"
    first, second = new["steps"][0]["groups"]
    first["options"][0]["name"] = "Renamed label"
    second["options"] = [
        second["options"][0],
        {"id": "step:0/group:1/option:1", "name": "Village patch", "selected": picked_patch, "enabled": True,
         "plugins": [VILLAGE], "masters": ["Village.esp"], "detects": []},
        {"id": "step:0/group:1/option:2", "name": "Elsewhere patch", "selected": False, "enabled": True,
         "plugins": ["GP - Elsewhere.esp"], "masters": ["Elsewhere.esp"], "detects": []},
    ]
    load_order = {"instance_id": old["instance"]["id"], "active_plugins": ["Village.esp"],
                  "base_plugins": [], "mods": []}
    return old, new, load_order


def test_model_lists_installer_changes_and_what_was_marked_seen(tmp_path, installation_event):
    old, new, load_order = _updated_pair(installation_event)
    database = _store(tmp_path, old, new)
    snapshot = model.load(database, model.ViewState(), lambda: load_order)
    [change] = snapshot.changes
    assert change["event_id"] == NEW_ID and change["old_event_id"] == OLD_ID
    assert model.changes_line(change) == "1.0 → 1.1: 2 new options, 1 removed, 1 renamed"
    [patch] = change["patches"]
    assert model.patch_line(patch) == (
        "New: Village patch — you have Village.esp; you skipped it", "missed")
    assert model.changes_for(snapshot, NEW_ID) is change and model.changes_for(snapshot, OLD_ID) is None
    assert snapshot.changes_seen == set()
    with EventStore(database) as store:
        assert store.set_changes_seen(NEW_ID, True) and store.set_changes_seen(NEW_ID, True)
        assert store.set_changes_seen("00000000-0000-4000-8000-000000000000", True) is False
    assert model.load(database, model.ViewState()).changes_seen == {NEW_ID}
    # A hidden mod's card is left out unless hidden mods are shown.
    with EventStore(database) as store:
        store.set_hidden(NEW_ID, True)
    assert model.load(database, model.ViewState()).changes == []
    assert len(model.load(database, model.ViewState(include_hidden=True)).changes) == 1


def test_window_lists_a_changed_installer_with_its_new_patches_and_settles_it_with_seen(qt, tmp_path, installation_event):
    old, new, load_order = _updated_pair(installation_event)
    changes = []
    database = _store(tmp_path, old, new)
    window = _window(qt, database, load_order=lambda: load_order, on_changed=lambda: changes.append(1))
    box = window.changes_box
    assert box.isVisibleTo(window) and window.changes_count.text() == "1"
    titles = [label.text() for label in box.findChildren(qt.QLabel) if label.isVisibleTo(window)]
    assert "Fixture Mod" in titles
    assert "1.0 → 1.1: 2 new options, 1 removed, 1 renamed" in titles
    assert "New: Village patch — you have Village.esp; you skipped it" in titles
    assert not any("Elsewhere" in text for text in titles)  # its mod isn't in the list: not called out
    # It sits between "Present but plugin disabled" and "To revisit", and adds nothing to the header chips.
    order = [window.side.itemAt(i).widget() for i in range(window.side.count())]
    assert order.index(window.off_box) + 1 == order.index(box) and order.index(box) + 1 == order.index(window.review_box)
    assert window.missed_chip.text() == "1 possible missed patch"  # the skipped patch is counted once, as before
    assert len(window.snapshot.missed) == 1 and window.revisit_chip.isHidden()

    assert next(b for b in box.findChildren(qt.QPushButton) if b.text() == "Show").isVisibleTo(window)
    next(b for b in box.findChildren(qt.QPushButton) if b.text() == "Seen").click()
    _settle()  # the button acts once its click has returned
    assert changes == [1]  # the plugin is told, as for any other change
    assert window.snapshot.changes_seen == {NEW_ID}
    assert window.changes_count.isHidden()  # nothing left to check
    assert box.isVisibleTo(window)
    assert any(button.text().startswith("Seen (1)") for button in box.findChildren(qt.QToolButton))
    assert window.missed_chip.text() == "1 possible missed patch"  # unchanged by Seen
    # Reopen it from the Seen list.
    window.set_changes_seen(NEW_ID, False)
    assert window.snapshot.changes_seen == set() and window.changes_count.text() == "1"
    window.deleteLater()


def test_show_selects_the_newest_install(qt, tmp_path, installation_event):
    old, new, load_order = _updated_pair(installation_event)
    window = _window(qt, _store(tmp_path, old, new), load_order=lambda: load_order)
    next(b for b in window.changes_box.findChildren(qt.QPushButton) if b.text() == "Show").click()
    _settle()
    assert window.tree.currentItem().data(0, qt._ROLE) == ("event", NEW_ID, "")
    window.deleteLater()


def test_a_newer_install_that_changes_things_again_brings_a_new_card(qt, tmp_path, installation_event):
    old, new, load_order = _updated_pair(installation_event)
    newer = deepcopy(new)
    newer.update(event_id=NEWER_ID, completed_at="2026-09-10T12:00:00+00:00")
    newer["mod"]["version"] = "1.2"
    newer["steps"][0]["groups"][1]["options"].append(
        {"id": "step:0/group:1/option:3", "name": "Brand new", "selected": False, "enabled": True})
    database = _store(tmp_path, old, new)
    with EventStore(database) as store:
        store.set_changes_seen(NEW_ID, True)
    window = _window(qt, database, load_order=lambda: load_order)
    assert window.changes_count.isHidden()
    with EventStore(database) as store:
        store.ingest(newer)
    window.refresh(force=True)
    [change] = window.snapshot.changes
    assert change["event_id"] == NEWER_ID and model.changes_line(change) == "1.1 → 1.2: 1 new option"
    assert window.changes_count.text() == "1"
    # A reinstall that changes nothing leaves no card at all.
    same = deepcopy(newer)
    same.update(event_id="77777777-7777-4777-8777-777777777777", completed_at="2026-09-20T12:00:00+00:00")
    with EventStore(database) as store:
        store.ingest(same)
    window.refresh(force=True)
    assert window.snapshot.changes == [] and not window.changes_box.isVisibleTo(window)
    window.deleteLater()


def test_the_tree_marks_what_is_new_on_the_newest_install_only(qt, tmp_path, installation_event):
    old, new, load_order = _updated_pair(installation_event, picked_patch=True)
    window = _window(qt, _store(tmp_path, old, new), load_order=lambda: load_order)
    window.state.view = "history"
    window.refresh()
    _open_all(window)
    marks = {item.text(0): item.text(1) for item in window._items() if item.text(1)}
    assert marks["✓  Village patch"] == "+ new patch"           # picked: no missed-patch alarm
    assert marks["○  Elsewhere patch"] == "+ new"
    assert marks["✓  Renamed label"] == "~ was “Shared label”"
    [gone] = [item for item in window._items() if item.text(0) == "–  Old extra"]  # only under the newest install
    assert gone.text(1) == "removed in this version" and gone.data(0, qt._ROLE) is None
    assert not gone.flags() & qt.Qt.ItemFlag.ItemIsSelectable
    assert gone.parent().text(0) == "Second group"
    assert "○  Old extra" in [item.text(0) for item in window._items()]  # the old install: a normal option
    assert "+2 new" in window.tree.topLevelItem(0).text(1)  # the newest install (history lists it first)
    assert "new" not in window.tree.topLevelItem(1).text(1)
    # The "Picked" filter hides a removed option the old install had not picked.
    window.state.choices = "picked"
    window._render()
    _open_all(window)
    assert "–  Old extra" not in [item.text(0) for item in window._items()]
    window.deleteLater()


def test_a_removed_option_whose_group_is_gone_is_listed_at_the_end(qt, tmp_path, installation_event):
    old, new, load_order = _updated_pair(installation_event)
    new["steps"][0]["groups"].pop(1)  # "Second group" is gone from the installer
    window = _window(qt, _store(tmp_path, old, new), load_order=lambda: load_order)
    _open_all(window)
    texts = [item.text(0) for item in window._items()]
    assert "No longer in the installer" in texts
    assert "–  Main › Second group › Old extra" in texts
    window.deleteLater()


def test_the_tree_marker_follows_the_load_order(qt, tmp_path, installation_event):
    old, new, load_order = _updated_pair(installation_event)
    current = {"order": dict(load_order, active_plugins=[])}
    window = _window(qt, _store(tmp_path, old, new), load_order=lambda: current["order"])
    _open_all(window)
    assert {i.text(0): i.text(1) for i in window._items()}["○  Village patch"] == "+ new"
    current["order"] = load_order  # Village.esp is switched on
    window.refresh()
    _open_all(window)
    marks = {i.text(0): i.text(1) for i in window._items()}
    assert marks["○  Village patch"].startswith("+ new patch")
    window.deleteLater()


# ---------- changed picks since the last install ----------

def _picks_pair(installation_event, *, unpick_first=False):
    """The fixture mod installed twice with the same installer (plus a third option in the second
    group) and one more pick the second time; optionally the first group's pick is dropped too."""

    old = deepcopy(installation_event)
    old["event_id"] = OLD_ID
    old["mod"]["version"] = "1.0"
    old["steps"][0]["groups"][1]["options"].append(
        {"id": "step:0/group:1/option:1", "name": "Extra shine", "selected": False, "enabled": True})
    new = deepcopy(old)
    new.update(event_id=NEW_ID, completed_at="2026-09-01T12:00:00+00:00")
    new["source"]["reinstallation"] = True
    new["steps"][0]["groups"][1]["options"][1]["selected"] = True
    if unpick_first:
        new["steps"][0]["groups"][0]["options"][0]["selected"] = False
    return old, new


def test_model_lists_changed_picks_without_a_card(tmp_path, installation_event):
    old, new = _picks_pair(installation_event)
    snapshot = model.load(_store(tmp_path, old, new), model.ViewState())
    assert snapshot.changes == []                       # the installer is the same: no card
    [compared] = snapshot.compared
    assert compared["event_id"] == NEW_ID and compared["changed"] is False
    assert model.picks_text(compared) == "+1 picked"
    old, new = _picks_pair(installation_event, unpick_first=True)
    (tmp_path / "second").mkdir()
    [compared] = model.load(_store(tmp_path / "second", old, new), model.ViewState()).compared
    assert model.picks_text(compared) == "+1 picked, \u22121 unpicked"


def test_the_tree_marks_changed_picks_and_the_mod_row_says_so_with_no_card(qt, tmp_path, installation_event):
    old, new = _picks_pair(installation_event, unpick_first=True)
    window = _window(qt, _store(tmp_path, old, new))
    assert window.snapshot.changes == [] and not window.changes_box.isVisibleTo(window)  # no side card
    assert window.tree.topLevelItemCount() == 1
    row = window.tree.topLevelItem(0)
    assert row.text(1).startswith("+1 picked, \u22121 unpicked") and "Picks changed since the previous install" in row.toolTip(0)
    _open_all(window)
    marks = {item.text(0): item.text(1) for item in window._items() if item.text(1)}
    assert marks["✓  Extra shine"] == "newly picked"
    assert marks["○  Shared label"] == "was picked"       # the first group's option (the row stays a normal one)
    assert "✓  Shared label" not in marks
    unpicked = next(i for i in window._items() if i.text(0) == "○  Shared label" and i.text(1) == "was picked")
    assert unpicked.flags() & qt.Qt.ItemFlag.ItemIsSelectable
    assert unpicked.foreground(1).color() == window._colours["accent"]
    newly = next(i for i in window._items() if i.text(0) == "✓  Extra shine")
    assert newly.foreground(1).color() == window._colours["picked"]
    assert "didn't pick this in the previous install" in newly.toolTip(0)
    window.deleteLater()


def test_the_older_install_is_not_marked_for_picks_and_new_picks_redraw_the_tree(qt, tmp_path, installation_event):
    old, new = _picks_pair(installation_event)
    database = _store(tmp_path, old)
    window = _window(qt, database)
    window.state.view = "history"
    window.refresh()
    assert window.tree.topLevelItem(0).text(1) == ""     # one install: nothing to compare with
    signature = window._tree_signature()
    with EventStore(database) as store:
        store.ingest(new)
    window.refresh(force=True)
    assert window._tree_signature() != signature
    assert window.tree.topLevelItem(0).text(1).startswith("+1 picked") and window.tree.topLevelItem(1).text(1) == ""
    _open_all(window)
    assert [i.text(1) for i in window._items() if i.text(0) == "✓  Extra shine"] == ["newly picked"]
    assert [i.text(1) for i in window._items() if i.text(0) == "○  Extra shine"] == [""]  # the older install
    # Picking the same again in a third install is no change from the one before it.
    third = deepcopy(new)
    third.update(event_id=NEWER_ID, completed_at="2026-09-10T12:00:00+00:00")
    with EventStore(database) as store:
        store.ingest(third)
    window.refresh(force=True)
    assert window.snapshot.compared == []
    assert not any("picked" in i.text(1) for i in window._items())
    window.deleteLater()


def test_an_installer_change_card_mentions_picks_changed_too(qt, tmp_path, installation_event):
    old, new, load_order = _updated_pair(installation_event)
    new["steps"][0]["groups"][1]["options"][0]["selected"] = True  # "Shared label" in the second group, picked now
    window = _window(qt, _store(tmp_path, old, new), load_order=lambda: load_order)
    [change] = window.snapshot.changes
    assert model.picks_text(change) == "+1 picked"
    titles = [label.text() for label in window.changes_box.findChildren(qt.QLabel) if label.isVisibleTo(window)]
    assert "You also changed 1 pick since the last install." in titles
    assert window.changes_count.text() == "1"
    window.deleteLater()


# ---------- new-patch alerts ----------

def _alert_world(tmp_path: Path, *, disabled: bool = False, alert: bool = True):
    """Grass Patches skipped a Village patch; Village was just installed (switched on, or still off)."""

    from test_patch_alerts import GRASS_ID, INSTANCE, VILLAGE_OPTION, grass_patches, order

    database = _store(tmp_path, grass_patches())
    load_order = (order(disabled={"Village.esp": "Village"}, disabled_mods=("Village",)) if disabled
                  else order("Village.esp", mods=("Village",)))
    if alert:
        with EventStore(database) as store:
            store.add_patch_alerts(INSTANCE, "Village", "", [(GRASS_ID, VILLAGE_OPTION)])
    return database, load_order


def _new_labels(qt, window) -> list[str]:
    return [label.text() for label in window.findChildren(qt.QLabel)
            if label.objectName() == "cardNew" and label.isVisibleTo(window)]


def test_model_lists_the_alerts_that_still_apply(tmp_path):
    from test_patch_alerts import GRASS_ID, VILLAGE_OPTION

    database, load_order = _alert_world(tmp_path)
    snapshot = model.load(database, model.ViewState(), lambda: load_order)
    [alert] = snapshot.alerts
    assert (alert["event_id"], alert["option_id"], alert["mod_name"]) == (GRASS_ID, VILLAGE_OPTION, "Village")
    assert alert["item"]["label"] == "Village patch"
    # No load order, no alerts; the patch installed or marked Not needed, no alert.
    assert model.load(database, model.ViewState()).alerts == []
    installed = {**load_order, "active_plugins": [*load_order["active_plugins"], "GP - Village.esp"]}
    assert model.load(database, model.ViewState(), lambda: installed).alerts == []
    with EventStore(database) as store:
        store.save_review({"event_id": GRASS_ID, "option_id": VILLAGE_OPTION, "reason": "", "state": "dismissed"})
    assert model.load(database, model.ViewState(), lambda: load_order).alerts == []


def test_the_card_of_a_new_alert_says_so_and_opens_a_folded_section_once(qt, tmp_path):
    import json

    database, load_order = _alert_world(tmp_path)
    settings = tmp_path / "window.json"
    settings.write_text(json.dumps({"folded_sections": ["missed"]}), encoding="utf-8")
    window = _window(qt, database, load_order=lambda: load_order, geometry_file=settings)
    assert window.missed_box.isVisibleTo(window)
    assert _new_labels(qt, window) == ["New: you just installed Village"]    # unfolded although it was folded
    assert json.loads(settings.read_text(encoding="utf-8"))["folded_sections"] == ["missed"]   # but not remembered
    assert window.missed_count.text() == "1"
    _open_all(window)
    assert [i.text(1) for i in window._items() if i.text(0).endswith("Village patch")] == ["just installed  ·  ⚠ possible missed patch"]

    # The user folds it again: a later redraw leaves it alone.
    window._arrows["missed"].setChecked(True)
    window.refresh(force=True)
    assert _new_labels(qt, window) == []
    assert json.loads(settings.read_text(encoding="utf-8"))["folded_sections"] == ["missed"]
    window.deleteLater()

    # No alert, no unfolding.
    plain_db, plain_order = _alert_world(tmp_path / "plain", alert=False)
    plain = _window(qt, plain_db, load_order=lambda: plain_order, geometry_file=settings)
    assert _new_labels(qt, plain) == []
    assert not plain.missed_list.itemAt(0).widget().isVisibleTo(plain)
    plain.deleteLater()


def test_the_alert_for_a_mod_still_switched_off_points_at_the_switched_off_card(qt, tmp_path):
    import json

    database, load_order = _alert_world(tmp_path, disabled=True)
    settings = tmp_path / "window.json"
    settings.write_text(json.dumps({"folded_sections": ["off", "missed"]}), encoding="utf-8")
    window = _window(qt, database, load_order=lambda: load_order, geometry_file=settings)
    [item] = window.snapshot.missed
    assert item["state"] == "needs-disabled"
    assert window.off_box.isVisibleTo(window) and not window.missed_box.isVisibleTo(window)
    assert _new_labels(qt, window) == ["New: you just installed Village"]    # in the orange section, unfolded
    texts = [label.text() for label in window.findChildren(qt.QLabel) if label.isVisibleTo(window)]
    assert any("Village.esp is in the mod “Village”, which is disabled" in text for text in texts)
    assert json.loads(settings.read_text(encoding="utf-8"))["folded_sections"] == ["off", "missed"]
    window.deleteLater()


def test_fix_in_mo2_marks_the_cards_until_the_window_is_closed(qt, tmp_path):
    from test_patch_alerts import GRASS_ID, VILLAGE_OPTION

    database, load_order = _alert_world(tmp_path, alert=False)    # the plugin settled the alert before opening
    window = _window(qt, database, load_order=lambda: load_order)
    assert _new_labels(qt, window) == []
    window.highlight_alerts([(GRASS_ID, VILLAGE_OPTION, "Village")])
    assert _new_labels(qt, window) == ["New: you just installed Village"]
    window.refresh(force=True)
    assert _new_labels(qt, window) == ["New: you just installed Village"]   # stays through redraws
    window.close()
    window.refresh()    # nothing changed in the data: the marks still have to go
    assert _new_labels(qt, window) == []
    assert not [i for i in window._items() if i.text(1).startswith("just installed")]
    window.deleteLater()


def test_an_alert_goes_from_the_window_when_the_patch_is_marked_not_needed(qt, tmp_path):
    from test_patch_alerts import GRASS_ID, VILLAGE_OPTION

    database, load_order = _alert_world(tmp_path)
    window = _window(qt, database, load_order=lambda: load_order)
    assert len(_new_labels(qt, window)) == 1
    window.apply_reminder(GRASS_ID, VILLAGE_OPTION, "save", "Not needed", "dismissed")
    assert window.snapshot.alerts == [] and _new_labels(qt, window) == []
    window.deleteLater()


def test_a_card_for_a_mod_just_installed_is_never_behind_show_more(qt, tmp_path, monkeypatch):
    from copy import deepcopy

    from test_patch_alerts import GRASS_ID, INSTANCE, VILLAGE_OPTION, grass_patches, order

    event = grass_patches()
    bridges = deepcopy(event["steps"][0]["groups"][0]["options"][1])
    bridges.update(id="step:0/group:0/option:2", name="Bridges patch", plugins=["GP - Bridges.esp"],
                   masters=["Skyrim.esm", "GrassPatches.esp", "Bridges.esp"], detects=["Bridges.esp"])
    event["steps"][0]["groups"][0]["options"].append(bridges)
    database = _store(tmp_path, event)
    load_order = order("Village.esp", "Bridges.esp", mods=("Village", "Bridges"))
    monkeypatch.setattr(qt, "_CARD_LIMIT", 1)

    def titles(window) -> list[str]:
        return [label.text() for label in window.findChildren(qt.QLabel)
                if label.objectName() == "cardTitle" and label.isVisibleTo(window)]

    plain = _window(qt, database, load_order=lambda: load_order)
    assert titles(plain) == ["Bridges patch"]    # alphabetical: Village's card is behind "Show 1 more"
    plain.deleteLater()
    with EventStore(database) as store:
        store.add_patch_alerts(INSTANCE, "Village", "", [(GRASS_ID, VILLAGE_OPTION)])
    alerted = _window(qt, database, load_order=lambda: load_order)
    assert titles(alerted) == ["Village patch"] and len(_new_labels(qt, alerted)) == 1
    alerted.deleteLater()


def test_two_mods_installed_together_are_named_on_the_card_they_share(qt, tmp_path):
    from test_patch_alerts import GRASS_ID, INSTANCE, VILLAGE_OPTION

    database, load_order = _alert_world(tmp_path, alert=False)
    with EventStore(database) as store:
        store.add_patch_alerts(INSTANCE, "Village", "", [(GRASS_ID, VILLAGE_OPTION)])
        store.add_patch_alerts(INSTANCE, "Bridges", "", [(GRASS_ID, VILLAGE_OPTION)])
    window = _window(qt, database, load_order=lambda: load_order)
    assert _new_labels(qt, window) == ["New: you just installed Bridges and Village"]
    window.deleteLater()


# ---------- export and import of choices ----------

class _Boxes:
    """Stands in for QMessageBox: records what the user would be told and answers questions."""

    def __init__(self, qt, answer=None):
        self.StandardButton = qt.QMessageBox.StandardButton
        self.answer = answer if answer is not None else self.StandardButton.Yes
        self.said: list[tuple[str, str]] = []

    def information(self, _parent, _title, text):
        self.said.append(("information", text))

    def warning(self, _parent, _title, text):
        self.said.append(("warning", text))

    def question(self, _parent, _title, text, *_rest):
        self.said.append(("question", text))
        return self.answer


def _choices_window(qt, tmp_path, monkeypatch, *events, answer=None, **kwargs):
    from test_export_choices import INSTANCE

    boxes = _Boxes(qt, answer)
    monkeypatch.setattr(qt, "QMessageBox", boxes)
    kwargs.setdefault("instance_id", lambda: INSTANCE)
    kwargs.setdefault("game", lambda: {"name": "Skyrim Special Edition", "nexus_domain": "skyrimspecialedition"})
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _window(qt, _store(tmp_path, *events), **kwargs), boxes


def _pick_file(qt, monkeypatch, *, save: str | None = None, open_: str | None = None):
    asked = []

    def save_name(_parent, title, name, filters):
        asked.append(("save", title, name, filters))
        return (save or "", filters.split(";;")[-1] if save and save.endswith(".html") else filters.split(";;")[0])

    def open_name(_parent, title, _folder, filters):
        asked.append(("open", title, filters))
        return (open_ or "", "")

    monkeypatch.setattr(qt.QFileDialog, "getSaveFileName", staticmethod(save_name))
    monkeypatch.setattr(qt.QFileDialog, "getOpenFileName", staticmethod(open_name))
    return asked


def test_the_more_menu_offers_export_and_import(qt, tmp_path, monkeypatch):
    from test_export_choices import grass_event

    window, _boxes = _choices_window(qt, tmp_path, monkeypatch, grass_event())
    actions = [action.text() for action in window.findChildren(qt.QAction) if action.text()]
    for wanted in ("Export install guide…", "Export choices file…", "Export history…", "Import choices file…",
                   "Remove imported choices"):
        assert wanted in actions
    assert actions.index("Export install guide…") < actions.index("Export choices file…") < actions.index("Import choices file…")
    window.deleteLater()


def test_export_install_guide_writes_markdown_or_html_by_extension(qt, tmp_path, monkeypatch):
    from test_export_choices import grass_event

    window, boxes = _choices_window(qt, tmp_path, monkeypatch, grass_event())
    asked = _pick_file(qt, monkeypatch, save=str(tmp_path / "guide.md"))
    assert window.export_install_guide() == 1
    assert asked[0][:3] == ("save", "Export install guide", "modlist-companion-install-guide.md")
    assert asked[0][3] == "Markdown (*.md);;HTML page (*.html)"
    text = (tmp_path / "guide.md").read_text(encoding="utf-8")
    assert text.startswith("# FOMOD install guide") and "- [x] **Tall**" in text
    assert "https://www.nexusmods.com/skyrimspecialedition/mods/4242" in text
    assert boxes.said[-1][0] == "information" and "guide.md" in boxes.said[-1][1]

    _pick_file(qt, monkeypatch, save=str(tmp_path / "guide.html"))
    assert window.export_install_guide() == 1
    assert (tmp_path / "guide.html").read_text(encoding="utf-8").startswith("<!doctype html>")

    _pick_file(qt, monkeypatch, save=str(tmp_path / "plain"))          # no extension: the chosen filter decides
    window.export_install_guide()
    assert (tmp_path / "plain.md").is_file()
    window.deleteLater()


def test_export_choices_file_writes_a_file_the_importer_accepts(qt, tmp_path, monkeypatch):
    from fomod_logger_support.core.choices_file import parse_choices_file
    from test_export_choices import grass_event

    window, boxes = _choices_window(qt, tmp_path, monkeypatch, grass_event())
    asked = _pick_file(qt, monkeypatch, save=str(tmp_path / "mine.json"))
    assert window.export_choices_file() == 1
    assert asked[0][1:3] == ("Export choices file", "modlist-companion-choices.json")
    choices = parse_choices_file((tmp_path / "mine.json").read_bytes())
    assert choices["source"]["game"] == "Skyrim Special Edition" and choices["source"]["nexus_domain"] == "skyrimspecialedition"
    assert [mod["name"] for mod in choices["mods"]] == ["Grass Patches"]
    window.deleteLater()


def test_cancelling_an_export_writes_nothing_and_a_list_with_no_mods_says_so(qt, tmp_path, monkeypatch):
    from test_export_choices import grass_event

    window, boxes = _choices_window(qt, tmp_path, monkeypatch, grass_event())
    _pick_file(qt, monkeypatch, save="")
    assert window.export_install_guide() is None and window.export_choices_file() is None
    assert boxes.said == [] and list(tmp_path.glob("*.md")) + list(tmp_path.glob("*.html")) == []

    empty, boxes = _choices_window(qt, tmp_path / "empty", monkeypatch)
    assert empty.export_choices_file() is None
    assert boxes.said and "no installed mods" in boxes.said[0][1]
    for each in (window, empty):
        each.deleteLater()


def test_exports_leave_out_hidden_mods_until_show_hidden_is_on(qt, tmp_path, monkeypatch):
    from test_export_choices import grass_event

    shown = grass_event("11111111-1111-4111-8111-111111111111", name="Shown Mod")
    hidden = grass_event("22222222-2222-4222-8222-222222222222", name="Hidden Mod")
    window, _boxes = _choices_window(qt, tmp_path, monkeypatch, shown, hidden)
    window.set_hidden(hidden["event_id"], True)

    def exported() -> list[str]:
        window.export_choices_file(str(tmp_path / "out.json"), quiet=True)
        return [mod["name"] for mod in json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["mods"]]

    assert exported() == ["Shown Mod"]
    window.hidden_action.setChecked(True)
    assert exported() == ["Hidden Mod", "Shown Mod"]
    window.deleteLater()


def test_a_failed_export_is_reported_not_raised(qt, tmp_path, monkeypatch):
    from test_export_choices import grass_event

    window, boxes = _choices_window(qt, tmp_path, monkeypatch, grass_event())
    _pick_file(qt, monkeypatch, save=str(tmp_path / "no" / "such" / "folder" / "guide.md"))
    assert window.export_install_guide() is None
    assert boxes.said[-1][0] == "warning" and "Couldn't export" in boxes.said[-1][1]
    with pytest.raises(OSError):
        window.export_install_guide(str(tmp_path / "no" / "such" / "folder" / "guide.md"), quiet=True)
    window.deleteLater()


def _choices_file(tmp_path, *names) -> Path:
    from test_import_choices import choices, entry

    path = tmp_path / "friend.json"
    path.write_text(json.dumps(choices(*(entry(name) for name in names))), encoding="utf-8")
    return path


def test_import_choices_file_stores_the_picks_and_tells_the_user_what_happened(qt, tmp_path, monkeypatch):
    from fomod_logger_support.core import import_choices
    from test_export_choices import INSTANCE, grass_event

    window, boxes = _choices_window(qt, tmp_path, monkeypatch, grass_event())
    path = _choices_file(tmp_path, "Grass Patches", "Brand New Mod")
    asked = _pick_file(qt, monkeypatch, open_=str(path))
    summary = window.import_choices_file()
    assert asked[0][1] == "Import choices file"
    assert (summary.imported, summary.own_wins, summary.not_installed) == (2, 1, 1)
    kind, text = boxes.said[-1]
    assert kind == "information" and "Imported choices for 2 mods." in text
    assert "1 already has your own previous choices" in text and "1 is not installed yet" in text
    assert import_choices.imported_count(tmp_path, INSTANCE) == 2
    window.deleteLater()


def test_a_bad_choices_file_is_refused_with_a_message_and_nothing_is_stored(qt, tmp_path, monkeypatch):
    from fomod_logger_support.core import import_choices
    from fomod_logger_support.core.choices_file import ChoicesFileError
    from test_export_choices import INSTANCE, grass_event

    window, boxes = _choices_window(qt, tmp_path, monkeypatch, grass_event())
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "fomod-companion-choices", "format_version": 1, "mods": [{"name": "A"}]}', encoding="utf-8")
    _pick_file(qt, monkeypatch, open_=str(bad))
    assert window.import_choices_file() is None
    kind, text = boxes.said[-1]
    assert kind == "warning" and "Couldn't import that file" in text and "steps must be a list" in text
    assert import_choices.imported_count(tmp_path, INSTANCE) == 0
    with pytest.raises(ChoicesFileError):
        window.import_choices_file(str(bad), quiet=True)

    _pick_file(qt, monkeypatch, open_="")          # cancelled
    assert window.import_choices_file() is None
    window.deleteLater()


def test_import_needs_to_know_the_instance(qt, tmp_path, monkeypatch):
    window, boxes = _choices_window(qt, tmp_path, monkeypatch, instance_id=lambda: "")
    window.snapshot.instances = []
    path = _choices_file(tmp_path, "Alpha")
    assert window.import_choices_file(str(path)) is None
    assert "isn't known yet" in boxes.said[-1][1]
    window.deleteLater()


def test_the_instance_falls_back_to_the_plugins_snapshot_then_the_only_one_in_the_database(qt, tmp_path, monkeypatch):
    from test_export_choices import INSTANCE, grass_event

    window, _ = _choices_window(qt, tmp_path, monkeypatch, grass_event(), instance_id=lambda: "",
                                load_order=lambda: {"instance_id": "c" * 64})
    assert window._this_instance() == "c" * 64
    alone, _ = _choices_window(qt, tmp_path / "alone", monkeypatch, grass_event(), instance_id=lambda: "")
    assert alone._this_instance() == INSTANCE
    for each in (window, alone):
        each.deleteLater()


def test_remove_imported_choices_asks_first_and_only_removes_imports(qt, tmp_path, monkeypatch):
    from fomod_logger_support.core import import_choices
    from test_export_choices import INSTANCE, grass_event

    window, boxes = _choices_window(qt, tmp_path, monkeypatch, grass_event())
    assert window.remove_imported_choices() == 0 and "no imported choices" in boxes.said[-1][1]
    window.import_choices_file(str(_choices_file(tmp_path, "Alpha", "Beta")), quiet=True)
    window._update_import_actions()
    assert window.remove_imported_action.isEnabled()

    boxes.answer = boxes.StandardButton.No
    assert window.remove_imported_choices() is None
    assert import_choices.imported_count(tmp_path, INSTANCE) == 2         # said no: still there
    assert "Remove the imported choices for 2 mods?" in [t for k, t in boxes.said if k == "question"][-1]

    boxes.answer = boxes.StandardButton.Yes
    assert window.remove_imported_choices() == 2
    assert import_choices.imported_count(tmp_path, INSTANCE) == 0
    window._update_import_actions()
    assert not window.remove_imported_action.isEnabled()
    assert len(window.snapshot.events) == 1 and window.snapshot.events[0]["mod"]["name"] == "Grass Patches"   # own record untouched
    window.deleteLater()


def test_a_choices_file_for_another_game_is_refused_with_a_message(qt, tmp_path, monkeypatch):
    from fomod_logger_support.core import import_choices
    from test_export_choices import INSTANCE, grass_event
    from test_import_choices import choices, entry

    window, boxes = _choices_window(qt, tmp_path, monkeypatch, grass_event())
    data = choices(entry("Alpha", nexus_id=5))
    data["source"] = {"game": "Fallout 4", "nexus_domain": "fallout4", "instance_label": "x"}
    path = tmp_path / "fo4.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert window.import_choices_file(str(path)) is None
    kind, text = boxes.said[-1]
    assert kind == "warning" and "Fallout 4" in text and "Skyrim Special Edition" in text
    assert import_choices.imported_count(tmp_path, INSTANCE) == 0
    window.deleteLater()


# ---------- which option installed a file ----------

PEAK = "Textures/Landscape/Mountains/Peak.dds"


def _mountains(installation_event):
    from test_file_origin import mountains_event

    return mountains_event(installation_event)


def _searched(qt, tmp_path, installation_event, query, *others):
    window = _window(qt, _store(tmp_path, _mountains(installation_event), *others))
    window.search.setText(query)
    window._apply_search()
    return window


def _row(window, text: str):
    return next(item for item in window._items() if item.text(0) == text)


def test_model_lists_the_options_that_install_the_searched_file(tmp_path, installation_event):
    event = _mountains(installation_event)
    database = _store(tmp_path, event, installation_event)
    snapshot = model.load(database, model.ViewState(query=PEAK))
    assert [e["mod"]["name"] for e in snapshot.events] == ["Majestic Mountains"]
    hits = snapshot.file_hits
    assert {option_id for (event_id, option_id) in hits if event_id == event["event_id"]} == {
        "step:0/group:0/option:0", "step:0/group:0/option:1", "step:0/group:0/option:2",   # the folders
        "step:0/group:1/option:0", "step:0/group:1/option:1"}                              # the two files
    assert hits[(event["event_id"], "step:0/group:1/option:0")]["winner"] is True
    assert model.match_count(snapshot.events[0], PEAK, hits) == 5
    # A word is a name search and reads no file data.
    assert model.load(database, model.ViewState(query="mountains")).file_hits == {}
    assert model.load(database, model.ViewState(query="Meshes/none.nif")).events == []


def test_the_search_box_says_it_searches_files(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    assert window.search.placeholderText() == "Search mods, pages, options and files…"
    window.deleteLater()


def test_searching_a_file_shows_the_options_that_install_it(qt, tmp_path, installation_event):
    window = _searched(qt, tmp_path, installation_event, PEAK, installation_event)
    assert window.summary_label.text().startswith("1 mod match")
    top = window.tree.topLevelItem(0)
    assert top.text(0) == "Majestic Mountains" and "5 matches" in top.text(1)
    assert top.isExpanded()
    snow, darker = _row(window, "✓  Snow Peak Fix"), _row(window, "✓  Darker Peaks")
    assert snow.text(1).startswith(f"installs {PEAK}") and "wins" in snow.text(1)
    assert darker.text(1).startswith(f"installs {PEAK}") and "overwritten" in darker.text(1)
    assert "likely winner" in snow.toolTip(0)
    assert "overwritten by" in darker.toolTip(0)
    # The resolution folders that land on Textures/ may hold it too; options that can't install it are not listed.
    assert _row(window, "✓  2K").text(1).startswith("installs Textures/…")
    # A folder might not hold the file, so it is never marked as winning or overwritten.
    assert not any(tag in _row(window, label).text(1) for label in ("✓  2K", "○  1K", "○  4K")
                   for tag in ("wins", "overwritten"))
    assert "may also write it" in _row(window, "✓  2K").toolTip(0)
    assert _row(window, "○  1K").text(1).startswith("installs Textures/…")
    window.deleteLater()


def test_a_plugin_name_search_finds_the_option_that_lists_it(qt, tmp_path, installation_event):
    window = _searched(qt, tmp_path, installation_event, "majestic mountains.esp")
    two = _row(window, "✓  2K")
    assert two.text(1).startswith("installs Majestic Mountains.esp")
    assert "○  1K" not in [item.text(0) for item in window._items()]
    window.deleteLater()


def test_a_word_search_still_matches_names_and_marks_no_file(qt, tmp_path, installation_event):
    window = _searched(qt, tmp_path, installation_event, "darker")
    assert "installs" not in _row(window, "✓  Darker Peaks").text(1)
    window.deleteLater()


def test_a_long_path_is_shortened_from_the_left_in_the_status(qt, tmp_path, installation_event):
    from test_file_origin import entry, make, opt

    long_path = "Textures/" + "Deeply/Nested/" * 6 + "texture_d.dds"
    event = make(installation_event, "Deep", [("Main", [opt("Pack", True, [entry("x", long_path)])])])
    window = _window(qt, _store(tmp_path, event))
    window.search.setText(long_path)
    window._apply_search()
    status = _row(window, "✓  Pack").text(1)
    assert status.startswith("installs …") and status.endswith("texture_d.dds") and len(status) < len(long_path)
    assert long_path in _row(window, "✓  Pack").toolTip(0)
    window.deleteLater()


def test_an_automatic_install_that_installs_the_file_is_listed_under_the_install(qt, tmp_path, installation_event):
    from test_file_origin import entry, make, opt

    event = make(installation_event, "Auto", [("Main", [opt("Core", True, [])])],
                 auto=[{"id": "conditional:0", "installed": True, "plugins": [], "detects": [],
                        "files": [entry("p.dds", "Textures/auto.dds", order=3)]}])
    window = _window(qt, _store(tmp_path, event))
    window.search.setText("Textures/auto.dds")
    window._apply_search()
    assert window.tree.topLevelItem(0).text(0) == "Auto"
    assert "1 match" in window.tree.topLevelItem(0).text(1)
    assert "Installed automatically by the installer" in [item.text(0) for item in window._items()]
    row = _row(window, "✓  Automatic install 1")
    assert row.text(1).startswith("installs Textures/auto.dds")
    window.deleteLater()


def test_each_option_with_file_data_has_a_collapsed_files_row_made_when_opened(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, _mountains(installation_event), installation_event))
    texts = _texts(window)
    assert "Files (1)" in texts and "Files (2)" in texts            # 1K / 2K ... and the two single-file fixes
    holder = next(item for item in window._items() if item.text(0) == "Files (2)")
    assert not holder.isExpanded()
    assert [holder.child(i).text(0) for i in range(holder.childCount())] == ["…"]   # nothing built yet
    holder.setExpanded(True)                                                          # as a click does
    rows = [(holder.child(i).text(0), holder.child(i).text(1)) for i in range(holder.childCount())]
    assert rows == [("Textures/…", "folder"), ("Majestic Mountains.esp", "")]
    window.deleteLater()


def test_files_rows_show_folders_with_a_trailing_ellipsis_priority_and_a_cut_list(qt, tmp_path, installation_event):
    from test_file_origin import entry, make, opt

    event = make(installation_event, "Layout", [("Main", [
        opt("Everything", True, [entry("Data", "", folder=True, order=1),
                                  entry("Fix\\peak.dds", "Textures\\peak.dds", order=2, priority=4)],
            truncated=True),
        opt("Nothing", False, [])])])
    window = _window(qt, _store(tmp_path, event))
    _texts(window)
    holders = [item for item in window._items() if item.text(0).startswith("Files (")]
    assert [h.text(0) for h in holders] == ["Files (2+)"]            # an option that installs nothing has no row
    holders[0].setExpanded(True)
    rows = [(holders[0].child(i).text(0), holders[0].child(i).text(1)) for i in range(holders[0].childCount())]
    assert rows == [("(the mod folder)/…", "folder"), ("Textures/peak.dds", "priority 4")]
    assert "From Fix\\peak.dds" in holders[0].child(1).toolTip(0)
    window.deleteLater()


def test_installs_logged_before_files_were_recorded_have_no_files_row(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    assert not [t for t in _texts(window) if t.startswith("Files (")]
    window.deleteLater()


def test_files_rows_are_not_made_by_expand_all_or_a_redraw(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, _mountains(installation_event)))
    _texts(window)
    holders = [item for item in window._items() if item.text(0).startswith("Files (")]
    assert holders and all(not h.isExpanded() and h.childCount() == 1 and h.child(0).text(0) == "…" for h in holders)
    window.deleteLater()


def test_the_more_menu_offers_which_option_installed(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    actions = [action.text() for action in window.findChildren(qt.QAction) if action.text()]
    assert "Which option installed…?" in actions
    window.deleteLater()


def test_the_dialog_answers_a_path_a_plugin_and_not_from_a_logged_installer(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, _mountains(installation_event)))
    dialog = window.which_option("Majestic Mountains.esp")
    assert dialog.isVisible() and dialog.windowTitle() == "Which option installed…?"
    assert len(dialog.results_text()) == 1
    assert "Majestic Mountains › Textures › Texture resolution › 2K (picked)" in dialog.results_text()[0]
    assert "installs Majestic Mountains.esp" in dialog.results_text()[0]
    assert dialog.message.text().startswith("1 installer option installs Majestic Mountains.esp")

    dialog.set_query("Textures\\Landscape\\Mountains\\Peak.dds")
    texts = dialog.results_text()
    assert len(texts) == 5 and dialog.message.text().startswith("5 installer options install ")
    assert "Snow Peak Fix (picked)" in texts[0] and "likely winner" in texts[0]
    assert "Darker Peaks (picked)" in texts[1] and "overwritten by" in texts[1]
    assert any("1K (not picked)" in text for text in texts)

    dialog.set_query("Meshes/Nothing/here.nif")
    assert dialog.results_text() == []
    assert "Not from a logged installer" in dialog.message.text()
    dialog.set_query("")
    assert "Nothing typed" in dialog.message.text()
    dialog.close()
    window.deleteLater()


def test_the_dialog_says_when_an_install_has_no_file_data(qt, tmp_path, installation_event):
    from test_file_origin import make, opt

    old = make(installation_event, "Old Mod", [("Main", [opt("Core", True, plugins=["Old Core.esp"])])])
    window = _window(qt, _store(tmp_path, old))
    dialog = window.which_option("Old Core.esp")
    assert len(dialog.results_text()) == 1
    assert "file data isn't recorded for this install" in dialog.results_text()[0]
    assert "only plugin names were checked" in dialog.notes.text()
    window.deleteLater()


def test_the_dialog_only_looks_at_each_mods_current_install(qt, tmp_path, installation_event):
    first = _mountains(installation_event)
    newer = deepcopy(first)
    newer["event_id"] = "99999999-0000-4000-8000-000000000000"
    newer["completed_at"] = "2026-10-01T12:00:00+00:00"
    newer["steps"][0]["groups"][0]["options"][1]["selected"] = False
    window = _window(qt, _store(tmp_path, first, newer))
    dialog = window.which_option("Majestic Mountains.esp")
    assert len(dialog.results_text()) == 1 and "2K (not picked)" in dialog.results_text()[0]
    window.deleteLater()


def test_double_clicking_an_answer_shows_the_option_in_the_list(qt, tmp_path, installation_event):
    event = _mountains(installation_event)
    window = _window(qt, _store(tmp_path, event))
    dialog = window.which_option("Majestic Mountains.esp")
    dialog._double_clicked(dialog.results.item(0))
    current = window.tree.currentItem()
    assert current is not None and current.text(0) == "✓  2K"
    # An install the list isn't showing is said, not raised.
    window.state.query = "nothing is called this"
    window.refresh(force=True)
    dialog._double_clicked(dialog.results.item(0))
    assert "isn't in the list" in dialog.notes.text()
    window.deleteLater()


def test_an_open_dialog_follows_new_installs(qt, tmp_path, installation_event):
    from test_file_origin import entry, make, opt

    database = _store(tmp_path, _mountains(installation_event))
    window = _window(qt, database)
    dialog = window.which_option("Meshes/a.nif")
    assert dialog.results_text() == []
    with EventStore(database) as store:
        store.ingest(make(installation_event, "Late", [("Main", [opt("x", True, [entry("a", "Meshes/a.nif")])])]))
    window.refresh(force=True)
    assert len(dialog.results_text()) == 1 and "Late › Textures › Main › x (picked)" in dialog.results_text()[0]
    window.deleteLater()


def test_the_dialog_closes_with_the_window_and_is_reused(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, _mountains(installation_event)))
    first = window.which_option("a.dds")
    assert window.which_option("b.dds") is first and first.edit.text() == "b.dds"
    window.show()
    window.close()
    assert not first.isVisible()
    window.deleteLater()


def test_a_folder_never_makes_an_exact_file_overwritten_in_the_window(qt, tmp_path, installation_event):
    from test_file_origin import entry, make, opt

    event = make(installation_event, "Core and Extras", [("Main", [
        opt("Core", True, [entry("core\\x.dds", "textures/x.dds", order=1)]),
        opt("Extras", True, [entry("extras", "textures", folder=True, order=2)])])])
    window = _window(qt, _store(tmp_path, event))
    window.search.setText("textures/x.dds")
    window._apply_search()
    assert _row(window, "✓  Core").text(1) == "installs textures/x.dds"
    assert _row(window, "✓  Extras").text(1) == "installs textures/…"
    window.deleteLater()


def test_the_files_the_installer_always_installs_are_found_under_the_automatic_row(qt, tmp_path, installation_event):
    from test_file_origin import entry, make, opt

    event = make(installation_event, "Always", [("Main", [opt("Pick", True, [])])])
    event["required_files"] = {"files": [entry("core\\core.dds", "Textures/core.dds", order=1)]}
    window = _window(qt, _store(tmp_path, event))
    window.search.setText("textures/core.dds")
    window._apply_search()
    assert "Installed automatically by the installer" in [item.text(0) for item in window._items()]
    assert _row(window, "✓  Required by the installer").text(1).startswith("installs Textures/core.dds")
    dialog = window.which_option("Textures\\Core.dds")
    assert dialog.results_text()[0].startswith("Always › Required by the installer (always installed)")
    window.deleteLater()


def test_an_entry_outside_the_mod_folder_is_listed_as_such_in_the_files_row(qt, tmp_path, installation_event):
    from test_file_origin import entry, make, opt

    outside = {"source": "x", "destination": "..\\..\\x.dds", "resolved": "", "kind": "file", "order": 2,
               "escapes": True}
    event = make(installation_event, "Hostile", [("Main", [opt("Out", True, [entry("a", "tex/a.dds"), outside])])])
    window = _window(qt, _store(tmp_path, event))
    _texts(window)
    holder = next(item for item in window._items() if item.text(0).startswith("Files ("))
    holder.setExpanded(True)
    assert [holder.child(i).text(0) for i in range(holder.childCount())] == [
        "tex/a.dds", "(outside the mod folder) ..\\..\\x.dds"]
    window.deleteLater()


def test_the_window_keeps_one_file_index_across_searches_and_dialog_questions(qt, tmp_path, installation_event, monkeypatch):
    from fomod_logger_support.core.file_origin import FileIndex

    built = []
    real = FileIndex.__init__

    def counting(self, events):
        built.append(1)
        real(self, events)

    monkeypatch.setattr(FileIndex, "__init__", counting)
    monkeypatch.setattr(qt, "_SETTLED_SECONDS", -1.0)      # the database counts as untouched
    database = _store(tmp_path, _mountains(installation_event))
    window = _window(qt, database)
    for query in ("textures/landscape", "textures/landscape/mountains", "textures/landscape/mountains/peak.dds"):
        window.search.setText(query)
        window._apply_search()
    dialog = window.which_option("Majestic Mountains.esp")
    for query in ("Textures\\Landscape\\Mountains\\Peak.dds", "peak.dds", "Meshes/none.nif"):
        dialog.set_query(query)
    assert built == [1]
    # A new install changes the database, so the index is built again.
    from test_file_origin import entry, make, opt

    with EventStore(database) as store:
        store.ingest(make(installation_event, "Late", [("Main", [opt("x", True, [entry("a", "Meshes/a.nif")])])]))
    dialog.set_query("Meshes/a.nif")
    assert built == [1, 1] and len(dialog.results_text()) == 1
    window.deleteLater()


# ---------- compare choices ----------

B_INSTANCE = {"id": "b" * 64, "label": "Second list"}


def _db(tmp_path: Path, *events: dict) -> Path:
    (tmp_path / "db").mkdir(exist_ok=True)
    return _store(tmp_path / "db", *events)


def _variant(base, number, name, *, instance=None, archive="mm.7z", nexus=1234, picks=(True, False), day=26):
    """A copy of the fixture install: its two options picked as ``picks`` say, under another name or instance."""

    event = deepcopy(base)
    event["event_id"] = f"{number:08d}-1111-4111-8111-111111111111"
    event["completed_at"] = f"2026-08-{day:02d}T12:01:00+00:00"
    event["mod"].update(name=name, nexus_id=nexus)
    event["source"]["archive"] = archive
    if instance:
        event["instance"] = dict(instance)
    event["steps"][0]["groups"][0]["options"][0]["selected"] = picks[0]
    event["steps"][0]["groups"][1]["options"][0]["selected"] = picks[1]
    return event


def _two_lists(base):
    """The main list and a second one: one mod picked differently, one the same, one only in each; and two
    copies of Majestic Mountains in the main list."""

    return [
        _variant(base, 1, "Majestic Mountains - Dark", picks=(True, False)),
        _variant(base, 2, "Majestic Mountains - Light", picks=(False, True)),
        _variant(base, 3, "Lux", archive="lux.7z", nexus=88, picks=(True, True)),
        _variant(base, 4, "Main Only", archive="mo.7z", nexus=91),
        _variant(base, 5, "Lux", instance=B_INSTANCE, archive="lux.7z", nexus=88, picks=(True, True)),
        _variant(base, 6, "Majestic Mountains - Dark", instance=B_INSTANCE, picks=(False, False)),
        _variant(base, 7, "Second Only", instance=B_INSTANCE, archive="so.7z", nexus=92),
    ]


def _rows(dialog):
    dialog.expand_all()
    return dialog.rows()


def test_the_more_menu_offers_compare_choices(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    actions = [action.text() for action in window.findChildren(qt.QAction) if action.text()]
    assert "Compare choices…" in actions
    window.deleteLater()


def test_the_compare_dialog_says_when_there_is_nothing_to_compare(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event))
    dialog = window.compare_choices()
    assert not dialog.isModal() and dialog.kind == "" and dialog.kind_box.count() == 0
    assert dialog.summary_text().startswith("Nothing to compare yet")
    assert not dialog.a_box.isEnabled()
    window.close()
    assert not dialog.isVisible()
    window.deleteLater()


def test_two_instances_are_only_offered_when_the_database_has_two(qt, tmp_path, installation_event):
    one = _window(qt, _store(tmp_path, _variant(installation_event, 1, "Solo")))
    assert not one.compare_choices().available("instances")
    one.deleteLater()
    window = _window(qt, _db(tmp_path, *_two_lists(installation_event)),
                     instance_id=lambda: installation_event["instance"]["id"])
    dialog = window.compare_choices()
    assert dialog.available("instances") and dialog.kind_box.itemData(0) == "instances"
    assert [dialog.kind_box.itemText(i) for i in range(dialog.kind_box.count())] == [
        "Two MO2 instances", "Two copies of a mod"]
    window.deleteLater()


def test_the_dialog_compares_two_instances_mod_by_mod_with_words_and_hides_what_is_the_same(qt, tmp_path, installation_event):
    window = _window(qt, _db(tmp_path, *_two_lists(installation_event)),
                     instance_id=lambda: installation_event["instance"]["id"])
    dialog = window.compare_choices("instances", installation_event["instance"]["id"], B_INSTANCE["id"])
    assert dialog.a_box.currentText().endswith("(this instance)") and dialog.b_box.currentText() == "Second list"
    assert dialog.summary_text() == "5 mods: 1 with different picks, 2 only in A, 1 only in B, 1 identical."
    assert "A = MO2 Test (this instance)" in dialog.legend.text()
    rows = _rows(dialog)
    names = [row[1] for row in rows if row[0] == 0]
    assert names == ["Majestic Mountains - Dark", "Main Only", "Majestic Mountains - Light", "Second Only"]   # Lux: same, hidden
    mod = next(row for row in rows if row[1] == "Majestic Mountains - Dark")
    assert mod[4] == "different" and mod[2].startswith("1.0 ·")
    assert next(row for row in rows if row[1] == "Main Only")[2:] == ("1.0 · Aug 26, 2026", "not installed", "only in A")
    assert next(row for row in rows if row[1] == "Second Only")[4] == "only in B"
    option = next(row for row in rows if row[0] == 3 and row[4] == "picked only in A")
    assert option[2:4] == ("✓ picked", "○ not picked")
    dialog.identical_box.setChecked(True)
    names = [row[1] for row in _rows(dialog) if row[0] == 0]
    assert names == ["Majestic Mountains - Dark", "Main Only", "Majestic Mountains - Light", "Second Only", "Lux"]
    assert next(row for row in dialog.rows() if row[1] == "Lux")[4] == "same"
    window.deleteLater()


def test_the_dialog_compares_two_copies_of_a_mod(qt, tmp_path, installation_event):
    events = _two_lists(installation_event)
    window = _window(qt, _db(tmp_path, *events))
    dialog = window.compare_choices("copies", events[0]["event_id"], events[1]["event_id"])
    assert dialog.kind == "copies" and dialog.scope_label.text() == "Copies of"
    assert "same archive and Nexus ID" in dialog.scope_box.currentText()
    assert dialog.summary_text().startswith("2 picks differ (1 only in A, 1 only in B)")
    assert "A = Majestic Mountains - Dark" in dialog.legend.text() and "B = Majestic Mountains - Light" in dialog.legend.text()
    rows = _rows(dialog)
    assert rows[0][1] == "Majestic Mountains - Dark  ↔  Majestic Mountains - Light" and rows[0][4] == "different"
    assert [row[4] for row in rows if row[0] == 1] == ["different"]                  # the page
    assert [(row[1], row[4]) for row in rows if row[0] == 3] == [
        ("Shared label", "picked only in A"), ("Shared label", "picked only in B")]
    dialog.a_box.setCurrentIndex(dialog.b_box.currentIndex())                       # the same copy twice
    assert dialog.summary_text() == "Pick two different ones to compare." and len(dialog.rows()) == 1
    window.deleteLater()


def test_the_dialog_compares_a_mod_with_its_older_install_and_identical_choices_say_so(qt, tmp_path, installation_event):
    older = _variant(installation_event, 1, "Lux", archive="lux.7z", day=20, picks=(True, False))
    newer = _variant(installation_event, 2, "Lux", archive="lux.7z", day=25, picks=(True, True))
    same = _variant(installation_event, 3, "Lux", archive="lux.7z", day=26, picks=(True, True))
    database = _db(tmp_path, older, newer)
    window = _window(qt, database)
    dialog = window.compare_choices("installs")
    assert dialog.kind == "installs" and "(2 installs)" in dialog.scope_box.currentText()
    assert "newest" in dialog.b_box.currentText() and "newest" not in dialog.a_box.currentText()   # older against newest
    assert dialog.summary_text().startswith("1 pick differ")
    assert [row[4] for row in _rows(dialog) if row[0] == 3] == ["picked only in B"]
    with EventStore(database) as store:
        store.ingest(same)
    window.refresh(force=True)
    dialog = window.compare_choices("installs", newer["event_id"], same["event_id"])
    assert dialog.summary_text() == "The choices are identical."
    assert dialog.tree.topLevelItemCount() == 1 and "Show identical" in dialog.tree.topLevelItem(0).text(0)
    dialog.identical_box.setChecked(True)
    assert any(row[1] == "Shared label" and row[4] == "same" for row in _rows(dialog))
    window.deleteLater()


def test_an_open_compare_dialog_follows_new_installs(qt, tmp_path, installation_event):
    database = _db(tmp_path, _variant(installation_event, 1, "Lux", archive="lux.7z", day=20))
    window = _window(qt, database)
    window.show()
    dialog = window.compare_choices()
    assert dialog.kind == ""
    with EventStore(database) as store:
        store.ingest(_variant(installation_event, 2, "Lux", archive="lux.7z", day=25, picks=(False, True)))
    window.refresh(force=True)
    assert dialog.kind == "installs" and dialog.summary_text().startswith("2 picks differ")
    window.close()
    window.deleteLater()


def test_the_compare_dialog_uses_the_windows_colours_and_survives_a_theme_switch(qt, tmp_path, installation_event):
    events = _two_lists(installation_event)
    window = _window(qt, _db(tmp_path, *events))
    dialog = window.compare_choices("copies", events[0]["event_id"], events[1]["event_id"])
    dialog.expand_all()
    item = next(i for i in dialog._items() if i.text(3) == "picked only in A")
    assert item.foreground(3).color() == window._colours["accent"]
    window.set_theme_mode("dark")
    dialog.reload()
    dialog.expand_all()
    item = next(i for i in dialog._items() if i.text(3) == "picked only in B")
    assert item.foreground(3).color() == window._colours["off"]
    window.deleteLater()


def _profile_folder(tmp_path, **profiles):
    root = tmp_path / "profiles"
    for name, text in profiles.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "modlist.txt").write_text(text, encoding="utf-8")
    return root


def _first_row(window, name):
    return next(window.tree.topLevelItem(i) for i in range(window.tree.topLevelItemCount())
                if window.tree.topLevelItem(i).text(0) == name)


def test_a_mod_row_says_which_profiles_enable_it_and_flags_a_mod_in_none(qt, tmp_path, installation_event):
    from fomod_logger_support.core.compare_choices import read_profiles

    events = [_variant(installation_event, 1, "In Both", archive="a.7z", nexus=1),
              _variant(installation_event, 2, "In One", archive="b.7z", nexus=2),
              _variant(installation_event, 3, "In None", archive="c.7z", nexus=3),
              _variant(installation_event, 4, "Elsewhere", instance=B_INSTANCE, archive="d.7z", nexus=4),
              _variant(installation_event, 5, "Gone", archive="e.7z", nexus=5)]
    database = _db(tmp_path, *events)
    with EventStore(database) as store:
        store.mark_removed(installation_event["instance"]["id"], "Gone", "2026-09-01T00:00:00+00:00")
    folder = _profile_folder(tmp_path, Main="+In Both\n+In One\n-In None\n", Test="+in both\n-In One\n")
    window = _window(qt, database, instance_id=lambda: installation_event["instance"]["id"],
                     profiles=lambda: read_profiles(folder))
    both, one, none = (_first_row(window, name) for name in ("In Both", "In One", "In None"))
    assert "Enabled in profiles: Main, Test" in both.toolTip(0) and "in no profile" not in both.text(1)
    assert one.toolTip(0).endswith("Enabled in profiles: Main") and "in no profile" not in one.text(1)
    assert "Not enabled in any profile." in none.toolTip(0) and "in no profile" in none.text(1)
    elsewhere = _first_row(window, "Elsewhere")
    assert "profile" not in elsewhere.toolTip(0) and "in no profile" not in elsewhere.text(1)   # another instance
    window.deleteLater()


def test_a_mod_row_makes_no_claim_about_profiles_when_none_can_be_read(qt, tmp_path, installation_event):
    window = _window(qt, _store(tmp_path, installation_event), profiles=lambda: {"profiles": [], "mods": {}})
    row = _first_row(window, "Fixture Mod")
    assert "profile" not in row.toolTip(0) and "in no profile" not in row.text(1)
    broken = _window(qt, _db(tmp_path, installation_event), profiles=lambda: 1 / 0)
    assert "profile" not in _first_row(broken, "Fixture Mod").toolTip(0)
    window.deleteLater()
    broken.deleteLater()


def test_a_profile_change_redraws_the_list_and_an_unchanged_one_does_not(qt, tmp_path, installation_event):
    from fomod_logger_support.core.compare_choices import read_profiles

    folder = _profile_folder(tmp_path, Main="+Other\n")
    window = _window(qt, _db(tmp_path, installation_event), instance_id=lambda: installation_event["instance"]["id"],
                     profiles=lambda: read_profiles(folder))
    assert "in no profile" in _first_row(window, "Fixture Mod").text(1)
    drawn = []
    real = window._render_tree
    window._render_tree = lambda: (drawn.append(1), real())[1]
    window.refresh()
    window.refresh()
    assert drawn == []                                          # nothing changed: nothing redrawn
    (folder / "Main" / "modlist.txt").write_text("+Other\n+Fixture Mod\n", encoding="utf-8")
    window.refresh()
    assert drawn == [1]
    row = _first_row(window, "Fixture Mod")
    assert "in no profile" not in row.text(1) and "Enabled in profiles: Main" in row.toolTip(0)
    window.deleteLater()
