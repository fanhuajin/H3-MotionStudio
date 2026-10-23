import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { transformWithEsbuild } from "vite";

const require = createRequire(import.meta.url);
const { code: source } = await transformWithEsbuild(
  readFileSync(new URL("../src/CloseProject.tsx", import.meta.url), "utf8"), "CloseProject.tsx",
  { format: "cjs", jsx: "automatic" },
);

// Exercise the closed screen and startup polling without opening a browser or launching services.
function screen(fetch) {
  const values = ["closed", "idle"], effects = [], timers = [];
  let index = 0, now = 0;
  const context = {
    module: { exports: {} }, fetch, AbortSignal, Date: { now: () => now },
    window: {
      location: { href: "" },
      setTimeout: callback => { timers.push(callback); return timers.length; },
      clearTimeout: () => {},
    },
    require: name => {
      if (name === "react") return {
        useState: () => {
          const slot = index++;
          return [values[slot], value => { values[slot] = value; }];
        },
        useEffect: callback => effects.push(callback),
      };
      if (name === "react-dom" || name.endsWith(".css")) return {};
      if (name === "@phosphor-icons/react") return new Proxy({}, { get: (_, key) => key });
      return require(name);
    },
  };
  vm.runInNewContext(source, context);
  function render() {
    index = 0;
    effects.length = 0;
    return context.module.exports.ProjectClosingScreen({ onReturn() {} });
  }
  function button(node) {
    if (!node || typeof node !== "object") return;
    if (node.type === "button") return node;
    for (const child of [node.props?.children].flat()) {
      const found = button(child);
      if (found) return found;
    }
  }
  async function settle() {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  }
  return {
    context, values, timers, render, button, settle,
    start: () => { button(render()).props.onClick(); render(); return effects[0](); },
    expire: () => { now = 120_001; },
  };
}

test("closed screen launches the desktop entry and waits for a healthy service", async () => {
  let running = false;
  const view = screen(async () => ({ ok: true, json: async () => ({ running }) }));
  view.start();
  assert.equal(view.context.window.location.href, "h3-motionstudio://start");
  assert.equal(view.button(view.render()).props.disabled, true);
  await view.settle();
  assert.equal(view.values[1], "starting");
  running = true;
  view.timers.shift()();
  await view.settle();
  assert.equal(view.values[1], "ready");
  assert.equal(view.button(view.render()).props.children, "返回工作台");
});

test("unavailable service times out and allows retry", async () => {
  const view = screen(async () => { throw new Error("offline"); });
  view.start();
  await view.settle();
  view.expire();
  view.timers.shift()();
  await view.settle();
  assert.equal(view.values[1], "failed");
  assert.equal(view.button(view.render()).props.disabled, false);
  view.button(view.render()).props.onClick();
  assert.equal(view.values[1], "starting");
});

test("leaving the screen stops startup polling", async () => {
  let resolve;
  const view = screen(() => new Promise(done => { resolve = done; }));
  const cleanup = view.start();
  cleanup();
  resolve({ ok: true, json: async () => ({ running: true }) });
  await view.settle();
  assert.equal(view.values[1], "starting");
  assert.equal(view.timers.length, 0);
});

test("a rejected launcher request leaves the retry button available", () => {
  const view = screen(async () => { throw new Error("offline"); });
  Object.defineProperty(view.context.window.location, "href", {
    set() { throw new Error("blocked"); },
  });
  view.button(view.render()).props.onClick();
  assert.equal(view.values[1], "failed");
  assert.equal(view.button(view.render()).props.disabled, false);
});
