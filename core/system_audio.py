"""Windows system-wide default playback-device switching."""
from __future__ import annotations

from core.text_match import partial_ratio

_ERole = {"console": 0, "multimedia": 1, "communications": 2}
_MATCH_THRESHOLD = 0.45


def _best_device_match(name: str, devices: list[tuple[str, str]]):
    query = name.casefold()
    best = None
    best_score = _MATCH_THRESHOLD
    for label, device_id in devices:
        score = partial_ratio(query, label.casefold())
        if score > best_score:
            best, best_score = (label, device_id), score
    return best


def list_playback_devices() -> list[str]:
    return [label for label, _device_id in _list_playback_endpoints()]


def _list_playback_endpoints() -> list[tuple[str, str]]:
    return _list_windows_playback_devices()


def get_default_playback_device() -> str | None:
    try:
        from pycaw.pycaw import AudioUtilities
        speakers = AudioUtilities.GetSpeakers()
        name = str(getattr(speakers, "FriendlyName", "") or "")
        return name or None
    except Exception:
        return None


def set_default_playback_device(name: str) -> tuple[bool, str]:
    name = str(name or "").strip()
    if not name:
        return False, "Tell me the speaker or headset name, or say list audio devices."
    devices = _list_playback_endpoints()
    if not devices:
        return False, "No active Windows playback devices could be listed, so switching blind was refused. Nothing was changed."
    match = _best_device_match(name, devices)
    if not match:
        shown = ", ".join(label for label, _id in devices[:6])
        return False, f"No playback device close to '{name}' was found. Available: {shown}."
    label, device_id = match
    ok, detail = _set_windows_default(device_id)
    if ok:
        return True, f"System audio output switched to {label}."
    return False, detail or f"Windows refused to switch the output device to {label}."


def _list_windows_playback_devices() -> list[tuple[str, str]]:
    try:
        from pycaw.pycaw import AudioUtilities
        devices = AudioUtilities.GetAllDevices()
    except Exception:
        return []
    results = []
    for device in devices:
        try:
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
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from ctypes import c_int, c_wchar_p

    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    stub_methods = [COMMETHOD([], HRESULT, f"_reserved{i}") for i in range(stub_count)]

    class _IPolicyConfig(IUnknown):
        _iid_ = GUID(iid)
        _methods_ = stub_methods + [
            COMMETHOD(
                [], HRESULT, "SetDefaultEndpoint",
                (["in"], c_wchar_p, "device_id"),
                (["in"], c_int, "role"),
            ),
        ]

    policy = comtypes.CoCreateInstance(GUID(clsid), _IPolicyConfig, comtypes.CLSCTX_ALL)
    for role in _ERole.values():
        result = policy.SetDefaultEndpoint(device_id, role)
        if result not in (0, None):
            raise OSError(f"SetDefaultEndpoint returned 0x{result & 0xFFFFFFFF:08X}")
