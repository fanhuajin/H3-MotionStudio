(function (scope) {
  "use strict";
  const validId = (id) => typeof id === "string" && /^\d{15,22}$/.test(id);
  function videoId(value) {
    try {
      const url = new URL(value, "https://www.douyin.com");
      if (url.protocol !== "https:" || url.hostname !== "www.douyin.com") return null;
      const pathId = url.pathname.match(/^\/video\/(\d+)\/?$/)?.[1];
      // A modal identifies the selected work even when the underlying route differs.
      const id = url.searchParams.get("modal_id") || pathId;
      return validId(id) ? id : null;
    } catch { return null; }
  }
  function cleanItem(item) {
    if (!item || !validId(item.id)) {
      throw new Error("未识别到有效作品，请重新选择。");
    }
    let thumbnail = "";
    try {
      const url = new URL(item.thumbnail);
      if (url.protocol === "https:") thumbnail = url.href.slice(0, 4096);
    } catch { /* A thumbnail is optional; the work ID is authoritative. */ }
    return {
      id: item.id,
      title: String(item.title || `作品 ${item.id}`).slice(0, 300),
      thumbnail,
    };
  }
  function update(items, message) {
    if (message.type === "clear") return [];
    if (message.type === "remove") {
      if (!validId(message.id)) throw new Error("无效作品号。");
      return items.filter((item) => item.id !== message.id);
    }
    if (message.type !== "select") throw new Error("不支持的操作。");
    const item = cleanItem(message.item);
    const index = items.findIndex((entry) => entry.id === item.id);
    if (index < 0) return [...items, item];
    return items.map((entry, i) => i === index ? item : entry);
  }
  function links(items) {
    return items.map((item) => `https://www.douyin.com/video/${item.id}`).join("\n");
  }
  scope.H3Picker = { videoId, cleanItem, update, links };
})(globalThis);
