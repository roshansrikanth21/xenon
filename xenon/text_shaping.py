"""Correctly shaped text for scripts Pillow cannot lay out on its own.

Pillow places one glyph per character unless it was built with libraqm,
and this install was not (``PIL.features.check("raqm")`` is False). That
is fine for Latin, but Malayalam, Tamil, Hindi, Thai or Arabic need
shaping: a Malayalam vowel sign drawn before its consonant, conjuncts
merged into one glyph, Arabic letters joined and run right to left.
Without it the lyrics come out as broken letters.

Windows shapes those scripts itself whenever GDI draws text (its Uniscribe
layer), so ``ShapedFont`` keeps Pillow's font object for everything else
and, for text that needs shaping, measures and draws it through GDI into
a greyscale mask that Pillow then paints like any other glyph mask. Latin
text never goes near GDI.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

from PIL import Image, ImageFont

# Unicode blocks that need shaping, and the Windows font (by face name)
# that covers them.
COMPLEX = (
    ((0x0590, 0x05FF), "Segoe UI"),        # Hebrew
    ((0x0600, 0x06FF), "Segoe UI"),        # Arabic
    ((0x0750, 0x077F), "Segoe UI"),        # Arabic supplement
    ((0x0900, 0x0DFF), "Nirmala UI"),      # Devanagari .. Sinhala (Hindi, Tamil, Malayalam, ...)
    ((0x0E00, 0x0E7F), "Leelawadee UI"),   # Thai
)


def complex_face(text: str) -> str | None:
    """The Windows face to shape ``text`` with, or None when Pillow's own
    layout is right (Latin, CJK, kana, Hangul...)."""
    for ch in text or "":
        code = ord(ch)
        if code < 0x0590:
            continue
        for (lo, hi), face in COMPLEX:
            if lo <= code <= hi:
                return face
    return None


def available() -> bool:
    return sys.platform == "win32"


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _TEXTMETRICW(ctypes.Structure):
    _fields_ = [("tmHeight", wintypes.LONG), ("tmAscent", wintypes.LONG), ("tmDescent", wintypes.LONG),
                ("tmInternalLeading", wintypes.LONG), ("tmExternalLeading", wintypes.LONG),
                ("tmAveCharWidth", wintypes.LONG), ("tmMaxCharWidth", wintypes.LONG),
                ("tmWeight", wintypes.LONG), ("tmOverhang", wintypes.LONG),
                ("tmDigitizedAspectX", wintypes.LONG), ("tmDigitizedAspectY", wintypes.LONG),
                ("tmFirstChar", wintypes.WCHAR), ("tmLastChar", wintypes.WCHAR),
                ("tmDefaultChar", wintypes.WCHAR), ("tmBreakChar", wintypes.WCHAR),
                ("tmItalic", wintypes.BYTE), ("tmUnderlined", wintypes.BYTE),
                ("tmStruckOut", wintypes.BYTE), ("tmPitchAndFamily", wintypes.BYTE),
                ("tmCharSet", wintypes.BYTE)]


_DT_FLAGS = 0x0020 | 0x0800 | 0x0100          # SINGLELINE | NOPREFIX | NOCLIP
_DT_CALCRECT = 0x0400
_ANTIALIASED_QUALITY = 4
_gdi_lock = threading.Lock()
_fonts: dict = {}
_masks: dict = {}


def _api():
    gdi, user = ctypes.windll.gdi32, ctypes.windll.user32
    gdi.CreateFontW.restype = wintypes.HFONT
    gdi.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.SelectObject.restype = wintypes.HGDIOBJ
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    gdi.CreateDIBSection.restype = wintypes.HBITMAP
    gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                     ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
    gdi.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi.GetTextMetricsW.argtypes = [wintypes.HDC, ctypes.POINTER(_TEXTMETRICW)]
    user.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(_RECT), wintypes.UINT]
    return gdi, user


def _hfont(face: str, size: int, weight: int):
    key = (face, size, weight)
    handle = _fonts.get(key)
    if handle is None:
        gdi, _ = _api()
        # Negative height: the em size in pixels, as FreeType's ``size``.
        handle = gdi.CreateFontW(-size, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, _ANTIALIASED_QUALITY, 0, face)
        _fonts[key] = handle
    return handle


def shaped(text: str, face: str, size: int, weight: int):
    """(mask "L", advance, ascent): ``text`` shaped and drawn by Windows,
    white on black, the mask's top at the font's ascent line."""
    key = (text, face, size, weight)
    hit = _masks.get(key)
    if hit is not None:
        return hit
    import numpy as np

    gdi, user = _api()
    with _gdi_lock:
        dc = gdi.CreateCompatibleDC(None)
        try:
            gdi.SelectObject(dc, _hfont(face, size, weight))
            tm = _TEXTMETRICW()
            gdi.GetTextMetricsW(dc, ctypes.byref(tm))
            rect = _RECT(0, 0, 0, 0)
            user.DrawTextW(dc, text, -1, ctypes.byref(rect), _DT_FLAGS | _DT_CALCRECT)
            advance = rect.right - rect.left
            # Room either side: shaped marks can hang past the advance.
            margin = max(2, size // 3)
            w, h = max(1, advance + margin * 2), max(1, tm.tmHeight + margin * 2)
            info = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            bits = ctypes.c_void_p()
            bmp = gdi.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
            try:
                gdi.SelectObject(dc, bmp)
                gdi.SetBkMode(dc, 1)                 # TRANSPARENT
                gdi.SetTextColor(dc, 0x00FFFFFF)
                box = _RECT(margin, margin, margin + advance + margin, margin + tm.tmHeight)
                user.DrawTextW(dc, text, -1, ctypes.byref(box), _DT_FLAGS)
                gdi.GdiFlush()
                raw = np.ctypeslib.as_array((ctypes.c_uint8 * (w * h * 4)).from_address(bits.value))
                grey = raw.reshape(h, w, 4)[:, :, 1].copy()
            finally:
                gdi.DeleteObject(bmp)
        finally:
            gdi.DeleteDC(dc)
    mask = Image.fromarray(grey, "L")
    result = (mask, advance, tm.tmAscent, margin)
    if len(_masks) > 256:
        _masks.clear()
    _masks[key] = result
    return result


class ShapedFont(ImageFont.FreeTypeFont):
    """A Pillow font that hands text needing shaping to Windows (see the
    module notes) and everything else to FreeType as usual."""

    def __init__(self, *args, face: str = "Nirmala UI", gdi_weight: int = 700, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.gdi_face = face
        self.gdi_weight = gdi_weight

    def _shapes(self, text) -> bool:
        return isinstance(text, str) and available() and complex_face(text) is not None

    def _shaped(self, text):
        mask, advance, ascent, margin = shaped(text, complex_face(text) or self.gdi_face, int(self.size),
                                               self.gdi_weight)
        # Pillow's default anchor ("la") puts the ascender at y; GDI's cell
        # top is the ascent above the baseline: line the baselines up.
        fa = self.getmetrics()[0]
        return mask, advance, (-margin, fa - ascent - margin)

    def getlength(self, text, mode="", direction=None, features=None, language=None):
        if self._shapes(text):
            return float(self._shaped(text)[1])
        return super().getlength(text, mode, direction, features, language)

    def getbbox(self, text, mode="", direction=None, features=None, language=None, stroke_width=0,
                anchor=None):
        if self._shapes(text):
            mask, _adv, (ox, oy) = self._shaped(text)
            box = mask.getbbox()
            if not box:
                return (0, 0, 0, 0)
            return (box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy)
        return super().getbbox(text, mode, direction, features, language, stroke_width, anchor)

    def getmask2(self, text, mode="", direction=None, features=None, language=None, stroke_width=0,
                 anchor=None, ink=0, start=None, *args, **kwargs):
        if self._shapes(text):
            mask, _adv, offset = self._shaped(text)
            if mode == "1":
                mask = mask.point(lambda v: 255 if v >= 128 else 0).convert("1")
            return mask.im, offset
        return super().getmask2(text, mode, direction, features, language, stroke_width, anchor, ink,
                                start, *args, **kwargs)


def truetype(path: str, size: int, index: int = 0, bold: bool = True):
    """A ``ShapedFont`` for a script font file (Pillow's for the rest)."""
    import os

    if not os.path.isabs(path) and not os.path.exists(path):
        # ImageFont.truetype looks in the system font folder; the class
        # itself does not.
        path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", path)
    return ShapedFont(path, size, index=index, gdi_weight=900 if bold else 400)
