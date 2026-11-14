"""The lyric layer in its own process.

Python runs one thread's code at a time per process, and the widget's
rays (~22 ms a frame at 30 fps), spinning record and lyrics all wanted
that one slot: measured live, the lyrics got 5-8 frames a second with
gaps of up to a third of a second. Here the lyric window, its clock and
its animator run in a child process on their own core; the widget only
sends it what changed (track, position, colour, settings, placement).

Messages from the widget, each a tuple:
    ("state", {...})        track key/title, playing, position + when, duration,
                            accent, covered, sleeping
    ("lines", key, lines)   the synced lyrics for that track
    ("settings", {...})     MusicSettings as a dict
    ("opacity", 0..255)     whole-window alpha
    ("place", mode)         "attach" (wallpaper) or "float" (over windows);
                            answered with ("placed",)
    ("sleep", True)         drop the window's image (idle)
    ("quit",)
"""
from __future__ import annotations

import time


def _place(hwnd: int, mode: str) -> None:
    import ctypes

    from .wallpaper import (WS_EX_TRANSPARENT, attach_behind_icons, clear_top_most, detach_from_desktop,
                            send_to_top_most, show)

    if mode == "float":
        user = ctypes.windll.user32
        detach_from_desktop(hwnd)
        style = user.GetWindowLongPtrW(hwnd, -20)
        user.SetWindowLongPtrW(hwnd, -20, style | WS_EX_TRANSPARENT)
        send_to_top_most(hwnd)
        show(hwnd)
    else:
        clear_top_most(hwnd)
        attach_behind_icons(hwnd)


def position_now(state: dict, now: float | None = None) -> int:
    """The media session's position, carried forward while playing."""
    now = time.monotonic() if now is None else now
    pos = state.get("pos", 0)
    if state.get("playing"):
        pos += (now - state.get("updated", now)) * 1000
        if state.get("duration"):
            pos = min(pos, state["duration"])
    return int(pos)


def song_times(onsets, position_ms: int, now: float, keep_ms: int = 9000) -> list[int]:
    """Voice bursts (monotonic seconds, shared by both processes on
    Windows) as song positions, judged from where the song is now. Only
    the last few seconds matter."""
    out = []
    for t in onsets:
        ago = (now - t) * 1000
        if 0 <= ago <= keep_ms:
            out.append(int(position_ms - ago))
    return out


def run(conn, box, screen_height: int) -> None:
    from PIL import Image

    from . import music_scene as scene
    from .music_settings import MusicSettings, coerce
    from .music_sync import SpotifyProgress
    from .music_widget import _LayeredSurface

    surface = _LayeredSurface(box.w, box.h, x=box.x, y=box.y)
    if not surface.create():
        conn.send(("error",))
        return
    _place(surface.hwnd, "attach")
    conn.send(("ready", surface.hwnd))

    blank = Image.new("RGBA", (box.w, box.h), (0, 0, 0, 0))
    settings = MusicSettings()
    state: dict = {}
    lines: list = []
    word_times: dict = {}
    from collections import deque
    onsets: deque = deque(maxlen=48)       # monotonic times of voice bursts
    anim = scene.LyricAnimator()
    clock = scene.LyricClock()
    progress = SpotifyProgress()
    drawn = None
    import os
    stats = os.environ.get("XENON_DOCK_FPS") == "1"
    pushes, build, worst, last_push, since = 0, 0.0, 0.0, 0.0, time.monotonic()
    while True:
        start = time.monotonic()
        if stats and start - since >= 5.0:
            if pushes:
                print(f"lyric layer: animating {pushes} frames in 5 s, frame {build / pushes:.1f} ms, "
                      f"worst gap {worst * 1000:.0f} ms", flush=True)
            pushes, build, worst, since = 0, 0.0, 0.0, start
        try:
            while conn.poll():
                msg = conn.recv()
                kind = msg[0]
                if kind == "quit":
                    surface.close()
                    return
                if kind == "state":
                    was_asleep = state.get("sleeping")
                    state.update(msg[1])
                    if was_asleep and not state.get("sleeping"):
                        drawn = object()
                elif kind == "lines":
                    lines = msg[2]
                    word_times = msg[3] if len(msg) > 3 else {}
                    onsets.clear()
                    drawn = object()
                elif kind == "onset":
                    onsets.append(msg[1])
                elif kind == "settings":
                    settings = coerce(msg[1])
                    drawn = object()
                elif kind == "opacity":
                    surface.set_opacity(msg[1])
                elif kind == "sleep":
                    state["sleeping"] = True
                    surface.push_raw(bytes(4), 1, 1)
                    surface._release_push_gdi()
                    drawn = object()
                elif kind == "place":
                    _place(surface.hwnd, msg[1])
                    surface._region = None          # next frame in full
                    drawn = object()            # re-parenting drops the frame
                    conn.send(("placed",))
        except (EOFError, OSError):
            surface.close()                     # the widget is gone
            return
        surface.pump()
        if state.get("sleeping") or state.get("covered"):
            time.sleep(0.15)
            continue
        key, image, at = None, None, (0, 0)
        try:
            title = state.get("title")
            if title and lines and settings.show_lyrics:
                playing = bool(state.get("playing"))
                progress.poll(title, playing)
                reported = progress.position(title)
                if reported is None:
                    reported = position_now(state)
                position = clock.now(reported, playing, state.get("key"))
                position += int(settings.lyric_offset * 1000)
                heard = (song_times(onsets, position, time.monotonic())
                         if playing and getattr(settings, "follow_voice", True) else None)
                key, image, at = anim.frame_region(lines, position, state.get("duration", 0), box,
                                                   tuple(state.get("accent", (255, 255, 255))), screen_height,
                                                   settings, word_times, heard)
            if key != drawn:
                drawn = key
                surface.push_region(image, at)
                if stats and getattr(anim, "active", False):
                    done = time.monotonic()
                    if last_push and done - last_push < 0.5:
                        worst = max(worst, done - last_push)
                    pushes, build, last_push = pushes + 1, build + (done - start) * 1000, done
                elif stats:
                    last_push = 0.0
        except Exception as error:              # keep the layer alive
            print(f"lyric layer: frame failed: {error}", flush=True)
            time.sleep(0.5)
        period = 1.0 / max(20, settings.lyric_fps)
        time.sleep(max(0.002, period - (time.monotonic() - start)))
