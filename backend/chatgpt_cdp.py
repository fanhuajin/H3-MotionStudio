"""backend.chatgpt_cdp — **静默**驱动 ChatGPT 桌面端出图（CDP，不抢前台、不动鼠标）。

与 `chatgpt_desktop`（坐标点击版）的区别：
  - 坐标版：要抢前台、要接管鼠标键盘、要处理「另存为」对话框 —— 会打扰用户。
  - CDP 版：连上专用实例的调试端口，**直接操作真实 DOM**，窗口可以最小化着跑。

前提（已实测）：
  - 真实登录 profile 启动时应用**不开放**调试端口；**专用 profile** 才会开。
  - 但登录态存在 `~/.codex/auth.json`（不在 Chromium profile 里），所以专用 profile
    **自动就是登录状态**，不需要重新登录。
  - 端口必须避开主实例已占用的 9222（否则端口不生效），默认用 9444。

生成图在 DOM 里是 `<img src="blob:app://-/...">`：
  - 它**不会落盘**（所以监听 generated_images 是白等）；
  - `fetch(blob:)` 会被 CSP 拦（实测 Failed to fetch）；
  - **canvas 绘制后 toDataURL** 可行（同源 blob 不污染画布）——这就是取图方式。

⚠️ 调用方必须保证单链路（同一时刻只有一处调用）。
"""
from __future__ import annotations

import base64
import logging
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("batch.chatgpt_cdp")

DEFAULT_PORT = 9444
DEFAULT_PROFILE = Path.home() / ".codex-automation" / "profile"

# 生成图默认落盘目录（用户 2026-09-15：「建议保存到 E 盘」）
DEFAULT_IMAGE_DIR = Path(r"E:\AI_Exports\H3-MotionStudio\ChatGPT生成图")


def image_dir() -> Path:
    """生成图保存目录，可用 H3_CHATGPT_IMAGE_DIR 覆盖。"""
    import os
    raw = os.getenv("H3_CHATGPT_IMAGE_DIR")
    p = Path(raw) if raw else DEFAULT_IMAGE_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p

# 取页面上「新的」blob 大图（生成结果）
_JS_LIST_BLOBS = """
() => Array.from(document.querySelectorAll('img'))
  .filter(e => (e.getAttribute('src') || '').startsWith('blob:') && e.naturalWidth > 300)
  .map(e => e.getAttribute('src'))
"""

# canvas 导出（fetch(blob:) 被 CSP 拦，只能走 canvas）
_JS_CANVAS_EXPORT = """
(src) => new Promise((resolve, reject) => {
  const img = Array.from(document.querySelectorAll('img')).find(e => e.getAttribute('src') === src);
  if (!img) { reject(new Error('img not found')); return; }
  const c = document.createElement('canvas');
  c.width = img.naturalWidth || img.width;
  c.height = img.naturalHeight || img.height;
  const ctx = c.getContext('2d');
  ctx.drawImage(img, 0, 0, c.width, c.height);
  resolve(c.toDataURL('image/png'));
})
"""


def endpoint(port: int | None = None) -> str:
    import os
    p = port or int(os.getenv("H3_CHATGPT_CDP_PORT", str(DEFAULT_PORT)))
    return f"http://127.0.0.1:{p}"


def _exe() -> str:
    """桌面端 exe：优先运行中的 ChatGPT.exe，其次 Get-AppxPackage，再 glob。"""
    import glob
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            if (p.info.get("name") or "").lower() == "chatgpt.exe":
                exe = p.info.get("exe") or ""
                if exe and Path(exe).is_file():
                    return exe
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage OpenAI.Codex).InstallLocation"],
            capture_output=True, text=True, timeout=30,
        )
        loc = (out.stdout or "").strip()
        if loc:
            cand = Path(loc) / "app" / "ChatGPT.exe"
            if cand.is_file():
                return str(cand)
    except Exception:
        pass
    cands = glob.glob(r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\ChatGPT.exe")
    if cands:
        return cands[-1]
    raise FileNotFoundError("未找到 ChatGPT 桌面端 exe")


def cdp_ready(port: int | None = None) -> bool:
    import httpx
    try:
        return httpx.get(f"{endpoint(port)}/json/version", timeout=2).status_code == 200
    except Exception:
        return False


def ensure_instance(port: int | None = None, profile: Path | None = None,
                    timeout: float = 60.0) -> str:
    """确保「专用实例 + CDP 端口」在跑，返回 endpoint。不会动用户的主实例。"""
    import os
    ep = endpoint(port)
    if cdp_ready(port):
        return ep

    profile = Path(os.getenv("H3_CHATGPT_AUTOMATION_PROFILE") or (profile or DEFAULT_PROFILE))
    profile.mkdir(parents=True, exist_ok=True)
    p = port or int(os.getenv("H3_CHATGPT_CDP_PORT", str(DEFAULT_PORT)))

    subprocess.Popen(
        [_exe(), f"--remote-debugging-port={p}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp_ready(port):
            time.sleep(1.5)  # 让页面加载
            return ep
        time.sleep(0.5)
    raise RuntimeError(f"专用实例的 CDP 端口 {p} 在 {timeout:.0f} 秒内未就绪")


def _page(browser):
    """挑真正的主聊天页。

    注意：桌面端会同时存在 `detached-window.html`（分离窗口）和 `avatar-overlay` 等页面，
    它们的标题也都是 "ChatGPT"，抓错就会操作到空白页（踩过）。
    这里要求 URL 是 index.html 且不是 detached/avatar，并带输入框。
    """
    pages = browser.contexts[0].pages if browser.contexts else []
    if not pages:
        raise RuntimeError("桌面上没有可用页面")
    best = None
    for pg in pages:
        try:
            url = pg.url or ""
            if "detached" in url or "avatar-overlay" in url:
                continue
            if "index.html" not in url:
                continue
            if pg.locator("div.ProseMirror").count():
                return pg
            if best is None:
                best = pg
        except Exception:
            continue
    if best is not None:
        return best
    for pg in pages:
        try:
            if pg.locator("div.ProseMirror").count():
                return pg
        except Exception:
            continue
    return pages[0]


def _new_chat(page) -> None:
    """开一个新对话。

    用 **JS 直接 click**，不用 Playwright 的 click()：后者会先把元素滚到可视区，
    在桌面端表现为「页面滑来滑去」（用户实测反馈）。JS click 不产生滚动。
    """
    ok = page.evaluate(
        """() => {
            const sels = [
                'button[aria-label*="开始新聊天"]',
                'button[aria-label*="新聊天"]',
                'a[href="/"]',
            ];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el) { el.click(); return s; }
            }
            // 退一步：按可见文本找「新聊天 / 新对话」
            const cands = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
            const hit = cands.find(e => /^(新聊天|新对话)$/.test((e.innerText || '').trim()));
            if (hit) { hit.click(); return 'text'; }
            return null;
        }"""
    )
    if ok:
        page.wait_for_timeout(1500)
    else:
        logger.warning("没找到「新对话」入口，继续用当前对话")


def _focus_composer(page) -> None:
    """把真实焦点放进输入框，**且不滚动页面**。

    要点：JS `el.focus()` 能让文字插进去，但**不足以让 Enter 触发发送**
    （实测：内容留在输入框里发不出去）。所以这里用 `page.mouse.click` 在输入框
    的包围盒中心点一下——真实鼠标事件能建立焦点，而 `mouse.click` 不像
    `locator.click()` 那样会自动把元素滚进可视区，所以不会「滑来滑去」。
    """
    try:
        box = page.locator("div.ProseMirror").first
        bb = box.bounding_box()
        if bb:
            # **必须点靠近底部**：长提示词会把输入框撑得很高（实测 1740px），
            # 顶部 y 变成负值（跑到视口外面），点中心会落到视口外 → 拿不到焦点。
            vh = page.evaluate("() => window.innerHeight") or 900
            y = min(bb["y"] + bb["height"] - 25, vh - 15)
            y = max(y, 10)
            x = bb["x"] + bb["width"] / 2
            page.mouse.click(x, y)
            page.wait_for_timeout(300)
            focused = page.evaluate(
                """() => {
                    const a = document.activeElement;
                    if (!a) return false;
                    return a.closest('div.ProseMirror') !== null || a.tagName === 'BODY' ? 
                           a.closest('div.ProseMirror') !== null : false;
                }"""
            )
            if focused:
                return
    except Exception:
        pass
    # 兜底：JS 聚焦
    page.evaluate(
        """() => {
            const el = document.querySelector('div.ProseMirror');
            if (el) el.focus();
        }"""
    )
    page.wait_for_timeout(250)


def _clear_composer(page, box) -> None:
    """彻底清空输入框：**文字 + 附件**（全部用 JS，避免滚动）。

    实测踩过的大坑：上一次失败留下的**附件不会自己消失**，会在输入框里越堆越多
    （用户看到「怎么会有三张图片」——候选图那次的 2 张 + 封面那次的 1 张叠在一起），
    最后连测试文字一起被发出去。所以每次生成前必须把附件也点掉。
    """
    try:
        _focus_composer(page)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(300)
    except Exception:
        pass
    # 附件：关闭按钮是 `pointer-events-none opacity-0`（只在悬停时可点），
    # Playwright 的 click() 会超时，所以用 JS 直接触发。
    for _ in range(12):
        try:
            if not page.locator('button[aria-label^="移除"]').count():
                break
            clicked = page.evaluate(
                """() => {
                    const b = document.querySelector('button[aria-label^="移除"]');
                    if (!b) return false;
                    b.click();
                    return true;
                }"""
            )
            if not clicked:
                break
            page.wait_for_timeout(400)
        except Exception:
            break


def _list_blobs(page) -> list[str]:
    try:
        return page.evaluate(_JS_LIST_BLOBS)
    except Exception:
        return []


def _export_png(page, src: str) -> bytes | None:
    try:
        data_url = page.evaluate(_JS_CANVAS_EXPORT, src)
    except Exception:
        logger.exception("canvas 导出失败")
        return None
    m = re.match(r"data:image/\w+;base64,(.*)", data_url or "", re.S)
    if not m:
        return None
    return base64.b64decode(m.group(1))


def _resembles_input(data: bytes, images: list[str | Path], threshold: float = 2.0) -> bool:
    """取回的图是否与某张输入参考图几乎一致。

    一致说明抓到的是**附件被重新渲染后**的副本，而不是 ChatGPT 生成的结果，
    应该跳过继续等（用户实测反馈「获取的图片不对 不是gpt生成的」）。
    """
    try:
        import io as _io

        from PIL import Image, ImageChops

        got = Image.open(_io.BytesIO(data)).convert("L").resize((32, 32))
        for src in images:
            try:
                ref = Image.open(src).convert("L").resize((32, 32))
            except Exception:
                continue
            diff = ImageChops.difference(got, ref)
            hist = diff.histogram()
            mean = sum(i * n for i, n in enumerate(hist)) / max(1, sum(hist))
            if mean < threshold:
                return True
    except Exception:
        pass
    return False


def generate(
    images: list[str | Path],
    prompt: str,
    *,
    save_to: str | Path,
    timeout: float = 420.0,
    poll: float = 3.0,
    new_chat: bool = True,
) -> Path | None:
    """静默生成：连 CDP → 新对话 → 清空输入框 → 附图 → 填提示词 → 发送 → 等新图 → canvas 取回。

    返回落盘路径；超时/取图失败返回 None。调用方负责单链路。
    """
    from playwright.sync_api import sync_playwright

    ep = ensure_instance()
    save_to = Path(save_to)
    save_to.parent.mkdir(parents=True, exist_ok=True)

    pw = sync_playwright().start()
    browser = None
    try:
        browser = pw.chromium.connect_over_cdp(ep)
        page = _page(browser)

        if new_chat:
            # 新对话必须真的清空页面：否则残留的旧生成图会被当成「新图」取走
            # （实测踩过：取回的图和上一次一模一样，sha 完全相同）。
            for _ in range(3):
                _new_chat(page)
                if not _list_blobs(page):
                    break
            leftover = _list_blobs(page)
            if leftover:
                logger.warning("新对话后页面仍有 %d 张图，可能取到旧图", len(leftover))

        box = page.locator("div.ProseMirror").first
        # 生成前**彻底清空**（文字 + 附件），否则残留会叠加进这次请求
        _clear_composer(page, box)

        # 附图（隐藏的 file input 也能 set_input_files）
        attached: set[str] = set()
        if images:
            page.locator('input[type="file"]').first.set_input_files([str(p) for p in images])
            page.wait_for_timeout(2500)
            # **贴完图之后**再记录基线：否则贴进去的参考图会被当成「新生成的图」取回来
            # （实测踩过：取回来的其实是图二本人，因为 canvas 重编码后字节数不同所以没被发现）。
            attached = set(_list_blobs(page))

        before = set(_list_blobs(page))

        # 填提示词（contenteditable 用 insert_text，长文也快）
        _focus_composer(page)
        page.keyboard.insert_text(prompt)
        page.wait_for_timeout(1000)

        # 发送并**校验**：输入框被清空才算真的发出去；没清空就重试
        def _composer_empty() -> bool:
            try:
                return not box.inner_text().strip()
            except Exception:
                return True

        sent = False
        for _ in range(3):
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            if _composer_empty():
                sent = True
                break
        if not sent:
            # 兜底：点发送按钮（用 mouse，不滚动）
            try:
                bb = page.evaluate(
                    """() => {
                        const sels = ['button[aria-label*="发送"]', 'button[data-testid="send-button"]'];
                        for (const s of sels) {
                            const b = document.querySelector(s);
                            if (b) { const r = b.getBoundingClientRect();
                                     return {x: r.x + r.width/2, y: r.y + r.height/2}; }
                        }
                        const box = document.querySelector('div.ProseMirror');
                        const form = box && (box.closest('form') || box.parentElement.parentElement);
                        if (form) {
                            const btns = Array.from(form.querySelectorAll('button'));
                            const last = btns[btns.length - 1];
                            if (last) { const r = last.getBoundingClientRect();
                                        return {x: r.x + r.width/2, y: r.y + r.height/2}; }
                        }
                        return null;
                    }"""
                )
                if bb:
                    page.mouse.click(bb["x"], bb["y"])
                    page.wait_for_timeout(1800)
                    if _composer_empty():
                        sent = True
            except Exception:
                pass
        if not sent:
            logger.warning("发送未生效：输入框仍有内容")
        else:
            logger.info("已发送（%s）", save_to.name)

        # 等**真正新出现**的 blob 大图（参考图已排除在基线里）。
        # 逐个候选校验：和输入参考图长得像的就是「重新渲染的附件」，跳过再等。
        deadline = time.time() + timeout
        data: bytes | None = None
        while time.time() < deadline:
            time.sleep(poll)
            now = _list_blobs(page)
            fresh = [s for s in now if s not in before and s not in attached]
            for cand in reversed(fresh):  # 最新的优先（助手回复排在最后）
                blob = _export_png(page, cand)
                if not blob:
                    continue
                if _resembles_input(blob, images):
                    logger.info("跳过一张与参考图一致的候选（应是附件重绘）")
                    continue
                data = blob
                break
            if data:
                break
        if not data:
            logger.warning("等待生成图超时（%s）", save_to.name)
            return None

        save_to.write_bytes(data)
        return save_to
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
