import { useEffect, useId, useRef, useState } from "react";
import { BotEngine, type BotFrame } from "./vendor/bloub/engine";
import { DEMI_VIEWBOX as VB, RAYON } from "./vendor/bloub/repere";
import type { StateId } from "./vendor/bloub/states";
import { SHAPE_BY_ID, type ShapeId } from "./vendor/bloub/skins";

export type StudioMood = "idle" | "working" | "waiting" | "success" | "error" | "offline";
const STATES: Record<StudioMood, StateId> = { idle: "idle", working: "orbit", waiting: "thinking", success: "burst", error: "alert", offline: "sleep" };
const COLORS: Record<StudioMood, string> = { idle: "#a9c6af", working: "#a9c6af", waiting: "#d6bd86", success: "#97d6b3", error: "#df9c99", offline: "#89918b" };

/** React renderer for the unmodified MIT Bloub engine; no Vue runtime required. */
export function StudioBot({ mood = "idle", size = 72, variant, interactive = false, shape }: { mood?: StudioMood; size?: number; variant?: StateId; interactive?: boolean; shape?: ShapeId }) {
  const state = variant ?? STATES[mood];
  const radii = shape ? SHAPE_BY_ID.get(shape)?.radii ?? null : null;
  const uid = `bloub-${useId().replace(/:/g, "")}`;
  const engine = useRef<BotEngine | null>(null);
  if (!engine.current) engine.current = new BotEngine(RAYON, state, radii);
  const clock = useRef(0);
  const [frame, setFrame] = useState<BotFrame>(() => engine.current!.sample(0));
  const host = useRef<SVGSVGElement>(null);
  useEffect(() => { engine.current!.setState(state, clock.current); }, [state]);
  useEffect(() => { engine.current!.setShape(radii, clock.current); }, [radii]);
  useEffect(() => {
    let raf = 0, last = 0, visible = true;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)");
    const draw = (now: number) => {
      raf = 0;
      if (document.hidden || !visible) { last = 0; return; }
      if (!reduced.matches && last && now - last < 1000 / 30) { raf = requestAnimationFrame(draw); return; }
      if (!reduced.matches) clock.current += last ? Math.min((now - last) / 1000, .1) : 0;
      last = now;
      // The engine reuses a frame object; copy the shell to trigger React rendering.
      setFrame({ ...engine.current!.sample(reduced.matches ? clock.current + 2 : clock.current) });
      if (!reduced.matches) raf = requestAnimationFrame(draw);
    };
    const resume = () => { cancelAnimationFrame(raf); last = 0; raf = requestAnimationFrame(draw); };
    const observer = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; resume(); });
    if (host.current) observer.observe(host.current);
    document.addEventListener("visibilitychange", resume);
    reduced.addEventListener("change", resume);
    resume();
    return () => { cancelAnimationFrame(raf); observer.disconnect(); document.removeEventListener("visibilitychange", resume); reduced.removeEventListener("change", resume); };
  }, [state, shape]);
  const dots = frame.dots.map((dot, i) => dot.d
    ? <path key={i} d={dot.d} transform={`translate(${dot.x} ${dot.y}) rotate(${dot.rot ?? 0}) scale(${RAYON})`} opacity={dot.opacity} />
    : <circle key={i} cx={dot.x} cy={dot.y} r={dot.r} opacity={dot.opacity} />);
  return <svg ref={host} className="studio-bot" width={size} height={size} viewBox={`${-VB} ${-VB} ${VB * 2} ${VB * 2}`} aria-hidden="true" style={{ color: COLORS[mood], width: size, height: size }}
    onPointerEnter={interactive ? () => engine.current!.setState("wink", clock.current) : undefined}
    onPointerLeave={interactive ? () => { engine.current!.setState(state, clock.current); engine.current!.setLook(null, clock.current); } : undefined}
    onPointerMove={interactive ? (event) => { const rect = event.currentTarget.getBoundingClientRect(); engine.current!.setLook({ yaw: (event.clientX - rect.left - rect.width / 2) / rect.width * 45, pitch: (event.clientY - rect.top - rect.height / 2) / rect.height * 35, mix: 1, spin: 0, wander: 0 }, clock.current); } : undefined}>
    <defs>
      <mask id={uid} maskUnits="userSpaceOnUse" x={-VB} y={-VB} width={VB * 2} height={VB * 2}>
        <path d={frame.bodyPath} fill="white" />
        {frame.eyes.map((eye, i) => <path key={i} d={eye.d} transform={eye.matrix} opacity={eye.alpha} fill="black" />)}
        {frame.notch && <circle {...frame.notch} fill="black" />}
      </mask>
    </defs>
    <g fill="none" stroke="currentColor" strokeLinecap="round">{frame.arcs.map(arc => <path key={arc.id} d={arc.back} strokeWidth={arc.width} opacity={arc.opacity * .6} />)}</g>
    {frame.dotsBehind && <g fill="currentColor">{dots}</g>}
    <g opacity={frame.bodyAlpha}>
      <path d={frame.bodyPath} fill="var(--bot-paper, #191a1c)" />
      <rect x={-VB} y={-VB} width={VB * 2} height={VB * 2} fill="currentColor" mask={`url(#${uid})`} />
    </g>
    {!frame.dotsBehind && <g fill="currentColor">{dots}</g>}
    {frame.notif && <circle {...frame.notif} fill="#d6bd86" />}
    <g fill="none" stroke="currentColor" strokeLinecap="round">{frame.arcs.map(arc => <path key={arc.id} d={arc.front} strokeWidth={arc.width} opacity={arc.opacity} />)}</g>
  </svg>;
}

export function statusMood(status?: string): StudioMood {
  if (status === "running") return "working";
  if (["queued", "waiting", "paused", "awaiting_review"].includes(status || "")) return "waiting";
  if (status === "completed") return "success";
  if (["failed", "interrupted"].includes(status || "")) return "error";
  return "idle";
}
