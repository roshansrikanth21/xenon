"""Voice hooks for tasks: hand-off, one wake-less answer, and no approvals.

The voice loop itself runs here against scripted stand-ins for the
microphone, detector, transcriber and speaker, so no audio device, speech
model or TTS voice is touched.
"""
from __future__ import annotations

import importlib.util
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from xenon import __main__ as cli
from xenon import orb
from xenon.actions import Risk
from xenon.approvals import ApprovalBroker, AuditLog, Field, PendingAction
from xenon.orb import OrbTasks, TaskServices, VoiceHooks
from xenon.tasks import TaskManager

_SCRUB = (
    "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY",
    "OPENAI_API_KEY", "XENON_MODEL_API_KEY", "XENON_VOICE_APPROVE", "XENON_OUTWARD",
    "XENON_AGENT", "XENON_WAKE_MODE", "XENON_WAKE_WORD", "XENON_WAKE_ALIASES",
)


def scrubbed_env(test: unittest.TestCase, **extra: str) -> None:
    patcher = mock.patch.dict(os.environ, extra, clear=False)
    patcher.start()
    test.addCleanup(patcher.stop)
    for key in _SCRUB:
        os.environ.pop(key, None)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class HookHelperTests(unittest.TestCase):
    def test_task_hook_takes_do_and_skips_the_parsers(self) -> None:
        hooks = VoiceHooks(lambda *a: None, lambda *a: None, lambda *a: None,
                           task=lambda text, origin: "On it." if text.startswith("do ") else None)
        smart = mock.Mock(return_value=(None, None))
        answer = mock.Mock(return_value="an answer")

        self.assertEqual(cli._voice_dispatch("do type hello", hooks, smart, answer), ("On it.", False))
        smart.assert_not_called()
        answer.assert_not_called()

    def test_everything_else_falls_through_as_today(self) -> None:
        hooks = VoiceHooks(lambda *a: None, lambda *a: None, lambda *a: None, task=lambda t, o: None)
        self.assertEqual(
            cli._voice_dispatch("open notepad", hooks, lambda t: ("Opened Notepad.", None), lambda t: "x"),
            ("Opened Notepad.", False),
        )
        # The assistant reads the screen and memory: private on the tablet.
        self.assertEqual(
            cli._voice_dispatch("what is this", hooks, lambda t: (None, None), lambda t: "a slide"),
            ("a slide", True),
        )

    def test_a_task_layer_error_does_not_kill_the_voice_loop(self) -> None:
        def broken(text, origin):
            raise RuntimeError("boom")

        hooks = VoiceHooks(lambda *a: None, lambda *a: None, lambda *a: None, task=broken)
        reply, private = cli._voice_dispatch("do x", hooks, lambda t: (None, None), lambda t: "x")
        self.assertIn("boom", reply)
        self.assertFalse(private)

    def test_focus_listen_only_inside_the_window_and_while_waiting(self) -> None:
        waiting = {"value": True}
        hooks = VoiceHooks(lambda *a: None, lambda *a: None, lambda *a: None,
                           task=lambda t, o: "", focus_listen=lambda: waiting["value"])
        self.assertTrue(cli._focus_utterance(hooks, focus_until=10.0, now=5.0))
        self.assertFalse(cli._focus_utterance(hooks, focus_until=10.0, now=10.0))  # 8 s are up
        waiting["value"] = False
        self.assertFalse(cli._focus_utterance(hooks, focus_until=10.0, now=5.0))  # answered already
        no_task = VoiceHooks(lambda *a: None, lambda *a: None, lambda *a: None, focus_listen=lambda: True)
        self.assertFalse(cli._focus_utterance(no_task, focus_until=10.0, now=5.0))

        def broken() -> bool:
            raise RuntimeError("x")

        failing = VoiceHooks(lambda *a: None, lambda *a: None, lambda *a: None,
                             task=lambda t, o: "", focus_listen=broken)
        self.assertFalse(cli._focus_utterance(failing, focus_until=10.0, now=5.0))

    def test_announcements(self) -> None:
        said: "queue.Queue[tuple[str, str]]" = queue.Queue()
        hooks = VoiceHooks(lambda *a: None, lambda *a: None, lambda *a: None, say=said)
        self.assertIsNone(cli._next_announcement(hooks))
        said.put(("say", "   "))
        self.assertIsNone(cli._next_announcement(hooks))  # nothing to speak
        said.put(("ask", "Which file?"))
        self.assertEqual(cli._next_announcement(hooks), ("ask", "Which file?"))
        self.assertIsNone(cli._next_announcement(VoiceHooks(lambda *a: None, lambda *a: None, lambda *a: None)))

    def test_exchange_works_with_an_older_two_argument_hook(self) -> None:
        seen = []
        hooks = VoiceHooks(lambda *a: None, lambda *a: None, lambda you, answer: seen.append((you, answer)))
        cli._exchange(hooks, "hi", "hello", True)
        self.assertEqual(seen, [("hi", "hello")])

    def test_voice_confirm_hook_is_off(self) -> None:
        hooks = VoiceHooks(lambda *a: None, lambda *a: None, lambda *a: None)
        self.assertIsNone(hooks.confirm)


class VoiceRoutingTests(unittest.TestCase):
    """The orb's task layer as the voice thread sees it."""

    def setUp(self) -> None:
        scrubbed_env(self)
        self.cards: dict[str, object] = {}
        self.choices: dict[str, tuple] = {}
        self.bodies: dict[str, object] = {}
        self.results: list[str] = []
        ui = mock.Mock()
        ui.task_card.side_effect = lambda pending, decide: self.cards.__setitem__(pending.action_id, decide)
        ui.task_choice.side_effect = lambda pid, title, labels, on_pick, origin="typed": self.choices.__setitem__(
            pid, (labels, origin))
        ui.task_update.side_effect = lambda update, origin="typed": (
            self.results.append(update.text) if update.state == "done" else None)

        services = TaskServices(
            make_task=lambda goal, origin, tools: self.bodies.get(goal, lambda ctx: f"did {goal}"),
            is_t0=lambda text: False,
        )
        self.tasks = OrbTasks(
            services, post=lambda callback: callback(), ui=ui, audit=AuditLog(None),
            manager_factory=lambda **kw: TaskManager(com_init=lambda: None, **kw),
            agent_check=lambda: (True, ""),
        )
        self.addCleanup(self.tasks.close)

    def focus_listen(self) -> bool:
        # What OrbApp gives VoiceHooks.focus_listen.
        return self.tasks.waiting_for_input or bool(self.tasks.open_choices())

    def test_wakeless_answer_goes_to_the_waiting_task(self) -> None:
        self.bodies["rename a file"] = lambda ctx: f"renamed {ctx.ask('Which file?')}"
        self.assertEqual(self.tasks.route("do rename a file", "voice"), "On it.")
        self.assertTrue(wait_for(self.focus_listen))
        self.assertEqual(self.tasks.route("notes.txt", "voice-focus"), "Got it.")
        self.assertTrue(wait_for(lambda: self.results == ["renamed notes.txt"]))
        self.assertFalse(self.focus_listen())

    def test_a_pending_card_opens_no_wakeless_listen_and_voice_cannot_approve_it(self) -> None:
        tokens = []

        def body(ctx):
            pending = PendingAction(
                "c1", "desktop_commit", Risk.OUTWARD, "Press enter in WhatsApp",
                (Field("App", "WhatsApp", "xenon"),), "d" * 64, voice_ok=True, nonce="47",
                task_id=ctx.task_id,
            )
            self.tasks.caps.start_task(ctx.task_id, 1)
            tokens.append(self.tasks.broker.request(pending, cancel=ctx.cancel))
            return "finished"

        self.bodies["press enter in whatsapp"] = body
        self.tasks.route("do press enter in whatsapp", "voice")
        self.assertTrue(wait_for(lambda: "c1" in self.cards))
        self.assertFalse(self.focus_listen())  # a card is not a question
        for text in ("send 47", "send forty seven", "yes", "confirm"):
            with self.subTest(text=text):
                self.tasks.route(text, "voice")
                self.assertEqual(self.tasks.route(text, "voice-focus"), "")
        # Even the broker refuses a spoken approval while XENON_VOICE_APPROVE is off.
        self.assertFalse(self.tasks.broker.resolve("c1", "approve", via="voice", spoken="send 47"))
        self.assertEqual(tokens, [])
        self.assertEqual(len(self.tasks.broker.pending()), 1)
        self.assertEqual(self.tasks.route("stop", "voice"), "Stopped.")
        self.assertTrue(wait_for(lambda: tokens == [None]))

    def test_choice_card_picked_by_spoken_ordinal(self) -> None:
        release = threading.Event()
        self.bodies["first job"] = lambda ctx: (release.wait(3), "first done")[1]
        self.tasks.route("do first job", "voice")
        self.assertTrue(wait_for(lambda: self.tasks.busy))
        self.tasks.route("do second job", "voice")
        self.assertTrue(self.focus_listen())  # the choice holds the focus
        (labels, origin), = self.choices.values()
        self.assertEqual(origin, "voice")
        self.assertEqual(self.tasks.route("the first one", "voice-focus"), "OK.")  # "Queue it"
        release.set()
        self.assertTrue(wait_for(lambda: self.results == ["first done", "did second job"]))


# ---------------------------------------------------------------------------
# The voice loop, with scripted audio
# ---------------------------------------------------------------------------


class FakeSource:
    sample_rate = 16000

    def __init__(self, device=None) -> None:
        pass

    def read_frame(self):
        return [0.0] * 8

    def close(self) -> None:
        pass


class FakeSpeaker:
    said: list[str] = []

    def speak(self, text: str) -> None:
        FakeSpeaker.said.append(text)


class FakeTranscriber:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def transcribe(self, pcm, rate) -> str:
        return pcm  # the scripted detector hands over the words themselves


class FakeDucker:
    def duck(self, *args) -> int:
        return 0

    def restore(self) -> None:
        pass


class VoiceLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scrubbed_env(self, XENON_CHIME="off", XENON_DB_PATH=str(Path(tmp.name) / "voice.db"))
        FakeSpeaker.said = []

    def run_loop(self, utterances: list[str], announcements: list[tuple[str, str]], task, focus_listen):
        stop = threading.Event()
        script = list(utterances)

        class FakeDetector:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def reset(self) -> None:
                pass

            def feed(self, frame):
                if not script:
                    stop.set()
                    return None
                return script.pop(0)

        real_find_spec = importlib.util.find_spec

        def find_spec(name, *args, **kwargs):
            if name in {"sounddevice", "faster_whisper"}:
                return object()
            return real_find_spec(name, *args, **kwargs)

        said: "queue.Queue[tuple[str, str]]" = queue.Queue()
        for item in announcements:
            said.put(item)
        exchanges = []
        hooks = VoiceHooks(
            lambda state, detail: None, lambda level: None,
            lambda you, answer, private=False: exchanges.append((you, answer)),
            listen_now=None, task=task, focus_listen=focus_listen, say=said,
        )
        from xenon import audio, duck

        with mock.patch("importlib.util.find_spec", find_spec), mock.patch.multiple(
            audio, MicrophoneSource=FakeSource, SapiSpeaker=FakeSpeaker,
            UtteranceDetector=FakeDetector, WhisperTranscriber=FakeTranscriber,
        ), mock.patch.object(duck, "make_ducker", FakeDucker):
            runner = cli._orb_voice_runner(None)
            self.assertIsNotNone(runner)
            worker = threading.Thread(target=runner, args=(hooks, stop), daemon=True)
            worker.start()
            worker.join(10)
        self.assertFalse(worker.is_alive(), "the voice loop did not stop")
        return exchanges

    def test_question_opens_one_wakeless_answer_then_wake_word_again(self) -> None:
        calls = []

        def task(text, origin):
            calls.append((text, origin))
            if origin == "voice-focus":
                return "Got it."
            return "On it." if text.startswith("do ") else None

        exchanges = self.run_loop(
            ["notes dot txt", "send it", "xenon do open notepad"],
            [("ask", "Which file?")],
            task,
            focus_listen=lambda: True,
        )

        # The wake-less answer went to the task; the next wake-less sentence
        # ("send it") went nowhere; the woken "do ..." was handed off.
        self.assertEqual(calls, [("notes dot txt", "voice-focus"), ("do open notepad", "voice")])
        self.assertEqual(exchanges, [("do open notepad", "On it.")])
        spoken = " | ".join(FakeSpeaker.said)
        self.assertIn("Which file?", spoken)
        self.assertIn("Got it.", spoken)
        self.assertIn("On it.", spoken)

    def test_no_wakeless_listen_without_a_question(self) -> None:
        calls = []
        self.run_loop(["notes dot txt"], [], lambda text, origin: calls.append((text, origin)),
                      focus_listen=lambda: True)
        self.assertEqual(calls, [])

    def test_result_announcement_is_spoken_without_opening_focus(self) -> None:
        calls = []
        self.run_loop(["and another thing"], [("say", "Done.")],
                      lambda text, origin: calls.append((text, origin)), focus_listen=lambda: True)
        self.assertEqual(calls, [])
        self.assertIn("Done.", " | ".join(FakeSpeaker.said))


if __name__ == "__main__":
    unittest.main()
