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
  FolderOpen,
  Graph,
  Info,
  Pause,
  Play,
  SpinnerGap,
  Subtitles,
  UploadSimple,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { QueuePanel } from "./QueuePanel";
import { readJsonOrNull } from "./api";
import { elapsedMs, formatElapsedMs, useNowTick } from "./jobTime";
import type { JobState, Milestone, MilestoneStatus } from "./types";

type RecentItem = {
  id: string;
  kind: string;
  status: string;
  title: string;
  songName?: string | null;
  createdAt?: string;
  media: { key: string; label: string; url: string }[];
};

type RecentPayload = { jobs: RecentItem[] };

// 与 backend/store.subtitle_milestones() 一一对应：剪映识别时间轴 → 官方歌词核对文字 → 烧录
const SKELETON_MILESTONES: Milestone[] = [
  { id: "recognize", label: "剪映识别歌词", subtitle: "准备草稿并识别时间轴（打开草稿时会动一次鼠标）", status: "pending" },
  { id: "verify", label: "官方歌词核对", subtitle: "网易云官方歌词逐句核对文字，时间轴不动", status: "pending" },
  { id: "burn", label: "烧录字幕成片", subtitle: "ffmpeg + 剪映手书 JYgangbi", status: "pending" },
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
  if (kind === "subtitle") return "生成字幕";
  if (kind === "lyrics") return "歌词字幕";
  if (kind === "migrate") return "动作迁移";
  if (kind === "upscale") return "二采放大";
  if (kind === "rvc") return "音色转换";
  return "歌曲生成";
}

function MilestoneIcon({ status }: { status: MilestoneStatus }) {
  if (status === "completed") return <Check weight="bold" />;
  if (status === "running") return <Play weight="fill" />;
  if (status === "skipped") return <ArrowsClockwise />;
  if (status === "error") return <WarningCircle weight="fill" />;
  return <Circle weight="regular" />;
}

function PipelineRow({ step, index, liveNow }: { step: Milestone; index: number; liveNow?: number | null }) {
  const elapsedText =
    step.status === "running" && step.startedAt && liveNow
      ? formatElapsedMs(liveNow - Date.parse(step.startedAt))
      : step.elapsed || "--:--";

  return (
    <div className={`pipeline-row state-${step.status} comfy-row`}>
      <div className="rail"><span className="rail-icon"><MilestoneIcon status={step.status} /></span></div>
      <div className="pipeline-card">
        <span className="step-number">{String(index + 1).padStart(2, "0")}</span>
        <div className="step-copy">
          <div className="step-title-line"><strong>{step.label}</strong></div>
          <span>{step.subtitle}</span>
        </div>
        <span className="elapsed">{elapsedText}</span>
        <span className="status-chip">{statusLabel(step.status)}</span>
      </div>
    </div>
  );
}

export function SubtitleRoute() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const socketRef = useRef<WebSocket | null>(null);

  const [mode, setMode] = useState<"upload" | "recent">("upload");
  const [file, setFile] = useState<File | null>(null);
  const [recent, setRecent] = useState<RecentItem[]>([]);
  const [recentPick, setRecentPick] = useState<{ jobId: string; key: string; label: string } | null>(null);
  const [song, setSong] = useState("");
  const [job, setJob] = useState<JobState | null>(null);
  const [logsOpen, setLogsOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [queueOpen, setQueueOpen] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [batchSubmitting, setBatchSubmitting] = useState(false);

  // 「进行中」含 cancelling：点了取消/重新开始之后到真正收尾之间，按钮不该又变得可点
  // 「waiting」= 已接单、在等流水线空闲（这条链路不用 ComfyUI，但剪映要独占桌面与显存）
  const LIVE_STATUSES = ["waiting", "queued", "running", "cancelling"];
  const jobActive = Boolean(job && LIVE_STATUSES.includes(job.status));
  // 一键批量任务（kind === "subtitle_batch"）：跑着/暂停时占用这条链路，单条 UI 让位；
  // 已经跑完/失败的批量只是「上一次任务」，不该把单条生成入口藏起来。
  const isBatch = job?.kind === "subtitle_batch";
  const batchBusy = Boolean(isBatch && ["waiting", "queued", "running", "cancelling", "paused"].includes(job?.status || ""));
  // 失败/取消/中断（以及已完成的）都能「重新开始」：源成片留在任务目录里，不用重新上传
  const canRestart = Boolean(job && ["failed", "cancelled", "interrupted", "completed"].includes(job.status));
  const tickNow = useNowTick(jobActive);
  const totalElapsedMs = elapsedMs(job?.startedAt || job?.createdAt, job?.finishedAt, tickNow);
  const milestones = job?.milestones?.length ? job.milestones : SKELETON_MILESTONES;
  const picked = recent.find((item) => item.id === recentPick?.jobId) || null;

  const loadRecent = useCallback(async () => {
    try {
      const response = await fetch("/api/jobs/recent", { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json() as RecentPayload;
      // 已经带字幕的成片（字幕任务本身）不再作为输入：往字幕版上再烧一遍只会叠字
      const jobs = (payload.jobs || []).filter(
        (item) => item.kind !== "subtitle" && item.media.some((entry) => entry.key === "final"),
      );
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
    const single = readJsonOrNull<JobState>(fetch("/api/jobs/latest?kind=subtitle", { cache: "no-store" }), "读取上次任务失败");
    const batch = readJsonOrNull<JobState>(fetch("/api/jobs/latest?kind=subtitle_batch", { cache: "no-store" }), "读取上次批量失败");
    Promise.all([single, batch])
      .then(([singleJob, batchJob]) => {
        if (cancelled) return;
        const live = ["waiting", "queued", "running", "paused", "cancelling"];
        const activeBatch = batchJob && live.includes(batchJob.status) ? batchJob : null;
        const activeSingle = singleJob && live.includes(singleJob.status) ? singleJob : null;
        // 优先恢复「正在跑/暂停」的任务（批量优先），都没有就显示最近一次结束的任务，让上次结果还看得见
        const terminal = [batchJob, singleJob]
          .filter((item): item is JobState => Boolean(item && !live.includes(item.status)))
          .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))[0] || null;
        const latest = activeBatch || activeSingle || terminal;
        if (!latest?.id) return;
        setJob(latest);
        if (latest.songName) setSong(latest.songName);
        if (["waiting", "queued", "running", "cancelling"].includes(latest.status)) connectJob(latest.id);
      })
      .catch(() => setLocalError("本地服务尚未启动，启动后页面会自动连接任务系统。"));
    return () => {
      cancelled = true;
      socketRef.current?.close();
    };
  }, [connectJob]);

  useEffect(() => {
    if (!job || !["waiting", "queued", "running", "cancelling"].includes(job.status)) return;
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
      setLocalError("请先选择要加字幕的成片。");
      fileInputRef.current?.click();
      return;
    }
    if (mode === "recent" && !recentPick) {
      setLocalError("暂无可加字幕的历史任务成片。");
      return;
    }
    setSubmitting(true);
    setLocalError(null);
    const form = new FormData();
    if (mode === "upload" && file) form.append("video", file);
    if (mode === "recent" && recentPick) {
      form.append("source_job_id", recentPick.jobId);
      form.append("source_key", recentPick.key);
    }
    form.append("song_name", song.trim());
    try {
      const response = await fetch("/api/jobs/subtitle", { method: "POST", body: form });
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

  const completionTime = useMemo(() => {
    const value = job?.output?.completedAt;
    if (!value) return "--";
    return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  }, [job?.output?.completedAt]);

  // 排队中（waiting）：已接单、在等流水线空闲。这条链路不用 ComfyUI，但剪映要独占桌面与显存，
  // 所以不能和正在出片的任务同时跑 —— 当时那句「已有任务正在运行」就是直接 409 的报错来源。
  const queued = job?.status === "waiting";

  /** 重新开始：沿用同一条源成片重跑（失败/取消/中断/已完成都能点）。 */
  const restart = async () => {
    if (!job) return;
    setRestarting(true);
    setLocalError(null);
    try {
      const response = await fetch(`/api/jobs/${job.id}/retry`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "重新开始失败");
      setJob(payload);
      connectJob(payload.id);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "重新开始失败");
    } finally {
      setRestarting(false);
    }
  };

  /** 打开发布目录（字幕版落地的那个 `{编号}_{歌名}_{作品号}` 目录）。 */
  const openPublish = async () => {
    if (!job) return;
    try {
      const response = await fetch(`/api/jobs/${job.id}/open-publish`, { method: "POST" });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.detail || "打开发布目录失败");
      }
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "打开发布目录失败");
    }
  };

  /** 发布目录里的相对位置：`{目录} / 最终成片_字幕版.mp4`（完整路径在 title 里）。 */
  const publishLabel = useMemo(() => {
    const raw = job?.subtitlePublishPath || "";
    const parts = raw.split(/[\\/]/).filter(Boolean);
    return parts.length >= 2 ? `${parts[parts.length - 2]} / ${parts[parts.length - 1]}` : raw;
  }, [job?.subtitlePublishPath]);

  /** 取消正在跑的这一条（卡住时用它脱身，之后可以「重新开始」）。 */
  const cancelRun = async () => {
    if (!job) return;
    setLocalError(null);
    try {
      const response = await fetch(`/api/jobs/${job.id}/cancel`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "取消失败");
      setJob(payload);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "取消失败");
    }
  };

  /** 一键批量：给发布成品里所有缺字幕版的唱歌视频逐条生成字幕（后端串行，已存在的不重复）。 */
  const startBatch = async () => {
    if (jobActive) return;
    setBatchSubmitting(true);
    setLocalError(null);
    try {
      const response = await fetch("/api/jobs/subtitle/publish-batch", { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "一键批量启动失败");
      setJob(payload);
      connectJob(payload.id);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "一键批量启动失败");
    } finally {
      setBatchSubmitting(false);
    }
  };

  /** 暂停一键批量：当前这条跑完即停（剪映识别进行到一半没法安全打断）。 */
  const pauseBatch = async () => {
    if (!job) return;
    setBatchSubmitting(true);
    setLocalError(null);
    try {
      const response = await fetch(`/api/jobs/${job.id}/pause`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "暂停失败");
      setJob(payload);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "暂停失败");
    } finally {
      setBatchSubmitting(false);
    }
  };

  /** 继续一条已暂停的一键批量，从暂停处接着生成字幕。 */
  const resumeBatch = async () => {
    if (!job) return;
    setBatchSubmitting(true);
    setLocalError(null);
    try {
      const response = await fetch(`/api/jobs/${job.id}/resume`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "继续失败");
      setJob(payload);
      connectJob(payload.id);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "继续失败");
    } finally {
      setBatchSubmitting(false);
    }
  };

  return (
    <div className="subtitle-route">
      <header className="route-hero">
        <div>
          <p className="route-eyebrow"><span /> H3 · SUBTITLE</p>
          <h1>成片加字幕，<em>剪映的时间轴。</em></h1>
          <p className="route-description">
            用剪映「识别歌词」拿最准的时间轴，再用网易云官方歌词逐句核对文字（时间轴一字不动），
            最后按剪映手书观感烧录出字幕版成片；无字幕原片原样保留。
          </p>
        </div>
        <span className={`connection ${job?.status === "running" ? "connected" : queued ? "queued" : "idle"}`}>
          <span className="connection-dot" />
          {jobActive
            ? isBatch ? "批量生成字幕中" : "剪映识别中"
            : queued ? "已排队，轮到自动开始" : "资源空闲，按需启动"}
        </span>
      </header>

      <main className="workspace">
        <section className="input-panel" aria-label="生成字幕设置">
          {/* 一键批量放最上面：这是这个页面最常用的入口（用户 2026-09-19：「一键批量 放到最上边」） */}
          <div className="field-block">
            <div className="field-heading"><h2><span>⚡</span> 一键批量 <em>（发布成品里的唱歌视频）</em></h2></div>
            <div className="subtitle-batch-row">
              <button className="primary-action batch-action" onClick={startBatch} disabled={batchSubmitting || batchBusy || Boolean(job && jobActive)}>
                {batchSubmitting ? <SpinnerGap className="spin" /> : <Subtitles weight="fill" />}
                {batchSubmitting ? "正在扫描…" : "一键给所有唱歌视频生成字幕"}
              </button>
              <span className="action-hint">
                扫描 E:\AI_Exports\H3-MotionStudio\发布成品，给缺字幕版的唱歌视频逐条生成；
                已经出过字幕版的不重复生成。
              </span>
            </div>
            {batchBusy && (
              <div className="subtitle-batch-controls">
                {job?.status === "running" && (
                  <button className="ghost-action" onClick={pauseBatch} disabled={batchSubmitting}>
                    <Pause weight="fill" /> 暂停（当前这条跑完即停）
                  </button>
                )}
                {job?.status === "paused" && (
                  <button className="secondary-action" onClick={resumeBatch} disabled={batchSubmitting}>
                    <Play weight="fill" /> 继续生成
                  </button>
                )}
                {["paused", "running"].includes(job?.status || "") && (
                  <button className="ghost-action" onClick={cancelRun}>取消</button>
                )}
              </div>
            )}
          </div>

          <div className="field-block">
            <div className="field-heading"><h2><span>1.</span> 输入成片 <em>（要加字幕的那一条）</em></h2></div>
            <div className="ratio-cards" role="radiogroup" aria-label="输入来源">
              <button type="button" role="radio" aria-checked={mode === "upload"} className={mode === "upload" ? "selected" : ""} onClick={() => setMode("upload")}>
                <strong>上传本地视频</strong><span>MP4 / MOV / MKV / WebM</span>
              </button>
              <button type="button" role="radio" aria-checked={mode === "recent"} className={mode === "recent" ? "selected" : ""} onClick={() => setMode("recent")}>
                <strong>从最近任务选</strong><span>直接给该任务的最终成片加字幕</span>
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
                  <p className="queue-empty">暂无可加字幕的最终成片（先完成一单歌曲生成，或改用上传本地视频）</p>
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
                        onClick={() => {
                          if (!media) return;
                          setRecentPick({ jobId: item.id, key: "final", label: media.label });
                          if (item.songName) setSong(item.songName);
                        }}
                      >
                        <span className="recent-choice-head">
                          <strong>{kindLabel(item.kind)} · {item.title}</strong>
                          <em>{item.status === "completed" ? "已完成" : item.status}</em>
                        </span>
                        <span className="recent-choice-target"><Subtitles weight="bold" /> 给其{media?.label || "最终成片"}加字幕</span>
                      </button>
                    );
                  })
                )}
              </div>
            )}
            {mode === "recent" && picked && (
              <p className="field-note">将给任务 {picked.id.slice(0, 8)} 的最终成片（{kindLabel(picked.kind)}）加字幕。</p>
            )}
          </div>

          <div className="field-block text-field">
            <div className="field-heading"><h2><span>2.</span> 歌名 <em>（可选）</em></h2><span>{song.length} / 60</span></div>
            <textarea
              value={song}
              maxLength={60}
              rows={2}
              onChange={(event) => setSong(event.target.value)}
              placeholder="留空也可以：系统会拿剪映识别出的歌词去反查官方歌词"
            />
            <p className="field-note">
              填了歌名会优先按歌名找官方歌词；留空则用识别出的歌词反查。两条路都找不到时，
              字幕文字与时间轴都用剪映识别结果，不会因此失败。
            </p>
          </div>

          <div className="field-block">
            <div className="field-heading"><h2><span>3.</span> 链路说明</h2></div>
            <ul className="subtitle-steps">
              <li><strong>剪映识别歌词</strong><span>剪映云端对这条视频的真实音频做识别与对齐（时间最准）。打开草稿时会动一次鼠标，识别完自动关闭剪映。</span></li>
              <li><strong>官方歌词核对</strong><span>只核对文字：把识别错字改成官方歌词，剪映的断句与时间轴一个字都不动。</span></li>
              <li><strong>烧录字幕成片</strong><span>ffmpeg + libass 按剪映手书（JYgangbi）标定值烧录，与剪映导出的观感一致。</span></li>
            </ul>
            <p className="field-note">
              需要本机剪映可用（当前锁定 5.9.0.11632）。这条链路不经过 ComfyUI，但剪映要独占桌面
              （打开草稿会动一次鼠标）与显存，所以不能和正在出片的任务同时跑：提交时若还有任务在跑，
              会先排队，轮到本任务自动开始。
            </p>
          </div>

          {(localError || job?.errorSummary) && (
            <div className="error-banner" role="alert">
              <WarningCircle weight="fill" />
              <div><strong>任务需要处理</strong><span>{localError || job?.errorSummary}</span></div>
              {job?.errorDetail && <button onClick={() => setLogsOpen(true)}>查看详情</button>}
            </div>
          )}

          {canRestart && !batchBusy && (
            <div className="subtitle-actions">
              <button className="secondary-action" onClick={restart} disabled={restarting}>
                {restarting ? <SpinnerGap className="spin" /> : <ArrowsClockwise weight="bold" />}
                {restarting ? "正在重新开始…" : job?.status === "completed" ? "重新生成字幕版" : "重新开始"}
              </button>
              <span className="action-hint">
                沿用同一条源成片重跑（不用重新上传）；上一次的日志会保留，方便对照失败原因。
              </span>
            </div>
          )}

          {!batchBusy && (
            <>
              <button className="primary-action" onClick={submit} disabled={submitting || jobActive}>
                {submitting || jobActive ? <SpinnerGap className="spin" /> : <Subtitles weight="fill" />}
                {submitting ? "正在创建任务…" : queued ? "已排队，轮到自动开始" : jobActive ? "生成字幕任务运行中" : "开始生成字幕"}
              </button>
              {jobActive && (
                <div className="subtitle-actions">
                  <button className="ghost-action" onClick={cancelRun}>取消这一次</button>
                  <span className="action-hint">取消后可以点「重新开始」再来一次。</span>
                </div>
              )}
            </>
          )}
        </section>

        <section className="execution-panel" aria-label="执行进度与结果">
          <div className="panel-heading">
            <Graph />
            <div>
              <h2>执行流程</h2>
              <span>{queued ? "已接单排队 · 轮到本任务自动开始" : jobActive ? "剪映识别 · 文字核对 · 烧录" : "提交后展示实际进度"}</span>
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

          {queued && job?.currentNodeTitle && (
            <p className="field-note queue-note">
              <Info /> {job.currentNodeTitle} —— 这条链路不用 ComfyUI，只是剪映要独占桌面与显存，
              所以要等前面的出片任务结束；轮到自己会自动开始，不用重新提交。
            </p>
          )}

          <div className={`pipeline ${job?.finalReady ? "pipeline-compact" : ""}`}>
            {milestones.map((step, index) => (
              <div className="pipeline-step" key={step.id}>
                <PipelineRow step={step} index={index} liveNow={jobActive ? tickNow : null} />
              </div>
            ))}
          </div>

          {job?.finalReady && (
            <section className="result-panel">
              <div className="result-heading">
                <h3>字幕结果</h3>
                <span><Check weight="bold" /> 字幕版已完成{job.subtitleLineCount ? ` · ${job.subtitleLineCount} 句` : ""}</span>
              </div>
              <div className="result-grid">
                <div className="result-video">
                  <video src={`/api/jobs/${job.id}/media/final#t=0.001`} controls preload="auto" />
                </div>
                <div className="result-details">
                  <div className="result-title">
                    <Subtitles />
                    <div>
                      <strong>字幕版成片 · 剪映手书</strong>
                      <span>{job.output?.width && job.output?.height ? `${job.output.width} × ${job.output.height}` : ""}</span>
                    </div>
                  </div>
                  <dl>
                    <div><dt>字幕句数</dt><dd>{job.subtitleLineCount ?? "--"}</dd></div>
                    <div><dt>时长</dt><dd>{formatDuration(job.output?.duration || job.sourceDuration)}</dd></div>
                    <div><dt>文件大小</dt><dd>{formatBytes(job.output?.size)}</dd></div>
                    <div><dt>完成时间</dt><dd>{completionTime}</dd></div>
                    {totalElapsedMs != null && <div className="total-elapsed"><dt>任务总耗时</dt><dd>{formatElapsedMs(totalElapsedMs)}</dd></div>}
                  </dl>
                  {job.subtitleLyricSource && (
                    <p className="field-note">歌词来源：{job.subtitleLyricSource}{job.subtitleCorrected ? ` · 核对修正 ${job.subtitleCorrected} 句` : ""}</p>
                  )}
                  {job.subtitlePublishPath ? (
                    <div className="publish-line">
                      <FolderOpen weight="fill" />
                      <div>
                        <strong>已放进发布目录</strong>
                        <span title={job.subtitlePublishPath}>{publishLabel}</span>
                      </div>
                      <button onClick={openPublish}><FolderOpen /> 打开文件夹</button>
                    </div>
                  ) : job.subtitlePublishError ? (
                    <p className="field-note">
                      放进发布目录失败（字幕版仍在任务目录里，可直接下载）：{job.subtitlePublishError}
                    </p>
                  ) : null}
                  <a className="result-button primary" href={`/api/jobs/${job.id}/media/final?download=1`}><DownloadSimple /> 下载字幕版成片</a>
                  <a className="result-button" href={`/api/jobs/${job.id}/media/original`} target="_blank" rel="noreferrer"><Eye /> 对比无字幕原片</a>
                </div>
              </div>
            </section>
          )}

          <section className={`log-panel ${logsOpen ? "open" : ""}`}>
            <button className="log-toggle" onClick={() => setLogsOpen((value) => !value)}>
              <span><FileText /> 运行日志与文字核对</span>{logsOpen ? <CaretUp /> : <CaretDown />}
            </button>
            {logsOpen && (
              <div className="log-content">
                {job?.logs?.length ? job.logs.map((entry, index) => (
                  <div className="log-line" key={`${entry.time}-${index}`}><time>{formatClock(entry.time)}</time><span>{entry.message}</span></div>
                )) : <div className="empty-log"><Info /> 运行时会显示剪映识别结果、官方歌词来源与每一句的文字核对。</div>}
                {job?.errorDetail && <pre>{job.errorDetail}</pre>}
              </div>
            )}
          </section>
        </section>
      </main>
    </div>
  );
}
