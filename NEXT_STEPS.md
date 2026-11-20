# Xenon — development checklist

Worked one at a time: finish, show, get approval, commit, then next.

- [x] **P0 — Speech-driven system audio ducking (revised).** Not just Xenon's
      own voice: while the *user* is talking, lower or pause whatever other
      apps are playing (music, browser, games). Track the whole utterance from
      first word to last, then restore every app's exact previous level after
      the user finishes the command. Keep the TTS barge-in ducking too.
- [ ] **P1 — Smart form-field suggestions.** When the user clicks a text field
      (job application, bio, email, resume upload), read what the field is and
      suggest 2-3 non-sensitive variations in a pop-up. Never auto-fill;
      clicking a suggestion fills it. Process the field's context in real time.
- [ ] **1 — 3D Core page.** WebGL polish: load progress, click/fullscreen,
      state-driven glow and motion.
- [ ] **2 — Hand control / Holo.** Webcam gesture layer, real-hand test.
- [ ] **3 — Voice quality.** Whisper model/latency, SAPI voice, reliable
      push-to-talk.
- [ ] **4 — Orb visuals.** Notch fold/unfold, idle "alive" breathing,
      ultrawide placement.
- [x] **5 — Music widget polish.** Lyrics sync, follow-the-singing,
      visible-by-default overlay.
- [ ] **6 — Smarter navigation + gestures.** Navigate the computer on voice
      command; understand pause/resume music from hand gestures.
- [ ] **7 — Custom wake word + UI rename (lowest priority).** Change the wake
      word (e.g. "Rose") and have the whole UI/branding reflect it.
