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
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from . import batch_ai, batch_image, batch_portrait
from .batch_store import batch_store
from .douyin_mirror import upsert_jobs as mirror_upsert
from .douyin_preview import ensure_download_playable
from .douyin_service import DouyinServiceError, douyin_service
from .lyrics_worker import netease_lyric, netease_search
from .settings import (
    BATCH_OUTPUT_ROOT,
    BATCH_SELF_URL,
    DATA_DIR,
    env_value,
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
        {"id": "deliver", "label": "整理发布文件", "subtitle": "文案、双平台封面与最终视频", "status": "pending"}
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


def new_batch_state(singing_urls: list[str], dance_urls: list[str]) -> dict[str, Any]:
    created = now_iso()
    batch_id = uuid.uuid4().hex
    rows = [("singing", url) for url in unique_urls(singing_urls)] + [
        ("dance", url) for url in unique_urls(dance_urls)
    ]
    items: list[dict[str, Any]] = []
    for index, (kind, url) in enumerate(rows, start=1):
        items.append(
            {
                "id": uuid.uuid4().hex[:12],
                "index": index,
                "kind": kind,
                "url": url,
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
        )
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
    #    失败不抛错，降级到源作品信息继续，避免单个条目把整批卡死。
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
            previous=item.get("ai") or {},
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        result = batch_ai.fallback_result(kind=item["kind"], description=meta_desc, tags=meta_tags)
        warning = f"模型分析不可用，已降级为源作品信息：{error}"
        batch_store.add_item_log(batch_id, item_id, warning)

    # 2) 候选人物图：中转站图片接口优先 → 本地 Krea2（可选，画质不达标）→ 源视频取帧兜底。
    revision = int(item.get("revision") or 0)
    previous_image = Path((item.get("ai") or {}).get("reference_image_path") or "")
    target_image = work / f"candidate_r{revision}.png"
    fallback_image = work / f"candidate_r{revision}_frame.png"
    provider = _image_provider()
    if provider == "auto":
        provider = "api" if batch_image.configured() else "frame"

    if mode == "copy" and previous_image.is_file():
        image_path = previous_image
    elif provider == "frame":
        await asyncio.to_thread(shutil.copy2, scene_frame, fallback_image)
        image_path = fallback_image
        note = "未配置中转站图片接口，已直接用源视频取帧作为候选人物图。"
        warning = f"{warning} {note}".strip()
        batch_store.add_item_log(batch_id, item_id, note)
    else:

        async def report(fraction: float, note: str) -> None:
            changes: dict[str, Any] = {"currentNode": note}
            # 中转站没有节点进度，只更新时间说明，不编造百分比。
            if fraction > 0:
                changes["progress"] = round(30 + 68 * max(0.0, min(1.0, fraction)))
            batch_store.set_item_milestone(
                batch_id, item_id, "prepare", status="running", **changes
            )

        style_source = str(result.get("style_source") or "video")
        prompt = batch_ai.compose_prompt(item["kind"], style_source)
        scene = scene_frame
        if item["kind"] == "singing" and style_source == "redesign":
            # 源视频造型不适合出片：改按歌曲情绪重做造型。工作流固定要两张输入，
            # 所以两张都喂身份图，并由提示词声明「本次没有造型参考图」。
            scene = IDENTITY_PATH
            prompt = (
                "本次没有造型参考图。图像-1 与图像-2 是同一位人物的身份参考，"
                "请按歌曲情绪完全重新设计造型、服装、背景与灯光。\n\n" + prompt
            )
        ratio = "4:3" if item["kind"] == "singing" else "9:16"
        try:
            batch_store.set_item_milestone(
                batch_id, item_id, "prepare", status="running", progress=32, currentNode="正在生成候选人物图"
            )
            if provider == "local":
                await batch_portrait.generate_portrait(
                    scene_image=scene,
                    identity_image=IDENTITY_PATH,
                    prompt=prompt,
                    ratio=ratio,
                    output_path=target_image,
                    prefix=f"batch_{item_id}",
                    on_progress=report,
                )
            else:
                await batch_image.generate_candidate_image(
                    scene_image=scene,
                    identity_image=IDENTITY_PATH,
                    prompt=prompt,
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

    image_path = image_path.resolve()
    try:
        image_path.relative_to(work.resolve())
    except ValueError:
        raise RuntimeError("人物图必须保存在当前批次目录内") from None
    if not image_path.is_file():
        raise RuntimeError("人物图生成没有留下可用文件，请点击重试")

    result["reference_image_path"] = str(image_path)
    result["tags"] = [
        str(tag).strip().lstrip("#") for tag in result.get("tags") or [] if str(tag).strip()
    ][:12]
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
    batch_store.add_item_log(batch_id, item_id, "候选图片和发布文案已生成，等待你确认。")
    batch_store.update(
        batch_id,
        status="awaiting_review",
        stage="review",
        runnerActive=False,
        currentItemId=item_id,
        notice="请查看当前条目的图片与发布文案；确认后才会开始生成视频。",
    )


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


IMAGE_PROVIDERS = {"auto", "api", "local", "frame"}


def _image_provider() -> str:
    """候选人物图来源：`api` 中转站 / `local` 本地 Krea2 / `frame` 源视频取帧 / `auto`。

    `auto` 只在显式配置了中转站时才走 `api`，否则直接用源视频取帧 —— 官方账号没有
    `gpt-image-*` 余额，本地 Krea2 的画质用户已明确不接受。
    """
    value = env_value("H3_BATCH_IMAGE_PROVIDER", "auto").strip().lower()
    return value if value in IMAGE_PROVIDERS else "auto"


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
    await _wait_for_free_pipeline(batch_id, item_id)
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
            "ratio": "4:3",
            "use_rvc": "1",
            "use_upscale": "1",
        }
        endpoint = "/api/jobs"
    else:
        data = {
            "ratio": "9:16",
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
        _set_item(batch_id, item_id, childJob=snapshot)
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


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_size: int, min_size: int = 28) -> ImageFont.FreeTypeFont:
    for size in range(max_size, min_size - 1, -2):
        font = ImageFont.truetype(str(COVER_FONT), size)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
    return ImageFont.truetype(str(COVER_FONT), min_size)


def _cover_background(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    background = ImageOps.fit(source.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(size) / 70))
    background = ImageEnhance.Brightness(background).enhance(0.46)
    return background


def _draw_wrapped(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int], font: ImageFont.FreeTypeFont, spacing: int) -> None:
    x0, y0, x1, _y1 = box
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > x1 - x0:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    stroke = max(2, font.size // 18)
    for line in lines[:3]:
        draw.text(
            (x0, y0),
            line,
            font=font,
            fill="#ffffff",
            stroke_width=stroke,
            stroke_fill="#17112f",
        )
        y0 += font.size + spacing


def render_covers(source_path: Path, headline: str, out_dir: Path) -> tuple[Path, Path]:
    if not COVER_FONT.is_file():
        raise RuntimeError(f"封面字体不存在：{COVER_FONT}")
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as opened:
        source = opened.convert("RGB")

        bilibili = _cover_background(source, (1440, 1080))
        foreground = ImageOps.contain(source, (810, 1000), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", foreground.size, 255)
        bilibili.paste(foreground, (1440 - foreground.width - 42, (1080 - foreground.height) // 2), mask)
        overlay = Image.new("RGBA", bilibili.size, (0, 0, 0, 0))
        gradient = Image.new("L", (760, 1080))
        gd = ImageDraw.Draw(gradient)
        for x in range(760):
            gd.line((x, 0, x, 1080), fill=max(0, 230 - round(x / 760 * 210)))
        overlay.paste((8, 6, 28, 245), (0, 0, 760, 1080), gradient)
        bilibili = Image.alpha_composite(bilibili.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(bilibili)
        title = headline.strip() or "今日作品"
        font = _fit_font(draw, title[:18], 590, 92, 46)
        _draw_wrapped(draw, title, (86, 390, 650, 760), font, 18)
        small = ImageFont.truetype(str(COVER_FONT), 28)
        draw.text((90, 840), "H3 MOTIONSTUDIO", font=small, fill="#62d8e9")
        bili_path = out_dir / "B站封面_4比3.png"
        bilibili.convert("RGB").save(bili_path, quality=96)

        douyin = _cover_background(source, (1080, 1440)).convert("RGBA")
        foreground = ImageOps.contain(source, (990, 1320), method=Image.Resampling.LANCZOS)
        fg_x = (1080 - foreground.width) // 2
        fg_y = max(20, (1440 - foreground.height) // 2 - 35)
        douyin.paste(foreground, (fg_x, fg_y))
        shade = Image.new("RGBA", douyin.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade)
        for y in range(760, 1440):
            alpha = round((y - 760) / 680 * 205)
            sd.line((0, y, 1080, y), fill=(8, 6, 28, alpha))
        douyin = Image.alpha_composite(douyin, shade)
        draw = ImageDraw.Draw(douyin)
        font = _fit_font(draw, title[:18], 870, 88, 46)
        _draw_wrapped(draw, title, (105, 1030, 975, 1370), font, 15)
        douyin_path = out_dir / "抖音封面_3比4.png"
        douyin.convert("RGB").save(douyin_path, quality=96)
    return bili_path, douyin_path


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
    bili, douyin = await asyncio.to_thread(
        render_covers,
        Path(ai["reference_image_path"]),
        str(ai.get("cover_headline") or ai.get("title") or "今日作品"),
        folder,
    )
    outputs["coverBilibili"] = str(bili)
    outputs["coverDouyin"] = str(douyin)
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


def _first_actionable(state: dict[str, Any]) -> dict[str, Any] | None:
    for item in state.get("items") or []:
        if item.get("status") not in {"completed", "skipped", "deleted"}:
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
            item = _first_actionable(state)
            if item is None:
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
            batch_store.update(
                batch_id,
                currentItemId=item["id"],
                currentIndex=item["index"],
                notice=f"正在处理第 {item['index']} / {state['total']} 条。",
            )
            try:
                if item.get("skipRequested"):
                    _set_item(batch_id, item["id"], status="skipped", stage="skipped", finishedAt=now_iso())
                    for milestone in item.get("milestones") or []:
                        if milestone.get("status") == "pending":
                            batch_store.set_item_milestone(batch_id, item["id"], milestone["id"], status="skipped")
                    continue
                if item.get("status") == "awaiting_review":
                    batch_store.update(batch_id, status="awaiting_review", stage="review", runnerActive=False)
                    return
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
                return
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
