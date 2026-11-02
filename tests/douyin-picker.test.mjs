import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

const directory = new URL("../extensions/douyin-picker/", import.meta.url);
const coreSource = await readFile(new URL("core.js", directory), "utf8");
const context = vm.createContext({ URL });
vm.runInContext(coreSource, context);
const { videoId, update, links, cleanItem } = context.H3Picker;
const id = "7703404416980823217";
const item = { id, title: "示例作品" };

test("列表不当作作品；弹层和标准链接准确提取长作品号", () => {
  assert.equal(videoId("https://www.douyin.com/user/self?showTab=like"), null);
  assert.equal(videoId(`https://www.douyin.com/user/self?modal_id=${id}&showTab=like`), id);
  assert.equal(videoId(`https://www.douyin.com/video/${id}`), id);
  assert.equal(videoId(`https://www.douyin.com/video/1111111111111111111?modal_id=${id}`), id);
  for (const value of [`https://evil.test/video/${id}`, `https://www.douyin.com.evil.test/video/${id}`, "javascript:alert(1)", "/video/123", "/video/7703404416980823217oops"]) {
    assert.equal(videoId(value), null);
  }
});

test("按作品号去重、统一导出和删除，不保留分类", () => {
  let items = update([], { type: "select", item });
  items = update(items, { type: "select", item: { ...item, kind: "singing" } });
  assert.equal(items.length, 1);
  assert.equal(items[0].kind, undefined);
  assert.equal(links(items), `https://www.douyin.com/video/${id}`);
  assert.equal(links([{ ...item, kind: "singing" }]), `https://www.douyin.com/video/${id}`);
  assert.equal(update(items, { type: "remove", id }).length, 0);
  assert.equal(update(items, { type: "clear" }).length, 0);
  assert.throws(() => cleanItem({ ...item, id: Number(id) }));
  assert.equal(cleanItem({ ...item, thumbnail: "javascript:alert(1)" }).thumbnail, "");
});

test("后台串行保存多个标签页的选择，失败后仍可继续", async () => {
  let listener;
  let toggle;
  let badge;
  let storageChanged;
  let saved = {};
  const backgroundContext = vm.createContext({
    URL,
    importScripts() { vm.runInContext(coreSource, backgroundContext); },
    chrome: {
      action: {
        onClicked: { addListener(fn) { toggle = fn; } },
        async setBadgeText(value) { badge = value.text; },
        async setBadgeBackgroundColor() {},
        async setTitle() {},
      },
      runtime: { id: "test-extension", onMessage: { addListener(fn) { listener = fn; } } },
      storage: { onChanged: { addListener(fn) { storageChanged = fn; } }, local: {
        async get() { return structuredClone(saved); },
        async set(value) {
          saved = { ...saved, ...structuredClone(value) };
          storageChanged(Object.fromEntries(Object.entries(value).map(([key, newValue]) => [key, { newValue }])), "local");
        },
      } },
    },
  });
  vm.runInContext(await readFile(new URL("background.js", directory), "utf8"), backgroundContext);
  const sender = { id: "test-extension", url: "https://www.douyin.com/user/self" };
  const send = (message, source = sender) => new Promise((resolve) => listener(message, source, resolve));
  const results = await Promise.all([
    send({ type: "select", item }),
    send({ type: "select", item: { ...item, id: "7703404416980823218" } }),
  ]);
  assert.equal(results[1].items.length, 2);
  assert.ok((await send({ type: "select", item: {} })).error);
  assert.equal((await send({ type: "list" })).items.length, 2);
  await send({ type: "disable" });
  assert.equal(saved.enabled, false);
  assert.equal((await send({ type: "list" })).items.length, 2);
  toggle();
  await send({ type: "list" });
  assert.equal(saved.enabled, true);
  toggle();
  await send({ type: "list" });
  assert.equal(saved.enabled, false);
  assert.equal(saved.selections.length, 2);
  assert.equal(badge, "关");
  assert.ok((await send({ type: "clear" }, { ...sender, url: "https://evil.test/" })).error);
  assert.equal((await send({ type: "list" })).items.length, 2);
});

test("扩展只申请本地存储并仅注入抖音网页", async () => {
  const manifest = JSON.parse(await readFile(new URL("manifest.json", directory), "utf8"));
  assert.deepEqual(manifest.permissions, ["storage"]);
  assert.deepEqual(manifest.content_scripts[0].matches, ["https://www.douyin.com/*"]);
  assert.equal(manifest.manifest_version, 3);
});
