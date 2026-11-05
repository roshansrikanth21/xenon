---
name: task-assist
description: Help the user move forward on whatever task is visible on the screen: summarize the goal, propose concrete next steps, and suggest the Xenon command that would do it.
---

# Task assistance

Use this when the user asks "help me with this", "what should I do next", or
"what is this and what do I do".

## Procedure
1. Read the screen context: window title, app, visible controls and text.
2. Infer the concrete task (edit a document, fix an error, reply to a message,
   etc.) without inventing contents.
3. Suggest the next 1-3 actions in priority order.
4. When an action maps to a Xenon capability, name the command
   (e.g. `python -m xenon ask "..."`, `python -m xenon do "..."`).
5. Ask one focused question only if a critical detail is missing.

## Rules
- Do not just describe the window; say what to do next.
- Cite on-screen evidence by window title.
- Cite memory with its path when it informs the answer.
- Never pretend an action ran. Offer the command and let the user approve.
