# Troubleshooting

Operator-direct troubleshooting recipes. Items here are problems operators actually hit, not theoretical edge cases.

> **Note:** Sections are added when a real issue comes up. If you solve something painful, drop the recipe here so the next operator doesn't rediscover it.

---

## Telegram: "TELEGRAM_HOME_CHAT_ID" not found

**Symptom:** Daemon raises `ConfigError` on every alert.

**Cause:** The canonical env var name is `TELEGRAM_HOME_CHAT_ID`, **not** `TELEGRAM_CHAT_ID`. If your `~/.env` has the wrong name, the dispatcher refuses to start.

**Verify:**

```bash
python3 -c 'from dotenv import dotenv_values; from pathlib import Path; print(bool(dotenv_values(Path.home() / ".env").get("TELEGRAM_HOME_CHAT_ID")))'
```

If this prints `False`, rename `TELEGRAM_CHAT_ID` to `TELEGRAM_HOME_CHAT_ID` in `~/.env`.

**Origin:** This rename was made in an earlier phase. Old `TELEGRAM_CHAT_ID` references still appear in PLAN.md (`telegram-creds.env` section) and V2-LOGIC-FLOW.md line 312 — those are stale doc references that the daemon itself never reads. The dispatcher is strict about the canonical name.

---

## Webhook rejected: IP mismatch

**Symptom:** Camera motion alerts log `IP mismatch` errors in `logs/daemon.log`. No Telegram notifications arrive even though the camera is firing motion events.

**Cause:** The daemon validates every incoming webhook against the `_IP` field in `camera-creds.env`. The `_IP` field is the source of truth for camera identity (comments in `camera-creds.env` header state this explicitly).

**Fix:**

1. Find the camera's current IP (Reolink web UI → Network → LAN IP).
2. Update the `_IP` field for that camera in `camera-creds.env`.
3. Bounce the daemon.

The camera name (e.g. `OUTSIDE_FRONT_SOLAR`) is just a label; the daemon matches on IP.

---

## Daemon log location

Default: `logs/daemon.log`. Watch live:

```bash
tail -f logs/daemon.log
```

Diagnostic endpoint:

```bash
curl -s http://127.0.0.1:8090/debug/rtsp
```

Returns per-reader ring size, decoded total, last-frame age, container-open flag, health flag, error count.

---

## Telegram alert timestamps are in UTC

**Symptom:** Telegram alerts show timestamps like `Timestamp: 2026-09-15T15:10:48.000+0000`. An operator in EDT reads `15:10` as 3 PM when it is actually 11 AM local.

**Cause:** The camera sends the timestamp in UTC ISO-8601 format. Prior to US-046c, alerts displayed this raw UTC value without conversion.

**Fix:** US-046c (2026-09-15) added local-timezone rendering. The daemon reads `DISPLAY_TZ` from the environment (default `America/New_York`) and converts all alert timestamps to that timezone before displaying. The format is `YYYY-MM-DD HH:MM:SS TZ` (e.g. `2026-09-15 11:10:48 EDT`).

**Configure:** Set `DISPLAY_TZ` in your `.env` file or launchd plist:

```
DISPLAY_TZ=America/New_York   # Eastern Time (auto-EDT/EST)
DISPLAY_TZ=America/Los_Angeles  # Pacific Time
DISPLAY_TZ=UTC                # keep UTC if you prefer
```

The IANA timezone name is resolved at daemon startup; no restart is needed after a timezone change — just restart the daemon to pick up the new value.

---

## See also

- [README.md](../../README.md) — main docs
- [docs/PLAN.md](../PLAN.md) — pipeline architecture
