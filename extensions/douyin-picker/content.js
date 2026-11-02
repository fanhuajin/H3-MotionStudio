(() => {
  "use strict";
  if (document.getElementById("h3-douyin-picker")) return;
  const { videoId, links, groupRows } = H3Picker;
  const host = document.createElement("div");
  host.id = "h3-douyin-picker";
  host.style.cssText = "all:initial;display:none;position:fixed;inset:0;z-index:2147483647;pointer-events:none";
  const root = host.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  style.textContent = `
    :host{color-scheme:dark}*{box-sizing:border-box}
    button,textarea,a{font:inherit}button{min-height:36px;border:1px solid #667b71;border-radius:8px;background:#26362f;color:#f2f8f4;padding:6px 10px;cursor:pointer}
    button:hover{background:#3b5146}button:disabled{opacity:.55;cursor:default}
    :focus-visible{outline:3px solid #b8e9ce;outline-offset:2px}
    button[aria-pressed=true]{background:#b7dec8;color:#14281d;border-color:#b7dec8}
    .panel{pointer-events:auto;position:fixed;right:20px;bottom:24px;width:min(350px,calc(100vw - 32px));padding:14px;border:1px solid #667b71;border-radius:14px;background:#19251ff5;color:#f2f8f4;box-shadow:0 8px 32px #0006;font:14px/1.5 system-ui,sans-serif}
    header,.actions,.row{display:flex;gap:8px;align-items:center}header{justify-content:space-between}h2{font-size:16px;margin:0}p{margin:8px 0;color:#c8d6ce}.actions{flex-wrap:wrap;margin-top:8px}
    .current{padding:10px 0;border-bottom:1px solid #52645a}.title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .list{max-height:32vh;overflow:auto;margin:10px 0;padding:0;list-style:none}.row{padding:8px 0;border-bottom:1px solid #384c40}.row img{width:42px;height:56px;object-fit:cover;border-radius:5px}.detail{flex:1;min-width:0}.detail a{display:block;color:#e3f4ea;text-decoration:underline}.row button{padding:4px 8px}
    .status{min-height:21px;color:#cbead7}.card{pointer-events:auto;position:fixed;display:flex;align-items:center;gap:6px;padding:5px 8px;border:1px solid #91bca2;border-radius:8px;background:#18291fee;color:#f4fff7;font:13px/1.4 system-ui,sans-serif;box-shadow:0 2px 8px #0008;cursor:pointer}.card input{width:18px;height:18px;accent-color:#b7dec8;margin:0}
    textarea{width:100%;height:130px;margin-top:8px;background:#101c16;color:#fff;border:1px solid #91bca2;border-radius:6px;padding:8px}small{color:#bdcec3} [hidden]{display:none!important}
    @media(max-width:600px){.panel{right:10px;bottom:12px}.list{max-height:25vh}}
  `;
  root.append(style);
  const panel = document.createElement("section");
  panel.className = "panel";
  panel.setAttribute("aria-label", "H3 抖音选片助手");
  panel.innerHTML = `
    <header><h2>H3 选片 · <span id="count">0</span> 条</h2><div class="actions"><button id="collapse" aria-expanded="true">收起</button><button id="disable" title="可从浏览器工具栏的插件图标重新开启">关闭</button></div></header>
    <div id="body">
      <div class="current"><small>当前打开的作品</small><div id="current-title" class="title">点开视频后可在这里加入</div>
        <div class="actions"><button id="choose">选择当前视频</button></div>
      </div>
      <ul class="list" id="list" aria-label="已选作品"></ul>
      <div class="actions"><button id="copy">复制全部链接</button><button id="clear">清空清单</button></div>
      <div id="confirmation" hidden><p>确定清空全部已选作品？</p><div class="actions"><button id="confirm-clear">确定清空</button><button id="cancel-clear">保留</button></div></div>
      <p><small>复制后粘贴到 H3 的跳舞链接输入框，由你手动提交。</small></p>
      <textarea id="fallback" aria-label="待复制的链接，全选后复制" readonly hidden></textarea>
    </div><div class="status" id="status" role="status" aria-live="polite"></div>`;
  root.append(panel);
  document.documentElement.append(host);
  const $ = (id) => root.getElementById(id);
  let items = [];
  let ready = false;
  let busy = false;
  let cards = [];
  const rowButtons = [];
  let lastUrl = "";
  let urlChangedAt = performance.now();
  let transitionUntil = 0;
  let currentId = null;
  let collapsed = false;
  let enabled = false;
  let scanTimer;
  function applyEnabled(value) {
    enabled = value !== false;
    host.style.display = enabled ? "block" : "none";
    clearInterval(scanTimer);
    if (enabled && ready) {
      scanCards();
      scanTimer = setInterval(scanCards, 800);
    } else {
      cancelAnimationFrame(frame);
    }
  }
  const status = (value) => { $("status").textContent = value; };
  async function request(message) {
    const result = await chrome.runtime.sendMessage(message);
    if (!result || result.error) throw new Error(result?.error || "选片助手未响应，请刷新页面。");
    return result.items;
  }
  async function mutate(message) {
    if (busy || !ready) return;
    busy = true;
    refreshCurrent();
    positionCards();
    try {
      items = await request(message);
      renderList();
      status(message.type === "select" ? "已保存选择" : "已更新清单");
    } catch (error) { status(`保存失败：${error.message}`); }
    finally { busy = false; refreshCurrent(); positionCards(); }
  }
  function metadata(id, anchor) {
    const card = anchor || cards.find((entry) => entry.id === id)?.anchor;
    const image = card?.querySelector("img");
    return {
      id,
      title: card?.getAttribute("aria-label") || image?.alt || card?.textContent?.trim() || `作品 ${id}`,
      thumbnail: image?.currentSrc || image?.src || "",
    };
  }
  function refreshCurrent() {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      urlChangedAt = performance.now();
    }
    const id = videoId(location.href);
    const stable = performance.now() - urlChangedAt >= 650 && performance.now() >= transitionUntil;
    currentId = stable ? id : null;
    const selected = items.find((item) => item.id === currentId);
    $("current-title").textContent = !stable ? "正在切换，请稍候…" : id ? metadata(id).title : "点开视频后可在这里加入";
    $("choose").disabled = !ready || busy || !currentId;
    $("choose").textContent = selected ? "取消选择当前视频" : "选择当前视频";
    $("choose").setAttribute("aria-pressed", String(Boolean(selected)));
  }
  function renderList() {
    $("count").textContent = String(items.length);
    const fragment = document.createDocumentFragment();
    for (const item of items) {
      const row = document.createElement("li");
      row.className = "row";
      if (item.thumbnail) {
        const image = document.createElement("img");
        image.src = item.thumbnail;
        image.alt = "";
        image.loading = "lazy";
        image.referrerPolicy = "no-referrer";
        row.append(image);
      }
      const detail = document.createElement("div");
      detail.className = "detail";
      const link = document.createElement("a");
      link.className = "title";
      link.textContent = item.title;
      link.title = `${item.title}\n作品号 ${item.id}`;
      link.href = `https://www.douyin.com/video/${item.id}`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      detail.append(link);
      const remove = document.createElement("button");
      remove.textContent = "移除";
      remove.setAttribute("aria-label", `移除${item.title}`);
      remove.onclick = () => void mutate({ type: "remove", id: item.id });
      row.append(detail, remove);
      fragment.append(row);
    }
    if (!items.length) {
      const empty = document.createElement("li");
      empty.textContent = "还没有选择，勾选视频卡片即可加入。";
      fragment.append(empty);
    }
    $("list").replaceChildren(fragment);
    $("copy").textContent = `复制全部链接（${items.length}）`;
    $("copy").disabled = !items.length || !ready;
    $("clear").disabled = !items.length || !ready;
    $("fallback").hidden = true;
    refreshCurrent();
  }
  function scanCards() {
    if (!enabled) return;
    const anchors = [...document.querySelectorAll('a[href*="/video/"]')];
    const existing = new Map(cards.map((card) => [card.anchor, card]));
    const next = [];
    for (const anchor of anchors) {
      const id = videoId(anchor.href);
      if (!id || !anchor.querySelector("img")) continue;
      const old = existing.get(anchor);
      if (old) { old.id = id; next.push(old); existing.delete(anchor); continue; }
      const label = document.createElement("label");
      label.className = "card";
      const input = document.createElement("input");
      input.type = "checkbox";
      const text = document.createElement("span");
      label.append(input, text);
      const card = { id, anchor, label, input, text };
      input.addEventListener("change", () => {
        // Re-read the href because virtualized cards can be reused for another work.
        const actualId = videoId(anchor.href);
        if (actualId !== card.id) { scanCards(); status("卡片已变化，请重新勾选。"); return; }
        const selected = items.some((item) => item.id === actualId);
        void mutate(selected ? { type: "remove", id: actualId } : {
          type: "select", item: metadata(actualId, anchor),
        });
      });
      root.append(label);
      next.push(card);
    }
    for (const card of existing.values()) card.label.remove();
    cards = next;
    positionCards();
    refreshCurrent();
  }
  let frame = 0;
  function positionCards() {
    if (!enabled) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const modal = Boolean(videoId(location.href));
      const visibleCards = [];
      for (const card of cards) {
        const rect = card.anchor.getBoundingClientRect();
        const visible = !modal && rect.width >= 100 && rect.height >= 100 && rect.bottom > 100 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth;
        card.label.hidden = !visible;
        if (!visible) continue;
        visibleCards.push({ card, rect });
        card.label.style.left = `${Math.max(4, rect.left + 8)}px`;
        card.label.style.top = `${Math.max(90, rect.top + 8)}px`;
        const selected = items.find((item) => item.id === card.id);
        card.input.checked = Boolean(selected);
        card.input.disabled = busy || !ready;
        card.input.setAttribute("aria-label", `${selected ? "取消选择" : "选择"}${metadata(card.id, card.anchor).title}`);
        card.text.textContent = selected ? "已选" : "选择";
      }
      const rows = groupRows(visibleCards);
      for (const button of rowButtons) button.hidden = true;
      rows.forEach((row, index) => {
        let button = rowButtons[index];
        if (!button) {
          button = document.createElement("button");
          button.className = "card";
          button.type = "button";
          root.append(button);
          rowButtons.push(button);
        }
        const selected = row.every(({ card }) => items.some((item) => item.id === card.id));
        button.hidden = false;
        button.disabled = busy || !ready;
        button.textContent = selected ? "取消整排" : "选择整排";
        button.setAttribute("aria-pressed", String(selected));
        button.setAttribute("aria-label", `${button.textContent}，共 ${row.length} 个视频`);
        const right = Math.max(...row.map(({ rect }) => rect.right));
        const outside = right + 116 <= innerWidth;
        button.style.left = `${Math.max(4, outside ? right + 8 : Math.min(right, innerWidth) - 108)}px`;
        button.style.top = `${Math.max(90, row[0].rect.top + (outside ? 8 : 46))}px`;
        button.onclick = () => {
          // Virtualized cards may have changed since the last layout pass.
          if (row.some(({ card }) => !card.anchor.isConnected || videoId(card.anchor.href) !== card.id)) {
            scanCards(); status("卡片已变化，请重新选择整排。"); return;
          }
          const allSelected = row.every(({ card }) => items.some((item) => item.id === card.id));
          void mutate({
            type: allSelected ? "removeRow" : "selectRow",
            items: row.map(({ card }) => metadata(card.id, card.anchor)),
          });
        };
      });
    });
  }
  $("choose").onclick = () => {
    const previous = currentId;
    refreshCurrent();
    if (!currentId || previous !== currentId) { status("视频正在切换，请稍候再选。"); return; }
    void mutate(items.some((item) => item.id === currentId)
      ? { type: "remove", id: currentId }
      : { type: "select", item: metadata(currentId) });
  };
  $("copy").onclick = async () => {
    const value = links(items);
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      status(`已复制 ${value.split("\n").length} 条链接`);
    } catch {
      $("fallback").hidden = false;
      $("fallback").value = value;
      $("fallback").focus();
      $("fallback").select();
      status("浏览器未允许自动复制，请按 Ctrl+C 复制下方链接。");
    }
  };
  $("collapse").onclick = () => {
    collapsed = !collapsed;
    $("body").hidden = collapsed;
    $("collapse").textContent = collapsed ? "展开" : "收起";
    $("collapse").setAttribute("aria-expanded", String(!collapsed));
  };
  $("clear").onclick = () => { $("confirmation").hidden = false; };
  $("disable").onclick = () => void mutate({ type: "disable" });
  $("cancel-clear").onclick = () => { $("confirmation").hidden = true; };
  $("confirm-clear").onclick = () => { $("confirmation").hidden = true; void mutate({ type: "clear" }); };
  addEventListener("scroll", positionCards, { capture: true, passive: true });
  addEventListener("resize", positionCards);
  const transitioning = (event) => {
    if (!enabled) return;
    if (event.composedPath().includes(host)) return;
    if (event.type === "keydown" && !["ArrowDown", "ArrowUp", "PageDown", "PageUp"].includes(event.key)) return;
    transitionUntil = performance.now() + 700;
    refreshCurrent();
  };
  addEventListener("wheel", transitioning, { passive: true });
  addEventListener("keydown", transitioning);
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.enabled) applyEnabled(changes.enabled.newValue);
    if (area === "local" && changes.selections) {
      items = changes.selections.newValue || [];
      renderList();
      positionCards();
    }
  });
  void (async () => {
    try {
      const [savedItems, settings] = await Promise.all([
        request({ type: "list" }), chrome.storage.local.get("enabled"),
      ]);
      items = savedItems;
      ready = true;
      renderList();
      applyEnabled(settings.enabled);
    }
    catch (error) {
      host.style.display = "block";
      status(`读取失败：${error.message}；更新扩展后请刷新抖音页。`);
    }
  })();
})();
