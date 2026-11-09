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
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

SUPPORTED_MAJOR_MINOR = ("1.2",)
MAX_ELEMENTS = 70
MAX_NAME = 60
MAX_VALUE = 80


# ----------------------------------------------------------------- finding --


def find_exe() -> str | None:
    """The agent-desktop native exe, or None when it is not installed."""
    configured = os.environ.get("AGENT_DESKTOP_EXE", "").strip()
    candidates = [configured] if configured else []
    appdata = os.environ.get("APPDATA", "")
    candidates.append(os.path.join(appdata, "npm", "node_modules", "agent-desktop", "bin",
                                   "agent-desktop-win32-x64.exe"))
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "vendor", "agent-desktop-win32-x64.exe"))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return shutil.which("agent-desktop")


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
    def __init__(self, exe: str | None = None, runner=None, timeout_s: float = 40.0) -> None:
        self.exe = exe or find_exe()
        self._runner = runner or self._run_process
        self._timeout_s = timeout_s

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
        return Envelope(bool(raw.get("ok")), raw.get("command", ""), raw.get("data") or {}, raw.get("error") or {})

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
