"""automation.cover_flow — 封面生成：回到同一个 ChatGPT 聊天，先 B站4:3 后 抖音3:4，严格串行。

规则（用户定死）：
  - 回到「同一个」聊天（不新开）。
  - 上传 发布文案.txt + 人物图.png。
  - 先做 B站 4:3，完成后再做 抖音 3:4，不能并行。
顺序写在 config.json 的 cover_order，代码固定按序执行，不做成可并行。
"""
from __future__ import annotations

from pathlib import Path

from . import settings as _s
from .codex_automation import CodexSession

# 封面任务定义
COVER_TASKS = {
    "bilibili_4x3": "请根据这份发布文案和人物图，生成一张 B站 4:3 横版封面图。",
    "douyin_3x4": "请根据这份发布文案和人物图，生成一张 抖音 3:4 竖版封面图。",
}


def generate_covers(
    cfg: dict,
    *,
    publish_folder: str | Path,
    copy_file: str | Path,
    image_file: str | Path,
    session: CodexSession,
) -> dict[str, Path]:
    """在给定 session 的「当前聊天」里依次生成封面。返回 {任务key: 图路径}。

    session 必须已指向那条封面聊天（调用方负责把聊天切到封面会话）。
    """
    publish_folder = Path(publish_folder)
    out_dir = publish_folder
    results: dict[str, Path] = {}
    order = cfg.get("cover_order") or ["bilibili_4x3", "douyin_3x4"]

    session.upload_images([str(copy_file), str(image_file)])

    for key in order:
        prompt = COVER_TASKS.get(key, f"生成封面（{key}）")
        suffix = cfg.get("cover_43_suffix" if key == "bilibili_4x3" else "cover_916_suffix",
                         ".png")
        session.paste_prompt(prompt)
        session.wait_reply_done()
        imgs = session.collect_images(out_dir)
        if imgs:
            target = out_dir / f"封面{suffix}"
            # 取第一张（或最后一张生成图）作为该规格封面
            chosen = imgs[-1]
            if target.exists():
                target.unlink()
            chosen.replace(target)
            results[key] = target
            print(f"[cover] {key} → {target}")
        else:
            print(f"[cover] {key} 未取到图")
    return results
