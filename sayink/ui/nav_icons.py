"""Sidebar navigation icons for the settings window."""

from __future__ import annotations

import math

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from sayink.ui.design_tokens import TEXT, TEXT_SEC


def nav_icon(shape: str, active: bool = False) -> QIcon:
    sz = 18
    pm = QPixmap(sz, sz)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Neutral when selected — left bar is the single blue accent device.
    c = QColor(TEXT if active else TEXT_SEC)
    pen = QPen(c, 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)

    if shape == "general":
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(3, 3, 12, 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(7, 7, 4, 4)
        notch = QPen(c, 2.5)
        notch.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(notch)
        for deg in range(0, 360, 45):
            rad = math.radians(deg)
            p.drawLine(
                int(9 + 5.5 * math.cos(rad)), int(9 + 5.5 * math.sin(rad)),
                int(9 + 7.5 * math.cos(rad)), int(9 + 7.5 * math.sin(rad)),
            )

    elif shape == "model":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        for x, y, w, h in [(2, 10, 3, 6), (6, 4, 3, 12), (10, 7, 3, 9), (14, 5, 3, 11)]:
            p.drawRoundedRect(x, y, w, h, 1, 1)

    elif shape == "polish":
        pen2 = QPen(c, 1.8)
        pen2.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen2)
        p.drawLine(3, 15, 12, 3)
        p.drawLine(12, 3, 15, 6)
        p.drawLine(15, 6, 6, 15)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(1, 1, 3, 3)
        p.drawEllipse(13, 0, 2, 2)

    elif shape == "about":
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(2, 2, 14, 14)
        thick = QPen(c, 2)
        thick.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(thick)
        p.drawLine(9, 8, 9, 13)
        p.drawPoint(9, 5)

    p.end()
    return QIcon(pm)
