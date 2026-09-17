"""scripts/jianying_draft_report.py — 读剪映草稿，输出可核对的规格报告。

用途（加字幕流程的「验证」环节）：我操作剪映后，用它确认草稿真的生成了什么——
素材路径、画布、每条字幕的文本/时间轴/字体/字号/颜色，一次看全。

用法：
    python scripts/jianying_draft_report.py                # 最新一条草稿
    python scripts/jianying_draft_report.py 9月16日          # 指定草稿
    python scripts/jianying_draft_report.py --list          # 只列草稿
    python scripts/jianying_draft_report.py --json 9月16日   # 机器可读
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DRAFT_ROOT = Path(os.getenv("H3_JIANYING_DRAFT_ROOT")
                  or (Path(os.environ["LOCALAPPDATA"]) / "JianyingPro" / "User Data"
                      / "Projects" / "com.lveditor.draft"))


def drafts() -> list[Path]:
    if not DRAFT_ROOT.is_dir():
        return []
    rows = [d for d in DRAFT_ROOT.iterdir() if d.is_dir() and (d / "draft_content.json").is_file()]
    return sorted(rows, key=lambda d: (d / "draft_content.json").stat().st_mtime, reverse=True)


def load(d: Path) -> dict:
    return json.loads((d / "draft_content.json").read_text(encoding="utf-8"))


def report(d: Path) -> dict:
    j = load(d)
    mats = j.get("materials") or {}
    texts = {m.get("id"): m for m in (mats.get("texts") or [])}
    videos = mats.get("videos") or []
    audios = mats.get("audios") or []

    subs = []
    for t in (j.get("tracks") or []):
        if t.get("type") != "text":
            continue
        for s in (t.get("segments") or []):
            tr = s.get("target_timerange") or {}
            st = tr.get("start") or 0
            du = tr.get("duration") or 0
            m = texts.get(s.get("material_id")) or {}
            try:
                c = json.loads(m.get("content") or "{}")
            except Exception:
                c = {}
            style = (c.get("styles") or [{}])[0]
            font = style.get("font") or {}
            fill = (((style.get("fill") or {}).get("content") or {}).get("solid") or {}).get("color")
            subs.append({
                "start": round(st / 1e6, 3),
                "end": round((st + du) / 1e6, 3),
                "duration": round(du / 1e6, 3),
                "text": c.get("text") or "",
                "size": style.get("size"),
                "font_name": font.get("name") or Path(font.get("path") or "").name,
                "font_path": font.get("path") or "",
                "font_id": font.get("id") or "",
                "color": fill,
            })
    subs.sort(key=lambda s: s["start"])

    return {
        "draft": d.name,
        "draft_path": str(d),
        "canvas": j.get("canvas_config"),
        "fps": j.get("fps"),
        "duration_s": round((j.get("duration") or 0) / 1e6, 3),
        "videos": [{"path": (v.get("path") or "").replace("\\", "/"),
                    "w": v.get("width"), "h": v.get("height"),
                    "duration_s": round((v.get("duration") or 0) / 1e6, 3)} for v in videos],
        "audios": [(a.get("path") or "").replace("\\", "/") for a in audios],
        "track_types": [t.get("type") for t in (j.get("tracks") or [])],
        "subtitle_count": len(subs),
        "subtitles": subs,
    }


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]

    if "--list" in argv:
        for d in drafts():
            print(f"{d.name}")
        return 0

    name = next((a for a in argv if not a.startswith("-")), None)
    if name:
        target = DRAFT_ROOT / name
        if not (target / "draft_content.json").is_file():
            print(f"找不到草稿：{target}", file=sys.stderr)
            return 2
    else:
        found = drafts()
        if not found:
            print(f"草稿目录里没有草稿：{DRAFT_ROOT}", file=sys.stderr)
            return 2
        target = found[0]

    r = report(target)
    if as_json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    print(f"草稿      : {r['draft']}")
    print(f"路径      : {r['draft_path']}")
    print(f"画布      : {r['canvas']}   fps={r['fps']}   总时长={r['duration_s']}s")
    print(f"轨道      : {r['track_types']}")
    for v in r["videos"]:
        print(f"视频素材  : {v['path']}")
        print(f"            {v['w']}x{v['h']}  {v['duration_s']}s")
    for a in r["audios"]:
        print(f"音频素材  : {a}")
    print(f"字幕段数  : {r['subtitle_count']}")
    if r["subtitles"]:
        fonts = {s["font_name"] for s in r["subtitles"]}
        sizes = {s["size"] for s in r["subtitles"]}
        colors = {tuple(s["color"]) if s["color"] else None for s in r["subtitles"]}
        print(f"字体      : {', '.join(sorted(f for f in fonts if f)) or '-'}")
        print(f"字号      : {', '.join(str(s) for s in sorted(s for s in sizes if s is not None)) or '-'}")
        print(f"颜色      : {colors}")
        print("-" * 96)
        for s in r["subtitles"]:
            print(f"  {s['start']:7.2f} → {s['end']:7.2f} ({s['duration']:5.2f}s)  "
                  f"size={s['size']}  {s['text']}")
        gap = [round(r["subtitles"][i + 1]["start"] - r["subtitles"][i]["end"], 3)
               for i in range(len(r["subtitles"]) - 1)]
        if gap:
            print(f"  段间空隙: {gap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
