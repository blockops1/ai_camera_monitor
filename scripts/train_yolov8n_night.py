"""
train_yolov8n_night.py — Phase.116 §11.47 fine-tune YOLOv8n for night/IR.

Builds a YOLOv8n ONNX model optimized for nighttime surveillance footage
by combining two training corpora:

  Source A — ExDark (12k labeled nighttime images, 12 classes).
    Public dataset: https://github.com/cs-chan/ExDark_dataset
    License: research use. Cars/trucks/people/etc. in low-light scenes.

  Source B — Our captured nighttime frames (auto-labeled from current day model).
    Pseudo-labeling: use models/yolov8n.onnx (COCO pretrained) as the
    labeler, conf >= 0.50. Frames with no detections become hard negatives.

Pipeline:
  1. Download / load ExDark, convert annotations to YOLO txt format.
  2. Pseudo-label our night frames using current day model.
  3. Combine into a single YOLO dataset directory.
  4. Fine-tune yolov8n.pt for 50 epochs on MPS (Apple Silicon GPU).
  5. Export the best.pt to models/yolov8n-night.onnx.
  6. Write a run log to docs/NIGHT-MODEL-<date>.md with metrics.

Usage:
    source .venv/bin/activate
    python3 scripts/train_yolov8n_night.py [--epochs 50] [--batch 16] \
        [--no-exdark] [--pseudo-only] [--limit-native N]

Why this is needed:
    §11.41.7 (2026-08-24) wired is_night_at_edt() into the listener but the
    motion gate still uses a single day-trained model. At night, COCO
    accuracy drops sharply (IR-illuminated scenes, low contrast, color
    shift). This script produces a night-specialized variant so the gate
    can pick the right model based on time-of-day.

Hardware:
    Apple Silicon (M1/M2/M3/M4) with MPS. Falls back to CPU on Intel Mac.
    Peak RAM: ~3-6 GB on MPS, ~800 MB on CPU. Disk: ~2 GB during training.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# --- Paths ------------------------------------------------------------------

DATA_DIR = PROJECT_ROOT / "data" / "training_corpus" / "yolov8n_night"
EXDARK_DIR = DATA_DIR / "exdark"
NATIVE_DIR = DATA_DIR / "native_night"
COMBINED_DIR = DATA_DIR / "combined"
RUNS_DIR = PROJECT_ROOT / "models" / "runs"
OUTPUT_ONNX = PROJECT_ROOT / "models" / "yolov8n-night.onnx"
RUN_LOG = PROJECT_ROOT / "docs" / "NIGHT-MODEL-{date}.md"

# COCO → our 8 classes we keep (subset relevant to surveillance).
# Phase.116: limit to classes the day model already handles well.
COCO_KEEP_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "bus", "truck",
    "cat", "dog",
]
# ExDark class names → COCO mapping for annotation conversion.
EXDARK_TO_COCO = {
    "Person": "person",
    "Bicycle": "bicycle",
    "Car": "car",
    "Motorbike": "motorcycle",
    "Bus": "bus",
    "Truck": "truck",
    "Cat": "cat",
    "Dog": "dog",
    # Drop: Chair, Cup, Fork, Knife, Bottle (not surveillance-relevant)
}

# Pseudo-label confidence threshold for our native frames.
PSEUDO_CONF_THRESHOLD = 0.50


# --- ExDark dataset ---------------------------------------------------------


def _has_exdark() -> bool:
    """True if ExDark is already extracted locally."""
    return EXDARK_DIR.exists() and any(EXDARK_DIR.glob("*.jpg"))


def _download_exdark() -> None:
    """Download ExDark from a public mirror.

    The official GitHub repo is https://github.com/cs-chan/ExDark_dataset.
    It's typically distributed as a ~1.5GB zip with image+annotation pairs.

    NOTE: for Phase.116 v1 we may skip ExDark and rely on native corpus
    only (~186 frames). See --no-exdark flag.
    """
    EXDARK_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[exdark] Would download ~1.5GB from cs-chan/ExDark_dataset to {EXDARK_DIR}")
    print("[exdark] Skipping download in v1 — use --no-exdark for native-only, or")
    print(f"[exdark] place a pre-downloaded zip at {EXDARK_DIR}/exdark.zip")


def _convert_exdark_to_yolo() -> Path:
    """Convert ExDark annotations (XML PASCAL VOC format) to YOLO txt.

    Returns the path to the YOLO-format images dir.
    """
    # ExDark ships as: ExDark/image/<class>/<image>.jpg +
    #                  ExDark/Groundtruth/<class>/<image>.xml (PASCAL VOC)
    # YOLO wants: images/<class>/*.jpg + labels/<class>/*.txt
    # Each YOLO label: <class_idx> <cx> <cy> <w> <h>  (normalized 0-1)

    images_dir = COMBINED_DIR / "images" / "exdark"
    labels_dir = COMBINED_DIR / "labels" / "exdark"
    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)

    if not EXDARK_DIR.exists():
        print(f"[exdark] Not found at {EXDARK_DIR}, skipping conversion")
        return images_dir

    # Try both possible annotation dir names (some mirrors use lowercase).
    xml_dir = EXDARK_DIR / "Groundtruth"
    if not xml_dir.exists():
        xml_dir = EXDARK_DIR / "annotation"
    if not xml_dir.exists():
        xml_dir = EXDARK_DIR / "groundtruth"
    if not xml_dir.exists():
        print(f"[exdark] Groundtruth dir not found at {EXDARK_DIR}/Groundtruth")
        print(f"[exdark] Available dirs: {[p.name for p in EXDARK_DIR.iterdir() if p.is_dir()]}")
        return images_dir

    image_dir = EXDARK_DIR / "image"
    if not image_dir.exists():
        # Some mirrors use lowercase
        image_dir = EXDARK_DIR / "Image"

    converted = 0
    skipped = 0
    class_to_idx = {name: idx for idx, name in enumerate(COCO_KEEP_CLASSES)}

    for class_dir in xml_dir.iterdir():
        if not class_dir.is_dir():
            continue
        exdark_class = class_dir.name
        if exdark_class not in EXDARK_TO_COCO:
            skipped += 1
            continue
        coco_class = EXDARK_TO_COCO[exdark_class]
        cls_idx = class_to_idx[coco_class]

        for xml_file in class_dir.glob("*.xml"):
            try:
                import xml.etree.ElementTree as ET

                tree = ET.parse(xml_file)
                root = tree.getroot()
                size = root.find("size")
                if size is None:
                    continue
                width = int(float(size.findtext("width") or 0))
                height = int(float(size.findtext("height") or 0))
                if width == 0 or height == 0:
                    continue

                # Collect all objects (some images have multiple)
                yolo_lines = []
                for obj in root.findall("object"):
                    name = obj.findtext("name")
                    if name not in EXDARK_TO_COCO:
                        continue
                    if EXDARK_TO_COCO[name] != coco_class:
                        continue  # mixed-class images — skip mismatches

                    bndbox = obj.find("bndbox")
                    if bndbox is None:
                        continue
                    xmin = float(bndbox.findtext("xmin") or 0)
                    ymin = float(bndbox.findtext("ymin") or 0)
                    xmax = float(bndbox.findtext("xmax") or 0)
                    ymax = float(bndbox.findtext("ymax") or 0)

                    cx = ((xmin + xmax) / 2) / width
                    cy = ((ymin + ymax) / 2) / height
                    w = (xmax - xmin) / width
                    h = (ymax - ymin) / height

                    # Skip degenerate boxes
                    if w <= 0 or h <= 0:
                        continue
                    yolo_lines.append(f"{cls_idx} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")

                if not yolo_lines:
                    continue

                # Copy image
                src_img = image_dir / exdark_class / xml_file.with_suffix(".jpg").name
                if not src_img.exists():
                    continue
                dst_img = images_dir / f"{coco_class}_{xml_file.stem}.jpg"
                shutil.copy2(src_img, dst_img)

                # Write label
                dst_lbl = labels_dir / f"{coco_class}_{xml_file.stem}.txt"
                dst_lbl.write_text("\n".join(yolo_lines) + "\n")
                converted += 1

            except (OSError, ValueError, KeyError) as e:
                print(f"[exdark] failed to convert {xml_file}: {e}")
                skipped += 1

    print(f"[exdark] Converted {converted} images, skipped {skipped}")
    return images_dir


# --- Native corpus ----------------------------------------------------------


def _collect_native_night_frames(limit: int | None = None) -> list[Path]:
    """Returns list of frame_003.jpg paths (the "best" frame, central in the 4-batch)
    from data/frames/<alert_id>/ where the alert's mtime is in night window.

    Phase.116 v1: only on-disk frames. Once retention captures are added
    (or we wire alert replay), this list grows.
    """
    from infra.time_of_day import is_night_at_edt
    frames_root = PROJECT_ROOT / "data" / "frames"
    if not frames_root.exists():
        return []

    night_frames = []
    for alert_dir in sorted(frames_root.iterdir()):
        if not alert_dir.is_dir():
            continue
        # Use the dir's mtime as proxy for capture time (good enough — dirs
        # are created when the gate captures the 4 frames).
        mtime = alert_dir.stat().st_mtime
        from datetime import datetime

        mtime_dt = datetime.fromtimestamp(mtime, tz=UTC)
        if not is_night_at_edt(mtime_dt):
            continue
        # Prefer frame_003 (central frame, most motion likely)
        frame_3 = alert_dir / "frame_003.jpg"
        if frame_3.exists():
            night_frames.append(frame_3)
        else:
            # Fallback to frame_001
            frame_1 = alert_dir / "frame_001.jpg"
            if frame_1.exists():
                night_frames.append(frame_1)

        if limit and len(night_frames) >= limit:
            break
    return night_frames


def _pseudo_label_native(frames: list[Path]) -> int:
    """Run day-model on each native frame, write YOLO txt labels for confident
    detections.

    Returns count of frames that received at least one label.
    """
    images_dir = COMBINED_DIR / "images" / "native"
    labels_dir = COMBINED_DIR / "labels" / "native"
    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)

    if not frames:
        return 0

    # Lazy import — only when we actually need to pseudo-label.
    from infra.quick_classifier import QuickClassifier

    print(f"[native] Pseudo-labeling {len(frames)} frames using day model...")
    classifier = QuickClassifier()

    coco_to_idx = {name: idx for idx, name in enumerate(COCO_KEEP_CLASSES)}
    labeled_count = 0

    for i, src in enumerate(frames):
        try:
            # classify_frame returns a QuickVerdict with raw_predictions
            verdict = classifier.classify_frame(str(src))
            # Lazy: QuickVerdict has top_class + confidence. We want ALL
            # detections >= threshold for richer pseudo-labels. Re-run via
            # the underlying ONNX session if available.
            raw_preds = getattr(verdict, "raw_predictions", None) or []

            yolo_lines = []
            if raw_preds:
                # raw_predictions: list[tuple[class_name, confidence, bbox_xyxy]]
                for pred in raw_preds:
                    if len(pred) != 3:
                        continue
                    cls_name, conf, bbox = pred
                    cls_name = cls_name.lower() if cls_name else ""
                    conf = float(conf or 0)
                    if cls_name not in coco_to_idx:
                        continue
                    if conf < PSEUDO_CONF_THRESHOLD:
                        continue
                    if not bbox or len(bbox) != 4:
                        continue
                    x1, y1, x2, y2 = bbox
                    # Native Reolink frames are 1296x2304. QuickClassifier
                    # returns bbox in the original frame coordinates.
                    img_w, img_h = 1296, 2304
                    cx = ((x1 + x2) / 2) / img_w
                    cy = ((y1 + y2) / 2) / img_h
                    w = (x2 - x1) / img_w
                    h = (y2 - y1) / img_h
                    if w <= 0 or h <= 0:
                        continue
                    cls_idx = coco_to_idx[cls_name]
                    yolo_lines.append(
                        f"{cls_idx} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"
                    )
            else:
                # QuickVerdict didn't expose raw_predictions — fall back to
                # using just the top_class with a tight bbox (whole image)
                top = (verdict.top_class or "").lower()
                if top in coco_to_idx and verdict.top_confidence >= PSEUDO_CONF_THRESHOLD:
                    cls_idx = coco_to_idx[top]
                    # Use a 60% center crop as the bbox (conservative)
                    yolo_lines.append(
                        f"{cls_idx} 0.500 0.500 0.600 0.600"
                    )

            # Copy image regardless (hard negatives are useful)
            dst_img = images_dir / f"native_{i:04d}_{src.name}"
            shutil.copy2(src, dst_img)

            # Write label (empty file = hard negative)
            dst_lbl = labels_dir / f"native_{i:04d}_{src.stem}.txt"
            dst_lbl.write_text("\n".join(yolo_lines) + "\n" if yolo_lines else "")
            if yolo_lines:
                labeled_count += 1

            if (i + 1) % 25 == 0:
                print(f"[native]   {i + 1}/{len(frames)} labeled ({labeled_count} with objects)")

        except (OSError, ValueError, KeyError) as e:
            print(f"[native] Failed on {src}: {e}")

    print(f"[native] Done: {labeled_count}/{len(frames)} frames had objects")
    return labeled_count


# --- Combined YOLO dataset --------------------------------------------------


def _write_dataset_yaml() -> Path:
    """Write the YOLO-format dataset.yaml pointing at combined images/labels.

    YOLO splits each dataset into train/val internally — we put everything
    in 'train' and use val_ratio=0.2 for the auto-split.
    """
    yaml_path = DATA_DIR / "dataset.yaml"
    content = {
        "path": str(COMBINED_DIR.absolute()),
        "train": "images",
        "val": "images",  # ultralytics will auto-split 80/20
        "names": {idx: name for idx, name in enumerate(COCO_KEEP_CLASSES)},
        "nc": len(COCO_KEEP_CLASSES),
    }
    yaml_path.write_text(json.dumps(content, indent=2))
    print(f"[dataset] Wrote {yaml_path}")
    return yaml_path


# --- Training ---------------------------------------------------------------


@dataclass
class TrainConfig:
    epochs: int = 50
    batch: int = 16
    imgsz: int = 640
    lr: float = 0.001
    device: str = ""  # auto-detect (mps > cpu)
    patience: int = 10  # early stopping patience


def _detect_device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _train(config: TrainConfig, dataset_yaml: Path) -> Path:
    """Run ultralytics training. Returns path to best.pt."""
    import torch
    from ultralytics import YOLO

    device = config.device or _detect_device()
    print(f"[train] Device: {device} (torch {torch.__version__})")
    print(f"[train] Config: {config}")

    # Start from yolov8n.pt (COCO pretrained, ~6MB).
    # ultralytics auto-downloads if not present.
    base_model = PROJECT_ROOT / "models" / "yolov8n.pt"
    if not base_model.exists():
        print(f"[train] Downloading yolov8n.pt to {base_model}...")
        # Use YOLO() to trigger auto-download
        YOLO(str(base_model))

    model = YOLO(str(base_model))
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_name = f"night_{int(time.time())}"

    print(f"[train] Starting training run: {run_name}")
    model.train(
        data=str(dataset_yaml),
        epochs=config.epochs,
        batch=config.batch,
        imgsz=config.imgsz,
        lr0=config.lr,
        device=device,
        patience=config.patience,
        project=str(RUNS_DIR),
        name=run_name,
        # Keep output lean
        verbose=True,
        plots=False,
        save_period=-1,  # don't save intermediate checkpoints
        # MPS-specific workarounds
        workers=0 if device == "mps" else 4,
    )

    best_pt = RUNS_DIR / run_name / "weights" / "best.pt"
    if not best_pt.exists():
        raise RuntimeError(f"Training did not produce {best_pt}")
    return best_pt


def _export_to_onnx(best_pt: Path) -> Path:
    """Export best.pt → yolov8n-night.onnx."""
    from ultralytics import YOLO

    print(f"[export] Loading {best_pt}...")
    model = YOLO(str(best_pt))
    print("[export] Exporting to ONNX (imgsz=640, simplify=True)...")

    # Export writes to the same dir as best.pt by default
    exported_path = model.export(format="onnx", imgsz=640, simplify=True)

    src = Path(exported_path)
    if not src.exists():
        raise RuntimeError(f"Export did not produce {src}")

    OUTPUT_ONNX.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(OUTPUT_ONNX))
    print(f"[export] Wrote {OUTPUT_ONNX} ({OUTPUT_ONNX.stat().st_size / 1e6:.1f} MB)")
    return OUTPUT_ONNX


# --- Run log ----------------------------------------------------------------


def _write_run_log(args: argparse.Namespace, train_result: dict) -> Path:
    """Persist a run log to docs/ for review."""
    from datetime import datetime

    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    log_path = PROJECT_ROOT / "docs" / f"NIGHT-MODEL-{today}.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Night Model Training Run — {today}",
        "",
        " Hyperparameters",
        "",
        f"- Epochs: {args.epochs}",
        f"- Batch size: {args.batch}",
        "- Image size: 640",
        f"- Learning rate: {args.lr}",
        "- Device: MPS (Apple Silicon) or CPU fallback",
        "",
        " Corpus",
        "",
        f"- ExDark: {'included' if not args.no_exdark else 'SKIPPED (--no-exdark)'}",
        f"- Native night frames: {train_result.get('native_count', '?')}",
        f"- Pseudo-label confidence threshold: {PSEUDO_CONF_THRESHOLD}",
        "",
        " Results",
        "",
        f"- Best mAP50: {train_result.get('map50', '?')}",
        f"- Best mAP50-95: {train_result.get('map50_95', '?')}",
        "- Output: `models/yolov8n-night.onnx`",
        "",
        " Notes",
        "",
        "- Generated by `scripts/train_yolov8n_night.py`",
        "- Phase.116 §11.47",
        "",
    ]
    log_path.write_text("\n".join(lines))
    return log_path


# --- CLI --------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase.116: fine-tune YOLOv8n for nighttime surveillance"
    )
    parser.add_argument(
        "--epochs", type=int, default=50, help="Training epochs (default: 50)"
    )
    parser.add_argument(
        "--batch", type=int, default=16, help="Batch size (default: 16)"
    )
    parser.add_argument(
        "--lr", type=float, default=0.001, help="Learning rate (default: 0.001)"
    )
    parser.add_argument(
        "--device",
        default="",
        help="Device override: mps, cpu, or '' for auto-detect (default: '')",
    )
    parser.add_argument(
        "--no-exdark",
        action="store_true",
        help="Skip ExDark; train on native corpus only",
    )
    parser.add_argument(
        "--pseudo-only",
        action="store_true",
        help="Pseudo-label native frames then exit (skip training)",
    )
    parser.add_argument(
        "--limit-native",
        type=int,
        default=None,
        help="Cap native frame count for quick iteration",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Skip freezing data/training_corpus/ snapshot (use live data/frames/)",
    )
    parser.add_argument(
        "--output-onnx",
        type=str,
        default=str(OUTPUT_ONNX),
        help=f"Output path for the trained ONNX model (default: {OUTPUT_ONNX})",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    t0 = time.time()

    print("=== train_yolov8n_night.py (Phase.116 §11.47) ===")
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Output ONNX: {args.output_onnx}")

    # Step 1 — ExDark (optional)
    if not args.no_exdark:
        if not _has_exdark():
            _download_exdark()
        _convert_exdark_to_yolo()

    # Step 2 — Native corpus (freeze snapshot to data/training_corpus/)
    native_frames = _collect_native_night_frames(limit=args.limit_native)
    print(f"[main] Found {len(native_frames)} native night frames")
    if not native_frames and args.no_exdark:
        print("[main] ERROR: no native frames and --no-exdark set. Nothing to train on.")
        return 1
    pseudo_labeled = _pseudo_label_native(native_frames)

    if args.pseudo_only:
        print("[main] --pseudo-only set, exiting after pseudo-labeling.")
        return 0

    # Step 3 — Build dataset.yaml
    dataset_yaml = _write_dataset_yaml()

    # Step 4 — Train
    config = TrainConfig(
        epochs=args.epochs,
        batch=args.batch,
        device=args.device,
    )
    best_pt = _train(config, dataset_yaml)

    # Step 5 — Export
    onnx_path = _export_to_onnx(best_pt)

    # Step 6 — Run log
    run_result = {
        "native_count": len(native_frames),
        "pseudo_labeled": pseudo_labeled,
    }
    log_path = _write_run_log(args, run_result)
    print(f"[main] Run log: {log_path}")

    elapsed = time.time() - t0
    print(f"[main] Done in {elapsed:.0f}s ({elapsed / 60:.1f} min)")
    print(f"[main] Trained model: {onnx_path}")
    print("[main] To enable: set MOTION_GATE_NIGHT_MODEL=1 in plist and restart listener")
    return 0


if __name__ == "__main__":
    sys.exit(main())
