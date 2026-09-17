"""backend.jianying_recognize — 用剪映「识别歌词」拿最准的时间轴。

## 为什么值得为它动一次鼠标

用户 2026-09-16：「剪映的歌词识别 他的时间是最准的」。确实如此——它是剪映云端模型
对**这首歌的真实音频**做识别+对齐；而本地那套（whisper 实测 + FunASR 强制对齐）本质是
"把官方歌词强行贴到人声上"，遇到转音/拖腔/和声会偏。

## 鼠标只用在"打开草稿"这一下

剪映首页草稿项是 QML 控件：投递鼠标消息不认、UIA 模式调用（DefaultAction='按'）无效，
只有真实双击才能打开（用户已授权这一次）。**打开之后全部靠投递消息**：
编辑器里已验证「文本 / 识别歌词 / 开始识别」三处坐标点击有效，不需要鼠标。

（GitHub 的 pyJianYingDraft 也是用真实点击打开草稿：`control.Click(simulateMove=False)`，
而 uiautomation 的实现里 `Click()` 最终会 `SetCursorPos`。）

## 草稿怎么来

克隆 `data/jianying_template`（从已验证草稿剥掉字幕轨得到的骨架），把视频素材换成
本条成片，写入剪映草稿目录 —— 纯文件操作，不用剪映界面。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import jianying_desktop as jy  # noqa: E402
from backend import jianying_uia as u  # noqa: E402

DRAFT_ROOT = Path(r"C:\Users\admin\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft")
TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "jianying_template"
PLACEHOLDER = "__VIDEO_PATH__"
EDITOR_CLASS_PREFIX = "MainWindow"

# 编辑器内坐标（窗口最大化到 3458x1398 时实测有效；剪映没给这些控件 FD，只能按位置点）
TEXT_TAB = (194, 60)        # 顶部「文本」
RECOGNIZE_ITEM = (87, 343)  # 左栏「识别歌词」
START_BUTTON = (615, 747)   # 面板右下「开始识别」（青色块实测 (571,734)-(659,760)，2026-09-16 重标定）
EDITOR_SIZE = (3458, 1398)

# 草稿名**每次都新建一个**（用户 2026-09-16：「不允许用草稿 每次都是新开始」）：
# 固定复用一个名字会让第二次跑打开上一条的现场（旧字幕/旧素材残留），所以改成按时间戳
# 生成全新草稿名，骨架仍从 `data/jianying_template` 重建。仍然**绝不删草稿目录**
# （删了会触发剪映「草稿列表异常：检测到部分草稿丢失」）。
DEFAULT_DRAFT_PREFIX = "H3字幕"


def new_draft_name() -> str:
    """每次调用都生成一个**没被占用过**的新草稿名（同秒内重试也安全）。"""
    stamp = time.strftime("%m%d_%H%M%S")
    name = f"{DEFAULT_DRAFT_PREFIX}_{stamp}"
    suffix = 1
    while (DRAFT_ROOT / name).exists():
        suffix += 1
        name = f"{DEFAULT_DRAFT_PREFIX}_{stamp}_{suffix}"
    return name


DEFAULT_DRAFT_NAME = DEFAULT_DRAFT_PREFIX      # 兼容旧引用（仅作前缀语义）


def _probe(video: Path) -> tuple[int, int, int]:
    """读视频宽高与时长（微秒）。"""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height",
                          "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1", str(video)],
                         capture_output=True, text=True).stdout
    w = h = 0
    dur = 0.0
    for line in out.splitlines():
        if line.startswith("width="):
            w = int(line.split("=")[1])
        elif line.startswith("height="):
            h = int(line.split("=")[1])
        elif line.startswith("duration="):
            dur = float(line.split("=")[1])
    if not (w and h and dur):
        raise RuntimeError(f"读不出视频信息：{video}")
    return w, h, int(round(dur * 1e6))


def cleanup_drafts(keep: str | None = None) -> None:
    """**已停用**：删草稿文件夹会触发剪映「草稿列表异常」告警，改成固定复用一个草稿名。

    保留这个函数只是为了兼容旧调用；现在什么都不做。
    """
    return


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001 —— 坏文件按"没有"处理
        return {}


def template_skeleton() -> dict:
    """模板草稿骨架（**每次都以它为准重建**，见 `clone_draft` 的注释）。

    模板里必须有可用的视频素材与视频轨，否则重建出来的还是一条空时间线 —— 宁可当场报错，
    也不要让剪映里出现一条「什么都没有」的草稿。
    """
    path = TEMPLATE / "draft_content.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"缺少剪映草稿模板：{TEMPLATE}（用 scripts/make_jianying_template.py 从一条已验证草稿生成）")
    content = _read_json(path)
    videos = (content.get("materials") or {}).get("videos") or []
    video_tracks = [t for t in (content.get("tracks") or [])
                    if t.get("type") == "video" and (t.get("segments") or [])]
    if not videos or not video_tracks:
        raise RuntimeError(
            f"剪映草稿模板缺少视频素材或视频轨（{path}）：重建出来的会是一条空时间线。"
            "请用 scripts/make_jianying_template.py 从一条已验证草稿重新生成模板。")
    return content


def clone_draft(name: str, video: Path, *, canvas: tuple[int, int, str] = (1920, 1440, "4:3")) -> Path:
    """按模板准备草稿：**骨架每次都从模板重建**，写回同一个草稿目录（绝不删目录）。

    为什么不"就地改已有草稿"（2026-09-16 踩到的坑，用户原话「剪映里的文件是错误的」）：

        剪映打开草稿后会按自己的状态回存 `draft_content.json`。只要有一次它存下的是
        **空项目**（`duration: 0`、`materials.videos: []`、`tracks: []`），旧实现
        「就地改」就再也补不回视频了 —— 它只会去更新*已存在*的素材与轨道，而两者都已经
        没了。于是双击打开草稿后剪映里是一条空时间线，识别自然一行都出不来
        （实测：草稿被存成 3257 字节的空项目，模板是 8067 字节）。

    所以骨架**只能来自模板**（模板是从一条已验证草稿剥出来的，带视频素材+视频轨）；
    草稿目录仍然原地复用、一个文件都不删（删了剪映会弹「草稿列表异常：检测到部分草稿丢失」），
    只把内容覆盖回模板骨架，再打上本条的 id / 画布 / 视频。
    """
    if not TEMPLATE.is_dir():
        raise FileNotFoundError(
            f"缺少剪映草稿模板：{TEMPLATE}（用 scripts/make_jianying_template.py 从一条已验证草稿生成）")
    w, h, dur = _probe(video)
    dest = DRAFT_ROOT / name
    if not dest.is_dir():
        shutil.copytree(TEMPLATE, dest)
    else:
        # 目录还在但模板文件缺了（被剪映清过/手工删过）就补回来；**绝不 rmtree**
        for src in TEMPLATE.iterdir():
            target = dest / src.name
            if src.is_file() and not target.exists():
                shutil.copy2(src, target)

    content = template_skeleton()
    # 沿用这条草稿原来的 id（剪映的索引里记着它），没有才新生成
    uid = str(_read_json(dest / "draft_content.json").get("id") or "").strip() or str(uuid.uuid4()).upper()
    content["id"] = uid
    content["duration"] = dur
    content["canvas_config"] = {"width": canvas[0], "height": canvas[1], "ratio": canvas[2]}
    # 每次识别前清掉上次的字幕，否则「识别歌词」会追加到旧结果后面
    mats = content.setdefault("materials", {})
    mats["texts"] = []
    content["tracks"] = [t for t in content.get("tracks") or [] if t.get("type") != "text"]
    videos = mats.get("videos") or []
    for v in videos:
        v.update({"id": str(uuid.uuid4()).upper(), "path": str(video).replace("\\", "/"),
                  "material_name": video.name, "duration": dur, "width": w, "height": h})
    for track in content.get("tracks") or []:
        for seg in track.get("segments") or []:
            if videos:
                seg["material_id"] = videos[0]["id"]
            seg["target_timerange"] = {"start": 0, "duration": dur}
    (dest / "draft_content.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    meta_path = dest / "draft_meta_info.json"
    raw = meta_path.read_text(encoding="utf-8") if meta_path.is_file() else "{}"
    raw = raw.replace("__DRAFT_NAME__", name).replace("__DRAFT_FOLD__", str(dest).replace("\\", "/"))
    raw = raw.replace(PLACEHOLDER, str(video).replace("\\", "/"))
    try:
        meta = json.loads(raw)
    except Exception:
        meta = {}
    meta["draft_id"] = uid
    meta["draft_name"] = name
    meta["draft_fold_path"] = str(dest).replace("\\", "/")
    meta["tm_draft_create"] = meta.get("tm_draft_create") or int(time.time() * 1e6)
    meta["tm_draft_modified"] = int(time.time() * 1e6)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return dest


def _in_editor() -> bool:
    try:
        return u.app_window().ClassName.startswith(EDITOR_CLASS_PREFIX)
    except Exception:
        return False


def open_draft(name: str, *, timeout: float = 60.0, attempts: int = 3) -> int:
    """在剪映首页找到该草稿并打开（**这里用真实双击，用户已授权**），返回窗口句柄。

    2026-09-16 实测两个坑：
      · 剪映不在前台时双击会落到别的窗口上（当前台是浏览器时表现为"双击后没进编辑页"）——
        所以**每次双击前都强切前台并校验**；
      · 首页草稿列表是懒加载的，刚新建的草稿可能还没渲染出来 → 找不到就等一会重找。
    """
    u.ensure_app_accessibility(restart=_in_editor())   # 在编辑器里就先重启，必定回首页
    last_error = "未知原因"
    for attempt in range(1, max(1, attempts) + 1):
        ctrl = u.find(f"HomePageDraftTitle:{name}", exact=True, timeout=20)
        if not ctrl:
            last_error = f"剪映首页找不到草稿：{name}"
            time.sleep(3)
            continue
        ctrl.Refind() if hasattr(ctrl, "Refind") else None
        try:
            jy.force_foreground_hard(jy.find_window(restore=False) or 0)
        except Exception:
            pass
        if jy.GetForegroundWindow() != (jy.find_window(restore=False) or 0):
            last_error = "剪映没能切到前台（拒绝双击，避免点到别的窗口）"
            continue
        btn = ctrl.GetParentControl()
        btn.Click(simulateMove=False)      # GitHub 同款：会短暂移动光标
        time.sleep(0.8)
        btn.Click(simulateMove=False)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.5)
            if _in_editor():
                return jy.find_window()
        last_error = f"双击后剪映没有进入编辑页（第 {attempt} 次）"
        time.sleep(2)
    raise RuntimeError(last_error)


def _draft_texts(draft: Path) -> list[tuple[float, float, str]]:
    """读草稿里的字幕（识别结果）：[(start_s, end_s, text)]。"""
    f = draft / "draft_content.json"
    if not f.is_file():
        return []
    try:
        j = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    timerange: dict[str, tuple[float, float]] = {}
    for track in j.get("tracks") or []:
        for seg in track.get("segments") or []:
            t = seg.get("target_timerange") or {}
            timerange[seg.get("material_id")] = (t.get("start", 0) / 1e6, t.get("duration", 0) / 1e6)
    rows = []
    for t in (j.get("materials") or {}).get("texts") or []:
        try:
            text = (json.loads(t.get("content") or "{}").get("text") or "").strip()
        except Exception:
            continue
        st, du = timerange.get(t.get("id"), (0.0, 0.0))
        if text and du > 0:
            rows.append((round(st, 2), round(st + du, 2), text))
    rows.sort()
    return rows


def _real_click(hwnd: int, x: int, y: int, *, settle: float = 2.5) -> None:
    """真实鼠标点击（渐进移动 → 停顿 → 按下抬起），并在点前把剪映抢到前台。

    为什么不能用 `jy.post_click`（2026-09-16 全流程实测）：
      · 投递鼠标消息对字号 spinner、字体下拉浮层**完全无效**（浮层还会被当成"框外点击"关掉）；
      · 更要紧的是**点击会落到当前真实前台窗口上**——实测前台是浏览器时，所有点击都打到浏览器，
        表现为"怎么点都不中"。
    所以这里：① `AttachThreadInput` 抢前台并**校验**成功；② 分步移动光标 + 落点前停顿
    （QML 的悬停/焦点态需要真实鼠标移动才会激活）；③ 再按下/抬起。
    """
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    # 强切前台（不校验就点击 = 点到别的窗口上）；抢不到就**拒绝点击**而不是照点。
    if not jy.force_foreground_hard(hwnd) or user32.GetForegroundWindow() != hwnd:
        raise RuntimeError("剪映不在前台，拒绝点击（否则会点到别的窗口）")
    time.sleep(0.3)

    cur = wt.POINT()
    user32.GetCursorPos(ctypes.byref(cur))
    for step in range(1, 13):
        user32.SetCursorPos(int(cur.x + (x - cur.x) * step / 12),
                            int(cur.y + (y - cur.y) * step / 12))
        time.sleep(0.012)
    user32.SetCursorPos(x, y)
    time.sleep(0.45)                        # 停一下：让控件进入悬停/激活态
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.06)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(settle)


def recognize(draft: Path, *, timeout: float = 240.0) -> list[tuple[float, float, str]]:
    """在已打开的草稿里跑「文本 → 识别歌词 → 开始识别」，返回识别出的字幕。"""
    hwnd = jy.find_window()
    if not hwnd:
        raise RuntimeError("剪映没在运行")
    # maximize 之后**必须重新取句柄**：剪映从首页进入编辑器时窗口会被重建，
    # 旧句柄随即失效（实测 6425002 → 11535444），继续用它会在 post_click 里抛
    # `pywintypes.error (1400, 'GetWindowRect', '无效的窗口句柄')`（2026-09-16 实跑踩到）。
    jy.maximize(hwnd)                      # 固定布局，坐标才有效
    time.sleep(1.5)
    hwnd = jy.find_window() or hwnd
    wx, wy, _, _ = jy.window_rect(hwnd)

    _real_click(hwnd, wx + TEXT_TAB[0], wy + TEXT_TAB[1])
    _real_click(hwnd, wx + RECOGNIZE_ITEM[0], wy + RECOGNIZE_ITEM[1])
    before = _draft_texts(draft)
    _real_click(hwnd, wx + START_BUTTON[0], wy + START_BUTTON[1])

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        rows = _draft_texts(draft)
        if rows and len(rows) >= len(before):
            return rows
        # 剪映被关掉（用户手滑关了 / 崩了 / 被「重新开始」踢掉）就立刻失败：
        # 否则这里会空等满 240 秒，而任务还占着 pipeline_lock，「重新开始」也进不来。
        if not jy.has_window():
            raise RuntimeError("剪映在识别过程中被关闭了：草稿里没有出现任何字幕行")
    raise RuntimeError("剪映识别歌词超时（草稿里没有出现字幕）")


def kill_app() -> None:
    """**强杀**剪映（不优雅退出），并确认它真的没了。

    用途：任务卡在剪映的 UIA/Win32 调用里时（实测出现过「识别中途剪映消失、轮询空等到超时」），
    只有让它消失才能让那个调用立刻返回、把 pipeline_lock 让出来 —— 「重新开始」按钮走的就是这条路。
    正常收尾仍然用 `close_app()`（先礼后兵）。
    """
    for image in ("JianyingPro.exe", "解锁版.exe"):
        subprocess.run(["taskkill", "/F", "/IM", image], capture_output=True)
    deadline = time.time() + 15
    while time.time() < deadline and jy.has_window():
        time.sleep(0.5)


def close_app(*, timeout: float = 30.0) -> bool:
    """识别完就关掉剪映（用户 2026-09-16：「完成后需要 关闭 剪映」）。

    为什么必须关：批量会跑很久，剪映常驻既没必要，又会和 ComfyUI 抢显存
    （实测 H3 采样时显存只剩 ~400MB）。先礼后兵：先发 WM_CLOSE 让它自己存盘退出
    （编辑器窗口可能只关草稿、退回首页，所以要反复发），再用 taskkill 兜底。
    """
    import win32con
    import win32gui

    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = jy.find_window(restore=False)
        if not hwnd:
            return True
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
        time.sleep(2.5)
    subprocess.run(["taskkill", "/F", "/IM", "JianyingPro.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "解锁版.exe"], capture_output=True)
    time.sleep(2.0)
    return jy.find_window(restore=False) is None


def _newest_draft_dir() -> Path:
    """当前最新改动的那条草稿目录（刚「开始创作」出来的新工程）。"""
    dirs = [d for d in DRAFT_ROOT.iterdir()
            if d.is_dir() and not d.name.startswith(".") and (d / "draft_content.json").is_file()]
    if not dirs:
        raise RuntimeError(f"剪映草稿目录里没有任何草稿：{DRAFT_ROOT}")
    return max(dirs, key=lambda d: (d / "draft_content.json").stat().st_mtime)


def _click_window_rel(hwnd: int, x: int, y: int, *, settle: float = 1.5) -> None:
    """真实鼠标点窗口内坐标（先强切前台并校验）。"""
    if not jy.force_foreground_hard(hwnd):
        raise RuntimeError("剪映抢不到前台，拒绝点击（否则会点到别的窗口）")
    wx, wy, _, _ = jy.window_rect(hwnd)
    jy.click_px(hwnd, x, y, settle=settle)      # 内部 SetCursorPos + 真实按下抬起
    _ = (wx, wy)


def start_new_project_and_import(video: Path, *, song_name: str | None = None) -> Path:
    """**每次都是新的开始创作**（用户 2026-09-16：「每次都是新的开始创作 你为什么一定要点击草稿呢」）。

    流程完全照用户手工那一套（坐标取自他亲自演示的全局录制 `data/_recording.jsonl`）：
      ① 首页点「开始创作」（UIA `HomePageStartProjectDesp`，真实点击）
      ② 素材面板「导入」窗口内 (429,431)
      ③ 文件框里点目标缩略图 → 点「打开」
      ④ 素材库「导入」按钮窗口内 (289,332)：把素材放到时间轴
    返回新草稿目录。**不克隆模板、不点草稿列表、不删任何草稿。**
    """
    hwnd = jy.find_window()
    if not hwnd:
        raise RuntimeError("剪映没在运行")
    if _in_editor():
        # 已经在编辑器里：先回首页（用户流程从首页「开始创作」起）
        u.ensure_app_accessibility(restart=True)
        time.sleep(2.0)
        hwnd = jy.find_window()

    with u.uia_in_thread():
        # ① 开始创作
        ctrl = u.find("HomePageStartProjectDesp", exact=True, timeout=20)
        if not ctrl:
            raise RuntimeError("首页找不到「开始创作」按钮")
        r = ctrl.BoundingRectangle
        jy.force_foreground_hard(hwnd)
        jy.click_px(hwnd, (r.left + r.right) // 2 - jy.window_rect(hwnd)[0],
                    (r.top + r.bottom) // 2 - jy.window_rect(hwnd)[1], settle=4.5)

    hwnd = jy.find_window()
    if not hwnd or not _in_editor():
        raise RuntimeError("点了「开始创作」但没进编辑页")
    time.sleep(2.0)

    # ② 素材面板「导入」
    _click_window_rel(hwnd, 429, 431, settle=3.0)
    # ③ 文件框：点缩略图 → 打开
    _pick_file_in_dialog(video)
    # ④ 素材库「导入」按钮（上轨）
    hwnd = jy.find_window()
    _click_window_rel(hwnd, 289, 332, settle=3.0)

    draft = _newest_draft_dir()
    print(f"新工程草稿：{draft.name}")
    # **校验导入的到底是哪个文件**：每个发布目录里都有同名的 `最终成片.mp4`，
    # 光看文件名分不出来，必须比对草稿里记录的完整路径（2026-09-16 用户实测反馈）。
    want = str(video.resolve()).replace("\\", "/").lower()
    deadline = time.time() + 25
    got = ""
    while time.time() < deadline:
        try:
            content = json.loads((draft / "draft_content.json").read_text(encoding="utf-8"))
        except Exception:
            content = {}
        videos = (content.get("materials") or {}).get("videos") or []
        got = str((videos[0] or {}).get("path") or "") if videos else ""
        if got:
            break
        time.sleep(1.5)
    got_norm = got.replace("\\", "/").lower()
    if got_norm and got_norm != want:
        raise RuntimeError(f"导入的视频不是目标文件：\n  期望 {want}\n  实际 {got_norm}")
    print(f"   已确认导入：{got or '（草稿暂时还没记录路径）'}", flush=True)
    return draft


def _pick_file_in_dialog(video: Path, *, timeout: float = 40.0) -> None:
    """在「请选择媒体资源」里选中目标文件。

    **用完整路径填文件名框再点「打开」**（2026-09-16 用户实测反馈）：对话框会记住上次访问的目录，
    点缩略图的做法容易点到别的目录里的同名文件（`最终成片.mp4` 每个发布目录里都有！）；
    填完整路径则与当前目录无关，打开的一定是这一个文件。
    """
    import uiautomation as auto
    import win32gui

    dlg = None
    deadline = time.time() + timeout
    while time.time() < deadline and dlg is None:
        dlg = next((w["hwnd"] for w in jy.list_windows() if w["title"] == "请选择媒体资源"), None)
        time.sleep(0.5)
    if dlg is None:
        raise RuntimeError("等不到「请选择媒体资源」文件框")
    win32gui.SetForegroundWindow(dlg)
    time.sleep(0.7)

    edit = open_btn = None
    with auto.UIAutomationInitializerInThread():
        root = auto.ControlFromHandle(dlg)
        stack = [(root, 0)]
        while stack:
            c, d = stack.pop()
            try:
                t = c.ControlTypeName
                nm = c.Name or ""
                r = c.BoundingRectangle
                box = (r.left, r.top, r.right, r.bottom)
            except Exception:
                continue
            if t == "EditControl" and nm.startswith("文件名"):
                edit = c
            # Windows 文件框的按钮名并不稳定：不同系统/主题下见过
            # 「打开(O)」「打开(&O)」「打开」「Open」。它只作为最后的鼠标兜底；
            # 主路径是在文件名框写入完整路径后直接按 Enter。
            normalized_name = nm.replace("&", "").replace("(", "").replace(")", "").strip().lower()
            if t == "ButtonControl" and (
                normalized_name.startswith("打开") or normalized_name in {"open", "openo"}
            ):
                open_btn = box
            if d < 10:
                try:
                    for ch in c.GetChildren():
                        stack.append((ch, d + 1))
                except Exception:
                    pass
        if edit is None:
            raise RuntimeError("文件框里找不到「文件名」输入框")
        full = str(video.resolve())
        print(f"     文件名框 ← {full}", flush=True)
        try:
            edit.GetValuePattern().SetValue(full)
        except Exception:
            # 退路：双击框内后走剪贴板粘贴
            rect = edit.BoundingRectangle
            jy.click_px(dlg, (rect.left + rect.right) // 2 - win32gui.GetWindowRect(dlg)[0],
                        (rect.top + rect.bottom) // 2 - win32gui.GetWindowRect(dlg)[1], settle=0.8)
            from pywinauto.keyboard import send_keys

            send_keys("^a", pause=0.05)
            send_keys("{DELETE}", pause=0.05)
            jy.set_clipboard_text(full)
            send_keys("^v", pause=0.08)
            time.sleep(0.4)
        print("     框内现在 =", repr(edit.GetValuePattern().Value), flush=True)

    def dialog_is_open() -> bool:
        try:
            return bool(win32gui.IsWindow(dlg) and win32gui.IsWindowVisible(dlg))
        except Exception:
            return False

    def wait_closed(seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if not dialog_is_open():
                return True
            time.sleep(0.2)
        return not dialog_is_open()

    # 文件名框已经是完整路径，标准文件框直接 Enter 最稳定，也不依赖按钮的语言、
    # Accessible Name 或层级。此次线上故障正是路径已写好、但按钮名称没被枚举到。
    from pywinauto.keyboard import send_keys

    try:
        win32gui.SetForegroundWindow(dlg)
    except Exception:
        pass
    send_keys("{ENTER}", pause=0.08)
    if wait_closed(4.0):
        return

    # 某些文件框把 Enter 留给地址栏/搜索框；Alt+O 是 Windows「打开」的标准助记键。
    send_keys("%o", pause=0.08)
    if wait_closed(3.0):
        return

    # 最后一层才使用枚举到的按钮坐标。没有按钮也不立即报错，因为前两条路径已经试过。
    if open_btn is not None:
        rect = win32gui.GetWindowRect(dlg)
        jy.click_px(dlg, (open_btn[0] + open_btn[2]) // 2 - rect[0],
                    (open_btn[1] + open_btn[3]) // 2 - rect[1], settle=2.0)
        if wait_closed(3.0):
            return

    try:
        current = edit.GetValuePattern().Value
    except Exception:
        current = "（无法读取）"
    raise RuntimeError(
        "文件路径已经写入，但文件框没有确认打开；"
        f"已依次尝试 Enter、Alt+O 和打开按钮。框内当前值：{current!r}"
    )


def recognize_video(video: Path, *, name: str | None = None, close_after: bool | None = None
                    ) -> list[tuple[float, float, str]]:
    """一条视频走完整流程，**每次都从「开始创作」新建工程**，不再克隆/打开草稿。

    `close_after`：识别完是否关掉剪映。默认读 `H3_KEEP_JIANYING`
    （=1/true/yes 则**保留剪映不关**，用户要求「自动运行的时候剪映不要关掉」）。
    """
    if close_after is None:
        keep = os.getenv("H3_KEEP_JIANYING", "").strip().lower() in ("1", "true", "yes", "on")
        close_after = not keep
    try:
        with u.uia_in_thread():
            u.ensure_app_accessibility()
        time.sleep(1.5)
        draft = start_new_project_and_import(video)
        with u.uia_in_thread():
            return recognize(draft)
    finally:
        if close_after:
            with u.uia_in_thread():
                if close_app():
                    print("剪映已关闭")
                else:
                    print("⚠ 剪映没能关掉，请手动确认")
