# Cleanup Policy

## Policy Summary

The farm surveillance system runs an automated cleanup daemon that enforces two
constraints on captured alert data:

1. **Age-based eviction:** Alert directories older than **24 hours**
   (`FRAME_RETENTION_HOURS = 24` in `infra/paths.py`) are deleted along with all
   their contents (frames, crops, JSON metadata). Audit log entries older than
   90 days (`AUDIT_RETENTION_DAYS = 90`) are similarly pruned.

2. **Disk-cap enforcement:** The total size of `data/frames/` is capped at
   **20 GB** (`FRAME_MAX_BYTES = 20 * 1024 * 1024 * 1024` in `infra/paths.py`).
   When the directory exceeds this cap, the oldest alert directories are deleted
   first until the total falls below 20 GB.

Alert metadata (JSON sidecars) and audit log entries follow their own retention
rules (`ALERT_RETENTION_DAYS = 7`, `AUDIT_RETENTION_DAYS = 90`) and are cleaned
up separately from frame data.

## Run Frequency

The cleanup daemon runs **hourly** (every 60 minutes) via two mechanisms:

1. **Primary:** A daemon thread spawned inside the listener process (`listener/daemon.py`).
   This thread calls `infra.cleanup.run()` on the cadence set by
   `FARMSURV_CLEANUP_INTERVAL_MIN` (default: 60 minutes).

2. **Backup (launchd):** A `launchd` agent plist at
   `infra/launchd/com.farm.surveillance.v2-cleanup.plist` with
   `StartInterval=3600` (1 hour). If the listener daemon dies or the thread
   is killed, the hourly launchd job still prunes expired alert directories and
   enforces the disk budget.

   Install (load) the backup:

   ```bash
   launchctl load ~/Library/LaunchAgents/com.farm.surveillance.v2-cleanup.plist
   ```

   Remove (unload) the backup:

   ```bash
   launchctl unload ~/Library/LaunchAgents/com.farm.surveillance.v2-cleanup.plist
   ```

## Kill Switch

Two environment variables control cleanup behavior at runtime:

| Variable | Default | Effect |
|---|---|---|
| `FARMSURV_CLEANUP_DISABLED=1` | `0` | **Disables** the cleanup daemon thread entirely. When set to `1`, no age-based eviction or disk-cap enforcement runs — data accumulates indefinitely until the variable is cleared. |
| `FARMSURV_CLEANUP_INTERVAL_MIN=N` | `60` | Changes the cleanup run cadence to every *N* minutes. Setting this to `0` or a negative value is not recommended (the daemon will run continuously). |

To set these for the launchd backup agent, use:

```bash
launchctl setenv FARMSURV_CLEANUP_DISABLED 1
launchctl setenv FARMSURV_CLEANUP_INTERVAL_MIN 30
```

## Telegram Alert Interpretation

When the system sends a Telegram alert reading:

> **"cleanup exceeded 20 GB cap"**

This means the cleanup daemon found the `data/frames/` directory above 20 GB
*after* attempting to prune it. There are three possible root causes:

1. **The cap is too low for the camera's frame rate.** A high-traffic camera
   produces frames faster than the hourly cleanup can evict them.

2. **Cleanup is broken.** The daemon may be encountering errors (permission
   issues, missing directories) and silently skipping evictions.

3. **The frames directory is growing faster than eviction.** E.g., a camera
   started capturing continuously and the window between growth and the next
   cleanup cycle is too wide.

**Investigate — do not just raise the cap.** Raising `FRAME_MAX_BYTES` without
understanding the cause delays the inevitable disk exhaustion. Check:

- `infra.cleanup.run()` output in `listener.log` for errors.
- The current size of `data/frames/` vs. the cap.
- Whether alert volume has increased (new camera, motion-triggered capture).

## Recovery Commands

### Manually invoke cleanup

```bash
cd /path/to/farm-surveillance-v2
.venv/bin/python -m infra.cleanup
```

This runs a one-shot cleanup pass and prints statistics to stdout.

### Reload the launchd agent

```bash
# Stop the current job
launchctl stop com.farm.surveillance.v2-cleanup

# Reload the plist (in case you edited it)
launchctl unload ~/Library/LaunchAgents/com.farm.surveillance.v2-cleanup.plist
launchctl load ~/Library/LaunchAgents/com.farm.surveillance.v2-cleanup.plist
```

### Change cleanup interval (runtime, daemon thread)

```bash
# Set env var, then restart the listener daemon
export FARMSURV_CLEANUP_INTERVAL_MIN=30
# or for the launchd backup:
launchctl setenv FARMSURV_CLEANUP_INTERVAL_MIN 30
```

### Disable cleanup (emergency — preserves all data)

```bash
export FARMSURV_CLEANUP_DISABLED=1
# or for the launchd backup:
launchctl setenv FARMSURV_CLEANUP_DISABLED 1
```

### Re-enable cleanup

```bash
export FARMSURV_CLEANUP_DISABLED=0
launchctl setenv FARMSURV_CLEANUP_DISABLED 0
```
