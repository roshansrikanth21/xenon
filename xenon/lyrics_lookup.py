"""Lyrics for whatever is playing, from LRCLIB (lrclib.net).

LRCLIB is a free, open lyrics database that needs no account or key and serves
time-synced lyrics in the LRC format. The track is searched for by name, and
the candidate whose length is closest to what the player reports wins: a song
often has several entries (album, deluxe, live), and only one of them lines up
with the recording that is actually playing.

Result shape mirrors Weyv's main/lyrics.js, which this module ports:

    {'lines': [(time_ms, text), ...], 'synced': True, 'instrumental': False}
    {'lines': [], 'untimed': True}   -- only plain lyrics; never fake timing
    {'lines': [], 'instrumental': True}
    {'failed': True}                 -- offline / site down; worth a retry
    None                             -- nothing found

``time`` is in milliseconds into the song.
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
import urllib.parse
import urllib.request
from collections import OrderedDict

API = "https://lrclib.net/api"
TIMEOUT_S = 10
MAX_CACHE = 60
USER_AGENT = "Xenon/1.0 (https://github.com/roshansrikanth21/xenon)"

# Bracketed asides that say nothing about which recording it is.
NOISE = re.compile(
    r"\b(official|video|audio|lyrics?|lyric video|visuali[sz]er|music video|mv|"
    r"hd|hq|4k|remaster(ed)?|explicit|clean|color coded|live performance|"
    r"performance video|full album|out now|premiere)\b",
    re.IGNORECASE,
)

_STAMP = re.compile(r"^\s*\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
_WORD_STAMP = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
_REMASTER_TAIL = re.compile(
    r"\s+[-\u2013\u2014]\s+(\d{4}\s+)?"
    r"(remaster(ed)?|mono|stereo|single version|radio edit|bonus track).*$",
    re.IGNORECASE,
)
_FEAT_TAIL = re.compile(r"\s+(feat|ft)\.?\s.+$", re.IGNORECASE)
_BRACKETS = re.compile(r"[\[({\u3010]([^)\]}\u3011]*)[)\]}\u3011]")
_DASH_SPLIT = re.compile(r"^(.+?)\s+[-\u2013\u2014]\s+(.+)$")
_PLAYER_APPS = re.compile(
    r"spotify|music\.ui|itunes|applemusic|tidal|deezer|foobar|musicbee|aimp",
    re.IGNORECASE,
)
_VARIANT = re.compile(
    r"\b(remix(es)?|live|karaoke|instrumental|tribute|acoustic|sped up|slowed)\b",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ text

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def clean_title(title: str) -> str:
    """A title without the "(Official Video)", "[Lyrics]", "feat. X" and
    "- 2011 Remaster" around it."""

    def bracket(whole: re.Match) -> str:
        inner = whole.group(1)
        return " " if NOISE.search(inner) or re.match(r"^\s*(feat|ft|with)\b", inner, re.IGNORECASE) else whole.group(0)

    text = _BRACKETS.sub(bracket, str(title or ""))
    text = _REMASTER_TAIL.sub("", text)
    text = _FEAT_TAIL.sub("", text)
    text = re.sub(r"\s*\|.*$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_artist(artist: str) -> str:
    """A channel or credit name reduced to the artist: "ArtistVEVO",
    "Artist - Topic"."""
    text = str(artist or "")
    text = re.sub(r"\s*-\s*topic$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"vevo$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+official$", "", text, flags=re.IGNORECASE)
    return re.split(r"\s*(?:,|;|\bfeat\.?|\bft\.?|&|\bx\b)\s*", text, flags=re.IGNORECASE)[0].strip()


def parse_track(track: dict) -> dict:
    """What to search for. Video titles are usually "Artist - Title (Official
    Video)" with the channel as the artist, so a dash that splits off the
    artist (or appears in a browser, where the channel is often a label)
    wins."""
    raw_title = str(track.get("title") or "")
    artist = clean_artist(track.get("artist") or "")
    title = raw_title

    dash = _DASH_SPLIT.match(raw_title)
    if dash:
        left = normalize(dash.group(1))
        from_player = bool(_PLAYER_APPS.search(str(track.get("app") or "")))
        if (
            not artist
            or left in normalize(artist)
            or normalize(artist) in left
            or not from_player
        ):
            artist = clean_artist(dash.group(1))
            title = dash.group(2)

    return {
        "title": clean_title(title),
        "artist": artist,
        "album": clean_title(track.get("album") or ""),
    }


def _words(text: str) -> list[str]:
    return [w for w in normalize(text).split(" ") if w]


def similarity(a: str, b: str) -> float:
    """0..1: how much of the shorter name the longer one contains, word for
    word."""
    left, right = _words(a), _words(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    bag = set(right)
    shared = sum(1 for word in left if word in bag)
    return (
        shared / max(len(left), len(right)) * 0.6
        + shared / min(len(left), len(right)) * 0.4
    )


def score(wanted: dict, candidate: dict) -> float:
    """0..~1: how well a search result matches the playing track."""
    title = similarity(wanted["title"], clean_title(candidate.get("title", "")))
    artist = max(
        similarity(wanted["artist"], candidate.get("artist", "")),
        similarity(wanted["artist"], clean_artist(candidate.get("artist", ""))),
    )
    if title < 0.6 or artist < 0.5:
        return 0.0
    album = (
        similarity(wanted["album"], clean_title(candidate.get("album", "")))
        if wanted["album"] and candidate.get("album")
        else 0.0
    )
    # With no album to go on, the original release beats a remix EP or live album.
    off_cut = (
        0.2
        if _VARIANT.search(candidate.get("album") or "")
        and not _VARIANT.search(wanted["title"])
        and not _VARIANT.search(wanted["album"])
        else 0.0
    )
    return title * 0.55 + artist * 0.45 + album * 0.15 - off_cut


# ------------------------------------------------------------------ parsing

def parse_lrc(source: str) -> list[tuple[int, str]]:
    """LRC: "[mm:ss.xx]text", possibly several stamps on one line, an optional
    "[offset:+/-ms]" header, and word stamps "<mm:ss.xx>" in the enhanced
    variant, which are dropped here."""
    lines: list[tuple[int, str]] = []
    offset = 0
    for raw in str(source or "").splitlines():
        header = _OFFSET.match(raw)
        if header:
            offset = int(header.group(1)) or 0
            continue
        stamps: list[int] = []
        rest = raw
        while True:
            match = _STAMP.match(rest)
            if not match:
                break
            seconds = float(match.group(2).replace(":", "."))
            stamps.append(int(match.group(1)) * 60000 + round(seconds * 1000))
            rest = rest[match.end() :]
        if not stamps:
            continue
        text = re.sub(r"\s+", " ", _WORD_STAMP.sub("", rest)).strip()
        for time_ms in stamps:
            lines.append((max(0, time_ms - offset), text))
    lines.sort(key=lambda line: line[0])
    return lines


def _stamp_ms(minutes: str, seconds: str) -> int:
    return int(minutes) * 60000 + round(float(seconds.replace(":", ".")) * 1000)


_WORD_STAMP_PARTS = re.compile(r"<(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)>")


def _word_times_in(rest: str, line_ms: int, offset: int):
    """(starts, end) for one enhanced line's words, or None when it carries
    too few word stamps to trust. A stamp inside a word (per syllable)
    belongs to that word; a stamp after the last word is when it ends."""
    text, marks, at = "", [], 0
    for match in _WORD_STAMP_PARTS.finditer(rest):
        text += rest[at:match.start()]
        marks.append((len(text), max(0, _stamp_ms(match.group(1), match.group(2)) - offset)))
        at = match.end()
    text += rest[at:]
    words = list(re.finditer(r"\S+", text))
    if not words or not marks:
        return None
    starts: list = []
    i = 0
    for word in words:
        found = None
        while i < len(marks) and marks[i][0] < word.end():
            if found is None:
                found = marks[i][1]
            i += 1
        starts.append(found)
    end = marks[i][1] if i < len(marks) else None
    if sum(s is None for s in starts) * 2 > len(starts):
        return None
    # Fill the odd unstamped word between its neighbours.
    for k, value in enumerate(starts):
        if value is None:
            before = next((starts[j] for j in range(k - 1, -1, -1) if starts[j] is not None), line_ms)
            after = next((starts[j] for j in range(k + 1, len(starts)) if starts[j] is not None),
                         end if end is not None else before + 300)
            starts[k] = (before + after) // 2 if k else min(line_ms, after)
    for k in range(1, len(starts)):
        starts[k] = max(starts[k], starts[k - 1])
    return starts, end


def parse_word_times(source: str) -> dict:
    """Enhanced LRC ("[00:12.00]<00:12.00>Hello <00:12.48>world <00:13.10>"):
    each line's start (ms, as ``parse_lrc`` gives it) -> (word starts, end
    or None). Lines without word stamps are left out, so an ordinary LRC
    gives {}."""
    out: dict = {}
    offset = 0
    for raw in str(source or "").splitlines():
        header = _OFFSET.match(raw)
        if header:
            offset = int(header.group(1)) or 0
            continue
        stamps: list[int] = []
        rest = raw
        while True:
            match = _STAMP.match(rest)
            if not match:
                break
            stamps.append(_stamp_ms(match.group(1), match.group(2)))
            rest = rest[match.end():]
        if not stamps or "<" not in rest:
            continue
        for time_ms in stamps:
            line_ms = max(0, time_ms - offset)
            found = _word_times_in(rest, line_ms, offset)
            # Word stamps are absolute: a line sung twice ("[01:00][02:00]")
            # only matches the stamp its words sit near.
            if found and abs(found[0][0] - line_ms) <= 1500:
                out[line_ms] = found
    return out


# ------------------------------------------------------------------ ranking

def rank(wanted: dict, duration_ms: int, candidate: dict) -> float:
    """Name match first; then the length has to agree with the player's (when
    it reports one), then synced beats plain."""
    names = score(
        wanted,
        {
            "title": candidate.get("trackName", ""),
            "artist": candidate.get("artistName", ""),
            "album": candidate.get("albumName", ""),
        },
    )
    if names <= 0:
        return 0.0
    value = names
    seconds = candidate.get("duration") or 0
    if duration_ms > 0 and seconds > 0:
        # Versions that run a few seconds apart can be timed differently, so a
        # near-exact length outweighs a matching album name.
        off = abs(seconds - duration_ms / 1000)
        if off > 12:
            return 0.0
        value += 0.6 - off * 0.05 if off <= 2 else 0.4 * (1 - off / 12)
    if candidate.get("syncedLyrics"):
        value += 0.3
    elif candidate.get("plainLyrics"):
        value += 0.05
    elif not candidate.get("instrumental"):
        return 0.0
    return value


def to_result(candidate: dict) -> dict | None:
    if candidate.get("instrumental") and not candidate.get("syncedLyrics") and not candidate.get("plainLyrics"):
        return {"lines": [], "synced": False, "instrumental": True}
    if candidate.get("syncedLyrics"):
        lines = parse_lrc(candidate["syncedLyrics"])
        if lines:
            result = {"lines": lines, "synced": True, "instrumental": False}
            words = parse_word_times(candidate["syncedLyrics"])
            if words:                           # only enhanced LRC has word stamps
                result["words"] = words
            return result
    plain = candidate.get("plainLyrics")
    if plain and plain.strip():
        return {"lines": [], "synced": False, "instrumental": False, "untimed": True}
    return None


def _line_times(source: str) -> dict[str, int]:
    """Each distinct line's text -> when it is first sung."""
    times: dict[str, int] = {}
    for time_ms, text in parse_lrc(source):
        key = text.lower()
        if key and key not in times:
            times[key] = time_ms
    return times


def _agree(a: dict[str, int], b: dict[str, int]) -> bool:
    """Whether two uploads sing most of the lines they share within a third of
    a second."""
    shared = 0
    close = 0
    for text, time_ms in a.items():
        other = b.get(text)
        if other is None:
            continue
        shared += 1
        if abs(time_ms - other) <= 350:
            close += 1
    return shared >= 4 and close / shared >= 0.6


def most_agreed(scored: list[tuple[dict, float]]) -> dict:
    """Popular songs are uploaded many times, and the top few often score the
    same. Most uploads are timed alike, but an odd one out can run a second
    late on every line, so among the near-best synced ones take the one whose
    timing the most others agree with. Ties go to the higher score."""
    if not scored:
        return None
    close = [
        entry
        for entry in scored
        if entry[0].get("syncedLyrics") and entry[1] >= scored[0][1] - 0.25
    ]
    if len(close) < 3:
        return scored[0][0]
    timings = [_line_times(entry[0]["syncedLyrics"]) for entry in close]
    best, best_votes = 0, -1
    for i, mine in enumerate(timings):
        votes = sum(
            1 for j, theirs in enumerate(timings) if j != i and _agree(mine, theirs)
        )
        if votes > best_votes:
            best, best_votes = i, votes
    return close[best][0]


# ------------------------------------------------------------------ lookup

def _http_json(url: str, fetch=None) -> list:
    if fetch is not None:
        return fetch(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    return data if isinstance(data, list) else []


def find(track: dict, duration_ms: int = 0, fetch=None) -> dict | None:
    """Best LRCLIB candidate for a playing track, ranked and agreed upon."""
    wanted = parse_track(track)
    if not wanted["title"]:
        return None

    params = {"track_name": wanted["title"]}
    if wanted["artist"]:
        params["artist_name"] = wanted["artist"]
    candidates = _http_json(f"{API}/search?{urllib.parse.urlencode(params)}", fetch)
    # Some entries file the artist differently ("A & B", a band's full name).
    if not candidates:
        query = f"{wanted['artist']} {wanted['title']}".strip()
        candidates = _http_json(f"{API}/search?{urllib.parse.urlencode({'q': query})}", fetch)

    scored = [
        (candidate, rank(wanted, duration_ms, candidate))
        for candidate in candidates
    ]
    scored = [entry for entry in scored if entry[1] > 0]
    scored.sort(key=lambda entry: entry[1], reverse=True)
    if not scored:
        return None
    # Any upload with timing beats any without, however well the other's name
    # matches.
    timed = [entry for entry in scored if entry[0].get("syncedLyrics")]
    return to_result(most_agreed(timed if timed else scored))


class LyricsLookup:
    """Cached lookup with a background thread, so a slow LRCLIB round trip
    never stalls the render loop. Failures from the network are not cached:
    the next track change retries them."""

    def __init__(self, fetch=None) -> None:
        self._fetch = fetch
        self._cache: OrderedDict[str, dict | None] = OrderedDict()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._result: dict | None = None
        self._key: str | None = None
        self._done = threading.Event()

    def result(self) -> dict | None:
        """The lyrics for the most recently requested track, or None while a
        lookup is in flight."""
        return self._result

    def request(self, track: dict | None, duration_ms: int = 0) -> None:
        """Start (or join) a lookup for ``track``. Non-blocking."""
        if not track or not track.get("title"):
            self._result = None
            return
        key = "\n".join(
            str(track.get(field) or "") for field in ("title", "artist", "album")
        ).lower()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None or key in self._cache:
                if hit is not None:
                    self._cache.move_to_end(key)
                self._result = hit
                return
            if self._thread and self._thread.is_alive() and self._key == key:
                return  # already looking this one up

            self._done.clear()
            self._key = key
            wanted = dict(track)
            duration = duration_ms
            previous = self._thread

            def run() -> None:
                if previous:
                    previous.join(timeout=TIMEOUT_S)
                try:
                    found = find(wanted, duration, self._fetch)
                except Exception:
                    found = {"failed": True}  # offline; retry next track change
                with self._lock:
                    self._cache[key] = found
                    if self._key == key:
                        self._result = found
                        self._key = None
                    while len(self._cache) > MAX_CACHE:
                        self._cache.popitem(last=False)
                self._done.set()

            self._thread = threading.Thread(target=run, name="xenon-lyrics", daemon=True)
            self._thread.start()
