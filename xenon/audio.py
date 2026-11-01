"""Audio adapters behind the voice loop: utterance detection, STT, and TTS.

``UtteranceDetector`` is pure logic and fully tested with synthetic frames. The
microphone, speech-to-text, and text-to-speech classes are thin wrappers so that
the expensive or hardware-bound parts stay injectable and testable.
"""
from __future__ import annotations

import array
import math
import sys
from collections import deque
from typing import Callable, Iterable, Protocol, Sequence

SampleFrame = Sequence[float]


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
        silence_ms: int = 700,
        min_speech_ms: int = 250,
        max_utterance_ms: int = 15000,
        pre_roll_ms: int = 300,
    ) -> None:
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

    @property
    def speaking(self) -> bool:
        return self._speaking

    def reset(self) -> None:
        self._pre_roll.clear()
        self._utterance = []
        self._speech_frames = 0
        self._quiet_run = 0
        self._speaking = False

    def feed(self, frame: SampleFrame) -> bytes | None:
        """Return completed 16-bit mono PCM for one utterance, or ``None``."""
        loud = rms(frame) >= self.threshold

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


class Transcriber(Protocol):
    def transcribe(self, pcm: bytes, sample_rate: int) -> str: ...


class Speaker(Protocol):
    def speak(self, text: str) -> None: ...


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
        model_factory: Callable[[str, str, str], object] | None = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._initial_prompt = initial_prompt
        self._factory = model_factory or _default_whisper_factory
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            self._model = self._factory(
                self._model_name, self._device, self._compute_type
            )
        return self._model

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        model = self._load()
        audio = _pcm_to_float(pcm)
        options = {
            "language": self._language,
            "beam_size": 1,
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


class SapiSpeaker:
    """Windows speech synthesis through SAPI, with an injectable engine."""

    def __init__(self, rate: int = 1, volume: int = 100, engine=None) -> None:
        if sys.platform != "win32" and engine is None:
            raise RuntimeError("SAPI speech is Windows-only")
        self._engine = engine if engine is not None else _default_sapi_engine()
        try:
            self._engine.Rate = rate
            self._engine.Volume = volume
        except Exception:
            pass

    def speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        # 0 waits for playback to finish, which keeps the voice loop's mute
        # window aligned with the actual audio.
        self._engine.Speak(text, 0)


def _default_sapi_engine():
    import win32com.client

    return win32com.client.Dispatch("SAPI.SpVoice")


class MicrophoneSource:
    """Pulls fixed-size mono frames from the default input device."""

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        stream_factory: Callable[[int, int], object] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self._frame_samples = int(sample_rate * frame_ms / 1000)
        self._factory = stream_factory or _default_stream_factory
        self._stream = None

    def _open(self):
        if self._stream is None:
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


def _default_stream_factory(sample_rate: int, frame_samples: int):
    import sounddevice as sd

    return sd.InputStream(
        samplerate=sample_rate,
        blocksize=frame_samples,
        channels=1,
        dtype="float32",
    ).__enter__()


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
