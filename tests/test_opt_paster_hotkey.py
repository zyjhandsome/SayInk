"""Tests for Phase 2 paster, window_input, and hotkey optimizations."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import types
from unittest.mock import patch

import pytest
from pynput import keyboard
from PyQt6.QtWidgets import QApplication

import sayink.hotkey_manager as hm_mod
import sayink.platform.window_input as wi_mod
import sayink.text_paster as tp_mod
from sayink.hotkey_manager import (
    MIN_HOLD_CONTINUOUS_MS,
    HotKeyManager,
)
from sayink.platform.window_input import (
    ALL_TERMINAL_EXES,
    CONSOLE_HOST_EXES,
    TERMINAL_APP_EXES,
    read_selected_text_uia,
)
from sayink.text_paster import (
    CLIPBOARD_EXCLUSION_FORMAT_NAMES,
    MAX_CLIPBOARD_SNAPSHOT_BYTES,
    RESTORE_CLIPBOARD_DELAY_MS,
    RESTORE_CLIPBOARD_RETRY_DELAY_MS,
    ClipboardSnapshot,
    PasteResult,
    PasteTargetSnapshot,
    TextPaster,
    capture_paste_target_snapshot,
    capture_win32_clipboard_snapshot,
    detect_session_change,
    detect_terminal_paste_mode,
    is_native_readonly_edit,
    remember_paste_target,
    restore_win32_clipboard_snapshot,
    set_win32_clipboard_text,
    snapshot_win32_clipboard,
)


class _Suppressed(Exception):
    pass


class _HookEvent:
    def __init__(self, vk: int, flags: int = 0, extra: int | None = None) -> None:
        self.vkCode = vk
        self.flags = flags
        self.dwExtraInfo = extra


def _make_fake_win32_clipboard(
    initial_entries: dict[int, tuple[str, bytes]] | None = None,
    *,
    open_results: list[bool] | None = None,
    size_overrides: dict[int, int] | None = None,
):
    """Build deterministic fake (ctypes, user32, kernel32) tuple for Win32 clipboard tests."""
    entries = dict(initial_entries or {})
    sizes = dict(size_overrides or {})
    open_queue = list(open_results or [])
    mem_blocks: dict[int, bytes] = {}
    handle_by_fmt: dict[int, int] = {}
    name_by_fmt: dict[int, str] = {}
    registered_by_name: dict[str, int] = {}
    next_id = {"mem": 100, "fmt": 0xC100}
    state = {
        "empty_calls": 0,
        "set_calls": [],
        "registered_names": [],
        "seq": 10,
        "written_by_name": {},
        "written_by_fmt": {},
    }

    for fmt_id, (fmt_name, payload) in entries.items():
        next_id["mem"] += 1
        hid = next_id["mem"]
        mem_blocks[hid] = payload
        handle_by_fmt[fmt_id] = hid
        if fmt_name:
            name_by_fmt[fmt_id] = fmt_name
            registered_by_name[fmt_name] = fmt_id

    class _FakeCtypes:
        @staticmethod
        def create_unicode_buffer(init_or_size):
            return ctypes.create_unicode_buffer(init_or_size)

        @staticmethod
        def string_at(ptr: int, size: int) -> bytes:
            return mem_blocks[ptr][:size]

        @staticmethod
        def memmove(dst: int, src: bytes, count: int) -> None:
            mem_blocks[dst] = bytes(src[:count])

    fmt_keys = list(entries.keys())
    enum_map: dict[int, int] = {}
    for idx, key in enumerate(fmt_keys):
        prev = 0 if idx == 0 else fmt_keys[idx - 1]
        enum_map[prev] = key
    if fmt_keys:
        enum_map[fmt_keys[-1]] = 0

    class _FakeUser32:
        def OpenClipboard(self, _hwnd) -> int:
            if open_queue:
                return 1 if open_queue.pop(0) else 0
            return 1

        def CloseClipboard(self) -> int:
            return 1

        def EmptyClipboard(self) -> int:
            state["empty_calls"] += 1
            state["written_by_name"].clear()
            state["written_by_fmt"].clear()
            state["seq"] += 1
            return 1

        def EnumClipboardFormats(self, prev: int) -> int:
            return enum_map.get(prev, 0)

        def GetClipboardFormatNameW(self, fmt: int, buf, _max_len: int) -> int:
            fname = name_by_fmt.get(fmt, "")
            if fname:
                buf.value = fname
                return len(fname)
            return 0

        def GetClipboardData(self, fmt: int) -> int:
            return handle_by_fmt.get(fmt, 0)

        def RegisterClipboardFormatW(self, name: str) -> int:
            state["registered_names"].append(name)
            if name not in registered_by_name:
                next_id["fmt"] += 1
                registered_by_name[name] = next_id["fmt"]
                name_by_fmt[next_id["fmt"]] = name
            return registered_by_name[name]

        def SetClipboardData(self, fmt: int, hmem: int) -> int:
            payload = mem_blocks.get(hmem, b"")
            state["set_calls"].append((fmt, payload))
            state["written_by_fmt"][fmt] = payload
            fname = name_by_fmt.get(fmt, "")
            if fname:
                state["written_by_name"][fname] = payload
            return hmem

        def GetClipboardSequenceNumber(self) -> int:
            return state["seq"]

    class _FakeKernel32:
        def GlobalSize(self, handle: int) -> int:
            for fmt_id, hid in handle_by_fmt.items():
                if hid == handle and fmt_id in sizes:
                    return sizes[fmt_id]
            return len(mem_blocks.get(handle, b""))

        def GlobalLock(self, handle: int) -> int:
            return handle if handle in mem_blocks else 0

        def GlobalUnlock(self, _handle: int) -> int:
            return 1

        def GlobalAlloc(self, _flags: int, size: int) -> int:
            next_id["mem"] += 1
            hid = next_id["mem"]
            mem_blocks[hid] = b"\x00" * size
            return hid

        def GlobalFree(self, handle: int) -> int:
            mem_blocks.pop(handle, None)
            return 0

    return _FakeCtypes, _FakeUser32(), _FakeKernel32(), state


# ---------------------------------------------------------------------------
# 1. P0 Terminal Multi-line Guard & CONSOLE_HOST_EXES (Omission #1)
# ---------------------------------------------------------------------------


class TestTerminalMultilineAndConsoleHosts:
    def test_console_host_exes_and_terminal_paste_chords(self, monkeypatch):
        expected_consoles = {"cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "wsl.exe", "bash.exe"}
        assert expected_consoles.issubset(CONSOLE_HOST_EXES)
        assert (CONSOLE_HOST_EXES | TERMINAL_APP_EXES) == ALL_TERMINAL_EXES

        # Windows Terminal window class overrides child shell exe -> ctrl_shift_v
        monkeypatch.setattr(tp_mod, "_get_window_class_name", lambda _h: "CASCADIA_HOSTING_WINDOW_CLASS")
        assert detect_terminal_paste_mode(10, "pwsh.exe") == "ctrl_shift_v"
        assert detect_terminal_paste_mode(10, "WindowsTerminal.exe") == "ctrl_shift_v"

        # Standalone console hosts & ConsoleWindowClass -> shift_insert (safe in conhost/WSL/SSH)
        monkeypatch.setattr(tp_mod, "_get_window_class_name", lambda _h: "ConsoleWindowClass")
        for exe in expected_consoles:
            assert detect_terminal_paste_mode(20, exe) == "shift_insert"

        monkeypatch.setattr(tp_mod, "_get_window_class_name", lambda _h: "Chrome_WidgetWin_1")
        assert detect_terminal_paste_mode(30, "Code.exe") == "ctrl_v"

    @pytest.mark.parametrize("term_exe", sorted(CONSOLE_HOST_EXES | {"windowsterminal.exe", "mintty.exe"}))
    def test_terminal_strips_trailing_crlf_and_blocks_multiline_execution(self, monkeypatch, term_exe):
        copied: list[str] = []
        sent_modes: list[str] = []
        monkeypatch.setattr(tp_mod.QTimer, "singleShot", lambda _ms, fn: fn())
        monkeypatch.setattr(tp_mod.pyperclip, "copy", lambda t: copied.append(t))
        monkeypatch.setattr(tp_mod.pyperclip, "paste", lambda: copied[-1] if copied else "")
        monkeypatch.setattr(tp_mod, "get_win32_clipboard_sequence_number", lambda: None)
        monkeypatch.setattr(tp_mod, "get_foreground_window_info", lambda: (501, "Shell", 99999))
        monkeypatch.setattr(tp_mod, "target_rejects_synthetic_input", lambda _info: False)
        monkeypatch.setattr(tp_mod, "_is_ime_composing", lambda *_a: False)
        monkeypatch.setattr(tp_mod, "is_native_readonly_edit", lambda *_a: False)
        monkeypatch.setattr(tp_mod, "_get_window_class_name", lambda _h: "")
        monkeypatch.setattr(tp_mod, "_paste_shortcut", lambda: sent_modes.append(tp_mod._ACTIVE_PASTE_MODE))

        paster = TextPaster()

        # 1) Single-line with trailing \r\n -> stripped and safely pasted without Enter
        res_single: list[PasteResult] = []
        paster.paste_async("git status\r\n", res_single.append, target_hwnd=501, target_app=term_exe)
        assert res_single == [PasteResult("sent", target_app=term_exe)]
        assert copied[-1] == "git status"
        assert len(sent_modes) == 1
        assert sent_modes[-1] in ("shift_insert", "ctrl_shift_v")

        # 2) Multi-line after stripping trailing \r\n -> NEVER sends keys, copies to clipboard, returns skipped
        sent_modes.clear()
        res_multi: list[PasteResult] = []
        paster.paste_async(
            "git status\nrm -rf build\r\n",
            res_multi.append,
            target_hwnd=501,
            target_app=term_exe,
        )
        assert sent_modes == []
        assert copied[-1] == "git status\nrm -rf build"
        assert res_multi == [
            PasteResult(status="skipped", target_app=term_exe, detail="terminal_multiline")
        ]

        # 3) Sync paste() entrypoint enforces identical terminal multiline guard
        sync_res = paster.paste("line1\rline2\n", target_hwnd=501, target_app=term_exe)
        assert sent_modes == []
        assert copied[-1] == "line1\rline2"
        assert sync_res == PasteResult(status="skipped", target_app=term_exe, detail="terminal_multiline")


# ---------------------------------------------------------------------------
# 2. Per-Utterance Snapshot, Subfocus Policy & No Title-Hash False Aborts
# ---------------------------------------------------------------------------


class TestSnapshotSubfocusAndTitleDigestPolicy:
    def test_capture_paste_target_snapshot_does_not_mutate_global_state(self, monkeypatch):
        tp_mod._REMEMBERED_TARGET_SNAPSHOTS.clear()
        monkeypatch.setattr(tp_mod, "get_foreground_window_info", lambda: (888, "Doc.md - Editor", 77777))
        monkeypatch.setattr(tp_mod, "_get_gui_focus_hwnd", lambda _h: 889)
        monkeypatch.setattr(tp_mod, "_is_desktop_locked", lambda: False)

        snap = capture_paste_target_snapshot()
        assert snap is not None
        assert snap.hwnd == 888
        assert snap.focus_hwnd == 889
        assert tp_mod.get_remembered_paste_target(888) is None

        # Own window or hwnd=0 returns None
        assert capture_paste_target_snapshot(0) is None
        monkeypatch.setattr(tp_mod, "get_foreground_window_info", lambda: (999, "SayInk", os.getpid()))
        assert capture_paste_target_snapshot() is None

    def test_detect_session_change_ignores_title_digest_and_splits_subfocus_policy(self):
        before = PasteTargetSnapshot(hwnd=100, focus_hwnd=201, title_digest="clean_doc")
        dirty_title = PasteTargetSnapshot(hwnd=100, focus_hwnd=201, title_digest="dirty_star_doc")
        moved_subfocus = PasteTargetSnapshot(hwnd=100, focus_hwnd=202, title_digest="dirty_star_doc")
        other_window = PasteTargetSnapshot(hwnd=999, focus_hwnd=500, title_digest="other")
        locked = PasteTargetSnapshot(hwnd=0, focus_hwnd=0, title_digest="", desktop_locked=True)

        # Title digest change (* dirty indicator / (1) unread badge) NEVER aborts paste!
        assert detect_session_change(before, dirty_title, check_subfocus=True) == ""
        assert detect_session_change(before, dirty_title, check_subfocus=False) == ""

        # Single-shot (check_subfocus=True) catches intra-window child HWND focus change
        assert detect_session_change(before, moved_subfocus, check_subfocus=True) == "subfocus_changed"

        # Continuous (check_subfocus=False) allows intra-window Tab/click while catching cross-HWND & lock
        assert detect_session_change(before, moved_subfocus, check_subfocus=False) == ""
        assert detect_session_change(before, other_window, check_subfocus=False) == "focus_changed"
        assert detect_session_change(before, locked, check_subfocus=False) == "session_changed"

    def test_per_utterance_baseline_snapshot_isolated_from_global_overwrite(self, monkeypatch):
        clipboard = {"val": ""}
        shortcuts = []
        monkeypatch.setattr(tp_mod.QTimer, "singleShot", lambda _ms, fn: fn())
        monkeypatch.setattr(tp_mod.pyperclip, "copy", lambda t: clipboard.__setitem__("val", t))
        monkeypatch.setattr(tp_mod.pyperclip, "paste", lambda: clipboard["val"])
        monkeypatch.setattr(tp_mod, "get_win32_clipboard_sequence_number", lambda: None)
        monkeypatch.setattr(tp_mod, "target_rejects_synthetic_input", lambda _info: False)
        monkeypatch.setattr(tp_mod, "_is_ime_composing", lambda *_a: False)
        monkeypatch.setattr(tp_mod, "is_native_readonly_edit", lambda *_a: False)
        monkeypatch.setattr(tp_mod, "_paste_shortcut", lambda: shortcuts.append(1))
        monkeypatch.setattr(tp_mod, "get_foreground_window_info", lambda: (300, "App *", 55555))
        monkeypatch.setattr(
            tp_mod,
            "build_paste_target_snapshot",
            lambda _info: PasteTargetSnapshot(hwnd=300, focus_hwnd=22, title_digest="new_hash"),
        )

        # Utterance 1 captured sub-box 11; Utterance 2 overwrote global remembered target with sub-box 22!
        snap_u1 = PasteTargetSnapshot(hwnd=300, focus_hwnd=11, title_digest="old_hash")
        remember_paste_target(PasteTargetSnapshot(hwnd=300, focus_hwnd=22, title_digest="new_hash"))

        paster = TextPaster()
        single_res: list[PasteResult] = []
        paster.paste_async(
            "单次第一句",
            single_res.append,
            target_hwnd=300,
            target_app="app.exe",
            baseline_snapshot=snap_u1,
            strict_subfocus=True,
        )
        assert shortcuts == []
        assert single_res == [PasteResult("clipboard", target_app="app.exe", detail="subfocus_changed")]

        # Continuous mode (strict_subfocus=False) allows moving from sub-box 11 -> 22 in same HWND
        cont_res: list[PasteResult] = []
        paster.paste_async(
            "持续第一句",
            cont_res.append,
            target_hwnd=300,
            target_app="app.exe",
            baseline_snapshot=snap_u1,
            strict_subfocus=False,
        )
        assert shortcuts == [1]
        assert cont_res == [PasteResult("sent", target_app="app.exe")]


# ---------------------------------------------------------------------------
# 3. Zero-IPC Win32 ES_READONLY & Safer 800ms / Unverified / Retry Restore
# ---------------------------------------------------------------------------


class TestNativeReadonlyAndSaferClipboardRestore:
    @pytest.mark.parametrize(
        "cls_name, style, expected",
        [
            ("Edit", 0x50000800, True),
            ("Edit", 0x50000000, False),
            ("RichEdit20W", 0x50000804, True),
            ("RICHEDIT50W", 0x00000800, True),
            ("MSFTEDIT_CLASS", 0x00000800, True),
            ("MSFTEDIT_CLASS", 0x00000044, False),
            ("Chrome_WidgetWin_1", 0x50000800, False),
            ("CASCADIA_HOSTING_WINDOW_CLASS", 0x50000800, False),
            ("ConsoleWindowClass", 0x50000800, False),
        ],
    )
    def test_zero_ipc_es_readonly_checks_only_classic_edit_and_richedit(
        self, monkeypatch, cls_name, style, expected
    ):
        monkeypatch.setattr(tp_mod.sys, "platform", "win32")
        monkeypatch.setattr(tp_mod, "_get_window_class_name", lambda _h: cls_name)
        monkeypatch.setattr(tp_mod, "_get_win32_window_style", lambda _h: style)
        assert is_native_readonly_edit(100, 200) is expected

    def test_readonly_edit_skips_ctrl_v_and_copies_to_clipboard(self, monkeypatch):
        copied: list[str] = []
        shortcuts: list[int] = []
        monkeypatch.setattr(tp_mod.QTimer, "singleShot", lambda _ms, fn: fn())
        monkeypatch.setattr(tp_mod.pyperclip, "copy", copied.append)
        monkeypatch.setattr(tp_mod.pyperclip, "paste", lambda: copied[-1] if copied else "")
        monkeypatch.setattr(tp_mod, "get_win32_clipboard_sequence_number", lambda: None)
        monkeypatch.setattr(tp_mod, "get_foreground_window_info", lambda: (400, "Viewer", 33333))
        monkeypatch.setattr(tp_mod, "target_rejects_synthetic_input", lambda _info: False)
        monkeypatch.setattr(tp_mod, "_is_ime_composing", lambda *_a: False)
        monkeypatch.setattr(tp_mod, "is_native_readonly_edit", lambda *_a: True)
        monkeypatch.setattr(tp_mod, "_paste_shortcut", lambda: shortcuts.append(1))

        res: list[PasteResult] = []
        TextPaster().paste_async("只读控件内容", res.append, target_hwnd=400, target_app="notepad.exe")
        assert shortcuts == []
        assert copied == ["只读控件内容"]
        assert res == [PasteResult("skipped", target_app="notepad.exe", detail="readonly_target")]

    def test_800ms_restore_unverified_policy_and_contention_retries(self, monkeypatch):
        assert RESTORE_CLIPBOARD_DELAY_MS == 800
        assert RESTORE_CLIPBOARD_RETRY_DELAY_MS == 150

        scheduled: list[tuple[int, object]] = []
        monkeypatch.setattr(tp_mod.QTimer, "singleShot", lambda ms, fn: scheduled.append((ms, fn)))

        clip_seq = {"val": 40}
        monkeypatch.setattr(tp_mod.pyperclip, "copy", lambda _t: clip_seq.__setitem__("val", clip_seq["val"] + 1))
        monkeypatch.setattr(tp_mod, "target_rejects_synthetic_input", lambda _info: False)
        monkeypatch.setattr(tp_mod, "_is_ime_composing", lambda *_a: False)
        monkeypatch.setattr(tp_mod, "is_native_readonly_edit", lambda *_a: False)
        monkeypatch.setattr(tp_mod, "_paste_shortcut", lambda: None)

        # Focus stays at 700 until Ctrl+V sent, then switches to 999 on verify -> "unverified"
        fg_items = [(700, "Ed", 12345), (700, "Ed", 12345), (999, "Other", 54321)]
        monkeypatch.setattr(
            tp_mod,
            "get_foreground_window_info",
            lambda: fg_items.pop(0) if len(fg_items) > 1 else fg_items[0],
        )

        paster = TextPaster(restore_clipboard=True)
        monkeypatch.setattr(paster, "_use_native_win32_clipboard", lambda: True)
        monkeypatch.setattr(paster, "_current_clipboard_seq", lambda: clip_seq["val"])
        monkeypatch.setattr(
            paster,
            "_capture_clipboard_snapshot",
            lambda: ClipboardSnapshot(win32_entries=[(13, "", b"O\x00\x00\x00")], use_win32=True),
        )

        # Contention: fail first 2 restore attempts, succeed on 3rd (retry #2)
        attempts = {"count": 0}

        def _flaky_restore(_snap: ClipboardSnapshot) -> bool:
            attempts["count"] += 1
            return attempts["count"] >= 3

        monkeypatch.setattr(tp_mod, "restore_win32_clipboard_snapshot", _flaky_restore)

        results: list[PasteResult] = []
        paster.paste_async("未确认粘贴", results.append, target_hwnd=700, target_app="Code.exe")
        # Step 1: PASTE_DELAY_MS
        assert scheduled[0][0] == tp_mod.PASTE_DELAY_MS
        scheduled.pop(0)[1]()
        # Step 2: VERIFY_AFTER_PASTE_MS -> emits unverified AND schedules 800ms restore
        assert scheduled[0][0] == tp_mod.VERIFY_AFTER_PASTE_MS
        scheduled.pop(0)[1]()
        assert results == [PasteResult("unverified", target_app="Code.exe", detail="focus_changed_after_send")]
        assert len(scheduled) == 1
        assert scheduled[0][0] == 800
        assert paster._restore_snapshot is not None

        # Attempt 1 fails -> schedules 150ms retry #1 without clearing _restore_snapshot
        scheduled.pop(0)[1]()
        assert attempts["count"] == 1
        assert paster._restore_snapshot is not None
        assert scheduled[0][0] == 150

        # Retry #1 fails -> schedules 150ms retry #2 without clearing _restore_snapshot
        scheduled.pop(0)[1]()
        assert attempts["count"] == 2
        assert paster._restore_snapshot is not None
        assert scheduled[0][0] == 150

        # Retry #2 succeeds -> clears _restore_snapshot
        scheduled.pop(0)[1]()
        assert attempts["count"] == 3
        assert paster._restore_snapshot is None
        assert scheduled == []

        # With keep_on_unverified=True, unverified does NOT schedule restore
        fg_items[:] = [(700, "Ed", 12345), (700, "Ed", 12345), (999, "Other", 54321)]
        keep_res: list[PasteResult] = []
        paster.paste_async(
            "保留未确认文本",
            keep_res.append,
            target_hwnd=700,
            target_app="Code.exe",
            keep_on_unverified=True,
        )
        scheduled.pop(0)[1]()
        scheduled.pop(0)[1]()
        assert keep_res[-1].status == "unverified"
        assert scheduled == []
        assert paster._restore_snapshot is None


# ---------------------------------------------------------------------------
# 4. Consecutive Clipboard Fallback Accumulation (Continuous #P1-1 / Omission #5)
# ---------------------------------------------------------------------------


class TestConsecutiveFallbackAccumulation:
    def test_append_fallback_accumulates_until_external_copy_or_reset(self, monkeypatch):
        clipboard = {"text": "", "seq": 100}

        def _copy(val: str) -> None:
            clipboard["text"] = val
            clipboard["seq"] += 1

        monkeypatch.setattr(tp_mod.QTimer, "singleShot", lambda _ms, fn: fn())
        monkeypatch.setattr(tp_mod.pyperclip, "copy", _copy)
        monkeypatch.setattr(tp_mod.pyperclip, "paste", lambda: clipboard["text"])
        monkeypatch.setattr(tp_mod.sys, "platform", "win32")
        monkeypatch.setattr(tp_mod, "get_win32_clipboard_sequence_number", lambda: clipboard["seq"])
        monkeypatch.setattr(tp_mod, "get_foreground_window_info", lambda: (999, "PDF Reader", 22222))
        monkeypatch.setattr(tp_mod, "target_rejects_synthetic_input", lambda _info: False)
        monkeypatch.setattr(tp_mod, "_is_ime_composing", lambda *_a: False)
        monkeypatch.setattr(tp_mod, "is_native_readonly_edit", lambda *_a: False)

        paster = TextPaster()
        out: list[PasteResult] = []

        paster.paste_async("第一段", out.append, target_hwnd=100, target_app="Code.exe", append_fallback=True)
        assert clipboard["text"] == "第一段"

        paster.paste_async("第二段", out.append, target_hwnd=100, target_app="Code.exe", append_fallback=True)
        assert clipboard["text"] == "第一段\n第二段"

        # User manually copies something else -> sequence changes -> next fallback starts fresh
        _copy("用户手动复制的参考资料")
        paster.paste_async("第三段", out.append, target_hwnd=100, target_app="Code.exe", append_fallback=True)
        assert clipboard["text"] == "第三段"

        # Explicit reset_fallback_accumulator() starts fresh even without external copy
        paster.reset_fallback_accumulator()
        paster.paste_async("第四段", out.append, target_hwnd=100, target_app="Code.exe", append_fallback=True)
        assert clipboard["text"] == "第四段"


# ---------------------------------------------------------------------------
# 5. Win32 Win+V / Cloud Exclusion & Custom HGLOBAL Snapshot Preservation
# ---------------------------------------------------------------------------


class TestWin32ClipboardExclusionAndCustomFormats:
    def test_transient_paste_and_restore_set_exclusion_flags_while_fallback_leaves_win_v_enabled(
        self, monkeypatch
    ):
        monkeypatch.setattr(tp_mod.sys, "platform", "win32")
        fake_c, fake_u32, fake_k32, state = _make_fake_win32_clipboard(
            {
                13: ("", "old".encode("utf-16-le") + b"\x00\x00"),
            }
        )
        monkeypatch.setattr(tp_mod, "_win32_clipboard_apis", lambda: (fake_c, fake_u32, fake_k32))

        # 1) Transient paste injection sets all 3 exclusion formats to DWORD(0)
        assert set_win32_clipboard_text("临时粘贴", exclude_from_history=True) is True
        for fmt_name in CLIPBOARD_EXCLUSION_FORMAT_NAMES:
            assert state["written_by_name"][fmt_name] == b"\x00\x00\x00\x00"

        # 2) Deliberate fallback copy omits exclusion formats so Win+V records it
        assert set_win32_clipboard_text("回退复制", exclude_from_history=False) is True
        for fmt_name in CLIPBOARD_EXCLUSION_FORMAT_NAMES:
            assert fmt_name not in state["written_by_name"]

        # 3) Snapshot restore also sets all 3 exclusion formats
        snap = ClipboardSnapshot(
            use_win32=True,
            win32_entries=[(13, "", "old".encode("utf-16-le") + b"\x00\x00")],
        )
        assert restore_win32_clipboard_snapshot(snap) is True
        for fmt_name in CLIPBOARD_EXCLUSION_FORMAT_NAMES:
            assert state["written_by_name"][fmt_name] == b"\x00\x00\x00\x00"

    def test_snapshot_preserves_custom_hglobal_skips_unsafe_gdi_and_guards_open_or_oversize(
        self, monkeypatch
    ):
        monkeypatch.setattr(tp_mod.sys, "platform", "win32")
        monkeypatch.setattr(tp_mod.time, "sleep", lambda _s: None)

        fake_c, fake_u32, fake_k32, state = _make_fake_win32_clipboard(
            {
                2: ("", b"UNSAFE_CF_BITMAP"),
                3: ("", b"UNSAFE_CF_METAFILEPICT"),
                9: ("", b"UNSAFE_CF_PALETTE"),
                14: ("", b"UNSAFE_CF_ENHMETAFILE"),
                0x0082: ("", b"UNSAFE_CF_DSPBITMAP"),
                13: ("", "表格".encode("utf-16-le") + b"\x00\x00"),
                0xC020: ("Biff12", b"EXCEL_BIFF12_BYTES"),
                0xC021: ("image/svg+xml", b"<svg></svg>"),
                0xC022: ("com.figma.node", b"FIGMA_PAYLOAD"),
                0xC023: ("DataObject", b"OLE_INTERNAL_PTR"),
                0xC024: ("CanIncludeInClipboardHistory", b"\x00\x00\x00\x00"),
            }
        )
        monkeypatch.setattr(tp_mod, "_win32_clipboard_apis", lambda: (fake_c, fake_u32, fake_k32))

        snap = snapshot_win32_clipboard()
        assert snapshot_win32_clipboard is capture_win32_clipboard_snapshot
        assert bool(snap) is True
        captured_names = [entry[1] for entry in snap.win32_entries]
        captured_fmts = [entry[0] for entry in snap.win32_entries]
        assert captured_fmts == [13, 0xC020, 0xC021, 0xC022]
        assert captured_names == ["", "Biff12", "image/svg+xml", "com.figma.node"]

        # OpenClipboard failure marks open_failed=True and restore skips without EmptyClipboard
        fail_c, fail_u32, fail_k32, fail_state = _make_fake_win32_clipboard(
            open_results=[False, False, False]
        )
        monkeypatch.setattr(tp_mod, "_win32_clipboard_apis", lambda: (fail_c, fail_u32, fail_k32))
        locked_snap = snapshot_win32_clipboard()
        assert locked_snap.open_failed is True
        assert bool(locked_snap) is False
        assert restore_win32_clipboard_snapshot(locked_snap) is False
        assert fail_state["empty_calls"] == 0

        # Partial oversized snapshot (small text + >32MB custom format) marks oversized=True and never wipes
        over_c, over_u32, over_k32, over_state = _make_fake_win32_clipboard(
            {
                13: ("", "small".encode("utf-16-le") + b"\x00\x00"),
                0xC030: ("Biff12", b"HUGE"),
            },
            size_overrides={0xC030: MAX_CLIPBOARD_SNAPSHOT_BYTES + 4096},
        )
        monkeypatch.setattr(tp_mod, "_win32_clipboard_apis", lambda: (over_c, over_u32, over_k32))
        over_snap = snapshot_win32_clipboard()
        assert over_snap.oversized is True
        assert bool(over_snap) is False
        assert restore_win32_clipboard_snapshot(over_snap) is False
        assert over_state["empty_calls"] == 0


# ---------------------------------------------------------------------------
# 6. HotKeyManager Stuck-Mic Pause, Continuous Toggle-Stop, Post-Release Esc & Async UIA
# ---------------------------------------------------------------------------


class TestHotKeyManagerAndAsyncUia:
    def test_pause_while_recording_stops_timer_clears_recording_and_emits_stop(self):
        qapp = QApplication.instance() or QApplication(sys.argv)
        mgr = HotKeyManager("alt+x", parent=qapp)
        rec_stops: list[bool] = []
        rw_stops: list[bool] = []
        mgr.recording_stop.connect(lambda: rec_stops.append(True))
        mgr.rewrite_stop.connect(lambda: rw_stops.append(True))

        mgr._hold_watch_timer.start(100)
        mgr._is_recording = True
        mgr._recording_kind = "dictate"
        mgr.pause()

        assert mgr._paused is True
        assert mgr._is_recording is False
        assert mgr._hold_watch_timer.isActive() is False
        assert rec_stops == [True]
        assert rw_stops == []

        # Rewrite hold also stops cleanly on pause()
        mgr.resume()
        mgr._is_recording = True
        mgr._recording_kind = "rewrite"
        mgr.pause()
        assert mgr._is_recording is False
        assert rw_stops == [True]

    def test_continuous_toggle_stop_debounces_start_replays_short_shift_taps_and_stops_on_hold(
        self, monkeypatch
    ):
        qapp = QApplication.instance() or QApplication(sys.argv)
        mgr = HotKeyManager("shift+x", parent=qapp)
        mgr.set_continuous_trigger_mode(True)

        class _ListenerStub:
            def suppress_event(self) -> None:
                raise _Suppressed

        mgr._listener = _ListenerStub()
        monkeypatch.setattr(mgr, "_modifiers_held", lambda _mods: True)
        monkeypatch.setattr(mgr, "_async_key_down", lambda _vk: True)
        monkeypatch.setattr(mgr, "_send_menu_mask", lambda: None)
        replayed: list[int] = []
        monkeypatch.setattr(mgr, "_replay_swallowed_tap", replayed.append)

        starts: list[int] = []
        stops: list[int] = []
        mgr.continuous_listen_start.connect(lambda: starts.append(1))
        mgr.continuous_listen_stop.connect(lambda: stops.append(1))

        clock = {"t": 100.0}
        monkeypatch.setattr(hm_mod.time, "monotonic", lambda: clock["t"])

        vk_x = 0x58
        # 1) Initial >=300ms hold starts continuous listening
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _HookEvent(vk_x))
        clock["t"] += MIN_HOLD_CONTINUOUS_MS / 1000.0
        mgr._on_hold_timeout()
        mgr.set_continuous_listening_active(True)
        assert starts == [1]
        assert stops == []

        # Key-up of initial start hold -> starts 350ms debounce
        clock["t"] += 0.05
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0101, _HookEvent(vk_x))
        assert replayed == []

        # 2) Bounce press 150ms after key-up (<350ms debounce) does NOT arm toggle-stop,
        #    and short Shift+X tap still replays capital 'X'!
        clock["t"] += 0.15
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _HookEvent(vk_x))
        assert mgr._hold_pending is False
        clock["t"] += 0.08
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0101, _HookEvent(vk_x))
        assert stops == []
        assert replayed == [vk_x]

        # 3) After >=350ms debounce, a <300ms short Shift+X tap STILL replays 'X' without stopping!
        replayed.clear()
        clock["t"] += 0.30
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _HookEvent(vk_x))
        assert mgr._hold_pending is True
        clock["t"] += 0.12
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0101, _HookEvent(vk_x))
        assert stops == []
        assert replayed == [vk_x]

        # 4) Fresh >=300ms hold emits continuous_listen_stop (and auto-repeat before release never restarts)
        replayed.clear()
        clock["t"] += 0.10
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _HookEvent(vk_x))
        clock["t"] += MIN_HOLD_CONTINUOUS_MS / 1000.0
        mgr._on_hold_timeout()
        mgr.set_continuous_listening_active(False)
        assert stops == [1]
        assert starts == [1]

        # Auto-repeat while user's finger is still releasing stop hold does not re-arm start
        clock["t"] += 0.05
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _HookEvent(vk_x))
        assert mgr._hold_pending is False
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0101, _HookEvent(vk_x))
        assert replayed == []
        assert starts == [1]

    def test_post_release_esc_cancels_processing_cleanly(self):
        mgr = HotKeyManager("alt+x")
        esc_cancels: list[int] = []
        rec_cancels: list[int] = []
        mgr.esc_cancel_requested.connect(lambda: esc_cancels.append(1))
        mgr.recording_cancel.connect(lambda: rec_cancels.append(1))

        # Idle Esc -> does not emit esc_cancel_requested
        mgr._on_press(keyboard.Key.esc)
        mgr._on_release(keyboard.Key.esc)
        assert esc_cancels == []

        # Post-release recognizing state -> Esc emits esc_cancel_requested once
        mgr.set_cancellable_processing(True)
        assert mgr.cancellable_processing is True
        mgr._on_press(keyboard.Key.esc)
        mgr._on_release(keyboard.Key.esc)
        assert esc_cancels == [1]
        assert rec_cancels == []
        assert mgr.cancellable_processing is False

        # Mid-hold recording Esc -> emits recording_cancel, not esc_cancel_requested
        mgr.set_cancellable_processing(True)
        mgr._is_recording = True
        mgr._recording_kind = "dictate"
        mgr._on_press(keyboard.Key.esc)
        assert rec_cancels == [1]
        assert esc_cancels == [1]

    def test_read_selected_text_uia_runs_on_daemon_sta_thread_with_timeout(self, monkeypatch):
        monkeypatch.setattr(wi_mod.sys, "platform", "win32")
        caller_tid = threading.get_ident()
        observed: dict[str, object] = {}

        fake_pythoncom = types.SimpleNamespace(
            CoInitialize=lambda: observed.__setitem__("coinit_tid", threading.get_ident()),
            CoUninitialize=lambda: observed.__setitem__("couninit", True),
        )

        class _FakeRange:
            def GetText(self, _max_len: int) -> str:
                return "异步读取的选中文本"

        class _FakeRanges:
            Length = 1

            def GetElement(self, _idx: int) -> _FakeRange:
                return _FakeRange()

        class _FakePattern:
            def GetSelection(self) -> _FakeRanges:
                observed["worker_daemon"] = threading.current_thread().daemon
                return _FakeRanges()

        class _FakeFocused:
            def GetCurrentPattern(self, pattern_id: int) -> _FakePattern | None:
                return _FakePattern() if pattern_id == 10014 else None

        class _FakeUia:
            def GetFocusedElement(self) -> _FakeFocused:
                return _FakeFocused()

        fake_win32com_client = types.SimpleNamespace(
            Dispatch=lambda _prog_id: _FakeUia(),
        )
        fake_win32com = types.ModuleType("win32com")
        fake_win32com.client = fake_win32com_client  # type: ignore[attr-defined]

        with patch.dict(
            sys.modules,
            {
                "pythoncom": fake_pythoncom,
                "win32com": fake_win32com,
                "win32com.client": fake_win32com_client,
            },
        ):
            text = read_selected_text_uia(timeout_s=0.5)
            assert text == "异步读取的选中文本"
            assert observed["coinit_tid"] != caller_tid
            assert observed["worker_daemon"] is True
            assert observed["couninit"] is True

            # Simulate hung target window blocking COM RPC -> returns None promptly on timeout
            release_hang = threading.Event()

            class _HungUia:
                def GetFocusedElement(self):
                    release_hang.wait(timeout=2.0)
                    return None

            fake_win32com_client.Dispatch = lambda _prog_id: _HungUia()
            t0 = time.perf_counter()
            try:
                hung_result = read_selected_text_uia(timeout_s=0.05)
                elapsed = time.perf_counter() - t0
                assert hung_result is None
                assert elapsed < 0.45
            finally:
                release_hang.set()
