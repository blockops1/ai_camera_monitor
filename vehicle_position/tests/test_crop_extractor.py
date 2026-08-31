"""Unit tests for crop_extractor helpers."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


from vehicle_position.crop_extractor import (
    DEFAULT_TOP_N,
    best_crop_path,
    crop_paths_for_identifier,
    extract_crops_for_identifier,
    primary_avg_area,
    primary_trajectory,
)
from vehicle_position.motion_detector import (
    MovingObject,
    PositionResult,
)


def _pos_with_primary_crop_paths():
    primary = MovingObject(
        bbox_per_frame=[(10, 20, 30, 40)],
        trajectory=["B2"],
        avg_area=1200,
        frames_seen=3,
        best_crop_path="/tmp/crops/abc_crop_0.jpg",
        crop_paths=[
            "/tmp/crops/abc_crop_0.jpg",
            "/tmp/crops/abc_crop_1.jpg",
            "/tmp/crops/abc_crop_2.jpg",
        ],
    )
    return PositionResult(
        moving_objects=[primary],
        primary_moving_object=primary,
        best_crop_path="/tmp/crops/abc_crop_0.jpg",
        crop_paths=[
            "/tmp/crops/abc_crop_0.jpg",
            "/tmp/crops/abc_crop_1.jpg",
            "/tmp/crops/abc_crop_2.jpg",
        ],
        no_motion_detected=False,
    )


def _pos_no_motion():
    return PositionResult(no_motion_detected=True)


def _pos_with_top_level_only():
    """No primary, but top-level crop_paths is set."""
    return PositionResult(
        best_crop_path="/tmp/crops/x_crop_0.jpg",
        crop_paths=["/tmp/crops/x_crop_0.jpg"],
        no_motion_detected=False,
    )


def test_default_top_n_is_three():
    assert DEFAULT_TOP_N == 3


# --- crop_paths_for_identifier ----------------------------------------------


def test_crop_paths_uses_primary_crop_paths():
    pos = _pos_with_primary_crop_paths()
    paths = crop_paths_for_identifier(pos)
    assert len(paths) == 3
    assert paths[0] == "/tmp/crops/abc_crop_0.jpg"


def test_crop_paths_caps_at_top_n():
    pos = _pos_with_primary_crop_paths()
    paths = crop_paths_for_identifier(pos, top_n=2)
    assert len(paths) == 2


def test_crop_paths_top_n_zero_returns_empty():
    pos = _pos_with_primary_crop_paths()
    paths = crop_paths_for_identifier(pos, top_n=0)
    assert paths == []


def test_crop_paths_falls_back_to_top_level_when_no_primary():
    pos = _pos_with_top_level_only()
    paths = crop_paths_for_identifier(pos)
    assert paths == ["/tmp/crops/x_crop_0.jpg"]


def test_crop_paths_falls_back_to_best_crop_path():
    pos = PositionResult(
        best_crop_path="/tmp/crops/y_crop_0.jpg",
        no_motion_detected=False,
    )
    paths = crop_paths_for_identifier(pos)
    assert paths == ["/tmp/crops/y_crop_0.jpg"]


def test_crop_paths_no_motion_returns_empty():
    pos = _pos_no_motion()
    paths = crop_paths_for_identifier(pos)
    assert paths == []


def test_crop_paths_dedupes_preserving_order():
    pos = PositionResult(
        primary_moving_object=MovingObject(
            crop_paths=["/a", "/b", "/a", "/c", "/b"],
        ),
        no_motion_detected=False,
    )
    paths = crop_paths_for_identifier(pos)
    assert paths == ["/a", "/b", "/c"]


def test_crop_paths_top_n_larger_than_available():
    pos = _pos_with_primary_crop_paths()
    paths = crop_paths_for_identifier(pos, top_n=10)
    assert len(paths) == 3


# --- best_crop_path ---------------------------------------------------------


def test_best_crop_path_uses_primary():
    pos = _pos_with_primary_crop_paths()
    assert best_crop_path(pos) == "/tmp/crops/abc_crop_0.jpg"


def test_best_crop_path_falls_back_to_top_level():
    pos = _pos_with_top_level_only()
    assert best_crop_path(pos) == "/tmp/crops/x_crop_0.jpg"


def test_best_crop_path_returns_none_if_no_crops():
    pos = _pos_no_motion()
    assert best_crop_path(pos) is None


# --- extract_crops_for_identifier (Path objects) ---------------------------


def test_extract_crops_returns_path_objects():
    pos = _pos_with_primary_crop_paths()
    paths = extract_crops_for_identifier(pos)
    assert all(isinstance(p, Path) for p in paths)
    assert len(paths) == 3


def test_extract_crops_respects_top_n():
    pos = _pos_with_primary_crop_paths()
    paths = extract_crops_for_identifier(pos, top_n=1)
    assert len(paths) == 1


def test_extract_crops_empty_when_no_motion():
    pos = _pos_no_motion()
    paths = extract_crops_for_identifier(pos)
    assert paths == []


# --- primary_trajectory + primary_avg_area ----------------------------------


def test_primary_trajectory_returns_labels():
    pos = _pos_with_primary_crop_paths()
    assert primary_trajectory(pos) == ["B2"]


def test_primary_trajectory_empty_when_no_motion():
    pos = _pos_no_motion()
    assert primary_trajectory(pos) == []


def test_primary_avg_area_returns_value():
    pos = _pos_with_primary_crop_paths()
    assert primary_avg_area(pos) == 1200


def test_primary_avg_area_zero_when_no_motion():
    pos = _pos_no_motion()
    assert primary_avg_area(pos) == 0
