importScripts("core.js");

// Serialize read/modify/write across tabs so concurrent selections are preserved.
let pending = Promise.resolve();
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || !sender.url?.startsWith("https://www.douyin.com/")) {
    reply({ error: "不支持的来源。" });
    return false;
  }
  pending = pending.then(async () => {
    const stored = await chrome.storage.local.get("selections");
    const items = stored.selections ?? [];
    if (!Array.isArray(items)) throw new Error("选片记录格式异常，请先备份扩展数据。");
    if (message.type === "list") return items;
    const next = H3Picker.update(items, message);
    await chrome.storage.local.set({ selections: next });
    return next;
  }).then((items) => reply({ items }), (error) => reply({ error: error.message }));
  return true;
});
