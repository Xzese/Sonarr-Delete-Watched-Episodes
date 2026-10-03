"""Per-invocation rotating file logging, configured only by the CLI."""

import logging
from datetime import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path, PureWindowsPath

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


ROUTINE_RETENTION_REASONS = (
    "retained: genre policy.",
    "retained: unwatched, in progress or inside retention.",
    "retained: ambiguous, protected, in progress or outside watch policy.",
    "retained: missing user metadata.",
    "retained: not every episode has eligible watch evidence.",
)


def report_events(report):
    """Describe actions and actionable problems, omitting routine retention."""
    if report.get("status") == "stopped":
        yield logging.ERROR, f"Cleanup stopped: {report['reason']}"
        return
    for reason in dict.fromkeys(report.get("exclusions", [])):
        if not reason.endswith(ROUTINE_RETENTION_REASONS):
            yield logging.WARNING, reason

    results = report.get("results", [])
    for result in results:
        identity = result.get("identity") or {}
        path = identity.get("path")
        label = PureWindowsPath(path).name if path else f"Sonarr file {result['file_id']}"
        status = result["status"]
        if status == "preview":
            yield (
                logging.INFO,
                f"Would delete and unmonitor: {label} ({identity.get('size', 0)} bytes).",
            )
        elif status == "deleted":
            yield (
                logging.INFO,
                f"Deleted and unmonitored: {label} ({identity.get('size', 0)} bytes).",
            )
        elif status == "not_attempted":
            yield (
                logging.ERROR,
                f"Not attempted: {label}; cleanup stopped after an earlier failure.",
            )
        else:
            details = result.get("reason", "Inspect remote state before another apply run.")
            if status == "unconfirmed":
                details = (
                    f"phase {result.get('phase', 'unknown')}; "
                    f"{result.get('unmonitor_calls_confirmed', 0)} unmonitor calls confirmed. {details}"
                )
            yield logging.ERROR, f"Cleanup {status}: {label}; {details}"

    failures = sum(
        result["status"] in {"blocked", "unconfirmed", "not_attempted"} for result in results
    )
    if report["mode"] == "preview":
        yield (
            logging.INFO,
            (
                f"Dry run complete: {report['planned_files']} file(s) would be deleted "
                f"({report['planned_bytes']} bytes)."
            ),
        )
    else:
        deleted = [result for result in results if result["status"] == "deleted"]
        size = sum((result.get("identity") or {}).get("size", 0) for result in deleted)
        summary = f"Cleanup {'incomplete' if failures else 'complete'}: deleted {len(deleted)} file(s) ({size} bytes)."
        if failures:
            summary += f" {failures} file(s) blocked, unconfirmed or not attempted."
        yield logging.ERROR if failures else logging.INFO, summary


def log_report(logger, report, *, console=False):
    for level, message in report_events(report):
        # Remote names and paths must not create fake multiline log entries.
        message = message.replace("\r", " ").replace("\n", " ")
        if logger is not None:
            logger.log(level, "%s", message)
        if console:
            print(f"[{logging.getLevelName(level)}] {message}")
