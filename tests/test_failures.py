from unittest.mock import Mock, patch
from actions.browser_agent import autonomous_browser_task
from core.jev.security import assess_action_risk, guard_check
from core.jev import AutonomousAgent

def test_missing_goal():
    res = autonomous_browser_task({"goal": "   "})
    assert "Error: A 'goal' description is required" in res

def test_missing_typesafe_key():
    with patch("actions.browser_agent.get_typesafe_key", return_value=None):
        res = autonomous_browser_task({"goal": "Search flights"})
        assert "TypeSafe API key is not configured" in res

def test_missing_gemini_key():
    with patch("actions.browser_agent.get_typesafe_key", return_value="ts-123"), \
         patch("actions.browser_agent.get_gemini_key", return_value=None):
        res = autonomous_browser_task({"goal": "Search flights"})
        assert "Gemini API key is not configured" in res

def test_chrome_cdp_unavailable():
    with patch("actions.browser_agent.get_typesafe_key", return_value="ts-123"), \
         patch("actions.browser_agent.get_gemini_key", return_value="gemini-123"), \
         patch("core.jev.agent.Browser", side_effect=RuntimeError("Chrome connection refused")):
        res = autonomous_browser_task({"goal": "Search flights"})
        assert "Autonomous browser agent encountered an error" in res
        assert "Chrome connection refused" in res

def test_step_limit_handling():
    mock_browser = Mock()
    mock_browser.fresh.return_value = True
    page = {
        "url": "https://example.com",
        "title": "Example",
        "text": "Hello world",
        "actions": [{"id": "e1", "kind": "wait", "label": "Wait", "node": 1}],
    }
    mock_browser.observe.return_value = page
    mock_browser.act.return_value = {"executed": "e1"}

    agent = AutonomousAgent(
        url="https://example.com",
        goal="Do something",
        max_steps=2,
        browser_instance=mock_browser,
        typesafe_key="fake-ts",
        gemini_key="fake-gemini",
    )
    # Mock history to simulate reaching max steps
    agent.state["history"] = [{"step": 1}, {"step": 2}]
    snap = agent.step()
    assert snap["status"] == "blocked"
    assert "Reached maximum allowed step budget" in snap["blocked_reason"]

def test_timeout_handling():
    mock_browser = Mock()
    page = {"url": "https://example.com", "title": "Example", "text": "", "actions": []}
    mock_browser.observe.return_value = page
    agent = AutonomousAgent(
        url="https://example.com",
        goal="Do something",
        timeout=0.001,
        browser_instance=mock_browser,
        typesafe_key="fake-ts",
        gemini_key="fake-gemini",
    )
    import time
    time.sleep(0.01)
    snap = agent.step()
    assert snap["status"] == "blocked"
    assert "Execution timed out" in snap["blocked_reason"]

def test_sensitive_field_interception():
    # Attempting to fill password or credit card field
    pwd_action = {"id": "e5", "kind": "fill", "label": "Enter your Master Password", "role": "textbox", "node": 42}
    risk = assess_action_risk(pwd_action)
    assert risk is not None
    assert "Browser Input: Enter your Master Password" in risk["title"]
    assert "sensitive field" in risk["detail"]

def test_destructive_button_interception():
    # Attempting to click payment/delete button
    checkout_action = {"id": "e9", "kind": "click", "label": "Pay Now & Place Order", "role": "button", "node": 88}
    risk = assess_action_risk(checkout_action)
    assert risk is not None
    assert "Browser Action: Pay Now & Place Order" in risk["title"]
    assert "financial charge" in risk["detail"]

if __name__ == "__main__":
    test_missing_goal()
    print("test_missing_goal: PASS")
    test_missing_typesafe_key()
    print("test_missing_typesafe_key: PASS")
    test_missing_gemini_key()
    print("test_missing_gemini_key: PASS")
    test_chrome_cdp_unavailable()
    print("test_chrome_cdp_unavailable: PASS")
    test_step_limit_handling()
    print("test_step_limit_handling: PASS")
    test_timeout_handling()
    print("test_timeout_handling: PASS")
    test_sensitive_field_interception()
    print("test_sensitive_field_interception: PASS")
    test_destructive_button_interception()
    print("test_destructive_button_interception: PASS")
    print("\nALL FAILURE CASE TESTS PASSED!")
