"""网易云歌词抓取（供 `/subtitle` 字幕链路复用）。

原 `/lyrics` 歌词字幕路由已于 2026-09 随「这两个不要了」一并移除；这里只保留**两条
链路共用**的部分：语种粗判 + 网易云搜歌/按歌词搜歌/取词（含 LRC 解析与制作人员行过滤）。
`subtitle_step` 用它们按歌名或歌词反查官方歌词，只核对文字、不动时间轴。
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .pipeline import PipelineError

_NETEASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://music.163.com/",
}
_NETEASE = "https://music.163.com/api"

# 网易云 LRC 常混入的制作人员/版权行（如「和声 : 萧贺硕」「OP : 华纳…」），
# 不是歌词正文，抓取时直接滤掉，避免被当成歌词行对齐/烧录出来。
_METADATA_PREFIX = (
    "作词", "作曲", "编曲", "词曲", "制作人", "和声", "录音", "混音", "母带",
    "监制", "吉他", "贝斯", "键盘", "钢琴", "鼓手", "架子鼓", "弦乐",
    "小提琴", "中提琴", "大提琴", "低音提琴", "配唱", "原唱", "翻唱",
    "发行", "出品", "OP", "SP", "by:",
)

# ---------------------------------------------------------------------------
# 语种识别（歌词正文 / whisper 结果都可用）
# ---------------------------------------------------------------------------

_HANGUL = re.compile(r"[\uac00-\ud7af]")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HAN = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_lang(text: str) -> str:
    """按字符占比粗判：ko / ja / zh / en / other（用于显示与排版决策）。"""
    counts = {
        "ko": len(_HANGUL.findall(text)),
        "ja": len(_KANA.findall(text)),
        "zh": len(_HAN.findall(text)),
        "en": len(_LATIN.findall(text)),
    }
    total = max(1, sum(counts.values()))
    best = max(counts, key=counts.get)
    if counts[best] / total < 0.15 and best != "other":
        return "other"
    return best


_LANG_LABEL = {"ko": "韩语", "ja": "日语", "zh": "中文", "en": "英语", "other": "其它"}


def lang_label(code: str) -> str:
    return _LANG_LABEL.get(code, code or "?")


# ---------------------------------------------------------------------------
# 网易云歌词抓取
# ---------------------------------------------------------------------------


def _parse_lrc(raw: str) -> list[tuple[float, str]]:
    lines = []
    for line in raw.splitlines():
        m = re.match(r"\s*\[(\d+):(\d+(?:\.\d+)?)\](.*)", line)
        if not m:
            continue
        text = m.group(3).strip()
        if not text:
            continue
        if text.startswith(_METADATA_PREFIX):
            continue
        t = int(m.group(1)) * 60 + float(m.group(2))
        lines.append((t, text))
    return lines


async def netease_search(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """搜索并附带歌词语种/翻译可用性信息（供前端挑选正确语种条目）。"""
    candidates: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=15, headers=_NETEASE_HEADERS) as client:
            response = await client.get(f"{_NETEASE}/search/get/web", params={"s": query, "type": 1, "limit": limit})
            response.raise_for_status()
            songs = ((response.json().get("result") or {}).get("songs")) or []
    except (httpx.HTTPError, ValueError) as error:
        raise PipelineError("歌词搜索失败", repr(error)) from error
    if not songs:
        return candidates

    async def enrich(song: dict[str, Any]) -> dict[str, Any] | None:
        song_id = song.get("id")
        if not song_id:
            return None
        item: dict[str, Any] = {
            "id": int(song_id),
            "name": song.get("name") or "",
            "artist": ((song.get("artists") or [{}])[0].get("name")) or "",
            "album": ((song.get("album") or {}).get("name")) or "",
        }
        try:
            async with httpx.AsyncClient(timeout=15, headers=_NETEASE_HEADERS) as client:
                lyric_response = await client.get(
                    f"{_NETEASE}/song/lyric", params={"id": song_id, "lv": 1, "kv": 1, "tv": -1}
                )
                payload = lyric_response.json()
            lrc_lines = _parse_lrc(((payload.get("lrc") or {}).get("lyric") or ""))
            zh_lines = _parse_lrc(((payload.get("tlyric") or {}).get("lyric") or ""))
        except (httpx.HTTPError, ValueError):
            return None
        if not lrc_lines:
            return None
        body = " ".join(text for _, text in lrc_lines[:12])
        lang = detect_lang(body)
        item["lang"] = lang
        item["langLabel"] = lang_label(lang)
        item["hasZh"] = len(zh_lines) > 0
        item["lineCount"] = len(lrc_lines)
        item["preview"] = lrc_lines[0][1][:36] if lrc_lines else ""
        return item

    for song in songs[:limit]:
        enriched = await enrich(song)
        if enriched:
            candidates.append(enriched)
    return candidates


async def netease_search_by_lyric(line: str, limit: int = 8) -> list[dict[str, Any]]:
    """**按歌词搜歌**（网易云 `type=1006`）—— 歌名缺失或写错时的兜底。

    用户 2026-09-16：「你可以根据歌词找吗」。实测有效：
      · 「夏天快要过去」→ 《有何不可》- 许嵩
      · 「是谁把牵挂埋在了土里」→ 《茶花开了…》系列（这条条目 `ai.song_name` 是空的）
    返回 [{id, name, artist}]（按接口顺序，可能含翻唱/说唱版；由调用方按歌词重合度挑）。
    """
    try:
        async with httpx.AsyncClient(timeout=15, headers=_NETEASE_HEADERS) as client:
            response = await client.get(f"{_NETEASE}/search/get/web",
                                        params={"s": line, "type": 1006, "limit": limit})
            response.raise_for_status()
            songs = ((response.json().get("result") or {}).get("songs")) or []
    except (httpx.HTTPError, ValueError) as error:
        raise PipelineError("按歌词搜索失败", repr(error)) from error
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for song in songs:
        sid = song.get("id")
        if not sid or int(sid) in seen:
            continue
        seen.add(int(sid))
        out.append({
            "id": int(sid),
            "name": song.get("name") or "",
            "artist": ((song.get("artists") or [{}])[0].get("name")) or "",
        })
    return out


async def netease_lyric(song_id: int) -> dict[str, Any]:
    """取某条目的完整歌词：lines=[{time, orig, zh}]、lang、hasZh。"""
    async with httpx.AsyncClient(timeout=15, headers=_NETEASE_HEADERS) as client:
        response = await client.get(f"{_NETEASE}/song/lyric", params={"id": song_id, "lv": 1, "kv": 1, "tv": -1})
        response.raise_for_status()
        payload = response.json()
    orig_lines = _parse_lrc(((payload.get("lrc") or {}).get("lyric") or ""))
    zh_lines = _parse_lrc(((payload.get("tlyric") or {}).get("lyric") or ""))
    if not orig_lines:
        raise PipelineError("该歌曲没有可用歌词文本")
    zh_by_time: dict[float, str] = {}
    for t, text in zh_lines:
        zh_by_time.setdefault(round(t, 2), text)
    body = " ".join(text for _, text in orig_lines[:12])
    lines: list[dict[str, Any]] = []
    zh_count = 0
    for t, text in orig_lines:
        zh = zh_by_time.get(round(t, 2), "")
        if zh:
            zh_count += 1
        lines.append({"time": round(t, 3), "orig": text, "zh": zh})
    return {
        "lang": detect_lang(body),
        "hasZh": zh_count > 0,
        "lines": lines,
    }

