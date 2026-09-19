"""源视频完整性校验：把「半截下载」挡在 ComfyUI 之前。

2026-09-19 事故：条目 012 的驱动视频被下载器写成了半截文件 —— moov 里声明 343 帧，
mdat 只够 209 帧（ffprobe：`stream 1, offset 0x1c20ac: partial file`）。Mie 循环按 343 帧
排了 5 段，跑到第 3 段（`skip_first_frames=228`）时 VHS 解出 0 帧、抛
`RuntimeError("No frames generated")`：**15 分钟 GPU 只换来一句看不懂的 ComfyUI 报错**，
而且因为 `sourcePath` 已登记，之后每次「重新开始」都复用这个坏文件、原地复现。

判定口径只有一条：**容器声明的帧数 > 实际能解出来的帧数 = 下载被截断**。
（`-count_frames` 会整条解码，十几秒的短视频约 1 秒；拿不到这条正面证据就一律放行，
绝不误伤本来能跑的条目。）
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROBE_TIMEOUT_SECONDS = 300
# 末尾帧的 nb_frames/nb_read_frames 偶有 1 帧出入，0.5% 以内算完整。
FRAME_TOLERANCE = 0.005

# ffprobe 打不开或中途断流时的特征（半截文件的典型报错，出现在给用户看的原因里）。
UNREADABLE_MARKERS = (
    "moov atom not found",
    "Invalid data found when processing input",
    "partial file",
    "Error splitting the input",
    "Invalid NAL unit size",
    "could not find codec parameters",
)

# 同一份文件在「下载校验 → 提交前校验」里会被问两遍，整条解码不便宜（842 帧约 3 秒），
# 所以只缓存**通过**的结论，并用 (大小, mtime) 当指纹 —— 重新下载过的文件指纹变了，
# 会老老实实重验；没通过的一律不缓存（坏文件马上就被改名挪走了）。
_VALIDATED: dict[str, tuple[int, float]] = {}
_VALIDATED_LIMIT = 64


def _remember_valid(source: Path, size: int, mtime: float) -> None:
    if len(_VALIDATED) >= _VALIDATED_LIMIT:
        _VALIDATED.clear()
    _VALIDATED[str(source)] = (size, mtime)


def _as_int(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def probe_frame_counts(path: Path) -> tuple[int | None, int | None, str]:
    """返回 (容器声明帧数, 实际可解码帧数, ffprobe stderr)；读不出给 None。"""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-count_frames",
            "-show_entries", "stream=nb_read_frames,nb_frames",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PROBE_TIMEOUT_SECONDS,
        creationflags=CREATE_NO_WINDOW,
    )
    claimed = decoded = None
    try:
        streams = (json.loads(result.stdout or "{}") or {}).get("streams") or []
        if streams:
            claimed = _as_int(streams[0].get("nb_frames"))
            decoded = _as_int(streams[0].get("nb_read_frames"))
    except (ValueError, TypeError):
        pass
    return claimed, decoded, (result.stderr or "").strip()


def _last_stderr_line(stderr: str) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    return lines[-1][:160] if lines else ""


def probe_duration_seconds(path: str | Path) -> float | None:
    """读容器声明的时长（秒）；读不出给 None。"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=PROBE_TIMEOUT_SECONDS, creationflags=CREATE_NO_WINDOW,
    )
    try:
        value = float((result.stdout or "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


def duration_mismatch_reason(
    path: str | Path, expected_seconds: float | None, *, tolerance_seconds: float = 1.0
) -> str | None:
    """对照**平台声明的时长**：文件短太多就判为不完整（返回原因），否则 None。

    为什么还要这一条（帧数校验不是已经够了吗）：帧数校验比的是「容器声明 vs 实际解码」，
    如果拿到的是一份**自洽的短文件**（容器和内容都短），它查不出来 —— 只有跟官方时长对照
    才发现少了一截。探测不出结果时一律放行。
    """
    if not expected_seconds or expected_seconds <= 0:
        return None
    source = Path(path)
    if not source.is_file():
        return None
    try:
        actual = probe_duration_seconds(source)
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("时长探测失败，跳过对照：%s（%s）", source, error)
        return None
    if actual is None:
        return None
    if actual < expected_seconds - tolerance_seconds:
        return (
            f"源视频比平台声明的短：平台说 {expected_seconds:.2f} 秒，"
            f"下载到的只有 {actual:.2f} 秒（少了 {expected_seconds - actual:.2f} 秒）"
            f" —— {source.name}"
        )
    return None


def truncation_reason(path: str | Path) -> str | None:
    """完整返回 None；确认被截断时返回一句能直接给用户看的中文原因。

    **只在拿到正面证据时才判失败**：容器声明了 N 帧、而且实际只解出 M<N 帧。这条判据
    正好命中 012 那种「moov 完整、mdat 少了半截」的下载文件；至于 ffprobe 根本打不开的
    文件（错误页、空壳、损坏容器）一律放行 —— 那是另一类故障，ComfyUI 自己会报，
    这里不越权，也绝不误伤本来能跑的条目。
    """
    source = Path(path)
    if not source.is_file():
        return None
    try:
        stat = source.stat()
    except OSError as error:  # 权限/占用等
        logger.warning("源视频大小读取失败，跳过完整性校验：%s（%s）", source, error)
        return None
    if stat.st_size == 0:
        return None
    if _VALIDATED.get(str(source)) == (stat.st_size, stat.st_mtime):
        return None

    try:
        claimed, decoded, stderr = probe_frame_counts(source)
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("源视频完整性探测失败，跳过校验：%s（%s）", source, error)
        return None

    if claimed is None or decoded is None:
        # 判不出来（容器没写 nb_frames / 整条读不出）→ 放行；只把可疑特征记进日志。
        if any(marker in stderr for marker in UNREADABLE_MARKERS):
            logger.warning("源视频读不出完整帧信息，跳过完整性校验：%s（%s）", source, _last_stderr_line(stderr))
        return None

    if decoded < claimed * (1 - FRAME_TOLERANCE):
        hint = _last_stderr_line(stderr)
        return (
            f"源视频文件不完整：容器声明 {claimed} 帧，实际只能解出 {decoded} 帧"
            f"（只有 {decoded / claimed:.0%}），下载被截断了 —— {source.name}"
            + (f"（{hint}）" if hint else "")
        )
    _remember_valid(source, stat.st_size, stat.st_mtime)
    return None
