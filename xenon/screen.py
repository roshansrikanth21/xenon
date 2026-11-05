"""Screen context contracts and a UI Automation-first observer.

The Windows adapter implements ``foreground_window`` and ``capture_foreground``.
Keeping the observer adapter-driven makes it testable without a live desktop
and prevents vision calls when accessible text already answers the request.
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
class CapturedFrame:
    image: bytes
    width: int
    height: int


@dataclass(frozen=True)
class ScreenContext:
    app_name: str
    title: str
    text: str
    used_vision: bool
    error: str = ""


class UiAutomationProvider(Protocol):
    def foreground_window(self) -> WindowSnapshot: ...


class ScreenCapture(Protocol):
    def capture_foreground(self) -> CapturedFrame: ...


class VisionProvider(Protocol):
    def describe(
        self, frame: CapturedFrame | None, snapshot: WindowSnapshot
    ) -> str: ...


class ScreenObserver:
    max_context_chars = 6000
    min_content_chars = 40
    truncation_marker = "\n[truncated]"
    chrome_roles = frozenset({"pane", "group", "window", "titlebar", "toolbar"})
    content_name_roles = frozenset({"text", "listitem", "cell", "hyperlink"})

    def __init__(
        self,
        automation: UiAutomationProvider,
        vision: VisionProvider,
        capture: ScreenCapture | None = None,
    ) -> None:
        self._automation = automation
        self._vision = vision
        self._capture = capture

    def prewarm(self) -> None:
        """Best-effort warm-up of the adapter, so the first request is not slow.

        Cold pywinauto/COM initialization costs a few hundred milliseconds. A
        long-running service calls this at startup; a wrong or missing prewarm
        must never prevent observation.
        """
        warm = getattr(self._automation, "prewarm", None)
        if warm is None:
            return
        try:
            warm()
        except Exception:
            pass

    def observe(self) -> ScreenContext:
        snapshot = self._automation.foreground_window()
        content = self._readable_text(snapshot.controls)
        if self._is_sufficient(snapshot.controls, content):
            return ScreenContext(
                app_name=snapshot.app_name,
                title=snapshot.title,
                text=content,
                used_vision=False,
            )

        # Keep whatever the accessible tree did expose as a weak fallback. A
        # browser or Electron window often has useful landmarks even when it
        # has no readable page body; throwing that away leaves the assistant
        # with nothing but an error to work from.
        frame = None
        if self._capture is not None:
            try:
                frame = self._capture.capture_foreground()
            except Exception as error:
                return ScreenContext(
                    app_name=snapshot.app_name,
                    title=snapshot.title,
                    text=content,
                    used_vision=False,
                    error=(
                        "could not read the screen pixels "
                        f"({type(error).__name__}: {error}); only window "
                        "identity and limited accessible text are available"
                    ),
                )

        try:
            description = self._vision.describe(frame, snapshot)
        except Exception as error:
            return ScreenContext(
                app_name=snapshot.app_name,
                title=snapshot.title,
                text=content,
                used_vision=False,
                error=(
                    "no fallback could read the screen "
                    f"({error}); only window identity and limited accessible "
                    "text are available"
                ),
            )

        # Some providers explain *why* they could not read the image instead
        # of returning a description. Do not pass that off as screen content.
        if not description.strip():
            return ScreenContext(
                app_name=snapshot.app_name,
                title=snapshot.title,
                text=content,
                used_vision=False,
                error=(
                    "the fallback returned no description; only window "
                    "identity and limited accessible text are available"
                ),
            )

        return ScreenContext(
            app_name=snapshot.app_name,
            title=snapshot.title,
            text=description,
            used_vision=True,
        )

    @classmethod
    def _is_sufficient(cls, controls: tuple[UiControl, ...], content: str) -> bool:
        """Decide whether UIA text can stand in for a screenshot.

        Window chrome (panes, groups, toolbars) always exists, so its presence
        is not evidence that the accessible tree describes the page. Require
        either real editable content or enough content-bearing text.
        """
        if not content:
            return False
        text_chars = 0
        for control in controls:
            role = control.role.lower()
            if role == "edit" and control.value.strip() and not control.is_password:
                return True
            if role in cls.chrome_roles:
                continue
            text_chars += len(control.value.strip())
            if role in cls.content_name_roles:
                text_chars += len(control.name.strip())
        return text_chars >= cls.min_content_chars

    @classmethod
    def _readable_text(cls, controls: tuple[UiControl, ...]) -> str:
        values: list[str] = []
        seen: set[str] = set()
        for control in controls:
            if control.is_password:
                line = f"{control.name}: [REDACTED]"
            else:
                line = control.value.strip() or control.name.strip()
            if not line or line in seen:
                continue
            seen.add(line)
            values.append(line)
        return cls._cap("\n".join(values))

    @classmethod
    def _cap(cls, text: str) -> str:
        if len(text) <= cls.max_context_chars:
            return text
        room = cls.max_context_chars - len(cls.truncation_marker)
        return text[:room] + cls.truncation_marker
