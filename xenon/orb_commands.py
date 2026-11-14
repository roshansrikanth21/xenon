"""Xenon's own controls, understood without a model: modes, the music
widget, its lyric and glow settings, and what Xenon learns about your habits.

"game mode on", "go xtreme", "hide the music widget", "lyrics in english
letters", "make the glow stronger", "stop learning my habits", "what do i
listen to most".

The parser is deliberately strict. It runs on every voice transcript, and a
transcript often carries song lyrics, a video or a TV in the background, so
it only accepts short phrases that name one of these controls explicitly
(game mode, xtreme mode, the music widget or visualizer, lyrics, the glow).
"Play some games" or "stop" are left to the other parsers.

It also runs BEFORE the brightness, fastpath and music parsers, because some
of these phrases look like theirs: brightness claims "make the glow
brighter", fastpath turns "start xtreme" into opening an app called Xtreme,
and music claims "stop". Matching the whole phrase here first keeps each
request with the code that really owns it.

Mode switches change the whole machine (game mode kills apps, xtreme mode
raises power plans), so they only run when the request came straight from
the user, never from speech overheard in the background: see ``allowed``.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Callable

from . import music_settings
from .untrusted import strip_invisible

# --------------------------------------------------------------- parsing --


@dataclass(frozen=True)
class OrbCommand:
    """``kind``: game_on, game_off, xtreme_on, xtreme_off, widget_on,
    widget_off, widget_settings, setting, learn_on, learn_off,
    forget_history or listening_summary. ``field`` and ``value`` are set for
    ``setting`` (a MusicSettings field and its new value, or
    ``("delta", step)`` for a relative change). ``text`` is the normalised
    phrase that matched."""

    kind: str
    field: str = ""
    value: object = None
    text: str = ""


# Switches that must never run from background speech.
MODE_KINDS = frozenset({"game_on", "game_off", "xtreme_on", "xtreme_off"})

# Canonical phrasings, for the router prompt: each one parses here.
CANONICAL = (
    "game mode on",
    "game mode off",
    "xtreme mode on",
    "xtreme mode off",
    "music widget on",
    "music widget off",
    "music widget settings",
    "lyrics in english letters",
    "lyrics in original script",
    "lyrics on",
    "lyrics off",
    "glow stronger",
    "glow weaker",
    "glow off",
    "stop learning my habits",
    "start learning my habits",
    "forget my listening history",
    "what do i listen to most",
)

MAX_WORDS = 12
GLOW_STEP = 0.25

_WAKE_LEAD = re.compile(r"^(?:(?:hey|hi|ok|okay|yo)\s+)?xenon\b\s*")
_WAKE_TAIL = re.compile(r"\s*\b(?:hey\s+)?xenon$")
_POLITE_LEAD = re.compile(
    r"^(?:(?:please|pls|can you|could you|would you|will you|can u|i want you to|i need you to)\s+)+"
)
_POLITE_TAIL = re.compile(r"(?:\s+(?:please|pls|for me|thanks|thank you))+$")
_NEGATORS = {"not", "don't", "dont", "never", "do not", "doesn't", "no"}


def normalise(text: str) -> str:
    """Lower-case, straighten quotes, drop end punctuation, the wake word
    and politeness, so "Hey Xenon, could you turn on game mode please?" and
    "game mode on" compare on the words that matter."""
    phrase = strip_invisible(text or "").lower()
    phrase = phrase.replace("’", "'").replace("‘", "'")
    phrase = phrase.replace("“", '"').replace("”", '"').replace('"', "")
    phrase = re.sub(r"[,;:]", " ", phrase)
    phrase = " ".join(phrase.split()).strip(" .!?")
    # Repeat until stable: "please xenon game mode on" has both in front.
    previous = None
    while previous != phrase:
        previous = phrase
        phrase = _WAKE_LEAD.sub("", phrase)
        phrase = _WAKE_TAIL.sub("", phrase)
        phrase = _POLITE_LEAD.sub("", phrase)
        phrase = _POLITE_TAIL.sub("", phrase)
        phrase = phrase.strip(" .!?")
    return phrase


def _has_negator(phrase: str) -> bool:
    words = phrase.split()
    return any(word in _NEGATORS for word in words) or "do not" in phrase


# Building blocks. Every rule is matched against the WHOLE phrase.
_ON = r"(?:turn on|switch on|start|enable|activate|enter|go into|put on|switch to|go to|launch)"
_OFF = r"(?:turn off|switch off|stop|end|exit|leave|disable|deactivate|quit|get out of|close)"
_GAME = r"(?:the |my )?(?:game|gaming) mode"
_XTREME = r"(?:the |my )?(?:(?:xtreme|extreme) mode|(?:max|maximum|full) performance(?: mode)?)"
_XWORD = r"(?:xtreme|extreme)"
_WIDGET = r"(?:the |my )?(?:music widget|widget|music visuali[sz]er|visuali[sz]er)"
_LYRICS = r"(?:the |my )?lyrics"
_GLOW = r"(?:the |my )?(?:corner |edge |screen )?glow"
_SOME = r"(?:a (?:bit|little|lot) |much |way )?"
_SCRIPT_TAIL = r"(?: letters| script| alphabet| characters| spelling| writing)?"
_ORIGINAL = (
    r"(?:the )?(?:original|native|malayalam|hindi|tamil|telugu|kannada|devanagari)"
    r"(?: script| language| letters| writing| alphabet)?"
)
_SHOW = r"(?:show|put|make|display|switch|change|set|write|give me|i want)"
_VOICE = r"(?:the )?(?:singing|singer|voice|vocals|vocalist)"
_VIDEOS = r"lyrics (?:for|on|with) (?:videos|youtube videos|youtube)"
_FPS60 = r"(?:60|sixty) ?(?:fps|frames per second|frames a second)"
_FPS30 = r"(?:30|thirty) ?(?:fps|frames per second|frames a second)"
_VIS = r"(?:the |my )?(?:music )?visuali[sz]er"
_HABITS = r"(?: my habits| about me| what i (?:like|listen to)| my music| my listening)?"


def _setting(field: str, value: object):
    return lambda phrase: OrbCommand("setting", field, value, phrase)


def _kind(kind: str):
    return lambda phrase: OrbCommand(kind, text=phrase)


# (pattern, builder). Order matters only where two rules could both match;
# the more specific one comes first.
_PC = r"(?:(?:my|the|this) )?(?:pc|computer|laptop|machine|system)"
_SHUT = r"(?:shut ?down|turn off|power off|switch off|power down)"

_RULES_NEGATION_OK: tuple[tuple[str, Callable[[str], OrbCommand]], ...] = (
    # ---- the PC itself (cancel says "don't", so it lives here)
    (r"(?:cancel|abort|stop) (?:the )?(?:shut ?down|restart|reboot)|(?:don't|dont|do not) (?:shut ?down|restart|reboot)(?: .*)?",
     _kind("cancel_shutdown")),
    (rf"(?:don't|dont|do not) learn{_HABITS}(?: anymore| any more)?", _kind("learn_off")),
    (rf"(?:stop|don't|dont|do not) (?:follow|following|track|tracking) {_VOICE}", _setting("follow_voice", False)),
    (rf"(?:stop|don't|dont|do not) (?:look|looking|find|finding|fetch|fetching|get|getting) up {_VIDEOS}",
     _setting("lyrics_for_videos", False)),
    (rf"(?:stop|don't|dont|do not) (?:look up|looking up|find|finding|fetch|fetching|show|showing) {_VIDEOS}",
     _setting("lyrics_for_videos", False)),
    (rf"no {_VIDEOS}", _setting("lyrics_for_videos", False)),
    (r"no (?:corner |edge |screen )?glow", _setting("corner_glow", 0.0)),
    (r"no (?:more )?lyrics", _setting("show_lyrics", False)),
)

_RULES: tuple[tuple[str, Callable[[str], OrbCommand]], ...] = (
    # ---- start listening now (Xenon Holo's open palm / Talk panel)
    (r"(?:start )?listen(?:ing)? now|start listening|talk to me", _kind("listen_now")),
    # ---- media keys (what hand gestures send: they work with any player)
    (r"media (?:play ?pause|toggle)|play ?pause", _kind("media_toggle")),
    (r"media next", _kind("media_next")),
    (r"media (?:previous|prev|back)", _kind("media_prev")),
    # ---- Xenon Holo (hand-controlled panels round the core)
    (r"(?:(?:open|start|show|launch) )?(?:the )?(?:xenon )?holo(?: ?explorer| lab| mode)?|(?:open|show|start) (?:the )?(?:holograms?|hologram panels)",
     _kind("holo_on")),
    # ---- Xenon Core (the 3D presence view)
    (r"(?:(?:open|show|start|launch) )?(?:the |xenon |your )?(?:energy )?core(?: view| mode)?|show (?:me )?yourself",
     _kind("core_on")),
    # ---- hand control (the webcam gesture HUD)
    (r"(?:(?:open|start|show|launch|turn on|enable) )?(?:the )?(?:hand control|hand tracking|gesture control|hud|holograms?)",
     _kind("hud_on")),
    # ---- the PC itself
    (rf"lock {_PC}|lock (?:my |the )?screen|lock (?:it|up)", _kind("lock_pc")),
    (rf"{_SHUT} {_PC}(?: now)?|shut ?down now", _kind("shutdown_pc")),
    (rf"(?:restart|reboot) {_PC}(?: now)?", _kind("restart_pc")),
    # ---- game mode
    (rf"{_GAME} (?:off|stop)", _kind("game_off")),
    (rf"turn {_GAME} off|switch {_GAME} off", _kind("game_off")),
    (rf"{_OFF} {_GAME}", _kind("game_off")),
    (rf"(?:{_ON} )?{_GAME}(?: on)?", _kind("game_on")),
    (rf"turn {_GAME} on|switch {_GAME} on", _kind("game_on")),
    # ---- xtreme mode
    (rf"(?:{_XTREME}|{_XWORD}) off", _kind("xtreme_off")),
    (rf"turn {_XTREME} off|switch {_XTREME} off", _kind("xtreme_off")),
    (rf"{_OFF} (?:{_XTREME}|{_XWORD})", _kind("xtreme_off")),
    (rf"(?:{_ON} )?{_XTREME}(?: on)?", _kind("xtreme_on")),
    (rf"turn {_XTREME} on|switch {_XTREME} on", _kind("xtreme_on")),
    (rf"go {_XWORD}(?: mode)?", _kind("xtreme_on")),
    (rf"(?:start|launch|enable|activate) {_XWORD}|{_XWORD} on", _kind("xtreme_on")),
    # ---- the widget
    (rf"(?:open |show |show me |bring up )?{_WIDGET} settings"
     r"|(?:open |show |show me |bring up )?(?:the |my )?music settings"
     rf"|(?:open |show |show me )?(?:the )?settings for {_WIDGET}", _kind("widget_settings")),
    (rf"{_WIDGET} on|turn {_WIDGET} on|switch {_WIDGET} on", _kind("widget_on")),
    (rf"{_WIDGET} off|turn {_WIDGET} off|switch {_WIDGET} off", _kind("widget_off")),
    (rf"(?:turn on|switch on|show|show me|start|enable|open|bring up|bring back) {_WIDGET}", _kind("widget_on")),
    (rf"(?:turn off|switch off|hide|stop|disable|close|kill|get rid of) {_WIDGET}", _kind("widget_off")),
    # ---- lyric script
    (rf"(?:{_SHOW} )?{_LYRICS} (?:in|to|into) (?:english|latin|roman)(?: letters| script| alphabet| characters| spelling)?"
     rf"|(?:make )?{_LYRICS} readable in english|transliterate {_LYRICS}", _setting("lyric_script", "latin")),
    (rf"(?:{_SHOW} )?{_LYRICS} (?:in|to|into|back to) {_ORIGINAL}", _setting("lyric_script", "original")),
    # ---- lyrics on/off
    (rf"{_LYRICS} on|turn {_LYRICS} on|switch {_LYRICS} on", _setting("show_lyrics", True)),
    (rf"{_LYRICS} off|turn {_LYRICS} off|switch {_LYRICS} off", _setting("show_lyrics", False)),
    (rf"(?:turn on|switch on|show|show me|enable|bring back) {_LYRICS}", _setting("show_lyrics", True)),
    (rf"(?:turn off|switch off|hide|disable|stop showing|get rid of) {_LYRICS}", _setting("show_lyrics", False)),
    # ---- glow bands (before the strength rules: both start with "glow")
    (rf"(?:make )?{_GLOW} (?:only )?(?:follow|follows|react to|reacts to|track|tracks|use|uses) "
     r"(?:only )?(?:the )?bass(?: only)?", _setting("glow_bands", False)),
    (rf"(?:make )?{_GLOW} (?:follow|follows|react to|reacts to|track|tracks|use|uses) "
     r"(?:the )?(?:bass (?:the )?mids? (?:and )?(?:the )?highs?|all (?:the )?(?:bands|frequencies)"
     r"|everything|the whole song)", _setting("glow_bands", True)),
    # ---- glow strength
    (rf"(?:set |turn |put |crank |make )?{_GLOW} (?:to |at |all the way to |up to |all the way )?"
     r"(?:max|maximum|full|100|100%|up all the way)"
     r"|(?:max|maximum|full) (?:corner |edge )?glow", _setting("corner_glow", 2.0)),
    (rf"turn {_GLOW} off|switch {_GLOW} off|{_GLOW} off"
     rf"|(?:turn off|switch off|disable|kill|remove|hide|get rid of) {_GLOW}", _setting("corner_glow", 0.0)),
    (rf"turn {_GLOW} on|switch {_GLOW} on|{_GLOW} on"
     rf"|(?:turn on|switch on|enable|bring back) {_GLOW}", _setting("corner_glow", 1.0)),
    (rf"(?:make )?{_GLOW} {_SOME}(?:stronger|brighter|more|bigger|more intense|harder|louder)"
     rf"|{_SOME}(?:more|stronger|brighter|bigger) glow"
     rf"|(?:increase|raise|boost|turn up|brighten|intensify) {_GLOW}|turn {_GLOW} up", _setting("corner_glow", ("delta", GLOW_STEP))),
    (rf"(?:make )?{_GLOW} {_SOME}(?:weaker|dimmer|less|softer|lower|smaller|less intense|subtler|darker)"
     rf"|{_SOME}(?:less|weaker|softer|dimmer|subtler) glow"
     rf"|(?:dim|decrease|lower|reduce|soften|turn down|tone down) {_GLOW}|turn {_GLOW} down|tone {_GLOW} down",
     _setting("corner_glow", ("delta", -GLOW_STEP))),
    # ---- follow the singing
    (rf"(?:follow|track) {_VOICE}(?: again)?|{_VOICE} following on|(?:turn on|enable) (?:voice following|following {_VOICE})"
     rf"|follow {_VOICE} on", _setting("follow_voice", True)),
    (rf"follow {_VOICE} off|{_VOICE} following off|(?:turn off|disable) (?:voice following|following {_VOICE})",
     _setting("follow_voice", False)),
    # ---- lyrics for videos
    (rf"(?:look up|find|fetch|get|show) {_VIDEOS}(?: too| as well| also)?|{_VIDEOS} on"
     rf"|(?:turn on|enable) {_VIDEOS}", _setting("lyrics_for_videos", True)),
    (rf"{_VIDEOS} off|(?:turn off|disable) {_VIDEOS}", _setting("lyrics_for_videos", False)),
    # ---- visualizer frame rate
    (rf"(?:make )?{_VIS} smoother|smoother {_VIS}|(?:set |run |put )?{_VIS} (?:to |at |on )?{_FPS60}"
     rf"|{_FPS60}(?: for {_VIS}| on {_VIS}| visuali[sz]er)?", _setting("fps", 60)),
    (rf"save power on {_VIS}|(?:set |run |put )?{_VIS} (?:to |at |on )?{_FPS30}"
     rf"|{_FPS30}(?: for {_VIS}| on {_VIS}| visuali[sz]er)?", _setting("fps", 30)),
    # ---- learning
    (rf"(?:stop|pause|turn off|disable) (?:habit )?learning{_HABITS}", _kind("learn_off")),
    (rf"(?:start|resume|restart|turn on|enable|keep) (?:habit )?learning{_HABITS}(?: again)?"
     rf"|(?:you can )?learn my habits(?: again)?", _kind("learn_on")),
    (r"forget (?:all )?(?:my |about my |everything about my )?(?:listening |music )?(?:history|habits)"
     r"|(?:delete|erase|clear|wipe) (?:all )?(?:of )?my (?:listening|music) (?:history|habits)"
     r"|(?:delete|erase|clear|wipe|forget) (?:everything |all (?:of )?)?(?:what |that )?you(?:'ve| have)? learned(?: about me)?"
     r"|forget everything you(?:'ve| have)? learned(?: about me)?", _kind("forget_history")),
    # ---- what do I listen to
    (r"what do i (?:listen to|play) (?:the )?most(?: often)?"
     r"|what(?:'s|s| is) my (?:music )?taste(?: in music)?"
     r"|who do i listen to (?:the )?most(?: often)?"
     r"|(?:what|who) are my (?:top|favou?rite|most played) (?:artists|songs|singers)"
     r"|what do i usually listen to|what kind of music do i (?:like|listen to)(?: most)?",
     _kind("listening_summary")),
)

_COMPILED_NEGATION_OK = tuple((re.compile(p), b) for p, b in _RULES_NEGATION_OK)
_COMPILED = tuple((re.compile(p), b) for p, b in _RULES)


def parse(text: str) -> OrbCommand | None:
    """Return the Xenon control ``text`` asks for, or None.

    Only whole short phrases match, so "turn on game mode" does but "and then
    we turn on game mode and play" (a lyric, a story, a video) does not, and
    "don't turn on game mode" is never read as turning it on.
    """
    phrase = normalise(text)
    if not phrase or len(phrase.split()) > MAX_WORDS:
        return None
    for pattern, build in _COMPILED_NEGATION_OK:
        if pattern.fullmatch(phrase):
            return build(phrase)
    if _has_negator(phrase):
        return None
    for pattern, build in _COMPILED:
        if pattern.fullmatch(phrase):
            return build(phrase)
    return None


# ------------------------------------------------------------ permission --

_DIRECT_ORIGINS = frozenset({"voice", "typed", "model", "remote", ""})
PC_KINDS = frozenset({"lock_pc", "shutdown_pc", "restart_pc", "cancel_shutdown"})
MEDIA_KINDS = frozenset({"media_toggle", "media_next", "media_prev"})
_SENSITIVE = MODE_KINDS | {"forget_history", "lock_pc", "shutdown_pc", "restart_pc"}
SHUTDOWN_DELAY_S = 60


def allowed(cmd: OrbCommand, origin: str) -> bool:
    """Whether ``cmd`` may run for a request that came from ``origin``.

    Mode switches and forgetting the history change the machine or delete
    data, so they need a direct request (voice addressed to Xenon, typing,
    or the router reading such a request). "voice-ambient" (speech caught
    without the wake word) and "voice-focus" (speech heard while the orb was
    listening to something else) never trigger them. "voice-focus" may not
    change anything at all: it is the user talking to someone else.
    """
    origin = (origin or "").strip().lower()
    if origin == "voice-focus":
        return False
    if cmd.kind in _SENSITIVE:
        return origin in _DIRECT_ORIGINS
    return True


# --------------------------------------------------------------- settings --

_DESCRIBE = {
    ("lyric_script", "latin"): "Lyrics now in English letters.",
    ("lyric_script", "original"): "Lyrics now in the original script.",
    ("show_lyrics", True): "Lyrics shown.",
    ("show_lyrics", False): "Lyrics hidden.",
    ("glow_bands", False): "Glow follows the bass only.",
    ("glow_bands", True): "Glow follows the bass, mids and highs.",
    ("follow_voice", True): "Lyrics now follow the singing.",
    ("follow_voice", False): "Lyrics no longer follow the singing.",
    ("lyrics_for_videos", True): "I'll look up lyrics for videos too.",
    ("lyrics_for_videos", False): "No more lyrics for videos.",
}


def _reply(field: str, old: object, new: object, delta: float | None) -> str:
    if field == "corner_glow":
        if new == 0:
            return "Glow off."
        if delta is not None and new == old:
            return f"Glow is already at {'full' if delta > 0 else 'its lowest'} strength."
        return f"Glow strength {new:g}."
    if field == "fps":
        return f"Visualizer at {new} fps."
    return _DESCRIBE.get((field, new), f"{field.replace('_', ' ').capitalize()} set to {new}.")


def apply_setting(cmd: OrbCommand, load=None, save=None) -> str:
    """Change one music widget setting and say what it is now.

    The running widget re-reads its settings file when it changes, so saving
    is enough for the change to show up live. A relative value
    ``("delta", step)`` is added to the current value, and every value goes
    through ``coerce`` so it is clamped to the same range the settings
    window allows. ``load``/``save`` are injectable for tests.
    """
    if cmd.kind != "setting":
        raise ValueError(f"not a setting command: {cmd.kind!r}")
    load = load or music_settings.load
    save = save or music_settings.save
    old = load()
    current = asdict(old)
    if cmd.field not in current:
        raise ValueError(f"unknown music setting: {cmd.field!r}")
    delta = None
    value = cmd.value
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "delta":
        delta = float(value[1])
        value = current[cmd.field] + delta
    fresh = music_settings.coerce({**current, cmd.field: value})
    try:
        save(fresh)
    except OSError:
        return "I couldn't save that setting."
    return _reply(cmd.field, current[cmd.field], getattr(fresh, cmd.field), delta)


# ---------------------------------------------------------------- running --

Handler = Callable[[OrbCommand, str], "str | None"]
_handler: Handler | None = None

_SETTINGS_KINDS = frozenset({"setting"})


def set_handler(fn: Handler | None) -> None:
    """Register the running orb's handler for modes, the widget, learning and
    the summary. The orb owns those (they touch windows and processes), so
    this module only forwards; None unregisters it when the orb exits."""
    global _handler
    _handler = fn


def media_key(kind: str, press=None) -> str:
    """Press the keyboard media key. Unlike the Spotify API this toggles
    whatever player is active (desktop Spotify, the web player in a
    browser, YouTube) and never fails on "already playing"."""
    from xenon import music

    code = {"media_toggle": music.VK_MEDIA_PLAY_PAUSE, "media_next": music.VK_MEDIA_NEXT_TRACK,
            "media_prev": music.VK_MEDIA_PREV_TRACK}[kind]
    ok = (press or music.press_media_key)(code)
    if not ok:
        return "I couldn't press the media key."
    return {"media_toggle": "Play / pause.", "media_next": "Next.", "media_prev": "Previous."}[kind]


def pc_action(kind: str, runner=None, lock=None) -> str:
    """Lock, shut down, restart, or cancel. Shut down and restart wait
    ``SHUTDOWN_DELAY_S`` with Windows' own warning on screen and never
    force apps closed (no /f), so unsaved work can still ask to be saved
    and "cancel shutdown" stops it."""
    import subprocess

    run_ = runner or (lambda args: subprocess.run(args, capture_output=True, text=True, timeout=15,
                                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    if kind == "lock_pc":
        try:
            (lock or (lambda: __import__("ctypes").windll.user32.LockWorkStation()))()
        except Exception:
            return "I couldn't lock it."
        return "Locked."
    if kind == "cancel_shutdown":
        result = run_(["shutdown", "/a"])
        ok = getattr(result, "returncode", 0) == 0
        return "Cancelled. It's staying on." if ok else "There was nothing to cancel."
    if kind in {"shutdown_pc", "restart_pc"}:
        verb = "Shutting down" if kind == "shutdown_pc" else "Restarting"
        flag = "/s" if kind == "shutdown_pc" else "/r"
        result = run_(["shutdown", flag, "/t", str(SHUTDOWN_DELAY_S), "/c",
                       f"Xenon: {verb.lower()} in {SHUTDOWN_DELAY_S} seconds. Say or tap cancel shutdown to stop."])
        if getattr(result, "returncode", 0) != 0:
            return "Windows wouldn't schedule that (one may already be pending: say cancel shutdown first)."
        return f"{verb} in {SHUTDOWN_DELAY_S} seconds. Say cancel shutdown to stop it."
    return ""


def run(cmd: OrbCommand, origin: str = "") -> str:
    """Carry out ``cmd`` and return a short spoken reply."""
    if not allowed(cmd, origin):
        if cmd.kind in MODE_KINDS:
            return "I only switch modes when you ask me directly."
        if cmd.kind in PC_KINDS:
            return "I only lock or shut down the PC when you ask me directly."
        return "I only change that when you ask me directly."
    if cmd.kind in _SETTINGS_KINDS:
        return apply_setting(cmd)
    if cmd.kind in PC_KINDS:
        return pc_action(cmd.kind)
    if cmd.kind in MEDIA_KINDS:
        return media_key(cmd.kind)
    handler = _handler
    if handler is None:
        return "That needs the orb running."
    try:
        reply = handler(cmd, origin)
    except Exception:
        return "That didn't work."
    return reply or ""
