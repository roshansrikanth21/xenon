import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xenon import brightness, fastpath, music, orb_commands as oc, patterns

T0 = 1_700_000_000.0          # a fixed "now" so hours/days are stable


def store():
    return patterns.PatternStore(Path(tempfile.mkdtemp()) / "p.sqlite3")


def listen(s, title, artist, seconds, start, playing=True):
    for i in range(int(seconds) + 1):
        s.track_update(title, artist, playing=playing, now=start + i)
    return start + seconds + 1


class PlayCountingTests(unittest.TestCase):
    def test_play_skip_and_blip(self):
        s = store()
        t = listen(s, "Long", "A", 40, T0)        # >= 30 s: a play
        t = listen(s, "Short", "B", 10, t)        # 3..30 s: a skip
        t = listen(s, "Blip", "C", 1, t)          # < 3 s: nothing
        s.track_update("Next", "D", now=t)
        s.flush()
        rows = {r["title"]: r for r in s.recent_plays(10)}
        self.assertIn("Long", rows)
        self.assertNotIn("Blip", rows)
        self.assertEqual([a for a, _ in s.top_artists(now=t)], ["A"])   # skips do not count
        s.close()

    def test_paused_time_and_videos_do_not_count(self):
        s = store()
        t = listen(s, "Paused", "A", 60, T0, playing=False)
        for i in range(60):
            s.track_update("Clip", "B", video=True, now=t + i)
        s.track_update("Next", "C", now=t + 61)
        s.flush()
        self.assertEqual(s.top_artists(now=t + 61), [])
        s.close()

    def test_disabled_store_learns_nothing(self):
        s = patterns.PatternStore(Path(tempfile.mkdtemp()) / "p.sqlite3", enabled=lambda: False)
        t = listen(s, "Song", "A", 40, T0)
        s.track_update("Next", "B", now=t)
        s.log_request("game mode on", "command")
        s.flush()
        self.assertEqual(s.stats()["plays"], 0)
        self.assertEqual(s.stats()["requests"], 0)
        s.close()


class HabitPhraseTests(unittest.TestCase):
    def test_usual_again_and_more_like_this(self):
        s = store()
        t = listen(s, "Blinding Lights", "The Weeknd", 40, T0)
        s.track_update("x", "y", now=t)
        s.flush()
        again = s.rewrite_music_request("xenon play that again please", now=t)
        usual = s.rewrite_music_request("play my usual", now=t)
        self.assertEqual(again, "play Blinding Lights by The Weeknd")
        self.assertIn("The Weeknd", usual)
        for out in (again, usual, s.rewrite_music_request("play more like this", now=t)):
            with self.subTest(out=out):
                self.assertIsNotNone(music.parse(out))          # the music parser can run it
        self.assertIsNone(s.rewrite_music_request("play blinding lights", now=t))
        s.close()

    def test_nothing_learned_means_no_rewrite(self):
        s = store()
        self.assertIsNone(s.rewrite_music_request("play my usual", now=T0))
        self.assertIn("", s.summary_for_prompt(now=T0))
        self.assertTrue(s.describe_listening(now=T0))            # a friendly fallback
        s.close()

    def test_summary_is_short_and_clean(self):
        s = store()
        t = listen(s, "Song​", "Art​ist", 40, T0)
        s.track_update("x", "y", now=t)
        s.flush()
        text = s.summary_for_prompt(now=t, max_chars=120)
        self.assertLessEqual(len(text), 120)
        self.assertNotIn("​", text)
        s.close()

    def test_forget_clears_everything(self):
        s = store()
        t = listen(s, "Song", "A", 40, T0)
        s.track_update("x", "y", now=t)
        s.flush()
        s.forget()
        s.flush()
        self.assertEqual(s.stats()["plays"], 0)
        s.close()


class ControlParserTests(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "game mode on": "game_on", "hey xenon turn off game mode": "game_off",
            "go xtreme": "xtreme_on", "start xtreme": "xtreme_on", "exit xtreme": "xtreme_off",
            "turn off the music widget": "widget_off", "open music settings": "widget_settings",
            "stop learning my habits": "learn_off", "forget my history": "forget_history",
            "what do i listen to most": "listening_summary",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(oc.parse(text).kind, kind)

    def test_settings(self):
        self.assertEqual(oc.parse("show lyrics in english letters").value, "latin")
        self.assertEqual(oc.parse("make the glow brighter").value, ("delta", 0.25))

    def test_near_misses_stay_with_their_own_parsers(self):
        for text in ("play some games", "dim my monitor", "stop", "next song", "open chrome",
                     "don't turn on game mode", "what time is it"):
            with self.subTest(text=text):
                self.assertIsNone(oc.parse(text))
        # Why the control parser runs first: these would be misread otherwise.
        self.assertIsNotNone(brightness.parse("make the glow brighter"))
        self.assertIsNotNone(fastpath.parse("start xtreme"))

    def test_background_speech_cannot_switch_modes(self):
        game = oc.parse("game mode on")
        self.assertFalse(oc.allowed(game, "voice-ambient"))
        self.assertFalse(oc.allowed(game, "voice-focus"))
        self.assertTrue(oc.allowed(game, "voice"))
        calls = []
        oc.set_handler(lambda cmd, origin: calls.append((cmd.kind, origin)) or "ok")
        try:
            self.assertNotEqual(oc.run(game, "voice-ambient"), "ok")
            self.assertEqual(calls, [])
            self.assertEqual(oc.run(game, "voice"), "ok")
            self.assertEqual(calls, [("game_on", "voice")])
        finally:
            oc.set_handler(None)

    def test_glow_setting_is_clamped_and_saved(self):
        from xenon import music_settings as ms

        saved = []
        current = ms.coerce({"corner_glow": 1.9})
        reply = oc.apply_setting(oc.parse("make the glow stronger"), load=lambda: current,
                                 save=lambda s: saved.append(s))
        self.assertEqual(saved[0].corner_glow, 2.0)
        self.assertTrue(reply)


class VoiceWiringTests(unittest.TestCase):
    """The voice and typed path send Xenon's controls to the orb, with the
    real origin, before brightness/fastpath can claim the words."""

    def test_controls_reach_the_handler_with_the_real_origin(self):
        from xenon import __main__ as main

        seen = []
        oc.set_handler(lambda cmd, origin: seen.append((cmd.kind, origin)) or "Game mode on.")
        try:
            with mock.patch.dict(os.environ, {"XENON_PATTERNS": "off"}):
                self.assertEqual(main._voice_fast_reply("xenon game mode on", origin="voice",
                                                        control_origin="voice"), "Game mode on.")
                refused = main._voice_fast_reply("game mode on", origin="voice",
                                                 control_origin="voice-ambient")
        finally:
            oc.set_handler(None)
        self.assertEqual(seen, [("game_on", "voice")])
        self.assertNotEqual(refused, "Game mode on.")

    def test_router_accepts_the_new_commands(self):
        from xenon import __main__ as main

        with mock.patch.dict(os.environ, {"XENON_PATTERNS": "off"}):
            for text in ("game mode on", "xtreme mode on", "lyrics in english letters", "glow stronger"):
                with self.subTest(text=text):
                    self.assertTrue(main._recognised(text))


if __name__ == "__main__":
    unittest.main()


class PcControlTests(unittest.TestCase):
    """Lock / shut down / restart / cancel, with fake executors: the test
    run never touches the real PC."""

    def test_parse(self):
        cases = {"lock my pc": "lock_pc", "shut down my laptop": "shutdown_pc", "restart my computer": "restart_pc",
                 "cancel shutdown": "cancel_shutdown", "don't shut down": "cancel_shutdown"}
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(oc.parse(text).kind, kind)
        for text in ("shut down", "lock", "turn off the music widget", "stop"):
            with self.subTest(text=text):
                cmd = oc.parse(text)
                self.assertTrue(cmd is None or cmd.kind not in oc.PC_KINDS)

    def test_shutdown_waits_and_never_forces(self):
        calls = []
        ok = type("R", (), {"returncode": 0})()
        reply = oc.pc_action("shutdown_pc", runner=lambda args: calls.append(args) or ok)
        self.assertEqual(calls[0][:4], ["shutdown", "/s", "/t", str(oc.SHUTDOWN_DELAY_S)])
        self.assertNotIn("/f", calls[0])
        self.assertIn("cancel", reply.lower())
        oc.pc_action("cancel_shutdown", runner=lambda args: calls.append(args) or ok)
        self.assertEqual(calls[1], ["shutdown", "/a"])

    def test_lock(self):
        locked = []
        self.assertEqual(oc.pc_action("lock_pc", lock=lambda: locked.append(1)), "Locked.")
        self.assertEqual(locked, [1])

    def test_background_speech_cannot_shut_down(self):
        cmd = oc.parse("shut down my pc")
        self.assertFalse(oc.allowed(cmd, "voice-ambient"))
        self.assertTrue(oc.allowed(cmd, "remote"))
        self.assertTrue(oc.allowed(cmd, "voice"))


class MediaKeyTests(unittest.TestCase):
    """Gestures press media keys: works with any player, never 'already playing' errors."""

    def test_parse_and_press(self):
        from xenon import music
        pressed = []
        for text, code in (("media play pause", music.VK_MEDIA_PLAY_PAUSE), ("media next", music.VK_MEDIA_NEXT_TRACK),
                           ("media previous", music.VK_MEDIA_PREV_TRACK)):
            with self.subTest(text=text):
                cmd = oc.parse(text)
                reply = oc.media_key(cmd.kind, press=lambda c: pressed.append(c) or True)
                self.assertTrue(reply)
                self.assertEqual(pressed[-1], code)
