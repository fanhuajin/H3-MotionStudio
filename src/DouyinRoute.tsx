import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowSquareOut,
  CheckCircle,
  ClockCounterClockwise,
  DownloadSimple,
  FileVideo,
  FolderOpen,
  LinkSimple,
  SpinnerGap,
  WarningCircle,
  XCircle,
} from "@phosphor-icons/react";

type DouyinServiceStatus = {
  available: boolean;
  connected: boolean;
  cookieReady: boolean;
  outputDirectory: string;
  message: string;
};

type DouyinResult = {
  awemeId: string;
  filename: string;
  path: string;
  size: number;
  mediaType: string;
  mediaUrl: string;
  downloadUrl: string;
};

type DouyinJob = {
  job_id: string;
  url: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  total: number;
  success: number;
  failed: number;
  skipped: number;
  error?: string | null;
  result?: DouyinResult | null;
};

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

function relativeTime(value?: string | null) {
  if (!value) return "刚刚";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function statusText(job: DouyinJob) {
  if (job.status === "pending") return "排队中";
  if (job.status === "running") return "下载中";
  if (job.status === "completed") return job.skipped ? "已存在" : "已完成";
  if (job.status === "cancelled") return "已取消";
  return "失败";
}

function isDouyinUrl(value: string) {
  return /https?:\/\/(?:[\w-]+\.)*(?:douyin\.com|iesdouyin\.com)(?=[/:?#]|$)/i.test(value);
}

function JobIcon({ status }: { status: DouyinJob["status"] }) {
  if (status === "completed") return <CheckCircle weight="fill" />;
  if (status === "failed" || status === "cancelled") return <XCircle weight="fill" />;
  if (status === "running") return <SpinnerGap className="spin" />;
  return <ClockCounterClockwise />;
}

export function DouyinRoute() {
  const [url, setUrl] = useState("");
  const [service, setService] = useState<DouyinServiceStatus | null>(null);
  const [jobs, setJobs] = useState<DouyinJob[]>([]);
  const [activeJob, setActiveJob] = useState<DouyinJob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const response = await fetch("/api/douyin/status");
      if (!response.ok) throw new Error("无法读取下载服务状态");
      setService(await response.json());
    } catch (nextError) {
      setService({
        available: false,
        connected: false,
        cookieReady: false,
        outputDirectory: "",
        message: nextError instanceof Error ? nextError.message : "下载服务不可用",
      });
    }
  }, []);

  const loadJobs = useCallback(async () => {
    try {
      const response = await fetch("/api/douyin/jobs");
      if (!response.ok) return;
      const payload = await response.json();
      const nextJobs = (payload.jobs || []) as DouyinJob[];
      setJobs(nextJobs);
      if (!activeJob && nextJobs.length) setActiveJob(nextJobs[0]);
    } catch {
      // The service may still be starting. Status copy already communicates this.
    }
  }, [activeJob]);

  const loadJob = useCallback(async (jobId: string) => {
    const response = await fetch(`/api/douyin/jobs/${jobId}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "无法读取任务状态");
    const nextJob = payload as DouyinJob;
    setActiveJob(nextJob);
    setJobs((current) => {
      const remaining = current.filter((item) => item.job_id !== nextJob.job_id);
      return [nextJob, ...remaining];
    });
    return nextJob;
  }, []);

  useEffect(() => {
    void loadStatus();
    void loadJobs();
  }, [loadJobs, loadStatus]);

  useEffect(() => {
    if (!activeJob || !["pending", "running"].includes(activeJob.status)) return;
    const timer = window.setInterval(() => {
      void loadJob(activeJob.job_id).catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "任务状态更新失败");
      });
    }, 1800);
    return () => window.clearInterval(timer);
  }, [activeJob?.job_id, activeJob?.status, loadJob]);

  const recognized = useMemo(
    () => isDouyinUrl(url.trim()),
    [url],
  );

  const submit = async () => {
    if (!recognized) {
      setError("请粘贴有效的抖音作品链接或短链。");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/douyin/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url.trim() }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "下载任务创建失败");
      const nextJob = payload as DouyinJob;
      setActiveJob(nextJob);
      setJobs((current) => [nextJob, ...current.filter((item) => item.job_id !== nextJob.job_id)]);
      await loadStatus();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "下载任务创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  const isBusy = submitting || activeJob?.status === "pending" || activeJob?.status === "running";

  return (
    <main className="douyin-route">
      <header className="route-hero douyin-hero">
        <div>
          <p className="route-eyebrow"><span /> 抖音 · DOWNLOAD</p>
          <h1>粘贴抖音链接，<em>马上开始。</em></h1>
          <p className="route-description">单个视频、图文与短链都可识别，任务状态和错误会原样显示。</p>
        </div>
        <div className="hero-icon"><DownloadSimple weight="regular" /></div>
      </header>

      <section className="download-card">
        <div className="download-card-label"><span /> 抖音 URL</div>
        <h2>下载链接</h2>
        <div className="download-form">
          <div className={`url-field ${recognized ? "recognized" : ""}`}>
            <LinkSimple />
            <input
              value={url}
              onChange={(event) => { setUrl(event.target.value); setError(null); }}
              onKeyDown={(event) => { if (event.key === "Enter" && !isBusy) void submit(); }}
              placeholder="https://www.douyin.com/video/7613347091070692019"
              aria-label="抖音下载链接"
            />
          </div>
          <button className="download-action" onClick={submit} disabled={isBusy}>
            {isBusy ? <SpinnerGap className="spin" /> : <DownloadSimple weight="bold" />}
            {isBusy ? "处理中" : "下载"}
          </button>
        </div>
        <div className="recognition-line">
          <span className={recognized ? "ready" : ""} />
          {recognized ? "已识别：抖音链接 · 点击“下载”即可" : "支持视频完整链接、分享短链和带 modal_id 的链接"}
        </div>
      </section>

      {(error || (service && !service.connected) || (service && !service.cookieReady)) && (
        <section className={`service-notice ${error ? "error" : ""}`} role={error ? "alert" : undefined}>
          {error ? <WarningCircle weight="fill" /> : <ShieldNotice />}
          <div>
            <strong>{error ? "任务需要处理" : service?.connected ? "登录状态尚未接入" : service?.message}</strong>
            <span>{error || (service?.connected ? "下载服务已连接；遇到抖音风控时，需要在本机配置登录 Cookie。" : "首次提交任务时会尝试自动启动下载服务。")}</span>
          </div>
        </section>
      )}

      {activeJob?.result && (
        <section className="download-result-card">
          <div className="download-result-video">
            <video src={activeJob.result.mediaUrl} controls preload="metadata" />
          </div>
          <div className="download-result-copy">
            <p className="route-eyebrow"><span /> DOWNLOAD COMPLETE</p>
            <h2>作品已保存</h2>
            <strong className="download-filename">{activeJob.result.filename}</strong>
            <dl>
              <div><dt>作品 ID</dt><dd>{activeJob.result.awemeId}</dd></div>
              <div><dt>文件大小</dt><dd>{formatBytes(activeJob.result.size)}</dd></div>
              <div><dt>保存位置</dt><dd title={activeJob.result.path}>{service?.outputDirectory || "Downloaded"}</dd></div>
            </dl>
            <a className="result-download-link" href={activeJob.result.downloadUrl}>
              <DownloadSimple weight="bold" /> 下载到本机
            </a>
          </div>
        </section>
      )}

      <section className="recent-jobs">
        <div className="recent-jobs-heading">
          <div><p className="route-eyebrow"><span /> TASKS</p><h2>最近任务</h2></div>
          <button onClick={() => void loadJobs()}><ClockCounterClockwise /> 刷新</button>
        </div>
        {jobs.length ? (
          <div className="job-list">
            {jobs.slice(0, 6).map((job) => (
              <button className={`job-row state-${job.status} ${activeJob?.job_id === job.job_id ? "selected" : ""}`} key={job.job_id} onClick={() => void loadJob(job.job_id)}>
                <span className="job-status-icon"><JobIcon status={job.status} /></span>
                <span className="job-summary">
                  <strong>抖音作品 {job.url.match(/(?:modal_id=|\/video\/)(\d+)/)?.[1] || job.job_id}</strong>
                  <small>{job.url}</small>
                </span>
                <span className="job-counts">{job.success ? `成功 ${job.success}` : job.failed ? `失败 ${job.failed}` : "等待结果"}</span>
                <span className="job-state">{statusText(job)}</span>
                <time>{relativeTime(job.finished_at || job.created_at)}</time>
                <ArrowSquareOut />
              </button>
            ))}
          </div>
        ) : (
          <div className="empty-jobs">
            <FileVideo />
            <div><strong>还没有下载任务</strong><span>粘贴一个作品链接，任务会在这里持续更新。</span></div>
          </div>
        )}
        {service?.outputDirectory && (
          <p className="output-location"><FolderOpen /> 文件保存到 <span>{service.outputDirectory}</span></p>
        )}
      </section>
    </main>
  );
}

function ShieldNotice() {
  return <WarningCircle weight="regular" />;
}
