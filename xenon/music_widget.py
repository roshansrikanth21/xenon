"""The desktop music interface — Weyv-style, living on the wallpaper.

A full-screen layered window re-parented into the wallpaper layer (see
``wallpaper.py``): the album cover blurred up into a room light behind
everything, the cover itself centered with a ring of spectrum bars radiating
from it, track title/artist beneath, and the current synced lyric line floating
near the bottom. Everything reacts to whatever is playing through the speakers
— Spotify, a YouTube tab, VLC — with no sign-in anywhere:

* audio: ``system_audio`` taps the default render endpoint via WASAPI loopback
* track/controls: ``smtc`` reads the Windows media session
* lyrics: ``lyrics_lookup`` fetches time-synced LRC from LRCLIB

Rendering is PIL → premultiplied BGRA → ``UpdateLayeredWindow`` at ~30 fps
while playing, and stops entirely when a maximized window covers the screen
(the widget sits underneath everything, so pixels nobody can see are waste —
Weyv's same trick cuts its CPU by ~95%).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import io
import math
import os
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .lyrics_lookup import LyricsLookup
from .system_audio import LoopbackSource
from .wallpaper import (
    attach_behind_icons,
    is_covered,
    make_tool_window,
    set_no_activate,
)

# Layered-window push constants.
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
GWL_EXSTYLE = -20

_REATTACH_SECONDS = 4.0
_COVERAGE_CHECK_SECONDS = 0.7
# Playing-state animation target. The player region is small, so 24 fps reads
# as smooth for a 33 rpm disc and leaves the rest of the core to the music.
_PLAYING_FPS = 20.0
# Nothing moves while playback is paused, so the idle loop renders rarely.
_IDLE_FRAME_SECONDS = 0.5
# The stage only changes on track/lyric events; this caps how often it
# repaints (the ghost fade quantizes to 4 steps over 1.5 s).
_STAGE_FRAME_SECONDS = 0.35
# SetWindowPos flags for stacking the player above the stage.
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010


# ------------------------------------------------------------ pure helpers

def spectrum_bars(
    samples: list[float],
    sample_rate: int,
    bars: int,
    fft_size: int = 2048,
    lo_hz: float = 40.0,
    hi_hz: float = 12000.0,
    min_db: float = -60.0,
    max_db: float = -10.0,
) -> list[float]:
    """Fold the newest ``fft_size`` samples into ``bars`` log-spaced 0..1
    magnitudes, the way an AnalyserNode feeds a visualizer."""
    import numpy as np

    values = np.zeros(bars, dtype=float)
    window_samples = np.asarray(samples[-fft_size:], dtype=float)
    if window_samples.size < 16:
        return values.tolist()
    window = np.blackman(window_samples.size)
    spectrum = np.abs(np.fft.rfft(window_samples * window))
    freqs = np.fft.rfftfreq(window_samples.size, 1.0 / sample_rate)

    edges = np.geomspace(max(lo_hz, freqs[1] + 1e-6), hi_hz, bars + 1)
    for i in range(bars):
        band = spectrum[(freqs >= edges[i]) & (freqs < edges[i + 1])]
        if band.size == 0:
            continue
        power = float(band.max()) / window_samples.size
        db = 20.0 * math.log10(power + 1e-9)
        values[i] = (db - min_db) / (max_db - min_db)
    return [min(1.0, max(0.0, v)) for v in values]


def palette_from_image(image: Image.Image, count: int = 3) -> list[tuple[int, int, int]]:
    """The album's main colours, most important first: the cover is reduced
    to a handful of colours and each is scored by how much of the cover it
    fills and how colourful it is (near-black, near-white and grey count
    for little), so a pink desert cover gives pink, not its one red pixel."""
    import colorsys

    small = image.convert("RGB").resize((48, 48))
    try:
        quant = small.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        pal = quant.getpalette() or []
        counts = sorted(quant.getcolors() or [], reverse=True)
        swatches = [(n, tuple(pal[i * 3:i * 3 + 3])) for n, i in counts]
    except Exception:
        swatches = [(1, c) for c in small.getdata()]
    total = sum(n for n, _ in swatches) or 1
    scored = []
    for n, (r, g, b) in swatches:
        h, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        colourful = sat * min(1.0, val * 1.6)          # dark or grey: little colour
        scored.append(((n / total) ** 0.6 * (0.08 + colourful), (r, g, b)))
    scored.sort(reverse=True)
    palette: list[tuple[int, int, int]] = []
    for _, color in scored:
        if any(sum(abs(a - b) for a, b in zip(color, picked)) < 60 for picked in palette):
            continue
        palette.append(color)
        if len(palette) >= count:
            break
    return palette or [(120, 130, 160)]


def current_line(lines: list[tuple[int, str]], position_ms: int) -> str:
    """The lyric line being sung at ``position_ms`` ("", before the first)."""
    result = ""
    for time_ms, text in lines:
        if time_ms <= position_ms:
            result = text
        else:
            break
    return result


def smooth_bars(previous: list[float], target: list[float], attack: float = 0.5, decay: float = 0.15) -> list[float]:
    """Fast attack, slow decay — bars that snap up and fall gracefully."""
    out = []
    for i, want in enumerate(target):
        have = previous[i] if i < len(previous) else 0.0
        rate = attack if want > have else decay
        out.append(have + (want - have) * rate)
    return out


def cover_rect(width: int, height: int) -> tuple[int, int, int]:
    """The stage layout: (cx, cy, diameter) of the cover's circle."""
    diameter = int(min(height * 0.30, width * 0.20) * 2)
    return width // 2, int(height * 0.38), diameter


def player_region(width: int, height: int) -> tuple[int, int, int, int]:
    """The square around the vinyl that actually animates, as
    ``(x, y, w, h)``: the disc plus the spectrum ring, tonearm and its
    shadow. Everything outside it is the static stage, so while playing
    only this region — a fraction of the screen's pixels — is re-rendered
    and re-pushed every frame."""
    cx, cy, diameter = cover_rect(width, height)
    r = diameter / 2
    bar_reach = r + max(6, diameter // 40) + height * 0.11 * 1.6
    half = int(max(r * 1.16, bar_reach)) + 48
    size = half * 2
    px = max(0, cx - half)
    py = max(0, cy - half)
    return px, py, min(size, width - px), min(size, height - py)


# ------------------------------------------------------------ vinyl

VINYL_RPM = 33.3333
LABEL_FRACTION = 0.40      # album-art label diameter / record diameter
HOTKEY_ID = 0x584D         # "XM"
HOTKEY_FULL = 0x5846       # "XF": Ctrl+Alt+F full-screen view
VK_F = 0x46
BACKDROP_CLASS = "XenonMusicBackdrop"
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
VK_M = 0x4D
WM_HOTKEY = 0x0312


def vinyl_angle(elapsed_s: float, rpm: float = VINYL_RPM) -> float:
    """Rotation in degrees after ``elapsed_s`` seconds of play (clockwise)."""
    return -(elapsed_s * rpm / 60.0 * 360.0) % 360.0


def render_record(size: int, cover: "Image.Image | None") -> "Image.Image":
    """The record as one image: vinyl, grooves and the album label.

    Kept for compatibility and tests; the live renderer uses ``build_vinyl``
    so only the small label is rotated per frame."""
    disc, label = build_vinyl(size, cover)
    if label is not None:
        half = size // 2
        lr = label.width // 2
        disc.alpha_composite(label, (half - lr, half - lr))
    return disc


def build_vinyl(size: int, cover: "Image.Image | None") -> tuple["Image.Image", "Image.Image | None"]:
    """The vinyl split into its static and moving halves.

    The grooves, rim and track gaps are concentric circles — rotationally
    symmetric — so rotating the whole record every frame buys nothing.
    Returns ``(disc, label)``: the disc is static and rendered once, and the
    label (the album art, 40% of the diameter, or ``None`` when there is no
    art to spin) is the only part that rotates."""
    scale = 2  # supersample, then downsize for smooth groove edges
    big = size * scale
    record = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(record)
    c = big / 2
    draw.ellipse((0, 0, big - 1, big - 1), fill=(14, 14, 16, 255))
    label_r = big * LABEL_FRACTION / 2
    outer = big / 2 - big * 0.012
    r = outer
    i = 0
    step = max(2.0, big / 260)
    while r > label_r + big * 0.035:
        shade = 22 if i % 2 else 10
        if i % 37 == 0:
            shade = 34  # track gap
        draw.ellipse((c - r, c - r, c + r, c + r), outline=(shade, shade, shade + 2, 255), width=1)
        r -= step
        i += 1
    draw.ellipse((c - outer, c - outer, c + outer, c + outer), outline=(40, 40, 44, 255), width=max(2, scale * 2))

    if cover is not None:
        disc = record.resize((size, size), Image.LANCZOS)
        lsize = int(round(size * LABEL_FRACTION)) & ~1
        label = Image.new("RGBA", (lsize, lsize), (0, 0, 0, 0))
        art = cover.convert("RGBA").resize((lsize, lsize), Image.LANCZOS)
        mask = Image.new("L", (lsize, lsize), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, lsize - 1, lsize - 1), fill=255)
        label.paste(art, (0, 0), mask)
        ldraw = ImageDraw.Draw(label)
        ldraw.ellipse(
            (0, 0, lsize - 1, lsize - 1), outline=(0, 0, 0, 160),
            width=max(1, scale),
        )
        hole = max(3, int(size * 0.018))
        ldraw.ellipse(
            (lsize // 2 - hole, lsize // 2 - hole,
             lsize // 2 + hole, lsize // 2 + hole), fill=(8, 8, 10, 255),
        )
        return disc, label

    # No art: the fallback label is a solid disc — static, so bake it in.
    record = record.resize((size, size), Image.LANCZOS)
    lr = int(size * LABEL_FRACTION / 2)
    ImageDraw.Draw(record).ellipse(
        (size // 2 - lr, size // 2 - lr, size // 2 + lr, size // 2 + lr),
        fill=(120, 40, 50, 255),
    )
    ImageDraw.Draw(record).ellipse(
        (size // 2 - lr, size // 2 - lr, size // 2 + lr, size // 2 + lr),
        outline=(0, 0, 0, 160), width=2,
    )
    hole = max(3, int(size * 0.018))
    ImageDraw.Draw(record).ellipse(
        (size // 2 - hole, size // 2 - hole, size // 2 + hole, size // 2 + hole),
        fill=(8, 8, 10, 255),
    )
    return record, None


def render_sheen(size: int) -> "Image.Image":
    """Fixed light reflection over the grooves: two soft opposing wedges, the
    way a studio light catches vinyl. It does not rotate with the record."""
    sheen = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sheen)
    for start, alpha in ((-60, 34), (120, 22)):
        draw.pieslice((0, 0, size - 1, size - 1), start, start + 34, fill=(255, 255, 255, alpha))
    sheen = sheen.filter(ImageFilter.GaussianBlur(size / 30))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    label = int(size * LABEL_FRACTION / 2)
    half = size // 2
    ImageDraw.Draw(mask).ellipse((half - label, half - label, half + label, half + label), fill=0)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(sheen, (0, 0), mask)
    return out


def tonearm_points(cx: int, cy: int, diameter: int, playing: bool) -> dict:
    """Tonearm geometry: pivot at the upper right of the record, the arm
    swung onto the outer grooves while playing and parked off it otherwise."""
    r = diameter / 2
    pivot = (cx + r * 1.08, cy - r * 0.92)
    angle = math.radians(112 if playing else 96)
    length = r * 0.95
    head = (pivot[0] + math.cos(angle) * length, pivot[1] + math.sin(angle) * length)
    return {"pivot": pivot, "head": head, "radius": max(6, int(r * 0.07))}


# ------------------------------------------------------------ font loading

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = (
        ["segoeuib.ttf", "arialbd.ttf"] if bold else ["segoeui.ttf", "arial.ttf"]
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


# Segoe UI has no Indic or CJK glyphs, so Hindi/Telugu/Tamil lyrics drew as
# empty boxes. Pick a Windows font that covers the text's script.
_SCRIPT_FONTS = (
    ((0x0900, 0x0DFF), ("Nirmala.ttc", 1), ("Nirmala.ttc", 0)),   # Indic scripts
    ((0x3040, 0x30FF), ("YuGothB.ttc", 0), ("YuGothM.ttc", 0)),   # kana
    ((0xAC00, 0xD7AF), ("malgunbd.ttf", 0), ("malgun.ttf", 0)),   # Hangul
    ((0x4E00, 0x9FFF), ("msyhbd.ttc", 0), ("msyh.ttc", 0)),       # CJK ideographs
)


def script_font_file(text: str, bold: bool) -> tuple[str, int] | None:
    """The font file (and face index) covering the first non-Latin script in
    ``text``, or None when Segoe UI is fine."""
    for ch in text or "":
        code = ord(ch)
        for (lo, hi), bold_face, regular_face in _SCRIPT_FONTS:
            if lo <= code <= hi:
                return bold_face if bold else regular_face
    return None


_font_cache: dict = {}


def font_for(text: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    face = script_font_file(text, bold)
    key = (face, size, bold)
    if key in _font_cache:
        return _font_cache[key]
    font = None
    if face is not None:
        try:
            font = ImageFont.truetype(face[0], size, index=face[1])
        except OSError:
            font = None
    if font is None:
        font = _font(size, bold)
    _font_cache[key] = font
    return font


# ------------------------------------------------------------ window layer

# One wndproc per registered class name; RegisterClassW fails if the same
# name is registered twice, and the widget runs two surfaces of one class.
_registered_classes: dict = {}


class _LyricProcess:
    """Stand-in for the lyric layer's window, which lives in a child process
    (``lyric_layer``) so its animation gets a core of its own. Offers the
    few surface calls the scene makes; everything else is a message."""

    def __init__(self, box, screen_height: int) -> None:
        self.box = box
        self.screen_height = screen_height
        self.hwnd = 0
        self.opacity = 255
        self._sent_opacity = None
        self._conn = None
        self._proc = None

    def create(self) -> bool:
        import multiprocessing

        from . import lyric_layer

        try:
            ctx = multiprocessing.get_context("spawn")
            self._conn, child = ctx.Pipe()
            self._proc = ctx.Process(target=lyric_layer.run, args=(child, self.box, self.screen_height),
                                     daemon=True, name="xenon-lyrics")
            self._proc.start()
            child.close()
            if self._conn.poll(25):
                msg = self._conn.recv()
                if msg[0] == "ready":
                    self.hwnd = msg[1]
                    return True
        except Exception as error:
            print(f"music widget: lyric process failed ({error}); drawing lyrics in-process", flush=True)
        self.close()
        return False

    def send(self, *msg) -> None:
        try:
            if self._conn is not None:
                self._conn.send(msg)
        except (OSError, EOFError, ValueError):
            pass

    def place(self, mode: str) -> None:
        """Attach / float the window, and wait (briefly) until it is done so
        the layers above it can be stacked after it."""
        self.send("place", mode)
        try:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and self._conn is not None:
                if self._conn.poll(0.05) and self._conn.recv()[0] == "placed":
                    if mode == "float":
                        # A background process can't raise its own window;
                        # stack it from here, in order with the other layers.
                        from .wallpaper import send_to_top_most
                        send_to_top_most(self.hwnd)
                    return
        except (OSError, EOFError):
            pass

    def set_opacity(self, value) -> None:
        value = max(0, min(255, int(value)))
        self.opacity = value
        if value != self._sent_opacity:
            self._sent_opacity = value
            self.send("opacity", value)

    def push(self, image) -> bool:
        return True

    def push_raw(self, data, width: int, height: int) -> bool:
        self.send("sleep", True)            # the only raw push the scene sends it
        return True

    def _release_push_gdi(self) -> None:
        pass

    def pump(self) -> int:
        return 0

    def close(self) -> None:
        self.send("quit")
        proc = self._proc
        if proc is not None:
            proc.join(1.5)
            if proc.is_alive():
                proc.terminate()
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = self._proc = None
        self.hwnd = 0


class _LayeredSurface:
    """A borderless per-pixel-alpha window, driven from the thread that owns
    it. Deliberately bare Win32 (no Tk): the widget is re-parented into the
    wallpaper layer, where Tk's assumptions about top-level windows break."""

    def __init__(
        self, width: int, height: int, x: int = 0, y: int = 0,
        class_name: str = "XenonMusicWidget",
    ) -> None:
        self.width = width
        self.height = height
        self.x = x
        self.y = y
        self.hwnd = 0
        self._class = class_name
        # Reusable GDI objects for push(): one DIB + memory DC per size.
        self._gdi_dc = None
        self._bitmap = 0
        self._bits = None
        self._push_size: tuple[int, int] | None = None
        self.opacity = 255         # whole-window alpha, from the settings

    # -- declarations -------------------------------------------------------
    def _declare(self) -> None:
        user = ctypes.windll.user32
        gdi = ctypes.windll.gdi32
        user.RegisterClassW.argtypes = [ctypes.c_void_p]
        user.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, ctypes.c_void_p, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        user.CreateWindowExW.restype = wintypes.HWND
        user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.DefWindowProcW.restype = wintypes.LPARAM
        user.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
            ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
            wintypes.COLORREF, ctypes.c_void_p, wintypes.DWORD,
        ]
        user.UpdateLayeredWindow.restype = wintypes.BOOL
        user.GetDC.restype = wintypes.HDC
        user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user.SetProcessDPIAware.restype = wintypes.BOOL
        user.GetSystemMetrics.argtypes = [ctypes.c_int]
        user.GetSystemMetrics.restype = ctypes.c_int
        user.PeekMessageW.argtypes = [
            ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
        ]
        # Every handle-taking call needs argtypes: on 64-bit Python an HDC or
        # HWND above 2**31 overflows ctypes' default int conversion.
        user.GetDC.argtypes = [wintypes.HWND]
        user.ReleaseDC.restype = ctypes.c_int
        user.DestroyWindow.argtypes = [wintypes.HWND]
        user.DestroyWindow.restype = wintypes.BOOL
        user.TranslateMessage.argtypes = [ctypes.c_void_p]
        user.DispatchMessageW.argtypes = [ctypes.c_void_p]
        user.DispatchMessageW.restype = wintypes.LPARAM
        kernel = ctypes.windll.kernel32
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi.CreateCompatibleDC.restype = wintypes.HDC
        gdi.DeleteDC.argtypes = [wintypes.HDC]
        gdi.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD,
        ]
        gdi.CreateDIBSection.restype = wintypes.HBITMAP
        gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HBITMAP]
        gdi.SelectObject.restype = wintypes.HBITMAP
        gdi.DeleteObject.argtypes = [wintypes.HBITMAP]
        self._declared = True

    # -- lifecycle ----------------------------------------------------------
    def create(self) -> bool:
        self._declare()
        user = ctypes.windll.user32
        try:
            user.SetProcessDPIAware()
        except Exception:
            pass

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        if self._class not in _registered_classes:
            wndproc = ctypes.WINFUNCTYPE(
                wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
            )(lambda hwnd, msg, wparam, lparam: user.DefWindowProcW(hwnd, msg, wparam, lparam))
            wc = WNDCLASS()
            wc.style = 0
            wc.lpfnWndProc = wndproc
            wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
            wc.lpszClassName = self._class
            if not user.RegisterClassW(ctypes.byref(wc)):
                return False
            _registered_classes[self._class] = wndproc

        self.hwnd = user.CreateWindowExW(
            WS_EX_LAYERED, self._class, "Xenon Music", WS_POPUP,
            self.x, self.y, self.width, self.height, None, None, None, None,
        )
        if not self.hwnd:
            return False
        user.ShowWindow(self.hwnd, 8)  # SW_SHOWNOACTIVATE
        make_tool_window(self.hwnd)
        set_no_activate(self.hwnd, True)
        return True

    def close(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.DestroyWindow(self.hwnd)
            self.hwnd = 0
        self._release_push_gdi()

    # -- frame push ---------------------------------------------------------
    def push(self, image: Image.Image) -> bool:
        """UpdateLayeredWindow one RGBA frame. PIL's raw "BGRa" packer
        premultiplies and swaps channels in one pass (half the time of the
        convert/split/merge route)."""
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        return self.push_raw(image.tobytes("raw", "BGRa"), image.width, image.height)

    def push_raw(self, data: bytes, width: int, height: int) -> bool:
        """UpdateLayeredWindow one premultiplied-BGRA frame. The memory DC
        and DIB are cached per surface — recreating them every frame showed
        up as real CPU in the profile."""
        user = ctypes.windll.user32
        gdi = ctypes.windll.gdi32

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [
                ("BlendOp", wintypes.BYTE),
                ("BlendFlags", wintypes.BYTE),
                ("SourceConstantAlpha", wintypes.BYTE),
                ("AlphaFormat", wintypes.BYTE),
            ]

        class BIHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        if len(data) < width * height * 4:
            return False
        if self._push_size != (width, height):
            self._release_push_gdi()
            screen_dc = user.GetDC(None)
            header = BIHEADER()
            header.biSize = ctypes.sizeof(BIHEADER)
            header.biWidth = width
            header.biHeight = -height  # top-down
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0  # BI_RGB
            bits = ctypes.c_void_p()
            self._bitmap = gdi.CreateDIBSection(
                screen_dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0
            )
            user.ReleaseDC(None, screen_dc)
            if not self._bitmap or not bits.value:
                self._release_push_gdi()
                return False
            self._bits = bits.value
            self._push_size = (width, height)
        # Created after the size check: _release_push_gdi() above also drops
        # the DC, so creating it first left every push without one.
        if self._gdi_dc is None:
            screen_dc = user.GetDC(None)
            self._gdi_dc = gdi.CreateCompatibleDC(screen_dc)
            user.ReleaseDC(None, screen_dc)
        if not (self._bits and self._gdi_dc):
            return False
        ctypes.memmove(self._bits, data, width * height * 4)
        self._region = (0, 0, width, height)
        return self._present(width, height)

    def push_region(self, image, at=(0, 0)) -> bool:
        """For a mostly-empty layer: write only ``image`` at ``at`` into the
        window's pixel buffer (clearing the last frame's block first) and
        present. A full-size frame cost 7 ms just to convert; this block is
        a fraction of it, copied with one numpy slice."""
        import numpy as np

        w, h = self.width, self.height
        last = getattr(self, "_region", None)
        if self._push_size != (w, h) or not self._bits or last is None:
            full = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            if image is not None:
                full.paste(image, at)
            ok = self.push(full)
            self._region = self._clip(image, at)
            return ok
        buf = np.ctypeslib.as_array((ctypes.c_uint8 * (w * h * 4)).from_address(self._bits)).reshape(h, w * 4)
        x0, y0, x1, y1 = last
        if x1 > x0 and y1 > y0:
            buf[y0:y1, x0 * 4:x1 * 4] = 0
        rect = self._clip(image, at)
        x0, y0, x1, y1 = rect
        if x1 > x0 and y1 > y0:
            ix, iy = x0 - at[0], y0 - at[1]
            part = image.crop((ix, iy, ix + x1 - x0, iy + y1 - y0)) if (ix or iy or x1 - x0 != image.width
                                                                        or y1 - y0 != image.height) else image
            if part.mode != "RGBA":
                part = part.convert("RGBA")
            buf[y0:y1, x0 * 4:x1 * 4] = np.frombuffer(part.tobytes("raw", "BGRa"), np.uint8).reshape(
                y1 - y0, (x1 - x0) * 4)
        self._region = rect
        return self._present(w, h)

    def _clip(self, image, at) -> tuple:
        if image is None:
            return (0, 0, 0, 0)
        x0, y0 = max(0, at[0]), max(0, at[1])
        x1, y1 = min(self.width, at[0] + image.width), min(self.height, at[1] + image.height)
        return (x0, y0, max(x0, x1), max(y0, y1))

    def _present(self, width: int, height: int) -> bool:
        user = ctypes.windll.user32
        gdi = ctypes.windll.gdi32

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [
                ("BlendOp", wintypes.BYTE),
                ("BlendFlags", wintypes.BYTE),
                ("SourceConstantAlpha", wintypes.BYTE),
                ("AlphaFormat", wintypes.BYTE),
            ]

        ok = False
        if self._bits and self._gdi_dc:
            previous = gdi.SelectObject(self._gdi_dc, self._bitmap)

            screen_dc = user.GetDC(None)
            # pptDst = NULL keeps the window where it is; passing a position
            # would re-place the window on every frame (this moved the player
            # region to the top-left corner, its local origin, each push).
            size = wintypes.SIZE(width, height)
            src_point = wintypes.POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, self.opacity)), AC_SRC_ALPHA)
            ok = bool(user.UpdateLayeredWindow(
                self.hwnd, screen_dc, None, ctypes.byref(size),
                self._gdi_dc, ctypes.byref(src_point), 0, ctypes.byref(blend), ULW_ALPHA,
            ))
            user.ReleaseDC(None, screen_dc)
            gdi.SelectObject(self._gdi_dc, previous)
        return ok

    def set_opacity(self, value: int) -> None:
        """Change the whole window's alpha. Once a frame is on screen this
        updates only the blend (no new pixels), so fades cost nothing."""
        value = max(0, min(255, int(value)))
        if value == self.opacity:
            return
        self.opacity = value
        if not self.hwnd or self._push_size is None:
            return

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("BlendOp", wintypes.BYTE), ("BlendFlags", wintypes.BYTE),
                        ("SourceConstantAlpha", wintypes.BYTE), ("AlphaFormat", wintypes.BYTE)]

        blend = BLENDFUNCTION(AC_SRC_OVER, 0, value, AC_SRC_ALPHA)
        ctypes.windll.user32.UpdateLayeredWindow(
            self.hwnd, None, None, None, None, None, 0, ctypes.byref(blend), ULW_ALPHA)

    def _release_push_gdi(self) -> None:
        self._region = None
        gdi = ctypes.windll.gdi32
        if self._bitmap:
            gdi.DeleteObject(self._bitmap)
            self._bitmap = 0
        if self._gdi_dc is not None:
            gdi.DeleteDC(self._gdi_dc)
            self._gdi_dc = None
        self._bits = None
        self._push_size = None

    def pump(self) -> int:
        """Drain the message queue so the re-parented window stays healthy.
        Returns how many times the toggle hotkey was pressed."""
        msg = wintypes.MSG()
        hits = 0
        # No hwnd filter: a thread-level WM_HOTKEY (hwnd None) arrives here too.
        while ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                hits += 1
                continue
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_FULL:
                self.full_hits = getattr(self, "full_hits", 0) + 1
                continue
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
        return hits


# ------------------------------------------------------------ pointer

def _work_area_bottom(height: int) -> int:
    """Top of the taskbar on the primary monitor (the work area's bottom)."""
    rect = wintypes.RECT()
    try:
        user = ctypes.windll.user32
        user.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
        if user.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.bottom
    except Exception:
        pass
    return height


def _bounced(disc, scale: float, size: int):
    """The disc scaled by ``scale`` and centred on a ``size`` canvas."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = max(2, int(disc.width * scale))
    if d != disc.width:
        disc = disc.resize((d, d), Image.BILINEAR)
    off = (size - d) // 2
    if off < 0:
        disc = disc.crop((-off, -off, -off + size, -off + size))
        off = 0
    canvas.alpha_composite(disc, (off, off))
    return canvas


class _KeepFocus:
    """Playing a song may have to bring the browser forward (to load the
    Spotify tab). Afterwards put it back: minimised again if it was, else
    behind the other windows, and hand focus back to where it was (the
    desktop, when the click came from the dock)."""

    _BROWSER_CLASSES = ("Chrome_WidgetWin_1", "MozillaWindowClass")

    def __enter__(self):
        self._prev = None
        self._iconic: dict[int, bool] = {}
        if sys.platform != "win32":
            return self
        try:
            user32 = ctypes.windll.user32
            self._prev = user32.GetForegroundWindow()
            proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            def collect(hwnd, _):
                buffer = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, buffer, 64)
                if buffer.value in self._BROWSER_CLASSES and user32.IsWindowVisible(hwnd):
                    self._iconic[int(hwnd)] = bool(user32.IsIconic(hwnd))
                return True

            self._cb = proto(collect)
            user32.EnumWindows(self._cb, 0)
        except Exception:
            pass
        return self

    def __exit__(self, *_exc):
        if sys.platform != "win32" or not self._prev:
            return False
        try:
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            if fg and fg != self._prev and int(fg) in self._iconic:
                if self._iconic[int(fg)]:
                    user32.ShowWindow(fg, 7)            # SW_SHOWMINNOACTIVE
                else:
                    # HWND_BOTTOM, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
                    user32.SetWindowPos(fg, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                user32.SetForegroundWindow(self._prev)
        except Exception:
            pass
        return False


class _Pointer:
    """Where the mouse is, whether the left button is down, and whether the
    desktop itself is under it.

    The widget's windows stay click-through (the shell's icon view owns the
    wallpaper's input), so instead of receiving clicks the widget watches the
    pointer: a press and release over one of its controls, with the desktop
    — not some app window — under the pointer, counts as a click."""

    # Clicks count on bare desktop, and on the full-screen view's backdrop.
    DESKTOP = frozenset({"SysListView32", "SHELLDLL_DefView", "WorkerW", "Progman", BACKDROP_CLASS})

    def __init__(self) -> None:
        user = ctypes.windll.user32
        user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = wintypes.HWND
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user.GetAsyncKeyState.restype = ctypes.c_short
        user.GetSystemMetrics.argtypes = [ctypes.c_int]
        self._user = user
        self._buf = ctypes.create_unicode_buffer(64)
        self._last_hwnd = None
        self._last_desktop = False
        # Swapped mouse buttons: the "primary" button is VK_RBUTTON.
        self._button = 0x02 if user.GetSystemMetrics(23) else 0x01

    # -- click poller ---------------------------------------------------------
    # The render loop runs every 16-50 ms, too slow to see a quick tap; a tiny
    # thread samples the button at ~250 Hz and queues whole clicks. The low
    # bit of GetAsyncKeyState ("pressed since the last call") catches even a
    # click shorter than one sample.

    def start(self) -> None:
        import collections

        self._clicks = collections.deque(maxlen=16)
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._poll, name="xenon-music-pointer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        halt = getattr(self, "_halt", None)
        if halt is not None:
            halt.set()

    def clicks(self) -> list:
        out = []
        queue = getattr(self, "_clicks", None)
        while queue:
            out.append(queue.popleft())
        return out

    def _sample(self):
        point = wintypes.POINT()
        self._user.GetCursorPos(ctypes.byref(point))
        state = self._user.GetAsyncKeyState(self._button)
        return point.x, point.y, bool(state & 0x8000), bool(state & 1)

    def _desktop_at(self, x: int, y: int) -> bool:
        hwnd = self._user.WindowFromPoint(wintypes.POINT(x, y))
        buf = ctypes.create_unicode_buffer(64)
        self._user.GetClassNameW(hwnd, buf, 64)
        return buf.value in self.DESKTOP

    def _poll(self) -> None:
        down_at = None
        self._sample()   # clear a stale "pressed since" bit
        while not self._halt.wait(0.004):
            x, y, down, tapped = self._sample()
            if down and down_at is None:
                down_at = (x, y, self._desktop_at(x, y))
            elif not down and down_at is not None:
                self._clicks.append((*down_at, x, y, self._desktop_at(x, y)))
                down_at = None
            elif not down and tapped:
                desk = self._desktop_at(x, y)
                self._clicks.append((x, y, desk, x, y, desk))

    def read(self) -> tuple[int, int, bool, bool]:
        point = wintypes.POINT()
        if not self._user.GetCursorPos(ctypes.byref(point)):
            return -1, -1, False, False
        hwnd = self._user.WindowFromPoint(point)
        if hwnd != self._last_hwnd:
            self._last_hwnd = hwnd
            self._user.GetClassNameW(hwnd, self._buf, 64)
            self._last_desktop = self._buf.value in self.DESKTOP
        down = bool(self._user.GetAsyncKeyState(self._button) & 0x8000)
        return point.x, point.y, self._last_desktop, down


# ------------------------------------------------------------ the widget

class MusicWidget:
    """Runs the render loop. Construct and ``start()``; ``stop()`` when the
    session ends. Degrades by stage: no winsdk → no track info (visualizer
    only); no loopback → static ring; neither → a plain ambient glow."""

    def __init__(self, full_screen: bool = True) -> None:
        self.full_screen = full_screen
        self._surface: _LayeredSurface | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        from .smtc import NowPlaying

        self._now_playing = NowPlaying()
        self._audio = LoopbackSource()
        self._lyrics = LyricsLookup()
        self._bars: list[float] = [0.0] * 64
        self._track_key: tuple | None = None
        self._backdrop: Image.Image | None = None
        self._cover: Image.Image | None = None
        self._palette: list[tuple[int, int, int]] = [(120, 130, 160)]
        self._font_title = None
        self._font_artist = None
        self._font_lyric = None
        self._has_frame = False
        self._needs_frame = False
        self._sheen: Image.Image | None = None
        self._disc: Image.Image | None = None
        self._disc_key: tuple | None = None
        self._disc_dim: Image.Image | None = None
        self._disc_dim_key: tuple | None = None
        self._label: Image.Image | None = None
        self._shadow: Image.Image | None = None
        self._shadow_key: int | None = None
        self._backdrop_key: tuple | None = None
        self._base_frame: Image.Image | None = None
        self._base_key: tuple | None = None
        self._player_surface: _LayeredSurface | None = None
        self._player_rect: tuple[int, int, int, int] | None = None
        self._stage_key: tuple | None = None
        self._spin = 0.0           # accumulated play time driving the rotation
        self._last_tick = time.monotonic()
        self.overlay = False       # Ctrl+Alt+Shift+M floats the player above windows
        from .music_settings import SettingsWatcher

        self._settings = SettingsWatcher()
        self._surfaces: dict = {}
        self._art_key = -1
        from .music_history import MusicHistory

        self._history = MusicHistory()
        from .music_suggest import Suggester
        from .music_cover import CoverFinder
        self._suggester = Suggester()
        self._covers = CoverFinder()
        self._hires_key = None
        self._disc_cache: dict = {}
        self._suggest_for = None
        self._dock_rows: list = []
        self._controller = None
        self._settings_proc = None
        self.on_open_settings = None   # set by the orb to open its own window
        self._track_changed_at = 0.0
        self._ready = threading.Event()
        self._mutex = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> bool:
        """Start the render thread and wait (at most a few seconds) until the
        layers exist. Only one widget runs per user session: a second start
        (e.g. the orb plus a manual ``xenon music-widget``) returns False."""
        if self.running:
            return True
        if not self._claim_instance():
            print("music widget: already running", flush=True)
            return False
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="xenon-music", daemon=True)
        self._thread.start()
        started = time.monotonic()
        self._ready.wait(timeout=6.0)
        if self._surface is None:
            if self._thread.is_alive():
                # A cold start (first audio/media-session setup after boot or
                # right after the last widget was killed) can take longer;
                # keep it rather than silently giving up.
                print("music widget: still starting...", flush=True)
                self._ready.wait(timeout=24.0)
                if self._surface is not None:
                    print(f"music widget: up after {time.monotonic() - started:.1f} s", flush=True)
                    return True
            print("music widget: could not create its windows", flush=True)
            self.stop()
            return False
        return True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None
        self._release_instance()

    def _claim_instance(self) -> bool:
        # A named mutex marks "a widget is running in this session"; it dies
        # with the last handle, so a crashed widget never blocks a new one.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateMutexW(None, False, "Local\\XenonMusicWidget")
        if not handle:
            return True  # cannot tell; do not block the widget on it
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel.CloseHandle(handle)
            return False
        self._mutex = handle
        return True

    def _release_instance(self) -> None:
        if self._mutex:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(self._mutex)
            self._mutex = None

    # -- commands (voice fast-path hooks land here later) --------------------
    def play_pause(self) -> None:
        self._now_playing.play_pause()

    def next_track(self) -> None:
        self._now_playing.next_track()

    def previous_track(self) -> None:
        self._now_playing.previous_track()

    # -- worker -------------------------------------------------------------
    def _run(self) -> None:
        """Three transparent layers over the wallpaper (see ``music_scene``):
        the orb (cover disc + visualizer, the only thing animating every
        frame), the lyrics (redrawn when a word lights up or a line enters),
        and the now-playing card (redrawn about once a second).

        Settings (``music_settings``) are re-read live; a change that moves
        the layers (side, orb size) tears the windows down and rebuilds."""
        user = ctypes.windll.user32
        width = user.GetSystemMetrics(0)
        height = user.GetSystemMetrics(1)
        if not self.full_screen:
            width, height = width // 3, height // 3

        self._now_playing.start()
        audio_ok, _ = self._audio.start()

        user.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        hotkey_ok = bool(user.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_M))
        if not hotkey_ok:
            print("music widget: Ctrl+Alt+Shift+M is taken by another app", flush=True)
        full_ok = bool(user.RegisterHotKey(None, HOTKEY_FULL, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_F))
        if not full_ok:
            print("music widget: Ctrl+Alt+F is taken by another app", flush=True)
        try:
            while not self._stop.is_set():
                if self._scene(width, height, audio_ok) != "rebuild":
                    break
        finally:
            self._ready.set()
            if hotkey_ok:
                user.UnregisterHotKey(None, HOTKEY_ID)
            if full_ok:
                user.UnregisterHotKey(None, HOTKEY_FULL)
            self._surface = None
            self._player_surface = None
            self._player_rect = None
            self._audio.stop()
            self._now_playing.stop()

    def _scene(self, width: int, height: int, audio_ok: bool) -> str:
        """Build the layers for the current layout and drive them until the
        widget stops ("stop") or the layout changes ("rebuild").

        Layers, bottom to top: halo (static per track), ring (the visualizer,
        every frame), disc (album art, turns slowly), lyrics, card, dock."""
        from dataclasses import replace as _replace

        from . import music_dock as dock
        from . import music_scene as scene

        settings = self._settings.current
        layout = scene.scene_layout(width, height, settings.side, settings.orb_size)
        art_d = layout.orb_diameter
        # Room round the disc for its beat bounce (it scales up to ~8%).
        disc_w = int(art_d * 1.1) | 1
        disc_box = scene.Box(int(layout.orb_cx - disc_w / 2), int(layout.orb_cy - disc_w / 2), disc_w, disc_w)
        floor = _work_area_bottom(height)
        dock_full = dock.dock_geometry(width, height, dock.MAX_ROWS, floor)
        if settings.show_dock and settings.side != "left":
            # Keep the card clear of the dock's disc in the corner.
            clear = max(dock_full.panel.x, dock_full.disc_center[0] + int(dock_full.disc * 0.62))
            card = layout.card_box
            if card.x < clear:
                layout = _replace(layout, card_box=scene.Box(clear, card.y, card.w, card.h))
        # Beat glow round the screen: full-length bands on all four edges
        # (or just the corners), drawn once per colour; the bass sets their
        # opacity every ring frame.
        if settings.glow_shape == "corners":
            cw, ch = int(width * 0.26), int(height * 0.5)
            glow_boxes = {
                "corner_tl": scene.Box(0, 0, cw, ch), "corner_tr": scene.Box(width - cw, 0, cw, ch),
                "corner_bl": scene.Box(0, floor - ch, cw, ch),
                "corner_br": scene.Box(width - cw, floor - ch, cw, ch)}
        else:
            bh, bw = int(height * 0.17), int(width * 0.075)
            glow_boxes = {
                "edge_top": scene.Box(0, 0, width, bh), "edge_bottom": scene.Box(0, floor - bh, width, bh),
                "edge_left": scene.Box(0, 0, bw, floor), "edge_right": scene.Box(width - bw, 0, bw, floor)}
        corner_names = list(glow_boxes)
        order = corner_names + ["halo", "ring", "disc", "lyrics", "card"] + (
            ["dock", "vinyl"] if settings.show_dock else [])
        boxes = {
            **glow_boxes,
            "halo": layout.orb_box, "ring": layout.orb_box, "disc": disc_box,
            "lyrics": layout.lyric_box, "card": layout.card_box, "dock": dock_full.box,
            "vinyl": dock.vinyl_box(dock_full),
        }
        surfaces: dict[str, _LayeredSurface] = {}
        for name in order:
            box = boxes[name]
            if name == "lyrics" and os.environ.get("XENON_LYRICS_INPROCESS") != "1":
                remote = _LyricProcess(box, height)
                if remote.create():
                    remote.set_opacity(255 * settings.opacity)
                    surfaces[name] = remote
                    continue
            surface = _LayeredSurface(box.w, box.h, x=box.x, y=box.y)
            if not surface.create():
                for made in surfaces.values():
                    made.close()
                return "stop"
            surface.opacity = 0 if name in corner_names else int(255 * settings.opacity)
            surfaces[name] = surface
        self._surfaces = surfaces
        self._surface = surfaces["lyrics"]
        self._player_surface = surfaces["ring"]
        self._player_rect = (layout.orb_box.x, layout.orb_box.y, layout.orb_box.w, layout.orb_box.h)
        self._place_all(surfaces, order)
        self._ready.set()

        blank = {name: Image.new("RGBA", (boxes[name].w, boxes[name].h), (0, 0, 0, 0)) for name in order}
        pointer = _Pointer()
        pointer.start()
        # The corner record and its pop-out menu run on their own thread
        # (``_dock_loop``); this loop only hands it the current state.
        from types import SimpleNamespace
        self._painting()
        share = SimpleNamespace(track=None, playing=False, accent=(255, 255, 255), settings=settings,
                                layout=layout, covered=False, repaint=0, rows_dirty=0, openness=0.0,
                                timeline=None, sleeping=False)
        dock_stop = threading.Event()
        dock_thread = None
        if "dock" in surfaces:
            dock_thread = threading.Thread(
                target=self._dock_loop, daemon=True, name="xenon-music-dock",
                args=(share, dock_stop, surfaces, boxes, pointer, dock_full, width, height, floor))
            dock_thread.start()
        lyric_proc = surfaces["lyrics"] if isinstance(surfaces["lyrics"], _LyricProcess) else None
        lyric_sent: dict = {}
        lyric_thread = threading.Thread(
            target=self._lyric_loop, daemon=True, name="xenon-music-lyrics",
            args=(share, dock_stop, surfaces["lyrics"], blank["lyrics"], height))
        if lyric_proc is None:
            lyric_thread.start()

        last_attach = time.monotonic()
        last_coverage = 0.0
        covered = False
        next_ring = next_lyric = next_card = 0.0
        last_played = time.monotonic()
        sleeping = False
        self._vinyl_drawn = None
        self._disc_drawn = None
        lyric_key = card_key = object()
        corner_key = None
        orb_art = None
        orb_art_key = None
        halo_key = None
        disc_frame = 0
        last_tick = time.monotonic()
        orb_idle_done = False
        recorded = None
        result = "stop"

        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if surfaces["card"].pump() % 2:
                    if self.overlay and not self.fullscreen:
                        # Back to the wallpaper with fresh windows: layers that
                        # were floated come back blank if re-attached.
                        self.overlay = False
                        result = "rebuild"
                        break
                    self._toggle_overlay(*[surfaces[n] for n in order])
                    covered = False
                    lyric_key = card_key = object()
                    share.repaint += 1
                    corner_key = None
                    orb_idle_done = False
                    halo_key = None
                    self._vinyl_drawn = self._disc_drawn = None
                if getattr(surfaces["card"], "full_hits", 0):
                    flips = surfaces["card"].full_hits
                    surfaces["card"].full_hits = 0
                    if flips % 2 and self.fullscreen:
                        self._close_backdrop()
                        self.overlay = bool(getattr(self, "_overlay_before_full", False))
                        if not self.overlay:
                            result = "rebuild"        # fresh windows on the wallpaper
                            break
                    elif flips % 2:
                        self._toggle_fullscreen(surfaces, order, width, height)
                        covered = False
                        last_played = now           # waking it counts as activity
                        # re-parenting drops each window's frame: repaint all
                        lyric_key = card_key = object()
                        share.repaint += 1
                        corner_key = None
                        orb_idle_done = False
                        halo_key = None
                        self._vinyl_drawn = self._disc_drawn = None

                if self._settings.poll(now):
                    fresh = self._settings.current
                    if fresh.layout_key() != settings.layout_key():
                        result = "rebuild"
                        break
                    settings = fresh
                    with self._painting():
                        for name, surface in surfaces.items():
                            if name not in corner_names:      # those follow the beat
                                surface.set_opacity(255 * settings.opacity)
                    lyric_key = card_key = object()
                    share.repaint += 1
                    corner_key = None
                    orb_art_key = halo_key = None
                    orb_idle_done = False
                    if not settings.pause_when_covered:
                        covered = False

                if not self.overlay and now - last_attach >= _REATTACH_SECONDS:
                    last_attach = now
                    from .wallpaper import get_parent
                    if any(not get_parent(s.hwnd) for s in surfaces.values()):
                        self._place_all(surfaces, order)

                if (settings.pause_when_covered and not self.overlay
                        and now - last_coverage >= _COVERAGE_CHECK_SECONDS):
                    last_coverage = now
                    covered = is_covered(surfaces["ring"].hwnd, (0, 0, width, height))

                track, timeline = self._now_playing.state()
                self._refresh_assets(track, timeline)
                playing = bool(timeline and timeline.playing)

                # -- sleep: no music for a while -> only the corner record --
                if playing:
                    last_played = now
                idle_after = settings.idle_minutes * 60
                want_sleep = (idle_after > 0 and not playing and now - last_played >= idle_after
                              and not self.fullscreen)
                if want_sleep != sleeping:
                    sleeping = want_sleep
                    share.sleeping = sleeping
                    if sleeping:
                        with self._painting():
                            audio_ok = self._sleep_surfaces(surfaces)
                    else:
                        audio_ok, _ = self._audio.start()
                        self._track_changed_at = now        # come back with the fade-in
                        lyric_key = card_key = object()
                        share.repaint += 1
                        corner_key = None
                        orb_art_key = halo_key = None
                        orb_idle_done = False
                        self._disc_drawn = None
                if lyric_proc is not None:
                    res = self._lyrics.result() or {}
                    lines = res.get("lines") or []
                    want = (self._track_key, len(lines), lines[0] if lines else None)
                    if lyric_sent.get("lines") != want:
                        lyric_sent["lines"] = want
                        lyric_proc.send("lines", want, lines)
                    if lyric_sent.get("settings") is not settings:
                        lyric_sent["settings"] = settings
                        from dataclasses import asdict as _asdict
                        lyric_proc.send("settings", _asdict(settings))
                    msg = {
                        "title": track.title if track else "", "key": self._track_key, "playing": playing,
                        "pos": timeline.position_ms if timeline else 0,
                        "updated": timeline.updated_monotonic if timeline else 0.0,
                        "duration": timeline.duration_ms if timeline else 0,
                        "accent": settings.accent_rgb(scene.accent_from_palette(self._palette)),
                        "covered": covered, "sleeping": sleeping,
                    }
                    if lyric_sent.get("state") != msg:
                        lyric_sent["state"] = msg
                        lyric_proc.send("state", msg)
                # Record once the art has settled (players publish it late).
                if (track and recorded != (self._track_key, id(self._cover))
                        and now - self._track_changed_at > 2.5):
                    recorded = (self._track_key, id(self._cover))
                    try:
                        self._history.record(track.title, track.artist, track.app, self._cover)
                    except Exception:
                        pass
                    share.rows_dirty += 1
                share.covered = covered
                if covered:
                    time.sleep(0.25)
                    continue
                if self._needs_frame:
                    self._needs_frame = False
                    lyric_key = card_key = object()
                    share.repaint += 1
                    corner_key = None
                    orb_idle_done = False

                accent = settings.accent_rgb(scene.accent_from_palette(self._palette))
                position = timeline.position_now() if timeline else 0
                duration = timeline.duration_ms if timeline else 0
                since_change = now - self._track_changed_at
                intro = dock.ease_out_back(since_change / 0.7) if self._track_changed_at else 1.0
                fade_in = dock.ease_out_cubic(since_change / 0.45) if self._track_changed_at else 1.0

                # -- dock: its own thread draws it; hand over the state ------
                share.track, share.playing, share.accent = track, playing, accent
                share.settings, share.layout, share.timeline = settings, layout, timeline
                if dock_thread is not None:
                    # The open menu shows what the card shows: hand over.
                    surfaces["card"].set_opacity(
                        255 * settings.opacity * (1.0 - dock.ease_out_cubic(min(1.0, share.openness * 3.0))))
                    # ...and the lyrics step back so the menu reads clearly.
                    surfaces["lyrics"].set_opacity(
                        255 * settings.opacity * (1.0 - 0.75 * dock.ease_out_cubic(share.openness)))

                if sleeping:
                    time.sleep(0.03)        # the record and its dock stay live
                    continue

                # -- card: title, art, progress --------------------------------
                if now >= next_card:
                    next_card = now + 1.0
                    progress = (position / duration) if duration else 0.0
                    key = (self._track_key, playing, int(progress * 300), settings.show_card, id(self._cover))
                    if key != card_key:
                        card_key = key
                        if track and settings.show_card:
                            image = scene.render_card(layout.card_box, track.title, track.artist,
                                                      self._cover, progress, playing, accent)
                        else:
                            image = blank["card"]
                        surfaces["card"].push(image)

                # -- corners: one picture per colour, the beat sets opacity --
                look = (accent, settings.corner_glow > 0)
                if corner_key != look:
                    corner_key = look
                    with self._painting():
                        for name in corner_names:
                            box_ = boxes[name]
                            kind, where = name.split("_", 1)
                            draw = scene.corner_glow if kind == "corner" else scene.edge_glow
                            surfaces[name].push(draw(box_.w, box_.h, where, accent) if look[1] else blank[name])

                # -- orb: halo once per look, ring every frame, disc half rate --
                art_key = (id(self._cover), art_d, accent, settings.disc, settings.halo, settings.backdrop)
                if orb_art_key != art_key:
                    orb_art = scene.OrbArt(art_d, self._cover, accent, settings.disc, settings.halo, settings.backdrop)
                    orb_art_key = art_key
                    halo_key = None
                    orb_idle_done = False
                fading = fade_in < 0.999 or intro < 0.999
                for name in ("halo", "ring", "disc"):
                    surfaces[name].set_opacity(255 * settings.opacity * (fade_in if track else 0.0))
                if halo_key != (art_key, bool(track)):
                    halo_key = (art_key, bool(track))
                    surfaces["halo"].push(scene.render_halo(layout.orb_box, layout, orb_art) if track
                                          else blank["halo"])
                    disc_frame = 0
                active = playing or fading or max(map(abs, self._bars), default=0.0) >= 0.004
                if track and (active or not orb_idle_done):
                    if now < next_ring:
                        time.sleep(min(0.008, max(0.002, next_ring - now)))
                        continue
                    next_ring = now + 1.0 / settings.fps
                    if audio_ok and playing:
                        samples = self._audio.read_latest(2048)
                        rate = self._audio.format.sample_rate if self._audio.format else 48000
                        target = [min(1.2, v * settings.sensitivity) for v in spectrum_bars(samples, rate, 64)]
                    else:
                        target = [0.0] * 64
                    keep = settings.smoothing
                    self._bars = smooth_bars(
                        self._bars, target,
                        attack=0.8 - 0.55 * keep,
                        decay=(0.4 - 0.36 * keep) if playing else 0.35,
                    )
                    dt = min(now - last_tick, 0.25)
                    last_tick = now
                    if playing:
                        self._spin = (self._spin - dt * settings.spin_speed) % 360.0
                    energy = sum(self._bars[:12]) / 12 if self._bars else 0.0
                    # Beat bounce: a kick is bass well above its recent
                    # average; the pulse jumps on it and eases back down.
                    bass = sum(self._bars[:6]) / 6 if self._bars else 0.0
                    self._bass_avg = 0.92 * getattr(self, "_bass_avg", bass) + 0.08 * bass
                    # The bass level depends on the song and the volume (it
                    # can sit near 0.02): judge it against its own recent
                    # peak, so every track pulses about as hard.
                    self._bass_peak = max(bass, getattr(self, "_bass_peak", 0.0) * (0.996 ** (dt * 30)), 0.004)
                    level = bass / self._bass_peak
                    kick = max(0.0, bass - self._bass_avg * 1.15) / self._bass_peak * 2.5 if playing else 0.0
                    self._pulse = max(min(1.0, kick), getattr(self, "_pulse", 0.0) * (0.82 ** (dt * 30)))
                    # The screen corners breathe with the bass and flash on
                    # each kick (only the windows' opacity changes).
                    glow = (settings.corner_glow * min(1.0, 0.85 * self._pulse + 0.35 * level ** 2)
                            if playing else 0.0)
                    for name in corner_names:
                        surfaces[name].set_opacity(255 * settings.opacity * min(1.0, glow) * fade_in)
                    if os.environ.get("XENON_DOCK_FPS") == "1":
                        self._glow_max = max(getattr(self, "_glow_max", 0.0), glow)
                        if now - getattr(self, "_glow_logged", 0.0) > 5:
                            self._glow_logged = now
                            print(f"music widget: glow max {self._glow_max:.2f} bass {bass:.2f} "
                                  f"pulse {self._pulse:.2f} fade {fade_in:.2f} "
                                  f"pushed {surfaces[corner_names[0]]._push_size}", flush=True)
                            self._glow_max = 0.0
                    scale = 1.0 + 0.07 * settings.bounce * self._pulse
                    box = layout.orb_box
                    surfaces["ring"].push_raw(scene.render_ring_bgra(
                        box, layout, self._bars, playing, energy, accent, height, settings,
                        intro=intro if track else 0.0, diameter=int(art_d * scale),
                    ), box.w, box.h)
                    # The bounce has only ~a dozen visible sizes: quantise and
                    # cache each one's packed bytes while the disc stays still.
                    level = int(round((scale - 1.0) / 0.006))
                    bounce_key = (level, id(orb_art), playing, round(self._spin, 1) if settings.spin_speed else 0)
                    if bounce_key != getattr(self, "_disc_drawn", None) or fading:
                        self._disc_drawn = bounce_key
                        cache = self._disc_cache
                        if cache.get("art") is not orb_art or settings.spin_speed or fading:
                            cache.clear()
                            cache["art"] = orb_art
                        packed = cache.get((level, playing))
                        if packed is None:
                            disc = scene.render_disc(orb_art, self._spin, playing)
                            img = (_bounced(disc, 1.0 + level * 0.006, disc_box.w) if disc is not None
                                   else blank["disc"])
                            packed = img.tobytes("raw", "BGRa")
                            if not settings.spin_speed and not fading:
                                cache[(level, playing)] = packed
                        surfaces["disc"].push_raw(packed, disc_box.w, disc_box.h)
                    disc_frame += 1
                    if not active:
                        orb_idle_done = True
                elif not track and not orb_idle_done:
                    for name in ("ring", "disc"):
                        surfaces[name].push(blank[name])
                    orb_idle_done = True
                else:
                    last_tick = now
                    for name in corner_names:
                        surfaces[name].set_opacity(0)
                    time.sleep(0.03)
        finally:
            dock_stop.set()
            if dock_thread is not None:
                dock_thread.join(timeout=2.0)
            if lyric_thread.is_alive():
                lyric_thread.join(timeout=2.0)
            pointer.stop()
            from .wallpaper import detach_from_desktop
            for surface in surfaces.values():
                try:
                    if not isinstance(surface, _LyricProcess):
                        detach_from_desktop(surface.hwnd)
                    surface.close()
                except Exception:
                    pass
            self._surfaces = {}
            self._close_backdrop()
        return result

    def _place_all(self, surfaces: dict, order: list[str]) -> None:
        with self._painting():
            self._place_all_locked(surfaces, order)

    def _place_all_locked(self, surfaces: dict, order: list[str]) -> None:
        """Stack the layers in ``order`` (bottom first) on the wallpaper or,
        in overlay mode, above every window."""
        if self.overlay:
            for name in order:
                self._float(surfaces[name])
        else:
            # Each attach sends its window to the bottom, so go top-down.
            for name in reversed(order):
                self._attach(surfaces[name])

    @staticmethod
    def _on_orb(layout, x: int, y: int) -> bool:
        r = layout.orb_diameter / 2
        return (x - layout.orb_cx) ** 2 + (y - layout.orb_cy) ** 2 <= r * r

    def _painting(self) -> "threading.RLock":
        """Held while pushing the dock's frames and while re-parenting or
        re-fading windows, so the dock thread never paints a window that is
        halfway through being moved."""
        lock = getattr(self, "_paint_lock", None)
        if lock is None:
            lock = self._paint_lock = threading.RLock()
        return lock

    def _dock_loop(self, share, stop, surfaces, boxes, pointer, dock_full, width, height, floor) -> None:
        """The corner record and its pop-out menu, on their own thread at up
        to 60 fps. The main loop's ring and lyric frames take 20-40 ms each,
        which made the spin and the fan-out stutter while they shared it."""
        from dataclasses import replace as _replace

        from . import music_dock as dock

        renderer = dock.DockRenderer()
        geom = dock_full
        content = dock.DockContent()
        key = None
        open_ = False
        openness = 0.0
        hover_since = None
        last_inside = 0.0
        press = 0.0
        drawn = vinyl_drawn = None
        repaint = share.repaint
        spin = 0.0
        last_vinyl = 0.0
        last = time.monotonic()
        count_fps = os.environ.get("XENON_DOCK_FPS") == "1"
        frames, since = 0, last
        while not stop.is_set():
            start = time.monotonic()
            busy = False
            if count_fps and start - since >= 5.0:
                print(f"music widget: dock {frames / (start - since):.0f} fps", flush=True)
                frames, since = 0, start
            try:
                now = start
                dt = min(0.1, now - last)
                last = now
                if share.covered:
                    time.sleep(0.25)
                    continue
                if share.repaint != repaint:
                    repaint = share.repaint
                    drawn = vinyl_drawn = None
                settings, track, playing, accent = share.settings, share.track, share.playing, share.accent
                exclude = (track.title.strip().lower(), track.artist.strip().lower()) if track else None
                if settings.suggestions and track and self._suggest_for != self._track_key:
                    self._suggest_for = self._track_key
                    recent = [(e.title, e.artist) for e in self._history.recent(exclude, 4)]
                    self._suggester.request(track.title, track.artist, recent)
                suggest_v = self._suggester.version if settings.suggestions else -1
                want = (self._track_key, self._art_key, suggest_v, share.rows_dirty)
                if key != want:
                    picks = []
                    if settings.suggestions:
                        picks = [x for x in self._suggester.current() if x.key != exclude][:3]
                    taken = {x.key for x in picks}
                    recent = [e for e in self._history.recent(exclude, dock.MAX_ROWS) if e.key not in taken]
                    entries = picks + recent[:dock.MAX_ROWS - len(picks)]
                    rows = []
                    for i, entry in enumerate(entries):
                        if i < len(picks):
                            rows.append((entry.title, entry.artist, entry.cover, True))
                        else:
                            rows.append((entry.title, entry.artist, self._history.thumbnail(entry), False))
                    self._dock_rows = entries
                    geom = _replace(dock.dock_geometry(width, height, len(rows), floor), box=dock_full.box)
                    content = dock.DockContent(track.title if track else "", track.artist if track else "",
                                               playing, self._cover, rows)
                    key = want
                    renderer.render(geom, content, 1.0, None, accent, draw_disc=False)   # build the pieces now
                    renderer.prepare(geom, content.cover)
                    drawn = None
                if content.playing != playing or content.cover is not self._cover:
                    content = dock.DockContent(content.title, content.artist, playing, self._cover, content.rows)
                    renderer.prepare(geom, content.cover)
                x, y, on_desktop, _down = pointer.read()
                inside = on_desktop and dock.in_dock(geom, x, y, open_)
                if inside:
                    last_inside = now
                    hover_since = hover_since or now
                    if not open_ and now - hover_since > 0.12:
                        open_ = True
                else:
                    hover_since = None
                    if open_ and now - last_inside > 0.5:
                        open_ = False
                hover = dock.hit(geom, x, y, openness > 0.85) if inside else None
                for (px, py, desk_down, ux, uy, desk_up) in pointer.clicks():
                    # A click counts when it pressed and released on the same
                    # control, with the desktop under the pointer.
                    def target_at(cx_, cy_, desk_):
                        if not desk_:
                            return None
                        if dock.in_dock(geom, cx_, cy_, open_):
                            return dock.hit(geom, cx_, cy_, open_)
                        return "orb" if self._on_orb(share.layout, cx_, cy_) else None
                    target = target_at(px, py, desk_down)
                    if target and target == target_at(ux, uy, desk_up):
                        press = 1.0
                        if target == "disc":
                            open_ = not open_
                            last_inside = now
                        elif self._dock_action(target):
                            open_ = False
                step = dt / 0.5
                openness = min(1.0, openness + step) if open_ else max(0.0, openness - step * 1.4)
                press = max(0.0, press - dt / 0.16)
                share.openness = openness
                if playing:
                    spin = (spin + dt * 90.0) % 360.0
                eased = dock.ease_out_cubic(openness)
                with self._painting():
                    frame = (round(openness, 3), hover, id(content), accent)
                    if frame != drawn:
                        drawn = frame
                        surfaces["dock"].push(renderer.render(geom, content, openness, hover, accent,
                                                              draw_disc=False))
                    turn = (round(spin, 2), round(eased, 3), round(press, 2), id(content.cover))
                    # The grown record costs ~13 ms a frame (mostly the
                    # turn itself): 40 fps while the menu is open.
                    calm = eased > 0.99 and turn[1:] == (vinyl_drawn or (None,))[1:]
                    if turn != vinyl_drawn and not (calm and now - last_vinyl < 1 / 40):
                        vinyl_drawn = turn
                        last_vinyl = now
                        surfaces["vinyl"].push(renderer.render_vinyl(
                            geom, boxes["vinyl"], content, eased, accent, press, spin))
                        frames += 1
                busy = playing or 0.0 < openness < 1.0 or press > 0.0 or inside
            except Exception as error:
                print(f"music widget: dock frame failed: {error}", flush=True)
                time.sleep(0.5)
            period = 1.0 / 60 if busy else 1.0 / 20
            time.sleep(max(0.002, period - (time.monotonic() - start)))

    def _lyric_loop(self, share, stop, surface, blank, height) -> None:
        """The lyric layer on its own thread: a steady clock (``LyricClock``)
        and cheap sprite frames (``LyricAnimator``), pushed only when the
        picture changes, up to ``lyric_fps`` times a second. On the render
        loop they had to wait behind 20-ms ray frames, which made the
        highlight stutter."""
        from . import music_scene as scene

        from .music_sync import SpotifyProgress

        anim = scene.LyricAnimator()
        clock = scene.LyricClock()
        progress = SpotifyProgress()
        drawn = None
        repaint = share.repaint
        stats = os.environ.get("XENON_DOCK_FPS") == "1"
        pushes, build_ms, worst_gap, last_push, since = 0, 0.0, 0.0, 0.0, time.monotonic()
        while not stop.is_set():
            start = time.monotonic()
            settings = share.settings
            if stats and start - since >= 5.0:
                if pushes:
                    print(f"music widget: lyrics {pushes / (start - since):.0f} fps, "
                          f"frame {build_ms / pushes:.1f} ms, worst gap {worst_gap * 1000:.0f} ms", flush=True)
                pushes, build_ms, worst_gap, since = 0, 0.0, 0.0, start
            try:
                if share.sleeping or share.covered:
                    time.sleep(0.2)
                    continue
                if share.repaint != repaint:
                    repaint = share.repaint
                    drawn = object()
                track, timeline, layout = share.track, share.timeline, share.layout
                key, image = None, None
                if track and timeline and settings.show_lyrics:
                    lines = (self._lyrics.result() or {}).get("lines") or []
                    if lines:
                        # Spotify's own progress (official API, when logged
                        # in) is exact; the media session is the fallback.
                        progress.poll(track.title, timeline.playing)
                        reported = progress.position(track.title)
                        if reported is None:
                            reported = timeline.position_now()
                        position = clock.now(reported, timeline.playing, self._track_key)
                        position += int(settings.lyric_offset * 1000)
                        key, image = anim.frame(lines, position, timeline.duration_ms, layout.lyric_box,
                                                share.accent, height, settings)
                if key != drawn:
                    drawn = key
                    with self._painting():
                        if not share.sleeping:
                            surface.push(image if image is not None else blank)
                    self._has_frame = True
                    if stats:
                        done = time.monotonic()
                        if last_push and done - last_push < 0.5:
                            worst_gap = max(worst_gap, done - last_push)
                        pushes, build_ms, last_push = pushes + 1, build_ms + (done - start) * 1000, done
            except Exception as error:
                print(f"music widget: lyric frame failed: {error}", flush=True)
                time.sleep(0.5)
            period = 1.0 / max(20, settings.lyric_fps)
            time.sleep(max(0.002, period - (time.monotonic() - start)))

    _SLEEP_KEEP = ("dock", "vinyl")

    def _sleep_surfaces(self, surfaces) -> bool:
        """Idle: shrink every layer but the corner record and its dock to
        1x1 (Windows frees the window's image), drop our own frame buffers
        and caches, stop audio capture and trim the working set. Returns
        the new audio state (off)."""
        import gc

        for name, surface in surfaces.items():
            if name in self._SLEEP_KEEP:
                continue
            surface.push_raw(bytes(4), 1, 1)
            surface._release_push_gdi()
        self._disc_cache.clear()
        try:
            from .music_scene import _RING_LAYER
            _RING_LAYER.clear()
        except Exception:
            pass
        try:
            self._audio.stop()
        except Exception:
            pass
        gc.collect()
        if sys.platform == "win32":
            try:
                kernel = ctypes.windll.kernel32
                kernel.SetProcessWorkingSetSize(kernel.GetCurrentProcess(), ctypes.c_size_t(-1), ctypes.c_size_t(-1))
            except Exception:
                pass
        return False

    def _dock_action(self, target: str) -> bool:
        """Run a dock click off the render thread. Returns True when the dock
        should fold away afterwards (a song was picked)."""
        if target == "disc":
            return False
        if target == "now":
            return False
        if target == "settings":
            self.open_settings()
            return True
        if target in ("playpause", "orb"):
            action = self._now_playing.play_pause
        elif target == "next":
            action = self._now_playing.next_track
        elif target == "previous":
            action = self._now_playing.previous_track
        elif target.startswith("row"):
            rows = getattr(self, "_dock_rows", [])
            index = int(target[3:])
            if index >= len(rows):
                return False
            entry = rows[index]

            def action(entry=entry):
                from .music import MusicCommand, MusicController

                # Fresh each time, so a `xenon spotify login` done while the
                # widget runs is picked up.
                self._controller = MusicController.from_env()
                with _KeepFocus():
                    reply = self._controller.run(
                        MusicCommand("play", f"{entry.title}\x1f{entry.artist}", "track"))
                print(f"music widget: {reply}", flush=True)
        else:
            return False

        def run():
            try:
                action()
            except Exception as error:
                print(f"music widget: {target} failed: {error}", flush=True)

        threading.Thread(target=run, daemon=True, name="xenon-music-action").start()
        return target.startswith("row")

    def open_settings(self) -> None:
        """The settings window: the orb's own when the widget runs inside the
        orb (``on_open_settings``), otherwise a small separate process."""
        if self.on_open_settings is not None:
            self.on_open_settings()
            return
        proc = self._settings_proc
        if proc is not None and proc.poll() is None:
            return
        import subprocess
        import sys

        self._settings_proc = subprocess.Popen(
            [sys.executable, "-m", "xenon", "music-widget", "--settings"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _attach(self, surface) -> None:
        if isinstance(surface, _LyricProcess):
            surface.place("attach")
            return
        from .wallpaper import clear_top_most
        clear_top_most(surface.hwnd)
        attach_behind_icons(surface.hwnd)

    def _float(self, surface: "_LayeredSurface") -> None:
        if isinstance(surface, _LyricProcess):
            surface.place("float")
            return
        from .wallpaper import WS_EX_TRANSPARENT, detach_from_desktop, send_to_top_most, show
        user = ctypes.windll.user32
        detach_from_desktop(surface.hwnd)
        style = user.GetWindowLongPtrW(surface.hwnd, GWL_EXSTYLE)
        user.SetWindowLongPtrW(surface.hwnd, GWL_EXSTYLE, style | WS_EX_TRANSPARENT)
        send_to_top_most(surface.hwnd)
        show(surface.hwnd)

    def _toggle_fullscreen(self, surfaces: dict, order: list[str], width: int, height: int) -> None:
        with self._painting():
            self._toggle_fullscreen_locked(surfaces, order, width, height)

    def _toggle_fullscreen_locked(self, surfaces: dict, order: list[str], width: int, height: int) -> None:
        """Ctrl+Alt+F: a dark backdrop over every window with the whole
        scene floated on top; again to put it back on the wallpaper. The
        backdrop takes clicks (so the dock and orb work and nothing behind
        gets clicked) but never focus."""
        from .wallpaper import send_to_top_most, show

        if getattr(self, "_backdrop", None) is None:
            backdrop = _LayeredSurface(width, height, 0, 0, class_name=BACKDROP_CLASS)
            if not backdrop.create():
                return
            # A fresh top-level window from this process can come up
            # shell-cloaked (invisible); the widget's own layers never do,
            # because they start as children of the desktop and float from
            # there. Take the same path, then paint it (re-parenting drops
            # whatever was pushed before).
            from .wallpaper import detach_from_desktop
            attach_behind_icons(backdrop.hwnd)
            detach_from_desktop(backdrop.hwnd)
            user = ctypes.windll.user32
            style = user.GetWindowLongPtrW(backdrop.hwnd, GWL_EXSTYLE)
            user.SetWindowLongPtrW(backdrop.hwnd, GWL_EXSTYLE, style & ~0x20)   # takes clicks (WS_EX_TRANSPARENT off)
            send_to_top_most(backdrop.hwnd)
            show(backdrop.hwnd)
            backdrop.push_raw(Image.new("RGBA", (width, height), (0, 0, 0, 236)).tobytes("raw", "BGRa"),
                              width, height)
            backdrop._release_push_gdi()      # the window keeps the frame
            self._backdrop = backdrop
            self._overlay_before_full = self.overlay
            if not self.overlay:
                self._toggle_overlay(*[surfaces[n] for n in order])
            else:
                for name in order:                # re-float above the backdrop
                    self._float(surfaces[name])
            # The fresh backdrop's own re-parenting can settle after the
            # layers float (one layer ended up under it): pin it just below
            # the lowest layer.
            user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user.SetWindowPos(backdrop.hwnd, surfaces[order[0]].hwnd, 0, 0, 0, 0,
                              0x0001 | 0x0002 | 0x0010)   # NOSIZE | NOMOVE | NOACTIVATE
        else:
            self._close_backdrop()
            if self.overlay and not getattr(self, "_overlay_before_full", False):
                self._toggle_overlay(*[surfaces[n] for n in order])
        self._needs_frame = True

    def _close_backdrop(self) -> None:
        backdrop = getattr(self, "_backdrop", None)
        if backdrop is not None:
            try:
                backdrop.close()
            except Exception:
                pass
        self._backdrop = None

    @property
    def fullscreen(self) -> bool:
        return getattr(self, "_backdrop", None) is not None

    def _toggle_overlay(self, *surfaces) -> None:
        with self._painting():
            self._toggle_overlay_locked(*surfaces)

    def _toggle_overlay_locked(self, *surfaces) -> None:
        """Wallpaper <-> floating overlay for every layer. The overlay is
        click-through and never takes focus, so it can stay up while you work."""
        self.overlay = not self.overlay
        self._needs_frame = True
        if self.overlay:
            for surface in surfaces:            # each float goes to the top: bottom first
                self._float(surface)
        else:
            # each attach goes to the bottom: top layer first, like _place_all
            for surface in reversed(surfaces):
                self._attach(surface)

    def _refresh_assets(self, track, timeline) -> None:
        if not track:
            if self._track_key is not None:
                self._track_key = None
                self._needs_frame = True
                self._lyrics.request(None)
            return
        key = (track.title, track.artist, track.album)
        art_key = len(track.art or b"")
        if key == self._track_key:
            if self._hires_key != key:
                hires = self._covers.take(track.title, track.artist)
                if hires is not None:
                    # The full-size cover arrived: it beats the thumbnail.
                    self._hires_key = key
                    self._cover = hires
                    self._palette = palette_from_image(hires)
                    self._art_key = art_key
                    self._needs_frame = True
                    return
            elif art_key != self._art_key:
                self._art_key = art_key
                return
            if art_key == self._art_key:
                return
            # The art often lands a poll or two after the title: take it then.
            self._art_key = art_key
            self._needs_frame = True
            try:
                self._cover = Image.open(io.BytesIO(track.art)).convert("RGB") if track.art else None
            except Exception:
                self._cover = None
            self._palette = palette_from_image(self._cover) if self._cover else [(120, 130, 160)]
            return
        self._track_key = key
        self._art_key = art_key
        self._track_changed_at = time.monotonic()
        self._needs_frame = True

        cover = None
        if track.art:
            try:
                cover = Image.open(io.BytesIO(track.art)).convert("RGB")
            except Exception:
                cover = None
        self._cover = cover
        self._palette = (
            palette_from_image(cover) if cover else [(120, 130, 160)]
        )
        self._hires_key = None
        if cover is None or min(cover.size) < 400:
            self._covers.request(track.title, track.artist)
        self._lyrics.request(
            {"title": track.title, "artist": track.artist, "album": track.album, "app": track.app},
            timeline.duration_ms if timeline else 0,
        )

    def _backdrop_for(self, width: int, height: int) -> Image.Image:
        """The album cover blurred up into a room light, darkened so text and
        icons stay readable. Cached: the blur is far too expensive for a
        per-frame render."""
        cover = self._cover
        key = (width, height, id(cover))
        if self._backdrop is not None and self._backdrop_key == key:
            return self._backdrop
        if cover is None:
            return Image.new("RGBA", (width, height), (16, 17, 22, 255))
        scale = max(width, height) * 1.15 / min(cover.size)
        big = cover.resize(
            (int(cover.width * scale), int(cover.height * scale)), Image.LANCZOS
        )
        left = (big.width - width) // 2
        top = (big.height - height) // 2
        cropped = big.crop((left, top, left + width, top + height))
        blurred = cropped.filter(ImageFilter.GaussianBlur(width / 34.0))
        backdrop = Image.eval(blurred, lambda v: int(v * 0.42)).convert("RGBA")
        self._backdrop = backdrop
        self._backdrop_key = key
        return backdrop

    # -- rendering ----------------------------------------------------------
    def _stage_text_key(self, track, timeline) -> tuple | None:
        """What the stage's text currently shows: the lyric line plus the
        ghost fade quantized to 4 steps. Stage redraws follow content, not
        the render clock."""
        if not track or not timeline:
            return None
        position = timeline.position_now()
        lines = (self._lyrics.result() or {}).get("lines") or []
        upcoming = [(t, text) for t, text in lines if t > position]
        ghost = None
        if upcoming:
            lead = upcoming[0][0] - position
            if lead <= 1500:
                ghost = int((1 - lead / 1500) * 4)
        return (current_line(lines, position), ghost)

    def _render_stage(self, width, height, track, timeline, playing) -> Image.Image:
        """The static layer: room light from the album art, title/artist, and
        the line being sung. Redrawn only when one of those changes."""
        backdrop = self._backdrop_for(width, height)
        # Fade the room light in, and dim it further when nothing is playing.
        # The composite only changes when the playing state (or the backdrop
        # behind it) changes, so cache it — a full-screen alpha_composite is
        # the single most expensive op in this loop.
        light_key = (playing, self._backdrop_key)
        if self._base_frame is None or self._base_key != light_key:
            alpha = 235 if playing else 190
            base = Image.new("RGBA", (width, height), (0, 0, 0, 255 - alpha))
            self._base_frame = Image.alpha_composite(base, backdrop)
            self._base_key = light_key
        frame = self._base_frame.copy()

        position = timeline.position_now() if timeline else 0
        draw = ImageDraw.Draw(frame)
        self._draw_text(frame, width, height, track, playing)
        if track and timeline:
            lines = (self._lyrics.result() or {}).get("lines") or []
            line = current_line(lines, position)
            if line:
                self._draw_lyric(draw, frame, width, height, line, position, lines)
        return frame

    def _render_player(
        self, width, height, offset_x, offset_y, screen_width, screen_height,
        track, timeline, playing, audio_ok,
    ) -> Image.Image:
        """The animated region: spectrum ring, spinning vinyl and tonearm on
        transparency, so the static stage shows through around it. The ring's
        length budget comes from the full screen height; positions are local
        to this window."""
        frame = Image.new("RGBA", (width, height), (0, 0, 0, 0))

        if audio_ok and playing:
            samples = self._audio.read_latest(2048)
            # The tap may not have resolved the endpoint format yet; the
            # spectrum is decorative, so fall back to the common rate.
            rate = self._audio.format.sample_rate if self._audio.format else 48000
            target = spectrum_bars(samples, rate, 64)
        else:
            target = [0.0] * 64
        self._bars = smooth_bars(self._bars, target, attack=0.5, decay=0.12 if playing else 0.35)

        cx, cy, diameter = cover_rect(screen_width, screen_height)
        cx -= offset_x
        cy -= offset_y
        draw = ImageDraw.Draw(frame)
        self._draw_ring(draw, cx, cy, diameter, screen_width, screen_height, playing)
        self._draw_cover(frame, cx, cy, diameter, playing)
        return frame

    def _render(self, width, height, track, timeline, playing, audio_ok) -> Image.Image:
        """Single-image composition of stage + player. The live loop renders
        the two surfaces separately; kept for callers that want one frame."""
        frame = self._render_stage(width, height, track, timeline, playing)
        px, py, pw, ph = player_region(width, height)
        player = self._render_player(
            pw, ph, px, py, width, height, track, timeline, playing, audio_ok
        )
        frame.alpha_composite(player, (px, py))
        return frame

    def _draw_ring(self, draw: ImageDraw.Draw, cx, cy, diameter, width, height, playing) -> None:
        """Spectrum bars radiating from the cover — the widget's heartbeat."""
        count = len(self._bars)
        gap = max(6, diameter // 40)
        inner = diameter / 2 + gap
        max_len = height * 0.11
        for i, value in enumerate(self._bars):
            angle = 2 * math.pi * i / count - math.pi / 2
            length = inner + max(0.0, value) * max_len * (1.6 if playing else 0.6)
            if length <= inner + 1:
                continue
            color = self._palette[i % len(self._palette)]
            alpha = 150 + int(100 * value)
            # The bars are tapered wedges, drawn as thick lines: cheap and smooth.
            half_width = max(1.5, math.pi * inner / count * 0.42)
            for side in (-1, 1):
                offset = angle + side * half_width / inner
                x1 = cx + math.cos(offset) * inner
                y1 = cy + math.sin(offset) * inner
                x2 = cx + math.cos(offset) * length
                y2 = cy + math.sin(offset) * length
                draw.line(
                    (x1, y1, x2, y2),
                    fill=color + (alpha,),
                    width=max(2, int(half_width)),
                )

    def _draw_cover(self, frame: Image.Image, cx, cy, diameter, playing) -> None:
        """A spinning record with the album art as its label, a fixed sheen,
        and a tonearm that drops onto the grooves while playing. The disc is
        static (concentric grooves are rotationally symmetric) and only the
        album label rotates — that label is 16% of the disc's pixel area."""
        now = time.monotonic()
        if playing:
            self._spin += min(now - self._last_tick, 0.25)
        self._last_tick = now
        size = diameter
        key = (size, id(self._cover))
        if self._disc is None or self._disc_key != key:
            self._disc, self._label = build_vinyl(size, self._cover)
            self._sheen = render_sheen(size)
            self._disc_key = key
        if self._shadow is None or self._shadow_key != size:
            shadow = Image.new("RGBA", (size + 80, size + 80), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).ellipse(
                (40, 52, size + 40, size + 52), fill=(0, 0, 0, 150)
            )
            self._shadow = shadow.filter(ImageFilter.GaussianBlur(22))
            self._shadow_key = size
        half = size // 2
        top_left = (cx - half, cy - half)
        frame.alpha_composite(self._shadow, (cx - half - 40, cy - half - 40))
        if playing:
            frame.alpha_composite(self._disc, top_left)
        else:
            # Paused: the record sits in half light. Pre-dimmed once per
            # build instead of a per-frame pass over the whole disc.
            if self._disc_dim_key != key:
                self._disc_dim = Image.eval(self._disc, lambda v: int(v * 0.85))
                self._disc_dim_key = key
            frame.alpha_composite(self._disc_dim, top_left)
        if self._label is not None:
            rotated = self._label.rotate(
                vinyl_angle(self._spin), resample=Image.BILINEAR
            )
            lr = rotated.width // 2
            frame.alpha_composite(rotated, (cx - lr, cy - lr))
        frame.alpha_composite(self._sheen, top_left)
        self._draw_tonearm(frame, cx, cy, diameter, playing)

    def _draw_tonearm(self, frame: Image.Image, cx, cy, diameter, playing) -> None:
        geo = tonearm_points(cx, cy, diameter, playing)
        draw = ImageDraw.Draw(frame)
        (px, py), (hx, hy), pr = geo["pivot"], geo["head"], geo["radius"]
        arm_w = max(4, diameter // 70)
        draw.line((px + 3, py + 5, hx + 3, hy + 5), fill=(0, 0, 0, 110), width=arm_w + 2)
        draw.line((px, py, hx, hy), fill=(196, 198, 204, 255), width=arm_w)
        draw.line((px, py, hx, hy), fill=(240, 242, 246, 160), width=max(1, arm_w // 3))
        draw.ellipse((px - pr, py - pr, px + pr, py + pr), fill=(60, 62, 70, 255), outline=(170, 172, 180, 255), width=2)
        draw.ellipse((px - pr // 2, py - pr // 2, px + pr // 2, py + pr // 2), fill=(150, 152, 160, 255))
        head = max(8, diameter // 30)
        draw.rounded_rectangle((hx - head, hy - head // 2, hx + head, hy + head // 2), radius=3, fill=(40, 42, 48, 255))

    def _draw_text(self, frame: Image.Image, width, height, track, playing) -> None:
        if not track:
            return
        draw = ImageDraw.Draw(frame)
        cx, cy, diameter = cover_rect(width, height)
        self._font_title = self._font_title or _font(max(22, height // 34), bold=True)
        self._font_artist = self._font_artist or _font(max(16, height // 50))

        title = track.title or "Now playing"
        artist = track.artist
        base_y = cy + diameter // 2 + height // 24
        alpha = 255 if playing else 170

        def centered(text, y, font, fill):
            box = draw.textbbox((0, 0), text, font=font)
            draw.text((cx - (box[2] - box[0]) / 2, y), text, font=font, fill=fill)

        centered(title, base_y, font_for(title, self._font_title.size, True), (245, 245, 250, alpha))
        if artist:
            centered(artist, base_y + height // 28, font_for(artist, self._font_artist.size),
                     (215, 215, 225, alpha - 60))

    def _draw_lyric(self, draw: ImageDraw.Draw, frame, width, height, line, position, lines) -> None:
        """The line being sung now, near the bottom. The next line ghosts in
        beneath it just before it starts."""
        cx = width // 2
        y = int(height * 0.80)

        font = font_for(line, max(24, height // 28), bold=True)
        box = draw.textbbox((0, 0), line, font=font)
        if box[2] - box[0] > width * 0.86:
            font = font_for(line, max(18, height // 38), bold=True)
            box = draw.textbbox((0, 0), line, font=font)
        draw.text(
            (cx - (box[2] - box[0]) / 2, y), line, font=font,
            fill=(250, 250, 255, 235),
        )

        # Ghost of the next line, fading in over the last 1.5 s.
        upcoming = [(t, text) for t, text in lines if t > position]
        if upcoming:
            next_time, next_text = upcoming[0]
            lead = next_time - position
            if lead <= 1500:
                fade = int(120 * (1 - lead / 1500))
                if fade > 8:
                    font_next = font_for(next_text, max(16, height // 44))
                    nbox = draw.textbbox((0, 0), next_text, font=font_next)
                    draw.text(
                        (cx - (nbox[2] - nbox[0]) / 2, y + height // 22),
                        next_text, font=font_next, fill=(230, 230, 240, fade),
                    )


def _make_now_playing():
    """Kept for backwards compatibility of the module's public surface."""
    from .smtc import NowPlaying

    return NowPlaying()
