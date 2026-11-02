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
    if (message.type === "selectRow" || message.type === "removeRow") {
      if (!Array.isArray(message.items) || !message.items.length) throw new Error("整排没有有效作品。");
      const row = message.items.map(cleanItem);
      const ids = new Set(row.map((item) => item.id));
      if (message.type === "removeRow") return items.filter((item) => !ids.has(item.id));
      const merged = new Map(items.map((item) => [item.id, item]));
      for (const item of row) merged.set(item.id, item);
      return [...merged.values()];
    }
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
  function groupRows(entries) {
    const rows = [];
    for (const entry of [...entries].sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left)) {
      const row = rows.find((row) => Math.abs(row[0].rect.top - entry.rect.top) <= 8);
      if (row) row.push(entry);
      else rows.push([entry]);
    }
    return rows.map((row) => row.sort((a, b) => a.rect.left - b.rect.left));
  }
  scope.H3Picker = { videoId, cleanItem, update, links, groupRows };
})(globalThis);
