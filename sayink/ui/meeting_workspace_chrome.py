"""Meeting theme refresh and hosted narrow-workspace layout.

These routines stay off ``MeetingPage`` so the page module remains under the
modularization line budget. Compact single-pane mode applies only while the
page is hosted in the main window; a free-standing page keeps both panes.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidgetItem

from sayink.ui import design_tokens as tok
from sayink.ui.flow_layout import defer_while_live_resize, flush_live_resize
from sayink.ui.history_window import session_list_item_css
from sayink.ui.meeting_batch import fit_action_button
from sayink.ui.meeting_page import PAGE_LIVE
from sayink.ui.meeting_views import find_in_reading_view


def reapply_theme(page: object) -> None:
    from sayink.ui import design_tokens as tok
    from sayink.ui import settings_styles as ss
    from sayink.ui.settings_components import PAGE_TITLE

    page.setStyleSheet(ss.WINDOW_CSS)
    page._title_label.setStyleSheet(PAGE_TITLE)
    page._card_title.setStyleSheet(
        f"font-family: {tok.FONT_DISPLAY}; font-size: {tok.TYPE_TITLE}px; font-weight: 700;"
        f" background: transparent; color: {tok.TEXT_SEC}; padding: 2px 0 0 0;"
    )
    page._live_title.setStyleSheet(
        f"font-size: {tok.TYPE_TITLE}px; font-weight: 700; color: {tok.TEXT}; background: transparent;"
    )
    page._paint_live_clock()
    dim = f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
    for label in (page._list_summary, page._detail_meta, page._card_hint, page._live_caps,
                  page._card_notes, page._empty_label, page._template_label, page._content_hint,
                  page._notes_empty, page._live_speaker_hint, page._live_speaker_feedback,
                  page._materials_agenda, page._opt_loopback_hint, page._live_mic_label,
                  page._live_sys_label, page._live_find_status, page._detail_find_status):
        label.setStyleSheet(dim)
    for opt_lbl in (getattr(page, "_opt_save_audio_hint", None),):
        if opt_lbl is not None:
            opt_lbl.setStyleSheet(dim)
    start_err = getattr(page, "_start_error_banner", None)
    if start_err is not None:
        start_err.setStyleSheet(
            f"color: {tok.RED_TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; background: {tok.RED_BG};"
            f" border: 1px solid {tok.RED}; border-radius: {tok.RADIUS_MD}px; padding: 6px 10px;"
        )
    integrity_banner = getattr(page, "_integrity_banner", None)
    if integrity_banner is not None:
        integrity_banner.setStyleSheet(
            f"QFrame#meetingIntegrityBanner {{ background: {tok.AMBER_SOFT};"
            f" border: 1px solid {tok.CALLOUT_BORDER}; border-radius: {tok.RADIUS_MD}px; }}"
        )
    integrity_label = getattr(page, "_integrity_label", None)
    if integrity_label is not None:
        integrity_label.setStyleSheet(
            f"color: {tok.AMBER_TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
    field_label_css = (
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 700; background: transparent;"
    )
    for field_lbl in getattr(page, "_card_field_labels", ()):
        field_lbl.setStyleSheet(field_label_css)
    for label in (page._marks_title, page._notes_title, page._materials_notes_label, page._materials_marks_label):
        label.setStyleSheet(
            f"color: {tok.TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 700; background: transparent;"
        )
    status_css = (
        f"color: {tok.TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; background: {tok.SURFACE_PEARL};"
        f" border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px; padding: 6px 10px;"
    )
    page._status_label.setStyleSheet(status_css)
    page._paint_status_label()
    page._import_progress.setStyleSheet(
        f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; background: transparent; border: none;"
    )
    page._import_kicker.setStyleSheet(
        f"color: {tok.TEXT}; font-size: {tok.TYPE_TITLE}px; font-weight: 700; background: transparent;"
    )
    page._import_filename.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
    )
    page._import_live_caption.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
    )
    page._start_block.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
    )
    page._audio_missing.setStyleSheet(
        f"color: {tok.AMBER_TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; background: {tok.AMBER_SOFT};"
        f" border: 1px solid {tok.CALLOUT_BORDER}; border-radius: {tok.RADIUS_MD}px; padding: 6px 10px;"
    )
    page._live_status.setStyleSheet(status_css)
    page._import_progress_bar.setStyleSheet(
        f"QProgressBar {{ background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};"
        f" border-radius: 5px; }}"
        f"QProgressBar::chunk {{ background: {tok.ACCENT}; border-radius: 4px; }}"
    )
    page._system_silent_banner.setStyleSheet(
        f"QFrame#meetingSilentBanner {{ background: {tok.AMBER_SOFT}; border: 1px solid {tok.CALLOUT_BORDER};"
        f" border-radius: {tok.RADIUS_MD}px; }}"
    )
    page._system_silent_label.setStyleSheet(
        f"color: {tok.AMBER_TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 700; background: transparent;"
    )
    page._audio_player.reapply_theme()
    edit_css = f"""
        QLineEdit {{
            background: {tok.INPUT_BG}; color: {tok.TEXT};
            border: 1px solid {tok.CONTROL_BORDER}; border-radius: {tok.RADIUS_MD}px;
            padding: 6px 12px; min-height: {tok.CONTROL_FIELD_HEIGHT - 14}px; font-size: {tok.TYPE_BODY_SM}px;
        }}
        QLineEdit:focus {{ border: 2px solid {tok.ACCENT}; padding: 5px 11px; }}
    """
    page._search_edit.setStyleSheet(ss.build_search_field_css())
    for edit in (page._opt_title, page._opt_participants, page._mark_note, page._live_find_edit, page._detail_find_edit):
        edit.setStyleSheet(edit_css)
    page._detail_title.setStyleSheet(f"""
        QLineEdit {{
            background: transparent; color: {tok.TEXT};
            border: 1px solid transparent; border-radius: {tok.RADIUS_MD}px;
            padding: 4px 4px; min-height: 32px;
            font-size: {tok.TYPE_HERO}px; font-weight: 700;
        }}
        QLineEdit:hover {{ border: 1px solid {tok.HAIRLINE}; }}
        QLineEdit:focus {{ background: {tok.SURFACE_PEARL}; border: 2px solid {tok.ACCENT}; padding: 3px 3px; }}
    """)
    page._paint_notes_state()
    page._template_combo.setMinimumHeight(tok.CONTROL_BTN_SM_HEIGHT)
    from sayink.ui.tray_icon import _menu_font, _menu_stylesheet

    for menu in (
        page._more_menu,
        page._speaker_menu,
        page._export_menu,
        page._live_speaker_menu,
        page._template_menu,
    ):
        menu.setFont(_menu_font())
        menu.setStyleSheet(_menu_stylesheet())
    text_css = f"""
        QTextBrowser, QPlainTextEdit {{
            background: {tok.SURFACE_PEARL}; color: {tok.TEXT};
            border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px;
            padding: 8px; font-size: {tok.TYPE_BODY_SM}px;
            selection-background-color: {tok.ACCENT_SOFT}; selection-color: {tok.TEXT};
        }}
        QPlainTextEdit:focus {{ border: 2px solid {tok.ACCENT}; }}
    """
    for view in (page._notes_edit, page._live_transcript, page._user_notes,
                 page._opt_agenda, page._record_notes, page._record_marks):
        view.setStyleSheet(text_css)
    page._marks_view.setStyleSheet(
        text_css
        + f"QTextBrowser {{ padding: 4px 8px; font-size: {tok.TYPE_FOOTNOTE}px; }}"
    )
    page._notes_edit.setStyleSheet(text_css + f"QPlainTextEdit {{ font-size: {tok.READING_FONT_PX}px; }}")
    reading_css = f"""
        QTextBrowser {{
            background: transparent; color: {tok.TEXT}; border: none; padding: 4px 0;
            font-size: {tok.READING_FONT_PX}px;
            selection-background-color: {tok.ACCENT_SOFT}; selection-color: {tok.TEXT};
        }}
    """
    for view in (page._notes_view, page._transcript_view):
        view.setStyleSheet(reading_css)
        view.document().setDocumentMargin(0)
    page._list.setStyleSheet(f"""
        QListWidget {{ background: transparent; border: 2px solid transparent; outline: none;
                       color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; }}
        {session_list_item_css()}
        QLabel#meetingRowTitle {{ color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; background: transparent; }}
        QLabel#meetingRowMeta {{ color: {tok.TEXT_SEC}; font-size: {tok.TYPE_CAPTION}px;
                                 background: transparent; }}
    """)
    for combo in (page._opt_source, page._opt_model, page._opt_language, page._template_combo):
        combo.setStyleSheet("")
    page._template_combo.setStyleSheet(ss.build_toolbar_combo_css())
    disabled_sm = page._disabled_btn_sm_override()
    for btn in (page._start_btn, page._card_start, page._resume_btn, page._live_new_msg_btn):
        btn.setStyleSheet(ss.BTN_ACCENT_SM)
    page._mark_btn.setStyleSheet(
        ss.BTN_ACCENT_SM if page._mark_btn.isEnabled() else ss.BTN_ACCENT_SM + disabled_sm
    )
    for btn in (page._generate_btn, page._cancel_notes_edit_btn, page._copy_btn,
                page._transcript_copy_btn, page._export_btn, page._card_cancel,
                page._pause_btn, page._cancel_btn, page._return_notes_btn, page._clear_search_btn,
                page._live_find_prev_btn, page._live_find_next_btn, page._live_find_close_btn,
                page._detail_find_prev_btn, page._detail_find_next_btn, page._detail_find_close_btn,
                page._system_silent_dismiss_btn,
                page._manage_templates_btn,
                page._card_dl_btn, page._live_dl_btn, page._load_more_btn, page._materials_toggle,
                page._sort_btn, page._selection_mode_btn, page._batch_all_btn, page._batch_export_btn, page._batch_generate_btn,
                page._import_btn, page._media_btn, page._media_retry_btn, page._media_again_btn,
                page._back_to_list_btn):
        btn.setStyleSheet(ss.BTN_GHOST_SM)
    for extra_ghost in (
        getattr(page, "_finalize_retry_btn", None),
        getattr(page, "_segment_retry_btn", None),
        getattr(page, "_empty_play_btn", None),
        getattr(page, "_empty_device_btn", None),
    ):
        if extra_ghost is not None:
            extra_ghost.setStyleSheet(ss.BTN_GHOST_SM)
    for extra_danger in (
        getattr(page, "_media_discard_btn", None),
        getattr(page, "_finalize_discard_btn", None),
    ):
        if extra_danger is not None:
            extra_danger.setStyleSheet(ss.BTN_DANGER_SM)
    for btn in (page._edit_btn, page._correct_speaker_btn, page._live_correct_speaker_btn):
        btn.setStyleSheet(ss.BTN_GHOST_SM + disabled_sm)
    page._media_cancel_btn.setStyleSheet(ss.BTN_DANGER_SM)
    page._batch_delete_btn.setStyleSheet(ss.BTN_DANGER_SM)
    for btn in (
        page._sort_btn, page._selection_mode_btn, page._batch_all_btn, page._batch_export_btn,
        page._batch_generate_btn, page._batch_delete_btn,
    ):
        fit_action_button(btn)
    tab_h = max(page._export_btn.sizeHint().height(), tok.CONTROL_BTN_SM_HEIGHT)
    chip_h = max(20, tab_h - 8)
    page._tab_bar.setStyleSheet(f"""
        QWidget#meetingTabs {{ background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};
                               border-radius: {tok.RADIUS_MD}px; }}
        QPushButton#meetingTab {{ background: transparent; color: {tok.TEXT_SEC};
                                  border: 1px solid transparent; border-radius: {tok.RADIUS_SM}px;
                                  padding: 0 16px; min-height: {chip_h}px; max-height: {chip_h}px;
                                  font-size: {tok.TYPE_BODY_SM}px; font-weight: 400; }}
        QPushButton#meetingTab:hover:!checked {{ color: {tok.TEXT}; background: {tok.ROW_HOVER}; }}
        QPushButton#meetingTab:checked {{ background: {tok.SECONDARY_CONTAINER}; color: {tok.TEXT};
                                          border-color: transparent; font-weight: 400; }}
        QPushButton#meetingTab:focus {{ border-color: {tok.ACCENT_FOCUS}; }}
    """)
    page._tab_bar.setFixedHeight(tab_h)
    no_indicator = "QPushButton::menu-indicator { image: none; width: 0px; }"
    page._more_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
    page._template_menu_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
    page._speaker_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator + disabled_sm)
    page._live_speaker_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator + disabled_sm)
    page._end_btn.setStyleSheet(ss.BTN_DANGER_SOLID_SM)
    for splitter in (page._splitter, page._live_split):
        splitter.setStyleSheet(
            "QSplitter::handle { background: transparent; }"
            f"QSplitter::handle:hover {{ background: {tok.ACCENT_FOCUS}; }}"
        )
    page._drafts.live_marks_html = ""
    if page._stack.currentIndex() == PAGE_LIVE and page._ctrl.is_active:
        page._sync_live_state()
        page._render_live()
    if page._detail_id:
        page._render_detail()
    if page._detail_find_bar.isVisible() and page._detail_find_edit.text().strip():
        page._find_detail_text(restart=False)
    if page._live_find_bar.isVisible() and page._live_find_edit.text().strip():
        idx, total = find_in_reading_view(
            page._live_transcript, page._live_find_edit.text(), step=False
        )
        page._live_find_status.setText("未找到" if total == 0 else f"{idx} / {total}")
    if hasattr(page, "_list"):
        page._sync_batch_checks()
    page._sync_media_buttons()


def _set_margins_if_changed(layout, left: int, top: int, right: int, bottom: int) -> None:
    cur = layout.contentsMargins()
    if (cur.left(), cur.top(), cur.right(), cur.bottom()) != (left, top, right, bottom):
        layout.setContentsMargins(left, top, right, bottom)


def _sync_live_marks_height(page, has_marks: bool | None = None) -> None:
    if not hasattr(page, "_marks_view"):
        return
    compact_h = 0 < page.height() < 540
    if has_marks is None:
        try:
            current_id = page._ctrl.current_id
            has_marks = bool(current_id and page._store.get_marks(current_id))
        except Exception:
            has_marks = False
    policy = Qt.ScrollBarPolicy.ScrollBarAsNeeded if has_marks else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    if page._marks_view.verticalScrollBarPolicy() != policy:
        page._marks_view.setVerticalScrollBarPolicy(policy)
    if compact_h:
        max_h = 68 if has_marks else 48
    else:
        max_h = 160 if has_marks else 88
    min_h = 34 if compact_h else 36
    if page._marks_view.minimumHeight() != min_h:
        page._marks_view.setMinimumHeight(min_h)
    if page._marks_view.maximumHeight() != max_h:
        page._marks_view.setMaximumHeight(max_h)


def show_compact_list(page) -> None:
    """Return to the meeting list pane in compact narrow mode."""
    page._compact_showing_detail = False
    page._apply_compact_pane_visibility()


def show_compact_detail(page) -> None:
    """Open the selected meeting detail pane in compact narrow mode."""
    if not page.is_compact_single_pane:
        return
    page._compact_showing_detail = True
    page._apply_compact_pane_visibility()


def _on_compact_meeting_clicked(page, item: QListWidgetItem) -> None:
    if (
        page.is_compact_single_pane
        and not page._batch_mode
        and bool(item.data(Qt.ItemDataRole.UserRole))
    ):
        page.show_compact_detail()


def _apply_compact_pane_visibility(page) -> None:
    importing = bool(getattr(page._ctrl, "importing_media", False))
    compact = page.is_compact_single_pane
    show_detail = (
        (bool(page._compact_showing_detail) or (importing and page._import_owns_detail))
        and not page._batch_mode
        and (bool(page._records) or page._editing_notes or importing)
    )
    if compact:
        page._browse_left.setVisible(not show_detail)
        page._detail_pane.setVisible(show_detail)
        if hasattr(page, "_back_to_list_btn"):
            page._back_to_list_btn.setVisible(show_detail and not (importing and page._import_owns_detail))
    else:
        page._browse_left.setVisible(True)
        page._detail_pane.setVisible(bool(page._records) or page._editing_notes or importing)
        if hasattr(page, "_back_to_list_btn"):
            page._back_to_list_btn.hide()


def _sync_materials_and_overlay(page) -> None:
    if hasattr(page, "_materials_scroll"):
        cap = max(108, min(200, int(page.height() * 0.28)))
        if page._materials_scroll.maximumHeight() != cap:
            page._materials_scroll.setMaximumHeight(cap)
    page._position_live_new_msg_btn()


def sync_viewport_width(page, host_width: int | None = None) -> None:
    if host_width is not None and host_width > 0:
        page._host_viewport_width = int(host_width)
    if defer_while_live_resize(page, page._follow_live_resize):
        return
    _sync_responsive_layout(page, host_width=host_width)
    _sync_materials_and_overlay(page)
    flush_live_resize(page)


def follow_live_resize(page) -> None:
    if defer_while_live_resize(page, page._follow_live_resize):
        return
    _sync_responsive_layout(page)
    _sync_materials_and_overlay(page)
    flush_live_resize(page)


def _sync_responsive_layout(page, host_width: int | None = None) -> None:
    if not hasattr(page, "_outer_layout"):
        return
    stashed_host_w = getattr(page, "_host_viewport_width", None)
    page._host_viewport_width = None
    top_win = page.window()
    in_host_shell = top_win is not page
    explicit_host_w = (
        int(host_width)
        if host_width is not None and host_width > 0
        else (int(stashed_host_w) if stashed_host_w is not None and stashed_host_w > 0 else None)
    )
    if explicit_host_w is not None:
        host_w: int | None = explicit_host_w
    elif in_host_shell and top_win.width() > 0:
        host_w = top_win.width()
    else:
        host_w = None
    w = page.width()
    if host_w is not None and in_host_shell and (explicit_host_w is not None or hasattr(top_win, "sidebar_width")):
        sidebar_w = (
            tok.COMPACT_SIDEBAR_WIDTH
            if host_w < tok.COMPACT_WINDOW_THRESHOLD
            else tok.SIDEBAR_WIDTH
        )
        eff_w = max(220, host_w - sidebar_w - 2)
    else:
        eff_w = w if w > 0 else (host_w or 0)
    h = page.height()
    compact_h = 0 < h < 540
    narrow_w = 0 < eff_w < 880 or (host_w is not None and 0 < host_w < 880)
    # Single-pane mode is a hosted split-screen behavior. A free-standing
    # page keeps both panes so review, copy, and batch tools stay reachable.
    compact_single = in_host_shell and (
        (host_w is not None and 0 < host_w < tok.COMPACT_WINDOW_THRESHOLD)
        or (0 < eff_w < tok.COMPACT_WORKSPACE_THRESHOLD)
    )

    base_left, base_top, _base_right, base_bottom = tok.WORKSPACE_MARGINS
    if compact_single:
        h_margin = 12
    elif narrow_w and eff_w < 720:
        h_margin = 16
    elif compact_h or narrow_w:
        h_margin = 20
    else:
        h_margin = base_left
    v_top = 8 if (compact_h or compact_single) else base_top
    v_bottom = 8 if (compact_h or compact_single) else base_bottom
    _set_margins_if_changed(page._outer_layout, h_margin, v_top, h_margin, v_bottom)
    outer_spacing = 6 if (compact_h or compact_single) else 14
    if page._outer_layout.spacing() != outer_spacing:
        page._outer_layout.setSpacing(outer_spacing)

    if hasattr(page, "_live_layout"):
        live_spacing = 4 if compact_h else 10
        if page._live_layout.spacing() != live_spacing:
            page._live_layout.setSpacing(live_spacing)
    if hasattr(page, "_live_split"):
        desired_orient = (
            Qt.Orientation.Vertical if compact_single else Qt.Orientation.Horizontal
        )
        if page._live_split.orientation() != desired_orient:
            page._live_split.setOrientation(desired_orient)
    if hasattr(page, "_live_side_layout"):
        side_spacing = 4 if compact_h else 8
        if page._live_side_layout.spacing() != side_spacing:
            page._live_side_layout.setSpacing(side_spacing)
        _set_margins_if_changed(
            page._live_side_layout,
            0 if compact_single else (8 if narrow_w else 12),
            6 if compact_single else 0,
            0,
            0,
        )
    if hasattr(page, "_user_notes"):
        notes_min_h = 46 if compact_h else 64
        if page._user_notes.minimumHeight() != notes_min_h:
            page._user_notes.setMinimumHeight(notes_min_h)
    page._sync_live_marks_height()

    if hasattr(page, "_browse_left") and hasattr(page, "_detail_pane"):
        _sync_browse_panes(page, compact_single=compact_single, narrow_w=narrow_w, eff_w=eff_w, h_margin=h_margin)
        page._fit_template_cluster()


def _set_min_width_if_changed(widget, width: int) -> None:
    if widget.minimumWidth() != width:
        widget.setMinimumWidth(width)


def _sync_browse_panes(page, *, compact_single: bool, narrow_w: bool, eff_w: int, h_margin: int) -> None:
    if compact_single:
        page._compact_single_pane = True
        _set_min_width_if_changed(page._browse_left, 180)
        _set_min_width_if_changed(page._detail_pane, 180)
        if hasattr(page, "_detail_layout"):
            _set_margins_if_changed(page._detail_layout, 0, 0, 0, 0)
        if hasattr(page, "_live_side_scroll"):
            _set_min_width_if_changed(page._live_side_scroll, 0)
        page._apply_compact_pane_visibility()
        return

    if page._compact_single_pane:
        page._compact_single_pane = False
        page._apply_compact_pane_visibility()
    if narrow_w:
        avail = max(300, eff_w - 2 * h_margin - page._splitter.handleWidth())
        ratio = eff_w / 880.0
        list_min = max(
            140,
            min(
                tok.WORKSPACE_LIST_MIN_WIDTH - 16,
                int(tok.WORKSPACE_LIST_MIN_WIDTH * ratio),
                int(avail * 0.34),
            ),
        )
        detail_min = max(
            200,
            min(
                tok.WORKSPACE_DETAIL_MIN_WIDTH - 20,
                int(tok.WORKSPACE_DETAIL_MIN_WIDTH * ratio),
                avail - list_min - 4,
            ),
        )
        _set_min_width_if_changed(page._browse_left, list_min)
        _set_min_width_if_changed(page._detail_pane, detail_min)
        if hasattr(page, "_detail_layout"):
            _set_margins_if_changed(page._detail_layout, 12 if eff_w < 720 else 16, 0, 0, 0)
        if hasattr(page, "_live_side_scroll"):
            _set_min_width_if_changed(page._live_side_scroll, 180)
        return

    _set_min_width_if_changed(page._browse_left, tok.WORKSPACE_LIST_MIN_WIDTH)
    _set_min_width_if_changed(page._detail_pane, tok.WORKSPACE_DETAIL_MIN_WIDTH)
    if hasattr(page, "_detail_layout"):
        _set_margins_if_changed(page._detail_layout, 20, 0, 0, 0)
    if hasattr(page, "_live_side_scroll"):
        _set_min_width_if_changed(page._live_side_scroll, 240)


def confirm_export_overwrite_if_adjusted(page, raw_path: str, final_path: str) -> bool:
    from pathlib import Path
    from sayink.ui import meeting_page as mp
    from sayink.ui.prompts import confirm_box

    raw_p = Path(raw_path)
    final_p = Path(final_path)
    if raw_p != final_p and final_p.exists():
        box, overwrite = confirm_box(
            page,
            "确认覆盖已有文件",
            f"文件「{final_p.name}」已存在，是否覆盖？",
            accept="覆盖",
            cancel="取消",
            destructive=True,
        )
        with mp._quiet_tooltips(page):
            box.exec()
        return box.clickedButton() == overwrite
    return True


def export_selected_meetings(page) -> None:
    import time
    from sayink.ui import meeting_page as mp

    ids = page._selected_meeting_ids()
    if not ids:
        return
    default = mp.export_save_path(f"会议导出-{time.strftime('%Y%m%d')}.md")
    with mp.native_file_dialog(page):
        raw_path, selected = mp.QFileDialog.getSaveFileName(
            page,
            "导出选中的会议",
            default,
            "Markdown (*.md);;Word 文档 (*.docx)",
        )
    if not raw_path:
        return
    kind = "docx" if mp._export_kind(raw_path, selected) == "docx" else "md"
    path = mp._with_export_suffix(raw_path, kind)
    if not page._confirm_export_overwrite_if_adjusted(raw_path, path):
        return
    try:
        body_md = page._ctrl.export_meetings_markdown(ids)
        if kind == "docx":
            from sayink.docx_io import write_docx_document

            write_docx_document(path, title=f"会议导出（{len(ids)} 场）", markdown_body=body_md)
        else:
            mp.atomic_write_text(path, body_md, encoding="utf-8")
    except PermissionError as exc:
        mp.alert_error(page, "导出失败", f"目标文件可能正被其他程序占用，无法写入：{exc}")
        return
    except OSError as exc:
        mp.alert_error(page, "导出失败", f"无法写入文件：{exc}")
        return
    page._set_status(f"已导出 {len(ids)} 场会议")


def _notes_text_for_export(page, record) -> str:
    if record.id == page._detail_id:
        return page._current_notes_text()
    notes = page._store.get_notes(record.id)
    return notes.markdown if notes is not None else ""


def _markdown_for_record_export_scope(page, record, scope: str) -> str:
    if record.id == page._detail_id and scope in ("notes", "transcript", "all"):
        return page._markdown_for_export_scope(record, scope)
    if scope == "transcript":
        return page._ctrl.export_transcript_markdown(record.id)
    if scope == "notes":
        return _notes_text_for_export(page, record)
    return page._ctrl.export_markdown(record.id)


def export_meeting_to_path(page, meeting_id: str, path, *, scope: str = "all") -> bool:
    from pathlib import Path
    from sayink.ui import meeting_page as mp

    if scope not in ("notes", "transcript", "all", "srt"):
        return False
    record = page._store.get_meeting(meeting_id)
    if record is None:
        return False
    target = Path(path)
    kind = mp._export_kind(str(target), "")
    effective_scope = "transcript" if scope == "srt" else scope
    if kind == "srt" and effective_scope == "notes":
        return False
    try:
        if kind == "docx":
            from sayink.docx_io import write_docx_document

            body_md = _markdown_for_record_export_scope(page, record, effective_scope)
            write_docx_document(
                target,
                title=record.display_title,
                metadata_rows=page._docx_metadata_rows(record),
                markdown_body=body_md,
            )
            return True
        if kind == "srt":
            _has_speakers, has_times = page._ctrl.transcript_shape(record)
            if not has_times:
                return False
            mp.atomic_write_text(target, page._ctrl.export_transcript_srt(record.id), encoding="utf-8")
            return True
        if kind == "txt":
            if effective_scope == "notes":
                text = _notes_text_for_export(page, record)
            elif effective_scope == "transcript":
                text = page._ctrl.transcript_text(record.id)
            else:
                notes_txt = _notes_text_for_export(page, record).rstrip()
                supplement = page._ctrl.supplements_markdown(record.id).strip()
                chunks = [
                    part
                    for part in (notes_txt, supplement, page._ctrl.transcript_text(record.id).rstrip())
                    if part
                ]
                text = "\n\n".join(chunks)
                if text and not text.endswith("\n"):
                    text += "\n"
            mp.atomic_write_text(target, text, encoding="utf-8")
            return True
        body_md = _markdown_for_record_export_scope(page, record, effective_scope)
        mp.atomic_write_text(target, body_md, encoding="utf-8")
        return True
    except OSError:
        return False


def export_single_meeting(page, scope: str = "all") -> None:
    from sayink.ui import meeting_page as mp

    if not page._flush_bound_title(sync=True) or not page._save_record_notes():
        return
    record = page._store.get_meeting(page._detail_id) if page._detail_id else None
    if record is None:
        return
    if scope == "srt":
        if not page._transcript_has_times:
            page._set_status("这场文字稿没有时间戳，无法导出字幕")
            return
        label = "文字稿字幕"
        default = mp.export_save_path(
            f"{mp.safe_filename(record.display_title)}-{label}-{mp._safe_dt(record.started_at).strftime('%Y%m%d')}.srt"
        )
        filters = "SRT 字幕 (*.srt)"
    else:
        label = {"notes": page._result_label(), "transcript": "文字稿", "all": "完整会议记录"}[scope]
        default = mp.export_save_path(
            f"{mp.safe_filename(record.display_title)}-{label}-{mp._safe_dt(record.started_at).strftime('%Y%m%d')}.md"
        )
        filters = "Markdown (*.md);;Word 文档 (*.docx);;纯文本 (*.txt)"
        if scope != "notes" and page._transcript_has_times:
            filters += ";;SRT 字幕 (*.srt)"
    with mp.native_file_dialog(page):
        raw_path, selected = mp.QFileDialog.getSaveFileName(
            page, "导出" + label, default, filters,
        )
    if not raw_path:
        return
    kind = "srt" if scope == "srt" else mp._export_kind(raw_path, selected)
    if kind == "srt" and scope == "notes":
        page._set_status("仅导出纪要不支持 SRT 字幕格式；如需字幕请选择「导出文字稿字幕 (.srt)」。", tone="error")
        return
    if kind == "srt" and not page._transcript_has_times:
        page._set_status("这场文字稿没有时间戳，无法导出字幕")
        return
    eff_scope = "transcript" if scope == "srt" else scope
    path = mp._with_export_suffix(raw_path, kind)
    if not page._confirm_export_overwrite_if_adjusted(raw_path, path):
        return
    srt_note = ""
    try:
        if kind == "docx":
            from sayink.docx_io import write_docx_document

            write_docx_document(
                path,
                title=record.display_title,
                metadata_rows=page._docx_metadata_rows(record),
                markdown_body=page._markdown_for_export_scope(record, eff_scope),
            )
        elif kind == "srt":
            text = page._ctrl.export_transcript_srt(record.id)
            srt_note = getattr(page._ctrl, "srt_skip_note", "") or ""
            mp.atomic_write_text(path, text, encoding="utf-8")
        elif kind == "txt":
            if eff_scope == "notes":
                text = page._current_notes_text()
            elif eff_scope == "transcript":
                text = page._ctrl.transcript_text(record.id)
            else:
                notes = page._current_notes_text().rstrip()
                supplement = page._ctrl.supplements_markdown(record.id).strip()
                chunks = [part for part in (notes, supplement, page._ctrl.transcript_text(record.id).rstrip()) if part]
                text = "\n\n".join(chunks)
                if text and not text.endswith("\n"):
                    text += "\n"
            mp.atomic_write_text(path, text, encoding="utf-8")
        else:
            text = page._markdown_for_export_scope(record, eff_scope)
            mp.atomic_write_text(path, text, encoding="utf-8")
    except PermissionError as exc:
        mp.alert_error(page, "导出失败", f"目标文件可能正被其他程序占用，无法写入：{exc}")
        return
    except OSError as exc:
        mp.alert_error(page, "导出失败", f"无法写入文件：{exc}")
        return
    if kind == "srt":
        page._set_status(f"已导出文字稿字幕（{srt_note}）" if srt_note else "已导出文字稿字幕")
    else:
        page._set_status("已导出" + label + ("（包含当前草稿）" if page._editing_notes and eff_scope != "transcript" else ""))


def confirm_remote_ai_consent(
    page,
    host: str,
    *,
    is_refinement: bool,
    batch: bool = False,
) -> tuple[bool, bool | None]:
    from PyQt6.QtWidgets import QCheckBox
    from sayink.ui import meeting_page as mp

    box = mp.QMessageBox(page)
    box.setWindowTitle("发送到远端模型")
    box.setIcon(mp.QMessageBox.Icon.Question)
    if batch:
        if is_refinement:
            lead = f"所选会议里还没有整理结果的文字稿、说话人称呼与自定义词汇将发送到 {host}。"
        else:
            lead = (
                f"所选会议里还没有纪要的完整文字稿、会议标题、参会人、议程、"
                f"时间标记、我的笔记、自定义词汇与说话人称呼将发送到 {host}。"
            )
    elif is_refinement:
        lead = f"这场会议的文字稿、说话人称呼与自定义词汇将发送到 {host} 整理原文。"
    else:
        lead = (
            f"这场会议的完整文字稿、会议标题、参会人、议程、时间标记、"
            f"我的笔记、自定义词汇与说话人称呼将发送到 {host} 生成纪要。"
        )
    box.setText(lead)
    box.setInformativeText("不会发送音频。确认后本机记住该地址，后续生成不再重复询问。")
    notes_cb: QCheckBox | None = None
    if not is_refinement:
        notes_cb = QCheckBox("发送时包含「我的笔记」", page)
        notes_cb.setChecked(True)
        set_cb = getattr(box, "setCheckBox", None)
        if callable(set_cb):
            set_cb(notes_cb)
    ok = box.addButton("发送并生成", mp.QMessageBox.ButtonRole.AcceptRole)
    box.addButton("取消", mp.QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(ok)
    mp._style_dialog_box(box)
    with mp._quiet_tooltips(page):
        box.exec()
    if box.clickedButton() != ok:
        return (False, None)
    include_notes = bool(notes_cb.isChecked()) if notes_cb is not None else None
    return (True, include_notes)


def splice_asr_gaps_into_html(base_html: str, lines, gap_lines) -> str:
    import html as html_mod
    from sayink.ui import design_tokens as tok

    if not gap_lines:
        return base_html
    gap_css = (
        f"margin:6px 0; color:{tok.AMBER_TEXT}; font-size:{tok.TYPE_FOOTNOTE}px;"
        f" font-weight:700;"
    )
    if not lines or not base_html:
        return "".join(
            f"<p style='{gap_css}'>{html_mod.escape(gap.text)}</p>" for gap in gap_lines
        )
    out = base_html
    by_before_index: dict[int, list[str]] = {}
    trailing: list[str] = []
    for gap in gap_lines:
        g_off = int(gap.offset_ms)
        placed = False
        for idx, line in enumerate(lines):
            if int(line.offset_ms) > g_off:
                by_before_index.setdefault(idx, []).append(
                    f"<p style='{gap_css}'>{html_mod.escape(gap.text)}</p>"
                )
                placed = True
                break
        if not placed:
            trailing.append(f"<p style='{gap_css}'>{html_mod.escape(gap.text)}</p>")
    for idx in sorted(by_before_index.keys(), reverse=True):
        anchor = f"<a name='seg{idx}'></a>"
        pos = out.find(anchor)
        if pos >= 0:
            p_start = out.rfind("<p", 0, pos)
            insert_at = p_start if p_start >= 0 else pos
            out = out[:insert_at] + "".join(by_before_index[idx]) + out[insert_at:]
    if trailing:
        out += "".join(trailing)
    return out


def sync_detail_integrity_and_empty_state(
    page,
    record,
    segments,
    *,
    has_audio: bool,
    configured: bool,
    markdown: str,
    generating: bool,
) -> None:
    from sayink.meeting_store import STATUS_LIVE
    from sayink.media_import import MEDIA_SOURCE
    from sayink.transcript_import import IMPORT_SOURCE

    banner = getattr(page, "_integrity_banner", None)
    label = getattr(page, "_integrity_label", None)
    retry_btn = getattr(page, "_segment_retry_btn", None)
    if banner is not None and label is not None:
        banner_fn = getattr(page._ctrl, "segment_integrity_banner_text", None)
        text = (
            str(banner_fn(record.id, has_saved_audio=has_audio, saved_segments=segments) or "").strip()
            if callable(banner_fn) and record.status != STATUS_LIVE
            else ""
        )
        label.setText(text)
        banner.setVisible(bool(text))
        if retry_btn is not None:
            has_unsaved_fn = getattr(page._ctrl, "has_unsaved_segments", None)
            retry_btn.setVisible(bool(text and callable(has_unsaved_fn) and has_unsaved_fn(record.id)))

    empty_bar = getattr(page, "_empty_speech_bar", None)
    play_btn = getattr(page, "_empty_play_btn", None)
    device_btn = getattr(page, "_empty_device_btn", None)
    is_live_recorded = record.source not in (IMPORT_SOURCE, MEDIA_SOURCE)
    asr_fn = getattr(page._ctrl, "asr_failures", None)
    unsaved_fn = getattr(page._ctrl, "has_unsaved_segments", None)
    has_failures = bool(
        (callable(asr_fn) and asr_fn(record.id))
        or (callable(unsaved_fn) and unsaved_fn(record.id))
    )
    zero_spoken = (
        is_live_recorded
        and record.status != STATUS_LIVE
        and len(segments) == 0
        and not has_failures
    )
    if empty_bar is not None:
        empty_bar.setVisible(zero_spoken and not page._editing_notes)
        if play_btn is not None:
            play_btn.setVisible(zero_spoken and has_audio)
        if device_btn is not None:
            device_btn.setVisible(zero_spoken)
    if zero_spoken:
        empty_msg = "本场未录到发言，暂无文字稿。"
        if has_audio:
            empty_msg += "可点击「回放本场录音」核对原始声音，或点击「检查音频设备」排查麦克风与电脑声回环设置。"
        else:
            empty_msg += "请点击「检查音频设备」排查麦克风或电脑声输出设备，确认未静音后再试。"
        if not configured:
            empty_msg += "\n后续生成纪要前可点击「配置 AI 接口」填写接口地址、密钥和模型，无需开启文字润色。"
        page._transcript_view.setPlaceholderText("本场未录到发言")
        if not markdown and not generating and not page._editing_notes:
            page._notes_view.setPlaceholderText(empty_msg)
            page._notes_empty.setText(empty_msg)
            page._notes_stack.setCurrentWidget(page._notes_empty)
    else:
        page._transcript_view.setPlaceholderText("文字稿为空")


def retry_unsaved_segments_ui(page, meeting_id: str | None = None) -> tuple[bool, str]:
    mid = str(meeting_id or page._detail_id or "").strip()
    retry_fn = getattr(page._ctrl, "retry_unsaved_segments", None)
    if not mid or not callable(retry_fn):
        return (True, "")
    ok, msg = retry_fn(mid)
    page.refresh()
    if not ok:
        page._set_status(msg or "仍有缺失片段未能写入数据库，请稍后重试。", tone="error")
    elif msg:
        page._set_status(msg)
    else:
        page._set_status("已补存缺失的文字稿片段。")
    return (ok, msg)


def set_start_card_fields_ui(
    page,
    *,
    title: str | None = None,
    participants: str | None = None,
    agenda: str | None = None,
    input_source: str | None = None,
    language: str | None = None,
) -> None:
    if title is not None:
        page._opt_title.setText(title)
    if participants is not None:
        page._opt_participants.setText(participants)
    if agenda is not None:
        page._opt_agenda.setPlainText(agenda)
    if input_source is not None:
        idx = page._opt_source.findData(input_source)
        if idx >= 0:
            page._opt_source.setCurrentIndex(idx)
    if language is not None:
        idx = page._opt_language.findData(language)
        if idx >= 0:
            page._opt_language.setCurrentIndex(idx)


def page_ui_snapshot(page) -> dict[str, object]:
    export_menu = {
        k: (act.text(), act.isEnabled())
        for k, act in getattr(page, "_export_actions", {}).items()
    }
    return {
        "view_mode": page.current_view_mode(),
        "active_tab": "transcript" if page._content_stack.currentIndex() == 1 else "notes",
        "status_text": page._status_label.text() if not page._status_label.isHidden() else "",
        "start_error": page._start_error_banner.text() if not page._start_error_banner.isHidden() else "",
        "save_audio_hint": page._opt_save_audio_hint.text(),
        "integrity_banner": page._integrity_label.text() if not page._integrity_banner.isHidden() else "",
        "segment_retry_visible": not page._segment_retry_btn.isHidden(),
        "empty_bar_visible": not page._empty_speech_bar.isHidden(),
        "empty_play_visible": not page._empty_play_btn.isHidden(),
        "empty_device_visible": not page._empty_device_btn.isHidden(),
        "notes_empty_text": page._notes_empty.text(),
        "transcript_placeholder": page._transcript_view.placeholderText(),
        "transcript_text": page._transcript_view.toPlainText(),
        "live_transcript_text": page._live_transcript.toPlainText(),
        "live_dot_a11y": page._live_rec_dot.accessibleName(),
        "live_dot_style": page._live_rec_dot.styleSheet(),
        "live_clock_style": page._live_clock.styleSheet(),
        "live_status_text": page._live_status.text() if not page._live_status.isHidden() else "",
        "export_menu": export_menu,
        "finalize_retry_visible": not page._finalize_retry_btn.isHidden(),
        "finalize_retry_text": page._finalize_retry_btn.text(),
        "finalize_retry_tooltip": page._finalize_retry_btn.toolTip(),
    }


def finish_page_refresh(page, *, has_records: bool, importing: bool) -> None:
    if page.is_compact_single_pane:
        page._apply_compact_pane_visibility()
    else:
        page._detail_pane.setVisible(has_records or page._editing_notes or importing)
    if page._stack.currentIndex() == PAGE_LIVE and page._ctrl.is_active:
        page._render_live()


def select_meetings_for_batch_ui(page, meeting_ids: list[str] | tuple[str, ...]) -> None:
    from PyQt6.QtCore import Qt

    target = set(meeting_ids)
    page._set_batch_mode(True)
    for idx in range(page._list.count()):
        item = page._list.item(idx)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) in target:
            item.setSelected(True)
    page._sync_batch_actions()


