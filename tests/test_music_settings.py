import json
import os
import tempfile
import unittest
from dataclasses import replace

from PIL import Image

from xenon import music_scene as scene
from xenon import music_settings as ms


class CoerceTests(unittest.TestCase):
    def test_defaults_when_empty(self):
        self.assertEqual(ms.coerce({}), ms.MusicSettings())

    def test_clamps_and_rejects_bad_values(self):
        s = ms.coerce({
            "fps": 999, "orb_size": 0.01, "visualizer": "lasers", "show_card": "yes",
            "lyric_size": True, "custom_color": "not a colour", "bar_count": 77.6,
            "unknown_key": 1,
        })
        self.assertEqual(s.fps, 60)
        self.assertEqual(s.orb_size, 0.6)
        self.assertEqual(s.visualizer, "rays")
        self.assertTrue(s.show_card)
        self.assertEqual(s.lyric_size, 1.0)
        self.assertEqual(s.custom_color, ms.MusicSettings().custom_color)
        self.assertEqual(s.bar_count, 78)

    def test_accent(self):
        self.assertEqual(ms.MusicSettings(accent="album").accent_rgb((1, 2, 3)), (1, 2, 3))
        self.assertEqual(ms.MusicSettings(accent="white").accent_rgb((1, 2, 3)), (255, 255, 255))
        self.assertEqual(ms.MusicSettings(accent="custom", custom_color="#102030").accent_rgb((1, 2, 3)),
                         (16, 32, 48))
        self.assertEqual(ms.parse_color("#abc"), (170, 187, 204))


class FileTests(unittest.TestCase):
    def test_round_trip_and_watcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x", "music_widget.json")
            self.assertEqual(ms.load(path), ms.MusicSettings())
            watcher = ms.SettingsWatcher(path, interval=0)
            ms.save(replace(ms.MusicSettings(), visualizer="wave", side="left"), path)
            os.utime(path, (1, 1))
            self.assertTrue(watcher.poll(1.0))
            self.assertEqual(watcher.current.visualizer, "wave")
            self.assertFalse(watcher.poll(2.0))

    def test_corrupt_file_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            with open(path, "w") as handle:
                handle.write("{nope")
            self.assertEqual(ms.load(path), ms.MusicSettings())
            with open(path, "w") as handle:
                json.dump([1, 2], handle)
            self.assertEqual(ms.load(path), ms.MusicSettings())


class StyleRenderTests(unittest.TestCase):
    def setUp(self):
        self.cover = Image.new("RGB", (64, 64), (200, 40, 60))

    def test_every_orb_combination_renders(self):
        bars = [0.6] * 64
        for side in ms.SIDES:
            layout = scene.scene_layout(1280, 720, side, 1.0)
            for disc in ms.DISCS:
                art = scene.OrbArt(layout.orb_diameter, self.cover, (255, 255, 255), disc, True, 0.5)
                for viz in ms.VISUALIZERS:
                    st = ms.MusicSettings(disc=disc, visualizer=viz, side=side, bar_count=60)
                    img = scene.render_orb(layout.orb_box, layout, art, bars, 30.0, True, 0.5,
                                           (255, 255, 255), 720, st)
                    self.assertEqual(img.size, (layout.orb_box.w, layout.orb_box.h))
                    if disc != "none" or viz != "none":
                        self.assertGreater(img.getchannel("A").getextrema()[1], 0, (side, disc, viz))

    def test_left_layout_mirrors(self):
        right = scene.scene_layout(1920, 1080, "right")
        left = scene.scene_layout(1920, 1080, "left")
        self.assertGreater(right.orb_cx, right.lyric_box.x)
        self.assertLess(left.orb_cx, left.lyric_box.x)

    def test_every_lyric_style_renders(self):
        lines = [(0, "first line here"), (2000, "hello there world"), (5000, "next")]
        frame = scene.lyric_frame(lines, 3000, 8000)
        self.assertEqual(frame.previous, "first line here")
        box = scene.Box(0, 0, 700, 360)
        for style in ms.LYRIC_STYLES:
            for align in ms.LYRIC_ALIGNS:
                st = ms.MusicSettings(lyric_style=style, lyric_align=align, lyric_case="original")
                img = scene.render_lyrics(box, frame, (120, 200, 255), 720, st)
                self.assertGreater(img.getchannel("A").getextrema()[1], 200, (style, align))


if __name__ == "__main__":
    unittest.main()
