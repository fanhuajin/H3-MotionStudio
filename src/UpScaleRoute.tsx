import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowsClockwise,
  CaretDown,
  CaretUp,
  Check,
  Circle,
  DownloadSimple,
  Eye,
  FileText,
  FilmSlate,
  Graph,
  Info,
  MagnifyingGlassPlus,
  Play,
  SpinnerGap,
  UploadSimple,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { QueuePanel } from "./QueuePanel";
import { elapsedMs, formatElapsedMs, useNowTick } from "./jobTime";
import type { JobState, Milestone, MilestoneStatus } from "./types";

type RecentItem = {
  id: string;
  kind: string;
  status: string;
  title: string;
  createdAt?: string;
  media: { key: string; label: string; url: string }[];
};

type RecentPayload = { jobs: RecentItem[] };

const SKELETON_MILESTONES: Milestone[] = [
  { id: "upscale", label: "RealESRGAN 逐帧放大", subtitle: "8 帧分批超采样", status: "pending" },
  { id: "hd", label: "收 1080p 档输出", subtitle: "缩放并封装输出视频", status: "pending" },
];

function formatBytes(value?: number | null) {
  if (!value) return "--";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index < 2 ? 0 : 1)} ${units[index]}`;
}

function formatDuration(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--:--";
  const total = Math.max(0, Math.floor(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatClock(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}

function statusLabel(status: MilestoneStatus) {
  return { pending: "等待中", running: "运行中", completed: "已完成", skipped: "已跳过", error: "出错" }[status];
}

function kindLabel(kind?: string) {
  if (kind === "migrate") return "动作迁移";
  if (kind === "upscale") return "二采放大";
  return "歌曲生成";
}

function MilestoneIcon({ status }: { status: MilestoneStatus }) {
  if (status === "completed") return <Check weight="bold" />;
  if (status === "running") return <Play weight="fill" />;
  if (status === "skipped") return <ArrowsClockwise />;
  if (status === "error") return <WarningCircle weight="fill" />;
  return <Circle weight="regular" />;
}

export function UpScaleRoute() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const socketRef = useRef<WebSocket | null>(null);

  const [mode, setMode] = useState<"upload" | "recent">("upload");
  const [file, setFile] = useState<File | null>(null);
  const [recent, setRecent] = useState<RecentItem[]>([]);
  const [recentPick, setRecentPick] = useState<{ jobId: string; key: string; label: string } | null>(null);
  const [multiplier, setMultiplier] = useState<"2x" | "4x">("4x");
  const [job, setJob] = useState<JobState | null>(null);
  const [logsOpen, setLogsOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [queueOpen, setQueueOpen] = useState(false);

  const jobActive = Boolean(job && ["queued", "running"].includes(job.status));
  const tickNow = useNowTick(jobActive);
  const totalElapsedMs = elapsedMs(job?.startedAt || job?.createdAt, job?.finishedAt, tickNow);
  const milestones = job?.milestones?.length ? job.milestones : SKELETON_MILESTONES;

  const loadRecent = useCallback(async () => {
    try {
      const response = await fetch("/api/jobs/recent", { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json() as RecentPayload;
      // 历史任务一律放大「最终成片」，不再让用户选原版/迁移等变体
      const jobs = (payload.jobs || []).filter((item) => item.media.some((entry) => entry.key === "final"));
      setRecent(jobs);
      setRecentPick((current) => {
        if (current && jobs.some((item) => item.id === current.jobId)) return current;
        const first = jobs[0]?.media.find((entry) => entry.key === "final");
        return first ? { jobId: jobs[0].id, key: "final", label: first.label } : null;
      });
    } catch {
      // 忽略：服务未就绪
    }
  }, []);

  const connectJob = useCallback((jobId: string) => {
    socketRef.current?.close();
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${protocol}://${window.location.host}/api/jobs/${jobId}/ws`);
    socketRef.current = ws;
    ws.onmessage = (event) => {
      const next = JSON.parse(event.data) as JobState;
      setJob(next);
      if (["completed", "failed", "cancelled", "interrupted"].includes(next.status)) ws.close();
    };
    ws.onerror = () => setLocalError("实时进度连接暂时中断，任务仍会在后台继续。页面会自动轮询状态。");
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetch("/api/jobs/latest?kind=upscale", { cache: "no-store" })
        .then((response) => response.status === 204 ? null : response.json()),
    ]).then(([latest]) => {
      if (cancelled) return;
      if (latest?.id) {
        setJob(latest);
        if (["queued", "running"].includes(latest.status)) connectJob(latest.id);
      }
    }).catch(() => setLocalError("本地服务尚未启动，启动后页面会自动连接任务系统。"));
    return () => {
      cancelled = true;
      socketRef.current?.close();
    };
  }, [connectJob]);

  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      const response = await fetch(`/api/jobs/${job.id}`, { cache: "no-store" });
      if (response.ok) setJob(await response.json());
    }, 4000);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status]);

  useEffect(() => {
    if (mode === "recent") void loadRecent();
  }, [mode, loadRecent]);

  const pickFile = (nextFile: File | null) => {
    if (!nextFile) return;
    const allowed = [".mp4", ".mov", ".mkv", ".webm"];
    const suffix = nextFile.name.slice(nextFile.name.lastIndexOf(".")).toLowerCase();
    if (!nextFile.type.startsWith("video/") && !allowed.includes(suffix)) {
      setLocalError("请选择 MP4、MOV、MKV 或 WebM 视频文件。");
      return;
    }
    setFile(nextFile);
    setRecentPick(null);
    setLocalError(null);
  };

  const submit = async () => {
    if (mode === "upload" && !file) {
      setLocalError("请先选择要放大的视频。");
      fileInputRef.current?.click();
      return;
    }
    if (mode === "recent" && !recentPick) {
      setLocalError("暂无可放大的历史任务成片。");
      return;
    }
    setSubmitting(true);
    setLocalError(null);
    const form = new FormData();
    form.append("multiplier", multiplier);
    if (mode === "upload" && file) form.append("video", file);
    if (mode === "recent" && recentPick) {
      form.append("source_job_id", recentPick.jobId);
      form.append("source_key", recentPick.key);
    }
    try {
      const response = await fetch("/api/jobs/upscale", { method: "POST", body: form });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "任务创建失败");
      setJob(payload);
      connectJob(payload.id);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "任务创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  const completionTime = useMemoSafe(job);

  return (
    <div className="upscale-route">
      <header className="route-hero">
        <div>
          <p className="route-eyebrow"><span /> H3 · UPSCALE</p>
          <h1>二采放大，<em>自主选择。</em></h1>
          <p className="route-description">对任意视频做 RealESRGAN 2× / 4× 放大，并统一收到 1080p 标准档（4:3→1440×1080、9:16→1080×1920、16:9→1920×1080）。</p>
        </div>
        <span className={`connection ${job?.status === "running" ? "connected" : "idle"}`}>
          <span className="connection-dot" />
          {job?.status === "running" ? "放大运行中" : "资源空闲，按需启动"}
        </span>
      </header>

      <main className="workspace">
        <section className="input-panel" aria-label="二采放大设置">
          <div className="field-block">
            <div className="field-heading"><h2><span>1.</span> 输入视频</h2></div>
            <div className="ratio-cards" role="radiogroup" aria-label="输入来源">
              <button type="button" role="radio" aria-checked={mode === "upload"} className={mode === "upload" ? "selected" : ""} onClick={() => setMode("upload")}>
                <strong>上传本地视频</strong><span>MP4 / MOV / MKV / WebM</span>
              </button>
              <button type="button" role="radio" aria-checked={mode === "recent"} className={mode === "recent" ? "selected" : ""} onClick={() => setMode("recent")}>
                <strong>从最近任务选</strong><span>直接放大该任务的最终成片</span>
              </button>
            </div>

            {mode === "upload" ? (
              <div className="upload-simple-row">
                <input ref={fileInputRef} className="visually-hidden" type="file" accept="video/mp4,video/quicktime,video/x-matroska,video/webm,.mkv" onChange={(event) => pickFile(event.target.files?.[0] || null)} />
                {file ? (
                  <div className="simple-file">
                    <FilmSlate weight="fill" />
                    <div><strong>{file.name}</strong><span>{formatBytes(file.size)}</span></div>
                    <button onClick={() => { setFile(null); if (fileInputRef.current) fileInputRef.current.value = ""; }} aria-label="移除视频" title="移除视频"><X weight="bold" /></button>
                  </div>
                ) : (
                  <button className="simple-upload" onClick={() => fileInputRef.current?.click()}>
                    <UploadSimple /> 点击选择视频
                  </button>
                )}
              </div>
            ) : (
              <div className="recent-pick">
                {recent.length === 0 ? (
                  <p className="queue-empty">暂无可放大的最终成片（先完成一单歌曲/迁移任务）</p>
                ) : (
                  recent.slice(0, 6).map((item) => {
                    const media = item.media.find((entry) => entry.key === "final");
                    const selected = recentPick?.jobId === item.id;
                    return (
                      <button
                        type="button"
                        role="radio"
                        aria-checked={selected}
                        className={`recent-choice ${selected ? "selected" : ""}`}
                        key={item.id}
                        onClick={() => media && setRecentPick({ jobId: item.id, key: "final", label: media.label })}
                      >
                        <span className="recent-choice-head">
                          <strong>{kindLabel(item.kind)} · {item.title}</strong>
                          <em>{item.status === "completed" ? "已完成" : item.status}</em>
                        </span>
                        <span className="recent-choice-target"><MagnifyingGlassPlus weight="bold" /> 放大其{media?.label || "最终成片"}</span>
                      </button>
                    );
                  })
                )}
              </div>
            )}
            {mode === "recent" && recentPick && (
              <p className="field-note">将放大任务 {recentPick.jobId.slice(0, 8)} 的最终成片（{kindLabel(recent.find((item) => item.id === recentPick.jobId)?.kind)}）</p>
            )}
          </div>

          <div className="field-block">
            <div className="field-heading"><h2><span>2.</span> 放大倍数</h2></div>
            <div className="ratio-cards" role="radiogroup" aria-label="放大倍数">
              <button type="button" role="radio" aria-checked={multiplier === "2x"} className={multiplier === "2x" ? "selected" : ""} onClick={() => setMultiplier("2x")}>
                <strong>2×（推荐）</strong><span>更快 · 竖版目标 2.1× 已够用</span>
              </button>
              <button type="button" role="radio" aria-checked={multiplier === "4x"} className={multiplier === "4x" ? "selected" : ""} onClick={() => setMultiplier("4x")}>
                <strong>4×</strong><span>细节最强 · 更慢</span>
              </button>
            </div>
          </div>

          {(localError || job?.errorSummary) && (
            <div className="error-banner" role="alert">
              <WarningCircle weight="fill" />
              <div><strong>任务需要处理</strong><span>{localError || job?.errorSummary}</span></div>
              {job?.errorDetail && <button onClick={() => setLogsOpen(true)}>查看详情</button>}
            </div>
          )}

          <button className="primary-action" onClick={submit} disabled={submitting || jobActive}>
            {submitting || jobActive ? <SpinnerGap className="spin" /> : <MagnifyingGlassPlus weight="fill" />}
            {submitting ? "正在创建任务…" : jobActive ? "放大任务运行中" : `开始 ${multiplier} 放大`}
          </button>
        </section>

        <section className="execution-panel" aria-label="执行进度与结果">
          <div className="panel-heading">
            <Graph />
            <div>
              <h2>执行流程</h2>
              <span>{jobActive ? "节点级运行状态" : "提交后展示实际进度"}</span>
            </div>
            {job && (
              <span className="queue-anchor">
                <button className={`job-id job-id-button ${queueOpen ? "job-id-open" : ""}`} onClick={() => setQueueOpen((value) => !value)} title="任务队列：查看当前任务并取消">
                  任务 {job.id.slice(0, 8)}
                </button>
                <QueuePanel open={queueOpen} onClose={() => setQueueOpen(false)} />
              </span>
            )}
            {jobActive && totalElapsedMs != null && (
              <span className="job-timer" title="任务已运行时间（含排队）"><Play weight="fill" /> {formatElapsedMs(totalElapsedMs)}</span>
            )}
          </div>

          <div className={`pipeline ${job?.finalReady ? "pipeline-compact" : ""}`}>
            {milestones.map((step, index) => (
              <div className="pipeline-step" key={step.id}>
                <div className={`pipeline-row state-${step.status} comfy-row`}>
                  <div className="rail"><span className="rail-icon"><MilestoneIcon status={step.status} /></span></div>
                  <div className="pipeline-card">
                    <span className="step-number">{String(index + 1).padStart(2, "0")}</span>
                    <div className="step-copy">
                      <div className="step-title-line"><strong>{step.label}</strong></div>
                      <span>{step.subtitle}</span>
                    </div>
                    <span className="elapsed">{step.elapsed || "--:--"}</span>
                    <span className="status-chip">{statusLabel(step.status)}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {job?.finalReady && (
            <section className="result-panel">
              <div className="result-heading"><h3>放大结果</h3><span><Check weight="bold" /> 放大成片已完成</span></div>
              <div className="result-grid">
                <div className="result-video">
                  <video src={`/api/jobs/${job.id}/media/final#t=0.001`} controls preload="auto" />
                </div>
                <div className="result-details">
                  <div className="result-title">
                    <FilmSlate />
                    <div>
                      <strong>{job.multiplier || "2×/4×"} 放大成片</strong>
                      <span>{job.output?.width && job.output?.height ? `${job.output.width} × ${job.output.height}` : ""}</span>
                    </div>
                  </div>
                  <dl>
                    <div><dt>时长</dt><dd>{formatDuration(job.output?.duration || job.sourceDuration)}</dd></div>
                    <div><dt>文件大小</dt><dd>{formatBytes(job.output?.size)}</dd></div>
                    <div><dt>完成时间</dt><dd>{completionTime}</dd></div>
                    {totalElapsedMs != null && <div className="total-elapsed"><dt>任务总耗时</dt><dd>{formatElapsedMs(totalElapsedMs)}</dd></div>}
                  </dl>
                  <a className="result-button primary" href={`/api/jobs/${job.id}/media/final?download=1`}><DownloadSimple /> 下载放大成片</a>
                  <a className="result-button" href={`/api/jobs/${job.id}/media/final`} target="_blank" rel="noreferrer"><Eye /> 在线查看</a>
                </div>
              </div>
            </section>
          )}

          <section className={`log-panel ${logsOpen ? "open" : ""}`}>
            <button className="log-toggle" onClick={() => setLogsOpen((value) => !value)}>
              <span><FileText /> 运行日志与报错详情</span>{logsOpen ? <CaretUp /> : <CaretDown />}
            </button>
            {logsOpen && (
              <div className="log-content">
                {job?.logs?.length ? job.logs.map((entry, index) => (
                  <div className="log-line" key={`${entry.time}-${index}`}><time>{formatClock(entry.time)}</time><span>{entry.message}</span></div>
                )) : <div className="empty-log"><Info /> 运行时会显示当前 ComfyUI 节点与错误详情。</div>}
                {job?.errorDetail && <pre>{job.errorDetail}</pre>}
              </div>
            )}
          </section>
        </section>
      </main>
    </div>
  );
}

function useMemoSafe(job: JobState | null): string {
  return useMemo(() => {
    const value = job?.output?.completedAt;
    if (!value) return "--";
    return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  }, [job?.output?.completedAt]);
}
