"""
Uninstall an app through its own, vendor-registered uninstaller.

The rules this action follows, in order:

1. Resolution goes through the same app finder ``open_app`` uses
   (``core/app_finder.find_uninstaller``), so the app being removed is the app
   the user can see in their inventory — and portable folders with no
   uninstaller are refused honestly instead of having their directory deleted.
2. Nothing runs until the human presses CONFIRM on the HUD
   (``core/install_safety.guard``). The banner shows the exact program that
   will run, because the model never sees a "remove" it can talk its way past.
3. System components (Windows itself, drivers, runtimes) are refused outright —
   there is no confirmation that makes those safe.
4. Completion is the resolver's answer, not the uninstaller's exit code:
   "removed", "still running on your screen" and "did not work" are three
   different sentences, and the tool says the true one.
"""
from __future__ import annotations

from core import install_safety, tasks


def uninstall_app(parameters=None, response=None, player=None,
                  session_memory=None, speak=None) -> str:
    params = parameters if isinstance(parameters, dict) else {}
    app_name = str(params.get("app_name") or params.get("name") or "").strip()
    if not app_name:
        return "Which app should I uninstall?"

    if player:
        try:
            player.write_log(f"[uninstall_app] {app_name}")
        except Exception:
            pass
    print(f"[uninstall_app] requested: '{app_name}'")

    try:
        from core import app_controller as _ac
    except Exception as e:
        print(f"[uninstall_app] controller unavailable: {e}")
        return "Uninstalling is unavailable (app_controller failed to load)."

    plan, refusal = _ac.uninstall_plan(app_name)
    if plan is None:
        # Includes protected targets and the honest "no reliable uninstaller".
        return refusal or f"I could not find a way to uninstall '{app_name}'."

    source = str(getattr(plan, "source", "") or "uninstaller")
    exe = str(getattr(plan, "exe", "") or "")
    display = str(getattr(plan, "name", "") or app_name)
    detail = f"Runs {plan.command_line() if hasattr(plan, 'command_line') else exe}"
    if source:
        detail += f" ({source})."
    vendor_note = str(getattr(plan, "detail", "") or "")
    if vendor_note:
        detail += f" {vendor_note}."

    request = install_safety.InstallRequest(
        kind=install_safety.KIND_UNINSTALL,
        target=display,
        detail=detail,
        source="user",
        origin="uninstall_app",
    )
    outcome: dict = {}

    def _run() -> str:
        task = tasks.start(tasks.KIND_UNINSTALL, f"Uninstall {display}",
                           detail=f"running {exe or source}")
        by_name = getattr(plan, "name", "") or app_name
        status, message = _ac.run_uninstall(plan, name=by_name, task=task)
        outcome["status"] = status
        outcome["message"] = message
        if status == "removed":
            # Only the resolver's own answer closes this task as done.
            verified, why = install_safety.verify(
                lambda: _gone(by_name, app_name), timeout=10.0, interval=1.0)
            if not verified:
                task.fail(error="removal could not be verified", detail=why)
                return install_safety.unverified(
                    "uninstall", display, "The uninstaller said it was finished")
            task.finish(detail="app no longer found by the resolver")
            return f"{message} (verified: it is gone from the app list.)"
        if status == "running":
            # Honest middle: it may well finish, but nothing is claimed yet.
            task.finish(detail="uninstaller still on screen — not confirmed")
            return (f"{message} The removal is not confirmed yet, so I am not "
                    f"claiming it is uninstalled.")
        task.fail(error=message[:160], detail=f"{exe or source} did not remove it")
        return message

    def _announce(message: str) -> str:
        """Speak the outcome after the button press, then hand it back."""
        if speak and message:
            try:
                speak(message)
            except Exception:
                pass
        return message

    return install_safety.guard(
        request, run=lambda: _announce(_run()), key="uninstall_app")


def _gone(*names: str) -> bool:
    """True when the resolver no longer knows any of `names`."""
    try:
        from core import app_finder as _af
    except Exception:
        return False
    for name in names:
        if not name:
            continue
        try:
            if _af.resolve(str(name)) is not None:
                return False
        except Exception:
            return False
    return True


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "uninstall_app",
    "description": (
        "Uninstalls an application by running its own uninstaller, after the "
        "user confirms on screen. Use for 'uninstall Steam', 'deinstalliere "
        "Discord', 'remove WhatsApp from my PC'. It never deletes folders by "
        "hand, refuses system components (Windows, drivers, runtimes), and says "
        "so honestly when an app has no reliable uninstaller or when the "
        "removal could not be verified. Never claim an app is uninstalled "
        "unless this tool's own result says it was verified."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Natural name of the app to uninstall ('Discord', 'Steam', 'WhatsApp')"
            }
        },
        "required": ["app_name"]
    },
    "handler": uninstall_app,
}
