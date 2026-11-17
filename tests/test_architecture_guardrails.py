"""Automated architectural ratchet and boundary guardrails.

Enforces the Phase 0+ engineering invariants so parallel development can only
improve structural health and can never re-introduce retired anti-patterns:
1. Single unified ``sayink/llm/`` transport kernel (no duplicate SSE/HTTP stacks).
2. Windows 10/11 native focus: zero ``xdotool`` / ``osascript`` calls in ``sayink/``,
   and fully qualified ``{sys}\\taskkill.exe`` in ``installer/SayInk-Setup.iss``.
3. Monotonic ratchets on legacy Mixins, ``_delegate_attr``, and ``_LEGACY_PAGE_ATTRS``
   (counts can decrease to 0 as tracks land, never increase).
4. Strict ``C901 <= 15`` and 100% return-type annotations on all newly refactored
   packages (``sayink/llm/``, ``sayink/platform/``, ``sayink/contracts.py``).
5. Zero cross-module ``app._*`` / ``page._*`` private probing in newly added test suites.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAYINK_DIR = ROOT / "sayink"
TESTS_DIR = ROOT / "tests"

# Ratchet ceilings — lower these monotonically as Track A / B / C land; never raise.
MAX_MIXIN_CLASSES = 0  # All *Mixin inheritance classes eliminated via composition!
MAX_APP_DELEGATE_ATTRS = 0  # All 29 _delegate_attr shims eliminated across Track B & Track A!
MAX_SETTINGS_LEGACY_ATTRS = 118  # -> target 0 in Phase 2

STRICT_TYPED_PATHS = (
    SAYINK_DIR / "contracts.py",
    SAYINK_DIR / "asr_correction.py",
    SAYINK_DIR / "docx_io.py",
    SAYINK_DIR / "recognition_settings.py",
    SAYINK_DIR / "reload_coordinator.py",
    SAYINK_DIR / "output_pipeline.py",
    SAYINK_DIR / "text_polisher.py",
    SAYINK_DIR / "llm",
    SAYINK_DIR / "platform",
    SAYINK_DIR / "ui" / "session_stream_view.py",
    SAYINK_DIR / "ui" / "meeting_live_controller.py",
    SAYINK_DIR / "ui" / "meeting_import_controller.py",
)

# Static pre-Phase-0 legacy test allowlist. Any newly added test_*.py file must
# assert through public contracts rather than ``app._*`` or ``page/win/window._*``.
_LEGACY_WHITEBOX_TEST_FILES = frozenset(
    {
        "test_about_ux.py", "test_app.py", "test_app_continuous_turns.py",
        "test_app_file_transcription.py", "test_app_history_wiring.py",
        "test_app_meeting_turns.py", "test_app_meeting_wiring.py",
        "test_app_quit_relaunch.py", "test_asr_bench.py", "test_asr_context.py",
        "test_asr_correction.py", "test_asr_short_retry.py", "test_audio_devices.py",
        "test_audio_file_decoder.py", "test_audio_recorder.py",
        "test_audio_recorder_configure.py", "test_audio_recorder_flow.py",
        "test_audio_recovery.py", "test_audio_utils.py", "test_audit_audio_ai.py",
        "test_audit_meeting_data.py", "test_audit_sys_paste.py",
        "test_audit_ui_meeting.py", "test_audit_ui_shell.py",
        "test_auto_start_command.py", "test_aux_models.py",
        "test_batch3_core_polish.py", "test_batch3_meeting_db_floating.py",
        "test_build_bundle_models.py", "test_build_signing_hooks.py",
        "test_clipboard_copy.py", "test_config.py", "test_config_audio.py",
        "test_config_history.py", "test_dependency_check.py", "test_exit_guard.py",
        "test_floating_window.py", "test_history_export_and_leave.py",
        "test_history_store.py", "test_history_window.py", "test_hotkey_hold_timer.py",
        "test_hotkey_manager.py", "test_hotwords_silent_reload.py",
        "test_import_ux.py", "test_installer_user_scope.py", "test_island_surface.py",
        "test_lane_echo.py", "test_live_theme_colors.py", "test_llm_transport.py",
        "test_main.py", "test_main_window.py", "test_media_foundation_streaming.py",
        "test_media_import.py", "test_meeting_audio.py", "test_meeting_batch.py",
        "test_meeting_live_speakers.py", "test_meeting_reliability.py",
        "test_meeting_session.py", "test_meeting_session_policy.py",
        "test_meeting_speaker_accuracy.py", "test_meeting_speaker_bench.py",
        "test_meeting_speakers.py", "test_meeting_store.py",
        "test_meeting_timeline_bench.py", "test_meeting_turn_bench.py",
        "test_meeting_turn_recognition.py", "test_meeting_ui_fixes.py",
        "test_meeting_ux.py", "test_minutes.py", "test_minutes_templates.py",
        "test_native_ui_runtime.py", "test_onboarding.py", "test_output_pipeline.py",
        "test_phase3_wave1_regressions.py", "test_polish_fidelity.py",
        "test_prompts.py", "test_readme_features.py", "test_recognition_continuity.py",
        "test_recognition_replay.py", "test_recognition_settings.py",
        "test_recording_options_ui.py", "test_release_gate.py",
        "test_reload_coordinator.py", "test_runtime_ci_fixes.py",
        "test_secret_store.py", "test_separate_capture_lanes.py",
        "test_settings_general.py", "test_settings_glossary_commit.py",
        "test_settings_history.py", "test_settings_responsiveness.py",
        "test_settings_typed_pages.py", "test_sound_manager.py",
        "test_speaker_activity.py", "test_speaker_clustering.py",
        "test_speaker_eval.py", "test_speaker_history.py", "test_speaker_naming.py",
        "test_speaker_short_context.py", "test_speaker_turn_context.py",
        "test_speaker_turns.py", "test_speech_recognizer.py",
        "test_swarm_core_lifecycle.py", "test_swarm_data_pipeline.py",
        "test_swarm_ui_foundation.py", "test_swarm_ui_workspaces.py",
        "test_text_paster.py", "test_text_paster_procname.py", "test_text_polisher.py",
        "test_textgrid_reference.py", "test_theme_resolve.py",
        "test_transcript_import.py", "test_tray_icon.py", "test_ui_consistency.py",
        "test_ui_font.py", "test_ui_shell_fixes.py", "test_ui_styles.py",
        "test_update_controller.py", "test_updater.py", "test_updater_signature.py",
        "test_user_data_migration.py", "test_ux_redesign.py", "test_vad_segmenter.py",
        "test_vad_timeline.py", "test_version.py", "test_windows_identity.py",
    }
)

_PRIVATE_PROBE_RE = re.compile(r"\b(?:app|page|win|window)\._[a-zA-Z]\w*")


def _iter_py_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob("*.py") if "__pycache__" not in p.parts)


def _mccabe_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Compute McCabe cyclomatic complexity of a single function AST node (excluding nested defs)."""
    complexity = 1
    stack: list[ast.AST] = list(node.body)
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(
            cur,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.IfExp,
                ast.ExceptHandler,
            ),
        ):
            complexity += 1
        elif isinstance(cur, ast.BoolOp):
            complexity += len(cur.values) - 1
        elif isinstance(cur, ast.comprehension):
            complexity += len(cur.ifs) + 1
        stack.extend(ast.iter_child_nodes(cur))
    return complexity


def test_unified_llm_transport_has_zero_duplication():
    from sayink import llm, llm_transport, text_polisher

    assert llm_transport.chat_completion is llm.chat_completion
    assert text_polisher.chat_completion is llm.chat_completion
    assert llm_transport.stream_chat_completion is llm.stream_chat_completion
    assert text_polisher.stream_chat_completion is llm.stream_chat_completion
    assert llm_transport.completion is llm.async_completion

    tp_source = (SAYINK_DIR / "text_polisher.py").read_text(encoding="utf-8")
    lt_source = (SAYINK_DIR / "llm_transport.py").read_text(encoding="utf-8")
    assert "def _iter_sse_payloads" not in tp_source
    assert "async def _stream_text" not in lt_source


def test_windows_native_platform_hygiene_and_installer_paths():
    forbidden = re.compile(r"\b(xdotool|osascript)\b")
    offenders: list[str] = []
    for path in _iter_py_files(SAYINK_DIR):
        text = path.read_text(encoding="utf-8")
        if forbidden.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"Retired non-Windows subprocess tools found in: {offenders}"

    iss_text = (ROOT / "installer" / "SayInk-Setup.iss").read_text(encoding="utf-8")
    assert 'Filename: "taskkill"' not in iss_text
    assert "Exec('taskkill'" not in iss_text
    assert r"{sys}\taskkill.exe" in iss_text


def test_legacy_mixin_and_private_delegation_ratchets():
    mixin_classes: list[str] = []
    for path in _iter_py_files(SAYINK_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Mixin"):
                mixin_classes.append(f"{path.relative_to(ROOT)}:{node.name}")
    assert len(mixin_classes) <= MAX_MIXIN_CLASSES, (
        f"New Mixin classes are forbidden (use composition): {mixin_classes}"
    )

    app_text = (SAYINK_DIR / "app.py").read_text(encoding="utf-8")
    delegate_count = len(re.findall(r"=\s*_delegate_attr\(", app_text))
    assert delegate_count <= MAX_APP_DELEGATE_ATTRS, (
        f"Do not add new _delegate_attr shims on App ({delegate_count} > {MAX_APP_DELEGATE_ATTRS})"
    )

    from sayink.ui import settings_window

    assert len(settings_window._LEGACY_PAGE_ATTRS) <= MAX_SETTINGS_LEGACY_ATTRS


def test_refactored_packages_enforce_complexity_ceiling_and_return_types():
    complex_offenders: list[str] = []
    untyped_offenders: list[str] = []

    for target in STRICT_TYPED_PATHS:
        for path in _iter_py_files(target):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel = path.relative_to(ROOT)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    score = _mccabe_complexity(node)
                    if score > 15:
                        complex_offenders.append(f"{rel}:{node.lineno} {node.name} (C901={score})")
                    if node.returns is None:
                        untyped_offenders.append(f"{rel}:{node.lineno} {node.name}")

    assert complex_offenders == [], f"Functions exceeding C901 <= 15: {complex_offenders}"
    assert untyped_offenders == [], f"Functions missing return type annotation: {untyped_offenders}"


def test_new_test_files_do_not_probe_cross_module_privates():
    violations: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name in _LEGACY_WHITEBOX_TEST_FILES or path.name == "test_architecture_guardrails.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _PRIVATE_PROBE_RE.search(line):
                violations.append(f"{path.name}:{lineno}: {stripped}")
    assert violations == [], (
        "New test suites must assert via public contracts, not app._*/page._*/win._*: "
        f"{violations}"
    )


def test_fakes_satisfy_runtime_protocols():
    from sayink.contracts import SecretStoreProtocol
    from sayink.platform.window_input import WindowInputBackend
    from tests.fakes import FakeSecretStore, FakeWindowInputBackend

    assert isinstance(FakeSecretStore(), SecretStoreProtocol)
    assert isinstance(FakeWindowInputBackend(), WindowInputBackend)
