"""Windows "now playing" for the desktop music widget, via SMTC.

The System Media Transport Controls session is the same source behind the
volume flyout's media controls, so this works for Spotify, a YouTube tab, VLC,
anything that registers a session — no per-service integration, nothing to
sign in to. It reports title/artist/album, the album thumbnail, and the
timeline (position/duration/playback status), and can send play-pause,
next and previous commands back to the player.

This is the Python counterpart of Weyv's ``pulse-nowplaying.exe`` helper (which
reads the exact same API from C#); here it is done in-process with ``winsdk``
instead of a helper process.

``winsdk`` is optional: without it, ``start()`` reports the reason and the
widget runs visualizer-only.
"""
from __future__ import annotations

import asyncio
import base64
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Track:
    title: str = ""
    artist: str = ""
    album: str = ""
    app: str = ""
    # Decoded image bytes (JPEG/PNG — whatever the player supplied), or None.
    art: bytes | None = None

    def art_base64(self) -> str | None:
        if not self.art:
            return None
        return base64.b64encode(self.art).decode("ascii")


@dataclass
class Timeline:
    position_ms: int = 0
    duration_ms: int = 0
    playing: bool = False
    # time.monotonic() when position_ms was true; the widget extrapolates.
    updated_monotonic: float = field(default_factory=time.monotonic)

    def position_now(self) -> int:
        """Best current estimate of the playback position, in milliseconds."""
        if self.playing:
            drift = (time.monotonic() - self.updated_monotonic) * 1000
            position = self.position_ms + drift
            if self.duration_ms > 0:
                position = min(position, self.duration_ms)
            return int(position)
        return self.position_ms


class NowPlaying:
    """Polls the media session once a second on a background thread.

    ``state()`` returns the latest ``(Track, Timeline)`` snapshot, or
    ``(None, None)``. Commands are fire-and-forget, like the media keys.
    """

    def __init__(self, poll_seconds: float = 1.0) -> None:
        self.poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._track: Track | None = None
        self._timeline: Timeline | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> tuple[bool, str]:
        try:
            from winsdk.windows.media.control import (  # noqa: F401
                GlobalSystemMediaTransportControlsSessionManager as _manager,
            )
        except ImportError as error:
            return False, f"now playing unavailable ({error}); install winsdk"
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="xenon-smtc", daemon=True)
        self._thread.start()
        return True, ""

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None

    # -- reading ------------------------------------------------------------
    def state(self) -> tuple[Track | None, Timeline | None]:
        with self._lock:
            return self._track, self._timeline

    # -- commands -----------------------------------------------------------
    def play_pause(self) -> None:
        # Chromium browsers (Opera, Chrome, Edge) ignore the "toggle"
        # command; send an explicit pause or play based on the live status.
        self._command("play_pause")

    def next_track(self) -> None:
        self._command("try_skip_next_async")

    def previous_track(self) -> None:
        self._command("try_skip_previous_async")

    def _command(self, method: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def send() -> None:
            session = await self._shown_session()
            if session is None:
                return
            if method == "play_pause":
                status = session.get_playback_info().playback_status.name
                ok = await (session.try_pause_async() if status == "PLAYING" else session.try_play_async())
                if not ok:
                    await session.try_toggle_play_pause_async()
            else:
                await getattr(session, method)()
            # Refresh right away so the button flips without waiting a poll.
            try:
                track, timeline, _props = await self._read(session)
                with self._lock:
                    if self._track is not None:
                        track.art = self._track.art
                self._publish(track, timeline)
            except Exception:
                pass

        asyncio.run_coroutine_threadsafe(send(), loop)

    # -- worker -------------------------------------------------------------
    def _run(self) -> None:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        )

        async def main() -> None:
            last_key = None
            art = None
            retry = 0
            while not self._stop.is_set():
                try:
                    manager = await MediaManager.request_async()
                    session = await self._session(manager)
                    if session is None:
                        self._publish(None, None)
                        last_key = None
                    else:
                        track, timeline, props = await self._read(session)
                        key = (track.title, track.artist, track.album, track.app)
                        if key != last_key:
                            previous = art
                            art = await self._thumbnail(props)
                            if art and previous and art == previous and last_key is not None:
                                # The player still shows the last song's art:
                                # wait for the real one (retried below).
                                art = None
                            last_key = key
                            retry = 0
                        elif not art:
                            # Players often publish the thumbnail a moment
                            # after the title (or drop it across a pause):
                            # keep asking every few polls until it lands.
                            retry += 1
                            if retry % 3 == 0:
                                art = await self._thumbnail(props)
                        # Each poll builds a fresh Track; carry the art over.
                        track.art = art
                        self._publish(track, timeline)
                except Exception:
                    self._publish(None, None)
                    last_key = None
                await asyncio.sleep(self.poll_seconds)

        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(main())
        finally:
            self._loop.close()
            self._loop = None

    async def _shown_session(self):
        """The session the widget is showing (by app id), so a button never
        lands on some other app that happens to be Windows' "current"."""
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        )
        manager = await MediaManager.request_async()
        with self._lock:
            app = self._track.app if self._track else ""
        if app:
            for session in manager.get_sessions():
                if self._app_of(session) == app:
                    return session
        return await self._session(manager)

    async def _session(self, manager=None):
        if manager is None:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as MediaManager,
            )
            manager = await MediaManager.request_async()
        current = manager.get_current_session()
        # Several apps register sessions (WhatsApp does, for its own window).
        # Prefer one that is playing a real track (it has an artist), then
        # the current one if it plays, then anything playing, then current.
        playing, music, paused_music = None, None, None
        for session in manager.get_sessions():
            if not _has_media(session):
                continue
            is_playing = session.get_playback_info().playback_status.name == "PLAYING"
            if is_playing:
                playing = playing or session
            if music is None:
                try:
                    props = await session.try_get_media_properties_async()
                    if props.artist:
                        if is_playing:
                            music = session
                        elif paused_music is None:
                            paused_music = session
                except Exception:
                    pass
        if music is not None:
            return music
        if playing is None and paused_music is not None:
            return paused_music      # a paused song beats an app's own window
        if current is not None and _has_media(current):
            if current.get_playback_info().playback_status.name == "PLAYING" or playing is None:
                return current
        return playing or current

    async def _read(self, session):
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionPlaybackStatus,
        )

        props = await session.try_get_media_properties_async()
        timeline_props = session.get_timeline_properties()
        playback = session.get_playback_info()
        playing = (
            playback.playback_status
            == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
        )

        position_ms = int(timeline_props.position.total_seconds() * 1000)
        duration_ms = int(timeline_props.end_time.total_seconds() * 1000)
        app = self._app_of(session)
        track = Track(
            title=props.title or "",
            artist=props.artist or "",
            album=props.album_title or "",
            app=app,
        )
        # Browsers only report the position on play/pause/seek; it is as of
        # ``last_updated_time``, not now. Date the reading back to then so
        # position_now() keeps counting instead of sticking.
        age_s = 0.0
        try:
            import datetime

            updated = timeline_props.last_updated_time
            if updated is not None and updated.year > 2000:
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=datetime.timezone.utc)
                age_s = (datetime.datetime.now(datetime.timezone.utc) - updated).total_seconds()
                age_s = max(0.0, min(age_s, 24 * 3600))
        except Exception:
            age_s = 0.0
        timeline = Timeline(
            position_ms=position_ms,
            duration_ms=duration_ms,
            playing=playing,
            updated_monotonic=time.monotonic() - (age_s if playing else 0.0),
        )
        return track, timeline, props

    def _publish(self, track, timeline) -> None:
        with self._lock:
            self._track = track
            self._timeline = timeline

    @staticmethod
    def _app_of(session) -> str:
        try:
            return session.source_app_user_model_id or ""
        except Exception:
            return ""

    @staticmethod
    async def _thumbnail(props) -> bytes | None:
        """The player's own art — usually a real album cover (Spotify's is
        300px, a YouTube tab a video frame). Decoded by the widget with PIL."""
        try:
            reference = props.thumbnail
            if reference is None:
                return None
            stream = await reference.open_read_async()
            try:
                size = stream.size
                if not size:
                    return None
                reader = _data_reader(stream)
                loaded = await reader.load_async(size)
                # winsdk's read_bytes wants a pre-sized array; read_buffer +
                # CryptographicBuffer is the reliable way to get the bytes out.
                from winsdk.windows.security.cryptography import CryptographicBuffer

                buffer = reader.read_buffer(loaded)
                return bytes(CryptographicBuffer.copy_to_byte_array(buffer))
            finally:
                stream.close()
        except Exception:
            return None


def _has_media(session) -> bool:
    try:
        return bool(session.get_playback_info())
    except Exception:
        return False


def _data_reader(stream):
    from winsdk.windows.storage.streams import DataReader

    return DataReader(stream)
