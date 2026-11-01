import unittest
from unittest import mock

from xenon.audio import (
    MicrophoneSource,
    SapiSpeaker,
    UtteranceDetector,
    WhisperTranscriber,
    rms,
    to_pcm16,
)

QUIET = [0.0005] * 480
LOUD = [0.2] * 480


def frames(count: int, value):
    return [list(value) for _ in range(count)]


class RmsTests(unittest.TestCase):
    def test_silence_is_near_zero(self) -> None:
        self.assertLess(rms(QUIET), 0.001)

    def test_speech_is_above_a_typical_threshold(self) -> None:
        self.assertGreater(rms(LOUD), 0.01)

    def test_empty_frame_is_zero(self) -> None:
        self.assertEqual(rms([]), 0.0)


class PcmConversionTests(unittest.TestCase):
    def test_clips_out_of_range_samples(self) -> None:
        pcm = to_pcm16([2.0, -2.0, 0.0])

        self.assertEqual(list(pcm), [32767, -32767, 0])


class UtteranceDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = UtteranceDetector(
            sample_rate=16000,
            frame_ms=30,
            threshold=0.012,
            silence_ms=90,
            min_speech_ms=60,
            max_utterance_ms=600,
            pre_roll_ms=60,
        )

    def feed_all(self, sequence):
        results = [self.detector.feed(frame) for frame in sequence]
        return [result for result in results if result is not None]

    def test_silence_produces_no_utterance(self) -> None:
        self.assertEqual(self.feed_all(frames(20, QUIET)), [])

    def test_speech_followed_by_silence_produces_one_utterance(self) -> None:
        results = self.feed_all(frames(5, LOUD) + frames(5, QUIET))

        self.assertEqual(len(results), 1)
        # 5 speech frames + the 3-frame silence window
        self.assertEqual(len(results[0]), 8 * 480 * 2)

    def test_a_blip_shorter_than_min_speech_is_discarded(self) -> None:
        self.assertEqual(self.feed_all(frames(1, LOUD) + frames(5, QUIET)), [])

    def test_pre_roll_includes_frames_before_the_onset(self) -> None:
        # Two quiet frames fit in the 60 ms pre-roll and are kept.
        results = self.feed_all(frames(2, QUIET) + frames(5, LOUD) + frames(5, QUIET))

        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]), (2 + 5 + 3) * 480 * 2)

    def test_pre_roll_is_bounded_by_its_window(self) -> None:
        for _ in range(6):
            self.detector.feed(QUIET)

        # 60 ms pre-roll at 30 ms per frame keeps exactly two frames.
        self.assertEqual(len(self.detector._pre_roll), 2)

    def test_long_speech_is_cut_at_the_maximum(self) -> None:
        results = self.feed_all(frames(40, LOUD))

        # Continuous speech yields consecutive capped chunks, never one
        # unbounded recording.
        self.assertGreaterEqual(len(results), 1)
        for chunk in results:
            self.assertLessEqual(len(chunk), 20 * 480 * 2)

    def test_one_utterance_does_not_retrigger_on_its_own_tail(self) -> None:
        self.feed_all(frames(5, LOUD) + frames(5, QUIET))

        self.assertEqual(self.feed_all(frames(3, QUIET)), [])
        self.assertFalse(self.detector.speaking)

    def test_second_utterance_is_detected_separately(self) -> None:
        first = self.feed_all(frames(5, LOUD) + frames(5, QUIET))
        second = self.feed_all(frames(5, LOUD) + frames(5, QUIET))

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)

    def test_reset_clears_state(self) -> None:
        self.detector.feed(LOUD)
        self.assertTrue(self.detector.speaking)

        self.detector.reset()

        self.assertFalse(self.detector.speaking)


class FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeWhisperModel:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append({"audio": audio, **kwargs})
        return [FakeSegment(" xeno "), FakeSegment("open notepad ")], None


class WhisperTranscriberTests(unittest.TestCase):
    def setUp(self) -> None:
        # Decoding PCM to a float array needs numpy, which the voice extra
        # installs. The unit tests only exercise orchestration.
        patcher = mock.patch("xenon.audio._pcm_to_float", lambda pcm: [0.0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loads_the_model_lazily_and_joins_segments(self) -> None:
        created: list[tuple[str, str, str]] = []
        model = FakeWhisperModel()

        def factory(name, device, compute_type):
            created.append((name, device, compute_type))
            return model

        transcriber = WhisperTranscriber("small", model_factory=factory)
        self.assertEqual(created, [])

        text = transcriber.transcribe(b"\x00\x00" * 480, 16000)

        self.assertEqual(text, "xeno open notepad")
        self.assertEqual(created, [("small", "cpu", "int8")])
        self.assertFalse(model.calls[0]["condition_on_previous_text"])

    def test_reuses_one_model_across_utterances(self) -> None:
        created: list[int] = []
        model = FakeWhisperModel()

        def factory(*args):
            created.append(1)
            return model

        transcriber = WhisperTranscriber(model_factory=factory)
        transcriber.transcribe(b"\x00\x00", 16000)
        transcriber.transcribe(b"\x00\x00", 16000)

        self.assertEqual(len(created), 1)

    def test_initial_prompt_biases_the_spelling_of_the_wake_word(self) -> None:
        model = FakeWhisperModel()

        WhisperTranscriber(
            initial_prompt="Xenon.", model_factory=lambda *a: model
        ).transcribe(b"\x00\x00", 16000)

        self.assertEqual(model.calls[0]["initial_prompt"], "Xenon.")

    def test_no_initial_prompt_is_sent_when_unset(self) -> None:
        model = FakeWhisperModel()

        WhisperTranscriber(model_factory=lambda *a: model).transcribe(b"\x00\x00", 16000)

        self.assertNotIn("initial_prompt", model.calls[0])


class FakeSapiEngine:
    def __init__(self) -> None:
        self.spoken: list[tuple[str, int]] = []
        self.Rate = 0
        self.Volume = 100

    def Speak(self, text: str, flags: int) -> None:  # noqa: N802 - SAPI naming
        self.spoken.append((text, flags))


class SapiSpeakerTests(unittest.TestCase):
    def test_speaks_synchronously_and_trims_text(self) -> None:
        engine = FakeSapiEngine()

        SapiSpeaker(engine=engine).speak("  hello there  ")

        self.assertEqual(engine.spoken, [("hello there", 0)])

    def test_ignores_empty_text(self) -> None:
        engine = FakeSapiEngine()

        SapiSpeaker(engine=engine).speak("   ")

        self.assertEqual(engine.spoken, [])

    def test_applies_rate_and_volume(self) -> None:
        engine = FakeSapiEngine()

        SapiSpeaker(rate=2, volume=80, engine=engine)

        self.assertEqual(engine.Rate, 2)
        self.assertEqual(engine.Volume, 80)


class FakeMicStream:
    def __init__(self, values) -> None:
        self.values = values
        self.reads = 0

    def read(self, count):
        self.reads += 1
        return FakeArray(self.values[:count]), False

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeArray:
    def __init__(self, values) -> None:
        self._values = values

    def reshape(self, *shape):
        return self

    def __iter__(self):
        return iter(self._values)


class MicrophoneSourceTests(unittest.TestCase):
    def test_reads_frames_of_the_requested_size(self) -> None:
        stream = FakeMicStream([0.5] * 480)

        source = MicrophoneSource(stream_factory=lambda rate, size: stream)
        frame = source.read_frame()

        self.assertEqual(len(frame), 480)
        self.assertEqual(source.sample_rate, 16000)

    def test_opens_the_stream_once(self) -> None:
        stream = FakeMicStream([0.1] * 480)
        opened: list[int] = []

        def factory(rate, size):
            opened.append(size)
            return stream

        source = MicrophoneSource(stream_factory=factory)
        source.read_frame()
        source.read_frame()

        self.assertEqual(len(opened), 1)
        self.assertEqual(stream.reads, 2)

    def test_close_is_idempotent(self) -> None:
        source = MicrophoneSource(stream_factory=lambda r, s: FakeMicStream([0.0]))

        source.close()
        source.close()


class ListenOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = UtteranceDetector(
            sample_rate=16000,
            frame_ms=30,
            threshold=0.012,
            silence_ms=90,
            min_speech_ms=60,
        )

    def source(self, sequence):
        state = {"index": 0}

        class Source:
            sample_rate = 16000

            def read_frame(self):
                frame = sequence[state["index"]]
                state["index"] += 1
                return frame

        return Source()

    def test_transcribes_the_first_utterance(self) -> None:
        from xenon.audio import listen_once

        sequence = frames(3, QUIET) + frames(5, LOUD) + frames(5, QUIET)
        transcribed: list[int] = []

        class FakeTranscriber:
            def transcribe(self, pcm, sample_rate):
                transcribed.append(len(pcm))
                return "  xenon open notepad  "

        text = listen_once(self.source(sequence), self.detector, FakeTranscriber())

        self.assertEqual(text, "xenon open notepad")
        self.assertEqual(len(transcribed), 1)

    def test_empty_transcript_becomes_none(self) -> None:
        from xenon.audio import listen_once

        sequence = frames(5, LOUD) + frames(5, QUIET)

        class MuteTranscriber:
            def transcribe(self, pcm, sample_rate):
                return "   "

        self.assertIsNone(listen_once(self.source(sequence), self.detector, MuteTranscriber()))


if __name__ == "__main__":
    unittest.main()
