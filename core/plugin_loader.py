"""
Plugin discovery, validation, collision detection, and dispatch.

Discovery runs once (JarvisLive.__init__ calls discover_plugins()); the resulting
PluginRegistry is cached for the process lifetime. Enable/disable state is re-read
from config on every call to get_tool_declarations() / run() / list_for_ui(), so
toggling a plugin does not require restarting the app or re-importing anything.
"""
from __future__ import annotations

import copy
import ast
import importlib.util
import inspect
import math
import os
import re
import stat
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core.action_loader import (
    ParameterValidationError,
    validate_parameters,
    validate_parameter_schema,
)
from core.action_result import ActionResult
from memory.config_manager import get_plugin_enabled, get_plugin_config

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}

# Optional, and the same contract actions use: a plugin that takes a moment can
# say so, and the model carries on talking instead of waiting on it. See
# core/action_loader.py for what each value means.
_BEHAVIORS = ("BLOCKING", "NON_BLOCKING")
_SCHEDULING = ("WHEN_IDLE", "SILENT", "INTERRUPT")
_PLUGIN_RUN_SLOTS = threading.BoundedSemaphore(8)


def _opt_upper(value, allowed: tuple[str, ...]) -> Optional[str]:
    v = str(value or "").strip().upper()
    return v if v in allowed else None


@dataclass
class PluginRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    run: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""
    settings: Optional[dict] = None   # optional PLUGIN_SETTINGS schema (config fields)
    behavior: Optional[str] = None    # None = the API's default (blocking)
    scheduling: Optional[str] = None  # None = the API's default (WHEN_IDLE)
    timeout_seconds: float = 60.0


class PluginRegistry:
    def __init__(self, plugins: dict[str, PluginRecord], logger: Callable[[str], None],
                 notify: Callable[[str], None] | None = None):
        self._plugins = plugins          # name -> PluginRecord, VALID entries only
        self._all_records: list[PluginRecord] = []   # valid + invalid, for UI listing
        self._logger = logger
        # Where user-facing notices go. `logger` is the console transcript and
        # carries everything; `notify` reaches the activity log, so only things
        # the user has to know about are sent to it. Defaults to dropping them,
        # which keeps every existing single-sink caller working unchanged.
        self._notify = notify or (lambda _msg: None)

    # -- called by main.py at LiveConnectConfig build time --
    def get_tool_declarations(self) -> list[dict]:
        decls = []
        for name, rec in self._plugins.items():
            if get_plugin_enabled(name):
                decl = {
                    "name": rec.name,
                    "description": rec.description,
                    "parameters": copy.deepcopy(rec.parameters),
                }
                if rec.behavior:
                    decl["behavior"] = rec.behavior
                decls.append(decl)
        return decls

    def has(self, name: str) -> bool:
        return name in self._plugins

    def scheduling(self, name: str) -> Optional[str]:
        """How this plugin's result should re-enter the conversation, if it said."""
        rec = self._plugins.get(name)
        return rec.scheduling if rec else None

    def timeout(self, name: str) -> float:
        rec = self._plugins.get(name)
        return rec.timeout_seconds if rec else 60.0

    # -- called by main.py from _execute_tool's else branch --
    def execute(self, name: str, parameters: dict, player=None, session_memory=None,
                cancel_event=None, action_id=None) -> ActionResult:
        rec = self._plugins.get(name)
        if rec is None or not rec.valid:
            return ActionResult.failure(
                name, f"Plugin '{name}' is not available.", status="unavailable"
            )
        if not get_plugin_enabled(name):
            return ActionResult.failure(
                name, f"The '{name}' plugin is currently disabled.", status="disabled"
            )
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            return ActionResult.failure(
                name,
                f"Invalid parameters for plugin '{name}': expected an object.",
                status="invalid_parameters",
            )
        try:
            parameters = copy.deepcopy(parameters)
            for key in ("confirmed", "confirmation", "confirmation_bypass"):
                parameters.pop(key, None)
            validate_parameters(parameters, rec.parameters)
        except (ParameterValidationError, TypeError, ValueError) as exc:
            return ActionResult.failure(
                name,
                f"Invalid parameters for plugin '{name}': {exc}",
                error=str(exc),
                status="invalid_parameters",
            )
        if cancel_event is not None and cancel_event.is_set():
            return ActionResult.failure(
                name, f"Plugin '{name}' was cancelled.", status="cancelled"
            )
        run_slots = _PLUGIN_RUN_SLOTS
        if not run_slots.acquire(blocking=False):
            return ActionResult.failure(
                name,
                "Plugin worker capacity is busy; wait for existing plugins to finish.",
                status="busy",
            )
        try:
            value = _call_run(
                rec.run, parameters, player, session_memory,
                cancel_event=cancel_event, action_id=action_id,
            )
            return ActionResult.from_handler(name, value)
        except Exception as exc:
            self._logger(f"Plugin '{name}' crashed during run ({type(exc).__name__}).")
            self._notify(f"Plugin '{name}' failed — see the console for details.")
            return ActionResult.failure(
                name,
                f"Plugin '{name}' failed ({type(exc).__name__}).",
                error=type(exc).__name__,
            )
        finally:
            run_slots.release()

    def run(self, name: str, parameters: dict, player=None, session_memory=None,
            cancel_event=None, action_id=None) -> str:
        """Compatibility text wrapper for older callers."""
        return self.execute(
            name, parameters, player, session_memory,
            cancel_event=cancel_event, action_id=action_id,
        ).as_text()

    # -- called by ui.py's settings tab to render per-plugin config forms --
    def settings_schemas(self) -> list[dict]:
        """One entry per settings SECTION, for enabled plugins that declare a
        PLUGIN_SETTINGS schema. Sections are deduped by namespace so a suite of
        plugins sharing one namespace (e.g. the printer trio) shows a single
        form. Current stored values are merged in so the UI can pre-fill fields.
        """
        seen: set[str] = set()
        out: list[dict] = []
        for name, rec in self._plugins.items():
            if not rec.settings or not get_plugin_enabled(name):
                continue
            ns = rec.settings.get("namespace") or rec.name
            if ns in seen:
                continue
            seen.add(ns)
            out.append({
                "plugin":    rec.name,
                "namespace": ns,
                "title":     rec.settings.get("title") or rec.name,
                "fields":    rec.settings.get("fields", []),
                "values":    get_plugin_config(ns),
                "action":    rec.settings.get("action"),   # optional test/connect button
            })
        return out

    # -- called by ui.py's Plugin Manager overlay --
    def list_for_ui(self) -> list[dict]:
        out = []
        for rec in self._all_records:
            out.append({
                "name": rec.name,
                "description": rec.description,
                "file": rec.file,
                "valid": rec.valid,
                "error": rec.error,
                "enabled": get_plugin_enabled(rec.name) if rec.valid else False,
            })
        return out


def _call_run(run_fn, parameters, player, session_memory, cancel_event=None, action_id=None):
    """Invoke run() passing only the kwargs it actually declares (or all of them
    if it has **kwargs), so a minimal `def run(parameters):` plugin still works."""
    sig = inspect.signature(run_fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    if has_var_kw or "player" in sig.parameters:
        kwargs["player"] = player
    if has_var_kw or "session_memory" in sig.parameters:
        kwargs["session_memory"] = session_memory
    if has_var_kw or "cancel_event" in sig.parameters:
        kwargs["cancel_event"] = cancel_event
    if has_var_kw or "action_id" in sig.parameters:
        kwargs["action_id"] = action_id
    return run_fn(parameters, **kwargs)


def _validate(module, filename: str) -> PluginRecord:
    """Returns a PluginRecord; .valid=False + .error set on any problem. Never raises."""
    plugin_meta = getattr(module, "PLUGIN", None)
    if not isinstance(plugin_meta, dict):
        return PluginRecord(name=Path(filename).stem, file=filename,
                             error="Missing PLUGIN dict constant.")

    name = plugin_meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return PluginRecord(name=str(name or Path(filename).stem), file=filename,
                             error="PLUGIN['name'] missing or not a valid identifier "
                                   "(letters/digits/underscore, must start with letter/underscore).")

    description = plugin_meta.get("description")
    if (
        not isinstance(description, str) or not description.strip()
        or len(description) > 4_000
        or any(ord(char) < 32 and char not in "\n\t" for char in description)
    ):
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['description'] must be non-empty bounded text.")

    parameters = plugin_meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['parameters'] must be a dict with \"type\": \"OBJECT\".")
    try:
        validate_parameter_schema(parameters)
    except (TypeError, ValueError) as exc:
        return PluginRecord(name=name, file=filename,
                             error=f"Invalid PLUGIN parameter schema: {exc}")

    run_fn = getattr(module, "run", None)
    if not callable(run_fn):
        return PluginRecord(name=name, file=filename,
                             error="Missing callable run(parameters, ...) function.")

    # Optional, self-describing settings schema (rendered by the settings UI).
    # A malformed schema is ignored, never fatal — the plugin still loads.
    settings = getattr(module, "PLUGIN_SETTINGS", None)
    if not (isinstance(settings, dict) and isinstance(settings.get("fields"), list)):
        settings = None

    behavior = _opt_upper(plugin_meta.get("behavior"), _BEHAVIORS)
    scheduling = _opt_upper(plugin_meta.get("scheduling"), _SCHEDULING)
    if "behavior" in plugin_meta and behavior is None:
        return PluginRecord(name=name, file=filename, error="PLUGIN['behavior'] is invalid.")
    if "scheduling" in plugin_meta and scheduling is None:
        return PluginRecord(name=name, file=filename, error="PLUGIN['scheduling'] is invalid.")
    try:
        timeout_raw = plugin_meta.get("timeout_seconds", 60.0)
        if isinstance(timeout_raw, bool):
            raise ValueError
        timeout_seconds = float(timeout_raw)
        if not math.isfinite(timeout_seconds) or not 1.0 <= timeout_seconds <= 900.0:
            raise ValueError
    except (TypeError, ValueError):
        return PluginRecord(name=name, file=filename,
                            error="PLUGIN['timeout_seconds'] must be between 1 and 900.")
    return PluginRecord(name=name, description=description.strip(), parameters=copy.deepcopy(parameters),
                         run=run_fn, file=filename, valid=True, error="", settings=settings,
                         behavior=behavior, scheduling=scheduling,
                         timeout_seconds=timeout_seconds)


def _load_error(path: Path, plugins_dir: Path, exc: Exception) -> str:
    """Turn an import failure into something the person who downloaded the file
    can act on.

    Plugins are shared one file at a time, but some of them sit on a helper —
    anything named with a leading underscore, which this loader deliberately
    skips so it is never treated as a plugin of its own. Download the plugin
    without its helper and Python reports `No module named 'plugins._x'`, which
    is accurate and tells a non-programmer nothing. Naming the missing file, and
    saying it belongs next to this one, turns a support question into a
    thirty-second fix. Nothing here is specific to any plugin: the helper's name
    comes from the exception itself.
    """
    if isinstance(exc, PermissionError):
        message = str(exc)
        safe_policy_prefixes = (
            "plugins directory is writable by other users",
            "plugin file is writable by other users",
            "plugins directory is not owned by the current user or system administrator",
            "plugin file is not owned by the current user or system administrator",
            "plugins directory must be a regular directory",
            "plugin files must be regular files",
            "plugin file exceeds the 2 MB source limit",
            "plugin source changed during validation",
            "plugin source permissions changed during validation",
        )
        if message.startswith(safe_policy_prefixes):
            return message
        return "Plugin source could not be opened safely (PermissionError)."
    if isinstance(exc, ModuleNotFoundError):
        missing = (getattr(exc, "name", "") or "").split(".")
        if (
            len(missing) == 2
            and missing[0] == "plugins"
            and re.fullmatch(r"_[A-Za-z0-9_]{0,63}", missing[1] or "")
        ):
            helper = missing[1] + ".py"
            return (f"Needs the shared file '{helper}', which is not in "
                    f"{plugins_dir.name}/. It comes with this plugin — download "
                    f"'{helper}' into the same folder as {path.name} and restart.")
        if (
            missing
            and missing[0] != "plugins"
            and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", missing[0] or "")
        ):
            return (f"Needs a package that is not installed: "
                    f"pip install {missing[0]}")
    return f"Failed to load ({type(exc).__name__}). See the console for details."


def _is_reparse_point(details) -> bool:
    return bool(int(getattr(details, "st_file_attributes", 0)) & 0x400)


_ALLOWED_TOP_LEVEL = (
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Assign,
    ast.AnnAssign,
    ast.Try,          # guarded optional imports
    ast.If,           # "if TYPE_CHECKING" and the __main__ guard
    ast.Pass,
)


def check_no_import_side_effects(source: str, filename: str) -> None:
    """Refuse a plugin that *does* something merely by being loaded.

    Discovery imports every file in the plugins directory, and validation only
    happens afterwards — so a plugin rejected for a malformed PLUGIN dict had
    already run its module body. "Rejected" read like "did not run", and it was
    not true.

    A plugin module is supposed to define things: imports, constants, functions,
    a PLUGIN dict. Anything that acts at the top level — a bare call, a loop, a
    `with` block, an assignment into someone else's namespace — is refused
    before the file is executed.

    This is not a sandbox and does not pretend to be one: `X = shutil.rmtree(...)`
    is an assignment and would pass. The real boundary is the ownership and
    permission check on the file; this closes the gap between being rejected
    and having already run.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise ValueError(f"{filename} could not be parsed: {exc.msg}") from exc

    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue                      # docstring
        if not isinstance(node, _ALLOWED_TOP_LEVEL):
            raise ValueError(
                f"{filename} runs code at import time "
                f"({type(node).__name__.lower()} at line {node.lineno}); a plugin may "
                "only define imports, constants, functions and classes"
            )
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, (ast.Name, ast.Tuple, ast.List)):
                    raise ValueError(
                        f"{filename} assigns into another object at import time "
                        f"(line {node.lineno}); a plugin may only bind its own names"
                    )


def _trusted_plugin_source(path: Path, plugins_dir: Path) -> str:
    """Read one trusted regular plugin through a bounded, no-follow descriptor."""
    directory_details = plugins_dir.lstat()
    if (
        plugins_dir.is_symlink()
        or _is_reparse_point(directory_details)
        or not stat.S_ISDIR(directory_details.st_mode)
    ):
        raise PermissionError("plugins directory must be a regular directory")
    if os.name != "nt":
        if directory_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("plugins directory is writable by other users")
        if hasattr(os, "getuid") and directory_details.st_uid not in {0, os.getuid()}:
            raise PermissionError("plugins directory is not owned by the current user or system administrator")

    details = path.lstat()
    if path.is_symlink() or _is_reparse_point(details) or not stat.S_ISREG(details.st_mode):
        raise PermissionError("plugin files must be regular files, not links")
    if details.st_size > 2_000_000:
        raise PermissionError("plugin file exceeds the 2 MB source limit")
    if os.name != "nt":
        if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError(
                "plugin file is writable by other users; use permissions 0644 or stricter"
            )
        if hasattr(os, "getuid") and details.st_uid not in {0, os.getuid()}:
            raise PermissionError("plugin file is not owned by the current user or system administrator")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or opened.st_size > 2_000_000
        ):
            raise PermissionError("plugin source changed during validation")
        if os.name != "nt" and (
            opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (hasattr(os, "getuid") and opened.st_uid not in {0, os.getuid()})
        ):
            raise PermissionError("plugin source permissions changed during validation")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(2_000_001)
    finally:
        os.close(descriptor)
    if len(content) > 2_000_000:
        raise PermissionError("plugin file exceeds the 2 MB source limit")
    return content.decode("utf-8-sig")


def discover_plugins(plugins_dir: Path, core_tool_names: set[str],
                      logger: Callable[[str], None] = print,
                      notify: Callable[[str], None] | None = None) -> PluginRegistry:
    """
    Scans plugins_dir for *.py files (skips files starting with '_', e.g. __init__.py,
    _template.py, and any shared-helper modules an author prefixes with '_').
    Import errors, validation errors, and name collisions are logged and the offending
    file is skipped — they NEVER raise out of this function and never abort the scan
    of remaining files.
    """
    plugins_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, PluginRecord] = {}
    all_records: list[PluginRecord] = []

    files = sorted(plugins_dir.glob("*.py"), key=lambda p: p.name)  # deterministic order
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            source = _trusted_plugin_source(path, plugins_dir)
            # Checked before the module is executed, not after.
            check_no_import_side_effects(source, path.name)
            module_name = f"plugins.{path.stem}"
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

            rec = _validate(module, path.name)

            if rec.valid and rec.name in core_tool_names:
                rec = PluginRecord(name=rec.name, file=path.name,
                                    error=f"Name '{rec.name}' collides with a core tool — rejected.")
            elif rec.valid and rec.name in valid:
                other = valid[rec.name].file
                rec = PluginRecord(name=rec.name, file=path.name,
                                    error=f"Name '{rec.name}' already used by plugin '{other}' — rejected.")

        except Exception as e:
            rec = PluginRecord(name=path.stem, file=path.name,
                                error=_load_error(path, plugins_dir, e))

        all_records.append(rec)
        if rec.valid:
            valid[rec.name] = rec
            logger(f"Plugin loaded: {rec.name} ({path.name})")
        else:
            logger(f"Plugin rejected: {path.name} — {rec.error}")

    notify = notify or (lambda _msg: None)
    registry = PluginRegistry(valid, logger, notify)
    registry._all_records = all_records
    rejected = len(all_records) - len(valid)
    logger(f"Plugin discovery complete: {len(valid)} active, "
           f"{rejected} rejected, {len(all_records)} total.")
    # The activity log is the user's conversation, not a boot transcript: a
    # plugin that loaded correctly is not news, so only a failure surfaces there
    # — and then as one line, because the per-plugin detail is on the console.
    if rejected:
        notify(f"{rejected} plugin(s) could not be loaded — see the console.")
    return registry
