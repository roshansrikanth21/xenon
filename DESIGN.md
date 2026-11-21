# Xenon — design, decisions and work log

> **READ THIS FIRST — THIS FILE IS APPEND-ONLY.**
> Never overwrite, truncate or regenerate `DESIGN.md`. Every new session or
> change appends a new dated section at the bottom. If something earlier turns
> out to be wrong, add a correction in the new section instead of editing the
> old one. The history is the point: it is the context a later model (or a
> human) needs to continue without re-discovering everything.

Owner: Roshan Srikanth · Repo: `https://github.com/roshansrikanth21/xenon` ·
Platform: Windows 11, Python 3.11+ · Started: 2026-10-30 · This log: 2026-11-22

---

## 1. What Xenon is

A local-first, screen-aware desktop assistant that lives in a notch at the top
of the screen. It always listens, wakes on a spoken name, answers aloud, drives
the local machine, and keeps memory on disk. Cloud models are optional; screen
reading, OCR, speech-to-text, speech output, memory and control are local.

The daily-use target, in the user's words:

- Always-listening voice that is reliable, not a demo.
- Saying "Xenon" activates it and brings the orb out of the notch.
- Alexa-style barge-in: while the user talks, duck or pause **all** system
  audio (music, browser, games), track from the first word to the last, then
  restore every app's exact previous level. The same ducking applies while
  Xenon itself speaks.
- Xenon must never transcribe its own TTS back as a command.
- Clicking a text field offers 2–3 non-sensitive suggestions from a local
  profile. It never auto-fills; clicking a suggestion fills it.
- Navigation of the computer by voice, including pause/resume of music.
- A custom wake word renames the whole UI (example: "Rose").
- One orb per session, no process fights, no unrelated Python process killed.
- Each change is reviewable on its own branch, then merged.

Non-goals: a cloud account, telemetry, reading hidden app stores, autonomous
actions without approval, or killing processes Xenon did not start.

---

## 2. Hard rules and safety boundaries

1. **No unrelated process is killed.** Xenon only closes processes it started
   itself (music widget, snapshots, helpers). `graphify-mcp` and the
   `D:\c0mr4des_terminal` scripts are never touched.
2. **No auto-fill of forms.** Suggestions are shown; the user clicks one.
   Password, card, CVV, OTP, PIN, SSN, token and private-key fields return
   nothing at all.
3. **Profile data stays local** in `%APPDATA%\xenon\profile.json`.
4. **Approval-gated actions.** Open/focus/search may auto-run. Typing, key
   presses, file writes and destructive actions require explicit approval.
5. **Truthful verification.** A desktop action reports `unverified` rather than
   claiming success it cannot observe.
6. **One orb per Windows session.** A named mutex (`Local\XenonOrb`) makes a
   second launch exit or forward "listen now" to the running orb.
7. **Launch processes through WMI** (`Invoke-CimMethod Win32_Process Create`).
   `Start-Process` children are killed by the job-object cleanup of the agent
   shell.
8. **No em dashes or decorative noise in user-facing copy** is a style
   preference, not a runtime rule.

---

## 3. Architecture map

| Area | Files | Responsibility |
|---|---|---|
| CLI / orchestration | `xenon/__main__.py` | subcommands, voice loop, duck hold logic, orb launch, mutex |
| Audio | `xenon/audio.py` | `UtteranceDetector`, `BargeInDetector`, `MicrophoneSource`, `WhisperTranscriber`, `SapiSpeaker` |
| Ducking | `xenon/duck.py` | per-app Core Audio sessions through pycaw; exact save/restore |
| Wake word | `xenon/wake.py` | `WakeGate`, aliases, speaking suppression |
| Identity | `xenon/identity.py` | one name for wake word, orb, notch, tray, 3D core |
| Navigation | `xenon/navigate.py` | spoken known folders; never guesses a path |
| Forms | `xenon/field_assistant.py` | field classification, local profile, click-to-fill popup |
| Orb model / render | `xenon/orb_model.py`, `xenon/orb_render.py` | pure state, Pillow frames, notch, breathing |
| Orb shell | `xenon/orb.py` | Tk window, hotkeys, tray, start-at-sign-in, remote server |
| 3D core | `xenon/static/core.html` | Three.js core, load progress, state hue, fullscreen |
| Holo / gestures | `xenon/static/holo.html`, `xenon/static/holo/*.js` | MediaPipe landmarks → pinch/palm/fist/point |
| Music widget | `xenon/music_widget.py`, `xenon/music_scene.py`, `xenon/music_settings.py` | wallpaper/overlay player, lyrics, dock |
| Verification | `xenon/verify.py` | real microphone, SAPI, pycaw, UIA checks |
| Tests | `tests/` | 1126 tests, 633 subtests, 1 skipped |

---

## 4. Work history

### 4.1 2026-10-21 to 2026-10-30 — foundation (already on `main`)

Screen context (UIA + OCR fallback), memory (SQLite/FTS), model providers,
approval-gated desktop actions, fast path, app catalog, command box, voice
loop with injectable adapters, benchmarks, doctor, skills. This is the base the
rest of the log builds on.

### 4.2 2026-11-21 — rough edges (on `main`)

Commit `3f2892f`:

- No self-echo: speech-tail drain plus `_is_own_echo`; Xenon's own TTS can
  never be transcribed as a command.
- Orb survives bad frames and `TclError` around quit; late callbacks dropped.
- Hotkey fallback bindings because PowerToys/IMEs grab `Ctrl+Alt+Space`.
- One orb per session through the named mutex.
- Tray item that floats the music widget above windows
  (`float_above_windows()`).

Commit `56498fa` (CI repair): `cover_name()` splits on both separators;
`psutil` added to `orb` and `dev` extras; Windows-only lyric-wipe test skipped
on Linux. CI green (run `44813123453`).

Commit `5eb2843`: wake word defaults to "xenon" with alias "zenon"; saying it
calls `_summon()` and unfolds the orb out of the notch.

### 4.3 2026-11-21 — P0 barge-in (on `main`)

Commit `4ed3d62`:

- `SapiSpeaker.speak_async()`, `stop()`, `is_speaking()`, `set_volume()`.
  SAPI flags verified: 0 synchronous, 1 asynchronous, 3 purge.
- `BargeInDetector` learns the mic's self-leak for ~350 ms, then compares the
  user's level against it.
- The voice loop watches the microphone while Xenon speaks: stage 1 ducks,
  stage 2 after ~180 ms stops speech and reopens listening.

Commit `377358e` — revised P0, **system-wide ducking**:

- Ducking starts on the first detected speech frame, not only while Xenon
  speaks.
- `Ducker.duck()` lowers every other app's per-session volume; `restore()`
  puts every session back to its exact saved level.
- Hold through the utterance plus `SPEECH_DUCK_TAIL_S = 0.8` after the last
  word. If a command was given, the duck stays held until the reply finishes.
- Background speech without a wake word also ducks, then restores.

Result: 1088 tests, CI green.

### 4.4 2026-11-22 — seven feature branches

Each branch was cut from `main` at `377358e`, tested alone, and pushed without
merging, so a reviewing model can diff one concern at a time.

| Branch | Commit | What it adds |
|---|---|---|
| `feature/form-field-suggestions` | `ac719ca` | `xenon fields`; classify a clicked field from its UIA label + window title; 2–3 variations from `profile.json`; secrets excluded; click-to-fill only |
| `feature/core-3d-polish` | `e7cd4a2` | GLB load-progress bar, fade-out, click-to-fullscreen, state-coloured core glow and dust |
| `feature/voice-quality` | `f40bca2` | `XENON_TTS_RATE`, `XENON_TTS_VOLUME`, `XENON_TTS_VOICE`, `xenon voice --list-voices`, `XENON_WHISPER_LANGUAGE`, background Whisper warm-up |
| `feature/orb-visuals` | `227602a` | `XENON_ORB_POSITION=left\|center\|right` for ultrawide; multi-monitor offsets respected |
| `feature/music-widget-polish` | `0b50664` | `start_on_top` setting, `xenon music-widget --overlay`, `XENON_MUSIC_OVERLAY=1`, settings checkbox |
| `feature/smarter-navigation` | `bc3c914` | `xenon goto desktop\|documents\|downloads\|pictures\|music\|videos\|home`; unknown places are never guessed into a path |
| `feature/custom-wake-word-ui` | `9d41caa` | `XENON_NAME=Rose` drives wake word, orb idle label, notch wordmark, tray tooltip and 3D core brand |

Test counts per branch: 1092–1094 passed. None had CI because `ci.yml` only
runs on `main` and pull requests.

### 4.5 2026-11-22 — integration merge (this session)

All seven branches merged into `main` with `--no-ff` merge commits, in this
order:

| Merge commit | Branch |
|---|---|
| `89fc2d4` | form-field suggestions |
| `0d2b0eb` | 3D core polish |
| `21faffd` | voice quality |
| `71d7a61` | orb notch placement |
| `fb850aa` | music widget overlay |
| `79e3836` | voice folder navigation |
| `5fc9403` | custom wake word / UI naming |

Conflicts and how they were resolved:

1. `xenon/__main__.py` (navigation merge): both sides added a handler before
   `_claim_orb_mutex`. Kept **both** `_fields()` and `_goto()`; the `goto`
   subparser got its own `goto` variable instead of reusing `fields`.
2. `xenon/static/core.html` (identity merge): core-polish had state-hue +
   xtreme/game labels, identity had brand-name replacement. Combined into one
   poll block: brand update, `XENON` → configured name replacement, state hue,
   then xtreme/game override.

`NEXT_STEPS.md` auto-merged each time; only the union of completed items
remains checked.

Integration result: **1115 passed, 1 skipped** before `xenon verify` was added.

### 4.6 2026-11-22 — "make it real": `xenon verify`

The unit suite deliberately uses fakes (see section 5). To test actual
hardware, `xenon/verify.py` and the `xenon verify` command were added:

```
python -m xenon verify          # read-only: platform, deps, SAPI, mic list, pycaw, UIA, profile
python -m xenon verify --mic    # opens the microphone for one second
python -m xenon verify --speak  # says one sentence through SAPI
python -m xenon verify --duck   # ducks other apps for 1.5 s, then restores exact levels
python -m xenon verify --all    # all of the above
```

It prints `PASS` / `FAIL` / `SKIP` per check and exits non-zero only on a real
failure. First run on this machine: 7/7 passed or skipped, 0 failed; SAPI has
two voices (Microsoft David Desktop first), 24 input devices, pycaw and UI
Automation ready, no `profile.json` yet (SKIP, expected).

`field_assistant.profile_path()` was added so the verifier and the form helper
resolve the same profile file.

Final suite after this work: **1126 passed, 1 skipped, 633 subtests**.

---

## 5. Why the tests contain "fake" things

This was an explicit question, so it is recorded here.

`tests/` uses `FakeSpeaker`, `FakeDucker`, `FakeDetector`, synthetic audio
frames, a stub model and a fake SAPI engine. That is intentional and correct:

- A unit test must not open the microphone, change the user's volume, wait on
  Whisper, or speak out loud. Those tests would be slow, flaky and intrusive.
- The fakes prove the **logic**: wake gating, barge-in thresholds, duck/restore
  ordering, profile classification, secret exclusion, CLI wiring, orb state.
- The **production** path is real, not fake: `make_ducker()` returns a real
  pycaw `Ducker` on Windows, `MicrophoneSource` opens sounddevice, `SapiSpeaker`
  drives SAPI, `WhisperTranscriber` loads faster-whisper, and
  `FieldAssistant` reads real UI Automation elements.

The gap was never that the code was fake; it was that no automated test proved
the real adapters work on this machine. `xenon verify` closes that gap, and the
manual test plan below closes it for the interaction paths.

---

## 6. How to run, and the manual test plan

Interpreter on this machine:
`C:\Users\rosha\AppData\Local\Programs\Python\Python311\python.exe`

```powershell
cd C:\Users\rosha\Documents\Codex\2026-10-30\check\outputs\xenon

# 1. Real-hardware report (safe, read-only)
python -m xenon verify

# 2. Full hardware exercise: speaks, records 1 s, ducks audio 1.5 s
python -m xenon verify --all

# 3. The orb (this is the main thing to test)
python -m xenon orb
```

Manual checklist:

1. **Wake word.** Orb folded in the notch. Say "Xenon" (or "Zenon"). It should
   unfold and listen. Try "Xenon, open notepad".
2. **Barge-in.** While Xenon is speaking, start talking. It should stop within
   roughly 200 ms and listen.
3. **Ducking.** Play music in Spotify or a browser. Talk. Music should drop to
   ~15% while you speak, stay down through the reply, then return to exactly
   its previous level.
4. **Self-echo.** Let Xenon answer a long question. It must not treat its own
   reply as a new command.
5. **Navigation.** Say "go to downloads", "open my documents folder",
   "take me to the desktop". An unknown place should be refused, not guessed.
6. **Forms.** Create `%APPDATA%\xenon\profile.json` (schema below), run
   `python -m xenon fields`, click a text field in a browser. A popup offers
   2–3 variations; click one to fill. A password/card/OTP field must offer
   nothing.
7. **3D core.** With the orb running, open
   `http://127.0.0.1:8766/core#k=<key>`; the key is in
   `%APPDATA%\xenon\remote.json`. Check the load bar, click for fullscreen,
   and the state colour.
8. **Music widget.** `python -m xenon music-widget --overlay` (or
   `Ctrl+Alt+Shift+M`) should float it above windows.
9. **Rename.** `set XENON_NAME=Rose`, restart the orb; tray tooltip, notch and
   core should say Rose, and "Rose" should wake it.
10. **No collateral damage.** Confirm `graphify-mcp` and the terminal scripts
    are still running and untouched.

Profile schema:

```json
{
  "name": "Roshan Srikanth",
  "email": "you@example.com",
  "phone": "",
  "bio": "",
  "short_bio": "",
  "skills": "python, security, reverse engineering",
  "portfolio": "",
  "linkedin": "",
  "github": "",
  "headline": "",
  "extra": {"resume": "C:\\path\\a.pdf|C:\\path\\b.pdf"}
}
```

Hotkeys: `Ctrl+Alt+X` ask · `Ctrl+Alt+V` voice · `Ctrl+Alt+Space`
push-to-talk (with fallbacks) · `Ctrl+Alt+B` dim · `Ctrl+Alt+Esc` stop ·
`Ctrl+Alt+G` game mode · `Ctrl+Alt+Shift+G` xtreme ·
`Ctrl+Alt+Shift+M` music widget on top.

---

## 7. Environment knobs

| Variable | Default | Meaning |
|---|---|---|
| `XENON_VOICE_AUTO` | on | `0` disables always-listening voice |
| `XENON_WAKE_WORD` / `XENON_NAME` | `xenon` | wake word and UI name |
| `XENON_WAKE_MODE` | always | `wake` requires the wake word again |
| `XENON_MIC_DEVICE` | default | input device index or name |
| `XENON_VAD_THRESHOLD` | tuned | energy threshold |
| `XENON_SILENCE_MS` | 350 | silence before dispatch |
| `XENON_WHISPER_MODEL` | `base.en` | faster-whisper model |
| `XENON_WHISPER_LANGUAGE` | `en` | transcription language |
| `XENON_WHISPER_WARMUP` | on | load the model in the background |
| `XENON_WHISPER_PROMPT` | wake-word hint | initial prompt |
| `XENON_WHISPER_BEAM` | 1 | beam size |
| `XENON_TTS_RATE` | 1 | SAPI rate |
| `XENON_TTS_VOLUME` | 100 | SAPI volume |
| `XENON_TTS_VOICE` | system | SAPI voice name substring |
| `XENON_DUCK` | on | `off` disables system ducking |
| `XENON_BARGE_IN` | on | `off` disables interruption detection |
| `XENON_BARGE_IN_RATIO` | 1.6 | stage-1 speech ratio |
| `XENON_BARGE_IN_STOP_RATIO` | 2.4 | stage-2 stop ratio |
| `XENON_BARGE_IN_MS` | 180 | stage-2 delay |
| `XENON_BARGE_IN_VOLUME` | 35 | reply volume while interrupted |
| `XENON_ORB_POSITION` | center | `left` / `right` on ultrawide |
| `XENON_ORB_NOTCH` | on | `off` for free-floating mode |
| `XENON_MUSIC_OVERLAY` | off | start the music player above windows |
| `XENON_MAX_TOKENS` | 220 | model output cap |
| `XENON_REASONING_EFFORT` | preset | `off` removes the field |

---

## 8. Known gaps (honest list)

1. **Holo hand control has not been tested with real hands.** The gesture engine
   (pinch, point, fist, palm, palm-up, double-pinch, resize, palms-apart,
   palm-flip) and the `media play pause` tile exist, but no real webcam session
   has validated lighting, latency or false poses.
2. **No packaging.** No `[project.scripts]` entry point, no PyInstaller/Inno
   Setup recipe, no `.exe`, no autostart for the voice process (only the orb
   tray's "Start at sign-in").
3. **Energies, not WebRTC.** The endpointer is tuned for a quiet room; a noisy
   room may need a real VAD.
4. **Wake-to-command latency with the mic in the loop is unmeasured.**
5. **One machine, English.** Everything has only run on this Windows 11 box.
6. **Music lyrics/follow-the-singing and gesture pause/resume were pre-existing
   and were not re-verified in this session.** They are checked in the
   checklist as "present", not as "proven live".
7. **Docs drift.** `README.md` still claims there is no tray icon or global
   hotkey; both exist. `TODO.md` still lists the shared UIA fixture layer.
8. **Groq key hygiene.** A key was pasted into chat earlier; rotate it.
9. **CI coverage.** `ci.yml` triggers on `main` and PRs only; the seven feature
   branches never ran CI.

---

## 9. Branch and commit index (for the auditing model)

`main` after this session contains all seven merges. Original branches are
kept, unmodified, for per-feature diffs:

```
feature/form-field-suggestions   ac719ca
feature/core-3d-polish           e7cd4a2
feature/voice-quality            f40bca2
feature/orb-visuals              227602a
feature/music-widget-polish      0b50664
feature/smarter-navigation       bc3c914
feature/custom-wake-word-ui      9d41caa
```

Base before the merges: `377358e`. Merge commits: `89fc2d4`, `0d2b0eb`,
`21faffd`, `71d7a61`, `fb850aa`, `79e3836`, `5fc9403`, plus the `verify`
commit appended after this file was written.

---

## 10. Change policy for this document

- Append, never overwrite.
- Each new session adds `## Session — YYYY-MM-DD` at the bottom with: what was
  asked, what changed, files, commits, test counts, and what is still open.
- Record failed attempts too; they are cheaper to read than to repeat.
- Keep secrets out. Environment variable names are fine; values are not.

---

## Session — 2026-11-22 (merge + real verification)

Asked: merge everything, explain/replace the fake parts with real checks, push,
keep one append-only Markdown record, then start the tool for a manual test.

Done:

- Merged all seven branches into `main` (`--no-ff`), resolved two content
  conflicts (`__main__.py`, `core.html`).
- Added `xenon/verify.py`, `xenon verify [--speak|--mic|--duck|--all]`, and
  `tests/test_verify.py`; added `profile_path()` to `field_assistant.py`.
- Full suite: **1126 passed, 1 skipped, 633 subtests**.
- Real read-only verifier on this machine: 7/7 passed or skipped.
- Updated `NEXT_STEPS.md` to point here and to show the true open items.
- Pushed `main`; launched the orb for manual testing.

---

## Session — 2026-11-22 (microphone and speaker settings)

Asked: which microphone is Xenon using right now, and add a setting to change
both the input and output device.

### What this machine was using

- **Input:** sounddevice's system default, device `#1`,
  `Microphone Array (Realtek(R) Au...` (the built-in Realtek array). It was not
  chosen by Xenon; it was simply the Windows default, because
  `MicrophoneSource(device=None)` lets sounddevice pick.
- **Output:** SAPI's system default, `ED340CUR J0 (NVIDIA High Definition
  Audio)`, which is the monitor's audio endpoint, not the Realtek speakers.
  That is why a spoken reply could be inaudible even though SAPI "worked".

### What was added (commit `adfe2dd`)

New `xenon/audio_settings.py`:

- `%APPDATA%\xenon\audio.json` stores `{"input": "...", "output": "..."}`.
  Friendly names are stored, not indices, because Windows renumbers devices.
- `resolve_input()` accepts an index or a case-insensitive name fragment.
- `current_input()` precedence: `XENON_MIC_DEVICE` → saved file → system default.
- `current_output()` precedence: `XENON_TTS_OUTPUT` → saved file → SAPI default.
- `default_input()` reports what sounddevice would pick, for display.
- `test()` speaks through the chosen output and records the chosen input,
  returning a peak level.

New `xenon/audio_settings_ui.py`: a small Tk window with microphone and speaker
dropdowns, a real Test button, and Save. Run with `xenon audio --settings`.

`xenon/audio.py`:

- `_sapi_output_tokens()` enumerates `SAPI.SpObjectTokenCategory` under
  `HKLM\SOFTWARE\Microsoft\Speech\AudioOutput`.
- `_apply_sapi_output()` sets `engine.AudioOutput` to the matching token.
- `list_outputs()` and `default_output()` expose the list and the current
  endpoint.
- `SapiSpeaker(output=...)` applies the saved output. Injected test engines do
  not read the settings file, so tests stay deterministic.

`xenon/__main__.py`:

- New `xenon audio` command:
  `--list`, `--input`, `--output`, `--test`, `--settings`.
- The voice loop now calls `audio_settings.current_input()` instead of reading
  only `XENON_MIC_DEVICE`.

`xenon/verify.py` reports the selected input and the SAPI output in its
microphone/SAPI lines.

### How to use it

```powershell
python -m xenon audio                      # show current input and output
python -m xenon audio --list               # every input + SAPI output
python -m xenon audio --input realtek      # name fragment or index
python -m xenon audio --output "Speakers (Realtek"
python -m xenon audio --test               # speak + record 0.8 s
python -m xenon audio --settings           # graphical picker
```

Changes take effect on the next orb/voice start; the settings window says so.
The current file is only written when a device is actually chosen, so a fresh
checkout keeps using Windows defaults.

### Tests

`tests/test_audio_settings.py` covers the file format, resolution, env
precedence and bad input. `tests/test_audio.py` gained SAPI output-token
selection tests. Full suite: **1138 passed, 1 skipped, 633 subtests**.

---

## Session — 2026-11-22 (device pickers in the music settings window)

Asked: "add an option here" while looking at the Music widget settings, Audio
tab. The separate `xenon audio --settings` window was not where they wanted it.

Added in commit `e6837bf` to `xenon/music_settings_ui.py`:

- The Audio tab now starts with a **Xenon voice** section containing
  **Microphone** and **Speaker** dropdowns.
- Input values are `index: friendly name` from sounddevice; output values are
  SAPI audio-output descriptions. Both include **System default**.
- Selecting either one writes `%APPDATA%\xenon\audio.json` immediately through
  `audio_settings.save()`; the status line says a restart is needed.
- The existing Sensitivity and Smoothing sliders moved under a **Visualizer**
  heading, with a corrected note: the visualizer still follows the default
  speaker through WASAPI loopback, it is not affected by the voice device
  pickers.
- A test instance of `SettingsWindow` was created and destroyed successfully;
  full suite remains **1138 passed, 1 skipped**.

Note for the next session: this window and the orb are separate processes. The
running orb must be restarted (or relaunched from the tray) before it reads a
new `audio.json`; the settings window itself always reads the file when it
opens.

---

## Session — 2026-11-22 (proximity / microphone light bar)

Asked: "add a proximity visualizer as well like when I talk there be a small
lightbar that lights up based on the audio inputs volume".

### What already existed

The voice loop already pushes a live microphone RMS into the orb at every
capture frame (`hooks.level(...)`, from both the standby loop and the speaking
watcher). `OrbModel.level` already drove the halo and the in-orb equaliser, but
there was no small, always-visible meter, and the folded notch showed nothing
about the microphone.

### What was added

`xenon/orb_model.py`:

- `OrbModel.meter` is the display level: it jumps up with `set_level()` but
  falls at 4.5/s in `step()`, so the bar goes dark soon after the last word
  while the halo keeps its slower 1.6/s decay.
- `OrbModel.mic_bar: bool = True` — `XENON_MIC_BAR=off` hides both bars.

`xenon/orb_render.py`:

- `OrbRenderer._mic_bar()` draws a faint hairline track and a bright fill that
  grows from the centre outwards with `model.meter`. Colour runs from the mic
  green `(74, 222, 128)` to cyan `(34, 211, 238)` as it gets louder, with a
  small blurred glow.
- In `_notch_content()`, when the voice is on and the state is
  `standby`/`listening`, the bar sits along the notch's bottom edge. It is
  suppressed while a task-progress bar occupies that line or a flash is up.
- `_draw_proximity_bar()` puts a separate short bar under the open orb; this
  covers the moment Xenon unfolds to listen, which is exactly when the user
  talks. It is hidden while the orb is shrunk or folded.

`xenon/orb.py`:

- The app reads `XENON_MIC_BAR` into `model.mic_bar`.
- A folded notch with an active meter redraws at 20 fps instead of 4, so the
  bar follows speech instead of stepping.

No test double was added: the meter is fed by the same real microphone frames
that drive wake-word detection. A silent mic shows only the faint track.

### Tests

`tests/test_orb_model.py`: meter attack/decay and the default-on flag.
`tests/test_orb.py`: notch bar lights with a level and respects `mic_bar=False`;
the orb's light bar follows the level and can be disabled. Both bars were also
rendered to images and inspected visually. Full suite: **1143 passed, 1 skipped,
633 subtests**.

### Manual check

Say something while Xenon is folded into the notch: the hairline under the
clock should fill cyan from the centre and fall back when you stop. Say
"Xenon" and keep talking: the short bar under the open orb should track your
voice. `XENON_MIC_BAR=off` removes both.

---

## Session — 2026-11-22 (why Xenon felt slow: mic loop CPU)

Asked: "why does xenon feel so slow restart it".

### Measurement, not a guess

The running orb (`python -m xenon orb`) was using about **105% of one core**
continuously (6.31 s of CPU over 6.03 s wall). `py-spy record -p <pid> -d 6`
put ~90% of samples in the `xenon-orb-voice` thread, with the hottest frames:

- `MicrophoneSource.read_frame.<locals>.<listcomp>` — converting 480 numpy
  floats to Python floats with a comprehension on every 30 ms frame.
- two separate `rms()` generators over the same frame: one for `hooks.level`
  (the light bar) and another inside `UtteranceDetector.feed()`.

Orb rendering was not the problem: a docked notch frame takes ~2 ms and a full
orb ~9 ms (2 ms was even measured while the light bar was active).

### What was changed

- `xenon/audio.py`: `MicrophoneSource.read_frame_with_level()` now computes the
  RMS with `numpy.dot` and converts samples with `ndarray.tolist()`, both in C.
  `read_frame()` delegates to it. A fallback keeps injected fake streams
  working.
- `UtteranceDetector.feed(frame, level=None)`: the caller can hand in an RMS it
  already computed instead of recomputing it.
- `xenon/__main__.py`: new `_read_frame_and_level()` helper; both the standby
  loop and the barge-in watcher read once and compute one level, then pass it to
  the orb and the endpointer. It also throttles a broken stream that returns
  instantly to roughly one frame interval, instead of spinning at thousands of
  reads a second.
- Test doubles in `tests/test_voice_hooks.py` accept the optional `level`
  argument.

A local microbenchmark of the old three Python passes vs the new numpy path:
**61.6 µs/frame → 6.4 µs/frame (9.6x)**.

### Tests

`tests/test_audio.py` covers the new fast path, its fake-stream fallback, and
`feed(..., level=...)`. Full suite: **1146 passed, 1 skipped, 633 subtests**.
After deploying, CPU should be re-measured on the restarted orb; if it is still
high, the next suspect is the music widget's own loops, not the voice loop.


