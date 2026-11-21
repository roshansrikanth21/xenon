"""The real-hardware verifier: honest PASS/FAIL/SKIP, no fake adapters here."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from xenon import verify


class ReportTests(unittest.TestCase):
    def test_all_pass_exits_zero(self) -> None:
        checks = [verify.Check("one", verify.PASS, "ok"), verify.Check("two", verify.SKIP, "later")]
        self.assertEqual(verify.report(checks), 0)

    def test_any_failure_exits_one(self) -> None:
        checks = [verify.Check("one", verify.PASS, "ok"), verify.Check("two", verify.FAIL, "broken")]
        self.assertEqual(verify.report(checks), 1)

    def test_lines_carry_the_status_and_name(self) -> None:
        line = verify.Check("mic", verify.PASS, "2 devices").line()
        self.assertIn("PASS", line)
        self.assertIn("mic", line)
        self.assertIn("2 devices", line)


class PlatformTests(unittest.TestCase):
    def test_non_windows_is_reported_as_failed(self) -> None:
        with mock.patch.object(verify.sys, "platform", "linux"):
            self.assertEqual(verify.check_platform().status, verify.FAIL)

    def test_windows_is_a_pass(self) -> None:
        with mock.patch.object(verify.sys, "platform", "win32"):
            self.assertEqual(verify.check_platform().status, verify.PASS)


class ProfileTests(unittest.TestCase):
    def test_a_missing_profile_is_skipped_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as appdata:
            with mock.patch.dict(os.environ, {"APPDATA": appdata}):
                self.assertEqual(verify.check_profile().status, verify.SKIP)

    def test_an_existing_profile_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as appdata:
            folder = os.path.join(appdata, "xenon")
            os.makedirs(folder)
            open(os.path.join(folder, "profile.json"), "w", encoding="utf-8").write("{}")
            with mock.patch.dict(os.environ, {"APPDATA": appdata}):
                check = verify.check_profile()
            self.assertEqual(check.status, verify.PASS)
            self.assertIn("profile.json", check.detail)


class DuckTests(unittest.TestCase):
    def test_without_pycaw_the_check_is_skipped(self) -> None:
        from xenon.duck import NullDucker

        with mock.patch("xenon.duck.make_ducker", return_value=NullDucker()):
            self.assertEqual(verify.check_ducker().status, verify.SKIP)

    def test_read_only_does_not_touch_volume(self) -> None:
        class FakeDucker:
            ducked = False

            def __init__(self):
                self.calls = []

            def duck(self, to=0.15):
                self.calls.append(("duck", to))
                return 2

            def restore(self):
                self.calls.append(("restore",))

        ducker = FakeDucker()
        with mock.patch("xenon.duck.make_ducker", return_value=ducker):
            check = verify.check_ducker(duck=False)
        self.assertEqual(check.status, verify.PASS)
        self.assertEqual(ducker.calls, [])

    def test_exercise_ducks_and_restores(self) -> None:
        class FakeDucker:
            ducked = False

            def __init__(self):
                self.calls = []

            def duck(self, to=0.15):
                self.calls.append("duck")
                return 3

            def restore(self):
                self.calls.append("restore")

        ducker = FakeDucker()
        with mock.patch("xenon.duck.make_ducker", return_value=ducker):
            check = verify.check_ducker(duck=True, seconds=0.2)
        self.assertEqual(check.status, verify.PASS)
        self.assertIn("3 app session", check.detail)
        self.assertEqual(ducker.calls, ["duck", "restore"])


class RunTests(unittest.TestCase):
    def test_run_returns_one_result_per_check(self) -> None:
        with mock.patch.multiple(
            verify,
            check_platform=mock.Mock(return_value=verify.Check("p", verify.PASS, "")),
            check_voice_deps=mock.Mock(return_value=verify.Check("v", verify.PASS, "")),
            check_sapi=mock.Mock(return_value=verify.Check("s", verify.PASS, "")),
            check_microphone=mock.Mock(return_value=verify.Check("m", verify.PASS, "")),
            check_ducker=mock.Mock(return_value=verify.Check("d", verify.PASS, "")),
            check_uia=mock.Mock(return_value=verify.Check("u", verify.PASS, "")),
            check_profile=mock.Mock(return_value=verify.Check("f", verify.PASS, "")),
        ):
            checks = verify.run()
        self.assertEqual(len(checks), 7)
        self.assertTrue(all(check.status == verify.PASS for check in checks))


if __name__ == "__main__":
    unittest.main()
