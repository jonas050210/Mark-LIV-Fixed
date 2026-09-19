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
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


# ── refusal patterns: open() NEVER installs / repairs / updates ──────────────
# The patterns themselves live in core/install_safety.py — the one module every
# install/download path consults — and are re-exported here so the launcher side
# and the safety side can never disagree about what an install request is.

from core.install_safety import (  # noqa: E402  (kept next to their use)
    INSTALLER_TARGET_RE as INSTALLER_TARGET_RE,
    INSTALL_INTENT_RE as INSTALL_INTENT_RE,
    classify_intent as _classify_install_intent,
    is_installer_target as _is_installer_target,
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


def is_self_name(name: str) -> bool:
    """True when `name` means this assistant itself."""
    canon = _canon(name)
    return bool(canon) and (canon in SELF_NAMES or canon.rstrip("s") in SELF_NAMES)


def _refuse_install_intent(name: str) -> str | None:
    kind = _classify_install_intent(name or "")
    if not kind:
        return None
    if kind == "uninstall":
        return (
            f"I do not install, update, repair, or download software — and I "
            f"never remove an app from the open path. '{name.strip()[:80]}' is "
            "an uninstall request: use the uninstall_app tool, which goes "
            "looking for the vendor's real uninstaller and asks you to confirm "
            "on screen first."
        )
    return (
        f"I do not install, update, repair, or download software — "
        f"'{name.strip()[:80]}' sounds like one of those jobs. "
        "Please install it yourself (Microsoft Store, Steam, or the "
        "vendor's site); once it is installed I can open and close it."
    )


def looks_like_installer(path: str) -> bool:
    """True when `path` smells like an installer/updater, not the app."""
    return _is_installer_target(path)


def _observe_launch(name: str, tgt, seconds: float) -> dict:
    """Watch the process table right after a launch.

    Returns ``{"running": bool|None, "appeared": bool, "vanished": bool}``:
    ``appeared`` means the process existed at least once, ``vanished`` that it
    then disappeared — which is a crash or a launcher handing off, and deserves
    a different (honest, actionable) message than "never started".
    """
    appeared = False
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        running = is_running(name, tgt)
        if running is None:
            return {"running": None, "appeared": appeared, "vanished": False}
        if running:
            appeared = True
        elif appeared:
            return {"running": False, "appeared": True, "vanished": True}
        if time.monotonic() >= deadline:
            return {"running": appeared, "appeared": appeared, "vanished": False}
        time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))


def _launch_strategies(tgt, _af) -> list:
    """The target, plus any documented fallbacks (Roblox has some)."""
    out = [tgt]
    try:
        out.extend(_af.launch_alternatives(tgt))
    except Exception:
        pass
    return out


def open(name: str, verify_seconds: float = 6.0) -> tuple[bool, str]:
    """Resolve → launch → verify. Returns ``(ok, message)``. Never raises.

    Refuses installer-intent requests and installer-like targets. Verifies by
    polling the process table for a few seconds; reports honestly when the
    process never appears, when it appeared and vanished, or when process
    access is unavailable. A target may declare fallback strategies
    (:func:`core.app_finder.launch_alternatives`) — each is launched and
    verified in turn, so an alternative that works is reported as the one that
    worked instead of a blanket failure.
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

    last_problem = f"{display} could not be started."
    for index, attempt in enumerate(_launch_strategies(tgt, _af)):
        try:
            from core.gaming import protect_focus
            if protect_focus():
                ok, msg = _af.launch(attempt, background=True)
            else:
                ok, msg = _af.launch(attempt)
        except Exception as e:
            diag = ""
            try:
                diag = _af.diagnose_launch_error(e, attempt)
            except Exception:
                pass
            last_problem = (f"Could not start {display}: {e}"
                            f"{(' — ' + diag) if diag else ''}")
            continue
        if not ok:
            last_problem = msg or f"Could not start {display}."
            continue

        # Verify: poll the process table (launchers often return before the
        # process exists; Roblox's launcher hands off to the player).
        obs = _observe_launch(name, attempt, max(0.5, verify_seconds))
        if obs["running"] is None:
            return True, (f"{display} started ({msg}). I cannot verify it is "
                          "running — process access is unavailable on this machine.")
        if obs["running"]:
            via = "" if index == 0 else f" (via {getattr(attempt, 'source', '') or 'fallback'})"
            return True, f"{display} is now running{via}."
        if obs["vanished"]:
            hint = ""
            try:
                hint = _af.launch_hint(attempt, vanished=True)
            except Exception:
                pass
            last_problem = (f"{display} started and then exited immediately"
                            f"{(' — ' + hint) if hint else ' — it may have crashed on startup.'}")
        else:
            last_problem = (f"{display} did not appear to start ({msg}). It may need "
                            "a click in a launcher window, or it crashed on startup.")

    # Every strategy failed. Say what was tried, then the most useful hint.
    hint = ""
    try:
        hint = _af.launch_hint(tgt)
    except Exception:
        pass
    if hint and "roblox" in str(getattr(tgt, "target", "")).lower():
        return False, f"{last_problem} {hint}"
    if len(_launch_strategies(tgt, _af)) > 1:
        last_problem += " I tried every way of starting it that I know."
    return False, last_problem


# ── windows ──────────────────────────────────────────────────────────────────
# Window handling lives here for the same reason process handling does: one
# implementation, one set of refusals. `window_control` (the tool) and the
# `computer_control` focus_window action both come through this, so they can
# never disagree about which window is meant.
#
# What is deliberately absent: closing windows. Sending Alt+F4/Ctrl+W to
# whatever happens to be focused is how a save dialog gets dismissed by
# accident; apps are closed by name through close() instead.


def _window_api():
    """The pygetwindow module, or None when it is not usable here."""
    try:
        import pygetwindow as gw  # type: ignore

        return gw
    except Exception:
        return None


def _shell_safe(title: str) -> bool:
    """False when a title cannot be put into a command line safely."""
    text = str(title or "")
    if not text.strip() or len(text) > 200:
        return False
    return not any(ch in text for ch in '"\'\n\r\x00$`\\')


def _window_titles_from_os(limit: int) -> tuple[list[str] | None, str]:
    """Platform fallback for listing window titles."""
    try:
        if sys.platform == "win32":
            script = ("Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | "
                      "Select-Object -ExpandProperty MainWindowTitle")
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=8, **_no_window(),
            ).stdout
            return [t.strip() for t in out.splitlines() if t.strip()][:limit], ""
        if sys.platform == "darwin":
            out = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of every process '
                 'whose background only is false'],
                capture_output=True, text=True, timeout=8).stdout
            return [t.strip() for t in out.split(",") if t.strip()][:limit], ""
        out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True,
                             timeout=8).stdout
        titles = []
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[3].strip():
                titles.append(parts[3].strip())
        if titles:
            return titles[:limit], ""
        return None, "no window listing tool found (wmctrl is not installed)"
    except FileNotFoundError:
        return None, "no window listing tool found (wmctrl is not installed)"
    except Exception as e:
        return None, f"could not list windows ({type(e).__name__})"


def _no_window() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def list_windows(limit: int = 40) -> tuple[list[str] | None, str]:
    """Visible window titles, or ``(None, reason)`` when they cannot be read."""
    limit = max(1, min(100, int(limit or 40)))
    gw = _window_api()
    if gw is not None:
        try:
            titles: list[str] = []
            for win in gw.getAllWindows():
                title = str(getattr(win, "title", "") or "").strip()
                if not title:
                    continue
                if hasattr(win, "visible") and not win.visible:
                    continue
                titles.append(title)
            if titles:
                return titles[:limit], ""
        except Exception:
            pass
    return _window_titles_from_os(limit)


def _matching_windows(query: str, gw):
    """Window objects whose title contains `query` (case-insensitive)."""
    want = str(query or "").strip().lower()
    if not want:
        return []
    out = []
    for win in gw.getAllWindows():
        title = str(getattr(win, "title", "") or "")
        if title and want in title.lower():
            out.append(win)
    return out


def _window_flag(win, name):
    """Missing/unreadable state is unknown, not evidence of success."""
    try:
        value = getattr(win, name, None)
        return value if type(value) is bool else None
    except Exception:
        return None


def _focus_via_os(title: str) -> tuple[bool, str]:
    """Platform command that raises a window, used when pygetwindow is absent."""
    if not _shell_safe(title):
        return False, "that window title cannot be used safely (quotes or control characters)"
    titles, _ = _window_titles_from_os(100)
    if titles is not None:
        matches = [t for t in titles if title.casefold() in t.casefold()]
        matches = [t for t in matches if t.casefold() == title.casefold()] or matches
        if len(matches) > 1:
            return False, "Several windows match; provide an exact, unique title."
        if matches:
            title = matches[0]
            if not _shell_safe(title):
                return False, "The resolved window title cannot be used safely."
    try:
        if sys.platform == "win32":
            script = (f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")')
            response = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", script], capture_output=True, text=True, timeout=8,
                           **_no_window())
            if response.returncode or response.stdout.strip().lower() != "true":
                return False, f"Windows did not accept focusing '{title}'."
            observed = foreground_window()
            verified = str(observed.get("title", "")).casefold() == title.casefold()
            return verified, (f"'{title}' is in front now." if verified else
                              f"Focus requested for '{title}', but the foreground window is unverified.")
        if sys.platform == "darwin":
            script = (f'tell application "System Events" to set frontmost of '
                      f'(first process whose name contains "{title}") to true')
            response = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=8)
            return False, (f"Focus request failed for '{title}'." if response.returncode else
                           f"Focus requested for '{title}', but the foreground window is unverified.")
        for cmd in (["wmctrl", "-a", title],
                    ["xdotool", "search", "--name", title, "windowactivate"]):
            try:
                if subprocess.run(cmd, capture_output=True, timeout=8).returncode == 0:
                    return False, f"Focus requested for '{title}', but the foreground window is unverified."
            except FileNotFoundError:
                continue
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return False, ("focusing windows needs pygetwindow, wmctrl or xdotool on "
                   "this machine — none of them is usable here")


def foreground_window() -> dict:
    """Read-only Windows observation. Never changes focus or attaches input queues."""
    if sys.platform != "win32":
        return {}
    try:
        import win32gui
        import win32process
        import win32api
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == _own_pid():
            return {}
        ps = _psutil()
        process = ps.Process(pid)
        path = process.exe()
        rect = win32gui.GetWindowRect(hwnd)
        monitor = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd))["Monitor"]
        fullscreen = all(abs(a - b) <= 2 for a, b in zip(rect, monitor))
        return {"hwnd": hwnd, "pid": pid, "exe": process.name().lower(), "path": path,
                "title": win32gui.GetWindowText(hwnd), "fullscreen": fullscreen,
                "monitor_device": win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd)).get("Device", "")}
    except Exception:
        return {}


def _geometry_action(win, action, monitor=None, width=None, height=None, topmost=False):
    """Work-area geometry with SWP_NOACTIVATE; verifies the resulting rectangle."""
    if sys.platform != "win32":
        return False, "Monitor placement is currently supported on Windows only."
    try:
        import win32api
        import win32gui
        import win32con
        import win32process
        hwnd = win._hWnd
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        ps = _psutil()
        if ps is None or pid == _own_pid() or _is_protected_name(ps.Process(pid).name()):
            return False, "Window placement refused for a protected or unverifiable process."
        if not win32gui.IsWindow(hwnd):
            return False, "That window no longer exists."
        current = win32gui.GetWindowRect(hwnd)
        monitors = [entry[0] for entry in win32api.EnumDisplayMonitors()]
        selected = win32api.MonitorFromWindow(hwnd)
        if monitor is not None:
            index = int(monitor) - 1
            if not 0 <= index < len(monitors):
                return False, f"Choose a monitor from 1 to {len(monitors)}."
            selected = monitors[index]
        l, t, r, b = win32api.GetMonitorInfo(selected)["Work"]
        x, y = max(l, current[0]), max(t, current[1])
        w, h = min(current[2] - current[0], r-l), min(current[3] - current[1], b-t)
        if action in ("snap_left", "snap_right"):
            w, h, y = (r-l)//2, b-t, t
            x = l if action == "snap_left" else r-w
        elif action == "resize":
            w = max(160, min(int(width), r-l))
            h = max(100, min(int(height), b-t))
        x, y = min(x, r-w), min(y, b-h)
        flags = win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER
        order = 0
        if action == "always_on_top":
            flags = win32con.SWP_NOACTIVATE | win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
            order = win32con.HWND_TOPMOST if topmost else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(hwnd, order, x, y, w, h, flags)
        if action == "always_on_top":
            actual = bool(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST)
            ok = actual == bool(topmost)
        else:
            actual = win32gui.GetWindowRect(hwnd)
            ok = all(abs(a-b) <= 2 for a, b in zip(actual, (x, y, x+w, y+h)))
        return ok, (f"Window {action} verified." if ok else
                    "The app did not accept the requested window geometry; it may enforce a minimum size.")
    except (TypeError, ValueError):
        return False, "Resize needs numeric width and height; monitor numbers start at 1."
    except Exception as exc:
        return False, f"Window placement unavailable: {type(exc).__name__}."


def window_action(action: str, name: str = "", title: str = "",
                  limit: int = 40, *, monitor=None, width=None, height=None,
                  topmost=False) -> tuple[bool, str]:
    """List, focus, minimize, maximize or restore windows. Never raises.

    ``name`` is an app name (resolved to its own window titles) and ``title`` a
    literal window title; whichever is given is what gets matched.
    """
    act = str(action or "").strip().lower()
    _KNOWN = ("list", "show", "focus", "activate", "switch", "minimize",
              "minimise", "maximize", "maximise", "restore", "unminimize",
              "unminimise", "snap_left", "snap_right", "move_monitor", "resize", "always_on_top")
    if act not in _KNOWN:
        return False, (f"Unknown window action '{action}'. Use: list, focus, "
                       f"minimize, maximize, restore.")
    if act in ("list", "show"):
        titles, why = list_windows(limit=limit)
        if titles is None:
            return False, f"I cannot read the window list here: {why}."
        if not titles:
            return True, "No windows with a title are open."
        return True, ("Open windows: " + "; ".join(titles)).strip()

    query = str(title or "").strip()
    if not query and name:
        query = str(name).strip()
    if not query:
        return False, "Which window? Give me an app name or a window title."

    # An app name means "its window(s)" — resolved through the same window-title
    # lookup the status check uses, so app and window views agree.
    if not title and name:
        app_titles = window_titles(name, name)
        if app_titles:
            query = app_titles[0]

    gw = _window_api()
    wins = []
    if gw is not None:
        try:
            wins = _matching_windows(query, gw)
        except Exception:
            wins = []
    exact = [w for w in wins if str(w.title).casefold() == query.casefold()]
    if exact:
        wins = exact
    if len(wins) > 1:
        return False, "Several windows match. Give an exact title: " + "; ".join(str(w.title) for w in wins[:5])
    win = wins[0] if wins else None

    from core.gaming import protect_focus
    protected = protect_focus()
    if protected and (act in ("maximize", "maximise", "restore", "unminimize", "unminimise")
                      or (act == "always_on_top" and topmost)):
        return False, "Gaming Mode protected the game. Explicitly focus the utility first to bring it forward."
    if act in ("snap_left", "snap_right", "move_monitor", "resize", "always_on_top"):
        if win is None:
            return False, f"I could not find a window matching '{query}'."
        return _geometry_action(win, act, monitor, width, height, topmost)

    if act in ("focus", "activate", "switch"):
        if win is not None:
            try:
                if bool(getattr(win, "isMinimized", False)):
                    win.restore()
                win.activate()
                ok = _window_flag(win, "isActive") is True
                if ok:
                    return True, (f"'{win.title}' is in front now."
                                  if getattr(win, "title", "") else "Window focused.")
                return False, (f"Asked '{query}' to come to the front, but it is "
                              f"not the active window yet.")
            except Exception as e:
                return False, f"Could not focus '{query}': {type(e).__name__}."
        ok, msg = _focus_via_os(query)
        return ok, msg if ok else (f"I could not find a window matching "
                                   f"'{query}': {msg}")

    if act in ("minimize", "minimise"):
        if win is None:
            return False, f"I could not find a window matching '{query}'."
        try:
            win.minimize()
        except Exception as e:
            return False, f"Could not minimize '{query}': {type(e).__name__}."
        done = _window_flag(win, "isMinimized") is True
        return done, (f"'{win.title}' is minimized." if done else
                      f"Minimize requested for '{query}', but the resulting state is unverified.")

    if act in ("maximize", "maximise"):
        if win is None:
            return False, f"I could not find a window matching '{query}'."
        try:
            win.maximize()
        except Exception as e:
            return False, f"Could not maximize '{query}': {type(e).__name__}."
        done = _window_flag(win, "isMaximized") is True
        return done, (f"'{win.title}' is maximized." if done else
                      f"Maximize requested for '{query}', but the resulting state is unverified.")

    if act in ("restore", "unminimize", "unminimise"):
        if win is None:
            return False, f"I could not find a window matching '{query}'."
        try:
            win.restore()
            done = (_window_flag(win, "isMinimized") is False
                    and _window_flag(win, "isMaximized") is False)
            return done, (f"'{win.title}' is restored." if done else
                          f"Restore requested for '{query}', but the resulting state is unverified.")
        except Exception as e:
            return False, f"Could not restore '{query}': {type(e).__name__}."

    return False, f"Unknown window action '{action}'."


# ── uninstall ────────────────────────────────────────────────────────────────
# Removal is the one app operation with no undo, so it is split in two: the plan
# (what would be run, shown to the user before anything happens) and the run
# itself. Nothing here deletes an install directory — that is not an uninstall,
# it leaves registry entries and user data behind — and an app with no
# vendor-registered uninstaller gets an honest refusal instead.


def uninstall_plan(name: str) -> tuple[object | None, str]:
    """``(plan, refusal)`` for removing `name`. Exactly one of them is set.

    The plan is resolved through the same app finder ``open`` uses, so an app
    that cannot be found cannot be removed either.
    """
    from core import app_finder as _af
    from core.install_safety import protected_target

    raw = (name or "").strip()
    if not raw:
        return None, "Which app should I uninstall?"
    if _canon(raw) in SELF_NAMES or _canon(raw).rstrip("s") in SELF_NAMES:
        return None, "I will not uninstall myself — say 'shutdown' if you want me to quit."
    guard = protected_target(raw)
    if guard:
        return None, guard

    tgt = _resolve(raw)
    try:
        plan = _af.find_uninstaller(raw, tgt)
    except Exception as e:
        return None, f"I could not look for an uninstaller for {raw} ({type(e).__name__})."
    if plan is None:
        display = _tgt_display(tgt, raw) if tgt is not None else raw
        return None, (
            f"I found no reliable uninstaller for '{display}'. Uninstalling "
            "means running the vendor's own removal program, and this machine "
            "has none registered for it (portable builds and unpacked folders "
            "usually have none). I will not delete its folder instead: that "
            "leaves registry entries and user data behind and is not an "
            "uninstall. Remove it through Windows' 'Apps & features', the "
            "Microsoft Store, or the vendor's own page."
        )
    return plan, ""


def run_uninstall(plan, name: str = "", *, timeout: float = 180.0,
                  task=None) -> tuple[str, str]:
    """Run a resolved uninstall plan and say what was actually observed.

    Returns ``(status, message)`` with status one of:

    - ``"removed"`` — the app is gone by the resolver's own account;
    - ``"running"`` — the uninstaller is still on screen (it usually asks
      questions), so removal is *not* claimed;
    - ``"failed"`` — it exited without removing anything, or never started.

    ``task`` is optional and duck-typed: anything with ``update(...)`` (the
    central activity registry's :class:`core.tasks.Task`) is told what is going
    on while the uninstaller runs.
    """

    def _note(detail: str) -> None:
        if task is not None:
            try:
                task.update(detail=detail)
            except Exception:
                pass

    raw = (name or "").strip()
    source = str(getattr(plan, "source", "") or "uninstaller")
    location = str(getattr(plan, "location", "") or "")

    # macOS: the bundle goes to the Trash — removable, and nothing to wait for.
    if source.startswith("macOS"):
        bundle = str(getattr(plan, "exe", ""))
        try:
            try:
                from send2trash import send2trash  # type: ignore

                send2trash(bundle)
            except Exception:
                trash = Path.home() / ".Trash"
                trash.mkdir(exist_ok=True)
                shutil.move(bundle, str(trash / Path(bundle).name))
        except Exception as e:
            return "failed", f"Could not move '{bundle}' to the Trash: {e}"
        if not Path(bundle).exists():
            return "removed", f"'{Path(bundle).name}' is in the Trash."
        return "failed", f"'{bundle}' is still there after the move."

    exe = str(getattr(plan, "exe", ""))
    args = list(getattr(plan, "args", ()) or ())
    if not exe or not Path(exe).exists():
        return "failed", f"The registered uninstaller is gone: {exe or '(none)'}."

    _note(f"started {Path(exe).name} — answer its window yourself")
    try:
        # No shell, no creationflags: an uninstaller is a GUI program that has to
        # be visible, and argv never passes through a shell.
        proc = subprocess.Popen([exe, *args], cwd=str(Path(exe).parent))
    except Exception as e:
        return "failed", f"Could not start the uninstaller ({type(e).__name__}: {e})."

    deadline = time.monotonic() + max(5.0, timeout)
    while True:
        gone = _resolve(raw) is None
        if gone:
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
            return "removed", (
                f"'{raw}' is uninstalled — the resolver no longer finds it."
            )
        if proc.poll() is not None:
            break
        _note(f"{Path(exe).name} is still running — it is probably asking you something")
        if time.monotonic() >= deadline:
            return "running", (
                f"The uninstaller for '{raw}' is still running on your screen "
                f"({Path(exe).name}). I cannot confirm the removal until it "
                f"finishes — check its window."
            )
        time.sleep(1.0)

    code = proc.returncode
    if _resolve(raw) is None:
        return "removed", f"'{raw}' is uninstalled."
    if code == 0:
        return "failed", (
            f"The uninstaller finished (exit code 0) but '{raw}' is still "
            f"installed{(' in ' + location) if location else ''}. It may have "
            f"asked something that was not answered, or cancelled itself."
        )
    return "failed", (
        f"The uninstaller exited with code {code} and '{raw}' is still "
        f"installed. Nothing can be claimed from that."
    )


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
