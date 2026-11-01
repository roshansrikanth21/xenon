"""Xenon command line: ``python -m xenon screen`` / ``memory``."""
from __future__ import annotations

import argparse
import os
import sys

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


class UnconfiguredVision:
    """Vision provider that explains how to enable the fallback."""

    def describe(self, frame, snapshot) -> str:
        raise VisionUnavailable(
            "set XENON_VISION_API_KEY and XENON_VISION_MODEL to enable vision fallback"
        )


def _observer():
    from xenon.windows import WindowsUiAutomation

    automation = WindowsUiAutomation()
    config = config_from_env()
    vision = OpenAICompatibleVision(config) if config is not None else UnconfiguredVision()
    return ScreenObserver(automation, vision, capture=automation)


def _screen(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("xenon screen requires Windows", file=sys.stderr)
        return 2
    context = _observer().observe()
    print(f"app: {context.app_name}")
    print(f"title: {context.title}")
    print(f"source: {'vision' if context.used_vision else 'ui-automation'}")
    if context.error:
        print(f"warning: {context.error}")
    if context.text:
        print("--- context ---")
        print(context.text)
    return 0


def _ask(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
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
    source = "vision" if answer.used_vision else "ui-automation"
    print(f"\n[{answer.latency_ms:.0f} ms, screen via {source}]")
    return 0


def _do(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("xenon do requires Windows", file=sys.stderr)
        return 2

    config = chat_config_from_env()
    if config is None:
        print(
            "set XENON_MODEL_API_KEY and XENON_MODEL_NAME to plan tasks",
            file=sys.stderr,
        )
        return 2

    goal = " ".join(args.goal).strip()
    context = _observer().observe()
    screen = f"{context.title}\n{context.text}"[:4000]
    planner = ModelPlanner(OpenAICompatibleChat(config))
    try:
        plan = planner.plan(goal, screen)
    except (ChatUnavailable, PlannerError) as error:
        print(f"planning error: {error}", file=sys.stderr)
        return 1

    print(f"plan for: {goal}")
    for index, action in enumerate(plan.actions, start=1):
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

    if args.dry_run:
        return 0

    if not args.yes:
        try:
            reply = input("Run this plan? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in {"y", "yes"}:
            print("cancelled")
            return 1

    from xenon.windows_actions import ForegroundTitleVerifier, WindowsActionBackend

    verifier = (
        ForegroundTitleVerifier(args.expect_title) if args.expect_title else None
    )
    outcome = TaskRunner(
        ApprovalPolicy(),
        WindowsActionBackend(),
        approver=lambda action: True,
        verifier=verifier,
    ).run(goal, plan.actions)

    print(f"status: {outcome.status}")
    for step in outcome.steps:
        result = step.result.detail if step.result is not None else "not run"
        print(f"  - {step.action.kind}: {result}")
    if outcome.detail:
        print(f"detail: {outcome.detail}")
    return 0 if outcome.status == "succeeded" else 1


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

    ask = subparsers.add_parser("ask", help="answer a question from screen and memory")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--skill", default="", help="load one Agent Skill by name")
    ask.set_defaults(handler=_ask)

    do = subparsers.add_parser("do", help="plan and run an approved desktop task")
    do.add_argument("goal", nargs="+")
    do.add_argument("--dry-run", action="store_true", help="print the plan only")
    do.add_argument("--yes", action="store_true", help="skip the plan confirmation")
    do.add_argument("--expect-title", default="", help="verify the foreground title contains this")
    do.set_defaults(handler=_do)

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
