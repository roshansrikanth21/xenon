import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from xenon import xtreme


class _Proc:
    def __init__(self, name, exe="", parents=()):
        self._name, self._exe, self._parents = name, exe, parents

    def name(self):
        return self._name

    def exe(self):
        return self._exe

    def parents(self):
        return [_Proc(n) for n in self._parents]


class RankTests(unittest.TestCase):
    def test_heavy_windowed_apps_only_heaviest_first(self):
        usage = {"chrome.exe": (2400, 12.0), "discord.exe": (600, 1.0), "tiny.exe": (50, 1.0),
                 "busy.exe": (80, 40.0), "service.exe": (3000, 50.0)}
        windowed = {"chrome.exe": "Chrome", "discord.exe": "Discord", "tiny.exe": "x", "busy.exe": "b"}
        out = xtreme.rank_heavy(usage, windowed, skip=set())
        self.assertEqual([a.exe for a in out], ["chrome.exe", "busy.exe", "discord.exe"])
        self.assertEqual(out[0].name, "Chrome")
        # no window -> cannot be closed politely -> not offered
        self.assertNotIn("service.exe", [a.exe for a in out])

    def test_never_offers_protected_launchers_or_skipped(self):
        usage = {"explorer.exe": (900, 30.0), "steam.exe": (900, 30.0), "vgc.exe": (900, 30.0),
                 "notepad.exe": (900, 30.0), "game.exe": (5000, 90.0), "chrome.exe": (900, 1.0)}
        windowed = {k: k for k in usage}
        out = xtreme.rank_heavy(usage, windowed, skip={"game.exe"})
        self.assertEqual([a.exe for a in out], ["chrome.exe"])

    def test_games_are_recognised(self):
        self.assertTrue(xtreme.is_game(_Proc("FortniteClient-Win64-Shipping.exe")))
        self.assertTrue(xtreme.is_game(_Proc("cs2.exe", r"D:\Steam\steamapps\common\cs2\cs2.exe")))
        self.assertTrue(xtreme.is_game(_Proc("valorant.exe", r"C:\Riot Games\VALORANT\valorant.exe")))
        self.assertTrue(xtreme.is_game(_Proc("whatever.exe", r"C:\x\whatever.exe", parents=("steam.exe",))))
        self.assertFalse(xtreme.is_game(_Proc("chrome.exe", r"C:\Program Files\Google\chrome.exe",
                                              parents=("explorer.exe",))))

    def test_launcher_children_found_through_one_tree(self):
        tree = {10: (1, "explorer.exe"), 20: (10, "epicgameslauncher.exe"), 30: (20, "bootstrap.exe"),
                40: (30, "game.exe"), 50: (10, "chrome.exe"), 60: (60, "loop.exe")}

        class P(_Proc):
            def __init__(self, pid, name):
                super().__init__(name, r"C:\x\%s" % name)
                self.pid = pid

        self.assertTrue(xtreme.is_game(P(40, "game.exe"), tree))       # grandchild of a launcher
        self.assertFalse(xtreme.is_game(P(50, "chrome.exe"), tree))
        self.assertFalse(xtreme.is_game(P(60, "loop.exe"), tree))      # a pid that is its own parent
        self.assertTrue(xtreme.is_game(P(70, "FortniteClient-Win64-Shipping_EAC_EOS.exe"), tree))

    def test_guid_round_trip(self):
        g = xtreme._GUID.of("ded574b5-45a0-4f42-8737-46345c09c238")
        self.assertEqual(g.text(), "ded574b5-45a0-4f42-8737-46345c09c238")

    def test_restore_tolerates_nothing_saved(self):
        xtreme.restore_power(None)
        xtreme.restore_priorities({})

    def test_purge_needs_admin(self):
        with mock.patch.object(xtreme, "is_admin", return_value=False):
            self.assertFalse(xtreme.purge_standby())


@unittest.skipUnless(sys.platform == "win32", "needs a Windows desktop")
class OrbXtremeTests(unittest.TestCase):
    def make(self):
        import tkinter

        from xenon import orb

        if orb.Worker is None:
            self.skipTest("orb worker unavailable")
        env = mock.patch.dict(os.environ, {"XENON_COMPANION": "off", "APPDATA": tempfile.mkdtemp()})
        env.start()
        self.addCleanup(env.stop)
        try:
            app = orb.OrbApp(lambda: None, None, tray=False)
        except tkinter.TclError as error:
            self.skipTest(f"Tk unavailable: {error}")
        if app._hotkeys is not None:
            app._hotkeys.stop()
            app._hotkeys = None

        def pump(seconds=0.5, until=None):
            end = time.time() + seconds
            while time.time() < end:
                app.root.update()
                if until is not None and until():
                    return
                time.sleep(0.02)

        self.addCleanup(lambda: (app._worker.stop(1), app.window.close(), app.root.destroy()))
        return app, pump

    def patched(self, heavy):
        calls = {"closed": [], "power": 0, "restored": []}
        patches = [
            mock.patch.object(xtreme, "find_heavy", lambda keep=(): heavy),
            mock.patch.object(xtreme, "set_best_performance",
                              lambda: calls.__setitem__("power", calls["power"] + 1) or {"overlay": "x"}),
            mock.patch.object(xtreme, "restore_power", lambda saved: calls["restored"].append(saved)),
            mock.patch.object(xtreme, "calm_background", lambda keep=(): {}),
            mock.patch.object(xtreme, "restore_priorities", lambda changed: None),
            mock.patch.object(xtreme, "trim_background", lambda skip=(): 0),
            mock.patch.object(xtreme, "purge_standby", lambda: False),
            mock.patch("xenon.game_mode.close_apps", lambda names: calls["closed"].extend(names) or list(names)),
            mock.patch.object(xtreme, "free_memory_mb", lambda: 1000),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_confirmed_xtreme_closes_tunes_and_undoes_on_leave(self):
        app, pump = self.make()
        heavy = [xtreme.HeavyApp("chrome.exe", "Chrome", 2400, 12.0)]
        calls = self.patched(heavy)
        titles = []
        original = app.task_choice
        app.task_choice = lambda pid, title, *a, **k: (titles.append(title), original(pid, title, *a, **k))
        app.enter_xtreme_mode()
        pump(3.0, until=lambda: "xtreme" in app._choice_cards)
        self.assertIn("Chrome (2.3 GB)", titles[0])      # the card names what will close
        card = app._choice_cards["xtreme"]
        card._on_pick(card.choices[0])
        pump(6.0, until=lambda: calls["power"] == 1)
        self.assertTrue(app.game_mode)
        self.assertTrue(app.xtreme)
        self.assertEqual(calls["closed"], ["chrome.exe"])
        self.assertEqual(calls["power"], 1)
        app.toggle_xtreme_mode()                      # leave
        pump(3.0, until=lambda: calls["restored"])
        self.assertFalse(app.game_mode)
        self.assertFalse(app.xtreme)
        self.assertEqual(calls["restored"], [{"overlay": "x"}])

    def test_keep_apps_open_choice_closes_nothing(self):
        app, pump = self.make()
        calls = self.patched([xtreme.HeavyApp("chrome.exe", "Chrome", 2400, 12.0)])
        app.enter_xtreme_mode()
        pump(3.0, until=lambda: "xtreme" in app._choice_cards)
        card = app._choice_cards["xtreme"]
        card._on_pick(card.choices[1])
        pump(6.0, until=lambda: calls["power"] == 1)
        self.assertTrue(app.xtreme)
        self.assertEqual(calls["closed"], [])

    def test_dismissing_the_card_changes_nothing(self):
        app, pump = self.make()
        calls = self.patched([xtreme.HeavyApp("chrome.exe", "Chrome", 2400, 12.0)])
        app.enter_xtreme_mode()
        pump(3.0, until=lambda: "xtreme" in app._choice_cards)
        app._choice_cards["xtreme"]._on_pick(None)
        pump(0.5)
        self.assertFalse(app.game_mode)
        self.assertFalse(app.xtreme)
        self.assertEqual(calls["power"], 0)
        self.assertEqual(calls["closed"], [])


if __name__ == "__main__":
    unittest.main()
