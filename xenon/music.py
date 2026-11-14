"""Music control: "play my liked songs", "pause", "next song", "play X".

Two layers, both official:

* **Media keys** work with Spotify or any player and need no setup. They can
  pause/resume (a toggle), skip, and go back, but they cannot choose *what*
  plays.
* **Spotify Web API** chooses what plays: liked songs, a song, an artist, or
  one of your playlists. It needs a one-time login (``xenon spotify login``)
  with your own free app from https://developer.spotify.com/dashboard, and
  Spotify only allows playback control for **Premium** accounts.

This is the same public API that smart speakers use. No private endpoints,
no reverse engineering, and nothing that puts the account at risk.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

API = "https://api.spotify.com/v1"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
# Spotify requires a loopback IP literal (not "localhost") for http redirects.
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = (
    "user-read-playback-state user-modify-playback-state "
    "user-read-currently-playing user-library-read playlist-read-private "
    # For xenon.patterns: learn what you play even when Xenon wasn't
    # watching. Tokens from before these were added simply get HTTP 403 on
    # those two endpoints until ``xenon spotify login`` is run again.
    "user-read-recently-played user-top-read"
)

# ----------------------------------------------------------------- parsing --


@dataclass(frozen=True)
class MusicCommand:
    """``kind`` is pause, resume, next, previous, liked, play or now."""

    kind: str
    query: str = ""
    hint: str = ""   # "playlist", "artist", "album" or "" for best match


_FILLER = r"(?:please\s+|can you\s+|could you\s+|just\s+)?"
_ON_SPOTIFY = r"(?:\s+(?:on|in|from|with)\s+spotify)?"
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pause", re.compile(rf"^{_FILLER}(?:pause|stop)(?:\s+(?:the\s+)?(?:music|song|track|spotify|playback|it))?{_ON_SPOTIFY}$")),
    ("resume", re.compile(rf"^{_FILLER}(?:resume|unpause|continue|keep playing|play)(?:\s+(?:the\s+)?(?:music|song|spotify|playback|it))?{_ON_SPOTIFY}$")),
    ("next", re.compile(rf"^{_FILLER}(?:next|skip)(?:\s+(?:this|the))?(?:\s+(?:one|song|track))?{_ON_SPOTIFY}$")),
    ("next", re.compile(rf"^{_FILLER}(?:play\s+)?(?:the\s+)?next\s+(?:song|track){_ON_SPOTIFY}$")),
    ("previous", re.compile(rf"^{_FILLER}previous(?:\s+(?:song|track))?{_ON_SPOTIFY}$")),
    ("previous", re.compile(rf"^{_FILLER}(?:go\s+)?back\s+(?:a|one)\s+(?:song|track){_ON_SPOTIFY}$")),
    ("previous", re.compile(rf"^{_FILLER}(?:play\s+)?(?:the\s+)?(?:previous|last)\s+(?:song|track)(?:\s+again)?{_ON_SPOTIFY}$")),
    ("liked", re.compile(
        rf"^{_FILLER}(?:play|shuffle|put on|start)\s+(?:my\s+|the\s+)?"
        r"(?:liked|loved|saved|favou?rite|favou?rites|likes)"
        rf"(?:\s+(?:songs?|music|tracks?|playlist|list))?{_ON_SPOTIFY}$"
    )),
    ("now", re.compile(
        r"^(?:what(?:'s|s| is)\s+(?:this\s+song|playing|this|the song|the current song)(?:\s+(?:right\s+)?now)?"
        r"|what\s+song\s+is\s+(?:this|playing)|who\s+(?:sings|is singing)\s+this(?:\s+song)?)$"
    )),
]
_PLAY_RE = re.compile(
    rf"^{_FILLER}(?:play|put on|queue up|start playing)\s+(?P<what>.+?){_ON_SPOTIFY}$"
)


_LIKED_WORDS = ("liked", "loved", "saved", "favorite", "favourite")


def _ratio(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def parse(text: str) -> MusicCommand | None:
    """Recognise a music request, or return None so other handlers can try."""
    phrase = " ".join(text.lower().strip().strip(".!?").split())
    phrase = phrase.replace("’", "'")
    if not phrase:
        return None
    for kind, pattern in _PATTERNS:
        if pattern.match(phrase):
            return MusicCommand(kind)
    match = _PLAY_RE.match(phrase)
    if not match:
        return None
    what = match.group("what").strip()
    # "play some jazz" / "play something chill" -> find a playlist.
    some = re.match(r"^(?:some|something)\s+(?P<rest>.+)$", what)
    if some:
        return MusicCommand("play", some.group("rest"), "playlist")
    for hint in ("playlist", "album", "artist"):
        tagged = re.match(rf"^(?:my\s+|the\s+)?(?P<name>.+?)\s+{hint}$", what) or re.match(
            rf"^(?:the\s+)?{hint}\s+(?P<name>.+)$", what
        )
        if tagged:
            return MusicCommand("play", tagged.group("name").strip(), hint)
    # Typed typos ("likled songs", "favourit music") still mean Liked Songs.
    loose = re.match(r"^(?:my\s+|the\s+)?(?P<word>[a-z]+)\s+(?:songs?|music|tracks?)$", what)
    if loose and max(_ratio(loose.group("word"), w) for w in _LIKED_WORDS) >= 0.75:
        return MusicCommand("liked")
    if what.startswith("my "):
        return MusicCommand("play", what[3:], "playlist")
    # Games, videos and the like are not music; leave them to the model.
    if re.search(r"\b(?:video|game|movie|youtube|episode|podcast)\b", what):
        return None
    return MusicCommand("play", what)


# -------------------------------------------------------------- media keys --

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002


def press_media_key(code: int) -> bool:
    """Send a global media key; any player listening for them reacts."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.keybd_event(code, 0, KEYEVENTF_EXTENDEDKEY, 0)
        user32.keybd_event(code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        return False


# ----------------------------------------------------------- token storage --


def token_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "xenon", "spotify.json")


def load_tokens(path: str | None = None) -> dict | None:
    try:
        with open(path or token_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if data.get("refresh_token") else None
    except Exception:
        return None


def save_tokens(tokens: dict, path: str | None = None) -> None:
    path = path or token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(tokens, handle)


def forget_tokens(path: str | None = None) -> bool:
    try:
        os.remove(path or token_path())
        return True
    except FileNotFoundError:
        return False


# ------------------------------------------------------------------ OAuth --


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": SCOPES,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _post_form(url: str, fields: dict, timeout: float = 15.0) -> dict:
    body = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _stamp(tokens: dict, previous: dict | None = None) -> dict:
    stamped = dict(previous or {})
    stamped.update(tokens)
    stamped["expires_at"] = time.time() + float(tokens.get("expires_in", 3600)) - 60
    return stamped


def login(
    client_id: str,
    redirect_uri: str = DEFAULT_REDIRECT,
    open_browser: Callable[[str], object] | None = None,
    timeout_s: float = 180.0,
) -> dict:
    """Run the PKCE flow: open the browser, catch the redirect, save tokens."""
    import http.server
    import webbrowser

    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ValueError("the redirect URI must be http://127.0.0.1:<port>/<path>")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return
            if query.get("state", [""])[0] != state:
                result["error"] = "state mismatch (possible stale or forged redirect)"
            elif "error" in query:
                result["error"] = query["error"][0]
            else:
                result["code"] = query.get("code", [""])[0]
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = "Xenon is connected to Spotify. You can close this tab." if ok else (
                f"Spotify login failed: {result.get('error')}"
            )
            self.wfile.write(f"<html><body style='font-family:sans-serif;background:#0d111c;color:#e6eaf5;padding:40px'><h2>{message}</h2></body></html>".encode())
            done.set()

        def log_message(self, *_args) -> None:
            pass

    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (open_browser or webbrowser.open)(authorize_url(client_id, redirect_uri, challenge, state))
        if not done.wait(timeout_s):
            raise TimeoutError("no response from the Spotify login page")
    finally:
        server.shutdown()
        server.server_close()
    if "code" not in result:
        raise RuntimeError(f"Spotify login failed: {result.get('error', 'unknown error')}")
    tokens = _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    stamped = _stamp(tokens)
    stamped["client_id"] = client_id
    save_tokens(stamped)
    return stamped


# ----------------------------------------------------------------- client --


class SpotifyError(RuntimeError):
    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


# (method, url, headers, body) -> (status, parsed json or None)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, object]]


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, object]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw) if raw else None
        except ValueError:
            return error.code, None


class SpotifyClient:
    def __init__(
        self,
        tokens: dict,
        transport: Transport | None = None,
        refresh: Callable[[dict], dict] | None = None,
        persist: Callable[[dict], None] | None = save_tokens,
    ) -> None:
        self._tokens = tokens
        self._transport = transport or _urllib_transport
        self._refresh = refresh or self._default_refresh
        self._persist = persist

    @staticmethod
    def _default_refresh(tokens: dict) -> dict:
        fresh = _post_form(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": tokens.get("client_id") or os.environ.get("XENON_SPOTIFY_CLIENT_ID", ""),
            },
        )
        return _stamp(fresh, tokens)

    def _token(self) -> str:
        if time.time() >= float(self._tokens.get("expires_at", 0)):
            self._tokens = self._refresh(self._tokens)
            if self._persist:
                self._persist(self._tokens)
        return self._tokens["access_token"]

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Authorization": f"Bearer {self._token()}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        elif method in {"PUT", "POST"}:
            data = b""
        status, payload = self._transport(method, url, headers, data)
        if status >= 400:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            reason = error.get("reason", "") if isinstance(error, dict) else ""
            message = error.get("message", f"HTTP {status}") if isinstance(error, dict) else str(error)
            raise SpotifyError(status, reason, message)
        return payload

    # convenience

    def devices(self) -> list[dict]:
        payload = self.call("GET", "/me/player/devices") or {}
        return list(payload.get("devices", []))

    def search(self, query: str, kinds: str, limit: int = 5) -> dict:
        return self.call("GET", "/search", {"q": query, "type": kinds, "limit": limit}) or {}

    def my_playlists(self) -> list[dict]:
        payload = self.call("GET", "/me/playlists", {"limit": 50}) or {}
        return [item for item in payload.get("items", []) if item]

    def liked_tracks(self, count: int = 50, rng: random.Random | None = None) -> list[str]:
        first = self.call("GET", "/me/tracks", {"limit": 1}) or {}
        total = int(first.get("total", 0))
        if total == 0:
            return []
        rng = rng or random.Random()
        offset = rng.randint(0, max(0, total - count)) if total > count else 0
        page = self.call("GET", "/me/tracks", {"limit": min(count, 50), "offset": offset}) or {}
        uris = [item["track"]["uri"] for item in page.get("items", []) if item.get("track")]
        rng.shuffle(uris)
        return uris

    def now_playing(self) -> dict | None:
        return self.call("GET", "/me/player/currently-playing")

    # read-only history, for xenon.patterns (needs the newer scopes; an old
    # token raises SpotifyError(403) and the caller treats that as "not yet")

    def recently_played(self, after_ms: int | None = None, limit: int = 50) -> list[dict]:
        """Your last plays as plain dicts, oldest data first is not promised.

        ``played_at`` is converted to epoch seconds so callers never parse
        Spotify's ISO strings themselves.
        """
        from datetime import datetime

        params = {"limit": max(1, min(int(limit), 50)), "after": int(after_ms) if after_ms else None}
        payload = self.call("GET", "/me/player/recently-played", params) or {}
        out: list[dict] = []
        for item in payload.get("items", []) if isinstance(payload, dict) else []:
            track = (item or {}).get("track") or {}
            stamp = str((item or {}).get("played_at") or "")
            if not track.get("name") or not stamp:
                continue
            try:
                played = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            out.append({
                "track_id": str(track.get("id") or ""),
                "title": str(track.get("name") or ""),
                "artist": ", ".join(str(a.get("name", "")) for a in track.get("artists", []) if a),
                "album": str((track.get("album") or {}).get("name") or ""),
                "played_at": played,
            })
        return out

    def top(self, kind: str = "artists", time_range: str = "short_term", limit: int = 10) -> list:
        """Your top artists or tracks as [{name, artist, id}] (artist is ""
        for kind="artists")."""
        if kind not in {"artists", "tracks"}:
            raise ValueError("kind must be 'artists' or 'tracks'")
        params = {"time_range": time_range, "limit": max(1, min(int(limit), 50))}
        payload = self.call("GET", f"/me/top/{kind}", params) or {}
        out: list[dict] = []
        for item in payload.get("items", []) if isinstance(payload, dict) else []:
            if not item or not item.get("name"):
                continue
            artist = ""
            if kind == "tracks":
                artist = ", ".join(str(a.get("name", "")) for a in item.get("artists", []) if a)
            out.append({"name": str(item["name"]), "artist": artist, "id": str(item.get("id") or "")})
        return out


# ------------------------------------------------------------- controller --


def _similar(name: str, query: str) -> float:
    """How well a spoken name matches a title: 'gym' fits 'Gym Bangers'."""
    from difflib import SequenceMatcher

    name, query = name.lower().strip(), query.lower().strip()
    if not name or not query:
        return 0.0
    ratio = SequenceMatcher(None, name, query).ratio()
    words = set(re.findall(r"[a-z0-9]+", name))
    asked = set(re.findall(r"[a-z0-9]+", query)) - {"the", "my", "a"}
    if asked and asked <= words:
        # Every spoken word is in the title; fewer extra words is better.
        return max(ratio, 0.9 - 0.02 * len(words - asked))
    return ratio


class MusicController:
    """Turns a ``MusicCommand`` into playback, and says what happened."""

    def __init__(
        self,
        client: SpotifyClient | None = None,
        media_key: Callable[[int], bool] = press_media_key,
        open_uri: Callable[[str], object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        web: object | None = None,
    ) -> None:
        self._client = client
        self._media_key = media_key
        self._open_uri = open_uri or _open_uri
        self._sleep = sleep
        self._web = web

    @classmethod
    def from_env(cls) -> "MusicController":
        tokens = load_tokens()
        web = None
        if sys.platform == "win32":
            try:
                from xenon.webplayer import WebPlayer

                web = WebPlayer()
            except Exception:
                web = None
        return cls(SpotifyClient(tokens) if tokens else None, web=web)

    def run(self, command: MusicCommand) -> str:
        if self._client is None:
            if "\x1f" in command.query:
                command = MusicCommand(command.kind, " ".join(command.query.split("\x1f")).strip(), "")
            return self._without_api(command)
        try:
            return self._with_api(command)
        except SpotifyError as error:
            if error.reason == "PREMIUM_REQUIRED" or error.status == 403:
                # Free accounts cannot be controlled through the API; the
                # media keys still work for the basics.
                fallback = self._without_api(command)
                return f"Spotify only lets Premium accounts be controlled remotely. {fallback}"
            return f"Spotify said: {error}"
        except (urllib.error.URLError, OSError) as error:
            return f"I couldn't reach Spotify ({error})."

    # --- no login: media keys and URIs -----------------------------------

    def _without_api(self, command: MusicCommand) -> str:
        kind = command.kind
        replies = {"pause": "Paused.", "resume": "Playing.", "next": "Skipped.", "previous": "Going back."}
        if kind in replies:
            # The web player's own buttons first: browsers do not always pass
            # Windows media keys to the Spotify tab.
            pressed = None
            if self._web is not None:
                try:
                    pressed = self._web.control(kind)
                except Exception:
                    pressed = None
            if pressed:
                return replies[kind]
            if pressed is False:
                return "I found the Spotify player but couldn't press that button."
            key = {"pause": VK_MEDIA_PLAY_PAUSE, "resume": VK_MEDIA_PLAY_PAUSE,
                   "next": VK_MEDIA_NEXT_TRACK, "previous": VK_MEDIA_PREV_TRACK}[kind]
            if not self._media_key(key):
                return "Media keys are unavailable."
            return "Done." if kind in {"pause", "resume"} else replies[kind]
        if kind == "liked":
            opened: list = []
            played = self._press_play(
                "Liked Songs", lambda: opened.append(self._open_uri("spotify:collection:tracks"))
            )
            if played:
                return "Playing your Liked Songs."
            if not opened:
                opened.append(self._open_uri("spotify:collection:tracks"))
            return (
                f"I opened your Liked Songs in {self._where(opened[0])}; press play. Run "
                "`python -m xenon spotify login` once and I'll start it myself."
            )
        if kind == "play":
            if command.hint == "playlist":
                # Your playlists are in the web player's sidebar on every page.
                played = self._press_play(
                    command.query, lambda: self._open_uri("spotify:collection:tracks")
                )
                if played:
                    return f"Playing {played}."
            uri = "spotify:search:" + urllib.parse.quote(command.query)
            # Reuse the Spotify tab that's already open; only open a new
            # page when there is none.
            reused = False
            if self._web is not None and hasattr(self._web, "open_in_tab"):
                try:
                    reused = bool(self._web.open_in_tab(web_url(uri)))
                except Exception:
                    reused = False
            opened_where = "web" if reused else self._open_uri(uri)
            played = self._press_play(command.query, None)
            if played:
                return f"Playing {played}."
            return (
                f"I searched {self._where(opened_where)} for {command.query}. Log in with "
                "`python -m xenon spotify login` and I'll play it directly."
            )
        if kind == "now":
            return "Log in with `python -m xenon spotify login` and I can tell you what's playing."
        return "I can't do that with music yet."

    def _press_play(self, wanted: str, open_page) -> str | None:
        """Click the web player's own Play button; None if that didn't work."""
        if self._web is None:
            return None
        try:
            return self._web.play(wanted, open_page)
        except Exception:
            return None

    @staticmethod
    def _where(opened: object) -> str:
        return "the Spotify web player" if opened == "web" else "Spotify"

    # --- logged in: Web API ------------------------------------------------

    def _with_api(self, command: MusicCommand) -> str:
        client = self._client
        assert client is not None
        kind = command.kind
        if kind == "pause":
            client.call("PUT", "/me/player/pause")
            return "Paused."
        if kind == "resume":
            self._play(body=None)
            return "Playing."
        if kind == "next":
            client.call("POST", "/me/player/next")
            return "Skipped."
        if kind == "previous":
            client.call("POST", "/me/player/previous")
            return "Going back."
        if kind == "now":
            current = client.now_playing()
            item = (current or {}).get("item") if isinstance(current, dict) else None
            if not item:
                return "Nothing is playing."
            artists = ", ".join(artist["name"] for artist in item.get("artists", []))
            return f"{item.get('name')} by {artists}." if artists else f"{item.get('name')}."
        if kind == "liked":
            uris = client.liked_tracks()
            if not uris:
                return "Your Liked Songs are empty."
            self._play({"uris": uris})
            return "Playing your liked songs on shuffle."
        if kind == "play":
            return self._play_query(command)
        return "I can't do that with music yet."

    def _play_query(self, command: MusicCommand) -> str:
        client = self._client
        assert client is not None
        query, hint = command.query, command.hint
        if hint == "track" and "\x1f" in query:
            # From the dock: "title\x1fartist" -> Spotify's field search.
            title, artist = query.split("\x1f", 1)
            query = f'track:"{title.strip()}" artist:"{artist.strip()}"' if artist.strip() else title.strip()
        if hint in {"playlist", ""}:
            # Your own playlists beat public ones with the same name.
            best = max(
                client.my_playlists(),
                key=lambda p: _similar(p.get("name", ""), query),
                default=None,
            )
            if best and _similar(best.get("name", ""), query) >= (0.6 if hint else 0.85):
                self._play({"context_uri": best["uri"]})
                return f"Playing your {best['name']} playlist."
        kinds = {"playlist": "playlist", "album": "album", "artist": "artist",
                 "track": "track"}.get(hint, "track,artist")
        found = client.search(query, kinds, limit=5)
        if hint == "playlist":
            items = [p for p in found.get("playlists", {}).get("items", []) if p]
            if items:
                self._play({"context_uri": items[0]["uri"]})
                return f"Playing {items[0]['name']}."
        elif hint == "album":
            items = found.get("albums", {}).get("items", [])
            if items:
                self._play({"context_uri": items[0]["uri"]})
                return f"Playing the album {items[0]['name']}."
        else:
            artists = found.get("artists", {}).get("items", [])
            tracks = found.get("tracks", {}).get("items", [])
            # "play drake" means the artist; "play hotline bling" the song.
            if artists and hint != "track" and (hint == "artist" or _similar(artists[0]["name"], query) >= 0.85):
                self._play({"context_uri": artists[0]["uri"]})
                return f"Playing {artists[0]['name']}."
            if tracks:
                track = tracks[0]
                self._play({"uris": [track["uri"]]})
                by = ", ".join(a["name"] for a in track.get("artists", []))
                return f"Playing {track['name']} by {by}." if by else f"Playing {track['name']}."
        return f"I couldn't find {query} on Spotify."

    def _play(self, body: dict | None) -> None:
        """Start playback, waking the desktop app first if nothing is active."""
        client = self._client
        assert client is not None
        try:
            client.call("PUT", "/me/player/play", body=body)
            return
        except SpotifyError as error:
            if error.status != 404 and error.reason != "NO_ACTIVE_DEVICE":
                raise
        device = self._wake_device()
        if device is None:
            raise SpotifyError(404, "NO_ACTIVE_DEVICE", "no Spotify device is open; start Spotify and try again")
        client.call("PUT", "/me/player/play", params={"device_id": device}, body=body)

    def _wake_device(self) -> str | None:
        client = self._client
        assert client is not None
        devices = client.devices()
        if not devices:
            self._open_uri("spotify:")
            for _ in range(30):
                self._sleep(0.5)
                devices = client.devices()
                if devices:
                    break
        if not devices:
            return None
        preferred = [d for d in devices if d.get("type") == "Computer"] or devices
        return preferred[0].get("id")


def spotify_client() -> "SpotifyClient | None":
    """The logged-in Spotify client, or None (not logged in). Used by the
    habit store to read recently played and top artists."""
    tokens = load_tokens()
    return SpotifyClient(tokens) if tokens else None


def spotify_app_installed() -> bool:
    """True when the desktop app (classic or Store) handles spotify: links.

    A leftover ``HKCR/spotify`` key without an open command is common after
    an uninstall, and opening a link then silently does nothing.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"spotify\shell\open\command") as key:
            if str(winreg.QueryValue(key, None)).strip():
                return True
    except OSError:
        pass
    candidates = (
        os.path.join(os.environ.get("APPDATA", ""), "Spotify", "Spotify.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "Spotify.exe"),
    )
    return any(os.path.exists(path) for path in candidates)


def web_url(uri: str) -> str:
    """The open.spotify.com page for a spotify: link."""
    if uri.startswith("spotify:search:"):
        return "https://open.spotify.com/search/" + uri[len("spotify:search:"):]
    if uri == "spotify:collection:tracks":
        return "https://open.spotify.com/collection/tracks"
    parts = uri.split(":")
    if len(parts) == 3 and parts[1] in {"track", "album", "artist", "playlist"}:
        return f"https://open.spotify.com/{parts[1]}/{parts[2]}"
    return "https://open.spotify.com/"


def _open_uri(uri: str) -> str:
    """Open in the desktop app if installed, else the web player. Says where."""
    if sys.platform == "win32" and spotify_app_installed():
        os.startfile(uri)  # type: ignore[attr-defined]
        return "app"
    import webbrowser

    webbrowser.open(web_url(uri))
    return "web"
