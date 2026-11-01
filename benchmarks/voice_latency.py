"""Measure wake-gate and dispatch overhead, with STT/TTS deliberately excluded.

Run from the repository root:

    python -m benchmarks.voice_latency --samples 200

The real wake-to-command budget also contains microphone capture, VAD, and
speech-to-text, none of which exist yet. This benchmark isolates the part Xenon
owns today, so a regression in gating shows up as a number instead of a feeling.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time

from xenon.voice import VoiceLoop
from xenon.wake import WakeGate

GATE_BUDGET_MS = 1.0
DISPATCH_BUDGET_MS = 5.0


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def measure_gate(samples: int) -> list[float]:
    """Time ``feed`` for the common case: one utterance carrying the command."""
    timings: list[float] = []
    for _ in range(samples):
        gate = WakeGate("xenon")
        started = time.perf_counter()
        gate.feed("xenon open notepad")
        timings.append((time.perf_counter() - started) * 1000)
    return timings


def measure_dispatch(samples: int) -> list[float]:
    """Time a full loop tick: wake check, handler, reply, mute/unmute."""
    replies = [f"reply {index}" for index in range(samples)]
    cursor = {"index": 0}
    transcripts = ["xenon summarize this" for _ in range(samples)]

    def transcribe() -> str | None:
        if not transcripts:
            return None
        return transcripts.pop(0)

    loop = VoiceLoop(
        gate=WakeGate("xenon"),
        transcribe=transcribe,
        speak=lambda reply: None,
        on_command=lambda command: replies[cursor["index"]],
    )

    timings: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        loop.tick()
        timings.append((time.perf_counter() - started) * 1000)
        cursor["index"] += 1
    return timings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    samples = max(1, args.samples)

    gate = measure_gate(samples)
    dispatch = measure_dispatch(samples)

    gate_median = statistics.median(gate)
    dispatch_median = statistics.median(dispatch)
    result = {
        "samples": samples,
        "gate_median_ms": round(gate_median, 4),
        "gate_p95_ms": round(percentile(gate, 0.95), 4),
        "dispatch_median_ms": round(dispatch_median, 4),
        "dispatch_p95_ms": round(percentile(dispatch, 0.95), 4),
        "gate_budget_ms": GATE_BUDGET_MS,
        "dispatch_budget_ms": DISPATCH_BUDGET_MS,
        "passed": gate_median <= GATE_BUDGET_MS
        and dispatch_median <= DISPATCH_BUDGET_MS,
    }

    if args.json:
        import json

        print(json.dumps(result))
    else:
        print("excludes microphone capture, VAD, STT, and TTS by design")
        print(
            f"wake gate: median={result['gate_median_ms']}ms "
            f"p95={result['gate_p95_ms']}ms budget={GATE_BUDGET_MS}ms"
        )
        print(
            f"full dispatch: median={result['dispatch_median_ms']}ms "
            f"p95={result['dispatch_p95_ms']}ms budget={DISPATCH_BUDGET_MS}ms"
        )
        print("PASS" if result["passed"] else "FAIL: orchestration overhead exceeded budget")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
