import json
import os
import unittest

from xenon.chat import (
    DEFAULT_MAX_TOKENS,
    ChatConfig,
    ChatUnavailable,
    OpenAICompatibleChat,
    config_from_env,
    is_local_base_url,
)

MESSAGES = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "what is on screen?"},
]


class FakeTransport:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response or {
            "choices": [{"message": {"content": "a slide about AI"}}]
        }
        self.error = error
        self.requests: list[tuple[str, dict, dict, float]] = []

    def __call__(self, url: str, headers: dict, body: bytes, timeout: float) -> dict:
        self.requests.append((url, headers, json.loads(body), timeout))
        if self.error is not None:
            raise self.error
        return self.response


class ChatClientTests(unittest.TestCase):
    def test_posts_messages_to_the_chat_completions_endpoint(self) -> None:
        transport = FakeTransport()
        client = OpenAICompatibleChat(
            ChatConfig("https://models.example/v1", "chat-small", "test-key"),
            transport=transport,
        )

        text = client.complete(MESSAGES)

        self.assertEqual(text, "a slide about AI")
        url, headers, payload, timeout = transport.requests[0]
        self.assertEqual(url, "https://models.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(payload["model"], "chat-small")
        self.assertEqual(payload["messages"], MESSAGES)
        self.assertLessEqual(timeout, 120)

    def test_wraps_transport_failures(self) -> None:
        client = OpenAICompatibleChat(
            ChatConfig("https://models.example/v1", "chat-small", "k"),
            transport=FakeTransport(error=TimeoutError("slow")),
        )

        with self.assertRaises(ChatUnavailable) as raised:
            client.complete(MESSAGES)

        message = str(raised.exception)
        self.assertIn("https://models.example/v1", message)
        self.assertIn("doctor", message)
        self.assertIn("slow", message)

    def test_caps_generation_and_does_not_send_ollama_knobs_to_a_cloud_endpoint(
        self,
    ) -> None:
        transport = FakeTransport()
        client = OpenAICompatibleChat(
            ChatConfig("https://models.example/v1", "chat-small", "k"),
            transport=transport,
        )

        client.complete(MESSAGES)

        payload = transport.requests[0][2]
        self.assertEqual(payload["max_tokens"], DEFAULT_MAX_TOKENS)
        self.assertNotIn("keep_alive", payload)

    def test_sends_keep_alive_only_when_configured(self) -> None:
        transport = FakeTransport()
        client = OpenAICompatibleChat(
            ChatConfig(
                "http://127.0.0.1:11434/v1",
                "qwen2.5:7b",
                "ollama",
                keep_alive="2h",
            ),
            transport=transport,
        )

        client.complete(MESSAGES)

        self.assertEqual(transport.requests[0][2]["keep_alive"], "2h")

    def test_warm_up_loads_the_model_and_reports_failure_quietly(self) -> None:
        transport = FakeTransport()
        client = OpenAICompatibleChat(
            ChatConfig("https://models.example/v1", "chat-small", "k"),
            transport=transport,
        )
        self.assertTrue(client.warm_up())
        self.assertEqual(len(transport.requests), 1)

        offline = OpenAICompatibleChat(
            ChatConfig("https://models.example/v1", "chat-small", "k"),
            transport=FakeTransport(error=TimeoutError("down")),
        )
        self.assertFalse(offline.warm_up())

    def test_rejects_empty_responses(self) -> None:
        client = OpenAICompatibleChat(
            ChatConfig("https://models.example/v1", "chat-small", "k"),
            transport=FakeTransport(response={"choices": [{"message": {"content": ""}}]}),
        )

        with self.assertRaises(ChatUnavailable):
            client.complete(MESSAGES)

    def test_config_from_env_is_opt_in(self) -> None:
        keys = ["XENON_MODEL_API_KEY", "XENON_MODEL_NAME", "XENON_MODEL_BASE_URL"]
        original = {key: os.environ.pop(key, None) for key in keys}
        try:
            self.assertIsNone(config_from_env())
            os.environ["XENON_MODEL_API_KEY"] = "k"
            os.environ["XENON_MODEL_NAME"] = "chat-small"
            config = config_from_env()
            self.assertIsNotNone(config)
            self.assertEqual(config.model, "chat-small")
            self.assertEqual(config.base_url, "https://api.openai.com/v1")
        finally:
            for key in keys:
                os.environ.pop(key, None)
                if original[key] is not None:
                    os.environ[key] = original[key]

    def test_local_endpoints_get_a_keep_alive_and_a_token_cap(self) -> None:
        keys = [
            "XENON_MODEL_API_KEY",
            "XENON_MODEL_NAME",
            "XENON_MODEL_BASE_URL",
            "XENON_MAX_TOKENS",
            "XENON_KEEP_ALIVE",
        ]
        original = {key: os.environ.pop(key, None) for key in keys}
        try:
            os.environ["XENON_MODEL_API_KEY"] = "ollama"
            os.environ["XENON_MODEL_NAME"] = "qwen2.5:7b"
            os.environ["XENON_MODEL_BASE_URL"] = "http://127.0.0.1:11434/v1"

            config = config_from_env()

            self.assertIsNotNone(config)
            self.assertEqual(config.max_tokens, DEFAULT_MAX_TOKENS)
            self.assertEqual(config.keep_alive, "2h")
            self.assertTrue(is_local_base_url(config.base_url))

            os.environ["XENON_MAX_TOKENS"] = "160"
            os.environ["XENON_KEEP_ALIVE"] = "off"
            config = config_from_env()
            self.assertEqual(config.max_tokens, 160)
            self.assertEqual(config.keep_alive, "")
        finally:
            for key in keys:
                os.environ.pop(key, None)
                if original[key] is not None:
                    os.environ[key] = original[key]

    def test_cloud_endpoints_never_get_a_keep_alive(self) -> None:
        keys = ["XENON_MODEL_API_KEY", "XENON_MODEL_NAME", "XENON_MODEL_BASE_URL"]
        original = {key: os.environ.pop(key, None) for key in keys}
        try:
            os.environ["XENON_MODEL_API_KEY"] = "k"
            os.environ["XENON_MODEL_NAME"] = "chat-small"
            os.environ["XENON_MODEL_BASE_URL"] = "https://models.example/v1"

            config = config_from_env()

            self.assertIsNotNone(config)
            self.assertEqual(config.keep_alive, "")
            self.assertFalse(is_local_base_url(config.base_url))
        finally:
            for key in keys:
                os.environ.pop(key, None)
                if original[key] is not None:
                    os.environ[key] = original[key]


if __name__ == "__main__":
    unittest.main()
