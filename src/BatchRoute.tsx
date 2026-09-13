import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { readJson } from "./api";
import { elapsedMs, formatElapsedMs, useNowTick } from "./jobTime";
import {
  ArrowClockwise,
  ArrowUUpLeft,
  Check,
  Circle,
  ImageSquare,
  ListChecks,
  MusicNotes,
  Pause,
  PersonSimpleRun,
  Play,
  SpinnerGap,
  Timer,
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
  progress?: number | null;
  currentNode?: string | null;
  startedAt?: string | null;
}

/**
 * 步骤已经跑了多久。跳舞（SCAIL 迁移）链路不会广播节点级进度，子任务的 progress 一路是
 * null —— 只有「进行中 + 已耗时」能证明它还在动（2026-09-14 用户：「批量跳舞视频没有进度吗」）。
 */
function elapsedLabel(startedAt?: string | null) {
  if (!startedAt) return "";
  const started = Date.parse(startedAt);
  if (Number.isNaN(started)) return "";
  const total = Math.max(0, Math.floor((Date.now() - started) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

/** 只有真实、非零、未完成的百分比才显示数字；0 与 null 都按「进度未知」处理。 */
function stepPercent(step: BatchStep) {
  return typeof step.progress === "number" && step.progress > 0 ? Math.round(step.progress) : null;
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
  /** 下载子任务 id（抖音下载服务里的任务号） */
  downloadJobId?: string | null;
  createdAt?: string;
  updatedAt?: string;
  finishedAt?: string | null;
  /** 源视频文件名（含抖音作品号） */
  sourceName?: string;
  /** 抖音作品号：链接写法不同（短链 / modal_id / 喜欢列表）时唯一能认人的标识 */
  awemeId?: string;
  /** 源作品自己的文案（`desc` 第一行就是用户在抖音上看到的那句话） */
  sourceMetadata?: { desc?: string; tags?: string[] } | null;
  videoJobId?: string | null;
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
  pauseRequested?: boolean;
  /** 时间戳（ISO）：用来算「已运行多久」 */
  createdAt?: string;
  startedAt?: string | null;
  finishedAt?: string | null;
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

/**
 * 这一条是不是**真的**在出片（有子任务在跑）。
 *
 * `confirmed` 只是「已放行、还没轮到它」：取消它不会动任何正在跑的东西，所以不能对它
 * 弹「已生成到一半的进度作废」（2026-09-13 用户：「我操作的是没有开始的任务，为什么
 * 回影响到正在生成的内容呢」——那句话对 `confirmed` 是错的，正在出片的有可能是别的条目）。
 */
function renderingNow(item: BatchItem): boolean {
  if (item.status === "running") return true;
  return ["queued", "running", "cancelling"].includes(String(item.childJob?.status || ""));
}

/**
 * 这一条对应的**源视频**自己的文案。
 *
 * 条目上显示的 `title` 是模型重新起的发布标题（例如「只对你心动的花季暗号」），用户根本
 * 认不出它是哪条抖音视频；源作品的原始文案才是他认识的那句话（2026-09-13 用户：
 * 「你可以在让我确认的时候让我知道现在的是哪个视频吗」）。
 */
function sourceCaption(item: BatchItem): string {
  const desc = String(item.sourceMetadata?.desc || "").split("\n")[0].trim();
  return desc || item.sourceName || "";
}

/** 日志时间：ISO（UTC）→ 本地时间；解析不了就原样显示。 */
function formatLogTime(value?: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

/** 造型来源：模型判定「源视频不适合出片」时会按歌曲情绪重做。 */
function styleSourceLabel(value?: string): string {
  return value === "redesign" ? "源视频不适合出片 → 按歌曲情绪重做造型" : "沿用源视频造型 / 服装 / 场景";
}

/**
 * 条目该显示的标题：**只认发布标题 `ai.title`**，没有才回退到 `item.title`。
 *
 * 用户 2026-09-13 实测「批量生成任务 4 为什么标题不一致」：左侧队列显示的是预审阶段写的
 * `item.title`，审核面板与发布文案用的是用户上传候选图后 `write_copy` 重写的 `ai.title`，
 * 两个字段各自更新就会出现两个标题。后端读取时已把 `item.title` 同步成 `ai.title`，
 * 这里再兜一层，保证同一屏永远不会出现两个不同的标题。
 */
function itemTitle(item: BatchItem): string {
  return String(item.ai?.title || "").trim() || item.title || `第 ${item.index} 条`;
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
  return {
    singing: String(parsed.singing || ""),
    dance: String(parsed.dance || ""),
    // 开关：关掉的一类既不展示输入框，也不会被提交执行；默认两类都开着。
    singingOn: parsed.singingOn !== false,
    danceOn: parsed.danceOn !== false,
  };
}

function splitUrls(value: string) {
  return value.split(/[\r\n]+/).map((url) => url.trim()).filter(Boolean);
}

function batchStatusLabel(status: string) {
  return ({
    queued: "等待开始",
    pending: "排队中",
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

function responseMessage(response: Response, fallback: string): Promise<string> {  // 交给统一的 readJson：后端 500 现在也是 JSON（{"detail": ...}），且绝不会把
  // "Unexpected token 'I'..." 这种解析错误当成给用户看的提示。
  return readJson<{ detail?: string }>(response, fallback)
    .then(() => fallback)
    .catch((reason) => (reason instanceof Error ? reason.message : fallback));
}

export function BatchRoute() {
  const initial = useMemo(readInputDraft, []);
  const [singing, setSinging] = useState(initial.singing);
  const [dance, setDance] = useState(initial.dance);
  const [singingOn, setSingingOn] = useState(initial.singingOn);
  const [danceOn, setDanceOn] = useState(initial.danceOn);
  const [batch, setBatch] = useState<BatchState | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  // 中性提示（比如「这次没有新增任务」）：不是错误，但必须让人看见
  const [notice, setNotice] = useState("");
  const [dragging, setDragging] = useState(false);
  const [imageToken, setImageToken] = useState(0);
  // 「替换源视频」：贴错链接 / 放错槽位（唱歌视频贴进跳舞口）时不用删了重加
  const [replacingSource, setReplacingSource] = useState(false);
  const [replaceUrl, setReplaceUrl] = useState("");
  const [replaceKind, setReplaceKind] = useState<"singing" | "dance">("singing");
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
      setBatch(await readJson<BatchState>(response, "无法读取上次批次"));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    loadLatest().catch((reason) => setError(String(reason.message || reason)));
  }, [loadLatest]);

  useEffect(() => {
    localStorage.setItem(INPUT_KEY, JSON.stringify({ singing, dance, singingOn, danceOn }));
  }, [singing, dance, singingOn, danceOn]);

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

  // 自动跟随「当前正在处理的条目」只发生在**用户没有自己选**的时候：
  // 用户 2026-09-13 实测「取消出片之后为什么点击不了了 一点就跳转到了其他的」——旧逻辑
  // 只要选中项的 status 是 completed/skipped 就强行跳回 currentItemId，于是刚取消出片
  // （→ skipped）的条目根本点不开，已完成的条目也看不了。`followedItemRef` 记住「上一次是
  // 自动选中的那一条」：只有还在跟随并且它确实不是当前条目时，才继续跟着走。
  const followedItemRef = useRef<string | null>(null);
  const selectItem = (itemId: string) => {
    followedItemRef.current = null;   // 用户自己点的，别再来抢
    setSelectedId(itemId);
  };

  useEffect(() => {
    if (!batch?.currentItemId) return;
    const target = batch.items.find((item) => item.id === selectedId);
    // 只有「选中的条目已经不存在/已删除」或者「本来就是自动跟随」时才自动跳；
    // 用户自己点开的条目（哪怕是 completed / skipped）一律留在原地。
    const unusable = !target || target.status === "deleted";
    const following = followedItemRef.current !== null && followedItemRef.current === selectedId;
    if (!unusable && !following) return;
    if (selectedId === batch.currentItemId) return;
    followedItemRef.current = batch.currentItemId;
    setSelectedId(batch.currentItemId);
  }, [batch?.currentItemId, batch?.items, selectedId]);

  // 换条目就收起「替换源视频」表单，免得把 A 条的链接写到 B 条上
  useEffect(() => {
    setReplacingSource(false);
    setReplaceUrl("");
  }, [selectedId]);

  // 「准备任务」：把填好的链接交给后端，并立即开始准备
  // （下载抖音视频 → 生成人物图与发布文案 → 停在等确认）。
  // 暂停中的批次尊重暂停，只入队不偷跑。
  const prepare = async () => {
    setBusyAction("start");
    setError("");
    // 报错信息要带上具体端点，所以在外层先声明（catch 里还要用）
    let target = "/api/batches";
    try {
      const singingUrls = singingOn ? splitUrls(singing) : [];
      const danceUrls = danceOn ? splitUrls(dance) : [];
      if (!singingUrls.length && !danceUrls.length) {
        throw new Error("请先打开要制作的那一类（歌曲 / 跳舞）并填写链接");
      }
      // 已经有一个批次（不管在跑、暂停、等审核还是刚做完）就往里追加，随时能加；
      // 只有「已取消」的批次需要新开一个。
      const append = Boolean(batch && batch.status !== "cancelled");
      const autoStart = !(batch && (batch.status === "paused" || batch.pauseRequested));
      target = append ? `/api/batches/${batch!.id}/items` : "/api/batches";
      const response = await fetch(target, {        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ singingUrls, danceUrls, autoStart }),
      });
      if (!response.ok) throw new Error(await responseMessage(response, append ? "加入队列失败" : "创建队列失败"));
      const state = await readJson<BatchState>(response, append ? "加入队列失败" : "创建队列失败");
      setBatch(state);
      if (!append) {
        followedItemRef.current = state.currentItemId ?? null;
        setSelectedId(state.currentItemId ?? null);
      }
      // 一条都没新增（全被判重过滤）时必须说清楚，否则点了看起来像没反应
      if (append && (state.items?.length || 0) <= (batch?.items.length || 0)) {
        setNotice("这些链接都已经在队列里了（重复链接自动跳过），这次没有新增任务。");
      } else {
        setNotice("");
      }
      // 输入框内容保留：重复链接后端会自动过滤（notice 里写明跳过了几条），
      // 想接着补链接或核对粘贴内容都不用重新粘一遍。
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(`${message}　〔POST ${target}〕`);
    } finally {
      setBusyAction("");
    }
  };

  const call = async (action: string, method = "POST", body?: object) => {
    if (!batch) return;
    const endpoint = `/api/batches/${batch.id}/${action}`;
    setBusyAction(action);
    setError("");
    try {
      const response = await fetch(endpoint, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!response.ok) throw new Error(await responseMessage(response, "操作失败"));
      setBatch(await readJson<BatchState>(response, "操作失败"));
    } catch (reason) {
      // 报错要把**所有能给的定位信息**都带上：后端 detail + HTTP 状态 + 具体端点，
      // 否则用户只能看到一句「操作失败」，没法反馈也没法自己排查。
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(`${message}　〔${method} ${endpoint}〕`);
    } finally {
      setBusyAction("");
    }
  };

  const itemCall = async (action: string, method = "POST", body?: object) => {
    if (!selected || !batch) return;
    await call(`items/${selected.id}${action ? `/${action}` : ""}`, method, body);
  };

  const hasImage = Boolean(selected?.ai?.reference_image_path);
  // 用户 2026-09-14：「未开始前的任务都允许修改」——没开始出片的条目都能改比例/去除字幕
  const settingsEditable = Boolean(
    selected?.ai && !["running", "revising", "completed", "deleted"].includes(selected.status),
  );
  // 「回到确认」：过了审核点的条目（含正在出片，会先安全取消）都能退回去重做
  const canReopen = Boolean(
    selected?.ai && !["awaiting_review", "deleted"].includes(selected.status),
  );
  // 「替换源视频」：没开始出片（pending / awaiting_review / confirmed / failed / skipped）都能换，
  // 和改比例同一条规则；正在出片或已经出片要先「取消出片」/「回到确认」。
  const canReplaceSource = Boolean(
    selected && !["running", "revising", "completed", "deleted"].includes(selected.status),
  );
  // 画布比例 / 去除字幕：没开始出片的条目都能改（审核区是主要入口，见 renderSettings）
  const selectedRatio = selected ? itemRatio(selected) : DEFAULT_RATIO.singing;
  // 出了审核点（已加入队列/出片中/已完成）以后，同一屏信息继续显示，但只读：
  // 用户 2026-09-13「等待你的确认 的信息在加入队列之后也要展示，只是不允许修改了」。
  const atReview = selected?.status === "awaiting_review";
  const changeRatio = (ratio: CanvasRatio) => {
    if (!selected || selectedRatio === ratio) return;
    void itemCall("ratio", "POST", { ratio });
  };
  const selectedSubtitles = Boolean(selected?.ai?.remove_subtitles);
  const changeSubtitles = (value: boolean) => {
    if (!selected || selectedSubtitles === value) return;
    void itemCall("remove-subtitles", "POST", { removeSubtitles: value });
  };

  /** 出片前的两个设置（画布比例 + 跳舞条目的去除字幕）：审核区与「出片前设置」面板共用。 */
  const renderSettings = (item: BatchItem) => {
    const ratio = itemRatio(item);
    const subtitles = Boolean(item.ai?.remove_subtitles);
    return (
      <>
        <label>
          <span>画布比例</span>
          <div className="batch-ratio-pick" role="radiogroup" aria-label="这一条的画布比例">
            {RATIOS.map((value) => (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={ratio === value}
                className={ratio === value ? "selected" : ""}
                disabled={Boolean(busyAction)}
                onClick={() => changeRatio(value)}
              >
                {RATIO_LABEL[value]}
              </button>
            ))}
            <i>{ratioDetail(item.kind, ratio)}</i>
          </div>
        </label>
        {item.kind === "dance" && (
          <label>
            <span>去除字幕</span>
            <div className="batch-ratio-pick" role="radiogroup" aria-label="出片前是否先去字幕">
              {[true, false].map((value) => (
                <button
                  key={String(value)}
                  type="button"
                  role="radio"
                  aria-checked={subtitles === value}
                  className={subtitles === value ? "selected" : ""}
                  disabled={Boolean(busyAction)}
                  onClick={() => changeSubtitles(value)}
                >
                  {value ? "先去字幕再迁移" : "不去字幕"}
                </button>
              ))}
              <i>{subtitles ? "出片前先跑一遍 ProPainter 去字幕" : "直接用源视频驱动，不去字幕"}</i>
            </div>
          </label>
        )}
      </>
    );
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

  // 「等待你的确认」那一屏要把后端掌握的**全部**信息摊开：识别歌曲/情绪、造型来源判断、
  // 作品号、源作品文案、候选图来源与版本、比例与放行状态、时间线与标识。
  // 用户 2026-09-13：「报所有可以展示的信息都展示出来」「是指等待你的确认里的信息」。
  // **不要条目日志**（同日追加：「信息展示 条目日志不要」）——日志留在后端/接口里排查用。
  const reviewFacts = useMemo(() => {
    if (!selected?.ai) return [] as Array<[string, string]>;
    const ai = selected.ai;
    const image = String(ai.reference_image_path || "");
    return [
      ["类型", selected.kind === "singing" ? "唱歌视频" : "跳舞视频"],
      ["状态", `${batchStatusLabel(selected.status)} · ${selected.stage}`],
      ["抖音作品号", String(selected.awemeId || "")],
      ["源作品文案", sourceCaption(selected)],
      ["源文件名", String(selected.sourceName || "")],
      ["识别歌曲", String(ai.song_name || "")],
      ["歌曲情绪", String(ai.song_mood || "")],
      ["造型来源", styleSourceLabel(ai.style_source)],
      ["候选人物图", image ? image.split(/[\\/]/).pop() || image : "还没有（等你上传 GPT 生成的图）"],
      ["候选图版本", `第 ${(selected.revision || 0) + 1} 版`],
      ["审核放行", selected.status === "awaiting_review" ? "还没放行" : "已放行"],
      ["条目 id", selected.id],
      ["下载子任务", String(selected.downloadJobId || "")],
      ["视频子任务", String(selected.videoJobId || "")],
      ["源文件路径", String(selected.sourcePath || "")],
      ["创建时间", formatLogTime(selected.createdAt)],
      ["最近更新", formatLogTime(selected.updatedAt)],
    ] as Array<[string, string]>;
  }, [selected]);

  // 已运行时间：批次还在跑就实时跳秒；已结束显示总耗时。
  const batchLive = Boolean(batch && !batch.finishedAt && !["completed", "cancelled", "failed"].includes(batch.status));
  // 队列里每一条也要有时间，所以只要还有条目在跑/已放行就继续跳秒（批次可能刚收尾）
  const queueLive = visibleItems.some(
    (item) => !item.finishedAt && ["running", "revising", "confirmed"].includes(item.status),
  );
  const batchNowTick = useNowTick(batchLive || queueLive);

  // 本条已运行时间：从条目创建算到结束（或此刻）。
  const itemElapsedMs = selected
    ? elapsedMs(selected.createdAt, selected.finishedAt, batchNowTick)
    : null;

  /**
   * 队列里那条的时间：排队中写「排队」（还没开始，别让人以为在跑）、
   * 正在跑写「已用」、结束写「耗时」。都从加入队列算起。
   */
  const queueTime = (item: BatchItem): string => {
    const ms = elapsedMs(item.createdAt, item.finishedAt, batchNowTick);
    if (ms === null) return "";
    const label = item.status === "pending"
      ? "排队"
      : item.finishedAt
        ? "耗时"
        : "已用";
    return `${label} ${formatElapsedMs(ms)}`;
  };

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
      setBatch(await readJson<BatchState>(response, "图片上传失败"));
      setImageToken(Date.now());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction("");
    }
  };

  const canStart = (singingOn && splitUrls(singing).length > 0) || (danceOn && splitUrls(dance).length > 0);
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
          <small>点「准备任务」后立即开始：下载抖音视频 → 生成人物图与发布文案 → 停下来等你确认；确认后点「加入队列」直接开跑 ComfyUI 出片</small>
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
                <small>{splitUrls(singing).length} 条 · 默认 4:3 横版（审核时每条都能改）· 交付最终成片（无字幕）+ 人物图 + 发布文案</small>
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
        <div className="batch-input-actions">
          <p><ListChecks /> 重复链接会自动跳过；备好料就停下来等你确认，点「加入队列」才真正出片。</p>
          <div className="batch-input-buttons">
            <button
              className="batch-primary"
              disabled={!canStart || !loaded || busyAction === "start"}
              onClick={prepare}
              title="加入队列并立即开始准备：下载抖音视频 → 生成人物图与发布文案 → 等你确认"
            >
              {busyAction === "start" ? <SpinnerGap className="spin" /> : <Play weight="fill" />}
              准备任务（{splitUrls(singing).length + splitUrls(dance).length} 条）
            </button>
          </div>
        </div>
      </section>

      {error && <div className="batch-alert"><WarningCircle weight="fill" />{error}</div>}
      {!error && notice && <div className="batch-alert info"><ListChecks weight="fill" />{notice}</div>}

      {batch && (
        <section className="batch-workspace">
          <aside className="batch-queue">
            <div className="batch-section-head">
              <div><span>制作队列</span><small>{batch.notice}</small></div>
              <div className="batch-head-actions">
                {/* 不再显示**批次总耗时**（2026-09-13 用户：「每一个队列里的任务都是独立的计算时间
                    我不需要看总时间」）：每条自己的时间在队列行里，选中条目在标题下有耗时。 */}
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
                <button key={item.id} className={`batch-item ${selected?.id === item.id ? "selected" : ""}`} onClick={() => selectItem(item.id)}>
                  <span className={`batch-item-index ${item.status}`}>{item.status === "completed" ? <Check /> : item.index}</span>
                  <span className="batch-item-copy">
                    <strong>{itemTitle(item)}</strong>
                    <small>
                      {item.kind === "singing" ? "歌曲视频" : "跳舞视频"} · {batchStatusLabel(item.status)}
                      {/* 出片中的条目在列表里也给出真实进度：分段 / 去字幕 / 二采第几批 */}
                      {item.childJob?.currentSegment && item.childJob.estimatedSegments
                        ? ` · 分段 ${item.childJob.currentSegment}/${item.childJob.estimatedSegments}`
                        : ""}
                      {/* 每条自己的时间（用户 2026-09-13：「当前任务队列的时间也给下」） */}
                      {queueTime(item) ? ` · ${queueTime(item)}` : ""}
                    </small>
                    {/* 模型起的标题认不出是哪条视频：列表里再挂一行源作品自己的文案 */}
                    {sourceCaption(item) && (
                      <small className="batch-item-source" title={sourceCaption(item)}>
                        源：{sourceCaption(item)}
                      </small>
                    )}
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
                    <h2>{itemTitle(selected)}</h2>
                    <div className="batch-detail-meta">
                      <a href={selected.url} target="_blank" rel="noreferrer">查看原抖音链接</a>
                      {/* 本条已运行时间：跑到哪一步、一共花了多久 */}
                      {itemElapsedMs !== null && (
                        <span className="batch-timer" title={selected.finishedAt ? "本条总耗时" : "本条已运行时间（含排队）"}>
                          <Timer weight="fill" /> {formatElapsedMs(itemElapsedMs)}
                          {selected.finishedAt ? "（总）" : ""}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="batch-item-actions">
                    {/* 失败 / 已跳过 / 已出片：都能直接再出一版（同一接口，已确认过的只重跑出片） */}
                    {selected.status === "failed" && <button onClick={() => itemCall("retry")}><ArrowClockwise />重试</button>}
                    {["skipped", "completed"].includes(selected.status) && (
                      <button
                        onClick={() => {
                          if (
                            selected.status !== "completed"
                            || window.confirm("再出一版？会重新跑一遍生成链路，新成片会覆盖发布目录里的同名文件。")
                          ) {
                            void itemCall("retry");
                          }
                        }}
                        title={hasImage ? "沿用已有的候选图与文案，只重跑出片" : "从下载抖音视频与备料开始重做这一条"}
                      >
                        <ArrowClockwise />重新开始
                      </button>
                    )}
                    {/* 开始中了（已放行 / 正在出片）：给一个明确的「取消出片」，取消后就能重新开始 */}
                    {["confirmed", "running", "revising"].includes(selected.status) && (
                      <button
                        className="danger"
                        onClick={() => {
                          const message = renderingNow(selected)
                            ? "取消这一条当前的出片？已经生成到一半的进度会作废，取消后可以点「重新开始」再出片。"
                            : "这一条还没开始出片，取消这次放行不会动到其它条目。取消后可以点「重新开始」。";
                          if (window.confirm(message)) {
                            void itemCall("skip");
                          }
                        }}
                        title="停止这一条当前的生成/出片；取消后可以重新开始"
                      >
                        <X weight="bold" />取消出片
                      </button>
                    )}
                    {["pending", "awaiting_review"].includes(selected.status) && (
                      <button onClick={() => itemCall("skip")}><X />跳过</button>
                    )}
                    {canReopen && (
                      <button
                        onClick={() => {
                          // 只有真的在出片的条目才会作废进度；`confirmed`（已放行、还没轮到）
                          // 退回去只是把放行作废，不碰任何正在跑的生成，不用吓唬用户。
                          if (
                            !renderingNow(selected)
                            || window.confirm("这一条正在出片。回到确认会先取消当前出片（已生成到一半的进度作废），确定吗？")
                          ) {
                            void itemCall("reopen-review");
                          }
                        }}
                        title={
                          renderingNow(selected)
                            ? "回到「等待你的确认」：会先安全取消这一条当前的出片"
                            : "回到「等待你的确认」，可以换图、改比例或改去除字幕后重新确认（不会影响其它条目）"
                        }
                      >
                        <ArrowUUpLeft />回到确认
                      </button>
                    )}
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

                {/* 本条对应的源视频：确认前必须先能认出「这是哪条抖音视频」。
                    条目上的标题是模型重起的发布标题，源作品文案 + 可播放源片 + 作品号才认得出。
                    认出来不对就地替换（用户 2026-09-13：「要有让我可以替换的操作」）。 */}
                <section className="batch-source-panel">
                  <div className="batch-panel-title">
                    <span>本条源视频</span>
                    <div className="batch-source-head">
                      <small>
                        {selected.kind === "singing" ? "唱歌条目" : "跳舞条目"}
                        {selected.awemeId ? ` · 抖音作品号 ${selected.awemeId}` : " · 还没下载"}
                      </small>
                      {canReplaceSource && (
                        <button
                          onClick={() => {
                            setReplaceKind(selected.kind);
                            setReplaceUrl("");
                            setReplacingSource((open) => !open);
                          }}
                        >
                          <ArrowClockwise />{replacingSource ? "收起" : "替换源视频"}
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="batch-source-body">
                    {selected.sourcePath ? (
                      <video
                        key={selected.sourcePath}
                        src={`/api/batches/${batch.id}/items/${selected.id}/stage/source`}
                        controls
                        preload="metadata"
                      />
                    ) : (
                      <div className="batch-source-empty">还没下载源视频<br />下载完成后这里可以直接播放核对</div>
                    )}
                    <div className="batch-source-meta">
                      <strong title={sourceCaption(selected)}>
                        {sourceCaption(selected) || "这一条还没有下载源视频"}
                      </strong>
                      {selected.sourceName && <small title={selected.sourceName}>{selected.sourceName}</small>}
                      <em>
                        {selected.kind === "singing"
                          ? "出片时按这条视频的画面与音轨生成"
                          : "出片时按这条视频的动作做迁移"}
                      </em>
                      <a href={selected.url} target="_blank" rel="noreferrer">打开抖音原链接</a>
                    </div>
                  </div>

                  {replacingSource && (
                    <div className="batch-source-replace">
                      <label>
                        <span>换成哪条抖音链接</span>
                        <textarea
                          rows={2}
                          value={replaceUrl}
                          onChange={(event) => setReplaceUrl(event.target.value)}
                          placeholder="粘贴抖音分享链接，或 www.douyin.com/video/作品号"
                        />
                      </label>
                      <div className="batch-source-kind">
                        <span>类型</span>
                        {(["singing", "dance"] as const).map((value) => (
                          <button
                            key={value}
                            className={replaceKind === value ? "active" : ""}
                            onClick={() => setReplaceKind(value)}
                          >
                            {value === "singing" ? "唱歌视频" : "跳舞视频"}
                          </button>
                        ))}
                      </div>
                      <p className="field-note">
                        替换后这一条会作废按旧视频做的分析、出图提示词与文案，重新下载并备料，然后停在「等待你的确认」。
                        {replaceKind !== selected.kind
                          ? ` 类型改成${replaceKind === "singing" ? "唱歌视频" : "跳舞视频"}，画布比例回到该类型默认值。`
                          : ""}
                      </p>
                      <div className="batch-review-actions">
                        <button onClick={() => setReplacingSource(false)}>取消</button>
                        <button
                          className="batch-primary"
                          disabled={!replaceUrl.trim() || Boolean(busyAction)}
                          onClick={async () => {
                            await itemCall("source", "POST", { url: replaceUrl.trim(), kind: replaceKind });
                            setReplaceUrl("");
                            setReplacingSource(false);
                          }}
                        >
                          {busyAction.endsWith("/source") ? <SpinnerGap className="spin" /> : <ArrowClockwise />}
                          替换并重新备料
                        </button>
                      </div>
                    </div>
                  )}
                </section>

                {selected.status === "pending" && (
                  <p className="field-note">
                    {batch?.status === "paused" || batch?.pauseRequested
                      ? "这一条还在排队：批次处于暂停，点队列上方的「继续」后才会开始（下载抖音视频 → 生成人物图素材与发布文案 → 停下来等你确认）。"
                      : "这一条还在排队：轮到它就会自动下载抖音视频、生成人物图素材与发布文案，然后停下来等你确认。"}
                  </p>
                )}

                {/* 这一屏的信息在**加入队列之后也要继续显示**（用户 2026-09-13），
                    只是出了审核点就不给改了：上传/换图与设置开关只在这里是 awaiting_review 时可用。 */}
                {selected.ai && (
                  <section className="batch-review">
                    <div className="batch-review-image">
                      <div className="batch-review-label"><ImageSquare /> 候选人物图 · 第 {(selected.revision || 0) + 1} 版</div>
                      {hasImage ? (
                        <img
                          src={`/api/batches/${batch.id}/items/${selected.id}/image?v=${imageToken || selected.revision || 0}`}
                          alt="候选人物图"
                        />
                      ) : atReview ? (
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
                      ) : (
                        <p className="batch-empty">这一条没有留下候选人物图。</p>
                      )}
                      {hasImage && atReview && (
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
                        <span>{atReview ? "等待你的确认" : `本条信息（只读）· ${batchStatusLabel(selected.status)}`}</span>
                        <small>{atReview ? `按 ${selectedRatio} 出片 · 确认前不会启动 ComfyUI` : `按 ${selectedRatio} 出片`}</small>
                      </div>

                      {selected.ai.song_name && <p className="batch-song-name">识别歌曲：{selected.ai.song_name}</p>}
                      <label><span>标题</span><p>{selected.ai.title}</p></label>
                      <label><span>简介</span><p>{selected.ai.introduction}</p></label>
                      <label><span>标签</span><div className="batch-tags">{selected.ai.tags.map((tag) => <i key={tag}>#{tag.replace(/^#/, "")}</i>)}</div></label>
                      {/* 确认这一屏要把后端掌握的**全部**信息给出来（用户 2026-09-13：
                          「报所有可以展示的信息都展示出来」「是指等待你的确认里的信息」）——
                          以前这里只有标题/简介/标签，歌曲情绪、造型来源、作品号、时间线
                          都藏在后端里，用户没法核对。条目日志不放这里（用户要求去掉）。 */}
                      <label>
                        <span>本条全部信息</span>
                        <dl className="batch-facts">
                          {reviewFacts.map(([label, value]) => (
                            <div key={label}>
                              <dt>{label}</dt>
                              <dd>{value || "—"}</dd>
                            </div>
                          ))}
                        </dl>
                      </label>
                      {atReview && renderSettings(selected)}
                      {atReview && (
                        <div className="batch-review-actions">
                          <button
                            className="batch-primary"
                            disabled={Boolean(busyAction) || !hasImage}
                            onClick={() => itemCall("confirm")}
                            title={hasImage ? undefined : "请先添加上这一条的候选人物图"}
                          >
                            <Check weight="bold" />加入队列并出片
                          </button>
                        </div>
                      )}
                    </div>
                  </section>
                )}

                {selected.ai && selected.status !== "awaiting_review" && settingsEditable && (
                  <section className="batch-prompt-panel">
                    <div className="batch-panel-title">
                      <span>出片前设置</span>
                      <small>
                        {selected.status === "confirmed"
                          ? "已加入出片队列，轮到它之前仍可改"
                          : "这一条还没开始出片，可以直接改"}
                      </small>
                    </div>
                    <div className="batch-review-copy">{renderSettings(selected)}</div>
                  </section>
                )}

                {/* 「已填写的动作与运镜 / 迁移提示词」整块去掉（2026-09-13 用户：
                    「已填写的迁移提示词 这块内容整个都可以去掉 我不关心」）——
                    提示词仍然照常提交给工作流，只是不再在页面上展示。 */}

                <section className="batch-progress-panel">
                  <div className="batch-panel-title"><span>当前条目进度</span><small>{batchStatusLabel(selected.status)}</small></div>
                  <div className="batch-steps">
                    {selected.milestones.map((step) => {
                      const percent = stepPercent(step);
                      const elapsed = elapsedLabel(step.startedAt);
                      return (
                        <div className={`batch-step ${step.status}`} key={step.id}>
                          <span className="batch-step-icon">{stepIcon(step.status)}</span>
                          <div><strong>{step.label}</strong><small>{step.currentNode || step.subtitle}</small></div>
                          {step.status === "running" && (percent !== null
                            ? <em>{percent}%</em>
                            : (
                              <em className="indeterminate" title="该步骤没有节点级进度，按实际耗时显示">
                                进行中{elapsed ? ` · ${elapsed}` : ""}
                              </em>
                            ))}
                        </div>
                      );
                    })}
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

                {/* 成品区整个去掉（2026-09-13 用户指着一张只有「最终成片 / 人物图 / 发布文案 /
                    打开文件夹」的截图说「这个没有去掉吗 不是说去掉吗」）：交付照做（文件照样写进
                    发布目录），页面上不再放这块面板。成片仍可在「生成阶段产物」里点开看。 */}

                {(selected.error || selected.warning) && <div className="batch-alert"><WarningCircle weight="fill" />{selected.error || selected.warning}</div>}
              </>
            ) : <div className="batch-empty-detail"><ListChecks /><p>填写链接并开始后，审核和真实进度会显示在这里。</p></div>}
          </div>
        </section>
      )}
    </main>
  );
}
