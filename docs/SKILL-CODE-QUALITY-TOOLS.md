---
name: code-quality-tools
description: "Use when running bandit (security), mypy (types), or vulture (dead code) on the ai_camera_monitor repo. Covers invocation, configuration, fix patterns, and the triage workflow for each tool."
version: 1.0.0
author: Jill + Note (name one)
license: MIT
---

# Code Quality Toolchain — ai_camera_monitor

The repo's quality toolchain (Phase.119+) is **ruff + bandit + mypy + vulture + pytest**. This file is the source of truth for how to run each tool, what to fix vs suppress, and the triage workflow.

All three tools are configured in `pyproject.toml` under `[tool.bandit]`, `[tool.mypy]`, `[tool.vulture]`. All are installed via `pip install -e ".[dev]"`.

---

## Quick reference

```bash
cd ~/ai_camera_monitor

# Run all four gates in sequence (use as pre-commit check)
.venv/bin/ruff check infra/ listener/ scripts/           # lint (already wired)
.venv/bin/bandit -r infra/ listener/ -c pyproject.toml  # security
.venv/bin/mypy --explicit-package-bases \
    --exclude='_.*archive' --exclude='.*archive.*' \
    infra/ listener/ vehicle_position/ vehicle_identifier/ \
    vehicle_matcher/ known_vehicles/ telegram_formatter/ pipeline/  # types
.venv/bin/vulture infra/ listener/ vehicle_position/ vehicle_identifier/ \
    vehicle_matcher/ known_vehicles/ telegram_formatter/ pipeline/  # dead code
.venv/bin/python -m pytest                              # tests
```

**Expected baselines (2026-08-26, commit 6a03828):**

| Tool | Errors | Notes |
|---|---|---|
| ruff | 0 | Already enforced |
| bandit | 0 | One `# nosec` per suppressed line; rationale in comment |
| mypy | 164 | Mostly missing annotations in test files; tracked separately |
| vulture | 99 (60% conf) / 31 (100% conf) | Triage weekly; never auto-delete |
| pytest | 1191 pass, 1 skip | |

---

## Bandit — security linter

### What it catches

- `B324`: weak hash (SHA1/MD5) used for security. Filename digests need `# nosec` with comment.
- `B310`: `urllib.request.urlopen` (can handle file:// schemes). Local llama-server calls need `# nosec`.
- `B104`: `app.run(host="0.0.0.0", ...)`. Acceptable for LAN listeners if documented + auth-gated.
- `B108`: hardcoded `/tmp/...` paths. Common in test fixtures; excluded via `exclude_dirs`.
- `B602/B603/B607`: subprocess shell=True. Hard error if seen.

### Configuration (`pyproject.toml` → `[tool.bandit]`)

```toml
skips = ["B101", "B311", "B404", "B603", "B607", "B105", "B106"]
exclude_dirs = ["data", "logs", ".venv", "tests/sandbox", "tests"]
```

### When to add `# nosec`

**Only when the rule doesn't apply to your context, with a comment explaining why.** Example patterns:

```python
# Good: SHA1 used for filename uniqueness, NOT auth/security
digest = hashlib.sha1(  # nosec B324 — non-cryptographic digest
    raw_line.encode("utf-8", errors="replace")
).hexdigest()[:10]

# Good: URL is hardcoded local endpoint
with urllib.request.urlopen(req, timeout=_timeout) as resp:  # nosec B310

# Good: Listener binds 0.0.0.0 for LAN camera access; auth mitigation pending
app.run(host="0.0.0.0", port=8090)  # nosec B104
```

**Bad:** `# nosec B303` with no comment. Future readers can't tell if the suppression is correct.

### When to fix the underlying issue

- `subprocess` with `shell=True` → change to `shell=False` + argv list
- `pickle.load()` on untrusted data → use `json.loads()` or restrict source
- `yaml.load()` without `Loader=SafeLoader` → use `yaml.safe_load()`
- Hardcoded password in test fixture → use `monkeypatch.setenv()`

---

## mypy — static type checker

### Why we run it (not strict mode)

The repo has partial type coverage (listener.py ~62%, infra/quick_classifier.py ~12%). Running `mypy --strict` would produce thousands of errors. Instead we use **pragmatic mode**:

- `check_untyped_defs = true` — type-check function bodies even when the function itself is untyped. **This is the killer feature** — it catches `AttributeError` on dataclass fields, wrong arg types, missing return values. The 2026-08-26 `ctx.legacy_capture_avoided` bug would have been caught by this.
- `warn_unused_ignores = true` — flags dead `# type: ignore` comments
- `warn_return_any = true` — warns when a function returns `Any`
- `ignore_missing_imports = true` — third-party libs without stubs (insightface, onnxruntime, PyAV) are allowed
- `follow_imports = "silent"` — don't follow third-party imports deeply

### Why `explicit-package-bases` is needed in CLI

The repo root has `__init__.py` for pytest but contains hyphens (`ai_camera_monitor`), which mypy rejects as a package name. Solution: pass `--explicit-package-bases` and run on subdirectories. The `[tool.mypy]` block in `pyproject.toml` configures everything else.

**Don't put `[tool.mypy] files = ...` in pyproject.toml** — mypy reads `files` relative to the package root, and the hyphen breaks it. CLI invocation is the source of truth.

### Triage workflow

When mypy reports 100+ errors after a refactor, the default workflow
is to categorize and fix in batches:

1. **Real bugs** (`[attr-defined]`, `[name-defined]`, `[union-attr]` on production code) — fix immediately
2. **Missing annotations** (`[var-annotated]`, `[no-any-return]`) — fix in the file you touched, defer the rest
3. **Test-fixture mismatches** (`[assignment]` in tests/* where the test stub has slightly wrong types) — fix the stub or add `# type: ignore[assignment]`
4. **Lazy-loaded cross-repo imports** (Phase 6A's `face_recognition`/`property_state`/`response_engine`) — add `# type: ignore[import-not-found]` at the import line, with comment

### ⚠️ When the user asks for full cleanup — fix ALL of them, don't defer

Note 2026-08-26 (Phase.123): *"I want you to fix all of them.
If they're trivial, then they're quick to fix, if they're structural
and important, and then they should also be fixed."*

When the user explicitly asks for a cleanup pass, the default triage
above (which prioritizes quick wins and defers cosmetic stuff) does
NOT apply. Apply the same diligence to trivial errors (unused-ignore,
func-returns-value, var-annotated) as to structural ones (no-any-return,
assignment unions in production code). The reasoning: cosmetic
mypy errors that pile up across the codebase create a "low-grade
fever" — future contributors stop trusting the type checker, and
real bugs hide among the noise. Getting to 0 (or as close as is
honest) is worth the extra hour.

The technical-budget ceiling to flag to Note before starting: if the
fix would require new TypedDict definitions for every JSON-loaded
dict, or require touching listener.py (which means restarting the
listener and waiting for live verification), state the trade-off
before doing 4 hours of work. Otherwise, just do it.

### When to add `# type: ignore`

```python
# Good: cv2.dnn.NMSBoxes returns np.ndarray OR sequence-of-lists
# depending on OpenCV version; flatten covers both shapes.
indices = np.array(indices).flatten()  # type: ignore[attr-defined]

# Bad: blanket suppression
x = some_func()  # type: ignore
```

**Always include the error code in brackets** (`#[attr-defined]`, not just `# type: ignore`). mypy's `warn_unused_ignores` will tell you when the suppression is no longer needed.

### When to fix the underlying type

- `[assignment]` from a test stub → make the stub match the production type signature
- `[no-any-return]` from a stub function → annotate the return type properly
- `[index]` on a `list[Any]` → narrow the list type with a real annotation

---

## Vulture — dead-code detector

### What it catches

- Unused functions (60% confidence by default)
- Unused variables (100% confidence is rare — usually means a leftover from a refactor)
- Unused attributes on classes (mostly from test fixtures)
- Unused imports (90% confidence)

### The triage workflow

**Never auto-delete.** Every finding is a hypothesis that needs verification. Some patterns look dead but aren't:

1. **Flask routes**: `@app.route("/foo")` on a function that has no Python callers. vulture doesn't see the route decorator → false positive.
2. **pytest fixtures**: functions decorated with `@pytest.fixture` are injected via fixture name. vulture doesn't see this.
3. **`__init__.py` exports**: `from .x import y` exposes `y` as a public API. vulture doesn't know the package is the unit.
4. **ABC abstract methods**: declared but only called by the subclass.
5. **Module-level `__all__` exports**: explicit re-exports.

### How to investigate a finding

```bash
# Did anyone import this from anywhere?
grep -rn "function_name" --include="*.py" | grep -v "_archive"

# Is it in __all__?
grep "function_name" infra/__init__.py

# Is it decorated with @app.route or @pytest.fixture?
grep -B2 "def function_name" file.py
```

If none of the above: it's likely dead code from a prior refactor. Two options:

**A. Delete it.** Safe if:
- The function isn't in `__all__` anywhere
- No test file imports it
- It's not a Flask route or pytest fixture
- The git log shows it was added then never called

**B. Whitelist with `# noqa: ARG` style comment.** Use vulture's `--ignore-names` flag for cases where the function is called dynamically (e.g. via `getattr` or `globals()` lookup).

### Suppression patterns

```toml
# pyproject.toml → [tool.vulture]
ignore_decorators = [
    "pytest.fixture",
    "app.route",
    "app.errorhandler",
]
```

Note: `--ignore-decorators` only suppresses unused-attribute findings, not unused-function. For functions, add to `ignore_names` or use a vulture whitelist file.

### Weekly cadence

Don't run vulture on every commit. Run it weekly (cron-style) and triage findings as a 30-min batched task. Real findings are usually:

- Leftover functions from prior-session refactors that got bundled into a commit
- Test fixtures that were renamed but the assignments didn't follow
- Module-private helpers that became unused after a split

---

## The "build for the system" loop

When a quality gate fails, follow the investigation-then-fix pattern:

1. **Run the tool, capture the output**
2. **Categorize each finding** (real bug / missing annotation / false positive / dead code)
3. **Fix real bugs immediately** — these are bugs in production code that the gate caught
4. **Add `# nosec` / `# type: ignore` for false positives** — with comments explaining why
5. **Defer the rest** — don't try to make the count zero in one commit. Track in PLAN.md open questions.

The `ctx.legacy_capture_avoided` bug from 2026-08-26 is a good example: mypy would have caught it. The bandit findings from 6B.119 launch included a real `urllib.request.urlopen` on local llama-server (false positive, but the suppression comment documents WHY it's safe). The vulture 100% findings in `infra/tests/test_camera_audio.py` are leftover test fixtures from a prior refactor.

---

## Common pitfalls

### "Bandit says I'm using weak crypto, but I'm just hashing for a filename"

Use `usedforsecurity=False`:

```python
# Old:
digest = hashlib.sha1(data).hexdigest()[:10]

# New:
digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()[:10]
```

If the hashlib version on your Python doesn't support `usedforsecurity`, use `# nosec B324` with a comment.

### "mypy says my function returns Any"

Three options, in order of preference:

1. **Add a real return type annotation.** If the function is genuinely untyped (uses `dict`, `list`), use `dict[str, Any]` or `Any` deliberately.
2. **Cast the value to the declared type.** For `json.load()` / `json.loads()` / `r.json()` / `cv2.threshold` returns:
   ```python
   parsed: dict[str, Any] = json.loads(text)
   return parsed
   # or:
   return cast(dict[str, Any], json.load(f))
   ```
   The runtime tests already verify the shape; the cast is honest about what we have.
3. **Mark it as a stub:** `# type: ignore[no-any-return]` if the function is a thin wrapper around an untyped third-party API.

TypedDict is the ideal answer (defines the exact shape) but it's a 30-minute
sprint per callsite; the cast approach gets you 90% of the safety in 30 seconds.

### "mypy accepts my cast but it crashes at runtime with NameError"

If you're using `cast(SomeType, ...)` and `SomeType` is only imported under
`if TYPE_CHECKING:`, the type name is in scope for mypy but **not at runtime** —
`cast()` evaluates its first argument when called, which raises `NameError`.

Fix: use a string forward-ref:
```python
return cast("GateVerdict | None", run_gate(...))  # string — not evaluated
```

Or: bind the runtime import to an alias and use `_GateVerdict(...)` instead of
trying to also cast it:
```python
from motion_gate_pipeline import GateVerdict as _GateVerdict
return _GateVerdict(...)  # type: ignore[no-any-return]
```

The first form is cleaner; the second is necessary if you also need mypy to
see the return value's type (a plain `cast("X", ...)` returns `Any`, while
the runtime call returns a typed object).

### "vulture says my Flask route is dead"

It's not. vulture can't see Flask's `app.route` registration. Add it to `ignore_decorators` in `pyproject.toml`.

### "I fixed all 31 vulture 100% findings and tests broke"

That means the function wasn't actually dead — it was called via dynamic dispatch (e.g. `getattr(module, function_name)(...)`, registry pattern, or ABC dispatch). Restore it and add a comment explaining the dynamic call.

### ⚠️ Pitfall: bulk regex sweeps of test annotations

When fixing ~15 `[var-annotated]` errors in one pass, it's tempting
to do something like:
```python
content = re.sub(r'^(\s+)(v)(\s*)=\s*\[', rf'\1\2: list[Any]\3= [', content)
```

DON'T — without auditing each replacement, you'll ship wrong types:

| Original | Wrong bulk fix | Right fix |
|---|---|---|
| `empty = {}` (test) | `empty: list[Any] = {}` | `empty: dict[str, Any] = {}` |
| `face_recognition = {...}` (dict literal) | `face_recognition: list[Any] = {...}` | `face_recognition: dict[str, Any] = {...}` |
| `v = {"color": "white", ...}` (dict literal) | `v: list[Any] = {"color": ...}` | `v: dict[str, Any] = {...}` |

The right pattern when sweeping test annotations:

1. **Bulk-fix only one specific shape per pass** — e.g. only `result = {[]}`-style initializations, where the type IS list.
2. **Run mypy after each pass and grep for `[assignment]` errors** — those will flag the wrong bulk replacements immediately.
3. **For ambiguous cases** (could be `list`, `dict`, or `tuple`), hand-fix with `read_file` to confirm the actual structure first.

The 2026-08-26 6B.123 session shipped ~5 wrong `list[Any] = {...}` replacements because of an over-broad regex. Each one had to be fixed individually in a follow-up sweep. The cost: +5 minutes of round-trips that wouldn't have existed with a per-file hand-fix on a 15-error baseline.

---

## Audit trail

- **Phase.119 (2026-08-26):** toolchain added. bandit=0, mypy=164 (baseline), vulture=99/31 (baseline). 3 real bugs caught and fixed (`_cached_sun`, `Sequence[int].flatten`, `app.run` nosec). 0 nosec suppressions added (3 total — all documented).
- **Phase.120 (2026-08-26):** mypy cleanup. 164 → 88 errors (47%). Patterns: JSON-loaded dict constants typed `dict[str, Any]`; `lift_crop_to_alert_schema` widened to `Any → Any`; `extract_signature crop_paths` widened to `Sequence[str | Path]`; lazy-loaded Phase6A modules `# type: ignore[name-defined]`; `assert result is not None` for unreachable None returns.
- **Phase.121 (2026-08-26):** vulture cleanup pass 1. 31 100%-conf findings → 0. Patterns: pytest fixtures that are only used as `monkeypatch.setattr` setters moved to `@pytest.fixture(autouse=True)`; dead `pass_def` parameters renamed `_pass_def` to preserve stable symbols (vulture skill mandates don't break introspection).
- **Phase.122 (2026-08-26):** pre-commit hook (`.pre-commit-config.yaml`). Three local hooks: ruff, bandit, vulture. mypy + pytest deliberately excluded (too slow for every commit).
- **Phase.123 (2026-08-26):** mypy cleanup round 2 — Note: *"fix all of them"*. 88 → 0 errors. Patterns: `(list.append(x), True)[1]` lambdas replaced with `def _track_call()` helper; `json.load()`/`json.loads()` annotated as `dict[str, Any]`; `cv2.threshold`/`cv2.bitwise_or` results cast via local variable annotations; `sys.modules[...] = None` test injections got `# type: ignore[assignment]`; dynamic-import `_GateVerdict` casts use string forward-refs to avoid runtime `NameError`; `extract_signature`/`is_empty_signature` widened from `dict[str, Any]` to `object` (the type was lying — runtime already tolerated None/str/list).

**Final state (2026-08-26 23:59 EDT):** ruff 0, bandit 0, mypy **0** (161 files), vulture 0/99, pytest 1191/1 skip, listener PID 66820 healthy.

- **Future phases:**
  - 6B.124: weekly vulture triage pass (target: 0 60%-conf findings → promote vulture to blocking gate)
  - 6B.125: pip-audit + interrogate (already mentioned in the original quality-toolchain proposal)
  - mypy pre-commit hook — only after we're confident the 0-error baseline holds for at least a week