from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend import chatgpt_cdp as cdp
from scripts.cover_common import PUBLISH_ROOT, SPECS, build_prompt, existing_cover, parse_copy

# 身份原型与规则提示词**按候选顺序找**，不再写死发布根：发布成品根上的
# `晚柠.png` / `4比3图片.txt` / `9比16图片.txt` 已于 2026-09-22 删除
# （`晚柠.png` 与「本人固定参考.png」是同一个文件、sha 相同；两份提示词与桌面那份
#  逐字节相同、仓库 `backend/prompts/` 里也有同口径的一份）。
# 顺序：桌面（用户手改的那份）→ 发布根（老位置还在就用它）→ 仓库规范位置。
def _first_existing(paths) -> Path:
    options = tuple(Path(p) for p in paths)
    for path in options:
        if path.is_file():
            return path
    return options[-1]


IDENTITY = _first_existing((
    Path(r"E:\AI_Assets\PortraitIdentity\本人固定参考.png"),
    PUBLISH_ROOT / "晚柠.png",
))
SING_PROMPT = _first_existing((
    Path.home() / "Desktop" / "4比3图片.txt",
    PUBLISH_ROOT / "4比3图片.txt",
    PROJECT_ROOT / "backend" / "prompts" / "4x3唱歌构图.txt",
))
DANCE_PROMPT = _first_existing((
    Path.home() / "Desktop" / "9比16图片.txt",
    PUBLISH_ROOT / "9比16图片.txt",
    PROJECT_ROOT / "backend" / "prompts" / "9x16跳舞构图.txt",
))
STOP_FILE = PROJECT_ROOT / "data" / "cover-stop.flag"


def stop_requested():
    return STOP_FILE.exists()


def clear_stop_flag():
    STOP_FILE.unlink(missing_ok=True)


def work_type(folder: Path):
    if "_唱歌_" in folder.name:
        return "唱歌"
    if "_跳舞_" in folder.name:
        return "跳舞"
    return None


def prompt_path_for(kind: str):
    return SING_PROMPT if kind == "唱歌" else DANCE_PROMPT


def collect(filters):
    rows = []
    if not PUBLISH_ROOT.is_dir():
        raise SystemExit(f"发布根目录不存在：{PUBLISH_ROOT}")
    for folder in sorted(p for p in PUBLISH_ROOT.iterdir() if p.is_dir()):
        if filters and not any(x in folder.name for x in filters):
            continue
        kind = work_type(folder)
        if not kind:
            continue
        frame = folder / "抽帧.jpg"
        copy_path = folder / "发布文案.txt"
        portrait = folder / "人物图.png"
        if not frame.is_file() or not copy_path.is_file():
            print(f"[跳过] {folder.name}：缺 抽帧.jpg 或 发布文案.txt")
            continue
        title, intro = parse_copy(copy_path)
        rows.append(dict(path=folder, name=folder.name, kind=kind, frame=frame, copy=copy_path,
                         portrait=portrait, title=title, intro=intro))
    return rows


def valid_portrait(path: Path):
    try:
        return path.is_file() and path.stat().st_size >= 50 * 1024
    except OSError:
        return False


def plan(filters, as_json=False):
    rows = collect(filters)
    detail = []
    pending_items = []
    for row in rows:
        portrait_ok = valid_portrait(row["portrait"])
        bili = existing_cover(row["path"], "bili")
        douyin = existing_cover(row["path"], "douyin")
        if not portrait_ok:
            pending_items.append(f'{row["name"]} / 人物图.png')
        if not bili:
            pending_items.append(f'{row["name"]} / B站4x3.png')
        if not douyin:
            pending_items.append(f'{row["name"]} / 抖音3x4.png')
        detail.append(dict(folder=row["name"], type=row["kind"], title=row["title"],
                           portrait=row["portrait"].name if portrait_ok else None,
                           bili=bili.name if bili else None,
                           douyin=douyin.name if douyin else None))
    if as_json:
        print(json.dumps(dict(folders=len(rows), pending=len(pending_items),
                              pendingItems=pending_items, detail=detail), ensure_ascii=False))
        return 0
    if not rows:
        print("没有匹配的可处理作品目录")
        return 0
    for d in detail:
        print(f'{d["folder"]}\n    类型：{d["type"]}\n    标题：{d["title"]}')
        print("    人物图：" + ("已有" if d["portrait"] else "待生成"))
        print("    B站4x3：" + ("已有" if d["bili"] else "待生成"))
        print("    抖音3x4：" + ("已有" if d["douyin"] else "待生成"))
    print(f"\n共 {len(rows)} 条作品，待生成 {len(pending_items)} 个文件")
    return 0


def generate_with_retry(images, prompt, target, timeout, new_chat, retries):
    last = ""
    for attempt in range(retries + 1):
        if attempt:
            print(f"    ↻ 第 {attempt + 1} 次尝试（上一次：{last}）", flush=True)
        try:
            got = cdp.generate(images, prompt, save_to=target, timeout=timeout, new_chat=new_chat)
        except Exception as exc:
            got = None
            last = f"{type(exc).__name__}: {exc}"
            continue
        if got and Path(got).is_file():
            return Path(got)
        last = "没取到图（发送失败或出图超时）"
    print(f"    ✗ 失败：{last}", flush=True)
    return None


def run(filters, force=False, timeout=1200.0, retries=1):
    rows = collect(filters)
    if not rows:
        print("没有匹配的可处理作品目录")
        return 0

    for required in (IDENTITY,):
        if not required.is_file():
            print(f"缺少必需文件：{required}")
            return 2

    if stop_requested():
        clear_stop_flag()

    cdp.ensure_instance()
    if not cdp.cdp_ready():
        print("CDP 未就绪：专用实例没起来（端口 9444）")
        return 2

    failed = []
    done = 0

    for idx, row in enumerate(rows, 1):
        if stop_requested():
            clear_stop_flag()
            print("收到停止请求，已停止。")
            return 3

        folder = row["path"]
        print(f'\n===== [{idx}/{len(rows)}] {row["name"]}（{row["kind"]}） =====', flush=True)

        if valid_portrait(row["portrait"]) and not force:
            print("  [已有] 人物图.png，不重复生成")
        else:
            from backend import batch_ai, batch_image
            try:
                scene = batch_image.prepare_defocused_reference(row["frame"], folder / ".h3-reference-guard")
            except (ValueError, OSError) as exc:
                failed.append(f'{row["name"]} / 人物图糊脸失败：{exc}')
                continue
            prompt = batch_ai.compose_image_prompt(row["kind"])
            print('  → 人物图：已糊脸抽帧=图一，固定原型=图二，使用项目当前提示词', flush=True)
            got = generate_with_retry([scene, IDENTITY], prompt, row["portrait"],
                                      timeout, True, retries)
            if not got:
                failed.append(f'{row["name"]} / 人物图.png')
                print("  ✗ 人物图失败，本作品不继续生成封面", flush=True)
                continue
            try:
                with Image.open(got) as im:
                    print(f"  ✓ 人物图.png {im.size[0]}x{im.size[1]}", flush=True)
            except Exception:
                print("  ✓ 人物图.png 已落盘", flush=True)
            done += 1

        if not valid_portrait(row["portrait"]):
            failed.append(f'{row["name"]} / 人物图.png（落盘校验失败）')
            continue

        pending = []
        for spec in ("bili", "douyin"):
            found = None if force else existing_cover(folder, spec)
            if found:
                print(f"  [已有] {SPECS[spec][0]}：{found.name}，不重复生成")
            else:
                pending.append(spec)

        first_cover = True
        for spec in pending:
            if stop_requested():
                clear_stop_flag()
                print("收到停止请求，已停止。")
                return 3
            platform, filename, want, tolerance, _ = SPECS[spec]
            target = folder / filename
            prompt = build_prompt(row["title"], row["intro"], spec)
            print(f"  → {platform}：{'新对话 + 人物图' if first_cover else '同一对话续发'}", flush=True)
            got = generate_with_retry([row["portrait"]] if first_cover else [], prompt, target,
                                      timeout, first_cover, retries)
            if not got:
                failed.append(f'{row["name"]} / {filename}')
                break
            with Image.open(got) as im:
                size = im.size
            ok = abs(size[0] - want[0]) <= tolerance and abs(size[1] - want[1]) <= tolerance
            print(f"  {'✓' if ok else '⚠'} {filename} {size[0]}x{size[1]}", flush=True)
            done += 1
            first_cover = False

    print(f"\n本次生成 {done} 个文件")
    if failed:
        print("失败清单：")
        for item in failed:
            print("  -", item)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="发布素材自动生成：人物图 -> B站4x3 -> 抖音3x4")
    parser.add_argument("--folders", nargs="*", default=[], help="只处理目录名含这些片段的作品，如 001 002")
    parser.add_argument("--force", action="store_true", help="已有文件也重做")
    parser.add_argument("--timeout", type=float, default=1200.0, help="单张等待上限秒数")
    parser.add_argument("--retries", type=int, default=1, help="单张失败后重试次数")
    parser.add_argument("--plan", action="store_true", help="只看计划，不生成")
    parser.add_argument("--plan-json", action="store_true", help="输出机器可读计划 JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    if args.plan_json:
        return plan(args.folders, as_json=True)
    if args.plan:
        return plan(args.folders)
    return run(args.folders, force=args.force, timeout=args.timeout, retries=args.retries)


if __name__ == "__main__":
    raise SystemExit(main())
