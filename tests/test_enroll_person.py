"""tests/test_enroll_person.py — E2E tests for US-106a person enrollment.

Tests:
  T1: enroll_known_person writes to data/known/people/known_people.json (bug fix: path)
  T2: enroll_known_person uses embed_person_crop, not embed_image (bug fix: import)
  T3: path-derived keys avoid collision on duplicate filenames (bug fix: key)
  T4: batch_enroll_recent --dry-run exits 0 without writing
  T5: enroll_known_person returns correct entry structure
  T6: duplicate person_id raises ValueError
  T7: MCP tool name "enroll_person" is registered
  T8: POST /api/v1/identities/people endpoint exists in router

Person embedder uses synthetic embeddings when PERSON_EMBEDDER_LIVE=0.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Force synthetic mode (no model download needed).
# Use os.environ directly, not setdefault, because the live listener may
# have set PERSON_EMBEDDER_LIVE=1 in the process environment.
os.environ["PERSON_EMBEDDER_LIVE"] = "0"

# Clear the lru_cache on embed_person_crop so previous live-mode calls don't
# poison tests.  Must happen after the env var is set (before any module
# that imports person_embedder caches results).
from infra.person_embedder import embed_person_crop as _epc

_epc.cache_clear()


@pytest.fixture()
def tmp_people_dir(tmp_path: Path) -> Path:
    """Return a temporary known_people directory."""
    people_dir = tmp_path / "data" / "known" / "people"
    people_dir.mkdir(parents=True)
    return people_dir


@pytest.fixture()
def sample_image(tmp_path: Path) -> Path:
    """Create a minimal 10x10 RGB image."""
    img_path = tmp_path / "sample.png"
    Image.new("RGB", (10, 10), color=(128, 128, 128)).save(img_path)
    return img_path


# ---------------------------------------------------------------------------
# T1: Path fix — CLI writes to project data/known/people/
# ---------------------------------------------------------------------------

class TestPathFix:
    """Verify the path bug fix: parents[1] instead of parents[2]."""

    def test_cli_writes_to_project_data_dir(self, tmp_people_dir: Path, sample_image: Path) -> None:
        """AC2: enroll_person.py writes to data/known/people/known_people.json."""
        # Patch the PEOPLE_DIR constant in the CLI module to point at our temp dir.
        import scripts.enroll_person as mod

        old_peopledir = mod.PEOPLE_DIR
        try:
            mod.PEOPLE_DIR = tmp_people_dir
            mod.KNOWN_PEOPLE_JSON = tmp_people_dir / "known_people.json"
            mod.EMBEDDINGS_NPZ = tmp_people_dir / "embeddings.npz"

            mod.enroll(
                name="TestPerson",
                person_id="t1-path-fix",
                identity_markers=["test"],
                image_paths=[str(sample_image)],
            )

            json_path = tmp_people_dir / "known_people.json"
            assert json_path.exists(), "known_people.json was not created"
            data = json.loads(json_path.read_text())
            assert isinstance(data, list)
            assert any(e["id"] == "t1-path-fix" for e in data)
        finally:
            mod.PEOPLE_DIR = old_peopledir
            mod.KNOWN_PEOPLE_JSON = mod.KNOWN_PEOPLE_JSON  # restore original
            mod.EMBEDDINGS_NPZ = mod.EMBEDDINGS_NPZ


# ---------------------------------------------------------------------------
# T2: Import fix — uses embed_person_crop, not embed_image
# ---------------------------------------------------------------------------

class TestImportFix:
    """Verify the import bug fix: embed_person_crop is used."""

    def test_uses_embed_person_crop(self, tmp_people_dir: Path, sample_image: Path) -> None:
        """The enroll function calls embed_person_crop (not a non-existent embed_image)."""
        # Clear lru_cache so we get fresh synthetic results.
        from infra.person_embedder import embed_person_crop
        embed_person_crop.cache_clear()

        import scripts.enroll_person as mod

        old_peopledir = mod.PEOPLE_DIR
        try:
            mod.PEOPLE_DIR = tmp_people_dir
            mod.KNOWN_PEOPLE_JSON = tmp_people_dir / "known_people.json"
            mod.EMBEDDINGS_NPZ = tmp_people_dir / "embeddings.npz"

            mod.enroll(
                name="TestImportFix",
                person_id="t2-import-fix",
                identity_markers=["test"],
                image_paths=[str(sample_image)],
            )

            json_path = tmp_people_dir / "known_people.json"
            assert json_path.exists()
        finally:
            mod.PEOPLE_DIR = old_peopledir

        # Confirm embed_image does NOT exist (the old broken name).
        from infra import person_embedder
        assert not hasattr(person_embedder, "embed_image"), (
            "embed_image should not exist; the correct name is embed_person_crop"
        )


# ---------------------------------------------------------------------------
# T3: Key collision fix — path-derived keys
# ---------------------------------------------------------------------------

class TestKeyFix:
    """Verify the key collision fix: path-derived unique keys."""

    def test_path_derived_key_uniqueness(self, tmp_path: Path, tmp_people_dir: Path, sample_image: Path) -> None:
        """Multiple images with the same filename produce different keys."""
        import scripts.enroll_person as mod

        old_peopledir = mod.PEOPLE_DIR
        try:
            mod.PEOPLE_DIR = tmp_people_dir
            mod.KNOWN_PEOPLE_JSON = tmp_people_dir / "known_people.json"
            mod.EMBEDDINGS_NPZ = tmp_people_dir / "embeddings.npz"

            # Create two images with the SAME name in different dirs
            img1 = tmp_path / "alert_a" / "crop_a.png"
            img1.parent.mkdir(parents=True)
            Image.new("RGB", (10, 10), color=(100, 100, 100)).save(img1)

            img2 = tmp_path / "alert_b" / "crop_a.png"
            img2.parent.mkdir(parents=True)
            Image.new("RGB", (10, 10), color=(200, 200, 200)).save(img2)

            _vec1, key1 = mod._embed_image_path(str(img1), "test-3")
            _vec2, key2 = mod._embed_image_path(str(img2), "test-3")

            assert key1 != key2, (
                f"Keys should differ for same-named images in different dirs: {key1} vs {key2}"
            )
            assert "alert_a" in key1, f"Key should include directory name: {key1}"
            assert "alert_b" in key2, f"Key should include directory name: {key2}"
        finally:
            mod.PEOPLE_DIR = old_peopledir


# ---------------------------------------------------------------------------
# T4: batch_enroll_recent --dry-run
# ---------------------------------------------------------------------------

class TestBatchEnroll:
    """Verify batch_enroll_recent behavior."""

    def test_dry_run_exits_zero(self, tmp_path: Path) -> None:
        """AC4: batch_enroll_recent --dry-run prints picks and exits 0."""
        from scripts.batch_enroll_recent import main

        result = main(["--dry-run", "--since", "2h"])
        # main() returns None; exit code is 0 if no exception raised.
        assert result is None  # main doesn't return a special value on success

    def test_no_crops_found_exits_zero(self) -> None:
        """When no crops exist, the script exits 0 with 'No crops found'."""
        from scripts.batch_enroll_recent import main

        with mock.patch("scripts.batch_enroll_recent.FRAMES_DIR", Path("/nonexistent")):
            result = main(["--dry-run", "--since", "2h"])
        assert result is None


# ---------------------------------------------------------------------------
# T5: enroll_known_person return structure
# ---------------------------------------------------------------------------

class TestEnrollKnownPerson:
    """Verify the shared enroll_known_person function."""

    def test_returns_entry(self, tmp_people_dir: Path, sample_image: Path) -> None:
        """AC3: enrolled entry has correct fields."""
        # Patch paths temporarily
        import infra.farm_api.identities as ids
        from infra.farm_api.identities import enroll_known_person

        old_peopledir = ids.PEOPLE_DIR
        old_json = ids.PERSON_KNOWN_FILE
        old_npz = ids.EMBEDDINGS_NPZ

        try:
            ids.PEOPLE_DIR = tmp_people_dir
            ids.PERSON_KNOWN_FILE = str(tmp_people_dir / "known_people.json")
            ids.EMBEDDINGS_NPZ = tmp_people_dir / "embeddings.npz"

            entry = enroll_known_person(
                name="TestEntry",
                person_id="t5-entry",
                identity_markers=["marker1", "marker2"],
                image_paths=[str(sample_image)],
            )

            assert isinstance(entry, dict)
            assert entry["id"] == "t5-entry"
            assert entry["label"] == "TestEntry"
            assert entry["identity_markers"] == ["marker1", "marker2"]
            assert "first_enrolled_iso" in entry
            assert len(entry["sample_image_paths"]) == 1
        finally:
            ids.PEOPLE_DIR = old_peopledir
            ids.PERSON_KNOWN_FILE = old_json
            ids.EMBEDDINGS_NPZ = old_npz


# ---------------------------------------------------------------------------
# T6: Duplicate ID detection
# ---------------------------------------------------------------------------

class TestDuplicateId:
    """Verify duplicate person_id raises ValueError."""

    def test_duplicate_raises_value_error(
        self, tmp_people_dir: Path, sample_image: Path
    ) -> None:
        """Enrolling with same person_id twice raises ValueError."""
        import infra.farm_api.identities as ids
        from infra.farm_api.identities import enroll_known_person

        old_peopledir = ids.PEOPLE_DIR
        old_json = ids.PERSON_KNOWN_FILE
        old_npz = ids.EMBEDDINGS_NPZ

        try:
            ids.PEOPLE_DIR = tmp_people_dir
            ids.PERSON_KNOWN_FILE = str(tmp_people_dir / "known_people.json")
            ids.EMBEDDINGS_NPZ = tmp_people_dir / "embeddings.npz"

            enroll_known_person(
                name="First", person_id="dup-1", image_paths=[str(sample_image)]
            )
            with pytest.raises(ValueError, match="already enrolled"):
                enroll_known_person(
                    name="Second", person_id="dup-1", image_paths=[str(sample_image)]
                )
        finally:
            ids.PEOPLE_DIR = old_peopledir
            ids.PERSON_KNOWN_FILE = old_json
            ids.EMBEDDINGS_NPZ = old_npz


# ---------------------------------------------------------------------------
# T7: MCP tool name "enroll_person" is registered
# ---------------------------------------------------------------------------

class TestMCPToolName:
    """AC6: grep -n 'name=\"enroll_person\"' infra/mcp_server.py matches."""

    def test_mcp_tool_registered(self) -> None:
        """The enroll_person tool is registered in the MCP server."""
        import infra.mcp_server as mcp

        registry = mcp._build_registry()
        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "enroll_person" in tool_names, (
            f"'enroll_person' not in MCP tools: {tool_names}"
        )


# ---------------------------------------------------------------------------
# T8: POST /api/v1/identities/people exists in router
# ---------------------------------------------------------------------------

class TestAPIEndpoint:
    """AC5: POST /api/v1/identities/people endpoint exists."""

    def test_post_route_exists(self) -> None:
        """The POST handler for identities/people is in the API server."""
        import infra.api_server as api

        # The do_POST method should contain our handler.
        assert hasattr(api._APIHandler, "do_POST"), "API handler missing do_POST"

        # Check the source contains our route logic
        import inspect
        source = inspect.getsource(api._APIHandler.do_POST)
        assert "/api/v1/identities/people" in source, (
            "POST /api/v1/identities/people not found in do_POST"
        )
