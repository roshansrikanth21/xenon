import json
import time
import unittest
import urllib.request

from xenon.companion import Companion
from xenon.lyrics import Lyrics, LyricsFinder, clean_title, parse_lrc


class LrcTests(unittest.TestCase):
    def test_parse_lrc_timestamps(self) -> None:
        lines = parse_lrc("[00:01.50]first line\n[01:02.00]second\nnot a lyric\n[00:00.00]")
        self.assertEqual(lines, [(0, ""), (1500, "first line"), (62000, "second")])

    def test_clean_title(self) -> None:
        self.assertEqual(clean_title("Song (feat. Someone)"), "Song")
        self.assertEqual(clean_title("Song - Remastered 2011"), "Song")
        self.assertEqual(clean_title("Plain"), "Plain")


class FinderTests(unittest.TestCase):
    def test_exact_match_then_cache(self) -> None:
        calls = []

        def fetch(path, params):
            calls.append((path, params))
            return {"syncedLyrics": "[00:01.00]hello", "plainLyrics": "hello"}

        finder = LyricsFinder(fetch)
        lyrics = finder.find("Song (feat. X)", "Artist, Other", 200000)
        self.assertEqual(lyrics.lines, [(1000, "hello")])
        self.assertEqual(calls[0][1], {"track_name": "Song", "artist_name": "Artist", "duration": 200})
        finder.find("Song (feat. X)", "Artist, Other", 200000)
        self.assertEqual(len(calls), 1)  # cached

    def test_search_fallback_picks_closest_duration(self) -> None:
        def fetch(path, params):
            if path == "get":
                return None
            return [
                {"duration": 150, "syncedLyrics": "[00:01.00]short"},
                {"duration": 214, "syncedLyrics": "[00:01.00]right one"},
                {"duration": 214, "syncedLyrics": None},
            ]

        lyrics = LyricsFinder(fetch).find("Mashup x Other", "Ecra", 216000)
        self.assertEqual(lyrics.lines, [(1000, "right one")])

    def test_network_errors_are_not_cached(self) -> None:
        attempts = []

        def fetch(path, params):
            attempts.append(path)
            raise OSError("offline")

        finder = LyricsFinder(fetch)
        self.assertIsNone(finder.find("Song", "Artist"))
        self.assertIsNone(finder.find("Song", "Artist"))
        self.assertEqual(len(attempts), 2)


class CompanionTests(unittest.TestCase):
    def make(self, music=None, lyrics=None):
        state = {"state": "listening", "timers": [], "last": "hi"}
        companion = Companion(lambda: state, (lambda: music) if music else None,
                              (lambda t, a, d: lyrics) if lyrics else None, port=0, host="127.0.0.1")
        return companion

    def test_snapshot_without_music_shows_xenon_state(self) -> None:
        snap = self.make().snapshot()
        self.assertEqual(snap["xenon"]["state"], "listening")
        self.assertIsNone(snap["music"])

    def test_snapshot_extrapolates_position_and_sends_lyrics_once(self) -> None:
        companion = self.make()
        companion._music = {"title": "Song", "artist": "A", "position_ms": 10000,
                            "duration_ms": 200000, "playing": True}
        companion._music_at = time.monotonic() - 2
        companion._track_lyrics["Song|A"] = Lyrics([(0, "la"), (12000, "da")])
        first = companion.snapshot()
        self.assertGreaterEqual(first["music"]["position_ms"], 11900)
        self.assertEqual(first["music"]["lyrics_state"], "synced")
        self.assertIn("lyrics", first["music"])
        again = companion.snapshot(have_lyrics="Song|A")
        self.assertNotIn("lyrics", again["music"])  # the page already has them

    def test_http_server_serves_page_and_state(self) -> None:
        companion = self.make()
        companion.start()
        self.addCleanup(companion.stop)
        port = companion._server.server_address[1]
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
        self.assertIn(b"<title>Xenon</title>", page)
        state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=5))
        self.assertEqual(state["xenon"]["state"], "listening")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/control", timeout=5)


class RemoteScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.companion = Companion(lambda: {"state": "idle"}, port=0, host="127.0.0.1")
        self.companion.start()
        self.addCleanup(self.companion.stop)
        self.base = f"http://127.0.0.1:{self.companion._server.server_address[1]}"

    def post(self, path, payload):
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                         headers=self.companion.push_headers(), method="POST")
        return json.load(urllib.request.urlopen(request, timeout=5))

    def test_screens_report_their_size_and_smoothness(self) -> None:
        urllib.request.urlopen(self.base + "/state?w=1280&h=800&dpr=1.5&fps=58", timeout=5).read()
        screens = json.load(urllib.request.urlopen(self.base + "/screens", timeout=5))
        info = screens["127.0.0.1"]
        self.assertEqual((info["width"], info["height"], info["fps"]), ("1280", "800", "58"))

    def test_pushed_text_reaches_the_page_until_cleared(self) -> None:
        self.post("/show", {"kind": "text", "content": "Dinner's ready", "seconds": 30})
        state = json.load(urllib.request.urlopen(self.base + "/state", timeout=5))
        self.assertEqual(state["overlay"]["content"], "Dinner's ready")
        self.post("/clear", {})
        state = json.load(urllib.request.urlopen(self.base + "/state", timeout=5))
        self.assertNotIn("overlay", state)

    def test_only_web_addresses_can_be_shown_as_pages(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/show", {"kind": "url", "content": "file:///C:/secret.txt"})
        self.assertEqual(caught.exception.code, 400)

    def test_lan_devices_cannot_push(self) -> None:
        from xenon.companion import lan_address

        address = lan_address()
        if address.startswith("127."):
            self.skipTest("no LAN address on this machine")
        # Reaching the server through the LAN address makes this machine look
        # like any other device on the network.
        lan = Companion(lambda: {}, port=0, host=address, network_public=lambda: False)
        lan.start()
        self.addCleanup(lan.stop)
        url = f"http://{address}:{lan._server.server_address[1]}/show"
        # Even with the right secret, a LAN client may not push.
        request = urllib.request.Request(url, data=b'{"kind": "text", "content": "x"}', method="POST",
                                         headers=lan.push_headers())
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://{address}:{lan._server.server_address[1]}/screens", timeout=5)


class HardeningTests(unittest.TestCase):
    """Phase 0: Host allowlist, POST secret, private redaction, Public profile."""

    def start(self, state=None, **kwargs):
        kwargs.setdefault("network_public", lambda: False)
        companion = Companion(lambda: state or {"state": "idle", "last": "hi"},
                              port=0, host="127.0.0.1", **kwargs)
        companion.start()
        self.addCleanup(companion.stop)
        return companion, companion._server.server_address[1]

    def raw(self, port, method, path, host, headers=None, body=None):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest(method, path, skip_host=True)
        if host is not None:
            connection.putheader("Host", host)
        for key, value in (headers or {}).items():
            connection.putheader(key, value)
        data = json.dumps(body).encode() if body is not None else b""
        connection.putheader("Content-Length", str(len(data)))
        connection.endheaders(data)
        response = connection.getresponse()
        response.read()
        return response.status

    def test_companion_rejects_foreign_host_header(self) -> None:
        _companion, port = self.start()
        for host in ("evil.example", f"evil.example:{port}", "127.0.0.1.nip.io",
                     "localhost.evil.example", None, ""):
            with self.subTest(host=host):
                self.assertEqual(self.raw(port, "GET", "/state", host), 403)
                self.assertEqual(self.raw(port, "GET", "/", host), 403)
        for host in (f"localhost:{port}", f"127.0.0.1:{port}", "LOCALHOST", "[::1]:8765"):
            with self.subTest(host=host):
                self.assertEqual(self.raw(port, "GET", "/state", host), 200)

    def test_host_allowlist_knows_this_pc(self) -> None:
        import socket

        companion = Companion(lambda: {}, port=0, local_ips=lambda: {"192.168.1.20"})
        self.assertTrue(companion.host_allowed("192.168.1.20:8765"))
        self.assertTrue(companion.host_allowed(socket.gethostname()))
        self.assertFalse(companion.host_allowed("192.168.1.99"))
        self.assertFalse(companion.host_allowed("attacker.example"))
        extra = Companion(lambda: {}, port=0, allowed_hosts=["tablet-pc.home"])
        self.assertTrue(extra.host_allowed("tablet-pc.home:8765"))

    def test_companion_post_requires_boot_secret(self) -> None:
        companion, port = self.start()
        host = f"127.0.0.1:{port}"
        payload = {"kind": "text", "content": "x"}
        self.assertTrue(companion.post_secret)
        self.assertEqual(self.raw(port, "POST", "/show", host, body=payload), 403)
        self.assertEqual(self.raw(port, "POST", "/show", host, {"X-Xenon-Secret": "guess"}, payload), 403)
        self.assertEqual(self.raw(port, "POST", "/clear", host, body={}), 403)
        self.assertEqual(self.raw(port, "POST", "/show", host, companion.push_headers(), payload), 200)
        other, _ = self.start()
        self.assertNotEqual(other.post_secret, companion.post_secret)  # per boot, per server

    def test_secret_is_published_for_the_cli_and_removed_on_stop(self) -> None:
        import tempfile
        from pathlib import Path

        from xenon.companion import read_post_secret

        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder, "companion.key")
            companion = Companion(lambda: {}, port=0, host="127.0.0.1", network_public=lambda: False,
                                  secret_path=key, publish_secret=True)
            companion.start()
            self.assertEqual(read_post_secret(key), companion.post_secret)
            companion.stop()
            self.assertFalse(key.exists())
        # Port 0 (tests) never publishes to the real location.
        self.assertFalse(Companion(lambda: {}, port=0)._publish_secret)

    def test_state_redacts_private_reply(self) -> None:
        private = {"state": "speaking", "detail": "reading your screen", "last": "Your bank balance is 42",
                   "private": True}
        snap = Companion(lambda: private, port=0).snapshot()
        self.assertEqual(snap["xenon"]["last"], "Private")
        self.assertEqual(snap["xenon"]["detail"], "Private task")
        self.assertNotIn("balance", json.dumps(snap["xenon"]))
        tainted = Companion(lambda: {"state": "idle", "last": "page text", "tainted": True}, port=0).snapshot()
        self.assertEqual(tainted["xenon"]["last"], "Private")
        public = Companion(lambda: {"state": "idle", "last": "It is 5 pm", "detail": ""}, port=0).snapshot()
        self.assertEqual(public["xenon"]["last"], "It is 5 pm")

    def test_public_profile_serves_only_this_subnet(self) -> None:
        """His home Wi-Fi is profiled Public and the tablet (192.168.1.11)
        must keep working: the page listens on the LAN but a Public or
        unknown profile only answers this PC's own private /24."""
        import os
        from unittest import mock

        from xenon.companion import LAN_OFF, LAN_OPEN, LAN_SUBNET

        own = lambda: {"127.0.0.1", "192.168.1.20"}
        with mock.patch.dict(os.environ, {"XENON_COMPANION_LAN": ""}):
            public = Companion(lambda: {}, port=0, host="0.0.0.0", network_public=lambda: True,
                               local_ips=own)
            self.assertEqual(public._choose_bind_host(), "0.0.0.0")
            self.assertEqual(public.resolve_profile(), LAN_SUBNET)
            self.assertIn("Public", public.lan_note)
            self.assertTrue(public.client_allowed("192.168.1.11"))   # the tablet
            self.assertTrue(public.client_allowed("127.0.0.1"))
            self.assertFalse(public.client_allowed("192.168.2.11"))  # another subnet
            self.assertFalse(public.client_allowed("10.20.30.40"))
            self.assertFalse(public.client_allowed("8.8.8.8"))

            unknown = Companion(lambda: {}, port=0, host="0.0.0.0", network_public=lambda: None,
                                local_ips=own)
            unknown._choose_bind_host()
            self.assertEqual(unknown.resolve_profile(), LAN_SUBNET)  # unknown resolves stricter
            self.assertFalse(unknown.client_allowed("172.16.5.5"))

            private = Companion(lambda: {}, port=0, host="0.0.0.0", network_public=lambda: False,
                                local_ips=own)
            private._choose_bind_host()
            self.assertEqual(private.resolve_profile(), LAN_OPEN)
            self.assertTrue(private.client_allowed("10.20.30.40"))

            # Before the profile is read (it is read off the Tk thread) the
            # stricter rule applies.
            fresh = Companion(lambda: {}, port=0, host="0.0.0.0", network_public=lambda: False,
                              local_ips=own)
            fresh._choose_bind_host()
            self.assertEqual(fresh.lan_mode, LAN_SUBNET)
            self.assertFalse(fresh.client_allowed("10.20.30.40"))

        with mock.patch.dict(os.environ, {"XENON_COMPANION_LAN": "1"}):
            forced = Companion(lambda: {}, port=0, host="0.0.0.0", network_public=lambda: True,
                               local_ips=own)
            self.assertEqual(forced._choose_bind_host(), "0.0.0.0")
            self.assertEqual(forced.resolve_profile(), LAN_OPEN)
            self.assertTrue(forced.client_allowed("10.20.30.40"))

        with mock.patch.dict(os.environ, {"XENON_COMPANION_LAN": "0"}):
            off = Companion(lambda: {}, port=0, host="0.0.0.0", network_public=lambda: False,
                            local_ips=own)
            self.assertEqual(off._choose_bind_host(), "127.0.0.1")
            self.assertEqual(off.lan_mode, LAN_OFF)
            self.assertFalse(off.client_allowed("192.168.1.11"))

    def test_lan_clients_never_see_questions_or_transcripts(self) -> None:
        heard = {"state": "heard", "label": "Heard", "detail": "do tell mom on whatsapp the pin is 4417",
                 "last": "It is 5 pm", "private": False, "timers": []}
        thinking = {"state": "thinking", "label": "Thinking",
                    "detail": "tell me if the mole on my back looks like melanoma", "last": "", "private": False}
        for state in (heard, thinking):
            with self.subTest(state=state["state"]):
                lan = Companion(lambda s=state: s, port=0).snapshot(lan=True)["xenon"]
                self.assertNotIn(state["detail"], json.dumps(lan))
                self.assertEqual(lan["detail"], state["label"])
                local = Companion(lambda s=state: s, port=0).snapshot()["xenon"]
                self.assertEqual(local["detail"], state["detail"])  # this PC still sees it
        lan = Companion(lambda: heard, port=0).snapshot(lan=True)["xenon"]
        self.assertEqual(lan["last"], "It is 5 pm")  # a non-private answer still shows

    def test_state_over_the_network_is_redacted_and_foreign_subnets_refused(self) -> None:
        companion = Companion(
            lambda: {"state": "heard", "label": "Heard", "detail": "secret question"},
            port=0, host="127.0.0.1", network_public=lambda: True,
        )
        # A request whose source is not this PC: drive the handler's view.
        companion.lan_mode = "subnet"
        companion._local_ips = lambda: {"192.168.1.20"}
        self.assertFalse(companion.client_allowed("192.168.7.9"))
        snap = companion.snapshot(lan=True)
        self.assertNotIn("secret question", json.dumps(snap))


if __name__ == "__main__":
    unittest.main()
