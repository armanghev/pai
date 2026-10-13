"""pi_health: temperature, disk, memory, load and uptime of the box Pai runs on."""

from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path

from pai.tools import Tool, ToolContext


def cpu_temp_c() -> float | None:
    temps = []
    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            temps.append(int(Path(path).read_text().strip()) / 1000)
        except (OSError, ValueError):
            continue
    return round(max(temps), 1) if temps else None


def meminfo(text: str | None = None) -> dict[str, int] | None:
    """Total and available memory in MB from /proc/meminfo."""
    if text is None:
        try:
            text = Path("/proc/meminfo").read_text()
        except OSError:
            return None
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            values[key] = int(rest.split()[0]) // 1024
    if "MemTotal" not in values:
        return None
    return {"total_mb": values["MemTotal"], "available_mb": values.get("MemAvailable", 0)}


def uptime_hours() -> float | None:
    try:
        return round(float(Path("/proc/uptime").read_text().split()[0]) / 3600, 1)
    except (OSError, ValueError, IndexError):
        return None


def health(data_dir: Path | str = "/") -> dict:
    disk = shutil.disk_usage(data_dir)
    load1, load5, _ = os.getloadavg()
    return {
        "cpu_temp_c": cpu_temp_c(),
        "disk_used_pct": round(disk.used / disk.total * 100, 1),
        "disk_free_gb": round(disk.free / 1e9, 1),
        "memory": meminfo(),
        "load_1m": round(load1, 2),
        "load_5m": round(load5, 2),
        "cpus": os.cpu_count(),
        "uptime_hours": uptime_hours(),
    }


def format_health(h: dict) -> str:
    lines = []
    lines.append(
        f"CPU temp: {h['cpu_temp_c']}°C" if h["cpu_temp_c"] is not None else "CPU temp: n/a"
    )
    lines.append(f"Disk: {h['disk_used_pct']}% used, {h['disk_free_gb']} GB free")
    if h["memory"]:
        m = h["memory"]
        lines.append(f"Memory: {m['available_mb']} of {m['total_mb']} MB free")
    lines.append(f"Load: {h['load_1m']} (1m), {h['load_5m']} (5m) on {h['cpus']} cores")
    if h["uptime_hours"] is not None:
        lines.append(f"Up {h['uptime_hours']} h")
    return "\n".join(lines)


async def pi_health(args: dict, ctx: ToolContext) -> dict:
    return health(ctx.config.db_path.parent if ctx.config.db_path.parent.exists() else "/")


TOOLS = [
    Tool(
        name="pi_health",
        description=(
            "Read the health of the computer Pai runs on: CPU temperature (°C), disk use, "
            "free memory, "
            "load average and uptime. Use it whenever the owner asks how the machine or the server "
            "is doing. Some values may be null where the system doesn't expose them."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        fn=pi_health,
    )
]
