import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CheckCircle, Power, SpinnerGap, WarningCircle } from "@phosphor-icons/react";
import "./close-project.css";

export function CloseProjectButton({ onAccepted }: { onAccepted: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const requesting = useRef(false);
  useEffect(() => {
    if (!open) return;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); trigger.current?.focus(); };
  }, [open]);
  async function close() {
    if (requesting.current) return;
    requesting.current = true; setBusy(true); setError("");
    try {
      const response = await fetch("/api/system/close-project", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(result.detail || "关闭请求未成功，请重试。");
      }
      onAccepted();
    } catch (failure) { setError((failure as Error).message); }
    finally { requesting.current = false; setBusy(false); }
  }
  return <>
    <button ref={trigger} className="project-close-button" onClick={() => { setError(""); setOpen(true); }}>
      <Power /><span>关闭项目</span>
    </button>
    {open && createPortal(<dialog ref={dialog} className="batch-settings-dialog project-close-dialog" aria-labelledby="project-close-title"
      onCancel={event => { event.preventDefault(); if (!busy) setOpen(false); }}>
      <header className="batch-settings-header"><span className="batch-modal-icon"><Power /></span><div><p>本地服务</p><h2 id="project-close-title">关闭项目？</h2></div></header>
      <div className="batch-settings-scroll"><p>将停止工作台服务和本项目启动的后台进程，包括 ComfyUI。</p>
        <p>正在进行的生成、下载或发布会中断；已保存的文件和任务记录保留。此操作不会关闭电脑。</p>
        {error && <p className="project-close-error" role="alert">{error}</p>}</div>
      <footer className="batch-settings-footer"><button className="batch-add-toggle" autoFocus disabled={busy} onClick={() => setOpen(false)}>取消</button>
        <button className="batch-add-toggle" disabled={busy} onClick={() => void close()}>{busy ? <SpinnerGap className="spin" /> : <Power />}{busy ? "正在关闭…" : "确认关闭项目"}</button></footer>
    </dialog>, document.body)}
  </>;
}

export function ProjectClosingScreen({ onReturn }: { onReturn: () => void }) {
  const [phase, setPhase] = useState<"closing" | "closed" | "failed">("closing");
  useEffect(() => {
    let stopped = false, timer = 0, attempts = 0, missing = 0;
    const check = async () => {
      attempts++;
      try {
        await fetch("/api/system/project-status", { cache: "no-store", signal: AbortSignal.timeout(1500) });
        missing = 0;
      } catch { missing++; }
      if (stopped) return;
      if (missing >= 2) { setPhase("closed"); return; }
      if (attempts >= 15) { setPhase("failed"); return; }
      timer = window.setTimeout(() => void check(), 1000);
    };
    void check();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, []);
  return <main className="project-closed-screen"><div role="status">
    {phase === "closing" ? <SpinnerGap className="spin" /> : phase === "closed" ? <CheckCircle /> : <WarningCircle />}
    <h1>{phase === "closing" ? "正在关闭项目…" : phase === "closed" ? "项目服务已断开" : "关闭尚未完成"}</h1>
    <p>{phase === "closing" ? "正在停止本地服务和后台进程。" : phase === "closed" ? "可以关闭此页面。下次使用时重新启动工作台即可。" : "本地服务仍在响应，请返回工作台重试。"}</p>
    {phase === "failed" && <button className="batch-add-toggle" onClick={onReturn}>返回工作台</button>}
  </div></main>;
}
