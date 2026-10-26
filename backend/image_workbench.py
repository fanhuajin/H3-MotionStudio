from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

import httpx

from . import batch_ai, batch_image, cover_review, source_appearance
from . import batch_worker as batch_worker
from .batch_store import batch_store
from .batch_worker import IDENTITY_PATH
from .settings import BATCH_OUTPUT_ROOT, PUBLISH_KIND_DIRS, DATA_DIR

logger = logging.getLogger("uvicorn.error")


PERSON_NAME = "人物图.png"
COVER_NAMES = {"bilibili": "B站4x3.png", "douyin": "抖音3x4.png"}
LEGACY_COVER_NAMES = {"bilibili": "封面_B站4x3.png", "douyin": "封面_抖音3x4.png"}
TARGETS = {"bilibili": (1448, 1086), "douyin": (1086, 1448)}
# 人物图完成后仍检查 70 分门槛，但默认只生成一次；低分由用户明确决定是否重做。
# 手部只在提示词中约束，成图问题由用户自行处理，不自动校验或重绘。
PERSON_MIN_SIMILARITY = batch_image.PERSON_MIN_SIMILARITY
PERSON_MAX_ATTEMPTS = batch_image.PERSON_MAX_ATTEMPTS
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
WATCHES: dict[str, dict[str, Any]] = {}
AUTOMATIONS: dict[str, dict[str, Any]] = {}
# 人物图合规自检的一句话结论（按作品目录 id 暂存，`run_automatic` 收尾时拼进状态消息）。
# 不放进 AUTOMATIONS 是因为那条消息每 2 秒被页面读一次，混进状态里会互相覆盖。
_PERSON_NOTES: dict[str, str] = {}
_AUTO_LOCK = asyncio.Lock()
# 批量后台与图片工作台会从不同入口触发封面；按「作品目录 + 平台」合并同一时刻的请求。
_COVER_INFLIGHT: dict[tuple[str, str], asyncio.Future[tuple[bool, str]]] = {}


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
    # 作品编号只编码**目录名**（URL 因此稳定），所以要在两层里找：2026-09-20 起作品目录在
    # `singing/` `dance/` 子目录里，旧的扁平结构也要认。允许的父目录就是根与两个子目录，
    # 任何解析到别处的编号（`../` 之类）一律拒绝。
    allowed = {root}
    candidates = [root / name]
    for sub in PUBLISH_KIND_DIRS.values():
        base = root / sub
        allowed.add(base.resolve())
        candidates.append(base / name)
    for candidate in candidates:
        folder = candidate.resolve()
        if folder.parent in allowed and folder.is_dir():
            return folder
    raise FileNotFoundError("作品目录不存在")


def _meta(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            width, height = image.size
        stat = path.stat()
        return {"name": path.name, "width": width, "height": height, "bytes": stat.st_size,
                "version": f"{stat.st_mtime_ns}-{stat.st_size}"}
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


def _publish_folders() -> list[Path]:
    """发布根下所有**作品目录**：`singing/`、`dance/` 两层，外加旧的扁平结构兜底。

    2026-09-20 用户：「在发布目录里加一个跳舞文件夹 一个唱歌文件夹各自放对应的内容」——
    `/images` 页的作品列表因此要扫两层。排序固定成"先唱歌、再跳舞、各自按编号"，
    和批量页的直觉一致（老结构里没有子目录，直接按名字排）。
    """
    if not BATCH_OUTPUT_ROOT.is_dir():
        return []
    folders: list[tuple[int, str, Path]] = []
    for order, sub in enumerate(PUBLISH_KIND_DIRS.values()):
        base = BATCH_OUTPUT_ROOT / sub
        if base.is_dir():
            folders.extend((order, path.name, path) for path in base.iterdir() if path.is_dir())
    legacy = [(len(PUBLISH_KIND_DIRS), path.name, path)
              for path in BATCH_OUTPUT_ROOT.iterdir() if path.is_dir()]
    return [path for _, _, path in sorted(folders + legacy, key=lambda row: (row[0], row[1]))]


def expose_failed_items() -> None:
    # 旧批次可能在备料失败时尚未写发布目录；打开页面时补齐上传入口。
    latest = batch_store.latest()
    if latest:
        for row in latest.get("items") or []:
            if row.get("status") == "failed" and not row.get("deleteRequested"):
                try:
                    batch_worker._expose_failed_item_in_image_workbench(latest["id"], row["id"])
                except OSError:
                    logger.exception("补建异常条目的图片上传入口失败：%s", row.get("id"))


def list_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for folder in _publish_folders():
        copy = _copy_text(folder)
        frame = _first(folder, "抽帧.jpg", "抽帧.png", "scene-frame.jpg")
        person = _first(folder, PERSON_NAME)
        # Only the current canonical names count as complete. Legacy cover names remain readable
        # through asset_path, but the workbench must still offer to create the required outputs.
        bili = _first(folder, COVER_NAMES["bilibili"])
        douyin = _first(folder, COVER_NAMES["douyin"])
        if not any((copy, frame, person, bili, douyin)):
            continue
        has_video = (folder / "最终成片.mp4").is_file()
        items.append({
            "id": _folder_id(folder),
            "folderName": folder.name,
            "title": _title(copy, folder.name),
            "copy": copy,
            "kind": infer_kind(folder),
            "automation": AUTOMATIONS.get(_folder_id(folder)),
            "hasVideo": has_video,
            "canCover": bool(person and (folder / "发布文案.txt").is_file()),
            "coverReview": cover_review.read_report(person, folder / ".h3-cover-hands.json") if person else None,
            "assets": {
                "frame": _meta(frame), "identity": _meta(IDENTITY_PATH), "person": _meta(person),
                "bilibili": _meta(bili), "douyin": _meta(douyin),
            },
        })
    # Batch records exist before review materials reach the publishing folder.
    # Expose them without creating placeholder files or changing batch state.
    latest = batch_store.latest()
    by_id = {row["id"]: row for row in items}
    for row in (latest or {}).get("items") or []:
        if row.get("status") in {"deleted", "skipped"} or row.get("deleteRequested"):
            continue
        folder = (batch_worker._existing_publish_folder(row)
                  or batch_worker._recorded_publish_folder(row)
                  or batch_worker._ideal_publish_folder(row))
        key = _folder_id(folder)
        live = row.get("status") not in {"completed", "failed"}
        if key in by_id:
            by_id[key]["batchLive"] = live
            continue
        ai = row.get("ai") or {}
        entry = {
            "id": key, "folderName": folder.name,
            "title": str(ai.get("title") or row.get("title") or f"第 {row.get('index')} 条"),
            "copy": "", "kind": row.get("kind"),
            "automation": None, "awaitingMaterials": True, "batchLive": live,
            "hasVideo": False, "canCover": False,
            "assets": {name: None for name in ("frame", "identity", "person", "bilibili", "douyin")},
        }
        items.append(entry)
        by_id[key] = entry
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
    if key == "frame":
        return batch_image.prepare_defocused_reference(path, folder / ".h3-reference-guard")
    return path


def preview_path(path: Path) -> Path:
    """Small, versioned display copy. Originals remain the source for viewing/copying."""
    stat = path.stat()
    token = hashlib.sha256(f"{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
    target = DATA_DIR / "image-workbench-previews" / f"{token}.webp"
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        image.thumbnail((960, 960), Image.Resampling.LANCZOS)
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".webp", delete=False) as temporary:
            staging = Path(temporary.name)
        try:
            image.save(staging, "WEBP", quality=82, method=4)
            os.replace(staging, target)
        finally:
            staging.unlink(missing_ok=True)
    return target


def prompt(folder: Path, stage: str, kind: str = "singing", *, hand_report: dict[str, Any] | None = None, failure_fallback: bool = False) -> str:
    copy = _copy_text(folder)
    if stage == "person":
        return person_prompt(folder, kind)
    # 画幅话术只写比例、不写平台名（用户 2026-09-20：封面多平台复用，提示词里出现
    # 「B站」/「抖音」会让模型把对应平台的 logo 画进封面）。
    # 原人物图单参考；手部正常但贴边时保留动作并留足边距，异常时调整姿态。
    ratio = "4:3 横版" if stage == "bilibili" else "3:4 竖版"
    title = _copy_field(copy, "标题") or _title(copy, folder.name)
    introduction = _copy_field(copy, "简介")
    if batch_ai.has_disallowed_copy(title) or batch_ai.has_disallowed_copy(introduction):
        raise ValueError("发布标题或简介含不适合发布的表达，请先修改发布文案后再生成封面；未提交新图片请求")
    person = _first(folder, PERSON_NAME)
    report = hand_report or (cover_review.read_report(person, folder / ".h3-cover-hands.json") if person else None)
    # B站恢复脸部突出的近景；抖音继续沿用当前拉远半身构图。
    framing_scale = (
        "头部与肩颈为主体，脸部突出，画面下边缘裁至胸部附近，头顶保留自然边距，不拉远至腰部。"
        if stage == "bilibili" else
        "双肩总宽约占画面宽度的一半，最多不超过六成，头顶留出自然余量。"
    )
    avoid_hands_crop = (
        "保持参考人物的脸部五官、发型、发饰、服装、场景与光线，采用胸部以上近景构图，"
        "人物偏左，右侧留出连续的文字空间；"
        f"{framing_scale}"
        if stage == "bilibili" else
        "保持参考人物的脸部五官、发型、发饰、服装、场景与光线，改为适度拉远的半身构图，展示头部至腰部附近，"
        "人物偏左，右侧留出连续的文字空间；完整保留双肩，"
        f"{framing_scale}"
        "头顶和两侧留有自然边距，不裁切肩膀，不放大头脸，不用贴脸特写或肩部以上近景来避手。"
    )
    needs_pose_adjustment = failure_fallback or cover_review.hide_hands(report)
    needs_hand_margin = not needs_pose_adjustment and report.get("handsNearOrOutsideFrame") != "no"
    framing = (
        avoid_hands_crop +
        "若参考图的手臂横伸出画，或手或手臂举到胸前、肩旁或脸旁，不得仅靠裁切隐藏；将双臂调整为自然下垂；画面包含袖口或手腕时，必须自然连接完整的手掌和手指，双手完整入画并留足边距。需要近景时在远离手腕的前臂或上臂处自然裁切，使袖口、手腕和双手一起位于画外，不在袖口或手腕处截断。"
        "自然补全原先被手或手臂遮挡的衣服、背景及必要的人物轮廓，不改变人物五官身份；"
        "不残留手指、手腕、截断手掌或突兀的手臂，不得只删除手掌而留下断臂。"
        if needs_pose_adjustment else
        "保留原图人物的五官身份、发型、发饰、表情、服装、场景与光线，保留原有举手或伸臂动作及手部结构、左右对应和遮挡关系。"
        "适当缩小人物并重新安排位置，必要时只延展同一场景的背景，使原图可见的双臂、手腕、手掌和全部手指完整入画，手指与四周边缘至少留出画面对应边长5%的安全距离。"
        "不为了避开边缘放下手臂、删除手掌、藏手或改变动作，不在袖口和手腕处截断，不新增肢体；文字避让人物和手部。"
        if needs_hand_margin else
        "保留原图人物的脸部五官、发型、发饰、表情、视线、姿态、服装、背景与光线，保持人物原有比例和位置，不重新摆姿势。"
        "原图中已经可见的手臂、双手和手指保持原样，不改变动作、位置、形态和遮挡关系，不重画已有手部。"
        "在目标画幅内安排文字留白，不扩展背景，不缩小人物来增加留白。"
        "不扩展人物肢体，不补画原图画外的手掌或手指。"
        "不得出现空袖口、手腕突然截断、双手消失或新增手臂，不得只删除手掌而留下断臂。"
        "仍处于最终画幅之外，或因双手背在身后、被身体或衣物自然遮挡而不可见的手部，保持原有遮挡关系，无需补出或强行展示，不为露出双手改变姿态。"
        "不为排字删除、遮盖或改变人物及手部；留白不足时优先移动文字、换行或缩小字号，不通过扩展背景增加留白。"
    )
    background_rule = "仅为完整容纳手部调整构图和延展背景，不为增加文字空间继续缩小人物。" if needs_hand_margin else "不扩展背景，不为增加文字空间拉远或缩小人物。"
    return f"""仅以这张原人物图为参考制作 {ratio} 封面。{framing}{background_rule}只将下列文字自然融入画面。
标题：{title}
简介：{introduction}
标题采用自然有手写感的毛笔字，重点词用协调的颜色突出，搭配少量自然笔触或手绘下划线；简介用清楚易读的小字轻量融入画面，不使用厚重边框。文字准确完整，不重复，不改写标题与简介。
标题、简介及装饰放在连续留白处，避开人物的脸、头发、发饰、手指、手臂和服装轮廓，保留适当间距；留白不足时优先移动、换行或缩小文字。
画面仍只有参考图中的一人。除标题和简介外，不添加其他文字、平台 Logo、水印或角标。""".strip()


def material_zip(folder: Path, stage: str, kind: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("提示词.txt", prompt(folder, stage, kind))
        if stage == "person":
            for filename, key in (("图一_已糊脸抽帧.png", "frame"), ("图二_固定人物参考.png", "identity")):
                archive.write(asset_path(folder, key), filename)
        else:
            archive.write(asset_path(folder, "person"), PERSON_NAME)
            copy_path = folder / "发布文案.txt"
            if copy_path.is_file():
                archive.write(copy_path, "发布文案.txt")
    return output.getvalue()


_WAKE_TASKS: set[asyncio.Task] = set()


def _register_published_portrait(folder: Path) -> dict[str, Any] | None:
    """人物图落进发布目录后**当场**登记成对应条目的候选图。

    用户 2026-09-20 选了「谁写图谁登记」：`/images` 出图 / 上传成功后直接更新条目，
    不再靠每 5 秒扫一遍发布目录的中间人（`backend/publish_watcher.py` 已删除）。
    登记失败只记日志 —— 绝不能影响出图结果本身。
    """
    try:
        return batch_worker.adopt_published_portrait(folder)
    except Exception:  # noqa: BLE001
        logger.exception("登记发布目录人物图失败（忽略）")
        return None


async def _post_batch_start(batch_id: str) -> None:
    """自调用 `POST /api/batches/{id}/start` 叫醒那一批（与页面点「准备任务」同一条路）。"""
    from .settings import BATCH_SELF_URL

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{BATCH_SELF_URL}/api/batches/{batch_id}/start")
    except Exception:  # noqa: BLE001
        logger.warning("登记候选图后叫醒批次失败（忽略）", exc_info=True)


def _wake_batch_soon(batch_id: str) -> None:
    """在当前事件循环里排一个「叫醒批次」的任务；没有循环（同步调用/测试）就只登记。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_post_batch_start(batch_id))
    _WAKE_TASKS.add(task)
    task.add_done_callback(_WAKE_TASKS.discard)


def save_upload(
    folder: Path, stage: str, payload: bytes, kind: str = "singing", *, allow_repair: bool = False,
    preserve_cover: bool = False,
) -> dict[str, Any]:
    if stage not in {"person", "bilibili", "douyin"}:
        raise ValueError("图片类型无效")
    try:
        with Image.open(io.BytesIO(payload)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except OSError as exc:
        raise ValueError("请选择有效的 PNG、JPG 或 WebP 图片") from exc
    width, height = image.size
    original_cover = preserve_cover and stage in COVER_NAMES
    if allow_repair and not original_cover and min(width, height) < 640:
        # 我们自己的低分辨率补放大：放大是内存里的任务，后端在排队
        # 期间被杀就没了 —— 这时不能把这张**已经付过钱**的图判成「分辨率太低」，本地补一刀。
        image, note = batch_image.ensure_deliverable_image(image)
        if note:
            width, height = image.size
            logger.warning("出图短边不足，已本地补到 %sx%s（%s）", width, height, stage)
    if not original_cover and min(width, height) < 640:
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
        if not original_cover and abs(actual - expected) / expected > 0.035:
            ratio = "4:3" if stage == "bilibili" else "3:4"
            raise ValueError(f"画面比例不正确，需要 {ratio}")
        if not original_cover:
            image = ImageOps.fit(image, (tw, th), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        destination = folder / COVER_NAMES[stage]
    handle, tmp_name = tempfile.mkstemp(prefix=".image-workbench-", suffix=".png", dir=folder)
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        if original_cover:
            # 中转站封面直接采用原始字节；不放大、不裁切、不重新编码。
            tmp.write_bytes(payload)
        else:
            image.save(tmp, "PNG", optimize=True)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)
    meta = _meta(destination) or {}
    if stage == "person":
        # 人物图是天生的「候选图」来源：落进发布目录就当场登记到条目上（谁写图谁登记）。
        outcome = _register_published_portrait(folder)
        if outcome:
            meta["adopted"] = outcome
            if outcome.get("needsWake"):
                _wake_batch_soon(str(outcome.get("batchId") or ""))
    else:
        # 封面落盘 = 这份文案已经被画进图里 → **持久锁死**（2026-09-20）。
        # 以前只靠"封面文件还在不在"判断，删封面去重出会把锁一起解掉、标题被重写。
        try:
            batch_worker.mark_publish_copy_locked(folder)
        except Exception:  # noqa: BLE001 - 锁标记失败不该影响图片保存
            logger.exception("标记发布文案锁失败（忽略）")
        recovered = batch_worker.resume_failed_item_with_complete_images(folder)
        if recovered:
            meta["recovered"] = recovered
            _wake_batch_soon(recovered["batchId"])
    return meta


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


AUTO_LIVE_STATUSES = {"queued", "running"}


def _set_auto(folder_id: str, **changes: Any) -> None:
    current = dict(AUTOMATIONS.get(folder_id) or {})
    # 本轮开始时间由**后端**记：页面切走再切回来会重建组件、丢掉本地计时，
    # 于是「已用 N 秒」会从 0 重新数（看起来像重跑了一遍，用户 2026-09-19 问过）。
    # 记在这里之后，切换/刷新页面都按真实开始时间接着数。
    status = str(changes.get("status") or "")
    was_live = str(current.get("status") or "") in AUTO_LIVE_STATUSES
    if status in AUTO_LIVE_STATUSES and not was_live:
        current["startedAt"] = time.time()
    current.update(changes)
    if str(current.get("status") or "") in AUTO_LIVE_STATUSES and not current.get("startedAt"):
        current["startedAt"] = time.time()
    current["updatedAt"] = time.time()
    AUTOMATIONS[folder_id] = current


# 自动出图的「停止」开关（用户 2026-09-19：「启动后如果我要暂停怎么办」）。
# 之前一键生成启动后**没有任何停止途径** —— 排队里的任务只能等它跑完，或者重启后端
# （而任务状态在内存里，重启会把它整个丢掉）。这里给一个软停止：
#   · 正在跑的那一步**跑完就停**（中转站的 HTTP 请求没法中断，硬砍会留下半张图）；
#   · 还在排队的条目立刻转成 cancelled，不再往下走；
#   · 下一次用户主动点生成时自动清掉这个开关。
_STOP = {"requested": False}


def stop_automatic() -> int:
    """请求停止自动出图，返回被取消的排队条目数。"""
    _STOP["requested"] = True
    cancelled = 0
    for folder_id, state in list(AUTOMATIONS.items()):
        if str(state.get("status") or "") == "queued":
            _set_auto(folder_id, status="cancelled", stage="stopped", message="已停止（排队已清空）")
            cancelled += 1
    return cancelled


def _stop_requested() -> bool:
    return bool(_STOP.get("requested"))


def _clear_stop() -> None:
    _STOP["requested"] = False


def person_prompt(folder: Path, kind: str) -> str:
    """自动出图、复制与素材包共用当前精简人物图提示词。"""
    song_name, song_mood = "", ""
    aweme = folder.name.rsplit("_", 1)[-1].strip()
    for item in batch_store.items_for_aweme(aweme) or []:
        ai = item.get("ai") or {}
        song_name = song_name or str(ai.get("song_name") or "")
        song_mood = song_mood or str(ai.get("song_mood") or "")
    value = batch_ai.compose_image_prompt(
        kind, "video", "", "both",
        song_name=song_name,
        song_mood=song_mood,
        ratio="4:3" if kind == "singing" else "9:16",
    )

    frame = _first(folder, "抽帧.jpg", "抽帧.png", "scene-frame.jpg")
    return source_appearance.enrich_cached(frame, value) if frame else value


async def prepare_person_materials(folder: Path) -> None:
    """用户取人物图提示词或素材包时，补齐糊脸前的造型记录。"""
    frame = _first(folder, "抽帧.jpg", "抽帧.png", "scene-frame.jpg")
    if frame is not None:
        await source_appearance.review(frame)


def _compliance_note(report: dict[str, Any]) -> str:
    """把合规自检结果变成一句给用户看的话（拼进 AUTOMATIONS 的收尾消息）。"""
    compliant = report.get("compliant")
    if compliant is None:
        return ""
    if compliant:
        return "着装合规自检已通过"
    issues = "、".join(str(item) for item in (report.get("issues") or [])[:3]) or "疑似擦边"
    return (
        f"⚠️ 着装合规自检未通过（{issues}）：本次所有尝试都不合规，"
        "建议点「重新生成这一步」，或换一个源视频"
    )


async def _generate_person(
    folder: Path, kind: str, *, allow_uncertain_retry: bool = False
) -> int:
    """API 生成人物图后直接保存，不进行自动自检。"""
    frame = _first(folder, "抽帧.jpg", "抽帧.png", "scene-frame.jpg")
    if frame is None:
        raise FileNotFoundError("抽帧不存在")
    ratio = "4:3" if kind == "singing" else "9:16"
    # 必须放在作品目录里，不能用系统临时目录：后端重启会清掉临时路径里的 pending，
    # 下一轮就会误以为从未提交过并再次扣费。成功后才清理；失败/取消则留下检查点。
    work = folder / ".h3-person-work"
    if allow_uncertain_retry and work.is_dir():
        work.rename(work.with_name(f"{work.name}.previous-unknown-{uuid.uuid4().hex[:8]}"))
    work.mkdir(parents=True, exist_ok=True)
    completed = False
    try:
        candidate = work / "person.png"
        image_prompt = await source_appearance.prepare_prompt(
            frame, person_prompt(folder, kind), candidate
        )
        scene, defocused = await batch_image.defocus_reference_face(frame, work)
        if not defocused:
            raise ValueError("抽帧未成功糊脸，已停止人物图生成。")
        # 与批量备料共用流程。合规结论写进状态消息，页面能看到原因。
        report: dict[str, Any] = {}
        _, similarity = await batch_image.generate_person_image(
            scene_image=scene,
            identity_image=IDENTITY_PATH,
            prompt=image_prompt,
            ratio=ratio,
            output_path=candidate,
            compliance=report,
            request_key=f"person:{folder.name.rsplit('_', 1)[-1].strip()}:{ratio}",
        )
        note = _compliance_note(report)
        appearance = source_appearance.cached(frame)
        if appearance and appearance.get("status") != "completed":
            note = "；".join(filter(None, (
                note, "抽帧发型与配饰未能确认，未猜测隐藏装饰；未自动重复识别",
            )))
        if note:
            _PERSON_NOTES[_folder_id(folder)] = note
        save_upload(folder, "person", candidate.read_bytes(), kind, allow_repair=True)
        completed = True
        return similarity
    finally:
        if completed:
            import shutil
            shutil.rmtree(work, ignore_errors=True)


async def _generate_cover(
    folder: Path, stage: str, kind: str, *, allow_uncertain_retry: bool = False
) -> None:
    """用原人物图检查手部并生成封面，不等待成片；旧付费任务保持原输入续取。"""
    key = (str(folder.resolve()).lower(), stage)
    existing = _COVER_INFLIGHT.get(key)
    if existing is not None:
        ok, detail = await asyncio.shield(existing)
        if ok:
            return
        raise RuntimeError(detail)

    loop = asyncio.get_running_loop()
    completion: asyncio.Future[tuple[bool, str]] = loop.create_future()
    _COVER_INFLIGHT[key] = completion
    marker = folder / f".h3-{stage}-request-pending"
    destination = folder / COVER_NAMES[stage]
    # 请求和原图必须跨失败/重启保留；只有最终封面可靠保存后才清理。
    work = folder / f".h3-{stage}-work"
    candidate = work / f"{stage}.png"
    checkpoint = batch_image._task_checkpoint(candidate)
    inputs = work / "request.json"
    completed = False
    try:
        work.mkdir(parents=True, exist_ok=True)
        ready = False
        if candidate.is_file():
            try:
                with Image.open(candidate) as image:
                    image.load()
                ready = True
            except OSError:
                pass  # 下载中断的文件不当成功；有任务记录则只续取原任务。
        if marker.is_file() and not allow_uncertain_retry:
            # 若新封面比 marker 更新，说明上次已经成功落盘、只是进程没来得及清 marker。
            if (not checkpoint.is_file() and not ready and destination.is_file()
                    and destination.stat().st_mtime_ns > marker.stat().st_mtime_ns):
                marker.unlink(missing_ok=True)
                completed = True
                completion.set_result((True, ""))
                return
            if not ready and not checkpoint.is_file():
                raise RuntimeError(
                    f"上一次{stage}封面请求已提交但结果未确认；为避免重复扣费，本次没有自动重发。"
                    "请核对中转站后台后，在图片工作台明确点击这一步重做"
                )
        # 用户主动重试允许新请求；旧任务和冻结输入先归档，已下载原图仍直接保存。
        if allow_uncertain_retry and not ready:
            work.rename(work.with_name(f"{work.name}.previous-unknown-{uuid.uuid4().hex[:8]}"))
            work.mkdir()
            if marker.is_file():
                marker.rename(marker.with_name(f"{marker.name}.previous-unknown-{uuid.uuid4().hex[:8]}"))
        if not ready:
            if not inputs.is_file():
                if checkpoint.is_file():
                    raise RuntimeError("封面任务记录缺少原始输入，请核对渠道后台；未重新提交生成")
                reference = asset_path(folder, "person")
                frozen = work / "reference.png"
                frozen.write_bytes(reference.read_bytes())
                # 先冻结实际提交的图片，再检查；两种比例共用同内容的检查结果。
                report = await cover_review.review(frozen, folder / ".h3-cover-hands.json")
                base_prompt = prompt(folder, stage, kind, hand_report=report)
                snapshot = {"reference": frozen.name, "prompt": base_prompt,
                            "handReview": report, "ratio": "4:3" if stage == "bilibili" else "3:4"}
                # 截指输入首次使用备用构图；正常手部贴边只调整边距，仍保留一次终态失败备用机会。
                if report.get("fingersCutByFrame") in {"yes", "uncertain"}:
                    snapshot["failureFallback"] = True
                temporary = inputs.with_suffix(".tmp")
                temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, inputs)
            snapshot = json.loads(inputs.read_text(encoding="utf-8"))
            if not marker.is_file():
                marker.write_text(json.dumps({"submittedAt": time.time(), "stage": stage}), encoding="utf-8")
            # 新请求冻结原人物图；历史请求可含双参考，
            # 续取时保持原参考和文案，避免改图/改文案使原任务指纹不匹配。
            while True:
                try:
                    await batch_image.generate_candidate_image(
                        scene_image=work / snapshot["reference"],
                        identity_image=work / snapshot["identity"] if snapshot.get("identity") else None,
                        prompt=snapshot["prompt"],
                        ratio=snapshot["ratio"],
                        output_path=candidate,
                        request_key=f"cover:{str(folder.resolve()).lower()}:{stage}",
                        upscale=False,
                        retain_checkpoint=True,
                    )
                    break
                except RuntimeError:
                    # Only a provider-confirmed terminal failure permits a fresh paid request.
                    # Timeouts, query/download errors and unknown outcomes retain the original task.
                    record = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.is_file() else {}
                    if not record.get("terminalFailure") or snapshot.get("failureFallback"):
                        raise
                    logger.warning("%s 封面任务明确失败，换用避手半身构图备用提示词重试一次：%s", stage, folder.name)
                    (work / "failed-task.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                    reference = asset_path(folder, "person")
                    frozen = work / "fallback-reference.png"
                    frozen.write_bytes(reference.read_bytes())
                    snapshot = {
                        "reference": frozen.name,
                        "prompt": prompt(folder, stage, kind, failure_fallback=True),
                        "ratio": "4:3" if stage == "bilibili" else "3:4",
                        "failureFallback": True,
                    }
                    temporary = inputs.with_suffix(".tmp")
                    temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
                    os.replace(temporary, inputs)
                    batch_image._clear_task_checkpoint(candidate)
        save_upload(folder, stage, candidate.read_bytes(), preserve_cover=True)
        completed = True
        marker.unlink(missing_ok=True)
        completion.set_result((True, ""))
    except batch_image.PolicyRejected as exc:
        # 明确的 400 拒绝没有待确认结果；允许用户之后在异常区点「重试」。
        marker.unlink(missing_ok=True)
        completed = True
        if not completion.done():
            completion.set_result((False, str(exc)))
        raise
    except BaseException as exc:
        # 同一请求被计费去重层合并时，明确的政策 400 可能被转成 RuntimeError；
        # 结果已确定为拒绝，不能留下“结果未知”标记阻止用户日后主动重试。
        if "图片接口错误 400" in str(exc) and "内容政策" in str(exc):
            marker.unlink(missing_ok=True)
            completed = True
        if not completion.done():
            completion.set_result((False, str(exc)))
        raise
    finally:
        _COVER_INFLIGHT.pop(key, None)
        if completed:
            import shutil
            shutil.rmtree(work, ignore_errors=True)


async def run_automatic(folder_id: str, *, force: bool = False,
                        stage: str | None = None, retry_missing: bool = False) -> None:
    """自动出图。`stage` 给定就**只跑那一步**（用户在页面上单独点某一张）。

    stage ∈ {None(整条), "person", "bilibili", "douyin"}。
    人物图与文案齐备后即可生成封面，先检查原人物图手部再决定构图。
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
            person_score = -1
            for step in steps:
                # 用户点了「停止」：正在跑的那一步跑完就停，后面的步骤不再开始。
                if _stop_requested():
                    _set_auto(folder_id, status="cancelled", stage="stopped",
                              message="已停止（这一步跑完就停）", kind=kind)
                    return
                label = {"bilibili": "B站封面", "douyin": "抖音封面"}.get(step, "人物图")
                if step == "person":
                    if force or not (folder / PERSON_NAME).is_file():
                        _set_auto(folder_id, status="running", stage="person",
                                  message="正在生成人物图（生成后直接使用，不做自检）", kind=kind)
                        person_score = await _generate_person(
                            folder, kind,
                            allow_uncertain_retry=retry_missing or bool(force and stage == "person"),
                        )
                    continue
                if not force and (folder / COVER_NAMES[step]).is_file():
                    continue
                _set_auto(folder_id, status="running", stage=step,
                          message=f"正在检查人物图手部并生成{label}（异常或不确定时改为不露手半身构图）", kind=kind)
                with batch_image.report_request_status(
                    lambda message: _set_auto(folder_id, message=message)
                ):
                    await _generate_cover(
                        folder, step, kind,
                        # 用户明确重提缺图或单步重做时归档旧请求，自动恢复仍续取。
                        allow_uncertain_retry=retry_missing or bool(force and stage == step),
                    )
            done = "人物图与封面已自动补齐" if not stage else "已完成生成"
            if stage in (None, "person") and person_score >= 0:
                done += f"（人物图相似度 {person_score}/100）"
            note = _PERSON_NOTES.pop(folder_id, "")
            if note:
                done += f"；{note}"
            _set_auto(folder_id, status="completed", stage="done", message=done, kind=kind)
        except Exception as exc:  # noqa: BLE001 - status is surfaced to the page
            _set_auto(folder_id, status="failed", message=str(exc), kind=kind)


def start_automatic(folder_id: str, *, force: bool = False,
                    stage: str | None = None, retry_missing: bool = False) -> dict[str, Any]:
    if stage not in {None, "person", "bilibili", "douyin"}:
        raise ValueError(f"不支持的步骤：{stage}")
    current = AUTOMATIONS.get(folder_id) or {}
    if current.get("status") in {"queued", "running"}:
        return current
    _clear_stop()  # 用户主动点生成 = 新的一轮意图，清掉上次的停止开关
    asyncio.create_task(run_automatic(folder_id, force=force, stage=stage, retry_missing=retry_missing))
    _set_auto(folder_id, status="queued", stage="waiting", message="正在准备自动生成")
    return AUTOMATIONS[folder_id]


def start_all_missing() -> dict[str, int]:
    """一键补齐人物图与封面；已有素材不重做，无需等待成片。"""
    _clear_stop()  # 一键生成 = 新的一轮意图
    queued = 0
    waiting = 0
    for item in list_items():
        assets = item["assets"]
        need_person = not assets["person"]
        need_covers = not (assets["bilibili"] and assets["douyin"])
        if not need_person and not need_covers:
            continue
        if need_covers and not need_person and not item.get("canCover"):
            waiting += 1
            continue
        start_automatic(item["id"])
        queued += 1
    return {"queued": queued, "waiting": waiting}
