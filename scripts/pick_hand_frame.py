"""Pick the frame with the **clearest hands** from a finished video.

Run by ComfyUI's embedded python (ultralytics + torch + cv2 are available there).
The batch worker calls this before generating covers: person images may have
malformed fingers, while the hands in the generated video are normal, so the
cover reference frame must show hands clearly.

Scoring: a local hand detector (`hand_yolov8s.pt` from Impact Pack) locates the
hands, then Laplacian variance inside each hand box measures blur. The weakest
of the two best hands dominates, boxes touching the frame border are penalized
(fingers cut off), and low-confidence boxes are ignored. Sampling is ~4 fps.

Output: writes the chosen frame at full resolution and prints one JSON line.
Exit code 0 = frame written; 2 = no usable hand frame (caller falls back).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import cv2
from ultralytics import YOLO


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--sample-fps", type=float, default=4.0)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(json.dumps({"ok": False, "reason": "cannot-open-video"}))
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(fps / max(0.5, args.sample_fps)))
    model = YOLO(args.model)
    started = time.time()
    best_score = -1.0
    best_frame = None
    best_meta: dict = {}
    sampled = 0
    index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % step:
            index += 1
            continue
        height, width = frame.shape[:2]
        small = cv2.resize(frame, (640, max(2, round(height * 640 / width))))
        result = model.predict(small, conf=0.25, verbose=False, device=args.device)[0]
        boxes = result.boxes.xyxy.cpu().numpy().tolist() if result.boxes is not None else []
        confs = result.boxes.conf.cpu().numpy().tolist() if result.boxes is not None else []
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        score, hands = _score_frame(gray, boxes, confs)
        sampled += 1
        if score > best_score:
            best_score = score
            best_frame = frame.copy()
            best_meta = {"score": round(score, 1), "time": round(index / fps, 2), "hands": hands}
        index += 1
    cap.release()

    if best_frame is None or not best_meta.get("hands"):
        print(json.dumps({"ok": False, "reason": "no-hand", "sampled": sampled}))
        return 2
    written = cv2.imwrite(args.output, best_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    print(json.dumps({
        "ok": bool(written),
        "sampled": sampled,
        "elapsed": round(time.time() - started, 1),
        **best_meta,
    }, ensure_ascii=False))
    return 0 if written else 3


if __name__ == "__main__":
    sys.exit(main())
