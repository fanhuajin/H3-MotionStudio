"""backend.douyin_direct — 抖音「直连下载」路由（绕开被 Argus 门禁的 Web 详情接口）。

## 为什么需要它

批量下载原先走 `D:\\project\\douyin-downloader` 的 REST 服务，它取作品详情用的是
`/aweme/v1/web/aweme/detail/`。这个接口现在被抖音的 **ArgusSecurityPlugin** 门禁：

- 只带 cookie → `403 Uifid Not Found`
- 补上 `uifid` → `403 Signature Not Found`（还需要页面 SDK 的 `x-secsdk-web-signature`）

而下载器那边的「页面签名通道」`core/page_bridge.py` 是**桌面版专有**的，开源版没有，
所以这条路会**时好时坏**（实测同一台机器一会儿成功一会儿 403）。

## 思路（2026-09-15 实测通过，2026-09-16 加固）

```
① iPhone UA 拉分享页  https://www.iesdouyin.com/share/video/{aweme_id}/
     → HTML 里有  "play_addr":{"uri":"v2800fgi…"（video_id）、desc、hashtag_name
② 用 video_id 换 CDN 直链
     https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0
     → 302 到 CDN 地址（**这一步不需要任何签名**）
③ 带 referer 下载
```

`aweme.snssdk.com` 是 App 端旧域名，`/aweme/v1/play/` 是给播放器喂流的直链接口，
不承担反爬职责，所以只校验 `video_id` 合法性与时间戳，**不需要 a_bogus / msToken**。

来源参考：`cat-xierluo/legal-skills` 的 `douyin-nocookie-approach.md`（含 2026-08-30 补记）。
**注意**：分享页必须用 **iPhone UA**，桌面 Chrome UA 只会拿到配置页（没有视频数据）。

## 2026-09-16 两个实测坑（用户又看到「Cookie 可能已失效」的根因）

1. **video_id 前缀变了**：旧代码写死 `"uri":"v0…"`，而这次拿到的是
   `v2800fgi0000d8qi147og65j651va0ig`（`v2` 开头）。正则失配 → 分享页明明有数据也解析不出
   video_id → 兜底整条失败，最后把下载器那句「Cookie 可能已失效」原样抛给用户。
   现在锚定 `play_addr` 但不锁前缀（见 `extract_video_id`）。
2. **分享页会概率性被换成壳页**（`video_layout:null`，没有 `play_addr`，HTTP 仍是 200）。
   同一个作品连续请求时好时坏，实测同一 cookie 下一次壳页一次有数据 —— 所以要**重试**
   （`SHARE_ATTEMPTS` 轮、间隔 `SHARE_RETRY_SECONDS`，与 GitHub 上 `legal-skills` 0.5.1
   「两轮重试、间隔 3s」同一结论），并且两个分享域名轮着来。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger("batch.douyin_direct")

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
# 两个分享页渲染的是同一份 SSR 数据，但壳页是**按请求**概率出现的，所以轮着试。
SHARE_URLS = (
    "https://www.iesdouyin.com/share/video/{aweme_id}/",
    "https://m.douyin.com/share/video/{aweme_id}",
)
PLAY_HOSTS = ("https://aweme.snssdk.com", "https://api.amemv.com")
PLAY_PATH = "/aweme/v1/play/"
RATIOS = ("1080p", "720p", "540p")
SHARE_ATTEMPTS = 4
SHARE_RETRY_SECONDS = 1.5

# 锚定 play_addr 再取里面的 uri：不锁 `v0` 前缀（实测有 v2/v3 开头），也避开封面等其它 uri。
PLAY_ADDR_URI = re.compile(r'"play_addr[^"]*"\s*:\s*\{[^{}]*?"uri"\s*:\s*"(v[0-9a-zA-Z]{10,})"')
ANY_VIDEO_URI = re.compile(r'"uri"\s*:\s*"(v[0-9a-zA-Z]{10,})"')
URL_VIDEO_ID = re.compile(r"[?&]video_id=(v[0-9a-zA-Z]{10,})")


class DouyinDirectError(RuntimeError):
    """直连路由失败。消息里带「卡在哪一步」，批量条目直接把这句话显示给用户。"""


def _downloader_root() -> Path:
    import os
    return Path(os.getenv("H3_DOUYIN_DOWNLOADER_ROOT", r"D:\project\douyin-downloader"))


def load_cookies() -> dict[str, str]:
    """借用下载器已登录的 cookie（纯读取，不修改）。

    带 cookie 的分享页比游客更容易渲染出 `play_addr`（GitHub 上 `legal-skills` 0.5.0
    的实测结论：「SSR share 页对无 cookie 请求停止渲染 `play_addr.uri`」）。
    """
    import json
    path = _downloader_root() / ".cookies.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(raw, list):
        return {str(c.get("name")): str(c.get("value")) for c in raw if isinstance(c, dict)}
    return {str(k): str(v) for k, v in raw.items()}


def resolve_aweme_id(url: str, client: httpx.Client) -> str | None:
    """从链接里取作品号；短链（v.douyin.com/xxx）跟随重定向解析。"""
    match = re.search(r"/(?:video|note|gallery|slides)/(\d+)", url)
    if not match:
        match = re.search(r"[?&]modal_id=(\d+)", url)
    if match:
        return match.group(1)
    try:
        resp = client.get(url, follow_redirects=True)
    except Exception:
        return None
    match = re.search(r"/(?:video|note|gallery|slides)/(\d+)", str(resp.url))
    return match.group(1) if match else None


def extract_video_id(html: str) -> str | None:
    """从分享页 HTML 里取 video_id（SSR 数据里的 `play_addr.uri`）。

    **不要**再写回 `v0[0-9a-z]+`：2026-09-16 实测这批作品的 video_id 是
    `v2800fgi0000d8qi147og65j651va0ig`（`v2` 开头），旧前缀直接失配，于是分享页明明
    渲染出了完整数据也判成「拿不到 video_id」—— 用户看到的「Cookie 可能已失效」就是这么来的。
    """
    idx = html.find('"play_addr"')
    if idx >= 0:
        match = PLAY_ADDR_URI.search(html, idx)
        if not match:
            # 中间夹了别的对象（`[^{}]*?` 过不去）时，就近取第一个 uri
            match = re.search(r'"uri"\s*:\s*"(v[0-9a-zA-Z]{10,})"', html[idx:idx + 2000])
        if match:
            return match.group(1)
    match = ANY_VIDEO_URI.search(html)
    if match:
        return match.group(1)
    match = URL_VIDEO_ID.search(html)
    return match.group(1) if match else None


def _decode_json_string(value: str) -> str:
    """SSR 数据里的字符串可能带 \\uXXXX / \\u002F 转义（`legal-skills` 记的坑）。

    纯中文（没有转义）时 roundtrip 会抛异常，直接原样返回即可。
    """
    if "\\" not in value:
        return value
    try:
        return value.encode("utf-8").decode("unicode_escape").encode("latin1").decode("utf-8")
    except Exception:
        return value


def _share_metadata(html: str) -> dict[str, Any]:
    desc = re.search(r'"desc"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    nickname = re.search(r'"nickname"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    tags = re.findall(r'"hashtag_name"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    return {
        "desc": _decode_json_string(desc.group(1)) if desc else "",
        "nickname": _decode_json_string(nickname.group(1)) if nickname else "",
        "tags": [_decode_json_string(item) for item in tags][:20],
    }


def share_payload(
    aweme_id: str,
    client: httpx.Client,
    *,
    attempts: int = SHARE_ATTEMPTS,
    retry_seconds: float = SHARE_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    """拉分享页（iPhone UA），取出 video_id 与作品文案；壳页就重试。

    壳页判定：HTTP 200 但 HTML 里没有 `play_addr.uri`（抖音把详情换成 `video_layout:null`
    的空壳 + 风控标记）。实测**同一个作品、同一份 cookie**，一次壳页一次有数据，所以这不是
    「作品没了」，重试就能过。
    """
    reason = "还没有请求过分享页"
    for attempt in range(1, max(1, int(attempts)) + 1):
        url = SHARE_URLS[(attempt - 1) % len(SHARE_URLS)].format(aweme_id=aweme_id)
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001 - 换下一个域名/下一轮
            reason = f"{url} 请求异常（{exc}）"
            logger.warning("拉取分享页失败：%s（%s）", aweme_id, exc)
        else:
            if resp.status_code != 200:
                reason = f"{url} 返回 HTTP {resp.status_code}"
                logger.warning("分享页返回 %s：%s", resp.status_code, aweme_id)
            else:
                video_id = extract_video_id(resp.text)
                if video_id:
                    payload = _share_metadata(resp.text)
                    payload["videoId"] = video_id
                    return payload
                reason = f"{url} 只返回壳页（没有 play_addr.uri）"
                logger.warning(
                    "分享页里没有 video_id（第 %d/%d 轮，可能被风控壳页替换）：%s",
                    attempt,
                    attempts,
                    aweme_id,
                )
        if attempt < attempts:
            sleep(retry_seconds)
    logger.warning("分享页连续 %d 轮都没拿到 video_id：%s（%s）", attempts, aweme_id, reason)
    return None


def play_url(
    video_id: str,
    client: httpx.Client,
    *,
    hosts: tuple[str, ...] = PLAY_HOSTS,
    ratios: tuple[str, ...] = RATIOS,
) -> tuple[str, str] | None:
    """用 video_id 换 CDN 直链，返回 (直链, 清晰度)。"""
    for ratio in ratios:
        for host in hosts:
            try:
                resp = client.get(
                    f"{host}{PLAY_PATH}",
                    params={"video_id": video_id, "ratio": ratio, "line": "0"},
                    follow_redirects=False,
                )
            except Exception:
                continue
            if resp.status_code in (301, 302):
                location = resp.headers.get("location") or ""
                if location:
                    return location, ratio
    return None


def fetch(
    url: str,
    dest_dir: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """直连下载：返回 (落盘文件, 元信息)；失败抛 `DouyinDirectError`（带卡住的步骤）。

    元信息形如 `{"awemeId","desc","tags","nickname","videoId","ratio"}`，
    与下载器 `_manifest_metadata` 的结构保持兼容，便于直接塞进条目。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": IPHONE_UA,
        "Referer": "https://www.douyin.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    with httpx.Client(headers=headers, cookies=load_cookies(), timeout=40) as client:
        aweme_id = resolve_aweme_id(url, client)
        if not aweme_id:
            raise DouyinDirectError("解析不出作品号（链接可能已失效，或不是抖音视频链接）")
        payload = share_payload(aweme_id, client)
        if not payload:
            raise DouyinDirectError(
                f"分享页连续 {SHARE_ATTEMPTS} 轮都被抖音换成壳页，拿不到 video_id"
            )
        resolved = play_url(payload["videoId"], client)
        if not resolved:
            raise DouyinDirectError("拿不到播放直链（aweme/v1/play 没有返回 302）")
        cdn, ratio = resolved

        first_line = (payload["desc"] or "").splitlines()[0].strip()
        safe = re.sub(r'[\\/:*?"<>|\s#]+', " ", first_line)[:60].strip() or "douyin"
        dest = dest_dir / f"{safe}_{aweme_id}.mp4"

        with client.stream("GET", cdn, follow_redirects=True) as resp:
            if resp.status_code not in (200, 206):
                raise DouyinDirectError(f"CDN 直链下载失败：HTTP {resp.status_code}")
            total = int(resp.headers.get("content-length") or 0)
            written = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(256 * 1024):
                    fh.write(chunk)
                    written += len(chunk)
                    if on_progress:
                        on_progress(written, total)
        if not dest.is_file() or dest.stat().st_size < 10_000:
            raise DouyinDirectError("下载到的文件过小（可能只拿到错误页）")
        # CDN 声称的 content-length 没拿满 = 只写了半截文件。httpx 对 content-length
        # 不匹配通常会自己抛，但 206/分块响应下会静默收尾，落到 ComfyUI 里才炸成
        # 「No frames generated」（2026-09-19 条目 012 就是这种半截文件）。
        if total and written < total:
            dest.unlink(missing_ok=True)
            raise DouyinDirectError(
                f"CDN 直链只传了 {written} 字节（应为 {total} 字节），文件不完整，已丢弃"
            )

        return dest, {
            "awemeId": aweme_id,
            "desc": payload["desc"],
            "tags": payload["tags"],
            "nickname": payload["nickname"],
            "videoId": payload["videoId"],
            "ratio": ratio,
            "source": "direct",
        }
