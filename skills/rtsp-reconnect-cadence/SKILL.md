---
name: rtsp-reconnect-cadence
description: Pick an RTSP reconnect cadence. go2rtc, NVR, DeepStream.
---

# RTSP reconnect cadence — research notes (2026-08-16)

Condensed after a 2026-08-16 incident where the OFS persistent
RTSP reader went zombie (frames_decoded=1,883,544 frozen,
reconnects=0, uptime=41h) and dropped 6 morning alerts. The
existing error-backoff reconnect path never fires because no
exception is raised — the TCP socket dies silently.

## Survey of cadence choices in the wild

| Project | Cadence | Mechanism | Notes |
|---|---|---|---|
| **go2rtc** | **25s keepalive** | RTSP OPTIONS ping | Default. Prevents the camera from aging out the session entirely. Reconnect on error: exponential backoff starting at 10s. |
| **go2rtc yaml** | user-configurable (`reconnect_interval`) | Reactive reconnect | Example user picks 60s. Configurable per stream. |
| **NVIDIA DeepStream** | `rtsp-reconnect-interval` role | Configurable in seconds | Server-class consumer; default ~10min in some versions. |
| **OBS auto-reconnect** | reactive only, ~30s–2min observed | Reactive | No proactive reconnect; common complaint: "RTSP drops daily." |
| **Wyze/ContaCam forums** | workaround: daily app restart | Reactive | "At least once per day" drops observed on consumer WiFi cams. |
| **Some vendor NVRs + Reolink** | drops within 24–48h of fresh RTSP open | Reactive | Common community workaround: nightly service restart. |
| **Our OFS reader** | **zombie at 41h** | Reactive (broken — see above) | This incident. |

## Patterns

1. **Keepalive (OPTIONS ping every 25–60s)** is the most common
   solution. go2rtc ships it by default. Prevents the camera
   from aging out the session table entry. Best long-term but
   a bigger design surface — needs RTSP client integration with
   PyAV's container or a side-channel.

2. **Proactive reconnect every X minutes/hours** is the fallback
   when keepalive isn't possible. Typical NVR range: **30 min – 4h**.

3. **Daily reconnect** is the conservative end. Long exposure
   window if the camera dies at hour 23.

4. **Hourly reconnect** sits in the middle of the practical range.

## Our situation

Two facts push shorter than daily:

- Our failure took **41 hours** to manifest. A 24h cadence
  would have caught it only by coincidence.
- The OFS camera shares an RTSP session table with Surveillance
  Station (3–4 slots per Reolink). Long-lived idle sockets get
  silently aged out.

## Recommended default

**1 hour (3600 seconds)**, configurable via constructor arg +
env var `FARMSV_RTSP_RECONNECT_SECONDS`.

Implementation shape: scheduled container-level reconnect from
inside `PersistentRTSPReader`. Close `self._container` from a
separate watchdog thread; let the existing `_run_loop` reopen
it via the same code path as a real failure. Ring buffer is
preserved across reconnect (only the av container is replaced,
the decode thread is not torn down). No new exception logic
needed.

## Why NOT add keepalive in the same change

It's the better long-term solution but a bigger design surface —
needs to send RTSP OPTIONS, handle responses, integrate with
PyAV's container (which doesn't expose raw RTSP request methods
cleanly). Separate change, separate review.

## Sources

- go2rtc RTSP keepalive: <https://deepwiki.com/skrashevich/go2rtc/4.2-rtsp-sources>
- go2rtc reconnect_interval yaml: <https://github.com/AlexxIT/go2rtc/issues/1372>
- NVIDIA DeepStream rtsp-reconnect-interval: <https://forums.developer.nvidia.com/t/rtsp-reconnect-interval-role/293138>
- Frigate + Reolink daily drops: <https://github.com/blakeblackshear/frigate/discussions/14903>
- OBS auto-reconnect: <https://obsproject.com/forum/threads/autoreconnect-rtsp-stream.141004/>
- Wyze/ContaCam daily drops: <https://forums.wyze.com/t/rtsp-drops-daily-forced-to-restart-camera/81802>
- StackOverflow RTSP keepalive: <https://stackoverflow.com/questions/9880998/how-to-keep-rtsp-session-alive>
- StackOverflow RTP/RTSP keepalive: <https://stackoverflow.com/questions/7722467/keeping-alive-rtsp-connection>
