import unittest

from xenon import fastpath


class OpenCommandTests(unittest.TestCase):
    def test_maps_a_known_app_to_its_executable_and_title(self) -> None:
        command = fastpath.parse("open notepad")

        self.assertIsNotNone(command)
        self.assertEqual(command.kind, "open_app")
        self.assertEqual(command.target, "notepad")
        self.assertEqual(command.label, "Notepad")

    def test_accepts_politeness_the_article_and_the_wake_word(self) -> None:
        for phrase in (
            "please open the calculator",
            "xenon open the calculator",
            "hey xenon, launch calculator",
        ):
            with self.subTest(phrase=phrase):
                command = fastpath.parse(phrase)
                self.assertIsNotNone(command)
                self.assertEqual(command.target, "calc")

    def test_accepts_a_multiword_alias(self) -> None:
        command = fastpath.parse("launch visual studio code")

        self.assertIsNotNone(command)
        self.assertEqual(command.target, "code")
        self.assertEqual(command.label, "Visual Studio Code")

    def test_allows_an_unknown_single_executable_token(self) -> None:
        command = fastpath.parse("open 7-zip")

        self.assertIsNotNone(command)
        self.assertEqual(command.target, "7-zip")

    def test_rejects_a_compound_task(self) -> None:
        self.assertIsNone(fastpath.parse("open notepad and type my notes"))

    def test_rejects_a_task_that_starts_with_open(self) -> None:
        for phrase in (
            "start writing a poem",
            "open my email",
            "open a note",
            "start the build",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(fastpath.parse(phrase))

    def test_rejects_an_empty_phrase(self) -> None:
        self.assertIsNone(fastpath.parse("   "))


class FocusCommandTests(unittest.TestCase):
    def test_switches_to_a_named_window(self) -> None:
        command = fastpath.parse("switch to chrome")

        self.assertIsNotNone(command)
        self.assertEqual(command.kind, "focus_window")
        self.assertEqual(command.target, "chrome")

    def test_strips_articles_and_the_word_window(self) -> None:
        command = fastpath.parse("focus the bank window")

        self.assertIsNotNone(command)
        self.assertEqual(command.target, "bank")

    def test_rejects_a_pronoun(self) -> None:
        self.assertIsNone(fastpath.parse("switch to it"))


class SearchCommandTests(unittest.TestCase):
    def test_recognises_search_phrasings(self) -> None:
        for phrase, query in (
            ("search for python asyncio", "python asyncio"),
            ("look up the weather", "the weather"),
            ("google best pizza", "best pizza"),
        ):
            with self.subTest(phrase=phrase):
                command = fastpath.parse(phrase)
                self.assertIsNotNone(command)
                self.assertEqual(command.kind, "search")
                self.assertEqual(command.target, query)

    def test_rejects_a_pronoun_query(self) -> None:
        self.assertIsNone(fastpath.parse("search for it"))

    def test_search_has_no_desktop_action(self) -> None:
        command = fastpath.parse("google rust ownership")

        self.assertIsNotNone(command)
        self.assertIsNone(command.action())


class ActionTests(unittest.TestCase):
    def test_open_action_carries_the_title_hint(self) -> None:
        action = fastpath.parse("open calculator").action()

        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "open_app")
        self.assertEqual(action.target, "calc")
        self.assertEqual(action.value, "Calculator")

    def test_focus_action_uses_the_window_fragment(self) -> None:
        action = fastpath.parse("switch to chrome").action()

        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "focus_window")
        self.assertEqual(action.target, "chrome")


class PoliteRequestTests(unittest.TestCase):
    def test_can_you_and_please_are_ignored(self) -> None:
        for text in (
            "Can you open calculator?",
            "could you please open calculator",
            "open calculator please",
            "I need you to open calculator",
        ):
            command = fastpath.parse(text)
            self.assertIsNotNone(command, text)
            self.assertEqual(command.target, "calc", text)

    def test_a_polite_compound_task_still_goes_to_the_model(self) -> None:
        self.assertIsNone(fastpath.parse("can you open my email and reply"))


if __name__ == "__main__":
    unittest.main()
