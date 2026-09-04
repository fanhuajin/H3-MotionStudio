import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowsClockwise, ListChecks, SpinnerGap, X } from "@phosphor-icons/react";
import { elapsedMs, formatElapsedMs, useNowTick } from "./jobTime";

type QueueEntry = {
  promptId: string;
  node?: string;
};

type AppJobSummary = {
  id: string;
  kind: string;
  status: string;
  stage: string;
  canvas?: string;
  migrateMode?: string;
  sourceName?: string;
  progress?: number | null;
  currentNodeTitle?: string | null;
  startedAt?: string | null;
  runningMilestone?: string | null;
  promptIds?: string[];
};

type QueuePayload = {
  connected: boolean;
  running: QueueEntry[];
  pending: QueueEntry[];
  app: AppJobSummary | null;
};

const POLL_MS = 3000;

function kindLabel(job: AppJobSummary) {
  if (job.kind === "migrate") {
    const ratio = job.canvas === "4:3" ? "4:3" : "9:16";
    const mode = job.migrateMode === "replacement" ? "人物替换" : "动作迁移";
    return `${ratio} ${mode}`;
  }
  return "影动生成";
}

export function QueuePanel({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const [payload, setPayload] = useState<QueuePayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const confirmTimer = useRef<number | null>(null);

  const appActive = payload?.app?.status === "queued" || payload?.app?.status === "running";
  const tickNow = useNowTick(Boolean(appActive) && open);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/comfy/queue", { cache: "no-store" });
      if (!response.ok) return;
      setPayload(await response.json() as QueuePayload);
    } catch {
      // 服务未就绪时保留旧数据
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => {
      window.clearInterval(timer);
      if (confirmTimer.current) window.clearTimeout(confirmTimer.current);
    };
  }, [load]);

  const runCancel = useCallback(async (jobId: string) => {
    setBusy(true);
    try {
      const response = await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        window.alert(body?.detail || "取消失败，任务可能已结束");
      }
      await load();
    } finally {
      setBusy(false);
      setConfirmId(null);
    }
  }, [load]);

  const requestCancel = (jobId: string) => {
    if (confirmId !== jobId) {
      setConfirmId(jobId);
      if (confirmTimer.current) window.clearTimeout(confirmTimer.current);
      confirmTimer.current = window.setTimeout(() => setConfirmId(null), 5000);
      return;
    }
    void runCancel(jobId);
  };

  const requestClearPending = () => {
    if (!confirmClear) {
      setConfirmClear(true);
      if (confirmTimer.current) window.clearTimeout(confirmTimer.current);
      confirmTimer.current = window.setTimeout(() => setConfirmClear(false), 5000);
      return;
    }
    setBusy(true);
    void (async () => {
      try {
        const response = await fetch("/api/comfy/queue", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "clear-pending" }) });
        if (!response.ok) {
          const body = await response.json().catch(() => null);
          window.alert(body?.detail || "清空失败");
        }
        await load();
      } finally {
        setBusy(false);
        setConfirmClear(false);
      }
    })();
  };

  const app = payload?.app ?? null;
  const appElapsedMs = app ? elapsedMs(app.startedAt, null, tickNow) : null;
  const hasQueueBadge = appActive || (payload?.connected && (payload.running.length > 0 || payload.pending.length > 0));

  return (
    <div className="queue-panel">
      <button className={`queue-entry ${open ? "open" : ""}`} onClick={onToggle} aria-expanded={open}>
        <ListChecks weight={open || hasQueueBadge ? "fill" : "regular"} />
        <span>任务队列</span>
        {hasQueueBadge && <i className="queue-dot" />}
      </button>

      {open && (
        <div className="queue-popover">
          <div className="queue-popover-head">
            <strong>任务队列</strong>
            <span className={payload?.connected ? "ok" : ""}>{payload?.connected ? "ComfyUI 已连接" : "ComfyUI 未运行"}</span>
            <button onClick={onToggle} aria-label="关闭"><X weight="bold" /></button>
          </div>

          <div className="queue-section">
            <p className="queue-section-title">当前应用任务</p>
            {app ? (
              <div className="queue-job">
                <div className="queue-job-line">
                  <strong>{kindLabel(app)}</strong>
                  {appActive && appElapsedMs != null && <time>{formatElapsedMs(appElapsedMs)}</time>}
                </div>
                <span className="queue-job-detail">
                  {app.runningMilestone || app.currentNodeTitle || app.stage || app.sourceName || app.id.slice(0, 8)}
                </span>
                {app.progress != null && (
                  <span className="queue-job-bar"><i style={{ width: `${Math.min(100, Math.max(2, app.progress))}%` }} /></span>
                )}
                {appActive && (
                  <button className={`queue-cancel ${confirmId === app.id ? "armed" : ""}`} disabled={busy} onClick={() => requestCancel(app.id)}>
                    {busy ? <SpinnerGap className="spin" /> : confirmId === app.id ? <X weight="bold" /> : <X />}
                    {confirmId === app.id ? "再点一次确认取消" : "取消当前任务"}
                  </button>
                )}
              </div>
            ) : (
              <p className="queue-empty">当前没有运行中的任务</p>
            )}
          </div>

          <div className="queue-section">
            <p className="queue-section-title">ComfyUI 队列</p>
            {!payload?.connected ? (
              <p className="queue-empty">未连接（提交任务时会自动启动 ComfyUI）</p>
            ) : payload.running.length === 0 && payload.pending.length === 0 ? (
              <p className="queue-empty">队列为空</p>
            ) : (
              <>
                {payload.running.map((entry) => (
                  <div className="queue-row" key={entry.promptId}>
                    <span className="queue-row-state running" />
                    <strong>运行中</strong>
                    <code>{entry.promptId.slice(0, 8)}</code>
                    <span>{entry.node?.replace(/\|.*$/, "") || "任务"}</span>
                  </div>
                ))}
                {payload.pending.map((entry) => (
                  <div className="queue-row" key={entry.promptId}>
                    <span className="queue-row-state" />
                    <strong>排队中</strong>
                    <code>{entry.promptId.slice(0, 8)}</code>
                    <span>{entry.node?.replace(/\|.*$/, "") || "任务"}</span>
                  </div>
                ))}
                {payload.pending.length > 0 && (
                  <button className={`queue-cancel quiet ${confirmClear ? "armed" : ""}`} disabled={busy} onClick={requestClearPending}>
                    <ArrowsClockwise /> {confirmClear ? "再点一次确认清空" : "清空等待队列"}
                  </button>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
