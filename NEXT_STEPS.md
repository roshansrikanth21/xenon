# Xenon - development checklist

**Full history, decisions, branch index and the manual test plan live in
[`DESIGN.md`](DESIGN.md). That file is append-only: never overwrite it.**

## The original checklist

- [x] **P0 - Speech-driven system audio ducking (revised).** While the user is
      talking, lower or pause whatever other apps are playing (music, browser,
      games); track the whole utterance from first word to last, then restore
      every app's exact previous level. TTS barge-in ducking included.
- [x] **P1 - Smart form-field suggestions.** Click a text field, read what it
      is, suggest 2-3 non-sensitive variations in a pop-up. Never auto-fill;
      clicking a suggestion fills it. Secrets return nothing.
- [x] **1 - 3D Core page.** WebGL polish: load progress, click/fullscreen,
      state-driven glow and motion.
- [ ] **2 - Hand control / Holo.** Gesture engine and media pause/resume are
      implemented; **real-hand webcam test still needed (requires the user).**
- [x] **3 - Voice quality.** Whisper model/latency controls, SAPI voice
      selection, background warm-up, push-to-talk fallbacks.
- [x] **4 - Orb visuals.** Notch fold/unfold, idle "alive" breathing,
      ultrawide placement.
- [x] **5 - Music widget polish.** Lyrics/follow-the-singing were already
      present; visible-by-default overlay added. Not re-verified live.
- [x] **6 - Smarter navigation + gestures.** `xenon goto` known folders by
      voice; gesture pause/resume was already wired in Holo. Not re-verified
      live.
- [x] **7 - Custom wake word + UI rename (lowest priority).** `XENON_NAME=Rose`
      renames wake word, orb, notch, tray and 3D core.

## Added while finishing the work

- [x] **Merge all seven feature branches into `main`** and run the combined
      suite (1126 passed, 1 skipped).
- [x] **Real-hardware verifier.** `xenon verify [--speak|--mic|--duck|--all]`
      exercises the actual SAPI, microphone, pycaw and UI Automation.
- [x] **Microphone light bar.** A proximity bar in the folded notch and under
      the open orb follows live mic volume (`XENON_MIC_BAR=off` disables it).
- [x] **Audio device settings.** `xenon audio` shows and changes the microphone
      and SAPI speaker, persists them in `%APPDATA%\xenon\audio.json`, and
      `xenon audio --settings` opens a picker with a real test. The same two
      pickers are in the Music widget settings, Audio tab.
- [ ] **Packaging.** No `xenon` entry point, installer/exe, or voice autostart
      yet.
- [ ] **Noisy-room VAD / mic-in-the-loop latency.** Energy endpointer still
      unproven outside a quiet room.
- [ ] **Docs refresh.** `README.md` still says there is no tray icon or global
      hotkey; `TODO.md` still lists the shared UIA fixture layer.
- [ ] **Rotate the Groq key** that was pasted into chat earlier.
