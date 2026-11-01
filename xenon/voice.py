"""Voice loop orchestration, independent of any particular STT or TTS engine.

Wire ``transcribe`` to a VAD + speech-to-text source and ``speak`` to a TTS
engine; the loop enforces the wake gate and mutes detection while it is
processing or speaking so its own audio cannot retrigger it.
"""
from __future__ import annotations

from typing import Callable

from xenon.wake import WakeGate

Transcriber = Callable[[], str | None]
Speaker = Callable[[str], None]
CommandHandler = Callable[[str], str]


class VoiceLoop:
    def __init__(
        self,
        gate: WakeGate,
        transcribe: Transcriber,
        speak: Speaker,
        on_command: CommandHandler,
    ) -> None:
        self._gate = gate
        self._transcribe = transcribe
        self._speak = speak
        self._on_command = on_command

    def tick(self) -> str | None:
        """Poll one transcript; return the spoken reply when a command ran."""
        transcript = self._transcribe()
        if not transcript:
            return None

        command = self._gate.feed(transcript)
        if command is None:
            return None

        self._gate.set_speaking(True)
        try:
            reply = self._on_command(command)
            self._speak(reply)
            return reply
        except Exception:
            return None
        finally:
            self._gate.set_speaking(False)
