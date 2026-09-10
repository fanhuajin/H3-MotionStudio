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
  Plus,
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
  /** 歌曲条目：直接进唱歌工作流节点 ③ 的「人物动作要求」与「运镜要求」 */
  action_prompt?: string;
  camera_prompt?: string;
  /** 跳舞条目：迁移工作流的三段提示词 + 是否先去字幕 */
  content_prompt?: string;
  video_prompt?: string;
  image_prompt?: string;
  remove_subtitles?: boolean;
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
  /** 本条视频的画布比例：歌曲默认 4:3、跳舞默认 9:16，可逐条改 */
  ratio?: CanvasRatio;
  title: string;
  status: string;
  stage: string;
  revision?: number;
  milestones: BatchStep[];
  ai?: BatchAI;
  childJob?: ChildJob | null;
  stageMedia?: Record<string, string>;
  sourcePath?: string;
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

type CanvasRatio = "4:3" | "9:16";

const INPUT_KEY = "h3-motionstudio:batch-input:v2";
const LEGACY_INPUT_KEY = "h3-motionstudio:batch-input:v1";
// 歌曲默认 4:3、跳舞默认 9:16：这是新建批次时的默认值，每条视频都能单独改。
const DEFAULT_RATIO: Record<BatchItem["kind"], CanvasRatio> = { singing: "4:3", dance: "9:16" };
const RATIOS: CanvasRatio[] = ["4:3", "9:16"];

const RATIO_LABEL: Record<CanvasRatio, string> = { "4:3": "4:3 横版", "9:16": "9:16 竖版" };

/** 各链路生成分辨率不同：唱歌 640×480 / 480×864，跳舞 512×384 / 512×896。 */
function ratioDetail(kind: BatchItem["kind"], ratio: CanvasRatio) {
  if (kind === "singing") return ratio === "4:3" ? "生成 640×480 · 二采 1440×1080" : "生成 480×864 · 二采 1080×1920";
  return ratio === "4:3" ? "生成 512×384 · 二采 1440×1080" : "生成 512×896 · 二采 1080×1920";
}

function itemRatio(item: BatchItem): CanvasRatio {
  return item.ratio === "4:3" || item.ratio === "9:16" ? item.ratio : DEFAULT_RATIO[item.kind];
}

interface StagedTask {
  kind: BatchItem["kind"];
  url: string;
}

function readInputDraft() {
  const read = (key: string) => {
    try {
      return JSON.parse(localStorage.getItem(key) || "{}") as Record<string, unknown>;
    } catch {
      return {} as Record<string, unknown>;
    }
  };
  const parsed = { ...read(LEGACY_INPUT_KEY), ...read(INPUT_KEY) };
  const staged = Array.isArray(parsed.staged) ? parsed.staged : [];
  return {
    singing: String(parsed.singing || ""),
    dance: String(parsed.dance || ""),
    // 开关：关掉的一类既不展示输入框，也不会被提交执行；默认两类都开着。
    singingOn: parsed.singingOn !== false,
    danceOn: parsed.danceOn !== false,
    // 「准备任务」阶段攒下来的待加入清单（只入队、不开跑）
    staged: staged
      .map((row) => row as Partial<StagedTask>)
      .filter((row) => (row.kind === "singing" || row.kind === "dance") && typeof row.url === "string" && row.url)
      .map((row) => ({ kind: row.kind as BatchItem["kind"], url: String(row.url) })),
  };
}

function splitUrls(value: string) {
  return value.split(/[\r\n]+/).map((url) => url.trim()).filter(Boolean);
}

function batchStatusLabel(status: string) {
  return ({
    queued: "等待开跑",
    pending: "等待开跑",
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
  const [singingOn, setSingingOn] = useState(initial.singingOn);
  const [danceOn, setDanceOn] = useState(initial.danceOn);
  // 「准备任务」阶段攒下来的清单：先核对、再一次性加入队列
  const [staged, setStaged] = useState<StagedTask[]>(initial.staged);
  const [batch, setBatch] = useState<BatchState | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [imageToken, setImageToken] = useState(0);
  // 先把上次的队列读回来再允许提交：否则刚打开页面就点「加入队列」会新开一个批次，
  // 看到的现象就是「我排好的队列不见了」。
  const [loaded, setLoaded] = useState(false);

  const visibleItems = useMemo(() => batch?.items.filter((item) => item.status !== "deleted") || [], [batch]);
  const selected = useMemo(
    () => visibleItems.find((item) => item.id === selectedId)
      || visibleItems.find((item) => item.id === batch?.currentItemId)
      || visibleItems[0],
    [visibleItems, selectedId, batch?.currentItemId],
  );

  const loadLatest = useCallback(async () => {
    try {
      const response = await fetch("/api/batches/latest", { cache: "no-store" });
      if (response.status === 204) return;
      if (!response.ok) throw new Error(await responseMessage(response, "无法读取上次批次"));
      setBatch(await response.json());
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    loadLatest().catch((reason) => setError(String(reason.message || reason)));
  }, [loadLatest]);

  useEffect(() => {
    localStorage.setItem(INPUT_KEY, JSON.stringify({ singing, dance, singingOn, danceOn, staged }));
  }, [singing, dance, singingOn, danceOn, staged]);

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

  // ① 准备任务：把当前粘贴的链接整理进「待加入清单」（只在本页，还没进队列）
  const stageTasks = () => {
    setError("");
    const picked: StagedTask[] = [
      ...(singingOn ? splitUrls(singing).map((url) => ({ kind: "singing" as const, url })) : []),
      ...(danceOn ? splitUrls(dance).map((url) => ({ kind: "dance" as const, url })) : []),
    ];
    if (!picked.length) {
      setError("请先打开要制作的那一类（歌曲 / 跳舞）并填写链接");
      return;
    }
    setStaged((current) => {
      const seen = new Set(current.map((row) => `${row.kind}:${row.url}`));
      const next = [...current];
      for (const row of picked) {
        const key = `${row.kind}:${row.url}`;
        if (seen.has(key)) continue;
        seen.add(key);
        next.push(row);
      }
      return next;
    });
    // 已整理的链接从输入框移走，方便继续粘下一批（清单里还能逐条删）
    if (singingOn) setSinging("");
    if (danceOn) setDance("");
  };

  const unstage = (position: number) => {
    setStaged((current) => current.filter((_, index) => index !== position));
  };

  // ② 加入队列：把待加入清单交给后端排队，**不会开跑**（要用户点「开跑」）
  const enqueue = async () => {
    setBusyAction("start");
    setError("");
    try {
      const singingUrls = staged.filter((row) => row.kind === "singing").map((row) => row.url);
      const danceUrls = staged.filter((row) => row.kind === "dance").map((row) => row.url);
      if (!singingUrls.length && !danceUrls.length) {
        throw new Error("待加入清单是空的：先点「准备任务」把填好的链接整理进来");
      }
      // 已经有一个批次（不管在跑、暂停、等审核还是刚做完）就往里追加，随时能加；
      // 只有「已取消」的批次需要新开一个。
      const append = Boolean(batch && batch.status !== "cancelled");
      const response = await fetch(append ? `/api/batches/${batch!.id}/items` : "/api/batches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ singingUrls, danceUrls }),
      });
      if (!response.ok) throw new Error(await responseMessage(response, append ? "加入队列失败" : "创建队列失败"));
      const state = await response.json();
      setBatch(state);
      if (!append) setSelectedId(state.currentItemId);
      setStaged([]);
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
  // 画布比例只在审核时展示/修改（审核区唯一的比例入口）。
  const selectedRatio = selected ? itemRatio(selected) : DEFAULT_RATIO.singing;
  const changeRatio = (ratio: CanvasRatio) => {
    if (!selected || selectedRatio === ratio) return;
    void itemCall("ratio", "POST", { ratio });
  };

  // 只读回看每一阶段的产物；顺序按生成先后排列
  const STAGE_LABELS: Array<[string, string]> = [
    ["source", "源视频"],
    ["candidate", "候选人物图"],
    ["draft", "迁移草稿"],
    ["clean", "去字幕视频"],
    ["original", "原版成片"],
    ["enhanced", "二采高清"],
    ["final", "最终成片"],
    ["lyrics", "歌词字幕版"],
  ];
  const stageEntries = useMemo(() => {
    if (!selected) return [] as Array<[string, string]>;
    const media = selected.stageMedia || {};
    const available = new Set<string>(Object.keys(media));
    if (selected.sourcePath) available.add("source");
    if (selected.ai?.reference_image_path) available.add("candidate");
    return STAGE_LABELS.filter(([key]) => available.has(key));
  }, [selected]);

  // 本条实际会写进工作流的动作/运镜（歌唱）或迁移提示词（跳舞）：只读展示给用户核对。
  const promptBlocks = useMemo(() => {
    const ai = selected?.ai;
    if (!ai) return [] as Array<{ label: string; text: string }>;
    const blocks = selected!.kind === "singing"
      ? [
        { label: "人物动作要求", text: String(ai.action_prompt || "").trim() },
        { label: "运镜要求", text: String(ai.camera_prompt || "").trim() },
      ]
      : [
        { label: "内容提示词", text: String(ai.content_prompt || "").trim() },
        { label: "视频人物", text: String(ai.video_prompt || "").trim() },
        { label: "参考图人物", text: String(ai.image_prompt || "").trim() },
      ];
    return blocks.filter((block) => block.text);
  }, [selected]);

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

  const canStart = (singingOn && splitUrls(singing).length > 0) || (danceOn && splitUrls(dance).length > 0);
  // 已有批次（取消的除外）时「加入队列」是追加到队尾；两种都不会自动开跑。
  const canAppend = Boolean(batch && batch.status !== "cancelled");
  // 队列里有待处理条目、且没在跑也没暂停 → 显示「开跑」（整个队列的唯一启动开关）
  const canRunQueue = Boolean(
    batch
    && !["cancelled", "running", "paused"].includes(batch.status)
    && (batch.items || []).some((item) => ["pending", "revising", "confirmed"].includes(item.status)),
  );
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
        <div className="batch-prepare-head">
          <span>准备任务</span>
          <small>① 填好链接 →「准备任务」整理进待加入清单 → ②「加入队列」→ ③ 队列里点「开跑」才开始跑</small>
        </div>
        <div className={`batch-input-grid ${singingOn && danceOn ? "" : "single"}`}>
          <label className={singingOn ? "" : "off"}>
            <span className="batch-input-head">
              <b><MusicNotes weight="fill" /> 歌曲视频链接</b>
              <button
                type="button"
                role="switch"
                aria-checked={singingOn}
                className={`batch-switch ${singingOn ? "on" : ""}`}
                onClick={(event) => { event.preventDefault(); setSingingOn((value) => !value); }}
              >
                <i />{singingOn ? "已开启" : "已关闭"}
              </button>
            </span>
            {singingOn ? (
              <>
                <textarea value={singing} onChange={(event) => setSinging(event.target.value)} placeholder="每行粘贴一条抖音链接&#10;https://v.douyin.com/……" />
                <small>{splitUrls(singing).length} 条 · 默认 4:3 横版（审核时每条都能改）· 生成无字幕版和歌词字幕版</small>
              </>
            ) : (
              <small>已关闭：这类链接不会展示，也不会加入批次。</small>
            )}
          </label>
          <label className={danceOn ? "" : "off"}>
            <span className="batch-input-head">
              <b><PersonSimpleRun weight="fill" /> 跳舞视频链接</b>
              <button
                type="button"
                role="switch"
                aria-checked={danceOn}
                className={`batch-switch ${danceOn ? "on" : ""}`}
                onClick={(event) => { event.preventDefault(); setDanceOn((value) => !value); }}
              >
                <i />{danceOn ? "已开启" : "已关闭"}
              </button>
            </span>
            {danceOn ? (
              <>
                <textarea value={dance} onChange={(event) => setDance(event.target.value)} placeholder="每行粘贴一条抖音链接&#10;https://v.douyin.com/……" />
                <small>{splitUrls(dance).length} 条 · 默认 9:16 竖版（审核时每条都能改）· 动作迁移成片</small>
              </>
            ) : (
              <small>已关闭：这类链接不会展示，也不会加入批次。</small>
            )}
          </label>
        </div>
        <div className="batch-staged">
          <div className="batch-staged-head">
            <span>待加入清单</span>
            <small>{staged.length ? `${staged.length} 条 · 还没进队列，可逐条删除` : "还没有内容：上面填好链接后点「准备任务」"}</small>
          </div>
          {staged.length > 0 && (
            <ul className="batch-staged-list">
              {staged.map((row, position) => (
                <li key={`${row.kind}:${row.url}:${position}`}>
                  <i>{row.kind === "singing" ? "歌曲" : "跳舞"}</i>
                  <span title={row.url}>{row.url}</span>
                  <button type="button" onClick={() => unstage(position)} aria-label="从待加入清单里移除"><X weight="bold" /></button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="batch-input-actions">
          <p><ListChecks /> 重复链接会自动跳过；加入队列后不会自己跑，要等你在队列里点「开跑」。</p>
          <div className="batch-input-buttons">
            <button
              className="batch-secondary"
              disabled={!canStart || !loaded || Boolean(busyAction)}
              onClick={stageTasks}
              title="把上面填好的链接整理进待加入清单（先核对再入队）"
            >
              <ListChecks />准备任务（{splitUrls(singing).length + splitUrls(dance).length} 条）
            </button>
            <button
              className="batch-primary"
              disabled={!staged.length || !loaded || busyAction === "start"}
              onClick={enqueue}
              title={canAppend ? "追加到当前批次队尾（不会自动开跑）" : "新建队列（不会自动开跑）"}
            >
              {busyAction === "start" ? <SpinnerGap className="spin" /> : <Plus weight="bold" />}
              加入队列（{staged.length} 条）
            </button>
          </div>
        </div>
      </section>

      {error && <div className="batch-alert"><WarningCircle weight="fill" />{error}</div>}

      {batch && (
        <section className="batch-workspace">
          <aside className="batch-queue">
            <div className="batch-section-head">
              <div><span>制作队列</span><small>{batch.notice}</small></div>
              <div className="batch-head-actions">
                {canRunQueue ? (
                  <button className="start" onClick={() => call("start")} disabled={Boolean(busyAction)} title="按队列顺序开始处理（同时把 ComfyUI 拉起来预热）"><Play weight="fill" />开跑</button>
                ) : batch.status === "paused" ? (
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
                    <button
                      className="danger"
                      onClick={() => {
                        if (window.confirm(`删除第 ${selected.index} 条？正在跑的步骤会被安全取消，已生成的文件会保留。`)) {
                          void itemCall("", "DELETE");
                        }
                      }}
                    >
                      <Trash />删除这一条
                    </button>
                  </div>
                </div>

                {selected.status === "pending" && (
                  <p className="field-note">
                    这一条还在排队：回到队列上方点「开跑」才会按顺序开始（下载抖音视频 → 生成人物图素材与发布文案 → 停下来等你确认）。
                  </p>
                )}

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
                        <small>按 {selectedRatio} 出片 · 确认前不会启动 ComfyUI</small>
                      </div>

                      {selected.ai.song_name && <p className="batch-song-name">识别歌曲：{selected.ai.song_name}</p>}
                      <label><span>标题</span><p>{selected.ai.title}</p></label>
                      <label><span>简介</span><p>{selected.ai.introduction}</p></label>
                      <label><span>标签</span><div className="batch-tags">{selected.ai.tags.map((tag) => <i key={tag}>#{tag.replace(/^#/, "")}</i>)}</div></label>
                      <label>
                        <span>画布比例</span>
                        <div className="batch-ratio-pick" role="radiogroup" aria-label="这一条的画布比例">
                          {RATIOS.map((value) => (
                            <button
                              key={value}
                              type="button"
                              role="radio"
                              aria-checked={selectedRatio === value}
                              className={selectedRatio === value ? "selected" : ""}
                              disabled={Boolean(busyAction)}
                              onClick={() => changeRatio(value)}
                            >
                              {RATIO_LABEL[value]}
                            </button>
                          ))}
                          <i>{ratioDetail(selected.kind, selectedRatio)}</i>
                        </div>
                      </label>
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

                {selected.ai && (
                  <section className="batch-prompt-panel">
                    <div className="batch-panel-title">
                      <span>{selected.kind === "singing" ? "已填写的动作与运镜" : "已填写的迁移提示词"}</span>
                      <small>只读 · 确认出片时按原文提交给工作流</small>
                    </div>
                    <div className="batch-prompt-list">
                      {promptBlocks.map((block) => (
                        <article key={block.label}>
                          <h4>{block.label}</h4>
                          <pre>{block.text}</pre>
                        </article>
                      ))}
                      {selected.kind === "dance" && (
                        <article>
                          <h4>先去字幕</h4>
                          <pre>{selected.ai.remove_subtitles ? "是 · 出片前先跑一遍去字幕" : "否 · 直接用源视频驱动"}</pre>
                        </article>
                      )}
                      {promptBlocks.length === 0 && (
                        <p className="batch-empty">这一条还没有动作/运镜或迁移提示词（预审未完成或模型降级）。</p>
                      )}
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

                {stageEntries.length > 0 && (
                  <section className="batch-child-panel">
                    <div className="batch-panel-title">
                      <span>生成阶段产物</span>
                      <small>只读 · 点开即看，不影响任务</small>
                    </div>
                    <div className="batch-stage-list">
                      {stageEntries.map(([key, label]) => (
                        <a
                          key={key}
                          href={`/api/batches/${batch.id}/items/${selected.id}/stage/${key}`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {label}
                        </a>
                      ))}
                    </div>
                  </section>
                )}

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
                    <div className="batch-panel-title"><span>发布文件已整理</span><small>{selected.warning || "文案和成片均已保存"}</small></div>
                    <div className="batch-output-grid">
                      {selected.outputs.videoWithLyrics && <a href={`/api/batches/${batch.id}/items/${selected.id}/output/videoWithLyrics`} target="_blank">有字幕成片</a>}
                      {selected.outputs.videoNoLyrics && <a href={`/api/batches/${batch.id}/items/${selected.id}/output/videoNoLyrics`} target="_blank">无字幕成片</a>}
                      {selected.outputs.videoFinal && <a href={`/api/batches/${batch.id}/items/${selected.id}/output/videoFinal`} target="_blank">最终成片</a>}
                      <a href={`/api/batches/${batch.id}/items/${selected.id}/output/copy?download=true`}><Copy />发布文案</a>
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
