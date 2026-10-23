import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Camera, CheckCircle, LockKey, ShieldCheck, SignOut, SpinnerGap, User, X } from "@phosphor-icons/react";
import { readJson } from "./api";

export interface OwnerProfile {
  username: string;
  displayName: string;
  bio: string;
  avatarUrl: string | null;
  sessionCount: number;
}

export function OwnerAvatar({ profile }: { profile: OwnerProfile | null }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [profile?.avatarUrl]);
  return <span className="owner-avatar">{profile?.avatarUrl && !failed
    ? <img src={profile.avatarUrl} alt="" onError={() => setFailed(true)} />
    : <span>{Array.from(profile?.displayName || profile?.username || "H")[0].toUpperCase()}</span>}</span>;
}

export function AccountSettings({ profile, onSaved, onClose }: {
  profile: OwnerProfile; onSaved: (value: OwnerProfile) => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [tab, setTab] = useState<"profile" | "security">("profile");
  const [username, setUsername] = useState(profile.username);
  const [displayName, setDisplayName] = useState(profile.displayName);
  const [bio, setBio] = useState(profile.bio);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [confirmLogout, setConfirmLogout] = useState(false);
  useEffect(() => { const node = dialog.current; node?.showModal(); return () => node?.close(); }, []);
  const run = async (key: string, action: () => Promise<void>) => {
    if (busy) return;
    setBusy(key); setError(""); setMessage("");
    try { await action(); }
    catch (reason) { setError(reason instanceof Error ? reason.message.replace(/^HTTP \d+：/, "") : "保存失败，请重试"); }
    finally { setBusy(""); }
  };
  const saveProfile = (event: FormEvent) => {
    event.preventDefault();
    void run("profile", async () => {
      const result = await readJson<OwnerProfile>(fetch("/api/auth/me", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, displayName, bio }) }));
      onSaved(result); setUsername(result.username); setDisplayName(result.displayName); setBio(result.bio);
      setMessage("个人资料已保存");
    });
  };
  const savePassword = (event: FormEvent) => {
    event.preventDefault();
    if (newPassword !== confirm) { setError("两次新密码不一致"); return; }
    void run("password", async () => {
      await readJson(fetch("/api/auth/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ currentPassword, newPassword }) }));
      setCurrentPassword(""); setNewPassword(""); setConfirm("");
      onSaved({ ...profile, sessionCount: 1 });
      setMessage("密码已更新，其他登录会话已退出");
    });
  };
  return <dialog className="studio-dialog account-dialog" ref={dialog} aria-labelledby="account-title" closedby="closerequest"
    onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
    <div className="studio-dialog-head"><div><p className="studio-eyebrow">YOUR ACCOUNT</p><h2 id="account-title">账户设置</h2></div><button className="studio-icon-button" type="button" aria-label="关闭账户设置" autoFocus disabled={Boolean(busy)} onClick={onClose}><X /></button></div>
    <div className="account-tabs" role="group" aria-label="账户设置分类">
      <button type="button" aria-pressed={tab === "profile"} disabled={Boolean(busy)} onClick={() => { setTab("profile"); setError(""); setMessage(""); }}><User />个人资料</button>
      <button type="button" aria-pressed={tab === "security"} disabled={Boolean(busy)} onClick={() => { setTab("security"); setError(""); setMessage(""); }}><ShieldCheck />账号安全</button>
    </div>
    <div className="studio-dialog-body">
      {tab === "profile" ? <>
        <div className="account-avatar-editor"><OwnerAvatar profile={profile} /><div><strong>你的个人头像</strong><p>JPG、PNG 或 WebP，最大 2 MB</p><div className="account-avatar-actions"><label className={`studio-button ${busy ? "is-disabled" : ""}`}><Camera />更换头像<input type="file" accept="image/png,image/jpeg,image/webp" disabled={Boolean(busy)} onChange={(event) => {
          const file = event.target.files?.[0]; event.target.value = "";
          if (!file) return;
          if (file.size > 2 * 1024 * 1024) { setError("头像不能超过 2 MB"); return; }
          void run("avatar", async () => { const body = new FormData(); body.append("file", file); onSaved(await readJson<OwnerProfile>(fetch("/api/auth/avatar", { method: "POST", body }))); setMessage("头像已更新"); });
        }} /></label>{profile.avatarUrl && <button className="studio-text-button" type="button" disabled={Boolean(busy)} onClick={() => void run("avatar", async () => { onSaved(await readJson<OwnerProfile>(fetch("/api/auth/avatar", { method: "DELETE" }))); setMessage("已恢复默认头像"); })}>移除头像</button>}</div></div></div>
        <form className="account-form" onSubmit={saveProfile}>
          <label>显示名称<input value={displayName} maxLength={64} disabled={Boolean(busy)} onChange={(event) => setDisplayName(event.target.value)} placeholder="你希望如何被称呼" /></label>
          <label>登录账号<input value={username} required maxLength={64} pattern=".*\S.*" autoComplete="username" disabled={Boolean(busy)} onChange={(event) => setUsername(event.target.value)} /><small>修改后，下次使用新账号登录。</small></label>
          <label>个人简介<textarea value={bio} rows={3} maxLength={200} disabled={Boolean(busy)} onChange={(event) => setBio(event.target.value)} placeholder="记录你的创作方向" /><small>{bio.length} / 200</small></label>
          <button className="studio-button studio-button-primary" disabled={Boolean(busy)}>{busy === "profile" ? <SpinnerGap className="spin" /> : <CheckCircle />}保存资料</button>
        </form>
      </> : <>
        <form className="account-form" onSubmit={savePassword}>
          <div className="account-section-title"><LockKey /><div><h3>修改登录密码</h3><p>至少 6 位。修改后其他设备会退出登录。</p></div></div>
          <label>当前密码<input type="password" autoComplete="current-password" required minLength={6} maxLength={256} value={currentPassword} disabled={Boolean(busy)} onChange={(event) => setCurrentPassword(event.target.value)} /></label>
          <label>新密码<input type="password" autoComplete="new-password" required minLength={6} maxLength={256} value={newPassword} disabled={Boolean(busy)} onChange={(event) => setNewPassword(event.target.value)} /></label>
          <label>确认新密码<input type="password" autoComplete="new-password" required minLength={6} maxLength={256} value={confirm} disabled={Boolean(busy)} onChange={(event) => setConfirm(event.target.value)} /></label>
          <button className="studio-button studio-button-primary" disabled={Boolean(busy)}>{busy === "password" ? <SpinnerGap className="spin" /> : <ShieldCheck />}更新密码</button>
        </form>
        <section className="account-sessions"><div><h3>登录会话</h3><p>当前有 {profile.sessionCount} 个有效登录会话。退出登录不会停止生成任务。</p></div><div className="account-session-actions"><button className="studio-button" disabled={Boolean(busy)} onClick={() => void run("logout", async () => { await readJson(fetch("/api/auth/logout", { method: "POST" })); window.dispatchEvent(new Event("h3:logout")); })}><SignOut />退出当前登录</button>{!confirmLogout && <button className="studio-text-button" disabled={Boolean(busy)} onClick={() => setConfirmLogout(true)}>退出全部会话</button>}</div>{confirmLogout && <div className="account-logout-confirm"><p>退出全部会话，包括当前浏览器？</p><button className="studio-button studio-button-danger" disabled={Boolean(busy)} onClick={() => void run("sessions", async () => { await readJson(fetch("/api/auth/logout-all", { method: "POST" })); window.dispatchEvent(new Event("h3:logout")); })}>确认全部退出</button><button className="studio-text-button" disabled={Boolean(busy)} onClick={() => setConfirmLogout(false)}>取消</button></div>}</section>
      </>}
      {busy === "avatar" && <p className="account-feedback" role="status"><SpinnerGap className="spin" />正在保存头像…</p>}
      {error && <p className="account-feedback error" role="alert">{error}</p>}
      {message && <p className="account-feedback success" role="status"><CheckCircle />{message}</p>}
    </div>
  </dialog>;
}
