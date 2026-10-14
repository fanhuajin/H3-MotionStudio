from __future__ import annotations

import math

MAX_DANCE_DURATION_SECONDS = 40.0


def validate_dance_duration(duration: float | None) -> None:
    """Reject unknown or over-limit dance sources before paid preparation/rendering."""
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ValueError("无法确认跳舞视频时长，已停止处理；请使用不超过 40 秒的视频")
    if duration > MAX_DANCE_DURATION_SECONDS:
        raise ValueError(f"跳舞视频时长为 {duration:.2f} 秒，不能超过 40 秒；已停止处理")
