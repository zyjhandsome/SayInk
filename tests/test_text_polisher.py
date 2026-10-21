import sys
from unittest.mock import MagicMock, patch

import pytest

from sayink.text_polisher import (
    INSECURE_URL_ERROR,
    POLISH_PROMPT,
    PolishWorker,
    TextPolisher,
    is_secure_or_local_url,
)


class TestSecureOrLocalUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://api.deepseek.com/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "http://localhost:11434/v1",
            "http://127.0.0.1:11434/v1",
            "http://[::1]:11434/v1",
            "http://localhost/chat/completions",
            "http://192.168.1.20:11434/v1",
            "http://10.0.0.5:8000/v1",
            "http://172.16.3.4/v1",
            "http://ollama-box.local:11434/v1",
            "http://api.example.com/v1",
            "http://8.8.8.8/v1",
            "http://172.32.0.1/v1",
            "http://insecure-remote.com/chat/completions",
            "HTTP://api.example.com/v1",
        ],
    )
    def test_allowed_urls(self, url):
        assert is_secure_or_local_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://localhost/x",
            "file:///etc/passwd",
            "api.example.com/v1",
            "http://",
            "",
            "   ",
        ],
    )
    def test_rejected_urls(self, url):
        assert is_secure_or_local_url(url) is False

    def test_ollama_default_endpoint_allowed(self):
        # Regression for SD-03: Ollama's default HTTP endpoint must be usable.
        assert is_secure_or_local_url("http://localhost:11434/v1") is True


def _fake_httpx_module(response=None, raise_exc=None):
    """Build a fake ``httpx`` module usable as ``import httpx`` inside run()."""
    fake = MagicMock()

    class _Timeout(Exception):
        pass

    class _HTTPStatusError(Exception):
        def __init__(self, resp):
            self.response = resp

    class _NetworkError(Exception):
        pass

    class _ConnectError(_NetworkError):
        pass

    class _RemoteProtocolError(Exception):
        pass

    fake.TimeoutException = _Timeout
    fake.HTTPStatusError = _HTTPStatusError
    fake.NetworkError = _NetworkError
    fake.ConnectError = _ConnectError
    fake.RemoteProtocolError = _RemoteProtocolError

    client = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    fake.Client.return_value = ctx
    if raise_exc is not None:
        client.post.side_effect = raise_exc
    else:
        client.post.return_value = response
    return fake, client


class TestPolishPrompt:
    def test_prompt_exists(self):
        assert POLISH_PROMPT is not None
        assert len(POLISH_PROMPT) > 0

    def test_prompt_contains_rules(self):
        assert "润色" in POLISH_PROMPT
        assert "规则" in POLISH_PROMPT or "严格规则" in POLISH_PROMPT

    def test_prompt_mentions_no_response(self):
        assert "不要回答" in POLISH_PROMPT or "不要" in POLISH_PROMPT

    def test_prompt_has_examples(self):
        assert "示例" in POLISH_PROMPT
        assert "输入" in POLISH_PROMPT
        assert "输出" in POLISH_PROMPT


class TestPolishPromptOnly:
    def test_build_polish_uses_default_prompt(self):
        from sayink.text_polisher import LLM_MODE_POLISH, build_system_prompt

        prompt = build_system_prompt(LLM_MODE_POLISH, custom_prompt="")
        assert prompt == POLISH_PROMPT

    def test_translate_mode_constant_removed(self):
        import sayink.text_polisher as tp

        assert not hasattr(tp, "LLM_MODE_TRANSLATE")
        assert not hasattr(tp, "TRANSLATE_PROMPT_TEMPLATE")


class TestPolishWorkerInit:
    def test_init_requires_api_params(self):
        worker = PolishWorker(
            api_url="https://api.example.com",
            api_key="test_key",
            model_name="test_model",
            text="测试文本"
        )
        assert worker._api_url == "https://api.example.com"
        assert worker._api_key == "test_key"
        assert worker._model_name == "test_model"
        assert worker._text == "测试文本"
        assert worker._cancelled is False

    def test_init_strips_trailing_slash(self):
        worker = PolishWorker(
            api_url="https://api.example.com/",
            api_key="key",
            model_name="model",
            text="text"
        )
        assert worker._api_url == "https://api.example.com"

    def test_init_custom_prompt(self):
        custom_prompt = "自定义提示词"
        worker = PolishWorker(
            api_url="https://api.example.com",
            api_key="key",
            model_name="model",
            text="text",
            prompt=custom_prompt
        )
        assert worker._prompt == custom_prompt

    def test_init_default_prompt(self):
        worker = PolishWorker(
            api_url="https://api.example.com",
            api_key="key",
            model_name="model",
            text="text"
        )
        assert worker._prompt == POLISH_PROMPT


class TestPolishWorkerCancel:
    def test_cancel_flag_initially_false(self):
        worker = PolishWorker(
            api_url="https://api.example.com",
            api_key="key",
            model_name="model",
            text="text"
        )
        assert worker._cancelled is False

    def test_cancel_sets_flag(self):
        worker = PolishWorker(
            api_url="https://api.example.com",
            api_key="key",
            model_name="model",
            text="text"
        )
        worker.cancel()
        assert worker._cancelled is True

    def test_double_cancel(self):
        worker = PolishWorker(
            api_url="https://api.example.com",
            api_key="key",
            model_name="model",
            text="text"
        )
        worker.cancel()
        worker.cancel()
        assert worker._cancelled is True


class TestPolishWorkerSignals:
    def test_signals_defined(self):
        worker = PolishWorker(
            api_url="https://api.example.com",
            api_key="key",
            model_name="model",
            text="text"
        )
        assert hasattr(worker, "result_ready")
        assert hasattr(worker, "error")


class TestTextPolisherInit:
    def test_init_no_worker(self):
        polisher = TextPolisher()
        assert polisher._worker is None


class TestTextPolisherPolish:
    def test_polish_empty_text(self):
        polisher = TextPolisher()
        emitted_values = []
        polisher.polish_complete.connect(lambda x: emitted_values.append(x))
        polisher.polish("", "https://api.example.com", "key", "model")
        assert len(emitted_values) == 1
        assert emitted_values[0] == ""


class TestTextPolisherCancel:
    def test_cancel_no_worker(self):
        polisher = TextPolisher()
        polisher.cancel()
        assert polisher._worker is None


class TestTestConnection:
    def test_scheme_required(self):
        success, message = TextPolisher.test_connection(
            "api.example.com",
            "key",
            "model"
        )
        assert success is False
        assert message == INSECURE_URL_ERROR

    def test_non_http_scheme_rejected(self):
        success, message = TextPolisher.test_connection(
            "ftp://insecure-api.com/chat/completions",
            "key",
            "model"
        )
        assert success is False
        assert message == INSECURE_URL_ERROR


class TestApiUrlConstruction:
    def test_url_with_chat_completions(self):
        worker = PolishWorker(
            api_url="https://api.example.com/chat/completions",
            api_key="key",
            model_name="model",
            text="text"
        )
        assert worker._api_url == "https://api.example.com/chat/completions"

    def test_url_without_chat_completions(self):
        worker = PolishWorker(
            api_url="https://api.example.com/v1",
            api_key="key",
            model_name="model",
            text="text"
        )
        assert worker._api_url == "https://api.example.com/v1"


class TestTextPolisherSignals:
    def test_signals_defined(self):
        polisher = TextPolisher()
        assert hasattr(polisher, "polish_complete")
        assert hasattr(polisher, "polish_error")


def _make_response(payload, status_ok=True):
    resp = MagicMock()
    resp.json.return_value = payload
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = RuntimeError("HTTP 500")
    return resp


class TestPolishWorkerRun:
    def _run_worker(self, worker, fake_httpx):
        results, errors = [], []
        worker.result_ready.connect(results.append)
        worker.error.connect(errors.append)
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            worker.run()
        return results, errors

    def test_run_success_emits_polished_text(self):
        payload = {"choices": [{"message": {"content": " 润色后的文本 "}}]}
        fake, client = _fake_httpx_module(_make_response(payload))
        worker = PolishWorker("https://api.example.com/v1", "k", "m", "原始文本")
        results, errors = self._run_worker(worker, fake)
        assert results == ["润色后的文本"]
        assert errors == []
        # URL gets /chat/completions appended
        assert client.post.call_args[0][0].endswith("/chat/completions")

    def test_cancel_during_the_request_closes_the_connection_and_emits_nothing(self):
        """Exit must not wait the full 15 s timeout on a request already sent:
        cancel() drops the client from the main thread."""
        payload = {"choices": [{"message": {"content": "迟到的结果"}}]}
        fake, client = _fake_httpx_module(_make_response(payload))
        worker = PolishWorker("https://api.example.com/v1", "k", "m", "原始文本")

        def slow_post(*args, **kwargs):
            worker.cancel()  # arrives while post() is blocking
            return _make_response(payload)

        client.post.side_effect = slow_post
        results, errors = self._run_worker(worker, fake)
        assert results == [] and errors == []
        client.close.assert_called_once()
        assert worker._client is None

    def test_run_allows_local_http_endpoint(self):
        payload = {"choices": [{"message": {"content": "本地润色"}}]}
        fake, _ = _fake_httpx_module(_make_response(payload))
        worker = PolishWorker("http://localhost:11434/v1", "", "m", "文本")
        results, errors = self._run_worker(worker, fake)
        assert results == ["本地润色"]
        assert errors == []

    def test_run_without_key_sends_no_authorization_header(self):
        payload = {"choices": [{"message": {"content": "本地润色"}}]}
        fake, client = _fake_httpx_module(_make_response(payload))
        worker = PolishWorker("http://localhost:11434/v1", "", "m", "文本")
        self._run_worker(worker, fake)
        assert "Authorization" not in client.post.call_args.kwargs["headers"]

    def test_run_with_key_sends_bearer_header(self):
        payload = {"choices": [{"message": {"content": "ok"}}]}
        fake, client = _fake_httpx_module(_make_response(payload))
        worker = PolishWorker("https://api.example.com/v1", "sk-x", "m", "文本")
        self._run_worker(worker, fake)
        assert client.post.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-x"

    def test_run_rejects_non_http_scheme(self):
        fake, client = _fake_httpx_module(_make_response({}))
        worker = PolishWorker("ftp://api.example.com/v1", "k", "m", "文本")
        results, errors = self._run_worker(worker, fake)
        assert results == []
        assert errors == [INSECURE_URL_ERROR]
        client.post.assert_not_called()

    def test_run_allows_remote_http(self):
        payload = {"choices": [{"message": {"content": "远程明文"}}]}
        fake, client = _fake_httpx_module(_make_response(payload))
        worker = PolishWorker("http://api.example.com/v1", "k", "m", "文本")
        results, errors = self._run_worker(worker, fake)
        assert results == ["远程明文"]
        assert errors == []
        client.post.assert_called_once()

    def test_run_cancelled_before_start_does_nothing(self):
        fake, client = _fake_httpx_module(_make_response({}))
        worker = PolishWorker("https://api.example.com/v1", "k", "m", "文本")
        worker.cancel()
        results, errors = self._run_worker(worker, fake)
        assert results == [] and errors == []
        client.post.assert_not_called()

    def test_run_timeout_emits_friendly_error(self):
        fake, client = _fake_httpx_module()
        client.post.side_effect = fake.TimeoutException()
        worker = PolishWorker("https://api.example.com/v1", "k", "m", "文本")
        results, errors = self._run_worker(worker, fake)
        assert results == []
        assert errors and "超时" in errors[0]

    def test_run_empty_choices_emits_error(self):
        fake, _ = _fake_httpx_module(_make_response({"choices": []}))
        worker = PolishWorker("https://api.example.com/v1", "k", "m", "文本")
        results, errors = self._run_worker(worker, fake)
        assert results == []
        assert errors and "响应格式异常" in errors[0]

    def test_run_empty_content_emits_error(self):
        payload = {"choices": [{"message": {"content": "   "}}]}
        fake, _ = _fake_httpx_module(_make_response(payload))
        worker = PolishWorker("https://api.example.com/v1", "k", "m", "文本")
        results, errors = self._run_worker(worker, fake)
        assert results == []
        assert errors and "空内容" in errors[0]

    def test_run_generic_exception_emits_error(self):
        fake, client = _fake_httpx_module()
        client.post.side_effect = ValueError("boom")
        worker = PolishWorker("https://api.example.com/v1", "k", "m", "文本")
        results, errors = self._run_worker(worker, fake)
        assert results == []
        assert errors and "润色失败" in errors[0]


class TestPolishWorkerRetry:
    """F-22: one retry for transient connection/gateway failures, none for timeouts."""

    def _worker(self):
        worker = PolishWorker("https://api.example.com/v1", "k", "m", "文本")
        worker._RETRY_DELAY_SEC = 0
        return worker

    def _run_worker(self, worker, fake_httpx):
        results, errors = [], []
        worker.result_ready.connect(results.append)
        worker.error.connect(errors.append)
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            worker.run()
        return results, errors

    def test_connect_error_is_retried_once_then_succeeds(self):
        ok = _make_response({"choices": [{"message": {"content": "润色"}}]})
        ok.status_code = 200
        fake, client = _fake_httpx_module()
        client.post.side_effect = [fake.ConnectError("reset"), ok]
        results, errors = self._run_worker(self._worker(), fake)
        assert results == ["润色"] and errors == []
        assert client.post.call_count == 2

    def test_connect_error_twice_gives_up(self):
        fake, client = _fake_httpx_module()
        client.post.side_effect = [fake.ConnectError("reset"), fake.ConnectError("reset")]
        results, errors = self._run_worker(self._worker(), fake)
        assert results == []
        assert errors and "润色失败" in errors[0]
        assert client.post.call_count == 2

    def test_gateway_error_is_retried_once(self):
        bad = _make_response({}, status_ok=False)
        bad.status_code = 503
        ok = _make_response({"choices": [{"message": {"content": "润色"}}]})
        ok.status_code = 200
        fake, client = _fake_httpx_module()
        client.post.side_effect = [bad, ok]
        results, errors = self._run_worker(self._worker(), fake)
        assert results == ["润色"] and errors == []
        assert client.post.call_count == 2

    def test_timeout_is_not_retried(self):
        fake, client = _fake_httpx_module()
        client.post.side_effect = fake.TimeoutException()
        results, errors = self._run_worker(self._worker(), fake)
        assert results == []
        assert errors and "超时" in errors[0]
        assert client.post.call_count == 1

    def test_rate_limit_is_not_retried(self):
        bad = _make_response({}, status_ok=False)
        bad.status_code = 429
        fake, client = _fake_httpx_module()
        client.post.side_effect = [bad]
        results, errors = self._run_worker(self._worker(), fake)
        assert results == [] and errors
        assert client.post.call_count == 1

    def test_cancel_between_attempts_emits_nothing(self):
        fake, client = _fake_httpx_module()
        worker = self._worker()

        def first_fails(*_a, **_k):
            worker.cancel()
            raise fake.ConnectError("reset")

        client.post.side_effect = first_fails
        results, errors = self._run_worker(worker, fake)
        assert results == [] and errors == []
        assert client.post.call_count == 1


class TestChatCompletionHelper:
    """chat_completion() is the one HTTP path shared by 润色 and 会议纪要."""

    def _call(self, client, fake, **overrides):
        from sayink.text_polisher import chat_completion

        kwargs = dict(
            api_url="https://api.example.com/v1/",
            api_key="sk-x",
            model_name="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=99,
            retry_delay_sec=0,
        )
        kwargs.update(overrides)
        return chat_completion(client, fake, **kwargs)

    def test_posts_openai_payload_and_returns_stripped_content(self):
        fake, client = _fake_httpx_module(_make_response({"choices": [{"message": {"content": "  好的 \n"}}]}))
        assert self._call(client, fake) == "好的"
        args, kwargs = client.post.call_args
        assert args[0] == "https://api.example.com/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-x"
        assert kwargs["json"] == {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
            "max_tokens": 99,
        }

    def test_url_already_ending_in_chat_completions_is_kept(self):
        from sayink.text_polisher import chat_completions_url

        assert chat_completions_url("https://x/v1/chat/completions") == "https://x/v1/chat/completions"
        assert chat_completions_url("https://x/v1/") == "https://x/v1/chat/completions"
        assert chat_completions_url("  https://x/v1/  ") == "https://x/v1/chat/completions"
        assert chat_completions_url("https://x/v1?x=1") == "https://x/v1/chat/completions?x=1"
        assert chat_completions_url("https://x/v1#frag") == "https://x/v1/chat/completions"
        assert (
            chat_completions_url("https://user:secret@x/v1/chat/completions?x=1")
            == "https://x/v1/chat/completions?x=1"
        )

    def test_malformed_and_empty_answers_raise_response_error(self):
        from sayink.text_polisher import LLMResponseError

        for payload, text in (
            ({"choices": []}, "响应格式异常"),
            ({"choices": [{"message": "nope"}]}, "响应格式异常"),
            ({"choices": [{"message": {"content": "  "}}]}, "空内容"),
            ({"choices": [{"message": {"content": None}}]}, "空内容"),
        ):
            fake, client = _fake_httpx_module(_make_response(payload))
            with pytest.raises(LLMResponseError, match=text):
                self._call(client, fake)

    def test_gateway_and_network_errors_retry_once_then_propagate(self):
        bad = _make_response({}, status_ok=False)
        bad.status_code = 502
        fake, client = _fake_httpx_module()
        client.post.side_effect = [bad, bad]
        with pytest.raises(RuntimeError, match="HTTP 500"):  # the fake's raise_for_status
            self._call(client, fake)
        assert client.post.call_count == 2

        fake, client = _fake_httpx_module()
        client.post.side_effect = [fake.RemoteProtocolError("eof"), fake.RemoteProtocolError("eof")]
        with pytest.raises(fake.RemoteProtocolError):
            self._call(client, fake)
        assert client.post.call_count == 2

    def test_cancel_is_checked_before_and_after_each_attempt(self):
        from sayink.text_polisher import LLMCancelled

        fake, client = _fake_httpx_module(_make_response({"choices": [{"message": {"content": "x"}}]}))
        with pytest.raises(LLMCancelled):
            self._call(client, fake, is_cancelled=lambda: True)
        client.post.assert_not_called()

        flag = {"c": False}

        def post_then_cancel(*_a, **_k):
            flag["c"] = True
            raise fake.ConnectError("reset")

        fake, client = _fake_httpx_module()
        client.post.side_effect = post_then_cancel
        with pytest.raises(LLMCancelled):
            self._call(client, fake, is_cancelled=lambda: flag["c"])
        assert client.post.call_count == 1


class TestPolishSettingsComplete:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:11434/v1",
            "http://127.0.0.1:1234/v1",
            "http://192.168.1.20:11434/v1",
            "http://gpu-box.local:8000/v1",
        ],
    )
    def test_local_endpoints_work_without_key(self, url):
        from sayink.text_polisher import polish_settings_complete

        assert polish_settings_complete(url, "", "local-model") is True

    def test_remote_endpoint_still_requires_key(self):
        from sayink.text_polisher import polish_settings_complete

        assert polish_settings_complete("https://api.deepseek.com/v1", "", "deepseek-chat") is False
        assert polish_settings_complete("https://api.deepseek.com/v1", "sk", "deepseek-chat") is True

    def test_url_and_model_always_required(self):
        from sayink.text_polisher import polish_settings_complete

        assert polish_settings_complete("", "sk", "m") is False
        assert polish_settings_complete("http://localhost:11434/v1", "", "") is False

    def test_keyless_connection_test_omits_authorization(self):
        fake, client = _fake_httpx_module(_make_response(_CHAT_OK))
        with patch.dict(sys.modules, {"httpx": fake}):
            ok, _ = TextPolisher.test_connection("http://localhost:11434/v1", "", "m")
        assert ok is True
        assert "Authorization" not in client.post.call_args.kwargs["headers"]


_CHAT_OK = {"choices": [{"message": {"content": "hi"}}]}


class TestApiInputNormalization:
    def test_spaced_host_is_rejected(self):
        assert is_secure_or_local_url("https://api.deepseek.com /v1") is False

    def test_fullwidth_scheme_explains_the_character(self):
        from sayink.text_polisher import FULLWIDTH_URL_ERROR, api_url_error

        assert api_url_error("https：//api.deepseek.com") == FULLWIDTH_URL_ERROR

    def test_userinfo_is_removed_but_the_path_stays(self):
        from sayink.text_polisher import redact_url_userinfo

        cleaned, removed = redact_url_userinfo("https://user:sk-secret@api.deepseek.com/v1")
        assert removed is True
        assert cleaned == "https://api.deepseek.com/v1"
        assert redact_url_userinfo("https://api.deepseek.com/v1") == ("https://api.deepseek.com/v1", False)

    def test_key_drops_bearer_quotes_and_internal_whitespace(self):
        from sayink.text_polisher import normalize_api_key

        assert normalize_api_key('  "Bearer sk-abc" \n') == "sk-abc"
        assert normalize_api_key("sk-ab\u200bcd") == "sk-abcd"

    def test_known_model_names_are_canonicalized(self):
        from sayink.text_polisher import normalize_model_name, thinking_payload_extra

        assert normalize_model_name(" DeepSeek－Flash ") == "deepseek-flash"
        assert normalize_model_name("deepseek flash") == "deepseek-flash"
        assert normalize_model_name("Qwen2.5") == "Qwen2.5"
        assert thinking_payload_extra(
            "https://api.deepseek.com/v1", "DeepSeek-Flash", "disabled"
        ) == {"thinking": {"type": "disabled"}}


class TestTestConnectionRun:
    def test_local_http_accepted(self):
        fake, client = _fake_httpx_module(_make_response(_CHAT_OK))
        with patch.dict(sys.modules, {"httpx": fake}):
            ok, msg = TextPolisher.test_connection(
                "http://localhost:11434/v1", "k", "m"
            )
        assert ok is True
        assert "成功" in msg

    def test_remote_http_is_requested(self):
        fake, client = _fake_httpx_module(_make_response(_CHAT_OK))
        with patch.dict(sys.modules, {"httpx": fake}):
            ok, msg = TextPolisher.test_connection(
                "http://api.example.com/v1", "k", "m"
            )
        assert ok is True
        assert "成功" in msg
        client.post.assert_called_once()

    def test_https_success(self):
        fake, _ = _fake_httpx_module(_make_response(_CHAT_OK))
        with patch.dict(sys.modules, {"httpx": fake}):
            ok, msg = TextPolisher.test_connection(
                "https://api.example.com/v1", "k", "m"
            )
        assert ok is True

    def test_empty_json_is_not_a_successful_connection(self):
        fake, _ = _fake_httpx_module(_make_response({}))
        with patch.dict(sys.modules, {"httpx": fake}):
            ok, msg = TextPolisher.test_connection("https://api.example.com/v1", "k", "m")
        assert ok is False
        assert "格式异常" in msg

    def test_blank_content_is_not_a_successful_connection(self):
        fake, _ = _fake_httpx_module(_make_response({"choices": [{"message": {"content": "  "}}]}))
        with patch.dict(sys.modules, {"httpx": fake}):
            ok, msg = TextPolisher.test_connection("https://api.example.com/v1", "k", "m")
        assert ok is False
        assert "空内容" in msg

    def test_deepseek_flash_test_disables_thinking(self):
        fake, client = _fake_httpx_module(_make_response(_CHAT_OK))
        with patch.dict(sys.modules, {"httpx": fake}):
            ok, _ = TextPolisher.test_connection(
                "https://api.deepseek.com", "Bearer sk-x", "deepseek-flash"
            )
        assert ok is True
        payload = client.post.call_args.kwargs["json"]
        assert payload["model"] == "deepseek-flash"
        assert payload["max_tokens"] == 16
        assert payload["thinking"] == {"type": "disabled"}
        assert client.post.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-x"
        assert client.post.call_args[0][0] == "https://api.deepseek.com/chat/completions"

    def test_http_401_names_the_key(self):
        resp = _make_response({})
        resp.status_code = 401
        fake, _ = _fake_httpx_module(resp)
        resp.raise_for_status.side_effect = fake.HTTPStatusError(resp)
        with patch.dict(sys.modules, {"httpx": fake}):
            ok, msg = TextPolisher.test_connection("https://api.example.com/v1", "k", "m")
        assert ok is False
        assert "密钥" in msg and "401" in msg


class TestPolishWorkerLifecycle:
    def test_idle_callbacks_without_a_live_sender_are_ignored(self):
        polisher = TextPolisher()
        results, errors = [], []
        polisher.polish_complete.connect(results.append)
        polisher.polish_error.connect(errors.append)
        polisher._on_result("stale result")
        polisher._on_error("stale error")
        assert results == errors == []

    @pytest.mark.parametrize("replace", [False, True])
    def test_queued_cancelled_results_cannot_escape_into_next_request(self, monkeypatch, replace):
        from PyQt6.QtCore import QCoreApplication, QObject, Qt, pyqtSignal

        class Worker(QObject):
            result_ready = pyqtSignal(str)
            error = pyqtSignal(str)
            finished = pyqtSignal()

            def __init__(self, *args, **kwargs):
                super().__init__()
                self.running = True

            def start(self):
                pass

            def isRunning(self):
                return self.running

            def cancel(self):
                pass

        monkeypatch.setattr("sayink.text_polisher.PolishWorker", Worker)
        polisher = TextPolisher()
        results, errors = [], []
        polisher.polish_complete.connect(results.append)
        polisher.polish_error.connect(errors.append)
        polisher.polish("old", "https://example.test", "", "m")
        old = polisher._worker
        old.result_ready.disconnect(polisher._on_result)
        old.error.disconnect(polisher._on_error)
        old.result_ready.connect(polisher._on_result, Qt.ConnectionType.QueuedConnection)
        old.error.connect(polisher._on_error, Qt.ConnectionType.QueuedConnection)
        old.result_ready.emit("stale result")
        old.error.emit("stale error")
        old.running = False  # Thread exited before its queued result reached the UI.
        polisher.cancel()
        if replace:
            polisher.polish("new", "https://example.test", "", "m")
            polisher._worker.result_ready.emit("current result")
        QCoreApplication.processEvents()
        assert results == (["current result"] if replace else [])
        assert errors == []

    def test_cancelled_worker_stays_retained_until_thread_finishes(self, monkeypatch):
        """A running QThread dropped by cancel() would be destroyed during GC
        and abort Qt; keep a reference until ``finished`` fires."""
        import threading

        from sayink.text_polisher import PolishWorker

        release = threading.Event()
        monkeypatch.setattr(PolishWorker, "run", lambda self: release.wait(2.0))

        polisher = TextPolisher()
        polisher.polish("需要润色", "https://api.example.com/v1", "k", "m")
        worker = polisher._worker
        assert worker is not None

        polisher.cancel()
        assert polisher._worker is None
        assert worker in polisher._workers

        release.set()
        assert worker.wait(2000)
        from PyQt6.QtCore import QCoreApplication

        QCoreApplication.processEvents()
        assert worker not in polisher._workers
