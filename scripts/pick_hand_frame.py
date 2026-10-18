"""Pick the frame with the **clearest hands** from a finished video.

Run by ComfyUI's embedded python (ultralytics + torch + cv2 are available there).
The batch worker calls this after a video is delivered: the chosen frame becomes
`封面人物图.png` in the publish folder, and both covers plus the `/images` page
use it as their reference instead of the person image (whose hands may be wrong).

The scan is **every frame** of the final video. At 640 px wide, a hand detector
(`hand_yolov8s.pt` from Impact Pack) locates hands; Laplacian variance inside each
hand box measures blur. The weaker of the two best hands dominates, boxes touching
the frame border (cut fingers) are penalized, low-confidence boxes are ignored.
Batches of 16 frames; CUDA is used when it has enough free memory, otherwise CPU.

Output: writes the chosen frame at full resolution and prints one JSON line.
Exit code 0 = written; 2 = no usable hand frame (caller falls back).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import cv2
from ultralytics import YOLO


def _resolve_device(name: str) -> str:
    if name and name != "auto":
        return name
    try:
        import torch

        if torch.cuda.is_available():
            free, _total = torch.cuda.mem_get_info()
            if free > 2500 * 1024 * 1024:
                return "0"
    except Exception:  # noqa: BLE001 - 选不到 GPU 就走 CPU
        pass
    return "cpu"


def _score_frame(gray, boxes, confs) -> tuple[float, list[dict]]:
    height, width = gray.shape[:2]
    hands: list[dict] = []
    for (x1, y1, x2, y2), conf in zip(boxes, confs):
        x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
        bw, bh = x2 - x1, y2 - y1
        if conf < 0.4 or bw < 18 or bh < 18:
            continue
        area = bw * bh / float(width * height)
        if area < 0.0015 or area > 0.08:
            continue
        cut = x1 <= 2 or y1 <= 2 or x2 >= width - 2 or y2 >= height - 2
        ix, iy = bw * 0.12, bh * 0.12
        crop = gray[int(y1 + iy):int(y2 - iy), int(x1 + ix):int(x2 - ix)]
        if crop.size < 400:
            continue
        lap = float(cv2.Laplacian(crop, cv2.CV_64F).var())
        hands.append({
            "lap": lap,
            "cut": bool(cut),
            "conf": float(conf),
            "box": [round(x1), round(y1), round(x2), round(y2)],
        })
    if not hands:
        return 0.0, []
    hands.sort(key=lambda row: row["lap"], reverse=True)
    top = hands[:2]
    base = min(row["lap"] for row in top) if len(top) >= 2 else top[0]["lap"] * 0.6
    cut_factor = 0.5 if any(row["cut"] for row in top) else 1.0
    conf_factor = 0.7 + 0.3 * (sum(row["conf"] for row in top) / len(top))
    return base * cut_factor * conf_factor, top


def _grab(cap, index: int):
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    if ok:
        return frame
    # 某些编码 seek 不精确：重开顺序读到那一帧。
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    frame = None
    for current in range(index + 1):
        ok, frame = cap.read()
        if not ok:
            return None
    return frame


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--batch", type=int, default=16)
    args = parser.parse_args()

    device = _resolve_device(args.device)
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(json.dumps({"ok": False, "reason": "cannot-open-video"}))
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    model = YOLO(args.model)
    started = time.time()
    best_score = -1.0
    best_index = -1
    best_hands: list[dict] = []
    frames = 0
    batch_frames: list = []
    batch_indexes: list[int] = []

    def flush() -> None:
        nonlocal best_score, best_index, best_hands, model, device
        if not batch_frames:
            return
        try:
            results = model.predict(batch_frames, conf=0.25, verbose=False, device=device)
        except Exception:  # noqa: BLE001 - GPU 显存不够时退回 CPU 重跑这一批
            if device == "cpu":
                raise
            model = YOLO(args.model)
            device = "cpu"
            results = model.predict(batch_frames, conf=0.25, verbose=False, device="cpu")
        for position, (index, result) in enumerate(zip(batch_indexes, results)):
            boxes = result.boxes.xyxy.cpu().numpy().tolist() if result.boxes is not None else []
            confs = result.boxes.conf.cpu().numpy().tolist() if result.boxes is not None else []
            gray = cv2.cvtColor(batch_frames[position], cv2.COLOR_BGR2GRAY)
            score, hands = _score_frame(gray, boxes, confs)
            if score > best_score:
                best_score, best_index, best_hands = score, index, hands
        batch_frames.clear()
        batch_indexes.clear()

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        height, width = frame.shape[:2]
        batch_frames.append(cv2.resize(frame, (640, max(2, round(height * 640 / width)))))
        batch_indexes.append(frames)
        frames += 1
        if len(batch_frames) >= max(1, args.batch):
            flush()
    flush()

    if best_index < 0 or not best_hands:
        cap.release()
        print(json.dumps({"ok": False, "reason": "no-hand", "frames": frames}))
        return 2
    chosen = _grab(cap, best_index)
    cap.release()
    if chosen is None:
        print(json.dumps({"ok": False, "reason": "cannot-grab-best", "frames": frames}))
        return 2
    if args.output.lower().endswith((".jpg", ".jpeg")):
        written = cv2.imwrite(args.output, chosen, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    else:
        written = cv2.imwrite(args.output, chosen)
    print(json.dumps({
        "ok": bool(written),
        "frames": frames,
        "time": round(best_index / fps, 3),
        "score": round(best_score, 1),
        "hands": best_hands,
        "device": device,
        "elapsed": round(time.time() - started, 1),
    }, ensure_ascii=False))
    return 0 if written else 3


if __name__ == "__main__":
    sys.exit(main())
