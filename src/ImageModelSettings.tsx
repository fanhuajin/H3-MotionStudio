import { useEffect, useState } from "react";
import { readJson } from "./api";
import "./image-model-settings.css";

export function ImageModelSettings() {
  const [model, setModel] = useState("");
  const [saved, setSaved] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true); setError("");
    void readJson<{ model: string }>(fetch("/api/image-settings"))
      .then((value) => { if (active) { setModel(value.model); setSaved(value.model); } })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "模型设置加载失败"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [reload]);
  return <form className="image-model-settings" onSubmit={(event) => {
    event.preventDefault();
    if (saving || loading || !saved || !model.trim()) return;
    setSaving(true); setError(""); setMessage("");
    void readJson<{ model: string }>(fetch("/api/image-settings", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }),
    })).then((value) => { setModel(value.model); setSaved(value.model); setMessage("模型已保存，后续新图片请求生效"); })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "模型保存失败"))
      .finally(() => setSaving(false));
  }}>
    <label htmlFor="image-model">图片模型</label>
    <input id="image-model" list="image-model-options" value={model} maxLength={128} required
      disabled={loading || saving || !saved} placeholder={loading ? "正在加载…" : "输入中转站模型名"}
      aria-describedby="image-model-help" onChange={(event) => { setModel(event.target.value); setMessage(""); }} />
    <datalist id="image-model-options">
      <option value="gpt-image-2" />
      <option value="gpt-image-2.5" />
      <option value="gpt-image-2.5-sunburst" />
    </datalist>
    <button className="studio-button" type="submit" disabled={loading || saving || !saved || !model.trim() || model.trim() === saved}>{saving ? "保存中…" : "保存模型"}</button>
    {!loading && !saved && <button className="studio-button" type="button" onClick={() => setReload((value) => value + 1)}>重新加载</button>}
    <small id="image-model-help">人物图、封面及批量制作共用；可选择或输入中转站支持的模型名。已提交任务沿用原模型。</small>
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
  </form>;
}
