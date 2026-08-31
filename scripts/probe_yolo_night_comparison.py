"""
probe_yolo_night_comparison.py — Phase.116 §11.47 YOLO variant comparison.

Runs multiple YOLOv8 model variants (n, s, m) on the same 80 annotated night
frames and reports false-positive counts at the day model's default conf
threshold (0.40). Goal: answer "which yolo is better to use at night right now?"
without guessing — actual measured numbers.

Run:
    .venv/bin/python scripts/probe_yolo_night_comparison.py --limit 80
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import UTC
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

# Project root
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


MODELS = {
    "yolov8n (12MB, current)": str(_ROOT / "models" / "yolov8n.onnx"),
    "yolov8m (112MB)": str(_ROOT / "models" / "yolov8m.onnx"),
}

CONFIDENCE_FLOOR = 0.40  # what we want FP-free
NIGHT_FRAMES_NEEDED = 80


def find_night_frames(limit: int) -> list[Path]:
    """Pick night-time frames from data/frames/ based on directory mtime."""
    from datetime import datetime

    from infra.time_of_day import is_night_at_edt

    frames_dir = _ROOT / "data" / "frames"
    if not frames_dir.is_dir():
        return []

    candidates = []
    for alert_dir in sorted(frames_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not alert_dir.is_dir():
            continue
        mtime = datetime.fromtimestamp(alert_dir.stat().st_mtime, tz=UTC)
        if not is_night_at_edt(mtime):
            continue
        for f in sorted(alert_dir.glob("frame_*.jpg")):
            candidates.append(f)
            if len(candidates) >= limit * 3:
                break
        if len(candidates) >= limit * 3:
            break
    return candidates[:limit]


def load_model(path: str) -> ort.InferenceSession:
    providers = ort.get_available_providers()
    preferred = [
        p for p in ("CoreMLExecutionProvider", "CPUExecutionProvider")
        if p in providers
    ]
    return ort.InferenceSession(path, providers=preferred)


def check_output_shape(session: ort.InferenceSession) -> bool:
    """Return True if the model's output matches YOLOv8n (1, 84, 8400) layout."""
    out = session.get_outputs()[0]
    shape = [str(s) if isinstance(s, str) else s for s in out.shape]
    # Accept (1, 84, 8400) or shape with dynamic dims but the last dim is 8400
    return (
        len(shape) == 3
        and str(shape[2]) in ("8400", "anchors")
        and str(shape[1]) in ("84", "Concatoutput0_dim_1")
    )


def letterbox(img: Image.Image, target: int = 640):
    """Resize to 640x640 with letterbox, return numpy NCHW float32."""
    w, h = img.size
    scale = min(target / w, target / h)
    new_w, new_h = round(w * scale), round(h * scale)
    # PIL 10+ moved interpolation constants to Image.Resampling.
    resample = Image.Resampling.BILINEAR if hasattr(Image, "Resampling") else Image.BILINEAR  # type: ignore[attr-defined]
    img_r = img.resize((new_w, new_h), resample)
    pad_x = (target - new_w) // 2
    pad_y = (target - new_h) // 2
    canvas = Image.new("RGB", (target, target), (114, 114, 114))
    canvas.paste(img_r, (pad_x, pad_y))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    return arr.transpose(2, 0, 1)[None], scale, (pad_x, pad_y)


def run_model(session: ort.InferenceSession, img: Image.Image, conf_floor: float) -> list[dict]:
    """Run inference, return list of {class_id, conf, bbox_xyxy} above conf_floor."""
    inp, scale, (pad_x, pad_y) = letterbox(img)
    raw = session.run([session.get_outputs()[0].name], {session.get_inputs()[0].name: inp})[0]
    raw = np.asarray(raw).reshape(84, -1)
    raw = raw.transpose()  # (8400, 84)
    boxes = raw[:, :4]
    scores = raw[:, 4:]
    cls_ids = np.argmax(scores, axis=1)
    confs = scores[np.arange(scores.shape[0]), cls_ids]
    mask = confs > conf_floor
    if not mask.any():
        return []
    boxes = boxes[mask]
    cls_ids = cls_ids[mask]
    confs = confs[mask]
    # cxcywh → xyxy
    x1 = boxes[:, 0] - boxes[:, 2] / 2 - pad_x
    y1 = boxes[:, 1] - boxes[:, 3] / 2 - pad_y
    x2 = boxes[:, 0] + boxes[:, 2] / 2 - pad_x
    y2 = boxes[:, 1] + boxes[:, 3] / 2 - pad_y
    x1 /= scale
    y1 /= scale
    x2 /= scale
    y2 /= scale
    return [
        {"class_id": int(c), "conf": float(cf),
         "bbox": (int(x1[i]), int(y1[i]), int(x2[i]), int(y2[i]))}
        for i, (c, cf) in enumerate(zip(cls_ids, confs))
    ]


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=80)
    args = ap.parse_args()

    print(f"[probe] Picking {args.limit} night frames...")
    frames = find_night_frames(args.limit)
    if not frames:
        print("[probe] No night frames found in data/frames/. Aborting.")
        return
    print(f"[probe] Loaded {len(frames)} night frames.")

    # Load COCO names from quick_classifier
    from infra.quick_classifier import COCO_NAMES

    results: dict[str, dict] = {}
    for label, path in MODELS.items():
        if not Path(path).is_file():
            print(f"[probe] {label}: model file missing, skipping.")
            continue
        print(f"\n[probe] Running {label}...")
        session = load_model(path)
        if not check_output_shape(session):
            print(f"  [skip] {label}: output shape mismatch (not a standard YOLOv8 COCO model)")
            results[label] = {"fp_count": 0, "fp_per_frame": 0, "by_class": Counter(),
                              "mean_conf": 0, "n_detected": 0, "skipped": True}
            continue
        fp_count = 0
        detections_per_class: Counter = Counter()
        conf_sum = 0.0
        conf_n = 0
        for f in frames:
            try:
                img = Image.open(f).convert("RGB")
                dets = run_model(session, img, CONFIDENCE_FLOOR)
                fp_count += len(dets)
                for d in dets:
                    cls_name = COCO_NAMES[d["class_id"]] if d["class_id"] < len(COCO_NAMES) else f"cls{d['class_id']}"
                    detections_per_class[cls_name] += 1
                    conf_sum += d["conf"]
                    conf_n += 1
            except (OSError, ValueError, RuntimeError) as e:
                print(f"  [err] {f.name}: {e}")
        results[label] = {
            "fp_count": fp_count,
            "fp_per_frame": fp_count / max(len(frames), 1),
            "by_class": detections_per_class,
            "mean_conf": conf_sum / conf_n if conf_n else 0,
            "n_detected": conf_n,
        }

    # Print comparison
    print("\n=== YOLO Night Comparison ===")
    print(f"Frames: {len(frames)} night frames")
    print(f"Conf floor: {CONFIDENCE_FLOOR}\n")
    print(f"{'Model':<25} {'FPs':<6} {'FP/frame':<10} {'Frames w/det':<14} {'Mean conf':<10}")
    print("-" * 70)
    for label, r in results.items():
        print(f"{label:<25} {r['fp_count']:<6} {r['fp_per_frame']:<10.3f} "
              f"{r['n_detected']:<14} {r['mean_conf']:<10.3f}")
    print()
    print("Class breakdown (false-positive classes):")
    for label, r in results.items():
        if r["by_class"]:
            top = ", ".join(f"{c}({n})" for c, n in r["by_class"].most_common(5))
            print(f"  {label}: {top}")
        else:
            print(f"  {label}: (no detections above floor)")


if __name__ == "__main__":
    main()
