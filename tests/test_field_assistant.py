"""Form-field suggestions: classify, source only from the local profile, never fill."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from xenon import field_assistant as fa


PROFILE = fa.Profile(
    name="Rose Hill", email="rose@example.com", phone="+1 555 0100",
    bio="I build calm, useful software.", short_bio="Software engineer.",
    skills=("Python", "Windows", "UI"), portfolio="https://example.com",
    headline="Engineer who likes local-first tools.",
)


class ClassifyTests(unittest.TestCase):
    def test_common_fields(self) -> None:
        self.assertEqual(fa.classify("Email address", "Internship application"), "email")
        self.assertEqual(fa.classify("Short bio", "Apply — Rose"), "bio")
        self.assertEqual(fa.classify("Technical skills", "Job application"), "skills")
        self.assertEqual(fa.classify("Portfolio URL", "Job"), "portfolio")
        self.assertEqual(fa.classify("Cover letter", "Careers"), "cover_letter")

    def test_secrets_are_never_suggested(self) -> None:
        for label in ("Password", "Card number", "CVV", "OTP", "Security code", "PIN"):
            with self.subTest(label=label):
                self.assertEqual(fa.classify(label, "Sign in"), "sensitive")

    def test_unknown_stays_unknown(self) -> None:
        self.assertEqual(fa.classify("Favorite colour", "Form"), "unknown")


class SuggestionTests(unittest.TestCase):
    def test_offers_two_or_three_clickable_options(self) -> None:
        for label in ("Email", "Short bio", "Skills", "Cover letter", "Portfolio"):
            with self.subTest(label=label):
                options = fa.suggestions(label, "Internship application", PROFILE)
                self.assertTrue(1 <= len(options) <= 3, options)
                self.assertEqual(len(options), len(set(options)))

    def test_no_suggestion_for_a_secret_or_unknown_field(self) -> None:
        self.assertEqual(fa.suggestions("Password", "Sign in", PROFILE), [])
        self.assertEqual(fa.suggestions("Favourite colour", "Form", PROFILE), [])

    def test_suggestions_come_only_from_the_profile(self) -> None:
        self.assertIn("rose@example.com", fa.suggestions("Email", "Form", PROFILE))
        empty = fa.Profile()
        self.assertEqual(fa.suggestions("Email", "Form", empty), [])
        self.assertEqual(fa.suggestions("Short bio", "Form", empty), [])


class ProfileLoadTests(unittest.TestCase):
    def test_bad_or_missing_file_is_an_empty_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "profile.json")
            self.assertEqual(fa.Profile.load(path), fa.Profile())
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not json")
            self.assertEqual(fa.Profile.load(path), fa.Profile())
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"email": "a@b.c", "skills": "One, Two", "password": "nope",
                           "extra": {"resume": "One.pdf|Two.pdf"}}, handle)
            profile = fa.Profile.load(path)
            self.assertEqual(profile.email, "a@b.c")
            self.assertEqual(profile.skills, ("One", "Two"))
            self.assertFalse(hasattr(profile, "password"))
            self.assertEqual(len(fa.suggestions("Resume", "Form", profile)), 2)


class PopupTests(unittest.TestCase):
    def test_a_click_offers_options_but_never_fills_until_a_pick(self) -> None:
        shown: list[tuple[str, list[str]]] = []
        picked: list[str] = []

        class FakePopup:
            def __init__(self, on_pick):
                self.on_pick = on_pick

            def show(self, label, options, x, y):
                shown.append((label, list(options)))

            def run(self):
                pass

            def stop(self):
                pass

        def fake_profile():
            return PROFILE

        assistant = fa.FieldAssistant(
            click_point=lambda: (40, 60),
            field_at=lambda x, y: ("Email address", "Internship application"),
            on_pick=picked.append,
            mouse_down=lambda: True,
        )
        assistant._profile = PROFILE
        with mock.patch.object(fa, "Profile", **{"load.return_value": PROFILE}), \
                mock.patch.object(fa, "FieldPopup", FakePopup):
            # First click shows the card; the same held click must not repeat.
            self.assertTrue(assistant.check())
            self.assertFalse(assistant.check())
            assistant._last = None
            self.assertTrue(assistant.check())
        self.assertEqual(shown[0][0], "Email address")
        self.assertIn("rose@example.com", shown[0][1])
        self.assertEqual(picked, [])   # nothing was filled automatically
        assistant._popup.on_pick(shown[0][1][0])
        self.assertEqual(picked, ["rose@example.com"])

    def test_holding_the_mouse_on_a_secret_shows_nothing(self) -> None:
        class FakePopup:
            def __init__(self, on_pick):
                pass

            def show(self, *args):
                raise AssertionError("no pop-up for a password field")

            def run(self):
                pass

            def stop(self):
                pass

        assistant = fa.FieldAssistant(
            click_point=lambda: (10, 10),
            field_at=lambda x, y: ("Password", "Sign in"),
            on_pick=lambda value: None,
            mouse_down=lambda: True,
        )
        with mock.patch.object(fa, "Profile", **{"load.return_value": PROFILE}), \
                mock.patch.object(fa, "FieldPopup", FakePopup):
            self.assertFalse(assistant.check())


if __name__ == "__main__":
    unittest.main()
