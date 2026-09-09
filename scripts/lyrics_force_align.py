"""用 FunASR fa-zh 将已确认的中文歌词强制对齐到分离后人声。

输入歌词必须已经由上游筛成视频实际唱到的行。本脚本把这些行拼成一条已知文本，
交给官方时间戳预测模型，再把逐字时间还原为逐行起止时间。模型失败或返回文本不一致
时以非零码退出，禁止静默退回等距/人工插值时间轴。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


_ALIGN_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9]")


def normalize_text(text: str) -> str:
    """保留 fa-zh 可对齐的中英文/数字字符，去掉空格与标点。"""
    return "".join(_ALIGN_CHAR.findall(str(text))).lower()


def map_tokens_to_lines(
    lines: list[dict[str, Any]], tokens: list[str], timestamps: list[list[float]]
) -> list[dict[str, Any]]:
    """把模型 token 时间（毫秒）按规范化字符跨度映射回歌词行。"""
    if not lines:
        raise ValueError("没有可对齐的歌词行")
    if len(tokens) != len(timestamps) or not tokens:
        raise ValueError("强制对齐模型未返回完整 token 时间戳")

    normalized_lines = [normalize_text(line.get("orig") or "") for line in lines]
    if any(not text for text in normalized_lines):
        raise ValueError("歌词行在去除标点后为空")
    expected = "".join(normalized_lines)

    spans: list[tuple[int, int, float, float]] = []
    cursor = 0
    observed_parts: list[str] = []
    for token, stamp in zip(tokens, timestamps):
        part = normalize_text(token)
        if not part:
            continue
        if not isinstance(stamp, (list, tuple)) or len(stamp) < 2:
            raise ValueError("强制对齐模型返回了无效时间戳")
        start_ms, end_ms = float(stamp[0]), float(stamp[1])
        spans.append((cursor, cursor + len(part), start_ms, end_ms))
        cursor += len(part)
        observed_parts.append(part)
    observed = "".join(observed_parts)
    if observed != expected:
        raise ValueError(
            f"强制对齐文本不一致：期望 {len(expected)} 字，模型返回 {len(observed)} 字"
        )

    result: list[dict[str, Any]] = []
    line_start = 0
    for line, text in zip(lines, normalized_lines):
        line_end = line_start + len(text)
        overlapping = [span for span in spans if span[0] < line_end and span[1] > line_start]
        if not overlapping:
            raise ValueError(f"歌词第 {line.get('index')} 行没有可用时间戳")
        result.append(
            {
                "index": int(line["index"]),
                "start": round(overlapping[0][2] / 1000.0, 3),
                "end": round(overlapping[-1][3] / 1000.0, 3),
                "orig": str(line.get("orig") or ""),
            }
        )
        line_start = line_end
    return result


def run_alignment(vocals: Path, lines: list[dict[str, Any]], model_dir: Path) -> dict[str, Any]:
    import torch
    from funasr import AutoModel

    transcript = "".join(normalize_text(line.get("orig") or "") for line in lines)
    if not transcript:
        raise ValueError("没有可对齐的中文歌词")
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"加载 FunASR fa-zh（{device}）", flush=True)
    model = AutoModel(
        model=str(model_dir),
        device=device,
        disable_update=True,
        disable_pbar=True,
    )
    with tempfile.TemporaryDirectory(prefix="lyrics_fa_zh_") as td:
        text_file = Path(td) / "transcript.txt"
        text_file.write_text(transcript + "\n", encoding="utf-8")
        generated = model.generate(
            input=(str(vocals), str(text_file)),
            data_type=("sound", "text"),
        )
    if not generated or not isinstance(generated[0], dict):
        raise ValueError("强制对齐模型返回空结果")
    item = generated[0]
    tokens = str(item.get("text") or "").split()
    timestamps = item.get("timestamp") or []
    aligned = map_tokens_to_lines(lines, tokens, timestamps)
    return {
        "engine": "funasr-fa-zh",
        "device": device,
        "tokenCount": len(tokens),
        "lines": aligned,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("vocals", type=Path)
    parser.add_argument("lines_json", type=Path)
    parser.add_argument("out_json", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()

    try:
        vocals = args.vocals.resolve()
        lines_path = args.lines_json.resolve()
        model_dir = args.model.resolve()
        if not vocals.is_file():
            raise FileNotFoundError(f"找不到分离后人声：{vocals}")
        if not lines_path.is_file():
            raise FileNotFoundError(f"找不到歌词输入：{lines_path}")
        if not model_dir.is_dir():
            raise FileNotFoundError(f"找不到强制对齐模型：{model_dir}")
        lines = json.loads(lines_path.read_text(encoding="utf-8"))
        if not isinstance(lines, list):
            raise ValueError("歌词输入必须是数组")
        payload = run_alignment(vocals, lines, model_dir)
        out = args.out_json.resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"强制对齐完成：{len(payload['lines'])} 行 / {payload['tokenCount']} token",
            flush=True,
        )
        print("RESULT " + str(out), flush=True)
    except Exception as error:
        print(f"ERR {error}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
