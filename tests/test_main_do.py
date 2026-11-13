"""`xenon do` from the CLI: --yes and 'y' approve LOCAL steps only.

OUTWARD and DESTRUCTIVE steps are found before anything runs. If the orb is
running the goal is handed to it over IPC (its card does the confirming);
otherwise the CLI refuses with "needs the orb". DENIED steps are refused
outright and never handed off. Nothing here touches the desktop, a model,
the network or the real named pipe.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import unittest
from unittest import mock

from xenon import __main__ as cli
from xenon.actions import Action, ActionContext, ActionResult, Risk
from xenon.planner import Plan
from xenon.screen import ScreenContext

# Provider keys and kill switches from the real environment must not leak in.
_SCRUB = (
    "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY",
    "OPENAI_API_KEY", "XENON_MODEL_API_KEY", "XENON_OUTWARD", "XENON_AGENT",
    "XENON_VOICE_APPROVE",
)

PLAN_SEND = (
    Action("open_app", "WhatsApp"),
    Action("type_text", value="hi"),
    Action("press_keys", value="enter"),
)


class FakeDesk:
    """Stands in for WindowsActionBackend: a scripted foreground, no input."""

    def __init__(self, image: str = "explorer.exe", title: str = "File Explorer", role: str = "") -> None:
        self.image, self.title, self.role = image, title, role
        self.calls: list[tuple[str, str, str]] = []
        self.task_context = ActionContext()

    def live_context(self, action: Action) -> ActionContext:
        task = self.task_context
        return ActionContext(
            process_image=self.image, window_title=self.title, focused_role=self.role,
            user_words=task.user_words, origin=task.origin, allowlist=task.allowlist,
            tainted=task.tainted,
        )

    def execute(self, action: Action) -> ActionResult:
        self.calls.append((action.kind, action.target, action.value))
        return ActionResult(True, f"ran {action.kind}")


class FakeChat:
    def __init__(self, config=None) -> None:
        pass

    def complete(self, messages, **_kwargs) -> str:
        return "none"  # the router understands nothing: the planner runs


class FakeObserver:
    def prewarm(self) -> None:
        pass

    def observe(self) -> ScreenContext:
        return ScreenContext("Explorer", "Downloads - File Explorer", "a folder", False)


def planner_for(actions):
    class FakePlanner:
        def __init__(self, model, catalog=None) -> None:
            pass

        def plan(self, goal: str, screen: str = "") -> Plan:
            return Plan(goal, tuple(actions))

    return FakePlanner


class CliDoTests(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in _SCRUB:
            os.environ.pop(key, None)
        self.sent: list[str] = []

    def run_do(self, argv, actions, desk: FakeDesk, orb_task_id=None, answer=None):
        def send_task(goal, *args, **kwargs):
            self.sent.append(goal)
            return orb_task_id

        def make_backend(*args, task_context=None, **kwargs):
            if task_context is not None:
                desk.task_context = task_context
            return desk

        def no_input(prompt=""):
            if answer is None:
                raise AssertionError("the CLI asked for y/n")
            return answer

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "is_windows", lambda: True), mock.patch.object(
            cli, "chat_config_from_env", lambda: object()
        ), mock.patch.object(cli, "OpenAICompatibleChat", FakeChat), mock.patch.object(
            cli, "ModelPlanner", planner_for(actions)
        ), mock.patch.object(cli, "_observer", FakeObserver), mock.patch(
            "xenon.windows_actions.WindowsActionBackend", make_backend
        ), mock.patch("xenon.ipc.send_task", send_task), mock.patch(
            "builtins.input", no_input
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    # -- the named tests -------------------------------------------------

    def test_cli_yes_cannot_send(self) -> None:
        desk = FakeDesk()
        code, out, err = self.run_do(["do", "tell mom on whatsapp hi", "--yes"], PLAN_SEND, desk)

        self.assertEqual(code, 1)
        self.assertIn("refused at step 3", out)
        self.assertIn("press_keys 'enter'", out)
        self.assertIn(cli.NEEDS_ORB, err)
        # Refused before anything ran: "hi" was never typed into WhatsApp.
        self.assertEqual(desk.calls, [])

    def test_cli_outward_without_orb_refused(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        code, out, err = self.run_do(
            ["do", "press enter in whatsapp", "--yes"], [Action("press_keys", value="enter")], desk
        )

        self.assertEqual(code, 1)
        self.assertIn(cli.NEEDS_ORB, err)
        self.assertNotIn(cli.SENT_TO_ORB, out)
        self.assertEqual(self.sent, ["press enter in whatsapp"])  # the orb was tried first
        self.assertEqual(desk.calls, [])

    # -- more ------------------------------------------------------------

    def test_y_on_stdin_cannot_send_either(self) -> None:
        desk = FakeDesk()
        code, out, err = self.run_do(["do", "tell mom on whatsapp hi"], PLAN_SEND, desk, answer="y")

        self.assertEqual(code, 1)
        self.assertIn(cli.NEEDS_ORB, err)
        self.assertEqual(desk.calls, [])

    def test_outward_goal_is_handed_to_a_running_orb(self) -> None:
        desk = FakeDesk()
        code, out, err = self.run_do(
            ["do", "tell mom on whatsapp hi", "--yes"], PLAN_SEND, desk, orb_task_id="t42"
        )

        self.assertEqual(code, 0, err)
        self.assertIn(cli.SENT_TO_ORB, out)
        self.assertEqual(self.sent, ["tell mom on whatsapp hi"])
        self.assertEqual(desk.calls, [])  # the orb plans and asks; the CLI ran nothing

    def test_newline_typed_into_comms_is_outward(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        code, _out, err = self.run_do(
            ["do", "say hi in whatsapp", "--yes"], [Action("type_text", value="hi\n")], desk
        )

        self.assertEqual(code, 1)
        self.assertIn(cli.NEEDS_ORB, err)
        self.assertEqual(desk.calls, [])

    def test_denied_step_is_refused_and_never_handed_off(self) -> None:
        desk = FakeDesk("WindowsTerminal.exe", "Windows PowerShell", role="edit")
        code, out, err = self.run_do(
            ["do", "type dir in the terminal", "--yes"], [Action("type_text", value="dir")], desk,
            orb_task_id="t1",
        )

        self.assertEqual(code, 1)
        self.assertIn("refused at step 1", err)
        self.assertEqual(self.sent, [])  # DENIED is never sent to the orb either
        self.assertEqual(desk.calls, [])

    def test_local_plan_still_runs_with_yes(self) -> None:
        desk = FakeDesk("notepad.exe", "Untitled - Notepad", role="document")
        plan = [Action("open_app", "notepad"), Action("type_text", value="hello")]
        code, out, err = self.run_do(["do", "type hello in notepad", "--yes"], plan, desk)

        # No verifier was given, so the exit code says "unverified", but
        # every LOCAL step ran.
        self.assertEqual(code, 1, err)
        self.assertIn("status: unverified", out)
        self.assertEqual([call[0] for call in desk.calls], ["open_app", "type_text"])
        self.assertEqual(self.sent, [])

    def test_runtime_refusal_when_the_foreground_differs(self) -> None:
        # The pre-scan predicted something harmless, but at run time Enter
        # lands in a WhatsApp composer: the approver refuses it there.
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        args = argparse.Namespace(expect_title=None, expect_file=None, expect_text="")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._run_actions("x", [Action("press_keys", value="enter")], args, backend=desk)

        self.assertEqual(code, 1)
        self.assertIn("status: denied", out.getvalue())
        self.assertIn(cli.NEEDS_ORB, err.getvalue())
        self.assertEqual(desk.calls, [])


class CliHelperTests(unittest.TestCase):
    def test_cli_approver_takes_read_and_local_only(self) -> None:
        action = Action("press_keys", value="enter")
        self.assertTrue(cli._cli_approver(action, Risk.READ))
        self.assertTrue(cli._cli_approver(action, Risk.LOCAL))
        for risk in (Risk.OUTWARD, Risk.DESTRUCTIVE, Risk.DENIED):
            with self.subTest(risk=risk):
                self.assertFalse(cli._cli_approver(action, risk))

    def test_unknown_window_is_predicted_strictly(self) -> None:
        desk = FakeDesk()
        plan = [Action("focus_window", "Mom"), Action("type_text", value="hi"), Action("press_keys", value="enter")]
        blocked = cli._preflight("message mom hi", plan, desk, origin="model")
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked[0], 3)
        self.assertEqual(blocked[2], Risk.OUTWARD)

    def test_enter_in_a_named_local_app_passes_the_pre_scan(self) -> None:
        desk = FakeDesk()
        plan = [Action("open_app", "notepad"), Action("type_text", value="hello"), Action("press_keys", value="enter")]
        self.assertIsNone(cli._preflight("type hello in notepad", plan, desk))

    def test_predicted_image(self) -> None:
        self.assertEqual(cli._predicted_image("WhatsApp"), "WhatsApp.Root.exe")
        self.assertEqual(cli._predicted_image("Notes - WhatsApp"), "WhatsApp.Root.exe")
        self.assertEqual(cli._predicted_image("Untitled - Notepad"), "notepad.exe")
        self.assertEqual(cli._predicted_image("Mom"), "")

    def test_outward_intents_count_his_sends(self) -> None:
        self.assertEqual(cli._outward_intents("type hello in notepad"), 0)
        self.assertEqual(cli._outward_intents("tell mom on whatsapp hi"), 1)
        self.assertEqual(cli._outward_intents("press enter in whatsapp"), 1)
        self.assertEqual(cli._outward_intents("send it and then post it"), 2)

    def test_allowlist_holds_the_named_apps_only(self) -> None:
        allowed = cli._allowlist_for("open calculator and type 2+2")
        self.assertIn("CalculatorApp.exe", allowed)
        self.assertIn("calculator", allowed)
        self.assertNotIn("Code.exe", cli._allowlist_for("type code in notepad"))
        self.assertEqual(cli._allowlist_for("what time is it"), frozenset())


if __name__ == "__main__":
    unittest.main()
