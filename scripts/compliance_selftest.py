"""scripts/compliance_selftest.py — 改完「出图/合规口径」后的自检（一条命令）。

为什么要有它：2026-09-20 的两条大弯路都是"**改了但没生效**"造成的 ——
  ① 改了提示词却**没重启后端**（uvicorn 没带 `--reload`），出图跑的还是旧代码；
     当天实测：线上返回的 person 提示词 8715 字、不含新加的合规段，白折腾了一轮；
  ② 自动出图那条路被拼回了 6628 字长提示词，合规要求被淹掉 —— 同一张抽帧、同一张身份图，
     长提示词出图被着装判据判**不合规**，精简到 400 字立刻**合规**（相似度还更高：85 vs 72）。

这个脚本把这两件事变成一次输出：**自动提示词长度/关键句 + 手工提示词是否仍为长版 + 线上服务
有没有加载新代码**。任何一项 FAIL 就说明改动没落地，别急着批量重出。

用法：
    python scripts/compliance_selftest.py
退出码：0 = 全部通过；1 = 有 FAIL（看输出里的 ❌）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import batch_ai, image_workbench  # noqa: E402

BASE = "http://127.0.0.1:15111"
MAX_AUTO_PROMPT = 2000  # 自动出图的精简模板上限（长提示词会把合规要求淹没）

# 自动出图提示词里必须出现的关键句（= 当天定死的口径）
REQUIRED = {
    "精简模板开头": "以图像-1（源视频抽帧）为唯一的",
    "平台规则三条": "【过度裸露 · 发布级硬要求",
    "允许少量露出": "只露上胸的一点点",
    "豁免清单": "吊带、抹胸、一字肩、露肩",
    "最小改动·只收领口": "把事业线收住就够了",
    "最小改动·下装同款": "同款、同色、同材质照旧",
    "最小改动·丝袜保留": "丝袜可以保留",
    "禁止换款式": "绝对不要换款式",
    "身份块在末尾": "保住图二的脸",
}

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"{'✅' if ok else '❌'} {label}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    auto = batch_ai.compose_image_prompt("dance")
    print(f"自动出图提示词：{len(auto)} 字")
    check(len(auto) < MAX_AUTO_PROMPT, f"自动出图是精简模板（< {MAX_AUTO_PROMPT} 字）",
          f"当前 {len(auto)} 字；若超过说明又把长提示词拼回来了")
    for label, needle in REQUIRED.items():
        check(needle in auto, f"自动提示词含：{label}")

    check(auto.rstrip().endswith(batch_ai.IDENTITY_PRIORITY_BLOCK.strip()), "身份块在最末尾")
    check(auto.index("【过度裸露") < auto.index(batch_ai.IDENTITY_PRIORITY_BLOCK.strip()),
          "合规块在身份块之前")

    # 手工入口（用户自己调教的长提示词）必须**保持长版**，别被顺手精简
    with tempfile.TemporaryDirectory() as raw:
        folder = Path(raw)
        (folder / "发布文案.txt").write_text("标题：自检\n简介：自检", encoding="utf-8")
        manual = image_workbench.prompt(folder, "person", "dance")
    check(len(manual) > MAX_AUTO_PROMPT, "手工入口仍是长提示词（不动用户调教的那份）",
          f"当前 {len(manual)} 字")
    check("过度裸露" in manual, "手工入口也带着合规约束")

    # 线上服务：最容易忘的一步（改了代码没重启 = 白改）
    try:
        import httpx

        items = httpx.get(f"{BASE}/api/image-workbench/items", timeout=15).json()["items"]
        row = items[0]
        served = httpx.get(
            f"{BASE}/api/image-workbench/{row['id']}/prompt/person",
            params={"kind": row.get("kind") or "dance"}, timeout=15,
        ).json()["prompt"]
        check("过度裸露" in served, f"线上服务已加载新代码（{BASE}）",
              "没看到合规段 → 说明后端还是旧进程，**先重启后端再出图**")
    except Exception as error:  # noqa: BLE001
        check(False, f"线上服务可访问（{BASE}）", f"{type(error).__name__}: {str(error)[:80]}")

    failed = [label for ok, label in results if not ok]
    print()
    if failed:
        print(f"❌ {len(failed)} 项未通过：" + "；".join(failed))
        return 1
    print(f"✅ 全部通过（{len(results)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
