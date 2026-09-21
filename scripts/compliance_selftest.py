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
    "合规可换款式": "为了合规可以换款式、换造型",
    "半透必须垫内衬": "同色、完全不透明",
    "低胸必须盖住胸口": "把胸口盖住",
    # 2026-09-20 用户：「生成的图片有的手会模糊 禁止模糊」——手/人必须锐利，且不许照抄抽帧的模糊
    "禁止模糊块": "【禁止模糊 · 硬要求",
    "禁止景深/浅景深糊手": "禁止景深虚化/浅景深把手糊掉",
    "参考图的模糊不许照搬": "不要照搬那份模糊",
    # 2026-09-21 用户：「抽帧背景里有人要把其他人去掉；背景杂乱要处理一下」——只留本人一个 + 清杂物
    "背景清理块": "【背景清理 · 硬要求",
    "背景里只留本人一个": "只能有图像-2 这一个人",
    "只清杂物不换场景": "保留原场景的类型、空间结构与光线",
    "不许拿虚化糊掉杂乱背景": "不许用重度虚化",
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
        cover = image_workbench.prompt(folder, "bilibili", "dance")
    check(len(manual) > MAX_AUTO_PROMPT, "手工入口仍是长提示词（不动用户调教的那份）",
          f"当前 {len(manual)} 字")
    check("过度裸露" in manual, "手工入口也带着合规约束")
    check("不要照搬" in manual and "锐利清晰" in manual, "手工入口也带着「禁止模糊」")
    check(
        "【背景清理" in manual and "只能有图像-2 这一个人" in manual,
        "手工入口也带着背景清理（其他人全去掉、只留本人）",
    )
    check(
        "【背景清理" not in cover and "只能有参考图里这一个人" in cover,
        "封面只写「不要第二个人」、不写背景风格规则（用户 2026-09-19）",
    )

    # 线上服务：最容易忘的一步（改了代码没重启 = 白改）。
    # ⚠️ 这里**必须比指纹**，不能比"提示词里有没有某个词"：2026-09-20 就用「有没有『过度裸露』」
    # 探过一次，14:21 起的旧进程照样回 ✅（那个词在更早的提交里就有），而它缺的正是当天最新
    # 那条口径。指纹跟着三处源头正文走，改一次口径就变一次。
    local = batch_ai.compliance_fingerprint()
    try:
        import httpx

        config = httpx.get(f"{BASE}/api/config", timeout=15).json()
        live = str(config.get("complianceFingerprint") or "")
        same = live == local
        check(same, f"线上后端已加载当前出图/合规口径（{BASE}）",
              "" if same else f"线上 {live or '（没有这个字段 = 旧进程）'} ≠ 本地 {local}"
                              " → **先重启后端再出图**")
        items = httpx.get(f"{BASE}/api/image-workbench/items", timeout=15).json()["items"]
        if not items:
            # 发布成品被清空时（2026-09-20 用户批量发布完就删光了）没有作品可探：
            # 这一项只是"手工入口也带合规段"的加验，跳过即可，别把它算成失败。
            print("↷ 发布成品里还没有作品 → 跳过「线上手工提示词」这一项")
        else:
            row = items[0]
            served = httpx.get(
                f"{BASE}/api/image-workbench/{row['id']}/prompt/person",
                params={"kind": row.get("kind") or "dance"}, timeout=15,
            ).json()["prompt"]
            carrying = "过度裸露" in served
            check(carrying, "线上手工提示词也带着合规段",
                  "" if carrying else "手工入口的提示词里没有合规段，检查 image_workbench.prompt()")
            cleaned = "【背景清理" in served
            check(cleaned, "线上手工提示词也带着背景清理",
                  "" if cleaned else "手工入口的提示词里没有背景清理段，检查 image_workbench.prompt()")
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
