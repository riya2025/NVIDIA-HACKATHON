"""Centralized loguru logger configuration.

Every module imports `log` from here so all logging goes through one logger.
"""
from __future__ import annotations

import sys

from loguru import logger

_CONFIGURED = False


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{extra[agent]}</cyan> | <level>{message}</level>"
        ),
        colorize=True,
    )
    logger.add(
        "logs/ai_foundry.log",
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        enqueue=True,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[agent]} | {message}",
    )
    _CONFIGURED = True


# Default `agent` binding so the format string always has the field.
configure_logging()
log = logger.bind(agent="system")
