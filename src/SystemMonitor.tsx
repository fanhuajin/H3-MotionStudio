import { useEffect, useState } from "react";

type GpuStat = {
  name: string;
  util: number;
  memUsed: number;
  memTotal: number;
  temp: number;
};

type DiskStat = {
  mount: string;
  percent: number;
  used: number;
  total: number;
};

type SystemStats = {
  sampleAt?: number;
  cpu?: { percent: number; cores: number } | null;
  memory?: { percent: number; used: number; total: number } | null;
  disks?: DiskStat[];
  net?: { rxBytesPerSec: number; txBytesPerSec: number };
  gpu?: GpuStat[];
  cpuTemp?: number | null;
};

const POLL_MS = 2500;

function formatBytes(value?: number | null) {
  if (value == null || !Number.isFinite(value)) return "--";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index < 2 ? 0 : 1)} ${units[index]}`;
}

function formatRate(bytesPerSec?: number | null) {
  if (bytesPerSec == null || !Number.isFinite(bytesPerSec) || bytesPerSec < 0) return "--";
  return `${formatBytes(bytesPerSec)}/s`;
}

function toneOf(percent: number) {
  if (percent >= 90) return "critical";
  if (percent >= 70) return "warn";
  return "normal";
}

function MeterRow({ label, percent, value, title }: { label: string; percent?: number | null; value: string; title?: string }) {
  return (
    <div className="monitor-row" title={title}>
      <span className="monitor-label">{label}</span>
      <span className="monitor-bar">
        {percent != null && <i className={toneOf(percent)} style={{ width: `${Math.max(2, Math.min(100, percent))}%` }} />}
      </span>
      <b className="monitor-value">{value}</b>
    </div>
  );
}

export function SystemMonitor() {
  const [stats, setStats] = useState<SystemStats | null>(null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetch("/api/system/stats", { cache: "no-store" });
        if (!response.ok) throw new Error("unavailable");
        const next = await response.json() as SystemStats;
        if (!cancelled) {
          setStats(next);
          setOffline(false);
        }
      } catch {
        if (!cancelled) setOffline(true);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const cpu = stats?.cpu;
  const memory = stats?.memory;
  const disks = stats?.disks || [];
  const net = stats?.net;
  const gpu = stats?.gpu?.[0];

  return (
    <div className={`monitor-card ${offline ? "offline" : ""}`}>
      <div className="monitor-title"><span /> 资源占用 {offline && <em>服务未连接</em>}</div>
      {!stats && !offline && <div className="monitor-empty">采集中…</div>}
      {stats && (
        <div className="monitor-grid">
          <MeterRow
            label="CPU"
            percent={cpu?.percent ?? null}
            value={cpu ? `${Math.round(cpu.percent)}%` : "--"}
            title={`${cpu?.cores ?? "?"} 逻辑核心${stats.cpuTemp != null ? ` · 温度 ${Math.round(stats.cpuTemp)}°C` : " · CPU 温度需管理员权限，暂不可读"}`}
          />
          <MeterRow
            label="内存"
            percent={memory?.percent ?? null}
            value={memory ? `${Math.round(memory.percent)}%` : "--"}
            title={memory ? `已用 ${formatBytes(memory.used)} / ${formatBytes(memory.total)}` : undefined}
          />
          {disks.map((disk) => (
            <MeterRow
              key={disk.mount}
              label={`磁盘 ${disk.mount}`}
              percent={disk.percent}
              value={`${Math.round(disk.percent)}%`}
              title={`${disk.mount} 已用 ${formatBytes(disk.used)} / ${formatBytes(disk.total)}`}
            />
          ))}
          {gpu ? (
            <>
              <MeterRow
                label="GPU"
                percent={gpu.util}
                value={`${Math.round(gpu.util)}% · ${Math.round(gpu.temp)}°C`}
                title={`${gpu.name} · 显存 ${formatBytes(gpu.memUsed * 1024 * 1024)} / ${formatBytes(gpu.memTotal * 1024 * 1024)}`}
              />
              <div className="monitor-row monitor-sub" title={gpu.name}>
                <span className="monitor-label">显存</span>
                <span className="monitor-bar-static">
                  <i className={toneOf((gpu.memUsed / Math.max(1, gpu.memTotal)) * 100)} style={{ width: `${Math.min(100, (gpu.memUsed / Math.max(1, gpu.memTotal)) * 100)}%` }} />
                </span>
                <b className="monitor-value">{formatBytes(gpu.memUsed * 1024 * 1024)}</b>
              </div>
            </>
          ) : (
            <div className="monitor-row monitor-sub"><span className="monitor-label">GPU</span><span className="monitor-bar-static" /><b className="monitor-value">--</b></div>
          )}
          <div className="monitor-row monitor-sub" title="活动网卡合计吞吐（含无线）">
            <span className="monitor-label">网络</span>
            <span className="monitor-value net-value">
              <i className="net-dn" />↓ {formatRate(net?.rxBytesPerSec)} <i className="net-up" />↑ {formatRate(net?.txBytesPerSec)}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
