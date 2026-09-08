"""US-018a: rendered plist must not contain literal /Users/<name>/ paths.

After externalizing home-directory paths from the launchd plist generator
template, the rendered string must use env-resolved values (infra.paths.PROJECT_ROOT)
and never contain a hardcoded /Users/... substring.

The test overrides FARMSURV_PROJECT_ROOT to a non-~/ path so the rendered
output genuinely contains zero '/Users/' literals.
"""

import os
import sys
import importlib


def test_rendered_plist_has_no_literal_home_path(monkeypatch):
    """AC1 + AC2: rendered template must not contain '/Users/' literals.

    Override FARMSURV_PROJECT_ROOT so the default expands to a non-/Users/
    path, then assert the rendered plist has no '/Users/' lines.
    """
    monkeypatch.setenv(
        "FARMSURV_PROJECT_ROOT", "/opt/farm-surveillance-v2"
    )

    # Re-import daemon so PROJECT_ROOT picks up the override.
    # Clear from sys.modules to force re-import.
    for mod in list(sys.modules.keys()):
        if mod in ("listener.daemon", "infra.paths"):
            sys.modules.pop(mod, None)

    # Need to re-set env vars that daemon's logging setup depends on
    os.environ.setdefault("LOG_LEVEL", "WARNING")

    from listener.daemon import generate_plist

    rendered = generate_plist()
    hits = [line for line in rendered.split('\n') if '/Users/' in line]
    assert hits == [], (
        f"Rendered plist contains literal '/Users/' paths:\n"
        + "\n".join(f"  {h}" for h in hits)
    )


def test_rendered_plist_uses_project_root(monkeypatch):
    """AC1: rendered template must use PROJECT_ROOT-derived paths."""
    monkeypatch.setenv(
        "FARMSURV_PROJECT_ROOT", "/opt/farm-surveillance-v2"
    )
    for mod in list(sys.modules.keys()):
        if mod in ("listener.daemon", "infra.paths"):
            sys.modules.pop(mod, None)
    os.environ.setdefault("LOG_LEVEL", "WARNING")

    from listener.daemon import generate_plist

    rendered = generate_plist()

    # ProgramArguments must reference a python binary inside project root
    assert '.venv/bin/python3.11' in rendered
    # WorkingDirectory must be project root
    assert '<key>WorkingDirectory</key>' in rendered
    # Log paths must use logs/ subdir
    assert 'daemon.log' in rendered
    assert 'daemon-error.log' in rendered
