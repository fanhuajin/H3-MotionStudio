"""用网页 ChatGPT 补齐发布作品的图片资产。

一个作品依次处理：
  1. 读取发布目录里的抽帧，并从批量记录判断唱歌（4:3）或跳舞（9:16）；
  2. 将抽帧和固定人物参考图交给 ChatGPT，生成并校验 ``人物图.png``；
  3. 用人物图和 ``发布文案.txt`` 生成 B站、抖音封面，校验后立即保存。

它复用 ``backend.chatgpt_cdp`` 的专用 ChatGPT 桌面实例：不操作用户正在使用的窗口，
但首次运行时可能会出现一个专用 ChatGPT 窗口。默认只补缺，不会消耗已有合格图片的
生图次数；先用 ``--plan`` 查看计划。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend import chatgpt_cdp as cdp
from backend.batch_worker import IDENTITY_PATH
from backend.image_workbench import (
    COVER_NAMES,
    PERSON_NAME,
    TARGETS,
    asset_path,
    infer_kind,
    prompt,
    save_upload,
)
from scripts.cover_common import MIN_VALID_BYTES, PUBLISH_ROOT


STAGES = ("person", "bilibili", "douyin")


@dataclass(frozen=True)
class PublishWork:
    path: Path
    kind: str | None
    frame: Path | None

    @property
    def label(self) -> str:
        return self.path.name


def _image_is_valid(path: Path, *, ratio: float | None = None,
                    exact: tuple[int, int] | None = None) -> bool:
    """Check an existing asset without touching it.

    Existing files are never replaced merely because they have an unfamiliar name or a
    decoding problem.  A bad canonical output is reported by ``--plan`` and only replaced
    with ``--force``.
    """
    try:
        if not path.is_file() or path.stat().st_size < MIN_VALID_BYTES:
            return False
        with Image.open(path) as image:
            width, height = image.size
    except OSError:
        return False
    if exact:
        return (width, height) == exact
    if ratio:
        return abs((width / height) - ratio) / ratio <= 0.035
    return True


def _frame(folder: Path) -> Path | None:
    for name in ("抽帧.jpg", "抽帧.png", "scene-frame.jpg"):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def collect(filters: list[str]) -> list[PublishWork]:
    if not PUBLISH_ROOT.is_dir():
        raise SystemExit(f"发布根目录不存在：{PUBLISH_ROOT}")
    works: list[PublishWork] = []
    for folder in sorted(path for path in PUBLISH_ROOT.iterdir() if path.is_dir()):
        if filters and not any(value in folder.name for value in filters):
            continue
        works.append(PublishWork(folder, infer_kind(folder), _frame(folder)))
    return works


def pending(work: PublishWork, *, force: bool, stage: str | None = None) -> list[str]:
    requested = (stage,) if stage else STAGES
    result: list[str] = []
    person = work.path / PERSON_NAME
    person_ok = _image_is_valid(person, ratio=4 / 3 if work.kind == "singing" else 9 / 16)
    if "person" in requested and (force or not person_ok):
        result.append("person")
    # A newly requested person image means both covers must be remade; otherwise they would
    # describe a different face/background than the final portrait.
    remaking_person = "person" in result
    for key in ("bilibili", "douyin"):
        if key not in requested:
            continue
        cover_ok = _image_is_valid(work.path / COVER_NAMES[key], exact=TARGETS[key])
        if force or remaking_person or not cover_ok:
            result.append(key)
    return result


def explain_unready(work: PublishWork, stages: list[str]) -> str | None:
    if not work.kind:
        return "无法从批量任务识别作品类型（唱歌 / 跳舞）"
    if "person" in stages and not work.frame:
        return "缺少抽帧（需要 抽帧.jpg、抽帧.png 或 scene-frame.jpg）"
    if "person" in stages and not IDENTITY_PATH.is_file():
        return f"缺少固定人物参考图：{IDENTITY_PATH}"
    if any(item in stages for item in ("bilibili", "douyin")) and not (work.path / "发布文案.txt").is_file():
        return "缺少 发布文案.txt，人物图可以生成，但不能生成封面"
    return None


def plan(filters: list[str], *, force: bool, stage: str | None) -> int:
    works = collect(filters)
    total = 0
    for work in works:
        stages = pending(work, force=force, stage=stage)
        problem = explain_unready(work, stages)
        type_label = {"singing": "唱歌 4:3", "dance": "跳舞 9:16"}.get(work.kind, "类型未识别")
        if problem:
            print(f"[异常] {work.label} · {type_label}：{problem}")
        elif stages:
            names = {"person": "人物图", "bilibili": "B站封面", "douyin": "抖音封面"}
            print(f"[待生成] {work.label} · {type_label}：" + "、".join(names[item] for item in stages))
            total += len(stages)
        else:
            print(f"[已齐全] {work.label} · {type_label}")
    print(f"\n共 {len(works)} 条作品，待生成 {total} 张图片")
    return 0


def _generate(images: list[Path], message: str, target: Path, *, new_chat: bool,
              timeout: float) -> Path:
    result = cdp.generate(images, message, save_to=target, timeout=timeout, new_chat=new_chat)
    if not result or not Path(result).is_file():
        raise RuntimeError("网页 ChatGPT 没有返回可保存的图片")
    return Path(result)


def generate_work(work: PublishWork, stages: list[str], *, timeout: float) -> list[str]:
    """Generate one work serially and return its completed stage names."""
    assert work.kind in {"singing", "dance"}
    done: list[str] = []
    with tempfile.TemporaryDirectory(prefix="h3-publish-assets-") as raw:
        temp = Path(raw)
        if "person" in stages:
            assert work.frame is not None
            candidate = _generate(
                [work.frame, IDENTITY_PATH], prompt(work.path, "person", work.kind),
                temp / "人物图.png", new_chat=True, timeout=timeout,
            )
            save_upload(work.path, "person", candidate.read_bytes(), work.kind)
            done.append("person")
        if "bilibili" not in stages and "douyin" not in stages:
            return done
        person = asset_path(work.path, "person")
        # Covers form one conversation: B站 carries the source image; 抖音 follows in the
        # same conversation so ChatGPT preserves the newly approved character/background.
        if "bilibili" in stages:
            candidate = _generate(
                [person], prompt(work.path, "bilibili", work.kind), temp / "B站4x3.png",
                new_chat=True, timeout=timeout,
            )
            save_upload(work.path, "bilibili", candidate.read_bytes(), work.kind)
            done.append("bilibili")
        if "douyin" in stages:
            candidate = _generate(
                [] if "bilibili" in stages else [person],
                prompt(work.path, "douyin", work.kind), temp / "抖音3x4.png",
                new_chat="bilibili" not in stages, timeout=timeout,
            )
            save_upload(work.path, "douyin", candidate.read_bytes(), work.kind)
            done.append("douyin")
    return done


def run(filters: list[str], *, force: bool, stage: str | None, timeout: float) -> int:
    works = collect(filters)
    candidates = [(work, pending(work, force=force, stage=stage)) for work in works]
    candidates = [(work, stages) for work, stages in candidates if stages]
    if not candidates:
        print("没有需要补齐的图片。")
        return 0
    cdp.ensure_instance()
    if not cdp.cdp_ready():
        print("专用 ChatGPT 实例未就绪。")
        return 2
    completed = 0
    errors: list[str] = []
    for index, (work, stages) in enumerate(candidates, 1):
        print(f"\n===== [{index}/{len(candidates)}] {work.label} =====", flush=True)
        problem = explain_unready(work, stages)
        if problem:
            print(f"  ✗ {problem}", flush=True)
            errors.append(f"{work.label}：{problem}")
            continue
        try:
            done = generate_work(work, stages, timeout=timeout)
            completed += len(done)
            print("  ✓ 已保存：" + "、".join({
                "person": PERSON_NAME, "bilibili": COVER_NAMES["bilibili"],
                "douyin": COVER_NAMES["douyin"],
            }[item] for item in done), flush=True)
        except Exception as exc:  # keep the remaining works independent
            print(f"  ✗ 失败：{exc}", flush=True)
            errors.append(f"{work.label}：{exc}")
    print(f"\n本次新增 {completed} 张图片")
    if errors:
        print("异常：")
        for item in errors:
            print("  - " + item)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="网页 ChatGPT：人物图 → B站/抖音封面")
    parser.add_argument("--folders", nargs="*", default=[], help="只处理名称包含这些片段的作品")
    parser.add_argument("--stage", choices=STAGES, help="只补人物图或其中一种封面")
    parser.add_argument("--force", action="store_true", help="明确重做已存在的对应图片")
    parser.add_argument("--plan", action="store_true", help="只查看计划，不启动 ChatGPT、不生成图片")
    parser.add_argument("--timeout", type=float, default=1200.0, help="单张等待上限（秒）")
    args = parser.parse_args(argv)
    if args.plan:
        return plan(args.folders, force=args.force, stage=args.stage)
    return run(args.folders, force=args.force, stage=args.stage, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
