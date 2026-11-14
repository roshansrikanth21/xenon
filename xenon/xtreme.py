"""Xtreme mode: game mode, plus the rest of the laptop tuned for one game.

On top of game mode (everything Xenon runs stops), Xtreme mode:

* finds the heavy apps: your own apps with a window, by memory (summed
  over all their processes, so 30 browser processes count as one browser)
  and by CPU over a one-second sample. You confirm the list on a card,
  then each is asked to close the normal way (WM_CLOSE, like its X), so
  anything with unsaved work can still ask. Nothing is force-killed. Never
  listed: the app in front (your game), game launchers, anti-cheat,
  Windows' own processes, Xenon, and anything in ``keep_apps``.
* switches Windows to its "Best performance" power mode (the laptop
  power slider; on PCs without one, the High performance plan), and back
  to what it was when you leave.
* drops other busy background apps to below-normal priority, restored
  when you leave.
* asks idle background apps to hand back memory they are not using, and,
  only when Xenon runs as administrator, empties Windows' standby file
  cache. Honest note: Windows already gives a game the memory it needs;
  closing heavy apps is what really frees RAM. These two steps are the
  small extra, and the numbers reported are measured, not guessed.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass

from .game_mode import PROTECTED

LAUNCHERS = frozenset({
    "steam.exe", "steamwebhelper.exe", "steamservice.exe", "epicgameslauncher.exe", "epicwebhelper.exe",
    "battle.net.exe", "agent.exe", "riotclientservices.exe", "riotclientux.exe", "riotclientcrashhandler.exe",
    "eadesktop.exe", "eabackgroundservice.exe", "origin.exe", "upc.exe", "ubisoftconnect.exe",
    "galaxyclient.exe", "xboxapp.exe", "gamingservices.exe", "xboxpcapp.exe", "rockstarlauncher.exe",
    "playnite.desktopapp.exe", "heroicgameslauncher.exe",
})
ANTICHEAT = frozenset({
    "vgc.exe", "vgtray.exe", "easyanticheat.exe", "easyanticheat_eos.exe", "beservice.exe",
    "faceitclient.exe", "faceit.exe", "ricochet.exe",
})
# Windows and drivers that show up as "yours" but must keep running.
SYSTEM = frozenset({
    "sihost.exe", "ctfmon.exe", "runtimebroker.exe", "taskhostw.exe", "fontdrvhost.exe",
    "audiodg.exe", "securityhealthsystray.exe", "smartscreen.exe", "lockapp.exe", "widgets.exe",
    "phoneexperiencehost.exe", "nvcontainer.exe", "nvidia share.exe", "nvidia overlay.exe",
    "radeonsoftware.exe", "amdrsserv.exe", "igfxem.exe", "conhost.exe", "dllhost.exe",
    "msedgewebview2.exe", "svchost.exe", "wmiprvse.exe", "searchindexer.exe", "searchprotocolhost.exe",
})
NEVER = PROTECTED | LAUNCHERS | ANTICHEAT | SYSTEM | {"nvidia app.exe", "nvidia broadcast.exe"}

# Where games live. A game is often running but not in front (alt-tabbed
# to start this), so "the app in front" alone is not enough: on a test
# run, Fortnite came up as the heaviest app to close.
GAME_DIRS = ("\\steamapps\\", "\\epic games\\", "\\riot games\\", "\\xboxgames\\", "\\gog galaxy\\games\\",
             "\\ubisoft game launcher\\games\\", "\\ea games\\", "\\battle.net\\", "\\origin games\\",
             "\\rockstar games\\", "\\games\\", "\\windowsapps\\microsoft.minecraft")


def process_tree() -> dict:
    """pid -> (parent pid, exe name), read once. Asking each process for
    its parents takes a full system snapshot per process (a test run took
    over 20 s for all of them); this map makes it one."""
    import psutil

    tree = {}
    for p in psutil.process_iter(["pid", "ppid", "name"]):
        tree[p.info["pid"]] = (p.info["ppid"], (p.info["name"] or "").lower())
    return tree


def _launched_by_launcher(pid: int, tree: dict) -> bool:
    seen = set()
    parent = tree.get(pid, (None, ""))[0]
    while parent and parent not in seen and parent in tree:
        seen.add(parent)
        ppid, name = tree[parent]
        if name in LAUNCHERS:
            return True
        parent = ppid
    return False


def is_game(proc, tree: dict | None = None) -> bool:
    """A running game: installed in a game library folder, an Unreal
    "-Shipping" build, or started by a game launcher."""
    try:
        name = (proc.name() or "").lower()
        if name.endswith(".exe") and "-shipping" in name:
            return True
        path = (proc.exe() or "").lower()
        if any(d in path for d in GAME_DIRS):
            return True
        if tree is not None:
            return _launched_by_launcher(proc.pid, tree)
        return any((p.name() or "").lower() in LAUNCHERS for p in proc.parents())
    except Exception:
        return False

HEAVY_MB = 500          # an app this big (all its processes) is listed
HEAVY_CPU = 15.0        # ...or one keeping a core this busy (% of one core)


@dataclass
class HeavyApp:
    exe: str
    name: str
    memory_mb: int
    cpu: float


def _foreground_exe() -> str:
    if sys.platform != "win32":
        return ""
    from .game_mode import _exe_of_window

    try:
        return _exe_of_window(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return ""


def fullscreen_exes() -> set[str]:
    """Apps with a window covering a whole monitor: borderless and
    full-screen games installed anywhere count as games."""
    if sys.platform != "win32":
        return set()
    from .game_mode import _exe_of_window

    user = ctypes.windll.user32
    user.MonitorFromWindow.restype = wintypes.HANDLE
    user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    shell = {"explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
             "lockapp.exe", "textinputhost.exe"}
    out: set[str] = set()
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lp):
        if not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd) or user.GetWindow(hwnd, 4):
            return True
        rect = wintypes.RECT()
        if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user.GetMonitorInfoW(user.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):
            return True
        m = info.rcMonitor
        if rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom:
            exe = _exe_of_window(hwnd)
            if exe and exe not in shell:
                out.add(exe)
        return True

    user.EnumWindows(proto(visit), 0)
    return out


def _windowed_exes() -> dict[str, str]:
    """exe -> a window title, for apps with a visible top-level window
    (only those can be asked to close the polite way)."""
    if sys.platform != "win32":
        return {}
    from .game_mode import _exe_of_window

    user = ctypes.windll.user32
    out: dict[str, str] = {}
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lp):
        if user.IsWindowVisible(hwnd) and not user.GetWindow(hwnd, 4):
            length = user.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user.GetWindowTextW(hwnd, buf, length + 1)
                exe = _exe_of_window(hwnd)
                if exe and exe not in out:
                    out[exe] = buf.value
        return True

    user.EnumWindows(proto(visit), 0)
    return out


def _mine(proc, me: str) -> bool:
    try:
        return proc.username() == me
    except Exception:
        return False


def _family(pid: int) -> set[int]:
    """This process, its parents and children (Xenon's own tree)."""
    import psutil

    ids = {pid}
    try:
        proc = psutil.Process(pid)
        ids |= {p.pid for p in proc.parents()} | {p.pid for p in proc.children(recursive=True)}
    except Exception:
        pass
    return ids


def rank_heavy(usage: dict, windowed: dict, skip: set, mb: int = HEAVY_MB,
               cpu: float = HEAVY_CPU) -> list[HeavyApp]:
    """Pure part of ``find_heavy``: ``usage`` is exe -> (memory MB, cpu %)."""
    out = []
    for exe, (memory, load) in usage.items():
        if exe in skip or exe in NEVER or exe not in windowed:
            continue
        if memory >= mb or load >= cpu:
            name = os.path.splitext(exe)[0]
            out.append(HeavyApp(exe, name[:1].upper() + name[1:], int(memory), round(load, 1)))
    out.sort(key=lambda a: (a.memory_mb + a.cpu * 20), reverse=True)
    return out


def find_heavy(keep=(), sample_s: float = 1.0) -> list[HeavyApp]:
    """Your windowed apps using a lot of memory or CPU, heaviest first."""
    import time

    import psutil

    me = psutil.Process().username()
    own = _family(os.getpid())
    procs = [p for p in psutil.process_iter(["name", "pid"]) if p.info["pid"] not in own and _mine(p, me)]
    # Games already running when Xtreme starts are kept, whatever window is
    # in front: by install folder / launcher, or a window covering a screen.
    tree = process_tree()
    games = {(p.info["name"] or "").lower() for p in procs if is_game(p, tree)} | fullscreen_exes()
    for p in procs:
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(sample_s)
    usage: dict[str, list] = {}
    for p in procs:
        try:
            exe = (p.info["name"] or "").lower()
            mem = p.memory_info().rss / 2 ** 20
            load = p.cpu_percent(None)
        except Exception:
            continue
        entry = usage.setdefault(exe, [0.0, 0.0])
        entry[0] += mem
        entry[1] += load
    skip = {str(k).strip().lower() for k in keep} | {_foreground_exe()} | games
    return rank_heavy({k: tuple(v) for k, v in usage.items()}, _windowed_exes(), skip)


# ------------------------------------------------------------------ power

_BEST_PERFORMANCE = "ded574b5-45a0-4f42-8737-46345c09c238"     # laptop power slider
_HIGH_PERFORMANCE = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"     # classic plan


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "_GUID":
        import uuid

        b = uuid.UUID(text).bytes_le
        g = cls()
        ctypes.memmove(ctypes.byref(g), b, 16)
        return g

    def text(self) -> str:
        import uuid

        return str(uuid.UUID(bytes_le=bytes(ctypes.string_at(ctypes.byref(self), 16))))


def set_best_performance() -> dict | None:
    """Switch to Windows' best-performance power mode; returns what to
    restore (or None when nothing could be changed)."""
    if sys.platform != "win32":
        return None
    power = ctypes.windll.powrprof
    # The power-mode slider (laptops on Windows 10/11 with Balanced).
    try:
        current = _GUID()
        if power.PowerGetEffectiveOverlayScheme(ctypes.byref(current)) == 0:
            if power.PowerSetActiveOverlayScheme(_GUID.of(_BEST_PERFORMANCE)) == 0:
                return {"overlay": current.text()}
    except (AttributeError, OSError):
        pass
    # Otherwise the High performance plan, if this PC has one.
    try:
        active = ctypes.POINTER(_GUID)()
        if power.PowerGetActiveScheme(None, ctypes.byref(active)) == 0:
            before = active.contents.text()
            ctypes.windll.kernel32.LocalFree(active)
            if power.PowerSetActiveScheme(None, ctypes.byref(_GUID.of(_HIGH_PERFORMANCE))) == 0:
                return {"scheme": before}
    except (AttributeError, OSError):
        pass
    return None


def restore_power(saved: dict | None) -> None:
    if not saved or sys.platform != "win32":
        return
    power = ctypes.windll.powrprof
    try:
        if "overlay" in saved:
            power.PowerSetActiveOverlayScheme(_GUID.of(saved["overlay"]))
        elif "scheme" in saved:
            power.PowerSetActiveScheme(None, ctypes.byref(_GUID.of(saved["scheme"])))
    except (AttributeError, OSError):
        pass


# ------------------------------------------------------------- priorities

def calm_background(skip=(), min_cpu: float = 2.0) -> dict[int, int]:
    """Busy background apps of yours drop to below-normal priority.
    Returns pid -> previous priority, for ``restore_priorities``."""
    import psutil

    me = psutil.Process().username()
    own = _family(os.getpid())
    skip = {str(s).lower() for s in skip} | {_foreground_exe()}
    procs = [p for p in psutil.process_iter(["name", "pid"]) if p.info["pid"] not in own and _mine(p, me)]
    tree = process_tree()
    for p in procs:
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    import time
    time.sleep(0.5)
    changed: dict[int, int] = {}
    for p in procs:
        try:
            exe = (p.info["name"] or "").lower()
            if exe in skip or exe in NEVER or p.cpu_percent(None) < min_cpu or is_game(p, tree):
                continue
            before = p.nice()
            if before == psutil.NORMAL_PRIORITY_CLASS:
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                changed[p.pid] = before
        except Exception:
            continue
    return changed


def restore_priorities(changed: dict[int, int]) -> None:
    import psutil

    for pid, before in (changed or {}).items():
        try:
            psutil.Process(pid).nice(before)
        except Exception:
            pass


# ------------------------------------------------------------------ memory

def trim_background(skip=()) -> int:
    """Ask your background apps to hand back pages they are not using
    (EmptyWorkingSet). Returns how many were trimmed."""
    if sys.platform != "win32":
        return 0
    import psutil

    kernel, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    me = psutil.Process().username()
    skip = {str(s).lower() for s in skip} | {_foreground_exe()} | fullscreen_exes()
    tree = process_tree()
    count = 0
    for p in psutil.process_iter(["name", "pid"]):
        exe = (p.info["name"] or "").lower()
        if exe in skip or exe in LAUNCHERS or exe in ANTICHEAT or not _mine(p, me) or is_game(p, tree):
            continue
        handle = kernel.OpenProcess(0x0100 | 0x1000, False, p.info["pid"])   # SET_QUOTA | QUERY_LIMITED
        if not handle:
            continue
        try:
            if psapi.EmptyWorkingSet(handle):
                count += 1
        finally:
            kernel.CloseHandle(handle)
    return count


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def purge_standby() -> bool:
    """Empty Windows' standby list (cached file pages), as RAMMap's "Empty
    Standby List" does. Needs administrator; False otherwise."""
    if sys.platform != "win32" or not is_admin():
        return False
    advapi, kernel, ntdll = ctypes.windll.advapi32, ctypes.windll.kernel32, ctypes.windll.ntdll

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Luid", LUID), ("Attributes", wintypes.DWORD)]

    token = wintypes.HANDLE()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(token)):
        return False
    try:
        luid = LUID()
        if not advapi.LookupPrivilegeValueW(None, "SeProfileSingleProcessPrivilege", ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES(1, luid, 0x2)            # SE_PRIVILEGE_ENABLED
        advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
        if ctypes.GetLastError() != 0:
            return False
    finally:
        kernel.CloseHandle(token)
    command = ctypes.c_int(4)                          # MemoryPurgeStandbyList
    status = ntdll.NtSetSystemInformation(80, ctypes.byref(command), ctypes.sizeof(command))
    return status == 0


def free_memory_mb() -> int:
    import psutil

    return int(psutil.virtual_memory().available / 2 ** 20)
