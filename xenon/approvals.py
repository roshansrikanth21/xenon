"""Approval cards, one-shot tokens, rate caps and the audit log.

The broker is the only thing that mints an ``ApprovalToken``, and it mints
one only from ``resolve()``: a real click on a card (the card itself rejects
injected clicks) or, when Roshan has opted in, a spoken nonce on a card that
allows voice. The CLI, IPC callers, models, recipes and replays have no path
to ``resolve``. ``gate.py`` spends a token with ``consume()``, which checks
the MAC, expiry, single use and that the content on screen still matches.

The HMAC key is 32 random bytes made when the broker is built (once per orb
process) and never written anywhere, so a token cannot outlive the process
or be forged from outside it.

Nothing here starts a thread; ``request`` blocks the calling task thread on
an Event while the card is up, and the audit database opens on first write.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from xenon.actions import Risk

# Where a field's value came from. "page" is page-authored (anything a web
# page, sender or app controls) and is the least trusted.
SOURCES = frozenset({"your words", "heard", "drafter", "claude", "page", "xenon"})

DEFAULT_TIMEOUT_S = 120.0
NOT_IN_REQUEST = "not in your request"
NO_PRESENTER = "start the orb to approve"
# How often a blocked request() wakes to look at its cancel Event.
_POLL_S = 0.1


@dataclass(frozen=True)
class Field:
    label: str
    value: str
    source: str

    def __post_init__(self) -> None:
        # An unknown source is shown as page-authored, never as his words.
        if self.source not in SOURCES:
            object.__setattr__(self, "source", "page")


@dataclass(frozen=True)
class PendingAction:
    action_id: str
    kind: str
    risk: Risk
    title: str
    fields: tuple[Field, ...]
    digest: str
    badges: tuple[str, ...] = ()
    voice_ok: bool = False
    nonce: str = ""
    task_id: str = ""
    created_at: float = 0.0


@dataclass(frozen=True)
class ApprovalToken:
    action_id: str
    digest: str
    expires_at: float
    via: str
    mac: str


def _remove_invisible(text: str) -> str:
    from xenon.untrusted import _is_invisible

    return "".join(char for char in text if not _is_invisible(char))


def _canonical(value):
    if isinstance(value, Risk):
        return value.value
    if isinstance(value, str):
        # NFC, not NFKC: compatibility folding would make two different
        # strings (full-width and ASCII) share a digest, which is looser.
        return unicodedata.normalize("NFC", _remove_invisible(value))
    if isinstance(value, dict):
        return {str(_canonical(str(key))): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _canonical(str(value))


def canonical_digest(kind: str, payload: dict) -> str:
    """sha256 hex of canonical JSON: sorted keys, NFC, invisibles stripped.

    Invisibles are stripped so a zero-width character smuggled into the
    live text cannot make the card show one thing while the digest of the
    "same" text differs, and so re-reading a control that adds or drops one
    does not spuriously refuse a commit.
    """
    body = json.dumps(
        {"kind": _canonical(kind), "payload": _canonical(payload)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _outward_switch() -> tuple[bool, str]:
    """``killswitch.outward_allowed`` with a fallback while that module is absent.

    The fallback reads XENON_OUTWARD itself; anything but an explicit "on"
    word when set means off (a typo in a safety flag never switches it on).
    """
    try:
        from xenon.killswitch import outward_allowed
    except ImportError:
        raw = os.environ.get("XENON_OUTWARD", "").strip().lower()
        if raw and raw not in {"1", "on", "true", "yes", "enabled", "enable"}:
            return False, "outward actions are switched off (XENON_OUTWARD=off)"
        return True, ""
    return outward_allowed()


def _voice_switch() -> bool:
    """XENON_VOICE_APPROVE: off unless explicitly on."""
    try:
        from xenon.killswitch import feature_enabled
    except ImportError:
        return os.environ.get("XENON_VOICE_APPROVE", "").strip().lower() in {"1", "on", "true"}
    return feature_enabled("VOICE_APPROVE", default=False)


# ---------------------------------------------------------------------------
# Rate caps
# ---------------------------------------------------------------------------


class RateCaps:
    """At most ``per_hour`` outward approvals an hour, and per-task intent caps.

    Both caps refuse. The per-task cap is the number of outward intents
    parsed from his words (``start_task``): a task that tries to send more
    than he asked for is stopped, and a task nobody registered has a cap of
    zero, because an unknown resolves to the stricter answer.
    """

    def __init__(self, per_hour: int = 10, clock: Callable[[], float] = time.time) -> None:
        self.per_hour = per_hour
        self._clock = clock
        self._lock = threading.Lock()
        self._times: list[float] = []
        self._task_caps: dict[str, int] = {}
        self._task_used: dict[str, int] = {}

    def _recent(self) -> list[float]:
        cutoff = self._clock() - 3600.0
        self._times = [stamp for stamp in self._times if stamp > cutoff]
        return self._times

    def allow(self, kind: str = "", task_id: str = "") -> bool:
        """True while under the sliding-window hourly cap."""
        with self._lock:
            return len(self._recent()) < self.per_hour

    def note(self, kind: str = "", task_id: str = "") -> None:
        """Count one approved outward action (hourly and, if given, per task)."""
        with self._lock:
            self._recent()
            self._times.append(self._clock())
            if task_id:
                self._task_used[task_id] = self._task_used.get(task_id, 0) + 1

    def start_task(self, task_id: str, outward_intents: int) -> None:
        with self._lock:
            self._task_caps[task_id] = max(0, int(outward_intents))
            self._task_used.setdefault(task_id, 0)

    def within_task(self, task_id: str) -> bool:
        """True while the task is under its outward-intent cap. Unknown: False."""
        with self._lock:
            cap = self._task_caps.get(task_id)
            if cap is None:
                return False
            return self._task_used.get(task_id, 0) < cap

    def check(self, kind: str = "", task_id: str = "") -> str | None:
        """The reason an outward approval is refused now, or None."""
        if not self.allow(kind, task_id):
            return f"hourly outward cap reached ({self.per_hour} per hour)"
        if not self.within_task(task_id):
            return "more outward actions than you asked for in this task"
        return None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

# The only columns. Bodies, typed text and message content have no column,
# so a caller that passes them loses them here.
AUDIT_COLUMNS = ("ts", "event", "action_id", "task_id", "kind", "risk", "digest", "recipients", "via", "result")
_FIELD_COLUMNS = frozenset(AUDIT_COLUMNS) - {"ts", "event"}
PENDING = "pending"
ABANDONED = "abandoned"


def default_audit_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "Xenon" / "audit.db"


def _clean(value, limit: int = 200) -> str:
    from xenon.untrusted import redact_secrets, strip_invisible

    if isinstance(value, Risk):
        return value.value
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_clean(item, 80) for item in list(value)[:20]]
        return json.dumps(items, ensure_ascii=False)
    return redact_secrets(strip_invisible(str(value)))[:limit]


class AuditLog:
    """Record of approvals and commits: digests, kinds and recipients, never bodies.

    A row opened as ``result='pending'`` (a card shown, a commit started) is
    closed by the next terminal event for the same action id. Rows still
    pending at startup were cut off by a crash; ``reconcile_pending`` marks
    them ``abandoned`` so each is reported once. ``path=None`` keeps rows in
    memory only (tests, or no profile folder).
    """

    def __init__(self, path: str | Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._memory: list[dict] = []

    def _db(self) -> sqlite3.Connection | None:
        if self._path is None:
            return None
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Records come from the task thread and the Tk thread; the lock
            # serialises them, so one shared connection is safe.
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts REAL, event TEXT, "
                "action_id TEXT, task_id TEXT, kind TEXT, risk TEXT, digest TEXT, recipients TEXT, "
                "via TEXT, result TEXT)"
            )
            self._conn.commit()
        return self._conn

    def record(self, event: str, **fields) -> dict:
        """Append one row. Unknown keyword fields are dropped, not stored."""
        row = {name: _clean(value) for name, value in fields.items() if name in _FIELD_COLUMNS}
        entry = {column: "" for column in AUDIT_COLUMNS}
        entry.update(row)
        entry["ts"] = float(self._clock())
        entry["event"] = _clean(event, 40)
        result = entry["result"]
        action_id = entry["action_id"]
        with self._lock:
            db = self._db()
            # A terminal event closes the action's open rows with its result.
            close = action_id and result and result != PENDING
            if db is None:
                if close:
                    for old in self._memory:
                        if old["action_id"] == action_id and old["result"] == PENDING:
                            old["result"] = result
                self._memory.append(dict(entry))
            else:
                if close:
                    db.execute(
                        "UPDATE audit SET result = ? WHERE action_id = ? AND result = ?",
                        (result, action_id, PENDING),
                    )
                db.execute(
                    f"INSERT INTO audit ({', '.join(AUDIT_COLUMNS)}) VALUES ({', '.join('?' * len(AUDIT_COLUMNS))})",
                    tuple(entry[column] for column in AUDIT_COLUMNS),
                )
                db.commit()
        return entry

    def rows(self) -> list[dict]:
        with self._lock:
            db = self._db()
            if db is None:
                return [dict(entry) for entry in self._memory]
            cursor = db.execute(f"SELECT {', '.join(AUDIT_COLUMNS)} FROM audit ORDER BY id")
            return [dict(zip(AUDIT_COLUMNS, values)) for values in cursor]

    def recent(self, limit: int = 20) -> list[dict]:
        return self.rows()[-limit:]

    def reconcile_pending(self) -> list[dict]:
        """Mark rows left 'pending' by an earlier run 'abandoned'; return them."""
        with self._lock:
            db = self._db()
            if db is None:
                left = [entry for entry in self._memory if entry["result"] == PENDING]
                found = [dict(entry) for entry in left]
                for entry in left:
                    entry["result"] = ABANDONED
                return found
            cursor = db.execute(
                f"SELECT {', '.join(AUDIT_COLUMNS)} FROM audit WHERE result = ? ORDER BY id", (PENDING,)
            )
            found = [dict(zip(AUDIT_COLUMNS, values)) for values in cursor]
            if found:
                db.execute("UPDATE audit SET result = ? WHERE result = ?", (ABANDONED, PENDING))
                db.commit()
            return found

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_SPOKEN_CANCEL = frozenset({"cancel", "deny", "stop", "dont send", "do not send"})


def normalise_spoken(text: str) -> str:
    """"Send forty-seven." -> "send 47"; anything else is lower-cased words."""
    from xenon.untrusted import strip_invisible

    words = re.findall(r"[a-z0-9]+", strip_invisible(text or "").lower().replace("-", " ").replace("'", ""))
    out: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in _TENS:
            value = _TENS[word]
            if index + 1 < len(words) and words[index + 1] in _NUMBER_WORDS and 0 < _NUMBER_WORDS[words[index + 1]] < 10:
                value += _NUMBER_WORDS[words[index + 1]]
                index += 1
            out.append(str(value))
        elif word in _NUMBER_WORDS:
            out.append(str(_NUMBER_WORDS[word]))
        else:
            out.append(word)
        index += 1
    return " ".join(out)


@dataclass
class _Slot:
    pending: PendingAction
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = ""
    via: str = ""
    reason: str = ""


class ApprovalBroker:
    """Shows one card per action, blocks the task until a human decides.

    ``presenter(pending)`` is called on the task thread and must hand the
    card to the UI thread itself (``OrbApp.post``); it must not block.
    ``on_settled(action_id, outcome)`` tells the UI to close a card that
    timed out or was cancelled.
    """

    def __init__(
        self,
        presenter: Callable[[PendingAction], None] | None = None,
        key: bytes | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        caps: RateCaps | None = None,
        audit: AuditLog | None = None,
        on_settled: Callable[[str, str], None] | None = None,
    ) -> None:
        self._presenter = presenter
        # Held only in memory, never logged, never in repr.
        self.__key = bytes(key) if key else os.urandom(32)
        self._clock = clock
        self.timeout_s = float(timeout_s)
        # No caps given still means caps: an unknown resolves stricter.
        self.caps = caps if caps is not None else RateCaps()
        self.audit = audit
        self.on_settled = on_settled
        self._lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self._spent: set[str] = set()
        # Why the last few requests ended without a token, by action id, so
        # a task can say "refused: hourly cap" rather than "you declined".
        self._outcomes: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"<ApprovalBroker pending={len(self._slots)}>"

    # -- tokens ------------------------------------------------------------

    def _mac(self, action_id: str, digest: str, expires_at: float, via: str) -> str:
        # \x1f separators: no field can shift its bytes into a neighbour.
        message = f"{action_id}\x1f{digest}\x1f{expires_at!r}\x1f{via}".encode("utf-8")
        return hmac.new(self.__key, message, hashlib.sha256).hexdigest()

    def _mint(self, pending: PendingAction, via: str) -> ApprovalToken:
        expires_at = self._clock() + self.timeout_s
        return ApprovalToken(
            pending.action_id, pending.digest, expires_at, via,
            self._mac(pending.action_id, pending.digest, expires_at, via),
        )

    def consume(self, token: ApprovalToken, live_digest: str) -> bool:
        """One-shot: MAC valid, unused, unexpired and the live digest equal."""
        if not isinstance(token, ApprovalToken):
            return False
        try:
            expected = self._mac(str(token.action_id), str(token.digest), float(token.expires_at), str(token.via))
        except (TypeError, ValueError):
            return False
        if not hmac.compare_digest(expected, str(token.mac)):
            self._log("commit_refused", token, result="refused: approval not valid")
            return False
        with self._lock:
            if token.mac in self._spent:
                spent = True
            else:
                spent = False
                # Spent on any attempt with a genuine MAC, even a failing
                # one: a token that met changed content is never tried again.
                self._spent.add(token.mac)
        if spent:
            self._log("commit_refused", token, result="refused: approval already used")
            return False
        if self._clock() > token.expires_at:
            self._log("commit_refused", token, result="refused: approval expired")
            return False
        if not hmac.compare_digest(str(token.digest), str(live_digest or "")):
            self._log("commit_refused", token, result="refused: changed since you approved")
            return False
        return True

    def _audit(self, event: str, **fields) -> bool:
        """Write one audit row. A failing write (disk full, corrupt db) is
        reported as False and never raises out of the broker."""
        if self.audit is None:
            return True
        try:
            self.audit.record(event, **fields)
            return True
        except Exception:
            return False

    def _log(self, event: str, token: ApprovalToken, **fields) -> None:
        self._audit(event, action_id=token.action_id, digest=token.digest, via=token.via, **fields)

    def _note_outcome(self, action_id: str, reason: str) -> None:
        with self._lock:
            self._outcomes[action_id] = reason
            while len(self._outcomes) > 64:
                self._outcomes.pop(next(iter(self._outcomes)))

    def outcome(self, action_id: str) -> str:
        """Why the request for ``action_id`` gave no token ("" if unknown).
        Read once: the entry is removed."""
        with self._lock:
            return self._outcomes.pop(action_id, "")

    # -- cards -------------------------------------------------------------

    def _refuse(self, pending: PendingAction, reason: str) -> None:
        self._note_outcome(pending.action_id, f"refused: {reason}")
        self._audit("approval_refused", action_id=pending.action_id, task_id=pending.task_id,
                    kind=pending.kind, risk=pending.risk, digest=pending.digest,
                    result=f"refused: {reason}")

    def request(self, pending: PendingAction, cancel: threading.Event | None = None) -> ApprovalToken | None:
        """Show the card and block until approve, deny, timeout or cancel."""
        if cancel is not None and cancel.is_set():
            self._refuse(pending, "task cancelled")
            return None
        if self._presenter is None:
            self._refuse(pending, NO_PRESENTER)
            return None
        if pending.risk is Risk.DENIED:
            self._refuse(pending, "this surface is never automated")
            return None
        gated = pending.risk in (Risk.OUTWARD, Risk.DESTRUCTIVE)
        if gated:
            allowed, why = _outward_switch()
            if not allowed:
                self._refuse(pending, why or "outward actions are switched off")
                return None
        outward = pending.risk is Risk.OUTWARD
        if outward:
            capped = self.caps.check(pending.kind, pending.task_id)
            if capped:
                self._refuse(pending, capped)
                return None
        if pending.risk is Risk.DESTRUCTIVE and pending.voice_ok:
            pending = dataclasses.replace(pending, voice_ok=False)
        if not pending.created_at:
            pending = dataclasses.replace(pending, created_at=self._clock())
        slot = _Slot(pending)
        with self._lock:
            if pending.action_id in self._slots:
                duplicate = True
            else:
                duplicate = False
                self._slots[pending.action_id] = slot
        if duplicate:
            self._refuse(pending, "a card for this action is already up")
            return None  # one card per action id, never two
        if not self._audit("approval_requested", action_id=pending.action_id, kind=pending.kind,
                           risk=pending.risk, digest=pending.digest, task_id=pending.task_id,
                           result=PENDING):
            # No record of the card means no card: fail closed, and do not
            # leave the slot behind (has_task() would stay True until Stop).
            with self._lock:
                self._slots.pop(pending.action_id, None)
            self._note_outcome(pending.action_id, "refused: could not write the audit log")
            return None
        # The clock starts before the card is handed over, so a slow UI
        # thread cannot stretch the window in which a click is accepted.
        deadline = self._clock() + self.timeout_s
        try:
            self._presenter(pending)
        except Exception as error:
            slot.decision, slot.reason = "deny", f"card failed ({type(error).__name__})"
            slot.event.set()
        ended = ""
        while not slot.event.is_set():
            if cancel is not None and cancel.is_set():
                ended = "task cancelled"
                break
            remaining = deadline - self._clock()
            if remaining <= 0:
                ended = "timed out"
                break
            # Short waits only while a card is up, so cancel is noticed fast.
            slot.event.wait(min(_POLL_S, remaining))
        with self._lock:
            # Decide under the lock: once the slot is gone resolve() finds
            # nothing, and a click that lands after a timeout or cancel was
            # seen still counts as a deny (the stricter answer).
            self._slots.pop(pending.action_id, None)
            if ended:
                slot.decision, slot.reason = "deny", ended
            slot.event.set()
        approved = slot.decision == "approve"
        token = self._mint(pending, slot.via) if approved else None
        if approved:
            self._audit("approval_granted", action_id=pending.action_id, task_id=pending.task_id,
                        kind=pending.kind, risk=pending.risk, digest=pending.digest, via=slot.via,
                        result="approved")
        else:
            self._note_outcome(pending.action_id, slot.reason or "denied")
            self._audit("approval_denied", action_id=pending.action_id, task_id=pending.task_id,
                        kind=pending.kind, risk=pending.risk, digest=pending.digest, via=slot.via,
                        result=f"denied: {slot.reason or 'denied'}")
        if approved and outward:
            self.caps.note(pending.kind, pending.task_id)
        if self.on_settled is not None:
            try:
                self.on_settled(pending.action_id, "approved" if approved else (slot.reason or "denied"))
            except Exception:
                pass
        return token

    def resolve(self, action_id: str, decision: str, via: str = "click", spoken: str = "") -> bool:
        """Record a human decision. Voice approval needs voice_ok, the nonce
        and XENON_VOICE_APPROVE=on; anything unexpected changes nothing.
        Denies are always accepted; a second resolve is a no-op."""
        if decision not in {"approve", "deny"} or via not in {"click", "voice"}:
            return False
        if via == "voice" and normalise_spoken(spoken) in _SPOKEN_CANCEL:
            decision = "deny"
        with self._lock:
            slot = self._slots.get(action_id)
            if slot is None or slot.event.is_set():
                return False
            if decision == "deny":
                slot.decision, slot.via, slot.reason = "deny", via, f"denied by {via}"
                slot.event.set()
                return True
            if via == "voice" and not self._voice_ok(slot.pending, spoken):
                return False
            slot.decision, slot.via = "approve", via
            slot.event.set()
            return True

    @staticmethod
    def _voice_ok(pending: PendingAction, spoken: str) -> bool:
        if not _voice_switch():
            return False
        # Never DESTRUCTIVE, never a card with the "not in your request" badge.
        if not pending.voice_ok or pending.risk is not Risk.OUTWARD:
            return False
        nonce = (pending.nonce or "").strip()
        if not nonce or NOT_IN_REQUEST in pending.badges:
            return False
        return hmac.compare_digest(normalise_spoken(spoken), f"send {normalise_spoken(nonce)}")

    def deny_all(self, reason: str = "") -> None:
        with self._lock:
            for slot in self._slots.values():
                if not slot.event.is_set():
                    slot.decision, slot.reason = "deny", reason or "stopped"
                    slot.event.set()

    def pending(self) -> list[PendingAction]:
        with self._lock:
            return [slot.pending for slot in self._slots.values() if not slot.event.is_set()]
