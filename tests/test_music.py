import base64
import hashlib
import json
import random
import time
import unittest
import urllib.parse

from xenon import music
from xenon.music import (
    VK_MEDIA_NEXT_TRACK,
    VK_MEDIA_PLAY_PAUSE,
    MusicCommand,
    MusicController,
    SpotifyClient,
    authorize_url,
    parse,
    pkce_pair,
)


class ParseTests(unittest.TestCase):
    def check(self, text, kind, query="", hint=""):
        self.assertEqual(parse(text), MusicCommand(kind, query, hint), text)

    def test_liked_songs_in_many_phrasings(self) -> None:
        for text in (
            "play my liked songs",
            "Play my liked music.",
            "shuffle my favorites",
            "play my saved tracks on spotify",
            "play my likled songs",  # typed typo
        ):
            self.check(text, "liked")

    def test_transport_controls(self) -> None:
        self.check("pause", "pause")
        self.check("stop the music", "pause")
        self.check("resume", "resume")
        self.check("play", "resume")
        self.check("next song", "next")
        self.check("skip this song", "next")
        self.check("play the next song", "next")
        self.check("previous song", "previous")
        self.check("go back a song", "previous")

    def test_what_is_playing(self) -> None:
        for text in ("what's playing", "whats playing", "what song is this"):
            self.check(text, "now")

    def test_play_something_specific(self) -> None:
        self.check("play blinding lights", "play", "blinding lights")
        self.check("play blinding lights on spotify", "play", "blinding lights")
        self.check("play my gym playlist", "play", "gym", "playlist")
        self.check("play the playlist chill vibes", "play", "chill vibes", "playlist")
        self.check("play some jazz", "play", "jazz", "playlist")
        self.check("play the album after hours", "play", "after hours", "album")

    def test_not_music(self) -> None:
        for text in ("open chrome", "go back", "play a video", "play minecraft game", ""):
            self.assertIsNone(parse(text), text)


class FakeSpotify:
    """Records calls and answers like the Web API."""

    def __init__(self, **overrides) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.devices = [{"id": "pc", "type": "Computer", "name": "PC", "is_active": False}]
        self.active = True
        self.premium = True
        self.liked_total = 120
        self.playlists = [{"name": "Gym Bangers", "uri": "spotify:playlist:gym"}]
        self.search_results = {
            "artists": {"items": [{"name": "Drake", "uri": "spotify:artist:drake"}]},
            "tracks": {"items": [{"name": "Hotline Bling", "uri": "spotify:track:hb",
                                  "artists": [{"name": "Drake"}]}]},
            "playlists": {"items": [None, {"name": "Jazz Classics", "uri": "spotify:playlist:jazz"}]},
        }
        self.__dict__.update(overrides)

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.replace("/v1", "", 1)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload, query))
        if path == "/me/player/play":
            if not self.premium:
                return 403, {"error": {"status": 403, "message": "Premium required", "reason": "PREMIUM_REQUIRED"}}
            if not self.active and "device_id" not in query:
                return 404, {"error": {"status": 404, "message": "No active device found", "reason": "NO_ACTIVE_DEVICE"}}
            return 204, None
        if path == "/me/player/devices":
            return 200, {"devices": self.devices}
        if path == "/me/tracks":
            limit = int(query.get("limit", 20))
            offset = int(query.get("offset", 0))
            items = [{"track": {"uri": f"spotify:track:{i}"}} for i in range(offset, min(offset + limit, self.liked_total))]
            return 200, {"total": self.liked_total, "items": items}
        if path == "/me/playlists":
            return 200, {"items": self.playlists}
        if path == "/search":
            return 200, self.search_results
        if path == "/me/player/currently-playing":
            return 200, {"item": {"name": "Hotline Bling", "artists": [{"name": "Drake"}]}}
        return 204, None


def controller(fake: FakeSpotify, **kwargs) -> MusicController:
    tokens = {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600}
    client = SpotifyClient(tokens, transport=fake, persist=None)
    opened: list[str] = []
    ctl = MusicController(client, media_key=lambda code: True, open_uri=opened.append,
                          sleep=lambda s: None, **kwargs)
    ctl.opened = opened  # type: ignore[attr-defined]
    return ctl


class ControllerTests(unittest.TestCase):
    def last_play(self, fake):
        return [call for call in fake.calls if call[1] == "/me/player/play"][-1]

    def test_liked_songs_start_a_shuffled_batch(self) -> None:
        fake = FakeSpotify()
        reply = controller(fake).run(MusicCommand("liked"))
        self.assertIn("liked songs", reply)
        _, _, body, _ = self.last_play(fake)
        self.assertEqual(len(body["uris"]), 50)
        self.assertTrue(all(uri.startswith("spotify:track:") for uri in body["uris"]))

    def test_a_name_that_matches_an_artist_plays_the_artist(self) -> None:
        fake = FakeSpotify()
        reply = controller(fake).run(MusicCommand("play", "drake"))
        self.assertEqual(reply, "Playing Drake.")
        self.assertEqual(self.last_play(fake)[2], {"context_uri": "spotify:artist:drake"})

    def test_a_song_title_plays_the_track(self) -> None:
        fake = FakeSpotify()
        fake.search_results["artists"]["items"][0]["name"] = "Someone Else"
        reply = controller(fake).run(MusicCommand("play", "hotline bling"))
        self.assertEqual(reply, "Playing Hotline Bling by Drake.")
        self.assertEqual(self.last_play(fake)[2], {"uris": ["spotify:track:hb"]})

    def test_your_own_playlist_wins(self) -> None:
        fake = FakeSpotify()
        reply = controller(fake).run(MusicCommand("play", "gym", "playlist"))
        self.assertIn("Gym Bangers", reply)
        self.assertEqual(self.last_play(fake)[2], {"context_uri": "spotify:playlist:gym"})

    def test_public_playlist_search_skips_null_items(self) -> None:
        fake = FakeSpotify(playlists=[])
        reply = controller(fake).run(MusicCommand("play", "jazz", "playlist"))
        self.assertEqual(reply, "Playing Jazz Classics.")

    def test_no_active_device_wakes_the_desktop_app(self) -> None:
        fake = FakeSpotify(active=False, devices=[])
        ctl = controller(fake)

        # The device shows up after the app is opened.
        def devices_after_open():
            if ctl.opened:  # type: ignore[attr-defined]
                fake.devices = [{"id": "pc", "type": "Computer", "name": "PC"}]
        original = fake.__call__

        def transport(method, url, headers, body):
            devices_after_open()
            return original(method, url, headers, body)

        ctl._client._transport = transport  # type: ignore[attr-defined]
        reply = ctl.run(MusicCommand("liked"))
        self.assertIn("liked songs", reply)
        self.assertEqual(ctl.opened, ["spotify:"])  # type: ignore[attr-defined]
        self.assertEqual(self.last_play(fake)[3], {"device_id": "pc"})

    def test_free_accounts_fall_back_to_media_keys(self) -> None:
        fake = FakeSpotify(premium=False)
        reply = controller(fake).run(MusicCommand("resume"))
        self.assertIn("Premium", reply)

    def test_transport_controls_use_the_api(self) -> None:
        fake = FakeSpotify()
        ctl = controller(fake)
        self.assertEqual(ctl.run(MusicCommand("pause")), "Paused.")
        self.assertEqual(ctl.run(MusicCommand("next")), "Skipped.")
        self.assertEqual(ctl.run(MusicCommand("now")), "Hotline Bling by Drake.")
        paths = [(method, path) for method, path, _, _ in fake.calls]
        self.assertIn(("PUT", "/me/player/pause"), paths)
        self.assertIn(("POST", "/me/player/next"), paths)

    def test_expired_token_is_refreshed_and_saved(self) -> None:
        saved = []
        client = SpotifyClient(
            {"access_token": "old", "refresh_token": "r", "expires_at": 0},
            transport=FakeSpotify(),
            refresh=lambda t: {**t, "access_token": "new", "expires_at": time.time() + 3600},
            persist=saved.append,
        )
        client.devices()
        self.assertEqual(saved[0]["access_token"], "new")


class WithoutLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys: list[int] = []
        self.opened: list[str] = []
        self.ctl = MusicController(None, media_key=lambda c: self.keys.append(c) or True,
                                   open_uri=self.opened.append)

    def test_media_keys_cover_the_basics(self) -> None:
        self.ctl.run(MusicCommand("pause"))
        self.ctl.run(MusicCommand("next"))
        self.assertEqual(self.keys, [VK_MEDIA_PLAY_PAUSE, VK_MEDIA_NEXT_TRACK])

    def test_liked_songs_opens_the_collection_and_says_so(self) -> None:
        reply = self.ctl.run(MusicCommand("liked"))
        self.assertEqual(self.opened, ["spotify:collection:tracks"])
        self.assertIn("press play", reply)
        self.assertIn("spotify login", reply)

    def test_play_opens_a_spotify_search(self) -> None:
        self.ctl.run(MusicCommand("play", "blinding lights"))
        self.assertEqual(self.opened, ["spotify:search:blinding%20lights"])

    def test_reply_names_the_web_player_when_the_app_is_missing(self) -> None:
        ctl = MusicController(None, media_key=lambda c: True, open_uri=lambda uri: "web")
        self.assertIn("web player", ctl.run(MusicCommand("liked")))
        app = MusicController(None, media_key=lambda c: True, open_uri=lambda uri: "app")
        self.assertNotIn("web player", app.run(MusicCommand("liked")))

    def test_web_urls_match_the_app_links(self) -> None:
        self.assertEqual(music.web_url("spotify:collection:tracks"),
                         "https://open.spotify.com/collection/tracks")
        self.assertEqual(music.web_url("spotify:search:a%20b"), "https://open.spotify.com/search/a%20b")
        self.assertEqual(music.web_url("spotify:playlist:x1"), "https://open.spotify.com/playlist/x1")
        self.assertEqual(music.web_url("spotify:"), "https://open.spotify.com/")


class OAuthTests(unittest.TestCase):
    def test_pkce_challenge_is_s256_of_the_verifier(self) -> None:
        verifier, challenge = pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(challenge, expected)

    def test_authorize_url_carries_pkce_and_scopes(self) -> None:
        url = authorize_url("cid", music.DEFAULT_REDIRECT, "chal", "st")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["redirect_uri"], "http://127.0.0.1:8888/callback")
        self.assertIn("user-modify-playback-state", query["scope"])
        self.assertNotIn("client_secret", query)

    def test_login_refuses_a_non_loopback_redirect(self) -> None:
        with self.assertRaises(ValueError):
            music.login("cid", "http://example.com/callback")

    def test_tokens_round_trip_and_forget(self) -> None:
        import os
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "spotify.json")
        music.save_tokens({"refresh_token": "r", "access_token": "a"}, path)
        self.assertEqual(music.load_tokens(path)["refresh_token"], "r")
        self.assertTrue(music.forget_tokens(path))
        self.assertIsNone(music.load_tokens(path))


class FastReplyTests(unittest.TestCase):
    def test_voice_fast_path_routes_music_before_apps(self) -> None:
        from xenon.__main__ import _voice_fast_reply

        keys = []
        ctl = MusicController(None, media_key=lambda c: keys.append(c) or True, open_uri=lambda u: None)
        self.assertEqual(_voice_fast_reply("skip this song", music_controller=ctl), "Skipped.")
        self.assertEqual(keys, [VK_MEDIA_NEXT_TRACK])

    def test_typed_wake_word_is_stripped_before_parsing(self) -> None:
        from xenon.__main__ import _voice_fast_reply

        opened = []
        ctl = MusicController(None, media_key=lambda c: True, open_uri=lambda u: opened.append(u) or "web")
        _voice_fast_reply("play my liked songs xenon", music_controller=ctl)
        self.assertEqual(opened, ["spotify:collection:tracks"])


if __name__ == "__main__":
    unittest.main()
