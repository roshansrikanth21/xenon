"""Xenon command line: ``python -m xenon screen`` / ``memory``."""
from __future__ import annotations

import argparse
import os
import re
import sys

from xenon import fastpath, websearch
from xenon.actions import ApprovalPolicy, Risk, TaskRunner
from xenon.assistant import Assistant
from xenon.chat import ChatUnavailable, OpenAICompatibleChat
from xenon.chat import config_from_env as chat_config_from_env
from xenon.exports import ExportFormatError, ingest_export
from xenon.memory import MemoryStore
from xenon.notion import (
    NotionClient,
    NotionConfig,
    NotionUnavailable,
    config_from_env as notion_config_from_env,
    sync_to_memory,
)
from xenon.planner import ModelPlanner, PlannerError
from xenon.screen import ScreenObserver
from xenon.skills import SkillError, SkillRegistry
from xenon.vision import OpenAICompatibleVision, VisionUnavailable, config_from_env

DEFAULT_DB_PATH = "memory/xenon.db"


def is_windows() -> bool:
    """Platform guard, separate from ``sys`` so tests can patch just this.

    Patching ``sys.platform`` globally would make stdlib modules such as ``ssl``
    take a Windows code path on Linux, which is a real bug, not a test detail.
    """
    return sys.platform == "win32"


class UnconfiguredVision:
    """Vision provider that explains how to enable the fallback."""

    def describe(self, frame, snapshot) -> str:
        raise VisionUnavailable(
            "local OCR is disabled and no vision model is configured; set "
            "XENON_OCR=1, or set XENON_VISION_API_KEY and XENON_VISION_MODEL"
        )


def _vision_provider():
    """Build the fallback chain: local OCR first, then a configured model."""
    from xenon.ocr import VisionChain, WindowsOcr, local_ocr_enabled

    providers: list[object] = []
    if local_ocr_enabled():
        providers.append(WindowsOcr())
    config = config_from_env()
    if config is not None:
        providers.append(OpenAICompatibleVision(config))
    if not providers:
        return UnconfiguredVision()
    return VisionChain(providers)


def _observer():
    """Build the observer and pay the COM warm-up now, not on the first request."""
    from xenon.windows import WindowsUiAutomation

    automation = WindowsUiAutomation()
    observer = ScreenObserver(automation, _vision_provider(), capture=automation)
    observer.prewarm()
    return observer


def _doctor(args: argparse.Namespace) -> int:
    from xenon.doctor import exit_code, run_checks

    observe = None
    if is_windows():
        observe = lambda: _observer().observe()

    checks = run_checks(
        observe,
        chat_config_from_env(),
        os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH),
        _skills_root(),
    )
    for check in checks:
        print(f"{check.status.upper():4}  {check.name:18} {check.detail}")
    failed = sum(1 for check in checks if check.status == "fail")
    warned = sum(1 for check in checks if check.status == "warn")
    print(f"\n{len(checks) - failed - warned} ok, {warned} warning(s), {failed} failure(s)")
    return exit_code(checks)


def _screen(args: argparse.Namespace) -> int:
    if not is_windows():
        print("xenon screen requires Windows", file=sys.stderr)
        return 2
    context = _observer().observe()
    print(f"app: {context.app_name}")
    print(f"title: {context.title}")
    print(f"source: {'screenshot' if context.used_vision else 'ui-automation'}"
          + (" (limited)" if context.error else ""))
    if context.error:
        print(f"warning: {context.error}")
    if context.text:
        print("--- context ---")
        print(context.text)
    return 0


def _ask(args: argparse.Namespace) -> int:
    if not is_windows():
        print("xenon ask requires Windows for screen context", file=sys.stderr)
        return 2

    question = _without_wake(" ".join(args.question))
    config = chat_config_from_env()
    if config is None:
        print(
            "no model configured; export GROQ_API_KEY or GEMINI_API_KEY (or set XENON_MODEL_PROVIDER=ollama) to enable answers. See `python -m xenon providers`.",
            file=sys.stderr,
        )
        return 2

    skill = None
    if args.skill:
        try:
            skill = SkillRegistry(_skills_root()).load(args.skill)
        except SkillError as error:
            print(f"skill error: {error}", file=sys.stderr)
            return 2

    with MemoryStore(os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH)) as store:
        assistant = Assistant(_observer(), store, OpenAICompatibleChat(config))
        try:
            answer = assistant.ask(question, skill=skill)
        except (ChatUnavailable, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    print(answer.text)
    if answer.memories:
        print("\nsources:")
        for memory in answer.memories:
            print(f"- {memory.title} ({memory.source_kind}: {memory.path})")
    source = "screenshot" if answer.used_vision else "ui-automation"
    if answer.screen_error:
        source += " (limited)"
    print(f"\n[{answer.latency_ms:.0f} ms, screen via {source}]")
    return 0


def _ui(args: argparse.Namespace) -> int:
    if not is_windows():
        print("xenon ui requires Windows", file=sys.stderr)
        return 2

    config = chat_config_from_env()
    if config is None:
        print(
            "no model configured; export GROQ_API_KEY or GEMINI_API_KEY (or set XENON_MODEL_PROVIDER=ollama) to use the command box. See `python -m xenon providers`.",
            file=sys.stderr,
        )
        return 2

    from xenon.ui import CommandBox

    store = MemoryStore(os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH))
    assistant = Assistant(_observer(), store, OpenAICompatibleChat(config))

    def ask(question: str) -> str:
        answer = assistant.ask(question)
        text = answer.text
        if answer.memories:
            text += "\n\nsources:\n" + "\n".join(
                f"- {memory.title} ({memory.source_kind}: {memory.path})"
                for memory in answer.memories
            )
        return text

    try:
        CommandBox(ask, topmost=not args.no_topmost).run()
    finally:
        store.close()
    return 0


def _voice(args: argparse.Namespace) -> int:
    if not is_windows():
        print("xenon voice requires Windows for speech and screen context", file=sys.stderr)
        return 2

    if getattr(args, "list_mics", False):
        try:
            import sounddevice as sd
        except Exception as error:
            print(f"sounddevice unavailable: {error}", file=sys.stderr)
            return 1
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_input_channels", 0)) > 0:
                print(f"{index}: {device.get('name')} (inputs={device.get('max_input_channels')})")
        return 0

    config = chat_config_from_env()
    if config is None:
        print(
            "no model configured; export GROQ_API_KEY or GEMINI_API_KEY (or set XENON_MODEL_PROVIDER=ollama) to answer by voice. See `python -m xenon providers`.",
            file=sys.stderr,
        )
        return 2

    mic_device = getattr(args, "mic", None)
    if mic_device is None:
        mic_device = os.environ.get("XENON_MIC_DEVICE")
    if mic_device is not None:
        try:
            mic_device = int(mic_device)
        except (TypeError, ValueError):
            pass

    vad_threshold = getattr(args, "threshold", None)
    if vad_threshold is None:
        raw_threshold = os.environ.get("XENON_VAD_THRESHOLD")
        if raw_threshold is not None:
            try:
                vad_threshold = float(raw_threshold)
            except (TypeError, ValueError):
                vad_threshold = None
    if vad_threshold is None:
        vad_threshold = 0.015

    silence_ms = getattr(args, "silence_ms", None)
    if silence_ms is None:
        raw_silence = os.environ.get("XENON_SILENCE_MS")
        if raw_silence is not None:
            try:
                silence_ms = int(raw_silence)
            except (TypeError, ValueError):
                silence_ms = None
    if silence_ms is None:
        silence_ms = 350
    silence_ms = min(max(silence_ms, 120), 2000)

    # Hands-free is the default: the microphone stays open and a sentence is
    # dispatched as soon as it is followed by a short silence. ``--wake`` (or
    # XENON_WAKE_MODE=wake) restores the wake-word requirement.
    always = os.environ.get("XENON_WAKE_MODE", "always").strip().lower() != "wake"
    if getattr(args, "always", False):
        always = True
    if getattr(args, "wake", False):
        always = False
    if os.environ.get("XENON_ALWAYS_LISTEN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        always = True

    beam_size = 1
    raw_beam = os.environ.get("XENON_WHISPER_BEAM")
    if raw_beam is not None:
        try:
            beam_size = int(raw_beam)
        except (TypeError, ValueError):
            pass

    from xenon.audio import (
        MicrophoneSource,
        SapiSpeaker,
        UtteranceDetector,
        WhisperTranscriber,
        listen_once,
    )
    from xenon.voice import VoiceLoop
    from xenon.wake import WakeGate

    wake_word = os.environ.get("XENON_WAKE_WORD", "xenon")
    aliases = tuple(
        part.strip()
        for part in os.environ.get("XENON_WAKE_ALIASES", "").split(",")
        if part.strip()
    )

    # Build the floating indicator first so the user gets immediate feedback.
    indicator = None
    try:
        from xenon.ui import VoiceIndicator

        indicator = VoiceIndicator()
        indicator.root.deiconify()
        indicator.root.lift()
        indicator.root.attributes("-topmost", True)
        indicator.root.focus_force()
        print("status window opened", flush=True)
    except Exception as error:  # pragma: no cover - environment dependent
        print(f"voice indicator unavailable: {error}", file=sys.stderr)

    _voice_state: dict = {"indicator": indicator}

    def _ind() -> object | None:
        return _voice_state["indicator"]

    import threading

    stop_event = threading.Event()

    # Terminal-only fallback when Tk is unavailable: everything stays on this thread.
    if indicator is None:
        try:
            speaker = SapiSpeaker()
        except Exception as error:
            print(f"speech output unavailable: {error}", file=sys.stderr)
            return 1
        source = MicrophoneSource(device=mic_device)
        detector = UtteranceDetector(
            threshold=vad_threshold, silence_ms=silence_ms, min_speech_ms=200
        )
        transcriber = WhisperTranscriber(
            os.environ.get("XENON_WHISPER_MODEL", "base.en"),
            os.environ.get("XENON_WHISPER_DEVICE", "cpu"),
            os.environ.get("XENON_WHISPER_COMPUTE", "int8"),
            initial_prompt=os.environ.get(
                "XENON_WHISPER_PROMPT",
                _whisper_hint(wake_word),
            ),
            beam_size=beam_size,
        )
        model = OpenAICompatibleChat(config)
        print("warming the model so the first answer is not a cold start...", flush=True)
        if not model.warm_up():
            print(
                "model warm-up failed; the first answer may be slow or fail",
                file=sys.stderr,
            )
        from xenon.apps import default_catalog

        default_catalog().prewarm()
        store = MemoryStore(os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH))
        assistant = Assistant(
            _observer(),
            store,
            model,
            max_memories=2,
            max_context_chars=1800,
        )

        def handle(command: str) -> str:
            print(f"you: {command}")
            fast_reply = _voice_fast_reply(command)
            if fast_reply is not None:
                print(f"xenon (fast path): {fast_reply}")
                return _speakable(fast_reply)
            answer = assistant.ask(command, brief=True)
            print(f"xenon: {answer.text}")
            return _speakable(answer.text)

        def transcribe() -> str | None:
            text = listen_once(source, detector, transcriber)
            if text:
                print(f"heard: {text!r}", flush=True)
            return text

        loop = VoiceLoop(
            gate=WakeGate(wake_word, aliases=aliases, always=always),
            transcribe=transcribe,
            speak=speaker.speak,
            on_command=handle,
        )
        mode = "always listening" if always else f"wake word: {wake_word}"
        print(f"{mode}; press Ctrl+C to stop", flush=True)
        try:
            while True:
                loop.tick()
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            source.close()
            store.close()
        return 0

    # GUI path: run the whole audio stack inside the worker thread so the SQLite
    # connection and COM objects stay on one thread.
    def worker() -> None:
        _worker_state: dict = {"last_transcript": None}
        # expose via closure scope
        globals()["_worker_state"] = _worker_state
        try:
            speaker = SapiSpeaker()
        except Exception as error:
            if indicator is not None:
                indicator.set("error", str(error))
            print(f"speech output unavailable: {error}", file=sys.stderr)
            stop_event.set()
            return
        source = MicrophoneSource(device=mic_device)
        detector = UtteranceDetector(
            threshold=vad_threshold, silence_ms=silence_ms, min_speech_ms=200
        )
        transcriber = WhisperTranscriber(
            os.environ.get("XENON_WHISPER_MODEL", "base.en"),
            os.environ.get("XENON_WHISPER_DEVICE", "cpu"),
            os.environ.get("XENON_WHISPER_COMPUTE", "int8"),
            initial_prompt=os.environ.get(
                "XENON_WHISPER_PROMPT",
                _whisper_hint(wake_word),
            ),
            beam_size=beam_size,
        )
        model = OpenAICompatibleChat(config)
        ind = _ind()
        if ind is not None:
            ind.set("working", "warming model")
        print("warming the model so the first answer is not a cold start...", flush=True)
        if model.warm_up():
            print("model warm", flush=True)
        else:
            print(
                "model warm-up failed; the first answer may be slow or fail",
                file=sys.stderr,
            )
        from xenon.apps import default_catalog

        if ind is not None:
            ind.set("working", "indexing apps")
        default_catalog().prewarm()
        store = MemoryStore(os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH))
        assistant = Assistant(
            _observer(),
            store,
            model,
            max_memories=2,
            max_context_chars=1800,
        )

        def handle(command: str) -> str:
            ind = _ind()
            if ind is not None:
                ind.set("working", command)
            print(f"you: {command}")
            fast_reply = _voice_fast_reply(command)
            if fast_reply is not None:
                print(f"xenon (fast path): {fast_reply}")
                return _speakable(fast_reply)
            answer = assistant.ask(command, brief=True)
            print(f"xenon: {answer.text}")
            ind = _ind()
            if ind is not None:
                ind.set("speaking", answer.text[:120])
            return _speakable(answer.text)

        def transcribe() -> str | None:
            ind = _ind()
            if ind is not None:
                ind.set("listening", "always listening" if always else f"wake word: {wake_word}")
            text = listen_once(source, detector, transcriber)
            if text:
                print(f"heard: {text!r}", flush=True)
                ind = _ind()
                if ind is not None:
                    ind.set("heard", text)
            return text

        def speak(text: str) -> None:
            ind = _ind()
            if ind is not None:
                ind.set("speaking", text[:120])
            speaker.speak(_speakable(text))

        loop = VoiceLoop(
            gate=WakeGate(wake_word, aliases=aliases, always=always),
            transcribe=transcribe,
            speak=speak,
            on_command=handle,
        )

        if indicator is not None:
            indicator.set(
                "listening",
                "always listening" if always else f"wake word: {wake_word}",
            )
        mode = "always listening" if always else f"wake word: {wake_word}"
        print(f"{mode}; close the status window to stop", flush=True)
        try:
            while not stop_event.is_set():
                loop.tick()
        except Exception as error:
            if indicator is not None:
                indicator.set("error", str(error))
            print(f"voice loop error: {error}", file=sys.stderr)
        finally:
            source.close()
            store.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        indicator.run_mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        thread.join(timeout=5)
    print("stopped")
    return 0


_NO_MODEL = (
    "No model configured. Export GROQ_API_KEY or GEMINI_API_KEY (or set "
    "XENON_MODEL_PROVIDER=ollama), then restart the orb."
)
_SCREEN_QUESTION = (
    "Look at what is on my screen and tell me the single most useful thing "
    "I should do next, with the concrete step."
)


class _OrbBackend:
    """Built on the orb's worker thread, so SQLite and COM live there."""

    def __init__(self, config) -> None:
        self._store = MemoryStore(os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH))
        self._chat = OpenAICompatibleChat(config) if config is not None else None
        self._assistant = (
            Assistant(_observer(), self._store, OpenAICompatibleChat(config))
            if config is not None
            else None
        )
        if config is not None:
            OpenAICompatibleChat(config).warm_up()

    def _answer(self, question: str):
        from xenon.orb import OrbReply

        if self._assistant is None:
            raise ChatUnavailable(_NO_MODEL)
        answer = self._assistant.ask(question)
        sources = [
            f"{memory.title} ({memory.source_kind})" for memory in answer.memories
        ]
        via = "screenshot" if answer.used_vision else "UI Automation"
        # Every Assistant answer is built from the screen (and often memory),
        # so the tablet shows "Private" instead of it.
        return OrbReply(
            answer.text, sources, f"{answer.latency_ms / 1000:.1f}s · {via}", private=True
        )

    def ask(self, text: str):
        from xenon.orb import OrbReply

        text = _without_wake(text)
        fast, understood = _smart_reply(text, self._chat)
        if fast is not None:
            return OrbReply(fast, note=f"did: {understood}" if understood else "fast path")
        return self._answer(text)

    def screen(self):
        return self._answer(_SCREEN_QUESTION)

    def search(self, query: str):
        from xenon.orb import OrbLink

        results = websearch.search(query, limit=5)
        return [OrbLink(r.title, r.url, re.sub(r"\s+", " ", r.snippet).strip()) for r in results]

    def close(self) -> None:
        self._store.close()


# ---------------------------------------------------------------- tasks --
# "do <goal>" from the Ask pill, voice or the CLI hand-off runs here, on the
# orb's TaskManager thread: the existing ModelPlanner plans, every step is
# classified against the live foreground, cards come from the broker, and
# anything OUTWARD is committed by gate.py with the approved token.

# Friendly names Roshan uses -> the process image they run as. Builds the
# task's app allowlist from his own words. Common English words ("code",
# "files") are left out: "type code in notepad" must not allowlist VS Code.
_APP_ALIASES = {
    "notepad": "notepad.exe", "paint": "mspaint.exe", "calculator": "CalculatorApp.exe",
    "calc": "CalculatorApp.exe", "wordpad": "wordpad.exe", "word": "WINWORD.EXE",
    "excel": "EXCEL.EXE", "powerpoint": "POWERPNT.EXE", "explorer": "explorer.exe",
    "vscode": "Code.exe", "spotify": "Spotify.exe",
    "obsidian": "Obsidian.exe", "settings": "SystemSettings.exe", "whatsapp": "WhatsApp.Root.exe",
    "telegram": "Telegram.exe", "discord": "Discord.exe", "teams": "ms-teams.exe",
    "slack": "slack.exe", "signal": "Signal.exe", "outlook": "olk.exe", "chrome": "chrome.exe",
    "edge": "msedge.exe", "firefox": "firefox.exe", "brave": "brave.exe", "opera": "opera.exe",
}
_KEYBOARD_KINDS = frozenset({"type_text", "press_keys"})
# Words that ask for a message to go out even though OUTWARD_PATTERN (which
# reads button names) does not list them: "tell mom on whatsapp hi" is one
# outward intent. Each such action still needs its own card and click; this
# only sets how many cards the task may raise.
_MESSAGE_VERBS = re.compile(
    r"\b(?:tell|message|msg|dm|ping|email|text|say|respond|answer|wish|"
    # ("reply" is already in OUTWARD_PATTERN, so it is not counted twice.)
    # "whatsapp mom that I'm late": a messenger used as the verb.
    r"(?<!\bon )(?<!\bin )(?<!\bto )(?<!\bvia )(?<!\bopen )(?<!\bfrom )(?<!\busing )"
    r"(?:whatsapp|telegram|discord|slack|signal)(?=\s+(?!and\b|app\b|web\b|desktop\b|window\b|"
    r"chats?\b|messages?\b|is\b|on\b|in\b|to\b)[a-z]))\b"
)
# Who typed the goal is who "your words" may mean. An IPC goal comes from
# another program (the CLI, or a local agent acting as a confused deputy),
# so its text is never labelled as his words on a card.
_HIS_WORDS_ORIGINS = frozenset({"typed", "voice", "cli"})
# A transcript is what Whisper heard, which may be the room: labelled
# "heard", never "your words".
_HEARD_ORIGINS = frozenset({"voice"})
IPC_BADGE = "asked from another program"
INVISIBLE_BADGE = "invisible characters removed"


def _allowlist_for(goal: str) -> frozenset:
    """Apps the task may touch without a card: only ones he named.

    Holds both the process image (what the live foreground reports) and the
    name he used (what a plan's open_app / focus_window target says), so
    "open calculator" matches CalculatorApp.exe and the target "calculator".
    """
    words = set(re.findall(r"[a-z0-9]+", goal.lower()))
    allowed: set[str] = set()
    for name, image in _APP_ALIASES.items():
        if name in words:
            allowed.update((image, name))
    return frozenset(allowed)


def _outward_intents(goal: str) -> int:
    """How many sends/commits his words ask for (the per-task outward cap)."""
    from xenon.actions import OUTWARD_PATTERN
    from xenon.untrusted import skeleton

    folded = skeleton(goal)
    return (
        len(OUTWARD_PATTERN.findall(folded))
        + len(_MESSAGE_VERBS.findall(folded))
        + len(re.findall(r"\b(?:enter|return)\b", folded))
    )


def _app_label(ctx) -> str:
    title = (getattr(ctx, "window_title", "") or "").strip()
    if " - " in title:
        return title.rsplit(" - ", 1)[-1].strip()
    image = os.path.basename(getattr(ctx, "process_image", "") or "")
    stem = re.sub(r"(?i)(\.root)?\.exe$", "", image)
    return title or stem or "an unidentified app"


def _step_label(action, app: str = "") -> str:
    kind = action.kind
    where = f" in {app}" if app else ""
    if kind == "type_text":
        return f"Type into {app}" if app else "Type text"
    if kind == "press_keys":
        return f"Press {action.value or action.target}{where}"
    if kind == "click_control":
        return f'Click "{action.target}"{where}'
    if kind == "open_app":
        return f"Open {action.target}"
    if kind == "focus_window":
        return f"Switch to {action.target}"
    if kind == "write_file":
        return f"Write {action.target}"
    return kind.replace("_", " ").capitalize()


class _DeskSteps:
    """Cards, gate and progress around one plan run by ``TaskRunner``.

    ``context`` is the TaskRunner's context_provider (called before each
    step), ``approve`` its risk-aware approver, and ``execute`` the backend
    it calls: OUTWARD/DESTRUCTIVE steps go through gate.py with the token
    the card produced, everything else through the ordinary backend (which
    refuses OUTWARD on its own).
    """

    def __init__(
        self,
        task,
        tools,
        backend,
        task_context,
        goal: str,
        actions,
        undo_s: float = 5.0,
        commit_backend_factory=None,
        origin: str = "typed",
    ) -> None:
        self.task = task
        self.tools = tools
        self.backend = backend
        self.task_context = task_context
        self.goal = goal
        # Who asked: only typed, spoken or CLI goals are "your words" on a card.
        self.origin = (origin or "").strip().lower()
        self.total = len(actions)
        self.step = 0
        self.undo_s = undo_s
        self.last_ctx = None
        self.keyboard_carded = False
        self.cards = 0
        # Builds the permissive backend inside gate.py (tests pass a fake);
        # None means the real WindowsActionBackend.
        self.commit_backend_factory = commit_backend_factory
        self._tokens: dict[int, tuple[object, Risk]] = {}
        # Composer read-backs shown on OUTWARD cards, by step.
        self._readbacks: dict[int, dict] = {}
        # Where a carded LOCAL step was approved (image, title), by step: it
        # only runs there, not in whatever window is in front at click time.
        self._approved_at: dict[int, tuple[str, str]] = {}

    # -- TaskRunner hooks --------------------------------------------------

    def context(self, action):
        self.step += 1
        self.task.progress(
            f"Step {self.step}/{self.total} · {_step_label(action)}", self.step, self.total
        )
        self.last_ctx = None
        self.last_ctx = self._live(action)
        return self.last_ctx

    def _live(self, action):
        reader = getattr(self.backend, "live_context", None)
        if callable(reader):
            return reader(action)
        return self.task_context  # no foreground facts: classified as unidentified

    def approve(self, action, risk) -> bool:
        from xenon.actions import ApprovalRefused, classify, needs_card

        if self.task.cancelled:
            return False
        ctx = self.last_ctx if self.last_ctx is not None else self.task_context
        fresh, reason = classify(action, ctx)
        risk = risk.stricter(fresh)
        if risk is Risk.DENIED:
            raise ApprovalRefused(f"refused: {reason}")
        keyboard_first = action.kind in _KEYBOARD_KINDS and not self.keyboard_carded
        if risk is Risk.LOCAL and not needs_card(risk, reason) and not keyboard_first:
            return True
        readback = None
        if risk in (Risk.OUTWARD, Risk.DESTRUCTIVE) and self._needs_readback(action, ctx):
            readback = self._readback(action)
            refusal = self._readback_refusal(ctx, readback)
            if refusal:
                raise ApprovalRefused(f"refused: {refusal}; nothing was sent")
            self._readbacks[self.step] = readback
        pending = self.pending_for(action, ctx, risk, reason, readback=readback)
        self.cards += 1
        token = self.tools.broker.request(pending, cancel=self.task.cancel)
        if token is None:
            why = ""
            outcome = getattr(self.tools.broker, "outcome", None)
            if callable(outcome):
                try:
                    why = outcome(pending.action_id) or ""
                except Exception:
                    why = ""
            if why.startswith("refused:"):
                # Not his "no": a cap, the kill switch, no card available.
                raise ApprovalRefused(why)
            return False
        if action.kind in _KEYBOARD_KINDS:
            self.keyboard_carded = True  # the keyboard card is once per task
        if risk in (Risk.OUTWARD, Risk.DESTRUCTIVE):
            self._tokens[self.step] = (token, risk)
        else:
            self._approved_at[self.step] = self._place(ctx)
        return True

    # -- what a card is bound to ---------------------------------------------

    @staticmethod
    def _place(ctx) -> tuple[str, str]:
        from xenon import surfaces

        return (
            surfaces.norm_image(getattr(ctx, "process_image", "") or ""),
            (getattr(ctx, "window_title", "") or "").strip(),
        )

    def _place_check(self, approved: tuple[str, str]):
        def check(live) -> str | None:
            if self._place(live) != approved:
                return "the window changed since you approved"
            return None

        return check

    @staticmethod
    def _needs_readback(action, ctx) -> bool:
        """Enter or a click that may send from a messenger or a web page."""
        from xenon.actions import APP_BROWSER, APP_COMMS, APP_UNKNOWN, strict_app_class

        if action.kind not in {"press_keys", "click_control"}:
            return False
        return strict_app_class(ctx) in (APP_COMMS, APP_BROWSER, APP_UNKNOWN)

    def _readback(self, action) -> dict | None:
        """The focused composer's name (the chat) and text, or None."""
        reader = getattr(self.backend, "composer_readback", None)
        if not callable(reader):
            return None
        try:
            readback = reader(action)
        except Exception:
            return None
        return readback if isinstance(readback, dict) else None

    @staticmethod
    def _readback_refusal(ctx, readback) -> str:
        from xenon.actions import APP_COMMS, strict_app_class

        if readback is None or readback.get("text") is None:
            return "could not read back the message box"
        if strict_app_class(ctx) == APP_COMMS and not str(readback.get("chat") or "").strip():
            return "could not read which chat this would go to"
        return ""

    def execute(self, action):
        from xenon import gate
        from xenon.actions import ActionResult

        if self.task.cancelled:
            return ActionResult(False, "stopped")
        entry = self._tokens.pop(self.step, None)
        if entry is None:
            approved = self._approved_at.pop(self.step, None)
            if approved is not None:
                check = self._place_check(approved)
                expect = getattr(self.backend, "expect_context", None)
                if callable(expect):
                    # Checked against the backend's own live read, right
                    # before input is sent.
                    expect(check)
                else:
                    try:
                        changed = check(self._live(action))
                    except Exception:
                        changed = "could not re-read the window"
                    if changed:
                        return ActionResult(False, f"refused: {changed}; nothing was typed")
            return self.backend.execute(action)
        token, risk = entry
        destructive = risk is Risk.DESTRUCTIVE
        # A few seconds to take an approved send back ("Sending to WhatsApp
        # in 4… Undo"). Stop during the countdown also undoes it.
        if not destructive and not self.tools.undo(
            f"Sending to {_app_label(self.last_ctx)}", self.undo_s, self.task.cancel
        ):
            return ActionResult(False, "undone; nothing was sent")
        commit = gate.commit_destroy if destructive else gate.commit_desktop
        read_back = self._readbacks.pop(self.step, None) is not None

        def live_digest(ctx) -> str:
            readback = None
            if read_back:
                readback = self._readback(action)
                if readback is None or readback.get("text") is None:
                    raise RuntimeError("the message box could not be read back")
            return self.digest(action, ctx, readback)

        approved_digest = getattr(token, "digest", "")

        def same_target(ctx) -> str | None:
            # The commit backend's own read, just before input is sent:
            # focus may have moved since the gate's read.
            if live_digest(ctx) != approved_digest:
                return "the target changed since you approved"
            return None

        try:
            return commit(
                token,
                self.tools.broker,
                lambda: live_digest(self._live(action)),
                gate.desktop_executor(
                    action,
                    self.task_context,
                    destructive=destructive,
                    backend_factory=self.commit_backend_factory,
                    context_check=same_target,
                ),
            )
        except gate.CommitRefused as refusal:
            return ActionResult(False, str(refusal))

    # -- cards -------------------------------------------------------------

    def digest(self, action, ctx, readback: dict | None = None) -> str:
        from xenon import surfaces
        from xenon.approvals import canonical_digest

        payload = {
            "kind": action.kind,
            "target": action.target,
            "value": action.value,
            "app": surfaces.norm_image(getattr(ctx, "process_image", "")),
            "title": getattr(ctx, "window_title", ""),
            "element": getattr(ctx, "element_name", ""),
            "role": getattr(ctx, "element_role", ""),
            "url": getattr(ctx, "url", ""),
        }
        if readback is not None:
            # What Enter or Send commits, and to whom: a draft that grew or
            # a different chat after the card changes the digest.
            payload["chat"] = str(readback.get("chat") or "")
            payload["composer"] = str(readback.get("text") or "")
        return canonical_digest("desktop_commit", payload)

    def pending_for(self, action, ctx, risk: Risk, reason: str, readback: dict | None = None):
        import uuid

        from xenon import surfaces
        from xenon.approvals import NOT_IN_REQUEST, Field, PendingAction
        from xenon.untrusted import exact_span, has_invisible

        app = _app_label(ctx)
        image = os.path.basename(getattr(ctx, "process_image", "") or "") or "unknown"

        his_words = self.origin in _HIS_WORDS_ORIGINS
        mine = "heard" if self.origin in _HEARD_ORIGINS else "your words"

        def in_request(text: str) -> bool:
            return bool(text) and exact_span(text, self.goal)

        def source_of(text: str) -> str:
            # The plan's text is the model's unless it is an exact span of
            # what he typed or said; a goal sent over the pipe is never his,
            # and a transcript is only what was heard.
            return mine if his_words and in_request(text) else "drafter"

        fields = [Field("App", f"{app} ({image})", "xenon")]
        fields.append(Field("Action", _step_label(action, app), source_of(action.value or action.target)))
        if action.kind == "type_text":
            fields.append(Field("Text", action.value, source_of(action.value)))
        elif action.kind == "press_keys":
            fields.append(Field("Keys", action.value or action.target, "xenon"))
            # Enter or Space acts on whatever has focus: name it.
            focused = (getattr(ctx, "element_name", "") or "").strip()
            fields.append(Field("Focused", focused or "(could not be read)", "page"))
        elif action.kind == "click_control":
            page = surfaces.is_browser_image(getattr(ctx, "process_image", ""))
            fields.append(Field("Element", action.target, "page" if page else source_of(action.target)))
        elif action.target:
            fields.append(Field("Target", action.target, source_of(action.target)))
        if readback is not None:
            fields.append(Field("Chat", str(readback.get("chat") or "") or "(not shown)", "page"))
            composer = str(readback.get("text") or "")
            fields.append(Field("Sends", composer or "(the message box is empty)", "page"))
        if getattr(ctx, "url", ""):
            fields.append(Field("Domain", surfaces.url_host(ctx.url) or ctx.url, "page"))
        fields.append(Field("Why", reason.removeprefix("card:").strip(), "xenon"))
        badges = []
        if not his_words:
            badges.append(IPC_BADGE)
        if self.task_context.tainted:
            badges.append("plan read your screen")
        if action.kind in {"type_text", "set_text"} and has_invisible(
            (action.value or "").replace("\n", "").replace("\t", "")
        ):
            badges.append(INVISIBLE_BADGE)
        # Key names ("enter") are never in his words; the badge is for text
        # and names the model chose.
        outward_text = action.value if action.kind in {"type_text", "set_text"} else (
            action.target if action.kind == "click_control" else ""
        )
        if risk is Risk.OUTWARD and outward_text and not in_request(outward_text):
            badges.append(NOT_IN_REQUEST)
        kind = {Risk.OUTWARD: "desktop_commit", Risk.DESTRUCTIVE: "desktop_destroy"}.get(risk, f"local_{action.kind}")
        return PendingAction(
            action_id=uuid.uuid4().hex[:16],
            kind=kind,
            risk=risk,
            title=_step_label(action, app),
            fields=tuple(fields),
            digest=self.digest(action, ctx, readback),
            badges=tuple(badges),
            voice_ok=False,  # voice approvals stay off in phase 1
            task_id=getattr(self.task, "task_id", ""),
        )


def _task_summary(outcome, seconds: float) -> str:
    steps = len(outcome.steps)
    count = f"{steps} step{'s' if steps != 1 else ''} · {seconds:.1f}s"
    if outcome.status == "succeeded":
        return f"Done. ({count})"
    if outcome.status == "unverified":
        return f"Done, but I couldn't verify it. ({count} · unverified)"
    if outcome.status == "denied":
        return f"Stopped: {outcome.detail or 'not approved'}. ({count})"
    return f"That didn't work: {outcome.detail or outcome.status}. ({count})"


def _desktop_task(
    goal: str,
    origin: str,
    tools,
    config,
    planner=None,
    backend_factory=None,
    observe=None,
    commit_backend_factory=None,
    agent_check=None,
    sweep=None,
):
    """The body of an orb task: plan ``goal``, then run it through the cards.

    ``origin`` is who asked (typed, voice, ipc); the plan's steps are always
    classified with origin "model", because the model chose their words.
    The keyword arguments replace the planner, the screen read and the
    backends in tests. ``sweep`` runs before planning (the orb passes the
    once-per-process agent-desktop snapshot sweep); None skips it.
    """
    import time as _time

    def run(task) -> str:
        from xenon.actions import ActionContext
        from xenon.tasks import TaskCancelled
        from xenon.untrusted import redact_secrets

        check = agent_check
        if check is None:
            from xenon.killswitch import agent_allowed as check
        allowed, why = check()
        if not allowed:
            return f"I can't do that: {why}."
        if sweep is not None:
            try:
                sweep()
            except Exception:
                pass  # leftover refmaps are a privacy chore, never a task failure
        started = _time.monotonic()
        task.progress("Planning")
        screen = ""
        try:
            context = (observe or (lambda: _observer().observe()))()
            screen = f"{context.title}\n{context.text}"[:4000]
        except Exception:
            screen = ""
        model_planner = planner
        if model_planner is None:
            if config is None:
                return "Planning a task needs a model key (GROQ_API_KEY or GEMINI_API_KEY)."
            model_planner = ModelPlanner(OpenAICompatibleChat(config))
        try:
            plan = model_planner.plan(goal, redact_secrets(screen))
        except (ChatUnavailable, PlannerError) as error:
            return f"I couldn't plan that: {error}"
        task.check()
        if not plan.actions:
            return "I couldn't find any steps for that."
        # The plan saw the screen, so the task is tainted: open/focus stay
        # inside the apps he named, and egress-shaped steps get cards.
        task_context = ActionContext(
            tainted=bool(screen.strip()),
            user_words=goal,
            allowlist=_allowlist_for(goal),
            origin="model",
        )
        if tools.caps is not None:
            tools.caps.start_task(task.task_id, _outward_intents(goal))
        if backend_factory is not None:
            backend = backend_factory(task_context)
        else:
            from xenon.windows_actions import WindowsActionBackend

            backend = WindowsActionBackend(task_context=task_context)
        try:
            undo_s = float(os.environ.get("XENON_UNDO_S", "5"))
        except ValueError:
            undo_s = 5.0
        steps = _DeskSteps(
            task, tools, backend, task_context, goal, plan.actions,
            undo_s=max(0.0, undo_s), commit_backend_factory=commit_backend_factory,
            origin=origin,
        )
        outcome = TaskRunner(
            ApprovalPolicy(), steps, approver=steps.approve, context_provider=steps.context
        ).run(goal, plan.actions)
        if task.cancelled:
            raise TaskCancelled("stopped")
        return _task_summary(outcome, _time.monotonic() - started)

    return run


def _orb_task_services(config):
    """What the orb needs to run "do <goal>" tasks (see xenon.orb.OrbTasks)."""
    from xenon.orb import TaskServices

    return TaskServices(
        make_task=lambda goal, origin, tools: _desktop_task(
            goal, origin, tools, config, sweep=_sweep_snapshots_once
        ),
        is_t0=_recognised,
        clean=_without_wake,
    )


_SWEPT: list[bool] = []


def _sweep_snapshots_once() -> None:
    """Delete stale agent-desktop refmaps (window titles in plain text) once
    per orb process, on the first task's thread rather than at startup, so
    an orb that never runs a task pays nothing."""
    if _SWEPT:
        return
    _SWEPT.append(True)
    from xenon.desktop import sweep_stale_snapshots

    sweep_stale_snapshots()


FOCUS_LISTEN_S = 8.0


def _next_announcement(hooks) -> tuple[str, str] | None:
    """A queued ("say" | "ask", text) from the orb (task results, questions)."""
    say_queue = getattr(hooks, "say", None)
    if say_queue is None:
        return None
    try:
        kind, text = say_queue.get_nowait()
    except Exception:
        return None
    text = str(text or "").strip()
    return (str(kind), text) if text else None


def _focus_utterance(hooks, focus_until: float, now: float) -> bool:
    """Take this utterance without the wake word? Only inside the 8 s after
    a waiting task's question was spoken, and only while it still waits."""
    focus = getattr(hooks, "focus_listen", None)
    if focus is None or getattr(hooks, "task", None) is None or now >= focus_until:
        return False
    try:
        return bool(focus())
    except Exception:
        return False


def _voice_dispatch(command: str, hooks, smart_reply, answer, origin: str = "voice") -> tuple[str, bool]:
    """One woken voice command -> (reply, private).

    Task hand-off first ("do ...", "stop", a waiting task's answer), then
    the instant parsers and router (T0/T1), then the assistant, as today.
    ``origin`` is "voice" when the utterance may start a task (push-to-talk,
    or the wake word started it) and "voice-ambient" otherwise.
    """
    task = getattr(hooks, "task", None)
    if task is not None:
        try:
            routed = task(command, origin)
        except Exception as error:  # a task-layer bug must not kill the voice loop
            routed = f"I couldn't start that: {error}"
        if routed is not None:
            return routed, False
    fast, _understood = smart_reply(command)
    if fast is not None:
        return fast, False
    return answer(command), True


def _exchange(hooks, command: str, reply: str, private: bool) -> None:
    try:
        hooks.exchange(command, reply, private)
    except TypeError:  # an older two-argument hook
        hooks.exchange(command, reply)


def _orb_voice_runner(config):
    """Return a runner for the orb's Voice toggle, or None without the extras."""
    import importlib.util

    # Only check that the voice packages exist. Importing faster-whisper here
    # reserved ~512 MB (numpy, its math library, the speech engine) at every
    # startup even with voice off; it now loads when voice is switched on.
    if importlib.util.find_spec("sounddevice") is None or importlib.util.find_spec("faster_whisper") is None:
        return None

    def run(hooks, stop) -> None:
        import math as _math
        import time

        from xenon.audio import (
            MicrophoneSource,
            SapiSpeaker,
            UtteranceDetector,
            WhisperTranscriber,
        )
        from xenon.wake import WakeGate

        wake_word = os.environ.get("XENON_WAKE_WORD", "xenon")
        aliases = tuple(
            part.strip()
            for part in os.environ.get("XENON_WAKE_ALIASES", "").split(",")
            if part.strip()
        )
        # The orb floats on screen all day, so it waits for its name by default
        # instead of answering every sentence; XENON_WAKE_MODE=always opts out.
        always = os.environ.get("XENON_WAKE_MODE", "wake").strip().lower() == "always"

        def env_number(name, default, kind):
            try:
                return kind(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        mic = os.environ.get("XENON_MIC_DEVICE") or None
        if mic is not None:
            try:
                mic = int(mic)
            except ValueError:
                pass
        hooks.state("thinking", "loading speech model")
        speaker = SapiSpeaker()
        source = MicrophoneSource(device=mic)

        def make_detector():
            # Adaptive: with music playing, speech is whatever rises clearly
            # above the music, and a sentence ends when it falls back to it.
            return UtteranceDetector(
                threshold=env_number("XENON_VAD_THRESHOLD", 0.015, float),
                silence_ms=min(max(env_number("XENON_SILENCE_MS", 350, int), 120), 2000),
                min_speech_ms=200,
                adaptive=True,
            )

        detector = make_detector()
        transcriber = WhisperTranscriber(
            os.environ.get("XENON_WHISPER_MODEL", "base.en"),
            os.environ.get("XENON_WHISPER_DEVICE", "cpu"),
            os.environ.get("XENON_WHISPER_COMPUTE", "int8"),
            initial_prompt=os.environ.get(
                "XENON_WHISPER_PROMPT", _whisper_hint(wake_word)
            ),
            beam_size=env_number("XENON_WHISPER_BEAM", 1, int),
        )
        store = MemoryStore(os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH))
        # Without a model, open/switch/search still work through the fast path.
        assistant = (
            Assistant(
                _observer(), store, OpenAICompatibleChat(config),
                max_memories=2, max_context_chars=1800,
            )
            if config is not None
            else None
        )
        from xenon import chime
        from xenon.wake import DEFAULT_TTL_S

        gate = WakeGate(wake_word, aliases=aliases, always=always)
        router_model = OpenAICompatibleChat(config) if config is not None else None
        armed_until = 0.0

        def waiting() -> None:
            """Standby (waiting for the name) or listening (taking a command)."""
            if always:
                hooks.state("listening", "always listening")
            elif time.monotonic() < armed_until:
                hooks.state("listening", "go ahead")
            else:
                hooks.state("standby", f"say “{wake_word}” anywhere in a sentence")

        from xenon.duck import make_ducker

        ducker = make_ducker()
        listen_now = getattr(hooks, "listen_now", None)
        focus_until = 0.0

        def say(reply: str) -> None:
            nonlocal detector
            if not (reply or "").strip():
                return  # e.g. a wake-less utterance nothing was waiting for
            hooks.state("speaking", reply[:120])
            gate.set_speaking(True)
            try:
                speaker.speak(_speakable(reply))
            finally:
                gate.set_speaking(False)
            # The input stream buffered Xenon's own voice while it spoke;
            # reopening it drops that audio so it cannot answer itself.
            source.close()
            detector = make_detector()

        def answer(command: str) -> str:
            hooks.state("thinking", command)
            try:
                if assistant is None:
                    raise ChatUnavailable(
                        "I can open apps, switch windows and search, but "
                        "answering needs a model key."
                    )
                return assistant.ask(command, brief=True).text
            except ChatUnavailable as error:
                return str(error)

        try:
            waiting()
            shown_armed = False
            while not stop.is_set():
                announcement = _next_announcement(hooks)
                if announcement is not None:
                    # A task finished or asked something: say it. A question
                    # opens one wake-less listen (8 s) for the answer.
                    kind, spoken = announcement
                    ducker.duck()
                    say(spoken)
                    ducker.restore()
                    if kind == "ask":
                        focus_until = time.monotonic() + FOCUS_LISTEN_S
                    waiting()
                    continue
                if listen_now is not None and listen_now.is_set():
                    # Push-to-talk: listen now, no wake word, music turned down.
                    listen_now.clear()
                    gate.arm()
                    ducker.duck()
                    detector.reset()
                    armed_until = time.monotonic() + DEFAULT_TTL_S
                    shown_armed = True
                    chime.play("wake")
                    waiting()
                frame = source.read_frame()
                if frame:
                    rms = _math.sqrt(sum(v * v for v in frame) / len(frame))
                    hooks.level(min(1.0, rms / 0.08))
                # Drop back to standby once the armed window runs out.
                if shown_armed and time.monotonic() >= armed_until:
                    shown_armed = False
                    ducker.restore()
                    waiting()
                pcm = detector.feed(frame)
                if pcm is None:
                    continue
                text = (transcriber.transcribe(pcm, source.sample_rate) or "").strip()
                if not text or stop.is_set():
                    continue
                command = gate.feed(text)
                if command is None and _focus_utterance(hooks, focus_until, time.monotonic()):
                    # One utterance without the wake word, for the task that
                    # asked. It can answer or pick; it can never approve.
                    focus_until = 0.0
                    chime.play("heard")
                    hooks.state("heard", text)
                    try:
                        reply = hooks.task(text, "voice-focus") or ""
                    except Exception:
                        reply = ""
                    if reply and not stop.is_set():
                        say(reply)
                    waiting()
                    continue
                if command is None:
                    if gate.armed and not shown_armed:
                        # Heard just the name: chime and wait for the command.
                        armed_until = time.monotonic() + DEFAULT_TTL_S
                        shown_armed = True
                        # Like a smart speaker: turn the music down while
                        # listening for the command.
                        ducker.duck()
                        detector.reset()
                        chime.play("wake")
                        waiting()
                    continue
                armed_until = 0.0
                shown_armed = False
                ducker.duck()  # keep it down while thinking and replying
                chime.play("heard")
                hooks.state("heard", command)
                reply, private = _voice_dispatch(
                    command,
                    hooks,
                    lambda text: _smart_reply(text, router_model, origin="voice"),
                    answer,
                    origin="voice" if gate.may_start_task else "voice-ambient",
                )
                _exchange(hooks, command, reply, private)
                if stop.is_set():
                    break
                say(reply)
                ducker.restore()
                waiting()
        finally:
            ducker.restore()
            source.close()
            store.close()

    return run


def _orb(args: argparse.Namespace) -> int:
    if not is_windows():
        print("xenon orb requires Windows", file=sys.stderr)
        return 2
    try:
        from xenon.orb import OrbApp
    except ImportError as error:
        print(f"the orb needs Pillow: pip install -e \".[capture]\" ({error})", file=sys.stderr)
        return 2

    if getattr(args, "startup", None):
        return _orb_startup(args.startup == "on")

    config = chat_config_from_env()
    if config is None:
        print(_NO_MODEL + " Search and app launching still work.", file=sys.stderr)
    app = OrbApp(
        lambda: _OrbBackend(config),
        _orb_voice_runner(config),
        task_services=_orb_task_services(config),
    )
    print(
        "orb running: hover for options, drag to move, Ctrl+Alt+X to ask "
        "(\"do ...\" runs a task), Ctrl+Alt+V for voice, Ctrl+Alt+Esc stops a task, "
        "Close to quit",
        flush=True,
    )
    app.run()
    if sys.stdout:
        sys.stdout.flush()
    # The mic and COM threads can sit in native calls for a while; the window
    # is gone and state is saved, so end the process now instead of waiting.
    os._exit(0)


def _music_widget(args: argparse.Namespace) -> int:
    if not is_windows():
        print("xenon music-widget requires Windows", file=sys.stderr)
        return 2
    try:
        from xenon.music_widget import MusicWidget
    except ImportError as error:
        print(
            "the music widget needs Pillow, numpy and pyaudiowpatch: "
            f"pip install -e \".[music]\" ({error})",
            file=sys.stderr,
        )
        return 2

    widget = MusicWidget()
    print(
        "music widget running on the wallpaper — Ctrl+Alt+Shift+M toggles the "
        "click-through overlay, Ctrl+C to quit",
        flush=True,
    )
    try:
        if not widget.start():
            print("the music widget could not start (see messages above)", file=sys.stderr)
            return 1
        import time as _time

        while True:
            _time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        widget.stop()
    return 0


def _orb_startup(enable: bool) -> int:
    from xenon import startup

    if not enable:
        if startup.disable():
            print(f"removed {startup.shortcut_path()}")
        else:
            print("the orb was not set to start at sign-in")
        return 0
    try:
        path = startup.enable()
    except Exception as error:
        print(f"could not create the startup shortcut: {error}", file=sys.stderr)
        return 1
    print(f"the orb will start when you sign in ({path})")
    # A process started at sign-in only sees variables saved with setx, not
    # ones set in this terminal session.
    if startup.key_only_in_this_session():
        print(
            "note: your model key is only set in this terminal. Save it so the "
            "startup orb sees it, e.g.:  setx GROQ_API_KEY \"gsk_...\"",
            file=sys.stderr,
        )
    return 0


def _render_search(query: str) -> int:
    try:
        results = websearch.search(query, limit=5)
    except (websearch.SearchUnavailable, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    if not results:
        print("no results")
        return 0
    for index, result in enumerate(results, 1):
        print(f"{index}. {result.title}")
        print(f"   {result.url}")
        if result.snippet:
            print(f"   {result.snippet}")
    return 0


def _search(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    if not query:
        print("search needs a query", file=sys.stderr)
        return 2
    return _render_search(query)


def _print_plan(goal: str, actions) -> None:
    print(f"plan for: {goal}")
    for index, action in enumerate(actions, start=1):
        detail = " ".join(
            part
            for part in (
                f"target={action.target!r}" if action.target else "",
                f"value={action.value!r}" if action.value else "",
            )
            if part
        )
        note = f"  # {action.reason}" if action.reason else ""
        print(f"  {index}. {action.kind} {detail}{note}")


def _confirmed(args: argparse.Namespace) -> bool:
    """Plan-level yes. It only ever covers LOCAL steps (see ``_cli_approver``):
    a blanket --yes or 'y' is not a confirm card, so it cannot send."""
    if args.yes:
        return True
    try:
        reply = input("Run this plan? [y/N] ").strip().lower()
    except EOFError:
        reply = ""
    return reply in {"y", "yes"}


NEEDS_ORB = "needs the orb: start it to approve messages"
SENT_TO_ORB = "Sent to the orb for confirmation"


CLI_NEEDS_CARD = "needs a confirm card, which the command line cannot show"


def _cli_approver(action, risk, reason: str = "") -> bool:
    """--yes / 'y' approve READ and LOCAL only; OUTWARD and worse need a card.

    A LOCAL step whose reason starts with "card:" (a write outside his
    words, typing into a messenger or browser, an app outside the task's
    allowlist) needs a one-tap card on the orb; a blanket --yes is not one,
    so the CLI refuses it and says why.
    """
    from xenon.actions import ApprovalRefused, needs_card

    if risk is Risk.READ:
        return True
    if risk is not Risk.LOCAL:
        return False
    if needs_card(risk, reason):
        raise ApprovalRefused(f"refused: {reason.removeprefix('card:').strip()} ({CLI_NEEDS_CARD})")
    return True


# Names that tell the CLI pre-flight which app a plan step will land in.
# Unknown names fall back to "<last title word>.exe"; the live check at
# execution time stays the real guard, this only refuses early.
_PREDICT_IMAGES = {
    "whatsapp": "WhatsApp.Root.exe", "telegram": "Telegram.exe", "discord": "Discord.exe",
    "teams": "ms-teams.exe", "slack": "slack.exe", "signal": "Signal.exe", "outlook": "olk.exe",
    "claude": "claude.exe", "chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe",
    "brave": "brave.exe", "opera": "opera.exe", "gmail": "chrome.exe", "mail": "olk.exe",
    "terminal": "WindowsTerminal.exe", "powershell": "powershell.exe", "cmd": "cmd.exe",
}


def _predicted_image(target: str) -> str:
    """The process an open/focus target will bring up, or "" when unknown.

    Comms, browsers and shells are checked first, so "Notepad - WhatsApp"
    predicts WhatsApp. A name nobody listed predicts "" (an unidentified
    app), which the classifier treats like comms: guessing "<name>.exe"
    would call an unknown window an ordinary local app, the looser answer.
    """
    lowered = (target or "").lower()
    for hints in (_PREDICT_IMAGES, _APP_ALIASES):
        for hint, image in hints.items():
            if re.search(rf"\b{re.escape(hint)}\b", lowered):
                return image
    return ""


def _preflight(goal: str, actions, backend, origin: str = "cli"):
    """The first step the CLI may not run, as ``(index, action, risk, reason)``.

    Classified before anything runs, against where each step will land
    (the live foreground, then whatever an open/focus step brings up), so
    "open whatsapp, type hi, press enter" is refused before "hi" is typed.
    """
    from xenon.actions import ActionContext, classify

    ctx = None
    live = getattr(backend, "live_context", None)
    if callable(live) and actions:
        try:
            ctx = live(actions[0])
        except Exception:
            ctx = None
    if ctx is None:
        # Foreground unknown: an unidentified app (strict), but with his words
        # and the real origin, so a model's open_app gets the catalog-only rule.
        ctx = ActionContext(user_words=goal, origin=origin)
    for index, action in enumerate(actions, start=1):
        kind = action.kind.strip().lower()
        risk, reason = classify(action, ctx)
        if risk not in (Risk.READ, Risk.LOCAL):
            return index, action, risk, reason
        if kind in {"open_app", "focus_window"}:
            ctx = ActionContext(
                process_image=_predicted_image(action.target),
                window_title=action.target,
                user_words=goal,
                origin=origin,
            )
        elif kind == "type_text":
            # Typing means focus is in something that takes text.
            from dataclasses import replace

            ctx = replace(ctx, focused_role=ctx.focused_role or "document")
    return None


def _describe_step(action) -> str:
    detail = action.value or action.target
    return f"{action.kind} {detail!r}" if detail else action.kind


def _refuse_or_hand_off(goal: str, index: int, action, risk: Risk, reason: str) -> int:
    """OUTWARD/DESTRUCTIVE: the orb shows a card, or the CLI refuses."""
    if risk is Risk.DENIED:
        print(f"refused at step {index} ({_describe_step(action)}): {reason}", file=sys.stderr)
        return 1
    try:
        from xenon import ipc

        task_id = ipc.send_task(goal)
    except Exception:
        task_id = None
    if task_id:
        print(SENT_TO_ORB)
        return 0
    print(f"refused at step {index} ({_describe_step(action)}): {reason}")
    print(NEEDS_ORB, file=sys.stderr)
    return 1


def _cli_task_context(origin: str = "cli", goal: str = "", tainted: bool = False, apps=()):
    """Task facts for a CLI run: who asked, his words, the apps he named
    (plus any the deterministic parser took from his words), and taint."""
    from xenon.actions import ActionContext

    allowlist = set(_allowlist_for(goal))
    allowlist.update(str(app) for app in apps if str(app or "").strip())
    return ActionContext(
        user_words=goal, origin=origin, tainted=bool(tainted), allowlist=frozenset(allowlist)
    )


def _cli_backend(origin: str = "cli", goal: str = "", tainted: bool = False, apps=()):
    """The ordinary (non-committing) backend, told who asked and his words."""
    from xenon.windows_actions import WindowsActionBackend

    try:
        return WindowsActionBackend(task_context=_cli_task_context(origin, goal, tainted, apps))
    except TypeError:  # a stand-in backend that takes no arguments
        return WindowsActionBackend()


def _cli_agent_refusal() -> str | None:
    """XENON_AGENT=off and running elevated stop every CLI agent path."""
    from xenon.killswitch import agent_allowed

    try:
        allowed, why = agent_allowed()
    except Exception as error:  # an unreadable switch counts as off
        return f"could not read the agent switch: {error}"
    return None if allowed else (why or "agent features are switched off")


def _run_actions(
    goal: str, actions, args: argparse.Namespace, verifier=None, backend=None, task_context=None
) -> int:
    from xenon.actions import FileVerifier
    from xenon.windows_actions import ForegroundTitleVerifier

    if args.expect_title:
        verifier = ForegroundTitleVerifier(args.expect_title)
    elif args.expect_file:
        verifier = FileVerifier(args.expect_file, args.expect_text)

    if backend is None:
        backend = _cli_backend(goal=goal)
    live = getattr(backend, "live_context", None)
    outcome = TaskRunner(
        ApprovalPolicy(),
        backend,
        approver=_cli_approver,
        verifier=verifier,
        # Without a live reader the task facts are all there is: classified
        # as an unidentified foreground with his words and allowlist.
        context_provider=live if callable(live) else (
            lambda _action: task_context if task_context is not None else _cli_task_context(goal=goal)
        ),
    ).run(goal, actions)

    print(f"status: {outcome.status}")
    for step in outcome.steps:
        result = step.result.detail if step.result is not None else "not run"
        print(f"  - {step.action.kind}: {result}")
    if outcome.detail:
        print(f"detail: {outcome.detail}")
    refused = outcome.steps[-1] if outcome.steps else None
    if (
        outcome.status == "denied"
        and refused is not None
        and refused.approved is False
        and refused.risk in (Risk.OUTWARD, Risk.DESTRUCTIVE)
    ):
        # Discovered only at run time (the window was not what the plan
        # expected). Earlier steps already ran, so the goal is not re-sent.
        print(f"refused at step {len(outcome.steps)} ({_describe_step(refused.action)}): {NEEDS_ORB}",
              file=sys.stderr)
    return 0 if outcome.status == "succeeded" else 1


def _recognised(command: str) -> bool:
    """Can a deterministic parser run this? (Keeps the router honest.)"""
    from xenon import brightness, music, quick

    command = _without_wake(command)
    return any(
        parser(command) is not None
        for parser in (quick.parse, brightness.parse, music.parse, fastpath.parse)
    )


def _smart_reply(text: str, model=None, origin: str = "typed") -> tuple[str | None, str | None]:
    """Run a request as a command if possible: instant parsers first, then
    the model's reading of it. Returns (reply, understood_command)."""
    reply = _voice_fast_reply(text, origin=origin)
    if reply is not None or model is None:
        return reply, None
    from xenon import router

    command = router.route(_without_wake(text), model.complete, _recognised)
    if command is None:
        return None, None
    # A routed command is the model's reading of his words, so it gets the
    # stricter origin.
    return _voice_fast_reply(command, origin="model" if origin == "typed" else origin), command


def _voice_fast_reply(
    command: str, backend=None, search_fn=None, music_controller=None, origin: str = "typed"
) -> str | None:
    """Run open/focus/search/music without a model, or return None otherwise.

    Only these deterministic intents auto-approve. Typing, key presses, and
    file writes still go through the normal approval path (or the model).
    """
    from xenon import music

    from xenon import quick

    command = _without_wake(command)
    quick_command = quick.parse(command)
    if quick_command is not None:
        return quick.run(quick_command)
    from xenon import brightness

    bright = brightness.parse(command)
    if bright is not None:
        return brightness.run(bright)
    music_command = music.parse(command)
    if music_command is not None:
        controller = music_controller or music.MusicController.from_env()
        return controller.run(music_command)

    fast = fastpath.parse(command)
    if fast is None:
        return None

    if fast.kind == "search":
        fetch = search_fn or websearch.search
        try:
            results = fetch(fast.target, limit=1)
        except (websearch.SearchUnavailable, ValueError):
            return "Search is unavailable right now."
        if not results:
            return f"I found nothing for {fast.target}."
        top = results[0]
        snippet = re.sub(r"\s+", " ", top.snippet).strip()
        reply = f"Top result: {top.title}."
        if snippet:
            reply += f" {snippet}"
        return reply[:260]

    action = fast.action()
    if action is None:
        return None

    from xenon.actions import ActionContext

    # Only open/focus get here, and only READ/LOCAL may run unasked; the
    # origin makes voice requests name a catalog app, never a path or link.
    # The backend gets the same facts, so its own launch rules match.
    # The deterministic parser took the target from his words, so it is in
    # the task's allowlist.
    request_context = ActionContext(
        user_words=command, origin=origin,
        allowlist=frozenset(_allowlist_for(command) | {action.target}),
    )
    if backend is None:
        backend = _cli_backend(origin=origin, goal=command, apps=(action.target,))
    outcome = TaskRunner(
        ApprovalPolicy(),
        backend,
        approver=_cli_approver,
        context_provider=lambda _action: request_context,
    ).run(command, [action])

    ok = bool(outcome.steps) and all(
        step.result is not None and step.result.ok for step in outcome.steps
    )
    if ok:
        if fast.kind == "open_app":
            return f"Opened {fast.label or fast.target}."
        return f"Switched to {fast.target}."
    return f"Couldn't do that: {outcome.detail or 'the action failed'}"


def _without_wake(text: str) -> str:
    """Drop the wake word from typed requests too ("open chrome xenon")."""
    from xenon.wake import strip_wake_word

    aliases = tuple(
        part.strip()
        for part in os.environ.get("XENON_WAKE_ALIASES", "").split(",")
        if part.strip()
    )
    return strip_wake_word(text, os.environ.get("XENON_WAKE_WORD", "xenon"), aliases)


def _whisper_hint(wake_word: str) -> str:
    """Spelling hint for Whisper. Listing aliases here made it echo them back
    on short clips ("Xenon, zenon, see none."), so it is just the name."""
    return f"Hey {wake_word.capitalize()}."


def _speakable(text: str) -> str:
    """Drop markdown punctuation that sounds wrong through SAPI."""
    cleaned = re.sub(r"[`*_#>|~]", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _do(args: argparse.Namespace) -> int:
    if not is_windows():
        print("xenon do requires Windows", file=sys.stderr)
        return 2

    goal = _without_wake(" ".join(args.goal))
    if not goal:
        print("do needs a goal", file=sys.stderr)
        return 2
    # Before routing or planning: XENON_AGENT=off, or an elevated (admin)
    # terminal, means no model-planned or agent step runs from here.
    refusal = _cli_agent_refusal()
    if refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2

    # Common commands skip the model entirely: no cold start, no planning
    # round trip. The same approval/verification rules still apply.
    from xenon import music

    from xenon import quick

    if not _recognised(goal):
        do_config = chat_config_from_env()
        if do_config is not None:
            from xenon import router

            routed = router.route(goal, OpenAICompatibleChat(do_config).complete, _recognised)
            if routed:
                print(f"understood as: {routed}")
                goal = routed

    quick_command = quick.parse(goal)
    if quick_command is not None:
        print(f"fast path: {quick_command.kind}")
        if args.dry_run:
            return 0
        print(quick.run(quick_command))
        return 0

    from xenon import brightness

    bright = brightness.parse(goal)
    if bright is not None:
        print(f"fast path: brightness {bright.action}")
        if args.dry_run:
            return 0
        print(brightness.run(bright))
        return 0

    music_command = music.parse(goal)
    if music_command is not None:
        print(f"fast path (music): {music_command.kind} {music_command.query}".rstrip())
        if args.dry_run:
            return 0
        print(music.MusicController.from_env().run(music_command))
        return 0

    fast = fastpath.parse(goal)
    if fast is not None and fast.kind == "search":
        print(f"fast path: {goal}")
        if args.dry_run:
            return 0
        return _render_search(fast.target)

    if fast is not None and fast.kind in {"open_app", "focus_window"}:
        action = fast.action()
        assert action is not None
        print(f"fast path: {goal}")
        _print_plan(goal, [action])
        if args.dry_run:
            return 0
        backend = _cli_backend(goal=goal, apps=(action.target,))
        blocked = _preflight(goal, [action], backend)
        if blocked is not None:
            return _refuse_or_hand_off(goal, *blocked)
        if not _confirmed(args):
            print("cancelled")
            return 1
        default_title = fast.label if fast.kind == "open_app" else fast.target
        if fast.kind == "open_app":
            from xenon.windows_actions import DesktopTitleVerifier

            verifier = DesktopTitleVerifier(default_title)
        else:
            from xenon.windows_actions import ForegroundTitleVerifier

            verifier = ForegroundTitleVerifier(default_title)

        return _run_actions(
            goal, [action], args, verifier=verifier, backend=backend,
            task_context=_cli_task_context(goal=goal, apps=(action.target,)),
        )

    config = chat_config_from_env()
    if config is None:
        print(
            "no model configured; export GROQ_API_KEY or GEMINI_API_KEY (or set XENON_MODEL_PROVIDER=ollama) to plan tasks. See `python -m xenon providers`.",
            file=sys.stderr,
        )
        return 2

    from xenon.untrusted import redact_secrets

    context = _observer().observe()
    screen = f"{context.title}\n{context.text}"[:4000]
    planner = ModelPlanner(OpenAICompatibleChat(config))
    try:
        # Redacted here as well as inside the planner: the terminal this
        # runs from may have just printed a key.
        plan = planner.plan(goal, redact_secrets(screen))
    except (ChatUnavailable, PlannerError) as error:
        print(f"planning error: {error}", file=sys.stderr)
        return 1

    _print_plan(goal, plan.actions)
    if args.dry_run:
        return 0
    # The plan came from a model, so its open_app targets get the strict
    # (catalog-name-only) rules; it read the screen, so the task is tainted
    # and may only open or focus the apps he named.
    backend = _cli_backend(origin="model", goal=goal, tainted=bool(screen.strip()))
    blocked = _preflight(goal, plan.actions, backend, origin="model")
    if blocked is not None:
        return _refuse_or_hand_off(goal, *blocked)
    if not _confirmed(args):
        print("cancelled")
        return 1
    return _run_actions(
        goal, plan.actions, args, backend=backend,
        task_context=_cli_task_context("model", goal, tainted=bool(screen.strip())),
    )


def _spotify(args: argparse.Namespace) -> int:
    from xenon import music

    if args.spotify_command == "logout":
        print("logged out" if music.forget_tokens() else "was not logged in")
        return 0
    if args.spotify_command == "status":
        tokens = music.load_tokens()
        if tokens is None:
            print("not logged in; run `python -m xenon spotify login`")
            print("without a login, pause/next/previous still work through media keys")
            return 1
        try:
            devices = music.SpotifyClient(tokens).devices()
        except Exception as error:
            print(f"logged in, but Spotify did not answer: {error}", file=sys.stderr)
            return 1
        print(f"logged in ({music.token_path()})")
        if devices:
            for device in devices:
                marker = "*" if device.get("is_active") else " "
                print(f"{marker} {device.get('name')} ({device.get('type')})")
        else:
            print("no open Spotify devices; Xenon starts the desktop app when you play")
        return 0

    client_id = (args.client_id or os.environ.get("XENON_SPOTIFY_CLIENT_ID", "")).strip()
    redirect = os.environ.get("XENON_SPOTIFY_REDIRECT_URI", music.DEFAULT_REDIRECT)
    if not client_id:
        print(
            "Spotify needs your own (free) app id once:\n"
            "  1. Open https://developer.spotify.com/dashboard and create an app.\n"
            f"  2. Add this Redirect URI exactly: {redirect}\n"
            "  3. Tick 'Web API', save, and copy the Client ID.\n"
            "  4. Run: python -m xenon spotify login --client-id <id>\n"
            "     (or set XENON_SPOTIFY_CLIENT_ID).\n"
            "Playback control through the API needs Spotify Premium.",
            file=sys.stderr,
        )
        return 2
    print("opening the Spotify login page in your browser...", flush=True)
    try:
        music.login(client_id, redirect)
    except Exception as error:
        print(f"login failed: {error}", file=sys.stderr)
        return 1
    print(f"connected. Tokens saved to {music.token_path()} (delete with `xenon spotify logout`).")
    print('try: python -m xenon do "play my liked songs"')
    return 0


def _brightness(args: argparse.Namespace) -> int:
    from xenon import brightness

    level = args.level.strip().lower()
    if level == "list":
        displays = brightness.discover()
        if not displays:
            print("no controllable displays (turn on DDC/CI in the monitor's menu)")
            return 1
        wanted = brightness.target_kinds()
        for display in displays:
            used = "" if display.kind in wanted else "   (not targeted; XENON_BRIGHTNESS_TARGET=all to include)"
            print(f"{display.name} ({display.kind}): {display.get()}%{used}")
        return 0
    if level.rstrip("%").isdigit():
        reply = brightness.apply("set", int(level.rstrip("%")))
    elif level in {"min", "max", "up", "down", "toggle", "restore"}:
        reply = brightness.apply(level)
    else:
        print("use: list, min, max, up, down, toggle, restore or 0-100", file=sys.stderr)
        return 2
    print(reply)
    return 0 if not reply.startswith(("Couldn't", "I can't")) else 1


def _companion_url(path: str) -> str:
    from xenon.companion import DEFAULT_PORT

    port = int(os.environ.get("XENON_COMPANION_PORT", DEFAULT_PORT))
    return f"http://127.0.0.1:{port}{path}"


def _screens(args: argparse.Namespace) -> int:
    import json as _json
    import time as _time
    import urllib.request

    try:
        with urllib.request.urlopen(_companion_url("/screens"), timeout=3) as response:
            screens = _json.load(response)
    except Exception as error:
        print(f"the orb's companion server is not running ({error})", file=sys.stderr)
        return 1
    if not screens:
        from xenon.companion import lan_address

        print(f"no screens yet; open http://{lan_address()}:{_companion_url('').rsplit(':', 1)[1]} on the tablet")
        return 1
    for address, info in screens.items():
        age = _time.time() - info.get("last_seen", 0)
        status = "connected" if age < 10 else f"last seen {int(age)} s ago"
        size = f"{info.get('width', '?')}x{info.get('height', '?')} @{info.get('dpr', '?')}x"
        print(f"{address}: {status}, {size}, {info.get('fps', '?')} fps")
        print(f"    {info.get('agent', '')}")
    return 0


def _show(args: argparse.Namespace) -> int:
    import json as _json
    import urllib.request

    if args.clear:
        path, payload = "/clear", {}
    else:
        if args.image:
            kind, content = "image", args.image
        elif args.url:
            kind, content = "url", args.url
        else:
            kind, content = "text", " ".join(args.text)
        if not content.strip():
            print("nothing to show", file=sys.stderr)
            return 2
        path = "/show"
        payload = {"kind": kind, "content": content, "seconds": args.seconds, "title": args.title or ""}
    from xenon.companion import SECRET_HEADER, read_post_secret

    # POST /show and /clear need the running orb's per-boot secret, which it
    # writes to %LOCALAPPDATA%\Xenon\companion.key for this user only.
    secret = read_post_secret()
    if not secret:
        print("the orb's companion server is not running (no companion key)", file=sys.stderr)
        return 1
    request = urllib.request.Request(
        _companion_url(path), data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", SECRET_HEADER: secret}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3):
            pass
    except Exception as error:
        print(f"could not reach the companion screen: {error}", file=sys.stderr)
        return 1
    print("cleared" if args.clear else f"showing on the companion screen for {args.seconds:g} s")
    return 0


def _providers(args: argparse.Namespace) -> int:
    from xenon.providers import PROVIDER_PRESETS, key_is_set, settings_from_env

    settings = settings_from_env()
    for preset in PROVIDER_PRESETS.values():
        marker = "*" if settings is not None and settings.provider == preset.name else " "
        ready = "key set" if key_is_set(preset) else ", ".join(preset.key_envs)
        print(f"{marker} {preset.name:11} {preset.base_url}")
        print(f"    model: {preset.model}")
        print(f"    key:   {ready}")
        print(f"    {preset.note}")
        if preset.signup:
            print(f"    get a key: {preset.signup}")
    if settings is None:
        print(
            "\nNo provider configured. Set XENON_MODEL_PROVIDER=groq and "
            "GROQ_API_KEY=... (or export just GROQ_API_KEY)."
        )
    else:
        effort = (
            f" (reasoning effort: {settings.reasoning_effort})"
            if settings.reasoning_effort
            else ""
        )
        print(f"\nactive: {settings.provider} / {settings.model}{effort}")
        print("run `python -m xenon doctor` to confirm the key and model name work")
    return 0


def _apps(args: argparse.Namespace) -> int:
    from xenon.apps import default_catalog, launch

    catalog = default_catalog()
    query = " ".join(args.query).strip()
    if not query:
        entries = catalog.entries
        print(f"{len(entries)} launchable apps discovered")
        for entry in entries[:50]:
            print(f"  {entry.name}")
        if len(entries) > 50:
            print(f"  ... and {len(entries) - 50} more")
        return 0

    # Typed at the CLI, so it may name a path or an http(s) link.
    resolved = catalog.resolve(query, origin="cli")
    if resolved is None:
        print(f"no app matches {query!r}")
        return 1
    print(f"name:   {resolved.name}")
    print(f"source: {resolved.source}")
    if resolved.app_id:
        print(f"app id: {resolved.app_id}")
    if resolved.path:
        print(f"path:   {resolved.path}")
    if resolved.target:
        print(f"target: {resolved.target}")
    if args.launch:
        print(launch(resolved))
    return 0


def _notion(args: argparse.Namespace) -> int:
    env_config = notion_config_from_env()
    token = os.environ.get("XENON_NOTION_TOKEN", "")
    page = args.page or (env_config.root_page_id if env_config is not None else "")
    if not token or not page:
        print(
            "set XENON_NOTION_TOKEN and pass --page (or set XENON_NOTION_ROOT_PAGE)",
            file=sys.stderr,
        )
        return 2

    config = NotionConfig(token=token, root_page_id=page)
    with MemoryStore(os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH)) as store:
        try:
            result = sync_to_memory(NotionClient(config), store, page)
        except NotionUnavailable as error:
            print(f"notion error: {error}", file=sys.stderr)
            return 1

    print(
        f"source {result.source_id}: added={result.added} "
        f"updated={result.updated} unchanged={result.unchanged} "
        f"removed={result.removed}"
    )
    return 0


def _skills_root() -> str:
    return os.environ.get("XENON_SKILLS_DIR", "skills")


def _skills(args: argparse.Namespace) -> int:
    try:
        registry = SkillRegistry(_skills_root())
    except SkillError as error:
        print(f"skill error: {error}", file=sys.stderr)
        return 1

    if args.skills_command == "find":
        items = registry.find(" ".join(args.query))
    else:
        items = registry.catalog()

    if not items:
        print("no matching skills")
        return 0
    for metadata in items:
        print(f"{metadata.name}: {metadata.description}")
    return 0


def _memory(args: argparse.Namespace) -> int:
    with MemoryStore(os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH)) as store:
        if args.memory_command == "ingest":
            result = store.ingest_markdown(args.kind, args.root)
            print(
                f"source {result.source_id}: added={result.added} "
                f"updated={result.updated} unchanged={result.unchanged} "
                f"removed={result.removed}"
            )
            return 0

        if args.memory_command == "import":
            path = args.path or os.environ.get(
                f"XENON_{args.kind.upper()}_EXPORT_DIR", ""
            )
            if not path:
                print(
                    f"pass --path or set XENON_{args.kind.upper()}_EXPORT_DIR",
                    file=sys.stderr,
                )
                return 2
            try:
                result = ingest_export(store, args.kind, path)
            except (ExportFormatError, FileNotFoundError) as error:
                print(f"import error: {error}", file=sys.stderr)
                return 1
            print(
                f"source {result.source_id}: added={result.added} "
                f"updated={result.updated} unchanged={result.unchanged} "
                f"removed={result.removed}"
            )
            return 0

        hits = store.search(args.query, limit=args.limit)
        if not hits:
            print("no matches")
            return 0
        for hit in hits:
            print(f"[{hit.source_kind}] {hit.title}")
            print(f"  {hit.path}")
            print(f"  {hit.snippet}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="xenon", description="Local screen-aware assistant")
    subparsers = parser.add_subparsers(dest="command", required=True)

    screen = subparsers.add_parser("screen", help="describe the active window")
    screen.set_defaults(handler=_screen)

    doctor = subparsers.add_parser(
        "doctor", help="check screen, model, memory, voice, and skills"
    )
    doctor.set_defaults(handler=_doctor)

    ask = subparsers.add_parser("ask", help="answer a question from screen and memory")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--skill", default="", help="load one Agent Skill by name")
    ask.set_defaults(handler=_ask)

    do = subparsers.add_parser("do", help="plan and run an approved desktop task")
    do.add_argument("goal", nargs="+")
    do.add_argument("--dry-run", action="store_true", help="print the plan only")
    do.add_argument("--yes", action="store_true", help="skip the plan confirmation")
    do.add_argument("--expect-title", default="", help="verify the foreground title contains this")
    do.add_argument("--expect-file", default="", help="verify this file exists afterwards")
    do.add_argument("--expect-text", default="", help="verify the file contains this text")
    do.set_defaults(handler=_do)

    ui = subparsers.add_parser("ui", help="open the always-on-top command box")
    ui.add_argument(
        "--no-topmost", action="store_true", help="let other windows cover the box"
    )
    ui.set_defaults(handler=_ui)

    orb = subparsers.add_parser(
        "orb", help="open the minimized floating orb (hover for options)"
    )
    orb.add_argument(
        "--startup", choices=("on", "off"),
        help="start the orb when you sign in to Windows (no console window)",
    )
    orb.set_defaults(handler=_orb)

    music_widget = subparsers.add_parser(
        "music-widget",
        help="desktop music interface on the wallpaper (vinyl player)",
    )
    music_widget.set_defaults(handler=_music_widget)

    voice = subparsers.add_parser("voice", help="listen continuously and answer aloud")
    voice.add_argument("--mic", default=None, help="input device index or name (or set XENON_MIC_DEVICE)")
    voice.add_argument("--threshold", type=float, default=None, help="VAD energy threshold (or set XENON_VAD_THRESHOLD)")
    voice.add_argument("--silence-ms", type=int, default=None, help="silence before dispatch (default 350; or set XENON_SILENCE_MS)")
    voice.add_argument("--always", action="store_true", help="dispatch every sentence without a wake word (default)")
    voice.add_argument("--wake", action="store_true", help="require the wake word again")
    voice.add_argument("--list-mics", action="store_true", help="list input devices and exit")
    voice.set_defaults(handler=_voice)

    search = subparsers.add_parser("search", help="search the web (DuckDuckGo)")
    search.add_argument("query", nargs="+")
    search.set_defaults(handler=_search)

    bright = subparsers.add_parser(
        "brightness", help="set display brightness (DDC/CI monitors and laptop panels)"
    )
    bright.add_argument("level", help="list, min, max, up, down, toggle, restore or 0-100")
    bright.set_defaults(handler=_brightness)

    screens = subparsers.add_parser("screens", help="list companion screens (tablets) and what they report")
    screens.set_defaults(handler=_screens)

    show = subparsers.add_parser("show", help="show text, an image or a page on the companion screens")
    show.add_argument("text", nargs="*", help="text to show")
    show.add_argument("--image", help="image address (http/https)")
    show.add_argument("--url", help="page address (http/https)")
    show.add_argument("--title", help="small heading above it")
    show.add_argument("--seconds", type=float, default=15.0, help="how long to show it")
    show.add_argument("--clear", action="store_true", help="remove whatever is shown")
    show.set_defaults(handler=_show)

    spotify = subparsers.add_parser("spotify", help="connect Spotify for music control")
    spotify_commands = spotify.add_subparsers(dest="spotify_command", required=True)
    spotify_login = spotify_commands.add_parser("login", help="one-time browser login")
    spotify_login.add_argument("--client-id", help="your app's Client ID")
    spotify_login.set_defaults(handler=_spotify)
    spotify_commands.add_parser("status", help="show login and devices").set_defaults(handler=_spotify)
    spotify_commands.add_parser("logout", help="delete saved tokens").set_defaults(handler=_spotify)

    providers = subparsers.add_parser(
        "providers", help="show free model providers and the active one"
    )
    providers.set_defaults(handler=_providers)

    apps = subparsers.add_parser(
        "apps", help="list discovered apps or resolve one by name"
    )
    apps.add_argument("query", nargs="*")
    apps.add_argument(
        "--launch", action="store_true", help="launch the resolved app"
    )
    apps.set_defaults(handler=_apps)

    skills = subparsers.add_parser("skills", help="list or match Agent Skills")
    skill_commands = skills.add_subparsers(dest="skills_command", required=True)
    skill_list = skill_commands.add_parser("list", help="list skill metadata")
    skill_list.set_defaults(handler=_skills)
    skill_find = skill_commands.add_parser("find", help="match skills to a request")
    skill_find.add_argument("query", nargs="+")
    skill_find.set_defaults(handler=_skills)

    memory = subparsers.add_parser("memory", help="ingest and search local memory")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)

    ingest = memory_commands.add_parser("ingest", help="mirror a Markdown tree")
    ingest.add_argument("--kind", default="obsidian", help="source label, e.g. obsidian")
    ingest.add_argument("--root", required=True, help="folder to mirror")
    ingest.set_defaults(handler=_memory)

    search = memory_commands.add_parser("search", help="full-text search memory")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.set_defaults(handler=_memory)

    import_command = memory_commands.add_parser(
        "import", help="import a Claude or Codex export"
    )
    import_command.add_argument("--kind", required=True, help="claude or codex")
    import_command.add_argument("--path", default="", help="export file or folder")
    import_command.set_defaults(handler=_memory)

    notion = subparsers.add_parser("notion", help="sync a Notion page tree")
    notion_commands = notion.add_subparsers(dest="notion_command", required=True)
    notion_ingest = notion_commands.add_parser("ingest", help="read-only page sync")
    notion_ingest.add_argument("--page", default="", help="root page id or URL id")
    notion_ingest.set_defaults(handler=_notion)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
