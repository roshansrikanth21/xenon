"""Saved microphone/speaker choices and how they resolve to devices."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from xenon import audio_settings


class SettingsFileTests(unittest.TestCase):
    def test_missing_file_means_system_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = audio_settings.load(os.path.join(folder, "audio.json"))
        self.assertEqual(settings, audio_settings.AudioSettings())

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "nested", "audio.json")
            written = audio_settings.save(
                audio_settings.AudioSettings(input="Microphone Array", output="Speakers"),
                path,
            )
            self.assertEqual(written, path)
            self.assertEqual(audio_settings.load(path).input, "Microphone Array")
            self.assertEqual(audio_settings.load(path).output, "Speakers")

    def test_bad_json_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "audio.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(audio_settings.load(path).input, "")

    def test_unknown_keys_and_non_strings_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "audio.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"input": 3, "output": None, "bogus": "x"}, handle)
            settings = audio_settings.load(path)
        self.assertEqual(settings, audio_settings.AudioSettings(input="3"))


class ResolveTests(unittest.TestCase):
    DEVICES = [(0, "Microsoft Sound Mapper - Input"), (1, "Microphone Array (Realtek)"), (5, "USB Mic")]

    def test_an_index_works(self) -> None:
        self.assertEqual(audio_settings.resolve_input("5", self.DEVICES), 5)
        self.assertEqual(audio_settings.resolve_input(1, self.DEVICES), 1)

    def test_a_name_fragment_is_case_insensitive(self) -> None:
        self.assertEqual(audio_settings.resolve_input("realtek", self.DEVICES), 1)
        self.assertEqual(audio_settings.resolve_input("usb", self.DEVICES), 5)

    def test_unknown_or_empty_returns_none(self) -> None:
        self.assertIsNone(audio_settings.resolve_input("no such mic", self.DEVICES))
        self.assertIsNone(audio_settings.resolve_input("", self.DEVICES))
        self.assertIsNone(audio_settings.resolve_input(None, self.DEVICES))

    def test_environment_overrides_the_saved_choice(self) -> None:
        with mock.patch.object(audio_settings, "input_devices", return_value=self.DEVICES), \
             mock.patch.object(audio_settings, "load", return_value=audio_settings.AudioSettings(input="usb")):
            self.assertEqual(audio_settings.current_input(), 5)
            with mock.patch.dict(os.environ, {"XENON_MIC_DEVICE": "realtek"}):
                self.assertEqual(audio_settings.current_input(), 1)


if __name__ == "__main__":
    unittest.main()
