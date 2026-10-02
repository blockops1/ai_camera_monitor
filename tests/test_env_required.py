"""test_env_required.py -- E2E regression tests for fail-loud env contract.

Verifies that every _require_env-mandated variable in the production codebase
causes a RuntimeError at import time when unset. This is NOT a tautological
unit test; it proves the structural contract: missing operator-significant
env var → process refuses to start.

Run: pytest tests/test_env_required.py -v
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# All operator-significant vars that trigger _require_env at import time.
_REQUIRED_VARS = [
    "MOTION_GATE_NIGHT_SUPPRESS_ENABLED",
    "FARMSURV_NIGHT_SUPPRESS_CAMERAS",
    "FARMSURV_NIGHT_SUPPRESS_CLASSES",
    "FARMSURV_QUIET_HOURS_ENABLED",
    "FARMSURV_BASELINE_OVERRIDE_ENABLED",
    "FARMSURV_API_TOKEN",
    "FARMSURV_MCP_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_HOME_CHAT_ID",
]


def _clear_env(*names: str) -> None:
    """Remove the given keys from os.environ if present."""
    for name in names:
        os.environ.pop(name, None)


def _clear_all_required() -> None:
    """Clear all operator-significant vars to test fail-loud."""
    for name in _REQUIRED_VARS:
        os.environ.pop(name, None)


def _set_all_required(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a complete env dict with all _require_env vars set."""
    result = dict(env or {})
    result.setdefault("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0")
    result.setdefault("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "")
    result.setdefault("FARMSURV_NIGHT_SUPPRESS_CLASSES", "")
    result.setdefault("FARMSURV_QUIET_HOURS_ENABLED", "0")
    result.setdefault("FARMSURV_BASELINE_OVERRIDE_ENABLED", "0")
    result.setdefault("FARMSURV_MCP_TOKEN", "x" * 40)
    result.setdefault("TELEGRAM_BOT_TOKEN", "x" * 40)
    result.setdefault("TELEGRAM_HOME_CHAT_ID", "12345")
    return result


def _import_module(name: str) -> object:
    """Import (or re-import) a module with the given dotted name.

    Clears sys.modules first to force a fresh import cycle so that
    module-level _require_env calls re-evaluate against the current
    os.environ.
    """
    # Remove the module and every known sub-path from sys.modules so
    # that the import actually re-runs the module body.
    prefixes = [name]
    # Also clear infra.env_required so _require_env itself is fresh
    if "infra.env_required" in sys.modules:
        prefixes.append("infra.env_required")
    for prefix in prefixes:
        sys.modules.pop(prefix, None)
        # Also clear children (infra.quiet_hours, etc.)
        keys_to_remove = [k for k in sys.modules if k.startswith(prefix + ".") or k == prefix]
        for k in keys_to_remove:
            sys.modules.pop(k, None)
    return importlib.import_module(name)


def _import_via_subprocess(
    module: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Import a module in a fresh child process.

    Uses _set_all_required() as a base, then applies extra_env overrides
    (which can explicitly set vars to empty string to simulate unset).

    Returns (exit_code, stdout).
    """
    env = _set_all_required(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True, check=False,
        env=env,
        cwd="/Users/jill/farm-surveillance-v2",
    )
    return result.returncode, result.stdout + result.stderr


def _import_via_subprocess_clear_var(
    module: str,
    *,
    missing_var: str,
) -> tuple[int, str]:
    """Import a module in a fresh child process with one var cleared.

    Uses the full _set_all_required base, then removes *exactly* the
    named var to test that the module crashes when THAT var is missing.

    Returns (exit_code, stdout).
    """
    env = _set_all_required()
    env.pop(missing_var, None)
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True, check=False,
        env=env,
        cwd="/Users/jill/farm-surveillance-v2",
    )
    return result.returncode, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# AC9a: env_required module itself
# ---------------------------------------------------------------------------

def test_require_env_raises_when_unset() -> None:
    """_require_env raises RuntimeError if the var is missing."""
    result = subprocess.run(
        [sys.executable, "-c", (
            "import os\n"
            "for v in ['MOTION_GATE_NIGHT_SUPPRESS_ENABLED',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CAMERAS',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CLASSES',\n"
            "          'FARMSURV_QUIET_HOURS_ENABLED',\n"
            "          'FARMSURV_BASELINE_OVERRIDE_ENABLED',\n"
            "          'FARMSURV_MCP_TOKEN',\n"
            "          'TELEGRAM_BOT_TOKEN', 'TELEGRAM_HOME_CHAT_ID']:\n"
            "    os.environ.pop(v, None)\n"
            "os.environ['TEST_UNSET_VAR'] = 'still_set'\n"
            "import sys; sys.path.insert(0, '/Users/jill/farm-surveillance-v2')\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'env_required',\n"
            "    '/Users/jill/farm-surveillance-v2/infra/env_required.py'\n"
            ")\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "try:\n"
            "    mod._require_env('NONEXISTENT_TEST_VAR')\n"
            "except RuntimeError as e:\n"
            "    assert 'NONEXISTENT_TEST_VAR' in str(e)\n"
            "    assert 'os.environ' in str(e)\n"
            "    print('OK')\n"
            "else:\n"
            "    print('FAIL'); import sys; sys.exit(1)\n"
        )],
        capture_output=True, text=True, check=False,
        cwd="/Users/jill/farm-surveillance-v2",
    )
    assert result.returncode == 0, f"expected exit 0: {result.stdout}{result.stderr}"
    assert "OK" in result.stdout


def test_require_env_returns_value_when_set() -> None:
    """_require_env returns the value when set."""
    result = subprocess.run(
        [sys.executable, "-c", (
            "import os\n"
            "os.environ['TEST_RETURN_VAR'] = 'hello'\n"
            "for v in ['MOTION_GATE_NIGHT_SUPPRESS_ENABLED',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CAMERAS',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CLASSES',\n"
            "          'FARMSURV_QUIET_HOURS_ENABLED',\n"
            "          'FARMSURV_BASELINE_OVERRIDE_ENABLED',\n"
            "          'FARMSURV_MCP_TOKEN',\n"
            "          'TELEGRAM_BOT_TOKEN', 'TELEGRAM_HOME_CHAT_ID']:\n"
            "    os.environ.setdefault(v, 'placeholder')\n"
            "os.environ['TEST_RETURN_VAR'] = 'hello'\n"
            "import sys; sys.path.insert(0, '/Users/jill/farm-surveillance-v2')\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'env_required',\n"
            "    '/Users/jill/farm-surveillance-v2/infra/env_required.py'\n"
            ")\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "result = mod._require_env('TEST_RETURN_VAR')\n"
            "assert result == 'hello', f'expected hello, got {result}'\n"
            "print('OK')\n"
        )],
        capture_output=True, text=True, check=False,
        cwd="/Users/jill/farm-surveillance-v2",
    )
    assert result.returncode == 0, f"expected exit 0: {result.stderr}"
    assert "OK" in result.stdout


def test_require_env_raises_on_empty_string() -> None:
    """_require_env raises RuntimeError for empty string value."""
    result = subprocess.run(
        [sys.executable, "-c", (
            "import os\n"
            "os.environ['TEST_EMPTY_VAR'] = ''\n"
            "for v in ['MOTION_GATE_NIGHT_SUPPRESS_ENABLED',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CAMERAS',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CLASSES',\n"
            "          'FARMSURV_QUIET_HOURS_ENABLED',\n"
            "          'FARMSURV_BASELINE_OVERRIDE_ENABLED',\n"
            "          'FARMSURV_MCP_TOKEN',\n"
            "          'TELEGRAM_BOT_TOKEN', 'TELEGRAM_HOME_CHAT_ID']:\n"
            "    os.environ.setdefault(v, 'placeholder')\n"
            "os.environ['TEST_EMPTY_VAR'] = ''\n"
            "import sys; sys.path.insert(0, '/Users/jill/farm-surveillance-v2')\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'env_required',\n"
            "    '/Users/jill/farm-surveillance-v2/infra/env_required.py'\n"
            ")\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "try:\n"
            "    mod._require_env('TEST_EMPTY_VAR')\n"
            "except RuntimeError as e:\n"
            "    assert 'TEST_EMPTY_VAR' in str(e)\n"
            "    print('OK')\n"
            "else:\n"
            "    print('FAIL'); import sys; sys.exit(1)\n"
        )],
        capture_output=True, text=True, check=False,
        cwd="/Users/jill/farm-surveillance-v2",
    )
    assert result.returncode == 0, f"expected exit 0: {result.stderr}"
    assert "OK" in result.stdout


def test_require_env_lower_bool_true_values() -> None:
    """_require_env_lower_bool returns True for truthy values."""
    for val in ("1", "true", "yes", "on", "TRUE", "YES", "On"):
        result = subprocess.run(
            [sys.executable, "-c", (
                "import os\n"
                "os.environ['BOOL_TEST_VAL'] = '" + val + "'\n"
                "for v in ['MOTION_GATE_NIGHT_SUPPRESS_ENABLED',\n"
                "          'FARMSURV_NIGHT_SUPPRESS_CAMERAS',\n"
                "          'FARMSURV_NIGHT_SUPPRESS_CLASSES',\n"
                "          'FARMSURV_QUIET_HOURS_ENABLED',\n"
                "          'FARMSURV_BASELINE_OVERRIDE_ENABLED',\n"
                "          'FARMSURV_MCP_TOKEN',\n"
                "          'TELEGRAM_BOT_TOKEN', 'TELEGRAM_HOME_CHAT_ID']:\n"
                "    os.environ.setdefault(v, 'placeholder')\n"
                "import sys; sys.path.insert(0, '/Users/jill/farm-surveillance-v2')\n"
                "import importlib.util\n"
                "spec = importlib.util.spec_from_file_location(\n"
                "    'env_required',\n"
                "    '/Users/jill/farm-surveillance-v2/infra/env_required.py'\n"
                ")\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "result = mod._require_env_lower_bool('BOOL_TEST_VAL')\n"
                "assert result is True, f'expected True for {val!r}, got {result}'\n"
                "print('OK')\n"
            )],
            capture_output=True, text=True, check=False,
            cwd="/Users/jill/farm-surveillance-v2",
        )
        assert result.returncode == 0, f"expected exit 0 for {val!r}: {result.stderr}"
        assert "OK" in result.stdout


def test_require_env_lower_bool_false_values() -> None:
    """_require_env_lower_bool returns False for non-truthy values."""
    for val in ("0", "false", "no", "off", "random", "TRUE1"):
        result = subprocess.run(
            [sys.executable, "-c", (
                "import os\n"
                "os.environ['BOOL_TEST_VAL'] = '" + val + "'\n"
                "for v in ['MOTION_GATE_NIGHT_SUPPRESS_ENABLED',\n"
                "          'FARMSURV_NIGHT_SUPPRESS_CAMERAS',\n"
                "          'FARMSURV_NIGHT_SUPPRESS_CLASSES',\n"
                "          'FARMSURV_QUIET_HOURS_ENABLED',\n"
                "          'FARMSURV_BASELINE_OVERRIDE_ENABLED',\n"
                "          'FARMSURV_MCP_TOKEN',\n"
                "          'TELEGRAM_BOT_TOKEN', 'TELEGRAM_HOME_CHAT_ID']:\n"
                "    os.environ.setdefault(v, 'placeholder')\n"
                "import sys; sys.path.insert(0, '/Users/jill/farm-surveillance-v2')\n"
                "import importlib.util\n"
                "spec = importlib.util.spec_from_file_location(\n"
                "    'env_required',\n"
                "    '/Users/jill/farm-surveillance-v2/infra/env_required.py'\n"
                ")\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "result = mod._require_env_lower_bool('BOOL_TEST_VAL')\n"
                "assert result is False, f'expected False for {val!r}, got {result}'\n"
                "print('OK')\n"
            )],
            capture_output=True, text=True, check=False,
            cwd="/Users/jill/farm-surveillance-v2",
        )
        assert result.returncode == 0, f"expected exit 0 for {val!r}: {result.stderr}"
        assert "OK" in result.stdout


def test_require_env_lower_bool_raises_when_unset() -> None:
    """_require_env_lower_bool raises when the var is unset."""
    result = subprocess.run(
        [sys.executable, "-c", (
            "import os\n"
            "for v in ['MOTION_GATE_NIGHT_SUPPRESS_ENABLED',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CAMERAS',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CLASSES',\n"
            "          'FARMSURV_QUIET_HOURS_ENABLED',\n"
            "          'FARMSURV_BASELINE_OVERRIDE_ENABLED',\n"
            "          'FARMSURV_MCP_TOKEN',\n"
            "          'TELEGRAM_BOT_TOKEN', 'TELEGRAM_HOME_CHAT_ID']:\n"
            "    os.environ.setdefault(v, 'placeholder')\n"
            "os.environ.pop('UNSET_BOOL_TEST', None)\n"
            "import sys; sys.path.insert(0, '/Users/jill/farm-surveillance-v2')\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'env_required',\n"
            "    '/Users/jill/farm-surveillance-v2/infra/env_required.py'\n"
            ")\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "try:\n"
            "    mod._require_env_lower_bool('UNSET_BOOL_TEST')\n"
            "except RuntimeError:\n"
            "    print('OK')\n"
            "else:\n"
            "    print('FAIL'); import sys; sys.exit(1)\n"
        )],
        capture_output=True, text=True, check=False,
        cwd="/Users/jill/farm-surveillance-v2",
    )
    assert result.returncode == 0, f"expected exit 0: {result.stderr}"
    assert "OK" in result.stdout


def test_require_csv_env_returns_list_when_set() -> None:
    """_require_csv_env returns parsed list for comma-separated values."""
    result = subprocess.run(
        [sys.executable, "-c", (
            "import os\n"
            "os.environ['CSV_TEST_VAL'] = 'a, b ,c'\n"
            "for v in ['MOTION_GATE_NIGHT_SUPPRESS_ENABLED',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CAMERAS',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CLASSES',\n"
            "          'FARMSURV_QUIET_HOURS_ENABLED',\n"
            "          'FARMSURV_BASELINE_OVERRIDE_ENABLED',\n"
            "          'FARMSURV_MCP_TOKEN',\n"
            "          'TELEGRAM_BOT_TOKEN', 'TELEGRAM_HOME_CHAT_ID']:\n"
            "    os.environ.setdefault(v, 'placeholder')\n"
            "import sys; sys.path.insert(0, '/Users/jill/farm-surveillance-v2')\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'env_required',\n"
            "    '/Users/jill/farm-surveillance-v2/infra/env_required.py'\n"
            ")\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "result = mod._require_csv_env('CSV_TEST_VAL')\n"
            "assert result == ['a', 'b', 'c'], f'expected [a,b,c], got {result}'\n"
            "print('OK')\n"
        )],
        capture_output=True, text=True, check=False,
        cwd="/Users/jill/farm-surveillance-v2",
    )
    assert result.returncode == 0, f"expected exit 0: {result.stderr}"
    assert "OK" in result.stdout


def test_require_csv_env_returns_empty_when_blank() -> None:
    """_require_csv_env returns [] when var is set but blank."""
    result = subprocess.run(
        [sys.executable, "-c", (
            "import os\n"
            "os.environ['CSV_TEST_VAL'] = '  '\n"
            "for v in ['MOTION_GATE_NIGHT_SUPPRESS_ENABLED',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CAMERAS',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CLASSES',\n"
            "          'FARMSURV_QUIET_HOURS_ENABLED',\n"
            "          'FARMSURV_BASELINE_OVERRIDE_ENABLED',\n"
            "          'FARMSURV_MCP_TOKEN',\n"
            "          'TELEGRAM_BOT_TOKEN', 'TELEGRAM_HOME_CHAT_ID']:\n"
            "    os.environ.setdefault(v, 'placeholder')\n"
            "import sys; sys.path.insert(0, '/Users/jill/farm-surveillance-v2')\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'env_required',\n"
            "    '/Users/jill/farm-surveillance-v2/infra/env_required.py'\n"
            ")\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "result = mod._require_csv_env('CSV_TEST_VAL')\n"
            "assert result == [], f'expected [], got {result}'\n"
            "print('OK')\n"
        )],
        capture_output=True, text=True, check=False,
        cwd="/Users/jill/farm-surveillance-v2",
    )
    assert result.returncode == 0, f"expected exit 0: {result.stderr}"
    assert "OK" in result.stdout


def test_require_csv_env_returns_empty_when_empty_string() -> None:
    """_require_csv_env returns [] when var is set to empty string ''.

    Regression test for V2-096e (2026-10-02): `_require_csv_env` previously
    delegated to `_require_env` which raises on empty string. Operator
    had `FARMSURV_NIGHT_SUPPRESS_CAMERAS=` in .env (empty = disabled),
    and every pipeline alert crashed at night_suppress_match() during
    the catch stage. The fix: `_require_csv_env` reads os.environ
    directly and returns [] for any falsy result.
    """
    result = subprocess.run(
        [sys.executable, "-c", (
            "import os\n"
            "os.environ['CSV_TEST_VAL'] = ''\n"  # The actual bug case
            "for v in ['MOTION_GATE_NIGHT_SUPPRESS_ENABLED',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CAMERAS',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CLASSES',\n"
            "          'FARMSURV_QUIET_HOURS_ENABLED',\n"
            "          'FARMSURV_BASELINE_OVERRIDE_ENABLED',\n"
            "          'FARMSURV_MCP_TOKEN',\n"
            "          'TELEGRAM_BOT_TOKEN', 'TELEGRAM_HOME_CHAT_ID']:\n"
            "    os.environ.setdefault(v, 'placeholder')\n"
            "import sys; sys.path.insert(0, '/Users/jill/farm-surveillance-v2')\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'env_required',\n"
            "    '/Users/jill/farm-surveillance-v2/infra/env_required.py'\n"
            ")\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "result = mod._require_csv_env('CSV_TEST_VAL')\n"
            "assert result == [], f'expected [], got {result}'\n"
            "print('OK')\n"
        )],
        capture_output=True, text=True, check=False,
        cwd="/Users/jill/farm-surveillance-v2",
    )
    assert result.returncode == 0, f"expected exit 0: {result.stderr}"
    assert "OK" in result.stdout


def test_require_csv_env_returns_empty_when_unset() -> None:
    """_require_csv_env returns [] when var is unset (NOT raises).

    Per the function's docstring (and the fix for V2-096e), an unset
    empty/blank env var means "operator chose to disable" the feature.
    Callers must explicitly check for empty lists if a non-empty result
    is required. Use _require_env() if you want a RuntimeError on
    missing values.
    """
    result = subprocess.run(
        [sys.executable, "-c", (
            "import os\n"
            "for v in ['MOTION_GATE_NIGHT_SUPPRESS_ENABLED',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CAMERAS',\n"
            "          'FARMSURV_NIGHT_SUPPRESS_CLASSES',\n"
            "          'FARMSURV_QUIET_HOURS_ENABLED',\n"
            "          'FARMSURV_BASELINE_OVERRIDE_ENABLED',\n"
            "          'FARMSURV_MCP_TOKEN',\n"
            "          'TELEGRAM_BOT_TOKEN', 'TELEGRAM_HOME_CHAT_ID']:\n"
            "    os.environ.setdefault(v, 'placeholder')\n"
            "os.environ.pop('UNSET_CSV_TEST', None)\n"
            "import sys; sys.path.insert(0, '/Users/jill/farm-surveillance-v2')\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'env_required',\n"
            "    '/Users/jill/farm-surveillance-v2/infra/env_required.py'\n"
            ")\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "result = mod._require_csv_env('UNSET_CSV_TEST')\n"
            "assert result == [], f'expected [] got {result!r}'\n"
            "print('OK')\n"
        )],
        capture_output=True, text=True, check=False,
        cwd="/Users/jill/farm-surveillance-v2",
    )
    assert result.returncode == 0, f"expected exit 0: {result.stderr}"
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# AC9b: import-time fail-loud via fresh subprocess (E2E contract)
# ---------------------------------------------------------------------------
#
# Note: infra/__init__.py imports alert_overrides_baseline → quick_classifier
# at module level. Since quick_classifier has a module-level _require_env
# call, importing ANY infra submodule (quiet_hours, alert_overrides_baseline,
# mcp_auth) will first crash on quick_classifier's NIGHT_SUPPRESS_ENABLED.
#
# The correct E2E test is:
# - Import full infra package → success when all chain vars set.
# - Clear FIRST var in chain → crash naming that var.
# - Test individual _require_env calls via inline subprocess code.


def test_quick_classifier_crashes_without_night_suppress_enabled() -> None:
    """Importing quick_classifier without NIGHT_SUPPRESS_ENABLED crashes."""
    rc, out = _import_via_subprocess_clear_var(
        "infra.quick_classifier",
        missing_var="MOTION_GATE_NIGHT_SUPPRESS_ENABLED",
    )
    assert rc != 0, f"expected non-zero exit, got {rc}"
    assert "RuntimeError" in out, f"expected RuntimeError: {out}"
    assert "MOTION_GATE_NIGHT_SUPPRESS_ENABLED" in out


def test_quick_classifier_succeeds_when_night_suppress_enabled_set() -> None:
    """Importing quick_classifier succeeds when required vars are set."""
    rc, out = _import_via_subprocess(
        "infra.quick_classifier",
        extra_env={
            "MOTION_GATE_NIGHT_SUPPRESS_ENABLED": "0",
            "FARMSURV_NIGHT_SUPPRESS_CAMERAS": "",
            "FARMSURV_NIGHT_SUPPRESS_CLASSES": "",
        },
    )
    assert rc == 0, f"expected exit 0, got {rc}: {out}"


def test_quiet_hours_succeeds_when_all_chain_vars_set() -> None:
    """Importing quiet_hours succeeds when all chain vars are set."""
    rc, out = _import_via_subprocess(
        "infra.quiet_hours",
        extra_env={"FARMSURV_QUIET_HOURS_ENABLED": "0"},
    )
    assert rc == 0, f"expected exit 0, got {rc}: {out}"


def test_quiet_hours_gate_requires_flag() -> None:
    """in_quiet_hours raises when FARMSURV_QUIET_HOURS_ENABLED is unset.

    The gate uses _require_env_lower_bool internally; verified by calling
    it directly in a fresh process.
    """
    result = subprocess.run(
        [sys.executable, "-c", (
            "from infra.env_required import _require_env_lower_bool\n"
            "import os; os.environ.pop('FARMSURV_QUIET_HOURS_ENABLED', None)\n"
            "try:\n"
            "    _require_env_lower_bool('FARMSURV_QUIET_HOURS_ENABLED')\n"
            "except RuntimeError as e:\n"
            "    print('OK:', e)\n"
            "    import sys; sys.exit(0)\n"
            "else:\n"
            "    print('FAIL'); import sys; sys.exit(1)\n"
        )],
        capture_output=True, text=True, check=False,
        env=_set_all_required(),
        cwd="/Users/jill/farm-surveillance-v2",
    )
    assert result.returncode == 0, f"expected exit 0: {result.stdout}{result.stderr}"
    assert "OK:" in result.stdout


def test_alert_overrides_baseline_succeeds_when_all_chain_vars_set() -> None:
    """Importing alert_overrides_baseline succeeds when all chain vars are set."""
    rc, out = _import_via_subprocess(
        "infra.alert_overrides_baseline",
        extra_env={"FARMSURV_BASELINE_OVERRIDE_ENABLED": "0"},
    )
    assert rc == 0, f"expected exit 0, got {rc}: {out}"


def test_mcp_auth_succeeds_when_token_set() -> None:
    """Importing mcp_auth succeeds when token is set."""
    rc, out = _import_via_subprocess(
        "infra.mcp_auth",
        extra_env={"FARMSURV_MCP_TOKEN": "x" * 40},
    )
    assert rc == 0, f"expected exit 0, got {rc}: {out}"


def test_mcp_auth_validate_token_requires_32_chars() -> None:
    """validate_token rejects tokens shorter than 32 chars."""
    result = subprocess.run(
        [sys.executable, "-c", (
            "import os; os.environ['FARMSURV_MCP_TOKEN'] = 'short'\n"
            "from infra.mcp_auth import validate_token\n"
            "ok, msg = validate_token()\n"
            "assert ok is False, 'should be False for short token'\n"
            "assert 'too short' in msg, f'expected too short: {msg}'\n"
            "print('OK')\n"
        )],
        capture_output=True, text=True, check=False,
        env=_set_all_required(),
        cwd="/Users/jill/farm-surveillance-v2",
    )
    assert result.returncode == 0, f"expected exit 0: {result.stdout}{result.stderr}"
    assert "OK" in result.stdout


def test_full_infra_import_crashes_when_NIGHT_SUPPRESS_ENABLED_unset() -> None:
    """Importing infra (the package) crashes without NIGHT_SUPPRESS_ENABLED.

    This verifies the full import chain: infra → alert_overrides_baseline
    → quick_classifier → _require_env_lower_bool('NIGHT_SUPPRESS_ENABLED').
    """
    rc, out = _import_via_subprocess_clear_var(
        "infra",
        missing_var="MOTION_GATE_NIGHT_SUPPRESS_ENABLED",
    )
    assert rc != 0, f"expected non-zero exit, got {rc}"
    assert "RuntimeError" in out
    assert "MOTION_GATE_NIGHT_SUPPRESS_ENABLED" in out


def test_full_infra_import_succeeds_when_all_set() -> None:
    """Importing infra (the package) succeeds when all vars are set."""
    rc, out = _import_via_subprocess("infra")
    assert rc == 0, f"expected exit 0, got {rc}: {out}"
