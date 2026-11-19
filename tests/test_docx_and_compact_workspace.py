"""Contract tests for Track C:
1. Pure-stdlib ``.docx`` round-trip read/write, zip-bomb defense, transcript import,
   and ``MeetingPage.export_meeting_to_path(..., .docx)``.
2. User live notes (``notes_user``) elevated as primary structural outline in
   ``build_user_prompt`` while preserving anti-hallucination verification tags.
3. Virtualized ``SessionStreamListView`` (``QListView`` + ``QStyledItemDelegate``)
   and single-pass paged ``HistoryStore.list_sessions`` window-function accuracy.
4. ``560px`` narrow split-screen compact mode across ``MainWindow``, ``HistoryWindow``,
   and ``MeetingPage`` (sidebar collapse + single-pane list<->detail navigation).
"""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest
from PyQt6.QtWidgets import QApplication

from sayink.atomic_io import atomic_write_bytes, atomic_write_text
from sayink.config import Config
from sayink.docx_io import DocxFormatError, read_docx_text, write_docx_document
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController
from sayink.minutes_prompts import (
    USER_NOTES_OUTLINE_DIRECTIVE,
    MeetingContext,
    build_user_prompt,
    extract_user_note_outline,
)
from sayink.transcript_import import (
    SUPPORTED_TRANSCRIPT_SUFFIXES,
    TranscriptDecodeError,
    parse_transcript,
    read_transcript_file,
)
from sayink.ui import design_tokens as tok
from sayink.ui.main_window import MainWindow
from sayink.ui.meeting_page import MeetingPage
from sayink.ui.session_stream_view import SessionStreamListView, StreamRowSpec


def test_docx_roundtrip_zip_bomb_guard_and_transcript_import(tmp_path: Path):
    assert ".docx" in SUPPORTED_TRANSCRIPT_SUFFIXES

    docx_path = tmp_path / "评审文字稿.docx"
    write_docx_document(
        docx_path,
        title="Q4 产品评审会",
        metadata_rows=[("参会人", "张三、李四"), ("日期", "2026-11-17")],
        markdown_body=(
            "# Q4 产品评审会\n\n"
            "## 发言记录\n\n"
            "[00:00:12] 张三：先把 **SayInk** 词典扩容到 500 条。\n\n"
            "[00:00:28] 李四：同意，解码器热词保持 30 条上限。\n\n"
            "- [ ] 补充端到端回归测试 — @张三\n"
        ),
    )

    extracted = read_docx_text(docx_path)
    assert "# Q4 产品评审会" in extracted
    assert "参会人  张三、李四" in extracted
    assert "[00:00:12] 张三：先把 SayInk 词典扩容到 500 条。" in extracted
    assert "☐ 补充端到端回归测试 — @张三" in extracted

    # Transcript file loader & parser handle .docx natively
    loaded_txt = read_transcript_file(docx_path)
    parsed = parse_transcript(loaded_txt)
    assert len(parsed.turns) >= 2

    # Zip-bomb / uncompressed byte limit guard
    with pytest.raises(DocxFormatError, match="超过"):
        read_docx_text(docx_path, max_uncompressed_bytes=64)

    # Legacy binary .doc rejected with clear actionable message
    legacy_doc = tmp_path / "old.doc"
    legacy_doc.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    with pytest.raises(TranscriptDecodeError, match=r"\.docx"):
        read_transcript_file(legacy_doc)

    # Corrupt zip rejected cleanly
    broken_docx = tmp_path / "broken.docx"
    with zipfile.ZipFile(broken_docx, "w") as zf:
        zf.writestr("word/document.xml", b"<w:document><unclosed>")
    with pytest.raises(TranscriptDecodeError, match="XML"):
        read_transcript_file(broken_docx)


def test_meeting_page_exports_structured_docx(qapp, tmp_path: Path):
    cfg = Config(config_dir=tmp_path)
    store = HistoryStore(tmp_path / "history.db")
    ctrl = MeetingController(cfg, store, pending_segments=lambda: 0)
    page = MeetingPage(cfg, ctrl)
    try:
        rec = ctrl.import_transcript(
            "[00:00:05] 张三：确认支持导出 Word 文档。\n[00:00:18] 李四：纯标准库零依赖实现。",
            title="办公闭环验收",
        )
        assert rec is not None
        ctrl.store.save_notes(
            rec.id,
            template_id="general",
            model="qwen2.5",
            json_payload="{}",
            markdown="# 办公闭环验收\n\n## 决策\n\n- 原生支持 Word 导入导出 [00:00:05]\n",
            duration_ms=20_000,
        )
        store.flush()
        page.show_meeting(rec.id)

        out_docx = tmp_path / "导出纪要.docx"
        assert page.export_meeting_to_path(rec.id, out_docx, scope="all") is True
        assert out_docx.is_file()
        roundtrip = read_docx_text(out_docx)
        assert "办公闭环验收" in roundtrip
        assert "原生支持 Word 导入导出" in roundtrip
        assert "纯标准库零依赖实现" in roundtrip
    finally:
        page.cleanup()
        page.close()
        page.deleteLater()
        store.close()


def test_user_notes_fused_as_primary_outline_in_minutes_prompt():
    outline = extract_user_note_outline("\n  1. 词典自学习飞轮\n\n  2. 560px 窄窗分屏  \n")
    assert outline == ["1. 词典自学习飞轮", "2. 560px 窄窗分屏"]

    # Empty notes_user emits zero directive bloat
    empty_prompt = build_user_prompt(MeetingContext(title="例会"), "[00:00:01] 说话人1：你好")
    assert USER_NOTES_OUTLINE_DIRECTIVE not in empty_prompt

    # Non-empty notes_user injects primary structural outline directive + anti-hallucination guard
    rich_prompt = build_user_prompt(
        MeetingContext(
            title="架构周会",
            notes_user="- 确认热词上限 30 条\n- 跟进离线安装包体积（待法务确认）",
        ),
        "[00:00:10] 张三：热词上限就定 30 条。",
    )
    assert USER_NOTES_OUTLINE_DIRECTIVE in rich_prompt
    assert "主干骨架" in rich_prompt
    assert "（待核对）" in rich_prompt
    assert "确认热词上限 30 条" in rich_prompt


def test_virtualized_session_stream_view_and_paged_sqlite_summaries(qapp, tmp_path: Path):
    store = HistoryStore(tmp_path / "history.db")
    try:
        # Session 1: first segment suppressed (\u200b), second segment has polished text
        store.enqueue(
            SegmentRecord(
                session_id="sess-a",
                seq=0,
                created_at=1_700_000_001_000,
                raw_text="嗯",
                polished_text="\u200b",
                source="microphone",
                target_app="Code.exe",
            )
        )
        store.enqueue(
            SegmentRecord(
                session_id="sess-a",
                seq=1,
                created_at=1_700_000_002_000,
                raw_text="第二句原文",
                polished_text="第二句润色后预览",
                source="microphone",
                target_app="Code.exe",
            )
        )
        # Session 2: newer session
        store.enqueue(
            SegmentRecord(
                session_id="sess-b",
                seq=0,
                created_at=1_700_000_010_000,
                raw_text="最新会话内容",
                polished_text="",
                source="system",
                target_app="WeChat.exe",
            )
        )
        store.flush()

        paged = store.list_sessions(limit=10, offset=0)
        assert [s.session_id for s in paged] == ["sess-b", "sess-a"]
        assert paged[0].preview == "最新会话内容"
        assert paged[0].target_app == "WeChat.exe"
        assert paged[1].segment_count == 2
        assert paged[1].source == "microphone"
        assert paged[1].target_app == "Code.exe"
        assert paged[1].preview == "第二句润色后预览"

        # Virtualized QListView + QStyledItemDelegate model/selection contract
        view = SessionStreamListView()
        try:
            view.resize(280, 320)
            view.set_rows(
                [
                    StreamRowSpec(item_id="", title="今天", is_header=True),
                    StreamRowSpec(item_id="sess-b", title="最新会话内容", meta="WeChat · 1 段"),
                    StreamRowSpec(item_id="sess-a", title="第二句润色后预览", meta="Code · 2 段"),
                ]
            )
            assert view.select_id("sess-a") is True
            assert view.selected_ids() == ["sess-a"]
            view.batch_mode = True
            assert view.batch_mode is True
        finally:
            view.close()
    finally:
        store.close()


def test_compact_560px_split_screen_mode_across_main_history_and_meeting(qapp, tmp_path: Path):
    cfg = Config(config_dir=tmp_path)
    store = HistoryStore(tmp_path / "history.db")
    store.enqueue(
        SegmentRecord(
            session_id="s-compact",
            seq=0,
            created_at=1_700_000_100_000,
            raw_text="窄窗分屏模式下的历史记录内容。",
            polished_text="窄窗分屏模式下的历史记录内容。",
            source="microphone",
        )
    )
    store.flush()
    ctrl = MeetingController(cfg, store, pending_segments=lambda: 0)
    rec = ctrl.import_transcript("[00:00:03] 张三：一边看腾讯会议一边记笔记。", title="窄窗并排测试")
    assert rec is not None
    store.flush()

    win = MainWindow(cfg, store, meeting=ctrl)
    try:
        # 1. Default 960x640 -> standard full sidebar & dual-pane workspace
        win.resize(960, 640)
        win.show()
        QApplication.processEvents()
        assert win.is_compact_mode is False
        assert win.sidebar_width == tok.SIDEBAR_WIDTH

        # 2. Resize down to 560x500 -> sidebar collapses to 64px, workspaces enter single-pane mode!
        win.resize(560, 500)
        win.show_page("history")
        QApplication.processEvents()
        assert win.width() == 560
        assert win.is_compact_mode is True
        assert win.sidebar_width == tok.COMPACT_SIDEBAR_WIDTH
        assert win.history_panel.is_compact_single_pane is True

        win.history_panel.show_compact_detail()
        win.history_panel.show_compact_list()

        win.show_page("meeting")
        QApplication.processEvents()
        assert win.meeting_panel.is_compact_single_pane is True
        win.meeting_panel.show_meeting(rec.id)
        win.meeting_panel.show_compact_list()

        # 3. Resize back to 960x640 -> seamlessly restores full sidebar & dual-pane layout!
        win.resize(960, 640)
        QApplication.processEvents()
        assert win.is_compact_mode is False
        assert win.sidebar_width == tok.SIDEBAR_WIDTH
        assert win.history_panel.is_compact_single_pane is False
        assert win.meeting_panel.is_compact_single_pane is False
    finally:
        win.meeting_panel.cleanup()
        win.close()
        win.deleteLater()
        store.close()


def test_docx_and_atomic_io_preserve_existing_file_on_write_or_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docx_path = tmp_path / "既有纪要.docx"
    write_docx_document(
        docx_path,
        title="原始会议纪要",
        metadata_rows=[("状态", "已归档")],
        markdown_body="# 原始会议纪要\n\n- 这一版内容绝不能因覆盖失败变成 22 字节空包。\n",
    )
    original_bytes = docx_path.read_bytes()
    assert len(original_bytes) > 500

    # 1. Failure on first ZipFile.writestr leaves original .docx completely intact (not 22B!)
    real_writestr = zipfile.ZipFile.writestr

    def fail_first_entry(self, zinfo_or_arcname, data, *args, **kwargs):
        raise OSError("simulated disk full during writestr")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_first_entry)
    with pytest.raises(OSError, match="simulated disk full"):
        write_docx_document(docx_path, title="新纪要", markdown_body="新内容")
    monkeypatch.setattr(zipfile.ZipFile, "writestr", real_writestr)

    assert docx_path.read_bytes() == original_bytes
    assert "原始会议纪要" in read_docx_text(docx_path)
    assert list(tmp_path.glob("*.tmp")) == []

    # 2. Transient PermissionError on os.replace retries and succeeds; persistent lock preserves target & cleans tmp
    attempts = {"count": 0}
    import sayink.atomic_io as atomic_io_mod

    real_replace = atomic_io_mod.os.replace
    monkeypatch.setattr(atomic_io_mod.time, "sleep", lambda _s: None)

    def flaky_replace(src, dst):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise PermissionError("transient AV scan lock")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io_mod.os, "replace", flaky_replace)
    txt_path = tmp_path / "export.md"
    atomic_write_text(txt_path, "第一次导出\n")
    assert attempts["count"] == 3
    assert txt_path.read_text(encoding="utf-8") == "第一次导出\n"
    assert list(tmp_path.glob(".*.tmp")) == []

    monkeypatch.setattr(
        atomic_io_mod.os,
        "replace",
        lambda _s, _d: (_ for _ in ()).throw(PermissionError("locked in Word")),
    )
    with pytest.raises(PermissionError, match="locked in Word"):
        atomic_write_bytes(docx_path, b"new-bytes")
    assert docx_path.read_bytes() == original_bytes
    assert list(tmp_path.glob(".*.tmp")) == []

