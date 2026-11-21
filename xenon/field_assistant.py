"""Suggest text for the form field you clicked — never fill it for you.

Xenon watches where you click, reads the field's label through Windows UI
Automation, works out what the field wants, and offers two or three variations
from your own local profile in a small always-on-top pop-up. Nothing is typed
until you click one of the suggestions.

The profile lives in ``%APPDATA%\\xenon\\profile.json``. Only fields you put
there are ever suggested. Passwords, card numbers, OTPs and similar secrets are
never offered anything at all.

Start it with ``xenon fields`` (or from the orb's tray later on).
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

PROFILE_NAME = "profile.json"


def profile_path() -> str:
    """Where the local-only form profile lives."""
    return os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "xenon", PROFILE_NAME
    )

# Fields that must never be filled from a profile, however they are labelled.
SENSITIVE = re.compile(
    r"password|passwd|pwd|secret|card|credit|cvv|cvc|ssn|social security|"
    r"otp|one.?time|verification code|security code|pin\b|private key|token",
    re.IGNORECASE,
)

# Order matters: the first pattern that matches wins.
KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|mobile|contact number|tel(?:ephone)?", re.I)),
    ("name", re.compile(r"full name|your name|first name|last name|applicant name|candidate name", re.I)),
    ("cover_letter", re.compile(r"cover letter|why (?:you|do you want)|motivation|statement of purpose|about why", re.I)),
    ("resume", re.compile(r"resume|cv\b|upload.*(?:cv|resume)", re.I)),
    ("portfolio", re.compile(r"portfolio|website|github|linkedin|personal site", re.I)),
    ("skills", re.compile(r"skills|technologies|what you know|expertise", re.I)),
    ("bio", re.compile(r"bio|about you|short (?:intro|introduction|description)|summary|profile", re.I)),
)


def classify(label: str, window: str = "", control_type: str = "") -> str:
    """What kind of answer the field wants, or 'sensitive' / 'unknown'.

    The label and the window title are both used: a bare "Email" on an
    internship form and "Password" in a login dialog must not be confused.
    """
    text = f"{label} {window}"
    if SENSITIVE.search(text):
        return "sensitive"
    for kind, pattern in KINDS:
        if pattern.search(text):
            return kind
    return "unknown"


@dataclass(frozen=True)
class Profile:
    """Only these details are ever suggested; all are optional."""

    name: str = ""
    email: str = ""
    phone: str = ""
    bio: str = ""
    short_bio: str = ""
    skills: tuple[str, ...] = ()
    portfolio: str = ""
    linkedin: str = ""
    github: str = ""
    headline: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None = None) -> "Profile":
        path = path or profile_path()
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        clean = {k: str(v).strip() for k, v in raw.items() if k in known and v is not None}
        if isinstance(clean.get("skills"), str):
            clean["skills"] = tuple(s.strip() for s in clean["skills"].split(",") if s.strip())
        extra = raw.get("extra")
        if isinstance(extra, dict):
            clean["extra"] = {str(k): str(v) for k, v in extra.items() if v is not None}
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in clean.items() if k in allowed})


def _variations(kind: str, profile: Profile) -> list[str]:
    skill_text = ", ".join(profile.skills)
    if kind == "email":
        base = [profile.email]
    elif kind == "phone":
        base = [profile.phone]
    elif kind == "name":
        base = [profile.name]
    elif kind == "portfolio":
        base = [p for p in (profile.portfolio, profile.github, profile.linkedin) if p]
    elif kind == "skills":
        base = [skill_text, ", ".join(profile.skills[:3])] if profile.skills else []
    elif kind == "bio":
        parts = [p for p in (profile.headline, profile.bio, profile.short_bio) if p]
        base = list(dict.fromkeys(parts))
        if profile.name and skill_text:
            base.append(f"{profile.name} — {skill_text}.")
    elif kind == "cover_letter":
        lead = profile.headline or profile.short_bio or profile.bio
        base = []
        if lead:
            base.append(f"I'm {profile.name}. {lead} I'd love to bring that to this role." if profile.name else lead)
            base.append(f"What interests me most is the chance to apply {skill_text or 'what I know'} here. {lead}".strip())
        if profile.bio:
            base.append(profile.bio)
    elif kind == "resume":
        base = [p for p in profile.extra.get("resume", "").split("|") if p.strip()]
    else:
        base = []
    # Keep order, drop blanks and duplicates, cap at three.
    out: list[str] = []
    for text in base:
        text = str(text).strip()
        if text and text not in out:
            out.append(text)
        if len(out) == 3:
            break
    return out


def suggestions(label: str, window: str = "", profile: Profile | None = None,
                control_type: str = "") -> list[str]:
    """Two or three clickable options for this field; empty for secrets."""
    kind = classify(label, window, control_type)
    if kind in {"sensitive", "unknown"}:
        return []
    profile = profile or Profile.load()
    return _variations(kind, profile)


class FieldPopup:
    """A tiny always-on-top Tk card with the suggestions as buttons."""

    def __init__(self, on_pick: Callable[[str], None]) -> None:
        import tkinter as tk

        self._tk = tk
        self._on_pick = on_pick
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.frame = tk.Frame(self.root, bg="#101820", highlightbackground="#6ff3ff",
                              highlightthickness=1)
        self.frame.pack(padx=1, pady=1)

    def show(self, label: str, options: Sequence[str], x: int, y: int) -> None:
        for child in list(self.frame.winfo_children()):
            child.destroy()
        self._tk.Label(self.frame, text=f" xenon · {label[:46]} ", fg="#6ff3ff", bg="#101820",
                       font=("Consolas", 10)).pack(anchor="w", pady=(6, 2))
        for text in options:
            button = self._tk.Button(
                self.frame, text=text[:90], wraplength=420, justify="left",
                fg="#e9f3f6", bg="#101820", activebackground="#1b2b35",
                relief="flat", anchor="w", command=lambda value=text: self._pick(value),
            )
            button.pack(fill="x", padx=6, pady=2)
        self.frame.update_idletasks()
        w, h = self.frame.winfo_reqwidth(), self.frame.winfo_reqheight()
        self.root.geometry(f"{w}x{h}+{max(0, min(x, self.root.winfo_screenwidth() - w))}+"
                           f"{max(0, min(y, self.root.winfo_screenheight() - h))}")
        self.root.deiconify()
        self.root.lift()

    def _pick(self, value: str) -> None:
        self.root.withdraw()
        self._on_pick(value)

    def run(self) -> None:
        self.root.mainloop()

    def stop(self) -> None:
        try:
            self.root.quit()
        finally:
            self.root.destroy()


def _fill(text: str) -> None:
    """Paste through the clipboard; the click is the only authorisation."""
    try:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        user = ctypes.windll.user32
        user.keybd_event(0x11, 0, 0, 0)   # Ctrl down
        user.keybd_event(0x56, 0, 0, 0)   # V
        user.keybd_event(0x56, 0, 2, 0)
        user.keybd_event(0x11, 0, 2, 0)   # Ctrl up
    except Exception:
        pass


class FieldAssistant:
    """Clicks -> classify -> offer; never types anything on its own."""

    def __init__(self, click_point: Callable[[], tuple[int, int]] | None = None,
                 field_at: Callable[[int, int], tuple[str, str]] | None = None,
                 on_pick: Callable[[str], None] | None = None,
                 mouse_down: Callable[[], bool] | None = None) -> None:
        self._click_point = click_point or _default_click_point
        self._field_at = field_at or _default_field_at
        self._on_pick = on_pick or _fill
        self._mouse_down = mouse_down or _default_mouse_down
        self._stop = threading.Event()
        self._last: tuple[int, int] | None = None
        self._popup = None

    def check(self) -> bool:
        """One poll: if a fresh click landed on a field, show its suggestions."""
        down = self._mouse_down()
        if not down:
            self._last = None
            return False
        point = self._click_point()
        if point == self._last:
            return False
        self._last = point
        label, window = self._field_at(*point)
        if not label:
            return False
        options = suggestions(label, window)
        if not options:
            return False
        if self._popup is None:
            self._popup = FieldPopup(self._on_pick)
            threading.Thread(target=self._popup.run, daemon=True, name="xenon-fields").start()
        self._popup.show(label, options, point[0] + 12, point[1] + 12)
        return True

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                pass
            self._stop.wait(0.18)

    def stop(self) -> None:
        self._stop.set()
        if self._popup is not None:
            self._popup.stop()


def _default_mouse_down() -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)


def _default_click_point() -> tuple[int, int]:
    point = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return int(point.x), int(point.y)


def _default_field_at(x: int, y: int) -> tuple[str, str]:
    """Read the editable control under a point through UI Automation."""
    try:
        from pywinauto.uia_defines import IUIA

        point = ctypes.wintypes.POINT(x, y)
        element = IUIA().ui_automation_client.ElementFromPoint(point)
        if element is None:
            return "", ""
        info = element.Current
        name = str(getattr(info, "Name", "") or "")
        control = str(getattr(info, "ControlType", "") or "")
        if "Edit" not in control and "Document" not in control:
            return "", ""
        parent = element.GetParent()
        window = str(getattr(getattr(parent, "Current", None), "Name", "") or "") if parent else ""
        return name, window
    except Exception:
        return "", ""
