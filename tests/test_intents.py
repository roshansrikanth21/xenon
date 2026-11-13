from __future__ import annotations

import unittest

from xenon.intents import Intent, is_escape, parse


class ParseTests(unittest.TestCase):
    def test_do_goal(self) -> None:
        intent = parse("do open the downloads folder")
        self.assertEqual(intent, Intent("do", goal="open the downloads folder", raw="do open the downloads folder"))
        self.assertEqual(parse("Please do send Sam the notes.").goal, "send Sam the notes")
        self.assertEqual(parse("ok, do rename the screenshots").goal, "rename the screenshots")

    def test_questions_and_negations_are_not_do(self) -> None:
        for text in ("can you do the dishes", "do you know the time", "do you have a minute?",
                     "do not send it", "do it", "do that", "don't do anything", "do", "do  ",
                     "what do I do next", "how do i open notepad", "do nothing"):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_stop_cancel_next_back(self) -> None:
        cases = {
            "stop": "stop", "Stop.": "stop", "stop the task": "stop", "please stop": "stop",
            "cancel": "cancel", "never mind": "cancel", "nevermind": "cancel", "exit": "cancel",
            "forget it": "cancel", "next": "next", "next step": "next", "back": "back",
            "go back": "back", "previous": "back",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                intent = parse(text)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.kind, kind)

    def test_other_commands_fall_through(self) -> None:
        for text in ("stop the music", "next song", "go back to the previous tab", "cancel my meeting",
                     "what's next", "", "   ", "hello"):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_invisible_characters_do_not_change_meaning(self) -> None:
        self.assertEqual(parse("st​op").kind, "stop")
        self.assertIsNone(parse("do​ you know"))


class EscapeTests(unittest.TestCase):
    def test_t0_command_escapes_focus(self) -> None:
        # A task waiting in ask() must not swallow these: they leave its focus.
        for text in ("stop", "cancel", "exit", "never mind", "Stop!", "cancel that"):
            with self.subTest(text=text):
                self.assertTrue(is_escape(text))

    def test_answers_do_not_escape(self) -> None:
        for text in ("report.pdf", "the second one", "next", "back", "do open notepad",
                     "stop the music", "cancel my meeting", ""):
            with self.subTest(text=text):
                self.assertFalse(is_escape(text))


if __name__ == "__main__":
    unittest.main()
