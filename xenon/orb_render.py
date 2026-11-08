"""Pillow renderer for the Xenon orb.

Produces one premultiplication-ready RGBA frame per tick from an ``OrbModel``.
Everything expensive (gradients, blurred glows, ring masks, sprites) is built
once per DPI scale; a frame only rotates, tints and composites those layers,
which keeps a 30 fps orb at a few milliseconds of CPU.

The orb itself is composed at 2x and reduced, so arcs and bars are
antialiased without Pillow having antialiased line drawing.
"""
from __future__ import annotations

import math
import os
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from xenon.orb_model import (
    NOTCH_DROP,
    NOTCH_HEIGHT,
    NOTCH_MAX_WIDTH,
    NOTCH_SHOULDER,
    NOTCH_WIDTH,
    ease_in_out,
    IDLE_FADE,
    IDLE_SCALE,
    ACTIONS,
    CENTER,
    ORB_RADIUS,
    SATELLITE_RADIUS,
    WINDOW,
    OrbModel,
    lerp_color,
)

ORB_SS = 2          # supersampling for the per-frame orb
STATIC_SS = 4       # supersampling for one-off masks and sprites
BODY_INNER = (13, 18, 36)
BODY_OUTER = (3, 5, 12)
GLASS = (12, 16, 28)

_FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def _font(names: tuple[str, ...], size: int) -> ImageFont.ImageFont:
    for name in names:
        path = os.path.join(_FONT_DIR, name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _icon_font(size: int) -> tuple[ImageFont.ImageFont, bool]:
    for name in ("SegoeIcons.ttf", "segmdl2.ttf"):
        path = os.path.join(_FONT_DIR, name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size), True
            except OSError:
                continue
    return _font(("segoeuib.ttf",), size), False


def _scale_alpha(mask: Image.Image, factor: float) -> Image.Image:
    factor = min(max(factor, 0.0), 1.0)
    if factor >= 0.999:
        return mask
    return mask.point(_alpha_lut(int(factor * 255)))


@lru_cache(maxsize=256)
def _alpha_lut(factor255: int) -> tuple[int, ...]:
    return tuple(value * factor255 // 255 for value in range(256))


def _tint(mask: Image.Image, color: tuple[int, int, int], factor: float = 1.0) -> Image.Image:
    layer = Image.new("RGBA", mask.size, color + (0,))
    layer.putalpha(_scale_alpha(mask, factor))
    return layer


def _smooth_disc(size: int, radius: float, ss: int = STATIC_SS) -> Image.Image:
    """Antialiased filled circle as an L mask."""
    big = Image.new("L", (size * ss, size * ss), 0)
    c = size * ss / 2
    r = radius * ss
    ImageDraw.Draw(big).ellipse((c - r, c - r, c + r, c + r), fill=255)
    return big.resize((size, size), Image.LANCZOS)


class OrbRenderer:
    def __init__(self, scale: float = 1.0) -> None:
        self.scale = max(scale, 0.5)
        self.size = int(round(WINDOW * self.scale))
        s = self.scale
        self._orb_px = int(round((ORB_RADIUS + 3) * 2 * s)) // 2 * 2
        self._d = self._orb_px * ORB_SS          # orb canvas, supersampled
        self._r = ORB_RADIUS * s * ORB_SS        # orb radius on that canvas
        self._label_font = _font(("segoeuisb.ttf", "segoeui.ttf"), max(8, int(8.5 * s * ORB_SS)))
        self._small_font = _font(("segoeuisb.ttf", "segoeui.ttf"), max(7, int(7 * s * ORB_SS)))
        self._build_static()
        self._build_effects()
        self._build_satellites()
        self._build_notch()

    # ----- one-off assets ----------------------------------------------

    def _build_static(self) -> None:
        d, r = self._d, self._r
        c = d / 2

        # Outer halo around the orb, blurred far beyond the window edge needs.
        halo = Image.new("L", (self.size, self.size), 0)
        hr = ORB_RADIUS * self.scale * 1.02
        hc = self.size / 2
        ImageDraw.Draw(halo).ellipse((hc - hr, hc - hr, hc + hr, hc + hr), fill=255)
        self._halo = halo.filter(ImageFilter.GaussianBlur(16 * self.scale))

        # Dark glass body: radial gradient clipped to a smooth disc.
        gradient = Image.radial_gradient("L").resize((d, d), Image.BICUBIC)
        inner = Image.new("RGB", (d, d), BODY_INNER)
        outer = Image.new("RGB", (d, d), BODY_OUTER)
        # radial_gradient is 0 at the centre and 255 at the corners; stretch it
        # so the edge of the orb, not the corner of the box, is darkest.
        stretched = gradient.point(lambda v: min(255, int(v * 1.42)))
        body = Image.composite(outer, inner, stretched).convert("RGBA")
        self._disc = _smooth_disc(d, r)
        body.putalpha(self._disc)
        self._body = body
        self._inner_disc = _smooth_disc(d, r - 7 * self.scale * ORB_SS)

        # Soft plasma blobs for the core.
        blob_px = int(r * 1.25)
        blob = Image.new("L", (blob_px, blob_px), 0)
        br = blob_px * 0.32
        bc = blob_px / 2
        ImageDraw.Draw(blob).ellipse((bc - br, bc - br, bc + br, bc + br), fill=255)
        self._blob = blob.filter(ImageFilter.GaussianBlur(blob_px * 0.14))

        # Rim light: a thin bright edge, stronger at the top.
        rim = Image.new("L", (d, d), 0)
        draw = ImageDraw.Draw(rim)
        draw.ellipse((c - r, c - r, c + r, c + r), outline=255, width=max(1, int(1.2 * self.scale * ORB_SS)))
        fade = Image.linear_gradient("L").resize((d, d)).point(lambda v: 255 - int(v * 0.75))
        self._rim = ImageChops.multiply(rim, fade)

        # Glass highlight near the top-left.
        highlight = Image.new("L", (d, d), 0)
        hx, hy = c - r * 0.28, c - r * 0.52
        hw, hh = r * 0.62, r * 0.30
        ImageDraw.Draw(highlight).ellipse((hx - hw, hy - hh, hx + hw, hy + hh), fill=60)
        self._highlight = ImageChops.multiply(
            highlight.filter(ImageFilter.GaussianBlur(r * 0.08)), self._disc
        )

        # Ring A: three arcs of different lengths.
        line = max(2, int(1.7 * self.scale * ORB_SS))
        ring_a = Image.new("L", (d, d), 0)
        ra = r - 4.5 * self.scale * ORB_SS
        draw = ImageDraw.Draw(ring_a)
        box = (c - ra, c - ra, c + ra, c + ra)
        for start, length, value in ((0, 78, 255), (110, 36, 170), (180, 120, 230)):
            draw.arc(box, start, start + length, fill=value, width=line)
        self._ring_a = ring_a

        # Ring B: fine ticks, every third one longer.
        ring_b = Image.new("L", (d, d), 0)
        draw = ImageDraw.Draw(ring_b)
        rb = r - 10.5 * self.scale * ORB_SS
        for index in range(72):
            angle = math.radians(index * 5)
            length = (3.2 if index % 3 == 0 else 1.6) * self.scale * ORB_SS
            x0, y0 = c + math.cos(angle) * rb, c + math.sin(angle) * rb
            x1, y1 = c + math.cos(angle) * (rb - length), c + math.sin(angle) * (rb - length)
            draw.line((x0, y0, x1, y1), fill=150 if index % 3 else 220, width=max(1, line // 2))
        self._ring_b = ring_b

    def _build_effects(self) -> None:
        """Masks for the state-specific motion."""
        s = self.scale
        d, r = self._d, self._r
        c = d / 2

        # Sonar ripple: one thin ring, drawn big and resized per frame.
        self._ripple_px = int(WINDOW * s * 0.9) // 2 * 2
        big = self._ripple_px * STATIC_SS
        ring = Image.new("L", (big, big), 0)
        ImageDraw.Draw(ring).ellipse(
            (4, 4, big - 4, big - 4), outline=255, width=int(2.2 * s * STATIC_SS)
        )
        self._ripple = ring.resize((self._ripple_px, self._ripple_px), Image.LANCZOS)

        # Radar sweep: a wedge whose alpha fades behind the leading edge.
        wedge = Image.new("L", (d, d), 0)
        draw = ImageDraw.Draw(wedge)
        steps = 40
        for index in range(steps):
            value = int(200 * (index + 1) / steps)
            draw.pieslice((c - r, c - r, c + r, c + r), -70 + index * 70 / steps, 0, fill=value)
        draw.line((c, c, c + r, c), fill=255, width=max(2, int(1.6 * s * ORB_SS)))
        self._wedge = ImageChops.multiply(wedge, self._inner_disc)

        # Voice badge: a small dot that sits on the rim while the mic is open.
        badge_px = int(14 * s)
        badge = Image.new("L", (badge_px * STATIC_SS,) * 2, 0)
        br = 4.2 * s * STATIC_SS
        bc = badge_px * STATIC_SS / 2
        ImageDraw.Draw(badge).ellipse((bc - br, bc - br, bc + br, bc + br), fill=255)
        self._badge = badge.resize((badge_px, badge_px), Image.LANCZOS)
        glow = Image.new("L", (badge_px, badge_px), 0)
        gr = 5 * s
        gc = badge_px / 2
        ImageDraw.Draw(glow).ellipse((gc - gr, gc - gr, gc + gr, gc + gr), fill=255)
        self._badge_glow = glow.filter(ImageFilter.GaussianBlur(2.2 * s))

    def _build_notch(self) -> None:
        s = self.scale
        self._notch_font = _font(("segoeuisb.ttf", "segoeui.ttf"), max(8, int(11.5 * s)))
        self._notch_small = _font(("segoeui.ttf", "segoeuisb.ttf"), max(7, int(10 * s)))
        self._notch_icon_font, self._notch_has_icons = _icon_font(max(7, int(10 * s)))
        self._notch_shapes: dict[int, Image.Image] = {}

    def _notch_shape(self, width: int) -> Image.Image:
        """Notch mask: flat top flush with the screen edge, rounded bottom,
        concave shoulders where it meets the edge. Drawn at 4x, cached."""
        cached = self._notch_shapes.get(width)
        if cached is not None:
            return cached
        s, ss = self.scale, STATIC_SS
        h = int(NOTCH_HEIGHT * s)
        sh = int(NOTCH_SHOULDER * s)
        W, H, SH = width * ss, h * ss, sh * ss
        big = Image.new("L", (W + 2 * SH, H), 0)
        draw = ImageDraw.Draw(big)
        radius = int(H * 0.5)
        # Body, with its top pushed above the canvas so only the bottom rounds.
        draw.rounded_rectangle((SH, -radius, SH + W - 1, H - 1), radius=radius, fill=255)
        # Shoulders: fill the corner squares, then cut a quarter circle out.
        draw.rectangle((0, 0, SH, SH), fill=255)
        draw.rectangle((SH + W - 1, 0, 2 * SH + W, SH), fill=255)
        draw.ellipse((-SH, 0, SH, 2 * SH), fill=0)
        draw.ellipse((W + SH - 1, 0, W + 3 * SH, 2 * SH), fill=0)
        mask = big.resize(((W + 2 * SH) // ss, H // ss), Image.LANCZOS)
        if len(self._notch_shapes) > 48:
            self._notch_shapes.clear()
        self._notch_shapes[width] = mask
        return mask

    def _notch_layout(self, model: OrbModel):
        """What to show, and how wide the notch must be for it."""
        info = model.notch_info or {}
        s = self.scale
        timer, music = info.get("timer"), info.get("music")
        clock = info.get("clock", "")
        if info.get("flash"):
            main, sub = info["flash"], ""
        elif timer:
            main, sub = timer.get("text", ""), timer.get("name", "")
        elif music:
            main, sub = music.get("title", ""), clock
        else:
            main, sub = clock or "XENON", ""
        measure = ImageDraw.Draw(Image.new("L", (1, 1)))
        main_w = measure.textlength(main, font=self._notch_font)
        sub_w = measure.textlength(sub, font=self._notch_small) if sub else 0
        slot = NOTCH_HEIGHT * s  # icon slot on the left (and right when needed)
        want = slot + min(main_w, (NOTCH_MAX_WIDTH - 90) * s) + (sub_w + 14 * s if sub else 0) + slot * 0.75
        width = min(max(want / s, NOTCH_WIDTH), NOTCH_MAX_WIDTH)
        return info, main, sub, main_w, width

    def _notch_content(self, model: OrbModel, width_px: int, core, accent) -> Image.Image:
        info, main, sub, main_w, _ = self._notch_layout(model)
        s = self.scale
        h = int(NOTCH_HEIGHT * s)
        sh = int(NOTCH_SHOULDER * s)
        canvas = Image.new("RGBA", (width_px + 2 * sh, h), (0, 0, 0, 0))
        mid = h / 2
        left = sh + h * 0.55
        t = model.time
        timer, music = info.get("timer"), info.get("music")
        listening = info.get("voice") and model.state in {"standby", "listening"}

        # ---- left icon -----------------------------------------------------
        icon = Image.new("L", (canvas.width * 2, h * 2), 0)
        draw = ImageDraw.Draw(icon)
        cx, cy, r = left * 2, mid * 2, h * 0.34 * 2 / 2 * 1.0
        if timer:
            fraction = max(0.0, min(1.0, timer.get("fraction", 0)))
            width = max(2, int(2.2 * s * 2))
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=70, width=width)
            draw.arc((cx - r, cy - r, cx + r, cy + r), -90, -90 + 360 * fraction, fill=255, width=width)
            icon_color = (251, 191, 36)
        elif music:
            playing = music.get("playing")
            bar_w = max(2, int(2.4 * s * 2))
            for i in range(4):
                level = 0.35 + 0.65 * abs(math.sin(t * (5.2 + i * 1.3) + i)) if playing else 0.3
                bh = r * 2 * level
                x = cx - r + i * (r * 2 / 3.2)
                draw.rounded_rectangle((x, cy + r - bh, x + bar_w, cy + r), radius=bar_w // 2, fill=255)
            icon_color = accent
        else:
            breath = 0.75 + 0.25 * math.sin(t * 2.0)
            rr = r * 0.62 * breath
            draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=255)
            icon_color = (74, 222, 128) if listening else core
        icon = icon.resize((canvas.width, h), Image.LANCZOS)
        if not timer and not music:
            glow = icon.filter(ImageFilter.GaussianBlur(3 * s))
            canvas.alpha_composite(_tint(glow, icon_color, 0.7))
        canvas.alpha_composite(_tint(icon, icon_color))

        # ---- main text (scrolls when a song title is too long) -------------
        text_left = int(sh + h * 1.05)
        right_room = (self._text_width(sub, self._notch_small) + 16 * s) if sub else h * 0.6
        avail = int(canvas.width - text_left - right_room - sh)
        text = Image.new("L", (max(1, avail), h), 0)
        tdraw = ImageDraw.Draw(text)
        if main_w > avail and music:
            gap = 40 * s
            offset = (t * 28 * s) % (main_w + gap)
            tdraw.text((-offset, mid), main, font=self._notch_font, fill=255, anchor="lm")
            tdraw.text((-offset + main_w + gap, mid), main, font=self._notch_font, fill=255, anchor="lm")
            fade = Image.linear_gradient("L").rotate(90, expand=True).resize((max(1, avail), h))
            edge_fade = ImageChops.multiply(fade, fade.transpose(Image.FLIP_LEFT_RIGHT))
            text = ImageChops.multiply(text, edge_fade.point(lambda v: min(255, v * 4)))
        else:
            tdraw.text((0, mid), main, font=self._notch_font, fill=255, anchor="lm")
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        layer.alpha_composite(_tint(text, (240, 243, 252)), (text_left, 0))
        canvas.alpha_composite(layer)

        # ---- right side: secondary text, or the mic when listening ---------
        if sub:
            label = Image.new("L", canvas.size, 0)
            ImageDraw.Draw(label).text(
                (canvas.width - sh - h * 0.45, mid), sub, font=self._notch_small, fill=255, anchor="rm"
            )
            canvas.alpha_composite(_tint(label, (160, 168, 188)))
        elif listening and self._notch_has_icons:
            mic = Image.new("L", canvas.size, 0)
            ImageDraw.Draw(mic).text(
                (canvas.width - sh - h * 0.5, mid), "\ue720", font=self._notch_icon_font, fill=255, anchor="mm"
            )
            canvas.alpha_composite(_tint(mic, (74, 222, 128), 0.9))
        return canvas

    @staticmethod
    def _text_width(text: str, font) -> float:
        if not text:
            return 0.0
        return ImageDraw.Draw(Image.new("L", (1, 1))).textlength(text, font=font)

    def _draw_notch(self, frame: Image.Image, model: OrbModel, core, accent, alpha: float) -> None:
        s = self.scale
        width_px = max(10, int(model.notch_width * s))
        shape = self._notch_shape(width_px)
        sh = int(NOTCH_SHOULDER * s)
        edge = int(self.size / 2 - NOTCH_DROP * s)  # the screen's top edge
        left = int(self.size / 2 - width_px / 2 - sh)
        info = model.notch_info or {}
        if info.get("music", {}) and info["music"].get("playing"):
            # A soft glow under the notch that breathes while music plays.
            glow = shape.filter(ImageFilter.GaussianBlur(6 * s))
            pulse = 0.25 + 0.15 * abs(math.sin(model.time * 3.1))
            frame.alpha_composite(_tint(glow, accent, pulse * alpha), (left, edge + int(2 * s)))
        body = Image.new("RGBA", shape.size, (0, 0, 0, 0))
        body.putalpha(_scale_alpha(shape, alpha))
        frame.alpha_composite(body, (left, edge))
        # Hairline highlight along the bottom curve.
        rim = ImageChops.subtract(shape, shape.transform(shape.size, Image.AFFINE, (1, 0, 0, 0, 1, int(1.2 * s))))
        frame.alpha_composite(_tint(rim, (255, 255, 255), 0.10 * alpha), (left, edge))
        content = self._notch_content(model, width_px, core, accent)
        content.putalpha(ImageChops.multiply(content.getchannel("A"), _scale_alpha(shape, alpha)))
        frame.alpha_composite(content, (left, edge))

    def _build_satellites(self) -> None:
        s = self.scale
        self._sat_px = int(round((SATELLITE_RADIUS + 8) * 2 * s))
        size = self._sat_px
        big = size * STATIC_SS
        c = big / 2
        r = SATELLITE_RADIUS * s * STATIC_SS
        icon_font, has_icons = _icon_font(int(13 * s * STATIC_SS))
        self._sat_base: dict[str, Image.Image] = {}

        disc = Image.new("L", (big, big), 0)
        ImageDraw.Draw(disc).ellipse((c - r, c - r, c + r, c + r), fill=255)
        disc = disc.resize((size, size), Image.LANCZOS)
        self._sat_disc = disc

        ring = Image.new("L", (big, big), 0)
        ImageDraw.Draw(ring).ellipse(
            (c - r, c - r, c + r, c + r), outline=255, width=int(1.3 * s * STATIC_SS)
        )
        self._sat_ring = ring.resize((size, size), Image.LANCZOS)

        glow = Image.new("L", (size, size), 0)
        gr = SATELLITE_RADIUS * s
        gc = size / 2
        ImageDraw.Draw(glow).ellipse((gc - gr, gc - gr, gc + gr, gc + gr), fill=255)
        self._sat_glow = glow.filter(ImageFilter.GaussianBlur(5 * s))

        for action in ACTIONS:
            glyph = Image.new("L", (big, big), 0)
            text = action.glyph if has_icons else action.fallback
            ImageDraw.Draw(glyph).text((c, c), text, font=icon_font, fill=255, anchor="mm")
            self._sat_base[action.key] = glyph.resize((size, size), Image.LANCZOS)

    # ----- per frame ----------------------------------------------------

    def render(self, model: OrbModel) -> Image.Image:
        core, accent = model.colors()
        frame = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 0))

        breath = 0.5 + 0.5 * math.sin(model.time * 1.6)
        halo_strength = 0.30 + 0.12 * breath + 0.45 * model.level
        if model.state == "thinking":
            halo_strength += 0.12 * (0.5 + 0.5 * math.sin(model.time * 6))
        halo_strength *= 0.35 + 0.65 * (model.shrink - IDLE_SCALE) / (1 - IDLE_SCALE)
        halo_color = lerp_color(core, accent, 0.5 + 0.5 * math.sin(model.time * 0.7))
        if model.state == "heard":
            halo_strength += 0.5 * max(0.0, 1 - model.state_age * 2.5)
        frame.alpha_composite(_tint(self._halo, halo_color, halo_strength))

        self._draw_ripples(frame, model, core)
        self._draw_satellites(frame, model, accent)

        orb = self._render_orb(model, core, accent)
        offset = int(round(self.size / 2 - orb.width / 2))
        shake = 0
        if model.state == "error" and model.state_age < 0.5:
            shake = int(round(math.sin(model.state_age * 60) * 4 * self.scale * (1 - model.state_age * 2)))
        if model.shrink < 0.999:
            # Idle: a smaller, fainter orb in the same spot.
            px = max(8, int(orb.width * model.shrink))
            fade = IDLE_FADE + (1 - IDLE_FADE) * (model.shrink - IDLE_SCALE) / (1 - IDLE_SCALE)
            orb = orb.resize((px, px), Image.LANCZOS)
            orb.putalpha(_scale_alpha(orb.getchannel("A"), fade))
            offset = int(round(self.size / 2 - px / 2))
        frame.alpha_composite(orb, (offset + shake, offset))
        if model.timer_fraction is not None:
            self._draw_timer_ring(frame, model)
        if model.voice_on:
            self._draw_badge(frame, model)
        if model.notch and model.dock > 0.001:
            frame = self._fold_into_notch(frame, model, core, accent)
        return frame

    def _fold_into_notch(self, frame: Image.Image, model: OrbModel, core, accent) -> Image.Image:
        """Liquid morph: a black shape stretches between the circle and the
        notch, the orb fades out early and the notch's content fades in late."""
        _, _, _, _, target = self._notch_layout(model)
        model.notch_target_width = target
        d = ease_in_out(model.dock)
        out = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        if d >= 0.999:
            self._draw_notch(out, model, core, accent, 1.0)
            return out
        s = self.scale
        c = self.size / 2
        r = ORB_RADIUS * s
        edge = c - NOTCH_DROP * s
        w_notch = model.notch_width * s
        h_notch = NOTCH_HEIGHT * s
        # The blob: circle (orb body) -> notch (above the edge, so flush).
        half_w = r + (w_notch / 2 - r) * d
        top = (c - r) + ((edge - h_notch) - (c - r)) * d
        bottom = (c + r) + ((edge + h_notch) - (c + r)) * d
        radius = r + (h_notch * 0.5 - r) * d
        ss = 2
        blob = Image.new("L", (self.size * ss, self.size * ss), 0)
        ImageDraw.Draw(blob).rounded_rectangle(
            ((c - half_w) * ss, top * ss, (c + half_w) * ss, bottom * ss),
            radius=max(1, radius * ss), fill=255,
        )
        blob = blob.reduce(ss)
        out.alpha_composite(_tint(blob, (0, 0, 0), min(1.0, d * 2.2)))
        # A faint accent rim so the moving shape reads as glass, not a hole.
        rim = ImageChops.subtract(blob, blob.filter(ImageFilter.MinFilter(3)))
        out.alpha_composite(_tint(rim, accent, 0.45 * min(1.0, d * 2.2)))
        if d < 0.5:
            fading = frame.copy()
            fading.putalpha(_scale_alpha(fading.getchannel("A"), (1 - d / 0.5) ** 1.3))
            out.alpha_composite(fading)
        if d > 0.75:
            self._draw_notch(out, model, core, accent, (d - 0.75) / 0.25)
        return out

    def _draw_ripples(self, frame: Image.Image, model: OrbModel, color) -> None:
        """Sonar rings: slow pings on standby, fast and bold while listening."""
        if model.state == "standby":
            period, count, strength = 2.6, 1, 0.45
        elif model.state == "listening":
            period, count, strength = 1.1, 3, 0.55 + 0.4 * model.level
        elif model.state == "heard":
            # One ring that snaps inward onto the orb.
            age = model.state_age
            if age > 0.45:
                return
            t = age / 0.45
            self._ripple_at(frame, ORB_RADIUS * (1.9 - 0.9 * t), color, 0.9 * (1 - t))
            return
        else:
            return
        for index in range(count):
            phase = (model.time / period + index / count) % 1.0
            radius = ORB_RADIUS * (1.0 + 0.95 * phase)
            fade = (1 - phase) ** 1.6
            self._ripple_at(frame, radius, color, strength * fade)

    def _ripple_at(self, frame: Image.Image, radius: float, color, strength: float) -> None:
        px = int(radius * 2 * self.scale) // 2 * 2
        if px < 8 or strength <= 0.01:
            return
        px = min(px, self.size)
        ring = self._ripple.resize((px, px), Image.BILINEAR)
        offset = (self.size - px) // 2
        frame.alpha_composite(_tint(ring, color, strength), (offset, offset))

    def _draw_timer_ring(self, frame: Image.Image, model: OrbModel) -> None:
        """An amber arc just outside the orb: the nearest timer's time left."""
        fraction = min(max(model.timer_fraction or 0.0, 0.0), 1.0)
        if fraction <= 0.001:
            return
        size = self.size
        big = Image.new("L", (size * 2, size * 2), 0)  # 2x is enough for a thin arc
        c = size
        radius = (ORB_RADIUS + 7) * self.scale * 2
        box = (c - radius, c - radius, c + radius, c + radius)
        draw = ImageDraw.Draw(big)
        draw.arc(box, 0, 360, fill=50, width=max(2, int(1.4 * self.scale * 2)))
        draw.arc(box, -90, -90 + 360 * fraction, fill=255, width=max(3, int(2.4 * self.scale * 2)))
        mask = big.reduce(2)
        pulse = 0.75 + 0.25 * math.sin(model.time * 2.5) if fraction < 0.1 else 0.9
        frame.alpha_composite(_tint(mask, (251, 191, 36), pulse))

    def _draw_badge(self, frame: Image.Image, model: OrbModel) -> None:
        """Green dot on the rim: the microphone is open."""
        listening = model.state in {"listening", "heard"}
        color = (74, 222, 128) if listening or model.state == "standby" else (148, 163, 184)
        pulse = 0.55 + 0.45 * math.sin(model.time * (6 if listening else 2.2))
        size = self._badge.width
        c = self.size / 2
        angle = math.radians(52)  # lower-right of the rim
        x = int(c + math.cos(angle) * ORB_RADIUS * self.scale - size / 2)
        y = int(c + math.sin(angle) * ORB_RADIUS * self.scale - size / 2)
        frame.alpha_composite(_tint(self._badge_glow, color, 0.6 * pulse), (x, y))
        frame.alpha_composite(_tint(self._badge, (10, 14, 24)), (x, y))
        inner = self._badge.resize((max(2, int(size * 0.72)),) * 2, Image.LANCZOS)
        off = (size - inner.width) // 2
        frame.alpha_composite(_tint(inner, color), (x + off, y + off))

    def _render_orb(self, model: OrbModel, core, accent) -> Image.Image:
        d, r = self._d, self._r
        c = d / 2
        t = model.time
        canvas = self._body.copy()

        # Plasma core: three blobs orbiting at different speeds.
        plasma = Image.new("RGBA", (d, d), (0, 0, 0, 0))
        energy = 0.78 + 0.22 * model.level + (0.12 if model.state != "idle" else 0.0)
        bw = self._blob.width
        blobs = (
            (core, 0.9, 0.0, 0.30, 0.95),
            (accent, -0.7, 2.1, 0.34, 0.85),
            (lerp_color(core, (255, 255, 255), 0.35), 1.3, 4.0, 0.18, 0.55),
        )
        for color, speed, phase, orbit, strength in blobs:
            wobble = 1 + 0.25 * math.sin(t * 1.7 + phase)
            angle = t * speed * (1.6 if model.state == "thinking" else 1.0) + phase
            x = c + math.cos(angle) * r * orbit * wobble - bw / 2
            y = c + math.sin(angle * 1.1) * r * orbit * wobble - bw / 2
            plasma.alpha_composite(
                _tint(self._blob, color, strength * energy), (int(x), int(y))
            )
        plasma.putalpha(ImageChops.multiply(plasma.getchannel("A"), self._inner_disc))
        canvas.alpha_composite(plasma)

        # Counter-rotating rings; speed follows the state.
        spin = model.spin_speed()
        ring_a = self._ring_a.rotate(-math.degrees(t * spin), resample=Image.BICUBIC)
        canvas.alpha_composite(_tint(ring_a, accent, 0.9))
        ring_b = self._ring_b.rotate(math.degrees(t * spin * 0.45), resample=Image.BICUBIC)
        canvas.alpha_composite(_tint(ring_b, core, 0.55))

        state = model.state
        # Mic equaliser while taking a command (or when standby hears sound).
        if state in {"listening", "heard"} or (state == "standby" and model.level > 0.08):
            canvas.alpha_composite(self._equaliser(model, core))
        elif state == "thinking":
            canvas.alpha_composite(self._comets(model, core, accent))
        elif state == "searching":
            sweep = self._wedge.rotate(-math.degrees(model.time * 3.4), resample=Image.BICUBIC)
            canvas.alpha_composite(_tint(sweep, core, 0.75))
        elif state == "speaking":
            canvas.alpha_composite(self._waveform(model, lerp_color(core, (255, 255, 255), 0.3)))
        if state == "heard":
            flash = max(0.0, 1 - model.state_age * 3)
            if flash > 0:
                canvas.alpha_composite(_tint(self._inner_disc, (220, 240, 255), 0.55 * flash))

        canvas.alpha_composite(_tint(self._rim, lerp_color(accent, (255, 255, 255), 0.4), 0.85))
        canvas.alpha_composite(_tint(self._highlight, (255, 255, 255)))
        self._draw_label(canvas, model)
        return canvas.reduce(ORB_SS)

    def _equaliser(self, model: OrbModel, color) -> Image.Image:
        d, r = self._d, self._r
        c = d / 2
        t = model.time
        mask = Image.new("L", (d, d), 0)
        draw = ImageDraw.Draw(mask)
        base = r * 0.28
        bars = 48
        width = max(2, int(1.5 * self.scale * ORB_SS))
        speaking = model.state == "speaking"
        for index in range(bars):
            angle = index / bars * math.tau
            wave = (
                math.sin(t * 7 + index * 0.9)
                + math.sin(t * 4.3 - index * 1.7) * 0.6
                + math.sin(t * 11 + index * 0.37) * 0.4
            )
            amp = 0.15 + 0.85 * (model.level if not speaking else 0.55)
            length = r * (0.04 + 0.22 * amp * (0.5 + 0.5 * wave / 2.0))
            x0, y0 = c + math.cos(angle) * base, c + math.sin(angle) * base
            x1 = c + math.cos(angle) * (base + length)
            y1 = c + math.sin(angle) * (base + length)
            draw.line((x0, y0, x1, y1), fill=230, width=width)
        return _tint(mask, lerp_color(color, (255, 255, 255), 0.25), 0.9)

    def _comets(self, model: OrbModel, core, accent) -> Image.Image:
        """Three comets chasing around the rim, each with a fading tail."""
        d, r = self._d, self._r
        c = d / 2
        t = model.time
        layer = Image.new("RGBA", (d, d), (0, 0, 0, 0))
        orbit = r - 15 * self.scale * ORB_SS
        dot = 2.6 * self.scale * ORB_SS
        for index, color in enumerate((core, accent, lerp_color(core, accent, 0.5))):
            head = t * 4.2 + index * math.tau / 3
            mask = Image.new("L", (d, d), 0)
            draw = ImageDraw.Draw(mask)
            for step in range(14):
                angle = head - step * 0.07
                size = dot * (1 - step / 16)
                x, y = c + math.cos(angle) * orbit, c + math.sin(angle) * orbit
                value = int(255 * (1 - step / 14) ** 1.5)
                draw.ellipse((x - size, y - size, x + size, y + size), fill=value)
            layer.alpha_composite(_tint(mask, color))
        return layer

    def _waveform(self, model: OrbModel, color) -> Image.Image:
        """A voice-like line across the middle of the orb."""
        d, r = self._d, self._r
        c = d / 2
        t = model.time
        mask = Image.new("L", (d, d), 0)
        draw = ImageDraw.Draw(mask)
        width = r * 1.3
        points = []
        for index in range(61):
            u = index / 60
            x = c - width / 2 + u * width
            envelope = math.sin(math.pi * u) ** 1.5
            wave = (
                math.sin(u * 13 + t * 9) * 0.6
                + math.sin(u * 23 - t * 13) * 0.3
                + math.sin(u * 5 + t * 4) * 0.4
            )
            amp = r * 0.20 * (0.75 + 0.25 * math.sin(t * 5.3))
            points.append((x, c - r * 0.06 + wave * amp * envelope))
        draw.line(points, fill=255, width=max(2, int(1.8 * self.scale * ORB_SS)), joint="curve")
        return _tint(mask, color, 0.95)

    def _draw_label(self, canvas: Image.Image, model: OrbModel) -> None:
        text = model.center_label()
        if not text:
            return
        if text == "XENON":
            text = " ".join(text)  # letter-spaced wordmark
        else:
            text = text.lower()
        c = canvas.width / 2
        hovering = model.hovered not in (None, "orb")
        # Busy states draw their motion in the middle, so their label sits low.
        busy = not hovering and model.state in {"thinking", "searching", "speaking", "listening", "heard"}
        y = c + self._r * 0.66 if busy else c
        font = self._small_font if busy else self._label_font
        # ImageDraw replaces alpha instead of blending, which would punch
        # see-through holes in the orb; draw on a mask and composite instead.
        mask = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(mask).text((c, y), text, font=font, fill=255, anchor="mm")
        canvas.alpha_composite(_tint(mask, (240, 244, 255), 0.96 if hovering else 0.78))

    def _draw_satellites(self, frame: Image.Image, model: OrbModel, accent) -> None:
        if model.progress <= 0.001:
            return
        size = self._sat_px
        for satellite in model.satellites:
            if satellite.alpha <= 0.01:
                continue
            key = satellite.action.key
            hot = satellite.hot
            if key == "voice" and model.voice_on:
                hot = max(hot, 0.65)
            if key == "close":
                tint = lerp_color(accent, (248, 113, 113), hot)
            else:
                tint = accent

            sprite = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            if hot > 0.02:
                sprite.alpha_composite(_tint(self._sat_glow, tint, 0.85 * hot))
            fill = lerp_color(GLASS, lerp_color(GLASS, tint, 0.55), hot)
            body = Image.new("RGBA", (size, size), fill + (0,))
            body.putalpha(_scale_alpha(self._sat_disc, 0.94))
            sprite.alpha_composite(body)
            sprite.alpha_composite(_tint(self._sat_ring, tint, 0.55 + 0.45 * hot))
            glyph_color = lerp_color((214, 222, 240), (255, 255, 255), hot)
            sprite.alpha_composite(_tint(self._sat_base[key], glyph_color))

            scale = satellite.scale * (1 + 0.12 * hot)
            if abs(scale - 1) > 0.01:
                px = max(2, int(size * scale))
                sprite = sprite.resize((px, px), Image.BILINEAR)
            if satellite.alpha < 0.99:
                sprite.putalpha(_scale_alpha(sprite.getchannel("A"), satellite.alpha))
            x = int(round(satellite.x * self.scale - sprite.width / 2))
            y = int(round(satellite.y * self.scale - sprite.height / 2))
            frame.alpha_composite(sprite, (max(x, 0), max(y, 0)))


def premultiplied_bgra(image: Image.Image) -> bytes:
    """Bytes in the layout UpdateLayeredWindow expects (premultiplied BGRA)."""
    premultiplied = image.convert("RGBa")
    r, g, b, a = premultiplied.split()
    return Image.merge("RGBa", (b, g, r, a)).tobytes()
