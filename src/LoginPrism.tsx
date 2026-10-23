import { useEffect, useRef } from "react";

/** Animate illumination only. Artwork coordinates never depend on time. */
export default function LoginPrism() {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    let cancelled = false;
    let dispose: (() => void) | undefined;
    void import("three").then((T) => {
      if (cancelled) return;
      let renderer: InstanceType<typeof T.WebGLRenderer>;
      try { renderer = new T.WebGLRenderer({ alpha: true, antialias: false, powerPreference: "low-power" }); }
      catch { return; }
      renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
      element.appendChild(renderer.domElement);
      const scene = new T.Scene();
      const camera = new T.OrthographicCamera(-1, 1, 1, -1, 0, 2);
      camera.position.z = 1;
      const uniforms = { art: { value: null as InstanceType<typeof T.Texture> | null }, time: { value: 0 }, aspect: { value: 1 } };
      const geometry = new T.PlaneGeometry(2, 2);
      const material = new T.ShaderMaterial({
        uniforms,
        vertexShader: `varying vec2 vUv;void main(){vUv=uv;gl_Position=vec4(position.xy,0.,1.);}`,
        fragmentShader: `
          uniform sampler2D art; uniform float time; uniform float aspect; varying vec2 vUv;
          float packet(float phase){return pow(.5+.5*cos(phase),12.);}
          void main(){
            // Only aspect ratio affects UVs; no displacement, morphing, zoom or time-based sampling.
            vec2 uv=vUv;
            float imageAspect=1134./1387.;
            uv.x=(uv.x-.5)*min(aspect/imageAspect,1.)+.5;
            uv.y=(uv.y-.5)*min(imageAspect/aspect,1.)+.5;
            vec3 base=texture2D(art,uv).rgb;
            float bright=max(base.r,max(base.g,base.b));
            float saturation=bright-min(base.r,min(base.g,base.b));
            float colored=smoothstep(.07,.25,saturation)*smoothstep(.1,.5,bright);
            // Traveling packets follow the upper sweep, the lower sweep and the glass highlights.
            float upper=exp(-pow((uv.y-(.53+.23*uv.x))/.07,2.));
            float lower=exp(-pow((uv.y-(.43-.22*uv.x))/.07,2.));
            float angle=atan((uv.y-.52)*1.4,uv.x-.48);
            float orbit=packet(angle*3.-time*1.4);
            float streams=upper*packet(uv.x*16.-time*2.4)+lower*packet(uv.x*18.-time*2.8+1.7);
            float glass=packet((uv.y-uv.x*.25)*20.-time*1.2);
            float light=colored*(streams*.65+orbit*.22+glass*.28);
            vec3 tint=mix(vec3(.38,.7,1.),vec3(.8,.45,1.),.5+.5*sin(uv.y*7.-time*.4));
            gl_FragColor=vec4(base+base*light*.45+tint*light*.1,1.);
          }`,
        depthTest: false,
        depthWrite: false,
      });
      scene.add(new T.Mesh(geometry, material));
      const reduced = matchMedia("(prefers-reduced-motion: reduce)");
      let texture: InstanceType<typeof T.Texture> | undefined;
      let last = 0;
      let elapsed = 0;
      const render = (now: number) => {
        if (!texture || now - last < 32) return;
        elapsed += last ? Math.min((now - last) / 1000, .1) : 0;
        last = now;
        uniforms.time.value = reduced.matches ? 0 : elapsed;
        renderer.render(scene, camera);
      };
      const update = () => {
        renderer.setAnimationLoop(null); last = 0;
        if (!texture || document.hidden) return;
        element.classList.add("is-ready");
        if (reduced.matches) render(performance.now());
        else renderer.setAnimationLoop(render);
      };
      new T.TextureLoader().load("/assets/login-prism.png", (loaded) => {
        if (cancelled) { loaded.dispose(); return; }
        texture = loaded;
        // Keep the original artwork's display colors; this shader outputs them without remapping.
        uniforms.art.value = loaded;
        update();
      }, undefined, () => element.classList.remove("is-ready"));
      const observer = new ResizeObserver(() => {
        const { width, height } = element.getBoundingClientRect();
        renderer.setSize(width, height);
        uniforms.aspect.value = width / Math.max(height, 1);
        if (texture) { last = 0; render(performance.now()); }
      });
      observer.observe(element);
      const lost = (event: Event) => { event.preventDefault(); renderer.setAnimationLoop(null); element.classList.remove("is-ready"); };
      document.addEventListener("visibilitychange", update); reduced.addEventListener("change", update);
      renderer.domElement.addEventListener("webglcontextlost", lost); renderer.domElement.addEventListener("webglcontextrestored", update);
      dispose = () => {
        renderer.setAnimationLoop(null); observer.disconnect();
        document.removeEventListener("visibilitychange", update); reduced.removeEventListener("change", update);
        renderer.domElement.removeEventListener("webglcontextlost", lost); renderer.domElement.removeEventListener("webglcontextrestored", update);
        texture?.dispose(); geometry.dispose(); material.dispose(); renderer.dispose(); renderer.domElement.remove();
        element.classList.remove("is-ready");
      };
    }).catch(() => { /* Original artwork remains visible when the optional renderer cannot load. */ });
    return () => { cancelled = true; dispose?.(); };
  }, []);
  return <div className="login-prism-renderer" ref={host} aria-hidden="true" />;
}
