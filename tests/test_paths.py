"""Tests for infra.paths constants (US-061a)."""

import importlib
import os

import infra.paths


def test_frame_max_bytes_is_20_gb():
    """FRAME_MAX_BYTES must be exactly 20 GB."""
    assert infra.paths.FRAME_MAX_BYTES == 20 * 1024 * 1024 * 1024


def test_farmsurv_cleanup_disabled_default():
    """FARMSURV_CLEANUP_DISABLED defaults to 0."""
    assert infra.paths.FARMSURV_CLEANUP_DISABLED == 0


def test_farmsurv_cleanup_interval_min_default():
    """FARMSURV_CLEANUP_INTERVAL_MIN defaults to 60."""
    assert infra.paths.FARMSURV_CLEANUP_INTERVAL_MIN == 60


def test_farmsurv_cleanup_disabled_env_override():
    """FARMSURV_CLEANUP_DISABLED reads from env var."""
    old = os.environ.get('FARMSURV_CLEANUP_DISABLED')
    os.environ['FARMSURV_CLEANUP_DISABLED'] = '1'
    try:
        importlib.reload(infra.paths)
        assert infra.paths.FARMSURV_CLEANUP_DISABLED == 1
    finally:
        if old is not None:
            os.environ['FARMSURV_CLEANUP_DISABLED'] = old
        else:
            os.environ.pop('FARMSURV_CLEANUP_DISABLED', None)
        importlib.reload(infra.paths)


def test_farmsurv_cleanup_interval_min_env_override():
    """FARMSURV_CLEANUP_INTERVAL_MIN reads from env var."""
    old = os.environ.get('FARMSURV_CLEANUP_INTERVAL_MIN')
    os.environ['FARMSURV_CLEANUP_INTERVAL_MIN'] = '15'
    try:
        importlib.reload(infra.paths)
        assert infra.paths.FARMSURV_CLEANUP_INTERVAL_MIN == 15
    finally:
        if old is not None:
            os.environ['FARMSURV_CLEANUP_INTERVAL_MIN'] = old
        else:
            os.environ.pop('FARMSURV_CLEANUP_INTERVAL_MIN', None)
        importlib.reload(infra.paths)
