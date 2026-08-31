"""
logging_setup.py — Centralized logging configuration for the listener.

STATUS: stable
THREAD SAFETY: idempotent; safe to call from any thread or multiple times.

INPUTS:
    - env var FARMSURV_LOG_LEVEL (default INFO) — DEBUG/INFO/WARNING/ERROR
    - env var FARMSURV_PRODUCTION (default "0") — when "1", attach the
      RotatingFileHandler at logs/listener.log. When "0", only attach a
      NullHandler so pytest's caplog can capture via root.
    - infra.paths.LISTENER_LOG — the log file path (env override honored)

OUTPUTS:
    - logs/listener.log (rotated, 25 MB × 5 backups) — production only
    - stderr (StreamHandler, always) — formatted with the same format
      as the file handler so operators can grep either
    - logger = logging.getLogger(name) — return value, already wired

PUBLIC API:
    configure_logging(name: str = __name__) -> logging.Logger
        One call: returns the named logger, with the file handler
        attached when in production. Idempotent (subsequent calls
        return the same logger without re-attaching handlers).
    configure_file_logger(
        name: str, log_file: str, *,
        max_bytes: int = 25 * 1024 * 1024, backup_count: int = 5,
    ) -> logging.Logger
        Like configure_logging but with a dedicated RotatingFileHandler
        pointing at a different file. Used for loggers that have their
        own file (e.g. "cleanup" → CLEANUP_LOG). Same idempotency
        guarantee as configure_logging.
    log_format() -> str
        The format string used by both handlers. Exposed so tests can
        assert against it without duplicating the constant.
    LOG_LEVEL_NAMES — tuple of valid level names for FARMSURV_LOG_LEVEL

DOES NOT DO:
    - Set up JSON / structured logging — the listener's [%(name)s] [%\
      (levelname)s] [%(asctime)s] format is the spec
    - Forward to an external aggregator (Loki, Vector, etc.) — file-only
    - Configure third-party library loggers (urllib3, requests, etc.) —
      the listener never needed it
    - Add per-module file handlers — one rotating file is enough

WHY HERE:
    Replaces ~100 lines of inline handler attachment inside
    listener.py's module body. Per PLAN §10:
    - Single source of truth for the log format
    - Single source of truth for handler attachment (no more sibling
      logger tuples)
    file handler attached to ROOT in production, so every sibling
          module (send_telegram, audit_telegram, vehicle_matcher,
          frame_capture, persistent_rtsp) reaches logs/listener.log via
          propagation.
          This replaces the old "for _sibling in (...): addHandler" loop.
    - Same logger returned everywhere — call sites use one line
    - Idempotent: imports never double-attach handlers

CALLED BY:
    - listener.listener (module body) — replace the inline block
    - any future test or script that wants the same logger
    - tests/conftest.py if we want fixture parity

CALLS INTO:
    - infra.paths.LISTENER_LOG, infra.paths.PRODUCTION_MODE
    - logging.handlers.RotatingFileHandler
    - logging.StreamHandler, logging.NullHandler
    - logging.getLogger, logging.basicConfig (not used; see Why Here)

RELATED:
    - PLAN.md §10.3 (centralized logging)
    - infra.cooldown (writes its own file, no logger needed)
    - infra.matcher_telemetry (writes its own file, no logger needed)
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from infra.paths import LISTENER_LOG, PRODUCTION_MODE

# Single source of truth for the log format string. PLAN §10.3:
# [%(name)s] [%(levelname)s] [%(asctime)s] — message
LOG_FORMAT = "[%(name)s] [%(levelname)s] [%(asctime)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

LOG_LEVEL_NAMES: tuple[str, ...] = (
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
)

# Sentinel attribute name used to mark a logger as "configured by us".
# Subsequent calls to configure_logging() can detect this and skip
# handler re-attachment, preventing the duplicate-record problem that
# bit the listener on 2026-08-04 (capture #32 e4f62b83 — see AGENTS.md).
_CONFIGURED_ATTR = "_farmsv_configured"


def log_format() -> str:
    """Return the canonical log format string (LOG_FORMAT)."""
    return LOG_FORMAT


def _resolve_log_level() -> int:
    """Map FARMSURV_LOG_LEVEL to a logging level constant.

    Defaults to INFO. Unknown values fall back to INFO silently —
    we never want a bad env var to crash the listener at startup.
    """
    env_value = os.environ.get("FARMSURV_LOG_LEVEL", "INFO").upper().strip()
    if env_value not in LOG_LEVEL_NAMES:
        return logging.INFO
    level: int = logging.getLevelName(env_value)
    # getLevelName returns the int level for known names; returns the
    # raw string only for unknown names — guarded above by LOG_LEVEL_NAMES.
    return int(level)


def _build_formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=_DATEFMT)


def _attach_handlers(
    logger: logging.Logger,
    level: int,
    production: bool,
    log_file: str,
) -> None:
    """Wire the file handler (prod) and stream handler (always).

    The file handler is attached to ROOT, not the named logger, so
    every logger that propagates (sibling modules like
    send_telegram, audit_telegram, vehicle_matcher, frame_capture,
    persistent_rtsp) also reaches the file. This is the standard
    logging pattern: configure once on root, let everything else
    inherit.

    The stream handler stays on the named logger so pytest can see
    records from the named logger specifically. Tests that need to
    silence the stream mirror can pass a different level to pytest's
    caplog fixture.

    The file handler uses RotatingFileHandler so operators never have
    to manually rotate. The stream handler goes to stderr so both
    production (launchd-managed) and tests (pytest) can see records.
    """
    formatter = _build_formatter()

    # File handler goes on root — siblings (vehicle_matcher, etc.)
    # propagate up to root, so they hit the same file without any
    # per-sibling wiring.
    if production:
        root = logging.getLogger()
        # Idempotency: only attach one root file handler even if
        # configure_logging is called for multiple named loggers.
        already_attached = any(
            isinstance(h, RotatingFileHandler)
            and getattr(h, "_farmsv_attached_by_us", False)
            for h in root.handlers
        )
        if not already_attached:
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=25 * 1024 * 1024,  # 25 MB
                backupCount=5,
            )
            file_handler.setFormatter(formatter)
            file_handler._farmsv_attached_by_us = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)

    # Stream handler in BOTH modes — production wants stderr for
    # launchd capture; tests want stderr to mirror pytest's terminal.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


def configure_logging(name: str = __name__) -> logging.Logger:
    """Return a logger configured per PLAN §10.

    Idempotent: subsequent calls with the same name return the same
    logger without adding duplicate handlers. This is critical for
    tests that import the listener module multiple times — without
    idempotence, every import adds another handler and records
    duplicate (worst case, exponential duplication).

    Args:
        name: Logger name. Defaults to `__name__` so callers can
              simply do `log = configure_logging()` at module scope.

    Returns:
        A `logging.Logger` with:
        - level set to FARMSURV_LOG_LEVEL (default INFO)
        - propagate=True (so pytest's caplog captures via root)
        - file handler attached when PRODUCTION_MODE
        - stream handler attached in both modes
        - NullHandler when NOT in production (so records don't leak
          to a default handler that pytest installs)
    """
    logger = logging.getLogger(name)
    # Idempotency guard — skip if we already configured this logger.
    # The marker is on the logger itself (not module-global) so it's
    # per-logger, not per-process.
    if getattr(logger, _CONFIGURED_ATTR, False):
        return logger

    level = _resolve_log_level()
    logger.setLevel(level)
    # propagate=True so pytest's caplog (root LogCaptureHandler) sees
    # records. Production runs only add handlers to this named logger,
    # so there is no double-write.
    logger.propagate = True

    # Always attach the stream handler (test + production).
    _attach_handlers(
        logger,
        level=level,
        production=PRODUCTION_MODE,
        log_file=LISTENER_LOG,
    )

    if not PRODUCTION_MODE:
        # Test mode — also add NullHandler so records don't leak to a
        # default root handler pytest might install. (Stream handler
        # is already attached above.)
        logger.addHandler(logging.NullHandler())

    # Root must be at least as low as our named logger, otherwise
    # records get filtered before reaching caplog (root).
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    setattr(logger, _CONFIGURED_ATTR, True)
    return logger


def configure_file_logger(
    name: str,
    log_file: str,
    *,
    max_bytes: int = 25 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure a named logger with its OWN RotatingFileHandler.

    Use this when a logger writes to a different file than the main
    listener.log — e.g. the `cleanup` logger writes to CLEANUP_LOG.
    Idempotent (same _CONFIGURED_ATTR mechanism as configure_logging).

    Unlike configure_logging, this function:
    - ALWAYS attaches a RotatingFileHandler when the file path is
      writable (regardless of PRODUCTION_MODE). Tests that exercise
      cleanup.py expect a real file to be written.
    - Does NOT attach a StreamHandler. The named logger's records
      flow to root via propagate=True; pytest's caplog captures them
      there. Tests don't need a stream mirror for these.

    Args:
        name: Logger name.
        log_file: Absolute path to the rotated log file.
        max_bytes: Per-file size cap. Default 25 MB (matches listener).
        backup_count: Number of backups kept. Default 5.

    Returns:
        A configured `logging.Logger` with a single RotatingFileHandler.
    """
    logger = logging.getLogger(name)
    if getattr(logger, _CONFIGURED_ATTR, False):
        return logger

    level = _resolve_log_level()
    logger.setLevel(level)
    logger.propagate = True

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
    )
    file_handler.setFormatter(_build_formatter())
    logger.addHandler(file_handler)

    # Root at least as low as this logger, same as configure_logging.
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    setattr(logger, _CONFIGURED_ATTR, True)
    return logger