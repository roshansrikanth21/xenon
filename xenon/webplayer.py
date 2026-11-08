"""Press Play in the Spotify web player through UI Automation.

No developer app and no API: the open.spotify.com page exposes its buttons to
Windows accessibility ("Play Liked Songs", "Play <playlist>", the top search
result), so Xenon invokes the same button you would click. The browser keeps
your normal Spotify sign-in.

This depends on Spotify's page labels, so every step reports what it actually
saw: a press counts as playing only when the player bar's button turns into
"Pause".
"""
from __future__ import annotations

import time
from difflib import SequenceMatcher
from typing import Callable

BROWSER_HINTS = ("opera", "chrome", "edge", "firefox", "brave", "comet")


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


class WebPlayer:
    def __init__(self, desktop_factory: Callable[[], object] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._desktop_factory = desktop_factory or _uia_desktop
        self._sleep = sleep

    # ----- finding things ------------------------------------------------

    def window(self):
        """The browser window whose active tab is the Spotify web player."""
        browsers = []
        for window in self._desktop_factory().windows():
            title = (window.window_text() or "").lower()
            browser_tab = "spotify" in title and any(hint in title for hint in BROWSER_HINTS)
            desktop_app = title.strip() in {"spotify", "spotify premium", "spotify free"}
            if browser_tab or desktop_app:
                return window
            if any(hint in title for hint in BROWSER_HINTS):
                browsers.append(window)
        # While a song plays, the tab is titled after the song, so fall back
        # to the page address.
        for window in browsers:
            if self.address(window).startswith("https://open.spotify.com"):
                return window
        return None

    @staticmethod
    def address(window) -> str:
        try:
            for document in window.descendants(control_type="Document"):
                return str(document.iface_value.CurrentValue or "")
        except Exception:
            pass
        return ""

    def wait_for_window(self, timeout_s: float = 10.0):
        for _ in range(max(1, int(timeout_s / 0.4))):
            window = self.window()
            if window is not None:
                return window
            self._sleep(0.4)
        return None

    @staticmethod
    def buttons(window) -> list:
        found = []
        for document in window.descendants(control_type="Document"):
            found.extend(document.descendants(control_type="Button"))
        return found

    def find_play(self, window, wanted: str, timeout_s: float = 8.0, min_score: float = 0.55):
        """The 'Play <wanted>' button that best matches, waiting for the page."""
        attempts = max(1, int(timeout_s / 0.5))
        target = f"play {wanted}".lower()
        for attempt in range(attempts):
            best, score = None, 0.0
            for button in self.buttons(window):
                name = (button.window_text() or "").strip()
                lowered = name.lower()
                if not lowered.startswith("play ") or lowered == "play":
                    continue
                current = 1.0 if lowered == target else _ratio(lowered, target)
                if current > score:
                    best, score = button, current
            if best is not None and score >= min_score:
                return best
            if attempt < attempts - 1:
                self._sleep(0.5)
        return None

    def is_playing(self, window) -> bool:
        """The player bar shows 'Pause' while something plays."""
        for button in self.buttons(window):
            if (button.window_text() or "").strip().lower() == "pause":
                return True
        return False

    # ----- actions -------------------------------------------------------

    CONTROLS = {"pause": "pause", "resume": "play", "next": "next", "previous": "previous"}

    def control(self, kind: str) -> bool | None:
        """Press the player bar's own button. None when no web player is open.

        Browsers do not always route Windows media keys to the Spotify tab
        (Opera ignores them), so this is tried first.
        """
        window = self.window()
        if window is None:
            return None
        wanted = self.CONTROLS[kind]
        if kind == "pause" and not self.is_playing(window):
            return True  # already paused
        if kind == "resume" and self.is_playing(window):
            return True
        for button in self.buttons(window):
            if (button.window_text() or "").strip().lower() == wanted:
                try:
                    button.invoke()
                except Exception:
                    return False
                if kind in {"pause", "resume"}:
                    for _ in range(6):
                        self._sleep(0.3)
                        if self.is_playing(window) == (kind == "resume"):
                            return True
                    return False
                return True
        return False

    def play(self, wanted: str, open_page: Callable[[], object] | None = None) -> str | None:
        """Press 'Play <wanted>'. Returns what played, or None if it couldn't."""
        window = self.window()
        if window is None and open_page is not None:
            open_page()
            window = self.wait_for_window()
        if window is None:
            return None
        button = self.find_play(window, wanted)
        if button is None:
            return None
        label = (button.window_text() or "").strip()[5:]
        try:
            button.invoke()
        except Exception:
            try:
                button.click_input()
            except Exception:
                return None
        for _ in range(8):
            self._sleep(0.4)
            if self.is_playing(window):
                return label
        return None


def _uia_desktop():
    from pywinauto import Desktop

    return Desktop(backend="uia")
