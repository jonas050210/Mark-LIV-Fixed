from __future__ import annotations

import unittest

from ui_panels.review import render_review_html


class _Palette:
    TEXT = "text"
    WHITE = "white"
    PRI = "primary"
    RED = "red"
    ACC2 = "amber"
    PRI_DIM = "dim"
    TEXT_DIM = "text-dim"
    BORDER = "border"
    TEXT_MED = "text-med"


class ReviewRendererTests(unittest.TestCase):
    def test_foreign_content_is_escaped(self) -> None:
        html = render_review_html(
            "<script>alert(1)</script>",
            [{"severity": "serious", "heading": "<b>bad</b>", "quote": '"quoted"'}],
            ["x & y"],
            _Palette,
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>bad</b>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("x &amp; y", html)
        self.assertIn("▲", html)

    def test_malformed_finding_rows_are_ignored(self) -> None:
        html = render_review_html("summary", [None, "bad"], [], _Palette)
        self.assertIn("summary", html)
