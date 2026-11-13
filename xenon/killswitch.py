"""Kill switches: environment flags that turn agent features off.

Read on every call, not cached, so flipping a flag (or a test patching the
environment) takes effect immediately. Reading an environment variable costs
nothing, and nothing here runs at import time.

Unknown values resolve to the stricter answer: ``XENON_OUTWARD=banana`` is
read as "off", because a typo in a safety flag must never switch a guard off.
"""
from __future__ import annotations

import os
import sys

_ON = frozenset({"1", "on", "true", "yes", "enabled", "enable"})
_OFF = frozenset({"0", "off", "false", "no", "disabled", "disable"})


def feature_enabled(name: str, default: bool = True) -> bool:
    """``XENON_<NAME>``: unset means ``default``; anything unrecognised means off."""
    key = "XENON_" + name.strip().upper().removeprefix("XENON_")
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in _ON:
        return True
    return False  # an explicit "off", or a value nobody can read


def is_elevated() -> bool:
    """True when this process runs with an elevated (administrator) token.

    Agents driven by a model must never act with admin rights: one injected
    instruction would then reach the whole machine. When the token cannot be
    read on Windows the answer is True (stricter), not False.
    """
    if sys.platform != "win32":
        geteuid = getattr(os, "geteuid", None)
        return bool(geteuid is not None and geteuid() == 0)
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        TOKEN_QUERY = 0x0008
        TOKEN_ELEVATION = 20  # TOKEN_INFORMATION_CLASS.TokenElevation
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            return True
        try:
            elevation = wintypes.DWORD(0)
            size = wintypes.DWORD(0)
            if not advapi32.GetTokenInformation(
                token, TOKEN_ELEVATION, ctypes.byref(elevation),
                ctypes.sizeof(elevation), ctypes.byref(size),
            ):
                return True
            return bool(elevation.value)
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return True


def agent_allowed() -> tuple[bool, str]:
    """May Xenon run agent tasks (plans that act on the desktop)?"""
    if not feature_enabled("AGENT"):
        return False, "agent features are switched off (XENON_AGENT=off)"
    if is_elevated():
        return False, "Xenon is running as administrator, so agent features are refused"
    return True, ""


def outward_allowed() -> tuple[bool, str]:
    """May anything be sent, posted or committed to another person?"""
    if not feature_enabled("OUTWARD"):
        return False, "outward actions are switched off (XENON_OUTWARD=off)"
    return True, ""
