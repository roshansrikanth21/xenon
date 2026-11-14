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


class PopOutTests(unittest.TestCase):
    def test_items_hug_the_record_on_an_arc(self):
        g = dock.dock_geometry(3440, 1440, 5, 1392)
        cx, cy = g.disc_center
        rows = [g.targets[f"row{i}"] for i in range(5)]
        xs = [b.x for b in rows]
        self.assertEqual(xs, sorted(xs))                      # curves out toward the record's middle
        self.assertGreater(xs[-1] - g.targets["previous"].x, 20)
        for name, b in g.targets.items():
            if name == "disc":
                continue
            overlaps = b.y < cy + g.disc / 2 and b.y + b.h > cy - g.disc / 2
            if overlaps:                                     # beside the record, clear of it
                self.assertGreater(b.x, cx + g.disc / 2 * 1.06, name)

    def test_pieces_start_inside_the_record(self):
        rows = [("A", "x", None), ("B", "y", None)]
        g = dock.dock_geometry(1920, 1080, len(rows))
        content = dock.DockContent("T", "a", True, None, rows)
        r = dock.DockRenderer()
        early = r.render(g, content, 0.06, None, draw_disc=False)
        bbox = early.getchannel("A").getbbox()
        self.assertIsNotNone(bbox)
        cx = g.disc_center[0] - g.box.x
        self.assertLess(bbox[0], cx + g.disc)                # still near the record
        full = r.render(g, content, 1.0, None, draw_disc=False).getchannel("A").getbbox()
        self.assertGreater(full[2], bbox[2])                 # flown out to its place


class SmoothLyricTests(unittest.TestCase):
    LINES = [(0, "I could have died for you"), (3000, "And I don't know why"), (6000, "Aeroplane")]

    def test_state_is_continuous(self):
        a = scene.lyric_state(self.LINES, 3050, 9000)
        b = scene.lyric_state(self.LINES, 3080, 9000)
        self.assertEqual(a.text, "And I don't know why")
        self.assertGreater(b.levels[0], a.levels[0])
        self.assertTrue(0 < a.levels[0] < 1 and 0 < a.entry < 1)

    def test_animator_fades_words_in_steps(self):
        box = scene.Box(0, 0, 900, 300)
        st = ms.coerce({"line_motion": "cut"})          # no old line drifting over the first word
        anim = scene.LyricAnimator()
        keys, images = [], []
        for pos in range(3000, 3400, 20):
            key, img = anim.frame(self.LINES, pos, 9000, box, (255, 200, 80), 1080, st)
            self.assertEqual(img.size, (box.w, box.h))
            keys.append(key)
            images.append(img)
        self.assertGreater(len(set(keys)), 8)              # many in-between pictures, not ~5
        first_word = images[0].crop((0, 0, 200, 300)).getchannel("A").getextrema()[1]
        later = images[-1].crop((0, 0, 200, 300)).getchannel("A").getextrema()[1]
        self.assertGreater(later, first_word)              # rose and lit up

    def test_clock_is_steady_and_snaps_on_seek(self):
        c = scene.LyricClock()
        self.assertEqual(c.now(1000, True, "a", t=0.0), 1000)
        mid = c.now(1300, True, "a", t=0.1)                # reading jumped ahead 200 ms
        self.assertTrue(1100 <= mid < 1300)                # folded in, not jumped
        later = c.now(1150, True, "a", t=0.15)             # reading fell back: never go backwards
        self.assertGreaterEqual(later, mid)
        self.assertEqual(c.now(60000, True, "a", t=0.2), 60000)   # a seek snaps
        self.assertEqual(c.now(500, True, "b", t=0.25), 500)      # new song snaps

    def test_wipe_crosses_the_word_and_lines_slide(self):
        box = scene.Box(0, 0, 900, 300)
        st = ms.coerce({"lyric_motion": "wipe", "line_motion": "slide"})
        anim = scene.LyricAnimator()
        lines = [(0, "Hello"), (2000, "Wonderful")]
        prog = scene.word_progress(2000, 6000, ["Wonderful"], 2100, "wipe")
        self.assertTrue(0 < prog[0] < 1)
        # mid-sweep: the left of the word is lit brighter than its right
        _, full = anim.frame(lines, 5000, 6000, box, (255, 255, 255), 1080, st)
        fb = full.getchannel("A").getbbox()
        mid = anim.frame(lines, 2500, 6000, box, (255, 255, 255), 1080, st)[1].convert("L")
        left = mid.crop((fb[0], fb[1], fb[0] + 60, fb[3])).getextrema()[1]
        right = mid.crop((fb[2] - 60, fb[1], fb[2], fb[3])).getextrema()[1]
        self.assertGreater(left, right)

    def test_rays_are_antialiased(self):
        lay = scene.scene_layout(1920, 1080)
        bars = [0.6] * 64
        data = scene.render_ring_bgra(lay.orb_box, lay, bars, True, 0.5, (255, 255, 255), 1080,
                                      ms.coerce({"glow": 0.0}))
        alpha = set(data[3::4])
        self.assertGreater(len(alpha), 40)                 # soft edges, not just on/off


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

    def test_video_look_keeps_behaviour_toggles(self):
        s = ms.video_look(ms.coerce({"disc": "vinyl", "bar_count": 36, "show_dock": False, "side": "left"}))
        self.assertEqual((s.disc, s.bar_count, s.accent, s.halo), ("glow", 120, "album", False))
        self.assertEqual((s.show_dock, s.side), (False, "left"))

    def test_v3_turns_the_old_halo_off_but_keeps_a_new_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            with open(path, "w") as f:
                json.dump({"version": 2, "halo": True, "fps": 20}, f)
            s = ms.load(path)
            self.assertEqual((s.halo, s.fps), (False, 20))
            ms.save(ms.coerce({"halo": True}), path)
            self.assertTrue(ms.load(path).halo)


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
