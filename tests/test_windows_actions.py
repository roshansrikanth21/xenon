import os
import sys
import unittest
from unittest import mock

from xenon.actions import Action, ActionContext
from xenon.windows_actions import (
    DesktopTitleVerifier,
    ForegroundTitleVerifier,
    WindowsActionBackend,
    parse_keys,
    virtual_key,
)

# No kill-switch flags (they would change the classification) and no
# provider keys (nothing here needs them, so nothing can leak them).
_CLEAN_ENV = {
    key: value
    for key, value in os.environ.items()
    if not key.upper().startswith("XENON_") and not key.upper().endswith("_API_KEY")
}


def notepad_reader(action: Action) -> ActionContext:
    """Live-context stand-in: an allowlisted Notepad document has focus."""
    return ActionContext(
        process_image="notepad.exe",
        window_title="Notes - Notepad",
        focused_role="Document",
        allowlist=frozenset({"notepad.exe"}),
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
            backend=title_backend,
            desktop_factory=lambda: desktop,
            context_reader=notepad_reader,
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


class FakeUser32:
    """Records key events instead of sending them."""

    def __init__(self) -> None:
        self.events: list[tuple[int, int]] = []

    def keybd_event(self, key, scan, flags, extra) -> None:
        self.events.append((key, flags))


class ExplodingDesktop:
    """Fails the test if a refused action reaches UI Automation."""

    def window(self, **kwargs):
        raise AssertionError("the desktop must not be touched")


def context(**fields):
    return lambda action: ActionContext(**fields)


class LiveGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, _CLEAN_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def backend(self, reader, **kwargs) -> tuple[WindowsActionBackend, FakeUser32, list[str]]:
        backend = WindowsActionBackend(
            backend=FakeBackend("WhatsApp"),
            desktop_factory=ExplodingDesktop,
            context_reader=reader,
            **kwargs,
        )
        user32 = FakeUser32()
        backend._user32 = user32
        typed: list[str] = []
        backend._send_unicode = typed.append
        return backend, user32, typed

    def test_legacy_press_enter_in_whatsapp_is_outward(self) -> None:
        backend, user32, _ = self.backend(
            context(process_image="WhatsApp.Root", window_title="WhatsApp", focused_role="Edit")
        )

        result = backend.execute(Action("press_keys", value="enter"))

        self.assertFalse(result.ok)
        self.assertIn("refused", result.detail)
        self.assertIn("orb", result.detail)
        self.assertEqual(user32.events, [])

    def test_type_into_console_is_denied(self) -> None:
        for image in ("WindowsTerminal", "cmd.exe", "powershell", "conhost.exe"):
            with self.subTest(image=image):
                backend, _, typed = self.backend(context(process_image=image, window_title="Windows PowerShell"))

                result = backend.execute(Action("type_text", value="Remove-Item -Recurse C:\\\\"))

                self.assertFalse(result.ok)
                self.assertIn("console", result.detail)
                self.assertEqual(typed, [])

    def test_click_send_in_comms_is_refused_before_ui_automation(self) -> None:
        backend, _, _ = self.backend(context(process_image="WhatsApp.Root.exe", window_title="WhatsApp"))

        result = backend.execute(Action("click_control", target="Send", value="Button"))

        self.assertFalse(result.ok)
        self.assertIn("refused", result.detail)

    def test_gate_instance_may_commit_outward(self) -> None:
        backend, user32, _ = self.backend(
            context(process_image="WhatsApp.Root.exe", focused_role="Edit"), allow_outward=True
        )

        result = backend.execute(Action("press_keys", value="enter"))

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(user32.events[0], (0x0D, 0))

    def test_outward_permission_does_not_cover_destructive(self) -> None:
        backend, user32, _ = self.backend(
            context(process_image="explorer.exe", window_title="Downloads", focused_role="ListItem"),
            allow_outward=True,
        )

        result = backend.execute(Action("press_keys", value="delete"))

        self.assertFalse(result.ok)
        self.assertIn("destructive", result.detail)
        self.assertEqual(user32.events, [])

    def test_denied_surface_is_refused_even_for_the_gate(self) -> None:
        backend, _, typed = self.backend(
            context(process_image="cmd.exe"), allow_outward=True, allow_destructive=True
        )

        result = backend.execute(Action("type_text", value="whoami"))

        self.assertFalse(result.ok)
        self.assertEqual(typed, [])

    def test_blocked_keys_are_refused(self) -> None:
        backend, user32, _ = self.backend(context(process_image="notepad.exe", focused_role="Document"))

        for keys in ("alt+f4", "win+r", "ctrl+shift+esc"):
            with self.subTest(keys=keys):
                self.assertFalse(backend.execute(Action("press_keys", value=keys)).ok)
        self.assertEqual(user32.events, [])

    def test_local_keys_in_notepad_still_work(self) -> None:
        backend, user32, _ = self.backend(context(process_image="notepad.exe", focused_role="Document"))

        result = backend.execute(Action("press_keys", value="ctrl+s"))

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(len(user32.events), 4)

    def test_unidentified_foreground_is_refused(self) -> None:
        backend, _, typed = self.backend(context(process_image=""))

        result = backend.execute(Action("type_text", value="hi"))

        self.assertFalse(result.ok)
        self.assertIn("could not identify", result.detail)
        self.assertEqual(typed, [])

    def test_unreadable_target_is_refused(self) -> None:
        def broken(action):
            raise OSError("access denied")

        backend, _, typed = self.backend(broken)

        result = backend.execute(Action("type_text", value="hi"))

        self.assertFalse(result.ok)
        self.assertEqual(typed, [])

    def test_unguarded_kinds_skip_the_live_read(self) -> None:
        def must_not_run(action):
            raise AssertionError("observe and wait do not need the live target")

        backend, _, _ = self.backend(must_not_run)

        self.assertTrue(backend.execute(Action("wait", value="0")).ok)


class StaticGuardTests(unittest.TestCase):
    """open_app / focus_window / write_file are refused by the backend itself.

    TaskRunner asks ApprovalPolicy first, but a caller that skipped it must
    still not reach a URI handler, a console or a Startup-folder script.
    """

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, _CLEAN_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.touched: list[str] = []

    def backend(self, task: ActionContext | None = None) -> WindowsActionBackend:
        test = self

        class NoCatalog:
            def resolve(self, target):
                test.touched.append(f"catalog:{target}")
                return None

        def no_live_read(action):
            raise AssertionError("these kinds do not depend on the foreground")

        def no_desktop():
            test.touched.append("desktop")
            raise AssertionError("the desktop must not be touched")

        return WindowsActionBackend(
            backend=FakeBackend(),
            desktop_factory=no_desktop,
            catalog=NoCatalog(),
            context_reader=no_live_read,
            task_context=task,
            title_scanner=lambda: [],
        )

    def test_open_app_uri_unc_scheme_and_console_are_refused(self) -> None:
        backend = self.backend()
        with mock.patch("os.startfile", create=True) as startfile, mock.patch(
            "xenon.windows_actions.subprocess.Popen"
        ) as popen:
            for target in (
                "ms-msdt:/id PCWDiagnostic",
                "search-ms:query=secrets",
                "\\\\evil.example\\share\\run.exe",
                "//evil.example/share/run.exe",
                "cmd.exe",
                "powershell",
                "regedit",
            ):
                with self.subTest(target=target):
                    result = backend.execute(Action("open_app", target=target))

                    self.assertFalse(result.ok)
                    self.assertTrue(result.detail.startswith("refused"), result.detail)
        startfile.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(self.touched, [])

    def test_model_origin_may_only_name_a_catalog_app(self) -> None:
        backend = self.backend(ActionContext(origin="model"))

        result = backend.execute(Action("open_app", target="C:\\Windows\\System32\\calc.exe"))

        self.assertFalse(result.ok)
        self.assertIn("catalog", result.detail)
        self.assertEqual(self.touched, [])

    def test_focus_window_on_a_console_or_outside_a_tainted_allowlist_is_refused(self) -> None:
        cases = (
            (ActionContext(), "Command Prompt"),
            (ActionContext(), "Administrator: Windows PowerShell"),
            (ActionContext(tainted=True, allowlist=frozenset({"notepad.exe"})), "WhatsApp"),
        )
        for task, target in cases:
            with self.subTest(target=target):
                result = self.backend(task).execute(Action("focus_window", target=target))

                self.assertFalse(result.ok)
                self.assertTrue(result.detail.startswith("refused"), result.detail)
        self.assertEqual(self.touched, [])

    def test_write_file_refuses_scripts_and_startup(self) -> None:
        import tempfile
        from pathlib import Path

        folder = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        startup = folder / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "a.txt"
        backend = self.backend()

        for target in (folder / "run.ps1", folder / "evil.lnk", startup, Path("\\\\evil.example\\share\\a.txt")):
            with self.subTest(target=str(target)):
                result = backend.execute(Action("write_file", target=str(target), value="x"))

                self.assertFalse(result.ok)
                self.assertTrue(result.detail.startswith("refused"), result.detail)
        self.assertEqual(list(folder.iterdir()), [])

    def test_ordinary_targets_still_reach_the_handler(self) -> None:
        import tempfile
        from pathlib import Path

        folder = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        target = folder / "notes.txt"

        result = self.backend().execute(Action("write_file", target=str(target), value="hello"))

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")


class Win32Facts:
    """A Win32Backend stand-in with a process id and name."""

    def __init__(self, title: str, pid: int, name: str) -> None:
        self._title = title
        self._pid = pid
        self._name = name

    def foreground_handle(self) -> int:
        return 42

    def title(self, handle: int) -> str:
        return self._title

    def process_id(self, handle: int) -> int:
        return self._pid

    def process_name(self, pid: int) -> str:
        return self._name


class LiveContextTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, _CLEAN_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("xenon.windows_actions.ui_language_is_english", lambda: True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_image_title_focus_and_task_facts(self) -> None:
        task = ActionContext(tainted=True, user_words="say hi", allowlist=frozenset({"WhatsApp.Root.exe"}), origin="voice")
        backend = WindowsActionBackend(
            backend=Win32Facts("WhatsApp", 1234, "WhatsApp.Root"),
            desktop_factory=ExplodingDesktop,
            task_context=task,
            focus_reader=lambda: ("Edit", "Type a message"),
            url_reader=lambda hwnd: self.fail("WhatsApp is not a browser"),
        )

        ctx = backend.live_context(Action("press_keys", value="enter"))

        self.assertEqual(ctx.process_image, "WhatsApp.Root")
        self.assertEqual(ctx.window_title, "WhatsApp")
        self.assertEqual((ctx.focused_role, ctx.element_name), ("Edit", "Type a message"))
        self.assertEqual((ctx.tainted, ctx.user_words, ctx.origin), (True, "say hi", "voice"))
        self.assertTrue(ctx.ui_language_english)

    def test_click_control_uses_the_target_as_the_element(self) -> None:
        backend = WindowsActionBackend(
            backend=Win32Facts("Calculator", 1234, "CalculatorApp"),
            desktop_factory=ExplodingDesktop,
            focus_reader=lambda: ("Button", "Clear"),
        )

        ctx = backend.live_context(Action("click_control", target="Seven", value="Button"))

        self.assertEqual((ctx.element_name, ctx.element_role), ("Seven", "Button"))

    def test_browser_url_is_read_and_failure_means_comms(self) -> None:
        backend = WindowsActionBackend(
            backend=Win32Facts("Inbox - Opera", 1234, "opera"),
            desktop_factory=ExplodingDesktop,
            focus_reader=lambda: ("", ""),
            url_reader=lambda hwnd: None,
        )
        backend._user32 = FakeUser32()

        ctx = backend.live_context(Action("press_keys", value="tab"))
        result = backend.execute(Action("press_keys", value="ctrl+v"))

        self.assertEqual(ctx.url, "")
        self.assertFalse(result.ok)  # unreadable URL: comms, so ctrl+v is outward
        self.assertEqual(backend._user32.events, [])

    def test_xenon_own_window_is_refused(self) -> None:
        backend = WindowsActionBackend(
            backend=Win32Facts("Xenon", os.getpid(), "pythonw"),
            desktop_factory=ExplodingDesktop,
            focus_reader=lambda: ("Button", "Confirm"),
        )
        typed: list[str] = []
        backend._send_unicode = typed.append

        result = backend.execute(Action("type_text", value="hi"))

        self.assertFalse(result.ok)
        self.assertIn("own windows", result.detail)
        self.assertEqual(typed, [])

    def test_frame_host_is_resolved_to_the_app_inside(self) -> None:
        names = {10: "ApplicationFrameHost", 20: "CalculatorApp"}

        class FrameHost(Win32Facts):
            def process_name(self, pid: int) -> str:
                return names[pid]

        backend = WindowsActionBackend(
            backend=FrameHost("Calculator", 10, ""),
            desktop_factory=ExplodingDesktop,
            focus_reader=lambda: ("Button", "Seven"),
        )
        with mock.patch("xenon.windows_actions.frame_host_child_pid", lambda hwnd, pid: 20):
            ctx = backend.live_context(Action("click_control", target="Seven"))

        self.assertEqual(ctx.process_image, "CalculatorApp")

    def test_live_context_works_as_a_task_runner_provider(self) -> None:
        from xenon.actions import ApprovalPolicy, Risk, TaskRunner

        backend = WindowsActionBackend(
            backend=Win32Facts("WhatsApp", 1234, "WhatsApp.Root"),
            desktop_factory=ExplodingDesktop,
            focus_reader=lambda: ("Edit", "Type a message"),
        )
        seen: list[Risk] = []

        class Recorder:
            def execute(self, action):
                raise AssertionError("nothing should run")

        TaskRunner(
            ApprovalPolicy(),
            Recorder(),
            approver=lambda action, risk: seen.append(risk) or False,
            context_provider=backend.live_context,
        ).run("send", [Action("press_keys", value="enter")])

        self.assertEqual(seen, [Risk.OUTWARD])


if __name__ == "__main__":
    unittest.main()
