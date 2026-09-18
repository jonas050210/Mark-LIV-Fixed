"""
Test suite for Jev Ultrafast integration in Mark LIV.
Tests action space, dynamic decisions, security guard checks,
agent loop behavior, and fallback handling without requiring live paid APIs.
"""

import json
from unittest.mock import Mock, patch

from core.jev import AutonomousAgent, action_space, choose, field_text
from core.jev.browser import fingerprint
from core.jev.security import assess_action_risk, guard_check


def dummy_page():
    state = {
        "url": "https://example.com/flights",
        "title": "Flight Search",
        "text": "Flight Search from Zurich to London",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "From city", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Search Flights", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait for updates"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def test_action_space_indexing():
    page = dummy_page()
    elements, targets, controls = action_space(page["actions"])
    assert len(elements) == 2
    assert "TYPE_TEXT" in targets
    assert "CLICK" in targets
    assert "WAIT" in controls


def test_security_risk_assessment_destructive():
    buy_action = {"id": "e10", "kind": "click", "label": "Confirm Purchase $450", "node": 99}
    risk = assess_action_risk(buy_action)
    assert risk is not None
    assert "Confirm Purchase" in risk["title"]

    safe_action = {"id": "e2", "kind": "click", "label": "Search Flights", "node": 20}
    assert assess_action_risk(safe_action) is None


def test_security_risk_assessment_credentials():
    pwd_action = {"id": "e11", "kind": "fill", "label": "Enter Password", "role": "textbox", "node": 101}
    risk = assess_action_risk(pwd_action)
    assert risk is not None
    assert "Enter Password" in risk["title"]


def test_choose_speculative_decision():
    page = dummy_page()

    def mock_post(_url, _key, body):
        return {
            "model": "jev-latest",
            "answers": {
                "operation": {"choice": "CLICK", "confidence": 0.95, "probabilities": {"CLICK": 0.95, "TYPE_TEXT": 0.05, "WAIT": 0.0, "DONE": 0.0, "BLOCKED": 0.0}},
                "click_target": {"choice": "2", "confidence": 0.99, "probabilities": {"2": 0.99}},
            },
        }

    with patch("core.jev.model.post_json", side_effect=mock_post):
        decision = choose(page, "Search flights", [], typesafe_key="fake-typesafe-key")
        assert decision["operation"] == "CLICK"
        assert decision["target"] == "2"
        assert decision["choice"] == "e2"


def test_agent_execution_loop():
    page = dummy_page()
    mock_browser = Mock()
    mock_browser.fresh.return_value = True
    mock_browser.observe.return_value = page
    mock_browser.act.return_value = {"executed": "e2"}

    def mock_post(_url, _key, body):
        return {
            "model": "jev-latest",
            "answers": {
                "operation": {"choice": "DONE", "confidence": 1.0, "probabilities": {"DONE": 1.0, "CLICK": 0.0, "TYPE_TEXT": 0.0, "WAIT": 0.0, "BLOCKED": 0.0}},
            },
        }

    with patch("core.jev.model.post_json", side_effect=mock_post):
        agent = AutonomousAgent(
            url="https://example.com/flights",
            goal="Find flight",
            browser_instance=mock_browser,
            typesafe_key="test-key",
            gemini_key="test-gemini",
        )
        res = agent.step()
        assert res["status"] == "done"


def test_autonomous_browser_tool_missing_keys():
    from actions.browser_agent import autonomous_browser_task
    with patch("actions.browser_agent.get_typesafe_key", return_value=None):
        out = autonomous_browser_task({"goal": "Search flights"})
        assert "TypeSafe API key is not configured" in out


def test_autonomous_browser_tool_missing_goal():
    from actions.browser_agent import autonomous_browser_task
    out = autonomous_browser_task({"goal": ""})
    assert "Error: A 'goal' description is required" in out
