"""Confirm/choice/undo cards: the safety rules, without real windows.

The pure parts (CardModel, UndoTimer, noactivate_exstyle) are tested
directly. The Tk tests build cards on a withdrawn root with the show call
faked, so no card is ever mapped on screen.
"""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from xenon import orb_cards
from xenon.orb_cards import (
    ARMED_AFTER_DESTRUCTIVE_S,
    ARMED_AFTER_S,
    FORBIDDEN_KEYS,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    CardModel,
    UndoTimer,
    bound_keys,
    card_rows,
    field_box_lines,
    noactivate_exstyle,
    show_without_activation,
)


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeWatch:
    def __init__(self, verdict: bool | None) -> None:
        self._verdict = verdict
        self.started = 0
        self.stopped = 0

    def verdict(self):
        return self._verdict

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class FakeWin32:
    def __init__(self, foreground: int = 77, exstyle: int = 0x100) -> None:
        self.calls: list[tuple] = []
        self._foreground = foreground
        self._exstyle = exstyle

    def toplevel(self, widget) -> int:
        return 42

    def exstyle(self, hwnd: int) -> int:
        return self._exstyle

    def set_exstyle(self, hwnd: int, style: int) -> None:
        self.calls.append(("set_exstyle", hwnd, style))

    def foreground(self) -> int:
        return self._foreground

    def set_foreground(self, hwnd: int) -> None:
        self.calls.append(("set_foreground", hwnd))

    def show_noactivate(self, hwnd: int) -> None:
        self.calls.append(("show_noactivate", hwnd))

    def place(self, hwnd: int, x: int, y: int) -> None:
        self.calls.append(("place", hwnd, x, y))


class FakeTop:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def geometry(self, spec: str) -> None:
        self.calls.append(("geometry", spec))

    def deiconify(self) -> None:
        self.calls.append(("deiconify",))

    def update_idletasks(self) -> None:
        pass


def _pending(risk: str = "outward", fields=None, badges=()):
    from xenon.actions import Risk
    from xenon.approvals import Field, PendingAction

    return PendingAction(
        action_id="a1",
        kind="send_message",
        risk=Risk(risk),
        title="Send to Mom in WhatsApp",
        fields=tuple(fields or (Field("To", "Mom", "your words"), Field("Message", "on my way", "drafter"))),
        digest="d" * 64,
        badges=tuple(badges),
    )


class CardModelTests(unittest.TestCase):
    def test_confirm_not_armed_ignored(self) -> None:
        model = CardModel.for_risk("outward", shown_at=10.0)
        self.assertEqual(model.armed_after_s, ARMED_AFTER_S)
        self.assertFalse(model.armed(10.5))
        self.assertIsNone(model.click("confirm", 10.5, injected=False))
        self.assertEqual(model.ignored, ["not armed"])
        self.assertFalse(model.can_confirm(10.5, injected=False))
        self.assertEqual(model.click("confirm", 10.0 + ARMED_AFTER_S, injected=False), "approve")

    def test_injected_click_ignored(self) -> None:
        model = CardModel.for_risk("outward", shown_at=0.0)
        self.assertFalse(model.can_confirm(5.0, injected=True))
        self.assertIsNone(model.click("confirm", 5.0, injected=True))
        self.assertEqual(model.ignored, ["injected"])
        # Cancel may be injected: it can only stop something.
        self.assertEqual(model.click("cancel", 5.0, injected=True), "deny")

    def test_destructive_arms_slower_and_cancel_default(self) -> None:
        model = CardModel.for_risk("destructive", shown_at=0.0)
        self.assertEqual(model.armed_after_s, ARMED_AFTER_DESTRUCTIVE_S)
        self.assertGreater(model.armed_after_s, ARMED_AFTER_S)
        self.assertEqual(model.default_button, "cancel")
        self.assertFalse(model.armed(1.0))
        self.assertTrue(model.armed(1.5))
        # A caller can't speed a destructive card up.
        self.assertEqual(CardModel.for_risk("destructive", 0.0, armed_after_s=0.1).armed_after_s,
                         ARMED_AFTER_DESTRUCTIVE_S)
        # Unknown risk resolves to the stricter answer.
        self.assertEqual(CardModel.for_risk("weird", 0.0).default_button, "cancel")
        self.assertEqual(CardModel.for_risk("outward", 0.0).default_button, "confirm")

    def test_decided_card_ignores_further_clicks(self) -> None:
        model = CardModel.for_risk("outward", 0.0)
        model.decided = True
        self.assertIsNone(model.click("confirm", 9.0, injected=False))
        self.assertIsNone(model.click("cancel", 9.0, injected=False))

    def test_is_injected_input_fails_closed(self) -> None:
        with mock.patch.object(orb_cards.sys, "platform", "linux"):
            self.assertTrue(orb_cards.is_injected_input())
        if sys.platform == "win32":
            import ctypes

            with mock.patch.object(ctypes.windll.user32, "GetCurrentInputMessageSource",
                                   side_effect=OSError("boom"), create=True):
                self.assertTrue(orb_cards.is_injected_input())

    def test_click_verdict_prefers_hook_then_strict_fallback(self) -> None:
        self.assertTrue(orb_cards.click_was_injected(FakeWatch(True), lambda: False))
        self.assertFalse(orb_cards.click_was_injected(FakeWatch(False), lambda: True))
        self.assertTrue(orb_cards.click_was_injected(FakeWatch(None), lambda: True))
        self.assertTrue(orb_cards.click_was_injected(None, lambda: True))


class UndoTimerTests(unittest.TestCase):
    def test_undo_fires_once(self) -> None:
        timer = UndoTimer(5.0, started_at=0.0)
        self.assertEqual(timer.label(0.0), "Sending in 5…")
        self.assertIsNone(timer.tick(2.0))
        self.assertTrue(timer.undo())
        self.assertFalse(timer.undo())
        self.assertIsNone(timer.tick(10.0))  # undone: never elapses too
        self.assertEqual(timer.outcome, "undo")

        timer = UndoTimer(5.0, started_at=0.0)
        self.assertEqual(timer.tick(5.0), "elapse")
        self.assertIsNone(timer.tick(6.0))
        self.assertFalse(timer.undo())   # too late
        self.assertEqual(timer.outcome, "elapse")


class WindowStyleTests(unittest.TestCase):
    def test_card_shown_without_activation(self) -> None:
        style = noactivate_exstyle(0x100)
        self.assertTrue(style & WS_EX_NOACTIVATE)
        self.assertTrue(style & WS_EX_TOOLWINDOW)
        self.assertTrue(style & WS_EX_TOPMOST)
        self.assertTrue(style & 0x100)  # existing bits kept

        win32, top = FakeWin32(foreground=77), FakeTop()
        hwnd = show_without_activation(top, 300, 40, win32)
        self.assertEqual(hwnd, 42)
        names = [call[0] for call in win32.calls]
        self.assertEqual(win32.calls[0], ("set_exstyle", 42, noactivate_exstyle(0x100)))
        # Shown no-activate and placed only after the style is set.
        self.assertLess(names.index("set_exstyle"), names.index("show_noactivate"))
        self.assertLess(names.index("show_noactivate"), names.index("place"))
        self.assertIn(("place", 42, 300, 40), win32.calls)
        # Mapped off screen first, so any activation Tk does is invisible.
        self.assertEqual(top.calls[0], ("geometry", "+-32000+-32000"))


class FieldRowTests(unittest.TestCase):
    def test_rows_strip_invisible_and_tag_source(self) -> None:
        from xenon.approvals import Field

        rows = card_rows([Field("To", "Mo​m‮", "your words"), Field("Body", "hi", "page")])
        self.assertEqual(rows[0].value, "Mom")
        self.assertEqual(rows[0].tag, "your words")
        self.assertEqual(rows[1].tag, "page-authored")
        unknown = card_rows([SimpleNamespace(label="X", value="y", source="model")])
        self.assertEqual(unknown[0].tag, "page-authored")
        heard = card_rows([Field("Text", "hi", "heard")])
        self.assertEqual(heard[0].tag, "heard")  # a transcript is never "your words"

    def test_not_in_your_request_badge_is_red(self) -> None:
        from xenon.approvals import NOT_IN_REQUEST

        self.assertEqual(orb_cards.badge_color(NOT_IN_REQUEST), orb_cards.BADGE_RED)
        self.assertEqual(orb_cards.badge_color("plan read your screen"), orb_cards.BADGE_AMBER)

    def test_long_values_scroll_at_twelve_lines(self) -> None:
        from xenon.approvals import Field

        rows = card_rows([Field("Message", "word " * 400, "drafter")])
        lines, scrolls = field_box_lines(rows)
        self.assertEqual(lines, 12)
        self.assertTrue(scrolls)
        self.assertEqual(field_box_lines(card_rows([Field("To", "Mom", "your words")])), (1, False))


def _tk_root():
    """A hidden Tk root. Tcl sometimes fails to read its own library on the
    first try, so it is retried; on Windows (always a desktop here) a Tk
    that never starts fails the test rather than skipping the safety checks."""
    import sys
    import time as _time

    try:
        import tkinter
    except Exception:
        return None
    error = None
    for _attempt in range(5):
        try:
            root = tkinter.Tk()
        except Exception as caught:
            error = caught
            _time.sleep(0.05)
            continue
        root.withdraw()
        return root
    if sys.platform == "win32":
        raise AssertionError(f"Tk would not start: {error}")
    return None


class TkCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _tk_root()
        if self.root is None:
            self.skipTest("no display for Tk")
        self.shown: list = []
        patcher = mock.patch.object(orb_cards, "show_without_activation",
                                    side_effect=lambda top, x, y, win32=None: self.shown.append((x, y)) or 1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        if self.root is not None:
            self.root.destroy()

    def _confirm(self, risk="outward", verdict=False):
        self.clock = Clock(100.0)
        self.decisions: list[str] = []
        self.logs: list[str] = []
        self.watch = FakeWatch(verdict)
        card = orb_cards.ConfirmCard(self.root, _pending(risk), self.decisions.append, (600, 80),
                                     watch=self.watch, clock=self.clock, log=self.logs.append,
                                     injected=lambda: True)
        self.addCleanup(card.close)
        return card

    def _click(self, card, name: str) -> None:
        x0, y0, x1, y1 = card._boxes()[name]
        event = SimpleNamespace(x=(x0 + x1) / 2, y=(y0 + y1) / 2)
        card._press(event)
        card._release(event)

    def test_injected_click_ignored(self) -> None:
        card = self._confirm(verdict=True)
        self.clock.now += 5
        self._click(card, "confirm")
        self.assertEqual(self.decisions, [])
        self.assertEqual(len(self.logs), 1)
        self.assertFalse(card.closed)
        self.watch._verdict = False
        self._click(card, "confirm")
        self.assertEqual(self.decisions, ["approve"])
        self.assertTrue(card.closed)

    def test_confirm_not_armed_ignored(self) -> None:
        card = self._confirm()
        self.clock.now += 0.3
        self._click(card, "confirm")
        self.assertEqual(self.decisions, [])
        self.clock.now += 1.0
        self._click(card, "confirm")
        self.assertEqual(self.decisions, ["approve"])

    def test_enter_never_confirms(self) -> None:
        card = self._confirm()
        keys = bound_keys(card.top)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(key, keys)
        # The field box carries no class bindings (Text's own <Return>).
        self.assertNotIn("Text", card.fields.bindtags())
        self.assertEqual(self.root.bind_all("<Return>"), "")
        self.assertEqual(self.watch.started, 1)
        self.assertEqual(len(self.shown), 1)

    def test_escape_denies_only_while_hovered(self) -> None:
        card = self._confirm()
        card._escape()
        self.assertEqual(self.decisions, [])
        card._set_hover(True)
        card._escape()
        self.assertEqual(self.decisions, ["deny"])
        self.assertEqual(self.watch.stopped, 1)

    def test_destructive_arms_slower_and_cancel_default(self) -> None:
        card = self._confirm(risk="destructive")
        self.assertEqual(card.model.default_button, "cancel")
        self.clock.now += 1.0
        self._click(card, "confirm")
        self.assertEqual(self.decisions, [])
        self._click(card, "cancel")
        self.assertEqual(self.decisions, ["deny"])

    def test_choice_card_numbered_and_dismiss_picks_none(self) -> None:
        picks: list = []
        card = orb_cards.ChoiceCard(self.root, "Which Mom?", ["Mom (mobile)", "Mom (work)"], picks.append,
                                    (600, 80), watch=FakeWatch(False))
        self.assertEqual(len(card.rows), 3)  # two choices + "None of these"
        self.assertTrue(card.pick_words("the second one"))
        self.assertEqual(picks, ["Mom (work)"])
        card.dismiss()
        self.assertEqual(picks, ["Mom (work)"])  # once only
        other = orb_cards.ChoiceCard(self.root, "Pick", ["a"], picks.append, (0, 0), watch=FakeWatch(True),
                                     log=lambda _m: None)
        other._click("a")       # injected: ignored
        other.dismiss()
        self.assertEqual(picks, ["Mom (work)", None])

    def test_unanswered_choice_card_cancels_itself(self) -> None:
        import time as _time

        picks: list = []
        card = orb_cards.ChoiceCard(self.root, "Still working. Start it?", ["Queue it"], picks.append,
                                    (600, 80), watch=FakeWatch(False), timeout_s=0.05)
        deadline = _time.monotonic() + 3
        while not card.closed and _time.monotonic() < deadline:
            self.root.update()
            _time.sleep(0.01)
        self.assertTrue(card.closed)
        self.assertEqual(picks, [None])  # the default is cancel
        self.assertEqual(orb_cards.ChoiceCard.__init__.__defaults__[-1], orb_cards.CHOICE_TIMEOUT_S)
        self.assertGreater(orb_cards.CHOICE_TIMEOUT_S, 0)

    def test_undo_fires_once(self) -> None:
        clock = Clock(0.0)
        undone: list[str] = []
        strip = orb_cards.UndoStrip(self.root, 5, lambda: undone.append("undo"), lambda: undone.append("elapse"),
                                    (600, 80), clock=clock)
        strip.undo()
        strip.undo()
        clock.now = 10
        strip._tick()
        self.assertEqual(undone, ["undo"])

        strip = orb_cards.UndoStrip(self.root, 5, lambda: undone.append("undo"), lambda: undone.append("elapse"),
                                    (600, 80), clock=clock)
        clock.now = 20
        strip._tick()
        strip._tick()
        strip.undo()
        self.assertEqual(undone, ["undo", "elapse"])


if __name__ == "__main__":
    unittest.main()
