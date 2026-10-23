import { Play, SpinnerGap } from "@phosphor-icons/react";
import "./content-loading.css";

export function ContentLoading({ label = "正在加载内容…", compact = false }: { label?: string; compact?: boolean }) {
  return <div className={`content-loading${compact ? " is-compact" : ""}`} role="status" aria-busy="true">
    {compact ? <><SpinnerGap aria-hidden="true" /><span>{label}</span></> : <div className="content-loading-panel">
      <div className="content-loading-orbit" aria-hidden="true">
        <i /><i /><div className="content-loading-core"><Play weight="fill" /></div>
      </div>
      <div className="content-loading-copy"><span className="content-loading-brand">H3 MOTION STUDIO</span><strong>{label}</strong><span>灵感即将就位</span></div>
      <div className="content-loading-track" aria-hidden="true"><i /></div>
    </div>}
  </div>;
}
