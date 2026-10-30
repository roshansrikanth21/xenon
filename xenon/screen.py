"""Screen context contracts and a UI Automation-first observer.

The Windows adapter will implement ``foreground_window`` later. Keeping the
observer adapter-driven makes it testable without a live desktop and prevents
vision calls when accessible text already answers the request.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class UiControl:
    role: str
    name: str
    value: str = ""
    is_password: bool = False


@dataclass(frozen=True)
class WindowSnapshot:
    app_name: str
    title: str
    controls: tuple[UiControl, ...]


@dataclass(frozen=True)
class ScreenContext:
    app_name: str
    title: str
    text: str
    used_vision: bool


class UiAutomationProvider(Protocol):
    def foreground_window(self) -> WindowSnapshot: ...


class VisionProvider(Protocol):
    def describe(self, snapshot: WindowSnapshot) -> str: ...


class ScreenObserver:
    def __init__(self, automation: UiAutomationProvider, vision: VisionProvider) -> None:
        self._automation = automation
        self._vision = vision

    def observe(self) -> ScreenContext:
        snapshot = self._automation.foreground_window()
        content = self._readable_text(snapshot.controls)
        if content:
            return ScreenContext(
                app_name=snapshot.app_name,
                title=snapshot.title,
                text=content,
                used_vision=False,
            )
        return ScreenContext(
            app_name=snapshot.app_name,
            title=snapshot.title,
            text=self._vision.describe(snapshot),
            used_vision=True,
        )

    @staticmethod
    def _readable_text(controls: tuple[UiControl, ...]) -> str:
        values = []
        for control in controls:
            if control.is_password:
                values.append(f"{control.name}: [REDACTED]")
            elif control.value.strip():
                values.append(control.value.strip())
            elif control.name.strip():
                values.append(control.name.strip())
        return "\n".join(values)
