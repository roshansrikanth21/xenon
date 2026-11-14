"""Game mode: get Xenon out of the way so a game has the machine to itself.

Entering it (Ctrl+Alt+G, the tray, or automatically when a full-screen
game starts, if you turn that on) stops everything Xenon runs: the music
widget and its helper processes, voice, the phone companion page and its
now-playing polling, any running task. The orb hides and its loop drops
to a few wake-ups a second. Only the tray icon and the hotkeys stay, so
the same key brings it all back as it was.

It can also close apps you list in ``game_mode.json`` (for example a
browser or a launcher). They are asked to close the normal way (the same
message as clicking their X), so each can ask to save; nothing is ever
force-killed, and a few system processes are never touched at all.

Settings, ``%APPDATA%/xenon/game_mode.json``::

    {"close_apps": ["chrome.exe"], "auto": false, "keep_apps": ["discord.exe"]}

``keep_apps`` is for Xtreme mode (see ``xtreme``): apps it never offers
to close. ``keep_voice`` (default true) leaves voice listening in game and
Xtreme mode, so you can say "game mode off"; the listener only transcribes
when it hears speech, but set it false to squeeze out the last bit.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Never asked to close, whatever the list says: the shell, Windows' own
# frames, Notepad (its unsaved tabs), and Xenon itself.
PROTECTED = frozenset({
    "explorer.exe", "applicationframehost.exe", "dwm.exe", "csrss.exe", "winlogon.exe",
    "notepad.exe", "python.exe", "pythonw.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "textinputhost.exe", "taskmgr.exe", "systemsettings.exe",
})


@dataclass
class GameModeConfig:
    close_apps: list = field(default_factory=list)   # exe names, asked to close on entry
    auto: bool = False                               # enter when a full-screen game runs
    keep_apps: list = field(default_factory=list)    # Xtreme mode never offers to close these
    keep_voice: bool = True                          # voice keeps listening, so "game mode off" works


def config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "xenon" / "game_mode.json"


def load(path: Path | None = None) -> GameModeConfig:
    path = path or config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return GameModeConfig()
    if not isinstance(data, dict):
        return GameModeConfig()

    def names(key):
        value = data.get(key)
        return [str(a).strip() for a in value if str(a).strip()] if isinstance(value, list) else []

    return GameModeConfig(close_apps=names("close_apps"), auto=bool(data.get("auto", False)),
                          keep_apps=names("keep_apps"), keep_voice=bool(data.get("keep_voice", True)))


def save(config: GameModeConfig, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


def closable(names) -> set[str]:
    """The listed exe names, lower-cased, minus the protected ones."""
    out = set()
    for name in names or ():
        name = str(name).strip().lower()
        if name and not name.endswith(".exe"):
            name += ".exe"
        if name and name not in PROTECTED:
            out.add(name)
    return out


def _exe_of_window(hwnd) -> str:
    user, kernel = ctypes.windll.user32, ctypes.windll.kernel32
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel.OpenProcess(0x1000, False, pid.value)       # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel.CloseHandle(handle)


def close_apps(names) -> list[str]:
    """Ask every visible top-level window of the listed apps to close
    (WM_CLOSE, like its X button). Returns the exe names asked."""
    wanted = closable(names)
    if not wanted or sys.platform != "win32":
        return []
    user = ctypes.windll.user32
    asked: list[str] = []
    targets = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        if user.IsWindowVisible(hwnd) and not user.GetWindow(hwnd, 4):     # GW_OWNER: top-level only
            exe = _exe_of_window(hwnd)
            if exe in wanted:
                targets.append((hwnd, exe))
        return True

    user.EnumWindows(proto(visit), 0)
    for hwnd, exe in targets:
        user.PostMessageW(hwnd, 0x0010, 0, 0)                       # WM_CLOSE
        if exe not in asked:
            asked.append(exe)
    return asked


def fullscreen_app_running() -> bool:
    """Is a full-screen app (a game, a D3D full-screen window) in front?
    Windows' own answer, the one it uses to hold back notifications."""
    if sys.platform != "win32":
        return False
    state = ctypes.c_int(0)
    try:
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
    except Exception:
        return False
    # 2: busy (a full-screen app), 3: running Direct3D full screen.
    return state.value in (2, 3)


def trim_memory() -> None:
    """Hand this process's idle memory back to Windows."""
    if sys.platform != "win32":
        return
    try:
        kernel = ctypes.windll.kernel32
        kernel.SetProcessWorkingSetSize(kernel.GetCurrentProcess(), ctypes.c_size_t(-1), ctypes.c_size_t(-1))
    except Exception:
        pass


class AutoWatch:
    """With ``auto`` on: enter game mode once a full-screen app has been in
    front for ``enter_s``, and leave ``leave_s`` after it is gone, but only
    if this watch entered it. Leaving by hand mid-game snoozes it until the
    full-screen app goes away, so it does not jump straight back in.
    ``tick(now, active)`` returns "enter", "leave" or None."""

    def __init__(self, enter_s: float = 4.0, leave_s: float = 8.0, probe=fullscreen_app_running) -> None:
        self.enter_s, self.leave_s, self.probe = enter_s, leave_s, probe
        self.entered = False
        self.snoozed = False
        self._since = None

    def left_by_hand(self) -> None:
        self.entered = False
        self.snoozed = True
        self._since = None

    def tick(self, now: float, active: bool) -> str | None:
        full = self.probe()
        if not full:
            self.snoozed = False
        if not active:
            ready = full and not self.snoozed
            wait = self.enter_s
        else:
            ready = self.entered and not full
            wait = self.leave_s
        if not ready:
            self._since = None
            return None
        if self._since is None:
            self._since = now
        if now - self._since < wait:
            return None
        self._since = None
        if not active:
            self.entered = True
            return "enter"
        self.entered = False
        return "leave"
