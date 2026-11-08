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
        # Explain the on-screen dimming once, not on every step.
        state_path = kwargs.get("state_path")
        saved = _load_saved(state_path)
        if not saved.get("explained_software"):
            saved["explained_software"] = True
            _save(saved, state_path)
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


_SCREEN_WORDS = {"brightness", "screen", "screens", "monitor", "monitors", "display", "displays"}
# Things that are dimmed but are not screens: leave them to other handlers.
_NOT_SCREENS = {"light", "lights", "lamp", "lamps", "bulb", "bulbs", "music", "volume", "sound", "voice"}
_LEAD = re.compile(
    r"^(?:(?:hey|ok|okay)\s+)?(?:(?:can|could|would|will)\s+you\s+|please\s+|i\s+(?:want|need)\s+you\s+to\s+)*"
)
_MIN_WORDS = ("minimum", "lowest", "all the way down", "completely", "fully", "zero")
_MAX_WORDS = ("maximum", "highest", "full", "all the way up")
_DOWN = re.compile(r"\b(?:lower|reduce|decrease|down|dim|dimmer|darker|darken|less bright)\b")
_UP = re.compile(r"\b(?:raise|increase|up|brighter|brighten|more bright|boost)\b")
_RESTORE = re.compile(r"\b(?:undim|un-dim|restore|reset|back to normal|normal brightness)\b")
_NUMBER = re.compile(r"\b(?P<n>\d{1,3})\s*(?:%|percent)?(?!\w)")


def parse(text: str) -> BrightnessCommand | None:
    """Understand brightness requests however they are phrased.

    "can you lower my brightness", "turn my screen down a bit", "dim my
    monitor", "brightness 30", "make it brighter", "restore brightness".
    """
    phrase = " ".join(text.lower().replace("\u2019", "'").strip().strip(".!?").split())
    phrase = _LEAD.sub("", phrase).strip()
    if not phrase:
        return None
    words = set(re.findall(r"[a-z]+", phrase))
    if words & _NOT_SCREENS:
        return None
    if phrase.startswith(("what", "how", "why", "is ", "does ")):
        return None  # a question about brightness, not a request
    about_screen = bool(words & _SCREEN_WORDS)
    # "dim it", "make it darker", "brighter": allowed without naming the
    # screen only when the sentence is short and has no other object.
    bare = not about_screen and len(phrase.split()) <= 4 and re.search(
        r"\b(?:dim|dimmer|darker|darken|brighter|brighten)\b", phrase
    )
    if not about_screen and not bare:
        return None

    if _RESTORE.search(phrase):
        return BrightnessCommand("restore")
    number = _NUMBER.search(phrase)
    if number and about_screen:
        return BrightnessCommand("set", clamp(int(number.group("n"))))
    down = bool(_DOWN.search(phrase))
    up = bool(_UP.search(phrase)) and not down
    if (any(word in phrase for word in _MIN_WORDS) or re.search(r"\bmin\b", phrase)) and not up:
        return BrightnessCommand("min")
    if (any(word in phrase for word in _MAX_WORDS) or re.search(r"\bmax\b", phrase)) and not down:
        return BrightnessCommand("max")
    if re.search(r"\bminimi[sz]e\b", phrase):
        return BrightnessCommand("min")
    if re.search(r"\bmaximi[sz]e\b", phrase):
        return BrightnessCommand("max")
    # "dim my monitor" on its own means as dark as it goes.
    if re.match(r"^(?:dim|darken|minimi[sz]e)\s+(?:the\s+|my\s+|all\s+)?(?:screen|screens|monitor|monitors|display|displays)$", phrase):
        return BrightnessCommand("min")
    step = 5 if re.search(r"\b(?:a bit|a little|slightly|a touch)\b", phrase) else 25 if "a lot" in phrase else 0
    if down:
        return BrightnessCommand("down", step)
    if up:
        return BrightnessCommand("up", step)
    return None


def run(command: BrightnessCommand, **kwargs) -> str:
    return apply(command.action, command.value, **kwargs)
