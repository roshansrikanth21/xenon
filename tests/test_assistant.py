import json
import unittest
from pathlib import Path

from xenon.assistant import Assistant
from xenon.memory import MemoryHit
from xenon.screen import ScreenContext
from xenon.skills import Skill


class FakeObserver:
    def __init__(self, context: ScreenContext) -> None:
        self.context = context
        self.calls = 0

    def observe(self) -> ScreenContext:
        self.calls += 1
        return self.context


class FakeMemory:
    def __init__(self, hits: list[MemoryHit]) -> None:
        self.hits = hits
        self.queries: list[tuple[str, int]] = []

    def search(self, query: str, limit: int = 5) -> list[MemoryHit]:
        self.queries.append((query, limit))
        return self.hits[:limit]


class FakeModel:
    def __init__(self, answer: str = "Use UI Automation.") -> None:
        self.answer = answer
        self.messages: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> str:
        self.messages.append(messages)
        return self.answer


def hit(title: str = "Xenon plan", snippet: str = "Use [UI Automation] first.") -> MemoryHit:
    return MemoryHit(
        document_id="doc-1",
        title=title,
        path=r"C:\vault\projects\xenon.md",
        snippet=snippet,
        tags=("assistant",),
        source_id="src-1",
        source_kind="obsidian",
        source_root=r"C:\vault",
        score=-1.5,
    )


CONTEXT = ScreenContext(
    app_name="opera",
    title="deck.pptx - Opera",
    text="Slide 2: What is AI?",
    used_vision=True,
)


class AssistantTests(unittest.TestCase):
    def test_builds_a_prompt_with_screen_context_and_cited_memories(self) -> None:
        model = FakeModel()
        assistant = Assistant(FakeObserver(CONTEXT), FakeMemory([hit()]), model)

        answer = assistant.ask("what should I do next?")

        self.assertEqual(answer.text, "Use UI Automation.")
        prompt = json.dumps(model.messages[0])
        self.assertIn("what should I do next?", prompt)
        self.assertIn("deck.pptx - Opera", prompt)
        self.assertIn("Slide 2: What is AI?", prompt)
        self.assertIn("Xenon plan", prompt)
        self.assertIn("xenon.md", prompt)
        self.assertIn("UI Automation", prompt)

    def test_returns_the_memories_and_vision_flag_used_for_the_answer(self) -> None:
        memory = FakeMemory([hit()])
        assistant = Assistant(FakeObserver(CONTEXT), memory, FakeModel())

        answer = assistant.ask("next step")

        self.assertEqual(len(answer.memories), 1)
        self.assertTrue(answer.used_vision)
        self.assertEqual(memory.queries, [("next step", 5)])
        self.assertGreaterEqual(answer.latency_ms, 0)

    def test_answers_without_a_memory_store(self) -> None:
        model = FakeModel("No stored notes yet.")
        assistant = Assistant(FakeObserver(CONTEXT), None, model)

        answer = assistant.ask("what is this?")

        self.assertEqual(answer.text, "No stored notes yet.")
        self.assertEqual(answer.memories, ())

    def test_limits_memories_retrieved_per_question(self) -> None:
        memory = FakeMemory([hit(f"note {index}") for index in range(10)])
        assistant = Assistant(FakeObserver(CONTEXT), memory, FakeModel(), max_memories=3)

        answer = assistant.ask("summarize")

        self.assertEqual(len(answer.memories), 3)
        self.assertEqual(memory.queries, [("summarize", 3)])

    def test_rejects_an_empty_question(self) -> None:
        assistant = Assistant(FakeObserver(CONTEXT), None, FakeModel())

        with self.assertRaises(ValueError):
            assistant.ask("   ")

    def test_answers_from_ui_automation_context_without_a_screenshot(self) -> None:
        context = ScreenContext(
            app_name="Notepad",
            title="notes.md - Notepad",
            text="draft the release notes",
            used_vision=False,
        )
        model = FakeModel()

        answer = Assistant(FakeObserver(context), None, model).ask("what am I writing?")

        prompt = json.dumps(model.messages[0])
        self.assertIn("UI Automation", prompt)
        self.assertNotIn("screenshot/vision", prompt)
        self.assertFalse(answer.used_vision)

    def test_includes_a_loaded_skill_in_the_system_prompt(self) -> None:
        skill = Skill(
            name="screen-context",
            description="Read the active window.",
            directory=Path("skills/screen-context"),
            instructions="Always cite the window title.",
        )
        model = FakeModel()

        Assistant(FakeObserver(CONTEXT), None, model).ask("what is this?", skill=skill)

        prompt = json.dumps(model.messages[0])
        self.assertIn("Always cite the window title.", prompt)
        self.assertIn("Skill: screen-context", prompt)

    def test_surfaces_screen_errors_in_the_prompt(self) -> None:
        context = ScreenContext(
            app_name="opera",
            title="deck.pptx",
            text="",
            used_vision=True,
            error="vision unavailable: offline",
        )
        model = FakeModel()

        Assistant(FakeObserver(context), None, model).ask("what is on screen?")

        prompt = json.dumps(model.messages[0])
        self.assertIn("vision unavailable: offline", prompt)


if __name__ == "__main__":
    unittest.main()
