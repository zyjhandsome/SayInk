"""Unit tests for live-resize deferral and flushing in sayink.ui.flow_layout."""

from __future__ import annotations

import time

from PyQt6.QtCore import QEvent, QTimer
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

from sayink.ui.flow_layout import (
    FlowLayout,
    PairedToolBar,
    WrappingToolBar,
    defer_resize,
    defer_while_live_resize,
    flush_live_resize,
    is_live_resizing,
    set_live_resizing,
)


class _HostShell(QWidget):
    """Lightweight top-level host exposing live-resize controls for tests."""

    def __init__(self) -> None:
        super().__init__()
        self._live_resizing = False

    def set_raw_live_flag(self, active: bool) -> None:
        self._live_resizing = bool(active)


def _wait_events(ms: int) -> None:
    app = QApplication.instance()
    assert app is not None
    done = False

    def _finish() -> None:
        nonlocal done
        done = True

    QTimer.singleShot(ms, _finish)
    deadline = time.perf_counter() + (ms / 1000.0) + 1.0
    while not done and time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.002)
    app.processEvents()


def test_defer_resize_runs_immediately_when_not_live_resizing(qapp):
    host = _HostShell()
    child = QWidget(host)
    calls: list[str] = []
    try:
        assert is_live_resizing(child) is False
        assert defer_while_live_resize(child, lambda: calls.append("immediate")) is False
        assert defer_resize(child, lambda: calls.append("alias")) is False
        assert calls == []
    finally:
        host.close()


def test_defer_resize_holds_past_32ms_pause_and_flushes_immediately_top_down(qapp):
    host = _HostShell()
    outer = QWidget(host)
    inner = QWidget(outer)
    events: list[str] = []

    def on_outer() -> None:
        events.append("outer")

    def on_inner_a() -> None:
        events.append("inner_a")

    def on_inner_b() -> None:
        events.append("inner_b")

    try:
        set_live_resizing(host, True)
        assert is_live_resizing(inner) is True

        # Queue inner before outer, and queue on_inner_a twice to verify deduplication.
        assert defer_while_live_resize(inner, on_inner_a) is True
        assert defer_resize(inner, on_inner_a) is True
        assert defer_while_live_resize(inner, on_inner_b) is True
        assert defer_while_live_resize(outer, on_outer) is True

        # Simulate >32ms mid-drag mouse pause (75ms > 2x32ms poll ticks).
        _wait_events(75)
        assert events == [], "Deferred callbacks must NEVER fire mid-drag during >32ms pauses"

        # Ending live resize flushes all queued callbacks synchronously in parent->child order.
        set_live_resizing(host, False)
        assert is_live_resizing(inner) is False
        assert events == ["outer", "inner_a", "inner_b"]

        # Subsequent flush is an idempotent no-op.
        assert flush_live_resize(host) == 0
        assert events == ["outer", "inner_a", "inner_b"]
    finally:
        host.close()


def test_defer_resize_timer_fallback_flushes_standalone_host_without_explicit_flush(qapp):
    host = _HostShell()
    child = QWidget(host)
    fired: list[int] = []
    try:
        host.set_raw_live_flag(True)
        assert defer_while_live_resize(child, lambda: fired.append(1)) is True

        _wait_events(65)
        assert fired == []

        # Flip the raw flag without calling set_live_resizing / flush_live_resize:
        # the window timer fallback must still flush cleanly within one 32ms tick.
        host.set_raw_live_flag(False)
        _wait_events(65)
        assert fired == [1]
    finally:
        host.close()


def test_wrapping_and_paired_toolbars_hold_reflow_during_live_resize_and_flush_on_end(qapp):
    host = _HostShell()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)

    wbar = WrappingToolBar(spacing=8)
    for idx in range(3):
        btn = QPushButton(f"操作按钮{idx}")
        btn.setMinimumWidth(120)
        wbar.add(btn, role="left" if idx < 2 else "right")
    lay.addWidget(wbar)

    pbar = PairedToolBar(spacing=8)
    for idx in range(2):
        lbtn = QPushButton(f"左侧{idx}")
        lbtn.setMinimumWidth(120)
        pbar.add_left(lbtn)
        rbtn = QPushButton(f"右侧{idx}")
        rbtn.setMinimumWidth(120)
        pbar.add_right(rbtn)
    lay.addWidget(pbar)

    try:
        host.resize(760, 200)
        host.show()
        qapp.processEvents()

        single_w_h = wbar.minimumHeight()
        single_p_h = pbar.minimumHeight()
        assert single_w_h > 0 and single_p_h > 0

        # Begin live resize and narrow across wrap thresholds, plus post LayoutRequest.
        set_live_resizing(host, True)
        host.resize(240, 200)
        QApplication.postEvent(wbar, QEvent(QEvent.Type.LayoutRequest))
        QApplication.postEvent(pbar, QEvent(QEvent.Type.LayoutRequest))
        _wait_events(70)

        assert wbar.minimumHeight() == single_w_h
        assert pbar.minimumHeight() == single_p_h

        # Release live resize -> both toolbars wrap immediately.
        set_live_resizing(host, False)
        assert wbar.minimumHeight() > single_w_h
        assert pbar.minimumHeight() > single_p_h
    finally:
        host.close()


def test_flow_layout_defers_row_wrapping_during_live_resize_and_flushes_on_end(qapp):
    host = _HostShell()
    outer = QVBoxLayout(host)
    outer.setContentsMargins(0, 0, 0, 0)
    chip_bar = QWidget(host)
    flow = FlowLayout(chip_bar, spacing=6)
    flow.setContentsMargins(0, 0, 0, 0)
    chips: list[QLabel] = []
    for idx in range(4):
        lbl = QLabel(f"标签项{idx}")
        lbl.setFixedSize(90, 24)
        flow.addWidget(lbl)
        chips.append(lbl)
    outer.addWidget(chip_bar)

    try:
        host.resize(500, 120)
        host.show()
        qapp.processEvents()
        assert len({c.geometry().y() for c in chips}) == 1

        set_live_resizing(host, True)
        host.resize(200, 120)
        _wait_events(70)
        # During live resize (even after >32ms pause), chips hold their single-row positions.
        assert len({c.geometry().y() for c in chips}) == 1

        set_live_resizing(host, False)
        qapp.processEvents()
        assert len({c.geometry().y() for c in chips}) >= 2
    finally:
        host.close()
