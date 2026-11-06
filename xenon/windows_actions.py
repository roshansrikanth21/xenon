"""Windows implementation of the action backend.

Every one of these calls changes the desktop, so they only run after the
approval runner allows them. Shell and destructive kinds are rejected by the
policy before they reach this module.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from xenon.actions import Action, ActionResult, Verification
from xenon.windows import Win32Backend

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MODIFIER_KEYS = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "windows": 0x5B,
}

NAMED_KEYS = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}

ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


def virtual_key(name: str) -> int:
    """Map a key name such as ``enter``, ``f5``, or ``a`` to a virtual key."""
    lowered = name.strip().lower()
    if lowered in NAMED_KEYS:
        return NAMED_KEYS[lowered]
    if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", lowered):
        return 0x6F + int(lowered[1:])
    if len(lowered) == 1 and (lowered.isalnum() or lowered in "`-=[]\\;',./"):
        return ord(lowered.upper())
    raise ValueError(f"unsupported key: {name}")


def parse_keys(sequence: str) -> tuple[list[int], int]:
    """Return ``(modifier_keys, key)`` for a sequence like ``ctrl+shift+s``."""
    parts = [part.strip() for part in sequence.split("+") if part.strip()]
    if not parts:
        raise ValueError("empty key sequence")
    modifiers = [MODIFIER_KEYS[part.lower()] for part in parts[:-1] if part.lower() in MODIFIER_KEYS]
    if len(modifiers) != len(parts) - 1:
        unknown = [part for part in parts[:-1] if part.lower() not in MODIFIER_KEYS]
        raise ValueError(f"unsupported modifiers: {', '.join(unknown)}")
    return modifiers, virtual_key(parts[-1])


def _default_desktop():
    from pywinauto import Desktop

    return Desktop(backend="uia")


class WindowsActionBackend:
    """Executes approved actions with Win32 and UI Automation."""

    def __init__(self, backend=None, desktop_factory=None) -> None:
        self._backend = backend if backend is not None else Win32Backend()
        self._desktop_factory = (
            desktop_factory if desktop_factory is not None else _default_desktop
        )
        self._focused_title: str | None = None
        self._user32 = None

    @property
    def user32(self):
        """Load user32 only when a key is actually sent.

        Deferring this keeps the class importable and testable on CI machines
        that have no Win32 library.
        """
        if self._user32 is None:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.user32.SendInput.argtypes = [
                wintypes.UINT,
                ctypes.POINTER(INPUT),
                ctypes.c_int,
            ]
            self.user32.SendInput.restype = wintypes.UINT
            self.user32.keybd_event.argtypes = [
                wintypes.BYTE,
                wintypes.BYTE,
                wintypes.DWORD,
                ULONG_PTR,
            ]
        return self._user32

    def execute(self, action: Action) -> ActionResult:
        handler = getattr(self, f"_do_{action.kind.strip().lower()}", None)
        if handler is None:
            return ActionResult(False, f"unsupported action: {action.kind}")
        try:
            return handler(action)
        except Exception as error:
            return ActionResult(False, f"{type(error).__name__}: {error}")

    def _do_observe(self, action: Action) -> ActionResult:
        backend = Win32Backend()
        handle = backend.foreground_handle()
        return ActionResult(True, f"foreground: {backend.title(handle)}")

    def _do_wait(self, action: Action) -> ActionResult:
        try:
            seconds = min(max(float(action.value or "1"), 0.0), 10.0)
        except ValueError:
            seconds = 1.0
        time.sleep(seconds)
        return ActionResult(True, f"waited {seconds:.1f}s")

    def _do_open_app(self, action: Action) -> ActionResult:
        target = action.target.strip()
        if not target:
            return ActionResult(False, "open_app needs a target")
        path = Path(target).expanduser()
        if path.exists() or "://" in target:
            os.startfile(str(path) if path.exists() else target)
        else:
            # ShellExecute resolves Start-menu app names and App Paths entries
            # that a bare Popen cannot find. Fall back for executables on PATH.
            try:
                os.startfile(target)
            except OSError:
                subprocess.Popen([target], shell=False)

        # Give the process a moment to create its window, so a following
        # focus_window step does not race the launcher. ``value`` can carry the
        # expected title fragment (for example "Calculator" for ``calc``).
        deadline = time.monotonic() + 5.0
        needle = (action.value or Path(target).stem or target).lower()
        while time.monotonic() < deadline:
            try:
                desktop = self._desktop_factory()
                for window in desktop.windows():
                    title = (getattr(window, "window_text", lambda: "")() or "").lower()
                    if needle and needle in title:
                        return ActionResult(True, f"opened {target}")
            except Exception:
                pass
            time.sleep(0.25)
        return ActionResult(True, f"opened {target}")

    def _do_focus_window(self, action: Action) -> ActionResult:
        target = action.target.strip()
        if not target:
            return ActionResult(False, "focus_window needs a target")

        self._focused_title = None
        desktop = self._desktop_factory()
        needle = target.lower()
        window = None

        windows_method = getattr(desktop, "windows", None)
        if callable(windows_method):
            for candidate in windows_method():
                title = (getattr(candidate, "window_text", lambda: "")() or "").lower()
                if needle not in title:
                    continue
                try:
                    candidate.set_focus()
                except Exception:
                    continue
                window = candidate
                break
        else:
            for build in (
                lambda: desktop.window(title=target),
                lambda: desktop.window(title_re=f"(?i).*{re.escape(target)}.*"),
            ):
                try:
                    candidate = build()
                    candidate.set_focus()
                except Exception:
                    continue
                window = candidate
                break

        if window is None:
            return ActionResult(False, f"no window matching {target!r}")

        title = self._foreground_title()
        if target.lower() not in title.lower():
            return ActionResult(
                False, f"focus landed on {title!r}, which is not {target!r}"
            )

        self._focused_title = title
        return ActionResult(True, f"focused {title}")

    def _do_click_control(self, action: Action) -> ActionResult:
        handle = self._backend.foreground_handle()
        window = self._desktop_factory().window(handle=handle)
        kwargs = {"title": action.target}
        if action.value.strip():
            kwargs["control_type"] = action.value.strip()
        control = window.child_window(**kwargs, found_index=0)
        control.click_input()
        return ActionResult(True, f"clicked {action.target}")

    def _require_the_focused_window(self) -> ActionResult | None:
        """Refuse to send input if the foreground window changed since focus."""
        if self._focused_title is None:
            return None
        try:
            current = self._foreground_title()
        except Exception as error:
            return ActionResult(False, f"could not read the foreground window: {error}")
        if current != self._focused_title:
            return ActionResult(
                False,
                f"foreground window changed to {current!r}; nothing was typed",
            )
        return None

    def _foreground_title(self) -> str:
        return self._backend.title(self._backend.foreground_handle())

    def _do_type_text(self, action: Action) -> ActionResult:
        if not action.value:
            return ActionResult(False, "type_text needs a value")
        refused = self._require_the_focused_window()
        if refused is not None:
            return refused
        self._send_unicode(action.value)
        return ActionResult(True, f"typed {len(action.value)} characters")

    def _do_press_keys(self, action: Action) -> ActionResult:
        refused = self._require_the_focused_window()
        if refused is not None:
            return refused
        modifiers, key = parse_keys(action.value)
        for modifier in modifiers:
            self.user32.keybd_event(modifier, 0, 0, 0)
            time.sleep(0.01)
        self.user32.keybd_event(key, 0, 0, 0)
        time.sleep(0.01)
        self.user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
        for modifier in reversed(modifiers):
            self.user32.keybd_event(modifier, 0, KEYEVENTF_KEYUP, 0)
        return ActionResult(True, f"pressed {action.value}")

    def _do_write_file(self, action: Action) -> ActionResult:
        path = Path(action.target).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(action.value, encoding="utf-8")
        return ActionResult(True, f"wrote {path}")

    def _send_unicode(self, text: str) -> None:
        units = text.encode("utf-16-le")
        events = []
        for index in range(0, len(units), 2):
            code = int.from_bytes(units[index : index + 2], "little")
            events.append(
                INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
            )
            events.append(
                INPUT(
                    type=INPUT_KEYBOARD,
                    u=INPUTUNION(
                        ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
                    ),
                )
            )
        array = (INPUT * len(events))(*events)
        sent = self.user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
        if sent != len(events):
            raise OSError(ctypes.get_last_error(), "SendInput did not send every event")


class DesktopTitleVerifier:
    """Considers a task verified when any desktop window title matches.

    Launching an app does not always bring it to the foreground (Windows may
    open it behind the current window), so ``open_app`` verifies against the
    whole desktop rather than only the foreground window.
    """

    def __init__(self, expected: str, desktop_factory=None) -> None:
        self.expected = expected
        self._desktop_factory = (
            desktop_factory if desktop_factory is not None else _default_desktop
        )

    def verify(self, goal: str) -> Verification:
        try:
            desktop = self._desktop_factory()
            windows = desktop.windows()
            for window in windows:
                title = (getattr(window, "window_text", lambda: "")() or "")
                if self.expected.lower() in title.lower():
                    return Verification(True, f"window {title!r} exists")
        except Exception as error:
            return Verification(False, f"could not list desktop windows: {error}")
        return Verification(False, f"no window title contains {self.expected!r}")


class ForegroundTitleVerifier:
    """Considers a task verified when the foreground title contains a string."""

    def __init__(self, expected: str, backend: Win32Backend | None = None) -> None:
        self.expected = expected
        self._backend = backend if backend is not None else Win32Backend()

    def verify(self, goal: str) -> Verification:
        try:
            handle = self._backend.foreground_handle()
            title = self._backend.title(handle)
        except Exception as error:
            return Verification(False, f"could not read the foreground window: {error}")
        if self.expected.lower() in title.lower():
            return Verification(True, f"foreground title matched {self.expected!r}")
        return Verification(False, f"expected {self.expected!r} in title {title!r}")
