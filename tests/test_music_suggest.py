import io
import json
import time
import unittest

from PIL import Image

from xenon import music_dock as dock
from xenon.music_suggest import Suggester, _image_ok, primary_artist


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (250, 250), "red").save(buf, "PNG")
    return buf.getvalue()


def fake_fetch(calls):
    cover = "https://cdn-images.dzcdn.net/images/cover/x/250x250.jpg"

    def fetch(url):
        calls.append(url)
        if "/search/artist" in url:
            return json.dumps({"data": [{"id": 7, "name": "The Neighbourhood"}]}).encode()
        if "/artist/7/radio" in url:
            return json.dumps({"data": [
                {"title": "Sweater Weather", "artist": {"name": "The Neighbourhood"}, "album": {"cover_medium": cover}},
                {"title": "Mama's Boy", "artist": {"name": "Dominic Fike"}, "album": {"cover_medium": cover}},
                {"title": "Freak", "artist": {"name": "Lana Del Rey"},
                 "album": {"cover_medium": "https://evil.example/x.png"}},
                {"title": "Mama's Boy", "artist": {"name": "Dominic Fike"}, "album": {}},
            ]}).encode()
        if url == cover:
            return _png()
        raise AssertionError(url)
    return fetch


class SuggesterTests(unittest.TestCase):
    def test_lookup_skips_playing_dupes_and_foreign_images(self):
        calls = []
        out = Suggester(fetch=fake_fetch(calls)).lookup("Sweater Weather", "The Neighbourhood")
        self.assertEqual([(s.title, s.artist) for s in out], [("Mama's Boy", "Dominic Fike"), ("Freak", "Lana Del Rey")])
        self.assertEqual(out[0].cover.size, (128, 128))
        self.assertIsNone(out[1].cover)                     # not Deezer's CDN: never fetched
        self.assertFalse(any("evil" in c for c in calls))

    def test_unknown_artist_gives_nothing(self):
        s = Suggester(fetch=lambda url: json.dumps({"data": [{"id": 1, "name": "Someone Else"}]}).encode())
        self.assertEqual(s.lookup("x", "The Neighbourhood"), [])

    def test_request_runs_in_background_and_caches(self):
        calls = []
        s = Suggester(fetch=fake_fetch(calls))
        s.request("Sweater Weather", "The Neighbourhood, Someone")
        for _ in range(50):
            if s.current():
                break
            time.sleep(0.02)
        self.assertEqual(len(s.current()), 2)
        before = len(calls)
        s.request("Other song", "The Neighbourhood")
        self.assertEqual(len(calls), before)                # cached per artist

    def test_helpers(self):
        self.assertEqual(primary_artist("Don Toliver, Travis Scott"), "Don Toliver")
        self.assertEqual(primary_artist("A feat. B"), "A")
        self.assertTrue(_image_ok("https://e-cdns-images.dzcdn.net/a.jpg"))
        self.assertFalse(_image_ok("http://cdn-images.dzcdn.net/a.jpg"))
        self.assertFalse(_image_ok("https://dzcdn.net.evil.com/a.jpg"))


class DockSuggestedRowTests(unittest.TestCase):
    def test_suggested_rows_render(self):
        rows = [("Mama's Boy", "Dominic Fike", Image.new("RGB", (64, 64), "blue"), True),
                ("DEEP IN YOUR SOUL", "alan vuong", None, False)]
        geom = dock.dock_geometry(1920, 1080, len(rows))
        content = dock.DockContent("Sweater Weather", "The Neighbourhood", False, None, rows)
        img = dock.DockRenderer().render(geom, content, 1.0, "row0")
        self.assertEqual(img.size, (geom.box.w, geom.box.h))


if __name__ == "__main__":
    unittest.main()
