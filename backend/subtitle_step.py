"""backend.subtitle_step — 生成字幕：剪映识别时间轴 + 官方歌词核对 + 剪映手书烧录。

同一套链路有**两个入口**：

  · 批量唱歌条目出片后自动加字幕 —— `add_for_item(batch_id, item_id)`，产物写进发布目录的
    `最终成片_字幕版.mp4`（与无字幕版并存），失败只标黄、不阻断整批。
  · 独立「生成字幕」路由（`/subtitle`，`POST /api/jobs/subtitle`）—— `run_subtitle_job(job_id)`，
    输入是任意上传/选中的成片，进度与日志走任务系统，失败就是失败。

## 链路（2026-09-16 用户确认）

    最终成片.mp4（无字幕，H3 直出）
      ↓ ① `jianying_recognize.recognize_video()` —— 用剪映「识别歌词」拿时间轴
           （用户：「剪映的歌词识别 他的时间是最准的」；实测 26 秒出 11 句，
            时间轴与手工流程完全一致）
      ↓ ② `correct_with_official()` —— 用网易云官方歌词逐句核对**文字**
           （只改文字，时间轴一个字不动；实测唯一错句「果要是不那么开心」被修正）
      ↓ ③ `jianying_style.burn()` —— ffmpeg + libass 按剪映观感烧录
          （剪映手书 JYgangbi，字号/位置已按剪映导出标定）
    最终成片_字幕版.mp4（与无字幕版并存）

**只有①里"打开草稿"这一下用真实鼠标双击**（用户已授权，每条约 1 秒）——剪映首页草稿项是
QML 控件，投递消息与 UIA 模式调用都打不开。打开之后「文本/识别歌词/开始识别」全部靠投递
消息完成，不碰光标。

产物 `最终成片_字幕版.mp4` 与 `最终成片.mp4` **并存**：无字幕版是以后重新烧字幕的唯一输入，
且不同平台对字幕需求不同（用户 2026-09-16 确认）。

失败一律降级：只记日志 + 条目标黄，**不阻断整批**。
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import jianying_recognize, jianying_style, jianying_uia  # noqa: E402
from backend.batch_ai import PLACEHOLDER_VALUES  # noqa: E402
# **必须导入单例**（`from .batch_store import batch_store`，与 batch_worker / app.py 一致）：
# 写成 `from backend import batch_store` 拿到的是模块，`batch_store.get(...)` 会 AttributeError。
from backend.batch_store import batch_store  # noqa: E402
from backend.lyrics_worker import (  # noqa: E402
    search_songs as _search_songs,
    search_songs_by_lyric as _search_songs_by_lyric,
    song_lyric as _song_lyric,
)
from backend.pipeline import (  # noqa: E402
    PipelineError,
    finish_cancelled,
    is_job_cancelled,
    media_metadata,
    raise_if_cancelled,
)
from backend.settings import BATCH_OUTPUT_ROOT, publish_subdir  # noqa: E402
from backend.store import now_iso, store, subtitle_milestones  # noqa: E402

_SONG_IN_TITLE = re.compile(r"《([^》]{1,40})》")
# 占位符不能当歌名：复用 batch_ai 里那份（未识别/未知/无/暂无/none/null/n-a/-/—），
# 再补几个歌词场景里会出现的。踩过：`ai.song_name = "未识别"` 被当成真歌名去搜歌。
GENERIC_SONGS = set(PLACEHOLDER_VALUES) | {
    "", "无", "未知", "未知歌曲", "无歌曲", "未识别歌曲", "none", "null", "无题",
}
_MATCH_MIN_RATIO = 0.6          # 官方歌词定位到同一首歌后，允许的修正门槛
_STRICT_MATCH_RATIO = 0.85      # 只有显式调用才用的"只改几乎一致的句子"门槛（老口径，见下）
_MIN_LYRIC_COVERAGE = 0.6       # 候选至少要覆盖识别文本的 60% 才敢拿来校对（口径见 lyric_coverage）

# **按字符对齐**（2026-09-22）：剪映会把官方的"一句半"塞进一行（「想从头在和你但我左右在迟疑」
# = 官方「想从头再和你」+「但我左右在迟疑」，还错了一个字），逐行比相似度只会认后半句、
# 把前半句当"多听出来的字"丢掉 —— 用户看到的就是歌词少字。改成按字符对齐后由
# `assign_official_to_rows` 决定"这一行覆盖官方哪几行"。
_ALIGN_ROW_MIN = 0.3            # 这一行与官方原文的**字符命中率**下限（低于它不动这行）
_ALIGN_LEN_SLACK = 6            # 一行最多拼多长的官方原文（这一行字数 + 这个余量，防吞掉后面几句）
_DERIVATIVE_PENALTY = 0.1       # 候选排序时给 DJ/Remix/翻唱/氛围版这类派生版本扣的分（见 _candidate_score）
_DERIVATIVE_MARKERS = (
    "dj", "remix", "0.8x", "0.9x", "1.1x", "1.2x", "加速", "慢速", "伴奏", "纯享", "翻唱",
    "cover", "女声", "男声", "童声", "钢琴版", "吉他版", "氛围", "现场", "live", "抖音",
)
_THROTTLE_RETRY_WAIT = 2.0      # 搜索返回空时等这么久重试一次


# ---------------------------------------------------------------------------
# 歌名
# ---------------------------------------------------------------------------
def song_name_for(item: dict[str, Any]) -> str:
    """条目里找歌名：`ai.song_name` → 标题/简介里的《…》 → 源作品文案里的《…》。

    实测批量条目里 `ai.song_name` 有时是空的（如唱歌条目 4），所以必须有兜底。
    """
    ai = item.get("ai") or {}
    raw_song = str(ai.get("song_name") or "").strip()
    if raw_song and raw_song.lower() not in GENERIC_SONGS:
        match = _SONG_IN_TITLE.search(raw_song)
        return match.group(1).strip() if match else raw_song
    for candidate in (item.get("title"), ai.get("title"), ai.get("introduction")):
        match = _SONG_IN_TITLE.search(str(candidate or ""))
        if match:
            return match.group(1).strip()
    match = _SONG_IN_TITLE.search(str((item.get("sourceMetadata") or {}).get("desc") or ""))
    return match.group(1).strip() if match else ""


# ---------------------------------------------------------------------------
# 官方歌词核对（只改文字，不动时间轴）
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    """归一化：全角转半角、去空白与标点，便于比较（保留汉字本身）。"""
    text = unicodedata.normalize("NFKC", str(text or ""))
    return "".join(ch for ch in text if ch.isalnum())


def map_official_chars(recognized: list[str], official: list[str]
                       ) -> tuple[list[list[tuple[int, int, int]]], list[float]]:
    """把官方歌词**按字符**映射到识别行：返回 `(每行命中的官方片段, 每行的字符命中率)`。

    片段形如 `(官方行号, 该行内起始下标, 结束下标)`，全部来自 `SequenceMatcher` 的匹配块 ——
    也就是说**每个官方字最多只会归给一行识别文本**，天然不重复；而一行识别文本只取它真正
    对上的那几个字，也就不会把隔壁行的词抢过来。

    为什么不能"逐行找最像的一句"（2026-09-22 实测的坑）：
      · 剪映会把官方的**一句半**塞进一行（「想从头在和你但我左右在迟疑」= 官方「想从头再和你」
        +「但我左右在迟疑」，还错了一个字），逐行比相似度只认得出后半句，前半句被当"多听出来
        的字"丢掉 → 歌词少字；
      · 反过来，官方副歌会重复出现，同一句可能被"最像"口径重复选进两行 → 字幕重复。
    按字符映射两个毛病一起没有。

    命中率（命中字符数 / 这一行字数）用来决定怎么替换：够高就逐片段取官方那几个字（精确），
    偏低说明整句都听错了、但**位置**还对，这时用它命中的官方**整句**（006《说书》就是这种）。
    """
    official_norm = [_norm(line) for line in official]
    rec_all: list[str] = []
    rec_owner: list[int] = []
    for index, text in enumerate(recognized):
        norm = _norm(text)
        rec_all.append(norm)
        rec_owner.extend([index] * len(norm))
    off_all: list[str] = []
    off_owner: list[int] = []
    off_line_start: list[int] = []
    for index, norm in enumerate(official_norm):
        off_line_start.append(len(off_owner))
        off_all.append(norm)
        off_owner.extend([index] * len(norm))
    rec_str, off_str = "".join(rec_all), "".join(off_all)

    blocks: list[list[tuple[int, int, int]]] = [[] for _ in recognized]
    matched = [0] * len(recognized)
    if rec_str and off_str:
        matcher = difflib.SequenceMatcher(None, rec_str, off_str, autojunk=False)
        for block in matcher.get_matching_blocks():
            offset = 0
            while offset < block.size:
                rec_index = block.a + offset
                off_index = block.b + offset
                row = rec_owner[rec_index]
                line = off_owner[off_index]
                step = 1
                while (offset + step < block.size
                       and rec_owner[rec_index + step] == row
                       and off_owner[off_index + step] == line):
                    step += 1
                start = off_index - off_line_start[line]
                blocks[row].append((line, start, start + step))
                matched[row] += step
                offset += step

    segments: list[list[tuple[int, int, int]]] = []
    for row_blocks in blocks:
        merged: list[tuple[int, int, int]] = []
        for line, start, end in sorted(row_blocks):
            if merged and merged[-1][0] == line and start <= merged[-1][2]:
                merged[-1] = (line, merged[-1][1], max(end, merged[-1][2]))
            else:
                merged.append((line, start, end))
        segments.append(merged)
    rates = [matched[index] / max(1, len(rec_all[index])) for index in range(len(recognized))]
    return segments, rates


def _row_text(segments: list[tuple[int, int, int]], official: list[str], official_norm: list[str],
              previous_lines: set[int], next_lines: set[int]) -> str:
    """按命中的片段拼出这一行的正确歌词。

    **整行官方歌词只被这一行用到** → 直接用官方原文（保留它的空格与标点，且把识别听错的字
    一并改掉；「书上说先生有人腾云下鹤」→「书上说 山上有人腾云驾鹤」就是这条）。
    **某一官方行被上一行/下一行也用到**（剪映把一句拆成两段，或一行识别文本跨了官方两句）
    → 各取自己那一半，谁都不重复、也不丢。
    """
    parts: list[str] = []
    seen: set[str] = set()
    budget = sum(end - start for _line, start, end in segments) + _ALIGN_LEN_SLACK
    used = 0
    for line, start, end in segments:
        onorm = official_norm[line]
        if line not in previous_lines and line not in next_lines:
            piece = official[line]
        else:
            piece_start = start if line in previous_lines else 0
            piece_end = end if line in next_lines else len(onorm)
            piece = onorm[piece_start:piece_end]
        length = len(_norm(piece))
        # 官方副歌会重复出现，同一句可能被这一行匹配到好几次（实测「过往我保留」被拼了两遍）
        if not piece or _norm(piece) in seen:
            continue
        # 按顺序拼，总长不超过"这一行自己的字数 + 余量"：既不会把后面三句都吞进这条字幕
        # （006 最后一行实测会变成 40 多字），也不会因为一句只对上两三个字就把它丢掉。
        if parts and used + length > budget:
            break
        seen.add(_norm(piece))
        parts.append(piece)
        used += length
    return " ".join(parts)


def _touched_lines(segments: list[tuple[int, int, int]]) -> set[int]:
    return {line for line, _start, _end in segments}


def correct_with_official(rows: list[tuple[float, float, str]],
                          official: list[str],
                          min_ratio: float = _MATCH_MIN_RATIO
                          ) -> tuple[list[tuple[float, float, str]], list[str]]:
    """用官方歌词核对识别结果：**只改文字，时间轴一字不动**。

    规则（都是为了让"剪映断句"不被破坏，同时把错的字换成官方的）：
      · 识别文本与官方某行归一化后相同 → 保留
      · **剪映把官方一行拆成两段**（识别文本是官方原文的一部分）→ 保留，不许拼回去
      · 其余情况按**字符映射**（`map_official_chars`）拿到这行对上的官方原文并替换：
        整句听错、并句、多听出来的字，一次全部解决
      · 命中率低于 `_ALIGN_ROW_MIN` / 没有对上的行 → 保持原样并记一条 note
      · 来源不够可信（`min_ratio == _STRICT_MATCH_RATIO`，只由显式调用传入）时退回老口径：
        只有相似度 ≥ `min_ratio` 才改

    用户 2026-09-22 的口径：「**把错误的全部替换成正确的，只是歌词的替换，剪映得时间节点还是
    很准的不需要你管，你只替换正确的歌词**」—— 所以定位到同一首歌后**逐行改成官方原文**，
    时间轴始终不动。
    """
    if not official:
        return rows, ["没有官方歌词，跳过文字核对"]
    official_pair = [(_norm(o), o) for o in official if _norm(o)]
    official_norm = [norm for norm, _text in official_pair]
    official_text = [text for _norm_text, text in official_pair]

    trusted = min_ratio <= _MATCH_MIN_RATIO + 1e-9
    segments: list[list[tuple[int, int, int]]] | None = None
    rates: list[float] = []
    if trusted:
        segments, rates = map_official_chars([text for _a, _b, text in rows], official_text)
    touched = [_touched_lines(hits) for hits in segments] if segments is not None else []

    out: list[tuple[float, float, str]] = []
    notes: list[str] = []
    for index, (start, end, text) in enumerate(rows):
        norm = _norm(text)
        hits = segments[index] if segments is not None else []
        if hits and rates[index] >= _ALIGN_ROW_MIN:
            joined = _row_text(hits, official_text, official_norm,
                              touched[index - 1] if index else set(),
                              touched[index + 1] if index + 1 < len(rows) else set())
            joined_norm = _norm(joined)
            if joined_norm == norm:
                out.append((start, end, text))                 # 本来就是对的
                continue
            if norm and norm in joined_norm:
                out.append((start, end, text))                 # 剪映拆句：保留它的断句
                continue
            if joined_norm:
                out.append((start, end, joined))
                notes.append(f"{start:6.2f}s 「{text}」→「{joined}」"
                             f"（按官方歌词逐字对齐，字符命中 {rates[index]:.0%}）")
                continue

        # 没对上（或来源不够可信）：退回"最像的一行"口径
        best_ratio, best_official = 0.0, ""
        for onorm, otext in official_pair:
            if norm == onorm:
                best_ratio, best_official = 1.0, otext
                break
            if norm and onorm and (norm in onorm or onorm in norm):
                best_ratio, best_official = max(best_ratio, 0.95), otext
                continue
            ratio = difflib.SequenceMatcher(None, norm, onorm).ratio()
            if ratio > best_ratio:
                best_ratio, best_official = ratio, otext
        if norm == _norm(best_official) or best_ratio >= 0.95:
            out.append((start, end, text))              # 命中 / 剪映拆句
        elif best_ratio >= min_ratio:
            out.append((start, end, best_official))
            notes.append(f"{start:6.2f}s 「{text}」→「{best_official}」（相似度 {best_ratio:.2f}）")
        else:
            out.append((start, end, text))
            notes.append(f"{start:6.2f}s 「{text}」未达校对门槛（最高相似度 {best_ratio:.2f}"
                         f" < {min_ratio:.2f}），保留原样")
    return out, notes


async def official_lyric_texts(song: str, *, limit: int = 6) -> list[str]:
    """按**歌名**取官方歌词文本（候选打分选最匹配的一条）。"""
    candidates = await _search_songs(song, limit=limit)
    if not candidates:
        raise RuntimeError(f"没搜到《{song}》")

    def score(c: dict[str, Any]) -> float:
        return difflib.SequenceMatcher(None, song, str(c.get("name") or "")).ratio()

    best = max(candidates, key=score)
    payload = await _song_lyric(str(best["id"]))
    return [str(line.get("orig") or "").strip()
            for line in (payload.get("lines") or []) if str(line.get("orig") or "").strip()]


# ---------------------------------------------------------------------------
# 按歌词找歌（歌名缺失/写错时的兜底，2026-09-16 用户提议）
# ---------------------------------------------------------------------------
def pick_lyric_query(rows: list[tuple[float, float, str]], *, top: int = 2) -> list[str]:
    """从识别结果里挑**最有辨识度**的歌词行做搜索词：取最长、且彼此不重复的几行。

    短句（如「哦」「啊」）辨识度太低，统一丢掉；避免拿两行几乎一样的歌词重复搜。
    """
    seen: set[str] = set()
    picked: list[tuple[int, str]] = []
    for _start, _end, text in rows:
        norm = _norm(text)
        if len(norm) < 4 or norm in seen:
            continue
        seen.add(norm)
        picked.append((len(norm), str(text).strip()))
    picked.sort(reverse=True)
    return [text for _n, text in picked[:top]]


def lyric_coverage(recognized: list[str], official: list[str]) -> float:
    """识别文本与官方歌词**整段字符**重合度（0~1）——判断这个候选是不是同一首歌。

    2026-09-22 换的口径（原来是「逐行相似度 ≥0.75 的行数占比」）：剪映识别错几个字，
    整行就掉到 0.75 以下，5 句里错两句 → 0.6 → 被 0.7 门槛整首挡掉。实测今天的 8 条里
    有 5 条就是这样"一条都没校对"。整段字符口径按**字符**算（顺序对齐后能对上的字符占比），
    错几个字只扣那几个字，正确的歌稳稳在 0.8 以上，错误的歌（不同歌词）仍在 0.4 以下。
    """
    rec = _norm("".join(recognized))
    off = _norm("".join(official))
    if not rec or not off:
        return 0.0
    matcher = difflib.SequenceMatcher(None, rec, off, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(rec)


def _candidate_score(coverage: float, how: str) -> float:
    """候选排序用的分数：**先看重合度，派生版本扣一点分**。

    2026-09-22 实测的坑：004 的重合度最高的候选是《看着你的笑 我好像就足够 (氛围版)》，可它那份
    LRC 自己有错字（「一些烂理理由」「浓了好久才明白」），拿它去替反而把**本来识别对了**的句子
    改坏。所以有原版/干净版本时优先用原版：派生版本（DJ/Remix/翻唱/氛围版…）扣 `_DERIVATIVE_PENALTY`
    的分，重合度差得不多就让原版赢。
    """
    lowered = str(how or "").lower()
    penalty = _DERIVATIVE_PENALTY if any(m in lowered for m in _DERIVATIVE_MARKERS) else 0.0
    return coverage - penalty


async def official_lyrics_for(rows: list[tuple[float, float, str]], song: str
                              ) -> tuple[list[str], str, float]:
    """定位这条视频对应的官方歌词：**歌名 + 标题当歌词 + 识别歌词三路搜索，按重合度选**。

    返回 `(官方歌词行, 来源说明, 修正门槛 min_ratio)`；都没找到就返回 `([], 说明, 0)`。

    **三路都要走**（2026-09-22 实测的教训）：① 按**歌名**搜；② 把**标题当歌词**再搜一次
    （标题多半就是副歌那句 —— 004 的标题「看着你的笑我好像就足够」一搜 top1 就是
    《碎碎念》-队长，而当时歌名那一路一个候选都没有）；③ 拿**识别出来的歌词行**搜
    （剪映听错太多时会落空，如 006《说书》整句是编的，但仍值得一试）。
    三路的结果**按与识别文本的整段重合度排序**，谁像就用谁。

    歌词源是 **QQ音乐**（2026-09-22 换掉网易云：它连发十几个请求就返回空、一限就是几十分钟，
    链路会静默退化成"未定位到官方歌词"）。这三路 QQ 都支持，按歌词搜的命中率还更高。

    **门槛（2026-09-22 用户口径：「把错误的全部替换成正确的，只是歌词的替换」）**：只要最佳
    候选与识别文本重合 ≥ `_MIN_LYRIC_COVERAGE`（60%）就认定"是同一首歌"，返回**宽松门槛
    `_MATCH_MIN_RATIO`（0.6）** —— 也就是允许按 0.6 相似度改字、允许按顺序对齐改整句听错的
    行、允许并句拼接。**不再用 0.85 那档"只改几乎一致的句子"**：实测 006《说书》找到了**正确
    的那首**（《说书》-一只白羊），却因为重合只有 66% 落进严档、**一句都没改**，用户看到的
    正是"歌词有的不对"。`_STRICT_MATCH_RATIO` 只保留给显式传门槛的调用方（测试/其它入口）。

    存量数据（2026-09-22 当天 8 条）实测：正确候选 66%~100%、不相干的候选 ≤40%，
    60% 这条线有足够余量；`how` 里会写明来源与重合度，改完的每一句也都进日志，可复核。
    """
    recognized = [text for _a, _b, text in rows]
    candidates: list[tuple[str, str]] = []          # (songmid, 来源说明)
    notes: list[str] = []

    async def search(how: str, query: str, *, by_lyric: bool) -> list[dict[str, Any]]:
        """搜一次，**空结果再重试一次** —— 搜索接口出问题时的表现往往是"返回空"而不是报错。"""
        for attempt in range(2):
            try:
                found = (await _search_songs_by_lyric(query, limit=6) if by_lyric
                         else await _search_songs(query, limit=6))
            except Exception as error:               # noqa: BLE001
                notes.append(f"{how}搜索失败（{error}）")
                return []
            if found:
                return found
            if attempt == 0:
                await asyncio.sleep(_THROTTLE_RETRY_WAIT)
        return []

    if song:
        for c in await search(f"歌名《{song}》", song, by_lyric=False):
            candidates.append((str(c["id"]), f"歌名搜索《{c.get('name')}》- {c.get('artist')}"))
        # 标题/歌名**当歌词**再搜一次（歌名搜不到时的主力，实测最有效的一路）
        for c in await search(f"标题「{song}」", song, by_lyric=True):
            candidates.append((str(c["id"]),
                               f"歌词搜索（标题「{song}」）→《{c.get('name')}》- {c.get('artist')}"))

    for query in pick_lyric_query(rows):
        for c in await search(f"歌词「{query}」", query, by_lyric=True):
            candidates.append((str(c["id"]),
                               f"歌词搜索（识别行「{query}」）→《{c.get('name')}》- {c.get('artist')}"))

    best: tuple[float, list[str], str] = (0.0, [], "")
    best_score = -1.0
    seen_ids: set[str] = set()
    for song_id, how in candidates:
        if song_id in seen_ids:
            continue
        seen_ids.add(song_id)
        try:
            payload = await _song_lyric(song_id)
        except Exception:                            # noqa: BLE001
            continue
        lines = [str(line.get("orig") or "").strip()
                 for line in (payload.get("lines") or []) if str(line.get("orig") or "").strip()]
        if not lines:
            continue
        coverage = lyric_coverage(recognized, lines)
        score = _candidate_score(coverage, how)
        if score > best_score:
            best_score = score
            best = (coverage, lines, f"{how}（与识别文本重合 {coverage:.0%}）")

    if best[0] >= _MIN_LYRIC_COVERAGE:
        return best[1], best[2], _MATCH_MIN_RATIO
    if best[0] <= 0:
        return [], "；".join(notes) or "歌名 / 标题 / 歌词三路搜索都没找到官方歌词", 0.0
    return [], f"找到的候选与识别文本重合度太低（最高 {best[0]:.0%}），不用于校对", 0.0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
# 里程碑 id（与 store.subtitle_milestones() 一一对应）：独立「生成字幕」路由按这三格推进度。
STAGE_RECOGNIZE = "recognize"
STAGE_VERIFY = "verify"
STAGE_BURN = "burn"

# 阶段回调：(里程碑 id, 日志) → None。批量链路不传，独立路由用它写进度与日志。
StageHook = Callable[[str, str], None]


async def burn_for_video(video: Path, song: str, out: Path, *, on_stage: StageHook | None = None,
                         stats: dict[str, Any] | None = None,
                         on_done: StageHook | None = None) -> tuple[Path, list[str]]:
    """识别 → 定位官方歌词（歌名或歌词搜）→ 核对文字 → 烧录。返回 (输出路径, 备注)。

    **没有歌名也要做**：实测条目 #4《这首民谣》的 `ai.song_name` 是空的（标题里也没有《歌名》），
    这时就靠**用识别出的歌词去搜歌**（用户 2026-09-16 提议：「你可以根据歌词找吗」）；
    两路都没定位到才退回"直接用剪映识别结果"——识别文字本身质量就不错。

    `on_stage(milestone_id, log)` 是给独立「生成字幕」路由的阶段回调（批量链路不传）；
    `stats` 传入时会被填上这次识别的真实结果（句数 / 歌词来源 / 修正门槛），供页面展示。
    `on_done(milestone_id, log)` 在**最后一格烧录真正结束**后回调——没有它的话 burn 会一直挂在
    「运行中」（2026-09-16 实测：任务都 completed 了，第三格还是 running）。
    """
    def stage(milestone_id: str, message: str) -> None:
        if on_stage:
            on_stage(milestone_id, message)

    stage(STAGE_RECOGNIZE, "正在用剪映识别歌词（会打开剪映草稿，届时动一次鼠标）……")
    rows = await asyncio.to_thread(jianying_recognize.recognize_video, video)
    if not rows:
        raise RuntimeError("剪映识别歌词没有返回任何行")
    stage(STAGE_VERIFY, f"剪映识别出 {len(rows)} 句歌词，正在定位并核对官方歌词……")
    official, how, min_ratio = await official_lyrics_for(rows, song)
    if official:
        rows, notes = correct_with_official(rows, official, min_ratio=min_ratio)
        notes.insert(0, f"官方歌词来源：{how}；修正门槛 {min_ratio:.2f}")
    else:
        notes = [f"未定位到官方歌词（{how}），字幕文字与时间轴都直接用剪映识别结果"]
    if stats is not None:
        stats.update({
            "lines": len(rows),
            "lyricSource": how,
            "minRatio": min_ratio,
            "corrected": len([n for n in notes if "→" in n]),
        })
    stage(STAGE_BURN, f"正在按剪映手书风格烧录 {len(rows)} 句字幕……")
    path = await asyncio.to_thread(jianying_style.burn, video, out, rows)
    if on_done is not None:
        on_done(STAGE_BURN)          # 独立路由用它把最后一格收尾（批量不传，行为不变）
    return path, notes


async def add_for_item(batch_id: str, item_id: str, video: Path | None = None) -> Path | None:
    """批量条目入口：给这条**唱歌**视频生成 `最终成片_字幕版.mp4`（幂等、失败不阻断）。"""
    state = batch_store.get(batch_id) or {}
    item = next((i for i in (state.get("items") or []) if i.get("id") == item_id), None)
    if not item:
        return None
    if str(item.get("kind") or "") != "singing":
        return None                                  # 只给唱歌条目加字幕

    outputs = dict(item.get("outputs") or {})
    clean = Path(str(video or outputs.get("videoFinal") or ""))
    if not clean.is_file():
        batch_store.add_item_log(batch_id, item_id, "跳过加字幕：发布目录里没有无字幕成片。")
        return None
    song = song_name_for(item)
    out = clean.parent / "最终成片_字幕版.mp4"
    batch_store.add_item_log(
        batch_id, item_id,
        f"开始生成字幕版：{f'《{song}》' if song else '（条目里没有歌名，跳过官方歌词核对）'}"
        f"（剪映识别时间轴{' + 官方歌词核对' if song else ''}）")
    try:
        path, notes = await burn_for_video(clean, song, out)
    except Exception as error:                        # noqa: BLE001 —— 降级，不阻断整批
        batch_store.add_item_log(batch_id, item_id, f"字幕版生成失败（本条标黄，不影响出片）：{error}")
        batch_store.mutate_item(batch_id, item_id,
                                lambda row, msg=str(error): row.update(warning=f"字幕版未生成：{msg}"))
        return None

    for note in notes:
        batch_store.add_item_log(batch_id, item_id, f"文字核对：{note}")
    batch_store.mutate_item(
        batch_id, item_id,
        lambda row, p=str(path): row.update(outputs={**(row.get("outputs") or {}), "videoSub": p}))
    batch_store.add_item_log(
        batch_id, item_id,
        f"字幕版已生成：{path.name}（{path.stat().st_size / 1024 / 1024:.2f} MB，与无字幕版并存）")
    return path


# ---------------------------------------------------------------------------
# 独立「生成字幕」任务（kind="subtitle"，2026-09-16 新增路由）
# ---------------------------------------------------------------------------
def close_jianying_quietly() -> bool:
    """关掉剪映（没开着就立刻返回）。默认**不关**——用户 2026-09-16 明确要求
    「自动运行的时候剪映不要关掉」，所以改成由 `H3_CLOSE_JIANYING_AFTER=1` 显式开启。"""
    if os.getenv("H3_CLOSE_JIANYING_AFTER", "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    try:
        with jianying_uia.uia_in_thread():
            return jianying_recognize.close_app(timeout=20)
    except Exception:                                     # noqa: BLE001 —— 兜底清理，绝不抛
        return False


def deliver_subtitle_output(job_id: str, subtitle: Path, **kwargs) -> Path:
    """把字幕版放进对应的发布目录（实现见 `batch_worker.deliver_subtitle_output`）。

    这里只是**延迟导入**的转发：`batch_worker` 用到 `subtitle_step`（`_add_subtitles_if_singing`），
    两边都在模块级互相 import 会绕成环，所以这一头保持函数内导入。
    """
    from backend import batch_worker

    return batch_worker.deliver_subtitle_output(job_id, subtitle, **kwargs)


def _stage_hook(job_id: str) -> StageHook:
    """把 burn_for_video 的阶段回调翻译成任务进度：上一格收尾、这一格转运行中 + 写日志。

    顺带在**每个阶段边界**检查取消：剪映识别要跑几十秒，用户中途点了「取消」就别再往下烧字幕
    （异常由 `run_subtitle_job` 的 except 认成取消并走 `finish_cancelled`）。
    """
    def report(milestone_id: str, message: str) -> None:
        raise_if_cancelled(job_id)
        _complete_running_stage(job_id, keep=milestone_id)
        store.set_milestone(job_id, milestone_id, status="running", progress=None)
        if message:
            store.add_log(job_id, message)
    return report


def _done_hook(job_id: str) -> StageHook:
    """烧录真正结束后的收尾回调：把最后一格标成 completed（否则它会一直挂在运行中）。"""
    def report(milestone_id: str, _message: str = "") -> None:
        store.set_milestone(job_id, milestone_id, status="completed", progress=100)
    return report


def _complete_running_stage(job_id: str, *, keep: str | None = None) -> None:
    """把当前处于运行中的里程碑收尾（`keep` 指向的那一格除外）。"""
    state = store.get(job_id) or {}
    running = next(
        (row["id"] for row in (state.get("milestones") or []) if row.get("status") == "running"),
        None,
    )
    if running and running != keep:
        store.set_milestone(job_id, running, status="completed", progress=100)


# 「重新开始」请求标记：正在跑的那一轮收尾后，由它自己再排一轮（同一个任务、同一条源成片）。
# **不能在端点里「先重置再 spawn」**：旧任务的 except 分支会发现 status 已经不是 cancelling，
# 于是把这次识别失败当真失败写回去，甚至把新一轮正在跑的那一格标成 error。
RESTART_KEY = "restartRequested"
# 自动重排的任务强引用（不持引用会被 GC 掉）
_RESTART_TASKS: set[asyncio.Task] = set()


def request_restart(job_id: str) -> dict[str, Any]:
    """给正在跑的字幕任务打上「重新开始」标记（当前这一轮收尾后自动重排）。"""
    return store.mutate(job_id, lambda row: row.update({RESTART_KEY: True}))


def take_restart(job_id: str) -> bool:
    """取走「重新开始」标记（取到即清掉，避免重复重排）。"""
    if not (store.get(job_id) or {}).get(RESTART_KEY):
        return False
    store.mutate(job_id, lambda row: row.update({RESTART_KEY: False}))
    return True


def reset_subtitle_job(job_id: str) -> dict[str, Any]:
    """把一条字幕任务重置回排队：里程碑按模板重建、错误清空、日志留一条「重新开始」。

    源成片留在任务目录里（`data/subtitle/{job_id}/source*`），所以**不用重新上传/复制**。
    """
    def apply(row: dict[str, Any]) -> None:
        row["milestones"] = subtitle_milestones()
        row.update({
            "status": "queued",
            "stage": "upload",
            "errorSummary": None,
            "errorDetail": None,
            "progress": 0,
            "progressValue": None,
            "progressMax": None,
            "currentNodeId": None,
            "currentNodeTitle": "等待重新开始",
            "startedAt": None,
            "finishedAt": None,
            "finalReady": False,
            "finalOutput": None,
            "output": None,
            "subtitleLineCount": None,
            "subtitleLyricSource": None,
            "subtitleCorrected": None,
            "subtitlePublishPath": None,
            "subtitlePublishError": None,
            # 「重新开始」必须**清掉取消印记**，否则这一条会一路被当成已取消
            # （`is_job_cancelled` 认印记，见 backend/pipeline.py）。
            "cancelRequested": False,
            RESTART_KEY: False,
        })
        logs = list(row.get("logs") or [])
        logs.append({"time": now_iso(),
                     "message": "重新开始：沿用同一条源成片，重新走剪映识别 → 官方歌词核对 → 烧录。"})
        row["logs"] = logs[-300:]

    return store.mutate(job_id, apply)


def _spawn_restart(job_id: str) -> None:
    task = asyncio.create_task(run_subtitle_job(job_id))
    _RESTART_TASKS.add(task)
    task.add_done_callback(_RESTART_TASKS.discard)


async def run_subtitle_job(job_id: str) -> None:
    """独立「生成字幕」任务：剪映识别时间轴 → 官方歌词核对文字 → 剪映手书烧录。

    与批量条目那条链路**完全同一条**（`burn_for_video`），区别只在输入是用户上传/选中的
    任意成片、产物落在任务目录而不是发布目录，而且失败就是失败（批量里失败只标黄）。

    **不再等 `pipeline_lock`**（用户 2026-09-16：「不要锁这个啊 和生成字幕有什么关系 取消这个锁」）：
    这条链路只用剪映 + ffmpeg，不碰 ComfyUI、也不吃采样显存，把它排在出片/迁移任务后面
    纯属白等（实测被一单 66% 的迁移堵了半小时以上）。提交即跑；唯一仍然互斥的是
    「同时只允许一条字幕任务」——那由 `create_subtitle_job` 的 `store.latest("subtitle")` 守卫，
    而不是靠这把全局锁。
    """
    if is_job_cancelled(job_id):
        # 排队期间被取消：取消端点已经收尾过，这里不要再写一遍状态
        return
    await _run_subtitle_once(job_id)
    if take_restart(job_id):
        reset_subtitle_job(job_id)
        _spawn_restart(job_id)


async def _run_subtitle_once(job_id: str) -> None:
    state = store.get(job_id)
    if not state:
        return
    if is_job_cancelled(job_id):
        await finish_cancelled(job_id)
        return
    store.update(
        job_id,
        status="running",
        stage="subtitle",
        errorSummary=None,
        errorDetail=None,
        startedAt=now_iso(),
        finishedAt=None,
    )
    # 起跑前再确认一次「还该跑」：这一条可能已经被标成 interrupted（本地服务重启时由
    # `_interrupt_orphan_jobs()` 收尾，用户也可能已经点了「重新开始」另起一轮）。
    # 2026-09-16 实测踩过：一条僵尸 running 任务会一直占着 `store.active()`，把批量出片的
    # 等待循环无限期堵死（用户：「批量点击重新开始怎么没有效果」），所以这里宁可少跑也不许赖着。
    fresh = store.get(job_id) or {}
    if fresh.get("status") != "running" or fresh.get("errorSummary"):
        store.add_log(job_id, "这一条已经被收尾（中断 / 换了一轮），本次不再继续。")
        return
    try:
        source = Path(str(state.get("subtitleSource") or state.get("sourcePath") or ""))
        if not source.is_file():
            raise PipelineError("找不到要加字幕的成片", str(source))
        raw_out = str(state.get("subtitleOutPath") or "").strip()
        if not raw_out:
            raise PipelineError("任务缺少输出路径", "subtitleOutPath 为空")
        out = Path(raw_out)
        song = str(state.get("songName") or "").strip()
        store.add_log(
            job_id,
            f"开始生成字幕版：{f'《{song}》' if song else '（没有歌名，用识别出的歌词反查官方歌词）'}"
            f"（剪映识别时间轴 + 官方歌词核对文字）",
        )
        stats: dict[str, Any] = {}
        path, notes = await burn_for_video(
            source, song, out, on_stage=_stage_hook(job_id), stats=stats,
            on_done=_done_hook(job_id),
        )
        raise_if_cancelled(job_id)
        _complete_running_stage(job_id)               # 兜底：确认没有残留的运行中里程碑
        for note in notes:
            store.add_log(job_id, f"文字核对：{note}")
        store.update(
            job_id,
            finalOutput=str(path),
            finalReady=True,
            subtitleLineCount=stats.get("lines"),
            subtitleLyricSource=stats.get("lyricSource"),
            subtitleCorrected=stats.get("corrected"),
        )
        store.add_log(
            job_id,
            f"字幕版成片已生成：{path.name}"
            f"（{path.stat().st_size / 1024 / 1024:.2f} MB，共 {stats.get('lines') or 0} 句字幕）",
        )
        # 生成完成就放进「对应的发布目录」（用户 2026-09-16：「生成完成放到对应的发布目录」）：
        # 源成片来自某条批量作品 → 那一条自己的发布目录；本地上传 → 发布根下按 job 新建一个。
        # **拷不进去不算任务失败**：字幕版已经在任务目录里，页面照样能看能下载，只把原因写出来。
        published: Path | None = None
        publish_error: str | None = None
        try:
            published = await asyncio.to_thread(
                deliver_subtitle_output,
                job_id,
                path,
                source_job_id=str(state.get("sourceJobId") or ""),
                song=song,
                source_name=str(state.get("sourceName") or ""),
                source_video=source,
            )
        except Exception as error:                        # noqa: BLE001 —— 只降级，不失败
            publish_error = str(error)
            store.add_log(job_id, f"放进发布目录失败（字幕版仍在任务目录里，可直接下载）：{error}")
        else:
            store.add_log(job_id, f"已放进发布目录：{published}")
        store.update(
            job_id,
            subtitlePublishPath=str(published) if published else None,
            subtitlePublishError=publish_error,
        )
        store.update(
            job_id,
            status="completed",
            stage="completed",
            currentNodeId=None,
            currentNodeTitle=None,
            progress=100,
            output=await media_metadata(path),
            finishedAt=now_iso(),
        )
        store.add_log(job_id, "生成字幕任务已完成。")
    except Exception as error:                        # noqa: BLE001 —— 独立任务：如实报错
        if is_job_cancelled(job_id):
            await finish_cancelled(job_id)
            return
        if isinstance(error, PipelineError):
            summary, detail = error.summary, error.detail
        else:
            # 页面上的黄色横幅只显示 summary，所以把真实原因放进去（否则只有一句「生成字幕失败」，
            # 用户看不到是剪映超时还是草稿打不开 —— 2026-09-16 实测就是这样）
            text = str(error).strip() or error.__class__.__name__
            summary, detail = f"生成字幕失败：{text}", repr(error)
        store.add_log(job_id, f"错误：{summary}")
        failed = store.get(job_id) or {}
        running = next(
            (row["id"] for row in (failed.get("milestones") or []) if row.get("status") == "running"),
            None,
        )
        if running:
            store.set_milestone(job_id, running, status="error")
        store.update(
            job_id,
            status="failed",
            stage="failed",
            errorSummary=summary,
            errorDetail=detail,
            finishedAt=now_iso(),
        )
    finally:
        # 剪映不能留在内存里跟 ComfyUI 抢显存（用户 2026-09-16：「完成后需要 关闭 剪映」）：
        # 成功、失败、取消三条路都要关。recognize_video 的正常路径已经关了，
        # 这里只是兜底；从没打开过剪映时它会立刻返回。
        await asyncio.to_thread(close_jianying_quietly)


# ---------------------------------------------------------------------------
# 一键批量：给发布根里「所有缺字幕版的唱歌视频」逐条生成字幕
# ---------------------------------------------------------------------------
BATCH_MILESTONES: list[dict[str, Any]] = [
    {
        "id": "batch",
        "label": "批量生成字幕",
        "subtitle": "逐条剪映识别 → 官方歌词核对 → 烧录",
        "status": "pending",
    },
]


def _publish_song_from_folder(name: str) -> str:
    """从 `{编号}_唱歌_{歌名或标题}_{作品号}` 里抽出歌名/标题（作品号是末尾那串数字）。

    发布目录名是批量那套命名（`_ideal_publish_folder`），歌名或标题可能带下划线
    （英文歌名常有），所以只把「唱歌标签之后、末尾作品号之前」的内容原样拼回来。
    """
    parts = str(name or "").strip().split("_")
    if "唱歌" in parts:
        idx = parts.index("唱歌")
        rest = parts[idx + 1:]
        if rest and len(rest[-1]) >= 8 and rest[-1].isdigit():
            rest = rest[:-1]
        return "_".join(rest).strip()
    return ""


def _publish_folder_aweme_id(name: str) -> str:
    """发布目录名末尾的作品号（`…_{作品号}`）；认不出来返回空串。"""
    parts = str(name or "").strip().split("_")
    if parts and len(parts[-1]) >= 8 and parts[-1].isdigit():
        return parts[-1]
    return ""


def _subtitle_is_stale(subtitle: Path, source: Path) -> bool:
    """字幕版是不是**基于旧版本**烧的（音色版更新 → 这份字幕版过期，要重烧）。

    用户 2026-09-20 定的链条：`最终成片.mp4`（原声）→ `最终成片_音色版.mp4` → `最终成片_字幕版.mp4`。
    如果先烧了字幕、之后才转音色，目录里就会同时有「音色版 + 原声字幕版」——看起来完整，
    实际拿去发布的是旧音频。所以判据不能只看「有没有字幕版」，还要看它比输入**新不新**。
    容差 1 秒：复制文件会带出毫秒级误差。
    """
    try:
        return subtitle.stat().st_mtime < source.stat().st_mtime - 1
    except OSError:
        return False


def subtitle_source_for(folder: Path) -> Path | None:
    """这条作品该拿哪个文件烧字幕：**优先音色版**（新链条），没有才退回原声母版。"""
    voice = folder / "最终成片_音色版.mp4"
    if voice.is_file():
        return voice
    clean = folder / "最终成片.mp4"
    return clean if clean.is_file() else None


def scan_singing_publish_videos(*, root: Path | None = None) -> list[dict[str, Any]]:
    """扫发布根：返回**缺字幕版（或字幕版已过期）**的唱歌视频，每个目录一条。

    判定口径（2026-09-19 用户：「一键生成 E:\\AI_Exports\\H3-MotionStudio\\发布成品
    下所有带唱歌的视频生成字幕，已存在字幕就不生成」；2026-09-20 补上音色版与过期规则）：
      · 目录名带「唱歌」标签（`_唱歌_`）—— 只给唱歌视频加字幕，跳舞不动；
      · **输入优先 `最终成片_音色版.mp4`**，没有才用 `最终成片.mp4`（原声母版）；
      · 目录里没有 `最终成片_字幕版.mp4`，**或者**字幕版比输入旧（音色版后来才转的）→ 要烧。

    返回 `[{folder, source, out, song, aweme_id, stale}]`。歌名优先从批量数据库按作品号取
    （`ai.song_name` 更准），取不到再退回从目录名猜。

    2026-09-20：发布根下多了类型子目录，字幕只服务唱歌 → **默认扫 `singing/`**；
    显式传 `root=` 的调用（测试/诊断）保持原样，直接扫那一个目录。
    """
    base = Path(root) if root is not None else (BATCH_OUTPUT_ROOT / publish_subdir("singing"))
    if not base.is_dir():
        return []
    jobs: list[dict[str, Any]] = []
    for folder in sorted(base.iterdir(), key=lambda p: p.name):
        if not folder.is_dir():
            continue
        if "唱歌" not in folder.name:
            continue                          # 只给唱歌标签的视频加字幕
        source = subtitle_source_for(folder)
        if source is None:
            continue                          # 既没有音色版也没有原声母版：跳过
        sub = folder / "最终成片_字幕版.mp4"
        stale = False
        if sub.is_file():
            if not _subtitle_is_stale(sub, source):
                continue                      # 已经有字幕版、而且不比输入旧
            stale = True                      # 输入后来换过：重烧
        song = ""
        aweme_id = _publish_folder_aweme_id(folder.name)
        if aweme_id:
            try:
                item = next(iter(batch_store.items_for_aweme(aweme_id)), None)
            except Exception:                            # noqa: BLE001 —— 批量库不可用就退回猜歌名
                item = None
            if item:
                song = song_name_for(item)
        if not song:
            song = _publish_song_from_folder(folder.name)
        jobs.append({
            "folder": folder.name,
            "source": str(source.resolve()),
            "out": str(sub.resolve()),
            "song": song,
            "aweme_id": aweme_id,
            "stale": stale,
        })
    return jobs


async def run_publish_subtitle_batch(job_id: str) -> None:
    """一键批量：逐条给缺字幕版的唱歌视频生成字幕（**严格串行**，一次只占一个剪映）。

    与独立路由是同一条 `burn_for_video`（剪映识别时间轴 + 官方歌词核对 + 剪映手书烧录）。
    产物直接落回各自的发布目录 `最终成片_字幕版.mp4`（与无字幕版并存）。

    **每次都重新扫一遍发布根**（不沿用上次的队列/光标）：已经出过 `最终成片_字幕版.mp4`
    的自动排除，**上次失败或被中断的那条会自动回到队列**。2026-09-19 实测踩到过：
    第 2 条在剪映识别中被中断判失败后，旧实现把光标推过去了，点「继续」直接从第 3 条
    开始跑，第 2 条就永远补不上字幕。改成重扫之后这个入口是**幂等**的 ——
    「继续生成」/再点一次「一键给所有唱歌视频生成字幕」= 把还缺字幕的重跑一遍。

    **暂停/继续**：剪映识别进行到一半没法安全打断，所以暂停在**视频之间**生效：
    当前这条跑完、在下一条开始之前停下；取消优先于暂停。
    """
    if is_job_cancelled(job_id):
        return
    state = store.get(job_id) or {}
    queue = scan_singing_publish_videos()          # 每次重扫：已完成的不再出现，失败的回来
    total = len(queue)
    if total == 0:
        store.add_log(job_id, "发布根里没有需要补字幕的唱歌视频（都已存在字幕版或没有成片）。")
        store.set_milestone(job_id, "batch", status="completed", progress=100)
        store.update(job_id, status="completed", stage="completed", subtitleQueue=[],
                     subtitleIndex=0, currentNodeId=None, currentNodeTitle=None,
                     progress=100, finishedAt=now_iso())
        return

    store.set_milestone(job_id, "batch", status="running", progress=1)
    store.update(job_id, status="running", stage="subtitle", finishedAt=None,
                 subtitleQueue=queue, subtitleIndex=0,
                 currentNodeId=None, currentNodeTitle=f"待加字幕：{total} 条")
    if not state.get("subtitleStarted"):
        store.add_log(job_id, f"一键批量加字幕：{total} 条唱歌视频缺字幕版。")
        store.update(job_id, subtitleStarted=True, startedAt=now_iso())
    else:
        store.add_log(job_id, f"继续加字幕：发布根里还剩 {total} 条缺字幕版。")

    done = failed = 0
    failed_labels: list[str] = []
    try:
        for index, entry in enumerate(queue, start=1):
            # 取消优先于暂停：取消端点已把状态写成 cancelled，这里不能再改回 paused
            raise_if_cancelled(job_id)
            if (store.get(job_id) or {}).get("pauseRequested"):
                remaining = total - index + 1
                store.update(job_id, status="paused", stage="paused", pauseRequested=False,
                             currentNodeId=None, currentNodeTitle=f"已暂停（还剩 {remaining} 条）")
                store.add_log(job_id, f"已暂停：还剩 {remaining} 条没做，点「继续生成」接着跑。")
                return
            source = Path(str(entry.get("source") or ""))
            out = Path(str(entry.get("out") or ""))
            song = str(entry.get("song") or "").strip()
            label = str(entry.get("folder") or source.parent.name)
            store.update(job_id, currentNodeTitle=f"正在给 {label} 加字幕（{index}/{total}）",
                         subtitleIndex=index, progress=round(index / total * 100))
            if not source.is_file():
                failed += 1
                failed_labels.append(label)
                store.add_log(job_id, f"[{index}/{total}] 跳过：{label} 无字幕成片已不存在")
                store.update(job_id, subtitleDone=done, subtitleFailed=failed)
                continue
            store.add_log(job_id, f"[{index}/{total}] 开始：{label}"
                          + ("（字幕版已过期，重烧）" if entry.get("stale") else "")
                          + (f"《{song}》" if song else "（没有歌名，用识别歌词反查）"))
            stats: dict[str, Any] = {}
            try:
                _path, notes = await burn_for_video(source, song, out, stats=stats)
                done += 1
                store.add_log(job_id, f"[{index}/{total}] 完成：{out.name}"
                              + (f"（{stats.get('lines') or 0} 句字幕）" if stats.get("lines") else ""))
                for note in notes:
                    store.add_log(job_id, f"    文字核对：{note}")
            except Exception as error:                    # noqa: BLE001 —— 单条失败不阻断整批
                failed += 1
                failed_labels.append(label)
                store.add_log(job_id, f"[{index}/{total}] 失败：{label} —— {error}")
            store.update(job_id, subtitleDone=done, subtitleFailed=failed)
        raise_if_cancelled(job_id)
        store.set_milestone(job_id, "batch", status="completed", progress=100)
        store.update(job_id, status="completed", stage="completed",
                     currentNodeId=None, currentNodeTitle=None, progress=100, finishedAt=now_iso())
        store.add_log(job_id, f"批量加字幕结束：本次成功 {done} 条，失败 {failed} 条。")
        if failed:
            store.add_log(
                job_id,
                "失败的：" + "、".join(failed_labels)
                + "。它们还没有 `最终成片_字幕版.mp4` —— 再点一次「一键给所有唱歌视频生成字幕」"
                  "（或「继续生成」）会重扫发布根，只补还缺字幕的那几条。",
            )
    except Exception as error:                            # noqa: BLE001 —— 独立任务：如实报错
        if is_job_cancelled(job_id):
            await finish_cancelled(job_id)
            return
        store.set_milestone(job_id, "batch", status="error")
        store.update(job_id, status="failed", stage="failed",
                     errorSummary=f"批量加字幕失败：{error}", errorDetail=repr(error),
                     finishedAt=now_iso())
        store.add_log(job_id, f"批量加字幕失败：{error}")
    finally:
        await asyncio.to_thread(close_jianying_quietly)


# 一键批量「继续」重排的任务强引用（不持引用会被 GC 掉）
_BATCH_TASKS: set[asyncio.Task] = set()


def _spawn_batch_restart(job_id: str) -> None:
    task = asyncio.create_task(run_publish_subtitle_batch(job_id))
    _BATCH_TASKS.add(task)
    task.add_done_callback(_BATCH_TASKS.discard)


def request_publish_batch_pause(job_id: str) -> dict[str, Any] | None:
    """请求暂停一键批量：在**下一条开始之前**生效（当前这条跑完就停）。不是批量任务返回 None。"""
    state = store.get(job_id)
    if not state or state.get("kind") != "subtitle_batch":
        return None
    if state.get("status") == "running":
        store.add_log(job_id, "收到暂停请求：当前这条跑完即暂停。")
    store.update(job_id, pauseRequested=True)
    return store.get(job_id)


def resume_publish_batch(job_id: str) -> dict[str, Any] | None:
    """继续一条**已暂停**的一键批量（重扫发布根、只补还缺字幕的）。

    不是批量任务 / 不在暂停态就返回 None。

    ⚠️ **必须清掉取消印记 `cancelRequested`**：`is_job_cancelled()` 见了印记就为真，
    runner 第一行 `if is_job_cancelled(job_id): return` 会直接退回去 —— 表现就是
    「点击继续没有效果」（2026-09-19 实测：用户在暂停态点过一次「取消」，印记被永久留下，
    之后每次「继续」都空转）。跟 `reset_subtitle_job` 那条规矩一样：重排前一定清印记。
    """
    state = store.get(job_id)
    if not state or state.get("kind") != "subtitle_batch":
        return None
    if state.get("status") != "paused":
        return None
    store.update(job_id, pauseRequested=False, cancelRequested=False,
                 errorSummary=None, errorDetail=None)
    store.add_log(job_id, "收到继续请求：重扫发布根，只补还缺字幕的那几条。")
    _spawn_batch_restart(job_id)
    return store.get(job_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="剪映识别时间轴 + 官方歌词核对 + 剪映手书烧录")
    ap.add_argument("--video", required=True, help="无字幕成片")
    ap.add_argument("--song", default="", help="歌名；**可省略**——省略时会用识别出的歌词反查官方歌词")
    ap.add_argument("--out", required=True, help="输出路径")
    a = ap.parse_args(argv)
    path, notes = asyncio.run(burn_for_video(Path(a.video), a.song, Path(a.out)))
    for note in notes:
        print("  核对:", note)
    print("已输出:", path, f"{path.stat().st_size / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
