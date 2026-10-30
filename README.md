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

## Planned stack

- Python 3.11+ service
- Windows UI Automation for structured screen context
- Local screen capture plus optional vision fallback
- Wake word + VAD + local STT
- SQLite + FTS for durable memory, with optional semantic retrieval
- Small tray UI and typed command box

See [TODO.md](TODO.md) for the acceptance tests and delivery order.
