"""本地 CPU SCRFD 定位；只读取已有模型，不下载、不启动 ComfyUI。"""
from pathlib import Path
import json
import sys


def locate(image_path: Path, model: Path) -> list[dict[str, float]]:
    import cv2
    import numpy as np
    frame = cv2.imdecode(np.fromfile(str(image_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return []
    height, width = frame.shape[:2]
    scale = 640 / max(height, width)
    resized = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))))
    canvas = np.zeros((640, 640, 3), dtype=np.uint8)
    canvas[:resized.shape[0], :resized.shape[1]] = resized
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    session = ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
    blob = cv2.dnn.blobFromImage(canvas, 1 / 128, (640, 640), (127.5, 127.5, 127.5), swapRB=True)
    outputs = session.run(None, {session.get_inputs()[0].name: blob})
    boxes, scores = [], []
    for stride in (8, 16, 32):
        count = (640 // stride) ** 2 * 2
        level = [v.reshape(count, -1) for v in outputs if v.shape[0] == count]
        score = next(v for v in level if v.shape[1] == 1).ravel()
        distance = next(v for v in level if v.shape[1] == 4) * stride
        centers = np.stack(np.mgrid[:640 // stride, :640 // stride][::-1], axis=-1).reshape(-1, 2) * stride
        centers = np.repeat(centers, 2, axis=0)
        for i in np.where(score >= 0.65)[0]:
            left, top = (centers[i] - distance[i, :2]) / scale
            right, bottom = (centers[i] + distance[i, 2:]) / scale
            if right - left < 12 or bottom - top < 12:
                continue
            boxes.append([float(left), float(top), float(right-left), float(bottom-top)])
            scores.append(float(score[i]))
    selected = cv2.dnn.NMSBoxes(boxes, scores, 0.65, 0.4)
    result = []
    for i in np.asarray(selected).reshape(-1):
        x, y, w, h = boxes[int(i)]
        result.append({"x0": max(0, x-w*0.15)/width, "y0": max(0, y-h*0.18)/height,
                       "x1": min(width, x+w*1.15)/width, "y1": min(height, y+h*1.18)/height})
    return result


if __name__ == "__main__":
    print(json.dumps(locate(Path(sys.argv[1]), Path(sys.argv[2]))))
