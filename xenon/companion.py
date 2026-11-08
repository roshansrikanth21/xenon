"""Companion screen: a lightweight page for a tablet or phone on the same Wi-Fi.

The orb runs a small read-only HTTP server (default port 8765). Open
``http://<this PC's IP>:8765`` on the tablet:

* music playing -> song, artist and synced lyrics as kinetic typography;
* otherwise     -> a big clock, Xenon's state, running timers, last answer.

The page only polls ``/state`` once a second and animates with CSS
transforms, so a slow tablet copes. Nothing on the page can control the PC.
The music poller (a ~1.5 s UI Automation read of the Spotify web player)
only runs while a screen has asked for state in the last 15 seconds.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

DEFAULT_PORT = 8765
PAGE = os.path.join(os.path.dirname(__file__), "static", "companion.html")


def lan_address() -> str:
    """The PC's address on the local network (no packets are sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class Companion:
    def __init__(
        self,
        state: Callable[[], dict],
        now_playing: Callable[[], dict | None] | None = None,
        lyrics: Callable[[str, str, int], object] | None = None,
        port: int = DEFAULT_PORT,
        host: str = "0.0.0.0",
    ) -> None:
        self._state = state
        self._now_playing = now_playing
        self._lyrics = lyrics
        self.port = port
        self.host = host
        self._music: dict | None = None
        self._music_at = 0.0
        self._track_lyrics: dict[str, object] = {}
        self._last_request = 0.0
        self._stop = threading.Event()
        self._server: ThreadingHTTPServer | None = None

    # ----- state ---------------------------------------------------------

    def snapshot(self, have_lyrics: str = "") -> dict:
        self._last_request = time.monotonic()
        payload = {"time": time.time(), "xenon": self._safe_state(), "music": None}
        music = self._music
        if music and time.monotonic() - self._music_at < 10:
            track = f"{music['title']}|{music['artist']}"
            entry = dict(music)
            # Where the song is now, so the page can keep time between polls.
            elapsed = (time.monotonic() - self._music_at) * 1000 if music.get("playing") else 0
            entry["position_ms"] = min(music.get("duration_ms") or 10**9, int(music["position_ms"] + elapsed))
            entry["track"] = track
            lyrics = self._track_lyrics.get(track)
            entry["lyrics_state"] = "loading" if track not in self._track_lyrics else (
                "none" if lyrics is None else "instrumental" if getattr(lyrics, "instrumental", False)
                else "synced" if getattr(lyrics, "lines", None) else "none"
            )
            if have_lyrics != track and lyrics is not None and getattr(lyrics, "lines", None):
                entry["lyrics"] = lyrics.lines
            payload["music"] = entry
        return payload

    def _safe_state(self) -> dict:
        try:
            return self._state()
        except Exception:
            return {"state": "idle"}

    def _poll_music(self) -> None:
        try:  # UI Automation needs COM on this thread
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        while not self._stop.is_set():
            wanted = time.monotonic() - self._last_request < 15
            if wanted and self._now_playing is not None:
                try:
                    music = self._now_playing()
                except Exception:
                    music = None
                if music:
                    self._music, self._music_at = music, time.monotonic()
                    track = f"{music['title']}|{music['artist']}"
                    if track not in self._track_lyrics and self._lyrics is not None:
                        self._track_lyrics[track] = None  # mark as looked up
                        try:
                            self._track_lyrics[track] = self._lyrics(
                                music["title"], music["artist"], music.get("duration_ms", 0)
                            )
                        except Exception:
                            self._track_lyrics.pop(track, None)
                else:
                    self._music = None
            self._stop.wait(2.0 if wanted else 1.0)

    # ----- server --------------------------------------------------------

    def start(self) -> str:
        companion = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                path, _, query = self.path.partition("?")
                if path in {"/", "/index.html"}:
                    try:
                        with open(PAGE, "rb") as handle:
                            body = handle.read()
                    except OSError:
                        body = b"companion page missing"
                    self._send(200, "text/html; charset=utf-8", body)
                elif path == "/state":
                    have = ""
                    for part in query.split("&"):
                        if part.startswith("have="):
                            from urllib.parse import unquote_plus

                            have = unquote_plus(part[5:])
                    body = json.dumps(companion.snapshot(have)).encode("utf-8")
                    self._send(200, "application/json", body)
                else:
                    self._send(404, "text/plain", b"not found")

            def _send(self, status: int, kind: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True, name="xenon-companion").start()
        threading.Thread(target=self._poll_music, daemon=True, name="xenon-companion-music").start()
        return f"http://{lan_address()}:{self.port}"

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
