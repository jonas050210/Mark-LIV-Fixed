"""Conservative game classification and foreground policy, with no focus tricks.

Window/process inspection stays in app_controller. A borderless fullscreen
utility is not automatically a game. Unknown windowed games can be explicitly
listed in gaming_executables in the existing config.
"""
from pathlib import PureWindowsPath

KNOWN_GAMES = frozenset({"robloxplayerbeta.exe", "minecraft.windows.exe",
                        "fortniteclient-win64-shipping.exe", "cs2.exe",
                        "valorant-win64-shipping.exe", "dota2.exe", "gta5.exe",
                        "rocketleague.exe", "overwatch.exe", "eldenring.exe"})


def is_game(info: dict, extra=()) -> bool:
    name = PureWindowsPath(str(info.get("exe", ""))).name.lower()
    extra = extra if isinstance(extra, (list, tuple, set, frozenset)) else ()
    known = KNOWN_GAMES | {PureWindowsPath(str(x)).name.lower() for x in extra}
    if name in known:
        return True
    path = str(info.get("path", "")).lower().replace("/", "\\")
    return bool(info.get("fullscreen") and any(
        marker in path for marker in ("\\steamapps\\common\\", "\\epic games\\", "\\xboxgames\\")))


def foreground_game() -> dict:
    from memory.config_manager import load_api_keys
    config = load_api_keys()
    if not config.get("gaming_mode", False):
        return {}
    from core.app_controller import foreground_window
    info = foreground_window()
    return info if is_game(info, config.get("gaming_executables", [])) else {}


def protect_focus() -> bool:
    return bool(foreground_game())
