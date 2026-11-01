import unittest

from xenon.actions import (
    Action,
    ActionResult,
    ApprovalPolicy,
    TaskRunner,
    Verification,
)


class FakeBackend:
    def __init__(self, results: dict[str, ActionResult] | None = None) -> None:
        self.calls: list[Action] = []
        self.results = results or {}

    def execute(self, action: Action) -> ActionResult:
        self.calls.append(action)
        return self.results.get(action.kind, ActionResult(True, "done"))


class FakeVerifier:
    def __init__(self, verification: Verification) -> None:
        self.verification = verification
        self.goals: list[str] = []

    def verify(self, goal: str) -> Verification:
        self.goals.append(goal)
        return self.verification


class ApprovalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ApprovalPolicy()

    def test_read_only_actions_run_without_approval(self) -> None:
        decision = self.policy.decide(Action("observe"))

        self.assertTrue(decision.allowed)
        self.assertFalse(decision.requires_approval)

    def test_side_effects_require_approval(self) -> None:
        for kind in ("open_app", "type_text", "press_keys", "click_control"):
            with self.subTest(kind=kind):
                decision = self.policy.decide(Action(kind, target="x"))
                self.assertTrue(decision.allowed)
                self.assertTrue(decision.requires_approval)

    def test_shell_and_destructive_actions_are_denied(self) -> None:
        for kind in ("run_command", "delete_file", "format_disk"):
            with self.subTest(kind=kind):
                decision = self.policy.decide(Action(kind))
                self.assertFalse(decision.allowed)
                self.assertIn("not permitted", decision.reason)

    def test_unknown_kinds_are_denied(self) -> None:
        decision = self.policy.decide(Action("teleport"))

        self.assertFalse(decision.allowed)


class TaskRunnerTests(unittest.TestCase):
    def test_runs_read_only_steps_and_reports_verification(self) -> None:
        backend = FakeBackend()
        verifier = FakeVerifier(Verification(True, "title matched"))
        runner = TaskRunner(ApprovalPolicy(), backend, verifier=verifier)

        outcome = runner.run("look at the screen", [Action("observe")])

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(verifier.goals, ["look at the screen"])

    def test_side_effect_does_not_run_without_approval(self) -> None:
        backend = FakeBackend()
        runner = TaskRunner(ApprovalPolicy(), backend)

        outcome = runner.run("open notepad", [Action("open_app", target="notepad")])

        self.assertEqual(outcome.status, "denied")
        self.assertEqual(backend.calls, [])
        self.assertIn("approval", outcome.detail)

    def test_approver_can_deny_a_side_effect(self) -> None:
        backend = FakeBackend()
        runner = TaskRunner(ApprovalPolicy(), backend, approver=lambda action: False)

        outcome = runner.run("open notepad", [Action("open_app", target="notepad")])

        self.assertEqual(outcome.status, "denied")
        self.assertEqual(backend.calls, [])
        self.assertTrue(outcome.steps[0].approval_required)

    def test_approved_side_effect_executes(self) -> None:
        backend = FakeBackend()
        approved: list[Action] = []
        runner = TaskRunner(
            ApprovalPolicy(),
            backend,
            approver=lambda action: approved.append(action) or True,
            verifier=FakeVerifier(Verification(True)),
        )

        outcome = runner.run("type a note", [Action("type_text", value="hello")])

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(approved), 1)
        self.assertEqual(backend.calls[0].value, "hello")

    def test_read_only_steps_never_prompt(self) -> None:
        prompts: list[Action] = []
        runner = TaskRunner(
            ApprovalPolicy(),
            FakeBackend(),
            approver=lambda action: prompts.append(action) or True,
            verifier=FakeVerifier(Verification(True)),
        )

        runner.run("observe", [Action("observe")])

        self.assertEqual(prompts, [])

    def test_backend_failure_stops_the_plan(self) -> None:
        backend = FakeBackend({"type_text": ActionResult(False, "focus lost")})
        runner = TaskRunner(
            ApprovalPolicy(),
            backend,
            approver=lambda action: True,
            verifier=FakeVerifier(Verification(True)),
        )

        outcome = runner.run(
            "fill the form",
            [Action("type_text", value="a"), Action("press_keys", value="enter")],
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(len(backend.calls), 1)
        self.assertIn("focus lost", outcome.detail)

    def test_verification_failure_is_not_reported_as_success(self) -> None:
        runner = TaskRunner(
            ApprovalPolicy(),
            FakeBackend(),
            approver=lambda action: True,
            verifier=FakeVerifier(Verification(False, "title did not change")),
        )

        outcome = runner.run("open notepad", [Action("open_app", target="notepad")])

        self.assertEqual(outcome.status, "unverified")
        self.assertIn("title did not change", outcome.detail)

    def test_missing_verifier_is_reported_as_unverified(self) -> None:
        runner = TaskRunner(
            ApprovalPolicy(), FakeBackend(), approver=lambda action: True
        )

        outcome = runner.run("open notepad", [Action("open_app", target="notepad")])

        self.assertEqual(outcome.status, "unverified")
        self.assertIn("verifier", outcome.detail)

    def test_denied_step_stops_before_later_steps(self) -> None:
        backend = FakeBackend()
        runner = TaskRunner(ApprovalPolicy(), backend)

        outcome = runner.run(
            "dangerous",
            [Action("observe"), Action("run_command", value="rm -rf /")],
        )

        self.assertEqual(outcome.status, "denied")
        self.assertEqual([call.kind for call in backend.calls], ["observe"])

    def test_records_each_step_for_audit(self) -> None:
        runner = TaskRunner(
            ApprovalPolicy(),
            FakeBackend(),
            approver=lambda action: True,
            verifier=FakeVerifier(Verification(True)),
        )

        outcome = runner.run(
            "two steps",
            [Action("observe"), Action("type_text", value="hi")],
        )

        self.assertEqual([step.action.kind for step in outcome.steps], ["observe", "type_text"])
        self.assertTrue(all(step.result.ok for step in outcome.steps))


if __name__ == "__main__":
    unittest.main()
