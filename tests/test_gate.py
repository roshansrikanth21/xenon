from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from unittest import mock

from xenon import gate
from xenon.actions import ActionResult, Risk
from xenon.approvals import ApprovalBroker, AuditLog, Field, PendingAction, RateCaps, canonical_digest

PACKAGE = Path(__file__).resolve().parents[1] / "xenon"


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("XENON_OUTWARD", "XENON_VOICE_APPROVE"):
            os.environ.pop(name, None)
        self.clock = FakeClock()
        self.audit = AuditLog(None, clock=self.clock)
        caps = RateCaps(clock=self.clock)
        caps.start_task("t1", 5)
        self.broker = ApprovalBroker(
            presenter=lambda pending: self.broker.resolve(pending.action_id, "approve"),
            clock=self.clock, caps=caps, audit=self.audit,
        )
        self.digest = canonical_digest("desktop_commit", {"to": "Priya", "text": "hi"})
        self.calls = 0

    def approve(self, action_id: str = "a1", risk: Risk = Risk.OUTWARD):
        pending = PendingAction(action_id, "desktop_commit", risk, "Send to Priya",
                                (Field("To", "Priya", "your words"),), self.digest, task_id="t1")
        token = self.broker.request(pending)
        self.assertIsNotNone(token)
        return token

    def execute(self) -> ActionResult:
        self.calls += 1
        return ActionResult(True, "sent")

    def test_digest_mismatch_refuses_commit(self) -> None:
        token = self.approve()
        changed = canonical_digest("desktop_commit", {"to": "Mallory", "text": "hi"})
        with self.assertRaises(gate.CommitRefused) as caught:
            gate.commit_desktop(token, self.broker, lambda: changed, self.execute)
        self.assertIn(gate.CHANGED, str(caught.exception))
        self.assertEqual(self.calls, 0)
        # The token was spent by the failed attempt; changing back does not revive it.
        with self.assertRaises(gate.CommitRefused):
            gate.commit_desktop(token, self.broker, lambda: self.digest, self.execute)
        self.assertEqual(self.calls, 0)

    def test_commit_executes_exactly_once(self) -> None:
        token = self.approve()
        reads: list[int] = []

        def reread() -> str:
            reads.append(self.calls)  # read before execute, never after
            return self.digest

        result = gate.commit_desktop(token, self.broker, reread, self.execute)
        self.assertTrue(result.ok)
        self.assertEqual(self.calls, 1)
        self.assertEqual(reads, [0])
        with self.assertRaises(gate.CommitRefused):
            gate.commit_desktop(token, self.broker, reread, self.execute)
        self.assertEqual(self.calls, 1)
        events = [row["event"] for row in self.audit.rows()]
        self.assertIn("commit_started", events)
        self.assertEqual(events[-2:], ["committed", "commit_refused"])

    def test_failure_is_never_retried(self) -> None:
        token = self.approve()

        def flaky() -> ActionResult:
            self.calls += 1
            raise OSError("UIA went away")

        result = gate.commit_desktop(token, self.broker, lambda: self.digest, flaky)
        self.assertFalse(result.ok)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.audit.rows()[-1]["event"], "commit_failed")

    def test_no_token_refuses(self) -> None:
        with self.assertRaises(gate.CommitRefused):
            gate.commit_desktop(None, self.broker, lambda: self.digest, self.execute)
        with self.assertRaises(gate.CommitRefused):
            gate.commit_destroy(None, self.broker, lambda: self.digest, self.execute)
        self.assertEqual(self.calls, 0)

    def test_reread_failure_refuses(self) -> None:
        token = self.approve()

        def broken() -> str:
            raise RuntimeError("window closed")

        with self.assertRaises(gate.CommitRefused):
            gate.commit_desktop(token, self.broker, broken, self.execute)
        self.assertEqual(self.calls, 0)

    def test_expired_token_refuses(self) -> None:
        token = self.approve()
        self.clock.now += 121.0
        with self.assertRaises(gate.CommitRefused):
            gate.commit_desktop(token, self.broker, lambda: self.digest, self.execute)
        self.assertEqual(self.calls, 0)

    def test_commit_destroy(self) -> None:
        token = self.approve(risk=Risk.DESTRUCTIVE)
        self.assertTrue(gate.commit_destroy(token, self.broker, lambda: self.digest, self.execute).ok)
        self.assertEqual(self.calls, 1)


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


class ChokePointTests(unittest.TestCase):
    def test_only_gate_calls_commit_primitives(self) -> None:
        files = sorted(PACKAGE.rglob("*.py"))
        self.assertTrue(files)
        offenders: list[str] = []
        for path in files:
            if path.resolve() == (PACKAGE / "gate.py").resolve():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _called_name(node)
                if name in gate.COMMIT_PRIMITIVES:
                    offenders.append(f"{path.relative_to(PACKAGE.parent)}:{node.lineno} calls {name}")
                for keyword in node.keywords:
                    if keyword.arg in {"allow_outward", "allow_destructive"}:
                        offenders.append(
                            f"{path.relative_to(PACKAGE.parent)}:{node.lineno} passes {keyword.arg}"
                        )
        self.assertEqual(offenders, [], "only xenon/gate.py may commit")

    def test_scan_catches_a_planted_call(self) -> None:
        tree = ast.parse("import x\nx.client.send_message('hi')\nsend_draft()\n")
        names = [_called_name(node) for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertIn("send_message", names)
        self.assertIn("send_draft", names)
        self.assertEqual(gate.COMMIT_PRIMITIVES,
                         frozenset({"_invoke_send", "send_draft", "send_message", "commit_primitive"}))


if __name__ == "__main__":
    unittest.main()
