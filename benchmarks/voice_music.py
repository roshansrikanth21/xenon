"""Can Xenon hear commands over music? (`python -m benchmarks.voice_music`)

Builds a hostile background in memory, a second SAPI voice reading lyrics
non-stop plus chords and a kick drum, and mixes it under spoken commands at
a chosen level. The stream runs through the same endpointer, Whisper, wake
gate and parsers as the orb. When the gate arms (Xenon heard its name), the
background drops to the ducked level, as the real ducker does to other apps.

Compares the old fixed-threshold endpointer with the adaptive one.
"""
from __future__ import annotations

import argparse
import array
import math
import os
import random
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import win32com.client  # noqa: E402

from xenon import brightness, fastpath, music, quick  # noqa: E402
from xenon.audio import UtteranceDetector, WhisperTranscriber  # noqa: E402
from xenon.duck import DUCK_TO  # noqa: E402
from xenon.wake import WakeGate  # noqa: E402

RATE = 16000
FRAME = 480  # 30 ms
LYRICS = (
    "Baby I've been waiting all night long, under city lights we sing our song, "
    "hold me closer, never let me go, dancing in the rain, moving fast and slow. "
)
CASES = [
    ("Xenon, skip this song.", ("music", "next")),
    ("Pause the music, Xenon.", ("music", "pause")),
    ("Xenon, turn it down.", ("quick", "volume_down")),
    ("Hey Xenon, what time is it?", ("quick", "time")),
    ("Xenon, play my liked songs.", ("music", "liked")),
    ("Can you lower my brightness, Xenon?", ("brightness", "down")),
]


def speak(text: str, path: str, voice_index: int = 0, rate: int = 0) -> list[int]:
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
    fmt.Type = 18  # 16 kHz 16-bit mono
    stream.Format = fmt
    stream.Open(path, 3)
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    voices = voice.GetVoices()
    if voices.Count > voice_index:
        voice.Voice = voices.Item(voice_index)
    voice.Rate = rate
    voice.AudioOutputStream = stream
    voice.Speak(text)
    stream.Close()
    with wave.open(path, "rb") as handle:
        return list(array.array("h", handle.readframes(handle.getnframes())))


def background(seconds: float, lyrics: list[int]) -> list[float]:
    """Lyrics + chords + kick drum, normalised to RMS 1.0."""
    total = int(seconds * RATE)
    out = [0.0] * total
    chords = [(220.0, 277.2, 329.6), (196.0, 246.9, 293.7), (174.6, 220.0, 261.6), (196.0, 246.9, 311.1)]
    for i in range(total):
        t = i / RATE
        chord = chords[int(t / 1.0) % len(chords)]
        tone = sum(math.sin(math.tau * f * t) for f in chord) / 3
        beat = t % 0.5
        kick = math.sin(math.tau * 60 * beat) * math.exp(-beat * 18)
        vocal = lyrics[i % len(lyrics)] / 32768.0 * 3.0
        out[i] = 0.35 * tone + 0.6 * kick + vocal
    level = math.sqrt(sum(v * v for v in out) / total) or 1.0
    return [v / level for v in out]


def classify(command: str | None):
    if command is None:
        return ("ignored", "")
    for name, parser in (("quick", quick.parse), ("brightness", brightness.parse),
                         ("music", music.parse), ("app", fastpath.parse)):
        parsed = parser(command)
        if parsed is not None:
            return (name, getattr(parsed, "kind", None) or getattr(parsed, "action", ""))
    return ("model", command)


def understand(command, chat):
    """What the orb does: parsers first, then the model's reading."""
    if command is None or chat is None or classify(command)[0] not in {"model"}:
        return command
    from xenon import router
    from xenon.__main__ import _recognised

    return router.route(command, chat.complete, _recognised) or command


def run_case(sentence, transcriber, bed, music_level, adaptive, folder, index):
    voice = speak(sentence, os.path.join(folder, f"cmd{index}.wav"))
    voice_rms = math.sqrt(sum(v * v for v in voice) / len(voice)) / 32768.0
    # The music is already playing before you speak, as in real life.
    lead, tail = int(4.0 * RATE), int(2.5 * RATE)
    clip = [0] * lead + voice + [0] * tail
    detector = UtteranceDetector(threshold=0.015, silence_ms=350, min_speech_ms=200, adaptive=adaptive)
    clock = {"t": 0.0}
    gate = WakeGate("xenon", aliases=("zenon",), clock=lambda: clock["t"])
    offset = random.randint(0, len(bed) - len(clip) - 1)
    heard, command, ducked = [], None, False
    for start in range(0, len(clip) - FRAME, FRAME):
        clock["t"] += FRAME / RATE
        gain = music_level * voice_rms * (DUCK_TO if ducked else 1.0)
        frame = [
            max(-1.0, min(1.0, clip[start + k] / 32768.0 + bed[offset + start + k] * gain))
            for k in range(FRAME)
        ]
        pcm = detector.feed(frame)
        if pcm is None:
            continue
        text = (transcriber.transcribe(pcm, RATE) or "").strip()
        heard.append(text)
        command = gate.feed(text)
        if command is not None:
            break
        if gate.armed:
            ducked = True  # the real ducker lowers other apps here
            detector.reset()
    return heard, command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--music", type=float, default=0.7,
                        help="music loudness relative to the voice (RMS ratio)")
    args = parser.parse_args()
    random.seed(7)
    folder = tempfile.mkdtemp(prefix="xenon-music-")
    lyrics = speak(LYRICS * 3, os.path.join(folder, "lyrics.wav"), voice_index=1, rate=1)
    bed = background(60, lyrics)
    transcriber = WhisperTranscriber("base.en", "cpu", "int8", initial_prompt="Hey Xenon.", beam_size=1)
    chat = None
    try:
        from xenon.chat import OpenAICompatibleChat, config_from_env

        config = config_from_env()
        chat = OpenAICompatibleChat(config) if config else None
    except Exception:
        chat = None
    print("model cleanup:", "on" if chat else "off (no key)")
    print(f"music at {args.music:.2f}x the voice level\n")
    scores = {}
    for adaptive in (False, True):
        name = "adaptive" if adaptive else "fixed threshold"
        ok = 0
        for index, (sentence, expected) in enumerate(CASES):
            heard, command = run_case(sentence, transcriber, bed, args.music, adaptive, folder, index)
            got = classify(understand(command, chat))
            good = got == expected
            ok += good
            print(f"[{name:15}] {'PASS' if good else 'FAIL'} {sentence!r}\n"
                  f"{'':18}heard={' | '.join(heard)[:110]!r} -> {got}")
        scores[name] = ok
        print()
    for name, ok in scores.items():
        print(f"{name}: {ok}/{len(CASES)}")
    return 0 if scores["adaptive"] == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
