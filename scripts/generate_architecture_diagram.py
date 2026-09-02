#!/usr/bin/env python3
"""generate_architecture_diagram.py — regenerate listener-architecture.html.

Emits a dark-themed HTML+SVG module map of the refactor tree, built from the
actual intra-project import graph (greps `from X import` lines across all
production .py files in infra/, listener/, pipeline/, telegram_formatter/,
vehicle_matcher/, vehicle_position/, vehicle_identifier/, known_vehicles/).

Run from repo root:
    cd ai_camera_monitor && ./.venv/bin/python scripts/generate_architecture_diagram.py

Writes ./listener-architecture.html (overwrites).

Design system: see creative/architecture-diagram skill.
Layout follows the previous regenerated diagram (2026-08-12) — three
boundaries (EXTERNAL, LISTENER, INFRA) left-to-right, grouped boxes for
infra concerns. Updated to reflect the 6 module splits done in Part 9.

Part 9 splits reflected here:
  1. notifier         → send_telegram, audit_telegram (audit renamed)
  2. vehicle_matcher  → matcher_spec, matcher_scoring, matcher_telemetry
  3. heartbeat        → vision_cache
  4. vision_analyzer  → vision_client, vision_queue,
                       vision_response, prompt_templates, llm_config
  5. alert_generator  → alert_prompt, alert_overrides_baseline,
                        alert_overrides_offhours
  6. frame_capture    → image_prep, persistent_rtsp, frame_selector

STATUS: provisional — regenerator, run on demand after structural changes.
THREAD SAFETY: single-threaded — runs as a one-shot CLI script.
INPUTS:
  - filesystem: ./infra/, ./listener/, ./pipeline/, ./telegram_formatter/,
    ./vehicle_matcher/, ./vehicle_position/, ./vehicle_identifier/,
    ./known_vehicles/ (all under repo root)
  - argv: none
OUTPUTS:
  - file: ./listener-architecture.html (overwrites)
PUBLIC API:
  - main(): regenerate the HTML in place
DOES NOT DO:
  - parse non-`from X import Y` import styles (import X; from X import (a, b))
    — the layout treats any resolved import whose target is in our prod set
    as an edge, but multi-line `from` blocks are skipped. Acceptable: the
    modules we care about for the diagram use single-line imports.
  - diff against the previous HTML (visual diff check is manual)
CALLED BY: cron-free; manual run by Jill when module boundaries change.
CALLS INTO: stdlib only (os, re, datetime, html, pathlib).
RELATED: AGENTS.md §2 (source-of-truth docs).
"""
from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOTS = (
    "infra",
    "listener",
    "pipeline",
    "telegram_formatter",
    "vehicle_matcher",
    "vehicle_position",
    "vehicle_identifier",
    "known_vehicles",
)

# ---------------------------------------------------------------------------
# Step 1 — collect production modules + intra-project import edges
# ---------------------------------------------------------------------------

def collect_modules() -> tuple[set[str], set[tuple[str, str]]]:
    """Walk ROOTS, return (production_modules, edges)."""
    prod: set[str] = set()
    all_mods: set[str] = set()
    for root in ROOTS:
        root_path = REPO_ROOT / root
        if not root_path.exists():
            continue
        for dirpath, _, filenames in os.walk(root_path):
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                if fn == "__init__.py":
                    continue
                path = Path(dirpath) / fn
                rel = path.relative_to(REPO_ROOT).with_suffix("").as_posix()
                mod = rel.replace("/", ".")
                is_test = "/tests/" in str(path) or fn.startswith("test_")
                all_mods.add(mod)
                if not is_test:
                    prod.add(mod)

    # First pass: pre-populate all_mods with prod so cross-references resolve.
    edges: set[tuple[str, str]] = set()
    for mod in sorted(prod):
        path = REPO_ROOT / (mod.replace(".", "/") + ".py")
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        for line in text.splitlines():
            # Single-line: from X import Y
            m = re.match(r"^\s*from\s+([\w.]+)\s+import\s+", line)
            if not m:
                continue
            target = m.group(1)
            if target in prod and target != mod:
                edges.add((mod, target))
    return prod, edges


# ---------------------------------------------------------------------------
# Step 2 — group infra modules into the 7 functional clusters shown on canvas
# ---------------------------------------------------------------------------

GROUPS: list[tuple[str, str, list[str]]] = [
    (
        "FOUNDATIONS",
        "cyan",
        [
            "infra.paths",
            "infra.telegram_creds",
            "infra.camera_creds",
            "infra.camera_aliases",
            "infra.logging_setup",
            "infra.audit_telegram",
            "infra.alert_history",
            "infra.camera_queue",
            "infra.cooldown",
            "infra.cleanup",
            "infra.send_telegram",
            "infra.notifier",
            "infra.timezone",
            "infra.time_of_day",
            "infra.matcher_failures",
        ],
    ),
    (
        "FRAME CAPTURE PATH",
        "emerald",
        [
            "infra.frame_capture",
            "infra.image_prep",
            "infra.persistent_rtsp",
            "infra.frame_diff",
            "infra.motion_types",
            "infra.motion_visualization",
            "vehicle_position.crop_extractor",
            "vehicle_position.motion_detector",
            "vehicle_position.motion_detector_impl",
        ],
    ),
    (
        "MOTION GATE (Phase 6B.107 + 6B.116)",
        "amber",
        [
            "infra.quick_classifier",
        ],
    ),
    (
        "VISION PIPELINE",
        "violet",
        [
            "infra.vision_analyzer",
            "infra.vision_client",
            "infra.vision_queue",
            "infra.vision_response",
            "infra.prompt_templates",
            "infra.vision_cache",
            "infra.vision_schema_lift",
            "infra.matcher_spec",
            "infra.matcher_scoring",
            "infra.matcher_telemetry",
        ],
    ),
    (
        "ALERT GENERATION",
        "amber",
        [
            "infra.alert_generator",
            "infra.alert_prompt",
            "infra.alert_overrides_baseline",
            "infra.alert_overrides_offhours",
            "infra.quiet_hours",
        ],
    ),
    (
        "VEHICLE IDENTIFIER (Phase 6A)",
        "rose",
        [
            "vehicle_identifier.identifier",
            "vehicle_identifier.prompt_template",
            "vehicle_identifier.signature",
            "vehicle_identifier.vision_client",
        ],
    ),
    (
        "PERSON PIPELINE (Phase 6A)",
        "rose",
        [
            "infra.face_recognition",
            "infra.faces",
            "infra.camera_audio",
            "infra.person_matcher",
            "infra.person_prompt_template",
        ],
    ),
    (
        "VEHICLE MATCHER + TELEGRAM FORMAT",
        "slate",
        [
            "vehicle_matcher.matcher",
            "vehicle_matcher.scoring",
            "known_vehicles.store",
            "pipeline.orchestrator",
            "pipeline._legacy_match_adapter",
            "telegram_formatter.match_telegram",
            "telegram_formatter.motion_telegram",
            "telegram_formatter.no_match_telegram",
            "telegram_formatter.render_qwen",
            "telegram_formatter.composite_telegram",
            "telegram_formatter.vehicle_alert",
            "infra.synology_preview",
        ],
    ),
    (
        "ORCHESTRATION + HEARTBEAT",
        "cyan",
        [
            "infra.pipeline_integration",
            "infra.heartbeat",
        ],
    ),
    (
        "LISTENER PIPELINES (post Part 9 split)",
        "violet",
        [
            "listener.motion_gate_pipeline",
            "listener._gate_aware_capture",
            "listener._motion_gate_dispatch",
            "listener.vehicle_event_pipeline",
            "listener.person_event_pipeline",
            "listener.state",
        ],
    ),
]


# ---------------------------------------------------------------------------
# Step 3 — render SVG + cards + footer
# ---------------------------------------------------------------------------

# Layout constants (canvas coords)
EXT_X, EXT_Y, EXT_W, EXT_H = 20, 130, 280, 720
LIS_X, LIS_Y, LIS_W, LIS_H = 340, 130, 320, 720
INF_X, INF_Y, INF_W, INF_H = 700, 130, 660, 720
CANVAS_W = 1380
CANVAS_H = 880

# Alert flow stages — numbered lifecycle. The motion gate sits at stage 2 to
# make it visually clear that the gate runs BEFORE vision/matcher/telegram.
FLOW_STAGES: list[tuple[str, str, str]] = [
    ("1", "Camera", "#94a3b8"),
    ("2", "Motion Gate", "#fbbf24"),
    ("3", "Vision (Qwen)", "#a78bfa"),
    ("4", "Matcher", "#fb7185"),
    ("5", "Telegram", "#22d3ee"),
]


def render_svg(prod: set[str], edges: set[tuple[str, str]]) -> tuple[str, int]:
    """Emit the main SVG block (boundaries, boxes, arrows, legend).

    Returns (svg_text, canvas_h) — canvas_h is the dynamic height needed to
    fit all group boxes plus padding for the legend.
    """
    cols = 5
    box_h = 36
    box_gap_y = 8
    header_h = 28
    padding_top = 18
    padding_bottom = 14
    row_pitch = box_h + box_gap_y

    # ---- Module box geometry (compute per-group box positions) ----
    group_boxes: list[tuple[str, str, int, int, int, int]] = []
    cur_y = 170  # below the flow strip (y=20-110)
    for label, color, mods in GROUPS:
        mods = [m for m in mods if m in prod]
        rows = (len(mods) + cols - 1) // cols
        inner_h = header_h + rows * row_pitch
        h = inner_h + padding_top + padding_bottom
        group_boxes.append((label, color, INF_X + 20, cur_y, INF_W - 40, h))
        cur_y += h + 10

    # Dynamic canvas height — content + legend room
    canvas_h = max(CANVAS_H, cur_y + 60)

    boxes_html: list[str] = []

    # ---- infra module boxes ----
    for label, color, gx, gy, gw, gh in group_boxes:
        boxes_html.append(
            f'<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" rx="8" '
            f'fill="rgba(15, 23, 42, 0.3)" stroke="#475569" stroke-width="0.5" '
            f'stroke-dasharray="2,2"/>'
        )
        boxes_html.append(
            f'<text x="{gx + 10}" y="{gy + 18}" fill="#94a3b8" font-size="9" '
            f'font-weight="600">{label}</text>'
        )

    # Place each module as a small rounded rect within its group box.
    mod_xy: dict[str, tuple[int, int]] = {}
    box_w = 110
    box_gap_x = 8
    for label, color, gx, gy, gw, gh in group_boxes:
        mods = next(m for l, _, m in GROUPS if l == label)
        # Skip entries that aren't actually modules
        mods = [m for m in mods if m in prod]
        for i, mod in enumerate(mods):
            col = i % cols
            row = i // cols
            mx = gx + 10 + col * (box_w + box_gap_x)
            my = gy + header_h + row * row_pitch
            mod_xy[mod] = (mx + box_w // 2, my + box_h // 2)
            short = mod.rsplit(".", 1)[-1]
            # Top-level package for the sublabel (e.g. "infra", "vehicle_matcher")
            pkg = mod.split(".", 1)[0]
            # Shrink font for very long names so they fit in the 110px box
            short_size = 9 if len(short) <= 14 else 7.5
            boxes_html.append(
                f'<rect x="{mx}" y="{my}" width="{box_w}" height="{box_h}" rx="4" '
                f'fill="rgba(6, 78, 59, 0.4)" stroke="#34d399" stroke-width="1"/>'
            )
            boxes_html.append(
                f'<text x="{mx + box_w // 2}" y="{my + 16}" fill="white" '
                f'font-size="{short_size}" font-weight="500" text-anchor="middle">{short}</text>'
            )
            boxes_html.append(
                f'<text x="{mx + box_w // 2}" y="{my + 29}" fill="#94a3b8" '
                f'font-size="7" text-anchor="middle">{pkg}</text>'
            )

    # ---- arrows: listener → each module it imports ----
    # Anchor listener box at center of LISTENER region
    listener_anchor = (LIS_X + LIS_W - 4, LIS_Y + 60)
    for s, t in sorted(edges):
        if s != "listener.listener":
            continue
        if t not in mod_xy:
            continue
        tx, ty = mod_xy[t]
        short = t.rsplit(".", 1)[-1]
        boxes_html.append(
            f'<line x1="{listener_anchor[0]}" y1="{listener_anchor[1]}" '
            f'x2="{tx - 55}" y2="{ty}" stroke="#34d399" stroke-width="0.6" '
            f'stroke-opacity="0.6" marker-end="url(#arrow-emerald-thin)"/>'
        )

    # ---- listener.py header box ----
    lis_box_x, lis_box_y = LIS_X + 40, LIS_Y + 90
    lis_box_w, lis_box_h = LIS_W - 80, 80
    boxes_html.append(
        f'<rect x="{lis_box_x}" y="{lis_box_y}" width="{lis_box_w}" '
        f'height="{lis_box_h}" rx="8" fill="rgba(8, 51, 68, 0.6)" '
        f'stroke="#22d3ee" stroke-width="2"/>'
    )
    listener_imports = count_listener_imports()
    boxes_html.append(
        f'<text x="{lis_box_x + lis_box_w // 2}" y="{lis_box_y + 25}" '
        f'fill="white" font-size="14" font-weight="700" text-anchor="middle">'
        f'listener.py</text>'
    )
    boxes_html.append(
        f'<text x="{lis_box_x + lis_box_w // 2}" y="{lis_box_y + 45}" '
        f'fill="#22d3ee" font-size="10" text-anchor="middle">'
        f'{count_listener_lines()} lines &middot; composition root</text>'
    )
    boxes_html.append(
        f'<text x="{lis_box_x + lis_box_w // 2}" y="{lis_box_y + 65}" '
        f'fill="#94a3b8" font-size="9" text-anchor="middle">'
        f'{listener_imports} infra modules imported &middot; Flask :8090</text>'
    )

    # ---- external boundary (cameras, telegram api, qwen, fs) ----
    ext_items = [
        (90, 180, "Reolink Cameras (6)", "HTTP POST /alert", "RTSP :554"),
        (90, 410, "Telegram Bot API", "api.telegram.org", "sendMessage / sendPhoto"),
        (90, 600, "Qwen3.6-35B-A3B LLM Server", "127.0.0.1:8093", "/v1/chat/completions"),
        (90, 720, "Refactor Filesystem", "data/frames/, audit/,", "config/known_vehicles.json"),
    ]
    for x, y, name, l1, l2 in ext_items:
        boxes_html.append(
            f'<rect x="{x}" y="{y}" width="220" height="70" rx="6" '
            f'fill="rgba(30, 41, 59, 0.5)" stroke="#94a3b8" stroke-width="1.5"/>'
        )
        boxes_html.append(
            f'<text x="{x + 110}" y="{y + 23}" fill="white" font-size="12" '
            f'font-weight="600" text-anchor="middle">{name}</text>'
        )
        boxes_html.append(
            f'<text x="{x + 110}" y="{y + 40}" fill="#94a3b8" font-size="9" '
            f'text-anchor="middle">{l1}</text>'
        )
        boxes_html.append(
            f'<text x="{x + 110}" y="{y + 54}" fill="#94a3b8" font-size="9" '
            f'text-anchor="middle">{l2}</text>'
        )

    # ---- alert flow strip ----
    # Five-stage numbered lifecycle: Camera → Motion Gate → Vision → Matcher → Telegram
    # Drawn at y=30-110 so it sits above the module-area boundaries (which start at y=130).
    flow_y = 40
    flow_h = 70
    stage_w = 200
    stage_gap = 30
    total_flow_w = len(FLOW_STAGES) * stage_w + (len(FLOW_STAGES) - 1) * stage_gap
    flow_start_x = (CANVAS_W - total_flow_w) // 2
    flow_centers: list[tuple[int, int]] = []
    for i, (num, name, color) in enumerate(FLOW_STAGES):
        sx = flow_start_x + i * (stage_w + stage_gap)
        sy = flow_y
        boxes_html.append(
            f'<rect x="{sx}" y="{sy}" width="{stage_w}" height="{flow_h}" rx="6" '
            f'fill="rgba(15, 23, 42, 0.5)" stroke="{color}" stroke-width="2"/>'
        )
        # Number badge (top-left)
        boxes_html.append(
            f'<circle cx="{sx + 18}" cy="{sy + 18}" r="11" '
            f'fill="{color}" fill-opacity="0.85"/>'
        )
        boxes_html.append(
            f'<text x="{sx + 18}" y="{sy + 22}" fill="#020617" font-size="13" '
            f'font-weight="700" text-anchor="middle">{num}</text>'
        )
        # Stage name (center)
        boxes_html.append(
            f'<text x="{sx + stage_w // 2}" y="{sy + 30}" fill="white" '
            f'font-size="13" font-weight="600" text-anchor="middle">{name}</text>'
        )
        # Stage description
        descs = {
            "Camera":     "HTTP POST /alert",
            "Motion Gate": "yolov8n.onnx pre-Qwen",
            "Vision (Qwen)": "if gate=pass",
            "Matcher":    "score vs known",
            "Telegram":   "sendMessage",
        }
        boxes_html.append(
            f'<text x="{sx + stage_w // 2}" y="{sy + 48}" fill="#94a3b8" '
            f'font-size="9" text-anchor="middle">{descs[name]}</text>'
        )
        # Decision flag for stage 2 (Motion Gate)
        if name == "Motion Gate":
            boxes_html.append(
                f'<text x="{sx + stage_w // 2}" y="{sy + 62}" fill="#fbbf24" '
                f'font-size="8" font-weight="600" text-anchor="middle">'
                f'decision: pass / suppress</text>'
            )
        flow_centers.append((sx + stage_w // 2, sy + flow_h // 2))

    # Arrows between stages
    for i in range(len(flow_centers) - 1):
        x1, y1 = flow_centers[i]
        x2, y2 = flow_centers[i + 1]
        # Start arrow from right edge of current stage to left edge of next
        ax1 = x1 + stage_w // 2
        ax2 = x2 - stage_w // 2
        marker_color = "#fbbf24" if FLOW_STAGES[i + 1][1] == "Motion Gate" else "#64748b"
        marker_id = "arrow-flow-motion" if FLOW_STAGES[i + 1][1] == "Motion Gate" else "arrow-flow"
        boxes_html.append(
            f'<line x1="{ax1}" y1="{y1}" x2="{ax2}" y2="{y2}" '
            f'stroke="{marker_color}" stroke-width="2" '
            f'marker-end="url(#{marker_id})"/>'
        )

    # Flow strip header label
    boxes_html.append(
        f'<text x="{flow_start_x}" y="{flow_y - 12}" fill="#94a3b8" '
        f'font-size="11" font-weight="600">ALERT LIFECYCLE &middot; '
        f'motion gate runs at stage 2 before vision/matcher</text>'
    )

    # ---- boundaries (drawn last so they overlay cleanly) ----
    boundaries = [
        (EXT_X, EXT_Y, EXT_W, EXT_H, "EXTERNAL", "#94a3b8"),
        (LIS_X, LIS_Y, LIS_W, LIS_H, "LISTENER  (composition root)", "#22d3ee"),
        (INF_X, INF_Y, INF_W, INF_H, "INFRA  (split modules)", "#34d399"),
    ]
    for x, y, w, h, label, color in boundaries:
        boxes_html.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" '
            f'fill="none" stroke="{color}" stroke-width="1" stroke-dasharray="6,4"/>'
        )
        boxes_html.append(
            f'<text x="{x + 16}" y="{y + 22}" fill="{color}" font-size="11" '
            f'font-weight="600">{label}</text>'
        )

    # ---- legend ----
    legend_y = canvas_h - 25
    boxes_html.append(
        f'<text x="60" y="{legend_y - 12}" fill="white" font-size="10" '
        f'font-weight="600">Legend</text>'
    )
    boxes_html.append(
        f'<line x1="60" y1="{legend_y}" x2="86" y2="{legend_y}" stroke="#22d3ee" '
        f'stroke-width="1.5" marker-end="url(#arrow)"/>'
    )
    boxes_html.append(
        f'<text x="92" y="{legend_y + 3}" fill="#94a3b8" font-size="8">'
        f'External &rarr; Listener</text>'
    )
    boxes_html.append(
        f'<line x1="240" y1="{legend_y}" x2="266" y2="{legend_y}" stroke="#34d399" '
        f'stroke-width="1.5" marker-end="url(#arrow-emerald)"/>'
    )
    boxes_html.append(
        f'<text x="272" y="{legend_y + 3}" fill="#94a3b8" font-size="8">'
        f'Listener &rarr; Infra (intra-project)</text>'
    )
    boxes_html.append(
        f'<line x1="450" y1="{legend_y}" x2="476" y2="{legend_y}" stroke="#a78bfa" '
        f'stroke-width="1.5" marker-end="url(#arrow)"/>'
    )
    boxes_html.append(
        f'<text x="482" y="{legend_y + 3}" fill="#94a3b8" font-size="8">'
        f'Infra &rarr; External (network/disk)</text>'
    )
    boxes_html.append(
        f'<text x="700" y="{legend_y + 3}" fill="#fb7185" font-size="9">'
        f'6 Part 9 splits complete &middot; see PLAN.md Part 9</text>'
    )

    svg = (
        f'<svg viewBox="0 0 {CANVAS_W} {canvas_h}">\n'
        '  <defs>\n'
        '    <marker id="arrow" markerWidth="10" markerHeight="7" refX="9" '
        'refY="3.5" orient="auto">\n'
        '      <polygon points="0 0, 10 3.5, 0 7" fill="#64748b"/>\n'
        '    </marker>\n'
        '    <marker id="arrow-emerald" markerWidth="10" markerHeight="7" refX="9" '
        'refY="3.5" orient="auto">\n'
        '      <polygon points="0 0, 10 3.5, 0 7" fill="#34d399"/>\n'
        '    </marker>\n'
        '    <marker id="arrow-emerald-thin" markerWidth="8" markerHeight="6" '
        'refX="8" refY="3" orient="auto">\n'
        '      <polygon points="0 0, 8 3, 0 6" fill="#34d399" fill-opacity="0.6"/>\n'
        '    </marker>\n'
        '    <marker id="arrow-flow" markerWidth="10" markerHeight="7" refX="9" '
        'refY="3.5" orient="auto">\n'
        '      <polygon points="0 0, 10 3.5, 0 7" fill="#64748b"/>\n'
        '    </marker>\n'
        '    <marker id="arrow-flow-motion" markerWidth="10" markerHeight="7" refX="9" '
        'refY="3.5" orient="auto">\n'
        '      <polygon points="0 0, 10 3.5, 0 7" fill="#fbbf24"/>\n'
        '    </marker>\n'
        '    <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">\n'
        '      <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e293b" '
        'stroke-width="0.5"/>\n'
        '    </pattern>\n'
        '  </defs>\n'
        f'  <rect width="100%" height="100%" fill="url(#grid)"/>\n'
        + "\n".join(f"  {line}" for line in boxes_html)
        + "\n</svg>"
    )
    return svg, canvas_h


def count_listener_lines() -> int:
    """Return the line count of listener/listener.py."""
    path = REPO_ROOT / "listener" / "listener.py"
    try:
        return sum(1 for _ in path.open(encoding="utf-8"))
    except FileNotFoundError:
        return 0


def count_listener_imports() -> int:
    """Return the number of distinct infra modules listener.py imports."""
    import re
    path = REPO_ROOT / "listener" / "listener.py"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    mods = set()
    for line in text.splitlines():
        m = re.match(r"^\s*from\s+(infra\.[\w.]+)\s+import", line)
        if m:
            mods.add(m.group(1))
    return len(mods)


def render_html(svg: str) -> str:
    """Wrap the SVG in the dark-themed HTML shell."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Refactor Listener — Module Logic Diagram</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: 'JetBrains Mono', monospace;
      background: #020617;
      min-height: 100vh;
      padding: 2rem;
      color: white;
    }}
    .container {{ max-width: 1400px; margin: 0 auto; }}
    .header {{ margin-bottom: 2rem; }}
    .header-row {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 0.5rem; }}
    .pulse-dot {{
      width: 12px; height: 12px; background: #34d399; border-radius: 50%;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}
    h1 {{ font-size: 1.5rem; font-weight: 700; letter-spacing: -0.025em; }}
    .subtitle {{ color: #94a3b8; font-size: 0.875rem; margin-left: 1.75rem; }}
    .diagram-container {{
      background: rgba(15, 23, 42, 0.5);
      border-radius: 1rem;
      border: 1px solid #1e293b;
      padding: 1.5rem;
      overflow-x: auto;
    }}
    svg {{ width: 100%; min-width: 1200px; display: block; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1rem;
      margin-top: 2rem;
    }}
    .card {{
      background: rgba(15, 23, 42, 0.5);
      border-radius: 0.75rem;
      border: 1px solid #1e293b;
      padding: 1.25rem;
    }}
    .card-header {{
      display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.75rem;
    }}
    .card-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
    .card-dot.cyan {{ background: #22d3ee; }}
    .card-dot.emerald {{ background: #34d399; }}
    .card-dot.violet {{ background: #a78bfa; }}
    .card-dot.amber {{ background: #fbbf24; }}
    .card-dot.rose {{ background: #fb7185; }}
    .card-dot.slate {{ background: #94a3b8; }}
    .card h3 {{ font-size: 0.875rem; font-weight: 600; }}
    .card ul {{ list-style: none; color: #94a3b8; font-size: 0.75rem; }}
    .card li {{ margin-bottom: 0.375rem; }}
    .footer {{
      text-align: center; margin-top: 1.5rem; color: #475569; font-size: 0.75rem;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="header-row">
        <div class="pulse-dot"></div>
        <h1>Refactor Listener — Module Logic</h1>
      </div>
      <p class="subtitle">ai_camera_monitor/ &middot; alert lifecycle (top) + intra-project import graph (below) &middot; post Part 9 split</p>
    </div>

    <div class="diagram-container">
      {svg}
    </div>

    <div class="cards">
      <div class="card">
        <div class="card-header">
          <div class="card-dot cyan"></div>
          <h3>Listener (composition root)</h3>
        </div>
        <ul>
          <li>&bull; 1 file: listener/listener.py</li>
          <li>&bull; {count_listener_lines()} lines</li>
          <li>&bull; Imports {count_listener_imports()} infra modules at runtime</li>
          <li>&bull; Flask app on :8090</li>
          <li>&bull; 4 routes: /alert, /health, /status, /static</li>
          <li>&bull; Owns: cooldown, queue executor, payload normalization</li>
          <li>&bull; Phase 6B.115: pipeline extracted to listener/* modules</li>
        </ul>
      </div>

      <div class="card">
        <div class="card-header">
          <div class="card-dot amber"></div>
          <h3>Motion gate (Phase 6B.107 + 6B.116)</h3>
        </div>
        <ul>
          <li>&bull; YOLOv8n ONNX pre-Qwen classifier</li>
          <li>&bull; Runs before pipeline to skip noise</li>
          <li>&bull; 6B.116 night heuristic gate:
            <ul style="margin-left: 1rem; margin-top: 0.25rem;">
              <li>&bull; MOTION_GATE_NIGHT_SUPPRESS_ENABLED=1</li>
              <li>&bull; Conf floor 0.40 + implausible-class filter</li>
              <li>&bull; Brightness ratio gate (bottom/top &gt; 1.5)</li>
            </ul>
          </li>
          <li>&bull; 1.25% FP rate on 80-frame night probe</li>
        </ul>
      </div>

      <div class="card">
        <div class="card-header">
          <div class="card-dot emerald"></div>
          <h3>Infra + supporting packages</h3>
        </div>
        <ul>
          <li>&bull; 54 infra modules + 18 in 7 supporting packages</li>
          <li>&bull; 72 production modules total</li>
          <li>&bull; 6 Part 9 splits complete (see &darr;)</li>
          <li>&bull; paths.py imported by every other infra module</li>
          <li>&bull; Phase 6A (vehicle_identifier) present but disabled via env flag</li>
        </ul>
      </div>

      <div class="card">
        <div class="card-header">
          <div class="card-dot amber"></div>
          <h3>Recent phases</h3>
        </div>
        <ul>
          <li>&bull; 6B.107/109: motion gate (yolov8n.onnx pre-classifier)</li>
          <li>&bull; 6B.110: focused_pass audit</li>
          <li>&bull; 6B.111/112: composite_telegram (vehicle_alert formatter)</li>
          <li>&bull; 6B.115: GateVerdict frames in-memory (no disk for crop pass)</li>
          <li>&bull; <strong>6B.116: night heuristic gate live in plist</strong></li>
        </ul>
      </div>

      <div class="card">
        <div class="card-header">
          <div class="card-dot slate"></div>
          <h3>External dependencies</h3>
        </div>
        <ul>
          <li>&bull; 6 Reolink cameras (HTTP webhook + RTSP :554)</li>
          <li>&bull; Telegram Bot API (api.telegram.org)</li>
          <li>&bull; Qwen3.6-35B-A3B server (:8093, unified since §11.81)</li>
          <li>&bull; Refactor-only filesystem (data/, logs/, config/)</li>
          <li>&bull; Zero coupling to ~/ai_camera_monitor/</li>
        </ul>
      </div>
    </div>

    <p class="footer">
      Generated {today} from actual intra-project imports &middot; ai_camera_monitor/ &middot; post-6B.147 (drop vision_pool, configurable LLM endpoints)
    </p>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    prod, edges = collect_modules()
    svg, canvas_h = render_svg(prod, edges)
    html = render_html(svg)
    out = REPO_ROOT / "listener-architecture.html"
    out.write_text(html, encoding="utf-8")
    prod_count = len(prod)
    edge_count = len(edges)
    print(f"modules: {prod_count}  edges: {edge_count}  canvas_h: {canvas_h}")
    print(f"wrote {out.relative_to(REPO_ROOT)} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())