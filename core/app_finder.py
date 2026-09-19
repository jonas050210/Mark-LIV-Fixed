"""
Local application discovery, alias resolution, and launching.

Shared by the ``open_app`` action (launching) and the ``app_inventory`` action
(inspecting). One module on purpose: the thing that answers "do I have X?" and
the thing that opens X must agree, or the assistant claims an app exists that it
cannot start — or refuses to start one it just listed.

Windows sources, in resolution order:
  1. validated http(s) URLs                        → default browser
  2. allow-listed bare protocols (``ms-settings:``) → shell open
  3. executables on PATH                            → direct Popen, no shell
  4. ``App Paths`` registry keys                    → direct Popen, no shell
  5. Start Menu shortcuts (user + all-users)        → shell open of the .lnk
  6. known Microsoft-Store AUMIDs                   → shell open via AppsFolder
  7. installed Steam games                          → validated steam:// URL
  8. installed Epic games                           → validated Epic URL
  9. Roblox Player install                          → direct Popen, no shell

macOS falls back to /Applications (+ ~/Applications) and ``open -a``; Linux to
``.desktop`` entries, ``gtk-launch`` and PATH. Nothing here ever downloads or
installs anything: a missing app is a message, not a download.

Security boundaries:
  - model text NEVER reaches a shell (no shell=True anywhere in this module);
  - ``os.startfile``/``webbrowser`` only ever receive validated targets: paths
    that exist on disk, or URIs matched by strict allow-list regexes;
  - ``steam://`` / Epic URLs are only ever *constructed* from numeric IDs and
    manifest data found locally, never taken from model text.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

_SYSTEM = platform.system()
_IS_WIN = (_SYSTEM == "Windows")
_IS_MAC = (_SYSTEM == "Darwin")


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE = _base_dir()
_USER_ALIAS_FILE = _BASE / "config" / "app_aliases.json"

# Persistent cache. Same file open_app has always used, so an existing install
# keeps its entries; old flat {name: path} values are migrated on load.
_APP_CACHE_FILE = Path.home() / ".jarvis_app_cache.json"
_CACHE_VERSION = 2
_INVENTORY_TTL = 24 * 3600      # full rescan at most once a day per process run
_USER_CACHE_TTL = 30 * 24 * 3600  # resolved targets revalidated by existence anyway


# ── Launch targets ────────────────────────────────────────────────────────────

@dataclass
class LaunchTarget:
    kind: str        # exe | shortcut | uwp | steam | epic | roblox | url | protocol
    display: str     # human name ("Google Chrome", "Geometry Dash")
    target: str      # absolute path or validated URI/URL
    source: str = ""  # where it was found, for messages ("Start Menu", "PATH", ...)
    args: tuple = ()  # trailing CLI args ("libreoffice --writer"), argv-appended, never shelled


# ── Alias table ───────────────────────────────────────────────────────────────
# alias (lowercase) -> canonical display name. The canonical name is what the
# sources above are searched for, so "gd" finds a "Geometry Dash" shortcut, a
# Steam entry, or an App-Paths key without caring which one exists.
# New aliases NEVER touch dispatch logic: add a line here (or a user alias in
# config/app_aliases.json) and both open_app and app_inventory pick it up.

_BASE_ALIASES: dict[str, str] = {
    # browsers
    "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "firefox": "Firefox", "mozilla firefox": "Firefox",
    "edge": "Microsoft Edge", "microsoft edge": "Microsoft Edge",
    "brave": "Brave", "brave browser": "Brave",
    "opera": "Opera", "opera browser": "Opera",
    "safari": "Safari", "arc": "Arc",
    # chat / social / meetings
    "whatsapp": "WhatsApp", "wa": "WhatsApp",
    "telegram": "Telegram", "tg": "Telegram",
    "discord": "Discord", "dc": "Discord",
    "slack": "Slack", "zoom": "Zoom", "zoom us": "Zoom",
    "teams": "Microsoft Teams", "microsoft teams": "Microsoft Teams",
    "skype": "Skype", "signal": "Signal",
    "instagram": "Instagram", "insta": "Instagram", "ig": "Instagram",
    "tiktok": "TikTok",
    # media
    "spotify": "Spotify",
    "vlc": "VLC media player", "vlc media player": "VLC media player",
    "netflix": "Netflix",
    "youtube": "YouTube", "yt": "YouTube",
    "twitch": "Twitch",
    # editors / dev
    "vscode": "Visual Studio Code", "visual studio code": "Visual Studio Code",
    "code": "Visual Studio Code", "vs code": "Visual Studio Code",
    "visual studio": "Visual Studio",
    "notepad++": "Notepad++", "notepad plus plus": "Notepad++", "npp": "Notepad++",
    "notepad": "Notepad",
    "sublime": "Sublime Text", "sublime text": "Sublime Text",
    "pycharm": "PyCharm", "intellij": "IntelliJ IDEA",
    "postman": "Postman", "figma": "Figma", "blender": "Blender",
    "git": "Git Bash", "git bash": "Git Bash",
    "docker": "Docker Desktop", "docker desktop": "Docker Desktop",
    # office / notes
    "word": "Microsoft Word", "microsoft word": "Microsoft Word",
    "excel": "Microsoft Excel", "microsoft excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint", "microsoft powerpoint": "Microsoft PowerPoint",
    "ppt": "Microsoft PowerPoint",
    "libreoffice": "LibreOffice", "libre office": "LibreOffice",
    "writer": "LibreOffice Writer", "calc": "LibreOffice Calc",
    "notion": "Notion", "obsidian": "Obsidian", "onenote": "OneNote",
    "evernote": "Evernote",
    # system
    "terminal": "Terminal", "cmd": "Command Prompt", "command prompt": "Command Prompt",
    "powershell": "Windows PowerShell", "windows powershell": "Windows PowerShell",
    "windows terminal": "Windows Terminal",
    "explorer": "File Explorer", "file explorer": "File Explorer",
    "task manager": "Task Manager", "taskmanager": "Task Manager",
    "settings": "Settings", "windows settings": "Settings",
    "calculator": "Calculator", "calc": "Calculator",
    "paint": "Paint", "mspaint": "Paint",
    "snipping tool": "Snipping Tool", "snippingtool": "Snipping Tool",
    "control panel": "Control Panel", "registry editor": "Registry Editor",
    "regedit": "Registry Editor",
    # launchers / stores
    "steam": "Steam",
    "epic": "Epic Games Launcher", "epic games": "Epic Games Launcher",
    "epic games launcher": "Epic Games Launcher",
    "gog": "GOG Galaxy", "gog galaxy": "GOG Galaxy",
    "ubisoft": "Ubisoft Connect", "ubisoft connect": "Ubisoft Connect", "uplay": "Ubisoft Connect",
    "ea": "EA app", "ea app": "EA app", "origin": "EA app",
    "battlenet": "Battle.net", "battle.net": "Battle.net",
    "xbox": "Xbox", "microsoft store": "Microsoft Store",
    # games — canonical names match Steam / shortcut display names
    "gd": "Geometry Dash", "geometry dash": "Geometry Dash",
    "geometry dash lite": "Geometry Dash",
    "roblox": "Roblox Player", "roblox player": "Roblox Player",
    "roblox studio": "Roblox Studio",
    "minecraft": "Minecraft Launcher", "minecraft launcher": "Minecraft Launcher",
    "mc": "Minecraft Launcher",
    "ksp": "Kerbal Space Program", "kerbal space program": "Kerbal Space Program",
    "ksp2": "Kerbal Space Program 2", "kerbal space program 2": "Kerbal Space Program 2",
    "kerbal": "Kerbal Space Program",
    "gta5": "Grand Theft Auto V", "gta v": "Grand Theft Auto V",
    "gta": "Grand Theft Auto V",
    "fortnite": "Fortnite", "fn": "Fortnite",
    "valorant": "Valorant", "valo": "Valorant",
    "lol": "League of Legends", "league of legends": "League of Legends",
    "league": "League of Legends",
    "cs2": "Counter-Strike 2", "csgo": "Counter-Strike 2",
    "counter strike": "Counter-Strike 2", "counter-strike 2": "Counter-Strike 2",
    "apex": "Apex Legends", "apex legends": "Apex Legends",
    "overwatch": "Overwatch 2", "overwatch 2": "Overwatch 2", "ow2": "Overwatch 2",
    "ow": "Overwatch 2",
    "rocket league": "Rocket League", "rl": "Rocket League",
    "fall guys": "Fall Guys", "among us": "Among Us",
    "warzone": "Call of Duty", "cod": "Call of Duty", "call of duty": "Call of Duty",
    "war thunder": "War Thunder", "warthunder": "War Thunder", "wt": "War Thunder",
    "world of tanks": "World of Tanks", "wot": "World of Tanks",
    "genshin": "Genshin Impact", "genshin impact": "Genshin Impact",
    "honkai": "Honkai: Star Rail",
    "stardew": "Stardew Valley", "stardew valley": "Stardew Valley",
    "terraria": "Terraria", "rust": "Rust", "ark": "ARK: Survival Evolved",
    "dayz": "DayZ", "arma": "Arma 3", "arma 3": "Arma 3",
    "squad": "Squad", "hell let loose": "Hell Let Loose",
    "pubg": "PUBG: Battlegrounds", "battlegrounds": "PUBG: Battlegrounds",
    "elden ring": "ELDEN RING", "dark souls": "DARK SOULS III",
    "cyberpunk": "Cyberpunk 2077", "cyberpunk 2077": "Cyberpunk 2077",
    "witcher": "The Witcher 3: Wild Hunt", "witcher 3": "The Witcher 3: Wild Hunt",
    "rdr2": "Red Dead Redemption 2", "red dead": "Red Dead Redemption 2",
    "skyrim": "The Elder Scrolls V: Skyrim",
    "fallout": "Fallout 4", "fallout 4": "Fallout 4", "fallout 76": "Fallout 76",
    "doom": "DOOM Eternal", "doom eternal": "DOOM Eternal",
    "halo": "Halo Infinite", "halo infinite": "Halo Infinite",
    "forza": "Forza Horizon 5", "forza horizon": "Forza Horizon 5",
    "fifa": "EA SPORTS FC 24", "fc24": "EA SPORTS FC 24", "eafc": "EA SPORTS FC 24",
    "nba": "NBA 2K24",
    "sims": "The Sims 4", "sims 4": "The Sims 4",
    "cities skylines": "Cities: Skylines",
    "civilization": "Sid Meier's Civilization VI", "civ6": "Sid Meier's Civilization VI",
    "civ": "Sid Meier's Civilization VI",
    "total war": "Total War: WARHAMMER III",
    "age of empires": "Age of Empires IV", "aoe": "Age of Empires IV", "aoe4": "Age of Empires IV",
    "starcraft": "StarCraft II", "warcraft": "Warcraft III",
    "diablo": "Diablo IV", "diablo 4": "Diablo IV",
    "destiny": "Destiny 2", "destiny 2": "Destiny 2",
    "warframe": "Warframe", "dota": "Dota 2", "dota 2": "Dota 2",
    "team fortress": "Team Fortress 2", "tf2": "Team Fortress 2",
    "left 4 dead": "Left 4 Dead 2", "l4d2": "Left 4 Dead 2",
    "portal": "Portal 2", "portal 2": "Portal 2",
    "half life": "Half-Life: Alyx", "half-life": "Half-Life: Alyx",
    "hollow knight": "Hollow Knight", "celeste": "Celeste",
    "hades": "Hades", "baldurs gate": "Baldur's Gate 3", "bg3": "Baldur's Gate 3",
    "palworld": "Palworld", "lethal company": "Lethal Company",
    "sons of the forest": "Sons Of The Forest",
    "euro truck": "Euro Truck Simulator 2", "ets2": "Euro Truck Simulator 2",
    "farming simulator": "Farming Simulator 22", "fs22": "Farming Simulator 22",
    "msfs": "Microsoft Flight Simulator", "flight simulator": "Microsoft Flight Simulator",
    "assetto corsa": "Assetto Corsa", "iracing": "iRacing",
    "payday": "PAYDAY 3", "payday 3": "PAYDAY 3", "payday 2": "PAYDAY 2",
    "rainbow six": "Tom Clancy's Rainbow Six Siege", "r6": "Tom Clancy's Rainbow Six Siege",
    "siege": "Tom Clancy's Rainbow Six Siege",
    "smite": "SMITE", "paladins": "Paladins",
    "lost ark": "Lost Ark", "new world": "New World: Aeternum",
    "path of exile": "Path of Exile", "poe": "Path of Exile", "poe2": "Path of Exile 2",
    "war thunder mobile": "War Thunder",
    "dead by daylight": "Dead by Daylight", "dbd": "Dead by Daylight",
    "phasmophobia": "Phasmophobia", "phasmo": "Phasmophobia",
    "raft": "Raft", "subnautica": "Subnautica",
    "satisfactory": "Satisfactory", "factorio": "Factorio", "rimworld": "RimWorld",
    "oxygen not included": "Oxygen Not Included",
    "kerbal space program 2": "Kerbal Space Program 2",
    "cities skylines 2": "Cities: Skylines II",
    "goose goose duck": "Goose Goose Duck", "ggd": "Goose Goose Duck",
    # streaming / creative
    "obs": "OBS Studio", "obs studio": "OBS Studio",
    "streamlabs": "Streamlabs", "capcut": "CapCut",
    "photoshop": "Adobe Photoshop", "adobe photoshop": "Adobe Photoshop", "ps": "Adobe Photoshop",
    "premiere": "Adobe Premiere Pro", "lightroom": "Adobe Lightroom",
    "audacity": "Audacity", "gimp": "GIMP",
    "handbrake": "HandBrake", "vlc media player": "VLC media player",
    "media player": "Media Player",
    # misc
    "thunderbird": "Thunderbird", "outlook": "Outlook",
    "kindle": "Kindle", "calibre": "calibre",
    "qbittorrent": "qBittorrent", "utorrent": "uTorrent",
    "7zip": "7-Zip", "7-zip": "7-Zip", "winrar": "WinRAR",
    "everything": "Everything", "powertoys": "PowerToys",
}

# executable-name hints per canonical name: what the binary is usually called.
# Used for PATH/App-Paths lookup when no shortcut exists. Keys are canonical
# display names (lowercased); values tried in order via shutil.which().
_EXE_HINTS: dict[str, list[str]] = {
    "google chrome": ["chrome"],
    "firefox": ["firefox"], "microsoft edge": ["msedge"], "brave": ["brave"],
    "opera": ["opera"], "arc": ["arc"],
    "whatsapp": ["whatsapp"], "telegram": ["telegram"], "discord": ["discord"],
    "discord": ["discord"], "slack": ["slack"], "zoom": ["zoom"],
    "microsoft teams": ["msteams"], "skype": ["skype"], "signal": ["signal"],
    "spotify": ["spotify"], "vlc media player": ["vlc"],
    "visual studio code": ["code", "code-insiders"],
    "notepad++": ["notepad++"], "notepad": ["notepad"],
    "sublime text": ["subl", "sublime_text"],
    "terminal": ["wt", "windowsterminal"], "command prompt": ["cmd"],
    "windows powershell": ["powershell"], "windows terminal": ["wt"],
    "file explorer": ["explorer"], "task manager": ["taskmgr"],
    "calculator": ["calc"], "paint": ["mspaint"],
    "snipping tool": ["snippingtool"], "registry editor": ["regedit"],
    "microsoft word": ["winword"], "microsoft excel": ["excel"],
    "microsoft powerpoint": ["powerpnt"], "libreoffice": ["soffice"],
    "steam": ["steam"], "epic games launcher": ["epicgameslauncher"],
    "gog galaxy": ["galaxyclient"], "ubisoft connect": ["ubisoftconnect"],
    "ea app": ["eaapp", "eadestktop"], "battle.net": ["battle.net"],
    "obs studio": ["obs64", "obs"], "audacity": ["audacity"],
    "git bash": ["git-bash"], "docker desktop": ["docker desktop"],
    "7-zip": ["7z", "7zfm"], "winrar": ["winrar"],
    "everything": ["everything"], "powertoys": ["powertoys"],
    "postman": ["postman"], "figma": ["figma"], "blender": ["blender"],
    "notion": ["notion"], "obsidian": ["obsidian"], "capcut": ["capcut"],
    "thunderbird": ["thunderbird"], "qbittorrent": ["qbittorrent"],
    "utorrent": ["utorrent"], "kindle": ["kindle"], "calibre": ["calibre"],
    "outlook": ["outlook"], "onenote": ["onenote"],
    "minecraft launcher": ["minecraft", "minecraftlauncher"],
    "gimp": ["gimp"], "handbrake": ["handbrake"],
}

#: URI schemes the Roblox website itself uses to start the player. Only ever
#: handed to the launcher for an already-resolved Roblox target (see
#: launch_alternatives) — never taken from model text.
_ROBLOX_PROTOCOLS = ("roblox-player:", "roblox:")

# Bare protocols the model may name. Anchored single-token schemes only — no
# "://", no payload, no ticket — so nothing can be smuggled after the colon.
_PROTOCOL_ALLOWLIST = frozenset({"ms-settings:", *_ROBLOX_PROTOCOLS})

# Strict shapes for launcher URLs we CONSTRUCT ourselves (never from model text).
_STEAM_URL_RE = re.compile(r"^steam://rungameid/(\d{1,12})$")
_EPIC_URL_RE = re.compile(r"^com\.epicgames\.launcher://apps/([A-Za-z0-9]+)\?action=launch&silent=true$")
_APPSFOLDER_RE = re.compile(r"^shell:AppsFolder\\[A-Za-z0-9._]+![A-Za-z0-9._]+$")
_URL_RE = re.compile(r"^https?://[^/\s]+", re.IGNORECASE)

# Microsoft-Store apps with no classic exe. Family names embed the publisher-ID
# hash and are stable across versions; if the app is not installed the shell
# open simply fails and we report that — nothing is downloaded.
_UWP_AUMIDS: dict[str, str] = {
    "netflix": r"4DF9E0F8.Netflix_mcm4njqhnhss8!Netflix.App",
    "instagram": r"Facebook.Instagram_beta8jmt7jjc0p6!App",
    "tiktok": r"BytedancePte.Ltd.TikTok__6yccndn606msg!TikTok",
    "spotify": r"SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify",
}

# Steam AppIDs for alias-known games (launch only when the manifest proves the
# game is installed — see resolve(); never installs anything).
_STEAM_APPIDS: dict[str, str] = {
    "geometry dash": "322170",
    "kerbal space program": "220200",
    "kerbal space program 2": "954850",
    "grand theft auto v": "271590",
    "counter-strike 2": "730",
    "dota 2": "570",
    "team fortress 2": "440",
    "left 4 dead 2": "550",
    "portal 2": "620",
    "half-life: alyx": "546560",
    "pubg: battlegrounds": "578080",
    "apex legends": "1172470",
    "destiny 2": "1085660",
    "warframe": "230410",
    "rocket league": "252950",
    "fall guys": "1097150",
    "among us": "945360",
    "elden ring": "1245620",
    "cyberpunk 2077": "1091500",
    "the witcher 3: wild hunt": "292030",
    "red dead redemption 2": "1174180",
    "hades": "1145360",
    "hollow knight": "367520",
    "celeste": "504230",
    "stardew valley": "413150",
    "terraria": "105600",
    "factorio": "427520",
    "satisfactory": "526870",
    "rimworld": "294100",
    "subnautica": "264710",
    "raft": "648800",
    "phasmophobia": "739630",
    "lethal company": "1966720",
    "palworld": "1623730",
    "baldur's gate 3": "1086940",
    "euro truck simulator 2": "227300",
    "farming simulator 22": "1248130",
    "cities: skylines": "255710",
    "cities: skylines ii": "949230",
    "sid meier's civilization vi": "289070",
    "total war: warhammer iii": "1142710",
    "age of empires iv": "1466860",
    "overwatch 2": "2357570",
    "dead by daylight": "381210",
    "payday 2": "218620",
    "payday 3": "1272080",
    "tom clancy's rainbow six siege": "359550",
    "smite": "386360",
    "paladins": "444090",
    "lost ark": "1599340",
    "path of exile": "238960",
    "path of exile 2": "2694490",
    "goose goose duck": "1568590",
    "sons of the forest": "1326470",
    "microsoft flight simulator": "1250410",
    "assetto corsa": "244210",
    "doom eternal": "782330",
    "fallout 4": "377160",
    "the elder scrolls v: skyrim": "489830",
    "dark souls iii": "374320",
    "war thunder": "236390",
    "hello neighbor": "521890",
}


def register_alias(alias: str, canonical: str) -> None:
    """Add (or override) an alias at runtime. No dispatch code to touch."""
    alias, canonical = (alias or "").strip().lower(), (canonical or "").strip()
    if alias and canonical:
        _BASE_ALIASES[alias] = canonical


def _load_user_aliases() -> dict[str, str]:
    try:
        raw = _USER_ALIAS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[AppFinder] ⚠️ Could not parse {_USER_ALIAS_FILE.name}: {e}")
        return {}
    aliases = data.get("aliases", data if isinstance(data, dict) else {})
    out: dict[str, str] = {}
    if isinstance(aliases, dict):
        for k, v in aliases.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                out[k.strip().lower()] = v.strip()
    return out


def canonical_name(raw: str) -> str:
    """Alias → canonical display name. Unknown input comes back cleaned but unchanged."""
    key = (raw or "").strip().lower()
    if not key:
        return ""
    if key in _BASE_ALIASES:
        return _BASE_ALIASES[key]
    user = _load_user_aliases()
    if key in user:
        return user[key]
    # "open the chrome browser" → strip filler words, then retry
    stripped = re.sub(r"\b(the|app|application|program|browser|game|please|open|launch|start)\b", "", key)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped and stripped != key:
        if stripped in _BASE_ALIASES:
            return _BASE_ALIASES[stripped]
        if stripped in user:
            return user[stripped]
    return (raw or "").strip()


# ── Persistent cache ──────────────────────────────────────────────────────────

_CACHE: dict = {}
_CACHE_LOADED = False


def _load_cache() -> dict:
    global _CACHE, _CACHE_LOADED
    if _CACHE_LOADED:
        return _CACHE
    _CACHE = {"version": _CACHE_VERSION, "targets": {}, "shortcuts": {},
              "shortcuts_ts": 0.0, "inventory_ts": 0.0}
    if _APP_CACHE_FILE.exists():
        try:
            raw = json.loads(_APP_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("version") == _CACHE_VERSION:
                _CACHE.update(raw)
            elif isinstance(raw, dict):
                # Migrate the legacy flat {name: path} cache (open_app ≤ v1).
                for k, v in raw.items():
                    if isinstance(v, str) and v:
                        _CACHE["targets"][str(k)] = {"target": v, "kind": "exe",
                                                     "source": "legacy", "ts": 0.0}
        except Exception:
            pass
    _CACHE_LOADED = True
    return _CACHE


def _save_cache() -> None:
    try:
        _APP_CACHE_FILE.write_text(json.dumps(_load_cache()), encoding="utf-8")
    except Exception:
        pass


def _cached_target(key: str) -> LaunchTarget | None:
    """A cached target is only returned when it still validates (stale → dropped)."""
    cache = _load_cache()
    entry = cache.get("targets", {}).get(key)
    if not isinstance(entry, dict):
        return None
    target, kind = entry.get("target", ""), entry.get("kind", "")
    if not target:
        cache["targets"].pop(key, None)
        return None
    if kind in ("exe", "shortcut", "roblox"):
        if not Path(target).exists():
            cache["targets"].pop(key, None)  # stale — app moved or uninstalled
            _save_cache()
            return None
    return LaunchTarget(kind=kind, display=entry.get("display", key),
                        target=target, source=entry.get("source", "cache"))


def _store_target(key: str, tgt: LaunchTarget) -> None:
    cache = _load_cache()
    cache.setdefault("targets", {})[key] = {
        "target": tgt.target, "kind": tgt.kind, "display": tgt.display,
        "source": tgt.source, "ts": time.time(),
    }
    _save_cache()


# ── Windows sources ───────────────────────────────────────────────────────────

def _reg_app_paths(name: str) -> str | None:
    """HKLM/HKCU ...\\App Paths\\<name>.exe → install path. Name-matched only."""
    if not _IS_WIN:
        return None
    try:
        import winreg
    except ImportError:
        return None
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    needles = {name.lower(), name.lower() + ".exe"}
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, base) as hk:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(hk, i)
                    except OSError:
                        break
                    i += 1
                    if sub.lower() in needles:
                        try:
                            with winreg.OpenKey(hk, sub) as appk:
                                val, _ = winreg.QueryValueEx(appk, "")
                                if val and Path(str(val)).exists():
                                    return str(val)
                        except OSError:
                            continue
        except OSError:
            continue
    return None


def _start_menu_roots() -> list[Path]:
    if not _IS_WIN:
        return []
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    return [r for r in roots if r.is_dir()]


def _scan_shortcuts() -> dict[str, str]:
    """display-name.lower() → .lnk path. Cached on disk for 24 h (TTL), in-RAM after."""
    cache = _load_cache()
    now = time.time()
    if cache.get("shortcuts") and (now - float(cache.get("shortcuts_ts", 0))) < _INVENTORY_TTL:
        return dict(cache["shortcuts"])
    found: dict[str, str] = {}
    for root in _start_menu_roots():
        try:
            for p in root.rglob("*.lnk"):
                try:
                    key = p.stem.lower().strip()
                except Exception:
                    continue
                if key and key not in found:
                    found[key] = str(p)
        except Exception:
            continue
    cache["shortcuts"] = found
    cache["shortcuts_ts"] = now
    _save_cache()
    return found


def rescan_shortcuts() -> int:
    """Force a fresh Start Menu index (used by app_inventory rescan)."""
    cache = _load_cache()
    cache["shortcuts"] = {}
    cache["shortcuts_ts"] = 0.0
    n = len(_scan_shortcuts())
    return n


def _match_shortcut(canonical: str) -> tuple[str, str] | None:
    """(display, lnk) for exact / normalized / startswith match. No fuzzy guessing."""
    want = canonical.lower().strip()
    if not want:
        return None
    shortcuts = _scan_shortcuts()
    if want in shortcuts:
        return canonical, shortcuts[want]
    # "VLC media player" vs shortcut "VLC media player" etc. — normalize spaces/dashes
    norm = re.sub(r"[\s\-_.]+", " ", want).strip()
    for key, path in shortcuts.items():
        if re.sub(r"[\s\-_.]+", " ", key).strip() == norm:
            return Path(path).stem, path
    for key, path in shortcuts.items():
        if key == want or key.startswith(want + " ") or want.startswith(key + " "):
            return Path(path).stem, path
    # word-prefix: "Geometry Dash" shortcut vs canonical "Geometry Dash"
    for key, path in shortcuts.items():
        if key.startswith(want) or want.startswith(key):
            if abs(len(key) - len(want)) <= 12:
                return Path(path).stem, path
    return None


def parse_lnk_target(lnk_path: str) -> str | None:
    """Best-effort .lnk → target path, pure Python (no new dependency).

    Reads LinkInfo's LocalBasePath when present. Returns None on any parse
    failure — callers fall back to opening the .lnk itself, which always works.
    """
    try:
        data = Path(lnk_path).read_bytes()
    except Exception:
        return None
    try:
        if len(data) < 76 or data[0:4] != b"L\x00\x00\x00":
            return None
        flags, = struct.unpack_from("<I", data, 20)
        pos = 76
        if flags & 0x01:  # HasLinkTargetIDList
            if pos + 2 > len(data):
                return None
            (size,) = struct.unpack_from("<H", data, pos)
            pos += 2 + size
        if not (flags & 0x02):  # HasLinkInfo
            return None
        if pos + 28 > len(data):
            return None
        (info_size, _info_hdr, info_flags, _vol_off, local_off, _net_off) = \
            struct.unpack_from("<IIIIII", data, pos)
        if info_size < 28 or pos + info_size > len(data):
            return None
        if info_flags & 0x01 and local_off:  # VolumeIDAndLocalBasePath
            start = pos + local_off
            end = data.index(b"\x00", start, pos + info_size)
            local = data[start:end].decode("mbcs" if _IS_WIN else "utf-8", errors="ignore")
            if local and Path(local).exists():
                return local
        return None
    except Exception:
        return None


def _uninstall_entries() -> list[dict]:
    """DisplayName + InstallLocation from the Uninstall registry (inventory-grade)."""
    out: list[dict] = []
    if not _IS_WIN:
        return out
    try:
        import winreg
    except ImportError:
        return out
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, base) as hk:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(hk, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(hk, sub) as appk:
                            try:
                                name, _ = winreg.QueryValueEx(appk, "DisplayName")
                            except OSError:
                                continue
                            if not name:
                                continue
                            try:
                                loc, _ = winreg.QueryValueEx(appk, "InstallLocation")
                            except OSError:
                                loc = ""
                            try:
                                ver, _ = winreg.QueryValueEx(appk, "DisplayVersion")
                            except OSError:
                                ver = ""
                            uninstall = ""
                            for value in ("QuietUninstallString",
                                          "UninstallString"):
                                try:
                                    raw, _ = winreg.QueryValueEx(appk, value)
                                except OSError:
                                    continue
                                if raw:
                                    uninstall = str(raw)
                                    break
                            try:
                                pub, _ = winreg.QueryValueEx(appk, "Publisher")
                            except OSError:
                                pub = ""
                            out.append({"name": str(name), "location": str(loc or ""),
                                        "version": str(ver or ""),
                                        "uninstall": uninstall,
                                        "publisher": str(pub or "")})
                    except OSError:
                        continue
        except OSError:
            continue
    return out


# ── Uninstallers ──────────────────────────────────────────────────────────────
# "Remove X" may only ever run a *vendor-registered* uninstaller: the command
# comes out of the Windows Uninstall registry (the same source the Control Panel
# uses), out of an .app bundle on macOS, or — when neither exists — nothing
# happens and the refusal says so. The parsed command is shown to the user in
# the confirmation banner before anything runs, because the one thing that must
# not happen here is a silent "remove()" against a path a human never saw.

#: Generic interpreters/DLL hosts. A registry entry pointing at one of these is
#: not a real uninstaller, and running it would hand arbitrary strings to a
#: shell — refuse instead (quietly honest: "I found no reliable uninstaller").
_SHELL_HOSTS = frozenset({
    "cmd.exe", "command.com", "powershell.exe", "pwsh.exe", "wscript.exe",
    "cscript.exe", "mshta.exe", "rundll32.exe", "reg.exe", "regedit.exe",
    "sc.exe", "net.exe", "netsh.exe", "taskkill.exe", "wmic.exe", "curl.exe",
    "bitsadmin.exe", "certutil.exe", "forfiles.exe", "ieexec.exe",
    "bash", "sh", "zsh", "ksh", "csh", "fish", "sudo", "su",
    "python", "python3", "python.exe", "py.exe", "perl", "ruby",
    "osascript", "open", "env", "xargs", "find", "awk", "sed",
})

#: Windows binaries that must be looked up in %SystemRoot%\System32 rather
#: than trusted from the string as-is.
_SYSTEM_UNINSTALLERS = frozenset({"msiexec.exe"})


@dataclass
class UninstallPlan:
    """One resolvable way to remove an app, with where it came from."""

    name: str                  # app name as the vendor registered it
    exe: str                   # absolute path of the uninstaller
    args: tuple = ()           # arguments registered next to it (argv, no shell)
    source: str = ""           # which local source produced this
    location: str = ""         # install location, when the vendor recorded one
    detail: str = ""

    def command_line(self) -> str:
        parts = [f'"{self.exe}"' if " " in self.exe else self.exe]
        parts.extend(self.args)
        return " ".join(parts)


def split_command(command: str) -> tuple[str, tuple[str, ...]] | None:
    """Split a registered command line into ``(exe, args)``, quote-aware.

    Windows registries store these as one string; ``shlex`` (posix=False) keeps
    the quotes out of the tokens while still honouring quoted paths with spaces.
    Returns None when nothing usable is in there.
    """
    text = str(command or "").strip()
    if not text:
        return None
    try:
        parts = shlex.split(text, posix=False)
    except ValueError:
        return None
    parts = [p.strip().strip('"') for p in parts if p and p.strip()]
    if not parts:
        return None
    return parts[0], tuple(parts[1:])


def parse_uninstall_command(command: str) -> tuple[str, tuple[str, ...]] | None:
    """``(exe, args)`` for a registry uninstall command, or None when unusable.

    Accepted: an existing executable that is either a Windows installer service
    (``msiexec.exe``) or visibly an uninstall/repair program — its own name
    matches the installer/updater pattern, or the command mentions uninstall.
    Refused: shell/interpreter hosts, relative paths, and files that are not
    there. Pure string handling plus ``Path.exists``, so it is testable off
    Windows.
    """
    parsed = split_command(command)
    if parsed is None:
        return None
    exe, args = parsed
    base = Path(exe).name.lower()
    if base in _SHELL_HOSTS:
        return None

    resolved = exe
    if base in _SYSTEM_UNINSTALLERS and not Path(exe).is_absolute():
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        cand = Path(system_root) / "System32" / base
        resolved = str(cand)
    elif not Path(exe).is_absolute() and _IS_WIN and base in _SYSTEM_UNINSTALLERS:
        return None
    if not Path(resolved).exists():
        return None

    joined = " ".join((base,) + args).lower()
    if base in _SYSTEM_UNINSTALLERS:
        # msiexec must be told to remove (/x or /uninstall) — never to run a
        # script (/s) or a DLL, which is what a poisoned entry would use.
        if not any(a.lower().startswith(("/x", "/uninstall")) for a in args):
            return None
        return resolved, args
    if base == "steam.exe" or "steam://uninstall/" in joined:
        # Steam games uninstall through Steam itself (protocol or --uninstall).
        return resolved, args
    from core.install_safety import is_installer_target

    if is_installer_target(base) or "uninstall" in joined or "unins" in joined:
        return resolved, args
    return None


def _match_uninstall_entry(name: str, entries: list[dict]) -> dict | None:
    """Best DisplayName match: exact, then word-prefix, then containment."""
    want = re.sub(r"\s+", " ", str(name or "").strip()).casefold()
    if not want:
        return None
    def _has_uninstaller(entry: dict) -> bool:
        return bool(entry.get("uninstall"))

    exact = [e for e in entries if str(e.get("name", "")).casefold() == want]
    for entry in exact:
        if _has_uninstaller(entry):
            return entry
    for entry in entries:
        if not _has_uninstaller(entry):
            continue
        got = str(entry.get("name", "")).casefold()
        if got.startswith(want) or want.startswith(got):
            return entry
    for entry in entries:
        if not _has_uninstaller(entry):
            continue
        got = str(entry.get("name", "")).casefold()
        if want in got or got in want:
            return entry
    return None


def find_uninstaller(name: str, target=None) -> UninstallPlan | None:
    """The vendor's own way to remove `name`, or None when there is none.

    None is a real answer and the callers say so: an unpacked folder, a portable
    build or a store app has no uninstaller this code can run safely, and
    guessing (deleting the install directory) is not an uninstall.
    """
    raw = str(name or "").strip()
    if not raw:
        return None

    if _IS_WIN:
        entries = _uninstall_entries()
        if not entries:
            return None
        entry = _match_uninstall_entry(raw, entries)
        if entry is None and target is not None:
            # Fall back to the install location: the entry whose folder contains
            # the resolved executable is the app's own, whatever its DisplayName.
            try:
                tpath = Path(str(getattr(target, "target", "") or "")).resolve()
            except Exception:
                tpath = None
            if tpath is not None:
                for candidate in entries:
                    loc = str(candidate.get("location") or "").strip()
                    if not loc or not candidate.get("uninstall"):
                        continue
                    try:
                        Path(loc).resolve()
                        if tpath.is_relative_to(Path(loc).resolve()):
                            entry = candidate
                            break
                    except Exception:
                        continue
        if entry is None:
            return None
        parsed = parse_uninstall_command(str(entry.get("uninstall") or ""))
        if parsed is None:
            return None
        exe, args = parsed
        return UninstallPlan(
            name=str(entry.get("name") or raw),
            exe=exe, args=args, source="Windows uninstall registry",
            location=str(entry.get("location") or ""),
            detail=f"uninstaller registered by {entry.get('publisher') or 'the vendor'}",
        )

    if _IS_MAC:
        apps = _mac_apps()
        want = str(raw).strip().casefold()
        for app_name, path in apps.items():
            got = str(app_name).strip().casefold()
            if got == want or want in got or got in want:
                bundle = Path(path)
                if bundle.exists() and bundle.suffix == ".app":
                    # Moving a bundle to the Trash is the documented user-level
                    # uninstall on macOS, and it is reversible by the user.
                    return UninstallPlan(
                        name=str(app_name), exe=str(bundle), args=(),
                        source="macOS application bundle",
                        location=str(bundle.parent),
                        detail="moved to the Trash (its files under ~/Library stay)",
                    )
        return None

    # Linux: package-managed installs need root and a distribution-specific
    # command, and there is no user-level uninstaller to run. Rather than
    # deleting directories and calling it uninstalled, say so (the tool does).
    return None


# ── Roblox ────────────────────────────────────────────────────────────────────
# Roblox keeps several installed builds side by side under
# %LOCALAPPDATA%\Roblox\Versions, and *only one of them is the live one*. The
# registry records which: the roblox-player protocol handler points at the
# currently deployed player, and the Environments key names the version folder.
# Picking the newest directory by mtime instead (the old behaviour) can launch a
# half-updated or superseded build, which exits immediately — the "Roblox starts
# and vanishes" failure. So: registry first, directory scan as the fallback.

_ROBLOX_PLAYER_EXE = "RobloxPlayerBeta.exe"
_ROBLOX_STUDIO_EXE = "RobloxStudioBeta.exe"
_ROBLOX_LAUNCHER = "RobloxPlayerLauncher.exe"


def parse_registered_command(command: str) -> str | None:
    """Executable path out of a registry command line, or None.

    Windows stores protocol handlers as ``"C:\\...\\RobloxPlayerBeta.exe" -protocol "%1"``.
    Only the quoted (or first-token) executable is returned — the arguments are
    deliberately dropped, because nothing here should ever execute them.
    Pure string handling, so it is testable off Windows.
    """
    text = str(command or "").strip()
    if not text:
        return None
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 1:
            return text[1:end]
        return None
    first = text.split(" ", 1)[0]
    return first or None


def _winreg_value(hive_name: str, key_path: str, value: str = "") -> str | None:
    """Read one registry value (string), or None. Windows-only, never raises."""
    if not _IS_WIN:
        return None
    try:
        import winreg
    except ImportError:
        return None
    hive = getattr(winreg, hive_name, None)
    if hive is None:
        return None
    try:
        with winreg.OpenKey(hive, key_path) as key:
            data, _kind = winreg.QueryValueEx(key, value)
        return str(data or "").strip() or None
    except Exception:
        return None


def _roblox_registered_exe(studio: bool = False) -> str | None:
    """The build Windows currently has registered for Roblox, or None.

    Two independent records are consulted; either is enough. Both are validated
    against the expected exe name and the file actually existing, so a stale
    registry entry cannot send the launch somewhere wrong.
    """
    want = _ROBLOX_STUDIO_EXE if studio else _ROBLOX_PLAYER_EXE
    protocol = "roblox-studio" if studio else "roblox-player"

    command = _winreg_value("HKEY_CLASSES_ROOT",
                            rf"{protocol}\shell\open\command")
    exe = parse_registered_command(command or "")
    if exe and Path(exe).name.lower() == want.lower() and Path(exe).exists():
        return exe

    local = os.environ.get("LOCALAPPDATA", "")
    version = _winreg_value(
        "HKEY_CURRENT_USER",
        rf"SOFTWARE\Roblox Corporation\Environments\{protocol}",
        "Version",
    )
    if local and version:
        candidate = Path(local) / "Roblox" / "Versions" / version / want
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            return None
    return None


def _roblox_version_dirs() -> list[Path]:
    """Installed Roblox version directories, newest first (may be empty)."""
    local = os.environ.get("LOCALAPPDATA", "")
    if not (_IS_WIN and local):
        return []
    versions = Path(local) / "Roblox" / "Versions"
    if not versions.is_dir():
        return []
    try:
        return sorted((d for d in versions.iterdir() if d.is_dir()),
                      key=lambda d: d.stat().st_mtime, reverse=True)
    except Exception:
        return []


def _find_roblox_exe(studio: bool = False) -> str | None:
    """Installed Roblox exe (player or studio), registered build first.

    Both live in the same %LOCALAPPDATA%\\Roblox\\Versions tree - the scan is
    shared, only the file name differs. No shortcut fallback: this is the
    direct-exe check that resolve() prefers over any launcher stub.
    """
    want = _ROBLOX_STUDIO_EXE if studio else _ROBLOX_PLAYER_EXE
    registered = _roblox_registered_exe(studio=studio)
    if registered:
        return registered
    for d in _roblox_version_dirs():
        exe = d / want
        try:
            if exe.exists():
                return str(exe)
        except OSError:
            continue
    return None


def launch_alternatives(tgt) -> list["LaunchTarget"]:
    """Fallback launch strategies for a target, best first (may be empty).

    Only Roblox defines them today, and for a concrete reason: the player can
    be started three legitimate ways (deployed exe, the launcher stub beside
    it, the registered protocol handler) and which one works depends on the
    state of its self-updater. Trying them in order — each verified by the
    caller before the next — turns a dead end into a diagnosis.
    """
    out: list[LaunchTarget] = []
    try:
        target = str(getattr(tgt, "target", "") or "")
        display = str(getattr(tgt, "display", "") or "")
        studio = "studio" in display.lower() or _ROBLOX_STUDIO_EXE.lower() in target.lower()
        if "roblox" not in target.lower() and "roblox" not in display.lower():
            return out
        if studio:
            return out      # Studio has one exe and no launcher stub to fall back to

        current = Path(target).name.lower() if target else ""
        player = _find_roblox_exe(studio=False)

        # 1) The launcher stub next to the player: it syncs the install first,
        #    then hands off to the player — the path Roblox's own website uses.
        if current != _ROBLOX_LAUNCHER.lower() and target and Path(target).parent:
            stub = Path(target).parent / _ROBLOX_LAUNCHER
            try:
                if stub.exists():
                    out.append(LaunchTarget(kind="roblox", display=display or "Roblox Player",
                                            target=str(stub), source="Roblox launcher"))
            except OSError:
                pass

        # 2) The deployed player, when we were pointed at a shortcut or stub.
        if player and Path(player).name.lower() != current:
            out.append(LaunchTarget(kind="roblox", display=display or "Roblox Player",
                                    target=player, source="Roblox install"))

        # 3) The registered protocol handler — a last resort that also works
        #    when the exe layout changed under us.
        for protocol in _ROBLOX_PROTOCOLS:
            if _is_protocol_registered(protocol):
                out.append(LaunchTarget(kind="protocol", display=display or "Roblox Player",
                                        target=protocol, source="protocol"))
                break
    except Exception:
        return out[:2]
    return out[:3]


def _is_protocol_registered(protocol: str) -> bool:
    """True when Windows has a handler for this URI scheme."""
    if not _IS_WIN:
        return False
    return bool(_winreg_value("HKEY_CLASSES_ROOT", rf"{protocol}\shell\open\command"))


def launch_hint(tgt, vanished: bool = False) -> str:
    """One actionable sentence about *why* a launch did not stick."""
    try:
        target = str(getattr(tgt, "target", "") or "").lower()
        display = str(getattr(tgt, "display", "") or "")
    except Exception:
        return ""
    if "roblox" not in target and "roblox" not in display.lower():
        return ""
    if vanished:
        return ("Roblox started and exited immediately. That usually means the "
                "build it launched is stale or superseded — Roblox only accepts "
                "its currently deployed version. Start Roblox once by hand (or "
                "from roblox.com) so it can update itself, then I can open it again.")
    return ("Roblox did not stay running. Check Task Manager for a hidden "
            "RobloxPlayerBeta.exe from an earlier attempt, and start Roblox once "
            "by hand so its updater can finish.")


def _find_roblox() -> str | None:
    """Installed RobloxPlayerBeta.exe, newest Versions dir first. None if absent."""
    rb = _find_roblox_exe(studio=False)
    if rb:
        return rb
    # non-default installs still leave a Start Menu shortcut
    m = _match_shortcut("Roblox Player")
    if m:
        tgt = parse_lnk_target(m[1])
        return tgt or m[1]
    return None


def _steam_games_cached() -> list[dict]:
    """Installed Steam games via game_updater's own scanner (no duplication)."""
    try:
        from actions import game_updater as _gu
        steam_path = _gu._find_steam_path()
        if not steam_path:
            return []
        return _gu._get_steam_games(steam_path)
    except Exception:
        return []


def _epic_games_cached() -> list[dict]:
    try:
        from actions import game_updater as _gu
        return _gu._get_epic_games()
    except Exception:
        return []


def _mac_apps() -> dict[str, str]:
    """display.lower() → /.../*.app (macOS fallback source)."""
    out: dict[str, str] = {}
    for root in (Path("/Applications"), Path.home() / "Applications"):
        if not root.is_dir():
            continue
        try:
            for app in root.glob("*.app"):
                out[app.stem.lower().strip()] = str(app)
        except Exception:
            continue
    return out


def _linux_desktop_entries() -> dict[str, tuple[str, str]]:
    """display.lower() → (desktop-id, exec-binary). Linux fallback source."""
    out: dict[str, tuple[str, str]] = {}
    roots = [Path("/usr/share/applications"), Path.home() / ".local/share/applications"]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for f in root.glob("*.desktop"):
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                name = exe = None
                nodisplay = False
                for line in text.splitlines():
                    if line.startswith("Name=") and name is None:
                        name = line[5:].strip()
                    elif line.startswith("Exec=") and exe is None:
                        exe = line[5:].strip().split()[0] if line[5:].strip() else None
                    elif line.startswith("NoDisplay=true"):
                        nodisplay = True
                if name and not nodisplay and name.lower() not in out:
                    out[name.lower()] = (f.stem, exe or "")
        except Exception:
            continue
    return out


# ── Resolution ────────────────────────────────────────────────────────────────

def _valid_http_url(raw: str) -> str | None:
    text = (raw or "").strip()
    if not _URL_RE.match(text) or len(text) > 2048:
        return None
    if any(c in text for c in (" ", "\n", "\r", "\t", "<", ">", '"', "'", "`")):
        return None
    return text


def resolve(raw: str) -> LaunchTarget | None:
    """Natural name / alias → concrete launch target, or None when not installed."""
    text = (raw or "").strip()
    if not text:
        return None

    # 1) URLs — the open_app description promises websites, so honour them.
    url = _valid_http_url(text)
    if url:
        return LaunchTarget(kind="url", display=url, target=url, source="url")

    # 2) bare allow-listed protocols only (ms-settings:). Anything else shaped
    # like a URI is rejected — the model must not smuggle payloads past this.
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", text) and "://" not in text and " " not in text:
        if text in _PROTOCOL_ALLOWLIST:
            return LaunchTarget(kind="protocol", display=text, target=text, source="protocol")
        return None

    canon = canonical_name(text)
    key = canon.lower().strip()
    if not key:
        return None

    # 3) persistent cache (existence-validated, stale entries dropped)
    hit = _cached_target(key)
    if hit:
        return hit

    def _found(tgt: LaunchTarget) -> LaunchTarget:
        _store_target(key, tgt)
        return tgt

    # 4) PATH, via exe hints first (cheap) then the literal name.
    # Candidates containing whitespace are skipped: no executable basename
    # has a space, so "libreoffice --writer" must not resolve here as a
    # program name — the args-split fallback below handles that shape.
    for exe in _EXE_HINTS.get(key, []) + [key]:
        if any(c in exe for c in (" ", "\t", "\n", "\r")):
            continue
        found = shutil.which(exe)
        if found:
            return _found(LaunchTarget(kind="exe", display=canon, target=found, source="PATH"))

    if _IS_WIN:
        # 5) App Paths registry
        for exe in _EXE_HINTS.get(key, []) + [key]:
            ap = _reg_app_paths(exe)
            if ap:
                return _found(LaunchTarget(kind="exe", display=canon, target=ap, source="App Paths"))

        # 5b) Roblox (player + studio): the installed exe beats any shortcut.
        # The Start Menu entry is a launcher stub whose handoff fails
        # silently-ish (WinError 1223 on a dismissed UAC prompt, stale stub
        # after an update) — resolving the real exe first removes that layer.
        # The shortcut fallback stays at step 10 for non-default installs.
        if key in ("roblox player", "roblox"):
            _rb = _find_roblox_exe(studio=False)
            if _rb:
                return _found(LaunchTarget(kind="roblox", display="Roblox Player",
                                           target=_rb, source="Roblox install"))
        if key == "roblox studio":
            _rbs = _find_roblox_exe(studio=True)
            if _rbs:
                return _found(LaunchTarget(kind="roblox", display="Roblox Studio",
                                           target=_rbs, source="Roblox install"))

        # 6) Start Menu shortcuts
        m = _match_shortcut(canon)
        if m:
            return _found(LaunchTarget(kind="shortcut", display=m[0], target=m[1], source="Start Menu"))

        # 7) Microsoft-Store apps with no classic exe
        aumid = _UWP_AUMIDS.get(key)
        if aumid:
            return _found(LaunchTarget(kind="uwp", display=canon,
                                       target=f"shell:AppsFolder\\{aumid}", source="Microsoft Store"))

        # 8) Steam — only when the manifest proves it is installed
        appid = _STEAM_APPIDS.get(key)
        if appid and appid.isdigit():
            for g in _steam_games_cached():
                if str(g.get("id")) == appid:
                    uri = f"steam://rungameid/{appid}"
                    return _found(LaunchTarget(kind="steam", display=g.get("name", canon),
                                               target=uri, source="Steam"))
        else:
            for g in _steam_games_cached():
                if str(g.get("name", "")).lower().strip() == key:
                    uri = f"steam://rungameid/{g.get('id')}"
                    if _STEAM_URL_RE.match(uri):
                        return _found(LaunchTarget(kind="steam", display=g.get("name", canon),
                                                   target=uri, source="Steam"))

        # 9) Epic — same installed-only rule
        for g in _epic_games_cached():
            if str(g.get("name", "")).lower().strip() == key:
                uri = (f"com.epicgames.launcher://apps/{g.get('id')}"
                       f"?action=launch&silent=true")
                if _EPIC_URL_RE.match(uri):
                    return _found(LaunchTarget(kind="epic", display=g.get("name", canon),
                                               target=uri, source="Epic"))
                break

        # 10) Roblox Player — installed exe only, never a download
        if key in ("roblox player", "roblox"):
            rb = _find_roblox()
            if rb:
                kind = "exe" if rb.lower().endswith(".exe") else "shortcut"
                return _found(LaunchTarget(kind=kind, display="Roblox Player",
                                           target=rb, source="Roblox install"))

    if _IS_MAC:
        apps = _mac_apps()
        if key in apps:
            return _found(LaunchTarget(kind="app", display=canon, target=apps[key], source="Applications"))
        for name, path in apps.items():
            if name.startswith(key) or key.startswith(name):
                return _found(LaunchTarget(kind="app", display=Path(path).stem,
                                           target=path, source="Applications"))

    if _SYSTEM == "Linux":
        entries = _linux_desktop_entries()
        if key in entries:
            did, exe = entries[key]
            return _found(LaunchTarget(kind="app", display=canon,
                                       target=exe or did, source=".desktop"))
        # Trailing CLI args ("libreoffice --writer"): exact matches missed,
        # so resolve the head as a program and carry the rest as argv. Tried
        # before the fuzzy loop so "--writer" cannot fuzzy-match an entry and
        # swallow the arguments. Deliberately NOT cached: arguments are
        # per-call, and a cached arg-less target would eat them next time.
        if " " in text:
            try:
                parts = shlex.split(text)
            except ValueError:
                parts = []
            if len(parts) >= 2 and parts[0]:
                head, rest = parts[0], tuple(parts[1:])
                head_key = canonical_name(head).lower().strip()
                for exe in _EXE_HINTS.get(head_key, []) + [head_key, head]:
                    if not exe or any(c in exe for c in (" ", "\t", "\n", "\r")):
                        continue
                    found = shutil.which(exe)
                    if found:
                        return LaunchTarget(kind="exe", display=canonical_name(head),
                                            target=found, source="PATH", args=rest)
                if head_key in entries:
                    did, exe = entries[head_key]
                    return LaunchTarget(kind="app", display=canonical_name(head),
                                        target=exe or did, source=".desktop", args=rest)
        for name, (did, exe) in entries.items():
            if name.startswith(key) or key.startswith(name):
                return _found(LaunchTarget(kind="app", display=name,
                                           target=exe or did, source=".desktop"))

    return None


def suggest(raw: str, limit: int = 3) -> list[str]:
    """Close installed names for a 'not found' message (difflib, never auto-launched)."""
    import difflib
    canon = canonical_name(raw)
    pool: set[str] = set()
    if _IS_WIN:
        pool.update(Path(p).stem for p in _scan_shortcuts().values())
        pool.update(str(g.get("name", "")) for g in _steam_games_cached())
        pool.update(str(g.get("name", "")) for g in _epic_games_cached())
    elif _IS_MAC:
        pool.update(Path(p).stem for p in _mac_apps().values())
    elif _SYSTEM == "Linux":
        pool.update(_linux_desktop_entries().keys())
    pool = {p for p in pool if p}
    return difflib.get_close_matches(canon, sorted(pool), n=limit, cutoff=0.6)


# ── Launching (all non-blocking, no shell) ────────────────────────────────────

#: Windows launch failures worth translating: the raw "[WinError 1223]" tells
#: the user nothing, but the cause is usually one specific, fixable thing.
_WINDOWS_LAUNCH_DIAGNOSIS = {
    1223: ("The launch was cancelled — the Windows permission prompt (UAC) was "
           "dismissed or answered with 'No'. Approve it and try again."),
    740: ("This program needs administrator rights. I will not relaunch it "
          "elevated myself — right-click it and choose 'Run as administrator' "
          "if you trust it."),
    5: ("Access denied. The file may be blocked by Windows (right-click → "
        "Properties → Unblock), or antivirus is preventing the start."),
    2: ("The file is no longer where it was expected (moved, renamed, or "
        "uninstalled since)."),
    3: "The folder path no longer exists.",
    193: ("Not a valid Windows application — wrong architecture or a "
          "corrupted download."),
    1155: "No program is associated with this file or link type.",
    1156: "The shortcut target is unavailable.",
}


def diagnose_launch_error(exc: BaseException, tgt=None) -> str:
    """Plain-language diagnosis for a failed launch. Never raises.

    Translates Windows error codes (WinError 1223 et al.) and common POSIX
    errnos into one actionable sentence. Empty string when nothing useful can
    be said — callers fall back to the raw error text.
    """
    try:
        code = getattr(exc, "winerror", None)
        if code is None and isinstance(exc, OSError):
            code = exc.errno
        if isinstance(code, int):
            hit = _WINDOWS_LAUNCH_DIAGNOSIS.get(code)
            if hit:
                return hit
            if code == 13:
                return "Permission denied — the file or folder is not accessible."
            if code in (8,):
                return "Not enough memory to start the program."
        text = str(exc or "").strip()
        if tgt is not None:
            target = str(getattr(tgt, "target", "") or "")
            if target and "roblox" in target.lower() and "player" in text.lower():
                return ("Roblox's launcher stub failed its handoff to the player. "
                        "If this repeats, start Roblox once by hand so it can update itself.")
            hint = launch_hint(tgt)
            if hint and ("roblox" in target.lower()
                         or "roblox" in str(getattr(tgt, "display", "")).lower()):
                return hint
        if len(text) > 220:
            text = text[:220] + "…"
        return text
    except Exception:
        return ""


def launch(tgt: LaunchTarget, *, background: bool = False) -> tuple[bool, str]:
    """Start the target; optional documented no-activation startup hint."""
    if background and tgt.kind not in ("exe", "roblox"):
        return False, "Gaming Mode: this launcher cannot guarantee a background start. Focus JARVIS first or disable Gaming Mode to open it."
    try:
        if tgt.kind == "url":
            if not _valid_http_url(tgt.target):
                return False, "That address is not a safe web URL."
            webbrowser.open(tgt.target)
            return True, f"Opened {tgt.display} in the browser."

        if tgt.kind == "protocol":
            if tgt.target not in _PROTOCOL_ALLOWLIST:
                return False, "That system shortcut is not allowed."
            if not _IS_WIN:
                return False, f"{tgt.display} needs its Windows handler."
            os.startfile(tgt.target)  # noqa: S606 — allowlisted scheme, Windows only
            return True, f"Opened {tgt.display}."

        if tgt.kind in ("steam", "epic", "uwp"):
            if not (_STEAM_URL_RE.match(tgt.target) or _EPIC_URL_RE.match(tgt.target)
                    or _APPSFOLDER_RE.match(tgt.target)):
                return False, "That launcher link failed validation."
            if not _IS_WIN:
                return False, f"{tgt.display} needs its Windows launcher."
            os.startfile(tgt.target)
            return True, f"Opened {tgt.display}."

        if tgt.kind == "shortcut":
            if not tgt.target.lower().endswith(".lnk") or not Path(tgt.target).exists():
                return False, f"I could not find {tgt.display} anymore."
            if not _IS_WIN:
                return False, f"{tgt.display} is a Windows shortcut."
            os.startfile(tgt.target)
            return True, f"Opened {tgt.display}."

        if tgt.kind in ("exe", "roblox"):
            # No proactive exists() check: launch and handle FileNotFoundError
            # below instead. A check-then-launch pair is a TOCTOU race, and the
            # handler already produces the same "could not find" message.
            flags: dict = {}
            if _IS_WIN:
                flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if background:
                    startup = subprocess.STARTUPINFO()
                    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE; no focus restoration hacks
                    flags["startupinfo"] = startup
                # Roblox resolves its own DLLs and update files relative to the
                # build directory, so it must be started WITH that directory as
                # the working directory. Handing it the assistant's cwd is one
                # of the documented causes of "the player starts and vanishes".
                parent = Path(tgt.target).parent
                if "roblox" in Path(tgt.target).name.lower() and parent.is_dir():
                    flags["cwd"] = str(parent)
            subprocess.Popen([tgt.target, *tgt.args], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, **flags)
            return True, f"Opened {tgt.display}."

        if tgt.kind == "app" and _IS_MAC:
            cmd = ["open", tgt.target] + (["--args", *tgt.args] if tgt.args else [])
            r = subprocess.run(cmd, capture_output=True, timeout=10)
            return (True, f"Opened {tgt.display}.") if r.returncode == 0 else \
                (False, f"{tgt.display} did not start.")

        if tgt.kind == "app" and _SYSTEM == "Linux":
            # gtk-launch takes no arguments: with args, go straight to the exe.
            if not tgt.args and shutil.which("gtk-launch"):
                r = subprocess.run(["gtk-launch", Path(tgt.target).stem],
                                   capture_output=True, timeout=10)
                if r.returncode == 0:
                    return True, f"Opened {tgt.display}."
            exe = shutil.which(tgt.target) or (tgt.target if Path(tgt.target).exists() else None)
            if exe:
                subprocess.Popen([exe, *tgt.args], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                return True, f"Opened {tgt.display}."
            return False, f"{tgt.display} did not start."
    except FileNotFoundError:
        return False, f"I could not find {tgt.display} anymore."
    except Exception as e:
        print(f"[AppFinder] launch({tgt.kind}:{tgt.display}) failed: {e}")
        diag = diagnose_launch_error(e, tgt)
        if diag:
            return False, f"{tgt.display} did not start ({type(e).__name__}): {diag}"
        return False, f"{tgt.display} did not start ({type(e).__name__})."
    return False, f"Cannot launch {tgt.display} on {_SYSTEM}."


# ── Inventory ─────────────────────────────────────────────────────────────────

def list_apps(game_only: bool = False) -> list[dict]:
    """Installed-application inventory. Names + kinds; paths only via where()."""
    inv: dict[str, dict] = {}

    def _add(name: str, kind: str, source: str) -> None:
        name = (name or "").strip()
        if not name or name.lower() in ("setup", "uninstall", "uninstaller", "update"):
            return
        kl = name.lower()
        if kl not in inv:
            inv[kl] = {"name": name, "kind": kind, "source": source}

    if _IS_WIN:
        for p in _scan_shortcuts().values():
            _add(Path(p).stem, "shortcut", "Start Menu")
        for g in _steam_games_cached():
            _add(str(g.get("name", "")), "steam", "Steam")
        for g in _epic_games_cached():
            _add(str(g.get("name", "")), "epic", "Epic")
        for e in _uninstall_entries():
            _add(e["name"], "installed", "Windows")
    elif _IS_MAC:
        for p in _mac_apps().values():
            _add(Path(p).stem, "app", "Applications")
    elif _SYSTEM == "Linux":
        for name in _linux_desktop_entries():
            _add(name, "app", ".desktop")

    items = sorted(inv.values(), key=lambda d: d["name"].lower())
    if game_only:
        games: list[dict] = [d for d in items if d["kind"] in ("steam", "epic")]
        known = {v.lower() for v in _BASE_ALIASES.values()}
        for d in items:
            if d["kind"] in ("steam", "epic"):
                continue
            if d["name"].lower() in known and d["kind"] in ("shortcut", "installed", "app"):
                # alias-known names that are games, not tools: only the ones
                # whose canonical form appears in the Steam table or ends with
                # a game-ish source match keep this simple — Steam/Epic plus
                # shortcut names matching a game alias.
                games.append(d)
        # de-dup, keep order
        seen, out = set(), []
        for d in games:
            if d["name"].lower() not in seen:
                seen.add(d["name"].lower())
                out.append(d)
        return out
    return items


def where(name: str) -> dict | None:
    """Install location info for one app (used when the user explicitly asks)."""
    tgt = resolve(name)
    if tgt is None:
        return None
    info: dict = {"name": tgt.display, "kind": tgt.kind, "source": tgt.source}
    if tgt.kind in ("exe", "roblox", "shortcut", "app") and Path(tgt.target).exists():
        info["path"] = tgt.target
        if tgt.kind == "shortcut":
            real = parse_lnk_target(tgt.target)
            if real:
                info["target"] = real
    elif tgt.kind in ("steam", "epic", "uwp", "url", "protocol"):
        info["target"] = tgt.target
    return info
