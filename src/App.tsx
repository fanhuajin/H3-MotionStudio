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
  MusicNotes,
  PersonSimpleRun,
  Play,
  SpinnerGap,
  Timer,
  UploadSimple,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { DouyinRoute } from "./DouyinRoute";
import { MigrateRoute } from "./MigrateRoute";
import { QueuePanel } from "./QueuePanel";
import { SystemMonitor } from "./SystemMonitor";
import { TaskTabStatus } from "./TaskTabStatus";
import { UpScaleRoute } from "./UpScaleRoute";
import { elapsedMs, formatElapsedMs, useNowTick } from "./jobTime";
import type { AppConfig, JobState, Milestone, MilestoneStatus } from "./types";

const EMPTY_MILESTONES: Milestone[] = [
  { id: "input", label: "读取视频与音频", subtitle: "加载输入视频，分离音频轨道", status: "pending" },
  { id: "h3", label: "H3 分段生成", subtitle: "按时长生成连续唱歌片段", status: "pending" },
  { id: "stitch", label: "防闪拼接", subtitle: "平滑衔接并裁切到输入时长", status: "pending" },
  { id: "handoff", label: "关闭 ComfyUI", subtitle: "释放内存和显存，切换到 RVC", status: "pending" },
  { id: "stems", label: "分离人声与伴奏", subtitle: "Demucs 提取演唱人声", status: "pending" },
  { id: "voice", label: "转换为 ranran 音色", subtitle: "RVC 模型执行音色转换", status: "pending" },
  { id: "mux", label: "替换最终成片音频", subtitle: "重新混音并封装最终 MP4", status: "pending" },
];

const FALLBACK_CONFIG: AppConfig = {
  comfyuiConnected: false,
  fixedReferenceUrl: "/assets/fixed-reference.png",
  defaultAction: "",
  defaultCamera: "",
  maxDurationSeconds: 40,
  environmentReady: false,
  missingRequirements: [],
};

const DRAFT_STORAGE_KEY = "h3-motionstudio:draft:v1";
const DRAFT_DB_NAME = "h3-motionstudio-draft";
const DRAFT_DB_VERSION = 1;
const DRAFT_FILE_STORE = "files";

interface DraftState {
  actionPrompt?: string;
  cameraPrompt?: string;
  videoName?: string | null;
  videoSize?: number | null;
  imageName?: string | null;
  imageSize?: number | null;
  savedAt?: string;
}

function readDraft(): DraftState | null {
  try {
    const raw = window.localStorage.getItem(DRAFT_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as DraftState;
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function writeDraft(changes: Partial<DraftState>) {
  try {
    const current = readDraft() || {};
    window.localStorage.setItem(
      DRAFT_STORAGE_KEY,
      JSON.stringify({ ...current, ...changes, savedAt: new Date().toISOString() }),
    );
  } catch {
    // Draft persistence is best effort; it must never block generation.
  }
}

function releaseObjectUrl(value: string | null) {
  if (value?.startsWith("blob:")) URL.revokeObjectURL(value);
}

function openDraftDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = window.indexedDB.open(DRAFT_DB_NAME, DRAFT_DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(DRAFT_FILE_STORE)) {
        request.result.createObjectStore(DRAFT_FILE_STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("草稿存储不可用"));
  });
}

async function saveDraftFile(kind: "video" | "image", file: File) {
  try {
    const db = await openDraftDb();
    await new Promise<void>((resolve, reject) => {
      const request = db.transaction(DRAFT_FILE_STORE, "readwrite").objectStore(DRAFT_FILE_STORE).put(file, kind);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error || new Error("草稿文件保存失败"));
    });
    db.close();
  } catch {
    // The text draft and backend job state remain useful if IndexedDB is unavailable.
  }
}

async function loadDraftFile(kind: "video" | "image"): Promise<File | null> {
  try {
    const db = await openDraftDb();
    const file = await new Promise<File | null>((resolve, reject) => {
      const request = db.transaction(DRAFT_FILE_STORE, "readonly").objectStore(DRAFT_FILE_STORE).get(kind);
      request.onsuccess = () => resolve(request.result instanceof File ? request.result : null);
      request.onerror = () => reject(request.error || new Error("草稿文件读取失败"));
    });
    db.close();
    return file;
  } catch {
    return null;
  }
}

async function clearDraftFile(kind: "video" | "image") {
  try {
    const db = await openDraftDb();
    await new Promise<void>((resolve, reject) => {
      const request = db.transaction(DRAFT_FILE_STORE, "readwrite").objectStore(DRAFT_FILE_STORE).delete(kind);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error || new Error("草稿文件删除失败"));
    });
    db.close();
  } catch {
    // Best effort cleanup only.
  }
}

const DEMO_MILESTONES: Milestone[] = EMPTY_MILESTONES.map((step, index) => ({
  ...step,
  status: "completed",
  progress: 100,
  elapsed: ["00:05", "01:20", "00:15", "00:04", "00:38", "01:12", "00:06"][index],
}));

const DEMO_JOB: JobState = {
  id: "demo-complete-2026",
  status: "completed",
  stage: "completed",
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
  startedAt: new Date(Date.now() - 9 * 60 * 1000).toISOString(),
  finishedAt: new Date().toISOString(),
  sourceName: "演唱视频.mp4",
  sourceSize: 21_400_000,
  sourceDuration: 32.4,
  referenceName: "人物参考图.png",
  referenceSize: 4_800_000,
  actionPrompt: "主角自然深情地演唱，眼神专注，偶尔闭眼沉浸；副歌时情绪增强，微微抬头，右手轻抬并随节奏摆动；整体动作自然流畅。",
  cameraPrompt: "以稳定的推轨为主，开场中景缓慢推进至近景；副歌时轻微环绕 15°，保持主体居中；间奏切至侧面 3/4 角度，收尾回到正面特写。",
  milestones: DEMO_MILESTONES,
  logs: [
    { time: new Date().toISOString(), message: "ComfyUI 工作流已完成，显存已释放。" },
    { time: new Date().toISOString(), message: "RVC：ranran 音色转换完成。" },
    { time: new Date().toISOString(), message: "最终 MP4 已完成音频替换。" },
  ],
  originalReady: true,
  enhancedReady: false,
  finalReady: true,
  output: { width: 640, height: 480, duration: 32.4, size: 9_600_000, completedAt: new Date().toISOString() },
};

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

function MilestoneIcon({ status }: { status: MilestoneStatus }) {
  if (status === "completed") return <Check weight="bold" />;
  if (status === "running") return <Play weight="fill" />;
  if (status === "skipped") return <ArrowsClockwise />;
  if (status === "error") return <WarningCircle weight="fill" />;
  return <Circle weight="regular" />;
}

function PipelineRow({ step, index, liveNow }: { step: Milestone; index: number; liveNow?: number | null }) {
  const progressText = step.progressMax
    ? `采样 ${step.progressValue ?? 0} / ${step.progressMax} · ${Math.round(step.progress ?? 0)}%`
    : step.currentNode || null;
  const isRvc = ["stems", "voice", "mux"].includes(step.id);
  const elapsedText =
    step.status === "running" && step.startedAt && liveNow
      ? formatElapsedMs(liveNow - Date.parse(step.startedAt))
      : step.elapsed || "--:--";

  return (
    <div className={`pipeline-row state-${step.status} ${isRvc ? "rvc-row" : "comfy-row"}`}>
      <div className="rail"><span className="rail-icon"><MilestoneIcon status={step.status} /></span></div>
      <div className="pipeline-card">
        <span className="step-number">{String(index + 1).padStart(2, "0")}</span>
        <div className="step-copy">
          <div className="step-title-line">
            <strong>{step.label}</strong>
            {progressText && <span className="node-detail">{progressText}</span>}
          </div>
          <span>{step.subtitle}</span>
          {step.status === "running" && step.progress != null && (
            <div className="node-progress" aria-label={`进度 ${Math.round(step.progress)}%`}>
              <span style={{ width: `${Math.max(3, step.progress)}%` }} />
            </div>
          )}
        </div>
        <span className="elapsed">{elapsedText}</span>
        <span className="status-chip">{statusLabel(step.status)}</span>
      </div>
    </div>
  );
}

function MotionStudioRoute() {
  const demoMode = new URLSearchParams(window.location.search).get("demo") === "complete";
  const inputRef = useRef<HTMLInputElement>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const [config, setConfig] = useState<AppConfig>(FALLBACK_CONFIG);
  const [file, setFile] = useState<File | null>(null);
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [imagePreviewUrl, setImagePreviewUrl] = useState<string | null>(null);
  const [previewPreparing, setPreviewPreparing] = useState(false);
  const [previewConverting, setPreviewConverting] = useState(false);
  const previewTokenRef = useRef(0);
  const [lightboxImage, setLightboxImage] = useState<string | null>(null);

  useEffect(() => {
    if (!lightboxImage) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setLightboxImage(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightboxImage]);
  const [duration, setDuration] = useState<number | null>(null);
  const [actionPrompt, setActionPrompt] = useState(() => readDraft()?.actionPrompt || "");
  const [cameraPrompt, setCameraPrompt] = useState(() => readDraft()?.cameraPrompt || "");
  const [job, setJob] = useState<JobState | null>(demoMode ? DEMO_JOB : null);
  const [logsOpen, setLogsOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);

  const milestones = job?.milestones?.length ? job.milestones : EMPTY_MILESTONES;
  const isBusy = submitting || job?.status === "queued" || job?.status === "running";
  const jobActive = Boolean(job && ["queued", "running"].includes(job.status));
  const tickNow = useNowTick(jobActive);
  // 计时起点：开始执行时间（旧任务没有则退回创建时间）；运行中实时、结束后定格
  const totalElapsedMs = elapsedMs(job?.startedAt || job?.createdAt, job?.finishedAt, tickNow);
  const [queueOpen, setQueueOpen] = useState(false);
  const resultUrl = !demoMode && job?.finalReady ? `/api/jobs/${job.id}/media/final` : null;
  const originalUrl = !demoMode && job?.originalReady ? `/api/jobs/${job.id}/media/original` : null;

  const resourceStatus = useMemo(() => {
    if (job?.status === "running" && ["voice", "handoff"].includes(job.stage)) return { label: "RVC 单链路运行中", mode: "rvc" };
    if (job?.status === "running") return { label: "ComfyUI 运行中", mode: "connected" };
    if (config.comfyuiConnected) return { label: "ComfyUI 已连接", mode: "connected" };
    return { label: "资源空闲，按需启动", mode: "idle" };
  }, [config.comfyuiConnected, job?.stage, job?.status]);

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
    if (!job || !["queued", "running"].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      const response = await fetch(`/api/jobs/${job.id}`, { cache: "no-store" });
      if (response.ok) setJob(await response.json());
    }, 4000);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status]);

  useEffect(() => () => {
    releaseObjectUrl(previewUrl);
    releaseObjectUrl(imagePreviewUrl);
  }, [previewUrl, imagePreviewUrl]);

  const chooseImage = useCallback((nextFile: File | null) => {
    if (!nextFile) return;
    const allowed = [".png", ".jpg", ".jpeg", ".webp"];
    const suffix = nextFile.name.slice(nextFile.name.lastIndexOf(".")).toLowerCase();
    if (!nextFile.type.startsWith("image/") && !allowed.includes(suffix)) {
      setLocalError("请选择 PNG、JPG、JPEG 或 WebP 人物图片。");
      return;
    }
    setImageFile(nextFile);
    setImagePreviewUrl((current) => {
      releaseObjectUrl(current);
      return URL.createObjectURL(nextFile);
    });
    void saveDraftFile("image", nextFile);
    writeDraft({ imageName: nextFile.name, imageSize: nextFile.size });
    setLocalError(null);
  }, []);

  const chooseFile = useCallback(async (nextFile: File | null) => {
    if (!nextFile) return;
    const allowed = [".mp4", ".mov", ".mkv", ".webm"];
    const suffix = nextFile.name.slice(nextFile.name.lastIndexOf(".")).toLowerCase();
    if (!nextFile.type.startsWith("video/") && !allowed.includes(suffix)) {
      setLocalError("请选择 MP4、MOV、MKV 或 WebM 视频文件。");
      return;
    }
    // The browser cannot decode HEVC/H.265 (typical for Douyin downloads),
    // so the backend stores the file and serves an H.264 preview copy; the
    // original file is still what gets submitted to the pipeline.
    const token = ++previewTokenRef.current;
    setFile(nextFile);
    setPreviewUrl((current) => {
      releaseObjectUrl(current);
      return null;
    });
    setDuration(null);
    setPreviewConverting(false);
    setPreviewPreparing(true);
    setLocalError(null);
    void saveDraftFile("video", nextFile);
    writeDraft({ videoName: nextFile.name, videoSize: nextFile.size });
    try {
      const form = new FormData();
      form.append("video", nextFile);
      const response = await fetch("/api/uploads/preview", { method: "POST", body: form });
      const payload = await response.json().catch(() => null);
      if (token !== previewTokenRef.current) return;
      if (!response.ok || !payload?.url) throw new Error(payload?.detail || "预览准备失败");
      if (payload.converting) {
        setPreviewConverting(true);
        let previewReady = false;
        const deadline = Date.now() + 180_000;
        while (Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          if (token !== previewTokenRef.current) return;
          const statusResponse = await fetch(`/api/uploads/${payload.uploadId}/status`);
          const status = await statusResponse.json().catch(() => null);
          if (!statusResponse.ok || !status) throw new Error("预览状态读取失败");
          if (!status.converting) {
            if (status.ready) {
              previewReady = true;
              setPreviewUrl(status.url);
            }
            break;
          }
        }
        if (!previewReady) setLocalError("预览转码超时；不影响提交，管线可直接处理该视频。");
      } else {
        setPreviewUrl(payload.url);
      }
    } catch {
      if (token !== previewTokenRef.current) return;
      // Backend unavailable: fall back to the local file preview. Some
      // codecs may not play in the browser, but generation is unaffected.
      setPreviewUrl(URL.createObjectURL(nextFile));
    } finally {
      if (token === previewTokenRef.current) {
        setPreviewConverting(false);
        setPreviewPreparing(false);
      }
    }
  }, []);

  const restoreInputs = useCallback(async (latest: JobState | null) => {
    const restoreRemote = async (kind: "video" | "reference", name: string, fallbackType: string) => {
      if (!latest?.id) return null;
      try {
        const response = await fetch(`/api/jobs/${latest.id}/input/${kind}`, { cache: "no-store" });
        if (!response.ok) return null;
        const blob = await response.blob();
        return new File([blob], name, { type: blob.type || fallbackType, lastModified: Date.now() });
      } catch {
        return null;
      }
    };

    const draft = readDraft();
    const restoredImage = await restoreRemote("reference", latest?.referenceName || "reference.png", "image/png")
      || await loadDraftFile("image");
    if (restoredImage) chooseImage(restoredImage);

    const restoredRemoteVideo = await restoreRemote("video", latest?.sourceName || "singing-video.mp4", "video/mp4");
    if (restoredRemoteVideo && latest?.id) {
      setFile(restoredRemoteVideo);
      setDuration(latest.sourceDuration ?? null);
      setPreviewPreparing(false);
      setPreviewConverting(false);
      setPreviewUrl(`/api/jobs/${latest.id}/input/video/preview`);
      void saveDraftFile("video", restoredRemoteVideo);
      writeDraft({ videoName: restoredRemoteVideo.name, videoSize: restoredRemoteVideo.size });
    } else {
      const restoredDraftVideo = await loadDraftFile("video");
      if (restoredDraftVideo) await chooseFile(restoredDraftVideo);
    }

    if (!latest && draft) {
      setActionPrompt(draft.actionPrompt || "");
      setCameraPrompt(draft.cameraPrompt || "");
    }
  }, [chooseFile, chooseImage]);

  useEffect(() => {
    let cancelled = false;
    const draft = readDraft();
    Promise.all([
      fetch("/api/config", { cache: "no-store" }).then((response) => response.ok ? response.json() : Promise.reject()),
      demoMode
        ? Promise.resolve(null)
        : fetch("/api/jobs/latest?kind=singing", { cache: "no-store" }).then((response) => response.status === 204 ? null : response.json()),
    ]).then(([nextConfig, latest]) => {
      if (cancelled) return;
      setConfig(nextConfig);
      setActionPrompt(latest?.actionPrompt || draft?.actionPrompt || nextConfig.defaultAction || "");
      setCameraPrompt(latest?.cameraPrompt || draft?.cameraPrompt || nextConfig.defaultCamera || "");
      if (demoMode) {
        setJob(DEMO_JOB);
        setActionPrompt(DEMO_JOB.actionPrompt);
        setCameraPrompt(DEMO_JOB.cameraPrompt);
      } else if (latest?.id) {
        setJob(latest);
        if (["queued", "running"].includes(latest.status)) connectJob(latest.id);
      }
      void restoreInputs(latest);
    }).catch(() => {
      if (!cancelled) setLocalError("本地服务尚未启动，启动后页面会自动连接任务系统。");
    });
    return () => {
      cancelled = true;
      socketRef.current?.close();
    };
  }, [connectJob, demoMode, restoreInputs]);

  useEffect(() => {
    if (demoMode) return;
    writeDraft({ actionPrompt, cameraPrompt });
  }, [actionPrompt, cameraPrompt, demoMode]);

  const clearFile = () => {
    previewTokenRef.current += 1;
    releaseObjectUrl(previewUrl);
    setFile(null);
    setPreviewUrl(null);
    setDuration(null);
    setPreviewPreparing(false);
    setPreviewConverting(false);
    void clearDraftFile("video");
    writeDraft({ videoName: null, videoSize: null });
    if (inputRef.current) inputRef.current.value = "";
  };

  const submit = async () => {
    if (!imageFile && !demoMode) {
      setLocalError("请先上传一张人物参考图片。");
      imageInputRef.current?.click();
      return;
    }
    if (!file) {
      setLocalError("请先上传一个带歌声的视频。");
      inputRef.current?.click();
      return;
    }
    if (duration && duration > config.maxDurationSeconds + 0.25) {
      setLocalError(`视频时长不能超过 ${config.maxDurationSeconds} 秒，当前为 ${formatDuration(duration)}。`);
      return;
    }
    setSubmitting(true);
    setLocalError(null);
    const form = new FormData();
    form.append("video", file);
    if (imageFile) form.append("reference_image", imageFile);
    form.append("action_prompt", actionPrompt);
    form.append("camera_prompt", cameraPrompt);
    if (duration != null) form.append("duration", String(duration));
    try {
      const response = await fetch("/api/jobs", { method: "POST", body: form });
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

  const retryVoice = async () => {
    if (!job) return;
    setLocalError(null);
    const response = await fetch(`/api/jobs/${job.id}/retry-voice`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      setLocalError(payload.detail || "无法重新进行音色转换");
      return;
    }
    setJob(payload);
    connectJob(job.id);
  };

  const completionTime = useMemo(() => {
    const value = job?.output?.completedAt;
    if (!value) return "--";
    return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  }, [job?.output?.completedAt]);

  return (
    <div className="motion-route">
      <header className="route-hero motion-hero">
        <div>
          <p className="route-eyebrow"><span /> H3 · MOTION STUDIO</p>
          <h1>让演唱视频，<em>动起来。</em></h1>
          <p className="route-description">人物参考、演唱视频、动作和运镜，一条链路完成唱歌成片与音色转换；需要高清时用「二采放大」单独处理。</p>
        </div>
        <span className={`connection ${resourceStatus.mode}`}><span className="connection-dot" />{resourceStatus.label}</span>
      </header>

      <main className="workspace">
        <section className="input-panel" aria-label="生成设置">
          <div className="field-block">
            <div className="field-heading"><h2><span>1.</span> 上传人物图片与演唱视频</h2></div>
            <div className="source-grid">
              <div className="source-card image-source">
                <div className="source-label">人物参考图</div>
                <input ref={imageInputRef} className="visually-hidden" type="file" accept="image/png,image/jpeg,image/webp,.jpg,.jpeg" onChange={(event) => chooseImage(event.target.files?.[0] || null)} />
                {(imagePreviewUrl || demoMode) ? (
                  <div className="image-preview-wrap">
                    <button className="image-preview-button" onClick={() => setLightboxImage(imagePreviewUrl || config.fixedReferenceUrl)} aria-label="放大预览人物参考图">
                      <img src={imagePreviewUrl || config.fixedReferenceUrl} alt="人物参考图预览" />
                      <span><Graph weight="fill" /> 点击放大</span>
                    </button>
                    <button className="image-swap" onClick={() => imageInputRef.current?.click()} aria-label="更换人物参考图片">
                      <ArrowsClockwise weight="bold" /> 更换
                    </button>
                  </div>
                ) : (
                  <button className="image-empty" onClick={() => imageInputRef.current?.click()}>
                    <UploadSimple /><strong>上传图片</strong><span>PNG / JPG / WebP</span>
                  </button>
                )}
              </div>
              <div className="source-card video-source">
                <div className="source-label">演唱视频 · 含音频</div>
            <input ref={inputRef} className="visually-hidden" type="file" accept="video/mp4,video/quicktime,video/x-matroska,video/webm,.mkv" onChange={(event) => chooseFile(event.target.files?.[0] || null)} />
            <div
              className={`media-input ${dragActive ? "drag-active" : ""}`}
              onDragOver={(event) => { event.preventDefault(); setDragActive(true); }}
              onDragLeave={() => setDragActive(false)}
              onDrop={(event) => { event.preventDefault(); setDragActive(false); chooseFile(event.dataTransfer.files?.[0] || null); }}
            >
              {previewUrl ? (
                <>
                  <video src={previewUrl} controls preload="metadata" onLoadedMetadata={(event) => setDuration(event.currentTarget.duration)} />
                  <div className="media-meta">
                    <div><strong>{file?.name}</strong><span>{formatDuration(duration)} · {formatBytes(file?.size)}</span></div>
                    <button onClick={clearFile} aria-label="移除视频" title="移除视频"><X /></button>
                  </div>
                </>
              ) : previewPreparing ? (
                <div className="upload-busy">
                  <SpinnerGap className="spin" weight="bold" />
                  <strong>{previewConverting ? "正在转为可播放预览…" : "正在准备预览…"}</strong>
                  <span>H.265 / HEVC 视频将自动转码为 H.264<br />耗时约几秒，不影响后续生成</span>
                </div>
              ) : demoMode ? (
                <>
                  <img className="demo-source" src={config.fixedReferenceUrl} alt="演唱视频画面预览" />
                  <div className="media-meta demo-meta">
                    <div><strong>演唱视频.mp4</strong><span>00:00:32 · 21.4 MB</span></div>
                  </div>
                </>
              ) : (
                <button className="empty-upload" onClick={() => inputRef.current?.click()}>
                  <span className="upload-icon"><UploadSimple /></span>
                  <strong>点击选择或拖入演唱视频</strong>
                  <span>支持 MP4 / MOV / MKV，最长 {config.maxDurationSeconds} 秒</span>
                </button>
              )}
            </div>
              </div>
            </div>
            <p className="field-note">图片作为人物与首帧参考；视频提供歌声与时长。音色转换会在 ComfyUI 完全关闭后自动进行。</p>
          </div>

          <div className="field-block text-field">
            <div className="field-heading"><h2><span>2.</span> 人物动作 <em>（表情、肢体与互动细节）</em></h2><span>{actionPrompt.length} / 2000</span></div>
            <textarea value={actionPrompt} maxLength={2000} onChange={(event) => setActionPrompt(event.target.value)} placeholder="描述人物在不同时间段的动作；留空将使用工作流默认动作。" />
          </div>

          <div className="field-block text-field">
            <div className="field-heading"><h2><span>3.</span> 运镜要求 <em>（镜头运动与构图节奏）</em></h2><span>{cameraPrompt.length} / 2000</span></div>
            <textarea value={cameraPrompt} maxLength={2000} onChange={(event) => setCameraPrompt(event.target.value)} placeholder="描述推、拉、摇、移以及人物构图；留空将使用工作流默认运镜。" />
          </div>

          {!config.environmentReady && config.missingRequirements.length > 0 && (
            <div className="error-banner"><WarningCircle weight="fill" /><div><strong>本地环境缺少文件</strong><span>{config.missingRequirements.join("、")}</span></div></div>
          )}
          {(localError || job?.errorSummary) && (
            <div className="error-banner" role="alert">
              <WarningCircle weight="fill" />
              <div><strong>任务需要处理</strong><span>{localError || job?.errorSummary}</span></div>
              {job?.errorDetail && <button onClick={() => setLogsOpen(true)}>查看详情</button>}
            </div>
          )}

          <button className="primary-action" onClick={submit} disabled={isBusy || !config.environmentReady}>
            {isBusy ? <SpinnerGap className="spin" /> : <Play weight="fill" />}
            {isBusy ? "正在执行单链路任务" : "开始生成最终成片"}
          </button>
        </section>

        <section className="execution-panel" aria-label="执行进度与结果">
          <div className="panel-heading">
            <Graph />
            <div><h2>执行流程</h2><span>严格单链路 · 节点级运行状态</span></div>
            {job && (
              <span className="queue-anchor">
                <button className={`job-id job-id-button ${queueOpen ? "job-id-open" : ""}`} onClick={() => setQueueOpen((value) => !value)} title="任务队列：查看当前任务并取消">
                  任务 {job.id.slice(0, 8)}
                </button>
                <QueuePanel open={queueOpen} onClose={() => setQueueOpen(false)} />
              </span>
            )}
            {jobActive && totalElapsedMs != null && (
              <span className="job-timer" title="任务已运行时间（含排队）"><Timer weight="fill" /> {formatElapsedMs(totalElapsedMs)}</span>
            )}
          </div>

          <div className={`pipeline ${job?.finalReady ? "pipeline-compact" : ""}`}>
            {milestones.map((step, index) => (
              <div className="pipeline-step" key={step.id}>
                <PipelineRow step={step} index={index} liveNow={jobActive ? tickNow : null} />
                {step.id === "handoff" && (
                  <div className={`handoff-banner ${step.status === "completed" ? "ready" : ""}`}>
                    <ArrowsClockwise weight="bold" />
                    <span>{step.status === "completed" ? "资源已切换到 RVC 流程" : "ComfyUI 关闭后才会启动 RVC"}</span>
                  </div>
                )}
              </div>
            ))}
          </div>

          {(job?.originalReady || job?.finalReady) && (
            <section className="result-panel">
              <div className="result-heading"><h3>生成结果</h3><span><Check weight="bold" /> {job.finalReady ? "最终成片已完成" : "原版成片已保留"}</span></div>
              <div className="result-grid">
                <div className="result-video"><video src={!demoMode && (resultUrl || originalUrl) ? `${resultUrl || originalUrl}#t=0.001` : undefined} controls preload="auto" poster={demoMode ? config.fixedReferenceUrl : undefined} /></div>
                <div className="result-details">
                  <div className="result-title"><FilmSlate /><div><strong>{job.finalReady ? "最终成片 · ranran 音色" : "原版成片"}</strong><span>640 × 480</span></div></div>
                  <dl>
                    <div><dt>时长</dt><dd>{formatDuration(job.output?.duration || job.sourceDuration)}</dd></div>
                    <div><dt>文件大小</dt><dd>{formatBytes(job.output?.size)}</dd></div>
                    <div><dt>完成时间</dt><dd>{completionTime}</dd></div>
                    {totalElapsedMs != null && (
                      <div className="total-elapsed"><dt>任务总耗时</dt><dd>{formatElapsedMs(totalElapsedMs)}</dd></div>
                    )}
                  </dl>
                  {job.finalReady && <a className="result-button primary" href={`/api/jobs/${job.id}/media/final?download=1`}><DownloadSimple /> 下载最终成片</a>}
                  {job.originalReady && <a className="result-button" href={`/api/jobs/${job.id}/media/original`} target="_blank" rel="noreferrer"><Eye /> 查看原版</a>}
                  {job.status === "failed" && job.originalReady && !job.finalReady && <button className="result-button" onClick={retryVoice} disabled={isBusy}><ArrowsClockwise /> 重新音色转换</button>}
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
                )) : <div className="empty-log"><Info /> 运行时会显示当前 ComfyUI 节点、RVC 阶段和错误详情。</div>}
                {job?.errorDetail && <pre>{job.errorDetail}</pre>}
              </div>
            )}
          </section>
        </section>
      </main>

      {lightboxImage && (
        <div className="lightbox" role="dialog" aria-modal="true" aria-label="参考图大图预览" onClick={() => setLightboxImage(null)}>
          <button className="lightbox-close" onClick={() => setLightboxImage(null)} aria-label="关闭预览"><X weight="bold" /></button>
          <img src={lightboxImage} alt="人物参考图大图" onClick={(event) => event.stopPropagation()} />
          <p className="lightbox-hint" onClick={(event) => event.stopPropagation()}>人物参考图 · 点击空白处或按 Esc 关闭</p>
        </div>
      )}
    </div>
  );
}

export function App() {
  const path = window.location.pathname.replace(/\/+$/, "");
  const isDouyinRoute = path === "/douyin";
  const isMigrateRoute = path === "/migrate";
  const isUpscaleRoute = path === "/upscale";

  return (
    <div className="desktop-app-shell">
      <TaskTabStatus />
      <aside className="app-sidebar">
        <div className="sidebar-top">
          <p className="sidebar-kicker">DESKTOP</p>
          <a className="sidebar-brand" href="/" aria-label="H3 MotionStudio 首页">
            <span className="brand-mark">H3</span>
            <strong>MotionStudio</strong>
          </a>
        </div>

        <nav className="sidebar-navigation" aria-label="工作台导航">
          <p className="sidebar-section-label">工作台</p>
          <div className="route-switcher">
            <a className={isDouyinRoute ? "active" : ""} href="/douyin">
              <DownloadSimple weight={isDouyinRoute ? "fill" : "regular"} />
              <span>抖音下载</span>
            </a>
            <a className={!isDouyinRoute ? "active" : ""} href="/">
              <FilmSlate weight={!isDouyinRoute ? "fill" : "regular"} />
              <span>影动生成</span>
            </a>
          </div>
          <p className="route-caption">下载 · 生成 · 迁移 · 放大</p>

          {isDouyinRoute ? (
            <>
              <p className="sidebar-section-label">创作与管理</p>
              <a className="sidebar-nav-item active" href="/douyin">
                <DownloadSimple />
                <span>链接下载</span>
                <i />
              </a>
            </>
          ) : (
            <>
              <p className="sidebar-section-label">创作与管理</p>
              <a className={!isMigrateRoute && !isUpscaleRoute ? "sidebar-nav-item active" : "sidebar-nav-item"} href="/">
                <MusicNotes weight="fill" />
                <span>歌曲生成</span>
                {!isMigrateRoute && !isUpscaleRoute && <i />}
              </a>
              <a className={isMigrateRoute ? "sidebar-nav-item active" : "sidebar-nav-item"} href="/migrate">
                <PersonSimpleRun />
                <span>动作迁移</span>
                {isMigrateRoute && <i />}
              </a>
              <a className={isUpscaleRoute ? "sidebar-nav-item active" : "sidebar-nav-item"} href="/upscale">
                <MagnifyingGlassPlus />
                <span>二采放大</span>
                {isUpscaleRoute && <i />}
              </a>
            </>
          )}
        </nav>

        <div className="sidebar-system">
          <p className="sidebar-section-label">系统</p>
          <SystemMonitor />
          <div className="sidebar-runtime">
            <span className="runtime-icon"><Circle weight="fill" /></span>
            <div><strong>本地服务</strong><small>按需运行</small></div>
          </div>
          <footer>H3 MOTIONSTUDIO <span>V0.2.0</span></footer>
        </div>
      </aside>

      <div className="route-stage">
        {isDouyinRoute ? <DouyinRoute /> : isMigrateRoute ? <MigrateRoute /> : isUpscaleRoute ? <UpScaleRoute /> : <MotionStudioRoute />}
      </div>
    </div>
  );
}
