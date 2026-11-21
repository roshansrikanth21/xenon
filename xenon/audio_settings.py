"""Which microphone Xenon listens to, and which speaker it talks through.

Saved in ``%APPDATA%\\xenon\\audio.json`` so the choice survives restarts.
Environment variables still win for one-off runs:

    XENON_MIC_DEVICE=<index or name>    input
    XENON_TTS_OUTPUT=<name substring>   output

Names are stored, not indices: Windows renumbers devices when something is
plugged in, but the friendly name usually survives.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass


def settings_path() -> str:
    return os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "xenon", "audio.json"
    )


@dataclass
class AudioSettings:
    input: str = ""
    output: str = ""


def load(path: str | None = None) -> AudioSettings:
    path = path or settings_path()
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return AudioSettings()
    if not isinstance(raw, dict):
        return AudioSettings()
    return AudioSettings(
        input=str(raw.get("input") or "").strip(),
        output=str(raw.get("output") or "").strip(),
    )


def save(settings: AudioSettings, path: str | None = None) -> str:
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(asdict(settings), handle, indent=2)
    return path


def input_devices() -> list[tuple[int, str]]:
    """Every device that can record, as ``(index, name)``."""
    try:
        import sounddevice as sd
    except Exception:
        return []
    try:
        return [
            (int(index), str(device.get("name") or index))
            for index, device in enumerate(sd.query_devices())
            if int(device.get("max_input_channels", 0)) > 0
        ]
    except Exception:
        return []


def output_devices() -> list[str]:
    """Every SAPI output Xenon can speak through."""
    try:
        from xenon.audio import list_outputs

        return list_outputs()
    except Exception:
        return []


def resolve_input(selector, devices: list[tuple[int, str]] | None = None) -> int | None:
    """Turn an index or a friendly-name fragment into a device index."""
    if selector is None:
        return None
    text = str(selector).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    if devices is None:
        devices = input_devices()
    wanted = text.lower()
    for index, name in devices:
        if wanted in name.lower():
            return index
    return None


def describe_input(index: int | None) -> str:
    for device_index, name in input_devices():
        if device_index == index:
            return name
    return ""


def current_input() -> int | None:
    """Environment first, then the saved setting, then the system default."""
    selector = os.environ.get("XENON_MIC_DEVICE") or load().input
    return resolve_input(selector)


def default_input() -> tuple[int | None, str]:
    """What sounddevice would use with no selection at all."""
    try:
        import sounddevice as sd

        device = sd.query_devices(kind="input")
        return int(device.get("index")), str(device.get("name") or "")
    except Exception:
        return None, ""


def current_output() -> str:
    """Environment first, then the saved setting; empty means SAPI default."""
    return os.environ.get("XENON_TTS_OUTPUT") or load().output


def test(input_selector=None, output_selector=None, seconds: float = 0.6) -> tuple[str, float | None]:
    """Speak through the chosen output and record the chosen input.

    Returns ``(spoken_text, peak)``; peak is None when the microphone could not
    be opened. Blocks for a couple of seconds, so call it from a worker thread
    in a UI.
    """
    from xenon.audio import MicrophoneSource, SapiSpeaker

    speaker = SapiSpeaker(output=None if output_selector is None else str(output_selector))
    speaker.speak("Xenon audio test. You should hear this through the selected speaker.")

    peak = None
    source = MicrophoneSource(device=resolve_input(input_selector))
    try:
        deadline = time.monotonic() + max(0.2, seconds)
        samples: list[float] = []
        while time.monotonic() < deadline:
            samples.extend(source.read_frame())
        peak = max((abs(value) for value in samples), default=0.0)
    except Exception:
        peak = None
    finally:
        source.close()
    return "Xenon audio test.", peak
