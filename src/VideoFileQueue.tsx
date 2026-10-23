import { useEffect, useRef, useState } from "react";
import { FolderOpen, Play, SpinnerGap, X } from "@phosphor-icons/react";
import { readJson } from "./api";
import { ContentLoading } from "./ContentLoading";
import type { JobState } from "./types";
import "./video-file-queue.css";

type Row = { id: string; file?: File; job?: JobState };
const active = (job?: JobState) => Boolean(job && ["queued", "waiting", "running", "cancelling"].includes(job.status));
const labels: Record<string, string> = {
  queued: "等待处理", waiting: "等待处理", running: "处理中", paused: "已暂停",
  completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断",
  cancelling: "正在取消",
};

export function VideoFileQueue({ kind, disabled, song = "", onView }: {
  kind: "upscale" | "rvc" | "subtitle"; disabled: boolean; song?: string;
  onView: (job: JobState) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const view = useRef(onView);
  view.current = onView;
  const generation = useRef(0);
  const viewedId = useRef("");
  const follow = useRef(true);
  const [rows, setRows] = useState<Row[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const pending = rows.filter(row => !row.job && row.file);
  const chosen = pending.filter(row => selected.includes(row.id));
  const targets = chosen.length ? chosen : pending;
  const running = rows.some(row => active(row.job));
  const locked = submitting || disabled || running;
  const allSelected = pending.length > 0 && chosen.length === pending.length;

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const refresh = async () => {
      const version = generation.current;
      try {
        const { jobs } = await readJson<{ jobs: JobState[] }>(fetch(`/api/jobs/file-queue/${kind}`, { cache: "no-store" }));
        if (cancelled || version !== generation.current) return;
        setLoadError("");
        setRows(previous => [...previous.filter(row => !row.job), ...jobs.map(job => ({ id: job.id, job }))]);
        const current = jobs.find(job => job.status === "running") || jobs.find(active);
        if (follow.current && current && current.id !== viewedId.current) {
          viewedId.current = current.id;
          view.current(current);
        }
      } catch (reason) {
        if (!cancelled && version === generation.current) setLoadError(reason instanceof Error ? reason.message : "文件队列读取失败");
      } finally {
        if (!cancelled) {
          setLoading(false);
          timer = window.setTimeout(() => void refresh(), 3000);
        }
      }
    };
    void refresh();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [kind]);

  const add = (files: FileList | null) => {
    if (!files) return;
    const valid = [...files].filter(file => /\.(mp4|mov|mkv|webm)$/i.test(file.name));
    setError(valid.length !== files.length ? "已忽略不支持的文件，请选择 MP4、MOV、MKV 或 WebM 视频。" : "");
    setRows(previous => {
      const known = new Set(previous.map(row => row.id));
      const additions = valid.map(file => ({ id: `${file.name}:${file.size}:${file.lastModified}`, file })).filter(row => {
        if (known.has(row.id)) return false;
        known.add(row.id); return true;
      });
      return [...previous, ...additions];
    });
  };

  const run = async () => {
    if (locked || !targets.length) return;
    setSubmitting(true); setError(""); generation.current++; follow.current = true;
    const submitted = targets.map(row => row.id);
    const form = new FormData();
    targets.forEach(row => form.append("videos", row.file!));
    if (kind === "subtitle") form.append("song_name", song.trim());
    try {
      const { jobs } = await readJson<{ jobs: JobState[] }>(fetch(`/api/jobs/file-queue/${kind}`, { method: "POST", body: form }));
      generation.current++;
      setRows(previous => [...previous.filter(row => !submitted.includes(row.id) && !row.job), ...jobs.map(job => ({ id: job.id, job }))]);
      setSelected(previous => previous.filter(id => !submitted.includes(id)));
      if (jobs[0]) { viewedId.current = jobs[0].id; view.current(jobs[0]); }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "提交失败，请重试");
    } finally { setSubmitting(false); }
  };

  return <section className="video-file-queue" aria-label="本地视频文件队列">
    <input ref={input} className="visually-hidden" type="file" multiple accept=".mp4,.mov,.mkv,.webm" onChange={event => { add(event.target.files); event.target.value = ""; }} />
    <div className="video-file-toolbar">
      <button type="button" className="batch-add-toggle" disabled={submitting} onClick={() => input.current?.click()}><FolderOpen />选择视频文件</button>
      <span>{rows.length ? `共 ${rows.length} 个 · 待提交 ${pending.length} 个 · 已选 ${chosen.length} 个` : "支持多选 MP4 / MOV / MKV / WebM"}</span>
      <button type="button" className="batch-add-toggle" disabled={locked || !targets.length || loading} onClick={() => void run()}>
        {submitting ? <SpinnerGap className="spin" /> : <Play />}{submitting ? "正在上传并创建队列…" : `${chosen.length ? "运行所选" : "全部运行"}（${targets.length}）`}
      </button>
    </div>
    {(error || loadError) && <p className="video-file-error" role="alert">{error || loadError}</p>}
    {loading ? <ContentLoading compact label="正在读取文件队列…" /> : rows.length ? <div className="video-file-table-wrap"><table>
      <thead><tr><th><input type="checkbox" aria-label="全选待提交文件" checked={allSelected} ref={node => { if (node) node.indeterminate = chosen.length > 0 && !allSelected; }} disabled={!pending.length || submitting} onChange={event => setSelected(event.target.checked ? pending.map(row => row.id) : [])} /></th><th>视频文件</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.id}>
        <td><input type="checkbox" aria-label={`选择 ${row.file?.name || row.job?.sourceName}`} checked={selected.includes(row.id)} disabled={Boolean(row.job) || submitting} onChange={event => setSelected(previous => event.target.checked ? [...previous, row.id] : previous.filter(id => id !== row.id))} /></td>
        <td><strong>{row.file?.name || row.job?.sourceName}</strong><small>{((row.file?.size || row.job?.sourceSize || 0) / 1024 / 1024).toFixed(1)} MB</small></td>
        <td><span role={active(row.job) ? "status" : undefined}>{row.job ? labels[row.job.status] : "待提交"}</span>{row.job?.errorSummary && <small className="video-file-error">{row.job.errorSummary}</small>}</td>
        <td>{row.job ? <button type="button" className="batch-add-toggle" onClick={() => { follow.current = false; viewedId.current = row.job!.id; onView(row.job!); }}>{row.job.finalReady ? "查看结果" : "查看进度"}</button> : <button type="button" className="video-file-remove" aria-label={`移除 ${row.file?.name}`} disabled={submitting} onClick={() => { setRows(previous => previous.filter(item => item.id !== row.id)); setSelected(previous => previous.filter(id => id !== row.id)); }}><X /></button>}</td>
      </tr>)}</tbody>
    </table></div> : <p className="video-file-empty">选择一个或多个视频文件，勾选后运行所选，未勾选时运行全部。</p>}
    {running && <p className="field-note" role="status">文件已交给后台逐条处理，离开页面不影响队列。单条失败后继续处理下一条。</p>}
  </section>;
}
