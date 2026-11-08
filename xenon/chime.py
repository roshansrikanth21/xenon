"""Short audible cues for the voice loop.

"wake" is a soft rising two-note chime when Xenon hears its name and is ready
for a command; "heard" is a single high blip when a command was caught. Both
are 120-160 ms, shorter than the endpointer's minimum speech length, so the
microphone never mistakes them for a command.

Played asynchronously through ``winsound`` from tiny WAV files generated on
first use. ``XENON_CHIME=off`` silences them.
"""
from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import wave

SAMPLE_RATE = 22050
# (frequency Hz, duration s) notes per cue.
CUES: dict[str, tuple[tuple[float, float], ...]] = {
    "wake": ((660.0, 0.07), (988.0, 0.09)),
    "heard": ((1175.0, 0.08),),
    # Timer done: a bright three-note figure, played three times.
    "timer": ((784.0, 0.12), (988.0, 0.12), (1319.0, 0.22), (0.0, 0.18)) * 3,
}


def enabled() -> bool:
    return os.environ.get("XENON_CHIME", "on").strip().lower() not in {"0", "off", "false", "no"}


def synthesize(notes: tuple[tuple[float, float], ...], volume: float = 0.22) -> bytes:
    """16-bit mono PCM for the notes, with a quick fade so nothing clicks."""
    frames = bytearray()
    for frequency, duration in notes:
        count = int(SAMPLE_RATE * duration)
        fade = max(1, int(SAMPLE_RATE * 0.012))
        for index in range(count):
            envelope = min(1.0, index / fade, (count - index) / fade)
            # A touch of the octave makes it sound like a chime, not a beep.
            if frequency <= 0:  # a rest
                frames += struct.pack("<h", 0)
                continue
            phase = math.tau * frequency * index / SAMPLE_RATE
            value = math.sin(phase) * 0.8 + math.sin(phase * 2) * 0.2
            frames += struct.pack("<h", int(value * envelope * volume * 32767))
    return bytes(frames)


def _path(name: str) -> str:
    folder = os.path.join(tempfile.gettempdir(), "xenon-chimes")
    path = os.path.join(folder, f"{name}.wav")
    if not os.path.exists(path):
        os.makedirs(folder, exist_ok=True)
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(synthesize(CUES[name]))
    return path


def play(name: str) -> None:
    """Play a cue without blocking; silently does nothing off Windows."""
    if name not in CUES or not enabled() or sys.platform != "win32":
        return
    try:
        import winsound

        winsound.PlaySound(_path(name), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass
