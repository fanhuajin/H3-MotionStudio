"""backend/subtitle_detect.py — 自动定位视频里持续显示的字幕条（源像素坐标）。

供去字幕流程在提交工作流前调用。算法（v2）：
1. 只用 30%~97% 时间段的帧（跳过片头标题/特效），等宽抽帧（≤1280 宽，保比例）；
2. 对每一帧逐行计算横向边缘能量与「相对 ±110px 邻域中位数的尖峰比」，得到该帧的
   候选文字带 —— 字幕是叠加层，笔画密度远高于同帧背景；逐帧判定避免把不同时段、
   不同位置的字幕平均摊薄或与静止场景纹理混淆；
3. 跨帧投票：只在 ≥40% 采样帧里都出现的行带才是「持续字幕」；
4. 在胜出带内取列覆盖 p2~p98 得到横向范围并外扩边距，输出源分辨率像素坐标。

运行环境需要 numpy + Pillow（用 ComfyUI 自带的 python_embeded 执行即可）。
用法：
    python backend/subtitle_detect.py <video.mp4> [--debug]
输出（stdout JSON）：
    {"ok": true, "x0":.., "y0":.., "x1":.., "y1":.., "srcW":.., "srcH":.., "frames":12}
    {"ok": false, "reason": "..."}
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

START_FRACTION = 0.30
END_FRACTION = 0.97
N_FRAMES = 12
RING_RADIUS = 110
RING_RATIO = 1.45          # 行能量 / 邻域中位数的尖峰阈值
RUN_MIN_HEIGHT = 10
RUN_MAX_HEIGHT = 260
MIN_FRAME_SHARE = 0.40     # 带至少出现在多少比例的采样帧里
MERGE_GAP = 60             # 相邻带间隔 ≤ 该值合并（双行字幕）
X_MIN_SPAN_RATIO = 0.15    # 横向跨度至少占画面宽的比例
MAX_DECODE_WIDTH = 1280    # 分析解码宽度上限（保比例）
# 横向外扩要明显大于文字两端的淡笔画/阴影：单侧 = 带宽的 12%，保底 20 源像素
PAD_FRAC_X = 0.12
PAD_MIN_X = 20
PAD_FRAC_Y = 0.16          # 纵向外扩：带高的 16%（保底若干像素）


def _probe_stream(video: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-show_entries", "format=duration", "-of", "json", str(video),
        ],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    stream = (payload.get("streams") or [{}])[0]
    info = {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
    }
    try:
        info["duration"] = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        info["duration"] = 0.0
    return info


def _sample_frames(video: Path, times: list[float]) -> list[np.ndarray]:
    frames: list[np.ndarray] = []
    with tempfile.TemporaryDirectory(prefix="subtitle_detect_") as tmp:
        tmp_dir = Path(tmp)
        for index, t in enumerate(times):
            out = tmp_dir / f"fr_{index:02d}.png"
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
                    "-frames:v", "1", "-vf", f"scale=w={MAX_DECODE_WIDTH}:h=-2", str(out),
                ],
                capture_output=True, text=True, errors="replace", timeout=120,
            )
            if result.returncode != 0 or not out.is_file():
                continue
            try:
                gray = np.asarray(Image.open(out).convert("L")).astype(np.float32)
            except Exception:
                continue
            frames.append(gray)
    return frames


def _row_ratio(gray: np.ndarray, height: int) -> np.ndarray:
    gx = np.abs(np.diff(gray, axis=1))
    row_e = gx.mean(axis=1)

    def ring_median(y: int) -> float:
        lo = max(0, y - RING_RADIUS)
        hi = min(height, y + RING_RADIUS + 1)
        if hi - lo - 1 <= 4:
            return float(row_e[y])
        return float(np.median(np.concatenate([row_e[lo:y], row_e[y + 1:hi]])))

    ring = np.array([ring_median(y) for y in range(height)])
    return row_e / np.maximum(0.15, ring)


def _frame_bands(ratio: np.ndarray) -> list[tuple[int, int]]:
    hot = ratio >= RING_RATIO
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for y, is_hot in enumerate(hot):
        if is_hot and start is None:
            start = y
        elif not is_hot and start is not None:
            if y - start >= RUN_MIN_HEIGHT:
                bands.append((start, y - 1))
            start = None
    if start is not None and len(hot) - start >= RUN_MIN_HEIGHT:
        bands.append((start, len(hot) - 1))
    return [(y0, y1) for (y0, y1) in bands if RUN_MIN_HEIGHT <= (y1 - y0 + 1) <= RUN_MAX_HEIGHT]


def _locate(frames: list[np.ndarray]) -> dict:
    if len(frames) < 4:
        return {"ok": False, "reason": f"有效采样帧不足（{len(frames)} 帧）"}
    scaled_h, scaled_w = frames[0].shape

    vote = np.zeros(scaled_h, dtype=np.int32)
    per_frame_bands: list[list[tuple[int, int]]] = []
    for gray in frames:
        bands = _frame_bands(_row_ratio(gray, scaled_h))
        per_frame_bands.append(bands)
        for y0, y1 in bands:
            vote[y0:y1 + 1] += 1

    need = max(3, int(len(frames) * MIN_FRAME_SHARE))
    hot_rows = vote >= need
    runs: list[tuple[int, int, int]] = []  # (y0, y1, votes)
    start: int | None = None
    best_vote = 0
    for y, is_hot in enumerate(hot_rows):
        if is_hot and start is None:
            start = y
        elif not is_hot and start is not None:
            runs.append((start, y - 1, int(vote[start:y].max())))
            best_vote = max(best_vote, int(vote[start:y].max()))
            start = None
    if start is not None:
        runs.append((start, scaled_h - 1, int(vote[start:].max())))
        best_vote = max(best_vote, int(vote[start:].max()))
    runs = [(y0, y1, v) for (y0, y1, v) in runs
            if RUN_MIN_HEIGHT <= (y1 - y0 + 1) <= RUN_MAX_HEIGHT]

    # 合并相邻（双行/换行），然后优先保留出现帧数最多的带
    merged: list[list[int]] = []
    for y0, y1, v in sorted(runs, key=lambda r: r[0]):
        if merged and y0 - merged[-1][1] <= MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], y1)
        else:
            merged.append([y0, y1])
    candidates: list[tuple[int, int]] = []
    for y0, y1 in merged:
        span_vote = int(vote[y0:y1 + 1].max())
        if span_vote >= need and RUN_MIN_HEIGHT <= (y1 - y0 + 1) <= RUN_MAX_HEIGHT:
            candidates.append((y0, y1, span_vote))
    if not candidates:
        return {"ok": False, "reason": "中后段未检测到跨帧稳定的字幕条带"}
    candidates.sort(key=lambda c: (-c[2], c[1] - c[0]))
    # 纵向出现率接近时取更靠中下的带（歌词多在画面下部）；一般只应有一条
    y0, y1, _ = candidates[0]
    for cy0, cy1, cv in candidates[1:]:
        if cv >= need and abs(cy0 - y0) > MERGE_GAP * 2 and (cy1 - y0) <= scaled_h * 0.45:
            y0, y1 = min(y0, cy0), max(y1, cy1)  # 多条持续字幕（如歌名+歌词）时取并集
            break

    # 横向范围：跨帧在带内取最大列梯度，再取覆盖 p1~p99（放宽阈值与分位数，
    # 把两侧低对比的淡笔画/阴影也算进范围，避免字幕两端残留）
    acc = np.zeros(scaled_w - 1, dtype=np.float32)
    for gray in frames:
        gx = np.abs(np.diff(gray, axis=1))[y0:y1 + 1, :]
        acc = np.maximum(acc, gx.mean(axis=0))
    thr = float(acc.mean() + 0.6 * acc.std())
    hot_cols = np.where(acc > thr)[0]
    if len(hot_cols) < max(12, int((scaled_w - 1) * X_MIN_SPAN_RATIO)):
        return {"ok": False, "reason": "字幕带横向跨度过窄或缺失"}
    x0, x1 = np.percentile(hot_cols, [1, 99]).astype(int)
    x0, x1 = int(x0), int(x1)

    pad_x = max(PAD_MIN_X, int((x1 - x0) * PAD_FRAC_X))
    pad_y = max(6, int((y1 - y0 + 1) * PAD_FRAC_Y))
    # 横向大幅外扩后仍保留少量画布安全边（≤3% 每侧），避免整帧拉满误伤边缘 UI
    margin = max(8, int((scaled_w - 1) * 0.03))
    return {
        "ok": True,
        "x0": max(margin, x0 - pad_x),
        "y0": max(0, y0 - pad_y),
        "x1": min(scaled_w - 1 - margin, x1 + pad_x),
        "y1": min(scaled_h - 1, y1 + pad_y),
        "scaledW": scaled_w,
        "scaledH": scaled_h,
        "frames": len(frames),
        "bandY0": y0,
        "bandY1": y1,
    }


def main() -> int:
    debug = "--debug" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    video = Path(args[0]) if args else None
    if not video or not video.is_file():
        print(json.dumps({"ok": False, "reason": f"视频不存在：{video}"}))
        return 2
    info = _probe_stream(video)
    duration = info.get("duration") or 0
    src_w = info.get("width") or 0
    src_h = info.get("height") or 0
    if duration <= 0 or src_w <= 0:
        print(json.dumps({"ok": False, "reason": "无法探测视频时长/分辨率"}))
        return 2
    if duration <= 2.5:
        times = list(np.linspace(0.1, max(0.2, duration - 0.1), N_FRAMES))
    else:
        t_start = duration * START_FRACTION
        t_end = min(duration - 0.1, duration * END_FRACTION)
        if t_end <= t_start:
            t_start, t_end = 0.1, duration - 0.1
        times = list(np.linspace(t_start, t_end, N_FRAMES))
    frames = _sample_frames(video, times)
    result = _locate(frames)
    if result.get("ok"):
        # 把分析图（可能已缩宽）坐标还原回源分辨率
        scale_x = src_w / result["scaledW"]
        scale_y = src_h / result["scaledH"]
        for key in ("x0", "x1"):
            result[key] = int(result[key] * scale_x)
        for key in ("y0", "y1"):
            result[key] = int(result[key] * scale_y)
        result["srcW"], result["srcH"] = src_w, src_h
        result.pop("scaledW", None)
        result.pop("scaledH", None)
        result.pop("bandY0", None)
        result.pop("bandY1", None)
        result["duration"] = round(duration, 3)
    if debug:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
