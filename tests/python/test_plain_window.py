"""The window over mods that were not installed through a logged installer: their rows, files, search, the file
lookup, compare, the missing-master card and the quiet hint about mods that are not recorded yet. Like the other
window tests these borrow MO2's PyQt6 (see test_viewer.py) and show nothing."""

from __future__ import annotations

import json

from fomod_logger_support.core.storage import EventStore

from test_plain_records import INSTANCE, fomod_event, make_mod, plain_event, plugin_bytes
from test_plus_window import _Scanner
from test_viewer import _open_all, qt  # noqa: F401 - qt is the module's fixture


def _database(tmp_path, *events) -> object:
    with EventStore(tmp_path / "window.db") as store:
        for event in events:
            store.ingest(event)
    return tmp_path / "window.db"


def _window(qt, database, **kwargs):
    window = qt.LoggerWindow(database, **kwargs)
    window.refresh()
    return window


def _row(window, name: str):
    return next(item for item in window._items() if item.text(0) == name)


def _order(*active: str, **extra) -> dict:
    return {"instance_id": INSTANCE, "active_plugins": ["Skyrim.esm", *active], "base_plugins": ["Skyrim.esm"],
            "mods": ["Lantern"], "unchecked_plugins": [], "plugin_masters": {}, "disabled_mod_plugins": {}, **extra}


def test_a_mod_without_an_installer_has_a_quiet_row_with_its_version_archive_and_date(qt, tmp_path):
    window = _window(qt, _database(tmp_path, plain_event(tmp_path, version="2.3", archive="Lantern-Pack-1234-2-3.zip")))
    row = _row(window, "Lantern")
    assert row.text(1) == "no installer options  ·  Lantern-Pack-1234-2-3.zip"
    assert row.text(2).startswith("2.3 · ")
    tip = row.toolTip(0)
    assert "Lantern-Pack-1234-2-3.zip" in tip and "Not installed through a logged installer" in tip and "3 files recorded" in tip
    assert "Nexus ID 4242" in tip
    assert window.summary_label.text() == "1 mod logged · 1 without installer options"
    window.deleteLater()


def test_a_long_archive_name_is_cut_in_the_middle_on_the_row_and_whole_in_the_tooltip(qt, tmp_path):
    name = "A Very Long Archive Name For A Lantern Mod-12345-1-0-1699999999.7z"
    window = _window(qt, _database(tmp_path, plain_event(tmp_path, archive=name)))
    row = _row(window, "Lantern")
    shown = row.text(1).split("  ·  ")[1]
    assert len(shown) == 30 and "…" in shown and shown.endswith(name[-10:]) and name in row.toolTip(0)
    window.deleteLater()


def test_opening_a_plain_mod_lists_where_it_came_from_its_plugins_and_its_files(qt, tmp_path):
    window = _window(qt, _database(tmp_path, plain_event(tmp_path, archive="L.zip", version="1.0")))
    _open_all(window)
    texts = [item.text(0) for item in window._items()]
    assert "Not installed through a logged installer, so there are no options to show." in texts
    assert not any(t.startswith(("Version", "Archive", "Nexus")) for t in texts)       # the row and its tooltip say them
    plugin = next(item for item in window._items() if item.text(0) == "Plugin: Lantern.esp")
    assert plugin.text(1) == "needs Skyrim.esm"
    holder = next(item for item in window._items() if item.text(0).startswith("Files ("))
    assert holder.text(0) == "Files (3)" and holder.childCount() == 1                       # made when it is opened
    holder.setExpanded(True)
    assert [holder.child(n).text(0) for n in range(holder.childCount())] == [
        "Lantern.esp", "meshes/lamp.nif", "Textures/Lantern/lamp.dds"]
    window.deleteLater()


def test_a_cut_short_file_list_says_so_and_a_huge_one_is_shown_in_part(qt, tmp_path):
    files = {f"d/f{n:04}.txt": "x" for n in range(1_100)}
    window = _window(qt, _database(tmp_path, plain_event(tmp_path, files=files)))
    _open_all(window)
    holder = next(item for item in window._items() if item.text(0).startswith("Files ("))
    assert holder.text(0) == "Files (1,100)"
    holder.setExpanded(True)
    assert holder.childCount() == 1001 and holder.child(1000).text(0).startswith("…and 100 more files")
    window.deleteLater()


def test_the_search_box_finds_plain_mods_by_name_archive_plugin_and_file(qt, tmp_path):
    other = plain_event(tmp_path, name="Rugs", files={"Rugs.esp": plugin_bytes(), "textures/rug.dds": "x"}, archive="Rugs.zip")
    window = _window(qt, _database(tmp_path, plain_event(tmp_path), other))
    for query, expected in (("lant", ["Lantern"]), ("rugs.zip", ["Rugs"]), ("Rugs.esp", ["Rugs"]),
                            ("Textures/Lantern/lamp.dds", ["Lantern"]), ("lamp.dds", ["Lantern"])):
        window.state.query = query
        window._load(force=True)
        window._render()
        assert sorted(e["mod"]["name"] for e in window.snapshot.events) == expected, query
    window.deleteLater()


def test_a_file_search_opens_the_files_of_the_mod_it_found_and_marks_the_match(qt, tmp_path):
    window = _window(qt, _database(tmp_path, plain_event(tmp_path)))
    window.search.setText("Textures/Lantern/lamp.dds")
    window._apply_search()                              # what typing does once the box settles
    row = _row(window, "Lantern")
    assert "1 match" in row.text(1) and row.isExpanded()
    holder = next(item for item in window._items() if item.text(0).startswith("Files ("))
    assert holder.isExpanded()
    assert holder.child(0).text(0) == "Textures/Lantern/lamp.dds"                          # the match comes first
    assert holder.child(0).background(0).color() != holder.child(1).background(0).color()
    window.deleteLater()


def test_which_option_installed_answers_with_the_mod_and_says_it_had_no_installer(qt, tmp_path):
    window = _window(qt, _database(tmp_path, plain_event(tmp_path)), instance_id=lambda: INSTANCE)
    dialog = window.which_option("Textures/Lantern/lamp.dds")
    assert dialog.results_text()[0].startswith("Lantern (not installed through an installer)\nhas Textures/Lantern/lamp.dds")
    assert "not installed through an installer" in dialog.message.text() and "1 mod" in dialog.message.text()
    dialog.set_query("Nothing/Here.dds")
    assert "Not from a logged installer" in dialog.message.text()
    assert window._show_origin("00000000-0000-4000-8000-000000000000", "") is False     # an install that isn't listed is refused
    dialog.set_query("lamp.dds")
    assert window._show_origin(dialog.matches[0]["event_id"], dialog.matches[0]["option_id"]) is True
    assert window.tree.currentItem().text(0) == "Lantern"
    dialog.close()
    window.deleteLater()


def test_the_lookup_mixes_installer_options_and_plain_mods_in_one_answer(qt, tmp_path):
    folder = fomod_event(name="Mountains", archive="M.zip", number=7, plugin="Peak.esp")
    folder["steps"][0]["groups"][0]["options"][0]["files"] = [
        {"source": "Peak.esp", "destination": "", "resolved": "Peak.esp", "kind": "file", "order": 1}]
    plain = plain_event(tmp_path, name="Copy", files={"Peak.esp": plugin_bytes()}, archive="C.zip")
    window = _window(qt, _database(tmp_path, folder, plain), instance_id=lambda: INSTANCE)
    dialog = window.which_option("Peak.esp")
    assert len(dialog.matches) == 2 and "1 installer option installs it, 1 mod not installed" in dialog.message.text()
    dialog.close()
    window.deleteLater()


def test_a_plugin_on_without_its_master_is_a_missing_master_card_and_a_mark_on_the_row(qt, tmp_path):
    event = plain_event(tmp_path, files={"Lantern.esp": plugin_bytes("Skyrim.esm", "Needed.esm")})
    window = _window(qt, _database(tmp_path, event), load_order=lambda: _order("Lantern.esp"))
    row = _row(window, "Lantern")
    assert row.text(1).startswith("● 1 plugin can't load  ·  no installer options")
    assert window.masters_box.isVisibleTo(window) and window.masters_count.text() == "1"
    texts = [label.text() for label in window.masters_list.parentWidget().findChildren(qt.QLabel)]
    assert any("Lantern.esp is switched on, but it needs Needed.esm, which isn't in your list" in t for t in texts)
    assert any("switch the plugin off" in t for t in texts)
    assert not window.missed_box.isVisibleTo(window)          # not counted as a logged-before-patch-checking mod
    window.deleteLater()


def test_two_installs_of_a_plain_mod_compare_by_version_archive_plugins_and_files(qt, tmp_path):
    old = plain_event(tmp_path, version="1.0", archive="L-1.zip", files={"a.txt": "x", "same.txt": "x", "Old.esp": plugin_bytes()})
    new = plain_event(tmp_path, version="1.1", archive="L-2.zip", when="2026-10-22T12:00:00.000Z",
                      files={"b.txt": "x", "same.txt": "x", "New.esp": plugin_bytes()})
    window = _window(qt, _database(tmp_path, old, new))
    dialog = window.compare_choices("installs", old["event_id"], new["event_id"], show=False)
    assert dialog.summary_text() == "These differ: the version (1.0 → 1.1), the archive, the plugins and the files."
    dialog.expand_all()
    rows = {name: (a, b, difference) for _depth, name, a, b, difference in dialog.rows()}
    assert rows["Version"] == ("1.0", "1.1", "different") and rows["Archive"] == ("L-1.zip", "L-2.zip", "different")
    assert rows["Files"] == ("3", "3", "different") and rows["Plugins"][2] == "different"
    names = {name: difference for _depth, name, _a, _b, difference in dialog.rows()}
    assert names["a.txt"] == "only in A" and names["b.txt"] == "only in B" and names["Old.esp"] == "only in A"
    assert "same.txt" not in names                          # what is the same is left out until asked for
    dialog.close()
    window.deleteLater()


def test_a_plain_install_against_an_installer_install_in_the_compare_dialog(qt, tmp_path):
    fomod = fomod_event(archive="L-1.zip", completed="2026-10-01T12:00:00+00:00")
    plain = plain_event(tmp_path, archive="L-2.zip", when="2026-10-22T12:00:00.000Z")
    window = _window(qt, _database(tmp_path, fomod, plain))
    dialog = window.compare_choices("installs", fomod["event_id"], plain["event_id"], show=False)
    assert dialog.summary_text().startswith("One was installed through an installer and the other was not")
    dialog.expand_all()
    rows = {name: (a, b) for _depth, name, a, b, _d in dialog.rows()}
    assert rows["Installer"] == ("an installer", "none")
    dialog.close()
    window.deleteLater()


# ---------- the hint about mods that are not recorded ----------

def test_the_hint_appears_when_mods_are_unrecorded_and_goes_when_they_are_scanned(qt, tmp_path):
    scanner = _Scanner(count=3)
    window = _window(qt, _database(tmp_path, plain_event(tmp_path)), scanner=scanner, geometry_file=tmp_path / "window.json")
    window._update_scan_hint()
    assert window.scan_hint.isVisibleTo(window)
    assert window.scan_hint_label.text().startswith("3 mods are in your list but not recorded here yet.")
    assert not any(isinstance(w, qt.QMessageBox) for w in window.findChildren(qt.QMessageBox))     # a line, not a dialog
    scanner.found = 0
    window._scan_finished(None)
    assert not window.scan_hint.isVisibleTo(window)
    scanner.found = 1
    window._update_scan_hint()
    assert window.scan_hint_label.text().startswith("1 mod is in your list")
    window.deleteLater()


def test_not_now_hides_the_hint_for_the_session_and_dont_show_again_for_good(qt, tmp_path):
    scanner = _Scanner(count=2)
    saved = tmp_path / "window.json"
    window = _window(qt, _database(tmp_path, plain_event(tmp_path)), scanner=scanner, geometry_file=saved)
    window._update_scan_hint()
    assert [b.text() for b in (window.scan_hint_scan, window.scan_hint_later, window.scan_hint_never)] == [
        "Scan now", "Not now", "Don't show again"]
    window.scan_hint_later.click()
    assert not window.scan_hint.isVisibleTo(window) and (not saved.exists() or "scan_hint_dismissed" not in saved.read_text(encoding="utf-8"))   # nothing remembered
    window._update_scan_hint()
    assert not window.scan_hint.isVisibleTo(window)                                  # not again in this session
    window.deleteLater()
    next_session = _window(qt, _database(tmp_path), scanner=scanner, geometry_file=saved)
    next_session._update_scan_hint()
    assert next_session.scan_hint.isVisibleTo(next_session)                          # it comes back when MO2 starts again
    next_session.scan_hint_never.click()
    assert not next_session.scan_hint.isVisibleTo(next_session)
    assert json.loads(saved.read_text(encoding="utf-8"))["scan_hint_dismissed"] is True
    next_session.deleteLater()
    after = _window(qt, _database(tmp_path), scanner=scanner, geometry_file=saved)
    after._update_scan_hint()
    assert not after.scan_hint.isVisibleTo(after) and after.scan_action is not None   # for good; the menu still scans
    after.deleteLater()


def test_the_hint_button_starts_the_scan_and_a_scanner_that_fails_hides_the_hint(qt, tmp_path):
    scanner = _Scanner(count=2)
    window = _window(qt, _database(tmp_path, plain_event(tmp_path)), scanner=scanner, geometry_file=tmp_path / "window.json")
    window._update_scan_hint()
    window.scan_hint_scan.click()
    assert scanner.started is not None and window._scan_dialog is not None and window._scan_dialog.running
    window._scan_dialog.stop()

    def broken() -> int:
        raise RuntimeError("MO2 is busy")

    scanner.count = broken
    window._update_scan_hint()
    assert not window.scan_hint.isVisibleTo(window)
    window.deleteLater()
    quiet = _window(qt, _database(tmp_path))                              # no scanner: no hint at all
    quiet._update_scan_hint()
    assert not quiet.scan_hint.isVisibleTo(quiet)
    quiet.deleteLater()


def test_the_scan_dialog_says_what_each_edition_records(qt, tmp_path):
    full = _window(qt, _database(tmp_path), scanner=_Scanner(count=1))
    dialog = full.scan_existing(show=False)
    text = " ".join(label.text() for label in dialog.findChildren(qt.QLabel))
    assert "version, archive, files and plugins" in text and "FOMOD Plus" not in text
    dialog.stop()
    plus = _window(qt, _database(tmp_path), scanner=_Scanner(count=1), edition_label="for FOMOD Plus")
    dialog = plus.scan_existing(show=False)
    assert "Mods FOMOD Plus installed get their choices rebuilt" in " ".join(l.text() for l in dialog.findChildren(qt.QLabel))
    dialog.stop()
    full.deleteLater()
    plus.deleteLater()


def test_an_empty_list_points_at_the_scan(qt, tmp_path):
    window = _window(qt, _database(tmp_path))
    # The sayngwyn theme says it in the branded first-run message; the other themes in a row.
    assert window.empty_state.isVisibleTo(window) and "Scan existing mods…" in window.empty_state.guide.text()
    window.set_theme_mode("light")
    assert any("Scan existing mods…" in item.text(0) for item in window._items())
    window.deleteLater()


def test_the_exports_leave_mods_without_an_installer_out(qt, tmp_path):
    window = _window(qt, _database(tmp_path, plain_event(tmp_path)), instance_id=lambda: INSTANCE)
    assert window.export_choices_file(str(tmp_path / "choices.json"), quiet=True) is None        # nothing to export
    window.deleteLater()
