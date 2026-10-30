import io
import sys
import unittest

from xenon.screen import UiControl
from xenon.windows import (
    CapturedFrame,
    NullControlReader,
    Win32Backend,
    WindowsUiAutomation,
)


class FakeBackend:
    def __init__(
        self,
        handle: int = 42,
        title: str = "notes.md - Notepad",
        pid: int = 4242,
        process: str = "notepad",
        rect: tuple[int, int, int, int] | None = (0, 0, 800, 600),
        process_error: Exception | None = None,
    ) -> None:
        self.handle = handle
        self._title = title
        self.pid = pid
        self.process = process
        self.rect = rect
        self.process_error = process_error

    def foreground_handle(self) -> int:
        return self.handle

    def title(self, handle: int) -> str:
        return self._title

    def process_id(self, handle: int) -> int:
        return self.pid

    def process_name(self, pid: int) -> str:
        if self.process_error is not None:
            raise self.process_error
        return self.process

    def window_rect(self, handle: int) -> tuple[int, int, int, int] | None:
        return self.rect


class FakeControlReader:
    def __init__(
        self,
        controls: tuple[UiControl, ...] = (UiControl(role="Edit", name="editor", value="hello"),),
        error: Exception | None = None,
    ) -> None:
        self.controls = controls
        self.error = error
        self.calls: list[tuple[int, int]] = []

    def read(self, handle: int, limit: int) -> tuple[UiControl, ...]:
        self.calls.append((handle, limit))
        if self.error is not None:
            raise self.error
        return self.controls


class FakeGrabber:
    def __init__(self, size: tuple[int, int] = (800, 600)) -> None:
        self.size = size
        self.regions: list[tuple[int, int, int, int] | None] = []

    def grab(self, region: tuple[int, int, int, int] | None):
        from PIL import Image

        self.regions.append(region)
        return Image.new("RGB", self.size, "white")


class WindowsAdapterTests(unittest.TestCase):
    def test_builds_snapshot_from_foreground_window(self) -> None:
        reader = FakeControlReader()

        snapshot = WindowsUiAutomation(FakeBackend(), reader, max_controls=150).foreground_window()

        self.assertEqual(snapshot.app_name, "notepad")
        self.assertEqual(snapshot.title, "notes.md - Notepad")
        self.assertEqual(snapshot.controls, reader.controls)
        self.assertEqual(reader.calls, [(42, 150)])

    def test_falls_back_to_pid_when_process_name_is_denied(self) -> None:
        automation = WindowsUiAutomation(
            FakeBackend(process_error=OSError("access denied")),
            NullControlReader(),
        )

        snapshot = automation.foreground_window()

        self.assertEqual(snapshot.app_name, "pid-4242")
        self.assertEqual(snapshot.title, "notes.md - Notepad")
        self.assertEqual(snapshot.controls, ())

    def test_uia_failure_degrades_to_title_only_context(self) -> None:
        automation = WindowsUiAutomation(
            FakeBackend(),
            FakeControlReader(error=RuntimeError("uia unavailable")),
        )

        snapshot = automation.foreground_window()

        self.assertEqual(snapshot.title, "notes.md - Notepad")
        self.assertEqual(snapshot.controls, ())

    def test_capture_uses_window_rect_and_returns_png(self) -> None:
        grabber = FakeGrabber()
        automation = WindowsUiAutomation(FakeBackend(), NullControlReader())

        frame = automation.capture_foreground(grabber)

        self.assertIsInstance(frame, CapturedFrame)
        self.assertEqual((frame.width, frame.height), (800, 600))
        self.assertTrue(frame.image.startswith(b"\x89PNG"))
        self.assertEqual(grabber.regions, [(0, 0, 800, 600)])
        self.assertGreater(len(frame.image), 0)

    def test_capture_falls_back_to_full_screen_without_a_rect(self) -> None:
        grabber = FakeGrabber()
        automation = WindowsUiAutomation(FakeBackend(rect=None), NullControlReader())

        automation.capture_foreground(grabber)

        self.assertEqual(grabber.regions, [None])

    def test_decodes_png_dimensions(self) -> None:
        grabber = FakeGrabber((640, 480))
        frame = WindowsUiAutomation(FakeBackend(), NullControlReader()).capture_foreground(grabber)

        with io.BytesIO(frame.image) as stream:
            from PIL import Image

            with Image.open(stream) as image:
                self.assertEqual(image.size, (640, 480))


class Win32BackendTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Win32 backend is Windows-only")
    def test_reads_a_foreground_window_on_windows(self) -> None:
        backend = Win32Backend()

        handle = backend.foreground_handle()

        self.assertGreater(handle, 0)
        self.assertIsInstance(backend.title(handle), str)


if __name__ == "__main__":
    unittest.main()
