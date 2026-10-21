"""三端网页队列：仅显式开始时启动桌面发布进程。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
import psutil

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .settings import BATCH_OUTPUT_ROOT, DATA_DIR, PROJECT_ROOT

PLATFORMS = ("douyin", "xiaohongshu", "bilibili")
STATES = {"待发布", "不发布", "已完成", "待核对", "发布中", "失败"}
CHINA_TIME = timezone(timedelta(hours=8))
ENGINE = PROJECT_ROOT / "backend" / "creator_publish"


def execution_python():
    runtime = DATA_DIR / "publisher" / "runtime" / "Scripts" / "python.exe"
    return str(runtime) if runtime.exists() else sys.executable


def dependencies_ready():
    if execution_python() == sys.executable:
        return all(importlib.util.find_spec(n) is not None for n in ("windows_mcp", "pyperclip"))
    site = DATA_DIR / "publisher" / "runtime" / "Lib" / "site-packages"
    return all((site / n).is_dir() for n in ("windows_mcp", "pyperclip"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.05)


def normalize_item(saved):
    folder = str(Path(saved["folder"]).resolve())
    states = {p: saved.get("states", {}).get(p, "待发布") for p in PLATFORMS}
    if any(s not in STATES for s in states.values()):
        raise ValueError("队列含有未知平台状态，未导入。")
    details = deepcopy(saved.get("details", {}))
    for p in PLATFORMS:
        if states[p] == "发布中":
            states[p] = "待核对" if details.get(p, {}).get("submitted") else "失败"
    date = saved.get("date", (datetime.now(CHINA_TIME) + timedelta(days=1)).strftime("%Y-%m-%d"))
    hour = str(saved.get("hour", "01")).zfill(2)
    datetime.strptime(f"{date} {hour}", "%Y-%m-%d %H")
    return dict(saved, id=hashlib.sha256(folder.casefold().encode()).hexdigest()[:20],
                folder=folder, date=date, hour=hour, scheduled=bool(saved.get("scheduled", True)),
                states=states, details=details)


def assets(folder: Path):
    """只读列出作品素材；各平台的精确限制仍由原引擎在执行前检查。"""
    if not folder.is_dir():
        raise ValueError("作品目录不存在。")
    preferred = folder / "最终成片.mp4"
    videos = [preferred] if preferred.is_file() else [
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".mkv")
    ]
    video = videos[0] if len(videos) == 1 else None
    def first(names):
        return next((folder / n for n in names if (folder / n).is_file()), None)
    return {
        "video": video,
        "copy": first(("发布文案.txt",)),
        "bilibili": first(("B站4x3.png", "封面.png", "封面.jpg")),
        "portrait": first(("抖音3x4.png",)),
    }


class PublishStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "queue.json"
        self.control_path = directory / "control.json"
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None

    def read(self):
        if not self.path.exists():
            return {"items": [], "phase": "idle", "notice": "添加作品，安排时间后开始发布。"}
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(state.get("items"), list):
            raise ValueError("发布队列文件损坏，已停止写入。")
        return state

    def active(self):
        if self.process is not None and self.process.poll() is None:
            return True
        # 后端热重载时不把尚未退出的旧执行进程当作空闲并覆盖其状态。
        pid = self.read().get("workerPid")
        if not pid:
            return False
        try:
            command = psutil.Process(pid).cmdline()
            return str(ENGINE / "worker.py") in command and str(self.path) in command
        except (psutil.Error, OSError):
            return False

    def snapshot(self):
        with self.lock:
            state = self.read()
            interrupted = state.get("phase") in ("starting", "running", "paused") or any(
                "发布中" in i.get("states", {}).values() for i in state["items"]
            )
            if not self.active() and interrupted:
                state["items"] = [normalize_item(i) for i in state["items"]]
                state.update(phase="idle", notice="上次发布已中断；已提交但未确认的记录需先核对。")
                write_json(self.path, state)
            return state

    def mutate(self, operation):
        with self.lock:
            if self.active():
                raise ValueError("请先停止发布，再修改队列。")
            state = self.snapshot()
            operation(state)
            write_json(self.path, state)
            return state

    def start(self, platform):
        with self.lock:
            if _PICKER_LOCK.locked():
                raise ValueError("请先完成作品文件夹选择，再开始发布。")
            if self.active():
                raise ValueError("发布正在运行，请勿重复开始。")
            if not dependencies_ready():
                raise ValueError("发布依赖尚未安装，请安装 backend/requirements-publish.txt。")
            state = self.snapshot()
            platforms = (platform,) if platform else PLATFORMS
            if any(i["states"][p] == "待核对" for i in state["items"] for p in platforms):
                raise ValueError("所选平台有待核对记录，请先核对发布结果。")
            if not any(i["states"][p] in ("待发布", "失败") for i in state["items"] for p in platforms):
                raise ValueError("所选平台没有待发布作品。")
            import os
            state.update(phase="starting", notice="正在检查素材、时间与桌面发布环境…", ownerPid=os.getpid())
            write_json(self.path, state)
            self.control_path.unlink(missing_ok=True)
            command = [execution_python(), "-B", "-X", "utf8", str(ENGINE / "worker.py"), str(self.path)]
            if platform:
                command += ["--platform", platform]
            try:
                with (self.directory / "worker.log").open("ab") as log:
                    self.process = subprocess.Popen(command, cwd=ENGINE, stdout=log, stderr=log,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                state.update(phase="idle", notice="发布进程启动失败，请重试。")
                write_json(self.path, state)
                raise
            return state


publisher = PublishStore(DATA_DIR / "publisher")
router = APIRouter(prefix="/api/publish", tags=["publish"])
_PICKER_LOCK = threading.Lock()


def publishing_in_flight():
    """用于自动关机守卫：运行、暂停和正在安全停止均视为未收尾。"""
    try:
        with publisher.lock:
            return publisher.active()
    except (ValueError, OSError):
        return True


class AddRequest(BaseModel):
    folders: list[str] = Field(min_length=1, max_length=500)


class ScanRequest(BaseModel):
    directory: str = ""


class EditRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    date: str | None = None
    hour: str | None = None
    scheduled: bool | None = None
    platform: str | None = None
    state: str | None = None
    review: bool | None = None
    complete: bool = False


class StartRequest(BaseModel):
    platform: str | None = None


class ImportRequest(BaseModel):
    path: str


def checked_platform(platform):
    if platform is not None and platform not in PLATFORMS:
        raise ValueError("未知发布平台。")


def view(state):
    result = deepcopy(state)
    result["defaultDirectory"] = str(BATCH_OUTPUT_ROOT)
    result["dependenciesReady"] = dependencies_ready()
    for item in result["items"]:
        item["name"] = Path(item["folder"]).name
        try:
            found = assets(Path(item["folder"]))
            item["assets"] = {key: bool(value) for key, value in found.items()}
        except (ValueError, OSError):
            item["assets"] = {}
    return result


@router.get("")
def get_queue():
    try:
        return view(publisher.snapshot())
    except (ValueError, OSError) as error:
        raise HTTPException(409, str(error)) from error


@router.post("/scan")
def scan(request: ScanRequest):
    directory = Path(request.directory or BATCH_OUTPUT_ROOT).resolve()
    if not directory.is_dir():
        raise HTTPException(400, "目录不存在。")
    candidates = []
    # 发布根包含 dance/singing，或用户指定的作品父目录；不无限递归遍历磁盘。
    folders = [directory, *directory.iterdir()]
    for child in list(folders[1:]):
        if child.is_dir():
            folders.extend(p for p in child.iterdir() if p.is_dir())
    for folder in folders:
        if folder.is_dir() and (folder / "发布文案.txt").is_file():
            found = assets(folder)
            candidates.append({"folder": str(folder), "name": folder.name,
                               "assets": {key: bool(value) for key, value in found.items()}})
    return {"items": sorted(candidates, key=lambda i: i["folder"])}


@router.post("/pick")
def pick_folders(request: ScanRequest):
    if not _PICKER_LOCK.acquire(blocking=False):
        raise HTTPException(409, "作品选择窗口已经打开，请先完成选择或取消。")
    try:
        with publisher.lock:
            if publisher.active():
                raise HTTPException(409, "请先停止发布，再添加作品。")
        directory = Path(request.directory.strip().strip('"') or BATCH_OUTPUT_ROOT)
        if not directory.is_dir():
            directory = BATCH_OUTPUT_ROOT if BATCH_OUTPUT_ROOT.is_dir() else Path.home()
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(ENGINE / "folder_picker.py"), "--directory", str(directory)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise ValueError(result.stderr.strip() or "无法打开作品选择窗口。")
        folders = json.loads(result.stdout)["folders"]
        if not isinstance(folders, list) or len(folders) > 500 or any(not isinstance(p, str) for p in folders):
            raise ValueError("选择结果无效，单次最多添加500个作品文件夹。")
        return {"folders": folders}
    except (ValueError, OSError, KeyError) as error:
        raise HTTPException(400, f"文件夹选择失败：{error}") from error
    finally:
        _PICKER_LOCK.release()


@router.post("/add")
def add(request: AddRequest):
    try:
        def operation(state):
            known = {i["folder"].casefold() for i in state["items"]}
            for folder in request.folders:
                item = normalize_item({"folder": folder})
                found = assets(Path(item["folder"]))
                if not found["video"] or not found["copy"]:
                    raise ValueError(f"{Path(folder).name} 缺少明确的视频或发布文案。")
                if item["folder"].casefold() not in known:
                    if state["items"]:
                        previous = state["items"][-1]
                        next_time = datetime.strptime(f"{previous['date']} {previous['hour']}", "%Y-%m-%d %H") + timedelta(hours=1)
                        item.update(date=next_time.strftime("%Y-%m-%d"), hour=next_time.strftime("%H"))
                    state["items"].append(item)
                    known.add(item["folder"].casefold())
            state["notice"] = "作品已加入，默认从明天起逐条间隔一小时；可修改时间和发布平台。"
        return view(publisher.mutate(operation))
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/edit")
def edit(request: EditRequest):
    try:
        checked_platform(request.platform)
        def operation(state):
            matched = [i for i in state["items"] if i["id"] in request.ids]
            if len(matched) != len(set(request.ids)):
                raise ValueError("作品记录已变化，请刷新。")
            for item in matched:
                date, hour = request.date or item["date"], request.hour if request.hour is not None else item["hour"]
                datetime.strptime(f"{date} {hour}", "%Y-%m-%d %H")
                item.update(date=date, hour=str(int(hour)).zfill(2))
                if request.scheduled is not None:
                    item["scheduled"] = request.scheduled
                p = request.platform
                if not p:
                    if request.state is not None or request.review is not None or request.complete:
                        raise ValueError("请选择发布平台。")
                    continue
                current = item["states"][p]
                if request.review is not None:
                    if current != "待核对":
                        raise ValueError("只允许核对待核对记录。")
                    previous = item["details"].get(p, {})
                    item["states"][p] = "已完成" if request.review else "待发布"
                    item["details"][p] = {"manual_review": True, "submitted": request.review, "previous_attempt": previous}
                elif request.complete:
                    previous = item["details"].get(p, {})
                    item["states"][p] = "已完成"
                    item["details"][p] = {"manual": True, "submitted": True,
                        "completed_at": datetime.now(CHINA_TIME).isoformat(), "previous_attempt": previous}
                elif request.state is not None:
                    if current in ("已完成", "待核对", "发布中") or request.state not in ("待发布", "不发布"):
                        raise ValueError("该状态不能直接修改，请使用核对结果。")
                    item["states"][p] = request.state
        return view(publisher.mutate(operation))
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/remove")
def remove(request: EditRequest):
    try:
        def operation(state):
            state["items"] = [i for i in state["items"] if i["id"] not in request.ids]
            state["notice"] = "已删除选中记录，作品文件保持不变。"
        return view(publisher.mutate(operation))
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/import")
def import_queue(request: ImportRequest):
    try:
        source = Path(request.path).resolve()
        if source == publisher.path.resolve():
            raise ValueError("不能把当前队列作为旧队列导入。")
        saved = json.loads(source.read_text(encoding="utf-8"))
        incoming = [normalize_item(i) for i in saved["items"]]
        def operation(state):
            known = {i["folder"].casefold() for i in state["items"]}
            for item in incoming:
                if item["folder"].casefold() not in known:
                    state["items"].append(item)
                    known.add(item["folder"].casefold())
            state["notice"] = "旧队列已导入，保留各平台状态与历史；重复目录沿用当前记录。"
        return view(publisher.mutate(operation))
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(400, f"导入失败：{error}") from error


@router.post("/start")
def start(request: StartRequest):
    try:
        checked_platform(request.platform)
        return view(publisher.start(request.platform))
    except (ValueError, OSError) as error:
        raise HTTPException(409, str(error)) from error


@router.post("/control/{command}")
def control(command: str):
    if command not in ("pause", "resume", "stop"):
        raise HTTPException(400, "未知操作。")
    with publisher.lock:
        if not publisher.active():
            raise HTTPException(409, "当前没有运行中的发布流程。")
        if publisher.control_path.exists():
            current = json.loads(publisher.control_path.read_text(encoding="utf-8"))
            if current.get("command") == "stop" and command != "stop":
                raise HTTPException(409, "正在安全停止，请等待当前操作结束。")
        write_json(publisher.control_path, {"command": command, "id": uuid.uuid4().hex})
    return {"accepted": True}


@router.get("/items/{item_id}/asset/{key}")
async def get_asset(item_id: str, key: str):
    item = next((i for i in publisher.snapshot()["items"] if i["id"] == item_id), None)
    if not item or key not in ("video", "copy", "bilibili", "portrait"):
        raise HTTPException(404, "素材不存在。")
    folder = Path(item["folder"]).resolve()
    try:
        path = assets(folder).get(key)
        if not path or not path.resolve().is_relative_to(folder):
            raise ValueError("素材不存在。")
        if key == "video":
            from .douyin_preview import ensure_web_playable_at
            signature = f"{path.resolve()}:{path.stat().st_size}:{path.stat().st_mtime_ns}"
            target = DATA_DIR / "publisher" / "previews" / (hashlib.sha256(signature.encode()).hexdigest() + ".mp4")
            path = await ensure_web_playable_at(path, target)
        return FileResponse(path)
    except (ValueError, OSError) as error:
        raise HTTPException(404, str(error)) from error
