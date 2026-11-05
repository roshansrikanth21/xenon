"""Vision fallback for screens whose UI Automation tree is not informative.

The client speaks the OpenAI-compatible chat-completions shape so it can point
at a cloud endpoint or a local vision server. It is opt-in: without an API key
Xenon stays on the UIA path.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

from xenon.screen import CapturedFrame, WindowSnapshot

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_S = 20.0

PROMPT = (
    "You are looking at the user's active screen for an assistant that answers "
    "questions about it. Describe the visible content and transcribe the "
    "important text, in reading order. Skip chrome such as browser toolbars. "
    "Never transcribe masked password fields; call them a password field. "
    "Keep it under 200 words."
)


class VisionUnavailable(RuntimeError):
    """Raised when no frame or no usable vision response is available."""


@dataclass(frozen=True)
class VisionConfig:
    base_url: str
    model: str
    api_key: str
    timeout_s: float = DEFAULT_TIMEOUT_S


Transport = Callable[[str, dict, bytes, float], dict]


def config_from_env() -> VisionConfig | None:
    """Return a vision config only when vision is deliberately switched on.

    The chat model's credentials are *not* enough on their own: doing that
    silently sends an Ollama key to OpenAI whenever the text model is set, and
    a failed vision call then looks like a broken screen. Set at least one
    ``XENON_VISION_*`` variable to opt in. Shared credentials and base URL are
    inherited from the model environment when present, so a local vision model
    on the same server needs only ``XENON_VISION_MODEL``.
    """
    vision_key = os.environ.get("XENON_VISION_API_KEY", "")
    vision_model = os.environ.get("XENON_VISION_MODEL", "")
    if not vision_key and not vision_model:
        return None

    api_key = vision_key or os.environ.get("XENON_MODEL_API_KEY", "")
    model = vision_model or os.environ.get("XENON_MODEL_NAME", "")
    base_url = (
        os.environ.get("XENON_VISION_BASE_URL")
        or os.environ.get("XENON_MODEL_BASE_URL")
        or DEFAULT_BASE_URL
    )
    if not api_key or not model:
        return None
    try:
        timeout = float(os.environ.get("XENON_VISION_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    return VisionConfig(base_url=base_url, model=model, api_key=api_key, timeout_s=timeout)


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class OpenAICompatibleVision:
    """Implements ``screen.VisionProvider`` against a chat-completions API."""

    def __init__(
        self, config: VisionConfig, transport: Transport | None = None
    ) -> None:
        self._config = config
        self._transport = transport if transport is not None else _urllib_transport

    def describe(
        self, frame: CapturedFrame | None, snapshot: WindowSnapshot
    ) -> str:
        if frame is None:
            raise VisionUnavailable("no screenshot was captured")
        if not frame.image:
            raise VisionUnavailable("screenshot was empty")

        payload = {
            "model": self._config.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{PROMPT}\n\nActive window: {snapshot.app_name} — {snapshot.title}",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(frame.image).decode("ascii"),
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            "max_completion_tokens": 500,
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
            raise VisionUnavailable(str(error)) from error

        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise VisionUnavailable("vision response had no content") from error
        if not isinstance(content, str) or not content.strip():
            raise VisionUnavailable("vision response was empty")
        return content.strip()
