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


if __name__ == "__main__":
    unittest.main()
