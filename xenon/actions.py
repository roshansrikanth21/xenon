"""Approval-gated desktop actions and the observe -> plan -> act -> verify runner.

The policy is deliberately boring: read-only actions run, side effects need an
explicit approval event, and shell/destructive actions are refused outright.
The runner reports what actually happened, including "unverified" when no
verification succeeded.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

READ_ONLY_KINDS = frozenset({"observe", "wait"})
SIDE_EFFECT_KINDS = frozenset(
    {
        "open_app",
        "focus_window",
        "type_text",
        "press_keys",
        "click_control",
        "write_file",
    }
)
DENIED_KINDS = frozenset(
    {
        "run_command",
        "run_shell",
        "delete_file",
        "format_disk",
        "http_request",
    }
)


@dataclass(frozen=True)
class Action:
    kind: str
    target: str = ""
    value: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Verification:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class StepOutcome:
    action: Action
    approval_required: bool
    approved: bool | None
    result: ActionResult | None


@dataclass(frozen=True)
class TaskOutcome:
    goal: str
    status: str
    steps: tuple[StepOutcome, ...]
    verification: Verification | None = None
    detail: str = ""


class ActionBackend(Protocol):
    def execute(self, action: Action) -> ActionResult: ...


class Verifier(Protocol):
    def verify(self, goal: str) -> Verification: ...


Approver = Callable[[Action], bool]


class ApprovalPolicy:
    """Decides whether an action may run and whether a human must approve it."""

    def decide(self, action: Action) -> PolicyDecision:
        kind = action.kind.strip().lower()
        if kind in READ_ONLY_KINDS:
            return PolicyDecision(allowed=True)
        if kind in DENIED_KINDS:
            return PolicyDecision(
                allowed=False, reason=f"action {kind!r} is not permitted"
            )
        if kind in SIDE_EFFECT_KINDS:
            return PolicyDecision(allowed=True, requires_approval=True)
        return PolicyDecision(allowed=False, reason=f"unknown action kind {kind!r}")


class TaskRunner:
    """Executes a plan, stopping at the first refusal or failure."""

    def __init__(
        self,
        policy: ApprovalPolicy,
        backend: ActionBackend,
        approver: Approver | None = None,
        verifier: Verifier | None = None,
    ) -> None:
        self._policy = policy
        self._backend = backend
        self._approver = approver
        self._verifier = verifier

    def run(self, goal: str, actions: Sequence[Action]) -> TaskOutcome:
        completed: list[StepOutcome] = []

        for action in actions:
            decision = self._policy.decide(action)
            if not decision.allowed:
                return TaskOutcome(
                    goal=goal,
                    status="denied",
                    steps=tuple(completed),
                    detail=decision.reason,
                )

            approved: bool | None = None
            if decision.requires_approval:
                if self._approver is None:
                    return TaskOutcome(
                        goal=goal,
                        status="denied",
                        steps=tuple(completed),
                        detail=f"approval required for {action.kind!r} but no approver is configured",
                    )
                try:
                    approved = bool(self._approver(action))
                except Exception:
                    approved = False
                if not approved:
                    completed.append(
                        StepOutcome(
                            action=action,
                            approval_required=True,
                            approved=False,
                            result=None,
                        )
                    )
                    return TaskOutcome(
                        goal=goal,
                        status="denied",
                        steps=tuple(completed),
                        detail=f"user declined {action.kind!r}",
                    )

            try:
                result = self._backend.execute(action)
            except Exception as error:  # a backend bug must not look like success
                result = ActionResult(False, str(error))

            completed.append(
                StepOutcome(
                    action=action,
                    approval_required=decision.requires_approval,
                    approved=approved,
                    result=result,
                )
            )
            if not result.ok:
                return TaskOutcome(
                    goal=goal,
                    status="failed",
                    steps=tuple(completed),
                    detail=result.detail,
                )

        if self._verifier is None:
            return TaskOutcome(
                goal=goal,
                status="unverified",
                steps=tuple(completed),
                detail="no verifier is configured, so success cannot be claimed",
            )

        try:
            verification = self._verifier.verify(goal)
        except Exception as error:
            return TaskOutcome(
                goal=goal,
                status="unverified",
                steps=tuple(completed),
                detail=f"verification failed: {error}",
            )

        return TaskOutcome(
            goal=goal,
            status="succeeded" if verification.ok else "unverified",
            steps=tuple(completed),
            verification=verification,
            detail=verification.detail,
        )
