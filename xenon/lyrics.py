"""Synced lyrics from LRCLIB (https://lrclib.net): a free, open lyrics
database with line timestamps. No account or key.

Lyrics are fetched for display on the user's own companion screen only, and
cached in memory per track so each song is looked up once.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

API = "https://lrclib.net/api"
USER_AGENT = "xenon-assistant/0.1 (https://github.com/roshansrikanth21/xenon)"
_LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")


@dataclass
class Lyrics:
    lines: list[tuple[int, str]] = field(default_factory=list)  # (ms, text)
    plain: str = ""
    instrumental: bool = False

    @property
    def synced(self) -> bool:
        return bool(self.lines)


def parse_lrc(text: str) -> list[tuple[int, str]]:
    lines = []
    for raw in text.splitlines():
        match = _LRC_LINE.match(raw.strip())
        if match:
            minutes, seconds, words = match.groups()
            lines.append((int((int(minutes) * 60 + float(seconds)) * 1000), words.strip()))
    return sorted(lines)


def clean_title(title: str) -> str:
    """'Song (feat. X) - Remastered 2011' -> 'Song', for a better match."""
    title = re.sub(r"\s*[\(\[](?:feat|ft|with|prod)[^\)\]]*[\)\]]", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+-\s+.*(?:remaster|version|edit|mix|live|mono|stereo).*$", "", title, flags=re.IGNORECASE)
    return title.strip()


def _get(path: str, params: dict, timeout: float = 6.0):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


class LyricsFinder:
    def __init__(self, fetch: Callable[[str, dict], object] | None = None) -> None:
        self._fetch = fetch or _get
        self._cache: dict[tuple[str, str], Lyrics | None] = {}

    def find(self, title: str, artist: str, duration_ms: int = 0) -> Lyrics | None:
        key = (title.lower(), artist.lower())
        if key in self._cache:
            return self._cache[key]
        result = None
        try:
            result = self._lookup(title, artist, duration_ms)
        except Exception:
            return None  # network trouble: do not cache, try again later
        self._cache[key] = result
        return result

    def _lookup(self, title: str, artist: str, duration_ms: int) -> Lyrics | None:
        first_artist = re.split(r",|&| x | feat\.? ", artist, flags=re.IGNORECASE)[0].strip()
        params = {"track_name": clean_title(title), "artist_name": first_artist}
        if duration_ms:
            params["duration"] = round(duration_ms / 1000)
        data = self._fetch("get", params)
        if not data:
            # Exact match failed (remixes, mashups): try a search, pick the
            # closest duration among the synced results.
            results = self._fetch("search", {"q": f"{clean_title(title)} {first_artist}"}) or []
            synced = [r for r in results if isinstance(r, dict) and r.get("syncedLyrics")]
            if not synced:
                return None
            target = duration_ms / 1000 if duration_ms else None
            data = min(synced, key=lambda r: abs((r.get("duration") or 0) - target)) if target else synced[0]
        if data.get("instrumental"):
            return Lyrics(instrumental=True)
        return Lyrics(parse_lrc(data.get("syncedLyrics") or ""), data.get("plainLyrics") or "")
