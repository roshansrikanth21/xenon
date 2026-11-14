"""The visualizer ring in its own process.

Drawing the rays costs ~20 ms a frame, and in the widget's process that
time came out of everything else (the corner record and its menu, the
beat glow, the audio analysis). Here the ring window and its drawing run
in a child process; the widget only sends each frame's numbers (the
spectrum bars and a few levels), which are a few hundred bytes. When the
child falls behind it draws the newest frame and drops the rest, so the
ring never lags the music.

A frame is four steps of similar cost: drawing the rays
(``ring_shapes``), their sheen (``ring_sheen``), colouring and packing
them (``pack_ring``) and the window update. Pillow and Windows let go of
Python's lock in each, so they run on four threads, a frame apart: the
ring keeps up near 60 fps where one thread managed ~30.

Messages from the widget, each a tuple:
    ("frame", bars, playing, energy, accent, intro, diameter)
    ("settings", {...})     MusicSettings as a dict
    ("opacity", 0..255)
    ("place", mode)         answered with ("placed",)
    ("sleep", True)         drop the window's image (idle)
    ("quit",)
"""
from __future__ import annotations

import threading
import time


class _Stage:
    """One step of a frame on its own thread: the newest item in, ``work``
    on it, the result handed to the next stage. An item still waiting when
    a newer one arrives is dropped, so a slow step never builds a queue."""

    def __init__(self, work, after=None, name="xenon-ring-stage") -> None:
        self.work, self.after = work, after
        self._next = None
        self._wake = threading.Condition()
        self._stop = False
        self.done = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self._thread.start()

    def give(self, item) -> None:
        with self._wake:
            self._next = item
            self._wake.notify()

    def drop(self) -> None:
        with self._wake:
            self._next = None
        if self.after is not None:
            self.after.drop()

    def stop(self) -> None:
        with self._wake:
            self._stop = True
            self._wake.notify()
        self._thread.join(1.0)
        if self.after is not None:
            self.after.stop()

    def _run(self) -> None:
        while True:
            with self._wake:
                while self._next is None and not self._stop:
                    self._wake.wait(0.5)
                if self._stop:
                    return
                item, self._next = self._next, None
            try:
                out = self.work(item)
                self.done += 1
                if self.after is not None and out is not None:
                    self.after.give(out)
            except Exception as error:          # keep the layer alive
                print(f"ring layer: {self._thread.name} failed: {error}", flush=True)
                time.sleep(0.5)


def run(conn, box, screen_height: int, layout) -> None:
    import os

    from . import music_scene as scene
    from .lyric_layer import _place
    from .music_settings import MusicSettings, coerce
    from .music_widget import _LayeredSurface

    surface = _LayeredSurface(box.w, box.h, x=box.x, y=box.y)
    if not surface.create():
        conn.send(("error",))
        return
    _place(surface.hwnd, "attach")
    conn.send(("ready", surface.hwnd))

    lock = threading.Lock()
    settings = MusicSettings()

    def present(data):
        with lock:
            surface.push_raw(data, box.w, box.h)

    def pack(item):
        alpha, accent = item
        return scene.pack_ring(box, alpha, accent)

    def sheen(item):
        shapes, d, st, intro, accent = item
        return scene.ring_sheen(shapes, d, st, intro), accent

    shown = _Stage(present, name="xenon-ring-present")
    packer = _Stage(pack, shown, name="xenon-ring-pack")
    presenter = _Stage(sheen, packer, name="xenon-ring-sheen")
    stats = os.environ.get("XENON_DOCK_FPS") == "1"
    drawn, cost, since = 0, 0.0, time.monotonic()

    def close():
        presenter.stop()
        with lock:
            surface.close()

    while True:
        frame = None
        try:
            if not conn.poll(0.25):
                with lock:
                    surface.pump()
                continue
            while conn.poll():
                msg = conn.recv()
                kind = msg[0]
                if kind == "quit":
                    close()
                    return
                if kind == "frame":
                    frame = msg                 # only the newest one is drawn
                elif kind == "settings":
                    settings = coerce(msg[1])
                elif kind == "opacity":
                    with lock:
                        surface.set_opacity(msg[1])
                elif kind == "sleep":
                    frame = None
                    presenter.drop()
                    with lock:
                        surface.push_raw(bytes(4), 1, 1)
                        surface._release_push_gdi()
                elif kind == "place":
                    with lock:
                        _place(surface.hwnd, msg[1])
                    conn.send(("placed",))
        except (EOFError, OSError):
            close()                             # the widget is gone
            return
        with lock:
            surface.pump()
        if frame is None:
            continue
        _kind, bars, playing, energy, accent, intro, diameter = frame
        start = time.monotonic()
        try:
            shapes = scene.ring_shapes(box, layout, bars, playing, energy, screen_height, settings,
                                       intro=intro, diameter=diameter)
            presenter.give((shapes, diameter or layout.orb_diameter, settings, intro, tuple(accent)))
        except Exception as error:              # keep the layer alive
            print(f"ring layer: frame failed: {error}", flush=True)
            time.sleep(0.5)
        if stats:
            drawn, cost = drawn + 1, cost + (time.monotonic() - start) * 1000
            if start - since >= 5.0:
                span = start - since
                print(f"ring layer: drawn {drawn / span:.0f} fps, shown {shown.done / span:.0f} fps, "
                      f"rays {cost / max(1, drawn):.1f} ms", flush=True)
                drawn, cost, since, shown.done = 0, 0.0, start, 0
