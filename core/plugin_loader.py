"""
Plugin discovery, validation, collision detection, and dispatch.

Discovery runs once (JarvisLive.__init__ calls discover_plugins()); the resulting
PluginRegistry is cached for the process lifetime. Enable/disable state is re-read
from config on every call to get_tool_declarations() / run() / list_for_ui(), so
toggling a plugin does not require restarting the app or re-importing anything.

Two things keep plugins off the critical startup path:

- **Lazy bodies.** A plain plugin — literal ``PLUGIN`` dict, nothing executable at
  module level — is *read*, not imported: its metadata comes from the source via
  :mod:`ast`, and the module body is imported the first time it is really used (a
  tool call, a launch hook, a settings form). Startup no longer pays for code
  nobody asked for, and a plugin whose import fails does so at that moment, with
  the same diagnosed message, instead of taking the boot with it. Anything with a
  computed ``PLUGIN`` dict or top-level side effects is still imported eagerly,
  because its metadata cannot be trusted without running it.
- **Parked plugins.** :data:`PARKED_PLUGINS` names plugins that are deliberately
  not part of a live session yet: still discovered, still validated, still listed
  in the plugin manager — but not offered as a tool, not started, and refused by
  name. Parking is one dictionary entry, never a deletion.
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import re
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

#: Plugins the app deliberately does not run right now, with the reason that
#: goes into the log. Discovery, validation and the plugin manager ignore this
#: dict on purpose — the plugin stays first-class and loadable; only the live
#: session leaves it out.
PARKED_PLUGINS: dict[str, str] = {
}


def parked_reason(name: str) -> str:
    """The reason `name` is parked, or '' when it is not."""
    return PARKED_PLUGINS.get(str(name or "").strip().lower(), "")


def filter_parked(declarations: list[dict],
                  log: Callable[[str], None] | None = None) -> list[dict]:
    """Drop parked plugins from a tool-declaration list (one log line for all)."""
    out: list[dict] = []
    skipped: list[str] = []
    for decl in declarations or []:
        if parked_reason(str((decl or {}).get("name", ""))):
            skipped.append(str(decl.get("name")))
            continue
        out.append(decl)
    if skipped and log is not None:
        log("Parked plugins not offered as tools: " + ", ".join(sorted(skipped)))
    return out

from memory.config_manager import get_plugin_enabled, get_plugin_config

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}

# Optional, and the same contract actions use: a plugin that takes a moment can
# say so, and the model carries on talking instead of waiting on it. See
# core/action_loader.py for what each value means.
_BEHAVIORS = ("BLOCKING", "NON_BLOCKING")
_SCHEDULING = ("WHEN_IDLE", "SILENT", "INTERRUPT")


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
    on_launch: Optional[Callable] = None  # optional startup hook, called after UI/session are ready
    behavior: Optional[str] = None    # None = the API's default (blocking)
    scheduling: Optional[str] = None  # None = the API's default (WHEN_IDLE)
    # Set while the body has not been imported yet. `load_lock` serialises the
    # first import when two threads ask at once: the second waits, it does not
    # import twice.
    lazy_path: str = ""
    lazy_error: str = ""              # set when the deferred import failed
    lazy_on_launch: bool = False      # the source defines on_launch (see above)
    load_lock: threading.Lock = field(default_factory=threading.Lock,
                                      repr=False, compare=False)

    @property
    def deferred(self) -> bool:
        """True while this record is metadata only — the body is not imported."""
        return bool(self.lazy_path) and self.run is None


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
        _disabled_valid = []
        for name, rec in self._plugins.items():
            try:
                _enabled = bool(get_plugin_enabled(name))
            except Exception:
                # The enable flag is user config: a read error must never cost
                # a valid plugin its tool - default to the opt-out model's on.
                _enabled = True
            if _enabled:
                decl = {
                    "name": rec.name,
                    "description": rec.description,
                    "parameters": rec.parameters,
                }
                if rec.behavior:
                    decl["behavior"] = rec.behavior
                decls.append(decl)
            elif rec.valid:
                _disabled_valid.append(name)
        if _disabled_valid:
            # An empty tool list with valid plugins is ALWAYS this branch: say
            # so by name, so the next tools=[] needs no detective work.
            self._logger("Valid plugins disabled by user config "
                         "(not offered as tools): "
                         + ", ".join(sorted(_disabled_valid)))
        return decls

    def has(self, name: str) -> bool:
        return name in self._plugins

    def names(self) -> set[str]:
        """Callable plugin tools right now: valid, enabled, and not parked.

        The dispatcher/planner use this to ground plans, so a parked plugin is
        not proposed to the model in the first place.
        """
        out: set[str] = set()
        for name in self._plugins:
            if parked_reason(name):
                continue
            try:
                if get_plugin_enabled(name):
                    out.add(name)
            except Exception:
                out.add(name)
        return out

    def _materialize(self, rec: PluginRecord) -> PluginRecord:
        """Import a deferred plugin body once and adopt what it really declares.

        The AST snapshot decided *whether* to offer the plugin; this decides
        what it can actually do. A failure here is reported like a discovery
        failure — same diagnosed wording — and the plugin stops being valid, so
        nothing pretends it is callable.
        """
        if not rec.deferred:
            return rec
        with rec.load_lock:
            if not rec.deferred:
                return rec
            path = Path(rec.lazy_path)
            try:
                module = _import_module(path)
                loaded = _validate(module, path.name)
            except Exception as e:
                # Clearing lazy_path is what ends "deferred" (see the property):
                # nothing retries an import that has already failed once, not a
                # launch hook and not the next call.
                rec.lazy_path = ""
                rec.lazy_error = _load_error(path, path.parent, e)
                rec.valid = False
                rec.error = rec.lazy_error
                self._logger(f"Plugin '{rec.name}' failed to import on first "
                             f"use: {rec.lazy_error}")
                self._notify(f"Plugin '{rec.name}' could not be loaded — see the console.")
                traceback.print_exc()
                return rec
            if not loaded.valid:
                rec.lazy_path = ""
                rec.lazy_error = loaded.error
                rec.valid = False
                rec.error = loaded.error
                self._logger(f"Plugin '{rec.name}' is not usable: {loaded.error}")
                return rec
            rec.description = loaded.description
            rec.parameters = loaded.parameters
            rec.run = loaded.run
            rec.on_launch = loaded.on_launch
            rec.settings = loaded.settings or rec.settings
            rec.behavior = loaded.behavior
            rec.scheduling = loaded.scheduling
            rec.lazy_path = ""
            return rec

    def scheduling(self, name: str) -> Optional[str]:
        """How this plugin's result should re-enter the conversation, if it said."""
        rec = self._plugins.get(name)
        return rec.scheduling if rec else None

    # -- called by main.py from _execute_tool's else branch --
    def run(self, name: str, parameters: dict, player=None, session_memory=None,
            dispatcher=None) -> str:
        reason = parked_reason(name)
        if reason:
            # Parked is not disabled: the file is fine, the app chooses not to
            # run it. The refusal says exactly that.
            return f"The '{name}' plugin is {reason}. Nothing was run."
        rec = self._plugins.get(name)
        if rec is None or not rec.valid:
            return f"Plugin '{name}' is not available."
        if not get_plugin_enabled(name):
            return f"The '{name}' plugin is currently disabled."
        if rec.deferred:
            rec = self._materialize(rec)
            if not rec.valid:
                return f"Plugin '{name}' is not available ({rec.error})."
        try:
            return _call_run(rec.run, parameters, player, session_memory,
                             dispatcher) or "Done."
        except Exception as e:
            self._logger(f"Plugin '{name}' crashed during run(): {e}")
            self._notify(f"Plugin '{name}' failed — see the console for details.")
            traceback.print_exc()
            return f"Sir, the '{name}' plugin failed: {e}"

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

    # -- called by main.py once the UI facade and a Live session are ready --
    def launch_enabled(self, player=None) -> None:
        """Run optional plugin startup hooks without making startup depend on them.

        Tool calls have always received `player`, but discovery intentionally has
        no UI object. A plugin that wants to resume a user-enabled background
        service at launch (for example a remote bridge) can define
        `on_launch(player)`. Only enabled, valid plugins are called, each on its
        own daemon thread, and failures are logged instead of escaping into the
        Live session.
        """
        for name, rec in self._plugins.items():
            reason = parked_reason(name)
            if reason:
                self._logger(f"Plugin '{name}' is parked ({reason}) — its launch "
                             f"hook was not called.")
                continue
            if not get_plugin_enabled(name):
                continue
            if rec.deferred and rec.lazy_on_launch:
                rec = self._materialize(rec)
            hook = rec.on_launch
            if not hook:
                continue

            def _run_hook(rec=rec, hook=hook):
                try:
                    _call_on_launch(hook, player)
                except Exception as e:
                    self._logger(f"Plugin '{rec.name}' crashed during on_launch(): {e}")
                    self._notify(f"Plugin '{rec.name}' launch hook failed — see the console.")
                    traceback.print_exc()

            threading.Thread(target=_run_hook, daemon=True,
                             name=f"plugin-{name}-launch").start()

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


def _call_on_launch(hook_fn, player):
    """Invoke on_launch(), tolerating hooks that declare no parameters.

    The documented shape is on_launch(player), but keeping the dispatcher as
    forgiving as run() preserves the drop-in nature of plugins and makes tests
    easy to write.
    """
    sig = inspect.signature(hook_fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if has_var_kw or "player" in sig.parameters:
        return hook_fn(player=player)
    if len(sig.parameters) >= 1:
        return hook_fn(player)
    return hook_fn()


def _call_run(run_fn, parameters, player, session_memory, dispatcher=None):
    """Invoke run() passing only the kwargs it actually declares (or all of them
    if it has **kwargs), so a minimal `def run(parameters):` plugin still works."""
    sig = inspect.signature(run_fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    if has_var_kw or "player" in sig.parameters:
        kwargs["player"] = player
    if has_var_kw or "session_memory" in sig.parameters:
        kwargs["session_memory"] = session_memory
    if dispatcher is not None and (has_var_kw or "dispatcher" in sig.parameters):
        kwargs["dispatcher"] = dispatcher
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
    if not isinstance(description, str) or not description.strip():
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['description'] missing or empty.")

    parameters = plugin_meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['parameters'] must be a dict with \"type\": \"OBJECT\".")

    run_fn = getattr(module, "run", None)
    if not callable(run_fn):
        return PluginRecord(name=name, file=filename,
                             error="Missing callable run(parameters, ...) function.")

    on_launch = getattr(module, "on_launch", None)
    if on_launch is not None and not callable(on_launch):
        on_launch = None

    # Optional, self-describing settings schema (rendered by the settings UI).
    # A malformed schema is ignored, never fatal — the plugin still loads.
    settings = getattr(module, "PLUGIN_SETTINGS", None)
    if not (isinstance(settings, dict) and isinstance(settings.get("fields"), list)):
        settings = None

    return PluginRecord(name=name, description=description.strip(), parameters=parameters,
                         run=run_fn, file=filename, valid=True, error="", settings=settings,
                         on_launch=on_launch, behavior=_opt_upper(plugin_meta.get("behavior"), _BEHAVIORS),
                         scheduling=_opt_upper(plugin_meta.get("scheduling"), _SCHEDULING))


# ── lazy discovery: metadata without importing the body ──────────────────────
# A plugin written the documented way — literal PLUGIN dict, imports and defs at
# module level, nothing else — can be understood by reading it. This is the fast
# path: startup parses a small file instead of executing it, and the module is
# imported on first use. Anything else (computed metadata, top-level code, a
# syntax error) takes the eager path, where importing is the only way to know.

#: Statement kinds that make a module body *do* something when it is executed:
#: branches, loops, guarded imports, assertions and raises. A plugin containing
#: any of them at the top level is imported during discovery, because the only
#: way to know how it behaves at import time is to import it — and a plugin that
#: raises at import must be reported by discovery, not on the first call.
_EAGER_STATEMENTS = (ast.Raise, ast.Assert, ast.Try, ast.If, ast.For,
                     ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
                     ast.Delete, ast.Match)


def _defers_safely(tree: ast.Module) -> bool:
    """True when this body can be understood without running it.

    Imports, definitions, classes, assignments and expressions are all safe to
    defer: reading them says everything the loader needs, and if one of them
    fails when the body is finally imported, that failure lands on the call that
    needed the plugin instead of on the boot.
    """
    for node in tree.body:
        if isinstance(node, _EAGER_STATEMENTS):
            return False
    return True


def _top_level_import_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".", 1)[0])
    roots.discard("__future__")
    return roots


def _literal_assignment(tree: ast.Module, name: str):
    """The literal value assigned to `name` at module level, or None."""
    for node in tree.body:
        target_names = []
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
        if name in target_names and node.value is not None:
            try:
                return ast.literal_eval(node.value)
            except Exception:
                return None
    return None


def _missing_dependency(roots: set[str]) -> str:
    """First top-level import that is not importable, as a pip-install hint."""
    for root in sorted(roots):
        if root in ("plugins", "core", "config", "memory", "actions", "ui"):
            continue
        try:
            if importlib.util.find_spec(root) is None:
                return root
        except Exception:
            continue
    return ""


def _lazy_record(path: Path, plugins_dir: Path) -> Optional[PluginRecord]:
    """A metadata-only PluginRecord, or None when the file must be imported.

    None is not an error: it means "this file cannot be understood without
    running it", and the caller falls back to the eager path.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except Exception:
        return None
    if not _defers_safely(tree):
        return None
    meta = _literal_assignment(tree, "PLUGIN")
    if not isinstance(meta, dict):
        return None
    funcs = {node.name for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if "run" not in funcs:
        return None                      # let the importer say why, precisely
    missing = _missing_dependency(_top_level_import_roots(tree))
    if missing:
        return PluginRecord(
            name=str(meta.get("name") or path.stem), file=path.name,
            error=f"Needs a package that is not installed: pip install {missing}")

    name = meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return None                      # the eager path reports this properly
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    parameters = meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return None
    settings = _literal_assignment(tree, "PLUGIN_SETTINGS")
    if not (isinstance(settings, dict) and isinstance(settings.get("fields"), list)):
        settings = None
    return PluginRecord(
        name=name, description=description.strip(), parameters=parameters,
        run=None, file=path.name, valid=True, error="",
        settings=settings,
        on_launch=None, lazy_on_launch=("on_launch" in funcs),
        behavior=_opt_upper(meta.get("behavior"), _BEHAVIORS),
        scheduling=_opt_upper(meta.get("scheduling"), _SCHEDULING),
        lazy_path=str(path),
    )


def _import_module(path: Path):
    """Import a plugin file under its ``plugins.<stem>`` name (never raises)."""
    module_name = f"plugins.{path.stem}"
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
    return module


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
    if isinstance(exc, ModuleNotFoundError):
        missing = (getattr(exc, "name", "") or "").split(".")
        if len(missing) == 2 and missing[0] == "plugins" and missing[1].startswith("_"):
            helper = missing[1] + ".py"
            return (f"Needs the shared file '{helper}', which is not in "
                    f"{plugins_dir.name}/. It comes with this plugin — download "
                    f"'{helper}' into the same folder as {path.name} and restart.")
        if missing and missing[0] not in ("plugins",):
            return (f"Needs a package that is not installed: "
                    f"pip install {missing[0]}")
    return f"Failed to load: {exc}"


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
            # Cheap path first: a plugin whose body only defines things is
            # described from its source and imported later, on first use. None
            # means "read the real thing" — the body runs while a plugin that
            # misbehaves at import is still a discovery-time problem, not a
            # surprise on the user's first click.
            rec = _lazy_record(path, plugins_dir)
            if rec is None:
                module = _import_module(path)
                rec = _validate(module, path.name)
        except Exception as e:
            rec = PluginRecord(name=path.stem, file=path.name,
                                error=_load_error(path, plugins_dir, e))
            traceback.print_exc()

        # One collision rule for both paths, applied to whatever was just
        # learned about the plugin.
        if rec.valid:
            if rec.name in core_tool_names:
                rec = PluginRecord(name=rec.name, file=path.name,
                                    error=f"Name '{rec.name}' collides with a core tool — rejected.")
            elif rec.name in valid:
                other = valid[rec.name].file
                rec = PluginRecord(name=rec.name, file=path.name,
                                    error=f"Name '{rec.name}' already used by plugin '{other}' — rejected.")

        all_records.append(rec)
        if rec.valid:
            valid[rec.name] = rec
            logger(f"Plugin loaded{' lazily (body deferred)' if rec.deferred else ''}: "
                   f"{rec.name} ({path.name})")
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
