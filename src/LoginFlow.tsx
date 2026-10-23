import { useEffect, useRef } from "react";

/** Moving geometry and traveling highlights, rendered only while the login screen is visible. */
export default function LoginFlow() {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    let cancelled = false;
    let dispose: (() => void) | undefined;
    void import("three").then((THREE) => {
      if (cancelled) return;
      let renderer: InstanceType<typeof THREE.WebGLRenderer>;
      try { renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: "low-power" }); }
      catch { return; } // The glass artwork remains usable when WebGL is unavailable.
      renderer.setClearColor(0, 0);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
      element.appendChild(renderer.domElement);
      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(45, 1, .1, 100);
      camera.position.z = 8;
      const group = new THREE.Group();
      group.position.y = .25;
      scene.add(group);
      const uniforms = { time: { value: 0 } };
      const geometries: InstanceType<typeof THREE.BufferGeometry>[] = [];
      const materials: InstanceType<typeof THREE.Material>[] = [];
      let background: InstanceType<typeof THREE.Mesh> | undefined;
      let artwork: InstanceType<typeof THREE.Texture> | undefined;
      const textureLoader = new THREE.TextureLoader();
      textureLoader.load("/assets/login-prism.png", (texture) => {
        if (cancelled) { texture.dispose(); return; }
        artwork = texture;
        texture.colorSpace = THREE.SRGBColorSpace;
        const geometry = new THREE.PlaneGeometry(2, 2);
        const material = new THREE.ShaderMaterial({
          uniforms: { time: uniforms.time, art: { value: texture }, aspect: { value: 1 } },
          vertexShader: `varying vec2 vUv;void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);}`,
          fragmentShader: `uniform sampler2D art;uniform float time;uniform float aspect;varying vec2 vUv;
            void main(){vec2 uv=vUv;float cover=min(aspect/.818,1.);uv.x=(uv.x-.5)*cover+.5;
            uv.y=(uv.y-.5)*min(.818/aspect,1.)+.5;
            uv=(uv-.5)*.94+.5;uv.x+=sin(uv.y*7.+time*.65)*.012+sin(time*.35)*.009;
            uv.y+=cos(uv.x*6.+time*.48)*.009+sin(time*.42)*.012;
            vec3 col=texture2D(art,uv).rgb;
            float shimmer=sin(uv.x*9.+uv.y*6.-time*.8)*.06+.98;
            gl_FragColor=vec4(col*shimmer,1.);}`,
          depthWrite: false,
        });
        geometries.push(geometry); materials.push(material);
        background = new THREE.Mesh(geometry, material);
        background.position.z = -2;
        background.renderOrder = -1;
        scene.add(background);
        sizeBackground();
        element.classList.add("is-ready");
        renderer.render(scene, camera);
      });
      const sizeBackground = () => {
        if (!background) return;
        const height = 2 * Math.tan(THREE.MathUtils.degToRad(22.5)) * 10;
        background.scale.set(height * camera.aspect / 2, height / 2, 1);
        (background.material as InstanceType<typeof THREE.ShaderMaterial>).uniforms.aspect.value = camera.aspect;
      };
      // UVs run along each tube: the shader moves bright packets through the actual geometry.
      const vertexShader = `varying vec2 vUv; void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}`;
      const fragmentShader = `uniform float time; uniform vec3 tint; uniform float offset; varying vec2 vUv;
        void main(){float head=fract(vUv.x-time*.13+offset);float pulse=pow(max(0.,1.-head),12.);
        float edge=pow(abs(sin(vUv.y*3.14159)),.5);float strength=(.12+pulse*1.9)*edge;
        gl_FragColor=vec4(mix(tint,vec3(1.),pulse*.7),strength);}`;
      const paths: InstanceType<typeof THREE.CatmullRomCurve3>[] = [];
      for (let lane = 0; lane < 6; lane++) {
        const points = [];
        for (let i = 0; i <= 100; i++) {
          const angle = i / 100 * Math.PI * 2;
          points.push(new THREE.Vector3(Math.cos(angle) * (3.1 + lane * .08), Math.sin(angle) * (.8 + lane * .055) + Math.cos(angle) * 1.05, Math.sin(angle) * 1.4 + lane * .05));
        }
        const path = new THREE.CatmullRomCurve3(points, true);
        paths.push(path);
        const geometry = new THREE.TubeGeometry(path, 220, lane % 2 ? .012 : .022, 6, true);
        const material = new THREE.ShaderMaterial({ uniforms: { time: uniforms.time, tint: { value: new THREE.Color(lane % 2 ? "#55cfff" : "#ad65ff") }, offset: { value: lane * .17 } }, vertexShader, fragmentShader, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
        geometries.push(geometry); materials.push(material);
        group.add(new THREE.Mesh(geometry, material));
      }
      const positions = new Float32Array(180 * 3);
      const particlesGeometry = new THREE.BufferGeometry();
      particlesGeometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      const particleMaterial = new THREE.PointsMaterial({ color: "#cfb7ff", size: .025, transparent: true, opacity: .85, blending: THREE.AdditiveBlending, depthWrite: false });
      geometries.push(particlesGeometry); materials.push(particleMaterial);
      group.add(new THREE.Points(particlesGeometry, particleMaterial));
      const pointer = { x: 0, y: 0 };
      const move = (event: PointerEvent) => {
        const rect = element.getBoundingClientRect();
        pointer.x = ((event.clientX - rect.left) / rect.width - .5) * .28;
        pointer.y = ((event.clientY - rect.top) / rect.height - .5) * .2;
      };
      const leave = () => { pointer.x = 0; pointer.y = 0; };
      const parent = element.parentElement!;
      parent.addEventListener("pointermove", move);
      parent.addEventListener("pointerleave", leave);
      const resize = new ResizeObserver(() => {
        const { width, height } = element.getBoundingClientRect();
        renderer.setSize(width, height);
        camera.aspect = width / Math.max(height, 1);
        camera.updateProjectionMatrix();
        sizeBackground();
      });
      resize.observe(element);
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
      let start = performance.now();
      let last = 0;
      const render = (now: number) => {
        if (now - last < 32) return;
        last = now;
        const time = reduced.matches ? 0 : Math.max(0, (now - start) / 1000);
        uniforms.time.value = time;
        group.rotation.y += ((reduced.matches ? 0 : pointer.x + Math.sin(time * .22) * .13) - group.rotation.y) * .045;
        group.rotation.x += ((reduced.matches ? 0 : pointer.y + Math.cos(time * .18) * .08) - group.rotation.x) * .045;
        for (let i = 0; i < 180; i++) {
          const point = paths[i % paths.length].getPointAt((i / 180 + time * (.045 + i % 3 * .012)) % 1);
          positions.set([point.x, point.y, point.z], i * 3);
        }
        particlesGeometry.attributes.position.needsUpdate = true;
        renderer.render(scene, camera);
      };
      const update = () => {
        renderer.setAnimationLoop(null);
        if (document.hidden) return;
        if (artwork) element.classList.add("is-ready");
        start = performance.now(); last = 0;
        if (reduced.matches) render(start);
        else renderer.setAnimationLoop(render);
      };
      document.addEventListener("visibilitychange", update);
      reduced.addEventListener("change", update);
      const lost = (event: Event) => { event.preventDefault(); renderer.setAnimationLoop(null); element.classList.remove("is-ready"); };
      renderer.domElement.addEventListener("webglcontextlost", lost);
      renderer.domElement.addEventListener("webglcontextrestored", update);
      update();
      dispose = () => {
        renderer.setAnimationLoop(null); resize.disconnect();
        parent.removeEventListener("pointermove", move); parent.removeEventListener("pointerleave", leave);
        document.removeEventListener("visibilitychange", update); reduced.removeEventListener("change", update);
        renderer.domElement.removeEventListener("webglcontextlost", lost); renderer.domElement.removeEventListener("webglcontextrestored", update);
        geometries.forEach((geometry) => geometry.dispose()); materials.forEach((material) => material.dispose());
        renderer.dispose(); renderer.domElement.remove();
        artwork?.dispose(); element.classList.remove("is-ready");
      };
    }).catch(() => { /* Static artwork is the fallback if the optional renderer fails to load. */ });
    return () => { cancelled = true; dispose?.(); };
  }, []);
  return <div className="login-flow" ref={host} aria-hidden="true" />;
}
