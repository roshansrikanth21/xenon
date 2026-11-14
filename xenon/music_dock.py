"""The corner dock: a small album disc tucked into the bottom-left corner
that fans out into controls and a song list when the pointer reaches it.

Collapsed it is just the disc. Hovering opens a panel beside it: a row of
buttons (previous, play/pause, next, settings), what is playing now, and the
last few songs with their covers; clicking one plays it. Rendering is pure
PIL; ``music_widget`` owns the window, the pointer polling and the actions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter

from .music_scene import Box, font

BUTTONS = ("previous", "playpause", "next", "settings")
MAX_ROWS = 5


@dataclass(frozen=True)
class DockGeometry:
    box: Box                       # the dock window, screen coordinates
    disc_center: tuple[int, int]   # screen coordinates
    disc: int                      # disc diameter
    panel: Box                     # expanded panel, screen coordinates
    targets: dict = field(default_factory=dict)   # name -> Box (screen)
    rows: int = 0

    def local(self, b: Box) -> Box:
        return Box(b.x - self.box.x, b.y - self.box.y, b.w, b.h)


def dock_geometry(width: int, height: int, rows: int, bottom: int | None = None) -> DockGeometry:
    """``bottom`` is the lowest usable row (the top of the taskbar); the
    disc tucks into the corner just above it."""
    rows = max(0, min(MAX_ROWS, rows))
    floor = height if bottom is None else max(int(height * 0.6), min(height, bottom))
    d = int(height * 0.15)
    cx, cy = int(d * 0.42), floor - int(d * 0.42)
    pad = max(10, int(height * 0.009))
    pw = int(max(300, min(width * 0.16, height * 0.30)))
    icon = int(height * 0.030)
    now_h = int(height * 0.058)
    row_h = int(height * 0.046)
    inner_h = icon + pad + now_h + (pad if rows else 0) + rows * row_h
    ph = inner_h + pad * 2
    px = cx + int(d * 0.62)
    py = floor - int(height * 0.025) - ph
    panel = Box(px, py, pw, ph)

    targets: dict[str, Box] = {}
    x = px + pad
    for name in BUTTONS:
        targets[name] = Box(x, py + pad, icon, icon)
        x += icon + int(pad * 0.8)
    y = py + pad + icon + pad
    targets["now"] = Box(px + pad, y, pw - pad * 2, now_h)
    y += now_h + pad
    for i in range(rows):
        targets[f"row{i}"] = Box(px + pad, y + i * row_h, pw - pad * 2, row_h)
    targets["disc"] = Box(cx - d // 2, cy - d // 2, d, d)

    glow = int(d * 0.25)
    bx = 0
    by = min(py - 20, cy - d // 2 - glow)
    bw = px + pw + 24      # room for the panel's soft shadow
    box = Box(bx, max(0, by), bw, floor - max(0, by))
    return DockGeometry(box, (cx, cy), d, panel, targets, rows)


def hit(geom: DockGeometry, x: int, y: int, open_: bool) -> str | None:
    """Which control is under a screen point. Only the disc counts while the
    dock is closed; everything counts once it is open."""
    names = list(geom.targets) if open_ else ["disc"]
    for name in names:
        b = geom.targets[name]
        if name == "disc":
            cx, cy = geom.disc_center
            if (x - cx) ** 2 + (y - cy) ** 2 <= (geom.disc / 2) ** 2:
                return name
            continue
        if b.x <= x < b.x + b.w and b.y <= y < b.y + b.h:
            return name
    return None


def in_dock(geom: DockGeometry, x: int, y: int, open_: bool) -> bool:
    """Is the pointer over the dock (the disc, or the open panel with a
    little slack so the panel does not snap shut on the way across)?"""
    cx, cy = geom.disc_center
    if (x - cx) ** 2 + (y - cy) ** 2 <= (geom.disc / 2 * 1.05) ** 2:
        return True
    if not open_:
        return False
    p, slack = geom.panel, 24
    if p.x - geom.disc // 2 - slack <= x < p.x + p.w + slack and p.y - slack <= y <= geom.box.y + geom.box.h:
        return True
    return False


def ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def ease_out_back(t: float, s: float = 1.4) -> float:
    t = max(0.0, min(1.0, t)) - 1
    return 1 + t * t * ((s + 1) * t + s)


# ------------------------------------------------------------------ drawing


def _vinyl_parts(d: int, cover: Image.Image | None):
    """The corner disc: the album cover filling a record that spins, with
    faint grooves pressed into it. Returns (base, spinning, overlay): the
    base is the soft shadow, the overlay the still light sheen, rim and
    spindle hole (a real record's highlights don't turn with it)."""
    from PIL import ImageChops

    size = int(d * 1.5)
    c = size / 2
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow = Image.new("L", (size, size), 0)
    oy = d * 0.04
    ImageDraw.Draw(shadow).ellipse((c - d * 0.5, c - d * 0.5 + oy, c + d * 0.5, c + d * 0.5 + oy), fill=160)
    shadow = shadow.filter(ImageFilter.GaussianBlur(d / 14))
    dark = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dark.putalpha(shadow)
    base.alpha_composite(dark)

    s2 = d * 2      # supersample for clean edges and grooves
    if cover is not None:
        art = cover.convert("RGB")
        w, h = art.size
        side = min(w, h)
        art = art.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        art = art.resize((s2, s2), Image.LANCZOS)
    else:
        art = Image.new("RGB", (s2, s2), (30, 31, 38))
    # grooves: thin dark rings over the art, lighter toward the label area
    grooves = Image.new("L", (s2, s2), 0)
    gd = ImageDraw.Draw(grooves)
    r = s2 / 2 - s2 * 0.02
    i = 0
    while r > s2 * 0.17:
        gd.ellipse((s2 / 2 - r, s2 / 2 - r, s2 / 2 + r, s2 / 2 + r), outline=40 if i % 2 else 22, width=1)
        r -= max(3.0, s2 / 150)
        i += 1
    art = ImageChops.subtract(art, Image.merge("RGB", (grooves, grooves, grooves)))
    spinning = art.convert("RGBA")
    mask = Image.new("L", (s2, s2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, s2 - 1, s2 - 1), fill=255)
    spinning.putalpha(mask)
    spinning = spinning.resize((d, d), Image.LANCZOS)

    over = Image.new("RGBA", (s2, s2), (0, 0, 0, 0))
    sheen = Image.new("L", (s2, s2), 0)
    sd = ImageDraw.Draw(sheen)
    sd.pieslice((0, 0, s2 - 1, s2 - 1), 205, 245, fill=60)
    sd.pieslice((0, 0, s2 - 1, s2 - 1), 25, 65, fill=40)
    sheen = ImageChops.multiply(sheen.filter(ImageFilter.GaussianBlur(s2 / 25)), mask)
    light = Image.new("RGBA", (s2, s2), (255, 255, 255, 0))
    light.putalpha(sheen)
    over.alpha_composite(light)
    od = ImageDraw.Draw(over)
    od.ellipse((2, 2, s2 - 3, s2 - 3), outline=(0, 0, 0, 120), width=max(2, s2 // 90))       # pressed edge
    od.ellipse((1, 1, s2 - 2, s2 - 2), outline=(255, 255, 255, 34), width=2)                  # rim light
    hole = s2 * 0.035
    ring = s2 * 0.075
    od.ellipse((s2 / 2 - ring, s2 / 2 - ring, s2 / 2 + ring, s2 / 2 + ring), fill=(12, 12, 14, 150))
    od.ellipse((s2 / 2 - hole, s2 / 2 - hole, s2 / 2 + hole, s2 / 2 + hole), fill=(4, 4, 6, 255))
    over = over.resize((d, d), Image.LANCZOS)
    return base, spinning, over


def _disc_image(d: int, cover: Image.Image | None, accent) -> Image.Image:
    size = int(d * 1.5)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    c = size / 2
    # A soft dark drop shadow for depth instead of a coloured light aura.
    shadow = Image.new("L", (size, size), 0)
    oy = d * 0.04
    ImageDraw.Draw(shadow).ellipse((c - d * 0.5, c - d * 0.5 + oy, c + d * 0.5, c + d * 0.5 + oy), fill=150)
    shadow = shadow.filter(ImageFilter.GaussianBlur(d / 14))
    dark = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dark.putalpha(shadow)
    out.alpha_composite(dark)
    if cover is not None:
        art = cover.convert("RGB").resize((d, d), Image.LANCZOS)
    else:
        art = Image.new("RGB", (d, d), (46, 48, 58))
    shade = Image.radial_gradient("L").resize((d, d)).point(lambda v: int(255 - v * 0.45))
    from PIL import ImageChops

    art = ImageChops.multiply(art, Image.merge("RGB", (shade, shade, shade))).convert("RGBA")
    mask = Image.new("L", (d * 2, d * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d * 2 - 1, d * 2 - 1), fill=255)
    art.putalpha(mask.resize((d, d), Image.LANCZOS))
    out.alpha_composite(art, (int(c - d / 2), int(c - d / 2)))
    ImageDraw.Draw(out).ellipse((c - d / 2, c - d / 2, c + d / 2, c + d / 2),
                                outline=(255, 255, 255, 40), width=1)
    return out


def _icon(draw: ImageDraw.ImageDraw, name: str, b: Box, playing: bool, hot: bool) -> None:
    # Round buttons; play/pause is the solid white primary one.
    primary = name == "playpause"
    if primary:
        draw.ellipse((b.x, b.y, b.x + b.w - 1, b.y + b.h - 1), fill=(255, 255, 255, 255 if hot else 236))
    else:
        draw.ellipse((b.x, b.y, b.x + b.w - 1, b.y + b.h - 1), fill=(255, 255, 255, 40 if hot else 14))
    cx, cy, s = b.x + b.w / 2, b.y + b.h / 2, b.w * 0.2
    ink = (12, 12, 16, 255) if primary else (255, 255, 255, 250 if hot else 215)
    if primary and not playing:
        cx += s * 0.15   # optical centre of the triangle
    if name == "playpause":
        if playing:
            bw = s * 0.55
            draw.rectangle((cx - s * 0.9, cy - s, cx - s * 0.9 + bw, cy + s), fill=ink)
            draw.rectangle((cx + s * 0.9 - bw, cy - s, cx + s * 0.9, cy + s), fill=ink)
        else:
            draw.polygon([(cx - s * 0.7, cy - s), (cx - s * 0.7, cy + s), (cx + s * 1.0, cy)], fill=ink)
    elif name in ("previous", "next"):
        sign = 1 if name == "next" else -1
        for off in (-0.55, 0.45):
            x0 = cx + sign * s * off
            draw.polygon([(x0 - sign * s * 0.5, cy - s * 0.8), (x0 - sign * s * 0.5, cy + s * 0.8),
                          (x0 + sign * s * 0.5, cy)], fill=ink)
        xb = cx + sign * s * 0.98
        draw.rectangle((xb - 1, cy - s * 0.8, xb + 1, cy + s * 0.8), fill=ink)
    elif name == "settings":
        for k in range(8):
            a = k * math.pi / 4
            draw.line((cx + math.cos(a) * s * 0.7, cy + math.sin(a) * s * 0.7,
                       cx + math.cos(a) * s * 1.15, cy + math.sin(a) * s * 1.15), fill=ink, width=max(2, int(s / 3)))
        draw.ellipse((cx - s * 0.78, cy - s * 0.78, cx + s * 0.78, cy + s * 0.78), outline=ink, width=max(2, int(s / 3)))


_SHADOW = 18   # px of soft shadow drawn round the panel


def _panel_back(w: int, h: int) -> Image.Image:
    """Frosted dark glass: a soft shadow, a near-black body with a faint
    top sheen, and a hairline border."""
    s = _SHADOW
    out = Image.new("RGBA", (w + s * 2, h + s * 2), (0, 0, 0, 0))
    r = max(16, min(24, h // 12))
    shade = Image.new("L", out.size, 0)
    ImageDraw.Draw(shade).rounded_rectangle((s, s + 4, s + w - 1, s + h + 3), radius=r, fill=130)
    shade = shade.filter(ImageFilter.GaussianBlur(s / 2))
    dark = Image.new("RGBA", out.size, (0, 0, 0, 0))
    dark.putalpha(shade)
    out.alpha_composite(dark)

    body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mask = Image.new("L", (w * 2, h * 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * 2 - 1, h * 2 - 1), radius=r * 2, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    top, bottom = (24, 25, 32), (10, 10, 14)
    grad = Image.linear_gradient("L").resize((w, h))
    fill = Image.merge("RGB", [grad.point(lambda v, a=a, b=b: int(a + (b - a) * v / 255)) for a, b in zip(top, bottom)])
    body = fill.convert("RGBA")
    body.putalpha(mask.point(lambda v: v * 222 // 255))
    out.alpha_composite(body, (s, s))
    ImageDraw.Draw(out).rounded_rectangle((s, s, s + w - 1, s + h - 1), radius=r,
                                          outline=(255, 255, 255, 26), width=1)
    # a brighter hairline along the top edge catches the light
    ImageDraw.Draw(out).line((s + r, s, s + w - r, s), fill=(255, 255, 255, 46), width=1)
    return out


def _spaced(draw, xy, text: str, fnt, fill, spacing: float = 0.14) -> None:
    x, y = xy
    gap = fnt.size * spacing
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + gap


def _play_badge(img: Image.Image, x: int, y: int, size: int) -> None:
    """A dark veil and a small white play triangle over a hovered cover."""
    veil = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size - 1, size - 1), radius=max(6, size // 5), fill=110)
    veil.putalpha(m)
    img.alpha_composite(veil, (x, y))
    d = ImageDraw.Draw(img)
    cx, cy, s = x + size / 2 + size * 0.04, y + size / 2, size * 0.2
    d.polygon([(cx - s * 0.7, cy - s), (cx - s * 0.7, cy + s), (cx + s, cy)], fill=(255, 255, 255, 245))


def _sparkle(draw, cx: float, cy: float, r: float, fill) -> None:
    """A small four-point star marking a suggested song."""
    k = r * 0.28
    draw.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
                  (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)], fill=fill)


def _fit(draw, text: str, fnt, width: int) -> str:
    if draw.textlength(text, font=fnt) <= width:
        return text
    while text and draw.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text + "…"


def _thumb(image: Image.Image | None, size: int, radius: int) -> Image.Image:
    if image is not None:
        out = image.convert("RGB").resize((size, size), Image.LANCZOS).convert("RGBA")
        fill = 255
    else:
        out = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        fill = 34   # a faint placeholder tile
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=fill)
    out.putalpha(mask)
    return out


@dataclass
class DockContent:
    title: str = ""
    artist: str = ""
    playing: bool = False
    cover: Image.Image | None = None
    rows: list = field(default_factory=list)   # [(title, artist, thumb image | None)]


class DockRenderer:
    """Caches the disc and the panel pieces; ``render`` only composites."""

    def __init__(self) -> None:
        self._disc_key = None
        self._disc = None
        self._parts_key = None
        self._parts: list[tuple[Image.Image, tuple[int, int], int]] = []

    def _disc_for(self, geom: DockGeometry, cover, accent, spin: float = 0.0) -> Image.Image:
        key = (id(cover), geom.disc)
        if key != self._disc_key:
            self._disc, self._label, self._over = _vinyl_parts(geom.disc, cover)
            self._disc_key = key
        record = self._label.rotate(-spin, resample=Image.BICUBIC) if spin else self._label
        disc = self._disc.copy()
        off = ((disc.width - record.width) // 2, (disc.height - record.height) // 2)
        disc.alpha_composite(record, off)
        disc.alpha_composite(self._over, off)
        return disc

    def _panel_parts(self, geom: DockGeometry, content: DockContent, hover: str | None, accent):
        """Panel pieces in dock-local coordinates, each with a stagger index."""
        key = (geom, content.title, content.artist, content.playing, id(content.cover),
               tuple((r[0], r[1], id(r[2]), len(r) > 3 and r[3]) for r in content.rows), hover, tuple(accent))
        if key == self._parts_key:
            return self._parts
        parts = []
        p = geom.local(geom.panel)
        parts.append((_panel_back(p.w, p.h), (p.x - _SHADOW, p.y - _SHADOW), 0))

        for i, name in enumerate(BUTTONS):
            b = geom.local(geom.targets[name])
            img = Image.new("RGBA", (b.w, b.h), (0, 0, 0, 0))
            _icon(ImageDraw.Draw(img), name, Box(0, 0, b.w, b.h), content.playing, hover == name)
            parts.append((img, (b.x, b.y), 1 + i // 2))

        now = geom.local(geom.targets["now"])
        img = Image.new("RGBA", (now.w, now.h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        small = font("NOW PLAYING", max(10, now.h // 6), "bold")
        _spaced(draw, (now.h + 10, 0), "NOW PLAYING" if content.playing else "PAUSED", small,
                (255, 255, 255, 120))
        img.alpha_composite(_thumb(content.cover, now.h - 4, max(8, now.h // 5)), (0, 2))
        tf = font(content.title or "Nothing playing", max(13, int(now.h * 0.34)), "black")
        af = font(content.artist, max(11, int(now.h * 0.24)), "regular")
        tw = now.w - now.h - 12
        draw.text((now.h + 10, small.size + 3), _fit(draw, content.title or "Nothing playing", tf, tw),
                  font=tf, fill=(255, 255, 255, 245))
        if content.artist:
            draw.text((now.h + 10, small.size + tf.size + 6), _fit(draw, content.artist, af, tw), font=af,
                      fill=(220, 222, 232, 170))
        parts.append((img, (now.x, now.y), 2))
        if geom.rows:
            line = Image.new("RGBA", (now.w, 1), (255, 255, 255, 22))
            gap = geom.local(geom.targets["row0"]).y - (now.y + now.h)
            parts.append((line, (now.x, now.y + now.h + gap // 2), 2))

        for i, row in enumerate(content.rows[:geom.rows]):
            title, artist, thumb = row[:3]
            suggested = len(row) > 3 and row[3]
            hot = hover == f"row{i}"
            b = geom.local(geom.targets[f"row{i}"])
            img = Image.new("RGBA", (b.w, b.h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            if hot:
                draw.rounded_rectangle((0, 1, b.w - 1, b.h - 2), radius=max(10, b.h // 4), fill=(255, 255, 255, 26))
            ts = b.h - 12
            img.alpha_composite(_thumb(thumb, ts, max(6, ts // 5)), (6, 6))
            if hot:
                _play_badge(img, 6, 6, ts)
            tf = font(title, max(12, int(b.h * 0.29)), "bold")
            af = font(artist, max(10, int(b.h * 0.22)), "regular")
            mark = 18 if suggested else 0
            tw = b.w - ts - 24 - mark
            ty = (b.h - tf.size - af.size - 3) // 2 - 1
            draw.text((ts + 16, ty), _fit(draw, title, tf, tw), font=tf,
                      fill=(255, 255, 255, 250 if hot else 228))
            draw.text((ts + 16, ty + tf.size + 3), _fit(draw, artist, af, tw), font=af,
                      fill=(220, 222, 232, 150))
            if suggested:
                _sparkle(draw, b.w - 14, b.h / 2, 5, (255, 255, 255, 110 if not hot else 200))
            parts.append((img, (b.x, b.y), 3 + i))

        self._parts_key = key
        self._parts = parts
        return parts

    def render(self, geom: DockGeometry, content: DockContent, openness: float,
               hover: str | None, accent=(255, 255, 255), press: float = 0.0,
               spin: float = 0.0, draw_disc: bool = True) -> Image.Image:
        """``openness`` 0..1 drives the fan-out: each piece slides out from
        the disc and fades in, a few milliseconds after the one before."""
        out = Image.new("RGBA", (geom.box.w, geom.box.h), (0, 0, 0, 0))
        if openness > 0.001:
            for img, (x, y), order in self._panel_parts(geom, content, hover, accent):
                t = (openness - order * 0.06) / max(0.2, 1 - order * 0.06)
                if t <= 0:
                    continue
                e = ease_out_back(t) if order else ease_out_cubic(t)
                dx = int((1 - e) * -geom.disc * 0.45)
                dy = int((1 - e) * geom.disc * 0.18)
                alpha = max(0.0, min(1.0, t * 1.4))
                piece = img
                if alpha < 0.999:
                    piece = img.copy()
                    piece.putalpha(img.getchannel("A").point(lambda v, a=alpha: int(v * a)))
                _paste(out, piece, x + dx, y + dy)

        if draw_disc:
            disc = self._scaled_disc(geom, content, accent, openness, press, spin)
            cx, cy = geom.disc_center
            _paste(out, disc, int(cx - geom.box.x - disc.width / 2), int(cy - geom.box.y - disc.height / 2))
        return out

    def _scaled_disc(self, geom, content, accent, openness, press, spin):
        disc = self._disc_for(geom, content.cover, accent, spin)
        scale = 1.0 + 0.06 * openness - 0.05 * press
        if abs(scale - 1.0) > 0.004:
            size = max(2, int(disc.width * scale))
            disc = disc.resize((size, size), Image.BILINEAR)
        return disc

    def render_vinyl(self, geom: DockGeometry, box: Box, content: DockContent, openness: float,
                     accent=(255, 255, 255), press: float = 0.0, spin: float = 0.0) -> Image.Image:
        """Just the corner record, for its own small window (``vinyl_box``),
        so spinning it doesn't repaint the whole dock."""
        out = Image.new("RGBA", (box.w, box.h), (0, 0, 0, 0))
        disc = self._scaled_disc(geom, content, accent, openness, press, spin)
        cx, cy = geom.disc_center
        _paste(out, disc, int(cx - box.x - disc.width / 2), int(cy - box.y - disc.height / 2))
        return out


def vinyl_box(geom: DockGeometry) -> Box:
    """Screen box of the corner record (with its shadow and open-scale
    room), clipped to the dock window."""
    size = int(geom.disc * 1.5 * 1.07)
    cx, cy = geom.disc_center
    x0, y0 = max(geom.box.x, cx - size // 2), max(geom.box.y, cy - size // 2)
    x1 = min(geom.box.x + geom.box.w, cx + size // 2)
    y1 = min(geom.box.y + geom.box.h, cy + size // 2)
    return Box(x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def _paste(out: Image.Image, img: Image.Image, x: int, y: int) -> None:
    """alpha_composite that clips at the edges (the disc hangs off-screen)."""
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(img.width, out.width - x), min(img.height, out.height - y)
    if right <= left or bottom <= top:
        return
    if (left, top, right, bottom) != (0, 0, img.width, img.height):
        img = img.crop((left, top, right, bottom))
    out.alpha_composite(img, (x + left, y + top))
