"""Tests for Spotify transport extensions and the new dashboard panels."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from actions import media_control
import dashboard.server as dashboard_module


class SpotifyTransportTests(unittest.TestCase):
    """shuffle / repeat / seek were missing even though the scopes existed."""

    def _token(self):
        return patch.multiple(
            media_control,
            _load_token=lambda: {"client_id": "x" * 16},
            _refresh_token=lambda token, client_id: "token",
            _device_id=lambda token, requested="": ("device-1", ""),
        )

    def test_shuffle_sends_a_boolean_state(self) -> None:
        calls = []

        def fake_request(method, path, token, **kwargs):
            calls.append((method, path, kwargs.get("params", {})))
            return 204, {}

        with self._token(), patch.object(media_control, "_request", fake_request):
            result = media_control.media_control({"action": "shuffle", "enabled": False})
        self.assertIn("shuffle is now off", result)
        self.assertEqual(calls[0][1], "/me/player/shuffle")
        self.assertEqual(calls[0][2]["state"], "false")

    def test_repeat_translates_spoken_modes(self) -> None:
        captured = {}

        def fake_request(method, path, token, **kwargs):
            captured.update(kwargs.get("params", {}))
            return 204, {}

        with self._token(), patch.object(media_control, "_request", fake_request):
            result = media_control.media_control({"action": "repeat", "mode": "song"})
        self.assertEqual(captured["state"], "track")
        self.assertIn("repeating the current track", result)

    def test_repeat_rejects_an_unknown_mode(self) -> None:
        with self._token(), patch.object(media_control, "_request") as request:
            result = media_control.media_control({"action": "repeat", "mode": "sideways"})
        self.assertIn("must be off, track, or context", result)
        request.assert_not_called()

    def test_seek_converts_seconds_to_milliseconds(self) -> None:
        captured = {}

        def fake_request(method, path, token, **kwargs):
            captured.update(kwargs.get("params", {}))
            return 204, {}

        with self._token(), patch.object(media_control, "_request", fake_request):
            result = media_control.media_control({"action": "seek", "value": 95})
        self.assertEqual(captured["position_ms"], 95_000)
        self.assertIn("1:35", result)

    def test_seek_rejects_an_out_of_range_position(self) -> None:
        with self._token(), patch.object(media_control, "_request") as request:
            result = media_control.media_control({"action": "seek", "value": -5})
        self.assertIn("between 0 seconds", result)
        request.assert_not_called()

    def test_transport_falls_back_to_system_media_keys(self) -> None:
        """Without an active Connect device, the OS transport still works."""
        with patch.multiple(
            media_control,
            _load_token=lambda: {"client_id": "x" * 16},
            _refresh_token=lambda token, client_id: "token",
            _device_id=lambda token, requested="": (None, "no device"),
        ), patch.object(media_control, "_media_key", return_value=True) as key:
            result = media_control.media_control({"action": "next"})
        key.assert_called_once_with("next")
        self.assertIn("system media keys", result)

    def test_fallback_is_not_used_when_a_specific_track_was_requested(self) -> None:
        """A media key cannot honour 'play song X', so do not pretend it did."""
        with patch.multiple(
            media_control,
            _load_token=lambda: {"client_id": "x" * 16},
            _refresh_token=lambda token, client_id: "token",
            _device_id=lambda token, requested="": (None, "no device"),
        ), patch.object(media_control, "_media_key", return_value=True) as key:
            result = media_control.media_control({"action": "play", "query": "Bohemian Rhapsody"})
        key.assert_not_called()
        self.assertIn("no device", result)


class SpotifySnapshotTests(unittest.TestCase):
    def test_snapshot_reports_a_reason_instead_of_raising(self) -> None:
        with patch.object(media_control, "_load_token", return_value={}), \
             patch.object(media_control, "_refresh_token", return_value=None):
            snapshot = media_control.playback_snapshot()
        self.assertFalse(snapshot["connected"])
        self.assertIn("not connected", snapshot["reason"])

    def test_snapshot_only_accepts_https_artwork(self) -> None:
        body = {
            "is_playing": True,
            "progress_ms": 1000,
            "device": {"name": "Desk", "volume_percent": 40},
            "item": {
                "name": "Track", "duration_ms": 200000,
                "artists": [{"name": "Artist"}],
                "album": {"name": "Album", "images": [{"url": "http://insecure/x.jpg"}]},
            },
        }
        with patch.object(media_control, "_load_token", return_value={"client_id": "x" * 16}), \
             patch.object(media_control, "_refresh_token", return_value="token"), \
             patch.object(media_control, "_request", return_value=(200, body)):
            snapshot = media_control.playback_snapshot()
        self.assertEqual(snapshot["artwork"], "")
        self.assertEqual(snapshot["track"], "Track")


class PanelAllowlistTests(unittest.TestCase):
    """The browser must not be able to widen the action surface."""

    def test_launch_states_are_restricted(self) -> None:
        self.assertIn("fullscreen", dashboard_module._APP_LAUNCH_STATES)
        self.assertNotIn("sideways", dashboard_module._APP_LAUNCH_STATES)

    def test_spotify_panel_cannot_trigger_the_oauth_consent_flow(self) -> None:
        """'connect' opens a browser window; it must stay a deliberate command."""
        self.assertNotIn("connect", dashboard_module._SPOTIFY_PANEL_ACTIONS)

    def test_panel_actions_all_exist_in_the_media_tool_schema(self) -> None:
        allowed = set(media_control.TOOL["parameters"]["properties"]["action"]["enum"])
        self.assertTrue(dashboard_module._SPOTIFY_PANEL_ACTIONS <= allowed)


if __name__ == "__main__":
    unittest.main()
