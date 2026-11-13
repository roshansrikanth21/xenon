"""The look of the desktop music interface, as pure render functions.

Three transparent layers over the user's own wallpaper:

* lyrics  — the line being sung, huge, heavy and uppercase on the left,
            revealed word by word with a soft glow; the next line waits dim.
* orb     — the album art as a slowly turning glowing disc on the right,
            ringed by white spectrum bars and a halo of light.
* card    — a small now-playing card (art, title, artist, progress).

Everything here is PIL in, PIL out, so it can be tested without a window.
The widget (``music_widget``) owns timing, audio and the layered windows.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# ------------------------------------------------------------------ layout


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class SceneLayout:
    orb_cx: int
    orb_cy: int
    orb_diameter: int
    orb_box: Box       # window region of the orb layer (screen coords)
    lyric_box: Box     # window region of the lyric layer
    card_box: Box      # window region of the now-playing card


def scene_layout(width: int, height: int) -> SceneLayout:
    """Lyrics own the left ~55%, the orb sits right of centre, the card in
    the lower left. Proportions follow the reference, scaled to the screen."""
    diameter = int(min(height * 0.36, width * 0.24))
    cx = int(width * 0.70)
    cy = int(height * 0.50)
    reach = int(diameter / 2 + diameter * 0.06 + height * 0.17)
    half = reach + 24
    ox, oy = max(0, cx - half), max(0, cy - half)
    orb_box = Box(ox, oy, min(half * 2, width - ox), min(half * 2, height - oy))

    lx = int(width * 0.05)
    lyric_box = Box(lx, int(height * 0.16), int(width * 0.50), int(height * 0.50))

    cw, ch = int(min(width * 0.20, height * 0.42)), int(height * 0.085)
    card_box = Box(lx, int(height * 0.80), max(cw, 260), max(ch, 72))
    return SceneLayout(cx, cy, diameter, orb_box, lyric_box, card_box)


# ------------------------------------------------------------------ fonts

_FONT_FILES = {
    "black": ("seguibl.ttf", "segoeuib.ttf", "arialbd.ttf"),
    "bold": ("segoeuib.ttf", "arialbd.ttf"),
    "regular": ("segoeui.ttf", "arial.ttf"),
}
_font_cache: dict = {}


def _script_face(text: str, weight: str) -> tuple[str, int] | None:
    try:
        from .music_widget import script_font_file
    except Exception:  # pragma: no cover - import cycle guard
        return None
    return script_font_file(text, weight != "regular")


def font(text: str, size: int, weight: str = "black") -> ImageFont.FreeTypeFont:
    """Segoe UI Black for Latin; a script font (Nirmala UI, Yu Gothic, ...)
    when the text needs glyphs Segoe does not have."""
    face = _script_face(text, weight)
    key = (face, size, weight)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    result = None
    if face is not None:
        try:
            result = ImageFont.truetype(face[0], size, index=face[1])
        except OSError:
            result = None
    if result is None:
        for name in _FONT_FILES[weight]:
            try:
                result = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
    if result is None:
        result = ImageFont.load_default()
    _font_cache[key] = result
    return result


# ------------------------------------------------------------------ lyrics


def word_starts(line_start: int, line_end: int, words: list[str]) -> list[int]:
    """When each word of a line starts, in ms. LRCLIB gives line timestamps
    only, so the line's span is shared out by word length (longer words take
    longer to sing), leaving a short tail so the last word lands early."""
    if not words:
        return []
    span = max(400, min(line_end - line_start, 8000))
    sing = span * 0.82
    weights = [max(1, len(w)) + 1.5 for w in words]
    total = sum(weights)
    starts, t = [], float(line_start)
    for weight in weights:
        starts.append(int(t))
        t += sing * weight / total
    return starts


def line_at(lines: list[tuple[int, str]], position_ms: int) -> int:
    """Index of the line being sung (the last one started), or -1."""
    index = -1
    for i, (start, _text) in enumerate(lines):
        if start <= position_ms:
            index = i
        else:
            break
    return index


@dataclass(frozen=True)
class LyricFrame:
    """Everything the lyric layer shows at one instant. Quantised so equal
    frames compare equal and the widget only redraws on visible change."""
    text: str
    reveal: tuple[int, ...]   # per word: 0 (unsung) .. STEPS (fully lit)
    entry: int                # line entrance 0..STEPS
    upcoming: str


REVEAL_STEPS = 5
WORD_FADE_MS = 220
LINE_ENTRY_MS = 320


def lyric_frame(lines: list[tuple[int, str]], position_ms: int, duration_ms: int = 0) -> LyricFrame | None:
    index = line_at(lines, position_ms)
    if index < 0:
        if lines and lines[0][0] - position_ms < 2500:
            return LyricFrame("", (), 0, lines[0][1].strip())
        return None
    start, text = lines[index]
    text = text.strip()
    end = lines[index + 1][0] if index + 1 < len(lines) else (duration_ms or start + 5000)
    upcoming = lines[index + 1][1].strip() if index + 1 < len(lines) else ""
    if not text:
        return LyricFrame("", (), REVEAL_STEPS, upcoming)
    words = text.split()
    reveal = []
    for word_start in word_starts(start, end, words):
        progress = (position_ms - word_start) / WORD_FADE_MS
        reveal.append(max(0, min(REVEAL_STEPS, int(progress * REVEAL_STEPS + 0.999))))
    entry = max(0, min(REVEAL_STEPS, int((position_ms - start) / LINE_ENTRY_MS * REVEAL_STEPS + 0.999)))
    return LyricFrame(text, tuple(reveal), entry, upcoming)


def _wrap(words: list[str], fnt, max_width: int, draw) -> list[list[int]]:
    """Greedy wrap; returns lines as lists of word indices."""
    lines: list[list[int]] = []
    current: list[int] = []
    space = draw.textlength(" ", font=fnt)
    width = 0.0
    for i, word in enumerate(words):
        w = draw.textlength(word, font=fnt)
        if current and width + space + w > max_width:
            lines.append(current)
            current, width = [], 0.0
        width = w if not current else width + space + w
        current.append(i)
    if current:
        lines.append(current)
    return lines


def render_lyrics(box: Box, frame: LyricFrame | None, accent=(255, 255, 255), screen_height: int = 1440) -> Image.Image:
    """The lyric layer: heavy uppercase words, sung ones bright with a glow,
    unsung ones faint, the next line small and dim beneath."""
    image = Image.new("RGBA", (box.w, box.h), (0, 0, 0, 0))
    if frame is None:
        return image
    text = frame.text.upper() if frame.text.isascii() else frame.text
    words = text.split()
    draw = ImageDraw.Draw(image)

    size = int(screen_height / 8.5)
    fnt = font(text, size)
    rows = _wrap(words, fnt, int(box.w * 0.96), draw) if words else []
    while len(rows) > 3 and size > 48:
        size = int(size * 0.86)
        fnt = font(text, size)
        rows = _wrap(words, fnt, int(box.w * 0.96), draw)

    line_h = int(size * 1.02)
    block_h = line_h * len(rows)
    # Rise into place as the line enters.
    rise = int((1 - frame.entry / REVEAL_STEPS) * size * 0.35)
    top = max(0, int(box.h * 0.46 - block_h / 2)) + rise

    glyphs = Image.new("RGBA", image.size, (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glyphs)
    lit = Image.new("L", image.size, 0)
    ldraw = ImageDraw.Draw(lit)
    space = draw.textlength(" ", font=fnt)
    entry_alpha = frame.entry / REVEAL_STEPS
    for r, row in enumerate(rows):
        x = 0.0
        y = top + r * line_h
        for i in row:
            word = words[i]
            level = frame.reveal[i] / REVEAL_STEPS if i < len(frame.reveal) else 0.0
            alpha = int((46 + (255 - 46) * level) * entry_alpha)
            gdraw.text((x, y), word, font=fnt, fill=(255, 255, 255, alpha))
            if level > 0:
                ldraw.text((x, y), word, font=fnt, fill=int(255 * level * entry_alpha))
            x += draw.textlength(word, font=fnt) + space

    # Glow from the sung words only, blurred at quarter size (16x cheaper).
    small = lit.resize((max(1, lit.width // 4), max(1, lit.height // 4)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(max(2, size / 22)))
    glow_mask = small.resize(lit.size, Image.BILINEAR).point(lambda v: int(v * 0.75))
    glow = Image.new("RGBA", image.size, accent + (0,))
    glow.putalpha(glow_mask)
    # A soft dark shadow under every word keeps white text readable on a
    # bright wallpaper; it shares the quarter-size blur trick.
    shade = glyphs.getchannel("A")
    shade = shade.resize((max(1, shade.width // 4), max(1, shade.height // 4)), Image.BILINEAR)
    shade = shade.filter(ImageFilter.GaussianBlur(max(2, size / 30)))
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow.putalpha(shade.resize(image.size, Image.BILINEAR).point(lambda v: int(v * 0.55)))
    image = Image.alpha_composite(image, shadow)
    image = Image.alpha_composite(image, glow)
    image = Image.alpha_composite(image, glyphs)

    if frame.upcoming:
        up = frame.upcoming.upper() if frame.upcoming.isascii() else frame.upcoming
        ufnt = font(up, max(22, size // 3), "bold")
        uy = min(box.h - ufnt.size - 6, top - rise + block_h + int(size * 0.28))
        ImageDraw.Draw(image).text((2, uy), up, font=ufnt, fill=(255, 255, 255, 90))
    return image


# ------------------------------------------------------------------ orb

BAR_COUNT = 120


def ring_values(bars: list[float], count: int = BAR_COUNT) -> list[float]:
    """Spread the analyser's bands round the circle, mirrored left/right so
    the bass sits at the bottom and the ring looks balanced."""
    if not bars:
        return [0.0] * count
    half = count // 2
    out = []
    n = len(bars)
    for i in range(half):
        f = i / max(1, half - 1) * (n - 1)
        lo = int(f)
        hi = min(n - 1, lo + 1)
        v = bars[lo] + (bars[hi] - bars[lo]) * (f - lo)
        out.append(max(0.0, min(1.2, v)))
    # bottom -> top on the right, mirrored on the left
    return out + out[::-1]


class OrbArt:
    """Cached static parts of the orb for one cover and size."""

    def __init__(self, diameter: int, cover: Image.Image | None, accent=(255, 255, 255)) -> None:
        self.diameter = diameter
        d = diameter
        if cover is not None:
            art = cover.convert("RGB").resize((d, d), Image.LANCZOS)
        else:
            art = Image.new("RGB", (d, d), (40, 42, 52))
        # Sphere shading: brighter centre, darker limb, so flat art reads
        # as a lit globe like the reference.
        shade = Image.radial_gradient("L").resize((d, d))
        shade = shade.point(lambda v: int(255 - v * 0.55))
        art = ImageChops.multiply(art, Image.merge("RGB", (shade, shade, shade)))
        mask = Image.new("L", (d * 2, d * 2), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, d * 2 - 1, d * 2 - 1), fill=255)
        self.mask = mask.resize((d, d), Image.LANCZOS)
        self.art = art.convert("RGBA")
        self.art.putalpha(self.mask)
        # Halo: a big soft light behind the disc.
        size = int(d * 2.1)
        halo = Image.new("L", (size, size), 0)
        hd = ImageDraw.Draw(halo)
        for k in range(14, 0, -1):
            r = d / 2 * (1 + 0.075 * k)
            c = size / 2
            hd.ellipse((c - r, c - r, c + r, c + r), fill=int(150 * (1 - k / 15) ** 2))
        halo = halo.filter(ImageFilter.GaussianBlur(d / 14))
        self.halo = Image.new("RGBA", (size, size), accent + (0,))
        self.halo.putalpha(halo)
        # Rim light.
        rim = Image.new("RGBA", (d, d), (0, 0, 0, 0))
        ImageDraw.Draw(rim).ellipse((1, 1, d - 2, d - 2), outline=accent + (110,), width=max(2, d // 160))
        self.rim = rim.filter(ImageFilter.GaussianBlur(1.2))
        dim = self.art.copy()
        dim = Image.eval(dim.convert("RGB"), lambda v: int(v * 0.8)).convert("RGBA")
        dim.putalpha(self.mask)
        self.dim = dim


def render_orb(box: Box, layout: SceneLayout, art: OrbArt, bars: list[float], spin_deg: float,
               playing: bool, energy: float = 0.0, accent=(255, 255, 255), screen_height: int = 1440) -> Image.Image:
    cx, cy = layout.orb_cx - box.x, layout.orb_cy - box.y
    d = art.diameter

    # The halo never changes for a track: composite it once per box and
    # start every frame from a copy.
    base_key = (box.w, box.h, cx, cy)
    if getattr(art, "_base_key", None) != base_key:
        base = Image.new("RGBA", (box.w, box.h), (0, 0, 0, 0))
        base.alpha_composite(art.halo, (int(cx - art.halo.width / 2), int(cy - art.halo.height / 2)))
        art._base = base
        art._base_key = base_key
    image = art._base.copy()

    values = ring_values(bars)
    inner = d / 2 + d * 0.05
    reach = screen_height * 0.15 * (1.0 if playing else 0.35) * (1.0 + 0.15 * min(1.0, energy))
    count = len(values)
    bar_w = max(2, int(math.pi * d / count * 0.30))
    rays = Image.new("L", image.size, 0)
    rdraw = ImageDraw.Draw(rays)
    for i, v in enumerate(values):
        angle = math.pi / 2 + 2 * math.pi * i / count   # start at the bottom
        length = inner + d * 0.012 + max(0.0, v) * reach
        ca, sa = math.cos(angle), math.sin(angle)
        rdraw.line((cx + ca * inner, cy + sa * inner, cx + ca * length, cy + sa * length),
                   fill=int(150 + 105 * min(1.0, v)), width=bar_w)
    # Light bleed around the rays, blurred at quarter size, merged with the
    # crisp rays into one alpha so the whole ring is a single composite.
    q = rays.resize((max(1, rays.width // 4), max(1, rays.height // 4)), Image.BILINEAR)
    q = q.filter(ImageFilter.GaussianBlur(max(2, d / 90)))
    bleed = q.resize(rays.size, Image.BILINEAR).point(lambda v: min(255, int(v * 1.6)))
    alpha = ImageChops.lighter(bleed, rays)
    ring = Image.new("RGBA", image.size, accent + (0,))
    ring.putalpha(alpha)
    image.alpha_composite(ring)

    disc = art.art.rotate(spin_deg, resample=Image.BILINEAR) if spin_deg else art.art
    if not playing:
        disc = art.dim if hasattr(art, "dim") else disc
    image.alpha_composite(disc, (int(cx - d / 2), int(cy - d / 2)))
    image.alpha_composite(art.rim, (int(cx - d / 2), int(cy - d / 2)))
    return image


# ------------------------------------------------------------------ card


def render_card(box: Box, title: str, artist: str, cover: Image.Image | None,
                progress: float, playing: bool, accent=(255, 255, 255)) -> Image.Image:
    w, h = box.w, box.h
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = max(10, h // 5)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=(12, 12, 16, 150),
                           outline=(255, 255, 255, 34), width=1)
    pad = max(8, h // 8)
    art = h - pad * 2
    if cover is not None:
        thumb = cover.convert("RGB").resize((art, art), Image.LANCZOS)
        m = Image.new("L", (art, art), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, art - 1, art - 1), radius=max(6, art // 6), fill=255)
        image.paste(thumb, (pad, pad), m)
    tx = pad * 2 + art
    tmax = w - tx - pad
    tf = font(title, max(14, h // 4), "bold")
    af = font(artist, max(12, h // 5), "regular")

    def fit(text, fnt):
        if draw.textlength(text, font=fnt) <= tmax:
            return text
        while text and draw.textlength(text + "…", font=fnt) > tmax:
            text = text[:-1]
        return text + "…"

    draw.text((tx, pad), fit(title or "Now playing", tf), font=tf, fill=(250, 250, 255, 240))
    if artist:
        draw.text((tx, pad + tf.size + 4), fit(artist, af), font=af, fill=(220, 220, 230, 170))
    by = h - pad - 4
    draw.rounded_rectangle((tx, by, tx + tmax, by + 3), radius=2, fill=(255, 255, 255, 50))
    filled = int(tmax * max(0.0, min(1.0, progress)))
    if filled > 0:
        draw.rounded_rectangle((tx, by, tx + filled, by + 3), radius=2, fill=accent + (220,))
    if not playing:
        px = w - pad - 12
        draw.rectangle((px, pad, px + 3, pad + 12), fill=(255, 255, 255, 180))
        draw.rectangle((px + 7, pad, px + 10, pad + 12), fill=(255, 255, 255, 180))
    return image


def accent_from_palette(palette: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    """A light, slightly tinted glow colour from the art (pure white when
    the art is grey), so the scene picks up the album's mood."""
    if not palette:
        return (255, 255, 255)
    r, g, b = palette[0]
    return (int(r * 0.35 + 255 * 0.65), int(g * 0.35 + 255 * 0.65), int(b * 0.35 + 255 * 0.65))
