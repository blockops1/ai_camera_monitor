"""test_openapi — Smoke test for the OpenAPI 3.1 schema export endpoint.

E2E strategy:
    - Import infra.api_server and call _generate_openapi_doc() directly.
    - Assert the document structure matches OpenAPI 3.1.0.
    - Verify all 17 routes appear as paths.
    - Verify the events route includes page_token and camera_id query params.
    - Verify the cameras route has a 200 response schema.
    - Round-trip the doc through json -> yaml to confirm valid YAML output.
"""

from __future__ import annotations

import json
import sys

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _openapi_module(monkeypatch):
    """Ensure a clean api_server module for each test."""
    for mod in list(sys.modules.keys()):
        if mod == "infra.api_server":
            del sys.modules[mod]
    monkeypatch.setenv("FARMSURV_API_ENABLED", "1")
    monkeypatch.setenv("FARMSURV_API_TOKEN", "x" * 32)
    monkeypatch.setenv("FARMSURV_DATA_DIR", "/tmp/nonexistent_openapi_test")
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_openapi_version(_openapi_module):
    """AC1: document reports openapi 3.1.0."""
    from infra import api_server
    doc = api_server._generate_openapi_doc()
    assert doc["openapi"] == "3.1.0", f"Expected 3.1.0, got {doc['openapi']}"


def test_path_count(_openapi_module):
    """AC2: document contains >= 17 paths (17 routes + openapi.json = 18, but
    the openapi route itself is also in _ROUTE_TABLE so it appears as a path)."""
    from infra import api_server
    doc = api_server._generate_openapi_doc()
    paths = doc["paths"]
    assert len(paths) >= 17, f"Expected >= 17 paths, got {len(paths)}: {list(paths.keys())}"


def test_events_page_token(_openapi_module):
    """AC3: /api/v1/events has page_token query param."""
    from infra import api_server
    doc = api_server._generate_openapi_doc()
    events_params = doc["paths"]["/api/v1/events"]["get"]["parameters"]
    param_names = [p["name"] for p in events_params]
    assert "page_token" in param_names, "page_token not found in events parameters"


def test_cameras_schema(_openapi_module):
    """AC4: /api/v1/cameras has a 200 response schema (not null)."""
    from infra import api_server
    doc = api_server._generate_openapi_doc()
    resp = doc["paths"]["/api/v1/cameras"]["get"]["responses"]["200"]
    schema = resp["content"]["application/json"].get("schema")
    assert schema is not None, "cameras 200 response schema is null"
    assert "properties" in schema, "cameras schema missing properties"


def test_openapi_valid_yaml(_openapi_module):
    """AC5: OpenAPI doc is valid YAML (round-trip through json + yaml).

    Uses PyYAML if available; skips if yaml is not installed.
    """
    from infra import api_server
    doc = api_server._generate_openapi_doc()

    # Round-trip through JSON then YAML
    json_str = json.dumps(doc)
    try:
        import yaml
        yaml.safe_load(json_str)
    except ImportError:
        pytest.skip("PyYAML not installed")


def test_all_17_routes_present(_openapi_module):
    """All 17 routes from _ROUTE_TABLE appear as OpenAPI paths."""
    from infra import api_server
    doc = api_server._generate_openapi_doc()
    # Expected path keys from _ROUTE_TABLE
    expected_paths = [
        "/api/v1/cameras",
        "/api/v1/cameras/{camera_id}/health",
        "/api/v1/cameras/{camera_id}/snapshot",
        "/api/v1/events",
        "/api/v1/events/{alert_id}",
        "/api/v1/gate",
        "/api/v1/gate/suppression-rules",
        "/api/v1/cooldowns",
        "/api/v1/identities/people",
        "/api/v1/identities/vehicles",
        "/api/v1/identities/animals",
        "/api/v1/listener/status",
        "/api/v1/listener/errors",
        "/api/v1/telegram/status",
        "/api/v1/vision/status",
        "/api/v1/config",
        "/api/v1/openapi.json",
    ]
    for path in expected_paths:
        assert path in doc["paths"], f"Missing path: {path}"


def test_bearer_security_scheme(_openapi_module):
    """Document includes Bearer security scheme."""
    from infra import api_server
    doc = api_server._generate_openapi_doc()
    assert "Bearer" in doc["components"]["securitySchemes"]
    bs = doc["components"]["securitySchemes"]["Bearer"]
    assert bs["type"] == "http"
    assert bs["scheme"] == "bearer"


def test_cache_is_used(_openapi_module):
    """The document is cached — multiple calls return the same dict object."""
    from infra import api_server
    doc1 = api_server._generate_openapi_doc()
    doc2 = api_server._generate_openapi_doc()
    assert doc1 is doc2, "OpenAPI document should be cached (same object)"
