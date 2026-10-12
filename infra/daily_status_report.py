"""daily_status_report — generate daily HTML status report for the Crestview Orchards Telegram channel.

STATUS: stable
THREAD SAFETY: single-threaded; called once per day from launchd plist.

INPUTS:
    env TELEGRAM_BOT_TOKEN — Telegram bot token (required for send)
    env TELEGRAM_HOME_CHAT_ID — target chat ID (required for send)

OUTPUTS:
    - HTML file at logs/daily-status/crestview-orchards-surveillance-status-YYYY-MM-DD.html
    - Telegram document message to home chat with that file attached

PUBLIC API:
    build_report() -> dict         # pure: gather all metrics into a dict
    render_html(report: dict) -> str   # pure: dict -> HTML
    save_report(html: str, save_dir: str) -> pathlib.Path  # pure: write to disk
    send_document(file_path: Path, caption: str) -> bool   # I/O: Telegram API
    main() -> int                  # orchestrator: build -> render -> save -> send

DOES NOT DO:
    - Retry on Telegram send failure (alert-bucket-cooldown is the operator's pattern;
      daily report is fire-and-forget, one shot per day, so retry would just be noise)
    - Touch any other system state (read-only by design)
    - Read from NAS mounts (avoids the launchd-vs-shell SMBFS bug noted in
      daily-report-cron skill)

CALLS INTO:
    - subprocess (reolink-cli, lsof, ps, tail, df, uptime)
    - httpx (Telegram sendDocument)
    - logging (for error reporting)

DESIGN NOTES:
    - Pure-function shape per daily-report-cron skill: build / render / save / send
      are independently testable.
    - Module-level constants overridable via monkeypatch in tests.
    - No src/ dir exists in this project; lives in infra/ alongside telegram_alerts.py.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from html import escape
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants — overridable via monkeypatch in tests.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path("/Users/jill/farm-surveillance-v2")
LISTENER_PLIST = "com.farm.surveillance.v2"
LISTENER_PID_FILE = Path.home() / "Library" / "LaunchAgents" / f"{LISTENER_PLIST}.plist"
DAEMON_LOG = PROJECT_ROOT / "logs" / "daemon.log"
SAVE_DIR = PROJECT_ROOT / "logs" / "daily-status"
FILE_PREFIX = "crestview-orchards-surveillance-status"

# 6 cams — name, IP, listener cam-id, model. Order is operator's mental model:
# interior first (FRONT/BACK), then exterior clockwise.
CAMS: list[dict[str, str]] = [
    {"alias": "FRONT",  "ip": "192.168.1.39",  "label": "Front Door Outside",  "model": "RLC-833A"},
    {"alias": "BACK",   "ip": "192.168.1.85",  "label": "Back Door Inside",    "model": "RLC-833A"},
    {"alias": "OFG",    "ip": "192.168.1.73",  "label": "Outside Front Garage","model": "RLC-510A"},
    {"alias": "OFP",    "ip": "192.168.1.108", "label": "Outside Front Power", "model": "RLC-510A"},
    {"alias": "OFS",    "ip": "192.168.1.103", "label": "Outside Front Solar", "model": "RLC-510A"},
    {"alias": "OBS",    "ip": "192.168.1.113", "label": "Outside Back Solar",  "model": "RLC-510A"},
]

# Listener listen ports
LISTEN_PORTS = (8090, 8091, 8092)

# Threshold for "warning" status on per-cam CPU.
CPU_WARN_PCT = 70
CPU_CRIT_PCT = 90

# Absolute path to reolink-cli — not on the default launchd PATH.
REOLINK_CLI: Path = Path.home() / ".local" / "bin" / "reolink-cli"


# ---------------------------------------------------------------------------
# Pure data-gathering functions.
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a subprocess; return (rc, stdout, stderr). Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, "", str(e)


def get_listener_status() -> dict[str, Any]:
    """Return PID, uptime, and listen-port status for the surveillance v2 listener."""
    # Use launchctl print to find the PID — survives restarts.
    rc, out, _ = _run(["launchctl", "list"], timeout=5)
    pid: int | None = None
    if rc == 0:
        for line in out.splitlines():
            if LISTENER_PLIST in line:
                # columns: PID Status Label
                parts = line.split()
                if parts and parts[0].isdigit():
                    pid = int(parts[0])
                    break

    # Find uptime via ps if PID known
    uptime_s: int | None = None
    if pid is not None:
        rc, out, _ = _run(["ps", "-p", str(pid), "-o", "etime="], timeout=5)
        if rc == 0 and out.strip():
            uptime_s = _parse_etime(out.strip())

    # Check each listen port — match on listener PID (lsof shows process command,
    # not the plist label, so LISTENER_PLIST in out always fails).
    port_status: dict[int, bool] = {}
    pid_str = str(pid) if pid is not None else None
    for port in LISTEN_PORTS:
        rc, out, _ = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=5)
        port_status[port] = rc == 0 and pid_str is not None and pid_str in out

    return {
        "pid": pid,
        "uptime_s": uptime_s,
        "ports": port_status,
        "all_ports_up": all(port_status.values()),
    }


def _parse_etime(s: str) -> int | None:
    """Parse ps etime output (e.g. '01:23:45', '5-01:23:45') into seconds."""
    s = s.strip()
    try:
        if "-" in s:
            days, rest = s.split("-", 1)
            h, m, sec = rest.split(":")
            return int(days) * 86400 + int(h) * 3600 + int(m) * 60 + int(sec)
        h, m, sec = s.split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec)
    except (ValueError, AttributeError):
        return None


def _read_log_tail(path: Path, max_lines: int = 50000) -> list[str]:
    """Return the last max_lines of a log file. Empty list if missing."""
    if not path.exists():
        return []
    try:
        rc, out, _ = _run(["tail", f"-{max_lines}", str(path)], timeout=10)
        return out.splitlines() if rc == 0 else []
    except Exception:
        return []


def get_per_cam_status() -> list[dict[str, Any]]:
    """For each cam, return RTSP connection count and (if reachable) CPU% via reolink-cli.

    Uses reolink-cli directly (not the listener). Skips cams that fail fast.
    """
    cam_results: list[dict[str, Any]] = []
    # Check RTSP ESTABLISHED counts from listener in one lsof call
    rc, out, _ = _run(
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-a", "-p", _listener_pid_or_placeholder()],
        timeout=5,
    )
    rtsp_counts: dict[str, int] = {c["alias"]: 0 for c in CAMS}
    if rc == 0:
        for line in out.splitlines():
            for cam in CAMS:
                if cam["ip"] in line and ":554" in line:
                    rtsp_counts[cam["alias"]] += 1
                    break

    # For CPU, use reolink-cli per cam. Use env-passed password from camera-creds.env
    # so we don't expose the password in argv. Skip if env not set (caller's problem).
    password = _get_shared_cam_password()
    for cam in CAMS:
        info: dict[str, Any] = {
            "alias": cam["alias"],
            "ip": cam["ip"],
            "label": cam["label"],
            "model": cam["model"],
            "rtsp_conns": rtsp_counts.get(cam["alias"], 0),
            "cpu_pct": None,
            "encode_main": None,
            "reachable": False,
        }
        if not password or not REOLINK_CLI.exists():
            cam_results.append(info)
            continue
        env = {**os.environ, "REOLINK_PASSWORD": password}
        # Performance (CPU)
        try:
            r = subprocess.run(
                [
                    str(REOLINK_CLI), "--host", f"{cam['ip']}:9000", "--user", "admin",
                    "config", "get", "performance", "--output", "json",
                ],
                env=env, capture_output=True, text=True, timeout=8,
            )
            if r.returncode == 0:
                d = json.loads(r.stdout)
                v = (d.get("data") or {}).get("value") or {}
                if v:
                    info["reachable"] = True
                    info["cpu_pct"] = v.get("cpuUsedPercent")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass
        # Encode main
        try:
            r = subprocess.run(
                [
                    str(REOLINK_CLI), "--host", f"{cam['ip']}:9000", "--user", "admin",
                    "encode", "get", "--output", "json",
                ],
                env=env, capture_output=True, text=True, timeout=8,
            )
            if r.returncode == 0:
                d = json.loads(r.stdout)
                main = (d.get("data") or {}).get("main") or {}
                if main:
                    info["encode_main"] = (
                        f"{main.get('width', '?')}x{main.get('height', '?')}"
                        f"@{main.get('fps', '?')}fps {main.get('codec', '?')}"
                    )
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass
        cam_results.append(info)
    return cam_results


def _listener_pid_or_placeholder() -> str:
    """Return the listener PID, or '1' (a no-match) if not running."""
    rc, out, _ = _run(["launchctl", "list"], timeout=5)
    if rc != 0:
        return "1"
    for line in out.splitlines():
        if LISTENER_PLIST in line:
            parts = line.split()
            if parts and parts[0].isdigit():
                return parts[0]
    return "1"


def _get_shared_cam_password() -> str:
    """Read the shared Reolink admin password from camera-creds.env. Empty if missing."""
    creds_file = PROJECT_ROOT / "camera-creds.env"
    if not creds_file.exists():
        return ""
    try:
        for line in creds_file.read_text().splitlines():
            if line.startswith("FRONT_HTTP_PASS="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return ""
    return ""


def get_webhook_activity_24h() -> dict[str, int]:
    """Count POST /alert entries in the last 24h of daemon.log, broken down by source IP.

    Returns: {cam_alias: count} plus an 'other' bucket for non-cam sources.
    """
    log_lines = _read_log_tail(DAEMON_LOG, max_lines=200_000)
    if not log_lines:
        return {}
    cutoff = dt.datetime.now() - dt.timedelta(hours=24)
    ip_to_alias = {c["ip"]: c["alias"] for c in CAMS}
    counts: dict[str, int] = {c["alias"]: 0 for c in CAMS}
    counts["other"] = 0
    pat = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?(\d+\.\d+\.\d+\.\d+).*POST /alert"
    )
    for line in log_lines:
        m = pat.match(line)
        if not m:
            continue
        try:
            ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < cutoff:
            continue
        ip = m.group(2)
        alias = ip_to_alias.get(ip, "other")
        counts[alias] += 1
    return counts


def get_error_count_24h() -> int:
    """Count ERROR and Traceback lines in the last 24h of daemon.log."""
    log_lines = _read_log_tail(DAEMON_LOG, max_lines=200_000)
    if not log_lines:
        return 0
    cutoff = dt.datetime.now() - dt.timedelta(hours=24)
    n = 0
    for line in log_lines:
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        try:
            ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if " ERROR " in line or "Traceback" in line:
            n += 1
    return n


def get_disk_usage() -> dict[str, Any]:
    """Return free GB on the project volume and daemon.log size in MB."""
    try:
        usage = shutil.disk_usage(PROJECT_ROOT)
        free_gb = round(usage.free / 1024**3, 1)
    except OSError:
        free_gb = None
    log_mb: float | None = None
    if DAEMON_LOG.exists():
        try:
            log_mb = round(DAEMON_LOG.stat().st_size / 1024**2, 1)
        except OSError:
            pass
    return {"free_gb": free_gb, "daemon_log_mb": log_mb}


# ---------------------------------------------------------------------------
# Top-level build (pure: data only, no I/O).
# ---------------------------------------------------------------------------
def build_report(now: dt.datetime | None = None) -> dict[str, Any]:
    """Collect all daily-report data into a single dict.

    Args:
        now: Override the current time (for deterministic tests). Defaults to now().

    Returns:
        dict with keys: generated_at, listener, cams, webhooks_24h, errors_24h, disk.
    """
    now = now or dt.datetime.now()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "listener": get_listener_status(),
        "cams": get_per_cam_status(),
        "webhooks_24h": get_webhook_activity_24h(),
        "errors_24h": get_error_count_24h(),
        "disk": get_disk_usage(),
    }


# ---------------------------------------------------------------------------
# Pure render: dict -> HTML.
# ---------------------------------------------------------------------------
def _fmt_uptime(s: int | None) -> str:
    if s is None:
        return "—"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _cpu_class(cpu: int | None) -> str:
    if cpu is None:
        return "cam-unknown"
    if cpu >= CPU_CRIT_PCT:
        return "cam-crit"
    if cpu >= CPU_WARN_PCT:
        return "cam-warn"
    return "cam-ok"


def _rtsp_class(conns: int) -> str:
    return "cam-ok" if conns >= 1 else "cam-crit"


def render_html(report: dict[str, Any]) -> str:
    """Render the report dict as a self-contained HTML document (no external assets)."""
    listener = report["listener"]
    cams = report["cams"]
    webhooks = report["webhooks_24h"]
    errors = report["errors_24h"]
    disk = report["disk"]
    gen = report["generated_at"]

    # Listener summary
    pid = listener.get("pid")
    pid_str = str(pid) if pid is not None else "DOWN"
    uptime_str = _fmt_uptime(listener.get("uptime_s"))
    ports_html = " ".join(
        f'<span class="port port-{"ok" if ok else "bad"}">:{p}</span>'
        for p, ok in sorted(listener.get("ports", {}).items())
    )
    listener_class = "section section-ok" if listener.get("all_ports_up") and pid else "section section-bad"

    # Per-cam table rows
    cam_rows = []
    for c in cams:
        cpu = c.get("cpu_pct")
        cpu_str = f"{cpu}%" if cpu is not None else "—"
        rtsp = c.get("rtsp_conns", 0)
        rtsp_str = f"{rtsp} conn" if rtsp == 1 else f"{rtsp} conns"
        encode_str = c.get("encode_main") or "—"
        row_class = f"{_rtsp_class(rtsp)} {_cpu_class(cpu)}"
        cam_rows.append(
            f'<tr class="{row_class}">'
            f"<td>{escape(c['alias'])}</td>"
            f"<td>{escape(c.get('label', ''))}</td>"
            f"<td>{escape(c.get('model', ''))}</td>"
            f"<td class='num'>{rtsp_str}</td>"
            f"<td class='num'>{cpu_str}</td>"
            f"<td><code>{escape(encode_str)}</code></td>"
            f"</tr>"
        )

    # Webhook activity row
    webhook_cells = " ".join(
        f'<td class="num">{webhooks.get(c["alias"], 0)}</td>' for c in CAMS
    )
    webhook_total = sum(v for k, v in webhooks.items() if k != "other")
    webhook_other = webhooks.get("other", 0)

    # Disk + errors
    disk_free = disk.get("free_gb")
    disk_log = disk.get("daemon_log_mb")
    disk_html = (
        f"{disk_free} GB free" if disk_free is not None else "—"
    )
    log_html = f"{disk_log} MB" if disk_log is not None else "—"

    error_class = "section-bad" if errors > 50 else "section-ok"

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Crestview Orchards — Surveillance Daily Status — {gen[:10]}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    margin: 2em auto; max-width: 980px; color: #1a1a1a; line-height: 1.5;
    background: #fafafa; padding: 1em 2em;
  }}
  h1 {{ border-bottom: 2px solid #2e7d32; padding-bottom: 0.3em; }}
  h2 {{ margin-top: 2em; color: #2e7d32; }}
  .meta {{ color: #666; font-size: 0.9em; }}
  .section {{
    border-left: 4px solid #2e7d32; background: #fff; padding: 1em 1.5em;
    margin: 1em 0; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }}
  .section-ok {{ border-left-color: #2e7d32; }}
  .section-bad {{ border-left-color: #c62828; background: #fff5f5; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; background: #fff; }}
  th, td {{ padding: 0.5em 0.75em; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ background: #f1f8e9; font-weight: 600; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  code {{ background: #f5f5f5; padding: 0.1em 0.4em; border-radius: 3px; font-size: 0.9em; }}
  .port {{
    display: inline-block; padding: 0.2em 0.6em; margin: 0 0.2em;
    border-radius: 3px; font-family: monospace; font-size: 0.9em;
  }}
  .port-ok {{ background: #c8e6c9; color: #1b5e20; }}
  .port-bad {{ background: #ffcdd2; color: #b71c1c; }}
  .cam-ok td {{ background: #f1f8e9; }}
  .cam-warn td {{ background: #fff8e1; }}
  .cam-crit td {{ background: #ffebee; }}
  .cam-unknown td {{ background: #f5f5f5; color: #999; }}
  .summary-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1em; margin: 1em 0;
  }}
  .stat {{ background: #fff; padding: 1em; border-radius: 4px; text-align: center;
           box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
  .stat .label {{ color: #666; font-size: 0.85em; text-transform: uppercase;
                  letter-spacing: 0.05em; }}
  .stat .value {{ font-size: 1.8em; font-weight: 600; margin-top: 0.3em;
                  font-variant-numeric: tabular-nums; }}
  .stat.bad .value {{ color: #c62828; }}
  .stat.good .value {{ color: #2e7d32; }}
  footer {{ margin-top: 3em; padding-top: 1em; border-top: 1px solid #ddd;
            color: #888; font-size: 0.85em; }}
</style>
</head>
<body>
  <h1>Crestview Orchards — Daily Surveillance Status</h1>
  <p class="meta">Generated {escape(gen)} — automated daily report from farm-surveillance-v2</p>

  <h2>Listener</h2>
  <div class="{listener_class}">
    <p><strong>Status:</strong> {"Running" if pid else "DOWN"} |
       <strong>PID:</strong> {escape(pid_str)} |
       <strong>Uptime:</strong> {escape(uptime_str)}</p>
    <p><strong>Listening ports:</strong> {ports_html}</p>
  </div>

  <h2>Cameras ({len(cams)})</h2>
  <table>
    <thead>
      <tr>
        <th>Alias</th><th>Label</th><th>Model</th>
        <th>RTSP</th><th>CPU</th><th>Main encode</th>
      </tr>
    </thead>
    <tbody>
      {"".join(cam_rows)}
    </tbody>
  </table>
  <p class="meta">CPU thresholds: green &lt; 70%, yellow 70–89%, red ≥ 90%.</p>

  <h2>Webhook activity (last 24h)</h2>
  <div class="summary-grid">
    {"".join(
        f'<div class="stat"><div class="label">{escape(c["alias"])}</div>'
        f'<div class="value">{webhooks.get(c["alias"], 0)}</div></div>'
        for c in CAMS
    )}
  </div>
  <p class="meta">Total: {webhook_total} POST /alert from cams.
     {f"Other: {webhook_other}." if webhook_other else ""}</p>

  <h2>Health summary</h2>
  <div class="summary-grid">
    <div class="stat {'bad' if errors > 50 else 'good'}">
      <div class="label">Errors (24h)</div>
      <div class="value">{errors}</div>
    </div>
    <div class="stat">
      <div class="label">Disk free</div>
      <div class="value">{escape(disk_html)}</div>
    </div>
    <div class="stat">
      <div class="label">daemon.log</div>
      <div class="value">{escape(log_html)}</div>
    </div>
  </div>

  <footer>
    Report file: {escape(FILE_PREFIX)}-{gen[:10]}.html<br>
    Source: farm-surveillance-v2/infra/daily_status_report.py
  </footer>
</body>
</html>
"""
    return html


# ---------------------------------------------------------------------------
# I/O: save to disk.
# ---------------------------------------------------------------------------
def save_report(html: str, save_dir: Path, now: dt.datetime | None = None) -> Path:
    """Write the HTML report to save_dir with a date-stamped filename.

    Returns the absolute path of the written file.
    """
    now = now or dt.datetime.now()
    save_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{FILE_PREFIX}-{now.strftime('%Y-%m-%d')}.html"
    out = save_dir / fname
    out.write_text(html, encoding="utf-8")
    log.info("daily_status_report: saved %s (%d bytes)", out, len(html))
    return out


# ---------------------------------------------------------------------------
# I/O: send to Telegram.
# ---------------------------------------------------------------------------
def send_document(file_path: Path, caption: str = "") -> bool:
    """Send the file as a Telegram document to the home chat.

    Returns True on success, False on failure (logs the error, never raises).
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_HOME_CHAT_ID", "")
    if not bot_token or not chat_id:
        log.warning(
            "daily_status_report.send_document: TELEGRAM_BOT_TOKEN or "
            "TELEGRAM_HOME_CHAT_ID not set — skipping send"
        )
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    try:
        with httpx.Client(timeout=20.0) as client:
            with open(file_path, "rb") as f:
                resp = client.post(
                    url,
                    data={"chat_id": chat_id, "caption": caption[:1024]},
                    files={"document": (file_path.name, f, "text/html")},
                )
        if 400 <= resp.status_code < 600:
            log.error(
                "daily_status_report.send_document: Telegram API returned %d: %s",
                resp.status_code, resp.text[:200],
            )
            return False
        log.info("daily_status_report: sent %s to chat %s", file_path.name, chat_id)
        return True
    except (httpx.HTTPError, OSError) as e:
        log.error("daily_status_report.send_document: failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Orchestrator.
# ---------------------------------------------------------------------------
def _load_env() -> None:
    """Load TELEGRAM_BOT_TOKEN + TELEGRAM_HOME_CHAT_ID from telegram-creds.env.

    Mirrors the loader the listener uses (per AGENTS.md: per-app creds files
    at project root, mode 600, never in the launchd plist).
    """
    from dotenv import dotenv_values
    creds_file = PROJECT_ROOT / "telegram-creds.env"
    if not creds_file.exists():
        return
    values = dotenv_values(str(creds_file)) or {}
    for k, v in values.items():
        if v:
            os.environ.setdefault(k, v)
    # Also load the project .env for any other knobs
    repo_env = PROJECT_ROOT / ".env"
    if repo_env.exists():
        for k, v in (dotenv_values(str(repo_env)) or {}).items():
            if v and k not in os.environ:
                os.environ[k] = v


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _load_env()
    log.info("daily_status_report: building report")
    try:
        report = build_report()
    except Exception as e:
        log.exception("daily_status_report: build_report failed: %s", e)
        return 2

    log.info("daily_status_report: rendering HTML")
    html = render_html(report)

    log.info("daily_status_report: saving to %s", SAVE_DIR)
    out_path = save_report(html, SAVE_DIR)

    # Caption summarises the headline; HTML file is the detail.
    listener_pid = report["listener"].get("pid")
    listener_status = "UP" if listener_pid else "DOWN"
    total_webhooks = sum(
        v for k, v in report["webhooks_24h"].items() if k != "other"
    )
    errors = report["errors_24h"]
    caption = (
        f"Daily status — {report['generated_at'][:10]}\n"
        f"Listener: {listener_status} | "
        f"Webhooks 24h: {total_webhooks} | "
        f"Errors 24h: {errors}"
    )
    log.info("daily_status_report: sending to Telegram")
    if not send_document(out_path, caption=caption):
        log.warning("daily_status_report: Telegram send failed (file saved at %s)", out_path)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
