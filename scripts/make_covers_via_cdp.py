"""封面自动生成（CDP 静默链路）—— 一条命令把发布目录里的封面补齐。

## 日常怎么用

先把作品素材放进发布目录（`人物图.png` + `发布文案.txt` 齐了就行），然后：

    cd D:\\project\\H3-MotionStudio
    .venv\\Scripts\\python.exe scripts\\make_covers_via_cdp.py            # 补齐所有缺的封面
    .venv\\Scripts\\python.exe scripts\\make_covers_via_cdp.py --plan     # 只看会做什么，不动手
    .venv\\Scripts\\python.exe scripts\\make_covers_via_cdp.py --folders 007 008
    .venv\\Scripts\\python.exe scripts\\make_covers_via_cdp.py --folders 007 --force   # 重做已有封面
    .venv\\Scripts\\python.exe scripts\\make_covers_via_cdp.py --check    # 只验证链路通不通

产出（落在各自发布目录里，与 001-006 命名一致）：
    B站4x3.png   1448x1086
    抖音3x4.png  1086x1448

**已有封面会自动跳过**（按名字宽松匹配，含手工改名的变体），所以重复跑很安全：
只会补缺的，不会重复出图。

## 它为什么可靠（相比坐标点击版）

走 `backend/chatgpt_cdp.py`：Playwright 连**专用实例**的调试端口（9444），直接操作 DOM ——
新对话 / 清空输入框 / 附图 / 发消息 / 等新图 / canvas 导出 blob 取图全部在代码里完成。
不抢前台、不动鼠标键盘，窗口可以最小化着跑；也没有「找发送按钮、关预览、另存为」这些环节。
专用实例用的是独立 profile（`~/.codex-automation/profile`），与用户桌面开着的窗口互不影响。

工作方式（用户 2026-09-16 确认）：**一个作品 = 一个对话**，先发「B站 4:3」，
**这张落盘之后**才在**同一对话**里续发「抖音 3:4」（复用上下文里的图，不重传）。
提示词就是用户那两句原话 + 标题/简介，不加任何补充要求。

## 2026-09-17 适配新版桌面端

桌面端换成新外壳（26.908.9136.0）之后，挂图与取图都变了，这里是配套改动：

  - **挂图**：新版没有 `input[type="file"]`，「+ → 文件和文件夹」是原生对话框（CDP 打不到），
    所以改走 **CDP 真实拖放**（`Input.dispatchDragEvent` + `files`），完全静默；
    老外壳仍然优先用 file input（见 `backend.chatgpt_cdp.attach_images`）。
  - **取图**：新版把出图写到 `~/.codex/generated_images/<线程>/`（页面里是 `app://fs/...`），
    所以落盘目录与页面 blob **两条路一起等**。
  - **发送校验**：只有「输入框正文与附件都空了」才算发出去；上一轮还在生成时先等它结束
    （旧实现把「停止生成」按钮误当成功信号，于是消息留在输入框里永远不发 —— 用户看到的
    「到抖音图片了就不发送了」）。
  - **时间放宽**（用户 2026-09-17：「发送和等待生成图片的时间可以久一点无所谓」）：
    单张默认等 1200 秒（`--timeout`），发送重试 300 秒，附件 600 秒；失败**自动重试一次**。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image  # noqa: E402

from backend import chatgpt_cdp as cdp  # noqa: E402
from scripts.cover_common import (  # noqa: E402
    PUBLISH_ROOT, SPECS, build_prompt, collect_folders, existing_cover,
)

logger = logging.getLogger("covers_cdp")


def plan(folders_filter: list[str], only: str | None, *, as_json: bool = False) -> int:
    """打印计划：哪些已有、哪些要生成、每条的标题。

    `as_json=True` 时只输出一行 JSON（给启动器判断「这次到底要不要跑」用）。
    **不让启动器去匹配中文文案**：文案一改，判断就悄悄失效了 —— 机器可读的口径要单列。
    """
    folders = collect_folders(PUBLISH_ROOT, folders_filter)
    specs = [only] if only else ["bili", "douyin"]

    rows = []
    pending: list[str] = []
    for folder in folders:
        items = {}
        for spec in specs:
            found = existing_cover(folder.path, spec)
            items[spec] = found.name if found else None
            if not found:
                pending.append(f"{folder.name} / {SPECS[spec][1]}")
        rows.append({
            "folder": folder.name,
            "title": folder.title,
            "covers": items,
        })

    if as_json:
        print(json.dumps({
            "folders": len(folders),
            "pending": len(pending),
            "pendingItems": pending,
            "detail": rows,
        }, ensure_ascii=False))
        return 0

    if not folders:
        print("没有匹配的发布目录（检查发布根下是否有 人物图.png 与 发布文案.txt）")
        return 0
    for row in rows:
        parts = []
        for spec in specs:
            found = row["covers"][spec]
            parts.append(f"{SPECS[spec][0]} " + (f"已有（{found}）" if found else "待生成"))
        print(f"{row['folder']}\n    标题：{row['title']}\n    " + "；".join(parts))
    print(f"\n共 {len(folders)} 条作品，待生成 {len(pending)} 张")
    if pending:
        print("待生成清单：")
        for item in pending:
            print(f"  - {item}")
    return 0


def check() -> int:
    """连通性自检：起专用实例 → 连 CDP → 打开会话页（不发任何消息）。"""
    from playwright.sync_api import sync_playwright

    print("启动/连接专用实例……")
    endpoint = cdp.ensure_instance()
    print("endpoint:", endpoint, " 就绪:", cdp.cdp_ready())
    if not cdp.cdp_ready():
        print("CDP 未就绪")
        return 2
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        page = cdp._page(browser)
        print("页面标题:", page.title())
        print("找到输入框(ProseMirror):", page.locator("div.ProseMirror").first.count() > 0)
        print("会话里的图片数量:", len(cdp._list_blobs(page)))
        print("自检通过：CDP 链路可用（没有发送任何内容）")
    return 0


# 停止标记：启动器（run-covers.ps1）在窗口关闭/中断时写下它，
# 生成循环每张之前都检查一次 —— 光靠「关窗口杀进程」不够稳，Python 子进程有可能活下来继续跑。
STOP_FILE = PROJECT_ROOT / "data" / "cover-stop.flag"


def stop_requested() -> bool:
    return STOP_FILE.exists()


def clear_stop_flag() -> None:
    STOP_FILE.unlink(missing_ok=True)


def make_covers(folders_filter: list[str], *, only: str | None, force: bool,
                timeout: float, retries: int = 1) -> int:
    folders = collect_folders(PUBLISH_ROOT, folders_filter)
    if not folders:
        print("没有匹配的发布目录")
        return 0
    specs = [only] if only else ["bili", "douyin"]

    if stop_requested():
        print("检测到停止标记：上一次被中断过。清理后继续本次运行。")
        clear_stop_flag()

    cdp.ensure_instance()
    if not cdp.cdp_ready():
        print("CDP 未就绪：专用实例没起来（端口 9444）")
        return 2

    done = 0
    skipped_due_to_stop = False
    failed: list[str] = []
    for idx, folder in enumerate(folders, 1):
        if stop_requested():  # 每换一条作品都看一眼：窗口被关掉就立刻收手
            skipped_due_to_stop = True
            break
        print(f"\n===== [{idx}/{len(folders)}] {folder.name} =====", flush=True)
        # 这条作品要做的封面（已有的跳过；force 则全部重做）
        pending: list[str] = []
        for spec in specs:
            found = None if force else existing_cover(folder.path, spec)
            if found:
                print(f"  [已有] {SPECS[spec][0]}：{found.name}，不重复生成")
            else:
                pending.append(spec)
        if not pending:
            continue

        for i, spec in enumerate(pending):
            if stop_requested():  # 每张之前也检查：一张做到一半被中断就停在这里
                skipped_due_to_stop = True
                break
            platform, filename, want, tolerance, _ = SPECS[spec]
            target = folder.path / filename
            if target.exists() and not force:
                target.unlink()  # 去掉不合规的同名残留（比如半截图）
            prompt = build_prompt(folder.title, folder.intro, spec)
            first = i == 0
            print(f"  → {platform}：{'新对话 + 附图' if first else '同一对话续发'}", flush=True)
            got = None
            last_error = ""
            for attempt in range(max(1, retries + 1)):
                if attempt:
                    print(f"    ↻ 第 {attempt + 1} 次尝试（上一次：{last_error}）", flush=True)
                try:
                    got = cdp.generate(
                        [folder.portrait] if first else [],
                        prompt,
                        save_to=target,
                        timeout=timeout,
                        new_chat=first,   # 只有第一条开新对话，第二条在同一对话里续
                    )
                except Exception as exc:  # noqa: BLE001
                    got = None
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue
                if got and Path(got).is_file():
                    break
                last_error = "没取到图（发送失败或出图超时）"
            if not got or not Path(got).is_file():
                print(f"  ✗ {platform} 失败：{last_error}", flush=True)
                failed.append(f"{folder.name} / {filename}（{last_error}）")
                continue
            with Image.open(got) as im:
                size = im.size
            ok = abs(size[0] - want[0]) <= tolerance and abs(size[1] - want[1]) <= tolerance
            print(f"  {'✓' if ok else '⚠'} {platform}：{filename} {size[0]}x{size[1]}"
                  f"{'' if ok else f'   ← 期望 {want[0]}x{want[1]}'}", flush=True)
            done += 1

    print(f"\n本次生成 {done} 张")
    if skipped_due_to_stop:
        clear_stop_flag()
        print("收到停止请求（窗口被关闭）：已停在安全位置，没有继续生成。")
        print("已经出好的封面都已落盘，下次打开会自动接着补缺的。")
        return 3
    if failed:
        print("失败清单：")
        for item in failed:
            print("  -", item)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="封面自动生成（CDP 静默链路）：B站 4:3 + 抖音 3:4")
    parser.add_argument("--folders", nargs="*", default=[],
                        help="只处理目录名含这些片段的作品，如 007 008；不给则处理全部")
    parser.add_argument("--only", choices=sorted(SPECS), help="只做某一种封面")
    parser.add_argument("--force", action="store_true", help="已有的封面也重做")
    parser.add_argument("--timeout", type=float, default=1200.0,
                        help="单张等待上限（秒），默认 1200（用户 2026-09-17：久一点无所谓）")
    parser.add_argument("--retries", type=int, default=1,
                        help="单张失败后重试次数（默认 1 次；0 = 不重试）")
    parser.add_argument("--plan", action="store_true", help="只打印计划，不生成")
    parser.add_argument("--plan-json", action="store_true",
                        help="只输出一行 JSON 状态（启动器用来判断要不要跑）")
    parser.add_argument("--check", action="store_true", help="只做 CDP 连通性自检")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    if args.check:
        return check()
    if args.plan_json:
        return plan(args.folders, args.only, as_json=True)
    if args.plan:
        return plan(args.folders, args.only)
    return make_covers(args.folders, only=args.only, force=args.force,
                       timeout=args.timeout, retries=args.retries)


if __name__ == "__main__":
    sys.exit(main())
