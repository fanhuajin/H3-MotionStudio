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
  PersonSimpleRun,
  Play,
  SpinnerGap,
  Timer,
  UploadSimple,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { elapsedMs, formatElapsedMs, useNowTick } from "./jobTime";
import { QueuePanel } from "./QueuePanel";
import type { AppConfig, JobState, Milestone, MilestoneStatus } from "./types";

const FALLBACK_CONFIG: AppConfig = {
  comfyuiConnected: false,
  fixedReferenceUrl: "/assets/fixed-reference.png",
  defaultAction: "",
  defaultCamera: "",
  maxDurationSeconds: 40,
  environmentReady: false,
  missingRequirements: [],
};

type CanvasRatio = "4:3" | "9:16";
type MigrateMode = "animation" | "replacement";

const DRAFT_STORAGE_KEY = "h3-motionstudio:migrate-draft:v1";
const DRAFT_DB_NAME = "h3-motionstudio-migrate-draft";
const DRAFT_DB_VERSION = 1;
const DRAFT_FILE_STORE = "files";

interface MigrateDraft {
  ratio?: CanvasRatio;
  mode?: MigrateMode;
  removeSubtitles?: boolean;
  hd1080?: boolean;
  contentPrompt?: string;
  videoPrompt?: string;
  imagePrompt?: string;
  videoName?: string | null;
  videoSize?: number | null;
  imageName?: string | null;
  imageSize?: number | null;
  savedAt?: string;
}

function readDraft(): MigrateDraft | null {
  try {
    const raw = window.localStorage.getItem(DRAFT_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as MigrateDraft;
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function writeDraft(changes: Partial<MigrateDraft>) {
  try {
    const current = readDraft() || {};
    window.localStorage.setItem(
      DRAFT_STORAGE_KEY,
      JSON.stringify({ ...current, ...changes, savedAt: new Date().toISOString() }),
    );
  } catch {
    // Best effort persistence only.
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

async function saveDraftFile(kind: "video" | "reference", file: File) {
  try {
    const db = await openDraftDb();
    await new Promise<void>((resolve, reject) => {
      const request = db.transaction(DRAFT_FILE_STORE, "readwrite").objectStore(DRAFT_FILE_STORE).put(file, kind);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error || new Error("草稿文件保存失败"));
    });
    db.close();
  } catch {
    // Best effort; server-side inputs also survive restarts.
  }
}

async function loadDraftFile(kind: "video" | "reference"): Promise<File | null> {
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

async function clearDraftFile(kind: "video" | "reference") {
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

function ratioLabel(ratio: CanvasRatio) {
  return ratio === "9:16" ? "9:16 · 竖版（抖音）" : "4:3 · 横版";
}

function ratioNote(ratio: CanvasRatio, mode: MigrateMode, hd1080: boolean) {
  const canvas = ratio === "9:16" ? "512×896" : "512×384";
  const hd = ratio === "9:16" ? "1080×1920" : "1440×1080";
  return `${canvas} 加速草稿${hd1080 ? ` → ${hd} 高清成片` : ""} · ${mode === "animation" ? "动作迁移" : "人物替换"}`;
}

function migrateMilestoneSkeleton(removeSubtitles: boolean, mode: MigrateMode, hd1080: boolean, ratio: CanvasRatio): Milestone[] {
  const transfer = mode === "replacement" ? "人物替换" : "动作迁移";
  const multiplier = ratio === "9:16" ? "2" : "4";
  const list: Milestone[] = [];
  if (removeSubtitles) {
    list.push(
      { id: "read", label: "读取视频与音频", subtitle: "加载带字幕视频", status: "pending" },
      { id: "mask", label: "定位底部字幕区域", subtitle: "固定底部字幕遮罩", status: "pending" },
      { id: "paint", label: "ProPainter 时序去字幕", subtitle: "按前后帧修复字幕区域", status: "pending" },
      { id: "clean_save", label: "输出无字幕视频", subtitle: "保留原音频与帧率", status: "pending" },
    );
  }
  list.push(
    { id: "prep", label: "加载 SCAIL 模型与人物参考", subtitle: "读取驱动视频并准备参考人物", status: "pending" },
    { id: "sam", label: "SAM3 人物追踪与遮罩", subtitle: "定位视频与参考图中的人物", status: "pending" },
    {
      id: "migrate",
      label: `长视频分段${transfer}`,
      subtitle:
        mode === "replacement"
          ? "把视频中的人物替换成参考人物，保留场景与音频"
          : "让参考人物按视频动作表演，保留人物形象与音频",
      status: "pending",
    },
    { id: "save", label: "拼接输出成片", subtitle: "逐段衔接并封装输出视频", status: "pending" },
  );
  if (hd1080) {
    list.push(
      {
        id: "upscale",
        label: `RealESRGAN ${multiplier}× 放大`,
        subtitle: ratio === "9:16" ? "2× 快速档（竖版目标放大仅 2.1×）" : "逐帧超采样到所选比例高清",
        status: "pending",
      },
      { id: "hd", label: "输出高清成片", subtitle: "保存高清加强成片", status: "pending" },
    );
  }
  return list;
}

function ToggleRow({
  checked,
  onChange,
  title,
  description,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  title: string;
  description: string;
}) {
  return (
    <div className="toggle-row">
      <div className="toggle-copy">
        <strong>{title}</strong>
        <span>{description}</span>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        className={`toggle-switch ${checked ? "on" : ""}`}
        onClick={() => onChange(!checked)}
      >
        <span />
      </button>
    </div>
  );
}

export function MigrateRoute() {
  const videoInputRef = useRef<HTMLInputElement>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const previewTokenRef = useRef(0);

  const [config, setConfig] = useState<AppConfig>(FALLBACK_CONFIG);
  const [file, setFile] = useState<File | null>(null);
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [imagePreviewUrl, setImagePreviewUrl] = useState<string | null>(null);
  const [previewPreparing, setPreviewPreparing] = useState(false);
  const [previewConverting, setPreviewConverting] = useState(false);
  const [duration, setDuration] = useState<number | null>(null);

  const draft = useMemo(() => readDraft(), []);
  const [ratio, setRatio] = useState<CanvasRatio>(draft?.ratio || "9:16");
  const [mode, setMode] = useState<MigrateMode>(draft?.mode || "animation");
  const [removeSubtitles, setRemoveSubtitles] = useState<boolean>(draft?.removeSubtitles ?? false);
  const [hd1080, setHd1080] = useState<boolean>(draft?.hd1080 ?? false);
  const [contentPrompt, setContentPrompt] = useState(draft?.contentPrompt ?? "a person singing");
  const [videoPrompt, setVideoPrompt] = useState(draft?.videoPrompt ?? "person");
  const [imagePrompt, setImagePrompt] = useState(draft?.imagePrompt ?? "person");

  const [job, setJob] = useState<JobState | null>(null);
  const [logsOpen, setLogsOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [lightboxImage, setLightboxImage] = useState<string | null>(null);

  useEffect(() => {
    if (!lightboxImage) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setLightboxImage(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightboxImage]);

  // 运行/排队中的任务显示其真实里程碑；否则按当前表单设置实时预览
  // （切换 4:3/9:16、模式、去字幕、高清档位时流程文案立即同步）
  const liveJob = job && (job.status === "queued" || job.status === "running") ? job : null;
  const milestones = liveJob?.milestones?.length
    ? liveJob.milestones
    : migrateMilestoneSkeleton(removeSubtitles, mode, hd1080, ratio);
  const isBusy = submitting || job?.status === "queued" || job?.status === "running";
  const jobActive = Boolean(liveJob);
  const tickNow = useNowTick(jobActive);
  // 计时起点：开始执行时间（旧任务没有则退回创建时间）；运行中实时、结束后定格
  const totalElapsedMs = elapsedMs(job?.startedAt || job?.createdAt, job?.finishedAt, tickNow);
  const [queueOpen, setQueueOpen] = useState(false);

  const resourceStatus = useMemo(() => {
    if (job?.status === "running") return { label: "ComfyUI 单链路运行中", mode: "connected" };
    if (config.comfyuiConnected) return { label: "ComfyUI 已连接", mode: "connected" };
    return { label: "资源空闲，按需启动", mode: "idle" };
  }, [config.comfyuiConnected, job?.status]);

  useEffect(() => {
    writeDraft({
      ratio,
      mode,
      removeSubtitles,
      hd1080,
      contentPrompt,
      videoPrompt,
      imagePrompt,
      videoName: file?.name ?? null,
      videoSize: file?.size ?? null,
      imageName: imageFile?.name ?? null,
      imageSize: imageFile?.size ?? null,
    });
  }, [ratio, mode, removeSubtitles, hd1080, contentPrompt, videoPrompt, imagePrompt, file, imageFile]);

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
    socketRef.current?.close();
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
    void saveDraftFile("reference", nextFile);
    writeDraft({ imageName: nextFile.name, imageSize: nextFile.size });
    setLocalError(null);
  }, []);

  const chooseVideo = useCallback(async (nextFile: File | null) => {
    if (!nextFile) return;
    const allowed = [".mp4", ".mov", ".mkv", ".webm"];
    const suffix = nextFile.name.slice(nextFile.name.lastIndexOf(".")).toLowerCase();
    if (!nextFile.type.startsWith("video/") && !allowed.includes(suffix)) {
      setLocalError("请选择 MP4、MOV、MKV 或 WebM 视频文件。");
      return;
    }
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

    // 参考图恢复：最近任务上传过图就从后端恢复；否则（无任务/上一单未传图/
    // 用户新选未提交）回退本地草稿，避免切换页面或刷新后图片丢失。
    const restoredImage = latest?.referenceUploaded
      ? (await restoreRemote("reference", latest?.referenceName || "reference.png", "image/png"))
        || await loadDraftFile("reference")
      : await loadDraftFile("reference");
    if (restoredImage) chooseImage(restoredImage);
    const restoredVideo =
      (await restoreRemote("video", latest?.sourceName || "migrate-video.mp4", "video/mp4"))
      || await loadDraftFile("video");
    if (restoredVideo && latest?.id) {
      setFile(restoredVideo);
      setDuration(latest.sourceDuration ?? null);
      setPreviewPreparing(false);
      setPreviewConverting(false);
      setPreviewUrl(`/api/jobs/${latest.id}/input/video/preview`);
      void saveDraftFile("video", restoredVideo);
      writeDraft({ videoName: restoredVideo.name, videoSize: restoredVideo.size });
    } else if (restoredVideo) {
      await chooseVideo(restoredVideo);
    }
  }, [chooseImage, chooseVideo]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetch("/api/config", { cache: "no-store" }).then((response) => response.ok ? response.json() : Promise.reject()),
      fetch("/api/jobs/latest?kind=migrate", { cache: "no-store" })
        .then((response) => response.status === 204 ? null : response.json()),
    ]).then(([nextConfig, latest]) => {
      if (cancelled) return;
      setConfig(nextConfig);
      const source = latest ?? readDraft();
      if (source) {
        if (latest) {
          setRatio((latest.canvas as CanvasRatio) || "9:16");
          setMode((latest.migrateMode as MigrateMode) || "animation");
          setRemoveSubtitles(Boolean(latest.removeSubtitles));
          setHd1080(Boolean(latest.hd1080));
          setContentPrompt(latest.contentPrompt ?? "a person singing");
          setVideoPrompt(latest.videoPrompt ?? "person");
          setImagePrompt(latest.imagePrompt ?? "person");
        } else if ("ratio" in source) {
          const draftSource = source as MigrateDraft;
          setRatio(draftSource.ratio || "9:16");
          setMode(draftSource.mode || "animation");
          setRemoveSubtitles(draftSource.removeSubtitles ?? false);
          setHd1080(draftSource.hd1080 ?? false);
          setContentPrompt(draftSource.contentPrompt ?? "a person singing");
          setVideoPrompt(draftSource.videoPrompt ?? "person");
          setImagePrompt(draftSource.imagePrompt ?? "person");
        }
      }
      if (latest?.id) {
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
  }, [connectJob, restoreInputs]);

  const clearVideo = () => {
    previewTokenRef.current += 1;
    releaseObjectUrl(previewUrl);
    setFile(null);
    setPreviewUrl(null);
    setDuration(null);
    setPreviewPreparing(false);
    setPreviewConverting(false);
    void clearDraftFile("video");
    writeDraft({ videoName: null, videoSize: null });
    if (videoInputRef.current) videoInputRef.current.value = "";
  };

  const clearImage = () => {
    releaseObjectUrl(imagePreviewUrl);
    setImageFile(null);
    setImagePreviewUrl(null);
    void clearDraftFile("reference");
    writeDraft({ imageName: null, imageSize: null });
    if (imageInputRef.current) imageInputRef.current.value = "";
  };

  const submit = async () => {
    if (!file) {
      setLocalError("请先上传一个包含动作的视频。");
      videoInputRef.current?.click();
      return;
    }
    if (!config.environmentReady) {
      setLocalError("本地环境缺少文件：" + (config.missingRequirements.join("、") || "未知"));
      return;
    }
    setSubmitting(true);
    setLocalError(null);
    const form = new FormData();
    form.append("video", file);
    if (imageFile) form.append("reference_image", imageFile);
    form.append("ratio", ratio);
    form.append("remove_subtitles", removeSubtitles ? "1" : "0");
    form.append("mode", mode);
    form.append("hd1080", hd1080 ? "1" : "0");
    form.append("content_prompt", contentPrompt);
    form.append("video_prompt", videoPrompt);
    form.append("image_prompt", imagePrompt);
    try {
      const response = await fetch("/api/jobs/migrate", { method: "POST", body: form });
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

  const finalUrl = job?.finalReady ? `/api/jobs/${job.id}/media/final` : null;
  const draftUrl = job?.draftReady ? `/api/jobs/${job.id}/media/draft` : null;
  const cleanUrl = job?.cleanReady ? `/api/jobs/${job.id}/media/clean` : null;
  const originalUrl = job?.id ? `/api/jobs/${job.id}/input/video/preview` : null;
  const showResult = job && (job.finalReady || job.draftReady || job.cleanReady);
  const showDraftSeparately = Boolean(job?.draftReady && job?.finalReady && job?.enhancedReady);
  const resultCaption = job?.finalReady
    ? job.enhancedReady ? "高清成片已完成" : "迁移成片已完成"
    : job?.draftReady ? "迁移草稿已生成" : "中间产物已保留";

  return (
    <div className="migrate-route">
      <header className="route-hero">
        <div>
          <p className="route-eyebrow"><span /> H3 · ACTION MIGRATION</p>
          <h1>让参考人物，<em>动起来。</em></h1>
          <p className="route-description">上传一段动作视频与人物参考图，按所选画布把动作迁移/替换到参考人物身上，可选去字幕与 1080P 高清输出。</p>
        </div>
        <span className={`connection ${resourceStatus.mode}`}><span className="connection-dot" />{resourceStatus.label}</span>
      </header>

      <main className="workspace">
        <section className="input-panel" aria-label="动作迁移设置">
          <div className="field-block">
            <div className="field-heading"><h2><span>1.</span> 画布比例 <em>（先选择，全链路按此输出）</em></h2></div>
            <div className="ratio-cards" role="radiogroup" aria-label="画布比例">
              <button type="button" role="radio" aria-checked={ratio === "9:16"} className={ratio === "9:16" ? "selected" : ""} onClick={() => setRatio("9:16")}>
                <strong>9:16 · 竖版</strong><span>抖音发布常用 · 画布 512×896</span>
              </button>
              <button type="button" role="radio" aria-checked={ratio === "4:3"} className={ratio === "4:3" ? "selected" : ""} onClick={() => setRatio("4:3")}>
                <strong>4:3 · 横版</strong><span>现有成片比例 · 画布 512×384</span>
              </button>
            </div>
          </div>

          <div className="field-block">
            <div className="field-heading"><h2><span>2.</span> 上传动作视频与人物参考图</h2></div>
            <div className="source-grid">
              <div className="source-card image-source">
                <div className="source-label">人物参考图 · 可选</div>
                <input ref={imageInputRef} className="visually-hidden" type="file" accept="image/png,image/jpeg,image/webp,.jpg,.jpeg" onChange={(event) => chooseImage(event.target.files?.[0] || null)} />
                {imagePreviewUrl ? (
                  <div className="image-preview-wrap">
                    <button className="image-preview-button" onClick={() => setLightboxImage(imagePreviewUrl)} aria-label="放大预览人物参考图">
                      <img src={imagePreviewUrl} alt="人物参考图预览" />
                      <span><Graph weight="fill" /> 点击放大</span>
                    </button>
                    <div className="image-tools">
                      <button className="image-swap" onClick={() => imageInputRef.current?.click()} aria-label="更换人物参考图片">
                        <ArrowsClockwise weight="bold" /> 更换
                      </button>
                      <button className="image-remove" onClick={clearImage} aria-label="移除人物参考图" title="移除">
                        <X weight="bold" />
                      </button>
                    </div>
                  </div>
                ) : (
                  <button className="image-empty" onClick={() => imageInputRef.current?.click()}>
                    <UploadSimple /><strong>上传图片</strong><span>PNG / JPG / WebP · 建议 9:16 竖版</span>
                  </button>
                )}
                {!imagePreviewUrl && <p className="fallback-note"><Info /> 未上传时使用内置默认人物图</p>}
              </div>
              <div className="source-card video-source">
                <div className="source-label">动作视频 · 含音频</div>
                <input ref={videoInputRef} className="visually-hidden" type="file" accept="video/mp4,video/quicktime,video/x-matroska,video/webm,.mkv" onChange={(event) => chooseVideo(event.target.files?.[0] || null)} />
                <div className="media-input">
                  {previewUrl ? (
                    <>
                      <video src={previewUrl} controls preload="metadata" onLoadedMetadata={(event) => setDuration(event.currentTarget.duration)} />
                      <div className="media-meta">
                        <div><strong>{file?.name}</strong><span>{formatDuration(duration)} · {formatBytes(file?.size)}</span></div>
                        <button onClick={clearVideo} aria-label="移除视频"><X /></button>
                      </div>
                    </>
                  ) : previewPreparing ? (
                    <div className="upload-busy">
                      <SpinnerGap className="spin" weight="bold" />
                      <strong>{previewConverting ? "正在转为可播放预览…" : "正在准备预览…"}</strong>
                      <span>H.265 / HEVC 视频将自动转码为 H.264<br />耗时约几秒，不影响后续生成</span>
                    </div>
                  ) : (
                    <button className="empty-upload" onClick={() => videoInputRef.current?.click()}>
                      <span className="upload-icon"><UploadSimple /></span>
                      <strong>点击选择动作视频</strong>
                      <span>支持 MP4 / MOV / MKV，长短视频均可</span>
                    </button>
                  )}
                </div>
              </div>
            </div>
            <p className="field-note">视频只提供动作与场景、音频会保留；人物由参考图决定。素材会按所选比例统一缩放/居中裁切到画布。</p>
          </div>

          <div className="field-block">
            <div className="field-heading"><h2><span>3.</span> 迁移模式</h2></div>
            <div className="mode-cards" role="radiogroup" aria-label="迁移模式">
              <button type="button" role="radio" aria-checked={mode === "animation"} className={mode === "animation" ? "selected" : ""} onClick={() => setMode("animation")}>
                <span className="mode-icon"><PersonSimpleRun weight="fill" /></span>
                <strong>动作迁移</strong>
                <span>让参考人物按视频里的动作表演，保留人物自身形象、服装与风格</span>
              </button>
              <button type="button" role="radio" aria-checked={mode === "replacement"} className={mode === "replacement" ? "selected" : ""} onClick={() => setMode("replacement")}>
                <span className="mode-icon"><FilmSlate weight="fill" /></span>
                <strong>人物替换</strong>
                <span>把视频中的人物直接替换成参考人物，贴合原视频位置与构图</span>
              </button>
            </div>
          </div>

          <div className="field-block text-field">
            <div className="field-heading"><h2><span>4.</span> 提示词 <em>（留空则使用工作流默认）</em></h2></div>
            <div className="prompt-grid">
              <label className="prompt-field">
                <span>内容 / 画面描述</span>
                <textarea value={contentPrompt} maxLength={2000} onChange={(event) => setContentPrompt(event.target.value)} placeholder="例：一位女孩在唱歌…（默认 a person singing）" />
              </label>
              <label className="prompt-field">
                <span>视频里的人物</span>
                <textarea value={videoPrompt} maxLength={200} onChange={(event) => setVideoPrompt(event.target.value)} placeholder="SAM3 用于追踪视频中的人（默认 person）" />
              </label>
              <label className="prompt-field">
                <span>图片里的人物</span>
                <textarea value={imagePrompt} maxLength={200} onChange={(event) => setImagePrompt(event.target.value)} placeholder="SAM3 用于追踪参考图的人（默认 person）" />
              </label>
            </div>
          </div>

          <div className="field-block">
            <div className="field-heading"><h2><span>5.</span> 处理选项</h2></div>
            <div className="option-stack">
              <ToggleRow
                checked={removeSubtitles}
                onChange={setRemoveSubtitles}
                title="去除底部字幕"
                description="视频底部有字幕/水印条时开启：先用 ProPainter 固定底部修复，再把干净视频用于迁移"
              />
              <ToggleRow
                checked={hd1080}
                onChange={setHd1080}
                title="1080P 高清加强"
                description={`迁移完成后追加 RealESRGAN ${ratio === "9:16" ? "2×（快速档，目标仅2.1×放大）" : "4×"}：${ratio === "9:16" ? "1080×1920 竖版" : "1440×1080"}，耗时会增加`}
              />
            </div>
            <p className="field-note">当前设置：{ratioNote(ratio, mode, hd1080)}{removeSubtitles ? " · 先去除字幕" : ""}。输出保留原视频音频与帧率。</p>
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
            {isBusy ? "正在执行单链路任务" : `开始${ratio === "9:16" ? "竖版" : "横版"}${removeSubtitles ? "去字幕+" : ""}${mode === "replacement" ? "人物替换" : "动作迁移"}`}
          </button>
        </section>

        <section className="execution-panel" aria-label="执行进度与结果">
          <div className="panel-heading">
            <Graph />
            <div>
              <h2>执行流程</h2>
              <span>{liveJob ? "严格单链路 · 节点级运行状态" : "按当前设置预览 · 提交后展示实际进度"}</span>
            </div>
            {job && (
              <span className="queue-anchor">
                <button className={`job-id job-id-button ${queueOpen ? "job-id-open" : ""}`} onClick={() => setQueueOpen((value) => !value)} title="任务队列：查看当前任务并取消">
                  任务 {job.id.slice(0, 8)}
                </button>
                <QueuePanel open={queueOpen} onClose={() => setQueueOpen(false)} />
              </span>
            )}
            {liveJob?.estimatedSegments != null && liveJob?.stage === "migrating" && (
              <span
                className={`job-segments ${liveJob.currentSegment ? "live" : ""}`}
                title={`长视频分段进度：共 ${liveJob.estimatedSegments} 段（每段 81 帧、段间重叠 5 帧衔接）${liveJob.sourceFps ? ` · 源视频 ${liveJob.sourceFps}fps` : ""}`}
              >
                {liveJob.currentSegment
                  ? <>分段 {liveJob.currentSegment}/{liveJob.estimatedSegments}<i><b style={{ width: `${Math.min(100, (liveJob.currentSegment / liveJob.estimatedSegments) * 100)}%` }} /></i></>
                  : <>预计 {liveJob.estimatedSegments} 段</>}
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
              </div>
            ))}
          </div>

          {showResult && (
            <section className="result-panel">
              <div className="result-heading"><h3>生成结果</h3><span><Check weight="bold" /> {resultCaption}</span></div>
              <div className="result-grid">
                <div className="result-video">
                  {finalUrl || draftUrl || cleanUrl ? (
                    <video src={`${finalUrl || draftUrl || cleanUrl}#t=0.001`} controls preload="auto" />
                  ) : <div className="result-empty"><Info /> 成片文件暂不可用</div>}
                </div>
                <div className="result-details">
                  <div className="result-title">
                    <FilmSlate />
                    <div>
                      <strong>{job.finalReady ? (job.enhancedReady ? "高清成片" : "迁移成片") : job.draftReady ? "迁移草稿" : "中间产物"}</strong>
                      <span>{job.output?.width && job.output?.height ? `${job.output.width} × ${job.output.height}` : ratioLabel(ratio)}</span>
                    </div>
                  </div>
                  <dl>
                    <div><dt>时长</dt><dd>{formatDuration(job.output?.duration || job.sourceDuration)}</dd></div>
                    <div><dt>文件大小</dt><dd>{formatBytes(job.output?.size)}</dd></div>
                    <div><dt>完成时间</dt><dd>{completionTime}</dd></div>
                    {totalElapsedMs != null && (
                      <div className="total-elapsed"><dt>任务总耗时</dt><dd>{formatElapsedMs(totalElapsedMs)}</dd></div>
                    )}
                  </dl>
                  {finalUrl && <a className="result-button primary" href={`${finalUrl}?download=1`}><DownloadSimple /> 下载成片</a>}
                  {showDraftSeparately && draftUrl && <a className="result-button" href={draftUrl} target="_blank" rel="noreferrer"><Eye /> 查看迁移草稿</a>}
                  {cleanUrl && <a className="result-button" href={cleanUrl} target="_blank" rel="noreferrer"><Eye /> 查看去字幕视频</a>}
                  {originalUrl && <a className="result-button" href={originalUrl} target="_blank" rel="noreferrer"><Eye /> 查看原始视频</a>}
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
