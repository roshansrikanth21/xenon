import unittest

from xenon.voice import VoiceLoop
from xenon.wake import WakeGate


class VoiceLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = WakeGate("xenon", clock=lambda: 100.0)
        self.transcripts: list[str | None] = []
        self.spoken: list[str] = []
        self.commands: list[str] = []

    def make_loop(self, handler=None) -> VoiceLoop:
        def on_command(command: str) -> str:
            self.commands.append(command)
            if handler is not None:
                return handler(command)
            return "done"

        return VoiceLoop(
            gate=self.gate,
            transcribe=lambda: self.transcripts.pop(0) if self.transcripts else None,
            speak=self.spoken.append,
            on_command=on_command,
        )

    def test_ignores_silence(self) -> None:
        loop = self.make_loop()

        self.assertIsNone(loop.tick())

    def test_ignores_speech_without_the_wake_word(self) -> None:
        self.transcripts = ["open notepad"]
        loop = self.make_loop()

        self.assertIsNone(loop.tick())
        self.assertEqual(self.commands, [])

    def test_speaks_the_reply_after_one_command(self) -> None:
        self.transcripts = ["xenon what is on my screen?"]
        loop = self.make_loop(lambda command: "a slide deck")

        self.assertEqual(loop.tick(), "a slide deck")
        self.assertEqual(self.commands, ["what is on my screen?"])
        self.assertEqual(self.spoken, ["a slide deck"])
        self.assertEqual(loop.tick(), None)

    def test_own_speech_cannot_retrigger_the_wake_word(self) -> None:
        self.transcripts = ["xenon summarize this"]
        loop = self.make_loop()

        def handler(command: str) -> str:
            # While handling/speaking, an echo of our own voice must be ignored.
            self.assertIsNone(self.gate.feed("xenon summarize this"))
            return "summary"

        self.transcripts = ["xenon summarize this"]
        loop = VoiceLoop(
            gate=self.gate,
            transcribe=lambda: self.transcripts.pop(0) if self.transcripts else None,
            speak=self.spoken.append,
            on_command=handler,
        )

        self.assertEqual(loop.tick(), "summary")

    def test_gate_is_not_muted_after_a_failure(self) -> None:
        self.transcripts = ["xenon do something"]
        attempts = {"count": 0}

        def flaky(command: str) -> str:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("model offline")
            return "done"

        loop = self.make_loop(flaky)

        self.assertIsNone(loop.tick())
        self.assertFalse(self.gate.speaking)
        self.transcripts = ["xenon try again"]
        self.assertEqual(loop.tick(), "done")

    def test_custom_handler_result_is_returned(self) -> None:
        self.transcripts = ["xenon hello"]
        loop = self.make_loop(lambda command: f"you said {command}")

        self.assertEqual(loop.tick(), "you said hello")


if __name__ == "__main__":
    unittest.main()
