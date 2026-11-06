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
    "You are Xenon, a personal assistant working beside the user on their "
    "Windows desktop. The screen context is what the user is looking at right "
    "now. Your job is to help them move their current task forward, not to "
    "narrate the window.\n\n"
    "Answer like this by default:\n"
    "Next step: <one concrete action the user can take now>\n"
    "Why: <one sentence tied to the screen>\n"
    "Try: <the exact Xenon command, only when one would help>\n\n"
    "Add a second or third next step only if the first one clearly depends on "
    "it. Do not open with a summary of the screen and do not list files or "
    "controls unless the user asked what is visible. Keep the whole answer "
    "under 120 words.\n\n"
    "Use retrieved memory only when it is relevant and cite its source path in "
    "square brackets; use your own knowledge for general guidance. When the "
    "screen section says the pixels could not be read, do not guess at page "
    "contents or claim to see anything: work from the app and window title, "
    "give what help the question itself allows, and ask one focused question "
    "about what is actually on screen. Never invent screen contents or "
    "remembered facts."
)

BRIEF_PROMPT = (
    "You are Xenon, speaking out loud beside the user on their Windows desktop. "
    "The screen context is what they are looking at right now. Answer in one or "
    "two short sentences, under 45 words total, in plain spoken language. Give "
    "the single best next action; do not summarize the window and do not use "
    "markdown, lists, headings, or a 'Next step:' prefix. Use memory only when "
    "it clearly changes the answer, and name the source instead of reading a "
    "file path aloud. When the screen cannot be read, say so briefly and ask one "
    "focused question. Never invent screen contents or remembered facts."
)


@dataclass(frozen=True)
class Answer:
    text: str
    used_vision: bool
    memories: tuple[MemoryHit, ...]
    latency_ms: float
    screen_error: str = ""


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

    def ask(
        self,
        question: str,
        skill: Skill | None = None,
        brief: bool = False,
    ) -> Answer:
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")

        started = time.perf_counter()
        context = self._observer.observe()
        memories = ()
        if self._memory is not None:
            memories = tuple(self._memory.search(question, self._max_memories))

        system = BRIEF_PROMPT if brief else SYSTEM_PROMPT
        if skill is not None:
            system = (
                f"{system}\n\n## Skill: {skill.name}\n\n{skill.instructions}"
            )

        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": self._user_message(
                    question, context, memories, brief=brief
                ),
            },
        ]
        text = self._model.complete(messages)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return Answer(
            text=text,
            used_vision=context.used_vision,
            memories=memories,
            latency_ms=elapsed_ms,
            screen_error=context.error,
        )

    def _user_message(
        self,
        question: str,
        context: ScreenContext,
        memories: tuple[MemoryHit, ...],
        brief: bool = False,
    ) -> str:
        screen = context.text.strip()
        has_content = bool(screen)
        if context.used_vision and has_content:
            source = "screenshot/vision"
        elif has_content:
            source = "UI Automation (limited: window chrome only)"
        else:
            source = "window identity only"

        lines = [
            "## Current screen",
            f"App: {context.app_name}",
            f"Window: {context.title}",
            f"Source: {source}",
        ]
        if context.error:
            lines.append(
                "Screen limits: the pixels could not be read (" + context.error + ")."
            )
            lines.append(
                "Do not describe or invent visible content. Use only the app and "
                "window title above, and ask the user what is on screen if that "
                "would change your answer."
            )
        lines.append("")
        lines.append(screen[: self._max_context_chars] if has_content else "(no readable content)")

        lines.extend(["", "## Retrieved memory"])
        if not memories:
            lines.append("(none)")
        for memory in memories:
            if brief:
                # Spoken answers get a short name and snippet; paths and tags
                # waste tokens and sound wrong read aloud.
                lines.append(f"- {memory.title}")
                lines.append(f"  {memory.snippet[:240]}")
                continue
            tags = ", ".join(memory.tags)
            lines.append(
                f"- [{memory.title}]({memory.path})"
                + (f" tags: {tags}" if tags else "")
            )
            lines.append(f"  {memory.snippet}")

        lines.extend(["", "## Question", question])
        return "\n".join(lines)
