"""The "List history" page of the window: the switch between pages, the timeline by day, the markers for programs, the
filters (kinds, search, profile, "since the last program ran"), paging, the export, and keeping fast with a full history."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

from fomod_logger_support.core import history
from fomod_logger_support.core.storage import EventStore
from test_viewer import qt  # noqa: F401 - the module fixture that borrows MO2's PyQt6

INSTANCE = "d" * 64
OTHER = "e" * 64


def stamp(day: int, hour: int, minute: int = 0) -> str:
    return f"2026-10-{day:02d}T{hour:02d}:{minute:02d}:00.000Z"


def entry(kind: str, *names: str, **extra):
    return {"kind": kind, "count": len(names) or 1, "items": [{"name": name} for name in names], **extra}


def build(tmp_path: Path, items=None, runs=()) -> Path:
    """A database with entries: ``items`` are (profile, at, entry); ``runs`` (profile, at, ended, exit code, program)."""

    database = tmp_path / "logger.sqlite3"
    with EventStore(database) as store:
        for profile, at, one in items or []:
            store.history_add(INSTANCE, profile, [one], at=at)
        for profile, at, ended, code, name in runs:
            identity = store.history_start_run(INSTANCE, profile, name, rf"C:\Game\{name}.exe", at=at)
            if ended:
                store.history_finish_run(identity, code, at=ended)
    return database


def busy(tmp_path: Path) -> Path:
    return build(tmp_path, [
        ("Default", stamp(18, 10), entry(history.MOD_ENABLED, "Lux")),
        ("Default", stamp(18, 11), entry(history.PLUGIN_MOVED, "a.esp", "b.esp", "c.esp")),
        ("Hardcore", stamp(18, 12), entry(history.MOD_DISABLED, "Frost")),
        ("", stamp(19, 9), entry(history.MOD_INSTALLED, "Cathedral Weathers")),
        ("Default", stamp(19, 21), entry(history.PLUGIN_DISABLED, "lux.esp")),
        ("Default", stamp(20, 8), entry(history.MOD_UPDATED, "Lux")),
    ], runs=[("Default", stamp(19, 18), stamp(19, 20), 0, "SkyrimSE"), ("Default", stamp(20, 12), stamp(20, 13), 3221225477, "SSEEdit")])


def window_for(qt, database, *, profile="Default", instance=INSTANCE, **kwargs):
    window = qt.LoggerWindow(database, instance_id=lambda: instance, current_profile=lambda: profile, **kwargs)
    window.refresh()
    window.show_page("history")
    return window


def lines(window):
    """(text, detail, time) of each entry line, newest first."""

    view = window.history
    return [(item.text(0), item.text(1), item.text(2)) for item in view.items()
            if (item.data(0, qt_role()) or ("",))[0] == "entry"]


def qt_role():
    from PyQt6.QtCore import Qt
    return Qt.ItemDataRole.UserRole


def titles(window):
    return [text for text, _detail, _time in lines(window)]


# ---------- the two pages ----------

def test_a_segment_switches_between_the_installers_and_the_history(qt, tmp_path):
    window = qt.LoggerWindow(busy(tmp_path), instance_id=lambda: INSTANCE, current_profile=lambda: "Default")
    window.refresh()
    assert window.page == "installers" and window.page_button["installers"].isChecked()
    assert window.installer_chips.isVisibleTo(window) and not window.history_chips.isVisibleTo(window)
    window.page_button["history"].click()
    assert window.page == "history" and window.page_button["history"].isChecked() and not window.page_button["installers"].isChecked()
    assert window.pages.currentWidget() is window.history
    assert not window.installer_chips.isVisibleTo(window) and window.history_chips.isVisibleTo(window)
    window.show_page("nonsense")
    assert window.page == "history"
    window.page_button["installers"].click()
    assert window.page == "installers" and window.pages.currentWidget() is window.installers_page
    assert window.page_button["installers"].isChecked()


def test_the_history_page_does_not_make_the_installers_page_read_everything_again(qt, tmp_path):
    window = qt.LoggerWindow(busy(tmp_path), instance_id=lambda: INSTANCE, current_profile=lambda: "Default")
    window.refresh()
    serial = window._serial
    window.show_page("history")
    window.refresh(force=True)
    window.schedule_refresh()
    assert window._serial == serial                  # the installers' snapshot was not read for the history
    window.show_page("installers")
    assert window.page == "installers" and not window.snapshot.error and len(window.snapshot.instances) == 0


def test_the_more_menu_can_export_the_list_history(qt, tmp_path):
    window = qt.LoggerWindow(busy(tmp_path))
    texts = [action.text() for button in window.findChildren(qt.QToolButton) if button.menu() for action in button.menu().actions()]
    assert "Export list history…" in texts and "Export history…" in texts


# ---------- the timeline ----------

def test_entries_are_listed_newest_first_under_their_day_and_the_profile_shows_its_own_and_the_instances(qt, tmp_path):
    window = window_for(qt, busy(tmp_path))
    view = window.history
    days = [view.tree.topLevelItem(i).text(0) for i in range(view.tree.topLevelItemCount())]
    expected = [history.day_label(day, date.today()) for day, _group in history.by_day(view.entries)]
    assert days == expected and len(days) in (3, 4)      # by the local day, so a far-east time zone may split one
    shown = titles(window)
    assert shown[0].startswith("SSEEdit ran") and "Updated Lux" in shown[1]
    assert "Switched off Frost" not in shown                       # Hardcore's, not this profile's
    assert "Installed Cathedral Weathers" in shown                 # the instance's own
    assert view.total == 7 and window.history_chip.text() == "7 changes"
    assert view.summary.text().startswith("7 changes in profile Default")


def test_a_program_that_ran_is_a_bold_marker_that_says_how_it_ended(qt, tmp_path):
    window = window_for(qt, busy(tmp_path))
    markers = [item for item in window.history.items() if item.font(0).bold() and item.parent() is not None
               and item.parent().parent() is None]
    assert [item.text(0).split(" ran ")[0] for item in markers] == ["SSEEdit", "SkyrimSE"]
    assert markers[1].text(0).endswith("exited normally") and markers[0].text(0).endswith("exited with code 0xC0000005")
    assert markers[0].toolTip(0).splitlines()[-1] == r"C:\Game\SSEEdit.exe"
    from PyQt6.QtCore import Qt
    assert markers[0].background(0).style() == Qt.BrushStyle.SolidPattern


def test_a_grouped_entry_opens_to_its_items_which_are_made_when_it_is_opened(qt, tmp_path):
    window = window_for(qt, busy(tmp_path))
    view = window.history
    [group] = [item for item in view.items() if item.text(0) == "3 plugins moved"]
    assert group.childCount() == 1 and group.child(0).text(0) == "…"            # a placeholder until it is opened
    group.setExpanded(True)
    assert [group.child(n).text(0) for n in range(group.childCount())] == ["a.esp", "b.esp", "c.esp"]
    group.setExpanded(False)
    group.setExpanded(True)
    assert group.childCount() == 3


def test_changes_found_at_start_say_so_in_the_line_and_the_tooltip(qt, tmp_path):
    database = tmp_path / "logger.sqlite3"
    with EventStore(database) as store:
        store.history_add(INSTANCE, "Default", [entry(history.MOD_DISABLED, "Lux")], at=stamp(20, 9), offline=True)
    window = window_for(qt, database)
    [(text, detail, _time)] = lines(window)
    assert text == "Switched off Lux" and detail == "found at MO2 start"
    item = [i for i in window.history.items() if i.text(0) == text][0]
    assert "wasn't watching" in item.toolTip(0)


def test_an_empty_history_says_what_it_will_hold_and_an_unknown_instance_asks_which(qt, tmp_path):
    window = window_for(qt, build(tmp_path))
    assert window.history.empty.isVisibleTo(window) and not window.history.tree.isVisibleTo(window)
    assert "No changes recorded yet" in window.history.empty.text() and "while MO2 was closed" in window.history.empty.text()
    assert not window.history.export_button.isEnabled() and not window.history.since_button.isEnabled()
    (tmp_path / "b").mkdir()
    lost = window_for(qt, build(tmp_path / "b"), instance="")
    assert "Choose an MO2 instance" in lost.history.empty.text()


def test_a_database_that_cannot_be_read_is_said_not_raised(qt, tmp_path):
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not a database")
    window = qt.LoggerWindow(broken, instance_id=lambda: INSTANCE)
    window.show_page("history")
    assert "Couldn't read the history" in window.history.empty.text()


# ---------- filters ----------

def test_each_kind_can_be_switched_off_and_back_on(qt, tmp_path):
    window = window_for(qt, busy(tmp_path))
    view = window.history
    view.kind_buttons["programs"].setChecked(False)
    assert not any(" ran " in text for text in titles(window)) and view.total == 5
    view.kind_buttons["installs"].setChecked(False)
    view.kind_buttons["plugins_on_off"].setChecked(False)
    assert sorted(titles(window)) == sorted(["Switched on Lux", "3 plugins moved"])
    view.kind_buttons["mods_on_off"].setChecked(False)
    view.kind_buttons["plugin_order"].setChecked(False)
    assert view.tree.topLevelItemCount() == 0 and "Nothing matches these filters" in view.empty.text()
    for button in view.kind_buttons.values():
        button.setChecked(True)
    assert view.total == 7


def test_the_search_finds_mods_plugins_and_programs_inside_groups_too(qt, tmp_path):
    window = window_for(qt, busy(tmp_path))
    view = window.history
    view.search.setText("B.ESP")
    view._search_timer.stop()
    view._search_changed()
    assert titles(window) == ["3 plugins moved"] and view.summary.text().startswith("1 change in profile Default")
    view.search.setText("sseedit")
    view._search_changed()
    assert len(titles(window)) == 1 and titles(window)[0].startswith("SSEEdit ran")
    view.search.setText("nothing like this")
    view._search_changed()
    assert view.tree.topLevelItemCount() == 0 and "Nothing matches" in view.empty.text()


def test_since_the_last_program_ran_is_one_click_and_counts_from_that_program(qt, tmp_path):
    window = window_for(qt, busy(tmp_path))
    view = window.history
    assert view.since_button.isEnabled() and not view.since_button.isChecked() and view.total == 7
    view.since_button.click()
    # The last program started Oct 20, 12:00 (SSEEdit): only what happened after it counts.
    assert view.since_button.isChecked() and view.total == 0 and "Nothing matches" in view.empty.text()
    assert "since SSEEdit started" in view.summary.text()
    view.since_button.click()
    assert view.total == 7 and not view.since_button.isChecked()


def test_since_a_chosen_run_counts_from_that_run_and_the_baseline_is_the_one_asked_for(qt, tmp_path):
    window = window_for(qt, busy(tmp_path))
    view = window.history
    [skyrim] = [e for e in view.entries if e["subject"] == "SkyrimSE"]
    view.since_run(skyrim["id"])
    assert view.since_button.isChecked() and "since SkyrimSE started" in view.summary.text()
    after = titles(window)
    assert after[0].startswith("SSEEdit ran") and after[1:] == ["Updated Lux", "Plugin switched off: lux.esp"]
    view._since_clicked_off()
    assert view.total == 7 and view.since_entry is None


def test_the_since_button_is_off_while_no_program_has_ever_run(qt, tmp_path):
    window = window_for(qt, build(tmp_path, [("Default", stamp(20, 9), entry(history.MOD_ENABLED, "Lux"))]))
    view = window.history
    assert not view.since_button.isEnabled() and "No program has been started" in view.since_button.toolTip()


def test_the_profile_box_lists_the_profiles_and_starts_on_the_one_in_use(qt, tmp_path):
    window = window_for(qt, busy(tmp_path))
    view = window.history
    assert [view.profile_box.itemText(i) for i in range(view.profile_box.count())] == [
        "All profiles", "Profile: Default", "Profile: Hardcore"]
    assert view.profile_box.currentText() == "Profile: Default" and view.total == 7
    view.profile_box.setCurrentIndex(view.profile_box.findData("Hardcore"))
    assert titles(window) == ["Installed Cathedral Weathers", "Switched off Frost"] and view.total == 2
    view.profile_box.setCurrentIndex(0)
    assert view.total == 8 and view.profile is None
    (tmp_path / "x").mkdir()
    other = window_for(qt, busy(tmp_path / "x"), profile="Hardcore")
    assert other.history.profile_box.currentText() == "Profile: Hardcore"


def test_a_window_without_a_known_profile_shows_every_profile(qt, tmp_path):
    window = qt.LoggerWindow(busy(tmp_path), instance_id=lambda: INSTANCE)
    window.show_page("history")
    assert window.history.total == 8 and window.history.profile_box.currentText() == "All profiles"


def test_the_instance_chosen_in_the_window_decides_whose_history_is_shown(qt, tmp_path):
    database = busy(tmp_path)
    with EventStore(database) as store:
        store.history_add(OTHER, "Default", [entry(history.MOD_ENABLED, "Elsewhere")], at=stamp(20, 15))
    window = window_for(qt, database)
    assert "Elsewhere" not in str(titles(window))
    window.state.instance = OTHER
    window.history._fresh_filters()
    window.refresh()
    assert titles(window) == ["Switched on Elsewhere"]


# ---------- paging, speed, drawing ----------

def test_only_the_newest_page_is_drawn_and_show_more_adds_the_next(qt, tmp_path):
    database = tmp_path / "logger.sqlite3"
    with EventStore(database) as store:
        with store._connection:
            store._connection.executemany(
                "INSERT INTO history (instance_id, profile, at, kind, count, subject, names, detail, source) "
                "VALUES (?, 'Default', ?, 'mod_enabled', 1, ?, ?, ?, 'snapshot')",
                ((INSTANCE, history.shifted("2026-09-01T00:00:00.000Z", n * 60), f"Mod {n}", f"mod {n}",
                  f'{{"items":[{{"name":"Mod {n}"}}]}}') for n in range(600)))
    window = window_for(qt, database)
    view = window.history
    assert view.total == 600 and len(view.entries) == 250 and len(titles(window)) == 250
    assert titles(window)[0] == "Switched on Mod 599"
    assert view.more_button.isVisibleTo(window) and view.more_button.text().startswith("Show 250 older changes")
    view.more_button.click()
    assert len(titles(window)) == 500 and view.more_button.isVisibleTo(window) and "350 older" not in view.more_button.text()
    view.more_button.click()
    assert len(titles(window)) == 600 and not view.more_button.isVisibleTo(window)
    assert "newest" not in view.summary.text()
    view.search.setText("mod 59")
    view._search_changed()
    assert view.limit == 250 and len(titles(window)) == 11                      # a new search starts from the first page


def test_a_full_history_costs_the_window_no_more_than_a_short_one(qt, tmp_path):
    database = tmp_path / "logger.sqlite3"
    with EventStore(database) as store:
        with store._connection:
            store._connection.executemany(
                "INSERT INTO history (instance_id, profile, at, kind, count, subject, names, detail, source) "
                "VALUES (?, ?, ?, ?, 1, 'x', ?, '{\"items\":[{\"name\":\"x\"}]}', 'snapshot')",
                ((INSTANCE, "Default" if n % 3 else "Other", history.shifted("2024-01-01T00:00:00.000Z", n * 30),
                  ("mod_enabled", "plugin_moved", "mod_moved", "plugin_enabled")[n % 4], f"mod {n % 977}\nplugin{n % 911}.esp")
                 for n in range(history.KEEP_ENTRIES)))
    started = time.perf_counter()
    window = window_for(qt, database)
    first = time.perf_counter() - started
    view = window.history
    assert view.total == 33333 and len(view.entries) == 250
    started = time.perf_counter()
    view.reload()
    repeat = time.perf_counter() - started
    started = time.perf_counter()
    view.search.setText("plugin77")
    view._search_changed()
    searched = time.perf_counter() - started
    started = time.perf_counter()
    view.kind_buttons["mod_order"].setChecked(False)
    filtered = time.perf_counter() - started
    assert first < 1.5 and repeat < 0.05 and searched < 0.5 and filtered < 0.5, (first, repeat, searched, filtered)


def test_nothing_is_read_again_when_nothing_changed_and_a_write_is_noticed(qt, tmp_path):
    database = busy(tmp_path)
    window = window_for(qt, database)
    view = window.history
    time.sleep(0.01)
    assert view.reload() is False and view.reload(force=True) is True
    with EventStore(database) as store:
        store.history_add(INSTANCE, "Default", [entry(history.MOD_DISABLED, "Late")], at=stamp(21, 9))
    assert view.reload() is True and titles(window)[0] == "Switched off Late"
    window.set_theme_mode("dark")                      # a theme change draws the lines again
    assert titles(window)[0] == "Switched off Late"


# ---------- export ----------

def test_the_history_exports_as_markdown_or_plain_text_with_what_is_listed(qt, tmp_path):
    window = window_for(qt, busy(tmp_path))
    view = window.history
    markdown = tmp_path / "history.md"
    assert view.export(str(markdown), quiet=True) == 7
    text = markdown.read_text(encoding="utf-8")
    assert text.startswith("# Changes to the list · profile Default\n") and "## " in text and "**SSEEdit ran" in text
    assert "3 plugins moved" in text and "- a.esp" in text and "Switched off Frost" not in text
    plain = tmp_path / "history.txt"
    view.kind_buttons["programs"].setChecked(False)
    assert view.export(str(plain), quiet=True) == 5
    body = plain.read_text(encoding="utf-8")
    assert body.startswith("Changes to the list · profile Default · only installs and removals") and " ran " not in body and "#" not in body
    view.search.setText("lux")
    view._search_changed()
    assert view.export(str(tmp_path / "lux.md"), quiet=True) == 3
    assert "matching “lux”" in (tmp_path / "lux.md").read_text(encoding="utf-8")


def test_the_export_covers_every_entry_not_only_the_page_drawn_and_nothing_to_export_is_said(qt, tmp_path):
    database = tmp_path / "logger.sqlite3"
    with EventStore(database) as store:
        with store._connection:
            store._connection.executemany(
                "INSERT INTO history (instance_id, profile, at, kind, count, subject, names, detail, source) "
                "VALUES (?, 'Default', ?, 'mod_enabled', 1, ?, ?, ?, 'snapshot')",
                ((INSTANCE, history.shifted("2026-09-01T00:00:00.000Z", n * 60), f"Mod {n}", f"mod {n}",
                  f'{{"items":[{{"name":"Mod {n}"}}]}}') for n in range(400)))
    window = window_for(qt, database)
    assert len(window.history.entries) == 250
    assert window.history.export(str(tmp_path / "all.md"), quiet=True) == 400
    assert (tmp_path / "all.md").read_text(encoding="utf-8").count("Switched on Mod") == 400
    (tmp_path / "e").mkdir()
    empty = window_for(qt, build(tmp_path / "e"))
    told = []
    empty._tell = lambda kind, text: told.append((kind, text))
    assert empty.history.export() is None and told == [("information", "There is nothing in the history to export with these filters.")]
    assert empty.history.export(str(tmp_path / "none.md"), quiet=True) is None and not (tmp_path / "none.md").exists()


def test_every_theme_draws_the_page_and_the_markers_use_the_reminder_colour(qt, tmp_path):
    window = window_for(qt, busy(tmp_path))
    for mode in ("light", "dark", "auto"):
        window.set_theme_mode(mode)
        [marker] = [item for item in window.history.items() if item.text(0).startswith("SSEEdit ran")]
        assert marker.foreground(0).color() == window._tokens.mark and marker.font(0).bold()


def test_a_burst_that_keeps_the_count_but_changes_its_items_is_drawn_again(qt, tmp_path):
    database = build(tmp_path, [("Default", stamp(20, 9), entry(history.PLUGIN_MOVED, "a.esp", "b.esp"))])
    window = window_for(qt, database)
    [group] = [item for item in window.history.items() if item.text(0) == "2 plugins moved"]
    group.setExpanded(True)
    assert [group.child(n).text(0) for n in range(group.childCount())] == ["a.esp", "b.esp"]
    with EventStore(database) as store:      # a merge that swapped one item for another: same count, other items
        with store._connection:
            store._connection.execute(
                "UPDATE history SET detail = ?, names = ?", ('{"items":[{"name":"a.esp"},{"name":"c.esp"}]}', "a.esp\nc.esp"))
    assert window.history.reload() is True
    [group] = [item for item in window.history.items() if item.text(0) == "2 plugins moved"]
    group.setExpanded(True)
    assert [group.child(n).text(0) for n in range(group.childCount())] == ["a.esp", "c.esp"]


def test_entries_found_at_a_profile_switch_or_a_re_enable_are_labelled_by_why(qt, tmp_path):
    database = tmp_path / "logger.sqlite3"
    with EventStore(database) as store:
        for code, name in ((history.FOUND_START, "A"), (history.FOUND_PROFILE, "B"), (history.FOUND_RESTART, "C")):
            store.history_add(INSTANCE, "Default", [entry(history.MOD_DISABLED, name)], at=stamp(20, 9 + code), offline=code)
    window = window_for(qt, database)
    details = {text: detail for text, detail, _t in lines(window)}
    assert details == {"Switched off A": "found at MO2 start", "Switched off B": "found when this profile was opened",
                       "Switched off C": "found when Companion was switched on"}
    tips = {item.text(0): item.toolTip(0) for item in window.history.items() if item.text(0).startswith("Switched off")}
    assert "profile was opened" in tips["Switched off B"] and "switched on again" in tips["Switched off C"]


def test_exporting_before_the_page_was_ever_opened_uses_the_profile_in_use_like_the_page_does(qt, tmp_path):
    window = qt.LoggerWindow(busy(tmp_path), instance_id=lambda: INSTANCE, current_profile=lambda: "Default")
    assert window.history.entries == []                                # never loaded
    assert window.history.export(str(tmp_path / "early.md"), quiet=True) == 7          # Default's and the instance's, not Hardcore's
    assert "profile Default" in (tmp_path / "early.md").read_text(encoding="utf-8").splitlines()[0]
    window.show_page("history")
    assert window.history.total == 7
