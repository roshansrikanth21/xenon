from __future__ import annotations

import dataclasses
import os
import sqlite3
import tempfile
import threading
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from xenon.actions import Risk
from xenon.approvals import (
    ApprovalBroker,
    AuditLog,
    Field,
    PendingAction,
    RateCaps,
    canonical_digest,
    normalise_spoken,
)

_SWITCHES = ("XENON_OUTWARD", "XENON_VOICE_APPROVE", "XENON_AGENT")


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_pending(action_id: str = "a1", risk: Risk = Risk.OUTWARD, task_id: str = "t1", **extra) -> PendingAction:
    payload = {"to": "Priya", "text": "see you at 5"}
    values = dict(
        action_id=action_id,
        kind="desktop_commit",
        risk=risk,
        title="Send to Priya",
        fields=(Field("To", "Priya", "your words"), Field("Text", "see you at 5", "drafter")),
        digest=canonical_digest("desktop_commit", payload),
        task_id=task_id,
    )
    values.update(extra)
    return PendingAction(**values)


class BrokerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in _SWITCHES:
            os.environ.pop(name, None)
        self.clock = FakeClock()
        self.caps = RateCaps(clock=self.clock)
        self.audit = AuditLog(None, clock=self.clock)
        self.shown: list[PendingAction] = []
        self.on_show = None

    def presenter(self, pending: PendingAction) -> None:
        self.shown.append(pending)
        if self.on_show is not None:
            self.on_show(pending)

    def broker(self, **kwargs) -> ApprovalBroker:
        options = dict(presenter=self.presenter, clock=self.clock, caps=self.caps, audit=self.audit)
        options.update(kwargs)
        return ApprovalBroker(**options)

    def approve_on_show(self, broker: ApprovalBroker) -> None:
        self.on_show = lambda pending: broker.resolve(pending.action_id, "approve", via="click")


class TokenTests(BrokerTestCase):
    def test_token_is_one_shot(self) -> None:
        broker = self.broker()
        self.approve_on_show(broker)
        self.caps.start_task("t1", 1)
        pending = make_pending()
        token = broker.request(pending)
        self.assertIsNotNone(token)
        self.assertEqual(token.via, "click")
        self.assertTrue(broker.consume(token, pending.digest))
        self.assertFalse(broker.consume(token, pending.digest))
        # A second resolve of a settled card is a no-op.
        self.assertFalse(broker.resolve(pending.action_id, "approve"))

    def test_token_mac_from_other_key_rejected(self) -> None:
        broker = self.broker()
        other = self.broker()
        self.approve_on_show(broker)
        self.caps.start_task("t1", 1)
        pending = make_pending()
        token = broker.request(pending)
        self.assertFalse(other.consume(token, pending.digest))
        # Tampering with any covered field breaks the MAC too.
        forged = dataclasses.replace(token, digest="0" * 64)
        self.assertFalse(broker.consume(forged, "0" * 64))
        later = dataclasses.replace(token, expires_at=token.expires_at + 3600)
        self.assertFalse(broker.consume(later, pending.digest))
        voice = dataclasses.replace(token, via="voice")
        self.assertFalse(broker.consume(voice, pending.digest))
        # The genuine token is still good after the forgeries were refused.
        self.assertTrue(broker.consume(token, pending.digest))

    def test_key_is_not_exposed(self) -> None:
        key = b"k" * 32
        broker = self.broker(key=key)
        self.assertNotIn(key.hex(), repr(broker))
        self.assertNotIn("kkkk", repr(broker))
        self.assertFalse(hasattr(broker, "key"))
        self.assertFalse(hasattr(broker, "_key"))
        # A broker built without a key gets a fresh random one each time.
        first, second = self.broker(), self.broker()
        self.approve_on_show(first)
        self.caps.start_task("t1", 1)
        pending = make_pending()
        self.assertFalse(second.consume(first.request(pending), pending.digest))

    def test_consume_refuses_changed_digest_and_spends_token(self) -> None:
        broker = self.broker()
        self.approve_on_show(broker)
        self.caps.start_task("t1", 1)
        pending = make_pending()
        token = broker.request(pending)
        self.assertFalse(broker.consume(token, canonical_digest("desktop_commit", {"to": "Mallory"})))
        # Met changed content once: never tried again, even if it changes back.
        self.assertFalse(broker.consume(token, pending.digest))
        events = [row["result"] for row in self.audit.rows() if row["event"] == "commit_refused"]
        self.assertIn("refused: changed since you approved", events)


class TimeoutAndCancelTests(BrokerTestCase):
    def test_timeout_defaults_to_deny(self) -> None:
        broker = self.broker()
        self.caps.start_task("t1", 1)

        def advance(_pending: PendingAction) -> None:
            self.clock.now += 121.0

        self.on_show = advance
        self.assertIsNone(broker.request(make_pending()))
        self.assertEqual(broker.pending(), [])
        denied = [row for row in self.audit.rows() if row["event"] == "approval_denied"]
        self.assertEqual(denied[-1]["result"], "denied: timed out")
        # A click after the timeout changes nothing.
        self.assertFalse(broker.resolve("a1", "approve"))

    def test_token_expires_after_timeout(self) -> None:
        broker = self.broker()
        self.approve_on_show(broker)
        self.caps.start_task("t1", 1)
        pending = make_pending()
        token = broker.request(pending)
        self.clock.now += 121.0
        self.assertFalse(broker.consume(token, pending.digest))

    def test_cancel_event_denies(self) -> None:
        broker = self.broker()
        self.caps.start_task("t1", 2)
        cancel = threading.Event()
        self.on_show = lambda pending: cancel.set()
        self.assertIsNone(broker.request(make_pending("a1"), cancel=cancel))
        denied = [row for row in self.audit.rows() if row["event"] == "approval_denied"]
        self.assertEqual(denied[-1]["result"], "denied: task cancelled")
        # Already cancelled: no card at all.
        self.shown.clear()
        self.assertIsNone(broker.request(make_pending("a2"), cancel=cancel))
        self.assertEqual(self.shown, [])

    def test_cancel_from_other_thread_wakes_request(self) -> None:
        broker = self.broker()
        self.caps.start_task("t1", 1)
        cancel = threading.Event()
        timer = threading.Timer(0.05, cancel.set)
        timer.start()
        try:
            self.assertIsNone(broker.request(make_pending(), cancel=cancel))
        finally:
            timer.cancel()

    def test_deny_all_denies_waiting_card(self) -> None:
        broker = self.broker()
        self.caps.start_task("t1", 1)
        self.on_show = lambda pending: broker.deny_all("stopped")
        self.assertIsNone(broker.request(make_pending()))

    def test_no_presenter_refuses(self) -> None:
        broker = self.broker(presenter=None)
        self.caps.start_task("t1", 1)
        self.assertIsNone(broker.request(make_pending()))
        refused = [row for row in self.audit.rows() if row["event"] == "approval_refused"]
        self.assertEqual(refused[-1]["result"], "refused: start the orb to approve")

    def test_presenter_failure_denies(self) -> None:
        broker = self.broker()
        self.caps.start_task("t1", 1)

        def boom(_pending: PendingAction) -> None:
            raise RuntimeError("no Tk")

        self.on_show = boom
        self.assertIsNone(broker.request(make_pending()))

    def test_denied_risk_never_gets_a_card(self) -> None:
        broker = self.broker()
        self.approve_on_show(broker)
        self.assertIsNone(broker.request(make_pending(risk=Risk.DENIED)))
        self.assertEqual(self.shown, [])

    def test_outward_switch_off_refuses_outward_and_destructive(self) -> None:
        os.environ["XENON_OUTWARD"] = "off"
        broker = self.broker()
        self.approve_on_show(broker)
        self.caps.start_task("t1", 2)
        self.assertIsNone(broker.request(make_pending("a1", risk=Risk.OUTWARD)))
        self.assertIsNone(broker.request(make_pending("a2", risk=Risk.DESTRUCTIVE)))
        self.assertEqual(self.shown, [])
        # LOCAL cards are unaffected by the outward switch.
        self.assertIsNotNone(broker.request(make_pending("a3", risk=Risk.LOCAL)))


class CapTests(BrokerTestCase):
    def test_rate_cap_and_per_task_outward_cap(self) -> None:
        broker = self.broker()
        self.approve_on_show(broker)
        # Hourly: 10 outward approvals, the 11th is refused without a card.
        self.caps.start_task("big", 50)
        for index in range(10):
            self.assertIsNotNone(broker.request(make_pending(f"h{index}", task_id="big")))
            self.clock.now += 1.0
        shown = len(self.shown)
        self.assertIsNone(broker.request(make_pending("h10", task_id="big")))
        self.assertEqual(len(self.shown), shown)
        refused = [row for row in self.audit.rows() if row["event"] == "approval_refused"]
        self.assertTrue(refused[-1]["result"].startswith("refused: hourly outward cap"))
        # Sliding window: an hour later there is room again.
        self.clock.now += 3600.0
        self.assertIsNotNone(broker.request(make_pending("h11", task_id="big")))

        # Per task: one parsed outward intent allows one outward approval.
        self.caps.start_task("one", 1)
        self.assertIsNotNone(broker.request(make_pending("p1", task_id="one")))
        self.assertIsNone(broker.request(make_pending("p2", task_id="one")))
        refused = [row for row in self.audit.rows() if row["event"] == "approval_refused"]
        self.assertEqual(refused[-1]["result"], "refused: more outward actions than you asked for in this task")
        # A task nobody registered has a cap of zero.
        self.assertIsNone(broker.request(make_pending("p3", task_id="ghost")))
        # Denied cards do not use up the cap.
        self.caps.start_task("deny", 1)
        self.on_show = lambda pending: broker.resolve(pending.action_id, "deny")
        self.assertIsNone(broker.request(make_pending("d1", task_id="deny")))
        self.approve_on_show(broker)
        self.assertIsNotNone(broker.request(make_pending("d2", task_id="deny")))
        # LOCAL cards are not outward and not capped.
        self.assertIsNotNone(broker.request(make_pending("l1", risk=Risk.LOCAL, task_id="ghost")))

    def test_rate_caps_check_directly(self) -> None:
        clock = FakeClock()
        caps = RateCaps(per_hour=2, clock=clock)
        caps.start_task("t", 5)
        self.assertIsNone(caps.check("x", "t"))
        caps.note("x", "t")
        caps.note("x", "t")
        self.assertIn("hourly", caps.check("x", "t"))
        clock.now += 3601.0
        self.assertIsNone(caps.check("x", "t"))


class VoiceTests(BrokerTestCase):
    def _voice(self, broker: ApprovalBroker, pending: PendingAction, spoken: list[str]) -> list[bool]:
        results: list[bool] = []

        def speak(card: PendingAction) -> None:
            for words in spoken:
                results.append(broker.resolve(card.action_id, "approve", via="voice", spoken=words))
            broker.resolve(card.action_id, "deny")  # close the card if still up

        self.on_show = speak
        broker.request(pending)
        return results

    def test_voice_resolve_requires_voice_ok_and_nonce(self) -> None:
        broker = self.broker()
        self.caps.start_task("t1", 10)
        card = make_pending("v1", voice_ok=True, nonce="47")
        # Off by default.
        self.assertEqual(self._voice(broker, card, ["send 47"]), [False])
        os.environ["XENON_VOICE_APPROVE"] = "on"
        # Not voice_ok: click only.
        self.assertEqual(self._voice(broker, make_pending("v2", nonce="47"), ["send 47"]), [False])
        # Wrong or missing nonce.
        self.assertEqual(self._voice(broker, make_pending("v3", voice_ok=True, nonce="47"),
                                     ["send", "send 48", "yes", "send four seven"]), [False] * 4)
        # Destructive is never voice-approvable.
        self.assertEqual(self._voice(broker, make_pending("v4", risk=Risk.DESTRUCTIVE, voice_ok=True, nonce="47"),
                                     ["send 47"]), [False])
        # The "not in your request" badge makes a card click-only.
        self.assertEqual(self._voice(broker, make_pending("v5", voice_ok=True, nonce="47",
                                                          badges=("not in your request",)), ["send 47"]), [False])
        # Digits or English words both work.
        self.on_show = lambda pending: broker.resolve(pending.action_id, "approve", via="voice",
                                                      spoken="Send forty-seven.")
        token = broker.request(make_pending("v6", voice_ok=True, nonce="47"))
        self.assertIsNotNone(token)
        self.assertEqual(token.via, "voice")
        self.on_show = lambda pending: broker.resolve(pending.action_id, "approve", via="voice", spoken="send 47")
        self.assertIsNotNone(broker.request(make_pending("v7", voice_ok=True, nonce="47")))

    def test_spoken_cancel_denies(self) -> None:
        os.environ["XENON_VOICE_APPROVE"] = "on"
        broker = self.broker()
        self.caps.start_task("t1", 1)
        outcome: list[bool] = []
        self.on_show = lambda pending: outcome.append(
            broker.resolve(pending.action_id, "approve", via="voice", spoken="cancel"))
        self.assertIsNone(broker.request(make_pending(voice_ok=True, nonce="47")))
        self.assertEqual(outcome, [True])

    def test_normalise_spoken(self) -> None:
        self.assertEqual(normalise_spoken("Send forty-seven."), "send 47")
        self.assertEqual(normalise_spoken("send twenty"), "send 20")
        self.assertEqual(normalise_spoken("SEND 13"), "send 13")
        self.assertEqual(normalise_spoken("send thirteen"), "send 13")


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in _SWITCHES:
            os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "audit.db"

    def test_audit_never_stores_bodies(self) -> None:
        body = "meet me behind the library at 9 BODYMARKER"
        typed = "TYPEDMARKER hunter2"
        audit = AuditLog(self.path)
        self.addCleanup(audit.close)
        audit.record("approval_requested", action_id="a1", kind="desktop_commit", risk=Risk.OUTWARD,
                     digest="d" * 64, recipients=["Priya"], via="click", result="pending",
                     body=body, text=body, value=typed, fields=[body], reason=body)
        clock = FakeClock()
        caps = RateCaps(clock=clock)
        caps.start_task("t1", 1)
        broker = ApprovalBroker(clock=clock, caps=caps, audit=audit,
                                presenter=lambda pending: broker.resolve(pending.action_id, "approve"))
        pending = PendingAction("a2", "desktop_commit", Risk.OUTWARD, "Send",
                                (Field("Text", body, "drafter"), Field("Typed", typed, "your words")),
                                canonical_digest("desktop_commit", {"text": body}), task_id="t1")
        token = broker.request(pending)
        from xenon import gate
        from xenon.actions import ActionResult

        gate.commit_desktop(token, broker, lambda: pending.digest, lambda: ActionResult(True, body))
        audit.close()
        conn = sqlite3.connect(str(self.path))
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(audit)")]
        finally:
            conn.close()
        self.assertEqual(columns, ["id", "ts", "event", "action_id", "task_id", "kind", "risk", "digest",
                                   "recipients", "via", "result"])
        raw = self.path.read_bytes()
        self.assertNotIn(b"BODYMARKER", raw)
        self.assertNotIn(b"TYPEDMARKER", raw)
        self.assertNotIn(b"hunter2", raw)
        reopened = AuditLog(self.path)
        self.addCleanup(reopened.close)
        rows = reopened.rows()
        self.assertIn('["Priya"]', [row["recipients"] for row in rows])
        self.assertEqual([row["event"] for row in rows][-1], "committed")

    def test_audit_redacts_secrets_in_allowed_columns(self) -> None:
        audit = AuditLog(None)
        entry = audit.record("x", action_id="a", recipients=["gsk_" + "A" * 40])
        self.assertNotIn("A" * 40, entry["recipients"])

    def test_reconcile_pending(self) -> None:
        audit = AuditLog(self.path)
        audit.record("approval_requested", action_id="crashed", kind="desktop_commit", result="pending")
        audit.record("approval_requested", action_id="done", kind="desktop_commit", result="pending")
        audit.record("approval_granted", action_id="done", kind="desktop_commit", result="approved")
        audit.record("commit_started", action_id="done", result="pending")
        audit.close()
        # Next start: a fresh log on the same file.
        restarted = AuditLog(self.path)
        self.addCleanup(restarted.close)
        left = restarted.reconcile_pending()
        self.assertEqual([(row["action_id"], row["event"]) for row in left],
                         [("crashed", "approval_requested"), ("done", "commit_started")])
        self.assertEqual(restarted.reconcile_pending(), [])
        results = {(row["action_id"], row["event"]): row["result"] for row in restarted.rows()}
        self.assertEqual(results[("crashed", "approval_requested")], "abandoned")
        self.assertEqual(results[("done", "approval_requested")], "approved")

    def test_reconcile_pending_in_memory(self) -> None:
        audit = AuditLog(None)
        audit.record("approval_requested", action_id="a", result="pending")
        self.assertEqual(len(audit.reconcile_pending()), 1)
        self.assertEqual(audit.reconcile_pending(), [])


class DigestTests(unittest.TestCase):
    def test_canonical_digest_stable_and_invisible_insensitive(self) -> None:
        base = canonical_digest("desktop_commit", {"to": "Priya", "text": "café at 5"})
        self.assertEqual(len(base), 64)
        # Key order does not matter.
        self.assertEqual(base, canonical_digest("desktop_commit", {"text": "café at 5", "to": "Priya"}))
        # Zero-width and bidi controls are stripped.
        self.assertEqual(base, canonical_digest("desktop_commit", {"to": "Pri​ya", "text": "café‍ at 5"}))
        self.assertEqual(base, canonical_digest("desktop_commit", {"to": "‪Priya‬", "text": "café at 5"}))
        # NFD and NFC spellings agree.
        nfd = unicodedata.normalize("NFD", "café at 5")
        self.assertEqual(base, canonical_digest("desktop_commit", {"to": "Priya", "text": nfd}))
        # Real changes do not.
        self.assertNotEqual(base, canonical_digest("desktop_commit", {"to": "Priya", "text": "cafe at 5"}))
        self.assertNotEqual(base, canonical_digest("desktop_commit", {"to": "Priyа", "text": "café at 5"}))
        self.assertNotEqual(base, canonical_digest("desktop_destroy", {"to": "Priya", "text": "café at 5"}))
        # Compatibility forms are NOT folded (full-width differs from ASCII).
        self.assertNotEqual(base, canonical_digest("desktop_commit", {"to": "Ｐriya", "text": "café at 5"}))


class FieldTests(unittest.TestCase):
    def test_unknown_source_is_page(self) -> None:
        self.assertEqual(Field("x", "y", "your-words").source, "page")
        self.assertEqual(Field("x", "y", "your words").source, "your words")


if __name__ == "__main__":
    unittest.main()
