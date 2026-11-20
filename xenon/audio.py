"""Audio adapters behind the voice loop: utterance detection, STT, and TTS.

``UtteranceDetector`` is pure logic and fully tested with synthetic frames. The
microphone, speech-to-text, and text-to-speech classes are thin wrappers so that
the expensive or hardware-bound parts stay injectable and testable.
"""
from __future__ import annotations

import array
import math
import os
import sys
from collections import deque
from typing import Callable, Iterable, Protocol, Sequence

SampleFrame = Sequence[float]


def _env_number(name: str, default, kind):
    try:
        return kind(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def rms(frame: SampleFrame) -> float:
    if not frame:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in frame) / len(frame))


def to_pcm16(frame: SampleFrame) -> array.array:
    clipped = array.array("h")
    for sample in frame:
        value = int(round(max(-1.0, min(1.0, sample)) * 32767))
        clipped.append(value)
    return clipped


class UtteranceDetector:
    """Energy-based endpointer: collects one spoken utterance at a time.

    Speech starts when a frame is louder than ``threshold``. The utterance ends
    after ``silence_ms`` of quiet, or at ``max_utterance_ms``. A pre-roll keeps
    the frames just before the onset so the first syllable is not clipped.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        threshold: float = 0.012,
        silence_ms: int = 350,
        min_speech_ms: int = 200,
        max_utterance_ms: int = 15000,
        pre_roll_ms: int = 300,
        adaptive: bool = False,
        start_ratio: float = 2.4,
        end_ratio: float = 1.6,
    ) -> None:
        """``adaptive`` tracks the background level (music, a fan, a TV) and
        only treats sound clearly louder than it as speech: speech starts at
        ``start_ratio`` x the floor and ends when it falls under ``end_ratio``
        x the floor. Without it, steady music never counts as silence, so an
        utterance never ends."""
        if frame_ms <= 0 or sample_rate <= 0:
            raise ValueError("frame_ms and sample_rate must be positive")

        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.threshold = threshold
        self._silence_frames = max(1, silence_ms // frame_ms)
        self._min_speech_frames = max(1, min_speech_ms // frame_ms)
        self._max_frames = max(1, max_utterance_ms // frame_ms)
        self._pre_roll: deque[SampleFrame] = deque(
            maxlen=max(1, pre_roll_ms // frame_ms)
        )
        self._utterance: list[SampleFrame] = []
        self._speech_frames = 0
        self._quiet_run = 0
        self._speaking = False
        self.adaptive = adaptive
        self.start_ratio = start_ratio
        self.end_ratio = end_ratio
        self.noise_floor = threshold / 2
        # Recent frame levels (about 3 s). The floor is a low percentile of
        # them, updated on every frame, even mid-utterance: speech has gaps
        # between words, so the percentile stays at the music's level while
        # someone talks, and steady music can never freeze it.
        self._levels: deque[float] = deque(maxlen=max(10, 3000 // frame_ms))

    @property
    def speaking(self) -> bool:
        return self._speaking

    def reset(self) -> None:
        self._pre_roll.clear()
        self._utterance = []
        self._speech_frames = 0
        self._quiet_run = 0
        self._speaking = False

    def _track_floor(self, level: float) -> None:
        self._levels.append(level)
        ordered = sorted(self._levels)
        self.noise_floor = ordered[int(len(ordered) * 0.2)]

    def feed(self, frame: SampleFrame) -> bytes | None:
        """Return completed 16-bit mono PCM for one utterance, or ``None``."""
        level = rms(frame)
        if self.adaptive:
            self._track_floor(level)
            ratio = self.end_ratio if self._speaking else self.start_ratio
            loud = level >= max(self.threshold, self.noise_floor * ratio)
        else:
            loud = level >= self.threshold

        if not self._speaking:
            if not loud:
                self._pre_roll.append(frame)
                return None
            self._speaking = True
            self._utterance = list(self._pre_roll)
            self._pre_roll.clear()
            self._utterance.append(frame)
            self._speech_frames = 1
            self._quiet_run = 0
            return None

        self._utterance.append(frame)
        if loud:
            self._speech_frames += 1
            self._quiet_run = 0
        else:
            self._quiet_run += 1

        finished = self._quiet_run >= self._silence_frames
        too_long = len(self._utterance) >= self._max_frames
        if not finished and not too_long:
            return None

        frames = self._utterance
        speech_frames = self._speech_frames
        self.reset()
        if speech_frames < self._min_speech_frames:
            return None
        return _join_pcm(frames)


def _join_pcm(frames: Iterable[SampleFrame]) -> bytes:
    joined = array.array("h")
    for frame in frames:
        joined.extend(to_pcm16(frame))
    return joined.tobytes()


def resample_pcm16(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Linearly resample 16-bit mono PCM. No-op when the rates already match.

    Whisper only accepts 16 kHz audio, and a microphone may not offer it, so the
    rate the device gave us must be handled here rather than silently ignored.
    """
    if from_rate == to_rate or from_rate <= 0 or to_rate <= 0 or not pcm:
        return pcm

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()

    count = len(samples)
    if count < 2:
        return pcm

    out_count = max(1, int(round(count * to_rate / from_rate)))
    out = array.array("h", [0]) * out_count
    step = (count - 1) / max(1, out_count - 1)
    for index in range(out_count):
        position = index * step
        left = int(position)
        right = min(left + 1, count - 1)
        weight = position - left
        out[index] = int(round(samples[left] * (1 - weight) + samples[right] * weight))

    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


class Transcriber(Protocol):
    def transcribe(self, pcm: bytes, sample_rate: int) -> str: ...


class Speaker(Protocol):
    def speak(self, text: str) -> None: ...


TARGET_SAMPLE_RATE = 16000


class WhisperTranscriber:
    """Local speech-to-text through faster-whisper.

    The model loads lazily on the first utterance so startup stays fast, and the
    constructor takes a factory so tests never touch the network or a real model.
    """

    def __init__(
        self,
        model_name: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
        initial_prompt: str = "",
        beam_size: int = 1,
        model_factory: Callable[[str, str, str], object] | None = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._initial_prompt = initial_prompt
        self._beam_size = max(1, int(beam_size))
        self._factory = model_factory or _default_whisper_factory
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            self._model = self._factory(
                self._model_name, self._device, self._compute_type
            )
        return self._model

    def warmup(self) -> None:
        """Load the model ahead of the first real utterance (no transcription)."""
        self._load()

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        model = self._load()
        audio = _pcm_to_float(resample_pcm16(pcm, sample_rate, TARGET_SAMPLE_RATE))
        options = {
            "language": self._language,
            "beam_size": self._beam_size,
            "vad_filter": False,
            "condition_on_previous_text": False,
        }
        # An initial prompt is what makes an invented wake word come back
        # spelled the way its owner says it ("Xenon", not "See none").
        if self._initial_prompt:
            options["initial_prompt"] = self._initial_prompt
        segments, _ = model.transcribe(audio, **options)
        return " ".join(segment.text.strip() for segment in segments).strip()


def _default_whisper_factory(name: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    return WhisperModel(name, device=device, compute_type=compute_type)


def _pcm_to_float(pcm: bytes):
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16)
    return (samples.astype(np.float32) / 32768.0)


# SAPI Speak flags (verified against the installed SAPI type library):
# 0 = speak synchronously, 1 = speak asynchronously, 3 = purge + asynchronous.
SAPI_ASYNC = 1
SAPI_PURGE = 3


class SapiSpeaker:
    """Windows speech synthesis through SAPI, with an injectable engine."""

    def __init__(self, rate: int | None = None, volume: int | None = None, engine=None) -> None:
        if sys.platform != "win32" and engine is None:
            raise RuntimeError("SAPI speech is Windows-only")
        self._engine = engine if engine is not None else _default_sapi_engine()
        rate = _env_number("XENON_TTS_RATE", 1, int) if rate is None else rate
        volume = _env_number("XENON_TTS_VOLUME", 100, int) if volume is None else volume
        self._volume = max(0, min(100, int(volume)))
        self._async_speaking = False
        try:
            self._engine.Rate = int(rate)
            self._engine.Volume = self._volume
            wanted = os.environ.get("XENON_TTS_VOICE", "").strip().lower()
            if wanted:
                for voice in self._engine.GetVoices():
                    if wanted in str(voice.GetAttribute("Name")).lower():
                        self._engine.Voice = voice
                        break
        except Exception:
            pass

    def speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        # 0 waits for playback to finish, which keeps the voice loop's mute
        # window aligned with the actual audio.
        self._engine.Speak(text, 0)

    def speak_async(self, text: str) -> None:
        """Start speaking without blocking, so the caller can listen for barge-in."""
        text = text.strip()
        if not text:
            return
        self._engine.Speak(text, SAPI_ASYNC)
        # Engines without playback feedback (test doubles) are done instantly.
        self._async_speaking = hasattr(self._engine, "WaitUntilDone")

    def stop(self) -> None:
        """Cut the current reply short (purge everything SAPI has queued)."""
        self._async_speaking = False
        self._engine.Speak("", SAPI_PURGE)

    def is_speaking(self) -> bool:
        """True while an asynchronous reply is still playing."""
        wait = getattr(self._engine, "WaitUntilDone", None)
        if wait is not None:
            try:
                return not bool(wait(0))
            except Exception:
                pass
        return self._async_speaking

    def set_volume(self, volume: int) -> None:
        """Change SAPI's own volume (0-100), used to duck for the user."""
        self._volume = int(max(0, min(100, volume)))
        try:
            self._engine.Volume = self._volume
        except Exception:
            pass

    @property
    def volume(self) -> int:
        return self._volume


class BargeInDetector:
    """Notice the user talking over Xenon's own voice.

    SAPI returns before the speakers finish, and a little of Xenon's own audio
    always leaks into the microphone. The first fraction of a reply is used to
    learn that leak; after that a sustained rise means the user has joined in.
    Stage 1 asks the caller to duck Xenon's volume, stage 2 asks it to stop and
    listen. Ambient noise is already quiet because ``duck`` lowered the music.
    """

    def __init__(self, floor: float = 0.015, ratio: float = 1.6,
                 stop_ratio: float = 2.4, need_ms: float = 180.0,
                 learn_ms: float = 350.0, frame_ms: float = 30.0,
                 max_leak: float = 0.15) -> None:
        self._floor = floor
        self._ratio = ratio
        self._stop_ratio = stop_ratio
        self._need = need_ms
        self._learn = learn_ms
        self._frame_ms = frame_ms
        self._max_leak = max_leak
        self._learned = 0.0
        self._leak = 0.0
        self._loud_ms = 0.0
        self._ducked = False
        self._duck_seen = False

    def feed(self, rms: float, frame_ms: float | None = None) -> int:
        """Return 0 (keep talking), 1 (duck Xenon), or 2 (stop and listen)."""
        ms = self._frame_ms if frame_ms is None else frame_ms
        level = max(0.0, float(rms))
        if self._learned < self._learn:
            self._learned += ms
            self._leak = max(self._leak, min(level, self._max_leak))
            return 0
        # Keep the learned leak as a slowly decaying reference: a loud user
        # must not raise it to their own level and hide the interruption.
        self._leak *= 0.999
        duck_at = max(self._floor * 3.0, self._leak * self._ratio)
        stop_at = max(self._floor * 4.0, self._leak * self._stop_ratio)
        if level >= stop_at:
            self._loud_ms += ms
        elif level >= duck_at:
            self._loud_ms += ms * 0.4     # talking, but not clearly over Xenon
        else:
            self._loud_ms = max(0.0, self._loud_ms - ms * 2)
            self._duck_seen = False
        if level < duck_at:
            return 0
        # Always duck first, even if the interruption is loud from the start:
        # the caller needs one frame to turn Xenon's own volume down.
        if not self._duck_seen or self._loud_ms < self._need:
            self._duck_seen = True
            return 1
        return 2


def list_voices() -> list[str]:
    """Names of the installed SAPI voices (for 'xenon voice --list-voices')."""
    try:
        engine = _default_sapi_engine()
        return [str(voice.GetAttribute("Name")) for voice in engine.GetVoices()]
    except Exception:
        return []


def _default_sapi_engine():
    import win32com.client

    return win32com.client.Dispatch("SAPI.SpVoice")


class MicrophoneSource:
    """Pulls fixed-size mono frames from the default input device."""

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        device: object = None,
        stream_factory: Callable[[int, int], object] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.device = device
        self._frame_samples = int(sample_rate * frame_ms / 1000)
        self._factory = stream_factory or _default_stream_factory
        self._stream = None

    def _open(self):
        if self._stream is None:
            # Custom factories keep the old two-argument contract; the default
            # factory also accepts a device selector.
            if self._factory is _default_stream_factory:
                self._stream = self._factory(
                    self.sample_rate, self._frame_samples, self.device
                )
            else:
                self._stream = self._factory(self.sample_rate, self._frame_samples)
        return self._stream

    def read_frame(self) -> list[float]:
        stream = self._open()
        data, overflowed = stream.read(self._frame_samples)
        if overflowed:
            pass
        return [float(value) for value in data.reshape(-1)]

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None


def _default_stream_factory(sample_rate: int, frame_samples: int, device=None):
    import sounddevice as sd

    kwargs = dict(
        samplerate=sample_rate,
        blocksize=frame_samples,
        channels=1,
        dtype="float32",
    )
    if device is not None:
        kwargs["device"] = device
    return sd.InputStream(**kwargs).__enter__()


def listen_once(source, detector: UtteranceDetector, transcriber: Transcriber) -> str | None:
    """Block until one utterance ends, then transcribe it.

    Returns ``None`` for silence or a transcript that came back empty, which is
    what the voice loop treats as "nothing was said".
    """
    while True:
        pcm = detector.feed(source.read_frame())
        if pcm is None:
            continue
        text = transcriber.transcribe(pcm, source.sample_rate)
        text = (text or "").strip()
        return text or None
