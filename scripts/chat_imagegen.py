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
            if (w, h) in ref_sizes or src in seen_before:
                continue
            if src.startswith("data:image"):
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


def probe_image(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        img.load()
        return img.size


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
        images, prompt_file, body = [scene, identity], rule, ""
        log(f"门1 ✓ {scene.name}{probe_image(scene)} {identity.name}{probe_image(identity)} 规则={rule.name}")
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
        page.evaluate(
            """(text) => { const el = document.querySelector('div.ProseMirror[role="textbox"], [contenteditable="true"][role="textbox"]');
               if (!el) return false; el.focus();
               try { document.execCommand('selectAll'); document.execCommand('delete');
                     if (document.execCommand('insertText', false, text)) return true; } catch (e) {}
               el.replaceChildren(document.createTextNode(text));
               el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text}));
               return true; }""",
            body)
        page.wait_for_timeout(600)

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
