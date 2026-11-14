"""Local habit store: which songs he plays, when he listens, which apps he uses.

Why this exists: "play my usual" or "play that again" only make sense if
Xenon remembers what was played. Everything lives in one SQLite file
(``%APPDATA%\\xenon\\patterns.sqlite3``). Nothing is sent anywhere. The only
thing that can reach a model is the short text ``summary_for_prompt``
builds, and the caller decides whether to use it.

Design notes:

* Writes go through ONE background thread fed by a queue. The music widget
  polls from its render thread and focus changes arrive on the Tk thread,
  and neither may stall on disk I/O or SQLite locks.
* Reads use a second connection guarded by a lock. The file is in WAL mode,
  so reads never wait on the writer.
* Every public method swallows exceptions and reports the first one on
  stderr. Learning habits is a nice-to-have and must never break the
  assistant.
"""
from __future__ import annotations

import os
import queue
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from xenon import untrusted
from xenon.music_suggest import primary_artist

# A play counts once this much of it was heard; shorter means it was skipped.
PLAY_MS = 30_000
SKIP_MIN_MS = 3_000
# A poller that stalls (sleep, debugger, a busy Tk loop) must not credit
# minutes of listening it never saw.
MAX_GAP_S = 5.0
MIN_FOCUS_S = 2.0
_NAME_CAP = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY, ts REAL, title TEXT, artist TEXT, album TEXT, app TEXT,
    hour INTEGER, weekday INTEGER, listened_ms INTEGER, skipped INTEGER NULL,
    source TEXT, track_id TEXT NULL);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY, ts REAL, text TEXT, kind TEXT, ok INTEGER, origin TEXT);
CREATE TABLE IF NOT EXISTS app_focus (
    id INTEGER PRIMARY KEY, start REAL, "end" REAL, exe TEXT);
CREATE TABLE IF NOT EXISTS spotify_top (
    kind TEXT, time_range TEXT, rank INTEGER, name TEXT, artist TEXT, fetched REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS plays_ts ON plays(ts);
CREATE INDEX IF NOT EXISTS plays_artist ON plays(artist);
"""
_TABLES = ("plays", "requests", "app_focus", "spotify_top", "meta")


def default_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / "xenon" / "patterns.sqlite3"


def _clean(text: object, cap: int = _NAME_CAP) -> str:
    """Make a heard or played name safe for a prompt: no invisible or
    control characters, a single line, and a bounded length."""
    text = " ".join(untrusted.strip_invisible(str(text or "")).split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def _exe_name(exe: str | None) -> str:
    name = re.split(r"[\\/]", str(exe or "").strip())[-1].strip().lower()
    return name


def _app_label(exe: str) -> str:
    return exe[:-4] if exe.endswith(".exe") else exe


def _hours_around(hour: int, spread: int = 2) -> list[int]:
    return sorted({(hour + d) % 24 for d in range(-spread, spread + 1)})


def _part_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


class _Job:
    """A unit of work for the writer thread. ``done`` is set afterwards, so
    a caller that needs the result (forget, add_spotify_plays) can wait."""

    __slots__ = ("fn", "done", "result")

    def __init__(self, fn: Callable[[sqlite3.Connection], object] | None) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: object = None


class PatternStore:
    def __init__(
        self,
        path: Path | None = None,
        enabled: Callable[[], bool] | None = None,
        *,
        open_db: bool = True,
    ) -> None:
        """``open_db=False`` gives a store that never touches the disk, for
        when the user turned learning off entirely (XENON_PATTERNS=off)."""
        self.path = Path(path) if path else default_path()
        self._enabled = enabled
        self._logged = False
        self._state_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._current: dict | None = None      # the track being listened to
        self._focus: tuple[str, float] | None = None
        self._queue: "queue.Queue[_Job]" = queue.Queue()
        self._reader: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        if not open_db:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            reader = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
            reader.execute("PRAGMA journal_mode=WAL")
            reader.executescript(_SCHEMA)
            reader.commit()
            self._reader = reader
            self._thread = threading.Thread(target=self._writer, daemon=True, name="xenon-patterns")
            self._thread.start()
        except Exception as error:  # noqa: BLE001 - learning must never break Xenon
            self._log("open", error)
            self._reader = None

    # ------------------------------------------------------------ plumbing --
    def _log(self, where: str, error: BaseException) -> None:
        if self._logged:
            return
        self._logged = True
        try:
            print(f"xenon.patterns: {where} failed ({type(error).__name__}: {error})", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass

    def _on(self) -> bool:
        if self._reader is None or self._closed:
            return False
        if self._enabled is None:
            return True
        try:
            return bool(self._enabled())
        except Exception as error:  # noqa: BLE001
            self._log("enabled()", error)
            return False

    def _writer(self) -> None:
        try:
            conn = sqlite3.connect(str(self.path), timeout=10)
        except Exception as error:  # noqa: BLE001
            self._log("writer open", error)
            conn = None
        while True:
            job = self._queue.get()
            if job.fn is None:
                job.done.set()
                if job.result == "stop":
                    break
                continue
            try:
                if conn is not None:
                    job.result = job.fn(conn)
                    conn.commit()
            except Exception as error:  # noqa: BLE001
                self._log("write", error)
                try:
                    conn.rollback()  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    pass
            finally:
                job.done.set()
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _submit(self, fn: Callable[[sqlite3.Connection], object]) -> _Job | None:
        if self._thread is None or self._closed:
            return None
        job = _Job(fn)
        self._queue.put(job)
        return job

    def _read(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._reader is None:
            return []
        try:
            with self._read_lock:
                return list(self._reader.execute(sql, params).fetchall())
        except Exception as error:  # noqa: BLE001
            self._log("read", error)
            return []

    def _meta(self, key: str) -> str | None:
        rows = self._read("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    # --------------------------------------------------------------- plays --
    def track_update(self, title: str, artist: str, album: str = "", app: str = "",
                     playing: bool = True, video: bool = False, now: float | None = None) -> None:
        """Feed the current track about once a second; plays are inferred
        from how long each track was actually heard."""
        try:
            now = time.time() if now is None else float(now)
            if not self._on():
                with self._state_lock:
                    self._current = None
                return
            title = str(title or "").strip()
            artist = str(artist or "").strip()
            with self._state_lock:
                current = self._current
                if video or not title:
                    self._close_current(now)
                    return
                key = (title.lower(), artist.lower())
                if current is not None and current["key"] == key:
                    self._credit(current, now)
                    current["playing"] = bool(playing)
                    if album and not current["album"]:
                        current["album"] = str(album)
                    return
                self._close_current(now)
                self._current = {
                    "key": key, "title": title, "artist": artist, "album": str(album or ""),
                    "app": str(app or ""), "start": now, "last": now, "ms": 0.0,
                    "playing": bool(playing),
                }
        except Exception as error:  # noqa: BLE001
            self._log("track_update", error)

    @staticmethod
    def _credit(current: dict, now: float) -> None:
        """Count the time since the last poll if the track was playing then."""
        gap = max(0.0, now - current["last"])
        if current["playing"]:
            current["ms"] += min(gap, MAX_GAP_S) * 1000.0
        current["last"] = max(current["last"], now)

    def _close_current(self, now: float) -> None:
        """Turn the tracked song into a play or skip row. Caller holds the
        state lock."""
        current, self._current = self._current, None
        if current is None:
            return
        self._credit(current, now)
        listened = int(current["ms"])
        if listened >= PLAY_MS:
            skipped = 0
        elif listened >= SKIP_MIN_MS:
            skipped = 1
        else:
            return
        ts = current["start"]
        local = time.localtime(ts)
        row = (ts, current["title"], primary_artist(current["artist"]), current["album"],
               current["app"], local.tm_hour, local.tm_wday, listened, skipped)

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO plays (ts, title, artist, album, app, hour, weekday, listened_ms,"
                " skipped, source, track_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'local', NULL)",
                row,
            )

        self._submit(write)

    def finish(self, now: float | None = None) -> None:
        """Close the current track, e.g. on quit, so its play is not lost."""
        try:
            now = time.time() if now is None else float(now)
            with self._state_lock:
                if self._on():
                    self._close_current(now)
                else:
                    self._current = None
        except Exception as error:  # noqa: BLE001
            self._log("finish", error)

    # ------------------------------------------------------------ requests --
    def log_request(self, text: str, kind: str, ok: bool = True, origin: str = "",
                    now: float | None = None) -> None:
        try:
            if not self._on():
                return
            row = (time.time() if now is None else float(now), _clean(text, 300),
                   _clean(kind, 40), 1 if ok else 0, _clean(origin, 40))
            self._submit(lambda conn: conn.execute(
                "INSERT INTO requests (ts, text, kind, ok, origin) VALUES (?, ?, ?, ?, ?)", row))
        except Exception as error:  # noqa: BLE001
            self._log("log_request", error)

    # ----------------------------------------------------------- app focus --
    def focus(self, exe: str | None, now: float | None = None) -> None:
        """A new foreground exe closes the previous interval; None closes it
        (game mode, quit). Only the exe name is kept, never window titles."""
        try:
            now = time.time() if now is None else float(now)
            name = _exe_name(exe)
            with self._state_lock:
                if not self._on():
                    self._focus = None
                    return
                if self._focus is not None and self._focus[0] == name:
                    return
                previous, self._focus = self._focus, ((name, now) if name else None)
            if previous is not None and now - previous[1] >= MIN_FOCUS_S:
                row = (previous[1], now, previous[0])
                self._submit(lambda conn: conn.execute(
                    'INSERT INTO app_focus (start, "end", exe) VALUES (?, ?, ?)', row))
        except Exception as error:  # noqa: BLE001
            self._log("focus", error)

    # -------------------------------------------------------------- import --
    def import_history(self, entries: list) -> None:
        """One-time seed from the widget's music_history.json, so habits are
        not empty on the first day. A meta flag makes later calls no-ops."""
        try:
            if not self._on():
                return
            rows = []
            for entry in entries or []:
                get = entry.get if isinstance(entry, dict) else (lambda k, d=None, e=entry: getattr(e, k, d))
                title = str(get("title", "") or "").strip()
                try:
                    ts = float(get("played_at", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if not title or ts <= 0:
                    continue
                local = time.localtime(ts)
                rows.append((ts, title, primary_artist(str(get("artist", "") or "")), "",
                             str(get("app", "") or ""), local.tm_hour, local.tm_wday))

            def write(conn: sqlite3.Connection) -> int:
                if conn.execute("SELECT 1 FROM meta WHERE key = 'history_imported'").fetchone():
                    return 0
                conn.executemany(
                    "INSERT INTO plays (ts, title, artist, album, app, hour, weekday, listened_ms,"
                    " skipped, source, track_id) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL,"
                    " 'history_import', NULL)", rows)
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('history_imported', ?)",
                             (str(time.time()),))
                return len(rows)

            self._submit(write)
        except Exception as error:  # noqa: BLE001
            self._log("import_history", error)

    # ------------------------------------------------------------- spotify --
    def add_spotify_plays(self, rows: list[dict], timeout: float = 5.0) -> int:
        """Store Spotify's recently-played rows; returns how many were new.

        Deduped on (track_id, ts). A row is also skipped when the local
        tracker already recorded the same song within 15 minutes before or
        5 minutes after it, since the same play then shows up from both
        sources. Spotify's played_at is close to when the track finished,
        and the local row is stamped when it started.
        """
        try:
            if not self._on():
                return 0
            clean = []
            for row in rows or []:
                try:
                    ts = float(row.get("played_at") or 0)
                except (TypeError, ValueError, AttributeError):
                    continue
                title = str(row.get("title") or "").strip()
                if ts <= 0 or not title:
                    continue
                local = time.localtime(ts)
                clean.append((ts, title, primary_artist(str(row.get("artist") or "")),
                              str(row.get("album") or ""), local.tm_hour, local.tm_wday,
                              str(row.get("track_id") or "")))

            def write(conn: sqlite3.Connection) -> int:
                added, newest = 0, 0.0
                for ts, title, artist, album, hour, wday, track_id in clean:
                    newest = max(newest, ts)
                    if conn.execute("SELECT 1 FROM plays WHERE source = 'spotify' AND track_id = ?"
                                    " AND ts = ?", (track_id, ts)).fetchone():
                        continue
                    if conn.execute(
                        "SELECT 1 FROM plays WHERE source = 'local' AND lower(title) = lower(?)"
                        " AND lower(artist) = lower(?) AND ts BETWEEN ? AND ?",
                        (title, artist, ts - 900, ts + 300),
                    ).fetchone():
                        continue
                    conn.execute(
                        "INSERT INTO plays (ts, title, artist, album, app, hour, weekday, listened_ms,"
                        " skipped, source, track_id) VALUES (?, ?, ?, ?, 'spotify', ?, ?, 0, 0,"
                        " 'spotify', ?)", (ts, title, artist, album, hour, wday, track_id))
                    added += 1
                if newest:
                    old = conn.execute("SELECT value FROM meta WHERE key = 'spotify_last_ts'").fetchone()
                    if not old or float(old[0]) < newest:
                        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES"
                                     " ('spotify_last_ts', ?)", (repr(newest),))
                return added

            job = self._submit(write)
            if job is None or not job.done.wait(timeout):
                return 0
            return int(job.result or 0)
        except Exception as error:  # noqa: BLE001
            self._log("add_spotify_plays", error)
            return 0

    def set_spotify_top(self, kind: str, time_range: str, items: list[tuple[str, str]],
                        now: float | None = None) -> None:
        try:
            if not self._on():
                return
            fetched = time.time() if now is None else float(now)
            rows = [(kind, time_range, rank, str(name), str(artist or ""), fetched)
                    for rank, (name, artist) in enumerate(items or [], start=1) if name]

            def write(conn: sqlite3.Connection) -> None:
                conn.execute("DELETE FROM spotify_top WHERE kind = ? AND time_range = ?",
                             (kind, time_range))
                conn.executemany("INSERT INTO spotify_top (kind, time_range, rank, name, artist,"
                                 " fetched) VALUES (?, ?, ?, ?, ?, ?)", rows)

            self._submit(write)
        except Exception as error:  # noqa: BLE001
            self._log("set_spotify_top", error)

    def _spotify_top_artists(self, time_range: str = "short_term", limit: int = 3) -> list[str]:
        rows = self._read("SELECT name FROM spotify_top WHERE kind = 'artists' AND time_range = ?"
                          " ORDER BY rank LIMIT ?", (time_range, int(limit)))
        return [r[0] for r in rows if r[0]]

    # --------------------------------------------------------------- reads --
    def top_artists(self, limit: int = 5, hour: int | None = None, weekday: int | None = None,
                    days: int = 60, now: float | None = None) -> list[tuple[str, int]]:
        try:
            now = time.time() if now is None else float(now)
            sql = ("SELECT artist, COUNT(*) AS n, MAX(ts) FROM plays WHERE ts >= ? AND ts <= ?"
                   " AND (skipped IS NULL OR skipped = 0) AND artist != ''")
            params: list = [now - days * 86400.0, now + 60]
            if hour is not None:
                hours = _hours_around(int(hour))
                sql += f" AND hour IN ({','.join('?' * len(hours))})"
                params += hours
            if weekday is not None:
                sql += " AND weekday = ?"
                params.append(int(weekday))
            sql += " GROUP BY lower(artist) ORDER BY n DESC, MAX(ts) DESC LIMIT ?"
            params.append(int(limit))
            return [(r[0], int(r[1])) for r in self._read(sql, tuple(params))]
        except Exception as error:  # noqa: BLE001
            self._log("top_artists", error)
            return []

    def usual_now(self, now: float | None = None) -> str | None:
        """The artist he plays most around this hour, then overall, then
        Spotify's own short-term top, else None."""
        try:
            now = time.time() if now is None else float(now)
            around = self.top_artists(1, hour=time.localtime(now).tm_hour, days=60, now=now)
            if around and around[0][1] >= 3:
                return around[0][0]
            overall = self.top_artists(1, days=3650, now=now)
            if overall:
                return overall[0][0]
            spotify = self._spotify_top_artists("short_term", 1)
            return spotify[0] if spotify else None
        except Exception as error:  # noqa: BLE001
            self._log("usual_now", error)
            return None

    def recent_plays(self, n: int = 5) -> list[dict]:
        """Latest finished plays, newest first (skips excluded: a song he
        skipped is not one to play again)."""
        rows = self._read("SELECT title, artist, album, app, ts, source FROM plays"
                          " WHERE skipped IS NULL OR skipped = 0 ORDER BY ts DESC LIMIT ?", (int(n),))
        return [{"title": r[0], "artist": r[1], "album": r[2], "app": r[3], "ts": r[4], "source": r[5]}
                for r in rows]

    def last_play(self, now: float | None = None) -> dict | None:
        try:
            rows = self.recent_plays(1)
            if not rows:
                return None
            return {"title": rows[0]["title"], "artist": rows[0]["artist"], "ts": rows[0]["ts"]}
        except Exception as error:  # noqa: BLE001
            self._log("last_play", error)
            return None

    def top_apps(self, limit: int = 5, days: int = 14, now: float | None = None) -> list[tuple[str, float]]:
        """(exe, minutes) by foreground time, including the open interval so
        an app that has been in front all day still shows up."""
        try:
            now = time.time() if now is None else float(now)
            cutoff = now - days * 86400.0
            totals: dict[str, float] = {}
            for exe, seconds in self._read(
                'SELECT exe, SUM("end" - start) FROM app_focus WHERE start >= ? GROUP BY exe', (cutoff,)
            ):
                totals[exe] = totals.get(exe, 0.0) + float(seconds or 0)
            with self._state_lock:
                current = self._focus
            if current is not None and current[1] >= cutoff and now - current[1] >= MIN_FOCUS_S:
                totals[current[0]] = totals.get(current[0], 0.0) + (now - current[1])
            ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[: int(limit)]
            return [(exe, round(seconds / 60.0, 1)) for exe, seconds in ranked if seconds > 0]
        except Exception as error:  # noqa: BLE001
            self._log("top_apps", error)
            return []

    def _usual_hours(self, now: float) -> str | None:
        """The 4-hour window holding most of his listening, if there is one."""
        rows = self._read("SELECT hour, COUNT(*) FROM plays WHERE ts >= ? AND (skipped IS NULL"
                          " OR skipped = 0) AND hour IS NOT NULL GROUP BY hour", (now - 60 * 86400.0,))
        counts = {int(h): int(n) for h, n in rows}
        total = sum(counts.values())
        if total < 5:
            return None
        best_start, best = 0, -1
        for start in range(24):
            n = sum(counts.get((start + d) % 24, 0) for d in range(4))
            if n > best:
                best_start, best = start, n
        if best / total < 0.4:
            return None
        return f"{best_start:02d}:00-{(best_start + 4) % 24:02d}:00"

    def summary_for_prompt(self, now: float | None = None, max_chars: int = 500) -> str:
        """Compact facts for a model prompt. Names are cleaned with
        strip_invisible and capped; window titles are never stored, so none
        can appear here. Empty when nothing is known."""
        try:
            now = time.time() if now is None else float(now)
            parts: list[str] = []
            tops = [_clean(a) for a, _ in self.top_artists(3, days=60, now=now)]
            if tops:
                parts.append(f"Listens most to: {', '.join(tops)}.")
            else:
                spotify = [_clean(a) for a in self._spotify_top_artists("short_term", 3)]
                if spotify:
                    parts.append(f"Top on Spotify lately: {', '.join(spotify)}.")
            hour = time.localtime(now).tm_hour
            around = self.top_artists(1, hour=hour, days=60, now=now)
            if around and around[0][1] >= 3:
                parts.append(f"Around this time ({_part_of_day(hour)}): {_clean(around[0][0])}.")
            hours = self._usual_hours(now)
            if hours:
                parts.append(f"Usual listening hours: {hours}.")
            last = self.last_play(now)
            if last:
                by = f" by {_clean(last['artist'])}" if last.get("artist") else ""
                parts.append(f"Last played: {_clean(last['title'])}{by}.")
            apps = [_clean(_app_label(exe), 30) for exe, _ in self.top_apps(3, now=now)]
            if apps:
                parts.append(f"Most used apps: {', '.join(apps)}.")
            out = ""
            for part in parts:
                candidate = f"{out} {part}".strip()
                if len(candidate) > max_chars:
                    break
                out = candidate
            return out[:max_chars]
        except Exception as error:  # noqa: BLE001
            self._log("summary_for_prompt", error)
            return ""

    def describe_listening(self, now: float | None = None) -> str:
        """A spoken answer to "what do I listen to most"."""
        fallback = ("I haven't learned your listening habits yet. Play some music and "
                    "I'll start picking them up.")
        try:
            now = time.time() if now is None else float(now)
            tops = [_clean(a) for a, _ in self.top_artists(3, days=60, now=now)]
            if not tops:
                tops = [_clean(a) for a, _ in self.top_artists(3, days=3650, now=now)]
            if not tops:
                spotify = [_clean(a) for a in self._spotify_top_artists("short_term", 3)]
                if spotify:
                    return f"On Spotify you've mostly been playing {_join_names(spotify)} lately."
                return fallback
            answer = f"You listen to {tops[0]} the most"
            answer += f", then {_join_names(tops[1:])}." if len(tops) > 1 else "."
            hour = time.localtime(now).tm_hour
            around = self.top_artists(1, hour=hour, days=60, now=now)
            if around and around[0][1] >= 3 and around[0][0].lower() != tops[0].lower():
                answer += f" In the {_part_of_day(hour)} it's usually {_clean(around[0][0])}."
            return answer
        except Exception as error:  # noqa: BLE001
            self._log("describe_listening", error)
            return fallback

    # ------------------------------------------------------------- rewrite --
    def rewrite_music_request(self, text: str, now: float | None = None) -> str | None:
        """Turn "play my usual" and similar phrases into a plain command
        that xenon.music.parse understands, or None."""
        try:
            phrase = _normalise_request(text)
            if not phrase:
                return None
            if _USUAL_RE.match(phrase):
                artist = self.usual_now(now)
                return _artist_command(artist) if artist else None
            if _AGAIN_RE.match(phrase):
                last = self.last_play(now)
                return _track_command(last["title"], last["artist"]) if last else None
            if _MORE_RE.match(phrase):
                # "this" is the song playing now, if the tracker has one.
                with self._state_lock:
                    current = dict(self._current) if self._current else None
                artist = primary_artist(current["artist"]) if current else ""
                if not artist:
                    last = self.last_play(now)
                    artist = last["artist"] if last else ""
                return _artist_command(artist) if artist else None
            return None
        except Exception as error:  # noqa: BLE001
            self._log("rewrite_music_request", error)
            return None

    # --------------------------------------------------------- maintenance --
    def forget(self, timeout: float = 5.0) -> None:
        """Delete everything learned (the schema stays) and VACUUM so the
        old rows are not left in free pages of the file."""
        try:
            with self._state_lock:
                self._current = None
                self._focus = None

            def write(conn: sqlite3.Connection) -> None:
                for table in _TABLES:
                    conn.execute(f"DELETE FROM {table}")
                conn.commit()
                conn.execute("VACUUM")

            job = self._submit(write)
            if job is not None:
                job.done.wait(timeout)
        except Exception as error:  # noqa: BLE001
            self._log("forget", error)

    def stats(self) -> dict:
        out: dict = {}
        for table in _TABLES:
            rows = self._read(f"SELECT COUNT(*) FROM {table}")
            out[table] = int(rows[0][0]) if rows else 0
        return out

    def flush(self, timeout: float = 2.0) -> bool:
        """Block until every write queued so far is done."""
        try:
            if self._thread is None or not self._thread.is_alive():
                return self._queue.empty()
            marker = _Job(None)
            self._queue.put(marker)
            return marker.done.wait(timeout)
        except Exception as error:  # noqa: BLE001
            self._log("flush", error)
            return False

    def close(self) -> None:
        try:
            if self._closed:
                return
            self.finish()
            with self._state_lock:
                self._focus = None
            self.flush(3.0)
            self._closed = True
            if self._thread is not None:
                stop = _Job(None)
                stop.result = "stop"
                self._queue.put(stop)
                stop.done.wait(3.0)
            if self._reader is not None:
                with self._read_lock:
                    self._reader.close()
                self._reader = None
        except Exception as error:  # noqa: BLE001
            self._log("close", error)


# --------------------------------------------------------- request phrases --

_WAKE_RE = re.compile(r"^(?:(?:hey|ok|okay|yo)\s+)?xenon\b[\s,]*")
_POLITE_LEAD_RE = re.compile(r"^(?:please|can you|could you|would you|will you|just)\s+")
_POLITE_TAIL_RE = re.compile(r"(?:[\s,]+(?:please|for me|xenon|thanks))+$")

_USUAL_RE = re.compile(
    r"^(?:play|put on)\s+(?:my usual|the usual|my usual stuff|something i like"
    r"|what i usually (?:listen to|play)|my favou?rite artist)$"
)
_AGAIN_RE = re.compile(
    r"^(?:(?:play|put on)\s+(?:that|this|it|that song|this song|the last song|the last track"
    r"|the song)\s+again|play the last (?:song|track)|replay (?:that|it|that song|the last song))$"
)
_MORE_RE = re.compile(
    r"^(?:play\s+|put on\s+)?(?:some\s+)?more\s+(?:like this|like that|of this artist|of this"
    r"|from this artist|of that artist|of them|by them|by this artist)$"
)


def _normalise_request(text: str) -> str:
    phrase = untrusted.strip_invisible(str(text or "")).lower().replace("’", "'")
    phrase = " ".join(phrase.split()).strip(" .!?,")
    for _ in range(3):
        before = phrase
        phrase = _WAKE_RE.sub("", phrase).strip(" ,")
        phrase = _POLITE_LEAD_RE.sub("", phrase).strip(" ,")
        phrase = _POLITE_TAIL_RE.sub("", phrase).strip(" ,.!?")
        if phrase == before:
            break
    return phrase


def _command_name(text: str) -> str:
    """A name safe to put in a command: one line, no invisible characters,
    and nothing music.parse would strip off the end."""
    return " ".join(untrusted.strip_invisible(str(text or "")).split()).strip(" .!?")[:120]


def _artist_command(artist: str) -> str | None:
    """"play the artist X" makes music.parse set hint="artist", so the
    controller plays the artist instead of a same-named song or playlist."""
    from xenon import music

    name = _command_name(artist)
    if not name:
        return None
    command = f"play the artist {name}"
    parsed = music.parse(command)
    if parsed and parsed.kind == "play" and parsed.hint == "artist" and parsed.query == name.lower():
        return command
    command = f"play {name}"
    parsed = music.parse(command)
    return command if parsed and parsed.kind == "play" else None


def _track_command(title: str, artist: str) -> str | None:
    """"play Title by Artist": with the artist in the query Spotify's
    search ranks the right recording first, and the artist-similarity check
    in MusicController fails, so the track (not the artist) plays."""
    from xenon import music

    title, artist = _command_name(title), _command_name(primary_artist(artist))
    if not title:
        return None
    command = f"play {title} by {artist}" if artist else f"play {title}"
    parsed = music.parse(command)
    if parsed and parsed.kind == "play" and parsed.hint == "" and parsed.query == command[5:].lower():
        return command
    return None


# --------------------------------------------------------------- singleton --

_STORE: PatternStore | None = None
_STORE_LOCK = threading.Lock()


def get_store(enabled: Callable[[], bool] | None = None) -> PatternStore:
    """The process-wide store. XENON_PATTERNS=off gives one that records
    nothing and never creates the database file."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            if os.environ.get("XENON_PATTERNS", "").strip().lower() in {"off", "0", "false", "no"}:
                _STORE = PatternStore(enabled=lambda: False, open_db=False)
            else:
                _STORE = PatternStore(enabled=enabled)
        return _STORE


# ---------------------------------------------------------------- spotify --

TOP_REFRESH_S = 6 * 3600


def spotify_sync(store: PatternStore, client: object, now: float | None = None) -> int:
    """Pull recently played tracks (and, every few hours, top artists and
    tracks) from Spotify into the store. Returns new plays; 0 on any error,
    including 403 from a token that predates the newer scopes."""
    try:
        if not hasattr(client, "recently_played") or not hasattr(client, "top"):
            return 0
        now = time.time() if now is None else float(now)
        last = store._meta("spotify_last_ts")
        after_ms = int(float(last) * 1000) if last else None
        try:
            rows = client.recently_played(after_ms=after_ms, limit=50)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 403 (missing scope), network, anything
            return 0
        added = store.add_spotify_plays(list(rows or []))
        fetched = store._meta("spotify_top_fetched")
        if not fetched or now - float(fetched) >= TOP_REFRESH_S:
            try:
                for kind in ("artists", "tracks"):
                    for time_range in ("short_term", "medium_term"):
                        items = client.top(kind, time_range, 10)  # type: ignore[attr-defined]
                        pairs = [(str(i.get("name", "")), str(i.get("artist", "") or ""))
                                 for i in items or [] if isinstance(i, dict)]
                        store.set_spotify_top(kind, time_range, pairs, now=now)
                stamp = repr(now)
                store._submit(lambda conn: conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('spotify_top_fetched', ?)", (stamp,)))
            except Exception:  # noqa: BLE001
                pass
        return int(added)
    except Exception as error:  # noqa: BLE001
        try:
            store._log("spotify_sync", error)
        except Exception:  # noqa: BLE001
            pass
        return 0
