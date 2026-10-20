import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useRememberedTab } from "./useRememberedTab";
import { StudioBot } from "./StudioBot";
import {
  Check, ClipboardText, ImageSquare, MagnifyingGlassPlus, Minus, Plus, SpinnerGap, UploadSimple, WarningCircle, X,
} from "@phosphor-icons/react";
import { readJson } from "./api";

type Asset = { name: string; width: number; height: number; bytes: number; version?: string } | null;
/** 后端 image_workbench.AUTOMATIONS 里的单条状态（list_items 会带出来）。 */
type AutomationState = { status?: string; stage?: string; message?: string; startedAt?: number } | null;
type WorkItem = {
  id: string; folderName: string; title: string; copy: string;
  kind: "singing" | "dance" | null;
  assets: { frame: Asset; identity: Asset; person: Asset; coverPerson: Asset; bilibili: Asset; douyin: Asset };
  automation?: AutomationState;
  awaitingMaterials?: boolean;
  batchLive?: boolean;
  hasVideo?: boolean;
  canCover?: boolean;
};
type Stage = "person" | "bilibili" | "douyin";

/** 自动化任务的活跃状态：只要还有一条在这个集合里，页面就继续轮询。 */
const AUTO_LIVE = ["queued", "running"];

const STAGES: Array<{ id: Stage; label: string; spec: string }> = [
  { id: "person", label: "人物图", spec: "图一场景 + 图二五官" },
  { id: "bilibili", label: "B站封面", spec: "4:3 · 中转站原图" },
  { id: "douyin", label: "抖音封面", spec: "3:4 · 中转站原图" },
];

const identitySources = new Map<string, string>();

function assetUrl(item: WorkItem, key: keyof WorkItem["assets"]) {
  const version = String(item.assets[key]?.version || item.assets[key]?.bytes || 0);
  const url = `/api/image-workbench/${item.id}/asset/${key}?v=${version}`;
  if (key !== "identity") return url;
  if (!identitySources.has(version)) {
    identitySources.clear();
    identitySources.set(version, url);
  }
  return identitySources.get(version)!;
}

// 切换主页面时先恢复上次列表，再后台校对文件版本，避免等待目录扫描。
let cachedItems: WorkItem[] = [];
let cachedSelectedId = "";

function AssetPreview({ src, label, busy, onView, onCopy }: {
  src: string; label: string; busy: boolean; onView: () => void; onCopy: () => void;
}) {
  return <>
    <button type="button" className="image-view-trigger" aria-label={`放大查看${label}`} title="点击放大查看" onClick={onView}>
      <img src={`${src}&preview=true`} alt={label} draggable={false} decoding="async" />
    </button>
    <button type="button" className="image-hover-copy" aria-label={`复制${label}`} disabled={busy} onClick={onCopy}>
      <ClipboardText />复制图片
    </button>
  </>;
}

function ImageViewer({ src, label, onClose }: { src: string; label: string; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const changeZoom = useCallback((factor: number) => {
    setZoom((current) => Math.min(8, Math.max(0.25, current * factor)));
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog?.showModal();
    return () => {
      dialog?.close();
      document.body.style.overflow = previousOverflow;
    };
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1);
      changeZoom(Math.exp(-Math.max(-100, Math.min(100, delta)) * 0.003));
    };
    viewport.addEventListener("wheel", wheel, { passive: false });
    return () => viewport.removeEventListener("wheel", wheel);
  }, [changeZoom]);

  return createPortal(<dialog ref={dialogRef} className="workbench-image-viewer" aria-label={`${label}大图预览`}
    onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <header className="workbench-viewer-toolbar">
      <strong>{label}</strong>
      <div>
        <button type="button" aria-label="缩小" disabled={zoom <= 0.25} onClick={() => changeZoom(1 / 1.2)}><Minus /></button>
        <output aria-label="缩放比例">{Math.round(zoom * 100)}%</output>
        <button type="button" aria-label="放大" disabled={zoom >= 8} onClick={() => changeZoom(1.2)}><Plus /></button>
        <button type="button" onClick={() => { setZoom(1); setOffset({ x: 0, y: 0 }); }}>适应窗口</button>
        <button type="button" aria-label="关闭大图" autoFocus onClick={onClose}><X /></button>
      </div>
    </header>
    <div ref={viewportRef} className={`workbench-viewer-viewport ${dragging ? "dragging" : ""}`}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        dragRef.current = { x: event.clientX, y: event.clientY, left: offset.x, top: offset.y };
        setDragging(true);
      }}
      onPointerMove={(event) => {
        const drag = dragRef.current;
        if (drag) setOffset({ x: drag.left + event.clientX - drag.x, y: drag.top + event.clientY - drag.y });
      }}
      onLostPointerCapture={() => { dragRef.current = null; setDragging(false); }}
      onPointerUp={(event) => {
        dragRef.current = null; setDragging(false);
        if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
      }}>
      <img src={src} alt={label} draggable={false} style={{ transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})` }} />
    </div>
    <p className="workbench-viewer-hint">滚轮放大缩小 · 拖动查看细节 · Esc 关闭</p>
  </dialog>, document.body);
}

export function ImageWorkbenchRoute() {
  const [items, setItems] = useState<WorkItem[]>(() => cachedItems);
  const [selectedId, setSelectedId] = useState(() => cachedSelectedId);
  const [stage, setStage] = useRememberedTab<Stage>("h3-motionstudio:image-stage-tab:v1", ["person", "bilibili", "douyin"], "person");
  const [kind, setKind] = useState<"singing" | "dance" | null>(null);
  const [filter, setFilter] = useRememberedTab("h3-motionstudio:image-filter-tab:v1", ["pending", "all"] as const, "pending");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [viewer, setViewer] = useState<{ src: string; label: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  /** 每条自动化任务的本地开始时刻（后端不给百分比，就显示「已用 N 秒」避免看着像卡死）。 */
  const autoClock = useRef<Record<string, number>>({});
  const refreshInFlight = useRef<Promise<void> | null>(null);

  const refresh = useCallback(() => {
    if (refreshInFlight.current) return refreshInFlight.current;
    const request = (async () => {
      const data = await readJson<{ items: WorkItem[] }>(fetch("/api/image-workbench/items", { cache: "no-store" }), "读取发布目录失败");
      cachedItems = data.items;
      setItems(data.items);
      setSelectedId((current) => current && data.items.some((item) => item.id === current) ? current : data.items.find((row) => !(row.assets.person && row.assets.bilibili && row.assets.douyin))?.id || data.items[0]?.id || "");
    })();
    refreshInFlight.current = request.finally(() => { refreshInFlight.current = null; });
    return refreshInFlight.current;
  }, []);

  useEffect(() => { refresh().catch((error) => setNotice(String(error.message || error))); }, [refresh]);
  const item = items.find((row) => row.id === selectedId) || null;
  useEffect(() => { cachedSelectedId = selectedId; }, [selectedId]);
  const previewVersions = item ? STAGES.map(({ id }) => item.assets[id]?.version || item.assets[id]?.bytes || "").join(":") : "";
  useEffect(() => {
    if (!item) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      const index = items.findIndex((row) => row.id === selectedId);
      const candidates = [items[index + 1], items[index - 1]].filter(Boolean);
      const urls = candidates.filter((row) => row.assets[stage]).map((row) => assetUrl(row, stage));
      urls.push(...STAGES.filter(({ id }) => id !== stage && item.assets[id]).map(({ id }) => assetUrl(item, id)));
      // Sequential, bounded lightweight prefetch; never fetch whole batches or originals.
      for (const url of urls) {
        if (cancelled) break;
        await new Promise<void>((resolve) => {
          const image = new Image();
          image.decoding = "async";
          image.fetchPriority = "low";
          image.onload = image.onerror = () => resolve();
          image.src = `${url}&preview=true`;
        });
      }
    }, 700);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [selectedId, previewVersions, stage, items]);
  const chosenKind = kind || item?.kind || null;
  const complete = (row: WorkItem) => Boolean(row.assets.person && row.assets.bilibili && row.assets.douyin);
  const visible = useMemo(() => filter === "pending" ? items.filter((row) => !complete(row)) : items, [filter, items]);
  const finished = items.filter(complete).length;
  /** 活跃任务用于显示耗时；目录同步不依赖页面事先知道有任务。 */
  const autoLive = items.some((row) => row.batchLive || AUTO_LIVE.includes(row.automation?.status || ""));

  useEffect(() => {
    const sync = () => {
      if (document.visibilityState === "hidden") return;
      refresh().catch(() => { /* 下一轮再试，瞬时失败不该中断轮询 */ });
    };
    const timer = window.setInterval(sync, autoLive ? 2000 : 3500);
    window.addEventListener("focus", sync);
    document.addEventListener("visibilitychange", sync);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", sync);
      document.removeEventListener("visibilitychange", sync);
    };
  }, [autoLive, refresh]);

  // 记录/清除每条任务的开始时刻（每次刷新 items 都会重跑，正好跟着轮询走）。
  // 这只是**兜底**：真实开始时间以后端 `automation.startedAt` 为准 —— 切走再切回来时
  // 组件重建、这里的本地时刻会丢，光靠它「已用 N 秒」会从 0 重新数（用户 2026-09-19 问过）。
  useEffect(() => {
    const now = Date.now();
    for (const row of items) {
      const live = AUTO_LIVE.includes(row.automation?.status || "");
      if (live && !autoClock.current[row.id]) autoClock.current[row.id] = now;
      if (!live) delete autoClock.current[row.id];
    }
  }, [items]);

  /** 这一条当前这一步已经跑了多少秒（后端给了 startedAt 就用它，切页/刷新都不会归零）。 */
  const elapsedOf = (row: WorkItem) => {
    const started = row.automation?.startedAt;
    if (started) return Math.max(0, Math.round(Date.now() / 1000 - started));
    const local = autoClock.current[row.id];
    return local ? Math.max(0, Math.round((Date.now() - local) / 1000)) : 0;
  };

  // 「已用 N 秒」要 1 秒 1 秒地走：轮询是每 2 秒一次，光靠它秒数会一格格跳
  // （用户 2026-09-19：「已用多少秒 1秒 1秒的跳」）。这里挂一个独立心跳只做重渲染。
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!autoLive) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [autoLive]);

  /** 用第三方图片接口自动出图。
   *
   *  - 单步（`stageArg`）：**总是带 `force=true`** —— 用户点这一下就是要「重新生成这一张」。
   *    不带 force 的话后端看到文件已存在会直接跳过、只在页面上回一句「已完成生成」，
   *    用户会以为按钮坏了（2026-09-19 反馈：「已完成生成 如果我点击之后需要运行我重新生成」）。
   *  - 整条（不带 `stageArg`）：不 force，只补齐缺的那几张，不无谓重做。
   */
  const startAuto = useCallback(async (itemId: string, stageArg?: Stage) => {
    if (busy) return;
    setBusy(true);
    try {
      const label = STAGES.find((row) => row.id === stageArg)?.label || stageArg;
      const query = stageArg ? `?stage=${stageArg}&force=true` : "";
      await readJson(
        fetch(`/api/image-workbench/${itemId}/automatic${query}`, { method: "POST" }),
        "启动自动生成失败",
      );
      setNotice(stageArg
        ? `已开始重新生成${label}（会用新图覆盖现有那张）。`
        : "已开始补齐这条的缺图；封面会在成片后从封面人物图生成。");
      await refresh();
    } catch (error) {
      setNotice(String((error as Error).message || error));
    } finally {
      setBusy(false);
    }
  }, [busy, refresh]);

  const copyPrompt = useCallback(async (itemId: string, step: Stage) => {
    try {
      const data = await readJson<{ prompt: string }>(
        fetch(`/api/image-workbench/${itemId}/prompt/${step}`), "读取提示词失败",
      );
      await navigator.clipboard.writeText(data.prompt);
      setNotice(`已复制${STAGES.find((row) => row.id === step)?.label || "图片"}当前使用的提示词。`);
    } catch (error) {
      setNotice(String((error as Error).message || error));
    }
  }, []);

  /** 把所有缺图的作品排上队列（后端严格一条一条跑，不并发）。 */
  /** 停止自动出图队列：正在跑的那一步跑完就停，排队的立刻取消。 */
  const stopAuto = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      const data = await readJson<{ cancelled: number }>(
        fetch("/api/image-workbench/automatic-stop", { method: "POST" }),
        "停止失败",
      );
      setNotice(`已请求停止：正在跑的那一步会跑完，另有 ${data.cancelled} 条排队已取消。`);
      await refresh();
    } catch (error) {
      setNotice(String((error as Error).message || error));
    } finally {
      setBusy(false);
    }
  }, [busy, refresh]);

  const startAllMissing = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      const data = await readJson<{ queued: number; waiting?: number }>(
        fetch("/api/image-workbench/automatic-all", { method: "POST" }),
        "一键生成全部失败",
      );
      setNotice(
        data.queued
          ? `已把 ${data.queued} 条缺图作品排上队列${data.waiting ? `；另有 ${data.waiting} 条等成片后出封面` : ""}。`
          : data.waiting
            ? `没有可立即生成的：${data.waiting} 条等成片后从封面人物图出封面。`
            : "没有缺图的作品。",
      );
      await refresh();
    } catch (error) {
      setNotice(String((error as Error).message || error));
    } finally {
      setBusy(false);
    }
  }, [busy, refresh]);

  const copyImage = async (key: keyof WorkItem["assets"], label: string) => {
    if (!item?.assets[key]) { setNotice(`还没有${label}。`); return; }
    setBusy(true); setNotice("");
    try {
      const response = await fetch(assetUrl(item, key));
      if (!response.ok) throw new Error(`读取${label}失败`);
      const source = await createImageBitmap(await response.blob());
      const canvas = document.createElement("canvas");
      canvas.width = source.width; canvas.height = source.height;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("浏览器无法处理图片");
      context.drawImage(source, 0, 0); source.close();
      const png = await new Promise<Blob>((resolve, reject) => canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("图片转换失败")), "image/png"));
      await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
      setNotice(`${label}已复制，可以直接粘贴到 ChatGPT。`);
    } catch (error) { setNotice(error instanceof Error ? error.message : "复制图片失败"); }
    finally { setBusy(false); }
  };

  const upload = async (file: File) => {
    if (!item || busy) return;
    if (stage === "person" && !chosenKind) { setNotice("请先选择人物图属于唱歌还是跳舞，再保存成图。"); return; }
    const body = new FormData(); body.append("file", file);
    setBusy(true); setNotice("正在校验并保存……");
    try {
      await readJson(fetch(`/api/image-workbench/${item.id}/upload/${stage}?kind=${chosenKind}`, { method: "POST", body }), "保存失败");
      setNotice(stage === "person" ? "人物图已保存到对应作品目录。" : "封面已校验尺寸并保存到对应作品目录。");
      await refresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : "保存失败"); }
    finally { setBusy(false); if (inputRef.current) inputRef.current.value = ""; }
  };

  useEffect(() => {
    const paste = (event: ClipboardEvent) => {
      if (busy || !item) return;
      const image = Array.from(event.clipboardData?.files || []).find((file) => file.type.startsWith("image/"));
      if (!image) return;
      event.preventDefault();
      setNotice("已读取剪贴板图片，正在校验并保存……");
      upload(image).catch(() => undefined);
    };
    document.addEventListener("paste", paste);
    return () => document.removeEventListener("paste", paste);
  }, [busy, item, stage, chosenKind]);

  const stageAsset = item?.assets[stage];
  return (
    <main className="image-workbench-page">
      {notice && <div className="image-workbench-toast" role="status" aria-live="polite"><WarningCircle />{notice}</div>}
      <header className="image-workbench-head">
        <div><span className="image-workbench-kicker">第三方图片接口</span><h1>图片与封面</h1><p>人物图生成后直接采用；封面用成片逐帧挑出的「封面人物图」排入标题与简介：有手看手部清晰度，无手看画面清晰度；已有图片直接沿用。</p></div>
        <div className="image-workbench-head-actions">
          <button className="primary" onClick={startAllMissing} disabled={busy}>一键生成全部缺图</button>
          {autoLive && <button className="danger" onClick={stopAuto} disabled={busy}>停止（队列 {items.filter((row) => AUTO_LIVE.includes(row.automation?.status || "")).length} 条）</button>}
          <div className="image-workbench-stats"><strong>{finished}<small>已全部完成</small></strong><strong>{items.length - finished}<small>待处理</small></strong></div>
        </div>
      </header>

      <section className="image-workbench-shell">
        <aside className="image-workbench-list">
          <div className="image-workbench-list-head"><strong>发布作品</strong><div><button className={filter === "pending" ? "active" : ""} onClick={() => { setFilter("pending"); const first = items.find((row) => !complete(row)); if (first) { setSelectedId(first.id); setKind(null); } }}>待处理</button><button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>全部</button></div></div>
          <div className="image-workbench-items">
            {visible.map((row) => <button key={row.id} className={row.id === selectedId ? "active" : ""} onClick={() => { setSelectedId(row.id); setKind(null); setNotice(""); }}><span>{complete(row) ? <Check weight="bold" /> : <ImageSquare />}</span><div><strong>{row.title}</strong><small>{row.folderName}</small></div><em>{[row.assets.person, row.assets.bilibili, row.assets.douyin].filter(Boolean).length}/3</em></button>)}
            {!visible.length && <p className="image-workbench-empty">当前没有待处理作品。</p>}
          </div>
        </aside>

        <div className="image-workbench-main">
          {!item ? <div className="image-workbench-blank"><ImageSquare /><p>发布目录里暂时没有可处理的作品。</p></div> : item.awaitingMaterials ? <>
            <div className="image-workbench-title"><div><small>{item.folderName}</small><h2>{item.title}</h2></div></div>
            <div className="image-workbench-blank"><ImageSquare /><p>已同步批量制作记录，等待抽帧、人物图与文案就绪。</p><small>素材就绪后自动更新，可继续生成或查看封面。</small></div>
          </> : <>
            <div className="image-workbench-title"><div><small>{item.folderName}</small><h2>{item.title}</h2></div><div className="image-auto-controls"><span>{item.kind === "dance" ? "跳舞 · 9:16" : item.kind === "singing" ? "唱歌 · 4:3" : "类型未识别"}</span><button onClick={() => startAuto(item.id)} disabled={busy}>补齐这条的缺图</button></div></div>
            {item.automation && AUTO_LIVE.includes(item.automation.status || "") && <p className="image-auto-progress"><StudioBot mood="working" variant="swirl" size={42} />{item.automation.message || "正在自动生成…"}{elapsedOf(item) ? `（已用 ${elapsedOf(item)} 秒）` : ""}</p>}
            {item.automation?.status === "completed" && item.automation.message && <p className="image-auto-done"><StudioBot mood="success" size={42} />{item.automation.message}</p>}
            {item.automation?.status === "failed" && <p className="image-auto-failed"><StudioBot mood="error" variant="exclaim" size={42} />{item.automation.message || "自动生成失败"}</p>}
            <div className="image-stage-tabs">{STAGES.map((entry, index) => { const ready = Boolean(item.assets[entry.id]); return <button key={entry.id} className={stage === entry.id ? "active" : ""} onClick={() => setStage(entry.id)}><i>{ready ? <Check weight="bold" /> : index + 1}</i><span><strong>{entry.label}</strong><small>{entry.spec}</small></span></button>; })}</div>

            <div className="image-workbench-content">
              <div className={`image-workbench-preview image-copy-surface ${stage === "douyin" ? "portrait" : ""}`}
                aria-busy={busy}
                onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = busy ? "none" : "copy"; }}
                onDrop={(event) => { event.preventDefault(); const file = event.dataTransfer.files[0]; if (file && !busy) void upload(file); }}>
                <input ref={inputRef} type="file" hidden accept="image/png,image/jpeg,image/webp" disabled={busy}
                  onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file); }} />
                {stageAsset ? <AssetPreview src={assetUrl(item, stage)} label={STAGES.find((row) => row.id === stage)?.label || "图片"} busy={busy}
                  onView={() => setViewer({ src: assetUrl(item, stage), label: STAGES.find((row) => row.id === stage)?.label || "图片" })}
                  onCopy={() => void copyImage(stage, STAGES.find((row) => row.id === stage)?.label || "图片")} />
                  : <button className="image-preview-upload-empty" disabled={busy} onClick={() => inputRef.current?.click()}>
                    <StudioBot mood={autoLive ? "working" : "idle"} variant={autoLive ? "thinking" : "egg"} size={104} />
                    <strong>{busy ? "正在处理……" : "还没有这张图"}</strong><small>点击选择图片，或拖到这里、按 Ctrl+V 粘贴</small>
                  </button>}
                <div className="image-preview-upload-tools">
                  <button disabled={busy} onClick={() => inputRef.current?.click()}>{busy ? <SpinnerGap className="spin" /> : <UploadSimple />}{stageAsset ? "替换图片" : "选择图片"}</button>
                  <small>{stage === "person" ? "保存为 人物图.png" : `自动校验并保存为 ${stage === "bilibili" ? "B站4x3.png" : "抖音3x4.png"}`}</small>
                  {stageAsset && <small>支持拖入或 Ctrl+V 粘贴替换</small>}
                </div>
                <span>{stageAsset ? `${stageAsset.width} × ${stageAsset.height}` : "等待生成"}</span>
              </div>
              <div className="image-workbench-actions">
                <div className="image-workflow-note"><strong>怎么使用</strong><ol><li>点上方按钮用第三方接口自动出这张图</li><li>图片点击可放大；悬浮后点复制按钮，到别处自己出图</li><li>在左侧图片区域粘贴、拖入或选择图片，会自动保存并登记</li></ol></div>
                {stage === "person" && <div className="image-reference-pair">{([
                  { key: "frame", label: "图一抽帧", caption: "图一", missing: "缺少抽帧" },
                  { key: "identity", label: "图二固定五官", caption: "图二", missing: "缺少参考" },
                ] as const).map(({ key, label, caption, missing }) => <figure key={key} className="image-copy-surface">
                  {item.assets[key] ? <AssetPreview src={assetUrl(item, key)} label={label} busy={busy}
                    onView={() => setViewer({ src: assetUrl(item, key), label })} onCopy={() => void copyImage(key, label)} /> : <div>{missing}</div>}
                  <figcaption><MagnifyingGlassPlus />{caption} · 点击查看</figcaption>
                </figure>)}</div>}
                {stage !== "person" && <div className="image-cover-reference"><strong>封面参考：封面人物图（有手看手部、无手看画面清晰度）</strong><figure className="image-copy-surface">
                  {item.assets.coverPerson ? <AssetPreview src={assetUrl(item, "coverPerson")} label="封面人物图" busy={busy}
                    onView={() => setViewer({ src: assetUrl(item, "coverPerson"), label: "封面人物图" })} onCopy={() => void copyImage("coverPerson", "封面人物图")} />
                    : <div><ImageSquare />{item.hasVideo ? "将从成片自动抽帧生成" : "还没有封面人物图（出片后自动生成）"}</div>}
                  <figcaption><MagnifyingGlassPlus />封面人物图 · 点击查看</figcaption></figure></div>}
                <div className="image-action-grid">
                  <button
                    className="primary"
                    onClick={() => startAuto(item.id, stage)}
                    disabled={busy || (stage !== "person" && !item.canCover)}
                    title={stage !== "person" && !item.canCover ? "请先出片；封面人物图从成片抽帧生成（也可以手动上传封面）" : undefined}
                  ><SpinnerGap className={busy ? "spin" : undefined} />{stageAsset ? "重新生成这一步" : "API 自动生成这一步"}</button>
                  <button onClick={() => void copyPrompt(item.id, stage)}><ClipboardText />复制当前提示词</button>
                </div>

              </div>
            </div>
          </>}
        </div>
      </section>
      {viewer && <ImageViewer key={viewer.src} {...viewer} onClose={() => setViewer(null)} />}
    </main>
  );
}
