"""Companion screen: a lightweight page for a tablet or phone on the same Wi-Fi.

The orb runs a small read-only HTTP server (default port 8765). Open
``http://<this PC's IP>:8765`` on the tablet:

* music playing -> song, artist and synced lyrics as kinetic typography;
* otherwise     -> a big clock, Xenon's state, running timers, last answer.

The page only polls ``/state`` once a second and animates with CSS
transforms, so a slow tablet copes. Nothing on the page can control the PC.
The music poller (a ~1.5 s UI Automation read of the Spotify web player)
only runs while a screen has asked for state in the last 15 seconds.

Hardening (phase 0), because anyone on campus Wi-Fi can reach the port:

* every request must carry a Host header naming this PC (localhost, its IPs
  or its hostname). A web page that rebinds its own domain to 127.0.0.1 or
  to the PC's LAN address still sends *its* domain, so it gets 403;
* POST (show/clear) needs loopback AND the per-boot ``X-Xenon-Secret``. The
  secret is made with ``secrets.token_urlsafe`` at ``start()``; in-process
  pushers read it from ``Companion.post_secret`` and the CLI from the
  owner-only key file ``read_post_secret()`` reads;
* when the pushed state is private (an answer built from the screen or
  memory, or a task) ``/state`` shows "Private" instead of the text;
* a device that is not this PC never sees ``detail`` (typed questions,
  voice transcripts, task progress): only the state label, timers, music
  and the last non-private answer;
* the LAN policy (``XENON_COMPANION_LAN``):

  - ``0`` / ``off``: 127.0.0.1 only;
  - ``1`` / ``on`` / ``force``: any device on the LAN may read the page,
    whatever the network profile (a deliberate opt-in);
  - unset (the default): the server listens on the LAN, but on a Public or
    unknown profile it answers only devices on this PC's own private
    (RFC 1918) /24 subnet, such as the tablet at 192.168.1.11 on home Wi-Fi
    that Windows calls Public. A Private/Domain profile opens it to the
    whole LAN. The profile is read once, off the Tk thread; until it is
    known the stricter subnet rule applies.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable

DEFAULT_PORT = 8765
PAGE = os.path.join(os.path.dirname(__file__), "static", "companion.html")
SECRET_HEADER = "X-Xenon-Secret"
PRIVATE_TEXT = "Private"
PRIVATE_DETAIL = "Private task"
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_WILDCARDS = frozenset({"", "0.0.0.0", "::"})
_LAN_OFF = frozenset({"0", "off", "false", "no"})
_LAN_FORCE = frozenset({"1", "on", "true", "yes", "force"})
_RFC1918 = tuple(
    ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
# LAN modes: who besides this PC may read the page.
LAN_OFF = "off"          # nobody (bound to 127.0.0.1)
LAN_SUBNET = "subnet"    # devices on this PC's own private /24 only
LAN_OPEN = "open"        # any device on the LAN


def _ipv4(address: str):
    try:
        ip = ipaddress.ip_address(str(address or "").split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip if ip.version == 4 else None


def _rfc1918(ip) -> bool:
    return ip is not None and any(ip in net for net in _RFC1918)


def same_private_subnet(client: str, local_ips: Iterable[str]) -> bool:
    """Is ``client`` a private (RFC 1918) IPv4 address in the same /24 as
    one of this PC's own private addresses?"""
    ip = _ipv4(client)
    if not _rfc1918(ip):
        return False
    subnet = ipaddress.ip_network(f"{ip}/24", strict=False)
    for own in local_ips:
        mine = _ipv4(own)
        if _rfc1918(mine) and mine in subnet:
            return True
    return False


def is_loopback_client(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(address or "").split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def lan_view(state: dict) -> dict:
    """What a device that is not this PC may see of the orb state.

    ``detail`` carries typed questions, voice transcripts and task progress,
    so it is replaced by the state label. Only the state, its label, the
    voice flag, timers and the last (already redacted) answer are kept.
    """
    safe = redact_state(state)
    label = str(safe.get("label") or safe.get("state") or "")
    view = {
        "state": safe.get("state", "idle"),
        "label": label,
        "detail": label if safe.get("detail") else "",
        "voice": bool(safe.get("voice")),
        "timers": safe.get("timers") or [],
        "last": safe.get("last", ""),
        "private": bool(safe.get("private")),
    }
    return view


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


def local_addresses() -> set[str]:
    """Every IP this PC answers on, as far as the resolver knows (no packets)."""
    found = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(str(info[4][0]).split("%", 1)[0].lower())
    except OSError:
        pass
    address = lan_address()
    if address:
        found.add(address)
    return found


def _probe_public() -> bool | None:
    """Ask Windows' Network List Manager whether a connected network is Public.

    Category 0 is Public, 1 Private, 2 Domain. Any connected Public network
    counts, because a wildcard bind listens on every interface. None means
    "could not tell". Read-only COM, no PowerShell.
    """
    if sys.platform != "win32":
        return None
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    try:
        manager = comtypes.client.CreateObject("{DCB00C01-570F-4A9B-8D69-199FDBA5723B}", dynamic=True)
        categories = []
        for network in manager.GetNetworks(1):  # NLM_ENUM_NETWORK_CONNECTED
            if not network.IsConnected:
                continue
            categories.append(int(network.GetCategory()))
        if not categories:
            return None
        return any(category == 0 for category in categories)
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def network_is_public(timeout_s: float = 5.0) -> bool | None:
    """True on a Public profile, False on Private/Domain, None when unknown.

    The COM call runs on a short-lived thread with its own apartment so the
    caller's (often the Tk thread's) COM state is never touched.
    """
    result: list[bool | None] = [None]

    def probe() -> None:
        try:
            result[0] = _probe_public()
        except Exception:
            result[0] = None

    worker = threading.Thread(target=probe, daemon=True, name="xenon-netprofile")
    worker.start()
    worker.join(timeout_s)
    return result[0] if not worker.is_alive() else None


def redact_state(state: dict, taint: bool = False) -> dict:
    """The orb state with private answers replaced by a placeholder.

    ``private`` (answers built from the screen or memory) and ``tainted``
    (anything that read untrusted content) both redact; so does ``taint``.
    """
    safe = dict(state or {})
    if taint or bool(safe.get("private")) or bool(safe.get("tainted")):
        if safe.get("last"):
            safe["last"] = PRIVATE_TEXT
        if safe.get("detail"):
            safe["detail"] = PRIVATE_DETAIL
        safe["private"] = True
    return safe


def default_secret_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local, "Xenon", "companion.key")


def read_post_secret(path: str | Path | None = None) -> str:
    """The running orb's POST secret for local pushers (``xenon show``)."""
    try:
        return Path(path or default_secret_path()).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_secret(path: Path, secret: str) -> None:
    """Owner-only write: replace the file, never append to someone else's."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(secret)


def _host_name(header: str | None) -> str:
    """The name part of a Host header, lower-cased, without port or brackets."""
    value = (header or "").strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else ""
    name, sep, port = value.rpartition(":")
    if sep and port.isdigit() and ":" not in name:
        value = name
    return value.rstrip(".")


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


class Companion:
    def __init__(
        self,
        state: Callable[[], dict],
        now_playing: Callable[[], dict | None] | None = None,
        lyrics: Callable[[str, str, int], object] | None = None,
        port: int = DEFAULT_PORT,
        host: str = "0.0.0.0",
        allowed_hosts: Iterable[str] | None = None,
        post_secret: str | None = None,
        network_public: Callable[[], bool | None] | None = None,
        local_ips: Callable[[], Iterable[str]] | None = None,
        secret_path: str | Path | None = None,
        publish_secret: bool | None = None,
    ) -> None:
        self._state = state
        self._now_playing = now_playing
        self._lyrics = lyrics
        self.port = port
        self.host = host
        self.bind_host = host
        self.lan_note = ""
        # Until the profile is read, a device on the LAN gets the stricter rule.
        self.lan_mode = LAN_SUBNET
        self._extra_hosts = {_host_name(h) or h.lower() for h in (allowed_hosts or ())}
        self._post_secret = post_secret or ""
        self._network_public = network_public or network_is_public
        self._local_ips = local_ips or local_addresses
        self._ip_cache: set[str] = set()
        self._ip_cache_at = 0.0
        self._secret_path = Path(secret_path) if secret_path is not None else default_secret_path()
        # Only a server on a known port can be found by the CLI, so only that
        # one publishes its secret; tests on port 0 never touch the real file.
        self._publish_secret = (port != 0) if publish_secret is None else publish_secret
        self._music: dict | None = None
        self._music_at = 0.0
        self._track_lyrics: dict[str, object] = {}
        self._last_request = 0.0
        # The notch shows the song too, so it can ask for music polling even
        # when no tablet is connected.
        self.always_poll = False
        # Screens that polled recently: address -> what they reported.
        self.screens: dict[str, dict] = {}
        # Something the PC asked every screen to show (see ``show``).
        self._overlay: dict | None = None
        self._stop = threading.Event()
        self._server: ThreadingHTTPServer | None = None

    # ----- security ------------------------------------------------------

    @property
    def post_secret(self) -> str:
        """Per-boot secret for POST; empty until ``start()`` (or injected)."""
        return self._post_secret

    def push_headers(self) -> dict[str, str]:
        """Headers an in-process pusher sends with POST /show or /clear."""
        return {"Content-Type": "application/json", SECRET_HEADER: self._post_secret}

    def secret_ok(self, offered: str | None) -> bool:
        if not self._post_secret or not offered:
            return False
        return hmac.compare_digest(offered.encode("utf-8"), self._post_secret.encode("utf-8"))

    def host_allowed(self, header: str | None) -> bool:
        """Does the Host header name this PC? (DNS-rebinding defence)."""
        name = _host_name(header)
        if not name:
            return False
        if name in _LOOPBACK_NAMES or name in self._extra_hosts:
            return True
        if not _is_ip(name):
            hostname = socket.gethostname().lower()
            return name in {hostname, f"{hostname}.local", f"{hostname}.lan"}
        if name in self._ip_cache:
            return True
        # An address we have not seen: the PC may have changed networks.
        # Refresh at most every 30 s so a flood of bad Hosts costs nothing.
        if time.monotonic() - self._ip_cache_at > 30 or not self._ip_cache_at:
            try:
                self._ip_cache = {str(ip).lower() for ip in self._local_ips()}
            except Exception:
                self._ip_cache = set()
            self._ip_cache_at = time.monotonic()
        if self.bind_host not in _WILDCARDS:
            self._ip_cache.add(self.bind_host.lower())
        return name in self._ip_cache

    def _choose_bind_host(self) -> str:
        """Where to listen, and the starting LAN mode. Never reads the
        network profile (that is ``resolve_profile``, off the Tk thread)."""
        if self.host in _LOOPBACK_NAMES:
            self.lan_mode = LAN_OFF
            return self.host
        lan = os.environ.get("XENON_COMPANION_LAN", "").strip().lower()
        if lan in _LAN_OFF:
            self.lan_mode = LAN_OFF
            self.lan_note = "LAN page off (XENON_COMPANION_LAN=0)"
            return "127.0.0.1"
        if lan in _LAN_FORCE:
            self.lan_mode = LAN_OPEN
            self.lan_note = "LAN page open to the whole network (XENON_COMPANION_LAN=1)"
            return self.host
        self.lan_mode = LAN_SUBNET
        self.lan_note = "tablet page: this Wi-Fi subnet only, read-only"
        return self.host

    def resolve_profile(self) -> str:
        """Read the network profile once and settle the LAN mode.

        Private/Domain opens the page to the LAN; Public or unknown keeps the
        same-subnet rule (unknown resolves stricter). Returns the mode.
        """
        if self.lan_mode != LAN_SUBNET:
            return self.lan_mode
        try:
            public = self._network_public()
        except Exception:
            public = None
        if public is False:
            self.lan_mode = LAN_OPEN
            self.lan_note = "tablet page on the LAN (Private network), read-only"
        elif public is True:
            self.lan_note = ("tablet page: this Wi-Fi subnet only, read-only (the network is Public; "
                             "XENON_COMPANION_LAN=1 opens it to the whole LAN, =0 keeps it on this PC)")
        else:
            self.lan_note = ("tablet page: this Wi-Fi subnet only, read-only (network profile unknown; "
                             "XENON_COMPANION_LAN=1 opens it to the whole LAN, =0 keeps it on this PC)")
        return self.lan_mode

    def client_allowed(self, address: str) -> bool:
        """May the device at ``address`` read the page?"""
        if is_loopback_client(address):
            return True
        if self.lan_mode == LAN_OPEN:
            return True
        if self.lan_mode == LAN_OFF:
            return False
        try:
            own = set(self._local_ips())
        except Exception:
            own = set()
        if self.bind_host not in _WILDCARDS:
            own.add(self.bind_host)
        return same_private_subnet(address, own)

    # ----- state ---------------------------------------------------------

    def report(self, address: str, info: dict) -> None:
        """A screen told us about itself (size, browser, smoothness)."""
        entry = self.screens.setdefault(address, {})
        entry.update({k: v for k, v in info.items() if v not in (None, "")})
        entry["last_seen"] = time.time()

    def connected(self, within_s: float = 10.0) -> dict[str, dict]:
        now = time.time()
        return {a: s for a, s in self.screens.items() if now - s.get("last_seen", 0) <= within_s}

    def show(self, kind: str, content: str, seconds: float = 15.0, title: str = "") -> dict:
        """Show ``content`` on every screen: kind is text, image or url."""
        if kind not in {"text", "image", "url"}:
            raise ValueError("kind must be text, image or url")
        if kind in {"image", "url"} and not content.startswith(("http://", "https://", "/")):
            raise ValueError("images and pages must be http(s) addresses")
        self._overlay = {
            "id": f"{time.time():.3f}",
            "kind": kind,
            "content": content[:4000],
            "title": title[:200],
            "until": time.time() + max(1.0, min(seconds, 3600.0)),
        }
        return self._overlay

    def clear(self) -> None:
        self._overlay = None

    def current_music(self) -> dict | None:
        """The last song read from the web player, if it is still fresh."""
        if self._music and time.monotonic() - self._music_at < 10:
            return self._music
        return None

    def snapshot(self, have_lyrics: str = "", lan: bool = False) -> dict:
        """The page's state. ``lan`` is True for any client that is not this
        PC: it gets ``lan_view`` (no typed questions or transcripts)."""
        self._last_request = time.monotonic()
        state = self._safe_state()
        xenon = lan_view(state) if lan else redact_state(state)
        payload = {"time": time.time(), "xenon": xenon, "music": None}
        overlay = self._overlay
        if overlay and overlay["until"] > time.time():
            payload["overlay"] = overlay
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
            wanted = self.always_poll or time.monotonic() - self._last_request < 15
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
            tablet = time.monotonic() - self._last_request < 15
            self._stop.wait(2.0 if tablet else 6.0 if wanted else 1.0)

    # ----- server --------------------------------------------------------

    def start(self) -> str:
        companion = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if not self._client_ok() or not self._host_ok():
                    return
                path, _, query = self.path.partition("?")
                if path in {"/", "/index.html"}:
                    try:
                        with open(PAGE, "rb") as handle:
                            body = handle.read()
                    except OSError:
                        body = b"companion page missing"
                    self._send(200, "text/html; charset=utf-8", body)
                elif path == "/state":
                    from urllib.parse import parse_qs

                    params = {k: v[0] for k, v in parse_qs(query).items()}
                    companion.report(self.client_address[0], {
                        "width": params.get("w"), "height": params.get("h"),
                        "dpr": params.get("dpr"), "fps": params.get("fps"),
                        "agent": (self.headers.get("User-Agent") or "")[:200],
                    })
                    body = json.dumps(
                        companion.snapshot(params.get("have", ""), lan=not self._local())
                    ).encode("utf-8")
                    self._send(200, "application/json", body)
                elif path == "/screens" and self._local():
                    self._send(200, "application/json", json.dumps(companion.connected(3600)).encode("utf-8"))
                else:
                    self._send(404, "text/plain", b"not found")

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                if not self._client_ok() or not self._host_ok():
                    return
                # Only Xenon on this PC may push to the screens: loopback alone
                # is not enough, since any local process or a rebound page
                # could reach 127.0.0.1. LAN devices can only look.
                if not self._local() or not companion.secret_ok(self.headers.get(SECRET_HEADER)):
                    self._send(403, "text/plain", b"only this computer can do that")
                    return
                length = min(int(self.headers.get("Content-Length") or 0), 16384)
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._send(400, "text/plain", b"bad json")
                    return
                if self.path == "/show":
                    try:
                        shown = companion.show(data.get("kind", "text"), str(data.get("content", "")),
                                               float(data.get("seconds", 15)), str(data.get("title", "")))
                    except (ValueError, TypeError) as error:
                        self._send(400, "text/plain", str(error).encode("utf-8"))
                        return
                    self._send(200, "application/json", json.dumps(shown).encode("utf-8"))
                elif self.path == "/clear":
                    companion.clear()
                    self._send(200, "application/json", b"{}")
                else:
                    self._send(404, "text/plain", b"not found")

            def _local(self) -> bool:
                return is_loopback_client(self.client_address[0])

            def _client_ok(self) -> bool:
                if companion.client_allowed(self.client_address[0]):
                    return True
                self._send(403, "text/plain", b"this page is only for devices on this Wi-Fi")
                return False

            def _host_ok(self) -> bool:
                if companion.host_allowed(self.headers.get("Host")):
                    return True
                self._send(403, "text/plain", b"unknown host")
                return False

            def _send(self, status: int, kind: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass

        if not self._post_secret:
            self._post_secret = secrets.token_urlsafe(32)
        self.bind_host = self._choose_bind_host()
        self._server = ThreadingHTTPServer((self.bind_host, self.port), Handler)
        self._server.daemon_threads = True
        # Only after the bind worked: a second orb whose bind fails must not
        # replace the running server's key (that breaks `xenon show`).
        if self._publish_secret:
            try:
                _write_secret(self._secret_path, self._post_secret)
                self._wrote_secret = True
            except OSError as error:
                print(f"companion screen: could not save the push secret ({error}); "
                      "`xenon show` will not work", file=sys.stderr, flush=True)
        if self.lan_mode == LAN_SUBNET:
            # One short-lived thread reads the profile; the Tk thread never waits.
            threading.Thread(target=self._settle_profile, daemon=True, name="xenon-netprofile-once").start()
        threading.Thread(target=self._server.serve_forever, daemon=True, name="xenon-companion").start()
        threading.Thread(target=self._poll_music, daemon=True, name="xenon-companion-music").start()
        port = self._server.server_address[1]
        if self.bind_host in _LOOPBACK_NAMES:
            return f"http://127.0.0.1:{port}"
        return f"http://{lan_address()}:{self.port}"

    def _settle_profile(self) -> None:
        mode = self.resolve_profile()
        if mode != LAN_OPEN and self.lan_note:
            print(f"companion screen: {self.lan_note}", file=sys.stderr, flush=True)

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if (
            self._publish_secret
            and getattr(self, "_wrote_secret", False)
            and read_post_secret(self._secret_path) == self._post_secret
        ):
            try:  # a stale secret must not outlive the server it belonged to
                self._secret_path.unlink()
            except OSError:
                pass
