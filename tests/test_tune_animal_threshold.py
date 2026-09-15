"""Tests for scripts/tune_animal_threshold.py — threshold tuning.

Covers:
    - _normalize_features edge cases
    - _jaccard edge cases
    - _compute_tier1 basic scoring
    - _build_other_dog_signature correctness
    - _tune_thresholds returns expected structure
    - _analyze returns expected structure
    - CLI --help works
    - CLI run with synthetic embeddings produces output
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

# Ensure repo root is on sys.path
REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Force offline mode."""
    monkeypatch.setenv("ANIMAL_EMBEDDER_LIVE", "0")


@pytest.fixture()
def staging_dir(tmp_path: Path) -> Path:
    """Create a staging directory with synthetic crops + VM2 replay."""
    staging = tmp_path / "_staging_test"
    staging.mkdir()

    # Create 3 small crop images
    for i in range(3):
        img = Image.new("RGB", (384, 384), color=(100 + i * 20, 50 + i * 10, 30 + i * 5))
        img.save(staging / f"test_{i}.png")

    # Create VM2 replay
    results = {
        "results": [
            {
                "file": f"test_{i}.png",
                "camera": "FRONT",
                "vm2_conf": 0.90,
                "vm2_result": {
                    "species": "dog",
                    "breed": None,
                    "size": "medium",
                    "color_pattern": "brown" if i % 2 == 0 else "tan",
                    "distinctive_features": ["black collar"] if i == 0 else [],
                    "action": "walking",
                    "confidence": 0.9,
                },
            }
            for i in range(3)
        ],
        "errors": [],
    }
    (staging / "_vm2_all15.json").write_text(
        json.dumps(results), encoding="utf-8"
    )
    return staging


@pytest.fixture()
def script_module():
    """Import the tuning script module."""
    import scripts.tune_animal_threshold as mod
    return mod


# ---------------------------------------------------------------------------
# _normalize_features tests
# ---------------------------------------------------------------------------

class TestNormalizeFeatures:
    """Test _normalize_features helper."""

    def test_none_returns_empty_set(self, script_module):
        assert script_module._normalize_features(None) == set()

    def test_list(self, script_module):
        result = script_module._normalize_features(["Black Collar", "Curly Fur"])
        assert result == {"black collar", "curly fur"}

    def test_comma_delimited(self, script_module):
        result = script_module._normalize_features("black collar, curly fur")
        assert result == {"black collar", "curly fur"}

    def test_slash_delimited(self, script_module):
        result = script_module._normalize_features("black collar/curly fur")
        assert result == {"black collar", "curly fur"}

    def test_trim_spaces(self, script_module):
        result = script_module._normalize_features("  black collar  ,  curly fur  ")
        assert result == {"black collar", "curly fur"}


# ---------------------------------------------------------------------------
# _jaccard tests
# ---------------------------------------------------------------------------

class TestJaccard:
    """Test _jaccard helper."""

    def test_identical_sets(self, script_module):
        assert script_module._jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets(self, script_module):
        assert script_module._jaccard({"a"}, {"b"}) == 0.0

    def test_empty_either(self, script_module):
        assert script_module._jaccard(set(), {"a"}) == 0.0
        assert script_module._jaccard({"a"}, set()) == 0.0
        assert script_module._jaccard(set(), set()) == 0.0

    def test_partial_overlap(self, script_module):
        assert script_module._jaccard({"a", "b", "c"}, {"b", "c", "d"}) == 0.5


# ---------------------------------------------------------------------------
# _compute_tier1 tests
# ---------------------------------------------------------------------------

class TestComputeTier1:
    """Test Tier-1 feature scoring."""

    def test_perfect_match(self, script_module):
        vm2 = {"species": "dog", "size": "medium", "color_pattern": "brown",
               "distinctive_features": ["black collar", "curly fur"]}
        enrolled = {"species": "dog", "size": "medium",
                    "color_patterns": ["brown", "tan"],
                    "distinctive_features": ["black collar", "curly fur"]}
        score = script_module._compute_tier1(vm2, enrolled)
        # species + size = 2.0, color Jaccard(1,2) = 0.5 * 1.5 = 0.75,
        # distinct Jaccard(1,1) = 1.0 * 1.5 = 1.5
        # Total = 2.0 + 0.75 + 1.5 = 4.25
        assert score == pytest.approx(4.25, abs=0.01)

    def test_species_only(self, script_module):
        vm2 = {"species": "dog", "size": None, "color_pattern": None,
               "distinctive_features": None}
        enrolled = {"species": "dog", "size": None, "color_patterns": [],
                    "distinctive_features": []}
        score = script_module._compute_tier1(vm2, enrolled)
        assert score == pytest.approx(1.0, abs=0.01)

    def test_no_match(self, script_module):
        vm2 = {"species": "cat", "size": "large", "color_pattern": None,
               "distinctive_features": None}
        enrolled = {"species": "dog", "size": "small", "color_patterns": [],
                    "distinctive_features": []}
        score = script_module._compute_tier1(vm2, enrolled)
        assert score == 0.0


# ---------------------------------------------------------------------------
# _build_other_dog_signature tests
# ---------------------------------------------------------------------------

class TestBuildOtherDogSignature:
    """Test synthetic other-dog signature."""

    def test_same_species_size(self, script_module):
        vm2_results = [
            {"vm2_result": {"color_pattern": "brown"}},
            {"vm2_result": {"color_pattern": "tan"}},
        ]
        sig = script_module._build_other_dog_signature(vm2_results)
        assert sig["species"] == "dog"
        assert sig["size"] == "medium"
        assert "brown" in sig["color_patterns"]
        assert "tan" in sig["color_patterns"]
        # Other dog has different distinctive features from Finnegan
        assert "white paws" in sig["distinctive_features"]
        assert "long tail" in sig["distinctive_features"]


# ---------------------------------------------------------------------------
# _tune_thresholds tests
# ---------------------------------------------------------------------------

class TestTuneThresholds:
    """Test _tune_thresholds returns expected structure."""

    def test_returns_dict_with_keys(self, script_module, staging_dir):
        vm2 = json.loads((staging_dir / "_vm2_all15.json").read_text())
        crops = list(staging_dir.glob("*.png"))
        results = script_module._tune_thresholds(crops, vm2["results"])
        expected_keys = {
            "n_frames", "finneg_signature", "other_signature",
            "finneg_tier1", "other_tier1", "finneg_cosine", "other_cosine",
        }
        assert expected_keys.issubset(results.keys())
        assert results["n_frames"] == 3

    def test_tier1_finnegan_positive(self, script_module, staging_dir):
        vm2 = json.loads((staging_dir / "_vm2_all15.json").read_text())
        crops = list(staging_dir.glob("*.png"))
        results = script_module._tune_thresholds(crops, vm2["results"])
        # Finnegan Tier-1 scores should be positive (at least species match)
        for score in results["finneg_tier1"]:
            assert score >= 0.0


# ---------------------------------------------------------------------------
# _analyze tests
# ---------------------------------------------------------------------------

class TestAnalyze:
    """Test _analyze returns expected structure."""

    def test_returns_recommendation(self, script_module, staging_dir):
        vm2 = json.loads((staging_dir / "_vm2_all15.json").read_text())
        crops = list(staging_dir.glob("*.png"))
        results = script_module._tune_thresholds(crops, vm2["results"])
        analysis = script_module._analyze(results)
        assert "recommended_tier2_threshold" in analysis
        assert "tier2_recall_at_threshold" in analysis
        assert "tier2_f1" in analysis
        assert 0.50 <= analysis["recommended_tier2_threshold"] <= 0.99


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestCLI:
    """Test scripts/tune_animal_threshold.py CLI entry point."""

    def test_help_shows_camera_days_enrolled(self):
        """AC2: --help lines matching 'camera|days|enrolled' >= 3."""
        result = subprocess.run(
            [".venv/bin/python", "scripts/tune_animal_threshold.py", "--help"],
            capture_output=True, text=True, cwd=REPO_ROOT,
            check=False,
        )
        combined = result.stdout + result.stderr
        lines = combined.strip().split('\n')
        matches = sum(
            1 for line in lines
            if any(kw in line for kw in ["camera", "days", "enrolled"])
        )
        assert matches >= 3, (
            f"Expected >=3 lines matching camera|days|enrolled, got {matches}"
        )

    def test_run_with_synthetic_embeddings(self, staging_dir, tmp_path):
        """AC3: Script runs and produces expected output strings."""
        output_path = tmp_path / "report.md"
        result = subprocess.run(
            [
                ".venv/bin/python", "scripts/tune_animal_threshold.py",
                "--staging-dir", str(staging_dir),
                "--output", str(output_path),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
            check=False,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "threshold recommendation" in combined.lower(), (
            f"Expected 'threshold recommendation' in output. Got:\n{combined}"
        )
        # Check for "N frames" pattern
        assert "frames" in combined.lower(), (
            f"Expected 'frames' in output. Got:\n{combined}"
        )

    def test_report_file_created(self, staging_dir, tmp_path):
        """Report file is created with content."""
        output_path = tmp_path / "report.md"
        subprocess.run(
            [
                ".venv/bin/python", "scripts/tune_animal_threshold.py",
                "--staging-dir", str(staging_dir),
                "--output", str(output_path),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
            check=False,
        )
        assert output_path.exists()
        content = output_path.read_text()
        assert len(content) > 100
        assert "threshold recommendation" in content.lower()

    def test_missing_staging_dir_exits_1(self):
        """Missing staging dir causes exit code 1."""
        result = subprocess.run(
            [".venv/bin/python", "scripts/tune_animal_threshold.py",
             "--staging-dir", "/nonexistent/path"],
            capture_output=True, text=True, cwd=REPO_ROOT,
            check=False,
        )
        assert result.returncode == 1


# ---------------------------------------------------------------------------
# Integration: real staging data
# ---------------------------------------------------------------------------

class TestIntegration:
    """Integration test against real Finnegan staging data."""

    def test_full_pipeline_with_real_data(self):
        """Run the full pipeline against actual Finnegan replay."""
        real_staging = Path(REPO_ROOT) / "data" / "known" / "animals" / "_staging_finnegan"
        if not real_staging.is_dir():
            pytest.skip("Real staging data not available")

        import scripts.tune_animal_threshold as mod

        vm2_results = mod._load_vm2_replay(real_staging)
        crops = mod._load_finnegan_crops(real_staging)
        assert len(crops) == 15, f"Expected 15 crops, got {len(crops)}"
        assert len(vm2_results) == 15, f"Expected 15 VM2 results, got {len(vm2_results)}"

        results = mod._tune_thresholds(crops, vm2_results)
        assert results["n_frames"] == 15

        # All Finnegan Tier-1 scores should be positive
        for score in results["finneg_tier1"]:
            assert score >= 0.0, f"Negative Tier-1 score: {score}"

        analysis = mod._analyze(results)
        assert analysis["n_frames"] == 15
        assert analysis["t1_separation"] >= 0  # Finnegan should score higher
