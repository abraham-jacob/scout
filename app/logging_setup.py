"""Application logging configured from profiles/config.toml [logging].

Two log files live under the configured directory, both rotated daily at
midnight (rotated files get a date suffix, e.g. scout.log.2026-07-03):

- ``scout.log`` — the application log. Both the web app and the agent runner
  (a separate process) write to it; appends are line-buffered so interleaving
  is safe for a single-user app.
- ``model_calls.log`` — one human-readable block per Claude call (timestamp,
  pass, model, then the full system prompt and user message verbatim). Only
  written when a run is started with model-call logging enabled (the web UI
  checkbox / the runner's --log-model-calls flag).
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.config import BASE_DIR, load_config

APP_LOG_NAME = "scout"
MODEL_LOG_NAME = "scout.model_calls"

# Rotated daily files kept before the oldest is deleted.
LOG_BACKUP_DAYS = 30


def _resolve_log_dir() -> Path:
    """Return the configured log directory as an absolute path, creating it.

    ``~`` is expanded; relative paths are resolved against the project root.
    """
    log_dir = Path(load_config().log_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = BASE_DIR / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _daily_handler(path: Path, fmt: str) -> TimedRotatingFileHandler:
    """Build a midnight-rotating file handler with the given line format."""
    handler = TimedRotatingFileHandler(
        path, when="midnight", backupCount=LOG_BACKUP_DAYS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(fmt))
    return handler


def _is_configured(logger: logging.Logger) -> bool:
    """True when our file handler is already attached.

    Checked by handler type rather than `logger.handlers` truthiness because
    test runners (pytest's caplog) inject capture handlers of their own.
    """
    return any(isinstance(h, TimedRotatingFileHandler) for h in logger.handlers)


def setup_logging() -> logging.Logger:
    """Configure and return the application logger (idempotent).

    Safe to call from both the web app and the runner subprocess; a second
    call in the same process returns the already-configured logger unchanged.
    """
    logger = logging.getLogger(APP_LOG_NAME)
    if _is_configured(logger):
        return logger
    logger.addHandler(_daily_handler(
        _resolve_log_dir() / "scout.log",
        "%(asctime)s %(levelname)s [%(processName)s] %(message)s",
    ))
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def get_model_logger() -> logging.Logger:
    """Configure and return the model-interaction logger (idempotent).

    The handler adds no prefix of its own — each message (one pre-formatted
    block per Claude call, built by runner.log_model_call) lands verbatim.
    """
    logger = logging.getLogger(MODEL_LOG_NAME)
    if _is_configured(logger):
        return logger
    logger.addHandler(_daily_handler(
        _resolve_log_dir() / "model_calls.log", "%(message)s"
    ))
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
