"""Everyday commands that never need a model: time, date, volume, timers.

"what time is it", "what's the date", "volume up", "turn it down a lot",
"mute", "set a timer for 5 minutes", "cancel the timer", "how long is left".

Timers live in the running process (the orb or ``xenon voice``). When one
finishes, every registered listener is called; the orb shows a card and
chimes. A one-shot ``xenon do`` would exit before the timer fires, so it says
so instead of pretending.
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

# --------------------------------------------------------------- parsing --


@dataclass(frozen=True)
class QuickCommand:
    """``kind``: time, date, volume_up, volume_down, mute, timer,
    cancel_timer or timer_left. ``seconds`` is set for timers and ``steps``
    for volume."""

    kind: str
    seconds: int = 0
    steps: int = 0
    label: str = ""


_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90,
    "half an": 0.5, "half a": 0.5,
}
_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
}
_AMOUNT = r"(?:\d+(?:\.\d+)?|" + "|".join(sorted(map(re.escape, _NUMBER_WORDS), key=len, reverse=True)) + r")"
_UNIT = r"(?:" + "|".join(sorted(map(re.escape, _UNITS), key=len, reverse=True)) + r")"
_DURATION_PART = re.compile(rf"(?P<amount>{_AMOUNT})\s*(?P<unit>{_UNIT})\b")

_FILLER = r"^(?:please\s+|can you\s+|could you\s+)?"
_TIME_RE = re.compile(
    _FILLER + r"(?:what(?:'s|s| is)\s+the\s+time|what\s+time\s+is\s+it|tell me the time|time)(?:\s+(?:right\s+)?now)?$"
)
_DATE_RE = re.compile(
    _FILLER + r"(?:what(?:'s|s| is)\s+(?:the\s+date|today(?:'s date)?|the day)(?:\s+today)?"
    r"|what\s+day\s+is\s+(?:it|today)|what\s+date\s+is\s+(?:it|today)|today's date)$"
)
_VOLUME_RE = re.compile(
    _FILLER + r"(?:turn\s+(?:it|the\s+(?:volume|sound|music))\s+(?P<dir1>up|down)"
    r"|(?:volume|sound)\s+(?P<dir2>up|down)|(?P<dir3>louder|quieter|softer)"
    r"|turn\s+(?P<dir4>up|down)\s+the\s+(?:volume|sound|music))"
    r"(?P<amount>\s+(?:a\s+lot|a\s+little|a\s+bit|way\s+more|more))?$"
)
_MUTE_RE = re.compile(_FILLER + r"(?:mute|unmute)(?:\s+(?:the\s+)?(?:volume|sound|audio|it|everything))?$")
# Tried in order; the first form whose duration parses wins. ("set a timer
# for 5 minutes" also fits "set a <duration> timer" with duration "a".)
_TIMER_FORMS = (
    re.compile(_FILLER + r"(?:set|start)\s+(?:a\s+|the\s+)?timer\s+(?:for|of)\s+(?P<d>.+?)"
               r"(?:\s+(?:for|called|named)\s+(?P<label>.+))?$"),
    re.compile(_FILLER + r"(?:set|start|put on)\s+(?:a\s+|an\s+)?(?P<d>.+?)\s+timer"
               r"(?:\s+(?:for|called|named)\s+(?P<label>.+))?$"),
    re.compile(_FILLER + r"(?:remind me|wake me|ping me|tell me)\s+in\s+(?P<d>.+?)"
               r"(?:\s+(?:to|about|that)\s+(?P<label>.+))?$"),
)
_CANCEL_RE = re.compile(_FILLER + r"(?:cancel|stop|delete|clear)\s+(?:the\s+|my\s+|all\s+(?:the\s+)?)?timers?$")
_LEFT_RE = re.compile(
    r"^(?:how\s+(?:much\s+time|long)\s+is\s+left(?:\s+on\s+(?:the|my)\s+timer)?"
    r"|how\s+long\s+(?:until|till)\s+the\s+timer|time\s+left|timer\s+status)$"
)


def parse_duration(text: str) -> int | None:
    """'5 minutes', 'an hour and a half', '1h 30m', 'ninety seconds' -> seconds."""
    phrase = text.lower().strip()
    if not phrase:
        return None

    def amount_of(word: str) -> float:
        return float(_NUMBER_WORDS[word]) if word in _NUMBER_WORDS else float(word)

    # "2 and a half minutes" -> "2.5 minutes" (an "hour and a half" is below).
    phrase = re.sub(
        rf"(?P<a>{_AMOUNT})\s+and\s+a\s+half\s+(?P<u>{_UNIT})\b",
        lambda m: f"{amount_of(m.group('a')) + 0.5} {m.group('u')}",
        phrase,
    )
    total = 0.0
    found = False
    for match in _DURATION_PART.finditer(phrase):
        amount_text = match.group("amount")
        amount = float(_NUMBER_WORDS.get(amount_text, amount_text)) if amount_text in _NUMBER_WORDS else float(amount_text)
        total += amount * _UNITS[match.group("unit")]
        found = True
    if re.search(r"\band\s+a\s+half\b", phrase) and found:
        last = list(_DURATION_PART.finditer(phrase))[-1]
        total += 0.5 * _UNITS[last.group("unit")]
    if not found:
        return None
    seconds = int(round(total))
    return seconds if 0 < seconds <= 24 * 3600 else None


def parse(text: str) -> QuickCommand | None:
    phrase = " ".join(text.lower().replace("’", "'").strip().strip(".!?").split())
    if not phrase:
        return None
    if _TIME_RE.match(phrase):
        return QuickCommand("time")
    if _DATE_RE.match(phrase):
        return QuickCommand("date")
    volume = _VOLUME_RE.match(phrase)
    if volume:
        direction = next(
            (volume.group(name) for name in ("dir1", "dir2", "dir3", "dir4") if volume.group(name)), "up"
        )
        up = direction in {"up", "louder"}
        amount = (volume.group("amount") or "").strip()
        steps = 10 if amount in {"a lot", "way more"} else 2 if amount in {"a little", "a bit"} else 5
        return QuickCommand("volume_up" if up else "volume_down", steps=steps)
    if _MUTE_RE.match(phrase):
        return QuickCommand("mute")
    if _CANCEL_RE.match(phrase):
        return QuickCommand("cancel_timer")
    if _LEFT_RE.match(phrase):
        return QuickCommand("timer_left")
    for form in _TIMER_FORMS:
        timer = form.match(phrase)
        if not timer:
            continue
        seconds = parse_duration(timer.group("d"))
        if seconds:
            return QuickCommand("timer", seconds=seconds, label=(timer.group("label") or "").strip())
    return None


# ---------------------------------------------------------------- timers --


def describe(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " and ".join(parts) or "0 seconds"


@dataclass
class Timer:
    seconds: int
    label: str
    ends_at: float
    handle: threading.Timer | None = field(default=None, repr=False)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def remaining(self) -> float:
        return max(0.0, self.ends_at - self.clock())

    @property
    def name(self) -> str:
        return self.label or "your timer"


class TimerBoard:
    """Timers for this process; listeners are told when one finishes."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 start: Callable[[float, Callable[[], None]], threading.Timer] | None = None) -> None:
        self._clock = clock
        self._start = start or _start_thread_timer
        self._timers: list[Timer] = []
        self._listeners: list[Callable[[Timer], None]] = []
        self._lock = threading.Lock()

    @property
    def has_listeners(self) -> bool:
        return bool(self._listeners)

    def listen(self, callback: Callable[[Timer], None]) -> None:
        self._listeners.append(callback)

    def unlisten(self, callback: Callable[[Timer], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def add(self, seconds: int, label: str = "") -> Timer:
        timer = Timer(seconds, label, self._clock() + seconds, clock=self._clock)

        def fire() -> None:
            with self._lock:
                if timer in self._timers:
                    self._timers.remove(timer)
            for listener in list(self._listeners):
                try:
                    listener(timer)
                except Exception:
                    pass

        timer.handle = self._start(seconds, fire)
        with self._lock:
            self._timers.append(timer)
        return timer

    def cancel_all(self) -> int:
        with self._lock:
            timers, self._timers = self._timers, []
        for timer in timers:
            if timer.handle is not None:
                timer.handle.cancel()
        return len(timers)

    def active(self) -> list[Timer]:
        with self._lock:
            return sorted(self._timers, key=lambda t: t.ends_at)


def _start_thread_timer(seconds: float, callback: Callable[[], None]) -> threading.Timer:
    handle = threading.Timer(seconds, callback)
    handle.daemon = True
    handle.start()
    return handle


TIMERS = TimerBoard()


# --------------------------------------------------------------- volume --

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF


def _press(code: int, times: int = 1) -> bool:
    from xenon.music import press_media_key

    ok = True
    for _ in range(max(1, times)):
        ok = press_media_key(code) and ok
    return ok


# --------------------------------------------------------------- running --


def run(command: QuickCommand, board: TimerBoard | None = None,
        now: Callable[[], _dt.datetime] = _dt.datetime.now,
        press: Callable[[int, int], bool] = _press) -> str:
    board = board or TIMERS
    kind = command.kind
    if kind == "time":
        moment = now()
        return f"It's {moment.strftime('%I:%M %p').lstrip('0')}."
    if kind == "date":
        moment = now()
        return f"It's {moment.strftime('%A, %B')} {moment.day}, {moment.year}."
    if kind in {"volume_up", "volume_down"}:
        # Each press moves Windows' volume 2 points.
        code = VK_VOLUME_UP if kind == "volume_up" else VK_VOLUME_DOWN
        if not press(code, command.steps):
            return "Volume keys are unavailable."
        return "Turned it up." if kind == "volume_up" else "Turned it down."
    if kind == "mute":
        return "Toggled mute." if press(VK_VOLUME_MUTE, 1) else "Volume keys are unavailable."
    if kind == "timer":
        if not board.has_listeners:
            return (
                "Timers need Xenon to keep running. Open the orb "
                "(`python -m xenon orb`) or voice, and ask there."
            )
        timer = board.add(command.seconds, command.label)
        what = f" for {command.label}" if command.label else ""
        return f"Timer set{what}: {describe(timer.seconds)}."
    if kind == "cancel_timer":
        count = board.cancel_all()
        if not count:
            return "There are no timers running."
        return "Cancelled the timer." if count == 1 else f"Cancelled {count} timers."
    if kind == "timer_left":
        timers = board.active()
        if not timers:
            return "There are no timers running."
        parts = [f"{describe(t.remaining())} left on {t.name}" for t in timers]
        return "; ".join(parts).capitalize() + "."
    return "I can't do that yet."
