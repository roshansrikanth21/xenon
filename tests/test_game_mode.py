import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from xenon import game_mode


class ConfigTests(unittest.TestCase):
    def test_defaults_and_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.json"
            self.assertEqual(game_mode.load(path), game_mode.GameModeConfig())
            game_mode.save(game_mode.GameModeConfig(["chrome.exe"], True), path)
            self.assertEqual(game_mode.load(path), game_mode.GameModeConfig(["chrome.exe"], True))
            path.write_text("not json")
            self.assertEqual(game_mode.load(path), game_mode.GameModeConfig())
            path.write_text(json.dumps({"close_apps": "chrome.exe"}))
            self.assertEqual(game_mode.load(path).close_apps, [])

    def test_protected_apps_are_never_closed(self):
        out = game_mode.closable(["Chrome", "notepad.exe", "EXPLORER.EXE", "ApplicationFrameHost.exe",
                                  "python.exe", " discord.exe ", ""])
        self.assertEqual(out, {"chrome.exe", "discord.exe"})

    def test_nothing_listed_closes_nothing(self):
        self.assertEqual(game_mode.close_apps([]), [])
        self.assertEqual(game_mode.close_apps(["notepad.exe"]), [])


class AutoWatchTests(unittest.TestCase):
    def watch(self, states):
        it = iter(states)
        return game_mode.AutoWatch(enter_s=4, leave_s=8, probe=lambda: next(it))

    def test_enters_after_a_steady_full_screen_and_leaves_after_it_ends(self):
        w = self.watch([True, True, True, False, False, False])
        self.assertIsNone(w.tick(0, False))
        self.assertIsNone(w.tick(2, False))
        self.assertEqual(w.tick(4, False), "enter")
        self.assertIsNone(w.tick(10, True))
        self.assertIsNone(w.tick(14, True))
        self.assertEqual(w.tick(18, True), "leave")

    def test_a_flash_of_full_screen_does_nothing(self):
        w = self.watch([True, False, True])
        self.assertIsNone(w.tick(0, False))
        self.assertIsNone(w.tick(3, False))
        self.assertIsNone(w.tick(5, False))

    def test_never_leaves_a_mode_entered_by_hand(self):
        w = self.watch([False] * 5)
        for t in range(0, 40, 9):
            self.assertIsNone(w.tick(t, True))

    def test_leaving_by_hand_snoozes_until_the_game_closes(self):
        w = self.watch([True, True, True, False, True, True, True])
        w.tick(0, False)
        self.assertEqual(w.tick(4, False), "enter")
        w.left_by_hand()
        self.assertIsNone(w.tick(5, False))
        self.assertIsNone(w.tick(20, False))           # still the same game: stays out
        self.assertIsNone(w.tick(21, False))           # game closed: snooze ends
        self.assertIsNone(w.tick(22, False))
        self.assertEqual(w.tick(26, False), "enter")    # the next game


class _FakeWidget:
    def __init__(self):
        self.running = True
        self.stopped = threading.Event()

    def stop(self):
        self.running = False
        self.stopped.set()


@unittest.skipUnless(sys.platform == "win32", "needs a Windows desktop")
class OrbGameModeTests(unittest.TestCase):
    def make(self):
        import tkinter

        from xenon import orb

        if orb.Worker is None:
            self.skipTest("orb worker unavailable")
        env = mock.patch.dict(os.environ, {"XENON_COMPANION": "off", "APPDATA": tempfile.mkdtemp()})
        env.start()
        self.addCleanup(env.stop)
        try:
            app = orb.OrbApp(lambda: None, None, tray=False)
        except tkinter.TclError as error:
            self.skipTest(f"Tk unavailable: {error}")
        if app._hotkeys is not None:
            app._hotkeys.stop()
            app._hotkeys = None

        def pump(seconds=0.5):
            end = time.time() + seconds
            while time.time() < end:
                app.root.update()
                time.sleep(0.02)

        self.addCleanup(lambda: (app._worker.stop(1), app.window.close(), app.root.destroy()))
        return app, pump

    def test_game_mode_stops_xenon_and_brings_it_back(self):
        from xenon import orb

        app, pump = self.make()
        widget = _FakeWidget()
        app.music_widget = widget
        started = []
        with mock.patch.object(orb, "start_music_widget_async", lambda a: started.append(a)):
            app.toggle_game_mode()
            pump(0.3)
            self.assertTrue(app.game_mode)
            self.assertTrue(app.hidden)
            self.assertTrue(widget.stopped.wait(2))
            app._hotkey("ask")                          # nothing pops up over a game
            pump(0.2)
            self.assertFalse(app.pill.visible)
            app.toggle_game_mode()
            pump(0.5)
            for _ in range(20):
                if started:
                    break
                time.sleep(0.05)
        self.assertFalse(app.game_mode)
        self.assertFalse(app.hidden)
        self.assertEqual(started, [app])                 # the widget comes back

    def test_a_hidden_orb_stays_hidden_after(self):
        app, pump = self.make()
        app.toggle_hidden()
        app.toggle_game_mode()
        app.toggle_game_mode()
        pump(0.2)
        self.assertTrue(app.hidden)


if __name__ == "__main__":
    unittest.main()
