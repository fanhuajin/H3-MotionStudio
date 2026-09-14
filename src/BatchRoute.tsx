import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { readJson } from "./api";
import { formatElapsedMs, useNowTick } from "./jobTime";
import {
  ArrowClockwise,
  ArrowUUpLeft,
  CaretDown,
  CaretUp,
  Check,
  Circle,
  ImageSquare,
  ListChecks,
  MusicNotes,
  PersonSimpleRun,
  Play,
  Power,
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
  finishedAt?: string | null;
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

/**
 * 单条**实际用时**：只算真正干活的阶段（下载 / 备料 / 出片），不含排队与等你确认的空闲。
 *
 * 用户 2026-09-15：「我想知道的是单条用时，现在的时间不对都 22 小时还多了」——旧计时从条目
 * `createdAt`（加入队列那一刻）算起，批次放了 22 小时就一直累加，完全不是这条片子花了多久。
 * 现在：正在跑哪个阶段就从那个阶段的 `startedAt` 算到此刻（`review` 是「等你确认」，不算干活）；
 * 已经出完片就给出片（`video`）阶段的实际耗时；排队 / 等确认 / 还没开始则返回 null（不显示时间）。
 */
function itemActiveMs(item: BatchItem, nowMs: number): number | null {
  const milestones = item.milestones || [];
  const running = milestones.filter(
    (step) => step.status === "running" && step.id !== "review" && step.startedAt,
  );
  const active = running.length ? running[running.length - 1] : undefined;
  if (active?.startedAt) {
    const started = Date.parse(active.startedAt);
    if (Number.isFinite(started)) return Math.max(0, nowMs - started);
  }
  const video = milestones.find((step) => step.id === "video");
  if (video?.startedAt && video.finishedAt) {
    const started = Date.parse(video.startedAt);
    const finished = Date.parse(video.finishedAt);
    if (Number.isFinite(started) && Number.isFinite(finished)) return Math.max(0, finished - started);
  }
  return null;
}

/**
 * 列表行里的进度百分比：**只认真实、非零的进度**，点开前也能看到跑到哪了。
 *
 * 用户 2026-09-15：「除了时间 进入也同步的列表中 像现在的 17% 这样的 我有时候不想点开来看」。
 * 口径与展开详情一致，但更保守：优先用出片子任务的 `childJob.progress`；没有的话只看**正在出片**
 * 的那一格。**备料阶段的百分比是本地粗刻度（5/8/99），不往列表上放**。
 * 跳舞（SCAIL）链路不广播采样进度、`childJob.progress` 是 `None` —— 那就返回 null 不显示数字，
 * 绝不写 0 或假百分比（项目铁律：进度不得造假）。
 */
function itemPercent(item: BatchItem): number | null {
  const child = item.childJob?.progress;
  if (typeof child === "number" && child > 0) return Math.round(child);
  const video = (item.milestones || []).find(
    (step) => step.id === "video" && step.status === "running",
  );
  return video ? stepPercent(video) : null;
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
  /** 跳舞条目的迁移模式：`animation`=动作迁移（默认）/ `replacement`=人物替换 */
  migrate_mode?: MigrateMode;
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
  /** 源视频来源：`douyin`=抖音下载（默认）/ `local`=本机选择的文件 */
  sourceOrigin?: string;
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
  /** 整批跑完后是否自动关机（页面开关，**默认关闭**） */
  shutdownOnComplete?: boolean;
  /** 时间戳（ISO）：用来算「已运行多久」 */
  createdAt?: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  items: BatchItem[];
}

/**
 * 已经排好的自动关机（`GET /api/system/shutdown`）。
 *
 * 倒计时用后端给的 `executeAt`（epoch 秒）在本地每秒重算，所以数字会真的跳；
 * `secondsLeft` 只在 `executeAt` 缺失时兜底。
 */
interface ShutdownStatus {
  pending: boolean;
  disabled?: boolean;
  delaySeconds?: number;
  batchId?: string | null;
  executeAt?: number;
  secondsLeft?: number;
}

type CanvasRatio = "4:3" | "9:16";

/** 跳舞条目的迁移模式（对应 `/api/jobs/migrate` 的 `mode`，工作流节点 #353）。 */
type MigrateMode = "animation" | "replacement";
const MIGRATE_MODE_LABEL: Record<MigrateMode, string> = {
  animation: "动作迁移",
  replacement: "人物替换",
};
const MIGRATE_MODE_NOTE: Record<MigrateMode, string> = {
  animation: "把源视频的动作迁移到候选图的人身上",
  replacement: "保留源视频场景，把里面的人物换成候选图的人",
};

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

/**
 * 「本条源视频」卡片右上角那行：这条源视频是抖音下载的还是本机选的文件。
 *
 * 本机换源之后，旧的抖音作品号与源作品文案已经不再成立，必须显示「本机视频」，
 * 否则用户看不出替换到底成功没有（2026-09-15 用户：「本条源视频 那边的内容也替换一下
 * 不然我不知道是否修改成功了」）。
 */
function sourceOriginLabel(item: BatchItem): string {
  if (item.sourceOrigin === "local") return " · 本机视频";
  if (item.awemeId) return ` · 抖音作品号 ${item.awemeId}`;
  if (item.sourcePath) return " · 本机视频";
  return " · 还没下载";
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
    // 「全部完成后自动关机」：**默认关闭**（2026-09-15 用户：「默认关闭」）。
    // 还没建批次时先记在本地，点「准备任务」时随批次一起提交。
    shutdownOn: parsed.shutdownOn === true,
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
    awaiting_review: "待确认",
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

function responseMessage(response: Response, fallback: string): Promise<string> {
  // 交给统一的 readJson：后端 500 现在也是 JSON（{"detail": ...}），且绝不会把
  // "Unexpected token 'I'..." 这种解析错误当成给用户看的提示。
  return readJson<{ detail?: string }>(response, fallback)
    .then(() => fallback)
    .catch((reason) => (reason instanceof Error ? reason.message : fallback));
}

/**
 * 状态筛选标签：**只要两档 —— 「未完成 / 已完成」**（2026-09-15 用户：「其实状态我不关注其他的
 * 内容我只关注还未完成的 和已经完成的。除了已经完成的其他的都算未完成的」）。
 *
 * 细状态没有丢：**每一行的状态徽章照旧显示**（待确认 / 备料中 / 出片中 / 已确认 / 已跳过 /
 * 已失败），所以合在「未完成」里也能一眼看出这条卡在哪一步、哪几条需要你动手。
 * 「未完成」= 除 `completed` 以外的一切（含已跳过 / 已失败 —— 用户明确说它们算未完成）。
 */
type TabId = "open" | "completed";
const TABS: Array<{ id: TabId; label: string }> = [
  { id: "open", label: "未完成" },
  { id: "completed", label: "已完成" },
];
const TAB_MATCH: Record<TabId, (item: BatchItem) => boolean> = {
  open: (item) => item.status !== "completed",
  completed: (item) => item.status === "completed",
};

export function BatchRoute() {
  const initial = useMemo(readInputDraft, []);
  const [singing, setSinging] = useState(initial.singing);
  const [dance, setDance] = useState(initial.dance);
  const [singingOn, setSingingOn] = useState(initial.singingOn);
  const [danceOn, setDanceOn] = useState(initial.danceOn);
  // 「全部完成后自动关机」（默认关闭）：有批次时以批次上的值为准，没有批次时用本地草稿
  const [shutdownOn, setShutdownOn] = useState(initial.shutdownOn);
  const [shutdown, setShutdown] = useState<ShutdownStatus>({ pending: false });
  const [batch, setBatch] = useState<BatchState | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // 状态筛选标签只要两档：**默认「未完成」**（2026-09-15 用户：「除了已经完成的其他的都算未完成的」）
  const [activeTab, setActiveTab] = useState<TabId>("open");
  // 批量操作勾选：后台表格交互（2026-09-15 用户要求批量确认/跳过/删除）。
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
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
  // 当前标签下显示的条目；展开的详情行也只在当前标签里渲染。
  const filteredItems = useMemo(() => visibleItems.filter(TAB_MATCH[activeTab]), [visibleItems, activeTab]);
  const selected = useMemo(
    () => visibleItems.find((item) => item.id === selectedId) || null,
    [visibleItems, selectedId],
  );
  const tabCount = (id: TabId) => visibleItems.filter(TAB_MATCH[id]).length;

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
    localStorage.setItem(INPUT_KEY, JSON.stringify({ singing, dance, singingOn, danceOn, shutdownOn }));
  }, [singing, dance, singingOn, danceOn, shutdownOn]);

  /**
   * 自动关机状态：单独轮询（批次轮询 3.5 秒一次，这里 5 秒一次足够）。
   * 倒计时数字由 `executeAt` + 每秒 tick 在本地算，所以不依赖这个轮询的频率。
   */
  useEffect(() => {
    let alive = true;
    const load = () => {
      fetch("/api/system/shutdown", { cache: "no-store" })
        .then((response) => (response.ok ? response.json() : null))
        .then((data: ShutdownStatus | null) => {
          if (alive && data) setShutdown(data);
        })
        .catch(() => undefined);
    };
    load();
    const timer = window.setInterval(load, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

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

  // 详情**完全由用户自己点开**（2026-09-15 用户：「我希望启动页面的时候 列表默认都是收起来的
  // 由我自己点击要查看哪个」）：不再自动跟随「当前条目」、也不再在状态变化时抢着切换标签 ——
  // 那两套自动行为正是「默认展开后标签切换不了 / 点开又被抢走」的根因。
  const selectItem = (itemId: string) => {
    // 2026-09-15 用户：「首次点击现在是张开，再次点击要收起」——再点同一行就收起
    setSelectedId((current) => (current === itemId ? null : itemId));
  };

  // 换条目就收起「替换源视频」表单，免得把 A 条的链接写到 B 条上
  useEffect(() => {
    setReplacingSource(false);
    setReplaceUrl("");
  }, [selectedId]);

  const switchTab = (id: TabId) => {
    setActiveTab(id);
  };

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
      // 新建批次时把「全部完成后自动关机」一起提交（追加时该开关已经挂在批次上，由开关接口改）
      const payload = append
        ? { singingUrls, danceUrls, autoStart }
        : { singingUrls, danceUrls, autoStart, shutdownOnComplete: shutdownOn };
      const response = await fetch(target, {        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await responseMessage(response, append ? "加入队列失败" : "创建队列失败"));
      const state = await readJson<BatchState>(response, append ? "加入队列失败" : "创建队列失败");
      setBatch(state);
      // 新建批次也不自动展开任何一条：列表默认全收起，由用户自己点开要看的那条
      // （2026-09-15 用户：「启动页面的时候 列表默认都是收起来的 由我自己点击要查看哪个」）。
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

  /** 表格行里的单条操作：不要求是当前展开的那一条。 */
  const itemCallFor = async (item: BatchItem, action: string, method = "POST", body?: object) => {
    if (!batch) return;
    await call(`items/${item.id}${action ? `/${action}` : ""}`, method, body);
  };

  /** 批量操作：勾选后一次确认 / 跳过 / 删除（后端逐个校验，出片仍一条一条来）。 */
  const batchOp = async (action: "confirm-many" | "skip-many" | "delete-many", confirmText?: string) => {
    if (!batch || selectedIds.size === 0) return;
    if (confirmText && !window.confirm(confirmText.replace("N", String(selectedIds.size)))) return;
    const endpoint = `/api/batches/${batch.id}/items/${action}`;
    setBusyAction(action);
    setError("");
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ itemIds: [...selectedIds] }),
      });
      if (!response.ok) throw new Error(await responseMessage(response, "批量操作失败"));
      setBatch(await readJson<BatchState>(response, "批量操作失败"));
      setSelectedIds(new Set());
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(`${message}　〔POST ${endpoint}〕`);
    } finally {
      setBusyAction("");
    }
  };

  /** 队列里上移 / 下移（调处理顺序，出片严格按队列顺序跑）。 */
  const moveItem = (itemId: string, direction: "up" | "down") => {
    if (!batch) return;
    const endpoint = `/api/batches/${batch.id}/items/${itemId}/move`;
    setBusyAction(`move-${itemId}`);
    setError("");
    fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction }),
    })
      .then((response) => {
        if (!response.ok) return responseMessage(response, "调整顺序失败").then((message) => { throw new Error(message); });
        return readJson<BatchState>(response, "调整顺序失败");
      })
      .then(setBatch)
      .catch((reason) => setError(`${reason instanceof Error ? reason.message : String(reason)}　〔POST ${endpoint}〕`))
      .finally(() => setBusyAction(""));
  };

  /**
   * 「全部完成后自动关机」开关（默认关闭）。
   *
   * 有批次时以**批次上的值**为准（服务端持久化，重启和换页面都还在），没有批次时先记在本地，
   * 点「准备任务」时随批次一起提交。关掉开关会把已经排好的关停一并撤销。
   */
  const toggleShutdownOnComplete = () => {
    const next = !(batch ? Boolean(batch.shutdownOnComplete) : shutdownOn);
    setShutdownOn(next);
    if (!batch) return;
    void call("shutdown-on-complete", "POST", { enabled: next }).then(() => {
      fetch("/api/system/shutdown", { cache: "no-store" })
        .then((response) => (response.ok ? response.json() : null))
        .then((data: ShutdownStatus | null) => data && setShutdown(data))
        .catch(() => undefined);
    });
  };

  /** 撤销已经排好的自动关机（倒计时里点「取消关机」）。 */
  const cancelShutdown = async () => {
    setBusyAction("shutdown-cancel");
    setError("");
    try {
      const response = await fetch("/api/system/shutdown/cancel", { method: "POST" });
      if (!response.ok) throw new Error(await responseMessage(response, "取消关机失败"));
      setShutdown({ pending: false });
      await loadLatest();
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(`${message}　〔POST /api/system/shutdown/cancel〕`);
    } finally {
      setBusyAction("");
    }
  };

  // 开关显示的永远是**当前批次**的值（没批次才用本地草稿）
  const shutdownSwitchOn = batch ? Boolean(batch.shutdownOnComplete) : shutdownOn;
  // 倒计时每秒跳：后端只给时间戳，剩几秒在本地算
  const shutdownTick = useNowTick(Boolean(shutdown.pending));
  const shutdownSecondsLeft = shutdown.pending
    ? Math.max(
        0,
        Math.ceil(
          (typeof shutdown.executeAt === "number"
            ? shutdown.executeAt
            : shutdownTick / 1000 + Number(shutdown.secondsLeft || 0)) - shutdownTick / 1000,
        ),
      )
    : 0;
  const shutdownDelaySeconds = Number(shutdown.delaySeconds || 60);

  const hasImage = Boolean(selected?.ai?.reference_image_path);
  // 2026-09-15 用户：「只要状态是未完成的任务都可以进行编辑，当然正在运行的那条不允许编辑」——
  // 未完成的条目（pending / awaiting_review / confirmed / failed / skipped）都能直接改画布比例、
  // 去除字幕、换候选图；出片中/重新备料/已完成/已删除 不能编辑。
  const editable = Boolean(
    selected && !["running", "revising", "completed", "deleted"].includes(selected.status),
  );
  // 「回到确认」只保留给「正在出片/重新备料」与「已完成」（2026-09-15 用户确认）：
  // confirmed / failed / skipped 已经能直接编辑，不需要再退回审核点重来。
  const canReopen = Boolean(
    selected?.ai && ["running", "revising", "completed"].includes(selected.status),
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
  // 迁移模式：动作迁移（默认）/ 人物替换 —— 2026-09-15 用户想试人物替换的效果
  const selectedMigrateMode: MigrateMode =
    selected?.ai?.migrate_mode === "replacement" ? "replacement" : "animation";
  const changeMigrateMode = (mode: MigrateMode) => {
    if (!selected || selectedMigrateMode === mode) return;
    void itemCall("migrate-mode", "POST", { mode });
  };

  /** 出片前的两个设置（画布比例 + 跳舞条目的去除字幕）：审核区与「出片前设置」面板共用。 */
  const renderSettings = (item: BatchItem) => {
    const ratio = itemRatio(item);
    const subtitles = Boolean(item.ai?.remove_subtitles);
    // 迁移模式：老条目没有这个字段 → 按默认「动作迁移」显示（与出片时提交的默认值一致）
    const migrateMode: MigrateMode =
      item.ai?.migrate_mode === "replacement" ? "replacement" : "animation";
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
        {item.kind === "dance" && (
          <label>
            <span>迁移模式</span>
            <div className="batch-ratio-pick" role="radiogroup" aria-label="这一条的迁移模式">
              {(["animation", "replacement"] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={migrateMode === value}
                  className={migrateMode === value ? "selected" : ""}
                  disabled={Boolean(busyAction)}
                  onClick={() => changeMigrateMode(value)}
                >
                  {MIGRATE_MODE_LABEL[value]}
                </button>
              ))}
              <i>{MIGRATE_MODE_NOTE[migrateMode]}</i>
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

  // 「等待你的确认」那一屏要把后端掌握的**全部**信息摊开；但排障用的技术字段（条目 id、
  // 下载/视频子任务、源文件路径）收进「高级信息」折叠区，不再一上来就堆满一屏
  // （2026-09-15 用户：「信息展示太杂」）。
  const reviewCoreFacts = (ai: BatchAI, item: BatchItem): Array<[string, string]> => {
    const image = String(ai.reference_image_path || "");
    const facts: Array<[string, string]> = [
      ["类型", item.kind === "singing" ? "唱歌视频" : "跳舞视频"],
      ["状态", `${batchStatusLabel(item.status)} · ${item.stage}`],
    ];
    // 跳舞条目：这条到底按哪种模式出片 —— 它决定提交给迁移工作流 #353 的开关
    // （2026-09-15 用户：「本条信息 里也加上是动作迁移 还是人物替换 的信息描述」）。
    if (item.kind === "dance") {
      const mode: MigrateMode =
        ai.migrate_mode === "replacement" ? "replacement" : "animation";
      facts.push(["迁移模式", `${MIGRATE_MODE_LABEL[mode]} · ${MIGRATE_MODE_NOTE[mode]}`]);
    }
    facts.push(
      ["抖音作品号", String(item.awemeId || "")],
      ["源作品文案", sourceCaption(item)],
      ["源文件名", String(item.sourceName || "")],
      ["识别歌曲", String(ai.song_name || "")],
      ["歌曲情绪", String(ai.song_mood || "")],
      ["造型来源", styleSourceLabel(ai.style_source)],
      ["候选人物图", image ? image.split(/[\\/]/).pop() || image : "还没有（等你上传 GPT 生成的图）"],
      ["候选图版本", `第 ${(item.revision || 0) + 1} 版`],
      ["审核放行", item.status === "awaiting_review" ? "还没放行" : "已放行"],
      ["创建时间", formatLogTime(item.createdAt)],
    );
    return facts;
  };
  const reviewAdvancedFacts = (item: BatchItem): Array<[string, string]> => [
    ["条目 id", item.id],
    ["下载子任务", String(item.downloadJobId || "")],
    ["视频子任务", String(item.videoJobId || "")],
    ["源文件路径", String(item.sourcePath || "")],
    ["最近更新", formatLogTime(item.updatedAt)],
  ];

  // 已运行时间：批次还在跑就实时跳秒；已结束显示总耗时。
  const batchLive = Boolean(batch && !batch.finishedAt && !["completed", "cancelled", "failed"].includes(batch.status));
  // 时间只统计「本条」（2026-09-15 用户：「你只需统计 本条的时间 我不关心所有任务的时间」）：
  // 表格里只给**正在处理的那一条**（= batch.currentItemId，且状态在 备料中/出片中/重新备料）显示
  // 「已用 X」，其它任务不显示；展开详情头部另有「本条实际用时」。跳秒逻辑保留（批次可能刚收尾）。
  const queueLive = visibleItems.some(
    (item) => !item.finishedAt && ["running", "revising", "confirmed"].includes(item.status),
  );
  const batchNowTick = useNowTick(batchLive || queueLive);

  // 本条实际用时：只算真正干活的阶段（下载 / 备料 / 出片），不含排队与等你确认。
  const itemElapsedMs = selected ? itemActiveMs(selected, batchNowTick) : null;
  /** 表格行里「本条进行中」那条的实际用时（同上：只算真正干活的时间）。 */
  const itemElapsedText = (item: BatchItem): string => {
    const ms = itemActiveMs(item, batchNowTick);
    return ms === null ? "" : formatElapsedMs(ms);
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

  /**
   * 用**本机选择的视频文件**替换这一条的源视频：只换视频，其余内容一律不动
   * （2026-09-15 用户：「替换源视频可以让我进行本地选择」+「所有定义好的内容都不需要变」）。
   */
  const replaceSourceFile = async (file: File) => {
    if (!batch || !selected) return;
    const endpoint = `/api/batches/${batch.id}/items/${selected.id}/source-file`;
    setBusyAction("source-file");
    setError("");
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch(endpoint, { method: "POST", body });
      if (!response.ok) throw new Error(await responseMessage(response, "替换源视频失败"));
      setBatch(await readJson<BatchState>(response, "替换源视频失败"));
      setReplacingSource(false);
      setNotice(`已把这一条的源视频换成本机文件「${file.name}」，标题、文案、候选图保持原样。`);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(`${message}　〔POST ${endpoint}〕`);
    } finally {
      setBusyAction("");
    }
  };
  const canStart = (singingOn && splitUrls(singing).length > 0) || (danceOn && splitUrls(dance).length > 0);
  const effectiveTotal = Math.max(0, (batch?.total || 0) - (batch?.deletedCount || 0));

  // —— 批量操作（勾选） ——
  const allChecked = filteredItems.length > 0 && filteredItems.every((item) => selectedIds.has(item.id));
  const toggleSelect = (itemId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(itemId)) next.delete(itemId); else next.add(itemId);
      return next;
    });
  };
  const toggleSelectAll = () => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (allChecked) filteredItems.forEach((item) => next.delete(item.id));
      else filteredItems.forEach((item) => next.add(item.id));
      return next;
    });
  };
  const selectedItems = visibleItems.filter((item) => selectedIds.has(item.id));
  const anyConfirmable = selectedItems.some(
    (item) => item.status === "awaiting_review" && Boolean(item.ai?.reference_image_path),
  );
  const anySkippable = selectedItems.some(
    (item) => !["completed", "skipped", "deleted"].includes(item.status),
  );
  const anyDeletable = selectedItems.some((item) => item.status !== "deleted");

  // —— 表格行里按状态给的操作按钮（每行只给最该做的 1~2 个动作） ——
  const statusAction = (item: BatchItem) => {
    if (item.status === "awaiting_review") {
      return (
        <>
          <button
            className="batch-primary small"
            disabled={Boolean(busyAction) || !item.ai?.reference_image_path}
            onClick={() => void itemCallFor(item, "confirm")}
            title={item.ai?.reference_image_path ? undefined : "先添加上这一条的候选人物图"}
          >
            <Check weight="bold" />确认并出片
          </button>
          <button onClick={() => void itemCallFor(item, "skip")}><X />跳过</button>
        </>
      );
    }
    if (item.status === "pending") {
      return <button onClick={() => void itemCallFor(item, "skip")}><X />跳过</button>;
    }
    if (["confirmed", "running", "revising"].includes(item.status)) {
      return (
        <button
          className="danger"
          onClick={() => {
            const message = renderingNow(item)
              ? "停止这一条当前的出片？已经生成到一半的进度会作废，取消后可以点「重新开始」再出片。"
              : "这一条还没开始出片，停止这次放行不会动到其它条目。停止后可以点「重新开始」。";
            if (window.confirm(message)) void itemCallFor(item, "skip");
          }}
          title="停止这一条当前的生成/出片；取消后可以重新开始"
        >
          <X weight="bold" />取消
        </button>
      );
    }
    if (item.status === "failed") {
      return <button onClick={() => void itemCallFor(item, "retry")}><ArrowClockwise />重试</button>;
    }
    if (["skipped", "completed"].includes(item.status)) {
      return (
        <button
          onClick={() => {
            if (
              item.status !== "completed"
              || window.confirm("再出一版？会重新跑一遍生成链路，新成片会覆盖发布目录里的同名文件。")
            ) {
              void itemCallFor(item, "retry");
            }
          }}
          title={item.ai?.reference_image_path ? "沿用已有的候选图与文案，只重跑出片" : "从下载抖音视频与备料开始重做这一条"}
        >
          <ArrowClockwise />重新开始
        </button>
      );
    }
    return null;
  };

  // —— 展开行的完整详情 ——
  const renderSelectedDetail = () => {
    if (!selected) return null;
    const item = selected;
    const reviewCore = item.ai ? reviewCoreFacts(item.ai, item) : [];
    const reviewAdvanced = reviewAdvancedFacts(item);
    return (
      <div className="batch-expanded">
        <div className="batch-detail-head">
          <div>
            <p>第 {item.index} 条 · {item.kind === "singing" ? "歌曲视频" : "跳舞视频"}</p>
            <h2>{itemTitle(selected)}</h2>
            <div className="batch-detail-meta">
              <a href={item.url} target="_blank" rel="noreferrer">查看原抖音链接</a>
              {/* 本条实际用时：只算下载 / 备料 / 出片，不含排队与等你确认（只显示时间，不加标签字） */}
              {itemElapsedMs !== null && (
                <span className="batch-timer" title="本条实际用时（只算下载 / 备料 / 出片，不含排队与等你确认）">
                  <Timer weight="fill" /> {formatElapsedMs(itemElapsedMs)}
                </span>
              )}
            </div>
          </div>
          <div className="batch-item-actions">
            {/* 失败 / 已跳过 / 已出片：都能直接再出一版（同一接口，已确认过的只重跑出片） */}
            {["failed", "skipped", "completed"].includes(item.status) && (
              <button
                onClick={() => {
                  if (
                    item.status !== "completed"
                    || window.confirm("再出一版？会重新跑一遍生成链路，新成片会覆盖发布目录里的同名文件。")
                  ) {
                    void itemCall("retry");
                  }
                }}
                title={hasImage ? "沿用已有的候选图与文案，只重跑出片" : "从下载抖音视频与备料开始重做这一条"}
              >
                <ArrowClockwise />
                {item.status === "failed" ? "重试" : "重新开始"}
              </button>
            )}
            {canReopen && (
              <button
                onClick={() => {
                  // 只有真的在出片的条目才会作废进度；`confirmed`（已放行、还没轮到）
                  // 退回去只是把放行作废，不碰任何正在跑的生成，不用吓唬用户。
                  if (
                    !renderingNow(item)
                    || window.confirm("这一条正在出片。回到确认会先取消当前出片（已生成到一半的进度作废），确定吗？")
                  ) {
                    void itemCall("reopen-review");
                  }
                }}
                title={
                  renderingNow(item)
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
                if (window.confirm(`删除第 ${item.index} 条？正在跑的步骤会被安全取消，已生成的文件会保留。`)) {
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
                {item.kind === "singing" ? "唱歌条目" : "跳舞条目"}
                {sourceOriginLabel(item)}
              </small>
              {canReplaceSource && (
                <button
                  onClick={() => {
                    setReplaceKind(item.kind);
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
            {item.sourcePath ? (
              <video
                key={item.sourcePath}
                src={`/api/batches/${batch!.id}/items/${item.id}/stage/source`}
                controls
                preload="metadata"
              />
            ) : (
              <div className="batch-source-empty">还没下载源视频<br />下载完成后这里可以直接播放核对</div>
            )}
            <div className="batch-source-meta">
              <strong title={sourceCaption(item)}>
                {sourceCaption(item) || "这一条还没有下载源视频"}
              </strong>
              {item.sourceName && <small title={item.sourceName}>{item.sourceName}</small>}
              <em>
                {item.kind === "singing"
                  ? "出片时按这条视频的画面与音轨生成"
                  : "出片时按这条视频的动作做迁移"}
              </em>
              {/* 本机换源后没有抖音出处了，不显示「打开抖音原链接」，免得用户以为没换成功 */}
              {item.sourceOrigin !== "local" && (
                <a href={item.url} target="_blank" rel="noreferrer">打开抖音原链接</a>
              )}
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
                {replaceKind !== item.kind
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

              {/* 本机选择：只换视频，其它内容全部不动（2026-09-15 用户：「替换源视频可以让我进行
                  本地选择」+「其他内容都不需要改变只需要改变视频 所有定义好的内容都不需要变」）。 */}
              <div className="batch-local-source">
                <span>或者从本机选一个视频：<b>只换视频</b>，标题 / 简介 / 标签 / 候选图 / 比例全部保持不变</span>
                <label className={`batch-replace ${busyAction === "source-file" ? "busy" : ""}`}>
                  {busyAction === "source-file" ? <SpinnerGap className="spin" /> : <UploadSimple />}
                  {busyAction === "source-file" ? "正在上传…" : "选择本机视频"}
                  <input
                    type="file"
                    accept="video/mp4,video/quicktime,video/x-matroska,video/webm,.mp4,.mov,.mkv,.webm"
                    disabled={Boolean(busyAction)}
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void replaceSourceFile(file);
                      event.target.value = "";
                    }}
                  />
                </label>
              </div>
            </div>
          )}
        </section>

        {item.status === "pending" && (
          <p className="field-note">
            {batch?.status === "paused" || batch?.pauseRequested
              ? "这一条还在排队：批次处于暂停，点表格上方的「继续」后才会开始（下载抖音视频 → 生成人物图素材与发布文案 → 停下来等你确认）。"
              : "这一条还在排队：轮到它就会自动下载抖音视频、生成人物图素材与发布文案，然后停下来等你确认。"}
          </p>
        )}

        {/* 这一屏的信息在**加入队列之后也要继续显示**（用户 2026-09-13），
            只是出了审核点就不给改了：上传/换图与设置开关只在这里是 awaiting_review 时可用。 */}
        {item.ai && (
          <section className="batch-review">
            <div className="batch-review-image">
              <div className="batch-review-label"><ImageSquare /> 候选人物图 · 第 {(item.revision || 0) + 1} 版</div>
              {hasImage ? (
                <img
                  src={`/api/batches/${batch!.id}/items/${item.id}/image?v=${imageToken || item.revision || 0}`}
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
              {hasImage && editable && (
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
                <span>{atReview ? "等待你的确认" : `本条信息 · ${batchStatusLabel(item.status)}${editable ? "" : "（只读）"}`}</span>
                <small>{atReview ? `按 ${selectedRatio} 出片 · 确认前不会启动 ComfyUI` : `按 ${selectedRatio} 出片`}</small>
              </div>

              {item.ai.song_name && <p className="batch-song-name">识别歌曲：{item.ai.song_name}</p>}
              <label><span>标题</span><p>{item.ai.title}</p></label>
              <label><span>简介</span><p>{item.ai.introduction}</p></label>
              <label><span>标签</span><div className="batch-tags">{item.ai.tags.map((tag) => <i key={tag}>#{tag.replace(/^#/, "")}</i>)}</div></label>
              {/* 确认这一屏要把后端掌握的**全部**信息给出来（用户 2026-09-13）——
                  确认相关的字段直接展示；排障用的技术字段收进「高级信息」折叠区，避免一屏太杂
                  （2026-09-15 用户：「信息展示太杂」）。条目日志不放这里（用户要求去掉）。 */}
              <label>
                <span>本条全部信息</span>
                <dl className="batch-facts">
                  {reviewCore.map(([label, value]) => (
                    <div key={label}>
                      <dt>{label}</dt>
                      <dd>{value || "—"}</dd>
                    </div>
                  ))}
                </dl>
                <details className="batch-advanced">
                  <summary>高级信息（排障用）</summary>
                  <dl className="batch-facts">
                    {reviewAdvanced.map(([label, value]) => (
                      <div key={label}>
                        <dt>{label}</dt>
                        <dd>{value || "—"}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
              </label>
              {atReview && renderSettings(item)}
              {atReview && (
                <div className="batch-review-actions">
                  <button
                    className="batch-primary"
                    disabled={Boolean(busyAction) || !hasImage}
                    onClick={() => itemCall("confirm")}
                    title={hasImage ? undefined : "请先添加上这一条的候选人物图"}
                  >
                    <Check weight="bold" />确认并出片
                  </button>
                </div>
              )}
            </div>
          </section>
        )}

        {item.status !== "awaiting_review" && editable && (
          <section className="batch-prompt-panel">
            <div className="batch-panel-title">
              <span>出片前设置</span>
              <small>
                {item.status === "confirmed"
                  ? "已加入出片队列，轮到它之前仍可改"
                  : "这一条还没开始出片，可以直接改"}
              </small>
            </div>
            <div className="batch-review-copy">{renderSettings(item)}</div>
          </section>
        )}

        {/* 「已填写的动作与运镜 / 迁移提示词」整块去掉（2026-09-13 用户要求）——
            提示词仍然照常提交给工作流，只是不再在页面上展示。 */}

        <section className="batch-progress-panel">
          <div className="batch-panel-title"><span>当前条目进度</span><small>{batchStatusLabel(item.status)}</small></div>
          <div className="batch-steps">
            {item.milestones.map((step) => {
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
                  href={`/api/batches/${batch!.id}/items/${item.id}/stage/${key}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  {label}
                </a>
              ))}
            </div>
          </section>
        )}

        {item.childJob && (
          <section className="batch-child-panel">
            <div className="batch-panel-title">
              <span>真实生成流程</span>
              <small>{item.childJob.currentNodeTitle || batchStatusLabel(item.childJob.status)}</small>
            </div>
            <div className="batch-child-badges">
              {item.childJob.currentSegment && <i>H3 分段 {item.childJob.currentSegment}/{item.childJob.estimatedSegments}</i>}
              {item.childJob.cleanBatch && <i>去字幕 {item.childJob.cleanBatch}/{item.childJob.cleanBatches}</i>}
              {item.childJob.upscaleBatch && <i>二采 {item.childJob.upscaleBatch}/{item.childJob.upscaleBatches}</i>}
            </div>
            <div className="batch-steps compact">
              {(item.childJob.milestones || []).map((step) => (
                <div className={`batch-step ${step.status}`} key={step.id}>
                  <span className="batch-step-icon">{stepIcon(step.status)}</span>
                  <div><strong>{step.label}</strong><small>{step.subtitle}</small></div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* 成品区整个去掉（2026-09-13 用户要求）：交付照做（文件照样写进发布目录），
            页面上不再放这块面板。成片仍可在「生成阶段产物」里点开看。 */}

        {(item.error || item.warning) && <div className="batch-alert"><WarningCircle weight="fill" />{item.error || item.warning}</div>}
      </div>
    );
  };

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
          <small>点「准备任务」后立即开始：下载抖音视频 → 生成人物图与发布文案 → 停下来等你确认；确认后「确认并出片」直接开跑 ComfyUI 出片</small>
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
        <div className="batch-shutdown-row">
          <button
            type="button"
            role="switch"
            aria-checked={shutdownSwitchOn}
            className={`batch-switch ${shutdownSwitchOn ? "on" : ""}`}
            disabled={Boolean(busyAction)}
            onClick={toggleShutdownOnComplete}
          >
            <i />{shutdownSwitchOn ? "已开启" : "已关闭"}
          </button>
          <span>
            <b><Power weight="fill" /> 全部完成后自动关机</b>
            <small>
              所有条目都做完（没有排队、等你确认、待出片或出片中的）才会关机；关机前留 {shutdownDelaySeconds} 秒倒计时，
              随时可以点「取消关机」撤销。默认关闭。
            </small>
          </span>
        </div>
        <div className="batch-input-actions">
          <p><ListChecks /> 重复链接会自动跳过；备好料就停下来等你确认，点「确认并出片」才真正出片。</p>
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

      {/* 关机倒计时：这是**会真的关机**的状态，必须显眼并且能一键撤销 */}
      {shutdown.pending && (
        <div className="batch-alert shutdown">
          <Power weight="fill" />
          <span>
            所有任务已完成，<b>{shutdownSecondsLeft}</b> 秒后自动关机。
            <small>还有任务要处理的话现在点右边就能撤销。</small>
          </span>
          <button
            className="batch-primary small"
            disabled={Boolean(busyAction)}
            onClick={() => void cancelShutdown()}
          >
            <X />取消关机
          </button>
        </div>
      )}

      {batch && (
        <section className="batch-workspace">
          <div className="batch-table-card">
            <div className="batch-table-head">
              <div className="batch-tabs" role="tablist" aria-label="按状态筛选">
                {TABS.map((tab) => (
                  <button
                    key={tab.id}
                    type="button"
                    role="tab"
                    aria-selected={activeTab === tab.id}
                    className={`batch-tab ${activeTab === tab.id ? "active" : ""}`}
                    onClick={() => switchTab(tab.id)}
                  >
                    {tab.label}
                    <i>{tabCount(tab.id)}</i>
                  </button>
                ))}
              </div>
              {/* 「暂停 / 取消整批」两个按钮已去掉（2026-09-15 用户：「不需要暂停和取消整批
                  这两个按钮」）：整批控制改由**单条操作**承担（跳过 / 取消 / 删除，以及
                  批量操作栏），后端 `/pause`、`/resume`、`/cancel` 接口保留（API 能力）。
                  只留一个**恢复用**的「继续」，且只在批次已经处于暂停状态时出现 —— 这个状态页面
                  自己造不出来（唯一来源是本地服务在备料中途重启时的安全暂停），不留就会卡死。 */}
              {batch.status === "paused" && (
                <div className="batch-table-controls">
                  <button onClick={() => call("resume")} disabled={Boolean(busyAction)}><Play />继续</button>
                </div>
              )}
            </div>

            {/* 批量操作栏：勾选任意一条后出现 */}
            {selectedIds.size > 0 && (
              <div className="batch-bulk-bar">
                <span>已选 {selectedIds.size} 条</span>
                <button
                  className="batch-primary small"
                  disabled={!anyConfirmable || Boolean(busyAction)}
                  onClick={() => void batchOp("confirm-many")}
                  title={anyConfirmable ? "一次放行所有已勾选的待确认条目（缺候选图的不会放行）" : "勾选里没有可确认的条目（需待确认且有候选图）"}
                >
                  <Check weight="bold" />确认并出片
                </button>
                <button
                  disabled={!anySkippable || Boolean(busyAction)}
                  onClick={() => void batchOp("skip-many", "跳过选中的 N 条？正在出片的会先安全取消，成片已生成的会保留。")}
                >
                  <X />跳过
                </button>
                <button
                  className="danger"
                  disabled={!anyDeletable || Boolean(busyAction)}
                  onClick={() => void batchOp("delete-many", "删除选中的 N 条？正在跑的步骤会被安全取消，已生成的文件会保留。")}
                >
                  <Trash />删除
                </button>
                <button className="ghost" onClick={() => setSelectedIds(new Set())}>取消选择</button>
              </div>
            )}

            <div className="batch-table-wrap">
              <table className="batch-table">
                <thead>
                  <tr>
                    <th className="batch-check">
                      <input
                        type="checkbox"
                        aria-label="选择当前标签下全部任务"
                        checked={allChecked}
                        onChange={toggleSelectAll}
                      />
                    </th>
                    <th>任务</th>
                    <th>状态</th>
                    <th>比例</th>
                    <th className="right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredItems.length === 0 ? (
                    <tr>
                      <td colSpan={5}>
                        <p className="batch-empty">这个状态下还没有任务。</p>
                      </td>
                    </tr>
                  ) : (
                    filteredItems.map((item) => (
                      <Fragment key={item.id}>
                        <tr
                          className={`batch-row ${selectedId === item.id ? "selected" : ""} ${item.status}`}
                          onClick={() => selectItem(item.id)}
                        >
                          <td className="batch-check">
                            <input
                              type="checkbox"
                              aria-label={`选择第 ${item.index} 条`}
                              checked={selectedIds.has(item.id)}
                              onChange={() => toggleSelect(item.id)}
                              onClick={(event) => event.stopPropagation()}
                            />
                          </td>
                          <td className="batch-task-cell">
                            <div className="batch-task-title">
                              <i className={`batch-kind ${item.kind}`}>
                                {item.kind === "singing" ? <MusicNotes weight="fill" /> : <PersonSimpleRun weight="fill" />}
                                {item.kind === "singing" ? "歌曲" : "跳舞"}
                              </i>
                              <strong>{itemTitle(item)}</strong>
                            </div>
                            {/* 模型起的标题认不出是哪条视频：列表里再挂一行源作品自己的文案 */}
                            {sourceCaption(item) && (
                              <small className="batch-item-source" title={sourceCaption(item)}>
                                源：{sourceCaption(item)}
                              </small>
                            )}
                          </td>
                          <td className="batch-status-cell">
                            <span className={`batch-status ${item.status}`}>
                              {batchStatusLabel(item.status)}
                              {/* 进度直接在列表这一行显示，不用点开（2026-09-15 用户：「除了时间
                                  进入也同步的列表中 像现在的 17% 这样的 我有时候不想点开来看」）；
                                  只认真实非零进度，0/null 不显示数字（进度不得造假）。 */}
                              {itemPercent(item) !== null && (
                                <b className="batch-status-percent">{itemPercent(item)}%</b>
                              )}
                            </span>
                            {item.childJob?.currentSegment && item.childJob.estimatedSegments && (
                              <small>分段 {item.childJob.currentSegment}/{item.childJob.estimatedSegments}</small>
                            )}
                            {/* 只给「本条进行中」的那一条显示时间（实际干活时间，不含排队/等确认）；不要「已用」这类标签字 */}
                            {batch?.status === "running"
                              && item.id === batch?.currentItemId
                              && ["pending", "running", "revising"].includes(item.status)
                              && itemElapsedText(item) && (
                                <small className="batch-elapsed"><Timer weight="fill" />{itemElapsedText(item)}</small>
                              )}
                          </td>
                          <td className="batch-ratio-cell">{itemRatio(item)}</td>
                          <td className="batch-ops-cell" onClick={(event) => event.stopPropagation()}>
                            <div className="batch-row-ops">
                              {statusAction(item)}
                              {!["running", "revising", "completed", "deleted"].includes(item.status) && (
                                <span className="batch-move">
                                  <button
                                    className="ghost"
                                    disabled={Boolean(busyAction)}
                                    onClick={() => moveItem(item.id, "up")}
                                    title="在队列里上移（调处理顺序）"
                                    aria-label="上移"
                                  >
                                    <CaretUp weight="bold" />
                                  </button>
                                  <button
                                    className="ghost"
                                    disabled={Boolean(busyAction)}
                                    onClick={() => moveItem(item.id, "down")}
                                    title="在队列里下移（调处理顺序）"
                                    aria-label="下移"
                                  >
                                    <CaretDown weight="bold" />
                                  </button>
                                </span>
                              )}
                            </div>
                          </td>
                        </tr>
                        {selectedId === item.id && (
                          <tr className="batch-expanded-row">
                            <td colSpan={5}>{renderSelectedDetail()}</td>
                          </tr>
                        )}
                      </Fragment>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}
    </main>
  );
}
