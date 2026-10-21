import { useEffect, useState } from "react";
import { StudioBot } from "./StudioBot";
import type { StateId } from "./vendor/bloub/states";

// 每个工作区有独立招牌动作；短暂回到休息态，再完整播放下一轮。
const WORKSPACE_MOTIONS: Record<string, { state: StateId; duration: number }> = {
  "/batch": { state: "wide", duration: 2400 },
  "/images": { state: "play", duration: 2600 },
  "/singing": { state: "orbit", duration: 3600 },
  "/migrate": { state: "hexagon", duration: 2400 },
  "/upscale": { state: "swirl", duration: 1800 },
  "/rvc": { state: "comet", duration: 2800 },
  "/subtitle": { state: "notify", duration: 2600 },
  "/douyin": { state: "thinking", duration: 3000 },
  "/publish": { state: "burst", duration: 3000 },
};

export function WorkspaceBot({ path }: { path: string }) {
  const motion = WORKSPACE_MOTIONS[path] ?? WORKSPACE_MOTIONS["/batch"];
  const [variant, setVariant] = useState<StateId>(motion.state);

  useEffect(() => {
    const reduced = matchMedia("(prefers-reduced-motion: reduce)");
    let timer = 0;
    const start = () => {
      window.clearTimeout(timer);
      setVariant(motion.state);
      if (reduced.matches || document.hidden) return;
      timer = window.setTimeout(() => {
        setVariant("idle");
        timer = window.setTimeout(start, 1800);
      }, motion.duration);
    };
    start();
    reduced.addEventListener("change", start);
    document.addEventListener("visibilitychange", start);
    return () => {
      window.clearTimeout(timer);
      reduced.removeEventListener("change", start);
      document.removeEventListener("visibilitychange", start);
    };
  }, [motion]);

  return <StudioBot size={40} interactive variant={variant} />;
}
