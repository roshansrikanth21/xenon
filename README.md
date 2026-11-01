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
- Local SQLite + FTS5 memory: Obsidian Markdown ingestion, tags, source attribution,
  idempotent re-sync, per-file and per-source deletion, ranked retrieval.
- Connectors: read-only Notion page trees, Claude `conversations.json`, Codex JSONL.
- Agent Skills: metadata discovery at startup, instructions loaded only on use,
  keyword matching, and three bundled skills under `skills/`.
- Approval-gated desktop tasks: model planner limited to permitted actions, a
  policy that refuses shell/destructive kinds, and a runner that reports
  `succeeded`, `denied`, `failed`, or `unverified` truthfully.
- Windows actuator for open/focus/type/keys/click/write, plus a foreground-title
  verifier. Real actions require an explicit plan confirmation.
- Startup prewarm for the screen adapter: one throwaway foreground read drops the
  first real observation from 289 ms to 32 ms.
- Wake gate: a bare wake word only arms the next utterance briefly, commands
  without the wake word are ignored, and detection is muted while Xenon speaks.
- Voice loop orchestration with injectable transcribe/speak adapters, so a real
  STT and TTS engine can be dropped in without touching the gating logic.
- Minimal always-on-top Tk command box with a testable presenter.
- Typed assist loop: question + live screen + retrieved memory -> model answer
  with cited source paths.
- `python -m xenon screen` prints what Xenon currently sees.
- `python -m xenon ask "..." [--skill name]` answers from screen and memory.
- `python -m xenon do "..." [--dry-run]` plans and runs an approved task.
- `python -m xenon ui` opens the command box.
- `python -m xenon memory ingest|import|search` and `python -m xenon notion ingest`.
- `python -m xenon skills list|find`.
- Latency benchmarks with budget gates for screen context and memory retrieval.
- End-to-end CLI tests that drive the real HTTP request path against a local
  OpenAI-compatible stub, so command wiring cannot silently break.

Not built yet: the real microphone/VAD/STT/TTS adapters, a tray icon and global
hotkey, and a wake-to-command latency benchmark.

## Quick start

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,capture,uia]"
python -m pytest -q
python -m xenon screen
```

Enable answers and the vision fallback only if you want requests sent to a model:

```powershell
$env:XENON_MODEL_API_KEY = "..."
$env:XENON_MODEL_NAME = "your-chat-model"
# optional: $env:XENON_MODEL_BASE_URL = "http://localhost:11434/v1"

$env:XENON_VISION_API_KEY = "..."
$env:XENON_VISION_MODEL = "your-vision-model"
# optional: $env:XENON_VISION_BASE_URL = "http://localhost:11434/v1"

python -m xenon ask "what does this screen mean for my deadline?"
```

Plan a desktop task before letting it act:

```powershell
python -m xenon do "open notepad and type my standup notes" --dry-run
python -m xenon do "open notepad and type my standup notes" --expect-title Notepad
```

`do` exits non-zero unless verification passes. Without `--expect-title` there is
nothing to check afterwards, so it reports `status: unverified` and exits 1 even
when every step ran. Pass `--expect-title` when you want a success exit code.

Open the command box:

```powershell
python -m xenon ui
python -m xenon ui --no-topmost   # let other windows cover it
```

The box keeps the same screen + memory context as `ask` and prints retrieval
sources under each answer.

Measure screen-context latency:

```powershell
python -m benchmarks.screen_latency --samples 15
python -m benchmarks.screen_latency --samples 15 --prewarm   # as a service would run
python -m benchmarks.voice_latency --samples 500             # gate + dispatch only
```

Mirror an Obsidian vault and query it:

```powershell
$env:XENON_DB_PATH = "memory/xenon.db"
python -m xenon memory ingest --kind obsidian --root "C:\Users\you\vault"
python -m xenon memory search "project deadline"
```

Sync Notion and import chat exports:

```powershell
$env:XENON_NOTION_TOKEN = "secret_..."
$env:XENON_NOTION_ROOT_PAGE = "page-id"
python -m xenon notion ingest

python -m xenon memory import --kind claude --path "$env:USERPROFILE\Downloads\claude-export"
python -m xenon memory import --kind codex --path "$env:USERPROFILE\.codex\sessions"
```

Inspect Agent Skills:

```powershell
python -m xenon skills list
python -m xenon skills find "mirror my obsidian notes"
python -m xenon ask --skill memory-sync "sync my vault"
```

Hidden folders such as `.obsidian` and `.trash` are skipped. Re-running ingest is
idempotent: unchanged files stay untouched, edits replace old text, and deleted
files leave the index.

## Measured latency

Local run on 30 Oct–1 Nov 2026, Windows 11, Python 3.11.9, pywinauto 0.6.9:

| Path | Cold | Median | p95 | Budget |
| --- | ---: | ---: | ---: | ---: |
| UIA-only observe (depth 4), no prewarm | 289 ms | **24 ms** | 28 ms | 250 ms |
| UIA-only observe (depth 4), after `prewarm()` | 32 ms | **24 ms** | 28 ms | 250 ms |
| Full `ask` without vision, end to end | — | **210 ms** | — | — |
| Memory retrieval, 500 docs | ingest 264 ms | **0.1 ms** | 0.9 ms | 100 ms |
| Wake gate + dispatch (no audio) | — | **0.001 ms** | 0.001 ms | 5 ms |

Cold start is dominated by pywinauto/COM initialization. `ScreenObserver.prewarm()`
pays it once at startup (about 335 ms) and reads one real window so the UIA tree
walk is warm too; the first user request drops from 289 ms to 32 ms. The CLI calls
it before its first observation.

The 210 ms `ask` figure excludes the model round trip (it was measured against a
local stub that answers in about 3 ms) and excludes Python interpreter startup,
which adds roughly 0.5 s to a cold `python -m xenon` invocation.

The voice number excludes microphone capture, VAD, STT, and TTS, because those
adapters do not exist yet; it isolates the orchestration overhead Xenon owns.

## Planned stack

- Python 3.11+ service
- Windows UI Automation for structured screen context
- Local screen capture plus optional vision fallback
- Wake word + VAD + local STT
- SQLite + FTS for durable memory, with optional semantic retrieval
- Tray icon, global hotkey, and the Tk command box

See [TODO.md](TODO.md) for the acceptance tests and delivery order.
