"""Xenon from your phone, through your Azure VM.

    phone --HTTPS--> Cloudflare quick tunnel --> VM 127.0.0.1:8766
          --reverse SSH tunnel (dialled out by the laptop)--> laptop 127.0.0.1:8766

Nothing listens on the laptop beyond localhost and nothing new is opened
on the home network or the VM: the laptop dials the VM over SSH (the key
you already use for it) and the VM's cloudflared dials Cloudflare.

The phone page is guarded by a long random key kept in
%APPDATA%\\xenon\\remote.json. It travels in the link's #fragment (never
sent in a URL, so it is not in Cloudflare's or anyone's logs) and the page
sends it in a header; it is compared in constant time and wrong guesses
are rate-limited.

What the phone may do is deliberately narrow, whatever the key holder
types: music, volume, timers, game/Xtreme mode, the music widget and its
settings, habits questions, and short spoken-style answers. Never "do ..."
agent tasks, typing, app launching, web actions, or anything that sends,
pays or deletes: a leaked link must not be able to drive the laptop.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

PORT = 8766
VM = os.environ.get("XENON_REMOTE_VM", "")    # user@host, from remote.json "vm" (kept out of git)
KEY_FILE_ENV = "XENON_REMOTE_KEY"
MAX_TEXT = 300
MAX_FAILS_PER_MIN = 10

# First words that mean an agent task, an outward action or app control:
# refused from the phone even if a parser could run them.
_REFUSE_FIRST = frozenset("do type send email mail message text whatsapp delete remove erase pay buy order "
                          "open launch run close kill quit install download click press search google "
                          "switch write reply call".split())
_REFUSE_ANYWHERE = ("whatsapp", "password", "email", "bank", " send ", "delete ", " go to ")
# "start ..." is refused except for these (Xenon's own switches).
_ALLOW_START = ("start xtreme", "start extreme", "start game mode", "start learning", "start the visualizer")


def config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "xenon" / "remote.json"


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and len(str(data.get("key", ""))) >= 32:
            return data
    except (OSError, ValueError):
        pass
    data = {"key": secrets.token_urlsafe(32), "enabled": True}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
    return data


def allowed_from_phone(text: str) -> bool:
    """Coarse screen before any parser sees the text (see module notes)."""
    phrase = " ".join((text or "").lower().split())
    if not phrase or len(phrase) > MAX_TEXT:
        return False
    if phrase.startswith(_ALLOW_START):
        return True
    words = phrase.removeprefix("hey ").removeprefix("xenon ").split()
    if not words or words[0] in _REFUSE_FIRST or words[0] == "start":
        return False
    return not any(w in f" {phrase} " for w in _REFUSE_ANYWHERE)


class _Limiter:
    def __init__(self) -> None:
        self._fails: list[float] = []
        self._lock = threading.Lock()

    def blocked(self, now: float) -> bool:
        with self._lock:
            self._fails = [t for t in self._fails if now - t < 60]
            return len(self._fails) >= MAX_FAILS_PER_MIN

    def fail(self, now: float) -> None:
        with self._lock:
            self._fails.append(now)


class RemoteServer:
    """The phone page and its one API, on 127.0.0.1 only."""

    def __init__(self, handle: Callable[[str], str], key: str, port: int = PORT,
                 status: Callable[[], dict] | None = None) -> None:
        self._handle = handle
        self._key = key
        self._status = status or (lambda: {})
        self._limiter = _Limiter()
        self._lock = threading.Lock()          # one command at a time
        self.port = port
        self._server: ThreadingHTTPServer | None = None

    def check_key(self, given: str | None) -> bool:
        now = time.monotonic()
        if self._limiter.blocked(now):
            return False
        ok = bool(given) and hmac.compare_digest(str(given).encode(), self._key.encode())
        if not ok:
            self._limiter.fail(now)
        return ok

    def run_command(self, text: str) -> tuple[int, dict]:
        text = " ".join(str(text or "").split())[:MAX_TEXT]
        if not text:
            return 400, {"reply": "Say something."}
        if not allowed_from_phone(text):
            return 200, {"reply": "From the phone I only do music, game mode, the widget and quick "
                                  "questions. Do that one at the laptop."}
        with self._lock:
            try:
                reply = self._handle(text) or "Done."
            except Exception as error:          # never take the server down
                reply = f"That didn't work: {error}"
        return 200, {"reply": str(reply)[:600]}

    def start(self) -> bool:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:   # no request logs (keys are in headers)
                pass

            def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "connect-src 'self'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.split("?")[0] in ("/", "/index.html"):
                    self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif self.path.startswith("/api/status"):
                    if not server.check_key(self.headers.get("X-Xenon-Key")):
                        self._send(401, b'{"error":"key"}')
                        return
                    self._send(200, json.dumps(server._status()).encode())
                else:
                    self._send(404, b'{"error":"not found"}')

            def do_POST(self) -> None:  # noqa: N802
                if not self.path.startswith("/api/cmd"):
                    self._send(404, b'{"error":"not found"}')
                    return
                if not server.check_key(self.headers.get("X-Xenon-Key")):
                    self._send(401, b'{"error":"key"}')
                    return
                try:
                    size = min(int(self.headers.get("Content-Length") or 0), 4096)
                    data = json.loads(self.rfile.read(size) or b"{}")
                except (ValueError, OSError):
                    self._send(400, b'{"error":"bad request"}')
                    return
                code, body = server.run_command(data.get("text", "") if isinstance(data, dict) else "")
                self._send(code, json.dumps(body).encode())

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError as error:
            print(f"phone access: port {self.port} busy ({error})", file=sys.stderr, flush=True)
            return False
        threading.Thread(target=self._server.serve_forever, daemon=True, name="xenon-remote").start()
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class Tunnel:
    """Keeps the reverse SSH tunnel to the VM up (ssh.exe ships with
    Windows). Reconnects with backoff; ``url()`` asks the VM which public
    address its relay currently has."""

    def __init__(self, key_file: str | None = None, vm: str = VM, port: int = PORT) -> None:
        self.key_file = key_file or os.environ.get(KEY_FILE_ENV) or str(Path.home() / ".ssh" / "id_ed25519")
        self.vm, self.port = vm, port
        self._stop = threading.Event()
        self._proc = None
        self.up = False

    def _ssh(self, *extra) -> list[str]:
        return ["ssh", "-i", self.key_file, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", *extra, self.vm]

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="xenon-remote-tunnel").start()

    def _run(self) -> None:
        wait = 5.0
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(
                    self._ssh("-N", "-o", "ExitOnForwardFailure=yes",
                              "-R", f"127.0.0.1:{self.port}:127.0.0.1:{self.port}"),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags)
                self.up = True
                self._proc.wait()
            except OSError as error:
                print(f"phone access: ssh failed ({error})", file=sys.stderr, flush=True)
            self.up = False
            if self._stop.is_set():
                break
            wait = 5.0 if time.monotonic() - started > 120 else min(wait * 2, 300.0)
            self._stop.wait(wait)

    def url(self, timeout: float = 20.0) -> str | None:
        """The relay's current https://...trycloudflare.com address."""
        try:
            out = subprocess.run(
                self._ssh("journalctl -u xenon-relay --no-pager -n 200 -o cat | "
                          "grep -o 'https://[a-z0-9-]*\\.trycloudflare\\.com' | tail -1"),
                capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            url = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
            return url or None
        except (OSError, subprocess.SubprocessError, IndexError):
            return None

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass


def phone_link(url: str, key: str) -> str:
    return f"{url.rstrip('/')}/#k={key}"


def _load_page() -> str:
    """The phone page (static/phone.html): one file, no
    external fonts, scripts or images (the page's CSP allows none)."""
    try:
        return (Path(__file__).with_name("static") / "phone.html").read_text(encoding="utf-8")
    except OSError:
        return "<!doctype html><title>Xenon</title><p>Phone page missing.</p>"


PAGE = _load_page()
