"""Tiny deterministic intents for the task layer: do, stop, cancel, next, back.

Only exact, short phrases count. "can you do X" is a question for the
assistant, "do you know..." is a question, and "stop the music" is a music
command; none of them is a task intent. Anything not recognised returns
``None`` and falls through to today's behaviour.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Intent:
    kind: str          # do | stop | cancel | next | back
    goal: str = ""
    raw: str = ""


_STOP = frozenset(
    {"stop", "stop it", "stop that", "stop task", "stop the task", "stop this task",
     "stop everything", "stop now"}
)
_CANCEL = frozenset(
    {"cancel", "cancel it", "cancel that", "cancel task", "cancel the task", "cancel this",
     "never mind", "nevermind", "forget it", "abort", "exit", "exit lesson", "quit task",
     "quit the task"}
)
_NEXT = frozenset({"next", "next step", "go on"})
_BACK = frozenset({"back", "go back", "previous", "previous step"})

# "do <goal>" whose first goal word makes it a question or a negation.
_NOT_A_GOAL = frozenset(
    {"you", "u", "ya", "i", "we", "they", "he", "she", "it", "not", "nothing", "that",
     "this", "these", "those", "people", "anyone", "anybody", "someone", "somebody",
     "n't", "nt", "so", "too", "as", "what", "whatever", "any", "re"}
)


def _normalise(text: str) -> str:
    from xenon.untrusted import strip_invisible

    cleaned = strip_invisible(text or "").replace("’", "'").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def parse(text: str) -> Intent | None:
    raw = _normalise(text)
    if not raw:
        return None
    simple = re.sub(r"[.!,;:]+$", "", raw).strip().lower()
    simple = re.sub(r"^(?:please|ok|okay)\s*,?\s+", "", simple)
    simple = re.sub(r"\s*,?\s*please$", "", simple)
    if simple in _STOP:
        return Intent("stop", raw=raw)
    if simple in _CANCEL:
        return Intent("cancel", raw=raw)
    if simple in _NEXT:
        return Intent("next", raw=raw)
    if simple in _BACK:
        return Intent("back", raw=raw)
    if raw.rstrip().endswith("?"):
        return None  # "do you have...?" is a question
    match = re.match(r"(?i)^do\s+(.+)$", re.sub(r"^(?i:please|ok|okay)\s*,?\s+", "", raw))
    if match is None:
        return None
    goal = re.sub(r"[.!]+$", "", match.group(1)).strip()
    first = re.split(r"[\s,']+", goal.lower(), maxsplit=1)[0] if goal else ""
    if not goal or first in _NOT_A_GOAL or len(goal) < 3:
        return None
    return Intent("do", goal=goal, raw=raw)


def is_escape(text: str) -> bool:
    """True for stop / cancel / exit: these leave a waiting task's focus."""
    intent = parse(text)
    return intent is not None and intent.kind in {"stop", "cancel"}
