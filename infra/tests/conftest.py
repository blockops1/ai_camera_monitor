"""Auto-load conftest — stub playwright for tune_510a argparse tests.

Phase.166 §11.87.4: scripts/tune_510a_motion_sensitivity.py imports
cam_browser at module load, which imports playwright.sync_api. We don't want
to install chromium in the test venv, so this conftest provides a minimal
playwright stub scoped to the test_tune_510a_argparse module only.

If playwright + chromium ARE available, the stub is overwritten by the
real package — pytest collects this conftest first.
"""
import sys
import types


def _stub_playwright_if_missing() -> None:
    if "playwright" in sys.modules:
        return

    pw = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")

    class _Dummy:
        pass

    for name in (
        "Browser",
        "BrowserContext",
        "Page",
        "sync_playwright",
        "TimeoutError",
        "Error",
    ):
        setattr(sync_api, name, _Dummy)

    _impl = types.ModuleType("playwright._impl")
    _impl_errors = types.ModuleType("playwright._impl._errors")

    class _DummyErr(Exception):
        pass

    _impl_errors.Error = _DummyErr

    sys.modules["playwright"] = pw
    sys.modules["playwright.sync_api"] = sync_api
    sys.modules["playwright._impl"] = _impl
    sys.modules["playwright._impl._errors"] = _impl_errors
    pw.sync_api = sync_api


_stub_playwright_if_missing()