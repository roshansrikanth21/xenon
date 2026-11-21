"""One name for the wake word and everything the user sees.

Set ``XENON_NAME=Rose`` (or just ``XENON_WAKE_WORD=rose``) and the orb, its
notch, the tray and the 3D core all say "Rose" instead of "Xenon". The wake
word itself is always lower-case; display forms are title/upper case.
"""
from __future__ import annotations

import os

DEFAULT = "xenon"


def _clean(value: str) -> str:
    """Turn 'Rose!' / 'rose_girl' into plain words, without punctuation."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in value).split())


def wake_word() -> str:
    raw = os.environ.get("XENON_NAME") or os.environ.get("XENON_WAKE_WORD") or DEFAULT
    return _clean(raw).lower() or DEFAULT


def display_name() -> str:
    return wake_word().title()


def standby_hint() -> str:
    return f"say {wake_word()}"
