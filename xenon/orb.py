"""The minimized Xenon orb: a floating, always-on-top circle.

Hover it and six options fan out from the side that faces the middle of the
screen: Ask, Voice, Screen, Search, Expand and Close. Drag it anywhere; it
remembers where it was.

The orb is a per-pixel-alpha layered window (``UpdateLayeredWindow``), so its
glow and antialiased edges blend with whatever is behind it, and transparent
pixels are click-through. It never takes focus, so the window you were using
stays the one Xenon reads when you ask about the screen.

Everything that touches the model, memory or the microphone runs off the Tk
thread; results come back through ``post``.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import re
import sys
import zlib
import threading
import time
import tkinter as tk
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Protocol

from xenon.orb_model import (
    NOTCH_DROP,
    NOTCH_IDLE_S,
    CENTER,
    ORB_RADIUS,
    SATELLITE_DISTANCE,
    SATELLITE_RADIUS,
    WINDOW,
    OrbModel,
    fan_direction,
)
from xenon.orb_render import OrbRenderer, premultiplied_bgra

# ----------------------------------------------------------------- Win32 --

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND = 2

# The "got it" flash stays on screen at least this long.
HEARD_HOLD_S = 0.45

PANEL_BG = "#0d111c"
PANEL_FG = "#e6eaf5"
PANEL_DIM = "#8b93a7"
ENTRY_BG = "#151a28"


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _enable_dpi_awareness() -> None:
    _declare_win32()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _dpi_scale(hwnd: int) -> float:
    try:
        return ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0 or 1.0
    except Exception:
        return 1.0


def _toplevel_hwnd(widget: tk.Misc) -> int:
    widget.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
    return hwnd or widget.winfo_id()


def _round_corners(widget: tk.Misc, border: str | None = None) -> None:
    """Win11 rounded corners and an accent border on a borderless Toplevel."""
    try:
        hwnd = _toplevel_hwnd(widget)
        preference = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(preference), 4
        )
        if border:
            r, g, b = (int(border[i : i + 2], 16) for i in (1, 3, 5))
            colorref = ctypes.c_uint(r | (g << 8) | (b << 16))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_BORDER_COLOR, ctypes.byref(colorref), 4
            )
    except Exception:
        pass


def _declare_win32() -> None:
    """64-bit handles overflow ctypes' default int, so declare every call."""
    gdi, user = ctypes.windll.gdi32, ctypes.windll.user32
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    gdi.CreateDIBSection.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi.SelectObject.restype = wintypes.HGDIOBJ
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    user.GetDC.argtypes = [wintypes.HWND]
    user.GetDC.restype = wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user.GetParent.argtypes = [wintypes.HWND]
    user.GetParent.restype = wintypes.HWND
    user.GetForegroundWindow.restype = wintypes.HWND
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongW.restype = wintypes.LONG
    user.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user.SetWindowLongW.restype = wintypes.LONG
    user.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.HDC, ctypes.c_void_p, wintypes.COLORREF, ctypes.c_void_p,
        wintypes.DWORD,
    ]
    user.UpdateLayeredWindow.restype = wintypes.BOOL


class LayeredWindow:
    """Pushes premultiplied BGRA frames to a layered window."""

    def __init__(self, hwnd: int) -> None:
        _declare_win32()
        self.hwnd = hwnd
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd,
            GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
        )
        self._gdi = ctypes.windll.gdi32
        self._user32 = user32
        self._screen_dc = user32.GetDC(None)
        self._mem_dc = self._gdi.CreateCompatibleDC(self._screen_dc)
        self._bitmap = None
        self._bits = ctypes.c_void_p()
        self._size = (0, 0)
        self._old = None

    def _ensure_bitmap(self, width: int, height: int) -> None:
        if self._size == (width, height) and self._bitmap:
            return
        if self._bitmap:
            self._gdi.SelectObject(self._mem_dc, self._old)
            self._gdi.DeleteObject(self._bitmap)
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        self._gdi.CreateDIBSection.restype = wintypes.HBITMAP
        self._bitmap = self._gdi.CreateDIBSection(
            self._mem_dc, ctypes.byref(header), DIB_RGB_COLORS,
            ctypes.byref(self._bits), None, 0,
        )
        self._old = self._gdi.SelectObject(self._mem_dc, self._bitmap)
        self._size = (width, height)

    def update(self, data: bytes, width: int, height: int, x: int, y: int) -> None:
        self._ensure_bitmap(width, height)
        ctypes.memmove(self._bits, data, len(data))
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        point = wintypes.POINT(x, y)
        size = wintypes.SIZE(width, height)
        source = wintypes.POINT(0, 0)
        self._user32.UpdateLayeredWindow(
            self.hwnd, self._screen_dc, ctypes.byref(point), ctypes.byref(size),
            self._mem_dc, ctypes.byref(source), 0, ctypes.byref(blend), ULW_ALPHA,
        )

    def close(self) -> None:
        try:
            if self._bitmap:
                self._gdi.SelectObject(self._mem_dc, self._old)
                self._gdi.DeleteObject(self._bitmap)
            self._gdi.DeleteDC(self._mem_dc)
            self._user32.ReleaseDC(None, self._screen_dc)
        except Exception:
            pass


# --------------------------------------------------------------- backend --


@dataclass
class OrbReply:
    text: str
    sources: list[str] = field(default_factory=list)
    note: str = ""
    # Built from the screen, memory or a tainted task: the companion tablet
    # shows "Private" instead of it.
    private: bool = False


@dataclass
class OrbLink:
    title: str
    url: str
    snippet: str = ""


class OrbBackend(Protocol):
    def ask(self, text: str) -> OrbReply: ...
    def screen(self) -> OrbReply: ...
    def search(self, query: str) -> list[OrbLink]: ...
    def close(self) -> None: ...


@dataclass
class VoiceHooks:
    state: Callable[[str, str], None]
    level: Callable[[float], None]
    exchange: Callable[..., None]  # (you, answer[, private])
    # Set by the push-to-talk hotkey: listen now, without the wake word.
    listen_now: threading.Event | None = None
    # Task hand-off: ``task(text, origin)`` returns what to say ("On it.",
    # "Stopped.") or None to fall through to today's behaviour. The origin
    # "voice-focus" marks a wake-less utterance, which may only answer a
    # waiting task (or stop it), never start one or approve anything.
    task: Callable[[str, str], str | None] | None = None
    # True while a task waits for an answer (one wake-less listen allowed).
    focus_listen: Callable[[], bool] | None = None
    # ("say" | "ask", text) for the voice thread to speak: task results and
    # a waiting task's question.
    say: "queue.Queue[tuple[str, str]] | None" = None
    # Voice approvals (spoken nonce) stay off in phase 1: always None.
    confirm: Callable[[str, str], bool] | None = None


# Builds a voice session on its own thread and runs until ``stop`` is set.
VoiceRunner = Callable[[VoiceHooks, threading.Event], None]


class Worker:
    """One thread for model + memory work, so SQLite and COM stay on it."""

    def __init__(self, factory: Callable[[], OrbBackend]) -> None:
        self._factory = factory
        self._jobs: "queue.Queue[tuple[Callable[[OrbBackend], None], Callable[[str], None]] | None]" = queue.Queue()
        self._backend: OrbBackend | None = None
        self.error = ""
        self._thread = threading.Thread(target=self._run, daemon=True, name="xenon-orb-worker")
        self._thread.start()

    def submit(self, job: Callable[[OrbBackend], None], fail: Callable[[str], None]) -> None:
        """Run ``job`` on the worker; ``fail`` gets the message if it cannot."""
        self._jobs.put((job, fail))

    def _run(self) -> None:
        # The backend (model client, screen reader, memory) is built on the
        # first request, not at startup: an orb that just sits in the notch
        # stays small in memory.
        started = False
        while True:
            item = self._jobs.get()
            if item is None:
                break
            job, fail = item
            if not started:
                started = True
                try:
                    self._backend = self._factory()
                except Exception as error:
                    self.error = f"Xenon could not start: {error}"
            if self._backend is None:
                fail(self.error or "Xenon could not start")
                continue
            try:
                job(self._backend)
            except Exception as error:
                fail(str(error))
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception:
                pass

    def stop(self, timeout: float = 3.0) -> None:
        self._jobs.put(None)
        self._thread.join(timeout=timeout)


# ------------------------------------------------------------------ tasks --


def _never(_text: str) -> bool:
    return False


@dataclass
class TaskServices:
    """What the orb needs from ``__main__`` to run "do <goal>" tasks.

    ``make_task(goal, origin, tools)`` returns the task body, a callable
    that takes a ``tasks.TaskContext`` and returns the result text.
    ``is_t0(text)`` says whether an instant parser owns the text (volume,
    timers), which escapes a waiting task's focus. ``clean`` drops the wake
    word.
    """

    make_task: Callable[[str, str, "TaskTools"], Callable[[object], str]]
    is_t0: Callable[[str], bool] = _never
    clean: Callable[[str], str] = str.strip


@dataclass
class TaskTools:
    """Handed to a task body: the one broker, the caps, and the undo strip."""

    broker: object
    caps: object
    # undo(label, seconds, cancel) -> True to go ahead, False when undone.
    undo: Callable[[str, float, threading.Event], bool]
    task_id: str = ""


@dataclass
class ChoicePrompt:
    prompt_id: str
    title: str
    labels: list[str]
    on_pick: Callable[[str | None], None]


# A spoken request that may not start a task: heard in always-listening
# mode, or with the wake word somewhere other than the start.
VOICE_AMBIENT = "voice-ambient"
VOICE_TASK_REFUSAL = "Use push-to-talk, or start with my name, for tasks."
BUSY_CHOICES = {"Queue it": "queue", "Replace the current task": "replace", "Cancel the new one": "cancel"}
TERMINAL_TASK_STATES = frozenset({"done", "failed", "cancelled"})


class OrbTasks:
    """The orb's task layer, without Tk: routing, broker, TaskManager, IPC.

    Everything that touches a window goes through ``ui`` (the OrbApp) via
    ``post``, so this class is safe to call from the Tk thread, the voice
    thread and the IPC thread, and the tests drive it with fakes.

    Idle cost: the broker and the rate caps are plain objects; the audit
    database opens on first write; the TaskManager thread starts on the
    first task. Only the IPC thread exists from startup, asleep in
    ConnectNamedPipe.
    """

    def __init__(
        self,
        services: TaskServices,
        post: Callable[[Callable[[], None]], None],
        ui,
        broker=None,
        audit=None,
        manager_factory: Callable[..., object] | None = None,
        ipc_factory: Callable[..., object] | None = None,
        agent_check: Callable[[], tuple[bool, str]] | None = None,
    ) -> None:
        from xenon.approvals import ApprovalBroker, AuditLog, RateCaps, default_audit_path

        self.services = services
        self._post = post
        self._ui = ui
        self.audit = audit if audit is not None else AuditLog(default_audit_path())
        self.caps = RateCaps()
        self.broker = broker if broker is not None else ApprovalBroker(
            presenter=self._present, caps=self.caps, audit=self.audit, on_settled=self._settled
        )
        self._manager_factory = manager_factory
        self._ipc_factory = ipc_factory
        self._agent_check = agent_check
        self._manager = None
        self._ipc = None
        self._origins: dict[str, str] = {}
        # Title of the task most recently queued or started: the busy card
        # names it when the manager has queued it but not picked it up yet.
        self._latest_title = ""
        # Who is submitting right now, per thread: TaskManager.submit calls
        # on_busy_choice synchronously, and the Tk, voice and IPC threads
        # can all submit at once.
        self._submitting = threading.local()
        self._choices: list[ChoicePrompt] = []
        self._lock = threading.Lock()
        self._reconciled = False

    # -- lazy parts --------------------------------------------------------

    @property
    def manager(self):
        with self._lock:
            if self._manager is None:
                if self._manager_factory is not None:
                    factory = self._manager_factory
                else:
                    from xenon.tasks import TaskManager as factory
                self._manager = factory(on_update=self._update, on_busy_choice=self._busy_choice)
                reconcile = not self._reconciled
                self._reconciled = True
            else:
                reconcile = False
        if reconcile:
            try:
                left = self.audit.reconcile_pending()
                if left:
                    print(f"audit: {len(left)} approval(s) left open by an earlier run were closed",
                          file=sys.stderr, flush=True)
            except Exception:
                pass
        return self._manager

    def start_ipc(self) -> bool:
        """Start the pipe so ``xenon do`` can hand outward goals to the orb."""
        if self._ipc is not None:
            return True
        try:
            if self._ipc_factory is not None:
                server = self._ipc_factory(submit=self.submit_from_ipc)
            else:
                from xenon.ipc import IpcServer

                server = IpcServer(submit=self.submit_from_ipc)
            started = server.start()
        except Exception as error:
            print(f"task hand-off unavailable: {error}", file=sys.stderr, flush=True)
            return False
        self._ipc = server
        return bool(started)

    # -- state -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        manager = self._manager
        return manager is not None and manager.busy

    @property
    def waiting_for_input(self) -> bool:
        manager = self._manager
        return manager is not None and manager.waiting_for_input

    def has_task(self) -> bool:
        return self.busy or bool(self.broker.pending()) or bool(self._choices)

    def open_choices(self) -> list[ChoicePrompt]:
        with self._lock:
            return list(self._choices)

    # -- the request path --------------------------------------------------

    def route(self, text: str, origin: str = "typed") -> str | None:
        """Steps 2-4 of the request path for typed and spoken text.

        Returns what to tell Roshan, or None to fall through to today's
        behaviour (instant parsers, router, Assistant.ask). A wake-less
        utterance (origin "voice-focus") can only answer a waiting task or
        stop one; it never starts a task and never falls through.
        """
        from xenon import intents

        cleaned = self.services.clean(text or "").strip()
        focus_only = origin == "voice-focus"
        if not cleaned:
            return "" if focus_only else None
        intent = intents.parse(cleaned)
        prompts = self.open_choices()
        if prompts:
            # A choice card holds the focus: "the second one", "queue",
            # "none", or "cancel"/"never mind" (which drops just the new task).
            from xenon.orb_cards import pick_choice
            from xenon.tasks import match_choice

            prompt = prompts[-1]
            understood, label = pick_choice(cleaned, prompt.labels)
            if not understood and intent is None:
                label = match_choice(cleaned, prompt.labels)
                understood = label is not None
            if not understood and intent is not None and intent.kind == "cancel":
                understood, label = True, None
            if understood:
                self._resolve_choice(prompt.prompt_id, label)
                return "OK."
        if intent is not None and intent.kind in {"stop", "cancel"} and self.has_task():
            self.stop("stopped by you")
            return "Stopped."
        manager = self._manager
        if (
            manager is not None
            and manager.waiting_for_input
            and not intents.is_escape(cleaned)
            and not self.services.is_t0(cleaned)
        ):
            if manager.offer_input(cleaned):
                return "Got it."
        if focus_only:
            return ""  # never a new task, never the assistant
        if intent is not None and intent.kind == "do" and origin == VOICE_AMBIENT:
            # Always-listening mode, or the wake word was not at the start:
            # this may be a video or someone else in the room.
            return VOICE_TASK_REFUSAL
        if intent is not None and intent.kind == "do":
            check = self._agent_check
            if check is None:
                from xenon.killswitch import agent_allowed as check
            allowed, why = check()
            if not allowed:
                return f"I can't do that: {why}."
            was_busy = self.busy
            self.submit(intent.goal, origin)
            if was_busy:
                # The queue / replace / cancel card asks what to do with it
                # (and reads the question out for a spoken request).
                return "" if origin == "voice" else "Still on another task: choose on the card."
            return "On it."
        return None

    def submit(self, goal: str, origin: str = "typed") -> str:
        services = self.services
        broker, caps, undo = self.broker, self.caps, self._undo

        def body(context) -> str:
            task_id = getattr(context, "task_id", "")
            # Recorded before the first progress update, so a voice task's
            # questions and result are spoken however fast it runs.
            self._origins[task_id] = origin
            tools = TaskTools(broker, caps, undo, task_id)
            return services.make_task(goal, origin, tools)(context)

        # A second task while one runs raises the queue/replace/cancel card
        # from inside manager.submit; it reads this to know who asked.
        self._submitting.origin = origin
        try:
            return self.manager.submit(goal, body, source=origin)
        finally:
            self._submitting.origin = "typed"

    def origin_of(self, task_id: str) -> str:
        """Who asked for a running task: typed, voice or ipc."""
        return self._origins.get(task_id, "typed")

    def submit_from_ipc(self, goal: str, origin: str = "ipc") -> str:
        # Whatever the caller claimed, a pipe request is "ipc": it can start
        # a task (and so raise a card), never approve one.
        return self.submit(goal, "ipc")

    def stop(self, reason: str = "") -> bool:
        """Cancel the task, deny its cards, drop choice prompts."""
        manager = self._manager
        stopped = bool(manager.cancel(None)) if manager is not None else False
        pending = bool(self.broker.pending())
        self.broker.deny_all(reason or "stopped")
        with self._lock:
            prompts, self._choices = self._choices, []
        for prompt in prompts:
            self._post(lambda p=prompt: self._ui.task_choice_closed(p.prompt_id))
            try:
                prompt.on_pick(None)
            except Exception:
                pass
        self._post(self._ui.task_stopped)
        return stopped or pending or bool(prompts)

    def close(self) -> None:
        self.stop("Xenon closed")
        if self._manager is not None:
            try:
                self._manager.shutdown()
            except Exception:
                pass
        if self._ipc is not None:
            try:
                self._ipc.stop()
            except Exception:
                pass

    # -- callbacks from other threads --------------------------------------

    def _present(self, pending) -> None:
        """Broker presenter (task thread): show the card on the Tk thread."""
        broker = self.broker

        def decide(decision: str) -> None:
            broker.resolve(pending.action_id, decision, via="click")

        self._post(lambda: self._ui.task_card(pending, decide))

    def _settled(self, action_id: str, outcome: str) -> None:
        self._post(lambda: self._ui.task_card_closed(action_id))

    def _update(self, update) -> None:
        origin = self._origins.get(update.task_id, "typed")
        if update.state in {"queued", "running"} and update.title:
            self._latest_title = update.title
        if update.state in TERMINAL_TASK_STATES:
            self._origins.pop(update.task_id, None)
        self._post(lambda: self._ui.task_update(update, origin))

    def _busy_choice(self, title: str, decide: Callable[[str], None]) -> None:
        import uuid

        current = ""
        manager = self._manager
        if manager is not None:
            current = manager.current_title
        current = current or self._latest_title
        prompt_id = uuid.uuid4().hex[:12]
        origin = getattr(self._submitting, "origin", "typed")
        with self._lock:
            already_open = bool(self._choices)
        if already_open:
            # One queue/replace/cancel card at a time: further submits while
            # it is up (a looping IPC caller) are cancelled, never stacked.
            decide("cancel")
            return

        def on_pick(label: str | None) -> None:
            decide(BUSY_CHOICES.get(label or "", "cancel"))

        working_on = f"“{shorten(current, 40)}”" if current else "another task"
        prompt = ChoicePrompt(
            prompt_id,
            f"Still working on {working_on}. Start “{shorten(title, 40)}”?",
            list(BUSY_CHOICES),
            on_pick,
        )
        with self._lock:
            self._choices.append(prompt)
        self._post(lambda: self._ui.task_choice(
            prompt.prompt_id, prompt.title, prompt.labels,
            lambda label: self._resolve_choice(prompt.prompt_id, label),
            origin,
        ))

    def _resolve_choice(self, prompt_id: str, label: str | None) -> None:
        with self._lock:
            prompt = next((p for p in self._choices if p.prompt_id == prompt_id), None)
            if prompt is None:
                return  # already answered (click and voice raced)
            self._choices.remove(prompt)
        self._post(lambda: self._ui.task_choice_closed(prompt_id))
        prompt.on_pick(label)

    def _undo(self, label: str, seconds: float, cancel: threading.Event) -> bool:
        """Task thread: show the undo strip and wait. True means go ahead."""
        if cancel.is_set():
            return False
        if seconds <= 0:
            return True
        verdict: dict[str, bool] = {}
        done = threading.Event()

        def finish(go: bool) -> None:
            verdict.setdefault("go", go)
            done.set()

        self._post(lambda: self._ui.task_undo(label, seconds, lambda: finish(False), lambda: finish(True)))
        deadline = time.monotonic() + seconds + 3.0
        while not done.wait(0.1):  # only during a commit, a few seconds
            if cancel.is_set() or time.monotonic() > deadline:
                finish(False)  # stopped, or the strip never answered: do not send
        return verdict.get("go", False) and not cancel.is_set()


# What the orb shows for a task update. Pure, so the tests need no Tk.

TASK_ACCENTS = {"done": "#34d399", "failed": "#f87171", "cancelled": "#fbbf24", "waiting": "#fbbf24"}
# States a task's progress may replace. Anything else (thinking about a
# quick ask, listening, speaking) is left alone: quick asks keep working.
TASK_OVERRIDABLE_STATES = frozenset({"idle", "standby", "working", "error"})
# Cards opened while another is up sit side by side, never on top of its
# buttons.
CARD_STACK_PX = 410
_RESULT_NOTE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$", re.DOTALL)


@dataclass
class TaskDisplay:
    notch: str                        # the notch progress line; "" clears it
    state: str                        # "working", "rest", or "" (leave it)
    title: str = ""                   # AnswerPanel header; "" means no panel
    body: str = ""
    note: str = ""
    accent: str = "#c4b5fd"
    speak: tuple[str, str] | None = None  # ("say" | "ask", text) for a voice task
    history: bool = False             # add (title, body) to the chat history
    placeholder: str = ""             # the Ask pill's hint while it waits


def split_result(text: str) -> tuple[str, str]:
    """"Done. (2 steps · 3.1s)" -> ("Done.", "2 steps · 3.1s")."""
    match = _RESULT_NOTE.match((text or "").strip())
    if not match:
        return (text or "").strip(), ""
    return match.group(1).strip(), match.group(2).strip()


def task_display(update, origin: str = "typed") -> TaskDisplay:
    """Map a ``tasks.TaskUpdate`` onto the notch, the panel and the voice.

    Progress is never spoken; only a voice task's question and its final
    result are. Results go to the AnswerPanel like any other answer.
    """
    from xenon.orb_model import format_task_progress, step_fraction

    state = getattr(update, "state", "")
    title = shorten(getattr(update, "title", "") or "task", 48)
    text = (getattr(update, "text", "") or "").strip()
    voice = origin == "voice"
    if state == "queued":
        return TaskDisplay(f"Queued · {title}", "working")
    if state == "running":
        if step_fraction(text) is not None:
            line = text  # already "Step 2/5 · Type text": never "Step 2/5 · Step 2/5 · …"
        else:
            line = format_task_progress(text or title, getattr(update, "step", 0) or None,
                                        getattr(update, "total", 0) or None)
        return TaskDisplay(line, "working")
    if state == "waiting":
        question = text or "What should I do?"
        how = "Answer with Ctrl+Alt+X" + (", or just say it." if voice else ".")
        return TaskDisplay(
            "Waiting for your answer", "working", "Xenon needs an answer", question, how,
            TASK_ACCENTS["waiting"], ("ask", question) if voice else None, placeholder="your answer",
        )
    if state in ("done", "failed"):
        body, note = split_result(text or ("Done." if state == "done" else "That didn't work."))
        spoken = body if voice else ""
        return TaskDisplay(
            "", "rest", title if state == "done" else f"Didn't finish · {title}", body, note,
            TASK_ACCENTS[state], ("say", spoken) if spoken else None, history=True,
        )
    if state == "cancelled":
        if text in {"not started", "stopped before it started"}:
            return TaskDisplay("", "rest")  # declined on the queue card; nothing ran
        body, note = split_result(text or "Stopped.")
        if body.lower() in {"stopped", "stopped by you"} or body.endswith("was stopped"):
            body = "Stopped."
        return TaskDisplay("", "rest", f"Stopped · {title}", body, note, TASK_ACCENTS["cancelled"],
                           history=True)
    return TaskDisplay("", "")


def card_anchor(
    orb: tuple[int, int, int],
    scale: float,
    notch: bool,
    orb_area: tuple[int, int, int, int] | None,
    target_area: tuple[int, int, int, int] | None,
    index: int = 0,
) -> tuple[int, int]:
    """Top-centre point for a card: under the unfolded notch when the target
    app shares the orb's monitor, else near the top of the target's monitor
    (where he is looking). ``orb`` is (x, y, size) of the orb window; areas
    are monitor work areas (left, top, right, bottom)."""
    x, y, size = orb
    same_monitor = target_area is None or target_area == orb_area
    if notch and same_monitor:
        point = (x + size // 2, y + size + int(4 * scale))
    else:
        area = target_area or orb_area
        if area is None:
            point = (x + size // 2, y + size)
        else:
            point = ((area[0] + area[2]) // 2, area[1] + int(56 * scale))
    return point[0] + index * int(CARD_STACK_PX), point[1]


def audit_lines(rows: list[dict], limit: int = 20) -> list[str]:
    """The tray's "Recent actions": newest first, kinds, risks, results and
    short digests. The audit log never holds bodies, and neither does this."""
    lines = []
    for row in list(rows)[-limit:][::-1]:
        try:
            stamp = time.strftime("%H:%M:%S", time.localtime(float(row.get("ts") or 0)))
        except (TypeError, ValueError, OverflowError):
            stamp = "--:--:--"
        parts = [stamp, str(row.get("event") or "")]
        for key in ("kind", "risk", "via", "result"):
            value = str(row.get(key) or "").strip()
            if value:
                parts.append(value)
        digest = str(row.get("digest") or "").strip()
        if digest:
            parts.append(f"#{digest[:8]}")
        lines.append("  ".join(parts))
    return lines


def _work_area_for(hwnd: int) -> tuple[int, int, int, int] | None:
    """The work area of the monitor that holds ``hwnd`` (nearest one)."""
    if not hwnd:
        return None
    try:
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.restype = wintypes.HMONITOR
        monitor = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.rcWork
            return work.left, work.top, work.right, work.bottom
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- hotkeys --

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
# Ctrl+Alt+X summons Ask, Ctrl+Alt+V toggles voice.
VK_PRIOR = 0x21  # Page Up
VK_NEXT = 0x22   # Page Down
DEFAULT_HOTKEYS: dict[str, tuple[int, int]] = {
    "ask": (MOD_CONTROL | MOD_ALT, ord("X")),
    "voice": (MOD_CONTROL | MOD_ALT, ord("V")),
    # Ctrl+Alt+B drops every screen to minimum; again restores.
    "dim": (MOD_CONTROL | MOD_ALT, ord("B")),
    # Ctrl+Alt+Space: listen now (no wake word), music turned down.
    "listen": (MOD_CONTROL | MOD_ALT, 0x20),
    "brighter": (MOD_CONTROL | MOD_ALT, VK_PRIOR),
    "darker": (MOD_CONTROL | MOD_ALT, VK_NEXT),
    # Ctrl+Alt+Esc stops the running task and denies any open card. It does
    # nothing when idle.
    "stop": (MOD_CONTROL | MOD_ALT, 0x1B),
    # Ctrl+Alt+G: game mode (everything Xenon runs stops; again restores).
    "game": (MOD_CONTROL | MOD_ALT, ord("G")),
}


class GlobalHotkeys:
    """System-wide hotkeys on their own thread (RegisterHotKey + a message loop).

    ``on_press(key)`` runs on that thread, so callers should hop to Tk with
    ``OrbApp.post``. Keys another program already owns are reported in
    ``failed`` instead of raising.
    """

    def __init__(self, on_press: Callable[[str], None],
                 keys: dict[str, tuple[int, int]] | None = None) -> None:
        self._on_press = on_press
        self._keys = dict(keys or DEFAULT_HOTKEYS)
        self.failed: list[str] = []
        self._thread_id = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="xenon-hotkeys")
        self._thread.start()
        self._ready.wait(2)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self._thread_id = kernel32.GetCurrentThreadId()
        ids: dict[int, str] = {}
        for index, (name, (mods, vk)) in enumerate(self._keys.items(), start=1):
            if user32.RegisterHotKey(None, index, mods | MOD_NOREPEAT, vk):
                ids[index] = name
            else:
                self.failed.append(name)
        self._ready.set()
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY and message.wParam in ids:
                    try:
                        self._on_press(ids[message.wParam])
                    except Exception:
                        pass
        finally:
            for index in ids:
                user32.UnregisterHotKey(None, index)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=1)


# --------------------------------------------------------------- dimmer --

WS_EX_TRANSPARENT = 0x00000020


class ScreenDimmer:
    """Click-through dark overlays for monitors that ignore DDC/CI brightness."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._windows: dict[tuple[int, int, int, int], tk.Toplevel] = {}

    def sync(self, overlays) -> None:
        from xenon.brightness import overlay_alpha

        wanted = {tuple(rect): level for rect, level in overlays}
        for rect in list(self._windows):
            if rect not in wanted or overlay_alpha(wanted[rect]) <= 0.01:
                self._windows.pop(rect).destroy()
        for rect, level in wanted.items():
            alpha = overlay_alpha(level)
            if alpha <= 0.01:
                continue
            window = self._windows.get(rect)
            if window is None:
                window = self._make(rect)
                self._windows[rect] = window
            window.attributes("-alpha", alpha)

    def _make(self, rect) -> tk.Toplevel:
        left, top, right, bottom = rect
        window = tk.Toplevel(self._root)
        window.overrideredirect(True)
        window.configure(bg="black")
        window.geometry(f"{right - left}x{bottom - top}+{left}+{top}")
        window.attributes("-topmost", True)
        window.attributes("-alpha", 0.0)
        window.update_idletasks()
        hwnd = _toplevel_hwnd(window)
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        # Clicks and typing go straight through to the apps underneath.
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        )
        return window

    @property
    def active(self) -> int:
        return len(self._windows)

    def clear(self) -> None:
        for window in self._windows.values():
            window.destroy()
        self._windows.clear()


# ------------------------------------------------------------------ tray --


def start_music_widget_async(app: "OrbApp") -> None:
    """Start the desktop music widget off the Tk thread (it takes a moment
    to build its layers) and hand it to the orb. Missing extras (numpy,
    winsdk, pyaudiowpatch) or a widget already running elsewhere just leave
    it off; the orb never fails because of the widget."""

    def run() -> None:
        try:
            from xenon.music_widget import MusicWidget
        except ImportError as error:
            print(f"music widget off: pip install -e \".[music]\" ({error})", flush=True)
            return
        try:
            widget = MusicWidget()
            # The dock's gear button opens the orb's own settings window.
            widget.on_open_settings = lambda: app.post(app.open_music_settings)
            if widget.start():
                app.music_widget = widget
                print("music widget running (tray > Music widget settings…)", flush=True)
        except Exception as error:  # never take the orb down with it
            print(f"music widget could not start: {error}", flush=True)

    threading.Thread(target=run, daemon=True, name="xenon-music-start").start()


class Tray:
    """Notification-area icon: shows Xenon is running and holds its menu.

    pystray runs its own Win32 message loop on a thread; every menu action
    hops back to Tk through ``OrbApp.post``.
    """

    def __init__(self, app: "OrbApp") -> None:
        import pystray

        from xenon import startup

        self._app = app
        self._startup = startup
        self._last_title = ""
        item = pystray.MenuItem
        menu = pystray.Menu(
            item("Ask   Ctrl+Alt+X", lambda: app.post(lambda: app._action("ask")), default=True),
            item("Voice   Ctrl+Alt+V", lambda: app.post(lambda: app._action("voice")),
                 checked=lambda _i: app.model.voice_on),
            item("Show orb", lambda: app.post(app.toggle_hidden),
                 checked=lambda _i: not app.hidden),
            item("Start at sign-in", self._toggle_startup,
                 checked=lambda _i: self._startup.enabled()),
            item("Game mode   Ctrl+Alt+G", lambda: app.post(app.toggle_game_mode),
                 checked=lambda _i: app.game_mode),
            item("Music widget", lambda: app.post(app.toggle_music_widget),
                 checked=lambda _i: app.music_widget_running()),
            item("Music widget settings…", lambda: app.post(app.open_music_settings)),
            item(lambda _i: f"Companion screen: {app.companion_url or 'off'}" + (
                     f" ({app._companion.lan_note})" if app._companion is not None and app._companion.lan_note
                     else ""),
                 lambda: app.companion_url and webbrowser.open(app.companion_url),
                 enabled=lambda _i: bool(app.companion_url)),
            pystray.Menu.SEPARATOR,
            item("Stop task   Ctrl+Alt+Esc", lambda: app.post(app.stop_task),
                 enabled=lambda _i: app.tasks is not None and app.tasks.has_task()),
            item("Recent actions", lambda: app.post(app.show_recent_actions),
                 enabled=lambda _i: app.tasks is not None),
            pystray.Menu.SEPARATOR,
            item("Quit Xenon", lambda: app.post(app.quit)),
        )
        self._icon = pystray.Icon("xenon", self._image(), "Xenon", menu)
        self._icon.run_detached()

    def _image(self):
        """The idle orb itself, cropped and shrunk to an icon."""
        renderer = OrbRenderer(1.0)
        model = OrbModel()
        model.step(0.5)
        frame = renderer.render(model)
        pad = 4
        box = (int(CENTER - ORB_RADIUS - pad), int(CENTER - ORB_RADIUS - pad),
               int(CENTER + ORB_RADIUS + pad), int(CENTER + ORB_RADIUS + pad))
        return frame.crop(box).resize((64, 64))

    def _toggle_startup(self) -> None:
        try:
            if self._startup.enabled():
                self._startup.disable()
            else:
                self._startup.enable()
        except Exception as error:
            message = f"could not change start-at-sign-in: {error}"
            self._app.post(lambda: self._app._show_error(message))
        self._icon.update_menu()

    def refresh(self) -> None:
        """Keep the hover tooltip in step with what the orb is doing."""
        from xenon.orb_model import STATE_LABELS

        state = STATE_LABELS.get(self._app.model.state, self._app.model.state)
        title = "Xenon" if state == "XENON" else f"Xenon: {state}"
        if title != self._last_title:
            self._last_title = title
            try:
                self._icon.title = title
            except Exception:
                pass

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            pass


# ---------------------------------------------------------------- panels --


class InputPill:
    """A one-line prompt that slides out beside the orb."""

    def __init__(self, root: tk.Tk) -> None:
        self.top = tk.Toplevel(root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(bg=PANEL_BG)
        frame = tk.Frame(self.top, bg=PANEL_BG, padx=14, pady=10)
        frame.pack(fill="both", expand=True)
        self._icon = tk.Label(frame, text="", font=("Segoe MDL2 Assets", 12), fg="#a78bfa", bg=PANEL_BG)
        self._icon.pack(side="left", padx=(0, 10))
        self.entry = tk.Entry(
            frame, font=("Segoe UI", 11), fg=PANEL_FG, bg=PANEL_BG,
            insertbackground="#a78bfa", relief="flat", highlightthickness=0, width=34,
        )
        self.entry.pack(side="left", fill="x", expand=True)
        self._hint = tk.Label(frame, text="enter", font=("Segoe UI", 8), fg=PANEL_DIM, bg=PANEL_BG)
        self._hint.pack(side="left", padx=(8, 0))
        self.entry.bind("<Return>", self._submit)
        self.entry.bind("<Escape>", lambda _e: self.hide())
        self._on_submit: Callable[[str], None] | None = None
        self.visible = False
        _round_corners(self.top, "#2a3150")

    def fill(self, glyph: str, placeholder: str, accent: str,
             on_submit: Callable[[str], None]) -> None:
        """Set the content first, so ``size`` measures what will be shown."""
        self._on_submit = on_submit
        self._icon.config(text=glyph, fg=accent)
        self.entry.config(insertbackground=accent)
        self.entry.delete(0, tk.END)
        self._hint.config(text=placeholder)

    def show_at(self, x: int, y: int) -> None:
        self.top.geometry(f"+{x}+{y}")
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()
        self.entry.focus_set()
        self.visible = True

    def size(self) -> tuple[int, int]:
        self.top.update_idletasks()
        return self.top.winfo_reqwidth(), self.top.winfo_reqheight()

    def hide(self) -> None:
        self.top.withdraw()
        self.visible = False

    def _submit(self, _event=None) -> None:
        text = self.entry.get().strip()
        if not text or self._on_submit is None:
            return
        self.hide()
        self._on_submit(text)


class AnswerPanel:
    """A rounded card that shows an answer, search results, or a chat log."""

    def __init__(self, root: tk.Tk, scale: float) -> None:
        self.scale = scale
        self.top = tk.Toplevel(root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(bg=PANEL_BG)
        self.top.bind("<Escape>", lambda _e: self.hide())

        header = tk.Frame(self.top, bg=PANEL_BG)
        header.pack(fill="x", padx=16, pady=(12, 4))
        self._title = tk.Label(header, text="", font=("Segoe UI Semibold", 10), fg="#c4b5fd", bg=PANEL_BG)
        self._title.pack(side="left")
        self._note = tk.Label(header, text="", font=("Segoe UI", 8), fg=PANEL_DIM, bg=PANEL_BG)
        self._note.pack(side="left", padx=(8, 0))
        close = tk.Label(header, text="", font=("Segoe MDL2 Assets", 9), fg=PANEL_DIM, bg=PANEL_BG, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self.hide())
        copy = tk.Label(header, text="", font=("Segoe MDL2 Assets", 9), fg=PANEL_DIM, bg=PANEL_BG, cursor="hand2")
        copy.pack(side="right", padx=(0, 12))
        copy.bind("<Button-1>", lambda _e: self._copy())

        self.text = tk.Text(
            self.top, wrap="word", font=("Segoe UI", 10), fg=PANEL_FG, bg=PANEL_BG,
            relief="flat", highlightthickness=0, padx=16, pady=4, width=46, height=4,
            cursor="arrow", spacing1=2, spacing3=4,
        )
        self.text.pack(fill="both", expand=True, pady=(0, 6))
        self.text.tag_configure("dim", foreground=PANEL_DIM, font=("Segoe UI", 9))
        self.text.tag_configure("you", foreground="#93c5fd", font=("Segoe UI Semibold", 10))
        self.text.tag_configure("link", foreground="#7dd3fc", font=("Segoe UI Semibold", 10))
        self.text.tag_bind("link", "<Enter>", lambda _e: self.text.config(cursor="hand2"))
        self.text.tag_bind("link", "<Leave>", lambda _e: self.text.config(cursor="arrow"))

        self._entry_row = tk.Frame(self.top, bg=ENTRY_BG, padx=12, pady=8)
        self.entry = tk.Entry(
            self._entry_row, font=("Segoe UI", 10), fg=PANEL_FG, bg=ENTRY_BG,
            insertbackground="#a78bfa", relief="flat", highlightthickness=0,
        )
        self.entry.pack(fill="x")
        self.entry.bind("<Escape>", lambda _e: self.hide())
        self._chat_submit: Callable[[str], None] | None = None
        self.entry.bind("<Return>", self._submit_chat)
        self.visible = False
        self.chat_mode = False
        self._links = 0
        _round_corners(self.top, "#2a3150")

    # content

    def clear(self) -> None:
        self.text.config(state="normal")
        self.text.delete("1.0", tk.END)
        self._links = 0

    def append(self, line: str, tag: str | None = None) -> None:
        self.text.config(state="normal")
        self.text.insert(tk.END, line, tag or ())
        self.text.config(state="disabled")

    def append_link(self, title: str, url: str) -> None:
        tag = f"link{self._links}"
        self._links += 1
        self.text.config(state="normal")
        self.text.insert(tk.END, title, ("link", tag))
        self.text.tag_bind(tag, "<Button-1>", lambda _e, u=url: webbrowser.open(u))
        self.text.config(state="disabled")

    def set_header(self, title: str, note: str = "", accent: str = "#c4b5fd") -> None:
        self._title.config(text=title, fg=accent)
        self._note.config(text=note)

    def set_chat(self, enabled: bool, on_submit: Callable[[str], None] | None = None) -> None:
        self.chat_mode = enabled
        self._chat_submit = on_submit
        if enabled:
            self._entry_row.pack(fill="x", side="bottom")
        else:
            self._entry_row.pack_forget()

    def _submit_chat(self, _event=None) -> None:
        text = self.entry.get().strip()
        if text and self._chat_submit:
            self.entry.delete(0, tk.END)
            self._chat_submit(text)

    def _copy(self) -> None:
        body = self.text.get("1.0", tk.END).strip()
        self.top.clipboard_clear()
        self.top.clipboard_append(body)
        self._note.config(text="copied")

    # layout

    def fit(self, max_lines: int = 14) -> None:
        self.text.config(height=1)
        self.top.update_idletasks()
        try:
            lines = int(self.text.count("1.0", "end", "displaylines")[0])
        except Exception:
            lines = int(self.text.index("end-1c").split(".")[0])
        self.text.config(height=max(2, min(lines, max_lines)))
        if self.chat_mode:
            self.text.see(tk.END)

    def size(self) -> tuple[int, int]:
        self.top.update_idletasks()
        return self.top.winfo_reqwidth(), self.top.winfo_reqheight()

    def show_at(self, x: int, y: int) -> None:
        self.top.geometry(f"+{x}+{y}")
        self.top.deiconify()
        self.top.lift()
        self.visible = True
        if self.chat_mode:
            self.top.focus_force()
            self.entry.focus_set()

    def hide(self) -> None:
        self.top.withdraw()
        self.visible = False


# ------------------------------------------------------------------ app --


def _config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "xenon", "orb.json")


def load_position() -> tuple[int, int] | None:
    try:
        with open(_config_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return int(data["x"]), int(data["y"])
    except Exception:
        return None


def save_position(x: int, y: int) -> None:
    try:
        path = _config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"x": x, "y": y}, handle)
    except Exception:
        pass


def shorten(text: str, limit: int) -> str:
    """Trim at a word boundary and add an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:")
    return cut + "…"


def _work_area() -> tuple[int, int, int, int]:
    """The primary monitor minus the taskbar, in physical pixels."""
    rect = wintypes.RECT()
    try:
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    user = ctypes.windll.user32
    return 0, 0, user.GetSystemMetrics(0), user.GetSystemMetrics(1)


def _hex(color: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % color


class OrbApp:
    FPS = 30
    GLYPHS = {"ask": "", "search": "", "screen": "", "chat": ""}

    def __init__(
        self,
        backend_factory: Callable[[], OrbBackend],
        voice_runner: VoiceRunner | None = None,
        tray: bool = True,
        task_services: TaskServices | None = None,
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("the orb needs Windows (layered windows)")
        _enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.title("Xenon")
        self._events: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._worker = Worker(backend_factory)
        self._voice_runner = voice_runner
        self._voice_stop: threading.Event | None = None
        self._listen_event: threading.Event | None = None
        self._history: list[tuple[str, str]] = []
        self._busy = False
        self.music_widget = None        # desktop music widget, started by __main__
        self._music_settings_window = None

        hwnd = _toplevel_hwnd(self.root)
        self.scale = _dpi_scale(hwnd)
        self.size = int(round(WINDOW * self.scale))
        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()
        self.notch = os.environ.get("XENON_ORB_NOTCH", "on").strip().lower() not in {"0", "off", "false", "no"}
        if self.notch:
            position = self._notch_position()
        else:
            position = load_position()
            if position is None or not self._on_screen(*position):
                position = (
                    self.screen_w - self.size - int(8 * self.scale),
                    self.screen_h - self.size - int(40 * self.scale),
                )
        self.x, self.y = position
        self.root.geometry(f"{self.size}x{self.size}+{self.x}+{self.y}")
        # Mark the window "never activate" before it is first shown; showing
        # it first made the orb the active window, so the first question after
        # startup read the orb instead of the user's app.
        previous = ctypes.windll.user32.GetForegroundWindow()
        self.window = LayeredWindow(_toplevel_hwnd(self.root))
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self._give_back_focus(previous)

        self.model = OrbModel(notch=self.notch)
        default_idle = NOTCH_IDLE_S if self.notch else self.model.idle_after
        try:
            self.model.idle_after = float(os.environ.get("XENON_ORB_IDLE_S", default_idle))
        except ValueError:
            self.model.idle_after = default_idle
        if self.notch:
            self.model.set_direction(math.pi / 2)  # options fan down from the top edge
        else:
            self._update_direction()
        self.renderer = OrbRenderer(self.scale)
        self.pill = InputPill(self.root)
        self.panel = AnswerPanel(self.root, self.scale)
        self._own_hwnds = {
            self.window.hwnd, _toplevel_hwnd(self.pill.top), _toplevel_hwnd(self.panel.top)
        }
        # Tasks ("do <goal>"): one broker for the whole process, cards on
        # this thread, the TaskManager thread made on the first task. Only
        # the IPC thread starts now (asleep in ConnectNamedPipe), so `xenon
        # do` can hand an outward goal to the orb.
        self.tasks: OrbTasks | None = None
        self._task_text = ""
        self._cards: dict[str, object] = {}
        self._choice_cards: dict[str, object] = {}
        self._undo_strips: list = []
        self._history_private = False
        self._voice_say: "queue.Queue[tuple[str, str]] | None" = None
        self._answer_placeholder = ""  # "your answer" while a task waits
        if task_services is not None:
            self.tasks = OrbTasks(task_services, self.post, self)
            self.tasks.start_ipc()

        self._last_external = 0
        self._frame = 0
        self._render_failed = False
        self._last_frame: tuple | None = None
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._dragged = False
        self._press_target: str | None = None
        self.root.bind("<ButtonPress-1>", self._on_press)
        self.root.bind("<B1-Motion>", self._on_drag)
        self.root.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Button-3>", lambda _e: self._action("voice"))
        self.root.bind("<MouseWheel>", self._on_wheel)
        self._notch_flash: tuple[str, float] | None = None
        self.root.report_callback_exception = self._callback_error
        self._closing = False
        from xenon import quick

        self._timers = quick.TIMERS
        self._timers.listen(self._on_timer_thread)
        self.hidden = False
        self.game_mode = False
        self._game_restore: dict = {}
        self._game_watch = None
        self.companion_url = ""
        self._companion = None
        if os.environ.get("XENON_COMPANION", "on").strip().lower() not in {"0", "off", "false", "no"}:
            self._start_companion()
        self.dimmer = ScreenDimmer(self.root)
        self._last_overlays: list = []
        try:
            from xenon.brightness import software_overlays

            self.dimmer.sync(software_overlays())  # stay dimmed across restarts
        except Exception:
            pass
        self._tray: Tray | None = None
        if tray:
            try:
                self._tray = Tray(self)
            except Exception as error:  # pystray missing or no shell
                print(f"tray icon unavailable: {error}", file=sys.stderr, flush=True)
        self._hotkeys: GlobalHotkeys | None = None
        try:
            self._hotkeys = GlobalHotkeys(lambda key: self.post(lambda: self._hotkey(key)))
            if self._hotkeys.failed:
                print(
                    "hotkeys already taken by another app: " + ", ".join(self._hotkeys.failed),
                    file=sys.stderr, flush=True,
                )
        except Exception as error:
            print(f"global hotkeys unavailable: {error}", file=sys.stderr, flush=True)
        self._last = time.perf_counter()
        self._start_game_watch()
        self._tick()

    def _start_companion(self) -> None:
        try:
            from xenon.companion import DEFAULT_PORT, Companion
            from xenon.lyrics import LyricsFinder
            from xenon.webplayer import WebPlayer

            port = int(os.environ.get("XENON_COMPANION_PORT", DEFAULT_PORT))
            self._companion = Companion(
                self.companion_state, WebPlayer().now_playing, LyricsFinder().find, port=port
            )
            self._companion.always_poll = self.notch  # the notch shows the song
            self.companion_url = self._companion.start()
            print(f"companion screen: {self.companion_url}", flush=True)
        except Exception as error:
            self._companion = None
            self.companion_url = ""
            print(f"companion screen unavailable: {error}", file=sys.stderr, flush=True)

    # ----- game mode ----------------------------------------------------

    def toggle_game_mode(self) -> None:
        if self.game_mode:
            self.exit_game_mode(by_hand=True)
        else:
            self.enter_game_mode()

    def enter_game_mode(self) -> None:
        """Stop everything Xenon runs (see ``game_mode``), remembering what
        was on so leaving brings exactly that back."""
        if self.game_mode or self._closing:
            return
        from xenon import game_mode

        self.game_mode = True
        restore = self._game_restore = {
            "hidden": self.hidden,
            "voice": self._voice_stop is not None,
            "widget": self.music_widget_running(),
            "companion": self._companion is not None,
        }
        if self.tasks is not None and self.tasks.has_task():
            self.stop_task()
        if restore["voice"]:
            self._toggle_voice()
        for card in [*self._cards.values(), *self._choice_cards.values()]:
            try:
                card.close()
            except Exception:
                pass
        if not self.hidden:
            self.toggle_hidden()
        widget = self.music_widget
        companion = self._companion
        self._companion, self.companion_url = None, ""

        def wind_down() -> None:
            if widget is not None:
                try:
                    widget.stop()          # its ring and lyric processes quit with it
                except Exception:
                    pass
            if companion is not None:
                try:
                    companion.stop()
                except Exception:
                    pass
            asked = []
            try:
                asked = game_mode.close_apps(game_mode.load().close_apps)
            except Exception as error:
                print(f"game mode: could not close apps: {error}", file=sys.stderr, flush=True)
            game_mode.trim_memory()
            note = f"; asked to close: {', '.join(asked)}" if asked else ""
            print(f"game mode on (Ctrl+Alt+G to leave){note}", flush=True)

        self._game_wind_down = threading.Thread(target=wind_down, daemon=True, name="xenon-game-mode")
        self._game_wind_down.start()
        if self._tray is not None:
            self._tray._icon.update_menu()

    def exit_game_mode(self, by_hand: bool = False) -> None:
        if not self.game_mode:
            return
        self.game_mode = False
        if by_hand and self._game_watch is not None:
            self._game_watch.left_by_hand()
        restore, self._game_restore = self._game_restore, {}
        wind = getattr(self, "_game_wind_down", None)

        def come_back() -> None:
            # Left again quickly: let the old widget and server finish
            # stopping first (one widget at a time, one server per port).
            if wind is not None:
                wind.join(timeout=10.0)
            if self.game_mode:
                return
            if restore.get("companion") and self._companion is None:
                self._start_companion()
            if restore.get("widget"):
                start_music_widget_async(self)

        threading.Thread(target=come_back, daemon=True, name="xenon-game-back").start()
        if self.hidden and not restore.get("hidden", False):
            self.toggle_hidden()
        if restore.get("voice") and self._voice_stop is None:
            self._toggle_voice()
        self._last = time.perf_counter()
        print("game mode off", flush=True)
        if self._tray is not None:
            self._tray._icon.update_menu()

    def _start_game_watch(self) -> None:
        """With ``auto`` on in game_mode.json, a full-screen game switches
        game mode on by itself (and off again when it closes)."""
        try:
            from xenon import game_mode

            if not game_mode.load().auto:
                return
            watch = self._game_watch = game_mode.AutoWatch()
        except Exception:
            return

        def run() -> None:
            while not self._closing:
                try:
                    step = watch.tick(time.monotonic(), self.game_mode)
                except Exception:
                    step = None
                if step == "enter":
                    self.post(self.enter_game_mode)
                elif step == "leave":
                    self.post(self.exit_game_mode)
                time.sleep(2.0)

        threading.Thread(target=run, daemon=True, name="xenon-game-watch").start()

    # ----- timers -------------------------------------------------------

    def _on_timer_thread(self, timer) -> None:
        """Called on the timer's thread; hop to Tk."""
        self.post(lambda: self._timer_done(timer))

    def _timer_done(self, timer) -> None:
        from xenon import chime
        from xenon.quick import describe

        chime.play("timer")
        self.model.set_state("heard", f"{timer.name} is done")
        self.panel.set_chat(False)
        self.panel.clear()
        self.panel.set_header("Timer done", "", "#fbbf24")
        self.panel.append(f"{timer.name[0].upper() + timer.name[1:]} is done.")
        self.panel.append("\n" + describe(timer.seconds), "dim")
        self._place_panel()
        self._update_timer_ring()
        self.root.after(600, lambda: self.model.state == "heard" and self.model.set_state(
            "standby" if self.model.voice_on else "idle"))

    def _update_timer_ring(self) -> None:
        active = self._timers.active()
        if not active:
            self.model.timer_fraction = None
            return
        nearest = active[0]
        self.model.timer_fraction = nearest.remaining() / max(1, nearest.seconds)

    def _give_back_focus(self, previous=None) -> None:
        """The orb must never be the active window; hand focus back.

        Tk's show call activates the window despite WS_EX_NOACTIVATE, and at
        startup the window that was active is often gone (the old orb), so
        fall back to the next real window underneath.
        """
        try:
            self.root.update_idletasks()
            user32 = ctypes.windll.user32
            if user32.GetForegroundWindow() != self.window.hwnd:
                return
            target = previous if previous and user32.IsWindow(previous) else None
            if target in self._own_hwnds_safe():
                target = None
            target = target or self._next_real_window()
            if target:
                user32.SetForegroundWindow(target)
        except Exception:
            pass

    def _own_hwnds_safe(self) -> set:
        return getattr(self, "_own_hwnds", {self.window.hwnd})

    def _next_real_window(self):
        """The first visible, titled, normal window below the orb in Z-order."""
        user32 = ctypes.windll.user32
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        GW_HWNDNEXT = 2
        hwnd = user32.GetWindow(self.window.hwnd, GW_HWNDNEXT)
        own = self._own_hwnds_safe()
        for _ in range(500):
            if not hwnd:
                return None
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if (
                hwnd not in own
                and user32.IsWindowVisible(hwnd)
                and not user32.IsIconic(hwnd)
                and user32.GetWindowTextLengthW(hwnd) > 0
                and not style & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
            ):
                return hwnd
            hwnd = user32.GetWindow(hwnd, GW_HWNDNEXT)
        return None

    def _notch_info(self) -> dict:
        """Live content for the folded notch: clock, song, timer, voice."""
        info: dict = {"clock": time.strftime("%I:%M").lstrip("0"), "voice": self.model.voice_on}
        if self._notch_flash and self._notch_flash[1] > time.monotonic():
            info["flash"] = self._notch_flash[0]
        if self._task_text:
            info["task"] = self._task_text  # "Step 2/5 · Type text"
        active = self._timers.active()
        if active:
            nearest = active[0]
            left = int(round(nearest.remaining()))
            info["timer"] = {
                "text": f"{left // 60}:{left % 60:02d}" if left < 3600 else f"{left // 3600}h {left % 3600 // 60}m",
                "name": nearest.label,
                "fraction": nearest.remaining() / max(1, nearest.seconds),
            }
        if self._companion is not None:
            music = self._companion.current_music()
            if music and (music.get("playing") or music.get("title")):
                info["music"] = {"title": music.get("title", ""), "artist": music.get("artist", ""),
                                 "playing": bool(music.get("playing"))}
                if not music.get("playing"):
                    info.pop("music")  # paused: back to the clock
        return info

    def _on_wheel(self, event) -> None:
        """Scroll over the notch or orb to change the volume."""
        from xenon import quick

        lx, ly = self._local(event.x_root, event.y_root)
        if not (self.model.in_notch(lx, ly) or self.model.hit(lx, ly) == "orb"):
            return
        up = event.delta > 0
        quick.run(quick.QuickCommand("volume_up" if up else "volume_down", steps=1))
        self._notch_flash = ("Volume up" if up else "Volume down", time.monotonic() + 1.2)
        self.model.notch_info = self._notch_info()

    def companion_state(self) -> dict:
        """What the companion screen shows when no music plays (read-only)."""
        from xenon.orb_model import STATE_LABELS

        timers = [
            {"name": t.name, "remaining_s": t.remaining(), "total_s": t.seconds}
            for t in self._timers.active()
        ]
        last = self._history[-1][1] if self._history else ""
        detail = self.model.detail
        # Answers built from the screen or memory, and anything a task is
        # doing, never reach the LAN page.
        if self._history_private and last:
            last = "Private"
        if self._task_text or self.model.state in {"working", "confirm"} or (
            self._history_private and self.model.state not in {"idle", "standby"}
        ):
            detail = "Private task"
        return {
            "state": self.model.state,
            "label": STATE_LABELS.get(self.model.state, self.model.state),
            "detail": detail,
            "voice": self.model.voice_on,
            "timers": timers,
            "last": last[:400],
            "private": bool(self._history_private),
        }

    def _sync_dimmer(self) -> None:
        """Follow brightness changes made by voice, Ask or the CLI."""
        try:
            from xenon.brightness import software_overlays

            overlays = sorted(software_overlays())
        except Exception:
            return
        if overlays != self._last_overlays:
            self._last_overlays = overlays
            self.dimmer.sync(overlays)
            self._raise_orb()

    def _raise_orb(self) -> None:
        """Keep the orb above the dimming overlays."""
        try:
            flags = 0x0001 | 0x0002 | 0x0010  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            ctypes.windll.user32.SetWindowPos(self.window.hwnd, -1, 0, 0, 0, 0, flags)
        except Exception:
            pass

    def toggle_hidden(self) -> None:
        """Tuck the orb into the tray, or bring it back."""
        self.hidden = not self.hidden
        if self.hidden:
            self.pill.hide()
            self.panel.hide()
            self.root.withdraw()
        else:
            previous = ctypes.windll.user32.GetForegroundWindow()
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self._give_back_focus(previous)
        if self._tray is not None:
            self._tray._icon.update_menu()

    def _hotkey(self, key: str) -> None:
        """Ctrl+Alt+X toggles the Ask pill; Ctrl+Alt+V toggles voice."""
        if key == "game":
            self.toggle_game_mode()
            return
        if self.game_mode:
            return          # nothing pops up over a game
        if key == "ask":
            if self.pill.visible:
                self.pill.hide()
                self._return_focus()
            else:
                self._track_foreground()  # remember the window you were in
                self._action("ask")
        elif key == "voice":
            self._action("voice")
        elif key == "listen":
            self._push_to_talk()
        elif key == "stop":
            self.stop_task()
        elif key in {"dim", "brighter", "darker"}:
            self._brightness({"dim": "toggle", "brighter": "up", "darker": "down"}[key])

    def _push_to_talk(self) -> None:
        """Listen right now; starts voice first if it is off."""
        if self._voice_stop is None:
            self._toggle_voice()
        if self._listen_event is not None:
            self._listen_event.set()

    def _brightness(self, action: str, value: int = 0) -> None:
        """DDC/CI writes take ~100 ms per monitor, so they run off the Tk thread."""
        from xenon import brightness

        self.model.set_state("heard", f"brightness {action}")

        def work() -> None:
            outcome = brightness.apply_detailed(action, value)

            def done() -> None:
                # Monitors that ignored the hardware change are dimmed on screen.
                self.dimmer.sync(outcome.overlays)
                self._raise_orb()
                if not outcome.ok:
                    self._show_error(outcome.reply)
                elif self.model.state == "heard":
                    self.model.set_state("standby" if self.model.voice_on else "idle")
            self.post(done)

        threading.Thread(target=work, daemon=True, name="xenon-brightness").start()

    def _callback_error(self, kind, value, traceback) -> None:
        # Ctrl+C in the terminal lands inside whichever Tk callback is running.
        if issubclass(kind, KeyboardInterrupt):
            self.quit()
            return
        print(f"orb error: {value!r}", file=sys.stderr, flush=True)

    # ----- helpers ------------------------------------------------------

    def post(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` on the Tk thread (safe from any thread)."""
        self._events.put(callback)

    def _notch_position(self) -> tuple[int, int]:
        """Window position that centres the notch on the primary monitor's top edge."""
        left, top, right = 0, 0, self.screen_w
        try:
            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            user32.MonitorFromPoint.restype = wintypes.HMONITOR
            monitor = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # primary
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                left, top, right = info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right
        except Exception:
            pass
        x = (left + right) // 2 - self.size // 2
        y = top + int(NOTCH_DROP * self.scale) - self.size // 2
        return x, y

    def _on_screen(self, x: int, y: int) -> bool:
        return -self.size // 2 < x < self.screen_w - self.size // 2 and -self.size // 2 < y < self.screen_h - self.size // 2

    def _update_direction(self) -> None:
        self.model.set_direction(fan_direction(self.x / self.scale, self.y / self.scale,
                                               self.screen_w / self.scale, self.screen_h / self.scale))

    def _local(self, screen_x: int, screen_y: int) -> tuple[float, float]:
        return (screen_x - self.x) / self.scale, (screen_y - self.y) / self.scale

    def _accent(self) -> str:
        return _hex(self.model.colors()[1])

    def _side_position(self, width: int, height: int, clear_fan: bool = False) -> tuple[int, int]:
        """Place a panel beside the orb, on the side facing the screen centre.

        ``clear_fan`` keeps it outside the satellites' reach, so a card that
        stays open never covers the options it shares a side with.
        """
        orb_cx = self.x + self.size / 2
        orb_cy = self.y + self.size / 2
        reach = SATELLITE_DISTANCE + SATELLITE_RADIUS + 10 if clear_fan else ORB_RADIUS + 18
        gap = int(reach * self.scale)
        if orb_cx > self.screen_w / 2:
            x = orb_cx - gap - width
        else:
            x = orb_cx + gap
        y = orb_cy - height / 2
        margin = int(8 * self.scale)
        left, top, right, bottom = _work_area()
        x = min(max(x, left + margin), right - width - margin)
        y = min(max(y, top + margin), bottom - height - margin)
        return int(x), int(y)

    def _track_foreground(self) -> None:
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            return
        if hwnd and hwnd not in self._own_hwnds:
            self._last_external = hwnd
        elif hwnd == self.window.hwnd:
            # The orb itself should never hold focus (only the pill and cards
            # may, while you type in them).
            self._give_back_focus(self._last_external)

    def _return_focus(self) -> None:
        """Hand focus back to the user's window before Xenon reads the screen."""
        try:
            current = ctypes.windll.user32.GetForegroundWindow()
            if current in self._own_hwnds and self._last_external:
                ctypes.windll.user32.SetForegroundWindow(self._last_external)
        except Exception:
            pass

    # ----- loop ---------------------------------------------------------

    def _tick(self) -> None:
        now = time.perf_counter()
        dt = now - self._last
        self._last = now
        while True:
            try:
                callback = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as error:
                self.model.set_state("error", str(error))
                print(f"orb callback failed: {error!r}", file=sys.stderr, flush=True)
        if self.game_mode:
            # Gaming: only hand over posted work (the tray, the hotkey to
            # leave), a few times a second; no drawing, no window tracking.
            self.root.after(400, self._tick)
            return

        self._frame += 1
        if self._frame % 6 == 0:
            self._track_foreground()
        px, py = self.root.winfo_pointerxy()
        lx, ly = self._local(px, py)
        if self._drag_origin is not None and self._dragged:
            self.model.pointer(None, None)
        else:
            self.model.pointer(lx, ly)
        if self._frame % 10 == 0:
            self._update_timer_ring()
        if self._frame % 15 == 0 and self._tray is not None:
            self._tray.refresh()
        if self._frame % 30 == 0:
            self._sync_dimmer()
        if self.notch and self._frame % 10 == 0:
            self.model.notch_info = self._notch_info()
        if self.pill.visible or self.panel.visible or self._busy:
            self.model.mark_active()  # never shrink while you're using it
        self.model.step(dt)
        if self.hidden:
            # Tucked into the tray: keep events, timers and voice running but
            # skip drawing, and wake up less often.
            self.root.after(150, self._tick)
            return
        try:
            frame = self.renderer.render(self.model)
            data = premultiplied_bgra(frame)
            signature = (zlib.crc32(data), self.x, self.y)
            if signature != self._last_frame:
                self._last_frame = signature
                self.window.update(data, frame.width, frame.height, self.x, self.y)
        except Exception as error:
            if not self._render_failed:
                self._render_failed = True
                print(f"orb render failed: {error!r}", file=sys.stderr, flush=True)
        fps = self.FPS
        if self.notch and self.model.dock >= 0.999:
            music = (self.model.notch_info or {}).get("music")
            fps = 12 if music else 4  # bars and scrolling title vs a slow breathing dot
        delay = int(1000 / fps)
        self.root.after(max(5, delay - int((time.perf_counter() - now) * 1000)), self._tick)

    # ----- input --------------------------------------------------------

    def _on_press(self, event) -> None:
        lx, ly = self._local(event.x_root, event.y_root)
        self._press_target = self.model.hit(lx, ly)
        self.model.pressed = self._press_target
        self._drag_origin = (event.x_root, event.y_root, self.x, self.y)
        self._dragged = False

    def _on_drag(self, event) -> None:
        if self.notch:
            return  # the notch stays centred on the top edge
        if self._drag_origin is None or self._press_target != "orb":
            return
        sx, sy, wx, wy = self._drag_origin
        dx, dy = event.x_root - sx, event.y_root - sy
        if not self._dragged and math.hypot(dx, dy) < 5:
            return
        self._dragged = True
        self.x, self.y = wx + dx, wy + dy
        self.root.geometry(f"+{self.x}+{self.y}")
        self.pill.hide()

    def _on_release(self, event) -> None:
        target = self._press_target
        dragged = self._dragged
        self._drag_origin = None
        self._dragged = False
        self.model.pressed = None
        if dragged:
            self._update_direction()
            save_position(self.x, self.y)
            return
        if target == "orb":
            if self.pill.visible:
                self.pill.hide()
            else:
                self._action("ask")
        elif target:
            self._action(target)

    # ----- actions ------------------------------------------------------

    def _action(self, key: str) -> None:
        if self.hidden and key in {"ask", "search", "panel"}:
            self.toggle_hidden()
        if key == "ask":
            waiting = self.tasks is not None and self.tasks.waiting_for_input
            hint = (self._answer_placeholder or "your answer") if waiting else "ask or command"
            self._prompt("ask", hint, self._run_ask)
        elif key == "search":
            self._prompt("search", "search the web", self._run_search)
        elif key == "screen":
            self._run_screen()
        elif key == "voice":
            self._toggle_voice()
        elif key == "panel":
            self._open_chat()
        elif key == "close":
            self.quit()

    def _prompt(self, kind: str, placeholder: str, submit: Callable[[str], None]) -> None:
        self.panel.hide()
        self.model.fold()  # the pill sits where the fan would be
        self.pill.fill(self.GLYPHS[kind], placeholder, self._accent(), submit)
        width, height = self.pill.size()
        self.pill.show_at(*self._side_position(width, height))

    def _start(self, label: str, state: str = "thinking") -> bool:
        if self._busy:
            self.model.set_state("thinking", "still working on the last one")
            return False
        if self._worker.error:
            self._show_error(self._worker.error)
            return False
        self._busy = True
        self.model.set_state(state, label)
        self._return_focus()
        return True

    def _rest_state(self) -> str:
        """Where the orb settles: still 'working' while a task runs."""
        if self.tasks is not None and self.tasks.busy:
            return "working"
        return "standby" if self.model.voice_on else "idle"

    def _finish(self) -> None:
        self._busy = False
        if self.model.state in {"thinking", "searching"}:
            self.model.set_state(self._rest_state())

    def _show_error(self, message: str) -> None:
        self._busy = False
        self.model.set_state("error", message)
        self.panel.set_chat(False)
        self.panel.clear()
        self.panel.set_header("Something went wrong", "", "#f87171")
        self.panel.append(message)
        self._place_panel()
        self.root.after(4000, lambda: self.model.state == "error" and self.model.set_state(
            "standby" if self.model.voice_on else "idle"))

    def _place_panel(self) -> None:
        self.model.fold()
        self.panel.fit()
        width, height = self.panel.size()
        self.panel.show_at(*self._side_position(width, height, clear_fan=True))

    def _show_reply(self, title: str, question: str, reply: OrbReply, started: float) -> None:
        self._history.append((question, reply.text))
        self._history_private = bool(getattr(reply, "private", False))
        if self.panel.chat_mode and self.panel.visible:
            self._render_chat()
            self._place_panel()
            self._finish()
            return
        self.panel.set_chat(False)
        self.panel.clear()
        note = reply.note or f"{(time.perf_counter() - started):.1f}s"
        self.panel.set_header(title, note, self._accent())
        self.panel.append(reply.text)
        if reply.sources:
            self.panel.append("\n\nsources\n", "dim")
            self.panel.append("\n".join(reply.sources), "dim")
        self._place_panel()
        self._finish()

    def _run_ask(self, text: str) -> None:
        # Request path: a waiting task's focus, stop/cancel, "do <goal>"
        # first; everything else is answered exactly as before.
        if self.tasks is not None:
            try:
                routed = self.tasks.route(text, "typed")
            except Exception as error:
                self._show_error(f"task: {error}")
                return
            if routed is not None:
                self._task_ack(routed, text)
                return
        if not self._start(text):
            return
        started = time.perf_counter()

        def job(backend: OrbBackend) -> None:
            try:
                reply = backend.ask(text)
            except Exception as error:
                self.post(lambda message=str(error): self._show_error(message))
                return
            self.post(lambda: self._show_reply("Xenon", text, reply, started))

        self._worker.submit(job, lambda message: self.post(lambda: self._show_error(message)))

    def _run_screen(self) -> None:
        if not self._start("reading your screen"):
            return
        started = time.perf_counter()

        def job(backend: OrbBackend) -> None:
            try:
                reply = backend.screen()
            except Exception as error:
                self.post(lambda message=str(error): self._show_error(message))
                return
            self.post(lambda: self._show_reply("On your screen", "(screen)", reply, started))

        self._worker.submit(job, lambda message: self.post(lambda: self._show_error(message)))

    def _run_search(self, query: str) -> None:
        if not self._start(query, "searching"):
            return
        started = time.perf_counter()

        def job(backend: OrbBackend) -> None:
            try:
                results = backend.search(query)
            except Exception as error:
                self.post(lambda message=str(error): self._show_error(message))
                return
            self.post(lambda: self._show_results(query, results, started))

        self._worker.submit(job, lambda message: self.post(lambda: self._show_error(message)))

    def _show_results(self, query: str, results: list[OrbLink], started: float) -> None:
        self.panel.set_chat(False)
        self.panel.clear()
        self.panel.set_header(f"Search · {query}", f"{(time.perf_counter() - started):.1f}s", self._accent())
        if not results:
            self.panel.append("No results.", "dim")
        for index, result in enumerate(results):
            if index:
                self.panel.append("\n\n")
            self.panel.append_link(result.title, result.url)
            if result.snippet:
                self.panel.append("\n" + shorten(result.snippet, 180), "dim")
        self._place_panel()
        self._finish()

    def _open_chat(self) -> None:
        self.pill.hide()
        self.panel.set_chat(True, self._run_ask)
        self._render_chat()
        self._place_panel()

    def _render_chat(self) -> None:
        self.panel.clear()
        self.panel.set_header("Xenon", f"{len(self._history)} messages", self._accent())
        if not self._history:
            self.panel.append("Ask anything about your screen or notes, or say what to open.", "dim")
        for index, (question, answer) in enumerate(self._history[-12:]):
            if index:
                self.panel.append("\n\n")
            self.panel.append(question + "\n", "you")
            self.panel.append(answer)
        self.panel.fit()

    # ----- tasks --------------------------------------------------------
    # Called on the Tk thread (OrbTasks posts them). Cards never take focus;
    # the panel hands focus back to his window after showing a result.

    def _task_ack(self, text: str, question: str = "", typed: bool = True) -> None:
        """The task layer took a request ("On it.", "Got it.", "Stopped.")."""
        if typed:
            self._return_focus()  # the pill just closed: his window gets the keys back
        if not text:
            return
        if typed and question and self.panel.chat_mode and self.panel.visible:
            self._history.append((question, text))
            self._history_private = False
            self._render_chat()
            self._place_panel()
        self._notch_flash = (text, time.monotonic() + 1.5)
        if self.notch:
            self.model.notch_info = self._notch_info()
        elif self.model.state in TASK_OVERRIDABLE_STATES:
            self.model.set_state(self.model.state, text)

    def _task_anchor(self) -> tuple[int, int]:
        """Where the next card goes: by the notch, on the target app's monitor."""
        target = self._last_external
        try:
            foreground = ctypes.windll.user32.GetForegroundWindow()
            if foreground and foreground not in self._own_hwnds_safe():
                target = foreground
        except Exception:
            pass
        return card_anchor(
            (self.x, self.y, self.size),
            self.scale,
            self.notch,
            _work_area_for(self.window.hwnd),
            _work_area_for(target) if target else None,
            index=len(self._cards) + len(self._choice_cards),
        )

    def _settle_after_card(self) -> None:
        if self._cards or self._choice_cards:
            return
        if self.model.state == "confirm":
            self.model.set_state(self._rest_state(), self._task_text)

    def task_card(self, pending, decide: Callable[[str], None]) -> None:
        """Broker presenter: one ConfirmCard per pending action."""
        if pending.action_id in self._cards:
            return
        try:
            from xenon.orb_cards import ConfirmCard

            card = ConfirmCard(self.root, pending, decide, self._task_anchor())
        except Exception as error:
            print(f"confirm card failed: {error!r}", file=sys.stderr, flush=True)
            decide("deny")  # a card that cannot be shown is a no
            return
        self._cards[pending.action_id] = card
        self.model.card_opened()  # unfold, and never fold while it is up
        self.model.set_state("confirm", shorten(pending.title, 60))
        if self.tasks is not None and self.tasks.origin_of(pending.task_id) == "voice":
            self._voice_announce("say", "Please confirm on screen.")

    def task_card_closed(self, action_id: str) -> None:
        card = self._cards.pop(action_id, None)
        if card is None:
            return
        try:
            card.close()
        except Exception:
            pass
        self.model.card_closed()  # the 3 s fold restarts from now
        self._settle_after_card()

    def task_choice(self, prompt_id: str, title: str, labels: list[str],
                    on_pick: Callable[[str | None], None], origin: str = "typed") -> None:
        """Queue / replace / cancel for a second task (or any code-built pick)."""
        if prompt_id in self._choice_cards:
            return
        try:
            from xenon.orb_cards import ChoiceCard

            card = ChoiceCard(self.root, title, labels, on_pick, self._task_anchor())
        except Exception as error:
            print(f"choice card failed: {error!r}", file=sys.stderr, flush=True)
            on_pick(None)
            return
        self._choice_cards[prompt_id] = card
        self.model.card_opened()
        if origin == "voice":
            self._voice_announce("ask", f"{title} Say {', '.join(labels[:-1])} or {labels[-1]}.")

    def task_choice_closed(self, prompt_id: str) -> None:
        card = self._choice_cards.pop(prompt_id, None)
        if card is None:
            return
        try:
            card.close()
        except Exception:
            pass
        self.model.card_closed()
        self._settle_after_card()

    def task_undo(self, label: str, seconds: float, on_undo: Callable[[], None],
                  on_elapse: Callable[[], None]) -> None:
        """The few seconds to take an approved send back."""
        self._undo_strips = [strip for strip in self._undo_strips if not strip.closed]
        try:
            from xenon.orb_cards import UndoStrip

            strip = UndoStrip(self.root, seconds, on_undo, on_elapse, self._task_anchor(), label=label)
        except Exception as error:
            print(f"undo strip failed: {error!r}", file=sys.stderr, flush=True)
            on_undo()  # no grace period shown means no send
            return
        self._undo_strips.append(strip)

    def task_stopped(self) -> None:
        """After Stop: undo any countdown and say so in the notch."""
        strips, self._undo_strips = self._undo_strips, []
        for strip in strips:
            try:
                strip.undo()
            except Exception:
                pass
        self._notch_flash = ("Stopped", time.monotonic() + 1.5)
        if self.notch:
            self.model.notch_info = self._notch_info()

    def stop_task(self) -> None:
        """Ctrl+Alt+Esc and the tray's Stop task. Does nothing when idle."""
        if self.tasks is None or not self.tasks.has_task():
            return
        self.tasks.stop("stopped by you")

    def task_update(self, update, origin: str = "typed") -> None:
        view = task_display(update, origin)
        self._task_text = view.notch
        self._answer_placeholder = view.placeholder
        if self.notch:
            self.model.notch_info = self._notch_info()
        if view.state == "working":
            if not self._busy and not self._cards and self.model.state in TASK_OVERRIDABLE_STATES:
                self.model.set_state("working", view.notch)
        elif view.state == "rest":
            if not self._cards and self.model.state in {"working", "confirm"}:
                self.model.set_state(self._rest_state())
        if view.title:
            self._show_task_panel(view)
        if view.speak:
            self._voice_announce(*view.speak)

    def _show_task_panel(self, view: TaskDisplay) -> None:
        if view.history:
            self._history.append((view.title, view.body))
            # Tasks read the screen to plan: the tablet shows "Private".
            self._history_private = True
        if self.panel.chat_mode and self.panel.visible:
            self._render_chat()
            self._place_panel()
            return
        previous = 0
        try:
            previous = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            pass
        self.panel.set_chat(False)
        self.panel.clear()
        self.panel.set_header(view.title, view.note, view.accent)
        self.panel.append(view.body)
        self._place_panel()
        self._keep_focus(previous)

    def _keep_focus(self, previous: int) -> None:
        """Tk's show call can activate the panel; a result that arrives while
        he types elsewhere must not take his keystrokes."""
        try:
            user32 = ctypes.windll.user32
            if (previous and previous not in self._own_hwnds_safe()
                    and user32.GetForegroundWindow() != previous and user32.IsWindow(previous)):
                user32.SetForegroundWindow(previous)
        except Exception:
            pass

    def show_recent_actions(self) -> None:
        """Tray "Recent actions": the last 20 audit rows (no bodies, ever)."""
        rows: list[dict] = []
        error = ""
        if self.tasks is not None:
            try:
                rows = self.tasks.audit.recent(20)
            except Exception as failure:
                error = f"could not read the audit log: {failure}"
        self.panel.set_chat(False)
        self.panel.clear()
        self.panel.set_header("Recent actions", f"{len(rows)} entries", self._accent())
        lines = audit_lines(rows)
        if error or not lines:
            self.panel.append(error or "No approvals or commits yet.", "dim")
        else:
            self.panel.append("\n".join(lines), "dim")
        self._place_panel()

    def _voice_announce(self, kind: str, text: str) -> None:
        """Queue words for the voice thread (results and questions only)."""
        say = self._voice_say
        if say is not None and self.model.voice_on and (text or "").strip():
            say.put((kind, text))

    # ----- voice --------------------------------------------------------

    def _toggle_voice(self) -> None:
        if self._voice_runner is None:
            self._show_error("Voice needs the voice extras: pip install -e \".[voice]\"")
            return
        if self._voice_stop is not None:
            self._voice_stop.set()
            self._voice_stop = None
            self._voice_say = None
            self.model.voice_on = False
            self.model.set_state(self._rest_state())
            return
        stop = threading.Event()
        self._voice_stop = stop
        self._listen_event = threading.Event()
        listen_event = self._listen_event
        self.model.voice_on = True
        self.model.set_state("thinking", "loading voice")

        def apply(state: str, detail: str) -> None:
            if stop.is_set():
                return
            # Hold the "got it" flash long enough to see, even when the next
            # state (thinking, or a fast-path reply) arrives right after it.
            hold = HEARD_HOLD_S - self.model.state_age
            if self.model.state == "heard" and state != "heard" and hold > 0:
                self.root.after(int(hold * 1000) + 1, lambda: apply(state, detail))
                return
            if state == "standby" and (self._cards or (self.tasks is not None and self.tasks.busy)):
                # Back to waiting for the name, but a task is still running
                # (or a card is up): keep showing that instead.
                state, detail = ("confirm" if self._cards else "working"), self._task_text
                if state == self.model.state:
                    return
            self.model.set_state(state, detail)

        def on_state(state: str, detail: str) -> None:
            self.post(lambda: apply(state, detail))

        def on_level(level: float) -> None:
            self.model.set_level(level)  # a float write; safe without the queue

        def on_exchange(you: str, answer: str, private: bool = False) -> None:
            def show() -> None:
                self._history.append((you, answer))
                # Built from the screen or memory: the tablet shows "Private".
                self._history_private = bool(private)
                if self.panel.chat_mode and self.panel.visible:
                    self._render_chat()
            self.post(show)

        # Task hand-off for the voice thread. "do ..." starts a task (Xenon
        # says "On it."), "stop" stops it, and a waiting task's answer goes
        # to it. A wake-less "voice-focus" utterance can only answer a
        # waiting task; nothing spoken can approve a card (voice approvals
        # stay off: VoiceHooks.confirm is None).
        tasks = self.tasks
        say_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._voice_say = say_queue

        def on_task(text: str, origin: str) -> str | None:
            routed = tasks.route(text, origin)
            if routed:
                self.post(lambda: self._task_ack(routed, text, typed=False))
            return routed

        def focus_listen() -> bool:
            return tasks.waiting_for_input or bool(tasks.open_choices())

        hooks = VoiceHooks(
            on_state, on_level, on_exchange, listen_event,
            task=on_task if tasks is not None else None,
            focus_listen=focus_listen if tasks is not None else None,
            say=say_queue,
        )

        def run() -> None:
            try:
                self._voice_runner(hooks, stop)
            except Exception as error:
                message = str(error)

                def fail() -> None:
                    self._voice_stop = None
                    self._voice_say = None
                    self.model.voice_on = False
                    self._show_error(f"voice stopped: {message}")
                self.post(fail)

        threading.Thread(target=run, daemon=True, name="xenon-orb-voice").start()

    # ----- lifecycle ----------------------------------------------------

    # ----- music widget -------------------------------------------------

    def music_widget_running(self) -> bool:
        return self.music_widget is not None and self.music_widget.running

    def toggle_music_widget(self) -> None:
        """Tray switch: start/stop the widget, and remember the choice so the
        next orb start does the same."""
        from xenon import music_settings as ms

        want = not self.music_widget_running()
        try:
            ms.save(ms.coerce({**ms.asdict(ms.load()), "start_with_orb": want}))
        except OSError:
            pass
        if want:
            start_music_widget_async(self)
        elif self.music_widget is not None:
            widget = self.music_widget
            threading.Thread(target=widget.stop, daemon=True, name="xenon-music-stop").start()

    def open_music_settings(self) -> None:
        window = self._music_settings_window
        if window is not None:
            try:
                window.top.deiconify()
                window.top.lift()
                window.top.focus_force()
                return
            except tk.TclError:
                self._music_settings_window = None
        from xenon.music_settings_ui import SettingsWindow

        self._music_settings_window = SettingsWindow(self.root)

    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.music_widget is not None:
            try:
                self.music_widget.stop()
            except Exception:
                pass
        if self._voice_stop is not None:
            self._voice_stop.set()
        if self.tasks is not None:
            # Stop the task, deny its cards (nothing approves on the way
            # out), close the pipe and remove its key file.
            try:
                self.tasks.close()
            except Exception:
                pass
        for card in [*self._cards.values(), *self._choice_cards.values(), *self._undo_strips]:
            try:
                card.close()
            except Exception:
                pass
        if self._hotkeys is not None:
            self._hotkeys.stop()
        self._timers.unlisten(self._on_timer_thread)
        self.dimmer.clear()
        if self._companion is not None:
            try:
                self._companion.stop()
            except Exception:
                pass
        if self._tray is not None:
            self._tray.stop()
        save_position(self.x, self.y)
        # Vanish first, then clean up: closing should feel instant.
        try:
            self.root.withdraw()
        except tk.TclError:
            pass
        self._worker.stop(timeout=1.0)
        self.window.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.quit()
