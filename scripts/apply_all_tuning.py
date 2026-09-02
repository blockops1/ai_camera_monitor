#!/usr/bin/env python3
"""
apply_all_tuning.py — Apply the tuned motion settings to all cameras using a SINGLE
browser context (with drain delay between cameras).

Phase 6B.166 §11.87.6: cameras come from infra.camera_creds; recipe comes from
infra.recipe (config/motion_recipe.json). Each camera gets its own per-camera
override merged with the fleet recipe.

Burned 2026-07-26: naive --apply-all spins up one CamBrowser per camera in
quick succession → all cameras on the registry hit max-session (rspCode -5)
after ~10 cumulative logins. This script keeps one browser but paces itself:
  - Login + apply + verify on camera A
  - Close browser fully
  - Wait drain-secs for Reolink to release camera A's session slot
  - Open browser, login to camera B (fresh session)
  - ... etc.

Phase 6B.167 §13.5 (2026-08-30, Commit 9): --camera <code> flag added.
Cameras are identified by generic code (CAM1/CAM2/...) via
infra.cameras.by_code(). Legacy --start <friendly-name> still works for
back-compat (resolved through the registry). The recipe lookup key in
config/motion_recipe.json is still the friendly display name — per-camera
overrides keyed on name are preserved.

[STATUS / module header sections follow below — see refactor-module-header skill]

STATUS: stable
THREAD SAFETY: single-threaded (drives a headless Chromium browser)

INPUTS:
    - camera-creds.env (via infra.cameras) — source of camera list
    - config/motion_recipe.json (via infra.recipe) — per-camera tuning values
    - CLI flags: --camera <code> (preferred), --start <name> (legacy),
      --label, --list-cameras, --dry-run, --drain-secs,
      --creds-env, --recipe-path, --no-recipe

OUTPUTS:
    - per-camera dict {ip, name, baseline, applied, final}
    - exit codes: 0 = all applied, 1 = at least one failed, 2 = fatal
      config error

PUBLIC API:
    apply_to_one_camera(name: str, ip: str, user: str, password: str,
                        recipe: dict, *, dry_run: bool = False) -> dict
        Open browser, login, read baseline, apply recipe, re-read, close.

DOES NOT DO:
    - Configure webhook URLs — that's configure_webhook.py.
    - Edit camera-creds.env.
    - Skip cameras already at target (always applies the recipe).
    - Resolve recipe by camera CODE — recipe is keyed by friendly name
      in config/motion_recipe.json (see infra.recipe.resolve_for_camera).
      The script's --camera flag identifies WHICH camera to apply to,
      but the recipe per-camera override lookup still uses the name.

CALLED BY:
    - operator shell: scripts/apply_all_tuning.py
    - operator shell: scripts/apply_all_tuning.py --camera CAM1 --dry-run
    - operator shell: scripts/apply_all_tuning.py --start "<legacy friendly name>"

CALLS INTO:
    - infra.cameras: load_cameras, by_code, by_ip, all_codes
    - infra.camera_creds: get_http_user, get_http_password (now a shim
      that delegates to infra.cameras; preserved for back-compat)
    - infra.recipe: load_recipe, resolve_for_camera
    - infra.paths: CAMERA_CREDS_FILE, MOTION_RECIPE_FILE
    - cam_browser.CamBrowser
    - tune_510a_motion_sensitivity: open_alarm_settings, read_current_state,
      apply_recipe_with
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).parent))
from cam_browser import CamBrowser
from tune_510a_motion_sensitivity import (
    RECIPE,
    apply_recipe_with,
    open_alarm_settings,
    read_current_state,
)

from infra.camera_creds import (
    get_http_password,
    get_http_user,
)
from infra.cameras import all_codes, by_code, by_ip, load_cameras
from infra.paths import CAMERA_CREDS_FILE, MOTION_RECIPE_FILE
from infra.recipe import load_recipe, resolve_for_camera

# Drain delay between cameras. Reolink idle timeout is 5-15 min per the
# farm-surveillance-workflow skill — 60s is generous enough that the prior
# session is well-released before we open a new one.
DRAIN_SECS_DEFAULT = 60


def _list_cameras() -> None:
    """Print all registered cameras and exit.

    Phase 6B.167 §13.5 (Commit 9): matches the --list-cameras flag added to
    read_alarm_settings.py / cam_browser.py / tune_510a_motion_sensitivity.py.
    """
    try:
        fleet = load_cameras()
    except Exception as e:
        print(f"ERROR loading cameras: {e}", file=sys.stderr)
        sys.exit(2)
    if not fleet:
        print("(no cameras found in camera-creds.env)", file=sys.stderr)
        return
    for spec in fleet:
        print(f"{spec.code}\t{spec.name}\t{spec.ip}\t{spec.zone}")


def _resolve_targets(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """Return [(label, ip, recipe_lookup_label), ...] in registry order.

    Phase 6B.167 §13.5 (Commit 9): fleet comes from infra.cameras.load_cameras()
    (NEW + LEGACY schemas both supported). Camera identification uses generic
    codes (CAM1/CAM2/...) when --camera is given; --start <friendly-name>
    remains supported for back-compat.

    recipe_lookup_label is the key used to look up per-camera overrides in
    config/motion_recipe.json. It defaults to the camera's friendly name, but
    --label overrides it for every camera (useful for testing one recipe
    against the fleet).
    """
    try:
        fleet = load_cameras()
    except Exception as e:
        print(f"ERROR loading cameras: {e}", file=sys.stderr)
        sys.exit(2)
    if not fleet:
        print("ERROR: no cameras found in camera-creds.env", file=sys.stderr)
        sys.exit(2)

    # Build {name: spec} index for legacy --start lookup
    by_name = {spec.name: spec for spec in fleet}

    # Determine the starting index
    start_idx = 0
    if args.camera:
        try:
            spec = by_code(args.camera)
        except KeyError:
            known = ", ".join(all_codes()) or "(none — env file empty or missing)"
            print(f"ERROR: unknown camera code {args.camera!r}. Known: {known}",
                  file=sys.stderr)
            sys.exit(2)
        # Build a single-element target list from the spec
        recipe_lookup = args.label if args.label else spec.name
        return [(spec.name, spec.ip, recipe_lookup)]

    if args.start:
        for i, spec in enumerate(fleet):
            if spec.name == args.start:
                start_idx = i
                break
        else:
            print(f"ERROR: --start camera {args.start!r} not found in registry. "
                  f"Known names: {', '.join(by_name)}", file=sys.stderr)
            sys.exit(2)

    out: list[tuple[str, str, str]] = []
    recipe_lookup = args.label if args.label else None
    # We already sliced fleet[start_idx:], so every spec in the slice is
    # at-or-after --start and should be processed.
    for spec in fleet[start_idx:]:
        lookup = recipe_lookup if recipe_lookup else spec.name
        out.append((spec.name, spec.ip, lookup))
    return out


def apply_to_one_camera(
    name: str,
    ip: str,
    user: str,
    password: str,
    recipe: dict,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Open browser, login, read baseline, apply recipe, re-read, close."""
    print(f"\n=== {name} ({ip}) ===", file=sys.stderr)

    if dry_run:
        print(f"  DRY-RUN: would apply recipe = {recipe}", file=sys.stderr)
        return {
            "ip": ip, "name": name, "dry_run": True,
            "would_apply": recipe,
        }

    baseline: dict | None = None
    applied: dict | None = None
    final: dict | None = None

    try:
        with CamBrowser(headless=True) as cb:
            if not cb.login(ip, user, password):
                return {"ip": ip, "name": name, "error": "login failed"}

            if not open_alarm_settings(cb.page):
                return {"ip": ip, "name": name, "error": "could not open Alarm Settings"}

            baseline = read_current_state(cb.page)
            print(f"  baseline: {baseline}", file=sys.stderr)

            applied = apply_recipe_with(cb.page, recipe)
            print(f"  applied:  {applied}", file=sys.stderr)

            time.sleep(2)
            final = read_current_state(cb.page)
            print(f"  final:    {final}", file=sys.stderr)
    except Exception as e:
        return {
            "ip": ip, "name": name, "error": str(e),
            "baseline": baseline, "applied": applied, "final": final,
        }

    return {
        "ip": ip, "name": name,
        "baseline": baseline, "applied": applied, "final": final,
    }


def main():
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").split('\n')[0],
    )
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--camera", default=None, metavar="CODE",
                        help="Apply only to this camera code (e.g. CAM1). "
                             "Preferred over --start.")
    target.add_argument("--start", default=None, metavar="NAME",
                        help="[legacy] Start at this friendly camera name "
                             "(skip earlier cameras in registry order)")
    ap.add_argument("--list-cameras", action="store_true",
                    help="Print known cameras and exit")
    ap.add_argument("--label", default=None,
                    help="Override the recipe lookup key for all cameras")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be done, don't touch cameras")
    ap.add_argument("--drain-secs", type=int, default=DRAIN_SECS_DEFAULT,
                    help=f"Seconds to wait between cameras (default {DRAIN_SECS_DEFAULT})")
    ap.add_argument("--creds-env", default=None,
                    help="Override camera-creds.env path")
    ap.add_argument("--recipe-path", default=None,
                    help="Override motion_recipe.json path")
    ap.add_argument("--no-recipe", action="store_true",
                    help="Skip JSON recipe; use tune_510a.RECIPE embedded default")
    args = ap.parse_args()

    # --- List-and-exit ---
    if args.list_cameras:
        _list_cameras()
        return 0

    # --- Resolve targets (may also load + validate registry) ---
    creds_env = args.creds_env or str(CAMERA_CREDS_FILE)
    targets = _resolve_targets(args)
    if not targets:
        print("ERROR: no cameras matched (check --camera / --start / camera-creds.env)",
              file=sys.stderr)
        sys.exit(2)

    # --- Recipe ---
    if args.no_recipe:
        recipe_raw = None
        print("Using tune_510a_motion_sensitivity.RECIPE (--no-recipe)", file=sys.stderr)
    else:
        recipe_path = args.recipe_path or str(MOTION_RECIPE_FILE)
        try:
            recipe_raw = load_recipe(env_path=recipe_path)
        except (FileNotFoundError, ValueError) as e:
            print(f"ERROR: failed to load recipe from {recipe_path}: {e}",
                  file=sys.stderr)
            sys.exit(2)
        print(f"Loaded recipe from {recipe_path}", file=sys.stderr)

    results: list[dict] = []
    any_failed = False

    for idx, (label, ip, lookup_label) in enumerate(targets):
        # Resolve the recipe for this camera
        if args.no_recipe:
            recipe: dict = dict(RECIPE)
        else:
            recipe = resolve_for_camera(label=lookup_label, recipe=recipe_raw)

        user = get_http_user(ip, creds_env)
        password = get_http_password(ip, creds_env)
        if not password or not user:
            print(f"{label} ({ip}): SKIP — missing HTTP_USER/HTTP_PASS",
                  file=sys.stderr)
            results.append({
                "ip": ip, "name": label, "error": "missing credentials",
            })
            any_failed = True
            continue

        if args.dry_run:
            result = apply_to_one_camera(
                label, ip, user, password, recipe, dry_run=True,
            )
            results.append(result)
            print(f"\n>> {label} ({ip})")
            print(json.dumps(result, indent=2))
            continue

        try:
            result = apply_to_one_camera(
                label, ip, user, password, recipe, dry_run=False,
            )
            results.append(result)
            print(f"\n>> {label} ({ip})")
            print(json.dumps(result, indent=2))
            if "error" in result:
                any_failed = True
        except Exception as e:
            print(f"{label} ({ip}): FAILED — {e}", file=sys.stderr)
            results.append({"ip": ip, "name": label, "error": str(e)})
            any_failed = True

        # Drain between cameras (no wait after the last one)
        if idx < len(targets) - 1:
            print(f"\nWaiting {args.drain_secs}s for prior session to drain...",
                  file=sys.stderr)
            time.sleep(args.drain_secs)

    print("\n\n=== SUMMARY ===")
    print(json.dumps(results, indent=2))
    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
