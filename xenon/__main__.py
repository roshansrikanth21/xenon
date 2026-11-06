"""Xenon command line: ``python -m xenon screen`` / ``memory``."""
from __future__ import annotations

import argparse
import os
import re
import sys

from xenon import fastpath, websearch
from xenon.actions import ApprovalPolicy, TaskRunner
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

    question = " ".join(args.question).strip()
    config = chat_config_from_env()
    if config is None:
        print(
            "set XENON_MODEL_API_KEY and XENON_MODEL_NAME to enable answers",
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
            "set XENON_MODEL_API_KEY and XENON_MODEL_NAME to use the command box",
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
            "set XENON_MODEL_API_KEY and XENON_MODEL_NAME to answer by voice",
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

    beam_size = 3
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
        detector = UtteranceDetector(threshold=vad_threshold, min_speech_ms=300)
        transcriber = WhisperTranscriber(
            os.environ.get("XENON_WHISPER_MODEL", "base"),
            os.environ.get("XENON_WHISPER_DEVICE", "cpu"),
            os.environ.get("XENON_WHISPER_COMPUTE", "int8"),
            initial_prompt=os.environ.get(
                "XENON_WHISPER_PROMPT",
                f"{wake_word.capitalize()}, {', '.join(aliases)}.",
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
        store = MemoryStore(os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH))
        assistant = Assistant(
            _observer(),
            store,
            model,
            max_memories=3,
            max_context_chars=2500,
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
            gate=WakeGate(wake_word, aliases=aliases),
            transcribe=transcribe,
            speak=speaker.speak,
            on_command=handle,
        )
        print(f"listening for {wake_word!r}; press Ctrl+C to stop", flush=True)
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
        detector = UtteranceDetector(threshold=vad_threshold, min_speech_ms=300)
        transcriber = WhisperTranscriber(
            os.environ.get("XENON_WHISPER_MODEL", "base"),
            os.environ.get("XENON_WHISPER_DEVICE", "cpu"),
            os.environ.get("XENON_WHISPER_COMPUTE", "int8"),
            initial_prompt=os.environ.get(
                "XENON_WHISPER_PROMPT",
                f"{wake_word.capitalize()}, {', '.join(aliases)}.",
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
        store = MemoryStore(os.environ.get("XENON_DB_PATH", DEFAULT_DB_PATH))
        assistant = Assistant(
            _observer(),
            store,
            model,
            max_memories=3,
            max_context_chars=2500,
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
                ind.set("listening", "")
            text = listen_once(source, detector, transcriber)
            if text:
                print(f"heard: {text!r}", flush=True)
                ind = _ind()
                if ind is not None:
                    ind.set("heard", text)
                lowered = text.lower()
                wake_words = [wake_word, *aliases]
                if not any(lowered.startswith(word.lower()) for word in wake_words if word):
                    print(f"ignored (no wake word): {text!r}", flush=True)
            return text

        def speak(text: str) -> None:
            ind = _ind()
            if ind is not None:
                ind.set("speaking", text[:120])
            speaker.speak(_speakable(text))

        loop = VoiceLoop(
            gate=WakeGate(wake_word, aliases=aliases),
            transcribe=transcribe,
            speak=speak,
            on_command=handle,
        )

        if indicator is not None:
            indicator.set("listening", f"wake word: {wake_word}")
        print(f"listening for {wake_word!r}; close the status window to stop", flush=True)
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
    if args.yes:
        return True
    try:
        reply = input("Run this plan? [y/N] ").strip().lower()
    except EOFError:
        reply = ""
    return reply in {"y", "yes"}


def _run_actions(goal: str, actions, args: argparse.Namespace, verifier=None) -> int:
    from xenon.actions import FileVerifier
    from xenon.windows_actions import ForegroundTitleVerifier, WindowsActionBackend

    if args.expect_title:
        verifier = ForegroundTitleVerifier(args.expect_title)
    elif args.expect_file:
        verifier = FileVerifier(args.expect_file, args.expect_text)

    outcome = TaskRunner(
        ApprovalPolicy(),
        WindowsActionBackend(),
        approver=lambda action: True,
        verifier=verifier,
    ).run(goal, actions)

    print(f"status: {outcome.status}")
    for step in outcome.steps:
        result = step.result.detail if step.result is not None else "not run"
        print(f"  - {step.action.kind}: {result}")
    if outcome.detail:
        print(f"detail: {outcome.detail}")
    return 0 if outcome.status == "succeeded" else 1


def _voice_fast_reply(command: str, backend=None, search_fn=None) -> str | None:
    """Run open/focus/search without a model, or return None for other tasks.

    Only these three deterministic intents auto-approve. Typing, key presses,
    and file writes still go through the normal approval path (or the model).
    """
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

    if backend is None:
        from xenon.windows_actions import WindowsActionBackend

        backend = WindowsActionBackend()
    outcome = TaskRunner(
        ApprovalPolicy(), backend, approver=lambda _: True
    ).run(command, [action])

    ok = bool(outcome.steps) and all(
        step.result is not None and step.result.ok for step in outcome.steps
    )
    if ok:
        if fast.kind == "open_app":
            return f"Opened {fast.label or fast.target}."
        return f"Switched to {fast.target}."
    return f"Couldn't do that: {outcome.detail or 'the action failed'}"


def _speakable(text: str) -> str:
    """Drop markdown punctuation that sounds wrong through SAPI."""
    cleaned = re.sub(r"[`*_#>|~]", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _do(args: argparse.Namespace) -> int:
    if not is_windows():
        print("xenon do requires Windows", file=sys.stderr)
        return 2

    goal = " ".join(args.goal).strip()
    if not goal:
        print("do needs a goal", file=sys.stderr)
        return 2

    # Common commands skip the model entirely: no cold start, no planning
    # round trip. The same approval/verification rules still apply.
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

        return _run_actions(goal, [action], args, verifier=verifier)

    config = chat_config_from_env()
    if config is None:
        print(
            "set XENON_MODEL_API_KEY and XENON_MODEL_NAME to plan tasks",
            file=sys.stderr,
        )
        return 2

    context = _observer().observe()
    screen = f"{context.title}\n{context.text}"[:4000]
    planner = ModelPlanner(OpenAICompatibleChat(config))
    try:
        plan = planner.plan(goal, screen)
    except (ChatUnavailable, PlannerError) as error:
        print(f"planning error: {error}", file=sys.stderr)
        return 1

    _print_plan(goal, plan.actions)
    if args.dry_run:
        return 0
    if not _confirmed(args):
        print("cancelled")
        return 1
    return _run_actions(goal, plan.actions, args)


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

    voice = subparsers.add_parser("voice", help="listen for the wake word and answer aloud")
    voice.add_argument("--mic", default=None, help="input device index or name (or set XENON_MIC_DEVICE)")
    voice.add_argument("--threshold", type=float, default=None, help="VAD energy threshold (or set XENON_VAD_THRESHOLD)")
    voice.add_argument("--list-mics", action="store_true", help="list input devices and exit")
    voice.set_defaults(handler=_voice)

    search = subparsers.add_parser("search", help="search the web (DuckDuckGo)")
    search.add_argument("query", nargs="+")
    search.set_defaults(handler=_search)

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
