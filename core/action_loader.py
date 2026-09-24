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
import importlib.util
import inspect
import re
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


# A tool may declare that the model should NOT be held up waiting for it.
# `behavior` goes to the API with the declaration; `scheduling` decides when the
# eventual result is allowed back into the conversation:
#   WHEN_IDLE  — wait for a gap in the speech (the sane default)
#   SILENT     — record it, do not prompt a reply (the tool already announced)
#   INTERRUPT  — cut in immediately (only when the answer cannot wait)
_BEHAVIORS = ("BLOCKING", "NON_BLOCKING")
_SCHEDULING = ("WHEN_IDLE", "SILENT", "INTERRUPT")


def _opt_upper(value, allowed: tuple[str, ...]) -> Optional[str]:
    v = str(value or "").strip().upper()
    return v if v in allowed else None


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
                    "parameters": rec.parameters}
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
            "organize": ("Organize the desktop", "Desktop files will be moved into category folders."),
            "task": ("Run a generated desktop task", "Generated automation can interact with files or the desktop."),
            "install": ("Install a game", "The game launcher will download and install files."),
            "update": ("Update games", "The game launcher will download and change installed files."),
            "schedule": ("Schedule game updates", "MARK LIV will create a recurring system task."),
        }
        return titles.get(operation, (f"Run {rec.name}", f"MARK LIV is ready to run: {operation}."))

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

        def _worker() -> None:
            try:
                box["result"] = self._invoke(rec, parameters, call_ctx)
            except Exception as exc:  # keep the action worker from escaping
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True, name=f"action-{rec.name}").start()
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
        parameters = dict(parameters or {})
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
        if ctx.get("cancel_event") is not None and ctx["cancel_event"].is_set():
            return ActionResult.failure(name, f"Action '{name}' was cancelled.", status="cancelled")

        if self._needs_confirmation(rec, parameters) and not bool(ctx.get("confirmation_bypass", False)):
            try:
                from core import confirm
                if confirm.pending_title():
                    return ActionResult.failure(name, "There is already a confirmation waiting on screen. Ask the user to answer that one first.", status="confirmation_pending")
                title, detail = self._confirmation_title(rec, parameters)
                def _confirmed() -> str:
                    try:
                        result = self._invoke_bounded(rec, parameters, {**ctx, "confirmation_bypass": True})
                        self._logger(f"Action '{name}' confirmed: {result.as_text()[:160]}")
                        if action_id:
                            action_runtime.finish(action_id, ok=result.ok, message=result.as_text())
                        return result.as_text()
                    except Exception as exc:  # pragma: no cover - worker safety net
                        self._logger(f"Action '{name}' failed after confirmation: {exc}")
                        return f"Tool '{name}' failed: {exc}"
                message = confirm.request(name, title, detail, _confirmed)
                return ActionResult(name, False, "confirmation_pending", message,
                                    duration_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:
                return ActionResult.failure(name, f"Confirmation could not be requested: {exc}. Nothing was done.", error=str(exc), status="confirmation_failed")

        try:
            result = self._invoke_bounded(rec, parameters, ctx)
            return ActionResult(
                action=result.action, ok=result.ok, status=result.status,
                message=result.message,
                duration_ms=int((time.monotonic() - started) * 1000),
                data=result.data, error=result.error,
            )
        except Exception as exc:
            self._logger(f"Action '{name}' crashed during execute(): {exc}")
            traceback.print_exc()
            return ActionResult.failure(name, f"Tool '{name}' failed: {exc}", error=str(exc))

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
    if not isinstance(description, str) or not description.strip():
        return ActionRecord(name=name, file=filename,
                            error="TOOL['description'] missing or empty.")

    parameters = tool.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return ActionRecord(name=name, file=filename,
                            error="TOOL['parameters'] must be a dict with \"type\": \"OBJECT\".")

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
    if not isinstance(raw_confirm_actions, (list, tuple, set)):
        raw_confirm_actions = ()
    confirm_actions = tuple(str(v).strip().casefold() for v in raw_confirm_actions if str(v).strip())
    try:
        timeout_seconds = max(1.0, min(float(tool.get("timeout_seconds", 60.0)), 900.0))
    except (TypeError, ValueError):
        timeout_seconds = 60.0
    risk = str(tool.get("risk") or ("high" if inferred_confirmation or confirm_actions else "low"))
    return ActionRecord(name=name, description=description.strip(), parameters=parameters,
                        handler=handler, file=filename, valid=True, error="",
                        behavior=_opt_upper(tool.get("behavior"), _BEHAVIORS),
                        scheduling=_opt_upper(tool.get("scheduling"), _SCHEDULING),
                        category=str(tool.get("category") or "computer"),
                        risk=risk,
                        requires_confirmation=bool(tool.get("requires_confirmation", inferred_confirmation)),
                        confirmation_actions=confirm_actions,
                        requires_admin=bool(tool.get("requires_admin", inferred_admin)),
                        undoable=bool(tool.get("undoable", False)),
                        timeout_seconds=timeout_seconds)


def _unavailable_handler(parameters: dict | None = None, **_ctx) -> str:
    # Replaced per-record with a closure below; this fallback exists so a
    # malformed optional action can never make discovery crash.
    return "This action is unavailable because an optional dependency is missing."


def _metadata_without_import(path: Path) -> dict | None:
    """Read safe TOOL metadata from a module that could not be imported.

    Optional native packages often fail at import time.  Literal AST parsing
    lets the registry expose the action and return an actionable install message
    instead of silently deleting the capability from the model's vocabulary.
    The handler is intentionally never evaluated.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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


def _unavailable_record(path: Path, error: Exception) -> ActionRecord | None:
    meta = _metadata_without_import(path)
    if not isinstance(meta, dict):
        return None
    name = meta.get("name")
    description = meta.get("description")
    parameters = meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return None
    if not isinstance(description, str) or not description.strip():
        return None
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        parameters = dict(_DEFAULT_PARAMS)
    reason = str(error)
    if isinstance(error, ModuleNotFoundError):
        missing = getattr(error, "name", "optional dependency") or "optional dependency"
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
        try:
            module_name = f"actions.{path.stem}"
            # Reuse the already-imported module when present so handlers are the
            # same objects the rest of the app holds.
            module = sys.modules.get(module_name)
            if module is None:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("could not build import spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
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
            rec = _unavailable_record(path, e)
            if rec is None:
                rec = ActionRecord(name=path.stem, file=path.name,
                                   error=f"Failed to load: {e}")
            # Optional dependency failures are expected on minimal installs;
            # keep the traceback on the console for diagnostics without making
            # the whole application fail to start.
            if not (rec.valid and not rec.available):
                traceback.print_exc()

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
