"""Where the voice starts each word, from the audio the widget already hears.

LRCLIB times lines, not words, so a line's words are spread out by their
syllables. That guess drifts: a singer rushes one phrase and drags the
next. Most sung words begin with a burst of energy in the voice's range
(about 300 Hz - 3.4 kHz), so this module finds those bursts ("onsets") in
the live spectrum, and ``snap_starts`` pulls each guessed word start onto a
burst that lands close to it. Anything else in that range (a snare, a
strummed chord) can also make a burst, which is why a word only moves when
one lands within ``WINDOW_MS`` of the guess, and the correction is carried
to the words after it rather than letting each word jump about.
"""
from __future__ import annotations

from collections import deque

# Bars of ``spectrum_bars(..., 64)`` (log-spaced 40 Hz - 12 kHz) that hold
# the voice: bar i starts near 40 * 1.093**i Hz.
VOICE_BARS = (23, 50)
WINDOW_MS = 140         # how far a word may move onto a burst
MAX_SHIFT_MS = 300      # the most the guess is ever corrected by
MIN_GAP_MS = 60         # two words never start closer than this


class OnsetDetector:
    """Spectral flux over the voice bars against its own recent level.

    Feed it one spectrum a frame (``feed``); it returns True on a frame where
    a burst starts. The threshold follows the song (mean + 1.6 sd of the last
    second or so), and two bursts are at least ``refractory`` apart."""

    def __init__(self, history: int = 36, refractory: float = 0.09) -> None:
        self._prev = None
        self._flux: deque = deque(maxlen=history)
        self._last = -1.0
        self.refractory = refractory

    def feed(self, bars, t: float) -> bool:
        lo, hi = VOICE_BARS
        voice = list(bars[lo:hi])
        if self._prev is None or len(self._prev) != len(voice):
            self._prev = voice
            return False
        flux = sum(max(0.0, a - b) for a, b in zip(voice, self._prev)) / max(1, len(voice))
        self._prev = voice
        hits = False
        if len(self._flux) >= 8:
            mean = sum(self._flux) / len(self._flux)
            var = sum((f - mean) ** 2 for f in self._flux) / len(self._flux)
            limit = mean + 1.6 * var ** 0.5 + 0.004
            hits = flux > limit and t - self._last >= self.refractory
        self._flux.append(flux)
        if hits:
            self._last = t
        return hits


class SoundGate:
    """Is something clearly audible on the speakers? Some players (a
    streaming site in a browser) never tell Windows they are playing; the
    widget uses this to wake its visuals anyway. On after ``on_s`` of
    steady sound, off after ``off_s`` of quiet, so a notification ping or
    a gap between songs does not flicker it."""

    def __init__(self, level: float = 0.004, on_s: float = 0.5, off_s: float = 1.5) -> None:
        self.level, self.on_s, self.off_s = level, on_s, off_s
        self.on = False
        self._since = None

    def update(self, samples, t: float) -> bool:
        if samples is None or len(samples) == 0:
            loud = False
        else:
            import numpy as np

            data = np.asarray(samples[-1024:], dtype=np.float32)
            loud = float(np.sqrt(np.mean(data * data))) > self.level
        if loud != self.on:
            if self._since is None:
                self._since = t
            if t - self._since >= (self.on_s if loud else self.off_s):
                self.on, self._since = loud, None
        else:
            self._since = None
        return self.on


class BeatBand:
    """How hard one slice of the spectrum is hitting, 0..1, for the edge
    glow. The level is judged against the band's own recent peak (a quiet
    track or a low volume still pulses), and a hit well above the band's
    recent average flashes and eases back down (``decay`` per 1/30 s:
    lower = snappier, which suits hi-hats)."""

    def __init__(self, lo: int, hi: int, decay: float = 0.82) -> None:
        self.lo, self.hi, self.decay = lo, hi, decay
        self.avg = None
        self.peak = 0.0
        self.pulse = 0.0

    def update(self, bars, dt: float, playing: bool) -> float:
        part = list(bars[self.lo:self.hi]) or [0.0]
        value = sum(part) / len(part)
        mix = 0.92 ** (dt * 30)
        self.avg = value if self.avg is None else mix * self.avg + (1 - mix) * value
        self.peak = max(value, self.peak * (0.996 ** (dt * 30)), 0.004)
        level = value / self.peak
        kick = max(0.0, value - self.avg * 1.15) / self.peak * 2.5 if playing else 0.0
        self.pulse = max(min(1.0, kick), self.pulse * (self.decay ** (dt * 30)))
        return min(1.0, 0.85 * self.pulse + 0.35 * level ** 2) if playing else 0.0


# Which slice drives which edge: the floor thumps, the walls carry the
# body of the song, the ceiling sparkles.
BANDS = {"bass": (0, 6, 0.82), "mids": (15, 40, 0.78), "highs": (44, 64, 0.62)}
EDGE_BAND = {"edge_bottom": "bass", "edge_left": "mids", "edge_right": "mids", "edge_top": "highs",
             "corner_bl": "bass", "corner_br": "bass", "corner_tl": "highs", "corner_tr": "highs"}


def snap_starts(starts: list[int], onsets: list[int], line_end: int | None = None) -> list[int]:
    """Guessed word starts moved onto nearby voice bursts (song ms). A move
    carries over to the words after it (the singer is running early or
    late), capped at ``MAX_SHIFT_MS``; starts stay in order."""
    if not starts or not onsets:
        return list(starts)
    marks = sorted(onsets)
    out: list[int] = []
    shift = 0
    for s in starts:
        guess = s + shift
        near = min(marks, key=lambda o: abs(o - guess))
        if abs(near - guess) <= WINDOW_MS and abs(near - s) <= MAX_SHIFT_MS and \
                (not out or near >= out[-1] + MIN_GAP_MS):
            shift = near - s
            guess = near
        if out:
            guess = max(guess, out[-1] + MIN_GAP_MS)
        if line_end is not None:
            guess = min(guess, max(s, line_end - MIN_GAP_MS))
        out.append(int(guess))
    return out
