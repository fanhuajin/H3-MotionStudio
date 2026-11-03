import { createPortal } from "react-dom";
import { useEffect, useRef, useState } from "react";
import { ArrowUp } from "@phosphor-icons/react";
import "./batch-back-to-top.css";

export function BatchBackToTop() {
  const anchor = useRef<HTMLSpanElement>(null);
  const [visible, setVisible] = useState(false);
  const [left, setLeft] = useState(0);

  useEffect(() => {
    const content = anchor.current?.closest("main")?.querySelector<HTMLElement>(".batch-content");
    if (!content) return;
    const updateVisibility = () => setVisible(window.scrollY > 400);
    const updateLayout = () => {
      // 以实际内容右边界向外定位，避免按页面内边距反推后覆盖行内操作。
      const table = content.querySelector<HTMLElement>(".batch-table-card");
      const edge = Math.max(content.getBoundingClientRect().right, table?.getBoundingClientRect().right ?? 0);
      setLeft(edge + 16);
      updateVisibility();
    };
    const observer = new ResizeObserver(updateLayout);
    observer.observe(content);
    updateLayout();
    window.addEventListener("scroll", updateVisibility, { passive: true });
    window.addEventListener("resize", updateLayout);
    return () => {
      observer.disconnect();
      window.removeEventListener("scroll", updateVisibility);
      window.removeEventListener("resize", updateLayout);
    };
  }, []);

  return <>
    <span ref={anchor} hidden />
    {createPortal(
      <button
        type="button"
        className={`batch-back-to-top${visible ? " is-visible" : ""}`}
        style={{ left }}
        disabled={!visible}
        aria-hidden={!visible}
        aria-label="回到顶部"
        title="回到顶部"
        onClick={() => window.scrollTo({
          top: 0,
          behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
        })}
      >
        <ArrowUp size={19} weight="bold" aria-hidden="true" />
      </button>,
      document.body,
    )}
  </>;
}
