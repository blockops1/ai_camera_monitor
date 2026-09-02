"""
test_animal_pipeline_6B165_4.py — Animal pipeline per-camera cooldowns (PLAN §11.86.4).

STATUS: provisional
THREAD SAFETY: pytest test functions — run sequentially per file.

WHAT THIS TESTS:
    Phase 6B.165 §11.86.4 — animal cooldown entries in
    config/motion_gate_thresholds.json. The contract is:
        - Every camera has "gate_cooldown.animal": int seconds.
        - Approved values (maintainer 2026-08-29):
            CAM5 / CAM3 / CAM4 / CAM6 = 300s (low animal traffic)
            CAM1                     = 180s
            CAM2                     = 60s  (resident cats/dogs)
        - get_gate_cooldown_seconds(camera, "animal") reads the
          per-camera value.
        - Missing key → defaults to the global "default" value
          (from infra/gate_cooldown.py), not zero, not None.
        - is_gate_enabled(camera, "animal") defaults True when the
          gate_enabled field is absent. When gate_enabled.animal=false
          is set, it returns False.
        - is_gate_enabled is generic: it does NOT need a code change
          to honor a new event_type. Tests verify that "animal" is
          treated the same as "person" / "vehicle" / "motion".

WHAT THIS DOES NOT TEST:
    - Qwen vision calls → §11.86.3 (already shipped).
    - Animal matching → §11.86.2 (already shipped).
    - Telegram send → §11.86.6.
    - Known-animal enrollment / registry → §11.86.5.
    - The actual suppression of animal events when inside cooldown —
      covered by general cooldown tests in infra/tests/test_gate_cooldown.py.

DESIGN CHOICES:
    - We assert on the REAL config file's animal values, not a fake.
      The whole point of §11.86.4 is that the production config has
      the right numbers per camera; mocking the config would test
      the parser, not the file.
    - is_gate_enabled tests build a fake config in tmp_path with
      only the animal key, mirroring TestIsGateEnabled's existing
      style (lines 695-825 of listener/tests/test_motion_gate_pipeline.py).
    - We do NOT restart the listener in this phase — user directive
      2026-08-30: "Proceed, but don't restart the listener." This
      file is deployable independent of a listener restart; the
      values get picked up the next time the listener reads the
      config (which happens on every alert via _cached_thresholds
      resolution, see infra/gate_cooldown.py).
"""

import json
from pathlib import Path

import pytest

REAL_CONFIG = Path(__file__).resolve().parents[2] / "config" / "motion_gate_thresholds.json"

# Approved by maintainer on 2026-08-29. See PLAN §11.86.4.
# Phase 6B.167 §13.4 Commit 17 (T3 C17): keys are CAM{N} codes per
# infra.cameras._LEGACY_PREFIX_TO_CODE. CAM3/CAM4/CAM5/CAM6 = 300s
# (long driveway/field cameras, low animal traffic); CAM1 = 180s
# (front-door, animals sometimes wander near); CAM2 = 60s (indoor
# workshop, resident cats/dogs — short cooldown so we don't miss
# return visits).
EXPECTED_ANIMAL_COOLDOWNS = {
    "CAM5": 300,  # was "CAM5"
    "CAM3": 300,  # was "CAM3"
    "CAM4": 300,  # was "CAM4"
    "CAM6": 300,  # was "CAM6"
    "CAM1": 180,  # was "CAM1"
    "CAM2": 60,   # was "CAM2"
}


# ---------------------------------------------------------------------------
# Real config file
# ---------------------------------------------------------------------------


class TestRealConfigAnimalCooldowns:
    """The real motion_gate_thresholds.json must contain the approved
    per-camera animal cooldowns. If you change a value here, you
    need maintainer's explicit approval (PLAN §11.86.4)."""

    @pytest.fixture(scope="class")
    def config(self):
        assert REAL_CONFIG.exists(), (
            f"Config not found at {REAL_CONFIG}. Tests run from the "
            "project root in CI."
        )
        return json.loads(REAL_CONFIG.read_text())

    @pytest.mark.parametrize("camera,expected", list(EXPECTED_ANIMAL_COOLDOWNS.items()))
    def test_camera_has_animal_cooldown(self, config, camera, expected):
        cam_cfg = config.get(camera, {})
        assert "gate_cooldown" in cam_cfg, (
            f"{camera} missing gate_cooldown block"
        )
        gc = cam_cfg["gate_cooldown"]
        assert "animal" in gc, (
            f"{camera}.gate_cooldown missing 'animal' key"
        )
        assert isinstance(gc["animal"], int), (
            f"{camera}.gate_cooldown.animal must be int, got {type(gc['animal']).__name__}"
        )
        assert gc["animal"] == expected, (
            f"{camera}.gate_cooldown.animal = {gc['animal']}, expected {expected}. "
            "Approved values: CAM5/CAM3/CAM4/CAM6=300s, CAM1=180s, CAM2=60s (maintainer 2026-08-29)."
        )

    def test_all_fleet_cameras_have_animal_cooldown(self, config):
        """Every camera that exists in the config must have an animal cooldown."""
        cameras_in_config = [k for k in config if not k.startswith("_")]
        missing = []
        for cam in cameras_in_config:
            gc = config.get(cam, {}).get("gate_cooldown", {})
            if "animal" not in gc:
                missing.append(cam)
        assert not missing, (
            f"Cameras missing gate_cooldown.animal: {missing}. "
            "PLAN §11.86.4 requires ALL cameras to have an animal cooldown entry."
        )

    def test_animal_cooldowns_are_positive_integers(self, config):
        """Cooldowns must be > 0 (a zero/None cooldown would defeat the purpose).
        The contract in infra/gate_cooldown.py says 0 or absent = no cooldown,
        but §11.86.4 explicitly approves positive values for every camera."""
        for cam in config:
            if cam.startswith("_"):
                continue
            gc = config.get(cam, {}).get("gate_cooldown", {})
            animal = gc.get("animal")
            if animal is None:
                continue
            assert animal > 0, (
                f"{cam}.gate_cooldown.animal = {animal} (must be positive int seconds; "
                "0 means no cooldown per infra/gate_cooldown.py)."
            )


# ---------------------------------------------------------------------------
# Cooldown resolver — generic by event_type
# ---------------------------------------------------------------------------


class TestCooldownResolverReadsAnimal:
    """infra.gate_cooldown.get_gate_cooldown_seconds() is generic — it
    accepts any event_type string. These tests verify the 'animal'
    event_type resolves to the per-camera value we put in the config.
    """

    @pytest.fixture(autouse=True)
    def _clear_cooldown_cache(self):
        """infra/gate_cooldown.py caches per-camera state. Reset between tests."""
        from infra import gate_cooldown
        gate_cooldown.clear_all_gate_cooldowns()
        yield
        gate_cooldown.clear_all_gate_cooldowns()

    def test_real_config_resolves_animal_per_camera(self):
        from infra.gate_cooldown import get_gate_cooldown_seconds
        for cam, expected in EXPECTED_ANIMAL_COOLDOWNS.items():
            got = get_gate_cooldown_seconds(cam, "animal")
            assert got == expected, (
                f"get_gate_cooldown_seconds({cam!r}, 'animal') = {got}, expected {expected}"
            )

    def test_animal_event_type_falls_back_to_default(self, monkeypatch, tmp_path):
        """If a camera is missing gate_cooldown.animal, the resolver
        should fall back to gate_cooldown.default (per infra/gate_cooldown.py).
        This guards against the case where someone adds a new camera
        but forgets the animal key — we want a sane default, not 0."""
        from infra import gate_cooldown

        monkeypatch.setattr("infra.paths.PROJECT_ROOT", tmp_path)
        cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({
            "New Camera": {
                "gate_cooldown": {
                    "default": 90,
                    # no "animal" key
                },
            },
        }))

        got = gate_cooldown.get_gate_cooldown_seconds("New Camera", "animal")
        assert got == 90, (
            f"Expected fallback to gate_cooldown.default (90s), got {got}. "
            f"This is the contract from infra/gate_cooldown.py."
        )

    def test_missing_gate_cooldown_block_returns_zero(self, monkeypatch, tmp_path):
        """If a camera has no gate_cooldown block at all, the cooldown
        is 0 (no cooldown). This is the backward-compatible behavior
        documented in PLAN §11.77 and infra/gate_cooldown.py."""
        from infra import gate_cooldown

        monkeypatch.setattr("infra.paths.PROJECT_ROOT", tmp_path)
        cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"Brand New Camera": {"car": 0.5}}))

        got = gate_cooldown.get_gate_cooldown_seconds("Brand New Camera", "animal")
        assert got == 0


# ---------------------------------------------------------------------------
# is_gate_enabled — generic, animal is just another event_type
# ---------------------------------------------------------------------------


class TestIsGateEnabledAnimal:
    """is_gate_enabled() should treat 'animal' exactly like 'person' /
    'vehicle' / 'motion' — no code change required. These tests prove
    the generic contract holds for the animal event_type.
    """

    @pytest.fixture(autouse=True)
    def _reset_thresholds_cache(self):
        """listener.motion_gate_pipeline caches the parsed thresholds."""
        try:
            import listener.motion_gate_pipeline as lmgp
            lmgp._cached_thresholds = None
        except (ImportError, AttributeError):
            pass
        yield
        try:
            import listener.motion_gate_pipeline as lmgp
            lmgp._cached_thresholds = None
        except (ImportError, AttributeError):
            pass

    def test_animal_defaults_true_when_gate_enabled_absent(self, monkeypatch, tmp_path):
        """Camera exists, no gate_enabled field → animal defaults True.

        Phase 6B.167 §13.4 Commit 17: JSON keys are CAM{N} codes per
        infra.cameras._LEGACY_PREFIX_TO_CODE. CAM1 = CAM1.
        """
        monkeypatch.setattr("infra.paths.PROJECT_ROOT", tmp_path)
        cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"CAM1": {"car": 0.4}}))

        try:
            import motion_gate_pipeline  # noqa: F401 — may not be on path
        except ImportError:
            pass
        try:
            import listener.motion_gate_pipeline as lmgp
            lmgp._cached_thresholds = None
        except ImportError:
            pass

        from listener.motion_gate_pipeline import is_gate_enabled

        assert is_gate_enabled("CAM1", "animal") is True

    def test_animal_disabled_via_gate_enabled_matrix(self, monkeypatch, tmp_path):
        """Camera has gate_enabled.animal = false → returns False for animal,
        True for everything else. This is the same pattern as
        TestIsGateEnabled.test_disabled_event_type_returns_false for person."""
        monkeypatch.setattr("infra.paths.PROJECT_ROOT", tmp_path)
        cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({
            "CAM1": {  # §13.4: was "CAM1"
                "gate_enabled": {
                    "vehicle": True,
                    "person": True,
                    "animal": False,
                    "motion": True,
                },
            },
        }))

        try:
            import motion_gate_pipeline  # noqa: F401
        except ImportError:
            pass
        try:
            import listener.motion_gate_pipeline as lmgp
            lmgp._cached_thresholds = None
        except ImportError:
            pass

        from listener.motion_gate_pipeline import is_gate_enabled

        assert is_gate_enabled("CAM1", "animal") is False
        assert is_gate_enabled("CAM1", "person") is True
        assert is_gate_enabled("CAM1", "vehicle") is True
        assert is_gate_enabled("CAM1", "motion") is True

    def test_animal_partial_matrix_defaults_true_for_other_events(
        self, monkeypatch, tmp_path
    ):
        """Camera only disables animal — vehicle/person/motion all default True."""
        monkeypatch.setattr("infra.paths.PROJECT_ROOT", tmp_path)
        cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({
            "CAM3": {"gate_enabled": {"animal": False}},
        }))

        try:
            import motion_gate_pipeline  # noqa: F401
        except ImportError:
            pass
        try:
            import listener.motion_gate_pipeline as lmgp
            lmgp._cached_thresholds = None
        except ImportError:
            pass

        from listener.motion_gate_pipeline import is_gate_enabled

        assert is_gate_enabled("CAM3", "animal") is False
        assert is_gate_enabled("CAM3", "vehicle") is True
        assert is_gate_enabled("CAM3", "person") is True
        assert is_gate_enabled("CAM3", "motion") is True

    def test_real_config_all_cameras_animal_gate_enabled(self):
        """Smoke test: with the real config, all six cameras have
        is_gate_enabled(camera, 'animal') = True. (The config sets
        gate_cooldown but not gate_enabled, so all default True.)
        """
        try:
            import listener.motion_gate_pipeline as lmgp
            lmgp._cached_thresholds = None
        except ImportError:
            pytest.skip("listener.motion_gate_pipeline not importable in this env")

        from listener.motion_gate_pipeline import is_gate_enabled

        for cam in EXPECTED_ANIMAL_COOLDOWNS:
            assert is_gate_enabled(cam, "animal") is True, (
                f"{cam} unexpectedly disabled for animal events. "
                "Check gate_enabled matrix in motion_gate_thresholds.json."
            )


# ---------------------------------------------------------------------------
# Schema — gate_cooldown.animal type contract
# ---------------------------------------------------------------------------


class TestConfigSchemaContract:
    """The contract is: gate_cooldown.animal is int seconds, applied
    per-camera BEFORE the motion gate runs. This block documents the
    contract; if you change the schema, update the JSDoc-style
    comment in motion_gate_thresholds.json and infra/gate_cooldown.py.
    """

    def test_real_config_animal_values_are_int(self):
        config = json.loads(REAL_CONFIG.read_text())
        for cam in EXPECTED_ANIMAL_COOLDOWNS:
            v = config[cam]["gate_cooldown"]["animal"]
            assert isinstance(v, int) and not isinstance(v, bool), (
                f"{cam}.gate_cooldown.animal must be int (got {type(v).__name__}, "
                f"value={v}). Booleans are subclass of int — would silently work "
                "but indicate a schema mistake."
            )

    def test_animal_cooldown_not_zero_for_residential_cameras(self):
        """CAM2 (was CAM2 / CAM2) is the workshop — resident
        cats and dogs frequent it. The approved value is 60s (much
        shorter than motion's 180s) because we WANT short animal
        cooldowns to catch return visits. This test documents that
        intentional choice and guards against someone "fixing" the
        60s to match motion by accident.

        Phase 6B.167 §13.4 Commit 17: real-config lookup uses CAM{N}
        keys per infra.cameras._LEGACY_PREFIX_TO_CODE (CAM2 = CAM2).
        """
        config = json.loads(REAL_CONFIG.read_text())
        cam2_animal = config["CAM2"]["gate_cooldown"]["animal"]  # was "CAM2"
        cam2_motion = config["CAM2"]["gate_cooldown"]["motion"]
        assert cam2_animal == 60, (
            f"CAM2 animal cooldown = {cam2_animal}, expected 60s "
            f"(maintainer 2026-08-29). CAM2 is the workshop; resident "
            f"cats/dogs need short cooldown to catch return visits. "
            f"Don't change this without re-approval."
        )
        assert cam2_motion == 180, (
            f"CAM2 motion cooldown = {cam2_motion}, expected 180s."
        )
        # animal < motion is intentional here (resident pet revisit cadence)
        assert cam2_animal < cam2_motion