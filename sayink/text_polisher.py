import ipaddress
import logging
import time
from typing import Callable
from urllib.parse import urlparse

from PyQt6.QtCore import QObject, pyqtSignal, QThread

log = logging.getLogger("SayInk")

# Local endpoints that are allowed to use plain HTTP (e.g. Ollama, LM Studio,
# litellm running on the same machine). Everything else must use HTTPS.
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}

INSECURE_URL_ERROR = (
    "安全错误: 远程 API URL 必须使用 HTTPS 协议"
    "（本机或局域网地址，如 localhost、192.168.x.x，可用 HTTP）"
)


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


def is_secure_or_local_url(url: str) -> bool:
    """Allow HTTPS anywhere; plain HTTP only for this machine or the home/office LAN.

    Covers local OpenAI-compatible servers such as Ollama on
    ``http://localhost:11434/v1`` or on another PC at ``http://192.168.1.20:11434/v1``.
    Public addresses must use HTTPS because the API key travels in the header.
    """
    url = (url or "").strip()
    if url.startswith("https://"):
        return True
    if url.startswith("http://"):
        return is_local_endpoint(url)
    return False


def api_key_required(url: str) -> bool:
    """Local servers such as Ollama often run without authentication."""
    return not is_local_endpoint(url)


def polish_settings_complete(api_url: str, api_key: str, model_name: str) -> bool:
    if not (api_url or "").strip() or not (model_name or "").strip():
        return False
    return bool((api_key or "").strip()) or not api_key_required(api_url)


def _request_headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def chat_completions_url(api_url: str) -> str:
    url = (api_url or "").rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    return url


# One extra attempt for connection-level failures and gateway errors.
# Timeouts are deliberately not retried: the caller's budget is already the
# longest acceptable wait, and it falls back (raw text / error) anyway.
CHAT_RETRY_STATUS = frozenset({502, 503, 504})
CHAT_RETRY_DELAY_SEC = 0.5


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
) -> str:
    """One OpenAI-compatible chat completion, shared by polishing and minutes.

    POSTs once and retries once after ``retry_delay_sec`` on a transient
    network error or a 502/503/504. Returns the stripped message content.
    Raises :class:`LLMCancelled` when ``is_cancelled()`` turns true,
    :class:`LLMResponseError` for malformed/empty answers, and lets
    ``httpx`` exceptions (timeout, HTTP status) propagate.
    """
    cancelled = is_cancelled or (lambda: False)
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
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
    data = response.json()
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


def build_system_prompt(
    mode: str = LLM_MODE_POLISH,
    *,
    custom_prompt: str = "",
) -> str:
    """Return the system prompt for polish mode."""
    custom = (custom_prompt or "").strip()
    return custom or POLISH_PROMPT


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
    ):
        super().__init__()
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self._text = text
        self._mode = (mode or LLM_MODE_POLISH).strip().lower()
        self._prompt = prompt or POLISH_PROMPT
        self._cancelled = False
        self._action_label = "润色"

    def cancel(self):
        """Set cancellation flag to stop the worker."""
        self._cancelled = True

    def run(self):
        if self._cancelled:
            return

        try:
            import httpx

            # HTTPS 安全检查（本地 localhost 端点允许 HTTP，兼容 Ollama 等）
            if not is_secure_or_local_url(self._api_url):
                log.warning("API URL 未使用 HTTPS 且非本地端点，存在安全风险")
                self.error.emit(INSECURE_URL_ERROR)
                return

            log.info("正在调用 LLM %s (%s)...", self._action_label, self._model_name)
            log.debug("原文长度: %d 字符", len(self._text))

            with httpx.Client(timeout=15.0) as client:
                polished = chat_completion(
                    client, httpx,
                    api_url=self._api_url,
                    api_key=self._api_key,
                    model_name=self._model_name,
                    messages=[
                        {"role": "system", "content": self._prompt},
                        {"role": "user", "content": self._text},
                    ],
                    temperature=0.3,
                    max_tokens=2048,
                    is_cancelled=lambda: self._cancelled,
                    retry_delay_sec=self._RETRY_DELAY_SEC,
                    label=self._action_label,
                )
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

    def polish(
        self,
        text: str,
        api_url: str,
        api_key: str,
        model_name: str,
        prompt: str = "",
        *,
        mode: str = LLM_MODE_POLISH,
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
        )
        worker.result_ready.connect(self._on_result)
        worker.error.connect(self._on_error)
        if hasattr(worker, "finished"):
            worker.finished.connect(lambda w=worker: self._reap_worker(w))
        self._worker = worker
        self._workers.append(worker)
        worker.start()

    def _reap_worker(self, worker: PolishWorker) -> None:
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
        self.polish_complete.emit(polished_text)

    def _on_error(self, error_msg: str):
        self.polish_error.emit(error_msg)

    @staticmethod
    def test_connection(api_url: str, api_key: str, model_name: str) -> tuple[bool, str]:
        # HTTPS 安全检查（本地 localhost 端点允许 HTTP，兼容 Ollama 等）
        if not is_secure_or_local_url(api_url):
            return False, INSECURE_URL_ERROR

        try:
            import httpx

            url = chat_completions_url(api_url)
            headers = _request_headers(api_key)

            payload = {
                "model": model_name,
                "messages": [
                    {"role": "user", "content": "Hello"}
                ],
                "max_tokens": 5
            }

            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()

            return True, "连接成功"
        except httpx.TimeoutException:
            return False, "连接超时"
        except httpx.HTTPStatusError as e:
            return False, f"HTTP 错误: {e.response.status_code}"
        except Exception as e:
            return False, f"连接失败: {str(e)}"
