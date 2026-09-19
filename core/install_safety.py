"""
core/install_safety.py — the one place that decides how an install, update,
download or uninstall may happen.

Why this module exists
──────────────────────
The rule "JARVIS never installs anything" used to live inside
:mod:`core.app_controller` and was enforced only on the *open* path. Everything
else that touched installers had grown its own idea of safety: the game updater
fired ``steam://install/…`` straight at Steam, the dependency installer pip'd
packages and printed a tick, and an uninstall was not possible at all. A rule
that each caller re-implements is not a rule — it is a habit. This module holds
it once, and every install/download path is required to go through it:

- :func:`classify_intent` / :func:`is_installer_target` — the refusal patterns
  (moved here from ``app_controller``, which now imports them, so both the open
  path and the launcher path can never disagree).
- :func:`protected_target` — components that must never be installed *or*
  removed by an assistant, whatever the user says ("Windows", drivers,
  runtimes). An uninstall is irreversible enough that "are you sure" is not
  sufficient for the machine's own plumbing.
- :func:`requires_confirmation` / :func:`guard` — an install, update, download
  or uninstall is parked behind the on-screen confirmation gate
  (:mod:`core.confirm`). The *model* can never satisfy that gate, and with no
  interface bound the request is refused fail-closed rather than performed.
  The app's own dependency bootstrap is exempt and says why.
- :func:`verify` / :func:`verify_import` / :func:`unverified` — completion is a
  measurement, not a sentence. Every caller that claims "installed", "updated"
  or "downloaded" must first observe the world agree; when it cannot, it says
  so in exactly these words.

Nothing in here performs an install itself: it decides, gates and verifies, so
the calling action keeps owning the platform-specific mechanics.
"""

from __future__ import annotations

import importlib.util
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# ── kinds ────────────────────────────────────────────────────────────────────

KIND_INSTALL = "install"
KIND_UPDATE = "update"
KIND_DOWNLOAD = "download"
KIND_UNINSTALL = "uninstall"

#: Sources that may skip the confirmation gate. Only the application's own
#: dependency bootstrap qualifies: the package list is code in
#: ``core/installer.py`` (never model output), it installs into the running
#: interpreter, and it cannot be aimed at anything.
SELF_SOURCE = "bootstrap"


# ── refusal patterns (single source; app_controller re-exports them) ─────────

#: Verbs that turn a request into an install/repair/update job (EN + DE).
INSTALL_INTENT_RE = re.compile(
    r"\b(install|installation|installer|setup|update|upgrade|repair|reinstall|"
    r"download|uninstall|remove-app|deinstallier\w*|installier\w*|"
    r"aktualisier\w*|updat\w*|reparier\w*|herunterlad\w*|download\w*|"
    r"einricht\w*|besorg\w*|hol\s+dir)\b",
    re.IGNORECASE,
)

#: File-name fragments that mark a *target* as an installer/updater rather than
#: the app itself. Last line of defence: even a clean request refuses to launch
#: or execute these directly.
INSTALLER_TARGET_RE = re.compile(
    r"(setup|install|update|upgrade|patch|uninstall|unins\d*|repair|redist|"
    r"vcredist|dxsetup|oobe|installer)",
    re.IGNORECASE,
)

#: Names that are *always* the operating system, its runtimes, drivers or
#: servicing stack — never installed by the assistant, and, more importantly,
#: never uninstalled by it: there is no undo for that. Lookarounds instead of
#: \b because two alternatives end in punctuation ("C++", ".net"), where a word
#: boundary can never match.
_PROTECTED_STRONG_RE = re.compile(
    r"(?<![a-z0-9])("
    r"windows|win32|microsoft\s+visual\s+c\+\+|visual\s+c\+\+\s+redistributable|"
    r"\.net|dotnet|directx|vcredist|"
    r"treiber|chipset|bios|firmware|kernel|bootloader|"
    r"nvidia|geforce|amd\s+(chipset|graphics)|radeon|intel\s+(graphics|chipset)|"
    r"realtek|"
    r"servicing\s+stack|security\s+update|update\s+for|windows\s+update"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)

#: Words that mean "system plumbing" only in context: "System Shock" is a game
#: and "Driver: San Francisco" is one too, while "System", "operating system"
#: and "Driver Booster" are not. These refuse when the name is nothing but
#: those words, or when an OS-ish word stands next to them.
_PROTECTED_WEAK_RE = re.compile(r"(?<![a-z0-9])(system|driver)(?![a-z0-9])",
                                re.IGNORECASE)
_OS_CONTEXT_RE = re.compile(
    r"(?<![a-z0-9])("
    r"operating|windows|microsoft|32|64|update|updater|component|components|"
    r"framework|runtime|service|pack|file|files|folder|folders|registry|"
    r"restore|repair|cleaner|tweak|booster|manager|optimizer|optimiser|"
    r"utility|tool|tools|software|hardware|audio|video|network|graphics|"
    r"printer|motherboard|card|chip|controller|agent"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)

#: Filler words that do not make "my system" a different thing from "system".
_PROTECTED_STOPWORDS = frozenset({
    "my", "the", "this", "that", "a", "an", "our", "your", "of", "for", "and",
    "by", "from", "on", "in", "to", "please", "pc", "computer", "app",
    "application", "program", "software", "component", "components", "it",
})


def _protected_reason(token: str) -> str:
    return (f"'{token}' is part of the operating system or its drivers/runtimes. "
            f"I will not install or remove system components — that needs "
            f"Windows' own tools and a person who knows why.")


def protected_target(name: str) -> Optional[str]:
    """Reason string when `name` is a protected system component, else None."""
    text = str(name or "").strip()
    if not text:
        return None
    strong = _PROTECTED_STRONG_RE.search(text)
    if strong:
        return _protected_reason(strong.group(1))

    weak = _PROTECTED_WEAK_RE.search(text)
    if weak:
        rest = _PROTECTED_WEAK_RE.sub(" ", text)
        words = [w for w in re.split(r"[^a-z0-9+]+", rest.lower()) if w]
        meaningful = [w for w in words if w not in _PROTECTED_STOPWORDS]
        if not meaningful or _OS_CONTEXT_RE.search(rest):
            return _protected_reason(weak.group(1))
    return None


def classify_intent(text: str) -> Optional[str]:
    """Which install-family verb (if any) a request uses. ``None`` = ordinary."""
    match = INSTALL_INTENT_RE.search(str(text or ""))
    if not match:
        return None
    word = match.group(1).lower()
    if word.startswith(("uninstall", "deinstallier", "remove-app")):
        return KIND_UNINSTALL
    if word.startswith(("update", "upgrade", "aktualisier", "updat")):
        return KIND_UPDATE
    if word.startswith(("download", "herunterlad")):
        return KIND_DOWNLOAD
    return KIND_INSTALL


def is_installer_target(path: str) -> bool:
    """True when ``path`` smells like an installer/updater, not the app itself."""
    try:
        base = Path(str(path or "")).name
    except Exception:
        base = str(path or "")
    return bool(base and INSTALLER_TARGET_RE.search(base))


# ── the request ──────────────────────────────────────────────────────────────


@dataclass
class InstallRequest:
    """One install-family operation, described by whoever wants it done."""

    kind: str
    target: str
    detail: str = ""
    source: str = "user"        # "user" | "model" | "bootstrap"
    origin: str = ""            # free text: which tool asked ("game_updater", …)

    def title(self) -> str:
        verb = {
            KIND_INSTALL: "Install",
            KIND_UPDATE: "Update",
            KIND_DOWNLOAD: "Download",
            KIND_UNINSTALL: "Uninstall",
        }.get(self.kind, "Change")
        return f"{verb} {self.target}".strip()

    def summary(self) -> str:
        base = f"{self.title()} ({self.origin})" if self.origin else self.title()
        return f"{base} — {self.detail}" if self.detail else base


def requires_confirmation(request: InstallRequest) -> bool:
    """Every install-family change needs the on-screen gate, except bootstrap.

    The user asking out loud is not enough on its own: the model composes the
    arguments, so only a button the human presses can distinguish "the user
    asked for this" from "the model decided this".
    """
    return str(request.source or "").lower() != SELF_SOURCE


def refusal_for(request: InstallRequest) -> Optional[str]:
    """Blocked before the gate even opens (system components, missing target)."""
    if not str(request.target or "").strip():
        return f"{request.kind.capitalize()} asked for, but no target was named."
    if request.kind in (KIND_INSTALL, KIND_UPDATE, KIND_DOWNLOAD, KIND_UNINSTALL):
        bad = protected_target(request.target)
        if bad:
            return bad
    return None


def guard(request: InstallRequest,
          run: Callable[[], str],
          *,
          key: str = "") -> str:
    """Run an install-family operation only after the human presses CONFIRM.

    Returns the sentence the tool hands back to the model. With the interface
    unbound, or a protected target, nothing runs and the reason is returned —
    fail-closed, exactly like the shutdown/restart gate. Because the work starts
    after the button press, `run` is where a tool speaks its own outcome (see
    actions/uninstall_app.py); the return value here is for the model.
    """
    blocked = refusal_for(request)
    if blocked:
        return f"[REFUSED] {blocked} Nothing was {request.kind}ed."

    if not requires_confirmation(request):
        try:
            return run()
        except Exception as e:  # noqa: BLE001 — caller-facing text, never a crash
            return f"{request.title()} failed: {type(e).__name__}: {e}"

    from core import confirm

    if confirm.pending_title():
        return ("[REFUSED] There is already a confirmation waiting on screen. "
                f"Answer that one first — nothing was {request.kind}ed.")

    note = request.detail or "This downloads or changes files on this machine."
    return confirm.request(
        key=key or f"{request.kind}:{request.target}"[:40],
        title=request.title(),
        detail=note,
        run=run,
    )


# ── verification: completion has to be observed ─────────────────────────────

#: The sentence every caller must be able to say instead of claiming success.
def unverified(kind: str, target: str, what: str = "") -> str:
    """Honest wording for "I started it but could not confirm it"."""
    done = what or f"{kind} started"
    return (f"{done} for '{target}', but I could not verify it completed — "
            f"check it yourself before relying on it.")


def verify(probe: Callable[[], bool],
           *,
           timeout: float = 8.0,
           interval: float = 0.5) -> tuple[bool, str]:
    """Poll ``probe`` until it reports True or the timeout expires.

    Returns ``(verified, detail)``. ``probe`` is any ground-truth check — does
    the file exist now, does the package import, did the manifest change — and
    an exception from it counts as "not yet", because a probe that cannot run
    must never be mistaken for success.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    attempts = 0
    last_error = ""
    while True:
        attempts += 1
        try:
            if probe():
                return True, f"verified after {attempts} check(s)"
        except Exception as e:  # noqa: BLE001 — see docstring
            last_error = f"{type(e).__name__}: {e}"
        if time.monotonic() >= deadline:
            detail = "not verified before the timeout"
            if last_error:
                detail += f" (last probe error: {last_error})"
            return False, detail
        time.sleep(max(0.05, float(interval)))


def verify_import(module: str) -> bool:
    """True when ``module`` can actually be imported right now."""
    try:
        return importlib.util.find_spec(str(module)) is not None
    except Exception:
        return False


__all__ = [
    "KIND_INSTALL", "KIND_UPDATE", "KIND_DOWNLOAD", "KIND_UNINSTALL",
    "SELF_SOURCE", "INSTALL_INTENT_RE", "INSTALLER_TARGET_RE",
    "InstallRequest",
    "classify_intent", "is_installer_target", "protected_target",
    "requires_confirmation", "refusal_for", "guard",
    "unverified", "verify", "verify_import",
]
