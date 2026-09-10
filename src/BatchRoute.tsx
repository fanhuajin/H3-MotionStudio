import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowClockwise,
  Check,
  Circle,
  Copy,
  FolderOpen,
  ImageSquare,
  ListChecks,
  MusicNotes,
  Pause,
  PersonSimpleRun,
  Play,
  SpinnerGap,
  Trash,
  UploadSimple,
  WarningCircle,
  X,
} from "@phosphor-icons/react";

type StepStatus = "pending" | "running" | "completed" | "error" | "skipped";

interface BatchStep {
  id: string;
  label: string;
  subtitle?: string;
  status: StepStatus;
  progress?: number;
  currentNode?: string | null;
}

interface BatchAI {
  reference_image_path: string;
  song_name?: string;
  song_mood?: string;
  style_source?: "video" | "redesign";
  title: string;
  introduction: string;
  tags: string[];
  imagePrompt?: string;
  sceneFramePath?: string;
}

interface ChildJob {
  id: string;
  kind?: string;
  status: string;
  progress?: number;
  currentNodeTitle?: string;
  milestones?: BatchStep[];
  logs?: Array<{ time: string; message: string }>;
  currentSegment?: number;
  estimatedSegments?: number;
  upscaleBatch?: number;
  upscaleBatches?: number;
  cleanBatch?: number;
  cleanBatches?: number;
}

interface BatchItem {
  id: string;
  index: number;
  kind: "singing" | "dance";
  url: string;
  title: string;
  status: string;
  stage: string;
  revision?: number;
  milestones: BatchStep[];
  ai?: BatchAI;
  childJob?: ChildJob | null;
  logs?: Array<{ time: string; message: string }>;
  outputs?: Record<string, string>;
  error?: string | null;
  warning?: string | null;
}

interface BatchState {
  id: string;
  status: string;
  total: number;
  completedCount: number;
  deletedCount?: number;
  currentItemId?: string | null;
  notice?: string;
  items: BatchItem[];
}

const INPUT_KEY = "h3-motionstudio:batch-input:v1";

function readInputDraft() {
  try {
    const parsed = JSON.parse(localStorage.getItem(INPUT_KEY) || "{}") as Record<string, string>;
    return { singing: parsed.singing || "", dance: parsed.dance || "" };
  } catch {
    return { singing: "", dance: "" };
  }
}

function splitUrls(value: string) {
  return value.split(/[\r\n]+/).map((url) => url.trim()).filter(Boolean);
}

function batchStatusLabel(status: string) {
  return ({
    queued: "等待开始",
    running: "正在处理",
    revising: "正在调整",
    awaiting_review: "等待确认",
    confirmed: "已确认",
    paused: "已暂停",
    failed: "需要重试",
    completed: "已完成",
    skipped: "已跳过",
  } as Record<string, string>)[status] || status;
}

function stepIcon(status: StepStatus) {
  if (status === "completed") return <Check weight="bold" />;
  if (status === "running") return <SpinnerGap className="spin" />;
  if (status === "error") return <WarningCircle weight="fill" />;
  if (status === "skipped") return <X />;
  return <Circle />;
}

function responseMessage(response: Response, fallback: string) {
  return response.json().then((payload) => String(payload?.detail || fallback)).catch(() => fallback);
}

export function BatchRoute() {
  const initial = useMemo(readInputDraft, []);
  const [singing, setSinging] = useState(initial.singing);
  const [dance, setDance] = useState(initial.dance);
  const [batch, setBatch] = useState<BatchState | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [imageToken, setImageToken] = useState(0);

  const visibleItems = useMemo(() => batch?.items.filter((item) => item.status !== "deleted") || [], [batch]);
  const selected = useMemo(
    () => visibleItems.find((item) => item.id === selectedId)
      || visibleItems.find((item) => item.id === batch?.currentItemId)
      || visibleItems[0],
    [visibleItems, selectedId, batch?.currentItemId],
  );

  const loadLatest = useCallback(async () => {
    const response = await fetch("/api/batches/latest", { cache: "no-store" });
    if (response.status === 204) return;
    if (!response.ok) throw new Error(await responseMessage(response, "无法读取上次批次"));
    setBatch(await response.json());
  }, []);

  useEffect(() => {
    loadLatest().catch((reason) => setError(String(reason.message || reason)));
  }, [loadLatest]);

  useEffect(() => {
    localStorage.setItem(INPUT_KEY, JSON.stringify({ singing, dance }));
  }, [singing, dance]);

  useEffect(() => {
    if (!batch?.id) return;
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${location.host}/api/batches/${batch.id}/ws`);
    socket.onmessage = (event) => setBatch(JSON.parse(event.data));
    socket.onerror = () => undefined;
    const poll = window.setInterval(() => {
      fetch(`/api/batches/${batch.id}`, { cache: "no-store" })
        .then((response) => response.ok ? response.json() : null)
        .then((state) => state && setBatch(state))
        .catch(() => undefined);
    }, 3500);
    return () => {
      socket.close();
      window.clearInterval(poll);
    };
  }, [batch?.id]);

  useEffect(() => {
    if (!batch?.currentItemId) return;
    const currentSelection = batch.items.find((item) => item.id === selectedId);
    if (!currentSelection || ["completed", "skipped", "deleted"].includes(currentSelection.status)) {
      setSelectedId(batch.currentItemId);
    }
  }, [batch?.currentItemId, batch?.items, selectedId]);

  const start = async () => {
    setBusyAction("start");
    setError("");
    try {
      const response = await fetch("/api/batches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ singingUrls: splitUrls(singing), danceUrls: splitUrls(dance) }),
      });
      if (!response.ok) throw new Error(await responseMessage(response, "批次创建失败"));
      const state = await response.json();
      setBatch(state);
      setSelectedId(state.currentItemId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction("");
    }
  };

  const call = async (action: string, method = "POST", body?: object) => {
    if (!batch) return;
    setBusyAction(action);
    setError("");
    try {
      const response = await fetch(`/api/batches/${batch.id}/${action}`, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!response.ok) throw new Error(await responseMessage(response, "操作失败"));
      setBatch(await response.json());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction("");
    }
  };

  const itemCall = async (action: string, method = "POST", body?: object) => {
    if (!selected || !batch) return;
    await call(`items/${selected.id}${action ? `/${action}` : ""}`, method, body);
  };

  const openFolder = () => itemCall("open-output");
  const hasImage = Boolean(selected?.ai?.reference_image_path);

  const uploadImage = async (file: File) => {
    if (!batch || !selected) return;
    setBusyAction("upload");
    setError("");
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch(`/api/batches/${batch.id}/items/${selected.id}/image`, {
        method: "POST",
        body,
      });
      if (!response.ok) throw new Error(await responseMessage(response, "图片上传失败"));
      setBatch(await response.json());
      setImageToken(Date.now());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction("");
    }
  };

  const canStart = splitUrls(singing).length + splitUrls(dance).length > 0;
  const hasLiveBatch = batch && !["completed", "cancelled"].includes(batch.status);
  const effectiveTotal = Math.max(0, (batch?.total || 0) - (batch?.deletedCount || 0));

  return (
    <main className="batch-page">
      <header className="batch-header">
        <div>
          <p className="batch-kicker">SERIAL PRODUCTION</p>
          <h1>批量视频制作</h1>
          <p>粘贴链接即可开始。系统逐条准备候选图和发布文案，只有你确认后才生成该条视频。</p>
        </div>
        {batch && (
          <div className={`batch-status-pill ${batch.status}`}>
            {batch.status === "running" && <SpinnerGap className="spin" />}
            {batchStatusLabel(batch.status)} · {batch.completedCount}/{effectiveTotal}
          </div>
        )}
      </header>

      <section className="batch-input-card">
        <div className="batch-input-grid">
          <label>
            <span><MusicNotes weight="fill" /> 歌曲视频链接</span>
            <textarea value={singing} onChange={(event) => setSinging(event.target.value)} placeholder="每行粘贴一条抖音链接&#10;https://v.douyin.com/……" />
            <small>{splitUrls(singing).length} 条 · 生成无字幕版和歌词字幕版</small>
          </label>
          <label>
            <span><PersonSimpleRun weight="fill" /> 跳舞视频链接</span>
            <textarea value={dance} onChange={(event) => setDance(event.target.value)} placeholder="每行粘贴一条抖音链接&#10;https://v.douyin.com/……" />
            <small>{splitUrls(dance).length} 条 · 9:16 动作迁移成片</small>
          </label>
        </div>
        <div className="batch-input-actions">
          <p><ListChecks /> 页面会记住这些链接；不需要导入表格。</p>
          <button className="batch-primary" disabled={!canStart || Boolean(hasLiveBatch) || busyAction === "start"} onClick={start}>
            {busyAction === "start" ? <SpinnerGap className="spin" /> : <Play weight="fill" />}
            {hasLiveBatch ? "当前批次尚未结束" : "开始批量处理"}
          </button>
        </div>
      </section>

      {error && <div className="batch-alert"><WarningCircle weight="fill" />{error}</div>}

      {batch && (
        <section className="batch-workspace">
          <aside className="batch-queue">
            <div className="batch-section-head">
              <div><span>制作队列</span><small>{batch.notice}</small></div>
              <div className="batch-head-actions">
                {batch.status === "paused" ? (
                  <button onClick={() => call("resume")} disabled={Boolean(busyAction)}><Play />继续</button>
                ) : !["completed", "awaiting_review", "cancelled"].includes(batch.status) ? (
                  <button onClick={() => call("pause")} disabled={Boolean(busyAction)}><Pause />暂停</button>
                ) : null}
                {!["completed", "cancelled"].includes(batch.status) && (
                  <button
                    className="danger"
                    disabled={Boolean(busyAction)}
                    onClick={() => {
                      if (window.confirm("取消整批制作？未完成的条目会被标记为已跳过，已生成的候选结果会保留。")) {
                        void call("cancel");
                      }
                    }}
                  >
                    <X />取消整批
                  </button>
                )}
              </div>
            </div>
            <div className="batch-item-list">
              {visibleItems.map((item) => (
                <button key={item.id} className={`batch-item ${selected?.id === item.id ? "selected" : ""}`} onClick={() => setSelectedId(item.id)}>
                  <span className={`batch-item-index ${item.status}`}>{item.status === "completed" ? <Check /> : item.index}</span>
                  <span className="batch-item-copy">
                    <strong>{item.title || `第 ${item.index} 条`}</strong>
                    <small>{item.kind === "singing" ? "歌曲视频" : "跳舞视频"} · {batchStatusLabel(item.status)}</small>
                  </span>
                  {item.status === "running" && <SpinnerGap className="spin" />}
                </button>
              ))}
              {!visibleItems.length && <p className="batch-empty">队列里还没有任务。</p>}
            </div>
          </aside>

          <div className="batch-detail">
            {selected ? (
              <>
                <div className="batch-detail-head">
                  <div>
                    <p>第 {selected.index} 条 · {selected.kind === "singing" ? "歌曲视频" : "跳舞视频"}</p>
                    <h2>{selected.title}</h2>
                    <a href={selected.url} target="_blank" rel="noreferrer">查看原抖音链接</a>
                  </div>
                  <div className="batch-item-actions">
                    {selected.status === "failed" && <button onClick={() => itemCall("retry")}><ArrowClockwise />重试</button>}
                    {!['completed', 'skipped'].includes(selected.status) && <button onClick={() => itemCall("skip")}><X />跳过</button>}
                    <button className="danger" onClick={() => itemCall("", "DELETE")}><Trash />删除</button>
                  </div>
                </div>

                {selected.status === "awaiting_review" && selected.ai && (
                  <section className="batch-review">
                    <div className="batch-review-image">
                      <div className="batch-review-label"><ImageSquare /> 候选人物图 · 第 {(selected.revision || 0) + 1} 版</div>
                      {hasImage ? (
                        <img
                          src={`/api/batches/${batch.id}/items/${selected.id}/image?v=${imageToken || selected.revision || 0}`}
                          alt="待确认的人物图"
                        />
                      ) : (
                        <label
                          className={`batch-dropzone ${dragging ? "over" : ""}`}
                          onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
                          onDragLeave={() => setDragging(false)}
                          onDrop={(event) => {
                            event.preventDefault();
                            setDragging(false);
                            const file = event.dataTransfer.files?.[0];
                            if (file) void uploadImage(file);
                          }}
                        >
                          <UploadSimple weight="bold" />
                          <strong>{busyAction === "upload" ? "正在上传…" : "把 GPT 生成的图拖到这里"}</strong>
                          <small>或点击选择文件 · PNG / JPG / WEBP · 单张 25MB 以内</small>
                          <input
                            type="file"
                            accept="image/png,image/jpeg,image/webp"
                            onChange={(event) => {
                              const file = event.target.files?.[0];
                              if (file) void uploadImage(file);
                              event.target.value = "";
                            }}
                          />
                        </label>
                      )}
                      {hasImage && (
                        <label className="batch-replace">
                          <UploadSimple /> 换一张
                          <input
                            type="file"
                            accept="image/png,image/jpeg,image/webp"
                            onChange={(event) => {
                              const file = event.target.files?.[0];
                              if (file) void uploadImage(file);
                              event.target.value = "";
                            }}
                          />
                        </label>
                      )}
                    </div>
                    <div className="batch-review-copy">
                      <div className="batch-review-title">
                        <span>等待你的确认</span>
                        <small>确认前不会启动 ComfyUI</small>
                      </div>

                      {selected.ai.song_name && <p className="batch-song-name">识别歌曲：{selected.ai.song_name}</p>}
                      <label><span>标题</span><p>{selected.ai.title}</p></label>
                      <label><span>简介</span><p>{selected.ai.introduction}</p></label>
                      <label><span>标签</span><div className="batch-tags">{selected.ai.tags.map((tag) => <i key={tag}>#{tag.replace(/^#/, "")}</i>)}</div></label>
                      <div className="batch-review-actions">
                        <button
                          className="batch-primary"
                          disabled={Boolean(busyAction) || !hasImage}
                          onClick={() => itemCall("confirm")}
                          title={hasImage ? undefined : "请先添加上这一条的候选人物图"}
                        >
                          <Check weight="bold" />确认并开始生成视频
                        </button>
                      </div>
                    </div>
                  </section>
                )}

                <section className="batch-progress-panel">
                  <div className="batch-panel-title"><span>当前条目进度</span><small>{batchStatusLabel(selected.status)}</small></div>
                  <div className="batch-steps">
                    {selected.milestones.map((step) => (
                      <div className={`batch-step ${step.status}`} key={step.id}>
                        <span className="batch-step-icon">{stepIcon(step.status)}</span>
                        <div><strong>{step.label}</strong><small>{step.currentNode || step.subtitle}</small></div>
                        {step.status === "running" && (typeof step.progress === "number"
                          ? <em>{Math.round(step.progress)}%</em>
                          : <em className="indeterminate" title="该步骤没有节点级进度，按实际耗时显示">进行中</em>)}
                      </div>
                    ))}
                  </div>
                </section>

                {selected.childJob && (
                  <section className="batch-child-panel">
                    <div className="batch-panel-title">
                      <span>真实生成流程</span>
                      <small>{selected.childJob.currentNodeTitle || batchStatusLabel(selected.childJob.status)}</small>
                    </div>
                    <div className="batch-child-badges">
                      {selected.childJob.currentSegment && <i>H3 分段 {selected.childJob.currentSegment}/{selected.childJob.estimatedSegments}</i>}
                      {selected.childJob.cleanBatch && <i>去字幕 {selected.childJob.cleanBatch}/{selected.childJob.cleanBatches}</i>}
                      {selected.childJob.upscaleBatch && <i>二采 {selected.childJob.upscaleBatch}/{selected.childJob.upscaleBatches}</i>}
                    </div>
                    <div className="batch-steps compact">
                      {(selected.childJob.milestones || []).map((step) => (
                        <div className={`batch-step ${step.status}`} key={step.id}>
                          <span className="batch-step-icon">{stepIcon(step.status)}</span>
                          <div><strong>{step.label}</strong><small>{step.subtitle}</small></div>
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                {selected.status === "completed" && selected.outputs && (
                  <section className="batch-output-panel">
                    <div className="batch-panel-title"><span>发布文件已整理</span><small>{selected.warning || "文案、封面和成片均已保存"}</small></div>
                    <div className="batch-output-grid">
                      {selected.outputs.videoWithLyrics && <a href={`/api/batches/${batch.id}/items/${selected.id}/output/videoWithLyrics`} target="_blank">有字幕成片</a>}
                      {selected.outputs.videoNoLyrics && <a href={`/api/batches/${batch.id}/items/${selected.id}/output/videoNoLyrics`} target="_blank">无字幕成片</a>}
                      {selected.outputs.videoFinal && <a href={`/api/batches/${batch.id}/items/${selected.id}/output/videoFinal`} target="_blank">最终成片</a>}
                      <a href={`/api/batches/${batch.id}/items/${selected.id}/output/copy?download=true`}><Copy />发布文案</a>
                      <a href={`/api/batches/${batch.id}/items/${selected.id}/output/coverBilibili`} target="_blank">B站 4:3 封面</a>
                      <a href={`/api/batches/${batch.id}/items/${selected.id}/output/coverDouyin`} target="_blank">抖音 3:4 封面</a>
                    </div>
                    <button className="batch-primary" onClick={openFolder}><FolderOpen />打开文件夹</button>
                  </section>
                )}

                {(selected.error || selected.warning) && <div className="batch-alert"><WarningCircle weight="fill" />{selected.error || selected.warning}</div>}
              </>
            ) : <div className="batch-empty-detail"><ListChecks /><p>填写链接并开始后，审核和真实进度会显示在这里。</p></div>}
          </div>
        </section>
      )}
    </main>
  );
}
