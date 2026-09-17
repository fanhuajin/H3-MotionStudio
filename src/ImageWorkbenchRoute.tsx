import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check, ClipboardText, ImageSquare, RocketLaunch, SpinnerGap, UploadSimple, WarningCircle,
} from "@phosphor-icons/react";
import { readJson } from "./api";

type Asset = { name: string; width: number; height: number; bytes: number } | null;
type WorkItem = {
  id: string; folderName: string; title: string; copy: string;
  kind: "singing" | "dance" | null;
  automation: { status: string; stage: string; message: string } | null;
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
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    const data = await readJson<{ items: WorkItem[] }>(fetch("/api/image-workbench/items"), "读取发布目录失败");
    setItems(data.items);
    setSelectedId((current) => current && data.items.some((item) => item.id === current) ? current : data.items.find((row) => !(row.assets.person && row.assets.bilibili && row.assets.douyin))?.id || data.items[0]?.id || "");
  }, []);

  useEffect(() => { refresh().catch((error) => setNotice(String(error.message || error))); }, [refresh]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (items.some((row) => row.automation && ["queued", "running"].includes(row.automation.status))) refresh().catch(() => undefined);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [items, refresh]);
  const item = items.find((row) => row.id === selectedId) || null;
  const chosenKind = kind || item?.kind || null;
  const complete = (row: WorkItem) => Boolean(row.assets.person && row.assets.bilibili && row.assets.douyin);
  const visible = useMemo(() => filter === "pending" ? items.filter((row) => !complete(row)) : items, [filter, items]);
  const finished = items.filter(complete).length;

  useEffect(() => {
    const selected = items.find((row) => row.id === selectedId);
    if (selected) setStage(firstMissingStage(selected));
  }, [selectedId]);

  const startAutomatic = async (folderId?: string) => {
    setBusy(true); setNotice("");
    try {
      if (folderId) {
        await readJson(fetch(`/api/image-workbench/${folderId}/automatic`, { method: "POST" }), "启动自动生成失败");
        setNotice("已加入自动生成队列：缺人物图会先生成2K人物图，然后自动补齐两张封面。");
      } else {
        const result = await readJson<{ queued: number }>(fetch("/api/image-workbench/automatic-all", { method: "POST" }), "启动自动补齐失败");
        setNotice(`已加入队列 ${result.queued} 个作品，将严格逐条生成。`);
      }
      await refresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : "启动失败"); }
    finally { setBusy(false); }
  };

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
      if (stage === "person" && !item.assets.bilibili) setStage("bilibili");
      else if (stage !== "douyin" && !item.assets.douyin) setStage("douyin");
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
        <div><span className="image-workbench-kicker">GPT-IMAGE-2.5 自动流水线</span><h1>图片与封面</h1><p>自动生成人物图，再根据最终人物图补齐 B站与抖音封面。</p></div>
        <div className="image-workbench-head-actions"><button onClick={() => startAutomatic()} disabled={busy || items.length === finished}>{busy ? <SpinnerGap className="spin" /> : <RocketLaunch />}自动补齐全部</button><div className="image-workbench-stats"><strong>{finished}<small>已全部完成</small></strong><strong>{items.length - finished}<small>待处理</small></strong></div></div>
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
            <div className="image-workbench-title"><div><small>{item.folderName}</small><h2>{item.title}</h2></div><div className="image-auto-controls"><span>{item.kind === "dance" ? "跳舞 · 9:16" : item.kind === "singing" ? "唱歌 · 4:3" : "类型未识别"}</span><button onClick={() => startAutomatic(item.id)} disabled={busy || ["queued", "running"].includes(item.automation?.status || "")}>{item.automation?.status === "running" ? <SpinnerGap className="spin" /> : <RocketLaunch />}{item.automation?.status === "running" ? "自动生成中" : "自动补齐这一条"}</button></div></div>
            <div className="image-stage-tabs">{STAGES.map((entry, index) => { const ready = Boolean(item.assets[entry.id]); return <button key={entry.id} className={stage === entry.id ? "active" : ""} onClick={() => setStage(entry.id)}><i>{ready ? <Check weight="bold" /> : index + 1}</i><span><strong>{entry.label}</strong><small>{entry.spec}</small></span></button>; })}</div>

            <div className="image-workbench-content">
              <div className={`image-workbench-preview ${stage === "douyin" ? "portrait" : ""}`} onClick={() => stageAsset && copyImage(stage, STAGES.find((row) => row.id === stage)?.label || "图片")} role={stageAsset ? "button" : undefined} tabIndex={stageAsset ? 0 : undefined} title={stageAsset ? "点击复制图片" : undefined}>
                {stageAsset ? <><img src={assetUrl(item, stage)} alt={STAGES.find((row) => row.id === stage)?.label} /><em><ClipboardText />点击复制</em></> : <div><ImageSquare /><strong>还没有这张图</strong><small>从 ChatGPT 保存成图后拖到右侧</small></div>}
                <span>{stageAsset ? `${stageAsset.width} × ${stageAsset.height}` : "等待生成"}</span>
              </div>
              <div className="image-workbench-actions">
                {item.automation && <div className={`image-auto-status ${item.automation.status}`}><SpinnerGap className={item.automation.status === "running" ? "spin" : ""} /><div><strong>{item.automation.status === "failed" ? "自动生成失败" : item.automation.status === "completed" ? "自动生成完成" : "自动流水线运行中"}</strong><small>{item.automation.message}</small></div></div>}
                <div className="image-workflow-note"><strong>怎么使用</strong><ol><li>点击图片即可复制，直接粘贴到 ChatGPT</li><li>点击复制提示词，再粘贴并生成</li><li>把生成好的图片拖回下方，自动保存到对应目录</li></ol></div>
                {stage === "person" && <div className="image-reference-pair"><figure onClick={() => copyImage("frame", "图一场景造型")} role="button" tabIndex={0} title="点击复制图一">{item.assets.frame ? <img src={assetUrl(item, "frame")} alt="图一抽帧" /> : <div>缺少抽帧</div>}<figcaption><ClipboardText />图一 · 点击复制</figcaption></figure><figure onClick={() => copyImage("identity", "图二固定五官")} role="button" tabIndex={0} title="点击复制图二">{item.assets.identity ? <img src={assetUrl(item, "identity")} alt="图二固定五官" /> : <div>缺少参考</div>}<figcaption><ClipboardText />图二 · 点击复制</figcaption></figure></div>}
                {stage !== "person" && <div className="image-cover-reference"><strong>生成这张封面使用的人物图</strong><figure onClick={() => copyImage("person", "人物图")} role="button" tabIndex={0} title="点击复制人物图">{item.assets.person ? <img src={assetUrl(item, "person")} alt="人物图" /> : <div><ImageSquare />缺少人物图</div>}<figcaption><ClipboardText />人物图 · 点击复制到 ChatGPT</figcaption></figure></div>}
                <div className="image-action-grid"><button className="primary" onClick={copyPrompt} disabled={busy}><ClipboardText />复制提示词</button></div>
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
