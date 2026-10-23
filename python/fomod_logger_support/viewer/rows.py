"""Row tints that survive the window's stylesheet.

The window's own themes give list rows a ``border: none`` rule. On Qt's Windows style (MO2 2.5.2's), that rule is what
makes rows draw their own text colours (picked, switched off, missing masters): without it the style draws every row in
the plain text colour. But a styled row also drops the row's own background brush, so the search-match tint vanished.
This delegate paints that brush itself, under the row, before the usual drawing; selection and hover still draw on top.
Every theme, "Match MO2" included, carries that row rule, so Qt never draws the brush itself and it isn't drawn twice.
"""

from __future__ import annotations

from PyQt6.QtCore import QModelIndex, Qt
from PyQt6.QtGui import QBrush, QPainter
from PyQt6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem


class TintDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter | None, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        tint = index.data(Qt.ItemDataRole.BackgroundRole)
        if painter is not None and isinstance(tint, QBrush) and tint.style() != Qt.BrushStyle.NoBrush:
            painter.fillRect(option.rect, tint)
        super().paint(painter, option, index)
