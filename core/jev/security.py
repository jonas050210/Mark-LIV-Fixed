"""
Security guards for the autonomous browser agent.
Detects sensitive fields, high-risk interactive elements (payments, account deletion),
and blocks dangerous actions without user confirmation.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from core import confirm

# High-risk action patterns that must never be autonomously triggered
# without explicit HUD confirmation from the user.
_DESTRUCTIVE_PATTERNS = re.compile(
    r"\b(buy\s*now|place\s*order|pay\s*now|checkout|confirm\s*purchase|complete\s*order|"
    r"delete\s*account|close\s*account|erase\s*all|transfer\s*funds|wire\s*money|"
    r"subscribe\s*now|authorize\s*payment|cancel\s*subscription)\b",
    re.IGNORECASE,
)

# Password/credential pattern
_CREDENTIAL_PATTERNS = re.compile(
    r"\b(password|passwd|pin|credit\s*card|cvv|cvc|ssn|social\s*security)\b",
    re.IGNORECASE,
)


def assess_action_risk(action: dict[str, Any], text_value: Optional[str] = None) -> Optional[dict[str, str]]:
    """
    Examines an intended browser action. If it is high-risk, returns a dict with
    title and detail for core.confirm. Otherwise returns None (safe to proceed).
    """
    label = str(action.get("label", ""))
    kind = str(action.get("kind", ""))

    # Check for destructive/payment buttons
    if kind in ("click", "select"):
        if _DESTRUCTIVE_PATTERNS.search(label):
            return {
                "title": f"Browser Action: {label.strip()[:40]}",
                "detail": (
                    f"The autonomous browser is about to click '{label.strip()}'. "
                    "This action may trigger a financial charge, subscription, or permanent change."
                ),
            }

    # Check for password/secret typing
    if kind == "fill":
        role = str(action.get("role", "")).lower()
        if _CREDENTIAL_PATTERNS.search(label) or _CREDENTIAL_PATTERNS.search(role):
            return {
                "title": f"Browser Input: {label.strip()[:40]}",
                "detail": (
                    f"The autonomous browser is attempting to type into a sensitive field: '{label.strip()}'. "
                    "Do not enter passwords or payment credentials autonomously."
                ),
            }

    return None


def guard_check(action: dict[str, Any], text_value: Optional[str] = None) -> Optional[str]:
    """
    Checks if an action requires confirmation. If so, triggers confirm.request().
    Returns the pending confirmation message if blocked, or None if safe.
    """
    risk = assess_action_risk(action, text_value)
    if not risk:
        return None

    # Check if a confirmation is already pending
    if confirm.pending_title():
        return f"[BLOCKED] Confirmation already pending for '{confirm.pending_title()}'."

    # Gate via core.confirm
    return confirm.request(
        key="browser_agent_action",
        title=risk["title"],
        detail=risk["detail"],
        run=lambda: "Confirmed by user.",
    )
