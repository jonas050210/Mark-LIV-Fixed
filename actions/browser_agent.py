"""
Autonomous Browser Agent Tool for MARK LIV.
Integrates Jev Ultrafast with Mark LIV's Gemini agent and TypeSafe policy.

Allows Mark LIV to perform goal-directed, autonomous web tasks:
  autonomous_browser_task(goal="Find the current price of flights from Zurich to London", start_url="https://...")
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.jev import AutonomousAgent, DEFAULT_MAX_STEPS, DEFAULT_TIMEOUT_SECONDS
from memory.config_manager import get_gemini_key, get_typesafe_key

logger = logging.getLogger("markliv.browser_agent")


def autonomous_browser_task(params: dict[str, Any]) -> str:
    """Executes a goal-directed autonomous browser session using Jev Ultrafast."""
    goal = str(params.get("goal", "")).strip()
    if not goal:
        return "Error: A 'goal' description is required for the autonomous browser task."

    start_url = str(params.get("start_url", "")).strip()
    if not start_url:
        start_url = "https://www.google.com"
    elif "://" not in start_url:
        start_url = "https://" + start_url

    max_steps = int(params.get("max_steps", DEFAULT_MAX_STEPS))
    max_steps = max(1, min(max_steps, 60))

    timeout = float(params.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    timeout = max(10.0, min(timeout, 300.0))

    typesafe_key = get_typesafe_key()
    if not typesafe_key:
        return (
            "TypeSafe API key is not configured. Please open Settings (⚙) → API KEYS "
            "and configure your TypeSafe API key to use the autonomous browser agent."
        )

    gemini_key = get_gemini_key()
    if not gemini_key:
        return (
            "Gemini API key is not configured. Please open Settings (⚙) → API KEYS "
            "and configure your Gemini API key."
        )

    player = params.get("__player__") or params.get("player")
    if player and hasattr(player, "write_log"):
        player.write_log(f"[BrowserAgent] Starting task: {goal[:60]}...")

    try:
        with AutonomousAgent(
            url=start_url,
            goal=goal,
            max_steps=max_steps,
            timeout=timeout,
            typesafe_key=typesafe_key,
            gemini_key=gemini_key,
        ) as agent:
            final_snapshot = agent.run_all()

        status = final_snapshot.get("status", "unknown")
        history = final_snapshot.get("history", [])
        page = final_snapshot.get("page", {})
        page_title = page.get("title", "No Title")
        final_url = page.get("url", start_url)
        blocked_reason = final_snapshot.get("blocked_reason")

        summary_lines = [
            f"Autonomous Browser Task Completed: status={status.upper()}",
            f"Final URL: {final_url}",
            f"Page Title: {page_title}",
            f"Total Steps Executed: {len(history)}",
        ]

        if blocked_reason:
            summary_lines.append(f"Notice/Block Reason: {blocked_reason}")

        if history:
            summary_lines.append("Actions Executed:")
            for h in history[-5:]:
                action_text = f" -> typed '{h['text']}'" if h.get("text") else ""
                summary_lines.append(f"  Step {h['step']}: {h['kind'].upper()} on '{h['action']}'{action_text}")

        # Extract visible excerpt from page text
        page_text = str(page.get("text", "")).strip()
        if page_text:
            summary_lines.append(f"Current Visible Page Text Summary:\n{page_text[:1200]}")

        res = "\n".join(summary_lines)
        if player and hasattr(player, "write_log"):
            player.write_log(f"[BrowserAgent] Finished ({status}): {len(history)} steps.")
        return res

    except Exception as e:
        logger.exception("Browser agent encountered an error")
        return f"Autonomous browser agent encountered an error: {e}"


# Tool definition auto-discovered by core/action_loader.py
TOOL = {
    "name": "autonomous_browser_task",
    "description": (
        "Executes a multi-step, autonomous web-browsing task using the Jev Ultrafast browser agent. "
        "Use this for complex, goal-directed web workflows that require multiple interactions "
        "(e.g., flight searches, shopping price comparisons, navigating web forms, extracting data across steps). "
        "For simple direct actions (opening a single URL, taking a screenshot, simple navigation), "
        "use the regular browser_control tool instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "Natural-language description of the complete web browsing objective (e.g. 'Search for one-way flights from Zurich to London on Google Flights').",
            },
            "start_url": {
                "type": "STRING",
                "description": "Optional starting URL (e.g. 'https://www.google.com/travel/flights'). Defaults to Google search if omitted.",
            },
            "max_steps": {
                "type": "INTEGER",
                "description": "Maximum interactive steps allowed before stopping (default: 40, max: 60).",
            },
            "timeout_seconds": {
                "type": "INTEGER",
                "description": "Maximum seconds allowed for the entire browsing session (default: 120).",
            },
        },
        "required": ["goal"],
    },
    "handler": autonomous_browser_task,
}
