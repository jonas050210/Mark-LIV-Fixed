"""
TypeSafe makes choices; Mark LIV's Gemini 2.5 Flash writes field values.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Tuple

import httpx

from .prompts import NEXT_ACTION, TARGET, TEXT_VALUE
from memory.config_manager import get_gemini_key, get_typesafe_key

# HTTP client with persistent connection
CLIENT = httpx.Client(timeout=25.0)


def post_json(url: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as e:
            raise RuntimeError(f"Model connection failed ({e}); no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}: {response.text[:200]}")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer: dict[str, Any], ids: set | dict | list) -> dict[str, Any]:
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions: list[dict[str, Any]]) -> Tuple[list, dict, dict]:
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action.get("kind")
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def choose(state: dict[str, Any], goal: str, history: list[dict[str, Any]], typesafe_key: str | None = None) -> dict[str, Any]:
    key = typesafe_key or get_typesafe_key() or os.environ.get("TYPESAFE_API_KEY", "")
    if not key:
        raise ValueError("TypeSafe API key is not configured. Please set it in Settings → API Keys.")

    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key_name: labels[key_name] for key_name in targets}
    operations.update({key_name: value["label"] for key_name, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text") if k in state},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    result = post_json("https://api.typesafe.ai/v1/systemone", key, body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice_id = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice_id = controls[operation]["id"] if operation in controls else operation
        probabilities[choice_id] = operation_answer["probabilities"][operation]

    return {
        "choice": choice_id,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer.get("confidence") if target_answer else None,
        "raw_answers": result.get("answers", {}),
        "model": result.get("model", "jev-latest"),
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal: str, action: dict[str, Any], page: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page.get("title", ""), "text": page.get("text", "")[:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def field_text(context: dict[str, Any], gemini_key: str | None = None) -> Tuple[str, dict[str, Any]]:
    """Generate field text using Mark LIV's Gemini 2.5 Flash via OpenAI-compatible endpoint."""
    key = gemini_key or get_gemini_key() or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise ValueError("Gemini API key is required for TYPE_TEXT generation; none configured.")

    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    model = "gemini-2.5-flash"

    started = time.perf_counter()
    endpoint = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": TEXT_VALUE},
            {"role": "user", "content": json.dumps(context)},
        ],
    }

    result = post_json(endpoint, key, payload)
    try:
        content_str = result["choices"][0]["message"]["content"]
        output = json.loads(content_str)
        value = output.get("text")
        if set(output.keys()) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None

    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
