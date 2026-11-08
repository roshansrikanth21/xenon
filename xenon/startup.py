"""Start the orb when the user signs in to Windows.

A shortcut in the per-user Startup folder runs ``pythonw -m xenon orb`` from
the project folder, so there is no console window. Nothing is written to the
registry and removing the shortcut undoes it completely.
"""
from __future__ import annotations

import os
import sys

STARTUP_NAME = "Xenon Orb.lnk"
KEY_NAMES = (
    "XENON_MODEL_PROVIDER", "XENON_MODEL_API_KEY", "GROQ_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY",
)


def startup_dir() -> str:
    return os.path.join(
        os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
    )


def shortcut_path() -> str:
    return os.path.join(startup_dir(), STARTUP_NAME)


def enabled() -> bool:
    return os.path.exists(shortcut_path())


def enable() -> str:
    """Create the shortcut and return its path; raises on failure."""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import win32com.client

    path = shortcut_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(path)
    shortcut.TargetPath = pythonw
    shortcut.Arguments = "-m xenon orb"
    shortcut.WorkingDirectory = project
    shortcut.Description = "Xenon floating orb"
    shortcut.Save()
    return path


def disable() -> bool:
    try:
        os.remove(shortcut_path())
        return True
    except FileNotFoundError:
        return False


def user_env_has(name: str) -> bool:
    """True when ``name`` is saved in the user's environment (survives reboot)."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
        return bool(str(value).strip())
    except OSError:
        return False


def key_only_in_this_session() -> bool:
    """A model key is set here but not saved, so a sign-in launch won't see it."""
    return any(os.environ.get(name) for name in KEY_NAMES) and not any(
        user_env_has(name) for name in KEY_NAMES
    )
