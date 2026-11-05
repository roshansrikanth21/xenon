"""OpenAI-compatible chat client used by the assist loop.

Shares the transport shape with ``xenon.vision`` so both can point at the same
local server. Tests inject a transport; production uses urllib.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Callable

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_S = 60.0

Transport = Callable[[str, dict, bytes, float], dict]


class ChatUnavailable(RuntimeError):
    """Raised when the model cannot produce an answer."""


@dataclass(frozen=True)
class ChatConfig:
    base_url: str
    model: str
    api_key: str
    timeout_s: float = DEFAULT_TIMEOUT_S


def config_from_env() -> ChatConfig | None:
    api_key = os.environ.get("XENON_MODEL_API_KEY", "")
    model = os.environ.get("XENON_MODEL_NAME", "")
    if not api_key or not model:
        return None
    try:
        timeout = float(os.environ.get("XENON_MODEL_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    return ChatConfig(
        base_url=os.environ.get("XENON_MODEL_BASE_URL", DEFAULT_BASE_URL),
        model=model,
        api_key=api_key,
        timeout_s=timeout,
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
            "max_tokens": 400,
        }
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
            raise ChatUnavailable(str(error)) from error

        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ChatUnavailable("chat response had no content") from error
        if not isinstance(content, str) or not content.strip():
            raise ChatUnavailable("chat response was empty")
        return content.strip()
