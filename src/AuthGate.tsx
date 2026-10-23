import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { ArrowRight, Eye, EyeSlash, LockKey, SignOut, SpinnerGap, User } from "@phosphor-icons/react";
import { readJson } from "./api";
import "./login.css";
const LoginPrism = lazy(() => import("./LoginPrism"));

interface AuthState { configured: boolean; authenticated: boolean }

export function LogoutButton() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return <div className="auth-logout"><button type="button" disabled={busy} onClick={async () => {
    setBusy(true); setError("");
    try {
      await readJson(fetch("/api/auth/logout", { method: "POST" }));
      window.dispatchEvent(new Event("h3:logout"));
    } catch { setError("退出失败，请重试"); } finally { setBusy(false); }
  }}><SignOut />{busy ? "正在退出" : "退出登录"}</button>{error && <small role="alert">{error}</small>}</div>;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [remember, setRemember] = useState(true);
  const [visible, setVisible] = useState(false);
  const check = useCallback(async () => {
    setError("");
    try { setAuth(await readJson<AuthState>(fetch("/api/auth/status", { cache: "no-store" }))); }
    catch { setError("暂时无法连接登录服务，请确认后端已更新并启动。"); }
  }, []);
  useEffect(() => {
    void check();
    const logout = () => { setPassword(""); setConfirm(""); setAuth((state) => state ? { ...state, authenticated: false } : null); };
    window.addEventListener("h3:logout", logout);
    window.addEventListener("h3:unauthorized", logout);
    const refresh = () => { if (document.visibilityState === "visible") void check(); };
    document.addEventListener("visibilitychange", refresh);
    return () => { window.removeEventListener("h3:logout", logout); window.removeEventListener("h3:unauthorized", logout); document.removeEventListener("visibilitychange", refresh); };
  }, [check]);
  useEffect(() => {
    if (!auth?.authenticated) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void check();
    }, 30000);
    return () => window.clearInterval(timer);
  }, [auth?.authenticated, check]);
  if (auth?.authenticated) return <>{children}</>;
  // 初次会话检查完成前不挂载登录页，避免已登录用户刷新时闪出登录画面。
  if (!auth) return <main className="auth-pending">
    <div className="login-connecting" role="status" aria-busy={!error}>
      {error ? <><p role="alert">{error}</p><button type="button" onClick={() => void check()}>重新连接 <ArrowRight /></button></> : "正在连接创作空间"}
    </div>
  </main>;
  const setup = auth !== null && !auth.configured;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !auth) return;
    if (setup && password !== confirm) { setError("两次输入的密码不一致"); return; }
    setBusy(true); setError("");
    try {
      await readJson(fetch(`/api/auth/${setup ? "setup" : "login"}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password, remember }) }));
      setPassword(""); setConfirm("");
      setAuth({ configured: true, authenticated: true });
    } catch (reason) { setError(reason instanceof Error ? reason.message.replace(/^HTTP \d+：/, "") : "登录失败，请重试"); }
    finally { setBusy(false); }
  };
  return <main className="login-shell">
    <section className="login-art" aria-label="H3 MotionStudio 创作空间">
      <img className="login-prism" src="/assets/login-prism.png" alt="" />
      <Suspense fallback={null}><LoginPrism /></Suspense>
      <div className="login-brand"><span className="login-mark" aria-hidden="true" /><div>H3 MotionStudio<span>YOUR PRIVATE CREATIVE SPACE</span></div></div>
      <div className="login-art-note">用 AI<br />让创意<br />成为影像<span /></div>
      <div className="login-manifesto"><h1>让灵感，<br /><em>开始流动。</em></h1><p>H3 MOTIONSTUDIO</p><span /><small>AI 赋能的私人视频创作空间</small></div>
    </section>
    <section className="login-panel" aria-labelledby="login-title"><div className="login-form-wrap">
      <div className="login-eyebrow"><span /> PRIVATE STUDIO</div>
      <h2 id="login-title">{setup ? "开启创作空间" : "欢迎回来"}</h2>
      <p className="login-subtitle">{setup ? "创建你的专属管理员账号" : "登录你的创作工作台"}</p>
      <form onSubmit={submit}>
        <label htmlFor="login-user">账号</label><div className="login-input"><User /><input id="login-user" name="username" autoComplete="username" required maxLength={64} value={username} onChange={(event) => setUsername(event.target.value)} disabled={busy} /></div>
        <label htmlFor="login-password">密码</label><div className="login-input"><LockKey /><input id="login-password" name="password" type={visible ? "text" : "password"} autoComplete={setup ? "new-password" : "current-password"} required minLength={6} maxLength={256} placeholder={setup ? "设置至少 6 位密码" : "输入你的密码"} value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} /><button type="button" aria-label={visible ? "隐藏密码" : "显示密码"} aria-pressed={visible} onClick={() => setVisible(!visible)}>{visible ? <EyeSlash /> : <Eye />}</button></div>
        {setup && <><label htmlFor="login-confirm">确认密码</label><div className="login-input"><LockKey /><input id="login-confirm" type={visible ? "text" : "password"} autoComplete="new-password" required minLength={6} maxLength={256} placeholder="再次输入密码" value={confirm} onChange={(event) => setConfirm(event.target.value)} disabled={busy} /></div><p className="login-setup-note">首次设置请在本机完成，账号仅供你个人使用。</p></>}
        <label className="login-remember"><input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} disabled={busy} /><span>记住登录</span><small>30 天</small></label>
        {error && <p className="login-error" role="alert">{error}</p>}
        <button className="login-submit" type="submit" disabled={busy}><span>{busy ? "正在进入" : setup ? "创建并进入工作台" : "登录"}</span>{busy ? <SpinnerGap className="login-spinner" /> : <ArrowRight />}</button>
      </form>
      <p className="login-footnote"><LockKey /> 只属于你的灵感与作品</p>
    </div><span className="login-panel-footer">H3 MOTIONSTUDIO · PERSONAL EDITION</span></section>
  </main>;
}
