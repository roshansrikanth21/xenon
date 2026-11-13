"""Hands for any desktop app: a thin bridge to Vercel's agent-desktop.

agent-desktop (https://github.com/vercel-labs/agent-desktop, Apache-2.0) is
a CLI that reads and operates apps through the Windows UI Automation tree and
prints one JSON envelope per command. It has no AI of its own: Xenon brings
the model and the loop (see ``computer.py``).

This module:

* finds the native exe (the npm PowerShell shim costs ~1 s per call, the exe
  ~0.1 s) and runs it without a console window or a shell;
* turns a snapshot into a short numbered list for the model. The model only
  sees ``[7] button "Send"``; the real ``@snapshot:eN`` refs stay here, so it
  cannot invent one;
* exposes actions that report agent-desktop's delivery/retry verdict, so a
  caller never repeats an action that may already have happened.

Actions are headless by default: they use accessibility calls, so they do
not move the mouse or steal focus from what the user is doing.

Phase 0 hardening:

* integrity: every path ``find_exe`` resolves must hash to ``PINNED_SHA256``
  (agent-desktop 1.2.0, win32-x64). The ``.sha256`` file npm ships next to the
  exe travels in the same package, so it proves nothing; the pin lives here.
  A mismatch disables the feature instead of falling back to another copy;
* privacy: ``refmap.json`` stores element names and window titles in plain
  text. With ``session`` set every call runs in ``--session <id>`` so its
  refmaps land under ``<state root>/sessions/<id>/``, and ``end_session``
  deletes them. ``sweep_stale_snapshots`` removes leftovers at startup.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_MAJOR_MINOR = ("1.2",)
# sha256 of agent-desktop-win32-x64.exe 1.2.0. Change it only after checking
# the new binary against the GitHub release asset, not the npm .sha256 file.
PINNED_SHA256 = "d274667031322e50b9c2b1ab81c2ca46835d52cc932769a5c34bd4d481fd94c3"
# agent-desktop's own rule for session ids: 1-64 ASCII letters, digits, - or _.
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Xenon only ever names its sessions "xenon-<id>", so that prefix is the
# proof of ownership the cleanup code needs.
SESSION_PREFIX = "xenon-"
# Global snapshot dirs look like "s159dt9rjx0b3v".
_SNAPSHOT_ID = re.compile(r"^s[a-z0-9]{4,40}$")
MAX_ELEMENTS = 70
MAX_NAME = 60
MAX_VALUE = 80


# ----------------------------------------------------------------- finding --


_HASH_CACHE: dict[str, tuple[int, int, str]] = {}


def file_sha256(path: str) -> str:
    """sha256 of ``path``, computed once per (path, mtime, size)."""
    stat = os.stat(path)
    key = os.path.normcase(os.path.abspath(path))
    cached = _HASH_CACHE.get(key)
    if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2]
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    value = digest.hexdigest()
    _HASH_CACHE[key] = (stat.st_mtime_ns, stat.st_size, value)
    return value


def verify_binary(path: str, expected: str | None = None) -> tuple[bool, str]:
    """Is ``path`` the pinned agent-desktop build? (ok, human detail)."""
    expected = expected or PINNED_SHA256
    try:
        actual = file_sha256(path)
    except OSError as error:
        return False, f"cannot read {path}: {error}"
    if actual == expected.lower():
        return True, "sha256 ok"
    return False, f"sha256 mismatch for {path}: got {actual[:16]}..., pinned {expected[:16]}..."


def _candidates() -> list[str]:
    configured = os.environ.get("AGENT_DESKTOP_EXE", "").strip()
    candidates = [configured] if configured else []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates.append(os.path.join(appdata, "npm", "node_modules", "agent-desktop", "bin",
                                       "agent-desktop-win32-x64.exe"))
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "vendor", "agent-desktop-win32-x64.exe"))
    return candidates


def locate_exe() -> tuple[str | None, str | None, str]:
    """(found path, verified path or None, detail) for doctor and find_exe.

    The first existing candidate decides. A copy that fails the hash check
    disables the feature: silently falling back to some other copy would let
    whoever swapped the first one choose what runs next.
    """
    for path in _candidates():
        if path and os.path.isfile(path):
            ok, detail = verify_binary(path)
            return path, (path if ok else None), detail
    shim = shutil.which("agent-desktop")
    if shim:
        # The npm shim is a script, not the pinned exe, so it can never pass.
        ok, detail = verify_binary(shim)
        return shim, (shim if ok else None), detail
    return None, None, "agent-desktop is not installed"


def find_exe() -> str | None:
    """The verified agent-desktop native exe, or None (missing or mismatched)."""
    return locate_exe()[1]


def state_root() -> Path:
    """agent-desktop's state directory (``AGENT_DESKTOP_HOME`` relocates it)."""
    configured = os.environ.get("AGENT_DESKTOP_HOME", "").strip()
    if configured and os.path.isabs(configured):
        return Path(configured)
    return Path.home() / ".agent-desktop"


def new_session_id(task_id: str) -> str:
    """``xenon-<task_id>`` squeezed into agent-desktop's id alphabet."""
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", task_id).strip("-") or f"{int(time.time())}"
    return (SESSION_PREFIX + clean)[:64]


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    checker = getattr(os.path, "isjunction", None)  # 3.12+
    try:
        return bool(checker(path)) if checker else False
    except OSError:
        return True


def _inside(child: Path, parent: Path) -> bool:
    """``child`` is a real directory directly under ``parent`` (no links)."""
    try:
        if _is_link(child) or not child.is_dir():
            return False
        return os.path.normcase(os.path.realpath(child.parent)) == os.path.normcase(
            os.path.realpath(parent)
        )
    except OSError:
        return False


def _remove_tree(path: Path) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def _newest_mtime(path: Path) -> float:
    newest = 0.0
    try:
        newest = path.stat().st_mtime
        for child in path.rglob("*"):
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return newest


def sweep_stale_snapshots(max_age_s: float = 3600, root: str | Path | None = None,
                          now: float | None = None) -> int:
    """Delete leftover refmaps older than ``max_age_s``; returns how many dirs.

    Only two shapes are touched: ``sessions/xenon-*`` (Xenon's by name) and
    global ``snapshots/s<id>`` dirs that hold nothing but ``refmap.json``.
    Refs die with the process that captured them, so a refmap older than an
    hour is useless to anyone and only keeps names and titles on disk.
    """
    base = Path(root) if root is not None else state_root()
    current = time.time() if now is None else now
    removed = 0
    sessions = base / "sessions"
    if sessions.is_dir():
        for entry in list(sessions.iterdir()):
            if not entry.name.startswith(SESSION_PREFIX) or not _SESSION_ID.match(entry.name):
                continue
            if _inside(entry, sessions) and current - _newest_mtime(entry) > max_age_s:
                removed += _remove_tree(entry)
    snapshots = base / "snapshots"
    if snapshots.is_dir():
        for entry in list(snapshots.iterdir()):
            if not _SNAPSHOT_ID.match(entry.name) or not _inside(entry, snapshots):
                continue
            try:
                names = {child.name for child in entry.iterdir()}
            except OSError:
                continue
            if names - {"refmap.json"}:
                continue  # not the shape we know; leave it alone
            if current - _newest_mtime(entry) > max_age_s:
                removed += _remove_tree(entry)
    return removed


def leftover_snapshots(root: str | Path | None = None) -> int:
    """How many refmap dirs exist right now (doctor)."""
    base = Path(root) if root is not None else state_root()
    count = 0
    for folder, pattern in ((base / "snapshots", _SNAPSHOT_ID), (base / "sessions", _SESSION_ID)):
        try:
            count += sum(1 for entry in folder.iterdir() if pattern.match(entry.name))
        except OSError:
            continue
    return count


# --------------------------------------------------------------- envelopes --


@dataclass
class Envelope:
    ok: bool
    command: str
    data: dict = field(default_factory=dict)
    error: dict = field(default_factory=dict)

    @property
    def disposition(self) -> dict:
        source = self.data if self.ok else self.error
        return (source or {}).get("disposition") or {}

    @property
    def delivery(self) -> str:
        """not_delivered, delivery_uncertain, delivered_unverified, delivered_verified, or ''."""
        return self.disposition.get("delivery", "")

    @property
    def safe_to_retry(self) -> bool:
        """Only an action that certainly did not happen may be repeated."""
        return self.disposition.get("retry") == "safe"

    @property
    def code(self) -> str:
        return (self.error or {}).get("code", "")

    @property
    def message(self) -> str:
        return (self.error or {}).get("message", "")


class DesktopUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------- elements --


@dataclass
class Element:
    id: int          # what the model sees
    ref: str         # what agent-desktop needs
    role: str
    name: str = ""
    value: str = ""
    states: tuple = ()
    actions: tuple = ()

    def line(self) -> str:
        text = f'[{self.id}] {self.role}'
        if self.name:
            text += f' "{_clip(self.name, MAX_NAME)}"'
        if self.value:
            if len(self.value) <= MAX_VALUE:
                text += f' value="{_clip(self.value, MAX_VALUE)}"'
            else:
                # A document body is not something to pick a step from, and
                # sending it would waste tokens (and leak more than needed).
                text += f" (contains {len(self.value)} characters of text)"
        notable = [s for s in self.states if s in {"focused", "selected", "checked", "expanded", "disabled"}]
        if notable:
            text += " (" + ", ".join(notable) + ")"
        if self.actions:
            text += " can: " + ",".join(a for a in self.actions if a not in {"ScrollTo"})
        return text


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Snapshot:
    snapshot_id: str
    app: str
    window_title: str
    window_id: str
    elements: list[Element]
    complete: bool = True
    dropped: int = 0

    def by_id(self, element_id: int) -> Element | None:
        for element in self.elements:
            if element.id == element_id:
                return element
        return None

    def render(self) -> str:
        """Compact, numbered view for the model. Names and values are text
        from the screen: callers must present them as untrusted data."""
        head = f'App: {self.app}  Window: "{_clip(self.window_title, 80)}"'
        lines = [head] + [element.line() for element in self.elements]
        if self.dropped:
            lines.append(f"(+{self.dropped} more elements not shown)")
        if not self.complete:
            lines.append("(the app did not finish describing itself; some elements may be missing)")
        return "\n".join(lines)


def flatten(tree: dict, include_offscreen: bool = False, limit: int = MAX_ELEMENTS) -> tuple[list[Element], int]:
    """Actionable elements in reading order, numbered 1..N.

    Off-screen and disabled elements are dropped (unless asked), and elements
    inside an open dialog or menu come first, since that is where the next
    step almost always is.
    """
    found: list[tuple[int, dict]] = []

    def walk(node: dict, in_overlay: bool) -> None:
        if not isinstance(node, dict):
            return
        role = node.get("role", "")
        overlay = in_overlay or role in {"dialog", "alert", "menu", "sheet", "popover"}
        ref = node.get("ref_id")
        states = tuple(node.get("states") or ())
        actions = tuple(node.get("available_actions") or ())
        usable = ref and actions and (include_offscreen or "offscreen" not in states) and "disabled" not in states
        if usable and actions != ("ScrollTo",):
            found.append((0 if overlay else 1, node))
        for child in node.get("children") or ():
            walk(child, overlay)

    walk(tree, False)
    found.sort(key=lambda item: item[0])  # stable: overlay items first, reading order kept
    elements = []
    for index, (_, node) in enumerate(found[:limit], start=1):
        elements.append(Element(
            id=index,
            ref=node["ref_id"],
            role=node.get("role", ""),
            name=str(node.get("name") or ""),
            value=str(node.get("value") or ""),
            states=tuple(node.get("states") or ()),
            actions=tuple(node.get("available_actions") or ()),
        ))
    return elements, max(0, len(found) - limit)


# ------------------------------------------------------------------ bridge --


class AgentDesktop:
    def __init__(self, exe: str | None = None, runner=None, timeout_s: float = 40.0,
                 session: str | None = None, state_dir: str | Path | None = None) -> None:
        # An explicit ``exe`` is a code-supplied seam (tests); anything found
        # on disk goes through find_exe and its hash check.
        self.exe = exe or find_exe()
        self._runner = runner or self._run_process
        self._timeout_s = timeout_s
        if session is not None and not _SESSION_ID.match(session):
            raise ValueError("session ids are 1-64 letters, digits, - or _")
        self.session = session
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self._snapshot_ids: set[str] = set()

    @property
    def available(self) -> bool:
        return bool(self.exe)

    def _run_process(self, argv: list[str], stdin: str | None, timeout: float) -> tuple[int, str, str]:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, creationflags=flags,
        )
        return process.returncode, process.stdout, process.stderr

    def run(self, *args: str, stdin: str | None = None, timeout: float | None = None) -> Envelope:
        if not self.exe:
            raise DesktopUnavailable(
                "agent-desktop is not installed (npm install -g agent-desktop), "
                "or set AGENT_DESKTOP_EXE"
            )
        argv = [self.exe, *[str(a) for a in args]]
        if self.session and args:
            # Right after the subcommand, so it can never be read as a value.
            argv[2:2] = ["--session", self.session]
        try:
            code, out, err = self._runner(argv, stdin, timeout or self._timeout_s)
        except subprocess.TimeoutExpired:
            return Envelope(False, args[0] if args else "", error={
                "code": "TIMEOUT", "message": "agent-desktop did not answer in time",
                "disposition": {"delivery": "delivery_uncertain", "retry": "unknown"}})
        try:
            raw = json.loads(out.strip().splitlines()[-1] if out.strip() else "{}")
        except ValueError:
            raw = {}
        if not raw:
            return Envelope(False, args[0] if args else "", error={
                "code": "NO_OUTPUT", "message": (err or out or f"exit {code}").strip()[:300],
                "disposition": {"delivery": "delivery_uncertain", "retry": "unknown"}})
        envelope = Envelope(bool(raw.get("ok")), raw.get("command", ""), raw.get("data") or {},
                            raw.get("error") or {})
        data = envelope.data if isinstance(envelope.data, dict) else {}
        snapshot_id = data.get("snapshot_id")
        if isinstance(snapshot_id, str) and _SNAPSHOT_ID.match(snapshot_id):
            self._snapshot_ids.add(snapshot_id)  # for end_session
        return envelope

    # ----- session hygiene -----------------------------------------------

    def end_session(self) -> int:
        """Seal the session and delete every refmap this instance created.

        Returns how many directories were removed. Only paths that are
        provably ours are touched: the ``xenon-*`` session dir and global
        snapshot dirs whose ids came back in this instance's envelopes.
        """
        root = self._state_dir if self._state_dir is not None else state_root()
        removed = 0
        if self.session and self.exe:
            try:
                self.run("session", "end", self.session, timeout=10)
            except Exception:
                pass  # a bare --session has no manifest to seal; deletion still runs
        if self.session and self.session.startswith(SESSION_PREFIX):
            folder = root / "sessions" / self.session
            if _inside(folder, root / "sessions"):
                removed += _remove_tree(folder)
        snapshots = root / "snapshots"
        for snapshot_id in sorted(self._snapshot_ids):
            folder = snapshots / snapshot_id
            if _inside(folder, snapshots):
                removed += _remove_tree(folder)
        self._snapshot_ids.clear()
        return removed

    # ----- observation ---------------------------------------------------

    def version(self) -> str:
        envelope = self.run("version", timeout=10)
        return (envelope.data or {}).get("version", "") if envelope.ok else ""

    def healthy(self) -> tuple[bool, str]:
        """Installed, a supported version, and allowed to use accessibility."""
        if not self.exe:
            return False, "agent-desktop is not installed"
        version = self.version()
        if not version.startswith(SUPPORTED_MAJOR_MINOR):
            return False, f"agent-desktop {version or '?'} is not a supported version (want 1.2.x)"
        status = self.run("status", timeout=10)
        state = (((status.data or {}).get("permissions") or {}).get("accessibility") or {}).get("state")
        if state != "granted":
            return False, f"accessibility permission is {state or 'unknown'}"
        return True, f"agent-desktop {version}"

    def snapshot(self, app: str | None = None, window_id: str | None = None, root: str | None = None,
                 interactive: bool = True, timeout_ms: int = 8000, include_offscreen: bool = False,
                 limit: int = MAX_ELEMENTS) -> Snapshot:
        if not (app or window_id):
            # Without a target agent-desktop reads whatever is in front, which
            # may be a different app than the one the task is about.
            raise ValueError("snapshot needs an app or a window id")
        args = ["snapshot", "--compact", "--timeout-ms", str(timeout_ms)]
        if interactive:
            args.append("-i")
        if app:
            args += ["--app", app]
        if window_id:
            args += ["--window-id", window_id]
        if root:
            args += ["--root", root]
        envelope = self.run(*args, timeout=timeout_ms / 1000 + 15)
        if not envelope.ok:
            raise DesktopUnavailable(f"could not read the app ({envelope.code}): {envelope.message}")
        data = envelope.data
        elements, dropped = flatten(data.get("tree") or {}, include_offscreen, limit)
        window = data.get("window") or {}
        return Snapshot(
            snapshot_id=data.get("snapshot_id", ""),
            app=str(data.get("app") or app or ""),
            window_title=str(window.get("title") or ""),
            window_id=str(window.get("id") or ""),
            elements=elements,
            complete=bool(data.get("complete", True)),
            dropped=dropped,
        )

    def list_windows(self, app: str | None = None) -> list[dict]:
        envelope = self.run("list-windows", *(["--app", app] if app else []), timeout=15)
        data = envelope.data or {}
        windows = data.get("windows") if isinstance(data, dict) else data
        return list(windows or []) if envelope.ok else []

    def find_window(self, title: str) -> dict | None:
        """A visible window whose title contains ``title`` (case-insensitive).

        Store apps all live in ApplicationFrameHost.exe, so they are found by
        their window, not their process.
        """
        wanted = title.lower().strip()
        best = None
        for window in self.list_windows():
            current = str(window.get("title") or "").lower()
            if not current or wanted not in current:
                continue
            if current == wanted:
                return window
            if best is None or len(current) < len(str(best.get("title") or "")):
                best = window
        return best

    # ----- actions -------------------------------------------------------

    def click(self, ref: str) -> Envelope:
        return self.run("click", ref)

    def set_value(self, ref: str, text: str) -> Envelope:
        # --stdin keeps the text out of the process command line.
        return self.run("set-value", ref, "--stdin", stdin=text)

    def select(self, ref: str, option: str) -> Envelope:
        return self.run("select", ref, option)

    def toggle(self, ref: str) -> Envelope:
        return self.run("toggle", ref)

    def expand(self, ref: str) -> Envelope:
        return self.run("expand", ref)

    def scroll(self, ref: str, direction: str = "down", amount: int = 3) -> Envelope:
        return self.run("scroll", ref, "--direction", direction, "--amount", str(amount))

    def press(self, keys: str, app: str | None = None) -> Envelope:
        return self.run("press", keys, *(["--app", app] if app else []))

    def launch(self, target: str, wait: bool = True) -> Envelope:
        return self.run("launch", target, *(["--activate"] if wait else []), timeout=60)

    def focus_window(self, app: str) -> Envelope:
        return self.run("focus-window", "--app", app, timeout=20)

    def wait_for(self, app: str, selector: str, timeout_ms: int = 10000) -> Envelope:
        return self.run("wait", "--app", app, "--wait-for", selector, "--timeout", str(timeout_ms),
                        timeout=timeout_ms / 1000 + 10)
