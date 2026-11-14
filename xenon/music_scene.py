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
    # The layer reaches past the text by lyric_pad on each side (room for
    # the shadow/glow); the words themselves stay where they were.
    pad = lyric_pad(height)
    lyric_box = Box(max(0, lx - pad), int(height * 0.16), lw + pad * 2, int(height * 0.50))

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


@dataclass(frozen=True)
class LyricState:
    """Like ``LyricFrame`` but continuous (levels and entry 0..1), for the
    smooth animator."""
    text: str
    levels: tuple
    entry: float
    upcoming: str
    previous: str = ""
    index: int = -1


def lyric_state(lines: list[tuple[int, str]], position_ms: int, duration_ms: int = 0) -> LyricState | None:
    index = line_at(lines, position_ms)
    if index < 0:
        if lines and lines[0][0] - position_ms < 2500:
            return LyricState("", (), 0.0, lines[0][1].strip(), "", -1)
        return None
    start, text = lines[index]
    text = text.strip()
    end = lines[index + 1][0] if index + 1 < len(lines) else (duration_ms or start + 5000)
    upcoming = lines[index + 1][1].strip() if index + 1 < len(lines) else ""
    previous = lines[index - 1][1].strip() if index > 0 else ""
    if not text:
        return LyricState("", (), 1.0, upcoming, previous, index)
    words = text.split()
    levels = tuple(max(0.0, min(1.0, (position_ms - ws) / WORD_FADE_MS))
                   for ws in word_starts(start, end, words))
    entry = max(0.0, min(1.0, (position_ms - start) / LINE_ENTRY_MS))
    return LyricState(text, levels, entry, upcoming, previous, index)


def _smooth(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


class LyricClock:
    """A steady playback clock for the lyrics.

    The media session is polled about once a second and each reading can
    land a little ahead of or behind the running estimate; following it
    raw made words flicker back and lines stutter. This clock runs on its
    own and folds small errors in over about half a second (never stepping
    backwards while playing); a big jump (a seek, a new song) snaps."""

    SNAP_MS = 700

    def __init__(self) -> None:
        self._pos: float | None = None
        self._t = 0.0
        self._key = None
        self._playing = False

    def now(self, reported_ms: int, playing: bool, key=None, t: float | None = None) -> int:
        import time as _time

        t = _time.monotonic() if t is None else t
        if self._pos is None or key != self._key or playing != self._playing:
            pos = float(reported_ms)
        else:
            dt = max(0.0, t - self._t)
            pos = self._pos + dt * 1000.0 if playing else self._pos
            err = reported_ms - pos
            if abs(err) > self.SNAP_MS:
                pos = float(reported_ms)
            else:
                pos += err * min(1.0, dt * 2.0)
                if playing:
                    pos = max(pos, self._pos)
        self._pos, self._t, self._key, self._playing = pos, t, key, playing
        return int(pos)


LINE_EXIT_MS = 260
LINE_ENTRY_DELAY_MS = 80


def word_progress(start: int, end: int, words: list[str], position_ms: int, motion: str = "wipe") -> list[float]:
    """0..1 per word: how far its highlight has got. A wipe takes as long
    as the word is sung (kept between 0.14 and 1.1 s); fade and pop take a
    fixed short time from the word's start."""
    starts = word_starts(start, end, words)
    if not starts:
        return []
    span = max(400, min(end - start, 8000))
    ends = starts[1:] + [int(start + span * 0.82)]
    out = []
    for ws, we in zip(starts, ends):
        if motion == "wipe":
            length = max(140, min(1100, we - ws))
        else:
            length = WORD_FADE_MS if motion == "fade" else 260
        out.append(max(0.0, min(1.0, (position_ms - ws) / length)))
    return out


def _paste_clipped(out: Image.Image, img: Image.Image, x: int, y: int) -> None:
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(img.width, out.width - x), min(img.height, out.height - y)
    if right <= left or bottom <= top:
        return
    if (left, top, right, bottom) != (0, 0, img.width, img.height):
        img = img.crop((left, top, right, bottom))
    out.alpha_composite(img, (x + left, y + top))


def _scale_alpha(img: Image.Image, k: float) -> Image.Image:
    if k >= 0.999:
        return img
    img = img.copy()
    img.putalpha(img.getchannel("A").point(lambda v, k=k: int(v * k)))
    return img


class LyricAnimator:
    """Smooth lyric frames for little work per frame.

    Each line is drawn twice (every word unsung, every word lit, shadow
    and glow included) and cut into one sprite pair per word, kept for the
    line. A frame stacks the sprites: unsung words faint, sung ones lit and
    lifted a touch, and the word being sung highlighted by a soft-edged
    sweep that crosses it in the time it is sung (or a fade / pop). A new
    line slides up into place as the old one drifts up and out. The next
    line is drawn ahead on a background thread, so a line change never
    stalls a frame."""

    STEPS = 48           # visible steps per word highlight (quantises redraws)

    def __init__(self) -> None:
        import threading

        self._cache: dict = {}
        self._lock = threading.Lock()
        self._wanted = None
        self._wake = threading.Event()
        self._thread = None

    # -- per-line parts, cached ------------------------------------------------
    def _build(self, key):
        text, upcoming, previous, box, accent, screen_height, settings = key
        n = len(text.split())
        regions: list = []
        faint = render_lyrics(box, LyricFrame(text, (0,) * n, REVEAL_STEPS, upcoming, previous),
                              accent, screen_height, settings, regions)
        lit = render_lyrics(box, LyricFrame(text, (REVEAL_STEPS,) * n, REVEAL_STEPS, upcoming, previous),
                            accent, screen_height, settings)
        size = regions[0][4] if regions else int(screen_height / 8.5 * _settings(settings).lyric_size)
        # Everything below works on the line's own bounding box (plus room
        # for the lift), not the whole layer: full-size copies were most of
        # a frame's cost.
        boxes = [b for b in (faint.getbbox(), lit.getbbox()) if b]
        if boxes:
            m = int(size * 0.06 * 2) + 4
            bb = (max(0, min(b[0] for b in boxes)), max(0, min(b[1] for b in boxes) - m),
                  min(faint.width, max(b[2] for b in boxes)), min(faint.height, max(b[3] for b in boxes) + 2))
        else:
            bb = (0, 0, 1, 1)
        empty = faint.crop(bb)
        sprites = []
        words_lit = Image.new("RGBA", empty.size, (0, 0, 0, 0))
        for r in regions:
            rect = r[:4]
            sx, sy = rect[0] - bb[0], rect[1] - bb[1]
            lit_word = lit.crop(rect)
            sprites.append((faint.crop(rect), lit_word, sx, sy))
            empty.paste((0, 0, 0, 0), (sx, sy, sx + rect[2] - rect[0], sy + rect[3] - rect[1]))
            words_lit.alpha_composite(lit_word, (sx, sy))
        return {"empty": empty, "sprites": sprites, "size": size, "words_lit": words_lit,
                "origin": bb[:2], "full": faint.size}

    def _get(self, key):
        with self._lock:
            hit = self._cache.get(key)
        if hit is None:
            hit = self._build(key)
            self._store(key, hit)
        return hit

    def _store(self, key, value) -> None:
        with self._lock:
            self._cache[key] = value
            self._cache[key] = self._cache.pop(key)      # newest last
            while len(self._cache) > 5:
                self._cache.pop(next(iter(self._cache)))

    def _prefetch(self, key) -> None:
        import threading

        with self._lock:
            if key in self._cache:
                return
        self._wanted = key
        self._wake.set()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True, name="xenon-lyric-prefetch")
            self._thread.start()

    def _worker(self) -> None:
        while self._wake.wait(30):
            self._wake.clear()
            key = self._wanted
            if key is None:
                continue
            with self._lock:
                if key in self._cache:
                    continue
            try:
                self._store(key, self._build(key))
            except Exception:
                pass

    @staticmethod
    def _line(lines, index):
        start, text = lines[index]
        nxt = lines[index + 1] if index + 1 < len(lines) else None
        prev = lines[index - 1][1].strip() if index > 0 else ""
        return start, text.strip(), (nxt[1].strip() if nxt else ""), prev

    # -- one word ----------------------------------------------------------------
    def _word(self, faint, lit, q: float, motion: str, size: int):
        """The word's picture at highlight ``q`` and how far it lifts."""
        if q <= 0.0:
            return faint, 0.0
        if q >= 1.0:
            return lit, 1.0
        w, h = faint.size
        if motion == "wipe":
            soft = max(6, int(size * 0.5))
            front = q * (w + soft)
            row = bytes(max(0, min(255, int((front - x) * 255 / soft))) for x in range(w))
            mask = Image.frombytes("L", (w, 1), row).resize((w, h), Image.NEAREST)
            patch = faint.copy()
            patch.paste(lit, (0, 0), mask)
            return patch, _ease_out(q)
        k = _smooth(q)
        patch = Image.blend(faint, lit, k)
        if motion == "pop":
            grow = 1.0 + 0.10 * math.sin(math.pi * q)
            if grow > 1.004:
                big = patch.resize((max(1, int(w * grow)), max(1, int(h * grow))), Image.BILINEAR)
                patch = big.crop(((big.width - w) // 2, (big.height - h) // 2,
                                  (big.width - w) // 2 + w, (big.height - h) // 2 + h))
        return patch, k

    # -- a frame -----------------------------------------------------------------
    def frame(self, lines, position_ms: int, duration_ms: int, box: Box, accent=(255, 255, 255),
              screen_height: int = 1440, settings=None):
        """(key, image): ``key`` changes only when the picture would."""
        st = _settings(settings)
        accent = tuple(accent)
        motion = getattr(st, "lyric_motion", "wipe")
        line_motion = getattr(st, "line_motion", "slide")
        lift_k = getattr(st, "lyric_lift", 1.0)
        style = st.lyric_style
        if style == "fade":
            motion = "all"
        index = line_at(lines, position_ms)
        if index < 0:
            if not (lines and lines[0][0] - position_ms < 2500):
                return None, None
            key = ("pre", lines[0][1].strip(), box, accent, screen_height, st)
            parts = self._get(("", lines[0][1].strip(), "", box, accent, screen_height, st))
            out = Image.new("RGBA", parts["full"], (0, 0, 0, 0))
            out.paste(parts["empty"], parts["origin"])
            return key, out

        start, text, upcoming, previous = self._line(lines, index)
        end = lines[index + 1][0] if index + 1 < len(lines) else (duration_ms or start + 5000)
        static = (box, accent, screen_height, st)
        parts = self._get((text, upcoming, previous) + static)
        if index + 1 < len(lines):
            ns, nt, nu, np_ = self._line(lines, index + 1)
            if nt:
                self._prefetch((nt, nu, np_) + static)

        words = text.split()
        if motion == "all":
            progress = [1.0] * len(words)
        else:
            progress = word_progress(start, end, words, position_ms, motion)
        q = tuple(round(v * self.STEPS) / self.STEPS for v in progress)
        since = position_ms - start
        entry = 1.0 if (line_motion == "cut" or style == "karaoke") else \
            round(max(0.0, min(1.0, (since - LINE_ENTRY_DELAY_MS) / LINE_ENTRY_MS)) * self.STEPS) / self.STEPS
        exit_t = 1.0
        if line_motion != "cut" and index > 0 and since < LINE_EXIT_MS:
            exit_t = round(max(0.0, since / LINE_EXIT_MS) * self.STEPS) / self.STEPS
        key = (index, text, q, entry, exit_t, motion, line_motion, round(lift_k, 2)) + static

        size = parts["size"]
        line = parts["empty"].copy()
        lift_px = size * 0.06 * lift_k
        for (faint, lit, x, y), v in zip(parts["sprites"], q):
            img, k = self._word(faint, lit, v, motion, size)
            _paste_clipped(line, img, x, int(round(y - lift_px * k)))

        out = Image.new("RGBA", parts["full"], (0, 0, 0, 0))
        under = False
        if exit_t < 1.0:
            ps, pt, pu, pp = self._line(lines, index - 1)
            if pt:
                old = self._get((pt, pu, pp) + static)
                t = _ease_out(exit_t)
                drift = int(t * old["size"] * 1.1) if line_motion == "slide" else 0
                ox, oy = old["origin"]
                _paste_clipped(out, _scale_alpha(old["words_lit"], (1.0 - t) ** 2.5), ox, oy - drift)
                under = True
        bx, by = parts["origin"]
        if entry < 1.0:
            e = _ease_out(entry)
            rise = int((1 - e) * size * 0.55) if line_motion == "slide" else 0
            line, by = _scale_alpha(line, e), by + rise
        if under:
            _paste_clipped(out, line, bx, by)
        else:
            # Nothing underneath: a plain paste (no blend, no temporary copies).
            if by + line.height > out.height:
                line = line.crop((0, 0, line.width, max(1, out.height - by)))
            out.paste(line, (bx, by))
        return key, out


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
    small = mask.reduce(4) if mask.width >= 8 and mask.height >= 8 else mask
    small = small.filter(ImageFilter.GaussianBlur(max(2, radius)))
    # gain on the small image (16x fewer pixels), then scale up once
    small = small.point(lambda v: min(255, int(v * gain)))
    return small.resize(mask.size, Image.BILINEAR)


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


def lyric_pad(screen_height: int) -> int:
    return max(12, int(screen_height * 0.03))


def render_lyrics(box: Box, frame: LyricFrame | None, accent=(255, 255, 255), screen_height: int = 1440,
                  settings=None, regions: list | None = None) -> Image.Image:
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
    # Breathing room inside the layer so the shadow and glow round the
    # first letters aren't sliced off at the window edge.
    pad = lyric_pad(screen_height)
    max_w = int(box.w - pad * 2)
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
        px = (box.w - pw) / 2 if centered else pad
        py = max(0, top - rise - int(pf.size * 1.5))
        gdraw.text((px, py), prev, font=pf, fill=(255, 255, 255, int(110 * entry_alpha + 40)))

    for r, row in enumerate(rows):
        row_w = _row_width(words, word_fonts, row, draw)
        x = (box.w - row_w) / 2 if centered else float(pad)
        y = top + r * line_h
        for i in row:
            word = words[i]
            wf = word_fonts[i]
            wy = y + (small_dy if wf is not fnt else 0)
            if regions is not None:
                # Where this word (with a little of its glow) lives, for
                # LyricAnimator to cross-fade it on its own.
                gap = draw.textlength(" ", font=wf) / 2
                ww = draw.textlength(word, font=wf)
                m = int(size * 0.12)
                regions.append((max(0, int(x - gap)), max(0, int(y - m)),
                                min(box.w, int(x + ww + gap + 1)), min(box.h, int(y + line_h + m)), size))
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
    # Work only round the words (plus room for the blur): the layer is
    # mostly empty, and full-size composites were most of the cost.
    bbox = glyphs.getbbox()
    if bbox:
        m = int(size * 0.35)
        crop = (max(0, bbox[0] - m), max(0, bbox[1] - m), min(image.width, bbox[2] + m),
                min(image.height, bbox[3] + m))
        g = glyphs.crop(crop)
        out = Image.new("RGBA", g.size, (0, 0, 0, 0))
        out.putalpha(_blur_quarter(g.getchannel("A"), size / 30, 0.55))
        if st.glow > 0:
            glow = Image.new("RGBA", g.size, accent + (0,))
            # A faint, tight sheen on the sung words, not a fog round them.
            glow.putalpha(_blur_quarter(lit.crop(crop), size / 48, 0.3 * st.glow))
            out.alpha_composite(glow)
        out.alpha_composite(g)
        image.paste(out, crop[:2])

    if frame.upcoming and st.show_upcoming:
        up = _case(frame.upcoming, st)
        ufnt = font(up, max(22, size // 3), "bold")
        uy = min(box.h - ufnt.size - 6, top - rise + block_h + int(size * 0.28))
        ux = (box.w - draw.textlength(up, font=ufnt)) / 2 if centered else pad + 2
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
        else:  # rays: a thin bar with a rounded tip (drawn supersampled)
            hw = max(1.0, math.pi * d / count * 0.30) / 2
            x0, y0 = cx + ca * inner, cy + sa * inner
            x1, y1 = cx + ca * length, cy + sa * length
            px, py = -sa * hw, ca * hw
            draw.polygon([(x0 + px, y0 + py), (x1 + px, y1 + py), (x1 - px, y1 - py), (x0 - px, y0 - py)],
                         fill=level)
            draw.ellipse((x1 - hw, y1 - hw, x1 + hw, y1 + hw), fill=level)


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
    """The rays' colour from the album's main colour: same hue, lifted to a
    bright, glowing tone (pure white when the art is grey or black-and-white)."""
    import colorsys

    if not palette:
        return (255, 255, 255)
    r, g, b = palette[0]
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if s < 0.18 or v < 0.08:
        return (255, 255, 255)
    s = max(0.45, min(0.75, s))
    v = 1.0
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


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
    if st.visualizer != "none":
        values = ring_values(bars, st.bar_count)
        inner = d / 2 + d * 0.05
        reach = (screen_height * 0.15 * st.ray_length * (1.0 if playing else 0.35)
                 * (1.0 + 0.15 * min(1.0, energy)) * max(0.0, intro))
        # Drawn at twice the size and averaged down: PIL draws no
        # antialiasing, so 1x rays had stair-stepped edges.
        k = RAY_SUPERSAMPLE
        big = Image.new("L", (box.w * k, box.h * k), 0)
        _draw_visualizer(ImageDraw.Draw(big), st.visualizer, values, cx * k, cy * k, inner * k, reach * k, d * k)
        shapes = big.reduce(k) if k > 1 else big
    else:
        shapes = Image.new("L", (box.w, box.h), 0)
    alpha = shapes
    if st.glow > 0:
        # A faint, tight sheen right on the rays; the rays stay crisp.
        bleed = _blur_quarter(shapes, d / 200, 0.4 * st.glow)
        alpha = ImageChops.lighter(bleed, shapes)
    if intro < 0.999:
        k = max(0.0, min(1.0, intro))
        alpha = alpha.point(lambda v: int(v * k))
    # One solid-colour layer per size/colour, reused every frame: swap its
    # alpha in and let PIL's "BGRa" packer premultiply in C (one pass,
    # instead of three per-channel lookups and a merge).
    key = (box.w, box.h, tuple(accent))
    layer = _RING_LAYER.get(key)
    if layer is None:
        _RING_LAYER.clear()
        layer = _RING_LAYER[key] = Image.new("RGBA", (box.w, box.h), tuple(accent) + (0,))
    layer.putalpha(alpha)
    return layer.tobytes("raw", "BGRa")


_RING_LAYER: dict = {}
RAY_SUPERSAMPLE = 2


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
