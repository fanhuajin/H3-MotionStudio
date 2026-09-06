"""歌词字幕路由（多语言）：官方歌词抓取、人声实测时间对齐、字幕烧录。

数据流：
1) 网易云搜索/取词（lrc 原文 + tlyric 中文翻译），自动识别歌词语种；
2) scripts/lyrics_stage.py（RVC venv python）demucs 分离人声 +
   faster-whisper 自动语种识别，输出逐词时间戳 JSON；
3) 把每行歌词匹配到词级时间戳（difflib 文本相似 + 官方时间线性插值兜底）；
4) 用剪映手书（JYgangbi）烧录字幕，字号 ≈ 剪映字号 10（按画布高度换算）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

try:
    import zhconv
except ImportError:  # 可选依赖：未安装时繁体不转换，仅影响繁体歌词/识别文本的锚定率
    zhconv = None  # type: ignore[assignment]

from .settings import (
    DATA_DIR,
    JY_SHOU_SHU_FONT,
    LYRICS_ASR_MODEL,
    LYRICS_ASR_PY,
    PROJECT_ROOT,
)
from .store import lyrics_milestones, now_iso, store
from .pipeline import PipelineError, is_job_cancelled, pipeline_lock, raise_if_cancelled

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


# ---------------------------------------------------------------------------
# 时间对齐：whisper 词级时间戳 ↔ 歌词行
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    """归一化：小写、去标点；繁体先统一转简体（网易云歌词多为简体，
    而 whisper 中文输出常为繁体，不转换会让整行相似度跌破阈值）；
    中日韩去空格逐字比较，拉丁按词比较。"""
    if zhconv is not None and text:
        text = zhconv.convert(text, "zh-cn")
    text = text.lower()
    text = re.sub(r"[^\w\u3040-\u30ff\uac00-\ud7af\u4e00-\u9fff ]", "", text)
    if detect_lang(text) in ("zh", "ko", "ja"):
        return text.replace(" ", "")
    return " ".join(text.split())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# 对齐常量（只调这里，别散落在算法里）：
_ALIGN_MIN_SCORE = 0.50  # 文本窗口相似度门槛（命中才作候选）
_ALIGN_HEAD_TOL = 0.6  # 按整体偏移落到片头前 0.6s 内的行按 0 处理（半句起唱容差；
# 太大（旧值 3s）会把「紧挨着片段之前的一句」也闪出来）
_ALIGN_OFFSET_MAX_JITTER = 8.0  # 「官方时间 − 实测时间」抖动超过该值视为拼接/变速视频
_ALIGN_MERGE_GAP = 4.0  # 同一次演唱可能被识别分数波动切成几段窗口，合并间隔上限
_ALIGN_REPEAT_TEXT_GAP = 12.0  # 同一句正文两条字幕的最短间隔：短于它视为重复错配只留先出现者
_ALIGN_STRONG_SCORE = 0.72  # 锚点门槛：相似度 ≥ 此值，或整句被窗口完整包含（common=行长）
# 才配做锚点。演唱句在语音段里通常是「整句完整出现」（分数 ≈0.8~1.0），
# 而跨句杂凑窗口（如「会…我」「就…让…」「我…走」隔字命中）分数只有 ~0.5-0.67，作废不投。


def _pick_occurrences(
    target: str, tokens: list[tuple[str, float, float, int]]
) -> list[tuple[float, float, int]]:
    """在全部识别词里找 target 的高分演唱窗口。

    不依赖行号顺序：每行歌词的正文可能在视频的任何位置（源视频常常是
    歌曲中段的剪辑/拼接）。返回 [(出现起点秒, 最高相似度, 公共子序列长度)]，
    同一句文本多次出现会得到多个窗口（供副歌等重复歌词按轮次分配）。

    窗口被限制在单个 whisper 语音段内、且不允许跨越 >1s 的词间隙，否则
    会从相邻句子尾部捡到零散同字（的/心/难…）拼出假高分窗口；短行还要
    求公共子序列长度达标（两字行只要撞上一个常用字就有 0.5 分，必须挡掉）。

    起点选择：对同一窗口起点取相似度最高（同分取最短）的窗口，保证起点
    贴住演唱真正开始的词——否则前一短语中间的某个词一路扩展到整句也能
    得满分，出现起点会被整体前移（如把「我也不懂」锚到前一句的「眼」字）。
    """
    if not target:
        return []
    zh_style = " " not in target  # 中日韩逐字比较；拉丁按词比较
    tlen = len(target)
    cap = min(30, max(tlen + 4, 8))  # 窗口 token 数上限
    min_len = max(1, tlen // 3)  # 短于该长度的窗口相似度不可能 ≥ 门槛，直接跳过
    min_common = 2 if tlen <= 3 else max(2, int(round(tlen * 0.4)))  # 公共子序列下限
    occurrences: list[tuple[float, float, int]] = []  # (起点, 分数, 公共子序列长度)
    token_count = len(tokens)
    for j in range(token_count):
        best_score = 0.0
        best_common = 0
        best_win_len = 1 << 30
        best_start = tokens[j][1]
        window = ""
        seg_of_j = tokens[j][3]
        for k in range(j, min(j + cap, token_count)):
            if tokens[k][3] != seg_of_j:
                break  # 不跨语音段
            if k > j and tokens[k][1] - tokens[k - 1][2] > 1.0:
                break  # 词间大空隙 = 句子边界
            piece = tokens[k][0]
            if not piece:
                continue
            if zh_style:
                window += piece
            else:
                window = (" " if window else "") + piece
            if len(window) < min_len:
                continue
            matcher = SequenceMatcher(None, target, window)
            score = matcher.ratio()
            common = sum(block.size for block in matcher.get_matching_blocks())
            if score >= _ALIGN_MIN_SCORE and common >= min_common:
                win_len = k - j + 1
                if score > best_score or (score == best_score and win_len < best_win_len):
                    best_score = score
                    best_common = common
                    best_win_len = win_len
                    best_start = tokens[j][1]
        if best_score >= _ALIGN_MIN_SCORE:
            occurrences.append((best_start, best_score, best_common))
    # 相邻候选（同一句唱词被多个起点覆盖）合并成一次「出现」：取最高分那次
    # 自己的起点与公共子序列（分数相同保留先出现者）
    occurrences.sort(key=lambda item: (item[0], -item[1]))
    merged: list[tuple[float, float, int]] = []
    for start, score, common in occurrences:
        if merged and start - merged[-1][0] <= _ALIGN_MERGE_GAP:
            if score > merged[-1][1] or (score == merged[-1][1] and common > merged[-1][2]):
                merged[-1] = (start, score, common)
        else:
            merged.append((start, score, common))
    return merged


def align_line_times(
    asr: dict[str, Any], lyric_lines: list[dict[str, Any]]
) -> tuple[list[float | None], int, float]:
    """把每行歌词锚到实测演唱时间（视频时间轴）。

    返回（每行起始秒，可为 None=该行不在演唱范围内、实测锚定行数、末词起点、
    锚定行下标集合）。
    源视频常是整首歌的中段剪辑/拼接（开口处不在 0:00），因此旧版「按行号
    顺序 + 单向游标 + 官方 ±3.2s 窗」会系统性失败。v2 策略：

    1) 自由文本锚定：每行（正文相同的行合并计算）在全部识别词里找相似度
       ≥0.5 的窗口；同一句文本多次出现会得到多个候选轮次；
    2) 重复歌词（副歌）分配轮次：多句正文相同、官方时间又都在的行，用
       「官方时间 − 实测时间」的整体偏移就近匹配到各次演唱（剪辑视频偏移
       恒定；抖动过大的拼接视频退回按行序分配）；
    3) 锚点之间的行插值：有官方时间按官方比例（演唱节奏不变），否则按行序；
    4) 首锚点之前的行按「锚点官方 − 本行官方」外推：推回片头说明唱了（半句
       起唱放宽到片头），推不回来即该行没被唱到 → 剔除；末锚点之后的行不
       猜测（文本没被识别确认就不显示），避免在演唱收尾处堆假字幕。
    """
    duration = float(asr.get("duration") or 0)
    tokens: list[tuple[str, float, float, int]] = []
    for seg_index, seg in enumerate(asr.get("segments") or []):
        words = seg.get("words") or []
        if words:
            for word, start, end in words:
                piece = _norm(str(word))
                if piece:
                    tokens.append((piece, float(start), float(end or start), seg_index))
        else:
            piece = _norm(str(seg.get("text") or ""))
            if piece:
                tokens.append((piece, float(seg.get("start") or 0), float(seg.get("end") or 0), seg_index))
    tokens.sort(key=lambda item: item[1])
    last_vocal = tokens[-1][1] if tokens else duration
    official = [float(line.get("time") or 0.0) for line in lyric_lines]
    texts = [_norm(str(line.get("orig") or "")) for line in lyric_lines]

    # ---- 1) 候选锚点（正文相同的行只算一次） ----
    occurrences_by_text: dict[str, list[tuple[float, float, int]]] = {}
    for target in set(texts):
        if target:
            occurrences_by_text[target] = _pick_occurrences(target, tokens)

    def _strong(occ: tuple[float, float, int], target: str) -> bool:
        """只有「整句被完整包含」或高相似度的窗口才可能是真实演唱（见常量注释）。"""
        return occ[1] >= _ALIGN_STRONG_SCORE or occ[2] >= len(target)

    # ---- 2) 锚点分配 ----
    # 正文唯一的行：直接锚到它分数最高的强出现位置
    unique_candidates: list[tuple[float, int, tuple[float, float, int]]] = []  # (score, line_idx, 出现)
    repeat_groups: dict[str, list[int]] = {}
    for index, target in enumerate(texts):
        if not target:
            continue
        if texts.count(target) > 1:
            repeat_groups.setdefault(target, []).append(index)
        else:
            occ = [o for o in (occurrences_by_text.get(target) or []) if _strong(o, target)]
            if occ:
                best = max(range(len(occ)), key=lambda r: occ[r][1])
                unique_candidates.append((occ[best][1], index, occ[best]))

    # 唯一文本锚点间的整体偏移（官方时间 − 实测时间）：剪辑视频的起始处不在
    # 0:00，但偏移恒定。个别假锚点会给出离谱偏移，取「最密集的偏移簇」。
    def _offset_consensus(deltas: list[float]) -> float | None:
        if not deltas:
            return None
        ordered = sorted(deltas)
        best_cluster: list[float] = []
        for index, base in enumerate(ordered):
            end = index
            while end + 1 < len(ordered) and ordered[end + 1] - base <= _ALIGN_OFFSET_MAX_JITTER:
                end += 1
            cluster = ordered[index : end + 1]
            if len(cluster) > len(best_cluster):
                best_cluster = cluster
        if not best_cluster:
            return None
        return best_cluster[len(best_cluster) // 2]

    unique_deltas = [
        official[index] - occ[0]
        for score, index, occ in unique_candidates
        if official[index] > 0
    ]
    offset = _offset_consensus(unique_deltas)

    anchors: list[tuple[int, float, float]] = []  # (line_idx, start, score)
    seen_lines: set[int] = set()

    def _accept(line_idx: int, start: float, score: float) -> None:
        if line_idx in seen_lines:
            return
        anchors.append((line_idx, start, score))
        seen_lines.add(line_idx)

    # 唯一文本：按分数从高到低接受（同一位置一般不会撞车）
    for score, index, occ in sorted(unique_candidates, key=lambda item: (-item[0], item[1])):
        _accept(index, occ[0], score)

    # 重复歌词（副歌等）轮次分配：正文相同、官方时间又都在的行，把各次演唱
    # 按「官方时间 − (实测 + 偏移)」就近分配给对应轮次；高分的出现先分。
    for target, members in repeat_groups.items():
        members.sort()
        occ = sorted(
            (o for o in (occurrences_by_text.get(target) or []) if _strong(o, target)),
            key=lambda item: item[0],
        )
        if not occ:
            continue
        remaining = list(members)
        if offset is not None and all(official[m] > 0 for m in members):
            for start, score, _common in sorted(occ, key=lambda item: -item[1]):
                if not remaining:
                    break
                best_m = min(remaining, key=lambda m: abs(official[m] - (start + offset)))
                remaining.remove(best_m)
                _accept(best_m, start, score)
        else:
            # 无偏移可用：按行序对应演唱轮次（完整版翻唱顺序即如此）
            for (start, score, _common), member in zip(occ, members):
                _accept(member, start, score)
    # 保证锚点不挤在同一个小窗口里（不同文本撞车时保留高分者）
    anchors.sort(key=lambda item: (item[1], -item[2]))
    cleaned: list[tuple[int, float]] = []
    for idx, start, score in anchors:
        if cleaned and start - cleaned[-1][1] < 0.35:
            continue
        cleaned.append((idx, start))
    anchors = cleaned
    matched = len(anchors)

    # ---- 3) 每行时间：锚点 / 官方整体偏移映射 / 锚点间兜底插值 ----
    anchor_by_index = dict(anchors)
    anchor_indices = sorted(anchor_by_index)

    # 锚点行的「官方时间 − 实测时间」整体偏移：所有未锚定但带官方时间的行
    # 直接按该偏移落到时间轴；落点 < 0 = 该行在视频开口之前（没唱到）→ 剔除，
    # 不再按行号硬挤。
    deltas = [official[i] - anchor_by_index[i] for i in anchor_indices if official[i] > 0]
    delta = _offset_consensus(deltas)

    times: list[float | None] = []
    for index in range(len(lyric_lines)):
        if index in anchor_by_index:
            times.append(anchor_by_index[index])
            continue
        if official[index] > 0 and delta is not None:
            t = official[index] - delta
            if -_ALIGN_HEAD_TOL <= t < 0:
                times.append(0.0)  # 半句起唱：从片头显示
            elif t >= 0:
                times.append(t)
            else:
                times.append(None)
            continue
        # 无官方时间 / 无整体偏移：只在两侧锚点之间按比例兜底，锚点外不猜
        prev = [a for a in anchor_indices if a < index]
        nxt = [a for a in anchor_indices if a > index]
        if prev and nxt:
            ia, ib = prev[-1], nxt[0]
            ta, tb = anchor_by_index[ia], anchor_by_index[ib]
            if tb - ta >= 0.6:
                oa, ob, oi = official[ia], official[ib], official[index]
                if ob > oa >= 0 and 0 < oi < ob:
                    ratio = max(0.0, min(1.0, (oi - oa) / (ob - oa)))
                else:
                    ratio = (index - ia) / (ib - ia)
                times.append(max(ta + 0.12, min(tb - 0.12, ta + ratio * (tb - ta))))
            elif tb > ta:
                times.append(ta + (tb - ta) * (index - ia) / (ib - ia))
            else:
                times.append(None)
        else:
            times.append(None)

    # 完全无锚点：退化为等距铺开（尽力而为，调用方会给出提示）
    if not anchors:
        if duration > 0:
            span = max(0.0, duration - 0.8)
            times = [max(0.0, 0.25 + span * i / max(1, len(times))) for i in range(len(times))]
        else:
            times = [None] * len(times)
    return times, matched, last_vocal, set(anchor_by_index)


# ---------------------------------------------------------------------------
# 字幕烧录（剪映手书风格 · 字号≈剪映字号10）
# ---------------------------------------------------------------------------


def build_ass(
    width: int,
    height: int,
    cues: list[dict[str, Any]],
    duration: float,
) -> str:
    """cues: [{start, end, orig, zh}]（zh 空则单行）。排版：双语时
    翻译行在下、原词行在上（惯例）；画布高 H 时 字号≈88*H/1080。"""
    font_size = max(18, round(88 * height / 1080))
    bottom_mv = max(14, round(height * 0.093) - round(font_size * 0.205))
    top_mv = max(bottom_mv + round(font_size * 1.05), bottom_mv + round(height * 0.045))

    def ts(t: float) -> str:
        cs = int(round(t * 100))
        h, rem = divmod(cs, 360000)
        m, rem = divmod(rem, 6000)
        s, c = divmod(rem, 100)
        return f"{h}:{m:02d}:{s:02d}.{c:02d}"

    def style(name: str, mv: int) -> str:
        return (
            f"Style: {name},JYgangbi,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
            f"0,0,0,0,100,100,0,0,1,1,0,2,40,40,{mv},1"
        )

    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + style("LOWER", bottom_mv)
        + "\n"
        + style("UPPER", top_mv)
        + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []
    for cue in cues:
        start = max(0.0, float(cue["start"]))
        end = min(duration - 0.05, max(start + 0.8, float(cue["end"])))
        s, e = ts(start), ts(end)
        orig = cue.get("orig") or ""
        zh = cue.get("zh") or ""
        if zh and orig:
            # 底部事件先写（占据下方），原词事件后写（自动叠在上方）
            events.append(f"Dialogue: 0,{s},{e},LOWER,,0,0,0,,{zh}")
            events.append(f"Dialogue: 0,{s},{e},UPPER,,0,0,0,,{orig}")
        elif orig:
            events.append(f"Dialogue: 0,{s},{e},LOWER,,0,0,0,,{orig}")
    return head + "\n".join(events) + "\n"


async def burn_subtitles(source: Path, out_path: Path, ass_text: str, font: Path) -> None:
    if not font.is_file():
        raise PipelineError("缺少剪映手书字体", f"预期文件：{font}\n在剪映里使用一次「剪映手书」后即会缓存到本机。")
    def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="lyr_burn_") as td:
            work = Path(td)
            shutil.copy2(font, work / "JYgangbi.ttf")
            (work / "sub.ass").write_text(ass_text, encoding="utf-8")
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-i", str(source), "-vf", "subtitles=sub.ass:fontsdir=.",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
                    "-pix_fmt", "yuv420p", "-c:a", "copy",
                    "-movflags", "+faststart", str(out_path),
                ],
                capture_output=True,
                text=True,
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                cwd=str(work),
                timeout=30 * 60,
            )
            if result.returncode != 0:
                raise PipelineError("字幕烧录失败", (result.stderr or result.stdout)[-800:])
    await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# 任务执行器（kind="lyrics"，与其它任务共用 pipeline_lock 单任务互斥）
# ---------------------------------------------------------------------------


def _milestone_state(job_id: str, milestone_id: str) -> None:
    store.set_milestone(job_id, milestone_id, status="running")


async def _run_stage_script(
    job_id: str, video: Path, out_json: Path, prompt_text: str | None = None
) -> dict[str, Any]:
    """执行 RVC venv 阶段脚本；实时回传日志并推进里程碑。

    prompt_text：网易云官方歌词（含歌名/歌手）拼接文本，作为 initial_prompt
    注入 whisper，抑制演唱错字/幻觉、提高与歌词库文本的吻合度。
    """
    if not LYRICS_ASR_PY.is_file():
        raise PipelineError("语音识别环境不完整", f"缺少：{LYRICS_ASR_PY}")
    if not LYRICS_ASR_MODEL.is_dir():
        raise PipelineError(
            "缺少语音识别模型",
            f"预期模型目录：{LYRICS_ASR_MODEL}\n（faster-whisper base，可从 hf-mirror.com/Systran/faster-whisper-base 下载后解压使用）",
        )
    prompt_file: Path | None = None
    if prompt_text:
        prompt_file = out_json.parent / "asr_prompt.txt"
        prompt_file.write_text(prompt_text, encoding="utf-8")
    _milestone_state(job_id, "stems")
    store.add_log(job_id, "正在分离人声并识别（Demucs + faster-whisper，全程不占用 ComfyUI）……")
    command = [
        str(LYRICS_ASR_PY),
        str(PROJECT_ROOT / "scripts" / "lyrics_stage.py"),
        str(video),
        str(out_json),
        "--model",
        str(LYRICS_ASR_MODEL),
    ]
    if prompt_file is not None:
        command += ["--prompt-file", str(prompt_file)]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert process.stdout is not None
    lines: list[str] = []
    stage_marker: dict[str, str] = {"[2/4]": "stems", "[3/4]": "asr"}
    while True:
        raw = await process.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        lines.append(line)
        store.add_log(job_id, f"识别：{line}")
        for marker, milestone_id in stage_marker.items():
            if line.startswith(marker):
                _milestone_state(job_id, milestone_id)
    return_code = await process.wait()
    if return_code != 0:
        detail = "\n".join(lines[-60:])
        raise PipelineError("人声识别失败", detail or f"退出码 {return_code}")
    try:
        payload = json.loads(out_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PipelineError("识别结果缺失或不可读", str(out_json)) from None
    for milestone_id in ("stems", "asr"):
        store.set_milestone(job_id, milestone_id, status="completed", progress=100, currentNode=None)
    return payload


async def run_lyrics_job(job_id: str) -> None:
    async with pipeline_lock:
        state = store.get(job_id)
        if not state:
            return
        if is_job_cancelled(job_id):
            from .pipeline import finish_cancelled
            await finish_cancelled(job_id)
            return
        store.update(
            job_id,
            status="running",
            stage="starting",
            errorSummary=None,
            errorDetail=None,
            startedAt=now_iso(),
            finishedAt=None,
        )
        try:
            from .pipeline import media_metadata

            source = Path(state["lyricsSource"])
            if not source.is_file():
                raise PipelineError("找不到源视频", str(source))
            meta = await media_metadata(source)
            lines = state.get("lyricLines") or []
            if not lines:
                raise PipelineError("没有歌词行", "请先搜索并确认歌词文本")
            store.update(job_id, stage="lyrics")
            job_dir = Path(state["lyricsDir"])
            job_dir.mkdir(parents=True, exist_ok=True)

            store.set_milestone(job_id, "read", status="running", currentNode="读取音轨", progress=40)
            asr_json = job_dir / "asr.json"
            song_name = str(state.get("songName") or "")
            prompt_text = " ".join(
                str(line.get("orig") or "").strip()
                for line in lines
                if str(line.get("orig") or "").strip()
            )
            prompt_text = (f"《{song_name}》歌词：" if song_name else "歌词：") + prompt_text
            # initial_prompt 只作用于首个解码窗口，截断到约 900 字符即可
            payload = await _run_stage_script(job_id, source, asr_json, prompt_text[:900] or None)
            raise_if_cancelled(job_id)
            store.set_milestone(job_id, "read", status="completed", progress=100, currentNode=None)

            lang = str(payload.get("language") or "?")
            store.update(job_id, lyricAsrLang=lang)
            store.add_log(
                job_id,
                f"识别语种：{lang}（概率 {payload.get('language_probability', 0)}）· 音频 {payload.get('duration')}s · {len(payload.get('segments') or [])} 个语音段",
            )

            _milestone_state(job_id, "align")
            store.add_log(job_id, "正在把歌词逐句匹配到实测时间……")
            times, matched, last_vocal, anchored_idx = await asyncio.to_thread(align_line_times, payload, lines)
            if matched == 0:
                store.add_log(job_id, "警告：识别文本与歌词匹配度过低，字幕时间只能按等距预估（建议试听校对后重跑）。")
            store.add_log(job_id, f"对齐完成：{len(times)} 行（实测锚定 {matched} 行，其余按锚点插值/外推）。")
            raise_if_cancelled(job_id)

            # 组装 cue：丢弃越界行与「识别不到演唱」的尾部行；结束时间取下一句起点。
            # 注意：片段视频的官方歌词顺序 ≠ 实际演唱顺序（时间可能回退），
            # 因此先全量收集、再按实际时间排序后做 |Δ|<0.45s 的重复时刻去重，
            # 不能在按行序遍历时用单向游标（会把时间早于上一行的真唱行全删掉）。
            duration = float(payload.get("duration") or meta.get("duration") or 0)
            anchor_start_times = [
                start for index, start in enumerate(times)
                if index in anchored_idx and start is not None
            ]
            candidates: list[tuple[int, dict[str, Any], float]] = []
            for index, (line, start) in enumerate(zip(lines, times)):
                if start is None or start < -0.05 or start >= duration - 0.2:
                    continue
                if start > last_vocal + 1.0:
                    continue  # 该行在音频里没有被唱到（识别词已结束）
                if index not in anchored_idx and anchor_start_times:
                    # 未锚定行的时间是「官方时间 − 整体偏移」的映射值：只有当锚点存在时，
                    # 落在「锚点区间内」的映射才有意义（官方时间相邻的行在片段里连续演唱，
                    # 映射即真实位置；区间外说明该行属于片段前/后的段落，没被唱到）。
                    # 片头容差按 0.5s 起算，避免「紧挨片段之前的一句」在 0:00 闪出假字幕。
                    if start < 0.5 or start > max(anchor_start_times) + 1.0:
                        continue
                candidates.append((index, line, start))
            candidates.sort(key=lambda item: item[2])
            kept: list[tuple[int, dict[str, Any], float]] = []
            kept_texts: dict[str, float] = {}
            for index, line, start in candidates:
                if kept and abs(start - kept[-1][2]) < 0.45:
                    continue  # 同刻重复（尾部截断/重复句错配）只留最先出现的一条
                text_key = _norm(str(line.get("orig") or ""))
                if text_key and kept_texts.get(text_key, -1e9) > start - _ALIGN_REPEAT_TEXT_GAP:
                    continue  # 同一句正文在过短间隔内重复出现 = 重复句错配，只留先出现者
                kept.append((index, line, start))
                kept_texts[text_key] = start
            skipped = len(lines) - len(kept)
            if skipped:
                store.add_log(job_id, f"剔除视频中未唱到的歌词 {skipped} 行（保留 {len(kept)} 行，字幕只跟随实际演唱出现）。")
            cues = [
                {
                    "start": start,
                    "end": duration - 0.05,
                    "orig": str(line.get("orig") or ""),
                    "zh": str(line.get("zh") or ""),
                }
                for _index, line, start in kept
            ]
            for index, cue in enumerate(cues):
                start = cue["start"]
                if index + 1 < len(cues):
                    next_start = cues[index + 1]["start"]
                    cue["end"] = min(next_start - 0.05, start + 8.0)
                    if cue["end"] < start + 0.8:
                        cue["end"] = min(start + 0.8, next_start - 0.05)
                else:
                    cue["end"] = min(duration - 0.1, start + 8.0)

            width = int(meta.get("width") or 1440)
            height = int(meta.get("height") or 1080)
            ass_text = await asyncio.to_thread(build_ass, width, height, cues, duration)

            _milestone_state(job_id, "render")
            store.update(job_id, currentNodeTitle="剪映手书风格烧录")
            out_dir = Path(state["lyricsOutDir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            final = out_dir / f"{job_id}_歌词字幕.mp4"
            await burn_subtitles(source, final, ass_text, JY_SHOU_SHU_FONT)
            raise_if_cancelled(job_id)
            # 顺带保留 SRT（导入剪映精修用），只放任务目录、不与成片同目录：
            # 本地播放器会自动加载成片旁边的同名 srt，叠加出第二行字幕
            (job_dir / f"{job_id}_歌词字幕.srt").write_text(_cues_to_srt(cues), encoding="utf-8-sig")

            store.set_milestone(job_id, "align", status="completed", progress=100, currentNode=None)
            store.set_milestone(job_id, "render", status="completed", progress=100, currentNode=None)
            store.update(
                job_id,
                status="completed",
                stage="completed",
                finalOutput=str(final),
                finalReady=True,
                currentNodeId=None,
                currentNodeTitle=None,
                progress=100,
                output=await media_metadata(final),
                finishedAt=now_iso(),
            )
            store.add_log(job_id, f"歌词字幕成片已生成：{final.name}")
        except Exception as error:
            if is_job_cancelled(job_id):
                from .pipeline import finish_cancelled
                await finish_cancelled(job_id)
                return
            summary = error.summary if isinstance(error, PipelineError) else "歌词字幕任务失败"
            detail = error.detail if isinstance(error, PipelineError) else repr(error)
            store.add_log(job_id, f"错误：{summary}")
            failed = store.get(job_id) or {}
            running = next((m["id"] for m in failed.get("milestones", []) if m.get("status") == "running"), None)
            if running:
                store.set_milestone(job_id, running, status="error")
            store.update(job_id, status="failed", stage="failed", errorSummary=summary, errorDetail=detail, finishedAt=now_iso())


def _cues_to_srt(cues: list[dict[str, Any]]) -> str:
    def fmt(t: float) -> str:
        ms = int(round(t * 1000))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = []
    for index, cue in enumerate(cues, start=1):
        parts = [cue.get("orig") or ""]
        if cue.get("zh"):
            parts.append(cue.get("zh") or "")
        blocks.append(f"{index}\n{fmt(cue['start'])} --> {fmt(cue['end'])}\n" + "\n".join(parts) + "\n")
    return "\n".join(blocks)
