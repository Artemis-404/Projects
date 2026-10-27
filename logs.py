"""Logging setup shared by every UQAB process (NFR-11)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(
    role: str,
    log_dir: Path | str | None = None,
    level: int = logging.INFO,
    *,
    to_stdout: bool = True,
) -> logging.Logger:
    """Configure the root logger for one process and return its named logger."""
    handlers: list[logging.Handler] = []
    if to_stdout:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(FORMAT))
        handlers.append(stream)
    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(directory / f"{role}.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(FORMAT))
        handlers.append(file_handler)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    logger = logging.getLogger(f"uqab.{role}")
    logger.debug("logging configured for %s", role)
    return logger
