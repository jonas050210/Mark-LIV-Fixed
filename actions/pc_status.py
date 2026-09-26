"""Read-only PC status and lag diagnosis for everyday desktop control.

The background monitor already collects system metrics for alerts.  This action
makes the same real measurements available on demand, with a short answer to
"why is my PC lagging?".  It never closes, kills, changes, or uploads anything.
"""
from __future__ import annotations

import time

from actions import system_monitor

_MAX_APPS = 10
_CPU_SAMPLE_SECONDS = 0.20


def _limit(value, default: int = 5) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(_MAX_APPS, parsed))


def _clean_name(value) -> str:
    text = " ".join(str(value or "unknown app").split())
    text = "".join(char for char in text if ord(char) >= 32)
    return text[:100] or "unknown app"


def _top_memory_apps(limit: int) -> list[tuple[str, int]]:
    """Return visible process names by RSS without relying on shell commands."""
    if not system_monitor._PSUTIL or system_monitor.psutil is None:
        return []
    rows: list[tuple[str, int]] = []
    try:
        processes = system_monitor.psutil.process_iter(["name", "memory_info"])
    except Exception:
        return []
    for process in processes:
        try:
            info = getattr(process, "info", {}) or {}
            memory = info.get("memory_info")
            rss = int(getattr(memory, "rss", 0) or 0)
            if rss <= 0:
                continue
            rows.append((_clean_name(info.get("name")), rss))
        except Exception:
            # A process may exit or become protected while it is being read.
            continue
    rows.sort(key=lambda row: (-row[1], row[0].casefold()))
    return rows[:limit]


def _top_cpu_apps(limit: int) -> list[tuple[str, float]]:
    """Sample process CPU use once, without guessing from stale percentages.

    psutil's per-process ``cpu_percent(None)`` is calculated between two reads.
    Prime every readable process, wait a short bounded interval once, then read
    them again. Sampling each process with its own interval would turn a simple
    "why is my PC lagging" check into many seconds of serial waiting.
    """
    if not system_monitor._PSUTIL or system_monitor.psutil is None:
        return []
    processes = []
    try:
        candidates = system_monitor.psutil.process_iter(["name"])
    except Exception:
        return []
    for process in candidates:
        try:
            process.cpu_percent(interval=None)
            processes.append((process, _clean_name((getattr(process, "info", {}) or {}).get("name"))))
        except Exception:
            continue
    if not processes:
        return []
    time.sleep(_CPU_SAMPLE_SECONDS)
    rows: list[tuple[str, float]] = []
    for process, name in processes:
        try:
            percent = float(process.cpu_percent(interval=None))
            if percent > 0:
                rows.append((name, round(percent, 1)))
        except Exception:
            continue
    rows.sort(key=lambda row: (-row[1], row[0].casefold()))
    return rows[:limit]


def _metric_line(status: dict) -> str:
    cpu = status.get("cpu_percent")
    ram = status.get("ram_percent")
    used = status.get("ram_used_gb")
    total = status.get("ram_total_gb")
    gpu = status.get("gpu_percent")
    gpu_temp = status.get("gpu_temp_c")
    gpu_name = status.get("gpu_name")
    cpu_temp = status.get("cpu_temp_c")
    gpu_label = str(gpu_name or "GPU")[:80]
    parts = [
        f"CPU: {cpu}%" if isinstance(cpu, (int, float)) else "CPU: unavailable",
        (
            f"RAM: {used}/{total} GB ({ram}%)"
            if all(isinstance(value, (int, float)) for value in (used, total, ram))
            else "RAM: unavailable"
        ),
        f"{gpu_label}: {gpu}%" if isinstance(gpu, (int, float)) else f"{gpu_label}: unavailable",
        (
            f"GPU temperature: {gpu_temp}°C"
            if isinstance(gpu_temp, (int, float))
            else "GPU temperature: unavailable"
        ),
    ]
    if isinstance(cpu_temp, (int, float)):
        parts.append(f"CPU temperature: {cpu_temp}°C")
    return " • ".join(parts)


def _lag_notes(status: dict) -> list[str]:
    notes = []
    cpu = status.get("cpu_percent")
    ram = status.get("ram_percent")
    gpu = status.get("gpu_percent")
    gpu_temp = status.get("gpu_temp_c")
    cpu_temp = status.get("cpu_temp_c")
    gpu_name = str(status.get("gpu_name") or "GPU")[:80]
    if isinstance(cpu, (int, float)) and cpu >= 85:
        notes.append(f"CPU usage is high ({cpu}%).")
    if isinstance(ram, (int, float)) and ram >= 85:
        notes.append(f"RAM usage is high ({ram}%).")
    if isinstance(gpu, (int, float)) and gpu >= 95:
        notes.append(f"GPU usage is very high ({gpu}%).")
    if isinstance(gpu_temp, (int, float)) and gpu_temp >= 85:
        notes.append(f"{gpu_name} temperature is high ({gpu_temp}°C).")
    if isinstance(cpu_temp, (int, float)) and cpu_temp >= 85:
        notes.append(f"CPU temperature is high ({cpu_temp}°C).")
    return notes


def pc_status(parameters: dict | None = None, player=None) -> str:
    params = parameters if isinstance(parameters, dict) else {}
    action = str(params.get("action") or "status").strip().casefold().replace(" ", "_")
    if action not in {"status", "lag_check", "top_apps", "top_cpu_apps"}:
        return "Unknown PC status action. Use status, lag_check, top_apps, or top_cpu_apps."

    try:
        status = system_monitor.get_system_status()
    except Exception as exc:
        return f"I could not read PC status ({type(exc).__name__})."
    if not status.get("available", True):
        return (
            "PC status is unavailable because the optional psutil package is not installed. "
            "Install psutil to enable local CPU and RAM checks."
        )

    if action == "top_apps":
        apps = _top_memory_apps(_limit(params.get("limit")))
        if not apps:
            return "I could not read per-app memory usage right now."
        lines = ["Apps using the most memory:"]
        lines.extend(
            f"{index}. {name} ({memory / 1024 ** 3:.1f} GB RAM)"
            for index, (name, memory) in enumerate(apps, 1)
        )
        return "\n".join(lines)

    if action == "top_cpu_apps":
        apps = _top_cpu_apps(_limit(params.get("limit")))
        if not apps:
            return "I could not read per-app CPU usage right now."
        lines = ["Apps using the most CPU (short sample):"]
        lines.extend(
            f"{index}. {name} ({percent:.1f}% CPU)"
            for index, (name, percent) in enumerate(apps, 1)
        )
        return "\n".join(lines)

    lines = ["PC status", _metric_line(status)]
    if status.get("uptime"):
        lines.append(f"Uptime: {status['uptime']} • Processes: {status.get('process_count', 'unknown')}")

    if action == "lag_check":
        notes = _lag_notes(status)
        if notes:
            lines.append("Possible reason for lag: " + " ".join(notes))
            limit = _limit(params.get("limit"))
            if isinstance(status.get("cpu_percent"), (int, float)) and status["cpu_percent"] >= 85:
                cpu_apps = _top_cpu_apps(limit)
                if cpu_apps:
                    lines.append("Highest CPU users (short sample):")
                    lines.extend(f"- {name}: {percent:.1f}% CPU" for name, percent in cpu_apps)
            apps = _top_memory_apps(limit)
            if apps:
                lines.append("Largest RAM users:")
                lines.extend(
                    f"- {name}: {memory / 1024 ** 3:.1f} GB"
                    for name, memory in apps
                )
            lines.append(
                "I did not close anything. You can ask me to minimize other windows, "
                "or name a specific app to inspect or restart."
            )
        else:
            lines.append(
                "Nothing looks critically overloaded right now. If one app is still slow, "
                "ask me to diagnose that app by name."
            )
    return "\n".join(lines)


TOOL = {
    "name": "pc_status",
    "description": (
        "Read-only PC performance check. Use lag_check for phrases such as 'why is my PC "
        "lagging?' or German 'Warum laggt mein PC?': it reports real CPU, RAM, NVIDIA GPU "
        "load and GPU temperature when available, plus the highest CPU and largest RAM-using apps when "
        "there is pressure. Use top_apps to list RAM-heavy apps, top_cpu_apps for a short real CPU sample, "
        "or status for a compact snapshot. "
        "It never closes, kills, or changes anything."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["status", "lag_check", "top_apps", "top_cpu_apps"],
                "description": "status | lag_check | top_apps | top_cpu_apps",
            },
            "limit": {
                "type": "INTEGER",
                "minimum": 1,
                "maximum": 10,
                "description": "How many apps to show for top_apps, top_cpu_apps, or lag_check; default 5.",
            },
        },
        "required": ["action"],
    },
    "handler": pc_status,
    "risk": "low",
}
