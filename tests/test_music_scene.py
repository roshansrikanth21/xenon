import unittest

from PIL import Image

from xenon import music_scene as ms


class LyricTimingTests(unittest.TestCase):
    def test_words_light_up_in_order_across_the_line(self) -> None:
        lines = [(1000, "and all I am is a man"), (5000, "next")]
        early = ms.lyric_frame(lines, 1050, 9000)
        late = ms.lyric_frame(lines, 4200, 9000)
        self.assertEqual(early.text, "and all I am is a man")
        self.assertGreater(early.reveal[0], 0)
        self.assertEqual(early.reveal[-1], 0)
        self.assertEqual(late.reveal, (ms.REVEAL_STEPS,) * 7)
        self.assertEqual(late.upcoming, "next")

    def test_word_starts_are_monotonic_and_inside_the_line(self) -> None:
        starts = ms.word_starts(0, 4000, ["a", "longer", "line", "here"])
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(0 <= t < 4000 for t in starts))

    def test_before_first_line_shows_only_whats_coming(self) -> None:
        frame = ms.lyric_frame([(3000, "hello there")], 1000, 9000)
        self.assertEqual(frame.text, "")
        self.assertEqual(frame.upcoming, "hello there")
        self.assertIsNone(ms.lyric_frame([(30000, "late")], 0, 60000))

    def test_equal_moments_compare_equal_so_redraws_skip(self) -> None:
        lines = [(0, "one two three"), (6000, "four")]
        self.assertEqual(ms.lyric_frame(lines, 5000, 9000), ms.lyric_frame(lines, 5100, 9000))


class LayoutAndRenderTests(unittest.TestCase):
    def test_layers_fit_on_screen_and_do_not_overlap_the_orb(self) -> None:
        layout = ms.scene_layout(3440, 1440)
        for box in (layout.orb_box, layout.lyric_box, layout.card_box):
            self.assertGreaterEqual(box.x, 0)
            self.assertLessEqual(box.x + box.w, 3440)
            self.assertLessEqual(box.y + box.h, 1440)
        self.assertLessEqual(layout.lyric_box.x + layout.lyric_box.w, layout.orb_cx - layout.orb_diameter // 2)

    def test_renders_are_transparent_outside_the_content(self) -> None:
        layout = ms.scene_layout(1720, 720)
        art = ms.OrbArt(layout.orb_diameter, Image.new("RGB", (64, 64), (200, 30, 30)))
        orb = ms.render_orb(layout.orb_box, layout, art, [0.5] * 64, 10.0, True, 0.4, screen_height=720)
        self.assertEqual(orb.size, (layout.orb_box.w, layout.orb_box.h))
        self.assertEqual(orb.getpixel((0, 0))[3], 0)
        lyric = ms.render_lyrics(layout.lyric_box, ms.lyric_frame([(0, "hello world")], 3000, 6000), screen_height=720)
        self.assertEqual(lyric.getpixel((lyric.width - 1, lyric.height - 1))[3], 0)
        self.assertGreater(max(lyric.getchannel("A").getdata()), 200)

    def test_ring_is_mirrored(self) -> None:
        ring = ms.ring_values([0.1, 0.5, 0.9])
        self.assertEqual(ring, ring[::-1])


if __name__ == "__main__":
    unittest.main()
