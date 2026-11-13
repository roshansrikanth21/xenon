from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from xenon import ipc


class HandleRequestTests(unittest.TestCase):
    """The protocol, without a pipe: one verb-less request shape only."""

    SECRET = "s3cret-value"

    def setUp(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def submit(self, goal: str, origin: str) -> str:
        self.calls.append((goal, origin))
        return "task123"

    def handle(self, payload) -> dict:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        with redirect_stderr(StringIO()):
            return ipc.handle_request(raw, self.SECRET, self.submit)

    def test_valid_request_submits_as_ipc(self) -> None:
        reply = self.handle({"secret": self.SECRET, "goal": "  tidy downloads ", "source": "typed"})
        self.assertEqual(reply, {"ok": True, "task_id": "task123"})
        self.assertEqual(self.calls, [("tidy downloads", "ipc")])

    def test_wrong_secret_rejected_and_logged(self) -> None:
        err = StringIO()
        with redirect_stderr(err):
            reply = ipc.handle_request(json.dumps({"secret": "nope", "goal": "x"}).encode(), self.SECRET, self.submit)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "wrong secret")
        self.assertIn("wrong secret", err.getvalue())
        self.assertNotIn(self.SECRET, err.getvalue())
        self.assertNotIn("nope", err.getvalue())
        self.assertEqual(self.calls, [])

    def test_unknown_verbs_rejected(self) -> None:
        for payload in (
            {"secret": self.SECRET, "approve": "a1"},
            {"secret": self.SECRET, "goal": "send it", "approve": True},
            {"secret": self.SECRET, "goal": "x", "verb": "approve", "action_id": "a1"},
            {"secret": self.SECRET, "goal": "x", "decision": "approve"},
        ):
            with self.subTest(payload=payload):
                reply = self.handle(payload)
                self.assertFalse(reply["ok"])
        self.assertEqual(self.calls, [])

    def test_malformed_requests_rejected(self) -> None:
        for raw in (b"not json", b"[1,2]", b"\xff\xfe", json.dumps({"secret": self.SECRET}).encode(),
                    json.dumps({"secret": self.SECRET, "goal": "   "}).encode(),
                    json.dumps({"secret": self.SECRET, "goal": 5}).encode(),
                    json.dumps({"secret": self.SECRET, "goal": "x" * (ipc.MAX_GOAL + 1)}).encode(),
                    b"x" * (ipc.MAX_REQUEST + 1)):
            with self.subTest(raw=raw[:40]):
                self.assertFalse(self.handle(raw)["ok"])
        self.assertEqual(self.calls, [])

    def test_empty_server_secret_refuses_everything(self) -> None:
        with redirect_stderr(StringIO()):
            reply = ipc.handle_request(json.dumps({"secret": "", "goal": "x"}).encode(), "", self.submit)
        self.assertFalse(reply["ok"])
        self.assertEqual(self.calls, [])

    def test_protocol_has_no_approve_surface(self) -> None:
        self.assertEqual(ipc.REQUEST_FIELDS, frozenset({"secret", "goal", "source"}))
        self.assertFalse(any("approve" in name.lower() for name in dir(ipc)))


class ClientWithoutServerTests(unittest.TestCase):
    def test_send_task_returns_none_quickly_without_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "ipc.key"
            key.write_text("whatever", encoding="ascii")
            started = time.monotonic()
            result = ipc.send_task("x", name=rf"\\.\pipe\xenon-test-absent-{uuid.uuid4().hex}", key_file=key)
            self.assertIsNone(result)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_send_task_without_key_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ipc.send_task("x", key_file=Path(tmp) / "missing.key"))


@unittest.skipUnless(sys.platform == "win32", "named pipes are Windows-only")
class PipeRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.key = Path(self.tmp.name) / "Xenon" / "ipc.key"
        self.name = rf"\\.\pipe\xenon-test-{uuid.uuid4().hex}"
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

        def submit(goal: str, origin: str) -> str:
            with self.lock:
                self.calls.append((goal, origin))
            return f"t{len(self.calls)}"

        self.server = ipc.IpcServer(submit=submit, name=self.name, key_file=self.key)
        self.assertTrue(self.server.start())
        self.assertTrue(self.server.wait_ready(3), self.server.error)

    def tearDown(self) -> None:
        with redirect_stderr(StringIO()):
            self.server.stop()
        self.tmp.cleanup()

    def test_round_trip(self) -> None:
        self.assertTrue(self.key.exists())
        self.assertEqual(ipc.send_task("organise my screenshots", name=self.name, key_file=self.key), "t1")
        # The same pipe instance serves the next client too.
        self.assertEqual(ipc.send_task("second goal", name=self.name, key_file=self.key), "t2")
        self.assertEqual(self.calls, [("organise my screenshots", "ipc"), ("second goal", "ipc")])

    def test_wrong_secret_rejected_over_pipe(self) -> None:
        request = json.dumps({"secret": "guess", "goal": "x", "source": "cli"}).encode()
        with redirect_stderr(StringIO()):
            reply = ipc.exchange_raw(self.name, request)
        self.assertEqual(reply, {"ok": False, "error": "wrong secret"})
        self.assertEqual(self.calls, [])

    def test_unknown_verb_rejected_over_pipe(self) -> None:
        secret = ipc.read_secret(self.key)
        request = json.dumps({"secret": secret, "approve": "action-1"}).encode()
        with redirect_stderr(StringIO()):
            reply = ipc.exchange_raw(self.name, request)
        self.assertFalse(reply["ok"])
        self.assertEqual(self.calls, [])

    def test_secret_rotates_each_start_and_is_removed_on_stop(self) -> None:
        first = ipc.read_secret(self.key)
        with redirect_stderr(StringIO()):
            self.server.stop()
        self.assertFalse(self.key.exists())
        self.server = ipc.IpcServer(submit=lambda g, o: "t", name=self.name + "-2", key_file=self.key)
        self.assertTrue(self.server.start())
        self.assertTrue(self.server.wait_ready(3), self.server.error)
        self.assertNotEqual(ipc.read_secret(self.key), first)

    def test_second_server_on_same_name_refuses(self) -> None:
        other_key = Path(self.tmp.name) / "other.key"
        other = ipc.IpcServer(submit=lambda g, o: "t", name=self.name, key_file=other_key)
        with redirect_stderr(StringIO()):
            self.assertTrue(other.start())
            self.assertFalse(other.wait_ready(3))
            self.assertIn("could not create the pipe", other.error)
            other.stop()

    def test_pipe_served_by_this_user_is_verified(self) -> None:
        import msvcrt

        with open(self.name, "r+b", buffering=0) as pipe:
            self.assertTrue(ipc.pipe_server_is_current_user(msvcrt.get_osfhandle(pipe.fileno())))


if __name__ == "__main__":
    unittest.main()
