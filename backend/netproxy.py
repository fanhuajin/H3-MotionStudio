"""出站请求的**代理解析**：本机有些站点直连不通，但用户开着代理。

## 为什么要这个模块（2026-09-22 事故）

用户报「怎么生成的人物图完成不像就是抽帧图啊怎么回事昨天还可以啊」。
查下来：图**根本没生成** —— 中转站 `api.teamorouter.com` 当时**直连不通**
（DNS 把它解析到无效 IP，TCP 连不上/超时），而用户机器上就开着 Clash
（混合端口 127.0.0.1:7897，`curl -x http://127.0.0.1:7897` 打到中转站是 401/200，
说明链路本身好的，只是必须走代理）。后端 httpx 既不读 Windows 系统代理、
也没配代理，于是三套方案（multi/image[]、multi/image、composite）全部
`ConnectError: All connection attempts failed`；`batch_worker` 按设计**退回源视频取帧**
当候选人物图 —— 用户看到的就是「人物图 = 抽帧图」。

**为什么不用「打开 Clash 的系统代理」了事**：httpx 默认不读 Windows 注册表里的系统代理
（那是 WinINET 的设置），所以开了系统代理后端照样直连；而且 Clash 一重启设置就回去了。
所以修在**后端自己解析代理**这一层，并且**只给它该走的域名走**：

  · 中转站（出图）—— 国内直连不通，必须走代理；
  · DeepSeek / 抖音 / QQ音乐 / 本地 ComfyUI —— 实测**直连就是通的**，
    走代理反而多一跳、还可能被节点策略拦（实测 ComfyUI 走代理直接 502）。

## 解析顺序（取第一个非空）

  1. `H3_IMAGE_PROXY` —— 本项目专用，显式钉死用哪个代理；
  2. `H3_BATCH_IMAGE_PROXY` —— 同上的兼容别名；
  3. 环境变量 `HTTPS_PROXY` / `https_proxy` / `ALL_PROXY` / `all_proxy`；
  4. Windows 用户级注册表里的 `H3_IMAGE_PROXY`（后端常由 .bat / 计划任务拉起，
     那种方式不继承用户级变量，见 `settings.env_value` 的同一理由）；
  5. **自动探测**：本机 7897 端口有人监听（Clash Verge 的 `mixed-port`）就用它 ——
     用户是"昨天还能用、今天不行"，多半就是 Clash 重启后系统代理被关掉了，
     这一步让它在**不改任何配置**的情况下恢复。

`H3_NO_PROXY=1`（或 `H3_IMAGE_PROXY=direct`）整体关掉，只走直连。
**本机回环地址永远不代理**（`localhost` / `127.0.0.1` / `::1`）：ComfyUI、自己调自己
（`H3_SELF_URL`）一旦被塞进代理就是 502。
"""
from __future__ import annotations

import os
import socket
import time
from urllib.parse import urlparse

# 自动探测的候选：只有确实"是代理"的常用端口，别乱猜 —— 猜错会把本该直连的请求
# 丢给一个不认识 HTTP CONNECT 的服务（比如某个本地开发服务器）。
AUTO_PROBE_PORTS = (7897, 7890, 10809, 1080)

# 探测结果缓存秒数：点开一条要连十几次中转站，不能每次都去 TCP 探测；
# 但也不能永久缓存 —— 用户随时可能把 Clash 关掉/打开。
_PROBE_TTL = 5.0
_probe_cache: tuple[float, str] | None = None

_TRUTHY_OFF = {"0", "false", "no", "off", "none", "direct", "disable", "disabled"}


def _user_env(name: str) -> str:
    """Windows 用户级环境变量（不缓存：代理是"可能随时变"的东西）。"""
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            return str(winreg.QueryValueEx(handle, name)[0] or "").strip()
    except OSError:
        return ""
    except Exception:  # noqa: BLE001 - 注册表读不到就当作没配
        return ""


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    """端口有人监听吗（只做 TCP 连接，不握手 HTTP）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _auto_proxy() -> str:
    global _probe_cache
    now = time.monotonic()
    if _probe_cache is not None and now - _probe_cache[0] < _PROBE_TTL:
        return _probe_cache[1]
    found = ""
    for port in AUTO_PROBE_PORTS:
        if _port_open(port):
            found = f"http://127.0.0.1:{port}"
            break
    _probe_cache = (now, found)
    return found


def is_local(url: str) -> bool:
    """回环/本机地址：永远直连。"""
    try:
        host = (urlparse(str(url)).hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return True  # 相对地址 = 自己
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".localhost")


def proxy_url() -> str:
    """当前该用的代理地址；空串 = 直连。"""
    for name in ("H3_IMAGE_PROXY", "H3_BATCH_IMAGE_PROXY"):
        value = (os.environ.get(name) or "").strip() or _user_env(name)
        if value:
            return "" if value.lower() in _TRUTHY_OFF else value
    if (os.environ.get("H3_NO_PROXY") or "").strip().lower() not in {"", "0", "false", "no", "off"}:
        return ""
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.environ.get(name) or "").strip()
        if value and value.lower() not in _TRUTHY_OFF:
            return value
    return _auto_proxy()


def for_url(url: str) -> str | None:
    """给 httpx 的 `proxy=` 用：本机地址返回 None（直连），其余按解析结果。"""
    if is_local(url):
        return None
    return proxy_url() or None


# ---------------------------------------------------------------------------
# 「直连优先，直连不通才走代理」（用户 2026-09-22 二次确认的方案）
#
# 为什么不一律走代理：直连本来能通的时候（实测中转站直连 0.3s / 走代理 0.4s），
# 绕一圈只是多一跳，还会把出图节点换到别的出口。为什么不能只直连：VPN 一断
# 直连就超时（就是 2026-09-22 那次事故），必须能自动切到代理。
#
# 判定用中转站的 `/v1/models`：一个**只读、便宜、几秒就返回**的请求，
# 而不是拿真正的出图请求去试 —— 出图一次几十秒到几分钟，试两次等于白烧一次额度。
# 结果缓存 `_ROUTE_TTL` 秒；真正的请求失败（`note_failure`）时立刻作废缓存，
# 下一次调用重新判定 —— VPN 断掉之后最多浪费一次请求就能切到代理。
# ---------------------------------------------------------------------------
_ROUTE_TTL = 60.0
_route_cache: tuple[float, str, str] | None = None  # (时间, 目标 URL, "" 或代理地址)

# 不需要探测、直接用配置的路线（`H3_IMAGE_PROXY` / `H3_NO_PROXY` / 建站环境变量）。
# 返回 (代理地址, 来源说明)；没配就返回 None。
def _configured_route() -> tuple[str, str] | None:
    for name in ("H3_IMAGE_PROXY", "H3_BATCH_IMAGE_PROXY"):
        value = (os.environ.get(name) or "").strip() or _user_env(name)
        if value:
            if value.lower() in _TRUTHY_OFF:
                return "", f"按 {name} 强制直连"
            return value, f"按 {name} 强制走代理"
    if (os.environ.get("H3_NO_PROXY") or "").strip().lower() not in {"", "0", "false", "no", "off"}:
        return "", "按 H3_NO_PROXY 强制直连"
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.environ.get(name) or "").strip()
        if value and value.lower() not in _TRUTHY_OFF:
            return value, f"按 {name} 走代理"
    return None


def _probe(url: str, proxy: str, timeout: float) -> bool:
    """带超时地试一次；直连用短超时（4s），代理给宽一点（8s，要建隧道）。"""
    import httpx

    try:
        with httpx.Client(timeout=timeout, proxy=proxy or None, trust_env=False) as client:
            client.get(url, headers={"Authorization": "Bearer probe"})
        return True
    except Exception:  # noqa: BLE001 - 连不上就是连不上，任何异常都算这条路不通
        return False


def probe_route(url: str, *, timeout: float = 4.0) -> tuple[str, str]:
    """判定打 `url` 该走哪条路，返回 (代理地址 或 ""、说明)。结果缓存一小会儿。

    只对**同一目标**复用缓存（换域名要重新判定）。
    """
    global _route_cache
    configured = _configured_route()
    if configured is not None:
        return configured
    now = time.monotonic()
    if _route_cache is not None and _route_cache[1] == url and now - _route_cache[0] < _ROUTE_TTL:
        proxy = _route_cache[2]
        return proxy, ("直连可用，未使用代理" if not proxy else f"直连不通，已自动改走代理 {proxy}")
    proxy = _auto_proxy()
    if _probe(url, "", timeout):
        _route_cache = (now, url, "")
        return "", "直连可用，未使用代理"
    if proxy and _probe(url, proxy, max(timeout, 8.0)):
        _route_cache = (now, url, proxy)
        return proxy, f"直连不通，已自动改走代理 {proxy}"
    _route_cache = None
    return proxy, (f"直连与代理 {proxy} 都不通" if proxy else "直连不通，且没有可用的代理")


async def probe_route_async(url: str, *, timeout: float = 4.0) -> tuple[str, str]:
    """`probe_route` 的异步版（探测本身是同步阻塞的，丢到线程里跑）。"""
    import asyncio

    return await asyncio.to_thread(probe_route, url, timeout=timeout)


def note_failure() -> None:
    """一次真实请求失败 → 作废路线缓存，下次重新判定（VPN 断掉后能立刻切代理）。"""
    global _route_cache
    _route_cache = None


def describe() -> str:
    """给日志/状态页看的一句话。"""
    configured = _configured_route()
    if configured is not None:
        value, reason = configured
        return f"{reason}（{value}）" if value else reason
    value = _auto_proxy()
    if not value:
        return "直连（本机未发现可用代理端口）"
    if _route_cache is not None and _route_cache[2]:
        return f"直连不通，自动走代理 {value}"
    return f"直连优先；直连不通时自动改用代理 {value}"
