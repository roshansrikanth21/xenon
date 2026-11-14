import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from xenon import remote


class PhoneFilterTests(unittest.TestCase):
    def test_allowed(self):
        for text in ("pause", "next song", "play my usual", "game mode on", "go xtreme", "start xtreme",
                     "volume up", "what's playing", "what do i listen to most", "who sang this"):
            with self.subTest(text=text):
                self.assertTrue(remote.allowed_from_phone(text))

    def test_refused(self):
        for text in ("do open notepad", "open chrome", "send a message to mom", "delete my files",
                     "type hello", "search for passwords", "start notepad", "xenon do anything",
                     "email my boss", "x" * 400, ""):
            with self.subTest(text=text):
                self.assertFalse(remote.allowed_from_phone(text))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.server = remote.RemoteServer(lambda t: self.seen.append(t) or "ok", "k" * 43, port=0)
        # port 0: let the OS pick a free one
        self.assertTrue(self.server.start())
        self.port = self.server._server.server_address[1]
        self.addCleanup(self.server.stop)

    def post(self, text, key):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/cmd", data=json.dumps({"text": text}).encode(),
                                     headers={"X-Xenon-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    def test_key_required(self):
        self.assertEqual(self.post("pause", "wrong")[0], 401)
        self.assertEqual(self.seen, [])
        code, body = self.post("pause", "k" * 43)
        self.assertEqual((code, body["reply"]), (200, "ok"))
        self.assertEqual(self.seen, ["pause"])

    def test_refused_words_never_reach_the_handler(self):
        code, body = self.post("do open notepad", "k" * 43)
        self.assertEqual(code, 200)
        self.assertEqual(self.seen, [])
        self.assertIn("laptop", body["reply"])

    def test_guessing_is_rate_limited(self):
        for _ in range(remote.MAX_FAILS_PER_MIN):
            self.post("pause", "bad")
        # even the right key is refused while blocked
        self.assertEqual(self.post("pause", "k" * 43)[0], 401)

    def test_page_is_served_without_the_key_and_carries_no_secret(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as r:
            page = r.read().decode()
            self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertIn("X-Xenon-Key", page)
        self.assertNotIn("k" * 43, page)

    def test_only_localhost(self):
        self.assertEqual(self.server._server.server_address[0], "127.0.0.1")


class ConfigTests(unittest.TestCase):
    def test_key_created_once_and_long(self):
        path = Path(tempfile.mkdtemp()) / "remote.json"
        first = remote.load_config(path)
        self.assertGreaterEqual(len(first["key"]), 40)
        self.assertEqual(remote.load_config(path)["key"], first["key"])
        self.assertTrue(remote.phone_link("https://a.trycloudflare.com/", "abc").endswith("/#k=abc"))


if __name__ == "__main__":
    unittest.main()
