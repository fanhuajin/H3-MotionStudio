"""backend.publish_bilibili — 把一条发布成品发到 B站（**停在「立即投稿」前，由用户自己点**）。

用户口径（2026-09-29）：
  · 「必须鼠标真实操作 不然会被小红书封号」→ 全程走 `publish_desktop` 的 OS 级真实输入；
  · 「先试一下 B站 发布现在先由我来点击」→ 本模块**永远不点发布按钮**，
    到最后一步停下来，打印清单让用户核对后自己点。

发布物料（发布成品目录里）：
  · 视频：`最终成片_字幕版.mp4`（比音色版新）→ `最终成片_音色版.mp4` → `最终成片.mp4`
  · 文案：`发布文案.txt`（标题 / 简介 / 标签 三段，**utf-8-sig，带 BOM**）
  · 封面：`B站4x3.png`（1448×1086）

CLI：
    python -m backend.publish_desktop bilibili --folder "<发布成品目录>"
    python -m backend.publish_desktop bilibili --folder "..." --stop-after upload   # 只上传+抓表单
    python -m backend.publish_desktop bilibili --folder "..." --dry-run             # 只定位不动手
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from backend import publish_desktop as pd

logger = logging.getLogger("publish.bilibili")

UPLOAD_URL = "https://member.bilibili.com/platform/upload/video/frame"
WINDOW_KEYS = ("哔哩哔哩",)
TAB_KEYS = ("哔哩哔哩", "创作中心")

VIDEO_CANDIDATES = ("最终成片_字幕版.mp4", "最终成片_音色版.mp4", "最终成片.mp4")
COVER_CANDIDATES = ("B站4x3.png", "封面_B站4x3.png")


# ---------------------------------------------------------------------------
# 物料
# ---------------------------------------------------------------------------
def read_copy(folder: Path) -> dict[str, str]:
    """读 `发布文案.txt`：标题 / 简介 / 标签。

    ⚠️ 必须用 `utf-8-sig`：`batch_worker` 是用 `encoding="utf-8-sig"` 写这份文件的（带 BOM），
    用 `utf-8` 读的话第一行会变成 `"\\ufeff标题："`，`startswith("标题")` 不成立 →
    **整段标题被静默丢掉**（2026-09-29 实测踩到，差点发出一条没有标题的稿件）。
    """
    text = (folder / "发布文案.txt").read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    out = {"title": "", "intro": "", "tags": ""}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        matched = None
        for key, field in (("标题", "title"), ("简介", "intro"), ("标签", "tags")):
            if line.startswith(key):
                matched = field
                line = re.sub(rf"^{key}[：:]\s*", "", line)
                break
        if matched is not None:
            current = matched
            if line:
                out[current] = line
            continue
        if current:
            out[current] = f"{out[current]} {line}".strip() if out[current] else line
    return out


def tag_list(raw: str) -> list[str]:
    """标签串 → B站标签数组（去掉 # 与分隔符）。"""
    parts = re.split(r"[#\s,，、;；/]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


def pick_video(folder: Path) -> Path | None:
    for name in VIDEO_CANDIDATES:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def pick_cover(folder: Path) -> Path | None:
    for name in COVER_CANDIDATES:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def materials(folder: Path) -> dict:
    copy = read_copy(folder)
    return {"folder": folder, "video": pick_video(folder), "cover": pick_cover(folder),
            "title": copy["title"], "intro": copy["intro"], "tags": tag_list(copy["tags"])}


# ---------------------------------------------------------------------------
# 窗口 / 标签页
# ---------------------------------------------------------------------------
def activate_bili_tab(session: pd.PublishSession) -> bool:
    """在标签栏里找已有的 B站 标签并**真实点开它**，并**校验真的切过去了**。

    为什么必须这样：脚本不能依赖「当前激活的标签页是 B站」—— 用户随时可能切回来读消息
    （实测就是这样：一转眼活动标签就变成了 DSH 页面），那样窗口标题里没有「哔哩哔哩」，
    照标题匹配就会误判成「没有 B站 标签页」而反复新开标签。

    ⚠️ 也不能只看「点了标签」就返回成功：`dry_run` 下点击是空操作，必须回读窗口标题确认。
    """
    for n in session.snapshot_window():  # 标签栏属于浏览器外壳，要走整窗口树
        if n.ctype != "TabItemControl" or not n.visible:
            continue
        if not any(k in n.name for k in TAB_KEYS):
            continue
        if n.rect[2] - n.rect[0] < 40:
            continue
        session.click(n, f"切到 B站 标签：{n.name[:30]}")
        if session.dry_run:
            return True
        for _ in range(10):
            win = pd.find_browser(WINDOW_KEYS)
            if win is not None and win.hwnd == session.window.hwnd:
                time.sleep(1.5)
                return True
            time.sleep(0.5)
        logger.warning("点了标签但标题里没出现「哔哩哔哩」，继续找下一个候选")
    return False


def has_upload_button(session: pd.PublishSession, *, timeout: float = 6.0) -> bool:
    return session.find(name="上传视频", timeout=timeout) is not None


def goto_upload_page(session: pd.PublishSession) -> None:
    """页面不在投稿页时，用真实按键在地址栏输入地址（点地址栏 → 全选 → 输入 → 回车）。"""
    bar = None
    for n in session.snapshot_window():  # 地址栏也是浏览器外壳
        if n.ctype == "EditControl" and "地址" in n.name:
            bar = n
            break
    if bar is not None:
        session.click(bar, "地址栏")
    else:  # 退路：按窗口比例点地址栏
        left, top, right, _ = session.window.rect
        session.click_xy((left + right) // 2, top + 88, "地址栏（按比例）")
    session.press("^a", "全选地址", settle=0.3)
    session.type_text(UPLOAD_URL, "投稿页地址", settle=0.4)
    session.press("{ENTER}", "打开投稿页", settle=4.0)
    time.sleep(3.0)


def resolve_session(*, log_dir: Path, dry_run: bool) -> pd.PublishSession:
    """拿到一个「**活动标签确实是 B站 投稿页**」的会话。

    ⚠️ 不能假设「用 msedge.exe 打开地址 = 那个标签就在前台」：实测新标签会在**后台**打开，
    活动标签仍是用户原来那页 —— 所以每一步都要回读窗口标题复验，没到位就再想办法。
    """
    wanted = Path(log_dir)
    wins = pd.list_browsers()
    if not wins:
        pd.open_url(UPLOAD_URL)
        time.sleep(5)
        wins = pd.list_browsers()
    if not wins:
        raise RuntimeError("本机没有可见的 Edge/Chrome 窗口")

    def session_for(win: pd.BrowserWindow) -> pd.PublishSession:
        return pd.PublishSession(window=win, log_dir=wanted, dry_run=dry_run)

    # ① 某个窗口的活动标签就已经是 B站
    for win in wins:
        if "哔哩哔哩" in win.title:
            s = session_for(win)
            s.ensure_fully_visible()
            return s

    # ② 某个窗口的标签栏里躺着 B站 标签 → 真实点开它
    for win in wins:
        s = session_for(win)
        s.ensure_fully_visible()
        if activate_bili_tab(s):
            return s

    # ③ 都没有 → 开一个新标签，再回标签栏把它点开（因为新标签可能是后台打开的）
    s = session_for(wins[0])
    s.ensure_fully_visible()
    logger.info("没有现成的 B站 标签页，新开一个：%s", UPLOAD_URL)
    pd.open_url(UPLOAD_URL)
    for _ in range(3):
        time.sleep(4)
        if s.title_has("哔哩哔哩"):
            return s
        if activate_bili_tab(s):
            return s
    raise RuntimeError("打开了投稿页但没能把它切到前台（活动标签始终不是 B站）")


# ---------------------------------------------------------------------------
# 步骤
# ---------------------------------------------------------------------------
def click_upload_button(session: pd.PublishSession, timeout: float = 20.0) -> pd.Node:
    """点蓝色「上传视频」按钮（它触发隐藏的 file input → 原生「打开」对话框）。

    ⚠️ B站 把按钮文字暴露成 TextControl（不是 ButtonControl），两种都认。
    """
    node = None
    for want in ("ButtonControl", "TextControl"):
        node = session.find(name="上传视频", ctype=want, timeout=timeout if node is None else 0.5)
        if node is not None:
            break
    if node is None:
        raise RuntimeError("投稿页上没找到「上传视频」按钮（页面没加载完？）")
    session.click(node, "上传视频（触发文件对话框）")
    return node


def upload_video(session: pd.PublishSession, video: Path, *, run_dir: Path) -> bool:
    """点上传 → 处理原生对话框，把成片喂进去。失败会刷新页面重试一次。

    为什么要重试：Chromium 的文件选择器一旦被**非正常关掉**（比如上一次用 ESC 关的，
    而那个对话框其实在独立进程里，取消事件没传回渲染进程），页面就会卡在
    「file chooser 还挂着」的状态，后续 `input.click()` 一律被忽略 ——
    表现就是「点了上传按钮，什么都不弹」。刷新页面能把这个状态清掉。
    """
    # 屏幕上已经有浏览器弹的对话框（上次残留）→ 直接用它，别再点一次
    stale = {h: r for h, r in pd.file_dialogs().items()
             if r["proc"].lower() in pd.BROWSER_PROCS}
    if stale:
        logger.info("发现已开着的文件对话框 %s，直接填路径", list(stale))
        if pd.pick_file_in_dialog(video, timeout=3.0, only_procs=pd.BROWSER_PROCS):
            return True

    for attempt in (1, 2):
        before = set(pd.file_dialogs())
        click_upload_button(session)
        if pd.pick_file_in_dialog(video, timeout=25.0, before=before):
            return True
        logger.warning("第 %s 次点「上传视频」后没弹出文件对话框，刷新页面重试", attempt)
        session.press("{F5}", "刷新投稿页", settle=5.0)
        time.sleep(4)
        session.shot(f"retry{attempt}_reloaded")
    return False


def wait_form(session: pd.PublishSession, *, timeout: float = 900.0) -> bool:
    """等上传/转码完成、切到投稿表单（出现标题输入框）。"""
    started = time.time()
    deadline = started + timeout
    last_note = 0.0
    while time.time() < deadline:
        session._check_abort()
        for n in session.snapshot():
            if not n.visible:
                continue
            if n.ctype == "EditControl" and ("请输入标题" in n.name or n.name.strip() == "标题"):
                return True
        now = time.time()
        if now - last_note >= 30:  # 只报进度，不每秒截图（截图会拖慢整条链路）
            last_note = now
            logger.info("等上传/转码完成… 已 %ss", int(now - started))
            session.shot("waiting")
        time.sleep(2.0)
    return False


def dump_form(session: pd.PublishSession, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(session.map(limit=1500, only_named=True), ensure_ascii=False, indent=2),
                   encoding="utf-8")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def publish_folder(folder: Path, *, log_dir: Path, dry_run: bool = False,
                   stop_after: str = "form", skip_upload: bool = False) -> int:
    """把一条发布成品发到 B站，**停在「立即投稿」前**。

    `stop_after`：`upload` = 上传完抓一份表单控件清单就停（标定用）；`form` = 填完表停在投稿前。
    """
    folder = Path(folder)
    if not folder.is_dir():
        print("目录不存在：", folder)
        return 2
    data = materials(folder)
    print("=" * 72)
    print("发布目录：", folder)
    print("视频    ：", data["video"].name if data["video"] else "⚠️ 找不到成片！")
    print("封面    ：", data["cover"].name if data["cover"] else "⚠️ 没有 B站4x3.png")
    print("标题    ：", data["title"] or "⚠️ 空（发布文案.txt 解析失败？）")
    print("简介    ：", data["intro"][:60])
    print("标签    ：", " / ".join(data["tags"]))
    print("=" * 72)
    if data["video"] is None or not data["title"]:
        print("物料不全，退出。")
        return 2

    run_dir = Path(log_dir) / f"bilibili_{folder.name[:40]}_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.info("本次留证目录：%s", run_dir)

    from backend.jianying_uia import uia_in_thread

    with uia_in_thread():
        session = resolve_session(log_dir=run_dir, dry_run=dry_run)
        win = session.window
        session.step("00_attach")
        print(f"已挂上窗口 hwnd={win.hwnd} rect={win.rect}  「{session.live_title()[:60]}」")

        # 硬校验：活动标签**必须**是 B站，否则宁可停下 —— 不校验的话，
        # 名字相同的文字（比如聊天里出现的「上传视频」）会被当成按钮点掉。
        if not session.title_has("哔哩哔哩"):
            session.step("01_wrong_tab")
            print("⚠️ 当前激活标签不是 B站 页面，已停下（不做任何输入）。")
            print("   当前标题：", session.live_title()[:90])
            return 6

        if not has_upload_button(session, timeout=8.0):
            if dry_run:
                session.step("01_dry_run_no_upload_button")
                print("（dry-run：页面上暂时没看到「上传视频」按钮，不做任何输入）")
                return 0
            print("当前标签页不在投稿页，用地址栏导航过去…")
            goto_upload_page(session)
            session.step("01_navigated")
            if not has_upload_button(session, timeout=15.0):
                print("⚠️ 导航后仍然没有「上传视频」按钮，停下。留证：", run_dir)
                return 3

        if skip_upload:
            session.step("02_upload_skipped")
            print("按 --skip-upload 跳过上传（假定页面已在投稿表单）")
        else:
            picked = upload_video(session, data["video"], run_dir=run_dir)
            session.step("03_file_picked" if picked else "03_dialog_failed")
            if not picked:
                print("⚠️ 原生「打开」对话框没出现 / 没填进路径 —— 已停下，未做其它输入。")
                print("   若对话框正开在屏幕上，可手动补一步：")
                print(f"   python -m backend.publish_desktop fill-dialog --path \"{data['video']}\"")
                print("   留证：", run_dir)
                return 4
            print("已选择成片，等待上传与转码…")

        if not wait_form(session, timeout=900.0):
            session.step("04_form_timeout")
            print("⚠️ 等投稿表单超时。留证：", run_dir)
            return 5
        session.step("04_form_ready")
        print("投稿表单已出现。")

        if stop_after == "upload":
            out = run_dir / "form_map.json"
            dump_form(session, out)
            print("已停在「表单标定」这一步（未做任何填写）。控件清单：", out)
            return 0

        print("表单填写尚未标定，先停在这里。留证：", run_dir)
        return 0
