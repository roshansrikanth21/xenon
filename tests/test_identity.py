"""One configured name drives both the wake word and the visible branding."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from xenon import identity


class IdentityTests(unittest.TestCase):
    def test_default_is_xenon(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(identity.wake_word(), "xenon")
            self.assertEqual(identity.display_name(), "Xenon")
            self.assertEqual(identity.standby_hint(), "say xenon")

    def test_xenon_name_sets_both(self) -> None:
        with mock.patch.dict(os.environ, {"XENON_NAME": "Rose"}, clear=True):
            self.assertEqual(identity.wake_word(), "rose")
            self.assertEqual(identity.display_name(), "Rose")
            self.assertEqual(identity.standby_hint(), "say rose")

    def test_wake_word_alone_also_renames_the_ui(self) -> None:
        with mock.patch.dict(os.environ, {"XENON_WAKE_WORD": "friday"}, clear=True):
            self.assertEqual(identity.display_name(), "Friday")

    def test_punctuation_and_case_are_tidied(self) -> None:
        with mock.patch.dict(os.environ, {"XENON_NAME": " Rose!"}, clear=True):
            self.assertEqual(identity.wake_word(), "rose")
        with mock.patch.dict(os.environ, {"XENON_NAME": "   "}, clear=True):
            self.assertEqual(identity.wake_word(), "xenon")


if __name__ == "__main__":
    unittest.main()
