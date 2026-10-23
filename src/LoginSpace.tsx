import { useEffect, useRef } from "react";

/** Decorative space: never reads form values; stops and releases GPU resources on login. */
export default function LoginSpace() {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    let cancelled = false;
    let cleanup: (() => void) | undefined;
    void import("three").then((T) => {
      if (cancelled) return;
      let renderer: InstanceType<typeof T.WebGLRenderer>;
      try { renderer = new T.WebGLRenderer({ alpha: true, antialias: true, powerPreference: "low-power" }); }
      catch { return; }
      renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
      element.appendChild(renderer.domElement);
      const scene = new T.Scene();
      const camera = new T.PerspectiveCamera(45, 1, .1, 80);
      camera.position.z = 12;
      const universe = new T.Group();
      universe.position.x = -2;
      scene.add(universe);
      const geometries: InstanceType<typeof T.BufferGeometry>[] = [];
      const materials: InstanceType<typeof T.Material>[] = [];
      const rings: InstanceType<typeof T.Mesh>[] = [];
      for (let i = 0; i < 3; i++) {
        const geometry = new T.TorusGeometry(3.8 + i * .7, .008, 4, 150);
        const material = new T.MeshBasicMaterial({ color: i % 2 ? "#57bcd2" : "#9462ef", transparent: true, opacity: .18, blending: T.AdditiveBlending, depthWrite: false });
        const mesh = new T.Mesh(geometry, material);
        mesh.rotation.set(.8 + i * .2, .4 + i * .25, i * .6);
        universe.add(mesh); rings.push(mesh); geometries.push(geometry); materials.push(material);
      }
      const vertices = new Float32Array(220 * 3);
      for (let i = 0; i < vertices.length; i++) vertices[i] = (Math.random() - .5) * 24;
      const starsGeometry = new T.BufferGeometry();
      starsGeometry.setAttribute("position", new T.BufferAttribute(vertices, 3));
      const starsMaterial = new T.PointsMaterial({ color: "#bea9ff", size: .035, transparent: true, opacity: .65, depthWrite: false });
      const stars = new T.Points(starsGeometry, starsMaterial);
      scene.add(stars); geometries.push(starsGeometry); materials.push(starsMaterial);
      const shapes: InstanceType<typeof T.Mesh>[] = [];
      for (let i = 0; i < 7; i++) {
        const geometry = i % 2 ? new T.OctahedronGeometry(.16 + i * .008) : new T.IcosahedronGeometry(.14, 0);
        const material = new T.MeshBasicMaterial({ color: i % 2 ? "#b08afa" : "#6dcdd9", wireframe: true, transparent: true, opacity: .35 });
        const mesh = new T.Mesh(geometry, material);
        mesh.position.set(Math.cos(i * 2.4) * 4.3 - 1, Math.sin(i * 2.4) * 3.3, -1);
        universe.add(mesh); shapes.push(mesh); geometries.push(geometry); materials.push(material);
      }
      const pulseGeometry = new T.RingGeometry(.98, 1, 96);
      const pulseMaterial = new T.MeshBasicMaterial({ color: "#aa8bf4", transparent: true, opacity: 0, side: T.DoubleSide, depthWrite: false });
      const pulse = new T.Mesh(pulseGeometry, pulseMaterial);
      scene.add(pulse); geometries.push(pulseGeometry); materials.push(pulseMaterial);
      let pulseStart = -100;
      const pointer = new T.Vector2();
      const reduced = matchMedia("(prefers-reduced-motion: reduce)");
      const move = (event: PointerEvent) => { pointer.set(event.clientX / innerWidth - .5, event.clientY / innerHeight - .5); };
      const click = (event: PointerEvent) => {
        if (reduced.matches || (event.target instanceof Element && event.target.closest("input,button,label"))) return;
        const height = Math.tan(Math.PI / 8) * 24;
        pulse.position.set((event.clientX / innerWidth - .5) * height * camera.aspect, (.5 - event.clientY / innerHeight) * height, 0);
        pulseStart = performance.now();
      };
      const resize = new ResizeObserver(() => {
        const { width, height } = element.getBoundingClientRect();
        renderer.setSize(width, height); camera.aspect = width / Math.max(height, 1); camera.updateProjectionMatrix();
        if (reduced.matches) renderer.render(scene, camera);
      });
      resize.observe(element);
      let last = 0;
      const render = (now: number) => {
        if (now - last < 32) return;
        last = now;
        const time = reduced.matches ? 0 : now * .00015;
        universe.rotation.y += ((reduced.matches ? 0 : pointer.x * .12) - universe.rotation.y) * .035;
        universe.rotation.x += ((reduced.matches ? 0 : pointer.y * .1) - universe.rotation.x) * .035;
        rings.forEach((ring, i) => { ring.rotation.z = time * (i % 2 ? -.25 : .25) + i * .6; });
        shapes.forEach((shape, i) => { shape.rotation.set(time + i, time * .6, time * .3); shape.position.y = Math.sin(i * 2.4) * 3.3 + Math.sin(time * 2 + i) * .2; });
        stars.rotation.y = time * .035;
        const age = (now - pulseStart) / 1600;
        pulseMaterial.opacity = age >= 0 && age < 1 ? (1 - age) * .35 : 0;
        pulse.scale.setScalar(1 + Math.max(0, Math.min(age, 1)) * 3);
        renderer.render(scene, camera);
      };
      const update = () => {
        renderer.setAnimationLoop(null);
        if (document.hidden) return;
        last = 0;
        if (reduced.matches) render(performance.now());
        else renderer.setAnimationLoop(render);
      };
      const lost = (event: Event) => { event.preventDefault(); renderer.setAnimationLoop(null); };
      window.addEventListener("pointermove", move, { passive: true });
      window.addEventListener("pointerdown", click, { passive: true });
      document.addEventListener("visibilitychange", update); reduced.addEventListener("change", update);
      renderer.domElement.addEventListener("webglcontextlost", lost); renderer.domElement.addEventListener("webglcontextrestored", update);
      update();
      cleanup = () => {
        renderer.setAnimationLoop(null); resize.disconnect();
        window.removeEventListener("pointermove", move); window.removeEventListener("pointerdown", click);
        document.removeEventListener("visibilitychange", update); reduced.removeEventListener("change", update);
        renderer.domElement.removeEventListener("webglcontextlost", lost); renderer.domElement.removeEventListener("webglcontextrestored", update);
        geometries.forEach((geometry) => geometry.dispose()); materials.forEach((material) => material.dispose());
        renderer.dispose(); renderer.domElement.remove();
      };
    }).catch(() => { /* Decoration is optional; authentication stays available without WebGL. */ });
    return () => { cancelled = true; cleanup?.(); };
  }, []);
  return <div className="login-space" ref={host} />;
}
