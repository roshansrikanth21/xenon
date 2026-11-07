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


class AlwaysListeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.gate = WakeGate(
            "xenon", clock=lambda: self.now, always=True
        )

    def test_any_sentence_dispatches_without_a_wake_word(self) -> None:
        self.assertEqual(self.gate.feed("open notepad"), "open notepad")

    def test_wake_word_is_stripped_when_spoken(self) -> None:
        self.assertEqual(self.gate.feed("xenon open notepad"), "open notepad")

    def test_a_bare_wake_word_does_not_dispatch(self) -> None:
        self.assertIsNone(self.gate.feed("xenon"))

    def test_every_sentence_dispatches_until_speaking(self) -> None:
        self.assertEqual(self.gate.feed("open chrome"), "open chrome")
        self.assertEqual(self.gate.feed("search for cats"), "search for cats")

        self.gate.set_speaking(True)
        self.assertIsNone(self.gate.feed("open chrome"))

        self.gate.set_speaking(False)
        self.assertEqual(self.gate.feed("open chrome"), "open chrome")

    def test_always_property_reports_the_mode(self) -> None:
        self.assertTrue(self.gate.always)
        self.assertFalse(WakeGate("xenon").always)


class WakeWordAnywhereTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = WakeGate("xenon", aliases=("zenon", "see none"))

    def test_wake_word_at_the_end(self) -> None:
        self.assertEqual(self.gate.feed("play my liked music xenon"), "play my liked music")
        self.assertEqual(self.gate.feed("Play my liked music, Xenon."), "Play my liked music")

    def test_wake_word_in_the_middle(self) -> None:
        self.assertEqual(self.gate.feed("can you, xenon, open notepad"), "can you open notepad")

    def test_greeting_before_the_wake_word_is_dropped(self) -> None:
        self.assertEqual(self.gate.feed("Hey Xenon, open chrome"), "open chrome")
        self.assertEqual(self.gate.feed("okay xenon what time is it"), "what time is it")

    def test_question_mark_survives(self) -> None:
        self.assertEqual(self.gate.feed("what is on my screen, xenon?"), "what is on my screen")
        self.assertEqual(self.gate.feed("Zenon what is on my screen?"), "what is on my screen?")

    def test_multi_word_alias_at_the_end(self) -> None:
        self.assertEqual(self.gate.feed("what time is it see none"), "what time is it")

    def test_part_of_a_longer_word_does_not_wake(self) -> None:
        self.assertIsNone(self.gate.feed("xenonic power levels"))

    def test_wake_word_alone_with_punctuation_arms(self) -> None:
        self.assertIsNone(self.gate.feed("Xenon."))
        self.assertTrue(self.gate.armed)
        self.assertEqual(self.gate.feed("open chrome"), "open chrome")

    def test_said_twice_reaches_the_model_once(self) -> None:
        self.assertEqual(self.gate.feed("xenon open chrome xenon"), "open chrome")


if __name__ == "__main__":
    unittest.main()
