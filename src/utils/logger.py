"""
Logger setup using loguru
"""
import sys
import os
from typing import Optional
from loguru import logger


def setup_logger(log_level: str = "INFO", log_file: Optional[str] = None) -> None:
    """Configure the global logger"""
    logger.remove()

    # Console output
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
        colorize=True,
    )

    # File output (optional)
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        logger.add(
            log_file,
            level=log_level,
            rotation="1 day",
            retention="7 days",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            enqueue=True,
        )


def get_logger(name: str):
    """Get a logger instance bound to a module name"""
    return logger.bind(name=name)
