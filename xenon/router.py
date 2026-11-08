"""Understand a request with the model and turn it into a Xenon command.

The deterministic parsers (quick, brightness, music, fastpath) are instant
but only know the phrasings they were written for. When none of them match,
the model reads the request the way a person would and rewrites it as one of
Xenon's canonical commands, which the parsers then execute. "my eyes are
dying, tone the screen down" becomes "brightness down"; "it's way too loud"
becomes "volume down"; a real question becomes nothing and is answered
normally.

The model never runs anything itself: its output is only accepted when a
deterministic parser recognises it, so it cannot invent actions.
"""
from __future__ import annotations

import json
import re
from typing import Callable

ROUTER_PROMPT = """You turn a user's request to a desktop voice assistant into ONE command it can run.

Commands it can run (use these exact shapes):
- brightness down | brightness up | brightness <0-100> | dim my monitor | max brightness | restore brightness
- volume up | volume down | mute
- pause | resume | next song | previous song | what's playing
- play my liked songs | play <song or artist> | play my <name> playlist | play some <genre or mood>
- open <app> | switch to <window or app> | search for <query>
- set a timer for <duration> | remind me in <duration> to <thing> | cancel the timer | how long is left
- what time is it | what's the date

Rules:
- Understand intent, slang, typos and indirect requests ("my eyes hurt, screen's too bright" -> "brightness down"; "way too loud" -> "volume down"; "something chill" -> "play some chill"; "10 min break" -> "set a timer for 10 minutes").
- "dim it a little", "a bit darker" -> "brightness down"; "as dark as it goes" -> "dim my monitor".
- "turn it up/down" or "a notch" with no screen word means the sound: "volume up" / "volume down".
- Questions about the music are commands: "who sings this", "what song is this" -> "what's playing".
- Questions about timers are commands: "time left on my timer" -> "how long is left".
- Questions about the clock are commands: "got the time?" -> "what time is it".
- If it is a question, a chat message, a request about what is on screen, or anything not in the list, the command is null.
- Reply with JSON only: {"command": "<command>"} or {"command": null}"""

_LONG_FORM = re.compile(
    r"^(?:explain|why|summari[sz]e|describe|tell me about|write|draft|translate|compare|"
    r"teach me|help me understand|what does|what do|how does|how do i|how can i)\b"
)


def worth_routing(text: str) -> bool:
    """Skip only requests that are obviously long-form questions or writing.

    Everything else goes to the model, because people phrase commands in ways
    no keyword list anticipates ("bring up chrome", "who sings this").
    """
    phrase = " ".join(text.lower().split())
    if not phrase or len(phrase.split()) > 30:
        return False
    return not _LONG_FORM.match(phrase)


def _extract(reply: str) -> str | None:
    match = re.search(r"\{.*\}", reply, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    command = data.get("command") if isinstance(data, dict) else None
    if not isinstance(command, str):
        return None
    command = " ".join(command.split()).strip(" .")
    return command or None


def route(text: str, complete: Callable[[list[dict]], str],
          recognised: Callable[[str], bool]) -> str | None:
    """Return a canonical command for ``text``, or None to answer normally.

    ``complete`` is the chat model; ``recognised`` says whether a parser can
    run the command, so the model cannot make up actions.
    """
    if not worth_routing(text):
        return None
    try:
        reply = complete(
            [
                {"role": "system", "content": ROUTER_PROMPT},
                {"role": "user", "content": text.strip()},
            ]
        )
    except Exception:
        return None
    command = _extract(reply)
    if command and recognised(command):
        return command
    return None
