"""Turn other apps' audio down while Xenon listens or talks, like a speaker does.

Uses Windows' per-app audio sessions (Core Audio through pycaw), so only
other programs get quieter: Spotify, the browser, games. Xenon's own process
keeps its volume, so its spoken reply stays clear. Each app's previous level
is remembered and put back exactly.
"""
from __future__ import annotations

import os
import threading

DUCK_TO = 0.15  # fraction of each app's own level while ducked


class Ducker:
    def __init__(self, sessions_factory=None, own_pid: int | None = None) -> None:
        self._sessions_factory = sessions_factory or _sessions
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._saved: dict[object, float] = {}
        self._lock = threading.Lock()

    @property
    def ducked(self) -> bool:
        return bool(self._saved)

    def duck(self, to: float = DUCK_TO) -> int:
        """Lower every other app's audio; returns how many were lowered."""
        with self._lock:
            if self._saved:
                return len(self._saved)
            count = 0
            for session in self._sessions_factory():
                try:
                    process = session.Process
                    if process is None or process.pid == self._own_pid:
                        continue  # system sounds and Xenon itself stay put
                    volume = session.SimpleAudioVolume
                    level = float(volume.GetMasterVolume())
                    if level <= 0.01:
                        continue
                    self._saved[session.InstanceIdentifier or id(session)] = level
                    volume.SetMasterVolume(level * to, None)
                    count += 1
                except Exception:
                    continue
            return count

    def restore(self) -> None:
        with self._lock:
            if not self._saved:
                return
            saved, self._saved = self._saved, {}
            for session in self._sessions_factory():
                try:
                    key = session.InstanceIdentifier or id(session)
                    if key in saved:
                        session.SimpleAudioVolume.SetMasterVolume(saved[key], None)
                except Exception:
                    continue


def _sessions():
    """Current audio sessions; COM is initialised for whichever thread asks."""
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        pass
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetAllSessions()


class NullDucker:
    """Used when pycaw is missing: does nothing, never fails."""

    ducked = False

    def duck(self, to: float = DUCK_TO) -> int:
        return 0

    def restore(self) -> None:
        pass


def make_ducker():
    if os.environ.get("XENON_DUCK", "on").strip().lower() in {"0", "off", "false", "no"}:
        return NullDucker()
    try:
        import pycaw  # noqa: F401

        return Ducker()
    except Exception:
        return NullDucker()
