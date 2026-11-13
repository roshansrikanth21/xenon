"""The orb's task layer: request routing, the one broker, cards, and the
"do <goal>" body that plans, classifies and commits through gate.py.

No Tk window, no model, no real pipe and no desktop input: the UI is a
recorder, the TaskManager skips COM, the backends are scripted, and the
card classes are replaced where an OrbApp method would build one.
"""
from __future__ import annotations

import os
import threading
import time
import types
import unittest
from unittest import mock

from xenon import __main__ as cli
from xenon import orb
from xenon.actions import Action, ActionContext, ActionResult, Risk
from xenon.approvals import (
    NOT_IN_REQUEST,
    ApprovalBroker,
    AuditLog,
    Field,
    PendingAction,
    RateCaps,
    canonical_digest,
)
from xenon.orb import (
    BUSY_CHOICES,
    OrbTasks,
    TaskServices,
    TaskTools,
    audit_lines,
    card_anchor,
    split_result,
    task_display,
)
from xenon.orb_model import NOTCH_IDLE_S, OrbModel
from xenon.planner import Plan
from xenon.screen import ScreenContext
from xenon.tasks import TaskCancelled, TaskManager, TaskUpdate

_SCRUB = (
    "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY",
    "OPENAI_API_KEY", "XENON_MODEL_API_KEY", "XENON_OUTWARD", "XENON_AGENT",
    "XENON_VOICE_APPROVE", "XENON_UNDO_S",
)


def scrubbed_env(test: unittest.TestCase) -> None:
    patcher = mock.patch.dict(os.environ, {}, clear=False)
    patcher.start()
    test.addCleanup(patcher.stop)
    for key in _SCRUB:
        os.environ.pop(key, None)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def local_card(task_id: str, action_id: str = "a1") -> PendingAction:
    return PendingAction(
        action_id, "local_type_text", Risk.LOCAL, "Type into Notepad",
        (Field("Text", "hello", "your words"),), canonical_digest("x", {"v": 1}), task_id=task_id,
    )


class FakeUi:
    """Records what the orb would show. Calls arrive on the posting thread."""

    def __init__(self) -> None:
        self.cards: dict[str, tuple] = {}
        self.closed_cards: list[str] = []
        self.choices: dict[str, tuple] = {}
        self.closed_choices: list[str] = []
        self.updates: list[tuple[TaskUpdate, str]] = []
        self.stopped = 0
        self.undo_answer: str | None = "elapse"

    def task_card(self, pending, decide) -> None:
        self.cards[pending.action_id] = (pending, decide)

    def task_card_closed(self, action_id: str) -> None:
        self.closed_cards.append(action_id)

    def task_choice(self, prompt_id, title, labels, on_pick, origin="typed") -> None:
        self.choices[prompt_id] = (title, labels, on_pick, origin)

    def task_choice_closed(self, prompt_id: str) -> None:
        self.closed_choices.append(prompt_id)

    def task_update(self, update, origin="typed") -> None:
        self.updates.append((update, origin))

    def task_stopped(self) -> None:
        self.stopped += 1

    def task_undo(self, label, seconds, on_undo, on_elapse) -> None:
        if self.undo_answer == "undo":
            on_undo()
        elif self.undo_answer == "elapse":
            on_elapse()

    def states(self, task_id: str | None = None) -> list[str]:
        return [u.state for u, _o in self.updates if task_id is None or u.task_id == task_id]

    def final(self, task_id: str) -> TaskUpdate | None:
        for update, _origin in reversed(self.updates):
            if update.task_id == task_id and update.state in orb.TERMINAL_TASK_STATES:
                return update
        return None


class FakeIpc:
    instances: list["FakeIpc"] = []

    def __init__(self, submit) -> None:
        self.submit = submit
        self.stopped = False
        FakeIpc.instances.append(self)

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        self.stopped = True


class OrbTasksTests(unittest.TestCase):
    def setUp(self) -> None:
        scrubbed_env(self)
        self.ui = FakeUi()
        self.bodies: dict[str, object] = {}
        self.origins: list[str] = []
        self.agent = (True, "")

        def make_task(goal, origin, tools):
            self.origins.append(origin)
            return self.bodies[goal] if goal in self.bodies else (lambda ctx: f"did {goal}")

        services = TaskServices(
            make_task=make_task,
            is_t0=lambda text: text.lower().startswith("volume"),
            clean=lambda text: text.removeprefix("xenon ").strip(),
        )
        self.tasks = OrbTasks(
            services,
            post=lambda callback: callback(),
            ui=self.ui,
            audit=AuditLog(None),
            manager_factory=lambda **kw: TaskManager(com_init=lambda: None, **kw),
            ipc_factory=FakeIpc,
            agent_check=lambda: self.agent,
        )
        self.addCleanup(self.tasks.close)

    def last_task_id(self) -> str:
        return self.ui.updates[-1][0].task_id

    def test_plain_text_falls_through_and_nothing_starts(self) -> None:
        self.assertIsNone(self.tasks.route("what is on my screen"))
        self.assertIsNone(self.tasks.route("can you do my homework"))
        self.assertIsNone(self.tasks.route("stop"))  # no task: "stop" stays a music command
        self.assertIsNone(self.tasks._manager)  # no TaskManager, no thread

    def test_do_starts_a_task_and_reports_the_result(self) -> None:
        self.assertEqual(self.tasks.route("xenon do tidy my desktop"), "On it.")
        self.assertTrue(wait_for(lambda: "done" in self.ui.states()))
        task_id = self.ui.updates[0][0].task_id
        self.assertEqual(self.ui.states(task_id), ["queued", "running", "done"])
        self.assertEqual(self.ui.final(task_id).text, "did tidy my desktop")
        self.assertEqual(self.origins, ["typed"])

    def test_followup_routed_to_the_waiting_task(self) -> None:
        self.bodies["rename a file"] = lambda ctx: f"renamed {ctx.ask('Which file?')}"
        self.tasks.route("do rename a file")
        self.assertTrue(wait_for(lambda: self.tasks.waiting_for_input))
        self.assertEqual(self.tasks.route("notes.txt"), "Got it.")
        self.assertTrue(wait_for(lambda: "done" in self.ui.states()))
        self.assertEqual(self.ui.updates[-1][0].text, "renamed notes.txt")

    def test_t0_command_and_stop_escape_the_focus(self) -> None:
        self.bodies["rename a file"] = lambda ctx: f"renamed {ctx.ask('Which file?')}"
        self.tasks.route("do rename a file")
        self.assertTrue(wait_for(lambda: self.tasks.waiting_for_input))
        self.assertIsNone(self.tasks.route("volume up"))  # runs as a command, not an answer
        self.assertTrue(self.tasks.waiting_for_input)
        self.assertEqual(self.tasks.route("stop"), "Stopped.")
        self.assertTrue(wait_for(lambda: "cancelled" in self.ui.states()))
        self.assertEqual(self.ui.stopped, 1)

    def test_card_is_shown_on_the_ui_and_a_click_approves(self) -> None:
        tokens = []

        def body(ctx):
            tokens.append(self.tasks.broker.request(local_card(ctx.task_id), cancel=ctx.cancel))
            return "approved" if tokens[-1] is not None else "denied"

        self.bodies["type hello"] = body
        self.tasks.route("do type hello")
        self.assertTrue(wait_for(lambda: "a1" in self.ui.cards))
        _pending, decide = self.ui.cards["a1"]
        decide("approve")  # what ConfirmCard calls after a real, armed click
        self.assertTrue(wait_for(lambda: "done" in self.ui.states()))
        self.assertIsNotNone(tokens[0])
        self.assertEqual(tokens[0].via, "click")
        self.assertIn("a1", self.ui.closed_cards)  # on_settled closes the card

    def test_stop_cancels_the_task_and_denies_its_card(self) -> None:
        tokens = []

        def body(ctx):
            tokens.append(self.tasks.broker.request(local_card(ctx.task_id), cancel=ctx.cancel))
            ctx.check()
            return "should not get here"

        self.bodies["type hello"] = body
        self.tasks.route("do type hello")
        self.assertTrue(wait_for(lambda: "a1" in self.ui.cards))
        self.assertTrue(self.tasks.has_task())
        self.assertEqual(self.tasks.route("stop"), "Stopped.")
        self.assertTrue(wait_for(lambda: "cancelled" in self.ui.states()))
        self.assertEqual(tokens, [None])
        self.assertEqual(self.tasks.broker.pending(), [])
        self.assertIn("a1", self.ui.closed_cards)

    def test_nothing_typed_or_spoken_approves_a_card(self) -> None:
        tokens = []

        def body(ctx):
            tokens.append(self.tasks.broker.request(local_card(ctx.task_id), cancel=ctx.cancel))
            return "finished"

        self.bodies["type hello"] = body
        self.tasks.route("do type hello", "voice")
        self.assertTrue(wait_for(lambda: "a1" in self.ui.cards))
        for text, origin in (("yes", "voice"), ("confirm", "typed"), ("send it", "voice-focus"),
                             ("approve", "voice"), ("send 47", "voice")):
            with self.subTest(text=text, origin=origin):
                reply = self.tasks.route(text, origin)
                self.assertNotIn(reply, {"Got it.", "OK."})
                self.assertEqual(len(self.tasks.broker.pending()), 1)
        self.assertEqual(tokens, [])  # still waiting for a click
        self.tasks.stop()
        self.assertTrue(wait_for(lambda: tokens == [None]))

    def test_second_task_asks_queue_replace_or_cancel(self) -> None:
        release = threading.Event()
        self.bodies["first job"] = lambda ctx: (release.wait(3), "first done")[1]
        self.tasks.route("do first job")
        self.assertTrue(wait_for(lambda: self.tasks.busy))
        # The card asks (and, for voice, the question is read out), so the
        # reply itself says nothing more.
        self.assertEqual(self.tasks.route("do second job", "voice"), "")
        self.assertEqual(len(self.ui.choices), 1)
        prompt_id, (title, labels, _on_pick, origin) = next(iter(self.ui.choices.items()))
        self.assertEqual(labels, list(BUSY_CHOICES))
        self.assertIn("first job", title)
        self.assertEqual(origin, "voice")
        self.assertEqual(self.tasks.route("queue it"), "OK.")
        self.assertIn(prompt_id, self.ui.closed_choices)
        release.set()
        self.assertTrue(wait_for(lambda: self.ui.states().count("done") == 2))
        texts = [u.text for u, _o in self.ui.updates if u.state == "done"]
        self.assertEqual(texts, ["first done", "did second job"])

    def test_cancel_on_the_choice_drops_only_the_new_task(self) -> None:
        release = threading.Event()
        self.bodies["first job"] = lambda ctx: (release.wait(3), "first done")[1]
        self.tasks.route("do first job")
        self.assertTrue(wait_for(lambda: self.tasks.busy))
        self.tasks.route("do second job")
        self.assertEqual(self.tasks.route("never mind"), "OK.")
        self.assertIn(("cancelled", "not started"),
                      [(u.state, u.text) for u, _o in self.ui.updates])
        self.assertTrue(self.tasks.busy)  # the first task carries on
        release.set()
        self.assertTrue(wait_for(lambda: "done" in self.ui.states()))

    def test_wakeless_utterance_never_starts_a_task(self) -> None:
        self.assertEqual(self.tasks.route("do open notepad", "voice-focus"), "")
        self.assertEqual(self.tasks.route("what time is it", "voice-focus"), "")
        self.assertIsNone(self.tasks._manager)

    def test_ambient_speech_never_starts_a_task(self) -> None:
        # Always-listening mode, or the wake word late in the sentence: a
        # video saying "do tell mom ..." must not plan anything.
        reply = self.tasks.route("do tell mom on whatsapp I'm not coming home", orb.VOICE_AMBIENT)
        self.assertEqual(reply, orb.VOICE_TASK_REFUSAL)
        self.assertIsNone(self.tasks._manager)
        self.assertEqual(self.origins, [])
        # Other speech still falls through to the ordinary paths.
        self.assertIsNone(self.tasks.route("what time is it", orb.VOICE_AMBIENT))

    def test_only_one_busy_choice_card_at_a_time(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        self.bodies["first job"] = lambda ctx: (release.wait(3), "first done")[1]
        self.tasks.route("do first job")
        self.assertTrue(wait_for(lambda: self.tasks.busy))
        self.tasks.submit_from_ipc("second job")
        self.tasks.submit_from_ipc("third job")
        self.tasks.submit_from_ipc("fourth job")
        self.assertEqual(len(self.ui.choices), 1)  # never stacked
        cancelled = [u for u, _o in self.ui.updates if u.state == "cancelled"]
        self.assertEqual(len(cancelled), 2)

    def test_ipc_goals_are_always_ipc_origin(self) -> None:
        self.assertTrue(self.tasks.start_ipc())
        server = FakeIpc.instances[-1]
        task_id = server.submit("tidy my desktop", "typed")  # the caller's claim is ignored
        self.assertTrue(task_id)
        self.assertTrue(wait_for(lambda: "done" in self.ui.states()))
        self.assertEqual(self.origins, ["ipc"])
        self.tasks.close()
        self.assertTrue(server.stopped)

    def test_agent_switch_off_refuses_do(self) -> None:
        self.agent = (False, "agent features are off (XENON_AGENT=off)")
        self.assertEqual(self.tasks.route("do tidy my desktop"),
                         "I can't do that: agent features are off (XENON_AGENT=off).")
        self.assertIsNone(self.tasks._manager)

    def test_undo_strip_decides_and_stop_undoes(self) -> None:
        cancel = threading.Event()
        self.ui.undo_answer = "elapse"
        self.assertTrue(self.tasks._undo("Sending", 0.5, cancel))
        self.ui.undo_answer = "undo"
        self.assertFalse(self.tasks._undo("Sending", 0.5, cancel))
        self.ui.undo_answer = None  # the strip never answers: no send
        cancel.set()
        self.assertFalse(self.tasks._undo("Sending", 0.5, cancel))


# ---------------------------------------------------------------------------
# The "do <goal>" body (__main__._desktop_task)
# ---------------------------------------------------------------------------


class FakeTask:
    def __init__(self) -> None:
        self.task_id = "t1"
        self.cancel = threading.Event()
        self.progress_lines: list[str] = []

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def progress(self, text, step=None, total=None) -> None:
        self.progress_lines.append(text)

    def check(self) -> None:
        if self.cancel.is_set():
            raise TaskCancelled("stopped")


class FakeDesk:
    """The ordinary backend: a scripted foreground; records what it ran."""

    def __init__(self, image: str, title: str, role: str = "document") -> None:
        self.image, self.title, self.role = image, title, role
        self.calls: list[tuple[str, str]] = []
        self.task_context = ActionContext()
        # What the focused composer reads back (chat name, text); None text
        # means it could not be read.
        self.chat, self.composer = "Type a message to Mom", "hi"
        self.expected = None

    def composer_readback(self, action=None) -> dict:
        return {"chat": self.chat, "role": self.role, "text": self.composer}

    def expect_context(self, check) -> None:
        self.expected = check

    def live_context(self, action: Action) -> ActionContext:
        task = self.task_context
        return ActionContext(
            process_image=self.image, window_title=self.title, focused_role=self.role,
            tainted=task.tainted, user_words=task.user_words, allowlist=task.allowlist,
            origin=task.origin,
        )

    def execute(self, action: Action) -> ActionResult:
        check, self.expected = self.expected, None
        if check is not None:
            changed = check(self.live_context(action))
            if changed:
                return ActionResult(False, f"refused: {changed}")
        self.calls.append((action.kind, action.value or action.target))
        return ActionResult(True, f"ran {action.kind}")


class DesktopTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        scrubbed_env(self)
        self.shown: list[PendingAction] = []
        self.decision = "approve"
        self.undo_answer = True
        self.commits: list[dict] = []
        self.audit = AuditLog(None)

        def present(pending):
            self.shown.append(pending)
            if self.decision:
                self.broker.resolve(pending.action_id, self.decision, via="click")

        self.caps = RateCaps()
        self.broker = ApprovalBroker(presenter=present, audit=self.audit, caps=self.caps)
        self.tools = TaskTools(self.broker, self.caps, lambda label, s, cancel: self.undo_hook(), "t1")
        self.task = FakeTask()

    def undo_hook(self) -> bool:
        return self.undo_answer() if callable(self.undo_answer) else self.undo_answer

    def run_goal(self, goal, actions, desk, origin="typed", agent=(True, ""), sweep=None):
        def backend_factory(task_context):
            desk.task_context = task_context
            return desk

        class Planner:
            calls = 0

            def plan(self, goal, screen=""):
                Planner.calls += 1
                return Plan(goal, tuple(actions))

        def commit_factory(**kwargs):
            outer = self

            class Committer:
                def execute(self, action):
                    outer.commits.append({"action": action, **kwargs})
                    return ActionResult(True, "committed")

            return Committer()

        self.planner = Planner
        body = cli._desktop_task(
            goal, origin, self.tools, None,
            planner=Planner(),
            backend_factory=backend_factory,
            observe=lambda: ScreenContext("Notepad", "Untitled - Notepad", "", False),
            commit_backend_factory=commit_factory,
            agent_check=lambda: agent,
            sweep=sweep,
        )
        return body(self.task)

    def test_type_in_notepad_shows_one_keyboard_card_with_his_words(self) -> None:
        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        result = self.run_goal("type hello in notepad",
                               [Action("open_app", "notepad"), Action("type_text", value="hello")], desk)

        self.assertTrue(result.startswith("Done"), result)
        self.assertEqual(desk.calls, [("open_app", "notepad"), ("type_text", "hello")])
        self.assertEqual(len(self.shown), 1)  # the keyboard card, once per task
        card = self.shown[0]
        self.assertEqual(card.risk, Risk.LOCAL)
        fields = {f.label: f for f in card.fields}
        self.assertIn("Notepad", fields["App"].value)
        self.assertEqual(fields["App"].source, "xenon")
        self.assertEqual((fields["Text"].value, fields["Text"].source), ("hello", "your words"))
        self.assertFalse(card.voice_ok)
        self.assertIn("Step 1/2 · Open notepad", self.task.progress_lines)
        self.assertIn("Step 2/2 · Type text", self.task.progress_lines)
        self.assertEqual(self.commits, [])

    def test_model_text_is_labelled_drafter(self) -> None:
        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        self.run_goal("write a greeting in notepad", [Action("type_text", value="Dear sir")], desk)
        fields = {f.label: f for f in self.shown[0].fields}
        self.assertEqual(fields["Text"].source, "drafter")

    def test_denied_card_stops_before_typing(self) -> None:
        self.decision = "deny"
        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        result = self.run_goal("type hello in notepad",
                               [Action("open_app", "notepad"), Action("type_text", value="hello")], desk)

        self.assertTrue(result.startswith("Stopped:"), result)
        self.assertEqual(desk.calls, [("open_app", "notepad")])

    def test_outward_step_commits_only_through_the_gate(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        result = self.run_goal("press enter in whatsapp", [Action("press_keys", value="enter")], desk)

        self.assertTrue(result.startswith("Done"), result)
        self.assertEqual(desk.calls, [])  # the ordinary backend never pressed Enter
        self.assertEqual(len(self.commits), 1)
        self.assertTrue(self.commits[0].get("allow_outward"))
        card = self.shown[0]
        self.assertEqual((card.risk, card.kind), (Risk.OUTWARD, "desktop_commit"))
        events = [row["event"] for row in self.audit.rows()]
        self.assertIn("approval_granted", events)
        self.assertIn("committed", events)

    def test_changed_target_refuses_the_commit(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")

        def retitle() -> bool:
            desk.title = "Amma - WhatsApp"  # a different chat came forward
            return True

        self.undo_answer = retitle
        result = self.run_goal("press enter in whatsapp", [Action("press_keys", value="enter")], desk)

        self.assertIn("changed since you approved", result)
        self.assertEqual(self.commits, [])
        self.assertEqual(desk.calls, [])

    def test_undo_means_nothing_is_sent(self) -> None:
        self.undo_answer = False
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        result = self.run_goal("press enter in whatsapp", [Action("press_keys", value="enter")], desk)

        self.assertIn("nothing was sent", result)
        self.assertEqual(self.commits, [])

    def test_outward_step_beyond_his_request_is_refused_without_a_card(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        result = self.run_goal(
            "type hi in whatsapp",
            [Action("type_text", value="hi"), Action("press_keys", value="enter")],
            desk,
        )

        self.assertTrue(result.startswith("Stopped:"), result)
        self.assertEqual([card.risk for card in self.shown], [Risk.LOCAL])  # no card for Enter
        self.assertNotIn(("press_keys", "enter"), desk.calls)
        self.assertEqual(self.commits, [])

    def test_ipc_goal_is_never_labelled_his_words(self) -> None:
        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        self.run_goal("type hello in notepad", [Action("type_text", value="hello")], desk, origin="ipc")
        card = self.shown[0]
        fields = {f.label: f for f in card.fields}
        self.assertEqual(fields["Text"].source, "drafter")
        self.assertIn(cli.IPC_BADGE, card.badges)

    def test_unrequested_outward_text_gets_the_badge(self) -> None:
        self.caps.start_task = lambda task_id, n: RateCaps.start_task(self.caps, task_id, 1)
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        self.run_goal("send it", [Action("type_text", value="buy crypto\n")], desk)
        self.assertEqual(self.shown[0].risk, Risk.OUTWARD)
        self.assertIn(NOT_IN_REQUEST, self.shown[0].badges)

    def test_agent_switch_refuses_before_planning(self) -> None:
        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        result = self.run_goal("type hello in notepad", [Action("type_text", value="hello")], desk,
                               agent=(False, "Xenon is running elevated"))

        self.assertEqual(result, "I can't do that: Xenon is running elevated.")
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(desk.calls, [])

    def test_stop_while_a_card_is_up_cancels_the_task(self) -> None:
        self.decision = ""  # nobody clicks

        def present(pending):
            self.shown.append(pending)
            self.task.cancel.set()  # Ctrl+Alt+Esc while the card is up

        self.broker._presenter = present
        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        with self.assertRaises(TaskCancelled):
            self.run_goal("type hello in notepad", [Action("type_text", value="hello")], desk)
        self.assertEqual(desk.calls, [])

    def test_sweep_runs_and_its_failure_is_ignored(self) -> None:
        calls = []

        def sweep():
            calls.append(1)
            raise OSError("locked")

        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        self.run_goal("type hello in notepad", [Action("type_text", value="hello")], desk, sweep=sweep)
        self.assertEqual(calls, [1])

    # -- review fixes: OUTWARD cards bound to the chat and composer text -------

    def test_enter_card_shows_the_chat_and_the_composer_text(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        desk.chat, desk.composer = "Type a message to College Group", "I'll be late (draft: and the pin)"
        result = self.run_goal("press enter in whatsapp", [Action("press_keys", value="enter")], desk)

        self.assertTrue(result.startswith("Done"), result)
        card = self.shown[0]
        fields = {f.label: f for f in card.fields}
        self.assertEqual(fields["Chat"].value, "Type a message to College Group")
        self.assertEqual(fields["Sends"].value, "I'll be late (draft: and the pin)")
        self.assertEqual(fields["Chat"].source, "page")
        self.assertEqual(fields["Keys"].source, "xenon")
        # A key name is never "in his words": no badge that trains him to ignore it.
        self.assertNotIn(NOT_IN_REQUEST, card.badges)

    def test_composer_text_is_in_the_digest(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")

        def more_text() -> bool:
            desk.composer = "hi and a saved draft"  # the composer grew after the card
            return True

        self.undo_answer = more_text
        result = self.run_goal("press enter in whatsapp", [Action("press_keys", value="enter")], desk)

        self.assertIn("changed since you approved", result)
        self.assertEqual(self.commits, [])

    def test_other_chat_after_the_card_is_refused(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")

        def other_chat() -> bool:
            desk.chat = "Type a message to Boss"
            return True

        self.undo_answer = other_chat
        result = self.run_goal("press enter in whatsapp", [Action("press_keys", value="enter")], desk)
        self.assertIn("changed since you approved", result)
        self.assertEqual(self.commits, [])

    def test_unreadable_composer_refuses_without_a_card(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        desk.composer = None
        result = self.run_goal("press enter in whatsapp", [Action("press_keys", value="enter")], desk)
        self.assertIn("could not read back the message box", result)
        self.assertNotIn("declined", result)
        self.assertEqual(self.shown, [])
        self.assertEqual(self.commits, [])

    def test_unreadable_chat_name_in_a_messenger_refuses(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        desk.chat = ""
        result = self.run_goal("press enter in whatsapp", [Action("press_keys", value="enter")], desk)
        self.assertIn("which chat", result)
        self.assertEqual(self.commits, [])

    def test_commit_executor_gets_a_check_against_the_approved_digest(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        self.run_goal("press enter in whatsapp", [Action("press_keys", value="enter")], desk)
        check = self.commits[0]["context_check"]
        same = desk.live_context(Action("press_keys", value="enter"))
        self.assertIsNone(check(same))
        moved = ActionContext(process_image="Telegram.exe", window_title="Telegram", focused_role="edit")
        self.assertIn("changed", check(moved))

    def test_carded_local_step_only_runs_in_the_window_the_card_showed(self) -> None:
        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        original = desk.execute

        def switch_then_run(action):
            desk.image, desk.title = "chrome.exe", "New Tab - Google Chrome"  # he clicked into Chrome
            return original(action)

        desk.execute = switch_then_run
        result = self.run_goal("type hello in notepad", [Action("type_text", value="hello")], desk)
        self.assertIn("window changed since you approved", result)
        self.assertEqual(desk.calls, [])

    def test_send_beyond_the_task_cap_says_refused_not_declined(self) -> None:
        desk = FakeDesk("WhatsApp.Root.exe", "WhatsApp", role="edit")
        result = self.run_goal("type hi in whatsapp", [Action("press_keys", value="enter")], desk)
        self.assertIn("more outward actions than you asked for", result)
        self.assertNotIn("declined", result)

    def test_common_message_phrasings_count_as_sends(self) -> None:
        for goal in ("say hi to mom on whatsapp", "text mom hi on whatsapp", "whatsapp mom that I am late",
                     "reply to priya on telegram"):
            with self.subTest(goal=goal):
                self.assertEqual(cli._outward_intents(goal), 1)
        self.assertEqual(cli._outward_intents("open whatsapp and read my chats"), 0)

    def test_voice_text_is_labelled_heard_not_your_words(self) -> None:
        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        self.run_goal("type hello in notepad", [Action("type_text", value="hello")], desk, origin="voice")
        fields = {f.label: f for f in self.shown[0].fields}
        self.assertEqual(fields["Text"].source, "heard")

    def test_invisible_characters_get_a_red_badge_and_bidi_is_refused(self) -> None:
        from xenon.orb_cards import BADGE_RED, badge_color

        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        self.run_goal("type hello in notepad", [Action("type_text", value="hel" + chr(0x200B) + "lo")], desk)
        self.assertIn(cli.INVISIBLE_BADGE, self.shown[0].badges)
        self.assertEqual(badge_color(cli.INVISIBLE_BADGE), BADGE_RED)

        self.shown.clear()
        desk = FakeDesk("notepad.exe", "Untitled - Notepad")
        rlo, pdf = chr(0x202E), chr(0x202C)
        result = self.run_goal("type it in notepad",
                               [Action("type_text", value=f"transfer {rlo}001{pdf} rupees")], desk)
        self.assertIn("hidden or direction-changing", result)
        self.assertEqual(desk.calls, [])


# ---------------------------------------------------------------------------
# What the orb shows (OrbApp methods on a stand-in, no Tk)
# ---------------------------------------------------------------------------


class FakePanel:
    chat_mode = False
    visible = False

    def __init__(self) -> None:
        self.text: list[str] = []
        self.header = ""

    def set_chat(self, *args) -> None:
        pass

    def clear(self) -> None:
        self.text = []

    def set_header(self, title, note="", accent="") -> None:
        self.header = title

    def append(self, text, tag=None) -> None:
        self.text.append(text)


class FakeApp:
    task_update = orb.OrbApp.task_update
    task_card = orb.OrbApp.task_card
    task_card_closed = orb.OrbApp.task_card_closed
    task_choice = orb.OrbApp.task_choice
    task_choice_closed = orb.OrbApp.task_choice_closed
    _settle_after_card = orb.OrbApp._settle_after_card
    _rest_state = orb.OrbApp._rest_state
    _show_task_panel = orb.OrbApp._show_task_panel
    stop_task = orb.OrbApp.stop_task
    companion_state = orb.OrbApp.companion_state
    _voice_announce = orb.OrbApp._voice_announce

    def __init__(self, busy: bool = True) -> None:
        self.model = OrbModel(notch=True)
        self.model.idle_after = NOTCH_IDLE_S
        self.notch = True
        self.root = None
        self.tasks = types.SimpleNamespace(
            busy=busy, origin_of=lambda task_id: "typed",
            has_task=lambda: self.tasks.busy, stop=lambda reason="": self.stops.append(reason),
        )
        self.stops: list[str] = []
        self._cards: dict = {}
        self._choice_cards: dict = {}
        self._busy = False
        self._task_text = ""
        self._answer_placeholder = ""
        self._voice_say = None
        self._history: list = []
        self._history_private = False
        self._timers = types.SimpleNamespace(active=lambda: [])
        self.panel = FakePanel()

    def _notch_info(self) -> dict:
        return {"task": self._task_text} if self._task_text else {}

    def _task_anchor(self):
        return (0, 0)

    def _place_panel(self) -> None:
        pass

    def _keep_focus(self, previous) -> None:
        pass

    def advance(self, seconds: float) -> None:
        for _ in range(int(seconds / 0.1) + 1):
            self.model.step(0.1)


class FakeCard:
    def __init__(self, *args, **kwargs) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class OrbViewTests(unittest.TestCase):
    def running(self, text="Step 1/2 · Open notepad", step=1, total=2) -> TaskUpdate:
        return TaskUpdate("t1", "type hello in notepad", "running", text, step, total)

    def test_task_progress_keeps_the_notch_folded(self) -> None:
        app = FakeApp()
        app.task_update(self.running())
        self.assertEqual(app.model.state, "working")
        self.assertEqual(app.model.notch_info["task"], "Step 1/2 · Open notepad")
        self.assertEqual(app.model.progress_fraction, 0.5)
        app.advance(NOTCH_IDLE_S + 0.5)
        self.assertTrue(app.model.is_idle)  # working is quiet in the notch

    def test_confirm_card_unfolds_and_never_folds_while_up(self) -> None:
        app = FakeApp()
        app.task_update(self.running())
        pending = local_card("t1")
        with mock.patch("xenon.orb_cards.ConfirmCard", FakeCard):
            app.task_card(pending, lambda decision: None)
        self.assertEqual(app.model.state, "confirm")
        self.assertEqual(app.model.cards_open, 1)
        app.advance(10)
        self.assertFalse(app.model.is_idle)
        app.task_card_closed(pending.action_id)
        self.assertEqual(app.model.cards_open, 0)
        self.assertEqual(app.model.state, "working")  # the task still runs

    def test_folds_after_the_task_is_done_and_the_card_closed(self) -> None:
        app = FakeApp()
        app.task_update(self.running())
        with mock.patch("xenon.orb_cards.ConfirmCard", FakeCard):
            app.task_card(local_card("t1"), lambda decision: None)
        app.task_card_closed("a1")
        app.tasks.busy = False
        app.task_update(TaskUpdate("t1", "type hello in notepad", "done", "Done. (2 steps · 1.0s)"))
        self.assertEqual(app.model.state, "idle")
        self.assertEqual(app._task_text, "")
        self.assertEqual(app.panel.header, "type hello in notepad")
        self.assertEqual(app.panel.text, ["Done."])
        app.advance(NOTCH_IDLE_S + 0.5)
        self.assertTrue(app.model.is_idle)

    def test_a_card_that_cannot_be_shown_is_a_deny(self) -> None:
        app = FakeApp()
        decisions = []

        def broken(*args, **kwargs):
            raise RuntimeError("no Tk")

        with mock.patch("xenon.orb_cards.ConfirmCard", broken), mock.patch("sys.stderr"):
            app.task_card(local_card("t1"), decisions.append)
        self.assertEqual(decisions, ["deny"])
        self.assertEqual(app._cards, {})

    def test_stop_task_does_nothing_when_idle(self) -> None:
        app = FakeApp(busy=False)
        app.stop_task()
        self.assertEqual(app.stops, [])
        app.tasks.busy = True
        app.stop_task()
        self.assertEqual(app.stops, ["stopped by you"])

    def test_companion_never_shows_task_or_private_text(self) -> None:
        app = FakeApp()
        app.task_update(self.running())
        state = app.companion_state()
        self.assertEqual(state["detail"], "Private task")
        app.tasks.busy = False
        app.task_update(TaskUpdate("t1", "type hello", "done", "Done. Typed hello (1 step · 1s)"))
        state = app.companion_state()
        self.assertEqual(state["last"], "Private")
        self.assertTrue(state["private"])
        self.assertNotIn("hello", str(state))

    def test_voice_task_question_is_queued_for_the_voice_thread(self) -> None:
        import queue

        app = FakeApp()
        app.model.voice_on = True
        app._voice_say = queue.Queue()
        app.task_update(TaskUpdate("t1", "rename", "waiting", "Which file?"), "voice")
        self.assertEqual(app._voice_say.get_nowait(), ("ask", "Which file?"))
        self.assertEqual(app._answer_placeholder, "your answer")


class PureHelperTests(unittest.TestCase):
    def test_task_display(self) -> None:
        running = task_display(TaskUpdate("t", "x", "running", "Open notepad", 2, 5))
        self.assertEqual((running.notch, running.state), ("Step 2/5 · Open notepad", "working"))
        self.assertIsNone(running.speak)  # progress is never spoken
        done_typed = task_display(TaskUpdate("t", "x", "done", "Done. (1 step · 1.0s)"))
        self.assertIsNone(done_typed.speak)
        self.assertTrue(done_typed.history)
        done_voice = task_display(TaskUpdate("t", "x", "done", "Done. (1 step · 1.0s)"), "voice")
        self.assertEqual(done_voice.speak, ("say", "Done."))
        declined = task_display(TaskUpdate("t", "x", "cancelled", "not started"))
        self.assertEqual((declined.title, declined.state), ("", "rest"))

    def test_split_result(self) -> None:
        self.assertEqual(split_result("Done. (2 steps · 3.1s)"), ("Done.", "2 steps · 3.1s"))
        self.assertEqual(split_result("No note here"), ("No note here", ""))

    def test_card_anchor_follows_the_target_monitor(self) -> None:
        primary = (0, 0, 1920, 1040)
        second = (1920, 0, 3840, 1040)
        under_notch = card_anchor((800, -100, 320), 1.0, True, primary, primary)
        self.assertEqual(under_notch, (960, 224))
        elsewhere = card_anchor((800, -100, 320), 1.0, True, primary, second)
        self.assertEqual(elsewhere, (2880, 56))
        stacked = card_anchor((800, -100, 320), 1.0, True, primary, primary, index=1)
        self.assertEqual(stacked[0], 960 + orb.CARD_STACK_PX)

    def test_audit_lines_never_show_bodies(self) -> None:
        rows = [{"ts": 0, "event": "approval_granted", "kind": "desktop_commit", "risk": "outward",
                 "via": "click", "result": "approved", "digest": "abcdef0123456789",
                 "body": "secret message text"}]
        lines = audit_lines(rows)
        self.assertEqual(len(lines), 1)
        self.assertIn("#abcdef01", lines[0])
        self.assertNotIn("secret", lines[0])
        self.assertNotIn("0123456789", lines[0])


if __name__ == "__main__":
    unittest.main()
