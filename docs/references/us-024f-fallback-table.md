# US-024f — fallback inventory entries to remove

This is the supporting reference for card US-024f. The card body in kanban links here.

## Entries to remove (12 entries, all PRESENT on `main` per `git show main:infra/frame_capture.py` 2026-09-13)

| Line | Pattern (current) | Recommended fix (from inventory) | Pattern code |
|---|---|---|---|
| 79 | `def _resolve_scheduled_reconnect_seconds(arg_value: float \| None) -> float:` | Use sentinel `_UNSET = object()` instead of `None` | P09, P04 |
| 97 | `def _resolve_max_reconnect_attempts(arg_value: int \| None) -> int:` | Use sentinel `_UNSET = object()` instead of `None` | P09, P04 |
| 262 | `except Exception: # noqa: BLE001, S110` (container.close) | Re-raise after logging | P01, P10 |
| 304 | `except OSError: pass` (frame unlink cleanup) | Log the error and re-raise or return error indicator | P01 |
| 465 | `except Exception as e: # noqa: BLE001` (ring buffer) | Log and re-raise | P01 |
| 565 | `except Exception: # noqa: BLE001, S112` (frame.to_image) | Log and re-raise | P01, P10 |
| 573 | `except Exception: # noqa: BLE001, S110` (container.close reconnect) | Log and re-raise | P01, P10 |
| 599 | `def get(cls, camera_id: str) -> PersistentRTSPReader \| None:` | Raise `KeyError` when camera not found | P09 |
| 612 | `rtsp_url = cam.get("rtsp_url", "")` | Raise `KeyError` with clear message | P03 |
| 636 | `rtsp_url = cam_info.get("rtsp_url", "")` | Raise `KeyError` when rtsp_url is missing | P03 |
| 642 | `registry_key = cam_info.get("prefix") or camera_id` | Require "prefix" in the camera dict; raise if missing | P03 |
| 782 | `except OSError: continue` (getmtime scan) | Log the error | P01 |

## Inventory source

Each entry's full description (assigned-to, why-fallback, recommended-fix prose) lives in `docs/FALLBACK-INVENTORY.md` under the section `## infra/`.

## Verification commands (copy-paste to re-verify)

```bash
git -C /Users/jill/farm-surveillance-v2 grep -nE 'except (Exception|OSError):\s*(# noqa|pass)' -- infra/frame_capture.py
git -C /Users/jill/farm-surveillance-v2 grep -nE '\.get\("(rtsp_url|prefix)",' -- infra/frame_capture.py
git -C /Users/jill/farm-surveillance-v2 grep -nE 'arg_value: (float|int) \| None' -- infra/frame_capture.py
```

All three commands should return empty output after US-024f merges to `main`.
