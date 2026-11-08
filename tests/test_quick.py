import datetime
import unittest

from xenon import quick
from xenon.quick import QuickCommand, TimerBoard, describe, parse, parse_duration


class ParseTests(unittest.TestCase):
    def test_time_and_date(self) -> None:
        for text in ("what time is it", "What's the time?", "whats the time now", "tell me the time"):
            self.assertEqual(parse(text).kind, "time", text)
        for text in ("what's the date", "what day is it", "what is today's date"):
            self.assertEqual(parse(text).kind, "date", text)

    def test_volume(self) -> None:
        self.assertEqual(parse("volume up"), QuickCommand("volume_up", steps=5))
        self.assertEqual(parse("turn it down a lot"), QuickCommand("volume_down", steps=10))
        self.assertEqual(parse("turn up the volume a bit"), QuickCommand("volume_up", steps=2))
        self.assertEqual(parse("louder").kind, "volume_up")
        self.assertEqual(parse("mute").kind, "mute")

    def test_timers_in_many_phrasings(self) -> None:
        cases = {
            "set a timer for 5 minutes": (300, ""),
            "set a 10 minute timer": (600, ""),
            "set a five minute timer for tea": (300, "tea"),
            "set a timer for an hour and a half": (5400, ""),
            "set a timer for 2 and a half minutes": (150, ""),
            "set a timer for 1h 30m": (5400, ""),
            "set a timer for 90 seconds for the pasta": (90, "the pasta"),
            "start a timer for ten seconds": (10, ""),
            "remind me in 20 minutes to stretch": (1200, "stretch"),
            "set a timer for half an hour": (1800, ""),
        }
        for text, (seconds, label) in cases.items():
            command = parse(text)
            self.assertIsNotNone(command, text)
            self.assertEqual((command.kind, command.seconds, command.label), ("timer", seconds, label), text)

    def test_timer_management(self) -> None:
        self.assertEqual(parse("cancel the timer").kind, "cancel_timer")
        self.assertEqual(parse("stop all timers").kind, "cancel_timer")
        self.assertEqual(parse("how long is left").kind, "timer_left")

    def test_not_quick(self) -> None:
        for text in ("time is money", "set a timer", "open chrome", "play my liked songs", "stop the music"):
            self.assertIsNone(parse(text), text)

    def test_durations(self) -> None:
        self.assertEqual(parse_duration("two hours and ten minutes"), 7800)
        self.assertIsNone(parse_duration("three"))
        self.assertIsNone(parse_duration("90 hours"))  # over a day is refused


class FakeHandle:
    def __init__(self, seconds, callback):
        self.seconds = seconds
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class TimerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        self.handles: list[FakeHandle] = []

        def start(seconds, callback):
            handle = FakeHandle(seconds, callback)
            self.handles.append(handle)
            return handle

        self.board = TimerBoard(clock=lambda: self.now, start=start)
        self.fired = []
        self.board.listen(self.fired.append)

    def test_a_timer_fires_its_listeners_and_leaves_the_board(self) -> None:
        reply = quick.run(QuickCommand("timer", seconds=300, label="tea"), board=self.board)
        self.assertEqual(reply, "Timer set for tea: 5 minutes.")
        self.assertEqual(len(self.board.active()), 1)
        self.handles[0].callback()
        self.assertEqual(self.fired[0].name, "tea")
        self.assertEqual(self.board.active(), [])

    def test_time_left_and_cancel(self) -> None:
        quick.run(QuickCommand("timer", seconds=600), board=self.board)
        self.now += 90
        self.assertEqual(
            quick.run(QuickCommand("timer_left"), board=self.board),
            "8 minutes and 30 seconds left on your timer.",
        )
        self.assertEqual(quick.run(QuickCommand("cancel_timer"), board=self.board), "Cancelled the timer.")
        self.assertTrue(self.handles[0].cancelled)
        self.assertEqual(quick.run(QuickCommand("cancel_timer"), board=self.board), "There are no timers running.")

    def test_without_a_running_app_timers_are_refused_honestly(self) -> None:
        lonely = TimerBoard(start=lambda s, c: FakeHandle(s, c))
        reply = quick.run(QuickCommand("timer", seconds=60), board=lonely)
        self.assertIn("keep running", reply)
        self.assertEqual(lonely.active(), [])


class RunTests(unittest.TestCase):
    def test_time_and_date_answers(self) -> None:
        moment = datetime.datetime(2026, 11, 7, 9, 5)
        self.assertEqual(quick.run(QuickCommand("time"), now=lambda: moment), "It's 9:05 AM.")
        self.assertEqual(quick.run(QuickCommand("date"), now=lambda: moment), "It's Saturday, November 7, 2026.")

    def test_volume_presses_the_right_key_the_right_number_of_times(self) -> None:
        presses = []
        quick.run(QuickCommand("volume_down", steps=10), press=lambda code, n: presses.append((code, n)) or True)
        quick.run(QuickCommand("mute"), press=lambda code, n: presses.append((code, n)) or True)
        self.assertEqual(presses, [(quick.VK_VOLUME_DOWN, 10), (quick.VK_VOLUME_MUTE, 1)])

    def test_describe(self) -> None:
        self.assertEqual(describe(3600 + 120), "1 hour and 2 minutes")
        self.assertEqual(describe(61), "1 minute and 1 second")


class FastReplyTests(unittest.TestCase):
    def test_quick_commands_run_before_music_and_apps(self) -> None:
        from xenon.__main__ import _voice_fast_reply

        reply = _voice_fast_reply("what time is it xenon")
        self.assertTrue(reply.startswith("It's "), reply)


if __name__ == "__main__":
    unittest.main()
