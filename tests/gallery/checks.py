"""Automatic checks on a drawn Modlist Companion window.

Shared by ``tests/python/test_gallery.py`` (pass/fail) and ``render.py``
(the same results, written to checks.txt). Nothing here shows a window: the
window is laid out at a size, widgets are measured, and pictures come from
``QWidget.grab()``.

A ``Finding`` has a severity: ``FAIL`` is a real defect in the window,
``NOTE`` is something worth knowing (for example Qt's own eliding).
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import scenarios

THEMES = ("sayngwyn", "auto", "light", "dark", "auto-dark")
# (name, width, height, scale). Scale 1.25 and 1.5 are rendered by a Qt process
# started with QT_SCALE_FACTOR (see render.py); pytest runs scale 1.0 only.
SIZES = (("wide", 1500, 760, 1.0), ("narrow", 900, 700, 1.0),
         ("small-125", 1100, 650, 1.25), ("small-150", 1100, 650, 1.5))
NORMAL_RATIO = 4.5
LARGE_RATIO = 3.0


@dataclass(frozen=True)
class Finding:
    kind: str       # contrast, cutoff, overlap, content, note
    severity: str   # FAIL or NOTE
    widget: str
    detail: str

    def line(self) -> str:
        return f"[{self.severity}] {self.kind}: {self.widget}: {self.detail}"

    def group_key(self) -> tuple:
        """Findings that differ only in text or in a number of pixels are one defect."""

        style = self.widget.split(" '")[0]
        if self.kind == "contrast":  # the same text colour on the same kind of widget; backgrounds differ a little
            return (self.kind, self.severity, style, "text " + self.detail.split("text ")[-1].split(" on ")[0])
        detail = re.sub(r"\d+(px)?", "N", re.sub(r"'[^']*'", "'..'", self.detail))
        return (self.kind, self.severity, style, detail)

    def ratio(self) -> float:
        match = re.match(r"([\d.]+):1", self.detail)
        return float(match.group(1)) if match else 0.0


def grouped(findings: list[Finding]) -> list[tuple[Finding, int]]:
    """(first example, how many) for each distinct defect, in first-seen order."""

    groups: dict[tuple, list[Finding]] = {}
    for finding in findings:
        groups.setdefault(finding.group_key(), []).append(finding)
    return [(members[0], len(members)) for members in groups.values()]


# ---------- Qt setup ----------

class Env:
    """The Qt pieces the checks and the renderer need, plus the two app palettes."""

    def __init__(self, window_module):
        from PyQt6 import QtCore, QtGui, QtWidgets

        self.win = window_module
        self.Qt, self.Gui, self.W = QtCore, QtGui, QtWidgets
        self.app = QtWidgets.QApplication.instance()
        self.saved_style = self.app.style().objectName()
        self.saved_palette = QtGui.QPalette(self.app.palette())
        self.app.setStyle("Fusion")
        # Spelled out: Qt's own Fusion palette follows Windows' dark mode.
        self.light = self._palette(("#f0f0f0", "#000000", "#ffffff", "#f7f7f7", "#308cc6", "#ffffff", "#f0f0f0", "#a0a0a0"))
        self.dark = self._palette(("#353535", "#e0e0e0", "#232323", "#2c2c2c", "#2a82da", "#000000", "#404040", "#7a7a7a"))

    def _palette(self, colours):
        """A QPalette from (window, text, base, alternate, highlight, highlighted text, button, disabled text)."""

        P, C = self.Gui.QPalette, self.Gui.QColor
        window, text, base, alternate, highlight, highlighted, button, disabled = colours
        palette = P()
        for role, colour in ((P.ColorRole.Window, window), (P.ColorRole.WindowText, text), (P.ColorRole.Base, base),
                             (P.ColorRole.AlternateBase, alternate), (P.ColorRole.Text, text), (P.ColorRole.Button, button),
                             (P.ColorRole.ButtonText, text), (P.ColorRole.Highlight, highlight),
                             (P.ColorRole.HighlightedText, highlighted), (P.ColorRole.ToolTipBase, base),
                             (P.ColorRole.ToolTipText, text), (P.ColorRole.PlaceholderText, disabled)):
            palette.setColor(role, C(colour))
        for role in (P.ColorRole.Text, P.ColorRole.WindowText, P.ColorRole.ButtonText):
            palette.setColor(P.ColorGroup.Disabled, role, C(disabled))
        return palette

    def restore(self) -> None:
        self.app.setPalette(self.saved_palette)
        if self.saved_style:
            self.app.setStyle(self.saved_style)

    # --- windows ---
    def open(self, database: Path, scenario: scenarios.Scenario, theme: str, width: int, height: int):
        """A laid-out window showing the scenario, never shown on screen."""

        self.app.setPalette(self.dark if theme == "auto-dark" else self.light)
        window = self.win.LoggerWindow(
            database, load_order=lambda: scenario.load_order,
            profiles=(lambda: scenario.profiles) if scenario.profiles else None,
            current_profile=(lambda: scenario.history_profile) if scenario.history_profile else None)
        # The window wears sayngwyn when nothing is saved; the other pictures ask for their own mode.
        if theme != "sayngwyn":
            window.set_theme_mode(theme if theme in ("light", "dark") else "auto")
        window.resize(width, height)
        if scenario.page == "history":
            window.show_page("history")
            window.history.open_groups(scenario.expand)
            self.settle(window)
            return window
        if scenario.query:
            window.search.setText(scenario.query)
            window._search_timer.stop()
            window._apply_search()
        else:
            window.refresh()
        if scenario.expand:
            role = self.win._ROLE
            ids = []
            for index in range(min(scenario.expand, window.tree.topLevelItemCount())):
                data = window.tree.topLevelItem(index).data(0, role)
                if data and data[0] == self.win._EVENT:
                    ids.append(data[1])
            window._expanded.update(ids)
            window._drawn_signature = None
            window._render()
        if scenario.open_files:
            holders = [item for item in window._items() if item.text(0).startswith("Files (")]
            for holder in holders[:scenario.open_files]:
                parent = holder.parent()
                while parent is not None:      # the option the row hangs from has to be open to see it
                    parent.setExpanded(True)
                    parent = parent.parent()
                holder.setExpanded(True)
        self.settle(window)
        return window

    def settle(self, window) -> None:
        # Nested layouts (a fold that just opened, wrapped text in a scroll area) settle over a few
        # rounds of layout requests, as they do in a running window.
        for _ in range(3):
            self.app.processEvents()
            window.grab()  # lays out every nested layout, as a real show would

    def expand_folds(self, window) -> None:
        """Open the "Done or dismissed" and "Picked everywhere" folds so their cards are measured too."""

        for button in window.findChildren(self.W.QToolButton, "fold"):
            if button.isVisibleTo(window) and button.isCheckable() and not button.isChecked():
                button.setChecked(True)
        self.settle(window)


# ---------- colour maths ----------

def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _rgb(colour) -> tuple[int, int, int]:
    return colour.red(), colour.green(), colour.blue()


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


def _colours(env: Env, pixmap, inset: tuple[int, int] = (0, 0)) -> Counter:
    """Colour histogram of the pixmap (opaque RGB), ignoring ``inset`` pixels at each side."""

    image = pixmap.toImage().convertToFormat(env.Gui.QImage.Format.Format_RGB32)
    width, height = image.width(), image.height()
    ix, iy = inset
    ix, iy = (ix if width > 2 * ix + 2 else 0), (iy if height > 2 * iy + 2 else 0)
    view = memoryview(image.constBits().asstring(image.sizeInBytes())).cast("I")
    stride = image.bytesPerLine() // 4
    counts: Counter = Counter()
    for y in range(iy, height - iy):
        counts.update(view[y * stride + ix: y * stride + width - ix])
    return counts


def _as_rgb(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


def _background(counts: Counter) -> tuple[int, int, int]:
    return _as_rgb(counts.most_common(1)[0][0])


def _farthest(counts: Counter, background: tuple[int, int, int]) -> tuple[int, int, int]:
    """The colour furthest from the background: the text's own colour, as rendered."""

    best, distance = background, -1
    for value in counts:
        rgb = _as_rgb(value)
        d = sum((a - b) ** 2 for a, b in zip(rgb, background))
        if d > distance:
            best, distance = rgb, d
    return best


def _backdrop(env: Env, window, widget):
    """The widget as drawn on top of what is behind it: the nearest opaque ancestor (a card section or the
    window) rendered for just the widget's rectangle, so translucent chips and transparent labels show true colours."""

    ancestor = widget.parentWidget()
    while ancestor is not None and ancestor is not window and ancestor.objectName() != "section":
        ancestor = ancestor.parentWidget()
    ancestor = ancestor or window
    origin = widget.mapTo(ancestor, env.Qt.QPoint(0, 0))
    return ancestor.grab(env.Qt.QRect(origin, widget.size()))


def _describe(widget) -> str:
    text = getattr(widget, "text", lambda: "")()
    text = (text or "").replace("\n", " ")
    shown = text if len(text) <= 48 else text[:45] + "..."
    name = widget.objectName()
    return f"{type(widget).__name__}{'#' + name if name else ''} '{shown}'"


def _visible(window, widget) -> bool:
    return widget.isVisibleTo(window) and widget.width() > 0 and widget.height() > 0


def text_widgets(env: Env, window) -> list:
    return [w for cls in (env.W.QLabel, env.W.QPushButton, env.W.QToolButton) for w in window.findChildren(cls)
            if _visible(window, w) and (w.text() or "").strip()]


# ---------- contrast ----------

SAMPLE_PER_STYLE = 80


def _sample(widgets: list) -> list:
    """Every widget, except that a style (class and object name) with hundreds of look-alikes is
    sampled evenly: ``huge`` has thousands of identical cross-patch buttons, and drawing each
    one to measure it takes minutes without telling more than a spread of them does."""

    groups: dict[tuple[str, str], list] = {}
    for widget in widgets:
        groups.setdefault((type(widget).__name__, widget.objectName()), []).append(widget)
    chosen = []
    for members in groups.values():
        if len(members) <= SAMPLE_PER_STYLE:
            chosen.extend(members)
        else:
            step = len(members) / SAMPLE_PER_STYLE
            chosen.extend(members[int(i * step)] for i in range(SAMPLE_PER_STYLE))
    return chosen


def _required(widget) -> float:
    font = widget.font()
    points = font.pointSizeF()
    bold = font.weight() >= 600
    return LARGE_RATIO if points >= 18 or (bold and points >= 14) else NORMAL_RATIO


def contrast(env: Env, window, *, scroll_tree: bool = True) -> list[Finding]:
    found: list[Finding] = []
    P = env.Gui.QPalette
    for widget in _sample(text_widgets(env, window)):
        counts = _colours(env, _backdrop(env, window, widget), inset=(8, 3))
        background = _background(counts)
        if isinstance(widget, env.W.QLabel):
            text = _rgb(widget.palette().color(P.ColorRole.WindowText))  # the stylesheet's colour
        else:
            text = _farthest(counts, background)  # buttons change colour with their state
        ratio = contrast_ratio(text, background)
        need = _required(widget)
        if ratio < need:
            found.append(Finding("contrast", "FAIL", _describe(widget),
                                 f"{ratio:.2f}:1 (needs {need}:1), text {_hex(text)} on {_hex(background)}"))
    found.extend(_tree_contrast(env, window, scroll_tree))
    return found


def active_tree(window):
    """The list the window is showing: the installers' tree, or the list history's."""

    return window.history.tree if getattr(window, "page", "") == "history" else window.tree


def _tree_items(window):
    return window.history.items() if getattr(window, "page", "") == "history" else window._items()


def _tree_contrast(env: Env, window, scroll: bool) -> list[Finding]:
    tree = active_tree(window)
    bar = tree.verticalScrollBar()
    viewport = tree.viewport()
    header = tree.header()
    found: dict[tuple, Finding] = {}
    original = bar.value()
    items = _tree_items(window)
    values = range(bar.minimum(), bar.maximum() + 1, max(1, bar.pageStep())) if scroll else [original]
    checked = 0
    for value in (list(values) + ([bar.maximum()] if scroll and bar.maximum() > 0 else [])):
        bar.setValue(value)
        env.app.processEvents()
        # Scrolling makes the view hover whatever row is under the real mouse; that tint isn't part of the design.
        env.app.sendEvent(viewport, env.Qt.QEvent(env.Qt.QEvent.Type.Leave))
        env.app.processEvents()
        image = viewport.grab()
        for item in items:
            rect = tree.visualItemRect(item)
            if rect.height() <= 0 or rect.top() < 0 or rect.bottom() >= viewport.height():
                continue
            for column in range(tree.columnCount()):
                text = item.text(column)
                if not text.strip():
                    continue
                left = header.sectionViewportPosition(column)
                width = header.sectionSize(column)
                dpr = image.devicePixelRatio() if hasattr(image, "devicePixelRatio") else 1.0
                cell = image.copy(round(max(left, 0) * dpr), round(rect.top() * dpr),
                                  max(1, round(min(width, viewport.width() - left) * dpr)), round(rect.height() * dpr))
                counts = _colours(env, cell, inset=(0, 2))
                background = _background(counts)
                brush = item.foreground(column)
                colour = brush.color() if brush.style() != env.Qt.Qt.BrushStyle.NoBrush else tree.palette().color(env.Gui.QPalette.ColorRole.Text)
                checked += 1
                font = item.font(column)
                ratio = contrast_ratio(_rgb(colour), background)
                need = LARGE_RATIO if (font.pointSizeF() >= 18 or (font.weight() >= 600 and font.pointSizeF() >= 14)) else NORMAL_RATIO
                if ratio < need:
                    key = (column, text, _hex(_rgb(colour)), _hex(background))
                    found.setdefault(key, Finding("contrast", "FAIL", f"tree column {column} '{text[:48]}'",
                                                  f"{ratio:.2f}:1 (needs {need}:1), text {_hex(_rgb(colour))} on {_hex(background)}"))
    bar.setValue(original)
    return list(found.values())


# ---------- cut-off text ----------

def cutoff(env: Env, window) -> list[Finding]:
    found: list[Finding] = []
    for widget in text_widgets(env, window):
        text = widget.text().replace("&&", "&")
        metrics = widget.fontMetrics()
        if isinstance(widget, env.W.QLabel):
            if widget.wordWrap():
                # A line can break at a space, or at a zero-width space the window puts inside very long names.
                words = [part for line in text.split("\n") for part in re.split(r"[\s​]+", line) if part]
                longest = max(words, key=lambda word: metrics.horizontalAdvance(word), default="")
                advance = metrics.horizontalAdvance(longest)
                if advance > widget.width():
                    found.append(Finding("cutoff", "FAIL", _describe(widget),
                                         f"one unbreakable word '{longest[:40]}' is {advance}px wide but the label is {widget.width()}px"))
                needed = widget.heightForWidth(widget.width())
                if needed > widget.height():
                    found.append(Finding("cutoff", "FAIL", _describe(widget),
                                         f"wrapped text needs {needed}px of height but the label is {widget.height()}px"))
                continue
            advance = max(metrics.horizontalAdvance(line) for line in text.split("\n"))
            hint = widget.sizeHint().width()
            if advance > widget.width() or widget.width() < hint:
                found.append(Finding("cutoff", "FAIL", _describe(widget),
                                     f"text is {advance}px wide (size hint {hint}px) but the label is {widget.width()}px"))
        else:
            if widget.findChild(env.W.QLabel, "wrapText") is not None:
                continue  # a button that wraps its text in a label of its own; that label is measured above
            advance = metrics.horizontalAdvance(text)
            hint = widget.sizeHint().width()
            if advance > widget.width() or widget.width() < hint:
                found.append(Finding("cutoff", "FAIL", _describe(widget),
                                     f"text is {advance}px wide (size hint {hint}px) but the button is {widget.width()}px"))
    for box in window.findChildren(env.W.QComboBox):
        if _visible(window, box) and box.currentText():
            # What the box needs besides its text (arrow, padding, frame) is its size hint less its widest item.
            metrics = box.fontMetrics()
            widest = max(metrics.horizontalAdvance(box.itemText(i)) for i in range(box.count()))
            room = box.width() - (box.sizeHint().width() - widest)
            advance = metrics.horizontalAdvance(box.currentText())
            if advance > room:
                found.append(Finding("cutoff", "FAIL", f"QComboBox#{box.objectName()} '{box.currentText()[:40]}'",
                                     f"text is {advance}px wide but the box has {room}px for it"))
    found.extend(_tree_cutoff(env, window))
    found.extend(_side_overflow(env, window))
    return found


def _tree_cutoff(env: Env, window) -> list[Finding]:
    """Columns 1 and 2 size to their text, so eliding there is a defect. Column 0 elides by Qt's
    default (the code sets no eliding on purpose), which is reported once as a note."""

    tree = active_tree(window)
    header = tree.header()
    # The room a cell keeps around its text: the window's own themes pad items (10px in all); "Match MO2" leaves
    # items to MO2's theme, and then Qt's own margin applies (PM_FocusFrameHMargin + 1 on each side).
    if "::item { padding: 4px 2px" in window.styleSheet():
        padding = 10
    else:
        padding = 2 * (tree.style().pixelMetric(env.W.QStyle.PixelMetric.PM_FocusFrameHMargin, None, tree) + 1)
    found: list[Finding] = []
    elided0 = 0
    worst0 = ("", 0)
    for item in _tree_items(window):
        for column in range(tree.columnCount()):
            text = item.text(column)
            if not text or (column == 0 and item.columnCount() > 0 and item.parent() is None and not item.text(1) and not item.text(2) and item.childCount() == 0):
                continue
            metrics = env.Gui.QFontMetrics(item.font(column))
            advance = metrics.horizontalAdvance(text)
            depth = 0
            parent = item.parent()
            while parent is not None:
                depth += 1
                parent = parent.parent()
            room = header.sectionSize(column) - (tree.indentation() * (depth + 1) if column == 0 else 0) - padding
            if advance > room:
                if column == 0:
                    elided0 += 1
                    if advance - room > worst0[1]:
                        worst0 = (text, advance - room)
                else:
                    found.append(Finding("cutoff", "FAIL", f"tree column {column} '{text[:48]}'",
                                         f"text is {advance}px but the column is {header.sectionSize(column)}px"))
    if elided0:
        found.append(Finding("note", "NOTE", "tree column 0",
                             f"{elided0} names are elided with '...' by Qt's default (column is {header.sectionSize(0)}px "
                             f"of {tree.viewport().width()}px; worst '{worst0[0][:40]}' is {worst0[1]}px too wide)"))
    return found


def _side_overflow(env: Env, window) -> list[Finding]:
    side = window.findChild(env.W.QScrollArea, "side")
    if side is None or not side.isVisibleTo(window):       # a dialog has no side panel; the history page hides it
        return []
    holder = side.widget()
    needed, have = holder.minimumSizeHint().width(), side.viewport().width()
    if needed > have:
        return [Finding("cutoff", "FAIL", "side panel",
                        f"its contents need {needed}px but the panel is {have}px wide, so it scrolls sideways")]
    return []


# ---------- overlap ----------

def _children(env: Env, parent) -> list:
    skip = (env.W.QScrollBar, env.W.QSizeGrip, env.W.QMenu, env.W.QDialog)
    found = []
    for child in parent.children():
        if (isinstance(child, env.W.QWidget) and not isinstance(child, skip) and not child.isWindow()
                and child.objectName() != "qt_scrollarea_viewport" and _visible(parent, child)
                and type(child).__name__ != "QSplitterHandle"):
            found.append(child)
    return found


def overlap(env: Env, window) -> list[Finding]:
    found: list[Finding] = []
    parents = [window] + [w for w in window.findChildren(env.W.QWidget)
                          if (w.layout() is not None or isinstance(w, env.W.QSplitter)) and _visible(window, w)]
    for parent in parents:
        kids = sorted(_children(env, parent), key=lambda kid: kid.geometry().top())
        for index, a in enumerate(kids):
            ga = a.geometry()
            if (ga.left() < -1 or ga.top() < -1 or ga.right() > parent.width() + 1 or ga.bottom() > parent.height() + 1)                     and not isinstance(parent, env.W.QSplitter):
                found.append(Finding("overlap", "FAIL", _describe(a),
                                     f"sticks out of its parent {_describe(parent) if parent is not window else 'window'}: "
                                     f"{ga.left()},{ga.top()} {ga.width()}x{ga.height()} in {parent.width()}x{parent.height()}"))
            for b in kids[index + 1:]:
                if b.geometry().top() > ga.bottom():
                    break  # sorted by top: nothing further down can touch this one
                inter = ga.intersected(b.geometry())
                if inter.width() > 0 and inter.height() > 0:
                    found.append(Finding("overlap", "FAIL", f"{_describe(a)} / {_describe(b)}",
                                         f"overlap by {inter.width()}x{inter.height()}px inside "
                                         f"{_describe(parent) if parent is not window else 'the window'}"))
    return found


# ---------- content ----------

def _hue_ok(colour, low: int, high: int) -> bool:
    hue = colour.hsvHue()
    return low <= hue <= high if low <= high else hue >= low or hue <= high


def history_content(env: Env, window, scenario: scenarios.Scenario) -> list[Finding]:
    """The List history page shows what the database holds: the newest page of the profile's changes, newest first and
    grouped by day, the programs as bold markers with how they ended, the count and the controls."""

    from datetime import datetime

    from fomod_logger_support.core import history as hist

    found: list[Finding] = []

    def fail(widget: str, detail: str) -> None:
        found.append(Finding("content", "FAIL", widget, detail))

    view = window.history
    expect = scenario.expect
    wanted = [e for e in scenario.history if e["profile"] in ("", scenario.history_profile)]
    if window.page != "history":
        fail("window", f"shows the {window.page} page, not the history")
    if window.tree.isVisibleTo(window):
        fail("installers tree", "is visible on the history page")
    if window.installer_chips.isVisibleTo(window) or not window.history_chips.isVisibleTo(window):
        fail("header", "the history page shows the installers' chips, or lacks its own")
    if window.snapshot.error:
        fail("window", f"the window reports an error: {window.snapshot.error}")
    if view._error:
        fail("history", f"could not be read: {view._error}")
    if view.total != len(wanted):
        fail("history", f"counts {view.total} changes, the profile has {len(wanted)}")
    page = min(len(wanted), hist.KEEP_ENTRIES, 250)
    if len(view.entries) != page:
        fail("history", f"draws {len(view.entries)} entries, expected the newest {page}")
    times = [entry["at"] for entry in view.entries]
    if times != sorted(times, reverse=True):
        fail("history", "is not newest first")
    tops = [view.tree.topLevelItem(i) for i in range(view.tree.topLevelItemCount())]
    days = hist.by_day(view.entries)
    if len(tops) != len(days):
        fail("history", f"lists {len(tops)} days, expected {len(days)}")
    rows = [item for top in tops for item in (top.child(n) for n in range(top.childCount()))]
    if len(rows) != len(view.entries):
        fail("history", f"lists {len(rows)} lines for {len(view.entries)} entries")
    text = f"{len(wanted):,} change"
    if not view.summary.text().startswith(text):
        fail("summary", f"says '{view.summary.text()}', expected it to start '{text}'")
    if window.history_chip.text() != hist.plural(len(wanted), "change"):
        fail("header chip", f"says '{window.history_chip.text()}'")
    if view.more_button.isVisibleTo(window) != (len(wanted) > len(view.entries)):
        fail("Show more", "is shown, or missing, wrongly")
    if "history_kinds" in expect:
        missing = sorted(set(expect["history_kinds"]) - {entry["kind"] for entry in wanted})
        if missing and scenario.name == "history":
            fail("scenario", f"the profile has no {missing}")
    runs = [entry for entry in view.entries if entry["kind"] == "program_run"]
    markers = [item for item in rows if item.font(0).bold()]
    if len(markers) != len(runs):
        fail("programs", f"{len(markers)} bold marker lines for {len(runs)} program runs")
    for item in markers:
        line = item.text(0)
        if " ran " not in line and "started" not in line:
            fail("programs", f"the marker '{line}' doesn't say that the program ran")
        if (" ran " in line) != ("exited" in line):
            fail("programs", f"the marker '{line}' says how it ended only some of the time")
    if runs and not view.since_button.isEnabled():
        fail("Since the last program ran", "is off although programs have run")
    offline = [entry for entry in view.entries if entry["offline"]]
    noted = [item for item in rows if hist.OFFLINE_NOTE in item.text(1)]
    if len(noted) != len(offline):
        fail("history", f"{len(noted)} lines say 'found at MO2 start', {len(offline)} entries were")
    for key, button in view.kind_buttons.items():
        if not button.isVisibleTo(window) or not button.isChecked():
            fail(f"filter '{button.text()}'", "is hidden or off in a fresh window")
    opened = sum(1 for item in rows if item.childCount() and item.isExpanded())
    if scenario.expand and any(entry["count"] > 1 for entry in view.entries) and not opened:
        fail("history", "no grouped entry is open")
    return found


def content(env: Env, window, scenario: scenarios.Scenario) -> list[Finding]:
    if scenario.page == "history":
        return history_content(env, window, scenario)
    from fomod_logger_support.viewer import model

    found: list[Finding] = []
    expect = scenario.expect

    def fail(widget: str, detail: str) -> None:
        found.append(Finding("content", "FAIL", widget, detail))

    snapshot = window.snapshot
    if snapshot.error:
        fail("window", f"the window reports an error: {snapshot.error}")
    counts = Counter(item.get("state", "missing") for item in snapshot.missed)
    for state, number in (expect.get("states") or {}).items():
        if counts.get(state, 0) != number:
            fail("missed patches", f"expected {number} cards in state '{state}', found {counts.get(state, 0)}")
    if "states" in expect:
        extra = set(counts) - set(expect["states"])
        if extra:
            fail("missed patches", f"unexpected card states {sorted(extra)}")
    if "min_missing" in expect and counts.get("missing", 0) < expect["min_missing"]:
        fail("missed patches", f"expected at least {expect['min_missing']} missed patches, found {counts.get('missing', 0)}")
    if "min_events" in expect and len(scenario.events) < expect["min_events"]:
        fail("scenario", f"only {len(scenario.events)} events generated")
    missing = counts.get("missing", 0)
    broken = counts.get("missing-masters", 0)
    off = sum(v for k, v in counts.items() if k not in ("missing", "missing-masters"))
    pending = sum(1 for r in scenario.reviews if r["state"] == "pending")
    unseen = sum(1 for c in snapshot.changes if c["event_id"] not in snapshot.changes_seen)
    if "changes" in expect and unseen != expect["changes"]:
        fail("changed installers", f"expected {expect['changes']} cards to check, found {unseen}")
    if "changes_seen" in expect and len(snapshot.changes_seen) != expect["changes_seen"]:
        fail("changed installers", f"expected {expect['changes_seen']} settled cards, found {len(snapshot.changes_seen)}")
    # New-patch alerts: one visible "New: you just installed ..." line per pending alert, nowhere else.
    new_lines = [label for label in window.findChildren(env.W.QLabel)
                 if label.objectName() == "cardNew" and _visible(window, label)]
    wanted = expect.get("alerts", 0)
    if len(snapshot.alerts) != wanted:
        fail("new-patch alerts", f"expected {wanted} pending alerts, found {len(snapshot.alerts)}")
    if len(new_lines) != wanted:
        fail("new-patch alerts", f"expected {wanted} cards marked New, found {len(new_lines)}")
    if window.changes_box.isVisibleTo(window) != bool(snapshot.changes):
        fail("Changed installers", f"section is {'shown' if window.changes_box.isVisibleTo(window) else 'hidden'} "
                                   f"with {len(snapshot.changes)} changed installers")
    # Mods reinstalled with only different picks: a note on the mod row and the option markers, never a card.
    rows = {item.text(0): item for item in window._items() if item.parent() is None}
    for mod, note in (expect.get("picks_only") or {}).items():
        row = rows.get(mod)
        if row is None or not row.text(1).startswith(note):
            fail("mod row", f"{mod} should say '{note}', it says '{row.text(1) if row is not None else '(not listed)'}'")
        if any(change["mod_name"] == mod for change in snapshot.changes):
            fail("changed installers", f"{mod} only changed picks but has a card")
        if row is not None and not any(
                child.text(1).startswith(("newly picked", "was picked")) for child in _descendants(row)):
            fail("mod row", f"{mod} is not expanded or shows no 'newly picked' / 'was picked' marker")
    # Profiles: a faint "in no profile" on the mods no profile enables, and the profiles in the mod row's tooltip.
    if "no_profile" in expect:
        tops = [item for item in window._items() if item.parent() is None]
        flagged = sorted(item.text(0) for item in tops if "in no profile" in item.text(1))
        if flagged != sorted(expect["no_profile"]):
            fail("mod rows", f"'in no profile' is on {flagged}, expected {sorted(expect['no_profile'])}")
        for name, tip in expect.get("profile_tips", {}).items():
            tips = [item.toolTip(0) for item in tops if item.text(0) == name]    # a mod in two instances has two rows
            if not any(tip in text for text in tips):
                fail("mod row", f"{name}'s tooltip should say '{tip}', it says {tips}")
    # A file search: the options that install the file say so, and their (collapsed or opened) Files rows are there.
    if "file_hits" in expect:
        marked = [item for item in window._items() if item.text(1).startswith("installs ")]
        if len(snapshot.file_hits) != expect["file_hits"]:
            fail("file search", f"expected {expect['file_hits']} options installing the file, found {len(snapshot.file_hits)}")
        if len(marked) != expect["file_hits"]:
            fail("file search", f"expected {expect['file_hits']} rows saying 'installs ...', found {len(marked)}")
        if sum(1 for item in marked if " · wins" in item.text(1)) != 1:
            fail("file search", "exactly one picked option should be marked as the winner")
        holders = [item for item in window._items() if item.text(0).startswith("Files (")]
        if len(holders) != expect["file_hits"]:
            fail("file search", f"expected {expect['file_hits']} Files rows, found {len(holders)}")
        if scenario.open_files and sum(1 for item in holders if item.isExpanded()) != scenario.open_files:
            fail("file search", f"expected {scenario.open_files} opened Files rows")
        if not window.search.placeholderText().endswith("files…"):
            fail("search box", "the placeholder doesn't say it searches files")
    # Mods without installer options: a quiet row each, counted in the summary, never in the logged-before-patch-checking hint.
    tops = [item for item in window._items() if item.parent() is None]
    if "plain" in expect:
        quiet = [item for item in tops if "no installer options" in item.text(1)]
        if len(quiet) != expect["plain"]:
            fail("mod rows", f"{len(quiet)} rows say 'no installer options', expected {expect['plain']}")
        if f"{expect['plain']:,} without installer options" not in window.summary_label.text():
            fail("summary chip", f"shows '{window.summary_label.text()}', expected it to count {expect['plain']} mods without installer options")
    elif "plain_hits" not in expect and any("no installer options" in item.text(1) for item in tops):
        fail("mod rows", "a row says 'no installer options' in a scenario without such mods")
    if "plain_hits" in expect:
        hits = [item for item in tops if "match" in item.text(1) and "no installer options" in item.text(1)]
        holders = [item for item in window._items() if item.text(0).startswith("Files (") and item.isExpanded()]
        if len(hits) != expect["plain_hits"] or len(holders) != expect["plain_hits"]:
            fail("file search", f"expected {expect['plain_hits']} plain mod(s) found with their files opened, found {len(hits)} and {len(holders)}")
        if holders and not any(holders[0].child(n).background(0).style() != env.Qt.Qt.BrushStyle.NoBrush
                               for n in range(min(3, holders[0].childCount()))):
            fail("file search", "the matching file is not marked")
    # sections: visible exactly when they have something to show
    for box, label, number in ((window.missed_box, "Possible missed patches", missing),
                               (window.masters_box, "Missing masters", broken),
                               (window.off_box, "Present but plugin disabled", off)):
        shown = box.isVisibleTo(window)
        if label == "Possible missed patches":
            if missing and not shown:
                fail(label, "section is hidden although patches were missed")
        elif bool(number) != shown:
            fail(label, f"section is {'shown' if shown else 'hidden'} but has {number} cards")
    for count_label, number, name in ((window.missed_count, missing, "missed count"), (window.masters_count, broken, "masters count"),
                                      (window.off_count, off, "disabled count"), (window.review_count, pending, "to revisit count"),
                                      (window.changes_count, unseen, "changed installers count")):
        if number and count_label.text() != str(number):
            fail(name, f"shows '{count_label.text()}' but should be {number}")
        if not number and count_label.isVisibleTo(window):
            fail(name, f"is visible with {number}")
    for chip, number, word in ((window.missed_chip, missing, "possible missed patch"), (window.masters_chip, broken, "can't load"),
                               (window.off_chip, off, "switched off"), (window.revisit_chip, pending, "to revisit")):
        if number and (not chip.isVisibleTo(window) or word not in chip.text() or not chip.text().startswith(str(number))):
            fail(f"header chip '{chip.text()}'", f"should say {number} {word}")
        if not number and chip.isVisibleTo(window):
            fail(f"header chip '{chip.text()}'", "is visible with nothing to report")
    if "mods" in expect:
        want = model.plural(expect["mods"], "mod") + (" match" if scenario.query else " logged")
        if not window.summary_label.text().startswith(want) and not scenario.query:
            fail("summary chip", f"shows '{window.summary_label.text()}', expected it to start '{want}'")
    if scenario.query and "match" not in window.summary_label.text():
        fail("summary chip", f"search '{scenario.query}' is active but the summary says '{window.summary_label.text()}'")
    problems = len(scenario.problems)
    if window.problem_box.isVisibleTo(window) != bool(problems):
        fail("Problems", f"section {'shown' if window.problem_box.isVisibleTo(window) else 'hidden'} with {problems} bad files")
    mixed = sum(1 for p in snapshot.cross_patches if model.classify(p) == "mixed")
    if mixed and window.cross_count.text() != str(mixed):
        fail("Cross-patches", f"count shows '{window.cross_count.text()}' but {mixed} are mixed")
    texts = [label.text() for label in window.findChildren(env.W.QLabel) if _visible(window, label)]
    if expect.get("four_masters") and not any("it needs 4 masters that aren't in your list:" in t for t in texts):
        fail("Missing masters", "no card lists four missing masters")
    if scenario.name == "empty":
        branded = window.empty_state.isVisibleTo(window) and window.empty_state.title.text() == "No mods logged yet."
        if not branded and not any("No mods logged yet" in item.text(0) for item in window._items()):
            fail("list", "the first-run message is missing")
        if branded != (window._theme_mode == "sayngwyn"):
            fail("list", "the branded first-run message belongs to the sayngwyn theme only")
    found.extend(_colour_meaning(env, window))
    return found


def _descendants(item):
    for index in range(item.childCount()):
        child = item.child(index)
        yield child
        yield from _descendants(child)


def _colour_meaning(env: Env, window) -> list[Finding]:
    """Red for missed, orange for switched off, purple for missing masters, as the sections promise."""

    found: list[Finding] = []
    tokens = window._tokens
    for name, colour, low, high in (("missed (red)", tokens.missed, 345, 12), ("switched off (orange)", tokens.off, 14, 38),
                                    ("missing masters (purple)", tokens.masters, 255, 290), ("reminder (amber)", tokens.mark, 35, 52),
                                    ("picked (green)", tokens.picked, 120, 160)):
        if not _hue_ok(env.Gui.QColor(colour), low, high):
            found.append(Finding("content", "FAIL", f"theme token {name}", f"hue {colour.hsvHue()} is outside {low}-{high}"))
    for frame in window.findChildren(env.W.QFrame, "section"):
        if not frame.isVisibleTo(window):
            continue
        kind = frame.property("kind")
        want = {"missed": tokens.missed, "masters": tokens.masters, "off": tokens.off, "revisit": tokens.mark,
                "cross": tokens.stripe or tokens.accent, "changes": tokens.stripe or tokens.accent}.get(kind)
        if want is None:
            continue
        counts = _colours(env, frame.grab(env.Qt.QRect(1, frame.height() // 2, 3, 1)))  # a tall frame can't be drawn whole
        got = _as_rgb(counts.most_common(1)[0][0])
        if max(abs(a - b) for a, b in zip(got, _rgb(want))) > 16:
            found.append(Finding("content", "FAIL", f"section '{kind}'", f"edge stripe is {_hex(got)}, expected {_hex(_rgb(want))}"))
    return found


# ---------- the Compare choices dialog ----------

COMPARE_SIZE = (900, 640)
_COMPARE_WORDS = {"same", "different", "only in A", "only in B", "page only in A", "page only in B",
                  "page skipped in A", "page skipped in B", "picked only in A", "picked only in B",
                  "option only in A", "option only in B", "named differently", "page skipped"}


def _install_id(scenario: scenarios.Scenario, ref) -> str:
    """The event id of (instance id, mod name) or (instance id, mod name, n): the mod's n-th newest install."""

    instance, name = ref[0], ref[1]
    installs = sorted((e for e in scenario.events if e["instance"]["id"] == instance and e["mod"]["name"] == name),
                      key=lambda e: e["completed_at"], reverse=True)
    return installs[ref[2] if len(ref) > 2 else 0]["event_id"]


def open_compare(env: Env, window, scenario: scenarios.Scenario, spec: dict):
    """The Compare choices dialog of ``window`` showing ``spec`` (a scenario's ``compare`` entry), laid out but not shown."""

    if spec["kind"] == "instances":
        a, b = spec["a"], spec["b"]
    else:
        a, b = _install_id(scenario, spec["a"]), _install_id(scenario, spec["b"])
    dialog = window.compare_choices(spec["kind"], a, b, show=False)
    dialog.identical_box.setChecked(bool(spec.get("identical")))
    dialog.resize(*COMPARE_SIZE)
    dialog.expand_all()
    env.settle(dialog)
    return dialog


def compare_content(env: Env, dialog, spec: dict) -> list[Finding]:
    found: list[Finding] = []

    def fail(widget: str, detail: str) -> None:
        found.append(Finding("content", "FAIL", widget, detail))

    if dialog.summary_text() != spec["summary"]:
        fail("summary", f"says '{dialog.summary_text()}', expected '{spec['summary']}'")
    if dialog.kind != spec["kind"]:
        fail("kind", f"shows '{dialog.kind}', expected '{spec['kind']}'")
    rows = dialog.rows()
    if any(row[1] == "…" for row in rows):
        fail("tree", "a row was left unopened (still the placeholder)")
    words = {row[4] for row in rows if row[4]}
    if words - _COMPARE_WORDS:
        fail("tree", f"unknown difference words {sorted(words - _COMPARE_WORDS)}")
    if not rows or not any(row[4] for row in rows):
        fail("tree", "no differences listed")
    if spec.get("identical") != any(row[4] == "same" for row in rows) and spec["kind"] != "installs":
        fail("tree", "'same' rows are listed only while Show identical is on")
    tokens = dialog.parent()._tokens
    for name, colour in (("only in A", tokens.accent_text), ("only in B", tokens.off)):
        for item in dialog._items():
            if item.text(3) in (name, "picked " + name, "option " + name) and item.foreground(3).color() != colour:
                fail("tree", f"'{item.text(3)}' is not drawn in the {name[-1]} colour")
                break
    return found


def compare_all(env: Env, dialog, spec: dict) -> dict[str, list[Finding]]:
    return {"contrast": contrast(env, dialog), "cutoff": cutoff(env, dialog), "overlap": overlap(env, dialog),
            "content": compare_content(env, dialog, spec)}


# ---------- everything ----------

def run_all(env: Env, window, scenario: scenarios.Scenario, *, folds: bool = True) -> dict[str, list[Finding]]:
    if folds:
        env.expand_folds(window)
    return {"contrast": contrast(env, window), "cutoff": cutoff(env, window), "overlap": overlap(env, window),
            "content": content(env, window, scenario)}


def size_note(window, width: int, height: int) -> list[Finding]:
    if (window.width(), window.height()) != (width, height):
        return [Finding("note", "NOTE", "window",
                        f"asked for {width}x{height} but the layout's minimum made it {window.width()}x{window.height()}")]
    return []


def timings(env: Env, database: Path, scenario: scenarios.Scenario, theme: str = "light") -> tuple[float, float]:
    """(first refresh, repeat refresh) in seconds for a freshly built window."""

    # The window doesn't trust a file that was written to in the last couple of seconds, so a database that
    # was only just built would be read again by the repeat refresh. Hold it open (which keeps its journal
    # file in place) and age the files, so the timing is the same whenever this runs.
    from fomod_logger_support.core.storage import EventStore

    keeper = EventStore(database)
    keeper.event_count()
    aged = time.time() - 60
    for suffix in ("", "-wal", "-shm"):
        if Path(f"{database}{suffix}").exists():
            os.utime(f"{database}{suffix}", (aged, aged))
    env.app.setPalette(env.light)
    window = env.win.LoggerWindow(database, load_order=lambda: scenario.load_order,
                                  current_profile=(lambda: scenario.history_profile) if scenario.history_profile else None)
    if theme != "sayngwyn":
        window.set_theme_mode(theme)
    window.resize(1500, 760)
    start = time.perf_counter()
    if scenario.page == "history":
        window.show_page("history")
    else:
        window.refresh()
    first = time.perf_counter() - start
    start = time.perf_counter()
    window.refresh()
    repeat = time.perf_counter() - start
    window.deleteLater()
    keeper.close()
    return first, repeat


def summarize(results: dict[str, list[Finding]]) -> str:
    parts = []
    for kind, findings in results.items():
        fails = sum(1 for f in findings if f.severity == "FAIL")
        parts.append(f"{kind} {'ok' if not fails else f'{fails} FAIL'}")
    return ", ".join(parts)
