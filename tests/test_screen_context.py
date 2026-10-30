import unittest

from xenon.screen import ScreenObserver, UiControl, WindowSnapshot


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

    def describe(self, snapshot: WindowSnapshot) -> str:
        self.calls += 1
        return self.response


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

    def test_uses_vision_only_when_automation_has_no_readable_content(self) -> None:
        automation = FakeUiAutomation(
            WindowSnapshot(app_name="CanvasApp", title="Untitled", controls=())
        )
        vision = FakeVision("A whiteboard with a task flow diagram")

        context = ScreenObserver(automation, vision).observe()

        self.assertTrue(context.used_vision)
        self.assertEqual(context.text, "A whiteboard with a task flow diagram")
        self.assertEqual(vision.calls, 1)


if __name__ == "__main__":
    unittest.main()
