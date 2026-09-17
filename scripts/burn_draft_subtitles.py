"""scripts/burn_draft_subtitles.py — 按剪映观感把草稿里的歌词字幕烧到成片上。

## 为什么需要它

剪映专业版**没有命令行导出**，而本机要求自动化、且**不允许操作鼠标**。
所以这一环改为：从剪映草稿工程（`draft_content.json`）读出识别好的字幕文本与时间轴，
再用 `ffmpeg + libass + 同一个「剪映手书」字体`复刻剪映的观感。

好处：完全后台、可批量、可复现；字幕文本/时间轴仍然来自剪映自己的「识别歌词」。

## 参数怎么来的

按剪映 5.9.0 实际导出的成片**标定**得到（1440×1080 画布）：

    Fontname  JYgangbi（剪映手书）       ← 字体族名取自 TTF name 表
    Fontsize  94                        ← 使字幕宽度与剪映版一致（实测同为 482px）
    MarginV   90                        ← 字幕底边距画面底 ≈113px
    Outline   0                         ← 描边扫描中与剪映版差异最小

想重新标定：`--calibrate --reference <剪映导出的成片>`，会自动搜索 size/margin/outline。

## 用法

    python scripts/burn_draft_subtitles.py \
        --draft "9月16日 (4)" \
        --video "E:\\...\\001_有何不可_...\\最终成片.mp4" \
        --out   "E:\\...\\001_有何不可_...\\最终成片_字幕版.mp4"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops  # noqa: F401  (标定用)

DRAFT_ROOT = Path(os.getenv("H3_JIANYING_DRAFT_ROOT")
                  or (Path(os.environ["LOCALAPPDATA"]) / "JianyingPro" / "User Data"
                      / "Projects" / "com.lveditor.draft"))

# 剪映手书字体（剪映把用过的字体缓存到这里）
FONT_CANDIDATES = [
    Path(os.getenv("H3_JIANYING_FONT", "")) if os.getenv("H3_JIANYING_FONT") else None,
    Path(os.environ["LOCALAPPDATA"]) / "JianyingPro" / "User Data" / "Cache" / "effect"
    / "110450154" / "2e85dc679048a1da139661e050438ec2" / "JYgangbi.add_full_width_chars.ttf",
]
FONT_NAME = "JYgangbi"

# 标定结果（1440x1080）
STYLE = {"size": 94, "margin_v": 90, "outline": 0}


# ---------------------------------------------------------------------------
def find_font() -> Path:
    for c in FONT_CANDIDATES:
        if c and c.is_file():
            return c
    raise FileNotFoundError("找不到剪映手书字体（在剪映里用过一次「剪映手书」即会缓存到本机）")


def read_draft_lines(draft: str) -> list[tuple[float, float, str]]:
    """读草稿里的字幕：[(start_s, end_s, text)]，按时间排序。"""
    f = DRAFT_ROOT / draft / "draft_content.json"
    if not f.is_file():
        raise FileNotFoundError(f"找不到草稿：{f}")
    j = json.loads(f.read_text(encoding="utf-8"))
    timerange: dict[str, tuple[float, float]] = {}
    for tr in j.get("tracks") or []:
        for s in tr.get("segments") or []:
            t = s.get("target_timerange") or {}
            timerange[s.get("material_id")] = (t.get("start", 0) / 1e6, t.get("duration", 0) / 1e6)
    rows = []
    for t in (j.get("materials") or {}).get("texts") or []:
        try:
            text = (json.loads(t.get("content") or "{}").get("text") or "").strip()
        except Exception:
            continue
        if not text:
            continue
        st, du = timerange.get(t.get("id"), (0.0, 0.0))
        if du <= 0:
            continue
        rows.append((round(st, 3), round(st + du, 3), text))
    rows.sort()
    return rows


def ass_time(sec: float) -> str:
    """ASS 时间必须是 h:mm:ss.cc（两位百分秒）。"""
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m}:{s:05.2f}"


def write_ass(path: Path, lines, *, size: int, margin_v: int, outline: float,
              play_res: tuple[int, int] = (1440, 1080)) -> None:
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
    body = "".join(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},JY,,0,0,0,,{txt}\n"
                   for a, b, txt in lines)
    path.write_text(head + body, encoding="utf-8")


def burn(video: Path, out: Path, lines, *, size=STYLE["size"], margin_v=STYLE["margin_v"],
         outline=STYLE["outline"], fps: int = 30, crf: int = 18) -> Path:
    """把字幕烧到成片上。字幕文件放 %TEMP%，因为 ffmpeg 的 filter 参数里不能有空格/冒号。"""
    tmp = Path(tempfile.mkdtemp(prefix="h3_subs_"))
    # 必须转绝对路径：ffmpeg 的 cwd 是临时目录，相对路径会被解析到那里（踩过）
    video = Path(video).resolve()
    out = Path(out).resolve()
    try:
        font = find_font()
        (tmp / "fonts").mkdir(exist_ok=True)
        shutil.copy2(font, tmp / "fonts" / "JYgangbi.ttf")
        write_ass(tmp / "sub.ass", lines, size=size, margin_v=margin_v, outline=outline)

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


# ---------------------------------------------------------------------------
def calibrate(video: Path, reference: Path, lines, ts: float = 2.0) -> dict:
    """用剪映导出的成片当基准，自动搜索 size / margin_v / outline。"""
    tmp = Path(tempfile.mkdtemp(prefix="h3_calib_"))
    font = find_font()
    (tmp / "fonts").mkdir(exist_ok=True)
    shutil.copy2(font, tmp / "fonts" / "JYgangbi.ttf")

    def grab(vid: Path, name: str, vf: str | None = None) -> Image.Image:
        cmd = ["ffmpeg", "-v", "error", "-i", str(vid), "-ss", str(ts)]
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-frames:v", "1", "-y", name]
        subprocess.run(cmd, check=True, cwd=str(tmp))
        return Image.open(tmp / name).convert("RGB")

    def measure(im: Image.Image) -> dict:
        W, H = im.size
        px = im.load()
        white = lambda c: c[0] > 205 and c[1] > 205 and c[2] > 205
        band = [y for y in range(int(H * 0.72), H)
                if sum(1 for x in range(int(W * 0.08), int(W * 0.92)) if white(px[x, y])) >= 25]
        if not band:
            return {"height": 0, "bottom_gap": H, "width": 0}
        top, bot = min(band), max(band)
        cols = [x for x in range(int(W * 0.05), int(W * 0.95))
                for y in range(top, bot + 1) if white(px[x, y])]
        return {"height": bot - top + 1, "bottom_gap": H - bot - 1,
                "width": max(cols) - min(cols) + 1}

    target = measure(grab(reference, "target.png"))
    print("剪映基准:", target)
    size, margin = 80, 113
    for i in range(4):
        write_ass(tmp / "c.ass", lines, size=size, margin_v=margin, outline=2)
        m = measure(grab(video, "mine.png", "ass=c.ass:fontsdir=fonts"))
        print(f"  迭代{i}: size={size} margin={margin} → {m}")
        if m["height"] and abs(target["height"] - m["height"]) <= 1 \
                and abs(target["bottom_gap"] - m["bottom_gap"]) <= 1:
            break
        if m["height"]:
            size = max(20, round(size * target["height"] / m["height"]))
        margin += target["bottom_gap"] - m["bottom_gap"]

    clean = grab(video, "clean.png")
    jy = grab(reference, "jy.png")
    band = (400, 860, 1080, 1010)

    def contrib(img):
        return ImageChops.difference(img.crop(band).convert("L"), clean.crop(band).convert("L"))

    ref_c = contrib(jy)
    best = (0, 1e9)
    for o in (0, 0.5, 1, 1.5, 2):
        write_ass(tmp / "c.ass", lines, size=size, margin_v=margin, outline=o)
        mine = grab(video, "mine.png", "ass=c.ass:fontsdir=fonts")
        diff = ImageChops.difference(contrib(mine), ref_c).histogram()
        score = sum(i * n for i, n in enumerate(diff)) / max(1, sum(diff))
        print(f"  outline={o}: {score:.2f}")
        if score < best[1]:
            best = (o, score)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"size": size, "margin_v": margin, "outline": best[0]}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="按剪映观感烧录草稿歌词字幕")
    p.add_argument("--draft", required=True, help="剪映草稿名，如 '9月16日 (4)'")
    p.add_argument("--video", required=True, help="输入成片（建议用无字幕原片）")
    p.add_argument("--out", help="输出文件；不给则打印字幕清单")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--size", type=int, default=STYLE["size"])
    p.add_argument("--margin-v", type=int, default=STYLE["margin_v"])
    p.add_argument("--outline", type=float, default=STYLE["outline"])
    p.add_argument("--calibrate", action="store_true", help="以 --reference 为准自动标定参数")
    p.add_argument("--reference", help="剪映导出的成片（标定基准）")
    a = p.parse_args(argv)

    lines = read_draft_lines(a.draft)
    print(f"草稿「{a.draft}」字幕 {len(lines)} 句：")
    for s, e, t in lines:
        print(f"  {s:7.2f} → {e:7.2f}  {t}")
    if a.calibrate:
        if not a.reference:
            print("--calibrate 需要 --reference")
            return 2
        style = calibrate(Path(a.video), Path(a.reference), lines)
        print("标定结果:", style)
        return 0
    if not a.out:
        return 0
    burn(Path(a.video), Path(a.out), lines, size=a.size, margin_v=a.margin_v,
         outline=a.outline, fps=a.fps)
    print("已输出:", a.out, f"{Path(a.out).stat().st_size/1024/1024:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
