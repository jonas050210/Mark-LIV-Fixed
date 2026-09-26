from __future__ import annotations

import unittest
from unittest.mock import patch

import config


class ConfigHelperTests(unittest.TestCase):
    def test_non_dictionary_configuration_becomes_empty(self) -> None:
        with patch("config.load_api_keys", return_value=[]):
            self.assertEqual(config.get_config(), {})

    def test_runtime_is_always_windows(self) -> None:
        with patch("config.load_api_keys", return_value={"os_system": "linux"}):
            self.assertEqual(config.get_os(), "windows")
            self.assertTrue(config.is_windows())
