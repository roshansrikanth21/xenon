import unittest

from xenon.screen import (
    CapturedFrame,
    ScreenObserver,
    UiControl,
    WindowSnapshot,
)


class FakeUiAutomation:
    def __init__(self, snapshot: WindowSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def foreground_window(self) -> WindowSnapshot:
        self.calls += 1
        return self.snapshot


class FakeVision:
    def __init__(self, response: str = "vision context") -> None:
        self.response = response
        self.calls = 0
        self.frames: list[CapturedFrame | None] = []

    def describe(self, frame: CapturedFrame | None, snapshot: WindowSnapshot) -> str:
        self.calls += 1
        self.frames.append(frame)
        return self.response


class FailingVision:
    def describe(self, frame: CapturedFrame | None, snapshot: WindowSnapshot) -> str:
        raise RuntimeError("model offline")


class FakeCapture:
    def __init__(self, frame: CapturedFrame) -> None:
        self.frame = frame
        self.calls = 0

    def capture_foreground(self) -> CapturedFrame:
        self.calls += 1
        return self.frame


class ScreenObserverTests(unittest.TestCase):
    def test_uses_ui_automation_for_structured_foreground_context(self) -> None:
        automation = FakeUiAutomation(
            WindowSnapshot(
                app_name="Notepad",
                title="project-notes.md - Notepad",
                controls=(UiControl(role="Edit", name="editor", value="Plan the release"),),
            )
        )
        vision = FakeVision()

        context = ScreenObserver(automation, vision).observe()

        self.assertEqual(context.app_name, "Notepad")
        self.assertEqual(context.title, "project-notes.md - Notepad")
        self.assertEqual(context.text, "Plan the release")
        self.assertFalse(context.used_vision)
        self.assertEqual(vision.calls, 0)

    def test_redacts_sensitive_control_values_before_returning_context(self) -> None:
        automation = FakeUiAutomation(
            WindowSnapshot(
                app_name="Browser",
                title="Sign in",
                controls=(
                    UiControl(role="Edit", name="Email", value="roshan@example.com"),
                    UiControl(role="Edit", name="Password", value="secret-value", is_password=True),
                ),
            )
        )

        context = ScreenObserver(automation, FakeVision()).observe()

        self.assertIn("roshan@example.com", context.text)
        self.assertNotIn("secret-value", context.text)
        self.assertIn("[REDACTED]", context.text)

    def test_collapses_repeated_values_in_observed_text(self) -> None:
        automation = FakeUiAutomation(
            WindowSnapshot(
                app_name="Browser",
                title="Docs",
                controls=(
                    UiControl(role="Text", name="Speed Dial"),
                    UiControl(role="Text", name="Speed Dial"),
                    UiControl(role="Edit", name="Search", value="xenon"),
                ),
            )
        )

        context = ScreenObserver(automation, FakeVision()).observe()

        self.assertEqual(context.text.count("Speed Dial"), 1)
        self.assertIn("xenon", context.text)

    def test_caps_context_length_for_large_windows(self) -> None:
        automation = FakeUiAutomation(
            WindowSnapshot(
                app_name="Editor",
                title="huge.log",
                controls=tuple(
                    UiControl(role="Text", name=f"line {index}", value=f"{index}:" + "x" * 200)
                    for index in range(100)
                ),
            )
        )

        context = ScreenObserver(automation, FakeVision()).observe()

        self.assertLessEqual(len(context.text), ScreenObserver.max_context_chars)
        self.assertTrue(context.text.endswith("[truncated]"))

    def test_uses_vision_when_automation_only_sees_window_chrome(self) -> None:
        automation = FakeUiAutomation(
            WindowSnapshot(
                app_name="opera",
                title="deck.pptx - Opera",
                controls=(
                    UiControl(role="Document", name="Speed Dial"),
                    UiControl(role="Pane", name="deck.pptx - Opera"),
                    UiControl(role="Group", name="Browser client"),
                ),
            )
        )
        vision = FakeVision("Slide 2: What is AI?")

        context = ScreenObserver(automation, vision).observe()

        self.assertTrue(context.used_vision)
        self.assertEqual(context.text, "Slide 2: What is AI?")
        self.assertEqual(vision.calls, 1)

    def test_treats_editable_text_as_sufficient_without_vision(self) -> None:
        automation = FakeUiAutomation(
            WindowSnapshot(
                app_name="Notepad",
                title="todo.md - Notepad",
                controls=(UiControl(role="Edit", name="Text editor", value="buy milk"),),
            )
        )
        vision = FakeVision()

        context = ScreenObserver(automation, vision).observe()

        self.assertFalse(context.used_vision)
        self.assertEqual(vision.calls, 0)

    def test_passes_captured_frame_to_vision(self) -> None:
        frame = CapturedFrame(image=b"\x89PNG\r\n\x1a\nframe", width=640, height=480)
        capture = FakeCapture(frame)
        vision = FakeVision("a task diagram")
        automation = FakeUiAutomation(
            WindowSnapshot(app_name="CanvasApp", title="Untitled", controls=())
        )

        context = ScreenObserver(automation, vision, capture=capture).observe()

        self.assertEqual(capture.calls, 1)
        self.assertIs(vision.frames[0], frame)
        self.assertEqual(context.text, "a task diagram")

    def test_does_not_capture_when_uia_is_sufficient(self) -> None:
        frame = CapturedFrame(image=b"png", width=1, height=1)
        capture = FakeCapture(frame)
        automation = FakeUiAutomation(
            WindowSnapshot(
                app_name="Notepad",
                title="notes.md",
                controls=(UiControl(role="Edit", name="editor", value="draft"),),
            )
        )

        ScreenObserver(automation, FakeVision(), capture=capture).observe()

        self.assertEqual(capture.calls, 0)

    def test_reports_vision_failure_honestly(self) -> None:
        frame = CapturedFrame(image=b"png", width=1, height=1)
        automation = FakeUiAutomation(
            WindowSnapshot(app_name="CanvasApp", title="Untitled", controls=())
        )

        context = ScreenObserver(automation, FailingVision(), capture=FakeCapture(frame)).observe()

        self.assertFalse(context.used_vision)
        self.assertEqual(context.app_name, "CanvasApp")
        self.assertIn("no fallback could read", context.error)
        self.assertIn("model offline", context.error)

    def test_keeps_accessible_text_when_vision_fails(self) -> None:
        frame = CapturedFrame(image=b"png", width=1, height=1)
        automation = FakeUiAutomation(
            WindowSnapshot(
                app_name="opera",
                title="deck.pptx - Opera",
                controls=(
                    UiControl(role="Document", name="Speed Dial"),
                    UiControl(role="Pane", name="deck.pptx - Opera"),
                    UiControl(role="Group", name="Browser client"),
                ),
            )
        )

        context = ScreenObserver(automation, FailingVision(), capture=FakeCapture(frame)).observe()

        # The thin UIA trail is still useful context; it must not be replaced
        # by an error message.
        self.assertIn("deck.pptx - Opera", context.text)
        self.assertFalse(context.used_vision)
        self.assertIn("no fallback could read", context.error)

    def test_keeps_accessible_text_when_capture_fails(self) -> None:
        automation = FakeUiAutomation(
            WindowSnapshot(
                app_name="Editor",
                title="main.py - Code",
                controls=(UiControl(role="Pane", name="Explorer"),),
            )
        )

        class FailingCapture:
            def capture_foreground(self) -> CapturedFrame:
                raise RuntimeError("display asleep")

        context = ScreenObserver(
            automation, FakeVision(), capture=FailingCapture()
        ).observe()

        self.assertEqual(context.text, "Explorer")
        self.assertFalse(context.used_vision)
        self.assertIn("display asleep", context.error)
        self.assertIn("pixels", context.error)

    def test_empty_vision_description_is_not_treated_as_content(self) -> None:
        automation = FakeUiAutomation(
            WindowSnapshot(app_name="App", title="Window", controls=())
        )

        context = ScreenObserver(automation, FakeVision("   ")).observe()

        self.assertEqual(context.text, "")
        self.assertFalse(context.used_vision)
        self.assertIn("no description", context.error)

    def test_uses_vision_only_when_automation_has_no_readable_content(self) -> None:
        automation = FakeUiAutomation(
            WindowSnapshot(app_name="CanvasApp", title="Untitled", controls=())
        )
        vision = FakeVision("A whiteboard with a task flow diagram")

        context = ScreenObserver(automation, vision).observe()

        self.assertTrue(context.used_vision)
        self.assertEqual(context.text, "A whiteboard with a task flow diagram")
        self.assertEqual(vision.calls, 1)


class PrewarmTests(unittest.TestCase):
    def test_prewarm_delegates_to_the_adapter(self) -> None:
        automation = PrewarmableAutomation()

        ScreenObserver(automation, FakeVision()).prewarm()

        self.assertEqual(automation.prewarms, 1)

    def test_prewarm_is_optional_for_adapters_without_it(self) -> None:
        automation = FakeUiAutomation(WindowSnapshot(app_name="App", title="t", controls=()))

        ScreenObserver(automation, FakeVision()).prewarm()  # must not raise

    def test_a_failing_prewarm_does_not_break_observe(self) -> None:
        automation = PrewarmableAutomation(fail=True)
        observer = ScreenObserver(automation, FakeVision())

        observer.prewarm()
        context = observer.observe()

        self.assertEqual(context.title, "Editor")


class PrewarmableAutomation(FakeUiAutomation):
    def __init__(self, fail: bool = False) -> None:
        super().__init__(
            WindowSnapshot(
                app_name="App",
                title="Editor",
                controls=(
                    UiControl(role="text", name="x" * 200),
                ),
            )
        )
        self.prewarms = 0
        self.fail = fail

    def prewarm(self) -> None:
        self.prewarms += 1
        if self.fail:
            raise RuntimeError("COM unavailable")


if __name__ == "__main__":
    unittest.main()
