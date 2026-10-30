"""Benchmark local memory retrieval against the 100 ms Phase 3 budget.

    python -m benchmarks.memory_retrieval --documents 500 --queries 25
"""
from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from pathlib import Path

from xenon.memory import MemoryStore

WORDS = (
    "assistant memory screen latency obsidian notion export meeting project "
    "deadline review design python windows automation keyboard capture voice "
    "retrieval source citation archive taxonomy coffee travel budget invoice"
)


def build_corpus(root: Path, count: int) -> None:
    for index in range(count):
        folder = root / f"area-{index % 20:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        terms = [WORDS[(index * 7 + offset * 3) % len(WORDS)] for offset in range(12)]
        lines = [
            "---",
            f"tags: [area-{index % 20}, generated]",
            "---",
            f"# Note {index}",
            "",
            " ".join(terms),
            f"record {index} durable detail",
        ]
        (folder / f"note-{index:04d}.md").write_text("\n".join(lines), encoding="utf-8")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=500)
    parser.add_argument("--queries", type=int, default=25)
    parser.add_argument("--budget-ms", type=float, default=100.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    queries = ["memory", "obsidian source", "screen latency", "budget invoice", "note"]
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "vault"
        root.mkdir()
        build_corpus(root, args.documents)
        with MemoryStore(Path(temp) / "memory.db") as store:
            started = time.perf_counter()
            result = store.ingest_markdown("bench", root)
            ingest_ms = (time.perf_counter() - started) * 1000

            timings: list[float] = []
            hits = 0
            for index in range(args.queries):
                query = queries[index % len(queries)]
                started = time.perf_counter()
                hits += len(store.search(query, limit=5))
                timings.append((time.perf_counter() - started) * 1000)

    median_ms = statistics.median(timings)
    payload = {
        "documents": args.documents,
        "ingest_ms": round(ingest_ms, 1),
        "median_ms": round(median_ms, 1),
        "p95_ms": round(percentile(timings, 0.95), 1),
        "budget_ms": args.budget_ms,
        "hits": hits,
        "passed": median_ms <= args.budget_ms,
    }
    if args.json:
        import json

        print(json.dumps(payload))
    else:
        print(
            f"corpus: {args.documents} docs ingested in {payload['ingest_ms']}ms"
        )
        print(
            f"retrieval: median={payload['median_ms']}ms p95={payload['p95_ms']}ms "
            f"budget={args.budget_ms}ms hits={hits}"
        )
        print("PASS" if payload["passed"] else "FAIL: median exceeded budget")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
