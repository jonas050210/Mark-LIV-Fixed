"""Optional local fallback brain (Ollama) for intent and planning.

JARVIS talks through Gemini Live. This module is the *backup* used only when
the cloud path cannot do a job — today: grounding a multi-step goal into tool
steps inside :mod:`core.planner` when the rule-based decomposer finds nothing.

Design rules, in order:

#. **Never required.** Every function degrades to ``None``/``False`` when
   Ollama is not installed, not running, or too slow. Callers MUST treat
   ``None`` as "fall back", never as an error.
#. **Never slow.** Availability is probed passively with a 1 s timeout and
   cached for 30 s; generations carry short timeouts and small output budgets.
   Nothing here ever auto-launches ``ollama serve`` — a fallback path must not
   surprise the user with new processes.
#. **Never greedy.** One request at a time (module-level lock), small context,
   short answers. The budgets assume a modest GPU (RTX 4060 Ti 8 GB class).

Recommended models for 8 GB VRAM: ``llama3.2`` (3B), ``qwen3:4b``,
``mistral:7b-instruct`` (Q4). Set via the ``llm_model`` config key; the
default is ``llama3.2``. Provider/URL come from ``llm_provider``/``llm_url``
(the same keys :mod:`core.llm_client` reads).

This module is stdlib-only at import time: :mod:`core.llm_client` needs
``requests`` and is therefore imported lazily, inside the guarded call.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

_lock = threading.Lock()
_avail_cache: dict[str, float | bool] = {"at": 0.0, "ok": False}
_AVAIL_TTL = 30.0  # seconds between real probes


def _local_conf() -> tuple[str, str, str]:
    """(provider, url, model) from config, with Ollama defaults. Never raises."""
    provider, url, model = "ollama", "http://localhost:11434", "llama3.2"
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if isinstance(cfg, dict):
            provider = str(cfg.get("llm_provider") or provider).strip().lower()
            url = str(
                cfg.get("llm_url")
                or ("http://localhost:1234" if provider == "openai" else url)
            ).strip().rstrip("/")
            model = str(cfg.get("llm_model") or model).strip()
    except Exception:
        pass
    if provider not in ("ollama", "openai"):
        provider = "ollama"
    return provider, url, model


def is_config_enabled() -> bool:
    """Config flag ``local_ai_enabled`` (default True = try when reachable)."""
    try:
        from memory.config_manager import get_local_ai_enabled

        return bool(get_local_ai_enabled())
    except Exception:
        return True


def is_available(timeout: float = 1.0) -> bool:
    """True when a local LLM server answers. Passive, cached, never raises."""
    if not is_config_enabled():
        return False
    now = time.monotonic()
    if now - float(_avail_cache.get("at") or 0.0) < _AVAIL_TTL:
        return bool(_avail_cache.get("ok"))
    provider, url, _model = _local_conf()
    health = f"{url}/v1/models" if provider == "openai" else f"{url}/api/tags"
    ok = False
    try:
        req = urllib.request.Request(health, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= getattr(resp, "status", 0) < 300
    except Exception:
        ok = False
    _avail_cache["at"] = now
    _avail_cache["ok"] = ok
    return ok


def _generate(
    prompt: str,
    system: str,
    *,
    timeout: float,
) -> str | None:
    """One small completion via the configured local backend.

    None on ANY failure: backend down, ``requests`` missing, model missing,
    timeout, unparsable reply. Serialised so concurrent planner calls cannot
    stack up on a small GPU.
    """
    _provider, _url, model = _local_conf()
    with _lock:
        try:
            from core.llm_client import call_llm_text

            text = call_llm_text(
                prompt, system=system, model=model, timeout=int(timeout)
            )
        except Exception:
            _avail_cache["at"] = 0.0  # force a re-probe next time
            _avail_cache["ok"] = False
            return None
    text = (text or "").strip()
    return text or None


def classify_intent(text: str, tool_names: list[str], timeout: float = 15.0) -> str | None:
    """Map free text to ONE tool name from `tool_names`. None = fall back.

    Used only when the rule-based router has no answer. The model must reply
    with a bare tool name; anything else is discarded.
    """
    text = (text or "").strip()
    if not text or not tool_names or not is_available():
        return None
    tools = ", ".join(tool_names[:60])
    reply = _generate(
        text[:500],
        system=(
            "You route a voice-assistant command to exactly one tool. "
            f"Valid tools: {tools}. Reply with ONLY the tool name, "
            "nothing else. If none fits, reply with ONLY the word: none"
        ),
        timeout=timeout,
    )
    if not reply:
        return None
    cand = reply.strip().split()[0].strip("`.,:()").lower()
    for name in tool_names:
        if name.lower() == cand:
            return name
    return None


def plan_steps(
    goal: str, tool_names: list[str], timeout: float = 25.0
) -> list[dict] | None:
    """Decompose `goal` into ``[{tool, params}]`` steps. None = fall back.

    The model replies with a JSON array only. Steps naming unknown tools, or
    non-JSON replies, are discarded wholesale — a half-grounded plan is worse
    than the rule-based one.
    """
    goal = (goal or "").strip()
    if not goal or not tool_names or not is_available():
        return None
    tools = ", ".join(tool_names[:80])
    reply = _generate(
        goal[:800],
        system=(
            "You decompose a voice-assistant goal into tool calls. "
            f"Valid tools: {tools}. Reply with ONLY a JSON array like "
            '[{"tool": "open_app", "params": {"app_name": "Spotify"}}]. '
            "At most 6 steps. No prose, no markdown, no code fences."
        ),
        timeout=timeout,
    )
    if not reply:
        return None
    try:
        # Tolerate fences despite the instruction — then require real JSON.
        cleaned = reply.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        steps = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(steps, list) or not steps:
        return None
    known = {t.lower(): t for t in tool_names}
    grounded: list[dict] = []
    for step in steps[:6]:
        if not isinstance(step, dict):
            return None
        tool = str(step.get("tool", "")).strip().lower()
        if tool not in known:
            return None
        params = step.get("params", {})
        if not isinstance(params, dict):
            return None
        grounded.append({"tool": known[tool], "params": params})
    return grounded or None
