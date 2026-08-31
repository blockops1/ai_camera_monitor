"""
Probe Phase.80 (PLAN §11.13) — scheduled RTSP reconnect watchdog.

Spins up a live PersistentRTSPReader against the CAM1 camera with a SHORT
scheduled_reconnect_seconds (60s default), polls the reader every few
seconds for `frames_decoded_total` and `reconnects_total`, and prints a
timeline. After the cadence elapses, the watchdog should:
  - log "scheduled_reconnect fired" with uptime + counters
  - the existing _run_loop should reopen the container within ~1-2s
  - frames_decoded_total should keep climbing through the reconnect
  - reconnects_total should be > 0

This is the probe-first check (PLAN §11.13.3 step 1) — verify the
watchdog actually fires on a real reader before wiring it into the
production listener.

Usage:
    .venv/bin/python scripts/probe_scheduled_reconnect.py [--duration 180] [--cadence 60]

If the camera is unreachable, exits 2.
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Repo root → import infra
sys.path.insert(0, str(Path(__file__).parent.parent))

from infra.persistent_rtsp import PersistentRTSPReader

# Camera creds file is at repo root (next to PLAN.md). NEVER source via shell.
CREDS_PATH = Path(__file__).parent.parent / "camera-creds.env"


def _load_ofs_url() -> str | None:
    if not CREDS_PATH.exists():
        return None
    env = {}
    for line in CREDS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env.get("CAM1_RTSP_URL")


def probe(duration_s: int, cadence_s: int) -> int:
    rtsp_url = _load_ofs_url()
    if not rtsp_url:
        print("ERROR: CAM1_RTSP_URL not in camera-creds.env")
        return 2

    # Print WITHOUT leaking creds — only the host suffix after '@'.
    host_only = rtsp_url.split("@")[-1] if "@" in rtsp_url else rtsp_url
    print("=== Scheduled RTSP reconnect probe ===")
    print(f"Camera host: {host_only}")
    print(f"Cadence: {cadence_s}s")
    print(f"Duration: {duration_s}s")
    print(f"Env override: FARMSV_RTSP_RECONNECT_SECONDS={os.environ.get('FARMSV_RTSP_RECONNECT_SECONDS', '(unset)')}")
    print()

    reader = PersistentRTSPReader(
        rtsp_url,
        scheduled_reconnect_seconds=cadence_s,
    )
    reader.start()

    deadline = time.monotonic() + duration_s
    last_fdec = -1
    last_rconn = -1
    timeline = []

    # Give the reader a moment to open the RTSP socket and decode a few frames.
    time.sleep(2.0)

    while time.monotonic() < deadline:
        now = time.monotonic()
        elapsed = int(now - (deadline - duration_s))
        fdec = reader.frames_decoded_total
        rconn = reader.reconnects_total
        healthy = reader.is_healthy()
        delta_f = fdec - last_fdec if last_fdec >= 0 else 0
        delta_r = rconn - last_rconn if last_rconn >= 0 else 0
        timeline.append((elapsed, fdec, rconn, healthy, delta_f, delta_r))
        print(
            f"  t={elapsed:3d}s  frames_decoded={fdec:>8d}  "
            f"reconnects_total={rconn:>3d}  healthy={healthy}  "
            f"(Δframes=+{delta_f}, Δreconnects=+{delta_r})"
        )
        last_fdec = fdec
        last_rconn = rconn
        time.sleep(5.0)

    print()
    print("=== Summary ===")
    final_fdec = reader.frames_decoded_total
    final_rconn = reader.reconnects_total
    print(f"Final frames_decoded_total: {final_fdec}")
    print(f"Final reconnects_total:    {final_rconn}")
    print(f"Final uptime_seconds:       {reader.uptime_seconds():.0f}")

    # Verdict
    expected_reconnects = (duration_s // cadence_s) - 1  # first fire at cadence_s
    if final_rconn >= expected_reconnects and final_fdec > 0:
        print(
            f"\nPASS: watchdog fired {final_rconn}x (expected >= {expected_reconnects}), "
            f"frames kept decoding ({final_fdec})."
        )
        rc = 0
    elif final_rconn == 0:
        print(
            f"\nFAIL: watchdog never fired. expected >= {expected_reconnects} "
            f"reconnects in {duration_s}s with cadence {cadence_s}s."
        )
        rc = 1
    else:
        print(
            f"\nWARN: watchdog fired {final_rconn}x but frames didn't keep "
            f"growing (final={final_fdec}). Reader may not be recovering."
        )
        rc = 1

    reader.stop()
    return rc


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=180, help="Probe duration in seconds (default 180 = 3min)")
    p.add_argument(
        "--cadence",
        type=int,
        default=60,
        help="scheduled_reconnect_seconds (default 60 = fast for probing)",
    )
    args = p.parse_args()
    sys.exit(probe(args.duration, args.cadence))


if __name__ == "__main__":
    main()
