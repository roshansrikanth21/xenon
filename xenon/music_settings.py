"""User settings for the desktop music widget.

Stored as JSON in ``%APPDATA%\\xenon\\music_widget.json``. The settings window
writes the file and the running widget re-reads it when its mtime changes, so
every change shows up live without a restart. Unknown keys are ignored and
bad values fall back to the defaults, so a hand-edited file can never stop
the widget from starting.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace

VISUALIZERS = ("rays", "dots", "wave", "blocks", "none")
DISCS = ("glow", "vinyl", "square", "none")
SIDES = ("right", "left")
LYRIC_STYLES = ("poster", "reveal", "karaoke", "stack", "fade")
LYRIC_CASES = ("upper", "original")
LYRIC_ALIGNS = ("left", "center")
LYRIC_MOTIONS = ("wipe", "fade", "pop")
LINE_MOTIONS = ("slide", "fade", "cut")
GLOW_SHAPES = ("edges", "corners")
LYRIC_SCRIPTS = ("original", "latin")
# Theme presets: name -> glow colour. "album" follows the cover art,
# "custom" uses ``custom_color``.
THEMES = {
    "album": None,
    "white": (255, 255, 255),
    "ice": (170, 225, 255),
    "aurora": (130, 245, 200),
    "sunset": (255, 170, 120),
    "violet": (190, 150, 255),
    "rose": (255, 140, 180),
    "custom": None,
}
ACCENTS = tuple(THEMES)

# (min, max) for numeric fields; anything outside is clamped.
_RANGES = {
    "lyric_size": (0.5, 1.6),
    "unsung_opacity": (0.0, 1.0),
    "glow": (0.0, 2.0),
    "lyric_lift": (0.0, 2.0),
    "lyric_offset": (-2.0, 2.0),
    "lyric_fps": (20, 60),
    "spin_speed": (0.0, 60.0),
    "bounce": (0.0, 2.0),
    "corner_glow": (0.0, 2.0),
    "idle_minutes": (0.0, 120.0),
    "sensitivity": (0.3, 3.0),
    "fps": (10, 60),
    "bar_count": (36, 200),
    "orb_size": (0.6, 1.4),
    "opacity": (0.15, 1.0),
    "ray_length": (0.3, 2.0),
    "smoothing": (0.0, 0.95),
    "backdrop": (0.0, 1.0),
}
_CHOICES = {
    "visualizer": VISUALIZERS,
    "disc": DISCS,
    "side": SIDES,
    "accent": ACCENTS,
    "lyric_style": LYRIC_STYLES,
    "lyric_case": LYRIC_CASES,
    "lyric_align": LYRIC_ALIGNS,
    "lyric_motion": LYRIC_MOTIONS,
    "line_motion": LINE_MOTIONS,
    "glow_shape": GLOW_SHAPES,
    "lyric_script": LYRIC_SCRIPTS,
}


@dataclass(frozen=True)
class MusicSettings:
    # startup / behaviour
    start_with_orb: bool = True
    idle_minutes: float = 5.0       # no music this long -> only the corner record (0 = never)
    pause_when_covered: bool = True
    show_dock: bool = True           # corner disc with controls and recent songs
    # layout
    side: str = "right"              # where the orb sits; lyrics take the other side
    orb_size: float = 1.0
    opacity: float = 1.0             # whole widget
    # orb
    disc: str = "glow"               # glow | vinyl | square | none
    visualizer: str = "rays"         # rays | dots | wave | bars | none
    bar_count: int = 120
    ray_length: float = 1.0
    sensitivity: float = 1.0
    smoothing: float = 0.5           # 0 = twitchy, 0.95 = very slow fall
    spin_speed: float = 0.0          # degrees per second (0: the disc stays still)
    bounce: float = 1.0              # how hard the disc pulses on the beat
    corner_glow: float = 1.0         # the screen edges light up on the bass (0 = off)
    glow_shape: str = "edges"        # edges (all four, full length) | corners
    glow_bands: bool = True          # bottom: bass, sides: mids, top: highs (off: all bass)
    fps: int = 60                    # the ring draws in its own process, so 60 is affordable
    halo: bool = False               # soft light behind the disc (off: looked cheap)
    backdrop: float = 0.0            # dark pool of shade behind the orb
    # lyrics
    show_lyrics: bool = True
    lyric_style: str = "poster"      # poster | reveal | karaoke | stack | fade
    show_upcoming: bool = True
    lyric_size: float = 1.0
    lyric_case: str = "upper"
    lyric_align: str = "left"
    unsung_opacity: float = 0.18
    glow: float = 1.0
    lyric_motion: str = "wipe"       # how a sung word lights: wipe | fade | pop
    line_motion: str = "slide"       # how lines change: slide | fade | cut
    lyric_lift: float = 1.0          # how far a sung word rises
    lyric_offset: float = 0.0        # seconds; + shows the lyrics sooner
    lyric_fps: int = 60              # lyric animation frame rate
    follow_voice: bool = True        # nudge guessed word timing onto the singing it hears
    lyric_script: str = "original"   # original | latin (Indian scripts spelled in English letters)
    lyrics_for_videos: bool = False  # look up lyrics when a video (not a song) is playing
    # card
    show_card: bool = True
    suggestions: bool = True         # dock suggests similar songs (asks Deezer)
    # colour
    accent: str = "album"            # album | white | custom
    custom_color: str = "#8fd3ff"

    def layout_key(self) -> tuple:
        """Fields that move or resize the windows; a change rebuilds them."""
        return (self.side, round(self.orb_size, 3), self.show_dock, self.glow_shape)

    def accent_rgb(self, palette_accent: tuple[int, int, int]) -> tuple[int, int, int]:
        if self.accent == "custom":
            return parse_color(self.custom_color) or (255, 255, 255)
        return THEMES.get(self.accent) or palette_accent


def parse_color(text: str) -> tuple[int, int, int] | None:
    value = (text or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        return None
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


SETTINGS_VERSION = 5

# The look from the reference clip: a big lit album globe, a dense ring of
# long white rays, no halo, poster lyrics. Applied over the current settings
# so behaviour toggles (autostart, dock, side) stay as the user set them.
VIDEO_LOOK = {
    "disc": "glow", "visualizer": "rays", "bar_count": 120, "ray_length": 1.0,
    "orb_size": 1.0, "opacity": 1.0, "accent": "album", "halo": False,
    "backdrop": 0.0, "glow": 0.6, "fps": 60, "spin_speed": 0.0, "bounce": 1.0,
    "sensitivity": 1.0, "smoothing": 0.5,
    "show_lyrics": True, "lyric_style": "poster", "lyric_case": "upper",
    "lyric_align": "left", "lyric_size": 1.0, "show_upcoming": True,
}


def video_look(current: "MusicSettings") -> "MusicSettings":
    return coerce({**asdict(current), **VIDEO_LOOK})


def _migrate(data: dict) -> dict:
    """Old files stored every field, so a changed default never reached
    them. Version 2 moved to the poster lyrics and 30 fps, version 3 turned
    the light halo off: carry those over unless the user had picked
    something other than the old default."""
    version = data.get("version") if isinstance(data.get("version"), int) else 0
    if version < 3:
        data = dict(data)
        if version < 2:
            if data.get("lyric_style") == "reveal":
                data.pop("lyric_style")
            if data.get("fps") == 20:
                data.pop("fps")
        if data.get("halo") is True:
            data.pop("halo")
    if version < 4:
        data = dict(data)
        if data.get("spin_speed") == 9.0:     # v4: the disc stays still and bounces
            data.pop("spin_speed")
    if version < 5:
        data = dict(data)
        if data.get("fps") == 30:             # v5: the ring has its own process; 60 fps
            data.pop("fps")
    return data


def coerce(data: dict) -> MusicSettings:
    """Build settings from untrusted JSON: wrong types and out-of-range
    values fall back to (or are clamped towards) the defaults."""
    base = MusicSettings()
    clean = {}
    for f in fields(MusicSettings):
        if f.name not in data:
            continue
        default = getattr(base, f.name)
        value = data[f.name]
        if isinstance(default, bool):
            if isinstance(value, bool):
                clean[f.name] = value
            continue
        if isinstance(default, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            lo, hi = _RANGES.get(f.name, (value, value))
            value = max(lo, min(hi, value))
            clean[f.name] = int(round(value)) if isinstance(default, int) else float(value)
            continue
        if isinstance(default, str) and isinstance(value, str):
            choices = _CHOICES.get(f.name)
            if choices is not None and value not in choices:
                continue
            if f.name == "custom_color" and parse_color(value) is None:
                continue
            clean[f.name] = value
    return replace(base, **clean)


def settings_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "xenon", "music_widget.json")


def load(path: str | None = None) -> MusicSettings:
    path = path or settings_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return MusicSettings()
    return coerce(_migrate(data)) if isinstance(data, dict) else MusicSettings()


def save(settings: MusicSettings, path: str | None = None) -> None:
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"version": SETTINGS_VERSION, **asdict(settings)}, handle, indent=2)
    os.replace(tmp, path)


class SettingsWatcher:
    """Cheap change detection for the render loop: stat the file at most
    every ``interval`` seconds and reload when its mtime moves."""

    def __init__(self, path: str | None = None, interval: float = 0.5) -> None:
        self.path = path or settings_path()
        self.interval = interval
        self._mtime: float | None = self._stat()
        self._next = 0.0
        self.current = load(self.path)

    def _stat(self) -> float | None:
        try:
            return os.stat(self.path).st_mtime
        except OSError:
            return None

    def poll(self, now: float) -> bool:
        """True when the settings changed since the last poll."""
        if now < self._next:
            return False
        self._next = now + self.interval
        mtime = self._stat()
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        fresh = load(self.path)
        changed = fresh != self.current
        self.current = fresh
        return changed
