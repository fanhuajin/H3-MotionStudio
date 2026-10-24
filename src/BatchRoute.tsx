import { createPortal } from "react-dom";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { readJson } from "./api";
import { useVideoPreviewWheel } from "./useVideoPreviewWheel";
import { BatchVideo } from "./BatchVideo";
import { PreviewImage } from "./PreviewImage";
import { ImageViewer } from "./ImageWorkbenchRoute";
import { StudioBot, statusMood } from "./StudioBot";
import { elapsedMs, formatElapsedMs, useNowTick } from "./jobTime";
import {
  ArrowClockwise,
  ArrowUUpLeft,
  Check,
  Circle,
  GearSix,
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
 * 单条**出片用时**：只算「生成最终视频」这一段。
 *
 * 用户 2026-09-20：「时间应该在出片上 备料我不关心时间」—— 截图里**备料中**的那条顶着
 * `00:00`（它刚好是 `batch.currentItemId`），而真正在出片（33%）的那条反而没有时间。
 *
 * 演变：旧计时从条目 `createdAt`（加入队列那一刻）算起，批次放了 22 小时就一直累加
 * （2026-09-15 用户：「现在的时间不对都 22 小时还多了」）→ 改成只算真正干活的阶段
 * （下载 / 备料 / 出片）→ 现在**只看 `video` 里程碑**：正在出片就从它的 `startedAt` 跳到此刻，
 * 已经出完（或失败）就是这一段的总耗时；下载 / 备料 / 等确认**一律返回 null（不显示时间）**。
 */
function itemRenderMs(item: BatchItem, nowMs: number): number | null {
  const video = (item.milestones || []).find((step) => step.id === "video");
  if (!video?.startedAt) return null;
  const started = Date.parse(video.startedAt);
  if (!Number.isFinite(started)) return null;
  const finished = video.finishedAt ? Date.parse(video.finishedAt) : NaN;
  if (Number.isFinite(finished)) return Math.max(0, finished - started);
  if (video.status === "running") return Math.max(0, nowMs - started);
  return null;
}

/**
 * 这一条此刻是不是真的在出片（GPU 在跑最终视频）。
 *
 * 并行编排下 `batch.currentItemId` 可能指着**备料**的那条（出片跑着时同时备下一条，
 * `batch_worker` 的 ③b 会把 currentItemId 改到备料条目上），所以出片计时不能只看它。
 */
function itemIsRendering(item: BatchItem): boolean {
  if (item.status !== "running") return false;
  return item.stage === "video" || item.stage === "deliver";
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
  if (item.status === "running" && ["download", "prepare"].includes(item.stage)) {
    const step = (item.milestones || []).find((row) => row.id === item.stage && row.status === "running");
    return step ? stepPercent(step) : null;
  }
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
  /** 「让 AI 换一张」正在后台生成候选图时为 true（前端据此显示「正在生成…」）。 */
  image_generating?: boolean;
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
  /** 原备料类型内编号；未分配出片号的旧条目也用它作兜底。 */
  kindIndex?: number;
  /** 进入出片区后分配的连续编号；此前仍展示备料序号。 */
  renderIndex?: number;
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
  prepAttemptStartedAt?: string | null;
  coverStage?: "bilibili" | "douyin" | null;
  coverStartedAt?: string | null;
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
  /** 发布链条状态（原声母版 / 音色版 / 字幕版 与「该发哪个文件」），后端只读发布目录算出来 */
  publishChain?: {
    folder: string;
    clean: boolean;
    voice: boolean;
    subtitle: boolean;
    subtitleStale: boolean;
    needsVoice: boolean;
    publishFile: string;
    publishHint: string;
  } | null;
  error?: string | null;
  warning?: string | null;
  prepReady?: boolean;
  renderQueued?: boolean;
}

interface BatchState {
  id: string;
  status: string;
  updatedAt?: string;
  total: number;
  completedCount: number;
  deletedCount?: number;
  currentItemId?: string | null;
  notice?: string;
  pauseRequested?: boolean;
  recoveryActive?: boolean;
  autoRender?: boolean;
  renderRequested?: boolean;
  /** 整批跑完后是否自动关机（页面开关，**默认关闭**） */
  shutdownOnComplete?: boolean;
  shutdownAfterPrep?: boolean;
  /** 时间戳（ISO）：用来算「已运行多久」 */
  createdAt?: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  items: BatchItem[];
}

interface SubmissionResult {
  added: number;
  duplicates: { kind: "singing" | "dance"; inputIndex: number; url: string; reason: string }[];
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
// 列表展示顺序＝执行顺序（用户 2026-09-20：「把跳舞的列表排上边」）：跳舞在前、唱歌在后。
// 只影响这张表怎么排，条目的顺序、kindIndex 编号、发布目录名一概不动。
const KIND_RANK: Record<string, number> = { dance: 0, singing: 1 };

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
/** 出片序号与发布目录一致；未安排的任务仍展示原备料序号。 */
function itemNumber(item: BatchItem): number {
  return Number(item.renderIndex || item.kindIndex || item.index || 0);
}

/** 列表里显示的序号：补足三位，和发布目录名（`027_跳舞_…`）对得上。 */
function itemNumberLabel(item: BatchItem): string {
  return String(itemNumber(item)).padStart(3, "0");
}

function itemTitle(item: BatchItem): string {
  return String(item.ai?.title || "").trim() || item.title || `第 ${itemNumber(item)} 条`;
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
    shutdownAfterPrepOn: parsed.shutdownAfterPrepOn === true,
    autoRenderOn: false,
  };
}

function splitUrls(value: string) {
  return value.split(/[\r\n]+/).map((url) => url.trim()).filter(Boolean);
}

function batchStatusLabel(status: string, stage?: string) {
  // 「正在处理」在表格里会同时出现在**出片**和**备料**两条上 —— 两者并行是设计如此
  // （2026-09-20 起的编排：出片跑着的时候同时备下一条）。所以按 stage 把话说明白，
  // 免得看成"怎么又两个正在处理"。
  if (status === "running") {
    if (stage === "video" || stage === "deliver") return "出片中";
    if (stage === "prepare" || stage === "download") return "备料中";
  }
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
    skipped: "等待区",
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

/** 备料、待出片、异常、等待区和已完成各自分区。 */
type TabId = "prep" | "render" | "exception" | "archive" | "completed";

const TAB_KEY = "h3-motionstudio:batch-tab:v1";

function readActiveTab(): TabId {
  try {
    const saved = localStorage.getItem(TAB_KEY);
    if (saved === "prep" || saved === "render" || saved === "exception" || saved === "archive" || saved === "completed") {
      return saved;
    }
  } catch {
    // 浏览器禁用存储时仍可正常切换分区。
  }
  return "render";
}
const TABS: Array<{ id: TabId; label: string }> = [
  { id: "prep", label: "备料区" },
  { id: "archive", label: "等待区" },
  { id: "render", label: "出片区" },
  { id: "exception", label: "异常" },
  { id: "completed", label: "已完成" },
];
const TAB_MATCH: Record<TabId, (item: BatchItem) => boolean> = {
  prep: (item) => !["completed", "skipped", "failed"].includes(item.status) && !item.prepReady,
  render: (item) => !["skipped", "completed", "failed"].includes(item.status) && Boolean(item.prepReady) && Boolean(item.renderQueued || renderingNow(item)),
  exception: (item) => item.status === "failed",
  completed: (item) => item.status === "completed",
  archive: (item) => item.status === "skipped" || (item.status === "confirmed" && Boolean(item.prepReady) && !item.renderQueued),
};

/** 这一条有没有可看的成片：优先 outputs 快照，其次发布目录里的真实文件（publishChain.clean）。 */
function hasFinalVideo(item: BatchItem): boolean {
  return Boolean(item.outputs?.videoFinal || item.publishChain?.clean);
}

function itemZoneStatus(item: BatchItem): string {
  if (item.status === "skipped" || (item.status === "confirmed" && item.prepReady && !item.renderQueued)) return "等待安排";
  if (item.coverStage && !["failed", "skipped", "completed"].includes(item.status)) return "备料中";
  if (item.status === "confirmed" && !item.prepReady) return "备料中";
  return batchStatusLabel(item.status, item.stage);
}

function prepElapsedMs(item: BatchItem, nowMs: number): number | null {
  const startedAt = item.prepAttemptStartedAt
    || (item.milestones || []).find((step) => step.status === "running" && ["download", "prepare"].includes(step.id))?.startedAt;
  return elapsedMs(startedAt, null, nowMs);
}

function prepPhase(item: BatchItem): string {
  if (item.status === "skipped" || (item.status === "confirmed" && item.prepReady && !item.renderQueued)) return "等待安排";
  if (item.coverStage) return `正在生成${item.coverStage === "bilibili" ? "B站" : "抖音"}封面`;
  const runningStep = (item.milestones || []).find((step) => step.status === "running");
  if (runningStep?.currentNode) return runningStep.currentNode;
  if (item.stage === "download") return "下载源视频";
  if (item.status === "confirmed" && !item.prepReady) {
    const recent = (item.logs || []).slice(-3).map((row) => row.message).join(" ");
    if (recent.includes("抖音封面请求")) return "抖音封面生成中";
    if (recent.includes("B站封面请求")) return "B站封面生成中";
    return "补齐封面";
  }
  return "备料中";
}

/**
 * 页内确认框的内容。**不用 `window.confirm`**：系统原生的灰白弹框跟这套深色壳子完全不是
 * 一个东西，而且没法把「影响哪几条」摊开说清楚（2026-09-15 用户：「弹出框的效果样式改下
 * 现在的不好看」）。重新开始用灰绿，跳过/停止用琥珀色提醒，仅删除用红色。
 */
interface ConfirmAsk {
  title: string;
  detail?: string;
  /** 受影响的具体条目（批量操作时列出来，用户才知道自己到底在动哪几条）。 */
  items?: string[];
  confirmLabel: string;
  cancelLabel?: string;
  tone: "restart" | "warning" | "danger";
}

// Memory-only: navigation can reuse the last view, while a full reload still
// verifies the latest batch before displaying any queue or empty form.
let lastBatchView: { batch: BatchState | null; selectedId: string | null } | undefined;

function BatchDialog({ id, title, subtitle, description, icon, trigger, restoreFocus, onClose, children, footer, className = "" }: {
  id: string; title: string; subtitle: string; description: string; icon: ReactNode;
  trigger: RefObject<HTMLButtonElement | null>; restoreFocus?: RefObject<boolean>;
  onClose: () => void; children: ReactNode; footer: ReactNode; className?: string;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    return () => {
      dialog.close();
      if (restoreFocus?.current !== false) trigger.current?.focus({ preventScroll: true });
    };
  }, [trigger, restoreFocus]);
  return createPortal(<dialog closedby="closerequest" ref={dialogRef} id={id} className={`batch-settings-dialog ${className}`}
    aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`}
    onCancel={(event) => { event.preventDefault(); onClose(); }}
    onKeyDown={(event) => {
      if (event.key !== "Tab") return;
      const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(
        'button:not(:disabled), textarea:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
      )).filter((control) => control.getClientRects().length > 0);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}>
    <header className="batch-settings-header">
      <span className="batch-modal-icon" aria-hidden="true">{icon}</span>
      <div><p>{subtitle}</p><h2 id={`${id}-title`}>{title}</h2></div>
      <button type="button" className="batch-settings-close" autoFocus aria-label={`关闭${title}`} onClick={onClose}><X /></button>
    </header>
    <div className="batch-settings-scroll">
      <p id={`${id}-description`} className="batch-settings-description">{description}</p>
      {children}
    </div>
    <footer className="batch-settings-footer">{footer}</footer>
  </dialog>, document.body);
}

function BatchLoading({ error, onRetry }: { error: string; onRetry: () => void }) {
  return <main className="batch-page batch-loading-page" aria-busy={!error}>
    <header className="batch-header"><div><p className="batch-kicker">SERIAL PRODUCTION</p><h1>批量视频制作</h1><p>先完成整批备料，再逐条出片。</p></div></header>
    <div className="batch-sync-notice" role="status"><SpinnerGap className={error ? undefined : "spin"} />{error ? "暂时无法读取批次" : "正在读取最新批次…"}{error && <button onClick={onRetry}>重新读取</button>}</div>
    {error && <p className="batch-alert">{error}</p>}
    <div className="batch-loading-layout" aria-hidden="true">
      <section className="batch-monitor-toolbar"><div className="batch-skeleton batch-skeleton-setting" /><div className="batch-loading-tabs"><div className="batch-skeleton" /><div className="batch-skeleton" /></div></section>
      <section className="batch-table-card"><div className="batch-loading-tabs"><div className="batch-skeleton" /><div className="batch-skeleton" /><div className="batch-skeleton" /></div>{[0, 1, 2, 3].map(row => <div className="batch-loading-row" key={row}><div className="batch-skeleton" /><div className="batch-skeleton" /><div className="batch-skeleton" /></div>)}</section>
    </div>
  </main>;
}

export function BatchRoute() {
  const initial = useMemo(readInputDraft, []);
  const [singing, setSinging] = useState(initial.singing);
  const [dance, setDance] = useState(initial.dance);
  const [singingOn, setSingingOn] = useState(initial.singingOn);
  const [danceOn, setDanceOn] = useState(initial.danceOn);
  // 「全部完成后自动关机」（默认关闭）：有批次时以批次上的值为准，没有批次时用本地草稿
  const [shutdownOn, setShutdownOn] = useState(initial.shutdownOn);
  const [shutdownAfterPrepOn, setShutdownAfterPrepOn] = useState(initial.shutdownAfterPrepOn);
  const [autoRenderOn, setAutoRenderOn] = useState(initial.autoRenderOn);
  const [shutdown, setShutdown] = useState<ShutdownStatus>({ pending: false });
  const [batch, setBatch] = useState<BatchState | null>(() => lastBatchView?.batch ?? null);
  // 两个弹窗互斥；草稿独立保存在组件和本地存储，关闭不清空。
  const [activeDialog, setActiveDialog] = useState<"add" | "settings" | null>(null);
  const settingsOpen = activeDialog === "settings";
  const addOpen = activeDialog === "add";
  const [addError, setAddError] = useState("");
  const [addResult, setAddResult] = useState<SubmissionResult | null>(null);
  const restoreInputFocus = useRef(true);
  const [listFocusVersion, setListFocusVersion] = useState(0);
  const listSection = useRef<HTMLElement>(null);
  const inputToggle = useRef<HTMLButtonElement>(null);
  const [settingsFeedback, setSettingsFeedback] = useState<{ error: boolean; text: string } | null>(null);
  const settingsTrigger = useRef<HTMLButtonElement>(null);
  const [selectedId, setSelectedId] = useState<string | null>(() => lastBatchView?.selectedId ?? null);
  const [hasSnapshot, setHasSnapshot] = useState(lastBatchView !== undefined);
  // 刷新或重新进入页面时恢复上次选择的分区。
  const [activeTab, setActiveTab] = useState<TabId>(readActiveTab);
  const [videoPreview, setVideoPreview] = useState<{ id: string; title: string; url: string } | null>(null);
  const [imagePreview, setImagePreview] = useState<string | null>(null);
  const videoDialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (videoPreview) videoDialog.current?.showModal();
  }, [videoPreview]);
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
  const [syncError, setSyncError] = useState("");
  useEffect(() => {
    if (hasSnapshot) lastBatchView = { batch, selectedId };
  }, [batch, selectedId, hasSnapshot]);
  const acceptBatchState = useCallback((incoming: BatchState) => {
    setBatch((current) => {
      if (current?.id === incoming.id && current.updatedAt && incoming.updatedAt
        && current.updatedAt > incoming.updatedAt) return current;
      if (current?.id !== incoming.id) return incoming;
      const previous = new Map(current.items.map((item) => [item.id, item]));
      return {
        ...incoming,
        items: incoming.items.map((item) => {
          const before = previous.get(item.id);
          if (!before || before.status !== item.status) return item;
          return {
            ...item,
            prepReady: item.prepReady ?? before.prepReady,
            publishChain: item.publishChain ?? before.publishChain,
          };
        }),
      };
    });
  }, []);
  // 页内确认框（替代 window.confirm）：`askConfirm()` 返回 Promise<boolean>，
  // 所以调用点可以 `if (!(await askConfirm(...))) return;` 继续写同步逻辑。
  const [confirmAsk, setConfirmAsk] = useState<ConfirmAsk | null>(null);
  const confirmResolver = useRef<((ok: boolean) => void) | null>(null);
  const askConfirm = (ask: ConfirmAsk): Promise<boolean> =>
    new Promise<boolean>((resolve) => {
      // 上一个还没答完就再弹一个：先把上一个按「取消」结掉，避免 Promise 永远挂着
      confirmResolver.current?.(false);
      confirmResolver.current = resolve;
      setConfirmAsk(ask);
    });
  const answerConfirm = useCallback((ok: boolean) => {
    const resolve = confirmResolver.current;
    confirmResolver.current = null;
    setConfirmAsk(null);
    resolve?.(ok);
  }, []);
  // Esc = 取消（破坏性操作只能主动点确认键）
  useEffect(() => {
    if (!confirmAsk) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        answerConfirm(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmAsk, answerConfirm]);

  const visibleItems = useMemo(() => {
    const items = batch?.items.filter((item) => item.status !== "deleted") || [];
    // 跳舞排上边（同类型内保持原顺序 = 各自的编号顺序）；Array#sort 是稳定排序。
    return [...items].sort((a, b) => (KIND_RANK[a.kind] ?? 9) - (KIND_RANK[b.kind] ?? 9));
  }, [batch]);
  // 当前标签下显示的条目；展开的详情行也只在当前标签里渲染。
  const filteredItems = useMemo(() => {
    const rows = visibleItems.filter(TAB_MATCH[activeTab]);
    return activeTab === "render"
      ? rows.sort((a, b) => (KIND_RANK[a.kind] ?? 9) - (KIND_RANK[b.kind] ?? 9) || itemNumber(a) - itemNumber(b))
      : rows;
  }, [visibleItems, activeTab]);
  const previewItems = useMemo(
    () => visibleItems.filter((item) => item.status === "completed" && hasFinalVideo(item)),
    [visibleItems],
  );
  const previewIndex = previewItems.findIndex((item) => item.id === videoPreview?.id);
  const switchPreview = useCallback((direction: number) => {
    if (!batch || !videoPreview) return;
    const index = previewItems.findIndex((item) => item.id === videoPreview.id);
    if (index < 0) return;
    const item = previewItems[index + direction];
    if (!item) return;
    setVideoPreview({
      id: item.id,
      title: itemTitle(item),
      url: `/api/batches/${encodeURIComponent(batch.id)}/items/${encodeURIComponent(item.id)}/output/videoFinal`,
    });
  }, [batch, videoPreview, previewItems]);
  useVideoPreviewWheel(Boolean(videoPreview), switchPreview);
  useEffect(() => {
    if (!videoPreview) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        event.stopPropagation();
        switchPreview(event.key === "ArrowUp" ? -1 : 1);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [videoPreview, switchPreview]);
  const selected = useMemo(
    () => visibleItems.find((item) => item.id === selectedId) || null,
    [visibleItems, selectedId],
  );
  const tabCount = (id: TabId) => visibleItems.filter(TAB_MATCH[id]).length;
  const prepBlocked = visibleItems.some((item) =>
    ["pending", "revising", "awaiting_review"].includes(item.status)
    || (item.status === "running" && ["download", "prepare"].includes(item.stage))
    || (item.status === "confirmed" && !item.prepReady),
  );

  const loadLatest = useCallback(async (signal?: AbortSignal) => {
    setSyncError("");
    const response = await fetch("/api/batches/latest", { cache: "no-store", signal });
    if (response.status === 204) {
      if (signal?.aborted) return;
      setBatch(null);
      setSelectedId(null);
    } else {
      if (!response.ok) throw new Error(await responseMessage(response, "无法读取上次批次"));
      const incoming = await readJson<BatchState>(response, "无法读取上次批次");
      if (signal?.aborted) return;
      acceptBatchState(incoming);
    }
    setHasSnapshot(true);
    setLoaded(true);
  }, [acceptBatchState]);

  useEffect(() => {
    const controller = new AbortController();
    loadLatest(controller.signal).catch((reason) => {
      if (!controller.signal.aborted) setSyncError(String(reason.message || reason));
    });
    return () => controller.abort();
  }, [loadLatest]);
  const retryLatest = () => { void loadLatest().catch(reason => setSyncError(String(reason.message || reason))); };

  useEffect(() => {
    localStorage.setItem(INPUT_KEY, JSON.stringify({ singing, dance, singingOn, danceOn, shutdownOn, shutdownAfterPrepOn, autoRenderOn }));
  }, [singing, dance, singingOn, danceOn, shutdownOn, shutdownAfterPrepOn, autoRenderOn]);

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

  // 发布目录监听的状态读取与开关已随勾选框一起移除（见下方渲染处的说明）：
  // 全自动链路里它看不到任何新东西，留着只是每 10 秒白打一次接口。

  useEffect(() => {
    if (!loaded || !batch?.id) return;
    let alive = true;
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${location.host}/api/batches/${batch.id}/ws`);
    socket.onmessage = (event) => { if (alive) acceptBatchState(JSON.parse(event.data)); };
    socket.onerror = () => undefined;
    const poll = window.setInterval(() => {
      fetch(`/api/batches/${batch.id}`, { cache: "no-store" })
        .then((response) => response.ok ? response.json() : null)
        .then((state) => alive && state && acceptBatchState(state))
        .catch(() => undefined);
    }, 3500);
    return () => {
      alive = false;
      socket.close();
      window.clearInterval(poll);
    };
  }, [loaded, batch?.id, acceptBatchState]);

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
    if (id !== activeTab) setSelectedIds(new Set());
    setActiveTab(id);
    try {
      localStorage.setItem(TAB_KEY, id);
    } catch {
      // 存储不可用不影响当前页面切换。
    }
  };

  useEffect(() => {
    if (!listFocusVersion) return;
    const frame = requestAnimationFrame(() => {
      const list = listSection.current;
      if (!list) return;
      list.focus({ preventScroll: true });
      // 只在成功提交后定位一次；轮询和页内切换不抢走用户的位置。
      list.scrollIntoView({ block: "start", behavior: "instant" });
    });
    return () => cancelAnimationFrame(frame);
  }, [listFocusVersion]);

  // 「准备任务」：把填好的链接交给后端，并立即开始准备
  // （下载抖音视频 → 生成人物图与发布文案 → 停在等确认）。
  // 暂停中的批次尊重暂停，只入队不偷跑。
  const showTaskList = () => {
    restoreInputFocus.current = false;
    setActiveDialog(null);
    setListFocusVersion((version) => version + 1);
  };

  const prepare = async () => {
    setBusyAction("start");
    setError("");
    setAddError("");
    setAddResult(null);
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
        : { singingUrls, danceUrls, autoStart, shutdownOnComplete: shutdownOn && !shutdownAfterPrepOn, shutdownAfterPrep: shutdownAfterPrepOn, autoRender: false };
      const response = await fetch(target, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const failure = await response.clone().json().catch(() => null) as {
          detail?: { message?: string; submission?: SubmissionResult };
        } | null;
        if (failure?.detail?.submission) {
          setAddResult(failure.detail.submission);
          setAddError(failure.detail.message || "没有新增任务，请核对重复清单。");
          return;
        }
        throw new Error(await responseMessage(response, append ? "加入队列失败" : "创建队列失败"));
      }
      const state = await readJson<BatchState & { submission?: SubmissionResult }>(response, append ? "加入队列失败" : "创建队列失败");
      acceptBatchState(state);
      const result = state.submission;
      setAddResult(result ?? null);
      // 全部重复时保留原输入和窗口；有成功条目才清空已提交的草稿。
      if (result?.added === 0) return;
      // 链接已经进队列了，输入框清空，直接粘下一批就行
      // （2026-09-15 用户：「准备任务点击之后清空现有的歌曲视频链接和跳舞视频链接」）。
      // 只清**这次真的提交了**的那一类：开关关着的那一类不参与入队，原样留着不动。
      // 失败时不清（catch 里不碰输入框），免得白粘一遍。
      if (singingOn && singingUrls.length) setSinging((current) => current === singing ? "" : current);
      if (danceOn && danceUrls.length) setDance((current) => current === dance ? "" : current);
      // 有重复项时留在添加窗口展示清单，用户看完后再返回任务列表。
      if (!result?.duplicates.length) showTaskList();
      // 新建批次也不自动展开任何一条：列表默认全收起，由用户自己点开要看的那条
      // （2026-09-15 用户：「启动页面的时候 列表默认都是收起来的 由我自己点击要查看哪个」）。
      // 一条都没新增（全被判重过滤）时必须说清楚，否则点了看起来像没反应
      if (append && (state.items?.length || 0) <= (batch?.items.length || 0)) {
        setNotice("这些链接都已经在队列里了（重复链接自动跳过），这次没有新增任务。");
      } else {
        setNotice("");
      }
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(`${message}　〔POST ${target}〕`);
      setAddError(message);
    } finally {
      setBusyAction("");
    }
  };

  const call = async (action: string, method = "POST", body?: object) => {
    if (!batch || busyAction) return;
    const settingAction = ["auto-render", "shutdown-on-complete", "shutdown-after-prep"].includes(action);
    if (settingAction) setSettingsFeedback(null);
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
      acceptBatchState(await readJson<BatchState>(response, "操作失败"));
      if (settingAction) setSettingsFeedback({ error: false, text: "设置已保存并生效" });
    } catch (reason) {
      // 报错要把**所有能给的定位信息**都带上：后端 detail + HTTP 状态 + 具体端点，
      // 否则用户只能看到一句「操作失败」，没法反馈也没法自己排查。
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(`${message}　〔${method} ${endpoint}〕`);
      if (settingAction) setSettingsFeedback({ error: true, text: `保存失败：${message}。请核对当前状态后重试。` });
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

  const retryFailedItem = async (item: BatchItem) => {
    await itemCallFor(item, "retry");
  };

  /** 手动把一条停下来的任务标记为已完成（只改状态，不重新生成）。 */
  const completeItem = async (item: BatchItem) => {
    const ok = await askConfirm({
      title: "标记为已完成？",
      detail:
        "只改状态，不会生成或删除文件。发布目录里已经有 最终成片.mp4 会自动登记；"
        + "没有的话，完成页不会显示成片预览。",
      items: [`第 ${itemNumber(item)} 条 · ${itemTitle(item)}`],
      confirmLabel: "标记已完成",
      tone: "restart",
    });
    if (ok) await itemCallFor(item, "complete");
  };

  /** 批量操作：勾选后批量重新开始 / 跳过 / 删除（后端逐个校验，出片仍一条一条来）。 */
  const batchOp = async (
    action: "retry-many" | "skip-many" | "delete-many",
    ask?: ConfirmAsk,
  ) => {
    if (!batch || selectedIds.size === 0) return;
    if (ask && !(await askConfirm(ask))) return;
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
      acceptBatchState(await readJson<BatchState>(response, "批量操作失败"));
      setSelectedIds(new Set());
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(`${message}　〔POST ${endpoint}〕`);
    } finally {
      setBusyAction("");
    }
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
    if (!batch) {
      setSettingsFeedback({ error: false, text: "已记住此设置，点击准备任务时应用" });
      return;
    }
    void call("shutdown-on-complete", "POST", { enabled: next }).then(() => {
      fetch("/api/system/shutdown", { cache: "no-store" })
        .then((response) => (response.ok ? response.json() : null))
        .then((data: ShutdownStatus | null) => data && setShutdown(data))
        .catch(() => undefined);
    });
  };

  const toggleShutdownAfterPrep = () => {
    const next = !(batch ? Boolean(batch.shutdownAfterPrep) : shutdownAfterPrepOn);
    setShutdownAfterPrepOn(next);
    if (next) { setAutoRenderOn(false); setShutdownOn(false); }
    if (batch) void call("shutdown-after-prep", "POST", { enabled: next });
    else setSettingsFeedback({ error: false, text: "已记住此设置，点击准备任务时应用" });
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
  const prepShutdownSwitchOn = batch ? Boolean(batch.shutdownAfterPrep) : shutdownAfterPrepOn;
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
  // 画布比例：没开始出片的条目能改（审核区是主要入口，见 renderSettings）
  const selectedRatio = selected ? itemRatio(selected) : DEFAULT_RATIO.singing;
  // 出了审核点（已加入队列/出片中/已完成）以后，同一屏信息继续显示，但只读：
  // 用户 2026-09-13「等待你的确认 的信息在加入队列之后也要展示，只是不允许修改了」。
  const atReview = selected?.status === "awaiting_review";
  const changeRatio = (ratio: CanvasRatio) => {
    if (!selected || selectedRatio === ratio) return;
    void itemCall("ratio", "POST", { ratio });
  };
  // 迁移模式：动作迁移（默认）/ 人物替换 —— 2026-09-15 用户想试人物替换的效果
  const selectedMigrateMode: MigrateMode =
    selected?.ai?.migrate_mode === "replacement" ? "replacement" : "animation";
  const changeMigrateMode = (mode: MigrateMode) => {
    if (!selected || selectedMigrateMode === mode) return;
    void itemCall("migrate-mode", "POST", { mode });
  };

  /** 出片前设置：画布比例与迁移模式。 */
  const renderSettings = (item: BatchItem) => {
    const ratio = itemRatio(item);
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
  // 只保留「这一屏其它地方看不到」的字段：类型/状态在头部与状态徽章里，不再重复
  // （2026-09-18 用户：「列表展开信息简略一下」）。第三项是可选的跳转地址：
  // 源作品文案与「打开抖音原链接」原来挂在源视频卡片上，用户要求移进这里
  // （2026-09-18 用户：「你可以放到本条全部信息里面」）。
  const reviewCoreFacts = (ai: BatchAI, item: BatchItem): Array<[string, string, string?]> => {
    const image = String(ai.reference_image_path || "");
    const facts: Array<[string, string, string?]> = [];
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
      ["识别歌曲", String(ai.song_name || "")],
      ["歌曲情绪", String(ai.song_mood || "")],
      ["造型来源", styleSourceLabel(ai.style_source)],
      ["候选人物图", image ? image.split(/[\\/]/).pop() || image : "还没有（等你上传 GPT 生成的图）"],
      ["候选图版本", `第 ${(item.revision || 0) + 1} 版`],
      ["审核放行", item.status === "awaiting_review" ? "还没放行" : "已放行"],
      ["创建时间", formatLogTime(item.createdAt)],
    );
    // 本机换源后没有抖音出处了，不给这个链接，免得用户以为没换成功
    if (item.sourceOrigin !== "local" && item.url) {
      facts.push(["抖音原链接", "打开抖音原链接", item.url]);
    }
    return facts;
  };
  const reviewAdvancedFacts = (item: BatchItem): Array<[string, string]> => [
    ["条目 id", item.id],
    ["下载子任务", String(item.downloadJobId || "")],
    ["视频子任务", String(item.videoJobId || "")],
    ["源文件名", String(item.sourceName || "")],
    ["源文件路径", String(item.sourcePath || "")],
    ["最近更新", formatLogTime(item.updatedAt)],
  ];

  // 已运行时间：批次还在跑就实时跳秒；已结束显示总耗时。
  const batchLive = Boolean(batch && !batch.finishedAt && !["completed", "cancelled", "failed"].includes(batch.status));
  // 当前备料和出片条目各自实时计时；排队与已完成条目不跳秒。
  const queueLive = visibleItems.some(
    (item) => !item.finishedAt && ["running", "revising", "confirmed"].includes(item.status),
  );
  const batchNowTick = useNowTick(batchLive || queueLive);

  // 本条出片用时：只算「生成最终视频」这一段（备料时间不算，见 itemRenderMs）。
  const itemElapsedMs = selected ? itemRenderMs(selected, batchNowTick) : null;
  /** 表格行里的出片用时（正在出片就跳秒；刚出完那一下还能看到这一条的总时长）。 */
  const itemRenderText = (item: BatchItem): string => {
    const ms = itemRenderMs(item, batchNowTick);
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
      acceptBatchState(await readJson<BatchState>(response, "图片上传失败"));
      setImageToken(Date.now());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction("");
    }
  };

  // 该条当前是否正在让 AI 生成候选图（后台异步，轮询到后显示「正在生成…」）。
  const generatingImage = Boolean(selected?.ai?.image_generating);

  // 异步生成结束（image_generating 从 true → false）时刷新图片，显示新生成的那张。
  const prevGenerating = useRef(Boolean(selected?.ai?.image_generating));
  useEffect(() => {
    const now = Boolean(selected?.ai?.image_generating);
    if (prevGenerating.current && !now) {
      setImageToken(Date.now());
    }
    prevGenerating.current = now;
  }, [selected?.ai?.image_generating, selected?.id]);


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
      acceptBatchState(await readJson<BatchState>(response, "替换源视频失败"));
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
  const renderedCount = batch?.items.filter(item => item.status === "completed").length || 0;
  const skippedCount = batch?.items.filter(item => item.status === "skipped").length || 0;
  const batchFinishedWithSkips = batch?.status === "completed" && skippedCount > 0;

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
  // 能批量「重新开始」的只有已经跑完一轮的（失败 / 等待区 / 已出片），
  // 跟单条「重试 / 重新开始」按钮同一条规则。
  const anyRestartable = selectedItems.some((item) =>
    ["failed", "skipped", "completed"].includes(item.status),
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
          <button className="batch-warning" onClick={() => void itemCallFor(item, "skip")}><X />跳过</button>
        </>
      );
    }
    if (item.status === "pending") {
      return <button className="batch-warning" onClick={() => void itemCallFor(item, "skip")}><X />跳过</button>;
    }
    if (["confirmed", "running", "revising"].includes(item.status)) {
      return (
        <button
          className="batch-warning"
          onClick={() =>
            void (async () => {
              // 只有真的在出片时才需要确认（会作废一半进度）；只是放行了还没轮到出片的，
              // 取消不动任何已生成的内容，直接执行就好，不再多弹一次提示（2026-09-15 用户要求）。
              if (
                renderingNow(item)
                && !(await askConfirm({
                  title: "跳过这一条任务？",
                  detail: "已经生成到一半的进度会作废。跳过后进入等待区，可以点「开始出片」再次安排。",
                  items: [`第 ${itemNumber(item)} 条 · ${itemTitle(item)}`],
                  confirmLabel: "跳过任务",
                  tone: "warning",
                }))
              ) {
                return;
              }
              await itemCallFor(item, "skip");
            })()
          }
          title="跳过这一条任务并移入等待区；之后可以再次开始出片"
        >
          <X weight="bold" />跳过
        </button>
      );
    }
    if (item.status === "failed") {
      return (
        <>
          <button title="重新请求补齐缺失图片（可能产生费用），已有图片直接沿用" onClick={() => void retryFailedItem(item)}><ArrowClockwise />重试</button>
          <button title="只把状态改成已完成，不重新生成" onClick={() => void completeItem(item)}><Check weight="bold" />标记已完成</button>
        </>
      );
    }
    if (TAB_MATCH.archive(item)) {
      return <><button disabled={Boolean(busyAction) || prepBlocked}
        title={prepBlocked ? "备料区全部完成后可以开始出片" : "沿用已有素材，开始这一条出片"}
        onClick={() => void call("render", "POST", { itemIds: [item.id] })}><Play />开始出片</button>
        <button title="只把状态改成已完成，不重新生成" onClick={() => void completeItem(item)}><Check weight="bold" />标记已完成</button></>;
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
            <p>第 {itemNumber(item)} 条 · {item.kind === "singing" ? "歌曲视频" : "跳舞视频"}</p>
            <h2>{itemTitle(selected)}</h2>
            <div className="batch-detail-meta">
              <a href={item.url} target="_blank" rel="noreferrer">查看原抖音链接</a>
              {/* 本条出片用时：只算「生成最终视频」这一段，备料 / 排队 / 等确认不算（只显示数字，不加标签字） */}
              {itemElapsedMs !== null && (
                <span className="batch-timer" title="本条出片用时（只算生成最终视频这一段，不含下载 / 备料 / 排队 / 等确认）">
                  <Timer weight="fill" /> {formatElapsedMs(itemElapsedMs)}
                </span>
              )}
            </div>
          </div>
          <div className="batch-item-actions">
            {/* 失败 / 等待区 / 已出片：都能直接再出一版（同一接口，已确认过的只重跑出片） */}
            {TAB_MATCH.archive(item) && (
              <button disabled={Boolean(busyAction) || prepBlocked}
                title={prepBlocked ? "备料区全部完成后可以开始出片" : "沿用已有素材，开始这一条出片"}
                onClick={() => void call("render", "POST", { itemIds: [item.id] })}><Play />开始出片</button>
            )}
            {item.status === "failed" && (
              <button
                onClick={() =>
                  void retryFailedItem(item)
                }
                title={hasImage ? "沿用已有的候选图与文案，只重跑出片" : "从下载抖音视频与备料开始重做这一条"}
              >
                <ArrowClockwise />
                重试
              </button>
            )}
            {["failed", "skipped", "confirmed"].includes(item.status) && (
              <button
                onClick={() => void completeItem(item)}
                title="只把状态改成已完成，不重新生成；发布目录里已有的最终成片会自动登记"
              >
                <Check weight="bold" />标记已完成
              </button>
            )}
            {canReopen && (
              <button
                onClick={() =>
                  void (async () => {
                    // 只有真的在出片的条目才会作废进度；`confirmed`（已放行、还没轮到）
                    // 退回去只是把放行作废，不碰任何正在跑的生成，不用吓唬用户。
                    if (
                      renderingNow(item)
                      && !(await askConfirm({
                        title: "回到确认？",
                        detail: "这一条正在出片，会先安全取消当前出片，已生成到一半的进度作废。旧成片与发布目录不会被删。",
                        items: [`第 ${itemNumber(item)} 条 · ${itemTitle(item)}`],
                        confirmLabel: "回到确认",
                        tone: "warning",
                      }))
                    ) {
                      return;
                    }
                    await itemCall("reopen-review");
                  })()
                }
                title={
                  renderingNow(item)
                    ? "回到「等待你的确认」：会先安全取消这一条当前的出片"
                    : "回到「等待你的确认」，可以换图或改比例后重新确认（不会影响其它条目）"
                }
              >
                <ArrowUUpLeft />回到确认
              </button>
            )}
            <button
              className="danger"
              onClick={() =>
                void (async () => {
                  const ok = await askConfirm({
                    title: `删除第 ${itemNumber(item)} 条？`,
                    detail: "正在跑的步骤会被安全取消，已经生成的文件会保留。这一条不再占队列位置。",
                    items: [`${item.kind === "singing" ? "歌曲" : "跳舞"} · ${itemTitle(item)}`],
                    confirmLabel: "删除这一条",
                    tone: "danger",
                  });
                  if (ok) await itemCall("", "DELETE");
                })()
              }
            >
              <Trash />删除这一条
            </button>
          </div>
        </div>

        {/* 顶部同一行：本条源视频 | 候选人物图（2026-09-18 用户：「本条源视频 和 候选人物图
            放到同一行 图片不需要那么大 信息不需要那么多」）——左右两格并排，图小、信息精简。
            源视频的作用照旧：确认前认出「这是哪条抖音视频」，认出来不对就地替换。 */}
        <div className="batch-media-row">
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
              <BatchVideo
                key={item.sourcePath}
                src={`/api/batches/${batch!.id}/items/${item.id}/stage/source`}
              />
            ) : (
              <div className="batch-source-empty">还没下载源视频<br />下载完成后这里可以直接播放核对</div>
            )}
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

          <section className="batch-candidate-panel">
            <div className="batch-panel-title">
              <span>候选人物图{item.ai ? ` · 第 ${(item.revision || 0) + 1} 版` : ""}</span>
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
            <div className="batch-review-image">
              {hasImage ? (
                <>
                  <button type="button" className="batch-image-view-trigger"
                    aria-label="放大查看候选人物图" title="点击放大查看"
                    onClick={() => setImagePreview(`/api/batches/${batch!.id}/items/${item.id}/image?v=${imageToken || item.revision || 0}`)}>
                  <PreviewImage
                    src={`/api/batches/${batch!.id}/items/${item.id}/image?v=${imageToken || item.revision || 0}`}
                    alt="候选人物图"
                    retry={false}
                  />
                  </button>
                  {generatingImage && (
                    <div className="batch-image-loading">
                      <SpinnerGap className="spin" />
                      <strong>正在重新生成…</strong>
                      <small>ChatGPT 出图中，约 1~3 分钟</small>
                    </div>
                  )}
                </>
              ) : generatingImage ? (
                <div className="batch-image-loading">
                  <SpinnerGap className="spin" />
                  <strong>正在生成候选人物图…</strong>
                  <small>ChatGPT 静默出图中，约 1~3 分钟</small>
                </div>
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
                <p className="batch-empty">这一条还没有候选人物图。</p>
              )}
            </div>
          </section>
        </div>

        {item.status === "pending" && (
          <p className="field-note">
            {batch?.status === "paused" || batch?.pauseRequested
              ? "这一条还在排队：批次处于暂停，点表格上方的「继续」后才会开始（下载抖音视频 → 生成人物图素材与发布文案 → 停下来等你确认）。"
              : "这一条还在排队：轮到它就会自动下载抖音视频、生成人物图素材与发布文案，然后停下来等你确认。"}
          </p>
        )}

        {/* 这一屏的信息在**加入队列之后也要继续显示**（用户 2026-09-13），
            只是出了审核点就不给改了：上传/换图与设置开关只在这里是 awaiting_review 时可用。
            候选人物图已并到上方与「本条源视频」同一行，这里只剩文案与确认；
            首次出图期间（还没有文案）loading 显示在候选人物图那一格里。 */}
        {item.ai?.title && (
          <section className="batch-review">
            <div className="batch-review-copy">
              <div className="batch-review-title">
                <span>{atReview ? "等待你的确认" : `本条信息 · ${itemZoneStatus(item)}${editable ? "" : "（只读）"}`}</span>
                <small>{atReview ? `按 ${selectedRatio} 出片 · 确认前不会启动 ComfyUI` : `按 ${selectedRatio} 出片`}</small>
              </div>

              {item.ai.song_name && <p className="batch-song-name">识别歌曲：{item.ai.song_name}</p>}
              <label><span>标题</span><p>{item.ai.title}</p></label>
              <label><span>简介</span><p>{item.ai.introduction}</p></label>
              <label><span>标签</span><div className="batch-tags">{item.ai.tags.map((tag) => <i key={tag}>#{tag.replace(/^#/, "")}</i>)}</div></label>
              {/* 确认这一屏的信息照旧保留，但整块默认折叠（2026-09-18 用户：「列表展开信息
                  简略一下」）：字段没删，只是不再一上来就堆满一屏。排障字段列在后面。 */}
              <details className="batch-collapse">
                <summary>本条全部信息<small>含排障字段</small></summary>
                <div className="batch-collapse-body">
                  <dl className="batch-facts">
                    {reviewCore.map(([label, value, href]) => (
                      <div key={label}>
                        <dt>{label}</dt>
                        <dd>
                          {href
                            ? <a href={href} target="_blank" rel="noreferrer">{value}</a>
                            : (value || "—")}
                        </dd>
                      </div>
                    ))}
                  </dl>
                  <div className="batch-facts-divider">排障字段</div>
                  <dl className="batch-facts">
                    {reviewAdvanced.map(([label, value]) => (
                      <div key={label}>
                        <dt>{label}</dt>
                        <dd>{value || "—"}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              </details>
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
          {/* 真实的 H3 分段 / 去字幕 / 二采进度摆在标题右侧，不展开折叠区也能看见
              （2026-09-18 用户：「H3 分段 5/5 二采 21/41 这些全部放到当前条目的右侧」）。 */}
          <div className="batch-panel-title">
            <span>当前条目进度</span>
            <div className="batch-progress-head">
              <div className="batch-child-badges">
                {item.childJob?.currentSegment && <i>H3 分段 {item.childJob.currentSegment}/{item.childJob.estimatedSegments}</i>}
                {item.childJob?.cleanBatch && <i>去字幕 {item.childJob.cleanBatch}/{item.childJob.cleanBatches}</i>}
                {item.childJob?.upscaleBatch && <i>二采 {item.childJob.upscaleBatch}/{item.childJob.upscaleBatches}</i>}
              </div>
              <small>{itemZoneStatus(item)}</small>
            </div>
          </div>
          <div className="batch-steps">
            {item.milestones.map((step) => {
              const percent = stepPercent(step);
              // 只给「生成最终视频」这一格显示已跑多久（2026-09-20 用户：「时间应该在出片上
              // 备料我不关心时间」）；下载 / 备料即使没有百分比也不显示时间。
              const elapsed = step.id === "video" ? elapsedLabel(step.startedAt) : "";
              return (
                <div className={`batch-step ${step.status}`} key={step.id}>
                  <span className="batch-step-icon">{stepIcon(step.status)}</span>
                  <div><strong>{step.label}</strong><small>{step.currentNode || step.subtitle}</small></div>
                  {step.status === "running" && (percent !== null
                    ? <em>{percent}%</em>
                    : (
                      <em className="indeterminate" title="该步骤没有节点级进度，按出片耗时显示">
                        进行中{elapsed ? ` · ${elapsed}` : ""}
                      </em>
                    ))}
                </div>
              );
            })}
          </div>
        </section>

        {stageEntries.length > 0 && (
          <details className="batch-collapse">
            <summary>生成阶段产物<small>{stageEntries.length} 个 · 只读，点开即看</small></summary>
            <div className="batch-collapse-body">
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
            </div>
          </details>
        )}

        {item.childJob && (
          <details className="batch-collapse">
            <summary>
              真实生成流程
              <small>{item.childJob.currentNodeTitle || batchStatusLabel(item.childJob.status)}</small>
            </summary>
            <div className="batch-collapse-body">
              <div className="batch-steps compact">
                {(item.childJob.milestones || []).map((step) => (
                  <div className={`batch-step ${step.status}`} key={step.id}>
                    <span className="batch-step-icon">{stepIcon(step.status)}</span>
                    <div><strong>{step.label}</strong><small>{step.subtitle}</small></div>
                  </div>
                ))}
              </div>
            </div>
          </details>
        )}

        {/* 成品区整个去掉（2026-09-13 用户要求）：交付照做（文件照样写进发布目录），
            页面上不再放这块面板。成片仍可在「生成阶段产物」里点开看。 */}

        {/* 发布链条（2026-09-20 用户定的链条）：原声母版 → 音色版 → 字幕版（发布版）。
            后端 `publish_chain()` 只读发布目录里的文件时间，标题版比输入旧会标「已过期」。 */}
        {item.publishChain?.folder && (
          <div className="batch-chain">
            <span className="batch-chain-title">发布链条</span>
            <span className={item.publishChain.clean ? "on" : ""}>原声母版 {item.publishChain.clean ? "✓" : "—"}</span>
            {item.publishChain.needsVoice && (
              <span className={item.publishChain.voice ? "on" : ""}>音色版 {item.publishChain.voice ? "✓" : "—"}</span>
            )}
            <span className={item.publishChain.subtitleStale ? "stale" : item.publishChain.subtitle ? "on" : ""}>
              字幕版 {item.publishChain.subtitle ? (item.publishChain.subtitleStale ? "⚠ 已过期" : "✓") : "—"}
            </span>
            <strong>{item.publishChain.publishHint}</strong>
          </div>
        )}

        {(item.error || item.warning) && <div className="batch-alert"><WarningCircle weight="fill" />{item.error || item.warning}</div>}
      </div>
    );
  };

  const resumeOnly = Boolean(batch && batch.status === "paused" && !batch.recoveryActive);

  const renderQueueActions = () => (
        <div className="batch-entry-actions">
          {/* 2026-10-18 用户：「添加任务放到运行设置前边」「已安排出片样式和前边按钮样式保持一致」
              —— 整组都是灰绿描边按钮，不再单独用实心的 .batch-primary。 */}
          <button type="button" className="batch-add-toggle" ref={inputToggle}
            aria-haspopup="dialog" aria-expanded={addOpen} aria-controls="batch-add-dialog"
            onClick={() => { restoreInputFocus.current = true; setAddError(""); setActiveDialog("add"); }}>
            <UploadSimple />添加任务{(singing.trim() || dance.trim()) && <span className="batch-draft-badge">草稿 {splitUrls(singing).length + splitUrls(dance).length}</span>}
          </button>
          <button type="button" className="batch-add-toggle batch-settings-trigger" ref={settingsTrigger}
            aria-haspopup="dialog" aria-expanded={settingsOpen} aria-controls="batch-running-settings-dialog"
            title={`${prepShutdownSwitchOn ? "备料后关机" : shutdownSwitchOn ? "全部完成后关机" : "自动关机关闭"}`}
            onClick={() => { setSettingsFeedback(null); setActiveDialog("settings"); }}>
            <GearSix />运行设置{(prepShutdownSwitchOn || shutdownSwitchOn) && <span className="batch-settings-badge"><Power />{prepShutdownSwitchOn ? "备料后关机" : "完成后关机"}</span>}
          </button>
          {resumeOnly && <button type="button" className="batch-add-toggle" disabled={Boolean(busyAction)} onClick={() => void call("resume")}><Play />继续任务</button>}



        </div>
  );

  if (!hasSnapshot) {
    return <BatchLoading error={syncError} onRetry={retryLatest} />;
  }

  return (
    <main className="batch-page" aria-busy={!loaded}>
      {!loaded && <div className="batch-sync-notice" role="status"><SpinnerGap className={syncError ? undefined : "spin"} />{syncError ? "同步失败，当前显示上次内容" : "正在同步最新批次…"}{syncError && <button onClick={retryLatest}>重新同步</button>}</div>}
      <div className="batch-content" inert={!loaded}>
      <header className="batch-header">
        <div>
          <p className="batch-kicker">SERIAL PRODUCTION</p>
          <h1>批量视频制作</h1>
          <p>先备料，再逐条出片；已有文件直接沿用，封面在成片后补齐。</p>
        </div>
        {batch && (
          <div className={`batch-status-pill ${batch.status}`}>
            <StudioBot mood={batchFinishedWithSkips ? "waiting" : statusMood(batch.status)} variant={batch.status === "paused" ? "sleep" : undefined} size={38} />
            {batchFinishedWithSkips ? "本轮已结束" : batchStatusLabel(batch.status)} · 已完成 {renderedCount}/{effectiveTotal}{skippedCount > 0 ? ` · 等待区 ${skippedCount}` : ""}
          </div>
        )}
      </header>

      {!batch && <section className="batch-monitor-toolbar" aria-label="任务操作"><div className="batch-toolbar-summary"><strong>开始第一批制作</strong><small>添加视频链接，准备素材后逐条出片</small></div>{renderQueueActions()}</section>}



      {error && <div className="batch-alert"><WarningCircle weight="fill" />{error}</div>}
      {busyAction && <div className="batch-alert info" role="status"><SpinnerGap className="spin" />正在处理操作，请稍候…</div>}
      {!error && notice && <div className="batch-alert info"><ListChecks weight="fill" />{notice}</div>}

      {/* 关机倒计时：这是**会真的关机**的状态，必须显眼并且能一键撤销 */}
      {shutdown.pending && (
        <div className="batch-alert shutdown">
          <Power weight="fill" />
          <span>
            {batch?.shutdownAfterPrep ? "整批备料已完成" : "所有任务已完成"}，<b>{shutdownSecondsLeft}</b> 秒后自动关机。
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

      {!batch && <section className="batch-first-task"><ListChecks /><h2>从添加第一条视频开始</h2><p>点击上方「添加任务」，粘贴跳舞或歌曲视频链接。准备好的任务会显示在这里。</p></section>}

      {batch && (
        <section className="batch-workspace" ref={listSection} tabIndex={-1} aria-label="任务列表">
          <div className="batch-table-card">

            <div className="batch-table-head batch-list-toolbar">
              {/* 2026-10-18 用户：「这个不要」——列表只留 Tab 与操作按钮，不再单独放「任务队列」标题块。 */}
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
              {renderQueueActions()}
            </div>

            {activeTab === "render" && tabCount("prep") > 0 && (
              <div className="batch-render-context" role="status">
                <span><ListChecks />{tabCount("prep")} 条任务正在备料区准备，备齐后会进入这里。{tabCount("render") === 0 ? "任务已在队列中，无需重复添加。" : "可先关注下方出片进度。"}</span>
                <button type="button" onClick={() => switchTab("prep")}>查看备料区</button>
              </div>
            )}

            {/* 批量操作栏：勾选任意一条后出现 */}
            {selectedIds.size > 0 && (
              <div className="batch-bulk-bar">
                <span>已选 {selectedIds.size} 条</span>
                {activeTab !== "render" && activeTab !== "completed" && <>
                <button
                  disabled={Boolean(busyAction) || (activeTab === "archive" ? prepBlocked || !selectedItems.some(TAB_MATCH.archive) : !anyRestartable)}
                  onClick={() => activeTab === "archive"
                    ? void call("render", "POST", { itemIds: selectedItems.filter(TAB_MATCH.archive).map(item => item.id) })
                    : void batchOp("retry-many", {
                      title: `重新开始选中的 ${selectedIds.size} 条？`,
                      detail: "已经出过片的会重跑一遍生成链路，新成片会覆盖发布目录里的同名文件；"
                        + "已有图片直接沿用，缺失图片会重新请求补齐，可能产生新的图片费用；备料齐全后再出片。正在出片的不会被重开。",
                      items: selectedItems.map((item) => `第 ${itemNumber(item)} 条 · ${batchStatusLabel(item.status, item.stage)} · ${itemTitle(item)}`),
                      confirmLabel: "重新开始这 " + selectedIds.size + " 条",
                      tone: "restart",
                    })
                  }
                  title={
                    activeTab === "archive" ? (prepBlocked ? "备料区全部完成后可以开始出片" : "开始勾选任务的出片，按队列逐条制作") : anyRestartable
                      ? "把勾选里「已失败 / 等待区 / 已出片」的条目重新跑一遍（出片仍一条一条来）"
                      : "勾选里没有可重新开始的条目（只有失败 / 等待区 / 已出片的能重开）"
                  }
                >
                  {activeTab === "archive" ? <><Play />开始出片</> : <><ArrowClockwise />重新开始</>}
                </button>
                </>}
                {activeTab === "render" && <button
                  className="batch-warning"
                  disabled={Boolean(busyAction)}
                  onClick={() => void batchOp("skip-many", selectedItems.some(renderingNow) ? {
                    title: `跳过选中的 ${selectedItems.length} 条任务？`,
                    detail: "正在出片的任务会安全取消，未完成的出片进度会作废；已生成的文件保留。跳过后进入等待区，可以再次安排出片。",
                    items: selectedItems.map((item) => `第 ${itemNumber(item)} 条 · ${itemTitle(item)}`),
                    confirmLabel: "跳过所选任务",
                    tone: "warning",
                  } : undefined)}
                  title="跳过所选任务并移入等待区，之后可以再次安排出片"
                ><X />跳过</button>}
                <button
                  className="danger"
                  disabled={!anyDeletable || Boolean(busyAction)}
                  onClick={() =>
                    void batchOp("delete-many", {
                      title: `删除选中的 ${selectedIds.size} 条？`,
                      detail: "正在跑的步骤会被安全取消，已经生成的文件会保留。",
                      items: selectedItems.map((item) => `第 ${itemNumber(item)} 条 · ${itemTitle(item)}`),
                      confirmLabel: "删除这 " + selectedIds.size + " 条",
                      tone: "danger",
                    })
                  }
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
                    <th className="batch-index-head">序号</th>
                    <th>任务</th>
                    <th>状态</th>
                    <th>比例</th>
                    <th className="right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredItems.length === 0 ? (
                    <tr>
                      <td colSpan={6}>
                        <div className="batch-empty">
                          {activeTab === "render" ? <>
                            <p>{tabCount("prep") > 0 ? "正在准备素材，出片任务会显示在这里。" : "当前没有待出片任务。"}</p>
                            {tabCount("completed") > 0 && <button type="button" className="batch-add-toggle" onClick={() => switchTab("completed")}>查看已完成 {tabCount("completed")} 条</button>}
                            {tabCount("exception") > 0 && <button type="button" className="batch-add-toggle" onClick={() => switchTab("exception")}>处理异常 {tabCount("exception")} 条</button>}
                          </> : <p>这个状态下还没有任务。</p>}
                        </div>
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
                              aria-label={`选择第 ${itemNumber(item)} 条`}
                              checked={selectedIds.has(item.id)}
                              onChange={() => toggleSelect(item.id)}
                              onClick={(event) => event.stopPropagation()}
                            />
                          </td>
                          <td className="batch-index" title={`第 ${itemNumber(item)} 条（发布目录编号）`}>
                            {itemNumberLabel(item)}
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
                            <div className="batch-status-line">
                            <span className={`batch-status ${item.status}`}>
                              {itemZoneStatus(item)}
                              {/* 进度直接在列表这一行显示，不用点开（2026-09-15 用户：「除了时间
                                  进入也同步的列表中 像现在的 17% 这样的 我有时候不想点开来看」）；
                                  只认真实非零进度，0/null 不显示数字（进度不得造假）。 */}
                              {itemPercent(item) !== null && (
                                <b className="batch-status-percent">{itemPercent(item)}%</b>
                              )}
                            </span>
                            {item.childJob?.currentSegment && item.childJob.estimatedSegments && (
                              <small className="batch-seg">H3 分段 {item.childJob.currentSegment}/{item.childJob.estimatedSegments}</small>
                            )}
                            {item.childJob?.upscaleBatch && item.childJob.upscaleBatches && (
                              <small className="batch-seg">二采分段 {item.childJob.upscaleBatch}/{item.childJob.upscaleBatches}</small>
                            )}
                            {batch?.status === "running"
                              && item.coverStage
                              && batch.currentItemId === item.id && (
                                <small className="batch-elapsed">
                                  <Timer weight="fill" />{prepPhase(item)} · 已用 {formatElapsedMs(elapsedMs(item.coverStartedAt, null, batchNowTick) || 0)}
                                </small>
                              )}
                            {batch?.status === "running"
                              && batch.currentItemId === item.id
                              && !item.prepReady
                              && item.status === "running"
                              && !item.coverStage
                              && prepElapsedMs(item, batchNowTick) !== null && (
                                <small className="batch-elapsed">
                                  <Timer weight="fill" />备料已用 {formatElapsedMs(prepElapsedMs(item, batchNowTick)!)} · {prepPhase(item)}
                                </small>
                              )}
                            {/* 出片只给当前正在生成视频的条目计时。 */}
                            {batch?.status === "running"
                              && itemRenderText(item)
                              && (itemIsRendering(item) || item.id === batch?.currentItemId) && (
                                <small className="batch-elapsed"><Timer weight="fill" />{itemRenderText(item)}</small>
                              )}
                            </div>
                          </td>
                          <td className="batch-ratio-cell">{itemRatio(item)}</td>
                          <td className="batch-ops-cell" onClick={(event) => event.stopPropagation()}>
                            <div className="batch-row-ops" inert={Boolean(busyAction)} aria-busy={Boolean(busyAction)}>
                              {item.status === "completed" && hasFinalVideo(item) && (
                                <button
                                  className="batch-add-toggle"
                                  onClick={() => setVideoPreview({
                                    id: item.id,
                                    title: itemTitle(item),
                                    url: `/api/batches/${encodeURIComponent(batch.id)}/items/${encodeURIComponent(item.id)}/output/videoFinal`,
                                  })}
                                  aria-label={`查看成片：${itemTitle(item)}`}
                                >
                                  <Play />查看成片
                                </button>
                              )}
                              {statusAction(item)}
                              {item.status === "completed" && (
                                <button
                                  className="danger"
                                  disabled={Boolean(busyAction)}
                                  onClick={() => void (async () => {
                                    if (!(await askConfirm({
                                      title: "删除这条已完成记录？",
                                      detail: "仅从列表移除，最终成片及其他已生成文件保留。",
                                      items: [`第 ${itemNumber(item)} 条 · ${itemTitle(item)}`],
                                      confirmLabel: "删除记录",
                                      tone: "danger",
                                    }))) return;
                                    await itemCallFor(item, "", "DELETE");
                                  })()}
                                  aria-label={`删除记录：${itemTitle(item)}`}
                                >
                                  <Trash />删除
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                        {selectedId === item.id && (
                          <tr className="batch-expanded-row">
                            <td colSpan={6}>{renderSelectedDetail()}</td>
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

      {/* 页内确认框（不用 window.confirm）：深色壳子里的同风格弹框，把「影响哪几条」摊开，
          危险操作用红色确认键、Esc / 取消按钮 = 取消（2026-09-15 用户：「弹出框的效果样式改下」）。 */}
      {imagePreview && <ImageViewer key={imagePreview} src={imagePreview} label="候选人物图" onClose={() => setImagePreview(null)} />}
      {videoPreview && (
        <dialog closedby="closerequest"
          ref={videoDialog}
          className="batch-video-dialog"
          aria-labelledby="batch-video-title"
          onCancel={() => setVideoPreview(null)}
          onClose={() => setVideoPreview(null)}
        >
          <div className="batch-video-header">
            <h2 id="batch-video-title">{videoPreview.title}</h2>
            <button autoFocus aria-label="关闭成片预览" onClick={() => setVideoPreview(null)}><X /></button>
          </div>
          <BatchVideo key={videoPreview.url} src={videoPreview.url} autoPlay />
          <div className="batch-video-navigation">
            <button disabled={previewIndex <= 0} onClick={() => switchPreview(-1)}>↑ 上一条</button>
            <span>{previewIndex >= 0 ? `${previewIndex + 1} / ${previewItems.length}` : "成片预览"}</span>
            <button disabled={previewIndex < 0 || previewIndex >= previewItems.length - 1} onClick={() => switchPreview(1)}>↓ 下一条</button>
          </div>
        </dialog>
      )}
      {addOpen && <BatchDialog id="batch-add-dialog" title="添加任务" subtitle="批量制作 · 视频链接"
        icon={<UploadSimple weight="duotone" />} trigger={inputToggle} restoreFocus={restoreInputFocus}
        onClose={() => setActiveDialog(null)} className="batch-add-dialog"
        description={batch && batch.status !== "cancelled" ? "链接将追加到当前队列，重复链接自动跳过；关闭窗口会保留尚未提交的输入。" : "选择视频类型并粘贴链接，提交后开始准备人物图与发布文案；关闭窗口会保留输入。"}
        footer={<>
          <p className="batch-settings-feedback">{busyAction === "start" ? "正在加入队列…" : "关闭保留草稿 · 重复链接会逐条列出"}</p>
          {addResult && <button type="button" className="batch-secondary" onClick={showTaskList}>查看任务列表</button>}
          <button type="button" className="batch-primary small" disabled={!canStart || !loaded || busyAction === "start"} onClick={prepare}>
            {busyAction === "start" ? <SpinnerGap className="spin" /> : <Play weight="fill" />}准备任务（{splitUrls(singing).length + splitUrls(dance).length} 条）
          </button>
        </>}>
        <div className={`batch-input-grid ${singingOn && danceOn ? "" : "single"}`}>
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
                <small>{splitUrls(dance).length} 条 · 最长 40 秒 · 默认 9:16 竖版（审核时每条都能改）· 动作迁移成片</small>
              </>
            ) : (
              <small>已关闭：这类链接不会展示，也不会加入批次。</small>
            )}
          </label>
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
        </div>

        {addError && <div className="batch-alert" role="alert"><WarningCircle />{addError}</div>}
        <div role="status" aria-live="polite">
          {addResult && <p className="batch-settings-feedback">本次新增 {addResult.added} 条，跳过 {addResult.duplicates.length} 条重复链接。</p>}
        </div>
        {Boolean(addResult?.duplicates.length) && <section className="batch-duplicate-results" aria-label="重复链接清单">
          <strong>以下链接未重复添加</strong>
          <ol>
            {addResult!.duplicates.map((item) => <li key={`${item.kind}-${item.inputIndex}`}>
              <div><b>{item.kind === "singing" ? "歌曲" : "跳舞"}输入第 {item.inputIndex} 条</b><span>{item.reason}</span></div>
              <span className="batch-duplicate-url">{item.url}</span>
            </li>)}
          </ol>
        </section>}
        <p className="batch-settings-rule"><ListChecks />先备料，再逐条出片；出片与关机策略可在工具栏的「运行设置」中调整。</p>
      </BatchDialog>}
      {settingsOpen && <BatchDialog id="batch-running-settings-dialog" title="运行设置" subtitle="批量制作 · 运行策略"
        icon={<GearSix weight="duotone" />} trigger={settingsTrigger} onClose={() => setActiveDialog(null)}
        description={batch ? "开关修改后立即保存并生效，无需再次确认。关闭此窗口不会撤销已保存的设置。" : "设置会记在当前浏览器，点击「准备任务」时随新批次一起应用。"}
        footer={<>

            <div className={settingsFeedback?.error ? "batch-settings-feedback error" : "batch-settings-feedback"} role={settingsFeedback?.error ? "alert" : "status"}>
              {["auto-render", "shutdown-on-complete", "shutdown-after-prep"].includes(busyAction) ? <><SpinnerGap className="spin" />正在保存设置…</> : settingsFeedback?.text || (batch ? "当前显示已保存的设置" : "当前显示新批次的预设")}
            </div>
            <button type="button" className="batch-primary small" onClick={() => setActiveDialog(null)}>关闭</button>

        </>}>
        <div className="batch-settings-body">
        <div className="batch-shutdown-row">
          <span><b><Power weight="fill" /> 备料完成后关机</b>
            <small>默认关闭。其余条目备料完成后关机，异常条目留待之后处理，不阻止关机；关机前留 {shutdownDelaySeconds} 秒，可随时取消。</small></span>
          <button type="button" role="switch" aria-label="备料完成后关机" aria-checked={prepShutdownSwitchOn}
            className={`batch-switch ${prepShutdownSwitchOn ? "on" : ""}`}
            disabled={!loaded || Boolean(busyAction)} onClick={toggleShutdownAfterPrep}>
            <i />{prepShutdownSwitchOn ? "已开启" : "已关闭"}
          </button>
        </div>
        <div className="batch-shutdown-row">
          <span>
            <b><Power weight="fill" /> 全部完成后自动关机</b>
            <small>
              所有条目都做完（没有排队、等你确认、待出片或出片中的）才会关机；关机前留 {shutdownDelaySeconds} 秒倒计时，
              随时可以点「取消关机」撤销。默认关闭。
            </small>
          </span>
          <button
            type="button"
            role="switch"
            aria-label="全部完成后自动关机"
            aria-checked={shutdownSwitchOn}
            className={`batch-switch ${shutdownSwitchOn ? "on" : ""}`}
            disabled={!loaded || Boolean(busyAction)}
            onClick={toggleShutdownOnComplete}
          >
            <i />{shutdownSwitchOn ? "已开启" : "已关闭"}
          </button>
        </div>
        </div>
            <p className="batch-settings-rule"><ListChecks aria-hidden="true" />备料完成后关机开启时，会关闭全部完成后关机。关机前有 {shutdownDelaySeconds} 秒倒计时，可在页面取消。</p>
            {shutdown.pending && <div className="batch-alert shutdown"><Power /><span>关机倒计时进行中：剩余 <b>{shutdownSecondsLeft}</b> 秒</span><button type="button" className="batch-primary small" disabled={Boolean(busyAction)} onClick={() => void cancelShutdown()}>取消关机</button></div>}

      </BatchDialog>}
      {confirmAsk && createPortal(
        <div
          className="batch-modal-backdrop"
          role="presentation"
        >
          <div
            className={`batch-modal ${confirmAsk.tone}`}
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="batch-modal-title"
            aria-describedby={confirmAsk.detail ? "batch-modal-detail" : undefined}
          >
            <span className="batch-modal-icon" aria-hidden="true">{confirmAsk.tone === "restart" ? <ArrowClockwise weight="bold" /> : confirmAsk.tone === "danger" ? <Trash weight="fill" /> : <WarningCircle weight="fill" />}</span>
            <div className="batch-modal-body">
              <h2 id="batch-modal-title">{confirmAsk.title}</h2>
              {confirmAsk.detail && <p id="batch-modal-detail">{confirmAsk.detail}</p>}
              {confirmAsk.items && confirmAsk.items.length > 0 && (
                <ul className="batch-modal-items">
                  {confirmAsk.items.slice(0, 6).map((line) => <li key={line}>{line}</li>)}
                  {confirmAsk.items.length > 6 && <li>…还有 {confirmAsk.items.length - 6} 条</li>}
                </ul>
              )}
            </div>
            <div className="batch-modal-actions">
              {/* 确认前默认聚焦「取消」，保留原有防误操作行为 */}
              <button className="ghost" autoFocus onClick={() => answerConfirm(false)}>
                {confirmAsk.cancelLabel || "取消"}
              </button>
              <button
                className={confirmAsk.tone === "restart" ? "batch-primary" : confirmAsk.tone === "warning" ? "batch-warning" : "danger"}
                onClick={() => answerConfirm(true)}
              >
                {confirmAsk.confirmLabel}
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
      </div>
    </main>
  );
}
