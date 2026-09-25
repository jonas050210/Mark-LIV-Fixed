"""Tests for the handoff between the real browser and the automation window.

Two different surfaces can put a page on screen: ``open_app`` (and
``browser_control``'s navigation actions), which use the user's own browser,
and ``browser_control``'s interactive actions, which need a window Playwright
can drive. Without a shared note of the last page, the automation window starts
on ``about:blank`` and every click reports that the element does not exist.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from actions import browser_control, open_app
from core import browser_handoff


class HandoffStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_handoff.clear()

    tearDown = setUp

    def test_a_page_is_remembered_and_then_consumed(self) -> None:
        browser_handoff.note("https://example.com/news")
        self.assertEqual(browser_handoff.peek(), "https://example.com/news")
        self.assertEqual(browser_handoff.pop(), "https://example.com/news")
        self.assertEqual(browser_handoff.pop(), "")

    def test_only_web_addresses_are_recorded(self) -> None:
        for value in ("", "   ", "doc.txt", "file:///etc/passwd", "javascript:x", None):
            browser_handoff.note(value)
            self.assertEqual(browser_handoff.peek(), "", repr(value))

    def test_an_overlong_address_is_ignored(self) -> None:
        browser_handoff.note("https://example.com/" + "a" * 5000)
        self.assertEqual(browser_handoff.peek(), "")

    def test_the_newest_page_replaces_the_previous_one(self) -> None:
        browser_handoff.note("https://first.example")
        browser_handoff.note("https://second.example")
        self.assertEqual(browser_handoff.peek(), "https://second.example")


class OpenAppHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_handoff.clear()

    tearDown = setUp

    def test_a_url_argument_is_handed_to_the_automation_window(self) -> None:
        open_app._note_opened_pages(["https://bbc.com/news"])
        self.assertEqual(browser_handoff.peek(), "https://bbc.com/news")

    def test_a_document_argument_is_not(self) -> None:
        open_app._note_opened_pages(["report.pdf", "notes.txt"])
        self.assertEqual(browser_handoff.peek(), "")

    def test_no_arguments_is_harmless(self) -> None:
        open_app._note_opened_pages(None)
        open_app._note_opened_pages([])
        self.assertEqual(browser_handoff.peek(), "")


class InteractiveResumeTests(unittest.TestCase):
    """The automation window must resume the user's page, or say it has none."""

    class _Session:
        def __init__(self) -> None:
            self.visited: list[str] = []
            self.clicked = False

        def run(self, value):
            return value

        def go_to(self, url):
            self.visited.append(url)
            return f"Navigated to {url}"

        def click(self, selector=None, text=None):
            self.clicked = True
            return "Clicked."

    def setUp(self) -> None:
        browser_handoff.clear()

    tearDown = setUp

    def test_a_fresh_window_with_no_page_refuses_instead_of_acting_on_blank(self) -> None:
        session = self._Session()
        with patch.object(browser_control._registry, "get_with_state",
                          return_value=(session, True)), \
             patch.object(browser_control._registry, "close_one", return_value=""):
            result = browser_control.browser_control({"action": "click", "text": "Login"})
        self.assertIn("no page open", result)
        self.assertFalse(session.clicked)

    def test_a_page_opened_through_open_app_is_resumed(self) -> None:
        session = self._Session()
        open_app._note_opened_pages(["https://bbc.com/news"])
        with patch.object(browser_control._registry, "get_with_state",
                          return_value=(session, True)):
            result = browser_control.browser_control({"action": "click", "text": "Top story"})
        self.assertEqual(session.visited, ["https://bbc.com/news"])
        self.assertTrue(session.clicked)
        self.assertEqual(result, "Clicked.")

    def test_an_existing_window_without_a_new_page_simply_continues(self) -> None:
        session = self._Session()
        with patch.object(browser_control._registry, "get_with_state",
                          return_value=(session, False)):
            result = browser_control.browser_control({"action": "click", "text": "Next"})
        self.assertEqual(session.visited, [])
        self.assertEqual(result, "Clicked.")

    def test_navigation_records_the_page_for_the_next_interactive_action(self) -> None:
        with patch.object(browser_control, "_open_native", return_value="Opened Chrome."), \
             patch.object(browser_control._registry, "has", return_value=False):
            browser_control.browser_control({"action": "go_to", "url": "example.com"})
        self.assertEqual(browser_handoff.peek(), "https://example.com")


if __name__ == "__main__":
    unittest.main()
