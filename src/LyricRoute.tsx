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
  MagnifyingGlass,
  MusicNotes,
  Play,
  SpinnerGap,
  Subtitles,
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

type LyricCandidate = {
  id: number;
  name: string;
  artist: string;
  album?: string;
  lang?: string;
  langLabel?: string;
  hasZh?: boolean;
  lineCount?: number;
  preview?: string;
};

type LyricDetail = {
  lang?: string;
  langLabel?: string;
  hasZh?: boolean;
  lines: { time: number; orig: string; zh: string }[];
};

const SKELETON_MILESTONES: Milestone[] = [
  { id: "read", label: "读取视频与音频", subtitle: "加载视频并提取音轨", status: "pending" },
  { id: "stems", label: "Demucs 人声分离", subtitle: "提取演唱人声备用", status: "pending" },
  { id: "asr", label: "语音识别实测时间", subtitle: "自动语种 · 逐词时间戳", status: "pending" },
  { id: "align", label: "歌词逐句对齐", subtitle: "识别结果与歌词文本匹配", status: "pending" },
  { id: "render", label: "字幕烧录", subtitle: "剪映手书 · 白字细描边", status: "pending" },
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
  if (kind === "lyrics") return "歌词字幕";
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

function PipelineRow({ step, index, liveNow }: { step: Milestone; index: number; liveNow?: number | null }) {
  const progressText = step.progressMax
    ? `${step.progressValue ?? 0} / ${step.progressMax} · ${Math.round(step.progress ?? 0)}%`
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

export function LyricRoute() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const socketRef = useRef<WebSocket | null>(null);

  const [mode, setMode] = useState<"upload" | "recent">("upload");
  const [file, setFile] = useState<File | null>(null);
  const [recent, setRecent] = useState<RecentItem[]>([]);
  const [recentPick, setRecentPick] = useState<{ jobId: string; key: string; label: string } | null>(null);

  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [candidates, setCandidates] = useState<LyricCandidate[]>([]);
  const [picked, setPicked] = useState<LyricCandidate | null>(null);
  const [loadingLyric, setLoadingLyric] = useState(false);
  const [origText, setOrigText] = useState("");
  const [zhText, setZhText] = useState("");
  const [lyricNote, setLyricNote] = useState("");

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
      const jobs = (payload.jobs || []).filter((item) => item.media.some((entry) => entry.key === "final"));
      setRecent(jobs);
      setRecentPick((current) => {
        if (current && jobs.some((item) => item.id === current.jobId)) return current;
        const first = jobs[0]?.media.find((entry) => entry.key === "final");
        return first ? { jobId: jobs[0].id, key: "final", label: first.label } : null;
      });
    } catch {
      // 服务未就绪
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
      fetch("/api/jobs/latest?kind=lyrics", { cache: "no-store" })
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

  const doSearch = async () => {
    const q = query.trim();
    if (!q) {
      setLocalError("请输入歌名（可附带歌手，例如：Love Scenario iKON）。");
      return;
    }
    setSearching(true);
    setLocalError(null);
    setPicked(null);
    setCandidates([]);
    try {
      const response = await fetch(`/api/lyrics/search?q=${encodeURIComponent(q)}`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "搜索失败");
      setCandidates(payload.results || []);
      if (!payload.results?.length) setLyricNote("没有搜到结果，可直接在下方粘贴歌词文本。");
      else setLyricNote("");
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "搜索失败");
    } finally {
      setSearching(false);
    }
  };

  const pickCandidate = async (item: LyricCandidate) => {
    setPicked(item);
    setLoadingLyric(true);
    setLocalError(null);
    try {
      const response = await fetch(`/api/lyrics/lyric?song_id=${item.id}`, { cache: "no-store" });
      const payload = await response.json() as LyricDetail;
      if (!response.ok) throw new Error((payload as unknown as { detail?: string })?.detail || "歌词获取失败");
      const lines = payload.lines || [];
      setOrigText(lines.map((line) => line.orig).join("\n"));
      setZhText(lines.map((line) => line.zh || "").join("\n"));
      setLyricNote(
        `已载入 ${lines.length} 行歌词${payload.langLabel ? ` · ${payload.langLabel}` : ""}${payload.hasZh ? " · 含中文翻译（可编辑，删掉某行翻译则只显示原词）" : " · 无官方中文翻译，可在右列粘贴自己的翻译"}`,
      );
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "歌词获取失败");
    } finally {
      setLoadingLyric(false);
    }
  };

  const clearLyric = () => {
    setPicked(null);
    setCandidates([]);
    setQuery("");
    setOrigText("");
    setZhText("");
    setLyricNote("");
  };

  const buildLines = () => {
    const origLines = origText.split("\n").map((line) => line.trim());
    const zhLines = zhText.split("\n").map((line) => line.trim());
    return origLines
      .filter(Boolean)
      .map((orig, index) => ({ orig, zh: zhLines[index]?.trim() || "" }))
      .slice(0, 400);
  };

  const submit = async () => {
    if (mode === "upload" && !file) {
      setLocalError("请先选择要配歌词的视频。");
      fileInputRef.current?.click();
      return;
    }
    if (mode === "recent" && !recentPick) {
      setLocalError("暂无可用的历史成片。");
      return;
    }
    const lines = buildLines();
    if (lines.length === 0) {
      setLocalError("歌词为空：先搜索/粘贴歌词文本。");
      return;
    }
    setSubmitting(true);
    setLocalError(null);
    const form = new FormData();
    form.append("song_name", picked?.name ? `${picked.name}${picked.artist ? " - " + picked.artist : ""}` : "");
    form.append("lines_json", JSON.stringify(lines));
    if (mode === "upload" && file) form.append("video", file);
    if (mode === "recent" && recentPick) {
      form.append("source_job_id", recentPick.jobId);
      form.append("source_key", recentPick.key);
    }
    try {
      const response = await fetch("/api/jobs/lyrics", { method: "POST", body: form });
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

  const lineCount = buildLines().length;

  return (
    <div className="lyrics-route">
      <header className="route-hero">
        <div>
          <p className="route-eyebrow"><span /> H3 · LYRICS</p>
          <h1>歌词字幕，<em>多语言实测。</em></h1>
          <p className="route-description">选视频 → 自动抓官方歌词（韩/日/中/英）→ 人声实测逐句时间 → 剪映手书风格烧录，支持双语对照或单行。</p>
        </div>
        <span className={`connection ${job?.status === "running" ? "connected" : "idle"}`}>
          <span className="connection-dot" />
          {job?.status === "running" ? "歌词任务运行中" : "资源空闲"}
        </span>
      </header>

      <main className="workspace">
        <section className="input-panel" aria-label="歌词字幕设置">
          <div className="field-block">
            <div className="field-heading"><h2><span>1.</span> 输入视频</h2></div>
            <div className="ratio-cards" role="radiogroup" aria-label="输入来源">
              <button type="button" role="radio" aria-checked={mode === "upload"} className={mode === "upload" ? "selected" : ""} onClick={() => setMode("upload")}>
                <strong>上传本地视频</strong><span>MP4 / MOV / MKV / WebM</span>
              </button>
              <button type="button" role="radio" aria-checked={mode === "recent"} className={mode === "recent" ? "selected" : ""} onClick={() => setMode("recent")}>
                <strong>从最近任务选</strong><span>用歌曲/迁移/放大的成片</span>
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
                  <p className="queue-empty">暂无可配歌词的成片（先完成一单歌曲/迁移/放大任务）</p>
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
                        <span className="recent-choice-target"><Subtitles weight="bold" /> 给其{media?.label || "成片"}配歌词</span>
                      </button>
                    );
                  })
                )}
              </div>
            )}
            <p className="field-note">音频用于实测每句演唱时间；视频比例不限（4:3 / 9:16 / 16:9 均按画布自适应字号）。</p>
          </div>

          <div className="field-block">
            <div className="field-heading"><h2><span>2.</span> 歌词 <em>（自动抓取或直接粘贴）</em></h2></div>
            {!picked && (
              <div className="lyric-search-row">
                <input
                  className="text-input"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  onKeyDown={(event) => { if (event.key === "Enter") void doSearch(); }}
                  placeholder="歌名（可加歌手）如：Love Scenario iKON / 起风了 买辣椒也用券"
                />
                <button className="search-button" onClick={() => void doSearch()} disabled={searching}>
                  {searching ? <SpinnerGap className="spin" /> : <MagnifyingGlass weight="bold" />}
                  搜索歌词
                </button>
              </div>
            )}

            {!picked && candidates.length > 0 && (
              <div className="recent-pick lyric-candidates">
                {candidates.map((item) => (
                  <button type="button" className="recent-choice" key={item.id} onClick={() => void pickCandidate(item)}>
                    <span className="recent-choice-head">
                      <strong>{item.name} · {item.artist}</strong>
                      <em>{item.langLabel}{item.hasZh ? " · 含中文翻译" : ""}{item.album ? ` · ${item.album}` : ""}</em>
                    </span>
                    <span className="recent-choice-target"><MusicNotes weight="bold" /> {item.preview || `${item.lineCount ?? ""} 行歌词`}</span>
                  </button>
                ))}
              </div>
            )}

            {picked && (
              <div className="lyric-editor">
                <div className="lyric-editor-head">
                  <span><MusicNotes weight="fill" /> {picked.name} · {picked.artist}</span>
                  <button onClick={clearLyric}>换一首 / 重新搜索</button>
                </div>
                <div className="lyric-columns">
                  <label>
                    <span>原词（每行一句）{loadingLyric && <SpinnerGap className="spin" />}</span>
                    <textarea value={origText} onChange={(event) => setOrigText(event.target.value)} rows={12} placeholder="原语言歌词，每行一句……" />
                  </label>
                  <label>
                    <span>中文翻译（可整列清空 → 只显示原词）</span>
                    <textarea value={zhText} onChange={(event) => setZhText(event.target.value)} rows={12} placeholder="对应行的中文翻译……" />
                  </label>
                </div>
              </div>
            )}
            {lyricNote && <p className="field-note">{lyricNote}</p>}
          </div>

          {(localError || job?.errorSummary) && (
            <div className="error-banner" role="alert">
              <WarningCircle weight="fill" />
              <div><strong>任务需要处理</strong><span>{localError || job?.errorSummary}</span></div>
              {job?.errorDetail && <button onClick={() => setLogsOpen(true)}>查看详情</button>}
            </div>
          )}

          <button className="primary-action" onClick={submit} disabled={submitting || jobActive}>
            {submitting || jobActive ? <SpinnerGap className="spin" /> : <Subtitles weight="fill" />}
            {submitting ? "正在创建任务…" : jobActive ? "歌词任务运行中" : `开始生成歌词字幕（${lineCount} 行）`}
          </button>
        </section>

        <section className="execution-panel" aria-label="执行进度与结果">
          <div className="panel-heading">
            <Graph />
            <div>
              <h2>执行流程</h2>
              <span>{jobActive ? "识别与烧录节点级状态" : "提交后展示实际进度"}</span>
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
                <PipelineRow step={step} index={index} liveNow={jobActive ? tickNow : null} />
              </div>
            ))}
          </div>

          {job?.finalReady && (
            <section className="result-panel">
              <div className="result-heading"><h3>歌词成片</h3><span><Check weight="bold" /> 已生成</span></div>
              <div className="result-grid">
                <div className="result-video">
                  <video src={`/api/jobs/${job.id}/media/final#t=0.001`} controls preload="auto" />
                </div>
                <div className="result-details">
                  <div className="result-title">
                    <Subtitles />
                    <div>
                      <strong>{job.songName || job.sourceName}</strong>
                      <span>{job.output?.width && job.output?.height ? `${job.output.width} × ${job.output.height} · ${job.lyricLangLabel || ""}` : ""}</span>
                    </div>
                  </div>
                  <dl>
                    <div><dt>时长</dt><dd>{formatDuration(job.output?.duration || job.sourceDuration)}</dd></div>
                    <div><dt>文件大小</dt><dd>{formatBytes(job.output?.size)}</dd></div>
                    <div><dt>完成时间</dt><dd>{completionTime}</dd></div>
                    {totalElapsedMs != null && <div className="total-elapsed"><dt>任务总耗时</dt><dd>{formatElapsedMs(totalElapsedMs)}</dd></div>}
                  </dl>
                  <a className="result-button primary" href={`/api/jobs/${job.id}/media/final?download=1`}><DownloadSimple /> 下载歌词成片</a>
                  <a className="result-button" href={`/api/jobs/${job.id}/media/original`} target="_blank" rel="noreferrer"><Eye /> 查看原视频</a>
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
                )) : <div className="empty-log"><Info /> 运行时会显示 Demucs / 识别 / 对齐 / 烧录各阶段。</div>}
                {job?.errorDetail && <pre>{job.errorDetail}</pre>}
              </div>
            )}
          </section>
        </section>
      </main>
    </div>
  );
}
