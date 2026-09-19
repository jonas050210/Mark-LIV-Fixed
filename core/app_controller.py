"""Unified app controller: installed ≠ running, launch, and safe close.

One module answers the three questions every app-related tool needs:

- ``status(name)`` — is it installed, where, and is it *actually running*
  right now (process check, plus window titles when available)?
- ``open(name)`` — resolve via :mod:`core.app_finder`, launch, verify the
  process appeared. NEVER installs, repairs, updates, or downloads anything:
  installer-like requests and installer-like targets are refused outright.
- ``close(name)`` — terminate the app's own processes through the SAME
  resolver ``open`` uses (no Alt+F4, no Ctrl+W, no global shortcuts), with
  escalation terminate → wait → kill and a post-check that it is really gone.

Safety rules (non-negotiable):

- JARVIS never closes itself: its own PID, any process whose command line
  identifies it as this assistant, and the assistant's own names are refused.
- System-critical and shell processes are never touched, whatever the caller
  asks (exact-name blocklist).
- Processes are matched by executable basename ONLY — never by fuzzy command
  line substrings. A Java/Python/StoreContainer-hosted app whose own exe
  cannot be determined reports "not running" rather than risking a wrong kill.
- No ``shell=True`` anywhere, no installer execution, no wildcards.

When ``psutil`` is unavailable, process answers degrade to "unknown" with an
honest message instead of a guess.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


# ── refusal patterns: open() NEVER installs / repairs / updates ──────────────

#: Verbs that turn a request into an install/repair/update job (EN + DE).
#: Matched case-insensitively against the *requested name*. "mach was du
#: willst" does not override this: there is no code path from here to any
#: installer, package manager, or download.
INSTALL_INTENT_RE = re.compile(
    r"\b(install|installation|installer|setup|update|upgrade|repair|reinstall|"
    r"download|uninstall|remove-app|deinstallier\w*|installier\w*|"
    r"aktualisier\w*|updat\w*|reparier\w*|herunterlad\w*|download\w*|"
    r"einricht\w*|besorg\w*|hol\s+dir)\b",
    re.IGNORECASE,
)

#: File-name fragments that mark a *target* as an installer/updater rather
#: than the app itself. Last line of defense: even a clean request refuses to
#: launch these.
INSTALLER_TARGET_RE = re.compile(
    r"(setup|install|update|upgrade|patch|uninstall|unins\d*|repair|redist|"
    r"vcredist|dxsetup|oobe|installer)",
    re.IGNORECASE,
)

#: Names that mean this assistant itself — close() refuses all of them.
SELF_NAMES = frozenset({
    "jarvis", "mark", "mark liv", "mark-liv", "markliv", "assistant",
    "sprachassistent", "du selbst", "dich selbst", "yourself",
})

#: Command-line fragments identifying this assistant's processes (own PID and
#: siblings — e.g. the wake-word engine child process).
SELF_CMDLINE_HINTS = ("jarvis", "mark-liv", "mark_liv", "markliv")

#: Exact executable basenames that are NEVER terminated (case-insensitive).
SYSTEM_PROTECTED = frozenset({
    # Windows kernel / session / login
    "system", "registry", "memory compression", "idle",
    "smss.exe", "csrss.exe", "wininit.exe", "services.exe", "lsass.exe",
    "lsm.exe", "winlogon.exe", "logonui.exe", "svchost.exe", "dwm.exe",
    "fontdrvhost.exe", "sihost.exe", "taskhostw.exe", "conhost.exe",
    "ctfmon.exe",
    # The shell itself: killing it takes the taskbar/desktop with it.
    "explorer.exe",
    # macOS / Linux core
    "kernel_task", "launchd", "systemd", "init",
})

#: Generic runtimes: NEVER termination candidates (see close()), and never
#: matched when the target's own exe is known (see find_processes) — any
#: java.exe would otherwise make "minecraft" look running, or get killed for
#: it. A runtime-hosted app without a known exe reports honestly instead.
_GENERIC_RUNTIME_NAMES = frozenset({
    "python.exe", "pythonw.exe", "python", "python3",
    "node.exe", "node", "java.exe", "javaw.exe", "java",
    "powershell.exe", "cmd.exe", "wt.exe",
})

#: Canonical name → known executable basenames. Fallback for when resolution
#: fails (portable installs, PATH-only tools): the resolved target's own exe
#: name is ALWAYS preferred over these hints.
PROCESS_HINTS: dict[str, tuple[str, ...]] = {
    "spotify": ("spotify.exe", "spotify"),
    "chrome": ("chrome.exe", "chrome", "google-chrome",
               "google-chrome-stable"),
    "chromium": ("chromium.exe", "chromium"),
    "firefox": ("firefox.exe", "firefox"),
    "edge": ("msedge.exe", "msedge", "microsoft-edge"),
    "discord": ("discord.exe", "discord", "discordcanary.exe"),
    "steam": ("steam.exe", "steam"),
    "epic": ("epicgameslauncher.exe",),
    "roblox": ("robloxplayerbeta.exe", "robloxplayer.exe", "roblox"),
    "roblox studio": ("robloxstudiobeta.exe", "robloxstudio.exe"),
    "geometry dash": ("geometrydash.exe",),
    "gd": ("geometrydash.exe",),
    "ksp2": ("ksp2_x64.exe", "ksp2.exe", "kerbal space program 2.exe"),
    "kerbal space program 2": ("ksp2_x64.exe", "ksp2.exe"),
    "kerbal space program": ("ksp_x64.exe", "ksp.exe"),
    "minecraft": ("javaw.exe", "java.exe", "minecraft.exe",
                  "minecraftlauncher.exe"),
    "vscode": ("code.exe", "code"),
    "visual studio code": ("code.exe", "code"),
    "notepad": ("notepad.exe",),
    "vlc": ("vlc.exe", "vlc"),
    "obs": ("obs64.exe", "obs.exe", "obs"),
    "whatsapp": ("whatsapp.exe", "whatsapp"),
    "telegram": ("telegram.exe", "telegram"),
    "teams": ("ms-teams.exe", "teams.exe"),
    "zoom": ("zoom.exe", "zoom"),
    "outlook": ("outlook.exe", "olk.exe"),
    "excel": ("excel.exe",),
    "word": ("winword.exe",),
    "powerpoint": ("powerpnt.exe",),
}


@dataclass
class AppStatus:
    """Installed-vs-running truth for one app."""

    name: str
    display_name: str = ""
    installed: bool = False
    install_path: str = ""
    launchable: bool = False
    running: bool = False
    running_unknown: bool = False
    pids: list[int] = field(default_factory=list)
    window_titles: list[str] = field(default_factory=list)
    detail: str = ""

    def short(self) -> str:
        state = "unknown (no process access)" if self.running_unknown else (
            "RUNNING" if self.running else "not running")
        where = f" at {self.install_path}" if self.install_path else ""
        if not self.installed:
            return f"{self.display_name or self.name}: not installed, {state}."
        return f"{self.display_name or self.name}: installed{where}, {state}."


# ── process access (psutil, guarded) ─────────────────────────────────────────


def _psutil():
    try:
        import psutil  # type: ignore

        return psutil
    except Exception:
        return None


def _own_pid() -> int:
    try:
        return os.getpid()
    except Exception:
        return -1


def _is_self_process(name: str, cmdline: str) -> bool:
    low = (cmdline or "").lower()
    return any(h in low for h in SELF_CMDLINE_HINTS)


def _is_protected_name(exe_basename: str) -> bool:
    low = (exe_basename or "").lower()
    stem = low[:-4] if low.endswith(".exe") else low
    return low in SYSTEM_PROTECTED or stem in SYSTEM_PROTECTED


# ── process-name resolution ──────────────────────────────────────────────────


def _canon(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _target_exes(target) -> list[str]:
    """The resolved target's own exe basename(s), never generic runtimes."""
    out: list[str] = []
    if target is None:
        return out
    for attr in ("target", "path", "exe"):
        try:
            raw = getattr(target, attr, None)
        except Exception:
            raw = None
        if not raw:
            continue
        try:
            base = Path(str(raw)).name.lower()
        except Exception:
            base = ""
        if base and base not in out and base not in _GENERIC_RUNTIME_NAMES:
            out.append(base)
    return out


def process_names_for(name: str, target=None) -> list[str]:
    """Executable basenames identifying `name`. The resolved target's own exe
    comes first; canonical hints extend it. Lowercased, de-duplicated."""
    out: list[str] = list(_target_exes(target))
    for hint in PROCESS_HINTS.get(_canon(name), ()):
        if hint not in out:
            out.append(hint)
    # Last resort: "<name>.exe" on Windows, "<name>" elsewhere.
    if not out:
        guess = _canon(name).replace(" ", "")
        if guess:
            out.append(guess + (".exe" if sys.platform == "win32" else ""))
    return out


def find_processes(name: str, target=None) -> tuple[list[dict] | None, str]:
    """All processes whose exe basename matches `name`.

    Returns ``(matches, note)`` where matches is None when process access is
    unavailable (psutil missing). Each match: ``{pid, name, exe, cmdline}``.
    Basename-only matching — see module docstring for why.
    """
    psutil = _psutil()
    if psutil is None:
        return None, "process listing unavailable (psutil not installed)"
    wanted = set(process_names_for(name, target))
    if _target_exes(target):
        # Ground truth known: runtime hints would only add false positives.
        wanted -= _GENERIC_RUNTIME_NAMES
    matches: list[dict] = []
    try:
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                info = proc.info
                base = (info.get("name") or "").lower()
                if not base and info.get("exe"):
                    try:
                        base = Path(info["exe"]).name.lower()
                    except Exception:
                        base = ""
                if base in wanted:
                    cmd = info.get("cmdline") or []
                    matches.append({
                        "pid": info.get("pid"),
                        "name": base,
                        "exe": info.get("exe") or "",
                        "cmdline": " ".join(cmd) if isinstance(cmd, list) else str(cmd),
                    })
            except Exception:
                continue
    except Exception as e:
        return None, f"process listing failed: {e}"
    return matches, ""


def is_running(name: str, target=None) -> bool | None:
    """True/False, or None when process access is unavailable.

    Counts only non-self processes: this assistant's own PID or siblings never
    make an app "running".
    """
    matches, _note = find_processes(name, target)
    if matches is None:
        return None
    own = _own_pid()
    for m in matches:
        if m["pid"] == own:
            continue
        if _is_self_process(m["name"], m["cmdline"]):
            continue
        return True
    return False


def window_titles(name: str, display: str = "", limit: int = 5) -> list[str]:
    """Visible window titles belonging to `name` (best effort, never raises).

    Matches title substrings against name/display tokens (≥3 chars). Empty
    when the platform helper is unavailable — windows are a bonus signal, the
    process check is the ground truth.
    """
    tokens = {t for t in re.split(r"[^a-z0-9]+", (_canon(display) or _canon(name)))
              if len(t) >= 3}
    # Single generic tokens ("app", "the") would match everything.
    tokens -= {"app", "the", "and", "for", "pro", "new", "gmbh"}
    if not tokens:
        return []
    try:
        import pygetwindow as gw  # type: ignore

        titles = gw.getAllTitles()
    except Exception:
        return []
    out: list[str] = []
    try:
        for title in titles:
            low = str(title or "").lower()
            if low and any(t in low for t in tokens):
                out.append(str(title).strip())
                if len(out) >= limit:
                    break
    except Exception:
        pass
    return out


# ── status / open / close ────────────────────────────────────────────────────


def _resolve(name: str):
    from core import app_finder as _af

    try:
        return _af.resolve(name)
    except Exception:
        return None


def _tgt_display(tgt, fallback: str) -> str:
    for attr in ("display", "display_name"):
        try:
            val = str(getattr(tgt, attr, "") or "").strip()
        except Exception:
            val = ""
        if val:
            return val
    return fallback


def _tgt_path(tgt) -> str:
    for attr in ("target", "path", "exe"):
        try:
            val = str(getattr(tgt, attr, "") or "").strip()
        except Exception:
            val = ""
        if val:
            return val
    return ""


def status(name: str) -> AppStatus:
    """Installed + running truth for `name`. Never raises."""
    name = (name or "").strip()
    st = AppStatus(name=name, display_name=name)
    if not name:
        st.detail = "no app name given"
        return st
    tgt = _resolve(name)
    if tgt is not None:
        st.installed = True
        st.display_name = _tgt_display(tgt, name)
        st.install_path = _tgt_path(tgt)
        st.launchable = True
    else:
        st.display_name = name
        st.detail = "not found by the app resolver"
    running = is_running(name, tgt)
    if running is None:
        st.running_unknown = True
        st.detail = (st.detail + "; " if st.detail else "") + \
            "running state unknown (psutil not installed)"
    else:
        st.running = running
        if running:
            matches, _ = find_processes(name, tgt)
            own = _own_pid()
            st.pids = [m["pid"] for m in (matches or [])
                       if m["pid"] != own
                       and not _is_self_process(m["name"], m["cmdline"])][:20]
            st.window_titles = window_titles(name, st.display_name)
    return st


def _refuse_install_intent(name: str) -> str | None:
    if INSTALL_INTENT_RE.search(name or ""):
        return (
            f"I do not install, update, repair, or download software — "
            f"'{name.strip()[:80]}' sounds like one of those jobs. "
            "Please install it yourself (Microsoft Store, Steam, or the "
            "vendor's site); once it is installed I can open and close it."
        )
    return None


def looks_like_installer(path: str) -> bool:
    """True when `path` smells like an installer/updater, not the app."""
    base = ""
    try:
        base = Path(str(path or "")).name
    except Exception:
        base = str(path or "")
    return bool(base and INSTALLER_TARGET_RE.search(base))


def open(name: str, verify_seconds: float = 6.0) -> tuple[bool, str]:
    """Resolve → launch → verify. Returns ``(ok, message)``. Never raises.

    Refuses installer-intent requests and installer-like targets. Verifies by
    polling the process table for a few seconds; reports honestly when the
    process never appears (or when process access is unavailable).
    """
    from core import app_finder as _af

    name = (name or "").strip()
    if not name:
        return False, "Which app should I open?"
    refusal = _refuse_install_intent(name)
    if refusal:
        return False, refusal

    tgt = _resolve(name)
    if tgt is None:
        return False, (
            f"I could not find '{name}' on this machine — and I do not "
            "install software. If it is installed under a different name, "
            "tell me the exact name.")
    tgt_path = _tgt_path(tgt)
    if looks_like_installer(tgt_path):
        return False, (
            f"I will not launch '{Path(tgt_path).name}': it looks like an "
            "installer or updater, not the app itself.")
    display = _tgt_display(tgt, name)
    try:
        ok, msg = _af.launch(tgt)
    except Exception as e:
        diag = ""
        try:
            diag = _af.diagnose_launch_error(e, tgt)
        except Exception:
            pass
        return False, f"Could not start {display}: {e}{(' — ' + diag) if diag else ''}"
    if not ok:
        return False, msg or f"Could not start {display}."

    # Verify: poll the process table (launchers often return before the
    # process exists; Roblox's launcher hands off to the player).
    deadline = time.monotonic() + max(0.5, verify_seconds)
    running: bool | None = None
    while time.monotonic() < deadline:
        running = is_running(name, tgt)
        if running or running is None:
            break  # None = no process access: nothing to wait for
        time.sleep(0.5)
    if running is None:
        return True, (f"{display} started ({msg}). I cannot verify it is "
                      "running — process access is unavailable on this machine.")
    if running:
        return True, f"{display} is now running."
    return False, (f"{display} did not appear to start ({msg}). It may need "
                   "a click in a launcher window, or it crashed on startup.")


def close(name: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Close `name` via its own processes. Returns ``(ok, message)``.

    Same resolver as :func:`open`, terminate → wait → kill escalation, and a
    post-check. Refuses: empty names, the assistant itself, protected system
    processes, and apps that are not running.
    """
    name = (name or "").strip()
    if not name:
        return False, "Which app should I close?"
    if _canon(name) in SELF_NAMES or _canon(name).rstrip("s") in SELF_NAMES:
        return False, "I will not close myself. Say 'shutdown' if you want me to quit."
    refusal = _refuse_install_intent(name)
    if refusal:
        return False, refusal

    psutil = _psutil()
    if psutil is None:
        return False, ("I cannot close apps on this machine — process access "
                       "(psutil) is not installed.")

    tgt = _resolve(name)
    display = _tgt_display(tgt, name) if tgt else name
    matches, note = find_processes(name, tgt)
    if matches is None:
        return False, f"I cannot check {display}: {note}."
    own = _own_pid()
    candidates = []
    skipped_self = False
    skipped_protected: list[str] = []
    skipped_runtime: list[str] = []
    for m in matches:
        if m["pid"] == own or _is_self_process(m["name"], m["cmdline"]):
            skipped_self = True
            continue
        if _is_protected_name(m["name"]):
            skipped_protected.append(m["name"])
            continue
        if m["name"] in _GENERIC_RUNTIME_NAMES:
            # Shared runtimes (java/python/...) host other programs too —
            # killing them for one app would be uncontrolled termination.
            skipped_runtime.append(m["name"])
            continue
        candidates.append(m)
    if skipped_protected and not candidates:
        return False, (f"I will not touch {display}: it resolves to a system "
                       "process. That restriction cannot be overridden.")
    if skipped_runtime and not candidates:
        return False, (f"I cannot safely close {display}: it runs inside "
                       f"{sorted(set(skipped_runtime))[0]}, a runtime shared "
                       "with other programs. Close its window by hand.")
    if not candidates:
        if skipped_self and not tgt:
            return False, "I will not close myself."
        return False, f"{display} is not running — nothing to close."

    # Escalate: terminate → wait → kill → verify. Per-PID re-verification so
    # a PID reused between listing and killing cannot hit the wrong process:
    # every handle is checked to still be our exe before each signal.
    wanted = {m["name"] for m in candidates}
    killed_names = set(wanted)
    procs = []
    for m in candidates:
        try:
            p = psutil.Process(m["pid"])
        except Exception:
            continue
        try:
            if p.name().lower() not in wanted:
                continue
        except Exception:
            continue
        procs.append(p)
    if not procs:
        return True, f"{display} is already closed."
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    try:
        _gone, alive = psutil.wait_procs(procs, timeout=max(1.0, timeout))
    except Exception:
        alive = procs
    for p in list(alive):
        try:
            # Re-check identity before the uncatchable signal.
            if p.name().lower() in wanted:
                p.kill()
        except Exception:
            pass
    try:
        _gone2, alive2 = psutil.wait_procs(list(alive), timeout=3.0)
    except Exception:
        alive2 = alive
    if alive2:
        return False, (f"{display} did not close — {len(alive2)} process(es) "
                       "are still running. Try closing it by hand.")
    # Post-check: only the exes we signalled count (hint-bleed from
    # shared runtimes must not turn a clean close into a false "running").
    still = False
    try:
        recheck, _ = find_processes(name, tgt)
        if recheck:
            own_now = _own_pid()
            for m in recheck:
                if m["pid"] == own_now:
                    continue
                if _is_self_process(m["name"], m["cmdline"]):
                    continue
                if m["name"] in killed_names:
                    still = True
                    break
    except Exception:
        still = False
    if still:
        return False, (f"{display} restarted itself or never fully exited — "
                       "it is still running.")
    return True, f"{display} is closed."
