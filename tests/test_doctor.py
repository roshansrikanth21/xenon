import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xenon.chat import ChatConfig
from xenon.doctor import (
    FAIL,
    PASS,
    WARN,
    check_memory,
    check_model,
    check_python,
    check_screen,
    check_skills,
    check_voice,
    exit_code,
    run_checks,
)
from xenon.screen import ScreenContext

GOOD = ScreenContext(
    app_name="Notepad",
    title="notes.md - Notepad",
    text="draft the release notes",
    used_vision=False,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def models_response(ids: list[str]) -> FakeResponse:
    return FakeResponse(json.dumps({"data": [{"id": item} for item in ids]}).encode())


class DoctorCheckTests(unittest.TestCase):
    def test_python_check_runs_here(self) -> None:
        check = check_python()

        self.assertEqual(check.status, PASS)
        self.assertIn("Python", check.detail)

    def test_screen_check_reports_a_readable_window(self) -> None:
        check = check_screen(lambda: GOOD)

        self.assertEqual(check.status, PASS)
        self.assertIn("notes.md - Notepad", check.detail)
        self.assertIn("UI Automation", check.detail)

    def test_screen_check_warns_when_the_view_is_limited(self) -> None:
        context = ScreenContext(
            app_name="OpenCode",
            title="Xenon - OpenCode",
            text="Minimize",
            used_vision=False,
            error="no fallback could read the screen",
        )

        check = check_screen(lambda: context)

        self.assertEqual(check.status, WARN)
        self.assertIn("no fallback", check.detail)

    def test_screen_check_warns_when_nothing_is_readable(self) -> None:
        context = ScreenContext("App", "Title", "", used_vision=False)

        self.assertEqual(check_screen(lambda: context).status, WARN)

    def test_screen_check_fails_when_observation_raises(self) -> None:
        def observe():
            raise RuntimeError("no foreground window")

        check = check_screen(observe)

        self.assertEqual(check.status, FAIL)
        self.assertIn("no foreground window", check.detail)

    def test_screen_is_not_checked_without_a_provider(self) -> None:
        self.assertEqual(check_screen(None).status, WARN)

    def test_model_check_warns_when_answers_are_disabled(self) -> None:
        check = check_model(None)

        self.assertEqual(check.status, WARN)
        self.assertIn("XENON_MODEL_API_KEY", check.detail)

    def test_model_check_passes_when_the_model_is_listed(self) -> None:
        config = ChatConfig("http://127.0.0.1:11434/v1", "qwen2.5:7b", "ollama")
        with mock.patch(
            "xenon.doctor.urllib.request.urlopen",
            return_value=models_response(["qwen2.5:7b", "llava"]),
        ):
            check = check_model(config)

        self.assertEqual(check.status, PASS)
        self.assertIn("qwen2.5:7b", check.detail)

    def test_model_check_warns_when_the_model_is_missing(self) -> None:
        config = ChatConfig("http://127.0.0.1:11434/v1", "missing:7b", "ollama")
        with mock.patch(
            "xenon.doctor.urllib.request.urlopen",
            return_value=models_response(["qwen2.5:7b"]),
        ):
            check = check_model(config)

        self.assertEqual(check.status, WARN)
        self.assertIn("not listed", check.detail)

    def test_model_check_fails_when_the_server_is_down(self) -> None:
        config = ChatConfig("http://127.0.0.1:1/v1", "qwen2.5:7b", "ollama")
        with mock.patch(
            "xenon.doctor.urllib.request.urlopen",
            side_effect=ConnectionError("refused"),
        ):
            check = check_model(config)

        self.assertEqual(check.status, FAIL)
        self.assertIn("cannot reach", check.detail)
        self.assertIn("ollama serve", check.detail)

    def test_memory_check_opens_the_store_and_counts_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = check_memory(str(Path(tmp) / "memory.db"))

        self.assertEqual(check.status, PASS)
        self.assertIn("0 documents", check.detail)

    def test_memory_check_reports_an_unusable_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A directory cannot be opened as a SQLite database.
            check = check_memory(tmp)

        self.assertEqual(check.status, FAIL)

    def test_skills_check_counts_valid_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "demo"
            folder.mkdir()
            (folder / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill.\n---\n\nBody.\n",
                encoding="utf-8",
            )
            check = check_skills(tmp)

        self.assertEqual(check.status, PASS)
        self.assertIn("1 skills", check.detail)

    def test_voice_checks_degrade_when_dependencies_are_missing(self) -> None:
        with mock.patch("xenon.doctor.importlib.util.find_spec", return_value=None):
            checks = check_voice()

        names = [check.name for check in checks]
        self.assertIn("voice:sounddevice", names)
        self.assertIn("microphone", names)
        self.assertTrue(all(check.status == WARN for check in checks))

    def test_exit_code_is_failure_only_for_failures(self) -> None:
        self.assertEqual(exit_code([]), 0)
        self.assertEqual(exit_code([check_screen(None)]), 0)
        self.assertEqual(
            exit_code(
                [
                    check_screen(None),
                    check_screen(lambda: (_ for _ in ()).throw(RuntimeError("x"))),
                ]
            ),
            1,
        )


class RunChecksTests(unittest.TestCase):
    def test_runs_the_full_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checks = run_checks(
                observe=lambda: GOOD,
                model=None,
                db_path=str(Path(tmp) / "memory.db"),
                skills_root=tmp,
            )

        names = [check.name for check in checks]
        self.assertIn("python", names)
        self.assertIn("screen", names)
        self.assertIn("model", names)
        self.assertIn("memory", names)
        self.assertIn("skills", names)
        self.assertIn("microphone", names)
        self.assertFalse(any(check.status == FAIL for check in checks))


if __name__ == "__main__":
    unittest.main()
