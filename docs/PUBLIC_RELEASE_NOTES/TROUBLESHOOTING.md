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

## See also

- [README.md](../../README.md) — main docs
- [docs/PLAN.md](../PLAN.md) — pipeline architecture
