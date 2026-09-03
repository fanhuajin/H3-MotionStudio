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
  playableReady: boolean;
  mediaUrl: string;
  downloadUrl: string;
};

type DouyinLoginStatus = {
  state: "idle" | "opening" | "waiting" | "completed" | "error";
  message: string;
  error?: string | null;
  cookieReady: boolean;
  cookieCount: number;
  missing: string[];
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
  if (job.status === "completed" && job.result && !job.result.playableReady) return "转换中";
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
  const [login, setLogin] = useState<DouyinLoginStatus | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);
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

  const loadLoginStatus = useCallback(async () => {
    try {
      const response = await fetch("/api/douyin/login/status");
      if (!response.ok) return null;
      const nextLogin = await response.json() as DouyinLoginStatus;
      setLogin(nextLogin);
      if (nextLogin.cookieReady) await loadStatus();
      return nextLogin;
    } catch {
      return null;
    }
  }, [loadStatus]);

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
    void loadLoginStatus();
  }, [loadJobs, loadLoginStatus, loadStatus]);

  useEffect(() => {
    if (!login || !["opening", "waiting"].includes(login.state)) return;
    const timer = window.setInterval(() => { void loadLoginStatus(); }, 1500);
    return () => window.clearInterval(timer);
  }, [loadLoginStatus, login?.state]);

  useEffect(() => {
    const converting = activeJob?.status === "completed" && activeJob.result && !activeJob.result.playableReady;
    if (!activeJob || (!["pending", "running"].includes(activeJob.status) && !converting)) return;
    const timer = window.setInterval(() => {
      void loadJob(activeJob.job_id).catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "任务状态更新失败");
      });
    }, 1800);
    return () => window.clearInterval(timer);
  }, [activeJob?.job_id, activeJob?.status, activeJob?.result?.playableReady, loadJob]);

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

  const loginRequest = async (path: "start" | "finish" | "cancel") => {
    setLoginBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/douyin/login/${path}`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "登录操作失败");
      const nextLogin = payload as DouyinLoginStatus;
      setLogin(nextLogin);
      if (nextLogin.cookieReady) await loadStatus();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "登录操作失败");
    } finally {
      setLoginBusy(false);
    }
  };

  const mediaConverting = activeJob?.status === "completed" && Boolean(activeJob.result && !activeJob.result.playableReady);
  const isBusy = submitting || activeJob?.status === "pending" || activeJob?.status === "running" || mediaConverting;
  const loginActive = login?.state === "opening" || login?.state === "waiting";
  const taskError = activeJob?.status === "failed" ? activeJob.error : null;
  const visibleError = error || taskError || (login?.state === "error" ? login.error || login.message : null);

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

      {(visibleError || (service && !service.connected) || (service && !service.cookieReady)) && (
        <section className={`service-notice ${visibleError ? "error" : ""}`} role={visibleError ? "alert" : undefined}>
          {visibleError ? <WarningCircle weight="fill" /> : <ShieldNotice />}
          <div className="service-notice-copy">
            <strong>{visibleError ? "任务需要处理" : loginActive ? "等待抖音登录" : service?.connected ? "需要登录抖音" : service?.message}</strong>
            <span>{visibleError || (loginActive ? login?.message : service?.connected ? "打开独立登录窗口，完成登录后 Cookie 会自动保存到本机。" : "首次提交任务时会尝试自动启动下载服务。")}</span>
            {login?.state === "waiting" && login.missing.length > 0 && (
              <small>正在等待：{login.missing.join("、")}</small>
            )}
          </div>
          {service?.connected && !service.cookieReady && (
            <div className="login-actions">
              {loginActive ? (
                <>
                  <button className="login-primary" disabled={loginBusy} onClick={() => void loginRequest("finish")}>我已完成登录</button>
                  <button disabled={loginBusy} onClick={() => void loginRequest("cancel")}>取消</button>
                </>
              ) : (
                <button className="login-primary" disabled={loginBusy} onClick={() => void loginRequest("start")}>{loginBusy ? "正在打开" : "打开登录窗口"}</button>
              )}
            </div>
          )}
        </section>
      )}

      {activeJob?.result && (
        <section className="download-result-card">
          <div className="download-result-video">
            {activeJob.result.playableReady ? (
              <video src={activeJob.result.mediaUrl} controls preload="metadata" />
            ) : (
              <div className="download-converting">
                <SpinnerGap className="spin" weight="bold" />
                <strong>正在转换为可播放格式…</strong>
                <span>完成后会直接替换本地下载文件</span>
              </div>
            )}
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
            {activeJob.result.playableReady ? (
              <a className="result-download-link" href={activeJob.result.downloadUrl}>
                <DownloadSimple weight="bold" /> 下载可播放 MP4
              </a>
            ) : (
              <span className="result-download-link disabled"><SpinnerGap className="spin" /> 正在转换</span>
            )}
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
                <span className="job-counts">{job.success ? `成功 ${job.success}` : job.failed ? `失败 ${job.failed}` : job.skipped ? `本机已有 ${job.skipped}` : "等待结果"}</span>
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
