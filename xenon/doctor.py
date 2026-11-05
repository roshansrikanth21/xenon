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
            "answers are disabled; set XENON_MODEL_API_KEY and XENON_MODEL_NAME",
        )

    url = config.base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {config.api_key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.load(response)
    except Exception as error:
        return _check(
            "model",
            FAIL,
            f"cannot reach {url}: {error}. Start the server (for example "
            "`ollama serve`) or fix XENON_MODEL_BASE_URL.",
        )

    models = payload.get("data") if isinstance(payload, dict) else None
    ids = [
        item.get("id")
        for item in (models or [])
        if isinstance(item, dict) and item.get("id")
    ]
    if ids and config.model not in ids:
        return _check(
            "model",
            WARN,
            f"{url} is reachable but {config.model!r} is not listed "
            f"(available: {', '.join(ids[:5])})",
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


def run_checks(
    observe: Callable[[], object] | None,
    model: ChatConfig | None,
    db_path: str | Path,
    skills_root: str | Path,
) -> list[Check]:
    checks = [check_python(), check_screen(observe), check_model(model)]
    checks.append(check_memory(db_path))
    checks.extend(check_voice())
    checks.append(check_skills(skills_root))
    return checks


def exit_code(checks: list[Check]) -> int:
    return 1 if any(check.status == FAIL for check in checks) else 0
