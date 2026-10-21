import ipaddress
import logging
import time
from typing import Callable
from urllib.parse import urlparse, urlunparse

from PyQt6.QtCore import QObject, pyqtSignal, QThread

from sayink.asr_correction import CORRECTION_PROMPT, correction_user_prompt

log = logging.getLogger("SayInk")

# Hosts treated as this machine or the LAN. Plain HTTP is allowed for any
# host; this set only decides whether an API key is optional and whether
# sending a meeting transcript needs an extra confirmation.
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}

INSECURE_URL_ERROR = "接口地址须以 http:// 或 https:// 开头，并包含主机名"
FULLWIDTH_URL_ERROR = "接口地址含有全角冒号或斜杠，请改用半角的 http:// 或 https://，并包含主机名"
HOST_GARBLED_ERROR = "接口地址的主机名含有空格或不可见字符。"
URL_USERINFO_ERROR = "接口地址里的账号或密钥已去掉，请把密钥填在「API 密钥」。"
PUBLIC_HTTP_NOTE = "该地址是公网 HTTP，密钥和转写内容会明文传输。"

_INVISIBLE_CHARS = "\u200b\u200c\u200d\ufeff\u2060\u00ad"
_INVISIBLE_TRANS = dict.fromkeys(map(ord, _INVISIBLE_CHARS), None)
_DASH_CHARS = ("－", "—", "–", "‑", "−")
_CANONICAL_MODELS = {
    "deepseek-flash": "deepseek-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
}


def is_local_endpoint(url: str) -> bool:
    """True for this machine or the home/office LAN (localhost, 192.168.x.x, *.local…)."""
    host = (urlparse((url or "").strip()).hostname or "").strip("[]").lower()
    if not host:
        return False
    if host in _LOCAL_HOSTNAMES or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _host_is_clean(host: str) -> bool:
    return bool(host) and not any(ch.isspace() or ch in _INVISIBLE_CHARS for ch in host)


def _request_netloc(parsed) -> str:
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        return f"{host}:{parsed.port}"
    return host


def is_secure_or_local_url(url: str) -> bool:
    """Allow any ``http`` or ``https`` URL that has a host.

    The scheme is the only gate. Public HTTP is permitted because the user
    types the address; the key and transcript then travel in cleartext.
    Other schemes are rejected. A host that itself contains spaces or
    invisible characters is rejected. Whether the host is local is a
    separate question (:func:`is_local_endpoint`).
    """
    parsed = urlparse((url or "").strip())
    return parsed.scheme in {"http", "https"} and _host_is_clean(parsed.hostname or "")


def api_url_error(url: str) -> str | None:
    """User-facing reason the address cannot be used, or ``None`` when it can."""
    text = (url or "").strip()
    if is_secure_or_local_url(text):
        return None
    parsed = urlparse(text)
    host = parsed.hostname or ""
    if parsed.scheme in {"http", "https"} and host and not _host_is_clean(host):
        return HOST_GARBLED_ERROR
    if "：" in text or "／" in text:
        return FULLWIDTH_URL_ERROR
    return INSECURE_URL_ERROR


def redact_url_userinfo(url: str) -> tuple[str, bool]:
    """Drop ``user:password@`` so a secret is not stored in the address."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if not parsed.hostname or (parsed.username is None and parsed.password is None):
        return raw, False
    cleaned = urlunparse((parsed.scheme, _request_netloc(parsed), parsed.path, "", parsed.query, parsed.fragment))
    return cleaned, True


def public_cleartext_note(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme == "http" and not is_local_endpoint(url):
        return PUBLIC_HTTP_NOTE
    return ""


def normalize_api_key(api_key: str) -> str:
    """Strip a pasted ``Bearer`` prefix, quotes, and whitespace inside the key."""
    key = (api_key or "").strip().translate(_INVISIBLE_TRANS)
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'“”‘’":
        key = key[1:-1].strip()
    if key[:7].lower() == "bearer ":
        key = key[7:].strip()
    return "".join(ch for ch in key if not ch.isspace() and ch not in _INVISIBLE_CHARS)


def normalize_model_name(model_name: str) -> str:
    """Drop invisible characters. Known DeepSeek ids are also canonicalized.

    Other names keep their spelling: a local model id can be case-sensitive.
    """
    text = (model_name or "").replace("\u3000", " ").translate(_INVISIBLE_TRANS)
    for dash in _DASH_CHARS:
        text = text.replace(dash, "-")
    text = " ".join(text.split())
    key = "-".join(text.casefold().replace("_", " ").replace("-", " ").split())
    return _CANONICAL_MODELS.get(key, text)


def api_key_required(url: str) -> bool:
    """Local servers such as Ollama often run without authentication."""
    return not is_local_endpoint(url)


def polish_settings_complete(api_url: str, api_key: str, model_name: str) -> bool:
    if not (api_url or "").strip() or not (model_name or "").strip():
        return False
    return bool((api_key or "").strip()) or not api_key_required(api_url)


def _request_headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    key = normalize_api_key(api_key)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def chat_completions_url(api_url: str) -> str:
    """OpenAI-compatible chat URL. Query strings stay; userinfo and fragments do not."""
    raw = (api_url or "").strip()
    parsed = urlparse(raw)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not _host_is_clean(host):
        url = raw.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        return url
    path = (parsed.path or "").rstrip("/")
    if not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions" if path else "/chat/completions"
    return urlunparse((parsed.scheme, _request_netloc(parsed), path, "", parsed.query, ""))


# One extra attempt for connection-level failures and gateway errors.
# Timeouts are deliberately not retried: the caller's budget is already the
# longest acceptable wait, and it falls back (raw text / error) anyway.
CHAT_RETRY_STATUS = frozenset({502, 503, 504})
CHAT_RETRY_DELAY_SEC = 0.5

# Official dual-mode DeepSeek models think by default; the reasoning shares
# the output budget with the edit and can leave ``content`` empty. Other
# endpoints would reject the field, so it is only sent to the official host.
_THINKING_TOGGLE_HOST = "api.deepseek.com"
_THINKING_TOGGLE_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})


def thinking_payload_extra(api_url: str, model_name: str, thinking_mode: str | None) -> dict:
    """Extra request fields that switch a provider's reasoning off, if any."""
    if (
        thinking_mode == "disabled"
        and urlparse((api_url or "").strip()).hostname == _THINKING_TOGGLE_HOST
        and normalize_model_name(model_name) in _THINKING_TOGGLE_MODELS
    ):
        return {"thinking": {"type": "disabled"}}
    return {}


class LLMCancelled(Exception):
    """The caller cancelled while a request was in flight."""


class LLMResponseError(RuntimeError):
    """The endpoint answered, but not with a usable chat completion."""


class LLMTruncatedError(LLMResponseError):
    """The provider stopped at its output limit; the text is incomplete."""


def chat_completion(
    client,
    httpx,
    *,
    api_url: str,
    api_key: str,
    model_name: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    is_cancelled: Callable[[], bool] | None = None,
    retry_delay_sec: float = CHAT_RETRY_DELAY_SEC,
    label: str = "LLM",
    reject_truncated: bool = False,
    extra_payload: dict | None = None,
) -> str:
    """One OpenAI-compatible chat completion, shared by polishing and minutes.

    POSTs once and retries once after ``retry_delay_sec`` on a transient
    network error or a 502/503/504. Returns the stripped message content.
    Raises :class:`LLMCancelled` when ``is_cancelled()`` turns true,
    :class:`LLMResponseError` for malformed/empty answers, and lets
    ``httpx`` exceptions (timeout, HTTP status) propagate. ``extra_payload``
    adds provider-specific fields (see :func:`thinking_payload_extra`).
    """
    cancelled = is_cancelled or (lambda: False)
    model_name = normalize_model_name(model_name)
    api_key = normalize_api_key(api_key)
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        **(extra_payload or {}),
    }
    url = chat_completions_url(api_url)
    headers = _request_headers(api_key)
    transient = (httpx.NetworkError, httpx.RemoteProtocolError)
    response = None
    for attempt in (1, 2):
        if cancelled():
            raise LLMCancelled()
        try:
            response = client.post(url, headers=headers, json=payload)
        except transient as exc:
            if cancelled():
                raise LLMCancelled() from exc
            if attempt == 2:
                raise
            log.warning("LLM %s 连接异常，%.1f 秒后重试一次: %s", label, retry_delay_sec, exc)
        else:
            if cancelled():
                raise LLMCancelled()
            if attempt == 2 or response.status_code not in CHAT_RETRY_STATUS:
                break
            log.warning("LLM %s 返回 HTTP %s，%.1f 秒后重试一次", label, response.status_code, retry_delay_sec)
        time.sleep(retry_delay_sec)
    if cancelled():
        raise LLMCancelled()
    response.raise_for_status()
    return chat_message_text(response.json(), reject_truncated=reject_truncated)


def chat_message_text(data, *, reject_truncated: bool = False) -> str:
    """Message text from a chat-completion body, or a response error."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices, list):
        raise LLMResponseError("API 响应格式异常")
    if reject_truncated and isinstance(choices[0], dict) and choices[0].get("finish_reason") in ("length", "max_tokens"):
        raise LLMTruncatedError("模型输出被截断")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise LLMResponseError("API 响应格式异常")
    content = message.get("content")
    text = str(content).strip() if content is not None else ""
    if not text:
        raise LLMResponseError("API 返回空内容")
    return text


def http_status_message(code: int | None) -> str:
    if code in (401, 403):
        return f"密钥被拒绝（HTTP {code}）。请检查 API 密钥。"
    if code == 404:
        return "接口路径不存在（HTTP 404）。请检查接口地址是否为 OpenAI 兼容的基址。"
    if code == 400:
        return "请求被拒绝（HTTP 400）。请检查模型名称是否与该接口匹配。"
    if code == 429:
        return "请求过于频繁（HTTP 429），请稍后重试。"
    if code is None:
        return "HTTP 错误"
    return f"HTTP 错误: {code}"

POLISH_PROMPT = (
    "你是一个纯文本润色工具，不是对话助手。\n"
    "你的唯一任务是：将用户发来的语音转写文字做最小程度的润色，使其变为通顺的书面语。\n\n"
    "严格规则：\n"
    "1. 绝对不要回答、回应或评论文字的内容，无论内容是问题、请求还是陈述。\n"
    "2. 只做最小修改：修正口语赘词（嗯、啊、那个、就是说）、补全标点、理顺语序。\n"
    "3. 保持原意和原有信息量完全不变，不要添加、删除或改写任何实质内容。\n"
    "4. 如果包含中英文混合，保持原有语言不变。\n"
    "5. 只输出润色后的文字本身，不要加引号、不要加前缀、不要做任何解释。\n\n"
    "示例：\n"
    "输入：嗯那个我觉得这个方案还行吧就是可能需要再优化一下\n"
    "输出：我觉得这个方案还行，可能需要再优化一下。\n\n"
    "输入：今天天气怎么样啊感觉还不错\n"
    "输出：今天天气怎么样？感觉还不错。"
)

LLM_MODE_POLISH = "polish"
# Correction: fix mis-heard characters and glossary spellings, keep everything
# else — including filler words — exactly as recognized.
LLM_MODE_CORRECT = "correct"
LLM_MODES = (LLM_MODE_POLISH, LLM_MODE_CORRECT)


def normalize_llm_mode(value) -> str:
    """Known mode name or ``polish`` (the historical default) for anything else."""
    mode = str(value or "").strip().lower()
    return mode if mode in LLM_MODES else LLM_MODE_POLISH


def prompt_config_key(mode: str) -> str:
    """Config key of the custom prompt for ``mode``.

    Each mode keeps its own prompt: a polish prompt the user tuned must not
    silently drive the correction mode (and be rejected by its guard) after a
    mode switch. ``llm.prompt`` stays the polish key for existing configs.
    """
    return "llm.prompt_correct" if normalize_llm_mode(mode) == LLM_MODE_CORRECT else "llm.prompt"


def custom_prompt_or_empty(prompt: str) -> str:
    """The prompt as something the user wrote, or "" when it is a built-in copy
    (the old 恢复默认 button used to paste the built-in text into the editor)."""
    custom = (prompt or "").strip()
    return "" if custom in (POLISH_PROMPT, CORRECTION_PROMPT) else custom


def build_system_prompt(
    mode: str = LLM_MODE_POLISH,
    *,
    custom_prompt: str = "",
) -> str:
    """System prompt for ``mode``; a custom prompt replaces it in either mode.

    A built-in prompt pasted verbatim (the 恢复默认 button does that) is not a
    customization, so switching modes afterwards still switches prompts.
    """
    custom = custom_prompt_or_empty(custom_prompt)
    if custom:
        return custom
    return CORRECTION_PROMPT if normalize_llm_mode(mode) == LLM_MODE_CORRECT else POLISH_PROMPT


def _mode_label(mode: str) -> str:
    return "纠错" if normalize_llm_mode(mode) == LLM_MODE_CORRECT else "润色"


class PolishWorker(QThread):
    result_ready = pyqtSignal(str)
    error = pyqtSignal(str)

    # See chat_completion(); tests zero this to skip the wait.
    _RETRY_DELAY_SEC = CHAT_RETRY_DELAY_SEC

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model_name: str,
        text: str,
        prompt: str = "",
        *,
        mode: str = LLM_MODE_POLISH,
        glossary=(),
        context: str = "",
    ):
        super().__init__()
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self._text = text
        self._mode = normalize_llm_mode(mode)
        self._prompt = prompt or build_system_prompt(self._mode)
        self._glossary = tuple(glossary or ())
        self._context = context or ""
        self._cancelled = False
        self._client = None
        self._action_label = _mode_label(self._mode)

    @property
    def is_correction(self) -> bool:
        return self._mode == LLM_MODE_CORRECT

    def user_message(self) -> str:
        """The user turn: the text itself, or for correction the text with the
        glossary and the preceding output attached."""
        if self.is_correction:
            return correction_user_prompt(self._text, self._glossary, self._context)
        return self._text

    def request_options(self) -> dict:
        """Sampling and provider fields. Correction decodes greedily and asks
        DeepSeek's dual-mode models not to think, so a one-line fix returns in
        well under a second instead of spending the budget on reasoning."""
        if self.is_correction:
            return {
                "temperature": 0.0,
                "extra_payload": thinking_payload_extra(self._api_url, self._model_name, "disabled"),
            }
        return {"temperature": 0.3, "extra_payload": {}}

    def cancel(self):
        """Stop the worker: set the flag and drop the HTTP connection so a
        request already waiting on the server does not hold the thread (and
        app exit) for the full 15 s timeout."""
        self._cancelled = True
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:  # best effort from another thread
                pass

    def run(self):
        if self._cancelled:
            return

        try:
            import httpx

            problem = api_url_error(self._api_url)
            if problem:
                log.warning("API URL 不是 http 或 https 地址")
                self.error.emit(problem)
                return

            log.info("正在调用 LLM %s (%s)...", self._action_label, self._model_name)
            log.debug("原文长度: %d 字符", len(self._text))

            options = self.request_options()
            with httpx.Client(timeout=15.0) as client:
                self._client = client
                try:
                    if self._cancelled:
                        return
                    polished = chat_completion(
                        client, httpx,
                        api_url=self._api_url,
                        api_key=self._api_key,
                        model_name=self._model_name,
                        messages=[
                            {"role": "system", "content": self._prompt},
                            {"role": "user", "content": self.user_message()},
                        ],
                        temperature=options["temperature"],
                        max_tokens=2048,
                        is_cancelled=lambda: self._cancelled,
                        retry_delay_sec=self._RETRY_DELAY_SEC,
                        label=self._action_label,
                        extra_payload=options["extra_payload"],
                    )
                finally:
                    self._client = None
            if self._cancelled:
                return
            log.debug("%s结果长度: %d 字符", self._action_label, len(polished))
            self.result_ready.emit(polished)

        except LLMCancelled:
            return
        except httpx.TimeoutException:
            log.error("LLM %s超时", self._action_label)
            self.error.emit(f"{self._action_label}超时，请稍后重试")
        except Exception as e:
            log.error("LLM %s失败: %s", self._action_label, e)
            self.error.emit(f"{self._action_label}失败: {str(e)}")


class TextPolisher(QObject):
    polish_complete = pyqtSignal(str)
    polish_error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: PolishWorker | None = None
        # Keep running QThreads alive until ``finished`` fires so GC never
        # destroys a live QThread after cancel().
        self._workers: list[PolishWorker] = []

    @property
    def running_workers(self):
        return tuple(worker for worker in self._workers if worker.isRunning())

    def polish(
        self,
        text: str,
        api_url: str,
        api_key: str,
        model_name: str,
        prompt: str = "",
        *,
        mode: str = LLM_MODE_POLISH,
        glossary=(),
        context: str = "",
    ):
        if not text.strip():
            self.polish_complete.emit(text)
            return

        self.cancel()

        system_prompt = build_system_prompt(
            mode,
            custom_prompt=prompt,
        )
        worker = PolishWorker(
            api_url,
            api_key,
            model_name,
            text,
            system_prompt,
            mode=mode,
            glossary=glossary,
            context=context,
        )
        worker.result_ready.connect(self._on_result)
        worker.error.connect(self._on_error)
        if hasattr(worker, "finished"):
            worker.finished.connect(lambda w=worker: self._reap_worker(w))
        self._worker = worker
        self._workers.append(worker)
        worker.start()

    def _reap_worker(self, worker: PolishWorker) -> None:
        if worker.isRunning():
            return
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        if worker is self._worker and not worker.isRunning():
            self._worker = None

    def cancel(self):
        """Cancel running worker properly."""
        worker = self._worker
        if worker and worker.isRunning():
            worker.cancel()  # 设置取消标志
            try:
                worker.result_ready.disconnect(self._on_result)
            except TypeError:
                pass
            try:
                worker.error.disconnect(self._on_error)
            except TypeError:
                pass
        elif worker is not None:
            self._reap_worker(worker)
        self._worker = None

    def _on_result(self, polished_text: str):
        if self._worker is None or self.sender() is not self._worker:
            return
        self.polish_complete.emit(polished_text)

    def _on_error(self, error_msg: str):
        if self._worker is None or self.sender() is not self._worker:
            return
        self.polish_error.emit(error_msg)

    @staticmethod
    def test_connection(api_url: str, api_key: str, model_name: str) -> tuple[bool, str]:
        problem = api_url_error(api_url)
        if problem:
            return False, problem

        try:
            import httpx

            model = normalize_model_name(model_name)
            key = normalize_api_key(api_key)
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 16,
                **thinking_payload_extra(api_url, model, "disabled"),
            }

            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    chat_completions_url(api_url),
                    headers=_request_headers(key),
                    json=payload,
                )
                response.raise_for_status()
                chat_message_text(response.json())

            return True, "连接成功"
        except httpx.TimeoutException:
            return False, "连接超时"
        except httpx.HTTPStatusError as exc:
            return False, http_status_message(getattr(getattr(exc, "response", None), "status_code", None))
        except LLMResponseError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"连接失败: {str(exc)}"
