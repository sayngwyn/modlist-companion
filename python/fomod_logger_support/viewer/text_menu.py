"""The right-click menu of the window's text boxes: Qt's own (Undo, Cut, Copy, Paste...), shown by the window so that in
the window's own themes its icons take the theme's text colour. Qt picks those stock icons to match Windows' light or
dark mode, not the window's theme, so on one of the two they would barely show. "Match MO2" leaves them as they are."""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QSize, Qt, qWarning
from PyQt6.QtGui import QColor, QIcon, QPainter
from PyQt6.QtWidgets import QLineEdit, QPlainTextEdit, QStyle, QWidget


def tinted(icon: QIcon, colour: QColor, disabled: QColor, size: int, ratio: float) -> QIcon:
    """``icon`` redrawn in one ``colour`` (``disabled`` for a greyed-out item), keeping its shape."""

    result = QIcon()
    for mode, paint in ((QIcon.Mode.Normal, colour), (QIcon.Mode.Active, colour), (QIcon.Mode.Selected, colour),
                        (QIcon.Mode.Disabled, disabled)):
        pixmap = icon.pixmap(QSize(size, size), ratio)
        if pixmap.isNull():
            continue
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)   # the colour, only where the icon is
        painter.fillRect(pixmap.rect(), colour if mode != QIcon.Mode.Disabled else disabled)
        painter.end()
        result.addPixmap(pixmap, mode)
    return result


def _theme_owner(widget: QWidget):
    """The Companion window a text box belongs to (it, or a dialog of it): the one that knows the theme."""

    current: QWidget | None = widget
    while current is not None:
        if hasattr(current, "_theme_mode") and hasattr(current, "_tokens"):
            return current
        current = current.parentWidget()
    return None


def use_themed_menu(box: QLineEdit | QPlainTextEdit) -> None:
    """Show ``box``'s right-click menu through the window, with its icons in the theme's colours."""

    box.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    box.customContextMenuRequested.connect(lambda pos, b=box: _show(b, pos))


def _show(box: QLineEdit | QPlainTextEdit, pos: QPoint) -> None:
    try:
        menu = box.createStandardContextMenu()
        if menu is None:
            return
        owner = _theme_owner(box)
        tokens = getattr(owner, "_tokens", None)
        if tokens is not None and getattr(owner, "_theme_mode", "auto") != "auto":   # "Match MO2" leaves MO2's icons
            style = box.style()
            size = style.pixelMetric(QStyle.PixelMetric.PM_SmallIconSize) if style is not None else 16
            for action in menu.actions():
                if not action.icon().isNull():
                    action.setIcon(tinted(action.icon(), tokens.text, tokens.faint, size, box.devicePixelRatioF()))
        # Qt gives the menu no parent: parent it to the box (so Python doesn't delete it before it shows, and the
        # window's sheet reaches it), and let it go when it closes.
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.setParent(box, menu.windowFlags())
        origin = box.viewport() if isinstance(box, QPlainTextEdit) else box      # a text box's position is in its viewport
        menu.popup((origin or box).mapToGlobal(pos))
    except Exception as exc:  # a slot: a failure here must leave nothing half-done
        qWarning(f"Modlist Companion: could not show the right-click menu ({type(exc).__name__})")
