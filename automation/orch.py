"""automation.orch — 主编排入口（CLI）。

用法示例：
  # 添加链接（唱歌/跳舞标注；无批次则新建，有则追加到同一批次）
  python -m automation.orch add --singing "https://v.douyin.com/xxx" "https://...yyy"
  python -m automation.orch add --dance  "https://..."
  python -m automation.orch add --mixed "唱歌: url1" "跳舞: url2" "唱歌: url3"

  # 查看批次状态
  python -m automation.orch status

  # 确认所有「待确认且已有候选图」的条目出片
  python -m automation.orch confirm

  # 桌面端 CDP 引导（Step3 验证：开端口并接管）
  python -m automation.orch cdp-boot

  # 对某个发布目录生成封面
  python -m automation.orch cover --folder "E:\\...\\发布成品\\004_xxx_123"

  # 监听发布成品目录
  python -m automation.orch watch
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import settings as _s
from .h3_client import H3Client, STATUS_AWAITING_REVIEW, STATUS_COMPLETED, STATUS_FAILED, \
    STATUS_SKIPPED, STATUS_DELETED

KIND_SINGING = "singing"
KIND_DANCE = "dance"


def parse_links(items: list[str]) -> tuple[list[str], list[str]]:
    """解析带标注的链接。

    支持：
      "唱歌: url" / "跳舞: url"（含中文冒号/半角）
      "singing: url" / "dance: url"
      裸 url（归到 --default-kind）
    返回 (singing_urls, dance_urls)。
    """
    singing, dance = [], []
    for raw in items:
        text = raw.strip()
        m = re.match(r"^(唱歌|跳舞|singing|dance|song|dance)\s*[:：]\s*(.+)$",
                     text, re.IGNORECASE)
        if m:
            kind, url = m.group(1).lower(), m.group(2).strip()
            if kind in {"唱歌", "singing", "song"}:
                singing.append(url)
            else:
                dance.append(url)
        else:
            raise ValueError(f"无法解析链接（缺少 唱歌/跳舞 标注）：{raw[:80]}")
    return singing, dance


def _client(cfg) -> H3Client:
    return H3Client(cfg["backend_url"])


def cmd_add(cfg, args):
    singing, dance = parse_links(args.mixed or [])
    singing += args.singing or []
    dance += args.dance or []
    if not singing and not dance:
        raise SystemExit("没有可添加的链接")
    c = _client(cfg)
    batch = c.latest_batch()
    if batch is None or batch.get("status") in {"cancelled", "completed"}:
        # 新建批次
        state = c.create_batch(singing, dance, auto_start=True)
        bid = state["id"]
        print(f"新建批次 {bid}，singing={len(singing)} dance={len(dance)}，已开跑")
    else:
        bid = batch["id"]
        state = c.append_items(bid, singing, dance, auto_start=True)
        print(f"追加到批次 {bid}，singing={len(singing)} dance={len(dance)}")
    print("批次状态:", state.get("status"))
    for it in state.get("items") or []:
        print(f"  [{it.get('status')}] {it.get('kind')} :: {it.get('title') or it.get('url')}")


def cmd_status(cfg, args):
    c = _client(cfg)
    batch = c.latest_batch()
    if batch is None:
        print("还没有批次")
        return
    print(f"批次 {batch['id']}  status={batch.get('status')}")
    for it in batch.get("items") or []:
        ai = it.get("ai") or {}
        title = ai.get("title") or it.get("title") or it.get("url") or ""
        has_img = bool(str(ai.get("reference_image_path") or "").strip())
        print(f"  [{it.get('status'):<14}] {it.get('kind'):<7} 图={'Y' if has_img else 'N'} :: {title}")


def cmd_confirm(cfg, args):
    c = _client(cfg)
    batch = c.latest_batch()
    if batch is None:
        print("还没有批次")
        return
    bid = batch["id"]
    awaiting = c.awaiting_review_items(bid)
    if not awaiting:
        print("没有待确认条目")
        return
    for it in awaiting:
        ai = it.get("ai") or {}
        has_img = bool(str(ai.get("reference_image_path") or "").strip())
        if has_img:
            c.confirm_item(bid, it["id"])
            print(f"  已确认出片: {it['id']} :: {ai.get('title')}")
        else:
            print(f"  跳过（缺候选图）: {it['id']} :: {ai.get('title')}")


def cmd_cdp_boot(cfg, args):
    from .codex_automation import launch_with_cdp
    endpoint = launch_with_cdp(cfg)
    print(f"桌面端 CDP 已就绪：{endpoint}")


def cmd_cover(cfg, args):
    from .codex_automation import CodexSession
    from .cover_flow import generate_covers
    folder = Path(args.folder)
    copy_file = folder / "发布文案.txt"
    image_file = next(folder.glob("人物图.*"), None)
    if not copy_file.is_file():
        raise SystemExit(f"缺少 发布文案.txt：{folder}")
    if not image_file:
        raise SystemExit(f"缺少 人物图：{folder}")
    with CodexSession(cfg) as session:
        session.new_chat()
        generate_covers(cfg, publish_folder=folder,
                        copy_file=copy_file, image_file=image_file,
                        session=session)


def cmd_watch(cfg, args):
    from .publish_watcher import watch

    def on_ready(folder: Path):
        print(f"[watch] 就绪：{folder}（人物图+发布文案齐了），可触发封面生成")
        # 若要自动生成封面，在此调用 cmd_cover 逻辑

    watch(cfg, on_ready)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="orch", description="H3 自动化流水线")
    parser.add_argument("--config", default=None, help="config.json 路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="添加链接（唱歌/跳舞标注）")
    p_add.add_argument("--singing", nargs="*", default=[])
    p_add.add_argument("--dance", nargs="*", default=[])
    p_add.add_argument("--mixed", nargs="*", default=[],
                       help="带标注的链接，如 '唱歌: url' '跳舞: url'")
    p_add.set_defaults(func=cmd_add)

    sub.add_parser("status", help="查看批次状态").set_defaults(func=cmd_status)
    sub.add_parser("confirm", help="确认待确认条目出片").set_defaults(func=cmd_confirm)
    sub.add_parser("cdp-boot", help="引导桌面端 CDP").set_defaults(func=cmd_cdp_boot)

    p_cover = sub.add_parser("cover", help="对发布目录生成封面")
    p_cover.add_argument("--folder", required=True)
    p_cover.set_defaults(func=cmd_cover)

    sub.add_parser("watch", help="监听发布目录").set_defaults(func=cmd_watch)

    args = parser.parse_args(argv)
    cfg = _s.load(args.config)
    args.func(cfg, args)


if __name__ == "__main__":
    main()
