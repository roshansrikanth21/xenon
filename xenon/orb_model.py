"""Pure state and geometry for the minimized Xenon orb.

No Tk, Win32 or Pillow here: the renderer and the window shell read this model
every frame, and the tests drive it directly with synthetic time and pointer
positions.

Coordinates are logical pixels relative to the window's top-left corner; the
shell multiplies by the DPI scale when it draws.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# Window and orb geometry (logical px). The window is larger than the orb so
# the satellites can fan out around it without resizing the window.
WINDOW = 320
CENTER = WINDOW / 2
ORB_RADIUS = 44
SATELLITE_RADIUS = 17
SATELLITE_DISTANCE = 88
# The fan spans this many degrees, centred on the direction of the screen
# centre, so options never open off the edge of the monitor.
FAN_DEGREES = 210
# Hovering this close to the centre opens the fan; once open, the zone grows
# to cover the satellites so moving onto one does not close it.
OPEN_RADIUS = ORB_RADIUS + 4
STAY_OPEN_RADIUS = SATELLITE_DISTANCE + SATELLITE_RADIUS + 10
CLOSE_DELAY_S = 0.35
# After this long untouched (and not busy), the orb eases down to IDLE_SCALE
# and fades a little; any hover or activity grows it back.
IDLE_AFTER_S = 120.0
IDLE_SCALE = 0.55
IDLE_FADE = 0.72
QUIET_STATES = frozenset({"idle", "standby"})
# A task running in notch mode is quiet too: the orb stays folded for the
# whole task and the notch shows "Step 2/5 · ..." instead of unfolding.
# 'confirm' is the opposite: it unfolds and stays open while a card is up.
NOTCH_QUIET_STATES = QUIET_STATES | frozenset({"working"})
# Notch mode: the orb lives at the top centre of the monitor. Idle, it folds
# into a slim pill flush with the screen's top edge; hover it and it drops
# down into the full circle with the options fanning downward.
NOTCH_DROP = ORB_RADIUS + 16      # screen top edge -> orb centre (logical px)
NOTCH_WIDTH = 150                 # resting width; grows with what it shows
NOTCH_MAX_WIDTH = 300
NOTCH_HEIGHT = 30                 # visible height below the screen edge
NOTCH_SHOULDER = 9                # concave curve where it meets the edge
NOTCH_IDLE_S = 3.0
NOTCH_DWELL_S = 0.25              # rest this long on the notch to unfold it
OPEN_SPEED = 5.5   # progress units per second
CLOSE_SPEED = 4.0
STAGGER = 0.07     # each satellite starts this much progress later


@dataclass(frozen=True)
class OrbAction:
    key: str
    label: str
    glyph: str      # Segoe MDL2 / Fluent Icons code point
    fallback: str   # letter used when the icon font is missing


ACTIONS: tuple[OrbAction, ...] = (
    OrbAction("ask", "Ask", "", "A"),
    OrbAction("voice", "Voice", "", "V"),
    OrbAction("screen", "Screen", "", "S"),
    OrbAction("search", "Search", "", "?"),
    OrbAction("panel", "Expand", "", "+"),
    OrbAction("close", "Close", "", "x"),
)

# (core, accent) colours per state. The renderer blends between them so a
# state change glides instead of snapping.
#
# Every state also has its own motion (see ``orb_render``), so it reads at a
# glance even without colour:
#   idle       slow breathing plasma
#   standby    voice on, waiting for the wake word: a slow sonar ping
#   listening  taking a command: fast ripples + a live mic equaliser
#   heard      command caught: a bright flash and a ring that snaps inward
#   thinking   three comets chasing each other around the rim
#   searching  a radar sweep
#   speaking   a waveform across the middle
#   error      red, with a short shake
PALETTE: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "idle": ((56, 189, 248), (167, 139, 250)),
    "standby": ((45, 160, 120), (56, 189, 248)),
    "listening": ((52, 211, 153), (34, 211, 238)),
    "heard": ((125, 211, 252), (52, 211, 153)),
    "thinking": ((251, 191, 36), (244, 114, 182)),
    "searching": ((34, 211, 238), (59, 130, 246)),
    "speaking": ((192, 132, 252), (244, 114, 182)),
    "error": ((239, 68, 68), (249, 115, 22)),
    "working": ((96, 165, 250), (52, 211, 153)),
    "confirm": ((251, 191, 36), (245, 158, 11)),
}
STATE_LABELS = {
    "idle": "XENON",
    "standby": "say xenon",
    "listening": "listening",
    "heard": "got it",
    "thinking": "thinking",
    "searching": "searching",
    "speaking": "speaking",
    "error": "error",
    "working": "working",
    "confirm": "confirm",
}


_STEP_RE = re.compile(r"\bstep\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def format_task_progress(text: str, step: int | None = None, total: int | None = None) -> str:
    """The notch's progress line: "Step 1/2 · Opening Notepad"."""
    text = " ".join(str(text or "").split())
    if step and total and total > 0:
        prefix = f"Step {min(step, total)}/{total}"
        return f"{prefix} · {text}" if text else prefix
    return text


def step_fraction(text: str) -> float | None:
    """0..1 read from "Step 2/5 ...", or None when the line has no step count.

    Parsed from the text (rather than passed alongside it) so the shell only
    has to hand the notch one string, the way it already does.
    """
    match = _STEP_RE.search(text or "")
    if not match:
        return None
    step, total = int(match.group(1)), int(match.group(2))
    if total <= 0:
        return None
    return min(max(step / total, 0.0), 1.0)


def ease_out_back(t: float) -> float:
    """Overshoots slightly past 1, which gives the satellites a soft pop."""
    t = min(max(t, 0.0), 1.0)
    c1 = 1.4
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def ease_in_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_color(
    a: tuple[int, int, int], b: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return tuple(int(round(lerp(x, y, t))) for x, y in zip(a, b))  # type: ignore[return-value]


def fan_direction(
    window_x: float,
    window_y: float,
    screen_w: float,
    screen_h: float,
) -> float:
    """Angle (radians, screen coords) from the orb toward the screen centre."""
    cx = window_x + CENTER
    cy = window_y + CENTER
    dx = screen_w / 2 - cx
    dy = screen_h / 2 - cy
    if math.hypot(dx, dy) < 1:
        return math.pi  # dead centre: open to the left
    return math.atan2(dy, dx)


def satellite_angles(direction: float, count: int = len(ACTIONS)) -> list[float]:
    if count <= 1:
        return [direction]
    span = math.radians(FAN_DEGREES)
    start = direction - span / 2
    step = span / (count - 1)
    return [start + step * index for index in range(count)]


@dataclass
class Satellite:
    action: OrbAction
    angle: float
    x: float = CENTER
    y: float = CENTER
    scale: float = 0.0
    alpha: float = 0.0
    hot: float = 0.0   # 0..1 hover highlight, eased


@dataclass
class OrbModel:
    """Everything the renderer needs for one frame."""

    state: str = "idle"
    detail: str = ""
    progress: float = 0.0          # 0 closed .. 1 fully fanned out
    direction: float = math.pi
    level: float = 0.0             # 0..1 microphone level, decays
    timer_fraction: float | None = None  # nearest timer's time left, 1 -> 0
    voice_on: bool = False
    hovered: str | None = None     # action key under the pointer
    pressed: str | None = None
    time: float = 0.0
    color_from: str = "idle"
    color_mix: float = 1.0         # 1 = fully on ``state``
    satellites: list[Satellite] = field(default_factory=list)
    _open_wanted: bool = False
    _last_inside: float = -1e9
    _suppressed: bool = False
    state_since: float = 0.0
    idle_after: float = IDLE_AFTER_S
    shrink: float = 1.0            # 1 = full size .. IDLE_SCALE when idle
    notch: bool = False
    dock: float = 0.0              # notch mode: 0 = circle .. 1 = folded pill
    # What the folded notch shows: clock, music, timer, voice (set by the app).
    notch_info: dict = field(default_factory=dict)
    notch_width: float = NOTCH_WIDTH
    notch_target_width: float = NOTCH_WIDTH
    _notch_enter: float | None = None
    last_active: float = 0.0
    # Confirm/choice cards on screen. While any is open the orb never folds.
    cards_open: int = 0
    # The running task's progress line ("Step 1/2 · Opening Notepad"). The
    # shell may also pass it as notch_info["task"]; that copy wins.
    task_text: str = ""

    def __post_init__(self) -> None:
        if not self.satellites:
            self.set_direction(self.direction)

    # ----- inputs -------------------------------------------------------

    def set_direction(self, direction: float) -> None:
        self.direction = direction
        angles = satellite_angles(direction)
        if self.satellites:
            for satellite, angle in zip(self.satellites, angles):
                satellite.angle = angle
        else:
            self.satellites = [
                Satellite(action, angle) for action, angle in zip(ACTIONS, angles)
            ]

    def set_state(self, state: str, detail: str = "") -> None:
        if state not in PALETTE:
            state = "error"
        if state != self.state:
            self.color_from = self.state
            self.color_mix = 0.0
            self.state = state
            self.state_since = self.time
            if state not in self.quiet_states:
                # A task starting (or a step finishing) must not unfold a
                # folded notch; every other change is activity.
                self.mark_active()
        self.detail = detail

    def set_task_progress(self, text: str, step: int | None = None, total: int | None = None) -> None:
        """Progress for a task in the 'working' state. Not activity: the
        notch stays folded while the line under it changes."""
        self.task_text = format_task_progress(text, step, total)

    def clear_task(self) -> None:
        self.task_text = ""

    def card_opened(self) -> None:
        """A confirm or choice card is up: unfold and stay unfolded."""
        self.cards_open += 1
        self.mark_active()

    def card_closed(self) -> None:
        """A card went away. The normal idle fold restarts from now, so the
        notch folds NOTCH_IDLE_S later once the task is done too."""
        self.cards_open = max(0, self.cards_open - 1)
        self.mark_active()

    @property
    def progress_text(self) -> str:
        """What the folded notch says while a task runs ("" when none)."""
        info = self.notch_info or {}
        text = info.get("task") or self.task_text
        if not text and self.state == "working":
            text = self.detail
        return str(text or "")

    @property
    def progress_fraction(self) -> float | None:
        return step_fraction(self.progress_text)

    @property
    def quiet_states(self) -> frozenset:
        return NOTCH_QUIET_STATES if self.notch else QUIET_STATES

    @property
    def state_age(self) -> float:
        return self.time - self.state_since

    def set_level(self, level: float) -> None:
        # Rise fast, fall slowly: reads as a live meter rather than flicker.
        level = min(max(level, 0.0), 1.0)
        self.level = max(level, self.level)

    def pointer(self, x: float | None, y: float | None) -> None:
        """Feed the pointer position in window coordinates (None = far away)."""
        if x is None or y is None:
            distance = math.inf
        else:
            distance = math.hypot(x - CENTER, y - CENTER)
        docked = self.notch and self.dock >= 0.3
        if distance <= OPEN_RADIUS + 10 and not docked:
            # Near the orb counts as activity, except while it is folded into
            # the notch: then only resting on the notch itself does.
            self.mark_active()
        on_notch = x is not None and y is not None and self.in_notch(x, y)
        unfold = False
        if on_notch:
            # Rest on the notch briefly to unfold it, so sweeping the cursor
            # across the top of the screen (browser tabs) doesn't pop it open.
            if self._notch_enter is None:
                self._notch_enter = self.time
            if self.time - self._notch_enter >= NOTCH_DWELL_S:
                self.mark_active()
                unfold = True
        else:
            self._notch_enter = None
        if unfold:
            distance = 0.0
        elif docked:
            # Folded: the empty space where the circle would be is not the
            # orb, so passing through it must not open anything.
            distance = math.inf
        limit = STAY_OPEN_RADIUS if self.progress > 0.05 else OPEN_RADIUS
        if self._suppressed:
            # Stay folded until the pointer has left the orb once.
            if distance > STAY_OPEN_RADIUS:
                self._suppressed = False
            self._open_wanted = False
        elif distance <= limit:
            self._open_wanted = True
            self._last_inside = self.time
        elif self.time - self._last_inside > CLOSE_DELAY_S:
            self._open_wanted = False
        self.hovered = self.hit(x, y) if x is not None and y is not None else None

    def hit(self, x: float, y: float) -> str | None:
        """Action key under the point, ``"orb"`` for the orb, or None."""
        if self.progress > 0.6:
            for satellite in self.satellites:
                if satellite.alpha < 0.5:
                    continue
                reach = SATELLITE_RADIUS * max(satellite.scale, 0.6) + 4
                if math.hypot(x - satellite.x, y - satellite.y) <= reach:
                    return satellite.action.key
        if math.hypot(x - CENTER, y - CENTER) <= ORB_RADIUS:
            return "orb"
        return None

    @staticmethod
    def notch_edge() -> float:
        """Window y of the screen's top edge in notch mode."""
        return CENTER - NOTCH_DROP

    def in_notch(self, x: float, y: float) -> bool:
        if not self.notch or self.dock < 0.3:
            return False
        edge = self.notch_edge()
        return abs(x - CENTER) <= self.notch_width / 2 + 6 and y <= edge + NOTCH_HEIGHT + 6

    def mark_active(self) -> None:
        """Something happened: stay (or become) full size."""
        self.last_active = self.time

    @property
    def is_idle(self) -> bool:
        return (
            self.idle_after > 0
            and self.state in self.quiet_states
            and self.cards_open <= 0
            and self.progress <= 0.01
            and self.time - self.last_active >= self.idle_after
        )

    def fold(self) -> None:
        """Close the fan now and keep it shut until the pointer leaves."""
        self._open_wanted = False
        self._suppressed = True

    @property
    def is_open(self) -> bool:
        return self._open_wanted

    # ----- time ---------------------------------------------------------

    def step(self, dt: float) -> None:
        dt = min(max(dt, 0.0), 0.1)
        self.time += dt
        target = 1.0 if self._open_wanted else 0.0
        speed = OPEN_SPEED if target > self.progress else CLOSE_SPEED
        if self.progress < target:
            self.progress = min(target, self.progress + speed * dt)
        else:
            self.progress = max(target, self.progress - speed * dt)
        self.color_mix = min(1.0, self.color_mix + dt * 3.0)
        # Shrink slowly when idle, grow back quickly when needed.
        if self.state not in self.quiet_states or self.cards_open > 0:
            self.mark_active()
        if self.notch:
            # The notch widens or narrows smoothly to fit what it shows.
            self.notch_width += (self.notch_target_width - self.notch_width) * min(1.0, dt * 8)
            # Fold into the notch when idle; unfold fast when needed.
            target = 1.0 if self.is_idle else 0.0
            rate = 2.2 if target > self.dock else 6.0
            if self.dock < target:
                self.dock = min(target, self.dock + rate * dt)
            else:
                self.dock = max(target, self.dock - rate * dt)
            self.shrink = 1.0
        else:
            target = IDLE_SCALE if self.is_idle else 1.0
            rate = 0.6 if target < self.shrink else 5.0
            if self.shrink < target:
                self.shrink = min(target, self.shrink + rate * dt)
            else:
                self.shrink = max(target, self.shrink - rate * dt)
        self.level = max(0.0, self.level - dt * 1.6)

        count = len(self.satellites)
        span = 1 + STAGGER * (count - 1)
        for index, satellite in enumerate(self.satellites):
            # Open in order; close in reverse so the last one out leaves first.
            order = index if self._open_wanted else count - 1 - index
            local = self.progress * span - STAGGER * order
            local = min(max(local, 0.0), 1.0)
            reach = ease_out_back(local) if self._open_wanted else ease_in_out(local)
            distance = lerp(ORB_RADIUS * 0.4, SATELLITE_DISTANCE, reach)
            satellite.x = CENTER + math.cos(satellite.angle) * distance
            satellite.y = CENTER + math.sin(satellite.angle) * distance
            satellite.scale = lerp(0.35, 1.0, ease_in_out(local))
            satellite.alpha = ease_in_out(local)
            hot_target = 1.0 if self.hovered == satellite.action.key else 0.0
            satellite.hot += (hot_target - satellite.hot) * min(1.0, dt * 14)

    # ----- derived ------------------------------------------------------

    def colors(self) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        old_core, old_accent = PALETTE.get(self.color_from, PALETTE["idle"])
        new_core, new_accent = PALETTE[self.state]
        mix = ease_in_out(self.color_mix)
        return lerp_color(old_core, new_core, mix), lerp_color(old_accent, new_accent, mix)

    def center_label(self) -> str:
        if self.hovered and self.hovered != "orb":
            for action in ACTIONS:
                if action.key == self.hovered:
                    if action.key == "voice" and self.voice_on:
                        return "Stop voice"
                    return action.label
        return STATE_LABELS.get(self.state, self.state)

    def spin_speed(self) -> float:
        """Ring rotation speed in radians/second for the current state."""
        return {
            "thinking": 3.2,
            "searching": 1.4,
            "standby": 0.35,
            "listening": 1.1,
            "heard": 2.4,
            "speaking": 1.8,
            "error": 0.4,
        }.get(self.state, 0.55)
