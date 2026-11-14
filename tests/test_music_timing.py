import unittest

from xenon import lyric_layer
from xenon import music_scene as scene
from xenon.lyrics_lookup import parse_lrc, parse_word_times, to_result
from xenon.music_onsets import OnsetDetector, snap_starts

ENHANCED = """[offset:+0]
[00:12.00]<00:12.00>Hello <00:12.50>bright <00:13.20>world <00:14.00>
[00:15.00]plain line here
[00:16.00]<00:16.10>Sing<00:16.40>ing <00:17.00>loud
"""


class WordTimesTests(unittest.TestCase):
    def test_enhanced_lines_give_word_starts(self):
        words = parse_word_times(ENHANCED)
        self.assertEqual(words[12000], ([12000, 12500, 13200], 14000))
        self.assertNotIn(15000, words)                      # no word stamps
        # a stamp inside a word belongs to that word
        self.assertEqual(words[16000][0], [16100, 17000])
        self.assertIsNone(words[16000][1])

    def test_text_matches_parse_lrc(self):
        lines = dict(parse_lrc(ENHANCED))
        self.assertEqual(lines[12000], "Hello bright world")
        self.assertEqual(lines[16000], "Singing loud")

    def test_plain_lrc_has_none(self):
        self.assertEqual(parse_word_times("[00:01.00]a b c\n[00:02.00]d e"), {})

    def test_result_carries_words(self):
        result = to_result({"syncedLyrics": ENHANCED})
        self.assertIn(12000, result["words"])

    def test_known_timing_is_used(self):
        starts, ends = scene.word_spans(12000, 15000, ["Hello", "bright", "world"],
                                        known=([12000, 12500, 13200], 14000))
        self.assertEqual(starts, [12000, 12500, 13200])
        self.assertEqual(ends, [12500, 13200, 14000])

    def test_known_without_end_finishes_inside_the_line(self):
        _s, ends = scene.word_spans(16000, 17500, ["Singing", "loud"], known=([16100, 17000], None))
        self.assertLessEqual(ends[-1], 17500)
        self.assertGreater(ends[-1], 17000)


class OnsetTests(unittest.TestCase):
    def test_detector_fires_on_a_burst(self):
        det = OnsetDetector()
        quiet = [0.2] * 64
        fired = [det.feed(quiet, i / 30) for i in range(20)]
        self.assertFalse(any(fired))
        loud = [0.2] * 23 + [0.8] * 27 + [0.2] * 14
        self.assertTrue(det.feed(loud, 21 / 30))
        # refractory: the very next frame never fires again
        self.assertFalse(det.feed([0.9] * 64, 21.5 / 30))

    def test_bass_alone_is_not_a_voice_burst(self):
        det = OnsetDetector()
        for i in range(20):
            det.feed([0.2] * 64, i / 30)
        self.assertFalse(det.feed([0.9] * 20 + [0.2] * 44, 21 / 30))

    def test_snap_moves_close_guesses_and_carries_the_shift(self):
        out = snap_starts([1000, 1400, 1800], [1100])
        self.assertEqual(out[0], 1100)
        self.assertEqual(out[1], 1500)          # carried
        self.assertEqual(out[2], 1900)

    def test_far_bursts_are_ignored(self):
        self.assertEqual(snap_starts([1000, 1400], [1600 + 400]), [1000, 1400])

    def test_order_is_kept(self):
        out = snap_starts([1000, 1050, 1100], [1000, 1010, 1020])
        self.assertEqual(out, sorted(out))
        self.assertTrue(all(b - a >= 60 for a, b in zip(out, out[1:])))

    def test_song_times_from_monotonic(self):
        self.assertEqual(lyric_layer.song_times([9.5, 0.5], 20000, 10.0), [19500])

    def test_onsets_shift_guessed_spans(self):
        words = "one two three four".split()
        plain, _ = scene.word_spans(1000, 4000, words)
        moved, _ = scene.word_spans(1000, 4000, words, onsets=[plain[1] + 100])
        self.assertEqual(moved[1], plain[1] + 100)
        self.assertEqual(moved[2], plain[2] + 100)

    def test_animator_never_slides_a_word_back(self):
        anim = scene.LyricAnimator()
        from xenon.music_settings import MusicSettings
        st = MusicSettings(line_motion="cut")
        lines = [(1000, "one two three four"), (5000, "next")]
        box = scene.Box(0, 0, 900, 300)
        starts, _ = scene.word_spans(1000, 5000, lines[0][1].split(), anim._pace(lines))
        at = starts[1] + 60
        anim.frame_region(lines, at, 0, box, settings=st)
        before = anim._held[1]
        anim.frame_region(lines, at + 10, 0, box, settings=st, onsets=[starts[1] + 120])
        self.assertGreaterEqual(anim._held[1], before)


class GlowBandTests(unittest.TestCase):
    def test_each_band_follows_its_own_slice(self):
        from xenon.music_onsets import BANDS, BeatBand
        bands = {n: BeatBand(*s) for n, s in BANDS.items()}
        quiet = [0.1] * 64
        for _ in range(30):
            for b in bands.values():
                b.update(quiet, 1 / 30, True)
        hat = [0.1] * 44 + [0.9] * 20            # only the highs jump
        out = {n: b.update(hat, 1 / 30, True) for n, b in bands.items()}
        self.assertGreater(out["highs"], 0.8)
        self.assertLess(out["bass"], 0.5)

    def test_silent_when_paused(self):
        from xenon.music_onsets import BeatBand
        self.assertEqual(BeatBand(0, 6).update([0.9] * 64, 1 / 30, False), 0.0)

    def test_glow_colours_from_the_cover(self):
        tones = scene.glow_colors([(200, 40, 40), (40, 40, 200), (128, 128, 128)], (255, 80, 80))
        self.assertEqual(tones[0], (255, 80, 80))
        self.assertGreater(tones[1][2], tones[1][0])          # the blue one
        self.assertEqual(tones[2], (255, 80, 80))             # grey gives way
        self.assertEqual(len(scene.glow_colors([], (1, 2, 3))), 3)

    def test_edge_tone_drifts_between_two_colours(self):
        img = scene.edge_glow(400, 30, "top", (255, 0, 0), (0, 0, 255))
        reds = [img.getpixel((x, 0))[0] for x in range(0, 400, 10)]
        self.assertGreater(max(reds) - min(reds), 100)
        self.assertEqual(img.getchannel("A").tobytes(),
                         scene.edge_glow(400, 30, "top", (255, 0, 0)).getchannel("A").tobytes())


class RingPipelineTests(unittest.TestCase):
    def test_stages_pass_the_newest_item_along(self):
        import threading
        import time

        from xenon import ring_layer
        got, done = [], threading.Event()

        def last(x):
            got.append(x)
            if x == 9:
                done.set()

        tail = ring_layer._Stage(last)
        head = ring_layer._Stage(lambda x: x, tail)
        for i in range(10):
            head.give(i)
            time.sleep(0.002)
        self.assertTrue(done.wait(2.0))
        self.assertEqual(got, sorted(got))            # never out of order
        head.stop()

    def test_ring_split_matches_one_pass(self):
        from xenon.music_settings import MusicSettings
        st = MusicSettings()
        layout = scene.scene_layout(1720, 720)
        box = layout.orb_box
        bars = [0.1 + 0.6 * (i % 7) / 7 for i in range(64)]
        whole = scene.render_ring_bgra(box, layout, bars, True, 0.4, (200, 120, 255), 720, st)
        alpha = scene.ring_sheen(scene.ring_shapes(box, layout, bars, True, 0.4, 720, st),
                                 layout.orb_diameter, st)
        self.assertEqual(whole, scene.pack_ring(box, alpha, (200, 120, 255)))


if __name__ == "__main__":
    unittest.main()
