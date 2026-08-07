"""
Utility helpers
"""
import time
import asyncio
import json
from typing import Any, Callable, Optional
from datetime import datetime, timezone

from src.utils.timezone import market_status


def utc_now() -> datetime:
    """Get current UTC time as timezone-aware datetime"""
    return datetime.now(timezone.utc)


def is_market_open(now: Optional[datetime] = None, config: Optional[dict] = None) -> bool:
    """Check if the US market regular session is open (New York time, DST-aware)."""
    config = config or {}
    status = market_status(config, now)
    return status["open"]


def async_retry(retries: int = 3, delay: float = 2.0, backoff: float = 2.0):
    """Decorator to retry async functions"""
    def decorator(func: Callable):
        async def wrapper(*args, **kwargs):
            attempt = 0
            current_delay = delay
            while attempt < retries:
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    if attempt >= retries:
                        raise
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
            return None
        return wrapper
    return decorator


def safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float"""
    try:
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def format_price(value: float) -> str:
    """Format a price with thousands separator"""
    return f"{value:,.2f}"


def truncate_text(text: str, max_len: int = 100) -> str:
    """Truncate text to max_len with ellipsis"""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
