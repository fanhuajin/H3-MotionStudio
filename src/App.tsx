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
  GearSix,
  Graph,
  Info,
  Play,
  Question,
  SpinnerGap,
  UploadSimple,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import type { AppConfig, JobState, Milestone, MilestoneStatus } from "./types";

const EMPTY_MILESTONES: Milestone[] = [
  { id: "input", label: "读取视频与音频", subtitle: "加载输入视频，分离音频轨道", status: "pending" },
  { id: "h3", label: "H3 分段生成", subtitle: "按时长生成连续唱歌片段", status: "pending" },
  { id: "stitch", label: "防闪拼接", subtitle: "平滑衔接并裁切到输入时长", status: "pending" },
  { id: "upscale", label: "RealESRGAN 4× 放大", subtitle: "逐帧超采样并缩放到 1080P", status: "pending" },
  { id: "hd", label: "输出 1440 × 1080", subtitle: "保存高清加强成片", status: "pending" },
  { id: "handoff", label: "关闭 ComfyUI", subtitle: "释放内存和显存，切换到 RVC", status: "pending" },
  { id: "stems", label: "分离人声与伴奏", subtitle: "Demucs 提取演唱人声", status: "pending" },
  { id: "voice", label: "转换为我的音色", subtitle: "RVC 模型执行音色转换", status: "pending" },
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

const DEMO_MILESTONES: Milestone[] = EMPTY_MILESTONES.map((step, index) => ({
  ...step,
  status: "completed",
  progress: 100,
  elapsed: ["00:05", "01:20", "00:15", "01:45", "00:08", "00:04", "00:38", "01:12", "00:06"][index],
}));

const DEMO_JOB: JobState = {
  id: "demo-complete-2026",
  status: "completed",
  stage: "completed",
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
  sourceName: "演唱视频.mp4",
  sourceSize: 120_400_000,
  sourceDuration: 204,
  referenceName: "人物参考图.png",
  referenceSize: 4_800_000,
  actionPrompt: "主角自然深情地演唱，眼神专注，偶尔闭眼沉浸；副歌时情绪增强，微微抬头，右手轻抬并随节奏摆动；整体动作自然流畅。",
  cameraPrompt: "以稳定的推轨为主，开场中景缓慢推进至近景；副歌时轻微环绕 15°，保持主体居中；间奏切至侧面 3/4 角度，收尾回到正面特写。",
  milestones: DEMO_MILESTONES,
  logs: [
    { time: new Date().toISOString(), message: "ComfyUI 工作流已完成，显存已释放。" },
    { time: new Date().toISOString(), message: "RVC：我的音色转换完成。" },
    { time: new Date().toISOString(), message: "最终 MP4 已完成音频替换。" },
  ],
  originalReady: true,
  enhancedReady: true,
  finalReady: true,
  output: { width: 1440, height: 1080, duration: 204, size: 286_700_000, completedAt: new Date().toISOString() },
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

function PipelineRow({ step, index }: { step: Milestone; index: number }) {
  const progressText = step.progressMax
    ? `采样 ${step.progressValue ?? 0} / ${step.progressMax} · ${Math.round(step.progress ?? 0)}%`
    : step.currentNode || null;
  const isRvc = ["stems", "voice", "mux"].includes(step.id);

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
        <span className="elapsed">{step.elapsed || "--:--"}</span>
        <span className="status-chip">{statusLabel(step.status)}</span>
      </div>
    </div>
  );
}

export function App() {
  const demoMode = new URLSearchParams(window.location.search).get("demo") === "complete";
  const inputRef = useRef<HTMLInputElement>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const [config, setConfig] = useState<AppConfig>(FALLBACK_CONFIG);
  const [file, setFile] = useState<File | null>(null);
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [imagePreviewUrl, setImagePreviewUrl] = useState<string | null>(null);
  const [duration, setDuration] = useState<number | null>(null);
  const [actionPrompt, setActionPrompt] = useState("");
  const [cameraPrompt, setCameraPrompt] = useState("");
  const [job, setJob] = useState<JobState | null>(demoMode ? DEMO_JOB : null);
  const [logsOpen, setLogsOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);

  const milestones = job?.milestones?.length ? job.milestones : EMPTY_MILESTONES;
  const isBusy = submitting || job?.status === "queued" || job?.status === "running";
  const resultUrl = job?.finalReady ? `/api/jobs/${job.id}/media/final` : null;
  const originalUrl = job?.originalReady ? `/api/jobs/${job.id}/media/original` : null;

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
    let cancelled = false;
    Promise.all([
      fetch("/api/config").then((response) => response.ok ? response.json() : Promise.reject()),
      demoMode
        ? Promise.resolve(null)
        : fetch("/api/jobs/latest").then((response) => response.status === 204 ? null : response.json()),
    ]).then(([nextConfig, latest]) => {
      if (cancelled) return;
      setConfig(nextConfig);
      setActionPrompt(nextConfig.defaultAction || "");
      setCameraPrompt(nextConfig.defaultCamera || "");
      if (demoMode) {
        setJob(DEMO_JOB);
        setActionPrompt(DEMO_JOB.actionPrompt);
        setCameraPrompt(DEMO_JOB.cameraPrompt);
      } else if (latest?.id) {
        setJob(latest);
        if (["queued", "running"].includes(latest.status)) connectJob(latest.id);
      }
    }).catch(() => {
      if (!cancelled) setLocalError("本地服务尚未启动，启动后页面会自动连接任务系统。");
    });
    return () => {
      cancelled = true;
      socketRef.current?.close();
    };
  }, [connectJob, demoMode]);

  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      const response = await fetch(`/api/jobs/${job.id}`);
      if (response.ok) setJob(await response.json());
    }, 4000);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status]);

  useEffect(() => () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    if (imagePreviewUrl) URL.revokeObjectURL(imagePreviewUrl);
  }, [previewUrl, imagePreviewUrl]);

  const chooseImage = useCallback((nextFile: File | null) => {
    if (!nextFile) return;
    const allowed = [".png", ".jpg", ".jpeg", ".webp"];
    const suffix = nextFile.name.slice(nextFile.name.lastIndexOf(".")).toLowerCase();
    if (!nextFile.type.startsWith("image/") && !allowed.includes(suffix)) {
      setLocalError("请选择 PNG、JPG、JPEG 或 WebP 人物图片。");
      return;
    }
    if (imagePreviewUrl) URL.revokeObjectURL(imagePreviewUrl);
    setImageFile(nextFile);
    setImagePreviewUrl(URL.createObjectURL(nextFile));
    setLocalError(null);
  }, [imagePreviewUrl]);

  const chooseFile = useCallback((nextFile: File | null) => {
    if (!nextFile) return;
    const allowed = [".mp4", ".mov", ".mkv", ".webm"];
    const suffix = nextFile.name.slice(nextFile.name.lastIndexOf(".")).toLowerCase();
    if (!nextFile.type.startsWith("video/") && !allowed.includes(suffix)) {
      setLocalError("请选择 MP4、MOV、MKV 或 WebM 视频文件。");
      return;
    }
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    setFile(nextFile);
    setPreviewUrl(URL.createObjectURL(nextFile));
    setDuration(null);
    setLocalError(null);
  }, [previewUrl]);

  const clearFile = () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    setFile(null);
    setPreviewUrl(null);
    setDuration(null);
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
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">H3</span>
          <strong>H3 影动高清工作台</strong>
          <span className="brand-divider" />
          <span>本地 ComfyUI 视频创作工具</span>
        </div>
        <div className="topbar-actions">
          <span className={`connection ${resourceStatus.mode}`}><span className="connection-dot" />{resourceStatus.label}</span>
          <button className="icon-button" aria-label="设置"><GearSix /></button>
          <button className="icon-button" aria-label="帮助"><Question /></button>
        </div>
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
                  <button className="image-preview-button" onClick={() => imageInputRef.current?.click()} aria-label="更换人物参考图片">
                    <img src={imagePreviewUrl || config.fixedReferenceUrl} alt="人物参考图预览" />
                    <span>点击更换图片</span>
                  </button>
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
                    <button onClick={clearFile} aria-label="移除视频"><X /></button>
                  </div>
                </>
              ) : demoMode ? (
                <>
                  <img className="demo-source" src={config.fixedReferenceUrl} alt="演唱视频画面预览" />
                  <div className="media-meta demo-meta">
                    <div><strong>演唱视频.mp4</strong><span>00:03:24 · 120.4 MB</span></div>
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
            {job && <span className="job-id">任务 {job.id.slice(0, 8)}</span>}
          </div>

          <div className={`pipeline ${job?.finalReady ? "pipeline-compact" : ""}`}>
            {milestones.map((step, index) => <PipelineRow key={step.id} step={step} index={index} />)}
          </div>

          {(job?.originalReady || job?.finalReady) && (
            <section className="result-panel">
              <div className="result-heading"><h3>生成结果</h3><span><Check weight="bold" /> {job.finalReady ? "最终成片已完成" : "原版成片已保留"}</span></div>
              <div className="result-grid">
                <div className="result-video"><video src={resultUrl || originalUrl || undefined} controls preload="metadata" poster={config.fixedReferenceUrl} /></div>
                <div className="result-details">
                  <div className="result-title"><FilmSlate /><div><strong>{job.finalReady ? "最终成片 · 我的音色" : "原版成片"}</strong><span>{job.finalReady ? "1440 × 1080" : "640 × 480"}</span></div></div>
                  <dl>
                    <div><dt>时长</dt><dd>{formatDuration(job.output?.duration || job.sourceDuration)}</dd></div>
                    <div><dt>文件大小</dt><dd>{formatBytes(job.output?.size)}</dd></div>
                    <div><dt>完成时间</dt><dd>{completionTime}</dd></div>
                  </dl>
                  {job.finalReady && <a className="result-button primary" href={`/api/jobs/${job.id}/media/final?download=1`}><DownloadSimple /> 下载最终成片</a>}
                  {job.originalReady && <a className="result-button" href={`/api/jobs/${job.id}/media/original`} target="_blank" rel="noreferrer"><Eye /> 查看原版</a>}
                  {job.enhancedReady && <button className="result-button" onClick={retryVoice} disabled={isBusy}><ArrowsClockwise /> 重新音色转换</button>}
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
    </div>
  );
}
