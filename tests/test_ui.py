import unittest

from xenon.ui import CommandBoxModel


class CommandBoxModelTests(unittest.TestCase):
    def test_returns_an_answer_and_marks_done(self) -> None:
        model = CommandBoxModel(lambda question: f"answer to {question}")

        answer = model.submit("what is this?")

        self.assertEqual(answer, "answer to what is this?")
        self.assertEqual(model.status, "done")
        self.assertEqual(model.last_error, "")

    def test_ignores_blank_questions(self) -> None:
        calls: list[str] = []
        model = CommandBoxModel(lambda question: calls.append(question) or "x")

        self.assertIsNone(model.submit("   "))
        self.assertEqual(calls, [])
        self.assertEqual(model.status, "idle")

    def test_records_failures_without_crashing(self) -> None:
        def boom(question: str) -> str:
            raise RuntimeError("model offline")

        model = CommandBoxModel(boom)

        self.assertIsNone(model.submit("hello"))
        self.assertEqual(model.status, "error")
        self.assertIn("model offline", model.last_error)

    def test_clears_a_previous_error_on_success(self) -> None:
        attempts = {"count": 0}

        def flaky(question: str) -> str:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("slow")
            return "ok"

        model = CommandBoxModel(flaky)
        model.submit("first")
        model.submit("second")

        self.assertEqual(model.status, "done")
        self.assertEqual(model.last_error, "")


if __name__ == "__main__":
    unittest.main()
