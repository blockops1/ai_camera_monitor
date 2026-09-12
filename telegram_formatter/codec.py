"""codec.py — JPEG encode helper for Telegram photo delivery.

Reads a source image (PNG/JPEG/etc.) and writes a JPEG at q=88,
max-dimension 1920px to a cache directory on disk.

STATUS: new (US-030a)
THREAD SAFETY: single-threaded (called from daemon request thread)

INPUTS:
    - source_path: path to the source image on disk
    - cache_dir: path to the target cache directory (created if missing)
    - jpeg_quality: JPEG quality factor, default 88
    - max_dim: maximum width or height in pixels, default 1920

OUTPUTS:
    - Path to the cached JPEG file on disk
    - Returns None if conversion fails (caller logs and retries)

PUBLIC API:
    encode_jpeg(source_path, cache_dir, jpeg_quality=88, max_dim=1920) -> str | None

DOES NOT DO:
    - Modify the source file (read-only)
    - Delete old cache files (cleanup is separate)
    - Generate thumbnails or previews
    - Write to /tmp or any path outside cache_dir

CALLS INTO:
    - PIL.Image (for open, resize, save)
    - pathlib.Path (for mkdir)
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def encode_jpeg(
    source_path: str,
    cache_dir: str,
    *,
    jpeg_quality: int = 88,
    max_dim: int = 1920,
) -> str | None:
    """Convert source image to JPEG and cache on disk.

    Reads the source image, resizes (if necessary) so that neither
    dimension exceeds *max_dim*, and saves as JPEG with quality
    *jpeg_quality* into *cache_dir*.

    The cache directory is created (parents=True, exist_ok=True).
    If the source file does not exist or conversion fails, logs a
    warning and returns None.

    Args:
        source_path: Path to the source image (PNG, JPEG, etc.).
        cache_dir: Directory to write the JPEG into.
        jpeg_quality: JPEG quality factor (0-100). Default 88.
        max_dim: Maximum width or height in pixels. Default 1920.

    Returns:
        Absolute path to the cached JPEG file, or None on failure.
    """
    src = Path(source_path)
    if not src.is_file():
        log.warning("codec: source file not found: %s", source_path)
        return None

    try:
        from PIL import Image

        img = Image.open(src)
        # Convert to RGB if necessary (RGBA/PA/P modes don't support JPEG)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Resize to fit within max_dim x max_dim
        w, h = img.size
        if w > max_dim or h > max_dim:
            ratio = min(max_dim / w, max_dim / h)
            new_w = max(1, int(w * ratio))
            new_h = max(1, int(h * ratio))
            img = img.resize((new_w, new_h), 1)  # LANCZOS=1

        # Ensure cache directory exists
        dst_dir = Path(cache_dir)
        dst_dir.mkdir(parents=True, exist_ok=True)

        # Write JPEG
        dst = dst_dir / (src.stem + ".jpg")
        img.save(dst, format="JPEG", quality=jpeg_quality, optimize=False)
        img.close()

        log.debug(
            "codec: converted %s -> %s (%dx%d, q=%d, %d bytes)",
            source_path,
            dst,
            img.size[0],
            img.size[1],
            jpeg_quality,
            dst.stat().st_size,
        )
        return str(dst)

    except Exception as exc:  # noqa: BLE001
        log.error("codec: failed to convert %s: %s", source_path, exc)
        return None
