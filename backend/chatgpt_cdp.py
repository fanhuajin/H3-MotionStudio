"""backend.chatgpt_cdp — **静默**驱动 ChatGPT 桌面端出图（CDP，不抢前台、不动鼠标）。

与 `chatgpt_desktop`（坐标点击版）的区别：
  - 坐标版：要抢前台、要接管鼠标键盘、要处理「另存为」对话框 —— 会打扰用户。
  - CDP 版：连上专用实例的调试端口，**直接操作真实 DOM**，窗口可以最小化着跑。

前提（已实测）：
  - 真实登录 profile 启动时应用**不开放**调试端口；**专用 profile** 才会开。
  - 但登录态存在 `~/.codex/auth.json`（不在 Chromium profile 里），所以专用 profile
    **自动就是登录状态**，不需要重新登录。
  - 端口必须避开主实例已占用的 9222（否则端口不生效），默认用 9444。

**提示词一律走附件（.txt），不再往输入框里粘长文**（用户 2026-09-16：「9比16图片.txt 和
4比3图片.txt 提示词太长了自动化的适合需要将它变更为文本 而不是在输入框框中输入了
不然会被风控」）。9:16 那份就有 1.3 万字，`Input.insertText` 一次性插进 ProseMirror
既会被风控盯上，也会把输入框撑到 1700px 高（点焦点都得改成点底部）。
桌面端输入区有两个 `input[type=file]`：第一个是 `accept="image/*"`（图片），
第二个没有 accept（通用附件）——.txt 走第二个。实测草稿里能正常出现
`提示词.txt` 附件芯片。想要回旧的粘贴模式：`H3_CHATGPT_PROMPT_AS_FILE=0`。

**2026-09-17 改版适配（新版桌面端 26.908.9136.0，实测）**：App 换了新外壳之后，
下面这几件事全变了，照着旧假设写就会「启动了没反应 / 到了抖音那步不发送」：

  1. **`input[type="file"]` 已经不存在了**（原生 querySelector 与 Playwright 穿透 shadow DOM
     都是 0 个）。「+ → 文件和文件夹」走的是 **Electron 原生文件对话框**，CDP 拿不到
     （实测点了会弹出一个「选择文件」窗口，Playwright 的 file chooser 事件不触发）。
     现在挂图走 **CDP 真实拖放**：`Input.dispatchDragEvent(dragEnter/dragOver/drop)` +
     `data.files=[本地路径]`，输入框里会出现 `移除“人物图.png”` 附件芯片（实测有效、完全静默）。
  2. **生成图不再只有 blob**：新外壳把出图写到 `~/.codex/generated_images/<线程>/`
     （`exec-*.png` / `call-*.png`），页面上是 `app://fs/@fs/...`。所以取图**两条路一起等**：
     落盘目录出现新文件就用它，blob 大图（老版本）仍然保留。
  3. **发送按钮只在输入框有内容时才存在**（空输入框时页面上根本没有「发送」按钮，
     也没有 `<form>`，旧实现里「表单里最后一个按钮」的兜底已经失效）。
  4. **页面上的「停止生成」按钮不代表我们发出去了**：上一轮回复还没结束时它一直在，
     旧实现 `if _generating_now(page): return True` 会误判成「已发送」，于是那条消息
     **永远留在输入框里**——这正是用户 2026-09-17 看到的「到抖音图片了就不发送了」。
     现在发送的判定只有一个：**输入框正文与附件都空了**。

生成图取回方式（两选一，先出现先用）：
  - 落盘：`~/.codex/generated_images/**` 里在「发送之后」新出现的图（新外壳）；
  - 页面：`<img src="blob:app://-/...">` → canvas 绘制后 `toDataURL`（老外壳；`fetch(blob:)`
    会被 CSP 拦）。

**附图之后必须等上传完再发送**（2026-09-16 用户：「有的时候图片上传的比较慢 他就不会
点击开始了 一直卡在那里不动」）：附件上传期间发送按钮是 `disabled` 的，这时点它 / 按回车
都不会发出去 —— 由 `wait_for_attachments()` 轮询到按钮恢复可点，`_send()` 再在
`H3_CHATGPT_SEND_TIMEOUT`（默认 **300 秒**）内反复尝试并校验；确实发不出去就**立刻返回 None**，
不再白等满出图 timeout。上传等待上限见 `H3_CHATGPT_UPLOAD_TIMEOUT`（默认 **600 秒**）。
用户 2026-09-17 明确：「发送和等待生成图片的时间可以久一点无所谓」——所以这几个上限都比
老版本放宽了 5 倍左右，宁可慢也不要误判失败。

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

# 新版桌面端把出图落到这里（每个线程一个子目录，文件名形如 exec-*.png / call-*.png）。
# 用户 2026-09-17 实测：新外壳的页面上生成图是 `app://fs/@fs/...`，`blob:` 大图为 0 个，
# 只等 blob 会一直等到超时 —— 必须同时盯这个目录。
GENERATED_IMAGES_DIR = Path.home() / ".codex" / "generated_images"

# 附件上传的等待上限（秒）。**上传没完就发送 = 什么都没发出去**：
# 附件上传期间发送按钮带 `disabled`（实测 2.1 MB 的人物图正常网速要 ~10 秒，
# 慢的时候几十秒），这时点按钮、按回车都不认。
# 用户 2026-09-16 反馈：「有的时候图片上传的比较慢 他就不会点击开始了 一直卡在那里不动」。
DEFAULT_UPLOAD_TIMEOUT = 600.0
# 发送重试窗口（秒）：发送按钮可点之后，在这段时间里反复尝试，直到确认真的发出去。
# 用户 2026-09-17：「发送和等待生成图片的时间可以久一点无所谓」——从 60 秒放宽到 300 秒。
DEFAULT_SEND_TIMEOUT = 300.0
# 等出图的默认上限（秒）。同样按用户 2026-09-17 的要求放宽。
DEFAULT_IMAGE_TIMEOUT = 900.0
# 等「上一轮回复结束」的上限（秒）：上一轮没结束就发下一条，消息会卡在输入框里。
# 2026-09-17：从 900 秒收到 180 秒 —— 新版外壳回合结束后还会把「停止」按钮留在界面上，
# 死等没有意义；超时后交给 `_send()` 处理（它会点掉那个卡住的按钮）。
DEFAULT_IDLE_TIMEOUT = 180.0
# 新版外壳「停止」按钮卡住多久之后就点掉它（秒）。
DEFAULT_STUCK_GRACE = 90.0
# 等生成图时的心跳间隔（秒）——控制台长时间没输出会被误认为卡死。
WAIT_HEARTBEAT_SECONDS = 30.0


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

# 发送按钮的位置 + **可点状态**。
# 附件上传中它是 `disabled`（实测：`<button type="submit" aria-label="发送" disabled
# aria-disabled="true">`，计算样式 opacity 0.5），上传完 disabled 才被摘掉 ——
# 「能不能发」只能看这个状态，看输入框有没有内容判断不出来。
_JS_SEND_BUTTON = """
() => {
  const sels = ['button[aria-label*="发送"]', 'button[aria-label*="Send"]',
                'button[data-testid="send-button"]'];
  let b = null;
  for (const s of sels) { const hit = document.querySelector(s); if (hit) { b = hit; break; } }
  if (!b) {  // 版本换了选择器就退回「表单里最后一个按钮」（与旧实现一致）
    const box = document.querySelector('div.ProseMirror');
    const form = box && (box.closest('form') || (box.parentElement && box.parentElement.parentElement));
    if (form) { const btns = Array.from(form.querySelectorAll('button')); b = btns[btns.length - 1] || null; }
  }
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return {
    disabled: b.hasAttribute('disabled') || b.getAttribute('aria-disabled') === 'true',
    x: r.x + r.width / 2,
    y: r.y + r.height / 2,
  };
}
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



def wait_for_composer(page, timeout: float = 20.0):
    """等待新版聊天输入框真正挂载并有可用尺寸。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = page.evaluate("""() => {
                const el = document.querySelector('div.ProseMirror[role="textbox"], [contenteditable="true"][role="textbox"]');
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {x:r.x, y:r.y, w:r.width, h:r.height};
            }""")
            if info and info.get("w", 0) > 50 and info.get("h", 0) > 5:
                return page.locator('div.ProseMirror[role="textbox"], [contenteditable="true"][role="textbox"]').first
        except Exception:
            pass
        page.wait_for_timeout(300)
    return None


def _new_chat(page) -> None:
    """开一个新对话。

    用 **JS 直接 click**，不用 Playwright 的 click()：后者会先把元素滚到可视区，
    在桌面端表现为「页面滑来滑去」（用户实测反馈）。JS click 不产生滚动。
    """
    ok = page.evaluate(
        """() => {
            const all = Array.from(document.querySelectorAll('*'));
            const direct = all.find(e => /^(新聊天|新对话|新的聊天|New chat)$/i.test((e.innerText || '').trim()));
            if (direct) {
                const clickable = direct.closest('button, a, [role="button"]') || direct;
                clickable.click();
                return 'text';
            }
            const byLabel = Array.from(document.querySelectorAll('button, a, [role="button"]'))
                .find(e => /新聊天|新对话|新的聊天|开始新聊天|New chat/i.test(
                    (e.getAttribute('aria-label') || '') + ' ' +
                    (e.getAttribute('title') || '') + ' ' +
                    (e.getAttribute('data-testid') || '')
                ));
            if (byLabel) { byLabel.click(); return 'label'; }
            return null;
        }"""
    )
    if ok:
        page.wait_for_timeout(1500)
    else:
        logger.warning("没找到「新对话」入口，继续用当前对话")



def ensure_chat_mode(page, timeout: float = 15.0) -> bool:
    """强制切到「聊天」模式，避免误入 Work/Codex 额度。"""
    try:
        state = page.evaluate("""() => {
            const txt = document.body ? (document.body.innerText || '') : '';
            const buttons = Array.from(document.querySelectorAll('button, [role="button"], a'));
            const chat = buttons.find(e => /^(聊天|Chat)$/i.test((e.innerText || '').trim()));
            const work = buttons.find(e => /^(工作|Work)$/i.test((e.innerText || '').trim()));
            const out = /out of Codex and Work usage/i.test(txt);
            return {hasChat: !!chat, hasWork: !!work, out};
        }""")
        if state and state.get("hasChat"):
            clicked = page.evaluate("""() => {
                const buttons = Array.from(document.querySelectorAll('button, [role="button"], a'));
                const chat = buttons.find(e => /^(聊天|Chat)$/i.test((e.innerText || '').trim()));
                if (!chat) return false;
                chat.click();
                return true;
            }""")
            if clicked:
                page.wait_for_timeout(1200)

        deadline = time.time() + timeout
        while time.time() < deadline:
            body = page.evaluate("() => document.body ? (document.body.innerText || '') : ''")
            if not re.search(r"out of Codex and Work usage", body or "", re.I):
                logger.info("已确认处于聊天模式，不使用 Work/Codex 额度")
                return True
            page.wait_for_timeout(500)

        logger.warning("仍检测到 Work/Codex 额度耗尽提示，未能切回聊天模式")
        return False
    except Exception:
        logger.exception("切换聊天模式失败")
        return False


def _focus_composer(page) -> None:
    """只用 DOM 聚焦输入框，不调用 page.mouse.click，不抢用户鼠标/前台。"""
    try:
        page.evaluate(
            """() => {
                const el = document.querySelector('div.ProseMirror');
                if (el) {
                    el.focus({preventScroll: true});
                    return true;
                }
                return false;
            }"""
        )
        page.wait_for_timeout(100)
    except Exception:
        pass


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


# ---------------- 2026-09-17：新版桌面端的附件与出图 ----------------
# 新版外壳**没有** `input[type="file"]`（实测原生与 Playwright 穿透 shadow DOM 都是 0），
# 「+ → 文件和文件夹」是 Electron 原生对话框（CDP 拿不到）。唯一可自动化、且完全静默的
# 挂图方式就是**真实拖放**：CDP `Input.dispatchDragEvent` 带上 `data.files=[本地路径]`。
def attachment_names(page) -> list[str]:
    """输入框里挂着的附件名（从「移除“xxx”」按钮的 aria-label 里取）。"""
    try:
        return [
            str(a)
            for a in page.evaluate(
                """() => Array.from(document.querySelectorAll('button'))
                     .map(e => e.getAttribute('aria-label') || '')
                     .filter(a => a.indexOf('移除') === 0)"""
            )
        ]
    except Exception:
        return []


def _composer_has_files(page, names: list[str]) -> bool:
    """附件芯片里是不是已经出现了这些文件名（拖放成功的判据）。"""
    if not names:
        return True
    chips = " ".join(attachment_names(page))
    return any(Path(n).name in chips for n in names)


def drag_drop_files(page, paths: list[str | Path], *, what: str = "") -> bool:
    """用 CDP 真实拖放把文件丢进输入框（新版外壳唯一的挂图方式）。

    返回是否挂上（按「移除“xxx”」芯片判断）。实测有效且完全静默：
    不弹原生选择对话框、不动鼠标、不抢前台。
    """
    if not paths:
        return True
    names = [str(p) for p in paths]
    if any(not Path(p).is_file() for p in names):
        logger.warning("拖放失败：有文件不存在 %s", [p for p in names if not Path(p).is_file()])
        return False
    try:
        box_info = page.evaluate(
            """() => {
                const b = document.querySelector('div.ProseMirror');
                if (!b) return null;
                const r = b.getBoundingClientRect();
                return {x: r.x + r.width / 2, y: r.y + Math.min(r.height / 2, 24)};
            }"""
        )
    except Exception:
        box_info = None
    if not box_info or not box_info.get("x"):
        logger.warning("拖放失败：找不到输入框坐标")
        return False

    try:
        sess = page.context.new_cdp_session(page)
    except Exception:
        logger.exception("拖放失败：拿不到 CDP 会话")
        return False
    data = {
        "items": [{"mimeType": "image/png", "data": ""}],
        "files": names,
        "dragOperationsMask": 1,
    }
    try:
        for etype in ("dragEnter", "dragOver", "drop"):
            sess.send(
                "Input.dispatchDragEvent",
                {"type": etype, "x": box_info["x"], "y": box_info["y"], "data": data},
            )
            page.wait_for_timeout(600)
    except Exception:
        logger.exception("拖放派发失败%s", f"｜{what}" if what else "")
        return False

    # 芯片是异步渲染的：轮询 15 秒
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if _composer_has_files(page, names):
            logger.info("附件已挂上（拖放）：%s%s", [Path(n).name for n in names],
                        f"｜{what}" if what else "")
            return True
        page.wait_for_timeout(500)
    logger.warning("拖放之后没看到附件芯片：%s%s", names, f"｜{what}" if what else "")
    return False


def attach_images(page, images: list[str | Path], *, what: str = "") -> str:
    """把参考图挂进输入框，返回实际用的方式：`input` / `dragdrop` / `''`（没挂上）。

    优先老的 `input[type=file]`（老版本外壳仍是这条路，一次能挂多张）；
    新版没有这个 input，自动改用 CDP 拖放（逐张派发，一次拖放带全部路径）。
    """
    if not images:
        return ""
    paths = [str(p) for p in images]
    try:
        count = page.locator('input[type="file"]').count()
    except Exception:
        count = 0
    if count:
        try:
            # 图片口是第一个（第二个才是没有 accept 的通用附件口）
            page.locator('input[type="file"]').first.set_input_files(paths)
            logger.info("附件已提交（file input）：%s%s", [Path(p).name for p in paths],
                        f"｜{what}" if what else "")
            return "input"
        except Exception:
            logger.exception("set_input_files 失败，改用拖放%s", f"｜{what}" if what else "")
    for path in paths:  # 拖放：一张一张派发，芯片会依次出现
        if not drag_drop_files(page, [path], what=what):
            return ""
    return "dragdrop" if _composer_has_files(page, paths) else ""


def wait_until_idle(page, *, timeout: float | None = None, what: str = "") -> bool:
    """等**上一轮回复结束**再动手（页面上没有「停止生成」按钮）。

    为什么必须等：上一轮还在生成时发下一条，输入框里的东西发不出去（发送按钮此时不存在或
    回车不生效），消息就静静躺在输入框里 —— 用户 2026-09-17 看到的「到抖音图片了就不发送了」。

    新版外壳还有个坑：**回合结束后「停止」按钮仍然挂着**（实测 rollout 日志里 `task_complete`
    之后 5 分钟按钮还在），这时死等没意义 —— 超过 `H3_CHATGPT_STUCK_GRACE`（90 秒）就
    JS 点掉它（真实鼠标点不到，见 `_click_stop`），然后继续。
    """
    limit = _env_float("H3_CHATGPT_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT)
    if timeout is not None and timeout > 0:
        limit = float(timeout)
    grace = _env_float("H3_CHATGPT_STUCK_GRACE", DEFAULT_STUCK_GRACE)
    started = time.time()
    last_note = 0.0
    unstuck = False
    while _generating_now(page):
        waited = time.time() - started
        if not unstuck and waited >= grace:
            unstuck = True
            if _click_stop(page):
                logger.info("「停止」按钮挂了 %.0f 秒还没消失（回合其实已结束），点掉它%s",
                            waited, f"｜{what}" if what else "")
            page.wait_for_timeout(1500)
            continue
        if waited >= limit:
            logger.warning("等了 %.0f 秒上一轮还在生成，继续往下走%s", waited,
                           f"｜{what}" if what else "")
            return False
        if waited - last_note >= 5.0:
            last_note = waited
            logger.info("等上一轮回复结束……已等 %.0f 秒%s", waited, f"｜{what}" if what else "")
        page.wait_for_timeout(700)
    return True


def snapshot_generated_images() -> set[str]:
    """当前 `~/.codex/generated_images/**` 下所有文件的路径（新版外壳的出图落盘处）。"""
    root = GENERATED_IMAGES_DIR
    if not root.is_dir():
        return set()
    try:
        return {str(p) for p in root.rglob("*.png")}
    except Exception:
        return set()


def new_generated_images(before: set[str], since: float) -> list[str]:
    """发送之后**新出现**的出图（按修改时间正序）。

    只认「不在快照里」且「mtime 不早于发送时刻」的文件，避免把用户自己出图的结果抢过来。
    """
    root = GENERATED_IMAGES_DIR
    if not root.is_dir():
        return []
    out: list[tuple[float, str]] = []
    try:
        for p in root.rglob("*.png"):
            s = str(p)
            if s in before:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime + 0.5 < since or st.st_size < 50 * 1024:
                continue
            out.append((st.st_mtime, s))
    except Exception:
        return []
    out.sort()
    return [s for _, s in out]


def _stable_file(path: str | Path, *, wait: float = 1.5) -> bool:
    """文件还在写就别急着拷（两次 stat 大小一致才算稳定）。"""
    p = Path(path)
    try:
        first = p.stat().st_size
    except OSError:
        return False
    time.sleep(wait)
    try:
        return p.stat().st_size == first and first > 0
    except OSError:
        return False


def _env_raw(name: str, default: str = "") -> str:
    """读配置：**必须经 `settings.env_value` 回读**。

    后端常由别的进程拉起，`os.getenv` 只有启动那一刻的快照，用户级环境变量改了它看不见
    （`H3_CHATGPT_PROMPT_AS_FILE` 就这么静默失效过）。
    """
    try:
        from . import settings

        return str(settings.env_value(name, default) or default)
    except Exception:
        import os

        return str(os.getenv(name, default) or default)


def _env_float(name: str, default: float) -> float:
    """读一个秒数配置（写坏了就退回默认值，绝不因为一个环境变量把链路弄崩）。"""
    try:
        value = float(_env_raw(name, str(default)).strip())
    except Exception:
        return default
    return value if value > 0 else default


def prompt_as_file_enabled() -> bool:
    """提示词是否走 .txt **附件**。默认**不走附件**（直接作为消息正文发出去）。

    2026-09-16 用户最终决定：「不要上传文本了」—— 桌面端不接受上传文本这条路，
    提示词一律当**消息正文**发（正文本来就是这次请求要发的内容）。附件那条路
    （`attach_prompt_file()` / `H3_CHATGPT_PROMPT_AS_FILE=1`）保留代码但默认关闭。

    这里走 `settings.env_value`：后端常由别的进程拉起，`os.getenv` 只有启动时的快照，
    用户级环境变量必须回读，否则开关会静默失效。
    """
    return _env_raw("H3_CHATGPT_PROMPT_AS_FILE", "0").strip().lower() in {"1", "true", "yes", "on"}


def prompt_say_enabled() -> bool:
    """附件模式**是否再补一句话**。默认**不补**（用户 2026-09-16：「你不是已经又提示词文件了吗
    问什么输入框还要重新输入一遍」）—— 附件里已经是完整规则，输入框保持空的，直接发附件。

    只有当实测某个版本的桌面端「只挂附件、一个字都不发」不触发出图时，
    才用 `H3_CHATGPT_PROMPT_SAY=1` 补上 `default_prompt_instruction` 那句。
    """
    return _env_raw("H3_CHATGPT_PROMPT_SAY", "0").strip().lower() in {"1", "true", "yes", "on"}


def default_prompt_instruction(name: str = "提示词.txt", *, has_images: bool = True) -> str:
    """附带的那句**短说明**（仅 `H3_CHATGPT_PROMPT_SAY=1` 时才用）。

    只给「去哪读规则 + 立刻出图」，避免模型只回一句「收到，请告诉我需求」。
    """
    where = "结合上面两张参考图，" if has_images else ""
    attachment = name or "随附的提示词文本"
    return f"请严格按照附件 {attachment} 里的全部规则，{where}直接生成最终图片。不需要再问我。"


def attach_prompt_file(page, prompt_file: str | Path, *, timeout: float = 15.0) -> bool:
    """把提示词 .txt 作附件挂进输入区，返回是否挂上。

    挑「没有 accept 属性」的那个 file input（桌面端实测：第一个是 `accept="image/*"`），
    挂完**轮询**等附件芯片出现（芯片是异步渲染的：实测只等 1.8 秒会误判成没挂上，
    于是走了「退回粘贴长文」的兜底 —— 那正是用户 2026-09-16 看到的
    「输入框你怎么还是输入了文本」）。
    """
    path = Path(prompt_file)
    if not path.is_file():
        logger.warning("提示词附件不存在：%s", path)
        return False
    name = path.name
    try:
        tool = page.locator('input[type="file"]:not([accept])')
        count = tool.count()
        if not count:
            # 没有通用附件口就不要硬塞：整个页面只有 `accept="image/*"` 的图片口，
            # 把 .txt 塞进图片口既挂不上，也会污染附件区（实测「怎么会有三张图片」）。
            logger.warning("没有找到通用附件输入口（只有图片口），无法挂提示词附件")
            return False
        tool.nth(count - 1).set_input_files(str(path))
    except Exception:
        logger.exception("挂提示词附件失败：%s", path)
        return False
    if _wait_for_attachment(page, name, timeout=timeout):
        return True
    logger.warning("等了 %.0f 秒附件芯片里还是没看到 %s，可能没挂上", timeout, name)
    return False


def _wait_for_attachment(page, name: str, *, timeout: float = 15.0, poll: float = 0.5) -> bool:
    """轮询等附件芯片出现（最多 `timeout` 秒）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _composer_mentions(page, name):
            return True
        page.wait_for_timeout(poll * 1000)
    return False


def _composer_mentions(page, text: str) -> bool:
    """输入区里能不能看到这个附件名。

    附件芯片**不在** `div.ProseMirror` 内部，而在表单容器里，且不同版本层级不一样 ——
    所以这里从 ProseMirror 一路往上找到 `<form>`（找不到就再上溯 8 层）逐层查。
    """
    try:
        return bool(
            page.evaluate(
                """(text) => {
                    const box = document.querySelector('div.ProseMirror');
                    if (!box) return false;
                    const scopes = [box];
                    let node = box.parentElement;
                    for (let i = 0; i < 10 && node; i += 1) {
                        scopes.push(node);
                        if (node.tagName === 'FORM') break;
                        node = node.parentElement;
                    }
                    return scopes.some(scope => (scope.innerText || '').includes(text));
                }""",
                text,
            )
        )
    except Exception:
        return False


def _generating_now(page) -> bool:
    """页面上有没有「停止生成」按钮 —— 发送成功后最可靠的信号。

    附件模式下输入框本来就是空的（没有文字可清空），只靠 `_composer_empty()` 判断不了，
    所以再加一条：发送后出现 stop 按钮 = 已经在生成了。
    """
    try:
        return bool(
            page.evaluate(
                """() => {
                    const sels = ['button[data-testid="stop-button"]',
                                  'button[aria-label*="停止"]',
                                  'button[aria-label*="Stop"]'];
                    return sels.some(s => !!document.querySelector(s));
                }"""
            )
        )
    except Exception:
        return False


def _send_button(page) -> dict | None:
    """发送按钮在哪、能不能点（`{disabled, x, y}`）；找不到返回 None。"""
    try:
        return page.evaluate(_JS_SEND_BUTTON)
    except Exception:
        return None


def _click_stop(page) -> bool:
    """**JS** 点掉「停止」按钮（真实鼠标点不到它 —— 实测该坐标被内容区的 div 盖住）。

    只用在「回合早就结束、按钮却还留着」这种卡住的状态上（见 `clear_stuck_stop`）。
    """
    try:
        return bool(
            page.evaluate(
                """() => {
                    const sels = ['button[data-testid="stop-button"]',
                                  'button[aria-label*="停止"]', 'button[aria-label*="Stop"]'];
                    for (const s of sels) {
                        const b = document.querySelector(s);
                        if (b) { b.click(); return true; }
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def clear_stuck_stop(page, *, grace: float | None = None, what: str = "") -> bool:
    """新版外壳会把「停止」按钮留在输入框里（回合结束后依然在），导致**发送按钮不出现**。

    用户 2026-09-17 看到的「到抖音图片了就不发送了」就是这个：同一对话里的第二条消息
    永远发不出去。实测（rollout 日志里 `task_complete` 之后 5 分钟按钮还在）确认它只是
    界面没清干净，所以这里等 `grace` 秒仍不见好转就 **JS 点掉它**，让发送按钮回来。

    返回是否点过。`grace` 默认 `H3_CHATGPT_STUCK_GRACE`（90 秒）。
    """
    if not _generating_now(page):
        return False
    limit = _env_float("H3_CHATGPT_STUCK_GRACE", 90.0)
    if grace is not None and grace > 0:
        limit = float(grace)
    started = time.time()
    while _generating_now(page):
        if time.time() - started >= limit:
            clicked = _click_stop(page)
            logger.info("「停止」按钮挂了 %.0f 秒还没消失，点掉它让发送按钮回来%s（%s）",
                        time.time() - started, f"｜{what}" if what else "",
                        "已点" if clicked else "没找到按钮")
            page.wait_for_timeout(1500)
            return clicked
        page.wait_for_timeout(700)
    return False


def _attachment_count(page) -> int:
    """输入框里挂着的附件数量（每个附件一个「移除“xxx”」按钮）。"""
    try:
        return page.locator('button[aria-label^="移除"]').count()
    except Exception:
        return 0


def wait_for_attachments(page, expected: int, *, timeout: float | None = None,
                         what: str = "") -> bool:
    """等附件挂上（并尽量等它上传完）再继续。

    为什么非等不可：附件上传期间发送按钮是 `disabled` 的，这时点它 / 按回车都不会发出去
    ——旧实现贴完图只 `wait_for_timeout(2500)` 就去发，网慢时消息留在输入框里没发出去，
    脚本却接着去「等生成图」，于是白等满 timeout 才报超时。用户看到的就是
    「图片上传的比较慢 他就不会点击开始了 一直卡在那里不动」（2026-09-16 反馈）。

    **2026-09-17 修正判据**：新版外壳在「只有附件、没有正文」时页面上**根本没有发送按钮**
    （实测 12 秒内一直 `None`），所以判据不能只认「按钮可点」，否则这里必然白等满 600 秒。
    现在的判据：芯片数量够 + （发送按钮可点 或 页面暂时没有发送按钮）。
    真正的「到底发出去没有」由 `_send()` 负责校验（输入框与附件都空了才算发出去），
    上传没完时 `_send()` 会一直重试到按钮可点，所以这里宽松一点是安全的。

    `expected`：这次要挂几个附件（0 个直接返回 True）。等待期间每 5 秒写一行 INFO 心跳。
    """
    if expected <= 0:
        return True
    limit = _env_float("H3_CHATGPT_UPLOAD_TIMEOUT", DEFAULT_UPLOAD_TIMEOUT)
    if timeout is not None and timeout > 0:
        limit = float(timeout)
    started = time.time()
    last_note = 0.0
    chips_ready_at: float | None = None
    while True:
        chips = _attachment_count(page)
        btn = _send_button(page)
        if chips >= expected:
            if btn is not None and not btn["disabled"]:
                logger.info("附件已上传完（%d 个，耗时 %.1f 秒，发送按钮可点）%s",
                            chips, time.time() - started, f"｜{what}" if what else "")
                return True
            if btn is None:
                # 没有正文时新版外壳不渲染发送按钮：等一小段确认不是「刚拖进去还没反应」
                if chips_ready_at is None:
                    chips_ready_at = time.time()
                elif time.time() - chips_ready_at >= 5.0:
                    logger.info("附件已挂上（%d 个，耗时 %.1f 秒；此时页面没有发送按钮，"
                                "输入正文后才会出现）%s",
                                chips, time.time() - started, f"｜{what}" if what else "")
                    return True
        waited = time.time() - started
        if waited >= limit:
            logger.warning(
                "等了 %.0f 秒附件还是没就绪（芯片 %d/%d，发送按钮%s）%s",
                waited, chips, expected,
                "找不到" if btn is None else ("仍禁用" if btn["disabled"] else "可点"),
                f"｜{what}" if what else "",
            )
            return False
        if waited - last_note >= 5.0:
            last_note = waited
            logger.info("附件正在上传……已等 %.0f 秒（芯片 %d/%d，发送按钮%s）%s",
                        waited, chips, expected,
                        "找不到" if btn is None else ("仍禁用" if btn["disabled"] else "可点"),
                        f"｜{what}" if what else "")
        page.wait_for_timeout(400)


def _send(page, box, *, timeout: float | None = None, what: str = "") -> bool:
    """发送并**校验**：只有「输入框正文与附件都空了」才算真的发出去。

    **绝不拿「停止生成」按钮当成功信号**（2026-09-17 修）：上一轮回复没结束时它一直都在，
    旧实现 `if _generating_now(page): return True` 会把「还没发」判成「已发送」，
    消息就永远留在输入框里 —— 用户看到的就是「到抖音图片了就不发送了」。
    现在遇到还在生成就**等它结束再发**。

    **必须等发送按钮可点再点**：附件上传中它是 disabled 的，点了等于没点（2026-09-16 实测）。
    新版外壳在输入框为空时**根本没有发送按钮**，所以有内容却找不到按钮时，改用回车 + JS click。
    重试窗口见 `H3_CHATGPT_SEND_TIMEOUT`（默认 300 秒，用户 2026-09-17 允许放宽）。
    """

    def _composer_empty() -> bool:
        try:
            return not box.inner_text().strip()
        except Exception:
            return True

    def _has_content() -> bool:
        """输入框里还有没有东西要发：正文或附件（纯附件模式下正文本来就是空的）。"""
        return (not _composer_empty()) or _attachment_count(page) > 0

    limit = _env_float("H3_CHATGPT_SEND_TIMEOUT", DEFAULT_SEND_TIMEOUT)
    if timeout is not None and timeout > 0:
        limit = float(timeout)
    started = time.time()
    deadline = started + limit
    enter_budget = 4      # 有正文时先试回车（不依赖按钮坐标），试几次不成改点按钮
    last_note = 0.0
    attempts = 0
    idle_waited = False
    unstuck = False
    while True:
        if not _has_content():
            logger.info("发送成功（输入框已空，耗时 %.1f 秒，尝试 %d 次）%s",
                        time.time() - started, attempts, f"｜{what}" if what else "")
            return True
        if _generating_now(page):
            # 上一轮还在生成 / 或者新版外壳把「停止」留在了界面上 —— 两种都不能发
            if not idle_waited:
                idle_waited = True
                logger.info("上一轮回复还没结束，先等它跑完再发%s", f"｜{what}" if what else "")
            if not unstuck and time.time() - started >= _env_float("H3_CHATGPT_STUCK_GRACE", 90.0):
                unstuck = True
                if _click_stop(page):
                    logger.info("「停止」按钮一直不消失（回合其实已结束），已点掉它%s",
                                f"｜{what}" if what else "")
                    page.wait_for_timeout(1500)
                    continue
            if time.time() >= deadline:
                logger.warning("发送失败：等了 %.0f 秒上一轮仍在生成（内容还在输入框里）%s",
                               time.time() - started, f"｜{what}" if what else "")
                return False
            page.wait_for_timeout(700)
            continue
        btn = _send_button(page)
        if btn is not None and not btn["disabled"]:
            attempts += 1
            clicked = False
            if not _composer_empty():
                if enter_budget > 0:
                    enter_budget -= 1
                    _focus_composer(page)   # 焦点不在输入框里，回车不生效（新版尤其明显）
                    page.keyboard.press("Enter")
                    clicked = True
            if not clicked and btn.get("x") is not None:
                # 发送按钮的坐标被内容区盖住（实测鼠标点不中），所以先走 JS click
                clicked = bool(
                    page.evaluate(
                        """() => {
                            const sels = ['button[aria-label*="发送"]', 'button[aria-label*="Send"]',
                                          'button[data-testid="send-button"]'];
                            for (const s of sels) {
                                const b = document.querySelector(s);
                                if (b) { b.click(); return true; }
                            }
                            return false;
                        }"""
                    )
                )
                if not clicked:
                    logger.warning("JS 找到了发送按钮但点击未生效；为避免抢用户鼠标，本轮不使用 page.mouse.click")
            if attempts <= 2 or attempts % 5 == 0:
                logger.info("尝试发送（第 %d 次）%s", attempts, f"｜{what}" if what else "")
            page.wait_for_timeout(1600)
            continue
        if time.time() >= deadline:
            logger.warning(
                "发送失败：等了 %.0f 秒还没发出去（发送按钮%s，附件 %d 个）%s",
                time.time() - started,
                "找不到" if btn is None else "仍禁用",
                _attachment_count(page),
                f"｜{what}" if what else "",
            )
            return False
        waited = time.time() - started
        if waited - last_note >= 5.0:
            last_note = waited
            logger.info("还不能发送：发送按钮%s，已等 %.0f 秒%s",
                        "找不到（输入框为空时新版没有这个按钮）" if btn is None else "仍禁用",
                        waited, f"｜{what}" if what else "")
        page.wait_for_timeout(500)


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
    timeout: float | None = None,
    poll: float = 3.0,
    new_chat: bool = True,
    prompt_file: str | Path | None = None,
    instruction: str | None = None,
) -> Path | None:
    """静默生成：连 CDP → 新对话 → 等空闲 → 清空输入框 → 附图 → 发送 → 等新图 → 落盘。

    取图**两条路一起等**：`~/.codex/generated_images/**` 里的新文件（新版外壳）与页面上的
    `blob:` 大图（老外壳，canvas 导出），先出现先用。挂图同样两条路：`input[type=file]`
    （老外壳）与 **CDP 真实拖放**（新版外壳没有 file input，见模块 docstring）。

    `prompt_file`：提示词文件（.txt）。给了它就走**附件模式**（默认，见 `prompt_as_file_enabled`）——
    输入框**一个字都不打**（用户 2026-09-16：「你不是已经又提示词文件了吗 问什么输入框还要
    重新输入一遍」），长提示词只作为附件进去；只有 `H3_CHATGPT_PROMPT_SAY=1` 才补一句短说明。
    附件万一挂不上，把提示词作为**消息正文**发出（**不**退回「粘进输入框」）。
    `H3_CHATGPT_PROMPT_AS_FILE=0` 时忽略 `prompt_file`，回旧的粘贴模式。

    `timeout`：等出图的上限（秒）。默认 `H3_CHATGPT_IMAGE_TIMEOUT` / 900 秒
    （用户 2026-09-17：「等待生成图片的时间可以久一点无所谓」）。

    返回落盘路径；超时/取图失败返回 None。调用方负责单链路。
    """
    from playwright.sync_api import sync_playwright

    ep = ensure_instance()
    save_to = Path(save_to)
    save_to.parent.mkdir(parents=True, exist_ok=True)
    if timeout is None or timeout <= 0:
        timeout = _env_float("H3_CHATGPT_IMAGE_TIMEOUT", DEFAULT_IMAGE_TIMEOUT)

    # 默认：提示词直接当**消息正文**发出去（用户 2026-09-16：「不要上传文本了」）。
    # 只有显式 `H3_CHATGPT_PROMPT_AS_FILE=1` 才走 .txt 附件那条路。
    as_file = bool(prompt_file) and prompt_as_file_enabled()
    typed_prompt = prompt
    if as_file:
        typed_prompt = ""
        if str(instruction or "").strip():
            typed_prompt = str(instruction)
        elif prompt_say_enabled():
            name = Path(str(prompt_file)).name
            # 附件名可能是中文（实测中文名在草稿芯片里正常显示）；万一被转义，
            # 短说明里就别复述这个名字，否则模型会去找一个不存在的文件名。
            readable = name if re.search(r"[\u4e00-\u9fff]", name) else ""
            typed_prompt = default_prompt_instruction(readable, has_images=bool(images))

    pw = sync_playwright().start()
    browser = None
    try:
        browser = pw.chromium.connect_over_cdp(ep)
        page = _page(browser)
        if not ensure_chat_mode(page):
            logger.warning("无法进入聊天模式，本次生成停止：%s", save_to.name)
            return None
        if wait_for_composer(page, 20.0) is None:
            logger.warning("聊天模式已切换，但输入框 20 秒内未就绪：%s", save_to.name)
            return None

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
            if wait_for_composer(page, 20.0) is None:
                logger.warning("新对话后输入框没有重新就绪：%s", save_to.name)
                return None

        box = page.locator('div.ProseMirror[role="textbox"], [contenteditable="true"][role="textbox"]').first
        # **先等上一轮回复结束**：还在生成时发下一条，内容会卡在输入框里发不出去
        wait_until_idle(page, what=save_to.name)
        # 生成前**彻底清空**（文字 + 附件），否则残留会叠加进这次请求
        _clear_composer(page, box)

        # 附图：老版本走 file input，新版外壳没有 input → 自动改走 CDP 拖放
        attached: set[str] = set()
        if images:
            mode = attach_images(page, images, what=save_to.name)
            if not mode:
                logger.warning("参考图没能挂上（file input 与拖放都失败）：%s", save_to.name)
            # **等上传真的完成**（发送按钮从 disabled 变回可点）再往下走。上传中就发送，
            # 消息根本发不出去，接着还会白等十分钟生成图超时（用户 2026-09-16 实测）。
            if not wait_for_attachments(page, len(images), what=save_to.name):
                logger.warning("附件上传超过等待上限，仍然尝试发送一次：%s", save_to.name)
            # **等上传完之后**再记录基线：否则贴进去的参考图会被当成「新生成的图」取回来
            # （实测踩过：取回来的其实是图二本人，因为 canvas 重编码后字节数不同所以没被发现）。
            attached = set(_list_blobs(page))

        # 提示词走附件（用户 2026-09-16：长提示词不进输入框，避免风控）
        if as_file:
            if attach_prompt_file(page, str(prompt_file)):
                logger.info("提示词已作为附件挂上：%s", Path(str(prompt_file)).name)
            else:
                # **绝不退回「把长文粘进输入框」**：那正是用户 2026-09-16 看到的
                # 「输入框你怎么还是输入了文本」。挂不上就把提示词作为**消息正文**发出
                # （发出去的长文和留在输入框里的长文是两回事，前者本来就是要发的请求）。
                logger.warning("提示词附件没挂上，改为把提示词作为消息正文发出")
                typed_prompt = prompt

        before = set(_list_blobs(page))
        # 新版外壳的出图落盘目录快照：发送之后新出现的文件就是这次的结果
        disk_before = snapshot_generated_images()

        # 输入：默认把提示词作为**消息正文**发出去；附件模式下这里通常是空的
        if typed_prompt.strip():
            logger.info("开始写入提示词（%d 字符）｜%s", len(typed_prompt), save_to.name)
            landed = ""
            for attempt in range(3):
                try:
                    ok = page.evaluate(
                        """(text) => {
                            const el = document.querySelector('div.ProseMirror[role="textbox"], [contenteditable="true"][role="textbox"]');
                            if (!el) return false;
                            el.focus({preventScroll:true});
                            try {
                                document.execCommand('selectAll', false, null);
                                document.execCommand('delete', false, null);
                                const inserted = document.execCommand('insertText', false, text);
                                if (inserted) return true;
                            } catch (e) {}
                            el.replaceChildren(document.createTextNode(text));
                            el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text}));
                            return true;
                        }""",
                        typed_prompt,
                    )
                    if not ok:
                        logger.warning("DOM 写入提示词未找到输入框（第 %d 次）", attempt + 1)
                except Exception as exc:
                    logger.warning("DOM 写入提示词失败（第 %d 次）：%s", attempt + 1, exc)
                page.wait_for_timeout(500)
                try:
                    landed = box.inner_text().strip()
                except Exception:
                    landed = ""
                if landed:
                    logger.info("提示词已写入输入框（读取到 %d 字符）｜%s", len(landed), save_to.name)
                    break
                logger.warning("正文没进输入框（第 %d 次），重新写入", attempt + 1)
            if not landed:
                logger.warning("提示词没有写入输入框，禁止发送：%s", save_to.name)
                return None
        elif not as_file:
            logger.warning("提示词为空，禁止发送：%s", save_to.name)
            return None

        if _send(page, box, what=save_to.name):
            logger.info(
                "已发送（%s｜%s）",
                save_to.name,
                "附件模式" if as_file else "提示词正文模式",
            )
        else:
            # **消息没发出去就不要再等图了**：等下去只会白等满 timeout（默认 15 分钟），
            # 用户看到的就是「卡在那里不动」。直接返回 None，让调用方报一次明确的失败。
            logger.warning("发送未生效：输入框里还有正文/附件，本次不再等图（%s）", save_to.name)
            return None

        sent_at = time.time()
        # 等这次生成的图：**落盘目录与页面 blob 两条路一起等**（新外壳只有前者）
        deadline = time.time() + timeout
        data: bytes | None = None
        last_note = 0.0
        while time.time() < deadline:
            time.sleep(poll)
            # ① 新版外壳：出图落在 ~/.codex/generated_images/<线程>/ 里
            for cand in new_generated_images(disk_before, sent_at):
                if not _stable_file(cand):
                    continue
                try:
                    blob = Path(cand).read_bytes()
                except OSError:
                    continue
                if _resembles_input(blob, images):
                    logger.info("跳过一张与参考图一致的候选（%s，应是参考图副本）", Path(cand).name)
                    disk_before.add(str(cand))
                    continue
                logger.info("从落盘目录取到生成图：%s", cand)
                data = blob
                break
            if data:
                break
            # ② 老外壳：页面上的 blob 大图 → canvas 导出
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
            # 心跳：出图要一两分钟，长时间没输出会被误认为卡死
            waited = timeout - (deadline - time.time())
            if waited - last_note >= WAIT_HEARTBEAT_SECONDS:
                last_note = waited
                logger.info("等生成图……已等 %.0f 秒（%s）", waited, save_to.name)
        if not data:
            logger.warning("等待生成图超时（%s，已等 %.0f 秒）", save_to.name, timeout)
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
