"""
Jev Ultrafast Autonomous Browser Agent Module for MARK LIV.
"""

from .agent import AutonomousAgent
from .browser import Browser, StalePage
from .model import choose, field_text, action_space
from .prompts import DEFAULT_MAX_STEPS, DEFAULT_TIMEOUT_SECONDS

__all__ = [
    "AutonomousAgent",
    "Browser",
    "StalePage",
    "choose",
    "field_text",
    "action_space",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_TIMEOUT_SECONDS",
]
