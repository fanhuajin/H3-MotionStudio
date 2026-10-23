import { useEffect, useRef, useState } from "react";
import { CheckCircle, DownloadSimple, LinkSimple, SpinnerGap, WarningCircle } from "@phosphor-icons/react";
import { readJson } from "./api";
import "./douyin-tool.css";

type DownloadJob = {
  job_id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  error?: string | null;
  result?: { filename: string; path: string; size: number; mediaUrl: string; downloadUrl: string; playableReady: boolean } | null;
};
type LoginState = { state: "idle" | "opening" | "waiting" | "completed" | "error"; message: string; error?: string | null; cookieReady: boolean };
const isActive = (job: DownloadJob | null) => job?.status === "pending" || job?.status === "running";

export function DouyinRoute() {
  const [url, setUrl] = useState("");
  const [job, setJob] = useState<DownloadJob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [login, setLogin] = useState<LoginState | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);
  const [preview, setPreview] = useState<"loading" | "ready" | "error">("loading");
  const mounted = useRef(false);
  const submittingRef = useRef(false);
  const loginRef = useRef(false);
  const busy = submitting || isActive(job);
  const loginActive = login?.state === "opening" || login?.state === "waiting";
  const recognized = /https?:\/\/(?:[\w-]+\.)*(?:douyin\.com|iesdouyin\.com)(?=[/:?#]|$)/i.test(url);
  const visibleError = error || (job?.status === "failed" || job?.status === "cancelled" ? job.error || "下载未完成，请重试。" : "");

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  // 只轮询本次提交的任务，不在打开页面时读取历史、探测服务或扫描下载目录。
  useEffect(() => {
    if (!job || !isActive(job)) return;
    const controller = new AbortController();
    let timer: number | undefined;
    const refresh = async () => {
      try {
        const next = await readJson<DownloadJob>(fetch(`/api/douyin/jobs/${job.job_id}`, { cache: "no-store", signal: controller.signal }));
        if (controller.signal.aborted) return;
        setJob(next); setError("");
        if (!isActive(next)) return;
      } catch (reason) {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : "暂时无法读取下载进度，正在重试。");
      }
      timer = window.setTimeout(() => void refresh(), 1800);
    };
    timer = window.setTimeout(() => void refresh(), 1000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [job?.job_id, job?.status]);

  useEffect(() => {
    if (!loginActive) return;
    const controller = new AbortController();
    let timer: number | undefined;
    const refresh = async () => {
      try {
        const next = await readJson<LoginState>(fetch("/api/douyin/login/status", { signal: controller.signal }));
        if (controller.signal.aborted) return;
        setLogin(next);
        if (next.state === "error") setError(next.error || next.message);
        if (next.state !== "opening" && next.state !== "waiting") return;
      } catch (reason) {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : "读取登录状态失败");
      }
      timer = window.setTimeout(() => void refresh(), 1800);
    };
    timer = window.setTimeout(() => void refresh(), 1800);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [loginActive]);

  const submit = async () => {
    if (submittingRef.current || busy || loginActive) return;
    if (!recognized) { setError("请粘贴抖音视频链接或含链接的分享文字。"); return; }
    submittingRef.current = true; setSubmitting(true); setError(""); setJob(null); setPreview("loading");
    try {
      const next = await readJson<DownloadJob>(fetch("/api/douyin/download", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: url.trim() }),
      }));
      if (mounted.current) setJob(next);
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "下载失败，请重试。");
    } finally {
      submittingRef.current = false;
      if (mounted.current) setSubmitting(false);
    }
  };

  const loginRequest = async (action: "start" | "finish" | "cancel") => {
    if (loginRef.current) return;
    loginRef.current = true; setLoginBusy(true); setError("");
    try {
      const next = await readJson<LoginState>(fetch(`/api/douyin/login/${action}`, { method: "POST" }));
      if (mounted.current) { setLogin(next); if (next.state === "error") setError(next.error || next.message); }
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "登录操作失败");
    } finally {
      loginRef.current = false;
      if (mounted.current) setLoginBusy(false);
    }
  };

  return <main className="douyin-route douyin-tool">
    <section className="download-card">
      <h1>抖音视频下载</h1>
      <p className="douyin-tool-note">粘贴作品链接或分享文字，下载后直接查看视频。</p>
      <form className="download-form" onSubmit={event => { event.preventDefault(); void submit(); }}>
        <div className="url-field"><LinkSimple aria-hidden="true" /><input value={url} onChange={event => { setUrl(event.target.value); setError(""); }} placeholder="粘贴抖音视频链接或分享文字" aria-label="抖音视频链接" autoComplete="off" /></div>
        <button className="download-action" type="submit" disabled={busy || loginActive || !url.trim()}>
          {busy ? <SpinnerGap className="spin" /> : <DownloadSimple />}{submitting ? "正在提交" : isActive(job) ? "下载中" : "下载视频"}
        </button>
      </form>
      {busy && <p className="douyin-tool-progress" role="status"><SpinnerGap className="spin" />正在下载并准备视频，请稍候…</p>}
      {visibleError && <div className="douyin-tool-error" role="alert"><WarningCircle /><div><strong>下载暂未完成</strong><p>{visibleError}</p>{!busy && !loginActive && <button type="button" disabled={loginBusy} onClick={() => void loginRequest("start")}>{loginBusy ? "正在打开登录窗口…" : "需要登录时，打开抖音登录窗口"}</button>}</div></div>}
      {(loginActive || login?.cookieReady) && <div className="douyin-tool-login" role="status">
        <p>{loginActive ? login?.message || "请在打开的窗口完成抖音登录。" : "登录已完成，可以重新下载。"}</p>
        {loginActive && <div><button type="button" disabled={loginBusy} onClick={() => void loginRequest("finish")}>我已完成登录</button><button type="button" disabled={loginBusy} onClick={() => void loginRequest("cancel")}>取消登录</button></div>}
      </div>}
    </section>
    {job?.status === "completed" && job.result && <section className="douyin-tool-result" aria-label="本次下载的视频">
      <div className="douyin-tool-success" role="status"><CheckCircle weight="fill" /><strong>下载成功</strong><span>{(job.result.size / 1024 / 1024).toFixed(1)} MB</span></div>
      {preview === "loading" && <p className="douyin-tool-preview-note" role="status"><SpinnerGap className="spin" />正在加载视频预览…</p>}
      {preview === "error" && <p className="douyin-tool-preview-note" role="status">视频已下载，预览暂时无法播放，可以保存后在本机查看。</p>}
      <video key={job.job_id} src={job.result.mediaUrl} controls preload="metadata" playsInline aria-label="本次下载的视频预览" onLoadedMetadata={() => setPreview("ready")} onError={() => setPreview("error")} />
      <div className="douyin-tool-file"><div><strong>{job.result.filename}</strong><p title={job.result.path}>已保存到：{job.result.path}</p></div><a className="result-download-link" href={job.result.downloadUrl} download><DownloadSimple />保存视频</a></div>
    </section>}
  </main>;
}
