"""backend.chatgpt_image — 图片流程自动化：候选人物图 + 封面（走 ChatGPT 桌面端）。

只做图片这一环（用户 2026-09 确认）：批量制作其他环节都已完成，只补图片。

**单链路**：所有图片生成（候选人物图 + 封面）共用 `_IMAGE_LOCK`，
同一时刻只跑一条，前一条没跑完下一条不开始 —— 绝不并发。

实现方式见 `backend.chatgpt_desktop`（窗口置前 + 剪贴板粘贴 + 点发送 + 从查看器另存）。
CDP 与 UIA 两条路都已实测排除：
  - CDP：真实登录 profile 下应用不开放远程调试端口；
  - UIA：Chromium 不把网页内容暴露给无障碍树。

开关（默认关闭，不影响原 manual 模式）：
  H3_AUTO_CHATGPT_IMAGE=1   备料后自动生成候选人物图
  H3_AUTO_CHATGPT_COVER=1   落盘后自动生成封面
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from . import chatgpt_cdp as cd
from . import settings

logger = logging.getLogger("batch.chatgpt_image")

# 全局图片锁：人物图与封面共用，严格一条一条完成
_IMAGE_LOCK = asyncio.Lock()

IDENTITY_IMAGE = Path(r"E:\AI_Assets\PortraitIdentity\本人固定参考.png")

# 出图提示词用**用户桌面上那两份**（用户 2026-09-15 指定）：
#   唱歌（4:3）→ 4比3图片.txt    跳舞（9:16）→ 9比16图片.txt
# 不再用项目自动拼的「出图提示词.txt」（那份与用户手写的规则不一致）。
DEFAULT_PROMPT_43 = Path(r"C:\Users\admin\Desktop\4比3图片.txt")
DEFAULT_PROMPT_916 = Path(r"C:\Users\admin\Desktop\9比16图片.txt")


def prompt_for_kind(kind: str) -> str:
    """按条目类型取用户桌面上的提示词；文件缺失时返回空串（由调用方退回项目提示词）。"""
    if str(kind) == "dance":
        path = Path(settings.env_value("H3_CHATGPT_PROMPT_916", str(DEFAULT_PROMPT_916)))
    else:
        path = Path(settings.env_value("H3_CHATGPT_PROMPT_43", str(DEFAULT_PROMPT_43)))
    if path.is_file():
        return path.read_text(encoding="utf-8")
    logger.warning("提示词文件不存在，将退回项目生成的出图提示词：%s", path)
    return ""


# ---------------- 开关 ----------------
def auto_candidate_enabled() -> bool:
    return settings.env_value("H3_AUTO_CHATGPT_IMAGE", "0").strip() == "1"


def auto_cover_enabled() -> bool:
    return settings.env_value("H3_AUTO_CHATGPT_COVER", "0").strip() == "1"


def is_generating(batch_id: str, item_id: str) -> bool:
    """该条当前是否正在让 AI 生成候选图（前端据此显示「正在生成…」）。"""
    from . import batch_worker as bw
    try:
        item = bw._item(batch_id, item_id)
        return bool((item.get("ai") or {}).get("image_generating"))
    except Exception:
        return False


# ---------------- 内部工具 ----------------
def _work_dir(batch_id: str, item_id: str) -> Path:
    from . import batch_worker as bw
    return bw.DATA_DIR / "batches" / batch_id / item_id


def _out_path(batch_id: str, item_id: str, tag: str = "") -> Path:
    """生成图落盘路径（E 盘指定目录）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{batch_id[:8]}_{item_id[:8]}{('_' + tag) if tag else ''}_{stamp}.png"
    return cd.image_dir() / name


def _upload_item_image(batch_id: str, item_id: str, image_path: str) -> None:
    """把成图上传回该条目（后端会重写文案并把人物图写进发布目录）。"""
    import httpx
    with open(image_path, "rb") as fh:
        files = {"file": (Path(image_path).name, fh, "image/png")}
        resp = httpx.post(
            f"{settings.BATCH_SELF_URL}/api/batches/{batch_id}/items/{item_id}/image",
            files=files, timeout=180,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"上传成图失败 {resp.status_code}: {resp.text[:200]}")


def _mark(item_id: str, batch_id: str, **changes) -> None:
    from . import batch_worker as bw
    try:
        bw._set_item(batch_id, item_id, **changes)
    except Exception:
        pass


# ---------------- 候选人物图 ----------------
async def auto_generate_candidate_image(batch_id: str, item_id: str) -> None:
    """备料后自动生成候选人物图并上传。失败只标黄，不抛断调用方。"""
    async with _IMAGE_LOCK:
        await asyncio.to_thread(_generate_candidate_sync, batch_id, item_id, "")


async def auto_regenerate_candidate_image(batch_id: str, item_id: str, feedback: str = "") -> None:
    """「让 AI 换一张」：重新生成候选图并替换。生成期间打 image_generating 标记。"""
    from . import batch_worker as bw
    _set_generating(batch_id, item_id, True)
    bw.batch_store.add_item_log(
        batch_id, item_id,
        f"正在让 AI 重新生成候选人物图{('：' + feedback) if feedback else ''}……",
    )
    try:
        async with _IMAGE_LOCK:
            await asyncio.to_thread(_generate_candidate_sync, batch_id, item_id, feedback)
    except asyncio.CancelledError:
        raise
    finally:
        _set_generating(batch_id, item_id, False)


def _set_generating(batch_id: str, item_id: str, value: bool) -> None:
    from . import batch_worker as bw
    try:
        item = bw._item(batch_id, item_id)
        ai = dict(item.get("ai") or {})
        if value:
            ai["image_generating"] = True
        else:
            ai.pop("image_generating", None)
        bw._set_item(batch_id, item_id, ai=ai)
    except Exception:
        pass


def _generate_candidate_sync(batch_id: str, item_id: str, feedback: str = "") -> None:
    from . import batch_worker as bw
    work = _work_dir(batch_id, item_id)
    scene = work / "scene-frame.jpg"
    prompt_file = work / "出图提示词.txt"
    item = bw._item(batch_id, item_id)

    if not scene.is_file() or not IDENTITY_IMAGE.is_file():
        bw.batch_store.add_item_log(
            batch_id, item_id,
            f"自动生成图被跳过：素材不全（图一={scene.is_file()} "
            f"图二={IDENTITY_IMAGE.is_file()}）。"
        )
        return

    # 优先用用户桌面上的提示词（唱歌 4:3 / 跳舞 9:16）；缺失才退回项目生成的那份
    prompt = prompt_for_kind(str(item.get("kind") or "singing"))
    if not prompt:
        if not prompt_file.is_file():
            bw.batch_store.add_item_log(
                batch_id, item_id, "自动生成图被跳过：找不到提示词（桌面文件与项目提示词都没有）。"
            )
            return
        prompt = prompt_file.read_text(encoding="utf-8")
    else:
        bw.batch_store.add_item_log(
            batch_id, item_id,
            "使用你桌面上的提示词：" + ("9比16图片.txt" if str(item.get("kind")) == "dance" else "4比3图片.txt"),
        )

    images: list[Path] = [scene, IDENTITY_IMAGE]
    if feedback:
        # 带意见：把当前候选图也带上，让 AI 基于这张改
        cur = Path(str((item.get("ai") or {}).get("reference_image_path") or ""))
        if cur.is_file():
            images = [cur, IDENTITY_IMAGE]
        prompt = prompt.rstrip() + f"\n\n【修改意见】{feedback}"

    out = _out_path(batch_id, item_id, "反馈" if feedback else "")
    try:
        got = cd.generate(images, prompt, save_to=out)
        if not got:
            raise RuntimeError("桌面端未产出图片（生成超时或取图失败）")
        _upload_item_image(batch_id, item_id, str(got))
        bw.batch_store.add_item_log(
            batch_id, item_id,
            f"已生成候选人物图并上传：{got}" + (f"（按意见：{feedback}）" if feedback else ""),
        )
    except Exception as exc:  # noqa: BLE001 - 标黄停下等人工，绝不用差图顶替
        logger.exception("生成人物图失败（条目 %s/%s）", batch_id, item_id)
        _mark(item_id, batch_id, warning=str(exc))
        bw.batch_store.add_item_log(
            batch_id, item_id, f"生成人物图失败，请人工处理：{exc}"
        )


# ---------------- 封面 ----------------
COVER_TASKS = [
    ("B站4:3", "请根据这份发布文案和人物图，生成一张 B站 4:3 横版封面图。", "封面_B站4x3.png"),
    ("抖音3:4", "请根据这份发布文案和人物图，生成一张 抖音 3:4 竖版封面图。", "封面_抖音3x4.png"),
]


async def auto_generate_covers(publish_folder: str, copy_file: str, image_file: str) -> None:
    """生成两张封面（先 B站4:3，再 抖音3:4，严格串行）。受同一把图片锁保护。"""
    async with _IMAGE_LOCK:
        await asyncio.to_thread(_covers_sync, publish_folder, copy_file, image_file)


def _covers_sync(publish_folder: str, copy_file: str, image_file: str) -> None:
    """生成两张封面：**在同一个对话里**依次发两条不同的指令（用户 2026-09-15 要求）。

    用户原话：「B站4:3 → 抖音3:4，串行。不要新开窗口，在同一个窗口发布不同的文案就可以了，
    AI 会理解的。」——所以只有第一条开新对话，第二条直接在同一个对话里继续发指令
    （图已经在上下文里了，不必重贴）。
    """
    folder = Path(publish_folder)
    prompt_base = Path(copy_file).read_text(encoding="utf-8", errors="replace")
    for i, (tag, instruction, fname) in enumerate(COVER_TASKS):  # 顺序写死，不做成可并行
        target = folder / fname
        try:
            imgs = [image_file] if i == 0 else []  # 第二张复用上文里的图
            got = cd.generate(
                imgs,
                f"{prompt_base}\n\n{instruction}",
                save_to=target,
                new_chat=(i == 0),  # 只有第一条开新对话，其余在同一对话里继续
            )
            if got:
                logger.info("封面已生成 %s -> %s", tag, got)
        except Exception:
            logger.exception("生成封面失败（%s）", tag)
