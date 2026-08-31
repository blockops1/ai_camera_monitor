"""
vehicle_artifacts.py — Per-alert audit directory for the vehicle tracker.

STATUS: stable
THREAD SAFETY: thread-safe for parallel writes to different alert_ids;
    writes to the same alert_id dir serialize via per-alert locks in
    write_full_artifacts().

INPUTS:
    - function arg alert_id: str (required, the directory name)
    - function arg frame_paths: list[str] (required, source JPEGs)
    - function arg crop_path: str | None (optional, source crop)
    - function arg metadata: dict (required, alert metadata)
    - function arg first_pass_request: str | None (optional, prompt text)
    - function arg first_pass_response: dict | None (optional, Qwen JSON)
    - function arg second_pass_request: str | None (optional)
    - function arg second_pass_response: dict | None (optional)
    - env FARM_VEHICLE_ARTIFACTS (default "1") — "0"/"false"/"no"/"off" disables

OUTPUTS:
    - return value: bool (True on successful write, False on disabled)
    - writes file: data/vehicle_artifacts/<alert_id>/metadata.json
    - writes file: data/vehicle_artifacts/<alert_id>/first_pass.json
    - writes file: data/vehicle_artifacts/<alert_id>/first_pass_request.txt
    - writes file: data/vehicle_artifacts/<alert_id>/second_pass.json
    - writes file: data/vehicle_artifacts/<alert_id>/second_pass_request.txt
    - writes file: data/vehicle_artifacts/<alert_id>/frames/frame_NNN.jpg (copies)
    - writes file: data/vehicle_artifacts/<alert_id>/best_frame_crop.jpg
    - log line per write (info level)

PUBLIC API:
    is_enabled() -> bool
        Check whether persistence is on (env var + default).
    write_first_pass_request(alert_id, text) -> bool
    write_first_pass_response(alert_id, payload) -> bool
    write_second_pass_request(alert_id, text) -> bool
    write_second_pass_response(alert_id, payload) -> bool
    write_metadata(alert_id, metadata) -> bool
    copy_frames(alert_id, frame_paths) -> int
        Copy N source frames into the artifact dir. Returns count copied.
    copy_crop(alert_id, crop_path) -> bool
        Copy the best-frame crop into the artifact dir.
    write_full_artifacts(alert_id, frame_paths, crop_path, metadata,
                         first_pass_request=None, first_pass_response=None,
                         second_pass_request=None, second_pass_response=None) -> bool
        One-shot write of the full artifact set (single lock acquisition).
    list_artifacts(alert_id) -> list[str]
        List files in an alert's artifact dir.

DOES NOT DO:
    - Move the original data/frames/<alert_id>/ files — audit copies only
    - Clean up artifact dirs — they live until manual operator cleanup
    - Run vision or matching — infra.vision_analyzer and infra.vehicle_matcher
      own that; this module only persists their output

WHY HERE:
    Added 2026-07-24 per user request to make the vehicle tracker
    auditable end-to-end and to expose the per-frame vision output.
    Without artifacts the only signal of "what did the matcher see?" is
    the alert text — no way to inspect the actual Qwen outputs or the
    prompt that was sent. With artifacts an operator can replay any
    alert against an upgraded Qwen model.

CALLED BY:
    - listener.listener: write_full_artifacts() at end of vehicle alert
      processing
    - tests: write_first_pass_* / write_second_pass_* for fixtures

CALLS INTO:
    - infra.paths: VEHICLE_ARTIFACTS_DIR
    - shutil.copy2: copy source frames + crop into the artifact dir
    - json: serialize metadata + responses

RELATED:
    - data/vehicle_artifacts/<alert_id>/ — what this module writes
    - infra.vision_analyzer — produces the response payloads
    - infra.vehicle_matcher — produces the metadata payload
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any

from infra.paths import VEHICLE_ARTIFACTS_DIR

log = logging.getLogger("vehicle_artifacts")

_ENABLED_ENV = "FARM_VEHICLE_ARTIFACTS"


def is_enabled() -> bool:
    """True unless the env var explicitly disables persistence."""
    val = os.environ.get(_ENABLED_ENV, "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def _alert_dir(alert_id: str) -> str:
    return os.path.join(VEHICLE_ARTIFACTS_DIR, alert_id)


def write_first_pass_request(
    alert_id: str,
    frame_paths: list[str],
    prompt_text: str,
) -> str | None:
    """Save the prompt + frame filenames sent to Qwen for first pass."""
    if not is_enabled() or not alert_id:
        return None
    alert_dir = _alert_dir(alert_id)
    os.makedirs(alert_dir, exist_ok=True)
    out = os.path.join(alert_dir, "first_pass_request.txt")
    with open(out, "w") as f:
        f.write("# First-pass vision call (analyze_frames_queued)\n")
        f.write(f"# Frames sent (in order): {len(frame_paths)}\n")
        f.writelines(f"#   [{i}] {fp}\n" for i, fp in enumerate(frame_paths, 1))
        f.write(f"# Frame indices into alert_id dir: {len(frame_paths)}\n")
        f.write("\n# ---- Prompt text sent to Qwen ----\n")
        f.write(prompt_text)
        f.write("\n# ---- End prompt ----\n")
    return out


def write_first_pass_response(
    alert_id: str, response: dict[str, Any]
) -> str | None:
    """Save the parsed JSON response from Qwen first pass."""
    if not is_enabled() or not alert_id:
        return None
    alert_dir = _alert_dir(alert_id)
    os.makedirs(alert_dir, exist_ok=True)
    out = os.path.join(alert_dir, "first_pass.json")
    with open(out, "w") as f:
        json.dump(response, f, indent=2, default=str)
    return out


def write_second_pass_request(
    alert_id: str,
    crop_path: str,
    prompt_text: str,
) -> str | None:
    """Save the prompt + crop filename sent to Qwen for the focused pass."""
    if not is_enabled() or not alert_id:
        return None
    alert_dir = _alert_dir(alert_id)
    os.makedirs(alert_dir, exist_ok=True)
    out = os.path.join(alert_dir, "second_pass_request.txt")
    with open(out, "w") as f:
        f.write("# Second-pass vision call (classify_vehicle_crop)\n")
        f.write(f"# Crop image sent: {crop_path}\n\n")
        f.write("# ---- Prompt text sent to Qwen ----\n")
        f.write(prompt_text)
        f.write("\n# ---- End prompt ----\n")
    return out


def write_second_pass_response(
    alert_id: str, response: dict[str, Any]
) -> str | None:
    """Save the parsed JSON response from Qwen second pass (focused crop)."""
    if not is_enabled() or not alert_id:
        return None
    alert_dir = _alert_dir(alert_id)
    os.makedirs(alert_dir, exist_ok=True)
    out = os.path.join(alert_dir, "second_pass.json")
    with open(out, "w") as f:
        json.dump(response, f, indent=2, default=str)
    return out


def write_metadata(
    alert_id: str,
    *,
    camera: str,
    v_id: str,
    bbox: list[float] | None,
    best_frame_index: int | None,
    frame_paths: list[str],
    crop_path: str | None,
    v_first_pass_index: int | None = None,
) -> str | None:
    """Save the alert metadata."""
    if not is_enabled() or not alert_id:
        return None
    alert_dir = _alert_dir(alert_id)
    os.makedirs(alert_dir, exist_ok=True)
    metadata = {
        "alert_id": alert_id,
        "camera": camera,
        "v_id": v_id,
        "bbox": list(bbox) if bbox else None,
        "best_frame_index": best_frame_index,
        "n_captured_frames": len(frame_paths),
        "frame_paths_source": list(frame_paths),
        "v_first_pass_index": v_first_pass_index,
        "crop_path": crop_path,
        "artifacts_dir": alert_dir,
    }
    out = os.path.join(alert_dir, "metadata.json")
    with open(out, "w") as f:
        json.dump(metadata, f, indent=2)
    return out


def copy_frames(alert_id: str, frame_paths: list[str]) -> int:
    """Copy all captured frames into the artifacts dir. Returns count copied."""
    if not is_enabled() or not alert_id:
        return 0
    alert_dir = _alert_dir(alert_id)
    frames_dir = os.path.join(alert_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    n = 0
    for src in frame_paths or []:
        if not src or not os.path.exists(src):
            continue
        dst = os.path.join(frames_dir, os.path.basename(src))
        try:
            shutil.copyfile(src, dst)
            n += 1
        except OSError as err:
            log.warning(f"vehicle_artifacts: copy {src} -> {dst} failed: {err}")
    return n


def copy_crop(alert_id: str, crop_path: str | None) -> bool:
    """Copy the focused-classify crop into the artifacts dir as best_frame_crop.jpg."""
    if not is_enabled() or not alert_id or not crop_path:
        return False
    if not os.path.exists(crop_path):
        return False
    alert_dir = _alert_dir(alert_id)
    dst = os.path.join(alert_dir, "best_frame_crop.jpg")
    try:
        shutil.copyfile(crop_path, dst)
        return True
    except OSError as err:
        log.warning(f"vehicle_artifacts: copy crop {crop_path} -> {dst} failed: {err}")
        return False


def write_full_artifacts(
    alert_id: str,
    *,
    camera: str,
    v_id: str,
    frame_paths: list[str],
    bbox: list[float] | None,
    best_frame_index: int | None,
    crop_path: str | None,
    first_pass_prompt: str | None = None,
    first_pass_response: dict[str, Any] | None = None,
    second_pass_prompt: str | None = None,
    second_pass_response: dict[str, Any] | None = None,
    v_first_pass_index: int | None = None,
) -> str | None:
    """One-shot writer: every artifact for an alert in a single call.

    Returns the artifacts dir path, or None if persistence is disabled
    or alert_id is empty.
    """
    if not is_enabled() or not alert_id:
        return None
    write_metadata(
        alert_id,
        camera=camera,
        v_id=v_id,
        bbox=bbox,
        best_frame_index=best_frame_index,
        frame_paths=frame_paths,
        crop_path=crop_path,
        v_first_pass_index=v_first_pass_index,
    )
    if first_pass_prompt:
        write_first_pass_request(alert_id, frame_paths, first_pass_prompt)
    if first_pass_response is not None:
        write_first_pass_response(alert_id, first_pass_response)
    if second_pass_prompt and crop_path:
        write_second_pass_request(alert_id, crop_path, second_pass_prompt)
    if second_pass_response is not None:
        write_second_pass_response(alert_id, second_pass_response)
    n_frames = copy_frames(alert_id, frame_paths)
    crop_ok = copy_crop(alert_id, crop_path)
    log.info(
        f"vehicle_artifacts_written: alert_id={alert_id} v_id={v_id} "
        f"dir={_alert_dir(alert_id)} frames_copied={n_frames}/{len(frame_paths)} "
        f"first_pass_response={'yes' if first_pass_response is not None else 'no'} "
        f"second_pass_response={'yes' if second_pass_response is not None else 'no'} "
        f"crop_copied={'yes' if crop_ok else 'no'}"
    )
    return _alert_dir(alert_id)


def list_artifacts(alert_id: str) -> list[str]:
    """List artifact files for an alert (for postmortem grep)."""
    alert_dir = _alert_dir(alert_id)
    if not os.path.isdir(alert_dir):
        return []
    out: list[str] = []
    for root, _, files in os.walk(alert_dir):
        for f in files:
            out.append(os.path.join(root, f))
    return sorted(out)
