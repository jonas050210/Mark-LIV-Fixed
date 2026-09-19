"""
System Monitor — background metric checks with voice alert support.
Zero subprocess calls on all platforms — uses ctypes/pynvml/psutil/wmi only.
"""
import ctypes
import platform
import time

import psutil

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

DEFAULT_THRESHOLDS = {
    "cpu":  90.0,
    "ram":  90.0,
    "temp": 85.0,
    "gpu":  95.0,
}

_COOLDOWN   = 300
_CPU_STREAK = 3

# ── NVML DLL cache (Windows: nvml.dll, Linux: libnvidia-ml.so.1) ─────────────
_nvml_lib: object = None
_nvml_ok:  object = None   # None=untested  True=works  False=unavailable


def _nvml_gpu() -> float:
    """GPU utilisation via NVML — zero subprocess on all platforms."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            if _OS == "Windows":
                candidates = ("nvml", r"C:\Windows\System32\nvml.dll")
                _load = ctypes.WinDLL
            else:
                candidates = (
                    "libnvidia-ml.so.1",
                    "libnvidia-ml.so",
                    "libnvidia-ml.dylib",
                )
                _load = ctypes.CDLL
            for name in candidates:
                try:
                    lib = _load(name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            _nvml_ok = False
            return -1.0

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        u = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
        _nvml_ok = True
        return float(u.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


def _get_gpu_usage() -> float:
    # pynvml — subprocess-free, works everywhere if installed
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
    except Exception:
        pass

    return _nvml_gpu()


def _get_cpu_temp() -> float:
    # psutil — works on Linux; occasionally Windows with proper drivers
    try:
        temps = psutil.sensors_temperatures()
        for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                     "cpu-thermal", "zenpower", "it8688"]:
            if name in temps and temps[name]:
                return temps[name][0].current
        for entries in temps.values():
            if entries:
                return entries[0].current
    except Exception:
        pass

    # Windows: wmi module (pure Python COM, zero subprocess)
    if _OS == "Windows":
        try:
            import wmi  # type: ignore
            w = wmi.WMI(namespace="root/wmi")
            tz = w.MSAcpi_ThermalZoneTemperature()
            if tz:
                return (tz[0].CurrentTemperature / 10.0) - 273.15
        except Exception:
            pass

    return -1.0


def _disks(limit: int = 4) -> list[dict]:
    """Real partitions with a size — pseudo filesystems are noise, not status."""
    out: list[dict] = []
    try:
        for part in psutil.disk_partitions(all=False):
            if not part.fstype or part.fstype in ("squashfs", "tmpfs", "devtmpfs"):
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            out.append({
                "mount": part.mountpoint,
                "percent": round(usage.percent, 1),
                "free_gb": round(usage.free / 1024 ** 3, 1),
                "total_gb": round(usage.total / 1024 ** 3, 1),
            })
            if len(out) >= limit:
                break
    except Exception:
        return out
    return out


def _battery() -> dict | None:
    """Battery percentage + plugged-in state, or None on a machine without one."""
    try:
        bat = psutil.sensors_battery()
    except Exception:
        return None
    if bat is None:
        return None
    return {"percent": round(float(bat.percent), 1),
            "plugged": bool(getattr(bat, "power_plugged", False))}


def _network_totals() -> dict | None:
    try:
        net = psutil.net_io_counters()
    except Exception:
        return None
    return {"sent_gb": round(net.bytes_sent / 1024 ** 3, 2),
            "recv_gb": round(net.bytes_recv / 1024 ** 3, 2)}


def _top_processes(count: int = 3) -> tuple[list[dict], list[dict]]:
    """``(by_memory, by_cpu)``. CPU is a short delta sample, so it means load
    *now* rather than "CPU time since the process started"."""
    procs = []
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            procs.append(proc)
    except Exception:
        return [], []
    by_mem: list[dict] = []
    try:
        for proc in procs:
            try:
                info = proc.info
                mem = proc.memory_percent()
                by_mem.append({"name": info.get("name") or f"pid {info.get('pid')}",
                               "mem_percent": round(mem, 1)})
            except Exception:
                continue
    except Exception:
        pass
    by_mem.sort(key=lambda d: d["mem_percent"], reverse=True)

    by_cpu: list[dict] = []
    try:
        for proc in procs:
            try:
                proc.cpu_percent(None)     # prime the counters
            except Exception:
                continue
        time.sleep(0.3)
        for proc in procs:
            try:
                cpu = proc.cpu_percent(None)
                if cpu > 0:
                    by_cpu.append({"name": proc.info.get("name")
                                   or f"pid {proc.info.get('pid')}",
                                   "cpu_percent": round(cpu, 1)})
            except Exception:
                continue
    except Exception:
        pass
    by_cpu.sort(key=lambda d: d["cpu_percent"], reverse=True)
    return by_mem[:count], by_cpu[:count]


def _running_tasks() -> dict:
    """What the activity panel is showing, for "what is running?" questions."""
    try:
        from core import tasks as _tasks

        rows = _tasks.snapshot(limit=8)
    except Exception:
        return {"running": 0, "titles": []}
    live = [r for r in rows if r.get("state") == "running"]
    return {"running": len(live),
            "titles": [f"{r.get('title', '')} ({r.get('percent')}%)"
                       if r.get("percent") is not None else str(r.get("title", ""))
                       for r in live[:5]]}


def get_system_status() -> dict:
    """Snapshot of current system metrics for the system_status tool.

    Everything is optional by design: a metric this machine cannot report comes
    back as None (or an empty list) and ``format_system_status`` simply leaves it
    out — a status tool that invents numbers is worse than one that admits it
    cannot see the GPU.
    """
    cpu  = psutil.cpu_percent(interval=0.2)
    ram  = psutil.virtual_memory()
    temp = _get_cpu_temp()
    gpu  = _get_gpu_usage()

    boot_time   = psutil.boot_time()
    uptime_secs = time.time() - boot_time
    uptime_h    = int(uptime_secs // 3600)
    uptime_m    = int((uptime_secs % 3600) // 60)

    top_mem, top_cpu = _top_processes()

    return {
        "cpu_percent":   round(cpu, 1),
        "cpu_count":     psutil.cpu_count(logical=True),
        "ram_percent":   round(ram.percent, 1),
        "ram_used_gb":   round(ram.used   / 1024 ** 3, 1),
        "ram_total_gb":  round(ram.total  / 1024 ** 3, 1),
        "cpu_temp_c":    round(temp, 1) if temp > 0 else None,
        "gpu_percent":   round(gpu,  1) if gpu  >= 0 else None,
        "uptime":        f"{uptime_h}h {uptime_m}m",
        "process_count": len(psutil.pids()),
        "disks":         _disks(),
        "battery":       _battery(),
        "network":       _network_totals(),
        "top_memory":    top_mem,
        "top_cpu":       top_cpu,
        "tasks":         _running_tasks(),
    }


def format_system_status(status: dict | None = None) -> str:
    """Compact, spoken-friendly rendering. Omits what the machine cannot report."""
    data = dict(status if status is not None else get_system_status())
    lines = [
        f"CPU {data.get('cpu_percent')}% "
        f"({data.get('cpu_count') or '?'} cores)"
        + (f", temperature {data['cpu_temp_c']}°C" if data.get("cpu_temp_c") else ""),
        f"RAM {data.get('ram_percent')}% — "
        f"{data.get('ram_used_gb')} of {data.get('ram_total_gb')} GB in use",
    ]
    if data.get("gpu_percent") is not None:
        lines.append(f"GPU {data['gpu_percent']}%")
    for disk in data.get("disks") or []:
        lines.append(f"Disk {disk.get('mount')}: {disk.get('percent')}% full, "
                     f"{disk.get('free_gb')} GB free")
    battery = data.get("battery")
    if battery:
        lines.append(f"Battery {battery.get('percent')}%"
                     + (" (charging)" if battery.get("plugged") else ""))
    net = data.get("network")
    if net:
        lines.append(f"Network since boot: {net.get('recv_gb')} GB down, "
                     f"{net.get('sent_gb')} GB up")
    lines.append(f"Uptime {data.get('uptime')}, "
                 f"{data.get('process_count')} processes")
    top_mem = ", ".join(f"{p.get('name')} {p.get('mem_percent')}%"
                        for p in (data.get("top_memory") or []))
    if top_mem:
        lines.append(f"Most memory: {top_mem}")
    top_cpu = ", ".join(f"{p.get('name')} {p.get('cpu_percent')}%"
                        for p in (data.get("top_cpu") or []))
    if top_cpu:
        lines.append(f"Busiest now: {top_cpu}")
    tasks = data.get("tasks") or {}
    if tasks.get("running"):
        lines.append(f"Running tasks: {tasks.get('running')} — "
                     + "; ".join(str(t) for t in tasks.get("titles") or []))
    return " | ".join(lines)


class SystemMonitor:
    """
    Stateful monitor — cooldown state persists across session reconnections.
    Call check() periodically; returns a [SYSTEM_ALERT] string or None.
    """

    def __init__(self, thresholds: dict | None = None):
        self.thresholds   = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._last_alert: dict[str, float] = {}
        self._cpu_streak  = 0

    def _can_alert(self, key: str) -> bool:
        return (time.monotonic() - self._last_alert.get(key, 0)) > _COOLDOWN

    def _record(self, key: str):
        self._last_alert[key] = time.monotonic()

    def check(self) -> str | None:
        try:
            cpu  = psutil.cpu_percent(interval=None)
            ram  = psutil.virtual_memory().percent
            temp = _get_cpu_temp()
            gpu  = _get_gpu_usage()
        except Exception:
            return None

        alerts: list[str] = []

        if cpu >= self.thresholds["cpu"]:
            self._cpu_streak += 1
            if self._cpu_streak >= _CPU_STREAK and self._can_alert("cpu"):
                alerts.append(
                    f"[SYSTEM_ALERT] CPU usage has been critically high ({cpu:.0f}%) "
                    "for several seconds. Warn the user in their language and suggest "
                    "closing heavy applications."
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

        if temp > 0 and temp >= self.thresholds["temp"] and self._can_alert("temp"):
            alerts.append(
                f"[SYSTEM_ALERT] CPU temperature is {temp:.0f}°C — above the safe limit. "
                "Warn the user in their language and advise reducing system load "
                "or checking cooling."
            )
            self._record("temp")

        if gpu >= 0 and gpu >= self.thresholds["gpu"] and self._can_alert("gpu"):
            alerts.append(
                f"[SYSTEM_ALERT] GPU load is at {gpu:.0f}%. "
                "Briefly inform the user in their language."
            )
            self._record("gpu")

        return " ".join(alerts) if alerts else None
