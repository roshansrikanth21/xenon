"""Xenon command line: ``python -m xenon screen``."""
from __future__ import annotations

import argparse
import sys

from xenon.screen import ScreenObserver
from xenon.vision import OpenAICompatibleVision, VisionUnavailable, config_from_env


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xenon", description="Local screen-aware assistant")
    subparsers = parser.add_subparsers(dest="command", required=True)
    screen = subparsers.add_parser("screen", help="describe the active window")
    screen.set_defaults(handler=_screen)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("xenon currently supports Windows only", file=sys.stderr)
        return 2
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
