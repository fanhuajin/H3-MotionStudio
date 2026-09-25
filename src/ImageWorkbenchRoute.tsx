import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check, ClipboardText, ImageSquare, SpinnerGap, UploadSimple, WarningCircle,
} from "@phosphor-icons/react";
import { readJson } from "./api";

type Asset = { name: string; width: number; height: number; bytes: number } | null;
/** 后端 image_workbench.AUTOMATIONS 里的单条状态（list_items 会带出来）。 */
type AutomationState = { status?: string; stage?: string; message?: string; startedAt?: number } | null;
type WorkItem = {
  id: string; folderName: string; title: string; copy: string;
  kind: "singing" | "dance" | null;
  assets: { frame: Asset; identity: Asset; person: Asset; bilibili: Asset; douyin: Asset };
  automation?: AutomationState;
};
type Stage = "person" | "bilibili" | "douyin";

/** 自动化任务的活跃状态：只要还有一条在这个集合里，页面就继续轮询。 */
const AUTO_LIVE = ["queued", "running"];

const STAGES: Array<{ id: Stage; label: string; spec: string }> = [
  { id: "person", label: "人物图", spec: "图一场景 + 图二五官" },
  { id: "bilibili", label: "B站封面", spec: "4:3 · 1448×1086" },
  { id: "douyin", label: "抖音封面", spec: "3:4 · 1086×1448" },
];

function firstMissingStage(item: WorkItem): Stage {
  if (!item.assets.person) return "person";
  if (!item.assets.bilibili) return "bilibili";
  if (!item.assets.douyin) return "douyin";
  return "person";
}

function assetUrl(item: WorkItem, key: keyof WorkItem["assets"]) {
  return `/api/image-workbench/${item.id}/asset/${key}?v=${item.assets[key]?.bytes || 0}`;
}

export function ImageWorkbenchRoute() {
  const [items, setItems] = useState<WorkItem[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [stage, setStage] = useState<Stage>("person");
  const [kind, setKind] = useState<"singing" | "dance" | null>(null);
  const [filter, setFilter] = useState<"pending" | "all">("pending");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  /** 每条自动化任务的本地开始时刻（后端不给百分比，就显示「已用 N 秒」避免看着像卡死）。 */
  const autoClock = useRef<Record<string, number>>({});

  const refresh = useCallback(async () => {
    const data = await readJson<{ items: WorkItem[] }>(fetch("/api/image-workbench/items"), "读取发布目录失败");
    setItems(data.items);
    setSelectedId((current) => current && data.items.some((item) => item.id === current) ? current : data.items.find((row) => !(row.assets.person && row.assets.bilibili && row.assets.douyin))?.id || data.items[0]?.id || "");
  }, []);

  useEffect(() => { refresh().catch((error) => setNotice(String(error.message || error))); }, [refresh]);
  const item = items.find((row) => row.id === selectedId) || null;
  const chosenKind = kind || item?.kind || null;
  const complete = (row: WorkItem) => Boolean(row.assets.person && row.assets.bilibili && row.assets.douyin);
  const visible = useMemo(() => filter === "pending" ? items.filter((row) => !complete(row)) : items, [filter, items]);
  const finished = items.filter(complete).length;
  /** 有自动化任务在跑就持续刷新，直到全部落定 —— 页面上的进度就靠这个更新。 */
  const autoLive = items.some((row) => AUTO_LIVE.includes(row.automation?.status || ""));

  useEffect(() => {
    if (!autoLive) return;
    const timer = window.setInterval(() => {
      refresh().catch(() => { /* 下一轮再试，瞬时失败不该中断轮询 */ });
    }, 2000);
    return () => window.clearInterval(timer);
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
        : "已开始自动生成这条作品缺的人物图与封面。");
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
      const data = await readJson<{ queued: number }>(
        fetch("/api/image-workbench/automatic-all", { method: "POST" }),
        "一键生成全部失败",
      );
      setNotice(data.queued ? `已把 ${data.queued} 条缺图作品排上队列，系统会一条一条跑。` : "没有缺图的作品。");
      await refresh();
    } catch (error) {
      setNotice(String((error as Error).message || error));
    } finally {
      setBusy(false);
    }
  }, [busy, refresh]);

  useEffect(() => {
    const selected = items.find((row) => row.id === selectedId);
    if (selected) setStage(firstMissingStage(selected));
  }, [selectedId]);

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
    if (!item) return;
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
        <div><span className="image-workbench-kicker">第三方图片接口</span><h1>图片与封面</h1><p>人物图先生成一张，放大前用 DS 检查双手；手部不合格时最多定向修改一次并复检。身份低分不会自动重做，封面使用最终人物图。</p></div>
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
          {!item ? <div className="image-workbench-blank"><ImageSquare /><p>发布目录里暂时没有可处理的作品。</p></div> : <>
            <div className="image-workbench-title"><div><small>{item.folderName}</small><h2>{item.title}</h2></div><div className="image-auto-controls"><span>{item.kind === "dance" ? "跳舞 · 9:16" : item.kind === "singing" ? "唱歌 · 4:3" : "类型未识别"}</span><button onClick={() => startAuto(item.id)} disabled={busy}>本条三张一次跑完</button></div></div>
            {item.automation && AUTO_LIVE.includes(item.automation.status || "") && <p className="image-auto-progress"><SpinnerGap className="spin" />{item.automation.message || "正在自动生成…"}{elapsedOf(item) ? `（已用 ${elapsedOf(item)} 秒）` : ""}</p>}
            {item.automation?.status === "completed" && item.automation.message && <p className="image-auto-done"><Check weight="bold" />{item.automation.message}</p>}
            {item.automation?.status === "failed" && <p className="image-auto-failed"><WarningCircle />{item.automation.message || "自动生成失败"}</p>}
            <div className="image-stage-tabs">{STAGES.map((entry, index) => { const ready = Boolean(item.assets[entry.id]); return <button key={entry.id} className={stage === entry.id ? "active" : ""} onClick={() => setStage(entry.id)}><i>{ready ? <Check weight="bold" /> : index + 1}</i><span><strong>{entry.label}</strong><small>{entry.spec}</small></span></button>; })}</div>

            <div className="image-workbench-content">
              <div className={`image-workbench-preview ${stage === "douyin" ? "portrait" : ""}`} onClick={() => stageAsset && copyImage(stage, STAGES.find((row) => row.id === stage)?.label || "图片")} role={stageAsset ? "button" : undefined} tabIndex={stageAsset ? 0 : undefined} title={stageAsset ? "点击复制图片" : undefined}>
                {stageAsset ? <><img src={assetUrl(item, stage)} alt={STAGES.find((row) => row.id === stage)?.label} /><em><ClipboardText />点击复制</em></> : <div><ImageSquare /><strong>还没有这张图</strong><small>自己出好后拖到右侧上传</small></div>}
                <span>{stageAsset ? `${stageAsset.width} × ${stageAsset.height}` : "等待生成"}</span>
              </div>
              <div className="image-workbench-actions">
                <div className="image-workflow-note"><strong>怎么使用</strong><ol><li>点上方按钮用第三方接口自动出这张图</li><li>也可以点下方参考图复制，到别处自己出图</li><li>出好的图直接粘贴、拖入或选择上传，会自动保存并登记</li></ol></div>
                {stage === "person" && <div className="image-reference-pair"><figure onClick={() => copyImage("frame", "图一场景造型")} role="button" tabIndex={0} title="点击复制图一">{item.assets.frame ? <img src={assetUrl(item, "frame")} alt="图一抽帧" /> : <div>缺少抽帧</div>}<figcaption><ClipboardText />图一 · 点击复制</figcaption></figure><figure onClick={() => copyImage("identity", "图二固定五官")} role="button" tabIndex={0} title="点击复制图二">{item.assets.identity ? <img src={assetUrl(item, "identity")} alt="图二固定五官" /> : <div>缺少参考</div>}<figcaption><ClipboardText />图二 · 点击复制</figcaption></figure></div>}
                {stage !== "person" && <div className="image-cover-reference"><strong>生成这张封面使用的人物图</strong><figure onClick={() => copyImage("person", "人物图")} role="button" tabIndex={0} title="点击复制人物图">{item.assets.person ? <img src={assetUrl(item, "person")} alt="人物图" /> : <div><ImageSquare />缺少人物图</div>}<figcaption><ClipboardText />人物图 · 点击复制</figcaption></figure></div>}
                <div className="image-action-grid">
                  <button
                    className="primary"
                    onClick={() => startAuto(item.id, stage)}
                    disabled={busy || (stage !== "person" && !item.assets.person)}
                    title={stage !== "person" && !item.assets.person ? "请先生成人物图，再做封面" : undefined}
                  ><SpinnerGap className={busy ? "spin" : undefined} />{stageAsset ? "重新生成这一步" : "API 自动生成这一步"}</button>
                  <button onClick={() => void copyPrompt(item.id, stage)}><ClipboardText />复制当前提示词</button>
                </div>
                <label className="image-drop-zone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); const file = event.dataTransfer.files[0]; if (file) upload(file); }}>
                  <input ref={inputRef} type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => { const file = event.target.files?.[0]; if (file) upload(file); }} />
                  {busy ? <SpinnerGap className="spin" /> : <UploadSimple />}<strong>{stageAsset ? "粘贴、拖入或选择图片替换" : "按 Ctrl+V 粘贴，或拖入、选择生成好的图片"}</strong><small>{stage === "person" ? "保存为 人物图.png" : `自动校验并保存为 ${stage === "bilibili" ? "B站4x3.png" : "抖音3x4.png"}`}</small>
                </label>
              </div>
            </div>
          </>}
        </div>
      </section>
    </main>
  );
}
