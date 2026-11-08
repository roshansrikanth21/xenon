import math
import threading
import unittest

from xenon.orb_model import (
    ACTIONS,
    CENTER,
    CLOSE_DELAY_S,
    FAN_DEGREES,
    ORB_RADIUS,
    SATELLITE_DISTANCE,
    WINDOW,
    OrbModel,
    ease_out_back,
    fan_direction,
    satellite_angles,
)

try:
    from xenon.orb_render import OrbRenderer, premultiplied_bgra
except ImportError:  # Pillow is an optional extra
    OrbRenderer = None

import sys as _sys

try:
    from xenon.orb import OrbReply, Worker, shorten
except Exception:  # tkinter / wintypes may be missing off Windows
    if _sys.platform == "win32":
        raise  # on Windows a broken import is a bug, not a reason to skip
    Worker = None


def run(model: OrbModel, seconds: float, x=None, y=None) -> None:
    for _ in range(int(seconds * 60)):
        model.pointer(x, y)
        model.step(1 / 60)


class FanGeometryTests(unittest.TestCase):
    def test_fan_opens_toward_the_screen_centre(self) -> None:
        # Bottom-right corner of a 1920x1080 screen: open up and to the left.
        direction = fan_direction(1920 - WINDOW, 1080 - WINDOW, 1920, 1080)
        self.assertLess(math.cos(direction), 0)
        self.assertLess(math.sin(direction), 0)
        # Top-left corner: open down and to the right.
        direction = fan_direction(0, 0, 1920, 1080)
        self.assertGreater(math.cos(direction), 0)
        self.assertGreater(math.sin(direction), 0)

    def test_satellites_span_the_fan_evenly(self) -> None:
        angles = satellite_angles(0.0)
        self.assertEqual(len(angles), len(ACTIONS))
        self.assertAlmostEqual(math.degrees(angles[-1] - angles[0]), FAN_DEGREES)
        self.assertAlmostEqual(sum(angles) / len(angles), 0.0)

    def test_neighbouring_satellites_do_not_overlap(self) -> None:
        model = OrbModel()
        run(model, 1.0, CENTER, CENTER)
        points = [(s.x, s.y) for s in model.satellites]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            self.assertGreater(math.hypot(x1 - x0, y1 - y0), 34)

    def test_satellites_stay_inside_the_window(self) -> None:
        model = OrbModel()
        run(model, 1.0, CENTER, CENTER)
        for satellite in model.satellites:
            self.assertTrue(20 <= satellite.x <= WINDOW - 20)
            self.assertTrue(20 <= satellite.y <= WINDOW - 20)


class HoverTests(unittest.TestCase):
    def test_hovering_the_orb_fans_out_every_option(self) -> None:
        model = OrbModel()
        run(model, 1.0, CENTER, CENTER)
        self.assertEqual(model.progress, 1.0)
        for satellite in model.satellites:
            distance = math.hypot(satellite.x - CENTER, satellite.y - CENTER)
            self.assertAlmostEqual(distance, SATELLITE_DISTANCE, delta=1)
            self.assertAlmostEqual(satellite.alpha, 1.0)

    def test_pointer_far_away_keeps_it_closed(self) -> None:
        model = OrbModel()
        run(model, 1.0, 5, 5)
        self.assertEqual(model.progress, 0.0)

    def test_moving_onto_a_satellite_keeps_the_fan_open(self) -> None:
        model = OrbModel()
        run(model, 1.0, CENTER, CENTER)
        target = model.satellites[2]
        run(model, 1.0, target.x, target.y)
        self.assertEqual(model.progress, 1.0)
        self.assertEqual(model.hovered, target.action.key)
        self.assertEqual(model.center_label(), target.action.label)

    def test_leaving_closes_after_a_short_delay(self) -> None:
        model = OrbModel()
        run(model, 1.0, CENTER, CENTER)
        run(model, CLOSE_DELAY_S * 0.5, None, None)
        self.assertGreater(model.progress, 0.9)  # grace period
        run(model, 1.5, None, None)
        self.assertEqual(model.progress, 0.0)

    def test_hit_testing(self) -> None:
        model = OrbModel()
        self.assertEqual(model.hit(CENTER, CENTER), "orb")
        self.assertIsNone(model.hit(CENTER + ORB_RADIUS + 30, CENTER))
        run(model, 1.0, CENTER, CENTER)
        for satellite in model.satellites:
            self.assertEqual(model.hit(satellite.x, satellite.y), satellite.action.key)

    def test_closed_satellites_cannot_be_clicked(self) -> None:
        model = OrbModel()
        model.set_direction(0.0)
        self.assertNotIn(model.hit(CENTER + SATELLITE_DISTANCE, CENTER), {a.key for a in ACTIONS})

    def test_fold_keeps_it_shut_until_the_pointer_leaves(self) -> None:
        model = OrbModel()
        run(model, 1.0, CENTER, CENTER)
        model.fold()
        run(model, 1.0, CENTER, CENTER)
        self.assertEqual(model.progress, 0.0)
        run(model, 0.2, 2, 2)          # leave
        run(model, 1.0, CENTER, CENTER)  # come back
        self.assertEqual(model.progress, 1.0)


class StateTests(unittest.TestCase):
    def test_state_change_blends_colours(self) -> None:
        model = OrbModel()
        idle_core, _ = model.colors()
        model.set_state("listening")
        self.assertEqual(model.colors()[0], idle_core)  # starts from the old one
        run(model, 1.0)
        self.assertNotEqual(model.colors()[0], idle_core)

    def test_unknown_state_is_shown_as_an_error(self) -> None:
        model = OrbModel()
        model.set_state("nonsense")
        self.assertEqual(model.state, "error")

    def test_level_rises_fast_and_decays(self) -> None:
        model = OrbModel()
        model.set_level(0.9)
        self.assertEqual(model.level, 0.9)
        run(model, 1.0)
        self.assertEqual(model.level, 0.0)

    def test_voice_label_flips_while_listening(self) -> None:
        model = OrbModel()
        run(model, 1.0, CENTER, CENTER)
        voice = next(s for s in model.satellites if s.action.key == "voice")
        run(model, 0.2, voice.x, voice.y)
        self.assertEqual(model.center_label(), "Voice")
        model.voice_on = True
        self.assertEqual(model.center_label(), "Stop voice")

    def test_thinking_spins_faster_than_idle(self) -> None:
        model = OrbModel()
        idle = model.spin_speed()
        model.set_state("thinking")
        self.assertGreater(model.spin_speed(), idle)

    def test_state_age_resets_on_change_only(self) -> None:
        model = OrbModel()
        model.set_state("heard")
        run(model, 0.3)
        model.set_state("heard", "same state")
        self.assertAlmostEqual(model.state_age, 0.3, delta=0.02)
        model.set_state("thinking")
        self.assertEqual(model.state_age, 0.0)

    def test_every_state_has_a_label_and_colours(self) -> None:
        from xenon.orb_model import PALETTE, STATE_LABELS

        self.assertEqual(set(PALETTE), set(STATE_LABELS))
        model = OrbModel()
        model.set_state("standby")
        self.assertEqual(model.center_label(), "say xenon")

    def test_chime_is_short_enough_to_be_ignored_by_the_endpointer(self) -> None:
        from xenon.chime import CUES, SAMPLE_RATE, synthesize

        # The cues played while the mic is listening; the timer alarm is long
        # on purpose and cannot trigger anything without the wake word.
        for name in ("wake", "heard"):
            seconds = len(synthesize(CUES[name])) / 2 / SAMPLE_RATE
            self.assertLess(seconds, 0.2, name)  # min_speech_ms is 200

    def test_ease_out_back_overshoots_then_settles(self) -> None:
        self.assertEqual(ease_out_back(0), 0)
        self.assertAlmostEqual(ease_out_back(1), 1)
        self.assertGreater(max(ease_out_back(t / 20) for t in range(21)), 1.0)


@unittest.skipIf(OrbRenderer is None, "Pillow not installed")
class RendererTests(unittest.TestCase):
    def test_frame_is_transparent_outside_and_opaque_on_the_orb(self) -> None:
        renderer = OrbRenderer(1.0)
        model = OrbModel()
        run(model, 0.5)
        frame = renderer.render(model)
        self.assertEqual(frame.size, (WINDOW, WINDOW))
        self.assertEqual(frame.getpixel((0, 0))[3], 0)  # click-through corner
        self.assertEqual(frame.getpixel((int(CENTER), int(CENTER)))[3], 255)

    def test_open_fan_draws_satellites(self) -> None:
        renderer = OrbRenderer(1.0)
        model = OrbModel()
        run(model, 1.0, CENTER, CENTER)
        frame = renderer.render(model)
        for satellite in model.satellites:
            alpha = frame.getpixel((int(satellite.x), int(satellite.y)))[3]
            self.assertGreater(alpha, 200, satellite.action.key)

    def test_every_state_renders_at_high_dpi(self) -> None:
        renderer = OrbRenderer(1.5)
        model = OrbModel()
        model.voice_on = True
        for state in (
            "idle", "standby", "listening", "heard",
            "thinking", "searching", "speaking", "error",
        ):
            model.set_state(state)
            model.set_level(0.6)
            run(model, 0.1)
            self.assertEqual(renderer.render(model).size, (480, 480))

    def test_states_are_visually_distinct(self) -> None:
        """Each state's frame differs from idle by more than colour noise."""
        from PIL import ImageChops, ImageStat

        renderer = OrbRenderer(1.0)

        def frame(state: str):
            model = OrbModel()
            model.set_state(state)
            run(model, 1.0)
            return renderer.render(model).convert("RGB")

        idle = frame("idle")
        for state in ("standby", "listening", "thinking", "searching", "speaking"):
            difference = ImageStat.Stat(ImageChops.difference(idle, frame(state))).mean
            self.assertGreater(sum(difference), 6, state)

    def test_listening_draws_ripples_outside_the_orb(self) -> None:
        renderer = OrbRenderer(1.0)
        quiet, loud = OrbModel(), OrbModel()
        loud.set_state("listening")
        run(quiet, 0.5)
        run(loud, 0.5)
        def outside(model):
            # Sum alpha over the band between the orb edge and the window edge.
            image = renderer.render(model)
            return sum(
                image.getpixel((int(CENTER + math.cos(a / 24 * math.tau) * ring),
                                int(CENTER + math.sin(a / 24 * math.tau) * ring)))[3]
                for a in range(24)
                for ring in range(ORB_RADIUS + 6, ORB_RADIUS + 80, 2)
            )
        self.assertGreater(outside(loud), outside(quiet))

    def test_premultiplied_bgra_layout(self) -> None:
        from PIL import Image

        pixel = Image.new("RGBA", (1, 1), (200, 100, 50, 128))
        b, g, r, a = premultiplied_bgra(pixel)
        self.assertEqual(a, 128)
        self.assertAlmostEqual(r, 200 * 128 // 255, delta=1)
        self.assertAlmostEqual(b, 50 * 128 // 255, delta=1)


@unittest.skipIf(Worker is None, "orb shell unavailable on this platform")
class WorkerTests(unittest.TestCase):
    def test_jobs_run_on_one_thread_with_the_backend(self) -> None:
        threads = []

        class Backend:
            def __init__(self):
                threads.append(threading.get_ident())

            def ask(self, text):
                threads.append(threading.get_ident())
                return OrbReply(text.upper())

            def close(self):
                pass

        done = threading.Event()
        replies = []
        worker = Worker(Backend)
        worker.submit(lambda backend: (replies.append(backend.ask("hi")), done.set()), lambda m: None)
        self.assertTrue(done.wait(2))
        worker.stop()
        self.assertEqual(replies[0].text, "HI")
        self.assertEqual(len(set(threads)), 1)
        self.assertNotEqual(threads[0], threading.get_ident())

    def test_a_backend_that_fails_to_start_reports_every_job(self) -> None:
        def broken():
            raise RuntimeError("no microphone")

        failed = threading.Event()
        messages = []
        worker = Worker(broken)
        worker.submit(lambda backend: None, lambda m: (messages.append(m), failed.set()))
        self.assertTrue(failed.wait(2))
        worker.stop()
        self.assertIn("no microphone", messages[0])

    def test_a_failing_job_reports_instead_of_hanging(self) -> None:
        class Backend:
            def ask(self, text):
                raise ValueError("model down")

            def close(self):
                pass

        failed = threading.Event()
        messages = []
        worker = Worker(Backend)
        worker.submit(lambda b: b.ask("x"), lambda m: (messages.append(m), failed.set()))
        self.assertTrue(failed.wait(2))
        worker.stop()
        self.assertEqual(messages, ["model down"])

    def test_shorten_cuts_on_a_word(self) -> None:
        self.assertEqual(shorten("short", 50), "short")
        self.assertEqual(shorten("one two three four", 10), "one two…")


def _can_open_orb() -> bool:
    import sys

    return Worker is not None and sys.platform == "win32"


@unittest.skipUnless(_can_open_orb(), "needs a Windows desktop")
class OrbAppTests(unittest.TestCase):
    """Drives the real window with fake backends (no model, no network)."""

    def make(self, backend):
        import time as _time

        from xenon import orb

        import tkinter

        try:
            app = orb.OrbApp(lambda: backend, None, tray=False)
        except tkinter.TclError as error:  # no interactive desktop
            self.skipTest(f"Tk unavailable: {error}")
        if app._hotkeys is not None:  # don't fight a running orb for the keys
            app._hotkeys.stop()
            app._hotkeys = None

        def pump(seconds=1.0):
            end = _time.time() + seconds
            while _time.time() < end:
                app.root.update()
                _time.sleep(0.02)

        self.addCleanup(lambda: (app._worker.stop(1), app.window.close(), app.root.destroy()))
        return app, pump

    def test_a_failing_job_shows_an_error_card_with_the_reason(self) -> None:
        class Broken:
            def screen(self):
                raise RuntimeError("No model configured. test")

            def close(self):
                pass

        app, pump = self.make(Broken())
        app._run_screen()
        pump(1.0)
        self.assertEqual(app.model.state, "error")
        self.assertTrue(app.panel.visible)
        self.assertIn("No model configured", app.panel.text.get("1.0", "end"))

    def test_timers_show_a_ring_and_a_card_when_done(self) -> None:
        from xenon import quick

        class Idle:
            def close(self):
                pass

        import os
        from unittest import mock

        patcher = mock.patch.dict(os.environ, {"XENON_CHIME": "off"})  # tests stay silent
        patcher.start()
        self.addCleanup(patcher.stop)
        app, pump = self.make(Idle())
        self.assertIsNone(app.model.timer_fraction)
        timer = quick.TIMERS.add(600, "tea")
        self.addCleanup(quick.TIMERS.cancel_all)
        pump(0.6)
        self.assertGreater(app.model.timer_fraction, 0.99)
        self.assertIsNotNone(app.renderer.render(app.model))
        quick.TIMERS.cancel_all()
        app._timer_done(timer)  # what the timer thread posts when it fires
        pump(0.3)
        self.assertTrue(app.panel.visible)
        self.assertIn("Tea is done", app.panel.text.get("1.0", "end"))
        self.assertIsNone(app.model.timer_fraction)

    def test_hide_to_tray_and_back(self) -> None:
        class Idle:
            def close(self):
                pass

        app, pump = self.make(Idle())
        pump(0.2)
        app.toggle_hidden()
        pump(0.3)
        self.assertTrue(app.hidden)
        self.assertFalse(app.root.winfo_viewable())
        app._action("ask")  # asking brings it back so the pill has an anchor
        pump(0.3)
        self.assertFalse(app.hidden)
        self.assertTrue(app.pill.visible)

    def test_search_results_render_as_links(self) -> None:
        class Searcher:
            def search(self, query):
                return [OrbLinkType("Result one", "https://example.com/1", "snippet")]

            def close(self):
                pass

        from xenon.orb import OrbLink as OrbLinkType

        app, pump = self.make(Searcher())
        app._run_search("anything")
        pump(1.0)
        self.assertTrue(app.panel.visible)
        self.assertIn("Result one", app.panel.text.get("1.0", "end"))
        self.assertIn(app.model.state, {"idle", "searching"})


if __name__ == "__main__":
    unittest.main()
