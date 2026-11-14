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


class CoverFinderTests(unittest.TestCase):
    def test_itunes_match_upscales_url_and_checks_host(self):
        from xenon.music_cover import CoverFinder

        calls = []

        def fetch(url):
            calls.append(url)
            if url.startswith("https://itunes.apple.com/search"):
                return json.dumps({"results": [
                    {"trackName": "Other", "artistName": "Nobody", "artworkUrl100": "https://x.mzstatic.com/a/100x100bb.jpg"},
                    {"trackName": "A Little Death", "artistName": "The Neighbourhood",
                     "artworkUrl100": "https://is1-ssl.mzstatic.com/a/100x100bb.jpg"},
                ]}).encode()
            if url == "https://is1-ssl.mzstatic.com/a/1000x1000bb.jpg":
                buf = io.BytesIO()
                Image.new("RGB", (1000, 1000), "red").save(buf, "PNG")
                return buf.getvalue()
            raise AssertionError(url)

        f = CoverFinder(fetch=fetch, spotify_now=lambda: None)
        image = f.find("A Little Death (Remastered)", "The Neighbourhood")
        self.assertEqual(image.size, (640, 640))

    def test_no_match_and_foreign_host_give_none(self):
        from xenon.music_cover import CoverFinder

        f = CoverFinder(fetch=lambda url: json.dumps({"results": [
            {"trackName": "Boot Camp", "artistName": "TJR", "artworkUrl100": "https://evil.example/100x100bb.jpg"}]}).encode(),
            spotify_now=lambda: None)
        self.assertIsNone(f.find("Boot Camp", "TJR"))           # host not allowed
        self.assertIsNone(f.find("Something else", "Nobody"))   # no match

    def test_spotify_item_preferred_when_it_matches(self):
        from xenon.music_cover import CoverFinder

        now = {"item": {"name": "Sweater Weather", "artists": [{"name": "The Neighbourhood"}],
                        "album": {"images": [{"url": "https://i.scdn.co/image/big", "width": 640},
                                             {"url": "https://i.scdn.co/image/small", "width": 64}]}}}
        seen = []

        def fetch(url):
            seen.append(url)
            buf = io.BytesIO()
            Image.new("RGB", (640, 640), "blue").save(buf, "PNG")
            return buf.getvalue()

        f = CoverFinder(fetch=fetch, spotify_now=lambda: now)
        self.assertEqual(f.find("Sweater Weather", "The Neighbourhood").size, (640, 640))
        self.assertEqual(seen, ["https://i.scdn.co/image/big"])


class VinylDockAndBounceTests(unittest.TestCase):
    def test_spinning_record_renders_and_moves(self):
        geom = dock.dock_geometry(1920, 1080, 0)
        content = dock.DockContent("t", "a", True, Image.new("RGB", (64, 64), "red"), [])
        r = dock.DockRenderer()
        a = r.render(geom, content, 0.0, None, spin=0.0)
        b = r.render(geom, content, 0.0, None, spin=45.0)
        self.assertNotEqual(a.tobytes(), b.tobytes())

    def test_bounced_keeps_canvas_size(self):
        import sys
        if sys.platform != "win32":
            self.skipTest("Windows only module")
        from xenon.music_widget import _bounced

        disc = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
        for s in (1.0, 1.07, 1.3):
            self.assertEqual(_bounced(disc, s, 111).size, (111, 111))

    def test_v4_migration_stops_the_old_spin(self):
        import os
        import tempfile
        from xenon import music_settings as ms

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            with open(path, "w") as f:
                json.dump({"version": 3, "spin_speed": 9.0}, f)
            self.assertEqual(ms.load(path).spin_speed, 0.0)
            ms.save(ms.coerce({"spin_speed": 9.0}), path)
            self.assertEqual(ms.load(path).spin_speed, 9.0)
