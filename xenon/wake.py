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


def _wake_pattern(wake_word: str) -> re.Pattern[str]:
    words = r"\s+".join(re.escape(part) for part in wake_word.split())
    return re.compile(
        rf"^[^\w]*{words}\b[\s,.;:!?-]*(?P<rest>.*)$",
        flags=re.IGNORECASE | re.DOTALL,
    )


class WakeGate:
    """Turns transcripts into commands, or nothing at all."""

    def __init__(
        self,
        wake_word: str = "xenon",
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] | None = None,
        aliases: Sequence[str] = (),
        always: bool = False,
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

    @property
    def always(self) -> bool:
        return self._always

    @property
    def armed(self) -> bool:
        return self._armed_at is not None

    @property
    def speaking(self) -> bool:
        return self._speaking

    def set_speaking(self, speaking: bool) -> None:
        """Mute detection while the assistant's own audio is playing."""
        self._speaking = speaking
        if speaking:
            self._armed_at = None

    def feed(self, transcript: str) -> str | None:
        if self._speaking:
            return None

        text = transcript.strip()
        if not text:
            return None

        if self._always:
            # Hands-free mode: any complete sentence is the command. A spoken
            # wake word is still accepted and stripped, so both habits work.
            for pattern in self._patterns:
                match = pattern.match(text)
                if match is None:
                    continue
                command = match.group("rest").strip(" \t,.;:-")
                if not command or command.lower() in self._names:
                    return None
                return command
            return text

        match = None
        for pattern in self._patterns:
            match = pattern.match(text)
            if match is not None:
                break
        if match is not None:
            command = match.group("rest").strip(" \t,.;:-")
            lowered = command.lower()
            # A transcript that is just the wake word or one of its aliases (with
            # no real command after it) arms the next utterance instead of being
            # dispatched as a command.
            if not command or lowered in {w.lower() for w in (self._wake_word, *self._aliases) if w.strip()}:
                self._armed_at = self._clock()
                return None
            self._armed_at = None
            return command

        if self._armed_at is None:
            return None
        if self._clock() - self._armed_at > self._ttl_s:
            self._armed_at = None
            return None
        self._armed_at = None
        return text
