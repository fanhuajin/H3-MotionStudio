export type MilestoneStatus = "pending" | "running" | "completed" | "skipped" | "error";

export interface Milestone {
  id: string;
  label: string;
  subtitle: string;
  status: MilestoneStatus;
  startedAt?: string | null;
  elapsed?: string | null;
  progress?: number | null;
  progressValue?: number | null;
  progressMax?: number | null;
  currentNode?: string | null;
}

export interface JobLog {
  time: string;
  message: string;
}

export interface JobState {
  id: string;
  kind?: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";
  stage: string;
  createdAt: string;
  updatedAt: string;
  /** 首次进入执行（running）的时间；缺失时前端回退用 createdAt */
  startedAt?: string | null;
  /** 完成/失败/中断时间 */
  finishedAt?: string | null;
  sourceName: string;
  sourceSize: number;
  sourceDuration?: number | null;
  sourceFps?: number | null;
  /** 二采放大任务参数（kind === "upscale"） */
  multiplier?: string | null;
  /** 歌词字幕任务参数（kind === "lyrics"） */
  songName?: string | null;
  lyricLang?: string | null;
  lyricLangLabel?: string | null;
  lyricAsrLang?: string | null;
  /** 长视频分段：预估总段数与当前段（每段81帧、重叠5帧衔接） */
  estimatedSegments?: number | null;
  currentSegment?: number | null;
  /** 去字幕分批（8GB 显存保护）：预计总批数与当前批；批内平滑进度由后端投影 */
  cleanBatches?: number | null;
  cleanBatch?: number | null;
  /** 当前批预估秒数（后端用于把批内耗时折算成整段进度） */
  cleanBatchEstSecs?: number | null;
  referenceName?: string;
  referenceSize?: number;
  referenceInputName?: string;
  referenceUploaded?: boolean;
  // 动作迁移任务参数（kind === "migrate"）
  canvas?: string;
  migrateMode?: string;
  removeSubtitles?: boolean;
  hd1080?: boolean;
  contentPrompt?: string;
  videoPrompt?: string;
  imagePrompt?: string;
  actionPrompt: string;
  cameraPrompt: string;
  currentNodeId?: string | null;
  currentNodeTitle?: string | null;
  progress?: number | null;
  progressValue?: number | null;
  progressMax?: number | null;
  milestones: Milestone[];
  logs: JobLog[];
  errorSummary?: string | null;
  errorDetail?: string | null;
  originalReady: boolean;
  cleanReady?: boolean;
  draftReady?: boolean;
  enhancedReady: boolean;
  finalReady: boolean;
  output?: {
    width: number;
    height: number;
    duration?: number | null;
    size?: number | null;
    completedAt?: string | null;
  } | null;
}

export interface AppConfig {
  comfyuiConnected: boolean;
  comfyuiVersion?: string | null;
  fixedReferenceUrl: string;
  defaultAction: string;
  defaultCamera: string;
  maxDurationSeconds: number;
  environmentReady: boolean;
  missingRequirements: string[];
}
