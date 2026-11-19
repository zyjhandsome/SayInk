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
        mp._style_dialog_box(box, danger_btn=overwrite)
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


def export_meeting_to_path(page, meeting_id: str, path, *, scope: str = "all") -> bool:
    from pathlib import Path
    from sayink.ui import meeting_page as mp

    record = page._store.get_meeting(meeting_id)
    if record is None:
        return False
    target = Path(path)
    kind = mp._export_kind(str(target), "")
    try:
        if kind == "docx":
            from sayink.docx_io import write_docx_document

            if meeting_id == page._detail_id:
                body_md = page._markdown_for_export_scope(record, scope)
            elif scope == "transcript":
                body_md = page._ctrl.export_transcript_markdown(record.id)
            elif scope == "notes":
                notes = page._store.get_notes(record.id)
                body_md = notes.markdown if notes is not None else ""
            else:
                body_md = page._ctrl.export_markdown(record.id)
            write_docx_document(
                target,
                title=record.display_title,
                metadata_rows=page._docx_metadata_rows(record),
                markdown_body=body_md,
            )
            return True
        if kind == "srt":
            mp.atomic_write_text(target, page._ctrl.export_transcript_srt(record.id), encoding="utf-8")
            return True
        if kind == "txt":
            mp.atomic_write_text(target, page._ctrl.transcript_text(record.id), encoding="utf-8")
            return True
        mp.atomic_write_text(target, page._ctrl.export_markdown(record.id), encoding="utf-8")
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
    label = {"notes": page._result_label(), "transcript": "文字稿", "all": "完整会议记录"}[scope]
    default = mp.export_save_path(
        f"{mp.safe_filename(record.display_title)}-{label}-{mp._safe_dt(record.started_at).strftime('%Y%m%d')}.md"
    )
    filters = "Markdown (*.md);;Word 文档 (*.docx);;纯文本 (*.txt)"
    if page._transcript_has_times:
        filters += ";;SRT 字幕 (*.srt)"
    with mp.native_file_dialog(page):
        raw_path, selected = mp.QFileDialog.getSaveFileName(
            page, "导出" + label, default, filters,
        )
    if not raw_path:
        return
    kind = mp._export_kind(raw_path, selected)
    if kind == "srt" and not page._transcript_has_times:
        page._set_status("这场文字稿没有时间戳，无法导出字幕")
        return
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
                markdown_body=page._markdown_for_export_scope(record, scope),
            )
        elif kind == "srt":
            text = page._ctrl.export_transcript_srt(record.id)
            srt_note = getattr(page._ctrl, "srt_skip_note", "") or ""
            mp.atomic_write_text(path, text, encoding="utf-8")
        elif kind == "txt":
            if scope == "notes":
                text = page._current_notes_text()
            elif scope == "transcript":
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
            text = page._markdown_for_export_scope(record, scope)
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
        page._set_status("已导出" + label + ("（包含当前草稿）" if page._editing_notes and scope != "transcript" else ""))

