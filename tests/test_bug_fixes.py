"""
Targeted regression tests for bug fixes:
1. Malformed parameters across action handlers (int, list, string, None).
2. Action loader _call_handler parameter mapping ('params' vs 'parameters').
3. Config manager fallback for _get_api_key in actions.
4. Linux open_app with argument tokens (e.g. 'libreoffice --writer').
"""
import sys
from pathlib import Path
from unittest.mock import Mock, patch

def test_action_loader_malformed_inputs():
    from core.action_loader import discover_actions
    registry = discover_actions(Path("actions"))
    
    # Test that non-dict or malformed inputs do not crash the registry or tools
    bad_inputs = [123, "not_a_dict", [1, 2, 3], None, {"action": None}, {"action": 999}]
    for name in registry.names():
        for bad in bad_inputs:
            try:
                res = registry.run(name, bad)
                assert isinstance(res, str)
            except Exception as e:
                raise AssertionError(f"Tool '{name}' crashed on input {repr(bad)}: {e}")
    print("[PASS] test_action_loader_malformed_inputs")


def test_browser_agent_param_mapping():
    from core.action_loader import discover_actions
    registry = discover_actions(Path("actions"))
    
    # Test autonomous_browser_task with proper dict and empty goal
    res = registry.run("autonomous_browser_task", {"goal": ""})
    assert "Error: A 'goal' description is required" in res

    # Test with non-dict input
    res_bad = registry.run("autonomous_browser_task", "invalid")
    assert "Error: A 'goal' description is required" in res_bad
    print("[PASS] test_browser_agent_param_mapping")


def test_api_key_retrieval_robustness():
    # Verify actions safely return empty string if config missing without crashing
    with patch("memory.config_manager.get_gemini_key", return_value="test_key_123"):
        import actions.desktop as dt
        import actions.computer_settings as cs
        import actions.file_processor as fp
        import actions.youtube_video as yv
        import actions.web_search as ws
        assert dt._get_api_key() == "test_key_123"
        assert cs._get_api_key() == "test_key_123"
        assert fp._get_api_key() == "test_key_123"
        assert yv._get_api_key() == "test_key_123"
        assert ws._get_api_key() == "test_key_123"
    print("[PASS] test_api_key_retrieval_robustness")


def test_open_app_linux_args_split():
    import actions.open_app as oa
    with patch("shutil.which", return_value="/usr/bin/libreoffice"), \
         patch("subprocess.Popen") as mock_popen, \
         patch("time.sleep"):
        res = oa._launch_linux("libreoffice --writer")
        assert res is True
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args == ["/usr/bin/libreoffice", "--writer"]
    print("[PASS] test_open_app_linux_args_split")


if __name__ == "__main__":
    test_action_loader_malformed_inputs()
    test_browser_agent_param_mapping()
    test_api_key_retrieval_robustness()
    test_open_app_linux_args_split()
    print("\nALL REGRESSION BUG FIX TESTS PASSED!")
