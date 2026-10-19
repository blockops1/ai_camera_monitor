"""Tests for scripts/verify_recipe_live.py -- mocked, no real camera calls."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from scripts.verify_recipe_live import _scrub_sensitive

# CAMERA_META in scripts/verify_recipe_live.py uses [REDACTED-LAN-IP] as a
# placeholder. If the production deployment overrides the cam IPs via
# VERIFY_RECIPE_TEST_FRONT_IP (or the source fixture is changed), the test
# expectation follows. This is the same env-driven pattern as the other
# test files (FARM_TEST_FRONT_IP / FARM_TEST_BACK_IP).
_TEST_FRONT_IP = os.environ.get("VERIFY_RECIPE_TEST_FRONT_IP", "[REDACTED-LAN-IP]")

# ---------------------------------------------------------------------------
# _scrub_sensitive tests
# ---------------------------------------------------------------------------


class TestScrubSensitive:
    """_scrub_sensitive strips sensitive keys from the evidence structure."""

    def test_strips_password(self):
        data: dict = {"password": "s3cret", "name": "front"}
        result = cast(dict, _scrub_sensitive(data))
        assert "password" not in result
        assert result["name"] == "front"

    def test_strips_token_substring(self):
        """'api_token' contains 'token' -> stripped."""
        data: dict = {"api_token": "abc123", "safe": "value"}
        result = cast(dict, _scrub_sensitive(data))
        assert "api_token" not in result
        assert result["safe"] == "value"

    def test_strips_secret(self):
        data: dict = {"secret": "mysecret", "id": "cam1"}
        result = cast(dict, _scrub_sensitive(data))
        assert "secret" not in result
        assert result["id"] == "cam1"

    def test_strips_key_substring(self):
        """'private_key' contains 'key' -> stripped."""
        data: dict = {"private_key": "xxx", "key_label": "cam"}
        result = cast(dict, _scrub_sensitive(data))
        assert "private_key" not in result
        assert "key_label" not in result

    def test_strips_pass_substring(self):
        """'pass' is a substring pattern -> stripped."""
        data: dict = {"pass": "p@ss", "good": "ok"}
        result = cast(dict, _scrub_sensitive(data))
        assert "pass" not in result
        assert result["good"] == "ok"

    def test_strips_passwd_substring(self):
        """'passwd' contains 'pass' -> stripped."""
        data: dict = {"passwd": "p@sswd", "safe": "yes"}
        result = cast(dict, _scrub_sensitive(data))
        assert "passwd" not in result
        assert result["safe"] == "yes"

    def test_nested_dict_stripped(self):
        data: dict = {"detect_ai": {"password": "nope", "person": {"sensitivity": 50}}}
        result = cast(dict, _scrub_sensitive(data))
        assert "password" not in result["detect_ai"]
        assert result["detect_ai"]["person"]["sensitivity"] == 50

    def test_nested_list_stripped(self):
        """Password key removed from dict in list; empty dict stays."""
        data: dict = {"cameras": [{"password": "x"}, {"safe": "y"}]}
        result = cast(dict, _scrub_sensitive(data))
        assert result == {"cameras": [{}, {"safe": "y"}]}

    def test_no_false_positive_on_stay_time(self):
        """stay_time does not contain any sensitive substring."""
        data: dict = {"detect_ai": {"person": {"stay_time": 2, "sensitivity": 50}}}
        result = cast(dict, _scrub_sensitive(data))
        assert result["detect_ai"]["person"]["stay_time"] == 2


# ---------------------------------------------------------------------------
# Helpers for main integration tests
# ---------------------------------------------------------------------------


def _make_detect_config(stay_time: int = 2, sensitivity: int = 50) -> MagicMock:
    cfg = MagicMock()
    cfg.sensitivity = sensitivity
    cfg.person_stay_time = stay_time
    cfg.vehicle_stay_time = stay_time
    cfg.dog_cat_stay_time = stay_time
    return cfg


def _make_verify_result(camera: str, match: bool = True,
                        diffs: list[str] | None = None) -> MagicMock:
    r = MagicMock()
    r.camera = camera
    r.match = match
    r.diffs = diffs or []
    return r


def _detect_ai_get_side(cam_stays: dict[str, int]):
    """Return a callable for client.detect_ai_get that returns different configs per camera."""
    def _side(atype: str, camera: str, **_kw: object) -> MagicMock:
        stay = cam_stays.get(camera, 2)
        return _make_detect_config(stay_time=stay, sensitivity=50)
    return _side


def _verify_side(cam_match_map: dict[str, bool],
                 cam_diff_map: dict[str, list[str]]):
    """Return a callable for apply_recipe / verify_recipe that returns per-camera results."""
    def _side(recipe_path: str, camera: str, **_kw: object) -> list[MagicMock]:
        match = cam_match_map.get(camera, True)
        diffs = cam_diff_map.get(camera, [])
        return [_make_verify_result(camera, match=match, diffs=diffs)]
    return _side


# ---------------------------------------------------------------------------
# Main integration tests
# ---------------------------------------------------------------------------


class TestVerifyRecipeLiveMain:
    """End-to-end flow: mocked ReolinkClient, verify JSON shape and exit code."""

    @patch("scripts.verify_recipe_live.ReolinkClient")
    @patch("scripts.verify_recipe_live.apply_recipe")
    @patch("scripts.verify_recipe_live.verify_recipe")
    def test_all_match(self, mock_verify, mock_apply, mock_client_cls, tmp_path: Path):
        """All 6 cameras match -> exit 0, evidence match=True."""
        all_cams = ("front", "back", "outside_front_garage",
                     "outside_front_solar", "outside_front_power", "outside_back_solar")
        cam_stays = {cam: 2 for cam in all_cams}
        cam_match = {cam: True for cam in all_cams}
        cam_diffs = {cam: [] for cam in all_cams}

        client = MagicMock()
        mock_client_cls.return_value = client
        client.detect_ai_get.side_effect = _detect_ai_get_side(cam_stays)
        mock_verify.side_effect = _verify_side(cam_match, cam_diffs)
        mock_apply.side_effect = _verify_side(cam_match, cam_diffs)

        tmp_path = tmp_path / "evidence.json"

        with (patch("scripts.verify_recipe_live.EVIDENCE_PATH", str(tmp_path)),
              patch("scripts.verify_recipe_live._PROJECT_ROOT") as root_mock):
            root_mock.__truediv__.return_value.is_file.return_value = False
            import scripts.verify_recipe_live as m
            m.RECIPE_PATH = "/fake/recipe.toml"
            m.EVIDENCE_PATH = str(tmp_path)
            code = m.main()

        assert code == 0
        with open(tmp_path) as fh:
            evidence = cast(dict, json.load(fh))
        assert evidence["summary"]["matched"] == 6
        assert evidence["summary"]["mismatched"] == 0
        assert all(c["match"] for c in evidence["cameras"])
        assert evidence["prd"] == "PHASE-V2-065"
        assert len(evidence["cameras"]) == 6

        with open(tmp_path) as fh:
            content = fh.read()
        for kw in ("password", "token", "secret", "key", "pass", "passwd"):
            assert not re.search(r'"' + kw + r'"\s*:', content, re.IGNORECASE), \
                f"Found '{kw}' in evidence"

    @patch("scripts.verify_recipe_live.ReolinkClient")
    @patch("scripts.verify_recipe_live.apply_recipe")
    @patch("scripts.verify_recipe_live.verify_recipe")
    def test_one_mismatch(self, mock_verify, mock_apply, mock_client_cls, tmp_path: Path):
        """One camera mismatch -> exit 1, summary has mismatched=1."""
        all_cams = ("front", "back", "outside_front_garage",
                     "outside_front_solar", "outside_front_power", "outside_back_solar")
        cam_stays = {cam: 2 for cam in all_cams}
        cam_match = {cam: True for cam in all_cams}
        cam_match["outside_back_solar"] = False
        cam_diffs = {cam: [] for cam in all_cams}
        cam_diffs["outside_back_solar"] = ["outside_back_solar: stay_time mismatch"]

        client = MagicMock()
        mock_client_cls.return_value = client
        client.detect_ai_get.side_effect = _detect_ai_get_side(cam_stays)
        mock_verify.side_effect = _verify_side(cam_match, cam_diffs)
        mock_apply.side_effect = _verify_side(cam_match, cam_diffs)

        tmp_path = tmp_path / "evidence.json"

        with (patch("scripts.verify_recipe_live.EVIDENCE_PATH", str(tmp_path)),
              patch("scripts.verify_recipe_live._PROJECT_ROOT") as root_mock):
            root_mock.__truediv__.return_value.is_file.return_value = False
            import scripts.verify_recipe_live as m
            m.RECIPE_PATH = "/fake/recipe.toml"
            m.EVIDENCE_PATH = str(tmp_path)
            code = m.main()

        assert code == 1
        with open(tmp_path) as fh:
            evidence = cast(dict, json.load(fh))
        assert evidence["summary"]["matched"] == 5
        assert evidence["summary"]["mismatched"] == 1

    @patch("scripts.verify_recipe_live.ReolinkClient")
    @patch("scripts.verify_recipe_live.apply_recipe")
    @patch("scripts.verify_recipe_live.verify_recipe")
    def test_evidence_has_before_after(self, mock_verify, mock_apply, mock_client_cls, tmp_path: Path):
        """Evidence JSON contains before and after for each camera."""
        cam_stays = {"front": 2}
        client = MagicMock()
        mock_client_cls.return_value = client
        client.detect_ai_get.side_effect = _detect_ai_get_side(cam_stays)
        mock_verify.side_effect = _verify_side({"front": True}, {"front": []})
        mock_apply.side_effect = _verify_side({"front": True}, {"front": []})

        tmp_path = tmp_path / "evidence.json"

        with (patch("scripts.verify_recipe_live.EVIDENCE_PATH", str(tmp_path)),
              patch("scripts.verify_recipe_live._PROJECT_ROOT") as root_mock):
            root_mock.__truediv__.return_value.is_file.return_value = False
            import scripts.verify_recipe_live as m
            m.RECIPE_PATH = "/fake/recipe.toml"
            m.EVIDENCE_PATH = str(tmp_path)
            m.main()

        with open(tmp_path) as fh:
            evidence = cast(dict, json.load(fh))
        front = next(c for c in evidence["cameras"] if c["camera"] == "front")
        assert "before" in front
        assert "after" in front
        assert "detect_ai" in front["before"]
        assert "detect_ai" in front["after"]
        assert "person" in front["before"]["detect_ai"]
        assert "vehicle" in front["before"]["detect_ai"]
        assert "dog_cat" in front["before"]["detect_ai"]

    @patch("scripts.verify_recipe_live.ReolinkClient")
    @patch("scripts.verify_recipe_live.apply_recipe")
    @patch("scripts.verify_recipe_live.verify_recipe")
    def test_evidence_has_camera_metadata(self, mock_verify, mock_apply, mock_client_cls, tmp_path: Path):
        """Evidence includes ip and model per camera."""
        cam_stays = {"front": 2}
        client = MagicMock()
        mock_client_cls.return_value = client
        client.detect_ai_get.side_effect = _detect_ai_get_side(cam_stays)
        mock_verify.side_effect = _verify_side({"front": True}, {"front": []})
        mock_apply.side_effect = _verify_side({"front": True}, {"front": []})

        tmp_path = tmp_path / "evidence.json"

        with (patch("scripts.verify_recipe_live.EVIDENCE_PATH", str(tmp_path)),
              patch("scripts.verify_recipe_live._PROJECT_ROOT") as root_mock):
            root_mock.__truediv__.return_value.is_file.return_value = False
            import scripts.verify_recipe_live as m
            m.RECIPE_PATH = "/fake/recipe.toml"
            m.EVIDENCE_PATH = str(tmp_path)
            m.main()

        with open(tmp_path) as fh:
            evidence = cast(dict, json.load(fh))
        front = next(c for c in evidence["cameras"] if c["camera"] == "front")
        assert front["ip"] == _TEST_FRONT_IP
        assert front["model"] == "RLC-833A"
