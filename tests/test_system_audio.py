"""Regression tests for system-wide default playback device switching.

`core.system_audio` is the part of the audio stack that changes what the
*whole operating system* plays sound through, as opposed to `audio_devices`
(JARVIS's own microphone/speaker session). Windows has no public API for
this — the switch goes through an undocumented COM interface whose exact
method layout has drifted across Windows releases in community
reverse-engineering, so two known-working layouts are tried in order.

None of `pycaw`/`comtypes` are installed on this Linux sandbox (they are
Windows-only dependencies), and the real COM call cannot be executed or
verified here. Instead, fake modules are injected into `sys.modules` so the
*Python-level control flow* is verified: which functions get called, with
what arguments, in what order, and that any exception turns into an honest
failure rather than a false "done".
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from core import system_audio


def _fake_comtypes(policy_config: MagicMock, create_instance_side_effect=None) -> types.ModuleType:
    fake = types.ModuleType("comtypes")
    fake.CLSCTX_ALL = 23
    fake.IUnknown = object
    fake.GUID = lambda value: value
    fake.HRESULT = "HRESULT-placeholder"
    fake.COMMETHOD = lambda *args, **kwargs: MagicMock()
    fake.CoInitialize = MagicMock()
    if create_instance_side_effect is not None:
        fake.CoCreateInstance = MagicMock(side_effect=create_instance_side_effect)
    else:
        fake.CoCreateInstance = MagicMock(return_value=policy_config)
    return fake


class ApplyPolicyConfigTests(unittest.TestCase):
    """The raw COM call: three roles, honest failure, two-layout fallback."""

    def test_set_default_endpoint_is_called_for_all_three_roles(self) -> None:
        policy_config = MagicMock()
        policy_config.SetDefaultEndpoint.return_value = 0
        fake_comtypes = _fake_comtypes(policy_config)
        with patch.dict(sys.modules, {"comtypes": fake_comtypes}):
            system_audio._apply_policy_config("device-id-123", "{iid}", "{clsid}", 10)
        roles_used = [call.args[1] for call in policy_config.SetDefaultEndpoint.call_args_list]
        self.assertEqual(roles_used, [0, 1, 2])
        for call in policy_config.SetDefaultEndpoint.call_args_list:
            self.assertEqual(call.args[0], "device-id-123")

    def test_a_nonzero_hresult_is_a_failure_not_a_silent_success(self) -> None:
        policy_config = MagicMock()
        policy_config.SetDefaultEndpoint.return_value = 0x80070005  # E_ACCESSDENIED
        fake_comtypes = _fake_comtypes(policy_config)
        with patch.dict(sys.modules, {"comtypes": fake_comtypes}):
            with self.assertRaises(OSError):
                system_audio._apply_policy_config("device-id-123", "{iid}", "{clsid}", 10)

    def test_set_windows_default_falls_back_to_the_vista_layout(self) -> None:
        modern_policy_config = MagicMock()
        modern_policy_config.SetDefaultEndpoint.side_effect = RuntimeError("modern layout rejected")
        legacy_policy_config = MagicMock()
        legacy_policy_config.SetDefaultEndpoint.return_value = 0
        fake_comtypes = _fake_comtypes(
            None, create_instance_side_effect=[modern_policy_config, legacy_policy_config]
        )
        with patch.dict(sys.modules, {"comtypes": fake_comtypes}):
            ok, detail = system_audio._set_windows_default("device-id-123")
        self.assertTrue(ok)
        self.assertEqual(detail, "")
        self.assertEqual(legacy_policy_config.SetDefaultEndpoint.call_count, 3)

    def test_set_windows_default_reports_honestly_when_both_layouts_fail(self) -> None:
        fake_comtypes = _fake_comtypes(None, create_instance_side_effect=RuntimeError("no COM here"))
        with patch.dict(sys.modules, {"comtypes": fake_comtypes}):
            ok, detail = system_audio._set_windows_default("device-id-123")
        self.assertFalse(ok)
        self.assertIn("refused", detail)

    def test_a_com_initialize_error_on_an_already_initialised_thread_is_ignored(self) -> None:
        policy_config = MagicMock()
        policy_config.SetDefaultEndpoint.return_value = 0
        fake_comtypes = _fake_comtypes(policy_config)
        fake_comtypes.CoInitialize = MagicMock(side_effect=OSError("already initialised"))
        with patch.dict(sys.modules, {"comtypes": fake_comtypes}):
            system_audio._apply_policy_config("device-id-123", "{iid}", "{clsid}", 10)
        self.assertEqual(policy_config.SetDefaultEndpoint.call_count, 3)


class WindowsDeviceListingTests(unittest.TestCase):
    """Enumeration must skip inactive endpoints and tolerate odd attributes."""

    def _fake_pycaw(self, devices) -> tuple[types.ModuleType, types.ModuleType]:
        fake_pkg = types.ModuleType("pycaw")
        fake_mod = types.ModuleType("pycaw.pycaw")
        audio_utilities = MagicMock()
        audio_utilities.GetAllDevices.return_value = devices
        fake_mod.AudioUtilities = audio_utilities
        return fake_pkg, fake_mod

    def test_only_active_devices_are_listed(self) -> None:
        active = MagicMock(state=1, FriendlyName="Headset", id="dev-1")
        disabled = MagicMock(state=2, FriendlyName="Old Speakers", id="dev-2")
        fake_pkg, fake_mod = self._fake_pycaw([active, disabled])
        with patch.dict(sys.modules, {"pycaw": fake_pkg, "pycaw.pycaw": fake_mod}):
            result = system_audio._list_windows_playback_devices()
        self.assertEqual(result, [("Headset", "dev-1")])

    def test_a_device_missing_attributes_is_skipped_not_crashing(self) -> None:
        broken = object()  # no .state/.FriendlyName/.id at all
        fake_pkg, fake_mod = self._fake_pycaw([broken])
        with patch.dict(sys.modules, {"pycaw": fake_pkg, "pycaw.pycaw": fake_mod}):
            result = system_audio._list_windows_playback_devices()
        self.assertEqual(result, [])

    def test_pycaw_missing_entirely_returns_an_empty_list_not_an_exception(self) -> None:
        with patch.dict(sys.modules, {"pycaw": None, "pycaw.pycaw": None}):
            result = system_audio._list_windows_playback_devices()
        self.assertEqual(result, [])


class SetDefaultPlaybackDeviceTests(unittest.TestCase):
    """The public dispatch: matching, guards, and honest reporting."""

    def test_an_empty_name_is_refused_before_touching_the_system(self) -> None:
        ok, message = system_audio.set_default_playback_device("   ")
        self.assertFalse(ok)
        self.assertIn("Tell me", message)

    def test_no_devices_available_refuses_rather_than_switching_blind(self) -> None:
        with patch.object(system_audio, "_list_playback_endpoints", return_value=[]):
            ok, message = system_audio.set_default_playback_device("Headset")
        self.assertFalse(ok)
        self.assertIn("blind", message)

    def test_an_unmatched_name_lists_the_real_options(self) -> None:
        devices = [("JBL Quantum 400", "id-1"), ("Realtek Speakers", "id-2")]
        with patch.object(system_audio, "_list_playback_endpoints", return_value=devices):
            ok, message = system_audio.set_default_playback_device("Nonexistent Zzzz Device")
        self.assertFalse(ok)
        self.assertIn("JBL Quantum 400", message)

    def test_a_fuzzy_match_is_applied_through_the_platform_setter(self) -> None:
        devices = [("JBL Quantum 400", "id-1"), ("Realtek Speakers", "id-2")]
        with patch.object(system_audio, "_list_playback_endpoints", return_value=devices), \
             patch.object(system_audio, "_set_windows_default", return_value=(True, "")) as setter:
            ok, message = system_audio.set_default_playback_device("jbl quantum")
        self.assertTrue(ok)
        setter.assert_called_once_with("id-1")
        self.assertIn("JBL Quantum 400", message)

    def test_a_platform_refusal_is_reported_not_hidden(self) -> None:
        devices = [("JBL Quantum 400", "id-1")]
        with patch.object(system_audio, "_list_playback_endpoints", return_value=devices), \
             patch.object(system_audio, "_set_windows_default", return_value=(False, "Access denied")):
            ok, message = system_audio.set_default_playback_device("jbl")
        self.assertFalse(ok)
        self.assertEqual(message, "Access denied")

if __name__ == "__main__":
    unittest.main()
