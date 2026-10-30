# Xenon

A lightweight, local-first Windows personal assistant that works beside its user.

## v1 priorities

1. **Screen context** — read the foreground window through Windows UI Automation first; capture a screenshot only when the accessibility tree is insufficient.
2. **Task assistance** — accept a wake-word voice command or typed request, then observe, plan, act, and verify with explicit approval for consequential actions.
3. **Long-term memory** — ingest user-selected Obsidian Markdown, Notion pages, and exported Claude/Codex memory files into a local, attributable memory store.

## Product boundary

Xenon is a daily-use assistant. It does not include pentesting, trading, multi-agent councils, or a large dashboard. Those remain separate systems.

## Design rules

- Local-first and low-latency by default.
- UI Automation before screenshot vision; vision is a fallback, never a constant video stream.
- Retrieve only relevant memory for each request.
- Every remembered item keeps its source, timestamp, and a delete path.
- Never ingest credentials, session cookies, or hidden product memory automatically.
- Require confirmation before external side effects, destructive operations, or sending messages.

## Status

Supported platform: **Windows 11**, **Python 3.11+**.

Implemented and covered by tests:

- Read-only foreground window adapter: app, title, bounded UI Automation controls.
- Duplicate-node collapsing, password redaction, and a 6000-character context cap.
- Screenshot capture of the active window, used only when UIA has too little content.
- Opt-in OpenAI-compatible vision client, injectable transport for offline tests.
- `python -m xenon screen` prints what Xenon currently sees.
- Latency benchmark with a 250 ms median budget gate.

Not built yet: wake word and task orchestration, the memory store and connectors, and the tray UI.

## Quick start

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,capture,uia]"
python -m pytest -q
python -m xenon screen
```

Enable the vision fallback only if you want screenshots sent to a model:

```powershell
$env:XENON_VISION_API_KEY = "..."
$env:XENON_VISION_MODEL = "your-vision-model"
# optional: $env:XENON_VISION_BASE_URL = "http://localhost:11434/v1"
```

Measure screen-context latency:

```powershell
python -m benchmarks.screen_latency --samples 15
```

## Measured latency

Local run on 30 Oct 2026, Windows 11, Python 3.11.9, pywinauto 0.6.9, Opera in the foreground:

| Path | Cold | Median | p95 | Budget |
| --- | ---: | ---: | ---: | ---: |
| UIA-only observe (depth 4) | 322 ms | **29 ms** | 34 ms | 250 ms |

Cold start is dominated by pywinauto/COM initialization. A long-running service must prewarm the adapter at startup so the first user request does not pay it.

## Planned stack

- Python 3.11+ service
- Windows UI Automation for structured screen context
- Local screen capture plus optional vision fallback
- Wake word + VAD + local STT
- SQLite + FTS for durable memory, with optional semantic retrieval
- Small tray UI and typed command box

See [TODO.md](TODO.md) for the acceptance tests and delivery order.
