# Changelog

All notable changes to `ai_camera_monitor` are documented here.
This project does **not** follow [Semantic Versioning](https://semver.org/) in
the strict sense — versions mark the public release cadence, not API
stability guarantees. Pin to a tag if you need reproducibility.

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