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
- [ ] Test: wake word does not dispatch an action without a following command.
- [ ] Test: wake word plus command dispatches exactly one task.
- [ ] Test: speaking/audio playback cannot retrigger wake detection.
- [ ] Test: requested side effects require an approval event before execution.
- [ ] Test: observe → plan → act → verify returns a truthful final state.
- [ ] Benchmark wake-to-command and command-to-first-response latency.
- [ ] Implement the voice and task orchestrator after these tests exist.

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

- [ ] Scenario: user says “what is on this screen?” and Xenon answers from the foreground app.
- [ ] Scenario: user says “summarize this and save it to my notes,” then approves the write and receives verification.
- [ ] Scenario: user asks a question whose answer is in Obsidian/Notion and Xenon cites the source.
- [ ] Scenario: unplug network; local wake, screen context, and local memory still work.

## Research decisions to validate

- [ ] Evaluate Windows UI Automation as the default perception path.
- [ ] Evaluate `screen-use` only as a reference or optional adapter; do not import its autonomous loop blindly.
- [ ] Evaluate Mem0 concepts against a lean SQLite + FTS baseline before adding a memory framework.
- [ ] Evaluate a local STT model and VAD under actual microphone conditions.
