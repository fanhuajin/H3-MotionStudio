"""纯代码排版封面：不调用任何模型，把标题/简介按现有发布封面的视觉风格画到底图上。

用法（CLI）：

    python scripts/local_cover_typeset.py --base 底图.png --title 标题 --intro 简介 \
        --kind bilibili --out 封面.png [--font 字体.ttf] [--seed 7]

视觉口径（对齐 `发布成品` 里已有的 B站 / 抖音封面，见 AGENTS.md 的「本地封面效果基准」）：
手绘毛笔标题、逐字轻微旋转与大小抖动、重点词配色、字下的自然刷痕、柔和小装饰，
简介用同风格小字垫在一条半透明笔触上，不压暗、不叠系统字体描边方框。
"""

from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

# 发布尺寸（与 backend/image_workbench.py 的 TARGETS 一致）
TARGETS = {"bilibili": (1448, 1086), "douyin": (1086, 1448)}

# 本机可用的手写/毛笔候选字库（剪映字体缓存 + 仓库里已有的马善政）
FONT_CANDIDATES = {
    "qingxue": r"C:\Users\admin\AppData\Local\JianyingPro\User Data\Cache\AITextTemplate\Resource\18186340445230040290\晴雪体.otf",
    "jianghu": r"C:\Users\admin\AppData\Local\JianyingPro\User Data\Cache\AITextTemplate\Resource\10564411114143764996\HelloFont ID JiangHuTi.ttf",
    "youqi": r"C:\Users\admin\AppData\Local\JianyingPro\User Data\Cache\AITextTemplate\Resource\13444975759583733443\HelloFont ID YouQiTi.ttf",
    "yunji": r"C:\Users\admin\AppData\Local\JianyingPro\User Data\Cache\effect\115815828\7269f0ee6bcdd0cf8a1184618f65cbba\JYyunji.ttf",
    "tuanzi": r"C:\Users\admin\AppData\Local\JianyingPro\User Data\Cache\effect\113096610\fa201c52ba126f103a453b9668f6f442\JYtuanzi.ttf",
    "momo": r"C:\Users\admin\AppData\Local\JianyingPro\User Data\Cache\effect\1424938\64b0eee1f99387b2d7a27f9c961e6bba\默陌专辑手写体.ttf",
    "wenrou": r"C:\Users\admin\AppData\Local\JianyingPro\User Data\Cache\effect\1511616\eb4b8ebef19e89895193d930e7c607c1\温柔且甜腻.ttf",
    "mashanzheng": r"D:\project\H3-MotionStudio\data\local_cover_preview\MaShanZheng-Regular.ttf",
}

WHITE = (255, 252, 248)
PINK = (240, 118, 170)
DEEP_PINK = (232, 92, 152)
YELLOW = (255, 224, 130)
INK = (92, 66, 66)


@dataclass
class Style:
    """一套封面排版风格。"""

    font: Path
    title_size: int = 126
    line_styles: list[tuple[tuple[int, int, int], tuple[int, int, int] | None]] = field(
        default_factory=lambda: [(WHITE, DEEP_PINK), (PINK, WHITE)]
    )
    outline_px: int = 9
    shadow: tuple[int, int, int, int] = (5, 8, 8, 95)  # dx, dy, blur, alpha
    intro_size: int = 34
    intro_color: tuple[int, int, int] = INK
    intro_swash: tuple[tuple[int, int, int], int] = ((255, 252, 248), 236)
    swash_height: int = 54
    swash_color: tuple[int, int, int, int] = (*PINK, 215)
    doodles: bool = True


# ---------------------------------------------------------------- 基础绘制


def render_line(text: str, font: ImageFont.FreeTypeFont, rng: random.Random, *,
                rot: float = 2.6, jitter: float = 0.035) -> Image.Image:
    """逐字渲染一行文字，每个字带轻微旋转、缩放和基线抖动，返回 L 遮罩。"""
    ascent, descent = font.getmetrics()
    margin = int(font.size * 0.55)
    cell_h = ascent + descent + margin * 2
    laid: list[tuple[float, Image.Image]] = []
    for char in text:
        advance = font.getlength(char)
        cell = Image.new("L", (int(advance) + margin * 2, cell_h), 0)
        ImageDraw.Draw(cell).text((margin, margin), char, font=font, fill=255, anchor="la")
        cell = cell.rotate(rng.uniform(-rot, rot), Image.Resampling.BICUBIC, expand=True)
        scale = 1 + rng.uniform(-jitter, jitter)
        if abs(scale - 1) > 1e-3:
            cell = cell.resize((round(cell.width * scale), round(cell.height * scale)),
                               Image.Resampling.LANCZOS)
        laid.append((advance, cell))

    pad = int(font.size * 0.3)
    width = int(sum(a for a, _ in laid)) + margin * 2 + pad * 2
    height = cell_h + pad * 2
    line = Image.new("L", (width, height), 0)
    x = float(margin + pad)
    for advance, cell in laid:
        layer = Image.new("L", (width, height), 0)
        layer.paste(cell, (round(x - (cell.width - advance) / 2),
                           round(pad + (cell.height - cell_h) / 2 + rng.uniform(-font.size * 0.03,
                                                                               font.size * 0.03))))
        line = ImageChops.lighter(line, layer)
        x += advance
    return line.crop(line.getbbox() or (0, 0, 1, 1))


def colorize(mask: Image.Image, style: Style, outline: tuple[int, int, int] | None,
             fill: tuple[int, int, int]) -> Image.Image:
    """把 L 遮罩变成「阴影 + 描边 + 颜色填充」的 RGBA 图层。"""
    box = (mask.width + 4 * (style.outline_px + style.shadow[2]), mask.height + 4 * (style.outline_px + style.shadow[2]))
    offset = (2 * (style.outline_px + style.shadow[2]), 2 * (style.outline_px + style.shadow[2]))
    layer = Image.new("RGBA", box, (0, 0, 0, 0))

    dx, dy, blur, alpha = style.shadow
    shadow = Image.new("L", box, 0)
    shadow.paste(mask, offset)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * alpha / 255))
    layer.paste(Image.new("RGBA", box, (42, 30, 34, 255)), (dx, dy), shadow)

    if outline is not None and style.outline_px > 0:
        grown = Image.new("L", box, 0)
        grown.paste(mask, offset)
        grown = grown.filter(ImageFilter.MaxFilter(2 * style.outline_px + 1)).filter(
            ImageFilter.GaussianBlur(0.8))
        grown = grown.point(lambda v: 255 if v > 90 else 0)
        layer.paste(Image.new("RGBA", box, (*outline, 255)), (0, 0), grown)

    ink = Image.new("L", box, 0)
    ink.paste(mask, offset)
    layer.paste(Image.new("RGBA", box, (*fill, 255)), (0, 0), ink.filter(ImageFilter.GaussianBlur(0.4)))
    return layer


def _noise(size: tuple[int, int], rng: random.Random) -> Image.Image:
    noise = Image.effect_noise(size, 64)
    return noise.filter(ImageFilter.GaussianBlur(0.6)).resize((max(1, size[0] // 6), size[1])).resize(
        size, Image.Resampling.BILINEAR)


def brush_swash(length: int, height: int, color: tuple[int, int, int, int],
                rng: random.Random, angle: float = -1.6, taper: float = 0.0) -> Image.Image:
    """一条毛刷痕（笔触）：边缘柔和、带飞白。taper 越大两端越不收尖（简介垫底用 0.6 更像色块）。"""
    supersample = 3
    wide, high = length * supersample, height * supersample
    mask = Image.new("L", (wide, high), 0)
    pen = ImageDraw.Draw(mask)
    steps = 40
    top: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        profile = (1 - taper) * math.sin(math.pi * t) ** 0.6 + taper * math.sin(math.pi * t) ** 0.18
        thickness = profile * high * (0.5 - 0.18 * abs(t - 0.5) * 2)
        top.append((t * wide, high / 2 - thickness / 2 + rng.uniform(-high * 0.01, high * 0.01)))
    pen.polygon(top + [(x, high - y) for x, y in reversed(top)], fill=255)
    mask = mask.resize((length, height), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(1.4))
    mask = ImageChops.multiply(mask, _noise((length, height), rng).point(lambda v: 150 + v // 3))
    mask = mask.rotate(angle, Image.Resampling.BICUBIC, expand=True)
    layer = Image.new("RGBA", mask.size, color[:3] + (0,))
    layer.putalpha(mask.point(lambda v: int(v * color[3] / 255)))
    return layer


def heart(size: int, color: tuple[int, int, int, int], width: int) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    points = []
    for i in range(121):
        t = 2 * math.pi * i / 120
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        points.append((size / 2 + size * 0.44 * x / 17, size / 2 - size * 0.44 * y / 17))
    ImageDraw.Draw(layer).line(points, fill=color, width=width, joint="curve")
    return layer


def sparkle(size: int, color: tuple[int, int, int, int]) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(layer)
    center = size / 2
    for dx, dy in ((1, 0), (0, 1)):
        pen.polygon([(center - dx * size / 2, center - dy * size / 2),
                     (center + dx * size * 0.10, center + dy * size * 0.10),
                     (center + dx * size / 2, center + dy * size / 2),
                     (center - dx * size * 0.10, center - dy * size * 0.10)], fill=color)
    return layer


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines, current = [], ""
    for char in text:
        if font.getlength(current + char) <= max_width or not current:
            current += char
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def split_title(title: str, lines: int = 2) -> list[str]:
    """标题分行：优先在标点/空格处断开，否则均分。"""
    title = title.strip()
    if lines <= 1 or len(title) <= 6:
        return [title]
    for mark in ("，", " ", "、", "|", "："):
        if mark in title:
            parts = [p for p in title.split(mark) if p]
            if len(parts) == 2:
                return parts
    half = math.ceil(len(title) / 2)
    return [title[:half], title[half:]]


# ---------------------------------------------------------------- 排版


def compose(
    base: Image.Image,
    title: str,
    intro: str,
    kind: str,
    style: Style,
    *,
    seed: int = 7,
    title_lines: int = 2,
    block: tuple[float, float, float, float] | None = None,
) -> Image.Image:
    """把标题和简介排到底图上，返回目标尺寸的封面。"""
    rng = random.Random(seed)
    target = TARGETS[kind]
    canvas = base.convert("RGBA") if base.size == target else ImageOps.fit(
        base.convert("RGBA"), target, method=Image.Resampling.LANCZOS, centering=(0.5, 0.34))
    width, height = canvas.size

    if block is None:
        # 4:3 文字在右半（人物右侧留出干净栏），3:4 文字小一号放左上；两块都留安全边距
        block = (0.48 * width, 0.05 * height, 0.97 * width, 0.62 * height) if kind == "bilibili" \
            else (0.03 * width, 0.05 * height, 0.34 * width, 0.34 * height)
    block_w = block[2] - block[0]

    lines = split_title(title, title_lines)
    lines = [line for line in lines if line.strip()]
    size = style.title_size
    font = ImageFont.truetype(str(style.font), size)
    while size > 44 and max(font.getlength(line) for line in lines) > block_w * 0.93:
        size -= 3
        font = ImageFont.truetype(str(style.font), size)

    gap = int(size * 0.14)
    x = int(block[0])
    y = int(block[1])

    masks: list[Image.Image] = []
    for i, line in enumerate(lines):
        mask = render_line(line, font, rng)
        masks.append(mask)

    title_w = max(mask.width for mask in masks)
    title_h = sum(mask.height for mask in masks) + gap * (len(masks) - 1)

    # 重点词下面的毛刷痕：压在标题下方偏中间的位置
    last = masks[-1]
    swash = brush_swash(int(title_w * 0.92), style.swash_height, style.swash_color, rng)
    canvas.alpha_composite(swash, (x + int(title_w * 0.06), y + title_h - int(style.swash_height * 0.45)))

    cursor_y = y
    for i, mask in enumerate(masks):
        fill, outline = style.line_styles[i % len(style.line_styles)]
        layer = colorize(mask, style, outline, fill)
        canvas.alpha_composite(layer, (x - int(style.outline_px * 2), cursor_y - int(style.outline_px * 2)))
        cursor_y += mask.height + gap

    # 简介：小字垫在一条柔和的浅色笔触上（两行，居中压在笔触里）
    intro = intro.strip()
    if intro:
        small = ImageFont.truetype(str(style.font), style.intro_size)
        intro_lines = wrap(intro, small, min(int(block_w * 0.80), int(title_w * 0.72)))
        line_h = int(style.intro_size * 1.45)
        text_w = max(int(small.getlength(line)) for line in intro_lines)
        pad_x, pad_y = 42, 30
        patch = brush_swash(text_w + pad_x * 2, line_h * len(intro_lines) + pad_y * 2,
                            style.intro_swash[0] + (style.intro_swash[1],), rng, angle=-1.2, taper=0.62)
        px = min(max(6, x + int((title_w - patch.width) * 0.22)), width - patch.width - 6)
        py = min(y + title_h + int(size * 0.34), height - patch.height - 6)
        canvas.alpha_composite(patch, (px, py))
        pen = ImageDraw.Draw(canvas)
        for i, line in enumerate(intro_lines):
            text_x = px + (patch.width - int(small.getlength(line))) // 2
            pen.text((text_x, py + pad_y + i * line_h), line, font=small, fill=style.intro_color + (255,))

    if style.doodles:
        # 小装饰只允许落在文字栏里（含 10px 溢出），避免压到人物
        left_limit = int(block[0]) - 10
        right_limit = width - 6

        def place(layer: Image.Image, dx: int, dy: int) -> None:
            canvas.alpha_composite(layer, (min(max(left_limit, dx), right_limit - layer.width),
                                           min(max(0, dy), height - layer.height)))

        place(heart(int(size * 0.42), (*DEEP_PINK, 240), max(4, size // 26)),
              x + title_w + int(size * 0.05), y - int(size * 0.10))
        place(sparkle(int(size * 0.34), (*YELLOW, 235)), x - int(size * 0.16), y + int(size * 0.18))
        place(sparkle(int(size * 0.26), (*WHITE, 225)), x + title_w + int(size * 0.02),
              y + title_h - int(size * 0.30))
    return canvas.convert("RGB")


def main() -> None:
    parser = argparse.ArgumentParser(description="纯代码排版封面（标题 + 简介）")
    parser.add_argument("--base", required=True, type=Path, help="底图（人物图或扩图后的底图）")
    parser.add_argument("--title", required=True)
    parser.add_argument("--intro", default="")
    parser.add_argument("--kind", default="bilibili", choices=sorted(TARGETS))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--font", default="qingxue", help=f"内置名或字体文件路径：{', '.join(FONT_CANDIDATES)}")
    parser.add_argument("--lines", type=int, default=2, help="标题行数")
    parser.add_argument("--size", type=int, default=126, help="标题字号（4:3 基准）")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--block", default="", help="文字区域，相对画布的比例 x0,y0,x1,y1（默认按平台给）")
    args = parser.parse_args()

    font = Path(FONT_CANDIDATES.get(args.font, args.font))
    if not font.is_file():
        raise SystemExit(f"字体不存在：{font}")
    style = Style(font=font, title_size=args.size if args.kind == "bilibili" else int(args.size * 0.88))
    block = None
    if args.block:
        values = [float(v) for v in args.block.replace("，", ",").split(",")]
        if len(values) != 4:
            raise SystemExit("--block 需要 4 个 0~1 的比例值：x0,y0,x1,y1")
        width, height = TARGETS[args.kind]
        block = (values[0] * width, values[1] * height, values[2] * width, values[3] * height)
    cover = compose(Image.open(args.base), args.title, args.intro, args.kind, style,
                    seed=args.seed, title_lines=args.lines, block=block)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    cover.save(args.out, optimize=True)
    print(f"{args.out}  {cover.size}")


if __name__ == "__main__":
    main()
