"""Full-size album art for the widget.

Windows' media session hands over whatever thumbnail the player published,
often tiny (a browser tab's is 64-150 px), which looks soft at orb size.
This finds the real cover: from Spotify's Web API when you are logged in
(640 px, exactly the playing track), otherwise from Apple's public iTunes
Search API (up to 1000 px, no key). A result is only used when both title
and artist match, and images are only fetched from those services' CDNs.
"""
from __future__ import annotations

import io
import json
import threading
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from typing import Callable

from PIL import Image

_HOSTS = (".scdn.co", ".spotifycdn.com", ".mzstatic.com")
_UA = "xenon-music-widget/1.0"
SIZE = 640


def _get(url: str, limit: int = 3 * 1024 * 1024) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - fixed https hosts
        return response.read(limit)


def _host_ok(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host.endswith(h) for h in _HOSTS)


def _norm(text: str) -> str:
    import re

    text = re.sub(r"\s*[\(\[].*?[\)\]]", "", text or "")       # (feat. x), [Remastered]
    text = re.split(r"\s+-\s+", text)[0]                        # "Song - 2011 Remaster"
    return " ".join(text.lower().split())


def _match(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


class CoverFinder:
    """``request(title, artist)`` looks the cover up in the background;
    ``take(title, artist)`` returns it once found (None until then / if
    nothing matched)."""

    def __init__(self, fetch: Callable[[str], bytes] | None = None,
                 spotify_now: Callable[[], dict | None] | None = None) -> None:
        self._fetch = fetch or _get
        self._spotify_now = spotify_now if spotify_now is not None else _spotify_now
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], Image.Image | None] = {}
        self._pending: set[tuple[str, str]] = set()

    @staticmethod
    def _key(title: str, artist: str) -> tuple[str, str]:
        return (_norm(title), _norm(artist))

    def take(self, title: str, artist: str) -> Image.Image | None:
        with self._lock:
            return self._cache.get(self._key(title, artist))

    def request(self, title: str, artist: str) -> None:
        key = self._key(title, artist)
        if not key[0]:
            return
        with self._lock:
            if key in self._cache or key in self._pending:
                return
            self._pending.add(key)
        threading.Thread(target=self._work, args=(title, artist, key), daemon=True,
                         name="xenon-cover").start()

    def _work(self, title: str, artist: str, key) -> None:
        image = None
        try:
            image = self.find(title, artist)
        except Exception:
            image = None
        with self._lock:
            self._cache[key] = image
            self._pending.discard(key)
            if len(self._cache) > 64:
                self._cache.pop(next(iter(self._cache)))

    def find(self, title: str, artist: str) -> Image.Image | None:
        for source in (self._from_spotify, self._from_itunes):     # lazily: iTunes only if needed
            url = source(title, artist)
            if url and _host_ok(url):
                try:
                    image = Image.open(io.BytesIO(self._fetch(url))).convert("RGB")
                except Exception:
                    continue
                if image.width >= 300:
                    if image.width > SIZE:
                        image = image.resize((SIZE, SIZE * image.height // image.width), Image.LANCZOS)
                    return image
        return None

    def _from_spotify(self, title: str, artist: str) -> str | None:
        try:
            now = self._spotify_now()
        except Exception:
            return None
        item = (now or {}).get("item") if isinstance(now, dict) else None
        if not isinstance(item, dict):
            return None
        names = [a.get("name", "") for a in item.get("artists", []) if isinstance(a, dict)]
        if _match(item.get("name", ""), title) < 0.8 or not any(_match(n, artist) >= 0.7 for n in names):
            return None
        images = (item.get("album") or {}).get("images") or []
        images = [i for i in images if isinstance(i, dict) and i.get("url")]
        if not images:
            return None
        return max(images, key=lambda i: i.get("width") or 0)["url"]

    def _from_itunes(self, title: str, artist: str) -> str | None:
        if not (artist or "").strip():
            return None
        term = urllib.parse.urlencode({"term": f"{_norm(title)} {_norm(artist)}", "entity": "song", "limit": 5})
        try:
            data = json.loads(self._fetch(f"https://itunes.apple.com/search?{term}").decode("utf-8", "replace"))
        except Exception:
            return None
        for row in data.get("results", []) if isinstance(data, dict) else []:
            if not isinstance(row, dict):
                continue
            if _match(row.get("trackName", ""), title) >= 0.8 and _match(row.get("artistName", ""), artist) >= 0.7:
                url = str(row.get("artworkUrl100") or "")
                return url.replace("100x100bb", "1000x1000bb") if url else None
        return None


def _spotify_now() -> dict | None:
    """The logged-in user's currently playing item, or None (no login)."""
    from .music import SpotifyClient, load_tokens

    tokens = load_tokens()
    if not tokens:
        return None
    return SpotifyClient(tokens).now_playing()
