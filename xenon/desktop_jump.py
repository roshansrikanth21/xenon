"""Virtual-desktop helpers.

Windows "cloaks" windows that live on another virtual desktop: they stay
"visible" to Win32 but UI Automation sees nothing inside them, and keys sent
after activating them may land elsewhere. Xenon checks this before driving a
browser tab, so it never types into the wrong window.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

DWMWA_CLOAKED = 14


def is_cloaked(hwnd: int) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    value = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED,
                                                   ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        return False
    return value.value != 0
