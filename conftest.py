"""Top-level pytest conftest — resolve PROJECT_ROOT to the repo root for tests.

The runtime default in infra/paths.py is ~/ai_camera_monitor (the public
install location), but tests run from the source checkout and need the
project root resolved to that checkout, not the install path.

Override via FARMSURV_PROJECT_ROOT at pytest invocation if you need a
different root (rare — only when running integration tests against a
deployed install).
"""
import os
import sys

# Add repo root to sys.path so `import infra` works from any test subdir.
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Set PROJECT_ROOT for tests so MOTION_RECIPE_FILE / DATA_DIR etc.
# resolve to the checkout, not ~/ai_camera_monitor.
os.environ.setdefault("FARMSURV_PROJECT_ROOT", _REPO_ROOT)
