import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { CheckCircle, FolderOpen, Pause, Play, Plus, Stop, Trash, X } from "@phosphor-icons/react";
import "./publish.css";

const platforms = ["douyin", "xiaohongshu", "bilibili"] as const;
const previewPlatforms = ["douyin", "bilibili", "xiaohongshu"] as const;
type Platform = typeof platforms[number];
const labels: Record<Platform, string> = { douyin: "抖音", xiaohongshu: "小红书", bilibili: "B站" };
type Detail = { error?: string; stage?: string; requested_time?: string; publish_at?: string; submitted?: boolean; manual?: boolean };
type Work = { id: string; folder: string; name: string; date: string; hour: string; scheduled: boolean;
  states: Record<Platform, string>; details: Partial<Record<Platform, Detail>>; assets: Record<string, boolean> };
type Queue = { items: Work[]; phase: string; notice: string; defaultDirectory: string; dependenciesReady: boolean };
type Candidate = { folder: string; name: string; assets: Record<string, boolean> };
type Modal = { kind: "add" } | { kind: "preview"; id: string } | { kind: "schedule"; ids: string[] }
  | { kind: "review"; id: string; platform: Platform } | { kind: "remove"; ids: string[] }
  | { kind: "start"; platform?: Platform };

async function api<T>(path = "", body?: unknown): Promise<T> {
  const response = await fetch(`/api/publish${path}`, body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "操作未完成，请检查输入后重试。");
  return value as T;
}

function Dialog({ title, children, footer, close, className = "", compactHeader = false }: { title: string; children: ReactNode; footer?: ReactNode; close: () => void; className?: string; compactHeader?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => { ref.current?.close(); previous?.focus({ preventScroll: true }); };
  }, []);
  return <dialog ref={ref} className={`batch-settings-dialog publish-dialog ${className}`} aria-labelledby="publish-dialog-title"
    onCancel={event => { event.preventDefault(); close(); }} onClick={event => {
      if (event.target !== event.currentTarget) return;
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
    }}>
    <header className="batch-settings-header">{!compactHeader && <span className="batch-modal-icon"><FolderOpen /></span>}
      <div>{!compactHeader && <p>批量发布</p>}<h2 id="publish-dialog-title">{title}</h2></div>
      <button className="batch-settings-close" aria-label="关闭弹窗" autoFocus onClick={close}><X /></button></header>
    <div className="batch-settings-scroll">{children}</div>
    {footer && <footer className="batch-settings-footer">{footer}</footer>}
  </dialog>;
}

function done(item: Work) { return platforms.every(p => ["已完成", "不发布"].includes(item.states[p])); }
function issue(item: Work) { return platforms.some(p => ["失败", "待核对"].includes(item.states[p])); }

function VideoPreview({ id }: { id: string }) {
  const [failed, setFailed] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  return <div className="publish-video"><video ref={video} controls playsInline preload="metadata" aria-label="作品视频预览"
    src={`/api/publish/items/${id}/asset/video`} onError={() => setFailed(true)} onLoadedData={() => setFailed(false)} />
    {failed && <p role="status">视频暂时无法加载。<button className="batch-add-toggle" onClick={() => { setFailed(false); video.current?.load(); }}>重新加载</button></p>}</div>;
}

function PublishTime({ item, disabled, save }: { item: Work; disabled: boolean; save: (changes: { date?: string; hour?: string }) => Promise<boolean> }) {
  const [date, setDate] = useState(item.date);
  const [feedback, setFeedback] = useState("");
  const saving = useRef(false);
  useEffect(() => setDate(item.date), [item.date]);
  async function commit(changes: { date?: string; hour?: string }) {
    if (disabled || saving.current) return;
    saving.current = true;
    setFeedback("保存中…");
    const saved = await save(changes);
    saving.current = false;
    setFeedback(saved ? "" : "保存失败，请重试");
    if (!saved && changes.date !== undefined) setDate(item.date);
  }
  return <div className="publish-inline-time">
    <input type="date" aria-label={`${item.name} 发布日期`} value={date} disabled={disabled}
      onChange={event => { setDate(event.target.value); setFeedback(""); }} onBlur={event => {
        if (!event.target.value || !event.target.validity.valid) { setDate(item.date); return; }
        if (date !== item.date) void commit({ date });
      }} />
    <select aria-label={`${item.name} 发布小时`} value={item.hour} disabled={disabled}
      onChange={event => void commit({ hour: event.target.value })}>
      {Array.from({ length: 24 }, (_, i) => String(i).padStart(2, "0")).map(hour => <option key={hour} value={hour}>{hour} 时</option>)}
    </select>
    <small role="status">{!item.scheduled && "立即发布"}{!item.scheduled && feedback && " · "}{feedback}</small>
  </div>;
}

export function PublishRoute() {
  const [queue, setQueue] = useState<Queue | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [modal, setModal] = useState<Modal | null>(null);
  const [directory, setDirectory] = useState("");
  const [paths, setPaths] = useState("");
  const [importPath, setImportPath] = useState("");
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [chosen, setChosen] = useState<string[]>([]);
  const [date, setDate] = useState("");
  const [hour, setHour] = useState("01");
  const [scheduled, setScheduled] = useState(true);
  const generation = useRef(0);
  const alive = useRef(true);
  const busyRef = useRef(false);
  const running = Boolean(queue && queue.phase !== "idle");
  const readonly = running || busy || !queue;
  const load = useCallback(async () => {
    if (busyRef.current) return;
    const current = ++generation.current;
    try {
      const result = await api<Queue>();
      if (alive.current && current === generation.current) setQueue(result);
    } catch (failure) {
      if (alive.current && current === generation.current) setError((failure as Error).message);
    }
  }, []);
  useEffect(() => {
    alive.current = true;
    void load();
    const interval = window.setInterval(() => void load(), 2000);
    return () => { alive.current = false; generation.current++; window.clearInterval(interval); };
  }, [load]);
  useEffect(() => {
    if (queue) setSelected(previous => previous.filter(id => queue.items.some(i => i.id === id)));
  }, [queue]);
  useEffect(() => {
    if (modal?.kind !== "preview" || !queue) return;
    const navigate = (event: KeyboardEvent) => {
      if (busy || event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.target instanceof Element && event.target.closest('select, input, textarea, [contenteditable]:not([contenteditable="false"]), [role="combobox"], [role="slider"]')) return;
      const offset = event.key === "ArrowUp" ? -1 : event.key === "ArrowDown" ? 1 : 0;
      if (!offset) return;
      event.preventDefault();
      const index = queue.items.findIndex(i => i.id === modal.id) + offset;
      if (index >= 0 && index < queue.items.length) setModal({ kind: "preview", id: queue.items[index].id });
    };
    // 捕获阶段接住播放器上的上下键，让它们切换作品；表单控件仍保留自己的方向键行为。
    document.addEventListener("keydown", navigate, true);
    return () => document.removeEventListener("keydown", navigate, true);
  }, [modal, queue, busy]);

  async function action(path: string, body: unknown, close = true) {
    if (busyRef.current) return false;
    busyRef.current = true;
    generation.current++;
    setBusy(true); setError("");
    try {
      const result = await api<Queue>(path, body);
      if (!alive.current) return false;
      if (result.items) setQueue(result);
      if (close) setModal(null);
      return true;
    } catch (failure) {
      if (alive.current) setError((failure as Error).message);
      return false;
    } finally {
      busyRef.current = false;
      if (alive.current) { setBusy(false); void load(); }
    }
  }
  async function pickFolders() {
    if (busyRef.current || running) return;
    busyRef.current = true;
    generation.current++;
    setBusy(true); setPickerOpen(true); setError("");
    try {
      const result = await api<{ folders: string[] }>("/pick", { directory: directory || queue?.defaultDirectory || "" });
      if (!alive.current || !result.folders.length) return;
      const updated = await api<Queue>("/add", { folders: result.folders });
      if (alive.current) { setQueue(updated); setModal(null); }
    } catch (failure) {
      if (alive.current) setError((failure as Error).message);
    } finally {
      busyRef.current = false;
      if (alive.current) { setBusy(false); setPickerOpen(false); void load(); }
    }
  }
  function schedule(ids: string[]) {
    const item = queue?.items.find(i => ids.includes(i.id));
    if (!item) return;
    setDate(item.date); setHour(item.hour); setScheduled(item.scheduled); setModal({ kind: "schedule", ids });
  }
  function platformState(item: Work, p: Platform) {
    const state = item.states[p];
    return state === "待核对" ? <button className="publish-review" disabled={readonly}
      onClick={() => setModal({ kind: "review", id: item.id, platform: p })}>待核对</button>
      : ["已完成", "发布中"].includes(state) ? <span className={`publish-state ${state === "已完成" ? "success" : ""}`}>{state === "已完成" && <CheckCircle />}{state}</span>
      : <select aria-label={`${item.name} ${labels[p]}发布状态`} value={state} disabled={readonly}
        className={state === "失败" ? "publish-failed" : ""} onChange={event => void action("/edit", { ids: [item.id], platform: p, state: event.target.value }, false)}>
        {state === "失败" && <option value="失败">失败</option>}<option>待发布</option><option>不发布</option></select>;
  }
  const items = queue?.items ?? [];
  const preview = modal?.kind === "preview" ? queue?.items.find(i => i.id === modal.id) : undefined;
  const review = modal?.kind === "review" ? queue?.items.find(i => i.id === modal.id) : undefined;
  const modalError = error && <p className="publish-error" role="alert">{error}</p>;
  const button = (text: string, onClick: () => void, disabled = false, icon?: ReactNode) =>
    <button className="batch-add-toggle" onClick={onClick} disabled={disabled}>{icon}{text}</button>;

  return <main className="batch-page publish-page"><div className="batch-content">
    <header className="batch-header"><div><p className="batch-kicker">MULTI-PLATFORM PUBLISHING</p>
      <h1>批量发布</h1><p>安排作品与时间，按抖音 → 小红书 → B站依次发布。</p></div>
      {queue && <span className="batch-status-pill">{running ? queue.phase === "paused" ? "已暂停" : "发布中" : "等待安排"} · 已处理 {queue.items.filter(done).length}/{queue.items.length}</span>}
    </header>
    {error && !modal && <p className="publish-error" role="alert">{error} {button("重新同步", () => void load(), busy)}</p>}
    {!queue ? <p role="status">正在读取发布队列…</p> : <>
      <section className="publish-notice" role="status"><strong>{queue.notice}</strong>
        <span>发布时保持平台登录，请勿操作鼠标键盘。Esc 可暂停；离开本页面后队列仍会继续。</span>
        {!queue.dependenciesReady && <span className="publish-error">发布环境尚缺桌面执行依赖，请安装 backend/requirements-publish.txt 中的依赖后运行。</span>}
      </section>
      <section className="publish-toolbar" aria-label="发布队列操作">
        <div className="publish-actions">
          {button(pickerOpen ? "正在选择作品…" : "添加作品", () => void pickFolders(), readonly, <Plus />)}
          {button("安排时间", () => schedule(selected), readonly || !selected.length)}
          {button("删除记录", () => setModal({ kind: "remove", ids: [...selected] }), readonly || !selected.length, <Trash />)}
          {running ? <>
            {button(queue.phase === "paused" ? "继续" : "暂停", () => void action(`/control/${queue.phase === "paused" ? "resume" : "pause"}`, {}, false), busy, queue.phase === "paused" ? <Play /> : <Pause />)}
            {button("停止发布", () => void action("/control/stop", {}, false), busy, <Stop />)}
          </> : button("开始 / 继续发布", () => setModal({ kind: "start" }), readonly || !queue.dependenciesReady || !queue.items.some(i => !done(i)), <Play />)}
        </div>
      </section>
      <div className="publish-table-wrap"><table className="publish-table"><thead><tr>
        <th><input type="checkbox" aria-label="选择全部作品" disabled={readonly || !items.length}
          checked={items.length > 0 && items.every(i => selected.includes(i.id))} onChange={event => setSelected(event.target.checked ? items.map(i => i.id) : [])} /></th>
        <th>序号</th><th>作品</th><th>发布时间</th>{platforms.map(p => <th key={p}>{labels[p]}</th>)}<th className="publish-ops">操作</th>
      </tr></thead><tbody>
        {!items.length && <tr><td colSpan={8} className="publish-empty">还没有发布作品，点击「添加作品」从发布目录选择。</td></tr>}
        {items.map(item => <tr key={item.id}>
          <td><input type="checkbox" aria-label={`选择${item.name}`} checked={selected.includes(item.id)} disabled={readonly}
            onChange={event => setSelected(event.target.checked ? [...selected, item.id] : selected.filter(id => id !== item.id))} /></td>
          <td className="batch-index">{String(queue.items.indexOf(item) + 1).padStart(3, "0")}</td>
          <td className="publish-work"><strong title={item.folder}>{item.name}</strong>
            <small>{!item.assets.video ? "缺视频 · " : ""}{!item.assets.copy ? "缺文案 · " : ""}{!item.assets.bilibili ? "缺横封面 · " : ""}{!item.assets.portrait ? "缺竖封面" : ""}</small>
            {issue(item) && <details><summary>查看处理信息</summary>{platforms.filter(p => item.details[p]).map(p =>
              <p key={p}>{labels[p]}：{item.details[p]?.error || item.details[p]?.stage || item.states[p]}</p>)}</details>}
          </td>
          <td><PublishTime item={item} disabled={readonly} save={changes => action("/edit", { ids: [item.id], ...changes }, false)} /></td>
          {platforms.map(p => <td key={p}>{platformState(item, p)}{item.details[p]?.publish_at && <small className="publish-actual">实际：{item.details[p]?.publish_at}</small>}</td>)}
          <td className="publish-ops"><div className="batch-row-ops">{button("查看视频", () => setModal({ kind: "preview", id: item.id }), running, <Play />)}</div></td>
        </tr>)}
      </tbody></table></div>
    </>}
    {modal?.kind === "add" && <Dialog title="添加作品" close={() => !busy && setModal(null)}>
      <p className="batch-settings-description">打开系统选择框后，可按 Ctrl / Shift 多选要发布的作品文件夹。确认后直接加入队列；取消不添加。已有记录自动跳过。</p>
      {button(pickerOpen ? "正在选择作品…" : "选择作品文件夹（可多选）", () => void pickFolders(), busy, <FolderOpen />)}
      <p>也可读取父目录或手动粘贴作品路径：</p>
      <label className="publish-field">作品父目录<input value={directory} onChange={e => setDirectory(e.target.value)} /></label>
      {button("读取作品目录", () => {
        if (busyRef.current) return;
        busyRef.current = true; setBusy(true); setError("");
        void api<{ items: Candidate[] }>("/scan", { directory }).then(result => {
          if (alive.current) { setCandidates(result.items); setChosen([]); }
        }).catch(failure => { if (alive.current) setError(failure.message); }).finally(() => {
          busyRef.current = false; if (alive.current) setBusy(false);
        });
      }, busy, <FolderOpen />)}
      {candidates.length > 0 && <div className="publish-candidates">
        <label><input type="checkbox" checked={chosen.length === candidates.length} onChange={e => setChosen(e.target.checked ? candidates.map(i => i.folder) : [])} />全选目录中的作品</label>
        {candidates.map(item => <label key={item.folder}><input type="checkbox" checked={chosen.includes(item.folder)}
          onChange={e => setChosen(e.target.checked ? [...chosen, item.folder] : chosen.filter(i => i !== item.folder))} />
          <span>{item.name}<small>{item.assets.video ? "视频就绪" : "缺少明确的视频"} · {item.assets.bilibili && item.assets.portrait ? "封面齐全" : "封面待补齐"}</small></span></label>)}
      </div>}
      <label className="publish-field">作品文件夹路径（每行一个）<textarea rows={3} value={paths} onChange={e => setPaths(e.target.value)} placeholder="D:\\作品\\001_跳舞_…" /></label>
      {button("加入发布队列", () => void action("/add", { folders: [...new Set([...chosen, ...paths.split(/\r?\n/).map(p => p.trim().replace(/^"|"$/g, "")).filter(Boolean)])] }), busy || (!chosen.length && !paths.trim()), <Plus />)}
      <details className="publish-import"><summary>导入原发布队列</summary><p>保留原平台状态和检查点，原队列文件不修改。重复目录保留当前记录。</p>
        <label className="publish-field">原 multi_publish_state.json 路径<input value={importPath} onChange={e => setImportPath(e.target.value)} /></label>
        {button("导入记录", () => void action("/import", { path: importPath }), busy || !importPath.trim())}</details>{modalError}
    </Dialog>}
    {modal?.kind === "schedule" && <Dialog title={`安排 ${modal.ids.length} 条作品的时间`} close={() => !busy && setModal(null)} footer={button("保存安排", () => void action("/edit", { ids: modal.ids, date, hour, scheduled }), busy || !date)}>
      <p className="batch-settings-description">时间只应用到尚未完成的平台。分钟沿用平台页面值；已发布稿件不受影响。</p>
      <label><input type="checkbox" checked={scheduled} onChange={e => setScheduled(e.target.checked)} />定时发布（北京时间）</label>
      <div className="publish-time-fields"><label className="publish-field">日期<input type="date" value={date} disabled={!scheduled} onChange={e => setDate(e.target.value)} /></label>
        <label className="publish-field">小时<select value={hour} disabled={!scheduled} onChange={e => setHour(e.target.value)}>{Array.from({ length: 24 }, (_, i) => String(i).padStart(2, "0")).map(h => <option key={h}>{h}</option>)}</select></label></div>
      <p>取消定时后，下一次运行将立即发布这些作品。</p>{modalError}
    </Dialog>}
    {modal?.kind === "preview" && preview && <Dialog title={preview.name} compactHeader className="batch-video-dialog publish-video-dialog" close={() => !busy && setModal(null)} footer={
      <div className="publish-preview-platforms">{previewPlatforms.map(p => <label key={p}>{labels[p]}{platformState(preview, p)}</label>)}</div>
    }>
      {preview.assets.video ? <VideoPreview key={preview.id} id={preview.id} /> : <p>未找到可预览的视频。</p>}
      {modalError}
    </Dialog>}
    {modal?.kind === "review" && review && <Dialog title={`核对${labels[modal.platform]}发布结果`} close={() => !busy && setModal(null)} footer={<>
      {button("确认未发布", () => void action("/edit", { ids: [review.id], platform: modal.platform, review: false }), busy)}
      {button("已发布成功", () => void action("/edit", { ids: [review.id], platform: modal.platform, review: true }), busy)}
    </>}><p>请先到{labels[modal.platform]}作品管理中核对《{review.name}》。已点击发布但未确认结果的作品不会自动重发。</p><p>{review.details[modal.platform]?.error}</p><p>尚未确认可关闭此窗口，保留待核对状态。</p>{modalError}</Dialog>}
    {modal?.kind === "remove" && <Dialog title="删除发布记录" close={() => !busy && setModal(null)} footer={button("确认删除记录", () => void action("/remove", { ids: modal.ids }), busy)}>
      <p>删除选中的 {modal.ids.length} 条记录，视频、封面和文案文件都会保留。</p><p>之后重新添加会作为新作品，请留意已有发布结果。</p>{modalError}</Dialog>}
    {modal?.kind === "start" && <Dialog title={modal.platform ? `运行${labels[modal.platform]}发布` : "开始批量发布"} close={() => !busy && setModal(null)} footer={button("开始发布", () => void action("/start", { platform: modal.platform }), busy, <Play />)}>
      <p>将处理{modal.platform ? labels[modal.platform] : "抖音、小红书和B站"}全部待发布和失败记录；已完成与不发布的作品会跳过。</p>
      <p>开始后会先检查抖音、小红书和B站页面，只打开缺少的页面；三个页面就绪后再发布。请保持平台已登录，开始后不要操作鼠标键盘。可按 Esc 暂停。取消定时的作品会立即发布。</p>{modalError}</Dialog>}
  </div></main>;
}
