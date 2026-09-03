# Changelog

All notable changes to `ai_camera_monitor` are documented here.
This project does **not** follow [Semantic Versioning](https://semver.org/) in
the strict sense — versions mark the public release cadence, not API
stability guarantees. Pin to a tag if you need reproducibility.

---

## [0.4.2] — 2026-09-02

Hotfix on top of v0.4.1. v0.4.1 fixed the import regression but
introduced (and was followed by) two additional runtime bugs that
crashed the pipeline on the most common suppression path — motion
gate says "no subject" — preventing every alert from reaching the
classification stage.

### Fixed

- **`listener/single_pipeline.py`** — defensive guard for empty
  `frame_paths`. The motion gate can suppress an alert with no
  subject detected across the 4-frame window, leaving `frame_paths`
  empty. The pipeline previously called `frame_diff_fn(frame_paths)`
  unconditionally, which raised `IndexError` inside
  `listener._frame_diff_fn` (it tried to index `frame_paths[-1]`).
  The pipeline now returns `PipelineResult(skipped_reason="no_frames",
  log_only=True)` and never invokes `frame_diff_fn` on an empty list.

- **`listener/listener.py`** — `log_fn` adapter for structured context.
  The pipeline calls `log_fn(message, alert_id=..., camera=...,
  event=...)`, but `log_fn` was wired to `log.info` directly. Stdlib
  `logging.Logger.info` does **not** accept arbitrary kwargs; it
  treats them as `LogRecord` factory fields, raising
  `TypeError: Logger._log() got an unexpected keyword argument
  'alert_id'`. Added `listener._log_with_context(message, **context)`
  which stringifies the context and appends it to the message.

### Tests

- `listener/tests/test_single_pipeline.py::TestEmptyFramesGate` —
  two cases covering (1) empty `frame_paths` returns
  `skipped_reason="no_frames"` with `log_only=True` and logs the
  suppression reason; (2) `frame_diff_fn` is never invoked when
  `frame_paths` is empty.

## [0.4.1] — 2026-09-02

Hotfix on top of v0.4.0. v0.4.0 shipped a critical regression that broke
the pipeline at import time when the listener is launched as a script
(via launchd plist). This patch fixes the import and restores pipeline
operation. No behavior changes; pin to v0.4.0 only if you do not run
the listener via a launchd plist or systemd unit that invokes
`listener/listener.py` directly.

### Fixed

- **`listener/single_pipeline.py`** — relative import for sibling module
  (`from .pipeline_filters import …`). The v0.4.0 release used an
  absolute import (`from listener.pipeline_filters import …`) which
  fails at runtime with `ModuleNotFoundError: 'listener' is not a
  package` when `listener.py` is launched as a top-level script. This
  affected every operator running the listener via launchd, systemd,
  or any host that does not invoke it as `python -m listener.listener`.

## [0.4.0] — 2026-09-02

Single-pipeline architectural release. Consolidates the
classify → per-class pipeline (person/animal/vehicle) into one
orchestrator (`listener/single_pipeline.py`) with shared cooldown
+ vehicle camera allowlist between Qwen call 1 (shared classify)
and Qwen call 2 (per-class cascade). End-user behavior unchanged
from v0.3.0 when run with the default config; the pipeline now
applies a 15-minute property-wide cooldown for person + animal
events after Qwen call 1 confirms the class, and restricts vehicle
matching to the two designated solar cameras.

### Added

- **`listener/single_pipeline.py`** — new orchestrator. Replaces the
  per-class pipeline dispatch with a single webhook → RTSP-presence
  → pairwise diff → 2 crops → Qwen call 1 (shared classify) →
  PipelineCooldown (15-min, property-wide, person+animal only) →
  VEHICLE_CAMERAS_ALLOWLIST (CAM5/CAM6 only) → Qwen call 2
  (per-class) → matcher → Telegram flow.
- **`listener/pipeline_filters.py`** — `PipelineCooldown` class
  (property-wide, per-class, time-window throttle) + the
  `VEHICLE_CAMERAS_ALLOWLIST` frozenset (resolves friendly names to
  CAM{N} codes via `infra.cameras.code_for()`; falls back to friendly
  names when `cameras.env` is unavailable).
- **`listener/matcher_adapters.py`** — uniform adapter shape for
  person/animal/vehicle matcher output. Adapters normalize each
  matcher’s native result schema into a single shape for downstream
  Telegram formatting.
- **`infra/two_call_cascade.py`** — split into `cascade_call1()`
  (shared classify) and `cascade_call2()` (per-class).
- **New prompt + schema modules**:
  `infra/classify_prompt.py`, `infra/classify_schema.py`,
  `infra/classify_validator.py`, `infra/person_prompt.py`,
  `infra/animal_prompt.py`, `infra/vehicle_prompt.py`,
  plus the matching `_template` variants.
- **New tests**: `test_pipeline_filters_111513.py`,
  `test_pipeline_filters_integration_111513.py`,
  `test_single_pipeline.py`, `test_matcher_adapters.py`,
  `test_classify_prompt.py`, `test_two_call_cascade.py`,
  `test_person_prompt.py`, `test_animal_prompt.py`,
  `test_vehicle_prompt.py`.

### Notes

- The two-crop invariant is preserved: webhook → pairwise diff →
  exactly `crop_a` + `crop_b`.
- Vehicle matching is gated by `VEHICLE_CAMERAS_ALLOWLIST` which
  resolves to `{"CAM5", "CAM6"}` when `cameras.env` is loaded, or
  `{"Outside Front Solar", "Outside Back Solar"}` as a defensive
  fallback.
- Property-wide cooldown applies to person + animal only. Vehicle
  alerts are cooldown-free.
- **Known v0.4.0 regression (fixed in v0.4.1)**: launched via a
  top-level script (e.g. via launchd plist), `from listener.pipeline_filters`
  fails with `ModuleNotFoundError`. Pin to v0.4.1 or apply the
  one-line relative-import patch documented above.

## [0.3.0] — 2026-09-02

Incremental release on top of v0.2.1. Multi-area refactor and tuning hardening
covering Phase 6B.171–175, §11.88–§11.114. No behavior-changing features; this
release ships refactors + new tests that pin recent fixes.

### Added

- **§11.111 — Vehicle pipeline extraction** (`listener/`). The 1554-line
  `listener/vehicle_event_pipeline.py` monolith is split into a 9-module
  `listener/vehicle_pipeline/` package (`motion`, `vision`, `crop`, `identity`,
  `match`, `telegram`, `alert`, `enrichment`, `__init__`). Zero behavior change;
  pure structural refactor with 18 symbols remapped at the listener driver
  boundary.
- **§11.110 — QuickClassifier surveillance-class priority** (`infra/quick_classifier.py`).
  The `top_class` ordering now prefers the surveillance-class category when the
  model's first and second confidence candidates are close in score. Prevents
  rare-but-catastrophic "vehicle missed because background scored 0.001 higher"
  regressions.
- **Phase 6B.171 — Subject bbox for motion-gate crops** (`listener/motion_gate_pipeline.py`).
  5 of 7 "empty crop" alerts now ship a subject bbox instead of an empty crop.
- **Phase 6B.175 — Two independent per-frame subject bboxes** (`listener/motion_gate_pipeline.py`).
  Replaces the single-bbox pair model with one bbox per frame; downstream
  matcher sees both crops' subject regions.
- **§11.88 — Stop lossy downsample + JPEG compression in frame pipeline**
  (`listener/frame_pipeline.py`). Quality preserved end-to-end; crop quality
  is no longer the matcher bottleneck.

### Changed

- Camera codes `CAM1 / CAM2 / CAM3 / CAM4` are genericized to `CAM1 / CAM2 / CAM3 / CAM4`
  in all public-facing fixtures and config templates. Internal aliases retain
  the legacy codes for backward compat.
- `listener/listener.py` rewritten as a thin orchestrator; pipeline logic lives
  in the `listener/vehicle_pipeline/` package.
- Test fixtures sanitized: owner-name identities replaced with `person_a`,
  `employee_a`, `employee_b`; internal paths replaced with `ai_camera_monitor`;
  chat IDs replaced with `000000000`.

### Notes

- This release does NOT change detection behavior for vehicle/person/animal
  alerts. The §11.113 leakage probe (Variant A unified prompt) confirmed that
  Variant B (per-class pipelines) is the correct architecture — see internal
  §11.113 for the empirical results. Variant A code remains in the private
  repo as `STATUS: experimental` for audit-trail purposes only.
- YOLO remains suppress-only (used to silence false-positive frames before
  vision-LLM calls). Not used for primary detection.
- Public repository versions are release-cadence markers, not strict SemVer.
  Pin to a tag if you need reproducibility.

---

## [0.2.1] — 2026-08-31

Incremental release on top of v0.2.0. Two bug fixes targeting pre-existing
issues in the v0.2.0 release that were identified during production use.

### Fixed

- **Phase.168 — Gatekeeper membership was always False** (`listener.py`,
  `listener/vehicle_event_pipeline.py`, `infra/vision_queue.py`). The
  vehicle-event pipeline compared the friendly webhook label against
  a CAM{N}-keyed set; the comparison never matched, so the match-alert
  path silently bypassed every vehicle event. Fix: `AlertContext`
  now carries a `camera_code` field populated once at the listener
  driver boundary, and the four gatekeeper membership checks
  compare code-vs-code. `GATEKEEPER_CAMERAS` + `PHASE6A_ELIGIBLE_CAMERAS`
  in `infra/vision_queue.py` are CAM{N}-only.
- **Phase.169 — Diff bbox cropped the wrong frame** (`listener/motion_gate_pipeline.py`).
  The motion gate computed bbox_a from the diff between frame 2 and 3,
  but cropped frame 3 — the LATER frame in the diff pair. The LATER
  frame often shows the subject past the bbox boundary, so the crop
  contained only a smear. Fix: bbox_a now crops frame 2, bbox_b
  crops frame 3 (the EARLIER frame of each pair). YOLO classification
  path is unchanged; downstream Qwen + matcher see a tighter crop
  and classify with higher confidence.

### Added

- `listener/tests/test_vehicle_event_pipeline_6B168.py` — 11 regression
  tests pinning the Phase.168 contract (camera_code surface,
  match_stage uses it, vision queue sets are CAM{N}-only).
- 2 new tests in `listener/tests/test_motion_gate_pipeline.py` —
  pin the Phase.169 frame mapping for both PIL and disk-path
  branches, and assert both crops are non-empty.

### Notes

- Public repository versions are release-cadence markers, not strict
  SemVer. Pin to a tag if you need reproducibility.

---

## [0.2.0] — 2026-08-31

**Full overwrite release.** This version **replaces** all prior public
history. The previous public release (`dab8311` and earlier tags) contained
internal IP addresses and operator identifying information throughout.
That history is intentionally not preserved in the public repository.

### Added
- Sanitized public release pipeline (`devops/open-source-release-pipeline`
  skill) — repeatable production-to-public flow with PII scrubbing.
- `skills/` subtree with 35 PII-clean Hermes Agent skills (alert routing,
  AI inference, GitHub workflows, productivity tools, smart-home,
  image/video generation, etc.).
- `docs/AGENT-MATCHER-TUNING-LESSONS.md` — public field guide for AI
  agents tuning identity matchers without shipping wrong-name failure
  modes.
- Tier A + B test subset — 40 sanitized test files covering the core
  scoring, cooldown, motion, prompt-template, camera-credentials, and
  config-format modules.
- Pre-commit CI configuration (`.pre-commit-config.yaml`).

### Changed
- README rewritten for public audience (operator paths and internal IPs
  removed).
- Listener test files and vendor-specific code paths removed.
- Internal docs (PLAN.md, AGENTS.md, the internal project plan,
  ARCHITECTURE.md) excluded from public release.

### Removed
- vendor-specific NAS preview backend (`infra/<vendor-specific NAS>_preview.py`).
- Tier C test files (heavy operator-name fixtures; not scrubbable
  without losing pedagogical context).
- Internal research and cleanup notes (CLEANUP-*.md, RESEARCH-*.md,
  PROPOSAL-*.md, SKILL-*.md).

### Security
- All PII (operator names, family names, Telegram chat IDs, internal
  IPs, local filesystem paths) scrubbed from tracked files.
- `gitleaks` + `trufflehog` scans clean.
- Single squashed root commit; prior public history overwritten.

### Migration note for prior users
If you pinned to a v0.1.x tag, the upstream HEAD has changed
fundamentally. Re-clone and re-pin to `v0.2.0`. The old `dab8311` tree is
no longer reachable from this repository.

---

## [0.1.0]–[0.1.2] — superseded

The 0.1.x line shipped initial public releases but contained internal
infrastructure details. The 0.2.0 release supersedes them entirely; the
0.1.x history is not preserved in this repository.