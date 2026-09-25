"""Tests for the speech-path maths that used to sit untested inside main.py.

Loudness drives the HUD waveform, the viseme frames drive the avatar's mouth,
and the transcript helpers decide what the user sees written down. None of it
raises when it goes wrong — the mouth simply stops matching the words, or a
sentence appears twice — which is exactly the kind of failure that survives for
months without tests.
"""
from __future__ import annotations

import unittest

try:
    import numpy as np

    from core import speech_shaping

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - numpy is an optional install
    np = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


@unittest.skipIf(np is None, f"numpy is unavailable ({_IMPORT_ERROR})")
class LoudnessTests(unittest.TestCase):
    def test_silence_reads_as_zero(self) -> None:
        self.assertEqual(speech_shaping._pcm_level(np.zeros(480)), 0.0)

    def test_room_noise_below_the_floor_reads_as_zero(self) -> None:
        self.assertEqual(speech_shaping._pcm_level(np.full(480, 30.0)), 0.0)

    def test_a_loud_block_saturates_at_one(self) -> None:
        self.assertEqual(speech_shaping._pcm_level(np.full(480, 20_000.0)), 1.0)

    def test_speech_lands_between_the_extremes(self) -> None:
        level = speech_shaping._pcm_level(np.full(480, 800.0))
        self.assertGreater(level, 0.0)
        self.assertLess(level, 1.0)

    def test_louder_input_never_reads_quieter(self) -> None:
        levels = [speech_shaping._pcm_level(np.full(480, value))
                  for value in (100, 400, 900, 1600, 2500)]
        self.assertEqual(levels, sorted(levels))

    def test_invalid_input_cannot_raise(self) -> None:
        for value in ([], None, "not audio", object()):
            self.assertEqual(speech_shaping._pcm_level(value), 0.0)


@unittest.skipIf(np is None, f"numpy is unavailable ({_IMPORT_ERROR})")
class VisemeTests(unittest.TestCase):
    def _tone(self, hz: float, seconds: float = 1.0, amplitude: float = 4000.0):
        samples = np.arange(int(24_000 * seconds))
        return np.sin(samples / 24_000 * 2 * np.pi * hz) * amplitude

    def test_one_frame_is_produced_every_twenty_milliseconds(self) -> None:
        frames = speech_shaping._pcm_visemes(self._tone(700))
        self.assertEqual(len(frames), 50, "a second of audio must yield 50 frames")

    def test_the_whole_block_is_covered(self) -> None:
        """Stepping only while a full window fits used to drop the last 20% of
        every batch, so the mouth ran out of frames before the sound stopped."""
        frames = speech_shaping._pcm_visemes(self._tone(700, seconds=0.2))
        self.assertEqual(len(frames), 10)

    def test_a_block_shorter_than_the_window_yields_nothing(self) -> None:
        self.assertEqual(speech_shaping._pcm_visemes(np.zeros(100)), [])

    def test_silence_produces_closed_frames(self) -> None:
        frames = speech_shaping._pcm_visemes(np.zeros(24_000))
        self.assertTrue(frames)
        self.assertTrue(all(frame == (0.0, 0.0, 0.0) for frame in frames))

    def test_an_open_vowel_reads_more_open_than_a_close_one(self) -> None:
        # F1 near 800 Hz is an open jaw; F1 near 300 Hz is a closed one.
        open_frames = speech_shaping._pcm_visemes(self._tone(800))
        close_frames = speech_shaping._pcm_visemes(self._tone(300))
        open_mean = sum(frame[1] for frame in open_frames) / len(open_frames)
        close_mean = sum(frame[1] for frame in close_frames) / len(close_frames)
        self.assertGreater(open_mean, close_mean)

    def test_every_value_stays_in_range(self) -> None:
        for hz in (200, 500, 1200, 2500, 5000):
            for level, openness, width in speech_shaping._pcm_visemes(self._tone(hz)):
                self.assertTrue(0.0 <= level <= 1.0)
                self.assertTrue(0.0 <= openness <= 1.0)
                self.assertTrue(-1.0 <= width <= 1.0)

    def test_a_fricative_closes_the_mouth(self) -> None:
        """Hiss is made with a nearly shut mouth, so it must not read as open."""
        noise = np.random.default_rng(0).normal(0, 3000, 24_000)
        high = noise * 0  # start from silence and add only high-frequency energy
        samples = np.arange(24_000)
        for hz in (4200, 5600, 7000):
            high = high + np.sin(samples / 24_000 * 2 * np.pi * hz) * 3000
        hiss_frames = speech_shaping._pcm_visemes(high)
        vowel_frames = speech_shaping._pcm_visemes(self._tone(800))
        hiss_open = sum(f[1] for f in hiss_frames) / len(hiss_frames)
        vowel_open = sum(f[1] for f in vowel_frames) / len(vowel_frames)
        self.assertLess(hiss_open, vowel_open)

    def test_invalid_input_cannot_raise(self) -> None:
        for value in (None, "not audio", object()):
            self.assertEqual(speech_shaping._pcm_visemes(value), [])


@unittest.skipIf(np is None, f"numpy is unavailable ({_IMPORT_ERROR})")
class TranscriptTests(unittest.TestCase):
    def test_control_markers_and_bytes_are_removed(self) -> None:
        self.assertEqual(
            speech_shaping._clean_transcript("  hi <ctrl99>there\x07\x1b  "), "hi there"
        )

    def test_an_enormous_transcript_is_capped(self) -> None:
        self.assertEqual(len(speech_shaping._clean_transcript("x" * 50_000)), 20_000)

    def test_a_repeated_long_chunk_is_detected(self) -> None:
        buffer = ["the meeting is at four o'clock"]
        self.assertTrue(
            speech_shaping._is_repeat_chunk("the meeting is at four o'clock", buffer)
        )

    def test_a_new_chunk_is_not_a_repeat(self) -> None:
        self.assertFalse(
            speech_shaping._is_repeat_chunk("something else entirely", ["first part"])
        )

    def test_a_short_word_may_legitimately_repeat(self) -> None:
        """"evet, evet" is a real thing to say; only long chunks are deduped."""
        self.assertFalse(speech_shaping._is_repeat_chunk("evet", ["evet", "hayir"]))

    def test_an_immediate_short_repeat_is_still_caught(self) -> None:
        self.assertTrue(speech_shaping._is_repeat_chunk("evet", ["evet"]))

    def test_the_buffer_is_bounded(self) -> None:
        buffer: list[str] = []
        for _ in range(50):
            speech_shaping._append_transcript(buffer, "y" * 1_000)
        self.assertLessEqual(sum(len(part) for part in buffer), 21_000)

    def test_the_most_recent_text_survives_trimming(self) -> None:
        buffer: list[str] = []
        for _ in range(30):
            speech_shaping._append_transcript(buffer, "old " * 250)
        speech_shaping._append_transcript(buffer, "THE NEWEST SENTENCE")
        self.assertIn("THE NEWEST SENTENCE", " ".join(buffer))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(np is None, f"numpy is unavailable ({_IMPORT_ERROR})")
class NonFiniteInputTests(unittest.TestCase):
    """A block that is not audio must read as silence, not as full volume.

    Every comparison against NaN is False, so a NaN RMS slipped past the floor
    check and came out of min(1.0, nan) as 1.0 — the waveform slammed to full
    deflection and the avatar's mouth gaped, from input containing no sound.
    """

    def test_none_reads_as_silence(self) -> None:
        self.assertEqual(speech_shaping._pcm_level(None), 0.0)

    def test_a_nan_block_reads_as_silence(self) -> None:
        self.assertEqual(speech_shaping._pcm_level(np.full(480, np.nan)), 0.0)

    def test_an_infinite_block_reads_as_silence(self) -> None:
        self.assertEqual(speech_shaping._pcm_level(np.full(480, np.inf)), 0.0)

    def test_a_nan_block_produces_closed_mouth_frames(self) -> None:
        frames = speech_shaping._pcm_visemes(np.full(24_000, np.nan))
        self.assertTrue(frames)
        self.assertTrue(all(frame == (0.0, 0.0, 0.0) for frame in frames))
