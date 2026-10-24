"""图片工作台的手动换帧；不触发人物图或视频生成。"""
import asyncio
import io
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from . import batch_ai, batch_image, batch_worker as worker


def _thumbnail(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB").resize((32, 32))


def _candidates(source: Path, previous: list[bytes]) -> list[bytes]:
    duration = worker._video_duration_seconds(source)
    seen = [_thumbnail(data) for data in previous]
    candidates = []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for step in range(24):
        data = worker._extract_frame_bytes(source, duration * step / 24, flags=flags)
        if not data:
            continue
        thumb = _thumbnail(data)
        if any(sum(ImageStat.Stat(ImageChops.difference(thumb, old)).mean) / 3 < 8 for old in seen):
            continue
        seen.append(thumb)
        candidates.append((worker._frame_detail_score(data), data))
    return [data for _, data in sorted(candidates, reverse=True)[:12]]


def _vision_image(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail((768, 768))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85)
        return output.getvalue()


def _replace(path: Path, data: bytes) -> None:
    if path.suffix.lower() == ".png":
        with Image.open(io.BytesIO(data)) as image:
            output = io.BytesIO()
            image.save(output, format="PNG")
            data = output.getvalue()
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=path.suffix, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def replace_frame(folder: Path) -> None:
    from . import image_workbench as workbench

    if workbench._AUTO_LOCK.locked():
        raise ValueError("图片任务正在处理，请完成后再更换抽帧")
    async with workbench._AUTO_LOCK:
        found = worker._find_item_for_folder(folder)
        if found is None:
            raise ValueError("找不到这条作品的源视频记录，无法更换抽帧")
        batch, item = found
        task = worker._ITEM_TASKS.get((batch["id"], item["id"]))
        if item.get("status") in {"running", "revising"} or (task and not task.done()):
            raise ValueError("本条任务正在处理，请停下后再更换抽帧")
        if (workbench.AUTOMATIONS.get(workbench._folder_id(folder)) or {}).get("status") in {"queued", "running"}:
            raise ValueError("本条图片任务正在处理，请完成后再更换抽帧")
        source = Path(str(item.get("sourcePath") or ""))
        if not source.is_file():
            raise ValueError("本地源视频已不存在，请先补回源视频后再更换抽帧")
        frame = workbench._first(folder, "抽帧.jpg", "抽帧.png", "scene-frame.jpg")
        if frame is None:
            raise ValueError("当前抽帧不存在")
        history = folder / ".h3-frame-history"
        previous = [frame.read_bytes(), *(path.read_bytes() for path in history.glob("*.jpg"))]
        candidates = await asyncio.to_thread(_candidates, source, previous)
        if not candidates:
            raise ValueError("没有找到与已选画面不同的候选帧，已保留原抽帧")
        index = await batch_ai.choose_covered_frame([_vision_image(data) for data in candidates])
        selected = candidates[index - 1]
        # 先验证原整脸糊法能正常定位；成功前不改当前抽帧。
        with tempfile.TemporaryDirectory(prefix="h3-replace-frame-") as raw:
            candidate = Path(raw) / "candidate.jpg"
            candidate.write_bytes(selected)
            await asyncio.to_thread(batch_image.prepare_defocused_reference, candidate, Path(raw) / "guard")
        current = worker._item(batch["id"], item["id"])
        if current.get("status") in {"running", "revising"} or current.get("sourcePath") != item.get("sourcePath"):
            raise ValueError("任务或源视频已变化，未替换抽帧，请停下任务后重试")
        history.mkdir(exist_ok=True)
        (history / f"{uuid.uuid4().hex}.jpg").write_bytes(frame.read_bytes())
        canonical = Path(str((current.get("ai") or {}).get("sceneFramePath") or ""))
        try:
            canonical.resolve().relative_to(worker.DATA_DIR.resolve())
        except ValueError:
            canonical = frame
        targets = [frame]
        if canonical.is_file() and canonical != frame:
            targets.append(canonical)
        originals = {path: path.read_bytes() for path in targets}
        try:
            for path in targets:
                _replace(path, selected)
            worker._set_item(batch["id"], item["id"], ai={**(current.get("ai") or {}), "sceneFramePath": str(canonical if canonical.is_file() else frame)})
        except Exception:
            for path, original in originals.items():
                _replace(path, original)
            raise
        worker.batch_store.add_item_log(batch["id"], item["id"], "已手动更换抽帧：优先选择服装覆盖更完整的新画面；未重新生成人物图或视频。")
