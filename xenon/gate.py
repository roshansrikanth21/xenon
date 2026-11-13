"""The single commit choke point.

Only this module executes an approved OUTWARD or DESTRUCTIVE action, and
only with an ``ApprovalToken`` minted by the broker from a real click. Each
executor re-reads ground truth right before acting and spends the token
against it (``ApprovalBroker.consume``): a valid MAC, unused, unexpired, and
the digest of what is on screen now equal to the digest the card showed.

``tests/test_gate.py`` scans every module with the AST and fails the build
if anything other than this file calls a name in ``COMMIT_PRIMITIVES`` or
builds a ``WindowsActionBackend`` with ``allow_outward`` / ``allow_destructive``.
WhatsApp and email executors land in later phases.
"""
from __future__ import annotations

from typing import Callable

from xenon.actions import Action, ActionContext, ActionResult

COMMIT_PRIMITIVES = frozenset({"_invoke_send", "send_draft", "send_message", "commit_primitive"})


class CommitRefused(Exception):
    """The commit did not happen: no token, a changed target, or a spent token."""


CHANGED = "changed since you approved"


def _record(broker, event: str, token, kind: str, result: str) -> None:
    audit = getattr(broker, "audit", None)
    if audit is None:
        return
    try:
        audit.record(
            event,
            action_id=getattr(token, "action_id", ""),
            digest=getattr(token, "digest", ""),
            via=getattr(token, "via", ""),
            kind=kind,
            result=result,
        )
    except Exception:
        pass  # a full disk must not turn a refusal into a commit, or vice versa


def _commit(
    label: str,
    kind: str,
    token,
    broker,
    reread_digest: Callable[[], str],
    execute: Callable[[], ActionResult],
) -> ActionResult:
    """Re-read, spend the token against what is live, then act exactly once.

    Never retries: a send that may have half-happened is reported, not
    repeated (a retry is how one approval becomes two messages).
    """
    if token is None:
        _record(broker, "commit_refused", token, kind, "refused: no approval")
        raise CommitRefused(f"{label}: no approval; nothing was sent")
    try:
        # Immediately before acting, so the digest describes what Enter or
        # the click will actually commit.
        live = reread_digest()
    except Exception as error:
        _record(broker, "commit_refused", token, kind, "refused: could not re-read the target")
        raise CommitRefused(
            f"{label}: could not re-read the target ({type(error).__name__}); nothing was sent"
        ) from error
    if not broker.consume(token, live):
        # consume() has already written why (expired, used, changed).
        raise CommitRefused(
            f"{label}: {CHANGED} (or the approval expired or was already used); nothing was sent"
        )
    _record(broker, "commit_started", token, kind, "pending")
    try:
        result = execute()
    except Exception as error:
        result = ActionResult(False, f"{type(error).__name__}: {error}")
    if not isinstance(result, ActionResult):
        result = ActionResult(False, "the executor returned no result")
    _record(broker, "committed" if result.ok else "commit_failed", token, kind,
            "committed" if result.ok else "failed")
    return result


def commit_desktop(token, broker, reread_digest: Callable[[], str], execute: Callable[[], ActionResult]) -> ActionResult:
    """Commit one approved OUTWARD desktop action (click Send, press Enter...)."""
    return _commit("desktop commit", "desktop_commit", token, broker, reread_digest, execute)


def commit_destroy(token, broker, reread_digest: Callable[[], str], execute: Callable[[], ActionResult]) -> ActionResult:
    """Commit one approved DESTRUCTIVE action."""
    return _commit("destructive commit", "desktop_destroy", token, broker, reread_digest, execute)


def desktop_executor(
    action: Action,
    task_context: ActionContext | None = None,
    destructive: bool = False,
    backend_factory: Callable[..., object] | None = None,
    context_check: Callable[[ActionContext], str | None] | None = None,
) -> Callable[[], ActionResult]:
    """An ``execute`` callable for commit_desktop / commit_destroy.

    The permissive backend is built only when the gate calls this, i.e.
    after the token was spent. It still re-classifies the live foreground
    and refuses DENIED surfaces. ``context_check(live_ctx)`` is run against
    the backend's own live read, right before input is sent: it returns a
    reason when that target no longer matches the approved digest (focus
    moved to another chat or window between the gate's read and the send).
    """

    def run() -> ActionResult:
        if backend_factory is not None:
            factory = backend_factory
        else:
            from xenon.windows_actions import WindowsActionBackend as factory
        extra = {"context_check": context_check} if context_check is not None else {}
        if destructive:
            backend = factory(allow_destructive=True, task_context=task_context, **extra)
        else:
            backend = factory(allow_outward=True, task_context=task_context, **extra)
        return backend.execute(action)

    return run
