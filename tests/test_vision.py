import base64
import json
import os
import unittest

from xenon.screen import CapturedFrame, WindowSnapshot
from xenon.vision import (
    OpenAICompatibleVision,
    VisionConfig,
    VisionUnavailable,
    config_from_env,
)

SNAPSHOT = WindowSnapshot(app_name="opera", title="deck.pptx", controls=())
FRAME = CapturedFrame(image=b"\x89PNG\r\n\x1a\npayload", width=800, height=600)


class FakeTransport:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response or {
            "choices": [{"message": {"content": "Slide 2: what is AI?"}}]
        }
        self.error = error
        self.requests: list[tuple[str, dict, bytes, float]] = []

    def __call__(self, url: str, headers: dict, body: bytes, timeout: float) -> dict:
        self.requests.append((url, headers, body, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class VisionClientTests(unittest.TestCase):
    def test_sends_base64_frame_and_screen_context(self) -> None:
        transport = FakeTransport()
        client = OpenAICompatibleVision(
            VisionConfig(
                base_url="https://models.example/v1",
                model="vision-small",
                api_key="test-key",
            ),
            transport=transport,
        )

        text = client.describe(FRAME, SNAPSHOT)

        self.assertEqual(text, "Slide 2: what is AI?")
        url, headers, body, timeout = transport.requests[0]
        payload = json.loads(body)
        self.assertEqual(url, "https://models.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(payload["model"], "vision-small")
        image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))
        self.assertEqual(
            base64.b64decode(image_url.split(",", 1)[1]), FRAME.image
        )
        self.assertIn("deck.pptx", payload["messages"][0]["content"][0]["text"])
        self.assertLessEqual(timeout, 30)

    def test_requires_a_captured_frame(self) -> None:
        client = OpenAICompatibleVision(
            VisionConfig("https://models.example/v1", "vision-small", "k"),
            transport=FakeTransport(),
        )

        with self.assertRaises(VisionUnavailable):
            client.describe(None, SNAPSHOT)

    def test_wraps_transport_failures(self) -> None:
        client = OpenAICompatibleVision(
            VisionConfig("https://models.example/v1", "vision-small", "k"),
            transport=FakeTransport(error=TimeoutError("slow")),
        )

        with self.assertRaises(VisionUnavailable):
            client.describe(FRAME, SNAPSHOT)

    def test_rejects_a_response_without_content(self) -> None:
        client = OpenAICompatibleVision(
            VisionConfig("https://models.example/v1", "vision-small", "k"),
            transport=FakeTransport(response={"choices": []}),
        )

        with self.assertRaises(VisionUnavailable):
            client.describe(FRAME, SNAPSHOT)

    def test_config_from_env_is_opt_in(self) -> None:
        keys = [
            "XENON_VISION_API_KEY",
            "XENON_MODEL_API_KEY",
            "XENON_MODEL_NAME",
            "XENON_MODEL_BASE_URL",
            "XENON_VISION_MODEL",
            "XENON_VISION_BASE_URL",
        ]
        original = {key: os.environ.pop(key, None) for key in keys}
        try:
            self.assertIsNone(config_from_env())
            os.environ["XENON_VISION_API_KEY"] = "k"
            os.environ["XENON_VISION_MODEL"] = "vision-small"
            config = config_from_env()
            self.assertIsNotNone(config)
            self.assertEqual(config.model, "vision-small")
            self.assertEqual(config.base_url, "https://api.openai.com/v1")
        finally:
            for key in keys:
                os.environ.pop(key, None)
                if original[key] is not None:
                    os.environ[key] = original[key]

    def test_model_credentials_alone_do_not_enable_vision(self) -> None:
        keys = [
            "XENON_VISION_API_KEY",
            "XENON_VISION_MODEL",
            "XENON_MODEL_API_KEY",
            "XENON_MODEL_NAME",
        ]
        original = {key: os.environ.pop(key, None) for key in keys}
        try:
            os.environ["XENON_MODEL_API_KEY"] = "ollama"
            os.environ["XENON_MODEL_NAME"] = "qwen2.5:7b"
            self.assertIsNone(config_from_env())
        finally:
            for key in keys:
                os.environ.pop(key, None)
                if original[key] is not None:
                    os.environ[key] = original[key]

    def test_vision_inherits_the_local_model_endpoint(self) -> None:
        keys = [
            "XENON_VISION_API_KEY",
            "XENON_VISION_MODEL",
            "XENON_VISION_BASE_URL",
            "XENON_MODEL_API_KEY",
            "XENON_MODEL_BASE_URL",
        ]
        original = {key: os.environ.pop(key, None) for key in keys}
        try:
            os.environ["XENON_MODEL_API_KEY"] = "ollama"
            os.environ["XENON_MODEL_BASE_URL"] = "http://127.0.0.1:11434/v1"
            os.environ["XENON_VISION_MODEL"] = "llava"
            config = config_from_env()
            self.assertIsNotNone(config)
            self.assertEqual(config.api_key, "ollama")
            self.assertEqual(config.base_url, "http://127.0.0.1:11434/v1")
        finally:
            for key in keys:
                os.environ.pop(key, None)
                if original[key] is not None:
                    os.environ[key] = original[key]


if __name__ == "__main__":
    unittest.main()
