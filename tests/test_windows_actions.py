import sys
import unittest

from xenon.actions import Action
from xenon.windows_actions import (
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


class BackendSafetyTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows actuator is Windows-only")
    def test_unsupported_kinds_fail_without_touching_the_desktop(self) -> None:
        backend = WindowsActionBackend()

        result = backend.execute(Action("teleport"))

        self.assertFalse(result.ok)
        self.assertIn("unsupported", result.detail)


if __name__ == "__main__":
    unittest.main()
