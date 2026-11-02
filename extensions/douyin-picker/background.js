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
    if (message.type === "disable") {
      await chrome.storage.local.set({ enabled: false });
      return items;
    }
    if (message.type === "list") return items;
    const next = H3Picker.update(items, message);
    await chrome.storage.local.set({ selections: next });
    return next;
  }).then((items) => reply({ items }), (error) => reply({ error: error.message }));
  return true;
});

async function showEnabled(enabled) {
  await chrome.action.setBadgeText({ text: enabled ? "开" : "关" });
  await chrome.action.setBadgeBackgroundColor({ color: enabled ? "#37694e" : "#666666" });
  await chrome.action.setTitle({ title: enabled ? "H3 选片已开启，点击关闭" : "H3 选片已关闭，点击开启" });
}
chrome.action.onClicked.addListener(() => {
  pending = pending.then(async () => {
    const { enabled } = await chrome.storage.local.get("enabled");
    await chrome.storage.local.set({ enabled: enabled === false });
  }).catch((error) => console.error("切换选片助手失败", error));
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.enabled) {
    void showEnabled(changes.enabled.newValue !== false).catch(console.error);
  }
});
void chrome.storage.local.get("enabled")
  .then(({ enabled }) => showEnabled(enabled !== false)).catch(console.error);
