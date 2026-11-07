"""OpenAI-compatible chat client used by the assist loop.

Shares the transport shape with ``xenon.vision`` so both can point at the same
local server. Tests inject a transport; production uses urllib.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from xenon.providers import settings_from_env

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_S = 60.0
# Spoken and task-first answers fit comfortably in a few hundred tokens.
# Letting a local 7B model ramble for 400+ tokens costs seconds per answer.
DEFAULT_MAX_TOKENS = 220
# Keep local models loaded between requests; the first request after an idle
# unload costs ~16 s, every warm request costs ~1 s.
LOCAL_KEEP_ALIVE = "2h"
# Reasoning models count hidden thinking tokens against max_tokens. This extra
# headroom is only added when a reasoning effort is sent, so a short answer
# cap cannot be eaten entirely by thinking. Answer length is still held down
# by the prompt.
REASONING_HEADROOM_TOKENS = 1024
# Free tiers rate limit hard. One short retry hides a burst limit; anything
# longer is reported instead of making a voice user wait.
MAX_RETRY_AFTER_S = 5.0
USER_AGENT = "xenon-assistant/0.1"

Transport = Callable[[str, dict, bytes, float], dict]


class ChatUnavailable(RuntimeError):
    """Raised when the model cannot produce an answer."""


@dataclass(frozen=True)
class ChatConfig:
    base_url: str
    model: str
    api_key: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    keep_alive: str = ""
    reasoning_effort: str = ""
    provider: str = "custom"


def is_local_base_url(base_url: str) -> bool:
    """True for loopback endpoints, where Ollama-style knobs are safe to send."""
    host = urllib.parse.urlparse(base_url).hostname or ""
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _max_tokens_from_env() -> int:
    raw = os.environ.get("XENON_MAX_TOKENS", "")
    if not raw:
        return DEFAULT_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_TOKENS
    return min(max(value, 32), 4096)


def _keep_alive_from_env(base_url: str) -> str:
    if not is_local_base_url(base_url):
        return ""
    raw = os.environ.get("XENON_KEEP_ALIVE")
    if raw is None:
        return LOCAL_KEEP_ALIVE
    value = raw.strip()
    if value.lower() in {"", "0", "off", "false", "no"}:
        return ""
    return value


def config_from_env() -> ChatConfig | None:
    settings = settings_from_env()
    if settings is None:
        return None
    try:
        timeout = float(os.environ.get("XENON_MODEL_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    return ChatConfig(
        base_url=settings.base_url,
        model=settings.model,
        api_key=settings.api_key,
        timeout_s=timeout,
        max_tokens=_max_tokens_from_env(),
        keep_alive=_keep_alive_from_env(settings.base_url),
        reasoning_effort=settings.reasoning_effort,
        provider=settings.provider,
    )


class HTTPStatusError(RuntimeError):
    """A non-2xx reply, with the provider's error text and Retry-After."""

    def __init__(self, status: int, detail: str, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status}: {detail}" if detail else f"HTTP {status}")
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


def _error_detail(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:300]
    # OpenAI-style {"error": {"message": ...}}; Gemini sometimes wraps a list.
    if isinstance(data, list) and data:
        data = data[0]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:300]
    if isinstance(error, str):
        return error[:300]
    return text[:300]


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = _error_detail(error.read() or b"")
        raise HTTPStatusError(
            error.code, detail, _retry_after(error.headers.get("Retry-After"))
        ) from error


def _explain_http(error: HTTPStatusError, url: str, config: ChatConfig) -> str:
    if error.status in (401, 403):
        return (
            f"the model at {url} rejected the API key ({error}). Check the key "
            "for this provider; `python -m xenon providers` shows which is used."
        )
    if error.status == 429:
        return (
            f"the {config.provider} free tier is rate limiting requests ({error}). "
            "Wait a moment, or switch provider with XENON_MODEL_PROVIDER."
        )
    if error.status == 404:
        return (
            f"model {config.model!r} was not found at {url} ({error}). Set "
            "XENON_MODEL_NAME to a model the provider lists."
        )
    if error.status == 400 and config.reasoning_effort:
        return (
            f"the model rejected the request ({error}). If it mentions "
            "reasoning, set XENON_REASONING_EFFORT=off."
        )
    return f"the model at {url} returned an error: {error}"


class OpenAICompatibleChat:
    """Implements ``assistant.ChatModel`` against a chat-completions API."""

    def __init__(self, config: ChatConfig, transport: Transport | None = None) -> None:
        self._config = config
        self._transport = transport if transport is not None else _urllib_transport

    def complete(self, messages: list[dict]) -> str:
        payload = {
            "model": self._config.model,
            "messages": messages,
            "temperature": 0.2,
            # ``max_tokens`` is understood by Ollama, LM Studio, vLLM and the
            # OpenAI chat models. The newer ``max_completion_tokens`` spelling
            # is ignored by local servers, which lets a 7B model ramble for
            # half a minute on a long screen dump.
            "max_tokens": self._config.max_tokens,
        }
        if self._config.reasoning_effort:
            payload["reasoning_effort"] = self._config.reasoning_effort
            payload["max_tokens"] = (
                self._config.max_tokens + REASONING_HEADROOM_TOKENS
            )
        if self._config.keep_alive:
            # Ollama-specific; only sent to loopback endpoints. Without this the
            # model unloads after five idle minutes and the next request pays a
            # 15–16 s cold load.
            payload["keep_alive"] = self._config.keep_alive
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
            # Some providers sit behind bot filters that refuse urllib's default.
            "User-Agent": USER_AGENT,
        }
        url = self._config.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        try:
            response = self._send(url, headers, body)
        except HTTPStatusError as error:
            raise ChatUnavailable(_explain_http(error, url, self._config)) from error
        except Exception as error:
            raise ChatUnavailable(
                f"could not reach the model at {url}: {error}. Start the "
                "server (for example `ollama serve`) or run "
                "`python -m xenon doctor` to check the configuration."
            ) from error

        try:
            choice = response["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ChatUnavailable("chat response had no content") from error
        if not isinstance(content, str) or not content.strip():
            if choice.get("finish_reason") == "length":
                raise ChatUnavailable(
                    "the model used its whole token budget before answering "
                    "(likely hidden reasoning). Raise XENON_MAX_TOKENS or set "
                    "XENON_REASONING_EFFORT=low."
                )
            raise ChatUnavailable("chat response was empty")
        return content.strip()

    def _send(self, url: str, headers: dict, body: bytes) -> dict:
        try:
            return self._transport(url, headers, body, self._config.timeout_s)
        except HTTPStatusError as error:
            wait = error.retry_after if error.retry_after is not None else 1.0
            if error.status != 429 or wait > MAX_RETRY_AFTER_S:
                raise
            time.sleep(wait)
            return self._transport(url, headers, body, self._config.timeout_s)

    def warm_up(self) -> bool:
        """Load the model now so the first real question is not a cold start.

        Returns ``False`` when the endpoint is unreachable; callers should keep
        going and let the real request report the error.
        """
        try:
            self.complete(
                [{"role": "user", "content": "Reply with the single word: ready"}]
            )
        except ChatUnavailable:
            return False
        return True
