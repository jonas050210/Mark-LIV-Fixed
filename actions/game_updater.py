import os
import platform
import re
import sys
import json
import time
import subprocess
import threading
from pathlib import Path
from datetime import datetime

from core import confirm
from core import install_safety, tasks

from config import get_os, is_windows, is_mac, is_linux

_CNW: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if platform.system() == "Windows" else {}
)

_KNOWN_APPIDS: dict[str, tuple[str, str]] = {
    "pubg":                ("578080",  "PUBG: Battlegrounds"),
    "pubg battlegrounds":  ("578080",  "PUBG: Battlegrounds"),
    "pubg: battlegrounds": ("578080",  "PUBG: Battlegrounds"),
    "battlegrounds":       ("578080",  "PUBG: Battlegrounds"),
    "gta5":                ("271590",  "Grand Theft Auto V"),
    "gta v":               ("271590",  "Grand Theft Auto V"),
    "grand theft auto v":  ("271590",  "Grand Theft Auto V"),
    "cs2":                 ("730",     "Counter-Strike 2"),
    "csgo":                ("730",     "Counter-Strike 2"),
    "counter-strike 2":    ("730",     "Counter-Strike 2"),
    "counter strike 2":    ("730",     "Counter-Strike 2"),
    "dota2":               ("570",     "Dota 2"),
    "dota 2":              ("570",     "Dota 2"),
    "rust":                ("252490",  "Rust"),
    "valheim":             ("892970",  "Valheim"),
    "cyberpunk":           ("1091500", "Cyberpunk 2077"),
    "cyberpunk 2077":      ("1091500", "Cyberpunk 2077"),
    "elden ring":          ("1245620", "ELDEN RING"),
    "minecraft":           ("1672970", "Minecraft Launcher"),
    "apex legends":        ("1172470", "Apex Legends"),
    "apex":                ("1172470", "Apex Legends"),
    "fortnite":            ("1517990", "Fortnite"),
    "goose goose duck":    ("1568590", "Goose Goose Duck"),
    "among us":            ("945360",  "Among Us"),
    "fall guys":           ("1097150", "Fall Guys"),
    "rocket league":       ("252950",  "Rocket League"),
    "warframe":            ("230410",  "Warframe"),
    "destiny 2":           ("1085660", "Destiny 2"),
    "team fortress 2":     ("440",     "Team Fortress 2"),
    "tf2":                 ("440",     "Team Fortress 2"),
    "left 4 dead 2":       ("550",     "Left 4 Dead 2"),
    "l4d2":                ("550",     "Left 4 Dead 2"),
    "paladins":            ("444090",  "Paladins"),
    "smite":               ("386360",  "SMITE"),
    "war thunder":         ("236390",  "War Thunder"),
    "world of warships":   ("552990",  "World of Warships"),
    "path of exile":       ("238960",  "Path of Exile"),
    "poe":                 ("238960",  "Path of Exile"),
    "lost ark":            ("1599340", "Lost Ark"),
    "new world":           ("1063730", "New World: Aeternum"),
    "gd":                  ("322170",  "Geometry Dash"),
    "geometry dash":       ("322170",  "Geometry Dash"),
    "ksp":                 ("220200",  "Kerbal Space Program"),
    "kerbal space program": ("220200", "Kerbal Space Program"),
    "ksp2":                ("954850",  "Kerbal Space Program 2"),
    "kerbal space program 2": ("954850", "Kerbal Space Program 2"),
}

def _find_steam_path() -> Path | None:
    if is_windows(): return _find_steam_windows()
    if is_mac():     return _find_steam_mac()
    return _find_steam_linux()


def _find_steam_windows() -> Path | None:
    try:
        import winreg
        for hive, key_path in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Valve\Steam"),
        ]:
            try:
                key = winreg.OpenKey(hive, key_path)
                val, _ = winreg.QueryValueEx(key, "InstallPath")
                winreg.CloseKey(key)
                p = Path(val)
                if p.exists() and (p / "steam.exe").exists():
                    return p
            except Exception:
                continue
    except ImportError:
        pass
    for p in [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Steam",
        Path(os.environ.get("ProgramFiles", ""))       / "Steam",
        Path("C:/Steam"), Path("D:/Steam"), Path("E:/Steam"), Path("F:/Steam"),
    ]:
        if p.exists() and (p / "steam.exe").exists():
            return p
    return None


def _find_steam_mac() -> Path | None:
    for p in [
        Path.home() / "Library" / "Application Support" / "Steam",
        Path("/Applications/Steam.app/Contents/MacOS"),
    ]:
        if p.exists():
            return p
    return None


def _find_steam_linux() -> Path | None:
    for p in [
        Path.home() / ".steam" / "steam",
        Path.home() / ".steam" / "root",
        Path.home() / ".local"  / "share" / "Steam",
        Path("/usr/share/steam"),
        Path("/opt/steam"),
    ]:
        if p.exists():
            return p
    return None


def _steam_exe(steam_path: Path) -> Path:
    if is_windows(): return steam_path / "steam.exe"
    if is_mac():     return Path("/Applications/Steam.app/Contents/MacOS/steam_osx")
    return steam_path / "steam.sh"


def _launch_steam_url(exe: Path, url: str) -> None:
    if is_mac():
        subprocess.Popen(["open", url])
    elif is_linux():
        subprocess.Popen(["xdg-open", url])
    else:
        subprocess.Popen([str(exe), url])

def _get_steam_libraries(steam_path: Path) -> list[Path]:
    libraries = [steam_path / "steamapps"]
    vdf_path  = steam_path / "steamapps" / "libraryfolders.vdf"
    if not vdf_path.exists():
        return libraries
    try:
        content = vdf_path.read_text(encoding="utf-8", errors="ignore")
        for raw_path in re.findall(r'"path"\s+"([^"]+)"', content):
            lib = Path(raw_path.replace("\\\\", "/")) / "steamapps"
            if lib.exists() and lib not in libraries:
                libraries.append(lib)
    except Exception:
        pass
    return libraries


def _get_steam_games(steam_path: Path) -> list[dict]:
    games = []
    for lib in _get_steam_libraries(steam_path):
        for acf in lib.glob("appmanifest_*.acf"):
            try:
                content  = acf.read_text(encoding="utf-8", errors="ignore")
                app_id   = re.search(r'"appid"\s+"(\d+)"',     content)
                name     = re.search(r'"name"\s+"([^"]+)"',     content)
                state    = re.search(r'"StateFlags"\s+"(\d+)"', content)
                size     = re.search(r'"SizeOnDisk"\s+"(\d+)"', content)
                # Steam writes these two while it downloads. They are the only
                # progress numbers anyone outside Steam can see, so the panel
                # shows them instead of guessing.
                got      = re.search(r'"BytesDownloaded"\s+"(\d+)"', content)
                want     = re.search(r'"BytesToDownload"\s+"(\d+)"', content)
                if app_id and name:
                    games.append({
                        "id":    app_id.group(1),
                        "name":  name.group(1),
                        "state": int(state.group(1)) if state else 0,
                        "size":  int(size.group(1))  if size  else 0,
                        "downloaded": int(got.group(1))  if got  else 0,
                        "to_download": int(want.group(1)) if want else 0,
                        "lib":   str(lib),
                        "acf":   str(acf),
                    })
            except Exception:
                continue
    return games

# ── shared plumbing for every action that changes installed games ─────────────
# Steam never tells us "done" — the only ground truth is the app manifest on
# disk, so every claim this module makes is read back out of `_get_steam_games`
# after the request. The glosses below are only the values this file already
# acts on; an unknown flag is reported verbatim instead of guessed at.
_STEAM_STATES = {
    4: "up to date",
    6: "update pending",
    516: "update pending",
    1026: "downloading",
}


def _steam_state_text(state) -> str:
    try:
        return _STEAM_STATES.get(int(state), f"state {state}")
    except (TypeError, ValueError):
        return "state unknown"


def _steam_entry(steam_path: Path, app_id: str) -> dict | None:
    """The manifest entry for one AppID, or None when Steam has no such app."""
    wanted = str(app_id or "").strip()
    if not wanted:
        return None
    try:
        for game in _get_steam_games(steam_path):
            if str(game.get("id")) == wanted:
                return game
    except Exception:
        return None
    return None


def _await_steam_state(steam_path: Path, app_id: str, wanted: set[int],
                       *, timeout: float, task=None,
                       title: str = "") -> tuple[bool, dict | None, str]:
    """Poll Steam's manifest until the app reaches one of ``wanted`` states.

    Returns ``(verified, entry, detail)``. Verification is the whole point: a
    ``steam://`` URL only *asks* Steam to do something, and a launcher that
    silently declines must not turn into a cheerful "Update started".
    """
    seen: dict = {}

    def _probe() -> bool:
        entry = _steam_entry(steam_path, app_id)
        if entry is None:
            if task is not None:
                task.update(detail=f"waiting for Steam to register {title or app_id}")
            return False
        seen.update(entry)
        if task is not None:
            task.update(detail=f"{title or app_id}: {_steam_state_text(entry['state'])}")
        return int(entry.get("state", 0)) in wanted

    ok, why = install_safety.verify(_probe, timeout=timeout, interval=2.0)
    return ok, (seen or None), why


def _steam_bytes(entry: dict) -> tuple[int, int] | None:
    """(downloaded, total) from Steam's own manifest, or None when absent.

    Old Steam builds and apps that have never downloaded anything do not carry
    these keys; the caller then reports state without numbers, which is the
    honest answer rather than a fabricated bar.
    """
    try:
        done = int(entry.get("downloaded") or 0)
        total = int(entry.get("to_download") or 0)
    except (TypeError, ValueError):
        return None
    if done <= 0 or total <= 0:
        return None
    return done, total


def _watch_steam_download(steam_path: Path, app_id: str, title: str, task,
                          *, poll: float = 5.0, timeout_hours: float = 12.0) -> None:
    """Follow a Steam download to its end, reporting Steam's own numbers.

    The tool returns as soon as Steam has the download queued; the *download*
    outlives that answer. This runs on its own daemon thread, reads the app
    manifest every few seconds, and closes the row when Steam itself says the
    app is up to date — so the panel shows a real percentage, a real speed and
    a real ETA for as long as the transfer lasts, and never claims it finished
    early. Nothing here talks to Steam except through the files it writes.
    """
    last_t = time.monotonic()
    last_b: int | None = None
    last_growth = last_t
    deadline = last_t + max(0.25, timeout_hours) * 3600
    while True:
        try:
            entry = _steam_entry(steam_path, app_id)
        except Exception as e:
            task.fail(error=f"{type(e).__name__}: {e}"[:160],
                      detail="Steam's manifest became unreadable")
            return
        if entry is None:
            task.fail(error="the app left Steam's library",
                      detail="it is no longer in the manifests")
            return

        state = int(entry.get("state", 0) or 0)
        now = time.monotonic()
        update: dict = {"detail": f"Steam: downloading {title}"}
        measured = _steam_bytes(entry)
        if measured is not None:
            done, total = measured
            if last_b is not None and done > last_b:
                # Speed only from real growth: a manifest that has not moved yet
                # is not a download running at 0 B/s, it is a sample with no news.
                update["speed_bps"] = (done - last_b) / max(0.001, now - last_t)
                last_t, last_b, last_growth = now, done, now
            elif last_b is None:
                last_b, last_growth = done, now
            elif now - last_growth > 30:
                update["speed_bps"] = 0.0        # genuinely stalled
            update["done_bytes"] = done
            update["total_bytes"] = total
        task.update(**update)

        if state == 4:
            task.finish(detail="Steam reports the download finished")
            return
        if state not in (6, 516, 1026):
            task.fail(error=f"Steam reports state {state}",
                      detail="the download stopped")
            return
        if time.monotonic() >= deadline:
            task.fail(error="the download did not finish in time",
                      detail=f"gave up after {timeout_hours:.0f} h")
            return
        time.sleep(poll)


def _track_steam_download(task, steam_path: Path, app_id: str, title: str) -> None:
    """Hand `task` to the download watcher. The row now closes itself."""
    if task is None or not app_id:
        return
    threading.Thread(
        target=_watch_steam_download,
        args=(steam_path, str(app_id), title or str(app_id), task),
        name=f"steam-download-{app_id}",
        daemon=True,
    ).start()


def _announce(speak, message: str) -> str:
    """Speak a final sentence once and hand it back.

    The install/update gate runs this on a worker thread after the user pressed
    CONFIRM, so this is the only place the outcome can still reach them out
    loud — `main.speak` is built for exactly that (see actions/timer.py).
    """
    if speak and message:
        try:
            speak(message)
        except Exception:
            pass
    return message


def _is_steam_running() -> bool:
    try:
        if is_windows():
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe"],
                                 capture_output=True, text=True, **_CNW).stdout
            return "steam.exe" in out.lower()
        proc = "steam_osx" if is_mac() else "steam"
        return bool(subprocess.run(["pgrep", "-x", proc],
                                   capture_output=True, text=True).stdout.strip())
    except Exception:
        return False

def _get_steam_window_rect() -> tuple[int, int, int, int] | None:
    try:
        import pygetwindow as gw
        for w in gw.getAllWindows():
            if "steam" in w.title.lower() and w.width > 200 and w.visible:
                return w.left, w.top, w.width, w.height
    except Exception:
        pass
    return None


def _click_first_profile_by_screenshot() -> bool:

    try:
        import pyautogui
        import numpy as np

        time.sleep(1.5)
        win = _get_steam_window_rect()
        if not win:
            print("[GameUpdater] ⚠️ Steam window not found")
            return False

        wx, wy, ww, wh = win
        screenshot = pyautogui.screenshot(region=(wx, wy, ww, wh))
        img        = np.array(screenshot)
        h, w       = img.shape[:2]

        search_y1, search_y2 = h // 3,  h * 3 // 4
        search_x1, search_x2 = w // 5,  w * 4 // 5
        region = img[search_y1:search_y2, search_x1:search_x2]

        r, g, b = region[:,:,0].astype(int), region[:,:,1].astype(int), region[:,:,2].astype(int)
        max_c   = np.maximum(np.maximum(r, g), b)
        min_c   = np.minimum(np.minimum(r, g), b)
        colorful = (max_c > 60) & ((max_c - min_c) > 40)

        if not colorful.any():
            print("[GameUpdater] ⚠️ Avatar colour not found — clicking by guess")
            pyautogui.click(wx + ww // 2 - ww // 6, wy + wh // 2)
            return True

        cols = np.where(colorful.any(axis=0))[0]
        rows = np.where(colorful.any(axis=1))[0]
        if not len(cols) or not len(rows):
            return False

        avatar_w   = min(90, region.shape[1] // 4)
        first_col  = int(cols[0])
        block_cols = cols[cols < first_col + avatar_w]

        abs_x = wx + search_x1 + int(block_cols.mean())
        abs_y = wy + search_y1 + int(rows.mean())
        print(f"[GameUpdater] 🎯 Profile avatar ({abs_x}, {abs_y}) — clicking")
        pyautogui.click(abs_x, abs_y)
        return True

    except ImportError as e:
        print(f"[GameUpdater] ⚠️ Missing library: {e}")
        return False
    except Exception as e:
        print(f"[GameUpdater] ⚠️ Profile detection failed: {e}")
        return False


def _handle_steam_profile_selection() -> bool:
    print("[GameUpdater] 🔍 Checking profile-selection dialog...")
    win = _get_steam_window_rect()
    if not win:
        return False

    wx, wy, ww, wh = win
    try:
        import pyautogui, numpy as np
        screenshot   = pyautogui.screenshot(region=(wx, wy, ww, wh))
        img          = np.array(screenshot)
        is_small     = ww < 900 and wh < 700
        top_region   = img[:wh // 3, :, :]
        white_pixels = int(np.sum(
            (top_region[:,:,0] > 200) &
            (top_region[:,:,1] > 200) &
            (top_region[:,:,2] > 200)
        ))
        if not is_small and white_pixels <= 100:
            print("[GameUpdater] ℹ️ No profile dialog — Steam is already logged in")
            return False
    except ImportError:
        pass
    except Exception:
        pass

    print("[GameUpdater] 👤 Profile selection detected — clicking the first profile")
    return _click_first_profile_by_screenshot()

def _find_best_drive() -> dict | None:
    import shutil, string
    drives = []
    for letter in string.ascii_uppercase:
        drive_path = f"{letter}:\\"
        if os.path.exists(drive_path):
            try:
                free_gb = shutil.disk_usage(drive_path).free / (1024 ** 3)
                if free_gb > 0:
                    drives.append({"letter": letter, "path": drive_path, "free_gb": free_gb})
            except Exception:
                continue
    return max(drives, key=lambda d: d["free_gb"]) if drives else None


def _select_drive_in_dialog(dialog, drive_letter: str) -> bool:
    target = drive_letter.upper()
    for control_type in ("ListItem", "RadioButton"):
        try:
            for ctrl in dialog.descendants(control_type=control_type):
                if target in ctrl.window_text().upper():
                    ctrl.click_input()
                    print(f"[GameUpdater] ✅ Drive selected ({control_type}): {ctrl.window_text()}")
                    return True
        except Exception:
            continue
    try:
        for combo in dialog.descendants(control_type="ComboBox"):
            try:
                combo.expand()
                time.sleep(0.15)
                for idx, txt in enumerate(combo.texts()):
                    if target in txt.upper():
                        combo.select(idx)
                        return True
                combo.collapse()
            except Exception:
                continue
    except Exception:
        pass
    try:
        for ctrl in dialog.descendants():
            txt = ctrl.window_text().upper()
            if f"{target}:" in txt and len(txt) < 80:
                ctrl.click_input()
                return True
    except Exception:
        pass
    return False


def _click_button(window, keywords: list[str]) -> bool:
    try:
        for btn in window.descendants(control_type="Button"):
            try:
                txt = btn.window_text().lower().strip()
                if txt in keywords or any(kw in txt for kw in keywords):
                    btn.click_input()
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _handle_install_dialog_pyautogui(game_name: str, best_drive: dict) -> str:
    try:
        import pyautogui
        import pygetwindow as gw
    except ImportError:
        return (f"Install dialog opened for '{game_name}'. "
                f"Please select '{best_drive['letter']}:' and click Install manually.")

    pyautogui.FAILSAFE = False
    drive_label = f"{best_drive['letter']}:"
    install_win = None

    for _ in range(30):
        time.sleep(0.5)
        for w in gw.getAllWindows():
            if ("install" in w.title.lower() or "steam" in w.title.lower()) \
                    and w.width > 300 and w.visible:
                install_win = w
                break
        if install_win:
            break

    if not install_win:
        return f"Please select '{drive_label}' and click Install in Steam for '{game_name}'."

    try:
        install_win.activate()
        time.sleep(0.4)
    except Exception:
        pass

    wx, wy = install_win.left, install_win.top
    ww, wh = install_win.width, install_win.height
    pyautogui.click(wx + int(ww * 0.35), wy + int(wh * 0.45))
    time.sleep(0.2)
    pyautogui.typewrite(best_drive["letter"], interval=0.05)
    time.sleep(0.2)
    pyautogui.click(wx + int(ww * 0.72), wy + int(wh * 0.88))
    return f"Attempted drive {drive_label} selection and Install click for '{game_name}'."


def _handle_install_dialog(game_name: str) -> str:
    best_drive = _find_best_drive()
    if not best_drive:
        return f"Install dialog opened for '{game_name}'. Could not detect drives."

    drive_letter = best_drive["letter"]
    drive_label  = f"{drive_letter}:"
    print(f"[GameUpdater] 🏆 Target drive: {drive_label} ({best_drive['free_gb']:.1f} GB free)")

    try:
        from pywinauto import Application, findwindows
        dialog = None

        # NOTE: this drives Steam's native installer dialog by matching its
        # on-screen button/title text, which is localized. We match English and
        # Turkish labels (install/yükle, next/ileri, ok/tamam). On a Steam client
        # set to another language the match simply fails and the automation
        # reports it — it never crashes, and manual install still works.
        for _ in range(40):
            time.sleep(0.5)
            try:
                for hwnd in findwindows.find_windows(
                    title_re=r"(?i)(install|yükle|steam)", visible_only=True
                ):
                    try:
                        app  = Application(backend="uia").connect(handle=hwnd)
                        win  = app.window(handle=hwnd)
                        rect = win.rectangle()
                        if win.is_visible() and rect.width() > 300 and rect.height() > 200:
                            all_text = " ".join(
                                c.window_text() for c in win.descendants()
                                if c.window_text()
                            ).upper()
                            if any(x in all_text for x in
                                   ("C:", "D:", "E:", "F:", "INSTALL", "YÜKLE")):
                                dialog = win
                                break
                    except Exception:
                        continue
            except Exception:
                pass
            if dialog:
                break

        if not dialog:
            raise RuntimeError("Dialog not found")

        dialog.set_focus()
        time.sleep(0.4)
        drive_selected  = _select_drive_in_dialog(dialog, drive_letter)
        install_clicked = _click_button(
            dialog, ["install", "yükle", "next", "ileri", "ok", "tamam"]
        )

        if install_clicked:
            suffix = f"Selected {drive_label} and" if drive_selected else "Default drive used, but"
            return f"{suffix} clicked Install for '{game_name}'."
        return f"Please click Install manually in Steam for '{game_name}'."

    except ImportError:
        return _handle_install_dialog_pyautogui(game_name, best_drive)
    except Exception as e:
        print(f"[GameUpdater] ⚠️ pywinauto failed: {e}")
        return _handle_install_dialog_pyautogui(game_name, best_drive)

def _ensure_steam_running(steam_path: Path) -> bool:
    if _is_steam_running():
        return True

    exe = _steam_exe(steam_path)
    if not exe.exists():
        print(f"[GameUpdater] ❌ Steam not found: {exe}")
        return False

    print("[GameUpdater] 🚀 Starting Steam...")
    if is_mac():
        subprocess.Popen(["open", "-a", "Steam"])
    else:
        subprocess.Popen([str(exe)])

    for _ in range(20):
        time.sleep(1)
        if _is_steam_running():
            print("[GameUpdater] ✅ Steam is running")
            time.sleep(4)
            if is_windows():
                _handle_steam_profile_selection()
                time.sleep(2)
            return True

    print("[GameUpdater] ⚠️ Could not start Steam")
    return False

def _search_steam_appid(game_name: str) -> tuple[str | None, str | None]:
    name_lower = game_name.lower().strip()


    steam_path = _find_steam_path()
    if steam_path:
        for g in _get_steam_games(steam_path):
            if name_lower in g["name"].lower():
                return g["id"], g["name"]

    if name_lower in _KNOWN_APPIDS:
        app_id, canonical = _KNOWN_APPIDS[name_lower]
        print(f"[GameUpdater] 📖 Bilinen: {canonical} ({app_id})")
        return app_id, canonical

    for key, (app_id, canonical) in _KNOWN_APPIDS.items():
        if name_lower in key or key in name_lower:
            print(f"[GameUpdater] 📖 Partial match: {canonical} ({app_id})")
            return app_id, canonical

    try:
        import urllib.request, urllib.parse
        query = urllib.parse.quote(game_name)
        url   = f"https://store.steampowered.com/api/storesearch/?term={query}&l=english&cc=US"
        req   = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            items = json.loads(resp.read().decode()).get("items", [])
        if items:
            best = items[0]
            print(f"[GameUpdater] 🌐 Store API: {best['name']} ({best['id']})")
            return str(best["id"]), best["name"]
    except Exception as e:
        print(f"[GameUpdater] ⚠️ AppID lookup failed: {e}")

    return None, None

def _install_steam_game(steam_path: Path, game_name: str = None,
                        app_id: str = None, speak=None,
                        wait_seconds: float = 30.0) -> str:
    """Ask Steam to install a game and *verify* that Steam picked it up.

    The launch below is only a request: Steam can still open a "where should
    this go?" dialog that a human has to answer, or ignore the URL entirely.
    The manifest file is the ground truth, so success is only claimed once the
    AppID shows up in it — otherwise the honest "could not verify" sentence is
    returned instead (the caller has already taken the user's confirmation).
    """
    if not _ensure_steam_running(steam_path):
        return "Could not start Steam."

    exe             = _steam_exe(steam_path)
    installed_games = _get_steam_games(steam_path)

    already = None
    if app_id:
        already = next((g for g in installed_games if g["id"] == str(app_id)), None)
    elif game_name:
        name_lower = game_name.lower()
        already    = next((g for g in installed_games
                           if name_lower in g["name"].lower()), None)
    else:
        return "Please specify a game name or AppID."

    if already:
        state = already["state"]
        name  = already["name"]
        if state == 4:
            return f"'{name}' is already installed and up to date."
        if state == 1026:
            return f"'{name}' is currently downloading or updating."
        if state in (6, 516):
            _launch_steam_url(exe, f"steam://update/{already['id']}")
            return f"'{name}' has a pending update. Update started."
        return f"'{name}' is already installed."

    if not app_id and game_name:
        found_id, found_name = _search_steam_appid(game_name)
        if not found_id:
            return (f"Could not find '{game_name}' on Steam. "
                    f"Try providing the AppID directly.")
        app_id    = found_id
        game_name = found_name or game_name
        print(f"[GameUpdater] install request: {game_name} (AppID: {app_id})")

    task = tasks.start(
        tasks.KIND_INSTALL,
        f"Install {game_name or app_id}",
        detail="asking Steam to open the install dialog",
        app_id=str(app_id),
    )
    try:
        _launch_steam_url(exe, f"steam://install/{app_id}")

        if is_windows():
            threading.Thread(
                target=_handle_install_dialog,
                args=(game_name or str(app_id),),
                daemon=True
            ).start()

        # A queued download appears in the manifest within seconds; a dialog
        # that nobody answers never does, and that must be said out loud.
        ok, entry, why = _await_steam_state(
            steam_path, app_id, {4, 6, 516, 1026},
            timeout=wait_seconds, task=task, title=game_name or str(app_id),
        )
        if ok and entry is not None:
            state = int(entry.get("state", 0))
            name = entry.get("name") or game_name or app_id
            if state == 1026:
                # The tool is done; the download is not. The row stays open and
                # follows Steam's manifest to the end (see _watch_steam_download).
                _track_steam_download(task, steam_path, app_id, name)
                return f"Steam is downloading '{name}' now."
            if state == 4:
                sentence = f"'{name}' is installed and up to date."
            else:
                sentence = (f"Steam has '{name}' queued "
                            f"({_steam_state_text(state)}).")
            task.finish(detail=f"{name}: {_steam_state_text(state)}")
            return sentence

        task.fail(error="Steam did not start the download",
                  detail=f"no manifest entry for AppID {app_id} appeared")
        return install_safety.unverified(
            "install", game_name or str(app_id),
            "Steam accepted my request") + (
            " (If a Steam window is asking where to install it, answering that "
            "window is what starts the download.)")
    except Exception as e:
        task.fail(error=f"{type(e).__name__}: {e}")
        return f"Install failed: {e}"


def _update_steam_games(steam_path: Path, game_name: str = None,
                        wait_seconds: float = 15.0) -> str:
    """Trigger Steam updates and report only what the manifests confirm."""
    if not _ensure_steam_running(steam_path):
        return "Could not start Steam."

    exe   = _steam_exe(steam_path)
    games = _get_steam_games(steam_path)
    if not games:
        return "No Steam games found."

    if game_name:
        name_lower = game_name.lower()
        targets    = [g for g in games if name_lower in g["name"].lower()]
        if not targets:
            available = ", ".join(g["name"] for g in games[:5])
            return f"Game '{game_name}' not found. Installed: {available}..."
    else:
        targets = games

    task = tasks.start(
        tasks.KIND_UPDATE,
        f"Update {game_name}" if game_name else f"Update {len(targets)} Steam game(s)",
        detail="checking Steam for updates",
        games=len(targets),
    )

    already_updated, already_running = [], []
    verified, unverified, errors = [], [], []
    watching = False          # one download is followed by the watcher thread

    for index, game in enumerate(targets, start=1):
        state = game["state"]
        name  = game["name"]
        task.update(progress=(index - 1) / max(1, len(targets)),
                    detail=f"checking {name} ({index}/{len(targets)})")
        if state == 4:
            already_updated.append(name)
            continue
        if state == 1026:
            already_running.append(name)
            if len(targets) == 1:
                # A single game was asked for: report its own numbers until
                # Steam says it is done, instead of closing the row now.
                _track_steam_download(task, steam_path, game["id"], name)
                watching = True
            continue
        try:
            _launch_steam_url(exe, f"steam://update/{game['id']}")
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        # `steam://update/…` is a request, not a result. The manifest decides:
        # 1026 means Steam is downloading it now, 4 means it is finished (or
        # was already current). Anything else stays unverified on purpose.
        ok, entry, _why = _await_steam_state(
            steam_path, game["id"], {4, 1026},
            timeout=wait_seconds, task=task, title=name,
        )
        if ok and entry is not None:
            verified.append(f"{name} ({_steam_state_text(entry.get('state'))})")
            if int(entry.get("state", 0) or 0) == 1026 and len(targets) == 1:
                _track_steam_download(task, steam_path, game["id"], name)
                watching = True
        else:
            unverified.append(name)

    parts = []
    if verified:
        names  = ", ".join(verified[:3])
        suffix = f" and {len(verified) - 3} more" if len(verified) > 3 else ""
        parts.append(f"Steam confirmed updates for: {names}{suffix}.")
    if already_running:
        parts.append(f"Already updating: {', '.join(already_running)}.")
    if already_updated:
        parts.append(
            f"{already_updated[0]} is already up to date."
            if game_name else
            f"{len(already_updated)} game(s) already up to date."
        )
    if unverified:
        names = ", ".join(unverified[:3])
        suffix = f" (+{len(unverified) - 3} more)" if len(unverified) > 3 else ""
        parts.append(install_safety.unverified(
            "update", f"{names}{suffix}", "Steam accepted my request"))
    if errors:
        parts.append(f"Errors: {'; '.join(errors)}.")

    if unverified or errors:
        task.fail(error=f"{len(unverified)} update(s) could not be verified",
                  detail="; ".join(unverified[:3]) or "launch error")
    elif watching:
        # The download watcher owns this row; it closes when Steam says done.
        pass
    else:
        task.finish(detail=f"{len(verified)} update(s) confirmed by Steam")
    return " ".join(parts) if parts else "No games to update."

def _get_download_status(steam_path: Path) -> str:
    games   = _get_steam_games(steam_path)
    active  = [g for g in games if g["state"] == 1026]
    pending = [g for g in games if g["state"] in (6, 516)]
    lines   = []
    if active:
        lines.append(f"Downloading: {', '.join(g['name'] for g in active)}.")
    if pending:
        names  = ", ".join(g["name"] for g in pending[:5])
        suffix = f" and {len(pending) - 5} more" if len(pending) > 5 else ""
        lines.append(f"Pending updates: {names}{suffix}.")
    return " ".join(lines) if lines else "No active downloads or pending updates."


def _system_shutdown() -> None:
    if is_windows():
        subprocess.run(["shutdown", "/s", "/t", "10"], **_CNW)
    elif is_mac():
        subprocess.run(["osascript", "-e", 'tell app "System Events" to shut down'])
    else:
        subprocess.run(["systemctl", "poweroff"])


def _arm_auto_shutdown(steam_path: Path, speak=None) -> str:
    """Park the shutdown-after-download watcher behind the on-screen gate.

    Powering the machine off is irreversible — the same category as the
    restart/shutdown actions in computer_settings — so a human presses a
    button, or this does not happen. The download itself already started;
    only the shutdown afterwards waits for approval. With no interface
    bound, confirm.request refuses fail-closed instead of arming.
    """
    def _start() -> str:
        threading.Thread(
            target=_watch_and_shutdown,
            kwargs={"steam_path": steam_path, "speak": speak},
            daemon=True,
        ).start()
        return ("Auto-shutdown armed: the PC will shut down "
                "when the download finishes.")

    if confirm.pending_title():
        return ("There is already a confirmation waiting on screen. Ask the "
                "user to answer that one first — auto-shutdown was not armed.")
    return confirm.request(
        key="game_shutdown",
        title="Shut this computer down when the download finishes",
        detail=("Steam keeps downloading. Once it is done the computer powers "
                "off — anything unsaved will be lost."),
        run=_start,
    )


def _watch_and_shutdown(steam_path: Path, speak=None,
                        check_interval: int = 30, timeout_hours: int = 12):
    """Power the machine off once Steam reports no download left.

    The watcher reports itself in the activity panel for its whole lifetime —
    an armed auto-shutdown the user cannot see is a nasty surprise. Every exit
    path says out loud what happened, including "the download never started",
    which used to return silently and leave a person waiting for a shutdown
    that was never going to come.
    """
    print("[GameUpdater] watch-and-shutdown armed")
    task = tasks.start(tasks.KIND_TASK, "Shut down after the download",
                       detail="watching Steam for the download to start")
    deadline = time.time() + timeout_hours * 3600

    def _downloading() -> list[dict]:
        try:
            return [g for g in _get_steam_games(steam_path) if g["state"] == 1026]
        except Exception:
            return []

    for _ in range(24):
        time.sleep(5)
        active = _downloading()
        if active:
            names = ", ".join(g["name"] for g in active)
            task.update(detail=f"downloading {names}", progress=None)
            if speak:
                speak(f"Download started for {names}. I'll shut down when done.")
            break
    else:
        task.fail(error="no download started",
                  detail="nothing was downloading after two minutes")
        if speak:
            speak("Nothing started downloading, so the computer stays on — "
                  "the auto-shutdown was cancelled.")
        return

    while time.time() < deadline:
        time.sleep(check_interval)
        active = _downloading()
        if not active:
            if speak:
                speak("Download complete. Shutting down now.")
            task.finish(detail="download finished — shutting down")
            time.sleep(5)
            _system_shutdown()
            return
        task.update(detail="downloading " + ", ".join(g["name"] for g in active))

    task.fail(error="download did not finish in time",
              detail=f"gave up after {timeout_hours} h")
    if speak:
        speak("Download taking too long. Cancelling auto-shutdown.")


def _find_epic_exe() -> Path | None:
    if is_windows(): return _find_epic_exe_windows()
    if is_mac():     return _find_epic_exe_mac()
    return _find_epic_exe_linux()


def _find_epic_exe_windows() -> Path | None:
    try:
        import winreg
        for hive, key_path in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\EpicGames\EpicGamesLauncher"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\EpicGames\EpicGamesLauncher"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\EpicGames\EpicGamesLauncher"),
        ]:
            try:
                key = winreg.OpenKey(hive, key_path)
                val, _ = winreg.QueryValueEx(key, "AppDataPath")
                winreg.CloseKey(key)
                exe = Path(val) / "Binaries" / "Win64" / "EpicGamesLauncher.exe"
                if exe.exists():
                    return exe
            except Exception:
                continue
    except ImportError:
        pass
    for candidate in [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Epic Games" / "Launcher" / "Portal" / "Binaries" / "Win64" / "EpicGamesLauncher.exe",
        Path(os.environ.get("ProgramFiles", ""))       / "Epic Games" / "Launcher" / "Portal" / "Binaries" / "Win64" / "EpicGamesLauncher.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))        / "EpicGamesLauncher" / "Portal" / "Binaries" / "Win64" / "EpicGamesLauncher.exe",
    ]:
        if candidate.exists():
            return candidate
    return None


def _find_epic_exe_mac() -> Path | None:
    p = Path("/Applications/Epic Games Launcher.app/Contents/MacOS/EpicGamesLauncher")
    return p if p.exists() else None


def _find_epic_exe_linux() -> Path | None:
    for c in [Path.home() / ".local" / "bin" / "heroic", Path("/usr/bin/heroic")]:
        if c.exists():
            return c
    return None


def _epic_manifests_path() -> Path | None:
    if is_windows():
        p = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) \
            / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        return p if p.exists() else None
    if is_mac():
        p = Path.home() / "Library" / "Application Support" \
            / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        return p if p.exists() else None
    return None  


def _get_epic_games() -> list[dict]:
    manifests = _epic_manifests_path()
    if not manifests:
        return []
    games = []
    for item_file in manifests.glob("*.item"):
        try:
            data = json.loads(item_file.read_text(encoding="utf-8"))
            name = data.get("DisplayName") or data.get("AppName", "")
            if name:
                games.append({"id": data.get("AppName", ""), "name": name})
        except Exception:
            continue
    return games


def _is_epic_running() -> bool:
    try:
        if is_windows():
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq EpicGamesLauncher.exe"],
                capture_output=True, text=True, **_CNW
            ).stdout
            return "epicgameslauncher.exe" in out.lower()
        proc = "EpicGamesLauncher" if is_mac() else "heroic"
        return bool(subprocess.run(["pgrep", "-x", proc],
                                   capture_output=True, text=True).stdout.strip())
    except Exception:
        return False


def _update_epic_games(epic_exe: Path, game_name: str = None) -> str:
    games = _get_epic_games()

    if game_name:
        name_lower = game_name.lower()
        matched    = [g for g in games if name_lower in g["name"].lower()]
        if not matched:
            return f"'{game_name}' not found in Epic."
        try:
            url = f"com.epicgames.launcher://apps/{matched[0]['id']}?action=launch&silent=true"
            if is_mac():
                subprocess.Popen(["open", url])
            elif is_linux():
                subprocess.Popen([str(epic_exe), url] if epic_exe else ["xdg-open", url])
            else:
                subprocess.Popen([str(epic_exe), url])
            return f"Opened Epic for '{matched[0]['name']}'."
        except Exception as e:
            return f"Epic update failed: {e}"
    else:
        try:
            if is_mac():
                subprocess.Popen(["open", "-a", "Epic Games Launcher"])
            elif is_linux():
                if epic_exe:
                    subprocess.Popen([str(epic_exe)])
                else:
                    return ("Epic Games is not natively supported on Linux. "
                            "Consider using Heroic Launcher.")
            else:

                if _is_epic_running():
                    for g in games[:10]:
                        subprocess.Popen([str(epic_exe),
                            f"com.epicgames.launcher://apps/{g['id']}?action=launch&silent=true"])
                        time.sleep(0.5)
                    # Epic exposes no per-game download state to other programs,
                    # so "triggered update for N games" would be a claim nobody
                    # can check. Say what was actually done instead.
                    return (f"Asked the Epic Games Launcher to open {len(games)} "
                            f"game(s); Epic does not report download state to "
                            f"other apps, so I cannot confirm any of them "
                            f"started. Check the launcher's Downloads page.")
                else:
                    subprocess.Popen([str(epic_exe)])
            count = len(games)
            return (f"Epic Games Launcher opened. {count} game(s) will be checked."
                    if count else "Epic Games Launcher opened.")
        except Exception as e:
            return f"Epic launch failed: {e}"

def _schedule_daily_update(hour: int = 3, minute: int = 0) -> str:
    if is_windows(): return _schedule_windows(hour, minute)
    if is_mac():     return _schedule_mac(hour, minute)
    return _schedule_linux(hour, minute)


def _schedule_windows(hour: int, minute: int) -> str:
    task_name   = "JARVIS_GameUpdater"
    script_path = Path(__file__).resolve()
    subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"], capture_output=True, **_CNW)
    # Deliberately a plain per-user task: the scheduled command runs this
    # user-writable .py file, so registering it as SYSTEM/HIGHEST would turn
    # every future edit of this file into SYSTEM code execution. Steam
    # updates are per-user work and need no elevation.
    cmd    = ["schtasks", "/Create", "/TN", task_name,
              "/TR", f'"{sys.executable}" "{script_path}" --scheduled',
              "/SC", "DAILY", "/ST", f"{hour:02d}:{minute:02d}", "/F"]
    result = subprocess.run(cmd, capture_output=True, text=True, **_CNW)
    if result.returncode == 0:
        return f"Daily game update scheduled at {hour:02d}:{minute:02d}."
    return f"Scheduling failed: {result.stderr.strip()}"


def _schedule_mac(hour: int, minute: int) -> str:
    plist_dir   = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path  = plist_dir / "com.jarvis.gameupdater.plist"
    script_path = Path(__file__).resolve()
    from xml.sax.saxutils import escape as _xml_escape
    _exe_xml, _script_xml = _xml_escape(sys.executable), _xml_escape(str(script_path))
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.jarvis.gameupdater</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_exe_xml}</string>
        <string>{_script_xml}</string>
        <string>--scheduled</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>{hour}</integer>
        <key>Minute</key><integer>{minute}</integer>
    </dict>
    <key>RunAtLoad</key><false/>
</dict></plist>"""
    try:
        plist_path.write_text(plist_content, encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        result = subprocess.run(["launchctl", "load", str(plist_path)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return f"Daily game update scheduled at {hour:02d}:{minute:02d} via launchd."
        return f"Scheduling failed: {result.stderr.strip()}"
    except Exception as e:
        return f"Scheduling failed: {e}"


def _schedule_linux(hour: int, minute: int) -> str:
    script_path = Path(__file__).resolve()
    marker      = "# JARVIS_GameUpdater"
    cron_entry  = f"{minute} {hour} * * * {sys.executable} {script_path} --scheduled  {marker}"
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        lines    = [l for l in existing.stdout.splitlines()
                    if marker not in l and str(script_path) not in l]
        lines.append(cron_entry)
        proc = subprocess.run(["crontab", "-"],
                              input="\n".join(lines) + "\n",
                              text=True, capture_output=True)
        if proc.returncode == 0:
            return f"Daily game update scheduled at {hour:02d}:{minute:02d} via cron."
        return f"Scheduling failed: {proc.stderr.strip()}"
    except Exception as e:
        return f"Scheduling failed: {e}"


def _cancel_scheduled_update() -> str:
    if is_windows():
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", "JARVIS_GameUpdater", "/F"],
            capture_output=True, text=True, **_CNW
        )
        return ("Scheduled update cancelled."
                if result.returncode == 0 else "No scheduled update found.")
    if is_mac():
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.jarvis.gameupdater.plist"
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            plist_path.unlink()
            return "Scheduled update cancelled."
        return "No scheduled update found."

    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        lines    = [l for l in existing.stdout.splitlines()
                    if "JARVIS_GameUpdater" not in l]
        subprocess.run(["crontab", "-"],
                       input="\n".join(lines) + "\n", text=True)
        return "Scheduled update cancelled."
    except Exception as e:
        return f"Cancel failed: {e}"


def _get_schedule_status() -> str:
    if is_windows():
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", "JARVIS_GameUpdater", "/FO", "LIST"],
            capture_output=True, text=True, **_CNW
        )
        if result.returncode != 0:
            return "No scheduled game update found."
        for line in result.stdout.strip().splitlines():
            if any(k in line for k in
                   ("Next Run", "Sonraki", "Prochaine", "Próxima", "Nächste")):
                return f"Game update scheduled. {line.strip()}"
        return "Game update is scheduled."
    if is_mac():
        plist_path = (Path.home() / "Library" / "LaunchAgents"
                      / "com.jarvis.gameupdater.plist")
        return ("Game update is scheduled via launchd."
                if plist_path.exists() else "No scheduled game update found.")

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if "JARVIS_GameUpdater" in result.stdout:
            for line in result.stdout.splitlines():
                if "JARVIS_GameUpdater" in line:
                    return f"Game update is scheduled: {line.split('#')[0].strip()}"
        return "No scheduled game update found."
    except Exception:
        return "No scheduled game update found."


def game_updater(parameters: dict, player=None, speak=None) -> str:
    p         = parameters or {}
    action    = str(p.get("action") or "update").lower().strip()
    platform  = str(p.get("platform") or "both").lower().strip()
    game_name = str(p.get("game_name") or "").strip() or None
    app_id    = str(p.get("app_id") or "").strip() or None
    try:
        hour = max(0, min(23, int(p.get("hour", 3))))
    except (TypeError, ValueError):
        hour = 3
    try:
        minute = max(0, min(59, int(p.get("minute", 0))))
    except (TypeError, ValueError):
        minute = 0
    shutdown  = str(p.get("shutdown_when_done", "false")).lower() == "true"

    results = []

    if action == "schedule":        return _schedule_daily_update(hour=hour, minute=minute)
    if action == "cancel_schedule": return _cancel_scheduled_update()
    if action == "schedule_status": return _get_schedule_status()

    if action == "list":
        if platform in ("steam", "both"):
            steam_path = _find_steam_path()
            if steam_path:
                games = _get_steam_games(steam_path)
                if games:
                    names  = ", ".join(g["name"] for g in games[:8])
                    suffix = f" and {len(games) - 8} more" if len(games) > 8 else ""
                    results.append(f"Steam ({len(games)} games): {names}{suffix}.")
                else:
                    results.append("Steam: No games found.")
            else:
                results.append("Steam: Not installed.")
        if platform in ("epic", "both"):
            if is_linux():
                results.append("Epic: Not natively supported on Linux.")
            else:
                games = _get_epic_games()
                if games:
                    names  = ", ".join(g["name"] for g in games[:8])
                    suffix = f" and {len(games) - 8} more" if len(games) > 8 else ""
                    results.append(f"Epic ({len(games)} games): {names}{suffix}.")
                else:
                    results.append("Epic: No games found.")
        return " | ".join(results) or "No platforms found."

    if action == "download_status":
        if platform in ("steam", "both"):
            steam_path = _find_steam_path()
            results.append(
                _get_download_status(steam_path) if steam_path else "Steam: Not installed."
            )
        if platform in ("epic", "both"):
            results.append("Epic download status not available directly.")
        return " ".join(results)

    if action in ("install", "update"):
        if action == "install" and not (game_name or app_id):
            return "Steam: Please specify a game name to install."

        # Installing or updating a game writes to disk and pulls hundreds of
        # megabytes, so it goes through the same gate as every other install in
        # the app (core/install_safety.py): the human presses CONFIRM on the
        # HUD, the work then runs on the confirmation worker and reports its
        # verified outcome out loud. `steam://` URLs are a request to Steam,
        # never a result — core/install_safety.verify() reads the manifests.
        request = install_safety.InstallRequest(
            kind=(install_safety.KIND_INSTALL if action == "install"
                  else install_safety.KIND_UPDATE),
            target=game_name or app_id or f"{platform} game libraries",
            detail=(f"Steam/Epic will download or update "
                    f"{game_name or app_id or 'all installed games'} on this "
                    f"machine."),
            source="user",
            origin="game_updater",
        )
        return install_safety.guard(
            request,
            run=lambda: _announce(speak, _perform_install_or_update(
                action, platform, game_name, app_id, shutdown, speak, player)),
            key=f"game_{action}",
        )

    return f"Unknown action: '{action}'."


def _perform_install_or_update(action: str, platform: str, game_name: str | None,
                               app_id: str | None, shutdown: bool,
                               speak=None, player=None) -> str:
    """The confirmed half of install/update: do it, then report what happened."""
    results = []

    if platform in ("steam", "both"):
        steam_path = _find_steam_path()
        if not steam_path:
            results.append("Steam: Not installed.")
        else:
            if game_name:
                installed  = _get_steam_games(steam_path)
                name_lower = game_name.lower()
                is_installed = any(
                    name_lower in g["name"].lower() for g in installed
                )
                if not is_installed:
                    msg = _install_steam_game(
                        steam_path, game_name=game_name, app_id=app_id
                    )
                    if shutdown:
                        msg += " " + _arm_auto_shutdown(steam_path, speak=speak)
                    if player: player.write_log(f"[GameUpdater] {msg[:100]}")
                    return msg
                results.append(_update_steam_games(steam_path, game_name=game_name))
            else:
                results.append(_update_steam_games(steam_path))

            if shutdown:
                results.append(_arm_auto_shutdown(steam_path, speak=speak))

    if platform in ("epic", "both"):
        if is_linux():
            results.append(
                "Epic: Not natively supported on Linux. Use Heroic Launcher."
            )
        else:
            epic_exe = _find_epic_exe()
            if epic_exe:
                results.append(
                    f"Epic: {_update_epic_games(epic_exe, game_name=game_name)}"
                )
            else:
                results.append("Epic: Not installed.")

    output = " | ".join(results) or "Nothing to do."
    if player: player.write_log(f"[GameUpdater] {output[:100]}")
    return output


if __name__ == "__main__":
    if "--scheduled" in sys.argv:
        print(f"[GameUpdater] 🕐 Scheduled run at {datetime.now().strftime('%H:%M')}")
        result = game_updater({"action": "update", "platform": "both"})
        print(f"[GameUpdater] ✅ {result}")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "game_updater",
    "description": "THE ONLY tool for ANY Steam or Epic Games request. Use for: installing, downloading, updating games, listing installed games, checking download status, scheduling updates. ALWAYS call directly for any Steam/Epic/game request. NEVER use browser_control or web_search for Steam/Epic.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"
            },
            "platform": {
                "type": "STRING",
                "description": "steam | epic | both (default: both)"
            },
            "game_name": {
                "type": "STRING",
                "description": "Game name (partial match supported)"
            },
            "app_id": {
                "type": "STRING",
                "description": "Steam AppID for install (optional)"
            },
            "hour": {
                "type": "INTEGER",
                "description": "Hour for scheduled update 0-23 (default: 3)"
            },
            "minute": {
                "type": "INTEGER",
                "description": "Minute for scheduled update 0-59 (default: 0)"
            },
            "shutdown_when_done": {
                "type": "BOOLEAN",
                "description": ("Shut down the PC when the download finishes. "
                                "The user must approve this on screen first.")
            }
        },
        "required": []
    },
    "handler": game_updater,
}
