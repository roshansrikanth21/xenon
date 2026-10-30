"""Xenon command line: ``python -m xenon screen`` / ``memory``."""
from __future__ import annotations

import argparse
import os
import sys

from xenon.memory import MemoryStore
from xenon.screen import ScreenObserver
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

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
