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
import os
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


def _enable_cuda_dlls() -> None:
    """把 pip 安装的 nvidia cuBLAS/cuDNN DLL 目录加入加载路径（Windows）。

    faster-whisper/CTranslate2 的 CUDA 版本需要 cublas64_12.dll / cudnn64_*.dll，
    它们随 pip 包装在 site-packages\\nvidia\\*\\bin 下。必须在 import
    faster_whisper / ctranslate2 **之前**调用，否则 DLL 解析失败并缓存，
    之后补注入无效（报 "Library cublas64_12.dll is not found ..."）。
    """
    if sys.platform != "win32":
        return
    try:
        import sysconfig

        pure = Path(sysconfig.get_paths().get("purelib", ""))
        if not pure.is_dir():
            return
        nvidia_root = pure / "nvidia"
        if not nvidia_root.is_dir():
            return
        for bin_dir in sorted(nvidia_root.glob("*/bin")):
            try:
                os.add_dll_directory(str(bin_dir))
            except (OSError, ValueError):
                pass
        # PATH 兜底：个别加载器只按 PATH 找依赖库
        os.environ["PATH"] = os.pathsep.join(
            [str(b) for b in sorted(nvidia_root.glob("*/bin"))]
        ) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass


def _vocal_activity(vocal: np.ndarray, rate: int) -> tuple[list[float], list[list[float]]]:
    """人声干声能量分析：返回（起唱点秒列表, 停顿区间 [[s,e],...]）。

    whisper 词级时间戳在带伴奏/DJ 混音上普遍滞后或抖动（实测 0.3-1s），
    但字幕需要贴住真实发声：起唱点用于把推算行吸到真实发声处；停顿区间
    （低能量 ≥0.45s）用于让字幕在唱完的停顿处及时消失、并剔除落在长停顿
    中间的推算行。
    - 40ms 能量包络，噪声底取 5% 分位，阈值 = max(底×2.5, 峰值×0.10)；
    - 起唱点：上升沿（前一帧低于阈值），相邻 <0.25s 合并；
    - 停顿：连续低于阈值 ≥0.45s 的区段。
    """
    if vocal.ndim == 2:
        # separate_vocals 返回 [声道, 采样]
        vocal = vocal.mean(axis=0 if vocal.shape[0] < vocal.shape[1] else 1)
    win = max(1, int(rate * 0.04))
    env = np.sqrt(np.convolve(vocal ** 2, np.ones(win) / win, mode="same"))
    noise = float(np.percentile(env, 5))
    threshold = max(noise * 2.5, float(env.max()) * 0.10)
    above = env >= threshold
    onsets: list[float] = []
    prev_time = -1.0
    for i in range(1, len(above)):
        if above[i] and not above[i - 1]:
            t = i / rate
            if prev_time >= 0 and t - prev_time < 0.25:
                continue
            onsets.append(round(t, 3))
            prev_time = t
    pauses: list[list[float]] = []
    gap_start: int | None = None
    min_gap = int(0.45 * rate)
    n = len(above)
    for i in range(n + 1):
        is_low = i < n and not above[i]
        if is_low and gap_start is None:
            gap_start = i
        elif not is_low and gap_start is not None:
            if i - gap_start >= min_gap:
                pauses.append([round(gap_start / rate, 3), round(i / rate, 3)])
            gap_start = None
    return onsets, pauses


def _transcribe(
    device: str, model_dir: str, vocal_wav: Path, prompt_text: str | None, info_holder: list
) -> list[dict]:
    """跑一次完整识别，返回序列化后的语音段列表（可空）。"""
    from faster_whisper import WhisperModel

    _enable_cuda_dlls()
    model = WhisperModel(
        model_dir, device=device, compute_type="int8",
        cpu_threads=4 if device == "cpu" else 0,  # CPU 少线程更稳（14 线程易空转）
    )
    segments, info = model.transcribe(
        str(vocal_wav), language=None, word_timestamps=True,
        vad_filter=False, condition_on_previous_text=False,
        initial_prompt=prompt_text,
    )
    segs = []
    for seg in segments:
        words = [[w.word, float(w.start), float(w.end)] for w in (seg.words or [])]
        segs.append({
            "start": float(seg.start), "end": float(seg.end),
            "text": seg.text.strip(), "words": words,
        })
    info_holder.clear()
    info_holder.append(info)
    del model  # 立即释放显存/内存，避免重试链上模型越积越多
    import gc

    gc.collect()
    return segs


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
    parser.add_argument("--model", default=r"D:\tmp\fw-small")
    parser.add_argument(
        "--vocals-out",
        default=None,
        help="可选：持久化保存 Demucs 分离后的人声 WAV，供后续强制对齐复用",
    )
    parser.add_argument(
        "--prompt-file",
        default=None,
        help="官方歌词提示文件：内容会作为 initial_prompt 注入识别器（UTF-8 文本），"
        "抑制带伴奏演唱的错字/幻觉，明显提升歌词文本与官方歌词的吻合度",
    )
    args = parser.parse_args()

    # CUDA DLL 注入必须在任何 faster_whisper/ctranslate2 import 之前
    _enable_cuda_dlls()

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
        vocal_wav = (
            Path(args.vocals_out).resolve()
            if args.vocals_out
            else tmp / "vocals.wav"
        )
        vocal_wav.parent.mkdir(parents=True, exist_ok=True)
        sf.write(vocal_wav, vocal.T, rate, subtype="PCM_16")

        from faster_whisper import WhisperModel  # imported late: CPU-compat only needed

        log("[3/4] 语音识别（自动检测语种，实测逐词时间）")
        prompt_text: str | None = None
        if args.prompt_file:
            prompt_path = Path(args.prompt_file).resolve()
            if prompt_path.is_file():
                prompt_text = prompt_path.read_text(encoding="utf-8").strip()
                if prompt_text:
                    log(f"[3/4] 已注入官方歌词提示（{len(prompt_text)} 字符）")
            else:
                log(f"[3/4] 警告：提示文件不存在 {prompt_path}")

        # 设备选择：CUDA 快且稳定（CPU int8 推理既慢又会间歇性返回空语音段，
        # 实测 ~50% 空转）；CUDA 不可用/显存不足时回退 CPU。
        import ctranslate2

        devices = ["cuda", "cpu"]
        segs: list[dict] = []
        info_holder: list[object] = []
        last_error: str | None = None
        for device in devices:
            try:
                available = ctranslate2.get_cuda_device_count() > 0 if device == "cuda" else True
                if not available:
                    continue
                segs = _transcribe(device, args.model, vocal_wav, prompt_text, info_holder)
                if segs:
                    break
            except Exception as error:  # OOM / 驱动异常等 → 换设备或重试
                last_error = repr(error)
                log(f"[3/4] 警告：{device} 推理不可用（{last_error}），改用 CPU 重试")
                continue

        # 空段保护：whisper 偶发对整段音频返回 0 语音段（CPU int8 实测概率性出现），
        # 直接重跑整个识别（换新模型实例），连续多次仍空才按失败处理——绝不静默
        # 产出「等距铺开」的假字幕。
        attempt = 1
        while not segs and attempt <= 3:
            log(f"[3/4] 警告：第 {attempt} 次识别返回空语音段，重建模型重试……")
            try:
                segs = _transcribe("cpu", args.model, vocal_wav, prompt_text, info_holder)
            except Exception as error:
                last_error = repr(error)
                log(f"[3/4] 警告：CPU 推理异常（{last_error}）")
            attempt += 1
        if not segs:
            log("ERR 连续多次识别均为空（未检测到演唱人声），请换人声更清晰的视频后重试")
            if last_error:
                log("ERR " + last_error)
            sys.exit(2)
        onsets, pauses = _vocal_activity(vocal, rate)
        payload = {
            "duration": round(duration, 3),
            "language": str(getattr(info_holder[0], "language", "") or "") if info_holder else "",
            "language_probability": round(float(getattr(info_holder[0], "language_probability", 0) or 0), 3) if info_holder else 0.0,
            "segments": segs,
            # 干声能量分析（worker 用它贴住真实发声、在停顿处及时收字幕）：
            # onsets = 起唱点秒（升序）；pauses = 低能量停顿区间 [[s,e],...]
            "onsets": onsets,
            "pauses": pauses,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        log(f"[4/4] 识别完成：{payload['language']} · {len(segs)} 段")
        log("RESULT " + str(out))


if __name__ == "__main__":
    main()
