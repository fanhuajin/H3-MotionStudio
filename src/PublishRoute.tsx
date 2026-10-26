import { ContentLoading } from "./ContentLoading";
import { useCallback, useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { CheckCircle, FolderOpen, Pause, Play, Plus, Stop, Trash, X } from "@phosphor-icons/react";
import "./publish.css";
import { useVideoPreviewWheel } from "./useVideoPreviewWheel";

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
  return <dialog closedby="closerequest" ref={ref} className={`batch-settings-dialog publish-dialog ${className}`} aria-labelledby="publish-dialog-title"
    onCancel={event => { event.preventDefault(); close(); }}>
    <header className="batch-settings-header">{!compactHeader && <span className="batch-modal-icon"><FolderOpen /></span>}
      <div>{!compactHeader && <p>批量发布</p>}<h2 id="publish-dialog-title">{title}</h2></div>
      <button className="batch-settings-close" aria-label="关闭弹窗" autoFocus onClick={close}><X /></button></header>
    <div className="batch-settings-scroll">{children}</div>
    {footer && <footer className="batch-settings-footer">{footer}</footer>}
  </dialog>;
}

function done(item: Work) { return platforms.every(p => ["已完成", "不发布"].includes(item.states[p])); }
function issue(item: Work) { return platforms.some(p => ["失败", "待核对"].includes(item.states[p])); }

function formatPublishTime(value: string) {
  const normalized = value.trim().replace(" ", "T");
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized) ? normalized : `${normalized}+08:00`;
  const date = new Date(zoned);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(date);
}

function openDatePicker(event: MouseEvent<HTMLInputElement>) {
  const input = event.currentTarget;
  if (event.button !== 0 || input.disabled || input.readOnly || typeof input.showPicker !== "function") return false;
  try {
    input.showPicker();
    // 日历成功打开后阻止鼠标默认选中年份段；键盘聚焦仍使用原生行为。
    event.preventDefault();
    return true;
  } catch {
    // 浏览器不允许打开时，保留原生日期输入和日历按钮的操作。
    return false;
  }
}

function VideoPreview({ id, children, hasVideo = true, saving, saveError }: { id: string; children: ReactNode; hasVideo?: boolean; saving: boolean; saveError: string }) {
  const [failed, setFailed] = useState(false);
  const [playBlocked, setPlayBlocked] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const player = video.current;
    if (!player) return;
    let disposed = false;
    void player.play().catch(() => { if (!disposed && !player.error) setPlayBlocked(true); });
    return () => {
      disposed = true;
      player.pause();
    };
  }, [id, hasVideo]);
  async function play() {
    const player = video.current;
    if (!player) return;
    try { await player.play(); setPlayBlocked(false); }
    catch { if (!player.error) setPlayBlocked(true); }
  }
  return <div className="publish-video">
    {hasVideo ? <video ref={video} controls autoPlay playsInline preload="metadata" aria-label="作品视频预览"
      src={`/api/publish/items/${id}/asset/video`} onPlaying={() => setPlayBlocked(false)}
      onError={() => { setFailed(true); setPlayBlocked(false); }} onLoadedData={() => setFailed(false)} /> : <p>未找到可预览的视频。</p>}
    <div className="publish-video-platforms" role="group" aria-label="此视频发布平台" aria-busy={saving}>
      {children}
      <p className={`publish-video-save ${saveError ? "publish-error" : ""}`} role={saveError ? "alert" : "status"}>{saveError || (saving ? "保存中…" : "")}</p>
    </div>
    {playBlocked && !failed && <div className="publish-video-play"><button className="batch-add-toggle" onClick={() => void play()}><Play />点击播放</button></div>}
    {failed && <p role="status">视频暂时无法加载。<button className="batch-add-toggle" onClick={() => { setFailed(false); video.current?.load(); void play(); }}>重新加载</button></p>}
  </div>;
}

function PublishTime({ item, disabled, save }: { item: Work; disabled: boolean; save: (changes: { date?: string; hour?: string }) => Promise<boolean> }) {
  const [date, setDate] = useState(item.date);
  const [feedback, setFeedback] = useState("");
  const saving = useRef(false);
  const pickerOpened = useRef(false);
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
      onMouseDown={event => { pickerOpened.current = openDatePicker(event); }}
      onKeyDown={() => { pickerOpened.current = false; }}
      onChange={event => {
        const value = event.target.value;
        setDate(value); setFeedback("");
        // 鼠标打开日历不依赖输入框聚焦，选定日期即保存；键盘编辑仍在失焦后保存。
        if (pickerOpened.current && value && event.target.validity.valid && value !== item.date) {
          pickerOpened.current = false;
          void commit({ date: value });
        }
      }} onBlur={event => {
        if (!event.target.value || !event.target.validity.valid) { setDate(item.date); return; }
        if (date !== item.date) void commit({ date });
      }} />
    <select aria-label={`${item.name} 发布小时`} value={item.hour} disabled={disabled}
      onChange={event => void commit({ hour: event.target.value })}>
      {Array.from({ length: 24 }, (_, i) => String(i).padStart(2, "0")).map(hour => <option key={hour} value={hour}>{hour} 时</option>)}
    </select>
    {(feedback || !item.scheduled) && <small role="status">{!item.scheduled && "立即发布"}{!item.scheduled && feedback && " · "}{feedback}</small>}
  </div>;
}

export function PublishRoute() {
  const [queue, setQueue] = useState<Queue | null>(null);
  const [error, setError] = useState("");
  const [actionFeedback, setActionFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [platformEdit, setPlatformEdit] = useState<{ id: string; platform: Platform; checked: boolean } | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [modal, setModal] = useState<Modal | null>(null);
  const [directory, setDirectory] = useState("");
  const [paths, setPaths] = useState("");
  const [importPath, setImportPath] = useState("");
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [chosen, setChosen] = useState<string[]>([]);
  const [date, setDate] = useState("");
  const generation = useRef(0);
  const alive = useRef(true);
  const busyRef = useRef(false);
  const running = Boolean(queue && queue.phase !== "idle");
  const completed = Boolean(queue?.items.length && queue.items.every(done));
  const readonly = running || busy || !queue;
  const load = useCallback(async () => {
    if (busyRef.current) return;
    const current = ++generation.current;
    try {
      const result = await api<Queue>();
      if (alive.current && current === generation.current) {
        setQueue(result);
        setActionFeedback("");
        setError("");
      }
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
      if (event.target instanceof Element) {
        // 平台勾选后焦点仍在 checkbox 上；上下键继续切视频，空格仍由原生控件处理。
        const platformCheckbox = event.target.matches('.publish-video-platforms input[type="checkbox"]');
        if (!platformCheckbox && event.target.closest('select, input, textarea, [contenteditable]:not([contenteditable="false"]), [role="combobox"], [role="slider"]')) return;
      }
      const offset = event.key === "ArrowUp" ? -1 : event.key === "ArrowDown" ? 1 : 0;
      if (!offset) return;
      event.preventDefault();
      const index = queue.items.findIndex(i => i.id === modal.id) + offset;
      if (index >= 0 && index < queue.items.length) setModal({ kind: "preview", id: queue.items[index].id });
    };
    // 播放器和平台勾选框上的上下键切换作品，其余表单保留自己的方向键行为。
    document.addEventListener("keydown", navigate, true);
    return () => document.removeEventListener("keydown", navigate, true);
  }, [modal, queue, busy]);

  async function action(path: string, body: unknown, close = true, showPageFeedback = true) {
    if (busyRef.current) return false;
    busyRef.current = true;
    generation.current++;
    setBusy(true); setError("");
    if (showPageFeedback) setActionFeedback("正在处理…");
    try {
      const result = await api<Queue>(path, body);
      if (!alive.current) return false;
      if (result.items) setQueue(result);
      if (showPageFeedback) setActionFeedback(path === "/control/stop" ? "停止指令已接收，正在等待当前操作安全结束…" : "操作已接收，正在同步状态…");
      if (close) setModal(null);
      return true;
    } catch (failure) {
      if (alive.current) { setError((failure as Error).message); setActionFeedback(""); }
      return false;
    } finally {
      busyRef.current = false;
      if (alive.current) { setBusy(false); void load(); }
    }
  }
  async function selectPreviewPlatform(item: Work, platform: Platform, checked: boolean) {
    if (busyRef.current) return;
    setPlatformEdit({ id: item.id, platform, checked });
    // 弹窗已有保存反馈，避免页面提示增删时推动背景列表。
    await action("/edit", { ids: [item.id], platform, state: checked ? "待发布" : "不发布" }, false, false);
    if (alive.current) setPlatformEdit(null);
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
    setDate(item.date); setModal({ kind: "schedule", ids });
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
  const scheduleChanged = modal?.kind === "schedule" && items.some(item => modal.ids.includes(item.id)
    && (item.date !== date || !item.scheduled));
  const preview = modal?.kind === "preview" ? queue?.items.find(i => i.id === modal.id) : undefined;
  const previewIndex = preview ? items.findIndex(item => item.id === preview.id) : -1;
  function switchPreview(offset: number) {
    const target = items[previewIndex + offset];
    if (!busy && previewIndex >= 0 && target) setModal({ kind: "preview", id: target.id });
  }
  useVideoPreviewWheel(Boolean(preview) && !busy, switchPreview);
  const review = modal?.kind === "review" ? queue?.items.find(i => i.id === modal.id) : undefined;
  const modalError = error && <p className="publish-error" role="alert">{error}</p>;
  const button = (text: string, onClick: () => void, disabled = false, icon?: ReactNode) =>
    <button className="batch-add-toggle" onClick={onClick} disabled={disabled}>{icon}{text}</button>;

  return <main className="batch-page publish-page"><div className="batch-content">
    <header className="batch-header"><div><p className="batch-kicker">MULTI-PLATFORM PUBLISHING</p>
      <h1>批量发布</h1><p>安排作品与时间，按抖音 → 小红书 → B站依次发布。</p></div>
      {queue && <span className="batch-status-pill">{running ? queue.phase === "paused" ? "已暂停" : "发布中" : completed ? "已全部完成" : "等待安排"} · 已处理 {queue.items.filter(done).length}/{queue.items.length}</span>}
    </header>
    {error && !modal && <p className="publish-error" role="alert">{error} {button("重新同步", () => void load(), busy)}</p>}
    {!queue ? (error ? null : <ContentLoading label="正在读取发布队列…" />) : <>
      <section className="publish-notice">
        <div className="publish-notice-text" role="status"><strong>{queue.notice}</strong>
          {actionFeedback && <span>{queue.phase === "idle" && actionFeedback.startsWith("停止指令") ? "发布已停止。" : actionFeedback}</span>}
          <span>发布时保持平台登录，请勿操作鼠标键盘。Esc 可暂停；离开本页面后队列仍会继续。</span>
          {!queue.dependenciesReady && <span className="publish-error">发布环境尚缺桌面执行依赖，请安装 backend/requirements-publish.txt 中的依赖后运行。</span>}
        </div>
        <div className="publish-toolbar publish-actions" role="group" aria-label="发布队列操作">
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
          {platforms.map(p => {
            const actual = item.details[p]?.publish_at;
            return <td key={p}>{platformState(item, p)}{actual && <small className="publish-actual" title="北京时间">实际：{formatPublishTime(actual)}</small>}</td>;
          })}
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
    {modal?.kind === "schedule" && <Dialog title={`安排 ${modal.ids.length} 条作品的时间`} close={() => !busy && setModal(null)} footer={button("保存安排", () => void action("/edit", { ids: modal.ids, date, scheduled: true }), busy || !date || !scheduleChanged)}>
      <p className="batch-settings-description">所选作品按此日期定时发布，每条原有小时保持不变。</p>
      <label className="publish-field">日期<input type="date" value={date} disabled={busy} onMouseDown={openDatePicker} onChange={e => setDate(e.target.value)} /></label>
      {modalError}
    </Dialog>}
    {modal?.kind === "preview" && preview && <Dialog title={preview.name}
      compactHeader className="batch-video-dialog publish-video-dialog" close={() => !busy && setModal(null)}>
      <VideoPreview key={preview.id} id={preview.id} hasVideo={preview.assets.video} saving={busy} saveError={error}>
        {previewPlatforms.map(p => {
          const state = preview.states[p];
          const locked = ["已完成", "发布中", "待核对"].includes(state);
          return <label key={p} title={locked ? `${labels[p]}：${state}` : undefined}>
            <input type="checkbox" aria-label={`${preview.name} 发布到${labels[p]}`}
              checked={platformEdit?.id === preview.id && platformEdit.platform === p ? platformEdit.checked : state !== "不发布"}
              disabled={running || locked} aria-disabled={busy || running || locked}
              onChange={event => void selectPreviewPlatform(preview, p, event.target.checked)} />{labels[p]}{locked && <small>{state}</small>}
          </label>;
        })}
      </VideoPreview>
      <div className="batch-video-navigation">
        <button disabled={busy || previewIndex <= 0} onClick={() => switchPreview(-1)}>↑ 上一条</button>
        <span role="status">{previewIndex + 1} / {items.length}</span>
        <button disabled={busy || previewIndex < 0 || previewIndex >= items.length - 1} onClick={() => switchPreview(1)}>↓ 下一条</button>
      </div>
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
