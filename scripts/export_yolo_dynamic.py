"""export_yolo_dynamic — export a YOLOv8n ONNX model with dynamic input axes.

STATUS: US-019a (Phase 6B.171, 2026-09-09)
INPUTS:  models/yolov8n.pt (local, downloaded by ultralytics) or auto-download
OUTPUTS: models/yolov8n.onnx  (dynamic-axes ONNX, overwrites canonical path)
         models/yolov8n_static_640.onnx (preserved static fallback)

USAGE:
    python3 scripts/export_yolo_dynamic.py

VERIFICATION (runs automatically at end):
    Loads the exported ONNX with onnxruntime and runs inference on three
    dummy tensors at multiples-of-32 sizes. Asserts each succeeds.

CONSTRAINT (YOLOv8 export limitation, discovered 2026-09-09):
    YOLOv8's ONNX export has an upsample-nearest -> Concat chain with an
    off-by-one dimension mismatch for inputs where H or W is not a multiple
    of 32. The stride-16 feature map and the 2x upsample from stride-32
    produce slightly different spatial sizes (off by 1) when input isn't
    aligned to 32. This is structural — every export variant tested has it.

    SOLUTION: pad inputs to multiples of 32 before inference. The padding
    is small (typically <30 px on each axis) and aspect-preserving. The
    model is "dynamic" in that it accepts any (h, w) where h%32==0 and
    w%32==0; the caller pads as needed.

    US-019b's "drop _letterbox()" task is replaced by "replace _letterbox()
    with pad-to-multiple-of-32" — same dynamic-model intent, but the
    model genuinely requires this alignment.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO_ROOT / "models" / "yolov8n.onnx"
STATIC_FALLBACK = REPO_ROOT / "models" / "yolov8n_static_640.onnx"
PT_SOURCES = [
    Path.home() / "yolov8n.pt",
    Path.home() / "Downloads" / "yolov8n.pt",
    Path("/tmp/yolov8n.pt"),
]


# ---------------------------------------------------------------------------
# Padding helper (US-019b imports this)
# ---------------------------------------------------------------------------

def pad_to_multiple_of_32(h: int, w: int) -> tuple[int, int]:
    """Return (h_padded, w_padded) — smallest size >= (h, w) that is a multiple of 32.

    Used by infra/quick_classifier.py to pad crops before YOLOv8 inference.
    The YOLOv8 dynamic-axes ONNX export accepts any (h, w) where h%32==0
    AND w%32==0; off-by-one Concat mismatches appear otherwise. Zero-padding
    is performed by the caller (e.g. numpy.pad on the preprocessed tensor).

    Examples:
        pad_to_multiple_of_32(265, 112) -> (288, 128)
        pad_to_multiple_of_32(720, 1280) -> (736, 1280)
        pad_to_multiple_of_32(1080, 1920) -> (1088, 1920)
    """
    return (((h + 31) // 32) * 32, ((w + 31) // 32) * 32)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_dynamic_onnx() -> None:
    """Export YOLOv8n as dynamic-axes ONNX.

    The export produces a model with dynamic input shape ['batch', 3, 'height', 'width'].
    At inference time, callers must pad input to multiples of 32 (see module docstring).
    """
    print("[1/3] Finding or downloading yolov8n.pt ...")

    # Locate .pt
    pt_path = None
    for p in PT_SOURCES:
        if p.is_file():
            pt_path = p
            break

    if pt_path is None:
        print("  No local .pt found — downloading via ultralytics ...")
        from ultralytics import YOLO
        # YOLO() downloads to CWD if not in cache; check CWD first
        pt_path = REPO_ROOT / "yolov8n.pt"
        if pt_path.is_file():
            print(f"  Downloaded to CWD: {pt_path}")
        else:
            model = YOLO("yolov8n.pt")  # downloads, returns path
            if pt_path.is_file():
                print(f"  Downloaded to CWD: {pt_path}")
            else:
                # Fallback: check cache directory
                cache_dir = Path.home() / ".cache" / "ultralytics"
                pt_path = None
                for p in cache_dir.rglob("yolov8n.pt"):
                    if p.is_file():
                        pt_path = p
                        break
                if pt_path is None:
                    print("ERROR: ultralytics download did not produce yolov8n.pt", file=sys.stderr)
                    sys.exit(1)
                print(f"  Downloaded to cache: {pt_path}")
    else:
        print(f"  Found local: {pt_path}")

    print("[2/3] Exporting dynamic ONNX (imgsz=640, simplify=True) ...")
    from ultralytics import YOLO
    model = YOLO(str(pt_path))
    model.export(
        format="onnx",
        dynamic=True,
        imgsz=640,
        simplify=True,
        opset=18,
    )
    # Ultralytics writes to the same directory as .pt, named yolov8n.onnx
    exported = pt_path.with_suffix(".onnx")
    if not exported.is_file():
        # Fallback: ultralytics may have placed it next to .pt in cache
        cache_dir = Path.home() / ".cache" / "ultralytics"
        for p in cache_dir.rglob("yolov8n.onnx"):
            if p.is_file():
                exported = p
                break
    if not exported.is_file():
        print("ERROR: export did not produce yolov8n.onnx", file=sys.stderr)
        sys.exit(1)
    print(f"  Exported: {exported}")

    # Preserve static fallback
    if MODEL_PATH.is_file():
        print(f"[3/3] Preserving static fallback: {STATIC_FALLBACK}")
        shutil.copy2(MODEL_PATH, STATIC_FALLBACK)

    # Copy dynamic to canonical path
    shutil.copy2(exported, MODEL_PATH)
    print(f"  -> {MODEL_PATH}")
    print("Export complete.")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_dynamic_model() -> None:
    """Run inference on dummy tensors at multiples-of-32 sizes.

    The exported model requires input (h, w) with h%32==0 and w%32==0.
    Anchor counts at each size (computed from the model's output shape formula):
        640x640   -> 8400   anchors
        1280x736  -> 19320  anchors (padded from 1280x720)
        1920x1088 -> 42840  anchors (padded from 1920x1080)

    For inputs that are NOT multiples of 32, the model fails with a Concat
    node dimension mismatch. The caller is responsible for padding to multiples
    of 32 before calling the model.
    """
    print("Verifying dynamic model at multiples-of-32 sizes ...")
    expected_counts = [
        ((640, 640), 8400),
        ((1280, 736), 19320),    # 1280x720 native, padded 16px on width
        ((1920, 1088), 42840),   # 1920x1080 native, padded 8px on height
    ]

    # Load the dynamic model
    sess = ort.InferenceSession(str(MODEL_PATH))
    input_name = sess.get_inputs()[0].name

    for (h, w), expected_anchors in expected_counts:
        dummy = np.random.rand(1, 3, h, w).astype(np.float32)
        result = sess.run(None, {input_name: dummy})
        arr = np.asarray(result[0])  # type: ignore[arg-type]
        # Output shape: (1, 84, N) — check last dim
        anchor_count = arr.shape[-1]
        status = "OK" if anchor_count == expected_anchors else f"MISMATCH (got {anchor_count})"
        print(f"  {h:>4}x{w:<4} -> anchors={anchor_count} (expected {expected_anchors}) [{status}]")
        if anchor_count != expected_anchors:
            print(f"ERROR: anchor count mismatch at {h}x{w}", file=sys.stderr)
            sys.exit(1)

    # Also verify that non-multiples-of-32 FAIL — documenting the constraint.
    print("Verifying non-multiples-of-32 sizes FAIL (constraint documentation) ...")
    bad_sizes = [(1920, 1080), (1280, 720)]
    for h, w in bad_sizes:
        dummy = np.random.rand(1, 3, h, w).astype(np.float32)
        try:
            result = sess.run(None, {input_name: dummy})
            print(f"  WARNING: {h}x{w} unexpectedly succeeded", file=sys.stderr)
        except Exception as e:
            err_msg = str(e).split('\n')[0][:80]
            print(f"  {h}x{w} -> FAILS as expected ({err_msg})")

    print("All verification checks passed.")


if __name__ == "__main__":
    export_dynamic_onnx()
    verify_dynamic_model()
