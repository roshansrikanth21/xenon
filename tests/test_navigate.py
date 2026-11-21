"""Voice navigation: only known folders, never a guessed path."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from xenon import navigate


class ParseTests(unittest.TestCase):
    def test_common_phrasings(self) -> None:
        for phrase in ("go to downloads", "open my documents folder",
                       "take me to the desktop", "navigate to pictures", "home"):
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(navigate.parse(phrase))

    def test_unknown_or_long_requests_are_not_navigation(self) -> None:
        for phrase in ("go to the moon", "delete my documents", "open the folder and then copy it somewhere else",
                       ""):
            with self.subTest(phrase=phrase):
                self.assertIsNone(navigate.parse(phrase))

    def test_paths_come_from_the_users_profile(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"USERPROFILE": home}):
                place = navigate.parse("downloads")
                self.assertEqual(place.path, os.path.join(home, "Downloads"))


class OpenTests(unittest.TestCase):
    def test_a_real_folder_is_opened_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            folder = os.path.join(home, "Downloads")
            os.mkdir(folder)
            opened = []
            with mock.patch.dict(os.environ, {"USERPROFILE": home}):
                reply = navigate.run("go to downloads", opener=opened.append)
            self.assertEqual(reply, "Opening Downloads.")
            self.assertEqual(opened, [folder])

    def test_a_missing_folder_is_reported_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            opened = []
            with mock.patch.dict(os.environ, {"USERPROFILE": home}):
                reply = navigate.run("go to downloads", opener=opened.append)
            self.assertIn("can't find", reply)
            self.assertEqual(opened, [])
            self.assertFalse(os.path.exists(os.path.join(home, "Downloads")))

    def test_not_navigation_returns_none(self) -> None:
        self.assertIsNone(navigate.run("play some music", opener=lambda _p: None))


if __name__ == "__main__":
    unittest.main()
