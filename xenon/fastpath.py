"""Deterministic parsing for commands that should never wait on a model.

Launching an app, switching windows, and searching the web are common and
unambiguous enough to run directly. Anything else returns ``None`` so the model
planner keeps its job. The parser is deliberately conservative: an app name must
be in the alias table or be a single executable-looking token, and a phrase with
"and", "then", or a pronoun falls through instead of guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from xenon.actions import Action

# alias -> (launch target, window-title fragment used for verification)
APP_ALIASES: dict[str, tuple[str, str]] = {
    "notepad": ("notepad", "Notepad"),
    "note pad": ("notepad", "Notepad"),
    "calculator": ("calc", "Calculator"),
    "calc": ("calc", "Calculator"),
    "paint": ("mspaint", "Paint"),
    "microsoft paint": ("mspaint", "Paint"),
    "explorer": ("explorer", "File Explorer"),
    "file explorer": ("explorer", "File Explorer"),
    "files": ("explorer", "File Explorer"),
    "cmd": ("cmd", "Command Prompt"),
    "command prompt": ("cmd", "Command Prompt"),
    "terminal": ("cmd", "Command Prompt"),
    "powershell": ("powershell", "PowerShell"),
    "windows powershell": ("powershell", "PowerShell"),
    "task manager": ("taskmgr", "Task Manager"),
    "settings": ("ms-settings:", "Settings"),
    "windows settings": ("ms-settings:", "Settings"),
    "chrome": ("chrome", "Chrome"),
    "google chrome": ("chrome", "Chrome"),
    "edge": ("msedge", "Edge"),
    "microsoft edge": ("msedge", "Edge"),
    "firefox": ("firefox", "Firefox"),
    "opera": ("opera", "Opera"),
    "vscode": ("code", "Visual Studio Code"),
    "vs code": ("code", "Visual Studio Code"),
    "visual studio code": ("code", "Visual Studio Code"),
    "code": ("code", "Visual Studio Code"),
    "obsidian": ("obsidian", "Obsidian"),
    "spotify": ("spotify", "Spotify"),
    "discord": ("discord", "Discord"),
    "slack": ("slack", "Slack"),
    "steam": ("steam", "Steam"),
    "word": ("winword", "Word"),
    "microsoft word": ("winword", "Word"),
    "excel": ("excel", "Excel"),
    "microsoft excel": ("excel", "Excel"),
    "powerpoint": ("powerpnt", "PowerPoint"),
    "microsoft powerpoint": ("powerpnt", "PowerPoint"),
    "outlook": ("outlook", "Outlook"),
    "teams": ("msteams", "Teams"),
    "microsoft teams": ("msteams", "Teams"),
    "zoom": ("zoom", "Zoom"),
    "notion": ("notion", "Notion"),
    "whatsapp": ("whatsapp", "WhatsApp"),
    "telegram": ("telegram", "Telegram"),
    "antigravity": ("antigravity", "Antigravity"),
    "android studio": ("android studio", "Android Studio"),
    "7-zip": ("7-zip", "7-Zip File Manager"),
    "7zip": ("7-zip", "7-Zip File Manager"),
    "brave": ("brave", "Brave"),
    "libreoffice": ("libreoffice", "LibreOffice"),
    "youtube": ("https://www.youtube.com", "YouTube"),
    "gmail": ("https://mail.google.com", "Gmail"),
    "google": ("https://www.google.com", "Google"),
    "github": ("https://github.com", "GitHub"),
    "chatgpt": ("https://chatgpt.com", "ChatGPT"),
    "claude": ("https://claude.ai", "Claude"),
    "reddit": ("https://www.reddit.com", "Reddit"),
    "twitter": ("https://x.com", "X"),
    "x": ("https://x.com", "X"),
    "linkedin": ("https://www.linkedin.com", "LinkedIn"),
    "netflix": ("https://www.netflix.com", "Netflix"),
    "maps": ("https://maps.google.com", "Google Maps"),
}

# Words that look like an app name but make the sentence a task for the model.
_NOT_APPS = frozenset(
    {
        "it",
        "this",
        "that",
        "these",
        "those",
        "file",
        "files",
        "folder",
        "window",
        "tab",
        "document",
        "browser",
        "website",
        "url",
        "search",
        "searching",
        "timer",
        "alarm",
        "reminder",
        "music",
        "video",
        "email",
        "mail",
        "over",
        "again",
        "up",
        "my",
        "script",
        "test",
        "tests",
        "build",
        "program",
        "project",
    }
)

_WAKE_PREFIX = r"(?:(?:hey\s+)?xenon[,\s]+)?"

_POLITE_RE = re.compile(
    r"^(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|(?:i\s+want\s+you\s+to|i\s+need\s+you\s+to)\s+)",
    flags=re.IGNORECASE,
)
_OPEN_RE = re.compile(
    rf"^{_WAKE_PREFIX}(?:please\s+)?(?:open|launch|start|run)\s+"
    r"(?P<app>[a-z0-9][a-z0-9 .+#&:_'-]{0,40}?)[.!?]*$",
    flags=re.IGNORECASE,
)
_FOCUS_RE = re.compile(
    rf"^{_WAKE_PREFIX}(?:please\s+)?(?:focus|switch\s+to|switch|bring\s+up)\s+"
    r"(?P<window>[a-z0-9][a-z0-9 .+#&:_'-]{0,60}?)[.!?]*$",
    flags=re.IGNORECASE,
)
_SEARCH_RE = re.compile(
    rf"^{_WAKE_PREFIX}(?:please\s+)?"
    r"(?:search(?:\s+the\s+web)?(?:\s+for)?|look\s+up|google|web\s+search(?:\s+for)?)\s+"
    r"(?P<query>.+?)[.!?]*$",
    flags=re.IGNORECASE,
)

_SINGLE_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,30}$")


@dataclass(frozen=True)
class FastCommand:
    """A deterministic intent: ``open_app``, ``focus_window``, or ``search``."""

    kind: str
    target: str
    label: str = ""

    def action(self) -> Action | None:
        if self.kind == "open_app":
            return Action(
                "open_app",
                target=self.target,
                value=self.label,
                reason="deterministic launch",
            )
        if self.kind == "focus_window":
            return Action(
                "focus_window",
                target=self.target,
                reason="deterministic focus",
            )
        return None


def parse(text: str) -> FastCommand | None:
    """Return a deterministic command for a short, unambiguous request."""
    phrase = " ".join(text.strip().split())
    # "Can you open calculator?" is the same request as "open calculator".
    phrase = _POLITE_RE.sub("", phrase)
    phrase = re.sub(r"\s+(?:please|for me)(?=[.!?]*$)", "", phrase, flags=re.IGNORECASE)
    if not phrase:
        return None

    open_match = _OPEN_RE.match(phrase)
    if open_match:
        app = _clean(open_match.group("app"))
        return _open_command(app)

    focus_match = _FOCUS_RE.match(phrase)
    if focus_match:
        window = _clean(focus_match.group("window"))
        return _focus_command(window)

    search_match = _SEARCH_RE.match(phrase)
    if search_match:
        query = search_match.group("query").strip().strip("\"'").strip()
        if _usable(query, max_words=12):
            return FastCommand("search", query)
    return None


def _clean(value: str) -> str:
    cleaned = " ".join(value.strip().split()).lower()
    cleaned = re.sub(r"^(?:the|my)\s+", "", cleaned)
    cleaned = re.sub(r"\s+(?:app|application|window|please|for me)$", "", cleaned)
    return cleaned.strip()


def _open_command(app: str) -> FastCommand | None:
    if not _usable(app, max_words=3):
        return None
    if app in APP_ALIASES:
        target, label = APP_ALIASES[app]
        return FastCommand("open_app", target, label)
    # Unknown multi-word phrases are almost always a task ("open my email and
    # reply"), not an executable. Only accept one executable-looking token.
    if _SINGLE_TOKEN_RE.fullmatch(app):
        # "start writing" is a task, not an app called Writing.
        if app.endswith("ing") and len(app) > 5:
            return None
        return FastCommand("open_app", app, app.title())
    return None


def _focus_command(window: str) -> FastCommand | None:
    if not _usable(window, max_words=6):
        return None
    if window in APP_ALIASES:
        # Focus by the title fragment, not the executable name.
        return FastCommand("focus_window", window)
    return FastCommand("focus_window", window)


def _usable(value: str, max_words: int) -> bool:
    if not value or value in _NOT_APPS:
        return False
    if any(marker in value for marker in (" and ", " then ", ",", ";")):
        return False
    words = value.split()
    if not words or len(words) > max_words:
        return False
    return any(char.isalnum() for char in value)
