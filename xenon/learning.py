"""The one place the rest of Xenon reaches the habit store.

``patterns.PatternStore`` does the learning; this module decides whether
it may (the "learn my habits" switch, MusicSettings.learn_patterns, and
XENON_PATTERNS=off) and hands out the process-wide store, so the music
widget, the orb, the voice loop and the typed path all write to the same
place and honour the same switch.

The switch is read from the settings file at most every few seconds (the
widget asks about once a second; reading the file each time would be
waste), and it is checked on every write, so "stop learning" takes effect
right away from any thread.
"""
from __future__ import annotations

import os
import threading
import time

_CHECK_EVERY_S = 5.0
_lock = threading.Lock()
_cached: tuple[float, bool] = (0.0, True)


def enabled() -> bool:
    """Whether Xenon may learn right now."""
    global _cached
    if os.environ.get("XENON_PATTERNS", "").strip().lower() in {"0", "off", "false", "no"}:
        return False
    now = time.monotonic()
    with _lock:
        at, value = _cached
        if now - at < _CHECK_EVERY_S and at:
            return value
    try:
        from xenon import music_settings

        value = bool(getattr(music_settings.load(), "learn_patterns", True))
    except Exception:
        value = True
    with _lock:
        _cached = (now, value)
    return value


def refresh() -> None:
    """Forget the cached switch (after a voice "stop learning")."""
    global _cached
    with _lock:
        _cached = (0.0, True)


def store():
    """The shared habit store, or None if it cannot be opened."""
    try:
        from xenon import patterns

        return patterns.get_store(enabled=enabled)
    except Exception:
        return None


def summary() -> str:
    """Compact habits text for a model prompt ("" when nothing is known
    or learning is off)."""
    if not enabled():
        return ""
    s = store()
    try:
        return s.summary_for_prompt() if s is not None else ""
    except Exception:
        return ""


def set_learning(on: bool) -> str:
    """Flip the switch in the music settings file; returns what to say."""
    try:
        from xenon import music_settings as ms

        ms.save(ms.coerce({**ms.asdict(ms.load()), "learn_patterns": bool(on)}))
    except Exception as error:
        return f"I couldn't change that: {error}."
    refresh()
    return ("OK, I'll keep learning what you like." if on
            else "OK, I've stopped learning your habits. What I already know stays until you say forget my history.")


def forget() -> str:
    s = store()
    try:
        if s is not None:
            s.forget()
    except Exception as error:
        return f"I couldn't clear it: {error}."
    return "Done. I've forgotten your listening and app history."
