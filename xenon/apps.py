"""Resolve a spoken app name to something Windows can actually launch.

A bare executable name is not enough on a modern Windows install: Chrome,
Obsidian, Notion, Telegram and most Store apps are registered as Start-menu
entries with an *application user model ID* (AUMID), not as commands on PATH.
``os.startfile("chrome")`` therefore fails even though Chrome is installed.

This module builds one catalog from the places Windows itself uses:

* the ``App Paths`` registry keys (classic desktop programs),
* Start-menu shortcuts (``.lnk``/``.url``),
* ``Get-StartApps`` (Store apps and packaged desktop apps).

Resolution is alias-aware (``code`` -> ``Visual Studio Code``) and a launch
prefers a real path when one exists, then falls back to
``explorer.exe shell:AppsFolder\\<AUMID>`` for packaged apps.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

CREATE_NO_WINDOW = 0x08000000
CACHE_TTL_S = 12 * 60 * 60

# Spoken aliases -> names that appear in the Start menu. The key is what the
# user says or what a fast-path command carries as its target.
SEARCH_NAMES: dict[str, tuple[str, ...]] = {
    "notepad": ("notepad",),
    "calc": ("calculator",),
    "calculator": ("calculator",),
    "mspaint": ("paint",),
    "paint": ("paint",),
    "explorer": ("file explorer",),
    "files": ("file explorer",),
    "cmd": ("command prompt",),
    "command prompt": ("command prompt",),
    "powershell": ("windows powershell",),
    "taskmgr": ("task manager",),
    "ms-settings:": ("settings",),
    "chrome": ("google chrome", "chrome"),
    "msedge": ("microsoft edge", "edge"),
    "edge": ("microsoft edge", "edge"),
    "firefox": ("firefox",),
    "code": ("visual studio code", "vs code"),
    "vscode": ("visual studio code", "vs code"),
    "visual studio code": ("visual studio code", "vs code"),
    "obsidian": ("obsidian",),
    "notion": ("notion",),
    "telegram": ("telegram",),
    "whatsapp": ("whatsapp",),
    "spotify": ("spotify",),
    "discord": ("discord",),
    "slack": ("slack",),
    "steam": ("steam",),
    "zoom": ("zoom",),
    "teams": ("microsoft teams", "teams"),
    "msteams": ("microsoft teams", "teams"),
    "word": ("word",),
    "winword": ("word",),
    "excel": ("excel",),
    "powerpoint": ("powerpoint",),
    "powerpnt": ("powerpoint",),
    "outlook": ("outlook",),
    "antigravity": ("antigravity",),
    "android studio": ("android studio",),
    "7-zip": ("7-zip file manager", "7-zip"),
    "audacity": ("audacity",),
}

# Common requests that are really a website. Opening the default browser is the
# only sensible behaviour when no desktop client is installed.
WEB_APPS: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "github": "https://github.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "reddit": "https://www.reddit.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "linkedin": "https://www.linkedin.com",
    "netflix": "https://www.netflix.com",
    "maps": "https://maps.google.com",
    "drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com",
}

# FOLDERID_* values that ``Get-StartApps`` puts in front of a classic app's
# relative path. Resolving them lets Xenon launch the executable directly
# instead of going through the shell.
_KNOWN_FOLDERS: dict[str, tuple[str, ...]] = {
    "{6d809377-6af0-444b-8957-a3773f02200e}": ("ProgramFiles",),
    "{7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e}": ("ProgramFiles(x86)",),
    "{905e63b6-c1bf-494e-b29c-65b732d3d21a}": ("ProgramFiles",),
    "{f38bf404-1d43-42f2-9305-67de0b28fc23}": ("SystemRoot",),
    "{1ac14e77-02e7-4e5d-b744-2eb1ae5198b7}": ("SystemRoot", "System32"),
    "{d65231b0-b2f1-4857-a4ce-a8e7c6ea7d27}": ("SystemRoot", "SysWOW64"),
}

_NOISY_WORDS = (
    "uninstall",
    "readme",
    "release notes",
    "documentation",
    "help",
    "website",
    "manual",
)


@dataclass(frozen=True)
class AppEntry:
    """One launchable item discovered on this machine."""

    name: str
    app_id: str = ""
    path: str = ""


@dataclass(frozen=True)
class ResolvedApp:
    """The best launch target for a spoken name."""

    name: str
    app_id: str = ""
    path: str = ""
    target: str = ""
    source: str = ""


def _normalize(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\.(exe|lnk|url)$", "", value)
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _candidate_names(query: str) -> list[str]:
    normalized = _normalize(query)
    names = [normalized]
    names.extend(SEARCH_NAMES.get(normalized, ()))
    for name in tuple(names):
        if name.startswith("microsoft "):
            names.append(name[len("microsoft ") :])
        if name.startswith("google "):
            names.append(name[len("google ") :])
    return list(dict.fromkeys(name for name in names if name))


def _entry_keys(entry: AppEntry) -> list[str]:
    keys = [_normalize(entry.name)]
    if entry.path:
        keys.append(_normalize(Path(entry.path).stem))
    if entry.app_id:
        tail = entry.app_id.rsplit("\\", 1)[-1]
        tail = tail.split("!", 1)[0]
        keys.append(_normalize(tail))
    return list(dict.fromkeys(key for key in keys if key))


def _score(entry: AppEntry, candidates: Sequence[str]) -> int:
    best = 0
    for key in _entry_keys(entry):
        for candidate in candidates:
            if key == candidate:
                return 100
            if key.startswith(candidate + " "):
                best = max(best, 85)
            if key.endswith(" " + candidate):
                best = max(best, 75)
            if candidate.startswith(key + " "):
                best = max(best, 65)
            key_tokens = set(key.split())
            if candidate and set(candidate.split()).issubset(key_tokens):
                best = max(best, 55)
    if best and any(word in _normalize(entry.name) for word in _NOISY_WORDS):
        best -= 40
    if entry.app_id.endswith("!App"):
        best += 2
    # A path under WindowsApps is a packaged executable and often cannot be
    # started directly; its AUMID is the reliable launch target.
    if entry.path and "\\windowsapps\\" in entry.path.lower():
        best -= 20
    return best


def _path_from_app_id(app_id: str) -> str:
    match = re.match(r"^\{(?P<guid>[^}]+)\}\\(?P<rest>.+)$", app_id)
    if match is None:
        return ""
    parts = _KNOWN_FOLDERS.get("{" + match.group("guid").lower() + "}")
    if parts is None:
        return ""
    bases = [os.environ.get(part, "") for part in parts]
    rest = match.group("rest")
    for base in bases:
        if not base:
            continue
        candidate = Path(base, *rest.split("\\"))
        if candidate.exists():
            return str(candidate)
    return ""


def _which(name: str, which: Callable[[str], str | None]) -> str:
    for candidate in (name, f"{name}.exe"):
        found = which(candidate)
        if found:
            return found
    return ""


def _system_exe(name: str) -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    stem = name if name.lower().endswith(".exe") else f"{name}.exe"
    for folder in ("System32", ""):
        candidate = Path(root, folder, stem) if folder else Path(root, stem)
        if candidate.exists():
            return str(candidate)
    return ""


class AppCatalog:
    """Lazily discovered app catalog with an injectable loader for tests."""

    def __init__(
        self,
        entries: Iterable[AppEntry] | None = None,
        loader: Callable[[], Iterable[AppEntry]] | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self._entries: list[AppEntry] = list(entries or ())
        self._loader = loader
        self._loaded = entries is not None
        self._which = which or shutil.which

    @property
    def entries(self) -> tuple[AppEntry, ...]:
        self._ensure()
        return tuple(self._entries)

    def prewarm(self) -> None:
        """Discover now so the first real command does not pay for it."""
        self._ensure()

    def _ensure(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._loader is None:
            return
        try:
            self._entries.extend(self._loader())
        except Exception:
            # Discovery is a convenience; a broken registry or a missing
            # PowerShell must not stop the fallback launch path.
            pass

    def resolve(self, query: str) -> ResolvedApp | None:
        query = query.strip()
        if not query:
            return None

        candidates = _candidate_names(query)
        best_entry: AppEntry | None = None
        best_score = 0
        for entry in self.entries:
            score = _score(entry, candidates)
            if score > best_score or (
                score == best_score
                and score > 0
                and entry.app_id
                and best_entry is not None
                and not best_entry.app_id
            ):
                best_entry, best_score = entry, score
        if best_entry is not None and best_score >= 55:
            app_id = best_entry.app_id
            direct = _path_from_app_id(app_id) if app_id else ""
            if direct:
                return ResolvedApp(
                    best_entry.name, path=direct, source="start-menu path"
                )
            if app_id:
                return ResolvedApp(
                    best_entry.name, app_id=app_id, source="start-menu app id"
                )
            if best_entry.path:
                return ResolvedApp(
                    best_entry.name, path=best_entry.path, source="app path"
                )

        executable = _which(query, self._which)
        if executable:
            return ResolvedApp(Path(executable).stem, path=executable, source="PATH")

        system = _system_exe(query)
        if system:
            return ResolvedApp(Path(system).stem, path=system, source="System32")

        lowered = _normalize(query)
        url = WEB_APPS.get(lowered)
        if url:
            return ResolvedApp(lowered.title(), target=url, source="web")

        if "://" in query or re.match(r"^[a-z][a-z0-9+.-]*:", query, re.I):
            return ResolvedApp(query, target=query, source="shell")

        direct_path = Path(query).expanduser()
        if direct_path.exists():
            return ResolvedApp(direct_path.stem, path=str(direct_path), source="path")

        return None


def launch(
    resolved: ResolvedApp,
    startfile: Callable[[str], object] | None = None,
    spawn: Callable[[list[str]], object] | None = None,
) -> str:
    """Start a resolved app. Returns a short human-readable detail."""
    startfile = startfile or os.startfile
    if resolved.app_id:
        command = ["explorer.exe", f"shell:AppsFolder\\{resolved.app_id}"]
        if spawn is not None:
            spawn(command)
        else:
            subprocess.Popen(command, creationflags=_creation_flags())
        return f"launched {resolved.name} via its Start-menu app id"
    if resolved.path:
        startfile(resolved.path)
        return f"launched {resolved.path}"
    if resolved.target:
        startfile(resolved.target)
        return f"launched {resolved.target}"
    raise OSError("nothing to launch")


def _creation_flags() -> int:
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


# --------------------------------------------------------------------------
# Default discovery
# --------------------------------------------------------------------------


def _start_menu_roots() -> list[Path]:
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("PROGRAMDATA")
    if appdata:
        roots.append(
            Path(appdata, "Microsoft", "Windows", "Start Menu", "Programs")
        )
    if programdata:
        roots.append(Path(programdata, "Microsoft", "Windows", "Start Menu", "Programs"))
    return roots


def _shortcut_entries() -> list[AppEntry]:
    entries: list[AppEntry] = []
    for root in _start_menu_roots():
        if not root.exists():
            continue
        for suffix in ("*.lnk", "*.url"):
            for shortcut in root.rglob(suffix):
                name = shortcut.stem.strip()
                if not name or any(word in name.lower() for word in _NOISY_WORDS):
                    continue
                entries.append(AppEntry(name=name, path=str(shortcut)))
    return entries


def _registry_entries() -> list[AppEntry]:
    try:
        import winreg
    except ImportError:
        return []

    entries: list[AppEntry] = []
    locations = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
        ),
    )
    for root, location in locations:
        try:
            key = winreg.OpenKey(root, location)
        except OSError:
            continue
        with key:
            try:
                count = winreg.QueryInfoKey(key)[0]
            except OSError:
                continue
            for index in range(count):
                try:
                    name = winreg.EnumKey(key, index)
                    with winreg.OpenKey(key, name) as subkey:
                        value, _ = winreg.QueryValueEx(subkey, "")
                except OSError:
                    continue
                if value:
                    entries.append(AppEntry(name=name, path=value))
    return entries


def _start_apps_entries(timeout: float = 8.0) -> list[AppEntry]:
    if sys.platform != "win32":
        return []
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []

    entries: list[AppEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name", "") or "").strip()
        app_id = str(item.get("AppID", "") or "").strip()
        if name and app_id:
            entries.append(AppEntry(name=name, app_id=app_id))
    return entries


def _dedupe(entries: Iterable[AppEntry]) -> list[AppEntry]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[AppEntry] = []
    for entry in entries:
        key = (_normalize(entry.name), entry.app_id, entry.path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _cache_path() -> Path:
    configured = os.environ.get("XENON_APP_CACHE")
    if configured is not None:
        return Path(configured).expanduser() if configured else Path()
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local, "Xenon", "apps.json")


def _read_cache(path: Path) -> list[AppEntry] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    created = payload.get("created")
    if not isinstance(created, (int, float)) or time.time() - created > CACHE_TTL_S:
        return None
    raw = payload.get("entries")
    if not isinstance(raw, list):
        return None
    entries: list[AppEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entries.append(
            AppEntry(
                name=str(item.get("name", "") or ""),
                app_id=str(item.get("app_id", "") or ""),
                path=str(item.get("path", "") or ""),
            )
        )
    return entries or None


def _write_cache(path: Path, entries: Sequence[AppEntry]) -> None:
    if not str(path):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"created": time.time(), "entries": [asdict(entry) for entry in entries]}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def default_entries() -> list[AppEntry]:
    """Discover apps once and reuse the result for a few hours."""
    cache = _cache_path()
    if str(cache):
        cached = _read_cache(cache)
        if cached is not None:
            return cached

    entries = _dedupe(
        [*_registry_entries(), *_shortcut_entries(), *_start_apps_entries()]
    )
    if str(cache):
        _write_cache(cache, entries)
    return entries


_DEFAULT_CATALOG = AppCatalog(loader=default_entries)


def default_catalog() -> AppCatalog:
    return _DEFAULT_CATALOG
