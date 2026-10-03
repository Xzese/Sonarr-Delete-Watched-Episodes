"""Per-invocation rotating file logging, configured only by the CLI."""

import json
import logging
from datetime import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from cleanup_plan import CleanupError


def configure_logging(env):
    log_file = env.get("LOG_FILE", "output/log.txt").strip()
    if not log_file:
        raise CleanupError("LOG_FILE cannot be empty.")
    raw_level = env.get("LOG_LEVEL", "INFO").strip().upper()
    levels = {
        "NOTSET": logging.NOTSET,
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
        "FATAL": logging.CRITICAL,
    }
    if raw_level not in levels:
        raise CleanupError("LOG_LEVEL must be a standard Python logging level.")
    raw_weeks = env.get("LOG_RETENTION_WEEKS", "4")
    if not raw_weeks.isascii() or not raw_weeks.isdigit():
        raise CleanupError("LOG_RETENTION_WEEKS must be a non-negative integer.")
    weeks = int(raw_weeks)
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(
            log_file,
            when="W0",
            interval=1,
            atTime=time(0, 0),
            backupCount=weeks,
            encoding="utf-8",
        )
    except OSError:
        raise CleanupError(
            "Cannot create the configured rotating log file. Cleanup was not started."
        ) from None
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
    # A private logger keeps repeated invocations independent and leaves the
    # root/SDK loggers untouched. No handlers are installed during import.
    logger = logging.Logger("sonarr_delete_watched_episodes", level=levels[raw_level])
    logger.propagate = False
    logger.addHandler(handler)
    return logger, handler


def log_report(logger, report):
    if logger is None:
        return
    failed = report.get("status") == "stopped" or any(
        result["status"] in {"blocked", "unconfirmed", "not_attempted"}
        for result in report.get("results", [])
    )
    logger.log(logging.ERROR if failed else logging.INFO, "%s", json.dumps(report))
