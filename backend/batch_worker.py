from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import batch_ai, batch_image, batch_portrait
from .batch_store import batch_store
from .douyin_mirror import upsert_jobs as mirror_upsert
from .douyin_preview import ensure_download_playable
from .douyin_service import DouyinServiceError, douyin_service
from .lyrics_worker import netease_lyric, netease_search
from .settings import (
    BATCH_OUTPUT_ROOT,
    BATCH_RATIO_CHOICES,
    BATCH_SELF_URL,
    DATA_DIR,
    batch_default_ratio,
    env_value,
    normalize_batch_ratio,
)
from .store import now_iso


IDENTITY_PATH = Path(r"E:\AI_Assets\PortraitIdentity\本人固定参考.png")
MANIFEST_PATH = Path(r"D:\EV\download_manifest.jsonl")
COVER_FONT = Path(r"C:\Windows\Fonts\msyhbd.ttc")
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
_RUNNING_BATCHES: set[str] = set()
# 每个条目当前在跑的预审协程（模型调用 + 本地出图），跳过/删除时据此安全取消。
_ITEM_TASKS: dict[str, asyncio.Task] = {}


def cancel_item_work(batch_id: str, item_id: str) -> bool:
    task = _ITEM_TASKS.get(f"{batch_id}:{item_id}")
    if not task or task.done():
        return False
    task.cancel()
    return True


def item_milestones(kind: str) -> list[dict[str, Any]]:
    rows = [
        {"id": "download", "label": "下载抖音视频", "subtitle": "获取源视频与原作品文案", "status": "pending"},
        {"id": "prepare", "label": "生成人物图与发布文案", "subtitle": "本地分析画面并生成候选结果", "status": "pending"},
        {"id": "review", "label": "等待你的确认", "subtitle": "查看图片、标题、简介和标签", "status": "pending"},
        {"id": "video", "label": "生成最终视频", "subtitle": "复用工作台真实节点与单链路进度", "status": "pending"},
    ]
    if kind == "singing":
        rows.append(
            {"id": "lyrics", "label": "生成歌词字幕版", "subtitle": "保留无字幕版并烧录发布版", "status": "pending"}
        )
    rows.append(
        {"id": "deliver", "label": "整理发布文件", "subtitle": "发布文案与最终视频", "status": "pending"}
    )
    return rows


def unique_urls(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        url = str(value or "").strip()
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


# 一个批次（含随时追加）最多多少条视频
MAX_BATCH_ITEMS = 50


def new_item(kind: str, url: str, ratio: str, index: int, created: str) -> dict[str, Any]:
    """新建一个批次条目。新建批次与「随时追加」共用，保证字段一致。"""
    return {
        "id": uuid.uuid4().hex[:12],
        "index": index,
        "kind": kind,
        "url": url,
        "ratio": ratio,
        "status": "pending",
        "stage": "queued",
        "createdAt": created,
        "updatedAt": created,
        "title": "等待处理",
        "milestones": item_milestones(kind),
        "logs": [{"time": created, "message": "已加入批量制作队列"}],
        "revision": 0,
        "reviewApproved": False,
        "childJob": None,
        "outputs": {},
        "error": None,
        "warning": None,
    }


def _build_items(
    rows: list[tuple[str, str]],
    defaults: dict[str, str],
    start_index: int,
    created: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    index = start_index
    for kind, url in rows:
        index += 1
        items.append(new_item(kind, url, defaults[kind], index, created))
    return items


def new_batch_state(
    singing_urls: list[str],
    dance_urls: list[str],
    singing_ratio: str | None = None,
    dance_ratio: str | None = None,
) -> dict[str, Any]:
    """新建批次。比例是**条目级**字段：歌曲默认 4:3、跳舞默认 9:16，用户在页面里逐条可改。

    `singing_ratio` / `dance_ratio` 只是建批次时给整组链接的默认值（页面上的分组选择），
    之后每条都独立保存自己的 `ratio`，运行时以条目自己的值为准。
    """
    created = now_iso()
    batch_id = uuid.uuid4().hex
    defaults = {
        "singing": normalize_batch_ratio(singing_ratio, "singing"),
        "dance": normalize_batch_ratio(dance_ratio, "dance"),
    }
    rows = [("singing", url) for url in unique_urls(singing_urls)] + [
        ("dance", url) for url in unique_urls(dance_urls)
    ]
    items = _build_items(rows, defaults, 0, created)
    return {
        "id": batch_id,
        "status": "queued",
        "stage": "queued",
        "createdAt": created,
        "updatedAt": created,
        "startedAt": None,
        "finishedAt": None,
        "total": len(items),
        "completedCount": 0,
        "deletedCount": 0,
        "currentIndex": 0,
        "currentItemId": items[0]["id"] if items else None,
        "pauseRequested": False,
        "runnerActive": False,
        "notice": "任务已创建，准备处理第 1 条。" if items else "没有任务。",
        "items": items,
    }


def append_batch_items(
    batch_id: str,
    singing_urls: list[str],
    dance_urls: list[str],
    singing_ratio: str | None = None,
    dance_ratio: str | None = None,
) -> dict[str, Any]:
    """给已有批次追加条目：批次随时能加，新条目排在队尾（编号接着往下排）。

    用户 2026-09-10：「可以让我随时添加新的任务，删除单条任务」。重复粘贴的链接按
    「类型 + 链接」比对**未删除**的已有条目后直接跳过，返回跳过的条数给页面提示，
    避免同一条视频被做两遍。
    """
    state = batch_store.get(batch_id)
    if not state:
        raise KeyError(batch_id)
    defaults = {
        "singing": normalize_batch_ratio(singing_ratio, "singing"),
        "dance": normalize_batch_ratio(dance_ratio, "dance"),
    }
    rows = [("singing", url) for url in unique_urls(singing_urls)] + [
        ("dance", url) for url in unique_urls(dance_urls)
    ]
    if not rows:
        raise ValueError("请至少填写一条抖音链接")
    created = now_iso()
    result = {"added": 0, "duplicates": 0}

    def apply(row: dict[str, Any]) -> None:
        items = list(row.get("items") or [])
        known = {
            f"{item.get('kind')}:{item.get('url')}"
            for item in items
            if item.get("status") != "deleted"
        }
        fresh: list[tuple[str, str]] = []
        for kind, url in rows:
            key = f"{kind}:{url}"
            if key in known:
                result["duplicates"] += 1
                continue
            known.add(key)
            fresh.append((kind, url))
        if not fresh:
            return
        live = sum(1 for item in items if item.get("status") != "deleted")
        if live + len(fresh) > MAX_BATCH_ITEMS:
            raise ValueError(f"一个批次最多 {MAX_BATCH_ITEMS} 条视频")
        next_index = max((int(item.get("index") or 0) for item in items), default=0)
        added = _build_items(fresh, defaults, next_index, created)
        items.extend(added)
        row["items"] = items
        row["total"] = len(items)
        row["finishedAt"] = None
        if not row.get("currentItemId"):
            row["currentItemId"] = added[0]["id"]
        result["added"] = len(added)

    batch_store.mutate(batch_id, apply)
    return {"state": batch_store.get(batch_id) or {}, **result}


def _item(batch_id: str, item_id: str) -> dict[str, Any]:
    state = batch_store.get(batch_id)
    for item in (state or {}).get("items") or []:
        if item.get("id") == item_id:
            return item
    raise RuntimeError("批量条目不存在")


def _set_item(batch_id: str, item_id: str, **changes: Any) -> dict[str, Any]:
    def apply(item: dict[str, Any]) -> None:
        item.update(changes)
        item["updatedAt"] = now_iso()

    state = batch_store.mutate_item(batch_id, item_id, apply)
    return next(item for item in state["items"] if item["id"] == item_id)


def item_ratio(item: dict[str, Any]) -> str:
    """条目当前的画布比例：歌曲默认 4:3、跳舞默认 9:16，用户在页面上逐条可改。

    历史批次里没有 `ratio` 字段（甚至可能是脏值），这里兜底成类型默认值，保证老批次
    重试/出片时行为不变；写入路径（新建批次 / 改比例接口）仍然严格校验。
    """
    kind = str(item.get("kind") or "singing")
    try:
        return normalize_batch_ratio(item.get("ratio"), kind)
    except ValueError:
        return batch_default_ratio(kind)


def ratio_hint(kind: str, ratio: str) -> str:
    """给日志/前端用的比例说明（生成分辨率按各链路参数组不同）。"""
    if kind == "singing":
        return {
            "4:3": "4:3 横版（生成 640×480，二采 1440×1080）",
            "9:16": "9:16 竖版（生成 480×864，二采 1080×1920）",
        }.get(ratio, ratio)
    return {
        "4:3": "4:3 横版（生成 512×384，二采 1440×1080）",
        "9:16": "9:16 竖版（生成 512×896，二采 1080×1920）",
    }.get(ratio, ratio)


def _safe_name(value: str, fallback: str = "作品") -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value or "")
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return (clean[:48] or fallback).strip()


def _manifest_metadata(aweme_id: str) -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        return {}
    try:
        lines = MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if str(row.get("aweme_id") or "") == aweme_id:
            return row
    return {}


async def _download(batch_id: str, item_id: str) -> Path:
    item = _item(batch_id, item_id)
    existing = Path(item.get("sourcePath") or "")
    if existing.is_file():
        return existing
    batch_store.set_item_milestone(batch_id, item_id, "download", status="running", progress=5)
    _set_item(batch_id, item_id, stage="download", status="running", error=None)
    batch_store.add_item_log(batch_id, item_id, "正在下载抖音源视频……")
    try:
        job = await douyin_service.submit(item["url"])
        job_id = str(job.get("job_id") or "")
        if not job_id:
            raise RuntimeError("下载服务没有返回任务编号")
        _set_item(batch_id, item_id, downloadJobId=job_id)
        while job.get("status") not in {"success", "failed", "cancelled"}:
            await asyncio.sleep(2)
            job = await douyin_service.job(job_id)
            mirror_upsert([job])
            done = int(job.get("success") or 0) + int(job.get("failed") or 0) + int(job.get("skipped") or 0)
            total = max(1, int(job.get("total") or 1))
            batch_store.set_item_milestone(
                batch_id,
                item_id,
                "download",
                status="running",
                progress=min(92, max(8, round(done / total * 90))),
            )
        mirror_upsert([job])
        if job.get("status") != "success":
            raise RuntimeError(str(job.get("error") or "抖音下载失败"))
        result = douyin_service.result_for(job)
        if not result:
            raise RuntimeError("下载完成但没有找到视频文件")
        source = await ensure_download_playable(Path(result["path"]), str(result["awemeId"]))
        metadata = _manifest_metadata(str(result["awemeId"]))
        title = str(metadata.get("desc") or source.stem).splitlines()[0].strip() or source.stem
        _set_item(
            batch_id,
            item_id,
            sourcePath=str(source.resolve()),
            sourceName=source.name,
            awemeId=str(result["awemeId"]),
            sourceMetadata=metadata,
            title=title[:80],
        )
        batch_store.set_item_milestone(batch_id, item_id, "download", status="completed", progress=100)
        batch_store.add_item_log(batch_id, item_id, f"源视频已就绪：{source.name}")
        return source
    except DouyinServiceError as error:
        raise RuntimeError(str(error)) from error
    finally:
        # 下载完成立刻释放下载器内存，给图片分析与 ComfyUI 留足空间。
        await douyin_service.stop()


def extract_scene_frame(source: Path, target: Path, ratio: float = 0.38) -> Path:
    """从源视频抽一帧全分辨率画面，作为 Krea2 双图编辑的「图像-1 造型场景」。"""
    duration = _video_duration_seconds(source)
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{duration * ratio:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(target),
        ],
        capture_output=True,
        timeout=120,
        creationflags=flags,
    )
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError("从源视频抽帧失败，无法生成候选人物图")
    return target


def _video_duration_seconds(source: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source),
        ],
        capture_output=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if result.returncode != 0:
        raise RuntimeError("无法读取视频时长，关键帧准备失败")
    try:
        return max(0.1, float(result.stdout.decode("utf-8", errors="replace").strip()))
    except ValueError as error:
        raise RuntimeError("视频时长信息无效") from error


def build_contact_sheet(source: Path, target: Path) -> Path:
    """Extract six full-span frames once so the model need not inspect the whole video."""
    duration = _video_duration_seconds(source)
    timestamps = [duration * ratio for ratio in (0.06, 0.22, 0.38, 0.56, 0.73, 0.91)]
    frames: list[tuple[Image.Image, float]] = []
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    for timestamp in timestamps:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(640,iw)':-2",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            capture_output=True,
            timeout=90,
            creationflags=flags,
        )
        if result.returncode != 0 or not result.stdout:
            continue
        with Image.open(io.BytesIO(result.stdout)) as opened:
            frames.append((opened.convert("RGB"), timestamp))
    if len(frames) < 3:
        raise RuntimeError("关键帧提取不足，无法可靠分析视频造型")
    cell = (520, 390)
    sheet = Image.new("RGB", (cell[0] * 3, cell[1] * 2), "#090716")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(COVER_FONT), 24) if COVER_FONT.is_file() else ImageFont.load_default()
    for index, (frame, timestamp) in enumerate(frames[:6]):
        fitted = ImageOps.contain(frame, (cell[0] - 12, cell[1] - 12), method=Image.Resampling.LANCZOS)
        x = index % 3 * cell[0] + (cell[0] - fitted.width) // 2
        y = index // 3 * cell[1] + (cell[1] - fitted.height) // 2
        sheet.paste(fitted, (x, y))
        label = f"{timestamp:.1f}s"
        box = draw.textbbox((0, 0), label, font=font)
        draw.rectangle((x + 8, y + 8, x + 22 + box[2], y + 18 + box[3]), fill="#090716")
        draw.text((x + 15, y + 11), label, font=font, fill="#74e6ed")
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target, format="JPEG", quality=91, optimize=True)
    return target


async def _prepare_review(
    batch_id: str,
    item_id: str,
    *,
    feedback: str = "",
    mode: str = "both",
) -> None:
    """预审一个条目：模型分析 + 本地出候选图，然后停在审核点等用户确认。

    整个预审跑在独立子任务里，跳过/删除时由 `cancel_item_work` 取消，
    取消后由 `run_batch` 的条目级 CancelledError 分支收尾。
    """
    key = f"{batch_id}:{item_id}"
    task = asyncio.create_task(_prepare_review_work(batch_id, item_id, feedback=feedback, mode=mode))
    _ITEM_TASKS[key] = task
    try:
        await task
    finally:
        if _ITEM_TASKS.get(key) is task:
            _ITEM_TASKS.pop(key, None)


async def _prepare_review_work(
    batch_id: str,
    item_id: str,
    *,
    feedback: str = "",
    mode: str = "both",
) -> None:
    item = _item(batch_id, item_id)
    batch_store.set_item_milestone(batch_id, item_id, "prepare", status="running", progress=5)
    _set_item(batch_id, item_id, stage="prepare", status="running", error=None)
    batch_store.add_item_log(
        batch_id,
        item_id,
        "正在按修改意见重新准备候选结果……" if feedback else "正在分析源视频并准备候选图与发布文案……",
    )
    work = DATA_DIR / "batches" / batch_id / item_id
    work.mkdir(parents=True, exist_ok=True)
    source = Path(item["sourcePath"])
    duration = await asyncio.to_thread(_video_duration_seconds, source)

    contact_sheet = work / "source-contact-sheet.jpg"
    if not contact_sheet.is_file():
        batch_store.set_item_milestone(
            batch_id, item_id, "prepare", status="running", progress=8, currentNode="正在抽取 6 帧联系表"
        )
        await asyncio.to_thread(build_contact_sheet, source, contact_sheet)

    scene_frame = work / "scene-frame.jpg"
    if not scene_frame.is_file():
        scene_frame = await asyncio.to_thread(extract_scene_frame, source, scene_frame)

    meta = item.get("sourceMetadata") or {}
    meta_desc = str(meta.get("desc") or "")
    meta_tags = [str(tag) for tag in (meta.get("tags") or [])]
    warning = ""

    # 1) 模型分析：造型来源判断 + 发布文案 + 动作/运镜（或迁移提示词）。
    #    「只调图片」不重跑模型；失败不抛错，降级到源作品信息继续，避免单个条目把整批卡死。
    previous_ai = dict(item.get("ai") or {})
    result = reuse_previous_analysis(mode, previous_ai)
    if result is not None:
        batch_store.add_item_log(batch_id, item_id, "只调整图片：沿用上一版的文案与动作/运镜。")
    else:
        batch_store.set_item_milestone(
            batch_id, item_id, "prepare", status="running", progress=20, currentNode="正在分析画面与撰写文案"
        )
        try:
            result = await batch_ai.analyze(
                kind=item["kind"],
                duration=duration,
                contact_sheet=contact_sheet,
                description=meta_desc,
                tags=meta_tags,
                feedback=feedback,
                mode=mode,
                previous=previous_ai,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            result = batch_ai.fallback_result(kind=item["kind"], description=meta_desc, tags=meta_tags)
            warning = f"模型分析不可用，已降级为源作品信息：{error}"
            batch_store.add_item_log(batch_id, item_id, warning)

    # 歌曲条目的动作/运镜要能在审核时就给用户看，缺了就现在补上保守时间轴
    # （提交时 `_post_video_job` 还会再兜一次，两边逻辑一致）。
    if item["kind"] == "singing":
        action = str(result.get("action_prompt") or "").strip()
        camera = str(result.get("camera_prompt") or "").strip()
        if not action or not camera:
            fallback_action, fallback_camera = default_action_plan(duration)
            result["action_prompt"] = action or fallback_action
            result["camera_prompt"] = camera or fallback_camera
            batch_store.add_item_log(
                batch_id, item_id, "模型没有给出动作/运镜，已用按时长铺开的保守时间轴兜底。"
            )

    # 2) 候选人物图。
    #    默认 `manual`：系统只备料（提示词 + 图一 + 图二），图片由用户在 GPT 聊天里
    #    自行生成后上传回页面。也可用 H3_BATCH_IMAGE_PROVIDER 切回 api / local / frame。
    revision = int(item.get("revision") or 0)
    previous_image = Path((item.get("ai") or {}).get("reference_image_path") or "")
    target_image = work / f"candidate_r{revision}.png"
    fallback_image = work / f"candidate_r{revision}_frame.png"
    provider = _image_provider()
    if provider == "auto":
        provider = "api" if batch_image.configured() else "manual"

    style_source = str(result.get("style_source") or "video")
    ratio = item_ratio(item)
    image_prompt = batch_ai.compose_image_prompt(
        item["kind"],
        style_source,
        feedback,
        mode,
        song_name=str(result.get("song_name") or ""),
        song_mood=str(result.get("song_mood") or ""),
        ratio=ratio,
    )
    scene = scene_frame
    if provider != "manual":
        if item["kind"] == "singing" and style_source == "redesign":
            # 源视频造型不适合出片：改按歌曲情绪重做造型。
            scene = IDENTITY_PATH
            image_prompt = (
                "本次没有造型参考图。图像-1 与图像-2 是同一位人物的身份参考，"
                "请按歌曲情绪完全重新设计造型、服装、背景与灯光。\n\n" + image_prompt
            )
        else:
            # 图一里是源视频那个人的脸，必须糊掉：否则模型会把别人的五官混进来。
            scene, defocused = await batch_image.defocus_reference_face(scene_frame, work)
            if defocused:
                batch_store.add_item_log(
                    batch_id, item_id, "已把参考帧里的人脸模糊掉，避免混入源视频人物的五官。"
                )
            else:
                note = "参考帧人脸定位失败，未能屏蔽源视频人物的脸（可能影响五官一致性）。"
                warning = f"{warning} {note}".strip()
                batch_store.add_item_log(batch_id, item_id, note)

    # 出图素材写进状态；提示词同时落盘，方便直接从条目目录取用
    result["imagePrompt"] = image_prompt
    result["sceneFramePath"] = str(scene_frame)
    # 记住拼提示词用的参数：用户中途改画布比例时，要能按新比例重拼这一条提示词
    result["imagePromptFeedback"] = feedback
    result["imagePromptMode"] = mode
    result["imageRatio"] = ratio
    try:
        (work / "出图提示词.txt").write_text(image_prompt, encoding="utf-8")
    except OSError:
        pass

    image_path: Path | None = None

    if mode == "copy" and previous_image.is_file():
        image_path = previous_image
    elif provider == "manual":
        if previous_image.is_file():
            # 手动模式永远不自动出图：已有成图就保留，用户上传新图即为替换。
            image_path = previous_image
        else:
            batch_store.add_item_log(
                batch_id,
                item_id,
                "已备好出图素材：复制提示词、下载图一与图二，在 GPT 聊天里生成后把图上传回来。",
            )
    elif provider == "frame":
        await asyncio.to_thread(shutil.copy2, scene_frame, fallback_image)
        image_path = fallback_image
        note = "已直接用源视频取帧作为候选人物图。"
        warning = f"{warning} {note}".strip()
        batch_store.add_item_log(batch_id, item_id, note)
    else:

        async def report(fraction: float, note: str) -> None:
            changes: dict[str, Any] = {"currentNode": note}
            if fraction > 0:
                changes["progress"] = round(30 + 68 * max(0.0, min(1.0, fraction)))
            else:
                changes["progress"] = None
            batch_store.set_item_milestone(
                batch_id, item_id, "prepare", status="running", **changes
            )

        try:
            batch_store.set_item_milestone(
                batch_id, item_id, "prepare", status="running", progress=32, currentNode="正在生成候选人物图"
            )
            if provider == "local":
                await batch_portrait.generate_portrait(
                    scene_image=scene,
                    identity_image=IDENTITY_PATH,
                    prompt=image_prompt,
                    ratio=ratio,
                    output_path=target_image,
                    prefix=f"batch_{item_id}",
                    on_progress=report,
                )
            else:
                await batch_image.generate_candidate_image(
                    scene_image=scene,
                    identity_image=IDENTITY_PATH,
                    prompt=image_prompt,
                    ratio=ratio,
                    output_path=target_image,
                    on_progress=report,
                )
            image_path = target_image
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await asyncio.to_thread(shutil.copy2, scene_frame, fallback_image)
            image_path = fallback_image
            note = f"候选图生成失败，已退回源视频取帧：{error}"
            warning = f"{warning} {note}".strip()
            batch_store.add_item_log(batch_id, item_id, note)

        # 换脸锁定身份：编辑模型是重新合成脸，只靠提示词保不住五官。
        # **默认关闭**：用户实测 ReActor 换脸「效果太差」（贴脸感明显、肤色与脖子对不上），
        # 所以只在显式设置 H3_BATCH_FACE_SWAP=1 时才启用，不默认污染结果。
        if (
            env_value("H3_BATCH_FACE_SWAP", "0").strip() == "1"
            and image_path == target_image
            and target_image.is_file()
        ):
            swapped = work / f"candidate_r{revision}_swapped.png"
            try:
                batch_store.set_item_milestone(
                    batch_id, item_id, "prepare", status="running", progress=None,
                    currentNode="正在用 ReActor 锁定五官",
                )
                await batch_image.swap_face_with_prototype(
                    generated=target_image, prototype=IDENTITY_PATH, output_path=swapped
                )
                image_path = swapped
                batch_store.add_item_log(batch_id, item_id, "已用 ReActor 把原型图的五官换到候选图上。")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                note = f"换脸失败，沿用未换脸的生成图（五官可能偏离原型图）：{error}"
                warning = f"{warning} {note}".strip()
                batch_store.add_item_log(batch_id, item_id, note)

    if image_path is not None:
        image_path = image_path.resolve()
        try:
            image_path.relative_to(work.resolve())
        except ValueError:
            raise RuntimeError("人物图必须保存在当前批次目录内") from None
        if not image_path.is_file():
            raise RuntimeError("人物图没有留下可用文件，请重新上传或点击重试")
        result["reference_image_path"] = str(image_path)
    else:
        result["reference_image_path"] = ""

    result["tags"] = [
        str(tag).strip().lstrip("#") for tag in result.get("tags") or [] if str(tag).strip()
    ][:5]

    # 3) 看着最终候选图重写发布文案，保证图文一致。
    #    预审的分析只看得到源视频联系表、看不到之后生成的图，两者一旦不一致
    #    （实测出图换成黑发水晶场景、文案却还在写「粉色氛围」），文案就会和画面脱节。
    #    「只调图片」按用户约定不动文案，所以那条路径不重写。
    if mode != "image" and result.get("reference_image_path"):
        batch_store.set_item_milestone(
            batch_id, item_id, "prepare", status="running", progress=99, currentNode="正在看着最终图写发布文案"
        )
        try:
            copy = await batch_ai.write_copy(
                candidate_image=Path(str(result["reference_image_path"])),
                song_name=str(result.get("song_name") or ""),
                song_mood=str(result.get("song_mood") or ""),
                description=meta_desc,
                feedback=feedback if mode in {"copy", "both"} else "",
            )
            for key in ("title", "introduction", "tags"):
                if copy.get(key):
                    result[key] = copy[key]
            result["tags"] = [
                str(tag).strip().lstrip("#") for tag in result.get("tags") or [] if str(tag).strip()
            ][:5]
            batch_store.add_item_log(batch_id, item_id, "发布文案已按最终画面重写，确保图文一致。")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            note = f"图文一致性的文案重写失败，沿用上一版文案：{error}"
            warning = f"{warning} {note}".strip()
            batch_store.add_item_log(batch_id, item_id, note)
    _set_item(
        batch_id,
        item_id,
        ai=result,
        title=str(result.get("title") or item.get("title") or "候选作品"),
        status="awaiting_review",
        stage="review",
        reviewApproved=False,
        revisionFeedback="",
        revisionMode="",
        warning=warning or None,
    )
    batch_store.set_item_milestone(batch_id, item_id, "prepare", status="completed", progress=100)
    batch_store.set_item_milestone(batch_id, item_id, "review", status="running")
    if result.get("reference_image_path"):
        batch_store.add_item_log(batch_id, item_id, "候选图与发布文案已就绪，等待你确认出片。")
    else:
        batch_store.add_item_log(
            batch_id, item_id, "出图素材已备齐，等待你上传 GPT 生成的图片后再确认出片。"
        )
    # 注意：这里**不**把整个批次置为 awaiting_review，也不停 runner ——
    # 用户要求先整批备料，所以要让 run_batch 继续跑下一条的备料。


async def _wait_for_free_pipeline(batch_id: str, item_id: str) -> None:
    announced = False
    while True:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{BATCH_SELF_URL}/api/comfy/queue")
            payload = response.json() if response.is_success else {}
        active = payload.get("app")
        if not active or active.get("status") not in {"queued", "running"}:
            return
        if not announced:
            batch_store.add_item_log(batch_id, item_id, "已有单链路任务运行，当前条目正在等待资源。")
            announced = True
        await asyncio.sleep(4)


IMAGE_PROVIDERS = {"auto", "api", "local", "frame", "manual"}


def _image_provider() -> str:
    """候选人物图来源：`manual` 用户自己在 GPT 聊天里做（**默认**）/ `api` 中转站 /
    `local` 本地 Krea2 / `frame` 源视频取帧 / `auto`。

    默认 `manual`：用户明确要求「去掉图片生成，图片由我自己去 gpt 聊天补充」。
    出图质量与五官由用户把关，批量只负责备料（提示词 + 图一 + 图二）并接收成图。
    """
    value = env_value("H3_BATCH_IMAGE_PROVIDER", "manual").strip().lower()
    return value if value in IMAGE_PROVIDERS else "manual"


def reuse_previous_analysis(mode: str, previous: dict[str, Any]) -> dict[str, Any] | None:
    """「只调图片」时复用上一版的分析结果，不重新调用模型。

    否则文案与动作/运镜会被一起改写 —— 实测把「保留头顶留白」这类构图措辞
    串进了运镜时间轴。三种调整范围必须严格各管各的。
    """
    if mode == "image" and previous and previous.get("reference_image_path"):
        return dict(previous)
    return None


def default_action_plan(duration: float) -> tuple[str, str]:
    """模型不可用时的保守动作/运镜时间轴，按时长铺满，避免视频链路没有输入。"""
    span = max(4.0, min(60.0, float(duration or 0) or 30.0))
    half = round(span / 2, 1)
    action = (
        f"0–{half}秒：身体随节拍轻轻左右摇摆，目光自然看向镜头\n"
        f"{half}–{span}秒：头部小幅转动，肩部保持轻微律动，目光回到镜头"
    )
    camera = (
        f"0–{half}秒：保持稳定的近距离正面构图，只有轻微自然手持漂移\n"
        f"{half}–{span}秒：机位基本固定，人物保持居中，不做明显推拉摇移"
    )
    return action, camera


async def _post_video_job(batch_id: str, item_id: str) -> dict[str, Any]:
    item = _item(batch_id, item_id)
    ai = item.get("ai") or {}
    source = Path(item["sourcePath"])
    reference = Path(ai["reference_image_path"])
    # 比例以条目自己的选择为准（歌曲默认 4:3、跳舞默认 9:16，用户可逐条改）
    ratio = item_ratio(item)
    await _wait_for_free_pipeline(batch_id, item_id)
    batch_store.add_item_log(
        batch_id, item_id, f"本条画布比例：{ratio_hint(str(item['kind']), ratio)}"
    )
    if item["kind"] == "singing":
        # 动作/运镜已在预审时随文案一起产出（同一次模型调用）。
        # 模型降级时用按时长铺开的保守时间轴兜底，保证视频链路仍能启动。
        action = str(ai.get("action_prompt") or "").strip()
        camera = str(ai.get("camera_prompt") or "").strip()
        if not action or not camera:
            duration = await asyncio.to_thread(_video_duration_seconds, source)
            fallback_action, fallback_camera = default_action_plan(duration)
            action = action or fallback_action
            camera = camera or fallback_camera
            batch_store.add_item_log(batch_id, item_id, "动作/运镜缺少模型结果，已使用保守时间轴兜底。")
        current = _item(batch_id, item_id)
        if current.get("skipRequested") or current.get("deleteRequested"):
            raise asyncio.CancelledError
        _set_item(batch_id, item_id, actionPrompt=action, cameraPrompt=camera)
        data = {
            "action_prompt": action,
            "camera_prompt": camera,
            "ratio": ratio,
            "use_rvc": "1",
            "use_upscale": "1",
        }
        endpoint = "/api/jobs"
    else:
        data = {
            "ratio": ratio,
            "remove_subtitles": "1" if ai.get("remove_subtitles") else "0",
            "mode": "animation",
            "content_prompt": str(ai.get("content_prompt") or ""),
            "video_prompt": str(ai.get("video_prompt") or ""),
            "image_prompt": str(ai.get("image_prompt") or ""),
            "use_upscale": "1",
        }
        endpoint = "/api/jobs/migrate"
    async with httpx.AsyncClient(timeout=180) as client:
        with source.open("rb") as video_file, reference.open("rb") as image_file:
            response = await client.post(
                f"{BATCH_SELF_URL}{endpoint}",
                data=data,
                files={
                    "video": (source.name, video_file, "video/mp4"),
                    "reference_image": (reference.name, image_file, "image/png"),
                },
            )
    if not response.is_success:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        raise RuntimeError(detail or f"视频任务提交失败（HTTP {response.status_code}）")
    return response.json()


async def _watch_child(batch_id: str, item_id: str, child_id: str, milestone_id: str) -> dict[str, Any]:
    while True:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{BATCH_SELF_URL}/api/jobs/{child_id}")
        if not response.is_success:
            raise RuntimeError("无法读取子任务进度")
        child = response.json()
        snapshot = {
            "id": child.get("id"),
            "kind": child.get("kind"),
            "status": child.get("status"),
            "stage": child.get("stage"),
            "progress": child.get("progress"),
            "currentNodeTitle": child.get("currentNodeTitle"),
            "milestones": child.get("milestones") or [],
            "logs": (child.get("logs") or [])[-20:],
            "currentSegment": child.get("currentSegment"),
            "estimatedSegments": child.get("estimatedSegments"),
            "upscaleBatch": child.get("upscaleBatch"),
            "upscaleBatches": child.get("upscaleBatches"),
            "cleanBatch": child.get("cleanBatch"),
            "cleanBatches": child.get("cleanBatches"),
        }
        _set_item(batch_id, item_id, childJob=snapshot, stageMedia=stage_media(child))
        batch_store.set_item_milestone(
            batch_id,
            item_id,
            milestone_id,
            status="running",
            progress=child.get("progress") or 0,
            currentNode=child.get("currentNodeTitle"),
        )
        if child.get("status") == "completed":
            return child
        current = _item(batch_id, item_id)
        if current.get("skipRequested") or current.get("deleteRequested"):
            async with httpx.AsyncClient(timeout=20) as client:
                await client.post(f"{BATCH_SELF_URL}/api/jobs/{child_id}/cancel")
            raise asyncio.CancelledError
        if child.get("status") in {"failed", "cancelled", "interrupted"}:
            raise RuntimeError(str(child.get("errorSummary") or child.get("errorDetail") or "子任务失败"))
        await asyncio.sleep(3)


async def _create_lyrics_job(batch_id: str, item_id: str, source_job: dict[str, Any]) -> dict[str, Any] | None:
    item = _item(batch_id, item_id)
    ai = item.get("ai") or {}
    song_name = str(ai.get("song_name") or "").strip()
    if not song_name:
        raise RuntimeError("没有识别出歌曲名，无法自动搜索歌词")
    candidates = await netease_search(song_name, 6)
    if not candidates:
        raise RuntimeError(f"网易云没有找到《{song_name}》的歌词")
    normalized = re.sub(r"\s+", "", song_name).lower()
    selected = max(
        candidates,
        key=lambda row: (
            re.sub(r"\s+", "", str(row.get("name") or "")).lower() in normalized,
            int(row.get("lineCount") or 0),
        ),
    )
    detail = await netease_lyric(int(selected["id"]))
    lines = detail.get("lines") or []
    if not lines:
        raise RuntimeError("歌词候选没有可用正文")
    data = {
        "source_job_id": str(source_job["id"]),
        "source_key": "final",
        "song_name": f"{selected.get('name') or song_name} - {selected.get('artist') or ''}".strip(" -"),
        "lines_json": json.dumps(lines, ensure_ascii=False),
    }
    await _wait_for_free_pipeline(batch_id, item_id)
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(f"{BATCH_SELF_URL}/api/jobs/lyrics", data=data)
    if not response.is_success:
        try:
            detail_text = response.json().get("detail")
        except ValueError:
            detail_text = response.text
        raise RuntimeError(detail_text or "歌词字幕任务提交失败")
    child = response.json()
    _set_item(batch_id, item_id, lyricJobId=child["id"], childJob=child)
    return await _watch_child(batch_id, item_id, child["id"], "lyrics")


def _copy_file(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


async def _deliver(
    batch_id: str,
    item_id: str,
    video_job: dict[str, Any],
    lyric_job: dict[str, Any] | None,
) -> dict[str, str]:
    item = _item(batch_id, item_id)
    ai = item.get("ai") or {}
    aweme_id = str(item.get("awemeId") or item_id)
    base_name = _safe_name(str(ai.get("song_name") or ai.get("title") or item.get("title") or "作品"))
    folder = BATCH_OUTPUT_ROOT / f"{int(item['index']):03d}_{base_name}_{aweme_id}"
    folder.mkdir(parents=True, exist_ok=True)
    final_path = Path(video_job.get("finalOutput") or "")
    if not final_path.is_file():
        raise RuntimeError("视频任务完成但没有找到最终成片")
    outputs: dict[str, str] = {}
    if item["kind"] == "singing":
        no_lyrics = _copy_file(final_path, folder / "最终发布视频_无字幕.mp4")
        outputs["videoNoLyrics"] = str(no_lyrics)
        if lyric_job and Path(lyric_job.get("finalOutput") or "").is_file():
            with_lyrics = _copy_file(Path(lyric_job["finalOutput"]), folder / "最终发布视频_有字幕.mp4")
            outputs["videoWithLyrics"] = str(with_lyrics)
    else:
        final = _copy_file(final_path, folder / "最终发布视频.mp4")
        outputs["videoFinal"] = str(final)
    tags = " ".join(f"#{str(tag).strip().lstrip('#')}" for tag in ai.get("tags") or [] if str(tag).strip())
    copy_text = (
        f"标题：\n{str(ai.get('title') or '').strip()}\n\n"
        f"简介：\n{str(ai.get('introduction') or '').strip()}\n\n"
        f"标签：\n{tags}\n"
    )
    copy_path = folder / "发布文案.txt"
    copy_path.write_text(copy_text, encoding="utf-8-sig")
    outputs["copy"] = str(copy_path)
    # 双封面由用户自己在 GPT 聊天里生成，这里不再渲染（2026-09-10 用户要求）。
    outputs["folder"] = str(folder.resolve())
    return outputs


async def _process_confirmed(batch_id: str, item_id: str) -> None:
    item = _item(batch_id, item_id)
    _set_item(batch_id, item_id, status="running", stage="video", error=None, childJob=None)
    batch_store.set_item_milestone(batch_id, item_id, "video", status="running", progress=1)
    batch_store.add_item_log(batch_id, item_id, "已确认候选结果，开始执行视频生成单链路。")
    video_job = await _post_video_job(batch_id, item_id)
    _set_item(batch_id, item_id, videoJobId=video_job["id"], childJob=video_job)
    video_job = await _watch_child(batch_id, item_id, video_job["id"], "video")
    batch_store.set_item_milestone(batch_id, item_id, "video", status="completed", progress=100)
    lyric_job: dict[str, Any] | None = None
    if item["kind"] == "singing":
        batch_store.set_item_milestone(batch_id, item_id, "lyrics", status="running", progress=1)
        try:
            lyric_job = await _create_lyrics_job(batch_id, item_id, video_job)
            batch_store.set_item_milestone(batch_id, item_id, "lyrics", status="completed", progress=100)
            if lyric_job and Path(str(lyric_job.get("finalOutput") or "")).is_file():
                merged = dict(_item(batch_id, item_id).get("stageMedia") or {})
                merged["lyrics"] = str(lyric_job["finalOutput"])
                _set_item(batch_id, item_id, stageMedia=merged)
        except Exception as error:
            _set_item(batch_id, item_id, warning=f"歌词字幕未完成：{error}", childJob=None)
            batch_store.set_item_milestone(batch_id, item_id, "lyrics", status="error", progress=0, currentNode=str(error))
            batch_store.add_item_log(batch_id, item_id, f"歌词字幕未完成，已保留无字幕成片：{error}")
    batch_store.set_item_milestone(batch_id, item_id, "deliver", status="running", progress=20)
    _set_item(batch_id, item_id, stage="deliver", childJob=None)
    outputs = await _deliver(batch_id, item_id, video_job, lyric_job)
    _set_item(batch_id, item_id, status="completed", stage="completed", outputs=outputs, finishedAt=now_iso())
    batch_store.set_item_milestone(batch_id, item_id, "deliver", status="completed", progress=100)
    batch_store.add_item_log(batch_id, item_id, f"发布文件已整理：{outputs['folder']}")


def stage_media(state: dict[str, Any]) -> dict[str, str]:
    """把子任务状态里的中间成片路径抽出来，供页面只读回看每个阶段的产物。

    这些键在 `app.py` 的 `MEDIA_FIELD_BY_KEY` 里有对应项；成片入口只暴露
    final/original，但用户要求「可以回看每一阶段生成的内容」——所以中间产物
    （去字幕、二创草稿、二采高清）也要能被点开看，只是不能改。
    """
    media: dict[str, str] = {}
    for key, field in (
        ("draft", "draftOutput"),
        ("clean", "cleanOutput"),
        ("original", "originalOutput"),
        ("enhanced", "enhancedOutput"),
        ("final", "finalOutput"),
    ):
        raw = str(state.get(field) or "").strip()
        if raw and Path(raw).is_file():
            media[key] = raw
    return media


def _next_work(state: dict[str, Any]) -> dict[str, Any] | None:
    """下一个需要跑的任务：先把整批备料（pending / revising）跑完，再逐条出片（confirmed）。

    `awaiting_review` 是在等用户上传图片并确认，**不算可跑任务** —— 否则整批备料会被
    第一条的审核点卡住。用户明确要求「先整批备料，再逐条审核出片」。
    """
    items = state.get("items") or []
    for statuses in ({"pending", "revising"}, {"confirmed"}):
        for item in items:
            if item.get("status") in statuses:
                return item
    return None


async def run_batch(batch_id: str) -> None:
    if batch_id in _RUNNING_BATCHES:
        return
    _RUNNING_BATCHES.add(batch_id)
    try:
        state = batch_store.get(batch_id)
        if not state:
            return
        batch_store.update(
            batch_id,
            status="running",
            stage="running",
            startedAt=state.get("startedAt") or now_iso(),
            runnerActive=True,
            pauseRequested=False,
            notice="批次正在运行。",
        )
        while True:
            state = batch_store.get(batch_id)
            if not state:
                return
            if state.get("pauseRequested") or state.get("status") == "paused":
                batch_store.update(batch_id, status="paused", runnerActive=False, notice="批次已暂停。")
                return
            item = _next_work(state)
            if item is None:
                waiting = [
                    row for row in state.get("items") or [] if row.get("status") == "awaiting_review"
                ]
                if waiting:
                    # 整批素材已备齐，停下来等用户逐条上传图片 + 确认出片
                    batch_store.update(
                        batch_id,
                        status="awaiting_review",
                        stage="review",
                        runnerActive=False,
                        currentItemId=state.get("currentItemId") or waiting[0]["id"],
                        notice=f"整批素材已备齐（{len(waiting)} 条待审核）。请逐条上传图片并确认出片。",
                    )
                    return
                warnings = any(row.get("warning") for row in state.get("items") or [])
                batch_store.update(
                    batch_id,
                    status="completed",
                    stage="completed",
                    runnerActive=False,
                    currentItemId=None,
                    currentIndex=state.get("total") or 0,
                    finishedAt=now_iso(),
                    notice="全部条目已完成。" + (" 部分歌词字幕需要稍后重试。" if warnings else ""),
                )
                return
            preparing = item.get("status") in {"pending", "revising"}
            batch_store.update(
                batch_id,
                currentItemId=item["id"],
                currentIndex=item["index"],
                notice=(
                    f"正在备料第 {item['index']} / {state['total']} 条。"
                    if preparing
                    else f"正在出片第 {item['index']} / {state['total']} 条。"
                ),
            )
            try:
                if item.get("skipRequested"):
                    _set_item(batch_id, item["id"], status="skipped", stage="skipped", finishedAt=now_iso())
                    for milestone in item.get("milestones") or []:
                        if milestone.get("status") == "pending":
                            batch_store.set_item_milestone(batch_id, item["id"], milestone["id"], status="skipped")
                    continue
                if item.get("status") == "confirmed":
                    await _process_confirmed(batch_id, item["id"])
                    continue
                await _download(batch_id, item["id"])
                refreshed = _item(batch_id, item["id"])
                if refreshed.get("skipRequested") or refreshed.get("deleteRequested"):
                    raise asyncio.CancelledError
                feedback = str(refreshed.get("revisionFeedback") or "")
                mode = str(refreshed.get("revisionMode") or "both")
                await _prepare_review(batch_id, item["id"], feedback=feedback, mode=mode)
                continue
            except asyncio.CancelledError:
                current = _item(batch_id, item["id"])
                deleted = bool(current.get("deleteRequested"))
                next_status = "deleted" if deleted else "skipped"
                _set_item(
                    batch_id,
                    item["id"],
                    status=next_status,
                    stage=next_status,
                    finishedAt=now_iso(),
                    childJob=None,
                )
                for milestone in current.get("milestones") or []:
                    if milestone.get("status") in {"pending", "running"}:
                        batch_store.set_item_milestone(
                            batch_id, item["id"], milestone["id"], status="skipped"
                        )
                batch_store.add_item_log(
                    batch_id,
                    item["id"],
                    "当前条目已删除。" if deleted else "当前条目已跳过。",
                )
                continue
            except Exception as error:
                current = _item(batch_id, item["id"])
                if current.get("skipRequested") or current.get("deleteRequested"):
                    deleted = bool(current.get("deleteRequested"))
                    next_status = "deleted" if deleted else "skipped"
                    _set_item(
                        batch_id,
                        item["id"],
                        status=next_status,
                        stage=next_status,
                        finishedAt=now_iso(),
                        childJob=None,
                        error=None,
                    )
                    for milestone in current.get("milestones") or []:
                        if milestone.get("status") in {"pending", "running"}:
                            batch_store.set_item_milestone(
                                batch_id, item["id"], milestone["id"], status="skipped"
                            )
                    batch_store.add_item_log(
                        batch_id,
                        item["id"],
                        "当前条目已删除。" if deleted else "当前条目已跳过。",
                    )
                    continue
                message = str(error)
                _set_item(batch_id, item["id"], status="failed", stage="failed", error=message, childJob=None)
                active_milestone = next(
                    (row for row in _item(batch_id, item["id"])["milestones"] if row.get("status") == "running"),
                    None,
                )
                if active_milestone:
                    batch_store.set_item_milestone(
                        batch_id, item["id"], active_milestone["id"], status="error", currentNode=message
                    )
                batch_store.add_item_log(batch_id, item["id"], f"任务暂停：{message}")
                batch_store.update(
                    batch_id,
                    status="failed",
                    stage="failed",
                    runnerActive=False,
                    notice="当前条目需要处理后重试，后续条目尚未启动。",
                )
                return
    finally:
        _RUNNING_BATCHES.discard(batch_id)
        state = batch_store.get(batch_id)
        if state and state.get("runnerActive"):
            batch_store.update(batch_id, runnerActive=False)


def _ratio_aspect(ratio: str) -> float:
    width, height = (int(part) for part in ratio.split(":"))
    return (width / height) if height else 1.0


def _image_box(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except (OSError, ValueError):
        return None


def image_ratio_note(image: Path, ratio: str) -> str:
    """候选图与条目画布比例不一致时给一句人话提示；一致或读不出来时返回空串。

    只提示不拦截：最终构图由工作流按画布缩放，用户有权用任意比例的图出片。
    """
    box = _image_box(image)
    if not box:
        return ""
    width, height = box
    if not height:
        return ""
    expected = _ratio_aspect(ratio)
    if abs((width / height) - expected) / expected <= 0.08:
        return ""
    return (
        f"这张图是 {width}×{height}，与本条画布比例 {ratio} 不一致；"
        f"需要的话可以改用一张 {ratio} 的图（不换也能出片）。"
    )


def set_item_ratio(batch_id: str, item_id: str, ratio: str) -> dict[str, Any]:
    """改某一条视频的画布比例（歌曲默认 4:3、跳舞默认 9:16，用户逐条可改）。

    用户 2026-09-10：「每个视频需要让我选择比例，歌曲默认 4:3，跳舞默认 9:16，我可以改的」。

    - 正在跑（running/revising）、已确认出片（confirmed）或已结束的条目不接受修改：
      这时改比例会和已经提交出去的子任务打架。
    - 已经备过料的条目：按新比例重拼出图提示词并落盘，让用户拿到的备料与最终成片一致；
      旧候选图若与新比例不符只提示、不擅自删——图是用户自己出的，重做与否由他决定。
    """
    item = _item(batch_id, item_id)
    kind = str(item.get("kind") or "singing")
    target = normalize_batch_ratio(ratio, kind)
    if item.get("status") in {"running", "revising", "confirmed", "completed", "skipped", "deleted"}:
        raise ValueError("当前条目正在执行或已经结束，不能再改画布比例")
    previous = item_ratio(item)
    if target == previous:
        return batch_store.get(batch_id) or {}

    ai = dict(item.get("ai") or {})
    if ai.get("imagePrompt"):
        ai["imagePrompt"] = batch_ai.compose_image_prompt(
            kind,
            str(ai.get("style_source") or "video"),
            str(ai.get("imagePromptFeedback") or ""),
            str(ai.get("imagePromptMode") or "both"),
            song_name=str(ai.get("song_name") or ""),
            song_mood=str(ai.get("song_mood") or ""),
            ratio=target,
        )
        ai["imageRatio"] = target
        try:
            prompt_path = DATA_DIR / "batches" / batch_id / item_id / "出图提示词.txt"
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(str(ai["imagePrompt"]), encoding="utf-8")
        except OSError:
            pass

    warning = item.get("warning")
    note = f"画布比例已改为 {ratio_hint(kind, target)}。"
    mismatch = image_ratio_note(Path(str(ai.get("reference_image_path") or "")), target)
    if mismatch:
        warning = mismatch
        note = f"{note} {mismatch}"

    def apply(row: dict[str, Any]) -> None:
        row["ratio"] = target
        row["ai"] = ai
        row["warning"] = warning

    batch_store.mutate_item(batch_id, item_id, apply)
    batch_store.add_item_log(batch_id, item_id, note)
    return batch_store.get(batch_id) or {}


def request_review_adjustment(batch_id: str, item_id: str, feedback: str, mode: str) -> dict[str, Any]:
    item = _item(batch_id, item_id)
    if item.get("status") != "awaiting_review":
        raise ValueError("当前条目不在审核状态")
    if mode not in {"image", "copy", "both"}:
        raise ValueError("修改范围不正确")
    if not feedback.strip():
        raise ValueError("请填写需要调整的地方")
    revision = int(item.get("revision") or 0) + 1
    batch_store.set_item_milestone(batch_id, item_id, "review", status="pending")
    _set_item(
        batch_id,
        item_id,
        status="revising",
        stage="prepare",
        revision=revision,
        revisionFeedback=feedback.strip(),
        revisionMode=mode,
        error=None,
    )
    return batch_store.update(
        batch_id,
        status="running",
        stage="prepare",
        runnerActive=False,
        notice="正在按你的修改意见调整候选结果。",
    )
