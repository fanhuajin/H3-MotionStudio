"""backend.douyin_direct — 抖音「直连兜底」下载（绕开被 Argus 门禁的 Web 详情接口）。

## 为什么需要它

批量下载走的是 `D:\\project\\douyin-downloader` 的 REST 服务，它取作品详情用的是
`/aweme/v1/web/aweme/detail/`。这个接口现在被抖音的 **ArgusSecurityPlugin** 门禁：

- 只带 cookie → `403 Uifid Not Found`
- 补上 `uifid` → `403 Signature Not Found`（还需要页面 SDK 的 `x-secsdk-web-signature`）

而下载器那边的「页面签名通道」`core/page_bridge.py` 是**桌面版专有**的，开源版没有，
所以这条路会**时好时坏**（实测同一台机器一会儿成功一会儿 403）。

## 兜底思路（2026-09-15 实测通过）

```
① iPhone UA 拉分享页  https://www.iesdouyin.com/share/video/{aweme_id}/
     → HTML 里有  "uri":"v0300fg10000..."（video_id）、desc、hashtag_name
② 用 video_id 换 CDN 直链
     https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0
     → 302 到 CDN 地址（**这一步不需要任何签名**）
③ 带 referer 下载
```

`aweme.snssdk.com` 是 App 端旧域名，`/aweme/v1/play/` 是给播放器喂流的直链接口，
不承担反爬职责，所以只校验 `video_id` 合法性与时间戳，**不需要 a_bogus / msToken**。

来源参考：`cat-xierluo/legal-skills` 的 `douyin-nocookie-approach.md`（含 2026-08-30 补记）。
**注意**：分享页必须用 **iPhone UA**，桌面 Chrome UA 只会拿到配置页（没有视频数据）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger("batch.douyin_direct")

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
SHARE_URL = "https://www.iesdouyin.com/share/video/{aweme_id}/"
PLAY_URL = "https://aweme.snssdk.com/aweme/v1/play/"
RATIOS = ("1080p", "720p", "540p")


def _downloader_root() -> Path:
    import os
    return Path(os.getenv("H3_DOUYIN_DOWNLOADER_ROOT", r"D:\project\douyin-downloader"))


def load_cookies() -> dict[str, str]:
    """借用下载器已登录的 cookie（纯读取，不修改）。"""
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


def share_payload(aweme_id: str, client: httpx.Client) -> dict[str, Any] | None:
    """拉分享页（iPhone UA），取出 video_id 与作品文案。"""
    try:
        resp = client.get(SHARE_URL.format(aweme_id=aweme_id))
    except Exception:
        logger.exception("拉取分享页失败：%s", aweme_id)
        return None
    if resp.status_code != 200:
        logger.warning("分享页返回 %s：%s", resp.status_code, aweme_id)
        return None
    html = resp.text
    uri = re.search(r'"uri"\s*:\s*"(v0[0-9a-z]+)"', html)
    if not uri:
        logger.warning("分享页里没有 video_id（可能被风控壳页替换）：%s", aweme_id)
        return None
    desc = re.search(r'"desc"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    nickname = re.search(r'"nickname"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    tags = re.findall(r'"hashtag_name"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    return {
        "videoId": uri.group(1),
        "desc": _unescape(desc.group(1)) if desc else "",
        "nickname": _unescape(nickname.group(1)) if nickname else "",
        "tags": [_unescape(t) for t in tags][:20],
    }


def _unescape(text: str) -> str:
    try:
        return text.encode("utf-8").decode("unicode_escape").encode("latin1").decode("utf-8")
    except Exception:
        return text


def play_url(video_id: str, client: httpx.Client) -> tuple[str, str] | None:
    """用 video_id 换 CDN 直链，返回 (直链, 清晰度)。"""
    for ratio in RATIOS:
        try:
            resp = client.get(
                PLAY_URL,
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
) -> tuple[Path, dict[str, Any]] | None:
    """直连兜底下载：返回 (落盘文件, 元信息)。失败返回 None。

    元信息形如 `{"awemeId","desc","tags","nickname","videoId","ratio"}`，
    与下载器 `_manifest_metadata` 的结构保持兼容，便于直接塞进条目。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    cookies = load_cookies()
    headers = {"User-Agent": IPHONE_UA, "Referer": "https://www.douyin.com/",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    with httpx.Client(headers=headers, cookies=cookies, timeout=40) as client:
        aweme_id = resolve_aweme_id(url, client)
        if not aweme_id:
            logger.warning("解析作品号失败：%s", url)
            return None
        payload = share_payload(aweme_id, client)
        if not payload:
            return None
        resolved = play_url(payload["videoId"], client)
        if not resolved:
            logger.warning("拿不到播放直链：%s", aweme_id)
            return None
        cdn, ratio = resolved

        first_line = (payload["desc"] or "").splitlines()[0].strip()
        safe = re.sub(r'[\\/:*?"<>|\s#]+', " ", first_line)[:60].strip() or "douyin"
        dest = dest_dir / f"{safe}_{aweme_id}.mp4"

        with client.stream("GET", cdn, follow_redirects=True) as resp:
            if resp.status_code not in (200, 206):
                logger.warning("下载直链失败 %s：%s", resp.status_code, aweme_id)
                return None
            total = int(resp.headers.get("content-length") or 0)
            written = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(256 * 1024):
                    fh.write(chunk)
                    written += len(chunk)
                    if on_progress:
                        on_progress(written, total)
        if not dest.is_file() or dest.stat().st_size < 10_000:
            logger.warning("下载文件过小或不存在：%s", dest)
            return None

        return dest, {
            "awemeId": aweme_id,
            "desc": payload["desc"],
            "tags": payload["tags"],
            "nickname": payload["nickname"],
            "videoId": payload["videoId"],
            "ratio": ratio,
            "source": "direct",
        }
