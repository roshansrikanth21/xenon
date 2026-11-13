"""wallpaper.is_covered — the coverage gate that stops wasted rendering."""
from __future__ import annotations

import unittest
import pytest

from xenon import wallpaper

TARGET = (100, 100, 500, 400)


class FakeUser32:
    def __init__(self, styles: dict[int, int] | None = None) -> None:
        self.visible: set[int] = set()
        self.minimized: set[int] = set()
        self.styles = styles or {}

    def IsWindowVisible(self, hwnd: int) -> int:
        return 1 if hwnd in self.visible else 0

    def IsIconic(self, hwnd: int) -> int:
        return 1 if hwnd in self.minimized else 0

    def GetWindowLongPtrW(self, hwnd: int, index: int) -> int:
        return self.styles.get(hwnd, 0)


def rect_of(hwnd: int, box: tuple[int, int, int, int]):
    return lambda _hwnd: box if _hwnd == hwnd else None


@pytest.fixture()
def gate(monkeypatch):
    """Install a fake user32 plus fake rect/class/cloak lookups."""
    fake = FakeUser32()
    monkeypatch.setattr(wallpaper, "_user32", lambda: fake)
    rects: dict[int, tuple[int, int, int, int]] = {}
    names: dict[int, str] = {}

    monkeypatch.setattr(
        wallpaper, "window_rect", lambda hwnd: rects.get(hwnd)
    )
    monkeypatch.setattr(wallpaper, "class_name_of", lambda hwnd: names.get(hwnd, ""))
    monkeypatch.setattr(wallpaper, "is_cloaked", lambda hwnd: False)
    return fake, rects, names


def test_uncovered_when_nothing_else_visible(gate):
    fake, rects, names = gate
    fake.visible.add(1)
    rects[1] = TARGET
    assert wallpaper.is_covered(1, TARGET, windows=[]) is False


def test_partial_overlap_is_not_covered(gate):
    fake, rects, names = gate
    fake.visible.add(1)
    fake.visible.add(2)
    rects[1] = TARGET
    rects[2] = (100, 100, 500, 390)  # 10px short at the bottom
    assert wallpaper.is_covered(1, TARGET, windows=[2]) is False


def test_full_cover_is_covered(gate):
    fake, rects, names = gate
    fake.visible.add(1)
    fake.visible.add(2)
    rects[1] = TARGET
    rects[2] = (0, 0, 1920, 1080)
    assert wallpaper.is_covered(1, TARGET, windows=[2]) is True


def test_shell_windows_never_count_as_covers(gate):
    fake, rects, names = gate
    fake.visible.add(1)
    fake.visible.add(2)
    rects[1] = TARGET
    rects[2] = (0, 0, 1920, 1080)
    names[2] = "Progman"
    assert wallpaper.is_covered(1, TARGET, windows=[2]) is False


def test_invisible_or_minimized_windows_do_not_cover(gate):
    fake, rects, names = gate
    fake.visible.add(1)
    rects[1] = TARGET
    rects[2] = (0, 0, 1920, 1080)
    # Not visible at all.
    assert wallpaper.is_covered(1, TARGET, windows=[2]) is False
    # Visible but minimized.
    fake.visible.add(2)
    fake.minimized.add(2)
    assert wallpaper.is_covered(1, TARGET, windows=[2]) is False


def test_noactivate_overlays_do_not_cover(gate):
    """Invisible full-screen HUDs (NVIDIA overlay) sit over everything but
    must not pause the widget."""
    fake, rects, names = gate
    fake.visible.add(1)
    fake.visible.add(2)
    fake.styles[2] = wallpaper.WS_EX_NOACTIVATE
    rects[1] = TARGET
    rects[2] = (0, 0, 1920, 1080)
    assert wallpaper.is_covered(1, TARGET, windows=[2]) is False


def test_cloaked_windows_do_not_cover(gate, monkeypatch):
    fake, rects, names = gate
    monkeypatch.setattr(wallpaper, "is_cloaked", lambda hwnd: True)
    fake.visible.add(1)
    fake.visible.add(2)
    rects[1] = TARGET
    rects[2] = (0, 0, 1920, 1080)
    assert wallpaper.is_covered(1, TARGET, windows=[2]) is False


def test_no_rect_means_no_render_anyway(gate):
    assert wallpaper.is_covered(1, None, windows=[]) is False


class ChildStyleTests(unittest.TestCase):
    def test_child_style_swaps_popup_for_child_and_adds_click_through(self) -> None:
        import sys
        if sys.platform != "win32":
            self.skipTest("Windows only")
        from unittest import mock
        from xenon import wallpaper

        state = {-16: wallpaper.WS_POPUP | 0x10000000, -20: 0x80000}
        fake = mock.MagicMock()
        fake.GetWindowLongPtrW.side_effect = lambda h, i: state[i]
        fake.SetWindowLongPtrW.side_effect = lambda h, i, v: state.__setitem__(i, v & 0xFFFFFFFF)
        with mock.patch.object(wallpaper, "_user32", return_value=fake):
            wallpaper._set_child_style(1, True)
            self.assertTrue(state[-16] & wallpaper.WS_CHILD)
            self.assertFalse(state[-16] & wallpaper.WS_POPUP)
            self.assertTrue(state[-20] & wallpaper.WS_EX_TRANSPARENT)
            wallpaper._set_child_style(1, False)
            self.assertTrue(state[-16] & wallpaper.WS_POPUP)
            self.assertFalse(state[-16] & wallpaper.WS_CHILD)
