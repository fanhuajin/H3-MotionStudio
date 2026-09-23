import { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise, MicrophoneStage } from "@phosphor-icons/react";
import { readJson } from "./api";

type TrainingProgress = {
  name: string;
  state: "running" | "completed" | "failed" | "stopped" | "not_started";
  stage: string;
  completedEpochs: number;
  currentEpoch: number;
  currentEpochPercent: number;
  targetEpochs: number;
  progress: number;
  startedAt: string | null;
  elapsedSeconds: number;
  etaSeconds: number | null;
  lastUpdate: string | null;
  refreshedAt: string;
  recentLog: string[];
};

type ShutdownStatus = {
  enabled: boolean;
  watcherAlive: boolean;
  state: "waiting" | "scheduled" | "cancelled" | "error";
  shutdownAt: string | null;
  error: string | null;
};

function duration(seconds: number | null) {
  if (seconds == null) return "计算中";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分 ${secs} 秒`;
}

function stateLabel(data: TrainingProgress) {
  if (data.state === "completed") return "已完成 500 轮";
  if (data.state === "failed") return "训练出错";
  if (data.state === "stopped") return "训练进程已停止";
  if (data.state === "not_started") return "尚未启动";
  if (data.stage === "preprocess") return "正在处理素材";
  if (data.stage === "f0") return "正在提取音高";
  if (data.stage === "features") return "正在提取音色特征";
  return "正在训练";
}

export function RvcTrainingRoute() {
  const [data, setData] = useState<TrainingProgress | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [shutdown, setShutdown] = useState<ShutdownStatus | null>(null);
  const [shutdownBusy, setShutdownBusy] = useState(false);
  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const next = await readJson<TrainingProgress>(fetch("/api/rvc-training/progress", { cache: "no-store" }), "读取训练进度失败");
      setData(next);
      const shutdownNext = await readJson<ShutdownStatus>(fetch("/api/rvc-training/shutdown", { cache: "no-store" }), "读取关机设置失败");
      setShutdown(shutdownNext);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取训练进度失败");
    } finally {
      setRefreshing(false);
    }
  }, []);

  const toggleShutdown = async () => {
    if (!shutdown || shutdownBusy) return;
    setShutdownBusy(true);
    try {
      const next = await readJson<ShutdownStatus>(fetch("/api/rvc-training/shutdown", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !shutdown.enabled }),
      }), "更新关机设置失败");
      setShutdown(next);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "更新关机设置失败");
    } finally {
      setShutdownBusy(false);
    }
  };

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  return <main className="training-page">
    <header className="training-header">
      <div className="training-icon"><MicrophoneStage size={28} weight="fill" /></div>
      <div><p className="training-eyebrow">RVC 模型训练</p><h1>音色训练</h1><p>mogoo · 48kHz · v2 · 带音高</p></div>
      <button type="button" onClick={() => void refresh()} disabled={refreshing}><ArrowsClockwise size={18} /> {refreshing ? "刷新中…" : "刷新"}</button>
    </header>
    {error && <p className="training-error">{error}</p>}
    {data && <section className="training-panel" aria-live="polite">
      <div className="training-status"><span className={`training-dot ${data.state}`} />{stateLabel(data)}</div>
      <div className="training-count"><strong>{data.completedEpochs}</strong><span> / {data.targetEpochs} 轮</span></div>
      <div className="training-progress" role="progressbar" aria-label="训练进度" aria-valuenow={data.progress} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${data.progress}%` }} /></div>
      <p className="training-percent">{data.progress.toFixed(2)}% 完成{data.state === "running" && data.currentEpoch > data.completedEpochs ? ` · 第 ${data.currentEpoch} 轮已进行 ${data.currentEpochPercent}%` : ""}</p>
      <div className="training-metrics">
        <div><small>已运行时间</small><strong>{duration(data.elapsedSeconds)}</strong></div>
        <div><small>预计剩余</small><strong>{data.state === "completed" ? "已完成" : duration(data.etaSeconds)}</strong></div>
        <div><small>开始时间</small><strong>{data.startedAt ? new Date(data.startedAt).toLocaleString("zh-CN") : "--"}</strong></div>
      </div>
      {shutdown && <div className="training-shutdown">
        <div><strong>训练完成后关机</strong><p>成功完成 500 轮后，等待 5 分钟再关机。训练失败或中断不会触发。</p>
          {!shutdown.watcherAlive && <small className="training-error">关机监控未运行，请检查后台服务。</small>}
          {shutdown.state === "scheduled" && shutdown.shutdownAt && <small>已安排关机：{new Date(shutdown.shutdownAt).toLocaleString("zh-CN")}</small>}
          {shutdown.state === "error" && shutdown.error && <small className="training-error">关机安排失败：{shutdown.error}</small>}
        </div>
        <button type="button" className={`training-switch ${shutdown.enabled ? "on" : ""}`} role="switch" aria-checked={shutdown.enabled} aria-label="训练完成后关机" disabled={shutdownBusy} onClick={() => void toggleShutdown()}><span />{shutdown.enabled ? "已开启" : "已关闭"}</button>
      </div>}
      <p className="training-note">上次刷新：{new Date(data.refreshedAt).toLocaleTimeString("zh-CN")} · 最近训练日志：{data.lastUpdate ? new Date(data.lastUpdate).toLocaleTimeString("zh-CN") : "暂无"}。每 5 秒自动更新，关闭页面不会停止训练。</p>
    </section>}
  </main>;
}
