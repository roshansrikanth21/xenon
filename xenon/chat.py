"""OpenAI-compatible chat client used by the assist loop.

Shares the transport shape with ``xenon.vision`` so both can point at the same
local server. Tests inject a transport; production uses urllib.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_S = 60.0
# Spoken and task-first answers fit comfortably in a few hundred tokens.
# Letting a local 7B model ramble for 400+ tokens costs seconds per answer.
DEFAULT_MAX_TOKENS = 220
# Keep local models loaded between requests; the first request after an idle
# unload costs ~16 s, every warm request costs ~1 s.
LOCAL_KEEP_ALIVE = "2h"

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
    api_key = os.environ.get("XENON_MODEL_API_KEY", "")
    model = os.environ.get("XENON_MODEL_NAME", "")
    if not api_key or not model:
        return None
    try:
        timeout = float(os.environ.get("XENON_MODEL_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    base_url = os.environ.get("XENON_MODEL_BASE_URL", DEFAULT_BASE_URL)
    return ChatConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_s=timeout,
        max_tokens=_max_tokens_from_env(),
        keep_alive=_keep_alive_from_env(base_url),
    )


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


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
        if self._config.keep_alive:
            # Ollama-specific; only sent to loopback endpoints. Without this the
            # model unloads after five idle minutes and the next request pays a
            # 15–16 s cold load.
            payload["keep_alive"] = self._config.keep_alive
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        url = self._config.base_url.rstrip("/") + "/chat/completions"
        try:
            response = self._transport(
                url,
                headers,
                json.dumps(payload).encode("utf-8"),
                self._config.timeout_s,
            )
        except Exception as error:
            raise ChatUnavailable(
                f"could not reach the model at {url}: {error}. Start the "
                "server (for example `ollama serve`) or run "
                "`python -m xenon doctor` to check the configuration."
            ) from error

        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ChatUnavailable("chat response had no content") from error
        if not isinstance(content, str) or not content.strip():
            raise ChatUnavailable("chat response was empty")
        return content.strip()

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
