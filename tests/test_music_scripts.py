import sys
import unittest

from PIL import Image, ImageDraw, ImageFont

from xenon import lyric_layer
from xenon import music_scene as scene
from xenon import text_shaping as ts
from xenon.media_titles import is_video, tidy, tidy_title
from xenon.music_onsets import SoundGate
from xenon.smtc import Track
from xenon.transliterate import has_indic, latin_lines, to_latin

MALAYALAM = "കേൾക്കുന്നു"   # കേൾക്കുന്നു


class ScriptTests(unittest.TestCase):
    def test_complex_face_by_script(self):
        self.assertEqual(ts.complex_face(MALAYALAM), "Nirmala UI")
        self.assertEqual(ts.complex_face("สวัสดี"), "Leelawadee UI")
        self.assertIsNone(ts.complex_face("hello"))
        self.assertIsNone(ts.complex_face("こんにちは"))   # kana: Pillow is fine

    @unittest.skipUnless(sys.platform == "win32", "Windows shaping")
    def test_windows_shapes_malayalam(self):
        plain = ImageFont.truetype("Nirmala.ttc", 48, index=1)
        shaped = ts.truetype("Nirmala.ttc", 48, index=1)
        d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        # Conjuncts merge, so the shaped word is narrower than glyph-by-glyph.
        self.assertLess(d.textlength(MALAYALAM, font=shaped), d.textlength(MALAYALAM, font=plain) - 10)
        img = Image.new("L", (600, 120), 0)
        ImageDraw.Draw(img).text((10, 10), MALAYALAM, font=shaped, fill=255)
        box = img.getbbox()
        self.assertIsNotNone(box)
        self.assertGreater(box[3] - box[1], 20)
        # Latin text still goes through Pillow untouched.
        self.assertEqual(d.textlength("abc", font=shaped),
                         d.textlength("abc", font=ImageFont.truetype("Nirmala.ttc", 48, index=1)))

    @unittest.skipUnless(sys.platform == "win32", "Windows shaping")
    def test_lyrics_render_shaped(self):
        box = scene.Box(0, 0, 900, 300)
        frame = scene.LyricFrame(MALAYALAM + " love", (5, 0), 5, "")
        img = scene.render_lyrics(box, frame, screen_height=720)
        self.assertIsNotNone(img.getbbox())
        self.assertIsInstance(scene.font(MALAYALAM, 40), ts.ShapedFont)
        self.assertNotIsInstance(scene.font("plain words", 40), ts.ShapedFont)

    def test_latin_spelling(self):
        self.assertEqual(to_latin("മലയാളം"), "malayaalam")
        self.assertEqual(to_latin("പാട്ട്"), "paattu")      # final virama
        self.assertEqual(to_latin("എന്റെ"), "ente")             # nta
        self.assertEqual(to_latin(MALAYALAM), "kelkkunnu")
        self.assertEqual(to_latin("कमल"), "kamal")                         # Hindi schwa
        self.assertEqual(to_latin("தமிழ்"), "thamizh")
        self.assertEqual(to_latin("mix ഞാൻ ok"), "mix njaan ok")
        self.assertFalse(has_indic("plain"))

    def test_latin_lines_keep_timing_and_words(self):
        lines = [(1000, MALAYALAM + " മല"), (2000, "english line")]
        out = latin_lines(lines)
        self.assertEqual([t for t, _ in out], [1000, 2000])
        self.assertEqual(len(out[0][1].split()), 2)
        self.assertEqual(out[1][1], "english line")

    def test_layer_shows_latin_only_when_asked(self):
        from xenon.music_settings import MusicSettings
        lines = [(0, MALAYALAM)]
        self.assertEqual(lyric_layer._display(lines, MusicSettings()), lines)
        self.assertEqual(lyric_layer._display(lines, MusicSettings(lyric_script="latin")), [(0, "kelkkunnu")])


class TitleTests(unittest.TestCase):
    def test_streaming_tab_title(self):
        self.assertEqual(tidy_title("Watch I'm Game - JioHotstar"), ("I'm Game", "jiohotstar"))
        self.assertTrue(is_video("Watch I'm Game - JioHotstar", "", "opera"))

    def test_music_sites_stay_songs(self):
        self.assertEqual(tidy_title("Blinding Lights - YouTube")[0], "Blinding Lights")
        self.assertFalse(is_video("Blinding Lights - YouTube", "", "chrome"))

    def test_songs_are_left_alone(self):
        self.assertEqual(tidy_title("Watch Me")[0], "Watch Me")
        self.assertEqual(tidy_title("Lights Down Low - MAX")[0], "Lights Down Low - MAX")
        self.assertFalse(is_video("Lights Down Low - MAX", "", "chrome"))
        self.assertFalse(is_video("Kesariya", "Arijit Singh", "Spotify"))

    def test_episodes_in_a_browser_are_videos(self):
        self.assertTrue(is_video("The Boys S4 E2", "", "msedge"))
        self.assertFalse(is_video("The Boys S4 E2", "", "Spotify"))

    def test_tidy_marks_and_keeps_identity(self):
        song = Track(title="Kesariya", artist="Arijit Singh", app="Spotify")
        self.assertIs(tidy(song), song)
        self.assertFalse(song.video)
        show = tidy(Track(title="Stranger Things | Netflix", app="msedge"))
        self.assertEqual(show.title, "Stranger Things")
        self.assertTrue(show.video)
        self.assertIsNone(tidy(None))


class SoundGateTests(unittest.TestCase):
    def test_turns_on_after_steady_sound_and_off_after_quiet(self):
        gate = SoundGate()
        loud, quiet = [0.1, -0.1] * 512, [0.0] * 1024
        self.assertFalse(gate.update(loud, 0.0))
        self.assertFalse(gate.update(loud, 0.3))
        self.assertTrue(gate.update(loud, 0.6))
        self.assertTrue(gate.update(quiet, 1.0))          # a short gap keeps it on
        self.assertTrue(gate.update(quiet, 2.0))
        self.assertFalse(gate.update(quiet, 2.6))

    def test_a_blip_does_not_wake_it(self):
        gate = SoundGate()
        gate.update([0.1] * 1024, 0.0)
        self.assertFalse(gate.update([0.0] * 1024, 0.2))
        self.assertFalse(gate.update([0.1] * 1024, 0.3))
        self.assertFalse(gate.update([], 0.4))


if __name__ == "__main__":
    unittest.main()
