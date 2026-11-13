"""Wake-word gating for the voice loop.

This is the deterministic part of the voice path, kept separate from audio so
it can be tested: a transcript becomes a command only when the wake word is
present, a bare wake word arms the next utterance briefly, and anything heard
while the assistant is speaking is discarded.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Sequence

DEFAULT_TTL_S = 8.0
# A bare wake word this soon after an ignored sentence claims that sentence.
DEFAULT_LOOKBACK_S = 2.5

# How a command got through the gate (``WakeGate.last_source``). Only the
# first two may start a task: the design accepts spoken tasks from
# push-to-talk, or when the wake word starts the utterance.
SOURCE_PUSH_TO_TALK = "push-to-talk"
SOURCE_WAKE_START = "wake-start"
SOURCE_WAKE_LATE = "wake-late"      # wake word later in the sentence, or the look-back
SOURCE_ALWAYS = "always"            # always-listening mode: anything said in the room
TASK_SOURCES = frozenset({SOURCE_PUSH_TO_TALK, SOURCE_WAKE_START})


_EDGE = " \t\r\n,.;:!?-\"'"
# Greetings that only lead into the wake word ("hey xenon, ...").
_LEAD_IN = re.compile(r"(?:^|\s)(?:hey|hi|hello|ok|okay|yo)[\s,]*$", re.IGNORECASE)


def _wake_pattern(wake_word: str) -> re.Pattern[str]:
    """Matches the wake word as a whole word anywhere in a sentence."""
    words = r"[\s,.-]+".join(re.escape(part) for part in wake_word.split())
    return re.compile(rf"(?<!\w){words}(?!\w)", flags=re.IGNORECASE)


def split_wake_word(text: str, patterns: Sequence[re.Pattern[str]]) -> str | None:
    """Remove the wake word wherever it was said and return the command.

    "xenon open chrome", "open chrome xenon" and "can you, xenon, open chrome"
    all give "open chrome"-style commands. Returns ``None`` when the wake word
    is absent and ``""`` when it was said on its own.
    """
    for pattern in patterns:
        match = pattern.search(text)
        if match is None:
            continue
        before = text[: match.start()].rstrip(_EDGE)
        before = _LEAD_IN.sub("", before).strip(_EDGE)
        # Keep the sentence's own ending ("...screen?") but not punctuation
        # that only surrounded the wake word ("open chrome, xenon.").
        after = text[match.end() :].lstrip(_EDGE).rstrip()
        if not after.strip(_EDGE):
            after = ""
        # Strip every other mention of any wake name. Whisper sometimes echoes
        # its spelling hint ("Xenon, zenon, see none") on a short clip, and
        # none of that may reach the model as a command.
        for name in patterns:
            after = name.sub("", after)
            before = name.sub("", before)
        after = re.sub(r"^[\s,.;:!?-]+", "", after).rstrip()
        if not after.strip(_EDGE):
            after = ""
        before = before.strip(_EDGE)
        joined = " ".join(part for part in (before, after) if part)
        joined = re.sub(r"\s+([,.;:!?])", r"\1", joined)
        joined = re.sub(r"(?:,\s*){2,}", ", ", joined)
        return re.sub(r"\s{2,}", " ", joined).strip(" ,")
    return None


def strip_wake_word(text: str, wake_word: str = "xenon", aliases: Sequence[str] = ()) -> str:
    """Typed or spoken, "play my liked songs xenon" means "play my liked songs"."""
    patterns = [_wake_pattern(word) for word in (wake_word, *aliases) if word and word.strip()]
    command = split_wake_word(text, patterns)
    return text.strip() if command is None else command


class WakeGate:
    """Turns transcripts into commands, or nothing at all."""

    def __init__(
        self,
        wake_word: str = "xenon",
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] | None = None,
        aliases: Sequence[str] = (),
        always: bool = False,
        lookback_s: float = DEFAULT_LOOKBACK_S,
    ) -> None:
        self._wake_word = wake_word.strip()
        self._aliases = tuple(aliases)
        self._patterns = [
            _wake_pattern(word)
            for word in (self._wake_word, *aliases)
            if word and word.strip()
        ]
        self._names = {
            word.lower() for word in (self._wake_word, *aliases) if word.strip()
        }
        self._ttl_s = ttl_s
        self._clock = clock if clock is not None else time.monotonic
        self._armed_at: float | None = None
        self._speaking = False
        self._always = always
        self._lookback_s = lookback_s
        self._last_ignored: tuple[str, float] | None = None
        self._armed_by = ""
        # How the last returned command got through (SOURCE_* above).
        self.last_source = ""

    @property
    def always(self) -> bool:
        return self._always

    @property
    def armed(self) -> bool:
        return self._armed_at is not None

    @property
    def speaking(self) -> bool:
        return self._speaking

    def arm(self) -> None:
        """Take the next utterance as a command (push-to-talk)."""
        self._armed_at = self._clock()
        self._armed_by = SOURCE_PUSH_TO_TALK
        self._last_ignored = None

    @property
    def may_start_task(self) -> bool:
        """May the last command start a task ("do ...")?"""
        return self.last_source in TASK_SOURCES

    def _wake_starts(self, text: str) -> bool:
        """The wake word is within the utterance's first two tokens."""
        for pattern in self._patterns:
            match = pattern.search(text)
            if match is None:
                continue
            before = text[: match.start()].strip(_EDGE)
            return len(before.split()) <= 1
        return False

    def set_speaking(self, speaking: bool) -> None:
        """Mute detection while the assistant's own audio is playing."""
        self._speaking = speaking
        if speaking:
            self._armed_at = None
            self._last_ignored = None

    def feed(self, transcript: str) -> str | None:
        if self._speaking:
            return None

        text = transcript.strip()
        if not text:
            return None

        command = split_wake_word(text, self._patterns)
        if command is not None and command.lower() in self._names:
            command = ""

        if self._always:
            # Hands-free mode: any complete sentence is the command. A spoken
            # wake word is still accepted and stripped, so both habits work.
            # Either way it may be the room talking, so it never starts a task.
            self.last_source = SOURCE_ALWAYS
            if command is None:
                return text
            return command or None

        now = self._clock()
        if command is not None:
            if not command:
                # "Play my liked songs, ... Xenon." The pause before the name
                # ends the first utterance, so the name arrives on its own a
                # moment after the command it belongs to: use that sentence.
                previous = self._last_ignored
                self._last_ignored = None
                if previous is not None and now - previous[1] <= self._lookback_s:
                    self._armed_at = None
                    # A sentence heard before the name: could be anyone.
                    self.last_source = SOURCE_WAKE_LATE
                    return previous[0]
                # Otherwise the name on its own arms the next utterance.
                self._armed_at = now
                self._armed_by = SOURCE_WAKE_START
                return None
            self._armed_at = None
            self._last_ignored = None
            self.last_source = SOURCE_WAKE_START if self._wake_starts(text) else SOURCE_WAKE_LATE
            return command

        if self._armed_at is None:
            self._last_ignored = (text, now)
            return None
        if self._clock() - self._armed_at > self._ttl_s:
            self._armed_at = None
            return None
        self._armed_at = None
        self.last_source = self._armed_by or SOURCE_WAKE_START
        return text
