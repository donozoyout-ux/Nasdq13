"""
Utility helpers
"""
import time
import asyncio
import json
from typing import Any, Callable
from datetime import datetime, timezone


def utc_now() -> datetime:
    """Get current UTC time as timezone-aware datetime"""
    return datetime.now(timezone.utc)


def is_market_open(now: Optional[datetime] = None) -> bool:
    """Check if US market is open (simplified: Mon-Fri 9:30-16:00 ET)"""
    now = now or utc_now()
    et = now.astimezone(timezone.utc)
    # Convert to ET (UTC-4 for EDT, UTC-5 for EST) - simplified to UTC-4
    # For a real deployment, use a proper TZ lib
    weekday = et.weekday()
    hour = et.hour
    minute = et.minute

    # Rough approximation: 13:30-20:00 UTC = 9:30-16:00 ET
    if weekday >= 5:  # Saturday/Sunday
        return False
    if hour == 13 and minute >= 30:
        return True
    if 14 <= hour <= 19:
        return True
    if hour == 20 and minute == 0:
        return True
    return False


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
