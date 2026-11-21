"""Speak a place, land in it: deterministic navigation of the local machine.

Only well-known folders are resolved — nothing here deletes, moves or writes
anything, and an unknown place is never guessed into an arbitrary path.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    key: str
    path: str
    label: str


def places() -> dict[str, Place]:
    """The folders Xenon knows, resolved from the current user's environment."""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    raw = {
        "desktop": ("Desktop", "Desktop"),
        "documents": ("Documents", "Documents"),
        "downloads": ("Downloads", "Downloads"),
        "pictures": ("Pictures", "Pictures"),
        "photos": ("Pictures", "Pictures"),
        "music": ("Music", "Music"),
        "videos": ("Videos", "Videos"),
        "home": ("", "Home folder"),
        "user profile": ("", "Home folder"),
    }
    out = {}
    for key, (folder, label) in raw.items():
        path = os.path.join(home, folder) if folder else home
        out[key] = Place(key, path, label)
    return out


_LEAD = re.compile(
    r"^(?:(?:can you|could you|please|would you)\s+)?"
    r"(?:go to|take me to|navigate to|open|show me|bring up|switch to)\s+",
    re.IGNORECASE,
)
_TAIL = re.compile(r"\s*(?:folder|directory|please|for me|[.!?])+$", re.IGNORECASE)


def parse(text: str) -> Place | None:
    """Resolve 'go to downloads', 'open my documents folder', 'desktop' etc."""
    phrase = " ".join((text or "").split())
    phrase = _LEAD.sub("", phrase)
    phrase = re.sub(r"^(?:the|my|me)\s+", "", phrase, flags=re.IGNORECASE)
    phrase = _TAIL.sub("", phrase).strip().lower()
    if not phrase or len(phrase.split()) > 3:
        return None
    return places().get(phrase)


def open_place(place: Place, opener=None) -> bool:
    """Open a folder in File Explorer. Returns False if it does not exist."""
    if not place.path or not os.path.isdir(place.path):
        return False
    try:
        if opener is not None:
            opener(place.path)
        elif hasattr(os, "startfile"):
            os.startfile(place.path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["explorer.exe", place.path])
        return True
    except Exception:
        return False


def run(text: str, opener=None) -> str | None:
    """One-shot: parse a spoken request and open it. None means 'not for me'."""
    place = parse(text)
    if place is None:
        return None
    return f"Opening {place.label}." if open_place(place, opener) else f"I can't find {place.label}."
