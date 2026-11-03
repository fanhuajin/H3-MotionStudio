"""本机内部调用的凭据（批量流水线自调用自己 API 时用）。

2026-10-23 加登录页之后用户实测：批量制作直接报「请先登录」。原因不是登录坏了，而是
批量流水线**不是直接调函数，而是走 `H3_SELF_URL` 的 HTTP 接口**（提交动作迁移、轮询
子任务、取消子任务、查 ComfyUI 占用、上传人物图、唤醒批次……）。这些自调用是后台
线程发起的，没有浏览器 Cookie，于是被 `AuthMiddleware` 一律 401，条目被记成
「请先登录」直接失败。

这里用一份只落在本机 `data/` 下的随机令牌：内部调用带上 `x-h3-internal` 头即可绕过
登录；令牌只从回环地址接受，浏览器/局域网请求仍然必须登录。放在文件里（而不是每次
进程内存随机）是为了让 `scripts/batch_prep_item.py` 这类**单独进程**的本机脚本也能
读到同一份令牌。能读到 `data/` 的人本来就已经拥有整台机器的文件权限，因此不额外引入
风险。
"""
from __future__ import annotations

import hmac
import os
import secrets
import time
from pathlib import Path

from .settings import DATA_DIR

HEADER = "x-h3-internal"
TOKEN_PATH = DATA_DIR / ".internal-token"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _read() -> str:
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _load() -> str:
    token = _read()
    if len(token) >= 32:
        return token
    token = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # 别的进程正好在创建：等它写完再读，实在读不到就先用本次的令牌（同进程仍可用）。
        for _ in range(40):
            existing = _read()
            if len(existing) >= 32:
                return existing
            time.sleep(0.01)
        return token
    except OSError:
        # `data/` 不可写时退化成进程内令牌：本进程的自调用照常，独立脚本会拿不到。
        return token
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)
    return token


TOKEN = _load()
HEADERS = {HEADER: TOKEN}


def headers() -> dict[str, str]:
    """给 `httpx` 用的请求头副本（调用方可以放心改）。"""
    return dict(HEADERS)


def matches(headers_map, client) -> bool:
    """判断一个 ASGI 请求是不是「本机内部调用」。

    `headers_map` 是 ASGI 的原始 headers 列表转成的字典（字节键），`client` 是
    `scope["client"]`。
    """
    supplied = headers_map.get(HEADER.encode())
    if not supplied:
        return False
    host = (client or ("",))[0]
    if host not in LOOPBACK_HOSTS:
        return False
    try:
        return hmac.compare_digest(supplied, TOKEN.encode())
    except TypeError:
        return False
