import unittest

from xenon.providers import (
    PROVIDER_PRESETS,
    key_is_set,
    settings_from_env,
)


class ProviderConfigTests(unittest.TestCase):
    def test_explicit_values_still_work(self) -> None:
        settings = settings_from_env(
            {
                "XENON_MODEL_API_KEY": "key",
                "XENON_MODEL_NAME": "custom-model",
                "XENON_MODEL_BASE_URL": "https://example.com/v1",
            }
        )

        self.assertIsNotNone(settings)
        self.assertEqual(settings.provider, "custom")
        self.assertEqual(settings.model, "custom-model")

    def test_preset_fills_base_url_and_model(self) -> None:
        settings = settings_from_env(
            {"XENON_MODEL_PROVIDER": "groq", "GROQ_API_KEY": "gsk_123"}
        )

        self.assertIsNotNone(settings)
        self.assertEqual(settings.provider, "groq")
        self.assertEqual(settings.base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(settings.model, "openai/gpt-oss-20b")
        self.assertEqual(settings.api_key, "gsk_123")

    def test_detects_a_key_without_a_provider(self) -> None:
        settings = settings_from_env({"GEMINI_API_KEY": "AIza123"})

        self.assertIsNotNone(settings)
        self.assertEqual(settings.provider, "gemini")
        self.assertEqual(settings.model, "gemini-3.8-flash")

    def test_ollama_needs_no_key(self) -> None:
        settings = settings_from_env({"XENON_MODEL_PROVIDER": "ollama"})

        self.assertIsNotNone(settings)
        self.assertEqual(settings.api_key, "ollama")
        self.assertEqual(settings.base_url, "http://127.0.0.1:11434/v1")

    def test_gemini_accepts_the_google_key_name(self) -> None:
        settings = settings_from_env(
            {"XENON_MODEL_PROVIDER": "gemini", "GOOGLE_API_KEY": "AIza123"}
        )

        self.assertIsNotNone(settings)
        self.assertEqual(settings.api_key, "AIza123")

    def test_no_configuration_returns_none(self) -> None:
        self.assertIsNone(settings_from_env({}))
        self.assertIsNone(
            settings_from_env({"XENON_MODEL_PROVIDER": "groq"})
        )

    def test_explicit_model_overrides_the_preset_default(self) -> None:
        settings = settings_from_env(
            {
                "XENON_MODEL_PROVIDER": "cerebras",
                "CEREBRAS_API_KEY": "sk-x",
                "XENON_MODEL_NAME": "llama-3.3-70b",
            }
        )

        self.assertIsNotNone(settings)
        self.assertEqual(settings.model, "llama-3.3-70b")
        self.assertEqual(settings.base_url, "https://api.cerebras.ai/v1")

    def test_key_is_set_reports_each_preset(self) -> None:
        self.assertTrue(
            key_is_set(PROVIDER_PRESETS["groq"], {"GROQ_API_KEY": "x"})
        )
        self.assertFalse(key_is_set(PROVIDER_PRESETS["groq"], {}))
        self.assertTrue(key_is_set(PROVIDER_PRESETS["ollama"], {}))


if __name__ == "__main__":
    unittest.main()
