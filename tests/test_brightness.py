import os
import tempfile
import unittest

from xenon import brightness
from xenon.brightness import BrightnessCommand, HardwareIgnored, apply_detailed, overlay_alpha, parse


class FakeDisplay:
    def __init__(self, name, kind="monitor", level=100, rect=(0, 0, 100, 100), ignores=False):
        self.name, self.kind, self.level, self.rect, self.ignores = name, kind, level, rect, ignores
        self.writes = []

    def get(self):
        return self.level

    def set(self, value):
        self.writes.append(value)
        if self.ignores:
            raise HardwareIgnored("locked")
        self.level = value


class ParseTests(unittest.TestCase):
    def test_phrases(self) -> None:
        cases = {
            "dim my monitor": BrightnessCommand("min"),
            "minimize brightness": BrightnessCommand("min"),
            "lowest brightness": BrightnessCommand("min"),
            "max brightness": BrightnessCommand("max"),
            "brightness 30": BrightnessCommand("set", 30),
            "set brightness to 20 percent": BrightnessCommand("set", 20),
            "brightness up": BrightnessCommand("up"),
            "make the screen darker": BrightnessCommand("down"),
            "restore brightness": BrightnessCommand("restore"),
        }
        for text, expected in cases.items():
            self.assertEqual(parse(text), expected, text)
        self.assertIsNone(parse("dim the lights"))
        self.assertIsNone(parse("open chrome"))


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = os.path.join(tempfile.mkdtemp(), "brightness.json")

    def run_action(self, action, displays, value=0):
        return apply_detailed(action, value, displays=displays, state_path=self.state)

    def test_toggle_dims_then_restores_the_previous_levels(self) -> None:
        monitor, laptop = FakeDisplay("Monitor", level=80), FakeDisplay("Laptop", "laptop", level=55)
        self.assertIn("minimum", self.run_action("toggle", [monitor, laptop]).reply)
        self.assertEqual((monitor.level, laptop.level), (0, 0))
        self.assertEqual(self.run_action("toggle", [monitor, laptop]).reply, "Brightness restored.")
        self.assertEqual((monitor.level, laptop.level), (80, 55))

    def test_restore_never_dims(self) -> None:
        monitor = FakeDisplay("Monitor", level=90)
        self.assertEqual(self.run_action("restore", [monitor]).reply, "Your screens aren't dimmed.")
        self.assertEqual(monitor.writes, [])

    def test_steps_and_clamping(self) -> None:
        monitor = FakeDisplay("Monitor", level=95)
        self.run_action("up", [monitor])
        self.assertEqual(monitor.level, 100)
        self.run_action("down", [monitor])
        self.assertEqual(monitor.level, 90)
        self.run_action("set", [monitor], 250)
        self.assertEqual(monitor.level, 100)

    def test_a_monitor_that_ignores_ddc_gets_a_software_overlay(self) -> None:
        locked = FakeDisplay("Locked", rect=(0, 0, 3440, 1440), ignores=True)
        outcome = self.run_action("min", [locked])
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.overlays, [((0, 0, 3440, 1440), 0)])
        self.assertEqual(brightness.software_overlays(self.state), [((0, 0, 3440, 1440), 0)])
        # Steps continue from the software level, not the stuck hardware one.
        outcome = self.run_action("up", [locked])
        self.assertEqual(outcome.overlays, [((0, 0, 3440, 1440), 10)])
        # Back to full brightness removes the overlay.
        outcome = self.run_action("max", [locked])
        self.assertEqual(outcome.overlays, [])

    def test_text_reply_explains_the_software_fallback(self) -> None:
        locked = FakeDisplay("Locked", ignores=True)
        reply = brightness.apply("min", displays=[locked], state_path=self.state)
        self.assertIn("HDR", reply)

    def test_no_displays(self) -> None:
        outcome = self.run_action("min", [])
        self.assertFalse(outcome.ok)
        self.assertIn("DDC/CI", outcome.reply)

    def test_overlay_alpha_keeps_minimum_readable(self) -> None:
        self.assertEqual(overlay_alpha(100), 0.0)
        self.assertLess(overlay_alpha(0), 0.9)
        self.assertGreater(overlay_alpha(0), overlay_alpha(50))


class TargetTests(unittest.TestCase):
    def test_monitor_only_by_default_and_configurable(self) -> None:
        from unittest import mock

        displays = [FakeDisplay("Monitor"), FakeDisplay("Laptop", "laptop")]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XENON_BRIGHTNESS_TARGET", None)
            self.assertEqual([d.name for d in brightness.targets(displays)], ["Monitor"])
        with mock.patch.dict(os.environ, {"XENON_BRIGHTNESS_TARGET": "all"}):
            self.assertEqual(len(brightness.targets(displays)), 2)
        with mock.patch.dict(os.environ, {"XENON_BRIGHTNESS_TARGET": "laptop"}):
            self.assertEqual([d.name for d in brightness.targets(displays)], ["Laptop"])


class FastReplyTests(unittest.TestCase):
    def test_brightness_is_on_the_fast_path(self) -> None:
        from unittest import mock

        from xenon.__main__ import _voice_fast_reply

        with mock.patch.object(brightness, "run", return_value="Dimmed.") as run:
            self.assertEqual(_voice_fast_reply("dim my monitor xenon"), "Dimmed.")
        self.assertEqual(run.call_args[0][0], BrightnessCommand("min"))


if __name__ == "__main__":
    unittest.main()
