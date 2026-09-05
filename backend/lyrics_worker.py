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
        if not text or text.startswith(("作词", "作曲", "编曲", "by:")):
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
    """归一化：小写、去标点；中日韩去空格逐字比较，拉丁按词比较。"""
    text = text.lower()
    text = re.sub(r"[^\w\u3040-\u30ff\uac00-\ud7af\u4e00-\u9fff ]", "", text)
    if detect_lang(text) in ("zh", "ko", "ja"):
        return text.replace(" ", "")
    return " ".join(text.split())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def align_line_times(
    asr: dict[str, Any], lyric_lines: list[dict[str, Any]]
) -> tuple[list[float], int]:
    """逐行匹配 ASR 词时间戳；返回（每行 clip 起始秒, 高置信命中数）。

    官方时间（line.time，秒）存在时把候选窗口限定在 [official-3.2, official+3.2]
    内（翻唱可整体提前/拖后但不会差几秒），避免把歌词锚到错乱位置；行间
    未命中的用相邻锚点按官方时间线性插值兜底。
    """
    duration = float(asr.get("duration") or 0)
    tokens: list[tuple[str, float]] = []
    for seg in asr.get("segments") or []:
        words = seg.get("words") or []
        if words:
            for word, start, _end in words:
                tokens.append((str(word), float(start)))
        else:
            tokens.append((str(seg.get("text") or ""), float(seg.get("start") or 0)))
    tokens.sort(key=lambda item: item[1])

    anchors: list[tuple[int, float]] = []  # (line_index, clip_time)
    cursor = 0
    matched = 0
    for index, line in enumerate(lyric_lines):
        target = _norm(line["orig"])
        if not target:
            continue
        official = float(line.get("time") or 0.0)
        best: tuple[float, int, int] | None = None
        for j in range(cursor, min(cursor + 90, len(tokens))):
            token_time = tokens[j][1]
            if official > 0 and (token_time < official - 3.2 or token_time > official + 3.2):
                continue  # 超出官方时间窗的词不参与该行候选
            for k in range(j, min(j + 8, len(tokens))):
                window = " ".join(t for t, _ in tokens[j : k + 1])
                score = _ratio(target, window)
                if best is None or score > best[0]:
                    best = (score, j, k)
        if best and best[0] >= 0.50:
            start_time = tokens[best[1]][1]
            if not anchors or start_time > anchors[-1][1] - 0.8:
                anchors.append((index, start_time))
                cursor = best[1] + 1
                matched += 1

    official = [float(line.get("time") or 0.0) for line in lyric_lines]
    if not anchors and not any(t > 0.0 for t in official):
        # 完全无官方时间也无实测锚点：按行数等距铺开（尽力而为并提示）
        count = max(1, len(lyric_lines))
        span = max(0.0, duration - 0.8)
        return [max(0.0, 0.25 + span * index / count) for index in range(count)], matched, (
            tokens[-1][1] if tokens else duration
        )
    times: list[float] = []
    for index, line in enumerate(lyric_lines):
        t = float(line.get("time") or 0.0)
        prev_anchor = [a for a in anchors if a[0] <= index]
        next_anchor = [a for a in anchors if a[0] >= index]
        if prev_anchor and prev_anchor[-1][0] == index:
            times.append(prev_anchor[-1][1])
            continue
        if prev_anchor and next_anchor:
            (ia, ta), (ib, tb) = prev_anchor[-1], next_anchor[0]
            oa, ob = official[ia], official[ib]
            if ob > oa and t > oa:
                ratio = (t - oa) / (ob - oa)
                times.append(ta + ratio * (tb - ta))
                continue
        if prev_anchor:
            times.append(prev_anchor[-1][1] + (t - official[prev_anchor[-1][0]]))
            continue
        if next_anchor:
            times.append(next_anchor[0][1] - (official[next_anchor[0][0]] - t))
            continue
        times.append(max(0.0, min(duration - 0.2, t)))
    # 单调保护 + 夹到片长内
    last = -1.0
    clipped: list[float] = []
    for t in times:
        t = max(last + 0.05, min(duration - 0.15, t)) if duration else max(last + 0.05, t)
        clipped.append(t)
        last = t
    return clipped, matched, (tokens[-1][1] if tokens else duration)


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


async def _run_stage_script(job_id: str, video: Path, out_json: Path) -> dict[str, Any]:
    """执行 RVC venv 阶段脚本；实时回传日志并推进里程碑。"""
    if not LYRICS_ASR_PY.is_file():
        raise PipelineError("语音识别环境不完整", f"缺少：{LYRICS_ASR_PY}")
    if not LYRICS_ASR_MODEL.is_dir():
        raise PipelineError(
            "缺少语音识别模型",
            f"预期模型目录：{LYRICS_ASR_MODEL}\n（faster-whisper base，可从 hf-mirror.com/Systran/faster-whisper-base 下载后解压使用）",
        )
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
            payload = await _run_stage_script(job_id, source, asr_json)
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
            times, matched, last_vocal = await asyncio.to_thread(align_line_times, payload, lines)
            if matched == 0:
                store.add_log(job_id, "警告：识别文本与歌词匹配度低，已按官方歌词时间插值（建议试听后反馈修正）。")
            store.add_log(job_id, f"对齐完成：{len(times)} 行（高置信实测 {matched} 行）。")
            raise_if_cancelled(job_id)

            # 组装 cue：丢弃越界行与「识别不到演唱」的尾部行；结束时间取下一句起点
            duration = float(payload.get("duration") or meta.get("duration") or 0)
            kept: list[tuple[dict[str, Any], float]] = []
            for line, start in zip(lines, times):
                if start is None or start < -0.05 or start >= duration - 0.2:
                    continue
                if start > last_vocal + 1.0:
                    continue  # 该行在音频里没有被唱到（识别词已结束）
                if kept and start - kept[-1][1] < 0.45:
                    continue  # 尾部截断产生的重复时刻只留一条
                kept.append((line, start))
            skipped = len(lines) - len(kept)
            if skipped:
                store.add_log(job_id, f"自动截除超出视频时长的歌词 {skipped} 行（保留 {len(kept)} 行）。")
            cues = [
                {
                    "start": start,
                    "end": duration - 0.05,
                    "orig": str(line.get("orig") or ""),
                    "zh": str(line.get("zh") or ""),
                }
                for line, start in kept
            ]
            cues.sort(key=lambda cue: cue["start"])
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
            # 顺带保留 SRT（导入剪映精修用），不对外展示
            (out_dir / f"{job_id}_歌词字幕.srt").write_text(_cues_to_srt(cues), encoding="utf-8-sig")

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
