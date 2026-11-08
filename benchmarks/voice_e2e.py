"""End-to-end voice check without a microphone (`python -m benchmarks.voice_e2e`).

Windows SAPI speaks each sentence into a 16 kHz WAV. The audio is then fed in
30 ms frames through the same pieces the orb uses: the energy endpointer,
faster-whisper, the wake gate, and the music/app parsers. Nothing is played
aloud and no command is executed.
"""
import os
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import win32com.client  # noqa: E402

from xenon import fastpath, music, quick  # noqa: E402
from xenon.audio import UtteranceDetector, WhisperTranscriber  # noqa: E402
from xenon.wake import WakeGate  # noqa: E402

SAFT16kHz16BitMono = 18
SSFMCreateForWrite = 3

CASES = [
    # (spoken sentence, expected kind, expected detail)
    ("Play my liked songs, Xenon.", "music", "liked"),
    ("Xenon, play my liked songs.", "music", "liked"),
    ("Hey Xenon, skip this song.", "music", "next"),
    ("Pause the music, Xenon.", "music", "pause"),
    ("Xenon, open notepad.", "app", "open_app"),
    ("Can you open calculator, Xenon?", "app", "open_app"),
    ("Xenon, what's playing?", "music", "now"),
    ("What time is it, Xenon?", "quick", "time"),
    ("Xenon, set a timer for five minutes.", "quick", "timer"),
    ("Turn it up, Xenon.", "quick", "volume_up"),
    ("Play my liked songs.", "ignored", ""),       # no wake word
    ("The weather is nice today.", "ignored", ""),  # background talk
]


def speak_to_wav(text: str, path: str) -> None:
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
    fmt.Type = SAFT16kHz16BitMono
    stream.Format = fmt
    stream.Open(path, SSFMCreateForWrite)
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    voice.AudioOutputStream = stream
    voice.Speak(text)
    stream.Close()


def frames(path: str, frame_samples: int = 480):
    with wave.open(path, "rb") as handle:
        assert handle.getframerate() == 16000 and handle.getsampwidth() == 2
        raw = handle.readframes(handle.getnframes())
    import array

    samples = array.array("h", raw)
    # Lead-in and trailing silence, like a real room between sentences.
    padded = [0] * 4800 + list(samples) + [0] * 16000
    for start in range(0, len(padded) - frame_samples, frame_samples):
        yield [value / 32768.0 for value in padded[start:start + frame_samples]]


def classify(command: str | None) -> tuple[str, str]:
    if command is None:
        return "ignored", ""
    fast_quick = quick.parse(command)
    if fast_quick is not None:
        return "quick", fast_quick.kind
    parsed = music.parse(command)
    if parsed is not None:
        return "music", parsed.kind
    fast = fastpath.parse(command)
    if fast is not None:
        return "app", fast.kind
    return "model", command


def main() -> int:
    wake_word = "xenon"
    aliases = ("zenon", "see none")
    from xenon.__main__ import _whisper_hint

    transcriber = WhisperTranscriber(
        "base.en", "cpu", "int8", initial_prompt=_whisper_hint(wake_word), beam_size=1
    )
    failures = 0
    folder = tempfile.mkdtemp(prefix="xenon-voice-")
    for index, (sentence, kind, detail) in enumerate(CASES):
        path = os.path.join(folder, f"{index}.wav")
        speak_to_wav(sentence, path)
        detector = UtteranceDetector(threshold=0.015, silence_ms=350, min_speech_ms=200)
        # Audio time drives the gate's clock, as wall time would live.
        clock = {"t": 0.0}
        gate = WakeGate(wake_word, aliases=aliases, always=False, clock=lambda: clock["t"])
        pieces, command = [], None
        for frame in frames(path):
            clock["t"] += 0.03
            pcm = detector.feed(frame)
            if pcm is None:
                continue
            text = (transcriber.transcribe(pcm, 16000) or "").strip()
            pieces.append(text)
            command = gate.feed(text)
            if command is not None:
                break
        heard = " | ".join(pieces)
        got = classify(command)
        ok = got == (kind, detail)
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  said={sentence!r}\n      heard={heard!r} -> command={command!r} -> {got}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
