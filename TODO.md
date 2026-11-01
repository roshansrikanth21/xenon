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
- [x] Implement the typed command box (`python -m xenon ui`).
- [x] Test: every CLI command is registered and its handlers are reachable.
- [x] Benchmark wake-to-command and command-to-first-response latency (gate + dispatch 0.001 ms; audio path still unmeasured).
- [x] Add startup prewarm so the first request skips COM initialization (289 ms -> 32 ms).
- [x] Wire real VAD + STT + TTS adapters behind the voice loop (energy endpointer, faster-whisper, SAPI).
- [ ] Benchmark true wake-to-command latency with microphone capture in the loop.
- [ ] Add a tray icon and a global hotkey for the command box.
- [ ] Keep the service warm so the model, microphone, and screen adapter load once.

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
- [ ] Scenario: user says “summarize this and save it to my notes,” then approves the write and receives verification.
- [ ] Scenario: unplug network; local wake, screen context, and local memory still work.

## Research decisions to validate

- [x] Evaluate Windows UI Automation as the default perception path (29 ms warm; falls back to vision when the tree is empty, as with slide decks and Electron apps).
- [ ] Evaluate `screen-use` only as a reference or optional adapter; do not import its autonomous loop blindly.
- [x] Evaluate Mem0 concepts against a lean SQLite + FTS baseline before adding a memory framework (local retrieval is 0.1 ms median; no framework needed yet).
- [x] Evaluate a local STT model and VAD: faster-whisper `base` in int8 transcribes 2.2 s of speech in 4–6 ms warm; an energy endpointer suffices indoors, and `initial_prompt` plus wake-word aliases are required for an invented name.
- [ ] Evaluate the energy endpointer in a noisy room; replace with WebRTC VAD if false triggers appear.