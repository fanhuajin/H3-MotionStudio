"""歌词字幕（多语言）阶段脚本：demucs 分离人声 -> faster-whisper 自动语种+词级时间戳。

Run with the RVC venv python (torch/torchaudio/faster-whisper installed):
    .venv\\Scripts\\python.exe scripts/lyrics_stage.py <video> <out_json> [--model DIR]

Output JSON:
{
  "duration": float,
  "language": "ko|ja|zh|en|...",
  "segments": [{"start": s, "end": e, "text": "...", "words": [[w, start, end], ...]}]
}
Progress markers on stdout: [1/4]..[4/4] 供后端映射里程碑。
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from torchaudio.pipelines import HDEMUCS_HIGH_MUSDB_PLUS
from torchaudio.transforms import Fade, Resample


def log(msg: str) -> None:
    print(msg, flush=True)


def extract_wav(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "2", "-ar", "44100",
         "-c:a", "pcm_s16le", str(dst)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def separate_vocals(wav: Path, chunk_length: float = 10.0, overlap: float = 0.1):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = HDEMUCS_HIGH_MUSDB_PLUS.get_model().to(device)
    model.eval()
    model_rate = HDEMUCS_HIGH_MUSDB_PLUS.sample_rate
    waveform, sample_rate = sf.read(wav, dtype="float32", always_2d=True)
    waveform = torch.from_numpy(waveform.T).to(device)
    if waveform.shape[0] == 1:
        waveform = waveform.repeat(2, 1)
    if sample_rate != model_rate:
        waveform = Resample(sample_rate, model_rate).to(device)(waveform)
    ref = waveform.mean(0)
    waveform = (waveform - ref.mean()) / ref.std()

    chunk_len = int(model_rate * chunk_length * (1 + overlap))
    overlap_frames = int(overlap * model_rate)
    fade = Fade(fade_in_len=0, fade_out_len=overlap_frames, fade_shape="linear")
    final = torch.zeros(1, len(model.sources), 2, waveform.shape[1], device=device)
    start, end = 0, chunk_len
    with torch.no_grad():
        while start < waveform.shape[1] - overlap_frames:
            out = model.forward(waveform[:, start:end].unsqueeze(0))
            final[:, :, :, start:end] += fade(out)
            if start == 0:
                fade.fade_in_len = overlap_frames
                start += chunk_len - overlap_frames
            else:
                start += chunk_len
            end += chunk_len
            if end >= waveform.shape[1]:
                fade.fade_out_len = 0
    sources = (final[0] * ref.std() + ref.mean()).cpu().numpy()
    vocal = sources[list(model.sources).index("vocals")]
    return vocal, model_rate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("out_json", type=Path)
    parser.add_argument("--model", default=r"D:\tmp\fw-base")
    args = parser.parse_args()

    video = args.video.resolve()
    out = args.out_json.resolve()
    if not video.is_file():
        log("ERR video not found")
        sys.exit(1)

    log("[1/4] 提取音频")
    with tempfile.TemporaryDirectory(prefix="lyrics_stage_") as td:
        tmp = Path(td)
        wav = tmp / "src.wav"
        extract_wav(video, wav)
        duration = float(sf.info(wav).duration)
        log(f"[2/4] Demucs 人声分离（{duration:.1f}s）")
        vocal, rate = separate_vocals(wav)
        vocal_wav = tmp / "vocals.wav"
        sf.write(vocal_wav, vocal.T, rate, subtype="PCM_16")

        from faster_whisper import WhisperModel  # imported late: CPU-compat only needed

        log("[3/4] 语音识别（自动检测语种，实测逐词时间）")
        model = WhisperModel(str(args.model), device="cpu", compute_type="int8")
        segments, info = model.transcribe(
            str(vocal_wav), language=None, word_timestamps=True,
            vad_filter=False, condition_on_previous_text=False,
        )
        segs = []
        for seg in segments:
            words = [[w.word, float(w.start), float(w.end)] for w in (seg.words or [])]
            segs.append({
                "start": float(seg.start), "end": float(seg.end),
                "text": seg.text.strip(), "words": words,
            })
        payload = {
            "duration": round(duration, 3),
            "language": str(info.language or ""),
            "language_probability": round(float(info.language_probability or 0), 3),
            "segments": segs,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        log(f"[4/4] 识别完成：{payload['language']} · {len(segs)} 段")
        log("RESULT " + str(out))


if __name__ == "__main__":
    main()
