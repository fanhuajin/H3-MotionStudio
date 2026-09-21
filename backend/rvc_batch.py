"""一键批量转换音色（2026-09-20 step 5）：给还没有音色版的唱歌成片逐条跑 RVC。

用户定死的链条：

    最终成片.mp4（原声，批量出片交付的）
      → 最终成片_音色版.mp4（本模块产出，**原声并存**）
        → 最终成片_字幕版.mp4（字幕链路产出，发布版）

骨架与「一键批量加字幕」（`subtitle_step.run_publish_subtitle_batch`）完全一致：
**扫发布目录 → 排队 → 后台逐条串行 → 暂停在两条之间生效 → 单条失败只记日志不阻断**。
唯一的额外约束是它吃显卡：开跑前要**等出片空档**并关掉 ComfyUI（RVC 与 ComfyUI 不能
同时占显存），跑完让下一条出片自己把 ComfyUI 拉起来。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path
from typing import Any

from .batch_store import batch_store
from .pipeline import (
    comfy_health,
    finish_cancelled,
    is_job_cancelled,
    raise_if_cancelled,
    resources,
    run_rvc,
)
from .settings import BATCH_OUTPUT_ROOT, DATA_DIR, publish_subdir
from .store import now_iso, store

# 音色版与「无字幕母版」的文件名（都在各自的发布目录里）
CLEAN_NAME = "最终成片.mp4"
VOICE_NAME = "最终成片_音色版.mp4"

# 等出片空档的轮询间隔（有出片在跑就不抢显卡）
WAIT_INTERVAL_SECONDS = 10.0

BATCH_MILESTONES: list[dict[str, Any]] = [
    {
        "id": "batch",
        "label": "批量转换音色",
        "subtitle": "逐条跑 RVC（一条一条来，原声并存）",
        "status": "pending",
    },
]

# 「继续」重排的任务强引用（不持引用会被 GC 掉）
_BATCH_TASKS: set[asyncio.Task] = set()


def _find_item_for_folder(folder: Path) -> dict[str, Any] | None:
    """这条发布目录对应的批量条目（拿它的源视频做「原唱音轨」）。没有就返回 None。"""
    from .batch_worker import _find_item_for_folder as find

    try:
        found = find(folder)
    except Exception:  # noqa: BLE001 - 查不到就按成片自身音轨转，不影响主流程
        return None
    return found[1] if found else None


def scan_singing_videos_missing_voice(*, root: Path | None = None) -> list[dict[str, Any]]:
    """扫 `singing/`：**有** `最终成片.mp4`、**没有** `最终成片_音色版.mp4` 的目录。

    返回 `[{folder, source, out, label, audio_from}]`：

    - `audio_from`：该条目记着的**源视频**（有就按老链路的口径先取它的原唱音轨再转音色
      ——批量出片改成 `use_rvc=0` 之后成片里是原声，但沿用同一条路最稳）；没有就留空，
      直接拿成片自己的音轨转换。

    显式传 `root=`（测试/诊断）时只扫那一个目录。
    """
    base = Path(root) if root is not None else (BATCH_OUTPUT_ROOT / publish_subdir("singing"))
    if not base.is_dir():
        return []
    queue: list[dict[str, Any]] = []
    for folder in sorted(base.iterdir(), key=lambda path: path.name):
        if not folder.is_dir():
            continue
        source = folder / CLEAN_NAME
        out = folder / VOICE_NAME
        if not source.is_file():
            continue                       # 没有无字幕母版：跳过
        if out.is_file():
            continue                       # 已经有音色版：不重复转换
        item = _find_item_for_folder(folder)
        audio_from = ""
        if item:
            raw = str(item.get("sourcePath") or "").strip()
            if raw and Path(raw).is_file():
                audio_from = raw
        queue.append({
            "folder": folder.name,
            "source": str(source.resolve()),
            "out": str(out.resolve()),
            "label": folder.name,
            "audio_from": audio_from,
        })
    return queue


def _batch_busy() -> str:
    """现在能不能开工：返回空串 = 可以；否则返回「在忙什么」（人话，写进日志/状态）。

    RVC 与出片抢同一张卡，所以**有出片在跑就等**（不打断它）；ComfyUI 还开着也一样要等
    —— 等的那条出片交付完，`_stop_comfy_if_idle` 会把它关掉。

    ⚠️ 判据必须是 `pipeline.gpu_holder()`，**不能是 `store.active()`**：本任务在「等出片
    空档」期间自己就是最新的 `queued` 任务，用 `store.active()` 会看到自己、报「还有一条
    rvc_batch 任务在跑」并永远等下去 —— 2026-09-22 用户点的这一下就是这么静默死掉的
    （「批量音色转换开始了吗 怎么好像没有效果我点击了」）。`gpu_holder()` 会把还没抢到卡的
    rvc_batch（`queued`/`waiting`，包括它自己）与纯 CPU 的字幕任务都跳过。
    """
    try:
        from . import pipeline as pipeline_module

        active = pipeline_module.gpu_holder()
    except Exception:  # noqa: BLE001
        active = None
    if active and str(active.get("status")) in {"queued", "running"}:
        kind = str(active.get("kind") or "任务")
        return f"还有一条「{kind}」任务在跑"
    try:
        batch = batch_store.active()
    except Exception:  # noqa: BLE001
        batch = None
    if batch:
        busy = next(
            (
                item
                for item in batch.get("items") or []
                if str(item.get("status")) in {"running", "revising"}
            ),
            None,
        )
        if busy is not None:
            return f"批量第 {busy.get('index')} 条正在出片"
    return ""


async def _wait_for_idle_slot(job_id: str) -> None:
    """等出片空档（每 `WAIT_INTERVAL_SECONDS` 秒看一次，随时可取消）。"""
    reported = False
    while True:
        raise_if_cancelled(job_id)
        busy = _batch_busy()
        if not busy:
            return
        if not reported:
            store.add_log(job_id, f"正在等出片空档（{busy}）—— 转音色和出片抢同一张显卡，不打断它。")
            store.update(job_id, status="queued", stage="waiting",
                         currentNodeTitle=f"等出片空档：{busy}")
            reported = True
        await asyncio.sleep(WAIT_INTERVAL_SECONDS)


async def run_rvc_publish_batch(job_id: str) -> None:
    """一键批量：逐条给缺音色版的唱歌成片转音色（**严格串行**）。

    产物 `最终成片_音色版.mp4` 落回各自发布目录，**与无字幕母版并存**；单条失败只记日志、
    不阻断后面的。暂停在**两条之间**生效（RVC 一次要跑十几秒到一分钟，中途硬掐没有意义）。
    """
    if is_job_cancelled(job_id):
        return
    state = store.get(job_id) or {}
    # 开跑前先等出片空档，再关 ComfyUI（否则 `run_rvc` 会以「检测到 ComfyUI 仍在运行」拒绝）
    try:
        await _wait_for_idle_slot(job_id)
    except asyncio.CancelledError:
        await finish_cancelled(job_id)
        return
    if await comfy_health():
        store.add_log(job_id, "先关闭 ComfyUI 释放显存，再开始转音色（下一条出片会自己把它拉起来）。")
        await resources.stop_comfy(job_id)
        store.set_milestone(job_id, "batch", status="pending", currentNode=None, progress=0)

    queue = scan_singing_videos_missing_voice()
    total = len(queue)
    if total == 0:
        store.add_log(job_id, "发布成品里没有需要转音色的唱歌成片（都已存在音色版或没有成片）。")
        store.set_milestone(job_id, "batch", status="completed", progress=100)
        store.update(job_id, status="completed", stage="completed", rvcQueue=[],
                     rvcIndex=0, currentNodeId=None, currentNodeTitle=None,
                     progress=100, finishedAt=now_iso())
        return

    store.set_milestone(job_id, "batch", status="running", progress=1)
    store.update(job_id, status="running", stage="voice", finishedAt=None,
                 rvcQueue=queue, rvcIndex=0,
                 currentNodeId=None, currentNodeTitle=f"待转音色：{total} 条")
    if not state.get("rvcStarted"):
        store.add_log(job_id, f"一键批量转音色：{total} 条唱歌成片还没有音色版。")
        store.update(job_id, rvcStarted=True, startedAt=now_iso())
    else:
        store.add_log(job_id, f"继续转音色：发布成品里还剩 {total} 条没有音色版。")

    done = failed = 0
    failed_labels: list[str] = []
    try:
        for index, entry in enumerate(queue, start=1):
            raise_if_cancelled(job_id)
            if (store.get(job_id) or {}).get("pauseRequested"):
                remaining = total - index + 1
                store.update(job_id, status="paused", stage="paused", pauseRequested=False,
                             currentNodeId=None, currentNodeTitle=f"已暂停（还剩 {remaining} 条）")
                store.add_log(job_id, f"已暂停：还剩 {remaining} 条没做，点「继续」接着跑。")
                return
            source = Path(str(entry.get("source") or ""))
            out = Path(str(entry.get("out") or ""))
            label = str(entry.get("label") or source.parent.name)
            store.update(job_id, currentNodeTitle=f"正在给 {label} 转音色（{index}/{total}）",
                         rvcIndex=index, progress=round(index / total * 100))
            if not source.is_file():
                failed += 1
                failed_labels.append(label)
                store.add_log(job_id, f"[{index}/{total}] 跳过：{label} 的无字幕成片已不存在")
                store.update(job_id, rvcDone=done, rvcFailed=failed)
                continue
            work = DATA_DIR / "rvc_batch" / job_id / f"{index:03d}"
            try:
                work.mkdir(parents=True, exist_ok=True)
                # 在临时目录里转换：RVC 会在输入旁边留下 `_原唱音轨` 等中间文件，
                # 直接拿发布目录当工作区会把用户要发布的目录弄乱。
                staged = work / CLEAN_NAME
                await asyncio.to_thread(shutil.copy2, source, staged)
                audio_from = Path(str(entry.get("audio_from") or ""))
                store.add_log(job_id, f"[{index}/{total}] 开始：{label}")
                converted = await run_rvc(
                    job_id, staged, audio_from=audio_from if audio_from.is_file() else None
                )
                await asyncio.to_thread(shutil.copy2, converted, out)
                done += 1
                store.add_log(
                    job_id,
                    f"[{index}/{total}] 完成：{out.name}"
                    f"（{out.stat().st_size / 1024 / 1024:.1f} MB，原声母版并存）",
                )
            except Exception as error:                 # noqa: BLE001 —— 单条失败不阻断整批
                failed += 1
                failed_labels.append(label)
                store.add_log(job_id, f"[{index}/{total}] 失败：{label} —— {error}")
            finally:
                await asyncio.to_thread(shutil.rmtree, work, True)
            store.update(job_id, rvcDone=done, rvcFailed=failed)
        raise_if_cancelled(job_id)
        store.set_milestone(job_id, "batch", status="completed", progress=100)
        store.update(job_id, status="completed", stage="completed",
                     currentNodeId=None, currentNodeTitle=None, progress=100, finishedAt=now_iso())
        store.add_log(job_id, f"批量转音色结束：本次成功 {done} 条，失败 {failed} 条。")
        if failed:
            store.add_log(
                job_id,
                "失败的：" + "、".join(failed_labels)
                + "。它们还没有 `最终成片_音色版.mp4` —— 再点一次「一键批量转换音色」"
                  "（或「继续」）会重扫发布目录，只补还缺音色版的那几条。",
            )
    except Exception as error:                         # noqa: BLE001 —— 独立任务：如实报错
        if is_job_cancelled(job_id):
            await finish_cancelled(job_id)
            return
        store.set_milestone(job_id, "batch", status="error")
        store.update(job_id, status="failed", stage="failed",
                     errorSummary=f"批量转音色失败：{error}", errorDetail=repr(error),
                     finishedAt=now_iso())
        store.add_log(job_id, f"批量转音色失败：{error}")


def _spawn_batch_restart(job_id: str) -> None:
    task = asyncio.create_task(run_rvc_publish_batch(job_id))
    _BATCH_TASKS.add(task)
    task.add_done_callback(_BATCH_TASKS.discard)


def request_publish_batch_pause(job_id: str) -> dict[str, Any] | None:
    """请求暂停一键批量转音色：在**下一条开始之前**生效。不是这种任务返回 None。"""
    state = store.get(job_id)
    if not state or state.get("kind") != "rvc_batch":
        return None
    if state.get("status") == "running":
        store.add_log(job_id, "收到暂停请求：当前这条跑完即暂停。")
    store.update(job_id, pauseRequested=True)
    return store.get(job_id)


def resume_publish_batch(job_id: str) -> dict[str, Any] | None:
    """继续一条**已暂停**的一键批量转音色（重扫发布目录、只补还缺音色版的）。

    ⚠️ 与字幕批量同一条纪律：**必须清掉取消印记 `cancelRequested`**，否则 runner 第一行
    的 `is_job_cancelled` 会直接把这一轮退回去（表现是「点继续没效果」）。
    """
    state = store.get(job_id)
    if not state or state.get("kind") != "rvc_batch":
        return None
    if state.get("status") != "paused":
        return None
    store.update(job_id, pauseRequested=False, cancelRequested=False,
                 errorSummary=None, errorDetail=None)
    store.add_log(job_id, "收到继续请求：重扫发布目录，只补还缺音色版的那几条。")
    _spawn_batch_restart(job_id)
    return store.get(job_id)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="一键批量转换音色（扫 singing/ 里缺音色版的成片）")
    ap.add_argument("--list", action="store_true", help="只列出待转音色的目录")
    ap.parse_args(argv)
    queue = scan_singing_videos_missing_voice()
    if not queue:
        print("没有需要转音色的唱歌成片（都已存在音色版或没有成片）。")
        return 0
    for row in queue:
        print(f"{row['label']}  源视频={'有' if row['audio_from'] else '无'}")
    print(f"共 {len(queue)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
