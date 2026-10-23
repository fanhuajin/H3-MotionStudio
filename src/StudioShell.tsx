import { useEffect, useRef, useState } from "react";
import type { MouseEvent, ReactNode } from "react";
import { CaretRight, ClosedCaptioning, DownloadSimple, Gauge, ImageSquare, Keyboard, ListChecks, MagnifyingGlass, MagnifyingGlassPlus, MicrophoneStage, MusicNotes, PersonSimpleRun, SidebarSimple, Sparkle, X } from "@phosphor-icons/react";
import { AccountSettings, OwnerAvatar } from "./AccountSettings";
import type { OwnerProfile } from "./AccountSettings";
import { LogoutButton } from "./AuthGate";
import { CloseProjectButton } from "./CloseProject";
import { SystemMonitor } from "./SystemMonitor";
import { readJson } from "./api";

const sections = [
  { label: "创作中心", items: [
    { path: "/batch", label: "批量制作", icon: ListChecks, description: "管理备料、出片与制作队列", keywords: "queue batch" },
    { path: "/images", label: "图片与封面", icon: ImageSquare, description: "人物图、封面与发布素材", keywords: "image cover" },
    { path: "/singing", label: "歌曲生成", icon: MusicNotes, description: "从演唱参考制作唱歌视频", keywords: "singing music" },
    { path: "/migrate", label: "动作迁移", icon: PersonSimpleRun, description: "将参考动作迁移到人物", keywords: "dance motion" },
  ] },
  { label: "后期工具", items: [
    { path: "/upscale", label: "二采放大", icon: MagnifyingGlassPlus, description: "提升视频清晰度与分辨率", keywords: "upscale" },
    { path: "/rvc", label: "音色转换", icon: MicrophoneStage, description: "转换人声音色", keywords: "rvc voice" },
    { path: "/subtitle", label: "生成字幕", icon: ClosedCaptioning, description: "识别语音并制作字幕", keywords: "subtitle" },
  ] },
  { label: "素材与发布", items: [
    { path: "/douyin", label: "抖音下载", icon: DownloadSimple, description: "下载和整理参考视频", keywords: "download douyin" },
    { path: "/publish", label: "批量发布", icon: ListChecks, description: "安排作品的多平台发布", keywords: "publish" },
  ] },
];
const destinations = sections.flatMap((section) => section.items.map((item) => ({ ...item, group: section.label })));

export function StudioShell({ path, onNavigate, onCloseProject, children }: {
  path: string; onNavigate: (event: MouseEvent<HTMLElement>) => void; onCloseProject: () => void; children: ReactNode;
}) {
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem("h3:sidebar-collapsed") === "true"; } catch { return false; } });
  const [profile, setProfile] = useState<OwnerProfile | null>(null);
  const [profileLoading, setProfileLoading] = useState(true);
  const [profileError, setProfileError] = useState("");
  const [settings, setSettings] = useState(false);
  const [search, setSearch] = useState("");
  const command = useRef<HTMLDialogElement>(null);
  const accountTrigger = useRef<HTMLElement | null>(null);
  const previousPath = useRef(path);
  const active = destinations.find((item) => item.path === path) || destinations[0];
  const matches = destinations.filter((item) => `${item.label} ${item.description} ${item.keywords}`.toLowerCase().includes(search.trim().toLowerCase()));
  const loadProfile = async () => {
    setProfileLoading(true);
    setProfileError("");
    try { setProfile(await readJson<OwnerProfile>(fetch("/api/auth/me", { cache: "no-store" }))); }
    catch { setProfileError("账户资料暂时无法加载"); }
    finally { setProfileLoading(false); }
  };
  useEffect(() => { void loadProfile(); }, []);
  useEffect(() => {
    if (previousPath.current === path) return;
    previousPath.current = path;
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    document.getElementById("studio-main")?.focus({ preventScroll: true });
  }, [path]);
  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        if (document.querySelector("dialog[open]")) return;
        event.preventDefault(); setSearch(""); command.current?.showModal();
      }
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, []);
  const toggle = () => setCollapsed((value) => { try { localStorage.setItem("h3:sidebar-collapsed", String(!value)); } catch { /* Optional preference. */ } return !value; });
  const openAccount = (event: MouseEvent<HTMLButtonElement>) => {
    if (!profile) { void loadProfile(); return; }
    accountTrigger.current = event.currentTarget;
    setSettings(true);
  };
  const closeAccount = () => {
    setSettings(false);
    requestAnimationFrame(() => { if (accountTrigger.current?.isConnected) accountTrigger.current.focus(); });
  };
  return <div className={`desktop-app-shell prism-workspace ${collapsed ? "sidebar-collapsed" : ""}`}>
    <a href="#studio-main" className="studio-skip-link">跳到工作区</a>
    <aside className="app-sidebar" onClick={onNavigate}>
      <div className="sidebar-top"><a className="sidebar-brand" href="/batch" aria-label="H3 MotionStudio 首页"><span className="studio-brand-mark" aria-hidden="true" /><span className="studio-brand-copy"><strong>MotionStudio</strong><small>PERSONAL CREATIVE STUDIO</small></span></a></div>
      <button className="studio-search-trigger" type="button" title="搜索工作区（Ctrl K）" aria-label="搜索工作区，快捷键 Ctrl K" onClick={() => { setSearch(""); command.current?.showModal(); }}><MagnifyingGlass /><span>搜索工作区</span><kbd>Ctrl K</kbd></button>
      <nav className="sidebar-navigation" aria-label="工作台导航">{sections.map((section) => <div className="studio-nav-section" key={section.label}><p className="sidebar-section-label">{section.label}</p>{section.items.map((item) => <a key={item.path} href={item.path} title={item.label} aria-label={item.label} aria-current={active.path === item.path ? "page" : undefined} className={`sidebar-nav-item ${active.path === item.path ? "active" : ""}`}><item.icon aria-hidden="true" weight="regular" /><span className="studio-nav-label">{item.label}</span></a>)}</div>)}</nav>
      <div className="sidebar-system">
        {!collapsed && <details className="studio-runtime"><summary><Gauge aria-hidden="true" /><span>资源监控</span><CaretRight aria-hidden="true" /></summary><SystemMonitor embedded /></details>}
        <button type="button" className="studio-owner-card" aria-label={profile ? "打开个人资料与账号设置" : "重新加载账户资料"} onClick={openAccount}><OwnerAvatar profile={profile} /><span className="studio-profile-copy"><strong>{profile?.displayName || "我的账户"}</strong><small>{profile ? "个人创作空间" : "正在连接账户"}</small></span><CaretRight /></button>
        {profileError && <p className="studio-account-error" role="status">{profileError} <button onClick={() => void loadProfile()}>重试</button></p>}
        <div className="sidebar-service-row"><LogoutButton /><CloseProjectButton onAccepted={onCloseProject} /></div>
        <footer><span className="studio-edition-dot" />PERSONAL EDITION</footer>
      </div>
    </aside>
    <div className="studio-main-shell">
      <header className="studio-topbar"><div className="studio-breadcrumb"><button type="button" className="studio-icon-button studio-sidebar-toggle" aria-label={collapsed ? "展开侧栏" : "收起侧栏"} aria-expanded={!collapsed} onClick={toggle}><SidebarSimple /></button><span className="studio-breadcrumb-root">创作空间</span><CaretRight /><strong>{active.label}</strong></div><div className="studio-topbar-actions"><span className="studio-private-badge"><Sparkle />个人工作台</span><button className="studio-topbar-profile" type="button" aria-busy={profileLoading} disabled={profileLoading} onClick={openAccount} aria-label="打开账户设置"><OwnerAvatar profile={profile} /><span>{profileLoading ? "正在加载…" : profile?.displayName || "我的账户"}</span></button></div></header>
      <div className="route-stage" key={path} id="studio-main" tabIndex={-1}>{children}</div>
    </div>
    <dialog ref={command} className="studio-dialog studio-command" aria-labelledby="command-title" onClick={(event) => { if (event.target === event.currentTarget) command.current?.close(); }}>
      <div className="studio-command-search"><MagnifyingGlass /><label className="studio-sr-only" htmlFor="studio-search">搜索工作区</label><input id="studio-search" autoFocus placeholder="搜索功能或工作区…" value={search} onChange={(event) => setSearch(event.target.value)} /><button className="studio-icon-button" aria-label="关闭搜索" onClick={() => command.current?.close()}><X /></button></div>
      <h2 className="studio-sr-only" id="command-title">快速前往工作区</h2><div className="studio-command-results">{matches.length ? matches.map((item) => <a href={item.path} key={item.path} onClick={(event) => { command.current?.close(); onNavigate(event); }}><item.icon /><span><strong>{item.label}</strong><small>{item.description}</small></span><small>{item.group}</small><CaretRight /></a>) : <p>没有找到相关功能，试试“封面”或“发布”。</p>}</div><footer><Keyboard />Tab 切换 · Enter 打开 · Esc 关闭</footer>
    </dialog>
    {settings && profile && <AccountSettings profile={profile} onSaved={setProfile} onClose={closeAccount} />}
  </div>;
}
