import unittest

from xenon.actions import (
    APP_BROWSER,
    APP_COMMS,
    APP_OTHER,
    APP_SETTINGS,
    APP_SHELL,
    APP_UNKNOWN,
    ActionContext,
)
from xenon.surfaces import (
    BROWSER_IMAGES,
    COMMS_IMAGES,
    KEY_BLOCKLIST,
    SHELL_IMAGES,
    app_class_for,
    denied_url_reason,
    file_dialog_refusal,
    is_comms_url,
    is_denied_surface,
    is_nav_keys,
    key_refused,
    norm_image,
    normalize_chord,
    path_refusal,
    read_browser_url,
    registrable_domain,
)


class ImageTests(unittest.TestCase):
    def test_contract_images_are_present(self) -> None:
        for image in (
            "WhatsApp.Root.exe", "WhatsApp.exe", "olk.exe", "outlook.exe", "Telegram.exe",
            "Discord.exe", "ms-teams.exe", "slack.exe", "Signal.exe", "claude.exe",
        ):
            self.assertIn(image, COMMS_IMAGES)
        for image in ("msedge.exe", "chrome.exe", "brave.exe", "opera.exe", "firefox.exe", "vivaldi.exe"):
            self.assertIn(image, BROWSER_IMAGES)
        for image in (
            "cmd.exe", "powershell.exe", "pwsh.exe", "WindowsTerminal.exe", "OpenConsole.exe",
            "conhost.exe", "wsl.exe", "bash.exe", "mintty.exe",
        ):
            self.assertIn(image, SHELL_IMAGES)

    def test_norm_image_handles_paths_case_and_missing_extension(self) -> None:
        self.assertEqual(norm_image(r"C:\Program Files\WindowsApps\WhatsApp.Root.exe"), "whatsapp.root.exe")
        self.assertEqual(norm_image("WhatsApp.Root"), "whatsapp.root.exe")  # Win32Backend drops .exe
        self.assertEqual(norm_image("CMD.EXE"), "cmd.exe")
        self.assertEqual(norm_image(""), "")


class AppClassTests(unittest.TestCase):
    def test_classes_by_image(self) -> None:
        self.assertEqual(app_class_for("WhatsApp.Root"), APP_COMMS)
        self.assertEqual(app_class_for("claude.exe"), APP_COMMS)
        self.assertEqual(app_class_for("pwsh.exe"), APP_SHELL)
        self.assertEqual(app_class_for("SystemSettings.exe"), APP_SETTINGS)
        self.assertEqual(app_class_for("notepad.exe"), APP_OTHER)
        self.assertEqual(app_class_for(""), APP_UNKNOWN)

    def test_browser_with_unreadable_url_is_comms(self) -> None:
        self.assertEqual(app_class_for("chrome.exe", ""), APP_COMMS)
        self.assertEqual(app_class_for("opera.exe"), APP_COMMS)

    def test_browser_on_comms_domain_is_comms(self) -> None:
        for url in (
            "https://mail.google.com/mail/u/0/#inbox",
            "web.whatsapp.com",
            "https://www.linkedin.com/feed/",
            "https://github.com/anthropics",
            "https://docs.google.com/forms/d/e/x/viewform",
            "https://forms.office.com/r/abc",
        ):
            with self.subTest(url=url):
                self.assertEqual(app_class_for("msedge.exe", url), APP_COMMS)

    def test_browser_on_other_site_is_browser(self) -> None:
        self.assertEqual(app_class_for("chrome.exe", "https://docs.python.org/3/"), APP_BROWSER)

    def test_frame_host_is_unknown(self) -> None:
        self.assertEqual(app_class_for("ApplicationFrameHost.exe"), APP_UNKNOWN)

    def test_lookalike_comms_domain_is_not_trusted_as_comms(self) -> None:
        # Not comms (so not "known"), but still a strict browser.
        self.assertFalse(is_comms_url("https://mail.google.com.evil.example/"))
        self.assertEqual(app_class_for("chrome.exe", "https://mail.google.com.evil.example/"), APP_BROWSER)

    def test_registrable_domain(self) -> None:
        self.assertEqual(registrable_domain("https://mail.google.com/x"), "google.com")
        self.assertEqual(registrable_domain("https://www.amazon.co.in/cart"), "amazon.co.in")
        self.assertEqual(registrable_domain("user.github.io"), "user.github.io")
        self.assertEqual(registrable_domain("https://\u0430pple.com/"), "xn--pple-43d.com")


class DeniedSurfaceTests(unittest.TestCase):
    def denied(self, **fields) -> str | None:
        return is_denied_surface(ActionContext(**fields))

    def test_consoles_are_denied(self) -> None:
        for image in ("cmd.exe", "powershell.exe", "pwsh", "WindowsTerminal.exe", "OpenConsole.exe", "wsl.exe"):
            with self.subTest(image=image):
                self.assertIsNotNone(self.denied(process_image=image))

    def test_system_surfaces_are_denied(self) -> None:
        for image in (
            "StartMenuExperienceHost.exe", "SearchHost.exe", "regedit.exe", "mmc.exe",
            "SecHealthUI.exe", "consent.exe", "CredentialUIBroker.exe", "SystemPropertiesAdvanced.exe",
        ):
            with self.subTest(image=image):
                self.assertIsNotNone(self.denied(process_image=image))

    def test_run_dialog_and_explorer_address_bar(self) -> None:
        self.assertIsNotNone(self.denied(process_image="explorer.exe", window_title="Run"))
        self.assertIsNotNone(
            self.denied(process_image="explorer.exe", window_title="Downloads", element_name="Address: Downloads")
        )
        self.assertIsNone(self.denied(process_image="explorer.exe", window_title="Downloads", element_name="Name"))

    def test_xenon_own_windows_are_denied(self) -> None:
        self.assertIsNotNone(self.denied(process_image="pythonw.exe", window_title="Xenon"))
        self.assertIsNotNone(self.denied(process_image="python.exe", window_title="tk"))
        self.assertIsNotNone(self.denied(process_image="pythonw.exe", window_title="X\u200benon"))

    def test_vscode_terminal_panel_is_denied(self) -> None:
        self.assertIsNotNone(self.denied(process_image="Code.exe", element_name="Terminal 1, pwsh"))
        self.assertIsNone(self.denied(process_image="Code.exe", element_name="Editor content"))

    def test_network_proxy_and_vpn_settings_are_denied(self) -> None:
        self.assertIsNotNone(self.denied(process_image="SystemSettings.exe", element_name="Use a proxy server"))
        self.assertIsNotNone(self.denied(process_image="SystemSettings.exe", element_name="VPN"))
        self.assertIsNotNone(self.denied(process_image="explorer.exe", window_title="Network Connections"))
        self.assertIsNotNone(self.denied(process_image="openvpn-gui.exe", window_title="OpenVPN"))
        self.assertIsNone(self.denied(process_image="SystemSettings.exe", element_name="Bluetooth"))

    def test_his_own_files_named_after_tools_are_not_denied(self) -> None:
        self.assertIsNone(self.denied(process_image="notepad.exe", window_title="proxy.py - Notepad"))
        self.assertIsNone(self.denied(process_image="Code.exe", window_title="firewall_rules.md - xenon - Visual Studio Code"))

    def test_browser_internal_and_identity_pages(self) -> None:
        for url in (
            "chrome://settings/passwords",
            "edge://extensions",
            "opera://settings",
            "about:config",
            "https://accounts.google.com/signin",
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x",
            "https://github.com/login/oauth/authorize?client_id=abc",
            "https://example.com/oauth2/authorize?client_id=a&redirect_uri=b",
            "https://pay.google.com/gp/w/home",
            "https://www.paypal.com/checkoutnow?token=x",
            "https://checkout.razorpay.com/v1/x",
        ):
            with self.subTest(url=url):
                self.assertIsNotNone(self.denied(process_image="chrome.exe", url=url))

    def test_title_of_a_web_page_cannot_be_used_for_title_rules(self) -> None:
        # A page titled "Registry Editor" is just a page: the browser title is
        # page-authored, so it gets the strict browser rules, not a denial.
        self.assertIsNone(self.denied(process_image="chrome.exe", url="https://example.com", window_title="Registry Editor"))

    def test_devtools_is_denied(self) -> None:
        self.assertIsNotNone(self.denied(process_image="msedge.exe", url="https://x.example", window_title="DevTools - x.example"))

    def test_ordinary_app_is_not_denied(self) -> None:
        self.assertIsNone(self.denied(process_image="notepad.exe", window_title="notes.txt - Notepad"))


class DeniedUrlTests(unittest.TestCase):
    def test_javascript_with_hidden_whitespace_is_denied(self) -> None:
        self.assertIsNotNone(denied_url_reason("java\tscript:alert(1)"))
        self.assertIsNotNone(denied_url_reason("  JavaScript:alert(1)"))
        self.assertIsNotNone(denied_url_reason("data:text/html,<script>x</script>"))

    def test_device_code_pages_are_denied(self) -> None:
        self.assertIsNotNone(denied_url_reason("https://microsoft.com/devicelogin"))
        self.assertIsNotNone(denied_url_reason("https://github.com/login/device"))

    def test_userinfo_trick_uses_the_real_host(self) -> None:
        # The real host is evil.example, which is not an identity page, but
        # it must not be treated as accounts.google.com either.
        self.assertIsNone(denied_url_reason("https://accounts.google.com@evil.example/home"))

    def test_login_page_with_an_extension_is_denied(self) -> None:
        self.assertIsNotNone(denied_url_reason("https://bank.example/login.php"))
        self.assertIsNotNone(denied_url_reason("https://shop.example/checkout.aspx?cart=1"))

    def test_ordinary_urls_are_not_denied(self) -> None:
        for url in (
            "https://docs.python.org/3/library/re.html",
            "https://www.youtube.com/watch?v=x",
            "localhost:8080",
            "https://github.com/org/repo/blob/main/docs/auth.md",
        ):
            with self.subTest(url=url):
                self.assertIsNone(denied_url_reason(url))

    def test_empty_url_is_not_denied_here(self) -> None:
        self.assertIsNone(denied_url_reason(""))


class KeyTests(unittest.TestCase):
    def test_normalize_chord_order_and_aliases(self) -> None:
        self.assertEqual(normalize_chord("Shift+Ctrl+ESC"), "ctrl+shift+esc")
        self.assertEqual(normalize_chord("control + shift + escape"), "ctrl+shift+esc")
        self.assertEqual(normalize_chord("Del+Alt+Ctrl"), "ctrl+alt+delete")
        self.assertEqual(normalize_chord("Windows"), "win")

    def test_blocklist_entries(self) -> None:
        for chord in (
            "alt+f4", "win", "win+r", "win+x", "win+l", "win+s", "win+e", "win+q", "ctrl+esc",
            "ctrl+shift+esc", "ctrl+alt+delete", "f12", "ctrl+shift+i", "ctrl+shift+j", "ctrl+shift+c",
        ):
            with self.subTest(chord=chord):
                self.assertIn(chord, KEY_BLOCKLIST)

    def test_blocked_keys_are_refused_in_any_spelling(self) -> None:
        for keys in ("ALT+F4", "f4+alt", "R+Win", "windows+x", "Ctrl+Alt+Del", "ctrl+shift+escape", "F12", "Shift+Ctrl+I"):
            with self.subTest(keys=keys):
                self.assertIsNotNone(key_refused(keys))

    def test_any_windows_key_chord_is_refused(self) -> None:
        self.assertIsNotNone(key_refused("win+1"))
        self.assertIsNotNone(key_refused("win+shift+s"))

    def test_a_blocked_chord_inside_a_sequence_is_refused(self) -> None:
        self.assertIsNotNone(key_refused("ctrl+a, alt+f4"))

    def test_tainted_browser_keys(self) -> None:
        browser = ActionContext(process_image="chrome.exe", url="https://example.com", tainted=True)
        clean = ActionContext(process_image="chrome.exe", url="https://example.com", tainted=False)
        notepad = ActionContext(process_image="notepad.exe", tainted=True)
        for keys in ("ctrl+l", "alt+d", "f6"):
            with self.subTest(keys=keys):
                self.assertIsNotNone(key_refused(keys, browser))
                self.assertIsNone(key_refused(keys, clean))
                self.assertIsNone(key_refused(keys, notepad))

    def test_ordinary_keys_pass(self) -> None:
        self.assertIsNone(key_refused("ctrl+s"))
        self.assertIsNone(key_refused("enter"))

    def test_empty_keys_are_refused(self) -> None:
        self.assertIsNotNone(key_refused(""))

    def test_nav_keys(self) -> None:
        self.assertTrue(is_nav_keys("tab"))
        self.assertTrue(is_nav_keys("Shift+Tab"))
        self.assertTrue(is_nav_keys("page down"))
        self.assertFalse(is_nav_keys("enter"))
        self.assertFalse(is_nav_keys("ctrl+v"))
        self.assertFalse(is_nav_keys(""))


class FileDialogTests(unittest.TestCase):
    def test_filename_must_be_in_his_words(self) -> None:
        self.assertIsNone(file_dialog_refusal("todo.txt", "save it as todo.txt"))
        self.assertIsNotNone(file_dialog_refusal("other.txt", "save it as todo.txt"))
        self.assertIsNotNone(file_dialog_refusal("todo.txt", ""))

    def test_folder_must_be_in_his_words_too(self) -> None:
        self.assertIsNone(file_dialog_refusal(r"D:\notes\todo.txt", r"save it to D:\notes as todo.txt"))
        self.assertIsNotNone(file_dialog_refusal(r"D:\xenon\xenon\todo.txt", "save it as todo.txt"))

    def test_refuses_startup_programs_and_unc(self) -> None:
        words = "save it as a.txt"
        for path in (
            r"C:\Users\rosha\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\a.txt",
            r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\a.txt",
            r"\\server\share\a.txt",
            "//server/share/a.txt",
            r"\\?\C:\a.txt",
            r"shell:startup\a.txt",
            r"%APPDATA%\a.txt",
            "a.txt:hidden.ps1",
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(file_dialog_refusal(path, words + " " + path))

    def test_refuses_script_and_shortcut_extensions(self) -> None:
        for name in ("run.bat", "run.cmd", "run.ps1", "run.vbs", "run.js", "run.hta", "run.lnk",
                     "run.url", "run.exe", "run.scr", "run.msi", "run.reg", "run.ps1.", "run.ps1 ", "invoice.pdf.exe"):
            with self.subTest(name=name):
                self.assertIsNotNone(file_dialog_refusal(name, f"save it as {name}"))

    def test_path_refusal_without_span(self) -> None:
        self.assertIsNone(path_refusal(r"D:\notes\todo.md"))
        self.assertIsNotNone(path_refusal(r"\\host\c$\x.txt"))


class BrowserUrlReaderTests(unittest.TestCase):
    def test_reader_fails_closed(self) -> None:
        def broken_desktop():
            raise RuntimeError("no UIA here")

        self.assertIsNone(read_browser_url(1, desktop_factory=broken_desktop))

    def test_reader_returns_the_omnibox_value(self) -> None:
        class Info:
            name = "Address and search bar"

        class Edit:
            element_info = Info()

            def get_value(self):
                return "https://example.com/"

        class Window:
            def descendants(self, control_type=""):
                return [Edit()]

        class Desktop:
            def window(self, handle=0):
                return Window()

        self.assertEqual(read_browser_url(1, desktop_factory=Desktop), "https://example.com/")


if __name__ == "__main__":
    unittest.main()
