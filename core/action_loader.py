"""
Action discovery, validation, and dispatch — the built-in twin of plugin_loader.

Every actions/*.py that exposes a module-level ``TOOL`` dict is auto-discovered
here, exactly like a drop-in plugin, so main.py never has to hardcode a tool
declaration or a dispatch branch for it. Adding a new bundled action is then the
same one-file operation as writing a plugin: define ``TOOL`` and a handler.

``TOOL`` shape (see actions/open_app.py for a live example):

    TOOL = {
        "name":        "open_app",              # unique, ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
        "description":  "...",                   # what Gemini reads to route the call
        "parameters":  {"type": "OBJECT", ...}, # Gemini function-declaration schema
        "handler":      open_app,                # the callable to run
    }

The handler is invoked through signature introspection: it receives ``parameters``
plus whichever of ``player`` / ``speak`` / ``response`` / ``session_memory`` it
actually declares — so existing action signatures work unchanged.

Discovery runs once at startup; import errors, validation errors, and name
collisions are logged and the offending file is skipped — they NEVER raise out
of discover_actions() and never abort the scan of the remaining files.
"""
from __future__ import annotations

import ast
import copy
import importlib.util
import inspect
import json
import math
import os
import re
import stat
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from core.action_result import ActionResult
from core.action_runtime import runtime as action_runtime

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}
_CTX_KEYS = ("player", "speak", "response", "session_memory", "cancel_event", "action_id", "report_progress", "trusted")
_SCHEMA_TYPES = {"OBJECT", "STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY"}
_MODEL_CONFIRMATION_KEYS = {"confirmed", "confirmation", "confirmation_bypass"}
_MAX_PARAMETER_DEPTH = 8
_MAX_STRING_LENGTH = 1_000_000
_DEFAULT_STRING_LENGTH = 20_000
_MAX_ARRAY_LENGTH = 1_000
_MAX_PARAMETER_BYTES = 1_000_000
_MAX_SCHEMA_BYTES = 250_000


class ParameterValidationError(ValueError):
    """Raised when model-provided tool arguments violate the declared schema."""


def validate_parameter_schema(schema: dict, path: str = "parameters", depth: int = 0) -> None:
    if depth > _MAX_PARAMETER_DEPTH:
        raise ValueError(f"{path} exceeds the maximum schema depth")
    if not isinstance(schema, dict):
        raise ValueError(f"{path} must be an object schema")
    if depth == 0:
        try:
            encoded = json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path} must contain JSON-safe schema data") from exc
        if len(encoded) > _MAX_SCHEMA_BYTES:
            raise ValueError(f"{path} exceeds the schema size limit")
    schema_type = schema.get("type")
    if schema_type not in _SCHEMA_TYPES:
        raise ValueError(f"{path}.type must be one of {sorted(_SCHEMA_TYPES)}")
    enum = schema.get("enum")
    if enum is not None and (
        not isinstance(enum, list) or not enum or len(enum) > 100
        or any(isinstance(item, (dict, list)) for item in enum)
        or any(isinstance(item, str) and len(item) > 1_000 for item in enum)
    ):
        raise ValueError(f"{path}.enum must contain at most 100 bounded scalar values")
    constraint_caps = {
        "minLength": _MAX_STRING_LENGTH,
        "maxLength": _MAX_STRING_LENGTH,
        "maxItems": _MAX_ARRAY_LENGTH,
    }
    for keyword, cap in constraint_caps.items():
        if keyword in schema and (
            isinstance(schema[keyword], bool) or not isinstance(schema[keyword], int)
            or not 0 <= schema[keyword] <= cap
        ):
            raise ValueError(
                f"{path}.{keyword} must be a non-negative integer no greater than {cap}"
            )
    if schema.get("minLength", 0) > schema.get("maxLength", _DEFAULT_STRING_LENGTH):
        raise ValueError(f"{path}.minLength may not exceed maxLength")
    for keyword in ("minimum", "maximum"):
        if keyword in schema and (
            isinstance(schema[keyword], bool) or not isinstance(schema[keyword], (int, float))
            or not math.isfinite(schema[keyword])
        ):
            raise ValueError(f"{path}.{keyword} must be a finite number")
    if schema.get("minimum", -math.inf) > schema.get("maximum", math.inf):
        raise ValueError(f"{path}.minimum may not exceed maximum")
    if enum is not None:
        enum_types_valid = {
            "STRING": lambda item: isinstance(item, str),
            "BOOLEAN": lambda item: isinstance(item, bool),
            "INTEGER": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "NUMBER": lambda item: (
                isinstance(item, (int, float)) and not isinstance(item, bool)
                and math.isfinite(item)
            ),
        }
        if schema_type not in enum_types_valid or any(
            not enum_types_valid[schema_type](item) for item in enum
        ):
            raise ValueError(f"{path}.enum values do not match the schema type")
    if schema_type == "OBJECT":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or len(properties) > 200:
            raise ValueError(f"{path}.properties must be an object with at most 200 entries")
        if (
            not isinstance(required, list)
            or any(not isinstance(value, str) for value in required)
            or len(required) != len(set(required))
        ):
            raise ValueError(f"{path}.required must be a list of unique property names")
        unknown_required = set(required) - set(properties)
        if unknown_required:
            raise ValueError(f"{path}.required references unknown properties: {sorted(unknown_required)}")
        for name, child in properties.items():
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 100
                or any(ord(character) < 32 for character in name)
            ):
                raise ValueError(f"{path}.properties contains an invalid name")
            validate_parameter_schema(child, f"{path}.{name}", depth + 1)
    elif schema_type == "ARRAY":
        items = schema.get("items")
        if not isinstance(items, dict):
            raise ValueError(f"{path}.items must be a schema")
        validate_parameter_schema(items, f"{path}[]", depth + 1)


def validate_parameters(value: Any, schema: dict, path: str = "parameters", depth: int = 0) -> None:
    if depth == 0:
        try:
            encoded_size = len(
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise ParameterValidationError(f"{path} must contain JSON data") from exc
        if encoded_size > _MAX_PARAMETER_BYTES:
            raise ParameterValidationError(f"{path} payload is too large")
    if depth > _MAX_PARAMETER_DEPTH:
        raise ParameterValidationError(f"{path} is nested too deeply")
    expected = schema.get("type")
    if expected == "OBJECT":
        if not isinstance(value, dict):
            raise ParameterValidationError(f"{path} must be an object")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in value or value[name] is None]
        if missing:
            raise ParameterValidationError(f"missing required parameter(s): {', '.join(missing)}")
        unknown = sorted((key for key in value if key not in properties), key=str)
        if unknown:
            raise ParameterValidationError(f"unknown parameter(s): {', '.join(map(str, unknown))}")
        for name, child in value.items():
            validate_parameters(child, properties[name], f"{path}.{name}", depth + 1)
    elif expected == "STRING":
        if not isinstance(value, str):
            raise ParameterValidationError(f"{path} must be text")
        max_length = schema.get("maxLength", _DEFAULT_STRING_LENGTH)
        if len(value) > int(max_length):
            raise ParameterValidationError(f"{path} is too long")
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise ParameterValidationError(f"{path} is too short")
    elif expected == "BOOLEAN":
        if not isinstance(value, bool):
            raise ParameterValidationError(f"{path} must be true or false")
    elif expected == "INTEGER":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ParameterValidationError(f"{path} must be a whole number")
        if abs(value) > 1_000_000_000_000:
            raise ParameterValidationError(f"{path} is outside the supported range")
    elif expected == "NUMBER":
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise ParameterValidationError(f"{path} must be a finite number")
        if abs(value) > 1_000_000_000_000:
            raise ParameterValidationError(f"{path} is outside the supported range")
    elif expected == "ARRAY":
        if not isinstance(value, list):
            raise ParameterValidationError(f"{path} must be an array")
        if len(value) > int(schema.get("maxItems", _MAX_ARRAY_LENGTH)):
            raise ParameterValidationError(f"{path} has too many items")
        for index, child in enumerate(value):
            validate_parameters(child, schema["items"], f"{path}[{index}]", depth + 1)
    else:  # Definitions are checked at discovery; fail closed if one slips through.
        raise ParameterValidationError(f"{path} has an unsupported schema type")

    if value is not None and "enum" in schema and value not in schema["enum"]:
        raise ParameterValidationError(f"{path} must be one of: {', '.join(map(str, schema['enum']))}")
    if expected in {"INTEGER", "NUMBER"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ParameterValidationError(f"{path} is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ParameterValidationError(f"{path} is above the maximum")


# A tool may declare that the model should NOT be held up waiting for it.
# `behavior` goes to the API with the declaration; `scheduling` decides when the
# eventual result is allowed back into the conversation:
#   WHEN_IDLE  — wait for a gap in the speech (the sane default)
#   SILENT     — record it, do not prompt a reply (the tool already announced)
#   INTERRUPT  — cut in immediately (only when the answer cannot wait)
_BEHAVIORS = ("BLOCKING", "NON_BLOCKING")
_SCHEDULING = ("WHEN_IDLE", "SILENT", "INTERRUPT")
_ACTION_WORKER_SLOTS = threading.BoundedSemaphore(8)


def _opt_upper(value, allowed: tuple[str, ...]) -> Optional[str]:
    v = str(value or "").strip().upper()
    return v if v in allowed else None


def _metadata_bool(metadata: dict, key: str, default: bool = False) -> bool:
    if key not in metadata:
        return default
    value = metadata[key]
    if not isinstance(value, bool):
        raise ValueError(f"TOOL['{key}'] must be true or false")
    return value


@dataclass
class ActionRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    handler: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    available: bool = True
    error: str = ""
    behavior: Optional[str] = None     # None = the API's default (blocking)
    scheduling: Optional[str] = None   # None = the API's default (WHEN_IDLE)
    category: str = "computer"
    risk: str = "low"
    requires_confirmation: bool = False
    # Some bundled actions contain both safe and irreversible operations.  A
    # list lets the registry enforce confirmation for only the risky operation
    # instead of making harmless volume/list commands ask every time.
    confirmation_actions: tuple[str, ...] = ()
    requires_admin: bool = False
    undoable: bool = False
    timeout_seconds: float = 60.0


class ActionRegistry:
    def __init__(self, actions: dict[str, ActionRecord], logger: Callable[[str], None]):
        self._actions = actions          # name -> ActionRecord, VALID entries only
        self._all_records: list[ActionRecord] = []
        self._logger = logger

    # -- called by main.py at LiveConnectConfig build time --
    def get_tool_declarations(self) -> list[dict]:
        out = []
        for rec in self._actions.values():
            decl = {"name": rec.name, "description": rec.description,
                    "parameters": copy.deepcopy(rec.parameters)}
            if rec.behavior:
                decl["behavior"] = rec.behavior
            out.append(decl)
        return out

    def has(self, name: str) -> bool:
        return name in self._actions

    def scheduling(self, name: str) -> Optional[str]:
        """How this action's result should re-enter the conversation, if it said."""
        rec = self._actions.get(name)
        return rec.scheduling if rec else None

    def names(self) -> set[str]:
        return set(self._actions.keys())

    def admin_manifest(self) -> list[dict]:
        """JSON-safe capability list for the desktop/phone admin panel.

        The panel must use the same registry as voice commands; maintaining a
        second hard-coded list is how capabilities drifted in the past.
        """
        records = sorted(self._all_records or list(self._actions.values()), key=lambda r: r.name)
        return [
            {
                "name": rec.name,
                "description": rec.description,
                "file": rec.file,
                "valid": rec.valid and rec.available,
                "available": rec.available,
                "error": rec.error,
                "category": rec.category,
                "risk": rec.risk,
                "requires_confirmation": rec.requires_confirmation,
                "confirmation_actions": list(rec.confirmation_actions),
                "requires_admin": rec.requires_admin,
                "undoable": rec.undoable,
                "timeout_seconds": rec.timeout_seconds,
            }
            for rec in records
        ]

    def record(self, name: str) -> Optional[ActionRecord]:
        return self._actions.get(name)

    def timeout(self, name: str) -> float:
        rec = self._actions.get(name)
        return rec.timeout_seconds if rec else 60.0

    def _needs_confirmation(self, rec: ActionRecord, parameters: dict) -> bool:
        if rec.confirmation_actions:
            operation = str((parameters or {}).get("action") or "").strip().casefold()
            if not operation and parameters.get("task"):
                operation = "task"
            return operation in rec.confirmation_actions
        return rec.requires_confirmation

    def _confirmation_title(self, rec: ActionRecord, parameters: dict) -> tuple[str, str]:
        operation = str((parameters or {}).get("action") or ("task" if parameters.get("task") else rec.name)).strip().replace("_", " ")
        titles = {
            "delete": ("Delete a file or folder", "The item will be moved to the Recycle Bin / Trash."),
            "close": ("Close an application", "The application may contain unsaved work."),
            "close app": ("Close the active application", "The application may contain unsaved work."),
            "close window": ("Close the active window", "The window may contain unsaved work."),
            "restart": ("Restart the computer", "The computer will restart after the confirmation."),
            "shutdown": ("Shut down the computer", "The computer will power off after the confirmation."),
            "toggle wifi": ("Change Wi-Fi state", "The network connection may be interrupted."),
            "clean": ("Clean the desktop", "Desktop files will be moved into an archive folder."),
            "task": ("Run a generated desktop task", "Generated automation can interact with files or the desktop."),
            "install": ("Install a game", "The game launcher will download and install files."),
            "update": ("Update games", "The game launcher will download and change installed files."),
            "schedule": ("Schedule game updates", "MARK LIV will create a recurring system task."),
        }
        title, detail = titles.get(
            operation, (f"Run {rec.name}", f"MARK LIV is ready to run: {operation}.")
        )
        if rec.name == "session_manager" and operation == "delete":
            title = "Delete a saved session"
            detail = "The private conversation snapshot will be permanently removed."
        target_parts = []
        for key in ("file_path", "path", "source", "destination", "app", "name", "setting", "value"):
            value = parameters.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                clean = " ".join(str(value).split())[:240]
                if clean:
                    target_parts.append(f"{key.replace('_', ' ')}: {clean}")
        if target_parts:
            detail = f"{detail} Target — " + "; ".join(target_parts[:4])
        return title, detail[:1_000]

    def _invoke(self, rec: ActionRecord, parameters: dict, ctx: dict) -> ActionResult:
        value = _call_handler(rec.handler, parameters, ctx)
        return ActionResult.from_handler(rec.name, value)

    def _invoke_bounded(self, rec: ActionRecord, parameters: dict, ctx: dict) -> ActionResult:
        """Bound legacy Python handlers without pretending threads are killable.

        The daemon worker prevents a stuck legacy library from blocking JARVIS;
        actions that launch child processes use ``core.process_runner`` to kill
        their entire child process group as well.  The cancellation event is
        available to newly written actions for cooperative cancellation.
        """
        done = threading.Event()
        box: dict[str, object] = {}
        cancel = ctx.get("cancel_event")
        if cancel is None or not hasattr(cancel, "set") or not hasattr(cancel, "is_set"):
            cancel = threading.Event()
        call_ctx = {**ctx, "cancel_event": cancel}
        if cancel.is_set():
            return ActionResult.failure(
                rec.name, f"Action '{rec.name}' was cancelled.", status="cancelled"
            )
        worker_slots = _ACTION_WORKER_SLOTS
        if not worker_slots.acquire(blocking=False):
            return ActionResult.failure(
                rec.name,
                "Action worker capacity is busy; wait for existing actions to finish.",
                status="busy",
            )

        def _worker() -> None:
            try:
                if cancel.is_set():
                    box["result"] = ActionResult.failure(
                        rec.name, f"Action '{rec.name}' was cancelled.", status="cancelled"
                    )
                    return
                box["result"] = self._invoke(rec, parameters, call_ctx)
            except Exception as exc:  # keep the action worker from escaping
                box["error"] = exc
            finally:
                done.set()
                worker_slots.release()

        try:
            threading.Thread(
                target=_worker, daemon=True, name=f"action-{rec.name}"
            ).start()
        except Exception:
            worker_slots.release()
            raise
        deadline = time.monotonic() + rec.timeout_seconds
        while not done.wait(0.1):
            if cancel.is_set():
                return ActionResult.failure(
                    rec.name, f"Action '{rec.name}' was cancelled.", status="cancelled"
                )
            if time.monotonic() >= deadline:
                cancel.set()
                return ActionResult.failure(
                    rec.name,
                    f"Action '{rec.name}' exceeded its {rec.timeout_seconds:.0f}-second time limit.",
                    status="timed_out",
                )
        if cancel.is_set():
            return ActionResult.failure(rec.name, f"Action '{rec.name}' was cancelled.", status="cancelled")
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["result"]  # type: ignore[return-value]

    # -- called by main.py from _execute_tool --
    def execute(self, name: str, parameters: dict, ctx: dict | None = None) -> ActionResult:
        """Apply registry policy and execute one action.

        The handler compatibility layer still accepts plain strings, but every
        path through the registry now has one status vocabulary and one place
        for confirmation/admin policy.  The returned object is deliberately
        JSON-safe for the dashboard and easily converted to the old tool text.
        """
        started = time.monotonic()
        ctx = dict(ctx or {})
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            return ActionResult.failure(
                name,
                "Tool parameters must be a JSON object.",
                status="invalid_parameters",
            )
        try:
            parameters = copy.deepcopy(parameters)
            encoded_parameters = json.dumps(
                parameters, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            if len(encoded_parameters) > _MAX_PARAMETER_BYTES:
                raise ValueError("parameter payload is too large")
        except Exception:
            return ActionResult.failure(
                name,
                "Tool parameters are not bounded JSON data.",
                status="invalid_parameters",
            )
        # A model-supplied confirmation flag is never authority. Drop legacy
        # variants before schema validation and before invoking the handler.
        for key in _MODEL_CONFIRMATION_KEYS:
            parameters.pop(key, None)
        action_id = str(ctx.get("action_id") or "")
        if action_id:
            action_runtime.update(action_id, status="running", progress=10,
                                  message=f"Running {name}")
            external_cancel = action_runtime.cancellation_event(action_id)
            if external_cancel is not None:
                ctx["cancel_event"] = external_cancel
            ctx["report_progress"] = lambda progress, message="": action_runtime.update(
                action_id, progress=progress, message=message or f"Running {name}"
            )
        rec = self._actions.get(name)
        if rec is None or not rec.valid or not rec.available:
            detail = rec.error if rec is not None and rec.error else "the action is not installed"
            return ActionResult.failure(name, f"Action '{name}' is unavailable: {detail}", status="unavailable")
        if rec.requires_admin and not bool(ctx.get("trusted", False)):
            return ActionResult.failure(name, "This action is restricted to an authenticated local control session.", status="forbidden")
        try:
            validate_parameters(parameters, rec.parameters)
        except (ParameterValidationError, TypeError, ValueError) as exc:
            message = f"Invalid parameters for '{name}': {exc}"
            if action_id:
                action_runtime.finish(action_id, ok=False, message=message)
            return ActionResult.failure(
                name, message, error=str(exc), status="invalid_parameters"
            )
        if ctx.get("cancel_event") is not None and ctx["cancel_event"].is_set():
            return ActionResult.failure(name, f"Action '{name}' was cancelled.", status="cancelled")

        if self._needs_confirmation(rec, parameters) and not bool(ctx.get("confirmation_bypass", False)):
            try:
                from core import confirm
                if confirm.pending_title():
                    message = (
                        "There is already a confirmation waiting on screen. "
                        "Ask the user to answer that one first."
                    )
                    if action_id:
                        action_runtime.finish(action_id, ok=False, message=message)
                    return ActionResult.failure(
                        name, message, status="confirmation_busy"
                    )
                title, detail = self._confirmation_title(rec, parameters)
                def _confirmed() -> str:
                    try:
                        result = self._invoke_bounded(rec, parameters, {**ctx, "confirmation_bypass": True})
                        self._logger(f"Action '{name}' confirmed with status '{result.status}'.")
                        if action_id:
                            action_runtime.finish(
                                action_id, ok=result.ok, message=result.as_text(),
                                status=result.status,
                            )
                        return result.as_text()
                    except Exception as exc:  # pragma: no cover - worker safety net
                        kind = type(exc).__name__
                        message = f"Tool '{name}' failed after confirmation ({kind})."
                        self._logger(message)
                        if action_id:
                            action_runtime.finish(action_id, ok=False, message=message)
                        return message
                def _confirmation_cancelled(reason: str) -> None:
                    if not action_id:
                        return
                    # A dashboard cancellation already sets the event. A HUD
                    # cancel must set it here so the live-action card cannot
                    # remain stuck at confirmation_pending forever.
                    if reason == "cancelled":
                        action_runtime.cancel(action_id)
                    label = "Confirmation expired" if reason == "expired" else "Cancelled by user"
                    action_runtime.finish(action_id, ok=False, message=label)

                confirmation_key = action_id or f"{name}:{time.monotonic_ns()}"
                message = confirm.request(
                    confirmation_key,
                    title,
                    detail,
                    _confirmed,
                    on_cancel=_confirmation_cancelled,
                )
                if action_id:
                    action_runtime.update(
                        action_id,
                        status="confirmation_pending",
                        progress=25,
                        message="Waiting for confirmation",
                    )
                return ActionResult(name, False, "confirmation_pending", message,
                                    duration_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:
                kind = type(exc).__name__
                message = (
                    f"Confirmation could not be requested ({kind}). Nothing was done."
                )
                if action_id:
                    action_runtime.finish(action_id, ok=False, message=message)
                return ActionResult.failure(
                    name, message, error=kind, status="confirmation_failed"
                )

        try:
            result = self._invoke_bounded(rec, parameters, ctx)
            return ActionResult(
                action=result.action, ok=result.ok, status=result.status,
                message=result.message,
                duration_ms=int((time.monotonic() - started) * 1000),
                data=result.data, error=result.error,
            )
        except Exception as exc:
            kind = type(exc).__name__
            self._logger(f"Action '{name}' crashed during execute() ({kind}).")
            traceback.print_tb(exc.__traceback__)
            return ActionResult.failure(
                name, f"Tool '{name}' failed ({kind}).", error=kind
            )

    def run(self, name: str, parameters: dict, ctx: dict | None = None) -> str:
        """Compatibility wrapper for older callers and plugins."""
        return self.execute(name, parameters, ctx).as_text()


def _call_handler(fn: Callable, parameters: dict, ctx: dict) -> Any:
    """Invoke the handler passing only the context kwargs it actually declares
    (or all of them if it has **kwargs), so each action's existing signature
    works unchanged."""
    sig = inspect.signature(fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    for key in _CTX_KEYS:
        if has_var_kw or key in sig.parameters:
            kwargs[key] = ctx.get(key)
    return fn(parameters=parameters, **kwargs)


def _validate(module, filename: str) -> ActionRecord:
    """Returns an ActionRecord; .valid=False + .error set on any problem. Never raises."""
    tool = getattr(module, "TOOL", None)
    if not isinstance(tool, dict):
        return ActionRecord(name=Path(filename).stem, file=filename,
                            error="No module-level TOOL dict (not a discoverable action).")

    name = tool.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return ActionRecord(name=str(name or Path(filename).stem), file=filename,
                            error="TOOL['name'] missing or not a valid identifier.")

    description = tool.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 4_000
        or any(ord(char) < 32 and char not in "\n\t" for char in description)
    ):
        return ActionRecord(name=name, file=filename,
                            error="TOOL['description'] must be non-empty bounded text.")

    parameters = tool.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return ActionRecord(name=name, file=filename,
                            error="TOOL['parameters'] must be a dict with \"type\": \"OBJECT\".")
    try:
        validate_parameter_schema(parameters)
    except (TypeError, ValueError) as exc:
        return ActionRecord(name=name, file=filename,
                            error=f"Invalid TOOL parameter schema: {exc}")

    handler = tool.get("handler")
    if not callable(handler):
        return ActionRecord(name=name, file=filename,
                            error="TOOL['handler'] missing or not callable.")

    # Confirmation is explicit policy, not a model-supplied ``confirmed``
    # parameter.  Keep only genuinely high-impact defaults here; actions that
    # combine safe and destructive operations declare ``confirmation_actions``
    # in their TOOL metadata below.
    inferred_confirmation = False
    inferred_admin = name in {"system_control"}
    raw_confirm_actions = tool.get("confirmation_actions", ())
    if isinstance(raw_confirm_actions, str):
        raw_confirm_actions = (raw_confirm_actions,)
    if (not isinstance(raw_confirm_actions, (list, tuple, set))
            or any(not isinstance(value, str) for value in raw_confirm_actions)):
        return ActionRecord(
            name=name, file=filename,
            error="TOOL['confirmation_actions'] must contain action names.",
        )
    confirm_actions = tuple(
        value.strip().casefold() for value in raw_confirm_actions if value.strip()
    )
    behavior = _opt_upper(tool.get("behavior"), _BEHAVIORS)
    scheduling = _opt_upper(tool.get("scheduling"), _SCHEDULING)
    if "behavior" in tool and behavior is None:
        return ActionRecord(name=name, file=filename, error="TOOL['behavior'] is invalid.")
    if "scheduling" in tool and scheduling is None:
        return ActionRecord(name=name, file=filename, error="TOOL['scheduling'] is invalid.")
    try:
        timeout_raw = tool.get("timeout_seconds", 60.0)
        if isinstance(timeout_raw, bool):
            raise ValueError
        timeout_seconds = float(timeout_raw)
        if not 1.0 <= timeout_seconds <= 900.0:
            raise ValueError
        requires_confirmation = _metadata_bool(
            tool, "requires_confirmation", inferred_confirmation
        )
        requires_admin = _metadata_bool(tool, "requires_admin", inferred_admin)
        undoable = _metadata_bool(tool, "undoable", False)
    except (TypeError, ValueError) as exc:
        return ActionRecord(name=name, file=filename,
                            error=str(exc) or "TOOL timeout/boolean metadata is invalid.")
    risk = str(tool.get("risk") or ("high" if inferred_confirmation or confirm_actions else "low")).strip().lower()
    if risk not in {"low", "medium", "high"}:
        return ActionRecord(name=name, file=filename, error="TOOL['risk'] must be low, medium, or high.")
    category = str(tool.get("category") or "computer").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", category):
        return ActionRecord(name=name, file=filename, error="TOOL['category'] is invalid.")
    return ActionRecord(name=name, description=description.strip(), parameters=copy.deepcopy(parameters),
                        handler=handler, file=filename, valid=True, error="",
                        behavior=behavior,
                        scheduling=scheduling,
                        category=category,
                        risk=risk,
                        requires_confirmation=requires_confirmation,
                        confirmation_actions=confirm_actions,
                        requires_admin=requires_admin,
                        undoable=undoable,
                        timeout_seconds=timeout_seconds)


def _unavailable_handler(parameters: dict | None = None, **_ctx) -> str:
    # Replaced per-record with a closure below; this fallback exists so a
    # malformed optional action can never make discovery crash.
    return "This action is unavailable because an optional dependency is missing."


def _is_reparse_point(details) -> bool:
    return bool(int(getattr(details, "st_file_attributes", 0)) & 0x400)


def _trusted_action_source(path: Path, actions_dir: Path) -> str:
    """Read packaged action code through a bounded no-follow descriptor."""
    directory_details = actions_dir.lstat()
    if (
        actions_dir.is_symlink()
        or _is_reparse_point(directory_details)
        or not stat.S_ISDIR(directory_details.st_mode)
    ):
        raise PermissionError("actions directory must be a regular directory")
    if os.name != "nt":
        if directory_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("actions directory is writable by other users")
        if hasattr(os, "getuid") and directory_details.st_uid not in {0, os.getuid()}:
            raise PermissionError("actions directory has an untrusted owner")

    details = path.lstat()
    if path.is_symlink() or _is_reparse_point(details) or not stat.S_ISREG(details.st_mode):
        raise PermissionError("action files must be regular files, not links")
    if details.st_size > 2_000_000:
        raise PermissionError("action file exceeds the 2 MB source limit")
    if os.name != "nt":
        if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("action file is writable by other users")
        if hasattr(os, "getuid") and details.st_uid not in {0, os.getuid()}:
            raise PermissionError("action file has an untrusted owner")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or opened.st_size > 2_000_000
        ):
            raise PermissionError("action source changed during validation")
        if os.name != "nt" and (
            opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (hasattr(os, "getuid") and opened.st_uid not in {0, os.getuid()})
        ):
            raise PermissionError("action source permissions changed during validation")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(2_000_001)
    finally:
        os.close(descriptor)
    if len(content) > 2_000_000:
        raise PermissionError("action file exceeds the 2 MB source limit")
    return content.decode("utf-8-sig")


def _metadata_without_import(path: Path, source: str | None = None) -> dict | None:
    """Read safe TOOL metadata from a module that could not be imported.

    Optional native packages often fail at import time.  Literal AST parsing
    lets the registry expose the action and return an actionable install message
    instead of silently deleting the capability from the model's vocabulary.
    The handler is intentionally never evaluated.
    """
    try:
        if source is None:
            return None
        tree = ast.parse(source, filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "TOOL" for target in node.targets
            ):
                if not isinstance(node.value, ast.Dict):
                    return None
                value = {}
                for key_node, value_node in zip(node.value.keys, node.value.values):
                    try:
                        key = ast.literal_eval(key_node)
                        if key == "handler":
                            continue
                        value[key] = ast.literal_eval(value_node)
                    except Exception:
                        # A dynamic optional field is not needed to expose the
                        # action; skip only that field.
                        continue
                return value if isinstance(value, dict) else None
    except Exception:
        return None
    return None


def _unavailable_record(
    path: Path, error: Exception, source: str | None = None
) -> ActionRecord | None:
    meta = _metadata_without_import(path, source)
    if not isinstance(meta, dict):
        return None
    name = meta.get("name")
    description = meta.get("description")
    parameters = meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return None
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 4_000
        or any(ord(char) < 32 and char not in "\n\t" for char in description)
    ):
        return None
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        parameters = copy.deepcopy(_DEFAULT_PARAMS)
    else:
        try:
            validate_parameter_schema(parameters)
        except (TypeError, ValueError):
            parameters = copy.deepcopy(_DEFAULT_PARAMS)
        else:
            parameters = copy.deepcopy(parameters)
    reason = f"import failed ({type(error).__name__})"
    if isinstance(error, ModuleNotFoundError):
        missing = str(getattr(error, "name", "") or "optional dependency")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", missing):
            missing = "optional dependency"
        reason = f"missing optional dependency '{missing}'"
    def _missing_handler(parameters: dict | None = None, **_ctx) -> str:
        return f"Action '{name}' is unavailable: {reason}. Install the dependency and restart MARK LIV."
    return ActionRecord(
        name=name, description=description.strip(), parameters=parameters,
        handler=_missing_handler, file=path.name, valid=True, available=False,
        error=reason, behavior=_opt_upper(meta.get("behavior"), _BEHAVIORS),
        scheduling=_opt_upper(meta.get("scheduling"), _SCHEDULING),
        category=str(meta.get("category") or "computer"),
        risk=str(meta.get("risk") or "low"),
        requires_confirmation=bool(meta.get("requires_confirmation", False)),
        requires_admin=bool(meta.get("requires_admin", False)),
        undoable=bool(meta.get("undoable", False)),
        timeout_seconds=60.0,
    )


def discover_actions(actions_dir: Path, reserved_names: set[str] | None = None,
                     logger: Callable[[str], None] = print) -> ActionRegistry:
    """
    Scans actions_dir for *.py files (skips files starting with '_'). A file is
    only treated as an action if it exposes a module-level TOOL dict; files
    without one (shared helpers, capture-only modules) are silently ignored.
    Import/validation errors and name collisions are logged and the file is
    skipped — they NEVER raise out of this function.
    """
    reserved = reserved_names or set()
    actions_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, ActionRecord] = {}
    all_records: list[ActionRecord] = []

    files = sorted(actions_dir.glob("*.py"), key=lambda p: p.name)  # deterministic order
    for path in files:
        if path.name.startswith("_"):
            continue
        source: str | None = None
        try:
            source = _trusted_action_source(path, actions_dir)
            module_name = f"actions.{path.stem}"
            # Reuse the already-imported module when it came from this exact
            # file so handlers are the same objects the rest of the app holds.
            # Test registries and plugin reloads can scan another directory
            # with the same filename, though; reusing that stale module would
            # silently expose the previous TOOL metadata.
            module = sys.modules.get(module_name)
            loaded_from = getattr(module, "__file__", "") if module is not None else ""
            try:
                same_file = bool(loaded_from) and Path(loaded_from).resolve() == path.resolve()
            except OSError:
                same_file = False
            if module is not None and not same_file:
                sys.modules.pop(module_name, None)
                module = None
            if module is None:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("could not build import spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    code = compile(source, str(path), "exec", dont_inherit=True)
                    exec(code, module.__dict__)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise

            if getattr(module, "TOOL", None) is None:
                continue   # not an action file — a helper/capture-only module

            rec = _validate(module, path.name)

            if rec.valid and rec.name in reserved:
                rec = ActionRecord(name=rec.name, file=path.name,
                                   error=f"Name '{rec.name}' collides with a reserved core tool — rejected.")
            elif rec.valid and rec.name in valid:
                other = valid[rec.name].file
                rec = ActionRecord(name=rec.name, file=path.name,
                                   error=f"Name '{rec.name}' already used by action '{other}' — rejected.")

        except Exception as e:
            rec = _unavailable_record(path, e, source)
            if rec is None:
                rec = ActionRecord(
                    name=path.stem,
                    file=path.name,
                    error=f"Failed to load ({type(e).__name__}). See the console for details.",
                )
            # Optional dependency failures are expected on minimal installs;
            # keep the traceback on the console for diagnostics without making
            # the whole application fail to start.
            if not (rec.valid and not rec.available) and not isinstance(e, PermissionError):
                traceback.print_tb(e.__traceback__)

        all_records.append(rec)
        if rec.valid:
            valid[rec.name] = rec
            if rec.available:
                logger(f"Action loaded: {rec.name} ({path.name})")
            else:
                logger(f"Action unavailable: {rec.name} ({path.name}) — {rec.error}")
        else:
            # Only log a rejection if the file actually tried to be an action.
            logger(f"Action rejected: {path.name} — {rec.error}")

    registry = ActionRegistry(valid, logger)
    registry._all_records = all_records
    logger(f"Action discovery complete: {len(valid)} active.")
    return registry
