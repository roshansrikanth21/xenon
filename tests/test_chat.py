import json
import os
import unittest

from xenon.chat import ChatConfig, ChatUnavailable, OpenAICompatibleChat, config_from_env

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

        with self.assertRaises(ChatUnavailable):
            client.complete(MESSAGES)

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


if __name__ == "__main__":
    unittest.main()
