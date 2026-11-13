"""One-command health check: the parts that usually break, in one report.

The doctor never changes anything and never sends screen contents anywhere. It
answers the recurring "why is nothing working?" question in order: is this the
right platform, can Xenon see the screen, is the model reachable, does memory
open, and are the voice dependencies installed.
"""
from __future__ import annotations

import importlib.util
import json
import platform
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from xenon.chat import ChatConfig
from xenon.memory import MemoryStore

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _check(name: str, status: str, detail: str) -> Check:
    return Check(name=name, status=status, detail=detail)


def check_python() -> Check:
    version = platform.python_version()
    status = PASS if sys.version_info >= (3, 11) else WARN
    detail = f"Python {version} on {platform.system()}"
    if status == WARN:
        detail += "; Xenon expects 3.11 or newer"
    return _check("python", status, detail)


def check_screen(observe: Callable[[], object] | None) -> Check:
    if observe is None:
        return _check("screen", WARN, "not checked here; run on Windows")
    try:
        context = observe()
    except Exception as error:
        return _check(
            "screen",
            FAIL,
            f"cannot read the foreground window: {type(error).__name__}: {error}",
        )

    app = getattr(context, "app_name", "?")
    title = getattr(context, "title", "?")
    text = getattr(context, "text", "")
    error = getattr(context, "error", "")
    source = "screenshot" if getattr(context, "used_vision", False) else "UI Automation"
    summary = f"{app} — {title}; {source}, {len(text)} chars"
    if error:
        return _check("screen", WARN, f"{summary}; limited: {error}")
    if not text.strip():
        return _check("screen", WARN, f"{summary}; no readable content")
    return _check("screen", PASS, summary)


def check_model(config: ChatConfig | None, timeout_s: float = 5.0) -> Check:
    if config is None:
        return _check(
            "model",
            WARN,
            "answers are disabled; export a free key such as GROQ_API_KEY or "
            "GEMINI_API_KEY, or set XENON_MODEL_PROVIDER=ollama "
            "(see `python -m xenon providers`)",
        )

    url = config.base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "User-Agent": "xenon-assistant/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            hint = "the API key was rejected; check the key for this provider"
        elif error.code == 429:
            hint = "the provider is rate limiting; try again in a moment"
        else:
            hint = "the endpoint answered with an error; check XENON_MODEL_BASE_URL"
        return _check("model", FAIL, f"{url} returned HTTP {error.code}: {hint}")
    except Exception as error:
        return _check(
            "model",
            FAIL,
            f"cannot reach {url}: {error}. Start the server (for example "
            "`ollama serve`) or fix XENON_MODEL_BASE_URL.",
        )

    models = payload.get("data") if isinstance(payload, dict) else None
    ids = [
        # Gemini's OpenAI-compatible listing prefixes ids with "models/".
        str(item.get("id")).removeprefix("models/")
        for item in (models or [])
        if isinstance(item, dict) and item.get("id")
    ]
    if ids and config.model.removeprefix("models/") not in ids:
        # Free-tier defaults get renamed or retired; suggest the closest names.
        family = config.model.split("-")[0].split("/")[-1].lower()
        similar = [model_id for model_id in ids if family and family in model_id.lower()]
        shown = (similar or ids)[:6]
        return _check(
            "model",
            WARN,
            f"{url} is reachable but {config.model!r} is not listed; set "
            f"XENON_MODEL_NAME to one of: {', '.join(shown)}",
        )
    return _check("model", PASS, f"{config.model} via {config.base_url}")


def check_memory(db_path: str | Path) -> Check:
    try:
        with MemoryStore(db_path) as store:
            count = store.count_documents()
    except Exception as error:
        return _check(
            "memory", FAIL, f"cannot open {db_path}: {type(error).__name__}: {error}"
        )
    return _check("memory", PASS, f"{db_path} ({count} documents)")


def check_voice() -> list[Check]:
    checks: list[Check] = []
    for module, extra in (
        ("sounddevice", "voice"),
        ("faster_whisper", "voice"),
        ("win32com", "voice"),
    ):
        found = importlib.util.find_spec(module) is not None
        checks.append(
            _check(
                f"voice:{module}",
                PASS if found else WARN,
                "installed" if found else f"missing; pip install -e \".[{extra}]\"",
            )
        )

    if importlib.util.find_spec("sounddevice") is None:
        checks.append(
            _check(
                "microphone",
                WARN,
                "not checked; install the voice extras to list input devices",
            )
        )
        return checks
    try:
        import sounddevice

        devices = sounddevice.query_devices()
        inputs = [device for device in devices if device.get("max_input_channels", 0) > 0]
    except Exception as error:
        checks.append(_check("microphone", FAIL, f"cannot list devices: {error}"))
        return checks
    checks.append(
        _check(
            "microphone",
            PASS if inputs else WARN,
            f"{len(inputs)} input devices; choose one with `voice --mic <index|name>`",
        )
    )
    return checks


def check_skills(root: str | Path) -> Check:
    from xenon.skills import SkillRegistry

    try:
        skills = SkillRegistry(Path(root)).catalog()
    except Exception as error:
        return _check("skills", FAIL, f"cannot load {root}: {error}")
    return _check("skills", PASS, f"{len(skills)} skills in {root}")


def check_agent_desktop(
    locate: Callable[[], tuple[str | None, str | None, str]] | None = None,
    version: Callable[[str], str] | None = None,
    leftovers: Callable[[], int] | None = None,
) -> Check:
    """agent-desktop: installed, the pinned build, its version, leftover refmaps.

    An unverified binary is never run, not even for ``version``: running it
    is exactly what the pin exists to prevent.
    """
    from xenon import desktop

    found, verified, detail = (locate or desktop.locate_exe)()
    if found is None:
        return _check("agent-desktop", WARN, "not installed (npm install -g agent-desktop); "
                      "desktop control falls back to in-house UI Automation")
    if verified is None:
        return _check("agent-desktop", FAIL, f"{detail}; agent-desktop is disabled until the "
                      "binary matches the pinned 1.2.0 build")
    try:
        reported = (version or (lambda path: desktop.AgentDesktop(exe=path).version()))(verified)
    except Exception as error:
        reported = ""
        detail += f"; version check failed: {type(error).__name__}"
    try:
        count = (leftovers or desktop.leftover_snapshots)()
    except Exception:
        count = -1
    status = PASS
    parts = [f"agent-desktop {reported or '?'}", detail]
    if reported and not reported.startswith(desktop.SUPPORTED_MAJOR_MINOR):
        status = WARN
        parts.append("unsupported version (want 1.2.x)")
    if count > 0:
        status = WARN if status == PASS else status
        parts.append(f"{count} leftover snapshot dirs (swept at startup when older than an hour)")
    return _check("agent-desktop", status, ", ".join(parts))


def check_elevation(is_elevated: Callable[[], bool] | None = None) -> Check:
    from xenon import killswitch

    elevated = (is_elevated or killswitch.is_elevated)()
    if elevated:
        return _check("elevation", WARN, "running as administrator: agent features are refused; "
                      "start Xenon from a normal (non-admin) prompt")
    return _check("elevation", PASS, "not elevated")


def check_kill_flags() -> Check:
    from xenon import killswitch

    agent = killswitch.feature_enabled("AGENT")
    outward = killswitch.feature_enabled("OUTWARD")
    detail = f"XENON_AGENT={'on' if agent else 'off'}, XENON_OUTWARD={'on' if outward else 'off'}"
    return _check("kill flags", PASS, detail)


def check_network(public: Callable[[], bool | None] | None = None) -> Check:
    if public is None:
        from xenon.companion import network_is_public as public
    try:
        value = public()
    except Exception:
        value = None
    import os

    lan = os.environ.get("XENON_COMPANION_LAN", "").strip().lower()
    if lan in {"0", "off", "false", "no"}:
        return _check("network", PASS, "tablet page on this PC only (XENON_COMPANION_LAN=0)")
    if lan in {"1", "on", "true", "yes", "force"}:
        return _check("network", WARN, "tablet page open to the whole LAN (XENON_COMPANION_LAN=1)")
    if value is True:
        return _check("network", WARN, "Public profile: the tablet page answers only this Wi-Fi's own "
                      "subnet, read-only (XENON_COMPANION_LAN=1 opens it, =0 keeps it on this PC)")
    if value is False:
        return _check("network", PASS, "Private/Domain profile: the tablet page is on the LAN, read-only")
    return _check("network", WARN, "profile unknown: the tablet page answers only this Wi-Fi's own subnet")


def run_checks(
    observe: Callable[[], object] | None,
    model: ChatConfig | None,
    db_path: str | Path,
    skills_root: str | Path,
    agent_desktop: Callable[[], Check] | None = None,
    elevation: Callable[[], Check] | None = None,
    network: Callable[[], Check] | None = None,
) -> list[Check]:
    checks = [check_python(), check_screen(observe), check_model(model)]
    checks.append(check_memory(db_path))
    checks.extend(check_voice())
    checks.append(check_skills(skills_root))
    checks.append((agent_desktop or check_agent_desktop)())
    checks.append((elevation or check_elevation)())
    checks.append(check_kill_flags())
    checks.append((network or check_network)())
    return checks


def exit_code(checks: list[Check]) -> int:
    return 1 if any(check.status == FAIL for check in checks) else 0
