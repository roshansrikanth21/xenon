"""Free and local model-provider presets.

Xenon speaks the OpenAI chat-completions protocol, so switching providers is
mostly a base URL, a model name, and a key. These presets turn one environment
variable into a working configuration and let ``xenon providers`` show which
free option is ready to use.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    base_url: str
    model: str
    key_envs: tuple[str, ...]
    note: str
    free: bool = True
    signup: str = ""


# Model names are the provider's current free-tier defaults at the time this
# was written. Any OpenAI-compatible model can be selected explicitly with
# XENON_MODEL_NAME, so a renamed or retired default does not lock anyone in.
PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "groq": ProviderPreset(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        model="openai/gpt-oss-20b",
        key_envs=("GROQ_API_KEY", "XENON_MODEL_API_KEY"),
        note="Fastest free tier for voice; low time-to-first-token.",
        signup="https://console.groq.com/keys",
    ),
    "gemini": ProviderPreset(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-3.8-flash",
        key_envs=("GEMINI_API_KEY", "GOOGLE_API_KEY", "XENON_MODEL_API_KEY"),
        note="Strong free model and vision; generous developer tier.",
        signup="https://aistudio.google.com/apikey",
    ),
    "cerebras": ProviderPreset(
        name="cerebras",
        base_url="https://api.cerebras.ai/v1",
        model="qwen-3.8-27b",
        key_envs=("CEREBRAS_API_KEY", "XENON_MODEL_API_KEY"),
        note="Very fast inference; free tier with rate limits.",
        signup="https://cloud.cerebras.ai",
    ),
    "openrouter": ProviderPreset(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="meta-llama/llama-3.3-70b-instruct:free",
        key_envs=("OPENROUTER_API_KEY", "XENON_MODEL_API_KEY"),
        note="One key for many models; the ':free' variants are rate limited.",
        signup="https://openrouter.ai/keys",
    ),
    "ollama": ProviderPreset(
        name="ollama",
        base_url="http://127.0.0.1:11434/v1",
        model="qwen2.5:7b",
        key_envs=(),
        note="Fully local through Ollama; no key and no network required.",
        signup="https://ollama.com/download",
    ),
}


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    model: str
    api_key: str
    provider: str = "custom"


def settings_from_env(env: Mapping[str, str] | None = None) -> ModelSettings | None:
    """Build model settings from explicit values, a preset, or a detected key."""
    values = os.environ if env is None else env
    provider_name = values.get("XENON_MODEL_PROVIDER", "").strip().lower()
    api_key = values.get("XENON_MODEL_API_KEY", "").strip()
    model = values.get("XENON_MODEL_NAME", "").strip()
    base_url = values.get("XENON_MODEL_BASE_URL", "").strip()

    if not provider_name and not api_key:
        # A user who only exports GROQ_API_KEY (or GEMINI_API_KEY, etc.) should
        # not also have to name the provider.
        for name, preset in PROVIDER_PRESETS.items():
            if name == "ollama":
                continue
            if any(values.get(key, "").strip() for key in preset.key_envs):
                provider_name = name
                break

    if not provider_name:
        if api_key and model:
            return ModelSettings(
                base_url or DEFAULT_OPENAI_BASE_URL, model, api_key, "custom"
            )
        return None

    preset = PROVIDER_PRESETS.get(provider_name)
    if preset is None:
        # An unlisted OpenAI-compatible endpoint is fine when fully specified.
        if api_key and model:
            return ModelSettings(
                base_url or DEFAULT_OPENAI_BASE_URL, model, api_key, provider_name
            )
        return None

    if not api_key:
        for key in preset.key_envs:
            api_key = values.get(key, "").strip()
            if api_key:
                break
    if not api_key and preset.name == "ollama":
        # Ollama ignores the key but the client still needs a non-empty value.
        api_key = "ollama"
    if not model:
        model = preset.model
    if not base_url:
        base_url = preset.base_url
    if not api_key or not model:
        return None
    return ModelSettings(base_url, model, api_key, preset.name)


def key_is_set(preset: ProviderPreset, env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    if preset.name == "ollama":
        return True
    return any(values.get(key, "").strip() for key in preset.key_envs)
