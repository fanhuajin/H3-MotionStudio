"""系统资源采样：CPU / 内存 / 磁盘 / 网络吞吐 / NVIDIA GPU。

- CPU 温度：Windows 上无通用用户态接口（WMI 热区需管理员），返回 None；
- GPU 信息：nvidia-smi 子进程，仅当 NVIDIA 驱动可用时返回非空。
全部为同步函数，由调用方放到线程池执行。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

try:  # 服务环境可能尚未安装 psutil 时优雅降级
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class _NetworkMeter:
    """两次采样间的字节差 / 时间差 → 每秒收发速率。"""

    def __init__(self) -> None:
        self._last_at: float | None = None
        self._last_counters = None

    def rates(self) -> dict[str, float]:
        if psutil is None:
            return {"rxBytesPerSec": 0.0, "txBytesPerSec": 0.0}
        now = time.monotonic()
        counters = psutil.net_io_counters(pernic=True)
        active: list[Any] = []
        try:
            stats = psutil.net_if_stats()
            addrs = psutil.net_if_addrs()
        except OSError:
            stats, addrs = {}, {}
        for name, counter in counters.items():
            if name == "lo" or name.startswith("Loopback"):
                continue
            if not counter:
                continue
            interface = stats.get(name)
            if interface is not None and not getattr(interface, "isup", False):
                continue
            # 仅统计有实际地址的接口（滤掉虚拟/隧道适配器的空计数）
            if not addrs.get(name):
                continue
            active.append(counter)
        rx = sum(counter.bytes_recv for counter in active)
        tx = sum(counter.bytes_sent for counter in active)

        if self._last_at is None or self._last_counters is None:
            self._last_at, self._last_counters = now, (rx, tx)
            return {"rxBytesPerSec": 0.0, "txBytesPerSec": 0.0}
        elapsed = max(0.001, now - self._last_at)
        prev_rx, prev_tx = self._last_counters
        self._last_at, self._last_counters = now, (rx, tx)
        return {
            "rxBytesPerSec": max(0.0, (rx - prev_rx) / elapsed),
            "txBytesPerSec": max(0.0, (tx - prev_tx) / elapsed),
        }


_network_meter = _NetworkMeter()


def _gpu_stats() -> list[dict[str, Any]]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    gpus: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpus.append({
                "name": parts[0],
                "util": float(parts[1] or 0),
                "memUsed": int(float(parts[2] or 0)),
                "memTotal": int(float(parts[3] or 0)),
                "temp": float(parts[4] or 0),
            })
        except ValueError:
            continue
    return gpus


def _fixed_disks() -> list[dict[str, Any]]:
    if psutil is None:
        return []
    disks: list[dict[str, Any]] = []
    for partition in psutil.disk_partitions(all=False):
        if partition.fstype in ("", "cd9660") or "cdrom" in partition.opts.lower():
            continue
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except OSError:
            continue
        disks.append({
            "mount": partition.mountpoint.rstrip("\\/") or partition.mountpoint,
            "percent": usage.percent,
            "used": usage.used,
            "total": usage.total,
        })
    return disks


def collect_system_stats() -> dict[str, Any]:
    stats: dict[str, Any] = {"sampleAt": time.time(), "gpu": _gpu_stats(), "disks": _fixed_disks()}

    if psutil is not None:
        try:
            stats["cpu"] = {
                "percent": psutil.cpu_percent(interval=0.25),
                "cores": psutil.cpu_count(logical=True) or 0,
            }
            virtual = psutil.virtual_memory()
            stats["memory"] = {
                "percent": virtual.percent,
                "used": virtual.used,
                "total": virtual.total,
            }
        except OSError:
            pass
        stats["net"] = _network_meter.rates()
    else:  # pragma: no cover
        stats["cpu"] = None
        stats["memory"] = None
        stats["net"] = {"rxBytesPerSec": 0.0, "txBytesPerSec": 0.0}

    stats["cpuTemp"] = None  # Windows 用户态无法读取 CPU 温度（需管理员 WMI）
    return stats
