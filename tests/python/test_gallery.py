"""The scenario gallery: generated installs drawn in every theme, then measured.

For each scenario and theme the window is laid out at 1500x760 (never shown) and checked for
contrast, cut-off text, overlapping widgets and the content it should show; ``huge`` and ``plain_many`` are also timed.
The same checks run in ``tests/gallery/render.py``, which also draws the pictures at more sizes
and scales. Like test_viewer.py these need MO2's bundled PyQt6 and are skipped without it.

Defects the gallery finds in the window itself that can't be fixed yet are listed in ``KNOWN_DEFECTS``: the
tests pass while a defect stays inside its recorded bounds, any other or worse finding fails, and one xfail
test reports what is still wrong. The list is empty now, so every finding fails a test.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/, so `gallery` imports

from gallery import checks, qtenv, scenarios  # noqa: E402
from fomod_logger_support.core.installer_changes import installer_changes  # noqa: E402
from fomod_logger_support.core.missed_patches import find_missed_patches  # noqa: E402
from fomod_logger_support.core.storage import EventStore  # noqa: E402

NAMES = list(scenarios.SCENARIOS)
KINDS = ("contrast", "cutoff", "overlap", "content")
WIDE = ("wide", 1500, 760)

# (what is wrong, check, which findings, the worst measurement still tolerated: a contrast ratio, never reached by 0)
# Empty: every defect the gallery found has been fixed. A defect that can't be fixed yet goes here with its bound,
# so the tests stay green while it is the only thing wrong, and anything new or worse still fails.
KNOWN_DEFECTS: list[tuple[str, str, Any, float]] = []


def _known(finding: checks.Finding) -> int | None:
    for index, (_name, kind, matches, floor) in enumerate(KNOWN_DEFECTS):
        if finding.kind == kind and matches(finding) and (kind != "contrast" or finding.ratio() >= floor):
            return index
    return None


# ---------- data that needs no Qt ----------

def test_scenarios_are_repeatable_and_hold_what_they_promise():
    for name in NAMES:
        first, second = scenarios.build(name), scenarios.build(name)
        assert first.events == second.events and first.reviews == second.reviews and first.load_order == second.load_order
    huge = scenarios.build("huge")
    assert len(huge.events) >= 2000
    assert len({e["mod"]["name"] for e in huge.events}) == 300
    many = scenarios.build("plain_many")
    assert len(many.events) >= 2800 and len({e["mod"]["name"] for e in many.events}) == 1100
    assert sum(1 for e in many.events if e.get("kind") == "plain") == 800
    assert all(e["plain"]["file_count"] == len(e["plain"]["files"]) for e in many.events if e.get("kind") == "plain")
    assert any(e["source"]["reinstallation"] for e in huge.events)
    names = {event["mod"]["name"] for event in scenarios.build("long_names").events}
    assert any(len(n) > 120 for n in names) and any("Ætherium – Dwemer ✦ Overhaul" in n for n in names)


@pytest.mark.parametrize("name", [n for n in NAMES if n not in ("huge", "plain_many", "history_huge")])
def test_scenario_cards_come_out_as_designed(name, tmp_path):
    scenario = scenarios.build(name)
    database = scenarios.materialize(scenario, tmp_path)
    with EventStore(database) as store:
        result = find_missed_patches(store.latest(""), scenario.load_order, store.reviews())
    states = Counter(item["state"] for item in result["missed"])
    for state, number in (scenario.expect.get("states") or {}).items():
        assert states[state] == number, (name, state, dict(states))
    assert states["missing"] >= scenario.expect.get("min_missing", 0)
    if name == "every_state":
        assert set(states) == {"missing", "missing-masters", "picked-disabled", "patch-disabled", "needs-disabled"}


@pytest.mark.parametrize("name", ["changed_installers", "every_state", "typical"])
def test_scenario_installer_changes_come_out_as_designed(name, tmp_path):
    scenario = scenarios.build(name)
    database = scenarios.materialize(scenario, tmp_path)
    with EventStore(database) as store:
        changes = installer_changes(store.history("", include_hidden=True), scenario.load_order)
        seen = store.changes_seen()
    assert len(changes) - len(seen & {c["event_id"] for c in changes}) == scenario.expect.get("changes", 0)
    assert len(seen) == scenario.expect.get("changes_seen", 0)
    if name == "changed_installers":
        patches = {(c["mod_name"], p["name"]): p["patch"] for c in changes for p in c["patches"]}
        # New patches for mods in the list: picked and skipped, one for a switched-off mod, and none for Keld-Nar.
        assert patches[("Cathedral Weathers and Seasons", "Lux patch")]["picked"] is True
        assert patches[("Cathedral Weathers and Seasons", "Falskaar patch")]["picked"] is False
        assert patches[("Obsidian Weathers and Seasons", "Wet and Cold patch")]["state"] == "needs-disabled"
        assert not any(name == "Keld-Nar" for _mod, name in patches)
        assert {c["mod_name"] for c in changes if c["renamed"]} == {
            "Cathedral Weathers and Seasons", "Skyrim Immersive Creatures"}
        assert {c["mod_name"] for c in changes if c["removed"]} == {"Lux Via", "Majestic Mountains"}
        assert "Lux" not in {c["mod_name"] for c in changes}  # updated, installer unchanged


# ---------- the window ----------

@pytest.fixture(scope="module")
def env():
    window_module = qtenv.load()
    if window_module is None:
        pytest.skip("needs MO2's bundled PyQt6 (set FOMOD_LOGGER_MO2_ROOT)")
    environment = checks.Env(window_module)
    yield environment
    environment.restore()


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    """Scenario databases, built once each and shared (the window only reads them)."""

    root = tmp_path_factory.mktemp("gallery")
    built: dict[str, tuple[scenarios.Scenario, Path]] = {}

    def get(name: str) -> tuple[scenarios.Scenario, Path]:
        if name not in built:
            scenario = scenarios.build(name)
            built[name] = (scenario, scenarios.materialize(scenario, root / name))
        return built[name]

    return get


@pytest.fixture(scope="module")
def measured(env, library):
    """Check results per (scenario, theme), each measured once."""

    results: dict[tuple[str, str], dict[str, list[checks.Finding]]] = {}

    def get(name: str, theme: str) -> dict[str, list[checks.Finding]]:
        if (name, theme) not in results:
            scenario, database = library(name)
            window = env.open(database, scenario, theme, WIDE[1], WIDE[2])
            results[(name, theme)] = checks.run_all(env, window, scenario)
            window.deleteLater()
        return results[(name, theme)]

    return get


# `huge` and `plain_many` take ~5 s to draw per theme (that slowness is itself reported below), so pytest checks
# them in the default sayngwyn theme, a light-palette and a dark theme only; render.py checks every theme.
PAIRS = [(name, theme) for name in NAMES for theme in checks.THEMES
         if name not in ("huge", "plain_many", "history_huge") or theme in ("sayngwyn", "auto", "dark")]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name,theme", PAIRS)
def test_window_checks(measured, name, theme, kind):
    findings = [f for f in measured(name, theme)[kind] if f.severity == "FAIL"]
    unexpected = [f for f in findings if _known(f) is None]
    assert not unexpected, f"{name}/{theme}: {len(unexpected)} new {kind} findings:\n" + "\n".join(
        f.line() for f, _count in checks.grouped(unexpected)[:12])


def test_known_window_defects_still_present(measured, env):
    """One xfail that lists what the gallery found wrong in the window itself (nothing to fix in the tests)."""

    tally: dict[int, list[checks.Finding]] = {}
    for name, theme in PAIRS:
        for findings in measured(name, theme).values():
            for finding in findings:
                index = _known(finding) if finding.severity == "FAIL" else None
                if index is not None:
                    tally.setdefault(index, []).append(finding)
    if not tally:
        return
    lines = []
    for index, findings in sorted(tally.items()):
        worst = min((f.ratio() for f in findings if f.kind == "contrast"), default=None)
        lines.append(f"{KNOWN_DEFECTS[index][0]}: {len(findings)} findings"
                     + (f", worst {worst:.2f}:1" if worst is not None else ""))
    pytest.xfail("window defects found by the gallery (see tests/gallery/render.py checks.txt): " + " | ".join(lines))


# ---------- the Compare choices dialog ----------

COMPARE = scenarios.build("compare_choices").compare
COMPARE_PAIRS = [(index, theme) for index in range(len(COMPARE)) for theme in checks.THEMES]


@pytest.fixture(scope="module")
def compare_measured(env, library):
    results: dict[tuple[int, str], dict[str, list[checks.Finding]]] = {}

    def get(index: int, theme: str) -> dict[str, list[checks.Finding]]:
        if (index, theme) not in results:
            scenario, database = library("compare_choices")
            window = env.open(database, scenario, theme, WIDE[1], WIDE[2])
            dialog = checks.open_compare(env, window, scenario, scenario.compare[index])
            results[(index, theme)] = checks.compare_all(env, dialog, scenario.compare[index])
            dialog.close()
            window.deleteLater()
        return results[(index, theme)]

    return get


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("index,theme", COMPARE_PAIRS)
def test_compare_dialog_checks(compare_measured, index, theme, kind):
    findings = [f for f in compare_measured(index, theme)[kind] if f.severity == "FAIL"]
    lines = [f.line() for f, _count in checks.grouped(findings)[:12]]
    assert not findings, f"{COMPARE[index]['kind']}/{theme}: {len(findings)} {kind} findings: " + " | ".join(lines)


def test_the_compare_dialog_checks_detect_a_planted_defect(env, library):
    scenario, database = library("compare_choices")
    window = env.open(database, scenario, "light", WIDE[1], WIDE[2])
    dialog = checks.open_compare(env, window, scenario, scenario.compare[0])
    assert not [f for kind in checks.compare_all(env, dialog, scenario.compare[0]).values() for f in kind]
    dialog.summary.setStyleSheet("color: #dddddd; background: #ffffff;")
    env.settle(dialog)
    assert any(f.ratio() < 2 for f in checks.contrast(env, dialog))
    dialog.summary.setStyleSheet("")
    dialog.summary.setText("A different sentence")
    assert any("summary" in f.widget for f in checks.compare_content(env, dialog, scenario.compare[0]))
    dialog.close()
    window.deleteLater()


# ---------- the checks themselves must be able to fail ----------

def test_the_checks_detect_a_planted_defect(env, library):
    scenario, database = library("typical")
    window = env.open(database, scenario, "light", 1500, 760)
    assert not [f for f in checks.overlap(env, window) if f.severity == "FAIL"]
    home = window.theme_button.pos()
    window.theme_button.move(window.findChild(env.W.QLabel, "title").pos())
    assert any("overlap" in f.line() for f in checks.overlap(env, window))
    window.theme_button.move(home)
    window.summary_label.setStyleSheet("color: #dddddd; background: #ffffff;")  # pale grey on white
    env.settle(window)
    assert any("chip" in f.widget and f.ratio() < 2 for f in checks.contrast(env, window))
    window.summary_label.setStyleSheet("")
    window.missed_chip.setFixedWidth(20)
    env.settle(window)
    assert any("possible missed patch" in f.widget for f in checks.cutoff(env, window))
    window.deleteLater()


def test_content_check_notices_a_wrong_count(env, library):
    scenario, database = library("typical")
    window = env.open(database, scenario, "light", 1500, 760)
    assert not checks.content(env, window, scenario)
    window.missed_count.setText("99")
    assert any("missed count" in f.widget for f in checks.content(env, window, scenario))
    window.deleteLater()


def test_the_history_page_checks_detect_planted_defects(env, library):
    scenario, database = library("history")
    window = env.open(database, scenario, "light", 1500, 760)
    assert window.page == "history"
    assert not [f for kind in checks.run_all(env, window, scenario).values() for f in kind if f.severity == "FAIL"]
    # A line of the timeline in pale grey on white, a button squeezed too narrow, a wrong count, a missing marker.
    first = window.history.items()[1]
    from PyQt6.QtGui import QBrush, QColor
    first.setForeground(0, QBrush(QColor("#dddddd")))
    env.settle(window)
    assert any("tree column 0" in f.widget and f.ratio() < 2 for f in checks.contrast(env, window))
    first.setForeground(0, QBrush(window._tokens.text))
    window.history.since_button.setFixedWidth(30)
    env.settle(window)
    assert any("since" in f.widget.lower() for f in checks.cutoff(env, window))
    window.history.since_button.setMaximumWidth(16777215)
    window.history.since_button.setMinimumWidth(0)
    window.history.summary.setText("999 changes")
    assert any("summary" in f.widget for f in checks.content(env, window, scenario))
    window.history.summary.setText("")
    window.history.profile_box.setFixedWidth(40)
    env.settle(window)
    assert any("QComboBox" in f.widget for f in checks.cutoff(env, window))
    window.deleteLater()


# ---------- speed ----------

@pytest.mark.parametrize("name", ["huge", "plain_many", "history_huge"])
def test_huge_list_refreshes_quickly(env, library, capsys, name):
    scenario, database = library(name)
    first, repeat = checks.timings(env, database, scenario)
    with capsys.disabled():
        print(f"\n[gallery] {name}: first refresh {first:.2f}s (limit 1.5), repeat refresh {repeat:.2f}s (limit 0.5)")
    assert first < 1.5, f"{name}: first refresh took {first:.2f}s"
    assert repeat < 0.5, f"{name}: repeat refresh took {repeat:.2f}s"
