import { useEffect, useRef } from "react";

/** 两处视频预览共用：一次滚轮手势只切一条，抑制触控板惯性连跳。 */
export function useVideoPreviewWheel(enabled: boolean, navigate: (direction: number) => void) {
  const gesture = useRef({ last: 0, total: 0, switched: false });
  useEffect(() => {
    if (!enabled) {
      gesture.current = { last: 0, total: 0, switched: false };
      return;
    }
    const onWheel = (event: WheelEvent) => {
      if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey
        || Math.abs(event.deltaX) > Math.abs(event.deltaY) || !event.deltaY) return;
      if (!(event.target instanceof Element) || !event.target.closest("dialog.batch-video-dialog[open]")) return;
      event.preventDefault();
      const current = gesture.current;
      const now = performance.now();
      // 惯性期间持续更新时间，直到滚动停下才允许下一次手势。
      if (now - current.last > 250) { current.total = 0; current.switched = false; }
      current.last = now;
      if (current.switched) return;
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 600 : 1);
      if (Math.sign(delta) !== Math.sign(current.total)) current.total = 0;
      current.total += delta;
      if (Math.abs(current.total) < 40) return;
      current.switched = true;
      navigate(current.total > 0 ? 1 : -1);
    };
    document.addEventListener("wheel", onWheel, { passive: false, capture: true });
    return () => document.removeEventListener("wheel", onWheel, true);
  }, [enabled, navigate]);
}
