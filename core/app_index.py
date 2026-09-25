"""Installed-application index and launcher.

Launching an application by simulating the Windows key, typing its name into the
Start menu and pressing Enter is unreliable and dangerous: the keystrokes go to
whichever window currently has focus, the Start menu may resolve the text to a
web search or to a different application, and the caller has no way to tell
whether anything started at all.

This module replaces that guesswork with a real index of installed applications
and a launcher that returns the spawned process id, so the caller can verify the
result instead of assuming success.  Three Windows sources are consulted:

* the ``App Paths`` registry keys, which is where classic installers (Chrome,
  Firefox, VS Code, Steam, …) register their executables;
* the Start-menu shortcut trees, whose ``.lnk`` targets are resolved with
  ``pylnk3`` where it is installed so the real executable is launched and a
  process id comes back; the shortcut itself is the fallback;
* ``shell:AppsFolder`` AUMIDs for Store/UWP applications.

macOS enumerates application bundles and launches through ``open -a``; Linux
parses XDG desktop entries and falls back to ``PATH`` binaries.  The result is
cached so a launch is a dictionary lookup rather than a filesystem scan.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from core.json_store import JsonStore, JsonStoreCorruptError
from core.text_match import ratio as _text_ratio

_OS = platform.system()
BASE_DIR = Path(__file__).resolve().parent.parent
INDEX_FILE = BASE_DIR / "config" / "app_index.json"
USAGE_FILE = BASE_DIR / "config" / "app_usage.json"

INDEX_VERSION = 2
CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_RECENT = 12
MAX_PINNED = 12
MAX_USAGE_ENTRIES = 200
MAX_ENTRIES = 4_000
MAX_SCAN_FILES = 20_000
FUZZY_THRESHOLD = 0.62

_KIND_EXEC = "exec"      # direct executable / binary path
_KIND_URI = "uri"        # ms-settings:, mailto:, http(s)://
_KIND_LNK = "lnk"        # Windows shortcut, launched via os.startfile
_KIND_AUMID = "aumid"    # Store/UWP application user model id
_KIND_BUNDLE = "bundle"  # macOS .app bundle
_KINDS = {_KIND_EXEC, _KIND_URI, _KIND_LNK, _KIND_AUMID, _KIND_BUNDLE}

_NOISE_WORDS = {
    "open", "launch", "start", "run", "app", "application", "program",
    "the", "my", "please", "up", "for", "windows", "desktop",
}
_URI_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]{1,31}:")


# ── entry model ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AppEntry:
    """One launchable application discovered on this machine."""

    name: str
    kind: str
    target: str
    source: str = ""

    @property
    def key(self) -> str:
        return normalize_key(self.name)

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_key(value: str) -> str:
    """Fold a name into comparable words: 'Google Chrome.lnk' → 'google chrome'."""
    text = str(value or "")
    for suffix in (".lnk", ".exe", ".app", ".desktop"):
        if text.casefold().endswith(suffix):
            text = text[: -len(suffix)]
            break
    folded = "".join(char.casefold() if char.isalnum() else " " for char in text)
    words = [word for word in folded.split() if word]
    return " ".join(words)


def meaningful_terms(value: str) -> set[str]:
    return {word for word in normalize_key(value).split() if word not in _NOISE_WORDS}


def is_uri(value: str) -> bool:
    text = str(value or "").strip()
    if len(text) > 2_048 or any(ord(char) < 32 for char in text):
        return False
    if text[1:3] == ":\\" or text[1:3] == ":/":
        return False  # C:\... is a path, not a URI
    return bool(_URI_RE.match(text))


# ── cache ────────────────────────────────────────────────────────────────────

def _valid_cache(value: object) -> bool:
    if not isinstance(value, dict) or value.get("version") != INDEX_VERSION:
        return False
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        return False
    for item in entries:
        if not isinstance(item, dict):
            return False
        if item.get("kind") not in _KINDS:
            return False
        if not isinstance(item.get("name"), str) or not isinstance(item.get("target"), str):
            return False
        if len(item["name"]) > 200 or len(item["target"]) > 1_024:
            return False
    return True


def _store() -> JsonStore[dict]:
    return JsonStore(INDEX_FILE, dict, validator=_valid_cache)


def _read_cache() -> dict:
    if not INDEX_FILE.exists():
        return {}
    try:
        return _store().read()
    except (JsonStoreCorruptError, OSError, ValueError):
        return {}


def _source_directories() -> list[Path]:
    """Folders whose contents decide whether the cached index is still current."""
    directories: list[Path] = []
    if _OS == "Windows":
        for variable in ("ProgramData", "APPDATA"):
            base = os.environ.get(variable, "")
            if base:
                directories.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            directories.append(Path(local) / "Roblox" / "Versions")
    elif _OS == "Darwin":
        directories += [Path("/Applications"), Path.home() / "Applications"]
    else:
        data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/share:/usr/local/share").split(":")
        directories += [Path(item) / "applications" for item in data_dirs if item]
        directories.append(Path.home() / ".local" / "share" / "applications")
    return directories


def _source_signature() -> float:
    """Newest modification time across the discovery folders.

    Installing or removing an application rewrites its Start-menu folder or
    desktop entry, so this changes immediately.  It lets a fresh install show up
    without waiting out the 24-hour TTL and without a filesystem watcher.
    """
    newest = 0.0
    for directory in _source_directories():
        try:
            newest = max(newest, directory.stat().st_mtime)
            for child in directory.iterdir():
                if child.is_dir():
                    newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
    return round(newest, 3)


def _write_cache(entries: list[AppEntry]) -> None:
    payload = {
        "version": INDEX_VERSION,
        "system": _OS,
        "built_at": time.time(),
        "signature": _source_signature(),
        "entries": [entry.as_dict() for entry in entries[:MAX_ENTRIES]],
    }
    try:
        _store().write(payload)
    except Exception as exc:  # a cache miss is never fatal
        print(f"[app_index] could not persist the application index ({type(exc).__name__}).")


# ── Windows discovery ────────────────────────────────────────────────────────

def _windows_registry_entries() -> list[AppEntry]:
    entries: list[AppEntry] = []
    try:
        import winreg  # type: ignore
    except ImportError:
        return entries

    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_64KEY", 0)),
        (winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_32KEY", 0)),
        (winreg.HKEY_CURRENT_USER, 0),
    )
    for root, view in roots:
        try:
            handle = winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        try:
            for position in range(4_096):
                try:
                    child = winreg.EnumKey(handle, position)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(handle, child, 0, winreg.KEY_READ | view) as node:
                        target, _ = winreg.QueryValueEx(node, None)
                except OSError:
                    continue
                target = str(target or "").strip().strip('"')
                if not target:
                    continue
                try:
                    if not Path(target).is_file():
                        continue
                except OSError:
                    continue
                entries.append(AppEntry(
                    name=Path(child).stem,
                    kind=_KIND_EXEC,
                    target=target,
                    source="registry",
                ))
        finally:
            try:
                winreg.CloseKey(handle)
            except OSError:
                pass
    return entries


def _shortcut_metadata(path: Path) -> tuple[str, str]:
    """Return the executable and command-line arguments stored in a shortcut.

    Chrome/Edge installed web apps are ordinary ``.lnk`` files whose target is
    the browser and whose arguments contain the profile and application id. If
    those arguments are discarded, opening "YouTube" starts a normal browser
    window. The index therefore resolves only argument-free shortcuts and lets
    Windows launch parameterised shortcuts itself.
    """
    try:
        import pylnk3  # type: ignore
    except ImportError:
        return "", ""
    try:
        link = pylnk3.parse(str(path))
    except Exception:
        return "", ""

    arguments = ""
    for attribute in ("arguments", "_arguments", "command_line_arguments", "command_line_args"):
        value = str(getattr(link, attribute, "") or "").strip()
        if value:
            arguments = value
            break

    for attribute in ("path", "local_base_path"):
        candidate = str(getattr(link, attribute, "") or "").strip().strip('"')
        if not candidate or not candidate.casefold().endswith(".exe"):
            continue
        try:
            resolved = Path(os.path.expandvars(candidate))
            if resolved.is_file():
                return str(resolved), arguments
        except OSError:
            continue
    return "", arguments


def _resolve_shortcut_target(path: Path) -> tuple[str, str]:
    """Backward-compatible executable lookup used by tests and callers."""
    executable, _arguments = _shortcut_metadata(path)
    return executable, Path(executable).stem if executable else ""


def _windows_start_menu_entries() -> list[AppEntry]:
    entries: list[AppEntry] = []
    roots = []
    for variable in ("ProgramData", "APPDATA"):
        base = os.environ.get(variable, "")
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    scanned = 0
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*.lnk"):
                scanned += 1
                if scanned > MAX_SCAN_FILES:
                    return entries
                name = path.stem
                if not name or name.casefold().startswith(("uninstall", "remove ")):
                    continue
                executable, shortcut_arguments = _shortcut_metadata(path)
                if executable and not shortcut_arguments:
                    entries.append(AppEntry(
                        name=name,
                        kind=_KIND_EXEC,
                        target=executable,
                        source="startmenu",
                    ))
                else:
                    # Parameterised shortcuts must remain shortcuts. Chromium
                    # PWAs are tagged generically from their switches, not from
                    # a hard-coded site list, so Arena, Twitch, YouTube and any
                    # future installed web app follow the same path.
                    lowered_arguments = shortcut_arguments.casefold()
                    source = "webapp" if any(
                        switch in lowered_arguments
                        for switch in ("--app-id=", "--app=")
                    ) else "startmenu"
                    entries.append(AppEntry(
                        name=name,
                        kind=_KIND_LNK,
                        target=str(path),
                        source=source,
                    ))
        except OSError:
            continue
    return entries


def _windows_roblox_entries() -> list[AppEntry]:
    """Discover Roblox installations that do not register in App Paths.

    The current Roblox bootstrapper commonly installs a versioned executable
    below LOCALAPPDATA and registers the ``roblox-player`` URL protocol, but it
    does not always create an App Paths entry. Both sources are restricted to
    the expected executable name before they become launchable index entries.
    """
    entries: list[AppEntry] = []
    candidates: list[Path] = []

    try:
        import winreg  # type: ignore
    except ImportError:
        winreg = None

    if winreg is not None:
        subkey = r"Software\Classes\roblox-player\shell\open\command"
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as node:
                    command, _ = winreg.QueryValueEx(node, None)
            except OSError:
                continue
            text = os.path.expandvars(str(command or "").strip())
            match = re.match(r'^\s*"([^"]+\.exe)"|^\s*([^\s]+\.exe)', text, re.IGNORECASE)
            if match:
                candidates.append(Path(match.group(1) or match.group(2)))

    local = os.environ.get("LOCALAPPDATA", "")
    versions = Path(local) / "Roblox" / "Versions" if local else None
    if versions and versions.is_dir():
        try:
            candidates.extend(versions.glob("*/RobloxPlayerBeta.exe"))
        except OSError:
            pass

    valid: list[tuple[float, Path]] = []
    for candidate in candidates:
        try:
            if candidate.name.casefold() == "robloxplayerbeta.exe" and candidate.is_file():
                resolved = candidate.resolve()
                valid.append((resolved.stat().st_mtime, resolved))
        except OSError:
            continue
    if valid:
        # Version directory names are not reliably sortable. The newest binary
        # is the version the Roblox updater most recently installed.
        _modified, newest = max(valid, key=lambda item: item[0])
        entries.append(AppEntry("Roblox Player", _KIND_EXEC, str(newest), "roblox"))
    return entries


def _windows_store_entries() -> list[AppEntry]:
    """Store/UWP applications, listed through Get-StartApps (no extra packages)."""
    entries: list[AppEntry] = []
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return entries
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
            capture_output=True, text=True, timeout=25,
        )
    except Exception:
        return entries
    if completed.returncode != 0:
        return entries
    for line in (completed.stdout or "").splitlines()[:MAX_ENTRIES]:
        name, separator, app_id = line.partition("|")
        name, app_id = name.strip(), app_id.strip()
        if not separator or not name or not app_id or len(app_id) > 512:
            continue
        # Desktop apps are already covered by the registry and Start-menu scans;
        # only package AUMIDs need the AppsFolder launch path.
        if "!" not in app_id:
            continue
        entries.append(AppEntry(name=name, kind=_KIND_AUMID, target=app_id, source="appsfolder"))
    return entries


_WINDOWS_URI_ENTRIES = (
    AppEntry(name="Settings", kind=_KIND_URI, target="ms-settings:", source="builtin"),
    AppEntry(name="Windows Settings", kind=_KIND_URI, target="ms-settings:", source="builtin"),
)


def _scan_windows() -> list[AppEntry]:
    entries = list(_WINDOWS_URI_ENTRIES)
    entries += _windows_registry_entries()
    entries += _windows_start_menu_entries()
    entries += _windows_roblox_entries()
    entries += _windows_store_entries()
    return entries


# ── macOS discovery ──────────────────────────────────────────────────────────

def _scan_macos() -> list[AppEntry]:
    entries: list[AppEntry] = []
    roots = [
        Path("/Applications"),
        Path("/System/Applications"),
        Path("/System/Applications/Utilities"),
        Path.home() / "Applications",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in sorted(root.glob("*.app"))[:2_000]:
                entries.append(AppEntry(
                    name=path.stem,
                    kind=_KIND_BUNDLE,
                    target=str(path),
                    source="bundle",
                ))
        except OSError:
            continue
    return entries


# ── Linux discovery ──────────────────────────────────────────────────────────

def _parse_desktop_entry(path: Path) -> AppEntry | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > 64_000:
        return None
    name = ""
    command = ""
    in_entry = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_entry = stripped == "[Desktop Entry]"
            continue
        if not in_entry:
            continue
        if stripped.casefold().startswith("nodisplay=true"):
            return None
        if stripped.casefold().startswith("type=") and stripped.split("=", 1)[1].strip() != "Application":
            return None
        if not name and stripped.startswith("Name="):
            name = stripped.split("=", 1)[1].strip()
        if not command and stripped.startswith("Exec="):
            command = stripped.split("=", 1)[1].strip()
    if not name or not command:
        return None
    # Strip XDG field codes (%f, %U, …); they are placeholders, not arguments.
    words = [word for word in command.split() if not re.fullmatch(r"%[a-zA-Z]", word)]
    if not words:
        return None
    binary = shutil.which(words[0]) or (words[0] if Path(words[0]).is_file() else "")
    if not binary:
        return None
    return AppEntry(name=name, kind=_KIND_EXEC, target=binary, source="desktop")


def _scan_linux() -> list[AppEntry]:
    entries: list[AppEntry] = []
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/share:/usr/local/share").split(":")
    roots = [Path(item) / "applications" for item in data_dirs if item]
    roots.append(Path.home() / ".local" / "share" / "applications")
    scanned = 0
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in sorted(root.glob("*.desktop")):
                scanned += 1
                if scanned > 4_000:
                    break
                entry = _parse_desktop_entry(path)
                if entry is not None:
                    entries.append(entry)
        except OSError:
            continue
    return entries


_SCANNERS = {"Windows": _scan_windows, "Darwin": _scan_macos, "Linux": _scan_linux}


# ── index build / load ───────────────────────────────────────────────────────

def _deduplicate(entries: list[AppEntry]) -> list[AppEntry]:
    """Keep one entry per name, preferring the most directly launchable source."""
    rank = {
        "registry": 0, "builtin": 0, "bundle": 1, "desktop": 1,
        "webapp": 1, "startmenu": 2, "appsfolder": 3, "roblox": 1,
    }

    def _rank(item: AppEntry) -> int:
        # A Start-menu entry that pylnk3 resolved to a real executable is as
        # good as a registry hit, so it should not lose to an AppsFolder id.
        base = rank.get(item.source, 9)
        return base - 1 if item.source == "startmenu" and item.kind == _KIND_EXEC else base
    best: dict[str, AppEntry] = {}
    for entry in entries:
        key = entry.key
        if not key:
            continue
        current = best.get(key)
        if current is None or _rank(entry) < _rank(current):
            best[key] = entry
    return sorted(best.values(), key=lambda item: item.key)[:MAX_ENTRIES]


def build_index() -> list[AppEntry]:
    """Scan this machine for installed applications and refresh the cache."""
    scanner = _SCANNERS.get(_OS)
    if scanner is None:
        return []
    try:
        entries = _deduplicate(scanner())
    except Exception as exc:
        print(f"[app_index] application scan failed ({type(exc).__name__}).")
        return []
    _write_cache(entries)
    return entries


def load_index(*, refresh: bool = False) -> list[AppEntry]:
    """Return the cached index, rebuilding it when missing, stale, or forced."""
    if not refresh:
        cache = _read_cache()
        stale = (time.time() - float(cache.get("built_at") or 0)) > CACHE_TTL_SECONDS
        try:
            moved = float(cache.get("signature") or -1.0) != _source_signature()
        except Exception:
            moved = False
        stale = stale or moved
        same_system = cache.get("system") == _OS
        same_version = cache.get("version") == INDEX_VERSION
        raw = cache.get("entries") if isinstance(cache.get("entries"), list) else []
        if raw and same_system and same_version and not stale:
            return [
                AppEntry(
                    name=str(item.get("name", "")),
                    kind=str(item.get("kind", "")),
                    target=str(item.get("target", "")),
                    source=str(item.get("source", "")),
                )
                for item in raw
                if item.get("kind") in _KINDS
            ]
    return build_index()


# ── matching ─────────────────────────────────────────────────────────────────

def score_entry(query: str, entry: AppEntry) -> float:
    """Rank one entry against a request: exact, prefix, token, then fuzzy."""
    wanted = normalize_key(query)
    if not wanted:
        return 0.0
    key = entry.key
    if not key:
        return 0.0
    if key == wanted:
        return 100.0
    if key.startswith(wanted + " ") or key.endswith(" " + wanted):
        return 92.0
    wanted_terms = meaningful_terms(query)
    key_terms = set(key.split())
    if wanted_terms and wanted_terms <= key_terms:
        # Every requested word appears; prefer the entry with least extra noise.
        return 85.0 - min(10.0, len(key_terms - wanted_terms))
    score = _text_ratio(wanted, key)
    if score >= FUZZY_THRESHOLD:
        return 40.0 + score * 30.0
    return 0.0


def resolve(query: str, *, limit: int = 5, entries: list[AppEntry] | None = None) -> list[AppEntry]:
    """Best matching installed applications for a spoken or typed name."""
    pool = entries if entries is not None else load_index()
    scored = [(score_entry(query, entry), entry) for entry in pool]
    ranked = sorted(
        ((score, entry) for score, entry in scored if score > 0),
        key=lambda item: (-item[0], item[1].key),
    )
    if not ranked:
        return []
    # A single confident hit must not be diluted by weak fuzzy neighbours.
    top = ranked[0][0]
    if top >= 92.0:
        return [entry for score, entry in ranked if score >= 92.0][:limit]
    return [entry for _, entry in ranked][:limit]


# ── launching ────────────────────────────────────────────────────────────────

class LaunchError(RuntimeError):
    """Raised when an application could not be started."""


_SPAWNED: list[subprocess.Popen] = []


def _reap_spawned() -> None:
    """Collect finished children so a long session does not accumulate zombies."""
    alive = []
    for process in _SPAWNED:
        try:
            if process.poll() is None:
                alive.append(process)
        except Exception:
            continue
    _SPAWNED[:] = alive[-64:]


_MAX_ARGUMENTS = 8
_MAX_ARGUMENT_LENGTH = 2_048


def _is_existing_path(value: str) -> bool:
    """True when the text names a file or directory that exists right now."""
    try:
        return Path(value).expanduser().exists()
    except (OSError, ValueError):
        return False


def sanitise_arguments(arguments) -> list[str]:
    """Validate command-line arguments handed to a launched application.

    Arguments are passed as a real argv list, never through a shell, so quoting
    and metacharacters carry no meaning.  What still matters is that a model
    cannot smuggle switches in: anything starting with a dash is rejected, which
    keeps this to documents and URLs.  On Windows the switch character is the
    forward slash rather than the dash — ``/s``, ``/q``, ``/f`` — so a
    slash-prefixed argument is refused there too unless it names a file that
    actually exists, which is how a POSIX-style path reaches a cross-platform
    application.
    """
    if arguments in (None, "", []):
        return []
    if isinstance(arguments, str):
        arguments = [arguments]
    if not isinstance(arguments, (list, tuple)):
        raise ValueError("arguments must be a list of strings")
    if len(arguments) > _MAX_ARGUMENTS:
        raise ValueError(f"at most {_MAX_ARGUMENTS} arguments are allowed")
    cleaned = []
    for item in arguments:
        value = str(item or "").strip()
        if not value:
            continue
        if len(value) > _MAX_ARGUMENT_LENGTH:
            raise ValueError("an argument is too long")
        if any(ord(char) < 32 for char in value):
            raise ValueError("an argument contains control characters")
        if value.startswith("-"):
            raise ValueError(f"command-line switches are not allowed: {value[:32]}")
        if _OS == "Windows" and value.startswith("/") and not _is_existing_path(value):
            raise ValueError(f"command-line switches are not allowed: {value[:32]}")
        cleaned.append(value)
    return cleaned


def _spawn(argv: list[str]) -> int | None:
    _reap_spawned()
    process = subprocess.Popen(
        argv,
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=(_OS != "Windows"),
        start_new_session=(_OS != "Windows"),
    )
    _SPAWNED.append(process)
    return process.pid


def launch(entry: AppEntry, arguments=None) -> int | None:
    """Start an indexed application.  Returns the child pid when one is known.

    A ``None`` pid means the launch was delegated to the shell (Store app,
    ``.lnk``, URI, macOS ``open``) and the caller must verify by window rather
    than by process handle.  It never means failure — failure raises.
    """
    argv_extra = sanitise_arguments(arguments)

    if entry.kind == _KIND_EXEC:
        if not Path(entry.target).is_file():
            raise LaunchError(f"{entry.name} is no longer installed at its indexed location")
        return _spawn([entry.target, *argv_extra])

    if entry.kind == _KIND_BUNDLE:
        command = ["open", "-a", entry.target]
        if argv_extra:
            command += ["--args", *argv_extra]
        completed = subprocess.run(command, capture_output=True, timeout=15)
        if completed.returncode != 0:
            raise LaunchError(f"macOS refused to open {entry.name}")
        return None

    if entry.kind == _KIND_LNK:
        if not Path(entry.target).is_file():
            raise LaunchError(f"the shortcut for {entry.name} no longer exists")
        if argv_extra:
            # startfile cannot pass arguments to a shortcut; resolve the real
            # target through the shell verb instead of silently dropping them.
            raise LaunchError(
                f"{entry.name} is indexed as a Start-menu shortcut, which cannot "
                "receive arguments. Ask me to open the document directly instead."
            )
        os.startfile(entry.target)  # type: ignore[attr-defined]
        return None

    if entry.kind == _KIND_AUMID:
        if not re.fullmatch(r"[A-Za-z0-9_.\-+!{}\\ ]{1,512}", entry.target):
            raise LaunchError(f"{entry.name} has an unusable application id")
        if argv_extra:
            raise LaunchError(f"{entry.name} is a Store application and cannot take arguments")
        explorer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"
        return _spawn([str(explorer), f"shell:AppsFolder\\{entry.target}"])

    if entry.kind == _KIND_URI:
        if not is_uri(entry.target):
            raise LaunchError(f"{entry.name} has an unusable target")
        if _OS == "Windows":
            os.startfile(entry.target)  # type: ignore[attr-defined]
            return None
        opener = "open" if _OS == "Darwin" else "xdg-open"
        if not shutil.which(opener):
            raise LaunchError("no URI handler is available on this system")
        return _spawn([opener, entry.target])

    raise LaunchError(f"unsupported launch kind: {entry.kind}")


_DIRECT_LAUNCH_SUFFIXES = {".lnk", ".exe", ".app", ".desktop", ".url"}


def is_direct_launch_target(value: str) -> bool:
    """True when a spoken name or saved shortcut is itself a launchable path.

    Installed-application discovery only scans Start-menu, registry, and
    well-known program locations. A personal shortcut such as
    ``C:\\Users\\jonas\\OneDrive\\Desktop\\YouTube.lnk`` — remembered verbatim by
    ``shortcut_manager`` — lives on the Desktop, which is never scanned, so it
    would otherwise be reported as "not an installed application" even though
    it plainly exists. This is a narrow, explicit check: the text must resolve
    to an existing file or app bundle with a launchable suffix, not merely any
    path, so an ordinary document is still handled as an argument rather than
    as the application itself.
    """
    text = str(value or "").strip().strip('"').strip("'")
    if not text or is_uri(text):
        return False
    try:
        path = Path(text).expanduser()
    except (OSError, ValueError):
        return False
    if path.suffix.casefold() not in _DIRECT_LAUNCH_SUFFIXES:
        return False
    try:
        return path.exists()
    except OSError:
        return False


def launch_path(path: str, arguments=None) -> int | None:
    """Start a shortcut, executable, or app bundle directly from its path.

    Uses the same shell verb a double-click would use, so a ``.lnk``'s icon,
    working directory, and real target — all defined inside the shortcut
    itself — are respected instead of re-derived. Returns the child pid when
    one is known, the same contract as ``launch()``.
    """
    target = Path(str(path or "")).expanduser()
    if not target.exists():
        raise LaunchError(f"the shortcut target no longer exists: {target}")
    argv_extra = sanitise_arguments(arguments)
    suffix = target.suffix.casefold()

    if suffix == ".exe" and target.is_file():
        return _spawn([str(target), *argv_extra])

    if argv_extra:
        raise LaunchError(
            "this shortcut cannot receive extra arguments; ask me to open the "
            "document directly instead"
        )

    if _OS == "Windows":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return None
    if _OS == "Darwin":
        completed = subprocess.run(["open", str(target)], capture_output=True, timeout=15)
        if completed.returncode != 0:
            raise LaunchError(f"macOS refused to open {target.name}")
        return None
    opener = "xdg-open" if shutil.which("xdg-open") else None
    if opener is None:
        raise LaunchError("no file opener is available on this system")
    return _spawn([opener, str(target)])


def launch_uri(target: str) -> int | None:
    """Open a URI that is not part of the index (http://, ms-settings:, …)."""
    return launch(AppEntry(name=target, kind=_KIND_URI, target=target, source="direct"))


def describe(entries: list[AppEntry], limit: int = 5) -> str:
    return ", ".join(entry.name for entry in entries[:limit])


# ── usage: pinned and recently launched ──────────────────────────────────────
# The index answers "what is installed"; this answers "what do you actually
# open". A launcher that always starts from an empty search box makes the user
# retype the same six names every day.

def _valid_usage(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("pinned", "recent"):
        item = value.get(key, [])
        if not isinstance(item, list) or len(item) > MAX_USAGE_ENTRIES:
            return False
        if any(not isinstance(name, str) or len(name) > 200 for name in item):
            return False
    counts = value.get("counts", {})
    if not isinstance(counts, dict) or len(counts) > MAX_USAGE_ENTRIES:
        return False
    for name, total in counts.items():
        if not isinstance(name, str) or len(name) > 200:
            return False
        if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 1_000_000:
            return False
    return True


def _usage_store() -> JsonStore[dict]:
    return JsonStore(USAGE_FILE, dict, validator=_valid_usage)


def read_usage() -> dict:
    if not USAGE_FILE.exists():
        return {"pinned": [], "recent": [], "counts": {}}
    try:
        data = _usage_store().read()
    except (JsonStoreCorruptError, OSError, ValueError):
        return {"pinned": [], "recent": [], "counts": {}}
    return {
        "pinned": list(data.get("pinned") or [])[:MAX_PINNED],
        "recent": list(data.get("recent") or [])[:MAX_RECENT],
        "counts": dict(data.get("counts") or {}),
    }


def _write_usage(data: dict) -> None:
    pinned = list(data.get("pinned") or [])[:MAX_PINNED]
    recent = list(data.get("recent") or [])[:MAX_RECENT]
    # The count table is capped, and once it is full a plain "highest count
    # first" cut would evict every newly used application before its second
    # launch — the table would freeze forever. Anything pinned or recently used
    # is therefore kept regardless of its count.
    protected = {name.casefold() for name in pinned + recent}

    def _order(item: tuple[str, object]) -> tuple[int, int]:
        name, total = item
        return (0 if name.casefold() in protected else 1, -int(total))

    try:
        _usage_store().write({
            "pinned": pinned,
            "recent": recent,
            "counts": {
                name: int(total)
                for name, total in sorted(
                    (data.get("counts") or {}).items(), key=_order
                )[:MAX_USAGE_ENTRIES]
            },
        })
    except Exception as exc:
        print(f"[app_index] could not persist launcher usage ({type(exc).__name__}).")


def record_launch(name: str) -> None:
    """Remember that an application was opened, for the recent list."""
    clean = str(name or "").strip()[:200]
    if not clean:
        return
    data = read_usage()
    recent = [item for item in data["recent"] if item.casefold() != clean.casefold()]
    recent.insert(0, clean)
    data["recent"] = recent[:MAX_RECENT]
    # Count under the name already stored, so "Chrome" and "chrome" are one
    # application rather than two half-counted ones.
    counts = data["counts"]
    existing = next((key for key in counts if key.casefold() == clean.casefold()), clean)
    counts[existing] = int(counts.get(existing, 0)) + 1
    _write_usage(data)


def set_pinned(name: str, pinned: bool) -> list[str]:
    """Pin or unpin an application in the launcher. Returns the new pin list."""
    clean = str(name or "").strip()[:200]
    if not clean:
        raise ValueError("an application name is required")
    data = read_usage()
    pins = [item for item in data["pinned"] if item.casefold() != clean.casefold()]
    if pinned:
        if len(pins) >= MAX_PINNED:
            raise ValueError(f"you can pin at most {MAX_PINNED} applications")
        pins.append(clean)
    data["pinned"] = pins
    _write_usage(data)
    return pins


def quick_list(entries: list[AppEntry] | None = None) -> dict:
    """Pinned first, then most recent, resolved against the live index.

    Entries that no longer resolve are dropped rather than offered as dead
    buttons, which is what happens after an application is uninstalled.
    """
    pool = entries if entries is not None else load_index()
    known = {entry.key: entry for entry in pool}
    usage = read_usage()

    def _resolved(names: list[str]) -> list[AppEntry]:
        found = []
        seen = set()
        for name in names:
            entry = known.get(normalize_key(name))
            if entry is None:
                matches = resolve(name, limit=1, entries=pool)
                entry = matches[0] if matches else None
            if entry is not None and entry.key not in seen:
                seen.add(entry.key)
                found.append(entry)
        return found

    pinned = _resolved(usage["pinned"])
    pinned_keys = {entry.key for entry in pinned}
    recent = [entry for entry in _resolved(usage["recent"]) if entry.key not in pinned_keys]
    return {"pinned": pinned, "recent": recent[:MAX_RECENT]}
