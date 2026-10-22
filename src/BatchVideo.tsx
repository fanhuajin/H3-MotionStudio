import { useRef, useState } from "react";
import { ArrowClockwise, SpinnerGap, WarningCircle } from "@phosphor-icons/react";

/** Keep the preview stage stable while the browser fetches the first frame. */
export function BatchVideo({ src, autoPlay = false }: { src: string; autoPlay?: boolean }) {
  const video = useRef<HTMLVideoElement>(null);
  const [ready, setReady] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [failed, setFailed] = useState(false);
  return (
    <div className={`batch-video-stage${autoPlay ? " large" : ""}`} aria-busy={!failed && (!ready || waiting)}>
      <video ref={video} src={src} controls={ready} autoPlay={autoPlay} playsInline preload="auto"
        aria-label={autoPlay ? "成片预览" : "本条源视频"}
        onLoadedData={() => { setReady(true); setWaiting(false); setFailed(false); }}
        onWaiting={() => setWaiting(true)}
        onPlaying={() => setWaiting(false)}
        onCanPlay={() => setWaiting(false)}
        onError={() => { setFailed(true); setWaiting(false); }} />
      {(failed || !ready || waiting) && (
        <div className={`batch-video-status${ready && !failed ? " buffering" : ""}`} role="status">
          {failed ? <WarningCircle /> : <SpinnerGap className="batch-video-spinner" />}
          <strong>{failed ? "视频暂时无法加载" : ready ? "正在缓冲…" : "正在加载视频…"}</strong>
          {!ready && !failed && <small>画面就绪后即可播放</small>}
          {failed && <button onClick={() => {
            setFailed(false); setReady(false); setWaiting(false); video.current?.load();
          }}><ArrowClockwise />重新加载</button>}
        </div>
      )}
    </div>
  );
}
