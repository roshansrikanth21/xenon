from __future__ import annotations

import subprocess
import sys
import threading
import time
import unittest

from xenon.tasks import JobObject, TaskCancelled, TaskManager, TaskUpdate, match_choice

PYTHON = "D:/xenon/.venv/Scripts/python.exe"


class Recorder:
    """Collects TaskUpdates and lets a test wait for a given state."""

    def __init__(self) -> None:
        self.updates: list[TaskUpdate] = []
        self._cond = threading.Condition()

    def __call__(self, update: TaskUpdate) -> None:
        with self._cond:
            self.updates.append(update)
            self._cond.notify_all()

    def wait_for(self, state: str, task_id: str | None = None, timeout: float = 5.0) -> TaskUpdate:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for update in self.updates:
                    if update.state == state and (task_id is None or update.task_id == task_id):
                        return update
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no {state!r} update; got {[u.state for u in self.updates]}")
                self._cond.wait(left)

    def states(self, task_id: str) -> list[str]:
        with self._cond:
            return [u.state for u in self.updates if u.task_id == task_id]


def _manager(recorder: Recorder, **kwargs) -> TaskManager:
    return TaskManager(on_update=recorder, com_init=lambda: None, **kwargs)


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        time.sleep(0.01)


class TaskManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = Recorder()
        self.manager = _manager(self.recorder)

    def tearDown(self) -> None:
        self.manager.shutdown()

    def test_no_thread_until_first_submit(self) -> None:
        before = {t.name for t in threading.enumerate()}
        manager = _manager(Recorder())
        self.assertIsNone(manager._thread)
        self.assertNotIn("xenon-tasks", {t.name for t in threading.enumerate()} - before)
        self.assertFalse(manager.busy)
        manager.shutdown()

    def test_runs_to_done_with_progress(self) -> None:
        def body(ctx) -> str:
            ctx.progress("step one", step=1, total=2)
            return "all good"

        task_id = self.manager.submit("demo", body)
        done = self.recorder.wait_for("done", task_id)
        self.assertEqual(done.text, "all good")
        states = self.recorder.states(task_id)
        self.assertEqual(states[0], "queued")
        self.assertIn("running", states)
        progress = [u for u in self.recorder.updates if u.text == "step one"]
        self.assertEqual((progress[0].step, progress[0].total), (1, 2))

    def test_exception_marks_failed_and_thread_survives(self) -> None:
        def boom(ctx) -> str:
            raise ValueError("bad plan")

        first = self.manager.submit("broken", boom)
        failed = self.recorder.wait_for("failed", first)
        self.assertIn("bad plan", failed.text)
        second = self.manager.submit("after", lambda ctx: "fine")
        self.assertEqual(self.recorder.wait_for("done", second).text, "fine")

    def test_fifo_queue_when_busy_without_chooser(self) -> None:
        release = threading.Event()
        order: list[str] = []

        def first(ctx) -> str:
            release.wait(5)
            order.append("first")
            return ""

        def second(ctx) -> str:
            order.append("second")
            return ""

        a = self.manager.submit("a", first)
        b = self.manager.submit("b", second)
        self.assertTrue(self.manager.busy)
        release.set()
        self.recorder.wait_for("done", b)
        self.recorder.wait_for("done", a)
        self.assertEqual(order, ["first", "second"])

    def test_busy_choice_queue_replace_cancel(self) -> None:
        asked: list[str] = []
        choice = {"value": "cancel"}

        def chooser(title, decide) -> None:
            asked.append(title)
            decide(choice["value"])

        recorder = Recorder()
        manager = _manager(recorder, on_busy_choice=chooser)
        try:
            hold = threading.Event()
            running = manager.submit("long", lambda ctx: (hold.wait(5), "")[1])
            recorder.wait_for("running", running)

            dropped = manager.submit("dropped", lambda ctx: "never")
            self.assertEqual(recorder.wait_for("cancelled", dropped).text, "not started")

            choice["value"] = "replace"
            replacement = manager.submit("replacement", lambda ctx: "replaced")
            hold.set()
            self.assertEqual(recorder.wait_for("done", replacement).text, "replaced")
            self.assertEqual(recorder.wait_for("cancelled", running).task_id, running)
            self.assertEqual(asked, ["dropped", "replacement"])
        finally:
            manager.shutdown()

    def test_cancel_sets_event_and_reports_cancelled(self) -> None:
        def body(ctx) -> str:
            ctx.cancel.wait(5)
            ctx.check()
            return "should not finish"

        task_id = self.manager.submit("cancel me", body)
        self.recorder.wait_for("running", task_id)
        self.assertTrue(self.manager.cancel(task_id))
        self.recorder.wait_for("cancelled", task_id)
        self.assertFalse(self.manager.cancel(task_id))

    def test_cancel_wakes_ask(self) -> None:
        answers: list[object] = []

        def body(ctx) -> str:
            answers.append(ctx.ask("Which file?", timeout_s=10))
            return ""

        task_id = self.manager.submit("asker", body)
        _wait_until(lambda: self.manager.waiting_for_input)
        started = time.monotonic()
        self.manager.cancel()
        self.recorder.wait_for("cancelled", task_id)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(answers, [None])

    def test_followup_routed_to_focused_task(self) -> None:
        answers: list[object] = []

        def body(ctx) -> str:
            answer = ctx.ask("Which file should I attach?")
            answers.append(answer)
            return f"attached {answer}"

        self.assertFalse(self.manager.offer_input("nobody is asking"))
        task_id = self.manager.submit("attach", body)
        _wait_until(lambda: self.manager.waiting_for_input)
        waiting = self.recorder.wait_for("waiting", task_id)
        self.assertIn("Which file", waiting.text)
        self.assertEqual(self.manager.current_title, "attach")
        self.assertTrue(self.manager.offer_input("report.pdf"))
        done = self.recorder.wait_for("done", task_id)
        self.assertEqual(done.text, "attached report.pdf")
        self.assertEqual(answers, ["report.pdf"])
        self.assertFalse(self.manager.offer_input("late follow-up"))
        self.assertFalse(self.manager.waiting_for_input)

    def test_ask_times_out_to_none(self) -> None:
        answers: list[object] = []

        def body(ctx) -> str:
            answers.append(ctx.ask("Anyone?", timeout_s=0.1))
            return ""

        task_id = self.manager.submit("timeout", body)
        self.recorder.wait_for("done", task_id)
        self.assertEqual(answers, [None])

    def test_choice_by_voice_ordinal(self) -> None:
        answers: list[object] = []

        def body(ctx) -> str:
            answers.append(ctx.ask("Which browser?", choices=["Chrome", "Edge", "Firefox"]))
            return ""

        task_id = self.manager.submit("pick", body)
        _wait_until(lambda: self.manager.waiting_for_input)
        self.assertTrue(self.manager.offer_input("the second one"))
        self.recorder.wait_for("done", task_id)
        self.assertEqual(answers, ["Edge"])

    def test_unmatched_choice_reasks_once_then_none(self) -> None:
        answers: list[object] = []

        def body(ctx) -> str:
            answers.append(ctx.ask("Which browser?", choices=["Chrome", "Edge"]))
            return ""

        task_id = self.manager.submit("pick", body)
        _wait_until(lambda: self.manager.waiting_for_input)
        self.assertTrue(self.manager.offer_input("banana"))
        # The task asks again; wait for the second waiting update.
        _wait_until(lambda: self.recorder.states(task_id).count("waiting") == 2)
        _wait_until(lambda: self.manager.waiting_for_input)
        reask = [u for u in self.recorder.updates if u.state == "waiting"][-1]
        self.assertIn("Sorry", reask.text)
        self.assertTrue(self.manager.offer_input("the fifth one"))
        self.recorder.wait_for("done", task_id)
        self.assertEqual(answers, [None])

    def test_shutdown_refuses_new_tasks(self) -> None:
        self.manager.shutdown()
        with self.assertRaises(RuntimeError):
            self.manager.submit("late", lambda ctx: "")


class MatchChoiceTests(unittest.TestCase):
    CHOICES = ["Chrome", "Microsoft Edge", "Firefox"]

    def test_ordinals(self) -> None:
        cases = {
            "first": "Chrome", "1": "Chrome", "one": "Chrome", "the first one": "Chrome",
            "second": "Microsoft Edge", "2": "Microsoft Edge", "number two": "Microsoft Edge",
            "the third one please": "Firefox", "last": "Firefox", "3rd": "Firefox",
        }
        for answer, expected in cases.items():
            with self.subTest(answer=answer):
                self.assertEqual(match_choice(answer, self.CHOICES), expected)

    def test_text_exact_and_fuzzy(self) -> None:
        self.assertEqual(match_choice("chrome", self.CHOICES), "Chrome")
        self.assertEqual(match_choice("edge", self.CHOICES), "Microsoft Edge")
        self.assertEqual(match_choice("firefoks", self.CHOICES), "Firefox")

    def test_stricter_on_unknowns(self) -> None:
        for answer in ("", "banana", "fourth", "the first or second", "not the first one",
                       "no, the second", "don't use chrome", "second street one"):
            with self.subTest(answer=answer):
                self.assertIsNone(match_choice(answer, self.CHOICES))

    def test_ambiguous_text_is_none(self) -> None:
        self.assertIsNone(match_choice("report", ["report draft", "report final"]))

    def test_invisible_characters_ignored(self) -> None:
        self.assertEqual(match_choice("sec\u200bond", self.CHOICES), "Microsoft Edge")


@unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows-only")
class JobObjectTests(unittest.TestCase):
    def test_close_twice_is_safe(self) -> None:
        job = JobObject()
        self.assertTrue(job.create())
        self.assertTrue(job.active)
        job.close()
        job.close()
        self.assertFalse(job.active)

    def test_cancel_kills_job_children(self) -> None:
        recorder = Recorder()
        manager = _manager(recorder)
        children: list[subprocess.Popen] = []
        spawned = threading.Event()

        def body(ctx) -> str:
            children.append(ctx.spawn([PYTHON, "-c", "import time; time.sleep(30)"]))
            spawned.set()
            ctx.cancel.wait(30)
            return ""

        try:
            task_id = manager.submit("child", body)
            self.assertTrue(spawned.wait(10), "child never spawned")
            child = children[0]
            self.assertIsNone(child.poll())
            self.assertTrue(manager.cancel(task_id))
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                self.fail("the child outlived the cancelled task")
            recorder.wait_for("cancelled", task_id)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
            manager.shutdown()

    def test_spawn_after_cancel_refuses(self) -> None:
        recorder = Recorder()
        manager = _manager(recorder)
        outcome: list[str] = []

        def body(ctx) -> str:
            ctx.cancel.set()
            try:
                ctx.spawn([PYTHON, "-c", "pass"])
            except TaskCancelled:
                outcome.append("refused")
            return ""

        try:
            task_id = manager.submit("late spawn", body)
            recorder.wait_for("cancelled", task_id)
            self.assertEqual(outcome, ["refused"])
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
