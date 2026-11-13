import unittest

from xenon.apps import AppCatalog, AppEntry, launch
from xenon.windows_actions import DesktopTitleVerifier, find_window_title


def catalog_with(*entries: AppEntry, which=None) -> AppCatalog:
    return AppCatalog(entries=list(entries), which=which or (lambda name: None))


class ResolveTests(unittest.TestCase):
    def test_resolves_a_start_menu_app_id(self) -> None:
        catalog = catalog_with(
            AppEntry(name="Google Chrome", app_id="Chrome"),
        )

        resolved = catalog.resolve("chrome")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.app_id, "Chrome")
        self.assertEqual(resolved.source, "start-menu app id")

    def test_resolves_an_alias(self) -> None:
        catalog = catalog_with(
            AppEntry(name="Visual Studio Code", app_id="Microsoft.VisualStudioCode"),
        )

        resolved = catalog.resolve("code")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.name, "Visual Studio Code")

    def test_prefers_an_app_id_over_a_windowsapps_path(self) -> None:
        catalog = catalog_with(
            AppEntry(
                name="Notepad",
                path=r"C:\Program Files\WindowsApps\Notepad\Notepad.exe",
            ),
            AppEntry(name="Notepad", app_id="Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
        )

        resolved = catalog.resolve("notepad")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.app_id, "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App")

    def test_web_fallback_for_uninstalled_services(self) -> None:
        catalog = catalog_with()

        resolved = catalog.resolve("youtube")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.target, "https://www.youtube.com")
        self.assertEqual(resolved.source, "web")

    def test_unknown_names_return_none(self) -> None:
        catalog = catalog_with(AppEntry(name="Obsidian", app_id="md.obsidian"))

        self.assertIsNone(catalog.resolve("quantum frobnicate"))


class OriginTests(unittest.TestCase):
    """Model and voice words may only name catalog entries (phase 0)."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.existing = str(Path(folder.name, "payload.exe"))
        Path(self.existing).write_bytes(b"MZ")
        self.catalog = catalog_with(
            AppEntry(name="Spotify", app_id="SpotifyAB.SpotifyMusic!Spotify"),
            AppEntry(name="Settings", app_id="windows.immersivecontrolpanel!microsoft.windows.immersivecontrolpanel"),
            which=lambda name: r"C:\Users\x\Downloads\evil.exe",
        )

    def test_open_app_rejects_uri_unc_and_scheme_for_model_and_voice_origin(self) -> None:
        hostile = (
            "ms-settings:privacy-webcam", "search-ms:query=x", "https://evil.example",
            "file:///C:/x.exe", r"\\attacker\share\run.exe", "//attacker/share",
            self.existing, r"C:\Windows\System32\cmd.exe", "%COMSPEC%", "..\\payload",
            "evil",  # not in the catalog; PATH lookup is off for these origins
        )
        for origin in ("model", "voice", "something-new"):
            for query in hostile:
                with self.subTest(origin=origin, query=query):
                    self.assertIsNone(self.catalog.resolve(query, origin=origin))

    def test_default_origin_is_the_strict_one(self) -> None:
        self.assertIsNone(self.catalog.resolve("ms-settings:privacy"))
        self.assertIsNone(self.catalog.resolve(self.existing))

    def test_catalog_names_still_resolve_for_model_and_voice(self) -> None:
        for origin in ("model", "voice"):
            with self.subTest(origin=origin):
                self.assertEqual(self.catalog.resolve("spotify", origin=origin).source,
                                 "start-menu app id")
                self.assertEqual(self.catalog.resolve("youtube", origin=origin).target,
                                 "https://www.youtube.com")
                # The code-owned alias resolves through the catalog, never as a URI.
                resolved = self.catalog.resolve("ms-settings:", origin=origin)
                self.assertEqual(resolved.name, "Settings")
                self.assertFalse(resolved.target)
        self.assertTrue(self.catalog.is_catalog_id("spotify"))
        self.assertFalse(self.catalog.is_catalog_id("https://evil.example"))

    def test_typed_and_cli_keep_web_urls_and_paths_but_no_other_schemes(self) -> None:
        catalog = catalog_with()
        for origin in ("typed", "cli"):
            with self.subTest(origin=origin):
                self.assertEqual(catalog.resolve("https://example.com/a", origin=origin).target,
                                 "https://example.com/a")
                self.assertEqual(catalog.resolve(self.existing, origin=origin).path, self.existing)
                for query in ("ms-settings:privacy", "search-ms:query=x", "file:///C:/x.exe",
                              "javascript:alert(1)", r"\\attacker\share\run.exe"):
                    self.assertIsNone(catalog.resolve(query, origin=origin), query)


class LaunchTests(unittest.TestCase):
    def test_app_id_launches_through_explorer(self) -> None:
        from xenon.apps import ResolvedApp

        calls: list[list[str]] = []
        resolved = ResolvedApp(
            name="Notepad",
            app_id="Microsoft.WindowsNotepad_8wekyb3d8bbwe!App",
        )

        detail = launch(resolved, spawn=calls.append)

        self.assertIn("Notepad", detail)
        self.assertEqual(
            calls, [["explorer.exe", r"shell:AppsFolder\Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"]]
        )

    def test_path_launches_through_startfile(self) -> None:
        from xenon.apps import ResolvedApp

        started: list[str] = []
        resolved = ResolvedApp(
            name="Chrome", path=r"C:\Chrome\chrome.exe"
        )

        launch(resolved, startfile=started.append)

        self.assertEqual(started, [r"C:\Chrome\chrome.exe"])


class FastTitleScanTests(unittest.TestCase):
    def test_find_window_title_matches_case_insensitively(self) -> None:
        scanner = lambda: ["scratch - File Explorer", "Untitled - Notepad"]

        self.assertEqual(find_window_title("notepad", scanner), "Untitled - Notepad")
        self.assertIsNone(find_window_title("chrome", scanner))
        self.assertIsNone(find_window_title("", scanner))

    def test_desktop_verifier_uses_injected_titles(self) -> None:
        # No desktop_factory, so the verifier uses the Win32 title scan path.
        verifier = DesktopTitleVerifier(
            "Notepad", title_scanner=lambda: ["Untitled - Notepad"]
        )

        self.assertTrue(verifier.verify("open notepad").ok)

        missing = DesktopTitleVerifier(
            "Notepad", title_scanner=lambda: ["scratch - File Explorer"]
        )
        self.assertFalse(missing.verify("open notepad").ok)


if __name__ == "__main__":
    unittest.main()
