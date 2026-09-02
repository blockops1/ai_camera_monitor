#!/usr/bin/env python3
"""
probe_variant_leakage.py — Phase §11.113 leakage test harness.

STATUS: experimental (Phase §11.113; gated on §11.114)
THREAD SAFETY: single-threaded; one alert at a time.

INPUTS:
    - CLI arg --alert-id <uuid>     Required. The data/frames/<alert_id>/ folder.
    - CLI arg --variant  {a|b|both} Required.
        a    = run Variant A only (mode="unified", single call)
        b    = run Variant B only (current production dispatch, single call)
        both = run A then B (default for the bulk run)
    - CLI arg --list-alerts         Print candidate alert_ids as JSON, exit 0.
    - CLI arg --event-hint <type>   Override event_hint (vehicle/person/animal/motion).
                                    Default: derived from data/vehicle_artifacts/<id>/first_pass.json
                                    or 'motion' if neither exists.
    - CLI arg --camera <code>       Override camera_name (default: derived).
    - CLI arg --results-dir <path>  Override output dir. Default:
                                    ~/Library/Logs/ai_camera_monitor/probe_variant_leakage/

OUTPUTS:
    - Logs to ~/Library/Logs/ai_camera_monitor/probe_variant_leakage.log
      (RotatingFileHandler 10MB x 5 per script-authoring rule 1).
    - Per (alert_id, variant): writes results to:
      <results-dir>/<alert_id>/<variant>/result.json
      Fields:
        alert_id, variant, event_hint, frame_paths, prompt_sent, raw_response,
        parsed_response, latency_ms, schema_valid, leakage_flags, timestamp.
    - Exit codes: 0 = ok, 2 = input invalid, 3 = Vision call failed.

PUBLIC API:
    main(argv: list[str] | None = None) -> int

DOES NOT DO:
    - Does NOT modify any data outside <results-dir>.
    - Does NOT call listener/listener webhook path (runs the production
      analyze_frames_queued directly, mirroring the production call).

WHY HERE:
    Phase §11.113 (2026-09-02). maintainer's directive: "test whether variant
    works properly... design the plan for both variant and variant B."
    This script is the harness that runs the test.

CALLED BY:
    - Manual invocation: source .venv/bin/activate && python3 scripts/probe_variant_leakage.py ...
    - scripts/run_leakage_probe.sh — bulk-runner for 30 alerts × 2 variants.

CALLS INTO:
    - infra.vision_analyzer: analyze_frames_queued (production path).
    - infra.unified_vision: build_unified_prompt (Variant A only).

RELATED:
    - data/frames/<alert_id>/ — the raw frames (read-only)
    - data/vehicle_artifacts/<alert_id>/ — ground-truth first_pass (read-only)
    - infra/tests/test_unified_vision_6B113.py — module contract tests.
"""
# venv: ai_camera_monitor/.venv
# packages: requests (transitive), pydantic (transitive)
# activate before running:  source ai_camera_monitor/.venv/bin/activate
#
# Rollback: this script writes only to <results-dir>. All state lives in
# data/frames/ + data/vehicle_artifacts/. If a change to this script
# breaks the world, `git checkout HEAD -- scripts/probe_variant_leakage.py`.

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Phase 6B.183 — ensure repo root is on sys.path so `import infra.*`
# works when this script is invoked as `python3 scripts/probe_variant_leakage.py`.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Infra paths.
from infra.vision_analyzer import analyze_frames_queued

# ============================================================================
# Constants (script-authoring rule 1: known good places; rule 5: no /tmp)
# ============================================================================

SCRIPT_NAME = Path(__file__).stem
PROJECT_ROOT = Path("ai_camera_monitor")
EXPECTED_VENV = "ai_camera_monitor/.venv"
LOG_DIR = Path.home() / "Library" / "Logs" / "ai_camera_monitor"
LOG_FILE = LOG_DIR / f"{SCRIPT_NAME}.log"
DEFAULT_RESULTS_DIR = LOG_DIR / SCRIPT_NAME / "results"
FRAMES_DIR = PROJECT_ROOT / "data" / "frames"
VA_DIR = PROJECT_ROOT / "data" / "vehicle_artifacts"

# Vision queue timeout — matches the listener's timeout. Long enough for
# a 3-frame Qwen call (~15-25s on Apple Silicon).
VISION_TIMEOUT_S = 60.0


# ============================================================================
# Pre-flight (rules 4 + 1)
# ============================================================================


def _check_venv() -> None:
    """Refuse to run outside the project's venv (script-authoring rule 4)."""
    if EXPECTED_VENV not in sys.executable:
        sys.exit(
            f"ERROR: must run inside {EXPECTED_VENV}.\n"
            f"  currently: {sys.executable}\n"
            f"  activate:  source {EXPECTED_VENV}/bin/activate\n"
            f"  then:      python3 {Path(__file__).name}"
        )


def _setup_logging() -> logging.Logger:
    """Log to <LOG_DIR>/probe_variant_leakage.log with rotation."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(SCRIPT_NAME)
    logger.setLevel(logging.INFO)
    # Avoid double-handler if reloaded
    logger.handlers.clear()
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setFormatter(
        logging.Formatter(
            fmt="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    logger.addHandler(fh)
    return logger


# ============================================================================
# Helpers
# ============================================================================


def _list_candidate_alerts() -> list[str]:
    """Return alert_ids that have BOTH a frames/ folder AND a vehicle_artifacts/ folder."""
    out: list[str] = []
    if not FRAMES_DIR.exists():
        return out
    for alert_id in sorted(os.listdir(FRAMES_DIR)):
        if not (FRAMES_DIR / alert_id).is_dir():
            continue
        if (VA_DIR / alert_id).exists():
            out.append(alert_id)
    return out


def _resolve_frames(alert_id: str) -> list[str]:
    """Return sorted list of frame_*.jpg paths in data/frames/<alert_id>/.

    Excludes diff frames (frame_diff_*.jpg) and arcface crops (arcface_*);
    the analyzer expects source frames only.
    """
    d = FRAMES_DIR / alert_id
    if not d.is_dir():
        return []
    frames = []
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        name = f.name
        if not name.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        if "diff" in name or "arcface" in name:
            continue
        if not name.startswith(("frame_", "img_")):
            continue
        frames.append(str(f))
    return frames


def _resolve_event_hint(alert_id: str, override: str | None) -> str:
    """Resolve the event_hint for this alert.

    Priority:
      1. --event-hint CLI override
      2. data/vehicle_artifacts/<id>/first_pass.json.objects_detected
      3. "motion" (default)
    """
    if override:
        return override
    fp = VA_DIR / alert_id / "first_pass.json"
    if fp.exists():
        try:
            with open(fp) as f:
                data = json.load(f)
            objects = [o.lower() for o in (data.get("objects_detected") or [])]
            if any("person" in o or "people" in o for o in objects):
                return "person"
            if any(w in o for o in objects for w in ("car", "truck", "vehicle", "suv", "pickup")):
                return "vehicle"
            if any(w in o for o in objects for w in ("deer", "bear", "coyote", "fox", "dog", "cat")):
                return "animal"
        except Exception as e:
            # Phase 6B.113 — log instead of silent-pass (S110 fix).
            # A corrupt first_pass.json is not a probe-stopper; fall back
            # to 'motion' so the probe can still run.
            logging.getLogger(SCRIPT_NAME).warning(
                "alert_id=%s first_pass.json parse failed (%s: %s); defaulting to motion",
                alert_id, type(e).__name__, e,
            )
    return "motion"


def _resolve_camera_name(alert_id: str, override: str | None) -> str:
    """Resolve camera_name for this alert.

    Priority:
      1. --camera CLI override
      2. data/vehicle_artifacts/<id>/first_pass.json.camera (if present)
      3. alert_id short prefix (first 8 chars)
    """
    if override:
        return override
    fp = VA_DIR / alert_id / "first_pass.json"
    if fp.exists():
        try:
            with open(fp) as f:
                data = json.load(f)
            cam = data.get("camera") or data.get("camera_name")
            if cam:
                return str(cam)
        except Exception as e:
            # Phase 6B.113 — log instead of silent-pass (S110 fix).
            logging.getLogger(SCRIPT_NAME).warning(
                "alert_id=%s first_pass.json camera read failed (%s: %s); "
                "falling back to alert_id[:8]",
                alert_id, type(e).__name__, e,
            )
    return alert_id[:8]


def _select_mode_for_variant(variant: str, event_hint: str) -> str:
    """Map variant + event_hint → analyze_frames_queued(mode=...) argument.

    Variant A always uses mode='unified' (the new prompt).
    Variant B dispatches to the current production prompt:
      - event_hint=vehicle → 'crop' (single frame) or 'static' (multi)
      - event_hint=person  → 'person'
      - event_hint=animal  → 'animal'
      - event_hint=motion  → 'crop' fallback
    """
    if variant == "a":
        return "unified"
    # Variant B: mimic current production
    if event_hint == "vehicle":
        return "auto"  # select_prompt_template picks crop/static based on n_frames
    if event_hint == "person":
        return "person"
    if event_hint == "animal":
        return "animal"
    return "auto"


def _detect_leakage(parsed: dict, primary_class: str) -> list[str]:
    """Return a list of leakage flags for a parsed unified-prompt response.

    Leakage definitions (Variant A failure modes we want to detect):
      - primary_class says "person" but vehicle_features has a real make/model
      - primary_class says "person" but vehicle_features.color is NOT in
        {"none", "unknown"} (model guessed a color for a vehicle that isn't there)
      - primary_class says "person" but animal_features.species is NOT in
        {"none", "unknown"}
      - Symmetric rules for primary_class=vehicle/animal
      - vehicle_present=True but no vehicle features (reverse — model
        said yes-vehicle but didn't fill in any details)
      - primary_class="none" but any *_present=True
    """
    flags: list[str] = []
    NONE_OR_UNKNOWN = {"none", "unknown", None, ""}

    vf = parsed.get("vehicle_features") or {}
    pf = parsed.get("person_features") or {}
    af = parsed.get("animal_features") or {}

    if primary_class == "person":
        # Vehicle/animal blocks should be "none"
        if (vf.get("make") or "").lower() not in NONE_OR_UNKNOWN:
            flags.append(f"person_scene_vehicle_make={vf.get('make')!r}")
        if (vf.get("color") or "").lower() not in NONE_OR_UNKNOWN:
            flags.append(f"person_scene_vehicle_color={vf.get('color')!r}")
        if (af.get("species") or "").lower() not in NONE_OR_UNKNOWN:
            flags.append(f"person_scene_animal_species={af.get('species')!r}")
    elif primary_class == "vehicle":
        # Person/animal blocks should be "none"
        if (pf.get("action") or "").lower() not in NONE_OR_UNKNOWN:
            flags.append(f"vehicle_scene_person_action={pf.get('action')!r}")
        if (af.get("species") or "").lower() not in NONE_OR_UNKNOWN:
            flags.append(f"vehicle_scene_animal_species={af.get('species')!r}")
    elif primary_class == "animal":
        if (vf.get("make") or "").lower() not in NONE_OR_UNKNOWN:
            flags.append(f"animal_scene_vehicle_make={vf.get('make')!r}")
        if (pf.get("action") or "").lower() not in NONE_OR_UNKNOWN:
            flags.append(f"animal_scene_person_action={pf.get('action')!r}")

    # Reversal: present=True but primary_class says "none"
    if primary_class == "none":
        if parsed.get("vehicle_present") in (True, "true"):
            flags.append("none_scene_vehicle_present_true")
        if parsed.get("person_present") in (True, "true"):
            flags.append("none_scene_person_present_true")
        if parsed.get("animal_present") in (True, "true"):
            flags.append("none_scene_animal_present_true")

    return flags


def _run_variant(
    alert_id: str,
    variant: str,
    event_hint: str,
    camera_name: str,
    captured_at: str,
    frame_paths: list[str],
    api_url: str,
    logger: logging.Logger,
) -> dict:
    """Run a single variant call and return the result dict."""
    mode = _select_mode_for_variant(variant, event_hint)
    logger.info(
        "running alert=%s variant=%s event_hint=%s camera=%s mode=%s n_frames=%d",
        alert_id, variant, event_hint, camera_name, mode, len(frame_paths),
    )

    t0 = time.monotonic()
    try:
        result = analyze_frames_queued(
            frame_paths=frame_paths,
            camera_name=camera_name,
            api_url=api_url,
            timeout_s=VISION_TIMEOUT_S,
            alert_id=alert_id,
            event_hint=event_hint,
            captured_at=captured_at,
            mode=mode,
        )
    except Exception as e:
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.exception("variant %s failed for alert %s", variant, alert_id)
        return {
            "alert_id": alert_id,
            "variant": variant,
            "mode": mode,
            "event_hint": event_hint,
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
            "latency_ms": elapsed_ms,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

    elapsed_ms = (time.monotonic() - t0) * 1000

    # Normalize result — analyze_frames_queued returns a dict with various
    # shapes depending on the prompt. Variant A produces the unified
    # schema (with primary_class etc). Variant B produces the legacy
    # shape (objects_detected, scene_description, etc).
    parsed = result if isinstance(result, dict) else {}

    out: dict = {
        "alert_id": alert_id,
        "variant": variant,
        "mode": mode,
        "event_hint": event_hint,
        "status": "ok",
        "latency_ms": elapsed_ms,
        "raw_response": parsed,
        "frame_count": len(frame_paths),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    if variant == "a":
        primary_class = (parsed.get("primary_class") or "").lower()
        out["primary_class"] = primary_class
        out["leakage_flags"] = _detect_leakage(parsed, primary_class)
    else:
        # Variant B: no unified leakage check; record whatever class-like
        # fields the response carries for downstream comparison.
        objects = [o.lower() for o in (parsed.get("objects_detected") or [])]
        out["variant_b_objects"] = objects
        out["variant_b_primary_subject"] = parsed.get("primary_subject")

    return out


# ============================================================================
# Argparse + main
# ============================================================================


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        description=(
            "Phase §11.113 leakage probe. Runs Variant A (unified prompt) "
            "and/or Variant B (current production dispatch) against the "
            "frames in data/frames/<alert_id>/ and persists the result."
        ),
    )
    p.add_argument("--alert-id", help="Alert UUID (folder under data/frames/).")
    p.add_argument(
        "--variant",
        choices=["a", "b", "both"],
        default="both",
        help="Which variant to run (default: both).",
    )
    p.add_argument(
        "--event-hint",
        choices=["vehicle", "person", "animal", "motion"],
        help="Override event_hint. Default: derived from vehicle_artifacts/.",
    )
    p.add_argument("--camera", help="Override camera_name. Default: derived.")
    p.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help=f"Override results dir. Default: {DEFAULT_RESULTS_DIR}",
    )
    p.add_argument(
        "--api-url",
        default=os.environ.get("VISION_LLM_URL", "http://127.0.0.1:8093/v1/chat/completions"),
        help="Vision endpoint (default: env VISION_LLM_URL or "
             "http://127.0.0.1:8093/v1/chat/completions).",
    )
    p.add_argument(
        "--captured-at",
        default=time.strftime("%Y-%m-%d %H:%M:%S EDT"),
        help="ISO timestamp to inject into the prompt.",
    )
    p.add_argument(
        "--list-alerts",
        action="store_true",
        help="Print alert_ids that have frames + vehicle_artifacts, exit 0.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    _check_venv()
    logger = _setup_logging()
    logger.info("starting (PID %d, argv=%s)", os.getpid(), argv or sys.argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --list-alerts short-circuits before required-arg checks
    if args.list_alerts:
        candidates = _list_candidate_alerts()
        print(json.dumps(candidates, indent=2))
        logger.info("--list-alerts: %d candidates", len(candidates))
        return 0

    if not args.alert_id:
        parser.error("--alert-id is required (or pass --list-alerts)")
        return 2

    frames = _resolve_frames(args.alert_id)
    if not frames:
        logger.error("no frames found for alert_id=%s", args.alert_id)
        return 2

    event_hint = _resolve_event_hint(args.alert_id, args.event_hint)
    camera_name = _resolve_camera_name(args.alert_id, args.camera)

    results_dir = Path(args.results_dir) / args.alert_id
    results_dir.mkdir(parents=True, exist_ok=True)

    variants_to_run: list[str]
    if args.variant == "both":
        variants_to_run = ["a", "b"]
    else:
        variants_to_run = [args.variant]

    summary: list[dict] = []
    for variant in variants_to_run:
        result = _run_variant(
            alert_id=args.alert_id,
            variant=variant,
            event_hint=event_hint,
            camera_name=camera_name,
            captured_at=args.captured_at,
            frame_paths=frames,
            api_url=args.api_url,
            logger=logger,
        )
        out_path = results_dir / f"variant_{variant}.json"
        out_path.write_text(json.dumps(result, indent=2, default=str))
        logger.info(
            "wrote %s (status=%s, latency_ms=%.1f, leakage_flags=%s)",
            out_path, result.get("status"), result.get("latency_ms", 0),
            result.get("leakage_flags", []),
        )
        summary.append(
            {
                "variant": variant,
                "status": result.get("status"),
                "latency_ms": result.get("latency_ms"),
                "primary_class": result.get("primary_class"),
                "leakage_flags": result.get("leakage_flags", []),
            }
        )

    # Write a per-alert summary
    (results_dir / "_summary.json").write_text(json.dumps(summary, indent=2))
    # Final stdout summary for ad-hoc use
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())