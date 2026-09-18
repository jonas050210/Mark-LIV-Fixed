"""
The complete autonomous browser agent loop.
Bounded execution, typed choices, observable state, security gating.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, Generator, Optional

from .browser import Browser, StalePage
from .model import action_space, choose, field_context, field_text
from .prompts import DEFAULT_MAX_STEPS, DEFAULT_TIMEOUT_SECONDS
from .security import guard_check


class AutonomousAgent:
    def __init__(
        self,
        url: str,
        goal: str,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        typesafe_key: Optional[str] = None,
        gemini_key: Optional[str] = None,
        browser_instance: Optional[Any] = None,
    ):
        task = goal.strip()
        if not task:
            raise ValueError("Supply a goal for the browser agent.")
        self.goal = task
        self.max_steps = max_steps
        self.timeout = timeout
        self.typesafe_key = typesafe_key
        self.gemini_key = gemini_key
        self.pending_text = None

        if browser_instance:
            self.browser = browser_instance
            self._owns_browser = False
        else:
            self.browser = Browser(url)
            self._owns_browser = True

        try:
            page = self.browser.observe(screenshot=False)
        except Exception:
            if self._owns_browser:
                self.browser.close()
            raise

        self.state: dict[str, Any] = {
            "browser": self.browser,
            "goal": self.goal,
            "page": page,
            "decision": None,
            "history": [],
            "status": "ready",
            "decisions": [],
            "text_calls": [],
            "elapsed_ms": 0,
            "started_at": time.perf_counter(),
            "blocked_reason": None,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def step(self) -> dict[str, Any]:
        state = self.state
        if state["status"] in {"done", "blocked"}:
            return self.snapshot()

        # Check overall timeout
        elapsed = time.perf_counter() - state["started_at"]
        if elapsed > self.timeout:
            state["status"] = "blocked"
            state["blocked_reason"] = f"Execution timed out after {self.timeout:.1f}s"
            return self.snapshot()

        if len(state["history"]) >= self.max_steps:
            state["status"] = "blocked"
            state["blocked_reason"] = f"Reached maximum allowed step budget ({self.max_steps} steps)"
            return self.snapshot()

        # Step 1: Predict (Choose operation & target)
        if not self.browser.fresh(state["page"]):
            state["page"] = self.browser.observe(screenshot=False)

        try:
            state["decision"] = choose(
                state["page"],
                state["goal"],
                state["history"],
                typesafe_key=self.typesafe_key,
            )
        except StalePage:
            state["decision"] = None
            state["page"] = self.browser.observe(screenshot=False)
            return self.snapshot()

        decision, page = state["decision"], state["page"]
        selected = decision["choice"]

        if selected in {"DONE", "BLOCKED"}:
            state["status"] = "done" if selected == "DONE" else "blocked"
            if selected == "BLOCKED":
                state["blocked_reason"] = "Model determined no supported operation can progress."
            return self.snapshot()

        action = next((a for a in page["actions"] if a["id"] == selected), None)
        if not action:
            state["status"] = "blocked"
            state["blocked_reason"] = f"Selected action ID '{selected}' not found in current page actions."
            return self.snapshot()

        # Step 2: Text generation if fill
        text, helper = None, None
        if action["kind"] == "fill":
            if not self.browser.fresh(page):
                state["page"] = self.browser.observe(screenshot=False)
                return self.snapshot()
            context = field_context(state["goal"], action, page, state["history"])
            if self.pending_text and self.pending_text[0] == context:
                _, text, helper = self.pending_text
            else:
                text, helper = field_text(context, gemini_key=self.gemini_key)
                self.pending_text = (context, text, helper)

        # Step 3: Security & confirmation gate
        confirm_gate_msg = guard_check(action, text)
        if confirm_gate_msg:
            state["status"] = "blocked"
            state["blocked_reason"] = confirm_gate_msg
            return self.snapshot()

        # Step 4: Execute action via CDP
        try:
            self.browser.act(action, page, text=text)
        except StalePage:
            state["page"] = self.browser.observe(screenshot=False)
            return self.snapshot()

        self.pending_text = None
        state["history"].append({
            "step": len(state["history"]) + 1,
            "action": action["label"],
            "kind": action["kind"],
            "choice": selected,
            "text": text,
            "operation": decision["operation"],
            "url": page.get("url", ""),
            "timestamp": time.time(),
        })

        # Step 5: Observe updated state
        state["page"] = self.browser.observe(screenshot=False)
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)

        # Check for 3 consecutive stagnant actions
        repeated = state["history"][-3:]
        if len(repeated) == 3 and all(h.get("kind") == "wait" for h in repeated):
            state["status"] = "blocked"
            state["blocked_reason"] = "Stopped after repeated no-progress WAIT actions."

        return self.snapshot()

    def run_all(self) -> dict[str, Any]:
        """Execute until complete, blocked, or timed out."""
        while self.state["status"] not in {"done", "blocked"}:
            self.step()
        return self.snapshot()

    def close(self):
        if self._owns_browser and getattr(self, "browser", None):
            self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
