"""Local system metrics and safe background alerts.

All GPU readings use NVIDIA's NVML interface when an NVIDIA driver is present;
that includes the RTX 4060 Ti's actual GPU temperature.  The module never uses
shell commands, never uploads metrics, and never changes or closes programs.
"""
from __future__ import annotations

import ctypes
import platform
import threading
import time

try:
    import psutil
    _PSUTIL = True
except ImportError:
    psutil = None
    _PSUTIL = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

DEFAULT_THRESHOLDS = {
    "cpu": 90.0,
    "ram": 90.0,
    "temp": 85.0,
    "gpu": 95.0,
    "gpu_temp": 85.0,
}

_COOLDOWN = 300
_CPU_STREAK = 3

# NVML is available with NVIDIA's driver.  Keep one process-local handle rather
# than reinitialising it on every HUD/status refresh.
_nvml_lib: object | None = None
_nvml_ok: bool | None = None
_pynvml_module: object | None = None
_pynvml_handle: object | None = None
_pynvml_ok: bool | None = None
# A driver can be momentarily unavailable while Windows is waking, installing a
# driver update, or recovering from a reset.  Cache a failed probe briefly so a
# 180 FPS HUD does not repeatedly import/load NVML, but do not make one startup
# failure permanent for the rest of MARK LIV's session.
_NVML_RETRY_SECONDS = 30.0
_nvml_retry_after = 0.0
_pynvml_retry_after = 0.0
_gpu_lock = threading.Lock()


class _NvmlUtilisation(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def _blank_gpu_metrics() -> dict:
    return {"utilization_percent": None, "temperature_c": None, "name": None}


def _clean_gpu_name(value) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text[:120] or None


def _pynvml_gpu_metrics() -> dict:
    """Read the first NVIDIA GPU through maintained nvidia-ml-py bindings."""
    global _pynvml_module, _pynvml_handle, _pynvml_ok, _pynvml_retry_after
    now = time.monotonic()
    if _pynvml_ok is False and now < _pynvml_retry_after:
        return _blank_gpu_metrics()
    try:
        if _pynvml_handle is None:
            import pynvml  # provided by the maintained nvidia-ml-py package

            pynvml.nvmlInit()
            _pynvml_module = pynvml
            _pynvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        module = _pynvml_module
        handle = _pynvml_handle
        utilisation = module.nvmlDeviceGetUtilizationRates(handle)
        temperature = module.nvmlDeviceGetTemperature(
            handle, module.NVML_TEMPERATURE_GPU
        )
        name = module.nvmlDeviceGetName(handle)
        _pynvml_ok = True
        _pynvml_retry_after = 0.0
        return {
            "utilization_percent": float(utilisation.gpu),
            "temperature_c": float(temperature),
            "name": _clean_gpu_name(name),
        }
    except Exception:
        _pynvml_ok = False
        _pynvml_module = None
        _pynvml_handle = None
        _pynvml_retry_after = now + _NVML_RETRY_SECONDS
        return _blank_gpu_metrics()


def _native_nvml_gpu_metrics() -> dict:
    """Read NVML directly when the optional Python binding is unavailable."""
    global _nvml_lib, _nvml_ok, _nvml_retry_after
    now = time.monotonic()
    if _nvml_ok is False and now < _nvml_retry_after:
        return _blank_gpu_metrics()
    try:
        if _nvml_lib is None:
            if _OS == "Windows":
                loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
                candidates = ("nvml", r"C:\Windows\System32\nvml.dll")
            elif _OS == "Linux":
                loader = ctypes.CDLL
                candidates = ("libnvidia-ml.so.1", "libnvidia-ml.so")
            else:
                loader = ctypes.CDLL
                candidates = ("libnvidia-ml.dylib",)
            for candidate in candidates:
                try:
                    library = loader(candidate)
                    if int(library.nvmlInit_v2()) == 0:
                        _nvml_lib = library
                        break
                except Exception:
                    continue

        if _nvml_lib is None:
            _nvml_ok = False
            _nvml_retry_after = now + _NVML_RETRY_SECONDS
            return _blank_gpu_metrics()

        device = ctypes.c_void_p()
        if int(_nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(device))) != 0:
            raise OSError("NVML could not open NVIDIA GPU 0")

        utilisation = _NvmlUtilisation()
        if int(_nvml_lib.nvmlDeviceGetUtilizationRates(device, ctypes.byref(utilisation))) != 0:
            raise OSError("NVML could not read GPU utilisation")

        # NVML writes temperature through an output pointer.  Treating its
        # return code as a temperature is why a native implementation can show
        # N/A or nonsense even when an RTX card is installed.
        temperature = ctypes.c_uint()
        temp_result = _nvml_lib.nvmlDeviceGetTemperature(
            device, 0, ctypes.byref(temperature)  # NVML_TEMPERATURE_GPU
        )
        temp_value = float(temperature.value) if int(temp_result) == 0 else None

        name_buffer = ctypes.create_string_buffer(128)
        name_result = _nvml_lib.nvmlDeviceGetName(
            device, name_buffer, ctypes.sizeof(name_buffer)
        )
        name = _clean_gpu_name(name_buffer.value) if int(name_result) == 0 else None
        _nvml_ok = True
        _nvml_retry_after = 0.0
        return {
            "utilization_percent": float(utilisation.gpu),
            "temperature_c": temp_value,
            "name": name,
        }
    except Exception:
        _nvml_ok = False
        _nvml_retry_after = now + _NVML_RETRY_SECONDS
        return _blank_gpu_metrics()


def get_gpu_metrics() -> dict:
    """Return real NVIDIA load, temperature and model where NVML is available.

    The RTX 4060 Ti supports NVML, so a normal up-to-date NVIDIA driver exposes
    its GPU temperature here.  ``None`` means the local driver/API could not
    provide that one value; it is not invented from CPU sensors.
    """
    # The UI's monitoring thread and a voice/status request can arrive at the
    # same time. NVML initialisation is process-global, so serialize the first
    # probe and reuse its cache afterward.
    with _gpu_lock:
        metrics = _pynvml_gpu_metrics()
        if metrics["utilization_percent"] is not None:
            return metrics
        return _native_nvml_gpu_metrics()


def _get_gpu_usage() -> float:
    """Backward-compatible load helper for callers that only need a percent."""
    value = get_gpu_metrics().get("utilization_percent")
    return float(value) if isinstance(value, (int, float)) else -1.0


def _get_cpu_temp() -> float:
    """Best-effort CPU temperature; many Windows machines do not expose it."""
    if not _PSUTIL or psutil is None:
        return -1.0
    try:
        temps = psutil.sensors_temperatures()
        for name in [
            "coretemp", "k10temp", "cpu_thermal", "acpitz", "cpu-thermal", "zenpower", "it8688",
        ]:
            if name in temps and temps[name]:
                return float(temps[name][0].current)
        for entries in temps.values():
            if entries:
                return float(entries[0].current)
    except Exception:
        pass

    if _OS == "Windows":
        try:
            import wmi  # type: ignore

            temperatures = wmi.WMI(namespace="root/wmi").MSAcpi_ThermalZoneTemperature()
            if temperatures:
                return float(temperatures[0].CurrentTemperature / 10.0 - 273.15)
        except Exception:
            pass
    return -1.0


def get_system_status() -> dict:
    """Snapshot of current local metrics for the status and PC-lag actions."""
    if not _PSUTIL or psutil is None:
        return {
            "available": False,
            "error": "psutil is not installed. Run: pip install psutil",
        }
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory()
    cpu_temp = _get_cpu_temp()
    gpu = get_gpu_metrics()

    boot_time = psutil.boot_time()
    uptime_secs = time.time() - boot_time
    uptime_h = int(uptime_secs // 3600)
    uptime_m = int((uptime_secs % 3600) // 60)
    gpu_usage = gpu.get("utilization_percent")
    gpu_temp = gpu.get("temperature_c")

    return {
        "cpu_percent": round(cpu, 1),
        "ram_percent": round(ram.percent, 1),
        "ram_used_gb": round(ram.used / 1024 ** 3, 1),
        "ram_total_gb": round(ram.total / 1024 ** 3, 1),
        "cpu_temp_c": round(cpu_temp, 1) if cpu_temp > 0 else None,
        "gpu_percent": round(float(gpu_usage), 1) if isinstance(gpu_usage, (int, float)) else None,
        "gpu_temp_c": round(float(gpu_temp), 1) if isinstance(gpu_temp, (int, float)) else None,
        "gpu_name": gpu.get("name") if isinstance(gpu.get("name"), str) else None,
        "uptime": f"{uptime_h}h {uptime_m}m",
        "process_count": len(psutil.pids()),
    }


class SystemMonitor:
    """Stateful local monitor which returns an alert string only when needed."""

    def __init__(self, thresholds: dict | None = None):
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._last_alert: dict[str, float] = {}
        self._cpu_streak = 0

    def _can_alert(self, key: str) -> bool:
        return (time.monotonic() - self._last_alert.get(key, 0)) > _COOLDOWN

    def _record(self, key: str) -> None:
        self._last_alert[key] = time.monotonic()

    def check(self) -> str | None:
        if not _PSUTIL or psutil is None:
            return None
        try:
            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory().percent
            cpu_temp = _get_cpu_temp()
            gpu = get_gpu_metrics()
            gpu_usage = gpu.get("utilization_percent")
            gpu_temp = gpu.get("temperature_c")
        except Exception:
            return None

        alerts: list[str] = []
        if cpu >= self.thresholds["cpu"]:
            self._cpu_streak += 1
            if self._cpu_streak >= _CPU_STREAK and self._can_alert("cpu"):
                alerts.append(
                    f"[SYSTEM_ALERT] CPU usage has been critically high ({cpu:.0f}%) for several seconds. "
                    "Warn the user in their language and suggest closing heavy applications."
                )
                self._record("cpu")
                self._cpu_streak = 0
        else:
            self._cpu_streak = 0

        if ram >= self.thresholds["ram"] and self._can_alert("ram"):
            alerts.append(
                f"[SYSTEM_ALERT] RAM is at {ram:.0f}% — nearly exhausted. "
                "Warn the user in their language and suggest freeing memory."
            )
            self._record("ram")

        if cpu_temp > 0 and cpu_temp >= self.thresholds["temp"] and self._can_alert("temp"):
            alerts.append(
                f"[SYSTEM_ALERT] CPU temperature is {cpu_temp:.0f}°C — above the safe limit. "
                "Warn the user in their language and advise reducing system load or checking cooling."
            )
            self._record("temp")

        if isinstance(gpu_usage, (int, float)) and gpu_usage >= self.thresholds["gpu"] and self._can_alert("gpu"):
            alerts.append(
                f"[SYSTEM_ALERT] GPU load is at {gpu_usage:.0f}%. Briefly inform the user in their language."
            )
            self._record("gpu")

        if isinstance(gpu_temp, (int, float)) and gpu_temp >= self.thresholds["gpu_temp"] and self._can_alert("gpu_temp"):
            name = str(gpu.get("name") or "GPU")
            alerts.append(
                f"[SYSTEM_ALERT] {name} temperature is {gpu_temp:.0f}°C — above the safe limit. "
                "Warn the user in their language and advise reducing game load or checking cooling."
            )
            self._record("gpu_temp")

        return " ".join(alerts) if alerts else None
