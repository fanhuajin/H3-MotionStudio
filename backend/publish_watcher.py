"""backend.publish_watcher — 监听发布目录里的「人物图.png」，自动挂候选图 + 直接确认出片。

用户 2026-09-17：「帮我加一个监听如果 E:\\AI_Exports\\H3-MotionStudio\\发布成品 对应的视频目录
出现了人物图.png 你直接将人物图放到候选人物图 里并直接直接开始 确认并出片 不需要我去点击了」
+ 追问后的确认：「我一定会放一张 人物图.png，如果有你在开始后续的流程，只是简略了我上传而已，
后续流程还是需要跑；如果已经有的你不要重复跑」。

## 它到底做什么

发布目录里出现**一张新的**人物图 → 走页面按钮那两个接口（行为与手动点击一字不差）：

1. `POST /api/batches/{b}/items/{i}/image`：落成该条候选人物图 + 按这张图重写发布文案 +
   刷新发布目录里的「人物图 / 抽帧 / 发布文案」；
2. 条目正停在审核点 → `POST /api/batches/{b}/items/{i}/confirm`：等同点「确认并出片」，
   立刻进单链路出片队列（`_resume_batch` 会唤醒 runner）。

## 什么算「新的一张」（不重复跑的关键）

* 目录里的 `人物图.*` 与本条**当前候选图** sha256 不同 → 需要上传；
* 条目上记着 `ai.portrait_watch.sha`：**这张图已经驱动过一次就再也不动它** ——
  所以「回到确认」把条目退回审核点后不会被这里立刻又放行（你可以安心改比例/换图），
  你换一张新图（sha 变了）才会重新走一遍；
* 条目落到审核点时后端自己会写一份 `人物图`（源视频取帧的**兜底图**，文件名
  `candidate_r{n}_frame.png`）—— 那张不算「人物图」，既不会触发确认，也不会被当成你的成图。

## 按条目状态分工（其余一律不碰）

| 状态 | 动作 |
|---|---|
| `awaiting_review`（审核点） | 需要就上传 → **自动确认出片** |
| `confirmed`（已放行、排队等出片） | 只上传（出片自然用新图），不重复确认 |
| `failed`（出片失败等人工） | 需要就上传 → 自动 `retry` 重跑一次这条（每张图只重跑一次） |
| `pending` / `revising` / `running` | 这一轮不动（还在下载/备料/出片，换图会图文脱节） |
| `skipped` / `completed` / `deleted` | 永不触达（你主动取消的、已经出完片的、删掉的都不许复活） |

## 失败与开关

* 上传/确认失败 → 写条目日志，下一轮重试；**同一张图连续失败 3 次**就停下并标黄等你换图；
* 格式不是 PNG/JPG/WEBP、或超过 25MB → 忽略并写日志（不重试到死）；
* 开关：页面「批量制作」右上角的勾选框（默认开），落盘在 `data/publish_watch.json`；
  环境变量 `H3_WATCH_PUBLISH_PORTRAIT=0/1` 可以**强制**关/开（优先级高于文件）；
  扫描间隔 `H3_WATCH_INTERVAL`（默认 5 秒）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import batch_store, settings
 
logger = logging.getLogger("batch.publish_watcher")
# 这个监听是**后台**跑的：动作只写进条目日志（页面上看得到），但如果连进程日志里都没有
# 一行，出问题时只能靠翻数据库。uvicorn 只配了自己的 logger，别的 logger 的 INFO 会被
# 丢掉（root 没 handler，lastResort 只放 WARNING+），所以这里自己挂一个 handler，
# 输出照样进 `data/backend-stderr.log`（start-local.ps1 重定向的）。
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

PUBLISH_IMAGE_STEM = "人物图"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MAX_IMAGE_BYTES = 25 * 1024 * 1024          # 与上传接口同一条上限
DEFAULT_INTERVAL_SECONDS = 5.0
MAX_ATTEMPTS_PER_IMAGE = 3                  # 同一张图最多重试几次就停下等人工
SETTINGS_FILE = settings.DATA_DIR / "publish_watch.json"

# 上传要等模型按新图重写文案（实测 10~30 秒），给足时间
HTTP_TIMEOUT_SECONDS = 300.0

# 同一张图连续失败次数：key = f"{item_id}|{sha}"
_ATTEMPTS: dict[str, int] = {}
# 已经标黄提醒过、不要再刷日志的 key
_WARNED: set[str] = set()
# 一次只允许一个扫描在跑（页面手动触发的那次也要排队）
_SCAN_LOCK = asyncio.Lock()
_LAST_SCAN: dict[str, Any] = {}


# ---------------- 开关（页面勾选框 + 环境变量强制） ----------------
def _read_settings_file() -> dict[str, Any]:
    try:
        raw = SETTINGS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("监听设置文件读不出来，按默认（开）处理：%s", SETTINGS_FILE)
        return {}


def file_enabled() -> bool | None:
    """落盘的开关值；文件里没写就是 None（用默认）。"""
    value = _read_settings_file().get("enabled")
    return value if isinstance(value, bool) else None


def enabled() -> bool:
    """总开关：环境变量可以**强制**（0/1），否则看页面上的勾选框，默认开。"""
    forced = ""
    try:
        forced = str(settings.env_value("H3_WATCH_PUBLISH_PORTRAIT", "") or "").strip().lower()
    except Exception:  # noqa: BLE001 - 读环境变量失败不该让监听停摆
        forced = ""
    if forced:
        return forced not in {"0", "false", "no", "off"}
    stored = file_enabled()
    return True if stored is None else stored


def set_enabled(value: bool) -> bool:
    """页面勾选框调它：落盘 + 立即生效（下一轮扫描就读到）。"""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {**_read_settings_file(), "enabled": bool(value), "updatedAt": time.strftime(
        "%Y-%m-%dT%H:%M:%S")}
    SETTINGS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("发布目录监听已%s（页面开关）", "打开" if value else "关闭")
    return bool(value)


def interval() -> float:
    try:
        value = float(str(settings.env_value("H3_WATCH_INTERVAL", "") or DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return value if value >= 1.0 else DEFAULT_INTERVAL_SECONDS


# ---------------- 发布目录扫描 ----------------
def store():
    """批次的单例 store（`backend.batch_store.batch_store`，与 batch_worker 用的同一个）。"""
    return batch_store.batch_store


def publish_root() -> Path:
    from . import batch_worker as bw

    return Path(bw.BATCH_OUTPUT_ROOT)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def portrait_in(folder: Path) -> Path | None:
    """目录里的「人物图」：优先 `人物图.png`，也认手工存成 jpg/webp 的。"""
    if not folder.is_dir():
        return None
    exact = folder / f"{PUBLISH_IMAGE_STEM}.png"
    if exact.is_file() and exact.stat().st_size > 0:
        return exact
    for path in sorted(folder.iterdir()):
        try:
            if (
                path.is_file()
                and path.stem == PUBLISH_IMAGE_STEM
                and path.suffix.lower() in IMAGE_SUFFIXES
                and path.stat().st_size > 0
            ):
                return path
        except OSError:
            continue
    return None


def is_fallback_portrait(path: str | Path | None) -> bool:
    """是不是「源视频取帧的兜底图」（备料没拿到真人物图时的占位）。

    兜底图长得像 `candidate_r0_frame.png`；用户上传的是 `candidate_r0_upload.png`，
    本地/AI 换图是 `candidate_r0_swapped.png`。兜底图**不算你的成图**：
    既不会触发自动确认，也不会被当成「目录里已经有你的图」。
    """
    if not path:
        return False
    return Path(str(path)).stem.endswith("_frame")


def _item_image_sha(item: dict[str, Any]) -> str:
    raw = str((item.get("ai") or {}).get("reference_image_path") or "").strip()
    path = Path(raw) if raw else None
    if path is None or not path.is_file():
        return ""
    try:
        return sha256_of(path)
    except OSError:
        return ""


def _watch_marker(item: dict[str, Any]) -> dict[str, Any]:
    marker = (item.get("ai") or {}).get("portrait_watch")
    return marker if isinstance(marker, dict) else {}


def match_item(folder: Path, batches: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """发布目录 → (批次, 条目)。

    先按条目记录里的 `outputs.folder` 精确匹配（最准），再按目录名末尾的**作品号**匹配；
    批次按**新到旧**遍历，同一个作品出现在多个批次里时用最新那条。匹配不到就返回 None
    （历史目录 / 已删除条目，一律不碰）。
    """
    try:
        target = folder.resolve()
    except OSError:
        target = folder
    for batch in batches:
        for item in batch.get("items") or []:
            if str(item.get("status") or "") == "deleted":
                continue
            recorded = str((item.get("outputs") or {}).get("folder") or "").strip()
            if not recorded:
                continue
            try:
                if Path(recorded).resolve() == target:
                    return batch, item
            except OSError:
                continue
    aweme = folder.name.rsplit("_", 1)[-1].strip()
    if aweme.isdigit():
        for batch in batches:
            hits = [
                item
                for item in batch.get("items") or []
                if str(item.get("status") or "") != "deleted"
                and str(item.get("awemeId") or "").strip() == aweme
            ]
            if len(hits) == 1:
                return batch, hits[0]
    return None


@dataclass
class Action:
    """一轮扫描里要执行的一件事。"""

    batch_id: str
    item_id: str
    folder: Path
    image: Path
    sha: str
    mode: str                    # upload_confirm / upload / retry
    title: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.item_id}|{self.sha}"

    def describe(self) -> str:
        return {
            "upload_confirm": "挂候选图并确认出片",
            "upload": "只换候选图（本条已放行，不重复确认）",
            "retry": "挂候选图并重跑这一条",
        }.get(self.mode, self.mode)


def decide(folder: Path, item: dict[str, Any]) -> Action | None:
    """单个条目该怎么处理（纯函数，测试直接打它）。

    返回 None = 这一轮不动它。
    """
    status = str(item.get("status") or "")
    if status in {"pending", "revising", "running", "skipped", "completed", "deleted"}:
        return None
    if not (item.get("ai") or {}):
        return None                                    # 还没备料，别动
    batch_dir = folder
    image = portrait_in(batch_dir)
    if image is None:
        return None
    try:
        size = image.stat().st_size
    except OSError:
        return None
    if size > MAX_IMAGE_BYTES:
        return None                                    # 太大：上传接口也会拒，直接不碰
    if image.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    try:
        sha = sha256_of(image)
    except OSError:
        return None

    marker = _watch_marker(item)
    if marker.get("sha") == sha:
        return None                                    # 这张图已经驱动过一次：绝不重复跑
    candidate = str((item.get("ai") or {}).get("reference_image_path") or "")
    candidate_sha = _item_image_sha(item)
    needs_upload = candidate_sha != sha

    if not needs_upload and is_fallback_portrait(candidate):
        # 目录里那张就是备料留下的兜底图，还没等到你的成图
        return None
    if not needs_upload and status != "awaiting_review":
        return None                                    # 没有新图、也不需要放行

    batch_id = ""
    mode = {
        "awaiting_review": "upload_confirm",
        "confirmed": "upload",
        "failed": "retry",
    }.get(status, "")
    if not mode:
        return None
    if mode == "upload" and not needs_upload:
        return None
    return Action(
        batch_id=batch_id,
        item_id=str(item.get("id") or ""),
        folder=batch_dir,
        image=image,
        sha=sha,
        mode=mode,
        title=str(item.get("title") or folder.name),
        meta={"needs_upload": needs_upload, "status": status},
    )


def pending_actions(batches: list[dict[str, Any]] | None = None, *, root: Path | None = None) -> list[Action]:
    """扫一遍发布根，返回这一轮要执行的动作（纯读，不做任何写操作）。"""
    base = Path(root or publish_root())
    if not base.is_dir():
        return []
    if batches is None:
        batches = store().active_batches()
    actions: list[Action] = []
    for folder in sorted(path for path in base.iterdir() if path.is_dir()):
        found = match_item(folder, batches)
        if not found:
            continue
        batch, item = found
        action = decide(folder, item)
        if action is None:
            continue
        action.batch_id = str(batch.get("id") or "")
        if action.batch_id and action.item_id:
            actions.append(action)
    return actions


# ---------------- 执行 ----------------
async def _post(client, url: str, *, files=None) -> tuple[int, str]:
    response = await client.post(url, files=files)
    return response.status_code, (response.text or "")[:300]


def _mark_watch(action: Action) -> None:
    """在条目上记下「这张图已经处理过」——重启后也不会重复跑。"""

    def apply(row: dict[str, Any]) -> None:
        ai = row.setdefault("ai", {})
        ai["portrait_watch"] = {
            "sha": action.sha,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "action": action.mode,
            "image": action.image.name,
        }

    try:
        store().mutate_item(action.batch_id, action.item_id, apply)
    except Exception:  # noqa: BLE001 - 记账失败不该把已经完成的出片回滚
        logger.exception("写监听标记失败（%s/%s）", action.batch_id[:8], action.item_id[:8])


async def run_action(action: Action, *, client, write_state: bool = True) -> bool:
    """执行一件事：上传候选图 → （按状态）确认 / 重跑。返回是否成功。

    `write_state=False`：**一个字的批次状态都不写**（不记日志、不写 `portrait_watch` 标记）。
    给「后端还没重启、先用独立进程顶一会儿」用 —— 批次状态是「读整份 JSON → 改 → 写回」，
    两个进程同时写会互相覆盖，所以桥接模式下只走那两个 HTTP 接口，绝不碰数据库。
    代价：条目日志里没有「监听：…」那两行（页面仍能看到「已收到你上传的候选图」与状态变化），
    以及「回到确认」后同一张图可能被再放行一次（内置版有 sha 标记，不会有这个问题）。
    """
    url = f"{settings.BATCH_SELF_URL}/api/batches/{action.batch_id}/items/{action.item_id}"
    payload = action.image.read_bytes()
    files = {"file": (action.image.name, payload, "image/png")}
    try:
        status, text = await _post(client, f"{url}/image", files=files)
    except Exception as error:  # noqa: BLE001 - 网络/接口异常都算这次失败
        _note_failure(action, f"上传候选图失败：{type(error).__name__}: {error}",
                      write_state=write_state)
        return False
    if status >= 400:
        _note_failure(action, f"上传候选图被拒（HTTP {status}）：{text}", write_state=write_state)
        return False

    if write_state:
        store().add_item_log(
            action.batch_id, action.item_id,
            f"监听：发布目录里出现了人物图「{action.image.name}」，已自动挂成候选人物图。",
        )
    else:
        logger.info("已挂上候选图：%s｜%s", action.title[:24], action.image.name)
    _ATTEMPTS.pop(action.key, None)
    _WARNED.discard(action.key)

    if action.mode in {"upload_confirm", "retry"}:
        path = "confirm" if action.mode == "upload_confirm" else "retry"
        try:
            status, text = await _post(client, f"{url}/{path}")
        except Exception as error:  # noqa: BLE001
            _note_failure(action, f"自动放行失败：{type(error).__name__}: {error}",
                          write_state=write_state)
            # 图已经挂上了：标记这半件事完成，避免下一轮把图再传一次
            if write_state:
                _mark_watch(action)
            return False
        if status >= 400:
            _note_failure(action, f"自动放行被拒（HTTP {status}）：{text}", write_state=write_state)
            if write_state:
                _mark_watch(action)
            return False
        if write_state:
            store().add_item_log(
                action.batch_id, action.item_id,
                "监听：已自动确认并开始出片（等同你点「确认并出片」）。"
                if action.mode == "upload_confirm" else
                "监听：已自动重跑这一条。",
            )
    if write_state:
        _mark_watch(action)
    logger.info("监听已处理 %s（%s）", action.title[:24], action.describe())
    return True


def _note_failure(action: Action, message: str, *, write_state: bool = True) -> None:
    attempts = _ATTEMPTS.get(action.key, 0) + 1
    _ATTEMPTS[action.key] = attempts
    logger.warning("监听处理失败（第 %d 次）：%s｜%s", attempts, action.title[:24], message)
    if not write_state:
        return
    try:
        store().add_item_log(action.batch_id, action.item_id, f"监听：{message}")
    except Exception:  # noqa: BLE001
        pass
    if attempts >= MAX_ATTEMPTS_PER_IMAGE and action.key not in _WARNED:
        _WARNED.add(action.key)
        try:
            store().mutate_item(
                action.batch_id, action.item_id,
                lambda row: row.update(
                    warning=f"发布目录监听连续 {attempts} 次没能处理这张人物图：{message}"
                ),
            )
        except Exception:  # noqa: BLE001
            pass


async def scan_once(*, client=None, batches: list[dict[str, Any]] | None = None,
                    root: Path | None = None, write_state: bool = True) -> list[Action]:
    """扫一遍并执行（`client`/`batches`/`root`/`write_state` 可注入，测试用）。"""
    global _LAST_SCAN
    if not enabled():
        return []
    actions = pending_actions(batches, root=root)
    done: list[Action] = []
    if not actions:
        _LAST_SCAN = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "handled": 0, "pending": 0}
        return done

    owns_client = client is None
    if owns_client:
        import httpx

        client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
    try:
        for action in actions:
            try:
                if await run_action(action, client=client, write_state=write_state):
                    done.append(action)
            except Exception:  # noqa: BLE001 - 单条失败不影响其它条目
                logger.exception("监听处理 %s 时出错", action.item_id[:8])
    finally:
        if owns_client:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
    _LAST_SCAN = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "handled": len(done),
        "pending": len(actions) - len(done),
    }
    return done


def last_scan() -> dict[str, Any]:
    """最近一次扫描的摘要（给页面显示）。"""
    return dict(_LAST_SCAN)


async def scan_now() -> list[Action]:
    """页面点「立即扫描」时调它（与后台循环共用同一把锁，不会并发）。"""
    async with _SCAN_LOCK:
        return await scan_once()


async def watch_loop() -> None:
    """后台循环：服务起来就一直盯着发布目录。"""
    logger.info(
        "发布目录监听已启动：每 %.0f 秒扫一次 %s（H3_WATCH_PUBLISH_PORTRAIT=0 或页面勾选框可关）",
        interval(), publish_root(),
    )
    while True:
        try:
            async with _SCAN_LOCK:
                await scan_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 一轮失败不能把循环弄死
            logger.exception("发布目录监听这一轮失败（下一轮继续）")
        try:
            await asyncio.sleep(interval())
        except asyncio.CancelledError:
            raise


# ---------------- 独立进程入口（只读体检 / 后端没在跑时的补扫） ----------------
async def backend_has_watcher() -> bool:
    """后端进程里是否已经有内置监听（有 `/api/watch/publish-portrait` 就说明有）。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.BATCH_SELF_URL}/api/watch/publish-portrait")
        return response.status_code == 200
    except Exception:  # noqa: BLE001 - 后端不可达时按「没有」处理
        return False


async def backend_reachable() -> bool:
    """后端在不在跑（批次读接口能通就算在）。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.BATCH_SELF_URL}/api/batches/latest")
        return response.status_code in {200, 204}
    except Exception:  # noqa: BLE001
        return False


async def standalone_loop(*, force: bool = False) -> None:
    """独立监听循环（`python -m backend.publish_watcher --loop`）。

    ⚠️ **桥接用**：后端还没重启带上内置监听时，用它先顶一会儿。
    这个模式下 `write_state=False` —— **一个字的批次状态都不写**（批次状态是「读整份 JSON →
    改 → 写回」，两个进程同时写会互相覆盖），只走上传/确认那两个 HTTP 接口。
    后端一旦带上内置监听，这里检测到接口就自己退出，把活交回去。
    """
    if not force and await backend_has_watcher():
        logger.info("后端已内置发布目录监听，独立进程退出，交给它继续。")
        return
    logger.info("独立监听进程启动（每 %.0f 秒扫一次，只调接口、不写批次状态）", interval())
    while True:
        try:
            async with _SCAN_LOCK:
                await scan_once(write_state=False)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("独立监听这一轮失败（下一轮继续）")
        await asyncio.sleep(interval())


def _status_lines() -> list[str]:
    lines = [
        f"开关：{'开' if enabled() else '关'}（环境变量 H3_WATCH_PUBLISH_PORTRAIT 可强制）",
        f"间隔：{interval():.0f} 秒",
        f"发布根：{publish_root()}（存在={publish_root().is_dir()}）",
    ]
    actions = pending_actions() if enabled() else []
    lines.append(f"这一轮待处理：{len(actions)} 件")
    for action in actions:
        lines.append(f"  · [{action.item_id[:8]}] {action.title[:28]} → {action.describe()}"
                     f"（{action.image}）")
    return lines


def main(argv: list[str] | None = None) -> int:
    """命令行：`--status` 只看状态（只读）、`--once` 扫一次并执行、`--loop` 常驻独立监听。"""
    import argparse
    import asyncio as asyncio_module

    parser = argparse.ArgumentParser(description="发布目录监听：人物图.png → 候选图 → 自动确认出片")
    parser.add_argument("--status", action="store_true", help="只打印状态与待处理清单（只读）")
    parser.add_argument("--once", action="store_true", help="扫一次并执行（后端在跑时要加 --force）")
    parser.add_argument("--loop", action="store_true", help="常驻独立监听（后端接管后自动退出）")
    parser.add_argument("--force", action="store_true", help="后端在跑也强行执行（有覆盖状态的风险）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    if args.status or not (args.once or args.loop):
        for line in _status_lines():
            print(line)
        return 0
    if args.once:
        if not args.force and asyncio_module.run(backend_reachable()):
            print("后端正在运行：请用页面上的「监听发布目录人物图」开关，或重启后端让内置监听接管；"
                  "确实要单独跑请加 --force。")
            return 0
        done = asyncio_module.run(scan_now())
        print(f"处理了 {len(done)} 件：")
        for action in done:
            print(f"  · {action.title[:28]} → {action.describe()}")
        return 0
    try:
        asyncio_module.run(standalone_loop(force=args.force))
    except KeyboardInterrupt:
        print("已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
