"""
Local application inventory — "what apps do I have?", "do I have Spotify?",
"where is Chrome installed?", "what games are installed?".

All answers come from core/app_finder.py's on-disk sources (Start Menu, App
Paths, Uninstall registry, Steam/Epic manifests, PATH), so this action and
open_app can never disagree. Listings show names only; install paths are
revealed only by the explicit `where` action — never volunteered in bulk.
"""
from __future__ import annotations

try:
    from core import app_finder as _af
except ImportError:  # pragma: no cover — defensive; loader skips us instead
    _af = None

_DEFAULT_LIMIT = 40
_MAX_LIMIT = 100


def _clamp_limit(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, n))


def _fmt_names(items: list[dict], limit: int) -> str:
    names = [d["name"] for d in items]
    if len(names) > limit:
        return ", ".join(names[:limit]) + f", and {len(names) - limit} more"
    return ", ".join(names)


def app_inventory(parameters=None, response=None, player=None,
                  session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list") or "list").strip().lower()
    query = str(params.get("query", "") or "").strip()
    limit = _clamp_limit(params.get("limit", _DEFAULT_LIMIT))

    if _af is None:
        return "Application inventory is unavailable (app_finder failed to load)."

    try:
        if action in ("find", "have", "check", "exists"):
            if not query:
                return "Tell me which app to look for."
            tgt = _af.resolve(query)
            if tgt is None:
                sugg = _af.suggest(query)
                msg = f"No — I could not find {query} installed."
                if sugg:
                    msg += f" Closest matches: {', '.join(sugg)}."
                return msg
            return f"Yes — {tgt.display} is installed ({tgt.source})."

        if action in ("where", "path", "location"):
            if not query:
                return "Tell me which app to locate."
            info = _af.where(query)
            if info is None:
                sugg = _af.suggest(query)
                msg = f"I could not locate {query}."
                if sugg:
                    msg += f" Did you mean: {', '.join(sugg)}?"
                return msg
            path = info.get("target") or info.get("path") or ""
            if path:
                return f"{info['name']} ({info['source']}): {path}"
            return f"{info['name']} is installed ({info['source']})."

        if action in ("games", "game"):
            games = _af.list_apps(game_only=True)
            if not games:
                return "I could not find any installed games."
            noun = "game" if len(games) == 1 else "games"
            return (f"Installed {noun} ({len(games)}): " + _fmt_names(games, limit) + ".")

        if action in ("status", "running", "is_running", "state"):
            # Installed is not running: the controller checks the process
            # table, so "is Spotify running" gets a true answer, not a guess.
            if not query:
                return "Tell me which app to check."
            try:
                from core import app_controller as _ac
            except Exception:
                return "Running-state checks are unavailable (app_controller failed to load)."
            try:
                st = _ac.status(query)
            except Exception as e:
                return f"Status check failed ({type(e).__name__})."
            msg = st.short()
            if st.window_titles:
                msg += f" Windows: {'; '.join(st.window_titles[:3])}."
            if st.detail and not st.installed:
                sugg = _af.suggest(query)
                if sugg:
                    msg += f" Did you mean: {', '.join(sugg)}?"
            return msg

        if action in ("rescan", "refresh", "rebuild"):
            try:
                n = _af.rescan_shortcuts()
            except Exception as e:
                return f"Rescan failed: {type(e).__name__}."
            total = len(_af.list_apps())
            return f"Application index rebuilt: {n} shortcuts, {total} known apps."

        # default: list
        items = _af.list_apps()
        if not items:
            return "I could not find any installed applications."
        noun = "application" if len(items) == 1 else "applications"
        return (f"You have {len(items)} known {noun}: "
                + _fmt_names(items, limit) + ".")
    except Exception as e:
        print(f"[app_inventory] failed: {e}")
        return f"Application lookup failed ({type(e).__name__})."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "app_inventory",
    "description": (
        "Inspects the locally installed applications. Use to answer 'what apps "
        "do I have', 'do I have Spotify', 'what games are installed', "
        "'where is Chrome installed', and 'is Spotify running' (action=status "
        "checks the process table - installed is not the same as running). "
        "Never downloads or installs anything."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "list | find | where | games | status | rescan (default: list)"
            },
            "query": {
                "type": "STRING",
                "description": "App name for find/where (aliases work: 'GD', 'Spotify')"
            },
            "limit": {
                "type": "INTEGER",
                "description": "Max names to list (default 40, max 100)"
            }
        },
        "required": []
    },
    "handler": app_inventory,
}
