import json
import unittest

from xenon.desktop import AgentDesktop, Envelope, flatten

TREE = {
    "role": "window", "name": "Calculator", "children": [
        {"role": "button", "name": "Close Calculator", "ref_id": "@s1:e1", "available_actions": ["Click"]},
        {"role": "group", "children": [
            {"role": "button", "name": "Seven", "ref_id": "@s1:e2", "available_actions": ["Click"]},
            {"role": "button", "name": "Hidden", "ref_id": "@s1:e3", "available_actions": ["Click"], "states": ["offscreen"]},
            {"role": "button", "name": "Greyed", "ref_id": "@s1:e4", "available_actions": ["Click"], "states": ["disabled"]},
            {"role": "scrollbar", "name": "Vertical", "ref_id": "@s1:e5", "available_actions": ["ScrollTo"]},
            {"role": "textfield", "name": "Body", "ref_id": "@s1:e6", "available_actions": ["SetValue"],
             "value": "x" * 500},
        ]},
        {"role": "dialog", "name": "Save changes?", "children": [
            {"role": "button", "name": "Don't save", "ref_id": "@s1:e7", "available_actions": ["Click"]},
        ]},
        {"role": "statictext", "name": "Display is 42"},
    ],
}


class FakeRunner:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, argv, stdin, timeout):
        self.calls.append((argv[1:], stdin))
        reply = self.replies.pop(0)
        return (0 if reply.get("ok") else 1), json.dumps(reply), ""


class FlattenTests(unittest.TestCase):
    def test_only_usable_elements_are_numbered_dialogs_first(self) -> None:
        elements, dropped = flatten(TREE)
        names = [e.name for e in elements]
        self.assertEqual(names[0], "Don't save")  # open dialog first
        self.assertIn("Seven", names)
        for hidden in ("Hidden", "Greyed", "Vertical", "Display is 42"):
            self.assertNotIn(hidden, names)
        self.assertEqual([e.id for e in elements], list(range(1, len(elements) + 1)))
        self.assertEqual(dropped, 0)

    def test_limit_reports_what_was_dropped(self) -> None:
        elements, dropped = flatten(TREE, limit=2)
        self.assertEqual(len(elements), 2)
        self.assertEqual(dropped, 2)

    def test_long_text_is_summarised_not_sent(self) -> None:
        elements, _ = flatten(TREE)
        body = next(e for e in elements if e.name == "Body")
        self.assertIn("500 characters", body.line())
        self.assertNotIn("xxxx", body.line())


class BridgeTests(unittest.TestCase):
    def test_snapshot_maps_ids_to_hidden_refs(self) -> None:
        runner = FakeRunner([{"ok": True, "command": "snapshot", "data": {
            "snapshot_id": "s1", "app": "CalculatorApp.exe", "complete": True,
            "window": {"id": "w-1", "title": "Calculator"}, "tree": TREE}}])
        desktop = AgentDesktop(exe="ad.exe", runner=runner)
        snap = desktop.snapshot(window_id="w-1")
        self.assertIn("--window-id", runner.calls[0][0])
        seven = next(e for e in snap.elements if e.name == "Seven")
        self.assertEqual(snap.by_id(seven.id).ref, "@s1:e2")
        self.assertNotIn("@s1", snap.render())  # the model never sees raw refs

    def test_snapshot_refuses_without_a_target(self) -> None:
        desktop = AgentDesktop(exe="ad.exe", runner=FakeRunner([]))
        with self.assertRaises(ValueError):
            desktop.snapshot()

    def test_set_value_sends_text_through_stdin(self) -> None:
        runner = FakeRunner([{"ok": True, "command": "set-value", "data": {
            "disposition": {"delivery": "delivered_verified", "retry": "unsafe"}}}])
        envelope = AgentDesktop(exe="ad.exe", runner=runner).set_value("@s1:e6", "secret text")
        self.assertEqual(runner.calls[0], (["set-value", "@s1:e6", "--stdin"], "secret text"))
        self.assertEqual(envelope.delivery, "delivered_verified")
        self.assertFalse(envelope.safe_to_retry)

    def test_only_not_delivered_is_safe_to_retry(self) -> None:
        self.assertTrue(Envelope(False, "click", error={"disposition": {"delivery": "not_delivered", "retry": "safe"}}).safe_to_retry)
        self.assertFalse(Envelope(False, "click", error={"disposition": {"delivery": "delivery_uncertain", "retry": "unknown"}}).safe_to_retry)

    def test_garbage_output_is_an_uncertain_failure(self) -> None:
        def runner(argv, stdin, timeout):
            return 1, "", "boom"
        envelope = AgentDesktop(exe="ad.exe", runner=runner).click("@s1:e2")
        self.assertFalse(envelope.ok)
        self.assertEqual(envelope.code, "NO_OUTPUT")
        self.assertFalse(envelope.safe_to_retry)

    def test_find_window_prefers_exact_then_shortest(self) -> None:
        windows = [{"id": "w-1", "title": "Calculator - Notes"}, {"id": "w-2", "title": "Calculator"}]
        runner = FakeRunner([{"ok": True, "command": "list-windows", "data": windows}])
        self.assertEqual(AgentDesktop(exe="ad.exe", runner=runner).find_window("calculator")["id"], "w-2")


class IntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        from xenon import desktop

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.exe = str(Path(folder.name, "agent-desktop-win32-x64.exe"))
        Path(self.exe).write_bytes(b"not the pinned build")
        patcher = mock.patch.dict(os.environ, {"AGENT_DESKTOP_EXE": self.exe})
        patcher.start()
        self.addCleanup(patcher.stop)
        desktop._HASH_CACHE.clear()

    def test_desktop_rejects_hash_mismatch(self) -> None:
        from xenon import desktop

        self.assertIsNone(desktop.find_exe())
        found, verified, detail = desktop.locate_exe()
        self.assertEqual(found, self.exe)
        self.assertIsNone(verified)
        self.assertIn("mismatch", detail)
        # Without a verified exe the bridge is simply unavailable.
        with self.assertRaises(desktop.DesktopUnavailable):
            desktop.AgentDesktop().run("version")

    def test_matching_hash_is_accepted_and_hashed_once(self) -> None:
        import hashlib
        from unittest import mock

        from xenon import desktop

        pinned = hashlib.sha256(b"not the pinned build").hexdigest()
        with mock.patch.object(desktop, "PINNED_SHA256", pinned), \
                mock.patch("xenon.desktop.hashlib.sha256", wraps=hashlib.sha256) as hasher:
            self.assertEqual(desktop.find_exe(), self.exe)
            self.assertEqual(desktop.find_exe(), self.exe)
        self.assertEqual(hasher.call_count, 1)  # cached per path + mtime

    def test_the_pin_is_the_audited_value(self) -> None:
        from xenon.desktop import PINNED_SHA256

        self.assertEqual(PINNED_SHA256, "d274667031322e50b9c2b1ab81c2ca46835d52cc932769a5c34bd4d481fd94c3")


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def make_dir(self, *parts: str, files=("refmap.json",)):
        folder = self.root.joinpath(*parts)
        folder.mkdir(parents=True)
        for name in files:
            (folder / name).write_text('{"inner": {}}', encoding="utf-8")
        return folder

    def test_every_call_carries_the_session(self) -> None:
        runner = FakeRunner([{"ok": True, "command": "click", "data": {}},
                             {"ok": True, "command": "set-value", "data": {}}])
        desktop = AgentDesktop(exe="ad.exe", runner=runner, session="xenon-t1")
        desktop.click("@s1:e2")
        desktop.set_value("@s1:e6", "hi")
        self.assertEqual(runner.calls[0][0], ["click", "--session", "xenon-t1", "@s1:e2"])
        self.assertEqual(runner.calls[1][0], ["set-value", "--session", "xenon-t1", "@s1:e6", "--stdin"])

    def test_bad_session_ids_are_refused(self) -> None:
        for bad in ("", "../x", "a b", "x" * 65):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                AgentDesktop(exe="ad.exe", runner=FakeRunner([]), session=bad)
        from xenon.desktop import new_session_id

        self.assertEqual(new_session_id("task 7/ok"), "xenon-task-7-ok")

    def test_session_snapshot_dirs_deleted(self) -> None:
        mine_session = self.make_dir("sessions", "xenon-t1", "snapshots", "sabc1234")
        mine_global = self.make_dir("snapshots", "sglobal01")
        other_global = self.make_dir("snapshots", "sother001")
        other_session = self.make_dir("sessions", "claude-t9")
        runner = FakeRunner([
            {"ok": True, "command": "snapshot", "data": {"snapshot_id": "sglobal01", "tree": {}}},
            {"ok": True, "command": "session", "data": {}},
        ])
        desktop = AgentDesktop(exe="ad.exe", runner=runner, session="xenon-t1", state_dir=self.root)
        desktop.snapshot(app="calc")

        removed = desktop.end_session()

        self.assertEqual(removed, 2)
        self.assertFalse(mine_session.parent.parent.exists())
        self.assertFalse(mine_global.exists())
        self.assertTrue(other_global.exists())
        self.assertTrue(other_session.exists())
        self.assertEqual(runner.calls[1][0], ["session", "--session", "xenon-t1", "end", "xenon-t1"])

    def test_sweep_removes_only_stale_refmaps_of_known_shape(self) -> None:
        import os
        import time

        from xenon.desktop import sweep_stale_snapshots

        old = [self.make_dir("snapshots", "s159dt9rjx0b3v"), self.make_dir("sessions", "xenon-old")]
        fresh = self.make_dir("snapshots", "sfresh0001")
        foreign_session = self.make_dir("sessions", "invoice-bot")
        odd_shape = self.make_dir("snapshots", "soddshape1", files=("refmap.json", "notes.txt"))
        not_ours = self.make_dir("snapshots", "keepme")
        stamp = time.time() - 7200
        for folder in (*old, foreign_session, odd_shape, not_ours):
            for path in (folder, *folder.rglob("*")):
                os.utime(path, (stamp, stamp))

        removed = sweep_stale_snapshots(max_age_s=3600, root=self.root)

        self.assertEqual(removed, 2)
        for folder in old:
            self.assertFalse(folder.exists())
        for folder in (fresh, foreign_session, odd_shape, not_ours):
            self.assertTrue(folder.exists(), folder)


if __name__ == "__main__":
    unittest.main()
