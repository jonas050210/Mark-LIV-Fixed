from __future__ import annotations

import unittest

from core.viseme import VisemeStream, coverage, text_to_visemes, to_latin


class TransliterationTests(unittest.TestCase):
    def test_latin_accents_and_special_letters_reduce(self) -> None:
        self.assertEqual(to_latin("Ü"), "u")
        self.assertEqual(to_latin("ı"), "i")
        self.assertEqual(to_latin("ł"), "l")
        self.assertEqual(to_latin("Ж"), "j")
        self.assertEqual(to_latin("ω"), "o")
        self.assertEqual(to_latin("中"), "")

    def test_coverage_rejects_unpronounceable_scripts_and_empty_text(self) -> None:
        self.assertEqual(coverage(""), 0.0)
        self.assertEqual(coverage("123 !"), 0.0)
        self.assertEqual(text_to_visemes("中文测试"), [])
        self.assertGreater(coverage("Sözleşmesi"), 0.9)

    def test_text_mapping_handles_digraphs_pauses_and_duplicates(self) -> None:
        shapes = text_to_visemes("Sheep, moon!")
        names = [name for name, _duration in shapes]
        self.assertEqual(names[0], "S")
        self.assertIn("I", names)
        self.assertIn("MBP", names)
        self.assertIn("REST", names)
        self.assertFalse(any(a == b for a, b in zip(names, names[1:], strict=False) if a != "REST"))


class VisemeStreamTests(unittest.TestCase):
    def test_reset_and_pending_queue(self) -> None:
        stream = VisemeStream()
        stream.feed_text("hello")
        self.assertGreater(stream.pending, 0)
        stream.reset()
        self.assertEqual(stream.pending, 0)

    def test_silence_does_not_consume_the_queue(self) -> None:
        stream = VisemeStream()
        stream.feed_text("mouth")
        before = stream.pending
        frames = stream.frames([(0.0, 0.8, 0.4)] * 4, 0.02)
        self.assertEqual(stream.pending, before)
        self.assertEqual(frames, [(0.0, 0.0, 0.0)] * 4)

    def test_audio_advances_shapes_and_outputs_bounded_values(self) -> None:
        stream = VisemeStream()
        stream.feed_text("map voice")
        output = stream.frames([(0.8, 0.7, 0.2)] * 30, 0.05)
        self.assertLess(stream.pending, 8)
        for level, openness, width in output:
            self.assertEqual(level, 0.8)
            self.assertGreaterEqual(openness, 0.0)
            self.assertLessEqual(openness, 1.0)
            self.assertGreaterEqual(width, -1.0)
            self.assertLessEqual(width, 1.0)

    def test_queue_is_bounded(self) -> None:
        stream = VisemeStream()
        stream.feed_text("a b c d e f g h " * 300)
        self.assertLessEqual(stream.pending, 600)
