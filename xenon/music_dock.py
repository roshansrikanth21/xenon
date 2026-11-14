"""The corner dock: a small album disc tucked into the bottom-left corner
that fans out into controls and a song list when the pointer reaches it.

Collapsed it is just the spinning record. Hovering makes the rest pop out
of it, floating with no panel: a row of buttons (previous, play/pause,
next, settings), what is playing now, and suggested and recent songs with
their covers along an arc round the record; clicking one plays it. Rendering is pure
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
    open_center: tuple = (0, 0)    # where the record sits, grown, while open
    open_disc: int = 0             # its diameter then

    def local(self, b: Box) -> Box:
        return Box(b.x - self.box.x, b.y - self.box.y, b.w, b.h)


def dock_geometry(width: int, height: int, rows: int, bottom: int | None = None) -> DockGeometry:
    """``bottom`` is the lowest usable row (the top of the taskbar); the
    small record tucks into the corner just above it.

    Opening, the record grows into a big disc in the corner and the menu
    wraps round its right-hand edge: the controls, what is playing and the
    song list stack up with their left edges on a circle just outside the
    record, so the list curves round it like a moon's halo."""
    rows = max(0, min(MAX_ROWS, rows))
    floor = height if bottom is None else max(int(height * 0.6), min(height, bottom))
    d = int(height * 0.15)
    cx, cy = int(d * 0.42), floor - int(d * 0.42)
    pad = max(10, int(height * 0.009))
    iw = int(max(260, min(width * 0.14, height * 0.25)))
    icon = int(height * 0.026)
    now_h = int(height * 0.056)
    row_h = int(height * 0.044)

    big = int(height * 0.34)                       # the record, open
    ocx = int(big * 0.28)                          # a little of it off the left edge
    ocy = floor - big // 2 - pad
    radius = big / 2 + max(pad * 3, int(big * 0.09))   # the circle the menu sits on

    total = icon + pad + now_h + (int(pad * 0.6) + rows * row_h if rows else 0)
    top = max(pad, int(ocy - total / 2))

    def x_at(mid: float) -> int:
        dy = mid - ocy
        return int(ocx + math.sqrt(max(0.0, radius * radius - dy * dy)))

    targets: dict[str, Box] = {}
    y = top
    x = x_at(y + icon / 2)
    for name in BUTTONS:
        targets[name] = Box(x, y, icon, icon)
        x += icon + int(pad * 0.7)
    y += icon + pad
    targets["now"] = Box(x_at(y + now_h / 2), y, iw, now_h)
    y += now_h + int(pad * 0.6)
    for i in range(rows):
        targets[f"row{i}"] = Box(x_at(y + row_h / 2), y, iw, row_h)
        y += row_h
    x0 = min(b.x for b in targets.values())
    x1 = max(b.x + b.w for b in targets.values())
    panel = Box(x0, top, x1 - x0, y - top)
    targets["disc"] = Box(cx - d // 2, cy - d // 2, d, d)

    by = max(0, min(top - pad * 3, ocy - int(big * 0.75), cy - int(d * 0.75)))
    bw = max(x1 + 40, ocx + int(big * 0.8))     # room for the text shadows and the big record
    box = Box(0, by, bw, floor - by)
    return DockGeometry(box, (cx, cy), d, panel, targets, rows, (ocx, ocy), big)


def disc_at(geom: DockGeometry, e: float) -> tuple[float, float, float]:
    """Record centre and diameter at eased openness ``e`` (small in the
    corner at 0, grown at 1)."""
    if not geom.open_disc:
        return geom.disc_center[0], geom.disc_center[1], geom.disc
    (sx, sy), (bx, by) = geom.disc_center, geom.open_center
    return sx + (bx - sx) * e, sy + (by - sy) * e, geom.disc + (geom.open_disc - geom.disc) * e


def hit(geom: DockGeometry, x: int, y: int, open_: bool) -> str | None:
    """Which control is under a screen point. Only the record counts while
    the dock is closed; everything counts once it is open."""
    names = list(geom.targets) if open_ else ["disc"]
    for name in names:
        b = geom.targets[name]
        if name == "disc":
            cx, cy, d = disc_at(geom, 1.0 if open_ else 0.0)
            if (x - cx) ** 2 + (y - cy) ** 2 <= (d / 2) ** 2:
                return name
            continue
        if b.x <= x < b.x + b.w and b.y <= y < b.y + b.h:
            return name
    return None


def in_dock(geom: DockGeometry, x: int, y: int, open_: bool) -> bool:
    """Is the pointer over the dock (the record, or the open menu with a
    little slack so it does not snap shut on the way across)?"""
    cx, cy, d = disc_at(geom, 1.0 if open_ else 0.0)
    if (x - cx) ** 2 + (y - cy) ** 2 <= (d / 2 * 1.05) ** 2:
        return True
    if not open_:
        return False
    p, slack = geom.panel, 24
    top = min(p.y, cy - d / 2) - slack
    return x < p.x + p.w + slack and top <= y <= geom.box.y + geom.box.h


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
    # Small round glass buttons with a hairline rim; play/pause is the solid
    # white primary one.
    primary = name == "playpause"
    box = (b.x + 1, b.y + 1, b.x + b.w - 2, b.y + b.h - 2)
    if primary:
        draw.ellipse(box, fill=(255, 255, 255, 255 if hot else 238))
    else:
        draw.ellipse(box, fill=(255, 255, 255, 52 if hot else 26),
                     outline=(255, 255, 255, 120 if hot else 64), width=max(1, b.w // 30))
    cx, cy, s = b.x + b.w / 2, b.y + b.h / 2, b.w * 0.2
    ink = (12, 12, 16, 255) if primary else (255, 255, 255, 255 if hot else 225)
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


_SHADOW = 14   # px of soft shadow round each floating piece


def _shadowed(img: Image.Image, blur: float = 5.0) -> Image.Image:
    """The piece over a soft dark shadow of itself, so white text reads on
    any wallpaper without a panel behind it."""
    m = _SHADOW
    out = Image.new("RGBA", (img.width + m * 2, img.height + m * 2), (0, 0, 0, 0))
    shade = Image.new("L", out.size, 0)
    shade.paste(img.getchannel("A"), (m, m + 2))
    shade = shade.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: min(190, int(v * 1.5)))
    dark = Image.new("RGBA", out.size, (0, 0, 0, 0))
    dark.putalpha(shade)
    out.alpha_composite(dark)
    out.alpha_composite(img, (m, m))
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
        self._parts_key = None
        self._parts: list = []
        self._pieces: dict = {}
        self._pieces_for = None

    def _parts_for(self, size: int, cover):
        cache = self.__dict__.setdefault("_records", {})
        key = (id(cover), size)
        parts = cache.get(key)
        if parts is None:
            # Room for two covers (the swap on a new song draws the old one
            # for a moment), each small and grown.
            if len(cache) >= 4 and not any(k[0] == id(cover) for k in cache):
                cache.clear()
            base, label, over = _vinyl_parts(size, cover)
            # For the grow animation: label and sheen merged, premultiplied
            # (Pillow resizes RGBA by converting to RGBa and back each time).
            merged = Image.alpha_composite(label, over).convert("RGBa")
            parts = cache[key] = (base, label.convert("RGBa"), over, base.reduce(4), merged)
        return parts

    def prepare(self, geom: DockGeometry, cover) -> None:
        """Build the big record for this cover on a worker thread, so the
        first hover does not stall on it (~0.5 s)."""
        import threading

        if not geom.open_disc:
            return
        cache = self.__dict__.setdefault("_records", {})
        if (id(cover), geom.open_disc) in cache:
            return
        threading.Thread(target=self._parts_for, args=(geom.open_disc, cover), daemon=True,
                         name="xenon-dock-record").start()

    def _disc_for(self, size: int, cover, spin: float = 0.0, target: int | None = None) -> Image.Image:
        """The record built at ``size`` (each size once per cover), turned
        by ``spin`` and shown at ``target`` diameter. Growing, it scales
        the label and over-layer first and stretches a quarter-size shadow,
        instead of building the full-size disc and resizing it."""
        base, label, over, base_small, merged = self._parts_for(size, cover)
        target = target or size
        if abs(target - size) > 1:
            # Mid-grow (half a second): one premultiplied resize and turn
            # of the merged label (its sheen turns along, unnoticed), and a
            # stretched quarter-size shadow.
            k = target / size
            ls = max(2, int(label.width * k))
            record = merged.resize((ls, ls), Image.BILINEAR)
            if spin:
                record = record.rotate(-spin, resample=Image.BILINEAR)
            record = record.convert("RGBA")
            bs = max(2, int(base.width * k))
            disc = base_small.resize((bs, bs), Image.BILINEAR)
            layers = (record,)
        else:
            disc = base.copy()
            record = (label.rotate(-spin, resample=Image.BILINEAR) if spin else label).convert("RGBA")
            layers = (record, over)
        off = ((disc.width - record.width) // 2, (disc.height - record.height) // 2)
        rect = (off[0], off[1], off[0] + record.width, off[1] + record.height)
        region = disc.crop(rect)
        for layer in layers:
            region = Image.alpha_composite(region, layer)
        disc.paste(region, rect[:2])
        return disc

    def _piece(self, key, build) -> Image.Image:
        img = self._pieces.get(key)
        if img is None:
            img = _shadowed(build())
            self._pieces[key] = img
        return img

    def _panel_parts(self, geom: DockGeometry, content: DockContent, hover: str | None, accent):
        """The floating pieces in dock-local coordinates (shadow included),
        each with its pop-out order: the row nearest the record first."""
        key = (geom, content.title, content.artist, content.playing, id(content.cover),
               tuple((r[0], r[1], id(r[2]), len(r) > 3 and r[3]) for r in content.rows), hover)
        if key == self._parts_key:
            return self._parts
        if self._pieces_for != (geom, id(content.cover), content.title, content.artist):
            self._pieces = {}
            self._pieces_for = (geom, id(content.cover), content.title, content.artist)
        m = _SHADOW
        rows = min(geom.rows, len(content.rows))
        parts = []

        for i, name in enumerate(BUTTONS):
            b = geom.local(geom.targets[name])
            hot = hover == name

            def build(b=b, name=name, hot=hot):
                img = Image.new("RGBA", (b.w, b.h), (0, 0, 0, 0))
                _icon(ImageDraw.Draw(img), name, Box(0, 0, b.w, b.h), content.playing, hot)
                return img
            img = self._piece(("btn", name, hot, content.playing), build)
            parts.append((img, (b.x - m, b.y - m), rows + 1 + i * 0.5))

        now = geom.local(geom.targets["now"])

        def build_now():
            img = Image.new("RGBA", (now.w, now.h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            ts = now.h - 4
            img.alpha_composite(_thumb(content.cover, ts, max(8, ts // 6)), (0, 2))
            draw.rounded_rectangle((0, 2, ts - 1, ts + 1), radius=max(8, ts // 6),
                                   outline=(255, 255, 255, 40), width=1)
            small = font("NOW PLAYING", max(10, now.h // 7), "bold")
            tx = ts + 14
            tf = font(content.title or "Nothing playing", max(14, int(now.h * 0.32)), "black")
            af = font(content.artist, max(11, int(now.h * 0.22)), "regular")
            block = small.size + 4 + tf.size + 4 + af.size
            y0 = max(0, (now.h - block) // 2 - 2)
            _spaced(draw, (tx, y0), "NOW PLAYING" if content.playing else "PAUSED", small,
                    (255, 255, 255, 150), 0.18)
            tw = now.w - tx - 4
            draw.text((tx, y0 + small.size + 4), _fit(draw, content.title or "Nothing playing", tf, tw),
                      font=tf, fill=(255, 255, 255, 255))
            if content.artist:
                draw.text((tx, y0 + small.size + tf.size + 8), _fit(draw, content.artist, af, tw), font=af,
                          fill=(225, 227, 236, 190))
            return img
        parts.append((self._piece(("now", content.playing), build_now), (now.x - m, now.y - m), rows))

        for i, row in enumerate(content.rows[:rows]):
            title, artist, thumb = row[:3]
            suggested = len(row) > 3 and row[3]
            hot = hover == f"row{i}"
            b = geom.local(geom.targets[f"row{i}"])

            def build_row(b=b, title=title, artist=artist, thumb=thumb, suggested=suggested, hot=hot):
                img = Image.new("RGBA", (b.w, b.h), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                if hot:
                    draw.rounded_rectangle((0, 2, b.w - 1, b.h - 3), radius=max(10, b.h // 3),
                                           fill=(255, 255, 255, 30))
                ts = b.h - 14
                img.alpha_composite(_thumb(thumb, ts, max(6, ts // 6)), (7, 7))
                if hot:
                    _play_badge(img, 7, 7, ts)
                tf = font(title, max(12, int(b.h * 0.30)), "bold")
                af = font(artist, max(10, int(b.h * 0.22)), "regular")
                mark = 18 if suggested else 0
                tw = b.w - ts - 26 - mark
                ty = (b.h - tf.size - af.size - 3) // 2 - 1
                draw.text((ts + 18, ty), _fit(draw, title, tf, tw), font=tf,
                          fill=(255, 255, 255, 255 if hot else 238))
                draw.text((ts + 18, ty + tf.size + 3), _fit(draw, artist, af, tw), font=af,
                          fill=(225, 227, 236, 175))
                if suggested:
                    _sparkle(draw, b.w - 14, b.h / 2, 5, (255, 255, 255, 120 if not hot else 210))
                return img
            img = self._piece(("row", i, title, artist, id(thumb), suggested, hot), build_row)
            parts.append((img, (b.x - m, b.y - m), rows - 1 - i))

        self._parts_key = key
        self._parts = parts
        return parts

    def render(self, geom: DockGeometry, content: DockContent, openness: float,
               hover: str | None, accent=(255, 255, 255), press: float = 0.0,
               spin: float = 0.0, draw_disc: bool = True) -> Image.Image:
        """``openness`` 0..1 (linear) drives the pop-out: each piece flies
        out of the record's centre, growing and untwisting, and overshoots
        a touch into place, a beat after the one below it. Closing runs the
        same path backwards, top piece first."""
        out = Image.new("RGBA", (geom.box.w, geom.box.h), (0, 0, 0, 0))
        if openness > 0.001:
            parts = self._panel_parts(geom, content, hover, accent)
            last = max((p[2] for p in parts), default=0)
            stagger = 0.055
            span = max(0.3, 1.0 - last * stagger)
            oc = geom.open_center if geom.open_disc else geom.disc_center
            ox, oy = oc[0] - geom.box.x, oc[1] - geom.box.y
            for img, (x, y), order in parts:
                t = min(1.0, (openness - order * stagger) / span)
                if t <= 0:
                    continue
                if t >= 1.0:
                    _paste(out, img, x, y)
                    continue
                e = ease_out_back(t, 1.3)
                scale = 0.3 + 0.7 * e
                piece = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                                   Image.BILINEAR)
                angle = (1.0 - e) * 24.0
                if abs(angle) > 0.3:
                    piece = piece.rotate(angle, Image.BILINEAR, expand=True)
                alpha = min(1.0, t * 1.8)
                if alpha < 0.999:
                    piece.putalpha(piece.getchannel("A").point(lambda v, a=alpha: int(v * a)))
                tx, ty = x + img.width / 2, y + img.height / 2
                px, py = ox + (tx - ox) * e, oy + (ty - oy) * e
                _paste(out, piece, int(px - piece.width / 2), int(py - piece.height / 2))

        if draw_disc:
            disc, (cx, cy) = self._scaled_disc(geom, content, accent, ease_out_cubic(openness), press, spin)
            _paste(out, disc, int(cx - geom.box.x - disc.width / 2), int(cy - geom.box.y - disc.height / 2))
        return out

    def _scaled_disc(self, geom, content, accent, eased, press, spin):
        """(image, centre): the record grown by ``eased`` openness, pressed
        in a touch while clicked."""
        cx, cy, d = disc_at(geom, eased)
        d *= 1.0 - 0.05 * press
        source = geom.open_disc if (geom.open_disc and eased > 0.01) else geom.disc
        return self._disc_for(source, content.cover, spin, int(round(d))), (cx, cy)

    def render_vinyl(self, geom: DockGeometry, box: Box, content: DockContent, openness: float,
                     accent=(255, 255, 255), press: float = 0.0, spin: float = 0.0,
                     drop: int = 0) -> Image.Image:
        """Just the record, for its own window (``vinyl_box``), so spinning
        it doesn't repaint the whole dock. ``openness`` is eased; ``drop``
        (px) lowers it out of the corner, for the swap on a new song."""
        out = Image.new("RGBA", (box.w, box.h), (0, 0, 0, 0))
        disc, (cx, cy) = self._scaled_disc(geom, content, accent, openness, press, spin)
        x, y = int(cx - box.x - disc.width / 2), int(cy - box.y - disc.height / 2) + int(drop)
        # Nothing underneath: a clipped plain paste, no blending.
        left, top = max(0, -x), max(0, -y)
        right, bottom = min(disc.width, box.w - x), min(disc.height, box.h - y)
        if right > left and bottom > top:
            out.paste(disc.crop((left, top, right, bottom)), (x + left, y + top))
        return out


def vinyl_box(geom: DockGeometry) -> Box:
    """Screen box of the record (with its shadow), small or grown, clipped
    to the dock window."""
    spots = [(geom.disc_center, geom.disc)]
    if geom.open_disc:
        spots.append((geom.open_center, geom.open_disc))
    x0 = min(c[0] - int(d * 0.8) for c, d in spots)
    y0 = min(c[1] - int(d * 0.8) for c, d in spots)
    x1 = max(c[0] + int(d * 0.8) for c, d in spots)
    y1 = max(c[1] + int(d * 0.8) for c, d in spots)
    x0, y0 = max(geom.box.x, x0), max(geom.box.y, y0)
    x1, y1 = min(geom.box.x + geom.box.w, x1), min(geom.box.y + geom.box.h, y1)
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
