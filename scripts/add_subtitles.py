"""scripts/add_subtitles.py — 给任意成片生成「剪映手书」字幕版（正式入口，2026-09-16 固化）。

这条链路已经由用户逐步验证通过（2026-09-16），实现全部在 `backend/`，本脚本只是稳定的
命令行入口 + 产物交付，**不改动任何行为**。

流程（与用户手工操作一一对应）：
    ① 剪映「开始创作」新建工程（**每次都是新工程**，不复用/不点旧草稿）
    ② 素材面板「导入」→ 文件框里用**完整路径**选中该成片（避免点到别的目录里的同名文件）
    ③ 素材库「导入」按钮把素材放上时间轴
    ④ 「文本」→「识别歌词」→「开始识别」（剪映云端识别，时间轴最准）
    ⑤ 网易云官方歌词逐句核对**文字**（时间轴一字不动）
    ⑥ ffmpeg + libass 按剪映手书观感烧录
    ⑦ 交付到该作品自己的发布目录：`最终成片_字幕版.mp4`（与无字幕版并存）

关键约定（都是实测踩出来的）：
    · 每次新建工程；导入后用草稿 JSON **校验素材完整路径**；
    · 真实鼠标点击（分步移动 + 停顿 + 按下抬起），点击前强切前台并校验；
    · 跑完**不关剪映**（`H3_KEEP_JIANYING=1`；要关就设 `H3_CLOSE_JIANYING_AFTER=1`）；
    · 不等 `pipeline_lock`（这条链路不用 ComfyUI）。

用法：
    python scripts/add_subtitles.py "E:\\...\\发布成品\\001_xxx\\最终成片.mp4" --song 梁山伯与茱丽叶
    python scripts/add_subtitles.py <成片.mp4> --song 歌名 --out <发布目录>
    python scripts/add_subtitles.py <成片.mp4>            # 歌名留空 → 用识别出的歌词反查官方歌词
不给 `--out` 时产物只落在任务目录（`E:\\AI_Exports\\ComfyUI\\Main\\video\\H3_MotionStudio\\`）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import jianying_desktop as jy  # noqa: E402
from backend import jianying_recognize as jr  # noqa: E402
from backend import subtitle_step  # noqa: E402
from backend.pipeline import media_metadata  # noqa: E402
from backend.store import now_iso, store, subtitle_milestones  # noqa: E402

OUT_DIR = Path(r"E:\AI_Exports\ComfyUI\Main\video\H3_MotionStudio")
JY_EXE = r"D:\soft\剪映以激活版本免安装\剪映以激活版本免安装\剪映解锁版\JianyingPro\5.9.0.11632\JianyingPro.exe"


def ensure_jianying(*, timeout: float = 120.0) -> int:
    """剪映没在跑就带 `QT_ACCESSIBILITY=1` 拉起（控件树要靠它），返回主窗口句柄。"""
    hwnd = jy.find_window()
    if hwnd:
        print(f"剪映已在运行：{hwnd}")
        return hwnd
    import os

    env = dict(os.environ, QT_ACCESSIBILITY="1")
    print("启动剪映……")
    subprocess.Popen([JY_EXE], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        hwnd = jy.find_window()
        if hwnd:
            time.sleep(4)
            print(f"剪映就绪：{hwnd} {jy.window_rect(hwnd)}")
            return hwnd
    raise RuntimeError(f"剪映在 {timeout:.0f} 秒内没能启动")


def click_window_rel(x: int, y: int, *, label: str, rounds: int = 8) -> bool:
    """真实鼠标点窗口内坐标；每轮先强切前台（抢不到就重试）。"""
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    for _ in range(rounds):
        hwnd = jy.find_window()
        if not hwnd or not jy.force_foreground_hard(hwnd):
            time.sleep(0.6)
            continue
        wx, wy, _, _ = jy.window_rect(hwnd)
        sx, sy = wx + x, wy + y
        cur = wt.POINT()
        user32.GetCursorPos(ctypes.byref(cur))
        for i in range(1, 13):
            user32.SetCursorPos(int(cur.x + (sx - cur.x) * i / 12),
                                int(cur.y + (sy - cur.y) * i / 12))
            time.sleep(0.012)
        user32.SetCursorPos(sx, sy)
        time.sleep(0.5)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.06)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(1.4)
        print(f"   点「{label}」窗口内 ({x},{y})")
        return True
    print(f"   「{label}」抢不到前台，放弃")
    return False


def _texts(draft: Path) -> int:
    try:
        content = json.loads((draft / "draft_content.json").read_text(encoding="utf-8"))
    except Exception:
        return -1
    return len((content.get("materials") or {}).get("texts") or [])


async def run(source: Path, song: str, out_dir: Path | None, job: str) -> int:
    out = OUT_DIR / f"{job}_字幕版.mp4"
    print(f"源片 : {source}\n歌名 : {song or '（用识别歌词反查）'}\n产物 : {out}\n")

    ensure_jianying()

    print("① 新建工程（开始创作）+ 导入 + 上轨")
    draft = jr.start_new_project_and_import(source)
    print(f"   草稿 {draft.name}（已校验导入路径）")

    print("② 文本 → 识别歌词 → 开始识别")
    for label, (bx, by) in (("文本", jr.TEXT_TAB), ("识别歌词", jr.RECOGNIZE_ITEM),
                            ("开始识别", jr.START_BUTTON)):
        if not click_window_rel(bx, by, label=label):
            return 1
        time.sleep(1.2)

    print("③ 等剪映识别")
    rows: list = []
    for _ in range(60):
        time.sleep(3)
        if _texts(draft) > 0:
            rows = jr._draft_texts(draft)
            print(f"   识别出 {len(rows)} 条")
            break
    if not rows:
        print("❌ 超时未出字幕")
        return 1

    print("④ 官方歌词核对文字 + 剪映手书烧录")
    stats: dict = {}
    path, notes = await subtitle_step.burn_for_video(
        source, song, out, on_stage=subtitle_step._stage_hook(job), stats=stats,
        on_done=subtitle_step._done_hook(job))
    print(f"   产物 {path.name}（{path.stat().st_size / 1024 / 1024:.2f} MB）")
    print(f"   句数 {stats.get('lines')} · 来源 {stats.get('lyricSource')} · 修正 {stats.get('corrected')}")
    for note in notes:
        print("   核对:", note)

    if out_dir is not None:
        target = Path(out_dir) / "最终成片_字幕版.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, path, target)
        print(f"⑤ 已放进发布目录：{target}")

    if store.get(job):
        store.update(job, status="completed", stage="completed", progress=100,
                     finalOutput=str(path), finalReady=True,
                     subtitleLineCount=stats.get("lines"),
                     subtitleLyricSource=stats.get("lyricSource"),
                     subtitleCorrected=stats.get("corrected"),
                     finishedAt=now_iso(), output=await media_metadata(path))
    print("✅ 完成（剪映保持打开；需要关闭请设 H3_CLOSE_JIANYING_AFTER=1 或用 jianying_recognize.close_app）")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="给成片生成剪映手书字幕版（固化流程入口）")
    ap.add_argument("source", help="无字幕成片 mp4")
    ap.add_argument("--song", default="", help="歌名；留空则用识别出的歌词反查官方歌词")
    ap.add_argument("--out", default="", help="发布目录（通常是该作品自己的发布成品目录）")
    ap.add_argument("--job", default="", help="任务 id；留空自动生成")
    a = ap.parse_args(argv)

    source = Path(a.source)
    if not source.is_file():
        print("成片不存在：", source)
        return 1
    job = a.job or ("local_" + time.strftime("%m%d_%H%M%S"))
    if not store.get(job):
        created = now_iso()
        store.create({
            "id": job, "kind": "subtitle", "status": "queued", "stage": "upload",
            "createdAt": created, "updatedAt": created,
            "sourceName": source.parent.name, "songName": a.song,
            "sourcePath": str(source), "subtitleSource": str(source),
            "subtitleOutPath": str(OUT_DIR / f"{job}_字幕版.mp4"), "sourceJobId": "",
            "milestones": subtitle_milestones(), "logs": [], "progress": None,
            "finalReady": False, "finalOutput": None,
        })
    return asyncio.run(run(source.resolve(), a.song, Path(a.out) if a.out else None, job))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
