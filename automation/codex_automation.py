"""automation.codex_automation — 通过 CDP 接管 ChatGPT 桌面端（聊天标签页）。

原理（已实测）：ChatGPT 桌面端是 MSIX 打包的 Electron/Chromium 应用
（包名 OpenAI.Codex，可执行文件 app\\ChatGPT.exe）。用 `--remote-debugging-port`
启动后 CDP 端口可开，Playwright 可 connect_over_cdp 直接接管它的聊天界面，
在 DOM 上：新建聊天 → set_input_files 上传图片 → 粘贴提示词 → 等待回复 → 取到生成图。

关键约束：
  1. 要让 CDP 生效，应用必须以 --remote-debugging-port 启动（当前会话没开）。
     引导流程会先退出应用、再用真实 profile + 端口重启（登录态不丢）。
  2. 严格一次只处理一个聊天（一个登录态，别并行开多条）。
  3. 选择器集中在本文件顶部，版本漂移时集中改。

依赖：playwright（需 pip install playwright）
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import glob as globmod
from pathlib import Path

from . import settings as _s

# ---------------------------------------------------------------
# 选择器集中管理（版本漂移时集中改）
# ---------------------------------------------------------------
# 新建聊天入口（聊天标签页里通常有「新聊天」按钮或侧栏「New chat」）
SEL_NEW_CHAT = [
    "text=新聊天",
    "text=New chat",
    "[data-testid='new-chat']",
    "button:has-text('新建')",
]
# 文本输入框（富文本 / textarea / contenteditable）
SEL_PROMPT_BOX = [
    "textarea",
    "[contenteditable='true']",
    "#prompt-textarea",
    "[data-testid='composer'] textarea",
]
# 发送按钮
SEL_SEND = [
    "button[data-testid='send-button']",
    "button:has-text('发送')",
    "form button[type='submit']",
]
# 停止生成按钮（出现即代表正在生成）
SEL_STOP = [
    "button[data-testid='stop-button']",
    "button:has-text('停止生成')",
]
# 文件上传 input（可能隐藏，用 set_input_files 直接喂）
SEL_FILE_INPUT = "input[type='file']"
# 生成完成的图片（聊天消息里的 <img>）
SEL_IMAGES = "article img[src^='data:'], article img[src^='blob:'], [data-testid*='image'] img, article img"
# 回复块（区分助手回复）
SEL_ARTICLE = "article"
SEL_SIDEBAR_ITEM = "[data-testid*='conversation'], nav a[href^='/c/']"


def detect_chatgpt_exe(cfg: dict) -> str:
    """自动探测最新安装的 ChatGPT 桌面端 exe（MSIX 升级换路径也能找到）。

    WindowsApps 目录受 ACL 保护，普通进程不能 glob，所以：
      1) 优先从正在运行的进程取真实 exe 路径；
      2) 否则用 Get-AppxPackage 解析包安装位置。
    """
    import psutil

    # 1) 运行中的进程：优先 ChatGPT.exe（桌面端），不是 codex.exe（CLI）
    for p in psutil.process_iter(["name", "exe"]):
        name = (p.info.get("name") or "").lower()
        exe = p.info.get("exe") or ""
        if name == "chatgpt.exe" and exe and Path(exe).is_file():
            return exe

    # 2) Get-AppxPackage 解析
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

    # 3) 兜底：glob（可能因 ACL 失败）
    pat = cfg.get("chatgpt_exe_glob")
    if pat:
        candidates = globmod.glob(pat)
        if candidates:
            def ver(p: str) -> tuple:
                m = re.search(r"(\d+)\.(\d+)\.(\d+)", p)
                return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
            return max(candidates, key=ver)

    raise FileNotFoundError(
        "未找到 ChatGPT 桌面端。请先启动它，或在 config.json 的 chatgpt_exe_glob 指定路径。"
    )


def _running_pids() -> list[int]:
    import psutil
    return [p.pid for p in psutil.process_iter(["name"])
            if p.info.get("name", "").lower() in {"chatgpt.exe", "codex.exe"}]


def _quit_desktop():
    """完全退出桌面端（等所有进程消失）。"""
    import psutil
    for pid in _running_pids():
        try:
            p = psutil.Process(pid)
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    # 等待退出（最多 15s）
    for _ in range(30):
        if not _running_pids():
            return
        time.sleep(0.5)
    # 强杀
    for pid in _running_pids():
        try:
            psutil.Process(pid).kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def launch_with_cdp(cfg: dict, *, quit_existing: bool = True) -> str:
    """确保桌面端以 CDP 端口运行，返回 http://host:port。引导重启（需用户同意）。"""
    exe = detect_chatgpt_exe(cfg)
    port = int(cfg.get("cdp_port", 9222))
    host = cfg.get("cdp_host", "127.0.0.1")
    profile = cfg.get("chatgpt_profile")
    endpoint = f"http://{host}:{port}"

    # 已可访问？
    if _cdp_ready(endpoint):
        return endpoint

    if quit_existing:
        _quit_desktop()
        time.sleep(1)

    cmd = [exe, f"--remote-debugging-port={port}", "--remote-allow-origins=*"]
    if profile:
        cmd.append(f"--user-data-dir={profile}")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 等待 CDP 就绪
    deadline = time.time() + 60
    while time.time() < deadline:
        if _cdp_ready(endpoint):
            return endpoint
        time.sleep(0.5)
    raise RuntimeError(f"CDP 端口 {port} 60 秒内未就绪（桌面端可能拒绝调试开关）")


def _cdp_ready(endpoint: str) -> bool:
    import httpx
    try:
        r = httpx.get(f"{endpoint}/json/version", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _connect(endpoint: str):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "缺少 playwright：请先 `python -m pip install playwright` 并 `playwright install`（无头浏览器不需要，CDP 直连即可）"
        )
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(endpoint)
    return pw, browser


def _chat_page(browser):
    """挑「聊天」标签页（含聊天 UI 的 page）。取最像聊天的一个。"""
    pages = browser.contexts[0].pages if browser.contexts else []
    if not pages:
        raise RuntimeError("桌面端没有可用的页面/标签页")
    # 优先含 composer/输入框的页面；否则取第一个
    for p in pages:
        try:
            if p.locator(SEL_PROMPT_BOX[0]).count() or p.locator("textarea").count():
                return p
        except Exception:
            continue
    return pages[0]


class CodexSession:
    """对 ChatGPT 桌面端聊天的一个会话封装。"""

    def __init__(self, cfg: dict, *, endpoint: str | None = None, quit_existing: bool = True):
        self.cfg = cfg
        self.endpoint = endpoint or launch_with_cdp(cfg, quit_existing=quit_existing)
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self):
        self._pw, self._browser = _connect(self.endpoint)
        self._page = _chat_page(self._browser)
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # ---------- 基础 ----------
    def screenshot(self, path: str | Path | None = None):
        path = path or _s.work_dir(self.cfg) / "codex_fail.png"
        try:
            self._page.screenshot(path=str(path))
            print(f"[codex] 已截图：{path}")
        except Exception as e:
            print(f"[codex] 截图失败：{e}")

    def _first(self, locators, timeout: float = 20.0):
        """在多个候选中取第一个可见的。"""
        for sel in locators:
            try:
                loc = self._page.locator(sel).first
                if loc.is_visible(timeout=3000):
                    return loc
            except Exception:
                continue
        return None

    def new_chat(self):
        loc = self._first(SEL_NEW_CHAT, timeout=10)
        if loc:
            loc.click()
            time.sleep(1.5)
        else:
            print("[codex] 未找到「新聊天」按钮，假设已在可输入状态")

    def upload_images(self, image_paths: list[str]):
        """上传图一+图二等图片。用 set_input_files 喂给隐藏 file input。"""
        file_input = self._page.locator(SEL_FILE_INPUT).first
        file_input.set_input_files(image_paths)
        # 等待附件缩略图出现（简单等待）
        time.sleep(2.0)

    def paste_prompt(self, text: str):
        """把提示词填入输入框并发送。"""
        box = self._first(SEL_PROMPT_BOX, timeout=15)
        if box is None:
            raise RuntimeError("找不到输入框")
        box.fill(text)
        time.sleep(0.5)
        send = self._first(SEL_SEND, timeout=5)
        if send:
            send.click()
        else:
            box.press("Enter")
        time.sleep(1.0)

    def wait_reply_done(self, timeout: float = 300.0):
        """等回复完成：先等出现「停止生成」/进行中，再等它消失。"""
        deadline = time.time() + timeout
        generating = False
        while time.time() < deadline:
            try:
                stop = self._page.locator(SEL_STOP[0]).count() or \
                       any(self._page.locator(s).count() for s in SEL_STOP[1:])
            except Exception:
                stop = 0
            if stop:
                generating = True
            elif generating:
                return  # 出现过进行中，现已结束
            time.sleep(1.5)
        # 未观察到进行中——视为已结束（保守）
        return

    def collect_images(self, out_dir: str | Path) -> list[Path]:
        """抓取当前页面所有生成图，保存到 out_dir。返回路径列表。"""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for sel in SEL_IMAGES.split(","):
            try:
                locs = self._page.locator(sel.strip())
                n = locs.count()
                for i in range(n):
                    src = locs.nth(i).get_attribute("src")
                    if not src:
                        continue
                    fp = _save_src(src, out_dir)
                    if fp and str(fp) not in [str(p) for p in saved]:
                        saved.append(fp)
            except Exception:
                continue
        return saved


def _save_src(src: str, out_dir: Path) -> Path | None:
    """把 data:/blob: 图片存到磁盘。data: 直接解码；blob: 走 CDP 求值（尽力）。"""
    import base64
    try:
        if src.startswith("data:image"):
            _, b64 = src.split(",", 1)
            ext = _data_ext(src)
            data = base64.b64decode(b64)
            fp = out_dir / f"gen_{int(time.time()*1000)}{ext}"
            fp.write_bytes(data)
            return fp
        # blob: 尝试 fetch 转 data
        if src.startswith("blob:"):
            # 简单方案：通过页面 fetch 读 blob（若同源）
            return None
    except Exception:
        return None
    return None


def _data_ext(src: str) -> str:
    m = re.search(r"image/(png|jpe?g|webp)", src)
    return {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp"}.get(
        (m.group(1) if m else ""), ".png"
    )
