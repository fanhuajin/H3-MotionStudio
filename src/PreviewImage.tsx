import { useState, type CSSProperties } from "react";
import { ArrowClockwise, SpinnerGap, WarningCircle } from "@phosphor-icons/react";

export function PreviewImage(props: { src: string; alt: string; style?: CSSProperties; retry?: boolean }) {
  return <ImageRequest key={props.src} {...props} />;
}

function ImageRequest({ src, alt, style, retry = true }: {
  src: string; alt: string; style?: CSSProperties; retry?: boolean;
}) {
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [attempt, setAttempt] = useState(0);
  return <span className="preview-image-stage" aria-busy={state === "loading"}>
    <img key={attempt} src={src} alt={alt} draggable={false} decoding="async"
      style={{ ...style, visibility: state === "ready" ? "visible" : "hidden" }}
      onLoad={() => setState("ready")} onError={() => setState("error")} />
    {state !== "ready" && <span className="preview-image-status" role="status">
      {state === "error" ? <WarningCircle /> : <SpinnerGap className="batch-video-spinner" />}
      <strong>{state === "error" ? "图片暂时无法加载" : "正在加载图片…"}</strong>
      {state === "error" && (retry ? <button type="button" onPointerDown={(event) => event.stopPropagation()}
        onClick={(event) => { event.stopPropagation(); setState("loading"); setAttempt((value) => value + 1); }}>
        <ArrowClockwise />重新加载
      </button> : <small>点击打开大图重试</small>)}
    </span>}
  </span>;
}
