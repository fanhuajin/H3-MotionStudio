"""scripts/lock_jianying_update.py — 锁死剪映版本，禁止自动更新（可回滚）。

背景（2026-09-16 用户要求「禁止更新 保持本版本」）：
  · 本机用的是**免安装解锁版 5.9.0.11632**（`D:\\soft\\剪映以激活版本免安装\\...\\解锁版.exe`）；
  · 剪映的自动更新器会把新版下载到 `%LOCALAPPDATA%\\JianyingPro\\Apps\\<版本>` 并顶掉解锁版，
    实测它曾把 **11.3.0.14362** 下载进来（还建了一条 11.3 格式的空草稿）；
  · 用户的所有草稿都是 5.9.0 写的，被更新后解锁/兼容性都会出问题。

锁定手段（都不需要管理员权限，全部可回滚）：
  1) `User Data\\Config\\globalSetting`：enableAutoUpdate / totalSilentUpgradeSwitch → false，
     upgradeIntervalMin → 999999
  2) `User Data\\Config\\updateInfo`：joinBeta → false（退出内测通道）
  3) `解锁版\\JianyingPro\\Configure.ini`：last_version 记录为当前版本
  4) `5.9.0.11632\\update.exe` 改名停用
  5) 前两个配置文件设为只读，防止程序退出时把开关写回去

所有文件都**逐字节替换**（latin-1 往返，不改编码），改动前留 `.h3bak` 备份。
回滚：把 `.h3bak` 覆盖回去、把 `update.exe.h3disabled` 改回 `update.exe`、去掉只读属性。

用法：
    python scripts/lock_jianying_update.py            # 应用锁定
    python scripts/lock_jianying_update.py --check     # 只看现状
    python scripts/lock_jianying_update.py --revert    # 回滚
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

CRACK = Path(os.getenv("H3_JIANYING_ROOT")
             or r"D:\soft\剪映以激活版本免安装\剪映以激活版本免安装\剪映解锁版\JianyingPro")
VERSION = os.getenv("H3_JIANYING_VERSION", "5.9.0.11632")
CFG = Path(os.environ["LOCALAPPDATA"]) / "JianyingPro" / "User Data" / "Config"
GLOBAL = CFG / "globalSetting"
UPDATE_INFO = CFG / "updateInfo"
CONFIGURE = CRACK / "Configure.ini"
UPDATER = CRACK / VERSION / "update.exe"
DISABLED = UPDATER.with_name(UPDATER.name + ".h3disabled")
BAK = ".h3bak"

PATCHES = {
    GLOBAL: [
        (b"enableAutoUpdate=true", b"enableAutoUpdate=false"),
        (b"totalSilentUpgradeSwitch=true", b"totalSilentUpgradeSwitch=false"),
        (b"upgradeIntervalMin=30", b"upgradeIntervalMin=999999"),
        (b"upgradeIntervalMin=0", b"upgradeIntervalMin=999999"),
    ],
    UPDATE_INFO: [(b"joinBeta=true", b"joinBeta=false")],
    CONFIGURE: [(b"last_version=\r\n", f"last_version={VERSION}\r\n".encode()),
                (b"last_version=\n", f"last_version={VERSION}\n".encode())],
}
READONLY = (GLOBAL, UPDATE_INFO)
KEYS = ("enableAutoUpdate", "totalSilentUpgradeSwitch", "upgradeIntervalMin", "joinBeta")


def jianying_running() -> list[str]:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         errors="ignore").stdout
    return [ln for ln in out.splitlines() if ("Jianying" in ln or "解锁版" in ln)]


def show() -> None:
    print("==== 当前状态 ====")
    for p in (GLOBAL, UPDATE_INFO):
        if p.is_file():
            ro = not bool(p.stat().st_mode & stat.S_IWRITE)
            vals = [ln.strip() for ln in p.read_bytes().decode("latin-1").splitlines()
                    if any(k in ln for k in KEYS)]
            print(f"  {p.name} [{'只读' if ro else '可写'}]: {vals}")
    print(f"  Configure.ini: {CONFIGURE.read_text(encoding='utf-8', errors='replace').strip() if CONFIGURE.is_file() else '不存在'}")
    print(f"  update.exe 在用: {UPDATER.is_file()}   已停用: {DISABLED.is_file()}")
    apps = Path(os.environ["LOCALAPPDATA"]) / "JianyingPro" / "Apps"
    print(f"  更新下载目录 Apps: {'存在（可能已下载新版）: ' + str([d.name for d in apps.iterdir()]) if apps.is_dir() else '不存在 ✓'}")


def apply(revert: bool = False) -> int:
    if jianying_running():
        print("⚠ 剪映正在运行，请先完全退出（否则退出时会把配置写回去）")
        return 2
    if revert:
        for p in list(PATCHES) + [UPDATER]:
            bak = p.with_name(p.name + BAK)
            if bak.is_file():
                p.chmod(p.stat().st_mode | stat.S_IWRITE)
                shutil.copy2(bak, p)
                print("  已回滚:", p.name)
        if DISABLED.is_file() and not UPDATER.is_file():
            shutil.copy2(DISABLED, UPDATER)
            print("  已恢复: update.exe")
        show()
        return 0

    for path, pairs in PATCHES.items():
        if not path.is_file():
            print(f"  [跳过] 不存在: {path}")
            continue
        data = path.read_bytes()
        original = data
        notes = []
        for old, new in pairs:
            if old in data and old != new:
                data = data.replace(old, new)
                notes.append(f"{old.decode()}→{new.decode()}")
        if data == original:
            print(f"  [无需改动] {path.name}")
            continue
        bak = path.with_name(path.name + BAK)
        if not bak.exists():
            shutil.copy2(path, bak)
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
        path.write_bytes(data)
        print(f"  [已改] {path.name}: {'; '.join(notes) or '（仅规范化行尾）'}")

    if UPDATER.is_file():
        shutil.copy2(UPDATER, DISABLED)
        UPDATER.unlink()
        print(f"  [已停用] update.exe → {DISABLED.name}")

    for p in READONLY:
        if p.is_file():
            p.chmod(p.stat().st_mode & ~stat.S_IWRITE)
            print(f"  [只读] {p.name}")

    print()
    show()
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="锁定剪映版本、禁止自动更新")
    ap.add_argument("--check", action="store_true", help="只看现状")
    ap.add_argument("--revert", action="store_true", help="回滚到官方默认")
    a = ap.parse_args(argv)
    if a.check:
        show()
        return 0
    return apply(revert=a.revert)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
