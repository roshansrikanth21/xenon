import os
import unittest
from unittest import mock

from xenon import killswitch
from xenon.killswitch import agent_allowed, feature_enabled, is_elevated, outward_allowed

FLAGS = ("XENON_AGENT", "XENON_OUTWARD", "XENON_SOMETHING")


class KillSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)  # restores the environment afterwards
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in FLAGS:
            os.environ.pop(key, None)

    def test_feature_flag_default_on_off_and_unknown(self) -> None:
        self.assertTrue(feature_enabled("something"))
        self.assertFalse(feature_enabled("something", default=False))
        for value, expected in (("on", True), ("1", True), ("off", False), ("0", False),
                                ("banana", False)):
            with self.subTest(value=value):
                os.environ["XENON_SOMETHING"] = value
                self.assertIs(feature_enabled("SOMETHING"), expected)
                self.assertIs(feature_enabled("XENON_SOMETHING"), expected)

    def test_agent_features_refused_when_elevated(self) -> None:
        with mock.patch.object(killswitch, "is_elevated", return_value=True):
            allowed, reason = agent_allowed()
        self.assertFalse(allowed)
        self.assertIn("administrator", reason)
        with mock.patch.object(killswitch, "is_elevated", return_value=False):
            self.assertEqual(agent_allowed(), (True, ""))

    def test_agent_kill_flag(self) -> None:
        os.environ["XENON_AGENT"] = "off"
        with mock.patch.object(killswitch, "is_elevated", return_value=False):
            allowed, reason = agent_allowed()
        self.assertFalse(allowed)
        self.assertIn("XENON_AGENT", reason)

    def test_outward_kill_flag_and_typo_is_off(self) -> None:
        self.assertEqual(outward_allowed(), (True, ""))
        os.environ["XENON_OUTWARD"] = "of"  # a typo must not leave the guard open
        allowed, reason = outward_allowed()
        self.assertFalse(allowed)
        self.assertIn("XENON_OUTWARD", reason)

    def test_is_elevated_answers_a_bool_here(self) -> None:
        self.assertIsInstance(is_elevated(), bool)


if __name__ == "__main__":
    unittest.main()
