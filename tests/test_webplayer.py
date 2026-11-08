import unittest

from xenon.music import MusicCommand, MusicController
from xenon.webplayer import WebPlayer


class FakeButton:
    def __init__(self, name, page):
        self.name, self.page = name, page

    def window_text(self):
        return self.name

    def invoke(self):
        self.page.pressed.append(self.name)
        lowered = self.name.lower()
        if lowered.startswith("play ") or lowered == "play":
            self.page.playing = True
        elif lowered == "pause":
            self.page.playing = False


class FakePage:
    def __init__(self, labels, playing=False, address="https://open.spotify.com/"):
        self.labels, self.playing, self.address = labels, playing, address
        self.pressed: list[str] = []

    def buttons(self):
        bar = ["Pause" if self.playing else "Play", "Next", "Previous"]
        return [FakeButton(name, self) for name in self.labels + bar]


class FakeDocument:
    def __init__(self, page):
        self.page = page

        class Value:
            CurrentValue = page.address

        self.iface_value = Value()

    def descendants(self, control_type=None):
        return self.page.buttons()


class FakeWindow:
    def __init__(self, title, page=None):
        self.title, self.page = title, page

    def window_text(self):
        return self.title

    def descendants(self, control_type=None):
        return [FakeDocument(self.page)] if self.page else []


class FakeDesktop:
    def __init__(self, windows):
        self._windows = windows

    def windows(self):
        return self._windows


def player(windows):
    return WebPlayer(desktop_factory=lambda: FakeDesktop(windows), sleep=lambda s: None)


class WebPlayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = FakePage(["Play Liked Songs", "Play Lockedin", "Play Sereneade", "Liked Songs Pinned"])
        self.tab = FakeWindow("Spotify – Liked Songs - Opera", self.page)

    def test_presses_the_exact_play_button_and_confirms_playback(self) -> None:
        web = player([FakeWindow("Notepad"), self.tab])
        self.assertEqual(web.play("Liked Songs"), "Liked Songs")
        self.assertEqual(self.page.pressed, ["Play Liked Songs"])

    def test_playlist_names_match_loosely(self) -> None:
        web = player([self.tab])
        self.assertEqual(web.play("locked in"), "Lockedin")

    def test_finds_the_tab_by_address_while_a_song_is_titled(self) -> None:
        self.tab.title = "Sweetest Pie • Megan Thee Stallion - Opera"
        self.page.address = "https://open.spotify.com/collection/tracks"
        self.assertIs(player([self.tab]).window(), self.tab)

    def test_opens_the_page_when_no_tab_is_open(self) -> None:
        windows: list = []
        web = player(windows)
        web.play("Liked Songs", open_page=lambda: windows.append(self.tab))
        self.assertEqual(self.page.pressed, ["Play Liked Songs"])

    def test_no_match_presses_nothing(self) -> None:
        web = player([self.tab])
        self.assertIsNone(web.play("something completely different"))
        self.assertEqual(self.page.pressed, [])

    def test_transport_controls_use_the_player_bar(self) -> None:
        self.page.playing = True
        web = player([self.tab])
        self.assertTrue(web.control("pause"))
        self.assertFalse(self.page.playing)
        self.assertTrue(web.control("next"))
        self.assertEqual(self.page.pressed, ["Pause", "Next"])

    def test_no_tab_means_none_so_media_keys_take_over(self) -> None:
        self.assertIsNone(player([FakeWindow("Notepad")]).control("pause"))


class ControllerWithWebPlayerTests(unittest.TestCase):
    def test_liked_songs_actually_play_without_a_login(self) -> None:
        page = FakePage(["Play Liked Songs"])
        windows: list = []
        web = player(windows)
        opened = []

        def open_uri(uri):
            opened.append(uri)
            windows.append(FakeWindow("Spotify – Liked Songs - Opera", page))
            return "web"

        ctl = MusicController(None, media_key=lambda c: True, open_uri=open_uri, web=web)
        self.assertEqual(ctl.run(MusicCommand("liked")), "Playing your Liked Songs.")
        self.assertEqual(opened, ["spotify:collection:tracks"])

    def test_pause_prefers_the_web_player_over_media_keys(self) -> None:
        page = FakePage([], playing=True)
        keys = []
        web = player([FakeWindow("Spotify - Opera", page)])
        ctl = MusicController(None, media_key=lambda c: keys.append(c) or True, open_uri=lambda u: "web", web=web)
        self.assertEqual(ctl.run(MusicCommand("pause")), "Paused.")
        self.assertEqual(keys, [])


if __name__ == "__main__":
    unittest.main()
