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
    "user-read-currently-playing user-library-read playlist-read-private"
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
        open_uri: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._media_key = media_key
        self._open_uri = open_uri or _open_uri
        self._sleep = sleep

    @classmethod
    def from_env(cls) -> "MusicController":
        tokens = load_tokens()
        return cls(SpotifyClient(tokens) if tokens else None)

    def run(self, command: MusicCommand) -> str:
        if self._client is None:
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
        if kind in {"pause", "resume"}:
            return "Done." if self._media_key(VK_MEDIA_PLAY_PAUSE) else "Media keys are unavailable."
        if kind == "next":
            return "Skipped." if self._media_key(VK_MEDIA_NEXT_TRACK) else "Media keys are unavailable."
        if kind == "previous":
            return "Going back." if self._media_key(VK_MEDIA_PREV_TRACK) else "Media keys are unavailable."
        if kind == "liked":
            self._open_uri("spotify:collection:tracks")
            return (
                "I opened your Liked Songs in Spotify; press play. Run "
                "`python -m xenon spotify login` once and I'll start it myself."
            )
        if kind == "play":
            self._open_uri("spotify:search:" + urllib.parse.quote(command.query))
            return (
                f"I searched Spotify for {command.query}. Log in with "
                "`python -m xenon spotify login` and I'll play it directly."
            )
        if kind == "now":
            return "Log in with `python -m xenon spotify login` and I can tell you what's playing."
        return "I can't do that with music yet."

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
        kinds = {"playlist": "playlist", "album": "album", "artist": "artist"}.get(hint, "track,artist")
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
            if artists and (hint == "artist" or _similar(artists[0]["name"], query) >= 0.85):
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
            for _ in range(16):
                self._sleep(0.5)
                devices = client.devices()
                if devices:
                    break
        if not devices:
            return None
        preferred = [d for d in devices if d.get("type") == "Computer"] or devices
        return preferred[0].get("id")


def _open_uri(uri: str) -> None:
    if sys.platform == "win32":
        os.startfile(uri)  # type: ignore[attr-defined]
    else:  # pragma: no cover - Windows-first project
        import webbrowser

        webbrowser.open(uri)
