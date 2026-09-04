import { useEffect, useState } from "react";

/** 毫秒 → "MM:SS" 或 "H:MM:SS"（>=1 小时） */
export function formatElapsedMs(ms: number): string {
  const total = Math.max(0, Math.floor((ms || 0) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

/**
 * 时间段毫秒数：start/end 均为 ISO 字符串；end 为空时以 nowMs 计。
 * 无法解析（字段缺失）返回 null。
 */
export function elapsedMs(
  start?: string | null,
  end?: string | null,
  nowMs: number = Date.now(),
): number | null {
  const startMs = start ? Date.parse(start) : NaN;
  if (!Number.isFinite(startMs)) return null;
  const endMs = end ? Date.parse(end) : nowMs;
  if (!Number.isFinite(endMs)) return null;
  return Math.max(0, endMs - startMs);
}

/**
 * 每秒触发一次刷新；active 为 false 时不跑定时器（返回恒定值）。
 * 用于"已运行"实时计时。
 */
export function useNowTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}
