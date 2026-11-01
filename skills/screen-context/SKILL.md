---
name: screen-context
description: Inspect the user's active window, read its UI Automation tree, and decide whether a screenshot is genuinely needed.
---

# Screen context

Use this when the user asks what is on their screen, or when a request depends on
the active app or document.

## Procedure

1. Run `python -m xenon screen` and read `app`, `title`, `source`, and the context text.
2. If `source` is `ui-automation`, answer from that text. Do not request a screenshot.
3. If `source` is `vision` and the result is empty, report the capture warning instead
   of inventing screen contents.
4. For a task that changes the screen, capture the before-state first so the result
   can be verified.

## Rules

- Never capture or transcribe password fields; they are redacted by the observer.
- Prefer the window title as a citation when answering.
- Do not claim to see content that the context does not contain.
