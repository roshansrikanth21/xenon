import json
import unittest
from unittest import mock

from xenon import chat
from xenon.chat import (
    REASONING_HEADROOM_TOKENS,
    ChatConfig,
    ChatUnavailable,
    HTTPStatusError,
    OpenAICompatibleChat,
    _error_detail,
)
from xenon.providers import settings_from_env

MESSAGES = [{"role": "user", "content": "hi"}]
OK = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}


class ScriptedTransport:
    """Returns or raises each scripted item in order."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.payloads: list[dict] = []
        self.headers: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.payloads.append(json.loads(body))
        self.headers.append(headers)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def cloud(**overrides) -> ChatConfig:
    values = dict(
        base_url="https://api.groq.com/openai/v1",
        model="openai/gpt-oss-20b",
        api_key="k",
        max_tokens=220,
        provider="groq",
    )
    values.update(overrides)
    return ChatConfig(**values)


class ReasoningPresetTests(unittest.TestCase):
    def test_groq_and_gemini_defaults_ask_for_low_reasoning(self) -> None:
        groq = settings_from_env({"GROQ_API_KEY": "g"})
        gemini = settings_from_env({"GEMINI_API_KEY": "a"})

        self.assertEqual(groq.reasoning_effort, "low")
        self.assertEqual(gemini.reasoning_effort, "low")

    def test_local_and_custom_models_send_no_reasoning_field(self) -> None:
        self.assertEqual(
            settings_from_env({"XENON_MODEL_PROVIDER": "ollama"}).reasoning_effort, ""
        )
        custom = settings_from_env(
            {"XENON_MODEL_API_KEY": "k", "XENON_MODEL_NAME": "m"}
        )
        self.assertEqual(custom.reasoning_effort, "")

    def test_a_non_default_model_on_a_preset_drops_the_effort(self) -> None:
        settings = settings_from_env(
            {"GROQ_API_KEY": "g", "XENON_MODEL_NAME": "llama-3.3-70b-versatile"}
        )
        self.assertEqual(settings.reasoning_effort, "")

    def test_env_can_override_or_disable_the_effort(self) -> None:
        env = {"GROQ_API_KEY": "g"}
        self.assertEqual(
            settings_from_env({**env, "XENON_REASONING_EFFORT": "medium"}).reasoning_effort,
            "medium",
        )
        self.assertEqual(
            settings_from_env({**env, "XENON_REASONING_EFFORT": "off"}).reasoning_effort,
            "",
        )


class ReasoningPayloadTests(unittest.TestCase):
    def test_reasoning_models_get_effort_and_token_headroom(self) -> None:
        transport = ScriptedTransport(OK)
        OpenAICompatibleChat(cloud(reasoning_effort="low"), transport).complete(MESSAGES)

        payload = transport.payloads[0]
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["max_tokens"], 220 + REASONING_HEADROOM_TOKENS)

    def test_plain_models_keep_the_short_cap(self) -> None:
        transport = ScriptedTransport(OK)
        OpenAICompatibleChat(cloud(), transport).complete(MESSAGES)

        self.assertNotIn("reasoning_effort", transport.payloads[0])
        self.assertEqual(transport.payloads[0]["max_tokens"], 220)

    def test_sends_a_user_agent(self) -> None:
        transport = ScriptedTransport(OK)
        OpenAICompatibleChat(cloud(), transport).complete(MESSAGES)
        self.assertIn("xenon", transport.headers[0]["User-Agent"])

    def test_budget_spent_on_thinking_is_explained(self) -> None:
        empty = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        client = OpenAICompatibleChat(cloud(), ScriptedTransport(empty))

        with self.assertRaises(ChatUnavailable) as caught:
            client.complete(MESSAGES)
        self.assertIn("XENON_MAX_TOKENS", str(caught.exception))


class HTTPErrorTests(unittest.TestCase):
    def test_a_short_rate_limit_is_retried_once(self) -> None:
        transport = ScriptedTransport(HTTPStatusError(429, "slow down", 0.0), OK)
        with mock.patch.object(chat.time, "sleep") as sleep:
            answer = OpenAICompatibleChat(cloud(), transport).complete(MESSAGES)

        self.assertEqual(answer, "hello")
        self.assertEqual(len(transport.payloads), 2)
        sleep.assert_called_once_with(0.0)

    def test_a_long_rate_limit_is_reported_not_waited_on(self) -> None:
        transport = ScriptedTransport(HTTPStatusError(429, "daily cap", 3600.0))
        with mock.patch.object(chat.time, "sleep") as sleep:
            with self.assertRaises(ChatUnavailable) as caught:
                OpenAICompatibleChat(cloud(), transport).complete(MESSAGES)

        sleep.assert_not_called()
        self.assertIn("rate limiting", str(caught.exception))
        self.assertIn("groq", str(caught.exception))

    def test_bad_key_and_unknown_model_are_named(self) -> None:
        for status, expected in ((401, "API key"), (404, "XENON_MODEL_NAME")):
            with self.subTest(status=status):
                transport = ScriptedTransport(HTTPStatusError(status, "nope", None))
                with self.assertRaises(ChatUnavailable) as caught:
                    OpenAICompatibleChat(cloud(), transport).complete(MESSAGES)
                self.assertIn(expected, str(caught.exception))

    def test_error_detail_reads_openai_and_gemini_shapes(self) -> None:
        self.assertEqual(
            _error_detail(b'{"error": {"message": "bad model"}}'), "bad model"
        )
        self.assertEqual(
            _error_detail(b'[{"error": {"message": "quota"}}]'), "quota"
        )
        self.assertEqual(_error_detail(b"plain text"), "plain text")


if __name__ == "__main__":
    unittest.main()
