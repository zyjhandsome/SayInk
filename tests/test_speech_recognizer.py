import pytest
import numpy as np
from sayink.speech_recognizer import (
    MODEL_REGISTRY, normalize_asr_output, get_model_info,
    is_model_downloaded, get_model_dir, _get_models_dir,
    SAMPLE_RATE, DEFAULT_MODEL_ID, restore_glossary_case,
)


class TestSampleRate:
    def test_sample_rate_constant(self):
        assert SAMPLE_RATE == 16000


class TestDefaultModel:
    def test_default_model_is_funasr_nano(self):
        assert DEFAULT_MODEL_ID == "funasr-nano"
        info = get_model_info(DEFAULT_MODEL_ID)
        assert info is not None
        assert info["name"] == "Fun-ASR-Nano"


class TestNormalizeAsrOutput:
    def test_empty_string(self):
        result = normalize_asr_output("")
        assert result == ""

    def test_none_input(self):
        result = normalize_asr_output(None)
        assert result == ""

    def test_no_tags(self):
        text = "这是一段正常的语音识别结果"
        result = normalize_asr_output(text)
        assert result == text

    def test_removes_opening_tag(self):
        text = "<asr_text>识别结果</asr_text>"
        result = normalize_asr_output(text)
        assert "<asr_text>" not in result
        assert "识别结果" in result

    def test_removes_closing_tag(self):
        text = "开头<asr_text>中间</asr_text>结尾"
        result = normalize_asr_output(text)
        assert "<asr_text>" not in result
        assert "</asr_text>" not in result
        assert "开头" in result
        assert "中间" in result
        assert "结尾" in result

    def test_case_insensitive(self):
        text = "<ASR_TEXT>测试</ASR_TEXT>"
        result = normalize_asr_output(text)
        assert result == "测试"

    def test_strips_whitespace(self):
        text = "  <asr_text>内容</asr_text>  "
        result = normalize_asr_output(text)
        assert result == "内容"

    def test_opening_tag_only_before_text(self):
        """Qwen3 may emit ``<asr_text>嗯`` without a closing tag."""
        text = "<asr_text>嗯"
        result = normalize_asr_output(text)
        assert result == "嗯"
        assert "asr_text" not in result

    @pytest.mark.parametrize("text, expected", [
        ("language Chinese<asr_text>现在这个。", "现在这个。"),
        ("现在这个。language Chinese如果大家一起讲话。", "现在这个。如果大家一起讲话。"),
        ("现在这个。language Chinese<asr_text>如果大家一起讲话", "现在这个。如果大家一起讲话"),
        ("language English<asr_text>hello world", "hello world"),
        ("language None<asr_text>", ""),
    ])
    def test_removes_qwen3_language_prefix(self, text, expected):
        assert normalize_asr_output(text) == expected

    @pytest.mark.parametrize("text", [
        "The language English is widely spoken",
        "我们讨论 language Chinese learning 的方法",
    ])
    def test_keeps_spoken_language_phrase(self, text):
        assert normalize_asr_output(text) == text

    def test_removes_fireredasr_sil_tokens(self):
        text = "给他购买还出现了个什么标识呢<sil>个没有点伟大<sil><sil><sil>"
        result = normalize_asr_output(text)
        assert "<sil>" not in result
        assert result == "给他购买还出现了个什么标识呢个没有点伟大"

    @pytest.mark.parametrize("marker", ["/sil", "/SIL", "/sil>", "/sil >"])
    def test_removes_sil_fragment_from_screenshot(self, marker):
        spoken = "时间很紧。这次的效果会不会好一点？"
        assert normalize_asr_output(marker + spoken) == spoken

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/sil 你好 /sil", "你好"),
            ("你好/sil，世界/sil", "你好，世界"),
            ("<zh>/sil你好</sil>/sil>", "你好"),
            ("/sil /SIL> <sil>", ""),
        ],
    )
    def test_removes_sil_fragments_with_other_meta_tokens(self, text, expected):
        assert normalize_asr_output(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "使用 /silver 命令",
            "/silicon 是一个单词",
            "/sil2 和 /sil_name",
            "/sil.txt",
            "/sil-backup",
            "/sil/recording.wav",
            "C:/sil",
            r"C:\sil",
            "https://example.com/sil",
            "https://sil.example.com",
            "https://sil",
            "目录 ./sil",
            "路径 foo/sil",
            "and/or 和 1/2",
            "sil 是 silence 的缩写",
        ],
    )
    def test_keeps_words_paths_and_urls_with_sil(self, text):
        assert normalize_asr_output(text) == text

    def test_removes_fireredasr_lang_tags(self):
        text = "<zh>你好<en>hello"
        result = normalize_asr_output(text)
        assert "<" not in result
        assert result == "你好hello"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("<|zh|>你好<|en|>hello", "你好hello"),
            ("< sil >你好</ sil>", "你好"),
            ("<zh-CN>你好<en_US>hello", "你好hello"),
        ],
    )
    def test_removes_meta_tokens_with_pipes_spaces_and_dialect_codes(self, text, expected):
        assert normalize_asr_output(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "如果 a<b 并且 c>d 就成立",
            "x < 3 和 y > 5",
            "数组 <长度> 是十",
            "他说 <我 不同意> 这句话",
        ],
    )
    def test_keeps_comparisons_and_spoken_angle_brackets(self, text):
        assert normalize_asr_output(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            "请把 <div> 标签改成 <span>",
            "用 <b> 加粗，<br> 换行",
            "泛型写成 List<T>",
        ],
    )
    def test_keeps_spoken_html_and_generic_tags(self, text):
        """README: only known ASR markers and language codes are stripped."""
        assert normalize_asr_output(text) == text

    def test_keeps_colloquial_repeats_and_fillers(self):
        spoken = "说前面啊往前挪啊。往前挪啊。没问题没问题。嗯。嗯。嗯。嗯。嗯。"
        assert normalize_asr_output(spoken) == spoken


    @pytest.mark.parametrize(
        "text",
        [
            "预算是100000000元。",
            "电话 8888 8888 8888 8888",
            "他走了，他走了，他走了。",
            "谢谢谢谢，太好了。",
        ],
    )
    def test_numbers_and_short_repeats_are_untouched(self, text):
        assert normalize_asr_output(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            "こんにちは、今日はいい天気ですね。",
            "明日の会議は三時からです。",
            "안녕하세요 만나서 반갑습니다.",
        ],
    )
    def test_keeps_an_utterance_spoken_entirely_in_japanese_or_korean(self, text):
        """README lists 中/英/日: a Japanese sentence on its own is speech, not drift."""
        assert normalize_asr_output(text) == text

    def test_keeps_chinese_english_mix(self):
        text = "这个 API 怎么调用"
        assert normalize_asr_output(text) == text


class TestRestoreGlossaryCase:
    """FireRedASR2 emits English in one case (``GITHUB``); the glossary says how it is spelled."""

    def test_uppercase_model_output_takes_the_glossary_spelling(self):
        glossary = [{"term": "GitHub", "variants": []}, {"term": "SayInk"}]
        assert restore_glossary_case("代码在GITHUB上，用sayink转写", glossary) == "代码在GitHub上，用SayInk转写"

    def test_plain_string_entries_count_as_terms(self):
        assert restore_glossary_case("打开 VSCODE", ["VSCode"]) == "打开 VSCode"

    def test_only_whole_words_are_rewritten(self):
        assert restore_glossary_case("OPENAI 和 AI", ["AI"]) == "OPENAI 和 AI"
        assert restore_glossary_case("goals 和 GO", ["Go"]) == "goals 和 Go"

    def test_longer_term_wins_over_a_term_inside_it(self):
        glossary = ["code", "Visual Studio Code"]
        assert restore_glossary_case("VISUAL STUDIO CODE 写 CODE", glossary) == "Visual Studio Code 写 code"

    def test_chinese_terms_and_empty_glossary_leave_text_alone(self):
        assert restore_glossary_case("张新说GITHUB", [{"term": "张鑫"}]) == "张新说GITHUB"
        assert restore_glossary_case("GITHUB", []) == "GITHUB"
        assert restore_glossary_case("GITHUB", None) == "GITHUB"
        assert restore_glossary_case("", ["GitHub"]) == ""

    def test_regex_characters_in_terms_are_literal(self):
        assert restore_glossary_case("学 c++ 和 .net", ["C++", ".NET"]) == "学 C++ 和 .NET"


class TestModelRegistry:
    def test_registry_not_empty(self):
        assert len(MODEL_REGISTRY) >= 7

    def test_all_models_have_required_fields(self):
        required_fields = ["id", "name", "description", "accuracy", "speed", "languages", "size_mb", "loader", "hf_repo", "dir_name", "files"]
        for model in MODEL_REGISTRY:
            for field in required_fields:
                assert field in model, f"Model {model.get('id', 'unknown')} missing field {field}"

    def test_all_models_have_valid_accuracy(self):
        for model in MODEL_REGISTRY:
            accuracy = model["accuracy"]
            assert 1 <= accuracy <= 5

    def test_all_models_have_valid_speed(self):
        for model in MODEL_REGISTRY:
            speed = model["speed"]
            assert 1 <= speed <= 5

    def test_all_models_have_files_list(self):
        for model in MODEL_REGISTRY:
            files = model["files"]
            assert isinstance(files, list)
            assert len(files) > 0


class TestModelRegistryContent:
    def test_sensevoice_model(self):
        model = get_model_info("sensevoice")
        assert model is not None
        assert model["id"] == "sensevoice"
        assert model["loader"] == "sense_voice"

    def test_paraformer_zh_model(self):
        model = get_model_info("paraformer-zh")
        assert model is not None
        assert model["id"] == "paraformer-zh"
        assert model["loader"] == "paraformer"

    def test_fireredasr2_ctc_model(self):
        model = get_model_info("fireredasr2-ctc")
        assert model is not None
        assert model["id"] == "fireredasr2-ctc"
        assert model["loader"] == "fire_red_asr_ctc"

    def test_qwen3_asr_model(self):
        model = get_model_info("qwen3-asr-0.6b")
        assert model is not None
        assert model["id"] == "qwen3-asr-0.6b"
        assert model["loader"] == "qwen3_asr"

    def test_qwen3_asr_1_7b_model(self):
        model = get_model_info("qwen3-asr-1.7b")
        assert model is not None
        assert model["id"] == "qwen3-asr-1.7b"
        assert model["loader"] == "qwen3_asr"
        assert model["size_mb"] == 2400

    def test_funasr_nano_model(self):
        model = get_model_info("funasr-nano")
        assert model is not None
        assert model["id"] == "funasr-nano"
        assert model["loader"] == "funasr_nano"
        assert model["hf_repo"] == "csukuangfj/sherpa-onnx-funasr-nano-int8-2025-12-30"
        assert model["dir_name"] == "sherpa-onnx-funasr-nano-int8-2025-12-30"
        assert "encoder_adaptor.int8.onnx" in model["files"]
        assert "Qwen3-0.6B/vocab.json" in model["files"]
        assert DEFAULT_MODEL_ID == "funasr-nano"

    def test_catalog_lists_default_first_and_reserves_top_accuracy(self):
        order = [
            "funasr-nano",
            "sensevoice",
            "qwen3-asr-0.6b",
            "fireredasr2-ctc",
            "fireredasr2-aed",
            "paraformer-zh",
            "paraformer-trilingual",
            "zipformer-ctc-zh",
            "qwen3-asr-1.7b",
        ]
        assert [model["id"] for model in MODEL_REGISTRY] == order
        by_id = {model["id"]: model for model in MODEL_REGISTRY}
        assert by_id["funasr-nano"]["description"].startswith("默认")
        assert "热词" in by_id["funasr-nano"]["description"]
        assert "识别语言" in by_id["funasr-nano"]["description"]
        assert by_id["sensevoice"]["description"].startswith("更快")
        assert "热词" in by_id["qwen3-asr-0.6b"]["description"]
        assert "CTC" in by_id["fireredasr2-ctc"]["description"]
        assert "较慢" in by_id["fireredasr2-aed"]["description"]
        assert "中英" in by_id["paraformer-zh"]["description"]
        assert "粤语" in by_id["paraformer-trilingual"]["description"]
        assert "纯中文" in by_id["zipformer-ctc-zh"]["description"]
        assert "第三方" in by_id["qwen3-asr-1.7b"]["description"]
        assert [model["id"] for model in MODEL_REGISTRY if model["accuracy"] >= 5] == ["funasr-nano"]
        for model in MODEL_REGISTRY:
            assert "最高" not in model["description"]

    def test_unknown_model(self):
        model = get_model_info("nonexistent_model")
        assert model is None


class TestGetModelInfo:
    def test_returns_model_dict(self):
        model = get_model_info("sensevoice")
        assert isinstance(model, dict)

    def test_model_has_files(self):
        model = get_model_info("sensevoice")
        assert "files" in model
        assert isinstance(model["files"], list)


class TestIsModelDownloaded:
    def test_unknown_model_not_downloaded(self):
        result = is_model_downloaded("nonexistent_model_12345")
        assert result is False

    def test_valid_model_check(self):
        result = is_model_downloaded("sensevoice")
        assert isinstance(result, bool)

    def _model_dir(self, tmp_path, monkeypatch):
        import sayink.speech_recognizer as sr

        info = sr.get_model_info("sensevoice")
        model_dir = tmp_path / info["dir_name"]
        model_dir.mkdir()
        monkeypatch.setattr(sr, "_get_models_dir", lambda: tmp_path)
        monkeypatch.setattr(sr, "_get_portable_model_dir", lambda _mid: None)
        return model_dir, info["files"]

    def test_real_files_count_as_downloaded(self, tmp_path, monkeypatch):
        model_dir, files = self._model_dir(tmp_path, monkeypatch)
        for name in files:
            (model_dir / name).write_bytes(b"\x00" * 4096)
        assert is_model_downloaded("sensevoice") is True

    def test_git_lfs_pointer_files_do_not_count_as_downloaded(self, tmp_path, monkeypatch):
        """README: a checkout without `git lfs pull` is not a downloaded model."""
        model_dir, files = self._model_dir(tmp_path, monkeypatch)
        pointer = (
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
            b"size 950000000\n"
        )
        for name in files:
            (model_dir / name).write_bytes(pointer)
        assert is_model_downloaded("sensevoice") is False

    def test_empty_files_do_not_count_as_downloaded(self, tmp_path, monkeypatch):
        model_dir, files = self._model_dir(tmp_path, monkeypatch)
        for name in files:
            (model_dir / name).write_bytes(b"")
        assert is_model_downloaded("sensevoice") is False


class TestGetModelsDir:
    def test_returns_path(self):
        result = _get_models_dir()
        from pathlib import Path
        assert isinstance(result, Path)

    def test_frozen_read_only_install_dir_falls_back_to_user_dir(self, tmp_path, monkeypatch):
        import sys
        import sayink.speech_recognizer as sr

        install = tmp_path / "Program Files" / "SayInk"
        (install / "models").mkdir(parents=True)
        monkeypatch.setattr(sys, "_MEIPASS", str(install / "_internal"), raising=False)
        monkeypatch.setattr(sr, "is_dir_writable", lambda _path: False)
        assert sr.default_models_dir() == sr.user_models_dir()

    def test_frozen_writable_install_dir_is_used(self, tmp_path, monkeypatch):
        import sys
        import sayink.speech_recognizer as sr

        install = tmp_path / "SayInk"
        monkeypatch.setattr(sys, "_MEIPASS", str(install / "_internal"), raising=False)
        assert sr.default_models_dir() == install / "models"

    def test_is_dir_writable_true_for_temp_dir(self, tmp_path):
        from sayink.speech_recognizer import is_dir_writable
        assert is_dir_writable(tmp_path) is True


class TestDeleteModelResult:
    def test_reports_failure_when_directory_survives(self, tmp_path, monkeypatch):
        import sayink.speech_recognizer as sr

        info = sr.get_model_info("sensevoice")
        model_dir = tmp_path / info["dir_name"]
        model_dir.mkdir()
        monkeypatch.setattr(sr, "_get_models_dir", lambda: tmp_path)
        monkeypatch.setattr(sr, "_get_portable_model_dir", lambda _mid: None)
        monkeypatch.setattr(sr.shutil, "rmtree", lambda *_a, **_k: None)
        assert sr.delete_model("sensevoice") is False

    def test_reports_success_when_directory_removed(self, tmp_path, monkeypatch):
        import sayink.speech_recognizer as sr

        info = sr.get_model_info("sensevoice")
        (tmp_path / info["dir_name"]).mkdir()
        monkeypatch.setattr(sr, "_get_models_dir", lambda: tmp_path)
        monkeypatch.setattr(sr, "_get_portable_model_dir", lambda _mid: None)
        assert sr.delete_model("sensevoice") is True


class TestGetModelDir:
    def test_unknown_model_raises(self):
        with pytest.raises(ValueError):
            get_model_dir("nonexistent_model_xyz")

    def test_valid_model_returns_path(self):
        result = get_model_dir("sensevoice")
        from pathlib import Path
        assert isinstance(result, Path)


class TestModelAccuracySpeed:
    def test_only_the_default_model_is_rated_top_accuracy(self):
        from sayink.speech_recognizer import DEFAULT_MODEL_ID, MODEL_REGISTRY

        default = get_model_info(DEFAULT_MODEL_ID)
        assert default["accuracy"] == 5
        others = [model for model in MODEL_REGISTRY if model["id"] != DEFAULT_MODEL_ID]
        assert others
        assert all(model["accuracy"] < default["accuracy"] for model in others)

    def test_qwen3_1_7b_slowest_speed(self):
        model = get_model_info("qwen3-asr-1.7b")
        assert model["accuracy"] == 4
        assert model["speed"] == 1

    def test_sensevoice_fast_speed(self):
        model = get_model_info("sensevoice")
        assert model["speed"] == 5

    def test_fireredasr2_aed_is_a_slower_candidate(self):
        aed = get_model_info("fireredasr2-aed")
        fast = get_model_info("sensevoice")
        assert aed["accuracy"] == 4
        assert aed["speed"] == 2
        assert aed["speed"] < fast["speed"]


class TestModelLanguages:
    def test_sensevoice_multilingual(self):
        model = get_model_info("sensevoice")
        assert "中" in model["languages"]
        assert "英" in model["languages"]

    def test_zipformer_chinese_only(self):
        model = get_model_info("zipformer-ctc-zh")
        assert model["languages"] == "中"

    def test_funasr_nano_languages(self):
        model = get_model_info("funasr-nano")
        assert "中" in model["languages"]
        assert "英" in model["languages"]
        assert "日" in model["languages"]


class TestResolveStartupModelId:
    def test_uses_configured_when_downloaded(self, monkeypatch):
        from sayink.speech_recognizer import resolve_startup_model_id

        monkeypatch.setattr(
            "sayink.speech_recognizer.is_model_downloaded",
            lambda mid: mid == "sensevoice",
        )
        monkeypatch.setattr(
            "sayink.speech_recognizer.get_downloaded_models",
            lambda: ["sensevoice"],
        )
        assert resolve_startup_model_id("sensevoice") == "sensevoice"

    def test_prefers_default_when_configured_missing(self, monkeypatch):
        from sayink.speech_recognizer import (
            DEFAULT_MODEL_ID,
            resolve_startup_model_id,
        )

        monkeypatch.setattr(
            "sayink.speech_recognizer.is_model_downloaded",
            lambda mid: mid == DEFAULT_MODEL_ID,
        )
        monkeypatch.setattr(
            "sayink.speech_recognizer.get_downloaded_models",
            lambda: [DEFAULT_MODEL_ID],
        )
        assert resolve_startup_model_id("qwen3-asr-0.6b") == DEFAULT_MODEL_ID

    def test_falls_back_to_any_downloaded(self, monkeypatch):
        from sayink.speech_recognizer import resolve_startup_model_id

        monkeypatch.setattr(
            "sayink.speech_recognizer.is_model_downloaded",
            lambda mid: mid == "qwen3-asr-0.6b",
        )
        monkeypatch.setattr(
            "sayink.speech_recognizer.get_downloaded_models",
            lambda: ["qwen3-asr-0.6b"],
        )
        assert resolve_startup_model_id("fireredasr2-ctc") == "qwen3-asr-0.6b"

    def test_returns_configured_when_nothing_downloaded(self, monkeypatch):
        from sayink.speech_recognizer import resolve_startup_model_id

        monkeypatch.setattr(
            "sayink.speech_recognizer.is_model_downloaded", lambda mid: False
        )
        monkeypatch.setattr(
            "sayink.speech_recognizer.get_downloaded_models", list
        )
        assert resolve_startup_model_id("sensevoice") == "sensevoice"

    def test_blank_configured_falls_back_to_default(self, monkeypatch):
        from sayink.speech_recognizer import (
            DEFAULT_MODEL_ID,
            resolve_startup_model_id,
        )

        monkeypatch.setattr(
            "sayink.speech_recognizer.is_model_downloaded",
            lambda mid: mid == DEFAULT_MODEL_ID,
        )
        monkeypatch.setattr(
            "sayink.speech_recognizer.get_downloaded_models",
            lambda: [DEFAULT_MODEL_ID],
        )
        assert resolve_startup_model_id("") == DEFAULT_MODEL_ID


class TestModelsDirGlobal:
    def test_set_models_dir_used_by_get_model_dir(self, tmp_path, monkeypatch):
        from sayink import speech_recognizer as sr

        monkeypatch.setattr(sr, "_get_portable_model_dir", lambda mid: None)
        try:
            sr.set_models_dir(tmp_path)
            d = sr.get_model_dir("fireredasr2-ctc")
            assert str(d).startswith(str(tmp_path))
        finally:
            sr.set_models_dir(None)

    def test_unusable_models_dir_falls_back_to_the_default_instead_of_crashing(self, tmp_path, monkeypatch):
        """A configured folder on a removed drive must not take startup down."""
        from sayink import speech_recognizer as sr

        monkeypatch.setattr(sr, "_get_portable_model_dir", lambda mid: None)
        blocker = tmp_path / "file-not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        try:
            assert sr.set_models_dir(blocker / "models") is False
            assert sr._get_models_dir() == sr.default_models_dir()
            assert sr.set_models_dir(tmp_path / "ok") is True
            assert sr._get_models_dir() == tmp_path / "ok"
        finally:
            sr.set_models_dir(None)

    def test_model_dir_names_cover_both_registries(self):
        from sayink.speech_recognizer import AUX_MODEL_REGISTRY, MODEL_REGISTRY, model_dir_names

        names = model_dir_names()
        assert len(names) == len(MODEL_REGISTRY) + len(AUX_MODEL_REGISTRY)
        assert "silero-vad" in names

    def test_get_model_dir_unknown_raises(self):
        from sayink.speech_recognizer import get_model_dir

        with pytest.raises(ValueError):
            get_model_dir("no-such-model")

    def test_delete_unknown_model_returns_false(self):
        from sayink.speech_recognizer import delete_model

        assert delete_model("no-such-model") is False

    def test_delete_missing_dir_returns_false(self, tmp_path, monkeypatch):
        from sayink import speech_recognizer as sr

        monkeypatch.setattr(sr, "_get_portable_model_dir", lambda mid: None)
        try:
            sr.set_models_dir(tmp_path)
            assert sr.delete_model("fireredasr2-ctc") is False
        finally:
            sr.set_models_dir(None)


class _FakeStream:
    def __init__(self, text):
        self.result = type("R", (), {"text": text})()

    def accept_waveform(self, sr, audio):
        self._audio = audio


class _FakeRecognizer:
    def __init__(self, text="识别文本"):
        self._text = text

    def create_stream(self):
        return _FakeStream(self._text)

    def decode_stream(self, stream):
        pass


class TestTranscribeWorkerRun:
    def _run(self, worker):

        results, errors = [], []
        worker.result_ready.connect(results.append)
        worker.error.connect(errors.append)
        worker.run()
        return results, errors

    def test_run_emits_normalized_result(self):
        from sayink.speech_recognizer import TranscribeWorker

        rec = _FakeRecognizer("<asr_text>你好</asr_text>")
        worker = TranscribeWorker(rec, np.ones(16000, dtype=np.float32))
        results, errors = self._run(worker)
        assert results == ["你好"]
        assert errors == []

    @pytest.mark.parametrize(
        ("raw_text", "expected"),
        [
            ("/sil时间很紧。这次的效果会不会好一点？", "时间很紧。这次的效果会不会好一点？"),
            ("/sil <sil> /SIL>", ""),
        ],
    )
    def test_run_cleans_sil_fragments_before_partial_and_final_signals(
        self, raw_text, expected
    ):
        from sayink.speech_recognizer import TranscribeWorker

        worker = TranscribeWorker(
            _FakeRecognizer(raw_text), np.ones(16000, dtype=np.float32)
        )
        partials = []
        worker.partial_ready.connect(partials.append)
        results, errors = self._run(worker)
        assert partials == ([expected] if expected else [])
        assert results == [expected]
        assert errors == []

    def test_run_empty_audio_emits_error(self):
        from sayink.speech_recognizer import TranscribeWorker

        worker = TranscribeWorker(_FakeRecognizer(), np.array([], dtype=np.float32))
        results, errors = self._run(worker)
        assert results == []
        assert errors and "音频数据无效" in errors[0]

    def test_run_nan_audio_emits_error(self):
        from sayink.speech_recognizer import TranscribeWorker

        bad = np.array([np.nan, np.nan], dtype=np.float32)
        worker = TranscribeWorker(_FakeRecognizer(), bad)
        results, errors = self._run(worker)
        assert errors and "音频数据无效" in errors[0]

    def test_run_cancelled_does_nothing(self):
        from sayink.speech_recognizer import TranscribeWorker

        worker = TranscribeWorker(_FakeRecognizer(), np.ones(16000, dtype=np.float32))
        worker.cancel()
        results, errors = self._run(worker)
        assert results == [] and errors == []

    def test_run_recognizer_exception_emits_error(self):
        from sayink.speech_recognizer import TranscribeWorker

        rec = _FakeRecognizer()
        rec.decode_stream = lambda s: (_ for _ in ()).throw(RuntimeError("boom"))
        worker = TranscribeWorker(rec, np.ones(16000, dtype=np.float32))
        results, errors = self._run(worker)
        assert results == []
        assert errors and "识别失败" in errors[0]


class _SliceRecognizer:
    """Records each decoded slice and labels it by sample count."""

    def __init__(self):
        self.seen: list[int] = []

    def create_stream(self):
        return _FakeStream("")

    def decode_stream(self, stream):
        n = int(len(stream._audio))
        self.seen.append(n)
        stream.result.text = f"[{n}]"


def _slice_start(audio: np.ndarray, piece: np.ndarray) -> int:
    start = int(piece[0])
    assert np.array_equal(audio[start : start + piece.size], piece)
    return start


class TestLongUtteranceSlicing:
    def test_over_one_minute_under_ninety_seconds_is_fully_covered(self):
        from sayink.speech_recognizer import SAMPLE_RATE, plan_audio_slices

        seconds = 70
        audio = np.arange(int(seconds * SAMPLE_RATE), dtype=np.float32)
        slices = plan_audio_slices(audio, SAMPLE_RATE, slice_sec=15.0, overlap_sec=0.4)
        assert len(slices) >= 5
        assert all(s.size <= int(15 * SAMPLE_RATE) for s in slices)
        starts = [_slice_start(audio, piece) for piece in slices]
        assert starts[0] == 0
        assert starts[-1] + slices[-1].size == audio.size
        for earlier, later in zip(starts, starts[1:], strict=False):
            assert later > earlier
            assert later < earlier + slices[0].size

    def test_short_audio_stays_one_slice(self):
        from sayink.speech_recognizer import SAMPLE_RATE, plan_audio_slices

        audio = np.ones(int(8 * SAMPLE_RATE), dtype=np.float32)
        slices = plan_audio_slices(audio, SAMPLE_RATE, slice_sec=15.0, overlap_sec=0.4)
        assert len(slices) == 1
        assert slices[0].size == audio.size

    def test_merge_drops_duplicated_boundary(self):
        from sayink.speech_recognizer import merge_slice_texts

        assert merge_slice_texts(["今天天气不错", "气不错我们出发"]) == "今天天气不错我们出发"
        assert merge_slice_texts(["你好", "世界"]) == "你好世界"

    def test_merge_tolerates_a_misheard_character_in_the_overlap(self):
        """README: the join between two slices must not repeat or garble words."""
        from sayink.speech_recognizer import merge_slice_texts

        assert merge_slice_texts(["我们大约有两百个", "大约有两白个自动化用例"]) == "我们大约有两百个自动化用例"
        # Short overlaps still have to match exactly.
        assert merge_slice_texts(["你好啊", "你坏啊今天"]) == "你好啊你坏啊今天"
        assert merge_slice_texts(["前十五秒", "后十五秒"]) == "前十五秒后十五秒"

    def test_separate_utterances_keep_repeated_words_and_spaces(self):
        """Regression: hold-to-talk segments went through the overlap merger,
        which deleted 「谢谢」 from 「谢谢」+「谢谢大家」 and glued English words."""
        from sayink.speech_recognizer import join_segment_texts

        assert join_segment_texts(["谢谢", "谢谢大家"]) == "谢谢谢谢大家"
        assert join_segment_texts(["我说好", "好的没问题"]) == "我说好好的没问题"
        assert join_segment_texts(["hello world", "how are you"]) == "hello world how are you"
        assert join_segment_texts(["用 Python", "写脚本", "", "OK"]) == "用 Python写脚本OK"

    def test_slice_boundaries_land_on_the_quietest_frame(self):
        from sayink.speech_recognizer import SAMPLE_RATE, plan_audio_slices

        audio = np.full(int(40 * SAMPLE_RATE), 0.5, dtype=np.float32)
        # A breath at 13.0–13.1 s and another at 26.5–26.6 s.
        for at in (13.0, 26.5):
            lo = int(at * SAMPLE_RATE)
            audio[lo:lo + int(0.1 * SAMPLE_RATE)] = 0.01
        slices = plan_audio_slices(audio, SAMPLE_RATE, slice_sec=15.0, overlap_sec=0.4)
        ends = []
        start = 0
        for piece in slices:
            ends.append(start + piece.size)
            start = start + piece.size - int(0.4 * SAMPLE_RATE)
        assert ends[0] == int(13.1 * SAMPLE_RATE)
        assert ends[1] == int(26.6 * SAMPLE_RATE)
        assert ends[-1] == audio.size
        assert all(s.size <= int(15 * SAMPLE_RATE) for s in slices)

    def test_context_limited_models_use_slice_window(self):
        from sayink.speech_recognizer import slice_window_for_loader

        assert slice_window_for_loader("funasr_nano") == (15.0, 0.4)
        assert slice_window_for_loader("qwen3_asr") == (15.0, 0.4)
        assert slice_window_for_loader("sense_voice") is None
        assert slice_window_for_loader("paraformer") is None

    def test_worker_decodes_every_slice_of_a_seventy_second_utterance(self):
        from sayink.speech_recognizer import SAMPLE_RATE, TranscribeWorker

        rec = _SliceRecognizer()
        audio = np.ones(int(70 * SAMPLE_RATE), dtype=np.float32)
        worker = TranscribeWorker(
            rec, audio, max_slice_sec=15.0, overlap_sec=0.4
        )
        results, errors = TestTranscribeWorkerRun()._run(worker)
        assert errors == []
        assert len(rec.seen) >= 5
        assert all(n <= int(15 * SAMPLE_RATE) for n in rec.seen)
        assert sum(rec.seen) > audio.size
        assert results and results[0].startswith(f"[{rec.seen[0]}]")
        assert results[0].endswith(f"[{rec.seen[-1]}]")

    def test_worker_emits_the_slice_text_as_soon_as_one_piece_is_decoded(self):
        from sayink.speech_recognizer import SAMPLE_RATE, TranscribeWorker

        rec = _FakeRecognizer("前十五秒")
        audio = np.ones(int(15 * SAMPLE_RATE), dtype=np.float32)
        worker = TranscribeWorker(rec, audio, max_slice_sec=15.0, overlap_sec=0.4)
        partials: list[str] = []
        worker.partial_ready.connect(partials.append)
        results, errors = TestTranscribeWorkerRun()._run(worker)
        assert errors == []
        assert partials == ["前十五秒"]
        assert results == ["前十五秒"]

    def test_worker_shows_growing_text_before_the_final_result(self):
        from sayink.speech_recognizer import SAMPLE_RATE, TranscribeWorker

        rec = _SliceRecognizer()
        audio = np.ones(int(70 * SAMPLE_RATE), dtype=np.float32)
        worker = TranscribeWorker(rec, audio, max_slice_sec=15.0, overlap_sec=0.4)
        partials: list[str] = []
        worker.partial_ready.connect(partials.append)
        results, errors = TestTranscribeWorkerRun()._run(worker)
        assert errors == []
        assert len(partials) >= 2
        assert len(partials[-1]) >= len(partials[0])
        assert partials[0].startswith(f"[{rec.seen[0]}]")
        assert results and results[0].endswith(f"[{rec.seen[-1]}]")


class TestCreateRecognizer:
    def _install_sherpa(self, monkeypatch, **methods):
        import sys
        import types

        fake = types.ModuleType("sherpa_onnx")

        class OfflineRecognizer:
            pass

        for name, fn in methods.items():
            setattr(OfflineRecognizer, name, classmethod(fn))
        fake.OfflineRecognizer = OfflineRecognizer
        monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
        return fake

    def test_funasr_nano_uses_official_factory(self, tmp_path, monkeypatch):
        from sayink import speech_recognizer as sr

        captured = {}

        def from_funasr_nano(cls, **kwargs):
            captured.update(kwargs)
            return object()

        self._install_sherpa(monkeypatch, from_funasr_nano=from_funasr_nano)
        monkeypatch.setattr(sr, "get_model_dir", lambda mid: tmp_path)
        sr._create_recognizer("funasr-nano", 4)
        assert captured["encoder_adaptor"] == str(tmp_path / "encoder_adaptor.int8.onnx")
        assert captured["llm"] == str(tmp_path / "llm.int8.onnx")
        assert captured["embedding"] == str(tmp_path / "embedding.int8.onnx")
        assert captured["tokenizer"] == str(tmp_path / "Qwen3-0.6B")
        assert captured["num_threads"] == 4

    def test_qwen3_uses_official_factory(self, tmp_path, monkeypatch):
        from sayink import speech_recognizer as sr

        captured = {}

        def from_qwen3_asr(cls, **kwargs):
            captured.update(kwargs)
            return object()

        self._install_sherpa(monkeypatch, from_qwen3_asr=from_qwen3_asr)
        monkeypatch.setattr(sr, "get_model_dir", lambda mid: tmp_path)
        sr._create_recognizer("qwen3-asr-0.6b", 2)
        assert captured["conv_frontend"] == str(tmp_path / "conv_frontend.onnx")
        assert captured["encoder"] == str(tmp_path / "encoder.int8.onnx")
        assert captured["decoder"] == str(tmp_path / "decoder.int8.onnx")
        assert captured["tokenizer"] == str(tmp_path / "tokenizer")
        assert captured["num_threads"] == 2

    def test_funasr_nano_receives_hotwords_and_language(self, tmp_path, monkeypatch):
        from sayink import speech_recognizer as sr

        captured = {}

        def from_funasr_nano(cls, **kwargs):
            captured.update(kwargs)
            return object()

        self._install_sherpa(monkeypatch, from_funasr_nano=from_funasr_nano)
        monkeypatch.setattr(sr, "get_model_dir", lambda mid: tmp_path)
        sr._create_recognizer("funasr-nano", 4, hotwords="SayInk,张鑫", language="中文")
        assert captured["hotwords"] == "SayInk,张鑫"
        assert captured["language"] == "中文"

    def test_sense_voice_receives_language_only(self, tmp_path, monkeypatch):
        from sayink import speech_recognizer as sr

        captured = {}

        def from_sense_voice(cls, **kwargs):
            captured.update(kwargs)
            return object()

        self._install_sherpa(monkeypatch, from_sense_voice=from_sense_voice)
        monkeypatch.setattr(sr, "get_model_dir", lambda mid: tmp_path)
        sr._create_recognizer("sensevoice", 4, language="zh")
        assert captured["language"] == "zh"
        assert "hotwords" not in captured

    def test_aux_model_is_not_a_recognizer(self, monkeypatch):
        from sayink import speech_recognizer as sr

        self._install_sherpa(monkeypatch)
        with pytest.raises(ValueError):
            sr._create_recognizer(sr.AUX_MODEL_SILERO_VAD, 4)


class TestMeetingModelKnobs:
    def test_supports_flags(self):
        from sayink import speech_recognizer as sr

        assert sr.model_supports("funasr-nano", sr.SUPPORTS_HOTWORDS)
        assert sr.model_supports("funasr-nano", sr.SUPPORTS_LANGUAGE)
        assert sr.model_supports("qwen3-asr-0.6b", sr.SUPPORTS_HOTWORDS)
        assert not sr.model_supports("qwen3-asr-0.6b", sr.SUPPORTS_LANGUAGE)
        assert sr.model_supports("sensevoice", sr.SUPPORTS_LANGUAGE)
        assert not sr.model_supports("fireredasr2-ctc", sr.SUPPORTS_HOTWORDS)
        assert not sr.model_supports("nonexistent", sr.SUPPORTS_HOTWORDS)

    def test_language_argument_per_loader(self):
        from sayink import speech_recognizer as sr

        assert sr.language_argument("funasr-nano", "zh") == "中文"
        assert sr.language_argument("funasr-nano", "en") == "英文"
        assert sr.language_argument("funasr-nano", "auto") == ""
        assert sr.language_argument("sensevoice", "yue") == "yue"
        assert sr.language_argument("sensevoice", "zh") == "zh"
        assert sr.language_argument("fireredasr2-ctc", "zh") == ""
        assert sr.language_argument("funasr-nano", "ko") == ""

    def test_format_hotwords_uses_terms_only_and_dedups(self):
        from sayink import speech_recognizer as sr

        glossary = [
            {"term": "SayInk", "variants": ["say ink", "赛音"]},
            {"term": " 张鑫 ", "variants": ["张新"]},
            {"term": "SayInk"},
            "Fun-ASR",
            {"term": "a,b"},
            {"term": "甲\n乙"},
            {"term": ""},
            {"term": "x" * 30},
        ]
        assert sr.format_hotwords(glossary) == "SayInk,张鑫,Fun-ASR,a b,甲 乙"
        assert sr.format_hotwords([]) == ""
        assert sr.format_hotwords(None) == ""

    def test_format_hotwords_caps_count(self):
        from sayink import speech_recognizer as sr

        many = [{"term": f"词{i}"} for i in range(200)]
        assert len(sr.format_hotwords(many).split(",")) == sr.MAX_HOTWORDS

    def test_effective_language_defaults_to_chinese(self):
        from sayink import speech_recognizer as sr

        assert sr.effective_language("auto") == "zh"
        assert sr.effective_language("") == "zh"
        assert sr.effective_language(" EN ") == "en"

    def test_recognition_params_read_shared_stt_settings(self):
        """Dictation and meetings derive the same (hotwords, language) target
        from ``stt.*`` so starting a meeting never forces a reload."""
        from sayink import speech_recognizer as sr

        class _Cfg:
            def __init__(self, data):
                self._d = data

            def get(self, key, default=None):
                return self._d.get(key, default)

        cfg = _Cfg({"stt.glossary": [{"term": "SayInk"}, {"term": "张鑫"}], "stt.language": "en"})
        assert sr.recognition_params(cfg) == ("SayInk,张鑫", "en")
        assert sr.recognition_params(_Cfg({})) == ("", "zh")
        assert sr.recognition_params(_Cfg({"stt.glossary": None, "stt.language": None})) == ("", "zh")

    def test_aux_registry_reachable_via_get_model_info(self):
        from sayink import speech_recognizer as sr

        spk = sr.get_model_info(sr.AUX_MODEL_SPEAKER)
        assert spk is not None and spk["aux"] is True
        assert spk["hf_repo"] == "csukuangfj/speaker-embedding-models"
        vad = sr.get_model_info(sr.AUX_MODEL_SILERO_VAD)
        assert vad["files"] == ["silero_vad.onnx"]
        assert sr.is_aux_model(sr.AUX_MODEL_SPEAKER)
        assert not sr.is_aux_model("funasr-nano")
        # Never offered as a dictation model.
        assert all(not m.get("aux") for m in sr.MODEL_REGISTRY)

    def test_configure_drops_unsupported_knobs_and_reloads_on_change(self, monkeypatch):
        from sayink import speech_recognizer as sr

        loads: list[tuple] = []

        class FakeWorker:
            def __init__(self, model_id, num_threads=4, *, hotwords="", language=""):
                loads.append((model_id, num_threads, hotwords, language))
                self.loaded = _Sig()
                self.error = _Sig()
                self.finished = _Sig()
                self.running = False

            def start(self):
                self.running = True

            def isRunning(self):
                return self.running

            def wait(self, _ms=0):
                return True

        class _Sig:
            def connect(self, *_):
                pass

        def _finish_load(rec):
            rec._load_worker.running = False
            rec._on_model_loaded(object(), rec._target())
            rec._on_load_worker_finished()

        monkeypatch.setattr(sr, "ModelLoadWorker", FakeWorker)
        monkeypatch.setattr(sr, "is_model_downloaded", lambda mid: True)
        rec = sr.SpeechRecognizer()
        rec.configure("fireredasr2-ctc", 4, hotwords="A,B", language="zh")
        assert loads[-1] == ("fireredasr2-ctc", 4, "", "")
        # Same effective target → no reload even though knobs differ.
        rec.configure("fireredasr2-ctc", 4, hotwords="C", language="en")
        _finish_load(rec)
        rec.configure("fireredasr2-ctc", 4)
        assert len(loads) == 1

        rec.configure("funasr-nano", 4, hotwords="A,B", language="zh")
        assert loads[-1] == ("funasr-nano", 4, "A,B", "中文")
        _finish_load(rec)
        rec.configure("funasr-nano", 4, hotwords="A,B", language="zh")
        assert len(loads) == 2
        rec.configure("funasr-nano", 4, hotwords="", language="auto")
        assert loads[-1] == ("funasr-nano", 4, "", "")
        assert rec.current_hotwords == ""


class TestModelLoadWorkerRun:
    def test_run_success_emits_loaded(self, monkeypatch):
        from sayink import speech_recognizer as sr

        sentinel = object()
        monkeypatch.setattr(sr, "_create_recognizer", lambda mid, n, **kw: sentinel)
        worker = sr.ModelLoadWorker("fireredasr2-ctc", 4)
        loaded, errors = [], []
        worker.loaded.connect(loaded.append)
        worker.error.connect(errors.append)
        worker.run()
        assert loaded == [sentinel]
        assert errors == []

    def test_run_failure_emits_error(self, monkeypatch):
        from sayink import speech_recognizer as sr

        def _boom(mid, n, **kw):
            raise RuntimeError("load failed")

        monkeypatch.setattr(sr, "_create_recognizer", _boom)
        worker = sr.ModelLoadWorker("fireredasr2-ctc", 4)
        loaded, errors = [], []
        worker.loaded.connect(loaded.append)
        worker.error.connect(errors.append)
        worker.run()
        assert loaded == []
        assert errors and "模型加载失败" in errors[0]


class TestSpeechRecognizerBehavior:
    def test_transcribe_final_not_ready_emits_error(self):
        from sayink.speech_recognizer import SpeechRecognizer

        rec = SpeechRecognizer()
        errors = []
        rec.error.connect(errors.append)
        rec.transcribe_final(np.ones(1600, dtype=np.float32))
        assert errors and "未就绪" in errors[0]

    def test_stale_result_is_ignored(self):
        from sayink.speech_recognizer import SpeechRecognizer

        rec = SpeechRecognizer()
        finals = []
        rec.final_result.connect(finals.append)
        rec._active_seq = 5
        rec._on_final_result("新结果", 5)
        rec._on_final_result("过时结果", 4)
        assert finals == ["新结果"]

    def test_stale_error_is_ignored(self):
        from sayink.speech_recognizer import SpeechRecognizer

        rec = SpeechRecognizer()
        errors = []
        rec.error.connect(errors.append)
        rec._active_seq = 2
        rec._on_worker_error("当前错误", 2)
        rec._on_worker_error("过时错误", 1)
        assert errors == ["当前错误"]

    def test_transcribe_final_no_terminate_and_supersedes(self, monkeypatch):
        from sayink import speech_recognizer as sr

        # Do not run real threads; capture worker lifecycle instead.
        monkeypatch.setattr(sr.TranscribeWorker, "start", lambda self: None)
        monkeypatch.setattr(sr.TranscribeWorker, "isRunning", lambda self: True)

        rec = sr.SpeechRecognizer()
        rec._is_ready = True
        rec._recognizer = _FakeRecognizer()

        rec.transcribe_final(np.ones(1600, dtype=np.float32))
        first = rec._current_worker
        assert rec._active_seq == 1

        rec.transcribe_final(np.ones(1600, dtype=np.float32))
        assert rec._active_seq == 2
        assert first._cancelled is True  # old worker cancelled, not terminated

    def test_funasr_long_utterance_is_sliced(self, monkeypatch):
        from sayink import speech_recognizer as sr

        monkeypatch.setattr(sr.TranscribeWorker, "start", lambda self: None)
        rec = sr.SpeechRecognizer()
        rec._is_ready = True
        rec._recognizer = _FakeRecognizer()
        rec._model_id = "funasr-nano"
        rec.transcribe_final(np.ones(1600, dtype=np.float32))
        assert rec._current_worker._max_slice_sec == 15.0
        assert rec._current_worker._overlap_sec == 0.4

    def test_sensevoice_long_utterance_is_not_sliced(self, monkeypatch):
        from sayink import speech_recognizer as sr

        monkeypatch.setattr(sr.TranscribeWorker, "start", lambda self: None)
        rec = sr.SpeechRecognizer()
        rec._is_ready = True
        rec._recognizer = _FakeRecognizer()
        rec._model_id = "sensevoice"
        rec.transcribe_final(np.ones(1600, dtype=np.float32))
        assert rec._current_worker._max_slice_sec is None

    def test_configure_skips_when_model_not_downloaded(self, monkeypatch):
        from sayink import speech_recognizer as sr

        monkeypatch.setattr(sr, "is_model_downloaded", lambda mid: False)
        loads = []
        monkeypatch.setattr(
            sr.SpeechRecognizer, "_load_model", lambda self: loads.append(True)
        )
        rec = sr.SpeechRecognizer()
        rec.configure("fireredasr2-ctc", 4)
        assert loads == []

    def test_configure_triggers_load_when_downloaded(self, monkeypatch):
        from sayink import speech_recognizer as sr

        monkeypatch.setattr(sr, "is_model_downloaded", lambda mid: True)
        loads = []
        monkeypatch.setattr(
            sr.SpeechRecognizer, "_load_model", lambda self: loads.append(True)
        )
        rec = sr.SpeechRecognizer()
        rec.configure("fireredasr2-ctc", 4)
        assert loads == [True]

    def test_configure_skips_reload_when_ready(self, monkeypatch):
        from sayink import speech_recognizer as sr

        monkeypatch.setattr(sr, "is_model_downloaded", lambda mid: True)
        loads = []
        monkeypatch.setattr(
            sr.SpeechRecognizer, "_load_model", lambda self: loads.append(True)
        )
        rec = sr.SpeechRecognizer()
        rec._model_id = "fireredasr2-ctc"
        rec._num_threads = 4
        rec._is_ready = True
        rec._recognizer = _FakeRecognizer()
        rec.configure("fireredasr2-ctc", 4)
        assert loads == []  # already resident, no reload

    def test_is_ready_and_current_model_properties(self):
        from sayink.speech_recognizer import SpeechRecognizer

        rec = SpeechRecognizer()
        assert rec.is_ready is False
        assert rec.is_loading is False
        assert rec.current_model_id == ""


class _FakeLoadWorker:
    instances: tuple = ()

    def __init__(self, model_id, num_threads, *, hotwords="", language=""):
        from unittest.mock import MagicMock

        self.model_id = model_id
        self.hotwords = hotwords
        self.language = language
        self.running = False
        self.loaded = MagicMock()
        self.error = MagicMock()
        self.finished = MagicMock()
        _FakeLoadWorker.instances = (*_FakeLoadWorker.instances, self)

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def wait(self, _ms=0):
        return True

    def complete(self, recognizer):
        self.running = False
        self.loaded.connect.call_args[0][0](recognizer)
        self.finished.connect.call_args[0][0]()


class TestSwitchModelDuringLoad:
    def _recognizer(self, monkeypatch):
        import sayink.speech_recognizer as sr

        _FakeLoadWorker.instances = ()
        monkeypatch.setattr(sr, "ModelLoadWorker", _FakeLoadWorker)
        monkeypatch.setattr(sr, "is_model_downloaded", lambda _mid: True)
        return sr.SpeechRecognizer()

    def test_newer_choice_is_loaded_after_the_running_load(self, monkeypatch):
        rec = self._recognizer(monkeypatch)
        ready = []
        rec.ready.connect(lambda: ready.append(rec.current_model_id))

        rec.configure("sensevoice", 4)
        rec.configure("paraformer-zh", 4)
        assert len(_FakeLoadWorker.instances) == 1

        _FakeLoadWorker.instances[0].complete("sensevoice-engine")
        assert rec.is_ready is False
        assert ready == []
        assert [w.model_id for w in _FakeLoadWorker.instances] == ["sensevoice", "paraformer-zh"]

        _FakeLoadWorker.instances[1].complete("paraformer-engine")
        assert rec.is_ready is True
        assert rec._recognizer == "paraformer-engine"
        assert ready == ["paraformer-zh"]

    def test_switching_back_does_not_reload_twice(self, monkeypatch):
        rec = self._recognizer(monkeypatch)
        rec.configure("sensevoice", 4)
        rec.configure("paraformer-zh", 4)
        rec.configure("sensevoice", 4)

        _FakeLoadWorker.instances[0].complete("sensevoice-engine")

        assert rec.is_ready is True
        assert rec._recognizer == "sensevoice-engine"
        assert len(_FakeLoadWorker.instances) == 1


class _FakeHttpStream:
    def __init__(self, body: bytes, length: int | None = None, fail: Exception | None = None):
        self._body = body
        self._fail = fail
        self.headers = {"content-length": str(len(body) if length is None else length)}

    def __enter__(self):
        if self._fail is not None:
            raise self._fail
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size=0):
        yield self._body


_TINY_REVISION = "0123456789abcdef0123456789abcdef01234567"


def _sha256(body: bytes) -> str:
    import hashlib

    return hashlib.sha256(body).hexdigest()


class TestModelDownloadSources:
    def _worker(self, monkeypatch, tmp_path, responses, source="auto"):
        import httpx
        import sayink.speech_recognizer as sr

        info = {"id": "tiny", "name": "Tiny", "hf_repo": "org/tiny", "dir_name": "tiny", "files": ["a.bin"],
                "revision": _TINY_REVISION, "sha256": {"a.bin": _sha256(b"weights")}}
        monkeypatch.setattr(sr, "get_model_info", lambda _mid: info)
        monkeypatch.setattr(sr, "_get_models_dir", lambda: tmp_path)
        monkeypatch.setattr(sr, "is_model_downloaded", lambda _mid: (tmp_path / "tiny" / "a.bin").exists())
        urls = []

        def _stream(_method, url, **_kw):
            urls.append(url)
            return responses.pop(0)

        monkeypatch.setattr(httpx, "stream", _stream)
        worker = sr.ModelDownloadWorker("tiny", source=source)
        done, errors = [], []
        worker.finished_ok.connect(done.append)
        worker.error.connect(errors.append)
        return worker, urls, done, errors

    def test_auto_falls_back_to_mirror_when_official_unreachable(self, monkeypatch, tmp_path):
        import httpx

        responses = [_FakeHttpStream(b"", fail=httpx.ConnectError("blocked")), _FakeHttpStream(b"weights")]
        worker, urls, done, errors = self._worker(monkeypatch, tmp_path, responses)
        worker.run()
        assert urls[0].startswith("https://huggingface.co/")
        assert urls[1].startswith("https://hf-mirror.com/")
        assert done == ["tiny"] and errors == []
        assert (tmp_path / "tiny" / "a.bin").read_bytes() == b"weights"

    def test_truncated_file_is_not_kept(self, monkeypatch, tmp_path):
        responses = [_FakeHttpStream(b"half", length=100)]
        worker, _urls, done, errors = self._worker(monkeypatch, tmp_path, responses, source="huggingface")
        worker.run()
        assert done == []
        assert errors and "不完整" in errors[0]
        assert not (tmp_path / "tiny" / "a.bin").exists()
        assert not (tmp_path / "tiny" / "a.bin.tmp").exists()

    def test_a_corrupt_leftover_is_replaced_instead_of_kept(self, monkeypatch, tmp_path):
        """An empty stub / wrong-content file in the models dir used to be
        skipped as 「已存在」, so every retry failed with zero network calls."""
        (tmp_path / "tiny").mkdir()
        (tmp_path / "tiny" / "a.bin").write_bytes(b"garbage from an earlier attempt")
        responses = [_FakeHttpStream(b"weights")]
        worker, urls, done, errors = self._worker(monkeypatch, tmp_path, responses, source="huggingface")
        worker.run()
        assert len(urls) == 1
        assert done == ["tiny"] and errors == []
        assert (tmp_path / "tiny" / "a.bin").read_bytes() == b"weights"

    def test_a_verified_leftover_is_not_downloaded_again(self, monkeypatch, tmp_path):
        (tmp_path / "tiny").mkdir()
        (tmp_path / "tiny" / "a.bin").write_bytes(b"weights")
        worker, urls, done, errors = self._worker(monkeypatch, tmp_path, [], source="huggingface")
        worker.run()
        assert urls == []
        assert done == ["tiny"] and errors == []

    def test_download_uses_the_pinned_revision(self, monkeypatch, tmp_path):
        responses = [_FakeHttpStream(b"weights")]
        worker, urls, done, _errors = self._worker(monkeypatch, tmp_path, responses, source="huggingface")
        worker.run()
        assert urls == [f"https://huggingface.co/org/tiny/resolve/{_TINY_REVISION}/a.bin"]
        assert done == ["tiny"]

    def test_file_with_wrong_checksum_is_not_kept(self, monkeypatch, tmp_path):
        responses = [_FakeHttpStream(b"tampered")]
        worker, _urls, done, errors = self._worker(monkeypatch, tmp_path, responses, source="huggingface")
        worker.run()
        assert done == []
        assert errors and "校验失败" in errors[0]
        assert not (tmp_path / "tiny" / "a.bin").exists()
        assert not (tmp_path / "tiny" / "a.bin.tmp").exists()

    def test_auto_tries_the_mirror_after_a_checksum_mismatch(self, monkeypatch, tmp_path):
        responses = [_FakeHttpStream(b"tampered"), _FakeHttpStream(b"weights")]
        worker, urls, done, errors = self._worker(monkeypatch, tmp_path, responses)
        worker.run()
        assert urls[1].startswith("https://hf-mirror.com/")
        assert done == ["tiny"] and errors == []
        assert (tmp_path / "tiny" / "a.bin").read_bytes() == b"weights"

    def test_progress_is_weighted_by_bytes(self, monkeypatch, tmp_path):
        import httpx
        import sayink.speech_recognizer as sr

        mb = 1024 * 1024
        bodies = {"small.bin": b"s" * mb, "big.bin": b"b" * (3 * mb)}
        info = {"id": "tiny", "name": "Tiny", "hf_repo": "org/tiny", "dir_name": "tiny",
                "size_mb": 4, "files": ["small.bin", "big.bin"], "revision": _TINY_REVISION,
                "sha256": {name: _sha256(body) for name, body in bodies.items()}}
        monkeypatch.setattr(sr, "get_model_info", lambda _mid: info)
        monkeypatch.setattr(sr, "_get_models_dir", lambda: tmp_path)
        monkeypatch.setattr(sr, "is_model_downloaded", lambda _mid: True)
        monkeypatch.setattr(
            httpx, "stream",
            lambda _method, url, **_kw: _FakeHttpStream(bodies[url.rsplit("/", 1)[-1]]),
        )
        worker = sr.ModelDownloadWorker("tiny", source="huggingface")
        progress = []
        worker.progress.connect(progress.append)
        worker.run()
        assert progress == [25, 99, 100]

    def test_every_registry_file_is_pinned_to_a_commit_and_hash(self):
        import re

        from sayink.speech_recognizer import AUX_MODEL_REGISTRY, MODEL_REGISTRY

        for info in MODEL_REGISTRY + AUX_MODEL_REGISTRY:
            assert re.fullmatch(r"[0-9a-f]{40}", info.get("revision", "")), info["id"]
            hashes = info.get("sha256", {})
            assert set(hashes) == set(info["files"]), info["id"]
            assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes.values()), info["id"]

    def test_download_endpoints_default_to_auto(self):
        from sayink.speech_recognizer import download_endpoints, HF_URL, HF_MIRROR_URL

        assert download_endpoints("") == (HF_URL, HF_MIRROR_URL)
        assert download_endpoints("hf-mirror") == (HF_MIRROR_URL,)
