"""Stitch Desktop Pro building blocks for SayInk settings UI."""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QSize, QPointF, QRectF, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QColor, QKeyEvent, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel,
    QCheckBox, QPushButton, QRadioButton, QScrollArea, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

from sayink.ui import design_tokens as tok
# Only theme-independent layout tokens may be bound at import time. Colors
# change with the light/dark axis, so they are always read live as ``tok.X``;
# a widget built after a theme switch would otherwise inherit stale colors.
from sayink.ui.design_tokens import (
    PAGE_MARGIN_H,
    PAGE_MARGIN_V,
    RADIUS_MD,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
)

# ── Style fragments (rebuilt from active token axis via reload_styles) ──

SECTION_LABEL = ""
PAGE_TITLE = ""
PAGE_SUBTITLE = ""
FOOTNOTE = ""
GROUP_STYLE = ""
HERO_CARD_STYLE = ""
ROW_RADIO_STYLE = ""
NAV_BTN_STYLE = ""
LINK_BTN_STYLE = ""
SUB_TAB_BAR_STYLE = ""
SUB_TAB_BTN_STYLE = ""


def reload_styles() -> None:
    """Rebuild style fragments from the currently activated design tokens."""
    global SECTION_LABEL, PAGE_TITLE, PAGE_SUBTITLE, FOOTNOTE
    global GROUP_STYLE, HERO_CARD_STYLE, ROW_RADIO_STYLE, NAV_BTN_STYLE, LINK_BTN_STYLE
    global SUB_TAB_BAR_STYLE, SUB_TAB_BTN_STYLE
    from sayink.ui import design_tokens as tok

    SECTION_LABEL = (
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 700;"
        f" padding: 0 2px 2px 2px; letter-spacing: 0;"
        f" background: transparent;"
    )
    PAGE_TITLE = (
        f"color: {tok.TEXT}; font-family: {tok.FONT_DISPLAY}; font-size: {tok.TYPE_TITLE_LG}px;"
        f" font-weight: 700; letter-spacing: 0; padding: 2px 0 0 0;"
    )
    PAGE_SUBTITLE = (
        f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_BODY_SM}px; padding: 0;"
        f" line-height: 1.45; letter-spacing: 0;"
    )
    FOOTNOTE = (
        f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.45;"
        f" padding: 0 2px 0 2px; letter-spacing: 0;"
    )
    GROUP_STYLE = f"""
    QFrame#settingsGroup {{
        background: {tok.SURFACE};
        border: 1px solid {tok.BORDER};
        border-radius: {tok.RADIUS_LG}px;
    }}
"""
    HERO_CARD_STYLE = GROUP_STYLE.replace("settingsGroup", "settingsHeroCard")
    ROW_RADIO_STYLE = f"""
    QRadioButton {{
        color: {tok.TEXT};
        spacing: 12px;
        font-size: {tok.TYPE_BODY}px;
        padding: 10px 14px;
        background: transparent;
        border: none;
    }}
    QRadioButton::indicator {{
        width: 20px;
        height: 20px;
        subcontrol-position: right center;
        subcontrol-origin: padding;
        margin-right: 4px;
        border-radius: 10px;
        border: 2px solid {tok.CONTROL_BORDER_HOVER};
        background: {tok.SURFACE};
    }}
    QRadioButton::indicator:checked {{
        border: 2px solid {tok.ACCENT};
        background: qradialgradient(
            cx:0.5, cy:0.5, radius:0.45, fx:0.5, fy:0.5,
            stop:0 {tok.ACCENT}, stop:0.52 {tok.ACCENT},
            stop:0.53 {tok.SURFACE}, stop:1 {tok.SURFACE}
        );
    }}
    QRadioButton::indicator:unchecked:hover {{
        border-color: {tok.ACCENT_FOCUS};
    }}
    QRadioButton:focus {{
        border: 2px solid {tok.ACCENT_FOCUS};
        border-radius: {tok.RADIUS_SM}px;
        padding: 8px 12px;
    }}
"""
    NAV_BTN_STYLE = f"""
    QPushButton#settingsNavBtn {{
        text-align: left;
        padding: 0 12px 0 12px;
        border: none;
        border-left: {tok.NAV_SELECTED_BAR_PX}px solid transparent;
        border-radius: {tok.RADIUS_MD}px;
        color: {tok.TEXT_SEC};
        font-size: {tok.TYPE_BODY_SM}px;
        font-weight: 400;
        background: transparent;
    }}
    QPushButton#settingsNavBtn:checked {{
        background: {tok.NAV_SELECTED_BG};
        border-left: {tok.NAV_SELECTED_BAR_PX}px solid {tok.ACCENT};
        color: {tok.ACCENT_TEXT};
        font-weight: 700;
    }}
    QPushButton#settingsNavBtn:hover:!checked {{
        background: {tok.ROW_HOVER};
        color: {tok.TEXT};
    }}
    QPushButton#settingsNavBtn:focus {{
        border: 2px solid {tok.ACCENT_FOCUS};
        border-left: {tok.NAV_SELECTED_BAR_PX}px solid {tok.ACCENT_FOCUS};
        padding: 0 14px 0 12px;
    }}
"""
    LINK_BTN_STYLE = f"""
    QPushButton {{
        color: {tok.TEXT_SEC}; background: transparent; border: none;
        font-size: {tok.TYPE_FOOTNOTE}px; text-align: left; padding: 4px 0;
    }}
    QPushButton:hover {{ color: {tok.TEXT}; }}
    QPushButton:focus {{
        outline: none;
        border-bottom: 1px solid {tok.CONTROL_BORDER_HOVER};
    }}
"""
    SUB_TAB_BAR_STYLE = f"""
    QFrame#settingsSubTabBar {{
        background: transparent;
        border: none;
        border-bottom: 1px solid {tok.BORDER};
    }}
"""
    SUB_TAB_BTN_STYLE = f"""
    QPushButton#settingsSubTabBtn {{
        text-align: center;
        padding: 10px 14px;
        border: none;
        border-bottom: 2px solid transparent;
        border-radius: 0;
        color: {tok.TEXT_DIM};
        font-size: {tok.TYPE_BODY_SM}px;
        font-weight: 400;
        background: transparent;
        min-width: 56px;
    }}
    QPushButton#settingsSubTabBtn:checked {{
        color: {tok.ACCENT_TEXT};
        font-weight: 700;
        border-bottom: 2px solid {tok.ACCENT};
        background: transparent;
    }}
    QPushButton#settingsSubTabBtn:hover:!checked {{
        color: {tok.TEXT};
    }}
    QPushButton#settingsSubTabBtn:focus {{
        outline: none;
        border-bottom: 2px solid {tok.ACCENT_FOCUS};
    }}
"""


reload_styles()


def section_header(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("viRole", "sectionLabel")
    lbl.setStyleSheet(SECTION_LABEL)
    return lbl


def page_header(title: str, subtitle: str = "") -> QWidget:
    wrap = QWidget()
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 4)
    lay.setSpacing(4)
    t = QLabel(title)
    t.setProperty("viRole", "pageTitle")
    t.setStyleSheet(PAGE_TITLE)
    lay.addWidget(t)
    if subtitle:
        lay.addWidget(page_intro(subtitle))
    return wrap


def page_intro(text: str) -> QLabel:
    """Subtitle-only intro — sidebar already shows the page title."""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setProperty("viRole", "pageSubtitle")
    lbl.setStyleSheet(PAGE_SUBTITLE)
    return lbl


def elide_middle(text: str, max_len: int = 44) -> str:
    if len(text) <= max_len:
        return text
    head = max_len // 2 - 2
    tail = max_len - head - 1
    return f"{text[:head]}…{text[-tail:]}"


class PageHero(QWidget):
    """Page title with optional inline status and subtitle."""

    def __init__(self, title: str, tags: list[str] | None = None, subtitle: str = "", parent=None):
        super().__init__(parent)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 8)
        self._root.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._title = QLabel(title)
        self._title.setProperty("viRole", "pageTitle")
        self._title.setStyleSheet(PAGE_TITLE)
        top.addWidget(self._title, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)

        self._inline_status = QLabel("")
        self._inline_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._inline_status.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 400;"
            f" background: transparent;"
        )
        # Reserve status width so 已关闭 ↔ 已开启 · … does not shove the title.
        self._inline_status.setMinimumWidth(120)
        self._inline_status.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        self._inline_status.setVisible(False)
        top.addWidget(self._inline_status, 0, Qt.AlignmentFlag.AlignVCenter)
        self._root.addLayout(top)

        self._subtitle = QLabel(subtitle)
        self._subtitle.setProperty("viRole", "pageSubtitle")
        self._subtitle.setWordWrap(True)
        self._subtitle.setStyleSheet(PAGE_SUBTITLE)
        self._subtitle.setVisible(bool(subtitle))
        self._root.addWidget(self._subtitle)

        if tags:
            self.set_tags(tags)

        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )

    def reapply_styles(self) -> None:
        from sayink.ui import design_tokens as tok

        self._title.setStyleSheet(PAGE_TITLE)
        self._subtitle.setStyleSheet(PAGE_SUBTITLE)
        self._inline_status.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 400;"
            f" background: transparent;"
        )

    def set_inline_status(self, text: str) -> None:
        cleaned = text.strip()
        self._inline_status.setText(cleaned)
        self._inline_status.setVisible(bool(cleaned))

    def set_tags(self, tags: list[str]) -> None:
        parts = [t.strip() for t in tags if t and t.strip()]
        self.set_subtitle(" · ".join(parts))

    def set_status(self, text: str) -> None:
        self.set_inline_status(text)

    def set_subtitle(self, text: str) -> None:
        self._subtitle.setText(text)
        visible = bool(text)
        self._subtitle.setVisible(visible)
        self._root.setContentsMargins(0, 0, 0, 6 if visible else 2)


def hero_card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("settingsHeroCard")
    frame.setStyleSheet(HERO_CARD_STYLE)
    return frame


def usage_tip_bar(text: str) -> QFrame:
    """Inline hint strip below hero (shortcut / how-to)."""
    frame = QFrame()
    frame.setObjectName("usageTipBar")
    lay = QHBoxLayout(frame)
    lay.setContentsMargins(16, 12, 16, 12)
    lbl = QLabel(text)
    lbl.setObjectName("usageTipBarText")
    lbl.setWordWrap(True)
    lay.addWidget(lbl)
    paint_usage_tip_bar(frame)
    return frame


def paint_usage_tip_bar(frame: QFrame) -> None:
    """Refresh a usage tip bar from the active token axis."""
    from sayink.ui import design_tokens as live

    frame.setStyleSheet(f"""
        QFrame#usageTipBar {{
            background: {live.SURFACE_PEARL};
            border: 1px solid {live.HAIRLINE};
            border-radius: {live.RADIUS_MD}px;
        }}
    """)
    for child in frame.findChildren(QLabel):
        child.setStyleSheet(
            f"color: {live.TEXT_SEC}; font-size: {live.TYPE_BODY_SM}px;"
            f" line-height: 1.45; background: transparent;"
        )


def polish_preview_content() -> QWidget:
    """Compact before/after sample matching the selected HTML reference."""
    frame = QWidget()
    frame.setObjectName("polishPreview")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(SPACE_MD, 4, SPACE_MD, SPACE_MD)
    lay.setSpacing(8)

    head = QLabel("效果示例（非实时结果）")
    head.setProperty("viRole", "polishPreviewHeading")
    # Same rank as the other section labels on the page; it used to be set
    # at TYPE_TITLE and outranked the headings above it.
    head.setStyleSheet(SECTION_LABEL)
    lay.addWidget(head)

    def _sample(label: str, body: str) -> None:
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(6)
        tag = QLabel(label)
        tag.setProperty("viRole", "polishPreviewLabel")
        tag.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 700;"
            f" background: transparent;"
        )
        row_lay.addWidget(tag, 0, Qt.AlignmentFlag.AlignTop)
        txt = QLabel(body)
        txt.setProperty("viRole", "polishPreviewText")
        txt.setWordWrap(True)
        txt.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; line-height: 1.5;"
            f" background: transparent;"
        )
        row_lay.addWidget(txt, 1)
        lay.addWidget(row)

    _sample("原文", "嗯那个就是把会议纪要整理一下吧")
    _sample("润色", "请整理会议纪要。")
    return frame


def kv_row_elided(key: str, value: str, *, max_len: int = 44) -> QWidget:
    display = elide_middle(value, max_len)
    row = kv_row(key, display, mono=True)
    if display != value:
        for child in row.findChildren(QLabel):
            if child.text() == display:
                child.setToolTip(value)
                break
    return row


def footnote(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("viRole", "footnote")
    lbl.setWordWrap(True)
    # Maximum: word-wrapped QLabels otherwise report a huge heightHint inside
    # QScrollArea and leave a tall empty band below the last line of text.
    lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    lbl.setStyleSheet(FOOTNOTE)
    return lbl


def info_callout(text: str, object_name: str = "infoCallout") -> QFrame:
    """Prototype v3 amber callout — text only, no leading glyph."""
    frame = QFrame()
    frame.setObjectName(object_name)
    lay = QHBoxLayout(frame)
    lay.setContentsMargins(12, 10, 12, 10)
    lay.setSpacing(0)
    lbl = QLabel(text)
    lbl.setObjectName(f"{object_name}Text")
    lbl.setWordWrap(True)
    lay.addWidget(lbl, 1)
    paint_info_callout(frame)
    return frame


def paint_info_callout(frame: QFrame) -> None:
    """Refresh an info_callout frame from the active token axis."""
    from sayink.ui import design_tokens as tok

    object_name = frame.objectName() or "infoCallout"
    frame.setStyleSheet(f"""
        QFrame#{object_name} {{
            background: {tok.AMBER_SOFT};
            border: 1px solid {tok.CALLOUT_BORDER};
            border-radius: {tok.RADIUS_MD}px;
        }}
    """)
    for child in frame.findChildren(QLabel):
        child.setStyleSheet(
            f"color: {tok.AMBER_TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.45;"
            f" background: transparent;"
        )


def empty_state(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setProperty("viRole", "emptyState")
    lbl.setStyleSheet(stylesheet_for_role("emptyState"))
    return lbl


def group_divider() -> QWidget:
    """Full-bleed hairline inside a settings card (prototype .row + .row)."""
    wrap = QWidget()
    wrap.setObjectName("settingsGroupDivider")
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    line = QFrame()
    line.setObjectName("settingsGroupDividerLine")
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {tok.DIVIDER_SOFT};")
    lay.addWidget(line)
    return wrap


def recolor_group_divider(wrap: QWidget) -> None:
    from sayink.ui import design_tokens as tok

    for line in wrap.findChildren(QFrame):
        if line.objectName() == "settingsGroupDividerLine":
            line.setStyleSheet(f"background: {tok.DIVIDER_SOFT};")


def stylesheet_for_role(role: object) -> str | None:
    """Map a viRole to a live token stylesheet. None means leave the widget alone."""
    from sayink.ui import design_tokens as live

    if role == "pageTitle":
        return PAGE_TITLE
    if role == "pageSubtitle":
        return PAGE_SUBTITLE
    if role == "sectionLabel":
        return SECTION_LABEL
    if role == "rowTitle":
        return (
            f"color: {live.TEXT}; font-size: {live.TYPE_BODY_SM}px; font-weight: 400;"
            f" background: transparent;"
        )
    if role == "rowSubtitle":
        return (
            f"color: {live.TEXT_DIM}; font-size: {live.TYPE_FOOTNOTE}px; line-height: 1.4;"
            f" background: transparent;"
        )
    if role == "fieldLabel":
        return (
            f"color: {live.TEXT_SEC}; font-size: {live.TYPE_FOOTNOTE}px; font-weight: 400;"
            f" background: transparent;"
        )
    if role == "hint":
        return (
            f"color: {live.TEXT_DIM}; font-size: {live.TYPE_FOOTNOTE}px; line-height: 1.4;"
            f" background: transparent;"
        )
    if role == "footnote":
        return FOOTNOTE
    if role == "polishPreviewHeading":
        return (
            f"color: {live.TEXT}; font-size: {live.TYPE_TITLE}px; font-weight: 700;"
            f" background: transparent;"
        )
    if role == "polishPreviewLabel":
        return (
            f"color: {live.TEXT_DIM}; font-size: {live.TYPE_BODY_SM}px; font-weight: 700;"
            f" background: transparent;"
        )
    if role == "polishPreviewText":
        return (
            f"color: {live.TEXT_SEC}; font-size: {live.TYPE_BODY_SM}px; line-height: 1.5;"
            f" background: transparent;"
        )
    if role == "kvKey":
        return (
            f"color: {live.TEXT}; font-size: {live.TYPE_BODY_SM}px; font-weight: 700;"
            f" min-width: 80px; background: transparent;"
        )
    if role == "kvValue":
        return (
            f"color: {live.TEXT_DIM}; font-size: {live.TYPE_BODY_SM}px; background: transparent;"
        )
    if role == "kvValueMono":
        return (
            f"color: {live.TEXT_DIM}; font-size: {live.TYPE_FOOTNOTE}px;"
            f" font-family: {live.FONT_MONO}; background: transparent;"
        )
    if role == "pickTitle":
        return (
            f"color: {live.TEXT}; font-size: {live.TYPE_BODY_SM}px; font-weight: 700;"
            f" background: transparent;"
        )
    if role == "pickSubtitle":
        return (
            f"color: {live.TEXT_SEC}; font-size: {live.TYPE_FOOTNOTE}px; line-height: 1.35;"
            f" background: transparent;"
        )
    if role == "emptyState":
        return (
            f"color: {live.TEXT_DIM}; font-size: {live.TYPE_BODY_SM}px;"
            f" padding: 20px 12px; background: transparent;"
        )
    return None


def reapply_subtree(root: QWidget) -> None:
    """Broadcast theme refresh to widgets that already know how to restyle."""
    from sayink.ui import design_tokens as live
    from sayink.ui import settings_styles as ss

    for obj in root.findChildren(QWidget):
        hook = getattr(obj, "reapply_styles", None)
        if callable(hook):
            hook()
        if isinstance(obj, SwitchControl):
            obj.update()

    for frame in root.findChildren(QFrame):
        name = frame.objectName() or ""
        if name == "settingsGroup":
            frame.setStyleSheet(GROUP_STYLE)
        elif name.endswith("Callout") or name == "infoCallout":
            paint_info_callout(frame)
        elif name == "usageTipBar":
            paint_usage_tip_bar(frame)

    for wrap in root.findChildren(QWidget):
        if wrap.objectName() == "settingsGroupDivider":
            recolor_group_divider(wrap)

    for label in root.findChildren(QLabel):
        css = stylesheet_for_role(label.property("viRole"))
        if css:
            label.setStyleSheet(css)
        elif label.objectName() == "settingsGroupTitle":
            label.setStyleSheet(
                f"color: {live.TEXT_DIM}; font-size: {live.TYPE_FOOTNOTE}px; font-weight: 700;"
                f" padding: 0 2px 2px 2px; background: transparent;"
                f" letter-spacing: 0;"
            )

    for btn in root.findChildren(QPushButton):
        name = btn.objectName() or ""
        if name in ("settingsNavBtn", "deviceSelectionLink", "themeModeSegBtn"):
            continue
        role = btn.property("viBtn")
        if role == "primary":
            btn.setStyleSheet(ss.BTN_PRIMARY)
        elif role == "ghostSm":
            btn.setStyleSheet(ss.BTN_GHOST_SM)
        elif role == "accentSm":
            btn.setStyleSheet(ss.BTN_ACCENT_SM)


def settings_group() -> QFrame:
    frame = QFrame()
    frame.setObjectName("settingsGroup")
    frame.setStyleSheet(GROUP_STYLE)
    return frame


def settings_section(
    title: str,
    group: QFrame,
    *,
    header_action: QWidget | None = None,
) -> QWidget:
    """Section title above a soft white settings container.

    Optional ``header_action`` sits on the title row (e.g. 恢复默认).
    """
    wrap = QWidget()
    wrap.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8 if title else 0)
    if title:
        hdr = QLabel(title)
        hdr.setObjectName("settingsGroupTitle")
        hdr.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 700;"
            f" padding: 2px 2px 2px 2px; background: transparent;"
            f" letter-spacing: 0;"
        )
        if header_action is None:
            lay.addWidget(hdr)
        else:
            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(8)
            head.addWidget(hdr, 0, Qt.AlignmentFlag.AlignVCenter)
            head.addStretch(1)
            head.addWidget(header_action, 0, Qt.AlignmentFlag.AlignVCenter)
            lay.addLayout(head)
    lay.addWidget(group)
    return wrap


def settings_tab_pane(*sections: QWidget) -> QWidget:
    """Vertical stack of settings sections for one sub-tab page."""
    pane = QWidget()
    pane.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    lay = QVBoxLayout(pane)
    lay.setContentsMargins(0, SPACE_SM, 0, 0)
    lay.setSpacing(SPACE_LG + SPACE_XS)
    lay.setAlignment(Qt.AlignmentFlag.AlignTop)
    for section in sections:
        lay.addWidget(section)
    lay.addStretch(1)
    return pane


class SettingsSubTabs(QWidget):
    """Underline sub-tabs + stacked panes for settings pages (录音 / 音频 / 偏好)."""

    tab_changed = pyqtSignal(int)

    def __init__(
        self,
        labels: list[str],
        pages: list[QWidget],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        if len(labels) != len(pages) or not labels:
            raise ValueError("labels and pages must be non-empty and equal length")
        self._labels = list(labels)
        self._buttons: list[QPushButton] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("settingsSubTabBar")
        bar.setStyleSheet(SUB_TAB_BAR_STYLE)
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(0, 0, 0, 0)
        bar_lay.setSpacing(4)

        for i, label in enumerate(labels):
            btn = QPushButton(label)
            btn.setObjectName("settingsSubTabBtn")
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(SUB_TAB_BTN_STYLE)
            btn.setAccessibleName(f"通用子页签 {label}")
            btn.clicked.connect(lambda _checked=False, idx=i: self.set_current_index(idx))
            self._buttons.append(btn)
            bar_lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        bar_lay.addStretch(1)
        root.addWidget(bar)

        self._stack = QStackedWidget()
        self._stack.setObjectName("settingsSubTabStack")
        self._stack.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        for page in pages:
            self._stack.addWidget(page)
        root.addWidget(self._stack)

        self._buttons[0].setChecked(True)
        self._stack.setCurrentIndex(0)
        self._fit_stack_to_current()

    def tab_labels(self) -> list[str]:
        return list(self._labels)

    def current_index(self) -> int:
        return self._stack.currentIndex()

    def set_current_index(self, index: int) -> None:
        if index < 0 or index >= len(self._labels):
            return
        if self._stack.currentIndex() != index:
            self._stack.setCurrentIndex(index)
        btn = self._buttons[index]
        if not btn.isChecked():
            btn.setChecked(True)
        self._fit_stack_to_current()
        self.tab_changed.emit(index)

    def _fit_stack_to_current(self) -> None:
        """Keep stack height to the active pane so short tabs leave no empty scroll."""
        page = self._stack.currentWidget()
        if page is None:
            return
        height = max(page.sizeHint().height(), page.minimumSizeHint().height())
        if height > 0:
            self._stack.setFixedHeight(height)

    def reapply_styles(self) -> None:
        bar = self.findChild(QFrame, "settingsSubTabBar")
        if bar is not None:
            bar.setStyleSheet(SUB_TAB_BAR_STYLE)
        for btn in self._buttons:
            btn.setStyleSheet(SUB_TAB_BTN_STYLE)


def option_row(title: str, subtitle: str = "") -> QWidget:
    col = QWidget()
    lay = QVBoxLayout(col)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setProperty("viRole", "rowTitle")
    t.setStyleSheet(
        f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 400; background: transparent;"
    )
    lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setProperty("viRole", "rowSubtitle")
        s.setWordWrap(True)
        s.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.4;"
            f" background: transparent;"
        )
        lay.addWidget(s)
    return col


def labeled_row(label: str, widget: QWidget, hint: str = "") -> QWidget:
    """Title (+ optional subtitle) on the left; control on the right."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
    lay.setSpacing(SPACE_SM)

    text_col = option_row(label, hint)
    for child in text_col.findChildren(QLabel):
        if child.text() == label:
            child.setBuddy(widget)
            break
    lay.addWidget(text_col, 1)
    lay.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


def stacked_field_row(label: str, widget: QWidget, hint: str = "") -> QWidget:
    """Vertical field — label, control, optional hint (prototype .field)."""
    row = QWidget()
    outer = QVBoxLayout(row)
    outer.setContentsMargins(SPACE_MD, 12, SPACE_MD, 12)
    outer.setSpacing(6)

    lbl = QLabel(label)
    lbl.setProperty("viRole", "fieldLabel")
    lbl.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 400; background: transparent;"
    )
    lbl.setBuddy(widget)
    outer.addWidget(lbl)
    outer.addWidget(widget)

    if hint:
        h = QLabel(hint)
        h.setProperty("viRole", "hint")
        h.setWordWrap(True)
        h.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.4; background: transparent;"
        )
        outer.addWidget(h)
    return row


def inline_action_row(widget: QWidget, *, margins: tuple[int, int, int, int] | None = None) -> QWidget:
    """Padded row for buttons or status inside a settings group."""
    row = QWidget()
    m = margins or (SPACE_MD, SPACE_SM, SPACE_MD, SPACE_MD)
    lay = QVBoxLayout(row)
    lay.setContentsMargins(*m)
    lay.setSpacing(8)
    lay.addWidget(widget)
    return row


def kv_row(key: str, value: str, *, mono: bool = False) -> QWidget:
    from sayink.ui.design_tokens import FONT_MONO

    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(SPACE_MD, 11, SPACE_MD, 11)
    lay.setSpacing(SPACE_MD)
    k = QLabel(key)
    k.setProperty("viRole", "kvKey")
    k.setStyleSheet(
        f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 700; min-width: 80px;"
    )
    k.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    v = QLabel(value)
    v.setProperty("viRole", "kvValueMono" if mono else "kvValue")
    v.setWordWrap(True)
    v.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if mono:
        v.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; font-family: {FONT_MONO};"
        )
    else:
        v.setStyleSheet(f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_BODY_SM}px;")
    lay.addWidget(k)
    lay.addWidget(v, 1)
    return row


class _SourceIcon(QWidget):
    """Simple painted icon for audio source choice cards."""

    def __init__(self, kind: str, active: bool = False, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._active = active
        self.setFixedSize(32, 32)

    def set_active(self, active: bool) -> None:
        self._active = active
        self.update()

    def reapply_styles(self) -> None:
        self.update()

    def paintEvent(self, event):
        from sayink.ui import design_tokens as tok

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(tok.TEXT if self._active else tok.TEXT_SEC)
        pen = QPen(color, 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        if self._kind == "mic":
            p.drawRoundedRect(11, 4, 10, 16, 5, 5)
            p.drawLine(16, 20, 16, 24)
            p.drawLine(12, 24, 20, 24)
            p.drawLine(16, 4, 16, 2)
        elif self._kind == "sys":
            p.drawRoundedRect(6, 10, 8, 12, 2, 2)
            p.drawLine(10, 22, 10, 26)
            p.drawLine(6, 26, 14, 26)
            p.drawLine(18, 12, 24, 8)
            p.drawLine(24, 8, 24, 20)
            p.drawLine(18, 20, 24, 20)
        elif self._kind == "continuous":
            p.setBrush(color)
            for x, h in ((8, 10), (14, 18), (20, 12), (26, 16)):
                y = 24 - h
                p.drawRoundedRect(x, y, 4, h, 2, 2)
            p.setBrush(Qt.BrushStyle.NoBrush)
        elif self._kind == "hotkey":
            p.drawRoundedRect(7, 9, 18, 14, 4, 4)
            p.drawLine(11, 14, 21, 14)
            p.drawLine(11, 18, 17, 18)
        else:
            p.drawRoundedRect(4, 8, 8, 12, 3, 3)
            p.drawLine(8, 20, 8, 23)
            p.drawRoundedRect(18, 6, 10, 14, 4, 4)
            p.drawLine(23, 20, 23, 24)
            p.drawLine(20, 24, 26, 24)
        p.end()


class ChoiceCard(QFrame):
    """Clickable option card — vertical icon layout (legacy horizontal picker)."""

    clicked = pyqtSignal()

    def __init__(
        self,
        title: str,
        subtitle: str,
        icon_kind: str,
        radio: QRadioButton,
        parent=None,
    ):
        super().__init__(parent)
        self._radio = radio
        self._icon_kind = icon_kind
        self._subtitle = subtitle
        self.setObjectName("ChoiceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        if subtitle:
            self.setAccessibleDescription(subtitle)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 14, 12, 14)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._icon = _SourceIcon(icon_kind)
        lay.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignHCenter)

        t = QLabel(title)
        t.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        t.setStyleSheet(
            f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 700; background: transparent;"
        )
        lay.addWidget(t)

        s = QLabel(subtitle)
        s.setWordWrap(True)
        s.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        s.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_CAPTION}px; line-height: 1.35; background: transparent;"
        )
        lay.addWidget(s)

        radio.toggled.connect(self._sync_style)
        self._sync_style(radio.isChecked())

    def _sync_style(self, checked: bool) -> None:
        self.setProperty("accessibleChecked", checked)
        self.setAccessibleDescription(
            f"{self._subtitle}，{'已选中' if checked else '未选中'}".strip("，")
        )
        self._icon.set_active(checked)
        if checked:
            self.setStyleSheet(f"""
                ChoiceCard {{
                    background: {tok.NAV_SELECTED_BG};
                    border: 1px solid {tok.HAIRLINE};
                    border-radius: {tok.RADIUS_MD}px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                ChoiceCard {{
                    background: {tok.SURFACE};
                    border: 1px solid {tok.HAIRLINE};
                    border-radius: {tok.RADIUS_MD}px;
                }}
                ChoiceCard:hover {{
                    border-color: {tok.ACCENT_FOCUS};
                }}
            """)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._radio.setChecked(True)
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._radio.setChecked(True)
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class VerticalChoiceCard(QFrame):
    """Full-width selectable row — used by legacy layouts / style tests."""

    clicked = pyqtSignal()

    def __init__(
        self,
        title: str,
        subtitle: str,
        icon_kind: str,
        radio: QRadioButton,
        parent=None,
    ):
        super().__init__(parent)
        self._radio = radio
        self._subtitle = subtitle
        self.setObjectName("VerticalChoiceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        if subtitle:
            self.setAccessibleDescription(subtitle)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)

        self._icon = _SourceIcon(icon_kind)
        lay.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet(
            f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY}px; font-weight: 700; background: transparent;"
        )
        text_col.addWidget(t)
        s = QLabel(subtitle)
        s.setWordWrap(True)
        s.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.35; background: transparent;"
        )
        text_col.addWidget(s)
        lay.addLayout(text_col, 1)

        radio.setVisible(False)
        radio.toggled.connect(self._sync_style)
        self._sync_style(radio.isChecked())

    def _sync_style(self, checked: bool) -> None:
        self.setProperty("accessibleChecked", checked)
        self.setAccessibleDescription(
            f"{self._subtitle}，{'已选中' if checked else '未选中'}".strip("，")
        )
        self._icon.set_active(checked)
        if checked:
            self.setStyleSheet(f"""
                VerticalChoiceCard {{
                    background: {tok.NAV_SELECTED_BG};
                    border: 1px solid {tok.HAIRLINE};
                    border-radius: {tok.RADIUS_MD}px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                VerticalChoiceCard {{
                    background: {tok.SURFACE};
                    border: 1px solid {tok.HAIRLINE};
                    border-radius: {tok.RADIUS_MD}px;
                }}
                VerticalChoiceCard:hover {{
                    border-color: {tok.ACCENT_FOCUS};
                }}
            """)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._radio.setChecked(True)
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._radio.setChecked(True)
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class CompactPickCard(QFrame):
    """Task choice with visible native radio semantics and a full-row hit target."""

    clicked = pyqtSignal()

    def __init__(
        self,
        title: str,
        subtitle: str,
        radio: QRadioButton,
        parent=None,
    ):
        super().__init__(parent)
        self._radio = radio
        self._subtitle = subtitle
        self.setObjectName("CompactPickCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        if subtitle:
            self.setAccessibleDescription(subtitle)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(4)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._title_label = QLabel(title)
        self._title_label.setProperty("viRole", "pickTitle")
        title_row = QHBoxLayout()
        title_row.addWidget(self._title_label, 1)
        radio.setParent(self)
        radio.setAccessibleName(title)
        radio.setAccessibleDescription(subtitle)
        radio.setFixedSize(28, 28)
        title_row.addWidget(radio)
        lay.addLayout(title_row)
        self._subtitle_label = QLabel(subtitle)
        self._subtitle_label.setProperty("viRole", "pickSubtitle")
        self._subtitle_label.setWordWrap(True)
        lay.addWidget(self._subtitle_label)

        radio.setVisible(True)
        self.setFocusProxy(radio)
        radio.toggled.connect(self._sync_style)
        self._sync_style(radio.isChecked())

    def reapply_styles(self) -> None:
        self._sync_style(self._radio.isChecked())

    def _sync_style(self, checked: bool) -> None:
        from sayink.ui import design_tokens as tok

        self.setProperty("accessibleChecked", checked)
        self.setAccessibleDescription(
            f"{self._subtitle}，{'已选中' if checked else '未选中'}".strip("，")
        )
        self._title_label.setStyleSheet(
            f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 700;"
            f" background: transparent;"
        )
        self._subtitle_label.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.35;"
            f" background: transparent;"
        )
        self._radio.setStyleSheet(f"""
            QRadioButton {{ padding: 2px; spacing: 0; border: 2px solid transparent; border-radius: 6px; background: transparent; }}
            QRadioButton:focus {{ border-color: {tok.ACCENT_FOCUS}; }}
            QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 8px;
                border: 1px solid {tok.CONTROL_BORDER_HOVER}; background: {tok.SURFACE}; }}
            QRadioButton::indicator:checked {{ border: 1px solid {tok.PRIMARY_CONTAINER};
                background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
                    stop:0 {tok.PRIMARY_CONTAINER}, stop:0.55 {tok.PRIMARY_CONTAINER},
                    stop:0.56 {tok.SURFACE}, stop:1 {tok.SURFACE}); }}
        """)
        # Ink wash selected row — no accent ring or left bar.
        if checked:
            self.setStyleSheet(f"""
                CompactPickCard {{
                    background: {tok.NAV_SELECTED_BG};
                    border: 1px solid {tok.HAIRLINE};
                    border-radius: {tok.RADIUS_MD}px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                CompactPickCard {{
                    background: {tok.SURFACE_PEARL};
                    border: 1px solid {tok.BORDER};
                    border-radius: {tok.RADIUS_MD}px;
                }}
                CompactPickCard:hover {{
                    border-color: {tok.CONTROL_BORDER};
                    background: {tok.SURFACE};
                }}
            """)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._radio.setChecked(True)
            self._radio.setFocus()
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._radio.setChecked(True)
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class AudioSourcePicker(QWidget):
    """Horizontal 3-column audio source picks (settings › general prototype)."""

    def __init__(
        self,
        mic_radio: QRadioButton,
        sys_radio: QRadioButton,
        mixed_radio: QRadioButton,
        parent=None,
    ):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        # Prototype .picker: padding 12px; gap 8px.
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        specs = [
            (mic_radio, "仅麦克风", "日常口述"),
            (sys_radio, "仅电脑播放", "会议回放"),
            (mixed_radio, "混合", "麦 + 系统"),
        ]
        for rb, title, sub in specs:
            lay.addWidget(CompactPickCard(title, sub, rb), 1)


class TriggerModePicker(QWidget):
    """Side-by-side trigger mode picks (settings › general prototype)."""

    def __init__(
        self,
        continuous_radio: QRadioButton,
        hotkey_radio: QRadioButton,
        parent=None,
    ):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        specs = [
            (continuous_radio, "持续转写", "按住启动 · Esc 或「结束」停止"),
            (hotkey_radio, "按住说话", "按住录音 · 松开后识别并输入"),
        ]
        for rb, title, sub in specs:
            lay.addWidget(CompactPickCard(title, sub, rb), 1)


class ThemeModeSegment(QWidget):
    """Segmented theme control — API mirrors QComboBox data helpers used by settings."""

    currentIndexChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ThemeModeCombo")
        self.setAccessibleName("主题模式")
        self._items = [("系统", "system"), ("浅色", "light"), ("暗色", "dark")]
        self._index = 0
        self._buttons: list[QPushButton] = []

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._host = QFrame()
        self._host.setObjectName("themeModeSegment")
        host_lay = QHBoxLayout(self._host)
        host_lay.setContentsMargins(3, 3, 3, 3)
        host_lay.setSpacing(0)

        for i, (label, _mode) in enumerate(self._items):
            btn = QPushButton(label)
            btn.setObjectName("themeModeSegBtn")
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, idx=i: self.setCurrentIndex(idx))
            self._buttons.append(btn)
            host_lay.addWidget(btn)
        root.addWidget(self._host)
        self._buttons[0].setChecked(True)
        self.reapply_styles()

    def findData(self, data) -> int:
        for i, (_label, mode) in enumerate(self._items):
            if mode == data:
                return i
        return -1

    def currentData(self, _role=None):
        return self._items[self._index][1]

    def currentIndex(self) -> int:
        return self._index

    def setCurrentIndex(self, index: int) -> None:
        if index < 0 or index >= len(self._items):
            return
        changed = index != self._index
        self._index = index
        btn = self._buttons[index]
        if not btn.isChecked():
            btn.setChecked(True)
        if changed and not self.signalsBlocked():
            self.currentIndexChanged.emit(index)

    def reapply_styles(self) -> None:
        from sayink.ui import design_tokens as tok

        self._host.setStyleSheet(f"""
            QFrame#themeModeSegment {{
                background: {tok.SURFACE_PEARL};
                border: 1px solid {tok.BORDER};
                border-radius: {tok.RADIUS_MD}px;
            }}
        """)
        for btn in self._buttons:
            btn.setStyleSheet(f"""
                QPushButton#themeModeSegBtn {{
                    border: 2px solid transparent;
                    background: transparent;
                    color: {tok.TEXT_SEC};
                    font-size: {tok.TYPE_FOOTNOTE}px;
                    font-weight: 400;
                    padding: 5px 10px;
                    min-height: 28px;
                    border-radius: 6px;
                }}
                QPushButton#themeModeSegBtn:checked {{
                    background: {tok.SURFACE};
                    color: {tok.TEXT};
                    font-weight: 700;
                    border: 2px solid transparent;
                }}
                QPushButton#themeModeSegBtn:hover:!checked {{
                    color: {tok.TEXT};
                }}
                QPushButton#themeModeSegBtn:focus {{ border-color: {tok.ACCENT_FOCUS}; }}
            """)


class _PlayCircle(QWidget):
    """Neutral circular play affordance for audio test row."""

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        d = min(self.width(), self.height())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(tok.TEXT_SEC))
        p.drawEllipse(0, 0, d, d)
        tri = QPolygonF([
            QPointF(d * 0.38, d * 0.28),
            QPointF(d * 0.38, d * 0.72),
            QPointF(d * 0.72, d * 0.50),
        ])
        p.setBrush(QColor("#FFFFFF"))
        p.drawPolygon(tri)
        p.end()


class _WaveformBadge(QWidget):
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(tok.TEXT_DIM)
        c.setAlpha(120)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        w, h = self.width(), self.height()
        bars = [0.35, 0.7, 0.5, 0.9, 0.45, 0.75, 0.55]
        gap = 3
        bar_w = max(2, (w - gap * (len(bars) - 1)) // len(bars))
        for i, ratio in enumerate(bars):
            bh = max(4, int(h * ratio))
            x = i * (bar_w + gap)
            y = (h - bh) // 2
            p.drawRoundedRect(x, y, bar_w, bh, 2, 2)
        p.end()


class WideTestButton(QWidget):
    """Reference-style test row: play · label · waveform."""

    clicked = pyqtSignal()

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("WideTestButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(52)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(text)
        self._enabled = True
        self._sync_style()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)

        self._play = _PlayCircle()
        self._play.setFixedSize(32, 32)
        lay.addWidget(self._play, 0, Qt.AlignmentFlag.AlignVCenter)

        self._label = QLabel(text)
        self._label.setStyleSheet(
            f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY}px; font-weight: 400; background: transparent;"
        )
        lay.addWidget(self._label, 1, Qt.AlignmentFlag.AlignVCenter)

        wave = _WaveformBadge()
        wave.setFixedSize(64, 26)
        lay.addWidget(wave, 0, Qt.AlignmentFlag.AlignVCenter)

    def _sync_style(self) -> None:
        if self._enabled:
            self.setStyleSheet(f"""
                WideTestButton {{
                    background: {tok.SURFACE};
                    border: none;
                    border-radius: {RADIUS_MD}px;
                }}
                WideTestButton:hover {{
                    background: {tok.SURFACE_PEARL};
                }}
            """)
        else:
            self.setStyleSheet(f"""
                WideTestButton {{
                    background: {tok.SURFACE};
                    border: none;
                    border-radius: {RADIUS_MD}px;
                    opacity: 0.55;
                }}
            """)

    def setEnabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._sync_style()
        super().setEnabled(enabled)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.isEnabled():
                self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def device_selection_link(text: str = "手动选择音频设备") -> QPushButton:
    """Accent text link — toggles advanced device panel (prototype v3)."""
    btn = QPushButton(text)
    btn.setObjectName("deviceSelectionLink")
    btn.setCheckable(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAccessibleName(text)
    paint_device_selection_link(btn)
    return btn


def paint_device_selection_link(btn: QPushButton) -> None:
    from sayink.ui import design_tokens as tok

    btn.setStyleSheet(f"""
        QPushButton#deviceSelectionLink {{
            color: {tok.ACCENT_TEXT}; background: transparent; border: none;
            font-size: {tok.TYPE_BODY_SM}px; font-weight: 400; text-align: left; padding: 4px 0;
        }}
        QPushButton#deviceSelectionLink:hover {{ color: {tok.ACCENT_TEXT_HOVER}; }}
        QPushButton#deviceSelectionLink:focus {{
            outline: none;
            border-bottom: 1px solid {tok.ACCENT_FOCUS};
        }}
    """)


def collapsible_toggle_btn(text: str) -> QPushButton:
    btn = QPushButton(f"▸  {text}")
    btn.setCheckable(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(f"""
        QPushButton {{
            color: {tok.TEXT_SEC};
            background: transparent;
            border: none;
            font-size: {tok.TYPE_BODY_SM}px;
            font-weight: 400;
            text-align: left;
            padding: 6px 4px;
        }}
        QPushButton:hover {{
            color: {tok.TEXT};
        }}
    """)

    def _sync(expanded: bool) -> None:
        mark = "▾" if expanded else "▸"
        btn.setText(f"{mark}  {text}")

    btn.toggled.connect(_sync)
    return btn


def _option_text_column(title: str, subtitle: str) -> QWidget:
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    # Row padding tuned for 36×20 switch + 13px title (was 12/12 for 42×24).
    lay.setContentsMargins(SPACE_MD, 10, SPACE_SM, 10)
    lay.setSpacing(0)
    lay.addWidget(option_row(title, subtitle), 1)
    return wrap


class RadioOptionRow(QWidget):
    """Full-width selectable row; click anywhere or press Space to select."""

    def __init__(self, title: str, subtitle: str, radio_button, parent=None):
        super().__init__(parent)
        self._radio = radio_button
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        if subtitle:
            self.setAccessibleDescription(subtitle)
        radio_button.setAccessibleName(title)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(_option_text_column(title, subtitle), 1)
        radio_button.setText("")
        radio_button.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(radio_button)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._radio.setChecked(True)
            self.setFocus()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._radio.setChecked(True)
            event.accept()
            return
        super().keyPressEvent(event)


class SwitchControl(QCheckBox):
    """Compact pill switch: gray off track, green on, white knob."""

    toggled = pyqtSignal(bool)

    # Slightly smaller than prototype 42×24 so switches don't dominate row text.
    _TRACK_W = 36
    _TRACK_H = 20
    _KNOB_MARGIN = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SwitchControl")
        self._checked = False
        self._knob_pos = 0.0
        self._hover = False
        self._anim: QPropertyAnimation | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty("accessibleRole", "CheckBox")
        self.setProperty("accessibleChecked", False)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "SwitchControl { background: transparent; border: none; }"
            "SwitchControl::indicator { width: 0; height: 0; border: none; }"
        )
        self.setFixedSize(self._TRACK_W, self._TRACK_H)

    def _track_color(self) -> QColor:
        from sayink.ui import design_tokens as tok

        # Prototype: off #E5E2E3 / on #16A34A (green); hover only slightly shifts.
        if self._checked or self._knob_pos > 0.5:
            return QColor(tok.TOGGLE_ON_HOVER if self._hover else tok.TOGGLE_ON)
        return QColor(tok.TOGGLE_OFF_HOVER if self._hover else tok.TOGGLE_OFF)

    def _get_knob_pos(self) -> float:
        return self._knob_pos

    def _set_knob_pos(self, value: float) -> None:
        self._knob_pos = value
        self.update()

    knob_pos = pyqtProperty(float, _get_knob_pos, _set_knob_pos)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool, *, animate: bool = True) -> None:
        checked = bool(checked)
        if self._checked == checked and (
            (checked and self._knob_pos >= 0.99) or (not checked and self._knob_pos <= 0.01)
        ):
            return
        self._checked = checked
        super().setChecked(checked)
        self.setProperty("accessibleChecked", checked)
        target = 1.0 if checked else 0.0
        if animate and self.isVisible():
            if self._anim is not None:
                self._anim.stop()
            self._anim = QPropertyAnimation(self, b"knob_pos", self)
            self._anim.setDuration(220)
            self._anim.setStartValue(self._knob_pos)
            self._anim.setEndValue(target)
            self._anim.setEasingCurve(QEasingCurve.Type.OutQuart)
            self._anim.start()
        else:
            self._knob_pos = target
            self.update()
        if not self.signalsBlocked():
            self.toggled.emit(checked)

    def blockSignals(self, block: bool) -> bool:
        return super().blockSignals(block)

    def set_hovered(self, hovered: bool) -> None:
        hovered = bool(hovered)
        if self._hover == hovered:
            return
        self._hover = hovered
        self.update()

    def enterEvent(self, event):
        self.set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.set_hovered(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
            self.setFocus()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.setChecked(not self._checked)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        from sayink.ui import design_tokens as tok

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self._TRACK_W, self._TRACK_H
        radius = h / 2.0
        on = self._checked or self._knob_pos > 0.5

        # Flat pill track — no custom focus ring chrome.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._track_color())
        p.drawRoundedRect(0, 0, w, h, radius, radius)

        knob_d = h - self._KNOB_MARGIN * 2
        travel = w - knob_d - self._KNOB_MARGIN * 2
        knob_x = self._KNOB_MARGIN + int(travel * self._knob_pos)
        knob_y = self._KNOB_MARGIN

        # Hairline shadow only — heavy drop-shadow made the control feel chunky.
        shadow = QColor(0, 0, 0, 10 if on else 6)
        p.setBrush(shadow)
        p.drawEllipse(knob_x, knob_y + 1, knob_d, knob_d)

        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(knob_x, knob_y, knob_d, knob_d)

        if self.hasFocus():
            focus_pen = QPen(QColor(tok.ACCENT_FOCUS), 2)
            focus_pen.setCosmetic(True)
            p.setPen(focus_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), radius - 1, radius - 1)
        p.end()

    def sizeHint(self):
        return QSize(self._TRACK_W, self._TRACK_H)

    def minimumSizeHint(self):
        return QSize(self._TRACK_W, self._TRACK_H)


class ToggleOptionRow(QWidget):
    """Full-width toggle row with explicit title/subtitle.

    Must be a QWidget (not QCheckBox): QAbstractButton sizeHint ignores child
    layouts and uses Fixed vertical policy, which collapses the row to 0 height.
    """

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("ToggleOptionRow")
        self._hover = False
        self._switch = SwitchControl()
        self._switch.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._switch.setAccessibleName(title)
        self.setFocusProxy(self._switch)
        if subtitle:
            self._switch.setAccessibleDescription(subtitle)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setProperty("accessibleRole", "CheckBox")
        self.setProperty("accessibleChecked", False)
        self._switch.toggled.connect(
            lambda checked: self.setProperty("accessibleChecked", checked)
        )
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAccessibleName(title)
        if subtitle:
            self.setAccessibleDescription(subtitle)
        self._sync_row_style()

        text_col = _option_text_column(title, subtitle)
        _set_click_through(text_col)

        switch_slot = QWidget()
        switch_slot.setFixedSize(SwitchControl._TRACK_W, SwitchControl._TRACK_H)
        switch_slot.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        slot_lay = QHBoxLayout(switch_slot)
        slot_lay.setContentsMargins(0, 0, 0, 0)
        slot_lay.addWidget(self._switch)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 16, 0)
        lay.setSpacing(12)
        lay.addWidget(text_col, 1)
        lay.addWidget(switch_slot, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        self.setMinimumHeight(48)

    def reapply_styles(self) -> None:
        from sayink.ui import design_tokens as tok

        for label in self.findChildren(QLabel):
            role = label.property("viRole")
            if role == "rowTitle":
                label.setStyleSheet(
                    f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 400;"
                    f" background: transparent;"
                )
            elif role == "rowSubtitle":
                label.setStyleSheet(
                    f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.4;"
                    f" background: transparent;"
                )
        self._sync_row_style()
        self._switch.update()

    def _toggle(self) -> None:
        self._switch.setChecked(not self._switch.isChecked())
        self.setFocus()

    def _sync_row_style(self) -> None:
        from sayink.ui import design_tokens as tok

        hover_bg = tok.ROW_HOVER if self._hover else "transparent"
        # No rounded “card inside card” — rows are flat slices like prototype .row.
        self.setStyleSheet(f"""
            ToggleOptionRow {{
                background: {hover_bg};
                border: none;
                border-radius: 0;
            }}
            ToggleOptionRow:focus {{
                background: {tok.ROW_HOVER};
            }}
        """)

    def enterEvent(self, event):
        self._hover = True
        self._switch.set_hovered(True)
        self._sync_row_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._switch.set_hovered(False)
        self._sync_row_style()
        super().leaveEvent(event)

    @property
    def toggled(self):
        return self._switch.toggled

    def isChecked(self) -> bool:
        return self._switch.isChecked()

    def setChecked(self, checked: bool) -> None:
        self._switch.setChecked(checked, animate=False)
        self.setProperty("accessibleChecked", bool(checked))

    def blockSignals(self, block: bool) -> bool:
        return self._switch.blockSignals(block)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._toggle()
            event.accept()
            return
        super().keyPressEvent(event)


def _set_click_through(widget: QWidget) -> None:
    """Let parent rows receive clicks across labels and padding."""
    widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    for child in widget.findChildren(QWidget):
        child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)


class SettingsPage(QScrollArea):
    """Scrollable content shell with consistent margins."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        body = QWidget()
        body.setMaximumWidth(tok.CONTENT_MAX_WIDTH)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        # Maximum keeps the scroll document as tall as real content — not the
        # viewport — so AlignTop slack is not scrollable empty space.
        body.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        self._layout = QVBoxLayout(body)
        self._layout.setContentsMargins(PAGE_MARGIN_H, PAGE_MARGIN_V, PAGE_MARGIN_H, PAGE_MARGIN_V)
        self._layout.setSpacing(SPACE_LG + SPACE_XS)  # 24+8 — clearer section rhythm
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(body)
        # Keep AsNeeded, but reserve the bar gutter when it is hidden so
        # tall sections (e.g. 润色接口配置) don't shift rows horizontally.
        self.verticalScrollBar().rangeChanged.connect(self._sync_scroll_gutter)
        self._sync_scroll_gutter(
            self.verticalScrollBar().minimum(),
            self.verticalScrollBar().maximum(),
        )

    def _sync_scroll_gutter(self, vmin: int = 0, vmax: int = 0) -> None:
        bar = self.verticalScrollBar()
        gutter = bar.sizeHint().width()
        if gutter <= 0:
            gutter = 6
        if vmax > vmin:
            self.setViewportMargins(0, 0, 0, 0)
        else:
            self.setViewportMargins(0, 0, gutter, 0)

    def set_compact(self, compact: bool = True) -> None:
        m = (18, 12, 18, 14) if compact else (PAGE_MARGIN_H, PAGE_MARGIN_V, PAGE_MARGIN_H, PAGE_MARGIN_V)
        self._layout.setContentsMargins(*m)

    def set_spacing(self, px: int) -> None:
        self._layout.setSpacing(px)

    def add(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)

    def add_stretch(self) -> None:
        self._layout.addStretch(1)
