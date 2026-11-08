"""Display brightness: external monitors over DDC/CI, laptop panels over WMI.

External monitors are set through Windows' own monitor configuration API
(``dxva2``: ``SetMonitorBrightness``), which speaks DDC/CI to the monitor, so
nothing extra is installed. Laptop panels use WMI.

Some monitors accept the DDC/CI command and still keep their brightness:
Windows HDR, dynamic contrast and some eco or game picture modes lock it.
Every write is read back, and a display that ignored it is reported as
``ignored`` with its screen rectangle, so the orb can dim it in software (a
click-through dark overlay) instead of claiming success.

``toggle`` drops every display to its minimum and remembers the previous
levels in ``%APPDATA%\\xenon\\brightness.json``, so a second toggle (from the
hotkey, a voice command or the CLI) restores them.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol

Rect = tuple[int, int, int, int]

# ------------------------------------------------------------- displays --


class HardwareIgnored(OSError):
    """The display accepted the command but its brightness did not change."""


class Display(Protocol):
    name: str
    kind: str  # "monitor" (DDC/CI) or "laptop" (WMI)
    rect: Rect | None

    def get(self) -> int: ...
    def set(self, value: int) -> None: ...


@dataclass
class DdcMonitor:
    name: str
    index: int
    rect: Rect | None = None
    kind: str = "monitor"
    settle_s: float = 0.35

    def _with_handle(self, action):
        found = _physical_monitors()
        try:
            if self.index >= len(found):
                raise OSError("monitor disappeared")
            return action(ctypes.windll.dxva2, found[self.index][0])
        finally:
            _release(found)

    @staticmethod
    def _range(dxva2, handle) -> tuple[int, int, int]:
        from ctypes import wintypes

        lo, cur, hi = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        if not dxva2.GetMonitorBrightness(handle, ctypes.byref(lo), ctypes.byref(cur), ctypes.byref(hi)):
            raise OSError("DDC/CI read failed")
        return lo.value, cur.value, hi.value

    def get(self) -> int:
        def read(dxva2, handle):
            lo, cur, hi = self._range(dxva2, handle)
            return round(100 * (cur - lo) / max(1, hi - lo))

        return self._with_handle(read)

    def set(self, value: int) -> None:
        from ctypes import wintypes

        def write(dxva2, handle):
            lo, _cur, hi = self._range(dxva2, handle)
            raw = lo + round((hi - lo) * clamp(value) / 100)
            if not dxva2.SetMonitorBrightness(handle, wintypes.DWORD(raw)):
                raise OSError("DDC/CI write failed")

        self._with_handle(write)
        time.sleep(self.settle_s)
        if abs(self.get() - clamp(value)) > 5:
            raise HardwareIgnored(
                "the monitor accepted the command but kept its brightness "
                "(Windows HDR or a locked picture mode usually does this)"
            )


@dataclass
class WmiPanel:
    name: str
    rect: Rect | None = None
    kind: str = "laptop"

    def get(self) -> int:
        for item in _wmi().ExecQuery("SELECT CurrentBrightness FROM WmiMonitorBrightness"):
            return int(item.CurrentBrightness)
        raise OSError("no laptop panel")

    def set(self, value: int) -> None:
        for item in _wmi().ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods"):
            # Positional arguments are ordered by WMI's own parameter IDs, which
            # fails with "Invalid parameter"; pass them by name instead.
            params = item.Methods_("WmiSetBrightness").InParameters.SpawnInstance_()
            params.Properties_.Item("Timeout").Value = 0
            params.Properties_.Item("Brightness").Value = clamp(value)
            item.ExecMethod_("WmiSetBrightness", params)
            return
        raise OSError("no laptop panel")


def clamp(value: float) -> int:
    return int(min(max(round(value), 0), 100))


def _wmi():
    import win32com.client

    return win32com.client.GetObject(r"winmgmts:\\.\root\WMI")


def _monitor_rects() -> list[tuple[Rect, object]]:
    """(rect, HMONITOR) for every display, left to right."""
    from ctypes import wintypes

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    found: list[tuple[Rect, object]] = []

    @proc
    def collect(hmon, _hdc, rect, _data):
        r = rect.contents
        found.append(((r.left, r.top, r.right, r.bottom), hmon))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, collect, 0)
    return sorted(found, key=lambda item: (item[0][0], item[0][1]))


def _physical_monitors() -> list[tuple[object, str, Rect]]:
    """(handle, description, rect) for every physical monitor, in a stable order."""
    from ctypes import wintypes

    class PHYSICAL_MONITOR(ctypes.Structure):
        _fields_ = [("handle", wintypes.HANDLE), ("description", ctypes.c_wchar * 128)]

    dxva2 = ctypes.windll.dxva2
    found: list[tuple[object, str, Rect]] = []
    for rect, hmon in _monitor_rects():
        count = wintypes.DWORD()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)) or not count.value:
            continue
        array = (PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count, array):
            continue
        for monitor in array:
            found.append((monitor.handle, monitor.description, rect))
    return found


def _release(found) -> None:
    for item in found:
        try:
            ctypes.windll.dxva2.DestroyPhysicalMonitor(item[0])
        except Exception:
            pass


def target_kinds() -> set[str]:
    """Which displays brightness commands touch: XENON_BRIGHTNESS_TARGET.

    ``monitor`` (default) changes external monitors only, ``laptop`` only the
    built-in panel, ``all`` both.
    """
    value = os.environ.get("XENON_BRIGHTNESS_TARGET", "monitor").strip().lower()
    return {"monitor": {"monitor"}, "laptop": {"laptop"}, "all": {"monitor", "laptop"}}.get(
        value, {"monitor"}
    )


def targets(displays: list[Display]) -> list[Display]:
    return [display for display in displays if display.kind in target_kinds()]


def discover() -> list[Display]:
    """Displays whose brightness Xenon can address right now."""
    if sys.platform != "win32":
        return []
    from ctypes import wintypes

    displays: list[Display] = []
    without_ddc: list[Rect] = []
    try:
        found = _physical_monitors()
        try:
            for index, (handle, description, rect) in enumerate(found):
                lo, cur, hi = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
                if ctypes.windll.dxva2.GetMonitorBrightness(
                    handle, ctypes.byref(lo), ctypes.byref(cur), ctypes.byref(hi)
                ):
                    displays.append(DdcMonitor(description or f"Monitor {index + 1}", index, rect))
                else:
                    without_ddc.append(rect)
        finally:
            _release(found)
    except Exception:
        pass
    try:
        # A laptop panel does not speak DDC/CI, so it is one of the displays
        # that refused the DDC read.
        panel = WmiPanel("Laptop screen", without_ddc[0] if without_ddc else None)
        panel.get()
        displays.append(panel)
    except Exception:
        pass
    return displays


# ----------------------------------------------------------- remembering --


def _state_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "xenon", "brightness.json")


def _load_saved(path: str | None = None) -> dict:
    try:
        with open(path or _state_path(), encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _save(data: dict, path: str | None = None) -> None:
    path = path or _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def _key(display: Display, index: int) -> str:
    return f"{display.kind}:{index}:{display.name}"


# --------------------------------------------------------------- actions --

MIN_LEVEL = 0


@dataclass
class Outcome:
    reply: str
    ok: bool
    # Displays that ignored the change: (rect, target level). The orb dims
    # these in software.
    ignored: list[tuple[Rect, int]] = field(default_factory=list)
    # Every display currently dimmed in software, after this change.
    overlays: list[tuple[Rect, int]] = field(default_factory=list)


def apply_detailed(action: str, value: int = 0, displays: list[Display] | None = None,
                   state_path: str | None = None) -> Outcome:
    """``action``: set, up, down, min, max, toggle or restore."""
    if displays is None:
        displays = targets(discover())
    if not displays:
        return Outcome(
            "I can't control brightness on these displays. For an external "
            "monitor, turn on DDC/CI in its on-screen menu.",
            False,
        )
    saved = _load_saved(state_path)
    if action == "toggle":
        action = "restore" if saved.get("dimmed") else "min"
    elif action == "restore" and not saved.get("dimmed"):
        return Outcome("Your screens aren't dimmed.", True)

    software = saved.get("software", {})
    changed, failed, levels = [], [], []
    ignored: list[tuple[Rect, int]] = []
    before: dict[str, int] = {}
    for index, display in enumerate(displays):
        key = _key(display, index)
        try:
            # A display dimmed in software reports its hardware level, which
            # never moved; use the level we last asked for instead.
            current = int(software[key]["level"]) if key in software else display.get()
            before[key] = current
            target = _target(action, value, current, saved, key)
            if target is None:
                return Outcome(f"Unknown brightness action {action!r}.", False)
            try:
                if target != display.get():
                    display.set(target)
                software.pop(key, None)
            except HardwareIgnored:
                if display.rect is None:
                    raise
                ignored.append((display.rect, target))
                if target >= 100:
                    software.pop(key, None)
                else:
                    software[key] = {"level": target, "rect": list(display.rect)}
            changed.append(display.name)
            levels.append(target)
        except Exception as error:
            failed.append(f"{display.name} ({error})")

    if action == "min":
        if not saved.get("dimmed"):
            saved["levels"] = before
        saved["dimmed"] = True
    elif changed:
        saved["dimmed"] = False
    saved["software"] = software
    _save(saved, state_path)

    if not changed:
        return Outcome("Couldn't change brightness: " + "; ".join(failed), False)
    if action == "min":
        reply = "Dimmed your screens to the minimum. Say it again to bring them back."
    elif action == "restore":
        reply = "Brightness restored."
    elif len(set(levels)) == 1:
        reply = f"Brightness {levels[0]}%."
    else:
        reply = "Brightness " + ", ".join(f"{n} {lvl}%" for n, lvl in zip(changed, levels)) + "."
    if failed:
        reply += " Skipped: " + "; ".join(failed)
    return Outcome(reply, True, ignored, _overlays(software))


def _overlays(software: dict) -> list[tuple[Rect, int]]:
    result = []
    for entry in software.values():
        try:
            result.append((tuple(int(v) for v in entry["rect"]), int(entry["level"])))
        except (KeyError, TypeError, ValueError):
            continue
    return result


def software_overlays(state_path: str | None = None) -> list[tuple[Rect, int]]:
    """Displays that should stay dimmed in software (for the orb at startup)."""
    return _overlays(_load_saved(state_path).get("software", {}))


def overlay_alpha(level: int) -> float:
    """How dark the overlay is for a brightness level; 0% stays readable."""
    return round(0.82 * (1 - clamp(level) / 100), 3)


def _target(action: str, value: int, current: int, saved: dict, key: str) -> int | None:
    if action == "set":
        return clamp(value)
    if action == "up":
        return clamp(current + (value or 10))
    if action == "down":
        return clamp(current - (value or 10))
    if action == "min":
        return MIN_LEVEL
    if action == "max":
        return 100
    if action == "restore":
        return clamp(int(saved.get("levels", {}).get(key, 100)))
    return None


def apply(action: str, value: int = 0, **kwargs) -> str:
    """Text-only version for the CLI and one-shot commands."""
    outcome = apply_detailed(action, value, **kwargs)
    if outcome.ignored:
        return (
            outcome.reply + " Your monitor kept its own brightness (Windows HDR or "
            "a locked picture mode does this), so Xenon's orb dims it on screen "
            "instead."
        )
    return outcome.reply


# --------------------------------------------------------------- parsing --


@dataclass(frozen=True)
class BrightnessCommand:
    action: str  # set, up, down, min, max, toggle, restore
    value: int = 0


_SCREEN = r"(?:(?:the\s+|my\s+)?(?:screen|screens|monitor|monitors|display|displays|brightness))"
_FILLER = r"^(?:please\s+|can you\s+|could you\s+)?"
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("min", re.compile(_FILLER + rf"(?:dim|darken|minimi[sz]e)\s+{_SCREEN}(?:\s+(?:all the way|completely|fully|to (?:the )?(?:minimum|lowest)))?$")),
    ("min", re.compile(_FILLER + r"(?:minimum|lowest|min)\s+brightness$")),
    ("min", re.compile(_FILLER + r"(?:brightness|screen)\s+(?:to\s+)?(?:minimum|lowest|min|zero|off)$")),
    ("max", re.compile(_FILLER + r"(?:maximum|max|full|highest)\s+brightness$")),
    ("max", re.compile(_FILLER + r"(?:brightness|screen)\s+(?:to\s+)?(?:maximum|max|full|highest)$")),
    ("max", re.compile(_FILLER + rf"(?:brighten|maximi[sz]e)\s+{_SCREEN}(?:\s+(?:all the way|fully))?$")),
    ("restore", re.compile(_FILLER + r"(?:undim|restore|reset)\s+(?:the\s+|my\s+)?(?:screen|screens|monitor|monitors|brightness)$")),
    ("up", re.compile(_FILLER + rf"(?:brightness\s+up|brighter|turn\s+up\s+(?:the\s+)?brightness|make\s+{_SCREEN}\s+brighter|increase\s+(?:the\s+)?brightness)$")),
    ("down", re.compile(_FILLER + rf"(?:brightness\s+down|darker|dimmer|turn\s+down\s+(?:the\s+)?brightness|make\s+{_SCREEN}\s+(?:darker|dimmer)|decrease\s+(?:the\s+)?brightness|lower\s+(?:the\s+)?brightness)$")),
]
_SET_RE = re.compile(
    _FILLER + r"(?:set\s+)?(?:the\s+|my\s+)?(?:screen\s+|monitor\s+)?brightness\s+(?:to\s+|at\s+)?(?P<n>\d{1,3})\s*(?:%|percent)?$"
)


def parse(text: str) -> BrightnessCommand | None:
    phrase = " ".join(text.lower().strip().strip(".!?").split())
    if not phrase:
        return None
    match = _SET_RE.match(phrase)
    if match:
        return BrightnessCommand("set", clamp(int(match.group("n"))))
    for action, pattern in _PATTERNS:
        if pattern.match(phrase):
            return BrightnessCommand(action)
    return None


def run(command: BrightnessCommand, **kwargs) -> str:
    return apply(command.action, command.value, **kwargs)
