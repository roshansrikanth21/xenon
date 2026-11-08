import json
import time
import unittest
import urllib.request

from xenon.companion import Companion
from xenon.lyrics import Lyrics, LyricsFinder, clean_title, parse_lrc


class LrcTests(unittest.TestCase):
    def test_parse_lrc_timestamps(self) -> None:
        lines = parse_lrc("[00:01.50]first line\n[01:02.00]second\nnot a lyric\n[00:00.00]")
        self.assertEqual(lines, [(0, ""), (1500, "first line"), (62000, "second")])

    def test_clean_title(self) -> None:
        self.assertEqual(clean_title("Song (feat. Someone)"), "Song")
        self.assertEqual(clean_title("Song - Remastered 2011"), "Song")
        self.assertEqual(clean_title("Plain"), "Plain")


class FinderTests(unittest.TestCase):
    def test_exact_match_then_cache(self) -> None:
        calls = []

        def fetch(path, params):
            calls.append((path, params))
            return {"syncedLyrics": "[00:01.00]hello", "plainLyrics": "hello"}

        finder = LyricsFinder(fetch)
        lyrics = finder.find("Song (feat. X)", "Artist, Other", 200000)
        self.assertEqual(lyrics.lines, [(1000, "hello")])
        self.assertEqual(calls[0][1], {"track_name": "Song", "artist_name": "Artist", "duration": 200})
        finder.find("Song (feat. X)", "Artist, Other", 200000)
        self.assertEqual(len(calls), 1)  # cached

    def test_search_fallback_picks_closest_duration(self) -> None:
        def fetch(path, params):
            if path == "get":
                return None
            return [
                {"duration": 150, "syncedLyrics": "[00:01.00]short"},
                {"duration": 214, "syncedLyrics": "[00:01.00]right one"},
                {"duration": 214, "syncedLyrics": None},
            ]

        lyrics = LyricsFinder(fetch).find("Mashup x Other", "Ecra", 216000)
        self.assertEqual(lyrics.lines, [(1000, "right one")])

    def test_network_errors_are_not_cached(self) -> None:
        attempts = []

        def fetch(path, params):
            attempts.append(path)
            raise OSError("offline")

        finder = LyricsFinder(fetch)
        self.assertIsNone(finder.find("Song", "Artist"))
        self.assertIsNone(finder.find("Song", "Artist"))
        self.assertEqual(len(attempts), 2)


class CompanionTests(unittest.TestCase):
    def make(self, music=None, lyrics=None):
        state = {"state": "listening", "timers": [], "last": "hi"}
        companion = Companion(lambda: state, (lambda: music) if music else None,
                              (lambda t, a, d: lyrics) if lyrics else None, port=0, host="127.0.0.1")
        return companion

    def test_snapshot_without_music_shows_xenon_state(self) -> None:
        snap = self.make().snapshot()
        self.assertEqual(snap["xenon"]["state"], "listening")
        self.assertIsNone(snap["music"])

    def test_snapshot_extrapolates_position_and_sends_lyrics_once(self) -> None:
        companion = self.make()
        companion._music = {"title": "Song", "artist": "A", "position_ms": 10000,
                            "duration_ms": 200000, "playing": True}
        companion._music_at = time.monotonic() - 2
        companion._track_lyrics["Song|A"] = Lyrics([(0, "la"), (12000, "da")])
        first = companion.snapshot()
        self.assertGreaterEqual(first["music"]["position_ms"], 11900)
        self.assertEqual(first["music"]["lyrics_state"], "synced")
        self.assertIn("lyrics", first["music"])
        again = companion.snapshot(have_lyrics="Song|A")
        self.assertNotIn("lyrics", again["music"])  # the page already has them

    def test_http_server_serves_page_and_state(self) -> None:
        companion = self.make()
        companion.start()
        self.addCleanup(companion.stop)
        port = companion._server.server_address[1]
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
        self.assertIn(b"<title>Xenon</title>", page)
        state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=5))
        self.assertEqual(state["xenon"]["state"], "listening")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/control", timeout=5)


if __name__ == "__main__":
    unittest.main()
