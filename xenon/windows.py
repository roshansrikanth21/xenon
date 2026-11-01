"""Live Windows perception adapter: foreground window, UIA controls, capture.

This module is deliberately read-only. It observes the active window; it never
moves the mouse, sends keys, or modifies UI state. Acting on the desktop is a
separate component that must pass through the approval policy.

UI Automation is optional. Without ``pywinauto`` the adapter still reports the
foreground app and title, which keeps the assistant useful and testable.
"""
from __future__ import annotations

import ctypes
import io
import os
import sys
from ctypes import wintypes
from typing import Protocol

from xenon.screen import CapturedFrame, UiControl, WindowSnapshot

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DEFAULT_MAX_CONTROLS = 200
DEFAULT_MAX_DEPTH = 6


class ScreenUnavailable(RuntimeError):
    """Raised when the desktop cannot provide a foreground window."""


class WindowBackend(Protocol):
    def foreground_handle(self) -> int: ...

    def title(self, handle: int) -> str: ...

    def process_id(self, handle: int) -> int: ...

    def process_name(self, pid: int) -> str: ...

    def window_rect(self, handle: int) -> tuple[int, int, int, int] | None: ...


class ControlReader(Protocol):
    def read(self, handle: int, limit: int) -> tuple[UiControl, ...]: ...


class Grabber(Protocol):
    def grab(self, region: tuple[int, int, int, int] | None): ...


class NullControlReader:
    """Control reader used when UI Automation is unavailable."""

    def read(self, handle: int, limit: int) -> tuple[UiControl, ...]:
        return ()


class Win32Backend:
    """Minimal ctypes wrapper around the Win32 calls Xenon needs."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ScreenUnavailable("the Win32 backend requires Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetWindowRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def foreground_handle(self) -> int:
        handle = self._user32.GetForegroundWindow()
        if not handle:
            raise ScreenUnavailable("no foreground window")
        return int(handle)

    def title(self, handle: int) -> str:
        length = self._user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(max(length, 0) + 1)
        self._user32.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value

    def process_id(self, handle: int) -> int:
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return int(pid.value)

    def process_name(self, pid: int) -> str:
        handle = self._kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                raise OSError(
                    ctypes.get_last_error(), "QueryFullProcessImageNameW failed"
                )
            return os.path.splitext(os.path.basename(buffer.value))[0]
        finally:
            self._kernel32.CloseHandle(handle)

    def window_rect(self, handle: int) -> tuple[int, int, int, int] | None:
        rect = wintypes.RECT()
        if not self._user32.GetWindowRect(handle, ctypes.byref(rect)):
            return None
        box = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
            return None
        return box


class UiaControlReader:
    """Reads a bounded UI Automation subtree for the given window."""

    def __init__(self, max_depth: int = DEFAULT_MAX_DEPTH) -> None:
        self._max_depth = max_depth

    def read(self, handle: int, limit: int) -> tuple[UiControl, ...]:
        try:
            from pywinauto import Desktop
        except ImportError:
            return ()

        try:
            window = Desktop(backend="uia").window(handle=handle)
        except Exception:
            return ()

        controls: list[UiControl] = []
        self._walk(window, 0, limit, controls, set())
        return tuple(controls)

    def _walk(
        self,
        wrapper,
        depth: int,
        limit: int,
        out: list[UiControl],
        seen: set[tuple[str, str, str, bool]],
    ) -> None:
        if depth > self._max_depth or len(out) >= limit:
            return
        try:
            children = wrapper.children()
        except Exception:
            return
        for child in children:
            if len(out) >= limit:
                return
            try:
                info = child.element_info
                role = info.control_type or ""
                name = info.name or ""
            except Exception:
                continue

            value = ""
            try:
                raw = child.get_value()
                value = "" if raw is None else str(raw)
            except Exception:
                value = ""

            is_password = bool(getattr(info, "is_password", False))
            if not is_password:
                try:
                    is_password = bool(child.is_password())
                except Exception:
                    pass

            key = (role, name, value, is_password)
            if key in seen:
                continue
            if name or value:
                seen.add(key)
                out.append(
                    UiControl(
                        role=role,
                        name=name,
                        value=value,
                        is_password=is_password,
                    )
                )
            self._walk(child, depth + 1, limit, out, seen)


class MssGrabber:
    """Screen grabber backed by ``mss``; falls back to the primary monitor."""

    def grab(self, region: tuple[int, int, int, int] | None):
        import mss
        from PIL import Image

        with mss.mss() as sct:
            if region is None:
                monitor = sct.monitors[0]
            else:
                left, top, right, bottom = region
                monitor = {
                    "left": left,
                    "top": top,
                    "width": right - left,
                    "height": bottom - top,
                }
            shot = sct.grab(monitor)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def default_control_reader() -> ControlReader:
    try:
        import pywinauto  # noqa: F401
    except ImportError:
        return NullControlReader()
    return UiaControlReader()


class WindowsUiAutomation:
    """Read-only foreground window provider for ``ScreenObserver``."""

    def __init__(
        self,
        backend: WindowBackend | None = None,
        controls: ControlReader | None = None,
        max_controls: int = DEFAULT_MAX_CONTROLS,
    ) -> None:
        self._backend = backend if backend is not None else Win32Backend()
        self._controls = controls if controls is not None else default_control_reader()
        self._max_controls = max_controls

    def prewarm(self) -> None:
        """Pay the COM/pywinauto cost before the first user request.

        Constructing a ``Desktop`` object is not enough: the expensive part is
        wrapping a real window and walking its tree. So prewarm performs one
        complete foreground read and throws the result away. Failures are
        non-fatal by design.
        """
        try:
            from pywinauto import Desktop

            Desktop(backend="uia")
        except Exception:
            pass
        try:
            self.foreground_window()
        except Exception:
            pass

    def foreground_window(self) -> WindowSnapshot:
        handle = self._backend.foreground_handle()
        title = self._read_title(handle)
        pid = self._read_pid(handle)
        app_name = self._read_app_name(pid)
        return WindowSnapshot(
            app_name=app_name,
            title=title,
            controls=self._read_controls(handle),
        )

    def capture_foreground(self, grabber: Grabber | None = None) -> CapturedFrame:
        handle = self._backend.foreground_handle()
        try:
            region = self._backend.window_rect(handle)
        except Exception:
            region = None
        grabber = grabber if grabber is not None else MssGrabber()
        image = grabber.grab(region)
        with io.BytesIO() as buffer:
            image.save(buffer, format="PNG", optimize=False)
            payload = buffer.getvalue()
        return CapturedFrame(
            image=payload,
            width=image.width,
            height=image.height,
        )

    def _read_title(self, handle: int) -> str:
        try:
            return self._backend.title(handle)
        except Exception:
            return ""

    def _read_pid(self, handle: int) -> int:
        try:
            return self._backend.process_id(handle)
        except Exception:
            return 0

    def _read_app_name(self, pid: int) -> str:
        if pid <= 0:
            return "unknown"
        try:
            name = self._backend.process_name(pid)
        except Exception:
            name = ""
        return name or f"pid-{pid}"

    def _read_controls(self, handle: int) -> tuple[UiControl, ...]:
        try:
            return self._controls.read(handle, self._max_controls)
        except Exception:
            return ()
