#!/usr/bin/env python3
"""聊天模式出图（走 ChatGPT 订阅，不碰 Codex/Work 额度）—— 人物图 → B站 → 抖音。

为什么是聊天模式
----------------
`codex exec` 那种做法烧的是 **Codex 额度**（实测已被用尽，报错指向 /codex/settings/usage）。
聊天模式走的是 **ChatGPT 订阅额度**，两条是不同的桶。本脚本只操作聊天侧。

怎么做到"静默"
--------------
连桌面端**专用实例**（独立 profile：`~/.codex-automation/profile`，调试端口 9444）的 CDP，
直接操作真实 DOM：不抢前台、不动鼠标、窗口可以最小化。专用 profile 的登录态来自
`~/.codex/auth.json`，不需要重新登录，也**不会影响用户自己开着的那个窗口**。

三条判定（正好对应"上传成功 / 有没有点对发送 / 生成成功"）
----------------------------------------------------------
  上传成功 = 附件芯片出现，且发送按钮从 disabled 恢复可点（上传期间它是 disabled 的）
  发送成功 = 输入框正文与附件**都空了**（不拿"停止生成"按钮当信号——它上一轮结束还会留着）
  生成成功 = 页面上出现尺寸不等于任何参考图的 `data:image/png;base64` 大图
             （**2026-09-19 实测**：聊天模式的出图既不是 `blob:`，也不进
              `~/.codex/generated_images`；那条磁盘路是 Codex 侧出图才走的。
              所以页面 `data:` → `app://fs/@fs/` 本地路径 → `blob:` canvas 导出 →
              磁盘新文件，四路按序兜底；参考图一律按已知尺寸排除）

固定顺序（严格串行，绝不并发、绝不跳步）
----------------------------------------
  ① 人物图：新对话 + [抽帧.jpg(图一), 晚柠.png(图二)] + 提示词 .txt 作为**文本附件**
     （4比3图片.txt 唱歌 / 9比16图片.txt 跳舞）→ 人物图.png
     ✗ 没落盘 → 当前作品立即停止，不进入封面
  ② B站封面：新对话 + [人物图.png]，提示词 = 标题+简介的自然融入（4:3）→ B站4x3.png
  ③ 抖音封面：**继续②的同一个对话**，不再重传图，提示词同款（3:4）→ 抖音3x4.png

用法
----
  python scripts/chat_imagegen.py --list
  python scripts/chat_imagegen.py --dir 001_唱歌_... --steps person
  python scripts/chat_imagegen.py --dir 001_唱歌_... --steps person,bili,douyin
  python scripts/chat_imagegen.py --all --steps person,bili,douyin

按需模式（常驻小循环，2026-09-19 用户指定的流程）
------------------------------------------------
  python scripts/chat_imagegen.py --need-one --loop 300

「当批量任务中没有可以运行的任务、全部都是需要人物图的时候再去跑」，而且
「出图也是只需要运行的时候的那条出图」——所以每一轮只做三件事：
  ① 问后端 `GET /api/batches/latest`：批量自己还有事干（待备料/已确认/出片中）就**什么都不做**；
     后端读不出来也**什么都不做**（除非 --need-assume-idle）——避免后端一停就把缺的图全刷出来；
  ② 批量是空档时，**只挑批量里真实存在的、还缺图的那一条**（按编号最靠前，已删除/已跳过不碰）；
  ③ 人物图落盘后**先等后端把发布文案按新图重写完，再出封面**（封面大字取自发布文案的标题）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image  # noqa: E402

from scripts.cover_common import (  # noqa: E402
    MIN_VALID_BYTES,
    PUBLISH_ROOT,
    SPECS,
    build_prompt,
    existing_cover,
    parse_copy,
    publish_folders,
)

PORT = int(os.getenv("H3_CHATGPT_CDP_PORT", "9444"))
ENDPOINT = f"http://127.0.0.1:{PORT}"
PROFILE = Path(os.getenv("H3_CHATGPT_AUTOMATION_PROFILE") or (Path.home() / ".codex-automation" / "profile"))
GENERATED_DIR = Path(os.getenv("CODEX_HOME") or (Path.home() / ".codex")) / "generated_images"
# 糊脸后的「图一」缓存（每个作品一个子目录，避免都叫 抽帧-noface.png 互相覆盖）
GUARD_DIR = PROJECT_ROOT / "data" / "identity_guard"

IDENTITY = "晚柠.png"
SCENE = "抽帧.jpg"
COPY = "发布文案.txt"
PERSON = "人物图.png"
SING_RULE = "4比3图片.txt"
DANCE_RULE = "9比16图片.txt"

# 身份原型 / 规则文件按候选顺序找，**不再写死发布根**（2026-09-22：发布成品根上的
# `晚柠.png` 与用户桌面那两份提示词已删除，保留仓库自带的那一份）。
# 顺序：桌面（用户手改的那份）→ 发布根（老位置，还能找到就用）→ 仓库里的规范位置。
IDENTITY_CANDIDATES = (
    Path(r"E:\AI_Assets\PortraitIdentity\本人固定参考.png"),   # 与 backend/batch_worker.IDENTITY_PATH 同一个文件
    PUBLISH_ROOT / IDENTITY,
)
RULE_CANDIDATES = {
    "singing": (Path.home() / "Desktop" / SING_RULE, PUBLISH_ROOT / SING_RULE,
                PROJECT_ROOT / "backend" / "prompts" / "4x3唱歌构图.txt"),
    "dance": (Path.home() / "Desktop" / DANCE_RULE, PUBLISH_ROOT / DANCE_RULE,
              PROJECT_ROOT / "backend" / "prompts" / "9x16跳舞构图.txt"),
}


def first_existing(paths) -> Path:
    """返回第一个存在的候选；都不存在就返回最后一个（让报错指着一个明确的位置）。"""
    options = tuple(Path(p) for p in paths)
    for path in options:
        if path.is_file():
            return path
    return options[-1]


def identity_path() -> Path:
    return first_existing(IDENTITY_CANDIDATES)


def rule_path(kind: str) -> Path:
    return first_existing(RULE_CANDIDATES.get(kind) or RULE_CANDIDATES["dance"])

SING_RATIO, DANCE_RATIO, BILI_RATIO, DOUYIN_RATIO = 4 / 3, 9 / 16, 4 / 3, 3 / 4
RATIO_TOLERANCE = 0.06
STEP_ORDER = ("person", "bili", "douyin")

UPLOAD_TIMEOUT = float(os.getenv("H3_CHATGPT_UPLOAD_TIMEOUT", "600"))
SEND_TIMEOUT = float(os.getenv("H3_CHATGPT_SEND_TIMEOUT", "300"))
IMAGE_TIMEOUT = float(os.getenv("H3_CHATGPT_IMAGE_TIMEOUT", "900"))
# 三个等待上限**保持原有取值**（用户 2026-09-19：「这个你原来的就可以」）：
#   UPLOAD_TIMEOUT 600s / SEND_TIMEOUT 300s / IMAGE_TIMEOUT 900s（都可用 H3_CHATGPT_* 覆盖）
#
# 下面两个"静止判据"是用来避免**永远等下去**的兜底，必须**放得很长**：
# 用户 2026-09-19 指出「她有生产图片 只是我们等待时间不够」——页面显示
# 「Worked for 26s」/没有停止按钮、没有百分比，**并不代表出图已经结束**，
# 图可能几十秒甚至几分钟后才出现。早先设成 120s/180s 会把还在路上的图判成"没出图"，
# 于是白催一次、甚至误判失败。现在放到 600s，配合 900s 的总上限使用。
IDLE_GRACE = float(os.getenv("H3_CHATGPT_IDLE_GRACE", "600"))
NO_ACTIVITY_TIMEOUT = float(os.getenv("H3_CHATGPT_NO_ACTIVITY_TIMEOUT", "600"))
# 遇到限流（页面提示「请求过多 / Too many requests」）时，最多额外等这么久（默认 30 分钟），
# 每次检测到就给等待截止时间 +60 秒，直到用完这个预算
RATE_LIMIT_BUDGET = float(os.getenv("H3_CHATGPT_RATE_LIMIT_BUDGET", "1800"))

EXIT_OK, EXIT_GATE1, EXIT_GATE2, EXIT_GATE3 = 0, 1, 2, 3


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------
# 专用实例 / 连接
# --------------------------------------------------------------------------

def find_exe() -> str:
    """找桌面端 exe：跑着的进程 → Get-AppxPackage → glob。

    **实测坑（2026-09-19）**：桌面端自动更新过版本（26.915.3509.0 → 26.915.4065.0），
    旧路径直接消失；而 `C:\\Program Files\\WindowsApps` **普通权限列不出目录**，
    所以 `glob` 那条永远拿不到东西 —— 必须靠 `Get-AppxPackage`（不需要管理员）。
    """
    import glob

    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process ChatGPT -ErrorAction SilentlyContinue | Select-Object -First 1).Path"],
        capture_output=True, text=True, timeout=30)
    path = (out.stdout or "").strip()
    if path and Path(path).is_file():
        return path

    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-AppxPackage OpenAI.Codex | Select-Object -First 1).InstallLocation"],
        capture_output=True, text=True, timeout=60)
    location = (out.stdout or "").strip()
    if location:
        cand = Path(location) / "app" / "ChatGPT.exe"
        if cand.is_file():
            return str(cand)

    cands = sorted(glob.glob(r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\ChatGPT.exe"))
    if cands:
        return cands[-1]
    raise FileNotFoundError("找不到 ChatGPT 桌面端 exe（进程/Get-AppxPackage/glob 三条路都失败）")


def cdp_ready() -> bool:
    import httpx
    try:
        return httpx.get(f"{ENDPOINT}/json/version", timeout=2).status_code == 200
    except Exception:
        return False


def ensure_instance(timeout: float = 120.0) -> bool:
    if cdp_ready():
        log(f"专用实例已在（端口 {PORT}）")
        return True
    exe = find_exe()
    PROFILE.mkdir(parents=True, exist_ok=True)
    log(f"起专用实例：{exe} ｜ profile={PROFILE} ｜ 端口={PORT}")
    subprocess.Popen([exe, f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
                      f"--user-data-dir={PROFILE}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp_ready():
            time.sleep(3.0)
            return True
        time.sleep(1.0)
    log(f"✗ 端口 {PORT} 在 {timeout:.0f}s 内未就绪")
    return False


# --------------------------------------------------------------------------
# 页面上的 DOM 探针
# --------------------------------------------------------------------------

JS_COMPOSER = 'div.ProseMirror[role="textbox"], [contenteditable="true"][role="textbox"]'

JS_STATE = r"""
() => {
  const out = {};
  const pm = document.querySelector('div.ProseMirror[role="textbox"], [contenteditable="true"][role="textbox"]');
  out.composer = !!pm;
  out.text = pm ? (pm.innerText || '').trim() : null;
  out.chips = Array.from(document.querySelectorAll('[aria-label^="移除"], [aria-label^="Remove"]'))
      .map(e => (e.getAttribute('aria-label') || '').replace(/^(移除|Remove)\s*[“"]?/, '').replace(/[”"]$/, ''));
  const sels = ['button[aria-label*="发送"]', 'button[aria-label*="Send"]', 'button[data-testid="send-button"]'];
  let send = null;
  for (const s of sels) { const h = document.querySelector(s); if (h) { send = h; break; } }
  out.sendExists = !!send;
  out.sendDisabled = send ? (send.hasAttribute('disabled') || send.getAttribute('aria-disabled') === 'true') : null;
  out.stopExists = !!Array.from(document.querySelectorAll('button'))
      .find(b => /停止生成|Stop generating|停止响应/i.test((b.getAttribute('aria-label') || '') + (b.innerText || '')));
  return out;
}
"""

JS_NEW_CHAT = r"""
() => {
  const target = /^(新聊天|新对话|新的聊天|开始新聊天|New chat)$/i;
  const els = Array.from(document.querySelectorAll('button, a, [role="button"]'));
  const hit = els.find(e => target.test(((e.innerText || '').trim()) || (e.getAttribute('aria-label') || '').trim()));
  if (!hit) return false;
  hit.click();
  return true;
}
"""

JS_CLICK_SEND = r"""
() => {
  const sels = ['button[aria-label*="发送"]', 'button[aria-label*="Send"]', 'button[data-testid="send-button"]'];
  let b = null;
  for (const s of sels) { const h = document.querySelector(s); if (h) { b = h; break; } }
  if (!b) return 'no-button';
  if (b.hasAttribute('disabled') || b.getAttribute('aria-disabled') === 'true') return 'disabled';
  b.click();
  return 'clicked';
}
"""

JS_LIST_IMAGES = r"""
() => Array.from(document.querySelectorAll('img'))
  .filter(e => e.naturalWidth > 300)
  .map(e => ({src: e.getAttribute('src') || '', w: e.naturalWidth, h: e.naturalHeight}))
"""

JS_PROGRESS = r"""
() => {
  const t = (document.body ? document.body.innerText : '') || '';
  const m = t.match(/\d{1,3}\s*%/g);
  return m ? m.slice(-2).join(' ') : '';
}
"""

JS_CANVAS_EXPORT = r"""
(src) => new Promise((resolve, reject) => {
  const img = Array.from(document.querySelectorAll('img')).find(e => e.getAttribute('src') === src);
  if (!img) { reject(new Error('img not found')); return; }
  const c = document.createElement('canvas');
  c.width = img.naturalWidth || img.width;
  c.height = img.naturalHeight || img.height;
  c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
  resolve(c.toDataURL('image/png'));
})
"""


def state(page) -> dict:
    return page.evaluate(JS_STATE)


def pick_page(browser):
    pages = browser.contexts[0].pages if browser.contexts else []
    for pg in pages:
        try:
            url = pg.url or ""
            if "detached" in url or "avatar-overlay" in url or "index.html" not in url:
                continue
            if pg.locator("div.ProseMirror").count():
                return pg
        except Exception:
            continue
    for pg in pages:
        try:
            if pg.locator("div.ProseMirror").count():
                return pg
        except Exception:
            continue
    raise RuntimeError("没有可用的聊天页")


def wait_composer(page, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = page.evaluate(
                """() => { const el = document.querySelector('div.ProseMirror[role="textbox"], [contenteditable="true"][role="textbox"]');
                   if (!el) return null; const r = el.getBoundingClientRect();
                   return {w: r.width, h: r.height}; }""")
            if info and info.get("w", 0) > 50:
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    return False


def open_new_chat(page) -> bool:
    """点「新聊天/新对话」并等输入框就绪、附件区清空。"""
    try:
        clicked = page.evaluate(JS_NEW_CHAT)
    except Exception:
        clicked = False
    if not clicked:
        log("⚠ 没找到「新聊天」按钮，沿用当前对话")
    page.wait_for_timeout(1200)
    wait_composer(page, 20)
    deadline = time.time() + 20
    while time.time() < deadline:
        st = state(page)
        if not st["chips"] and not (st["text"] or "").strip():
            return True
        page.wait_for_timeout(500)
    log("⚠ 新对话后输入区仍非空")
    return False


# --------------------------------------------------------------------------
# 附件 / 上传 / 发送
# --------------------------------------------------------------------------

def attach_files(page, images: list[Path], prompt_file: Path | None) -> int:
    """挂附件：图片走 `accept="image/*"` 的口，提示词 .txt 走没有 accept 的那个口。"""
    attached = 0
    if images:
        locator = page.locator('input[type="file"][accept="image/*"]')
        if not locator.count():
            locator = page.locator('input[type="file"]').first
        locator.first.set_input_files([str(p) for p in images])
        attached += len(images)
        log(f"  已提交图片附件：{[p.name for p in images]}")
    if prompt_file is not None:
        generic = page.locator('input[type="file"]:not([accept])')
        if generic.count():
            generic.nth(generic.count() - 1).set_input_files(str(prompt_file))
            log(f"  已提交提示词附件：{prompt_file.name}")
        else:
            log("  ✗ 没有通用附件口，提示词无法作为 .txt 附件挂上")
            return -1
    return attached


def wait_attachments(page, expect: int, timeout: float = UPLOAD_TIMEOUT) -> bool:
    """等附件芯片出现**且**上传完成（发送按钮从 disabled 恢复）。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        st = state(page)
        chips = len(st["chips"])
        ready = bool(st["sendExists"]) and st["sendDisabled"] is False
        note = f"芯片 {chips}/{expect} ｜ 发送按钮 exists={st['sendExists']} disabled={st['sendDisabled']}"
        if note != last:
            log("  上传等待：" + note)
            last = note
        if chips >= expect and ready:
            log(f"  ✓ 上传完成（{chips} 个附件，发送按钮已可点）")
            return True
        page.wait_for_timeout(1000)
    log(f"  ✗ 上传等待超时（{timeout:.0f}s）")
    return False


def send_now(page, timeout: float = SEND_TIMEOUT) -> bool:
    """点发送，并且**只认**"输入框正文与附件都空"作为发送成功。"""
    deadline = time.time() + timeout
    attempt = 0
    last = ""
    while time.time() < deadline:
        attempt += 1
        result = page.evaluate(JS_CLICK_SEND)
        if result != last:
            log(f"  发送尝试 #{attempt} → {result}")
            last = result
        if result == "clicked":
            page.wait_for_timeout(1500)
        st = state(page)
        if not st["chips"] and not (st["text"] or "").strip():
            log("  ✓ 发送成功（输入框正文与附件都已清空）")
            return True
        page.wait_for_timeout(2000)
    log(f"  ✗ 发送未确认（输入框仍有内容 / 附件未清空，等待了 {timeout:.0f}s）")
    return False


# --------------------------------------------------------------------------
# 生成结果
# --------------------------------------------------------------------------

def snapshot_generated() -> dict[str, float]:
    if not GENERATED_DIR.is_dir():
        return {}
    return {str(p): p.stat().st_mtime for p in GENERATED_DIR.rglob("*.png")}


def _write_data_url(src: str, scratch: Path) -> Path:
    import base64
    data = base64.b64decode(src.split(",", 1)[1])
    out = scratch / f"chatgen_{int(time.time() * 1000)}.png"
    out.write_bytes(data)
    return out


def _path_from_fs_src(src: str) -> Path | None:
    if "/@fs/" not in src:
        return None
    raw = src.split("/@fs/", 1)[1].split("?")[0].replace("\\", "/")
    if re.match(r"^/[A-Za-z]:", raw):
        raw = raw[1:]
    return Path(raw)


JS_ACTIVITY = r"""
() => {
  const stop = Array.from(document.querySelectorAll('button'))
    .some(b => /停止生成|Stop generating|停止响应/i.test((b.getAttribute('aria-label') || '') + (b.innerText || '')));
  const body = document.body ? document.body.innerText : '';
  return {stop: stop, percent: /\d{1,3}\s*%/.test(body), worked: /Worked for|已工作/i.test(body)};
}
"""

# 这一轮结束却没出图时，用来催它真的去调出图工具（实测 011 出现过"26 秒结束、什么都没有"）
NUDGE_TEXT = (
    "这一轮没有生成图片。请**现在直接调用图片生成工具**把这张图生成出来，"
    "规格与上面完全相同（人物图：图二的脸 + 图一给图一的造型/场景/灯光；画幅按上面写死的比例）。"
    "不要只回复文字，不要解释，不要提问，不要再次确认。"
)


JS_RATE_LIMITED = r"""
() => {
  const t = (document.body ? document.body.innerText : '') || '';
  return t.includes('请求过多') || t.includes('Too many requests')
      || t.includes('too many requests') || t.includes('稍后再试');
}
"""


def wait_new_image(page, disk_before: dict, imgs_before: list[dict], ref_sizes: set,
                   sent_at: float, timeout: float | None = None,
                   scratch: Path | None = None, idle_grace: float | None = None) -> Path | None:
    """等生成结果。

    **聊天模式的真实落点（2026-09-19 实测）**：页面上一个
    `<img src="data:image/png;base64,...">` —— 既不是 `blob:`，
    也不会进 `~/.codex/generated_images`（那是 Codex 侧出图才走的路）。
    所以这里按优先级找，并**一律排除参考图**（抽帧/晚柠/人物图）：

      1. `data:image/...` 内联图（实测走的就是这条）
      2. `app://fs/@fs/<本地路径>` —— 能直接读到文件
      3. `blob:` 大图（老外壳）→ canvas 导出（fetch(blob:) 被 CSP 拦）
      4. `generated_images/**` 里新出现的 png（Codex 侧）

    **回合结束检测（2026-09-19 补）**：实测 011 有一轮 26 秒就结束、既没出图也没输出，
    旧实现会死等到 900 秒超时（用户看到的就是"卡死"）。现在：
      - 只要观察到"正在生成"（停止按钮或百分比）后再归于静止，宽限 `idle_grace` 秒仍无新图 → 提前返回 None；
      - 全程 45 秒都没观察到任何活动 → 也返回 None。
    返回 None 时由调用方决定是否催一次重来。
    """
    seen_before = {i["src"] for i in imgs_before}
    scratch = scratch or Path(os.getenv("TEMP", "."))
    timeout = IMAGE_TIMEOUT if timeout is None else timeout
    idle_grace = IDLE_GRACE if idle_grace is None else idle_grace
    deadline = time.time() + timeout
    heartbeat = time.time()
    started_at = time.time()
    seen_activity = False
    idle_since: float | None = None
    no_activity_deadline = started_at + NO_ACTIVITY_TIMEOUT
    rate_seen = False
    rate_extension = 0.0

    while time.time() < deadline:
        # 4) 磁盘（Codex 侧出图才走这条，留作兜底）
        fresh = [Path(p) for p in snapshot_generated()
                 if p not in disk_before and Path(p).stat().st_mtime >= sent_at - 5]
        fresh = [p for p in fresh if p.stat().st_size >= 20 * 1024]
        if fresh:
            pick = max(fresh, key=lambda p: p.stat().st_mtime)
            log(f"  ✓ 磁盘新图：{pick.name}（{pick.stat().st_size // 1024}KB）")
            return pick

        # 1/2/3) 页面
        try:
            items = page.evaluate(JS_LIST_IMAGES)
        except Exception as exc:
            log(f"  读取页面图片失败：{exc}")
            items = []
        for item in items:
            src, w, h = item["src"], item["w"], item["h"]
            if src in seen_before:
                continue
            if src.startswith("data:image"):
                # 聊天模式的生成结果就是 data: 内联图。
                # **故意不按尺寸排除**：封面（1448x1086）与人物图（1448x1086）可能同尺寸，
                # 按尺寸排会把刚生成的封面误当成参考图丢掉（2026-09-19 实测卡死在这）。
                out = _write_data_url(src, scratch)
                if out.stat().st_size >= 20 * 1024:
                    log(f"  ✓ 生成图（data 内联）{w}x{h} {out.stat().st_size // 1024}KB")
                    return out
            elif "/@fs/" in src:
                cand = _path_from_fs_src(src)
                if cand and cand.is_file() and cand.stat().st_size >= 20 * 1024:
                    log(f"  ✓ 生成图（本地文件）{w}x{h} {cand.name}")
                    return cand
            elif src.startswith("blob:"):
                # 老外壳的 blob 结果：参考图也渲染成 blob，所以**只有这条**按尺寸排除参考图
                if (w, h) in ref_sizes:
                    continue
                try:
                    data = page.evaluate(JS_CANVAS_EXPORT, src)
                    if data and data.startswith("data:image"):
                        out = _write_data_url(data, scratch)
                        log(f"  ✓ 生成图（blob 导出）{w}x{h} {out.stat().st_size // 1024}KB")
                        return out
                except Exception as exc:
                    log(f"  blob 导出失败：{exc}")

        # 回合结束检测
        try:
            activity = page.evaluate(JS_ACTIVITY)
        except Exception:
            activity = {"stop": True, "percent": True, "worked": False}
        # 限流检测：ChatGPT 回「请求过多 / Too many requests」时**不能**当成"没出图"——
        # 用户 2026-09-19 实测遇到限流，正确做法是继续等（并把截止时间往后推），
        # 而不是催一次或判失败，否则会白烧一次生成、还可能留下缺口。
        try:
            rate_limited = bool(page.evaluate(JS_RATE_LIMITED))
        except Exception:
            rate_limited = False
        if rate_limited:
            if not rate_seen:
                rate_seen = True
                log("  ⚠ 页面提示「请求过多」（限流）→ 继续等待，不催不判失败")
            if rate_extension < RATE_LIMIT_BUDGET:
                deadline += 60.0
                rate_extension += 60.0
                page.wait_for_timeout(60000)
                continue
        busy = bool(activity.get("stop") or activity.get("percent"))
        if busy:
            seen_activity = True
            idle_since = None
        elif seen_activity:
            idle_since = idle_since or time.time()
            if time.time() - idle_since >= idle_grace:
                log(f"  ⚠ 这一轮已经结束、静止 {idle_grace:.0f}s 也没出现新图 → 判定本轮未出图")
                return None
        elif time.time() > no_activity_deadline:
            log(f"  ⚠ 发出后 {NO_ACTIVITY_TIMEOUT:.0f}s 内没观察到任何生成活动，也没出现新图 → 判定本轮未出图")
            return None

        if time.time() - heartbeat > 30:
            heartbeat = time.time()
            try:
                prog = page.evaluate(JS_PROGRESS)
            except Exception:
                prog = ""
            log(f"  等出图中… 已等 {int(time.time() - started_at)}s"
                + (f" 进度 {prog}" if prog else ""))
        page.wait_for_timeout(3000)

    log(f"  ✗ 等出图超时（{timeout:.0f}s）")
    return None


# 禁止模糊（用户 2026-09-20：「生成的图片有的手会模糊 禁止模糊」）。
# 桌面那份规则文件（`9比16图片.txt` / `4比3图片.txt`）里有【禁止虚化】，但缺**最关键的那一句**：
# 抽帧里手正在动、本身就是糊的，模型会把这层模糊**照抄**进成图（013 实测）。所以这一句补在
# **消息正文**里 —— 正文排在 .txt 附件之后，是模型最后读到的东西。
SHARPNESS_NOTE = (
    "【禁止模糊 · 硬要求】整个人物必须落在清晰焦平面内：脸、头发、肩膀、手臂、双手、"
    "每根手指与指甲都要锐利清晰，能看清指节与指甲边缘。禁止景深虚化/浅景深把手糊掉，"
    "禁止运动模糊、局部模糊、涂抹感；背景也不要重度虚化。"
    "**参考图里的手若是模糊的（运动模糊/失焦），不要照搬那份模糊**，要按清晰锐利的正常手重画；"
    "实在画不清晰就把手势改简单（自然垂放或轻放身侧），或干脆让手不入镜 —— "
    "宁可手不入镜，也不许出现模糊的手。"
)


# 背景清理（用户 2026-09-21：「如果抽帧图片背景里有人需要将其他人去掉，现在生成的背景里的人
# 会变得很奇怪；如果背景很杂乱需要处理一下」）。桌面那份 .txt 只说「背景参考图一」，模型于是
# 把抽帧背景里的同框路人一起照抄进来（画得很怪），所以这一句也补进**消息正文**。
# 口径与 `backend.batch_ai.BACKGROUND_CLEAN_BLOCK` 一致：其他人全去掉、只留本人一个；
# 保留原场景只清杂物；不许用重度虚化糊掉背景。
BACKGROUND_NOTE = (
    "【背景清理 · 硬要求】画面里**只能有本人这一个人**：参考图背景里若出现其他人"
    "（路人、观众、同伴、伴舞、镜面或玻璃里的倒影），**一律不要画进成图**，也不许添加新的第二个人。"
    "去掉之后按同一场景的材质、透视与光线把背景自然补全：不要留下半个身子、残肢、多余的头或手，"
    "不要出现被抹掉的糊块或重复人影。背景里的杂物（桌椅、瓶罐、包袋、线缆、纸箱、堆叠物品、"
    "招牌字牌等）一并清理干净；**保留原场景的类型、空间结构与光线**，不要换成另一个地方。"
    "不许用重度虚化、大光圈或纯色块把杂乱背景糊掉，背景结构必须清楚、可供视频延展。"
)


def person_body(identity_text: str, ratio_text: str) -> str:
    """人物图那一步发在**消息正文**里的短指令：禁止模糊 → 背景清理 → 身份优先级 → 画面比例。

    顺序不能反：身份块与比例要求必须排在最后（模型对末尾最敏感），禁止模糊与背景清理排在它们之前。
    规则本体仍走 `.txt` 附件（用户 2026-09-16 的风控要求：长提示词不当正文发）。
    """
    parts = [
        SHARPNESS_NOTE,
        BACKGROUND_NOTE,
        (identity_text or "").strip(),
        (ratio_text or "").strip(),
    ]
    return "\n\n".join(part for part in parts if part)


def ratio_line(want_ratio: float) -> str:
    """画面比例的硬要求，放在消息末尾（模型对末尾最敏感）。

    为什么必须写死：实测 009 那次按 9比16图片.txt 出图，模型给了 1086x1448（3:4）——
    提示词里虽然写了 9:16，但不够硬。历史产出统计里 941x1672（真 9:16）出现过 24 次，
    说明模型支持这个比例，只是需要被明确要求。
    """
    if abs(want_ratio - 9 / 16) < 0.01:
        return ("【画面比例·硬要求】必须是 9:16 竖版（宽:高 = 9:16，例如 941x1672）。"
                "不要 3:4、不要 4:3、不要 1:1。")
    if abs(want_ratio - 4 / 3) < 0.01:
        return ("【画面比例·硬要求】必须是 4:3 横版（宽:高 = 4:3，例如 1448x1086）。"
                "不要 3:4、不要 16:9、不要 1:1。")
    if abs(want_ratio - 3 / 4) < 0.01:
        return ("【画面比例·硬要求】必须是 3:4 竖版（宽:高 = 3:4，例如 1086x1448）。"
                "不要 9:16、不要 4:3、不要 1:1。")
    return ""


def ratio_feedback(want_ratio: float) -> str:
    return ("画面比例不对。请重新生成同一张图，但" + ratio_line(want_ratio) +
            "\n人物身份、场景、构图内容、造型全部保持不变，只改画幅比例。")


# 封面背景口径（用户 2026-09-19）：
#   ① 「如果背景是凌乱的不需要和背景统一 只要适合comfyui就可以了」——凌乱就别硬套人物图的背景；
#   ② 紧接着又明确「封面图不需要出现 背景规则」——所以**不把这段写成提示词文本**，
#      封面提示词保持用户原话那几行（cover_common.build_prompt）＋比例硬要求即可。
#      上面那条口径作为"人工/后续判断"的准则，不进提示词。


def _type_text(page, text: str) -> None:
    """把短文本写进输入框（长提示词一律走 .txt 附件，这里只用于短指令/反馈）。"""
    page.evaluate(
        """(text) => { const el = document.querySelector('div.ProseMirror[role="textbox"], [contenteditable="true"][role="textbox"]');
           if (!el) return false; el.focus();
           try { document.execCommand('selectAll'); document.execCommand('delete');
                 if (document.execCommand('insertText', false, text)) return true; } catch (e) {}
           el.replaceChildren(document.createTextNode(text));
           el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text}));
           return true; }""",
        text)
    page.wait_for_timeout(600)


# 身份不达标时的反馈话术：短、明确、只说一件事 —— 让模型重新出图
IDENTITY_FEEDBACK = (
    "五官不对。这张脸和图二（晚柠.png）不是同一个人。\n"
    "请重新生成：以图二为**唯一**的面部来源，脸型、五官比例、眼距、眼型、眼皮结构、眉形、"
    "鼻梁、鼻头、鼻翼、唇形、唇厚、面颊、下颌线、年龄感都必须与图二完全一致，"
    "一眼就能认出是同一个人。\n"
    "图一只提供造型、服装、场景、灯光和构图，**不提供任何面部信息**（它的脸已经被模糊处理）。\n"
    "禁止把两张脸融合；禁止网红脸、模板脸、过度幼态、过度瘦脸；"
    "禁止为了好看去改脸型、放大眼睛、重塑鼻子、改变唇形与下颌线。\n"
    "其余构图与规格要求不变，直接重新生成最终图片。"
)

FACE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "same_person": {"type": "boolean"},
        "similarity": {"type": "integer"},
        "differences": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string"},
    },
    "required": ["same_person", "similarity", "differences", "verdict"],
}

FACE_INSTRUCTION = (
    "第一张图是**身份原型**（唯一身份锚点）。第二张图是**生成图**。\n"
    "判断第二张图里人物的脸是否与第一张图是**同一个人**。\n"
    "只依据面部结构：脸型、五官比例、眼型与眼距、眼皮结构、眉形、鼻梁鼻头鼻翼、"
    "唇形与唇厚、面颊、下颌线、年龄感。\n"
    "**不要**被发型、发色、妆容、服装、配饰、光线、背景、姿态、镜头角度影响。\n"
    "similarity：0-100 整数（90+ 一眼同一人；60-89 像但有差别；40-59 气质相近但不是同一张脸；"
    "40 以下明显不是）。differences：中文逐条列出具体五官差异（没有就空数组）。"
)


def judge_face(identity: Path, candidate: Path) -> dict | None:
    """用视觉模型判「这张脸是不是图二本人」。判据已在 001 上标定：
    没护栏的旧图 55/100（判不是）、带护栏的新图 86/100（判是）、原型对自己 97/100。
    """
    try:
        from backend import batch_ai

        content = [
            {"type": "text", "text": FACE_INSTRUCTION},
            {"type": "image_url", "image_url": {"url": batch_ai._data_url(identity), "detail": "high"}},
            {"type": "image_url", "image_url": {"url": batch_ai._data_url(candidate), "detail": "high"}},
        ]
        return _run_async(batch_ai._chat_structured(
            content=content, schema=FACE_SCHEMA, name="face_compare", label="人脸比对失败"))
    except Exception as exc:
        log(f"  ⚠ 人脸比对不可用：{type(exc).__name__}: {exc}")
        return None


def refine_identity(page, identity: Path, target: Path, want_ratio: float, args) -> None:
    """身份复核闭环：不达标就回一句「五官不对」让它重做；**始终保留分数最高的那张**。

    通过条件 = 判据的**二值结论 same_person=True**。
    分数只作参考：`--min-similarity > 0` 时才额外要求分数（默认 0 = 不额外卡分）。

    为什么这么改（2026-09-19 用户反馈「是不是太严格了，我看着像同一个人」+「怎么一直重复生成同一张」）：
    010 首图判 78/same_person=True（就是同一个人），却被 80 分线否掉 → 白重做两次，
    其中一次还把更好的那张覆盖成了 68/False。**判据说像就不该重做。**
    仍然依赖判据的硬结论：已知错例是 55/same_person=False，会被正确拦下。

    只在需要重做时才动手，且**始终保留分数最高的那张**（重做更差就丢弃）。
    """
    if not args.identity_check:
        return

    def passed(verdict: dict) -> bool:
        """通过条件 = 相似度 ≥ 门槛（默认 60）。

        用户 2026-09-19 定的规格：「**只有人物图要像，60 以上就可以了，封面图不需要管**」。
        所以这里只看分数，不再额外要求判据的二值结论（same_person 仍会打进日志供参考）。
        封面两步**不做**身份体检 —— 只校验画幅比例。
        """
        return int(verdict.get("similarity") or 0) >= args.min_similarity

    verdict = judge_face(identity, target)
    if verdict is None:
        log("  ⚠ 判据不可用，跳过身份复核")
        return
    best_score = int(verdict.get("similarity") or 0)
    best_ok = passed(verdict)
    log(f"  人物图身份体检：相似度={best_score}/100（门槛 {args.min_similarity}，"
        f"判据二值结论 same_person={verdict.get('same_person')}）→ "
        f"{'通过 ✅ 不重做' if best_ok else '低于门槛 ❌'}")
    for diff in (verdict.get("differences") or [])[:4]:
        log(f"      · {diff}")
    if best_ok or args.max_repairs <= 0:
        if not best_ok and args.max_repairs <= 0:
            log("  （--max-repairs 0：只体检不重做，保留这张）")
        return

    # 把当前这张记为「最佳」，后面只在重做更好时才替换
    best_file = Path(os.getenv("TEMP", ".")) / f"idbest_{target.stem}_{int(time.time())}.png"
    shutil.copy2(target, best_file)

    for attempt in range(1, args.max_repairs + 1):
        log(f"  → 回 Codex：五官不对，要求以图二为准重新生成（第 {attempt} 次）")
        imgs_before = page.evaluate(JS_LIST_IMAGES)
        ref_sizes = {probe_image(identity), probe_image(target)}
        _type_text(page, IDENTITY_FEEDBACK)
        disk_before = snapshot_generated()
        sent_at = time.time()
        if not send_now(page):
            log("  ✗ 反馈没发出去，停止修复")
            break
        got = wait_new_image(page, disk_before, imgs_before, ref_sizes, sent_at)
        if got is None:
            log("  ✗ 重做后没等到新图")
            break
        width, height = probe_image(got)
        drift = abs(width / height - want_ratio) / want_ratio
        if drift > RATIO_TOLERANCE:
            log(f"  ✗ 重做的图比例不对 {width}x{height}（偏差 {drift:.1%}），弃用")
            continue

        verdict2 = judge_face(identity, got)
        if verdict2 is None:
            log("  ⚠ 重做图判据不可用，弃用")
            continue
        score2 = int(verdict2.get("similarity") or 0)
        ok2 = bool(verdict2.get("same_person")) and score2 >= args.min_similarity
        log(f"  重做后 #{attempt}：same_person={verdict2.get('same_person')} "
            f"相似度={score2}/100（当前最佳 {best_score}）→ {'更好 ✅' if score2 > best_score else '没更好，弃用'}")
        if score2 > best_score:
            shutil.copy2(got, best_file)
            best_score, best_ok = score2, ok2
        if ok2:
            break

    shutil.copy2(best_file, target)
    log(f"  身份复核收尾：采用 {best_score}/100 的那张（{'达标 ✅' if best_ok else '仍未达标，保留最佳一张' }）")


def probe_image(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        img.load()
        return img.size


def _run_async(coro):
    """在**独立线程**里跑协程。

    为什么不能直接 `asyncio.run()`：Playwright 的 sync API 会在当前线程占住事件循环，
    直接调用会抛 `RuntimeError: asyncio.run() cannot be called from a running event loop`
    —— 实测导致 002 之后「糊脸」和「身份复核」全部静默失效（图一=原图）。
    """
    import asyncio
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def identity_block() -> str:
    """仓库里的身份优先级块（延迟导入，避免拖慢启动）。"""
    try:
        from backend.batch_ai import IDENTITY_PRIORITY_BLOCK

        return IDENTITY_PRIORITY_BLOCK
    except Exception as exc:
        log(f"  ⚠ 取身份优先级块失败：{type(exc).__name__}: {exc}")
        return ""


def defocus_scene(scene: Path, folder: Path) -> Path:
    """把「图一」（抽帧.jpg）里的人脸糊掉。

    为什么必须糊：图一里是**源视频那个人**的脸。原样送进去，模型会把那个人的五官
    混进成图，而用户要求「五官必须与晚柠.png 一致」—— 实测就是这条出的问题。
    糊掉之后图一仍然提供发型轮廓、服装、场景、灯光，但不再提供任何可换脸的面部信息。
    （复用仓库既有的 `batch_image.defocus_reference_face`，它用视觉模型定位人脸框。）
    """
    work = GUARD_DIR / folder.name
    cache = work / f"{scene.stem}-noface.png"
    if cache.is_file():
        log(f"  ↷ 复用已糊脸的图一：{cache.name}")
        return cache
    try:
        from backend.batch_image import defocus_reference_face

        work.mkdir(parents=True, exist_ok=True)
        path, ok = _run_async(defocus_reference_face(scene, work))
        if ok and Path(path).is_file():
            log(f"  ✓ 图一已糊脸：{Path(path).name}（模型看不到源视频那个人的五官了）")
            return Path(path)
        log("  ⚠ 没定位到人脸，仍用原图当图一")
    except Exception as exc:  # 糊脸失败绝不阻断出图
        log(f"  ⚠ 糊脸失败：{type(exc).__name__}: {exc}（仍用原图）")
    return scene


# --------------------------------------------------------------------------
# 步骤 / 作品
# --------------------------------------------------------------------------

def audit_person_identity(page, folders: list[Path], args) -> int:
    """身份返工：把「人物图身份分低于门槛」的作品整条重做。

    用户 2026-09-19：「记得把相似低于60的重跑」。

    - 逐个体检已有 `人物图.png`（判据 `judge_face`，门槛 `--fix-below`，默认 60）
    - 低于门槛的**整条重做**（person → bili → douyin，强制 `--force`）：
      人物图换掉之后，两张封面还是旧的那张脸就图文不一致了，所以必须一起重做
    - 最多 `--fix-rounds` 轮（默认 2），每轮重做完再体检一次，直到没有低于门槛的
    返回最终仍低于门槛的作品数。
    """
    if args.fix_below <= 0:
        return 0
    identity = identity_path()
    if not identity.is_file():
        log(f"身份原型不存在，跳过身份返工：{identity}")
        return 0

    def judge_existing() -> list[tuple[Path, int]]:
        lows: list[tuple[Path, int]] = []
        for folder in folders:
            if not folder.is_dir():
                continue
            person = folder / PERSON
            if not person.is_file() or person.stat().st_size < MIN_VALID_BYTES:
                continue
            verdict = judge_face(identity, person)
            if verdict is None:
                continue
            score = int(verdict.get("similarity") or 0)
            log(f"  体检 {folder.name[:34]:36s} {score:>3}/100"
                f"（门槛 {args.fix_below}）→ {'达标 ✅' if score >= args.fix_below else '要返工 ❌'}")
            if score < args.fix_below:
                lows.append((folder, score))
        return lows

    for round_no in range(1, args.fix_rounds + 1):
        log(f"──── 身份返工 第 {round_no}/{args.fix_rounds} 轮：体检所有人物图 ────")
        lows = judge_existing()
        if not lows:
            log(f"身份返工：没有低于 {args.fix_below} 分的人物图 ✅")
            return 0
        log(f"身份返工：发现 {len(lows)} 张低于 {args.fix_below} 分 → 整条重做（人物图+两张封面）")
        forced = argparse.Namespace(**vars(args))
        forced.force = True
        for folder, score in lows:
            log(f"  ↻ {folder.name}（{score} 分）")
            run_work(page, folder, list(STEP_ORDER), forced)

    rest = judge_existing()
    if rest:
        log(f"⚠ 身份返工结束，仍有 {len(rest)} 张低于 {args.fix_below} 分："
            + "、".join(f"{f.name[:20]}({s})" for f, s in rest))
    else:
        log("身份返工：全部达标 ✅")
    return len(rest)


# 后端地址按 `scripts/start-local.ps1` 同一份候选顺序探活（**首选 15111**：
# 用户 2026-09-19 要求默认端口全部挪到系统不会预留的 15000 以上），
# 否则后端明明活着、脚本却报「后端不可达」。
BACKEND_PORT_CANDIDATES = (15111, 15222, 18333)


def backend_url() -> str | None:
    """找到活着的后端地址（H3_BACKEND_URL / H3_SELF_URL 优先，其次按候选端口探活）。"""
    import httpx

    explicit = (os.getenv("H3_BACKEND_URL") or os.getenv("H3_SELF_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    for port in BACKEND_PORT_CANDIDATES:
        url = f"http://127.0.0.1:{port}"
        try:
            if httpx.get(f"{url}/api/batches/latest", timeout=5).status_code == 200:
                return url
        except Exception:
            continue
    return None


def batch_state() -> tuple[str, str, list[dict] | None]:
    """问 H3 后端：批量制作现在是什么状态，以及它有哪些条目。

    返回 (state, note, items)，state ∈ {"runnable", "idle", "unknown"}：
      - runnable：批量自己还有事干（待备料 / 已确认等出片 / 出片中 / 重新备料）
                  → **不要出图**，让批量先跑
      - idle    ：批量没事干了（全停在审核点等你确认，或已跳过/已完成）→ 这才是补图的空档
      - unknown ：后端读不出来 → **保守，不出图**（否则后端一停就会把缺的图一条条全刷出来，
                  正是 2026-09-19 触发 GPT 高频出图限制后要避免的）。要照跑传 --need-assume-idle。

    `items` 在 unknown 时是 None。用户 2026-09-19 的新流程：「当批量任务中没有可以运行的任务、
    全部都是需要人物图的时候再去跑」，而且「出图也是只需要运行的时候的那条出图」——只跑那一条。
    """
    import httpx

    url = backend_url()
    if url is None:
        return "unknown", f"后端不可达（试过 {', '.join(str(p) for p in BACKEND_PORT_CANDIDATES)}）", None
    try:
        resp = httpx.get(f"{url}/api/batches/latest", timeout=10)
        if resp.status_code != 200:
            return "unknown", f"后端 HTTP {resp.status_code}（{url}）", None
        state = resp.json()
    except Exception as exc:
        return "unknown", f"后端不可达（{type(exc).__name__}，{url}）", None
    items = [i for i in (state.get("items") or []) if str(i.get("status") or "") != "deleted"]
    busy = [i for i in items if str(i.get("status") or "") in BATCH_BUSY_STATUSES]
    note = (f"{url} status={state.get('status')} runnerActive={state.get('runnerActive')} "
            f"｜批量在干 {len(busy)} 条 / 缺东西 {len([i for i in items if _item_steps(i, PUBLISH_ROOT)])} 条 "
            f"/ 共 {len(items)} 条")
    return ("runnable" if busy else "idle"), note, items


# 批量「自己还有事干」的状态：等出片、出片中、重新备料、还没备料
BATCH_BUSY_STATUSES = {"pending", "confirmed", "running", "revising"}
# 这一轮可以替它出图的条目状态（已删除/已跳过的绝不复活）
GENERATABLE_STATUSES = {"awaiting_review", "failed", "confirmed", "completed"}

STEP_FILES = {"person": PERSON, "bili": SPECS["bili"][1], "douyin": SPECS["douyin"][1]}


def missing_steps(folder: Path) -> list[str]:
    """这个发布目录还缺哪几步（按 STEP_ORDER 返回）。"""
    out = []
    for step in STEP_ORDER:
        path = folder / STEP_FILES[step]
        if not path.is_file() or path.stat().st_size < MIN_VALID_BYTES:
            out.append(step)
    return out


def folder_for_item(publish_root: Path, item: dict) -> Path | None:
    """条目 → 它的发布目录。

    先认条目自己记的 `outputs.folder`（最准），再按**目录名末尾的作品号**
    （`_<awemeId>`，与 `backend/batch_worker._find_item_for_folder` 同一口径）匹配。
    """
    recorded = str((item.get("outputs") or {}).get("folder") or "").strip()
    if recorded and Path(recorded).is_dir():
        return Path(recorded)
    aweme = str(item.get("awemeId") or "").strip()
    if aweme.isdigit():
        for folder in publish_folders(publish_root):
            if folder.name.rsplit("_", 1)[-1] == aweme:
                return folder
    return None


def _item_steps(item: dict, publish_root: Path) -> list[str]:
    folder = folder_for_item(publish_root, item)
    return missing_steps(folder) if folder is not None else []


def find_next_needed(publish_root: Path, items: list[dict] | None) -> tuple[Path, list[str]] | None:
    """挑出「这一轮该出图的那一条」→ (发布目录, 还缺的步骤)。

    **只认批量里真实存在的条目**（按 `awemeId` / `outputs.folder` 对上发布目录）；已删除、
    已跳过、以及不在批量里的历史目录一律不碰 —— 用户 2026-09-19 的原话是「出图也是只需要
    运行的时候的那条出图」，而多余的出图正是触发 GPT 高频限制的原因。
    只有读不到批量（`items is None`，即 --need-assume-idle）时才退回「按目录名顺序找」。
    """
    if items is None:
        for folder in publish_folders(publish_root):
            steps = missing_steps(folder)
            if steps:
                return folder, steps
        return None
    for item in sorted(items, key=lambda i: i.get("index") or 0):
        if str(item.get("status") or "") not in GENERATABLE_STATUSES:
            continue
        folder = folder_for_item(publish_root, item)
        if folder is None:
            continue
        steps = missing_steps(folder)
        if steps:
            return folder, steps
    return None


def _copy_stamp(path: Path) -> tuple[float, int]:
    try:
        st = path.stat()
    except OSError:
        return (0.0, 0)
    return (st.st_mtime, st.st_size)


def _wait_copy_rewrite(path: Path, before: tuple[float, int], timeout: float) -> None:
    """等后端把发布文案按新人物图重写完 —— **必须先等到，再出封面**。

    封面上的大字取自 `发布文案.txt` 的标题，而「挂候选人物图」这一步过去会让后端
    `write_copy()` 按最终画面**重写标题**（老实现靠 `backend/publish_watcher` 每 5 秒扫
    发布目录）。先出封面就会把**旧标题**画进图里 —— 2026-09-19 实测 001~017 的 34 张封面
    就是这么和最终发布文案对不上的。

    ⚠️ 2026-09-20 起挂图改成「谁写图谁登记」（watcher 已删除），**登记不再重写文案**，
    所以这里通常等不到改动 —— 等不到就按现有文案出封面（默认 `--copy-wait 0` 可以不干等）。
    """
    if timeout <= 0:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _copy_stamp(path) != before:
            log("✓ 发布文案已按新人物图重写 → 封面用新标题")
            return
        time.sleep(5)
    log(f"↷ 发布文案没有被改过（挂图不再重写文案）→ 直接用现有文案出封面")


def run_need_one(page, args) -> int:
    """按需模式（--need-one）：批量没得跑时，**只补当时那一条**缺的图。"""
    state, note, items = batch_state()
    if state == "runnable":
        log(f"↷ 批量自己还有事干（{note}）→ 本次不出图，让批量先跑")
        return EXIT_OK
    if state == "unknown" and not args.need_assume_idle:
        log(f"↷ 批量状态读不出来（{note}）→ 保守起见本次不出图（要照跑加 --need-assume-idle）")
        return EXIT_OK
    pick = find_next_needed(PUBLISH_ROOT, items)
    if pick is None:
        log(f"↷ 批量里没有缺东西的作品 → 本次不出图（{note}）")
        return EXIT_OK
    folder, steps = pick
    log(f"✓ 批量是空档（{note}）→ 只跑这一条：{folder.name}（{'→'.join(steps)}）")

    if steps and steps[0] == "person":
        before = _copy_stamp(folder / COPY)
        run_work(page, folder, ["person"], args)
        if not (folder / PERSON).is_file():
            log("⛔ 人物图没落盘 → 当前作品立即停止（不进入封面）")
            return EXIT_OK
        _wait_copy_rewrite(folder / COPY, before, args.copy_wait)
        steps = [s for s in steps if s != "person"]

    if steps:
        run_work(page, folder, steps, args)
    return EXIT_OK


def detect_kind(folder: Path) -> str | None:
    if "_唱歌_" in folder.name:
        return "singing"
    if "_跳舞_" in folder.name:
        return "dance"
    return None


def run_step(page, step: str, folder: Path, kind: str, args) -> dict:
    targets = {"person": PERSON, "bili": SPECS["bili"][1], "douyin": SPECS["douyin"][1]}
    target = folder / targets[step]

    # gate 1
    if step == "person":
        rule = rule_path(kind)
        scene = folder / SCENE
        identity = identity_path()
        copy_path = folder / COPY
        for label, path in (("抽帧", scene), ("晚柠", identity), ("发布文案", copy_path), ("规则", rule)):
            if not path.is_file():
                log(f"门1 ✗ 缺少{label}：{path}")
                return {"code": EXIT_GATE1}
        want_ratio = SING_RATIO if kind == "singing" else DANCE_RATIO
        scene_used = defocus_scene(scene, folder) if args.identity_guard else scene
        # 消息正文 = 禁止模糊 + 身份优先级 + 画面比例（身份与比例排在最后，模型最后读到）
        body = person_body(identity_block() if args.identity_guard else "", ratio_line(want_ratio))
        images, prompt_file = [scene_used, identity], rule
        log(f"门1 ✓ {scene.name}{probe_image(scene)} {identity.name}{probe_image(identity)} "
            f"规则={rule.name} ｜ 图一={'已糊脸' if scene_used != scene else '原图'}"
            f" ｜ 身份块={'开' if body else '关'}")
    else:
        person = folder / PERSON
        copy_path = folder / COPY
        if not person.is_file() or person.stat().st_size < MIN_VALID_BYTES:
            log(f"门1 ✗ 缺少合格的人物图，封面阶段禁止开始：{person}")
            return {"code": EXIT_GATE1}
        title, intro = parse_copy(copy_path)
        want_ratio = BILI_RATIO if step == "bili" else DOUYIN_RATIO
        images = [person] if step == "bili" else []
        prompt_file = None
        # 封面提示词 = 用户原话那几行（cover_common.build_prompt）＋ 比例硬要求。
        # **不追加背景规则文本**（用户 2026-09-19：「封面图不需要出现 背景规则」）。
        body = build_prompt(title, intro, step)
        rl = ratio_line(want_ratio)
        if rl:
            body = body + "\n" + rl
        log(f"门1 ✓ 人物图{probe_image(person)} 标题「{title}」 简介「{intro[:30]}」")

    if step == "person":
        open_new_chat(page)
    # bili 前面已经新开过对话；douyin 故意沿用同一个对话
    if step == "bili":
        open_new_chat(page)

    st = state(page)
    if st["chips"] or (st["text"] or "").strip():
        log("  ⚠ 输入区不干净，先清空")
        page.evaluate("""() => { const el = document.querySelector('div.ProseMirror[role="textbox"], [contenteditable="true"][role="textbox"]');
            if (el) { el.focus(); document.execCommand('selectAll'); document.execCommand('delete'); } }""")
        page.wait_for_timeout(800)

    expect = len(images) + (1 if prompt_file else 0)
    if expect and attach_files(page, images, prompt_file) < 0:
        return {"code": EXIT_GATE1}
    if expect and not wait_attachments(page, expect):
        return {"code": EXIT_GATE2}

    if body.strip():
        _type_text(page, body)

    if step == "person":
        ref_sizes = {probe_image(scene), probe_image(identity)}
    else:
        ref_sizes = {probe_image(folder / PERSON)}   # 封面时对话里已有的人物图，必须排除
    imgs_before = page.evaluate(JS_LIST_IMAGES)
    disk_before = snapshot_generated()
    sent_at = time.time()
    if not send_now(page):
        return {"code": EXIT_GATE2}

    got = wait_new_image(page, disk_before, imgs_before, ref_sizes, sent_at)
    if got is None:
        # 这一轮结束了却没出图（实测 011 出现过"26 秒结束、既无图也无输出"）→ 催它一次
        for attempt in range(1, args.max_nudges + 1):
            log(f"  → 催一次：请现在直接调用出图工具生成（第 {attempt}/{args.max_nudges} 次）")
            imgs_before = page.evaluate(JS_LIST_IMAGES)
            ref_sizes = {probe_image(p) for p in images}
            _type_text(page, NUDGE_TEXT)
            disk_before = snapshot_generated()
            sent_at = time.time()
            if not send_now(page):
                break
            got = wait_new_image(page, disk_before, imgs_before, ref_sizes, sent_at)
            if got is not None:
                break
        if got is None:
            log("  ✗ 催过之后仍然没有产出图片 → 本作品停止")
            return {"code": EXIT_GATE3}

    width, height = probe_image(got)
    ratio = width / height
    drift = abs(ratio - want_ratio) / want_ratio
    log(f"  产物 {width}x{height} 比例{ratio:.3f}（期望{want_ratio:.3f} 偏差{drift:.1%}）")

    # 比例不对就回一句「比例硬要求」让它重做（和身份复核同一个思路）
    repairs = 0
    while drift > RATIO_TOLERANCE and repairs < args.max_ratio_repairs:
        repairs += 1
        log(f"  → 回 Codex：画面比例不对，要求 {want_ratio:.4f} 重做（第 {repairs} 次）")
        imgs_before = page.evaluate(JS_LIST_IMAGES)
        ref_sizes = {probe_image(p) for p in images}
        _type_text(page, ratio_feedback(want_ratio))
        disk_before = snapshot_generated()
        sent_at = time.time()
        if not send_now(page):
            log("  ✗ 比例反馈没发出去")
            break
        redo = wait_new_image(page, disk_before, imgs_before, ref_sizes, sent_at)
        if redo is None:
            log("  ✗ 重做后没等到新图")
            break
        got = redo
        width, height = probe_image(got)
        ratio = width / height
        drift = abs(ratio - want_ratio) / want_ratio
        log(f"  重做产物 {width}x{height} 比例{ratio:.3f}（偏差{drift:.1%}）")

    if drift > RATIO_TOLERANCE:
        log(f"  门3 ✗ 宽高比仍不合格（偏差 {drift:.1%}）")
        return {"code": EXIT_GATE3}
    if target.exists() and not args.force:
        log(f"  门3 ✗ 目标已存在（未加 --force）：{target}")
        return {"code": EXIT_GATE3}
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(got, target)
    log(f"  门3 ✓ 已落盘 {target.name} {probe_image(target)} {target.stat().st_size // 1024}KB")

    # 人物图：用标定好的判据复核身份，不达标就回「五官不对」让它重做
    if step == "person":
        refine_identity(page, identity, target, want_ratio, args)
    return {"code": EXIT_OK}


def run_work(page, folder: Path, steps: list[str], args) -> str:
    kind = detect_kind(folder)
    if not kind:
        log(f"跳过（目录名无法判断类型）：{folder.name}")
        return "类型未知"
    log(f"══════ {folder.name} ｜ {kind} ｜ {'→'.join(steps)} ══════")

    results: dict[str, str] = {}
    for step in steps:
        target_name = {"person": PERSON, "bili": SPECS["bili"][1], "douyin": SPECS["douyin"][1]}[step]
        target = folder / target_name
        if not args.force and target.is_file() and target.stat().st_size >= MIN_VALID_BYTES:
            results[step] = "已有（跳过）"
            log(f"↷ {step}：{target_name} 已存在，跳过")
            continue
        outcome = run_step(page, step, folder, kind, args)
        if outcome["code"] != EXIT_OK:
            results[step] = f"失败(码{outcome['code']})"
            log(f"⛔ {folder.name} 在「{step}」失败 → 当前作品立即停止")
            break
        results[step] = "完成"
        if step == "person" and not (folder / PERSON).is_file():
            results[step] = "人物图未落盘"
            log("⛔ 人物图未落盘 → 当前作品立即停止（不许跳过人物图做封面）")
            break
    log(f"──── {folder.name}：{'／'.join(f'{k}:{v}' for k, v in results.items())}")
    return "／".join(results.values())


def do_list() -> None:
    for folder in publish_folders(PUBLISH_ROOT):
        flags = []
        for label, name in (("人物图", PERSON), ("B站", SPECS["bili"][1]), ("抖音", SPECS["douyin"][1])):
            hit = (folder / name).is_file() and (folder / name).stat().st_size >= MIN_VALID_BYTES
            flags.append(f"{label}={'有' if hit else '无'}")
        log(f"{folder.name}  " + " ".join(flags))


def main() -> int:
    parser = argparse.ArgumentParser(description="聊天模式出图：人物图 → B站 → 抖音")
    parser.add_argument("--dir", dest="dir_name")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--steps", default="person,bili,douyin")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--identity-guard", dest="identity_guard", action="store_true", default=True,
                        help="糊掉图一的脸并在末尾追加身份优先级块（默认开）")
    parser.add_argument("--no-identity-guard", dest="identity_guard", action="store_false",
                        help="关掉身份护栏（用于对比测试）")
    parser.add_argument("--min-similarity", type=int, default=60,
                        help="人物图身份相似度门槛（默认 60，用户 2026-09-19 定的规格："
                             "60 以上就可以；封面不判身份）。0 = 只要判据返回就通过")
    parser.add_argument("--max-repairs", type=int, default=1,
                        help="判据判为「不是同一个人」时回「五官不对」重做的最大次数；0 = 只体检不重做")
    parser.add_argument("--identity-check", dest="identity_check", action="store_true", default=True,
                        help="出图后做身份体检（默认开）")
    parser.add_argument("--no-identity-check", dest="identity_check", action="store_false",
                        help="跳过身份体检")
    parser.add_argument("--max-ratio-repairs", type=int, default=1,
                        help="画幅比例不对时回「比例硬要求」让它重做的最大次数")
    parser.add_argument("--max-nudges", type=int, default=1,
                        help="这一轮结束却没出图时，催它「直接出图」的最大次数")
    parser.add_argument("--fix-below", type=int, default=60,
                        help="身份返工门槛：跑完后把人物图身份分低于它的作品整条重做（默认 60，"
                             "用户 2026-09-19「记得把相似低于60的重跑」；0 = 不返工）")
    parser.add_argument("--fix-rounds", type=int, default=2,
                        help="身份返工最多做几轮（每轮重做后再体检一次）")
    parser.add_argument("--cooldown", type=float, default=30.0,
                        help="每个作品之间的冷却秒数（降低 ChatGPT 限流概率，默认 30；0 = 不冷却）")
    parser.add_argument("--need-one", dest="need_one", action="store_true",
                        help="按需模式（新默认流程）：批量没得跑时，只补批量里那一条还缺的图")
    parser.add_argument("--loop", type=float, default=0.0,
                        help="配合 --need-one：每隔这么多秒再判断一次（0 = 只判断一轮就退出）")
    parser.add_argument("--need-assume-idle", dest="need_assume_idle", action="store_true",
                        help="--need-one 时：即使读不到批量状态也照跑（默认读不到就不跑）")
    parser.add_argument("--copy-wait", dest="copy_wait", type=float, default=180.0,
                        help="--need-one 时：人物图落盘后，等后端按新图重写发布文案的秒数（默认 180；0 = 不等）")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--no-instance", action="store_true", help="不自动起专用实例（假定已在跑）")
    args = parser.parse_args()

    if args.list:
        do_list()
        return EXIT_OK

    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    if steps != [s for s in STEP_ORDER if s in steps] or any(s not in STEP_ORDER for s in steps):
        parser.error(f"步骤必须严格按 {STEP_ORDER} 顺序，收到 {steps}")

    if args.need_one:
        folders = []
    elif args.all:
        folders = publish_folders(PUBLISH_ROOT)
    elif args.dir_name:
        p = Path(args.dir_name)
        folders = [p if p.is_absolute() else PUBLISH_ROOT / p]
    else:
        parser.error("需要 --need-one / --dir / --all 之一")

    if not args.no_instance and not ensure_instance():
        return 2

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(ENDPOINT)
        page = pick_page(browser)
        log(f"已连接聊天页：{page.url[:80]}")

        # 按需模式：只在批量有空档、且某条缺人物图时，跑那一条
        if args.need_one:
            while True:
                run_need_one(page, args)
                if args.loop <= 0:
                    return EXIT_OK
                log(f"按需模式：{args.loop:.0f} 秒后再判断一次（停掉这个进程即可结束）")
                time.sleep(args.loop)

        for folder in folders:
            if folder.is_dir():
                summary = run_work(page, folder, steps, args)
                # 只有真的发过请求（有"完成"）才需要冷却；整条都跳过就不用白等
                if args.cooldown > 0 and "完成" in summary:
                    log(f"冷却 {args.cooldown:.0f} 秒（降低被限流的概率）")
                    time.sleep(args.cooldown)
        # 跑完再做一遍身份返工：低于门槛的人物图整条重做（连带两张封面）
        audit_person_identity(page, folders, args)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
