import { useEffect } from "react";
import type { JobState } from "./types";

const DEFAULT_TITLE = "H3 影动高清工作台";
const DEFAULT_ICON = "/favicon.svg";
const POLL_MS = 3000;
/** 终态任务在标签页上保留提示的时长（之后恢复默认标题） */
const TERMINAL_HINT_MS = 150_000;

const STAGE_TEXT: Record<string, string> = {
  starting: "启动中",
  singing: "原版生成中",
  cleaning: "去字幕中",
  migrating: "分段迁移中",
  enhancing: "高清加强中",
  handoff: "切换 RVC",
  voice: "音色转换中",
};

function jobBrief(kind?: string) {
  return kind === "migrate" ? "动作迁移" : "影动生成";
}

function faviconSvg(color: string, symbol: "dot" | "check" | "cross" | "pulse"): string {
  let glyph = "";
  if (symbol === "dot") {
    glyph = `<circle cx='8' cy='8' r='6' fill='${color}' opacity='.22'/><circle cx='8' cy='8' r='3.2' fill='${color}'/>`;
  } else if (symbol === "pulse") {
    glyph = `<circle cx='8' cy='8' r='6' fill='${color}' opacity='.2'><animate attributeName='r' values='3;6' dur='1s' repeatCount='indefinite'/></circle><circle cx='8' cy='8' r='3.2' fill='${color}'/>`;
  } else if (symbol === "check") {
    glyph = `<circle cx='8' cy='8' r='7' fill='${color}'/><path d='M4.6 8.4l2.3 2.3 4.6-5' stroke='#071116' stroke-width='1.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/>`;
  } else {
    glyph = `<circle cx='8' cy='8' r='7' fill='${color}'/><path d='M5.4 5.4l5.2 5.2M10.6 5.4l-5.2 5.2' stroke='#2a0a12' stroke-width='1.6' stroke-linecap='round'/>`;
  }
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>${glyph}</svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function applyTabState(title: string | null, iconHref: string | null) {
  if (title !== null && document.title !== title) document.title = title;
  const link = document.querySelector<HTMLLinkElement>('link[rel~="icon"]');
  if (iconHref !== null && link && link.href !== iconHref) link.href = iconHref;
}

/** 由 App 外壳挂载：把最近任务进度同步到浏览器标签页标题与 favicon。 */
export function TaskTabStatus() {
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      let job: JobState | null = null;
      try {
        const response = await fetch("/api/jobs/latest", { cache: "no-store" });
        if (response.status === 204) job = null;
        else if (response.ok) job = await response.json() as JobState;
        else throw new Error("unavailable");
      } catch {
        return; // 服务未就绪时保持现状
      }
      if (cancelled) return;

      if (!job) {
        applyTabState(DEFAULT_TITLE, DEFAULT_ICON);
        return;
      }
      const brief = jobBrief(job.kind);
      if (job.status === "queued" || job.status === "running") {
        const runningMilestone = (job.milestones || []).find((m) => m.status === "running");
        const label = runningMilestone?.label
          || STAGE_TEXT[job.stage]
          || (job.status === "queued" ? "排队中" : job.stage);
        const percent = job.progress != null && Number.isFinite(job.progress)
          ? `${Math.round(job.progress)}% `
          : "";
        const running = job.status === "running";
        const color = running ? "#28b9d2" : "#8b829c";
        applyTabState(
          `⏳ ${percent}${label} · ${brief} · H3`,
          faviconSvg(color, running ? "pulse" : "dot"),
        );
        return;
      }
      // 终态：结束不久时在标签页提示，随后恢复默认
      const finished = Date.now() - Date.parse(job.updatedAt || job.createdAt);
      if (["completed", "failed", "cancelled", "interrupted"].includes(job.status) && finished < TERMINAL_HINT_MS) {
        const ok = job.status === "completed";
        applyTabState(
          `${ok ? "✅" : "❌"} ${brief}${ok ? "已完成" : "未完成"} · H3`,
          faviconSvg(ok ? "#3fe0bd" : "#ff8b9f", ok ? "check" : "cross"),
        );
        return;
      }
      applyTabState(DEFAULT_TITLE, DEFAULT_ICON);
    };
    void tick();
    const timer = window.setInterval(() => void tick(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      applyTabState(DEFAULT_TITLE, DEFAULT_ICON);
    };
  }, []);

  return null;
}
