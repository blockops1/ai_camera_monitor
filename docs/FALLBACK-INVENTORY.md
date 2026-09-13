# V2-023: Fallback Inventory

**PRD:** PHASE-V2-023
**Date:** 2026-09-10
**Scanner:** `scripts/find_fallbacks.py` (12-pattern catalog)
**Scope:** `infra/` (US-023b) + `telegram_formatter/` + `vehicle_matcher/` (US-023c) + `listener/` (US-023d)

This document catalogs every fallback pattern found in the production source tree.
Each entry has all 5 required fields (file:line, code excerpt, what-it-does,
why-fallback, recommended-fix) plus `assigned-to` and `status`.

**Pattern key:**
- P01: silent exception swallow
- P02: env-with-default
- P03: .get() returning fallback
- P04: optional kwarg with branch
- P05: multi-format normalizer
- P06: union-type coercion
- P07: membership fallback
- P08: cooldown/dedup/suppress
- P09: Optional[] in hot signatures
- P10: try/except broad catch
- P11: v1 surface in v2
- P12: resize/JPEG/letterbox

---

## infra/

### frame_capture.py:262

- **file:line:** `frame_capture.py:262`
- **code excerpt:** `except Exception:  # noqa: BLE001, S110`
- **what-it-does:** Catches any exception during RTSP stream processing and silently continues without re-raising or logging.
- **why-fallback:** Masks real failures (connection drops, decode errors) — the caller never knows the stream is broken and will keep returning stale frames from the ring buffer.
- **recommended-fix:** Re-raise after logging; let the caller handle the failure.
- **assigned-to:** US-024a (V2-024: RTSP stream processing silent except)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P01, P10

### frame_capture.py:304

- **file:line:** `frame_capture.py:304`
- **code excerpt:** `except OSError:`
- **what-it-does:** Catches OS-level errors (file I/O, socket) during frame saving and silently continues.
- **why-fallback:** If frame write fails (disk full, permissions), the pipeline silently drops the alert with no signal.
- **recommended-fix:** Log the error and re-raise or return an error indicator.
- **assigned-to:** US-024a (V2-024: frame save OS error silent except)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P01

### frame_capture.py:465

- **file:line:** `frame_capture.py:465`
- **code excerpt:** `except Exception as e:  # noqa: BLE001`
- **what-it-does:** Catches any exception during frame ring buffer operations and silently continues.
- **why-fallback:** Ring buffer corruption or memory errors are hidden — the caller gets partial data with no error signal.
- **recommended-fix:** Log and re-raise.
- **assigned-to:** US-024a (V2-024: ring buffer silent except)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P01

### frame_capture.py:565

- **file:line:** `frame_capture.py:565`
- **code excerpt:** `except Exception:  # noqa: BLE001, S112`
- **what-it-does:** Catches any exception during RTSP stream read and silently continues.
- **why-fallback:** Silent failure during frame capture means the ring buffer fills with stale data; downstream consumers see "no motion" when there IS motion.
- **recommended-fix:** Log and re-raise.
- **assigned-to:** US-024a (V2-024: RTSP read silent except)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P01, P10

### frame_capture.py:573

- **file:line:** `frame_capture.py:573`
- **code excerpt:** `except Exception:  # noqa: BLE001, S110`
- **what-it-does:** Catches any exception during stream reconnect and silently continues.
- **why-fallback:** Reconnect failures are hidden — the connection stays broken but the reader reports healthy.
- **recommended-fix:** Log and re-raise.
- **assigned-to:** US-024a (V2-024: stream reconnect silent except)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P01, P10

### frame_capture.py:782

- **file:line:** `frame_capture.py:782`
- **code excerpt:** `except OSError:`
- **what-it-does:** Catches OS errors during cleanup and silently continues.
- **why-fallback:** Cleanup failures (failed deletes, permission errors) are silently ignored.
- **recommended-fix:** Log the error.
- **assigned-to:** US-024a (V2-024: cleanup OS error silent except)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P01

### frame_capture.py:79

- **file:line:** `frame_capture.py:79`
- **code excerpt:** `def _resolve_scheduled_reconnect_seconds(arg_value: float | None) -> float:`
- **what-it-does:** Optional-typed parameter `arg_value` that defaults to None and changes behavior (env var lookup vs. direct use).
- **why-fallback:** The `None` branch picks env var, the non-None branch uses the arg — different data sources for the same value create inconsistent behavior between constructor and manual override.
- **recommended-fix:** Use a sentinel value (`_UNSET`) instead of `None`, or make the None-branch explicit with a clear raise.
- **assigned-to:** US-024a (V2-024: optional kwarg with branch — _resolve_scheduled_reconnect_seconds)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P09, P04

### frame_capture.py:97

- **file:line:** `frame_capture.py:97`
- **code excerpt:** `def _resolve_max_reconnect_attempts(arg_value: int | None) -> int:`
- **what-it-does:** Optional-typed parameter `arg_value` that defaults to None and changes behavior (env var lookup vs. direct use).
- **why-fallback:** Same issue as _resolve_scheduled_reconnect_seconds — None branch uses env, non-None uses arg.
- **recommended-fix:** Same: use sentinel or explicit None check with raise.
- **assigned-to:** US-024a (V2-024: optional kwarg with branch — _resolve_max_reconnect_attempts)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P09, P04

### frame_capture.py:599

- **file:line:** `frame_capture.py:599`
- **code excerpt:** `def get(cls, camera_id: str) -> PersistentRTSPReader | None:`
- **what-it-does:** Returns None if camera not found in registry.
- **why-fallback:** Callers may use the result as if always-present, leading to confusing AttributeError later.
- **recommended-fix:** Raise KeyError when camera not found; callers that need optional behavior should handle explicitly.
- **assigned-to:** US-024a (V2-024: Optional[] in hot signature — get() returns None)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P09

### frame_capture.py:612

- **file:line:** `frame_capture.py:612`
- **code excerpt:** `rtsp_url = cam.get("rtsp_url", "")`
- **what-it-does:** Returns empty string if "rtsp_url" key missing from camera dict.
- **why-fallback:** Empty RTSP URL causes downstream connection to fail silently or connect to wrong target.
- **recommended-fix:** Raise KeyError with clear message when rtsp_url is missing.
- **assigned-to:** US-024a (V2-024: .get() fallback to empty rtsp_url)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P03

### frame_capture.py:636

- **file:line:** `frame_capture.py:636`
- **code excerpt:** `rtsp_url = cam_info.get("rtsp_url", "")`
- **what-it-does:** Same pattern: returns empty string if rtsp_url missing.
- **why-fallback:** Same as above — silent fallback to empty string masks missing camera config.
- **recommended-fix:** Raise KeyError when rtsp_url is missing.
- **assigned-to:** US-024a (V2-024: .get() fallback to empty rtsp_url)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P03

### frame_capture.py:642

- **file:line:** `frame_capture.py:642`
- **code excerpt:** `registry_key = cam_info.get("prefix") or camera_id`
- **what-it-does:** Falls back to camera_id if "prefix" key missing.
- **why-fallback:** Masking a missing config key — the fallback value may not match the intended registry key.
- **recommended-fix:** Require "prefix" in the camera dict; raise if missing.
- **assigned-to:** US-024a (V2-024: .get() fallback for registry prefix)
- **status:** REMOVED
- **removed_by:** US-024f
- **removed_on:** 2026-09-13
- **pattern:** P03

### ### gate.py:232

- **file:line:** `gate.py:232`
- **code excerpt:** `except Exception as err:`
- **what-it-does:** Catches any exception during gate processing and silently continues.
- **why-fallback:** Gate failures (model errors, file errors) are hidden — alerts pass through unclassified.
- **recommended-fix:** Log and re-raise.
- **assigned-to:** US-025a (V2-025: gate processing silent except)
- **status:** IDENTIFIED
- **pattern:** P01

### ### gate.py:265

- **file:line:** `gate.py:265`
- **code excerpt:** `except Exception as err:`
- **what-it-does:** Catches any exception during gate decision and silently continues.
- **why-fallback:** Silent gate failure means the pipeline proceeds without a gate decision, potentially missing real alerts.
- **recommended-fix:** Log and re-raise.
- **assigned-to:** US-025a (V2-025: gate decision silent except)
- **status:** IDENTIFIED
- **pattern:** P01

### ### gate.py:486

- **file:line:** `gate.py:486`
- **code excerpt:** `except Exception as e:`
- **what-it-does:** Catches any exception during gate V2 path and silently continues.
- **why-fallback:** V2 gate failures are hidden — the fallback path may behave differently from the intended path.
- **recommended-fix:** Log and re-raise.
- **assigned-to:** US-025a (V2-025: gate V2 path silent except)
- **status:** IDENTIFIED
- **pattern:** P01

### ### gate.py:870

- **file:line:** `gate.py:870`
- **code excerpt:** `except Exception as e:`
- **what-it-does:** Catches any exception during gate cleanup and silently continues.
- **why-fallback:** Cleanup failures are silently ignored.
- **recommended-fix:** Log the error.
- **assigned-to:** US-025a (V2-025: gate cleanup silent except)
- **status:** IDENTIFIED
- **pattern:** P01

### ### gate.py:168

- **file:line:** `gate.py:168`
- **code excerpt:** `val = os.environ.get("GATE_KEEP_DISK_ARTIFACTS", "").strip().lower()`
- **what-it-dotes:** Returns empty string (falsy) when env var is unset, defaulting to disk artifacts disabled.
- **why-fallback:** Missing env var silently changes behavior — operator doesn't know disk artifacts won't be kept.
- **recommended-fix:** Fail at boot if the critical config is not set, or use a documented default with a warning log.
- **assigned-to:** US-024b (V2-024: DEFERRED-to-V2-021 — disk artifacts env-with-default)
- **status:** DEFERRED-to-V2-021
- **pattern:** P02

### ### gate.py:358

- **file:line:** `gate.py:358`
- **code excerpt:** `return os.environ.get("MOTION_GATE_V2", "").strip().lower() in ("1", "true", "yes")`
- **what-it-does:** Returns False when env var is unset.
- **why-fallback:** Silent default to V1 gate behavior when V2 is expected — the operator may not realize V1 is running.
- **recommended-fix:** Fail at boot if V2 gate is explicitly enabled but V1 is active, or log a warning.
- **assigned-to:** US-024b (V2-024: DEFERRED-to-V2-021 — V1/V2 gate env-with-default)
- **status:** DEFERRED-to-V2-021
- **pattern:** P02

### ### gate.py:530

- **file:line:** `gate.py:530`
- **code excerpt:** `gate_enabled = cfg.get("gate_enabled") or DEFAULT_GATE_ENABLED`
- **what-it-does:** Falls back to default gate_enabled when key missing from config.
- **why-fallback:** Missing config silently enables/disables the gate based on the default, which may differ from operator intent.
- **recommended-fix:** Require the key in config; raise if missing.
- **assigned-to:** US-025a (V2-025: .get() fallback for gate_enabled config)
- **status:** IDENTIFIED
- **pattern:** P03

### ### cleanup.py:126

- **file:line:** `cleanup.py:126`
- **code excerpt:** `except OSError:`
- **what-it-does:** Catches OS errors during frame cleanup and silently continues.
- **why-fallback:** Cleanup failures (disk full, permission denied) are hidden — frames accumulate and fill the disk.
- **recommended-fix:** Log the error.
- **assigned-to:** US-024a (V2-024: frame cleanup OSError silent except)
- **status:** IDENTIFIED
- **pattern:** P01

### ### cleanup.py:140

- **file:line:** `cleanup.py:140`
- **code excerpt:** `except OSError:`
- **what-it-does:** Same silent OSError handling.
- **why-fallback:** Same issue.
- **recommended-fix:** Log the error.
- **assigned-to:** US-024a (V2-024: frame cleanup OSError silent except)
- **status:** IDENTIFIED
- **pattern:** P01

### ### cleanup.py:142

- **file:line:** `cleanup.py:142`
- **code excerpt:** `except OSError as e:`
- **what-it-does:** Catches specific OSError during cleanup and silently continues.
- **why-fallback:** Same as above — the exception is caught but not re-raised or logged.
- **recommended-fix:** Log the error.
- **assigned-to:** US-024a (V2-024: frame cleanup OSError silent except)
- **status:** IDENTIFIED
- **pattern:** P01

### ### cleanup.py:209

- **file:line:** `cleanup.py:209`
- **code excerpt:** `except OSError:`
- **what-it-does:** Catches OS errors during alert cleanup and silently continues.
- **why-fallback:** Alert cleanup failures are hidden — old alerts accumulate.
- **recommended-fix:** Log the error.
- **assigned-to:** US-024a (V2-024: alert cleanup OSError silent except)
- **status:** IDENTIFIED
- **pattern:** P01

### ### camera_creds.py:85

- **file:line:** `camera_creds.py:85`
- **code excerpt:** `except (IndexError, ValueError):`
- **what-it-does:** Catches URL parsing errors and returns None.
- **why-fallback:** Returns None which the caller may use as if valid — but in practice the caller checks for None (line 75-76), so this is a defensible fallback.
- **recommended-fix:** Log the error at debug level; raise to surface config bugs.
- **assigned-to:** US-029a (V2-029: silent exception on URL parsing — returns None)
- **status:** REMOVED
- **removed_by:** US-029c
- **removed_on:** 2026-09-13
- **pattern:** P01

### ### pipeline_cooldown.py:74

- **file:line:** `pipeline_cooldown.py:74`
- **code excerpt:** `def should_suppress(`
- **what-it-does:** Returns True if (camera_id, classification) is within cooldown window — drops real events silently.
- **why-fallback:** This IS the cooldown mechanism: it returns True and the caller suppresses the alert. The operator never sees suppressed events.
- **recommended-fix:** Emit a telemetry event when suppression occurs; log to audit trail.
- **assigned-to:** US-029a (V2-029: cooldown suppress with no operator visibility)
- **status:** IDENTIFIED
- **pattern:** P08

### ### pipeline_cooldown.py:42-43

- **file:line:** `pipeline_cooldown.py:42`, `pipeline_cooldown.py:43`
- **code excerpt:** `infra/gate_cooldown.py — gate-level (camera, event_type) cooldown` and `infra/cooldown.py — alert-level (alert_id) and bucket cooldowns`
- **what-it-does:** Docstring references to v1 modules (infra/cooldown, infra/gate_cooldown).
- **why-fallback:** These v1 modules were removed (V2-020), but the references remain in docstrings. Future editors may look for them and find nothing.
- **recommended-fix:** Remove dead references from docstrings.
- **assigned-to:** US-029a (V2-029: DEFERRED-to-V2-020 — v1 surface references in docstrings)
- **status:** DEFERRED-to-V2-020
- **pattern:** P11

### ### paths.py:89

- **file:line:** `paths.py:89`
- **code excerpt:** `PRODUCTION_MODE = os.environ.get("FARMSURV_PRODUCTION", "0") == "1"`
- **what-it-does:** Defaults to development mode ("0") when env var is unset.
- **why-fallback:** In production, a missing env var silently runs in development mode — some features behave differently (e.g., logging, safety checks).
- **recommended-fix:** Fail at boot if FARMSURV_PRODUCTION is not set in production environments.
- **assigned-to:** US-024b (V2-024: env-with-default for production mode)
- **status:** DEFERRED-to-V2-021
- **pattern:** P02

### ### paths.py:109

- **file:line:** `paths.py:109`
- **code excerpt:** `_DATA_DIR_OVERRIDE = os.environ.get("FARMSURV_DATA_DIR")`
- **what-it-does:** Returns None when env var is unset, falling through to the default DATA_DIR.
- **why-fallback:** This is a documented override mechanism; the None case is intentional. Not a problematic fallback.
- **recommended-fix:** Keep as-is.
- **assigned-to:** FALSE-POSITIVE (documented override mechanism; None case is intentional)
- **status:** FALSE-POSITIVE
- **pattern:** P03

### ### paths.py:147

- **file:line:** `paths.py:147`
- **code excerpt:** `IDENTITY_BACKUP_DIR = os.environ.get("FARM_IDENTITY_BACKUP_DIR", "")`
- **what-it-does:** Defaults to empty string (disabled) when env var is unset.
- **why-fallback:** Privacy-critical: face embeddings stay local-only by default. This is documented and intentional.
- **recommended-fix:** Keep as-is; it's a privacy-by-default design.
- **assigned-to:** FALSE-POSITIVE (privacy-by-default design; empty = face embeddings local-only)
- **status:** FALSE-POSITIVE
- **pattern:** P02

### ### paths.py:248

- **file:line:** `paths.py:248`
- **code excerpt:** `BROWSER_CHROME_PATH = os.environ.get("BROWSER_CHROME_PATH", _DEFAULT_CHROME_PATH)`
- **what-it-does:** Defaults to system Chrome when env var is unset.
- **why-fallback:** This is a convenience fallback for the browser automation script. The default path is well-known and reliable on macOS.
- **recommended-fix:** Keep as-is; not a critical fallback.
- **assigned-to:** FALSE-POSITIVE (convenience fallback for macOS Chrome path)
- **status:** FALSE-POSITIVE
- **pattern:** P02

### ### quick_classifier.py:80

- **file:line:** `quick_classifier.py:80`
- **code excerpt:** `NIGHT_SUPPRESS_ENABLED = os.environ.get("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0") == "1"`
- **what-it-does:** Night suppression is disabled by default when env var is unset.
- **why-fallback:** Disabled-by-default means night-mode false positives (IR reflections, indoor objects) pass through unfiltered until the operator explicitly enables suppression.
- **recommended-fix:** Add a startup warning when night suppression is disabled.
- **assigned-to:** US-025b (V2-025: env-with-default for night suppression enabled)
- **status:** DEFERRED-to-V2-021
- **pattern:** P02

### ### quick_classifier.py:81

- **file:line:** `quick_classifier.py:81`
- **code excerpt:** `NIGHT_CONF_FLOOR = float(os.environ.get("MOTION_GATE_NIGHT_CONF_FLOOR", "0.40"))`
- **what-it-does:** Defaults confidence floor to 0.40.
- **why-fallback:** This is a tuning parameter with a documented empirical default. Changing the default silently changes detection sensitivity.
- **recommended-fix:** Document in a startup log line; allow operator to audit with a flag.
- **assigned-to:** US-025b (V2-025: env-with-default for night confidence floor)
- **status:** IDENTIFIED
- **pattern:** P02

### ### quick_classifier.py:83

- **file:line:** `quick_classifier.py:83`
- **code excerpt:** `os.environ.get("MOTION_GATE_NIGHT_BRIGHTNESS_RATIO", "1.5")`
- **what-it-does:** Defaults brightness ratio to 1.5.
- **why-fallback:** Same as above — tuning parameter with a silent default.
- **recommended-fix:** Same as above.
- **assigned-to:** US-025b (V2-025: env-with-default for night brightness ratio)
- **status:** IDENTIFIED
- **pattern:** P02

### ### quick_classifier.py:301

- **file:line:** `quick_classifier.py:301`
- **code excerpt:** `except Exception as err:`
- **what-it-does:** Catches file load errors and returns a QuickVerdict with decision="pass" and top_class="error".
- **why-fallback:** When the gate can't load a frame, it passes it through to Qwen for classification — a frame the gate couldn't read gets a full LLM call, wasting resources and potentially producing wrong results.
- **recommended-fix:** Return QuickVerdict with decision="suppress" and a reason="frame_load_failed".
- **assigned-to:** US-025a (V2-025: frame load error silent except — passes to LLM)
- **status:** IDENTIFIED
- **pattern:** P01

### ### quick_classifier.py:651

- **file:line:** `quick_classifier.py:651`
- **code excerpt:** `except Exception:`
- **what-it-does:** Catches any error in night suppression check and returns False (suppression not applied).
- **why-fallback:** Silent failure in night suppression means night-mode false positives are NOT suppressed when they should be — the bug is invisible to the operator.
- **recommended-fix:** Log the error at debug level; don't silently skip suppression.
- **assigned-to:** US-025a (V2-025: night suppress check silent except)
- **status:** IDENTIFIED
- **pattern:** P01, P10

### ### quick_classifier.py:655

- **file:line:** `quick_classifier.py:655`
- **code excerpt:** `except Exception:`
- **what-it-does:** Catches any error in brightness ratio calculation and returns False.
- **why-fallback:** Silent failure means brightness check is skipped — night suppression logic gets an incorrect signal.
- **recommended-fix:** Log the error at debug level.
- **assigned-to:** US-025a (V2-025: brightness ratio calculation silent except)
- **status:** IDENTIFIED
- **pattern:** P01, P10

### ### frame_diff.py:113

- **file:line:** `frame_diff.py:113`
- **code excerpt:** `def load_frame(path: str) -> np.ndarray | None:`
- **what-it-does:** Returns None if the image file can't be read.
- **why-fallback:** The caller (`diff_pair_with_bbox`) checks for None and returns an empty mask, which cascades to None bbox, which cascades to alert suppression. This is a documented design decision (STRICT mode).
- **recommended-fix:** Keep as-is; this is intentional STRICT mode behavior.
- **assigned-to:** FALSE-POSITIVE (intentional STRICT mode; caller checks for None)
- **status:** FALSE-POSITIVE
- **pattern:** P09

### ### frame_diff.py:577

- **file:line:** `frame_diff.py:577`
- **code excerpt:** `def crop_frame_to_bbox(frame_path: str, bbox: tuple[int, int, int, int]) -> str | None:`
- **what-it-does:** Returns None if cropping fails (bbox out of bounds, file not found).
- **why-fallback:** The caller checks for None and suppresses the alert. This is intentional.
- **recommended-fix:** Keep as-is; this is intentional STRICT mode behavior.
- **assigned-to:** FALSE-POSITIVE (intentional STRICT mode; caller checks for None)
- **status:** FALSE-POSITIVE
- **pattern:** P09

### ### vision_analyzer.py:162

- **file:line:** `vision_analyzer.py:162`
- **code excerpt:** `entry = DISPATCH.get(mode)`
- **what-it-does:** Returns None if mode is not in DISPATCH, which is then checked on line 163 and raises VisionAnalyzerError.
- **why-fallback:** This is a controlled fallback with an explicit raise — not a problematic silent fallback.
- **recommended-fix:** Keep as-is.
- **assigned-to:** FALSE-POSITIVE (controlled fallback with explicit raise)
- **status:** FALSE-POSITIVE
- **pattern:** P03

---

## telegram_formatter/ + vehicle_matcher/

### ### detail.py:54

- **file:line:** `detail.py:54`
- **code excerpt:** `cls = vm2_result.get("class_confirmed") or vm2_result.get("class", "unknown")`
- **what-it-does:** Falls back from `class_confirmed` to `class` with a hard-coded `"unknown"` default when neither key exists.
- **why-fallback:** If both keys are absent (malformed VM2 output or schema drift), the caption silently says "Class confirmed: unknown" instead of raising — the operator never sees that the pipeline produced incomplete data.
- **recommended-fix:** Require both keys in the contract; raise `KeyError` or `ValueError` when neither is present so the caller can handle the error path.
- **assigned-to:** US-028a (V2-028: .get() fallback for class — defaults to 'unknown')
- **status:** IDENTIFIED
- **pattern:** P03

### ### dispatcher.py:125

- **file:line:** `dispatcher.py:125`
- **code excerpt:** `caption = msg.get("caption", "")`
- **what-it-does:** Returns empty string when `caption` key is missing from a message dict.
- **why-fallback:** `caption` is a required key per the message contract. An empty default means a malformed message (missing caption) silently sends an empty Telegram message instead of failing fast.
- **recommended-fix:** Access with `msg["caption"]` (raises KeyError on missing key) so malformed messages are caught at the dispatch boundary.
- **assigned-to:** US-027a (V2-027: .get() fallback for caption — required field)
- **status:** IDENTIFIED
- **pattern:** P03

### ### match_alert.py:45

- **file:line:** `match_alert.py:45`
- **code excerpt:** `if match_result.get("matched"):`
- **what-it-does:** Treats missing `matched` key as `False` (no-match path).
- **why-fallback:** `matched` is the primary output key from `match_vehicle()`. If the caller passes a malformed dict (missing `matched`), the message silently says "unrecognized vehicle" without any error signal — the operator sees a false-negative.
- **recommended-fix:** Use `match_result["matched"]` (key access) so a missing key raises immediately; or assert the key exists.
- **assigned-to:** US-028a (V2-028: .get() fallback for matched key — defaults to False)
- **status:** IDENTIFIED
- **pattern:** P03

---

## listener/

### ### pipeline.py:128

- **file:line:** `pipeline.py:128`
- **code excerpt:** `except (json.JSONDecodeError, OSError): return []`
- **what-it-does:** Catches JSON decode / OS errors during known-vehicle candidate file load and returns an empty list.
- **why-fallback:** When the candidate file is corrupt or unreadable, the pipeline falls back to "no known vehicles" — every vehicle is treated as unrecognized. This is a silent failure that the operator never sees.
- **recommended-fix:** Log a warning at the fallback; consider returning an error indicator so the caller knows the candidate list is unavailable.
- **assigned-to:** US-026a (V2-026: known-vehicle candidate file load silent except)
- **status:** IDENTIFIED
- **pattern:** P01

### ### pipeline.py:141

- **file:line:** `pipeline.py:141`
- **code excerpt:** `camera_id = alert.get("camera_id", "unknown")`
- **what-it-does:** Returns "unknown" when `camera_id` key is missing from the alert dict.
- **why-fallback:** A malformed alert (missing camera_id) gets routed with "unknown" as the camera — all downstream logging, cooldown tracking, and frame paths use "unknown", contaminating the data directory and making it impossible to distinguish real "unknown" cameras from malformed alerts.
- **recommended-fix:** Raise ValueError when camera_id is missing; this is a required field per the alert contract.
- **assigned-to:** US-026a (V2-026: .get() fallback for camera_id — required field)
- **status:** IDENTIFIED
- **pattern:** P03

### ### pipeline.py:142

- **file:line:** `pipeline.py:142`
- **code excerpt:** `alert_id = alert.get("id", camera_id)`
- **what-it-does:** Falls back to `camera_id` when `id` key is missing from the alert dict.
- **why-fallback:** Uses camera_id (which itself may be "unknown" from L141) as the alert_id. Alerts from the same camera without explicit ids will share an alert_id, causing frame dedup to silently drop legitimate distinct alerts.
- **recommended-fix:** Generate a uuid when `id` is missing instead of falling back to camera_id.
- **assigned-to:** US-026a (V2-026: .get() fallback for alert id — falls back to camera_id)
- **status:** IDENTIFIED
- **pattern:** P03

### ### pipeline.py:143

- **file:line:** `pipeline.py:143`
- **code excerpt:** `classification = alert.get("classification", "motion")`
- **what-it-does:** Returns "motion" when `classification` key is missing.
- **why-fallback:** A malformed alert gets classified as "motion" — cooldown tracking and gate decisions use the wrong classification, potentially suppressing or allowing events based on the wrong type.
- **recommended-fix:** Require `classification` in the alert contract; raise KeyError when missing.
- **assigned-to:** US-026a (V2-026: .get() fallback for classification — defaults to 'motion')
- **status:** IDENTIFIED
- **pattern:** P03

### ### pipeline.py:144

- **file:line:** `pipeline.py:144`
- **code excerpt:** `camera_label = alert.get("camera_label", camera_id)`
- **what-it-does:** Falls back to `camera_id` when `camera_label` is missing.
- **why-fallback:** Less critical than L141-143 since camera_label is for display only, but still masks missing payload keys.
- **recommended-fix:** Keep the fallback for robustness (operator-friendly), but log a warning when it fires.
- **assigned-to:** FALSE-POSITIVE (display-only fallback, logged on fire)
- **status:** FALSE-POSITIVE
- **pattern:** P03

### ### pipeline.py:207

- **file:line:** `pipeline.py:207`
- **code excerpt:** `mode = vm1_result.get("class", "vehicle")`
- **what-it-does:** Returns "vehicle" when `class` key is missing from the vm1_result dict.
- **why-fallback:** A malformed vm1_result (missing class) silently routes to vehicle matching — the pipeline runs vehicle match logic (L217-219) on non-vehicle data, wasting LLM calls and potentially producing wrong match results.
- **recommended-fix:** Require `class` in vm1_result; raise ValueError when missing or add a validation check.
- **assigned-to:** US-026a (V2-026: .get() fallback for class in vm1_result — defaults to 'vehicle')
- **status:** IDENTIFIED
- **pattern:** P03

### ### daemon.py:76

- **file:line:** `daemon.py:76`
- **code excerpt:** `_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()`
- **what-it-does:** Defaults to INFO log level when env var is unset.
- **why-fallback:** This is a documented operational default; the operator can override at boot. Not a problematic fallback.
- **recommended-fix:** Keep as-is.
- **assigned-to:** FALSE-POSITIVE (documented operational default; operator overrides at boot)
- **status:** FALSE-POSITIVE
- **pattern:** P02

### ### daemon.py:161

- **file:line:** `daemon.py:161`
- **code excerpt:** `alarm = payload.get("alarm")`
- **what-it-does:** Returns None when `alarm` key is missing; checked on L162 with `if not isinstance(alarm, dict): return None`.
- **why-fallback:** Controlled fallback with an explicit guard — the None case is handled. Not a problematic silent fallback.
- **recommended-fix:** Keep as-is.
- **assigned-to:** FALSE-POSITIVE (controlled rejection of malformed payloads; None check on L167)
- **status:** FALSE-POSITIVE
- **pattern:** P03

### ### daemon.py:166

- **file:line:** `daemon.py:166`
- **code excerpt:** `device_name = alarm.get("channelName") or alarm.get("device") or alarm.get("name")`
- **what-it-does:** Chains three `.get()` calls with no defaults; returns None if all keys are absent.
- **why-fallback:** If none of the device name keys exist in the alarm dict, `device_name` is None and the function returns None (L167-168). This is a controlled rejection of malformed payloads, not a silent fallback.
- **recommended-fix:** Keep as-is; the None check on L167 properly rejects the payload.
- **assigned-to:** FALSE-POSITIVE (controlled rejection; None check on L167)
- **status:** FALSE-POSITIVE
- **pattern:** P03

### ### daemon.py:170

- **file:line:** `daemon.py:170`
- **code excerpt:** `event_type = alarm.get("type", "unknown")`
- **what-it-does:** Returns "unknown" when `type` key is missing from the alarm dict.
- **why-fallback:** A malformed alarm (missing type) gets classified as "unknown" — this propagates into the alert classification, potentially affecting cooldown tracking and gate decisions with the wrong event type.
- **recommended-fix:** Require `type` in the alarm schema; raise ValueError when missing.
- **assigned-to:** US-026a (V2-026: .get() fallback for alarm type — defaults to 'unknown')
- **status:** IDENTIFIED
- **pattern:** P03

### ### daemon.py:171

- **file:line:** `daemon.py:171`
- **code excerpt:** `outer_type = payload.get("type", "")`
- **what-it-does:** Returns empty string when outer `type` key is missing from the payload.
- **why-fallback:** Empty string is falsy, so L172's `if outer_type and outer_type != event_type` short-circuits and the "unknown" from L170 stands. This is a controlled fallback but masks the fact that the outer payload was malformed.
- **recommended-fix:** Same as L170 — require `type` at the payload level.
- **assigned-to:** US-026a (V2-026: .get() fallback for outer payload type — empty string)
- **status:** IDENTIFIED
- **pattern:** P03

### ### daemon.py:174

- **file:line:** `daemon.py:174`
- **code excerpt:** `event_type = event_type.lower() if isinstance(event_type, str) else "unknown"`
- **what-it-does:** Falls back to "unknown" when event_type is non-string (e.g., int, None, bool).
- **why-fallback:** Union-type coercion: if the alarm type is not a string (schema drift), it silently becomes "unknown" instead of raising. This masks downstream schema mismatches.
- **recommended-fix:** Raise TypeError when event_type is not a string; log the actual value for diagnostics.
- **assigned-to:** US-026a (V2-026: union-type coercion for event_type — defaults to 'unknown')
- **status:** IDENTIFIED
- **pattern:** P06

### ### daemon.py:177

- **file:line:** `daemon.py:177`
- **code excerpt:** `alarm.get("time") or alarm.get("alarmTime") or datetime.now(UTC).isoformat()`
- **what-it-does:** Chains two `.get()` calls with no defaults, then falls back to current UTC time.
- **why-fallback:** If both timestamp keys are missing from the alarm, the alert gets a "now" timestamp instead of the actual alarm time — the operator sees a fake timestamp and cannot correlate with camera-side logs.
- **recommended-fix:** Prefer `datetime.now(UTC)` as a last resort but log a warning; or require at least one timestamp key.
- **assigned-to:** US-026a (V2-026: .get() fallback for alarm timestamp — defaults to now())
- **status:** IDENTIFIED
- **pattern:** P03

### ### daemon.py:206

- **file:line:** `daemon.py:206`
- **code excerpt:** `request.headers.get("X-Forwarded-For", request.remote_addr or "")`
- **what-it-does:** Returns `request.remote_addr` if X-Forwarded-For is absent; returns "" if both are missing.
- **why-fallback:** Empty remote_addr is extremely rare (only in test/edge cases), but when it happens the source_ip becomes "" and IP validation will fail on L228 — the request is rejected. This is a safe fallback (fail-closed).
- **recommended-fix:** Keep as-is; the fail-closed behavior on empty IP is correct.
- **assigned-to:** FALSE-POSITIVE (fail-closed; empty IP rejected by L228 validation)
- **status:** FALSE-POSITIVE
- **pattern:** P03

### ### daemon.py:235-236

- **file:line:** `daemon.py:235`, `daemon.py:236`
- **code excerpt:** `cam.get("name") == camera_label and source_ip == cam.get("ip")` and `matched_cam_id = cam.get("prefix", camera_id)`
- **what-it-does:** `.get("name")` / `.get("ip")` with no defaults in the matching loop; `.get("prefix", camera_id)` as fallback.
- **why-fallback:** `.get("name")` / `.get("ip")` returning None in a comparison is safe (None != string). `.get("prefix", camera_id)` is a controlled fallback — if "prefix" is missing, the camera_id itself is used as the prefix, which is the correct default.
- **recommended-fix:** Keep as-is.
- **assigned-to:** FALSE-POSITIVE (controlled empty-dict filter; not all stages produce TG messages)
- **status:** FALSE-POSITIVE
- **pattern:** P03

### ### daemon.py:265-267

- **file:line:** `daemon.py:265`, `daemon.py:266`, `daemon.py:267`
- **code excerpt:** `result.get("tg1", {})`, `result.get("tg2", {})`, `result.get("tg3", {})`
- **what-it-does:** Returns empty dict when tg1/tg2/tg3 keys are missing from pipeline result.
- **why-fallback:** Empty dict is falsy, so L269's list comprehension filters them out. The dispatch block is skipped silently. This is intentional — not all pipeline stages produce Telegram messages (e.g., gate-suppressed alerts).
- **recommended-fix:** Keep as-is; the empty-dict filter pattern is correct and intentional.
- **assigned-to:** FALSE-POSITIVE (controlled empty-dict filter; not all stages produce TG messages)
- **status:** FALSE-POSITIVE
- **pattern:** P03

### ### daemon.py:284-287

- **file:line:** `daemon.py:284`
- **code excerpt:** `except Exception as exc: log.error("tg-dispatch: unexpected %s: %s", type(exc).__name__, exc)`
- **what-it-does:** Catches any exception during Telegram dispatch and logs it.
- **why-fallback:** Unlike P01 silent swallows in infra/, this handler logs the error at ERROR level. The alert pipeline still completes successfully and returns HTTP 200. The operator sees the gap in logs/daemon.log. This is the intended design (US-022b: "daemon still returns HTTP 200 even if dispatch fails").
- **recommended-fix:** Keep as-is; this is a deliberate design choice, not a silent failure.
- **assigned-to:** FALSE-POSITIVE (logged at ERROR level per US-022b; pipeline returns HTTP 200)
- **status:** FALSE-POSITIVE
- **pattern:** P10

### ### daemon.py:303

- **file:line:** `daemon.py:303`
- **code excerpt:** `tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")`
- **what-it-does:** Returns empty string when TELEGRAM_BOT_TOKEN env var is unset.
- **why-fallback:** Empty Telegram token in the plist means the operator's launchd plist will contain an empty string for the bot token — the daemon will fail to connect to Telegram at runtime. This is a boot-time failure, not a silent runtime failure.
- **recommended-fix:** Keep as-is; the failure is caught at dispatch time (L274: `os.environ["TELEGRAM_BOT_TOKEN"]` raises KeyError).
- **assigned-to:** FALSE-POSITIVE (boot-time failure, not silent runtime failure)
- **status:** FALSE-POSITIVE
- **pattern:** P02

### ### daemon.py:304

- **file:line:** `daemon.py:304`
- **code excerpt:** `tg_chat_id = os.environ.get("TELEGRAM_HOME_CHAT_ID", "")`
- **what-it-does:** Returns empty string when TELEGRAM_HOME_CHAT_ID env var is unset.
- **why-fallback:** Same as L303 — empty in plist, fails at dispatch time with KeyError.
- **recommended-fix:** Keep as-is.
- **assigned-to:** FALSE-POSITIVE (documented dev default; fails visibly at runtime)
- **status:** FALSE-POSITIVE
- **pattern:** P02

### ### daemon.py:305

- **file:line:** `daemon.py:305`
- **code excerpt:** `vision_url = os.environ.get("VISION_LLM_URL", "http://127.0.0.1:8080")`
- **what-it-does:** Defaults to localhost:8080 when VISION_LLM_URL is unset.
- **why-fallback:** This is a documented operational default for local dev. In production, the operator must set VISION_LLM_URL in launchd. If unset, the daemon connects to the local dev LLM — a misconfiguration that fails visibly at runtime, not silently.
- **recommended-fix:** Keep as-is; this is a documented dev default that fails visibly at runtime.
- **assigned-to:** FALSE-POSITIVE (documented dev default; fails visibly at runtime)
- **status:** FALSE-POSITIVE
- **pattern:** P02

### ### daemon.py:429

- **file:line:** `daemon.py:429`
- **code excerpt:** `values = dotenv_values(env_path) or {}`
- **what-it-does:** Returns empty dict when dotenv_values fails or returns None.
- **why-fallback:** Controlled fallback — if the env file is empty or unreadable, the for loop simply has no keys to load. This is intentional behavior.
- **recommended-fix:** Keep as-is.
- **assigned-to:** FALSE-POSITIVE (controlled fallback; empty dict means no keys to load)
- **status:** FALSE-POSITIVE
- **pattern:** P03

---

## Summary

| Pattern | Count (IDENTIFIED) | Count (FALSE-POSITIVE) | Count (DEFERRED) |
|---------|-------------------|----------------------|------------------|
| P01: silent exception swallow | 19 | 0 | 0 |
| P02: env-with-default | 2 | 5 | 4 |
| P03: .get() returning fallback | 13 | 5 | 0 |
| P04: optional kwarg with branch | 2 | 0 | 0 |
| P05: multi-format normalizer | 0 | — | — |
| P06: union-type coercion | 1 | — | — |
| P07: membership fallback | 0 | — | — |
| P08: cooldown/suppress | 1 | 0 | 0 |
| P09: Optional[] in hot signatures | 3 | 2 | 0 |
| P10: broad catch | 5 | 0 | 0 |
| P11: v1 surface | 0 | 0 | 1 |
| P12: resize/JPEG/letterbox | 0 | — | — |

**Total IDENTIFIED:** 40 entries (all assigned to V2-024 through V2-029 removal PRDs) — note: per-pattern column sums exceed 40 due to entries tagged with multiple patterns (7 entries have dual patterns).
**Total FALSE-POSITIVE:** 18 entries (documented as intentional, no removal needed).
**Total DEFERRED:** 5 entries (1 to V2-020, 4 to V2-021).
**Grand total:** 63 entries across all categories.

**Scanner hits from `find_fallbacks.py`:** 6 (all P02 env-with-default — scanner found LISTEN_HOST, LISTEN_PORT, VISION_LLM_URL at generate_plist() and main(), plus LOG_LEVEL at module level).
**Scanner misses from manual review:** All P01 silent except, P03 .get() fallbacks, P06 union-type coercion in listener/.

**Manual additions:** 43 entries (31 IDENTIFIED + 12 FALSE-POSITIVE from listener/ audit).

---

## Disposition Summary (operator-approved 2026-09-10)

**IDENTIFIED — 40 entries** → assigned to V2-024 through V2-029 removal PRDs:
- **V2-024** (infra/ frame pipeline): 18 entries (frame_capture.py × 12, cleanup.py × 4, paths.py × 1, gate.py × 1 DEFERRED-to-V2-021)
- **V2-025** (gate/classifier): 11 entries (gate.py × 4 IDENTIFIED, quick_classifier.py × 7)
- **V2-026** (listener/ pipeline + daemon): 8 entries (pipeline.py × 4, daemon.py × 4)
- **V2-027** (telegram dispatch): 1 entry (dispatcher.py × 1)
- **V2-028** (vehicle match): 2 entries (detail.py × 1, match_alert.py × 1)
- **V2-029** (test infra / misc): 2 entries (camera_creds.py × 1, pipeline_cooldown.py × 1)

**FALSE-POSITIVE — 18 entries** → documented as intentional, no removal needed.

**DEFERRED — 5 entries** → to V2-020 (1), V2-021 (4).

---

**FROZEN 2026-09-10** — All 63 inventory entries have operator-approved disposition. No new entries may be added without re-opening this story.
