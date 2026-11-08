import json
import unittest

from xenon import router
from xenon.router import route, worth_routing


def model(reply: str):
    calls = []

    def complete(messages):
        calls.append(messages)
        return reply

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


ALWAYS = lambda command: True  # noqa: E731


class RouteTests(unittest.TestCase):
    def test_model_translation_is_returned_when_a_parser_accepts_it(self) -> None:
        complete = model(json.dumps({"command": "brightness down"}))
        self.assertEqual(route("my eyes hurt", complete, ALWAYS), "brightness down")
        system = complete.calls[0][0]["content"]
        self.assertIn("brightness down", system)

    def test_unrecognised_commands_are_rejected(self) -> None:
        complete = model(json.dumps({"command": "format the hard drive"}))
        self.assertIsNone(route("clean up my disk", complete, lambda c: False))

    def test_null_and_garbage_replies_mean_answer_normally(self) -> None:
        self.assertIsNone(route("thanks xenon", model('{"command": null}'), ALWAYS))
        self.assertIsNone(route("thanks xenon", model("sure thing!"), ALWAYS))
        self.assertIsNone(route("thanks xenon", model('{"command": 5}'), ALWAYS))

    def test_json_inside_extra_text_is_found(self) -> None:
        reply = 'Here you go: {"command": "volume down"} done'
        self.assertEqual(route("too loud", model(reply), ALWAYS), "volume down")

    def test_model_failure_falls_back_quietly(self) -> None:
        def broken(messages):
            raise RuntimeError("network down")

        self.assertIsNone(route("too loud", broken, ALWAYS))

    def test_long_form_questions_skip_the_model(self) -> None:
        complete = model('{"command": "volume up"}')
        self.assertIsNone(route("explain how DDC/CI works", complete, ALWAYS))
        self.assertEqual(complete.calls, [])
        self.assertFalse(worth_routing("summarize this page"))
        self.assertTrue(worth_routing("bring up chrome"))
        self.assertTrue(worth_routing("who sings this"))


class SmartReplyTests(unittest.TestCase):
    def test_instant_parsers_win_without_a_model_call(self) -> None:
        from unittest import mock

        from xenon import brightness
        from xenon.__main__ import _smart_reply

        class Chat:
            def complete(self, messages):
                raise AssertionError("the model should not be asked")

        with mock.patch.object(brightness, "run", return_value="Brightness 90%."):
            reply, understood = _smart_reply("can you lower my brightness", Chat())
        self.assertEqual((reply, understood), ("Brightness 90%.", None))

    def test_model_reading_runs_the_real_command(self) -> None:
        from unittest import mock

        from xenon import brightness
        from xenon.__main__ import _smart_reply

        class Chat:
            def complete(self, messages):
                return '{"command": "brightness down"}'

        with mock.patch.object(brightness, "run", return_value="Brightness 80%.") as run:
            reply, understood = _smart_reply("screen is kinda bright ngl", Chat())
        self.assertEqual((reply, understood), ("Brightness 80%.", "brightness down"))
        self.assertEqual(run.call_args[0][0].action, "down")

    def test_questions_fall_through_to_a_normal_answer(self) -> None:
        from xenon.__main__ import _smart_reply

        class Chat:
            def complete(self, messages):
                return '{"command": null}'

        self.assertEqual(_smart_reply("what's on my screen", Chat()), (None, None))


class BrightnessPhrasingTests(unittest.TestCase):
    def test_natural_phrasings_parse_instantly(self) -> None:
        from xenon.brightness import parse

        cases = {
            "can you lower my brightness": ("down", 0),
            "turn my screen down a bit": ("down", 5),
            "reduce the brightness": ("down", 0),
            "make it darker": ("down", 0),
            "make my monitor brighter": ("up", 0),
            "turn the brightness up a lot": ("up", 25),
            "set my brightness to 20 percent": ("set", 20),
            "minimize brightness": ("min", 0),
        }
        for text, (action, value) in cases.items():
            command = parse(text)
            self.assertIsNotNone(command, text)
            self.assertEqual((command.action, command.value), (action, value), text)
        for text in ("dim the lights", "lower the volume", "what is my brightness"):
            self.assertIsNone(parse(text), text)


if __name__ == "__main__":
    unittest.main()
