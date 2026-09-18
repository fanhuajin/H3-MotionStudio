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

SING_RATIO, DANCE_RATIO, BILI_RATIO, DOUYIN_RATIO = 4 / 3, 9 / 16, 4 / 3, 3 / 4
RATIO_TOLERANCE = 0.06
STEP_ORDER = ("person", "bili", "douyin")

UPLOAD_TIMEOUT = float(os.getenv("H3_CHATGPT_UPLOAD_TIMEOUT", "600"))
SEND_TIMEOUT = float(os.getenv("H3_CHATGPT_SEND_TIMEOUT", "300"))
IMAGE_TIMEOUT = float(os.getenv("H3_CHATGPT_IMAGE_TIMEOUT", "900"))

EXIT_OK, EXIT_GATE1, EXIT_GATE2, EXIT_GATE3 = 0, 1, 2, 3


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------
# 专用实例 / 连接
# --------------------------------------------------------------------------

def find_exe() -> str:
    import glob
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process ChatGPT -ErrorAction SilentlyContinue | Select-Object -First 1).Path"],
        capture_output=True, text=True, timeout=30)
    path = (out.stdout or "").strip()
    if path and Path(path).is_file():
        return path
    cands = sorted(glob.glob(r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\ChatGPT.exe"))
    if cands:
        return cands[-1]
    raise FileNotFoundError("找不到 ChatGPT 桌面端 exe")


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


def wait_new_image(page, disk_before: dict, imgs_before: list[dict], ref_sizes: set,
                   sent_at: float, timeout: float = IMAGE_TIMEOUT,
                   scratch: Path | None = None) -> Path | None:
    """等生成结果。

    **聊天模式的真实落点（2026-09-19 实测）**：页面上一个
    `<img src="data:image/png;base64,...">`，尺寸 1448x1086 —— 既不是 `blob:`，
    也不会进 `~/.codex/generated_images`（那是 Codex 侧出图才走的路）。
    所以这里按优先级找，并**一律排除参考图**（抽帧/晚柠/人物图，尺寸已知）：

      1. `data:image/...` 内联图（实测走的就是这条）
      2. `app://fs/@fs/<本地路径>` —— 能直接读到文件
      3. `blob:` 大图（老外壳）→ canvas 导出（fetch(blob:) 被 CSP 拦）
      4. `generated_images/**` 里新出现的 png（Codex 侧）
    """
    seen_before = {i["src"] for i in imgs_before}
    scratch = scratch or Path(os.getenv("TEMP", "."))
    deadline = time.time() + timeout
    heartbeat = time.time()

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

        if time.time() - heartbeat > 30:
            heartbeat = time.time()
            try:
                prog = page.evaluate(JS_PROGRESS)
            except Exception:
                prog = ""
            log(f"  等出图中… 已等 {int(time.time() - (deadline - timeout))}s"
                + (f" 进度 {prog}" if prog else ""))
        page.wait_for_timeout(3000)

    log(f"  ✗ 等出图超时（{timeout:.0f}s）")
    return None


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
    """身份复核闭环：不达标就回一句「五官不对」，让它重做，直到达标或用完次数。"""
    if args.min_similarity <= 0:
        return
    for attempt in range(args.max_repairs + 1):
        verdict = judge_face(identity, target)
        if verdict is None:
            log("  ⚠ 判据不可用，跳过身份复核")
            return
        score = int(verdict.get("similarity") or 0)
        ok = bool(verdict.get("same_person")) and score >= args.min_similarity
        log(f"  身份复核 #{attempt}：same_person={verdict.get('same_person')} "
            f"相似度={score}/100（门槛 {args.min_similarity}）→ {'通过 ✅' if ok else '不通过 ❌'}")
        for diff in (verdict.get("differences") or [])[:4]:
            log(f"      · {diff}")
        if ok:
            return
        if attempt >= args.max_repairs:
            log("  ⚠ 修复次数用尽，保留当前这张（可选：调高 --max-repairs）")
            return

        # 回 Codex 一句「五官不对」→ 等它重做 → 覆盖目标
        log("  → 回 Codex：五官不对，要求以图二为准重新生成")
        imgs_before = page.evaluate(JS_LIST_IMAGES)
        ref_sizes = {probe_image(identity), probe_image(target)}
        _type_text(page, IDENTITY_FEEDBACK)
        disk_before = snapshot_generated()
        sent_at = time.time()
        if not send_now(page):
            log("  ✗ 反馈没发出去，停止修复")
            return
        got = wait_new_image(page, disk_before, imgs_before, ref_sizes, sent_at)
        if got is None:
            log("  ✗ 重做后没等到新图，保留当前这张")
            return
        width, height = probe_image(got)
        drift = abs(width / height - want_ratio) / want_ratio
        if drift > RATIO_TOLERANCE:
            log(f"  ✗ 重做的图比例不对 {width}x{height}（偏差 {drift:.1%}），保留当前这张")
            return
        shutil.copy2(got, target)
        log(f"  ↻ 已用重做的图覆盖 {target.name}（{width}x{height}）")


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
        rule = PUBLISH_ROOT / (SING_RULE if kind == "singing" else DANCE_RULE)
        scene = folder / SCENE
        identity = PUBLISH_ROOT / IDENTITY
        copy_path = folder / COPY
        for label, path in (("抽帧", scene), ("晚柠", identity), ("发布文案", copy_path), ("规则", rule)):
            if not path.is_file():
                log(f"门1 ✗ 缺少{label}：{path}")
                return {"code": EXIT_GATE1}
        want_ratio = SING_RATIO if kind == "singing" else DANCE_RATIO
        scene_used = defocus_scene(scene, folder) if args.identity_guard else scene
        # 身份优先级块放在**消息正文**里：它排在 .txt 附件之后，是模型最后读到的东西
        body = identity_block() if args.identity_guard else ""
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
        prompt_file, body = None, build_prompt(title, intro, step)
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
        return {"code": EXIT_GATE3}

    width, height = probe_image(got)
    ratio = width / height
    drift = abs(ratio - want_ratio) / want_ratio
    log(f"  产物 {width}x{height} 比例{ratio:.3f}（期望{want_ratio:.3f} 偏差{drift:.1%}）")
    if drift > RATIO_TOLERANCE:
        log("  门3 ✗ 宽高比不合格")
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
    for folder in sorted(p for p in PUBLISH_ROOT.iterdir() if p.is_dir()):
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
    parser.add_argument("--min-similarity", type=int, default=80,
                        help="人物图身份相似度门槛（0-100，判据已标定：不合 55 / 合 86 / 原型 97）；0 = 不复核")
    parser.add_argument("--max-repairs", type=int, default=2,
                        help="身份不达标时回「五官不对」让它重做的最大次数")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--no-instance", action="store_true", help="不自动起专用实例（假定已在跑）")
    args = parser.parse_args()

    if args.list:
        do_list()
        return EXIT_OK

    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    if steps != [s for s in STEP_ORDER if s in steps] or any(s not in STEP_ORDER for s in steps):
        parser.error(f"步骤必须严格按 {STEP_ORDER} 顺序，收到 {steps}")

    if args.all:
        folders = sorted(p for p in PUBLISH_ROOT.iterdir() if p.is_dir())
    elif args.dir_name:
        p = Path(args.dir_name)
        folders = [p if p.is_absolute() else PUBLISH_ROOT / p]
    else:
        parser.error("需要 --dir 或 --all")

    if not args.no_instance and not ensure_instance():
        return 2

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(ENDPOINT)
        page = pick_page(browser)
        log(f"已连接聊天页：{page.url[:80]}")
        for folder in folders:
            if folder.is_dir():
                run_work(page, folder, steps, args)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
