import { createPortal } from "react-dom";
import { useEffect, useRef, useState } from "react";
import { ArrowUp } from "@phosphor-icons/react";
import "./batch-back-to-top.css";

export function BatchBackToTop() {
  const anchor = useRef<HTMLSpanElement>(null);
  const [visible, setVisible] = useState(false);
  const [right, setRight] = useState(32);

  useEffect(() => {
    const page = anchor.current?.closest("main");
    if (!page) return;
    const updateVisibility = () => setVisible(window.scrollY > Math.max(320, window.innerHeight * 0.6));
    const updateLayout = () => {
      const padding = parseFloat(getComputedStyle(page).paddingRight);
      setRight(Math.max(16, window.innerWidth - page.getBoundingClientRect().right + padding + 12));
      updateVisibility();
    };
    const observer = new ResizeObserver(updateLayout);
    observer.observe(page);
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
    {visible && createPortal(
      <button
        type="button"
        className="batch-back-to-top"
        style={{ right }}
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
