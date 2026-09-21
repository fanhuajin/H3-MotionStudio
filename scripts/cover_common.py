"""封面生成的公共定义：素材解析、规格、已有文件判定。

**为什么单独一个模块**：封面走过两条实现（坐标点击 `make_covers_via_codex.py`、
CDP 静默 `make_covers_via_cdp.py`），一旦"解析文案""判断是否已存在"各写一份，
就会出现一边认、一边不认的荒唐结果 —— 实测就栽过：用户把文件手工命名成
`抖音站3x4.png`，按精确名判存在的脚本看不到它，于是又生成了一张。

所以这里集中三件事，所有入口都从这里取：
  1. `parse_copy()`  解析 `发布文案.txt`（带 BOM、`标题：` 单独占一行的格式）
  2. `SPECS`         两种封面的平台话术 / 目标文件名 / 目标像素
  3. `existing_cover()` 按**宽松匹配**找已有封面（含手工改名的变体）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PUBLISH_ROOT = Path(r"E:\AI_Exports\H3-MotionStudio\发布成品")

# 目标文件名小于这个大小就视为「没生成成功」（半截图/空文件）
MIN_VALID_BYTES = 50 * 1024

# 用户原话（2026-09-16 最终确认）：**只要这几行，不要任何补充要求**
# 2026-09-20 更新：去掉平台品牌名（「B站」/「抖音」会让模型在图上画对应平台的 logo，
# 用户多平台发布、一张封面要在 B站/抖音/小红书等各处复用，所以提示词里只留画幅比例，
# 并显式禁止任何 logo / 水印 / 平台角标）。`{ratio}` 只带画幅话术（4:3 横版 / 3:4 竖版）。
# 2026-09-20 再追加一行**过度裸露**：口径 = 平台规则原文那三条（不得露事业线、不得透视/裸色
# 似未着装、不得紧身到凸点轮廓突出）。吊带/露肩/露腰腹本身不算违规，不必写进提示词。
PROMPT_TEMPLATE = (
    "参考图片的背景和人物，将标题和简介自然融入图片生成 {ratio} 尺寸封面。\n"
    "不要添加任何平台的 Logo、水印、角标或文字（标题和简介本身除外）。\n"
    "人物的服装沿用参考图，不要明显露出事业线/乳沟（乳沟清晰可见才算），不要透视或裸色到像没穿衣服。\n"
    # 2026-09-20 用户：「生成的图片有的手会模糊 禁止模糊」——手必须重画清楚，不许照抄参考图的模糊。
    "人物的手与每根手指必须锐利清晰：不要用景深虚化把手指糊掉，不要运动模糊、不要涂抹感；"
    "参考图里的手若是模糊的，不要照搬，要重画成清晰的手。\n"
    # 2026-09-21 用户：「如果抽帧图片背景里有人需要将其他人去掉，现在生成的背景里的人会变得很奇怪」。
    # 背景清理整段只进人物图（`batch_ai.BACKGROUND_CLEAN_BLOCK`），封面**只加这一句主体约束**：
    # 用户 2026-09-19 明确「封面图不需要出现 背景规则」。
    "画面里只能有参考图里这一个人：不要添加第二个人、路人、观众、伴舞或其他人物，"
    "也不要把人物复制成多个。\n"
    "\n"
    "标题：{title}\n"
    "简介：{intro}"
)


@dataclass
class Folder:
    """一条待出封面的发布目录。"""

    path: Path
    title: str
    intro: str
    portrait: Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def tag(self) -> str:
        """短标签，用于日志与截图命名（编号三位）。"""
        return self.path.name[:3]


# 规格：键 -> (画幅话术, 标准文件名, 目标像素, 容差, 文件名匹配正则)
# 画幅话术**只写比例、不写平台名**（用户 2026-09-20：封面要多平台复用，不能带 B站/抖音
# 字样，否则模型会在图上画对应平台的 logo）。文件名仍保留平台前缀用于区分规格。
SPECS: dict[str, tuple[str, str, tuple[int, int], int, str]] = {
    "bili": ("4:3 横版", "B站4x3.png", (1448, 1086), 12, r"^B站4x3.*\.png$"),
    # 抖音那张要宽松匹配：用户手工改过名（实测出现过「抖音站3x4.png」）
    "douyin": ("3:4 竖版", "抖音3x4.png", (1086, 1448), 12, r"^抖音.*3x4.*\.png$"),
}


def read_sections(copy_path: Path) -> dict[str, str]:
    """把发布文案.txt 解析成 {标题/简介/标签: 正文}。

    实测格式（带 UTF-8 BOM）：段落标签单独占一行，正文在下一行；
    也兼容 `标题：xxx` 同行写法。标签一律不参与出图。
    """
    raw = copy_path.read_text(encoding="utf-8-sig", errors="replace")
    sections: dict[str, str] = {}
    current: str | None = None
    for line in [ln.strip() for ln in raw.splitlines()]:
        hit = next((tag for tag in ("标题", "封面标题", "简介", "标签") if line.startswith(tag)), None)
        if hit:
            current = "标题" if hit == "封面标题" else hit
            inline = ""
            for sep in ("：", ":"):
                if sep in line:
                    inline = line.split(sep, 1)[1].strip()
                    break
            sections[current] = inline
            continue
        if current and line and not line.startswith("#"):
            sections[current] = (sections.get(current, "") + line).strip()
    return sections


def parse_copy(copy_path: Path) -> tuple[str, str]:
    """返回 (标题, 简介)。标题必填；简介可以为空。"""
    sections = read_sections(copy_path)
    title = sections.get("标题", "").strip()
    intro = sections.get("简介", "").strip()
    if not title:  # 没有段落标签时退回第一行非空
        for line in copy_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line and not any(line.startswith(t) for t in ("标题", "简介", "标签")):
                title = line
                break
    if not title:
        raise ValueError(f"{copy_path} 里没有可用的标题")
    return title, intro


def build_prompt(title: str, intro: str, spec: str) -> str:
    """提示词 = 用户原话 + 标题 + 简介，不加任何补充要求。

    2026-09-20：`spec` 只提供画幅比例话术，不把平台品牌名（B站/抖音）写进提示词，
    否则模型会在封面上画对应平台的 logo（用户多平台发布，封面不能带 logo）。
    """
    ratio = SPECS[spec][0]
    text = PROMPT_TEMPLATE.format(ratio=ratio, title=title, intro=intro)
    if not intro:
        text = text.replace("\n简介：", "").rstrip()
    return text


def existing_cover(folder: Path, spec: str) -> Path | None:
    """该目录下**已经存在的**封面（宽松匹配，含手工改名的变体）。

    返回第一个体积达标的文件；存在但太小（半截图）视为没有，需要重做。
    """
    pattern = re.compile(SPECS[spec][4])
    for path in sorted(folder.iterdir()):
        if not path.is_file() or not pattern.match(path.name):
            continue
        try:
            if path.stat().st_size >= MIN_VALID_BYTES:
                return path
        except OSError:
            continue
    return None


def publish_folders(root: Path = PUBLISH_ROOT) -> list[Path]:
    """发布根下的**作品目录**：`singing/` `dance/` 两层（2026-09-20 起）+ 旧的扁平结构兜底。

    用户 2026-09-20：「在发布目录里加一个跳舞文件夹 一个唱歌文件夹各自放对应的内容」。
    排序固定成"先唱歌、再跳舞、各自按名字"，与批量页/图片工作台一致。
    """
    if not root.is_dir():
        return []
    rows: list[tuple[int, str, Path]] = []
    for order, sub in enumerate(("singing", "dance")):
        base = root / sub
        if base.is_dir():
            rows.extend((order, p.name, p) for p in base.iterdir() if p.is_dir())
    rows.extend(
        (len(("singing", "dance")), p.name, p)
        for p in root.iterdir()
        if p.is_dir() and p.name not in {"singing", "dance"}
    )
    return [path for _, _, path in sorted(rows, key=lambda row: (row[0], row[1]))]


def collect_folders(root: Path = PUBLISH_ROOT, filters: list[str] | None = None) -> list[Folder]:
    """列出待处理目录；`filters` 里的片段命中目录名即选中（如 003、夜色）。"""
    if not root.is_dir():
        raise SystemExit(f"发布根目录不存在：{root}")
    out: list[Folder] = []
    for folder in publish_folders(root):
        if filters and not any(f in folder.name for f in filters):
            continue
        portrait = folder / "人物图.png"
        copy_path = folder / "发布文案.txt"
        if not portrait.is_file() or not copy_path.is_file():
            print(f"[跳过] {folder.name}：缺 人物图.png 或 发布文案.txt")
            continue
        title, intro = parse_copy(copy_path)
        out.append(Folder(folder, title, intro, portrait))
    return out
