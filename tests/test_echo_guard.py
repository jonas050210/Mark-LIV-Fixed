from __future__ import annotations

import unittest

import numpy as np

from core.echo import EchoGuard, band_energies


class EchoBandTests(unittest.TestCase):
    def test_short_audio_returns_empty_band_vector(self) -> None:
        bands = band_energies(np.zeros(10, dtype=np.float32), 16_000)
        self.assertEqual(bands.shape, (8,))
        self.assertTrue(np.all(bands == 0))

    def test_tone_has_finite_energy(self) -> None:
        t = np.arange(1024, dtype=np.float32) / 16_000
        bands = band_energies(np.sin(2 * np.pi * 800 * t), 16_000)
        self.assertTrue(np.isfinite(bands).all())
        self.assertGreater(float(bands.sum()), 0.0)


class EchoGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        t = np.arange(1024, dtype=np.float32) / 16_000
        self.tone = np.sin(2 * np.pi * 800 * t).astype(np.float32)

    def test_audible_input_without_output_is_user_speech(self) -> None:
        guard = EchoGuard()
        self.assertTrue(guard.is_user_speech(self.tone, 16_000, 0.5, when=1.0))

    def test_quiet_input_is_not_user_speech(self) -> None:
        guard = EchoGuard()
        guard.note_output(self.tone, 16_000, 0.8, when=1.0)
        self.assertFalse(guard.is_user_speech(self.tone, 16_000, 0.01, when=1.1))

    def test_matching_output_is_learned_as_echo(self) -> None:
        guard = EchoGuard()
        for index in range(24):
            now = index * 0.05
            guard.note_output(self.tone, 16_000, 0.8, when=now)
            self.assertFalse(guard.is_user_speech(self.tone * 0.6, 16_000, 0.48, when=now))
        self.assertGreater(guard.last_similarity, 0.9)
        self.assertTrue(guard.reliable)
        self.assertGreaterEqual(guard.threshold, 0.15)

    def test_reset_drops_history_but_preserves_calibration_fields(self) -> None:
        guard = EchoGuard()
        guard.note_output(self.tone, 16_000, 0.8, when=1.0)
        guard.reset()
        self.assertEqual(guard.last_similarity, 0.0)
        self.assertTrue(guard.is_user_speech(self.tone, 16_000, 0.5, when=1.1))

    def test_bad_input_fails_closed(self) -> None:
        guard = EchoGuard()
        guard.note_output(object(), 16_000, 0.5)  # malformed playback is ignored
        guard.note_output(self.tone, 16_000, 0.5)
        self.assertFalse(guard.is_user_speech(object(), 16_000, 0.5))
