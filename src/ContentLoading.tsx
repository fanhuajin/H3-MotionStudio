import { SpinnerGap } from "@phosphor-icons/react";
import "./content-loading.css";

export function ContentLoading({ label = "正在加载内容…", compact = false }: { label?: string; compact?: boolean }) {
  return <div className={`content-loading${compact ? " is-compact" : ""}`} role="status" aria-busy="true">
    <SpinnerGap aria-hidden="true" /><span>{label}</span>
  </div>;
}
