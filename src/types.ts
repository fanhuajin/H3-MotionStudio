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
