"""Put a window on the desktop's wallpaper layer.

Windows has no widget layer, so we build one the same way wallpaper engines do:
Explorer draws the wallpaper into a ``WorkerW`` window underneath the desktop
icons, and re-parenting our window into it makes it part of the desktop itself
— behind every application, invisible to Alt-Tab and the taskbar, surviving
Win+D and "show desktop".

The technique is the classic Progman/WorkerW one, with the Windows 11 24H2
layout handled explicitly: on that build the icons (``SHELLDLL_DefView``) and
the wallpaper WorkerW are both children of Progman, so a full-screen window is
parented into Progman and slotted directly below the icons instead.

Every call is read-only with respect to other windows; the only mutations are
to our own window's parent, z-order and style bits.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

# Layer styles / z-order sentinels.
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020

HWND_BOTTOM = 1
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOZORDER = 0x0004

GA_PARENT = 1

# Undocumented message that asks Progman to spawn the wallpaper WorkerW.
WM_SPAWN_WORKER = 0x052C

# The desktop shell itself is a full-screen visible window, so coverage checks
# must never count it, and cloaked UWP host windows must not either.
SHELL_CLASSES = frozenset({"Progman", "WorkerW", "SHELLDLL_DefView", "Button"})

DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9

_declared = False


def _declare() -> None:
    """64-bit handles overflow ctypes' int defaults; declare every call."""
    global _declared
    if _declared or sys.platform != "win32":
        _declared = True
        return
    user = ctypes.windll.user32
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.FindWindowExW.argtypes = [
        wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
    ]
    user.FindWindowExW.restype = wintypes.HWND
    user.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD),
    ]
    user.SendMessageTimeoutW.restype = wintypes.LPARAM
    user.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user.SetParent.restype = wintypes.HWND
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.GetDesktopWindow.restype = wintypes.HWND
    user.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user.SetWindowPos.restype = wintypes.BOOL
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype = wintypes.BOOL
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.IsIconic.argtypes = [wintypes.HWND]
    user.IsIconic.restype = wintypes.BOOL
    user.IsZoomed.argtypes = [wintypes.HWND]
    user.IsZoomed.restype = wintypes.BOOL
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetClassNameW.restype = ctypes.c_int
    user.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    ]
    user.EnumWindows.restype = wintypes.BOOL
    dwm = ctypes.windll.dwmapi
    dwm.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    dwm.DwmGetWindowAttribute.restype = ctypes.HRESULT
    _declared = True


def _user32():
    _declare()
    return ctypes.windll.user32


def get_parent(hwnd: int) -> int:
    """The window's real parent, or 0 when it is a plain top-level window.

    ``GetParent()`` is the wrong call here: a window re-parented without
    WS_CHILD reports its *owner* instead. ``GetAncestor(GA_PARENT)`` reports
    the actual parent, and yields the desktop window when there is none.
    """
    user = _user32()
    parent = user.GetAncestor(hwnd, GA_PARENT)
    return 0 if parent == user.GetDesktopWindow() else parent


def find_wallpaper_worker_w() -> int:
    """The WorkerW that sits behind the desktop icons, or 0.

    The shell is occasionally in a state with no such window (Explorer
    restarting); callers then fall back to their non-wallpaper mode.
    """
    user = _user32()
    progman = user.FindWindowW("Progman", None)
    if not progman:
        return 0

    # Nudge Explorer into creating the split WorkerW/SHELLDLL_DefView layout.
    user.SendMessageTimeoutW(progman, WM_SPAWN_WORKER, 0, 0, 0, 1000, None)

    worker = 0

    def callback(hwnd: int, _lparam: int) -> bool:
        nonlocal worker
        def_view = user.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
        if def_view:
            # The wallpaper layer is the WorkerW that immediately follows the
            # window hosting the icons in the shell's own z-order.
            worker = user.FindWindowExW(0, hwnd, "WorkerW", None)
            return False
        return True

    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    try:
        user.EnumWindows(proto(callback), 0)
    except Exception:
        return 0
    return worker or progman


def attach_to_desktop(hwnd: int) -> int:
    """Parent ``hwnd`` into the wallpaper layer. Returns the new parent, or 0."""
    user = _user32()
    parent = find_wallpaper_worker_w()
    if not parent:
        return 0
    user.SetParent(hwnd, parent)
    return parent if get_parent(hwnd) == parent else 0


def attach_behind_icons(hwnd: int) -> int:
    """Parent ``hwnd`` onto the desktop *behind the icons*.

    ``attach_to_desktop`` is fine for small widgets, but on Windows 11 24H2 it
    can land a full-screen window in Progman above the icon view, where it
    would swallow every desktop click. This pins it below the icons on both
    shell layouts. Returns the new parent, or 0.
    """
    user = _user32()
    progman = user.FindWindowW("Progman", None)
    if not progman:
        return 0
    user.SendMessageTimeoutW(progman, WM_SPAWN_WORKER, 0, 0, 0, 1000, None)

    if user.FindWindowExW(progman, 0, "SHELLDLL_DefView", None):
        # 24H2 layout: icons and wallpaper WorkerW are both under Progman.
        user.SetParent(hwnd, progman)
        if get_parent(hwnd) != progman:
            return 0
        restack_behind_icons(hwnd)
        return progman

    worker = find_wallpaper_worker_w()
    if not worker or worker == progman:
        return 0
    user.SetParent(hwnd, worker)
    return worker if get_parent(hwnd) == worker else 0


def restack_behind_icons(hwnd: int) -> None:
    """Slot a window attached by ``attach_behind_icons`` under the icons."""
    parent = get_parent(hwnd)
    if not parent:
        return
    def_view = _user32().FindWindowExW(parent, 0, "SHELLDLL_DefView", None)
    if not def_view:
        return
    _user32().SetWindowPos(
        hwnd, def_view, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    )


def detach_from_desktop(hwnd: int) -> None:
    """Return ``hwnd`` to being a normal top-level window."""
    if get_parent(hwnd) == 0:
        return
    _user32().SetParent(hwnd, 0)


def make_tool_window(hwnd: int) -> None:
    """Keep the window out of Alt-Tab and the taskbar."""
    user = _user32()
    style = user.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)


def set_no_activate(hwnd: int, enabled: bool) -> None:
    """Prevent clicks on the window from stealing focus from the active app."""
    user = _user32()
    style = user.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    nxt = (style | WS_EX_NOACTIVATE) if enabled else style & ~WS_EX_NOACTIVATE
    if nxt != style:
        user.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, nxt)


def set_bounds(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    """Move/resize with raw Win32 — works while the window is re-parented."""
    _user32().SetWindowPos(hwnd, 0, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE)


def send_to_bottom(hwnd: int) -> None:
    _user32().SetWindowPos(
        hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    )


def send_to_top_most(hwnd: int) -> None:
    _user32().SetWindowPos(
        hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    )


def clear_top_most(hwnd: int) -> None:
    _user32().SetWindowPos(
        hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    )


def show(hwnd: int) -> None:
    _user32().SetWindowPos(
        hwnd, 0, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
    )


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def is_cloaked(hwnd: int) -> bool:
    """DWM-cloaked windows (hidden UWP hosts) sit over everything invisibly."""
    value = wintypes.DWORD(0)
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(value), 4
    )
    return hr == 0 and value.value != 0


def is_covered(hwnd: int, rect: tuple[int, int, int, int] | None, windows=None) -> bool:
    """Is every pixel of ``hwnd`` hidden behind some other top-level window?

    A wallpaper widget is underneath everything by definition, so any window
    covering its rectangle hides it completely — and rendering pixels nobody
    can see is the most wasteful thing a desktop widget can do. ``windows``
    injects a scan result for tests; live it walks top-level windows.
    """
    if not rect:
        return False
    user = _user32()
    covers = False

    def consider(other: int) -> bool:
        nonlocal covers
        if other == hwnd or not user.IsWindowVisible(other) or user.IsIconic(other):
            return True
        box = window_rect(other)
        if not box:
            return True
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return True
        if box[0] > rect[0] or box[1] > rect[1]:
            return True
        if box[2] < rect[2] or box[3] < rect[3]:
            return True
        name = class_name_of(other)
        if name in SHELL_CLASSES:
            return True
        ex = user.GetWindowLongPtrW(other, GWL_EXSTYLE)
        if ex & (WS_EX_NOACTIVATE | WS_EX_TRANSPARENT):
            # Invisible full-screen HUDs (NVIDIA overlay, touch keyboard) sit
            # over everything permanently; a real cover must be activatable.
            return True
        if is_cloaked(other):
            return True
        covers = True
        return False

    if windows is not None:
        for hwnd_other in windows:
            consider(hwnd_other)
        return covers

    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(other: int, _lparam: int) -> bool:
        return consider(other)

    try:
        user.EnumWindows(proto(callback), 0)
    except Exception:
        return False
    return covers


def class_name_of(hwnd: int) -> str:
    user = _user32()
    buffer = ctypes.create_unicode_buffer(256)
    length = user.GetClassNameW(hwnd, buffer, 256)
    return buffer.value if length > 0 else ""
