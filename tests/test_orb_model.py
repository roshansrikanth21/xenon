"""Orb states for tasks: 'working' stays folded, 'confirm' pins it open."""
from __future__ import annotations

import unittest

from xenon.orb_model import NOTCH_IDLE_S, OrbModel, format_task_progress, step_fraction


def _notch_model() -> OrbModel:
    model = OrbModel(notch=True)
    model.idle_after = NOTCH_IDLE_S
    return model


def _run(model: OrbModel, seconds: float, dt: float = 0.05) -> None:
    for _ in range(int(round(seconds / dt))):
        model.step(dt)


class OrbTaskStateTests(unittest.TestCase):
    def test_progress_text_helpers(self) -> None:
        self.assertEqual(format_task_progress("Opening Notepad", 1, 2), "Step 1/2 · Opening Notepad")
        self.assertEqual(format_task_progress("  Opening   Notepad "), "Opening Notepad")
        self.assertAlmostEqual(step_fraction("Step 1/4 · Typing"), 0.25)
        self.assertIsNone(step_fraction("Typing"))
        self.assertIsNone(step_fraction("Step 1/0"))

    def test_working_state_stays_folded_with_progress(self) -> None:
        model = _notch_model()
        _run(model, NOTCH_IDLE_S + 1.0)
        self.assertGreaterEqual(model.dock, 0.999)
        model.set_state("working")
        model.set_task_progress("Opening Notepad", 1, 2)
        _run(model, 5.0)
        self.assertGreaterEqual(model.dock, 0.999)   # never unfolded
        self.assertEqual(model.progress_text, "Step 1/2 · Opening Notepad")
        self.assertAlmostEqual(model.progress_fraction, 0.5)
        model.set_task_progress("Typing text", 2, 2)
        _run(model, 1.0)
        self.assertGreaterEqual(model.dock, 0.999)
        # The shell's notch_info copy wins over the model's own line.
        model.notch_info = {"task": "Step 2/5 · Type text"}
        self.assertEqual(model.progress_text, "Step 2/5 · Type text")

    def test_confirm_pending_never_folds(self) -> None:
        model = _notch_model()
        _run(model, NOTCH_IDLE_S + 1.0)
        model.set_state("working")
        model.card_opened()
        model.set_state("confirm")
        _run(model, 1.0)
        self.assertLessEqual(model.dock, 0.001)      # unfolded for the card
        _run(model, 60.0)
        self.assertLessEqual(model.dock, 0.001)      # and pinned open
        self.assertFalse(model.is_idle)
        # Even if the state drops back to working, an open card pins it.
        model.set_state("working")
        _run(model, 10.0)
        self.assertLessEqual(model.dock, 0.001)

    def test_folds_within_3s_after_task_done_and_card_closed(self) -> None:
        model = _notch_model()
        model.card_opened()
        model.set_state("confirm")
        _run(model, 2.0)
        model.card_closed()
        model.set_state("working")
        model.set_task_progress("Sending", 2, 2)
        _run(model, 0.5)
        model.clear_task()
        model.set_state("idle")           # task done
        done_at = model.time
        _run(model, NOTCH_IDLE_S - 0.6)
        self.assertLess(model.dock, 0.05)      # not before the idle delay
        while not model.is_idle and model.time - done_at < 10:
            model.step(0.05)
        self.assertLessEqual(model.time - done_at, NOTCH_IDLE_S + 0.1)
        _run(model, 0.6)
        self.assertGreaterEqual(model.dock, 0.999)
        self.assertEqual(model.progress_text, "")

    def test_card_count_never_negative(self) -> None:
        model = _notch_model()
        model.card_closed()
        self.assertEqual(model.cards_open, 0)


class OrbRenderWorkingTests(unittest.TestCase):
    def test_folded_notch_draws_progress(self) -> None:
        try:
            from xenon.orb_render import OrbRenderer
        except Exception:
            self.skipTest("Pillow not installed")
        model = _notch_model()
        _run(model, NOTCH_IDLE_S + 1.0)
        model.set_state("working")
        model.set_task_progress("Opening Notepad", 1, 2)
        renderer = OrbRenderer(1.0)
        with_bar = renderer.render(model).tobytes()
        model.clear_task()
        model.set_state("idle")
        model.notch_info = {"clock": "9:41"}
        without = renderer.render(model).tobytes()
        self.assertNotEqual(with_bar, without)
        # A known step draws a static bar: identical frames, so the shell's
        # crc32 skip drops them.
        model.set_state("working")
        model.set_task_progress("Opening Notepad", 1, 2)
        model.notch_info = {}
        first = renderer.render(model).tobytes()
        second = renderer.render(model).tobytes()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
