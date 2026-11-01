"""The assist loop: question + screen context + retrieved memory -> answer.

The model is injected so the loop can be tested without a network and so a
local model can replace a cloud one without touching this code.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from xenon.memory import MemoryHit
from xenon.screen import ScreenContext
from xenon.skills import Skill

SYSTEM_PROMPT = (
    "You are Xenon, a concise personal assistant working beside the user on "
    "their Windows desktop. Use the screen context as what the user is looking "
    "at right now. Use retrieved memory only when it is relevant, and cite the "
    "source path in square brackets. If neither the screen nor memory supports "
    "an answer, say what is missing instead of guessing. Never invent screen "
    "contents or remembered facts."
)


@dataclass(frozen=True)
class Answer:
    text: str
    used_vision: bool
    memories: tuple[MemoryHit, ...]
    latency_ms: float


class Observer(Protocol):
    def observe(self) -> ScreenContext: ...


class MemorySearch(Protocol):
    def search(self, query: str, limit: int = 5) -> list[MemoryHit]: ...


class ChatModel(Protocol):
    def complete(self, messages: list[dict]) -> str: ...


class Assistant:
    """Answers a user question from the live screen and local memory."""

    def __init__(
        self,
        observer: Observer,
        memory: MemorySearch | None,
        model: ChatModel,
        max_memories: int = 5,
        max_context_chars: int = 6000,
    ) -> None:
        self._observer = observer
        self._memory = memory
        self._model = model
        self._max_memories = max_memories
        self._max_context_chars = max_context_chars

    def ask(self, question: str, skill: Skill | None = None) -> Answer:
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")

        started = time.perf_counter()
        context = self._observer.observe()
        memories = ()
        if self._memory is not None:
            memories = tuple(self._memory.search(question, self._max_memories))

        system = SYSTEM_PROMPT
        if skill is not None:
            system = (
                f"{SYSTEM_PROMPT}\n\n## Skill: {skill.name}\n\n{skill.instructions}"
            )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._user_message(question, context, memories)},
        ]
        text = self._model.complete(messages)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return Answer(
            text=text,
            used_vision=context.used_vision,
            memories=memories,
            latency_ms=elapsed_ms,
        )

    def _user_message(
        self,
        question: str,
        context: ScreenContext,
        memories: tuple[MemoryHit, ...],
    ) -> str:
        screen = context.text.strip() or "(no readable content)"
        lines = [
            "## Current screen",
            f"App: {context.app_name}",
            f"Window: {context.title}",
            f"Source: {'screenshot/vision' if context.used_vision else 'UI Automation'}",
        ]
        if context.error:
            lines.append(f"Capture warning: {context.error}")
        lines.append("")
        lines.append(screen[: self._max_context_chars])

        lines.extend(["", "## Retrieved memory"])
        if not memories:
            lines.append("(none)")
        for memory in memories:
            tags = ", ".join(memory.tags)
            lines.append(
                f"- [{memory.title}]({memory.path})"
                + (f" tags: {tags}" if tags else "")
            )
            lines.append(f"  {memory.snippet}")

        lines.extend(["", "## Question", question])
        return "\n".join(lines)
