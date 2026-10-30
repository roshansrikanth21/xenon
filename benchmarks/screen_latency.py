"""Measure foreground context latency against the Phase 1 budget.

Run from the repository root:

    python -m benchmarks.screen_latency --samples 10

Exits non-zero when the median observe time exceeds the budget, so it can be
used as a local gate before the UI layer exists.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time

from xenon.screen import CapturedFrame, ScreenObserver, WindowSnapshot
from xenon.windows import UiaControlReader, WindowsUiAutomation


class NoVision:
    """Vision stub: the benchmark only measures the UIA-first path."""

    def describe(self, frame: CapturedFrame | None, snapshot: WindowSnapshot) -> str:
        return ""


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--budget-ms", type=float, default=250.0)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("screen latency benchmark requires Windows", file=sys.stderr)
        return 2

    automation = WindowsUiAutomation(
        controls=UiaControlReader(max_depth=args.depth),
        max_controls=args.limit,
    )
    observer = ScreenObserver(automation, NoVision())

    started = time.perf_counter()
    first = observer.observe()
    cold_ms = (time.perf_counter() - started) * 1000

    warm: list[float] = []
    for _ in range(max(1, args.samples)):
        started = time.perf_counter()
        observer.observe()
        warm.append((time.perf_counter() - started) * 1000)

    median_ms = statistics.median(warm)
    result = {
        "app": first.app_name,
        "title": first.title,
        "context_chars": len(first.text),
        "used_vision": first.used_vision,
        "cold_ms": round(cold_ms, 1),
        "median_ms": round(median_ms, 1),
        "p95_ms": round(percentile(warm, 0.95), 1),
        "budget_ms": args.budget_ms,
        "passed": median_ms <= args.budget_ms,
    }

    if args.json:
        import json

        print(json.dumps(result))
    else:
        print(f"window: {result['app']} | {result['title']}")
        print(f"context: {result['context_chars']} chars, vision={result['used_vision']}")
        print(
            f"latency: cold={result['cold_ms']}ms median={result['median_ms']}ms "
            f"p95={result['p95_ms']}ms budget={args.budget_ms}ms"
        )
        print("PASS" if result["passed"] else "FAIL: median exceeded budget")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
