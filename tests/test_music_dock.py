import json
import os
import tempfile
import unittest

from PIL import Image

from xenon import music_dock as dock
from xenon import music_scene as scene
from xenon import music_settings as ms
from xenon.music_history import MAX_ENTRIES, MusicHistory


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.geom = dock.dock_geometry(2560, 1440, 4)

    def test_panel_inside_window_and_screen(self):
        g, p = self.geom, self.geom.panel
        self.assertGreaterEqual(p.x, g.box.x)
        self.assertLessEqual(p.x + p.w, g.box.x + g.box.w)
        self.assertGreaterEqual(p.y, g.box.y)
        self.assertLessEqual(g.box.y + g.box.h, 1440)
        for name, b in g.targets.items():
            if name != "disc":
                self.assertTrue(p.x <= b.x and b.x + b.w <= p.x + p.w, name)

    def test_hit_testing(self):
        g = self.geom
        cx, cy = g.disc_center
        self.assertEqual(dock.hit(g, cx, cy, False), "disc")
        b = g.targets["next"]
        self.assertIsNone(dock.hit(g, b.x + 2, b.y + 2, False))      # closed: only the disc
        self.assertEqual(dock.hit(g, b.x + 2, b.y + 2, True), "next")
        r = g.targets["row2"]
        self.assertEqual(dock.hit(g, r.x + r.w // 2, r.y + r.h // 2, True), "row2")
        self.assertIsNone(dock.hit(g, 2000, 200, True))

    def test_in_dock_slack(self):
        g = self.geom
        p = g.panel
        self.assertFalse(dock.in_dock(g, p.x + 5, p.y + 5, False))
        self.assertTrue(dock.in_dock(g, p.x + 5, p.y + 5, True))
        self.assertFalse(dock.in_dock(g, 2400, 100, True))

    def test_render_every_stage(self):
        rows = [("505", "Arctic Monkeys", Image.new("RGB", (40, 40), "red")), ("E85", "Don Toliver", None)]
        geom = dock.dock_geometry(1920, 1080, len(rows))
        content = dock.DockContent("Sweater Weather", "The Neighbourhood", True,
                                   Image.new("RGB", (64, 64), "gray"), rows)
        renderer = dock.DockRenderer()
        for t in (0.0, 0.3, 1.0):
            img = renderer.render(geom, content, t, "row0" if t == 1.0 else None, (200, 220, 255))
            self.assertEqual(img.size, (geom.box.w, geom.box.h))
            self.assertGreater(img.getchannel("A").getextrema()[1], 0)
        closed = renderer.render(geom, content, 0.0, None)
        p = geom.local(geom.panel)
        self.assertEqual(closed.getpixel((p.x + p.w - 5, p.y + 5))[3], 0)   # panel hidden when closed


class HistoryTests(unittest.TestCase):
    def test_record_dedupes_caps_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = MusicHistory(tmp)
            h.record("A", "x", cover=Image.new("RGB", (300, 300), "blue"))
            h.record("B", "y")
            h.record("a ", "X")          # same song, different spacing/case
            self.assertEqual([e.title for e in h.entries()], ["a", "B"])
            self.assertTrue(h.entries()[0].cover)                     # kept the earlier cover
            self.assertEqual(h.thumbnail(h.entries()[0]).size, (96, 96))
            self.assertEqual([e.title for e in h.recent(("a", "x"))], ["B"])
            for i in range(MAX_ENTRIES + 5):
                h.record(f"song {i}", "z")
            self.assertEqual(len(MusicHistory(tmp).entries()), MAX_ENTRIES)

    def test_bad_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "music_history.json"), "w") as f:
                json.dump([{"title": ""}, "junk", {"title": "ok", "cover": "..\\..\\evil.png"}], f)
            entries = MusicHistory(tmp).entries()
            self.assertEqual([e.title for e in entries], ["ok"])
            self.assertEqual(entries[0].cover, "evil.png")             # no path escapes


class SplitOrbTests(unittest.TestCase):
    def test_ring_bytes_and_disc(self):
        lay = scene.scene_layout(1280, 720)
        data = scene.render_ring_bgra(lay.orb_box, lay, [0.6] * 64, True, 0.5, (255, 200, 100), 720,
                                      ms.MusicSettings())
        self.assertEqual(len(data), lay.orb_box.w * lay.orb_box.h * 4)
        self.assertGreater(max(data[3::4]), 200)
        silent = scene.render_ring_bgra(lay.orb_box, lay, [0.6] * 64, True, 0.5, (255, 255, 255), 720,
                                        ms.MusicSettings(), intro=0.0)
        self.assertLess(max(silent[3::4]), 140)    # only the stub ring at intro 0
        for disc in ms.DISCS:
            art = scene.OrbArt(lay.orb_diameter, Image.new("RGB", (50, 50), "red"), (255, 255, 255), disc)
            out = scene.render_disc(art, 10, True)
            if disc == "none":
                self.assertIsNone(out)
            else:
                self.assertEqual(out.size, (lay.orb_diameter, lay.orb_diameter))

    def test_poster_sets_small_words_small(self):
        self.assertTrue(scene._small_word("The"))
        self.assertFalse(scene._small_word("WORLD"))
        frame = scene.lyric_frame([(0, "I want the world in my hands")], 5000, 9000)
        img = scene.render_lyrics(scene.Box(0, 0, 900, 400), frame, (255, 255, 255), 720,
                                  ms.MusicSettings(lyric_style="poster"))
        self.assertGreater(img.getchannel("A").getextrema()[1], 200)


class MigrationTests(unittest.TestCase):
    def test_old_defaults_move_new_choices_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            with open(path, "w") as f:
                json.dump({"lyric_style": "reveal", "fps": 20, "disc": "vinyl"}, f)
            s = ms.load(path)
            self.assertEqual((s.lyric_style, s.fps, s.disc), ("poster", 30, "vinyl"))
            ms.save(ms.coerce({"lyric_style": "reveal", "fps": 20}), path)
            s = ms.load(path)                       # saved as version 2: kept
            self.assertEqual((s.lyric_style, s.fps), ("reveal", 20))


if __name__ == "__main__":
    unittest.main()


class PointerPollerTests(unittest.TestCase):
    def _run(self, samples):
        import sys
        import time
        if sys.platform != "win32":
            self.skipTest("Windows only")
        from xenon.music_widget import _Pointer

        p = _Pointer()
        feed = list(samples)
        p._sample = lambda: feed.pop(0) if feed else (feed_last[0])
        feed_last = [samples[-1]]
        p._desktop_at = lambda x, y: x < 1000
        p.start()
        time.sleep(0.25)
        p.stop()
        return p.clicks()

    def test_press_and_release_is_one_click(self):
        clicks = self._run([(0, 0, False, False), (10, 20, True, True), (10, 20, True, False),
                            (12, 21, False, False), (12, 21, False, False)])
        self.assertEqual(clicks, [(10, 20, True, 12, 21, True)])

    def test_tap_shorter_than_a_sample_still_counts(self):
        clicks = self._run([(0, 0, False, False), (5, 5, False, True), (5, 5, False, False)])
        self.assertEqual(clicks, [(5, 5, True, 5, 5, True)])

    def test_drag_onto_an_app_window_is_not_desktop(self):
        clicks = self._run([(0, 0, False, False), (10, 10, True, True), (1500, 10, False, False),
                            (1500, 10, False, False)])
        self.assertEqual(clicks, [(10, 10, True, 1500, 10, False)])
