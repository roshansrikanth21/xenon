"""System audio for the desktop music widget, via WASAPI loopback.

The widget reacts to whatever is playing through the speakers — Spotify, a
browser tab, a game, a call — with no per-service integration and nothing to
sign in to. Like Weyv we open our own WASAPI loopback tap of the default
render endpoint and follow the device when it is switched.

Capture goes through ``pyaudiowpatch`` (a PortAudio fork whose WASAPI backend
speaks loopback natively). An earlier build drove IAudioCaptureClient through
ctypes/comtypes, which kept faulting inside libffi on this machine — with
correct arguments, every formulation (WINFUNCTYPE, CFUNCTYPE, raw integer
addresses, struct holders) crashed at the same corrupted-pointer write, while
the identical render-side calls worked. PortAudio's C code has no ctypes in
the audio path at all, so that entire fault class is gone.

Format parsing and downmixing stay here (tested in
``tests/test_system_audio.py``); PortAudio hands us interleaved float32 in the
endpoint's channel count, which ``LoopbackFormat.mono`` folds to mono. Audio
never leaves the machine: samples go into the analyser and nowhere else.
"""
from __future__ import annotations

import ctypes
import struct
import threading
import time
from dataclasses import dataclass

AUDCLNT_BUFFERFLAGS_SILENT = 0x2


@dataclass(frozen=True)
class LoopbackFormat:
    """The endpoint's shared-mode mix format."""

    sample_rate: int
    channels: int
    float_samples: bool

    def mono(self, payload: bytes) -> "MonoChunk":
        """Downmix one interleaved packet to mono float32."""
        if self.float_samples:
            count = len(payload) // 4
            samples = struct.unpack(f"<{count}f", payload[: count * 4]) if count else ()
            mono = samples if self.channels == 1 else _frame_average(samples, self.channels)
        else:
            count = len(payload) // 2
            samples = struct.unpack(f"<{count}h", payload[: count * 2]) if count else ()
            if self.channels == 1:
                mono = tuple(s / 32768.0 for s in samples)
            else:
                mono = tuple(
                    frame / 32768.0
                    for frame in _frame_average(samples, self.channels)
                )
        return MonoChunk(mono, self.sample_rate)


def _frame_average(samples: tuple, channels: int) -> tuple[float, ...]:
    """Average every group of ``channels`` interleaved samples."""
    frames = len(samples) // channels
    if frames == 0:
        return ()
    return tuple(
        sum(samples[base : base + channels]) / channels
        for base in range(0, frames * channels, channels)
    )


class MonoChunk:
    __slots__ = ("samples", "sample_rate")

    def __init__(self, samples: tuple[float, ...], sample_rate: int) -> None:
        self.samples = samples
        self.sample_rate = sample_rate


def wave_format_from_pointer(pointer: int) -> LoopbackFormat:
    """Read a WAVEFORMATEX/WAVEFORMATEXTENSIBLE mix format at ``pointer``."""
    if not pointer:
        raise ValueError("no mix format")
    tag, channels, rate, _avg, _align, _bits, extra = struct.unpack_from(
        "<HHIIHHH", ctypes.string_at(pointer, 18)
    )
    float_samples = tag == 3  # WAVE_FORMAT_IEEE_FLOAT
    if tag == 0xFFFE and extra >= 22:  # WAVEFORMATEXTENSIBLE
        # Layout: WAVEFORMATEX (18) + wValidBitsPerSample (2) + dwChannelMask
        # (4) + SubFormat GUID. KSDATAFORMAT_SUBTYPE_IEEE_FLOAT's first four
        # GUID bytes are 03 00 00 00 when little-endian.
        sub_format = ctypes.string_at(pointer + 24, 4)
        float_samples = sub_format == b"\x03\x00\x00\x00"
    return LoopbackFormat(rate, channels, float_samples)


class RingBuffer:
    """Latest-wins ring of mono float32 samples for one reader."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._buffer = [0.0] * capacity
        self._write = 0
        self._filled = 0
        self._lock = threading.Lock()

    def push(self, samples) -> None:
        with self._lock:
            n = len(samples)
            if n >= self.capacity:
                self._buffer[:] = list(samples[-self.capacity :])
                self._write = 0
                self._filled = self.capacity
                return
            end = self._write + n
            if end <= self.capacity:
                self._buffer[self._write : end] = samples
            else:
                first = self.capacity - self._write
                self._buffer[self._write :] = samples[:first]
                self._buffer[: n - first] = samples[first:]
            self._write = end % self.capacity
            self._filled = min(self.capacity, self._filled + n)

    def read_latest(self, count: int) -> list[float]:
        """The most recent ``count`` samples, oldest first."""
        count = min(count, self.capacity)
        with self._lock:
            filled = min(count, self._filled)
            out = [0.0] * filled
            start = (self._write - filled) % self.capacity
            for i in range(filled):
                out[i] = self._buffer[(start + i) % self.capacity]
            return out


class LoopbackSource:
    """A background thread capturing system audio as mono float32.

    ``start()`` resolves once the format is known (or fails with a reason).
    On device invalidation the tap restarts automatically after a short delay
    so the widget follows output switches. Call ``stop()`` when nobody listens.
    """

    def __init__(self, ring_capacity: int = 1 << 16, restart_delay: float = 0.6) -> None:
        self.format: LoopbackFormat | None = None
        self.ring = RingBuffer(ring_capacity)
        self.restart_delay = restart_delay
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: str | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> tuple[bool, str]:
        self._stop.clear()
        self._error = None
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="xenon-loopback", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            reason = self._error or "loopback tap did not start"
            self.stop()
            return False, reason
        if self._error:
            return False, self._error
        return True, ""

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    # -- reading ------------------------------------------------------------
    def read_latest(self, count: int) -> list[float]:
        return self.ring.read_latest(count)

    # -- worker -------------------------------------------------------------
    def _run(self) -> None:
        """Tap sessions for the lifetime of the source.

        Each session re-opens ``PyAudio``, so a changed default output device
        (or a removed one, which makes reads fail) is picked up by the next
        session after ``restart_delay``.
        """
        try:
            import pyaudiowpatch
        except Exception as error:  # missing optional dependency
            self._error = (
                "loopback needs pyaudiowpatch: pip install -e \".[music]\" "
                f"({error})"
            )
            self._ready.set()
            return
        try:
            while not self._stop.is_set():
                if self._capture_once(pyaudiowpatch) == 0:
                    return
                # Session ended: device switched/removed, or a transient
                # failure. Pause briefly so the widget follows the switch
                # instead of spinning.
                self._stop.wait(self.restart_delay)
        except Exception as error:
            self._error = f"loopback unavailable: {error}"
            self._ready.set()

    def _capture_once(self, pyaudiowpatch) -> int:
        """One tap session. Returns nonzero when the session should restart."""
        import numpy

        audio = pyaudiowpatch.PyAudio()
        stream = None
        try:
            device = audio.get_default_wasapi_loopback()
            rate = int(device["defaultSampleRate"])
            channels = max(1, int(device["maxInputChannels"]))
            self.format = LoopbackFormat(rate, channels, float_samples=True)
            stream = audio.open(
                format=pyaudiowpatch.paFloat32,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=1024,
            )
            self._ready.set()
            while not self._stop.is_set():
                payload = stream.read(1024, exception_on_overflow=False)
                fmt = self.format
                if fmt:
                    self.ring.push(fmt.mono(payload).samples)
        except Exception:
            # Device invalidated (switched/removed) or a transient PortAudio
            # error: report restart, unless we were told to stop.
            return 0 if self._stop.is_set() else 1
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            try:
                audio.terminate()
            except Exception:
                pass
        return 0
