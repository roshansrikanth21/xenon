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
- [x] **Voice-loop CPU fix.** One mic read, one numpy RMS per frame, plus a
      guard against broken streams spinning hot.
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

## Xenon Holo (hand-gesture holograms, `/holo`)

Done: four modes (Xenon core, XR-7 reactor, Periodic Lab, Shards), the
Holo-Explorer gesture set, throw, cues, hand rings, occlusion, Bohr model,
ionic crystals, camera retry, slow-quality offer. Tested on real hand photos
through the real MediaPipe tracker. A 70-agent audit (2026-11-15) confirmed 53
open items. The top ones:

- [ ] **Security: no-key polling.** Holo opened without a key polls `/api/status`
      with an empty key and trips the global auth limiter for every client.
- [ ] **Stable hand identity.** `hands.js` keys state by MediaPipe array index, so
      when one hand leaves, the other inherits its pinch or fist and a held
      object drops. Key by handedness, as the original does.
- [ ] **Per-hand pinch target.** `main.js` shares one `pinchTarget`/`dragging`,
      so a second hand can steal a drag.
- [ ] **Table drops 1.25 units** every time it is shown again (`table.js`).
      There is a partial fix in `git stash` ("holo-fix-audit partial").
- [ ] **Palm flip:** add a neutral band, a 100 ms hold and a 0.8 s quick-flip
      window, and ignore a hand that has just appeared.
- [ ] **Twist** also rotates the reactor you are holding. Explode should follow
      the hands continuously (fist accordion, twist dial).
- [ ] **Shards:** fist-move is 2.6x too fast (scaled parent). Cards are
      double-escaped. The live refresh overwrites other cards.
- [ ] **Shell:** settings don't persist; dim is a toggle, not a slider; changing
      quality doesn't reopen the camera; restartTracking can die silently;
      inference runs even without a new video frame.
- [ ] **Parity extras:** reactor stats panel, slot tether and snap flash,
      two-hand grip move/rotate, forearm/elbow panel (pose tracking).
- [ ] **Live webcam session** by the user, including the palm-up flip.

The full list with evidence is in the audit output. The fix workflow script
`holo-fix-audit` groups the items by file. Rerun it to apply them.
