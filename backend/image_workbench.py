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

from PIL import Image, ImageOps

from . import batch_ai, batch_image
from .batch_store import batch_store
from .batch_worker import IDENTITY_PATH
from .settings import BATCH_OUTPUT_ROOT, PROJECT_ROOT


PERSON_NAME = "人物图.png"
COVER_NAMES = {"bilibili": "B站4x3.png", "douyin": "抖音3x4.png"}
LEGACY_COVER_NAMES = {"bilibili": "封面_B站4x3.png", "douyin": "封面_抖音3x4.png"}
TARGETS = {"bilibili": (1448, 1086), "douyin": (1086, 1448)}
# 人物图身份自检：相似度必须 ≥70（用户 2026-09-19：「人物相似度 必须70%以上」），
# 不够就重做，最多 PERSON_MAX_ATTEMPTS 次，保留分数最高的那张。
# ⚠️ 手部**没有**自动判据：三个视觉模型标定下来都不可靠（见 `_generate_person` 的说明），
# 手部只靠提示词约束 + 用户看图后自己点「重新生成这一步」。
PERSON_MIN_SIMILARITY = 70
PERSON_MAX_ATTEMPTS = 3
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
WATCHES: dict[str, dict[str, Any]] = {}
AUTOMATIONS: dict[str, dict[str, Any]] = {}
# 人物图合规自检的一句话结论（按作品目录 id 暂存，`run_automatic` 收尾时拼进状态消息）。
# 不放进 AUTOMATIONS 是因为那条消息每 2 秒被页面读一次，混进状态里会互相覆盖。
_PERSON_NOTES: dict[str, str] = {}
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
- **过度裸露（发布级硬要求）**：按平台规则原文只有三条——① 不得露出事业线/乳沟（胸部必须被
  衣服完整盖住）；② 不得穿透视、裸色到像没穿衣服；③ 不得紧身到凸点这类轮廓线过于突出。
  **吊带、抹胸、一字肩、露肩、露锁骨、露上胸一小部分、露腰腹、短裤、正常修身的针织衫都不算
  违规，必须照常保留**；只是图一若命中上面三条，就换成同一气质但不露事业线、不透、不凸点的款式。
- 不要文字、Logo、水印、边框。输出单张成图。""".strip()
    # 画幅话术只写比例、不写平台名（用户 2026-09-20：封面多平台复用，提示词里出现
    # 「B站」/「抖音」会让模型把对应平台的 logo 画进封面）。
    ratio = "4:3 横版" if stage == "bilibili" else "3:4 竖版"
    title = _copy_field(copy, "标题") or _title(copy, folder.name)
    introduction = _copy_field(copy, "简介")
    return f"""参考人物和背景，将标题、简介融入图片生成 {ratio} 尺寸封面。
不要添加任何平台的 Logo、水印、角标或文字（标题和简介本身除外）。
标题：
{title}
简介：
{introduction}

【手部硬要求 · 用户 2026-09-19：手可以入镜，但不能出问题】
- 人物的姿势与手势**沿用参考图**，不要自行设计新手势（不要比心、不要剪刀手、不要指尖相触）。
- 手可以入镜，但手指必须结构完全正确：五指分明、数量正确、手指不交叉、不打结、不粘连，
  指节弯曲方向与指甲朝向自然；画不准就把手改成简单自然的姿势（自然垂放或轻放身侧）。
- 手部出现任何结构错误都算不合格，必须重画到正确为止。

【过度裸露 · 发布级硬要求】
- 按平台规则原文只有三条：不得露出**事业线/乳沟**、不得**透视/裸色**到像没穿衣服、
  不得紧身到**凸点**这类轮廓线过于突出。服装沿用参考图（人物图已过这道判据），
  不要把领口改低、不要加透视，其余（吊带、露肩、露腰腹、短裤）照旧。""".strip()


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
    )


async def _hands_verdict(candidate: Path) -> tuple[bool, int]:
    """手部自检：返回 (是否合格, 问题条数)。

    判据本身失败（视觉端点不可用等）时**不阻断链路**：按合格放行、不重试 ——
    自检是质量门槛，不是出片前提（与身份自检同一条口径）。
    """
    try:
        result = await batch_ai.judge_hands(candidate)
    except Exception:  # noqa: BLE001 - 自检失败不该把整条链路判失败
        return True, 0
    problems = [str(item) for item in (result.get("problems") or [])]
    return bool(result.get("hands_ok")), len(problems)


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


async def _generate_person(folder: Path, kind: str) -> int:
    """API 出人物图 + 身份自检：**相似度 <70 就重做**（上限 3 次），**留分数最高的那张**。

    用户 2026-09-19：「人物相似度 必须70%以上」。
    返回最终保留那张的相似度；判据本身失败（模型不可用等）时不重试、不卡链路，
    直接收下当次结果并返回 -1 —— 自检只是质量门槛，不是出片前提。

    ⚠️ **手指不再做自动判据**（2026-09-19 实测记录见 `AGENTS.md`）：同一条「手部结构对不对」
    的判据换 DeepSeek / gemini-3.1-pro / gpt-5.6 三个模型标定，结果是「严格提示词把用户认可的
    图也全判不合格、放宽提示词把你指出的坏图也判成合格」——跟着措辞走而不是看图，不能当门槛。
    所以手部靠**提示词约束**（`batch_ai.VIDEO_READY_BLOCK` 禁止比心/剪刀手、要求结构正确），
    出图后由用户在页面上看一眼，不对就点「重新生成这一步」。
    """
    frame = asset_path(folder, "frame")
    ratio = "4:3" if kind == "singing" else "9:16"
    work = Path(tempfile.mkdtemp(prefix="h3-auto-person-"))
    try:
        scene, _ = await batch_image.defocus_reference_face(frame, work)
        candidate = work / "person.png"
        # 生成 + 身份自检（<70 重做、留最高分）+ **着装合规自检**（不合规继续重做）与
        # **批量全自动链路共用同一份实现**：`batch_image.generate_person_image`，
        # 两边的行为不会跑偏。合规结论写进状态消息，页面能看到原因。
        report: dict[str, Any] = {}
        _, similarity = await batch_image.generate_person_image(
            scene_image=scene,
            identity_image=IDENTITY_PATH,
            prompt=person_prompt(folder, kind),
            ratio=ratio,
            output_path=candidate,
            compliance=report,
        )
        note = _compliance_note(report)
        if note:
            _PERSON_NOTES[_folder_id(folder)] = note
        save_upload(folder, "person", candidate.read_bytes(), kind)
        return similarity
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


async def _generate_cover(folder: Path, stage: str, kind: str) -> None:
    """封面走**第三方 API** 出带标题文字的成图（用户 2026-09-19：「封面也用api生成 不需要本地」）。

    提示词用 `prompt(folder, stage, kind)`（里面就是发布文案的标题与简介）；
    参考图 = 这条自己的 `人物图.png`（保住人物与画面）。
    出图比例 4:3（B站）/ 3:4（抖音），`save_upload()` 会按 `TARGETS` 精确裁到
    1448×1086 / 1086×1448 并做比例校验。

    历史上这里曾是「本地确定性合成」（人物图 + 模糊背景垫底），因为中转站对封面类请求
    偶有审核失败；现在改为 API 直出，失败会如实写进 automation.message 由页面显示。
    """
    work = Path(tempfile.mkdtemp(prefix="h3-auto-cover-"))
    try:
        candidate = work / f"{stage}.png"
        await batch_image.generate_candidate_image(
            scene_image=asset_path(folder, "person"),
            # **封面只发这一张参考**（identity_image=None）：再给一张「本人固定参考」的话，
            # 两张图是同一人的不同服装/场景，模型会把**两个人一起画进封面**（实测踩过）。
            identity_image=None,
            prompt=prompt(folder, stage, kind),
            ratio="4:3" if stage == "bilibili" else "3:4",
            output_path=candidate,
        )
        save_upload(folder, stage, candidate.read_bytes())
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


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
                                  message=f"正在生成人物图（相似度需≥{PERSON_MIN_SIMILARITY}）", kind=kind)
                        person_score = await _generate_person(folder, kind)
                    continue
                if not (folder / PERSON_NAME).is_file():
                    raise RuntimeError("请先生成人物图，再做封面")
                if not force and (folder / COVER_NAMES[step]).is_file():
                    continue
                _set_auto(folder_id, status="running", stage=step,
                          message=f"正在生成{label}", kind=kind)
                await _generate_cover(folder, step, kind)
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
                    stage: str | None = None) -> dict[str, Any]:
    if stage not in {None, "person", "bilibili", "douyin"}:
        raise ValueError(f"不支持的步骤：{stage}")
    current = AUTOMATIONS.get(folder_id) or {}
    if current.get("status") in {"queued", "running"}:
        return current
    _clear_stop()  # 用户主动点生成 = 新的一轮意图，清掉上次的停止开关
    asyncio.create_task(run_automatic(folder_id, force=force, stage=stage))
    _set_auto(folder_id, status="queued", stage="waiting", message="正在准备自动生成")
    return AUTOMATIONS[folder_id]


def start_all_missing() -> int:
    _clear_stop()  # 一键生成 = 新的一轮意图
    count = 0
    for item in list_items():
        assets = item["assets"]
        if assets["person"] and assets["bilibili"] and assets["douyin"]:
            continue
        start_automatic(item["id"])
        count += 1
    return count
