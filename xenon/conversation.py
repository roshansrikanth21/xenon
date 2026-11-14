"""A short rolling log of what the user said and what Xenon answered.

Without it every question reaches the model alone, so "play it again",
"what about him?" or "do that for the other file" have nothing to resolve
against. The typed path and the voice thread both answer the user, so they
share one log and it is guarded by a lock.

The log stays in memory only. Old turns expire (default 20 minutes) because a
question asked after a long pause is usually a new topic, and dragging the
old one in makes the model confuse the two. Turns can be marked private so a
caller can keep them out of anything that leaves the machine.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from xenon import untrusted


@dataclass(frozen=True)
class Turn:
    user: str
    reply: str
    kind: str
    private: bool
    at: float


def _clip(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters, marking the cut.

    Prompts are paid per token and a long earlier answer adds little to
    resolving a pronoun, so each side of a turn is kept short.
    """
    text = " ".join(text.split())
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


class ConversationLog:
    """Thread-safe log of recent (user, reply) turns, oldest first."""

    def __init__(self, maxlen: int = 12, max_age_s: float = 1200) -> None:
        self._turns: deque[Turn] = deque(maxlen=max(1, int(maxlen)))
        self._max_age_s = float(max_age_s)
        self._lock = threading.Lock()

    def add(
        self,
        user: str,
        reply: str,
        kind: str = "chat",
        private: bool = False,
        now: float | None = None,
    ) -> None:
        """Record one exchange. Empty exchanges are ignored.

        The user text is cleaned of invisible characters on the way in, since
        heard or pasted text can carry hidden prompts that would otherwise be
        replayed to the model on every later turn.
        """
        user = untrusted.strip_invisible(user or "").strip()
        reply = untrusted.strip_invisible(reply or "").strip()
        if not user and not reply:
            return
        at = time.time() if now is None else float(now)
        with self._lock:
            self._turns.append(Turn(user, reply, kind or "chat", bool(private), at))

    def _recent(self, n: int, include_private: bool, now: float | None) -> list[Turn]:
        now = time.time() if now is None else float(now)
        with self._lock:
            turns = list(self._turns)
        fresh = [
            turn
            for turn in turns
            if now - turn.at <= self._max_age_s and (include_private or not turn.private)
        ]
        if n <= 0:
            return []
        return fresh[-n:]

    def messages(
        self,
        n: int = 6,
        include_private: bool = True,
        clip: int = 300,
        now: float | None = None,
    ) -> list[dict]:
        """Last ``n`` turns as chat messages, oldest first.

        Each turn becomes a user message followed by an assistant message so
        the model sees the conversation in the shape it was trained on.
        """
        out: list[dict] = []
        for turn in self._recent(n, include_private, now):
            if turn.user:
                out.append(
                    {"role": "user", "content": _clip(untrusted.strip_invisible(turn.user), clip)}
                )
            if turn.reply:
                out.append({"role": "assistant", "content": _clip(turn.reply, clip)})
        return out

    def as_text(
        self,
        n: int = 3,
        clip: int = 160,
        include_private: bool = True,
        now: float | None = None,
    ) -> str:
        """Compact one-line-per-turn summary for the intent router.

        The router only needs enough to resolve a reference ("again", "that
        one"), so it gets a few short lines instead of full messages.
        """
        lines = []
        for turn in self._recent(n, include_private, now):
            user = _clip(untrusted.strip_invisible(turn.user), clip)
            reply = _clip(turn.reply, clip)
            lines.append(f"User: {user} / Xenon: {reply}")
        return "\n".join(lines)

    def last_user(self, now: float | None = None) -> str:
        """The most recent unexpired thing the user said, or ""."""
        for turn in reversed(self._recent(len(self._turns) or 1, True, now)):
            if turn.user:
                return turn.user
        return ""

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._turns)


_SHARED: ConversationLog | None = None
_SHARED_LOCK = threading.Lock()


def shared() -> ConversationLog:
    """The process-wide log used by both the typed path and the voice thread."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = ConversationLog()
        return _SHARED
