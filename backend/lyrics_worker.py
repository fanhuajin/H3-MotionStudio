"""官方歌词抓取（QQ音乐），供 `/subtitle` 字幕链路复用。

原 `/lyrics` 歌词字幕路由已于 2026-09 随「这两个不要了」一并移除；这里只保留**两条
链路共用**的部分：语种粗判 + 搜歌/按歌词搜歌/取词（含 LRC 解析与制作人员行过滤）。
`subtitle_step` 用它们按歌名或歌词反查官方歌词，只核对文字、不动时间轴。

**2026-09-22 从网易云换成 QQ音乐**（用户：「不要用她的了 经常限流 直接网上搜索 多的是」）：
网易云那套 (`music.163.com/api`) 连发十几个请求就开始**返回空**（不是报错），限流能持续
几十分钟 —— 我当天做诊断时被限流两次，`official_lyrics_for` 于是静默退化成「未定位到官方
歌词」，用户看到的就是「歌词有的不对」。QQ音乐这套接口实测：**按歌名搜、按歌词搜、取词
三个都通**，而且按歌词搜的命中率更高（标题「看着你的笑我好像就足够」一搜 top1 就是
《碎碎念》-队长），也没有出现限流。⚠️ 三个接口都要带 `Referer: https://y.qq.com/`，
取词那个还必须 `nobase64=1` 才是明文 LRC。
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .pipeline import PipelineError

_QQ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://y.qq.com/",
}
_QQ_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
_QQ_LYRIC = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"

# LRC 里混入的制作人员/版权行，不是歌词正文，抓取时直接滤掉，避免被当成歌词行对齐/烧录。
# QQ 的写法很花：「词：罗健明」是单字 + 冒号，而「词Lyricist：兰湘怡」「编曲Arranger：黄振铎」
# 是**中文角色词 + 英文角色词 + 冒号** —— 2026-09-22 就是漏了后一种，006《说书》的歌词前 8 行
# 全是这种，识别行被对到「说书-一只白羊」这个标题行上（相似度 0.40），重合度被拖到 66%、
# 整首落进"不够可信"档 → 一句都没校对。所以：**中文角色词 + 可选的英文角色词 + 冒号**。
_ROLE_WORD = (
    r"(?:作词|作曲|编曲|词曲|词|曲|制作人|和声|录音|混音|母带|监制|吉他|贝斯|键盘|钢琴|"
    r"鼓手|架子鼓|鼓|弦乐|小提琴|中提琴|大提琴|低音提琴|配唱|原唱|翻唱|发行|出品|"
    r"缩混|混编|录音师|录音室|统筹|企划|出品人|监唱|人声|OP|SP|by|"
    r"Lyricist|Composer|Arranger|Producer|Mixing|Mastering|Vocal|Guitar|Piano|Drum)"
)
_METADATA_LINE = re.compile(
    # 「词：罗健明」「词Lyricist：今燃」「缩混/母带Mixing Engineer/Mastering Engineer：侯天宇」
    # —— 角色词可能串好几个（用 / 连），后面还跟英文角色词，最后才是冒号
    rf"^\s*(?:{_ROLE_WORD}\s*[/、·]?\s*)+(?:[A-Za-z][A-Za-z\s/&.]*)?\s*[:：]",
    re.IGNORECASE,
)
# 版权声明行（「【未经著作权人许可 不得翻唱 翻录或使用】」）
_NOTICE_LINE = re.compile(r"^\s*[【\[（(]?\s*(?:未经|版权|本歌曲|此歌曲|声明)")
# QQ 常把「歌名-歌手」当第一句歌词塞进来（如「说书-一只白羊」「川普来了也得等音乐(Remix)-青小葵」）
_TITLE_LINE = re.compile(r"^[^，。！？；、]{1,18}[-–—][^，。！？；、]{1,18}\(?.*\)?$")
_TITLE_LINE_SCAN = 3          # 只在**最前面几行**里认「歌名-歌手」，免得误杀正文里的破折号


def _parse_lrc(raw: str) -> list[tuple[float, str]]:
    lines: list[tuple[float, str]] = []
    for line in raw.splitlines():
        m = re.match(r"\s*\[(\d+):(\d+(?:\.\d+)?)\](.*)", line)
        if not m:
            continue
        text = m.group(3).strip()
        if not text:
            continue
        if _METADATA_LINE.match(text) or _NOTICE_LINE.match(text):
            continue
        if len(lines) < _TITLE_LINE_SCAN and _TITLE_LINE.match(text):
            continue
        t = int(m.group(1)) * 60 + float(m.group(2))
        lines.append((t, text))
    return lines

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
# QQ音乐 歌词抓取
# ---------------------------------------------------------------------------


def _song_entry(song: dict[str, Any]) -> dict[str, Any]:
    """把 QQ 搜索返回的一条整理成统一结构（`id` 就是 songmid —— 取歌词要用它）。"""
    singers = [str(x.get("name") or "") for x in (song.get("singer") or [])]
    return {
        "id": str(song.get("songmid") or ""),
        "name": str(song.get("songname") or ""),
        "artist": "/".join(name for name in singers if name),
        "album": str(song.get("albumname") or ""),
        "interval": song.get("interval"),
    }


async def _qq_search_raw(query: str, *, limit: int, by_lyric: bool) -> list[dict[str, Any]]:
    """打一次 `client_search_cp`：`t=0` 搜歌名、`t=7` 搜歌词。"""
    params = {"w": query, "format": "json", "n": max(1, int(limit)), "p": 1, "cr": 1, "t": 7 if by_lyric else 0}
    if by_lyric:
        params["remoteplace"] = "txt.yqq.lyric"
    try:
        async with httpx.AsyncClient(timeout=15, headers=_QQ_HEADERS) as client:
            response = await client.get(_QQ_SEARCH, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise PipelineError("歌词搜索失败", repr(error)) from error
    data = payload.get("data") or {}
    bucket = (data.get("lyric") if by_lyric else data.get("song")) or {}
    songs = bucket.get("list") or []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for song in songs:
        entry = _song_entry(song)
        if not entry["id"] or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        if by_lyric:
            entry["content"] = str(song.get("content") or "").replace("<em>", "").replace("</em>", "")
        out.append(entry)
    return out


async def search_songs(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """按**歌名**搜歌（`t=0`）。返回 `[{id(songmid), name, artist, album}]`。"""
    return await _qq_search_raw(query, limit=limit, by_lyric=False)


async def search_songs_by_lyric(line: str, limit: int = 6) -> list[dict[str, Any]]:
    """按**歌词**搜歌（`t=7` + `remoteplace=txt.yqq.lyric`）—— 歌名缺失/是副歌那句时的主力。

    实测（2026-09-22）：标题「看着你的笑我好像就足够」→ top1《碎碎念》-队长；
    「老夫子带着假发」→ 《你若成风》-许嵩。返回里还带一段 `content`（命中的歌词片段）。
    """
    return await _qq_search_raw(line, limit=limit, by_lyric=True)


# 取过的歌词缓存（同一进程内）：① 同一条视频里"候选富集"与"打分"会用同一个 songmid；
# ② 批量里同一首歌只会出现一次，但重烧/续跑会再遇到。也少给对端添压力。
_LYRIC_CACHE: dict[str, dict[str, Any]] = {}
# 有歌词的候选缓存（给 `search_songs_with_lyrics` 用，避免同一首歌反复取词）
_HAVE_LYRIC_CACHE: dict[str, bool] = {}


async def song_lyric(song_mid: str) -> dict[str, Any]:
    """取某首歌的完整歌词：`lines=[{time, orig, zh}]`、`lang`、`hasZh`（**带进程内缓存**）。

    `nobase64=1` 才返回明文 LRC（否则是 base64）；`trans` 是翻译（可空）。
    """
    key = str(song_mid)
    cached = _LYRIC_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=15, headers=_QQ_HEADERS) as client:
            response = await client.get(
                _QQ_LYRIC,
                params={"songmid": key, "format": "json", "nobase64": 1, "g_tk": 5381},
                headers={"Referer": "https://y.qq.com/portal/player.html"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise PipelineError("取歌词失败", repr(error)) from error
    orig_lines = _parse_lrc(str(payload.get("lyric") or ""))
    zh_lines = _parse_lrc(str(payload.get("trans") or ""))
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
    result = {
        "lang": detect_lang(body),
        "hasZh": zh_count > 0,
        "lines": lines,
    }
    _LYRIC_CACHE[key] = result
    _HAVE_LYRIC_CACHE[key] = True
    return result


async def search_songs_with_lyrics(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """按歌名搜 + 附上语种/行数/预览（取不到歌词的候选丢掉）。

    给需要"带歌词的候选列表"的调用方用（原来的 `netease_search` 就是这个语义）。
    取词走带缓存的 `song_lyric`，同一首歌全程只请求一次。
    """
    out: list[dict[str, Any]] = []
    for entry in await search_songs(query, limit=limit):
        key = str(entry["id"])
        if _HAVE_LYRIC_CACHE.get(key) is False:
            continue
        try:
            payload = await song_lyric(key)
        except Exception:  # noqa: BLE001 —— 取不到歌词的候选直接跳过
            _HAVE_LYRIC_CACHE[key] = False
            continue
        lines = [str(line.get("orig") or "") for line in (payload.get("lines") or [])]
        if not lines:
            _HAVE_LYRIC_CACHE[key] = False
            continue
        item = dict(entry)
        item.update({
            "lang": payload.get("lang"),
            "langLabel": lang_label(str(payload.get("lang") or "")),
            "hasZh": bool(payload.get("hasZh")),
            "lineCount": len(lines),
            "preview": lines[0][:36],
        })
        out.append(item)
    return out


