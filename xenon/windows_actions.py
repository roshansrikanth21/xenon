"""Windows implementation of the action backend.

Every one of these calls changes the desktop, so they only run after the
approval runner allows them. Shell and destructive kinds are rejected by the
policy before they reach this module.

The approval a plan got is not trusted blindly at execution time: the
foreground can change between planning and acting, and the legacy kinds
(type_text, press_keys, click_control) land wherever focus is. So before
each of those the backend re-reads the LIVE foreground (process image,
title, focused element, browser URL), classifies the action against it,
and refuses DENIED surfaces outright and OUTWARD/DESTRUCTIVE results unless
this instance was built by the gate for an approved commit.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from xenon.actions import (
    Action,
    ActionContext,
    ActionResult,
    Risk,
    Verification,
    classify,
)
from xenon.windows import Win32Backend

# Legacy kinds that act on whatever has focus, so they are classified
# against the live foreground before they run.
GUARDED_KINDS = frozenset({"type_text", "press_keys", "click_control"})
# Kinds whose risk does not depend on the foreground but can still name a
# denied target: a URI handler or UNC path (open_app launches "://" targets
# through os.startfile), a console window, a .ps1 or a Startup-folder file.
# They are classified against the task facts alone, so a caller that skipped
# ApprovalPolicy still cannot reach those targets through this backend.
STATIC_GUARDED_KINDS = frozenset({"open_app", "focus_window", "write_file"})

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
LANG_ENGLISH = 0x09


class TargetRefused(RuntimeError):
    """The live target may never be acted on (for example Xenon itself)."""

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


# ---------------------------------------------------------------------------
# Fast Win32 window-title scan
# ---------------------------------------------------------------------------
# ``pywinauto``'s ``Desktop(...).windows()`` walks UI Automation, which is
# slow enough (hundreds of ms per call) that polling for a freshly launched
# window dominated the ``open_app`` path. Top-level title enumeration through
# ``EnumWindows`` is a few milliseconds, so we use it for launch polling and
# desktop verification and only fall back to UI Automation when needed.

_FAST_USER32 = None
_ENUMERATED_TITLES: list[str] = []


def _load_fast_user32():
    global _FAST_USER32
    if _FAST_USER32 is not None:
        return _FAST_USER32
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    ]
    enum_windows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    _FAST_USER32 = user32
    return user32


def _enum_cb(hwnd, _lparam) -> bool:
    user32 = _FAST_USER32
    if user32 is None or not user32.IsWindowVisible(hwnd):
        return True
    try:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title:
            _ENUMERATED_TITLES.append(title)
    except Exception:
        return True
    return True


def list_window_titles(title_scanner=None) -> list[str]:
    """Return visible top-level window titles using Win32, or ``[]``.

    On non-Windows or when Win32 is unavailable this returns an empty list so
    callers can fall back to UI Automation. ``title_scanner`` is injectable
    for tests.
    """
    if title_scanner is not None:
        return list(title_scanner())
    if sys.platform != "win32":
        return []
    try:
        user32 = _load_fast_user32()
        _ENUMERATED_TITLES.clear()
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(
            _enum_cb
        )
        user32.EnumWindows(callback, 0)
        return list(_ENUMERATED_TITLES)
    except Exception:
        return []


def find_window_title(needle: str, title_scanner=None) -> str | None:
    """Return the first top-level title containing ``needle``, or ``None``."""
    lowered = (needle or "").lower()
    if not lowered:
        return None
    for title in list_window_titles(title_scanner):
        if lowered in title.lower():
            return title
    return None


# ---------------------------------------------------------------------------
# Live foreground facts for the classifier
# ---------------------------------------------------------------------------
# Each reader is a plain function so tests (and non-Windows CI) can replace
# it; none of them runs at import time.


def window_process_id(hwnd: int) -> int:
    """PID owning ``hwnd`` via GetWindowThreadProcessId, or 0."""
    if sys.platform != "win32" or not hwnd:
        return 0
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def process_image_path(pid: int) -> str:
    """Full image path of ``pid`` via QueryFullProcessImageNameW, or ""."""
    if sys.platform != "win32" or pid <= 0:
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def frame_host_child_pid(hwnd: int, host_pid: int) -> int:
    """PID of the app inside an ApplicationFrameHost window, or 0.

    UWP/WinUI apps (Calculator, Settings, some Store apps) put their content
    in a child window owned by the real process; the top-level frame belongs
    to ApplicationFrameHost.exe and says nothing about the app.
    """
    if sys.platform != "win32" or not hwnd:
        return 0
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
        found: list[int] = []

        def visit(child, _lparam) -> bool:
            pid = window_process_id(child)
            if pid and pid != host_pid:
                found.append(pid)
                return False
            return True

        user32.EnumChildWindows(hwnd, callback_type(visit), 0)
        return found[0] if found else 0
    except Exception:
        return 0


def read_focused_element() -> tuple[str, str]:
    """``(control_type, name)`` of the UIA focused element, or ``("", "")``.

    pywinauto is imported here, not at module load, so idle Xenon pays
    nothing for UI Automation.
    """
    try:
        from pywinauto.uia_defines import IUIA

        uia = IUIA()
        element = uia.iuia.GetFocusedElement()
        role = uia.known_control_type_ids.get(element.CurrentControlType, "")
        return str(role or ""), str(element.CurrentName or "")
    except Exception:
        return "", ""


def read_focused_value() -> str | None:
    """Text of the UIA focused control (Value, then Text pattern), or ``None``.

    This is what Enter or a Send button would send from a composer, so it
    is read back for the card and the digest. ``None`` means "could not
    read", which callers treat as "refuse", never as "empty".
    """
    try:
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        element = IUIA().iuia.GetFocusedElement()
        wrapper = UIAWrapper(UIAElementInfo(element))
    except Exception:
        return None
    try:
        value = wrapper.iface_value.CurrentValue
        if value is not None:
            return str(value)
    except Exception:
        pass
    try:
        return str(wrapper.iface_text.DocumentRange.GetText(-1) or "")
    except Exception:
        return None


def ui_language_is_english() -> bool:
    """True when the Windows UI language is English. Unknown counts as not."""
    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        lang = int(kernel32.GetUserDefaultUILanguage())
        return (lang & 0x3FF) == LANG_ENGLISH
    except Exception:
        return False


class WindowsActionBackend:
    """Executes approved actions with Win32 and UI Automation.

    ``allow_outward`` / ``allow_destructive`` default to False: an ordinary
    instance refuses any guarded action that classifies OUTWARD or
    DESTRUCTIVE against the live foreground, whatever approval the plan got
    upstream. Only gate.py builds an instance with them set, after consuming
    an approval token. DENIED surfaces are refused by every instance, and
    open_app / focus_window / write_file targets that classify DENIED (URI
    handlers, UNC paths, consoles, script files) are refused without a
    foreground read.

    ``context_reader(action) -> ActionContext`` replaces the live foreground
    read (tests, or a caller that already resolved the target).
    ``task_context`` carries the task-level facts the foreground cannot know:
    taint, Roshan's words, the app allowlist and the request origin.
    """

    def __init__(
        self,
        backend=None,
        desktop_factory=None,
        catalog=None,
        title_scanner=None,
        allow_outward: bool = False,
        allow_destructive: bool = False,
        context_reader: Callable[[Action], ActionContext] | None = None,
        task_context: ActionContext | None = None,
        focus_reader: Callable[[], tuple[str, str]] | None = None,
        url_reader: Callable[[int], str | None] | None = None,
        context_check: Callable[[ActionContext], str | None] | None = None,
        value_reader: Callable[[], str | None] | None = None,
        settings_reader: Callable[[int], str | None] | None = None,
    ) -> None:
        self._backend = backend if backend is not None else Win32Backend()
        self._desktop_factory = (
            desktop_factory if desktop_factory is not None else _default_desktop
        )
        self._catalog = catalog
        self._title_scanner = title_scanner
        self._focused_title: str | None = None
        self._user32 = None
        self._allow_outward = bool(allow_outward)
        self._allow_destructive = bool(allow_destructive)
        self._context_reader = context_reader
        self._task_context = task_context if task_context is not None else ActionContext()
        self._focus_reader = focus_reader if focus_reader is not None else read_focused_element
        self._url_reader = url_reader
        self._value_reader = value_reader if value_reader is not None else read_focused_value
        self._settings_reader = settings_reader
        self._ui_english: bool | None = None
        # The gate passes a check that compares the live target with what
        # the card showed (the approved digest); ``expect_context`` sets a
        # one-shot one for a single carded LOCAL step.
        self._context_check = context_check
        self._next_check: Callable[[ActionContext], str | None] | None = None
        # The foreground window a guarded action was classified against;
        # input is only sent while that window is still in front.
        self._last_handle: int | None = None
        self._pinned_handle: int | None = None

    # -- live classification -------------------------------------------------

    def _foreground_pid(self, handle: int) -> int:
        reader = getattr(self._backend, "process_id", None)
        if callable(reader):
            try:
                return int(reader(handle))
            except Exception:
                return 0
        return window_process_id(handle)

    def _image_for(self, pid: int) -> str:
        if pid <= 0:
            return ""
        reader = getattr(self._backend, "process_name", None)
        if callable(reader):
            try:
                return str(reader(pid) or "")
            except Exception:
                return ""
        return process_image_path(pid)

    def _read_url(self, handle: int) -> str:
        try:
            if self._url_reader is not None:
                return self._url_reader(handle) or ""
            from xenon.surfaces import read_browser_url

            return read_browser_url(handle) or ""
        except Exception:
            return ""  # unreadable URL: the classifier treats it as comms

    def live_context(self, action: Action) -> ActionContext:
        """Describe where ``action`` would land right now.

        Raises ``TargetRefused`` when the foreground is Xenon's own process.
        Usable as ``TaskRunner(context_provider=backend.live_context)``.
        """
        from xenon import surfaces

        handle = self._backend.foreground_handle()
        self._last_handle = handle
        title = self._backend.title(handle)
        pid = self._foreground_pid(handle)
        if pid and pid == os.getpid():
            raise TargetRefused("Xenon never targets its own windows")
        image = self._image_for(pid)
        if surfaces.norm_image(image) == "applicationframehost.exe":
            child = frame_host_child_pid(handle, pid)
            if child and child != os.getpid():
                image = self._image_for(child) or image
        if surfaces.norm_image(image) in {surfaces.norm_image(i) for i in surfaces.SETTINGS_IMAGES}:
            # Every Settings page is titled "Settings": name the page, or
            # mark it unknown (which the surface rules deny).
            title = surfaces.settings_title(title, self._read_settings_page(handle))
        role, name = ("", "")
        try:
            role, name = self._focus_reader()
        except Exception:
            role, name = ("", "")
        url = self._read_url(handle) if surfaces.is_browser_image(image) else ""
        if self._ui_english is None:
            self._ui_english = ui_language_is_english()
        kind = action.kind.strip().lower()
        if kind == "click_control":
            element_name, element_role = action.target, action.value
        else:
            element_name, element_role = name, role
        task = self._task_context
        return ActionContext(
            process_image=image,
            window_title=title,
            focused_role=role,
            element_name=element_name,
            element_role=element_role,
            url=url,
            ui_language_english=self._ui_english,
            tainted=task.tainted,
            user_words=task.user_words,
            allowlist=task.allowlist,
            origin=task.origin,
        )

    def _read_settings_page(self, handle: int) -> str | None:
        try:
            if self._settings_reader is not None:
                return self._settings_reader(handle)
            from xenon.surfaces import read_settings_page

            return read_settings_page(handle)
        except Exception:
            return None

    def composer_readback(self, action: Action | None = None) -> dict:
        """What Enter or a Send click would commit from the focused control.

        ``{"chat": <focused control's name>, "role": ..., "text": <its text>}``.
        In WhatsApp the composer is named "Type a message to <chat>", so the
        name says who it goes to. ``text`` is ``None`` when it could not be
        read; the caller refuses then, because unknown is not "empty".
        """
        try:
            role, name = self._focus_reader()
        except Exception:
            role, name = "", ""
        try:
            text = self._value_reader()
        except Exception:
            text = None
        return {"chat": str(name or ""), "role": str(role or ""), "text": text}

    def expect_context(self, check: Callable[[ActionContext], str | None] | None) -> None:
        """A one-shot check for the next guarded action: ``check(live_ctx)``
        returns a refusal reason when the target is no longer the one a card
        showed, so a step approved for Notepad never types into Chrome."""
        self._next_check = check

    def _guard(self, action: Action) -> ActionResult | None:
        """Refuse a guarded action the live classification does not allow."""
        check = self._next_check or self._context_check
        self._next_check = None
        self._last_handle = None
        try:
            if self._context_reader is not None:
                ctx = self._context_reader(action)
            else:
                ctx = self.live_context(action)
        except TargetRefused as refusal:
            return ActionResult(False, f"refused: {refusal}")
        except Exception as error:
            return ActionResult(
                False,
                f"refused: could not read the target window ({type(error).__name__}); nothing was sent",
            )
        if ctx is None or not (ctx.process_image or "").strip():
            return ActionResult(
                False, "refused: could not identify the foreground app; nothing was sent"
            )
        if check is not None:
            try:
                changed = check(ctx)
            except Exception as error:
                changed = f"could not compare the target ({type(error).__name__})"
            if changed:
                return ActionResult(False, f"refused: {changed}; nothing was sent")
        refused = self._refusal_for(action, ctx)
        if refused is None:
            # Input goes only to the window this was classified against.
            self._pinned_handle = self._last_handle
        return refused

    def _require_pinned_foreground(self) -> ActionResult | None:
        """Refuse when the foreground changed since the live classification."""
        pinned = self._pinned_handle
        if pinned is None:
            return None
        try:
            current = self._backend.foreground_handle()
        except Exception as error:
            return ActionResult(False, f"could not read the foreground window: {error}")
        if current != pinned:
            return ActionResult(False, "the foreground window changed after the check; nothing was sent")
        return None

    def _static_guard(self, action: Action) -> ActionResult | None:
        """Refuse open/focus/write targets that are denied whatever has focus.

        No foreground read: these kinds land on a named app or path, so the
        task facts (origin, taint, allowlist, his words) are all it needs.
        """
        return self._refusal_for(action, self._task_context)

    def _refusal_for(self, action: Action, ctx: ActionContext) -> ActionResult | None:
        risk, reason = classify(action, ctx)
        if risk is Risk.DENIED:
            return ActionResult(False, f"refused: {reason}")
        if risk is Risk.OUTWARD and not self._allow_outward:
            return ActionResult(
                False,
                f"refused: this would send or commit ({reason}); it needs a confirm card on the orb",
            )
        if risk is Risk.DESTRUCTIVE and not self._allow_destructive:
            return ActionResult(
                False,
                f"refused: this looks destructive ({reason}); it needs a confirm card on the orb",
            )
        return None

    def _app_catalog(self):
        if self._catalog is None:
            from xenon.apps import default_catalog

            self._catalog = default_catalog()
        return self._catalog

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
        kind = action.kind.strip().lower()
        handler = getattr(self, f"_do_{kind}", None)
        if handler is None:
            return ActionResult(False, f"unsupported action: {action.kind}")
        try:
            refused = None
            if kind in GUARDED_KINDS:
                refused = self._guard(action)
            elif kind in STATIC_GUARDED_KINDS:
                refused = self._static_guard(action)
            if refused is not None:
                return refused
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

        # Prefer the app catalog: most modern apps are Start-menu entries with
        # an app id, so os.startfile("chrome") cannot find them even though
        # they are installed. The task's origin picks the resolve rules: model,
        # voice and IPC words may only name catalog apps.
        from xenon.apps import TRUSTED_ORIGINS

        origin = (self._task_context.origin or "model").strip().lower()
        catalog = self._app_catalog()
        try:
            resolved = catalog.resolve(target, origin=origin)
        except TypeError:  # a catalog without origin rules: trusted words only
            resolved = catalog.resolve(target) if origin in TRUSTED_ORIGINS else None
        launch_detail = ""
        if resolved is not None:
            try:
                from xenon.apps import launch

                launch_detail = launch(resolved)
            except OSError as error:
                return ActionResult(False, f"could not launch {target!r}: {error}")
        elif origin not in TRUSTED_ORIGINS:
            # ShellExecute on an arbitrary name searches PATH and the current
            # directory and runs URI handlers: never for untrusted words.
            return ActionResult(
                False, f"refused: {target!r} is not an app Xenon knows (model and voice requests "
                "may only open catalog apps)"
            )
        else:
            path = Path(target).expanduser()
            try:
                if path.exists() or "://" in target:
                    os.startfile(str(path) if path.exists() else target)
                else:
                    # ShellExecute resolves some names a bare Popen cannot.
                    try:
                        os.startfile(target)
                    except OSError:
                        subprocess.Popen([target], shell=False)
            except OSError as error:
                return ActionResult(False, f"could not launch {target!r}: {error}")
            launch_detail = f"launched {target}"

        # Give the process a moment to create its window, so a following
        # focus_window step does not race the launcher. ``value`` can carry the
        # expected title fragment (for example "Calculator" for ``calc``).
        # Poll the cheap Win32 title list first; only fall back to UI
        # Automation when Win32 returned nothing (e.g. in tests / CI).
        deadline = time.monotonic() + 5.0
        expected = action.value or (resolved.name if resolved else "") or Path(target).stem or target
        needle = expected.lower()
        while time.monotonic() < deadline:
            titles = list_window_titles(self._title_scanner)
            for title in titles:
                if needle and needle in title.lower():
                    return ActionResult(True, f"{launch_detail}; window {title!r} exists")
            if titles:
                # Win32 can see the desktop; it is authoritative. Keep polling
                # it instead of paying for a UI Automation walk each cycle.
                time.sleep(0.1)
                continue
            try:
                desktop = self._desktop_factory()
                for window in desktop.windows():
                    title = (getattr(window, "window_text", lambda: "")() or "").lower()
                    if needle and needle in title:
                        detail = f"{launch_detail}; window {title!r} exists"
                        return ActionResult(True, detail)
            except Exception:
                pass
            time.sleep(0.1)
        return ActionResult(True, launch_detail)

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
        refused = self._require_expected_app(action)
        if refused is not None:
            return refused

        self._focused_title = title
        return ActionResult(True, f"focused {title}")

    def _require_expected_app(self, action: Action) -> ActionResult | None:
        """A title match is not enough: "Notepad" must land on notepad.exe,
        not on a Discord channel named "#notepad". When the target names a
        known app, the foreground image must be one that app runs as."""
        from xenon.surfaces import expected_images, norm_image

        expected = expected_images(action.target)
        if not expected:
            return None
        try:
            if self._context_reader is not None:
                ctx = self._context_reader(action)
            else:
                ctx = self.live_context(action)
            image = norm_image(getattr(ctx, "process_image", "") or "")
        except Exception:
            image = ""
        if image in expected:
            return None
        shown = image or "an app that could not be identified"
        return ActionResult(
            False, f"focus landed on {shown}, which is not {action.target!r}; nothing else was done"
        )

    def _do_click_control(self, action: Action) -> ActionResult:
        refused = self._require_pinned_foreground()
        if refused is not None:
            return refused
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
        from xenon.untrusted import strip_invisible

        if not action.value:
            return ActionResult(False, "type_text needs a value")
        refused = self._require_the_focused_window() or self._require_pinned_foreground()
        if refused is not None:
            return refused
        # Exactly the text a card shows (cards render through
        # strip_invisible), never hidden characters on top of it.
        text = strip_invisible(action.value)
        if not text:
            return ActionResult(False, "type_text has nothing visible to type")
        self._send_unicode(text)
        return ActionResult(True, f"typed {len(text)} characters")

    def _do_press_keys(self, action: Action) -> ActionResult:
        refused = self._require_the_focused_window() or self._require_pinned_foreground()
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

    def __init__(self, expected: str, desktop_factory=None, title_scanner=None) -> None:
        self.expected = expected
        self._desktop_factory = (
            desktop_factory if desktop_factory is not None else _default_desktop
        )
        self._title_scanner = title_scanner
        self._injected_desktop = desktop_factory is not None

    def verify(self, goal: str) -> Verification:
        expected = self.expected.lower()
        if not self._injected_desktop:
            try:
                titles = list_window_titles(self._title_scanner)
                for title in titles:
                    if expected in title.lower():
                        return Verification(True, f"window {title!r} exists")
                if titles:
                    return Verification(
                        False, f"no window title contains {self.expected!r}"
                    )
            except Exception:
                pass
        try:
            desktop = self._desktop_factory()
            windows = desktop.windows()
            for window in windows:
                title = (getattr(window, "window_text", lambda: "")() or "")
                if expected in title.lower():
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
