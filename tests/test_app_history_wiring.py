from __future__ import annotations

from unittest.mock import patch

import numpy as np

from tests.helpers.app_harness import app_harness
from sayink.app import OutputStage
from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.runtime_status import ModelLoadPhase


def _audio(seconds: float = 0.2) -> np.ndarray:
    return np.ones(int(TARGET_SAMPLE_RATE * seconds), dtype=np.float32)


def _start_continuous_user_session(app) -> None:
    with patch.object(app, "_start_continuous_listening") as start:
        app._on_continuous_hotkey_start()
        start.assert_called_once()


def _begin_and_finish_asr(app, paster, text: str, audio: np.ndarray | None = None):
    app._begin_transcription(audio if audio is not None else _audio())
    app._on_final_result(text)
    return paster.paste_async.call_args[0][1]


def _drive_segment(app, paster, text: str, result: str = "pasted") -> None:
    callback = _begin_and_finish_asr(app, paster, text)
    callback(result)


def _enqueued_records(history):
    return [call.args[0] for call in history.enqueue.call_args_list]


def test_live_continuous_session_is_published_as_open_history() -> None:
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        from unittest.mock import MagicMock

        panel = MagicMock()
        app._main = MagicMock()
        app._main.history_panel = panel
        h["recorder"].is_continuous = True
        context = app._capture_segment_context()
        assert context is not None
        panel.set_open_session.assert_called_with(context.session_id)


def test_start_enqueues_history_cleanup_once_with_active_session() -> None:
    with app_harness(
        {
            "history.retention_days": 7,
            "history.max_entries": 123,
        }
    ) as h:
        app = h["app"]
        app._current_session_id = "active-session"

        app.start()

        h["history"].enqueue_cleanup.assert_called_once_with(
            retention_days=7,
            max_entries=123,
            active_session_id="active-session",
        )


def test_start_shows_history_startup_notice_in_tray() -> None:
    """F-21: a rebuilt or unopenable history.db is announced once at startup."""
    with app_harness() as h:
        h["history"].startup_notice = "历史记录数据库已损坏，原文件已备份为 history.corrupt-x.db"
        h["app"].start()
        assert h["tray"].showMessage.called
        assert "已损坏" in h["tray"].showMessage.call_args[0][1]


def test_start_stays_quiet_when_history_is_healthy() -> None:
    with app_harness() as h:
        h["history"].startup_notice = ""
        h["app"].start()
        h["tray"].showMessage.assert_not_called()


def test_history_write_failure_warns_once_per_run() -> None:
    """F-21: the writer-thread failure callback reaches the tray exactly once."""
    with app_harness() as h:
        app = h["app"]
        callback = h["history"].add_failed_callback.call_args[0][0]
        callback("历史记录保存失败")
        callback("历史记录保存失败")
        from PyQt6.QtWidgets import QApplication

        QApplication.processEvents()
        assert h["tray"].showMessage.call_count == 1
        assert "保存失败" in h["tray"].showMessage.call_args[0][1]
        assert app._history_failure_notified is True


def test_quit_closes_history_before_saving_config() -> None:
    with app_harness() as h:
        events: list[str] = []
        h["history"].close.side_effect = lambda **_: events.append("history.close")
        h["config"].save_immediate.side_effect = lambda: events.append("config.save")

        h["app"]._quit()

        assert events[:2] == ["history.close", "config.save"]


def test_continuous_three_segments_share_session_and_increment_seq_with_texts() -> None:
    overrides = {
        "audio.trigger_mode": "continuous",
        "audio.input_source": "mixed",
        "llm.enabled": True,
        "llm.api_url": "https://api.example.test/v1",
        "llm.api_key": "key",
        "llm.model_name": "gpt-test",
        "stt.model_id": "fire-red-asr2-ctc-zh_en-int8",
    }
    with app_harness(overrides) as h:
        app = h["app"]
        app._current_session_id = "stale"
        app._current_seq = 42
        _start_continuous_user_session(app)

        for raw, polished in (
            ("raw one", "polished one"),
            ("raw two", "polished two"),
            ("raw three", "raw three"),
        ):
            app._begin_transcription(_audio())
            app._on_final_result(raw)
            if raw == "raw three":
                app._on_polish_error("network down")
            else:
                app._on_polish_complete(polished)
            h["paster"].paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        session_ids = {record.session_id for record in records}
        assert len(records) == 3
        assert len(session_ids) == 1
        assert "stale" not in session_ids
        assert [record.seq for record in records] == [0, 1, 2]
        assert [(r.raw_text, r.polished_text) for r in records] == [
            ("raw one", "polished one"),
            ("raw two", "polished two"),
            ("raw three", "raw three"),
        ]
        assert all(record.source == "mixed" for record in records)
        assert all(record.trigger_mode == "continuous" for record in records)
        assert all(record.model == "fire-red-asr2-ctc-zh_en-int8" for record in records)


def test_asr_error_auto_restart_keeps_continuous_session_id() -> None:
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        _drive_segment(app, h["paster"], "before error")
        first_record = _enqueued_records(h["history"])[0]

        h["recorder"].is_continuous = False
        app._continuous_user_stopped = False
        app._on_recognizer_error("ASR crashed")
        _drive_segment(app, h["paster"], "after restart")

        records = _enqueued_records(h["history"])
        assert [record.seq for record in records] == [0, 1]
        assert records[1].session_id == first_record.session_id


def test_pending_paste_callback_freezes_segment_values_across_race() -> None:
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]

        first_callback = _begin_and_finish_asr(app, h["paster"], "segment n")
        second_callback = _begin_and_finish_asr(app, h["paster"], "segment n plus one")

        first_callback("pasted")
        second_callback("pasted")

        records = _enqueued_records(h["history"])
        assert [record.raw_text for record in records] == ["segment n", "segment n plus one"]
        assert [record.seq for record in records] == [0, 1]


def test_history_disabled_skips_enqueue_and_mid_run_disable_affects_next_segment() -> None:
    with app_harness({"history.enabled": False}) as h:
        _drive_segment(h["app"], h["paster"], "disabled from start")
        h["history"].enqueue.assert_not_called()

    with app_harness() as h:
        _drive_segment(h["app"], h["paster"], "enabled segment")
        assert h["history"].enqueue.call_count == 1

        h["config"].set("history.enabled", False)
        _drive_segment(h["app"], h["paster"], "disabled segment")

        assert h["history"].enqueue.call_count == 1


def test_created_at_is_segment_finalization_time_not_begin() -> None:
    with app_harness() as h:
        app = h["app"]
        app._begin_transcription(_audio())
        with patch("sayink.app.time.time", return_value=1_005.5):
            app._on_final_result("finalized later")
            h["paster"].paste_async.call_args[0][1]("pasted")

        record = _enqueued_records(h["history"])[0]
        assert record.created_at == 1_005_500


def test_queued_segments_after_user_stop_keep_same_session() -> None:
    """Late/queued segments after Esc must keep the same session_id (ADR-0009/0001)."""
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        _start_continuous_user_session(app)
        h["recorder"].is_continuous = True

        first_cb = _begin_and_finish_asr(app, h["paster"], "first")
        queued = _audio()
        app._enqueue_audio(queued)
        session_before_stop = app._current_session_id

        app._stop_continuous_user_session()
        first_cb("pasted")

        app._begin_transcription(app._pop_queued_audio()[0])
        app._on_final_result("late after esc")
        h["paster"].paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        assert [r.raw_text for r in records] == ["first", "late after esc"]
        assert records[0].session_id == session_before_stop
        assert records[1].session_id == session_before_stop
        assert [r.seq for r in records] == [0, 1]


def test_restart_keeps_queued_old_session_audio_in_old_session() -> None:
    with app_harness({"audio.trigger_mode": "continuous", "audio.input_source": "mixed"}) as h:
        app, paster, recorder = h["app"], h["paster"], h["recorder"]
        recorder.consume_segment_route.return_value = ""
        _start_continuous_user_session(app)
        recorder.is_continuous = True
        old_speakers = app._speakers

        app._on_segment_ready(_audio())
        app._on_segment_ready(_audio())
        app._on_final_result("旧场第一句")
        first_callback = paster.paste_async.call_args[0][1]

        app._stop_continuous_user_session()
        recorder.is_continuous = False
        recorder.input_source = "microphone"
        _start_continuous_user_session(app)
        recorder.is_continuous = True
        app._on_segment_ready(_audio())

        assert app._segment_queue[0].context.speakers is old_speakers
        assert app._speakers is not old_speakers

        first_callback("pasted")
        for text in ("旧场第二句", "新场第一句"):
            app._pump_segment_queue()
            app._on_final_result(text)
            paster.paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        assert [r.raw_text for r in records] == ["旧场第一句", "旧场第二句", "新场第一句"]
        old_session, new_session = records[0].session_id, records[2].session_id
        assert old_session != new_session
        assert [(r.session_id, r.seq) for r in records] == [
            (old_session, 0),
            (old_session, 1),
            (new_session, 0),
        ]
        assert [r.source for r in records] == ["mixed", "mixed", "mic"]


def _backlog_session(h):
    app, recorder = h["app"], h["recorder"]
    recorder.consume_segment_route.return_value = ""
    _start_continuous_user_session(app)
    recorder.is_continuous = True
    recorder.stop_continuous.side_effect = lambda: setattr(recorder, "is_continuous", False)
    app._output_busy = True
    return app


def test_backlog_at_limit_keeps_listening() -> None:
    from sayink.app import MAX_BACKLOG_AUDIO_SECONDS

    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = _backlog_session(h)
        for _ in range(MAX_BACKLOG_AUDIO_SECONDS // 5):
            app._on_segment_ready(_audio(5.0))

        h["recorder"].stop_continuous.assert_not_called()
        assert app._continuous_user_stopped is False


def test_backlog_over_limit_pauses_listening_without_dropping_audio() -> None:
    from sayink.app import MAX_BACKLOG_AUDIO_SECONDS

    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = _backlog_session(h)
        count = MAX_BACKLOG_AUDIO_SECONDS // 5 + 1
        for _ in range(count):
            app._on_segment_ready(_audio(5.0))

        h["recorder"].stop_continuous.assert_called_once()
        assert app._continuous_user_stopped is True
        assert len(app._segment_queue) == count
        assert "跟不上" in h["tray"].showMessage.call_args[0][1]
        assert "暂停监听" in h["floating"].show_continuous_stopped.call_args[0][0]


class TestDictationSharesMeetingSpeakerStack:
    """持续转写 labels speakers with the meeting stack (embedder thresholds,
    continuity prior) and re-clusters once the session has fully drained."""

    def test_new_session_uses_embedder_thresholds_and_sticky_prior(self) -> None:
        from sayink.meeting_speakers import MeetingSpeakerSession
        from sayink.speaker_embedding import STICKY_GAP_MS

        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            app = h["app"]
            embedder = app._speaker_embedder()
            session = app._new_speaker_session()
            assert isinstance(session, MeetingSpeakerSession)
            assert session._threshold == embedder.threshold
            assert session._sticky_threshold == embedder.sticky_threshold
            assert session._sticky_gap_ms == STICKY_GAP_MS
            assert session.history == []  # keep_history=True
            assert app._speaker_embedder() is embedder  # cached

    def test_stop_with_nothing_pending_reclusters_and_relabels_history(self) -> None:
        from unittest.mock import MagicMock

        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            app, recorder = h["app"], h["recorder"]
            _start_continuous_user_session(app)
            recorder.is_continuous = True
            recorder.stop_continuous.side_effect = lambda: setattr(recorder, "is_continuous", False)
            # An empty session has no id and nothing to renumber.
            with patch("sayink.app.recluster") as rc:
                app._stop_continuous_user_session()
            rc.assert_not_called()

            _start_continuous_user_session(app)
            recorder.is_continuous = True
            _drive_segment(app, h["paster"], "第一句")
            session_id = app._current_session_id
            assert session_id
            fake = MagicMock(speaker_count=3, history=[("h", 1)])
            app._speakers = fake
            with patch("sayink.app.recluster", return_value={1: 1, 2: 1, 3: 2}) as rc:
                app._stop_continuous_user_session()
            rc.assert_called_once_with(fake.history, threshold=app._speaker_embedder().recluster_threshold)
            h["history"].relabel_session_speakers.assert_called_once_with(session_id, {1: 1, 2: 1, 3: 2})
            # Once per session: a second stop/drain does not renumber again.
            with patch("sayink.app.recluster", return_value={1: 2, 2: 1, 3: 3}):
                app._maybe_finish_dictation_speakers()
            assert h["history"].relabel_session_speakers.call_count == 1

    def test_keyed_turns_relabel_individual_rows_instead_of_renumbering(self) -> None:
        """When the session kept per-segment turns, the stop pass re-assigns
        each row by seq (so one wrong sentence can move) and never falls back
        to the old number-to-number merge."""
        from unittest.mock import MagicMock

        from sayink.speaker_clustering import TurnObservation

        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            app, recorder = h["app"], h["recorder"]
            _start_continuous_user_session(app)
            recorder.is_continuous = True
            recorder.stop_continuous.side_effect = lambda: setattr(recorder, "is_continuous", False)
            _drive_segment(app, h["paster"], "第一句")
            session_id = app._current_session_id
            a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
            b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
            turns = [TurnObservation(seq, 1 if seq % 2 == 0 else 2, a if seq % 2 == 0 else b, 4000) for seq in range(6)]
            turns.append(TurnObservation(6, 2, a, 4000))     # stuck in the wrong group online
            turns.append(TurnObservation(-7, 1, a, 4000))    # unkeyed: carries no row
            app._speakers = MagicMock(speaker_count=2, history=[("h", 1)], turns=turns)
            with patch("sayink.app.recluster") as rc:
                app._stop_continuous_user_session()
            rc.assert_not_called()
            h["history"].relabel_session_speakers.assert_not_called()
            h["history"].relabel_session_segments.assert_called_once_with(
                session_id, {0: 1, 1: 2, 2: 1, 3: 2, 4: 1, 5: 2, 6: 1})

    def test_recluster_waits_for_the_last_queued_segment(self) -> None:
        from unittest.mock import MagicMock

        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            app, recorder, paster = h["app"], h["recorder"], h["paster"]
            recorder.consume_segment_route.return_value = ""
            _start_continuous_user_session(app)
            recorder.is_continuous = True
            recorder.stop_continuous.side_effect = lambda: setattr(recorder, "is_continuous", False)
            callback = _begin_and_finish_asr(app, paster, "第一句")
            session_id = app._current_session_id
            app._speakers = MagicMock(speaker_count=2, history=[])
            with patch("sayink.app.recluster", return_value={1: 1, 2: 1}):
                app._stop_continuous_user_session()
                h["history"].relabel_session_speakers.assert_not_called()
                callback("pasted")
            h["history"].relabel_session_speakers.assert_called_once_with(session_id, {1: 1, 2: 1})

    def test_single_speaker_or_identity_mapping_skips_relabel(self) -> None:
        from unittest.mock import MagicMock

        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            app, recorder = h["app"], h["recorder"]
            _start_continuous_user_session(app)
            recorder.is_continuous = True
            recorder.stop_continuous.side_effect = lambda: setattr(recorder, "is_continuous", False)
            _drive_segment(app, h["paster"], "一")
            app._speakers = MagicMock(speaker_count=1, history=[])
            with patch("sayink.app.recluster") as rc:
                app._stop_continuous_user_session()
            rc.assert_not_called()
            h["history"].relabel_session_speakers.assert_not_called()

            _start_continuous_user_session(app)
            recorder.is_continuous = True
            _drive_segment(app, h["paster"], "二")
            app._speakers = MagicMock(speaker_count=2, history=[])
            with patch("sayink.app.recluster", return_value={1: 1, 2: 2}):
                app._stop_continuous_user_session()
            h["history"].relabel_session_speakers.assert_not_called()


class TestDictationSharesRecognitionSettings:
    """stt.glossary / stt.language / stt.vad drive dictation exactly as they
    drive meetings, so a meeting start never has to reload the model."""

    def test_configure_stt_passes_shared_hotwords_and_language(self) -> None:
        overrides = {"stt.glossary": [{"term": "SayInk"}, {"term": "张鑫"}], "stt.language": "en"}
        with app_harness(overrides) as h:
            app = h["app"]
            h["recognizer"].configure.reset_mock()
            app._configure_stt()
            kwargs = h["recognizer"].configure.call_args.kwargs
            assert kwargs == {"hotwords": "SayInk,张鑫", "language": "en"}

    def test_configure_stt_records_the_model_it_actually_falls_back_to(self) -> None:
        """A saved model that is not on disk (deleted, or the upgrade migration
        pointed at a default this build does not bundle) used to leave the
        engine page empty while another model silently ran."""
        with app_harness({"stt.model_id": "funasr-nano"}) as h:
            app = h["app"]
            with patch(
                "sayink.speech_recognizer.is_model_downloaded",
                side_effect=lambda mid: mid == "sensevoice",
            ):
                app._configure_stt()
            assert h["config"].get("stt.model_id") == "sensevoice"
            assert h["recognizer"].configure.call_args.args[0] == "sensevoice"

    def test_dictation_segmenter_uses_silero_when_downloaded(self) -> None:
        with app_harness({"stt.vad": "silero"}) as h:
            app = h["app"]
            with patch("sayink.app.aux_model_path", return_value="C:/models/silero_vad.onnx"):
                app._apply_dictation_segmenter_profile()
            h["recorder"].set_segmenter_profile.assert_called_with(
                vad="silero", vad_model_path="C:/models/silero_vad.onnx", meeting=False
            )

    def test_dictation_preserves_silero_request_for_recorder_fallback_status(self) -> None:
        with app_harness({"stt.vad": "silero"}) as h:
            app = h["app"]
            with patch("sayink.app.aux_model_path", return_value=None):
                app._apply_dictation_segmenter_profile()
            h["recorder"].set_segmenter_profile.assert_called_with(vad="silero", vad_model_path=None, meeting=False)

        with app_harness({"stt.vad": "energy"}) as h:
            app = h["app"]
            with patch("sayink.app.aux_model_path") as path:
                app._apply_dictation_segmenter_profile()
            path.assert_not_called()
            h["recorder"].set_segmenter_profile.assert_called_with(vad="energy", vad_model_path=None, meeting=False)

    def test_recognition_settings_change_rebuilds_embedder_and_reconfigures(self) -> None:
        with app_harness() as h:
            app = h["app"]
            before = app._speaker_embedder()
            h["recognizer"].configure.reset_mock()
            h["recorder"].set_segmenter_profile.reset_mock()
            app._on_recognition_settings_changed()
            h["recognizer"].configure.assert_called_once()
            h["recorder"].set_segmenter_profile.assert_called_once()
            assert app._speaker_embedder() is not before


def test_history_commit_refreshes_open_main_window() -> None:
    from PyQt6.QtWidgets import QApplication
    from unittest.mock import MagicMock

    with app_harness() as h:
        app = h["app"]
        history_ui = MagicMock()
        app._main = MagicMock()
        app._main.history_panel = history_ui

        callback = h["history"].add_committed_callback.call_args[0][0]
        callback()
        QApplication.processEvents()

        history_ui.refresh.assert_called()


def test_hold_records_one_history_row_for_the_whole_utterance() -> None:
    with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
        app = h["app"]
        h["recorder"].is_recording = True
        app._begin_transcription(_audio(0.2))
        app._on_final_result("前一段")
        app._begin_transcription(_audio(0.3))
        app._on_final_result("后一段")
        h["paster"].paste_async.assert_not_called()
        h["history"].enqueue.assert_not_called()

        h["recorder"].is_recording = False
        app._is_transcribing = False
        app._on_recording_finished(np.zeros(1, dtype=np.float32))
        h["paster"].paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        assert h["paster"].paste_async.call_args[0][0] == "前一段后一段"
        assert len(records) == 1
        assert records[0].raw_text == "前一段后一段"
        assert records[0].polished_text == ""
        assert records[0].trigger_mode == "hotkey"
        assert records[0].seq == 0
        assert records[0].duration_ms == 500


def test_hold_exhausted_short_retry_finishes_without_output_and_next_recording_can_succeed() -> None:
    from sayink.speech_recognizer import TranscribeWorker
    from tests.test_asr_short_retry import Recognizer, audio

    with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
        app = h["app"]
        engine = Recognizer(["", "", "下一次录音"])

        def transcribe(samples, **options):
            worker = TranscribeWorker(engine, samples, **options)
            worker.result_ready.connect(app._on_final_result)
            worker.error.connect(app._on_recognizer_error)
            worker.run()

        h["recognizer"].transcribe_final.side_effect = transcribe
        with patch("sayink.app.QTimer.singleShot"):
            h["recorder"].is_recording = True
            app._begin_transcription(audio())
            h["recorder"].is_recording = False
            app._on_recording_finished(np.zeros(1, dtype=np.float32))
            assert len(engine.seen) == 2 and app._pending_segment_count() == 0
            h["paster"].paste_async.assert_not_called()
            h["history"].enqueue.assert_not_called()

            app._on_recording_start()
            h["recorder"].is_recording = True
            app._begin_transcription(audio())
            h["recorder"].is_recording = False
            app._on_recording_finished(np.zeros(1, dtype=np.float32))
            assert h["paster"].paste_async.call_args.args[0] == "下一次录音"
            h["paster"].paste_async.call_args.args[1]("pasted")
        assert len(engine.seen) == 3
        records = _enqueued_records(h["history"])
        assert len(records) == 1 and records[0].raw_text == "下一次录音"
        assert records[0].duration_ms == 350


def test_hold_tail_and_polish_save_the_full_utterance() -> None:
    overrides = {
        "audio.trigger_mode": "hotkey",
        "llm.enabled": True,
        "llm.api_url": "https://api.example.test/v1",
        "llm.api_key": "key",
        "llm.model_name": "gpt-test",
    }
    with app_harness(overrides) as h:
        app = h["app"]
        h["recorder"].is_recording = True
        app._begin_transcription(_audio(0.2))
        app._on_final_result("前一段")
        h["polisher"].polish.assert_not_called()

        h["recorder"].is_recording = False
        app._begin_transcription(_audio(0.2))
        app._on_final_result("尾巴")
        h["polisher"].polish.assert_called_once()
        assert h["polisher"].polish.call_args[0][0] == "前一段尾巴"

        app._on_polish_complete("润色后的全文")
        h["paster"].paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        assert h["paster"].paste_async.call_args[0][0] == "润色后的全文"
        assert len(records) == 1
        assert records[0].raw_text == "前一段尾巴"
        assert records[0].polished_text == "润色后的全文"
        assert records[0].duration_ms == 400
        assert records[0].trigger_mode == "hotkey"


def test_hold_next_recording_during_polish_keeps_each_utterance_raw_text() -> None:
    overrides = {
        "audio.trigger_mode": "hotkey",
        "llm.enabled": True,
        "llm.api_url": "https://api.example.test/v1",
        "llm.api_key": "key",
        "llm.model_name": "gpt-test",
    }
    with app_harness(overrides) as h:
        app, paster, polisher = h["app"], h["paster"], h["polisher"]
        h["recorder"].is_recording = True
        app._begin_transcription(_audio(0.2))
        app._on_final_result("A句原文")
        h["recorder"].is_recording = False
        app._on_recording_finished(np.zeros(1, dtype=np.float32))
        assert polisher.polish.call_args[0][0] == "A句原文"

        app._on_recording_start()
        h["recorder"].start.assert_called_once_with(continuous=False)
        h["recorder"].is_recording = True
        app._on_segment_ready(_audio(0.3))
        assert len(app._segment_queue) == 1

        app._on_polish_error("network down")
        assert [c.args[0] for c in paster.paste_async.call_args_list] == ["A句原文"]
        paster.paste_async.call_args[0][1]("pasted")

        app._pump_segment_queue()
        app._on_final_result("B句原文")
        h["recorder"].is_recording = False
        app._on_recording_finished(np.zeros(1, dtype=np.float32))
        assert polisher.polish.call_args[0][0] == "B句原文"
        app._on_polish_complete("B句润色")
        paster.paste_async.call_args[0][1]("pasted")

        assert [c.args[0] for c in paster.paste_async.call_args_list] == ["A句原文", "B句润色"]
        records = _enqueued_records(h["history"])
        assert [(r.raw_text, r.polished_text) for r in records] == [
            ("A句原文", "A句原文"),
            ("B句原文", "B句润色"),
        ]


def test_hold_error_after_release_still_pastes_and_records_earlier_text() -> None:
    with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
        app = h["app"]
        h["recorder"].is_recording = True
        app._begin_transcription(_audio(0.2))
        app._on_final_result("已经说了")

        h["recorder"].is_recording = False
        app._begin_transcription(_audio(0.2))
        app._on_recognizer_error("ASR crashed")
        h["paster"].paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        assert h["paster"].paste_async.call_args[0][0] == "已经说了"
        assert [record.raw_text for record in records] == ["已经说了"]
        assert records[0].duration_ms == 400
        h["floating"].show_error.assert_not_called()


def test_hold_cancel_does_not_paste_or_record() -> None:
    with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
        app = h["app"]
        h["recorder"].is_recording = True
        app._begin_transcription(_audio(0.2))
        app._on_final_result("不要了")
        app._on_recording_cancel()
        app._on_final_result("迟到的结果")

        h["paster"].paste_async.assert_not_called()
        h["history"].enqueue.assert_not_called()


_POLISH_ON = {
    "audio.trigger_mode": "continuous",
    "llm.enabled": True,
    "llm.api_url": "https://api.example.test/v1",
    "llm.api_key": "key",
    "llm.model_name": "gpt-test",
}


def test_continuous_segment_waits_while_previous_segment_is_polishing() -> None:
    with app_harness(_POLISH_ON) as h:
        app = h["app"]
        _start_continuous_user_session(app)
        h["recorder"].is_continuous = True
        h["recorder"].consume_segment_route.return_value = ""

        app._on_segment_ready(_audio())
        app._on_final_result("AAA")
        app._on_segment_ready(_audio())

        assert h["recognizer"].transcribe_final.call_count == 1
        assert len(app._segment_queue) == 1
        assert app._pending_segment_count() == 2
        h["polisher"].polish.assert_called_once()

        app._on_polish_complete("polished AAA")
        h["paster"].paste_async.call_args[0][1]("pasted")
        app._pump_segment_queue()
        assert h["recognizer"].transcribe_final.call_count == 2
        app._on_final_result("BBB")
        app._on_polish_complete("polished BBB")
        h["paster"].paste_async.call_args[0][1]("pasted")

        pasted = [call.args[0] for call in h["paster"].paste_async.call_args_list]
        assert pasted == ["polished AAA", "polished BBB"]
        records = _enqueued_records(h["history"])
        assert [(r.seq, r.raw_text, r.polished_text) for r in records] == [
            (0, "AAA", "polished AAA"),
            (1, "BBB", "polished BBB"),
        ]


def test_stuck_polish_falls_back_to_raw_text() -> None:
    """README: a polisher that never answers must not swallow the sentence."""
    with app_harness(_POLISH_ON) as h:
        app, paster = h["app"], h["paster"]
        app._begin_transcription(_audio())
        app._on_final_result("AAA")
        app._enqueue_audio(_audio())
        assert app._output_busy is True
        assert not paster.paste_async.called

        app._release_stuck_output(app._output_token)

        # The raw words went to the paster as a degraded output ...
        assert paster.paste_async.call_args[0][0] == "AAA"
        assert app._output_busy is True
        paster.paste_async.call_args[0][1]("pasted")
        # ... and the queue moves on once that paste reports back.
        assert app._output_busy is False
        app._pump_segment_queue()
        assert h["recognizer"].transcribe_final.call_count == 2
        records = _enqueued_records(h["history"])
        assert [(r.raw_text, r.polished_text) for r in records] == [("AAA", "AAA")]


def test_polish_answering_after_the_timeout_is_not_pasted_again() -> None:
    with app_harness(_POLISH_ON) as h:
        app, paster = h["app"], h["paster"]
        app._begin_transcription(_audio())
        app._on_final_result("AAA")
        app._release_stuck_output(app._output_token)
        assert h["polisher"].cancel.called
        paster.paste_async.call_args[0][1]("pasted")

        app._on_polish_complete("AAA。")
        app._on_polish_error("超时")
        assert paster.paste_async.call_count == 1


def test_stuck_paste_releases_queue_and_keeps_words_on_clipboard() -> None:
    with app_harness(_POLISH_ON) as h, patch("sayink.app.pyperclip.copy") as copy:
        app, paster = h["app"], h["paster"]
        app._begin_transcription(_audio())
        app._on_final_result("AAA")
        app._on_polish_complete("AAA。")
        app._enqueue_audio(_audio())
        assert paster.paste_async.call_args[0][0] == "AAA。"

        app._release_stuck_output(app._output_token)

        assert app._output_busy is False
        copy.assert_called_once_with("AAA。")
        # No second paste attempt: Ctrl+V may already have landed.
        # A shortcut still waiting out its delay must not fire after this.
        paster.cancel.assert_called_once()
        assert paster.paste_async.call_count == 1
        assert h["recognizer"].transcribe_final.call_count == 2


def test_stuck_paste_still_saves_the_sentence_to_history() -> None:
    """The paster's callback normally writes the row; when the watchdog has to
    cancel the paster, the words must not vanish from history as well."""
    with app_harness(_POLISH_ON) as h, patch("sayink.app.pyperclip.copy"):
        app = h["app"]
        app._begin_transcription(_audio())
        app._on_final_result("AAA")
        app._on_polish_complete("AAA。")
        assert _enqueued_records(h["history"]) == []

        app._release_stuck_output(app._output_token)

        records = _enqueued_records(h["history"])
        assert [(r.raw_text, r.polished_text) for r in records] == [("AAA", "AAA。")]
        assert app._output_stage_record is None


def test_polish_reply_that_normalises_to_nothing_outputs_raw() -> None:
    """「。」 passes the fidelity checks but is empty once normalised; the user's
    sentence must still be pasted, and the output slot must be released."""
    with app_harness(_POLISH_ON) as h:
        app, paster = h["app"], h["paster"]
        app._begin_transcription(_audio())
        app._on_final_result("我们走吧")
        app._on_polish_complete("。")

        assert paster.paste_async.call_args[0][0] == "我们走吧"
        paster.paste_async.call_args[0][1]("pasted")
        record = _enqueued_records(h["history"])[0]
        assert (record.raw_text, record.polished_text) == ("我们走吧", "我们走吧")
        assert app._output_busy is False
        assert app._output_stage is OutputStage.IDLE


def test_empty_output_releases_stage_and_pending_record() -> None:
    with app_harness({"llm.enabled": False}) as h:
        app = h["app"]
        app._begin_transcription(_audio())
        app._output_stage = OutputStage.PASTE
        app._output_text("   ")

        assert app._output_busy is False
        assert app._output_stage is OutputStage.IDLE
        assert app._pending_record is None
        assert not h["paster"].paste_async.called
        assert h["floating"].show_error.called


def test_stale_watchdog_does_not_release_a_newer_output() -> None:
    with app_harness(_POLISH_ON) as h:
        app = h["app"]
        app._mark_output_busy()
        stale = app._output_token
        app._mark_output_busy()

        app._release_stuck_output(stale)

        assert app._output_busy is True


def test_polish_reply_far_longer_than_speech_falls_back_to_raw() -> None:
    with app_harness(_POLISH_ON) as h:
        app = h["app"]
        app._begin_transcription(_audio())
        app._on_final_result("今天开会")
        app._on_polish_complete("好的！以下是关于开会的建议：" + "内容" * 60)
        h["paster"].paste_async.call_args[0][1]("pasted")

        assert h["paster"].paste_async.call_args[0][0] == "今天开会"
        record = _enqueued_records(h["history"])[0]
        assert record.polished_text == "今天开会"


def test_local_polish_service_without_key_is_used() -> None:
    overrides = {
        "llm.enabled": True,
        "llm.api_url": "http://localhost:11434/v1",
        "llm.api_key": "",
        "llm.model_name": "local-model",
    }
    with app_harness(overrides) as h:
        h["app"]._deliver_recognized_text("本地润色")
        h["polisher"].polish.assert_called_once()
        assert h["polisher"].polish.call_args[0][2] == ""
        h["paster"].paste_async.assert_not_called()


def test_remote_polish_service_without_key_outputs_raw_text() -> None:
    overrides = {
        "llm.enabled": True,
        "llm.api_url": "https://api.example.test/v1",
        "llm.api_key": "",
        "llm.model_name": "gpt-test",
    }
    with app_harness(overrides) as h:
        h["app"]._deliver_recognized_text("远程未配置")
        h["polisher"].polish.assert_not_called()
        assert h["paster"].paste_async.call_args[0][0] == "远程未配置"


def test_clipboard_and_error_paste_results_enqueue_history() -> None:
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        _drive_segment(h["app"], h["paster"], "copied text", result="clipboard")
        _drive_segment(h["app"], h["paster"], "failed text", result="error:target locked")

        records = _enqueued_records(h["history"])
        assert [record.raw_text for record in records] == ["copied text", "failed text"]


def test_history_only_output_records_without_pasting() -> None:
    """P-01: with 「只记录到历史」 a meeting is never typed into the front window."""
    with app_harness({"audio.trigger_mode": "continuous", "output.mode": "history"}) as h:
        app = h["app"]
        _start_continuous_user_session(app)
        h["recorder"].is_continuous = True

        app._begin_transcription(_audio())
        app._on_final_result("远端在说话")

        h["paster"].paste_async.assert_not_called()
        records = _enqueued_records(h["history"])
        assert [r.raw_text for r in records] == ["远端在说话"]
        assert records[0].target_app == ""
        assert h["floating"].show_success.call_args[0][0] == "已记录"
        assert app._output_busy is False


def test_history_only_output_warns_when_history_is_disabled() -> None:
    with app_harness({"audio.trigger_mode": "hotkey", "output.mode": "history", "history.enabled": False}) as h:
        app = h["app"]
        app._begin_transcription(_audio())
        app._on_final_result("没人保存我")

        h["paster"].paste_async.assert_not_called()
        h["history"].enqueue.assert_not_called()
        title, hint = h["floating"].show_info.call_args[0][:2]
        assert "历史已关闭" in title
        assert "改回粘贴" in hint


def test_default_output_mode_still_pastes() -> None:
    with app_harness({"audio.trigger_mode": "hotkey"}) as h:
        app = h["app"]
        app._begin_transcription(_audio())
        app._on_final_result("口述一句")
        h["paster"].paste_async.assert_called_once()


def test_esc_does_not_end_continuous_session_when_disabled() -> None:
    with app_harness({"audio.trigger_mode": "continuous", "audio.esc_stops_continuous": False}) as h:
        app = h["app"]
        h["recorder"].is_continuous = True
        app._on_esc_pressed()
        h["recorder"].stop_continuous.assert_not_called()


def test_esc_ends_continuous_session_by_default() -> None:
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        h["recorder"].is_continuous = True
        app._on_esc_pressed()
        h["recorder"].stop_continuous.assert_called_once()


def test_model_load_hint_uses_last_measured_duration() -> None:
    with app_harness() as h:
        app = h["app"]
        h["recognizer"].current_model_id = "sensevoice"
        h["recorder"].is_continuous = False

        app._on_model_load_progress(ModelLoadPhase.LOADING, "正在加载 SenseVoice…")
        assert "约 10–40 秒" in h["floating"].show_model_loading.call_args[0][0]
        app._load_started_at -= 12.0
        app._on_model_load_progress(ModelLoadPhase.READY, "模型已就绪")
        assert round(h["store"]["stt_load_seconds"]["sensevoice"]) == 12

        app._on_model_load_progress(ModelLoadPhase.LOADING, "正在加载 SenseVoice…")
        assert "上次用时约 12 秒" in h["floating"].show_model_loading.call_args[0][0]


def test_history_conflict_cancel_does_not_start_or_change_settings(monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    shown = {}

    def exec_(box):
        shown["default"] = box.defaultButton().text()
        cancel = next(button for button in box.buttons() if button.text() == "取消")
        shown["cancel_name"] = cancel.accessibleName()
        shown["labels"] = [button.text() for button in box.buttons()]

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    with app_harness({"audio.trigger_mode": "hotkey", "output.mode": "history", "history.enabled": False}) as h:
        h["app"]._on_recording_start()
        assert h["floating"].call_when_painted.call_count == 0
        assert h["app"]._config.get("history.enabled") is False
        assert h["app"]._config.history_only_output()
        assert shown["default"] == "开启历史"
        assert shown["cancel_name"] == "取消"
        assert set(shown["labels"]) == {"开启历史", "改为输入光标", "取消"}


def test_enabling_history_lets_the_hold_start(monkeypatch):
    with app_harness({"audio.trigger_mode": "hotkey", "output.mode": "history", "history.enabled": False}) as h:
        monkeypatch.setattr(h["app"], "_ask_history_output_conflict", lambda: "history", raising=False)
        h["app"]._on_recording_start()
        assert h["app"]._config.get("history.enabled") is True
        assert h["app"]._config.history_only_output()
        assert h["floating"].call_when_painted.call_count == 1
        slots = h["app"]._config.get("output.by_mode") or {}
        assert "continuous" not in slots


def test_paste_choice_changes_only_the_active_mode_and_then_starts(monkeypatch):
    with app_harness({"audio.trigger_mode": "hotkey", "output.mode": "history", "history.enabled": False}) as h:
        monkeypatch.setattr(h["app"], "_ask_history_output_conflict", lambda: "paste", raising=False)
        h["app"]._on_recording_start()
        assert h["app"]._config.output_mode() == "paste"
        assert h["app"]._config.get("history.enabled") is False
        assert h["floating"].call_when_painted.call_count == 1
        slots = h["app"]._config.get("output.by_mode") or {}
        assert "continuous" not in slots


def test_continuous_conflict_cancel_does_not_open_the_microphone(monkeypatch):
    from PyQt6.QtCore import QTimer

    scheduled = []
    monkeypatch.setattr(QTimer, "singleShot", lambda _ms, fn: scheduled.append(fn))
    with app_harness({"audio.trigger_mode": "continuous", "output.mode": "history", "history.enabled": False}) as h:
        monkeypatch.setattr(h["app"], "_ask_history_output_conflict", lambda: "cancel", raising=False)
        h["app"]._start_continuous_listening()
        assert scheduled == []
        h["recorder"].start_continuous.assert_not_called()


def test_meeting_start_does_not_ask_about_the_dictation_history_conflict(monkeypatch):
    from sayink.meeting_session import MeetingOptions

    with app_harness({"output.mode": "history", "history.enabled": False}) as h:
        asked = []
        monkeypatch.setattr(
            h["app"], "_ask_history_output_conflict", lambda: asked.append("ask") or "cancel", raising=False,
        )
        monkeypatch.setattr(h["app"], "_meeting_refusal", lambda _options: "测试中不真正开会")
        started = h["app"].start_meeting(MeetingOptions())
        assert started is False
        assert asked == []
