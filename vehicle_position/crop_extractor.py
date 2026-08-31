"""Crop extraction — get the right crop paths from a PositionResult.

The identifier consumes up to TOP_N_CROPS images of the moving object,
saved by the motion detector. This module picks the right ones for
the identifier and gives the caller a clean interface.

Pure functions over the data the motion detector already produced.
No disk I/O here — the crops are already on disk when detect_motion
returns.
"""

from __future__ import annotations

from pathlib import Path

from .motion_detector import PositionResult

# Default cap. The detector saves up to TOP_N_CROPS=3 (Phase.65).
# The identifier also consumes up to TOP_N_CROPS (vehicle_identifier).
# Keep these in sync if we change either.
DEFAULT_TOP_N: int = 3


def crop_paths_for_identifier(
    position: PositionResult,
    top_n: int = DEFAULT_TOP_N,
) -> list[str]:
    """Return the crop paths the identifier should consume.

    Strategy:
      1. If primary_moving_object has crop_paths, use those (up to top_n).
      2. Else if the PositionResult has top-level crop_paths, use those.
      3. Else fall back to best_crop_path if present.
      4. Else empty list (no motion detected or no crops saved).

    The list is deduplicated and order-preserving.

    Args:
        position: Output of detect_motion().
        top_n: Maximum number of crops to return.

    Returns:
        List of absolute crop paths (strings). Empty if no crops.
        Length <= top_n. Deduplicated.
    """
    paths: list[str] = []
    if top_n <= 0:
        return []

    primary = position.primary_moving_object
    if primary is not None and primary.crop_paths:
        paths.extend(primary.crop_paths)
    elif position.crop_paths:
        paths.extend(position.crop_paths)
    elif position.best_crop_path:
        paths.append(position.best_crop_path)

    # Deduplicate while preserving order.
    seen = set()
    deduped: list[str] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        deduped.append(p)

    return deduped[:top_n]


def best_crop_path(position: PositionResult) -> str | None:
    """Return the single best crop path, or None.

    Convenience for callers that only want one image.
    """
    primary = position.primary_moving_object
    if primary is not None and primary.best_crop_path:
        return primary.best_crop_path
    if position.best_crop_path:
        return position.best_crop_path
    paths = crop_paths_for_identifier(position, top_n=1)
    return paths[0] if paths else None


def extract_crops_for_identifier(
    position: PositionResult,
    top_n: int = DEFAULT_TOP_N,
) -> list[Path]:
    """Like crop_paths_for_identifier, but returns Path objects.

    Convenience for callers that want pathlib.Path semantics.
    """
    return [Path(p) for p in crop_paths_for_identifier(position, top_n=top_n)]


def primary_trajectory(position: PositionResult) -> list[str]:
    """Return the trajectory labels of the primary moving object.

    Returns an empty list if no motion was detected.
    """
    if position.primary_moving_object is None:
        return []
    return list(position.primary_moving_object.trajectory)


def primary_avg_area(position: PositionResult) -> int:
    """Return the avg_area of the primary moving object, 0 if none."""
    if position.primary_moving_object is None:
        return 0
    return position.primary_moving_object.avg_area
