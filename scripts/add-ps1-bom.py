"""给仓库里的 .ps1 补上 UTF-8 BOM。

**为什么必须补**：Windows PowerShell 5.1（powershell.exe，快捷方式默认调它）读取
**没有 BOM 的 UTF-8** 脚本时，会按系统 ANSI 代码页（这台机器是 GBK）解码 ——
脚本里的中文注释和中文提示全变乱码，更糟的是错位解码会把字符串的引号吃掉，
直接报 `The string is missing the terminator`，脚本根本起不来（实测踩过）。
PowerShell 7 默认按 UTF-8 读，所以这个坑只在 5.1 下暴露。

用法：
    .venv\\Scripts\\python.exe scripts\\add-ps1-bom.py            # 补 scripts/ 下全部
    .venv\\Scripts\\python.exe scripts\\add-ps1-bom.py --check    # 只检查缺哪些
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOM = b"\xef\xbb\xbf"


def needs_bom(path: Path) -> bool:
    raw = path.read_bytes()
    if raw.startswith(BOM):
        return False
    try:
        raw.decode("utf-8")  # 含中文的 UTF-8 脚本才需要；纯 ASCII 加不加都行，但统一加更省事
    except UnicodeDecodeError:
        return False  # 不是 UTF-8（可能是 GBK），别乱动
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给 .ps1 补 UTF-8 BOM")
    parser.add_argument("--check", action="store_true", help="只列出缺 BOM 的文件")
    parser.add_argument("--dir", default="scripts", help="扫描目录（相对项目根）")
    args = parser.parse_args(argv)

    root = PROJECT_ROOT / args.dir
    targets = sorted(root.glob("*.ps1"))
    if not targets:
        print(f"{root} 下没有 .ps1")
        return 0

    missing = [p for p in targets if needs_bom(p)]
    if args.check:
        for path in missing:
            print(f"缺 BOM：{path.relative_to(PROJECT_ROOT)}")
        print(f"共 {len(targets)} 个脚本，缺 BOM {len(missing)} 个")
        return 1 if missing else 0

    for path in missing:
        path.write_bytes(BOM + path.read_bytes())
        print(f"已补 BOM：{path.relative_to(PROJECT_ROOT)}")
    if not missing:
        print(f"{len(targets)} 个脚本都已是 UTF-8 BOM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
