"""Approval-gated desktop actions and the observe -> plan -> act -> verify runner.

The policy is deliberately boring: read-only actions run, side effects need an
explicit approval event, and shell/destructive actions are refused outright.
The runner reports what actually happened, including "unverified" when no
verification succeeded.

Risk is decided here, in code, by ``classify``. Models only propose: a
model's own risk flag can raise the class (``model_hint``), never lower it.
"""
from __future__ import annotations

import inspect
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence

from xenon.untrusted import (
    exact_span,
    has_bidi_reordering,
    has_tag_characters,
    skeleton,
    strip_invisible,
)


class Risk(str, Enum):
    """Risk class decided in code. A model's flags can add confirmations,
    never remove them. Order matters: higher is stricter."""

    READ = "read"
    LOCAL = "local"
    OUTWARD = "outward"
    DESTRUCTIVE = "destructive"
    DENIED = "denied"

    @property
    def rank(self) -> int:
        return _RISK_ORDER.index(self)

    def stricter(self, other: "Risk") -> "Risk":
        return self if self.rank >= other.rank else other


_RISK_ORDER = [Risk.READ, Risk.LOCAL, Risk.OUTWARD, Risk.DESTRUCTIVE, Risk.DENIED]

# App classes, decided from the process image (and URL for browsers).
APP_COMMS = "comms"
APP_BROWSER = "browser"
APP_SHELL = "shell"
APP_SETTINGS = "settings"
APP_OTHER = "other"
APP_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActionContext:
    """What the classifier knows about where an action lands.

    Unknown fields resolve to the stricter answer, never the looser one.
    """

    process_image: str = ""          # e.g. "WhatsApp.Root.exe" (basename, any case)
    window_title: str = ""
    app_class: str = APP_UNKNOWN     # one of the APP_* constants
    focused_role: str = ""           # UIA role of the focused element: "edit", "document", "button", ...
    element_name: str = ""           # accessible name of the target element, if any
    element_role: str = ""
    url: str = ""                    # browser omnibox URL, "" when unknown
    ui_language_english: bool = True
    tainted: bool = False            # task has read untrusted content
    user_words: str = ""             # what Roshan actually said / typed
    allowlist: frozenset = field(default_factory=frozenset)  # app images the task may touch
    origin: str = "typed"            # "typed" | "voice" | "cli" | "ipc" | "model"

# The legacy planner offers exactly READ_ONLY_KINDS | SIDE_EFFECT_KINDS to the
# model, so these two sets must not grow: the new kinds below live in their
# own sets and can never become plannable by accident.
READ_ONLY_KINDS = frozenset({"observe", "wait"})
SIDE_EFFECT_KINDS = frozenset(
    {
        "open_app",
        "focus_window",
        "type_text",
        "press_keys",
        "click_control",
        "write_file",
    }
)
DENIED_KINDS = frozenset(
    {
        "run_command",
        "run_shell",
        "delete_file",
        "format_disk",
        "http_request",
        # URI handlers are not exposed anywhere (egress inventory).
        "open_url",
        "open_uri",
    }
)

# Kinds that commit something to another person, or destroy something. Only
# gate.py executes them, and only with an approval token.
OUTWARD_KINDS = frozenset({"send_email", "send_message", "desktop_commit"})
DESTRUCTIVE_KINDS = frozenset({"desktop_destroy"})

# Reads used by the task agents (desktop loop ops and tools).
AGENT_READ_KINDS = frozenset(
    {
        "read",
        "screen_read",
        "desktop_observe",
        "mail_read_thread",
        "whatsapp_read_chat",
        "memory_search",
        "ask_user",
        "done",
        "fail",
    }
)
# READ while untainted; once a task has read untrusted text the query itself
# may carry someone else's words out, so it needs a card.
EGRESS_READ_KINDS = frozenset({"web_search", "search"})
PLAY_KINDS = frozenset({"play"})

# Surface-touching kinds, legacy and desktop-agent ops, by what they do.
ACTIVATE_KINDS = frozenset(
    {"click_control", "click", "invoke", "select", "toggle", "check", "uncheck", "focus"}
)
NAV_KINDS = frozenset({"scroll", "scroll_to", "expand", "collapse"})
TEXT_KINDS = frozenset({"type_text", "set_text"})
KEY_KINDS = frozenset({"press_keys", "press"})
APP_KINDS = frozenset({"open_app", "focus_window"})
FILE_KINDS = frozenset({"write_file"})
SURFACE_KINDS = ACTIVATE_KINDS | NAV_KINDS | TEXT_KINDS | KEY_KINDS | APP_KINDS | FILE_KINDS

KNOWN_KINDS = (
    READ_ONLY_KINDS
    | SIDE_EFFECT_KINDS
    | DENIED_KINDS
    | OUTWARD_KINDS
    | DESTRUCTIVE_KINDS
    | AGENT_READ_KINDS
    | EGRESS_READ_KINDS
    | PLAY_KINDS
    | SURFACE_KINDS
)

# A reason starting with this marks a LOCAL action that still needs a
# one-tap card (outside the allowlist, Settings toggles, keyboard into a
# comms or browser window, tainted egress). See ``needs_card``.
CARD_PREFIX = "card:"


@dataclass(frozen=True)
class Action:
    kind: str
    target: str = ""
    value: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool = False
    reason: str = ""
    risk: Risk = Risk.LOCAL
    # "" (no card), "tap" (one-tap card), "hard" (OUTWARD: fresh card bound
    # to the content digest) or "destructive" (Cancel-default card).
    card_kind: str = ""


@dataclass(frozen=True)
class Verification:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class StepOutcome:
    action: Action
    approval_required: bool
    approved: bool | None
    result: ActionResult | None
    risk: Risk = Risk.LOCAL


@dataclass(frozen=True)
class TaskOutcome:
    goal: str
    status: str
    steps: tuple[StepOutcome, ...]
    verification: Verification | None = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------

def _verb_pattern(*stems: str) -> re.Pattern[str]:
    # Matched against skeleton(name) at a word start, with an optional "re"
    # ("Resend", "Repost"). Most stems match as a prefix ("Sending",
    # "Submitted", "SENDNOW"); short ones that start common harmless words
    # spell out their endings ("Replace", "Sentence", "Likely", "Payloads",
    # "Eraser" stay local). Over-matching costs a card; under-matching costs
    # a message sent without one, so prefixes are the default.
    return re.compile(r"(?<![a-z])(?:re-?)?(?:" + "|".join(stems) + ")")


_END = r"(?![a-z])"

OUTWARD_PATTERN = _verb_pattern(
    "send", "sent" + _END, "post(?:s|ed|ing)?" + _END, "submit",
    "repl(?:y|ies|ied|ying)" + _END, "publish", "share", "sharing",
    "pa(?:y|ys|id|ying|yment|yments)" + _END, "buy(?:s|ing)?" + _END, "bought",
    "purchas", r"check\s*-?\s*out", "order(?:s|ed|ing)?" + _END, "transfer",
    "confirm", "allow", "authori[sz]", "grant", "accept", "agree", "install",
    "invit", "join", "upload", "attach", "follow", "lik(?:e|es|ed|ing)" + _END,
    "request", "appl[yi]", "book(?:s|ed|ing)?" + _END, "vote", "voting", "rsvp",
    r"sign\s*-?\s*(?:in|up)", r"log\s*-?\s*(?:in|on)", "register", "subscrib",
    "donat", "forward", "call(?:s|ed|ing)?" + _END, "approv", "tweet", "comment",
)
DESTRUCTIVE_PATTERN = _verb_pattern(
    "delet", "remov", "uninstal", "discard", "eras(?:e|ed|es|ing|ure)" + _END,
    r"empty\s*(?:the\s*)?(?:trash|recycle|bin|folder|deleted)", "format", "reset",
    "wipe", "block", "leave", "unsubscrib", "revok", "destroy", "purge",
    "deactivat", r"close\s*(?:my\s*)?account", r"end\s*task", "terminat",
    "factory", "unfriend", r"move\s*to\s*(?:the\s*)?(?:trash|bin|recycle)",
    r"don'?t\s*save", "permanent",
)

# Names that confirm whatever the dialog is about. On their own they say
# nothing, so the window title is matched as well.
_GENERIC_AFFIRMATIVE = frozenset(
    {"yes", "y", "ok", "okay", "continue", "next", "proceed", "done", "finish",
     "go", "start", "run", "save", "apply", "retry", "sure", "confirm"}
)

_BUTTON_ROLES = frozenset(
    {"button", "splitbutton", "hyperlink", "link", "menuitem", "menubaritem", "dropdownbutton"}
)
_TOGGLE_ROLES = frozenset({"checkbox", "radiobutton", "togglebutton", "toggleswitch", "switch"})
_TEXT_ROLES = frozenset({"edit", "document", "text", "textbox", "searchbox", "richedit", "combobox"})
# Roles that do not commit anything when clicked in a native comms app:
# opening a chat row, switching a tab, focusing the composer.
_COMMS_NAV_ROLES = frozenset(
    {"tabitem", "tab", "listitem", "treeitem", "dataitem", "edit", "document", "text",
     "searchbox", "pane", "group", "scrollbar", "thumb", "header", "headeritem", "image"}
)
_ENTER_CHORDS = frozenset({"enter", "ctrl+enter", "shift+enter", "alt+enter", "ctrl+shift+enter"})
_ACTIVATION_CHORDS = _ENTER_CHORDS | frozenset({"space"})
_DELETE_CHORDS = frozenset({"delete", "shift+delete", "ctrl+delete", "ctrl+d"})

# How strict each app class is, for picking the stricter of two readings.
_CLASS_STRICTNESS = {
    APP_OTHER: 0,
    APP_SETTINGS: 1,
    APP_BROWSER: 2,
    APP_COMMS: 3,
    APP_UNKNOWN: 3,
    APP_SHELL: 4,
}

_URLISH = re.compile(
    r"(?i)(?:\b[a-z][a-z0-9+.-]*://[^\s<>\"']+|\bwww\.[^\s<>\"']+|"
    r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|in|co|me|ai|app|dev|xyz|ru|cn|info|biz|ly|gg|to|"
    r"tk|ml|ga|cf|gq|top|site|online|link|click|zip|mov|sh|so|tv|us|uk|de)\b[^\s<>\"']*)"
)


def _class_label(app_class: str) -> str:
    return {APP_COMMS: "a comms", APP_BROWSER: "a browser"}.get(app_class, "an unidentified")


def _surfaces():
    # surfaces imports this module for ActionContext and the APP_* names, so
    # it is imported on first use rather than at import time.
    from xenon import surfaces

    return surfaces


def _role(text: str) -> str:
    return re.sub(r"[\s_-]+", "", skeleton(text))


def _name(text: str) -> str:
    # Split camelCase before folding so "btnSend" reads as "btn send".
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", strip_invisible(text or ""))
    return " ".join(skeleton(spaced).split())


def _known_name(name: str) -> bool:
    return bool(name) and name not in {"unknown", "?", "none", "null", "unnamed"}


def _pattern_risk(text: str) -> tuple[Risk, str] | None:
    """OUTWARD/DESTRUCTIVE from a name or title, or ``None``. Stricter wins."""
    folded = _name(text)
    if not folded:
        return None
    plain = " ".join(skeleton(text).split())
    candidates = [folded, plain]
    if has_bidi_reordering(text):
        # "\u202edneS" is displayed as "Send": match what the eye sees too.
        candidates.append(plain[::-1])
    shown = strip_invisible(text).strip()
    if any(DESTRUCTIVE_PATTERN.search(candidate) for candidate in candidates):
        return Risk.DESTRUCTIVE, f"{shown!r} looks destructive"
    if any(OUTWARD_PATTERN.search(candidate) for candidate in candidates):
        return Risk.OUTWARD, f"{shown!r} looks like it sends or commits"
    return None


def _effective_app_class(ctx: ActionContext) -> str:
    """App class from the process image, never looser than the caller's.

    ``ActionContext.app_class`` defaults to unknown, which means "not
    supplied"; a supplied class only ever makes the reading stricter.
    """
    given = ctx.app_class or APP_UNKNOWN
    if not (ctx.process_image or "").strip():
        return given
    derived = _surfaces().app_class_for(ctx.process_image, ctx.url)
    if given == APP_UNKNOWN:
        return derived
    if _CLASS_STRICTNESS.get(given, 3) > _CLASS_STRICTNESS.get(derived, 3):
        return given
    return derived


def _strict_class(ctx: ActionContext, app_class: str) -> str:
    """``app_class`` for Enter, activation and newlines.

    APP_OTHER only means "not a listed messenger, browser or terminal". For
    the three things that send in a messenger (Enter, a click, a newline),
    an image that is not on the known-local list reads as unidentified: an
    unlisted messenger (Beeper, DiscordCanary) or an Electron app showing
    someone else's content (Notion) must not get the looser answer.
    """
    if app_class != APP_OTHER:
        return app_class
    if _surfaces().is_known_local_image(ctx.process_image):
        return app_class
    return APP_UNKNOWN


def strict_app_class(ctx: ActionContext | None) -> str:
    """The class Enter, a click or a newline is judged by where ``ctx`` lands."""
    ctx = ctx if ctx is not None else ActionContext()
    return _strict_class(ctx, _effective_app_class(ctx))


def _page_authored(ctx: ActionContext, app_class: str) -> bool:
    """True when names and roles come from a web page (and can be spoofed)."""
    lib = _surfaces()
    image = lib.norm_image(ctx.process_image)
    if image and lib.is_browser_image(image):
        return True
    if app_class in (APP_BROWSER, APP_UNKNOWN):
        return True
    return not image


# File views where Enter, Space or Invoke on an item opens (runs) it.
_FILE_DIALOG_TITLE = re.compile(
    r"^(?:open|save|save as|open file|browse|browse for (?:folder|files?)|select (?:a )?files?|"
    r"choose (?:a )?files?|file upload|upload|attach(?: files?)?|insert (?:picture|file))\b"
)
_ITEM_ROLES = frozenset({"listitem", "treeitem", "dataitem", "image", ""})
# "Run anyway" (SmartScreen) and "Run" on a security warning start a
# program Windows itself flagged.
_RUN_WARNED = re.compile(r"(?<![a-z])run\s*anyway(?![a-z])")
_SECURITY_TITLE = re.compile(r"security warning|protected your pc|smartscreen|unknown publisher")


def _launch_risk(ctx: ActionContext, name: str, role: str, opening: bool) -> tuple[Risk, str] | None:
    """DENIED / DESTRUCTIVE for opening a file item in Explorer or a file dialog.

    An item whose name ends in a program or script extension is DENIED: a
    plan must not get around the denied run_command kind by pressing Enter
    on "invoice.pdf.bat". Explorer hides known extensions, so any other
    item opened there may still be a program: it gets a hard card that
    names it (DESTRUCTIVE: Cancel is the default, no undo).
    """
    lib = _surfaces()
    image = lib.norm_image(ctx.process_image)
    title = skeleton(ctx.window_title).strip()
    explorer = image == "explorer.exe"
    if not (explorer or _FILE_DIALOG_TITLE.match(title)):
        return None
    shown = strip_invisible(name or "").strip()
    if lib.launch_extension(shown):
        return Risk.DENIED, f"opening {shown!r} would run a program or script"
    if explorer and opening and role in _ITEM_ROLES:
        what = repr(shown) if shown else "the selected item (its name could not be read)"
        return Risk.DESTRUCTIVE, f"opening {what} in File Explorer can start a program"
    return None


def _non_ascii_letters(text: str) -> bool:
    return any(char.isalpha() and not char.isascii() for char in strip_invisible(text or ""))


def _in_allowlist(ctx: ActionContext, target: str = "") -> bool:
    if not ctx.allowlist:
        return False
    lib = _surfaces()
    allowed = {lib.norm_image(str(item)) for item in ctx.allowlist}
    allowed_stems = {item[:-4].split(".")[0] for item in allowed if item}
    if target:
        stem = skeleton(target).strip().removesuffix(".exe")
        return lib.norm_image(target) in allowed or stem in allowed_stems
    image = lib.norm_image(ctx.process_image)
    return bool(image) and image in allowed


def _path_exists(target: str) -> bool:
    """Does the write target already exist? Unreadable counts as yes (stricter)."""
    raw = strip_invisible(target or "").strip()
    if not raw:
        return False
    try:
        return Path(raw).expanduser().exists()
    except (OSError, ValueError, RuntimeError):
        return True


def _keys_of(action: Action) -> str:
    return action.value if (action.value or "").strip() else action.target


def _app_target_refusal(kind: str, target: str, ctx: ActionContext) -> str | None:
    """DENIED reasons for open_app / focus_window targets."""
    lib = _surfaces()
    raw = strip_invisible(target or "").strip()
    folded = skeleton(raw)
    if not raw:
        return None  # the backend reports a missing target itself
    if kind == "open_app":
        if raw.startswith(("\\\\", "//")):
            return "UNC paths are never opened"
        scheme = lib.url_scheme(raw)
        if scheme and not re.match(r"^[a-z]:[\\/]", folded) and scheme not in {"http", "https"}:
            return f"{scheme}: links are never opened"
        if ctx.origin in {"model", "voice", "ipc"} and re.search(r"[:/\\%]", raw):
            return "model and voice requests may only name a catalog app"
        image = lib.norm_image(raw)
        if image in {lib.norm_image(item) for item in lib.SHELL_IMAGES} or folded in {
            "terminal", "windows terminal", "command prompt", "powershell", "wt", "console"
        }:
            return "consoles and terminals are never opened by an agent"
        if image in {lib.norm_image(item) for item in lib.DENIED_IMAGES} or folded in {
            "registry editor", "task scheduler", "task manager", "run", "control panel"
        }:
            return f"{raw} is a protected system surface"
    else:
        for prefix in ("command prompt", "windows powershell", "powershell", "administrator:",
                       "registry editor", "task scheduler", "user account control",
                       "windows security", "credential manager"):
            if folded.startswith(prefix):
                return f"{raw!r} is a protected window"
        if folded in {"run", "xenon"}:
            return f"{raw!r} is a protected window"
    if ctx.tainted and not _in_allowlist(ctx, raw):
        return "a tainted task cannot open or focus apps outside its allowlist"
    return None


def _looks_like_url_text(text: str) -> bool:
    stripped = re.sub(r"[\t\r\n]+", "", strip_invisible(text or "")).strip()
    if re.match(r"(?i)^(?:javascript|vbscript)\s*:", stripped):
        return True
    return bool(stripped) and not re.search(r"\s", stripped) and bool(
        re.match(r"(?i)^[a-z][a-z0-9+.-]*:", stripped) or "." in stripped or "/" in stripped
    )


def _foreign_hosts(text: str, user_words: str) -> list[str]:
    """Hosts in ``text`` that Roshan did not say himself."""
    lib = _surfaces()
    said = skeleton(user_words)
    foreign = []
    for match in _URLISH.finditer(strip_invisible(text or "")):
        host = lib.url_host(match.group(0))
        if not host:
            foreign.append(match.group(0))
            continue
        domain = lib.registrable_domain(host)
        if host not in said and (not domain or domain not in said):
            foreign.append(host)
    return foreign


def _is_search_box(ctx: ActionContext) -> bool:
    return "search" in _name(ctx.element_name) or _role(ctx.element_role) in {"searchbox"}


def _is_file_name_field(ctx: ActionContext) -> bool:
    name = _name(ctx.element_name)
    return name.startswith("file name") or name.startswith("filename")


def _classify_activation(
    kind: str, name: str, role: str, ctx: ActionContext, app_class: str
) -> tuple[Risk, str]:
    """Clicks, invokes and key activations (Enter/Space on a focused element)."""
    folded = _name(name)
    shown = strip_invisible(name or "").strip()
    if _RUN_WARNED.search(folded) or (
        folded in {"run", "open", "yes"} and _SECURITY_TITLE.search(skeleton(ctx.window_title))
    ):
        return Risk.DENIED, f"{shown!r} runs a program Windows warned about"
    launch = _launch_risk(ctx, name, role, opening=kind in {"invoke", "press"})
    if launch is not None:
        return launch
    app_class = _strict_class(ctx, app_class)
    if app_class in (APP_COMMS, APP_BROWSER, APP_UNKNOWN):
        label = _class_label(app_class)
        if _page_authored(ctx, app_class):
            lib = _surfaces()
            if app_class == APP_UNKNOWN and not lib.is_browser_image(ctx.process_image):
                # An unidentified native app: its own "Delete" is still the
                # stricter destructive card, everything else a hard card.
                patterned = _pattern_risk(name)
                if patterned is not None and patterned[0] is Risk.DESTRUCTIVE:
                    return patterned
                return Risk.OUTWARD, (
                    f"every click in an unidentified app is outward "
                    f"({lib.norm_image(ctx.process_image) or 'unknown app'} is not a known local app)"
                )
            # Accessible names and roles on a web page are page-authored: a
            # submit button can call itself "Cancel" and claim role=tab.
            return Risk.OUTWARD, f"every click in {label} page is outward (names can be spoofed)"
        if not _known_name(folded):
            return Risk.OUTWARD, f"element with an unknown or empty name in {label} app"
        patterned = _pattern_risk(name)
        if patterned is not None:
            return patterned
        if role in _COMMS_NAV_ROLES:
            return Risk.LOCAL, f"selecting a {role} does not send anything"
        if kind == "focus" and _is_search_box(ctx):
            return Risk.LOCAL, "focusing the app's own search box"
        if (
            not ctx.tainted
            and ctx.ui_language_english  # a non-English button name cannot be checked
            and role not in {"hyperlink", "link"}
            and exact_span(name, ctx.user_words)
        ):
            return Risk.LOCAL, f"{shown!r} is your own words in an untainted task"
        return Risk.OUTWARD, f"every button, link or menu item in {label} app is outward"

    # Everywhere else: names decide, then the dialog title for "Yes"/"OK".
    patterned = _pattern_risk(name)
    if patterned is not None:
        return patterned
    if folded in _GENERIC_AFFIRMATIVE or not _known_name(folded):
        titled = _pattern_risk(ctx.window_title)
        if titled is not None:
            risk, why = titled
            return risk, f"{shown or 'the button'!r} confirms a dialog where {why}"
    buttonish = role in _BUTTON_ROLES or role in _TOGGLE_ROLES or not role
    if not ctx.ui_language_english and buttonish:
        return Risk.OUTWARD, "the UI is not in English, so every button is outward"
    if buttonish and _non_ascii_letters(name):
        # Small capitals or Cherokee spell "Send" outside the confusables
        # table: a name the patterns cannot read counts as unknown.
        return Risk.OUTWARD, f"{shown!r} uses letters the safety patterns cannot read"
    if not _known_name(folded) and (role in _BUTTON_ROLES or not role):
        return Risk.OUTWARD, "a nameless button could do anything"
    if app_class == APP_SETTINGS:
        if role in _TOGGLE_ROLES or kind in {"toggle", "check", "uncheck"}:
            return Risk.LOCAL, f"{CARD_PREFIX} Settings toggle {shown!r} (show old and new state)"
        return Risk.LOCAL, f"{CARD_PREFIX} Settings change {shown!r}"
    return Risk.LOCAL, f"click {shown!r}"


def _classify_keys(action: Action, ctx: ActionContext, app_class: str) -> tuple[Risk, str]:
    lib = _surfaces()
    keys = _keys_of(action)
    chords = [lib.normalize_chord(chord) for chord in lib.split_chords(keys)]
    focus_role = _role(ctx.focused_role or ctx.element_role)
    in_text = focus_role in _TEXT_ROLES
    worst: tuple[Risk, str] = (Risk.LOCAL, f"press {keys.strip()}")

    def stricter(candidate: tuple[Risk, str]) -> None:
        nonlocal worst
        if candidate[0].rank > worst[0].rank:
            worst = candidate

    strict = _strict_class(ctx, app_class)
    for chord in chords:
        activating = chord in _ACTIVATION_CHORDS
        if activating and not in_text:
            launch = _launch_risk(ctx, ctx.element_name, focus_role, opening=True)
            if launch is not None:
                stricter(launch)
                continue
        # Enter and Space are judged strictly: in an app that is not a known
        # local one they may send, whatever its process name says.
        chord_class = strict if activating else app_class
        if chord_class in (APP_COMMS, APP_BROWSER, APP_UNKNOWN):
            label = _class_label(chord_class)
            if chord in _ENTER_CHORDS and (in_text or not focus_role):
                stricter((Risk.OUTWARD, f"{chord} in {label} composer sends"))
            elif chord not in lib.NAV_KEYS:
                stricter((Risk.OUTWARD, f"{chord}: only navigation keys are local in {label} window"))
            continue
        parts = chord.split("+")
        if "alt" in parts and len(parts) == 2 and re.fullmatch(r"[a-z0-9]", parts[-1]):
            stricter((Risk.OUTWARD, f"{chord} is an accelerator that can press any button"))
        elif chord in _DELETE_CHORDS and not in_text:
            stricter((Risk.DESTRUCTIVE, f"{chord} outside a text field deletes the selection"))
        elif chord in _ACTIVATION_CHORDS and not in_text:
            if not focus_role:
                stricter((Risk.OUTWARD, f"{chord} with unknown focus may press a button"))
            else:
                stricter(_classify_activation("press", ctx.element_name, focus_role, ctx, app_class))
        elif chord in _ENTER_CHORDS and focus_role == "edit" and " - " not in ctx.window_title:
            # Enter in a single-line field presses the dialog's default
            # button, so a dialog titled "Delete file" decides the risk.
            # Document windows ("notes.txt - Notepad") are skipped: Enter in
            # a document is a newline, and his file names are not verbs.
            titled = _pattern_risk(ctx.window_title)
            if titled is not None:
                stricter(titled)
    if app_class == APP_SETTINGS and worst[0] is Risk.LOCAL:
        worst = (Risk.LOCAL, f"{CARD_PREFIX} key press in Settings")
    return worst


def _classify_text(action: Action, ctx: ActionContext, app_class: str) -> tuple[Risk, str]:
    text = action.value or ""
    # Line/paragraph separators, NEL, VT and FF count too: some editors treat
    # them as Enter, and the stricter reading is the safe one.
    has_newline = bool(re.search(r"[\r\n\u2028\u2029\x85\x0b\x0c]", text))
    if app_class in (APP_COMMS, APP_BROWSER, APP_UNKNOWN):
        label = _class_label(app_class)
        if has_newline:
            return Risk.OUTWARD, f"a newline typed into {label} field can send it"
        return Risk.LOCAL, f"{CARD_PREFIX} typing into {label} window"
    if has_newline and _strict_class(ctx, app_class) == APP_UNKNOWN:
        return Risk.OUTWARD, "a newline typed into an app that is not a known local app can send it"
    if has_newline:
        enter = _classify_keys(Action("press_keys", value="enter"), ctx, app_class)
        if enter[0].rank > Risk.LOCAL.rank:
            return enter[0], f"the text contains a newline: {enter[1]}"
    if app_class == APP_SETTINGS:
        return Risk.LOCAL, f"{CARD_PREFIX} typing into Settings"
    return Risk.LOCAL, f"type {len(text)} characters"


def _classify_core(action: Action, ctx: ActionContext | None) -> tuple[Risk, str]:
    lib = _surfaces()
    kind = (action.kind or "").strip().lower()
    if kind.startswith("propose_"):
        kind = kind[len("propose_"):]
    observed = ctx is not None
    ctx = ctx if ctx is not None else ActionContext()

    # 1. Denied kinds and denied surfaces.
    if kind in DENIED_KINDS:
        return Risk.DENIED, f"action {kind!r} is not permitted"
    if kind not in KNOWN_KINDS:
        return Risk.DENIED, f"unknown action kind {kind!r}"
    if kind in READ_ONLY_KINDS or kind in AGENT_READ_KINDS:
        return Risk.READ, "read-only"
    if kind in EGRESS_READ_KINDS or kind in PLAY_KINDS:
        base = Risk.READ if kind in EGRESS_READ_KINDS else Risk.LOCAL
        if ctx.tainted:
            return Risk.LOCAL, f"{CARD_PREFIX} tainted task: confirm the exact query"
        return base, f"{kind} from your words"
    touches_foreground = kind in SURFACE_KINDS - APP_KINDS - FILE_KINDS or kind in {
        "desktop_commit",
        "desktop_destroy",
    }
    if touches_foreground:
        reason = lib.is_denied_surface(ctx)
        if reason:
            return Risk.DENIED, reason
    if kind in APP_KINDS:
        reason = _app_target_refusal(kind, action.target, ctx)
        if reason:
            return Risk.DENIED, reason
    if kind in KEY_KINDS:
        reason = lib.key_refused(_keys_of(action), ctx)
        if reason:
            return Risk.DENIED, reason
    if kind in TEXT_KINDS:
        if has_bidi_reordering(action.value) or has_tag_characters(action.value):
            # What a right-to-left override shows is not what is sent, and
            # tag characters are a hidden channel: neither can be approved
            # on a card that shows the text.
            return Risk.DENIED, "the text contains hidden or direction-changing characters"
        if _is_file_name_field(ctx):
            reason = lib.file_dialog_refusal(action.value, ctx.user_words)
            if reason:
                return Risk.DENIED, f"file dialog: {reason}"
        browserish = lib.is_browser_image(ctx.process_image) or not ctx.process_image.strip()
        if browserish and _looks_like_url_text(action.value):
            reason = lib.denied_url_reason(re.sub(r"[\t\r\n]+", "", action.value))
            if reason:
                return Risk.DENIED, f"typing that address is refused: {reason}"
    if kind in FILE_KINDS:
        reason = lib.path_refusal(action.target)
        if reason:
            return Risk.DENIED, f"write_file: {reason}"

    # 2. Kinds that are outward or destructive by definition.
    if kind in OUTWARD_KINDS:
        return Risk.OUTWARD, f"{kind} reaches another person"
    if kind in DESTRUCTIVE_KINDS:
        return Risk.DESTRUCTIVE, f"{kind} cannot be undone"

    # Opening or focusing an app, and writing a file, do not depend on what
    # is in the foreground right now.
    if kind in APP_KINDS:
        if _in_allowlist(ctx, action.target):
            return Risk.LOCAL, f"{kind} {action.target!r} is in the task's allowlist"
        return Risk.LOCAL, f"{CARD_PREFIX} {kind} {action.target!r} is outside the task's allowlist"
    if kind in FILE_KINDS:
        if _path_exists(action.target):
            # Overwriting loses what was there; it cannot be undone.
            return Risk.DESTRUCTIVE, f"write {action.target!r} would overwrite an existing file"
        if lib.file_dialog_refusal(action.target, ctx.user_words) is None:
            return Risk.LOCAL, f"write {action.target!r} (named in your words)"
        return Risk.LOCAL, f"{CARD_PREFIX} write {action.target!r}: the path is not in your words"

    # 3-6. Surface rules by app class.
    app_class = _effective_app_class(ctx)
    if kind in NAV_KINDS:
        risk, reason = Risk.LOCAL, f"{kind} is navigation"
    elif kind in KEY_KINDS:
        risk, reason = _classify_keys(action, ctx, app_class)
    elif kind in TEXT_KINDS:
        risk, reason = _classify_text(action, ctx, app_class)
    elif kind in ACTIVATE_KINDS:
        name = ctx.element_name if (ctx.element_name or "").strip() else action.target
        role_source = ctx.element_role
        if not (role_source or "").strip() and kind == "click_control":
            role_source = action.value  # legacy click_control carries the control type here
        risk, reason = _classify_activation(kind, name, _role(role_source), ctx, app_class)
    else:  # desktop_commit / desktop_destroy were handled above
        return Risk.DENIED, f"unhandled action kind {kind!r}"

    if risk is not Risk.LOCAL:
        return risk, reason

    # 7. A tainted task adds cards for egress-shaped LOCAL steps.
    if ctx.tainted and kind in TEXT_KINDS:
        foreign = _foreign_hosts(action.value, ctx.user_words)
        if foreign:
            return Risk.LOCAL, f"{CARD_PREFIX} tainted task: text names {foreign[0]!r}, which you did not say"
        if _page_authored(ctx, app_class) or _is_search_box(ctx):
            return Risk.LOCAL, f"{CARD_PREFIX} tainted task: typing into a browser or search box"

    # LOCAL needs no card only inside the task's app allowlist.
    if not reason.startswith(CARD_PREFIX) and not (observed and _in_allowlist(ctx)):
        reason = f"{CARD_PREFIX} outside the task's app allowlist: {reason}"
    return risk, reason


def _coerce_hint(hint: Risk | str | None) -> Risk | None:
    if hint is None:
        return None
    if isinstance(hint, Risk):
        return hint
    text = str(hint).strip().lower()
    if not text:
        return None
    try:
        return Risk(text)
    except ValueError:
        # A flag we do not understand still means "the model is worried".
        return Risk.OUTWARD


def _outward_switched_off() -> str | None:
    """Reason outward actions are switched off, or ``None``."""
    try:
        from xenon.killswitch import outward_allowed
    except ImportError:
        value = os.environ.get("XENON_OUTWARD", "").strip().lower()
        if value in {"off", "0", "false", "no", "disabled"}:
            return "outward actions are switched off (XENON_OUTWARD=off)"
        return None
    try:
        allowed, why = outward_allowed()
    except Exception as error:  # an unreadable kill switch counts as "off"
        return f"could not read the outward kill switch: {error}"
    return None if allowed else (why or "outward actions are switched off")


def classify(
    action: Action,
    ctx: ActionContext | None = None,
    model_hint: Risk | str | None = None,
) -> tuple[Risk, str]:
    """Decide the risk class of ``action`` where it lands, with a reason.

    Follows the classification order of the safety design: denied kinds and
    surfaces, outward/destructive kinds, comms and browser rules, name
    patterns elsewhere, Enter in composers, Settings toggles, then tainted
    egress. ``ctx`` is the live target; ``None`` means "not observed yet"
    and is read as an unidentified app (handled like comms), never as safe.

    A LOCAL result whose reason starts with ``CARD_PREFIX`` ("card:") still
    needs a one-tap card; ``needs_card`` answers that question.

    ``model_hint`` is a model's own risk flag. It can only raise the result;
    an unrecognised flag counts as OUTWARD.
    """
    risk, reason = _classify_core(action, ctx)
    hint = _coerce_hint(model_hint)
    if hint is not None and hint.rank > risk.rank:
        risk, reason = hint, f"the model flagged this as {hint.value}; {reason}"
    if risk in (Risk.OUTWARD, Risk.DESTRUCTIVE):
        off = _outward_switched_off()
        if off:
            return Risk.DENIED, off
    return risk, reason


def needs_card(risk: Risk, reason: str = "", ctx: ActionContext | None = None) -> bool:
    """True when a human must confirm this specific action on a card.

    OUTWARD and DESTRUCTIVE always need one (a hard card); LOCAL needs a
    one-tap card when ``classify`` prefixed its reason with "card:". A
    blanket "--yes" is not a card. DENIED needs none: it is refused.
    ``ctx`` is accepted for symmetry with ``classify``; a tainted context
    never removes a card.
    """
    if risk in (Risk.OUTWARD, Risk.DESTRUCTIVE):
        return True
    if risk is Risk.LOCAL:
        return (reason or "").strip().startswith(CARD_PREFIX)
    return False


def card_kind_for(risk: Risk, reason: str = "") -> str:
    if risk is Risk.DESTRUCTIVE:
        return "destructive"
    if risk is Risk.OUTWARD:
        return "hard"
    if needs_card(risk, reason):
        return "tap"
    return ""


class ApprovalRefused(Exception):
    """Raised by an approver when a step is refused for a reason other than
    Roshan saying no (a cap, an unreadable composer, no card available), so
    the task summary says why instead of "user declined"."""


class ActionBackend(Protocol):
    def execute(self, action: Action) -> ActionResult: ...


class Verifier(Protocol):
    def verify(self, goal: str) -> Verification: ...


# A legacy approver takes the action only; a risk-aware one also takes the
# Risk, so it can refuse OUTWARD/DESTRUCTIVE (a blanket "--yes" must not
# approve a send). TaskRunner calls whichever form it is given.
Approver = Callable[..., bool]
ContextProvider = Callable[[Action], ActionContext]


class ApprovalPolicy:
    """Decides whether an action may run and whether a human must approve it.

    Read-only kinds run, DENIED kinds and surfaces are refused, everything
    else needs approval, as before. The decision now also carries the risk
    class from ``classify`` and the kind of card a human must see.
    """

    def decide(self, action: Action, ctx: ActionContext | None = None) -> PolicyDecision:
        risk, reason = classify(action, ctx)
        if risk is Risk.DENIED:
            return PolicyDecision(allowed=False, reason=reason, risk=risk)
        if risk is Risk.READ:
            return PolicyDecision(allowed=True, reason=reason, risk=risk)
        return PolicyDecision(
            allowed=True,
            requires_approval=True,
            reason=reason,
            risk=risk,
            card_kind=card_kind_for(risk, reason),
        )


def _positional_args(func: Callable[..., object]) -> int:
    """How many positional arguments ``func`` takes (99 for ``*args``)."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return 1
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return 99
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return positional


def _accepts_two_args(func: Callable[..., object]) -> bool:
    """True when ``func`` can be called as ``func(action, risk)``."""
    return _positional_args(func) >= 2


class FileVerifier:
    """Considers a task verified when the expected file exists with content."""

    def __init__(self, path: str, contains: str = "") -> None:
        self.path = path
        self.contains = contains

    def verify(self, goal: str) -> Verification:
        path = Path(self.path).expanduser()
        if not path.is_file():
            return Verification(False, f"{path} was not written")
        if not self.contains:
            return Verification(True, f"{path} exists")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            return Verification(False, f"could not read {path}: {error}")
        if self.contains not in text:
            return Verification(False, f"{path} does not contain {self.contains!r}")
        return Verification(True, f"{path} contains the expected text")


class TaskRunner:
    """Executes a plan, stopping at the first refusal or failure."""

    def __init__(
        self,
        policy: ApprovalPolicy,
        backend: ActionBackend,
        approver: Approver | None = None,
        verifier: Verifier | None = None,
        context_provider: ContextProvider | None = None,
    ) -> None:
        self._policy = policy
        self._backend = backend
        self._approver = approver
        self._verifier = verifier
        # Reads the live target just before each step (after the previous
        # step ran), so the risk is decided where the action really lands.
        self._context_provider = context_provider
        arity = _positional_args(approver) if approver is not None else 0
        self._approver_takes_risk = arity >= 2
        # A three-argument approver also gets the classifier's reason, so it
        # can tell a "card:" LOCAL step from one that needs no card.
        self._approver_takes_reason = arity >= 3

    def _context_for(self, action: Action) -> ActionContext | None:
        if self._context_provider is None:
            return None
        try:
            return self._context_provider(action)
        except Exception:
            # Could not read the target: classify it as unidentified, which
            # is strict, rather than as "not observed".
            return ActionContext()

    def _decide(self, action: Action, ctx: ActionContext | None) -> PolicyDecision:
        if ctx is None:
            return self._policy.decide(action)
        return self._policy.decide(action, ctx)

    def _approve(self, action: Action, risk: Risk, reason: str = "") -> bool:
        if self._approver_takes_reason:
            return bool(self._approver(action, risk, reason))
        if self._approver_takes_risk:
            return bool(self._approver(action, risk))
        return bool(self._approver(action))

    def run(self, goal: str, actions: Sequence[Action]) -> TaskOutcome:
        completed: list[StepOutcome] = []

        for action in actions:
            decision = self._decide(action, self._context_for(action))
            if not decision.allowed:
                return TaskOutcome(
                    goal=goal,
                    status="denied",
                    steps=tuple(completed),
                    detail=decision.reason,
                )

            approved: bool | None = None
            if decision.requires_approval:
                if self._approver is None:
                    return TaskOutcome(
                        goal=goal,
                        status="denied",
                        steps=tuple(completed),
                        detail=f"approval required for {action.kind!r} but no approver is configured",
                    )
                refusal = ""
                try:
                    approved = self._approve(action, decision.risk, decision.reason)
                except ApprovalRefused as refused:
                    approved, refusal = False, str(refused) or "refused"
                except Exception:
                    approved = False
                if not approved:
                    completed.append(
                        StepOutcome(
                            action=action,
                            approval_required=True,
                            approved=False,
                            result=None,
                            risk=decision.risk,
                        )
                    )
                    return TaskOutcome(
                        goal=goal,
                        status="denied",
                        steps=tuple(completed),
                        detail=refusal or f"user declined {action.kind!r}",
                    )

            try:
                result = self._backend.execute(action)
            except Exception as error:  # a backend bug must not look like success
                result = ActionResult(False, str(error))

            completed.append(
                StepOutcome(
                    action=action,
                    approval_required=decision.requires_approval,
                    approved=approved,
                    result=result,
                    risk=decision.risk,
                )
            )
            if not result.ok:
                return TaskOutcome(
                    goal=goal,
                    status="failed",
                    steps=tuple(completed),
                    detail=result.detail,
                )

        if self._verifier is None:
            return TaskOutcome(
                goal=goal,
                status="unverified",
                steps=tuple(completed),
                detail="no verifier is configured, so success cannot be claimed",
            )

        try:
            verification = self._verifier.verify(goal)
        except Exception as error:
            return TaskOutcome(
                goal=goal,
                status="unverified",
                steps=tuple(completed),
                detail=f"verification failed: {error}",
            )

        return TaskOutcome(
            goal=goal,
            status="succeeded" if verification.ok else "unverified",
            steps=tuple(completed),
            verification=verification,
            detail=verification.detail,
        )
