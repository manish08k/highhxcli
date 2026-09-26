"""Python logging configuration with secret redaction."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from highhx.security.secrets import Redactor

LOGGER_NAME = "highhx"


class RedactingFilter(logging.Filter):
    """Redacts secrets from log records before they are emitted."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor.redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(
    *,
    verbose: bool = False,
    debug: bool = False,
    log_file: Path | None = None,
    redactor: Redactor | None = None,
) -> logging.Logger:
    """Configure the ``highhx`` logger. Idempotent."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    redacting = RedactingFilter(redactor or Redactor())

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if debug else logging.INFO if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s" if debug else "%(message)s"))
    console.addFilter(redacting)
    logger.addHandler(console)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
        except OSError:
            file_handler = None
        if file_handler is not None:
            file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            file_handler.addFilter(redacting)
            logger.addHandler(file_handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Child logger under ``highhx``."""
    if name.startswith(LOGGER_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
