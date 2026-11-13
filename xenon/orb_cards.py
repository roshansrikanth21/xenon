"""Cards the orb shows while a task runs: confirm, choose, undo.

The rules that make a card safe live here, not in the caller:

* The Confirm button is drawn on a Tk canvas, so it exposes no UI
  Automation Invoke pattern another agent could call.
* Cards are shown without activation (WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
  ShowWindow(SW_SHOWNOACTIVATE), SWP_NOACTIVATE, and focus handed straight
  back), so a card that appears while Roshan types never takes his
  keystrokes. No key binding confirms anything; Enter is never bound, and
  the field box is stripped of the Text class bindings so it is inert.
* Confirm arms after 0.8 s (1.5 s for destructive or unknown risk), and a
  click whose source is injected (SendInput from an agent) is ignored and
  logged. A low-level mouse hook, alive only while a card is visible, sees
  the injected flag; when it has nothing, GetCurrentInputMessageSource
  decides, and anything other than real hardware counts as injected.
* Every field value is rendered through ``untrusted.strip_invisible`` and
  labelled with where it came from (your words, drafter, Claude, page).

The pure parts (``CardModel``, ``UndoTimer``, ``card_rows``,
``noactivate_exstyle``, ``pick_choice``, ``primary_label``) have no Tk and
are what the tests drive.
"""
from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

# Dark glass, matching the orb's body and the answer card.
KEY_COLOR = "#010203"          # made transparent so the card's corners round
CARD_BG = "#0d111c"
CARD_EDGE = "#262d45"
PANEL_BG = CARD_BG
PANEL_FG = "#e6eaf5"
PANEL_DIM = "#8b93a7"
BUTTON_BG = "#1b2133"
BUTTON_EDGE = "#2a3150"
DISABLED_FG = "#5b6378"
CARD_RADIUS = 14
RISK_COLORS = {
    "read": "#93c5fd",
    "local": "#93c5fd",
    "outward": "#fbbf24",
    "destructive": "#f87171",
    "denied": "#f87171",
}
SOURCE_COLORS = {
    "your words": "#4ade80",
    "heard": "#fbbf24",
    "drafter": "#fbbf24",
    "claude": "#c4b5fd",
    "page": "#f87171",
    "xenon": "#8b93a7",
}
SOURCE_LABELS = {
    "your words": "your words",
    "heard": "heard",
    "drafter": "drafter",
    "claude": "Claude",
    "page": "page-authored",
    "xenon": "Xenon",
}
BADGE_RED = "#f87171"
BADGE_AMBER = "#fbbf24"

ARMED_AFTER_S = 0.8
# A choice card nobody answers cancels itself after this long.
CHOICE_TIMEOUT_S = 90.0
ARMED_AFTER_DESTRUCTIVE_S = 1.5
CLICK_MEMORY_S = 1.0
FIELD_MAX_LINES = 12
FIELD_CHARS_PER_LINE = 52
FIELD_VALUE_LIMIT = 4000       # beyond this even the scroll box elides

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
IMO_UNAVAILABLE = 0x00000000
IMO_HARDWARE = 0x00000001
IMO_INJECTED = 0x00000002
IMO_SYSTEM = 0x00000004
WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
LLMHF_INJECTED = 0x00000001
LLMHF_LOWER_IL_INJECTED = 0x00000002

# Keys that must never be bound on a card: a focused card plus a synthetic
# Enter would otherwise be a one-keystroke approval.
FORBIDDEN_KEYS = ("<Return>", "<KP_Enter>", "<Key-Return>", "<Key-KP_Enter>", "<space>", "<Key-space>")


def _log_stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def _risk_name(risk) -> str:
    return str(getattr(risk, "value", risk) or "").strip().lower()


def is_strict_risk(risk) -> bool:
    """Destructive, denied, or anything we don't recognise: arm slowly and
    make Cancel the default. Unknown resolves to the stricter answer."""
    return _risk_name(risk) not in {"read", "local", "outward"}


def armed_delay(risk) -> float:
    return ARMED_AFTER_DESTRUCTIVE_S if is_strict_risk(risk) else ARMED_AFTER_S


@dataclass
class CardModel:
    """When a card's Confirm may count. No Tk: the card asks this on every
    click, and the tests drive it with synthetic time."""

    shown_at: float
    armed_after_s: float = ARMED_AFTER_S
    default_button: str = "confirm"   # visual emphasis only; no key confirms
    decided: bool = False
    ignored: list[str] = field(default_factory=list)

    @classmethod
    def for_risk(cls, risk, shown_at: float, armed_after_s: float | None = None) -> "CardModel":
        strict = is_strict_risk(risk)
        delay = armed_after_s if armed_after_s is not None else armed_delay(risk)
        if strict:
            # A caller may slow arming further, never speed up a destructive card.
            delay = max(delay, ARMED_AFTER_DESTRUCTIVE_S)
        return cls(shown_at, delay, "cancel" if strict else "confirm")

    def armed(self, now: float) -> bool:
        return now - self.shown_at >= self.armed_after_s

    def arming_fraction(self, now: float) -> float:
        if self.armed_after_s <= 0:
            return 1.0
        return min(max((now - self.shown_at) / self.armed_after_s, 0.0), 1.0)

    def can_confirm(self, now: float, injected: bool) -> bool:
        return not self.decided and not injected and self.armed(now)

    def click(self, target: str | None, now: float, injected: bool) -> str | None:
        """Decision for a completed click on ``target`` ("cancel"/"confirm"),
        or None when the click must be ignored (reason kept in ``ignored``).

        Cancel always counts, even injected: it can only stop something.
        """
        if self.decided or target is None:
            return None
        if target == "cancel":
            return "deny"
        if target != "confirm":
            return None
        if injected:
            self.ignored.append("injected")
            return None
        if not self.armed(now):
            self.ignored.append("not armed")
            return None
        return "approve"


@dataclass
class UndoTimer:
    """The undo strip's countdown: exactly one of undo / elapse ever wins."""

    seconds: float
    started_at: float
    done: bool = False
    outcome: str = ""

    def left(self, now: float) -> float:
        return max(0.0, self.started_at + max(0.0, self.seconds) - now)

    def fraction_left(self, now: float) -> float:
        if self.seconds <= 0:
            return 0.0
        return self.left(now) / self.seconds

    def tick(self, now: float) -> str | None:
        """"elapse" the first time the countdown runs out, else None."""
        if self.done or self.left(now) > 0:
            return None
        self.done, self.outcome = True, "elapse"
        return "elapse"

    def undo(self) -> bool:
        if self.done:
            return False
        self.done, self.outcome = True, "undo"
        return True

    def label(self, now: float, verb: str = "Sending") -> str:
        return f"{verb} in {int(self.left(now) + 0.999)}…"


def _minimal_strip(text: str) -> str:
    """Used only when ``xenon.untrusted`` cannot be imported: drop format
    (Cf) characters such as zero-width and bidi controls, NFKC-normalise."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        char for char in text
        if unicodedata.category(char) != "Cf" and (char in "\n\t" or unicodedata.category(char) != "Cc")
    )


def strip_invisible(text: str) -> str:
    try:
        from xenon.untrusted import strip_invisible as strip
    except Exception:
        strip = _minimal_strip
    return strip(str(text or ""))


def safe_text(text, limit: int = 400) -> str:
    """Invisible characters stripped, elided to ``limit`` characters."""
    cleaned = strip_invisible("" if text is None else str(text))
    if len(cleaned) > limit:
        cleaned = cleaned[: max(0, limit - 1)] + "…"
    return cleaned


def source_label(source: str) -> str:
    # An unknown source is shown as page-authored, never as his words.
    return SOURCE_LABELS.get(source, SOURCE_LABELS["page"])


def source_color(source: str) -> str:
    return SOURCE_COLORS.get(source, SOURCE_COLORS["page"])


@dataclass(frozen=True)
class CardRow:
    label: str
    value: str
    source: str        # key into SOURCE_COLORS ("page" when unknown)
    tag: str           # what the card prints ("your words", "page-authored")
    lines: int         # wrapped line estimate


def card_rows(fields, chars_per_line: int = FIELD_CHARS_PER_LINE) -> list[CardRow]:
    """The readback rows, cleaned, each tagged with its source."""
    rows = []
    for item in fields or ():
        source = getattr(item, "source", "page")
        source = source if source in SOURCE_COLORS else "page"
        label = safe_text(getattr(item, "label", ""), 28)
        value = safe_text(getattr(item, "value", ""), FIELD_VALUE_LIMIT)
        text_len = len(label) + 3 + len(value) + len(source_label(source)) + 3
        lines = sum(max(1, -(-len(part) // chars_per_line)) for part in (label + " · " + value).split("\n"))
        lines = max(lines, -(-text_len // chars_per_line))
        rows.append(CardRow(label, value, source, source_label(source), lines))
    return rows


def field_box_lines(rows: list[CardRow]) -> tuple[int, bool]:
    """(visible lines, scrolls): long readbacks cap at FIELD_MAX_LINES."""
    total = sum(row.lines for row in rows) or 1
    return min(total, FIELD_MAX_LINES), total > FIELD_MAX_LINES


def badge_color(badge: str) -> str:
    """Red for the ones that mean "look twice", amber otherwise."""
    text = badge.lower()
    red_words = ("new", "first", "never", "mismatch", "invisible", "page", "link", "secret", "delete",
                 "unknown", "lookalike", "mixed", "not in your request")
    return BADGE_RED if any(word in text for word in red_words) else BADGE_AMBER


def primary_label(pending) -> str:
    """The explicit words on the Confirm button ("Press enter in WhatsApp")."""
    title = safe_text(getattr(pending, "title", "") or "Confirm", 44)
    return title or "Confirm"


def noactivate_exstyle(style: int) -> int:
    """The extended style a card window must carry: never activated by a
    click, no taskbar button, always on top. Existing bits are kept."""
    return int(style) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST


_ORDINALS = {
    "first": 0, "1st": 0, "one": 0, "1": 0,
    "second": 1, "2nd": 1, "two": 1, "2": 1,
    "third": 2, "3rd": 2, "three": 2, "3": 2,
    "fourth": 3, "4th": 3, "four": 3, "4": 3,
    "fifth": 4, "5th": 4, "five": 4, "5": 4,
    "sixth": 5, "6th": 5, "six": 5, "6": 5,
    "seventh": 6, "7th": 6, "seven": 6, "7": 6,
    "eighth": 7, "8th": 7, "eight": 7, "8": 7,
    "ninth": 8, "9th": 8, "nine": 8, "9": 8,
}
_NONE_WORDS = frozenset({"none", "neither", "nothing", "none of them", "none of those", "none of these", "no one"})


def pick_choice(text: str, choices: list[str]) -> tuple[bool, str | None]:
    """Read a spoken or typed pick: ``(understood, choice)``.

    "the second one" / "2" / "second" pick by position, the exact choice
    text picks itself, "none" is ``(True, None)``, anything else is
    ``(False, None)`` (not understood, so nothing is picked).
    """
    words = re.sub(r"[^a-z0-9 ]+", " ", safe_text(text).lower())
    words = " ".join(words.split())
    if not words or not choices:
        return False, None
    if words in _NONE_WORDS:
        return True, None
    for choice in choices:
        if words == " ".join(re.sub(r"[^a-z0-9 ]+", " ", safe_text(choice).lower()).split()):
            return True, choice
    trimmed = re.sub(r"^(?:the|number|option|pick|choose)\s+", "", words)
    trimmed = re.sub(r"\s+(?:one|option)$", "", trimmed) if trimmed not in _ORDINALS else trimmed
    if trimmed == "last":
        return True, choices[-1]
    index = _ORDINALS.get(trimmed)
    if index is not None and index < len(choices):
        return True, choices[index]
    return False, None


# ---------------------------------------------------------------------------
# Win32 (injectable so tests never touch real windows)
# ---------------------------------------------------------------------------


class Win32:
    """The handful of calls the cards make. Tests pass a fake."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        # The Ptr variants: GWL_EXSTYLE through the 32-bit call is fine
        # today, but the Ptr form is the one that is right on x64.
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32 = user32

    def toplevel(self, widget) -> int:
        widget.update_idletasks()
        return int(self._user32.GetParent(widget.winfo_id()) or widget.winfo_id())

    def exstyle(self, hwnd: int) -> int:
        return int(self._user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))

    def set_exstyle(self, hwnd: int, style: int) -> None:
        self._user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)

    def foreground(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def set_foreground(self, hwnd: int) -> None:
        if hwnd and self._user32.IsWindow(hwnd):
            self._user32.SetForegroundWindow(hwnd)

    def show_noactivate(self, hwnd: int) -> None:
        self._user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

    def place(self, hwnd: int, x: int, y: int) -> None:
        self._user32.SetWindowPos(hwnd, HWND_TOPMOST, int(x), int(y), 0, 0,
                                  SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)


def is_injected_input() -> bool:
    """Was the input message being handled injected? Unknown counts as yes.

    GetCurrentInputMessageSource fills INPUT_MESSAGE_SOURCE{deviceType,
    originId}; IMO_INJECTED means SendInput. Only IMO_HARDWARE is trusted:
    IMO_INJECTED, IMO_SYSTEM, IMO_UNAVAILABLE (no input message being
    processed, which is what Tk's deferred event dispatch usually sees) and
    an API failure all return True, so none of them can approve a card. A
    real click is recognised by the low-level hook (``InjectionWatch``),
    which this function only backs up.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        class INPUT_MESSAGE_SOURCE(ctypes.Structure):
            _fields_ = [("deviceType", wintypes.DWORD), ("originId", wintypes.DWORD)]

        source = INPUT_MESSAGE_SOURCE()
        if not ctypes.windll.user32.GetCurrentInputMessageSource(ctypes.byref(source)):
            return True
        if source.originId & IMO_INJECTED:
            return True
        return source.originId != IMO_HARDWARE
    except Exception:
        return True


class InjectionWatch:
    """A WH_MOUSE_LL hook that exists only while a card is visible.

    It records whether the latest left press/release carried the injected
    flag. Installed on the Tk thread, whose message loop services it, so no
    thread is added; it is unhooked when the last card closes, so idle cost
    stays zero. Set XENON_CARD_HOOK=off to disable it: clicks then fall back
    to ``is_injected_input``, which is stricter (cards may not confirm).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hook = None
        self._proc = None
        self._users = 0
        self.last_down: tuple[float, bool] | None = None
        self.last_up: tuple[float, bool] | None = None

    def note(self, message: int, injected: bool) -> None:
        stamp = (self._clock(), bool(injected))
        if message == WM_LBUTTONDOWN:
            self.last_down = stamp
        elif message == WM_LBUTTONUP:
            self.last_up = stamp

    def verdict(self) -> bool | None:
        """True/False for the click just made, or None when the hook saw none."""
        now = self._clock()
        seen = [entry for entry in (self.last_down, self.last_up)
                if entry is not None and now - entry[0] <= CLICK_MEMORY_S]
        if not seen:
            return None
        return any(injected for _stamp, injected in seen)

    def start(self) -> None:
        self._users += 1
        if self._hook is not None or sys.platform != "win32":
            return
        if os.environ.get("XENON_CARD_HOOK", "on").strip().lower() in {"0", "off", "false", "no"}:
            return
        try:
            import ctypes
            from ctypes import wintypes

            class MSLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                            ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
            user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, proc_type, wintypes.HINSTANCE, wintypes.DWORD]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE

            def callback(code, wparam, lparam):
                try:
                    if code >= 0 and wparam in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                        info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        self.note(int(wparam), bool(info.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED)))
                except Exception:
                    pass
                return user32.CallNextHookEx(None, code, wparam, lparam)

            self._proc = proc_type(callback)
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
            if not self._hook:
                self._proc = None
        except Exception:
            self._hook = None
            self._proc = None

    def stop(self) -> None:
        self._users = max(0, self._users - 1)
        if self._users or self._hook is None:
            return
        try:
            import ctypes

            ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        self._hook = None
        self._proc = None


_SHARED_WATCH: InjectionWatch | None = None


def shared_watch() -> InjectionWatch:
    """One hook for every card, built the first time a card is shown."""
    global _SHARED_WATCH
    if _SHARED_WATCH is None:
        _SHARED_WATCH = InjectionWatch()
    return _SHARED_WATCH


def click_was_injected(watch: InjectionWatch | None, fallback: Callable[[], bool] = is_injected_input) -> bool:
    verdict = watch.verdict() if watch is not None else None
    if verdict is not None:
        return verdict
    return fallback()


def show_without_activation(top, x: int, y: int, win32=None) -> int:
    """Map ``top`` and put it at (x, y) without taking keyboard focus.

    Tk only creates a toplevel's wrapper window when it is first mapped, and
    its own show call activates it, so the card is mapped off screen first;
    then it gets WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW, focus goes straight back
    to whatever had it, and only then is it shown with SW_SHOWNOACTIVATE and
    moved into view with SWP_NOACTIVATE.
    """
    win32 = win32 if win32 is not None else Win32()
    previous = win32.foreground()
    top.geometry("+-32000+-32000")
    top.deiconify()
    top.update_idletasks()
    hwnd = win32.toplevel(top)
    win32.set_exstyle(hwnd, noactivate_exstyle(win32.exstyle(hwnd)))
    if previous and previous != hwnd and win32.foreground() != previous:
        win32.set_foreground(previous)
    win32.show_noactivate(hwnd)
    win32.place(hwnd, x, y)
    return hwnd


def bound_keys(widget) -> set[str]:
    """Every key sequence bound on ``widget`` and its descendants (widget
    level and toplevel level). Used by tests to prove Enter is unbound."""
    found: set[str] = set()
    stack = [widget]
    while stack:
        current = stack.pop()
        try:
            found.update(current.bind())
        except Exception:
            pass
        try:
            stack.extend(current.winfo_children())
        except Exception:
            pass
    return found


# ---------------------------------------------------------------------------
# Tk cards
# ---------------------------------------------------------------------------


def _rounded(canvas, x0: float, y0: float, x1: float, y1: float, r: float, **kwargs):
    """A smooth rounded rectangle as one polygon (Tk has none built in)."""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    points = [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=12, **kwargs)


class _Card:
    """Shared shell: a borderless, topmost, never-activating Toplevel whose
    corners are rounded by drawing on a canvas over a transparent key colour.

    Content goes in ``self.body`` (a Frame placed on the canvas); ``_show``
    sizes the rounded background to it.
    """

    PAD = 14

    def __init__(self, root, anchor: tuple[int, int], win32=None, watch: InjectionWatch | None = None) -> None:
        import tkinter as tk

        self._tk = tk
        self.top = tk.Toplevel(root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(bg=KEY_COLOR, highlightthickness=0, takefocus=0)
        try:
            self.top.attributes("-transparentcolor", KEY_COLOR)
        except Exception:
            self.top.configure(bg=CARD_BG)   # square corners, still readable
        self.shell = tk.Canvas(self.top, bg=KEY_COLOR, highlightthickness=0, bd=0, takefocus=0)
        self.shell.pack()
        self.body = tk.Frame(self.shell, bg=CARD_BG, takefocus=0)
        self._body_item = self.shell.create_window(self.PAD, self.PAD, window=self.body, anchor="nw")
        self.anchor = anchor
        self._win32 = win32
        self._watch = watch
        self._after: list[str] = []
        self.closed = False
        self.hovered = False
        self.top.bind("<Enter>", lambda _e: self._set_hover(True))
        self.top.bind("<Leave>", lambda _e: self._set_hover(False))
        # Esc only ever cancels, and only while the pointer is on the card,
        # so an Esc meant for another window can't dismiss it by accident.
        self.top.bind("<Escape>", lambda _e: self._escape())

    def _set_hover(self, value: bool) -> None:
        self.hovered = value

    def _escape(self) -> None:
        if self.hovered:
            self.dismiss()

    def dismiss(self) -> None:  # overridden
        self.close()

    def _layout_shell(self) -> tuple[int, int]:
        self.top.update_idletasks()
        width = self.body.winfo_reqwidth() + 2 * self.PAD
        height = self.body.winfo_reqheight() + 2 * self.PAD
        self.shell.configure(width=width, height=height)
        self.shell.delete("shell-bg")
        _rounded(self.shell, 1, 1, width - 1, height - 1, CARD_RADIUS, fill=CARD_BG, outline=CARD_EDGE,
                 width=1, tags="shell-bg")
        self.shell.tag_lower("shell-bg")
        return width, height

    def _show(self) -> None:
        width, _height = self._layout_shell()
        x = int(self.anchor[0] - width / 2)
        y = int(self.anchor[1])
        try:
            show_without_activation(self.top, x, y, self._win32)
        except Exception:
            self.top.geometry(f"+{x}+{y}")
            self.top.deiconify()
        if self._watch is not None:
            self._watch.start()

    def _later(self, ms: int, callback: Callable[[], None]) -> None:
        if not self.closed:
            self._after.append(self.top.after(ms, callback))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for handle in self._after:
            try:
                self.top.after_cancel(handle)
            except Exception:
                pass
        if self._watch is not None:
            self._watch.stop()
        try:
            self.top.destroy()
        except Exception:
            pass


class ConfirmCard(_Card):
    """One action, its exact readback, and a Confirm that must be clicked."""

    WIDTH = 392
    BUTTON_H = 36

    def __init__(
        self,
        root,
        pending,
        on_decide: Callable[[str], None],
        anchor: tuple[int, int],
        armed_after_s: float | None = None,
        win32=None,
        watch: InjectionWatch | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = 120.0,
        log: Callable[[str], None] | None = None,
        injected: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(root, anchor, win32, watch if watch is not None else shared_watch())
        tk = self._tk
        import tkinter.font as tkfont

        self.pending = pending
        self._on_decide = on_decide
        self._clock = clock
        self._log = log or _log_stderr
        self._injected = injected or is_injected_input
        risk = _risk_name(pending.risk)
        self.risk = risk
        self.model = CardModel.for_risk(risk, clock(), armed_after_s)
        self._deadline = clock() + timeout_s
        self._accent = RISK_COLORS.get(risk, BADGE_RED)
        inner = self.WIDTH - 2 * self.PAD
        self._fonts = {
            "chip": tkfont.Font(root=self.top, family="Segoe UI Semibold", size=8),
            "title": tkfont.Font(root=self.top, family="Segoe UI Semibold", size=11),
            "body": tkfont.Font(root=self.top, family="Segoe UI", size=9),
            "small": tkfont.Font(root=self.top, family="Segoe UI", size=8),
            "button": tkfont.Font(root=self.top, family="Segoe UI Semibold", size=9),
        }

        # ---- header: risk chip + title ------------------------------------
        title = safe_text(pending.title, 80)
        header = tk.Canvas(self.body, width=inner, height=26, bg=CARD_BG, highlightthickness=0, takefocus=0)
        header.pack(fill="x")
        chip_text = (risk or "unknown").upper()
        chip_w = self._fonts["chip"].measure(chip_text) + 14
        _rounded(header, 0, 4, chip_w, 22, 9, fill=self._accent, outline="")
        header.create_text(chip_w / 2, 13, text=chip_text, fill=CARD_BG, font=self._fonts["chip"])
        header.create_text(chip_w + 10, 13, text=title, anchor="w", fill=PANEL_FG, font=self._fonts["title"],
                           width=inner - chip_w - 12)

        # ---- fields: "Label · value  [source]" in an inert scroll box ------
        self.rows = card_rows(pending.fields)
        lines, scrolls = field_box_lines(self.rows)
        box = tk.Frame(self.body, bg=CARD_BG, takefocus=0)
        box.pack(fill="x", pady=(10, 0))
        self.fields = tk.Text(box, wrap="word", font=self._fonts["body"], fg=PANEL_FG, bg=CARD_BG, relief="flat",
                              highlightthickness=0, bd=0, padx=0, pady=0, width=1, height=lines, cursor="arrow",
                              takefocus=0, spacing1=2, spacing3=2, insertwidth=0, exportselection=0)
        # Drop the Text class bindings (typing, selection, Return): the box is
        # display only, and nothing in it may react to a key.
        self.fields.bindtags((str(self.fields), str(self.top), "all"))
        self.fields.tag_configure("label", foreground=PANEL_DIM)
        self.fields.tag_configure("dot", foreground=DISABLED_FG)
        for source, color in SOURCE_COLORS.items():
            self.fields.tag_configure(f"src-{source}", foreground=color, font=self._fonts["small"])
        for index, row in enumerate(self.rows):
            if index:
                self.fields.insert("end", "\n")
            self.fields.insert("end", row.label, "label")
            self.fields.insert("end", " · ", "dot")
            self.fields.insert("end", row.value)
            self.fields.insert("end", f"   {row.tag}", f"src-{row.source}")
        self.fields.config(state="disabled")
        self.fields.pack(side="left", fill="x", expand=True)
        self.fields.configure(width=max(10, inner // max(1, self._fonts["body"].measure("0"))))
        self.scrolls = scrolls
        if scrolls:
            bar = tk.Scrollbar(box, orient="vertical", command=self.fields.yview, width=8, takefocus=0)
            self.fields.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
            self.fields.bind("<MouseWheel>", self._wheel)

        # ---- badges: pills -------------------------------------------------
        badges = [safe_text(b, 48) for b in (pending.badges or ()) if str(b or "").strip()]
        if badges:
            pills = tk.Canvas(self.body, width=inner, height=22, bg=CARD_BG, highlightthickness=0, takefocus=0)
            pills.pack(fill="x", pady=(10, 0))
            x, y = 0, 0
            for badge in badges:
                width = self._fonts["small"].measure(badge) + 16
                if x and x + width > inner:
                    x, y = 0, y + 24
                color = badge_color(badge)
                _rounded(pills, x, y + 2, x + width, y + 20, 9, fill="", outline=color, width=1)
                pills.create_text(x + width / 2, y + 11, text=badge, fill=color, font=self._fonts["small"])
                x += width + 6
            pills.configure(height=y + 24)

        # ---- footer: countdown + how to confirm ----------------------------
        self._countdown = tk.Label(self.body, text="", font=self._fonts["small"], fg=PANEL_DIM, bg=CARD_BG,
                                   anchor="w", takefocus=0)
        self._countdown.pack(fill="x", pady=(10, 0))
        if getattr(pending, "voice_ok", False) and getattr(pending, "nonce", ""):
            hint = f"Click Confirm, or say “send {safe_text(pending.nonce, 6)}”."
        else:
            hint = "Click Confirm to go ahead. Keys never confirm."
        tk.Label(self.body, text=hint, font=self._fonts["small"], fg=PANEL_DIM, bg=CARD_BG, anchor="w",
                 takefocus=0).pack(fill="x")

        # Buttons are pixels on a canvas: no control, no Invoke pattern.
        self.canvas = tk.Canvas(self.body, width=inner, height=self.BUTTON_H + 8, bg=CARD_BG,
                                highlightthickness=0, takefocus=0, cursor="hand2")
        self.canvas.pack(pady=(10, 0))
        self._inner = inner
        self._label = primary_label(pending)
        self._pressed: str | None = None
        self._hover_button: str | None = None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self._motion(None))
        self._draw()
        self._show()
        self._tick()

    # geometry of the two canvas buttons: Cancel left, Confirm right
    def _boxes(self) -> dict[str, tuple[int, int, int, int]]:
        h = self.BUTTON_H
        cancel_w = 96
        return {"cancel": (0, 0, cancel_w, h), "confirm": (cancel_w + 10, 0, self._inner, h)}

    def _hit(self, x: float, y: float) -> str | None:
        for name, (x0, y0, x1, y1) in self._boxes().items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        return None

    def _draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        now = self._clock()
        boxes = self._boxes()
        armed = self.model.armed(now)
        cancel_default = self.model.default_button == "cancel"
        hover = self._hover_button

        x0, y0, x1, y1 = boxes["cancel"]
        cancel_fill = "#2a3150" if cancel_default else BUTTON_BG
        if hover == "cancel":
            cancel_fill = "#323a5c"
        _rounded(canvas, x0, y0, x1, y1, 10, fill=cancel_fill,
                 outline=PANEL_FG if cancel_default else BUTTON_EDGE, width=1)
        canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="Cancel", fill=PANEL_FG, font=self._fonts["button"])

        x0, y0, x1, y1 = boxes["confirm"]
        if not armed:
            fill, outline, text_fg = BUTTON_BG, BUTTON_EDGE, DISABLED_FG
        elif cancel_default:
            # Destructive: armed Confirm stays outlined, Cancel keeps the weight.
            fill, outline, text_fg = ("#2a1618" if hover == "confirm" else BUTTON_BG), self._accent, self._accent
        else:
            fill, outline, text_fg = self._accent, self._accent, CARD_BG
        _rounded(canvas, x0, y0, x1, y1, 10, fill=fill, outline=outline, width=1)
        canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=self._label, fill=text_fg,
                           font=self._fonts["button"], width=x1 - x0 - 16)
        if not armed:
            # Thin arming bar under Confirm: it is greyed until this fills.
            fraction = self.model.arming_fraction(now)
            canvas.create_rectangle(x0 + 8, y1 + 4, x1 - 8, y1 + 6, fill=BUTTON_BG, outline="")
            canvas.create_rectangle(x0 + 8, y1 + 4, x0 + 8 + (x1 - x0 - 16) * fraction, y1 + 6,
                                    fill=self._accent, outline="")

    def _tick(self) -> None:
        if self.closed:
            return
        now = self._clock()
        left = self._deadline - now
        if left <= 0:
            self._decide("deny")
            return
        self._countdown.config(text=f"Cancels itself in {int(left + 0.999)} s")
        self._draw()
        self._later(60 if not self.model.armed(now) else 1000, self._tick)

    def _wheel(self, event) -> str:
        self.fields.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _motion(self, event) -> None:
        target = self._hit(event.x, event.y) if event is not None else None
        if target != self._hover_button:
            self._hover_button = target
            self._draw()

    def _press(self, event) -> None:
        self._pressed = self._hit(event.x, event.y)

    def _release(self, event) -> None:
        target = self._hit(event.x, event.y)
        pressed, self._pressed = self._pressed, None
        if target is None or target != pressed:
            return
        injected = target == "confirm" and click_was_injected(self._watch, self._injected)
        decision = self.model.click(target, self._clock(), injected)
        if decision is None:
            if self.model.ignored and self.model.ignored[-1] == "injected":
                self.model.ignored.append("logged")
                self._log(f"xenon: ignored an injected click on the confirm card ({safe_text(self.pending.title, 40)})")
            return
        self._decide(decision)

    def dismiss(self) -> None:
        self._decide("deny")

    def _decide(self, decision: str) -> None:
        if self.model.decided:
            return
        self.model.decided = True
        try:
            self._on_decide(decision)
        finally:
            self.close()


class ChoiceCard(_Card):
    """Numbered rows built by code; pick one by click (or by voice through
    the caller's ``pick_words``). Dismissing picks None."""

    WIDTH = 340

    def __init__(
        self,
        root,
        title: str,
        choices: list[str],
        on_pick: Callable[[str | None], None],
        anchor: tuple[int, int],
        win32=None,
        watch: InjectionWatch | None = None,
        log: Callable[[str], None] | None = None,
        injected: Callable[[], bool] | None = None,
        timeout_s: float = CHOICE_TIMEOUT_S,
    ) -> None:
        super().__init__(root, anchor, win32, watch if watch is not None else shared_watch())
        tk = self._tk
        self.choices = list(choices)
        self._on_pick = on_pick
        self._log = log or _log_stderr
        self._injected = injected or is_injected_input
        self.picked = False
        self.timeout_s = float(timeout_s)
        tk.Label(self.body, text=safe_text(title, 100), font=("Segoe UI Semibold", 11), fg=PANEL_FG, bg=CARD_BG,
                 wraplength=self.WIDTH - 2 * self.PAD, justify="left", anchor="w",
                 takefocus=0).pack(fill="x", pady=(0, 8))
        self.rows = []
        for number, choice in enumerate(self.choices + [None], start=1):
            row = tk.Frame(self.body, bg=BUTTON_BG, cursor="hand2", takefocus=0)
            row.pack(fill="x", pady=2)
            if choice is None:
                parts = [tk.Label(row, text="None of these", font=("Segoe UI", 10), fg=PANEL_DIM, bg=BUTTON_BG,
                                  anchor="w", padx=10, pady=6, takefocus=0)]
            else:
                parts = [
                    tk.Label(row, text=str(number), font=("Segoe UI Semibold", 10), fg=BADGE_AMBER, bg=BUTTON_BG,
                             width=2, padx=6, pady=6, takefocus=0),
                    tk.Label(row, text=safe_text(choice, 70), font=("Segoe UI", 10), fg=PANEL_FG, bg=BUTTON_BG,
                             anchor="w", pady=6, takefocus=0, wraplength=self.WIDTH - 80, justify="left"),
                ]
            for index, part in enumerate(parts):
                part.pack(side="left", fill="x", expand=index == len(parts) - 1)
            for widget in [row, *parts]:
                widget.bind("<ButtonRelease-1>", lambda _e, c=choice: self._click(c))
            self.rows.append(row)
        if self.timeout_s > 0:
            # Unanswered, it cancels itself (like ConfirmCard), so the orb
            # folds again and the injection hook comes down.
            tk.Label(self.body, text=f"Cancels itself after {int(self.timeout_s)} s", font=("Segoe UI", 8),
                     fg=PANEL_DIM, bg=CARD_BG, anchor="w", takefocus=0).pack(fill="x", pady=(6, 0))
        self._show()
        if self.timeout_s > 0:
            self._later(int(self.timeout_s * 1000), self.timed_out)

    def timed_out(self) -> None:
        """The default when nobody answers: None (cancel)."""
        self.pick(None)

    def _click(self, choice: str | None) -> None:
        if choice is not None and click_was_injected(self._watch, self._injected):
            self._log("xenon: ignored an injected click on a choice card")
            return
        self.pick(choice)

    def dismiss(self) -> None:
        self.pick(None)

    def pick(self, choice: str | None) -> None:
        if self.picked:
            return
        self.picked = True
        try:
            self._on_pick(choice)
        finally:
            self.close()

    def pick_words(self, text: str) -> bool:
        """A typed or spoken pick ("the second one"). True when understood."""
        understood, choice = pick_choice(text, self.choices)
        if understood:
            self.pick(choice)
        return understood


class UndoStrip(_Card):
    """"Sending in 5… Undo" with a draining bar: a few seconds to take an
    approval back. ``on_undo`` or ``on_elapse`` runs exactly once."""

    WIDTH = 260

    def __init__(
        self,
        root,
        seconds: float,
        on_undo: Callable[[], None],
        on_elapse: Callable[[], None],
        anchor: tuple[int, int],
        label: str = "Sending",
        win32=None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Undo only ever stops something, so it needs no injection check.
        super().__init__(root, anchor, win32, None)
        tk = self._tk
        self._on_undo = on_undo
        self._on_elapse = on_elapse
        self._clock = clock
        self.timer = UndoTimer(float(seconds), clock())
        self._verb = safe_text(label, 30) or "Sending"
        row = tk.Frame(self.body, bg=CARD_BG, takefocus=0)
        row.pack(fill="x")
        self._text = tk.Label(row, text="", font=("Segoe UI", 10), fg=PANEL_FG, bg=CARD_BG, anchor="w", takefocus=0)
        self._text.pack(side="left", fill="x", expand=True)
        undo = tk.Label(row, text="Undo", font=("Segoe UI Semibold", 10), fg=BADGE_AMBER, bg=CARD_BG,
                        padx=8, cursor="hand2", takefocus=0)
        undo.pack(side="right")
        undo.bind("<ButtonRelease-1>", lambda _e: self.undo())
        self._bar = tk.Canvas(self.body, width=self.WIDTH - 2 * self.PAD, height=4, bg=CARD_BG,
                              highlightthickness=0, takefocus=0)
        self._bar.pack(fill="x", pady=(8, 0))
        self._show()
        self._tick()

    @property
    def done(self) -> bool:
        return self.timer.done

    def _tick(self) -> None:
        if self.closed:
            return
        now = self._clock()
        if self.timer.tick(now) == "elapse":
            try:
                self._on_elapse()
            finally:
                self.close()
            return
        if self.timer.done:
            return
        self._text.config(text=f"{self.timer.label(now, self._verb)}")
        width = self.WIDTH - 2 * self.PAD
        self._bar.delete("all")
        self._bar.create_rectangle(0, 1, width, 3, fill=BUTTON_BG, outline="")
        self._bar.create_rectangle(0, 1, width * self.timer.fraction_left(now), 3, fill=BADGE_AMBER, outline="")
        self._later(100, self._tick)

    def dismiss(self) -> None:
        self.undo()

    def undo(self) -> None:
        if not self.timer.undo():
            return
        try:
            self._on_undo()
        finally:
            self.close()
