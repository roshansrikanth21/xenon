"""End-to-end checks of the CLI wiring, using a local OpenAI-compatible stub.

These talk to an in-process HTTP server, so the real request/response path is
exercised without any API key or network access.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from xenon import __main__ as cli
from xenon.screen import ScreenContext

COMMANDS = ["screen", "doctor", "ask", "do", "ui", "voice", "search", "skills", "memory", "notion"]


def reply_for(payload: dict) -> str:
    messages = payload.get("messages", [])
    system = messages[0].get("content", "") if messages else ""
    content = messages[-1].get("content", "") if messages else ""
    if "desktop task planner" in str(system):
        return json.dumps({"steps": [{"kind": "observe", "reason": "confirm the window"}]})
    if "Always cite the window title" in str(system):
        return "skill instructions were loaded"
    if isinstance(content, list):
        return "a slide deck about AI"
    window = next(
        (line for line in str(content).splitlines() if line.startswith("Window:")),
        "Window: (missing)",
    )
    return f"answer from {window}"


class StubModel(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        body = json.dumps(
            {"choices": [{"message": {"content": reply_for(payload)}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@contextlib.contextmanager
def stub_model():
    server = HTTPServer(("127.0.0.1", 0), StubModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


class FakeObserver:
    def __init__(self) -> None:
        self.prewarmed = 0

    def prewarm(self) -> None:
        self.prewarmed += 1

    def observe(self) -> ScreenContext:
        return ScreenContext(
            app_name="Opera",
            title="Intro - Opera",
            text="Slide 2 of 30 about AI",
            used_vision=False,
        )


class FakeActionBackend:
    """Never touches the desktop."""

    def __init__(self, ok: bool = True, detail: str = "") -> None:
        self.calls: list[str] = []
        self.ok = ok
        self.detail = detail

    def execute(self, action):
        self.calls.append(action.kind)
        from xenon.actions import ActionResult

        return ActionResult(self.ok, self.detail or f"ran {action.kind}")


class FakeVerifier:
    """Succeeds without reading the real foreground window."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def verify(self, goal: str):
        from xenon.actions import Verification

        return Verification(True, "fake verification")


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class ParserTests(unittest.TestCase):
    """Registration checks: these run on every platform."""

    def test_every_command_is_registered(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([command, "--help"])
                self.assertEqual(raised.exception.code, 0)

    def test_ui_help_documents_the_topmost_flag(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["ui", "--help"])

        self.assertIn("--no-topmost", out.getvalue())

    @unittest.skipIf(sys.platform == "win32", "Windows runs the real handler")
    def test_windows_only_commands_refuse_other_platforms(self) -> None:
        for argv in (
            ["screen"],
            ["ask", "what is this?"],
            ["do", "open notepad"],
            ["ui"],
            ["voice"],
        ):
            with self.subTest(argv=argv):
                code, _, err = run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn("Windows", err)

    def test_missing_config_is_reported_clearly(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XENON_MODEL_API_KEY", None)
            os.environ.pop("XENON_MODEL_NAME", None)
            with mock.patch.object(cli, "is_windows", lambda: True):
                code, _, err = run_cli(["ask", "anything"])

        self.assertEqual(code, 2)
        self.assertIn("XENON_MODEL_API_KEY", err)

    def test_unconfigured_notion_ingest_is_reported_clearly(self) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"XENON_NOTION_TOKEN", "XENON_NOTION_ROOT_PAGE"}
        }
        with mock.patch.dict(os.environ, env, clear=True):
            code, _, err = run_cli(["notion", "ingest"])

        self.assertEqual(code, 2)
        self.assertIn("XENON_NOTION_TOKEN", err)


class AssistAcceptanceTests(unittest.TestCase):
    """Full ask path: real HTTP request, real memory store, faked screen."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "memory.db")
        self.observer = FakeObserver()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def env(self, base_url: str) -> dict[str, str]:
        return {
            "XENON_MODEL_API_KEY": "test",
            "XENON_MODEL_NAME": "stub",
            "XENON_MODEL_BASE_URL": base_url,
            "XENON_DB_PATH": self.db,
            "PYTHONIOENCODING": "utf-8",
        }

    def test_ask_answers_from_the_screen(self) -> None:
        with stub_model() as base_url, mock.patch.dict(
            os.environ, self.env(base_url), clear=False
        ), mock.patch.object(cli, "is_windows", lambda: True), mock.patch.object(
            cli, "_observer", lambda: self.observer
        ):
            code, out, err = run_cli(["ask", "what is on my screen?"])

        self.assertEqual(code, 0, err)
        self.assertIn("Intro - Opera", out)

    def test_ocr_is_the_default_fallback_and_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XENON_VISION_API_KEY", None)
            os.environ.pop("XENON_VISION_MODEL", None)
            os.environ["XENON_OCR"] = "1"
            self.assertEqual(type(cli._vision_provider()).__name__, "VisionChain")

            os.environ["XENON_OCR"] = "off"
            self.assertEqual(
                type(cli._vision_provider()).__name__, "UnconfiguredVision"
            )

    def test_observer_is_prewarmed_before_the_first_request(self) -> None:
        automation = mock.MagicMock()
        observer = mock.MagicMock()
        with mock.patch("xenon.windows.WindowsUiAutomation", lambda: automation), mock.patch.object(
            cli, "ScreenObserver", lambda *args, **kwargs: observer
        ):
            cli._observer()

        observer.prewarm.assert_called_once_with()

    def test_ask_cites_previously_ingested_memory(self) -> None:
        from xenon.memory import MemoryStore

        vault = Path(self.tmp.name) / "vault"
        vault.mkdir()
        (vault / "deadline.md").write_text(
            "# Deadline\n\nThe AI assignment is due on Friday.\n", encoding="utf-8"
        )
        with MemoryStore(self.db) as store:
            store.ingest_markdown("obsidian", vault)

        with stub_model() as base_url, mock.patch.dict(
            os.environ, self.env(base_url), clear=False
        ), mock.patch.object(cli, "is_windows", lambda: True), mock.patch.object(
            cli, "_observer", lambda: self.observer
        ):
            code, out, err = run_cli(["ask", "when is the assignment due?"])

        self.assertEqual(code, 0, err)
        self.assertIn("Deadline", out)
        self.assertIn("obsidian", out)

    def test_ask_skill_is_loaded_into_the_request(self) -> None:
        skills = Path(self.tmp.name) / "skills"
        (skills / "screen-context").mkdir(parents=True)
        (skills / "screen-context" / "SKILL.md").write_text(
            "---\nname: screen-context\ndescription: Read the active window.\n---\n\n"
            "Always cite the window title.\n",
            encoding="utf-8",
        )
        env = self.env("http://127.0.0.1:1/v1")
        env["XENON_SKILLS_DIR"] = str(skills)
        with stub_model() as base_url, mock.patch.dict(
            os.environ, dict(env, XENON_MODEL_BASE_URL=base_url), clear=False
        ), mock.patch.object(
            cli, "is_windows", lambda: True
        ), mock.patch.object(cli, "_observer", lambda: self.observer):
            code, out, err = run_cli(["ask", "--skill", "screen-context", "hi"])

        self.assertEqual(code, 0, err)
        self.assertIn("skill instructions were loaded", out)

    def test_missing_skill_fails_before_calling_the_model(self) -> None:
        env = self.env("http://127.0.0.1:1/v1")
        env["XENON_SKILLS_DIR"] = str(Path(self.tmp.name) / "nope")
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            cli, "is_windows", lambda: True
        ):
            code, _, err = run_cli(["ask", "--skill", "missing", "hi"])

        self.assertEqual(code, 2)
        self.assertIn("skill error", err)

    def test_do_dry_run_prints_the_plan_and_does_nothing(self) -> None:
        with stub_model() as base_url, mock.patch.dict(
            os.environ, self.env(base_url), clear=False
        ), mock.patch.object(cli, "is_windows", lambda: True), mock.patch.object(
            cli, "_observer", lambda: self.observer
        ):
            code, out, err = run_cli(["do", "check the window", "--dry-run"])

        self.assertEqual(code, 0, err)
        self.assertIn("plan for: check the window", out)
        self.assertIn("observe", out)
        self.assertNotIn("status:", out)

    def test_do_with_yes_runs_the_approved_plan(self) -> None:
        backend = FakeActionBackend()
        with stub_model() as base_url, mock.patch.dict(
            os.environ, self.env(base_url), clear=False
        ), mock.patch.object(cli, "is_windows", lambda: True), mock.patch.object(
            cli, "_observer", lambda: self.observer
        ), mock.patch(
            "xenon.windows_actions.WindowsActionBackend", lambda: backend
        ):
            code, out, err = run_cli(["do", "check the window", "--yes"])

        # Without --expect-title nothing can be verified, so this is not a
        # success exit; the step still ran and the reason is printed.
        self.assertEqual(code, 1, err)
        self.assertEqual(backend.calls, ["observe"])
        self.assertIn("status: unverified", out)
        self.assertIn("ran observe", out)

    def test_do_declining_the_prompt_runs_nothing(self) -> None:
        backend = FakeActionBackend()
        with stub_model() as base_url, mock.patch.dict(
            os.environ, self.env(base_url), clear=False
        ), mock.patch.object(cli, "is_windows", lambda: True), mock.patch.object(
            cli, "_observer", lambda: self.observer
        ), mock.patch(
            "xenon.windows_actions.WindowsActionBackend", lambda: backend
        ), mock.patch("builtins.input", lambda prompt="": "n"):
            code, out, _ = run_cli(["do", "check the window"])

        self.assertEqual(code, 1)
        self.assertEqual(backend.calls, [])
        self.assertIn("cancelled", out)

    def test_do_fast_path_opens_an_app_without_a_model(self) -> None:
        backend = FakeActionBackend()
        with mock.patch.dict(
            os.environ, {"XENON_DB_PATH": self.db}, clear=True
        ), mock.patch.object(cli, "is_windows", lambda: True), mock.patch(
            "xenon.windows_actions.WindowsActionBackend", lambda: backend
        ), mock.patch(
            "xenon.windows_actions.ForegroundTitleVerifier", FakeVerifier
        ), mock.patch(
            "xenon.windows_actions.DesktopTitleVerifier", FakeVerifier
        ):
            code, out, err = run_cli(["do", "open", "notepad", "--yes"])

        self.assertEqual(code, 0, err)
        self.assertIn("fast path: open notepad", out)
        self.assertIn("target='notepad'", out)
        self.assertIn("status: succeeded", out)
        self.assertEqual(backend.calls, ["open_app"])

    def test_do_fast_path_dry_run_runs_nothing(self) -> None:
        backend = FakeActionBackend()
        with mock.patch.dict(
            os.environ, {"XENON_DB_PATH": self.db}, clear=True
        ), mock.patch.object(cli, "is_windows", lambda: True), mock.patch(
            "xenon.windows_actions.WindowsActionBackend", lambda: backend
        ):
            code, out, err = run_cli(["do", "open", "notepad", "--dry-run"])

        self.assertEqual(code, 0, err)
        self.assertIn("fast path:", out)
        self.assertEqual(backend.calls, [])

    def test_do_fast_path_searches_without_a_model(self) -> None:
        from xenon.websearch import SearchResult

        def fake_search(query: str, limit: int = 5):
            return [SearchResult("Python docs", "https://docs.python.org/3/", "Tutorial.")]

        with mock.patch.dict(
            os.environ, {"XENON_DB_PATH": self.db}, clear=True
        ), mock.patch.object(cli, "is_windows", lambda: True), mock.patch.object(
            cli.websearch, "search", fake_search
        ):
            code, out, err = run_cli(["do", "search", "for", "python", "asyncio"])

        self.assertEqual(code, 0, err)
        self.assertIn("fast path:", out)
        self.assertIn("Python docs", out)

    def test_model_failure_surfaces_as_a_clear_error(self) -> None:
        with mock.patch.dict(
            os.environ, self.env("http://127.0.0.1:1/v1"), clear=False
        ), mock.patch.object(cli, "is_windows", lambda: True), mock.patch.object(
            cli, "_observer", lambda: self.observer
        ):
            code, _, err = run_cli(["ask", "hello"])

        self.assertEqual(code, 1)
        self.assertIn("error:", err)


class VoiceFastPathTests(unittest.TestCase):
    def test_open_and_focus_auto_run_with_an_injected_backend(self) -> None:
        backend = FakeActionBackend()

        self.assertEqual(
            cli._voice_fast_reply("open calculator", backend=backend),
            "Opened Calculator.",
        )
        self.assertEqual(
            cli._voice_fast_reply("switch to chrome", backend=backend),
            "Switched to chrome.",
        )
        self.assertEqual(backend.calls, ["open_app", "focus_window"])

    def test_a_failed_action_is_reported_plainly(self) -> None:
        backend = FakeActionBackend(ok=False, detail="no window matching 'chrome'")

        reply = cli._voice_fast_reply("switch to chrome", backend=backend)

        self.assertIn("Couldn't", reply)
        self.assertIn("chrome", reply)

    def test_search_speaks_the_top_result(self) -> None:
        from xenon.websearch import SearchResult

        def fake_search(query: str, limit: int = 1):
            self.assertEqual(query, "example")
            return [SearchResult("Example", "https://example.com", "A useful page.")]

        reply = cli._voice_fast_reply("google example", search_fn=fake_search)

        self.assertIn("Example", reply)
        self.assertIn("A useful page.", reply)

    def test_a_model_question_is_not_a_fast_path(self) -> None:
        self.assertIsNone(
            cli._voice_fast_reply("write a short summary", backend=FakeActionBackend())
        )


if __name__ == "__main__":
    unittest.main()
