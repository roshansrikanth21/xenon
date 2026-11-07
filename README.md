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
- Local OCR fallback: when a window exposes no readable text (Electron, canvas,
  slide decks), Windows' built-in OCR reads the screenshot through PowerShell.
  No image leaves the machine and no API key is required; `XENON_OCR=off`
  disables it.
- Screenshot capture of the active window, used only when UIA has too little content.
- Opt-in OpenAI-compatible vision client, injectable transport for offline tests.
- Local SQLite + FTS5 memory: Obsidian Markdown ingestion, tags, source attribution,
  idempotent re-sync, per-file and per-source deletion, ranked retrieval.
- Connectors: read-only Notion page trees, Claude `conversations.json`, Codex JSONL.
- Agent Skills: metadata discovery at startup, instructions loaded only on use,
  keyword matching, and four bundled skills under `skills/`.
- Approval-gated desktop tasks: model planner limited to permitted actions, a
  policy that refuses shell/destructive kinds, and a runner that reports
  `succeeded`, `denied`, `failed`, or `unverified` truthfully.
- Deterministic fast path for "open/launch", "switch/focus", and "search/look up".
  These skip the model entirely, so a common command starts immediately. Voice
  auto-runs only open, focus, and search; typing, key presses, and file writes
  still require explicit approval.
- App catalog (`xenon/apps.py`): discovers launchable apps from App Paths,
  Start-menu shortcuts, and `Get-StartApps`. Resolves spoken aliases such as
  `code` -> Visual Studio Code, launches Store apps via
  `explorer.exe shell:AppsFolder\<AppID>` when no direct path exists, and
  opens websites in the browser when no desktop client is installed. Launch
  polling uses a fast Win32 title scan instead of UI Automation.
- Always-listening voice by default: the microphone stays open and a sentence
  is dispatched after ~350 ms of silence (`--silence-ms`, `XENON_SILENCE_MS`).
  `--wake` (or `XENON_WAKE_MODE=wake`) restores the wake-word requirement.
  Whisper runs with `beam_size = 1` and `base.en` by default.
- Free model presets (`xenon/providers.py`, `xenon providers`): Groq (fastest
  free tier), Gemini (strongest free model), Cerebras, OpenRouter `:free`
  variants, plus local Ollama. Set `XENON_MODEL_PROVIDER` or export just the
  provider key (e.g. `GROQ_API_KEY`). GitHub Models was retired on 2026-07-30.
  Groq's `gpt-oss-20b` and Gemini 3.x Flash are reasoning models whose hidden
  thinking counts against `max_tokens`, so those presets send
  `reasoning_effort: "low"` plus 1024 tokens of headroom; without it a 220-token
  cap can come back empty. Override with `XENON_REASONING_EFFORT` (`off` to omit).
  A short 429 is retried once; longer rate limits, bad keys, and unknown model
  names are reported by name. `doctor` suggests a listed model when a preset
  default has been renamed.
- Model latency controls: `XENON_MAX_TOKENS` (default 220), an Ollama `keep_alive`
  of `2h` on loopback endpoints, and a model warm-up at voice startup. The first
  request after an idle Ollama unload was ~16 s; warm answers are ~1–3 s.
- Spoken answers use a short brief prompt (under 45 words) so speech does not read
  markdown, headings, or file paths aloud.
- Windows actuator for open/focus/type/keys/click/write, plus foreground-title and
  file verifiers. Real actions require an explicit plan confirmation.
- Focus safety: `focus_window` confirms the title it actually reached, and
  `type_text`/`press_keys` refuse to run if the foreground window changed since
  that focus step. `open_app` waits for the new window so a following focus step
  does not race the launcher. Note: some Electron apps (e.g. Notion) title their
  window by the open page, so their launch reports `unverified` even though the
  process started — verification never claims success it cannot see.
- Startup prewarm for the screen adapter: one throwaway foreground read drops the
  first real observation from 289 ms to 32 ms.
- Wake gate: a bare wake word only arms the next utterance briefly, commands
  without the wake word are ignored, and detection is muted while Xenon speaks.
- Voice loop orchestration with injectable transcribe/speak adapters, so a real
  STT and TTS engine can be dropped in without touching the gating logic.
- Working local voice path: energy endpointer over the microphone, faster-whisper
  speech-to-text, and Windows SAPI speech output. No audio leaves the machine.
- Voice usability controls: `voice --list-mics`, `voice --mic <index|name>`,
  `voice --threshold`, plus environment equivalents, a floating status window,
  and "ignored (no wake word)" feedback for background speech.
- Minimal always-on-top Tk command box with a testable presenter.
- Task-oriented assist loop: question + live screen + retrieved memory -> model
  answer that leads with concrete next steps instead of describing the window.
- `python -m xenon screen` prints what Xenon currently sees.
- `python -m xenon doctor` checks screen, model reachability, memory, voice dependencies, microphone, and skills in one report.
- `python -m xenon ask "..." [--skill name]` answers from screen and memory.
- `python -m xenon do "..." [--dry-run]` plans and runs an approved task.
- `python -m xenon ui` opens the command box.
- `python -m xenon voice` listens for the wake word and answers aloud.
- `python -m xenon search "..."` searches the web without an API key.
- `python -m xenon memory ingest|import|search` and `python -m xenon notion ingest`.
- `python -m xenon skills list|find`.
- Latency benchmarks with budget gates for screen context and memory retrieval.
- End-to-end CLI tests that drive the real HTTP request path against a local
  OpenAI-compatible stub, so command wiring cannot silently break.

Not built yet: a tray icon and global hotkey, and an always-listening service
that keeps the microphone and screen adapter warm between commands.

## Daily use

Pick the entry point that matches how you want to interact:

| I want to | Run | First response |
| --- | --- | --- |
| Check why something is broken | `python -m xenon doctor` | full report in ~1–2 s |
| Ask about what is on screen | `python -m xenon ask "..."` | ~1 s start, then model time |
| Ask repeatedly without waiting | `python -m xenon ui` | model stays warm between questions |
| Talk to it | `python -m xenon voice` | always listening; dispatch after ~350 ms silence |
| Open an app or switch windows | `python -m xenon do "open notepad" --yes` | deterministic, no model |
| List / resolve apps | `python -m xenon apps` / `python -m xenon apps chrome` | Start-menu + App Paths catalog |
| See free model options | `python -m xenon providers` | Groq, Gemini, Cerebras, OpenRouter, Ollama |
| Change the desktop | `python -m xenon do "..." --dry-run` first | plan printed, nothing run |
| Look something up | `python -m xenon search "..."` | ~1 s, top five results |

`ui` and `voice` are long-lived processes, which is how the latency numbers stay
low: the model, microphone, and screen adapter load once instead of per command.

## What runs offline

Screen context, local OCR, memory ingest/search, wake detection, speech-to-text,
and speech output never touch the network. Only `ask`, `do`, the optional vision
fallback, and `search` call out; the model endpoint can be local, so point
`XENON_MODEL_BASE_URL` at Ollama or any OpenAI-compatible server on `localhost`.

## If something is not working

Run the doctor first; it names the failing layer instead of leaving you to guess:

```powershell
python -m xenon doctor
```

Typical results:

- `FAIL model` — the model server is not running. Start `ollama serve`, then confirm
  with `python -m xenon doctor` again.
- `WARN screen ... limited` — UI Automation had no text and OCR could not read the
  window either. Bring the target window to the front and retry; if it is a picture,
  configure a vision model.
- `WARN voice:sounddevice` — reinstall the voice extras: `pip install -e ".[voice]"`.
- `WARN microphone` — no input device was found; check Windows sound settings.

## Known limitations

- UI Automation is empty in some apps (slide decks, Electron, canvas views). Xenon
  first tries Windows' built-in OCR on a screenshot, which costs roughly 0.5–2 s
  and reads text only — it cannot describe a chart or a photo. A configured vision
  model is better for those.
- OCR output is lossy on small or unusual fonts, so treat its text as a clue, not
  a transcript. Like any screenshot path, it reads whatever is visible; unlike UI
  Automation, it cannot apply the password-field redaction.
- The fast path recognises a fixed app table plus single executable-looking names.
  Anything else falls through to the model planner. "Open", "focus", and "search"
  auto-run by voice; typing, key presses, and file writes still go through the
  approval path.
- The energy-based endpointer is tuned for a quiet room; a noisy room will need
  a real VAD such as WebRTC.
- `focus_window` matches on window titles, so two windows of the same app with the
  same title are ambiguous. Xenon refuses to type if focus moved after the focus
  step, but it cannot tell two identically titled windows apart.
- `voice` now opens a small always-on-top status window (listening / heard / working / speaking); there is still no tray icon or global hotkey, so start it from a terminal.
- This has only run on one Windows 11 machine, with an English wake word.

## Quick start

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,capture,uia]"
python -m pytest -q
python -m xenon doctor
python -m xenon screen
```

Add the voice dependencies if you want to talk to Xenon:

```powershell
pip install -e ".[voice]"
```

Enable answers and the vision fallback only if you want requests sent to a model:

```powershell
# Local model (no cloud, no key beyond Ollama's placeholder)
$env:XENON_MODEL_API_KEY = "ollama"
$env:XENON_MODEL_NAME = "qwen2.5:7b"
$env:XENON_MODEL_BASE_URL = "http://127.0.0.1:11434/v1"
$env:XENON_MAX_TOKENS = "220"      # optional; lower is faster
$env:XENON_KEEP_ALIVE = "2h"       # optional; local Ollama stays loaded

python -m xenon ask "what does this screen mean for my deadline?"
python -m xenon search "python asyncio tutorial"
```

`XENON_KEEP_ALIVE` is only sent to loopback model endpoints. Set it to `off`
if you would rather let Ollama unload the model when idle; the next request
will then pay a cold-load cost of roughly 16 s.

Local OCR is on by default and needs no configuration. To switch it off, or to
add a real vision model for diagrams and photos:

```powershell
$env:XENON_OCR = "off"

$env:XENON_VISION_API_KEY = "..."          # opt-in; a text-model key is not reused
$env:XENON_VISION_MODEL = "your-vision-model"
# optional: $env:XENON_VISION_BASE_URL = "http://localhost:11434/v1"
```

Plan a desktop task before letting it act:

```powershell
python -m xenon do "open notepad and type my standup notes" --dry-run
python -m xenon do "open notepad and type my standup notes" --expect-title Notepad
python -m xenon do "save my notes to C:\notes\today.md" --expect-file C:\notes\today.md --expect-text standup
```

`do` exits non-zero unless verification passes. Without `--expect-title`,
`--expect-file`, or `--expect-text` there is nothing to check afterwards, so it
reports `status: unverified` and exits 1 even when every step ran.

The common deterministic commands skip the model and its latency:

```powershell
python -m xenon do "open notepad" --yes
python -m xenon do "switch to chrome" --yes
python -m xenon do "search for python asyncio"
```

`open` is verified against the whole desktop (Windows may open a new app behind
the current window) and `focus` against the foreground title. The same commands
by voice auto-run; anything else spoken still uses the model and its approval
rules.

Open the command box:

```powershell
python -m xenon ui
python -m xenon ui --no-topmost   # let other windows cover it
```

The box keeps the same screen + memory context as `ask` and prints retrieval
sources under each answer.

Talk to Xenon locally (microphone, no cloud speech services):

```powershell
python -m xenon voice --list-mics       # find your input device
python -m xenon voice --mic 2           # index or a name substring
# then say: "Xenon, what is on my screen?"
```

If Xenon hears speech but ignores it, the log and the floating window show
`ignored (no wake word): '...'`. If it triggers on room noise, raise the energy
threshold (`--threshold 0.02`, default `0.015`). Speech recognition accuracy can
be improved with `XENON_WHISPER_BEAM=5` or `XENON_WHISPER_MODEL=small`.

Speech output uses Windows SAPI and speech input uses faster-whisper, so audio
never leaves the machine. The first run downloads the Whisper model (about 75 MB
for `base`); later runs load it in about 2 s and transcribe a few seconds of
speech in under 10 ms on CPU. Increase accuracy with `XENON_WHISPER_MODEL=small`
or use `XENON_WHISPER_DEVICE=cuda` when a GPU is available.

Whisper does not always spell an invented name the way you say it, so Xenon sends
a short `initial_prompt` containing the wake word and accepts extra spellings from
`XENON_WAKE_ALIASES` (default `zenon, see none`).

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
| Local OCR observe, Electron window (no UIA text) | — | **~0.7 s** | — | — |
| Full `ask` without vision, end to end | — | **210 ms** | — | — |
| Full `ask` with Ollama `qwen2.5:7b` and a live screen context, 220-token cap | — | **3.9–5.3 s** | — | — |
| Ollama `qwen2.5:7b`, first request after an idle unload | **15.9 s** | — | — | — |
| Ollama `qwen2.5:7b`, warm with `keep_alive: "2h"` | — | **1.1 s** | — | — |
| Deterministic `do "open notepad"` (no model) | — | **0.80 s** | — | — |
| Deterministic `do "open chrome"` via app-id + Win32 title poll | — | **1.17 s** | — | — |
| Deterministic `do "open calculator"` | — | **0.85 s** | — | — |
| Deterministic `do "switch to notepad"` (no model) | — | **0.13 s** | — | — |
| Memory retrieval, 500 docs | ingest 264 ms | **0.1 ms** | 0.9 ms | 100 ms |
| Wake gate + dispatch (no audio) | — | **0.001 ms** | 0.001 ms | 5 ms |
| Voice: 2.2 s of speech, warm Whisper `base` | model load 1.9 s | **4–6 ms** | 6 ms | — |

Cold start is dominated by pywinauto/COM initialization. `ScreenObserver.prewarm()`
pays it once at startup (about 335 ms) and reads one real window so the UIA tree
walk is warm too; the first user request drops from 289 ms to 32 ms. The CLI calls
it before its first observation.

The 210 ms `ask` figure excludes the model round trip (it was measured against a
local stub that answers in about 3 ms) and excludes Python interpreter startup,
which adds roughly 0.5 s to a cold `python -m xenon` invocation. The 3.9–5.3 s
figures are a real 7B model on CPU with a live screen context; the 15.9 s figure
is the same model after Ollama had unloaded it. `keep_alive` is what removes that
cold load, and the 220-token cap stops long rambling answers.

The 0.001 ms voice figure is gate + dispatch only; it excludes microphone capture,
VAD, STT, and TTS. Those adapters now exist, but true wake-to-command latency with
a microphone in a real room is still unmeasured.

## Planned stack

- Python 3.11+ service
- Windows UI Automation for structured screen context
- Local screen capture plus optional vision fallback
- Wake word + VAD + local STT
- SQLite + FTS for durable memory, with optional semantic retrieval
- Tray icon, global hotkey, and the Tk command box

See [TODO.md](TODO.md) for the acceptance tests and delivery order.
