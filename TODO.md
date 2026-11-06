# Xenon v1 TODO

## Definition of done

Xenon can be left running on Windows, wake on a chosen wake word, understand a task using the current screen as context, carry out approved actions, and remember user-approved knowledge from selected sources.

## Phase 0 — Foundation

- [x] Define supported Windows and Python versions (Windows 11, Python 3.11+).
- [x] Add a local `.env.example`; never commit keys or private memory.
- [x] Add CI for unit tests (Ubuntu 3.11/3.12) plus a non-blocking live Windows check.
- [ ] Add a shared fixture layer for synthetic UI Automation trees, screenshots, and memory sources.

## Phase 1 — Screen Context (test first)

- [x] Test: identify the foreground app, title, process, and UI Automation controls from a fixture.
- [x] Test: redact configured sensitive fields before screen context reaches a model.
- [x] Test: answer a context question from UIA-only input without a screenshot.
- [x] Test: invoke screenshot/vision fallback only when UIA confidence is insufficient.
- [x] Test: when vision is unavailable, keep the accessible text and tell the model plainly that pixels could not be read instead of presenting an error as content.
- [x] Implement a local OCR fallback using Windows' built-in OCR, with a provider chain and an `XENON_OCR` off switch.
- [x] Test: no image leaves the machine and no vision key is required for OCR; text-model credentials alone never enable the cloud vision client.
- [x] Test: capture-to-structured-context median latency stays below 250 ms for UIA-only screens (measured 35 ms warm; 337 ms cold).
- [x] Implement the screen observer and live Windows adapter.

## Phase 2 — Wake and Task Loop (test first)

- [x] Test: build one answer request from screen context plus cited memory.
- [x] Implement the typed assist loop (`python -m xenon ask`).
- [x] Test: requested side effects require an approval event before execution.
- [x] Test: observe → plan → act → verify returns a truthful final state.
- [x] Test: the planner may only propose permitted action kinds.
- [x] Implement the approval runner, model planner, Windows actuator, and `do` command.
- [x] Test: wake word does not dispatch an action without a following command.
- [x] Test: wake word plus command dispatches exactly one task.
- [x] Test: speaking/audio playback cannot retrigger wake detection.
- [x] Implement the wake gate and voice loop orchestration with injectable audio adapters.
- [x] Add microphone selection (`--mic`, `XENON_MIC_DEVICE`), a VAD threshold knob, and ignored-utterance feedback.
- [x] Make answers task-oriented: lead with a concrete next step instead of narrating the window.
- [x] Add `search` (DuckDuckGo, no key) and fix `open_app`/`focus_window` races so real desktop actions complete.
- [x] Add a deterministic fast path for open/focus/search so common commands skip the model entirely.
- [x] Auto-run deterministic open/focus/search by voice; keep typing, key presses, and file writes approval-bound.
- [x] Cap output tokens (`XENON_MAX_TOKENS`, default 220), send `keep_alive` to local Ollama, and warm the model at voice startup.
- [x] Add brief spoken answers (under 45 words, no markdown or file paths read aloud).
- [x] Add `doctor`: one read-only report for screen, model reachability, memory, voice deps, microphone, and skills.
- [x] Implement the typed command box (`python -m xenon ui`).
- [x] Test: every CLI command is registered and its handlers are reachable.
- [x] Benchmark wake-to-command and command-to-first-response latency (gate + dispatch 0.001 ms; audio path still unmeasured).
- [x] Add startup prewarm so the first request skips COM initialization (289 ms -> 32 ms).
- [x] Wire real VAD + STT + TTS adapters behind the voice loop (energy endpointer, faster-whisper, SAPI).
- [ ] Benchmark true wake-to-command latency with microphone capture in the loop.
- [x] Add a floating voice status indicator (listening/heard/working/speaking).
- [ ] Add a tray icon and a global hotkey for the command box.
- [x] Keep the voice process warm: model loaded with `keep_alive`, microphone open, screen adapter prewarmed.

## Phase 3 — Memory (test first)

- [x] Test: ingest an Obsidian Markdown fixture with path, tags, timestamps, and source attribution.
- [x] Implement the local SQLite + FTS store and the Obsidian connector.
- [x] Test: ingest a Notion export/API fixture without duplicating unchanged content.
- [x] Test: ingest user-provided Claude/Codex memory exports without reading hidden application stores.
- [x] Test: retrieve relevant memories while excluding unrelated sources and deleted records.
- [x] Test: delete a source and all derived memories deterministically.
- [x] Benchmark local memory retrieval below 100 ms for the initial corpus target (0.1 ms median at 500 documents).
- [x] Implement the Notion connector (read-only page tree, cited by URL).
- [x] Implement the Claude/Codex export connector (JSON, JSONL, Markdown).
- [x] Add Agent Skills discovery with progressive disclosure and bundled skills.

## Phase 4 — End-to-end validation

- [x] Scenario: user types “what is on this screen?” and Xenon answers from the foreground app (verified against a local stub model).
- [x] Scenario: Xenon plans one step, runs it after approval, and reports `unverified` when no verifier is configured (verified live).
- [x] Scenario: user asks a question whose answer is in a Claude export and Obsidian, and both are cited.
- [x] Test: `ask` and `do --dry-run` run end-to-end against a local stub model.
- [x] Scenario: user says “summarize this and save it to my notes,” then approves the write and receives verification (verified live: write + file-content verification, exit 0).
- [x] Scenario: real desktop actuation opens an app and verifies the foreground title (verified live).
- [x] Scenario: "open notepad" and "switch to notepad" run without a model and verify truthfully (verified live: 0.80 s open, 0.13 s focus).
- [x] Scenario: `ask` reads an Electron window with empty UI Automation through local OCR and returns a next step (verified live against Ollama `qwen2.5:7b`: ~11 s, `screen via screenshot`).
- [ ] Scenario: unplug network; local wake, screen context, and local memory still work.

## Research decisions to validate

- [x] Evaluate Windows UI Automation as the default perception path (29 ms warm; falls back to vision when the tree is empty, as with slide decks and Electron apps).
- [ ] Evaluate `screen-use` only as a reference or optional adapter; do not import its autonomous loop blindly.
- [x] Evaluate Mem0 concepts against a lean SQLite + FTS baseline before adding a memory framework (local retrieval is 0.1 ms median; no framework needed yet).
- [x] Evaluate a local STT model and VAD: faster-whisper `base` in int8 transcribes 2.2 s of speech in 4–6 ms warm; an energy endpointer suffices indoors, and `initial_prompt` plus wake-word aliases are required for an invented name.
- [ ] Evaluate the energy endpointer in a noisy room; replace with WebRTC VAD if false triggers appear.