"""A child process waiting to become one of the widget's layers.

A fresh Python takes seconds to start and import Pillow and numpy (far
longer on a cold boot), and the widget used to start its layer processes
only once it had worked out where every window goes. Now they are started
the moment the widget starts, warm up their imports while it sets up the
audio and media session, and are then told which layer to be:

    ("init", "ring_layer" | "lyric_layer", args)

after which the pipe belongs to that layer's ``run(conn, *args)``.
"""
from __future__ import annotations

import importlib

LAYERS = ("ring_layer", "lyric_layer")


def run(conn) -> None:
    try:
        import numpy  # noqa: F401  (warm-up)
        from PIL import Image, ImageDraw, ImageFilter  # noqa: F401

        from . import music_scene, music_settings, music_widget  # noqa: F401
    except Exception:
        pass
    try:
        msg = conn.recv()
    except (EOFError, OSError):
        return
    if not msg or msg[0] != "init" or msg[1] not in LAYERS:
        return
    layer = importlib.import_module(f"{__package__}.{msg[1]}")
    layer.run(conn, *msg[2])


def spawn():
    """(conn, process) for a new host, started without importing the
    program's main module: with ``python -m xenon`` that is the whole
    assistant (planner, screen reader, chat), which a layer never needs."""
    import multiprocessing
    import sys

    ctx = multiprocessing.get_context("spawn")
    conn, child = ctx.Pipe()
    proc = ctx.Process(target=run, args=(child,), daemon=True, name="xenon-layer")
    main = sys.modules.get("__main__")
    saved = {k: main.__dict__[k] for k in ("__spec__", "__file__") if main is not None and k in main.__dict__}
    try:
        if main is not None:
            # multiprocessing re-imports the main module in the child from
            # its __spec__ (or __file__); with neither it imports nothing.
            main.__dict__["__spec__"] = None
            main.__dict__.pop("__file__", None)
        proc.start()
    finally:
        if main is not None:
            main.__dict__.update(saved)
    child.close()
    return conn, proc


_POOL: list = []


def prespawn(count: int = 2) -> None:
    """Start ``count`` hosts now, for ``take`` to hand out later."""
    while len(_POOL) < count:
        try:
            _POOL.append(spawn())
        except Exception:
            break


def take():
    """A live pre-started host, or a fresh one."""
    while _POOL:
        conn, proc = _POOL.pop(0)
        if proc.is_alive():
            return conn, proc
    return spawn()
