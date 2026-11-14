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


def scene_layout(width: int, height: int, side: str = "right", orb_size: float = 1.0) -> SceneLayout:
    """Lyrics own one side (~55%), the orb sits on the other, the card in
    the lower corner under the lyrics. Proportions follow the reference,
    scaled to the screen; ``side`` says where the orb goes."""
    diameter = int(min(height * 0.36, width * 0.24) * orb_size)
    right = side != "left"
    cx = int(width * (0.70 if right else 0.27))
    cy = int(height * 0.50)
    reach = int(diameter / 2 + diameter * 0.06 + height * 0.17 * max(1.0, orb_size))
    half = max(reach + 24, int(diameter * 1.15))
    ox, oy = max(0, cx - half), max(0, cy - half)
    orb_box = Box(ox, oy, min(half * 2, width - ox), min(half * 2, height - oy))

    lw = int(width * (0.50 if right else 0.46))
    lx = int(width * 0.05) if right else int(width * 0.96) - lw
    lyric_box = Box(lx, int(height * 0.16), lw, int(height * 0.50))

    cw, ch = int(min(width * 0.20, height * 0.42)), int(height * 0.085)
    cw, ch = max(cw, 260), max(ch, 72)
    cx_card = lx if right else int(width * 0.95) - cw
    card_box = Box(cx_card, int(height * 0.80), cw, ch)
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
    previous: str = ""


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
    previous = lines[index - 1][1].strip() if index > 0 else ""
    if not text:
        return LyricFrame("", (), REVEAL_STEPS, upcoming, previous)
    words = text.split()
    reveal = []
    for word_start in word_starts(start, end, words):
        progress = (position_ms - word_start) / WORD_FADE_MS
        reveal.append(max(0, min(REVEAL_STEPS, int(progress * REVEAL_STEPS + 0.999))))
    entry = max(0, min(REVEAL_STEPS, int((position_ms - start) / LINE_ENTRY_MS * REVEAL_STEPS + 0.999)))
    return LyricFrame(text, tuple(reveal), entry, upcoming, previous)


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


def _settings(settings):
    if settings is None:
        from .music_settings import MusicSettings
        return MusicSettings()
    return settings


def _case(text: str, settings) -> str:
    return text.upper() if (settings.lyric_case == "upper" and text.isascii()) else text


def _blur_quarter(mask: Image.Image, radius: float, gain: float) -> Image.Image:
    """Gaussian blur at quarter size (16x cheaper), scaled back up."""
    small = mask.resize((max(1, mask.width // 4), max(1, mask.height // 4)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(max(2, radius)))
    return small.resize(mask.size, Image.BILINEAR).point(lambda v: min(255, int(v * gain)))


# Short function words a poster sets small ("I want THE world IN MY hands").
_SMALL_WORDS = frozenset("i a an the is in my of to on at it am and or so be by as for me we".split())


def _small_word(word: str) -> bool:
    return word.strip(".,!?;:'\"()").lower() in _SMALL_WORDS


def _row_width(words, fonts, row, draw) -> float:
    if not row:
        return 0.0
    width = sum(draw.textlength(words[i], font=fonts[i]) for i in row)
    width += sum(draw.textlength(" ", font=fonts[i]) for i in row[:-1])
    return width


def _wrap_sized(words: list[str], fonts: list, max_width: int, draw) -> list[list[int]]:
    """Greedy wrap where every word may have its own font."""
    lines: list[list[int]] = []
    current: list[int] = []
    width = 0.0
    for i, word in enumerate(words):
        w = draw.textlength(word, font=fonts[i])
        gap = draw.textlength(" ", font=fonts[current[-1]]) if current else 0.0
        if current and width + gap + w > max_width:
            lines.append(current)
            current, width, gap = [], 0.0, 0.0
        width += gap + w
        current.append(i)
    if current:
        lines.append(current)
    return lines


def render_lyrics(box: Box, frame: LyricFrame | None, accent=(255, 255, 255), screen_height: int = 1440,
                  settings=None) -> Image.Image:
    """The lyric layer: heavy words, sung ones bright with a glow, unsung
    ones faint, the next line small and dim beneath. ``settings`` picks the
    style (reveal / karaoke / stack / fade), size, case, alignment, glow."""
    st = _settings(settings)
    image = Image.new("RGBA", (box.w, box.h), (0, 0, 0, 0))
    if frame is None:
        return image
    text = _case(frame.text, st)
    words = text.split()
    draw = ImageDraw.Draw(image)
    centered = st.lyric_align == "center"
    style = st.lyric_style

    size = int(screen_height / 8.5 * st.lyric_size)
    max_w = int(box.w * 0.96)
    poster = style == "poster"

    def fonts_at(size_):
        big = font(text, size_)
        small = font(text, max(14, int(size_ * 0.46))) if poster else big
        return big, small, [small if poster and _small_word(w) else big for w in words]

    fnt, small_fnt, word_fonts = fonts_at(size)
    rows = _wrap_sized(words, word_fonts, max_w, draw) if words else []
    while len(rows) > 3 and size > 40:
        size = int(size * 0.86)
        fnt, small_fnt, word_fonts = fonts_at(size)
        rows = _wrap_sized(words, word_fonts, max_w, draw)
    # Small words sit at the cap line of the big ones, like a poster.
    try:
        small_dy = fnt.getbbox("H")[1] - small_fnt.getbbox("H")[1]
    except Exception:
        small_dy = 0

    line_h = int(size * 1.02)
    block_h = line_h * len(rows)
    rise = 0 if style in ("karaoke",) else int((1 - frame.entry / REVEAL_STEPS) * size * 0.35)
    top = max(0, int(box.h * 0.46 - block_h / 2)) + rise
    unsung = int(255 * st.unsung_opacity)
    entry_alpha = frame.entry / REVEAL_STEPS

    glyphs = Image.new("RGBA", image.size, (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glyphs)
    lit = Image.new("L", image.size, 0)
    ldraw = ImageDraw.Draw(lit)
    space = draw.textlength(" ", font=fnt)

    if style == "stack" and frame.previous:
        prev = _case(frame.previous, st)
        pf = font(prev, max(20, int(size * 0.42)), "black")
        pw = draw.textlength(prev, font=pf)
        px = (box.w - pw) / 2 if centered else 0
        py = max(0, top - rise - int(pf.size * 1.5))
        gdraw.text((px, py), prev, font=pf, fill=(255, 255, 255, int(110 * entry_alpha + 40)))

    for r, row in enumerate(rows):
        row_w = _row_width(words, word_fonts, row, draw)
        x = (box.w - row_w) / 2 if centered else 0.0
        y = top + r * line_h
        for i in row:
            word = words[i]
            wf = word_fonts[i]
            wy = y + (small_dy if wf is not fnt else 0)
            level = frame.reveal[i] / REVEAL_STEPS if i < len(frame.reveal) else 0.0
            if style == "fade":
                level = 1.0
            if style == "karaoke":
                # Sung words take the accent colour at full strength; the
                # line itself is always fully there.
                alpha = int(unsung + (255 - unsung) * level) if level else max(unsung, 120)
                colour = tuple(int(255 + (c - 255) * level) for c in accent) if level else (255, 255, 255)
                gdraw.text((x, wy), word, font=wf, fill=colour + (alpha,))
            else:
                alpha = int((unsung + (255 - unsung) * level) * entry_alpha)
                gdraw.text((x, wy), word, font=wf, fill=(255, 255, 255, alpha))
            if level > 0:
                ldraw.text((x, wy), word, font=wf, fill=int(255 * level * (1 if style == "karaoke" else entry_alpha)))
            x += draw.textlength(word, font=wf) + draw.textlength(" ", font=wf)

    # A soft dark shadow under every word keeps white text readable on a
    # bright wallpaper; then the glow from the sung words, then the words.
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow.putalpha(_blur_quarter(glyphs.getchannel("A"), size / 30, 0.55))
    image = Image.alpha_composite(image, shadow)
    if st.glow > 0:
        glow = Image.new("RGBA", image.size, accent + (0,))
        glow.putalpha(_blur_quarter(lit, size / 22, 0.75 * st.glow))
        image = Image.alpha_composite(image, glow)
    image = Image.alpha_composite(image, glyphs)

    if frame.upcoming and st.show_upcoming:
        up = _case(frame.upcoming, st)
        ufnt = font(up, max(22, size // 3), "bold")
        uy = min(box.h - ufnt.size - 6, top - rise + block_h + int(size * 0.28))
        ux = (box.w - draw.textlength(up, font=ufnt)) / 2 if centered else 2
        ImageDraw.Draw(image).text((ux, uy), up, font=ufnt, fill=(255, 255, 255, 90))
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
        # Highs carry far less energy than bass: tilt them up and lift
        # quiet bands, so the whole ring reaches out (not just the bottom).
        v = max(0.0, v) * (1.0 + 1.4 * i / max(1, half - 1))
        v = v ** 0.75 if v > 0 else 0.0
        out.append(min(1.2, v))
    # bottom -> top on the right, mirrored on the left
    return out + out[::-1]


class OrbArt:
    """Cached static parts of the orb for one cover, size and look.

    ``disc`` picks how the cover is shown: ``glow`` (a lit spinning globe),
    ``vinyl`` (a record with the cover as its spinning label), ``square``
    (the cover itself, still) or ``none`` (just the visualizer)."""

    def __init__(self, diameter: int, cover: Image.Image | None, accent=(255, 255, 255),
                 disc: str = "glow", halo: bool = True, backdrop: float = 0.0) -> None:
        self.diameter = diameter
        self.disc = disc
        self.spins = disc in ("glow", "vinyl")
        d = diameter
        self.label = None
        self.art = None
        self.dim = None
        self.rim = None

        def cover_rgb():
            if cover is not None:
                return cover.convert("RGB").resize((d, d), Image.LANCZOS)
            return Image.new("RGB", (d, d), (40, 42, 52))

        if disc == "glow":
            # Sphere shading: brighter centre, darker limb, so flat art reads
            # as a lit globe like the reference.
            shade = Image.radial_gradient("L").resize((d, d)).point(lambda v: int(255 - v * 0.55))
            art = ImageChops.multiply(cover_rgb(), Image.merge("RGB", (shade, shade, shade)))
            self.art = art.convert("RGBA")
            self.art.putalpha(_circle_mask(d))
        elif disc == "vinyl":
            from .music_widget import build_vinyl
            record, label = build_vinyl(d, cover)
            self.art = record           # static grooves
            self.label = label          # spins
        elif disc == "square":
            mask = Image.new("L", (d * 2, d * 2), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, d * 2 - 1, d * 2 - 1), radius=d // 6, fill=255)
            self.art = cover_rgb().convert("RGBA")
            self.art.putalpha(mask.resize((d, d), Image.LANCZOS))

        if self.art is not None and disc != "vinyl":
            dim = Image.eval(self.art.convert("RGB"), lambda v: int(v * 0.8)).convert("RGBA")
            dim.putalpha(self.art.getchannel("A"))
            self.dim = dim
        if disc == "glow":
            rim = Image.new("RGBA", (d, d), (0, 0, 0, 0))
            ImageDraw.Draw(rim).ellipse((1, 1, d - 2, d - 2), outline=(255, 255, 255, 60), width=max(1, d // 240))
            self.rim = rim.filter(ImageFilter.GaussianBlur(0.6))

        # Halo: a big soft light behind the disc (the album-art glow), and
        # an optional dark pool under everything so it reads on any wallpaper.
        size = int(d * 2.2)
        self.halo = None
        if halo or backdrop > 0:
            layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            c = size / 2
            if backdrop > 0:
                pool = Image.new("L", (size, size), 0)
                pd = ImageDraw.Draw(pool)
                for k in range(16, 0, -1):
                    r = d / 2 * (1 + 0.06 * k)
                    pd.ellipse((c - r, c - r, c + r, c + r), fill=int(200 * backdrop * (1 - k / 17)))
                pool = pool.filter(ImageFilter.GaussianBlur(d / 16))
                dark = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                dark.putalpha(pool)
                layer.alpha_composite(dark)
            if halo:
                light = Image.new("L", (size, size), 0)
                hd = ImageDraw.Draw(light)
                for k in range(10, 0, -1):
                    r = d / 2 * (1 + 0.04 * k)
                    hd.ellipse((c - r, c - r, c + r, c + r), fill=int(70 * (1 - k / 11) ** 2))
                light = light.filter(ImageFilter.GaussianBlur(d / 20))
                glow = Image.new("RGBA", (size, size), accent + (0,))
                glow.putalpha(light)
                layer.alpha_composite(glow)
            self.halo = layer


def _circle_mask(d: int) -> Image.Image:
    mask = Image.new("L", (d * 2, d * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d * 2 - 1, d * 2 - 1), fill=255)
    return mask.resize((d, d), Image.LANCZOS)


def _draw_visualizer(draw: ImageDraw.ImageDraw, style: str, values: list[float], cx: float, cy: float,
                     inner: float, reach: float, d: int) -> None:
    count = len(values)
    if style == "none" or not count:
        return
    bar_w = max(2, int(math.pi * d / count * 0.30))
    if style == "wave":
        # A closed, smoothed outline round the disc, filled faintly.
        smooth = [(values[i - 1] + 2 * values[i] + values[(i + 1) % count]) / 4 for i in range(count)]
        pts = []
        for i, v in enumerate(smooth):
            angle = math.pi / 2 + 2 * math.pi * i / count
            r = inner + d * 0.01 + max(0.0, v) * reach
            pts.append((cx + math.cos(angle) * r, cy + math.sin(angle) * r))
        draw.polygon(pts, fill=60)
        draw.line(pts + [pts[0]], fill=255, width=max(2, d // 110), joint="curve")
        return
    for i, v in enumerate(values):
        angle = math.pi / 2 + 2 * math.pi * i / count   # start at the bottom
        ca, sa = math.cos(angle), math.sin(angle)
        length = inner + d * 0.012 + max(0.0, v) * reach
        level = int(150 + 105 * min(1.0, v))
        if style == "dots":
            r = max(2, bar_w * 0.8)
            x, y = cx + ca * length, cy + sa * length
            draw.ellipse((x - r, y - r, x + r, y + r), fill=level)
        elif style == "blocks":
            seg = max(4.0, reach / 14)
            t = inner
            while t + seg * 0.6 <= length:
                draw.line((cx + ca * t, cy + sa * t, cx + ca * (t + seg * 0.6), cy + sa * (t + seg * 0.6)),
                          fill=level, width=bar_w + 1)
                t += seg
        else:  # rays
            draw.line((cx + ca * inner, cy + sa * inner, cx + ca * length, cy + sa * length),
                      fill=level, width=bar_w)


def render_orb(box: Box, layout: SceneLayout, art: OrbArt, bars: list[float], spin_deg: float,
               playing: bool, energy: float = 0.0, accent=(255, 255, 255), screen_height: int = 1440,
               settings=None) -> Image.Image:
    st = _settings(settings)
    cx, cy = layout.orb_cx - box.x, layout.orb_cy - box.y
    d = art.diameter

    # The halo never changes for a track: composite it once per box and
    # start every frame from a copy.
    base_key = (box.w, box.h, cx, cy)
    if getattr(art, "_base_key", None) != base_key:
        base = Image.new("RGBA", (box.w, box.h), (0, 0, 0, 0))
        if art.halo is not None:
            base.alpha_composite(art.halo, (int(cx - art.halo.width / 2), int(cy - art.halo.height / 2)))
        art._base = base
        art._base_key = base_key
    image = art._base.copy()

    if st.visualizer != "none":
        values = ring_values(bars, st.bar_count)
        inner = d / 2 + d * 0.05
        reach = (screen_height * 0.15 * st.ray_length * (1.0 if playing else 0.35)
                 * (1.0 + 0.15 * min(1.0, energy)))
        shapes = Image.new("L", image.size, 0)
        _draw_visualizer(ImageDraw.Draw(shapes), st.visualizer, values, cx, cy, inner, reach, d)
        # Light bleed around the shapes, blurred at quarter size, merged
        # with the crisp shapes into one alpha: one composite for the ring.
        alpha = shapes
        if st.glow > 0:
            bleed = _blur_quarter(shapes, d / 160, 0.55 * st.glow)
            alpha = ImageChops.lighter(bleed, shapes)
        ring = Image.new("RGBA", image.size, accent + (0,))
        ring.putalpha(alpha)
        image.alpha_composite(ring)

    if art.art is not None:
        origin = (int(cx - d / 2), int(cy - d / 2))
        if art.disc == "vinyl":
            image.alpha_composite(art.art, origin)
            if art.label is not None:
                label = art.label.rotate(spin_deg, resample=Image.BILINEAR) if spin_deg else art.label
                lw = label.width
                image.alpha_composite(label, (int(cx - lw / 2), int(cy - lw / 2)))
        else:
            disc = art.art
            if not playing and art.dim is not None:
                disc = art.dim
            if art.spins and spin_deg:
                disc = disc.rotate(spin_deg, resample=Image.BILINEAR)
            image.alpha_composite(disc, origin)
        if art.rim is not None:
            image.alpha_composite(art.rim, origin)
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


# ------------------------------------------------------------------ split orb
# The live widget draws the orb as three windows so each frame only redraws
# what moves: the halo (static per track), the ring (every frame, built
# straight into premultiplied BGRA bytes, no full-size composite), and the
# disc (a small window that only rotates).


def disc_box(layout: SceneLayout, art: OrbArt) -> Box:
    d = art.diameter
    return Box(int(layout.orb_cx - d / 2), int(layout.orb_cy - d / 2), d, d)


def render_halo(box: Box, layout: SceneLayout, art: OrbArt) -> Image.Image:
    image = Image.new("RGBA", (box.w, box.h), (0, 0, 0, 0))
    if art.halo is not None:
        cx, cy = layout.orb_cx - box.x, layout.orb_cy - box.y
        image.alpha_composite(art.halo, (int(cx - art.halo.width / 2), int(cy - art.halo.height / 2)))
    return image


def render_ring_bgra(box: Box, layout: SceneLayout, bars: list[float], playing: bool, energy: float,
                     accent=(255, 255, 255), screen_height: int = 1440, settings=None,
                     intro: float = 1.0, diameter: int | None = None) -> bytes:
    """The visualizer ring as premultiplied BGRA bytes for ``box``."""
    st = _settings(settings)
    d = diameter or layout.orb_diameter
    cx, cy = layout.orb_cx - box.x, layout.orb_cy - box.y
    shapes = Image.new("L", (box.w, box.h), 0)
    if st.visualizer != "none":
        values = ring_values(bars, st.bar_count)
        inner = d / 2 + d * 0.05
        reach = (screen_height * 0.15 * st.ray_length * (1.0 if playing else 0.35)
                 * (1.0 + 0.15 * min(1.0, energy)) * max(0.0, intro))
        _draw_visualizer(ImageDraw.Draw(shapes), st.visualizer, values, cx, cy, inner, reach, d)
    alpha = shapes
    if st.glow > 0:
        # A tight sheen right on the rays, not a wash of light round them.
        bleed = _blur_quarter(shapes, d / 160, 0.55 * st.glow)
        alpha = ImageChops.lighter(bleed, shapes)
    if intro < 0.999:
        k = max(0.0, min(1.0, intro))
        alpha = alpha.point(lambda v: int(v * k))
    r, g, b = accent
    bands = [alpha.point(lambda v, c=c: v * c // 255) for c in (b, g, r)]
    return Image.merge("RGBA", (*bands, alpha)).tobytes()


def render_disc(art: OrbArt, spin_deg: float, playing: bool) -> Image.Image | None:
    if art.art is None:
        return None
    if art.disc == "vinyl":
        image = art.art.copy()
        if art.label is not None:
            label = art.label.rotate(spin_deg, resample=Image.BILINEAR) if spin_deg else art.label
            off = (image.width - label.width) // 2
            image.alpha_composite(label, (off, off))
        return image
    disc = art.dim if (not playing and art.dim is not None) else art.art
    if art.spins and spin_deg:
        disc = disc.rotate(spin_deg, resample=Image.BILINEAR)
    if art.rim is not None:
        disc = disc.copy()
        disc.alpha_composite(art.rim)
    return disc
