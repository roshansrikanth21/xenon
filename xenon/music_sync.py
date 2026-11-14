"""Exact playback position from Spotify, for lyric timing.

The Windows media session reports the browser's position only now and
then, and its estimate can sit a few hundred ms off the audio. When the
user is logged in to Spotify (``xenon spotify login``), the official Web API
``/me/player/currently-playing`` gives ``progress_ms``; read every few
seconds off the render threads and timed to the middle of the request, it
anchors the lyric clock. Without a login this simply never answers and
the widget keeps the media-session position.
"""
from __future__ import annotations

import threading
import time
from typing import Callable


def _norm(text: str) -> str:
    from .music_cover import _norm as norm

    return norm(text or "")


def _spotify_now() -> dict | None:
    from .music_cover import _spotify_now as now

    return now()


class SpotifyProgress:
    INTERVAL = 4.0        # seconds between reads while playing
    FRESH = 15.0          # a reading older than this is not used

    def __init__(self, fetch: Callable[[], dict | None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch or _spotify_now
        self._clock = clock
        self._data: tuple[str, int, bool, float] | None = None
        self._busy = False
        self._next = 0.0
        self._title = None

    def poll(self, title: str, playing: bool) -> None:
        """Call freely (every frame); reads at most every few seconds, and
        soon after the song or the play state changes."""
        now = self._clock()
        changed = (title, playing) != self._title
        if self._busy or (now < self._next and not changed):
            return
        self._title = (title, playing)
        self._next = now + (self.INTERVAL if playing else 12.0)
        self._busy = True
        threading.Thread(target=self._read, daemon=True, name="xenon-spotify-progress").start()

    def _read(self) -> None:
        try:
            before = self._clock()
            data = self._fetch()
            after = self._clock()
            if not data:
                self._next = after + 60.0         # not logged in / nothing playing
                return
            item = data.get("item") or {}
            progress = data.get("progress_ms")
            if not isinstance(progress, int) or not item.get("name"):
                return
            # Spotify's figure is from somewhere inside the request: assume
            # the middle (the round trip is ~300-500 ms).
            self._data = (_norm(item["name"]), progress, bool(data.get("is_playing")), (before + after) / 2)
        except Exception:
            self._next = self._clock() + 30.0
        finally:
            self._busy = False

    def position(self, title: str) -> int | None:
        """Spotify's position now for this song, or None (no fresh reading
        for it)."""
        data = self._data
        if data is None:
            return None
        name, progress, playing, at = data
        age = self._clock() - at
        if age > self.FRESH or name != _norm(title):
            return None
        return int(progress + (age * 1000 if playing else 0))
