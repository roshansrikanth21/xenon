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
        """The browser window whose active tab is the Spotify web player.

        The last match is remembered and checked first; when there is none,
        the full search (which asks each browser window for its address)
        runs at most every 30 seconds.
        """
        cached = getattr(self, "_cached_window", None)
        if cached is not None:
            try:
                title = (cached.window_text() or "").lower()
                if cached.is_visible() and (
                    "spotify" in title or self.address(cached).startswith("https://open.spotify.com")
                ):
                    return cached
            except Exception:
                pass
            self._cached_window = None
        now = time.monotonic()
        if now < getattr(self, "_next_search", 0.0):
            return None
        found = self._search_window()
        self._cached_window = found
        if found is None:
            self._next_search = now + 30.0
        return found

    def forget(self) -> None:
        """Search again right away (after Xenon opened a Spotify page)."""
        self._cached_window = None
        self._next_search = 0.0

    def _search_window(self):
        browsers = []
        for window in self._candidate_windows():
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
        # A window on another virtual desktop shows UIA nothing (no address),
        # but Spotify titles its tab "Song • Artist" while it plays.
        for window in browsers:
            if " • " in (window.window_text() or ""):
                return window
        return None

    def _candidate_windows(self):
        """UIA's desktop listing can skip windows (for example on another
        virtual desktop), so also look them up by handle through Win32."""
        seen = []
        try:
            seen = list(self._desktop_factory().windows())
        except Exception:
            seen = []
        if self._desktop_factory is not _uia_desktop:
            return seen  # tests inject a fake desktop
        titles = {(w.window_text() or "") for w in seen}
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            handles = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def collect(hwnd, _):
                if user32.IsWindowVisible(hwnd):
                    buffer = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, buffer, 256)
                    title = buffer.value.lower()
                    if buffer.value not in titles and any(h in title for h in BROWSER_HINTS + ("spotify",)):
                        handles.append(hwnd)
                return True

            user32.EnumWindows(collect, 0)
            desktop = self._desktop_factory()
            for hwnd in handles:
                try:
                    seen.append(desktop.window(handle=hwnd).wrapper_object())
                except Exception:
                    continue
        except Exception:
            pass
        return seen

    def _player_bar(self, window):
        """The 'Now playing bar' found natively by UI Automation (FindFirst),
        instead of walking every element of the page in Python."""
        try:
            from pywinauto.uia_defines import IUIA

            uia = IUIA()
            condition = uia.iuia.CreatePropertyCondition(30005, "Now playing bar")  # Name
            element = window.element_info.element.FindFirst(4, condition)  # descendants
            if not element:
                return None
            from pywinauto.controls.uiawrapper import UIAWrapper
            from pywinauto.uia_element_info import UIAElementInfo

            return UIAWrapper(UIAElementInfo(element))
        except Exception:
            return None

    def now_playing(self) -> dict | None:
        """Title, artist and position from the player bar, or None."""
        window = self.window()
        if window is None:
            return None
        info: dict = {"title": "", "artist": "", "position_ms": 0, "duration_ms": 0,
                      "playing": False}
        bar = self._player_bar(window)
        try:
            scopes = [bar] if bar is not None else window.descendants(control_type="Document")[:1]
            for document in scopes:
                for element in document.descendants():
                    name = (element.window_text() or "").strip()
                    kind = element.element_info.control_type
                    if not info["title"] and name.lower().startswith("now playing:"):
                        rest = name.split(":", 1)[1].strip()
                        title, _, artist = rest.rpartition(" by ")
                        info["title"], info["artist"] = (title or rest).strip(), artist.strip()
                    elif kind == "Slider" and name.lower() == "change progress":
                        try:
                            info["position_ms"] = int(element.iface_range_value.CurrentValue)
                            info["duration_ms"] = int(element.iface_range_value.CurrentMaximum)
                        except Exception:
                            pass
                    elif kind == "Button" and name.lower() == "pause":
                        info["playing"] = True
                break
        except Exception:
            return None
        return info if info["title"] else None

    @staticmethod
    def address(window) -> str:
        try:
            for document in window.descendants(control_type="Document"):
                return str(document.iface_value.CurrentValue or "")
        except Exception:
            pass
        return ""

    def wait_for_window(self, timeout_s: float = 10.0):
        self.forget()
        for _ in range(max(1, int(timeout_s / 0.4))):
            self.forget()
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
        self.forget()
        return self._control(kind)

    def _control(self, kind: str) -> bool | None:
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

    ADDRESS_NAMES = ("address field", "address and search bar", "search or enter address",
                     "search with google or enter address", "search or enter web address")

    def open_in_tab(self, url: str) -> bool:
        """Load ``url`` in the Spotify tab that is already open (switching
        to it if it's a background tab) instead of opening a new one.
        False when there is no Spotify tab or the address bar can't be set."""
        if not url.startswith("https://open.spotify.com/"):
            return False
        self.forget()
        window = self.window() or self._select_spotify_tab()
        if window is None:
            return False
        window = self._bring_here(window)
        if window is None:
            return False
        edit = None
        try:
            for candidate in window.descendants(control_type="Edit"):
                name = (candidate.window_text() or candidate.element_info.name or "").strip().lower()
                if any(name.startswith(n) for n in self.ADDRESS_NAMES):
                    edit = candidate
                    break
        except Exception:
            edit = None
        try:
            from pywinauto.keyboard import send_keys

            window.set_focus()
            if edit is not None:
                edit.set_focus()
                edit.iface_value.SetValue(url)
            else:
                # No accessible address bar: the browser shortcut does it,
                # but only if that window really has the keyboard now.
                handle = self._handle(window)
                import ctypes

                if not handle or ctypes.windll.user32.GetForegroundWindow() != handle:
                    return False
                send_keys("^l")
                self._sleep(0.15)
                send_keys("".join("{%s}" % c if c in "+^%~(){}[]" else c for c in url), pause=0)
            send_keys("{ENTER}")
        except Exception:
            return False
        self._sleep(0.8)
        self.forget()
        return True

    @staticmethod
    def _handle(window) -> int:
        try:
            return int(window.handle)
        except Exception:
            return 0

    def _bring_here(self, window):
        """None for a window on another virtual desktop: Windows cloaks it,
        UI Automation sees nothing inside, and switching desktops for a
        song was tried and is unreliable (Windows may refuse the switch,
        and Opera exposes no page to press Play on even after it)."""
        handle = self._handle(window)
        if handle:
            from .desktop_jump import is_cloaked

            if is_cloaked(handle):
                return None
        return window

    def _select_spotify_tab(self):
        """Find a background Spotify tab in any browser window and select it.
        While a song plays, Spotify titles its tab 'Song • Artist'."""
        for window in self._candidate_windows():
            title = (window.window_text() or "").lower()
            if not any(h in title for h in BROWSER_HINTS):
                continue
            try:
                tabs = window.descendants(control_type="TabItem")
            except Exception:
                continue
            for tab in tabs:
                name = (tab.window_text() or "").lower()
                if "spotify" in name or " • " in name:
                    try:
                        tab.select()
                    except Exception:
                        try:
                            window.set_focus()
                            tab.click_input()
                        except Exception:
                            continue
                    self._sleep(0.4)
                    if self.address(window).startswith("https://open.spotify.com"):
                        self._cached_window = window
                        return window
        return None

    def play(self, wanted: str, open_page: Callable[[], object] | None = None) -> str | None:
        """Press 'Play <wanted>'. Returns what played, or None if it couldn't."""
        self.forget()  # a command is worth a fresh search
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
