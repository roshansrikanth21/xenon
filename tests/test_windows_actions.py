import sys
import unittest

from xenon.actions import Action
from xenon.windows_actions import (
    DesktopTitleVerifier,
    ForegroundTitleVerifier,
    WindowsActionBackend,
    parse_keys,
    virtual_key,
)


class FakeBackend:
    def __init__(self, title: str = "notes.md - Notepad") -> None:
        self._title = title

    def foreground_handle(self) -> int:
        return 1

    def title(self, handle: int) -> str:
        return self._title


class KeyParsingTests(unittest.TestCase):
    def test_parses_modifiers_and_letter(self) -> None:
        modifiers, key = parse_keys("ctrl+shift+s")

        self.assertEqual(modifiers, [0x11, 0x10])
        self.assertEqual(key, ord("S"))

    def test_parses_named_keys(self) -> None:
        self.assertEqual(virtual_key("enter"), 0x0D)
        self.assertEqual(virtual_key("Escape"), 0x1B)
        self.assertEqual(virtual_key("f5"), 0x74)

    def test_rejects_unknown_keys(self) -> None:
        with self.assertRaises(ValueError):
            virtual_key("hyperkey")
        with self.assertRaises(ValueError):
            parse_keys("ctrl+hyperkey")

    def test_rejects_an_empty_sequence(self) -> None:
        with self.assertRaises(ValueError):
            parse_keys("  ")


class VerifierTests(unittest.TestCase):
    def test_accepts_a_matching_title(self) -> None:
        verifier = ForegroundTitleVerifier("notepad", backend=FakeBackend("notes.md - Notepad"))

        self.assertTrue(verifier.verify("open notepad").ok)

    def test_rejects_a_mismatched_title(self) -> None:
        verifier = ForegroundTitleVerifier("excel", backend=FakeBackend("notes.md - Notepad"))

        result = verifier.verify("open excel")

        self.assertFalse(result.ok)
        self.assertIn("excel", result.detail)


class WindowText:
    def __init__(self, title: str) -> None:
        self._title = title

    def window_text(self) -> str:
        return self._title


class WindowListDesktop:
    def __init__(self, titles: list[str]) -> None:
        self._titles = titles

    def windows(self) -> list[WindowText]:
        return [WindowText(title) for title in self._titles]


class DesktopTitleVerifierTests(unittest.TestCase):
    def test_accepts_a_window_that_is_not_in_the_foreground(self) -> None:
        desktop = WindowListDesktop(["scratch - File Explorer", "notes.md - Notepad"])
        verifier = DesktopTitleVerifier("Notepad", desktop_factory=lambda: desktop)

        result = verifier.verify("open notepad")

        self.assertTrue(result.ok)
        self.assertIn("Notepad", result.detail)

    def test_rejects_a_missing_window(self) -> None:
        desktop = WindowListDesktop(["scratch - File Explorer"])
        verifier = DesktopTitleVerifier("Notepad", desktop_factory=lambda: desktop)

        result = verifier.verify("open notepad")

        self.assertFalse(result.ok)
        self.assertIn("Notepad", result.detail)


class BackendSafetyTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows actuator is Windows-only")
    def test_unsupported_kinds_fail_without_touching_the_desktop(self) -> None:
        backend = WindowsActionBackend()

        result = backend.execute(Action("teleport"))

        self.assertFalse(result.ok)
        self.assertIn("unsupported", result.detail)


class FakeWindow:
    def __init__(self, desktop, title: str) -> None:
        self._desktop = desktop
        self._title = title

    def set_focus(self) -> None:
        if self._desktop.fail_focus:
            raise RuntimeError("window is gone")
        self._desktop.foreground = self._title


class FakeDesktop:
    def __init__(self, windows: dict[str, str], fail_focus: bool = False) -> None:
        self.windows = windows
        self.fail_focus = fail_focus
        self.foreground = ""

    def window(self, title: str = "", title_re: str = "", handle: int = 0):
        if title:
            if title not in self.windows:
                raise RuntimeError("no exact match")
            return FakeWindow(self, self.windows[title])
        if title_re:
            import re as regex

            for name in self.windows.values():
                if regex.fullmatch(title_re, name):
                    return FakeWindow(self, name)
            raise RuntimeError("no fuzzy match")
        raise RuntimeError("handle lookups are not used in these tests")


class TitleBackend:
    """Reports the window a FakeDesktop focused, as Win32 would."""

    def __init__(self, desktop: FakeDesktop) -> None:
        self._desktop = desktop

    def foreground_handle(self) -> int:
        return 1

    def title(self, handle: int) -> str:
        return self._desktop.foreground


class FocusSafetyTests(unittest.TestCase):
    def backend(self, desktop, title_backend) -> WindowsActionBackend:
        return WindowsActionBackend(
            backend=title_backend, desktop_factory=lambda: desktop
        )

    def test_exact_title_is_preferred_over_a_longer_one(self) -> None:
        desktop = FakeDesktop(
            {
                "Notes - Notepad": "Notes - Notepad",
                "Notes - Notepad (read only)": "Notes - Notepad (read only)",
            }
        )
        backend = self.backend(desktop, TitleBackend(desktop))

        result = backend.execute(Action("focus_window", target="Notes - Notepad"))

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(desktop.foreground, "Notes - Notepad")

    def test_focus_that_lands_on_the_wrong_window_fails(self) -> None:
        class StubbornDesktop(FakeDesktop):
            def window(self, title: str = "", title_re: str = "", handle: int = 0):
                window = super().window(title=title, title_re=title_re, handle=handle)
                window.set_focus = lambda: None  # focus silently does nothing
                return window

        stale = StubbornDesktop({"Editor": "Editor"})
        stale.foreground = "Something Else"
        backend = self.backend(stale, TitleBackend(stale))

        result = backend.execute(Action("focus_window", target="Editor"))

        self.assertFalse(result.ok)
        self.assertIn("focus landed on", result.detail)

    def test_missing_window_is_reported(self) -> None:
        desktop = FakeDesktop({})
        backend = self.backend(desktop, TitleBackend(desktop))

        result = backend.execute(Action("focus_window", target="Nope"))

        self.assertFalse(result.ok)
        self.assertIn("no window matching", result.detail)

    def test_typing_is_refused_when_the_foreground_window_changed(self) -> None:
        desktop = FakeDesktop({"Notes - Notepad": "Notes - Notepad"})
        title_backend = TitleBackend(desktop)
        backend = self.backend(desktop, title_backend)
        typed: list[str] = []
        backend._send_unicode = typed.append

        backend.execute(Action("focus_window", target="Notes - Notepad"))
        desktop.foreground = "Your Bank - Opera"

        result = backend.execute(Action("type_text", value="hello"))

        self.assertFalse(result.ok)
        self.assertEqual(typed, [])
        self.assertIn("foreground window changed", result.detail)

    def test_typing_proceeds_when_the_window_is_still_focused(self) -> None:
        desktop = FakeDesktop({"Notes - Notepad": "Notes - Notepad"})
        backend = self.backend(desktop, TitleBackend(desktop))
        typed: list[str] = []
        backend._send_unicode = typed.append

        backend.execute(Action("focus_window", target="Notes - Notepad"))
        result = backend.execute(Action("type_text", value="hello"))

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(typed, ["hello"])

    def test_typing_without_a_focus_step_is_still_allowed(self) -> None:
        desktop = FakeDesktop({})
        backend = self.backend(desktop, TitleBackend(desktop))
        typed: list[str] = []
        backend._send_unicode = typed.append

        result = backend.execute(Action("type_text", value="hi"))

        self.assertTrue(result.ok)
        self.assertEqual(typed, ["hi"])


if __name__ == "__main__":
    unittest.main()
