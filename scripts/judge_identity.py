#!/usr/bin/env python3
"""人物图身份体检：判断 人物图.png 是不是「晚柠.png」本人。

判据用的是仓库已经配好的**视觉模型**端点（`H3_BATCH_TEXT_*`，DeepSeek），
复用 `backend.batch_ai._chat_structured` / `_data_url`，不新增任何依赖。

标定（2026-09-19 实测，别改判据口径而不重新标定）：
  - 没护栏的那张（图一里源视频那个人的脸原样送进去）→ `same_person=False`，**55/100**
  - 原型图 vs 它自己 → `True`，**97/100**（用来自证判据不是"永远说不像"）
  - 带护栏的正常产出 → **78~90/100**

用户定的口径：**只判人物图，60 分以上就算合格；封面不判身份**。

用法：
  python scripts/judge_identity.py                     # 体检发布根下所有已有 人物图.png
  python scripts/judge_identity.py --min-similarity 60 # 指定门槛（默认 60）
  python scripts/judge_identity.py --dir 013           # 只看目录名含 013 的
  python scripts/judge_identity.py --pair A.png B.png  # 任意两张图比对
  python scripts/judge_identity.py --json              # 机器可读输出

退出码：0 = 全部达标（或只做 pair 比对）；1 = 有低于门槛的。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image  # noqa: E402

from backend import batch_ai  # noqa: E402

PUBLISH_ROOT = Path(r"E:\AI_Exports\H3-MotionStudio\发布成品")
IDENTITY = PUBLISH_ROOT / "晚柠.png"
PERSON = "人物图.png"
MIN_BYTES = 50 * 1024        # 小于这个体积视为「没生成成功」（与仓库既有约定一致）

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "same_person": {"type": "boolean"},
        "similarity": {"type": "integer"},
        "differences": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string"},
    },
    "required": ["same_person", "similarity", "differences", "verdict"],
}

INSTRUCTION = (
    "第一张图是**身份原型**（唯一身份锚点）。第二张图是**生成图**。\n"
    "判断第二张图里人物的脸是否与第一张图是**同一个人**。\n"
    "只依据面部结构：脸型、五官比例、眼型与眼距、眼皮结构、眉形、鼻梁鼻头鼻翼、"
    "唇形与唇厚、面颊、下颌线、年龄感。\n"
    "**不要**被发型、发色、妆容、服装、配饰、光线、背景、姿态、镜头角度影响。\n"
    "similarity：0-100 整数（90+ 一眼同一人；60-89 像但有差别；40-59 气质相近但不是同一张脸；"
    "40 以下明显不是）。differences：中文逐条列出具体五官差异（没有就空数组）。"
)


async def judge(identity: Path, candidate: Path) -> dict:
    content = [
        {"type": "text", "text": INSTRUCTION},
        {"type": "image_url", "image_url": {"url": batch_ai._data_url(identity), "detail": "high"}},
        {"type": "image_url", "image_url": {"url": batch_ai._data_url(candidate), "detail": "high"}},
    ]
    return await batch_ai._chat_structured(
        content=content, schema=SCHEMA, name="face_compare", label="人脸比对失败")


def image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


async def run_pair(identity: Path, candidate: Path, min_similarity: int, as_json: bool) -> int:
    for path in (identity, candidate):
        if not path.is_file():
            print(f"文件不存在：{path}")
            return 2
    result = await judge(identity, candidate)
    score = int(result.get("similarity") or 0)
    ok = score >= min_similarity
    if as_json:
        print(json.dumps({"identity": str(identity), "candidate": str(candidate),
                          "min_similarity": min_similarity, "ok": ok, **result}, ensure_ascii=False))
    else:
        print(f"身份原型：{identity}")
        print(f"待判图：  {candidate}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"→ {'合格 ✅' if ok else '低于门槛 ❌'}  相似度 {score}/100（门槛 {min_similarity}）")
    return 0 if ok else 1


async def run_scan(filters: list[str], min_similarity: int, as_json: bool) -> int:
    if not IDENTITY.is_file():
        print(f"找不到身份原型：{IDENTITY}")
        return 2
    rows: list[dict] = []
    for folder in sorted(p for p in PUBLISH_ROOT.iterdir() if p.is_dir()):
        if filters and not any(f in folder.name for f in filters):
            continue
        person = folder / PERSON
        size = image_size(person) if person.is_file() else None
        entry: dict = {"folder": folder.name, "path": str(person), "size": size}
        if size is None or person.stat().st_size < MIN_BYTES:
            entry.update(score=None, same_person=None, ok=False, note="缺人物图或体积不足")
            rows.append(entry)
            if not as_json:
                print(f"❌ {folder.name[:46]:48s} 缺人物图/体积不足")
            continue
        try:
            result = await judge(IDENTITY, person)
        except Exception as exc:
            entry.update(score=None, same_person=None, ok=False, note=f"判据失败：{type(exc).__name__}")
            rows.append(entry)
            if not as_json:
                print(f"⚠ {folder.name[:46]:48s} 判据失败：{type(exc).__name__}")
            continue
        score = int(result.get("similarity") or 0)
        ok = score >= min_similarity
        entry.update(score=score, same_person=bool(result.get("same_person")), ok=ok,
                     differences=(result.get("differences") or [])[:3])
        rows.append(entry)
        if not as_json:
            flag = "✅" if ok else "❌"
            wh = f"{size[0]}x{size[1]}"
            print(f"{flag} {folder.name[:46]:48s} {score:>3}/100  same_person={entry['same_person']!s:5s} {wh}")

    if as_json:
        print(json.dumps({"min_similarity": min_similarity, "identity": str(IDENTITY),
                          "rows": rows}, ensure_ascii=False, indent=2))
    else:
        judged = [r for r in rows if r.get("score") is not None]
        bad = [r for r in judged if not r["ok"]]
        print(f"\n共 {len(judged)} 张已判；≥{min_similarity} 分 {len(judged) - len(bad)} 张，"
              f"低于门槛 {len(bad)} 张")
        for row in bad:
            print(f"  ❌ {row['folder']}  {row['score']}/100")
            for diff in row.get("differences") or []:
                print(f"       · {diff}")
    judged = [r for r in rows if r.get("score") is not None and not r["ok"]]
    return 1 if judged else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="人物图身份体检（对 晚柠.png）")
    parser.add_argument("--dir", dest="filters", action="append", default=[],
                        help="只看目录名含该片段的（可重复）")
    parser.add_argument("--min-similarity", type=int, default=60,
                        help="合格门槛（默认 60，用户 2026-09-19 定的口径）")
    parser.add_argument("--pair", nargs=2, metavar=("身份原型", "待判图"),
                        help="任意两张图比对，不扫目录")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()

    if args.pair:
        return asyncio.run(run_pair(Path(args.pair[0]), Path(args.pair[1]),
                                    args.min_similarity, args.json))
    return asyncio.run(run_scan(args.filters, args.min_similarity, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
