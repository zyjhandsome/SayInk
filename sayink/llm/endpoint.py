"""Endpoint security validation, proxy bypass resolution, and request header/URL builders."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import ssl
from typing import Any
from urllib.parse import ParseResult, urlparse, urlunparse

log = logging.getLogger("SayInk")

_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_TAILSCALE_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

INSECURE_URL_ERROR = "接口地址须以 http:// 或 https:// 开头，并包含主机名"
INVALID_IPV6_ERROR = "IPv6 地址不完整。请写成 http://[::1]:端口/路径，方括号要成对。"
INVALID_PORT_ERROR = "端口必须是 1 到 65535 的数字。"
PUBLIC_HTTP_ERROR = (
    "公网地址请使用 https://，避免明文传输密钥和转写内容；"
    "内网域名若确需 http://，请加入「额外直连域名」。"
)
API_ADDRESS_HINT = (
    "公网地址必须使用 https://。本机、局域网，以及已加入「额外直连域名」的内网，可以使用 http://。"
)
FULLWIDTH_URL_ERROR = "接口地址含有全角冒号或斜杠，请改用半角的 http:// 或 https://，并包含主机名"
HOST_GARBLED_ERROR = "接口地址的主机名含有空格或不可见字符。"
URL_USERINFO_ERROR = "接口地址里的账号或密钥已去掉，请把密钥填在「API 密钥」。"
PUBLIC_HTTP_NOTE = "该地址使用明文 HTTP，密钥和转写内容会明文传输。"

CHAT_RETRY_STATUS = frozenset({502, 503, 504})
CHAT_RETRY_DELAY_SEC = 0.5
STREAM_CONNECT_TIMEOUT = 10.0
STREAM_READ_TIMEOUT = 120.0
STREAM_TOTAL_TIMEOUT = 600.0

_INVISIBLE_CHARS = "\u200b\u200c\u200d\ufeff\u2060\u00ad"
_INVISIBLE_TRANS = dict.fromkeys(map(ord, _INVISIBLE_CHARS), None)
_DASH_CHARS = ("－", "—", "–", "‑", "−")
_CANONICAL_MODELS = {
    "deepseek-flash": "deepseek-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
}
_THINKING_TOGGLE_HOST = "api.deepseek.com"
_THINKING_TOGGLE_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})

_SUFFIX_SPLIT = re.compile(r"[\s,，;；]+")
_PUBLIC_MULTI_LABEL_TLDS = frozenset(
    {
        "com.cn", "org.cn", "net.cn", "gov.cn", "edu.cn", "ac.cn", "mil.cn",
        "co.uk", "org.uk", "ac.uk", "gov.uk", "net.uk", "me.uk",
        "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "ed.jp", "gr.jp",
        "com.hk", "org.hk", "net.hk", "edu.hk", "gov.hk",
        "com.tw", "org.tw", "net.tw", "edu.tw", "gov.tw",
        "com.sg", "org.sg", "net.sg", "edu.sg", "gov.sg",
        "co.kr", "or.kr", "ne.kr", "ac.kr", "go.kr",
        "com.au", "org.au", "net.au", "edu.au", "gov.au",
        "co.nz", "org.nz", "net.nz",
        "co.in", "org.in", "net.in", "ac.in", "gov.in",
        "com.br", "org.br", "net.br", "gov.br", "edu.br",
        "co.za", "org.za", "net.za", "gov.za", "ac.za",
    }
)


def host_is_clean(host: str) -> bool:
    return bool(host) and not any(ch.isspace() or ch in _INVISIBLE_CHARS for ch in host)


def url_parts(url: str) -> tuple[ParseResult | None, str, str | None]:
    """Return ``(parsed, host, error)`` with safe IPv6 and port parsing."""
    text = (url or "").strip()
    try:
        parsed = urlparse(text)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        message = str(exc)
        if "IPv6" in message:
            return None, "", INVALID_IPV6_ERROR
        if "port" in message.lower():
            return None, "", INVALID_PORT_ERROR
        return None, "", INSECURE_URL_ERROR
    if port is not None and not 1 <= int(port) <= 65535:
        return None, "", INVALID_PORT_ERROR
    return parsed, host, None


def request_netloc(parsed: ParseResult) -> str:
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        return f"{host}:{parsed.port}"
    return host


def is_local_endpoint(url: str) -> bool:
    """True for localhost, private/LAN IPs, Tailscale CGNAT, ``*.local``, or single-label hosts."""
    _parsed, host, error = url_parts(url)
    if error:
        return False
    cleaned = host.strip("[]").lower()
    if not host_is_clean(cleaned):
        return False
    if cleaned in _LOCAL_HOSTNAMES or cleaned.endswith(".local") or ("." not in cleaned and ":" not in cleaned):
        return True
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _TAILSCALE_CGNAT_NET)
    )


def parse_proxy_bypass_suffixes(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split proxy-bypass suffixes into ``(valid_suffixes, ignored_tokens)``."""
    valid: list[str] = []
    ignored: list[str] = []
    for token in _SUFFIX_SPLIT.split(text or ""):
        token = token.strip()
        if not token:
            continue
        suffix = token[1:] if token.startswith(".") else token
        lowered = suffix.lower()
        parts = lowered.split(".") if lowered else []
        if (
            len(parts) < 2
            or any(not part for part in parts)
            or any(char in token for char in "*/:@\\")
            or lowered in _PUBLIC_MULTI_LABEL_TLDS
        ):
            ignored.append(token)
            continue
        valid.append(lowered)
    return tuple(valid), tuple(ignored)


def host_matches_suffix(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def matches_bypass_suffixes(url: str, suffixes: str) -> bool:
    if not suffixes:
        return False
    _parsed, host, error = url_parts(url)
    if error:
        return False
    cleaned = host.strip("[]").lower()
    if not host_is_clean(cleaned):
        return False
    valid, _ignored = parse_proxy_bypass_suffixes(suffixes)
    return any(host_matches_suffix(cleaned, suffix) for suffix in valid)


def is_secure_or_local_url(url: str, *, proxy_bypass_suffixes: str = "") -> bool:
    """Allow ``https://`` anywhere, and ``http://`` only on local/LAN or bypassed intranet hosts."""
    parsed, host, error = url_parts(url)
    if error or parsed is None:
        return False
    if parsed.scheme not in {"http", "https"} or not host_is_clean(host):
        return False
    if parsed.scheme == "https":
        return True
    return is_local_endpoint(url) or matches_bypass_suffixes(url, proxy_bypass_suffixes)


def api_url_error(url: str, *, proxy_bypass_suffixes: str = "") -> str | None:
    """User-facing reason the address cannot be used, or ``None`` when valid."""
    text = (url or "").strip()
    parsed, host, error = url_parts(text)
    if error:
        return error
    if is_secure_or_local_url(text, proxy_bypass_suffixes=proxy_bypass_suffixes):
        return None
    if parsed is not None and parsed.scheme in {"http", "https"} and host and not host_is_clean(host):
        return HOST_GARBLED_ERROR
    if parsed is not None and parsed.scheme == "http" and host_is_clean(host):
        return PUBLIC_HTTP_ERROR
    if "：" in text or "／" in text:
        return FULLWIDTH_URL_ERROR
    return INSECURE_URL_ERROR


def redact_url_userinfo(url: str) -> tuple[str, bool]:
    """Drop ``user:password@`` so credentials are never persisted in URLs."""
    raw = (url or "").strip()
    parsed, host, error = url_parts(raw)
    if error or parsed is None or not host or (parsed.username is None and parsed.password is None):
        return raw, False
    cleaned = urlunparse((parsed.scheme, request_netloc(parsed), parsed.path, "", parsed.query, parsed.fragment))
    return cleaned, True


def public_cleartext_note(url: str) -> str:
    parsed, _host, error = url_parts(url)
    if error or parsed is None:
        return ""
    if parsed.scheme == "http" and not is_local_endpoint(url):
        return PUBLIC_HTTP_NOTE
    return ""


def normalize_api_key(api_key: str) -> str:
    """Strip a pasted ``Bearer`` prefix, wrapping quotes, and interior whitespace."""
    key = (api_key or "").strip().translate(_INVISIBLE_TRANS)
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'“”‘’":
        key = key[1:-1].strip()
    if key[:7].lower() == "bearer ":
        key = key[7:].strip()
    return "".join(ch for ch in key if not ch.isspace() and ch not in _INVISIBLE_CHARS)


def normalize_model_name(model_name: str) -> str:
    """Drop invisible characters and canonicalize known DeepSeek dual-mode ids."""
    text = (model_name or "").replace("\u3000", " ").translate(_INVISIBLE_TRANS)
    for dash in _DASH_CHARS:
        text = text.replace(dash, "-")
    text = " ".join(text.split())
    key = "-".join(text.casefold().replace("_", " ").replace("-", " ").split())
    return _CANONICAL_MODELS.get(key, text)


def api_key_required(url: str) -> bool:
    return not is_local_endpoint(url)


def polish_settings_complete(api_url: str, api_key: str, model_name: str) -> bool:
    if not (api_url or "").strip() or not (model_name or "").strip():
        return False
    return bool((api_key or "").strip()) or not api_key_required(api_url)


def request_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = normalize_api_key(api_key)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def chat_completions_url(api_url: str) -> str:
    """OpenAI-compatible chat completions URL preserving query strings and stripping userinfo/fragments."""
    raw = (api_url or "").strip()
    parsed, host, error = url_parts(raw)
    if error or parsed is None or parsed.scheme not in {"http", "https"} or not host_is_clean(host):
        url = raw.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        return url
    path = (parsed.path or "").rstrip("/")
    if not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions" if path else "/chat/completions"
    return urlunparse((parsed.scheme, request_netloc(parsed), path, "", parsed.query, ""))


def thinking_payload_extra(api_url: str, model_name: str, thinking_mode: str | None) -> dict[str, Any]:
    """Extra request fields disabling reasoning on official DeepSeek dual-mode endpoints."""
    _parsed, host, error = url_parts(api_url)
    if (
        not error
        and thinking_mode == "disabled"
        and host.strip("[]").lower() == _THINKING_TOGGLE_HOST
        and normalize_model_name(model_name) in _THINKING_TOGGLE_MODELS
    ):
        return {"thinking": {"type": "disabled"}}
    return {}


def http_stream_timeout(httpx_mod: Any) -> Any:
    return httpx_mod.Timeout(
        connect=STREAM_CONNECT_TIMEOUT,
        read=STREAM_READ_TIMEOUT,
        write=30.0,
        pool=10.0,
    )


def bypasses_env_proxy(mode: str, url: str = "", suffixes: str = "") -> bool:
    norm_mode = (mode or "").strip().lower()
    if norm_mode in ("direct", "none", "off", "bypass") or is_local_endpoint(url):
        return True
    _parsed, raw_host, error = url_parts(url)
    if error:
        return False
    host = raw_host.strip("[]").lower()
    if not host:
        return False
    valid, _ignored = parse_proxy_bypass_suffixes(suffixes)
    return any(host_matches_suffix(host, suffix) for suffix in valid)


def http_client_options(mode: str, url: str = "", suffixes: str = "") -> dict[str, Any]:
    """Disable environment proxies for direct or excepted hosts while retaining custom SSL CAs."""
    if not bypasses_env_proxy(mode, url, suffixes):
        return {"trust_env": True}
    options: dict[str, Any] = {"trust_env": False}
    cafile = (os.environ.get("SSL_CERT_FILE") or "").strip()
    capath = (os.environ.get("SSL_CERT_DIR") or "").strip()
    ssl_kwargs: dict[str, str] = {}
    if cafile:
        ssl_kwargs["cafile"] = cafile
    if capath:
        ssl_kwargs["capath"] = capath
    if ssl_kwargs:
        try:
            options["verify"] = ssl.create_default_context(**ssl_kwargs)
        except (OSError, ssl.SSLError) as exc:
            log.warning("自定义 CA 证书加载失败（%s），回退系统证书库", exc)
            options["verify"] = ssl.create_default_context()
    return options
