"""Tests for scripts/enroll_person.py - storage + CLI round-trip."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Helpers - fixture that isolates each test from real data/ files.
# ---------------------------------------------------------------------------

def _resolve_project_root() -> Path:
    """Resolve the project root, handling git worktree paths."""
    candidate = Path(__file__).resolve()
    for _ in range(10):
        if (candidate / "pyproject.toml").exists():
            return candidate
        gitfile = candidate / ".git"
        if gitfile.exists() and gitfile.is_file():
            gitdir_line = gitfile.read_text().strip()
            if gitdir_line.startswith("gitdir: "):
                gitdir_path = Path(gitdir_line[len("gitdir: "):])
                task_id = gitdir_path.name
                main_repo = gitdir_path.parent.parent
                worktrees_dir = main_repo / ".worktrees"
                for entry in worktrees_dir.iterdir():
                    if entry.is_dir() and entry.name == task_id:
                        return entry
                break
        candidate = candidate.parent
    return Path(__file__).resolve().parents[2]


_project_root_cache: Path | None = None


def _get_project_root() -> Path:
    """Cached project root lookup."""
    global _project_root_cache
    if _project_root_cache is None:
        _project_root_cache = _resolve_project_root()
    return _project_root_cache


@pytest.fixture()
def isolate_people_dir(tmp_path: Path) -> Path:
    """Copy the project's people dir to a temp dir and patch constants."""
    project_root = _get_project_root()
    src_people = project_root / "data" / "known" / "people"

    dst_people = tmp_path / "data" / "known" / "people"
    dst_people.mkdir(parents=True)
    src_json = src_people / "known_people.json"
    if src_json.exists():
        shutil.copy2(src_json, dst_people / "known_people.json")

    return dst_people


def _patch_constants(people_dir: Path) -> None:
    """Temporarily replace module-level constants in enroll_person."""
    import scripts.enroll_person as mod

    mod.PEOPLE_DIR = people_dir
    mod.KNOWN_PEOPLE_JSON = people_dir / "known_people.json"
    mod.EMBEDDINGS_NPZ = people_dir / "embeddings.npz"


def _make_dummy_image(color: tuple[int, int, int] = (128, 64, 32)) -> str:
    """Write a small RGB image and return its path."""
    img = Image.new("RGB", (64, 64), color=color)
    path = f"/tmp/enroll_test_{id(img)}.png"
    img.save(path)
    return path


# ---------------------------------------------------------------------------
# Storage files exist and are valid JSON list
# ---------------------------------------------------------------------------

class TestStorageFiles:
    """Verify data/known/people/ structure matches ACs."""

    def test_known_people_json_exists_and_is_list(self) -> None:
        """known_people.json exists and loads as a list."""
        project_root = _get_project_root()
        json_path = project_root / "data" / "known" / "people" / "known_people.json"

        assert json_path.exists(), "data/known/people/known_people.json must exist"

        with open(json_path) as f:
            data = json.load(f)

        assert isinstance(data, list), "known_people.json must be a JSON list"


# ---------------------------------------------------------------------------
# Round-trip storage tests
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """.npz save+load returns identical array; JSON save+load returns identical dict."""

    def test_npz_roundtrip(self, isolate_people_dir: Path) -> None:
        """Save random vectors to .npz, reload, verify identical."""
        _patch_constants(isolate_people_dir)
        from scripts.enroll_person import _load_embeddings, _save_embeddings

        embeddings = {
            "test__img1.png": np.array([1.0, 2.0, 3.0], dtype=np.float32),
            "test__avg": np.array([1.5, 2.5, 3.5], dtype=np.float32),
        }
        _save_embeddings(embeddings)

        loaded = _load_embeddings()
        assert set(loaded.keys()) == set(embeddings.keys())
        for key, expected in embeddings.items():
            np.testing.assert_array_equal(
                loaded[key], expected,
                err_msg=f"Mismatch for key {key}",
            )

    def test_json_roundtrip(self, isolate_people_dir: Path) -> None:
        """Save a registry entry, reload, verify identical structure."""
        _patch_constants(isolate_people_dir)
        from scripts.enroll_person import _load_registry, _save_registry

        entry = {
            "id": "roundtrip-001",
            "label": "Test Person",
            "identity_markers": ["red-jacket", "beard"],
            "first_enrolled_iso": "2026-01-01T00:00:00+00:00",
            "sample_image_paths": ["/tmp/test.png"],
        }
        _save_registry([entry])

        loaded = _load_registry()
        assert len(loaded) == 1
        assert loaded[0]["id"] == entry["id"]
        assert loaded[0]["label"] == entry["label"]


# ---------------------------------------------------------------------------
# enroll() happy-path test (no real model - synthetic embeddings)
# ---------------------------------------------------------------------------

class TestEnroll:
    """Test the enroll() function end-to-end with synthetic embeddings."""

    def test_enroll_without_images(self, isolate_people_dir: Path) -> None:
        """Enroll a person with no images."""
        _patch_constants(isolate_people_dir)
        from scripts.enroll_person import _load_registry, enroll

        entry = enroll(
            name="John Doe",
            person_id="john-001",
            identity_markers=["red-jacket", "beard", "tall"],
        )

        assert entry["id"] == "john-001"
        assert entry["label"] == "John Doe"
        assert sorted(entry["identity_markers"]) == ["beard", "red-jacket", "tall"]

        registry = _load_registry()
        assert len(registry) == 1
        assert registry[0]["id"] == "john-001"

    def test_enroll_with_images(self, isolate_people_dir: Path) -> None:
        """Enroll with one image - per-image and avg embeddings stored."""
        _patch_constants(isolate_people_dir)
        from scripts.enroll_person import _load_embeddings, enroll

        img_path = _make_dummy_image((42, 64, 128))

        entry = enroll(
            name="Jane Smith",
            person_id="jane-001",
            identity_markers=["blue-hat"],
            image_paths=[img_path],
        )

        assert entry["id"] == "jane-001"
        embeddings = _load_embeddings()
        assert "jane-001__" + os.path.basename(img_path) in embeddings
        assert "jane-001__avg" in embeddings

        img_vec = embeddings["jane-001__" + os.path.basename(img_path)]
        avg_vec = embeddings["jane-001__avg"]
        np.testing.assert_array_almost_equal(img_vec, avg_vec, decimal=5)

    def test_enroll_with_two_images(self, isolate_people_dir: Path) -> None:
        """Enroll with two images - avg = mean of both."""
        _patch_constants(isolate_people_dir)
        from scripts.enroll_person import _load_embeddings, enroll

        img1 = _make_dummy_image((10, 20, 30))
        img2 = _make_dummy_image((40, 50, 60))

        enroll(
            name="Bob",
            person_id="bob-001",
            identity_markers=["cap"],
            image_paths=[img1, img2],
        )

        embeddings = _load_embeddings()
        bname1 = os.path.basename(img1)
        bname2 = os.path.basename(img2)
        key1 = f"bob-001__{bname1}"
        key2 = f"bob-001__{bname2}"
        avg_key = "bob-001__avg"

        v1 = embeddings[key1]
        v2 = embeddings[key2]
        avg = embeddings[avg_key]
        expected_avg = (v1 + v2) / 2.0
        np.testing.assert_array_almost_equal(avg, expected_avg, decimal=5)

    def test_enroll_duplicate_id_fails(self, isolate_people_dir: Path) -> None:
        """Enrolling with a duplicate id must exit(1)."""
        _patch_constants(isolate_people_dir)
        from scripts.enroll_person import enroll

        enroll(name="A", person_id="dup-001", identity_markers=["marker1"])
        with pytest.raises(SystemExit):
            enroll(name="B", person_id="dup-001", identity_markers=["marker2"])


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestCLI:
    """Test scripts/enroll_person.py CLI entry point."""

    def test_help_shows_name_id_images(self, isolate_people_dir: Path) -> None:
        """--help mentions name, id, and images."""
        import subprocess

        project_root = _get_project_root()
        venv_python = str(project_root / ".venv" / "bin" / "python")

        result = subprocess.run(
            [
                venv_python,
                str(project_root / "scripts" / "enroll_person.py"),
                "--help",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            check=False,
        )
        combined = result.stdout + result.stderr
        # Should have at least 3 matches across name/id/images
        matches = sum(1 for kw in ["name", "id", "images"] if kw in combined.lower())
        assert matches >= 3, f"Expected >=3 keyword matches, got {matches}:\n{combined}"

    def test_cli_enroll_creates_entry(self) -> None:
        """CLI invocation creates a registry entry."""
        import subprocess
        import time

        project_root = _get_project_root()
        venv_python = str(project_root / ".venv" / "bin" / "python")

        uid = f"cli-{int(time.time() * 1000)}"

        result = subprocess.run(
            [
                venv_python,
                str(project_root / "scripts" / "enroll_person.py"),
                "--name", "TestCLI",
                "--id", uid,
                "--identity-markers", "test-marker",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            check=False,
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        assert "Enrolled" in result.stdout
        assert uid in result.stdout
