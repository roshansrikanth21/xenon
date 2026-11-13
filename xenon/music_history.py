"""Songs the widget has seen, for the dock's "play it again" list.

Recorded from the Windows media session as tracks change: title, artist,
the playing app and a small cover thumbnail. Nothing leaves the machine; the
list lives in ``%APPDATA%\\xenon\\music_history.json`` with thumbnails beside
it in ``covers\\``.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass

MAX_ENTRIES = 40
THUMB = 96


@dataclass(frozen=True)
class HistoryEntry:
    title: str
    artist: str
    app: str = ""
    cover: str = ""        # file name inside the covers directory, or ""
    played_at: float = 0.0

    @property
    def key(self) -> tuple[str, str]:
        return (self.title.strip().lower(), self.artist.strip().lower())


def history_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "xenon")


class MusicHistory:
    def __init__(self, directory: str | None = None) -> None:
        self.directory = directory or history_dir()
        self.path = os.path.join(self.directory, "music_history.json")
        self.covers = os.path.join(self.directory, "covers")
        self._lock = threading.Lock()
        self._entries: list[HistoryEntry] = self._load()
        self._thumbs: dict[str, object] = {}

    def _load(self) -> list[HistoryEntry]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return []
        entries = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
                continue
            entries.append(HistoryEntry(
                title=item["title"], artist=str(item.get("artist", "")), app=str(item.get("app", "")),
                cover=os.path.basename(str(item.get("cover", ""))),
                played_at=float(item.get("played_at", 0) or 0),
            ))
        return entries[:MAX_ENTRIES]

    def entries(self) -> list[HistoryEntry]:
        with self._lock:
            return list(self._entries)

    def recent(self, exclude: tuple[str, str] | None = None, limit: int = 5) -> list[HistoryEntry]:
        out = [e for e in self.entries() if e.key != exclude]
        return out[:limit]

    def record(self, title: str, artist: str, app: str = "", cover=None) -> HistoryEntry | None:
        """Move (or add) a track to the top. ``cover`` is a PIL image."""
        if not title or not title.strip():
            return None
        name = ""
        if cover is not None:
            name = hashlib.sha1(f"{title}\x00{artist}".encode("utf-8", "replace")).hexdigest()[:16] + ".png"
            try:
                os.makedirs(self.covers, exist_ok=True)
                thumb = cover.convert("RGB")
                thumb.thumbnail((THUMB, THUMB))
                thumb.save(os.path.join(self.covers, name))
                self._thumbs.pop(name, None)
            except (OSError, ValueError):
                name = ""
        entry = HistoryEntry(title.strip(), (artist or "").strip(), app, name, time.time())
        with self._lock:
            old = next((e for e in self._entries if e.key == entry.key), None)
            if old is not None and not name:
                entry = HistoryEntry(entry.title, entry.artist, entry.app, old.cover, entry.played_at)
            self._entries = [entry] + [e for e in self._entries if e.key != entry.key]
            dropped = self._entries[MAX_ENTRIES:]
            self._entries = self._entries[:MAX_ENTRIES]
            snapshot = list(self._entries)
        self._save(snapshot, dropped)
        return entry

    def _save(self, entries: list[HistoryEntry], dropped: list[HistoryEntry]) -> None:
        try:
            os.makedirs(self.directory, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump([asdict(e) for e in entries], handle, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            return
        keep = {e.cover for e in entries}
        for entry in dropped:
            if entry.cover and entry.cover not in keep:
                try:
                    os.remove(os.path.join(self.covers, entry.cover))
                except OSError:
                    pass

    def thumbnail(self, entry: HistoryEntry):
        """The saved cover as a PIL image, or None."""
        if not entry.cover:
            return None
        cached = self._thumbs.get(entry.cover)
        if cached is not None:
            return cached
        try:
            from PIL import Image

            with Image.open(os.path.join(self.covers, entry.cover)) as image:
                thumb = image.convert("RGB")
        except (OSError, ValueError):
            return None
        self._thumbs[entry.cover] = thumb
        return thumb
