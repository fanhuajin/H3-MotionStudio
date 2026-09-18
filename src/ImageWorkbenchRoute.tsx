import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check, ClipboardText, ImageSquare, SpinnerGap, UploadSimple, WarningCircle,
} from "@phosphor-icons/react";
import { readJson } from "./api";

type Asset = { name: string; width: number; height: number; bytes: number } | null;
type WorkItem = {
  id: string; folderName: string; title: string; copy: string;
  kind: "singing" | "dance" | null;
  assets: { frame: Asset; identity: Asset; person: Asset; bilibili: Asset; douyin: Asset };
};
type Stage = "person" | "bilibili" | "douyin";

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
  const [watchId, setWatchId] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    const data = await readJson<{ items: WorkItem[] }>(fetch("/api/image-workbench/items"), "读取发布目录失败");
    setItems(data.items);
    setSelectedId((current) => current && data.items.some((item) => item.id === current) ? current : data.items.find((row) => !(row.assets.person && row.assets.bilibili && row.assets.douyin))?.id || data.items[0]?.id || "");
  }, []);

  useEffect(() => { refresh().catch((error) => setNotice(String(error.message || error))); }, [refresh]);
  useEffect(() => {
    if (!watchId) return;
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch(`/api/image-workbench/watch/${watchId}`, { cache: "no-store" });
        if (!response.ok) return;
        const state = await response.json() as { status?: string; message?: string };
        if (["completed", "invalid", "timeout", "error"].includes(state.status || "")) {
          setNotice(state.message || "下载监听已结束。");
          setWatchId("");
          if (state.status === "completed") await refresh();
        }
      } catch { /* The next poll retries; a transient refresh error should not stop the watcher. */ }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [watchId, refresh]);
  const item = items.find((row) => row.id === selectedId) || null;
  const chosenKind = kind || item?.kind || null;
  const complete = (row: WorkItem) => Boolean(row.assets.person && row.assets.bilibili && row.assets.douyin);
  const visible = useMemo(() => filter === "pending" ? items.filter((row) => !complete(row)) : items, [filter, items]);
  const finished = items.filter(complete).length;

  useEffect(() => {
    const selected = items.find((row) => row.id === selectedId);
    if (selected) setStage(firstMissingStage(selected));
  }, [selectedId]);

  const copyPrompt = async () => {
    if (!item) return;
    if (stage === "person" && !chosenKind) { setNotice("系统无法从任务记录确定唱歌/跳舞类型，请手动选择。"); return; }
    setBusy(true); setNotice("");
    try {
      const data = await readJson<{ prompt: string }>(fetch(`/api/image-workbench/${item.id}/prompt/${stage}?kind=${chosenKind}`));
      await navigator.clipboard.writeText(data.prompt);
      setNotice("提示词已复制。请在 ChatGPT 上传素材包中的图片后粘贴使用。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "复制失败"); }
    finally { setBusy(false); }
  };

  const openWebChatGPT = async () => {
    if (!item) return;
    if (stage === "person" && !chosenKind) { setNotice("系统无法从任务记录确定唱歌/跳舞类型，请手动选择。"); return; }
    if (stage !== "person" && !item.assets.person) { setNotice("请先生成并保存人物图，再做封面。"); return; }
    const chat = window.open("https://chatgpt.com/", "_blank");
    setBusy(true); setNotice("");
    try {
      const activeKind = chosenKind || item.kind || "singing";
      const promptData = await readJson<{ prompt: string }>(fetch(`/api/image-workbench/${item.id}/prompt/${stage}?kind=${activeKind}`));
      await navigator.clipboard.writeText(promptData.prompt);
      const watch = await readJson<{ watchId: string }>(fetch(`/api/image-workbench/${item.id}/watch/${stage}?kind=${activeKind}`, { method: "POST" }), "启动下载监听失败");
      setWatchId(watch.watchId);
      setNotice(chat ? "已打开网页 ChatGPT，提示词已复制。粘贴素材与提示词后生图；点击下载即可自动保存。" : "提示词已复制，下载监听已启动。请手动打开 chatgpt.com，粘贴素材与提示词后生图。点击下载即可自动保存。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "准备网页 ChatGPT 失败"); }
    finally { setBusy(false); }
  };

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
        <div><span className="image-workbench-kicker">网页 ChatGPT 辅助</span><h1>图片与封面</h1><p>先用抽帧和任务类型生成最终人物图，再由网页 ChatGPT 生成两张封面；下载后自动归档。</p></div>
        <div className="image-workbench-head-actions"><div className="image-workbench-stats"><strong>{finished}<small>已全部完成</small></strong><strong>{items.length - finished}<small>待处理</small></strong></div></div>
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
            <div className="image-workbench-title"><div><small>{item.folderName}</small><h2>{item.title}</h2></div><div className="image-auto-controls"><span>{item.kind === "dance" ? "跳舞 · 9:16" : item.kind === "singing" ? "唱歌 · 4:3" : "类型未识别"}</span></div></div>
            <div className="image-stage-tabs">{STAGES.map((entry, index) => { const ready = Boolean(item.assets[entry.id]); return <button key={entry.id} className={stage === entry.id ? "active" : ""} onClick={() => setStage(entry.id)}><i>{ready ? <Check weight="bold" /> : index + 1}</i><span><strong>{entry.label}</strong><small>{entry.spec}</small></span></button>; })}</div>

            <div className="image-workbench-content">
              <div className={`image-workbench-preview ${stage === "douyin" ? "portrait" : ""}`} onClick={() => stageAsset && copyImage(stage, STAGES.find((row) => row.id === stage)?.label || "图片")} role={stageAsset ? "button" : undefined} tabIndex={stageAsset ? 0 : undefined} title={stageAsset ? "点击复制图片" : undefined}>
                {stageAsset ? <><img src={assetUrl(item, stage)} alt={STAGES.find((row) => row.id === stage)?.label} /><em><ClipboardText />点击复制</em></> : <div><ImageSquare /><strong>还没有这张图</strong><small>从 ChatGPT 保存成图后拖到右侧</small></div>}
                <span>{stageAsset ? `${stageAsset.width} × ${stageAsset.height}` : "等待生成"}</span>
              </div>
              <div className="image-workbench-actions">
                <div className="image-workflow-note"><strong>怎么使用</strong><ol><li>点击下方参考图复制，直接粘贴到网页 ChatGPT</li><li>点击“打开网页 ChatGPT”会复制提示词并开始等待下载</li><li>在网页 ChatGPT 生图后点击下载，系统会自动校验并保存</li></ol></div>
                {stage === "person" && <div className="image-reference-pair"><figure onClick={() => copyImage("frame", "图一场景造型")} role="button" tabIndex={0} title="点击复制图一">{item.assets.frame ? <img src={assetUrl(item, "frame")} alt="图一抽帧" /> : <div>缺少抽帧</div>}<figcaption><ClipboardText />图一 · 点击复制</figcaption></figure><figure onClick={() => copyImage("identity", "图二固定五官")} role="button" tabIndex={0} title="点击复制图二">{item.assets.identity ? <img src={assetUrl(item, "identity")} alt="图二固定五官" /> : <div>缺少参考</div>}<figcaption><ClipboardText />图二 · 点击复制</figcaption></figure></div>}
                {stage !== "person" && <div className="image-cover-reference"><strong>生成这张封面使用的人物图</strong><figure onClick={() => copyImage("person", "人物图")} role="button" tabIndex={0} title="点击复制人物图">{item.assets.person ? <img src={assetUrl(item, "person")} alt="人物图" /> : <div><ImageSquare />缺少人物图</div>}<figcaption><ClipboardText />人物图 · 点击复制到 ChatGPT</figcaption></figure></div>}
                <div className="image-action-grid"><button className="primary" onClick={openWebChatGPT} disabled={busy}><ClipboardText />打开网页 ChatGPT</button><button onClick={copyPrompt} disabled={busy}>仅复制提示词</button></div>
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
