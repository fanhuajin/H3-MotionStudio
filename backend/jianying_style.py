"""backend.jianying_style — 复刻剪映字幕观感的 ASS 生成与烧录（ffmpeg + libass）。

## 参数怎么来的

拿**剪映实际导出的成片**当基准自动标定出来的（1440×1080 画布）：

    Fontname  JYgangbi（剪映手书）    ← 字体族名取自 TTF 的 name 表
    Fontsize  94                     ← 使字幕宽度与剪映版一致（实测两边都是 482px）
    MarginV   90                     ← 字幕底边距画面底 ≈113px
    Outline   0                      ← 描边档位扫描里与剪映版差异最小

想重新标定：`scripts/burn_draft_subtitles.py --calibrate --reference <剪映导出的成片>`。

## 两个必须注意的坑（都踩过）

  · **ffmpeg 的 filter 参数里不能出现 Windows 盘符的冒号/空格** —— 所以字幕与字体都放到
    无空格临时目录，并用 `cwd` + 相对路径传参。
  · **`-ss` 必须放在 `-i` 之后**（输出侧 seek）—— 否则时间戳被重置到 0，ASS 找不到当前
    时刻的字幕，渲染出来一片空白。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

FONT_NAME = "JYgangbi"
STYLE = {"size": 94, "margin_v": 90, "outline": 0.0}

FONT_CANDIDATES = [
    Path(os.environ["H3_JIANYING_FONT"]) if os.getenv("H3_JIANYING_FONT") else None,
    Path(os.environ["LOCALAPPDATA"]) / "JianyingPro" / "User Data" / "Cache" / "effect"
    / "110450154" / "2e85dc679048a1da139661e050438ec2" / "JYgangbi.add_full_width_chars.ttf",
]


def find_font() -> Path:
    """剪映手书字体（在剪映里用过一次即会缓存到本机）。"""
    for c in FONT_CANDIDATES:
        if c and c.is_file():
            return c
    raise FileNotFoundError(
        "找不到剪映手书字体（JYgangbi）。在剪映里用一次「剪映手书」即会缓存，"
        "或用 H3_JIANYING_FONT 指定 ttf 路径。")


def ass_time(sec: float) -> str:
    """ASS 时间必须是 `h:mm:ss.cc`（**分钟两位**、两位百分秒）。

    踩过：只写 `{m}` 会输出 `0:0:00.97`，libass 虽然容忍，但不符合规范，
    而且历史上项目已经明确要求这个格式。
    """
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(lines, *, size: int = STYLE["size"], margin_v: int = STYLE["margin_v"],
              outline: float = STYLE["outline"], play_res: tuple[int, int] = (1440, 1080)) -> str:
    """lines: [(start_s, end_s, text)] → ASS 文本。"""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_res[0]}
PlayResY: {play_res[1]}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: JY,{FONT_NAME},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{outline},0,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    body = "".join(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},JY,,0,0,0,,{text}\n"
                   for a, b, text in lines)
    return head + body


def burn(video: Path, out: Path, lines, *, size: int = STYLE["size"],
         margin_v: int = STYLE["margin_v"], outline: float = STYLE["outline"],
         fps: int = 30, crf: int = 18) -> Path:
    """把 lines 烧到 video 上（视频重编码 CRF18，音频直通）。"""
    video = Path(video).resolve()
    out = Path(out).resolve()
    if not video.is_file():
        raise FileNotFoundError(f"输入成片不存在：{video}")
    if not lines:
        raise ValueError("没有字幕行，拒绝烧录空字幕")
    play_res = _video_size(video)
    tmp = Path(tempfile.mkdtemp(prefix="h3_subs_"))
    try:
        (tmp / "fonts").mkdir(exist_ok=True)
        shutil.copy2(find_font(), tmp / "fonts" / f"{FONT_NAME}.ttf")
        (tmp / "sub.ass").write_text(
            build_ass(lines, size=size, margin_v=margin_v, outline=outline, play_res=play_res),
            encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-i", str(video),
            "-vf", "ass=sub.ass:fontsdir=fonts", "-r", str(fps),
            "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", str(out),
        ], check=True, cwd=str(tmp))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _video_size(video: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height",
                          "-of", "csv=p=0:s=x", str(video)],
                         capture_output=True, text=True).stdout.strip()
    try:
        w, h = (int(x) for x in out.split("x")[:2])
        return (w, h)
    except Exception:
        return (1440, 1080)
