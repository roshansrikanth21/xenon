"""music_widget — the pure render logic: spectrum, palette, lyrics, layout."""
from __future__ import annotations

import math
import unittest

from PIL import Image

from xenon.music_widget import (
    cover_rect,
    current_line,
    palette_from_image,
    smooth_bars,
    spectrum_bars,
)


class TestSpectrumBars:
    def test_silence_is_flat(self):
        assert spectrum_bars([0.0] * 2048, 48000, 16) == [0.0] * 16

    def test_too_few_samples_is_flat(self):
        assert spectrum_bars([0.5] * 8, 48000, 8) == [0.0] * 8

    def test_tone_lands_in_its_band(self):
        # A 440 Hz sine, one full second: energy should sit in the low-mid
        # bands and be absent from the top bands.
        rate = 48000
        samples = [0.8 * math.sin(2 * math.pi * 440 * i / rate) for i in range(8192)]
        bars = spectrum_bars(samples, rate, 32)
        loud = [i for i, v in enumerate(bars) if v > 0.4]
        quiet = [i for i, v in enumerate(bars) if v < 0.05]
        assert loud, "440 Hz should register somewhere"
        assert max(loud) < 32 * 3 // 4, "440 Hz should not reach the top quarter"
        assert len(quiet) > 16, "most bands should stay quiet"

    def test_bars_are_bounded(self):
        rate = 48000
        samples = [1.0 * math.sin(2 * math.pi * 440 * i / rate) for i in range(4096)]
        bars = spectrum_bars(samples, rate, 64)
        assert all(0.0 <= v <= 1.0 for v in bars)


class TestPalette:
    def test_picks_dominant_saturated_color(self):
        image = Image.new("RGB", (64, 64), (20, 20, 30))
        for x in range(20, 44):
            for y in range(20, 44):
                image.putpixel((x, y), (220, 40, 40))  # red blob
        palette = palette_from_image(image, count=3)
        r, g, b = palette[0]
        assert r > g * 2 and r > b * 2

    def test_grayscale_image_gets_a_neutral(self):
        palette = palette_from_image(Image.new("RGB", (16, 16), (128, 128, 128)))
        assert palette and len(palette[0]) == 3


class TestCurrentLine:
    def test_before_first_line_is_empty(self):
        lines = [(1000, "one"), (5000, "two")]
        assert current_line(lines, 500) == ""

    def test_picks_the_line_being_sung(self):
        lines = [(1000, "one"), (5000, "two"), (9000, "three")]
        assert current_line(lines, 6000) == "two"

    def test_on_the_line_boundary(self):
        lines = [(1000, "one"), (5000, "two")]
        assert current_line(lines, 5000) == "two"

    def test_past_the_end_keeps_last_line(self):
        lines = [(1000, "one"), (5000, "two")]
        assert current_line(lines, 300000) == "two"


class TestSmoothBars:
    def test_attack_rises_quickly(self):
        state = smooth_bars([0.0] * 4, [1.0] * 4, attack=0.5, decay=0.1)
        assert all(0.4 < v < 0.6 for v in state)

    def test_decay_falls_slowly(self):
        state = smooth_bars([1.0] * 4, [0.0] * 4, attack=0.5, decay=0.1)
        assert all(0.85 < v < 0.95 for v in state)

    def test_shorter_target_list_is_tolerated(self):
        assert len(smooth_bars([1.0, 1.0, 1.0], [0.5])) == 1


class TestLayout:
    def test_cover_rect_scales_with_screen(self):
        cx, cy, diameter = cover_rect(1920, 1080)
        assert 0 < cx < 1920 and 0 < cy < 1080
        assert 0 < diameter < 1080
        cx2, cy2, diameter2 = cover_rect(2560, 1440)
        assert diameter2 > diameter


class ScriptFontTests(unittest.TestCase):
    def test_latin_uses_segoe_and_indic_uses_nirmala(self) -> None:
        from xenon.music_widget import script_font_file

        self.assertIsNone(script_font_file("Dil Haareya", bold=True))
        self.assertEqual(script_font_file("दिल हारेया", bold=True), ("Nirmala.ttc", 1))
        self.assertEqual(script_font_file("దిల్", bold=False), ("Nirmala.ttc", 0))
        self.assertEqual(script_font_file("사랑", bold=False), ("malgun.ttf", 0))
