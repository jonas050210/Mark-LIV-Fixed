"""Single synchronous entry point for running tools by name.

Three tool kinds exist — inline tools in ``main.py``, file-backed actions in
``actions/``, plugin tools in ``plugins/`` — and each had its own call path.
The multi-step agent (``actions/agent_task.py`` via ``core/planner.py``) needs
exactly one: :meth:`ToolDispatcher.run`.

The dispatcher is bound once at startup (see ``JarvisLive.__init__``) and is
deliberately dumb: it routes ``(name, params, ctx)`` to the right registry and
returns the tool's plain-text result. Verification of that result is
:mod:`core.verify`'s job, not this module's.

Threading: :meth:`run` is synchronous and may be called from executor threads
(the agent runs off the event loop). Registry ``run`` implementations are
expected to be thread-safe for independent tool calls, which holds for the
current action/plugin loaders.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


class ToolDispatcher:
    """Routes tool calls to the inline/action/plugin implementations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._action_registry = None
        self._plugin_registry = None
        self._inline_runner: Callable[[str, dict, dict], str] | None = None

    # ── wiring (called once at startup) ──────────────────────────────────────

    def bind(
        self,
        action_registry=None,
        plugin_registry=None,
        inline_runner: Callable[[str, dict, dict], str] | None = None,
    ) -> None:
        """Attach the registries. Any of them may be omitted (tests, partial
        wiring) — unbound kinds report honestly instead of raising."""
        with self._lock:
            if action_registry is not None:
                self._action_registry = action_registry
            if plugin_registry is not None:
                self._plugin_registry = plugin_registry
            if inline_runner is not None:
                self._inline_runner = inline_runner

    @property
    def is_bound(self) -> bool:
        with self._lock:
            return self._action_registry is not None or self._plugin_registry is not None

    # ── routing ──────────────────────────────────────────────────────────────

    def has(self, name: str) -> bool:
        with self._lock:
            if self._action_registry is not None:
                try:
                    if self._action_registry.has(name):
                        return True
                except Exception:
                    pass
            if self._plugin_registry is not None:
                try:
                    if self._plugin_registry.has(name):
                        return True
                except Exception:
                    pass
            return False

    def available_tools(self) -> list[str]:
        """Sorted names of every currently callable tool (inline + actions +
        enabled plugins). Used by the planner to ground its plans."""
        names: set[str] = set()
        with self._lock:
            regs = (self._action_registry, self._plugin_registry)
        for reg in regs:
            if reg is None:
                continue
            try:
                if hasattr(reg, "names"):
                    names |= set(reg.names())
                elif hasattr(reg, "list_for_ui"):
                    # PluginRegistry: only valid + enabled entries are tools.
                    for entry in reg.list_for_ui():
                        if entry.get("valid") and entry.get("enabled", True):
                            names.add(entry["name"])
                elif hasattr(reg, "get_tool_declarations"):
                    for decl in reg.get_tool_declarations():
                        names.add(decl["name"])
            except Exception:
                pass
        return sorted(names)

    def run(self, name: str, params: dict | None = None, ctx: dict | None = None) -> str:
        """Run tool `name` with `params`, return its plain-text result.

        Never raises for unknown tools or tool errors: both come back as
        text, because the caller is a verification loop that must be able to
        tell failure from crash. Only a dispatcher programming error (no
        binding at all) raises.
        """
        params = dict(params or {})
        ctx = dict(ctx or {})
        name = (name or "").strip()

        with self._lock:
            action_reg = self._action_registry
            plugin_reg = self._plugin_registry
            inline = self._inline_runner

        if inline is not None:
            try:
                result = inline(name, params, ctx)
            except Exception as e:  # noqa: BLE001 — tool errors are data
                return f"Tool '{name}' failed: {e}"
            if result is not None:
                return result

        if action_reg is not None:
            try:
                if action_reg.has(name):
                    return action_reg.run(name, params, ctx) or "Done."
            except Exception as e:  # noqa: BLE001 — tool errors are data
                return f"Tool '{name}' failed: {e}"

        if plugin_reg is not None:
            try:
                if plugin_reg.has(name):
                    return self._run_plugin(plugin_reg, name, params, ctx) or "Done."
            except Exception as e:  # noqa: BLE001 — tool errors are data
                return f"Tool '{name}' failed: {e}"

        if action_reg is None and plugin_reg is None and inline is None:
            raise RuntimeError("ToolDispatcher used before bind()")
        return f"Unknown tool '{name}'. Available: {', '.join(self.available_tools()) or 'none'}"

    @staticmethod
    def _run_plugin(plugin_reg, name: str, params: dict, ctx: dict) -> str:
        """Call a plugin registry in whichever shape it has.

        The real registry exposes ``run(name, parameters, player=None,
        session_memory=None, dispatcher=None)``; older/test doubles may expose
        ``call(name, params, ctx)``. Both are supported.
        """
        run_fn = getattr(plugin_reg, "run", None)
        if callable(run_fn):
            # New registries take `dispatcher`; older ones do not. Decided by
            # signature, not by try/except - a TypeError from INSIDE the tool
            # must never trigger a second run of it.
            try:
                import inspect as _inspect

                _sig = _inspect.signature(run_fn)
                _pars = _sig.parameters
                _takes_disp = ("dispatcher" in _pars or any(
                    pr.kind == _inspect.Parameter.VAR_KEYWORD
                    for pr in _pars.values()))
            except (TypeError, ValueError):
                _takes_disp = False
            if _takes_disp:
                return run_fn(
                    name,
                    params,
                    ctx.get("player"),
                    ctx.get("session_memory"),
                    dispatcher=ctx.get("dispatcher"),
                )
            return run_fn(name, params, ctx.get("player"),
                          ctx.get("session_memory"))
        call_fn = getattr(plugin_reg, "call", None)
        if callable(call_fn):
            return call_fn(name, params, ctx)
        raise RuntimeError("Plugin registry has neither run() nor call()")


_dispatcher = ToolDispatcher()


def get_dispatcher() -> ToolDispatcher:
    """Process-wide dispatcher singleton."""
    return _dispatcher
