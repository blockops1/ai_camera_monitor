"""
quick_classifier.py — Fast frame-level object classifier for the motion gate.

STATUS: provisional (gate-only, not used for final alert decisions)
THREAD SAFETY: single-threaded (CoreML EP doesn't benefit from concurrency)

INPUTS:
    - frame: str (path to JPEG) or PIL.Image
    - timestamp: datetime | None — used for night/day gate (Phase.116 §11.47).
        When None and frame is a path, falls back to file mtime.
    - Env vars:
        MOTION_GATE_NIGHT_CONF_FLOOR (float, default 0.40)
        MOTION_GATE_NIGHT_BRIGHTNESS_RATIO (float, default 1.5)
        MOTION_GATE_NIGHT_SUPPRESS_ENABLED (0|1, default 0) — feature flag.

OUTPUTS:
    QuickVerdict dataclass with: top_class, top_confidence, decision,
    n_detections, raw_predictions. decision ∈ {"suppress", "pass_with_hint", "pass"}.

PUBLIC API:
    QuickClassifier(model_path, confidence_threshold, keep_classes, raw_score_threshold)
        Load YOLOv8n ONNX via onnxruntime + CoreML EP.
    classify_frame(frame, timestamp=None) -> QuickVerdict
        Run inference on a single frame. Returns gate verdict.
    classify_batch(frame_paths, timestamps=None) -> list[QuickVerdict]
        Sequential batch classification.

DOES NOT DO:
    - Run Qwen3-VL or any LLM
    - Detect motion (uses infra/motion_detector.py output)
    - Train or fine-tune the model (COCO weights only)

CALLED BY:
    - listener/motion_gate_pipeline.py: classify_frames (primary)
    - listener/_gate_aware_capture.py: QuickClassifier() factory
    - scripts/probe_quick_classifier.py
    - scripts/probe_night_reflections.py (Phase.116)

CALLS INTO:
    - onnxruntime: model inference (CoreML EP on Apple Silicon)
    - PIL: image load + letterbox resize
    - numpy: tensor prep + brightness stats
    - cv2: NMS for duplicate detection removal
    - infra/time_of_day: is_night_at_edt() for night/day signal

RELATED:
    - infra/motion_detector.py: provides the bbox / crop this classifies
    - infra/vision_analyzer.py: receives the hint when gate passes
    - models/yolov8n.onnx: the pretrained model (COCO, ~12MB)
    - PLAN.md §11.41.7 + §11.47 — night-mode gate history
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import cv2  # only used for NMS in _postprocess
import numpy as np
import onnxruntime as ort
from PIL import Image

log = logging.getLogger("quick_classifier")

# Phase.116 §11.47 — night suppression env vars.
# Default: feature off (no behavior change). Set MOTION_GATE_NIGHT_SUPPRESS_ENABLED=1
# after validation. All three are env-tunable so we can sweep values without
# code changes.
NIGHT_SUPPRESS_ENABLED = os.environ.get("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0") == "1"
NIGHT_CONF_FLOOR = float(os.environ.get("MOTION_GATE_NIGHT_CONF_FLOOR", "0.40"))
NIGHT_BRIGHTNESS_RATIO = float(
    os.environ.get("MOTION_GATE_NIGHT_BRIGHTNESS_RATIO", "1.5")
)

# COCO classes that are physically implausible for outdoor night surveillance.
# At night with high IR illumination (brightness_ratio > 1.5), the day model
# frequently fires on indoor objects / wrong classes. These are always noise.
# yolov8m shifts FPs into a higher conf band than yolov8n, so a conf-only
# heuristic misses them — class-based filtering catches them regardless of model.
NIGHT_IMPLAUSIBLE_CLASSES = frozenset({
    # Indoor furniture / fixtures
    "chair", "couch", "bed", "dining table", "toilet", "potted plant",
    # Indoor items
    "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "vase", "clock", "scissors",
    # Tableware / food
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl",
    "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake",
    # Wrong-domain vehicles (rural surveillance property, no tracks/planes)
    "train", "airplane", "boat",
    # Sports / leisure items not relevant outdoors
    "frisbee", "skis", "snowboard", "sports ball", "kite", "skateboard",
    "surfboard", "tennis racket", "baseball bat", "baseball glove",
})

if NIGHT_SUPPRESS_ENABLED:
    log.info(
        f"quick_classifier: night suppression ENABLED "
        f"(conf_floor={NIGHT_CONF_FLOOR}, brightness_ratio={NIGHT_BRIGHTNESS_RATIO}, "
        f"implausible_classes={len(NIGHT_IMPLAUSIBLE_CLASSES)})"
    )

# COCO 80 classes (YOLOv8n output order — class index 0..79)
# Full list at https://github.com/ultralytics/ultralytics/blob/main/ultralytics/cfg/datasets/coco.yaml
COCO_NAMES: list[str] = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana",
    "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza",
    "donut", "cake", "chair", "couch", "potted plant", "bed", "dining table",
    "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock",
    "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
]

# Classes we care about for the surveillance use case. Everything else is
# treated as "noise / not a real alert" for the gate decision.
KEEP_CLASSES_DEFAULT: frozenset[str] = frozenset({
    "person", "bicycle", "car", "motorcycle", "bus", "truck",
    "cat", "dog", "horse", "sheep", "cow", "bear", "bird",
})

# Default confidence threshold below which we suppress.
# Tuned empirically on the OFG/OFS corpus — see PLAN.md §11.NN (planned).
# 0.40 means: if the model isn't 40%+ sure it's one of the COCO objects
# (after NMS), suppress. Shadows and flares typically score 0.10-0.30.
DEFAULT_CONFIDENCE_THRESHOLD = 0.40

# YOLOv8n ONNX expects 640x640 RGB, normalized to 0..1, NCHW float32.
MODEL_INPUT_SIZE = 640

# Default model path (relative to project root). Download with:
#   curl -sSL -o models/yolov8n.onnx \
#     https://github.com/yoobright/yolo-onnx/raw/master/yolov8n.onnx
DEFAULT_MODEL_PATH = "models/yolov8n.onnx"


@dataclass
class QuickVerdict:
    """Result of classify_frame().

    decision: "suppress" (gate says skip Qwen, log + drop)
              "pass_with_hint" (gate says real object, pass hint to Qwen)
              "pass" (uncertain, let Qwen decide without hint)
    top_class: best COCO class name (str)
    top_confidence: confidence of top prediction (float 0..1)
    n_detections: total number of detections above raw_threshold (int)
    raw_predictions: list of (class_name, confidence, bbox_xyxy) tuples
    reason: Phase.116 — human-readable explanation of WHY the decision is
            what it is. Used by _route_decision to surface a meaningful reason
            in the operator log (motion_gate: suppressed (...)). Examples:
              - "no_object_detected" — yolov8n found nothing
              - "night_low_confidence" — night + conf < MOTION_GATE_NIGHT_CONF_FLOOR
              - "night_implausible_class" — night + class in NIGHT_IMPLAUSIBLE_CLASSES
              - "class_below_threshold" — day gate per-class threshold not met
              - None — pass / pass_with_hint (no override reason needed)
    """

    top_class: str
    top_confidence: float
    decision: str
    n_detections: int = 0
    raw_predictions: list[tuple[str, float, tuple[int, int, int, int]]] = field(default_factory=list)
    reason: str | None = None


class QuickClassifier:
    """ONNX Runtime wrapper for YOLOv8n with CoreML acceleration on Apple Silicon.

    Loads the model once at construction. classify_frame() / classify_batch()
    are stateless — call them as many times as needed.

    Threading: CoreML execution provider doesn't benefit from concurrent
    inference calls. Use sequential classify_batch() rather than threading.
    """

    def __init__(
        self,
        model_path: str = DEFAULT_MODEL_PATH,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        keep_classes: frozenset[str] | None = None,
        raw_score_threshold: float = 0.25,
    ) -> None:
        """Load the ONNX model and configure gate parameters.

        Args:
            model_path: path to yolov8n.onnx file
            confidence_threshold: gate threshold for suppression (0..1)
            keep_classes: COCO classes that count as "real" detections
            raw_score_threshold: minimum score to keep a raw YOLO detection
                before NMS (lower = more candidates, slower; 0.25 is the
                Ultralytics default)
        """
        if not Path(model_path).is_file():
            raise FileNotFoundError(
                f"Model file not found: {model_path}. "
                f"Download with: curl -sSL -o {model_path} "
                f"https://github.com/yoobright/yolo-onnx/raw/master/yolov8n.onnx"
            )

        # Use CoreML execution provider if available (Apple Silicon GPU/ANE),
        # fall back to CPU otherwise.
        providers = ort.get_available_providers()
        preferred = [
            p for p in ("CoreMLExecutionProvider", "CPUExecutionProvider")
            if p in providers
        ]
        log.info(f"quick_classifier: loading {model_path} with providers={preferred}")

        # CoreML provider has CPU+ANE+GPU sub-options; defaults are fine for us.
        self.session = ort.InferenceSession(model_path, providers=preferred)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

        self.confidence_threshold = confidence_threshold
        self.keep_classes = keep_classes or KEEP_CLASSES_DEFAULT
        self.raw_score_threshold = raw_score_threshold

        log.info(
            f"quick_classifier: ready "
            f"(threshold={confidence_threshold}, keep_classes={len(self.keep_classes)})"
        )

    def classify_frame(
        self, frame: str | Image.Image, timestamp: datetime | None = None
    ) -> QuickVerdict:
        """Run inference on a single frame and return the gate verdict.

        Args:
          frame: either a path to a JPEG file, or an in-memory PIL.Image.
            Phase.115 (§11.46.6): the gate hands in PIL crops directly
            to avoid a temp-file round-trip when GATE_KEEP_DISK_ARTIFACTS=false.
          timestamp: optional datetime used for night-mode suppression
            (Phase.116 §11.47). When None and frame is a path, falls back
            to the file's mtime. When None and frame is a PIL.Image, the
            day/night signal is unknown → suppression skipped.

        Returns:
          QuickVerdict with decision ∈ {"suppress", "pass_with_hint", "pass"}
        """
        if isinstance(frame, Image.Image):
            img = frame.convert("RGB")
            frame_path = None
        else:
            frame_path = Path(frame)
            try:
                img = Image.open(frame).convert("RGB")
            except Exception as err:
                log.warning(f"quick_classifier: failed to load {frame}: {err}")
                return QuickVerdict(
                    top_class="error", top_confidence=0.0, decision="pass",
                    n_detections=0,
                )

        # Preprocess: letterbox to 640x640, normalize to [0,1], NCHW float32
        img_resized, scale, pad = _letterbox(img, MODEL_INPUT_SIZE)
        img_array = np.asarray(img_resized, dtype=np.float32) / 255.0
        img_array = img_array.transpose(2, 0, 1)[None]  # HWC -> CHW -> NCHW

        # Inference
        raw_output = self.session.run(
            [self.output_name], {self.input_name: img_array}
        )[0]  # shape: (1, 84, 8400) — 4 bbox + 80 class scores per anchor
        # Ensure 2D (84, 8400) for postprocess
        output_2d: np.ndarray = np.asarray(raw_output).reshape(84, -1)

        # Postprocess: NMS + scale bboxes back to original frame coords
        detections = _postprocess(
            output_2d,
            conf_threshold=self.raw_score_threshold,
            iou_threshold=0.45,
            scale=scale,
            pad=pad,
            img_size=img.size,
        )

        # Build verdict from detections
        if not detections:
            return QuickVerdict(
                top_class="none", top_confidence=0.0, decision="suppress",
                n_detections=0, reason="no_object_detected",
            )

        # Sort by confidence desc
        detections.sort(key=lambda d: d[1], reverse=True)
        top_class_id, top_conf, _top_bbox = detections[0]
        top_class = COCO_NAMES[top_class_id]

        # Gate decision
        reason: str | None = None
        if top_conf < self.confidence_threshold:
            decision = "suppress"
            reason = "class_below_threshold"
        elif top_class in self.keep_classes:
            decision = "pass_with_hint"
        else:
            decision = "pass"

        # Phase.116 §11.47 — night suppression override.
        # When enabled, and the timestamp is in the night window, AND the
        # frame's bottom-half is brighter than its top-half (the IR-illuminated
        # night signature), we suppress if EITHER:
        #   (a) top detection conf < NIGHT_CONF_FLOOR (low-confidence noise), OR
        #   (b) top class is in NIGHT_IMPLAUSIBLE_CLASSES (indoor / wrong-domain
        #       objects that can't realistically be on a rural property at night).
        # This dual condition handles both:
        #   - yolov8n: noise at conf 0.25-0.40 → caught by (a)
        #   - yolov8m: noise shifted to conf 0.40-0.50, wrong class → caught by (b)
        if (
            NIGHT_SUPPRESS_ENABLED
            and decision in ("pass_with_hint", "pass")
            and (
                top_conf < NIGHT_CONF_FLOOR
                or top_class in NIGHT_IMPLAUSIBLE_CLASSES
            )
            and _should_apply_night_suppression(img, timestamp, frame_path)
        ):
            log.debug(
                f"night suppression: class={top_class} conf={top_conf:.2f} → suppress"
            )
            decision = "suppress"
            # Phase.116 — reason plumbing. The two suppression triggers have
            # different operator meanings so we surface them separately:
            #   - night_low_confidence — yolov8n noise at night (most common case)
            #   - night_implausible_class — wrong-domain class at night (e.g. tv, laptop)
            # Without this, the operator log would lump both into the generic
            # "no_object_detected" or "class_below_threshold" reason and lose the
            # signal that the night heuristic was the suppressor.
            if top_class in NIGHT_IMPLAUSIBLE_CLASSES:
                reason = "night_implausible_class"
            else:
                reason = "night_low_confidence"

        raw = [
            (COCO_NAMES[cls], conf, bbox) for cls, conf, bbox in detections
        ]
        return QuickVerdict(
            top_class=top_class,
            top_confidence=top_conf,
            decision=decision,
            n_detections=len(detections),
            raw_predictions=raw,
            reason=reason,
        )

    def classify_batch(
        self,
        frame_paths: list[str],
        timestamps: list | None = None,
    ) -> list[QuickVerdict]:
        """Sequential batch classification. Returns one verdict per frame.

        Args:
          frame_paths: list of JPEG paths
          timestamps: optional list of datetime objects (one per frame) for
            night suppression. If shorter than frame_paths, missing entries
            fall back to file mtime.
        """
        if timestamps is None:
            timestamps = [None] * len(frame_paths)
        elif len(timestamps) < len(frame_paths):
            timestamps = list(timestamps) + [None] * (len(frame_paths) - len(timestamps))
        return [
            self.classify_frame(p, ts) for p, ts in zip(frame_paths, timestamps)
        ]


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def _letterbox(
    img: Image.Image, target_size: int, color: tuple[int, int, int] = (114, 114, 114)
) -> tuple[Image.Image, float, tuple[int, int]]:
    """Resize image to target_size x target_size with letterbox padding.

    Preserves aspect ratio. Returns (resized_image, scale, (pad_x, pad_y)).
    """
    w, h = img.size
    scale = min(target_size / w, target_size / h)
    new_w, new_h = round(w * scale), round(h * scale)
    img_resized = img.resize((new_w, new_h), Image.BILINEAR)  # type: ignore[attr-defined]

    pad_x = (target_size - new_w) // 2
    pad_y = (target_size - new_h) // 2

    # Paste onto gray canvas
    canvas = Image.new("RGB", (target_size, target_size), color)
    canvas.paste(img_resized, (pad_x, pad_y))
    return canvas, scale, (pad_x, pad_y)


# ---------------------------------------------------------------------------
# Output postprocessing (YOLOv8 ONNX format)
# ---------------------------------------------------------------------------

def _postprocess(
    output: np.ndarray,
    conf_threshold: float,
    iou_threshold: float,
    scale: float,
    pad: tuple[int, int],
    img_size: tuple[int, int],
) -> list[tuple[int, float, tuple[int, int, int, int]]]:
    """Convert YOLOv8 ONNX output to detection list.

    Args:
        output: shape (84, 8400) — 4 bbox values (cx, cy, w, h) + 80 class scores
            per anchor. The 8400 anchors come from the YOLOv8 grid at 640x640.
        conf_threshold: minimum class score to keep
        iou_threshold: NMS IoU threshold
        scale: letterbox scale factor (to un-scale bbox coords)
        pad: (pad_x, pad_y) from letterbox (to un-translate)
        img_size: original (w, h) of input frame

    Returns:
        List of (class_id, confidence, (x1, y1, x2, y2)) in original-frame coords.
    """
    # output shape: (84, 8400) -> transpose to (8400, 84)
    output = output.transpose()
    boxes = output[:, :4]      # cx, cy, w, h
    class_scores = output[:, 4:]  # 80 class probabilities

    # Best class per anchor
    class_ids = np.argmax(class_scores, axis=1)
    confidences = class_scores[np.arange(class_scores.shape[0]), class_ids]

    # Filter by confidence
    mask = confidences > conf_threshold
    boxes = boxes[mask]
    class_ids = class_ids[mask]
    confidences = confidences[mask]

    if boxes.shape[0] == 0:
        return []

    # Convert cxcywh to xyxy
    x1 = boxes[:, 0] - boxes[:, 2] / 2
    y1 = boxes[:, 1] - boxes[:, 3] / 2
    x2 = boxes[:, 0] + boxes[:, 2] / 2
    y2 = boxes[:, 1] + boxes[:, 3] / 2
    xyxy = np.stack([x1, y1, x2, y2], axis=1)

    # Undo letterbox: subtract pad, divide by scale
    pad_x, pad_y = pad
    xyxy[:, [0, 2]] -= pad_x
    xyxy[:, [1, 3]] -= pad_y
    xyxy /= scale

    # Clip to original image bounds
    img_w, img_h = img_size
    xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, img_w)
    xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, img_h)

    # NMS (using cv2.dnn.NMSBoxes — simpler than rolling our own)
    nms_boxes = [
        [int(xyxy[i, 0]), int(xyxy[i, 1]),
         int(xyxy[i, 2] - xyxy[i, 0]), int(xyxy[i, 3] - xyxy[i, 1])]
        for i in range(xyxy.shape[0])
    ]
    indices: np.ndarray = cv2.dnn.NMSBoxes(
        nms_boxes, confidences.tolist(), conf_threshold, iou_threshold
    )  # type: ignore[assignment]
    if len(indices) == 0:
        return []
    # cv2.dnn.NMSBoxes returns either np.ndarray or a sequence-of-lists
    # depending on OpenCV version; flatten covers both shapes.
    indices = np.array(indices).flatten()

    return [
        (int(class_ids[i]), float(confidences[i]),
         (int(xyxy[i, 0]), int(xyxy[i, 1]), int(xyxy[i, 2]), int(xyxy[i, 3])))
        for i in indices
    ]


# ---------------------------------------------------------------------------
# Night suppression helpers (Phase.116 §11.47)
# ---------------------------------------------------------------------------


def _brightness_ratio(img: Image.Image) -> float:
    """Compute bottom-half / top-half mean brightness on a PIL image.

    Returns a ratio > 1 when the bottom half is brighter than the top.
    For IR-illuminated night surveillance frames this is typically 1.5-3.0
    because the IR LEDs light up the foreground (ground, vehicles) more than
    the sky. Day frames usually sit at 0.8-1.2.

    Cheap: ~5ms on a 1296x2304 frame. Done on the unaltered RGB image
    BEFORE letterbox so the result is interpretable.
    """
    gray = np.asarray(img.convert("L"))
    h = gray.shape[0]
    top_half = gray[: h // 2, :]
    bottom_half = gray[h // 2 :, :]
    return float(bottom_half.mean()) / max(float(top_half.mean()), 1.0)


def _resolve_timestamp(
    timestamp: datetime | None,
    frame_path: Path | None,
) -> datetime | None:
    """Resolve the timestamp for night-suppression check.

    Priority:
      1. explicit `timestamp` arg (caller knows)
      2. file mtime (when frame_path given)
      3. None (PIL.Image, no mtime available) → suppression skipped
    """
    if timestamp is not None:
        return timestamp
    if frame_path is not None and frame_path.is_file():
        return datetime.fromtimestamp(frame_path.stat().st_mtime, tz=UTC)
    return None


def _should_apply_night_suppression(
    img: Image.Image,
    timestamp: datetime | None,
    frame_path: Path | None,
) -> bool:
    """Return True if the night-suppression override should kick in for this frame.

    Conditions (all three must be true):
      1. Resolved timestamp falls in the night window (is_night_at_edt)
      2. Frame's bottom-half is brighter than top-half (brightness_ratio > threshold)
      3. Top-class confidence < NIGHT_CONF_FLOOR (already checked by caller)

    Function #3 is the caller's responsibility — this helper only deals with
    "is this frame at night and does it look IR-illuminated".
    """
    ts = _resolve_timestamp(timestamp, frame_path)
    if ts is None:
        return False
    try:
        from infra.time_of_day import is_night_at_edt

        if not is_night_at_edt(ts):
            return False
    except Exception:
        return False
    try:
        ratio = _brightness_ratio(img)
    except Exception:
        return False
    return ratio > NIGHT_BRIGHTNESS_RATIO
