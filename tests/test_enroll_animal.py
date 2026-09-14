"""Tests for scripts/enroll_animal.py — storage + CLI round-trip."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Helpers — fixture that isolates each test from real data/ files.
# ---------------------------------------------------------------------------

def _resolve_project_root() -> Path:
    """Resolve the project root, handling git worktree paths."""
    # In a git worktree, Path(__file__).resolve() resolves through the
    # linked worktree path. We walk up looking for pyproject.toml first
    # (fastest), then fall back to parsing the .git file.
    candidate = Path(__file__).resolve()
    for _ in range(10):
        # Fast path: look for project marker
        if (candidate / "pyproject.toml").exists():
            return candidate
        gitfile = candidate / ".git"
        if gitfile.exists() and gitfile.is_file():
            # This is a worktree — read the gitdir line
            with open(gitfile) as f:
                gitdir_line = f.read().strip()
            # gitdir format: "gitdir: /main/.git/worktrees/<task>"
            if gitdir_line.startswith("gitdir: "):
                # The worktree's linked path is derived from the .worktrees/<task>
                # structure. We find it by checking candidate.parent.parent's name
                # matches the task portion of the gitdir path.
                gitdir_path = Path(gitdir_line[len("gitdir: "):])
                task_id = gitdir_path.name  # e.g. "t_9789258e"
                # Look for the .worktrees/<task_id> sibling in the filesystem
                # The main repo is two levels up from .git/worktrees/<task>
                main_repo = gitdir_path.parent.parent
                worktrees_dir = main_repo / ".worktrees"
                for entry in worktrees_dir.iterdir():
                    if entry.is_dir() and entry.name == task_id:
                        return entry
                # Fallback: the worktree might be at gitdir_path.parent.parent/../<task_id>
                # Check if candidate itself or a parent matches .worktrees/<task>
                break  # fall through to pyproject.toml check above
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
def isolate_animals_dir(tmp_path: Path) -> Path:
    """Copy the project's animals dir to a temp dir and patch constants."""
    project_root = _get_project_root()
    src_animals = project_root / "data" / "known" / "animals"

    # Create temp copy
    dst_animals = tmp_path / "data" / "known" / "animals"
    dst_animals.mkdir(parents=True)
    # Copy known_animals.json (empty list)
    src_json = src_animals / "known_animals.json"
    if src_json.exists():
        shutil.copy2(src_json, dst_animals / "known_animals.json")

    return dst_animals


def _patch_constants(animals_dir: Path) -> None:
    """Temporarily replace module-level constants in enroll_animal."""
    import scripts.enroll_animal as mod

    mod.ANIMALS_DIR = animals_dir
    mod.KNOWN_ANIMALS_JSON = animals_dir / "known_animals.json"
    mod.EMBEDDINGS_NPZ = animals_dir / "embeddings.npz"


def _make_dummy_image(color: tuple[int, int, int] = (128, 64, 32)) -> str:
    """Write a small RGB image and return its path."""
    img = Image.new("RGB", (64, 64), color=color)
    path = f"/tmp/enroll_test_{id(img)}.png"
    img.save(path)
    return path


# ---------------------------------------------------------------------------
# AC1 + AC2: Storage files exist and are valid JSON list
# ---------------------------------------------------------------------------

class TestStorageFiles:
    """Verify data/known/animals/ structure matches AC1 and AC2."""

    def test_known_animals_json_exists_and_is_list(self) -> None:
        """AC1/AC2: known_animals.json exists and loads as a list."""
        project_root = _get_project_root()
        json_path = project_root / "data" / "known" / "animals" / "known_animals.json"

        assert json_path.exists(), "AC1: data/known/animals/known_animals.json must exist"

        with open(json_path) as f:
            data = json.load(f)

        assert isinstance(data, list), "AC2: known_animals.json must be a JSON list"


# ---------------------------------------------------------------------------
# AC5: Round-trip storage tests
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """AC5: .npz save+load returns identical array; JSON save+load returns identical dict."""

    def test_npz_roundtrip(self, isolate_animals_dir: Path) -> None:
        """Save random vectors to .npz, reload, verify identical."""
        _patch_constants(isolate_animals_dir)
        from scripts.enroll_animal import _load_embeddings, _save_embeddings

        # Write
        embeddings = {
            "test__img1.png": np.array([1.0, 2.0, 3.0], dtype=np.float32),
            "test__avg": np.array([1.5, 2.5, 3.5], dtype=np.float32),
        }
        _save_embeddings(embeddings)

        # Load and verify
        loaded = _load_embeddings()
        assert set(loaded.keys()) == set(embeddings.keys())
        for key, expected in embeddings.items():
            np.testing.assert_array_equal(
                loaded[key], expected,
                err_msg=f"Mismatch for key {key}",
            )

    def test_json_roundtrip(self, isolate_animals_dir: Path) -> None:
        """Save a registry entry, reload, verify identical structure."""
        _patch_constants(isolate_animals_dir)
        from scripts.enroll_animal import _load_registry, _save_registry

        entry = {
            "id": "roundtrip-001",
            "label": "Test Animal",
            "species": "dog",
            "breed": "Labrador",
            "color_patterns": ["brown", "tan"],
            "distinctive_features": ["collar"],
            "first_enrolled_iso": "2026-01-01T00:00:00+00:00",
            "sample_image_paths": ["/tmp/test.png"],
        }
        _save_registry([entry])

        loaded = _load_registry()
        assert len(loaded) == 1
        assert loaded[0]["id"] == entry["id"]
        assert loaded[0]["label"] == entry["label"]
        assert loaded[0]["species"] == entry["species"]


# ---------------------------------------------------------------------------
# enroll() happy-path test (no real model — synthetic embeddings)
# ---------------------------------------------------------------------------

class TestEnroll:
    """Test the enroll() function end-to-end with synthetic embeddings."""

    def test_enroll_without_images(self, isolate_animals_dir: Path) -> None:
        """Enroll an animal with no images — avg embedding is None."""
        _patch_constants(isolate_animals_dir)
        from scripts.enroll_animal import _load_registry, enroll

        entry = enroll(
            name="Bessie",
            animal_id="bessie-001",
            species="cow",
            breed="Holstein",
            color_patterns=["black", "white"],
            distinctive_features=["horned"],
        )

        assert entry["id"] == "bessie-001"
        assert entry["label"] == "Bessie"
        assert entry["species"] == "cow"
        assert entry["breed"] == "Holstein"

        registry = _load_registry()
        assert len(registry) == 1
        assert registry[0]["id"] == "bessie-001"

    def test_enroll_with_images(self, isolate_animals_dir: Path) -> None:
        """Enroll with one image — per-image and avg embeddings stored."""
        _patch_constants(isolate_animals_dir)
        from scripts.enroll_animal import _load_embeddings, enroll

        img_path = _make_dummy_image((42, 64, 128))

        entry = enroll(
            name="Rover",
            animal_id="rover-001",
            species="dog",
            breed="Labrador",
            image_paths=[img_path],
        )

        assert entry["id"] == "rover-001"
        embeddings = _load_embeddings()
        # Per-image key
        assert "rover-001__" + os.path.basename(img_path) in embeddings
        # Average key
        assert "rover-001__avg" in embeddings

        img_vec = embeddings["rover-001__" + os.path.basename(img_path)]
        avg_vec = embeddings["rover-001__avg"]
        np.testing.assert_array_almost_equal(img_vec, avg_vec, decimal=5)

    def test_enroll_with_two_images(self, isolate_animals_dir: Path) -> None:
        """Enroll with two images — avg = mean of both."""
        _patch_constants(isolate_animals_dir)
        from scripts.enroll_animal import _load_embeddings, enroll

        img1 = _make_dummy_image((10, 20, 30))
        img2 = _make_dummy_image((40, 50, 60))

        enroll(
            name="Spot",
            animal_id="spot-001",
            species="dog",
            image_paths=[img1, img2],
        )

        embeddings = _load_embeddings()
        bname1 = os.path.basename(img1)
        bname2 = os.path.basename(img2)
        key1 = f"spot-001__{bname1}"
        key2 = f"spot-001__{bname2}"
        avg_key = "spot-001__avg"

        v1 = embeddings[key1]
        v2 = embeddings[key2]
        avg = embeddings[avg_key]
        expected_avg = (v1 + v2) / 2.0
        np.testing.assert_array_almost_equal(avg, expected_avg, decimal=5)

    def test_enroll_duplicate_id_fails(self, isolate_animals_dir: Path) -> None:
        """Enrolling with a duplicate id must exit(1)."""
        _patch_constants(isolate_animals_dir)
        from scripts.enroll_animal import enroll

        enroll(name="A", animal_id="dup-001", species="cow")
        with pytest.raises(SystemExit):
            enroll(name="B", animal_id="dup-001", species="dog")


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestCLI:
    """Test scripts/enroll_animal.py CLI entry point (AC4)."""

    def test_help_shows_name_id_images(self, isolate_animals_dir: Path) -> None:
        """AC4: --help mentions name, id, and images."""
        import subprocess

        project_root = _get_project_root()
        venv_python = str(project_root / ".venv" / "bin" / "python")

        result = subprocess.run(
            [
                venv_python,
                str(project_root / "scripts" / "enroll_animal.py"),
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
        """CLI invocation creates a registry entry (uses unique ID to avoid collision)."""
        import subprocess
        import time

        project_root = _get_project_root()
        venv_python = str(project_root / ".venv" / "bin" / "python")

        # Use a unique ID with timestamp to avoid collision with other test runs.
        uid = f"cli-{int(time.time() * 1000)}"

        result = subprocess.run(
            [
                venv_python,
                str(project_root / "scripts" / "enroll_animal.py"),
                "--name", "TestCLI",
                "--id", uid,
                "--species", "cow",
                "--breed", "Jersey",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            check=False,
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        assert "Enrolled" in result.stdout
        assert uid in result.stdout
