# Alert Pipeline — Actual Architecture (2026-09-27)

**Drawn from `_run_pipeline()` in `listener/daemon.py:351-535` and `infra/gate.py`.**

This is what is **actually** wired today, not what the README claims.

## ASCII diagram — actual flow

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ /alert POST  (listener/daemon.py:579)                                         │
│   - normalize_reolink(payload, source_ip)                                    │
│   - validate_source_ip(camera_id, source_ip)                                 │
│   - get_recent_frames(camera_id, n=4)          ◄── RTSP ring buffer         │
│   - alert_id = uuid.uuid4()                                                  │
│                                                                              │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 3  load_frames()         listener/pipeline.py:111                │   │
│ │   "no frames → refuse to process"                                       │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 4  YOLO GATE             infra/gate.py:1004  run()              │   │
│ │   per-crop classify_frame():                                           │   │
│ │     crop A → QuickVerdict(class=car,  conf=0.40, suppress=YES)         │   │
│ │                  reason="night_suppress_camera_class"   ✅ suppressed │   │
│ │     crop B → QuickVerdict(class=cat,  conf=0.67, suppress=NO)          │   │
│ │                  reason=""                              ❌ NOT suppr. │   │
│ │   _route_decision() picks highest-conf *passing* verdict (Rule 3):     │   │
│ │     top_class="cat" in ANIMAL_CLASSES                                   │   │
│ │     → return ("animal", "cat", 0.67, "animal_classified")              │   │
│ │                                                                        │   │
│ │   Gate verdict dict: {classification="animal",                          │   │
│ │                       class_label="cat",                                │   │
│ │                       confidence=0.67,                                  │   │
│ │                       reason="animal_classified"}                       │   │
│ │                                                                        │   │
│ │   *** Gate has NO check of (camera × time-of-day × any-class) ***      │   │
│ │   *** Gate has NO knowledge of which models will run after it ***       │   │
│ │   *** Gate has NO override path for higher-quality downstream models ***│   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 7a  COOLDOWN CHECK      listener/pipeline.py:125                 │   │
│ │   should_suppress(camera_id, classification="animal", now)             │   │
│ │   checks infra/pipeline_cooldown.py  (per-camera+per-class window)     │   │
│ │   No prior hits → proceed                                              │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 8  PREPARE ARTIFACTS    infra/alert_artifacts.py                │   │
│ │   write crops + composite to data/frames/<cam>/<alert_id>/             │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 9  VM1 (verify_class)   infra/vision_analyzer.py                │   │
│ │   → qwen2.5-vl via llama-server :8093                                  │   │
│ │   returns {"class": "vehicle", "subclass": "SUV",                      │   │
│ │             "confidence": "definite", "notes": "..."}                   │   │
│ │                                                                        │   │
│ │   *** VM1 result is NEVER compared against gate verdict ***            │   │
│ │   *** VM1 result is NEVER allowed to flip classification ***           │   │
│ │   *** VM1 only gets to short-circuit if class == "unsure" ***         │   │
│ │       (Stage 9b, daemon.py:437)                                        │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 9b  VM1 UNSURE SHORT-CIRCUIT  daemon.py:437                       │   │
│ │   if vm1_result["class"] == "unsure":                                  │   │
│ │     drop alert, write event with dropped="vm1_unsure"                  │   │
│ │                                                                        │   │
│ │   *** No "vm1 disagrees with gate" override path ***                   │   │
│ │   *** No "VM1 says vehicle, gate says animal — re-route" ***           │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 10  TG#1 BUILD          telegram_formatter/alert.py              │   │
│ │   emoji + class line uses vm1_result["class"]    ◄── user sees vehicle │   │
│ │   class_label used in TG comes from VM1                                  │   │
│ │   USER THINKING: "this is a vehicle alert"                              │   │
│ │   SYSTEM THINKING: classification="animal" from gate                     │   │
│ │                                                                        │   │
│ │   ⚠ DIVERGENCE: caption shows VM1's class but pipeline state is gate's │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 10b  VM2 (detail_class)  vision_analyzer.py                       │   │
│ │   mode = vm1_result["class"] = "vehicle"                              │   │
│ │   → "Ford, black, suv"                                                  │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 11  TG#2 BUILD          telegram_formatter/detail.py              │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 12  PER-CLASS MATCH     listener/pipeline.py:240                  │   │
│ │   match_vehicle(vm2_result, candidates)                                  │   │
│ │   → {"matched": false}                                                  │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 13  TG#3 BUILD          "No match" text-only                      │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ Stage 13b  BASELINE OVERRIDE  infra/alert_overrides_baseline.py          │   │
│ │   checks if VM1 says "distant vehicle" → override caption + suppression │   │
│ │   *** Fires AFTER TG#1 already sent — cannot stop it ***               │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ WRITE EVENT.JSON         infra/event_record.py                          │   │
│ │   event.json contains BOTH gate.classification="animal"                 │   │
│ │                          AND vm1.class="vehicle"                        │   │
│ │                          AND vm2.make="Ford"                            │   │
│ │   *** Operator reading event.json sees the conflict — but alerts are  │   │
│ │       already in Telegram by this point ***                             │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                    ▼                                         │
│ ┌────────────────────────────────────────────────────────────────────────┐   │
│ │ TG#1 → TG#2 → TG#3 DISPATCH  listener/daemon.py:651-693               │   │
│ │   record_hit(camera, classification="animal", ...)                      │   │
│ │   *** cooldown starts AFTER alerts fire — useless for this alert ***   │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Issues identified (numbered)

### 1. **Gate (YOLO) decides routing; VM1 (LLM) cannot override**
Gate returns `classification="animal"` because YOLO crop B said cat. VM1 later returns `class="vehicle"`. There is no stage that re-routes based on VM1 disagreeing with the gate. The pipeline forwards TG based on the gate's verdict, but the TG caption shows VM1's class. **Result: user sees "vehicle • definite" but the system internally believes "animal".** This is the root cause of the Ford SUV being alerted as a Ford SUV despite the gate wanting to drop it as an animal false-positive.

### 2. **Per-crop night-suppress is class-specific**
`infra/quick_classifier.py` suppresses per-crop based on class. The crop with `class=cat` passes the per-crop heuristic (cat is not in `FARMSURV_NIGHT_SUPPRESS_CLASSES`). When the routing picks `cat` over `car`, the suppression is silently abandoned at the alert level.

### 3. **No camera × time-of-day gate at the alert level**
The two layers of "camera + class night suppress" and "quiet hours Telegram gate" both exist, but neither checks (camera × night × any-class). If you want "OUTSIDE_BACK_SOLAR at night = silence regardless of class," that rule does not exist anywhere.

### 4. **Cooldown fires AFTER TG#3 dispatch**
`infra_pipeline_cooldown.record_hit()` is called only after TG#3 dispatches successfully (daemon.py:691). Cooldown's purpose is to suppress the *next* alert — but at that point, the user already got this one. For the Ford SUV that triggered twice in the same location (00:57, 04:16), cooldown would have helped on the second trigger only if the system had reached the record_hit call, which it does — but only AFTER the second TG#3 fires.

### 5. **Distant-vehicle baseline override runs AFTER TG#1 sends**
Stage 13b (`stage_apply_baseline_override`) is positioned after TG#1 has been queued. It can adjust the alert context but cannot recall the message. For the "vehicle at distance, headlight-only" FP class (V2-062), this is a known gap — the operator chose this design but it's worth re-examining.

### 6. **TG caption diverges from pipeline state**
The caption shows VM1's class ("vehicle • definite"). The pipeline state holds the gate's classification ("animal"). If the user reads the alert and the system log side-by-side, they don't match. This is the source of operator confusion in the morning: "I got a vehicle alert but you said the system suppressed it as an animal."

### 7. **Stage 9b "VM1 unsure" is the only downstream gate override**
This was added 2026-09-20 per `daemon.py:439-461`. It catches VM1-says-unsure, but does NOT catch VM1-says-vehicle-when-gate-said-animal. The "unsure" case is the easy case; the "disagreement" case is harder because it requires choosing a winner.

### 8. **Stage 10b "VM2 detail_class" uses VM1's class as `mode`**
`stage_detail_class_vm2` takes `mode = vm1_result["class"]` (daemon.py:475). So when VM1 says "vehicle", VM2 runs the vehicle prompt. Good. But the TG caption shows VM2's result (Ford) while the pipeline state shows gate's animal classification. So VM2 is reinforcing VM1 against the gate. This makes the divergence more confusing — the caption goes deeper into "vehicle" territory while the system route stays "animal."

### 9. **No event.json pre-dispatch hook**
The pipeline writes `event.json` *after* it has decided to alert (Stage: write_event_record at daemon.py:512). If the gate had a "decision log" emitted before the heavy work (VM1/VM2/TG), there would be evidence of bypass attempts before they happened. Today, the only way to detect a Ford-SUV-as-cat alert is after it has been sent.

### 10. **`infra/cooldown.py` referenced in conversation does not exist**
`infra/pipeline_cooldown.py` exists. The contract "cool down before TG#3" is enforced; but the file name in the codebase is `pipeline_cooldown`, not `cooldown`. Stale references in conversation context can mislead.

## Suppression-related env knobs (where they actually fire)

| Knob | File | Where it fires | What it actually does |
|---|---|---|---|
| `MOTION_GATE_NIGHT_SUPPRESS_ENABLED` | `infra/quick_classifier.py:81` | Per-crop YOLO classify | If night + brightness low, suppress car class on cameras in `FARMSURV_NIGHT_SUPPRESS_CAMERAS` |
| `FARMSURV_NIGHT_SUPPRESS_CAMERAS` | `infra/quick_classifier.py` | Same as above | Which cameras get the camera+class night suppress |
| `FARMSURV_NIGHT_SUPPRESS_CLASSES` | `infra/quick_classifier.py` | Same as above | Which COCO classes are subject to camera+class night suppress (default: car,truck,bicycle,motorcycle,bus) |
| `FARMSURV_QUIET_HOURS_ENABLED` | `infra/quiet_hours.py:86` | TG#1/TG#2/TG#3 dispatch | If night hours (sunset+30 to sunrise-30), DON'T send TG |
| `FARMSURV_QUIET_HOURS_CAMERAS` | `infra/quiet_hours.py` | Same as above | Empty by default = applies to all cameras |
| `infra_pipeline_cooldown` | `infra/pipeline_cooldown.py` | Stage 7a + record_hit at end | Per-(camera, classification) suppression window — fires AFTER TG#3 |

**Gap:** `FARMSURV_QUIET_HOURS_ENABLED=0` is the live value. So the TG-layer night suppress is disabled. The motion-gate per-crop night suppress IS enabled, but it only suppresses COCO car/truck classes (not cat). Hence the bypass.

## What was supposed to happen vs. what did

**Intended (operator's design):** "If a Ford SUV is parked at OUTSIDE_BACK_SOLAR at 4 AM, do not alert. Camera is configured for night suppression."

**Actual:** YOLO said cat on one crop. Gate routed as animal. Per-crop night suppress was class-specific. Gate has no camera-level time-of-day override. VM1 correctly identified vehicle but had no power to re-route. TG#1 sent "vehicle • definite" to user. User confused.

## What needs fixing (architectural, not patches)

**Either:**
- **A) Demote the gate:** Make the gate drop-or-keep only (binary), and route classification exclusively from VM1. This requires VM1 to run for *every* alert (or for any alert the gate kept), but eliminates the gate-vs-VM1 divergence.
- **B) Promote VM1's override:** Add Stage 9c that says "if gate.classification ≠ vm1.class, vm1 wins." Combined with A-camera-level-night-suppress for camera+time-of-day at the gate.
- **C) Reject the gate's classification layer entirely:** Gate returns only {kept, dropped}. VM1 runs on every kept alert. VM1's class drives TG. This is closer to V2-062's design intent but requires more compute per alert.

**Recommendation (per operator instruction to design before patching):**
- Pick (B). Keep gate cheap filtering, but VM1 must be able to flip the route.
- Add a camera × time-of-day gate at Stage 4 that drops alerts before VM1 even runs. Configured via existing env vars (no new knobs — reuse `FARMSURV_NIGHT_SUPPRESS_CAMERAS`).
- Make TG caption and pipeline state agree (use a single source of truth for classification, prefer VM1 when present).
- Move cooldown record_hit to gate-decision time, not TG#3 time.

---

**This diagram is the deliverable.** Once you confirm (A/B/C) I'll write a single US-091 with all four changes scoped.
