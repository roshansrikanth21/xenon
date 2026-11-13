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
    """A few bright, saturated colors to paint the bars with, so the widget
    inherits the album's own mood."""
    small = image.convert("RGB").resize((24, 24))
    pixels = list(small.getdata())
    scored = sorted(
        (
            (max(r, g, b) - min(r, g, b)) + 0.35 * (r + g + b) / 3,
            (r, g, b),
        )
        for r, g, b in pixels
    )
    palette: list[tuple[int, int, int]] = []
    for _, color in reversed(scored):
        if any(
            sum(abs(a - b) for a, b in zip(color, picked)) < 60
            for picked in palette
        ):
            continue
        palette.append(color)
        if len(palette) >= count:
            break
    if not palette:
        palette = [(120, 130, 160)]
    return palette


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
        """UpdateLayeredWindow one RGBA frame. The memory DC and DIB are
        cached per surface — recreating them every frame showed up as real
        CPU in the profile."""
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

        from .orb_render import premultiplied_bgra

        width, height = image.size
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
        ok = False
        if self._bits and self._gdi_dc:
            previous = gdi.SelectObject(self._gdi_dc, self._bitmap)
            ctypes.memmove(self._bits, premultiplied_bgra(image), width * height * 4)

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

    def _release_push_gdi(self) -> None:
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
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
        return hits


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
        self._ready.wait(timeout=6.0)
        if self._surface is None:
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
        try:
            while not self._stop.is_set():
                if self._scene(width, height, audio_ok) != "rebuild":
                    break
        finally:
            self._ready.set()
            if hotkey_ok:
                user.UnregisterHotKey(None, HOTKEY_ID)
            self._surface = None
            self._player_surface = None
            self._player_rect = None
            self._audio.stop()
            self._now_playing.stop()

    def _scene(self, width: int, height: int, audio_ok: bool) -> str:
        """Build the layers for the current layout and drive them until the
        widget stops ("stop") or the layout changes ("rebuild")."""
        from . import music_scene as scene

        settings = self._settings.current
        layout = scene.scene_layout(width, height, settings.side, settings.orb_size)
        boxes = {"lyrics": layout.lyric_box, "card": layout.card_box, "orb": layout.orb_box}
        surfaces: dict[str, _LayeredSurface] = {}
        for name, box in boxes.items():
            surface = _LayeredSurface(box.w, box.h, x=box.x, y=box.y)
            if not surface.create():
                for made in surfaces.values():
                    made.close()
                return "stop"
            surface.opacity = int(255 * settings.opacity)
            surfaces[name] = surface
        self._surfaces = surfaces
        self._surface = surfaces["lyrics"]
        self._player_surface = surfaces["orb"]
        self._player_rect = (layout.orb_box.x, layout.orb_box.y, layout.orb_box.w, layout.orb_box.h)
        for surface in surfaces.values():
            if self.overlay:
                self._float(surface)
            else:
                attach_behind_icons(surface.hwnd)
        self._ready.set()

        user = ctypes.windll.user32
        blank = {name: Image.new("RGBA", (box.w, box.h), (0, 0, 0, 0)) for name, box in boxes.items()}
        last_attach = time.monotonic()
        last_coverage = 0.0
        covered = False
        next_orb = next_lyric = next_card = 0.0
        lyric_key = card_key = object()
        orb_art = None
        orb_art_key = None
        last_tick = time.monotonic()
        orb_idle_done = False
        result = "stop"

        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if surfaces["lyrics"].pump() % 2:
                    self._toggle_overlay(*surfaces.values())
                    covered = False
                    lyric_key = card_key = object()
                    orb_idle_done = False

                if self._settings.poll(now):
                    fresh = self._settings.current
                    if fresh.layout_key() != settings.layout_key():
                        result = "rebuild"
                        break
                    settings = fresh
                    for surface in surfaces.values():
                        surface.opacity = int(255 * settings.opacity)
                    lyric_key = card_key = object()
                    orb_art_key = None
                    orb_idle_done = False
                    if not settings.pause_when_covered:
                        covered = False

                if not self.overlay and now - last_attach >= _REATTACH_SECONDS:
                    last_attach = now
                    from .wallpaper import get_parent
                    for surface in surfaces.values():
                        if not get_parent(surface.hwnd):
                            attach_behind_icons(surface.hwnd)

                if (settings.pause_when_covered and not self.overlay
                        and now - last_coverage >= _COVERAGE_CHECK_SECONDS):
                    last_coverage = now
                    covered = is_covered(surfaces["orb"].hwnd, (0, 0, width, height))

                track, timeline = self._now_playing.state()
                self._refresh_assets(track, timeline)
                playing = bool(timeline and timeline.playing)
                if covered:
                    time.sleep(0.25)
                    continue
                if self._needs_frame:
                    self._needs_frame = False
                    lyric_key = card_key = object()
                    orb_idle_done = False

                accent = settings.accent_rgb(scene.accent_from_palette(self._palette))
                position = timeline.position_now() if timeline else 0
                duration = timeline.duration_ms if timeline else 0

                # -- lyrics: only when what is lit actually changes ------------
                if now >= next_lyric:
                    frame = None
                    if track and settings.show_lyrics:
                        lines = (self._lyrics.result() or {}).get("lines") or []
                        frame = scene.lyric_frame(lines, position, duration) if lines else None
                    if frame != lyric_key:
                        lyric_key = frame
                        image = (scene.render_lyrics(layout.lyric_box, frame, accent, height, settings)
                                 if frame is not None else blank["lyrics"])
                        surfaces["lyrics"].push(image)
                        self._has_frame = True
                        next_lyric = now + 1.0 / 30
                    else:
                        next_lyric = now + 0.02

                # -- card: title, art, progress --------------------------------
                if now >= next_card:
                    next_card = now + 1.0
                    progress = (position / duration) if duration else 0.0
                    key = (self._track_key, playing, int(progress * 300), settings.show_card)
                    if key != card_key:
                        card_key = key
                        if track and settings.show_card:
                            image = scene.render_card(layout.card_box, track.title, track.artist,
                                                      self._cover, progress, playing, accent)
                        else:
                            image = blank["card"]
                        surfaces["card"].push(image)

                # -- orb: every frame while playing, settle when paused --------
                art_key = (id(self._cover), layout.orb_diameter, accent, settings.disc,
                           settings.halo, settings.backdrop)
                if orb_art_key != art_key:
                    orb_art = scene.OrbArt(layout.orb_diameter, self._cover, accent,
                                           settings.disc, settings.halo, settings.backdrop)
                    orb_art_key = art_key
                    orb_idle_done = False
                active = playing or max(map(abs, self._bars), default=0.0) >= 0.004
                if track and (active or not orb_idle_done):
                    if now < next_orb:
                        time.sleep(min(0.02, max(0.003, next_orb - now)))
                        continue
                    next_orb = now + 1.0 / settings.fps
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
                    surfaces["orb"].push(scene.render_orb(
                        layout.orb_box, layout, orb_art, self._bars, self._spin, playing, energy,
                        accent, height, settings,
                    ))
                    if not active:
                        orb_idle_done = True
                elif not track and not orb_idle_done:
                    surfaces["orb"].push(blank["orb"])
                    orb_idle_done = True
                else:
                    last_tick = now
                    time.sleep(0.03)
        finally:
            from .wallpaper import detach_from_desktop
            for surface in surfaces.values():
                try:
                    detach_from_desktop(surface.hwnd)
                    surface.close()
                except Exception:
                    pass
            self._surfaces = {}
        del user
        return result

    def _float(self, surface: "_LayeredSurface") -> None:
        from .wallpaper import WS_EX_TRANSPARENT, detach_from_desktop, send_to_top_most, show
        user = ctypes.windll.user32
        detach_from_desktop(surface.hwnd)
        style = user.GetWindowLongPtrW(surface.hwnd, GWL_EXSTYLE)
        user.SetWindowLongPtrW(surface.hwnd, GWL_EXSTYLE, style | WS_EX_TRANSPARENT)
        send_to_top_most(surface.hwnd)
        show(surface.hwnd)

    def _toggle_overlay(self, *surfaces) -> None:
        """Wallpaper <-> floating overlay for every layer. The overlay is
        click-through and never takes focus, so it can stay up while you work."""
        from .wallpaper import clear_top_most
        self.overlay = not self.overlay
        self._needs_frame = True
        for surface in surfaces:
            if self.overlay:
                self._float(surface)
            else:
                clear_top_most(surface.hwnd)
                attach_behind_icons(surface.hwnd)

    def _refresh_assets(self, track, timeline) -> None:
        if not track:
            if self._track_key is not None:
                self._track_key = None
                self._needs_frame = True
                self._lyrics.request(None)
            return
        key = (track.title, track.artist, track.album)
        if key == self._track_key:
            return
        self._track_key = key
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
