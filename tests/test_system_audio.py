"""system_audio — mix-format parsing, downmix and the ring buffer."""
from __future__ import annotations

import ctypes
import struct

import pytest

from xenon.system_audio import (
    AUDCLNT_BUFFERFLAGS_SILENT,
    LoopbackFormat,
    RingBuffer,
    wave_format_from_pointer,
)


def _float_extensible_pointer(channels: int = 2, rate: int = 48000) -> int:
    """A WAVEFORMATEXTENSIBLE IEEE-float mix format, as GetMixFormat returns."""
    # WAVEFORMATEX (18 bytes) + wValidBitsPerSample (2) + dwChannelMask (4)
    # + SubFormat GUID (16), GUID stored little-endian.
    sub_format = b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    blob = struct.pack(
        "<HHIIHHHHI", 0xFFFE, channels, rate, rate * channels * 4, channels * 4, 32, 22, 32, 1
    ) + sub_format
    buffer = ctypes.create_string_buffer(blob, len(blob))
    return ctypes.addressof(buffer), buffer  # type: ignore[return-value]


def _int16_pointer(channels: int = 2, rate: int = 44100) -> int:
    blob = struct.pack(
        "<HHIIHH", 1, channels, rate, rate * channels * 2, channels * 2, 16
    )
    buffer = ctypes.create_string_buffer(blob, len(blob))
    return ctypes.addressof(buffer), buffer  # type: ignore[return-value]


class TestWaveFormatParsing:
    def test_extensible_float(self):
        pointer, _keep = _float_extensible_pointer()
        fmt = wave_format_from_pointer(pointer)
        assert fmt.sample_rate == 48000
        assert fmt.channels == 2
        assert fmt.float_samples is True

    def test_plain_int16(self):
        pointer, _keep = _int16_pointer()
        fmt = wave_format_from_pointer(pointer)
        assert fmt.sample_rate == 44100
        assert fmt.channels == 2
        assert fmt.float_samples is False

    def test_null_pointer_rejected(self):
        with pytest.raises(ValueError):
            wave_format_from_pointer(0)


class TestDownmix:
    def test_stereo_float_averages_frames(self):
        fmt = LoopbackFormat(48000, 2, True)
        chunk = fmt.mono(struct.pack("<4f", 1.0, -1.0, 0.5, 0.5))
        assert chunk.samples == (0.0, 0.5)
        assert chunk.sample_rate == 48000

    def test_mono_float_passthrough(self):
        fmt = LoopbackFormat(48000, 1, True)
        chunk = fmt.mono(struct.pack("<2f", 0.25, -0.75))
        assert chunk.samples == (0.25, -0.75)

    def test_stereo_int16_averages_and_scales(self):
        fmt = LoopbackFormat(44100, 2, False)
        chunk = fmt.mono(struct.pack("<4h", 32767, -32768, 16384, 16384))
        assert chunk.samples == pytest.approx((0.0, 0.5), abs=1e-4)

    def test_mono_int16_scales(self):
        fmt = LoopbackFormat(44100, 1, False)
        chunk = fmt.mono(struct.pack("<2h", 16384, -16384))
        assert chunk.samples == (0.5, -0.5)

    def test_trailing_partial_frame_ignored(self):
        fmt = LoopbackFormat(48000, 2, True)
        chunk = fmt.mono(struct.pack("<3f", 1.0, -1.0, 0.5))
        assert chunk.samples == (0.0,)


class TestRingBuffer:
    def test_read_latest_returns_freshest(self):
        ring = RingBuffer(8)
        ring.push([1, 2, 3, 4])
        assert ring.read_latest(4) == [1, 2, 3, 4]
        ring.push([5, 6])
        assert ring.read_latest(4) == [3, 4, 5, 6]

    def test_wraparound(self):
        ring = RingBuffer(4)
        ring.push([1, 2, 3])
        ring.push([4, 5, 6])
        assert ring.read_latest(4) == [3, 4, 5, 6]

    def test_overflow_keeps_tail(self):
        ring = RingBuffer(3)
        ring.push([1, 2, 3, 4, 5, 6, 7])
        assert ring.read_latest(3) == [5, 6, 7]

    def test_partial_fill_pads_with_recent_only(self):
        ring = RingBuffer(8)
        ring.push([7, 8])
        assert ring.read_latest(4) == [7, 8]

    def test_silent_flag_constant_matches_wasapi(self):
        assert AUDCLNT_BUFFERFLAGS_SILENT == 0x2
