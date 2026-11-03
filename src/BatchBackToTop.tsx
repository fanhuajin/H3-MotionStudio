import { createPortal } from "react-dom";
import { ArrowUp } from "@phosphor-icons/react";
import "./batch-back-to-top.css";

export function BatchBackToTop() {
  return createPortal(
    <button
      type="button"
      className="batch-back-to-top"
      onClick={() => window.scrollTo({
        top: 0,
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
      })}
    >
      <ArrowUp size={18} aria-hidden="true" />
      回到顶部
    </button>,
    document.body,
  );
}
