"""Round 3 — German-language command coverage.

The assistant is spoken to in German ("auf meinen ersten Monitor"), and the
matching layers were English-only: monitor ordinals, browser words, filler
words, umlauts. These tests pin the German paths without touching any GUI.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import browser_control, computer_settings  # noqa: E402
from core import window_manager  # noqa: E402


class _FakePage:
    def __init__(self, url, title, closed=False):
        self.url = url
        self._title = title
        self._closed = closed
        self.brought_to_front = False

    def is_closed(self):
        return self._closed

    async def title(self):
        if self._closed:
            raise RuntimeError("closed")
        return self._title

    async def bring_to_front(self):
        self.brought_to_front = True


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages


class _FakeSession:
    """Just enough of _BrowserSession for the tab methods — no Playwright."""

    def __init__(self, pages, active):
        self._context = _FakeContext(pages)
        self._page = active

    # The real methods, bound to the fake state.
    list_tabs = browser_control._BrowserSession.list_tabs
    switch_tab = browser_control._BrowserSession.switch_tab

    def run(self, coro, timeout=60):
        return asyncio.run(coro)


class UmlautFoldingTests(unittest.TestCase):
    """Umlauts must fold, not vanish: 'Müller' used to become 'm ller'."""

    def test_normalise_folds_umlauts_to_base_letters(self) -> None:
        self.assertEqual(window_manager._normalise("Müller"), "muller")
        self.assertEqual(window_manager._normalise("öffne Spotify"), "offne spotify")
        self.assertEqual(window_manager._normalise("LÄUFT"), "lauft")

    def test_normalise_folds_sharp_s(self) -> None:
        self.assertEqual(window_manager._normalise("schließen"), "schliessen")

    def test_fold_umlauts_is_idempotent(self) -> None:
        once = window_manager.fold_umlauts("Größenwahn-Straße")
        self.assertEqual(window_manager.fold_umlauts(once), once)


class GermanMonitorTokenTests(unittest.TestCase):
    """'auf meinen ersten Monitor' must resolve like 'first monitor'."""

    def _resolve(self, token):
        return window_manager.resolve_monitor_token(token)

    def _with_monitors(self, count):
        monitors = [
            window_manager.MonitorInfo(
                1, "primary", 0, 0, 1920, 1080, 0, 0, 1920, 1040, primary=True
            )
        ]
        for i in range(2, count + 1):
            monitors.append(
                window_manager.MonitorInfo(
                    i, f"screen{i}", (i - 1) * 1920, 0, i * 1920, 1080,
                    (i - 1) * 1920, 0, i * 1920, 1040,
                )
            )
        return patch.object(window_manager, "list_monitors", return_value=monitors)

    def test_german_ordinals_resolve_in_any_case_form(self) -> None:
        with self._with_monitors(2):
            self.assertEqual(self._resolve("ersten Monitor"), 1)
            self.assertEqual(self._resolve("erste"), 1)
            self.assertEqual(self._resolve("erster"), 1)
            self.assertEqual(self._resolve("auf meinen zweiten Monitor"), 2)
            self.assertEqual(self._resolve("zweiten"), 2)
            self.assertEqual(self._resolve("zweite"), 2)

    def test_german_ordinals_with_more_monitors(self) -> None:
        with self._with_monitors(5):
            self.assertEqual(self._resolve("dritten"), 3)
            self.assertEqual(self._resolve("fünften Monitor"), 5)
            self.assertEqual(self._resolve("funften"), 5)  # typed without umlaut

    def test_high_german_ordinals_when_enough_monitors_exist(self) -> None:
        with self._with_monitors(8):
            self.assertEqual(self._resolve("sechsten"), 6)
            self.assertEqual(self._resolve("siebten"), 7)
            self.assertEqual(self._resolve("achten"), 8)
            with self.assertRaises(ValueError):
                self._resolve("zehnten")

    def test_german_ordinal_beyond_monitor_count_is_rejected(self) -> None:
        with self._with_monitors(2):
            with self.assertRaises(ValueError):
                self._resolve("dritten Monitor")

    def test_german_semantic_monitor_names(self) -> None:
        with self._with_monitors(2):
            self.assertEqual(self._resolve("Hauptmonitor"), 1)
            self.assertEqual(self._resolve("Hauptbildschirm"), 1)
            self.assertEqual(self._resolve("auf den anderen Monitor"), 2)
            self.assertEqual(self._resolve("ganz links"), 1)
            self.assertEqual(self._resolve("rechts"), 2)

    def test_bildschirm_number_still_resolves(self) -> None:
        with self._with_monitors(2):
            self.assertEqual(self._resolve("Bildschirm 2"), 2)
            self.assertEqual(self._resolve("Monitor 1"), 1)

    def test_english_tokens_still_work(self) -> None:
        with self._with_monitors(2):
            self.assertEqual(self._resolve("primary"), 1)
            self.assertEqual(self._resolve("second"), 2)
            self.assertEqual(self._resolve("left"), 1)
            self.assertEqual(self._resolve("monitor 2"), 2)


class GermanBrowserWordTests(unittest.TestCase):
    """'Browser-Fenster' must count as a generic browser reference."""

    def test_german_generic_browser_phrases(self) -> None:
        for phrase in (
            "Browser Fenster", "das Browser-Fenster", "Internetfenster",
            "Internet Fenster", "Browserfenster", "mein Browser",
        ):
            self.assertTrue(
                window_manager._is_generic_browser_target(phrase),
                f"{phrase!r} should be treated as a generic browser reference",
            )

    def test_specific_apps_stay_specific(self) -> None:
        for phrase in ("Spotify", "Discord", "Netflix"):
            self.assertFalse(window_manager._is_generic_browser_target(phrase))


class GermanLabelNoiseTests(unittest.TestCase):
    """German filler words must not fabricate agreement between labels."""

    def test_german_filler_words_do_not_link_unrelated_labels(self) -> None:
        # Only the shared filler 'bitte'/'offne' — no real word in common.
        self.assertFalse(
            window_manager._launch_labels_agree("bitte", "öffne die Uhr")
        )

    def test_german_command_phrase_agrees_with_plain_label(self) -> None:
        # 'öffne bitte das Fenster Spotify' names Spotify, and the umlaut
        # in 'öffne' must not split the words.
        self.assertTrue(
            window_manager._launch_labels_agree(
                "öffne bitte das Spotify Fenster", "spotify"
            )
        )

    def test_unrelated_labels_still_disagree(self) -> None:
        self.assertFalse(
            window_manager._launch_labels_agree("öffne die Uhr", "spotify")
        )


class GermanSettingsAliasTests(unittest.TestCase):
    """computer_settings must understand spoken German phrases."""

    def _detect(self, text):
        return computer_settings._detect_action(text)

    def test_volume_phrases(self) -> None:
        self.assertEqual(self._detect("mach lauter")["action"], "volume_up")
        self.assertEqual(self._detect("lauter")["action"], "volume_up")
        self.assertEqual(self._detect("mach leiser")["action"], "volume_down")

    def test_mute_phrase(self) -> None:
        self.assertEqual(self._detect("ton aus")["action"], "mute")
        self.assertEqual(self._detect("stumm")["action"], "mute")
        self.assertEqual(self._detect("stummschalten")["action"], "mute")

    def test_volume_number_with_german_words(self) -> None:
        self.assertEqual(
            self._detect("lautstärke auf 30"),
            {"action": "volume_set", "value": 30},
        )
        self.assertEqual(
            self._detect("ton 40"),
            {"action": "volume_set", "value": 40},
        )
        self.assertEqual(
            self._detect("lautstarke 20"),  # typed without umlaut
            {"action": "volume_set", "value": 20},
        )

    def test_button_number_is_not_a_volume_command(self) -> None:
        # 'button 3' contains 'ton' — the word boundary must save it.
        self.assertNotEqual(self._detect("button 3")["action"], "volume_set")

    def test_brightness_phrases(self) -> None:
        self.assertEqual(self._detect("heller")["action"], "brightness_up")
        self.assertEqual(self._detect("mach heller")["action"], "brightness_up")
        self.assertEqual(self._detect("dunkler")["action"], "brightness_down")
        self.assertEqual(self._detect("abdunkeln")["action"], "brightness_down")

    def test_display_and_lock_phrases(self) -> None:
        self.assertEqual(self._detect("bildschirm sperren")["action"], "lock_screen")
        self.assertEqual(self._detect("sperren")["action"], "lock_screen")
        self.assertEqual(
            self._detect("bildschirm aus")["action"], "sleep_display"
        )

    def test_theme_wifi_tabs_misc(self) -> None:
        self.assertEqual(self._detect("dunkelmodus")["action"], "dark_mode")
        self.assertEqual(self._detect("wlan")["action"], "toggle_wifi")
        self.assertEqual(self._detect("taskmanager")["action"], "task_manager")
        self.assertEqual(self._detect("neu laden")["action"], "refresh_page")
        self.assertEqual(self._detect("neuen tab")["action"], "new_tab")
        self.assertEqual(self._detect("vollbild")["action"], "full_screen")
        self.assertEqual(
            self._detect("screenshot machen")["action"], "screenshot"
        )

    def test_power_phrases_name_the_computer(self) -> None:
        self.assertEqual(self._detect("pc ausschalten")["action"], "shutdown")
        self.assertEqual(
            self._detect("fahre den pc herunter")["action"], "shutdown"
        )
        self.assertEqual(self._detect("neustart")["action"], "restart")
        self.assertEqual(self._detect("pc neu starten")["action"], "restart")

    def test_english_aliases_still_resolve(self) -> None:
        self.assertEqual(self._detect("louder")["action"], "volume_up")
        self.assertEqual(self._detect("night mode")["action"], "dark_mode")


class BrowserTabSessionTests(unittest.TestCase):
    """list_tabs/switch_tab on a controlled session (tab awareness, part 1)."""

    def _session(self):
        youtube = _FakePage("https://youtube.com", "YouTube")
        wiki = _FakePage("https://de.wikipedia.org", "Wikipedia – die freie Enzyklopädie")
        spiegel = _FakePage("https://spiegel.de", "Spiegel Online – Nachrichten")
        return _FakeSession(
            [youtube, wiki, spiegel], active=wiki
        )

    def test_list_tabs_numbers_and_marks_the_active_tab(self) -> None:
        sess = self._session()
        result = sess.run(sess.list_tabs())
        self.assertIn("3 tab(s) open", result)
        self.assertIn("1. YouTube", result)
        self.assertIn("Wikipedia", result)
        self.assertIn("(active)", result)

    def test_list_tabs_reports_empty_session(self) -> None:
        sess = _FakeSession([], active=None)
        self.assertIn("No tabs", sess.run(sess.list_tabs()))

    def test_switch_tab_by_number(self) -> None:
        sess = self._session()
        result = sess.run(sess.switch_tab("3"))
        self.assertIn("Spiegel", result)
        self.assertIs(sess._page, sess._context.pages[2])
        self.assertTrue(sess._context.pages[2].brought_to_front)

    def test_switch_tab_by_title_fragment(self) -> None:
        sess = self._session()
        result = sess.run(sess.switch_tab("youtube"))
        self.assertIn("YouTube", result)
        self.assertIs(sess._page, sess._context.pages[0])

    def test_switch_tab_folds_umlauts_in_both_directions(self) -> None:
        sess = self._session()
        # Spoken 'nachrichten' matches 'Nachrichten'; and an umlaut in the
        # request matches a folded title.
        self.assertIn("Spiegel", sess.run(sess.switch_tab("Nachrichten")))
        sess2 = self._session()
        page = _FakePage("https://example.de", "Müllhalde")
        sess2._context.pages.append(page)
        self.assertIn("Müllhalde", sess2.run(sess2.switch_tab("mullhalde")))

    def test_switch_tab_rejects_unknown_number_and_fragment(self) -> None:
        sess = self._session()
        self.assertIn("no tab 9", sess.run(sess.switch_tab("9")))
        self.assertIn("No open tab matches", sess.run(sess.switch_tab("gibberish")))

    def test_switch_tab_already_active(self) -> None:
        sess = self._session()
        self.assertIn("already active", sess.run(sess.switch_tab("2")))
        self.assertIn("already active", sess.run(sess.switch_tab("wikipedia")))

    def test_switch_tab_without_a_selection_asks_for_one(self) -> None:
        sess = self._session()
        self.assertIn("which tab", sess.run(sess.switch_tab("")))


class BrowserTabRoutingTests(unittest.TestCase):
    """The tool routing: honest answer without a session, pass-through with."""

    def test_list_tabs_without_session_is_honest_and_opens_nothing(self) -> None:
        with patch.object(browser_control._registry, "has", return_value=False), \
             patch.object(browser_control._registry, "get", side_effect=AssertionError(
                 "no session may be created for list_tabs")):
            result = browser_control.browser_control({"action": "list_tabs"})
        self.assertIn("only see the tabs", result)
        self.assertIn("None is open yet", result)

    def test_switch_tab_without_session_is_honest_too(self) -> None:
        with patch.object(browser_control._registry, "has", return_value=False), \
             patch.object(browser_control._registry, "get", side_effect=AssertionError(
                 "no session may be created for switch_tab")):
            result = browser_control.browser_control({"action": "switch_tab"})
        self.assertIn("only see the tabs", result)

    def test_list_tabs_uses_the_existing_session(self) -> None:
        pages = [_FakePage("https://youtube.com", "YouTube")]
        fake = _FakeSession(pages, active=pages[0])
        with patch.object(browser_control._registry, "has", return_value=True), \
             patch.object(browser_control._registry, "get", return_value=fake), \
             patch.object(browser_control._registry, "_active_browser", "chrome"):
            result = browser_control.browser_control({"action": "list_tabs"})
        self.assertIn("YouTube", result)

    def test_switch_tab_forwards_the_tab_parameter(self) -> None:
        pages = [
            _FakePage("https://youtube.com", "YouTube"),
            _FakePage("https://spiegel.de", "Spiegel"),
        ]
        fake = _FakeSession(pages, active=pages[0])
        with patch.object(browser_control._registry, "has", return_value=True), \
             patch.object(browser_control._registry, "get", return_value=fake), \
             patch.object(browser_control._registry, "_active_browser", "chrome"):
            result = browser_control.browser_control(
                {"action": "switch_tab", "tab": "spiegel"}
            )
        self.assertIn("Spiegel", result)
        self.assertIs(fake._page, pages[1])

    def test_actions_are_declared_in_the_tool_schema(self) -> None:
        source = str(browser_control.TOOL)
        self.assertIn("list_tabs", source)
        self.assertIn("switch_tab", source)
        self.assertIn("'tab'", source)

    def test_tab_actions_are_not_auto_session_actions(self) -> None:
        # They must never open an automation window on their own — that is
        # the whole point of the honest no-session answer. The interactive
        # set is the one that auto-creates a session.
        self.assertNotIn("list_tabs", browser_control._INTERACTIVE_ACTIONS)
        self.assertNotIn("switch_tab", browser_control._INTERACTIVE_ACTIONS)


if __name__ == "__main__":
    unittest.main()
