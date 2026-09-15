"""automation.frame_picker — 抽帧 + 挑「人物最完整」的帧。

策略：不抽第一帧，而是每隔一段时间抽多帧，再用 OpenCV 打分挑一帧。
打分规则（渐进）：
  V0 人脸框最大 / 居中（Haar Cascade）
  V1 清晰度（Laplacian 方差）过滤运动模糊
  V2（可选）眼开度 / 遮挡检测 —— 需额外模型，暂留接口

若 OpenCV 不可用，自动降级为「取中间帧」，绝不因依赖缺失而卡死。

ffmpeg 由项目自带的 ffmpeg 二进制提供（见 backend/settings.py H3_FFMPEG 相关），
这里允许用环境变量 H3AUT_FFMPEG 指定；缺省尝试 PATH 里的 ffmpeg/ffprobe。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import settings as _s


def _find_ffmpeg() -> str:
    exe = __import__("os").getenv("H3AUT_FFMPEG")
    if exe and Path(exe).is_file():
        return exe
    return shutil.which("ffmpeg") or "ffmpeg"


def _find_ffprobe() -> str:
    exe = __import__("os").getenv("H3AUT_FFPROBE")
    if exe and Path(exe).is_file():
        return exe
    return shutil.which("ffprobe") or "ffprobe"


def probe_duration(video: str) -> float:
    """用 ffprobe 取视频时长（秒）。失败抛 RuntimeError。"""
    ffprobe = _find_ffprobe()
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", video]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{out.stderr[-300:]}")
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise RuntimeError(f"无法解析时长：{out.stdout!r}")


def extract_frames(
    video: str,
    out_dir: str | Path,
    *,
    count: int = 10,
) -> list[Path]:
    """每隔时长/count 抽一帧（全时段覆盖）。返回落盘帧路径列表。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video)
    ffmpeg = _find_ffmpeg()
    frames: list[Path] = []
    for i in range(count):
        ts = duration * i / count
        out = out_dir / f"frame_{i:03d}.jpg"
        cmd = [ffmpeg, "-y", "-ss", f"{ts:.3f}", "-i", video,
               "-frames:v", "1", "-q:v", "2", str(out)]
        subprocess.run(cmd, capture_output=True, text=True)
        if out.is_file() and out.stat().st_size > 0:
            frames.append(out)
    return frames


def _pick_by_cv(frames: list[Path]) -> Path | None:
    """用 OpenCV 挑一帧；不可用返回 None（调用方降级）。"""
    try:
        import cv2
    except ImportError:
        return None

    cascade_path = str(Path(__file__).parent / "haarcascade_frontalface_default.xml")
    if not Path(cascade_path).is_file():
        # 尝试用 opencv 自带的数据
        try:
            cv2.data  # noqa: B018
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        except AttributeError:
            return None
    if not Path(cascade_path).is_file():
        return None
    cascade = cv2.CascadeClassifier(cascade_path)

    best: tuple[float, Path] | None = None
    for fp in frames:
        img = cv2.imread(str(fp))
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(80, 80))
        if not len(faces):
            continue
        # 取最大人脸框
        w_max, h_max, cx_off, cy_off = max(faces, key=lambda b: b[2] * b[3])
        h, w = gray.shape
        # 得分 = 脸面积占比 * 居中权重
        area_ratio = (w_max * h_max) / (w * h)
        face_cx = cx_off + w_max / 2
        face_cy = cy_off + h_max / 2
        centerness = 1.0 - min(abs(face_cx / w - 0.5) * 2, 1.0) * 0.5 \
                         - min(abs(face_cy / h - 0.5) * 2, 1.0) * 0.5
        clarity = cv2.Laplacian(gray, cv2.CV_64F).var()
        score = area_ratio * centerness * (1.0 if clarity > 50 else 0.3)
        if best is None or score > best[0]:
            best = (score, fp)
    return best[1] if best else None


def pick_best_frame(
    video: str,
    out_dir: str | Path,
    *,
    count: int = 10,
) -> Path:
    """抽多帧并挑「人物最完整」的一帧；无 OpenCV 时返回中间帧。"""
    out_dir = Path(out_dir)
    frames = extract_frames(video, out_dir, count=count)
    if not frames:
        raise RuntimeError(f"未能从视频抽到任何帧：{video}")
    chosen = _pick_by_cv(frames)
    if chosen is None:
        # 降级：取中间帧
        chosen = frames[len(frames) // 2]
        print(f"[frame_picker] OpenCV 不可用或无检测到人脸，取中间帧：{chosen}")
    return chosen


if __name__ == "__main__":
    # 用法：python -m automation.frame_picker <video> <out_dir>
    import sys
    video = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else str(_s.work_dir(_s.load()) / "frames")
    best = pick_best_frame(video, out)
    print(best)
