"""System-wide default playback device switching.

`audio_manager`'s existing `set_input`/`set_output` choose which device *this
process* (JARVIS's own speech pipeline) uses. That is a different, narrower
thing than what "switch my audio output to my headset" usually means: making
a device the default for the whole operating system, so every application
picks it up, not just JARVIS.

Windows has no public Win32 API for changing the system default endpoint —
even the Settings app and the classic Sound control panel go through the same
undocumented `IPolicyConfig` COM interface this module talks to. Because it
is undocumented, its exact method layout has drifted across Windows versions
in community reverse-engineering; two known-working layouts are tried in
order and the failure is reported honestly if neither applies, rather than
guessing that a change took effect.
"""
from __future__ import annotations

import platform
import subprocess

from core.text_match import partial_ratio

_OS = platform.system()

# A caller cannot fix a raw COM ArgumentException; only these two layouts are
# known to actually work across real Windows releases, so nothing narrower is
# worth catching individually.
_ERole = {"console": 0, "multimedia": 1, "communications": 2}

_MATCH_THRESHOLD = 0.45


def _best_device_match(name: str, devices: list[tuple[str, str]]):
    """Best (label, id) for a spoken fragment against real device names.

    A user says "jbl" or "headset", never the exact string Windows shows
    ("JBL Quantum 400 Wireless"), so this needs the same "is my short guess
    contained in the long real name" tolerance `window_manager` and
    `layout_manager` already use for window titles — a plain full-string
    ratio would score a short query against a long name far too low.
    """
    query = name.casefold()
    best = None
    best_score = _MATCH_THRESHOLD
    for label, device_id in devices:
        score = partial_ratio(query, label.casefold())
        if score > best_score:
            best, best_score = (label, device_id), score
    return best


def list_playback_devices() -> list[str]:
    """Friendly names of active playback (output) devices on this system."""
    return [label for label, _device_id in _list_playback_endpoints()]


def _list_playback_endpoints() -> list[tuple[str, str]]:
    """(label, id) pairs: the id is what the OS call actually needs, which is
    not always the same string as the friendly name shown to the user."""
    if _OS == "Windows":
        return _list_windows_playback_devices()
    if _OS == "Darwin":
        return _list_macos_playback_devices()
    return _list_linux_playback_devices()


def get_default_playback_device() -> str | None:
    """The system default playback device's friendly/sink name, if readable."""
    try:
        if _OS == "Windows":
            from pycaw.pycaw import AudioUtilities
            speakers = AudioUtilities.GetSpeakers()
            name = str(getattr(speakers, "FriendlyName", "") or "")
            return name or None
        if _OS == "Darwin":
            result = subprocess.run(
                ["SwitchAudioSource", "-c", "-t", "output"],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip() or None
        result = subprocess.run(
            ["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def set_default_playback_device(name: str) -> tuple[bool, str]:
    """Fuzzy-match `name` against active output devices and make it the default.

    Returns (ok, message). `ok` is only True after a real OS/COM call reports
    success — a device that merely sounds similar but was never applied is
    always reported as a failure, never assumed.
    """
    name = str(name or "").strip()
    if not name:
        return False, "Tell me the speaker or headset name, or say list audio devices."

    devices = _list_playback_endpoints()
    if not devices:
        return False, (
            "No playback devices could be listed on this system, so I would "
            "be switching blind. Nothing was changed."
        )
    match = _best_device_match(name, devices)
    if not match:
        shown = ", ".join(label for label, _id in devices[:6])
        return False, (
            f"No playback device close to '{name}' was found. "
            f"Available: {shown}."
        )
    label, device_id = match


    if _OS == "Windows":
        ok, detail = _set_windows_default(device_id)
    elif _OS == "Darwin":
        ok, detail = _set_macos_default(device_id)
    else:
        ok, detail = _set_linux_default(device_id)

    if ok:
        return True, f"System audio output switched to {label}."
    return False, detail or f"The system refused to switch the output device to {label}."


# ── Windows: pycaw for enumeration, raw IPolicyConfig COM for switching ──────

def _list_windows_playback_devices() -> list[tuple[str, str]]:
    try:
        from pycaw.pycaw import AudioUtilities
        devices = AudioUtilities.GetAllDevices()
    except Exception:
        return []
    results = []
    for device in devices:
        try:
            # state 1 == DEVICE_STATE_ACTIVE; anything else (disabled,
            # unplugged, not present) cannot become the default.
            if int(getattr(device, "state", 1)) != 1:
                continue
            name = str(getattr(device, "FriendlyName", "") or "")
            device_id = str(getattr(device, "id", "") or "")
            if name and device_id:
                results.append((name, device_id))
        except Exception:
            continue
    return results


def _set_windows_default(device_id: str) -> tuple[bool, str]:
    """Apply via IPolicyConfig, trying the two layouts known to work.

    Both use the same CLSID/IID pair used by most modern (Windows 7+) forks
    first, then the original Vista-era interface, since real installations
    are inconsistent about which one their audio stack actually implements.
    """
    attempts = (
        ("{f8679f50-850a-41cf-9c72-430f290290c8}", "{870af99c-171d-4f9e-af0d-e63df40c2bc9}", 10),
        ("{568b9108-44bf-40b4-9006-86afe5b5a620}", "{294935ce-f637-4e7c-a41b-ab255460b862}", 9),
    )
    last_error = ""
    for iid, clsid, stub_count in attempts:
        try:
            _apply_policy_config(device_id, iid, clsid, stub_count)
            return True, ""
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return False, f"Windows refused the default-device change ({last_error})."


def _apply_policy_config(device_id: str, iid: str, clsid: str, stub_count: int) -> None:
    import comtypes
    from comtypes import GUID, COMMETHOD, HRESULT, IUnknown
    from ctypes import c_wchar_p, c_int

    try:
        comtypes.CoInitialize()
    except OSError:
        pass  # already initialised on this thread; not an error

    stub_methods = [
        COMMETHOD([], HRESULT, f"_reserved{i}") for i in range(stub_count)
    ]

    class _IPolicyConfig(IUnknown):
        _iid_ = GUID(iid)
        _methods_ = stub_methods + [
            COMMETHOD(
                [], HRESULT, "SetDefaultEndpoint",
                (["in"], c_wchar_p, "device_id"),
                (["in"], c_int, "role"),
            ),
        ]

    policy_config = comtypes.CoCreateInstance(GUID(clsid), _IPolicyConfig, comtypes.CLSCTX_ALL)
    for role in _ERole.values():
        result = policy_config.SetDefaultEndpoint(device_id, role)
        if result not in (0, None):
            raise OSError(f"SetDefaultEndpoint returned 0x{result & 0xFFFFFFFF:08X}")


# ── macOS: SwitchAudioSource (widely-installed third-party CLI) ─────────────

def _list_macos_playback_devices() -> list[tuple[str, str]]:
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-a", "-t", "output"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []
    return [(line.strip(), line.strip()) for line in result.stdout.splitlines() if line.strip()]


def _set_macos_default(device_id: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-s", device_id, "-t", "output"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        return False, (
            f"Could not switch output ({type(exc).__name__}). "
            "SwitchAudioSource may not be installed (brew install switchaudio-osx)."
        )
    if result.returncode != 0:
        return False, "macOS refused to switch the output device."
    return True, ""


# ── Linux: PulseAudio/PipeWire's pactl ───────────────────────────────────────

def _list_linux_playback_devices() -> list[tuple[str, str]]:
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []
    devices = []
    for line in result.stdout.splitlines():
        columns = line.split("\t")
        if len(columns) >= 2 and columns[1].strip():
            sink_name = columns[1].strip()
            devices.append((sink_name, sink_name))
    return devices


def _set_linux_default(device_id: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["pactl", "set-default-sink", device_id],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        return False, f"Could not switch output ({type(exc).__name__})."
    if result.returncode != 0:
        return False, "PulseAudio/PipeWire refused to switch the output device."
    return True, ""
