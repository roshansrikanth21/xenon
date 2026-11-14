"""Song suggestions for the corner dock: more from the artist you are
playing and from similar artists, with their covers.

Uses Deezer's public catalogue API (no account, no key): find the artist,
then their top tracks and their related artists' top tracks. Only the artist
and title go out; covers come from Deezer's image CDN. Everything runs on a
background thread and is cached per artist, so the dock never waits on the
network. ``fetch`` is injectable for tests.
"""
from __future__ import annotations

import io
import json
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from PIL import Image

API = "https://api.deezer.com"
_IMAGE_HOSTS = (".dzcdn.net", ".deezer.com")
_MAX_IMAGE_BYTES = 512 * 1024
_UA = "xenon-music-widget/1.0"


@dataclass(frozen=True)
class Suggestion:
    title: str
    artist: str
    cover: Image.Image | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.title.strip().lower(), self.artist.strip().lower())


def _http_get(url: str, limit: int = 2 * 1024 * 1024) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - fixed https hosts
        return response.read(limit)


def _image_ok(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host.endswith(h) for h in _IMAGE_HOSTS)


class Suggester:
    """``request(title, artist)`` starts a lookup; ``current()`` returns the
    latest suggestions (possibly for an earlier request, or empty)."""

    def __init__(self, fetch: Callable[[str], bytes] | None = None, per_artist: int = 2,
                 related: int = 4) -> None:
        self._fetch = fetch or _http_get
        self._per_artist = per_artist
        self._related = related
        self._lock = threading.Lock()
        self._cache: dict[str, list[Suggestion]] = {}
        self._wanted: tuple[str, str] | None = None
        self._current: list[Suggestion] = []
        self._busy = False
        self._pending = None
        self.version = 0          # bumps whenever ``current()`` changes

    def current(self) -> list[Suggestion]:
        with self._lock:
            return list(self._current)

    def request(self, title: str, artist: str, fallbacks: list[tuple[str, str]] | None = None) -> None:
        """Look up suggestions for ``artist``; if Deezer does not know it
        (a podcast, a YouTube video), try ``fallbacks`` (recent songs)."""
        queue = [(title, artist)] + list(fallbacks or [])
        queue = [(t, primary_artist(a)) for t, a in queue if primary_artist(a)]
        if not queue:
            return
        with self._lock:
            self._wanted = queue[0]
            cached = self._cache.get(queue[0][1].strip().lower())
            if cached is not None:
                self._set(cached)
                return
            if self._busy:
                self._pending = queue
                return
            self._busy = True
        threading.Thread(target=self._work, args=(queue,), daemon=True, name="xenon-suggest").start()

    def _set(self, items: list[Suggestion]) -> None:
        self._current = items
        self.version += 1

    def _work(self, queue) -> None:
        while True:
            result: list[Suggestion] = []
            for title, artist in queue[:4]:
                key = artist.strip().lower()
                if key in self._cache:
                    result = self._cache[key]
                else:
                    try:
                        result = self.lookup(title, artist)
                    except Exception:
                        result = []
                    with self._lock:
                        self._cache[key] = result
                if result:
                    break
            with self._lock:
                if result or not self._current:
                    self._set(result)
                pending = getattr(self, "_pending", None)
                self._pending = None
                if not pending:
                    self._busy = False
                    return
                queue = pending

    # -- Deezer ---------------------------------------------------------------
    def _json(self, path: str, **params) -> dict:
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.loads(self._fetch(url).decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}

    def _cover(self, track: dict) -> Image.Image | None:
        album = track.get("album") or {}
        url = album.get("cover_medium") or album.get("cover_small") or ""
        if not isinstance(url, str) or not _image_ok(url):
            return None
        try:
            raw = self._fetch(url)
            if len(raw) >= _MAX_IMAGE_BYTES:
                return None
            image = Image.open(io.BytesIO(raw))
            image.thumbnail((128, 128))
            return image.convert("RGB")
        except Exception:
            return None

    def _top(self, artist_id: int, limit: int) -> list[dict]:
        rows = self._json(f"/artist/{int(artist_id)}/top", limit=limit).get("data") or []
        return [r for r in rows if isinstance(r, dict)]

    def lookup(self, title: str, artist: str) -> list[Suggestion]:
        found = self._json("/search/artist", q=artist, limit=1).get("data") or []
        if not found or not isinstance(found[0], dict):
            return []
        hit = found[0]
        if _similar(str(hit.get("name", "")), artist) < 0.6:
            return []      # Deezer guessed some other artist: no suggestions
        artist_id = int(hit.get("id", 0))
        # The artist's "radio" mixes their songs with similar artists' and,
        # unlike /top, still lists tracks in regions Deezer doesn't serve.
        radio = self._json(f"/artist/{artist_id}/radio", limit=12).get("data") or []
        picks = [t for t in radio if isinstance(t, dict)]
        if not picks:
            picks = self._top(artist_id, 6)
        playing = title.strip().lower()
        out, seen = [], {(playing, artist.strip().lower())}
        for track in picks:
            if len(out) >= self._per_artist + self._related:
                break
            name = str(track.get("title_short") or track.get("title") or "").strip()
            who = str((track.get("artist") or {}).get("name") or "").strip()
            if not name or (name.lower(), who.lower()) in seen:
                continue
            seen.add((name.lower(), who.lower()))
            out.append(Suggestion(name, who, self._cover(track)))
        return out


def primary_artist(artist: str) -> str:
    """'Don Toliver, Travis Scott' / 'A feat. B' / 'A & B' -> 'Don Toliver' / 'A'."""
    import re

    name = re.split(r",| feat\.? | ft\.? | featuring | x | & ", artist or "", maxsplit=1, flags=re.I)[0]
    return name.strip()


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()
