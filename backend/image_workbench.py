from __future__ import annotations

import asyncio
import io
import json
import os
import re
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from . import batch_ai, batch_image
from .batch_store import batch_store
from .batch_worker import IDENTITY_PATH
from .settings import BATCH_OUTPUT_ROOT, PROJECT_ROOT


PERSON_NAME = "人物图.png"
COVER_NAMES = {"bilibili": "B站4x3.png", "douyin": "抖音3x4.png"}
LEGACY_COVER_NAMES = {"bilibili": "封面_B站4x3.png", "douyin": "封面_抖音3x4.png"}
TARGETS = {"bilibili": (1448, 1086), "douyin": (1086, 1448)}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
WATCHES: dict[str, dict[str, Any]] = {}
AUTOMATIONS: dict[str, dict[str, Any]] = {}
_AUTO_LOCK = asyncio.Lock()


def _folder_id(folder: Path) -> str:
    return folder.name.encode("utf-8").hex()


def resolve_folder(folder_id: str) -> Path:
    if not _SAFE_ID.fullmatch(folder_id or ""):
        raise ValueError("作品编号无效")
    try:
        name = bytes.fromhex(folder_id).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("作品编号无效") from exc
    root = BATCH_OUTPUT_ROOT.resolve()
    folder = (root / name).resolve()
    if folder.parent != root or not folder.is_dir():
        raise FileNotFoundError("作品目录不存在")
    return folder


def _meta(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            width, height = image.size
        return {"name": path.name, "width": width, "height": height, "bytes": path.stat().st_size}
    except OSError:
        return None


def _first(folder: Path, *names: str) -> Path | None:
    for name in names:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def _copy_text(folder: Path) -> str:
    path = folder / "发布文案.txt"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="replace").strip()


def _title(copy: str, fallback: str) -> str:
    for line in copy.splitlines():
        value = re.sub(r"^(标题|题目)\s*[：:]\s*", "", line.strip())
        if value and not value.startswith("#"):
            return value[:46]
    parts = fallback.split("_", 2)
    if len(parts) > 1:
        # 发布目录名现在是 `编号_唱歌或跳舞_标题_作品号`：第二段若是类型标签就跳过，
        # 第三段（`标题_作品号`）里再去掉作品号（2026-09-18 加了类型标签后这里会取到「唱歌」）。
        title = parts[1]
        if title in {"唱歌", "跳舞"} and len(parts) > 2:
            title = parts[2].rsplit("_", 1)[0]
        return title[:46]
    return fallback[:46]


def _copy_field(copy: str, name: str) -> str:
    """Read one field from 发布文案.txt without carrying the following field."""
    match = re.search(
        rf"(?:^|\n)\s*{re.escape(name)}\s*[：:]\s*(.*?)(?=\n\s*(?:标题|题目|简介|标签)\s*[：:]|\Z)",
        copy,
        re.S,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def infer_kind(folder: Path) -> str | None:
    aweme_id = folder.name.rsplit("_", 1)[-1]
    rows = batch_store.items_for_aweme(aweme_id)
    kind = str((rows[0] if rows else {}).get("kind") or "")
    return kind if kind in {"singing", "dance"} else None


def list_items() -> list[dict[str, Any]]:
    if not BATCH_OUTPUT_ROOT.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for folder in sorted((p for p in BATCH_OUTPUT_ROOT.iterdir() if p.is_dir()), key=lambda p: p.name):
        copy = _copy_text(folder)
        frame = _first(folder, "抽帧.jpg", "抽帧.png", "scene-frame.jpg")
        person = _first(folder, PERSON_NAME)
        # Only the current canonical names count as complete. Legacy cover names remain readable
        # through asset_path, but the workbench must still offer to create the required outputs.
        bili = _first(folder, COVER_NAMES["bilibili"])
        douyin = _first(folder, COVER_NAMES["douyin"])
        if not any((copy, frame, person, bili, douyin)):
            continue
        items.append({
            "id": _folder_id(folder),
            "folderName": folder.name,
            "title": _title(copy, folder.name),
            "copy": copy,
            "kind": infer_kind(folder),
            "automation": AUTOMATIONS.get(_folder_id(folder)),
            "assets": {
                "frame": _meta(frame), "identity": _meta(IDENTITY_PATH), "person": _meta(person),
                "bilibili": _meta(bili), "douyin": _meta(douyin),
            },
        })
    return items


def asset_path(folder: Path, key: str) -> Path:
    choices = {
        "frame": _first(folder, "抽帧.jpg", "抽帧.png", "scene-frame.jpg"),
        "identity": IDENTITY_PATH if IDENTITY_PATH.is_file() else None,
        "person": _first(folder, PERSON_NAME),
        "bilibili": _first(folder, COVER_NAMES["bilibili"], LEGACY_COVER_NAMES["bilibili"]),
        "douyin": _first(folder, COVER_NAMES["douyin"], LEGACY_COVER_NAMES["douyin"]),
    }
    path = choices.get(key)
    if not path or not path.is_file():
        raise FileNotFoundError("图片不存在")
    return path


def _base_person_prompt(kind: str) -> str:
    desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop" / ("4比3图片.txt" if kind == "singing" else "9比16图片.txt")
    fallback = PROJECT_ROOT / "backend" / "prompts" / ("4x3唱歌构图.txt" if kind == "singing" else "9x16跳舞构图.txt")
    source = desktop if desktop.is_file() else fallback
    return source.read_text(encoding="utf-8-sig", errors="replace").strip() if source.is_file() else ""


def prompt(folder: Path, stage: str, kind: str = "singing") -> str:
    copy = _copy_text(folder)
    if stage == "person":
        ratio = "4:3 横版" if kind == "singing" else "9:16 竖版"
        activity = "正在自然演唱" if kind == "singing" else "正在自然跳舞"
        return f"""{_base_person_prompt(kind)}

请编辑生成一张适合 ComfyUI 后续生成视频的写实人物图。
- 图一（抽帧）决定场景、背景、灯光、镜头、姿态和服饰风格，尽量保持一致；若服饰过于普通，只做符合原造型与场景的精致化升级。
- 图二（固定人物参考）只决定人物身份与五官；眼睛、鼻子、嘴唇、脸型、下巴必须稳定自然，禁止混入图一人物的五官。
- 人物状态：{activity}；画幅：{ratio}。
- 皮肤保留真实纹理，避免塑料感、过度磨皮和明显 AI 味；头发、牙齿、下巴边缘自然。
- 手可以入镜，但必须清晰、结构正确、五指自然，禁止模糊、粘连、多指、少指。
- 背景必须可用于视频延展：结构稳定、不过度虚化、不堆叠碎钻光点或密集装饰。
- 不要文字、Logo、水印、边框。输出单张成图。""".strip()
    platform = "B站 4:3" if stage == "bilibili" else "抖音 3:4"
    title = _copy_field(copy, "标题") or _title(copy, folder.name)
    introduction = _copy_field(copy, "简介")
    return f"""参考人物和背景，将标题、简介融入图片生成 {platform} 尺寸封面。
标题：
{title}
简介：
{introduction}""".strip()


def material_zip(folder: Path, stage: str, kind: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("提示词.txt", prompt(folder, stage, kind))
        if stage == "person":
            for filename, key in (("图一_抽帧.jpg", "frame"), ("图二_固定人物参考.png", "identity")):
                try:
                    archive.write(asset_path(folder, key), filename)
                except FileNotFoundError:
                    pass
        else:
            archive.write(asset_path(folder, "person"), PERSON_NAME)
            copy_path = folder / "发布文案.txt"
            if copy_path.is_file():
                archive.write(copy_path, "发布文案.txt")
    return output.getvalue()


def save_upload(folder: Path, stage: str, payload: bytes, kind: str = "singing") -> dict[str, Any]:
    if stage not in {"person", "bilibili", "douyin"}:
        raise ValueError("图片类型无效")
    try:
        with Image.open(io.BytesIO(payload)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except OSError as exc:
        raise ValueError("请选择有效的 PNG、JPG 或 WebP 图片") from exc
    width, height = image.size
    if min(width, height) < 640:
        raise ValueError("图片分辨率太低，短边至少需要 640 像素")
    if stage == "person":
        expected = 4 / 3 if kind == "singing" else 9 / 16
        if abs((width / height) - expected) / expected > 0.035:
            ratio = "4:3" if kind == "singing" else "9:16"
            raise ValueError(f"人物图比例不正确，当前类型需要 {ratio}")
        destination = folder / PERSON_NAME
    else:
        tw, th = TARGETS[stage]
        expected, actual = tw / th, width / height
        if abs(actual - expected) / expected > 0.035:
            ratio = "4:3" if stage == "bilibili" else "3:4"
            raise ValueError(f"画面比例不正确，需要 {ratio}")
        image = ImageOps.fit(image, (tw, th), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        destination = folder / COVER_NAMES[stage]
    handle, tmp_name = tempfile.mkstemp(prefix=".image-workbench-", suffix=".png", dir=folder)
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        image.save(tmp, "PNG", optimize=True)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)
    return _meta(destination) or {}


async def watch_downloads(folder: Path, stage: str, kind: str = "singing", timeout: int = 600) -> str:
    """Wait for a browser download to finish, validate it, and land it in the selected work folder."""
    downloads = Path(os.environ.get("USERPROFILE", "")) / "Downloads"
    watch_id = uuid.uuid4().hex
    started = time.time()
    WATCHES[watch_id] = {"status": "waiting", "stage": stage, "message": "正在等待 ChatGPT 下载成图……"}

    async def run() -> None:
        seen_size: dict[Path, int] = {}
        try:
            while time.time() - started < timeout:
                candidates = []
                if downloads.is_dir():
                    for path in downloads.iterdir():
                        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                            try:
                                if path.stat().st_mtime >= started - 1:
                                    candidates.append(path)
                            except OSError:
                                continue
                for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
                    try:
                        size = path.stat().st_size
                        if size < 10_000 or seen_size.get(path) != size:
                            seen_size[path] = size
                            continue
                        saved = save_upload(folder, stage, path.read_bytes(), kind)
                        WATCHES[watch_id] = {"status": "completed", "stage": stage, "message": "检测到下载成图，已自动保存。", "saved": saved}
                        return
                    except (OSError, ValueError) as exc:
                        WATCHES[watch_id] = {"status": "invalid", "stage": stage, "message": str(exc)}
                        return
                await asyncio.sleep(1.2)
            WATCHES[watch_id] = {"status": "timeout", "stage": stage, "message": "等待超时，没有检测到新的下载图片。"}
        except Exception as exc:  # noqa: BLE001 - surface watcher failures in the page
            WATCHES[watch_id] = {"status": "error", "stage": stage, "message": f"监听下载失败：{exc}"}

    asyncio.create_task(run())
    return watch_id


def _set_auto(folder_id: str, **changes: Any) -> None:
    current = dict(AUTOMATIONS.get(folder_id) or {})
    current.update(changes)
    current["updatedAt"] = time.time()
    AUTOMATIONS[folder_id] = current


def person_prompt(folder: Path, kind: str) -> str:
    """人物图提示词：**和批量备料用的是同一份**（`batch_ai.compose_image_prompt`）。

    用户 2026-09-19 实测后明确：「按照你刚刚生成的图片流程来 你刚刚生成的很好」——
    那一版是「造型提示词文件（`9x16跳舞构图.txt` / `4x3唱歌构图.txt`）+ 构图规格 +
    歌曲信息 + 末尾身份优先级块」，约 5400 字，效果明显优于本模块原先的精简版。
    所以**自动出图**改用这一份；`prompt()` 原样保留给「复制提示词 / 下载素材包」两个
    手动入口，不改动用户已有的手工流程。
    """
    song_name, song_mood = "", ""
    aweme = folder.name.rsplit("_", 1)[-1].strip()
    for item in batch_store.items_for_aweme(aweme) or []:
        ai = item.get("ai") or {}
        song_name = song_name or str(ai.get("song_name") or "")
        song_mood = song_mood or str(ai.get("song_mood") or "")
    return batch_ai.compose_image_prompt(
        kind, "video", "", "both",
        song_name=song_name,
        song_mood=song_mood,
        ratio="4:3" if kind == "singing" else "9:16",
    ) + (
        # 用户 2026-09-19：「对比度记得也要」——影调/对比度也要跟着图一，别自己加滤镜。
        "\n\n【影调与对比度】画面整体的明暗层次、黑白场与对比度**参考图一**："
        "保留图一原有的明暗关系与对比强度，不要过曝、不要把暗部提灰、不要压暗成灰平，"
        "也不要自行添加高对比滤镜、HDR 质感或强烈调色；肤色与背景的明暗关系保持自然。"
    )


async def _generate_person(folder: Path, kind: str) -> None:
    frame = asset_path(folder, "frame")
    ratio = "4:3" if kind == "singing" else "9:16"
    work = Path(tempfile.mkdtemp(prefix="h3-auto-person-"))
    try:
        scene, _ = await batch_image.defocus_reference_face(frame, work)
        candidate = work / "candidate.png"
        await batch_image.generate_candidate_image(
            scene_image=scene,
            identity_image=IDENTITY_PATH,
            prompt=person_prompt(folder, kind),
            ratio=ratio,
            output_path=candidate,
        )
        save_upload(folder, "person", candidate.read_bytes(), kind)
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


async def _generate_cover(folder: Path, stage: str) -> None:
    # Covers must never redraw the approved person. Build them deterministically
    # from 人物图.png: a softly extended version of the same scene supplies the
    # canvas, while the original remains sharp in front. This also avoids relay
    # moderation failures and keeps face, clothing and background consistent.
    target_size = TARGETS[stage]
    with Image.open(asset_path(folder, "person")) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
    background = ImageOps.fit(source, target_size, method=Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(target_size) / 90))
    background = ImageEnhance.Brightness(background).enhance(0.72)
    foreground = ImageOps.contain(source, target_size, method=Image.Resampling.LANCZOS)
    left = (target_size[0] - foreground.width) // 2
    top = (target_size[1] - foreground.height) // 2
    background.paste(foreground, (left, top))
    payload = io.BytesIO()
    background.save(payload, format="PNG", optimize=True)
    save_upload(folder, stage, payload.getvalue())


async def run_automatic(folder_id: str, *, force: bool = False,
                        stage: str | None = None) -> None:
    """自动出图。`stage` 给定就**只跑那一步**（用户在页面上单独点某一张）。

    stage ∈ {None(整条三步), "person", "bilibili", "douyin"}；封面**必须先有合格人物图**，
    否则直接失败并把原因写进状态（页面上那两个按钮也据此置灰）。
    """
    folder = resolve_folder(folder_id)
    kind = infer_kind(folder)
    if not kind:
        _set_auto(folder_id, status="failed", stage="type", message="无法从批量任务确定唱歌/跳舞类型")
        return
    steps = [stage] if stage else ["person", "bilibili", "douyin"]
    _set_auto(folder_id, status="queued", stage="waiting", message="已加入自动生成队列", kind=kind)
    async with _AUTO_LOCK:
        try:
            for step in steps:
                label = {"bilibili": "B站封面", "douyin": "抖音封面"}.get(step, "人物图")
                if step == "person":
                    if force or not (folder / PERSON_NAME).is_file():
                        _set_auto(folder_id, status="running", stage="person",
                                  message="正在生成2K人物图", kind=kind)
                        await _generate_person(folder, kind)
                    continue
                if not (folder / PERSON_NAME).is_file():
                    raise RuntimeError("请先生成人物图，再做封面")
                if not force and (folder / COVER_NAMES[step]).is_file():
                    continue
                _set_auto(folder_id, status="running", stage=step,
                          message=f"正在生成{label}", kind=kind)
                await _generate_cover(folder, step)
            done = "人物图与封面已自动补齐" if not stage else "已完成生成"
            _set_auto(folder_id, status="completed", stage="done", message=done, kind=kind)
        except Exception as exc:  # noqa: BLE001 - status is surfaced to the page
            _set_auto(folder_id, status="failed", message=str(exc), kind=kind)


def start_automatic(folder_id: str, *, force: bool = False,
                    stage: str | None = None) -> dict[str, Any]:
    if stage not in {None, "person", "bilibili", "douyin"}:
        raise ValueError(f"不支持的步骤：{stage}")
    current = AUTOMATIONS.get(folder_id) or {}
    if current.get("status") in {"queued", "running"}:
        return current
    asyncio.create_task(run_automatic(folder_id, force=force, stage=stage))
    _set_auto(folder_id, status="queued", stage="waiting", message="正在准备自动生成")
    return AUTOMATIONS[folder_id]


def start_all_missing() -> int:
    count = 0
    for item in list_items():
        assets = item["assets"]
        if assets["person"] and assets["bilibili"] and assets["douyin"]:
            continue
        start_automatic(item["id"])
        count += 1
    return count
