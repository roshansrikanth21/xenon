import os
import unittest
from unittest import mock

from xenon.ocr import VisionChain, WindowsOcr, local_ocr_enabled
from xenon.screen import CapturedFrame, WindowSnapshot
from xenon.vision import VisionUnavailable

SNAPSHOT = WindowSnapshot(app_name="OpenCode", title="Xenon - OpenCode", controls=())
FRAME = CapturedFrame(image=b"\x89PNG\r\n\x1a\npayload", width=800, height=600)


class FakeProvider:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0

    def describe(self, frame, snapshot) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.text


class WindowsOcrTests(unittest.TestCase):
    def test_returns_text_from_the_ocr_runner(self) -> None:
        seen: list[bytes] = []
        ocr = WindowsOcr(runner=lambda image: seen.append(image) or "  File  Edit  \n")

        text = ocr.describe(FRAME, SNAPSHOT)

        self.assertEqual(text, "File  Edit")
        self.assertEqual(seen, [FRAME.image])

    def test_requires_a_captured_frame(self) -> None:
        with self.assertRaises(VisionUnavailable):
            WindowsOcr(runner=lambda image: "text").describe(None, SNAPSHOT)

    def test_rejects_an_empty_frame(self) -> None:
        empty = CapturedFrame(image=b"", width=0, height=0)
        with self.assertRaises(VisionUnavailable):
            WindowsOcr(runner=lambda image: "text").describe(empty, SNAPSHOT)

    def test_reports_when_no_text_was_found(self) -> None:
        with self.assertRaises(VisionUnavailable) as raised:
            WindowsOcr(runner=lambda image: "   ").describe(FRAME, SNAPSHOT)

        self.assertIn("no readable text", str(raised.exception))

    def test_wraps_runner_failures(self) -> None:
        def runner(image: bytes) -> str:
            raise RuntimeError("engine missing")

        with self.assertRaises(VisionUnavailable) as raised:
            WindowsOcr(runner=runner).describe(FRAME, SNAPSHOT)

        self.assertIn("local OCR failed", str(raised.exception))
        self.assertIn("engine missing", str(raised.exception))


class VisionChainTests(unittest.TestCase):
    def test_returns_the_first_provider_with_text(self) -> None:
        first = FakeProvider(error=VisionUnavailable("no text"))
        second = FakeProvider("Slide 2: What is AI?")
        third = FakeProvider("should not be used")

        text = VisionChain([first, second, third]).describe(FRAME, SNAPSHOT)

        self.assertEqual(text, "Slide 2: What is AI?")
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)
        self.assertEqual(third.calls, 0)

    def test_skips_a_provider_that_returns_blank_text(self) -> None:
        blank = FakeProvider("   ")
        usable = FakeProvider("readable")

        text = VisionChain([blank, usable]).describe(FRAME, SNAPSHOT)

        self.assertEqual(text, "readable")

    def test_reports_every_reason_when_all_providers_fail(self) -> None:
        with self.assertRaises(VisionUnavailable) as raised:
            VisionChain(
                [
                    FakeProvider(error=VisionUnavailable("offline")),
                    FakeProvider(""),
                ]
            ).describe(FRAME, SNAPSHOT)

        message = str(raised.exception)
        self.assertIn("FakeProvider", message)
        self.assertIn("offline", message)

    def test_requires_at_least_one_provider(self) -> None:
        with self.assertRaises(VisionUnavailable):
            VisionChain([]).describe(FRAME, SNAPSHOT)


class OcrToggleTests(unittest.TestCase):
    def test_defaults_to_on(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XENON_OCR", None)
            self.assertTrue(local_ocr_enabled())

    def test_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {"XENON_OCR": "off"}, clear=False):
            self.assertFalse(local_ocr_enabled())


if __name__ == "__main__":
    unittest.main()
