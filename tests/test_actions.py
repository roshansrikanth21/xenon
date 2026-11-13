import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xenon.actions import (
    APP_OTHER,
    READ_ONLY_KINDS,
    SIDE_EFFECT_KINDS,
    Action,
    ActionContext,
    ActionResult,
    ApprovalPolicy,
    FileVerifier,
    Risk,
    TaskRunner,
    Verification,
    classify,
    needs_card,
)

# Every test here classifies with the outward kill switch in its default
# (on) position, whatever the developer's shell has set.
_CLEAN_ENV = {key: value for key, value in os.environ.items() if key != "XENON_OUTWARD"}


def whatsapp(**fields) -> ActionContext:
    base = dict(process_image="WhatsApp.Root.exe", window_title="WhatsApp", focused_role="Edit")
    base.update(fields)
    return ActionContext(**base)


def notepad(**fields) -> ActionContext:
    base = dict(
        process_image="notepad.exe",
        window_title="notes.txt - Notepad",
        focused_role="Document",
        allowlist=frozenset({"notepad.exe"}),
    )
    base.update(fields)
    return ActionContext(**base)


class FakeBackend:
    def __init__(self, results: dict[str, ActionResult] | None = None) -> None:
        self.calls: list[Action] = []
        self.results = results or {}

    def execute(self, action: Action) -> ActionResult:
        self.calls.append(action)
        return self.results.get(action.kind, ActionResult(True, "done"))


class FakeVerifier:
    def __init__(self, verification: Verification) -> None:
        self.verification = verification
        self.goals: list[str] = []

    def verify(self, goal: str) -> Verification:
        self.goals.append(goal)
        return self.verification


class FileVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "note.md"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reports_a_missing_file(self) -> None:
        result = FileVerifier(self.path).verify("save the note")

        self.assertFalse(result.ok)
        self.assertIn("was not written", result.detail)

    def test_accepts_an_existing_file(self) -> None:
        self.path.write_text("hello", encoding="utf-8")

        self.assertTrue(FileVerifier(self.path).verify("save the note").ok)

    def test_checks_the_expected_content(self) -> None:
        self.path.write_text("the deadline is Friday", encoding="utf-8")

        self.assertTrue(FileVerifier(self.path, "Friday").verify("save").ok)
        self.assertFalse(FileVerifier(self.path, "Monday").verify("save").ok)


class ApprovalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ApprovalPolicy()

    def test_read_only_actions_run_without_approval(self) -> None:
        decision = self.policy.decide(Action("observe"))

        self.assertTrue(decision.allowed)
        self.assertFalse(decision.requires_approval)

    def test_side_effects_require_approval(self) -> None:
        for kind in ("open_app", "type_text", "press_keys", "click_control"):
            with self.subTest(kind=kind):
                decision = self.policy.decide(Action(kind, target="x"))
                self.assertTrue(decision.allowed)
                self.assertTrue(decision.requires_approval)

    def test_shell_and_destructive_actions_are_denied(self) -> None:
        for kind in ("run_command", "delete_file", "format_disk"):
            with self.subTest(kind=kind):
                decision = self.policy.decide(Action(kind))
                self.assertFalse(decision.allowed)
                self.assertIn("not permitted", decision.reason)

    def test_unknown_kinds_are_denied(self) -> None:
        decision = self.policy.decide(Action("teleport"))

        self.assertFalse(decision.allowed)


class TaskRunnerTests(unittest.TestCase):
    def test_runs_read_only_steps_and_reports_verification(self) -> None:
        backend = FakeBackend()
        verifier = FakeVerifier(Verification(True, "title matched"))
        runner = TaskRunner(ApprovalPolicy(), backend, verifier=verifier)

        outcome = runner.run("look at the screen", [Action("observe")])

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(verifier.goals, ["look at the screen"])

    def test_side_effect_does_not_run_without_approval(self) -> None:
        backend = FakeBackend()
        runner = TaskRunner(ApprovalPolicy(), backend)

        outcome = runner.run("open notepad", [Action("open_app", target="notepad")])

        self.assertEqual(outcome.status, "denied")
        self.assertEqual(backend.calls, [])
        self.assertIn("approval", outcome.detail)

    def test_approver_can_deny_a_side_effect(self) -> None:
        backend = FakeBackend()
        runner = TaskRunner(ApprovalPolicy(), backend, approver=lambda action: False)

        outcome = runner.run("open notepad", [Action("open_app", target="notepad")])

        self.assertEqual(outcome.status, "denied")
        self.assertEqual(backend.calls, [])
        self.assertTrue(outcome.steps[0].approval_required)

    def test_approved_side_effect_executes(self) -> None:
        backend = FakeBackend()
        approved: list[Action] = []
        runner = TaskRunner(
            ApprovalPolicy(),
            backend,
            approver=lambda action: approved.append(action) or True,
            verifier=FakeVerifier(Verification(True)),
        )

        outcome = runner.run("type a note", [Action("type_text", value="hello")])

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(approved), 1)
        self.assertEqual(backend.calls[0].value, "hello")

    def test_read_only_steps_never_prompt(self) -> None:
        prompts: list[Action] = []
        runner = TaskRunner(
            ApprovalPolicy(),
            FakeBackend(),
            approver=lambda action: prompts.append(action) or True,
            verifier=FakeVerifier(Verification(True)),
        )

        runner.run("observe", [Action("observe")])

        self.assertEqual(prompts, [])

    def test_backend_failure_stops_the_plan(self) -> None:
        backend = FakeBackend({"type_text": ActionResult(False, "focus lost")})
        runner = TaskRunner(
            ApprovalPolicy(),
            backend,
            approver=lambda action: True,
            verifier=FakeVerifier(Verification(True)),
        )

        outcome = runner.run(
            "fill the form",
            [Action("type_text", value="a"), Action("press_keys", value="enter")],
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(len(backend.calls), 1)
        self.assertIn("focus lost", outcome.detail)

    def test_verification_failure_is_not_reported_as_success(self) -> None:
        runner = TaskRunner(
            ApprovalPolicy(),
            FakeBackend(),
            approver=lambda action: True,
            verifier=FakeVerifier(Verification(False, "title did not change")),
        )

        outcome = runner.run("open notepad", [Action("open_app", target="notepad")])

        self.assertEqual(outcome.status, "unverified")
        self.assertIn("title did not change", outcome.detail)

    def test_missing_verifier_is_reported_as_unverified(self) -> None:
        runner = TaskRunner(
            ApprovalPolicy(), FakeBackend(), approver=lambda action: True
        )

        outcome = runner.run("open notepad", [Action("open_app", target="notepad")])

        self.assertEqual(outcome.status, "unverified")
        self.assertIn("verifier", outcome.detail)

    def test_denied_step_stops_before_later_steps(self) -> None:
        backend = FakeBackend()
        runner = TaskRunner(ApprovalPolicy(), backend)

        outcome = runner.run(
            "dangerous",
            [Action("observe"), Action("run_command", value="rm -rf /")],
        )

        self.assertEqual(outcome.status, "denied")
        self.assertEqual([call.kind for call in backend.calls], ["observe"])

    def test_records_each_step_for_audit(self) -> None:
        runner = TaskRunner(
            ApprovalPolicy(),
            FakeBackend(),
            approver=lambda action: True,
            verifier=FakeVerifier(Verification(True)),
        )

        outcome = runner.run(
            "two steps",
            [Action("observe"), Action("type_text", value="hi")],
        )

        self.assertEqual([step.action.kind for step in outcome.steps], ["observe", "type_text"])
        self.assertTrue(all(step.result.ok for step in outcome.steps))


class ClassifyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, _CLEAN_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class LegacyKindClassificationTests(ClassifyTestCase):
    def test_legacy_press_enter_in_whatsapp_is_outward(self) -> None:
        for keys in ("enter", "Enter", "ctrl+enter", "Return"):
            with self.subTest(keys=keys):
                risk, reason = classify(Action("press_keys", value=keys), whatsapp())
                self.assertIs(risk, Risk.OUTWARD, reason)

    def test_enter_in_a_browser_composer_is_outward(self) -> None:
        ctx = ActionContext(process_image="msedge.exe", url="https://mail.google.com/", focused_role="Edit")

        self.assertIs(classify(Action("press_keys", value="enter"), ctx)[0], Risk.OUTWARD)

    def test_enter_in_notepad_is_local(self) -> None:
        risk, reason = classify(Action("press_keys", value="enter"), notepad())

        self.assertIs(risk, Risk.LOCAL, reason)
        self.assertFalse(needs_card(risk, reason))

    def test_type_into_console_is_denied(self) -> None:
        for image in ("cmd.exe", "powershell.exe", "pwsh.exe", "WindowsTerminal.exe", "conhost.exe", "wsl.exe"):
            with self.subTest(image=image):
                risk, _ = classify(Action("type_text", value="dir"), ActionContext(process_image=image))
                self.assertIs(risk, Risk.DENIED)

    def test_supplied_app_class_never_loosens_the_image(self) -> None:
        ctx = ActionContext(process_image="WhatsApp.Root.exe", app_class=APP_OTHER, focused_role="Edit")

        self.assertIs(classify(Action("press_keys", value="enter"), ctx)[0], Risk.OUTWARD)

    def test_newline_in_text_typed_into_comms_is_outward(self) -> None:
        risk, _ = classify(Action("type_text", value="hi\n"), whatsapp())

        self.assertIs(risk, Risk.OUTWARD)

    def test_typing_into_comms_needs_a_card(self) -> None:
        risk, reason = classify(Action("type_text", value="hi"), whatsapp())

        self.assertIs(risk, Risk.LOCAL)
        self.assertTrue(needs_card(risk, reason))

    def test_navigation_keys_in_comms_are_local(self) -> None:
        for keys in ("tab", "shift+tab", "down", "pagedown", "esc"):
            with self.subTest(keys=keys):
                self.assertIs(classify(Action("press_keys", value=keys), whatsapp())[0], Risk.LOCAL)

    def test_other_keys_in_comms_are_outward(self) -> None:
        for keys in ("ctrl+v", "a", "space", "alt+s"):
            with self.subTest(keys=keys):
                self.assertIs(classify(Action("press_keys", value=keys), whatsapp())[0], Risk.OUTWARD)

    def test_click_send_in_whatsapp_is_outward_even_when_he_said_send(self) -> None:
        ctx = whatsapp(element_name="Send", element_role="Button", user_words="click send")

        self.assertIs(classify(Action("click_control", target="Send"), ctx)[0], Risk.OUTWARD)

    def test_chat_row_named_in_his_words_is_local(self) -> None:
        ctx = whatsapp(element_name="Amma", element_role="ListItem", user_words="open the amma chat")

        self.assertIs(classify(Action("click"), ctx)[0], Risk.LOCAL)

    def test_named_button_from_his_words_is_local_only_when_untainted(self) -> None:
        clean = whatsapp(element_name="Starred messages", element_role="Button", user_words="show starred messages")
        tainted = whatsapp(
            element_name="Starred messages", element_role="Button", user_words="show starred messages", tainted=True
        )

        self.assertIs(classify(Action("click"), clean)[0], Risk.LOCAL)
        self.assertIs(classify(Action("click"), tainted)[0], Risk.OUTWARD)

    def test_unknown_foreground_is_strict(self) -> None:
        self.assertIs(classify(Action("press_keys", value="enter"))[0], Risk.OUTWARD)
        self.assertIs(classify(Action("click_control", target="OK"))[0], Risk.OUTWARD)
        risk, reason = classify(Action("type_text", value="hi"))
        self.assertIs(risk, Risk.LOCAL)
        self.assertTrue(needs_card(risk, reason))

    def test_enter_with_unknown_focus_in_a_native_app_is_outward(self) -> None:
        ctx = ActionContext(process_image="notepad.exe", focused_role="")

        self.assertIs(classify(Action("press_keys", value="enter"), ctx)[0], Risk.OUTWARD)

    def test_delete_key_outside_a_text_field_is_destructive(self) -> None:
        ctx = ActionContext(process_image="explorer.exe", window_title="Downloads", focused_role="ListItem")

        self.assertIs(classify(Action("press_keys", value="delete"), ctx)[0], Risk.DESTRUCTIVE)
        self.assertIs(classify(Action("press_keys", value="delete"), notepad())[0], Risk.LOCAL)

    def test_dont_save_is_destructive(self) -> None:
        ctx = notepad(window_title="Notepad", element_name="Don't save", element_role="Button")

        self.assertIs(classify(Action("click"), ctx)[0], Risk.DESTRUCTIVE)

    def test_yes_in_a_delete_dialog_is_destructive(self) -> None:
        ctx = ActionContext(process_image="explorer.exe", window_title="Delete File", element_name="Yes", element_role="Button")

        self.assertIs(classify(Action("click"), ctx)[0], Risk.DESTRUCTIVE)

    def test_plain_button_in_a_native_app_is_local(self) -> None:
        ctx = ActionContext(process_image="CalculatorApp.exe", allowlist=frozenset({"CalculatorApp.exe"}))

        risk, reason = classify(Action("click_control", target="Seven", value="Button"), ctx)

        self.assertIs(risk, Risk.LOCAL)
        self.assertFalse(needs_card(risk, reason))

    def test_non_english_ui_makes_every_button_outward(self) -> None:
        ctx = ActionContext(process_image="CalculatorApp.exe", ui_language_english=False)

        self.assertIs(classify(Action("click_control", target="Sieben", value="Button"), ctx)[0], Risk.OUTWARD)

    def test_nameless_button_in_a_native_app_is_outward(self) -> None:
        ctx = ActionContext(process_image="someapp.exe", element_role="Button")

        self.assertIs(classify(Action("click"), ctx)[0], Risk.OUTWARD)

    def test_settings_toggle_is_local_with_a_card(self) -> None:
        ctx = ActionContext(process_image="SystemSettings.exe", element_name="Bluetooth", element_role="ToggleSwitch")

        risk, reason = classify(Action("toggle"), ctx)

        self.assertIs(risk, Risk.LOCAL)
        self.assertTrue(needs_card(risk, reason))
        self.assertIn("old and new", reason)


class KindClassificationTests(ClassifyTestCase):
    def test_outward_and_destructive_kinds(self) -> None:
        for kind in ("send_email", "send_message", "desktop_commit", "propose_send_email"):
            with self.subTest(kind=kind):
                self.assertIs(classify(Action(kind))[0], Risk.OUTWARD)
        self.assertIs(classify(Action("desktop_destroy"))[0], Risk.DESTRUCTIVE)

    def test_denied_and_unknown_kinds(self) -> None:
        for kind in ("run_command", "run_shell", "delete_file", "format_disk", "http_request", "open_url", "teleport", ""):
            with self.subTest(kind=kind):
                self.assertIs(classify(Action(kind))[0], Risk.DENIED)

    def test_read_kinds(self) -> None:
        for kind in ("observe", "wait", "read", "screen_read"):
            with self.subTest(kind=kind):
                self.assertIs(classify(Action(kind))[0], Risk.READ)

    def test_web_search_needs_a_card_only_when_tainted(self) -> None:
        self.assertIs(classify(Action("web_search", value="python re"), ActionContext())[0], Risk.READ)
        risk, reason = classify(Action("web_search", value="x"), ActionContext(tainted=True))
        self.assertIs(risk, Risk.LOCAL)
        self.assertTrue(needs_card(risk, reason))

    def test_open_app_refuses_consoles_schemes_and_unc(self) -> None:
        for target in ("cmd", "powershell", "Windows Terminal", "regedit", r"\\server\share\x.exe", "ms-settings:network", "whatsapp://send?text=x"):
            with self.subTest(target=target):
                self.assertIs(classify(Action("open_app", target=target))[0], Risk.DENIED)

    def test_open_app_from_a_model_may_only_name_an_app(self) -> None:
        model = ActionContext(origin="model")
        typed = ActionContext(origin="typed")

        self.assertIs(classify(Action("open_app", target="https://youtube.com"), model)[0], Risk.DENIED)
        self.assertIs(classify(Action("open_app", target="https://youtube.com"), typed)[0], Risk.LOCAL)
        self.assertIs(classify(Action("open_app", target="notepad"), model)[0], Risk.LOCAL)

    def test_tainted_task_cannot_open_apps_outside_its_allowlist(self) -> None:
        ctx = ActionContext(tainted=True, allowlist=frozenset({"notepad.exe"}))

        self.assertIs(classify(Action("open_app", target="chrome"), ctx)[0], Risk.DENIED)
        self.assertIs(classify(Action("open_app", target="notepad"), ctx)[0], Risk.LOCAL)

    def test_allowlist_decides_the_local_card(self) -> None:
        inside = classify(Action("type_text", value="hi"), notepad())
        outside = classify(Action("type_text", value="hi"), notepad(allowlist=frozenset()))

        self.assertFalse(needs_card(*inside))
        self.assertTrue(needs_card(*outside))

    def test_write_file_refuses_startup_and_scripts(self) -> None:
        for target in (
            r"C:\Users\rosha\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\x.txt",
            r"D:\notes\run.ps1",
            r"\\host\share\x.txt",
        ):
            with self.subTest(target=target):
                self.assertIs(classify(Action("write_file", target=target, value="x"))[0], Risk.DENIED)

    def test_write_file_outside_his_words_needs_a_card(self) -> None:
        # A name that does not exist: overwriting an existing file is its own rule.
        target = "xenon-test-new-note-7f3a.md"
        self.assertFalse(os.path.exists(target))
        named = classify(Action("write_file", target=target, value="x"), ActionContext(user_words=f"save {target}"))
        unnamed = classify(Action("write_file", target=target, value="x"), ActionContext(user_words="save a note"))

        self.assertIs(named[0], Risk.LOCAL)
        self.assertFalse(needs_card(*named))
        self.assertTrue(needs_card(*unnamed))

    def test_overwriting_an_existing_file_is_destructive(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            existing = os.path.join(folder, "main.tex")
            with open(existing, "w", encoding="utf-8") as handle:
                handle.write("paper")
            risk, reason = classify(
                Action("write_file", target=existing, value="x"), ActionContext(user_words=f"save {existing}")
            )
            self.assertIs(risk, Risk.DESTRUCTIVE, reason)
            self.assertIn("overwrite", reason)
            fresh = os.path.join(folder, "new.txt")
            self.assertIs(classify(Action("write_file", target=fresh, value="x"))[0], Risk.LOCAL)

    def test_write_file_refuses_code_and_persistence_targets(self) -> None:
        home = os.path.expanduser("~")
        for target in (
            os.path.join(home, ".claude", "settings.json"),
            r"D:\xenon\.venv\Lib\site-packages\zz.pth",
            os.path.join(home, ".gitconfig"),
            os.path.join(home, ".bashrc"),
            os.path.join(home, ".npmrc"),
            r"D:\xenon\xenon\fastpath.py",
            r"D:\work\tool.pyw",
            os.path.join(home, "AppData", "Roaming", "Code", "User", "settings.json"),
            r"D:\repo\.vscode\tasks.json",
            r"D:\repo\conftest.py",
        ):
            with self.subTest(target=target):
                risk, reason = classify(
                    Action("write_file", target=target, value="x"), ActionContext(user_words=f"save {target}")
                )
                self.assertIs(risk, Risk.DENIED, reason)

    def test_tainted_text_naming_a_foreign_host_needs_a_card(self) -> None:
        ctx = notepad(tainted=True, user_words="type a note about python.org")

        foreign = classify(Action("type_text", value="see https://evil.example/x"), ctx)
        said = classify(Action("type_text", value="see python.org"), ctx)

        self.assertTrue(needs_card(*foreign))
        self.assertIn("evil.example", foreign[1])
        self.assertFalse(needs_card(*said))

    def test_model_hint_only_raises(self) -> None:
        ctx = notepad()
        base = classify(Action("type_text", value="x"), ctx)

        self.assertIs(classify(Action("type_text", value="x"), ctx, model_hint=Risk.OUTWARD)[0], Risk.OUTWARD)
        self.assertIs(classify(Action("type_text", value="x"), ctx, model_hint=Risk.READ)[0], base[0])
        self.assertIs(classify(Action("type_text", value="x"), ctx, model_hint="destructive")[0], Risk.DESTRUCTIVE)
        self.assertIs(classify(Action("press_keys", value="enter"), whatsapp(), model_hint=Risk.READ)[0], Risk.OUTWARD)
        self.assertIs(classify(Action("type_text", value="x"), ctx, model_hint="scary")[0], Risk.OUTWARD)

    def test_outward_kill_switch_denies_outward_and_destructive(self) -> None:
        with mock.patch.dict(os.environ, {"XENON_OUTWARD": "off"}):
            self.assertIs(classify(Action("send_email"))[0], Risk.DENIED)
            self.assertIs(classify(Action("press_keys", value="enter"), whatsapp())[0], Risk.DENIED)
            self.assertIs(classify(Action("desktop_destroy"))[0], Risk.DENIED)
            self.assertIs(classify(Action("type_text", value="x"), notepad())[0], Risk.LOCAL)

    def test_needs_card(self) -> None:
        self.assertTrue(needs_card(Risk.OUTWARD, ""))
        self.assertTrue(needs_card(Risk.DESTRUCTIVE, ""))
        self.assertTrue(needs_card(Risk.LOCAL, "card: outside the allowlist"))
        self.assertFalse(needs_card(Risk.LOCAL, "type 5 characters"))
        self.assertFalse(needs_card(Risk.READ, "card: never for reads"))
        self.assertFalse(needs_card(Risk.DENIED, "refused"))

    def test_planner_kind_sets_did_not_grow(self) -> None:
        # The legacy planner offers exactly these to a model.
        self.assertEqual(READ_ONLY_KINDS, {"observe", "wait"})
        self.assertEqual(
            SIDE_EFFECT_KINDS,
            {"open_app", "focus_window", "type_text", "press_keys", "click_control", "write_file"},
        )


class PolicyRiskTests(ClassifyTestCase):
    def test_decision_carries_risk_and_card_kind(self) -> None:
        policy = ApprovalPolicy()

        outward = policy.decide(Action("press_keys", value="enter"), whatsapp())
        local = policy.decide(Action("type_text", value="hi"), notepad())
        denied = policy.decide(Action("type_text", value="dir"), ActionContext(process_image="cmd.exe"))
        read = policy.decide(Action("observe"))

        self.assertEqual((outward.allowed, outward.requires_approval, outward.risk, outward.card_kind), (True, True, Risk.OUTWARD, "hard"))
        self.assertEqual((local.allowed, local.requires_approval, local.risk, local.card_kind), (True, True, Risk.LOCAL, ""))
        self.assertEqual((denied.allowed, denied.risk), (False, Risk.DENIED))
        self.assertEqual((read.allowed, read.requires_approval, read.risk), (True, False, Risk.READ))

    def test_destructive_card_kind(self) -> None:
        decision = ApprovalPolicy().decide(Action("desktop_destroy"))

        self.assertEqual(decision.card_kind, "destructive")


class RiskAwareRunnerTests(ClassifyTestCase):
    def test_two_argument_approver_receives_the_risk(self) -> None:
        seen: list[tuple[str, Risk]] = []

        def approver(action: Action, risk: Risk) -> bool:
            seen.append((action.kind, risk))
            return risk is Risk.LOCAL

        backend = FakeBackend()
        runner = TaskRunner(
            ApprovalPolicy(),
            backend,
            approver=approver,
            context_provider=lambda action: whatsapp(),
        )

        outcome = runner.run(
            "tell mom hi",
            [Action("type_text", value="hi"), Action("press_keys", value="enter")],
        )

        self.assertEqual(seen, [("type_text", Risk.LOCAL), ("press_keys", Risk.OUTWARD)])
        self.assertEqual(outcome.status, "denied")
        self.assertEqual([call.kind for call in backend.calls], ["type_text"])
        self.assertIs(outcome.steps[-1].risk, Risk.OUTWARD)

    def test_one_argument_approvers_still_work(self) -> None:
        runner = TaskRunner(ApprovalPolicy(), FakeBackend(), approver=lambda action: True)

        outcome = runner.run("note", [Action("type_text", value="hi")])

        self.assertEqual(outcome.status, "unverified")
        self.assertEqual(len(outcome.steps), 1)

    def test_context_provider_denial_stops_the_plan(self) -> None:
        backend = FakeBackend()
        runner = TaskRunner(
            ApprovalPolicy(),
            backend,
            approver=lambda action, risk: True,
            context_provider=lambda action: ActionContext(process_image="powershell.exe"),
        )

        outcome = runner.run("type into the console", [Action("type_text", value="whoami")])

        self.assertEqual(outcome.status, "denied")
        self.assertEqual(backend.calls, [])
        self.assertIn("console", outcome.detail)

    def test_failing_context_provider_is_read_as_unidentified(self) -> None:
        seen: list[Risk] = []

        def broken(action: Action) -> ActionContext:
            raise OSError("no foreground")

        runner = TaskRunner(
            ApprovalPolicy(),
            FakeBackend(),
            approver=lambda action, risk: seen.append(risk) or False,
            context_provider=broken,
        )

        runner.run("press enter", [Action("press_keys", value="enter")])

        self.assertEqual(seen, [Risk.OUTWARD])


if __name__ == "__main__":
    unittest.main()
