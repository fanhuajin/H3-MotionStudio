import { useEffect, useMemo, useRef, useState } from "react";
import { Camera, Check, ImageSquare, MagicWand, SpinnerGap, UploadSimple, VideoCamera } from "@phosphor-icons/react";

type Mode = "4:3" | "9:16";
type SourceMode = "video" | "image" | "none";
type Analysis = { summary: string; background: { decision: string; reason: string; proposal: string }; clothing: { decision: string; reason: string; proposal: string }; hair: string; makeup: string; accessories: string; pose: string; composition: string; risks: string[]; confirmedPrompt: string };
type DouyinJob = { id: string; status: string; title?: string; mediaUrl?: string; created_at?: string };

async function errorText(response: Response) {
  try { const data = await response.json(); return data.detail || data.error || `请求失败 (${response.status})`; }
  catch { return `请求失败 (${response.status})`; }
}

export function PortraitRoute() {
  const [mode, setMode] = useState<Mode>("4:3");
  const [sourceMode, setSourceMode] = useState<SourceMode>("video");
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [sourceUrl, setSourceUrl] = useState("");
  const [notes, setNotes] = useState("");
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [plan, setPlan] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState<"analyze" | "generate" | null>(null);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ name: string; url: string; path: string; mode: Mode } | null>(null);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [douyinJobs, setDouyinJobs] = useState<DouyinJob[]>([]);
  const videoRef = useRef<HTMLVideoElement>(null);

  const previewUrl = useMemo(() => sourceFile ? URL.createObjectURL(sourceFile) : sourceUrl, [sourceFile, sourceUrl]);
  useEffect(() => () => { if (sourceFile && previewUrl.startsWith("blob:")) URL.revokeObjectURL(previewUrl); }, [sourceFile, previewUrl]);
  useEffect(() => {
    fetch("/api/portrait/config").then(r => r.json()).then(v => setConfigured(Boolean(v.configured))).catch(() => setConfigured(false));
    fetch("/api/douyin/jobs").then(r => r.json()).then(v => setDouyinJobs((v.jobs || []).filter((j: DouyinJob) => j.status === "completed" && j.mediaUrl).slice(0, 8))).catch(() => undefined);
  }, []);

  function invalidate() { setAnalysis(null); setPlan(""); setConfirmed(false); setResult(null); setError(""); }
  function chooseMode(next: Mode) { setMode(next); invalidate(); }
  function chooseSource(next: SourceMode) { setSourceMode(next); setSourceFile(null); setSourceUrl(""); invalidate(); }
  function acceptFile(file: File | null) { if (!file) return; setSourceFile(file); setSourceUrl(""); invalidate(); }

  async function captureFrame() {
    const video = videoRef.current;
    if (!video || !video.videoWidth) return setError("请先播放到需要截取的画面。") as never;
    const canvas = document.createElement("canvas"); canvas.width = video.videoWidth; canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0);
    canvas.toBlob(blob => { if (blob) acceptFile(new File([blob], `视频截帧_${Math.round(video.currentTime * 10) / 10}s.png`, { type: "image/png" })); }, "image/png");
  }

  async function analyze() {
    if (sourceMode !== "none" && !sourceFile) return setError("请先选择参考图，视频模式下请截取当前帧。") as never;
    setBusy("analyze"); setError(""); setConfirmed(false);
    const form = new FormData(); form.append("mode", mode); form.append("notes", notes); if (sourceFile) form.append("style_image", sourceFile);
    try {
      const response = await fetch("/api/portrait/analyze", { method: "POST", body: form });
      if (!response.ok) throw new Error(await errorText(response));
      const data = await response.json(); setAnalysis(data.analysis); setPlan(data.analysis.confirmedPrompt || "");
    } catch (e) { setError(e instanceof Error ? e.message : "分析失败"); } finally { setBusy(null); }
  }

  async function generate() {
    if (!analysis || !confirmed || !plan.trim()) return setError("请先分析并明确确认方案。") as never;
    setBusy("generate"); setError("");
    const form = new FormData(); form.append("mode", mode); form.append("notes", notes); form.append("plan", plan); form.append("confirmed", "1"); if (sourceFile) form.append("style_image", sourceFile);
    try {
      const response = await fetch("/api/portrait/generate", { method: "POST", body: form });
      if (!response.ok) throw new Error(await errorText(response));
      setResult(await response.json());
    } catch (e) { setError(e instanceof Error ? e.message : "生成失败"); } finally { setBusy(null); }
  }

  async function handoff() {
    if (!result) return;
    sessionStorage.setItem("h3-motionstudio:portrait-handoff", JSON.stringify({ url: result.url, name: result.name, mode }));
    window.location.href = mode === "4:3" ? "/" : "/migrate";
  }

  return <main className="portrait-route">
    <header className="route-hero"><div><p className="route-eyebrow"><span /> AI · PORTRAIT STUDIO</p><h1>人物<em>定妆</em></h1><p className="route-description">选择唱歌或跳舞构图，自主决定是否参考视频画面；先分析并确认背景与服装，再生成。</p></div><div className={`portrait-api ${configured ? "ready" : "warning"}`}>{configured ? <><Check /> 图片接口已连接</> : <>图片接口待配置</>}</div></header>

    <section className="portrait-steps"><span className="active">01 选择</span><span className={analysis ? "done" : ""}>02 分析</span><span className={confirmed ? "done" : ""}>03 确认</span><span className={result ? "done" : ""}>04 生成</span></section>

    <section className="portrait-grid">
      <div className="portrait-card portrait-controls">
        <div className="portrait-section"><label>作品类型</label><div className="portrait-mode-row"><button className={mode === "4:3" ? "selected" : ""} onClick={() => chooseMode("4:3")}><strong>4:3 唱歌定妆</strong><small>近距离胸像 · 头顶贴边 · 自然唱歌口型</small></button><button className={mode === "9:16" ? "selected" : ""} onClick={() => chooseMode("9:16")}><strong>9:16 跳舞定妆</strong><small>竖版上半身 · 动作轮廓稳定 · 适合迁移</small></button></div></div>
        <div className="portrait-section"><label>造型来源</label><div className="portrait-source-row"><button className={sourceMode === "video" ? "selected" : ""} onClick={() => chooseSource("video")}><VideoCamera />视频取帧</button><button className={sourceMode === "image" ? "selected" : ""} onClick={() => chooseSource("image")}><ImageSquare />上传参考图</button><button className={sourceMode === "none" ? "selected" : ""} onClick={() => chooseSource("none")}><MagicWand />不使用参考图</button></div></div>

        {sourceMode === "video" && <div className="portrait-section"><label>参考视频</label>{douyinJobs.length > 0 && <select value={sourceUrl} onChange={e => { setSourceUrl(e.target.value); setSourceFile(null); invalidate(); }}><option value="">选择最近下载的视频</option>{douyinJobs.map(job => <option key={job.id} value={job.mediaUrl}>{job.title || job.id}</option>)}</select>}<label className="portrait-file"><UploadSimple />选择本地视频<input type="file" accept="video/*" onChange={e => { const f = e.target.files?.[0]; if (f) { setSourceUrl(URL.createObjectURL(f)); setSourceFile(null); invalidate(); } }} /></label>{previewUrl && !sourceFile && <><video ref={videoRef} src={previewUrl} controls crossOrigin="anonymous" /><button className="secondary-action" onClick={captureFrame}><Camera />截取当前帧</button></>}{sourceFile && <img className="portrait-style-preview" src={previewUrl} alt="截取的造型参考帧" />}</div>}
        {sourceMode === "image" && <div className="portrait-section"><label>图一 · 造型与场景参考</label><label className="portrait-file"><UploadSimple />选择参考图片<input type="file" accept="image/*" onChange={e => acceptFile(e.target.files?.[0] || null)} /></label>{sourceFile && <img className="portrait-style-preview" src={previewUrl} alt="造型参考图" />}</div>}
        {sourceMode === "none" && <div className="portrait-empty"><MagicWand /><strong>本次只使用人物原型图</strong><span>AI 将根据用途和补充要求先提出完整造型方案。</span></div>}
        <div className="portrait-section"><label>本次补充要求</label><textarea value={notes} onChange={e => { setNotes(e.target.value); invalidate(); }} placeholder="例如：冷色室内氛围、深蓝色服装；留空则由 AI 推荐。" /></div>
        <button className="primary-action" disabled={Boolean(busy)} onClick={analyze}>{busy === "analyze" ? <SpinnerGap className="spin" /> : <MagicWand />}只分析，不生成</button>
      </div>

      <div className="portrait-card portrait-review">
        <div className="identity-strip"><img src="/api/portrait/identity" alt="固定人物原型图" /><div><span>唯一身份锚点</span><strong>人物原型图</strong><small>只决定人物是谁，不决定服装与背景</small></div></div>
        <div className="portrait-guide"><img src={`/api/portrait/guides/${mode === "4:3" ? "4x3" : "9x16"}`} alt={`${mode} 构图范例`} /><div><span>构图范例</span><strong>{mode === "4:3" ? "唱歌胸像近景" : "跳舞竖版人物"}</strong><small>只定义画面距离和人物占比，不参与身份识别</small></div></div>
        {!analysis ? <div className="portrait-wait"><span>02</span><strong>等待造型分析</strong><p>系统不会在你确认背景和衣服之前生成图片。</p></div> : <>
          <div className="analysis-summary"><span>分析结论</span><strong>{analysis.summary}</strong></div>
          <div className="decision-grid"><article><header><span>背景</span><b>{analysis.background.decision}</b></header><p>{analysis.background.reason}</p><small>{analysis.background.proposal}</small></article><article><header><span>衣服</span><b>{analysis.clothing.decision}</b></header><p>{analysis.clothing.reason}</p><small>{analysis.clothing.proposal}</small></article></div>
          <div className="analysis-details"><p><b>发型</b>{analysis.hair}</p><p><b>妆容</b>{analysis.makeup}</p><p><b>配饰</b>{analysis.accessories}</p><p><b>动作</b>{analysis.pose}</p><p><b>构图</b>{analysis.composition}</p></div>
          <label className="plan-label">最终确认方案<textarea value={plan} onChange={e => { setPlan(e.target.value); setConfirmed(false); }} /></label>
          <label className="confirm-row"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /><span>我已确认背景、服装和完整方案，可以开始生成</span></label>
          <button className="primary-action" disabled={!confirmed || Boolean(busy)} onClick={generate}>{busy === "generate" ? <SpinnerGap className="spin" /> : <MagicWand />}确认并生成图片</button>
        </>}
        {error && <div className="portrait-error">{error}</div>}
      </div>
    </section>

    {result && <section className="portrait-result"><img src={result.url} alt="生成的人物定妆图" /><div><p className="route-eyebrow"><span /> GENERATION COMPLETE</p><h2>{mode === "4:3" ? "唱歌人物图已生成" : "跳舞人物图已生成"}</h2><p>{result.path}</p><div className="result-actions"><button className="primary-action" onClick={handoff}>{mode === "4:3" ? "用于唱歌生成" : "用于动作迁移"}</button><a className="secondary-action" href={result.url} download={result.name}>下载图片</a></div></div></section>}
  </main>;
}
