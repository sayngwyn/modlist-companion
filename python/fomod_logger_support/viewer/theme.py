"""Colours and styles for the Modlist Companion window.

Everything is derived from the palette MO2's theme gives the window, so the
window looks at home in light and dark themes alike. Colour is never the only
signal: every coloured state also has a word or symbol.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFontDatabase, QImage, QPalette, QPixmap


@dataclass(frozen=True)
class Tokens:
    dark: bool
    text: QColor
    faint: QColor
    window: QColor
    surface: QColor      # the list and the side cards
    raised: QColor       # card headers, hovered rows
    border: QColor
    accent: QColor       # interactive fill: the active filter, stripes, selections
    on_accent: QColor    # text on that fill
    accent_text: QColor  # the accent as text (the card buttons)
    picked: QColor
    missed: QColor
    mark: QColor         # reminders and search matches
    off: QColor          # installed but switched off
    masters: QColor      # installed but can't load: a master is missing
    deep: QColor | None = None  # a deep fill for selected rows (the sayngwyn theme only)
    deep_hover: QColor | None = None  # the same for hovered rows (sayngwyn only)
    alternate: QColor | None = None  # every other row of a list (sayngwyn only; the others use raised)
    gradient: QColor | None = None  # where the window background fades to, top to bottom (sayngwyn only)
    hit_alpha: int = 48  # how strongly a row that matches the search is tinted (see HIT_ALPHA)
    silver: QColor | None = None  # the outline of every everyday button (the sayngwyn theme only)
    stripe: QColor | None = None  # the edge stripe of the neutral side cards, in place of the accent (sayngwyn only)
    alarm_alpha: int = 36  # the tint behind the missed-patch pill and badge (see _CHIP_ALPHA)


MODES = ("sayngwyn", "light", "dark", "auto")   # the menu's order (the sayngwyn-mod-style skill's theme labels)
DEFAULT_MODE = "sayngwyn"   # what anyone with no saved choice sees
# Plain words: symbol glyphs are missing from some of the fonts MO2 themes use. No "&": a menu would read it as a shortcut key.
MODE_LABELS = {"sayngwyn": "sayngwyn", "auto": "Match MO2", "light": "Light", "dark": "Dark"}

# Fixed palettes for when the user picks light or dark in the window itself.
_FIXED = {
    "light": {"window": "#f3f4f6", "base": "#ffffff", "alternate": "#f6f7f9", "text": "#1b1f24",
              "faint": "#6b7280", "highlight": "#2463d1", "button": "#ffffff"},
    "dark": {"window": "#1b1e23", "base": "#23272e", "alternate": "#292e36", "text": "#e6e8eb",
             "faint": "#8a929c", "highlight": "#4f8ff7", "button": "#2a2f36"},
    # The sayngwyn colours, taken from the brand character sheet (the sayngwyn-mod-style skill's "App and window
    # theme" table). The cards sit on the lower end of the window's fade.
    "sayngwyn": {"window": "#0f0d0e", "base": "#211a1a", "alternate": "#262020", "text": "#dededb",
                 "faint": "#aca3a2", "highlight": "#812727", "button": "#2b2324", "tooltip": "#2b2324"},
}

# The rest of the sayngwyn brand, used by tokens() when the mode is "sayngwyn".
_BRAND_DEEP = "#601d2c"       # selected rows
_BRAND_DEEP_HOVER = "#3f1b24"  # hovered rows
_BRAND_ACCENT_HOVER = "#963030"  # a hovered primary button
_BRAND_RAISED = "#2b2324"     # card headers, hovered segments
_BRAND_BORDER = "#403839"
_BRAND_STRIPE = "#767375"     # the edge stripe of the neutral side cards
_BRAND_CHIP_TEXT = "#d36e6e"  # the accent as text (card buttons, "New:")
# The red for "possible missed patch". The brand red is a deep blood red (hue 2); this alarm is a lighter,
# pinker red (hue ~348) so a missed-patch stripe or word is never mistaken for the brand accent. It also
# always comes with the word and the warning symbol.
_BRAND_ALARM = "#ff5c7c"
_BRAND_SILVER = "#c2c2c4"     # the outline of secondary buttons

# The brand where the eye rests (the sayngwyn-mod-style skill's "Brand inside the tools"): only the empty list carries
# the mark (faded) and Sayngwyn Serif with the red offset shadow; never rows, buttons or menus.
ASSETS = Path(__file__).resolve().parent / "assets"
MARK_FILE = "sayngwyn-mark.png"          # the stag skull, rendered from Branding's skull-main-web-blood.svg at 300 px
FONT_FILE = "SayngwynSerif-Regular.ttf"  # free under the SIL Open Font License (OFL.txt beside it)
CHECK_FILE = "check-bone.svg"            # the bone tick of a checked box in the sayngwyn theme (never a red fill)
CHECK_OFF_FILE = "check-faint.svg"       # the same tick in the faint grey, for a box that can't be changed
MARK_PX = 150
MARK_OPACITY = 0.2
BRAND_TEXT_PX = 20                       # Sayngwyn Serif only at 18 px or more
SHADOW_SHARE = 0.06                      # the shadow sits down and right by this share of the letter size
BRAND_SHADOW = _BRAND_DEEP


@functools.cache
def brand_family() -> str | None:
    """Sayngwyn Serif's family name once loaded into the application, or None when it can't be."""

    font_id = QFontDatabase.addApplicationFont(str(ASSETS / FONT_FILE))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    return families[0] if families else None


@functools.cache
def _mark_image() -> QImage | None:  # a QImage, not a QPixmap: safe to keep after the application has gone
    image = QImage(str(ASSETS / MARK_FILE))
    return None if image.isNull() else image


def mark(ratio: float) -> QPixmap | None:
    """The mark ``MARK_PX`` device-independent pixels tall, sharp at the screen's pixel ``ratio``."""

    image = _mark_image()
    if image is None:
        return None
    pixmap = QPixmap.fromImage(image.scaledToHeight(round(MARK_PX * ratio), Qt.TransformationMode.SmoothTransformation))
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def palette_for(mode: str, mo2: QPalette) -> QPalette:
    """MO2's own palette for "auto", otherwise a fixed light or dark one."""

    palette = QPalette(mo2)
    colours = _FIXED.get(mode)
    if colours is None:
        # Every colour set explicitly: a copy that only inherits would not undo a palette the window was given before.
        explicit = QPalette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            for role in QPalette.ColorRole:
                if role != QPalette.ColorRole.NColorRoles:
                    explicit.setColor(group, role, mo2.color(group, role))
        return explicit
    roles = {
        QPalette.ColorRole.Window: colours["window"], QPalette.ColorRole.WindowText: colours["text"],
        QPalette.ColorRole.Base: colours["base"], QPalette.ColorRole.AlternateBase: colours["alternate"],
        QPalette.ColorRole.Text: colours["text"], QPalette.ColorRole.Button: colours["button"],
        QPalette.ColorRole.ButtonText: colours["text"], QPalette.ColorRole.Highlight: colours["highlight"],
        QPalette.ColorRole.HighlightedText: "#ffffff", QPalette.ColorRole.ToolTipBase: colours.get("tooltip", colours["base"]),
        QPalette.ColorRole.ToolTipText: colours["text"], QPalette.ColorRole.PlaceholderText: colours["faint"],
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        for role, value in roles.items():
            palette.setColor(group, role, QColor(value))
    for role, value in roles.items():
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(value))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colours["faint"]))
    return palette


# ---------- colour maths ----------
#
# Every colour used for text is derived against the backgrounds it sits on, so a theme
# (MO2's own, or a light or dark one) can't leave it unreadable. WCAG asks for 4.5:1;
# a little more is kept so that rounding in the drawn pixels never drops it below.

MIN_CONTRAST = 4.6
_CHIP_ALPHA = 36      # the tint behind a count chip, as a share of 255
_SELECTED_ALPHA = 70  # a selected row in the list
HIT_ALPHA = 48        # a row that matches the search
BRAND_ALARM_ALPHA = 64  # the missed-patch pill and badge on the sayngwyn theme: a stronger fill, so they read as a warning
BRAND_HIT_ALPHA = 32  # the same on the sayngwyn theme's near-black rows: a lighter tint keeps the state colours true
_HOVER_ALPHA = 40     # a hovered card button


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(colour: QColor) -> float:
    return 0.2126 * _linear(colour.red()) + 0.7152 * _linear(colour.green()) + 0.0722 * _linear(colour.blue())


def contrast_ratio(a: QColor, b: QColor) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def mix(a: QColor, b: QColor, share: float) -> QColor:
    """``a`` moved ``share`` (0..1) of the way towards ``b``."""

    return QColor(*(round(x + (y - x) * share) for x, y in
                    ((a.red(), b.red()), (a.green(), b.green()), (a.blue(), b.blue()))))


def over(top: QColor, alpha: int, bottom: QColor) -> QColor:
    """``top`` drawn at ``alpha`` (0..255) on ``bottom``."""

    return mix(bottom, top, alpha / 255)


def readable(colour: QColor, backgrounds: Callable[[QColor], Iterable[QColor]]) -> QColor:
    """``colour`` moved towards black or white, as little as needed, until it reads at
    ``MIN_CONTRAST`` on every background (which may depend on the colour itself, like a tint).
    The hue is kept: only lightness moves."""

    def holds(candidate: QColor) -> bool:
        return all(contrast_ratio(candidate, background) >= MIN_CONTRAST for background in backgrounds(candidate))

    if holds(colour):
        return colour
    around = list(backgrounds(colour))
    light = sum(luminance(background) for background in around) / len(around) >= 0.18
    target = QColor(0, 0, 0) if light else QColor(255, 255, 255)
    for step in range(1, 41):
        candidate = mix(colour, target, step / 40)
        if holds(candidate):
            return candidate
    return target


def _accent_fill(accent: QColor) -> tuple[QColor, QColor]:
    """(fill, text on the fill) reaching ``MIN_CONTRAST``: white text on a fill darkened as far as
    needed (up to 40%), or black text when the accent is too light for that."""

    white, black = QColor(255, 255, 255), QColor(0, 0, 0)
    for step in range(0, 17):
        fill = mix(accent, black, step / 40)
        if contrast_ratio(white, fill) >= MIN_CONTRAST:
            return fill, white
    for step in range(0, 41):
        fill = mix(accent, white, step / 40)
        if contrast_ratio(black, fill) >= MIN_CONTRAST:
            return fill, black
    return white, black


def tokens(palette: QPalette, mode: str = "auto") -> Tokens:
    brand = mode == "sayngwyn"
    hit_alpha = BRAND_HIT_ALPHA if brand else HIT_ALPHA
    alarm_alpha = BRAND_ALARM_ALPHA if brand else _CHIP_ALPHA
    window = palette.color(QPalette.ColorRole.Window)
    dark = window.lightness() < 128
    base = palette.color(QPalette.ColorRole.Base)
    raised = base.lighter(118) if dark else base.darker(104)
    # (Light and Dark are the shared plain themes of sayngwyn's plugins: neutral greys and a calm blue, no brand reds.)
    if brand:  # the stock lift is too faint on near-black
        raised = QColor(_BRAND_RAISED)
    alternate = palette.color(QPalette.ColorRole.AlternateBase)
    text = palette.color(QPalette.ColorRole.Text)
    palette_accent = palette.color(QPalette.ColorRole.Highlight)
    if palette_accent.saturation() < 40:  # a grey highlight gives no accent; use a calm blue
        palette_accent = QColor("#4f8ff7" if dark else "#2463d1")
    accent, on_accent = _accent_fill(palette_accent)
    deep = QColor(_BRAND_DEEP) if brand else None
    deep_hover = QColor(_BRAND_DEEP_HOVER) if brand else None
    surfaces = [window, base, raised, alternate]
    if brand:  # the window fades from its own colour to the card colour
        surfaces += [mix(window, base, share / 4) for share in (1, 2, 3)]

    def chip(colour: QColor, alpha: int = _CHIP_ALPHA) -> list[QColor]:
        return [over(colour, alpha, window), over(colour, alpha, base)]

    # Colours used as text: the card buttons, the list rows and the chips. A selected row
    # draws its text in the plain text colour, so it isn't counted here.
    base_backgrounds = surfaces
    if deep_hover is not None:  # a hovered row is deep red; its text has to read there too
        base_backgrounds = surfaces + [deep_hover]
    mark = readable(QColor("#e9b949" if dark else "#9a6700"), lambda c: base_backgrounds + chip(c)
                    + [over(c, hit_alpha, row) for row in (base, raised, alternate)])
    hit = [over(mark, hit_alpha, row) for row in (base, raised, alternate)]  # a search match's row

    def text_colour(colour: QColor, *, chips: bool, alpha: int = _CHIP_ALPHA) -> QColor:
        return readable(colour, lambda c: base_backgrounds + hit + (chip(c, alpha) if chips else []))

    action = QColor(_BRAND_CHIP_TEXT) if brand else (palette_accent.lighter(130) if dark else palette_accent)
    # The faint grey is the text colour pulled towards the window as far as the backgrounds allow.
    faint = text
    if brand:  # the brand's fine-print colour, lightened only as far as the contrast needs
        preferred = palette.color(QPalette.ColorRole.PlaceholderText)
        for step in range(0, 41):
            faint = mix(preferred, text, step / 40)
            if all(contrast_ratio(faint, background) >= MIN_CONTRAST for background in base_backgrounds + hit):
                break
    else:
        for step in range(22, -1, -1):
            faint = mix(text, window, step / 40)
            if all(contrast_ratio(faint, background) >= MIN_CONTRAST for background in base_backgrounds + hit):
                break
    return Tokens(
        dark=dark,
        text=text,
        faint=faint,
        window=window,
        surface=base,
        raised=raised,
        border=QColor(_BRAND_BORDER) if brand else (window.lighter(150) if dark else window.darker(118)),
        accent=accent,
        on_accent=on_accent,
        accent_text=readable(action, lambda c: base_backgrounds + hit + [over(accent, _HOVER_ALPHA, base)]),
        picked=text_colour(QColor("#5cc98a" if dark else "#1b7f43"), chips=False),
        # The alarm keeps the brand alarm's pinkish hue (~348) in every theme, never the brand red's 0.
        missed=text_colour(QColor(_BRAND_ALARM if brand else "#f0728b" if dark else "#c82344"), chips=True,
                           alpha=alarm_alpha),
        mark=mark,
        off=text_colour(QColor("#f0883e" if dark else "#c2410c"), chips=True),
        masters=text_colour(QColor("#c4a1ff" if dark else "#6d28d9"), chips=True),
        deep=deep,
        deep_hover=deep_hover,
        alternate=alternate if brand else None,
        gradient=base if brand else None,
        hit_alpha=hit_alpha,
        silver=QColor(_BRAND_SILVER) if brand else None,
        stripe=QColor(_BRAND_STRIPE) if brand else None,
        alarm_alpha=alarm_alpha,
    )


def rgba(colour: QColor, alpha: int) -> str:
    return f"rgba({colour.red()}, {colour.green()}, {colour.blue()}, {alpha})"


def window_fill(t: Tokens) -> str:
    """The window background: flat, or in the sayngwyn theme a quiet fade from the window colour to the card colour."""

    if t.gradient is None:
        return t.window.name()
    return f"qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1, stop: 0 {t.window.name()}, stop: 1 {t.gradient.name()})"


def stylesheet(t: Tokens, *, match_mo2: bool = False) -> str:
    """Scoped to the window, so it never restyles MO2 itself. The window's own themes (sayngwyn, Light, Dark) cover
    every widget it uses, so MO2's app-wide sheet never shows through. ``match_mo2`` ("Match MO2"): only Companion's
    own pieces (chips, segments, side cards, card text), in colours taken from MO2's palette, plus one colourless
    rule on list rows; the standard controls are left to MO2's current theme, whatever it is."""

    own = _own_rules(t) + (_brand_own_rules(t) if t.silver is not None else "")
    if match_mo2:
        # The one rule Match MO2 keeps on standard controls: on Qt's Windows style it is what lets rows draw their own
        # colours (picked, switched off, missing masters); it sets no colour, so MO2's theme still colours the rest.
        return "\n#fomodCompanion QAbstractItemView::item { border: none; }\n" + own
    return _cover_rules(t) + _control_rules(t) + (_brand_control_rules(t) if t.silver is not None else "") + own


def _icon(t: Tokens, kind: str) -> str:
    """A tick ("check") or a drop-down arrow ("arrow") drawn in the theme's text colour, as a stylesheet url."""

    theme = "sayngwyn" if t.silver is not None else ("dark" if t.dark else "light")
    name = CHECK_FILE if (kind == "check" and theme == "sayngwyn") else f"{kind}-{theme}.svg"
    return f'url("{(ASSETS / name).as_posix()}")'


def _cover_rules(t: Tokens) -> str:
    """The base of the window's own themes: every kind of widget the window uses gets its colours, borders and
    subcontrols from here, so no rule of MO2's app-wide sheet (``* {background}``, list items, headers, branches,
    indicators, arrows, progress bars) can show through. The rules after this one refine it.

    Plain list rows: never a colour or background (that hides each row's own colours: picked, switched off, missing
    masters, and the alternating rows). They do get ``border: none``: on Qt's Windows style (MO2's) that is what makes
    a row draw its own text colour at all. A styled row drops its own background brush, so the search-match tint is
    painted by rows.TintDelegate instead. Selected and hovered rows are styled freely."""

    selected = t.deep.name() if t.deep is not None else rgba(t.accent, _SELECTED_ALPHA)
    chunk = t.deep.name() if t.deep is not None else t.accent.name()
    box = rgba(t.silver, 170) if t.silver is not None else t.faint.name()
    menu = t.raised.name() if t.silver is not None else t.surface.name()
    return f"""
#fomodCompanion, #fomodCompanion QDialog {{ border: none; }}
#fomodCompanion QSplitter::handle {{ background: transparent; border: none; }}
#fomodCompanion QScrollBar, #fomodCompanion QScrollBar::handle {{ border: none; }}
#fomodCompanion QWidget {{
    background: transparent; color: {t.text.name()}; border: none;
    selection-background-color: {selected}; selection-color: {t.text.name()};
}}
#fomodCompanion QAbstractItemView {{
    background: {t.surface.name()}; alternate-background-color: {(t.alternate or t.raised).name()};
    color: {t.text.name()}; border: 1px solid {t.border.name()};
}}
#fomodCompanion QAbstractItemView::item {{ border: none; }}
#fomodCompanion QAbstractItemView::item:selected {{ background: {selected}; color: {t.text.name()}; }}
#fomodCompanion QTreeView::branch {{ background: transparent; border: none; image: none; }}
#fomodCompanion QTreeView::branch:has-children:closed {{ image: {_icon(t, "branch")}; }}
#fomodCompanion QTreeView::branch:has-children:open {{ image: {_icon(t, "branch-open")}; }}
#fomodCompanion QHeaderView {{ background: {t.surface.name()}; border: none; }}
#fomodCompanion QHeaderView::section {{
    background: {t.surface.name()}; color: {t.faint.name()}; border: none; border-bottom: 1px solid {t.border.name()};
    padding: 4px 6px;
}}
#fomodCompanion QPushButton {{
    background: {t.surface.name()}; color: {t.text.name()}; border: 1px solid {t.border.name()};
    border-radius: 6px; padding: 4px 12px; min-height: 18px;
}}
#fomodCompanion QPushButton:hover {{ background: {t.raised.name()}; }}
#fomodCompanion QPushButton:disabled {{ color: {t.faint.name()}; }}
#fomodCompanion QPushButton:focus {{ border: 1px solid {t.text.name()}; }}
#fomodCompanion QPushButton::menu-indicator, #fomodCompanion QToolButton::menu-indicator {{ image: none; width: 0px; }}
#fomodCompanion QToolButton {{ background: transparent; color: {t.text.name()}; border: none; padding: 2px; }}
#fomodCompanion QToolButton:hover {{ background: {t.raised.name()}; }}
#fomodCompanion QComboBox::drop-down {{ border: none; background: transparent; width: 18px; }}
#fomodCompanion QComboBox::down-arrow {{ image: {_icon(t, "arrow")}; width: 10px; height: 10px; }}
#fomodCompanion QProgressBar {{
    background: {t.surface.name()}; color: {t.text.name()}; border: 1px solid {t.border.name()}; border-radius: 4px;
    text-align: center;
}}
#fomodCompanion QProgressBar::chunk {{ background: {chunk}; border-radius: 3px; }}
#fomodCompanion QCheckBox {{ background: transparent; spacing: 6px; }}
#fomodCompanion QCheckBox::indicator {{
    width: 14px; height: 14px; border: 1px solid {box}; border-radius: 3px; background: {t.raised.name()};
}}
#fomodCompanion QCheckBox::indicator:checked {{ image: {_icon(t, "check")}; }}
#fomodCompanion QCheckBox:disabled {{ color: {t.faint.name()}; }}
#fomodCompanion QMenu {{ background: {menu}; color: {t.text.name()}; border: 1px solid {t.border.name()}; }}
#fomodCompanion QMenu::separator {{ height: 1px; background: {t.border.name()}; margin: 4px 8px; }}
#fomodCompanion QToolTip {{ background: {menu}; color: {t.text.name()}; border: 1px solid {t.border.name()}; padding: 3px 6px; }}
#fomodCompanion QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
#fomodCompanion QScrollBar::handle:horizontal {{ background: {rgba(t.text, 60)}; border-radius: 4px; min-width: 30px; }}
#fomodCompanion QScrollBar::handle:horizontal:hover {{ background: {rgba(t.text, 100)}; }}
#fomodCompanion QAbstractScrollArea::corner {{ background: transparent; border: none; }}
"""


def _control_rules(t: Tokens) -> str:
    """MO2's standard controls: the window, labels, buttons, inputs, menus, scrollbars and the lists."""

    # Rows: the theme's accent as a light tint, or in the sayngwyn theme the deep red.
    hover = t.deep_hover.name() if t.deep_hover is not None else rgba(t.accent, 28)
    selected = t.deep.name() if t.deep is not None else rgba(t.accent, _SELECTED_ALPHA)
    return f"""
#fomodCompanion, #fomodCompanion QDialog {{ background: {window_fill(t)}; color: {t.text.name()}; }}
#fomodCompanion QLabel, #fomodCompanion QToolButton, #fomodCompanion QPushButton,
#fomodCompanion QCheckBox {{ color: {t.text.name()}; }}
#fomodCompanion QComboBox, #fomodCompanion QPlainTextEdit, #fomodCompanion QLineEdit {{
    color: {t.text.name()}; background: {t.surface.name()}; border: 1px solid {t.border.name()};
    border-radius: 6px; padding: 3px 6px; placeholder-text-color: {t.faint.name()};
}}
#fomodCompanion QComboBox QAbstractItemView, #fomodCompanion QMenu {{
    color: {t.text.name()}; background: {t.surface.name()}; border: 1px solid {t.border.name()};
    selection-background-color: {t.accent.name()}; selection-color: {t.on_accent.name()};
}}
#fomodCompanion QMenu::item {{ padding: 5px 18px; }}
#fomodCompanion QMenu::item:selected {{ background: {t.accent.name()}; color: {t.on_accent.name()}; }}
#fomodCompanion QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
#fomodCompanion QScrollBar::handle:vertical {{ background: {rgba(t.text, 60)}; border-radius: 4px; min-height: 30px; }}
#fomodCompanion QScrollBar::handle:vertical:hover {{ background: {rgba(t.text, 100)}; }}
#fomodCompanion QScrollBar::add-line, #fomodCompanion QScrollBar::sub-line {{ height: 0px; width: 0px; }}
#fomodCompanion QScrollBar::add-page, #fomodCompanion QScrollBar::sub-page {{ background: transparent; }}
#fomodCompanion QSplitter::handle {{ background: transparent; }}
#fomodCompanion QLineEdit#search {{
    padding: 6px 10px; border: 1px solid {t.border.name()}; border-radius: 8px;
    background: {t.surface.name()}; placeholder-text-color: {t.faint.name()};
}}
#fomodCompanion QLineEdit#search:focus {{ border: 1px solid {t.accent.name()}; }}
#fomodCompanion QPushButton#tool, #fomodCompanion QToolButton#tool, #fomodCompanion QComboBox#tool {{
    padding: 5px 12px; border: 1px solid {t.border.name()}; border-radius: 8px; background: {t.surface.name()};
}}
#fomodCompanion QPushButton#tool:hover, #fomodCompanion QToolButton#tool:hover {{ background: {t.raised.name()}; }}
#fomodCompanion QPushButton#tool:checked {{ background: {t.accent.name()}; color: {t.on_accent.name()}; border-color: {t.accent.name()}; }}
#fomodCompanion QPushButton#tool:disabled {{ color: {t.faint.name()}; }}
#fomodCompanion QTreeWidget#installs, #fomodCompanion QTreeWidget#compareTree, #fomodCompanion QTreeWidget#historyTree {{
    border: 1px solid {t.border.name()}; border-radius: 10px; background: {t.surface.name()};
    alternate-background-color: {(t.alternate or t.raised).name()}; padding: 4px; color: {t.text.name()};
}}
#fomodCompanion QTreeWidget#installs::item, #fomodCompanion QTreeWidget#compareTree::item, #fomodCompanion QTreeWidget#historyTree::item {{ padding: 4px 2px; border: none; }}
#fomodCompanion QTreeWidget#installs::item:hover, #fomodCompanion QTreeWidget#compareTree::item:hover, #fomodCompanion QTreeWidget#historyTree::item:hover {{ background: {hover}; }}
#fomodCompanion QTreeWidget#installs::item:selected, #fomodCompanion QTreeWidget#compareTree::item:selected, #fomodCompanion QTreeWidget#historyTree::item:selected {{
    background: {selected}; color: {t.text.name()};
}}
#fomodCompanion QTreeWidget#compareTree QHeaderView::section {{
    background: {t.surface.name()}; color: {t.faint.name()}; border: none; border-bottom: 1px solid {t.border.name()};
    padding: 4px 6px; font-weight: 600;
}}
#fomodCompanion QCheckBox {{ spacing: 6px; }}
#fomodCompanion QPushButton#action {{
    padding: 3px 10px; border: 1px solid {rgba(t.accent, 120)}; border-radius: 6px;
    color: {t.accent_text.name()}; background: transparent;
}}
#fomodCompanion QPushButton#action:hover {{ background: {rgba(t.accent, _HOVER_ALPHA)}; }}
"""


def _own_rules(t: Tokens) -> str:
    """Companion's own pieces, which no MO2 theme knows: chips, the filter segments, side cards and stripes, card text."""

    return f"""
#fomodCompanion QLabel#title {{ font-size: 18pt; font-weight: 600; }}
#fomodCompanion QLabel#chip, #fomodCompanion QLabel#chipAlert, #fomodCompanion QLabel#chipMark {{
    border-radius: 10px; padding: 2px 10px; font-weight: 600;
}}
#fomodCompanion QLabel#chip {{ background: {rgba(t.text, 22)}; color: {t.text.name()}; }}
#fomodCompanion QLabel#chipAlert {{ background: {rgba(t.missed, t.alarm_alpha)}; color: {t.missed.name()}; }}
#fomodCompanion QLabel#chipMark {{ background: {rgba(t.mark, _CHIP_ALPHA)}; color: {t.mark.name()}; }}
#fomodCompanion QLabel#chipMasters {{ background: {rgba(t.masters, _CHIP_ALPHA)}; color: {t.masters.name()}; border-radius: 10px; padding: 2px 10px; font-weight: 600; }}
#fomodCompanion QLabel#chipOff {{ background: {rgba(t.off, _CHIP_ALPHA)}; color: {t.off.name()}; border-radius: 10px; padding: 2px 10px; font-weight: 600; }}
#fomodCompanion QToolButton#segment {{
    padding: 5px 14px; border: 1px solid {t.border.name()}; background: {t.surface.name()};
    border-radius: 0px; margin: 0px;
}}
#fomodCompanion QToolButton#segment[edge="first"] {{ border-top-left-radius: 8px; border-bottom-left-radius: 8px; }}
#fomodCompanion QToolButton#segment[edge="last"] {{ border-top-right-radius: 8px; border-bottom-right-radius: 8px; }}
#fomodCompanion QToolButton#segment:hover {{ background: {t.raised.name()}; }}
#fomodCompanion QToolButton#segment:checked {{
    background: {t.accent.name()}; color: {t.on_accent.name()}; border-color: {t.accent.name()};
}}
#fomodCompanion QToolButton#kindChip {{
    padding: 3px 12px; border: 1px solid {t.border.name()}; border-radius: 12px; background: {t.surface.name()};
}}
#fomodCompanion QToolButton#kindChip:hover {{ background: {t.raised.name()}; }}
#fomodCompanion QToolButton#kindChip:checked {{
    background: {t.accent.name()}; color: {t.on_accent.name()}; border-color: {t.accent.name()}; font-weight: 600;
}}
#fomodCompanion QLabel#historySummary {{ font-weight: 600; }}
#fomodCompanion QToolButton#tool::menu-indicator {{ image: none; width: 0px; }}

#fomodCompanion QLabel#compareSummary {{ font-weight: 600; }}
#fomodCompanion QScrollArea#side, #fomodCompanion QWidget#sideHolder {{ background: transparent; }}
#fomodCompanion QFrame#section {{
    background: {t.surface.name()}; border: 1px solid {t.border.name()}; border-radius: 10px;
}}
#fomodCompanion QFrame#section[kind="missed"] {{ border-left: {6 if t.silver is not None else 4}px solid {t.missed.name()}; }}
#fomodCompanion QFrame#section[kind="revisit"] {{ border-left: 4px solid {t.mark.name()}; }}
#fomodCompanion QFrame#section[kind="off"] {{ border-left: 4px solid {t.off.name()}; }}
#fomodCompanion QFrame#section[kind="masters"] {{ border-left: 4px solid {t.masters.name()}; }}
#fomodCompanion QFrame#section[kind="cross"] {{ border-left: 4px solid {(t.stripe or t.accent).name()}; }}
#fomodCompanion QFrame#section[kind="changes"] {{ border-left: 4px solid {(t.stripe or t.accent).name()}; }}
#fomodCompanion QLabel#sectionTitle {{ font-size: 11pt; font-weight: 600; border: none; }}
#fomodCompanion QLabel#hint {{ color: {t.faint.name()}; border: none; }}
#fomodCompanion QWidget#emptyState, #fomodCompanion QWidget#emptyState QLabel {{ background: transparent; }}
#fomodCompanion QLabel#emptyTitle {{ color: {t.faint.name()}; border: none; }}
#fomodCompanion QLabel#cardTitle {{ font-weight: 600; border: none; }}
#fomodCompanion QLabel#cardLine {{ border: none; }}
#fomodCompanion QLabel#cardMark {{ color: {t.mark.name()}; font-weight: 600; border: none; }}
#fomodCompanion QLabel#cardAlert {{ color: {t.missed.name()}; border: none; }}
#fomodCompanion QLabel#cardMasters {{ color: {t.masters.name()}; font-weight: 600; border: none; }}
#fomodCompanion QLabel#cardPicked {{ color: {t.picked.name()}; font-weight: 600; border: none; }}
#fomodCompanion QLabel#cardOff {{ color: {t.off.name()}; font-weight: 600; border: none; }}
#fomodCompanion QLabel#cardNew {{ color: {t.accent_text.name()}; font-weight: 600; border: none; }}
#fomodCompanion QFrame#card {{ border: none; border-top: 1px solid {rgba(t.border, 160)}; }}
#fomodCompanion QFrame#card[first="true"] {{ border-top: none; }}
#fomodCompanion QToolButton#crossMod, #fomodCompanion QToolButton#fold {{ border: none; background: transparent; }}
#fomodCompanion QToolButton#sectionFold {{ border: none; background: transparent; padding: 0px; margin-top: 2px; }}
#fomodCompanion QToolButton#sectionFold:hover {{ background: {rgba(t.accent, 40)}; border-radius: 4px; }}
#fomodCompanion QToolButton#crossMod:hover {{ background: {rgba(t.accent, 28)}; border-radius: 4px; }}
#fomodCompanion QLabel#wrapText {{ border: none; background: transparent; }}
#fomodCompanion QToolButton#fold QLabel#wrapText, #fomodCompanion QLabel#wrapText[tone="faint"] {{ color: {t.faint.name()}; }}
#fomodCompanion QLabel#wrapText[tone="picked"] {{ color: {t.picked.name()}; }}
#fomodCompanion QLabel#wrapText[tone="missed"] {{ color: {t.missed.name()}; }}
#fomodCompanion QLabel#error {{ color: {t.missed.name()}; }}
"""


def _brand_control_rules(t: Tokens) -> str:
    """The sayngwyn theme's standard controls. Red (the accent) is only for what is selected; every everyday button
    is an outline in silver that brightens on hover and turns bone white when it has the keyboard focus. Controls
    follow the sayngwyn-mod-style skill's "Controls in every window", as the Sayngwyn MO2 theme does: checkboxes are
    a silver box on the raised colour with a bone tick, hovered menu and list items take the selected-row red,
    scrollbar handles are silver, tooltips are raised."""

    silver = t.silver or QColor(_BRAND_SILVER)       # only called when the theme has one
    buttons = ("QPushButton", "QPushButton#action", "QPushButton#tool", "QToolButton#tool")   # (dropdowns are inputs)

    def each(state: str = "") -> str:
        return ", ".join(f"#fomodCompanion {button}{state}" for button in buttons)

    plain, hover, focus, pressed = each(), each(":hover"), each(":focus"), each(":pressed")
    deep =(t.deep or QColor(_BRAND_DEEP)).name()
    tick = (ASSETS / CHECK_FILE).as_posix()
    tick_off = (ASSETS / CHECK_OFF_FILE).as_posix()
    return f"""
#fomodCompanion QMenu {{ background: {t.raised.name()}; border: 1px solid {t.border.name()}; }}
#fomodCompanion QMenu::item:selected {{ background: {deep}; color: {t.text.name()}; }}
#fomodCompanion QComboBox QAbstractItemView {{ selection-background-color: {deep}; selection-color: {t.text.name()}; }}
#fomodCompanion QToolTip {{ background: {t.raised.name()}; color: {t.text.name()}; border: 1px solid {t.border.name()}; padding: 3px 6px; }}
#fomodCompanion QCheckBox::indicator {{
    width: 14px; height: 14px; border: 1px solid {rgba(silver, 170)}; border-radius: 3px; background: {t.raised.name()};
}}
#fomodCompanion QCheckBox::indicator:hover {{ border: 1px solid {rgba(silver, 235)}; }}
#fomodCompanion QCheckBox::indicator:focus {{ border: 1px solid {t.text.name()}; }}
#fomodCompanion QCheckBox::indicator:checked {{ image: url("{tick}"); }}
#fomodCompanion QCheckBox::indicator:disabled {{ border: 1px solid {rgba(silver, 55)}; }}
#fomodCompanion QCheckBox::indicator:checked:disabled {{ image: url("{tick_off}"); }}
#fomodCompanion QCheckBox:disabled {{ color: {t.faint.name()}; }}
#fomodCompanion QScrollBar::handle:vertical, #fomodCompanion QScrollBar::handle:horizontal {{ background: {rgba(silver, 89)}; }}
#fomodCompanion QScrollBar::handle:vertical:hover, #fomodCompanion QScrollBar::handle:horizontal:hover {{ background: {rgba(silver, 153)}; }}
#fomodCompanion QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
#fomodCompanion QScrollBar::handle:horizontal {{ border-radius: 4px; min-width: 30px; }}
{plain} {{ color: {t.text.name()}; border: 1px solid {rgba(silver, 115)}; }}
{hover} {{ border: 1px solid {rgba(silver, 235)}; background: {rgba(silver, 28)}; }}
{pressed} {{ background: {rgba(silver, 52)}; }}
{focus} {{ border: 1px solid {t.text.name()}; }}
{each(":disabled")} {{ color: {t.faint.name()}; border: 1px solid {rgba(silver, 55)}; }}
""" + _brand_usage_rules(t)


def _brand_usage_rules(t: Tokens) -> str:
    """The rest of the skill's "Controls in every window", as the Sayngwyn MO2 theme draws them: the selected row's
    bone edge (on the selected row only: borders on every row hid the rows' own colours), toggled-on buttons, the
    default button, inputs, headers, progress bars, sliders and group boxes."""

    bone, deep, deep_hover = t.text.name(), (t.deep or QColor(_BRAND_DEEP)).name(), (t.deep_hover or QColor(_BRAND_DEEP_HOVER)).name()
    lists = ("QAbstractItemView", "QTreeWidget#installs", "QTreeWidget#compareTree", "QTreeWidget#historyTree")

    def rows(state: str) -> str:
        return ", ".join(f"#fomodCompanion {view}::item{state}" for view in lists)

    def trees(state: str) -> str:   # Companion's own lists, whose rows are padded 4px 2px
        return ", ".join(f"#fomodCompanion {view}::item{state}" for view in lists[1:])

    inputs = ("QLineEdit", "QLineEdit#search", "QPlainTextEdit", "QComboBox", "QComboBox#tool")

    def boxes(state: str) -> str:
        return ", ".join(f"#fomodCompanion {box}{state}" for box in inputs)

    headers = ("QHeaderView::section", "QTreeWidget#compareTree QHeaderView::section")

    def heads(state: str = "") -> str:
        return ", ".join(f"#fomodCompanion {head}{state}" for head in headers)

    return f"""
{trees(":first")} {{ padding-left: 5px; }}
{rows(":selected")} {{
    background: {deep}; color: {bone}; border-top: 1px solid {bone}; border-bottom: 1px solid {bone};
}}
{trees(":selected")} {{ padding-top: 3px; padding-bottom: 3px; }}
{rows(":selected:first")} {{ border-left: 3px solid {bone}; }}
{trees(":selected:first")} {{ padding-left: 2px; }}
#fomodCompanion QPushButton:checked, #fomodCompanion QToolButton:checked, #fomodCompanion QPushButton#tool:checked {{
    background: {t.accent.name()}; color: #ffffff; border: 1px solid {t.accent.name()};
}}
#fomodCompanion QToolButton#sectionFold:checked, #fomodCompanion QToolButton#fold:checked {{ background: transparent; border: none; }}
#fomodCompanion QPushButton:default, #fomodCompanion QPushButton#tool:default {{ background: transparent; border: 1px solid {bone}; }}
{boxes("")} {{ background: {t.surface.name()}; color: {bone}; border: 1px solid {t.border.name()}; }}
{boxes(":hover")} {{ border: 1px solid {_BRAND_STRIPE}; }}
{boxes(":focus")} {{ border: 1px solid {bone}; }}
{boxes(":disabled")} {{ background: {t.window.name()}; color: {t.faint.name()}; border: 1px solid {t.border.name()}; }}
{heads()} {{
    background: {t.raised.name()}; color: {bone}; border: none; border-right: 1px solid {t.border.name()};
    border-bottom: 1px solid {t.border.name()}; padding: 4px 6px; font-weight: 600;
}}
{heads(":hover")} {{ background: {deep_hover}; }}
#fomodCompanion QProgressBar {{ background: {t.surface.name()}; color: {bone}; }}
#fomodCompanion QProgressBar::chunk {{ background: {deep}; }}
#fomodCompanion QSlider::groove:horizontal {{ background: {t.border.name()}; height: 4px; border-radius: 2px; }}
#fomodCompanion QSlider::sub-page:horizontal {{ background: {deep}; border-radius: 2px; }}
#fomodCompanion QSlider::handle:horizontal {{ background: {bone}; width: 12px; margin: -5px 0; border-radius: 6px; }}
#fomodCompanion QGroupBox {{ border: 1px solid {t.border.name()}; border-radius: 6px; margin-top: 10px; padding-top: 6px; }}
#fomodCompanion QGroupBox::title {{ color: {bone}; font-weight: 600; subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
"""


def _brand_own_rules(t: Tokens) -> str:
    """The sayngwyn theme's own pieces: the one primary button of an area in the accent red, the fold buttons'
    silver hover, and the missed-patch pill's alarm outline."""

    silver = t.silver or QColor(_BRAND_SILVER)
    primary = ("#fomodCompanion QPushButton#action[primary=\"true\"], #fomodCompanion QPushButton#tool[primary=\"true\"]")
    primary_hover = primary.replace("\"]", "\"]:hover")
    primary_focus = primary.replace("\"]", "\"]:focus")
    deeper = _BRAND_ACCENT_HOVER
    return f"""
{primary} {{ background: {t.accent.name()}; color: {t.on_accent.name()}; border: 1px solid {t.accent.name()}; }}
{primary_hover} {{ background: {deeper}; border: 1px solid {deeper}; }}
{primary_focus} {{ border: 1px solid {t.text.name()}; }}
#fomodCompanion QToolButton#sectionFold:hover, #fomodCompanion QToolButton#crossMod:hover {{ background: {rgba(silver, 34)}; border-radius: 4px; }}
#fomodCompanion QLabel#chipAlert {{ border: 1px solid {t.missed.name()}; padding: 1px 9px; }}
"""
