"""scripts/make_jianying_template.py — 从一条**已验证可用**的剪映草稿生成加字幕用的模板。

## 为什么需要模板

批量加字幕时，`backend/jianying_recognize.clone_draft()` 要造一条"只有视频轨"的剪映草稿
（给剪映「识别歌词」用）。草稿 JSON 字段多且版本相关，**照抄一条真实草稿再裁**比手写可靠。

模板 = 真实草稿 **去掉字幕轨与字幕素材** + 把视频路径换成占位符 `__VIDEO_PATH__`，
运行时再按本条成片改路径/时长/画布。

## 用法

    python scripts/make_jianying_template.py --draft "9月16日 (4)"
    python scripts/make_jianying_template.py --draft "..." --out data/jianying_template

选哪条草稿都行，只要它是**剪映自己能正常打开**的（打开一次、跑过「识别歌词」更稳妥）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.jianying_recognize import PLACEHOLDER, TEMPLATE  # noqa: E402

DRAFT_ROOT = Path(os.getenv("H3_JIANYING_DRAFT_ROOT")
                  or (Path(os.environ["LOCALAPPDATA"]) / "JianyingPro" / "User Data"
                      / "Projects" / "com.lveditor.draft"))
DROP_FILES = ("draft_content.json.bak", "template.tmp", "template-2.tmp", ".locked")


def build(draft_name: str, out: Path) -> Path:
    src = DRAFT_ROOT / draft_name
    if not (src / "draft_content.json").is_file():
        raise FileNotFoundError(f"找不到草稿：{src}")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out)
    for name in DROP_FILES:
        p = out / name
        if p.is_file():
            p.unlink()

    content = json.loads((out / "draft_content.json").read_text(encoding="utf-8"))
    mats = content.setdefault("materials", {})
    removed_texts = len(mats.get("texts") or [])
    mats["texts"] = []
    content["tracks"] = [t for t in content.get("tracks") or [] if t.get("type") != "text"]
    for video in mats.get("videos") or []:
        video["path"] = PLACEHOLDER
        video["material_name"] = Path(PLACEHOLDER).name
    (out / "draft_content.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    meta_path = out / "draft_meta_info.json"
    if meta_path.is_file():
        raw = meta_path.read_text(encoding="utf-8")
        # 把原草稿的绝对路径与名字换成占位符（运行时由 clone_draft 填）
        raw = raw.replace(str(src).replace("\\", "/"), "__DRAFT_FOLD__")
        raw = raw.replace(src.name, "__DRAFT_NAME__")
        meta = json.loads(raw)
        meta["draft_name"] = "__DRAFT_NAME__"
        meta["draft_fold_path"] = "__DRAFT_FOLD__"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    print(f"模板已生成：{out}")
    print(f"  轨道：{[(t.get('type'), len(t.get('segments') or [])) for t in content['tracks']]}")
    print(f"  已移除字幕素材 {removed_texts} 个；视频路径占位符 = {PLACEHOLDER}")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="从已验证草稿生成剪映字幕草稿模板")
    ap.add_argument("--draft", required=True, help="源草稿名（剪映首页显示的名字）")
    ap.add_argument("--out", default=str(TEMPLATE))
    a = ap.parse_args(argv)
    build(a.draft, Path(a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
