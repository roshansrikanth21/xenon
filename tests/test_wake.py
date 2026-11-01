import unittest

from xenon.wake import WakeGate


class WakeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.gate = WakeGate("xenon", ttl_s=8.0, clock=lambda: self.now)

    def feed(self, text: str) -> str | None:
        return self.gate.feed(text)

    def test_wake_word_alone_does_not_dispatch(self) -> None:
        self.assertIsNone(self.feed("xenon"))

    def test_wake_word_arms_the_next_utterance(self) -> None:
        self.feed("Xenon")

        self.assertEqual(self.feed("open notepad"), "open notepad")

    def test_wake_word_plus_command_dispatches_once(self) -> None:
        self.assertEqual(self.feed("xenon open notepad"), "open notepad")

        self.assertIsNone(self.feed("open notepad"))

    def test_command_without_wake_word_is_ignored(self) -> None:
        self.assertIsNone(self.feed("open notepad"))

    def test_wake_word_with_punctuation_is_tolerated(self) -> None:
        self.assertEqual(self.feed("Xenon, what is this?"), "what is this?")

    def test_speaking_suppresses_wake_and_commands(self) -> None:
        self.gate.set_speaking(True)

        self.assertIsNone(self.feed("xenon open notepad"))
        self.assertIsNone(self.feed("open notepad"))

        self.gate.set_speaking(False)
        self.assertIsNone(self.feed("open notepad"))

    def test_armed_state_expires(self) -> None:
        self.feed("xenon")
        self.now += 9.0

        self.assertIsNone(self.feed("open notepad"))

    def test_armed_state_survives_within_the_window(self) -> None:
        self.feed("xenon")
        self.now += 7.0

        self.assertEqual(self.feed("open notepad"), "open notepad")

    def test_custom_multi_word_wake_phrase(self) -> None:
        gate = WakeGate("hey xenon", clock=lambda: self.now)

        self.assertEqual(gate.feed("Hey Xenon, summarize this"), "summarize this")

    def test_empty_transcript_is_ignored(self) -> None:
        self.assertIsNone(self.feed("   "))


class WakeAliasTests(unittest.TestCase):
    """Whisper does not always spell an invented name the way its owner does."""

    def test_aliases_are_accepted(self) -> None:
        gate = WakeGate("xenon", aliases=("zenon", "see none"))

        self.assertEqual(gate.feed("Zenon, open notepad"), "open notepad")
        self.assertEqual(gate.feed("See none open notepad"), "open notepad")

    def test_the_primary_wake_word_still_works(self) -> None:
        gate = WakeGate("xenon", aliases=("zenon",))

        self.assertEqual(gate.feed("xenon open notepad"), "open notepad")

    def test_blank_aliases_are_ignored(self) -> None:
        gate = WakeGate("xenon", aliases=("", "   "))

        self.assertIsNone(gate.feed("open notepad"))
        self.assertEqual(gate.feed("xenon open notepad"), "open notepad")

    def test_an_alias_only_arms_like_the_wake_word(self) -> None:
        gate = WakeGate("xenon", aliases=("zenon",))

        self.assertIsNone(gate.feed("zenon"))
        self.assertEqual(gate.feed("summarize this"), "summarize this")


if __name__ == "__main__":
    unittest.main()
