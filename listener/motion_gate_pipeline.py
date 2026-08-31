"""motion_gate_pipeline — pre-Qwen motion gate using YOLOv8n ONNX classifier.

STATUS: provisional (Phase.107 §11.37, 2026-08-23; Phase.109 §11.39 v2 fixes 2026-08-24;
        Phase.137 §11.59 catchall-when-no-vehicle fix 2026-08-27;
        Phase.144 §11.66 YOLO-tighten revert + pairwise diff image 2026-08-27;
        Phase.169 §11.93 apply diff bbox to EARLIER frame of pair 2026-08-31)
THREAD SAFETY: single-threaded (one alert at a time, no shared state)
INPUTS:
  - 4 frame paths (from persistent RTSP buffer via listener.py)
  - camera name (for per-camera threshold lookup)
  - paths.alert_id, paths.output_dir
  - env var MOTION_GATE_V2 (opt-in: enables V2 fallback + tighter routing)
OUTPUTS:
  - GateVerdict dataclass (decision, class_label, confidence, crop paths,
    bboxes, raw verdicts, reason)
  - pairwise diff image at `<output_dir>/pairwise_diff.jpg` (Phase.144,
    §11.66) — read by vehicle_identifier.identify_from_crops so Qwen can
    see what pixels changed between frame_3 and frame_4.
PUBLIC API:
  - GateVerdict dataclass
  - THRESHOLDS_BY_CLASS: per-class default confidence thresholds (module-level)
  - ANIMAL_CLASSES: set of COCO classes considered "animal"
  - VEHICLE_CLASSES: set of COCO classes considered "vehicle"
  - load_thresholds(camera_name: str) -> dict[str, float]
  - run(ctx: AlertContext) -> GateVerdict
  - is_v2_enabled() -> bool (Phase.109 — gates V2 fallback behavior)
DOES NOT DO:
  - Does NOT run Qwen3-VL (that's the downstream pipeline's job)
  - Does NOT send Telegram messages
  - Does NOT match vehicles to known_vehicles.json
  - Does NOT persist alerts (that's the pipeline's job)
  - Does NOT capture frames from RTSP (listener.py passes paths in)
  - Does NOT orchestrate downstream pipelines (listener.py routes)
  - Does NOT do its own motion detection on 6-frame bursts (that's
    infra/motion_detector.py — used by the old pipeline path)
  - Does NOT tighten streak crops via YOLO (Phase.144 §11.66). The
    crop that Qwen sees is the streak crop = the pairwise-diff bbox
    crop of the frame. YOLO inside the streak was unreliable when a
    non-vehicle (tractor) moved in front of a vehicle-class object.
CALLED BY: listener/listener.py (webhook handler, when MOTION_GATE_ENABLED=1)
CALLS INTO:
  - infra/frame_diff.py (pairwise diff + bbox extraction + crop)
  - infra/quick_classifier.py (YOLOv8n ONNX classifier)
  - infra/paths.py (paths.alert_id, paths.output_dir)
  - config/motion_gate_thresholds.json (per-camera overrides)
RELATED:
  - PLAN.md §11.37 (locked architecture), §11.39 (V2 fixes), §11.59 (rule-5 catchall fix),
    §11.66 (YOLO-tighten revert + 3-image Qwen payload),
    §11.93 (apply diff bbox to earlier frame of pair — Phase.169)
  - docs/RESEARCH-2026-08-23-reolink-vs-frigate-classification.md

Architecture (LOCKED §11.37, modified §11.66 + §11.93):
  Reolink fires webhook → listener peels 4 frames from persistent RTSP
  → this module runs:
    diff(frame_2, frame_3) → bbox_a → crop_a = frame_2.crop(bbox_a)
    diff(frame_3, frame_4) → bbox_b → crop_b = frame_3.crop(bbox_b)
    YOLOv8n ONNX on crop_a + crop_b (~25ms each) — gate decision only
    [Phase.144 §11.66] write `pairwise_diff.jpg` = abs(frame_3 − frame_4)
      with bbox overlay, so Qwen sees what pixels moved.
    per-class + per-camera threshold gate
    return GateVerdict
  → listener.py routes by verdict.decision

Routing decision tree (LOCKED §11.37 + V2 §11.39 + §11.59 catchall fix):
  1. ANY crop high-conf vehicle-class → vehicle pipeline ("high_conf_vehicle")
  2. BOTH crops high-conf person → person pipeline ("high_conf_person")
  3. ANY crop high-conf animal → suppress ("animal_suppressed_no_pipeline")
  4. All low conf → suppress ("no_object_detected")
  5. Mixed (vehicle somewhere in the high-conf mix) → vehicle pipeline
     ("mixed_vehicle_wins")
  5b. High-conf non-vehicle class with no vehicle anywhere in the mix →
      suppress ("high_conf_<class>_not_vehicle_no_pipeline").
      §11.59 split: the prior catchall unconditionally emitted
      ("vehicle", <class>, ..., "mixed_vehicle_wins") regardless of
      whether a vehicle was present. That produced decision=vehicle
      class=person alerts that the vehicle pipeline could not process.
      225 occurrences in logs/launchctl-stderr.log
      between 2026-08-23 and 2026-08-27. Now suppressed with the
      observed class named in the reason for postmortem clarity.
  V2 §11.39: rule 5 requires vehicle conf >= 0.6 to override person conf >= 0.4.

V2 fixes (Phase.109 §11.39, 2026-08-24 — opt-in via MOTION_GATE_V2=1):
  - F1. Full-frame YOLO fallback: when crop-based classification returns
        class=None OR when diff finds no motion, run YOLO on frame_3 as
        backstop. Catches stationary-person-at-door and parked-vehicle
        cases where pairwise diff misses the subject.
  - F2. Bypass no_server_motion early-return in V2 mode. Diff becomes a
        bbox-hint, not a hard gate. YOLO always gets a chance.
  - F3. Routing rule 5 tightened in V2 mode (see above).
  - F4. All V2 changes are env-gated; default behavior unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

import cv2
from PIL import Image

from infra.frame_diff import (
    DEFAULT_DIFF_THRESHOLD,
    DEFAULT_MIN_AREA_PX,
    crop_frame_to_bbox,
    diff_pair_with_bbox,
)
from infra.quick_classifier import KEEP_CLASSES_DEFAULT, QuickClassifier, QuickVerdict

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env var: GATE_KEEP_DISK_ARTIFACTS
# ---------------------------------------------------------------------------
#
# Phase.115 (§11.46.6): when True, the gate writes frame_001..004.jpg
# + frame_N_crop_*.jpg to disk (postmortem / debugging). When False
# (default), the gate only writes composite.jpg (composite_telegram needs
# a path). All other consumers read from GateVerdict.frames (PIL.Image).
#
# We default to False because the in-memory path is the new authoritative
# one and the disk writes are pure debugging convenience. Flip to True via
# the plist env vars when investigating a misroute.

_KEEP_DISK_ARTIFACTS_DEFAULT = False


def _is_keep_disk_artifacts_enabled() -> bool:
    """Return True if GATE_KEEP_DISK_ARTIFACTS is truthy.

    Accepts: 1, true, yes, on (case-insensitive). Empty/unset → False.
    """
    val = os.environ.get("GATE_KEEP_DISK_ARTIFACTS", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _gate_keep_disk_artifacts() -> bool:
    """Wrapper that allows tests to monkeypatch the policy."""
    return _is_keep_disk_artifacts_enabled()


def _load_frame_as_pil(path: str) -> Image.Image:
    """Load a frame from disk into a PIL.Image (RGB).

    Used by the gate at run() start to populate verdict.frames so the
    downstream pipeline doesn't need to re-read the disk. cv2.imread
    gives BGR; we convert to RGB for PIL.
    """
    bgr = cv2.imread(path, cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError(f"could not load frame from {path}")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def _pil_crop(image: Image.Image, bbox: tuple[int, int, int, int]) -> Image.Image:
    """Crop a PIL.Image to (x, y, w, h). PIL.Image.crop expects (left, top, right, bottom)."""
    x, y, w, h = bbox
    return image.crop((x, y, x + w, y + h))


def _write_pairwise_diff_image(
    frame_a: Image.Image,
    frame_b: Image.Image,
    bbox_a: tuple[int, int, int, int] | None,
    bbox_b: tuple[int, int, int, int] | None,
    output_dir: str,
    alert_id: str,
) -> str | None:
    """Write abs(frame_a − frame_b) as a JPEG with bbox overlays.

    Phase.144 (§11.66): Qwen3-VL needs to see what pixels changed between
    consecutive frames so it can identify the MOVING subject rather than
    any stationary vehicles that happen to be in the same frame. The diff
    image is bright where motion happened and dark where the scene is
    stationary. The moving object's pixels light up; the parked Sequoia
    behind the tractor stays dark — Qwen can disambiguate.

    Args:
        frame_a: PIL.Image from the persistent RTSP ring buffer (frame_3).
        frame_b: PIL.Image from the persistent RTSP ring buffer (frame_4).
        bbox_a: motion bbox for diff(frame_2, frame_3) — drawn in green.
        bbox_b: motion bbox for diff(frame_3, frame_4) — drawn in green.
        output_dir: where to save the diff JPEG (typically paths.output_dir).
        alert_id: for the filename and log lines.

    Returns:
        Path to the saved JPEG, or None on failure.
    """
    from pathlib import Path

    import numpy as np

    try:
        arr_a = np.asarray(frame_a.convert("RGB"), dtype=np.int16)
        arr_b = np.asarray(frame_b.convert("RGB"), dtype=np.int16)
    except Exception as err:
        log.warning(
            f"[{alert_id}] pairwise_diff: failed to convert frames to arrays: {err}"
        )
        return None

    diff = np.abs(arr_a - arr_b).astype(np.uint8)  # uint8 = saturating
    # Amplify low-intensity diffs so motion is easier to see. Cap at 255.
    diff_amplified = np.clip(diff.astype(np.int32) * 4, 0, 255).astype(np.uint8)

    diff_img = Image.fromarray(diff_amplified, mode="RGB")

    # Draw bbox overlays so Qwen knows where the streak crops came from.
    # The moving subject (tractor) is INSIDE the bbox; stationary
    # vehicles (Sequoia) are OUTSIDE the bbox.
    if bbox_a is not None or bbox_b is not None:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(diff_img)
        if bbox_a is not None:
            x, y, w, h = bbox_a
            draw.rectangle(
                [(x, y), (x + w, y + h)], outline=(0, 255, 0), width=4
            )
        if bbox_b is not None:
            x, y, w, h = bbox_b
            draw.rectangle(
                [(x, y), (x + w, y + h)], outline=(0, 200, 255), width=4
            )

    out_dir = Path(output_dir)
    out_path = out_dir / "pairwise_diff.jpg"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        diff_img.save(out_path, format="JPEG", quality=90)
        return str(out_path)
    except Exception as err:
        log.warning(
            f"[{alert_id}] pairwise_diff: failed to write {out_path}: {err}"
        )
        return None


# ---------------------------------------------------------------------------
# Classifier protocol — anything with classify_frame(path) -> QuickVerdict
# ---------------------------------------------------------------------------


class ClassifierProtocol(Protocol):
    """Structural type for the classifier dependency.

    QuickClassifier (real YOLOv8n ONNX) implements this. Tests use a
    FakeClassifier with the same interface. Adding more classifiers
    (e.g., a fine-tuned variant) is just implementing this protocol.
    """

    def classify_frame(
        self, frame: str | Image.Image, timestamp: datetime | None = None
    ) -> QuickVerdict:
        ...


# ---------------------------------------------------------------------------
# COCO class groupings for routing decisions
# ---------------------------------------------------------------------------

VEHICLE_CLASSES: set[str] = {
    "car",
    "truck",
    "bus",
    "motorcycle",
    "bicycle",
}

# Animals: currently suppressed (no animal pipeline). Listed here so future
# work has a clear group to route to when an animal pipeline is built.
ANIMAL_CLASSES: set[str] = {
    "dog",
    "cat",
    "horse",
    "sheep",
    "cow",
    "bear",
    "bird",
}

# COCO classes we care about for surveillance. Anything else is treated as
# "other" and subject to the default threshold.
SURVEILLANCE_CLASSES: set[str] = VEHICLE_CLASSES | ANIMAL_CLASSES | {"person"}

# Per-class default confidence thresholds (LOCKED §11.37).
# Override via config/motion_gate_thresholds.json per camera.
THRESHOLDS_BY_CLASS: dict[str, float] = {
    "car": 0.50,
    "truck": 0.50,
    "bus": 0.50,
    "motorcycle": 0.45,
    "bicycle": 0.45,
    "person": 0.35,
    # Animals — only "confident" animal detections pass (currently suppressed)
    "dog": 0.60,
    "cat": 0.60,
    "horse": 0.60,
    "sheep": 0.60,
    "cow": 0.60,
    "bear": 0.60,
    "bird": 0.60,
}

# Threshold for any COCO class not in THRESHOLDS_BY_CLASS
DEFAULT_OTHER_COCO_THRESHOLD = 0.55

# Path to per-camera overrides (relative to PROJECT_ROOT)
THRESHOLDS_CONFIG_PATH = Path("config") / "motion_gate_thresholds.json"


# ---------------------------------------------------------------------------
# Phase.109 §11.39 — V2 opt-in via env var
# ---------------------------------------------------------------------------
#
# When MOTION_GATE_V2=1, the gate activates:
#   F1. Full-frame YOLO fallback (catches stationary subjects, parked vehicles)
#   F2. Bypass no_server_motion early-return (YOLO always runs)
#   F3. Tighter routing rule 5 (vehicle conf >= 0.6 to override person conf >= 0.4)
#
# Default OFF so existing behavior is preserved. Roll back by unsetting env.
def is_v2_enabled() -> bool:
    """True iff MOTION_GATE_V2 env var is truthy.

    Accepted truthy values: "1", "true", "yes" (case-insensitive).
    Anything else (including unset / empty) is False.
    """
    return os.environ.get("MOTION_GATE_V2", "").strip().lower() in ("1", "true", "yes")


# V2 routing thresholds (used in rule 5 only)
V2_VEHICLE_OVERRIDE_MIN_CONF: float = 0.6  # vehicle must be at or above to override person
V2_PERSON_OVERRIDE_MAX_CONF: float = 0.4   # person at or below is overridden by vehicle


# ---------------------------------------------------------------------------
# Public dataclass: GateVerdict
# ---------------------------------------------------------------------------

DecisionType = Literal["vehicle", "person", "suppress"]


@dataclass
class GateVerdict:
    """Result of running the motion gate.

    Phase.115 (§11.46.6): the gate's output now includes in-memory
    PIL images of the 4 frames + the 2 cropped regions. This eliminates
    the TOCTOU race where the listener had to check `os.path.isfile()`
    on the gate's writes before reading them.

    Fields:
      decision: routing decision — "vehicle" / "person" / "suppress"
      class_label: top COCO class detected (or None if suppress)
      confidence: max confidence across both crops (0.0 if no detection)
      frames: 4 full frames as PIL.Image (BGR→RGB). Loaded once at gate
        start, used for diff + crops + downstream pipeline. This is the
        authoritative copy on the hot path — no filesystem reads.
      crop_a: PIL.Image crop from frames[1] using bbox_a (or None)
      crop_b: PIL.Image crop from frames[2] using bbox_b (or None)
      bbox_a: (x, y, w, h) from diff(2,3) — None if no motion
      bbox_b: (x, y, w, h) from diff(3,4) — None if no motion
      frame_paths: 4 disk paths; present when GATE_KEEP_DISK_ARTIFACTS=true
        (for postmortem / debugging). Empty list when env var is off.
      crop_a_path: disk path to frame_2 crop (None when env var off)
      crop_b_path: disk path to frame_3 crop (None when env var off)
      pairwise_diff_path: disk path to abs(frame_3 − frame_4) JPEG with
        bbox overlays (Phase.144 §11.66). Qwen sees this to
        disambiguate the moving subject from stationary vehicles that
        may be in the same frame. None when GATE_KEEP_DISK_ARTIFACTS=false
        or when frame_3/frame_4 can't be loaded.
      raw_verdicts: list of QuickVerdict from each crop classification
      reason: human-readable string for logging / audit
    """

    decision: DecisionType
    class_label: str | None
    confidence: float
    frames: list = field(default_factory=list)  # list[PIL.Image.Image] — 4 full frames
    crop_a: object | None = None  # PIL.Image.Image | None
    crop_b: object | None = None  # PIL.Image.Image | None
    bbox_a: tuple[int, int, int, int] | None = None
    bbox_b: tuple[int, int, int, int] | None = None
    frame_paths: list[str] = field(default_factory=list)
    crop_a_path: str | None = None
    crop_b_path: str | None = None
    pairwise_diff_path: str | None = None
    raw_verdicts: list[QuickVerdict] = field(default_factory=list)
    reason: str = ""

    @property
    def is_suppress(self) -> bool:
        return self.decision == "suppress"

    @property
    def is_pass(self) -> bool:
        return self.decision in ("vehicle", "person")


# ---------------------------------------------------------------------------
# Threshold loading
# ---------------------------------------------------------------------------

_cached_thresholds: dict | None = None

# Module-level singleton for QuickClassifier (lazy init on first use).
# QuickClassifier model load takes ~780ms on Apple Silicon (CoreML EP).
# Keeping it module-level means we load once per process, not once per alert.
# Tests inject their own FakeClassifier via the `classifier` param to bypass
# this singleton.
_cached_classifier: QuickClassifier | None = None


def _load_thresholds_config() -> dict:
    """Load config/motion_gate_thresholds.json from PROJECT_ROOT.

    Returns the parsed JSON dict. If the file is missing or malformed, returns
    an empty dict (which means: all cameras use per-class defaults).
    """
    try:
        from infra.paths import PROJECT_ROOT

        config_path = PROJECT_ROOT / THRESHOLDS_CONFIG_PATH
        if not config_path.is_file():
            log.warning(
                f"motion_gate: thresholds config not found at {config_path}, "
                "using per-class defaults for all cameras"
            )
            return {}
        with config_path.open() as f:
            data = json.load(f)
        # Strip the "_comment" / "_default_thresholds_reference" keys
        return {
            k: v for k, v in data.items() if not k.startswith("_")
        }
    except Exception as e:
        log.warning(f"motion_gate: failed to load thresholds config: {e!r}")
        return {}


# Phase.152 — per-camera × per-event-type gate configuration. See
# PLAN §11.75. Each camera may have a top-level "gate_enabled" field in
# motion_gate_thresholds.json shaped like:
#     "gate_enabled": {"vehicle": true, "person": true, "motion": true}
# Default if missing or field absent: all event types enabled (current
# behavior preserved).
DEFAULT_GATE_ENABLED: dict[str, bool] = {
    "vehicle": True,
    "person": True,
    "motion": True,
}


def is_gate_enabled(camera_name: str, event_type: str) -> bool:
    """Phase.152 — is the gate enabled for (camera, event_type)?

    Reads config/motion_gate_thresholds.json at PROJECT_ROOT and looks up
    camera's "gate_enabled" map. Returns True when:
      - the file is missing/malformed (graceful default)
      - the camera is missing from the config (graceful default)
      - the camera's "gate_enabled" entry is missing for the camera
      - the event_type is missing from the camera's map (graceful default)
    Returns False ONLY when the camera explicitly disables this event_type.

    Args:
      camera_name: canonical camera name (after alias resolution)
      event_type: lowercased event ("vehicle", "person", "people", "md")
                  — "people" treated as "person" for backward compat.

    Used by listener._motion_gate_dispatch.maybe_run_motion_gate() to decide
    whether to call the gate at all. When False, the gate is skipped (no
    capture, no YOLO, no verdict) and the alert routes directly to the
    vehicle/person pipeline downstream.

    Phase.167 §13.4 Commit 17 (T3 C17): JSON keys are CAM{N} codes,
    not friendly names. Lookup translates via infra.cameras.code_for
    which returns the camera code (or the input unchanged on miss).
    """
    from infra.cameras import code_for  # §13.4: name → CAM{N}
    cfg = _load_thresholds_config().get(code_for(camera_name)) or {}
    gate_enabled = cfg.get("gate_enabled") or DEFAULT_GATE_ENABLED

    # Normalize event_type → config key
    # "people" is the Reolink payload form; "person" is the singular YOLO form.
    if event_type in ("people",):
        key = "person"
    else:
        key = event_type

    return bool(gate_enabled.get(key, True))


def load_thresholds(camera_name: str) -> dict[str, float]:
    """Resolve the effective thresholds for a given camera.

    Lookup order (most-specific wins):
      1. Per-camera + per-class in config/motion_gate_thresholds.json
      2. Per-class default in THRESHOLDS_BY_CLASS (module constant)
      3. DEFAULT_OTHER_COCO_THRESHOLD for any COCO class not covered

    Returns dict mapping COCO class name → confidence threshold (0.0-1.0).

    Phase.167 §13.4 Commit 17 (T3 C17): JSON keys are CAM{N} codes,
    not friendly names. Lookup translates via infra.cameras.code_for
    which returns the camera code (or the input unchanged on miss).
    """
    from infra.cameras import code_for  # §13.4: name → CAM{N}
    global _cached_thresholds
    if _cached_thresholds is None:
        _cached_thresholds = _load_thresholds_config()

    camera_overrides = _cached_thresholds.get(code_for(camera_name), {})

    # Merge: per-camera overrides win over per-class defaults.
    # Phase.154 (§11.77): skip metadata keys (start with "_") AND
    # non-scalar values (gate_cooldown/gate_enabled are dicts, not
    # thresholds). Before this guard, load_thresholds would crash with
    # "float() argument must be a string or a real number, not 'dict'"
    # whenever a camera config contained a dict-valued field.
    effective = dict(THRESHOLDS_BY_CLASS)
    for cls, threshold in camera_overrides.items():
        if cls.startswith("_"):
            continue
        if not isinstance(threshold, (int, float)):
            continue  # skip dicts / lists / strings (gate_cooldown etc.)
        effective[cls] = float(threshold)
    return effective


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------


def _threshold_for(thresholds: dict[str, float], coco_class: str) -> float:
    """Get the threshold for a given COCO class."""
    return thresholds.get(coco_class, DEFAULT_OTHER_COCO_THRESHOLD)


def _classify_crop(
    classifier: ClassifierProtocol,
    crop: str | Image.Image | None,
    thresholds: dict[str, float],
    timestamp: datetime | None = None,
) -> QuickVerdict:
    """Classify a single crop. Returns QuickVerdict from quick_classifier.

    Phase.115: crop may be a disk path (legacy, GATE_KEEP_DISK_ARTIFACTS=true)
    OR an in-memory PIL.Image (new, default). Both routes go through
    classifier.classify_frame() which handles the union.

    Phase.116 (timestamp-fix): `timestamp` is forwarded to classify_frame()
    so the night-suppression heuristic can check `is_night_at_edt(timestamp)`.
    Previously the heuristic was dormant because crop=PIL.Image meant no file
    mtime fallback, and the timestamp was never threaded through. Now the
    webhook's ISO timestamp flows listener -> dispatch -> run_gate ->
    _classify_crop -> classify_frame.
    """
    if crop is None:
        return QuickVerdict(
            top_class="none",
            top_confidence=0.0,
            decision="suppress",
            reason="no_crop",
        )
    verdict = classifier.classify_frame(crop, timestamp=timestamp)
    # Day-gate threshold check — re-stamp decision to "pass" / "suppress".
    # IMPORTANT (Phase.116 timestamp-fix): only re-stamp if the classifier
    # didn't already suppress. The night heuristic inside classify_frame()
    # may have set decision="suppress" based on (timestamp, brightness, conf,
    # class) — we must NOT override that. Otherwise a conf=0.34 person at
    # night would be flipped back to "pass" just because the per-camera
    # person threshold (e.g. 0.30 for CAM1) is permissive.
    if verdict.decision == "suppress":
        # Classifier vetoed (night heuristic, no_object, etc.) — respect it.
        return verdict
    threshold = _threshold_for(thresholds, verdict.top_class)
    if verdict.top_confidence < threshold:
        verdict.decision = "suppress"
        verdict.reason = "class_below_threshold"
    elif verdict.top_class in KEEP_CLASSES_DEFAULT:
        verdict.decision = "pass_with_hint"
    else:
        verdict.decision = "pass"
    return verdict


def _route_decision(
    verdict_a: QuickVerdict,
    verdict_b: QuickVerdict,
    thresholds: dict[str, float],
    v2: bool = False,
) -> tuple[DecisionType, str | None, float, str]:
    """Apply routing decision tree (LOCKED §11.37 + V2 tightening in §11.39).

    Args:
      verdict_a, verdict_b: QuickVerdict from classify_frame on each crop.
      thresholds: per-camera threshold overrides (forwarded for compatibility).
      v2: if True, apply tighter rule 5 (Phase.109 §11.39 F3).

    Returns (decision, class_label, confidence, reason).
    """
    # Use only the passes — verdicts with decision == "suppress" don't count
    # toward the routing decision (they're treated as "nothing here").
    high_conf = []
    for v in (verdict_a, verdict_b):
        if v.decision in ("pass", "pass_with_hint"):
            high_conf.append(v)

    if not high_conf:
        # Rule 4: All low conf → suppress
        top = max([verdict_a, verdict_b], key=lambda x: x.top_confidence)
        # Phase.116 — prefer the most informative reason from the per-crop
        # verdicts. If EITHER crop carried a reason (e.g. "night_low_confidence"
        # from the night heuristic), surface it. Otherwise fall back to the
        # generic "no_object_detected" label. Picks the higher-confidence crop
        # first; if it has no reason, try the other crop.
        other = verdict_a if top is verdict_b else verdict_b
        reason: str = top.reason or other.reason or "no_object_detected"
        return ("suppress", top.top_class if top.top_confidence > 0 else None, top.top_confidence, reason)

    # Sort by confidence desc — highest-confidence verdict drives the decision
    high_conf.sort(key=lambda v: v.top_confidence, reverse=True)
    top = high_conf[0]

    # Rule 1: ANY crop high-conf vehicle-class → vehicle
    if top.top_class in VEHICLE_CLASSES:
        return ("vehicle", top.top_class, top.top_confidence, "high_conf_vehicle")

    # Rule 2: Person pipeline
    # V1: requires BOTH crops high-conf person.
    # V2 §11.39: relaxes to "high-conf person in any crop" when the other crop
    # is empty/none. This handles the V2 fallback case where full-frame YOLO
    # put a person in one slot and the other slot is still empty.
    if top.top_class == "person":
        if v2:
            # V2: single high-conf person with empty other slot → person pipeline.
            other_empty = (
                len(high_conf) == 1
                or (len(high_conf) >= 2 and high_conf[1].top_class in (None, "none"))
            )
            if other_empty or (
                len(high_conf) == 2 and high_conf[1].top_class == "person"
            ):
                return ("person", "person", top.top_confidence, "high_conf_person")
        elif len(high_conf) == 2 and high_conf[1].top_class == "person":
            return ("person", "person", top.top_confidence, "high_conf_person")

    # Rule 3: ANY crop high-conf animal → suppress (no animal pipeline)
    if top.top_class in ANIMAL_CLASSES:
        return ("suppress", top.top_class, top.top_confidence, "animal_suppressed_no_pipeline")

    # Rule 5: Mixed (person + other-not-vehicle, or other-not-vehicle alone)
    # Vehicle wins on ambiguity.
    #
    # V2 §11.39 F3: in V2 mode, if a person is anywhere in the high_conf mix
    # AND a vehicle-class detection is also present (regardless of which is top),
    # require vehicle conf >= V2_VEHICLE_OVERRIDE_MIN_CONF to override. Below
    # that, suppress rather than route a likely-person to the vehicle pipeline.
    person_top = next(
        (v for v in high_conf if v.top_class == "person"),
        None,
    )
    vehicle_top = next(
        (v for v in high_conf if v.top_class in VEHICLE_CLASSES),
        None,
    )
    if v2 and person_top is not None and vehicle_top is not None:  # noqa: SIM102 (nested-if kept for readability — flat version splits one logical check into three)
        if (
            person_top.top_confidence >= V2_PERSON_OVERRIDE_MAX_CONF
            and vehicle_top.top_confidence < V2_VEHICLE_OVERRIDE_MIN_CONF
        ):
            # Vehicle override not confident enough — suppress rather
            # than route a likely-person to the vehicle pipeline.
            return (
                "suppress",
                person_top.top_class,
                person_top.top_confidence,
                "v2_person_present_low_vehicle_override_suppressed",
            )

    # Catchall: at this point we have ≥1 high-conf verdict with a class
    # that is not in VEHICLE_CLASSES (rule 1 missed), not "person"
    # falling under rule 2, and not in ANIMAL_CLASSES (rule 3 missed).
    # Per PLAN §11.37 Q2/Q3, the LEGITIMATE rule 5 case is "vehicle
    # somewhere in the mix" — that's `vehicle_top is not None` below.
    #
    # Phase.137 (§11.59): the prior catchall unconditionally returned
    # decision="vehicle" regardless of whether a vehicle-class detection
    # was actually present. That meant a single high-conf person, or
    # train, bench, zebra, or any other non-vehicle COCO class with no
    # vehicle anywhere in the mix, was routed to the vehicle pipeline —
    # which then crashed because the vehicle pipeline requires a vehicle
    # shape (color/make/model) and emits Telegram messages that read as
    # "Vehicle in motion: <bench>". 225 such occurrences in the listener
    # log between 2026-08-23 and 2026-08-27.
    #
    # LOCKED fix: only default to "vehicle" when there is a vehicle in
    # the high_conf mix. Otherwise suppress with a clear reason that
    # names the class observed, so postmortem analysis can distinguish
    # "rule 5 mixed" from "rule 4 no-object" suppressions.
    if vehicle_top is not None:
        return (
            "vehicle",
            vehicle_top.top_class,
            vehicle_top.top_confidence,
            "mixed_vehicle_wins",
        )
    return (
        "suppress",
        top.top_class,
        top.top_confidence,
        f"high_conf_{top.top_class}_not_vehicle_no_pipeline",
    )


# ---------------------------------------------------------------------------
# Public entry point: run()
# ---------------------------------------------------------------------------


def run(
    frame_paths: list[str],
    camera_name: str,
    alert_id: str,
    output_dir: str,
    classifier: ClassifierProtocol | None = None,
    diff_threshold: int = DEFAULT_DIFF_THRESHOLD,
    min_area_px: int = DEFAULT_MIN_AREA_PX,
    timestamp: datetime | None = None,
) -> GateVerdict:
    """Run the motion gate on 4 captured frames.

    Args:
      frame_paths: exactly 4 frame paths (frame_1, frame_2, frame_3, frame_4).
        Order matters — index 1 and 2 are diff'd for bbox_a; index 2 and 3
        for bbox_b.
      camera_name: used to look up per-camera threshold overrides.
      alert_id: for logging.
      output_dir: where to save crop files when GATE_KEEP_DISK_ARTIFACTS=true
        (otherwise unused).
      classifier: optional classifier instance implementing ClassifierProtocol
        (one is created if not provided). Pass an instance to amortize
        model load across calls. Tests use FakeClassifier with the same
        protocol.
      diff_threshold: pixel threshold for the pairwise diff (default 25).
      min_area_px: minimum bbox area in pixels (default 64).
      timestamp: optional webhook event timestamp (Phase.116 timestamp-fix).
        Forwarded to classify_frame() so the night-suppression heuristic can
        check `is_night_at_edt(timestamp)`. When None, falls back to file
        mtime (legacy path). When all fallbacks fail, the heuristic is
        skipped (safe default — don't suppress without a signal).

    Returns:
      GateVerdict with the routing decision, in-memory PIL frames + crops,
      and (when GATE_KEEP_DISK_ARTIFACTS=true) disk paths for postmortem.

    Per §11.37 LOCKED architecture:
      - diff(frame_2, frame_3) → bbox_a → crop_a = frame_2.crop(bbox_a)
      - diff(frame_3, frame_4) → bbox_b → crop_b = frame_3.crop(bbox_b)
      - classify both crops, apply thresholds, route to vehicle/person/suppress

    Per §11.46.6 (Phase.115): the gate loads all 4 frames once at start
    into PIL.Image, stashes them in verdict.frames, and crops bbox regions
    into verdict.crop_a / verdict.crop_b. Downstream pipeline reads from
    the verdict — no filesystem check on the hot path. Disk writes
    (frame_001..004.jpg + crop_*.jpg) are gated by GATE_KEEP_DISK_ARTIFACTS
    for postmortem only.
    """
    t0 = time.perf_counter()
    keep_disk = _gate_keep_disk_artifacts()

    # Input validation
    if len(frame_paths) != 4:
        log.error(
            f"[{alert_id}] motion_gate: expected 4 frame_paths, got {len(frame_paths)}"
        )
        return GateVerdict(
            decision="suppress",
            class_label=None,
            confidence=0.0,
            bbox_a=None,
            bbox_b=None,
            reason="wrong_frame_count",
        )

    frame_2_path = frame_paths[1]
    frame_3_path = frame_paths[2]
    frame_4_path = frame_paths[3]

    log.info(
        f"[{alert_id}] motion_gate: starting camera={camera_name} "
        f"frames={[Path(p).name for p in frame_paths]} "
        f"keep_disk_artifacts={keep_disk}"
    )

    # Load all 4 frames into PIL.Image once. These are the authoritative
    # in-memory copies for the pipeline. Disk writes happen conditionally
    # below based on GATE_KEEP_DISK_ARTIFACTS.
    pil_frames: list[Image.Image] = []
    try:
        for p in frame_paths:
            pil_frames.append(_load_frame_as_pil(p))
    except Exception as e:
        log.error(f"[{alert_id}] motion_gate: failed to load frames: {e}")
        return GateVerdict(
            decision="suppress",
            class_label=None,
            confidence=0.0,
            bbox_a=None,
            bbox_b=None,
            reason="frame_load_failed",
        )

    frame_2_pil = pil_frames[1]
    frame_3_pil = pil_frames[2]
    frame_4_pil = pil_frames[3]

    # Lazy-load classifier (singleton across runs within this process).
    # When the caller doesn't supply one, use the module-level singleton so
    # we don't reload the ~12MB YOLOv8n ONNX model on every webhook.
    if classifier is None:
        global _cached_classifier
        if _cached_classifier is None:
            log.info(
                f"[{alert_id}] motion_gate: loading YOLOv8n classifier "
                "(one-time, ~780ms)"
            )
            _cached_classifier = QuickClassifier()
        classifier = _cached_classifier

    thresholds = load_thresholds(camera_name)

    # ---- diff(2,3) → bbox_a → crop_a ----
    bbox_a, count_a, _mask_a = diff_pair_with_bbox(
        frame_2_path, frame_3_path, threshold=diff_threshold, min_area_px=min_area_px
    )
    log.debug(
        f"[{alert_id}] motion_gate: diff(2,3) changed_pixels={count_a} "
        f"bbox_a={bbox_a}"
    )

    # ---- diff(3,4) → bbox_b → crop_b ----
    bbox_b, count_b, _mask_b = diff_pair_with_bbox(
        frame_3_path, frame_4_path, threshold=diff_threshold, min_area_px=min_area_px
    )
    log.debug(
        f"[{alert_id}] motion_gate: diff(3,4) changed_pixels={count_b} "
        f"bbox_b={bbox_b}"
    )

    # ---- edge case: no motion detected by either diff ----
    # V2 §11.39 F2: in V2 mode, bypass this early-return. Diff becomes a
    # bbox-hint, not a hard gate. YOLO gets a chance on the full frame
    # in the fallback below. Catches stationary-person-at-door and
    # parked-vehicle cases.
    if bbox_a is None and bbox_b is None and not is_v2_enabled():
        log.info(f"[{alert_id}] motion_gate: no motion detected by either diff — suppressing")
        return GateVerdict(
            decision="suppress",
            class_label=None,
            confidence=0.0,
            frames=pil_frames,
            bbox_a=None,
            bbox_b=None,
            reason="no_server_motion",
        )

    # ---- crop frames (in-memory + optional disk write) ----
    # In-memory PIL crops: always built (verdict.crop_a/b are the
    # authoritative copy on the hot path).
    crop_a_pil = _pil_crop(frame_2_pil, bbox_a) if bbox_a else None
    crop_b_pil = _pil_crop(frame_3_pil, bbox_b) if bbox_b else None

    # Disk writes: only when GATE_KEEP_DISK_ARTIFACTS=true. The crop
    # files are written next to the source frame with the existing
    # filename pattern (frame_N_crop<x>_<y>_<w>x<h>.jpg) so postmortem
    # tools keep working.
    crop_a_path: str | None = None
    crop_b_path: str | None = None
    if keep_disk:
        crop_a_path = crop_frame_to_bbox(frame_2_path, bbox_a) if bbox_a else None
        crop_b_path = crop_frame_to_bbox(frame_3_path, bbox_b) if bbox_b else None

    # ---- classify both crops ----
    # Phase.115 (§11.46.6): when GATE_KEEP_DISK_ARTIFACTS=true the
    # crop is on disk, so pass the path. When off, pass the in-memory
    # PIL crop directly (classify_frame accepts both).
    classifier_input_a = crop_a_path if keep_disk else crop_a_pil
    classifier_input_b = crop_b_path if keep_disk else crop_b_pil
    verdict_a = _classify_crop(classifier, classifier_input_a, thresholds, timestamp=timestamp)
    verdict_b = _classify_crop(classifier, classifier_input_b, thresholds, timestamp=timestamp)

    log.debug(
        f"[{alert_id}] motion_gate: "
        f"crop_a={verdict_a.top_class}@{verdict_a.top_confidence:.2f}({verdict_a.decision}) "
        f"crop_b={verdict_b.top_class}@{verdict_b.top_confidence:.2f}({verdict_b.decision})"
    )

    # ---- V2 §11.39 F1: full-frame YOLO fallback ----
    # When BOTH crop verdicts failed to detect anything (class=None / "none"
    # AND decision=="suppress"), run YOLO on frame_3 as a backstop. This is
    # the stationary-person-at-door case: diff fires on noise, not the
    # person, so the crop bbox misses them. Full-frame YOLO sees them.
    #
    # Also fires when there was no diff at all (bbox_a/b both None) and the
    # early-return above was bypassed (V2 mode).
    v2_fallback_fired = False
    if is_v2_enabled():
        crops_empty = (
            (verdict_a.top_class in (None, "none") or verdict_a.decision == "suppress")
            and (verdict_b.top_class in (None, "none") or verdict_b.decision == "suppress")
        )
        if crops_empty:
            log.info(
                f"[{alert_id}] motion_gate: V2 fallback — running YOLO on full frame_3 "
                f"(crop_a_class={verdict_a.top_class}@{verdict_a.top_confidence:.2f}, "
                f"crop_b_class={verdict_b.top_class}@{verdict_b.top_confidence:.2f})"
            )
            full_frame_verdict = classifier.classify_frame(frame_3_path, timestamp=timestamp)
            threshold = _threshold_for(thresholds, full_frame_verdict.top_class)
            log.info(
                f"[{alert_id}] motion_gate: V2 fallback — full-frame verdict="
                f"{full_frame_verdict.top_class}@{full_frame_verdict.top_confidence:.2f} "
                f"threshold={threshold:.2f}"
            )
            v2_fallback_fired = True
            if full_frame_verdict.top_confidence >= threshold:
                # Full-frame detection passes — use verdict_a's slot
                # so the routing tree sees a high-conf verdict.
                if full_frame_verdict.top_class in KEEP_CLASSES_DEFAULT:
                    full_frame_verdict.decision = "pass_with_hint"
                else:
                    full_frame_verdict.decision = "pass"
                # Replace verdict_a (cropped empty) with the full-frame verdict.
                verdict_a = full_frame_verdict

    # crop_a_pil and crop_b_pil are the streak crops (motion-bbox crop of
    # frame_3 / frame_4). Phase.144 §11.66 removed the YOLO-tighten
    # step — Qwen receives these streak crops directly (along with the
    # pairwise diff image — see `_write_pairwise_diff_image` below).

    # ---- route decision ----
    decision, class_label, confidence, reason = _route_decision(
        verdict_a, verdict_b, thresholds, v2=is_v2_enabled()
    )

    # Append v2-fallback marker so the audit log shows when fallback fired
    if v2_fallback_fired and decision != "suppress":
        reason = f"{reason}+v2_full_frame_fallback"

    elapsed_ms = (time.perf_counter() - t0) * 1000
    log.info(
        f"[{alert_id}] motion_gate: decision={decision} class={class_label} "
        f"conf={confidence:.2f} reason={reason} elapsed={elapsed_ms:.1f}ms"
    )

    # ---- pairwise differential image for Qwen (Phase.144 §11.66) ----
    # Qwen3-VL gets the streak crops (crop_a, crop_b) and now also the
    # full-frame pairwise diff between frame_3 and frame_4. The diff is
    # bright where motion happened; stationary vehicles stay dark. This
    # gives Qwen the disambiguating signal that YOLO's bbox never had:
    # "the moving object is whatever lights up the diff." When both bboxes
    # are present, the green rectangle shows diff(2,3) and the cyan shows
    # diff(3,4). Tractors, ATVs, mowers, and other equipment that YOLO
    # mis-labels as "car" are picked up correctly here.
    pairwise_diff_path: str | None = None
    if keep_disk and frame_3_pil is not None and frame_4_pil is not None:
        pairwise_diff_path = _write_pairwise_diff_image(
            frame_3_pil, frame_4_pil, bbox_a, bbox_b, output_dir, alert_id
        )
        if pairwise_diff_path:
            log.debug(
                f"[{alert_id}] motion_gate: wrote pairwise_diff "
                f"({pairwise_diff_path})"
            )

    return GateVerdict(
        decision=decision,
        class_label=class_label,
        confidence=confidence,
        frames=pil_frames,
        crop_a=crop_a_pil,
        crop_b=crop_b_pil,
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        frame_paths=frame_paths if keep_disk else [],
        crop_a_path=crop_a_path,
        crop_b_path=crop_b_path,
        pairwise_diff_path=pairwise_diff_path,
        raw_verdicts=[verdict_a, verdict_b],
        reason=reason,
    )
